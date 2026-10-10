# -*- coding: utf-8 -*-
"""D-14-RELAX R1：中文功能词闭集与内容词提取的单元钉。

锚定缺陷 D-20261009-14：自然问句原句 FTS 逐字 AND 零命中（红钉已复现），
本文件钉住提取器行为——闭集可审、领域词不伤、边界不出格。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.runtime.query_relaxation import (
    CHINESE_FUNCTION_WORDS,
    MAX_CONTENT_TERMS,
    MAX_OR_TERMS,
    extract_content_terms,
    or_tier_terms,
)


class ClosedSetContractTests(unittest.TestCase):
    """闭集本体纪律：可审、无重复、领域词不在表内。"""

    def test_closed_set_has_no_duplicates(self) -> None:
        self.assertEqual(len(CHINESE_FUNCTION_WORDS), len(set(CHINESE_FUNCTION_WORDS)))

    def test_domain_words_never_enter_closed_set(self) -> None:
        # 闭集边界纪律：领域词一律不进表（「几」会让「几何」误伤，已裁掉）。
        for domain_word in ("光刻胶", "显影", "几何", "贝叶斯", "收敛", "量子", "定理"):
            self.assertNotIn(domain_word, CHINESE_FUNCTION_WORDS)

    def test_task_named_function_words_present(self) -> None:
        # 任务点名的功能词必须可审地存在。
        for word in ("的", "是什么", "这节课", "核心", "结论", "作用", "什么", "怎么"):
            self.assertIn(word, CHINESE_FUNCTION_WORDS)


class ExtractionTests(unittest.TestCase):
    """内容词提取：D-14 三问形态 + 边界。"""

    def test_d14_wrapped_question_extracts_topic(self) -> None:
        self.assertEqual(extract_content_terms("光刻胶的作用是什么？"), ["光刻胶"])
        self.assertEqual(extract_content_terms("光刻胶的作用"), ["光刻胶"])

    def test_paraphrased_question_keeps_second_content_word(self) -> None:
        # 「是怎么运用的」剥离后剩余两个内容块；tier3 析取靠「光刻胶」兜底。
        self.assertEqual(extract_content_terms("光刻胶是怎么运用的"), ["光刻胶", "运用"])

    def test_why_question_extracts_subject_and_predicate(self) -> None:
        self.assertEqual(extract_content_terms("链式聚合为什么收敛？"), ["链式聚合", "收敛"])

    def test_topicless_question_extracts_nothing(self) -> None:
        # 无主题词问句=无内容可检索；诚实降级是设计行为（判据 4）。
        self.assertEqual(extract_content_terms("这节课的核心结论是什么？"), [])

    def test_fullwidth_and_punctuation_are_separators(self) -> None:
        # 全角括号/顿号/问号=分隔；「的」「作用」在闭集内被剥离。
        self.assertEqual(extract_content_terms("（光刻胶）的、作用？？"), ["光刻胶"])

    def test_nfkc_fullwidth_normalization(self) -> None:
        # 全角→半角由 normalize_search_text 同源处理。
        self.assertEqual(extract_content_terms("ＧＰＵ的作用"), ["gpu"])

    def test_duplicate_terms_dedupe_preserving_order(self) -> None:
        self.assertEqual(extract_content_terms("光刻胶 光刻胶 显影"), ["光刻胶", "显影"])

    def test_single_char_fragments_filtered(self) -> None:
        # 「作用力」的子串误伤由阶梯顺序兜底（tier1 先原句精确）；提取层只保证
        # 不产出 <2 字碎块。
        self.assertEqual(extract_content_terms("作用力"), [])

    def test_empty_and_noise_only_inputs(self) -> None:
        self.assertEqual(extract_content_terms(""), [])
        self.assertEqual(extract_content_terms("？？？？"), [])
        self.assertEqual(extract_content_terms("这个那个"), [])

    def test_cap_at_max_content_terms(self) -> None:
        # 9 个双字内容块 → 封顶 MAX_CONTENT_TERMS。
        query = "甲乙和丙丁和戊己和庚辛和壬癸和子丑和寅卯和辰巳和午未"
        terms = extract_content_terms(query)
        self.assertEqual(len(terms), MAX_CONTENT_TERMS)


class OrTierTests(unittest.TestCase):
    """tier3 析取词表：去重、按长度降序、硬帽。"""

    def test_sorted_by_length_desc(self) -> None:
        self.assertEqual(or_tier_terms(["显影", "光刻胶膜层"]), ["光刻胶膜层", "显影"])

    def test_hard_cap(self) -> None:
        terms = [f"词形{index:02d}字" for index in range(12)]
        self.assertEqual(len(or_tier_terms(terms)), MAX_OR_TERMS)

    def test_dedupe(self) -> None:
        self.assertEqual(or_tier_terms(["光刻胶", "光刻胶"]), ["光刻胶"])


if __name__ == "__main__":
    unittest.main()
