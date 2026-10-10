"""Fail-closed production release gate audit; never reads secret values."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import (
    DISTRIBUTION_REPOSITORY,
    MANIFEST_ASSET,
    TAG_NAMESPACE,
)
from src.update.service import REQUIRED_PRODUCTION_GATES


def audit(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # 与 TrustPolicy.load 同纪律：字符串 "false" 不是真布尔，视为未通过。
    gates = {str(k): v is True for k, v in (raw.get("production_gates") or {}).items()}
    missing = sorted(REQUIRED_PRODUCTION_GATES - {name for name, value in gates.items() if value})
    distribution = raw.get("distribution") or {}
    # policy_valid certifies the closed-set policy shape (schema + pinned
    # distribution identity); enablement and the production gates are scored
    # separately so the audit stays meaningful before and after the GO flip.
    policy_ok = (
        raw.get("schema") == "courselens.client-update-trust.v2"
        and distribution.get("repository") == DISTRIBUTION_REPOSITORY
        and distribution.get("visibility") == "public"
        and distribution.get("auth_model") == "none"
        and distribution.get("tag_namespace") == TAG_NAMESPACE
        and distribution.get("manifest_asset") == MANIFEST_ASSET
        and distribution.get("source_repository_access") is False
    )
    return {
        "schema": "courselens.client-release-gate-audit.v1",
        "production_enabled": bool(raw.get("enabled")),
        "policy_valid": policy_ok,
        "missing_gates": missing,
        "release_allowed": bool(
            policy_ok and raw.get("enabled") and not missing
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "config" / "client-update-trust.json",
    )
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args(argv)
    result = audit(args.config)
    print(json.dumps(result, sort_keys=True))
    if not result["policy_valid"]:
        return 2
    if args.require_release_ready and not result["release_allowed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
