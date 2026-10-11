"""Assessment IR v1：可追溯题目存储与验证。

覆盖：题型映射、多小问/选项/分值、跨页题、缺答案、答案冲突、幂等刷新、
文档删除级联与失据状态、缺页/无文本降级、条数上限、合同身份对齐。
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from shared.course_knowledge_contract import assessment_item_id_for, citation_id_for, validate_evidence_ref

from src.runtime.exam_paper_split import refresh_exam_questions

from src.runtime.assessment_ir import (
    ANSWER_SOURCES,
    ASSESSMENT_KINDS,
    SCHEMA,
    STATUSES,
    assessment_evidence_ref,
    assessment_view,
    build_assessment_item,
    delete_assessment_items,
    ensure_assessment_ir_schema,
    group_assessment_items,
    kind_for_document,
    list_assessment_items,
    mark_conflicts,
    normalize_assessment_item,
    refresh_assessment_items,
    save_assessment_items,
    sweep_orphan_assessment_items,
)

DOC_SHA = "a" * 64


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DatabaseFixture(unittest.TestCase):
    """最小 learning DB：只建 assessment_ir 需要的两张表。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "learning.db"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(
                """
                CREATE TABLE learning_documents (
                    document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
                    doc_type TEXT NOT NULL DEFAULT 'other', updated_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE learning_document_pages (
                    document_id TEXT NOT NULL, page_num INTEGER NOT NULL,
                    text TEXT NOT NULL DEFAULT '', text_hash TEXT NOT NULL DEFAULT '');
                """
            )

    def add_document(self, document_id="doc-exam", *, course_id="c1", sub_id="s1",
                     doc_type="exam_paper", title="往年真题", pages=()):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO learning_documents VALUES(?,?,?,?,?,?,?)",
                (document_id, course_id, sub_id, title, DOC_SHA, doc_type, 1.0),
            )
            for index, text in enumerate(pages, start=1):
                db.execute("INSERT OR REPLACE INTO learning_document_pages VALUES(?,?,?,?)",
                           (document_id, index, text, _text_hash(text)))


class KindMappingTests(unittest.TestCase):
    def test_assessment_document_types_map_to_kinds(self):
        self.assertEqual(kind_for_document("exam_paper"), "exam_paper")
        self.assertEqual(kind_for_document("homework"), "homework")
        self.assertEqual(kind_for_document(" quiz "), None, "非考核类文档不产出题目")

    def test_non_assessment_documents_produce_no_items(self):
        for doc_type in ("courseware", "textbook", "notes", "other", "", None):
            self.assertIsNone(kind_for_document(doc_type))


class NormalizationTests(unittest.TestCase):
    def _item(self, **overrides):
        value = {
            "course_id": "c1", "sub_id": "s1", "document_id": "doc-exam",
            "kind": "exam_paper", "question_no": 1, "stem": "1. 题干",
            "answer": "", "answer_source": "none",
            "content_hash": "c" * 32,
        }
        value.update(overrides)
        return value

    def test_answer_and_source_must_be_consistent(self):
        self.assertIsNotNone(normalize_assessment_item(self._item()))
        # 有答案却声明无来源
        self.assertIsNone(normalize_assessment_item(self._item(answer="答案")))
        # 声明有来源却没有答案
        self.assertIsNone(normalize_assessment_item(self._item(answer_source="official")))
        # AI 生成的答案不得冒充 official：来源是自报字段，非法值直接拒绝
        self.assertIsNone(normalize_assessment_item(
            self._item(answer="答案", answer_source="offical")))

    def test_required_fields_and_ranges_are_enforced(self):
        for broken in (
            self._item(course_id=""), self._item(document_id=""), self._item(stem="  "),
            self._item(kind="quiz_paper"), self._item(question_no=0),
            self._item(question_no="x"), self._item(content_hash="zz"),
            self._item(answer_source="nonexistent"),
        ):
            self.assertIsNone(normalize_assessment_item(broken), broken)
        self.assertIsNone(normalize_assessment_item("不是字典"))

    def test_closed_sets_are_exported_for_callers(self):
        self.assertIn("ai_generated", ANSWER_SOURCES)
        self.assertIn("official", ANSWER_SOURCES)
        self.assertIn("question_only", STATUSES)
        self.assertIn("answer_available", STATUSES)
        self.assertIn("conflicted", STATUSES)
        self.assertIn("orphaned", STATUSES)
        self.assertEqual(len(ASSESSMENT_KINDS), 4)

    def test_evidence_refs_use_contract_locator_shapes(self):
        value = normalize_assessment_item(self._item(evidence_refs=[
            {"kind": "document_page", "page": 3},
            {"kind": "transcript", "start_ms": 1000, "end_ms": 2000},
            {"kind": "assessment_item", "question_no": 5},
            {"kind": "slide", "page": 0},          # 页码非法
            {"kind": "unknown_kind", "page": 1},   # 种类越界
        ]))
        self.assertEqual(value["evidence_refs"],
                         [{"kind": "document_page", "page": 3},
                          {"kind": "transcript", "start_ms": 1000, "end_ms": 2000},
                          {"kind": "assessment_item", "question_no": 5}])

    def test_ai_generated_answers_never_become_official(self):
        value = normalize_assessment_item(
            self._item(answer="模型给的答案", answer_source="ai_generated"))
        self.assertEqual(value["answer_source"], "ai_generated")
        other = build_assessment_item(
            course_id="c1", sub_id="s1", document_id="doc-exam", kind="exam_paper",
            question_no=2, stem="2. 题\n参考答案：材料自带的答案", document_sha256=DOC_SHA)
        # 材料自带但出处不可验证 → 记最低可信档，绝不记 official
        self.assertEqual(other["answer_source"], "user_material")
        self.assertNotEqual(other["answer_source"], "official")


class ItemIdentityTests(unittest.TestCase):
    def test_item_id_matches_the_frozen_contract_derivation(self):
        item = normalize_assessment_item({
            "course_id": "c1", "document_id": "doc-exam", "kind": "exam_paper",
            "question_no": 7, "stem": "7. 题", "answer_source": "none",
            "content_hash": "d" * 32,
        })
        expected = assessment_item_id_for({
            "course_id": "c1", "document_id": "doc-exam", "question_no": 7,
            "content_hash": "d" * 32,
        })
        self.assertEqual(item["item_id"], expected)
        self.assertEqual(item["assessment_id"], expected, "assessment_id 是兼容别名")
        self.assertTrue(item["item_id"].startswith("cka:"))

    def test_evidence_ref_is_a_valid_contract_citation(self):
        item = normalize_assessment_item({
            "course_id": "c1", "document_id": "doc-exam", "kind": "exam_paper",
            "question_no": 7, "stem": "7. 题", "answer_source": "none",
            "content_hash": "d" * 32, "revision_id": "e" * 16, "label": "真题 第7题",
        })
        ref = assessment_evidence_ref(item)
        canonical = validate_evidence_ref(ref, "c1")
        self.assertEqual(canonical["kind"], "assessment_item")
        self.assertEqual(canonical["locator"], {"question_no": 7})
        self.assertEqual(citation_id_for(ref), canonical["citation_id"])
        for banned in ("answer", "answer_source", "stem"):
            self.assertNotIn(banned, ref, "引用视图不得携带任何答案字段")


class RefreshAndStorageTests(DatabaseFixture):
    def test_refresh_projects_pages_into_items(self):
        self.add_document(pages=[
            "一、选择题（10 分）\n下列正确的是\nA. 甲\nB. 乙\n(1) 选一个\n(2) 说明理由",
            "（接上页）参考答案：甲正确，因为…",
            "2. 计算题（15 分）\n求 p_c。\n本题无参考答案。",
        ])
        first = refresh_assessment_items(self.path, "doc-exam")
        self.assertTrue(first["changed"])
        self.assertEqual(first["items"], 2)
        items = list_assessment_items(self.path, course_id="c1")
        self.assertEqual([item["question_no"] for item in items], [1, 2])
        with_answer, without = items
        self.assertEqual(with_answer["status"], "answer_available")
        self.assertEqual(with_answer["answer_source"], "user_material")
        self.assertIn("甲正确", with_answer["answer"])
        self.assertNotIn("参考答案", with_answer["stem"], "题干与答案分开存")
        self.assertEqual(without["status"], "question_only")
        self.assertEqual(without["answer_source"], "none")
        self.assertEqual(without["answer"], "", "无答案就是空，绝不推断")
        self.assertEqual(without["points"], 15)

    def test_refresh_is_idempotent_and_resumes_on_restart(self):
        self.add_document(pages=["一、选择题\n1. 题一", "1. 题二"])
        first = refresh_assessment_items(self.path, "doc-exam")
        second = refresh_assessment_items(self.path, "doc-exam")
        self.assertFalse(second["changed"], "内容未变则跳过")
        self.assertEqual(second["items"], first["items"])
        rows = list_assessment_items(self.path, course_id="c1")
        self.assertEqual(len(rows), len({row["item_id"] for row in rows}), "不得产生重复行")
        # 模拟重启：重新建 schema 再刷一次，仍然稳定
        ensure_assessment_ir_schema(self.path)
        third = refresh_assessment_items(self.path, "doc-exam")
        self.assertFalse(third["changed"])
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1")), len(rows))

    def test_refresh_replaces_items_when_pages_change(self):
        self.add_document(pages=["一、题一", "（10 分）题二"])
        refresh_assessment_items(self.path, "doc-exam")
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1")), 2)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM learning_document_pages WHERE document_id='doc-exam'")
            db.execute("INSERT INTO learning_document_pages VALUES('doc-exam',1,'一、只要一题','h9')")
        result = refresh_assessment_items(self.path, "doc-exam")
        self.assertTrue(result["changed"])
        items = list_assessment_items(self.path, course_id="c1")
        self.assertEqual(len(items), 1, "旧题号的行必须被回收，不能残留")

    def test_non_assessment_document_is_skipped(self):
        self.add_document(document_id="doc-notes", doc_type="notes", pages=["笔记正文"])
        result = refresh_assessment_items(self.path, "doc-notes")
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "not_assessment_document")
        self.assertEqual(list_assessment_items(self.path, course_id="c1"), [])

    def test_missing_document_raises_keyerror(self):
        with self.assertRaises(KeyError):
            refresh_assessment_items(self.path, "doc-nonexistent")

    def test_pages_without_text_degrade_honestly(self):
        self.add_document(pages=["", "   "])
        result = refresh_assessment_items(self.path, "doc-exam")
        self.assertTrue(result["changed"])
        self.assertEqual(result["items"], 0, "无文本页不猜题")

    def test_list_limit_is_applied(self):
        items = [build_assessment_item(
            course_id="c1", sub_id="s1", document_id="doc-exam", kind="exam_paper",
            question_no=index, stem=f"{index}. 第{index}题", document_sha256=DOC_SHA)
            for index in range(1, 31)]
        save_assessment_items(self.path, items)
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1", limit=10)), 10)
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1")), 30)


class ConflictTests(DatabaseFixture):
    def _two_versions(self):
        self.add_document(pages=["一、凝胶点是多少\n1. 求 p_c。\n参考答案：0.80"])
        refresh_assessment_items(self.path, "doc-exam")
        original = list_assessment_items(self.path, course_id="c1")[0]
        # 同题的另一版本文档（题干相同、答案不同、文档不同）→ 只分组，不合并
        other = dict(original)
        other["item_id"] = ""
        other["document_id"] = "doc-exam-2019"
        other["content_hash"] = hashlib.sha256(b"other").hexdigest()
        other["answer"] = "0.765"
        other["revision_id"] = "f" * 16
        save_assessment_items(self.path, [other])
        return original

    def test_grouping_keeps_both_versions_without_merging(self):
        self._two_versions()
        items = list_assessment_items(self.path, course_id="c1")
        self.assertEqual(len(items), 2, "跨版本各行保留")
        groups = group_assessment_items(items)
        self.assertEqual(len(groups), 1, "题干相同归一个组")
        group = groups[0]
        self.assertEqual(len(group["items"]), 2)
        self.assertEqual(group["revision_count"], 2)
        self.assertTrue(group["conflicted"])
        self.assertEqual(sorted(group["answers"]), ["0.765", "0.80"])

    def test_conflicts_are_marked_not_resolved(self):
        self._two_versions()
        changed = mark_conflicts(self.path, course_id="c1")
        self.assertEqual(changed, 2)
        statuses = {item["status"] for item in list_assessment_items(self.path, course_id="c1")}
        self.assertEqual(statuses, {"conflicted"})
        self.assertEqual(mark_conflicts(self.path, course_id="c1"), 0, "重复调用是幂等的")

    def test_single_version_is_not_marked_conflicted(self):
        self.add_document(pages=["一、题\n1. 求 p_c。\n参考答案：0.80"])
        refresh_assessment_items(self.path, "doc-exam")
        self.assertEqual(mark_conflicts(self.path, course_id="c1"), 0)
        self.assertEqual(list_assessment_items(self.path, course_id="c1")[0]["status"],
                         "answer_available")


class LifecycleTests(DatabaseFixture):
    def test_document_deletion_cascades_explicitly(self):
        self.add_document(pages=["一、题\n1. 求 p_c。"])
        refresh_assessment_items(self.path, "doc-exam")
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1")), 1)
        removed = delete_assessment_items(self.path, document_id="doc-exam")
        self.assertEqual(removed, 1)
        self.assertEqual(list_assessment_items(self.path, course_id="c1"), [])
        # 文档本身也删掉后，再刷新不再复活（假数据不留影子）
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM learning_documents WHERE document_id='doc-exam'")
        with self.assertRaises(KeyError):
            refresh_assessment_items(self.path, "doc-exam")

    def test_orphan_sweep_marks_items_whose_document_disappeared(self):
        self.add_document(pages=["一、题\n1. 求 p_c。"])
        refresh_assessment_items(self.path, "doc-exam")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM learning_documents WHERE document_id='doc-exam'")
        self.assertEqual(sweep_orphan_assessment_items(self.path), 1)
        item = list_assessment_items(self.path, course_id="c1")[0]
        self.assertEqual(item["status"], "orphaned")
        self.assertEqual(item["stem"], "一、题" + chr(10) + "1. 求 p_c。",
                         "失据只标状态，不删行、不改内容")
        self.assertEqual(sweep_orphan_assessment_items(self.path), 0)

    def test_schema_is_additive_and_safe_without_documents_table(self):
        bare = Path(self._tmp.name) / "bare.db"
        ensure_assessment_ir_schema(bare)
        ensure_assessment_ir_schema(bare)  # 重复初始化幂等
        save_assessment_items(bare, [{
            "course_id": "c1", "document_id": "doc-x", "kind": "exam_paper",
            "question_no": 1, "stem": "1. 题", "answer_source": "none",
            "content_hash": "e" * 32,
        }])
        self.assertEqual(len(list_assessment_items(bare, course_id="c1")), 1)
        self.assertEqual(sweep_orphan_assessment_items(bare), 0, "无文档表时不误判失据")


class StructureTests(DatabaseFixture):
    def test_multi_subparts_options_and_points(self):
        self.add_document(pages=[
            "一、综合题（20 分）\n9. 阅读材料后作答：\n(1) 写出动力学方程\n"
            "(2) 求凝胶点\n(3) 说明理由\nA. 甲\nB. 乙\nC. 丙",
        ])
        refresh_assessment_items(self.path, "doc-exam")
        item = list_assessment_items(self.path, course_id="c1")[0]
        self.assertEqual(item["structure"], "parsed")
        self.assertEqual([sub["label"] for sub in item["subparts"]], ["(1)", "(2)", "(3)"])
        self.assertEqual([option["label"] for option in item["options"]], ["A", "B", "C"])
        self.assertEqual(item["points"], 20)

    def test_cross_page_question_keeps_full_stem(self):
        self.add_document(pages=[
            "一、阅读题\n8. 第一段材料，问题在下一页",
            "（接上页）第二段材料，请计算 p_c。",
            "二、下一大题",
        ])
        refresh_assessment_items(self.path, "doc-exam")
        first = list_assessment_items(self.path, course_id="c1")[0]
        self.assertEqual((first["start_page"], first["end_page"]), (1, 2),
                         "跨页题的页码区间只覆盖自己的正文")
        self.assertIn("第二段材料", first["stem"], "跨页正文必须完整保留")
        self.assertEqual(first["structure"], "raw", "解析不出结构就老实记 raw")

    def test_schema_and_refresh_report_carry_the_schema_id(self):
        self.add_document(pages=["一、题\n1. 求 p_c。"])
        self.assertEqual(refresh_assessment_items(self.path, "doc-exam")["schema"], SCHEMA)


class CrossComponentIdentityTests(DatabaseFixture):
    """与客户端产包的身份对齐（A4 合流）：同一道题只有一个引用身份。

    早期实现里本题库自算一份 content_hash、拆题表另有 32 位版本，两者派生出的
    citation_id 不同，知识点引用的题就回指不到题库——本条钉住"只有一份身份"。
    """

    def test_ir_citation_id_equals_the_packet_builder_citation_id(self):
        from shared.course_knowledge_contract import citation_id_for

        pages_text = ["一、选择题（10 分）\n(1) 问一\n(2) 问二", "参考答案：甲正确"]
        self.add_document(pages=pages_text)
        refresh_exam_questions(self.path, "doc-exam")  # 先建拆题表（真题刷新链）
        refresh_assessment_items(self.path, "doc-exam")
        item = list_assessment_items(self.path, course_id="c1")[0]
        # 拆题表里那份身份
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            row = dict(db.execute(
                "SELECT question_id, content_hash FROM exam_questions "
                "WHERE document_id='doc-exam' ORDER BY question_no LIMIT 1").fetchone())
        self.assertEqual(item["question_id"], row["question_id"],
                         "题库与拆题表必须共享 question_id")
        self.assertEqual(item["content_hash"], row["content_hash"],
                         "题库与拆题表必须共享同一份内容哈希")
        # 客户端产包用的引用（字段逐项对照 build_evidence_packet 的 _make_ref 调用）
        packet_ref = {
            "kind": "assessment_item",
            "source_id": row["question_id"],
            "revision_id": DOC_SHA,
            "content_hash": row["content_hash"],
            "locator": {"question_no": item["question_no"]},
            "label": item["label"],
        }
        self.assertEqual(citation_id_for(assessment_evidence_ref(item)),
                         citation_id_for(packet_ref),
                         "题库引用视图与产包引用的 citation_id 必须逐位相同")

    def test_view_shape_matches_what_the_frontend_reads(self):
        self.add_document(pages=["一、题（5 分）\n1. 求 p_c。\n参考答案：0.8"])
        refresh_assessment_items(self.path, "doc-exam")
        view = assessment_view(list_assessment_items(self.path, course_id="c1")[0])
        for key in ("item_id", "course_id", "sub_id", "document_id", "question_no", "label",
                    "content_hash", "citation_ids", "answer_source", "has_answer", "answer"):
            self.assertIn(key, view, f"前端 normalizeAssessmentItem 会读 {key}")
        self.assertTrue(view["has_answer"])
        self.assertEqual(view["answer_source"], "user_material")
        self.assertTrue(view["citation_ids"][0].startswith("ckc:"))

    def test_view_never_claims_an_answer_that_is_not_there(self):
        self.add_document(pages=["一、题（5 分）\n1. 求 p_c。\n本题无参考答案。"])
        refresh_assessment_items(self.path, "doc-exam")
        view = assessment_view(list_assessment_items(self.path, course_id="c1")[0])
        self.assertFalse(view["has_answer"])
        self.assertEqual(view["answer"], "")
        self.assertEqual(view["answer_source"], "none")


class ScaleAndScopeTests(DatabaseFixture):
    """课程级资料、无标记文档、条数上限。"""

    def _items(self, count, document_id="doc-bulk", sub_id="s1"):
        return [build_assessment_item(
            course_id="c1", sub_id=sub_id, document_id=document_id, kind="exam_paper",
            question_no=index, stem=f"{index}. 第{index}题", document_sha256=DOC_SHA,
        ) for index in range(1, count + 1)]

    def test_course_level_assessment_document_keeps_empty_sub_id(self):
        """课程级真题（scope=course → sub_id 为空）照样产题，且按课程可见。"""
        self.add_document(document_id="doc-course-exam", sub_id="", title="往年真题汇编",
                          pages=["一、选择题（10 分）\n1. 第一题", "二、简答题\n2. 第二题"])
        result = refresh_assessment_items(self.path, "doc-course-exam")
        self.assertTrue(result["changed"])
        self.assertEqual(result["items"], 2)
        items = list_assessment_items(self.path, course_id="c1")
        self.assertEqual([item["sub_id"] for item in items], ["", ""])
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1", sub_id="")), 2,
                         "空 sub_id 表示不过滤（课程级题目靠 course_id 取），与既有 list_* 同约定")

    def test_document_without_markers_yields_no_items(self):
        self.add_document(pages=["这是一份普通讲义，没有任何题号。", "第二页也一样。"])
        result = refresh_assessment_items(self.path, "doc-exam")
        self.assertTrue(result["changed"])
        self.assertEqual(result["items"], 0)

    def test_default_list_limit_is_five_hundred(self):
        save_assessment_items(self.path, self._items(520))
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1")), 500,
                         "默认上限 500，防一次拉爆界面")
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1", limit=99999)), 520,
                         "超上限的请求被夹到硬顶（2000）后再截断到实际条数")
        self.assertEqual(len(list_assessment_items(self.path, course_id="c1", limit=12)), 12)

    def test_repeat_refresh_over_many_items_stays_idempotent(self):
        save_assessment_items(self.path, self._items(120))
        first = len(list_assessment_items(self.path, course_id="c1", limit=2000))
        save_assessment_items(self.path, self._items(120))
        second = len(list_assessment_items(self.path, course_id="c1", limit=2000))
        self.assertEqual(first, second, "重复写入同内容不新增行")


class TotalityAndDeterminismTests(unittest.TestCase):
    """随机性反例（固定种子，可复现）：切分与校验对任意输入都必须是全函数。

    这类测试抓的是"某类畸形输入会抛异常"——抛异常在 worker 里等于整个任务失败，
    所以宁可拒绝条目，也不能让一条脏数据把一讲拖垮。
    """

    _PIECES = (
        "一、选择题（10 分）", "二、", "12.", "12.5", "A. 甲", "（3 分）", "(1) 问",
        "参考答案：0.8", "解答：", "参考答案", "本题无参考答案。", "把答案写在答题卡上",
        "", "   ", "普通正文一句话。", "\t制表符", "emoji🙂", "换行\n内部", "％％",
        "（接上页）", "第 3 页", "＝", "“引号”", "x" * 300,
    )

    def _random_pages(self, rng, count):
        pages = []
        for index in range(1, count + 1):
            lines = [rng.choice(self._PIECES) for _ in range(rng.randint(0, 6))]
            pages.append({
                "page_num": index,
                "text": "\n".join(lines),
                "text_hash": f"h{index}",
            })
        return pages

    def test_split_questions_is_total_and_deterministic(self):
        from src.runtime.exam_paper_split import split_questions

        rng = random.Random(20260923)
        for _ in range(300):
            pages = self._random_pages(rng, rng.randint(0, 4))
            if rng.random() < 0.2:
                rng.shuffle(pages)
            first = split_questions(pages)
            second = split_questions(pages)
            self.assertEqual(first, second, "同输入必须同输出")
            for unit in first:
                for key in ("question_no", "label", "kind", "start_page", "end_page",
                            "anchor_text", "content_hash", "stem", "stem_hash",
                            "cross_page", "subparts", "options", "points", "structure"):
                    self.assertIn(key, unit)
                self.assertLessEqual(unit["start_page"], unit["end_page"])
                self.assertIn(unit["structure"], ("parsed", "raw"))
                self.assertEqual(unit["cross_page"], unit["end_page"] > unit["start_page"])

    def test_split_answer_is_total_over_arbitrary_text(self):
        from src.runtime.exam_paper_split import split_answer

        rng = random.Random(7)
        for _ in range(400):
            text = "\n".join(rng.choice(self._PIECES) for _ in range(rng.randint(0, 6)))
            question, answer = split_answer(text)
            self.assertIsInstance(question, str)
            self.assertIsInstance(answer, str)
            self.assertLessEqual(len(answer), 2000)
            if not answer:
                self.assertTrue(question or not text.strip())

    def test_malformed_pages_are_ignored_not_fatal(self):
        from src.runtime.exam_paper_split import split_questions

        for bad in ([{"page_num": None, "text": None}], [{"text": "1. 题"}], [None], [{}],
                    [{"page_num": "x", "text": "一、题"}], [{"page_num": 1}],
                    [{"page_num": -3, "text": "一、题"}]):
            with self.subTest(bad=bad):
                result = split_questions(bad)
                self.assertIsInstance(result, list)

    def test_normalize_assessment_item_is_total(self):
        rng = random.Random(11)
        pool = (None, {}, [], "x", 3, True, {"course_id": "c1"}, {"kind": "exam_paper"},
                {"course_id": "c1", "document_id": "d", "kind": "exam_paper", "question_no": 1,
                 "stem": "题", "answer_source": "none", "content_hash": "a" * 32},
                {"course_id": "c1", "document_id": "d", "kind": "exam_paper", "question_no": -1,
                 "stem": "题", "answer_source": "none", "content_hash": "zz"})
        for candidate in pool:
            for _ in range(20):
                value = dict(candidate) if isinstance(candidate, dict) else candidate
                if isinstance(value, dict) and value.get("evidence_refs") is None:
                    value["evidence_refs"] = [rng.choice(pool) for _ in range(rng.randint(0, 3))]
                normalized = normalize_assessment_item(value)
                if normalized is not None:
                    for key in ("item_id", "assessment_id", "course_id", "document_id", "kind",
                                "question_no", "stem", "answer", "answer_source", "status",
                                "content_hash", "group_key", "structure"):
                        self.assertIn(key, normalized)
                    self.assertIn(normalized["answer_source"], ANSWER_SOURCES)
                    self.assertIn(normalized["status"], STATUSES)


if __name__ == "__main__":
    unittest.main()
