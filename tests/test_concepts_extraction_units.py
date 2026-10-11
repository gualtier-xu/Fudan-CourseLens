"""T13 薄件增量（夜14-R7 → N15-R4 P2）：概念抽取候选与先修判定直测。

tests/test_concepts.py 已盖关系证据面（三测）；本件补抽取器纯函数面：
候选名三源（引号体/英文词/中文 TECH 后缀）、泛化后缀折叠键、英文停用词、
显式先修句式双向判定。别名/键折叠是去重合同——折叠规则静默漂移会让
同一概念分裂成两条或误合并。
"""

from __future__ import annotations

import unittest

from src.runtime.concepts import (
    CONCEPT_EXTRACTOR_VERSION,
    _GENERIC_SUFFIXES,
    _candidate_names,
    _explicit_prerequisite,
    _normal_key,
)


class CandidateNameExtractionTests(unittest.TestCase):
    def test_quoted_terms_are_candidates(self):
        names = _candidate_names("今天讲《线性代数》中的「梯度下降」与“矩阵分解”。")
        self.assertIn("线性代数", names)
        self.assertIn("梯度下降", names)
        self.assertIn("矩阵分解", names)

    def test_english_terms_survive_stopword_filter(self):
        names = _candidate_names("we use PCA and the SVD method for this course example")
        self.assertIn("PCA", names)
        self.assertIn("SVD", names)
        self.assertNotIn("the", names)
        self.assertNotIn("course", names)
        self.assertNotIn("example", names)

    def test_chinese_tech_suffix_terms_are_candidates(self):
        names = _candidate_names("我们讲梯度下降算法和协方差矩阵。")
        joined = "|".join(names)
        self.assertIn("梯度下降算法", joined)
        self.assertIn("协方差矩阵", joined)

    def test_definition_markers_yield_named_terms(self):
        names = _candidate_names("这个方法称为反向传播。")
        self.assertIn("反向传播", names)
        # 观察注记（不钉）：「叫做BP算法」这类 ASCII+中文混排词，marker 的
        # 两个分支（纯 ASCII 类/纯中文类）都接不住——现抽取器限界，如需
        # 支持须改产品正则（本件零产品改动，只记录）。

    def test_short_noise_is_filtered(self):
        names = _candidate_names("好的 是的")
        self.assertEqual(names, set())


class NormalizedKeyFoldingTests(unittest.TestCase):
    def test_generic_suffix_is_folded_in_keys(self):
        # 泛化后缀（算法/方法/模型/理论/概念）在键面折叠：防同概念多后缀分裂。
        self.assertEqual(_normal_key("遗传算法"), _normal_key("遗传"))
        self.assertEqual(_normal_key("回归方法"), _normal_key("回归"))

    def test_folding_keeps_core_longer_than_suffix(self):
        # 全名恰为后缀+单字时不折叠（保护「矩阵」这类本身就是全名的词）。
        self.assertEqual(_normal_key("矩阵"), "矩阵")

    def test_key_is_casefolded_and_whitespace_free(self):
        self.assertEqual(_normal_key("PCA  Analysis"), "pcaanalysis")


class ExplicitPrerequisiteTests(unittest.TestCase):
    def test_prerequisite_wording_before_name(self):
        self.assertTrue(_explicit_prerequisite("本课先修课程：线性代数。", "线性代数"))

    def test_name_before_prerequisite_wording(self):
        self.assertTrue(_explicit_prerequisite("线性代数是本课的前置知识。", "线性代数"))

    def test_unrelated_text_is_not_prerequisite(self):
        self.assertFalse(_explicit_prerequisite("今天讲线性代数的行列式。", "线性代数"))

    def test_exact_name_required_not_substring_noise(self):
        # 词名未出现时不得误判（escaped 全词匹配）。
        self.assertFalse(_explicit_prerequisite("先修课程：高等数学。", "线性代数"))


class ExtractorVersionPinTests(unittest.TestCase):
    def test_extractor_version_is_frozen(self):
        # 抽取器版本是证据合同的一部分（evidence-contracts 闭集面）：
        # 改版本 = 旧证据重新校验语义，必须显式过门。
        self.assertEqual(CONCEPT_EXTRACTOR_VERSION, "evidence-concepts-zh-v1")

    def test_generic_suffix_table_is_frozen(self):
        # 泛化后缀表是键折叠规则的词表合同（T13 残余钉）：增删后缀 =
        # 概念合并/分裂语义变化，须与折叠行为测试一起显式过门。
        self.assertEqual(_GENERIC_SUFFIXES, ("算法", "方法", "模型", "理论", "概念"))


if __name__ == "__main__":
    unittest.main()
