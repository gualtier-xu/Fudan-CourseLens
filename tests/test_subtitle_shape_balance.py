# SUBTITLE-DEEP-1 Phase C（总控补充行 2026-09-29）字幕断句均衡钉面：
# 标点句读装箱 [12,22] 软目标带、硬帽 30 不变、单句读超帽退软边界/保留、
# 标点 cue（worker v3 供点）激活句读拆分路径。
from __future__ import annotations

import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.runtime.subtitle_reader import (
    SHAPE_BALANCE_MAX_CHARS,
    SHAPE_BALANCE_MIN_CHARS,
    SHAPE_SPLIT_MAX_CHARS,
    shape_display_cues_with_stats,
)


def _cue(start_ms: int, end_ms: int, text: str, **extra) -> dict:
    row = {"start_ms": start_ms, "end_ms": end_ms, "text": text}
    row.update(extra)
    return row


class BalancedPackingTests(unittest.TestCase):
    def test_constants_are_named(self) -> None:
        self.assertEqual(SHAPE_BALANCE_MIN_CHARS, 12)
        self.assertEqual(SHAPE_BALANCE_MAX_CHARS, 22)
        self.assertEqual(SHAPE_SPLIT_MAX_CHARS, 30)

    def test_long_punctuated_sentence_splits_into_balanced_bands(self) -> None:
        # 46 字多句读长句：标点拆分激活，块应尽量落 [12,22] 且全部 ≤30
        text = "这一节我们讲组合逻辑的设计方法，从真值表出发写出表达式，然后用卡诺图化简"
        rows = [_cue(0, 12_000, text)]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertGreater(len(shaped), 1)
        self.assertEqual(stats["split_units"], 1)
        lengths = [len(str(item["text"])) for item in shaped]
        self.assertTrue(all(length <= SHAPE_SPLIT_MAX_CHARS for length in lengths))
        self.assertTrue(
            any(SHAPE_BALANCE_MIN_CHARS <= length <= SHAPE_BALANCE_MAX_CHARS for length in lengths),
            f"至少一块落在均衡带：{lengths}",
        )
        # 拆分块按时间顺序切割源段锚窗
        self.assertEqual(shaped[0]["start_ms"], 0)
        self.assertEqual(shaped[-1]["end_ms"], 12_000)

    def test_unpunctuated_long_sentence_still_keeps_soft_boundary_path(self) -> None:
        # 零标点长句：均衡装箱产不出块 → 退话轮标记词软拆（夜10-C 语义不回退）
        text = "首先我们讲组合逻辑的设计方法然后从真值表出发写出逻辑表达式然后我们用卡诺图化简"
        rows = [_cue(0, 12_000, text)]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertGreater(len(shaped), 1)
        self.assertEqual(stats["soft_split_units"], 1)
        self.assertTrue(all(len(str(item["text"])) <= SHAPE_SPLIT_MAX_CHARS for item in shaped))

    def test_single_overlong_clause_without_boundary_is_kept(self) -> None:
        # 单句读 >30 且无标记词：保留整句计数（禁硬截断）
        text = "这是一个没有任何句读也没有任何话轮标记词的超长句子需要保持原样不硬截断"
        rows = [_cue(0, 12_000, text)]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(len(shaped), 1)
        self.assertEqual(shaped[0]["text"], text)
        self.assertEqual(stats["residual_overlong"], 1)

    def test_worker_punctuated_cues_flow_through_shaping(self) -> None:
        # worker v3 供点后的真实形态：短标点 cue 贴并（既有语义）、超长 cue
        # 标点/标记词拆分，全部 ≤30 且源锚透传
        rows = [
            _cue(0, 4_000, "电场就能得到电势，"),
            _cue(4_100, 9_000, "然后再负 q 一下就能得到能带图，"),
            _cue(9_100, 20_000, "这个过程我们叫作半导体器件的能带结构分析接下来看费米能级的位置分布"),
        ]
        shaped, _stats = shape_display_cues_with_stats(rows)
        texts = [str(item["text"]) for item in shaped]
        self.assertIn("电场就能得到电势，", texts[0], "短标点 cue 贴并保留句读")
        self.assertTrue(
            any("这个过程" in text and text != rows[2]["text"] for text in texts),
            f"超长第三条被拆分：{texts}",
        )
        self.assertTrue(all(len(text) <= SHAPE_SPLIT_MAX_CHARS for text in texts))
        for item in shaped:
            self.assertTrue(item["source_ids"])


if __name__ == "__main__":
    unittest.main()
