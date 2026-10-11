"""P61 behavior pin: the browser-reset page clears the live theme key.

The standalone reset page (scripts/reset_courselens_browser_state.py) keeps
a closed exactKeys list because the dotted courselens.* keys escape every
underscore prefix sweep.  P56 moved the theme key to the dotted
courselens.theme.v2 (frontend/modules/shell.js THEME_PREF_KEY) while the
reset page still listed the unreachable underscore form courselens_theme_v1,
so "重置浏览器状态" silently kept the new key.  P62 extended the same lockstep
to the two dotted sibling preference keys (course order and term filter,
frontend/modules/study.js COURSE_ORDER_KEY / COURSE_TERM_FILTER_KEY) that the
in-app reset closed set (settings.js RESET_BROWSER_KEYS) already clears.
These pins keep the page's
exactKeys in lockstep with the live frontend key and keep the sweep scoped
to CourseLens-only keys.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER_RESET = ROOT / "scripts" / "reset_courselens_browser_state.py"

LIVE_THEME_KEY = "courselens.theme.v2"
LIVE_SIBLING_KEYS = ("courselens.course-order.v1", "courselens.catalog-term.v1")


def _page_source() -> str:
    import importlib.util

    spec = importlib.util.spec_from_file_location("reset_courselens_browser_state", BROWSER_RESET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._page().decode("utf-8")


def _parse_reset_contract(page: str) -> tuple[set[str], list[str]]:
    block = re.search(r"const exactKeys = new Set\(\[(.*?)\]\);", page, re.DOTALL)
    assert block is not None, "exactKeys list vanished from the reset page"
    exact_keys = set(re.findall(r'"([^"]+)"', block.group(1)))
    prefixes = re.findall(r'key\.startsWith\("([^"]+)"\)', page)
    assert exact_keys, "exactKeys must stay a closed non-empty list"
    assert prefixes, "prefix sweep must stay present"
    return exact_keys, prefixes


def _matches(key: str, exact_keys: set[str], prefixes: list[str]) -> bool:
    """Faithful Python mirror of the page's JS matches() closure."""
    return key in exact_keys or any(key.startswith(prefix) for prefix in prefixes)


class BrowserResetPageKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = _page_source()
        self.exact_keys, self.prefixes = _parse_reset_contract(self.page)

    def test_live_theme_key_is_listed_and_cleared(self) -> None:
        self.assertIn(LIVE_THEME_KEY, self.exact_keys)
        # The dotted key escapes every underscore prefix: only the exact
        # entry clears it — the regression this pin exists for.
        self.assertTrue(_matches(LIVE_THEME_KEY, self.exact_keys, self.prefixes))

    def test_dotted_sibling_preference_keys_are_listed_and_cleared(self) -> None:
        for key in LIVE_SIBLING_KEYS:
            self.assertIn(key, self.exact_keys, f"missing live key: {key}")
            self.assertTrue(
                _matches(key, self.exact_keys, self.prefixes),
                f"reset page cannot clear live key: {key}",
            )

    def test_dead_underscore_v1_entry_is_gone(self) -> None:
        self.assertNotIn("courselens_theme_v1", self.exact_keys)
        self.assertNotIn('"courselens_theme_v1"', self.page)

    def test_sweep_stays_scoped_to_courselens_only_keys(self) -> None:
        for foreign in ("github_theme", "student_notes", "my_courselens_theme_v2",
                        "fudan_icourse_theme_v1"):
            self.assertFalse(
                _matches(foreign, self.exact_keys, self.prefixes),
                f"reset page must never clear foreign key: {foreign}",
            )


if __name__ == "__main__":
    unittest.main()
