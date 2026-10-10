"""Local end-to-end rehearsal of the signed client update chain.

Builds a release payload from a throwaway clean clone of this repository
(``scripts/build_client_update.py`` is invoked unmodified), serves the payload
from a one-shot ``127.0.0.1`` HTTP channel, and drives the real update state
machine — check → download → verify → extract → install → pending → apply →
confirm — inside an isolated managed-install layout under one stage root.

The real update transport validates every hop against https/443/global-IP
(anti-SSRF by design), so the rehearsal maps the policy-pinned canonical URLs
onto the local channel through the service's injected-transport seam; every
other line of trust, download, and install code runs unmodified.  The
rehearsal signs with locally generated, throwaway Ed25519 keys — production
keys live only in the protected release environment.

Nothing outside the stage root is written and no real release channel is
contacted.  See ``docs/update-chain.md`` for the full map.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import (  # noqa: E402
    ALLOWED_HOSTS,
    DISTRIBUTION_REPOSITORY,
    MANIFEST_ASSET,
    SOURCE_REPOSITORY,
)
from src.update.service import (  # noqa: E402
    REQUIRED_PRODUCTION_GATES,
    UpdateError,
    UpdateService,
)

from nacl.signing import SigningKey  # noqa: E402

DEFAULT_REPOSITORY = DISTRIBUTION_REPOSITORY
REHEARSAL_REPOSITORY = f"{DISTRIBUTION_REPOSITORY}-Rehearsal"
UPDATE_CHANNEL_ENV = "COURSELENS_UPDATE_CHANNEL"
# Canonical gate set, single source of truth = src/update/service.py
# (fail-closed preserved: every gate must be true in the rehearsal record).
PRODUCTION_GATES = REQUIRED_PRODUCTION_GATES
KEY_ID = "update-rehearsal-01"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_url_for(repository: str) -> str:
    return f"https://github.com/{repository}/releases/latest/download/{MANIFEST_ASSET}"


def package_url_for(repository: str, version: str) -> str:
    asset = f"courselens-{version}-windows-x86_64.zip"
    return f"https://github.com/{repository}/releases/download/client-v{version}/{asset}"


class RehearsalLog:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.lines.append(line)
        print(line, flush=True)


class LocalChannel:
    """One-shot 127.0.0.1 HTTP server whose routes the harness mutates."""

    def __init__(self, log: RehearsalLog):
        self.log = log
        self.requests: list[str] = []
        self.routes: dict[str, object] = {}
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                channel.requests.append(f"GET {self.path}")
                target = channel.routes.get(self.path)
                if target is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if isinstance(target, int):
                    self.send_response(target)
                    self.end_headers()
                    return
                body = target if isinstance(target, bytes) else Path(target).read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence default stderr logging
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"

    def serve_forever_background(self) -> None:
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def transport(self, url: str, destination, limit: int):
        """Injected UpdateService transport: canonical URL → local channel."""
        parsed = urllib.parse.urlsplit(url)
        local = f"{self.base}{parsed.path}"
        try:
            with urllib.request.urlopen(local, timeout=10) as response:
                content = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise UpdateError("download_http_error") from exc
        except urllib.error.URLError as exc:
            raise UpdateError("network_unavailable") from exc
        if len(content) > limit:
            raise UpdateError("download_too_large")
        if destination is None:
            return content
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return None


def fresh_clone(repo_root: Path, stage: Path, log: RehearsalLog) -> Path:
    clone = stage / "clone"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "--quiet", str(repo_root), str(clone)],
        check=True, cwd=str(repo_root),
    )
    subprocess.run(
        ["git", "-C", str(clone), "remote", "set-url", "origin",
         f"https://github.com/{SOURCE_REPOSITORY}.git"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    main_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    if head != main_head:
        raise SystemExit(f"clone HEAD {head} does not match source HEAD {main_head}")
    dirty = subprocess.run(
        ["git", "-C", str(clone), "status", "--porcelain"],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit("rehearsal clone must be a clean checkout")
    log(f"clone ready: HEAD={head[:12]} (clean, origin={SOURCE_REPOSITORY})")
    return clone


def build_payload(
    clone: Path, out: Path, repository: str, version: str, release_key: SigningKey,
    old_version: str, log: RehearsalLog,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    release_id = f"rehearsal-{uuid.uuid4().hex[:12]}"
    notes = out / "release-notes.txt"
    notes.write_text(
        "Local rehearsal release. Synthetic payload built from a clean clone; "
        "never published anywhere.\n", encoding="utf-8"
    )
    seed_fd, seed_name = tempfile.mkstemp(prefix="rehearsal-seed-", suffix=".b64")
    os.close(seed_fd)
    seed_file = Path(seed_name)
    try:
        seed_file.write_text(
            base64.b64encode(bytes(release_key)).decode("ascii"), encoding="ascii"
        )
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "build_client_update.py"),
             "--root", str(clone), "--output", str(out),
             "--package-url", package_url_for(repository, version),
             "--release-id", release_id,
             "--key-id", KEY_ID, "--key-epoch", "1",
             "--signing-key-file", str(seed_file),
             "--notes-file", str(notes),
             "--minimum-security-version", old_version],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    finally:
        seed_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(
            "payload build failed (fail-closed scan or provenance):\n"
            + (result.stderr or result.stdout or "").strip()[:2000]
        )
    built = json.loads(result.stdout.strip().splitlines()[-1])
    built["seconds"] = round(time.monotonic() - started, 1)
    built["release_id"] = release_id
    log(f"payload built in {built['seconds']}s: sha256={built['sha256'][:12]}… "
        f"size={Path(built['package']).stat().st_size} bytes")
    return built


def write_trust_policy(install: Path, repository: str, root: SigningKey,
                       release: SigningKey, old_version: str) -> Path:
    now = datetime.now(timezone.utc)
    authorization = {
        "release_key_id": KEY_ID,
        "public_key": base64.b64encode(bytes(release.verify_key)).decode(),
        "key_epoch": 1,
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "status": "active",
        "channels": ["stable"],
        "platforms": ["windows"],
    }
    trust = {
        "schema": "courselens.client-update-trust.v2",
        "enabled": True,
        "channel": "stable",
        "platform": "windows",
        "architecture": "x86_64",
        "minimum_version": old_version,
        "minimum_key_epoch": 1,
        "root_keys": {
            "update-rehearsal-root": base64.b64encode(bytes(root.verify_key)).decode(),
        },
        "release_key_authorizations": [{
            "schema": "courselens.update-key-authorization.v1",
            "authorization": authorization,
            "signature": {
                "root_key_id": "update-rehearsal-root",
                "value": base64.b64encode(root.sign(canonical(authorization)).signature).decode(),
            },
        }],
        "allowed_hosts": ALLOWED_HOSTS,
        "manifest_url": manifest_url_for(repository),
        "distribution": {
            "repository": repository,
            "visibility": "public",
            "auth_model": "none",
            "tag_namespace": "client-v",
            "manifest_asset": MANIFEST_ASSET,
            "source_repository_access": False,
        },
        "production_gates": {name: True for name in PRODUCTION_GATES},
    }
    trust_path = install / "trust" / "client-update-trust.json"
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(json.dumps(trust, indent=2), encoding="utf-8")
    return trust_path


def build_managed_install(stage: Path, repository: str, root: SigningKey,
                          release: SigningKey, old_version: str,
                          clone: Path | None = None) -> Path:
    install = stage / "install"
    (install / "state").mkdir(parents=True, exist_ok=True)
    (install / "state" / "install-layout.json").write_text(
        json.dumps({"schema": "courselens.managed-install.v1"}), encoding="utf-8"
    )
    write_trust_policy(install, repository, root, release, old_version)
    old_slot = install / "versions" / old_version
    if clone is not None:
        # Real previous-slot runtime so a post-rollback child can run the old
        # slot's own code, exactly like the launcher does after a rollback.
        shutil.copytree(clone / "src", old_slot / "src")
    else:
        (old_slot / "src").mkdir(parents=True, exist_ok=True)
    (old_slot / "courselens-version.json").write_text(json.dumps({
        "schema": "courselens.client-version.v1", "version": old_version, "channel": "stable",
    }), encoding="utf-8")
    (old_slot / "src" / "marker.txt").write_text("previous verified slot\n", encoding="utf-8")
    return install


def resign_manifest(envelope: dict, version: str, release_key: SigningKey) -> bytes:
    manifest = dict(envelope["manifest"])
    manifest["version"] = version
    manifest["package"] = {**manifest["package"]}
    signed = {
        "schema": "courselens.client-update.v1",
        "manifest": manifest,
        "signature": {
            "key_id": envelope["signature"]["key_id"],
            "key_epoch": envelope["signature"]["key_epoch"],
            "value": base64.b64encode(
                release_key.sign(canonical(manifest)).signature
            ).decode("ascii"),
        },
    }
    return json.dumps(signed).encode("utf-8")


def corrupt_signature(envelope_bytes: bytes) -> bytes:
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    value = bytearray(base64.b64decode(envelope["signature"]["value"]))
    value[0] ^= 0xFF
    envelope["signature"]["value"] = base64.b64encode(bytes(value)).decode("ascii")
    return json.dumps(envelope).encode("utf-8")


def update_child_code(slot: Path, trust: Path, updates: Path, install: Path,
                      confirm: bool = True) -> str:
    version_file = slot / "courselens-version.json"
    confirm_lines = (
        "health = service.confirm_health(version)\n"
        if confirm
        else "health = {'state': 'skipped'}\n"
    )
    return (
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, r'{slot}')\n"
        "from src.update.service import UpdateService\n"
        f"version = json.loads(open(r'{version_file}', encoding='utf-8').read())['version']\n"
        "service = UpdateService(current_version=version,\n"
        f"    trust_path=Path(r'{trust}'), state_root=Path(r'{updates}'),\n"
        f"    install_root=Path(r'{install}'))\n"
        "applied = service.apply_pending()\n"
        + confirm_lines +
        "print('REHEARSAL-CHILD ' + json.dumps({'slot_version': version,\n"
        "    'applied_state': applied['state'], 'health_state': health['state'],\n"
        "    'current_version': health.get('current_version', '')}))\n"
    )


def run_restart_child(slot: Path, trust: Path, updates: Path, install: Path,
                      confirm: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", update_child_code(slot, trust, updates, install, confirm)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(slot), timeout=120,
    )
    for line in result.stdout.splitlines():
        if line.startswith("REHEARSAL-CHILD "):
            return json.loads(line[len("REHEARSAL-CHILD "):])
    raise SystemExit(
        "restart child produced no rehearsal line; "
        f"rc={result.returncode} stderr={(result.stderr or '')[-800:]}"
    )


def run_rollback_pass(stage: Path, channel: LocalChannel, clone: Path, repository: str,
                      root: SigningKey, release: SigningKey, old_version: str,
                      new_version: str, log: RehearsalLog) -> dict:
    """Staged switch that never confirms health: recover() must roll it back."""
    log("pass rollback: staged switch abandoned before health confirmation")
    work = stage / "rollback"
    work.mkdir(parents=True, exist_ok=True)
    built = build_payload(clone, work / "out", repository, new_version, release, old_version, log)
    install = build_managed_install(work, repository, root, release, old_version, clone=clone)
    updates = work / "data" / "updates"
    package_path = Path(built["package"])
    channel.routes = {
        f"/{repository}/releases/latest/download/{MANIFEST_ASSET}": Path(built["manifest"]),
        f"/{repository}/releases/download/client-v{new_version}/{package_path.name}": package_path,
    }
    drive_update_pass(channel, install, updates, repository, old_version, log)
    trust = install / "trust" / "client-update-trust.json"
    child = run_restart_child(install / "versions" / new_version, trust, updates, install,
                              confirm=False)
    log(f"  apply-only child: {json.dumps(child)}")
    current = json.loads((install / "state" / "current.json").read_text(encoding="utf-8-sig"))
    if not current.get("awaiting_health") or str(current.get("version")) != new_version:
        raise SystemExit(f"expected awaiting_health at {new_version}, got {current}")

    # A fresh service launched later than the 120 s health window must decide
    # the new slot unhealthy and atomically restore the previous slot.
    later = UpdateService(
        current_version=new_version,
        trust_path=trust,
        state_root=updates,
        install_root=install,
        transport=channel.transport,
        now=lambda: time.time() + 121.0,
    )
    snapshot = later.snapshot()
    current = json.loads((install / "state" / "current.json").read_text(encoding="utf-8-sig"))
    ok = (
        snapshot["state"] == "rolled_back"
        and snapshot["error_code"] == "startup_health_failed"
        and str(current.get("version")) == old_version
        and not current.get("awaiting_health")
        and (install / "versions" / old_version).is_dir()
        and (install / "versions" / new_version).is_dir()
    )
    if not ok:
        raise SystemExit(f"rollback pass failed: snapshot={snapshot} current={current}")
    log(f"  rollback: current.json → {current['version']} (state=rolled_back, both slots kept)")
    resumed = run_restart_child(install / "versions" / old_version, trust, updates, install,
                                confirm=True)
    if not (resumed["slot_version"] == old_version and resumed["health_state"] == "healthy"):
        raise SystemExit(f"old-slot resume failed: {resumed}")
    log(f"  resume on old slot: {json.dumps(resumed)}")
    return {
        "pass": "rollback", "repository": repository, "build": built,
        "apply_child": child, "rolled_back_to": old_version, "resume_child": resumed,
        "ok": True,
    }


def drive_update_pass(channel: LocalChannel, install: Path, updates: Path,
                      repository: str, old_version: str, log: RehearsalLog) -> list[dict]:
    trust_path = install / "trust" / "client-update-trust.json"
    service = UpdateService(
        current_version=old_version,
        trust_path=trust_path,
        state_root=updates,
        install_root=install,
        transport=channel.transport,
    )
    trace: list[dict] = []

    def record(stage: str, snapshot: dict) -> dict:
        entry = {"stage": stage, "state": snapshot.get("state"),
                 "error_code": snapshot.get("error_code") or "",
                 "available_version": snapshot.get("available_version") or ""}
        trace.append(entry)
        log(f"  {stage}: state={entry['state']} code={entry['error_code'] or '-'} "
            f"avail={entry['available_version'] or '-'}")
        return entry

    snapshot = record("check", service.check())
    if snapshot["state"] != "available":
        raise SystemExit(f"expected available after check, got {snapshot['state']}")
    snapshot = record("download", service.download())
    if snapshot["state"] != "ready_to_restart":
        raise SystemExit(f"expected ready_to_restart after download, got {snapshot['state']}")
    snapshot = record("install", service.install())
    if snapshot["state"] != "ready_to_restart":
        raise SystemExit(f"expected ready_to_restart after install, got {snapshot['state']}")
    pending = install / "state" / "pending.json"
    if not pending.exists():
        raise SystemExit("pending.json missing after install")
    return trace


def verify_new_slot(install: Path, new_version: str) -> dict:
    slot = install / "versions" / new_version
    files = [path.relative_to(slot).as_posix() for path in slot.rglob("*") if path.is_file()]
    required = ["courselens-package.json", "courselens-version.json", "src/update/service.py"]
    missing = [name for name in required if name not in files]
    if missing:
        raise SystemExit(f"new slot missing required files: {missing}")
    return {"files": len(files), "has_runtime": True}


def run_happy_pass(stage: Path, channel: LocalChannel, clone: Path, repository: str,
                   root: SigningKey, release: SigningKey, old_version: str,
                   new_version: str, log: RehearsalLog, pass_name: str,
                   env_override: str | None) -> dict:
    log(f"pass {pass_name}: repository={repository} env[{UPDATE_CHANNEL_ENV}]="
        f"{env_override if env_override is not None else '<unset>'}")
    work = stage / pass_name
    work.mkdir(parents=True, exist_ok=True)
    built = build_payload(clone, work / "out", repository, new_version, release, old_version, log)
    install = build_managed_install(work, repository, root, release, old_version)
    updates = work / "data" / "updates"

    manifest_path = Path(built["manifest"])
    package_path = Path(built["package"])
    channel.routes = {
        f"/{repository}/releases/latest/download/{MANIFEST_ASSET}": manifest_path,
        f"/{repository}/releases/download/client-v{new_version}/{package_path.name}": package_path,
    }
    previous = os.environ.get(UPDATE_CHANNEL_ENV)
    try:
        if env_override is None:
            os.environ.pop(UPDATE_CHANNEL_ENV, None)
        else:
            os.environ[UPDATE_CHANNEL_ENV] = env_override
        trace = drive_update_pass(channel, install, updates, repository, old_version, log)
    finally:
        if previous is None:
            os.environ.pop(UPDATE_CHANNEL_ENV, None)
        else:
            os.environ[UPDATE_CHANNEL_ENV] = previous

    slot_report = verify_new_slot(install, new_version)
    child = run_restart_child(
        install / "versions" / new_version,
        install / "trust" / "client-update-trust.json", updates, install,
    )
    log(f"  restart(child): {json.dumps(child)}")
    current = json.loads((install / "state" / "current.json").read_text(encoding="utf-8-sig"))
    ok = (
        child["slot_version"] == new_version
        and child["applied_state"] == "applying"
        and child["health_state"] == "healthy"
        and child["current_version"] == new_version
        and str(current.get("version")) == new_version
        and not current.get("awaiting_health")
    )
    if not ok:
        raise SystemExit(f"happy pass {pass_name} failed identity checks: {child} / {current}")
    log(f"  restart: current.json → {current['version']} (healthy, awaiting_health cleared)")
    return {
        "pass": pass_name, "repository": repository, "env": env_override,
        "trace": trace, "build": built, "slot_files": slot_report["files"],
        "child": child, "current": current, "ok": True,
    }


def run_negative_passes(stage: Path, channel: LocalChannel, repository: str,
                        root: SigningKey, release: SigningKey, old_version: str,
                        new_version: str, manifest_path: Path, package_path: Path,
                        log: RehearsalLog) -> list[dict]:
    results = []
    base_envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
    canonical_manifest_path = f"/{repository}/releases/latest/download/{MANIFEST_ASSET}"
    canonical_package_path = (
        f"/{repository}/releases/download/client-v{new_version}/{package_path.name}"
    )

    def new_work(name: str) -> tuple[Path, Path, Path]:
        work = stage / name
        work.mkdir(parents=True, exist_ok=True)
        install = build_managed_install(work, repository, root, release, old_version)
        return work, install, work / "data" / "updates"

    # (a) manifest 404
    _, install, updates = new_work("negative-404")
    channel.routes = {canonical_manifest_path: 404}
    service = UpdateService(current_version=old_version,
                            trust_path=install / "trust" / "client-update-trust.json",
                            state_root=updates, install_root=install,
                            transport=channel.transport)
    snapshot = service.check()
    if snapshot["state"] == "available":
        raise SystemExit("404 pass unexpectedly succeeded")
    results.append({"pass": "negative-404", "state": snapshot["state"],
                    "error_code": snapshot["error_code"]})
    log(f"  negative 404: state={snapshot['state']} code={snapshot['error_code']}")

    # (b) corrupted signature
    _, install, updates = new_work("negative-badsig")
    channel.routes = {canonical_manifest_path: corrupt_signature(manifest_path.read_bytes())}
    service = UpdateService(current_version=old_version,
                            trust_path=install / "trust" / "client-update-trust.json",
                            state_root=updates, install_root=install,
                            transport=channel.transport)
    snapshot = service.check()
    results.append({"pass": "negative-badsig", "state": snapshot["state"],
                    "error_code": snapshot["error_code"]})
    log(f"  negative badsig: state={snapshot['state']} code={snapshot['error_code']}")

    # (c) same version served → up_to_date
    _, install, updates = new_work("negative-sameversion")
    same = resign_manifest(base_envelope, old_version, release)
    channel.routes = {
        canonical_manifest_path: same,
        canonical_package_path: package_path,
    }
    service = UpdateService(current_version=old_version,
                            trust_path=install / "trust" / "client-update-trust.json",
                            state_root=updates, install_root=install,
                            transport=channel.transport)
    snapshot = service.check()
    results.append({"pass": "negative-sameversion", "state": snapshot["state"],
                    "error_code": snapshot["error_code"],
                    "available_version": snapshot["available_version"]})
    log(f"  negative sameversion: state={snapshot['state']} code={snapshot['error_code']}")
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-root", type=Path, default=None,
                        help="stage directory (default: a fresh %TEMP% directory)")
    parser.add_argument("--old-version", default="0.0.9")
    parser.add_argument("--keep", action="store_true", help="keep the stage root for inspection")
    args = parser.parse_args(argv)

    started = time.monotonic()
    log = RehearsalLog()
    repo_version = json.loads(
        (PROJECT_ROOT / "courselens-version.json").read_text(encoding="utf-8")
    )
    new_version = str(repo_version["version"])
    stage = (args.stage_root or Path(tempfile.mkdtemp(prefix="courselens-update-rehearsal-"))).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    log(f"stage root: {stage}")
    log(f"versions: old={args.old_version} → new={new_version} (from HEAD courselens-version.json)")

    root_key = SigningKey.generate()
    release_key = SigningKey.generate()
    channel = LocalChannel(log)
    channel.serve_forever_background()
    log(f"local channel: {channel.base} (127.0.0.1 one-shot, port 0 → {channel.port})")

    report: dict = {"stage": str(stage), "old_version": args.old_version,
                    "new_version": new_version, "passes": [], "negatives": []}
    try:
        clone = fresh_clone(PROJECT_ROOT, stage, log)
        happy = run_happy_pass(
            stage, channel, clone, DEFAULT_REPOSITORY, root_key, release_key,
            args.old_version, new_version, log, "happy-default", env_override=None,
        )
        report["passes"].append(happy)
        happy2 = run_happy_pass(
            stage, channel, clone, REHEARSAL_REPOSITORY, root_key, release_key,
            args.old_version, new_version, log, "happy-override",
            env_override=REHEARSAL_REPOSITORY,
        )
        report["passes"].append(happy2)
        first = happy["build"]
        report["rollback"] = run_rollback_pass(
            stage, channel, clone, DEFAULT_REPOSITORY, root_key, release_key,
            args.old_version, new_version, log,
        )
        report["negatives"] = run_negative_passes(
            stage, channel, DEFAULT_REPOSITORY, root_key, release_key,
            args.old_version, new_version,
            Path(first["manifest"]), Path(first["package"]), log,
        )
        report["requests"] = channel.requests
        report["seconds"] = round(time.monotonic() - started, 1)
        report_path = stage / "rehearsal-report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log(f"REHEARSAL-OK report={report_path} passes={len(report['passes'])} "
            f"negatives={len(report['negatives'])} requests={len(channel.requests)}")
        if not args.keep:
            shutil.rmtree(stage, ignore_errors=True)
            log("stage root removed (re-run with --keep to inspect)")
        return 0
    finally:
        channel.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
