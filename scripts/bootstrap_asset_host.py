"""Bootstrap the public release portal for CourseLens client releases.

Default mode is a fully offline plan. Nothing in this script ever handles a
secret value: certificate PFX bytes, signing keys, and publisher credentials
are provided by the operator through the documented channels, never as script
arguments or script-generated content. The distribution repository
the distribution repository (``config/distribution.json``) is the pre-existing public signed Worker
template and client release portal (ADR 0006), so `apply --yes` never creates
or edits anything: it only verifies that the repository exists and is public,
failing closed otherwise. Enabling production remains a separately reviewed
configuration change.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nacl.signing import SigningKey
from scripts.check_client_release_gates import audit as audit_release_gates
from src.distribution import DISTRIBUTION_REPOSITORY, SOURCE_REPOSITORY

ASSET_REPOSITORY = DISTRIBUTION_REPOSITORY
SOURCE_REPOSITORY = SOURCE_REPOSITORY
PROTECTED_ENVIRONMENT = "client-release-production"
PUBLISHER_SECRET_NAME = "COURSELENS_RELEASE_PUBLISHER_TOKEN"
REPOSITORY_DESCRIPTION = (
    "CourseLens public signed Worker template + client release portal; "
    "approved packages, signed manifests, licenses and minimal release "
    "metadata only"
)
PLAN_SCHEMA = "courselens.asset-host-bootstrap-plan.v1"
CHECK_SCHEMA = "courselens.asset-host-bootstrap-check.v1"
APPLY_SCHEMA = "courselens.asset-host-bootstrap-apply.v1"
ROOT_KEY_SCHEMA = "courselens.asset-host-root-key.v1"


def load_trust_policy(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != "courselens.client-update-trust.v2":
        raise SystemExit("client update trust policy is invalid")
    return raw


def _gh(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["gh", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"gh command failed: {' '.join(args[:2])} ({detail[:300]})")
    return result.stdout.decode("utf-8", errors="replace").strip()


def build_plan(trust: dict) -> dict:
    secret_names = [str(name) for name in trust.get("required_secret_names") or []]
    return {
        "schema": PLAN_SCHEMA,
        "repository": {
            "name": ASSET_REPOSITORY,
            "visibility": "public",
            "description": REPOSITORY_DESCRIPTION,
            "content_policy": [
                "generated client packages, signed manifests, licenses, minimal release metadata only",
                "never monorepo source, credentials, course URLs, course data, logs, traces, screenshots",
            ],
        },
        "gh_commands": [],
        "secret_names": secret_names,
        "secret_commands": [
            (
                f"gh secret set {name} --env {PROTECTED_ENVIRONMENT} "
                f"--repo {SOURCE_REPOSITORY}"
            )
            for name in secret_names
        ],
        "manual_steps": [
            "Verify that the pre-existing public repository "
            f"{ASSET_REPOSITORY} exists and is public; `apply --yes` performs "
            "this read-only check and creates or edits nothing.",
            "Provision the publisher identity: a dedicated minimal GitHub App "
            "or a fine-grained PAT with contents:write on "
            f"{ASSET_REPOSITORY} only; never the student App, never "
            "all-repositories access, never write permission on the private "
            "source repository.",
            f"Store the publisher credential as {PUBLISHER_SECRET_NAME} in the "
            f"{PROTECTED_ENVIRONMENT} environment of {SOURCE_REPOSITORY} "
            "(the gated client-release workflow consumes only the secret name).",
            "Generate and publish the package SHA-256 checksum sheet "
            "(SHA256SUMS.txt); per the 2026-09-30 signing decision no "
            "Authenticode certificate is used, so tamper evidence is the "
            "checksum sheet plus the Ed25519-signed manifest.",
            "Escrow the update root key offline; never add any private seed to "
            "GitHub.",
        ],
        "key_ceremony": [
            "python scripts/bootstrap_asset_host.py generate-root-key --out <offline-path>",
            "Sign the courselens.update-key-authorization.v1 offline, binding key id, "
            "public key, epoch, validity window, active status, channel and platform.",
            "Only root public keys and root-signed release-key authorizations enter "
            "the controlled installer's external trust directory.",
        ],
        "explicitly_out_of_scope": [
            "setting secret values (this script never handles secret material)",
            "creating or editing the public repository (it must already exist)",
            "enabling production updates (separately reviewed trust-config change)",
            "publishing any package content (release-only workflow does that)",
        ],
    }


def offline_checks(trust_path: Path) -> dict:
    trust = load_trust_policy(trust_path)
    if trust.get("enabled") is True:
        raise SystemExit("refusing to operate while production updates are enabled")
    distribution = trust.get("distribution") or {}
    if distribution.get("repository") != ASSET_REPOSITORY:
        raise SystemExit("trust policy distribution repository does not match the asset host")
    return {
        "trust_policy_ok": True,
        "production_enabled": bool(trust.get("enabled")),
        "required_secret_names": [
            str(name) for name in trust.get("required_secret_names") or []
        ],
        "release_gate_audit": audit_release_gates(trust_path),
    }


def run_check(trust_path: Path, with_gh: bool) -> dict:
    result = {
        "schema": CHECK_SCHEMA,
        **offline_checks(trust_path),
        "gh_available": False,
        "gh_authenticated": False,
        "repo": {"exists": None, "public": None, "description": None},
    }
    if not with_gh:
        return result
    if not shutil.which("gh"):
        return result
    result["gh_available"] = True
    authenticated = _gh("auth", "status", check=False)
    result["gh_authenticated"] = "Logged in" in authenticated or "logged in" in authenticated
    view = _gh(
        "repo", "view", ASSET_REPOSITORY,
        "--json", "name,isPrivate,description", check=False,
    )
    if view:
        try:
            payload = json.loads(view)
            result["repo"] = {
                "exists": True,
                "public": not bool(payload.get("isPrivate")),
                "description": payload.get("description"),
            }
        except json.JSONDecodeError:
            pass
    return result


def run_apply(trust_path: Path, assume_yes: bool) -> dict:
    if not assume_yes:
        raise SystemExit("apply performs GitHub access; rerun with --yes after authorization")
    offline_checks(trust_path)
    if not shutil.which("gh"):
        raise SystemExit("gh CLI is required for apply")
    _gh("auth", "status")
    # Read-only verification: the public portal repository must already exist.
    # A missing repository must produce a clean fail-closed message, never a
    # raw subprocess error or a parse crash.
    view = _gh(
        "repo", "view", ASSET_REPOSITORY, "--json", "name,isPrivate,description",
        check=False,
    )
    if not view:
        raise SystemExit(
            f"{ASSET_REPOSITORY} is not accessible via gh; it must already "
            "exist and be public before apply"
        )
    try:
        payload = json.loads(view)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"gh repository lookup for {ASSET_REPOSITORY} returned unusable output"
        ) from exc
    if payload.get("isPrivate"):
        raise SystemExit(f"{ASSET_REPOSITORY} is not public; refusing to proceed")
    actions = [
        f"verified {ASSET_REPOSITORY} exists and is public; nothing was created or edited",
        f"next step: provision the publisher identity and store it as "
        f"{PUBLISHER_SECRET_NAME} in the {PROTECTED_ENVIRONMENT} environment; "
        "this script never handles secret values",
    ]
    return {"schema": APPLY_SCHEMA, "repository": ASSET_REPOSITORY, "actions": actions}


def generate_root_key(out_path: Path) -> dict:
    resolved = out_path.resolve()
    if PROJECT_ROOT in resolved.parents or resolved == PROJECT_ROOT:
        raise SystemExit("refusing to write the update root key inside the repository")
    if resolved.exists():
        raise SystemExit("refusing to overwrite an existing root key file")
    signing_key = SigningKey.generate()
    seed = bytes(signing_key)
    public = bytes(signing_key.verify_key)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(base64.b64encode(seed).decode("ascii") + "\n", encoding="ascii")
    return {
        "schema": ROOT_KEY_SCHEMA,
        "public_key": base64.b64encode(public).decode("ascii"),
        "suggested_key_id": "root-" + time_key_suffix(),
        "written_to": str(resolved),
        "custody_reminder": (
            "escrow the seed offline and independently of this machine; "
            "never add it to GitHub or the repository"
        ),
    }


def time_key_suffix() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "check", "apply", "generate-root-key"))
    parser.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "config" / "client-update-trust.json",
    )
    parser.add_argument("--yes", action="store_true", help="required by apply")
    parser.add_argument(
        "--with-gh", action="store_true",
        help="check mode only: also probe gh auth and repository state (read-only)",
    )
    parser.add_argument("--out", type=Path, help="generate-root-key output file")
    args = parser.parse_args(argv)
    if args.mode == "plan":
        print(json.dumps(build_plan(load_trust_policy(args.config)), indent=2, sort_keys=True))
        return 0
    if args.mode == "check":
        print(json.dumps(run_check(args.config, args.with_gh), indent=2, sort_keys=True))
        return 0
    if args.mode == "apply":
        print(json.dumps(run_apply(args.config, args.yes), indent=2, sort_keys=True))
        return 0
    if not args.out:
        raise SystemExit("generate-root-key requires --out <path>")
    print(json.dumps(generate_root_key(args.out), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
