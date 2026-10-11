"""RR-P6MEM-1 课程记忆反哺质量回路行为钉（AIRESEARCH P6）。

三路径：
1. **反哺闭环**——示例推导术语映射（单字错字类左扩成词级窗口、歧义丢弃/
   标点-only 跳过）；产物命中 wrong 变体 → 信号落记忆文档（计数/来源/末次），
   同文本回放幂等；信号把对应术语排到注入表前（闭环生效点）。
2. **可见标注**——只有生成侧实报注入数（metrics.course_memory_terms）才
   有学生可见人话标注；N<=0/缺报=零标注（宁缺毋滥）；总结导入把标注落进
   overview 与 generation.memory_applied。
3. **降级**——记忆缺席/损坏全链零注入零信号不抛、导入照常；学习店缺省
   kwarg 不写新键（旧形状）。

推导口径注记：difflib 对单字替换给 1 字块（如 及→级），借左邻等文左扩 3 字
成词级窗口（费米能及→费米能级）；部分改写会得到词组级映射（含共享上下文），
属预期——信号与注入都吃它，敏感度换精确度是可接受 trade。
course_memory 沉淀/字幕链注入本体由 tests/test_course_memory.py 钉住；
worker 读侧合同由 worker/tests 钉住。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication, _memory_terms_count
from src.runtime.course_memory import (
    load_course_examples,
    memory_path,
    sink_course_examples,
)
from src.runtime.course_memory_feedback import (
    _MAX_SIGNALS,
    course_memory_terms,
    derive_term_mappings,
    memory_annotation,
    record_product_deviations,
)
from src.runtime.learning_store import LearningStore

_FERM = ("这个费米能及很重要", "这个费米能级很重要，")
_WAVE = ("我们学了小波基的选取原里", "我们学了小波基的选取原理")


def _audit(before: str, after: str) -> dict:
    return {"start_ms": 0, "end_ms": 1000, "before": before, "after": after}


def _memory_with(*pairs: tuple[str, str], output_dir: Path, course_id: str) -> None:
    """按 (改前整段, 改后整段) 沉淀课程记忆。"""
    sink_course_examples(
        output_dir, course_id, [_audit(before, after) for before, after in pairs]
    )


def _hand_example(index: int, before: str, after: str) -> dict:
    return {
        "input": [{"id": f"e{index}", "text": before}],
        "ops": [{"id": f"e{index}", "old": before, "new": after}],
    }


class DeriveTermMappingTests(unittest.TestCase):
    """推导层：整段修正 → 术语级写法映射。"""

    def test_segment_correction_yields_term_pair(self) -> None:
        mappings = derive_term_mappings([_hand_example(0, *_FERM)])
        self.assertEqual(mappings, [("费米能及", "费米能级")])

    def test_single_char_correction_gets_word_window(self) -> None:
        # 里→理 是 1 字替换：左扩出「选取原里→选取原理」词级窗口
        mappings = derive_term_mappings([_hand_example(0, *_WAVE)])
        self.assertEqual(mappings, [("选取原里", "选取原理")])

    def test_punctuation_only_blocks_produce_no_mappings(self) -> None:
        self.assertEqual(derive_term_mappings([_hand_example(0, "今天,讲课", "今天，讲课")]), [])

    def test_identical_segments_produce_no_mappings(self) -> None:
        self.assertEqual(derive_term_mappings([_hand_example(0, "完全相同", "完全相同")]), [])

    def test_ambiguous_wrong_is_dropped(self) -> None:
        # 同一个 wrong（费米能及）映到两个不同 right：无证据不成对，整组丢弃
        mappings = derive_term_mappings([
            _hand_example(0, *_FERM),
            _hand_example(1, "费米能及的例子", "费米能律的例子"),
        ])
        self.assertEqual(mappings, [])

    def test_malformed_items_are_skipped(self) -> None:
        mappings = derive_term_mappings([
            "not-a-dict",
            {"input": [], "ops": []},
            _hand_example(0, "好的示例", "好的示例，"),  # 仅标点：无映射
        ])
        self.assertEqual(mappings, [])


class FeedbackLoopTests(unittest.TestCase):
    """反哺闭环：信号落账、幂等、注入排序生效。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        _memory_with(_FERM, _WAVE, output_dir=self.output_dir, course_id="course-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_deviation_signals_written_and_counted(self) -> None:
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1",
            "本讲要点：费米能及的物理意义。", source="summary",
        )
        self.assertEqual((hits, recorded), (1, 1))
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        signals = document["signals"]
        self.assertEqual(len(signals), 1)
        self.assertEqual((signals[0]["wrong"], signals[0]["right"]), ("费米能及", "费米能级"))
        self.assertEqual(signals[0]["count"], 1)
        self.assertEqual(signals[0]["sources"], {"summary": 1})

    def test_same_text_replay_is_idempotent(self) -> None:
        text = "本讲要点：费米能及的物理意义。"
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-1", text, source="summary"),
            (1, 1),
        )
        # 回放：命中仍如实观测，但零记账（recorded=0）
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-1", text, source="summary"),
            (1, 0),
        )
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        self.assertEqual(document["signals"][0]["count"], 1)

    def test_alternating_texts_stay_idempotent(self) -> None:
        # QA-SWEEP-1 P1-5：单哈希幂等会被 A→B→A 交替导入击穿（B 把 A 从
        # 「末次文本」上洗掉，A 再来时被当新文本重复计数）。每信号存最近
        # K 个文本哈希的有界环，环内命中一律跳过。
        text_a = "第一段：费米能及出现了。"
        text_b = "第二段：费米能及又出现了。"
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-1", text_a, source="summary"),
            (1, 1),
        )
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-1", text_b, source="answer"),
            (1, 1),
        )
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-1", text_a, source="summary"),
            (1, 0),
        )
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        signal = document["signals"][0]
        self.assertEqual(signal["count"], 2)
        self.assertEqual(signal["sources"], {"summary": 1, "answer": 1})

    def test_recent_hash_ring_is_bounded(self) -> None:
        # 环有界（K=8）：不算历史账的无限回放也不会让记忆文档无界膨胀。
        for index in range(12):
            record_product_deviations(
                self.output_dir, "course-1", f"第{index}段：费米能及出现。", source="summary"
            )
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        ring = document["signals"][0]["recent_text_hashes"]
        self.assertLessEqual(len(ring), 8)

    def test_hits_and_recorded_are_reported_separately(self) -> None:
        # QA-SWEEP-1 P2-12：命中数（观测口径）与实记账数（写账口径）分开
        # 返回——回放时 hits>0 而 recorded=0，旧单值口径把两者混成虚账。
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及与选取原里各错一次。", source="summary"
        )
        self.assertEqual((hits, recorded), (2, 2))
        replay_hits, replay_recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及与选取原里各错一次。", source="summary"
        )
        self.assertEqual((replay_hits, replay_recorded), (2, 0))

    def test_signal_cap_evicts_oldest_by_last_seen(self) -> None:
        # QA-SWEEP-1 P2-13：超帽淘汰按 last_seen 最旧，不按插入序——刚
        # 发生的信号绝不因排位靠后被挤掉。
        path = memory_path(self.output_dir, "course-1")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["signals"] = [
            {"wrong": f"旧词{index:03d}", "right": f"新词{index:03d}", "count": 1,
             "first_seen": float(index), "last_seen": float(index), "sources": {},
             "last_text_hash": ""}
            for index in range(_MAX_SIGNALS)
        ]
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及出现在末尾。", source="summary"
        )
        self.assertEqual((hits, recorded), (1, 1))
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(document["signals"]), _MAX_SIGNALS)
        wrongs = {signal["wrong"] for signal in document["signals"]}
        self.assertIn("费米能及", wrongs)  # 最新信号在账
        self.assertNotIn("旧词000", wrongs)  # last_seen 最旧的被淘汰
        self.assertIn("旧词099", wrongs)

    def test_new_text_bumps_count_and_sources(self) -> None:
        record_product_deviations(
            self.output_dir, "course-1", "第一讲：费米能及引入。", source="summary"
        )
        record_product_deviations(
            self.output_dir, "course-1", "提问里出现费米能及。", source="answer"
        )
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        signal = document["signals"][0]
        self.assertEqual(signal["count"], 2)
        self.assertEqual(signal["sources"], {"summary": 1, "answer": 1})

    def test_signal_prioritizes_term_in_injection_order(self) -> None:
        # 无信号：按首见序
        self.assertEqual(
            course_memory_terms(self.output_dir, "course-1"), ("费米能级", "选取原理")
        )
        # 反哺生效：带偏差信号的术语排到注入表最前
        record_product_deviations(
            self.output_dir, "course-1", "这里写成了选取原里。", source="summary"
        )
        self.assertEqual(
            course_memory_terms(self.output_dir, "course-1"), ("选取原理", "费米能级")
        )

    def test_right_form_only_text_produces_no_signal(self) -> None:
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能级与选取原理都正确。", source="summary"
        )
        self.assertEqual((hits, recorded), (0, 0))
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        self.assertNotIn("signals", document)

    def test_missing_or_corrupt_memory_fail_closed(self) -> None:
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-none", "费米能及", source="summary"),
            (0, 0),
        )
        path = memory_path(self.output_dir, "course-bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(
            record_product_deviations(self.output_dir, "course-bad", "费米能及", source="summary"),
            (0, 0),
        )
        self.assertEqual(course_memory_terms(self.output_dir, "course-bad"), ())
        self.assertEqual(course_memory_terms(self.output_dir, ""), ())

    def test_examples_survive_signal_writes(self) -> None:
        record_product_deviations(
            self.output_dir, "course-1", "费米能及又出现了。", source="summary"
        )
        document = json.loads(memory_path(self.output_dir, "course-1").read_text(encoding="utf-8"))
        self.assertEqual(len(document["examples"]), 2)
        self.assertEqual(len(document["signals"]), 1)
        # 信号写入后读取面仍同一口径（示例数不变）
        self.assertEqual(len(load_course_examples(self.output_dir, "course-1")), 2)


class MemoryAnnotationTests(unittest.TestCase):
    """可见标注：真注入才有人话行；kind 决定主语。"""

    def test_zero_and_negative_and_garbage_are_empty(self) -> None:
        self.assertEqual(memory_annotation(0), "")
        self.assertEqual(memory_annotation(-1), "")
        self.assertEqual(memory_annotation("bad"), "")  # type: ignore[arg-type]

    def test_answer_and_summary_wording(self) -> None:
        self.assertEqual(memory_annotation(3), "\n\n—— 本回答已应用课程记忆 3 条")
        self.assertEqual(memory_annotation(2, kind="summary"), "\n\n—— 本笔记已应用课程记忆 2 条")

    def test_metrics_gate(self) -> None:
        self.assertEqual(CourseLensApplication._memory_note_for({}, kind="answer"), "")
        self.assertEqual(CourseLensApplication._memory_note_for(None, kind="answer"), "")
        self.assertEqual(
            CourseLensApplication._memory_note_for({"course_memory_terms": 2}, kind="answer"),
            "\n\n—— 本回答已应用课程记忆 2 条",
        )
        self.assertEqual(_memory_terms_count({"metrics": {"course_memory_terms": 4}}), 4)
        self.assertEqual(_memory_terms_count({"metrics": {"course_memory_terms": "x"}}), 0)
        self.assertEqual(_memory_terms_count({}), 0)


class SummaryImportAnnotationTests(unittest.TestCase):
    """总结导入面：标注落 overview + generation.memory_applied + 反哺检查。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        self.app = CourseLensApplication(self.output_dir)
        self.app._run_assessment_radar = Mock()
        self.app._rebuild_course_knowledge = Mock(return_value={"state": "skipped"})

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _result(self, *, with_memory: bool) -> dict:
        metrics = {"deepseek_tokens": 10}
        if with_memory:
            metrics["course_memory_terms"] = 2
        return {
            "status": "completed",
            "input_hash": "a" * 64,
            "outputs": {"summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []}},
            "metrics": metrics,
        }

    def _stored_summary(self, sub_id: str) -> dict:
        artifact = self.app.learning_store.find_ai_artifact(sub_id, "timestamp_summary")
        return dict(artifact["content"]) if artifact else {}

    def test_import_annotates_when_memory_reported(self) -> None:
        _memory_with(_FERM, output_dir=self.app.output_dir, course_id="course")
        self.app._import_remote_summary_result("course", "lecture", self._result(with_memory=True))
        stored = self._stored_summary("lecture")
        self.assertTrue(stored["overview"].endswith("—— 本笔记已应用课程记忆 2 条"))
        self.assertEqual(stored["generation"]["memory_applied"], 2)

    def test_import_without_report_stays_clean(self) -> None:
        # 旧结果/回放没有注入数实报 → 零标注、零新键（宁缺毋滥+旧形状）
        self.app._import_remote_summary_result("course", "lecture", self._result(with_memory=False))
        stored = self._stored_summary("lecture")
        self.assertEqual(stored["overview"], "## 第一讲\n\n要点甲。")
        self.assertNotIn("memory_applied", stored["generation"])

    def test_import_feeds_back_deviations(self) -> None:
        _memory_with(_FERM, output_dir=self.app.output_dir, course_id="course")
        result = self._result(with_memory=False)
        result["outputs"]["summary"]["markdown"] = "## 第一讲\n\n费米能及很关键。"
        self.app._import_remote_summary_result("course", "lecture", result)
        document = json.loads(
            memory_path(self.app.output_dir, "course").read_text(encoding="utf-8")
        )
        self.assertEqual(document["signals"][0]["wrong"], "费米能及")

    def test_import_survives_corrupt_memory(self) -> None:
        # 记忆损坏：标注信任生成侧实报照常落；反哺检查零副作用不抛
        path = memory_path(self.app.output_dir, "course")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        self.app._import_remote_summary_result("course", "lecture", self._result(with_memory=True))
        stored = self._stored_summary("lecture")
        self.assertTrue(stored["overview"].endswith("—— 本笔记已应用课程记忆 2 条"))
        self.assertEqual(stored["generation"]["memory_applied"], 2)


class LearningStoreMemoryFieldTests(unittest.TestCase):
    """学习店：memory_applied 加性 kwarg（>0 才写，省键=旧形状）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.store = LearningStore(Path(self.temporary.name) / "learning.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, **kwargs) -> dict:
        self.store.import_remote_summary(
            course_id="course", sub_id="lecture", input_hash="a" * 64,
            model="deepseek-flash", markdown="正文", chapters=[], ppt_pages=[],
            **kwargs,
        )
        artifact = self.store.find_ai_artifact("lecture", "timestamp_summary")
        return dict(artifact["content"])

    def test_memory_applied_stored_when_positive(self) -> None:
        stored = self._import(memory_applied=3)
        self.assertEqual(stored["generation"]["memory_applied"], 3)

    def test_default_and_nonpositive_omit_key(self) -> None:
        self.assertNotIn("memory_applied", self._import())
        self.assertNotIn("memory_applied", self._import(memory_applied=0))


class CourseMemoryTermsHelperTests(unittest.TestCase):
    """应用层注入 helper：缺席/损坏=空列表（省键=旧行为）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_terms_empty_without_memory(self) -> None:
        self.assertEqual(self.app._course_memory_terms("course"), [])

    def test_terms_flow_from_memory(self) -> None:
        _memory_with(_FERM, output_dir=self.app.output_dir, course_id="course")
        self.assertEqual(self.app._course_memory_terms("course"), ["费米能级"])


class AutoPromotionTests(unittest.TestCase):
    """THINK-LADDER-1 确定性自动晋升：信号 ≥3 → 自动确认（provenance+可撤销）。

    语义闭集：低于阈值不确认；阈值一到进 confirmed_mappings 同落点（进
    P10 字幕链注入词表）；同文本回放幂等不重复；dismiss 撤销自动晋升件
    且撤销后不再晋升（用户终态裁决）；人工确认件保持一次性终态。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        _memory_with(_FERM, output_dir=self.output_dir, course_id="course-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _document(self) -> dict:
        return json.loads(
            memory_path(self.output_dir, "course-1").read_text(encoding="utf-8")
        )

    def _confirmed(self) -> list[tuple[str, str]]:
        return [
            (row["wrong"], row["right"])
            for row in self._document().get("confirmed_mappings", [])
        ]

    def _ferm_texts(self) -> list[str]:
        return [
            "先看费米能及这个概念。",
            "再说一遍费米能及的问题。",
            "最后总结费米能及的处理。",
        ]

    def test_below_threshold_not_confirmed(self) -> None:
        for text in self._ferm_texts()[:2]:
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        self.assertEqual(self._confirmed(), [], "信号 <3 不自动确认")
        signals = self._document()["signals"]
        self.assertEqual(signals[0]["count"], 2, "信号本身照常记账")

    def test_threshold_reached_auto_confirms_with_provenance(self) -> None:
        for text in self._ferm_texts():
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        self.assertEqual(self._confirmed(), [("费米能及", "费米能级")])
        row = self._document()["confirmed_mappings"][0]
        self.assertTrue(row["auto_confirmed"], "provenance 加性键")
        self.assertEqual(row["auto_signal_count"], 3)
        self.assertIn("confirmed_at", row, "与人工确认同落点同形状")
        # 晋升件进 P10 字幕链注入词表（confirm 同落点的生效点）
        from src.runtime.course_memory_feedback import confirmed_memory_terms

        self.assertIn("费米能级", confirmed_memory_terms(self.output_dir, "course-1"))

    def test_single_text_triple_occurrence_confirms(self) -> None:
        # 次数口径=信号计数：单文本命中 3 处同样过阈值
        record_product_deviations(
            self.output_dir, "course-1",
            "费米能及出现在三处：费米能及、费米能及，都在这段。",
            source="summary",
        )
        self.assertEqual(self._confirmed(), [("费米能及", "费米能级")])

    def test_replay_idempotent_keeps_single_ledger_row(self) -> None:
        for text in self._ferm_texts():
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        # 回放第一段：hits>0 recorded=0，不重复记账不重复晋升
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", self._ferm_texts()[0], source="summary",
        )
        self.assertEqual(recorded, 0)
        self.assertEqual(self._confirmed(), [("费米能及", "费米能级")])
        self.assertEqual(len(self._document()["confirmed_mappings"]), 1)

    def test_dismiss_undoes_auto_promotion_and_blocks_repromote(self) -> None:
        from src.runtime.course_memory_feedback import dismiss_term_mapping

        for text in self._ferm_texts():
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        result = dismiss_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级",
        )
        self.assertTrue(result["dismissed"], "自动晋升件可撤销")
        self.assertEqual(self._confirmed(), [], "移出确认台账")
        document = self._document()
        self.assertEqual(
            [(row["wrong"], row["right"]) for row in document["dismissed_mappings"]],
            [("费米能及", "费米能级")],
        )
        # 撤销后再命中：dismissed 终态挡住再晋升（用户终态裁决不翻案）
        record_product_deviations(
            self.output_dir, "course-1", "又一次费米能及的偏差。", source="summary",
        )
        self.assertEqual(self._confirmed(), [])

    def test_manual_confirm_stays_one_shot(self) -> None:
        from src.runtime.course_memory_feedback import (
            confirm_term_mapping,
            dismiss_term_mapping,
        )

        confirm_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级",
        )
        result = dismiss_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级",
        )
        self.assertFalse(result["dismissed"], "人工确认件保持一次性终态")
        self.assertEqual(self._confirmed(), [("费米能及", "费米能级")])
        # 后续信号到达：已人工确认（终态）不重复晋升不换 provenance
        for text in self._ferm_texts():
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        self.assertEqual(len(self._document()["confirmed_mappings"]), 1)
        self.assertNotIn("auto_confirmed", self._document()["confirmed_mappings"][0])

    def test_auto_confirmed_view_lists_promotions_only(self) -> None:
        from src.runtime.course_memory_feedback import (
            auto_confirmed_term_rows,
            confirm_term_mapping,
        )

        # 两个词对都进候选集：FERM 走自动晋升，WAVE 走人工确认（视图分流钉）
        _memory_with(
            _FERM, _WAVE, output_dir=self.output_dir, course_id="course-1",
        )
        for text in self._ferm_texts():
            record_product_deviations(
                self.output_dir, "course-1", text, source="summary",
            )
        confirm_term_mapping(
            self.output_dir, "course-1", wrong="选取原里", right="选取原理",
        )
        rows = auto_confirmed_term_rows(self.output_dir, "course-1")
        self.assertEqual(
            [(row["wrong"], row["right"]) for row in rows],
            [("费米能及", "费米能级")],
        )
        self.assertEqual(rows[0]["signal_count"], 3)
        self.assertGreater(rows[0]["confirmed_at"], 0)
        # 撤销后视图同步出列
        from src.runtime.course_memory_feedback import dismiss_term_mapping

        dismiss_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级",
        )
        self.assertEqual(auto_confirmed_term_rows(self.output_dir, "course-1"), [])


if __name__ == "__main__":
    unittest.main()
