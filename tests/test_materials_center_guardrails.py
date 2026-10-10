"""T13 薄件增量（夜14-R7 → N15-R4 P2）：讲义中心删除面安全护栏直测。

路由级删除流已由 tests/test_materials_center.py 覆盖；本件补其下的纯函数
护栏——``_course_data_rmtree_owned`` 是整个删除面的唯一边界护栏（域内才删、
符号链接拒删、域外拒删），此前零直测：边界守卫失效＝误删任意目录。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.runtime.materials_center import (
    _course_data_rmtree_owned,
    _materials_safe_stem,
    courseware_pdf_download_name,
    summary_export_key,
)


class OwnedRmtreeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "namespace"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_owned_directory_is_deleted_with_byte_accounting(self):
        target = self.root / "sum-abc123"
        target.mkdir()
        (target / "a.md").write_bytes(b"x" * 100)
        (target / "nested").mkdir()
        (target / "nested" / "b.md").write_bytes(b"y" * 30)
        deleted, total = _course_data_rmtree_owned(self.root, "sum-abc123")
        self.assertTrue(deleted)
        self.assertEqual(total, 130)
        self.assertFalse(target.exists())

    def test_escaping_name_is_refused_and_outside_tree_untouched(self):
        outside = Path(self.temporary.name) / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        deleted, total = _course_data_rmtree_owned(self.root, "../precious")
        self.assertFalse(deleted)
        self.assertEqual(total, 0)
        self.assertTrue((outside / "keep.txt").exists(), "域外目录绝不允许被删")

    def test_symlink_candidate_is_refused(self):
        real = Path(self.temporary.name) / "real-data"
        real.mkdir()
        (real / "f.txt").write_text("data", encoding="utf-8")
        link = self.root / "link"
        try:
            os.symlink(real, link)
        except OSError:
            # 环境守卫（同仓既有 skip 纪律）：无符号链接特权的主机跳过该腿，
            # 生产面的 is_symlink 拒删守卫仍在源码位（materials_center.py 域检查前）。
            self.skipTest("symlink privilege unavailable on this host")
        deleted, _ = _course_data_rmtree_owned(self.root, "link")
        self.assertFalse(deleted, "符号链接候选必须拒绝（防链接到域外目录被整删）")
        self.assertTrue((real / "f.txt").exists())

    def test_plain_file_candidate_is_refused(self):
        (self.root / "loose.txt").write_text("x", encoding="utf-8")
        deleted, total = _course_data_rmtree_owned(self.root, "loose.txt")
        self.assertFalse(deleted)
        self.assertEqual(total, 0)
        self.assertTrue((self.root / "loose.txt").exists())

    def test_missing_candidate_reports_not_deleted(self):
        self.assertEqual(_course_data_rmtree_owned(self.root, "ghost"), (False, 0))


class NamingGuardTests(unittest.TestCase):
    def test_safe_stem_strips_illegal_charset_and_caps_length(self):
        value = _materials_safe_stem('2026-09-01 第1讲:线代/入门*笔记?', fallback="f")
        self.assertNotIn("/", value)
        self.assertNotIn(":", value)
        self.assertNotIn("*", value)
        self.assertNotIn("?", value)
        self.assertTrue(value)
        long = _materials_safe_stem("x" * 80, fallback="f")
        self.assertEqual(len(long), 40)

    def test_safe_stem_falls_back_when_everything_stripped(self):
        self.assertEqual(_materials_safe_stem("///", fallback="untitled"), "untitled")
        self.assertEqual(_materials_safe_stem("", fallback="courseware"), "courseware")

    def test_courseware_download_name_defaults_when_unnamed(self):
        self.assertEqual(courseware_pdf_download_name({}), "courseware.pdf")
        self.assertEqual(courseware_pdf_download_name(None), "courseware.pdf")
        name = courseware_pdf_download_name({"date": "2026-09-01", "sub_title": "矩阵"})
        self.assertTrue(name.endswith(".pdf"))
        self.assertIn("矩阵", name)

    def test_summary_export_key_is_deterministic_and_pair_unique(self):
        first = summary_export_key("c1", "s1")
        self.assertEqual(first, summary_export_key("c1", "s1"))
        self.assertNotEqual(first, summary_export_key("c1", "s2"))
        self.assertNotEqual(first, summary_export_key("c2", "s1"))
        self.assertTrue(first.startswith("sum-"))


if __name__ == "__main__":
    unittest.main()
