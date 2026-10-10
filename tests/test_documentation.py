from __future__ import annotations

import unittest
from pathlib import Path

from scripts import check_markdown_links


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTest(unittest.TestCase):
    def test_local_markdown_links_resolve(self):
        self.assertEqual(check_markdown_links.failures(), [])

    def test_user_readme_has_synthetic_product_image(self):
        image = ROOT / "docs" / "assets" / "readme" / "course-workspace.png"
        self.assertTrue(image.is_file())
        self.assertGreater(image.stat().st_size, 10_000)

    def test_user_and_technical_readmes_are_separate(self):
        user = (ROOT / "README.md").read_text(encoding="utf-8")
        technical = (ROOT / "docs" / "technical" / "README.md").read_text(encoding="utf-8")
        self.assertIn("三步开始", user)
        self.assertIn("docs/technical/README.md", user)
        self.assertNotIn("python -m pytest", user)
        self.assertIn("python -m pytest", technical)
        self.assertNotIn("run ID", user)

    def test_live_early_disclosure_same_sentence_across_faces(self):
        """LIVE-DISCLOSURE-1：直播早期功能小字三面同源——客户端闭集唯一出处
        （frontend/modules/live-state.js）与 README、Pages（docs/index.html）
        逐字同句；改一处须三面同改，禁三处三套说法。"""
        canonical = "直播转写为早期功能，可用性视网络与课程环境而定。"
        live_state = (ROOT / "frontend" / "modules" / "live-state.js").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        pages = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'LIVE_DISCLOSURE_TEXT = "{canonical}";', live_state)
        self.assertIn(canonical, readme)
        self.assertIn(canonical, pages)


if __name__ == "__main__":
    unittest.main()
