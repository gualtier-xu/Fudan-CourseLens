# AS13（第五十三案）字幕 cue 确定性整形钉面：合并/删除/并入/句首剥离/安全拆分/
# 拖尾与静音上界/纯度（零标点新增、零改写、输入零变异）/证据锚点/进程内缓存。
from __future__ import annotations

import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.runtime.subtitle_reader import (
    SHAPE_MERGE_MAX_CHARS,
    SHAPE_SPLIT_MAX_CHARS,
    shape_display_cues,
    shape_display_cues_cached,
    shape_display_cues_with_stats,
)


def _cue(start_ms: int, end_ms: int, text: str, **extra) -> dict:
    row = {"start_ms": start_ms, "end_ms": end_ms, "text": text}
    row.update(extra)
    return row


class ShapeMergeTests(unittest.TestCase):
    def test_close_cues_merge_to_single_unit_with_source_anchors(self) -> None:
        rows = [
            _cue(0, 1500, "今天我们讲数字集成电路"),
            _cue(1800, 3000, "第一chapter是组合逻辑"),
        ]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(len(shaped), 1)
        unit = shaped[0]
        self.assertEqual(unit["text"], "今天我们讲数字集成电路第一chapter是组合逻辑")
        self.assertEqual(unit["start_ms"], 0)
        self.assertEqual(unit["end_ms"], 3500, "末段 end+0.5s 拖尾（末尾无下一条）")
        self.assertEqual(unit["source_ids"], [1, 2])
        self.assertGreaterEqual(unit["end_ms"] - unit["start_ms"], 1200)
        self.assertEqual(stats["merged_units"], 1)

    def test_merge_stops_at_char_budget(self) -> None:
        left = "这是一段已经很长的话 absolutely 需要注意"
        right = "这段不能再贴并了"
        self.assertGreaterEqual(len(left) + len(right), SHAPE_MERGE_MAX_CHARS)
        shaped, _stats = shape_display_cues_with_stats([
            _cue(0, 2000, left), _cue(2100, 3200, right),
        ])
        self.assertEqual(len(shaped), 2)

    def test_short_cue_merges_only_within_one_second(self) -> None:
        short = "好"
        shaped, _stats = shape_display_cues_with_stats([
            _cue(0, 800, "开始上课"), _cue(1000, 1500, short),
        ])
        self.assertEqual(len(shaped), 1, "≤6 字碎片 <1s 间隔贴并")
        # 夜10-C 数据判定：通用贴并间隔 1.5s→2.0s（微碎片残余 -1.2pp），
        # 1.8s 间隔现走通用规则贴并；边界钉随之移到 >2.0s。
        shaped2, _stats2 = shape_display_cues_with_stats([
            _cue(0, 800, "开始上课"), _cue(2600, 3200, short),
        ])
        self.assertEqual(len(shaped2), 1, "1.8s 间隔 ≤2.0s 通用贴并（夜10-C）")
        shaped3, _stats3 = shape_display_cues_with_stats([
            _cue(0, 800, "开始上课"), _cue(3000, 3200, short),
        ])
        self.assertEqual(len(shaped3), 2, "2.2s 间隔不贴并")

    def test_long_gap_keeps_cues_separate(self) -> None:
        shaped, _stats = shape_display_cues_with_stats([
            _cue(0, 2000, "上半句内容"), _cue(5000, 8000, "隔了很久的下半句内容"),
        ])
        self.assertEqual(len(shaped), 2)


class ShapeDeleteAnswerTests(unittest.TestCase):
    def test_pure_filler_cues_are_deleted(self) -> None:
        rows = [
            _cue(0, 500, "呃"), _cue(600, 1000, "嗯行"), _cue(1100, 1400, "啊对"),
            _cue(1600, 3000, "我们继续看下一页"),
        ]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(stats["deleted_fillers"], 3)
        self.assertTrue(all(row["text"] != "呃" for row in shaped))
        self.assertTrue(any("我们继续" in row["text"] for row in shaped))

    def test_answer_words_attach_as_prefix_not_deleted(self) -> None:
        rows = [
            _cue(0, 400, "对"), _cue(1200, 2600, "所以这个式子成立"),
        ]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(len(shaped), 1)
        self.assertEqual(shaped[0]["text"], "对所以这个式子成立")
        self.assertEqual(shaped[0]["source_ids"], [1, 2])
        self.assertEqual(stats["answer_attached"], 1)

    def test_trailing_answer_word_follows_generic_merge_rule(self) -> None:
        shaped, _stats = shape_display_cues_with_stats([
            _cue(0, 2000, "今天讲到这儿"), _cue(2400, 2600, "好"),
        ])
        self.assertEqual(len(shaped), 1, "尾随应答词按通用贴并规则并入前句（间隔<1.5s）")
        self.assertEqual(shaped[0]["text"], "今天讲到这儿好")

    def test_leading_filler_stripped_but_mid_sentence_fillers_kept(self) -> None:
        # 夜10-C 第九波：这个这个=口吃重复→折叠为单次（用户拍板新增语义）
        shaped, stats = shape_display_cues_with_stats([
            _cue(0, 2000, "嗯这个这个就是那个参数的话"),
        ])
        self.assertEqual(shaped[0]["text"], "这个就是那个参数的话", "句首剥离+叠词折叠")
        self.assertEqual(stats["folded_tokens"], 1)
        shaped2, _stats2 = shape_display_cues_with_stats([
            _cue(0, 2000, "这个的话嗯就是系数取负"),
        ])
        self.assertIn("嗯", shaped2[0]["text"], "句中填充词按用户拍板保留")


class ShapeNight10BoundaryTests(unittest.TestCase):
    """夜10-C 边界加深：词表扩展/边缘标点分类/应答词并入上限（T1+T2）。"""

    def test_extended_filler_alphabet_deleted_as_pure_fillers(self) -> None:
        rows = [
            _cue(0, 400, "欸"), _cue(500, 900, "哇"), _cue(1000, 1400, "嘿哟"),
            _cue(1500, 1900, "嗯啦"), _cue(2100, 4000, "我们看下一页"),
        ]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(stats["deleted_fillers"], 4, "扩展字母表纯语气 cue 删")
        self.assertTrue(any("我们看下一页" in row["text"] for row in shaped))

    def test_leading_filler_takes_its_own_punctuation(self) -> None:
        """LIVE-VALIDATE-1 LV1-2：剥离句首语气词后不留悬空标点头。

        「嗯，这个」被剥成「，这个」= 语气词自己的停顿符残留。紧随被剥
        语气词的标点随语气词一起剥；第一个实词处停止，绝不剥实词。"""
        shaped, _stats = shape_display_cues_with_stats([
            _cue(0, 2000, "嗯，这个，就是说，这个参数"),
        ])
        self.assertEqual(shaped[0]["text"], "这个，就是说，这个参数", "紧随标点随语气词剥")
        shaped2, _s2 = shape_display_cues_with_stats([
            _cue(0, 2000, "呃。 那我们看下一页"),
        ])
        self.assertEqual(shaped2[0]["text"], "那我们看下一页", "句号+空格同样随剥")
        shaped3, _s3 = shape_display_cues_with_stats([
            _cue(0, 2000, "嗯们这个参数"),
        ])
        self.assertEqual(shaped3[0]["text"], "们这个参数", "语气词本身恒剥、实词字符零剥（既有语义不变）")
        shaped4, _s4 = shape_display_cues_with_stats([
            _cue(0, 2000, "，这个开头是标点"),
        ])
        self.assertEqual(shaped4[0]["text"], "，这个开头是标点", "无语气词前导的标点不在剥离范围（拍板边界不变）")

    def test_leading_strip_scope_stays_e_only(self) -> None:
        shaped, _stats = shape_display_cues_with_stats([_cue(0, 2000, "欸这里有个坑")])
        self.assertEqual(
            shaped[0]["text"], "欸这里有个坑",
            "句首剥离拍板边界=仅呃/嗯，扩展词不参与句首剥离",
        )

    def test_edge_punctuation_classification(self) -> None:
        rows = [
            _cue(0, 400, "嗯。"), _cue(500, 900, "。。。"), _cue(1000, 2400, "正文内容"),
        ]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(stats["deleted_fillers"], 2, "纯标点壳与带尾标点语气词按纯语气删")
        self.assertEqual(len(shaped), 1)
        self.assertEqual(shaped[0]["text"], "正文内容")

    def test_answer_word_family_attaches_as_prefix(self) -> None:
        for word in ("好的", "是的", "对的", "是啊", "行啊", "好嘛", "行吧", "好呀", "好啦"):
            rows = [_cue(0, 400, word), _cue(800, 2400, "我们继续看下一题")]
            shaped, stats = shape_display_cues_with_stats(rows)
            self.assertEqual(len(shaped), 1, word)
            self.assertEqual(shaped[0]["text"], f"{word}我们继续看下一题", word)
            self.assertEqual(stats["answer_attached"], 1, word)

    def test_answer_prefix_drops_edge_punctuation(self) -> None:
        rows = [_cue(0, 400, "好，"), _cue(800, 2400, "我们继续看这道题")]
        shaped, _stats = shape_display_cues_with_stats(rows)
        self.assertEqual(shaped[0]["text"], "好我们继续看这道题", "并处零句中标点")
        self.assertNotIn("，", shaped[0]["text"])

    def test_answer_attach_skipped_over_long_gap(self) -> None:
        rows = [_cue(0, 400, "对"), _cue(5000, 7400, "所以这个式子成立")]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(stats["answer_attached"], 0, "间隔超 3s 不并入")
        self.assertEqual(len(shaped), 2, "应答词独立保留且不落入通用贴并（间隔更小上限）")
        self.assertEqual(shaped[0]["text"], "对")

    def test_answer_attach_respects_split_char_budget(self) -> None:
        long_text = "这一句话没有任何标点并且它的长度刚好达到了三十个字的上限值哦"[:30]
        self.assertEqual(len(long_text), 30)
        rows = [_cue(0, 400, "对"), _cue(800, 4000, long_text)]
        shaped, stats = shape_display_cues_with_stats(rows)
        self.assertEqual(stats["answer_attached"], 0, "并入后 31 字超拆分上限→不并入")
        self.assertEqual(len(shaped), 2)
        # 恰好 30 字的边界仍允许并入
        rows2 = [_cue(0, 400, "对"), _cue(800, 4000, long_text[:29])]
        shaped2, stats2 = shape_display_cues_with_stats(rows2)
        self.assertEqual(stats2["answer_attached"], 1, "并入后恰 30 字=预算内")
        self.assertEqual(len(shaped2), 1)
        self.assertEqual(len(shaped2[0]["text"]), 30)

    def test_soft_marker_split_fires_without_punctuation(self) -> None:
        # 真实语料零标点：31-50 字无句读边界的句在话轮标记词起点软拆（夜10-C）
        text = "我们先回顾上一节的内容然后进入今天的重点就是建立平衡方程的过程"
        self.assertGreater(len(text), SHAPE_SPLIT_MAX_CHARS)
        shaped, stats = shape_display_cues_with_stats([_cue(0, 9000, text)])
        self.assertEqual(stats["soft_split_units"], 1)
        self.assertEqual(stats["residual_overlong"], 0)
        self.assertGreaterEqual(len(shaped), 2)
        for row in shaped:
            self.assertGreaterEqual(len(row["text"]), 10, "软拆不产生新微碎片")
            self.assertLessEqual(len(row["text"]), SHAPE_SPLIT_MAX_CHARS)
        self.assertEqual("".join(row["text"] for row in shaped), text, "软拆零字符增删")
        self.assertEqual(shaped[0]["start_ms"], 0)
        self.assertEqual(shaped[-1]["end_ms"], 9000, "拆分子段首尾贴合源段")
        self.assertTrue(all(row["source_ids"] == [1] for row in shaped))

    def test_soft_split_refuses_and_keeps_whole_when_constraints_fail(self) -> None:
        # 标记词仅出现在句首（左块 1 字<10）且右侧 32 字越上限：块约束失败→整句保留
        text = "对然后" + "这句话剩余部分完全没有其他任何句读边界一直连续地说到结尾为止哦"
        self.assertGreater(len(text), SHAPE_SPLIT_MAX_CHARS)
        shaped, stats = shape_display_cues_with_stats([_cue(0, 9000, text)])
        self.assertEqual(stats["soft_split_units"], 0)
        self.assertEqual(stats["residual_overlong"], 1, "无合规软边界禁硬截断")
        self.assertEqual(len(shaped), 1)
        self.assertEqual(shaped[0]["text"], text)

    def test_punctuation_boundary_still_wins_over_soft_markers(self) -> None:
        # 有标点句读时走原路径（split_units），不落入软拆计数
        text = "第一句话比较短，" + "然后接一个稍微长一点的分句内容在中间" + "，再补一个尾巴收束"
        shaped, stats = shape_display_cues_with_stats([_cue(0, 6000, text)])
        self.assertEqual(stats["split_units"], 1)
        self.assertEqual(stats["soft_split_units"], 0)





    def test_overlong_with_punctuation_splits_at_clause_boundary(self) -> None:
        text = "第一句话比较短，" + "然后接一个稍微长一点的分句内容在中间" + "，再补一个尾巴收束"
        self.assertGreater(len(text), SHAPE_SPLIT_MAX_CHARS)
        shaped, stats = shape_display_cues_with_stats([_cue(0, 6000, text)])
        self.assertEqual(stats["split_units"], 1)
        self.assertEqual(stats["residual_overlong"], 0)
        self.assertTrue(all(len(row["text"]) <= SHAPE_SPLIT_MAX_CHARS for row in shaped))
        self.assertEqual(shaped[0]["start_ms"], 0)
        self.assertEqual(shaped[-1]["end_ms"], 6000, "拆分子段首尾贴合源段")

    def test_overlong_without_boundary_is_kept_whole_and_counted(self) -> None:
        text = "这句话完全没有标点但是它确实超过了三十个字的长度限制因为它一直在连续地说"[:40]
        shaped, stats = shape_display_cues_with_stats([_cue(0, 5000, text)])
        self.assertEqual(len(shaped), 1, "无安全边界禁硬截断")
        self.assertEqual(stats["residual_overlong"], 1)
        self.assertEqual(shaped[0]["text"], text)


class ShapePurityTests(unittest.TestCase):
    def test_input_rows_are_never_mutated_and_no_new_punctuation(self) -> None:
        import copy

        rows = [
            _cue(0, 800, "呃"), _cue(900, 2400, "第一部分内容"),
            _cue(2500, 4000, "第二部分内容"),
        ]
        frozen = copy.deepcopy(rows)
        shaped = shape_display_cues(rows)
        self.assertEqual(rows, frozen, "输入行零变异（纯函数）")
        before_punct = {ch for row in rows for ch in str(row["text"]) if ch in "。！？；，、"}
        after_punct = {ch for row in shaped for ch in str(row["text"]) if ch in "。！？；，、"}
        self.assertEqual(before_punct, after_punct, "零标点新增")

    def test_merged_row_never_claims_single_segment_evidence_identity(self) -> None:
        rows = [
            _cue(0, 800, "第一句", evidence_id="seg:aaaaaaaaaaaa"),
            _cue(900, 2400, "第二句", evidence_id="seg:bbbbbbbbbbbb"),
        ]
        shaped = shape_display_cues(rows)
        self.assertEqual(len(shaped), 1)
        self.assertNotIn("evidence_id", shaped[0], "合并行不冒领单段身份")
        self.assertEqual(
            shaped[0]["source_evidence_ids"], ["seg:aaaaaaaaaaaa", "seg:bbbbbbbbbbbb"],
        )

    def test_single_source_row_keeps_identity_and_fields(self) -> None:
        shaped = shape_display_cues([_cue(0, 2000, "独立的一句话", evidence_id="seg:cccccccccccc", lang="zh")])
        self.assertEqual(len(shaped), 1)
        self.assertEqual(shaped[0]["evidence_id"], "seg:cccccccccccc")
        self.assertEqual(shaped[0]["lang"], "zh")
        self.assertEqual(shaped[0]["source_ids"], [1])


class ShapeTimingTests(unittest.TestCase):
    def test_tail_never_crosses_next_start_nor_hangs_in_long_silence(self) -> None:
        shaped = shape_display_cues([
            _cue(0, 600, "短句一"), _cue(700, 1000, "短句二"), _cue(60000, 62000, "很久之后的第三句"),
        ])
        self.assertEqual(len(shaped), 2)
        merged = shaped[0]
        self.assertEqual(merged["source_ids"], [1, 2])
        # 拖尾+补时不越过 60s 后的下一条 start
        self.assertLessEqual(int(merged["end_ms"]), 60000)
        self.assertGreaterEqual(int(merged["end_ms"]) - int(merged["start_ms"]), 1200)

    def test_merged_unit_reaches_min_display_within_silence_bound(self) -> None:
        shaped = shape_display_cues([
            _cue(0, 300, "甲"), _cue(400, 700, "乙"), _cue(100000, 102000, "远处的下一句"),
        ])
        merged = shaped[0]
        self.assertEqual(merged["text"], "甲乙")
        display = int(merged["end_ms"]) - int(merged["start_ms"])
        self.assertGreaterEqual(display, 1200, "合并单元补到最小说明时长")
        self.assertLessEqual(int(merged["end_ms"]), 700 + 1500, "补时不超过 1.5s 静音上界")
        # 单源独立单元无拖尾：end 不变（纯变换最小化）
        self.assertEqual(int(shaped[1]["end_ms"]), 102000)

    def test_order_monotonic_and_indices_renumbered(self) -> None:
        rows = [
            _cue(5000, 6000, "后来"), _cue(0, 1000, "先说"), _cue(1500, 2500, "接着说"),
        ]
        shaped = shape_display_cues(rows)
        self.assertEqual([row["index"] for row in shaped], list(range(1, len(shaped) + 1)))
        for previous, current in zip(shaped, shaped[1:]):
            self.assertLessEqual(int(previous["start_ms"]), int(current["start_ms"]))


class ShapeCacheTests(unittest.TestCase):
    def test_cached_entry_reused_until_key_changes(self) -> None:
        rows = [_cue(0, 1000, "缓存钉一句话")]
        key = ("unit-test", 1, 2, 1)
        first = shape_display_cues_cached(key, rows)
        second = shape_display_cues_cached(key, rows)
        self.assertIs(first, second, "同指纹命中缓存（同一对象）")
        changed = shape_display_cues_cached(("unit-test", 1, 2, 99), rows)
        self.assertIsNot(first, changed)


class ShapeWiringPins(unittest.TestCase):
    """服务面接线钉：字幕文件路由（VTT）与阅读面板共用缓存整形入口。"""

    def test_vtt_serialization_roundtrip_matches_shaped_rows(self) -> None:
        from src.runtime.http_api import _vtt_bytes
        from src.runtime.subtitle_reader import parse_subtitle_text

        rows = shape_display_cues([
            _cue(0, 600, "短句一"),
            _cue(700, 1000, "短句二"),
            _cue(60000, 62000, "很久之后的第三句"),
        ])
        self.assertGreaterEqual(len(rows), 2)
        parsed = parse_subtitle_text(_vtt_bytes(rows).decode("utf-8"))
        self.assertEqual(len(parsed), len(rows), "整形行与 VTT 序列化逐条对应")
        for original, back in zip(rows, parsed):
            self.assertEqual(int(back["start_ms"]), int(original["start_ms"]))
            self.assertEqual(int(back["end_ms"]), int(original["end_ms"]))
            self.assertEqual(str(back["text"]), str(original["text"]))

    def test_http_route_and_panel_share_cached_shaper(self) -> None:
        http_source = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        app_source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertIn("shape_display_cues_cached", http_source)
        self.assertIn("shape_display_cues_cached", app_source)
        # 整形挂在面板缓存读取之后：证据层落库面（replace_transcript_segments）
        # 只见 split 行，绝不见整形行（裁定：不重写 runtime/data）
        seam = app_source.index("def subtitle_segments(")
        store_write = app_source.index("replace_transcript_segments", seam)
        shaper = app_source.index("shape_display_cues_cached(fingerprint, segments)", seam)
        self.assertLess(store_write, shaper, "落库面在整形之前")


if __name__ == "__main__":
    unittest.main()
