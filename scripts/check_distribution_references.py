"""Drift checker for distribution repository literals.

Single source of truth: ``config/distribution.json`` (loaded through
``src/distribution.py``).  Code surfaces outside the frozen PKG1 set must
not carry repository-name literals — they import the registry instead.
The checker verifies the sanctioned equality points (packaging fallback,
update-service pinned defaults, shipped disabled trust template) verbatim
against the registry and fails with exact ``file:line`` drift reports for
everything else.  Offline and stdlib-only by design, with one deliberate
exception: the source-repository privacy pin (``GH_TOKEN``/``GITHUB_TOKEN``
in the environment opts in; without a token the check is skipped loudly,
never reported as a pass).

CO-NEUTRAL-1 (2026-10-05): the payload registry value is neutralized, so
the checker enforces the dual-criteria pin — ``source_repository`` must be
either the neutral ``manifest_source_id`` sentinel or the real private
repository name, and the real name must live in the checkout-only sidecar
``config/ops-private.json`` whose privacy the GitHub API pin verifies.
The sidecar's repository name also joins the drift needles, so private-name
literals outside the sanctioned points keep failing this check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import DEFAULT_REGISTRY, REGISTRY_PATH, load_registry  # noqa: E402

# Frozen PKG1 surfaces: literals stay until the clean-rebuild unfreeze
# (release-checklist §1); the checker must never flag them.
FROZEN_FILES = {
    "scripts/build_client_update.py",
}
# Static single point registered in the N9-B2 migration matrix; the help
# link is plain HTML and gets no registry plumbing.
STATIC_EXEMPT_FILES = {
    "frontend/index.html",
}
# The registry file itself is the source these checks compare against, plus
# the checkout-only private sidecar whose whole purpose is carrying the real
# private repository name.
REGISTRY_SOURCE_FILES = {
    "config/distribution.json",
    "config/ops-private.json",
}
# Equality points: repo names here are sanctioned, but only as exact
# byte-identical mirrors of the registry (verified below, not just exempt).
EQUALITY_FILES = {
    "src/distribution.py",
    "src/update/service.py",
    "config/client-update-trust.json",
    # SWEEPFIX-R2 W3（UPDATE-UX-1 mac 检查道）：浏览器 JS 无法 import
    # src.distribution——mac Releases API URL 的仓库名段以字面量留在文件内，
    # 由 verify_mac_update_module 对表 registry（非豁免，是经验证的等值点）。
    "frontend/modules/update-mac.js",
}

def repo_names(private_source: str) -> list[str]:
    names = {
        REGISTRY_FILE["distribution_repository"],
        REGISTRY_FILE["source_repository"],
    }
    if private_source:
        names.add(private_source)
    return sorted(names)


def load_file_registry() -> dict:
    if not REGISTRY_PATH.exists():
        # No registry file in this checkout: defaults are the registry, and
        # the equality checks below degenerate to comparing against themselves.
        return dict(DEFAULT_REGISTRY)
    return load_registry(REGISTRY_PATH)


def verify_distribution_module(file_registry: dict, report: list[str]) -> bool:
    from src import distribution

    ok = True
    if distribution.DEFAULT_REGISTRY != file_registry and REGISTRY_PATH.exists():
        report.append(
            "src/distribution.py: DEFAULT_REGISTRY drifts from "
            f"{REGISTRY_PATH.as_posix()} (packaging fallback must stay "
            "byte-identical; update both together)"
        )
        ok = False
    return ok


def verify_service_literals(file_registry: dict, report: list[str]) -> bool:
    text = (PROJECT_ROOT / "src/update/service.py").read_text(encoding="utf-8")
    expected = {
        "MANIFEST_SOURCE_ID": file_registry["manifest_source_id"],
        "DISTRIBUTION_REPOSITORY": file_registry["distribution_repository"],
    }
    ok = True
    for name, value in expected.items():
        pattern = re.compile(rf'^{name}\s*=\s*"(?P<value>[^"]+)"', re.M)
        match = pattern.search(text)
        if match is None:
            report.append(f"src/update/service.py: pinned default {name} is missing")
            ok = False
        elif match.group("value") != value:
            report.append(
                f"src/update/service.py: {name}="
                f'"{match.group("value")}" drifts from registry value "{value}"'
            )
            ok = False
    return ok


def verify_mac_update_module(file_registry: dict, report: list[str]) -> bool:
    """mac 更新检查道（frontend/modules/update-mac.js）的仓库名等值点。

    浏览器 JS 无法 import src.distribution，MAC_RELEASES_API 的仓库名段
    以字面量留在文件内；本验证器把它钉在 registry 值上——漂移即 DRIFT
    （UPDATE-UX-1 两车道全量红 test_checker_passes_on_current_tree 的根修：
    该文件曾是无登记字面量，57bd120 仓迁移同族面）。"""
    text = (PROJECT_ROOT / "frontend/modules/update-mac.js").read_text(encoding="utf-8")
    pattern = re.compile(
        r'MAC_RELEASES_API\s*=\s*"[^"]*?/repos/(?P<repo>[^"\s]+/[^"\s]+)/releases'
    )
    match = pattern.search(text)
    if match is None:
        report.append(
            "frontend/modules/update-mac.js: MAC_RELEASES_API constant "
            "is missing (mac check channel lost its Releases endpoint)"
        )
        return False
    expected = file_registry["distribution_repository"]
    if match.group("repo") != expected:
        report.append(
            "frontend/modules/update-mac.js: MAC_RELEASES_API repository "
            f'"{match.group("repo")}" drifts from registry value "{expected}"'
        )
        return False
    return True


def verify_trust_template(file_registry: dict, report: list[str]) -> bool:
    path = PROJECT_ROOT / "config/client-update-trust.json"
    try:
        trust = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.append(f"config/client-update-trust.json: unreadable ({exc})")
        return False
    distribution = trust.get("distribution") or {}
    ok = True
    if distribution.get("repository") != file_registry["distribution_repository"]:
        report.append(
            'config/client-update-trust.json: distribution.repository '
            f'"{distribution.get("repository")}" drifts from registry value '
            f'"{file_registry["distribution_repository"]}"'
        )
        ok = False
    manifest_url = str(trust.get("manifest_url") or "")
    expected_url = (
        f"https://github.com/{file_registry['distribution_repository']}/releases"
        f"/latest/download/{file_registry['manifest_asset']}"
    )
    if manifest_url != expected_url:
        report.append(
            "config/client-update-trust.json: manifest_url drifts from the "
            f"registry-derived stable URL ({expected_url})"
        )
        ok = False
    return ok


def load_private_source(report: list[str]) -> str:
    """Load the checkout-only sidecar carrying the real private repo name.

    Returns the repository name, or ``""`` after appending a drift report —
    a missing or invalid sidecar fails closed (the dual-criteria pin and the
    privacy pin cannot be verified without it).
    """
    path = PROJECT_ROOT / "config" / "ops-private.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.append(
            "config/ops-private.json: missing — the real private repository "
            "name must live in this checkout-only sidecar"
        )
        return ""
    except (OSError, ValueError) as exc:
        report.append(f"config/ops-private.json: unreadable ({exc})")
        return ""
    value = raw.get("source_repository") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        report.append(
            'config/ops-private.json: expected a non-empty "source_repository"'
        )
        return ""
    return value.strip()


def verify_source_repository_neutral(
    file_registry: dict, private_source: str, report: list[str]
) -> bool:
    """Dual-criteria pin: payload value is the sentinel or the private repo.

    ``source_repository`` ships neutralized, so the registry value must be
    either the neutral ``manifest_source_id`` sentinel or the real private
    repository name; anything else would put an unresolvable identity into
    the payload or silently drop the private channel.
    """
    sentinel = file_registry["manifest_source_id"]
    value = file_registry["source_repository"]
    if not sentinel:
        report.append(
            "config/distribution.json: manifest_source_id must be a non-empty "
            "neutral manifest source id"
        )
        return False
    if value not in {sentinel, private_source}:
        report.append(
            f'config/distribution.json: source_repository "{value}" is neither '
            f'the neutral manifest source id "{sentinel}" nor the private '
            f'repository "{private_source}"'
        )
        return False
    return True


def verify_source_repository_private(private_source: str, report: list[str]) -> bool | None:
    """Privacy pin: the real private repository must stay private.

    Returns ``True`` when the GitHub API confirms ``private: true``, ``False``
    when privacy is violated or the repository cannot be confirmed (deleted,
    renamed, or invisible to the token), and ``None`` — an honest, loudly
    printed SKIP — when no token is available or GitHub is unreachable.
    ``None`` never counts as a pass.
    """
    repo = private_source
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not (token or "").strip():
        print(
            "SKIP: source-repository privacy check not run — no "
            "GITHUB_TOKEN/GH_TOKEN in environment; the privacy of "
            f"{repo} is NOT verified by this run"
        )
        return None

    def unverifiable(reason: str) -> bool | None:
        print(
            f"SKIP: source-repository privacy check could not confirm {repo} "
            f"({reason}); privacy is NOT verified by this run"
        )
        return None

    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "courselens-distribution-checker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            report.append(
                f"{repo}: not found via GitHub API (deleted, renamed, or the "
                "token lacks access) — the pinned source repository must exist"
            )
            return False
        if exc.code in (403, 429, 500, 502, 503, 504):
            return unverifiable(f"GitHub API HTTP {exc.code}")
        report.append(f"{repo}: GitHub API HTTP {exc.code} — privacy cannot be verified")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return unverifiable(f"GitHub API unreachable: {exc.__class__.__name__}")
    if not isinstance(payload, dict) or "private" not in payload:
        report.append(f"{repo}: unexpected GitHub API payload — privacy cannot be verified")
        return False
    if payload.get("private") is not True:
        report.append(
            f"{repo}: PRIVACY VIOLATION — repository is public; "
            "source_repository must stay private"
        )
        return False
    print(f"OK: source repository {repo} confirmed private via GitHub API")
    return True


def scan_drift(report: list[str], private_source: str) -> int:
    needles = repo_names(private_source)
    scanned = 0
    drift = 0
    for relative in _scoped_files():
        if (relative in FROZEN_FILES or relative in STATIC_EXEMPT_FILES
                or relative in REGISTRY_SOURCE_FILES):
            continue
        if relative in EQUALITY_FILES:
            continue  # verified separately, byte-identical or reported
        path = PROJECT_ROOT / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            report.append(f"{relative}: unreadable ({exc})")
            drift += 1
            continue
        scanned += 1
        for number, line in enumerate(lines, start=1):
            for needle in needles:
                if needle in line:
                    report.append(
                        f"{relative}:{number}: repository literal "
                        f'"{needle}" outside the registry (import '
                        "src.distribution instead)"
                    )
                    drift += 1
                    break
    print(f"scanned {scanned} non-frozen code files for repository literals")
    return drift


def _scoped_files() -> list[str]:
    scope: list[str] = []
    for folder, suffixes in (
        ("scripts", {".py"}),
        ("src", {".py"}),
        ("frontend", {".js", ".html"}),
        ("config", {".json"}),
    ):
        root = PROJECT_ROOT / folder
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if "__pycache__" in path.parts:
                continue
            scope.append(path.relative_to(PROJECT_ROOT).as_posix())
    return scope


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-docs", action="store_true",
        help="additionally print an informational count of repository "
             "mentions in narrative docs (never fails the run)",
    )
    args = parser.parse_args(argv)

    global REGISTRY_FILE
    REGISTRY_FILE = load_file_registry()

    report: list[str] = []
    private_source = load_private_source(report)
    ok = verify_distribution_module(REGISTRY_FILE, report)
    ok = verify_service_literals(REGISTRY_FILE, report) and ok
    ok = verify_mac_update_module(REGISTRY_FILE, report) and ok
    ok = verify_trust_template(REGISTRY_FILE, report) and ok
    if private_source:
        ok = verify_source_repository_neutral(REGISTRY_FILE, private_source, report) and ok
        if verify_source_repository_private(private_source, report) is False:
            ok = False
    else:
        ok = False
    drift = scan_drift(report, private_source)

    for line in report:
        print(f"DRIFT: {line}")
    if args.report_docs:
        docs_hits = sum(
            1 for path in (PROJECT_ROOT / "docs").rglob("*.md")
            for needle in repo_names()
            if needle in path.read_text(encoding="utf-8", errors="replace")
        )
        print(f"informational: {docs_hits} doc files still mention the repo names "
              "(narrative surfaces are migrated on rebuild day, see the checklist)")
    if not ok or drift:
        print(f"FAIL: {len(report)} problem(s); repository identity must come "
              "from config/distribution.json")
        return 1
    print("OK: registry equality points verified, zero literal drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
