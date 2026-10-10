"""N5A-P5 真题结构拆题 v1：三族题号、小问不误切、零切分、指纹缓存。"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path

from src.runtime.exam_paper_split import (
    MAX_ANSWER_CHARS,
    MAX_OPTIONS,
    MAX_STEM_CHARS,
    MAX_SUBPARTS,
    SCHEMA,
    extract_structure,
    list_exam_questions,
    refresh_exam_questions,
    search_exam_questions,
    split_answer,
    split_questions,
)


def _pages(*page_texts: str):
    return [
        {"page_num": index + 1, "text": text, "text_hash": f"h{index}"}
        for index, text in enumerate(page_texts)
    ]


class ExamSplitTests(unittest.TestCase):
    def test_big_questions_split_and_small_questions_survive(self):
        pages = _pages(
            "一、选择题（每小题 3 分）\n1. 下列说法正确的是\nA. 甲\nB. 乙\n2. 计算",
            "二、填空题\n3. ___",
        )
        units = split_questions(pages)
        self.assertEqual(len(units), 2, "两道大题各成单元，小问 A/B/1/2 不另切")
        self.assertEqual(units[0]["start_page"], 1)
        self.assertEqual(units[1]["start_page"], 2)

    def test_score_marker_opens_unit(self):
        pages = _pages("（10 分）设函数 f(x) 求导\n（15 分）证明不等式")
        units = split_questions(pages)
        self.assertEqual([unit["kind"] for unit in units], ["score", "score"])

    def test_decimal_lines_are_not_question_markers(self):
        pages = _pages("测得数值如下\n12.5 是结果\n3.14 也是小数")
        self.assertEqual(split_questions(pages), [], "小数行不得被当成小题题号")

    def test_document_without_question_markers_has_zero_split(self):
        self.assertEqual(split_questions(_pages("一段普通课文\n另一段课文")), [])

    def test_multi_page_question_spans_pages(self):
        pages = _pages("一、计算题\n题干第一页", "（接上页）题干继续", "参考答案")
        units = split_questions(pages)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["start_page"], 1)
        self.assertEqual(units[0]["end_page"], 3)

    def test_refresh_caches_by_page_fingerprint_and_serves_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(
                    """
                    CREATE TABLE learning_documents (
                        document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                        sub_id TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                        updated_at REAL NOT NULL DEFAULT 0);
                    CREATE TABLE learning_document_pages (
                        document_id TEXT NOT NULL, page_num INTEGER NOT NULL,
                        text TEXT NOT NULL DEFAULT '', text_hash TEXT NOT NULL DEFAULT '');
                    """
                )
                db.execute(
                    "INSERT INTO learning_documents VALUES('doc-x','c1','s1','期末真题','sha-x',1.0)"
                )
                db.execute("INSERT INTO learning_document_pages VALUES('doc-x',1,'一、选择题','h1')")
            first = refresh_exam_questions(path, "doc-x")
            self.assertTrue(first["changed"])
            self.assertEqual(first["questions"], 1)
            second = refresh_exam_questions(path, "doc-x")
            self.assertFalse(second["changed"], "页指纹未变不重算")
            rows = search_exam_questions(path, "s1")
            self.assertEqual(len(rows), 1)
            self.assertIn("一、选择题", rows[0]["label"])

class ExamSplitV2Tests(unittest.TestCase):
    """v2 加性扩展：题干正文、跨页、小问/选项/分值、答案切分与 raw 降级。"""

    def test_stem_keeps_the_whole_question_body(self):
        pages = _pages("一、选择题（10 分）\n下列正确的是\nA. 甲\nB. 乙")
        unit = split_questions(pages)[0]
        self.assertIn("下列正确的是", unit["stem"])
        self.assertEqual(unit["kind"], "big", "旧键语义不变")
        self.assertEqual(unit["anchor_text"], "一、选择题（10 分）")

    def test_subparts_options_and_points_are_extracted_when_confident(self):
        pages = _pages("一、综合题（20 分）\n(1) 第一问\n(2) 第二问\nA. 甲\nB. 乙\nC. 丙")
        unit = split_questions(pages)[0]
        self.assertEqual(unit["structure"], "parsed")
        self.assertEqual([item["label"] for item in unit["subparts"]], ["(1)", "(2)"])
        self.assertEqual([item["label"] for item in unit["options"]], ["A", "B", "C"])
        self.assertEqual(unit["points"], 20)

    def test_single_marker_is_not_treated_as_structure(self):
        """只有一个 (1) 或只有一个 A. 时不敢当结构，保留 raw stem。"""
        unit = split_questions(_pages("一、题\n(1) 只有一个小问\nA. 只有一个选项"))[0]
        self.assertEqual(unit["subparts"], [])
        self.assertEqual(unit["options"], [])
        self.assertEqual(unit["structure"], "raw")

    def test_cross_page_question_has_complete_text(self):
        pages = _pages("一、阅读题\n8. 第一段", "（接上页）第二段，请计算", "二、下一大题")
        units = split_questions(pages)
        unit = units[0]
        self.assertTrue(unit["cross_page"])
        self.assertEqual((unit["start_page"], unit["end_page"]), (1, 2))
        self.assertIn("第二段", unit["stem"])
        self.assertFalse(units[1]["cross_page"])

    def test_answer_is_split_only_on_explicit_markers(self):
        self.assertEqual(split_answer("1. 求 p_c。\n参考答案：p_c=0.8"),
                         ("1. 求 p_c。", "p_c=0.8"))
        self.assertEqual(split_answer("1. 求 p_c。\n解答：\n由定义得 p_c=0.8")[1], "由定义得 p_c=0.8")
        self.assertEqual(split_answer("1. 求 p_c。\n参考答案\np_c=0.8")[1], "p_c=0.8")
        # 没有标记时答案为空；正文里出现「答案」二字不算标记
        self.assertEqual(split_answer("1. 请把答案写在答题卡上。"),
                         ("1. 请把答案写在答题卡上。", ""))
        self.assertEqual(split_answer("1. 本题无参考答案。")[1], "")

    def test_extract_structure_ignores_decimals_and_plain_lines(self):
        structure = extract_structure("12.5 是小数\n3.14 也是\n普通一行")
        self.assertEqual(structure, {"subparts": [], "options": [], "points": None})

    def test_structure_caps_truncate_to_frozen_limits(self):
        """T13 增量（夜14-R7）：extract_structure 上限执法。MAX_SUBPARTS=20
        真实截断；选项标签闭集是 A-H 八个（第 9 个标签不识别），故
        MAX_OPTIONS=12 今日为裕量帽不绑定——常量即合同（防静默调参）。"""
        self.assertEqual((MAX_SUBPARTS, MAX_OPTIONS, MAX_STEM_CHARS), (20, 12, 4000))
        subpart_lines = "\n".join(f"({index}) 第{index}小问内容" for index in range(1, 26))
        structure = extract_structure(f"1. 大题干（5分）\n{subpart_lines}")
        self.assertEqual(len(structure["subparts"]), MAX_SUBPARTS)
        self.assertEqual(structure["points"], 5)

        option_lines = "\n".join(f"{chr(ord('A') + index)}. 选项{index}" for index in range(8))
        structure = extract_structure(f"1. 单选（3分）\n{option_lines}")
        self.assertEqual([item["label"] for item in structure["options"]], list("ABCDEFGH"))
        # 标签闭集+置信门槛：I 不在 A-H；只剩一条有效选项行时整族不猜（留空）。
        structure = extract_structure("1. 单选（3分）\nA. 甲\nI. 越界标签")
        self.assertEqual(structure["options"], [])

    def test_answer_split_truncates_to_frozen_char_caps(self):
        """T13 增量：split_answer 双向截断——题干 4000 帽、答案 2000 帽；
        无标记长题干同样截断且答案空。"""
        marker = "参考答案："
        stem = "题干" * 3000 + "\n" + marker + "答案" * 1500
        question, answer = split_answer(stem)
        self.assertEqual(len(question), MAX_STEM_CHARS)
        self.assertEqual(len(answer), MAX_ANSWER_CHARS)
        no_marker = "正文" * 3000
        question, answer = split_answer(no_marker)
        self.assertEqual(len(question), MAX_STEM_CHARS)
        self.assertEqual(answer, "")

    def test_v2_columns_are_persisted_and_schema_is_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(
                    """
                    CREATE TABLE learning_documents (
                        document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                        sub_id TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                        updated_at REAL NOT NULL DEFAULT 0);
                    CREATE TABLE learning_document_pages (
                        document_id TEXT NOT NULL, page_num INTEGER NOT NULL,
                        text TEXT NOT NULL DEFAULT '', text_hash TEXT NOT NULL DEFAULT '');
                    """
                )
                db.execute("INSERT INTO learning_documents VALUES('doc-y','c1','s1','真题','sha-y',1.0)")
                db.execute(
                    "INSERT INTO learning_document_pages VALUES('doc-y',1,?, 'h1')",
                    ("一、题（5 分）\n(1) 问一\n(2) 问二",),
                )
            result = refresh_exam_questions(path, "doc-y")
            self.assertEqual(result["schema"], SCHEMA)
            rows = list_exam_questions(path, "doc-y")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["points"], 5)
            self.assertEqual(len(rows[0]["subparts"]), 2)
            self.assertFalse(rows[0]["cross_page"])
            self.assertTrue(rows[0]["stem_hash"])
            # 旧消费者继续拿得到老键
            searched = search_exam_questions(path, "s1")
            self.assertEqual(searched[0]["question_id"], rows[0]["question_id"])
            self.assertIn("一、题", searched[0]["anchor_text"])
            # 页指纹未变 → 不重算
            self.assertFalse(refresh_exam_questions(path, "doc-y")["changed"])


class UpgradeFromV1Tests(unittest.TestCase):
    """v1 → v2 升级正确性：旧库页内容没变时也必须重算一次把新列补齐。"""

    def _v1_database(self, path):
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                """
                CREATE TABLE learning_documents (
                    document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE learning_document_pages (
                    document_id TEXT NOT NULL, page_num INTEGER NOT NULL,
                    text TEXT NOT NULL DEFAULT '', text_hash TEXT NOT NULL DEFAULT '');
                CREATE TABLE exam_questions (
                    question_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL, course_id TEXT NOT NULL,
                    question_no INTEGER NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
                    start_page INTEGER NOT NULL, end_page INTEGER NOT NULL,
                    anchor_text TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE exam_split_state (
                    document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    updated_at REAL NOT NULL);
                """
            )
            db.execute("INSERT INTO learning_documents VALUES('doc1','c1','s1','真题','sha1',1.0)")
            text = "一、选择题（10 分）\n(1) 问一\n(2) 问二"
            db.execute("INSERT INTO learning_document_pages VALUES('doc1',1,?, 'h1')", (text,))
            # v1 时代留下的缓存：指纹只覆盖页码+页哈希（没有 schema 版本），并已有一行旧题
            v1_fingerprint = hashlib.sha256("1:h1".encode("utf-8")).hexdigest()[:32]
            db.execute("INSERT INTO exam_split_state VALUES('doc1',?,1.0)", (v1_fingerprint,))
            db.execute(
                "INSERT INTO exam_questions VALUES('q-old','doc1','s1','c1',1,'一、选择题','big',"
                "1,1,'一、选择题','oldhash',1.0)")

    def test_v1_cache_is_invalidated_so_new_columns_get_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._v1_database(path)
            result = refresh_exam_questions(path, "doc1")
            self.assertTrue(result["changed"], "带 schema 版本的指纹必须让 v1 缓存失配")
            rows = list_exam_questions(path, "doc1")
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0]["question_id"], "q-old", "旧行被本次重算回收")
            self.assertIn("问一", rows[0]["stem"], "v2 新列必须被补齐")
            self.assertEqual(len(rows[0]["subparts"]), 2)
            self.assertEqual(rows[0]["points"], 10)
            # 重算之后再刷一次：稳定，不重复劳动
            self.assertFalse(refresh_exam_questions(path, "doc1")["changed"])

    def test_v2_database_created_from_scratch_has_no_legacy_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(
                    """
                    CREATE TABLE learning_documents (
                        document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                        sub_id TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                        updated_at REAL NOT NULL DEFAULT 0);
                    CREATE TABLE learning_document_pages (
                        document_id TEXT NOT NULL, page_num INTEGER NOT NULL,
                        text TEXT NOT NULL DEFAULT '', text_hash TEXT NOT NULL DEFAULT '');
                    """
                )
                db.execute("INSERT INTO learning_documents VALUES('doc1','c1','s1','真题','sha1',1.0)")
                db.execute("INSERT INTO learning_document_pages VALUES('doc1',1,'一、题','h1')")
            self.assertTrue(refresh_exam_questions(path, "doc1")["changed"])
            self.assertFalse(refresh_exam_questions(path, "doc1")["changed"])


class ModuleHygieneTests(unittest.TestCase):
    """N9-A2 U3：模块自身卫生钉。"""

    def test_module_compiles_without_syntax_warning(self) -> None:
        # docstring 里嵌正则示例（\s/\d），必须 raw string——否则每次启动
        # import 都打一行 SyntaxWarning（N9-A 活体实测 app 启动日志噪音）。
        source_path = Path(__file__).resolve().parents[1] / "src" / "runtime" / "exam_paper_split.py"
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            compile(source_path.read_bytes(), str(source_path), "exec")


if __name__ == "__main__":
    unittest.main()
