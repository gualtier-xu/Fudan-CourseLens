"""Alignment pin: every update error code the backend can surface must have a
human guidance line in the settings card's closed-set table."""

from __future__ import annotations

import re
from pathlib import Path

from tests.frontend_family import family_text

REPO = Path(__file__).resolve().parents[1]


def _backend_codes() -> set[str]:
    codes: set[str] = set()
    for name in ("src/update/service.py", "src/update/durability.py"):
        text = (REPO / name).read_text(encoding="utf-8")
        codes |= set(re.findall(r'UpdateError\(\s*"([a-z0-9_]+)"', text))
    service = (REPO / "src/update/service.py").read_text(encoding="utf-8")
    codes |= set(re.findall(r'error_code="([a-z0-9_]+)"', service))
    return codes


def _guidance_keys() -> set[str]:
    js = family_text("settings")  # 家族并集（ARCH-DEBT-1 拆分安全网）

    block = re.search(
        r"UPDATE_ERROR_GUIDANCE = Object\.freeze\(\{(.*?)\}\);", js, re.S
    ).group(1)
    return set(re.findall(r"([a-z0-9_]+):\s*\"", block))


def test_every_backend_update_code_has_human_guidance():
    missing = _backend_codes() - _guidance_keys()
    assert not missing, f"update codes without settings guidance: {sorted(missing)}"


def test_trust_config_invalid_present_after_bom_boundary_fix():
    assert "trust_config_invalid" in _guidance_keys()
