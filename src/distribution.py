"""Distribution registry: repository identities as the single source of truth.

Rebuild-day migration (release-checklist §1) edits
``config/distribution.json``; every non-frozen consumer imports the values
from here instead of carrying literals.  The registry file exists only in
the repository checkout — packaged clients ship no ``config/`` directory —
so ``DEFAULT_REGISTRY`` below is the packaging fallback and MUST stay
byte-identical to the file; ``scripts/check_distribution_references.py``
fails closed on any drift between them.

CO-NEUTRAL-1 (2026-10-05): the release payload carries no private-repo
name.  ``source_repository`` holds the neutral manifest source id (equal to
``manifest_source_id``), the update-service trust pin compares against it,
and the real private repository name lives only in the checkout-only
sidecar ``config/ops-private.json`` (never packaged), from which the
``SOURCE_REPOSITORY`` export below reads it for private-channel tooling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA = "courselens.distribution-registry.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "config" / "distribution.json"
OPS_PRIVATE_PATH = PROJECT_ROOT / "config" / "ops-private.json"

DEFAULT_REGISTRY: dict[str, Any] = {
    "schema": REGISTRY_SCHEMA,
    "distribution_repository": "gualtier-xu/Fudan-CourseLens",
    "source_repository": "Fudan-CourseLens-Source",
    "manifest_source_id": "Fudan-CourseLens-Source",
    "tag_namespace": "client-v",
    "manifest_asset": "courselens-windows-manifest.json",
    "allowed_hosts": [
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    ],
}

_TEXT_KEYS = (
    "distribution_repository",
    "source_repository",
    "manifest_source_id",
    "tag_namespace",
    "manifest_asset",
)


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """Merge the registry file over the packaging defaults.

    A missing file yields the defaults verbatim (packaged clients).  A file
    that is unreadable, carries an unknown key set, or breaks a value type
    fails closed — half-migrated registries must never silently degrade.
    """
    registry_path = REGISTRY_PATH if path is None else path
    registry: dict[str, Any] = dict(DEFAULT_REGISTRY)
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return registry
    except (OSError, ValueError) as exc:
        raise RuntimeError("distribution_registry_invalid") from exc
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT_REGISTRY):
        raise RuntimeError("distribution_registry_invalid")
    registry.update(raw)
    for key in _TEXT_KEYS:
        if not isinstance(registry[key], str) or not registry[key]:
            raise RuntimeError("distribution_registry_invalid")
    hosts = registry["allowed_hosts"]
    if not isinstance(hosts, list) or not hosts or not all(
        isinstance(host, str) and host for host in hosts
    ):
        raise RuntimeError("distribution_registry_invalid")
    return registry


REGISTRY = load_registry()
DISTRIBUTION_REPOSITORY: str = REGISTRY["distribution_repository"]
MANIFEST_SOURCE_ID: str = REGISTRY["manifest_source_id"]


def _private_source_repository() -> str:
    """Checkout-only private-channel truth for the private repository name.

    The registry value ships neutralized, so the real private repository
    name lives only in ``config/ops-private.json`` — a sidecar that exists
    solely in the repository checkout (packaged clients ship no ``config/``
    directory and never receive this file).  A missing sidecar is the
    packaged context and yields an empty string; a sidecar that exists but
    is unreadable or invalid fails closed, mirroring the registry loader's
    half-migration stance.
    """
    try:
        raw = json.loads(OPS_PRIVATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        raise RuntimeError("distribution_ops_private_invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"source_repository"}:
        raise RuntimeError("distribution_ops_private_invalid")
    value = raw["source_repository"]
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("distribution_ops_private_invalid")
    return value.strip()


# Private-channel consumers (release tooling, remote supervisor defaults)
# resolve the real repository name in the checkout; packaged clients get the
# neutral manifest source id, so the payload never carries the private name.
SOURCE_REPOSITORY: str = _private_source_repository() or MANIFEST_SOURCE_ID
TAG_NAMESPACE: str = REGISTRY["tag_namespace"]
MANIFEST_ASSET: str = REGISTRY["manifest_asset"]
ALLOWED_HOSTS: list[str] = list(REGISTRY["allowed_hosts"])
