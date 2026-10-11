"""长字幕 cue 拆分（split_long_cues）的单元与应用/路由级行为测试。"""


from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from path_utils import PROJECT_ROOT, relative_to_project
from src.application import CourseLensApplication
from src.runtime.http_api import _vtt_bytes, make_handler
from src.runtime.subtitle_reader import is_evidence_id, parse_subtitle_text, split_long_cues
from tests.http_services import http_services

def _cycle_text(count: int) -> str:
    """夜10-C 第九波：非重复且无话轮标记词的长文本夹具（块级折叠+软拆分
    双语义下，周期环与标记词段落都会改变行为；改用自然语段按需截取）。"""
    paragraph = (
        "这一节我们讲组合逻辑的设计方法从真值表出发写出逻辑表达式用卡诺图化简"
        "画出门级电路图整个过程需要反复练习才能掌握数字系统里时序与组合的差异"
        "是本课程的重点请课后把例题重新推一遍下一章进入时序电路的状态转移分析"
        "记得提前预习第七章前三小节"
    )
    return paragraph[:max(1, int(count))]










LONG_TEXT = _cycle_text(100)
CLAUSED_TEXT = _cycle_text(20) + "。" + _cycle_text(20) + "。" + _cycle_text(20)


def cue(start_ms: int, end_ms: int, text: str) -> dict:
    return {"index": 1, "start_ms": start_ms, "end_ms": end_ms, "text": text}


class SplitLongCuesUnitTests(unittest.TestCase):
    def test_short_cues_pass_through_unchanged(self):
        segments = [cue(0, 1500, "短句。"), cue(1500, 3000, _cycle_text(44))]
        result = split_long_cues(segments)
        self.assertEqual(len(result), 2)
        for original, kept in zip(segments, result):
            self.assertIs(original, kept)

    def test_splits_after_chinese_punctuation(self):
        result = split_long_cues([cue(0, 10_000, CLAUSED_TEXT)])
        # 子句 21/21/20 → 贪心聚合为 [42, 20] 两个 cue
        self.assertEqual([len(item["text"]) for item in result], [42, 20])
        self.assertTrue(result[0]["text"].endswith("。"))
        self.assertEqual("".join(item["text"] for item in result), CLAUSED_TEXT)
        self.assertEqual(result[0]["start_ms"], 0)
        self.assertEqual(result[-1]["end_ms"], 10_000)

    def test_hard_splits_run_on_text_without_punctuation(self):
        result = split_long_cues([cue(0, 10_000, LONG_TEXT)])
        self.assertEqual([len(item["text"]) for item in result], [44, 44, 12])
        self.assertEqual("".join(item["text"] for item in result), LONG_TEXT)

    def test_proportional_timing_enforces_minimum_duration(self):
        # 权重 44:2 → 957ms/43ms；43ms 不足 200ms，从最长兄弟借 157ms
        text = _cycle_text(43) + "，尾。"
        result = split_long_cues([cue(0, 1_000, text)], min_duration_ms=200)
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result], [(0, 800), (800, 1_000)])
        self.assertTrue(all(item["end_ms"] - item["start_ms"] >= 200 for item in result))

    def test_children_are_monotonic_renumbered_and_exact(self):
        segments = [cue(0, 30_000, LONG_TEXT), cue(30_000, 31_000, "短句。"), cue(31_000, 61_000, CLAUSED_TEXT)]
        result = split_long_cues(segments)
        self.assertEqual([item["index"] for item in result], list(range(1, len(result) + 1)))
        self.assertEqual(result[0]["start_ms"], 0)
        for previous, current in zip(result, result[1:]):
            self.assertEqual(current["start_ms"], previous["end_ms"])
            self.assertGreater(current["end_ms"], current["start_ms"])
        self.assertEqual(
            [item["text"] for item in result if item["start_ms"] >= 30_000 and item["start_ms"] < 31_000],
            ["短句。"],
        )
        self.assertEqual(result[-1]["end_ms"], 61_000)

    def test_children_of_identified_parent_get_distinct_cue_ids(self):
        parent = dict(cue(0, 10_000, CLAUSED_TEXT), evidence_id="seg:0123456789ab", source_hash="a" * 64)
        result = split_long_cues([dict(parent)])
        self.assertGreater(len(result), 1)
        ids = [item["evidence_id"] for item in result]
        self.assertEqual(len(set(ids)), len(ids), "子 cue 身份必须互不相同")
        self.assertNotIn("seg:0123456789ab", ids, "父段 ID 不得被复制到子 cue 上")
        self.assertTrue(all(is_evidence_id(item["evidence_id"]) for item in result))
        self.assertTrue(all(item["evidence_id"].startswith("cue:") for item in result))
        self.assertEqual({item["parent_evidence_id"] for item in result}, {"seg:0123456789ab"})
        self.assertTrue(all(item["source_hash"] == "a" * 64 for item in result))
        again = split_long_cues([dict(cue(0, 10_000, CLAUSED_TEXT), evidence_id="seg:0123456789ab")])
        self.assertEqual([item["evidence_id"] for item in again], ids, "子 cue 身份必须可复现")

    def test_children_of_unidentified_parent_do_not_invent_ids(self):
        result = split_long_cues([cue(0, 10_000, CLAUSED_TEXT)])
        self.assertTrue(all("evidence_id" not in item for item in result))
        self.assertTrue(all("parent_evidence_id" not in item for item in result))

    def test_identified_short_cue_passes_through_with_own_identity(self):
        parent = dict(cue(0, 1_500, "短句。"), evidence_id="seg:0123456789ab")
        result = split_long_cues([parent])
        self.assertIs(result[0], parent)
        self.assertEqual(result[0]["evidence_id"], "seg:0123456789ab")


class SubtitleSplitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        transcript = Path(self.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n"
            f"00:00:00.000 --> 00:00:30.000\n{LONG_TEXT}\n\n",
            encoding="utf-8",
        )
        self.app.catalog_repository.upsert_course("course", "Course")
        self.app.catalog_repository.upsert_lecture(
            "course",
            {
                "sub_id": "lecture",
                "sub_title": "Lecture",
                "has_playback": True,
                "vtt_path": str(transcript),
            },
        )

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_panel_segments_share_split_units_with_cache(self):
        first = self.app.subtitle_segments("lecture")
        self.assertGreater(len(first["segments"]), 1)
        self.assertTrue(all(len(str(item["text"])) <= 44 for item in first["segments"]))
        second = self.app.subtitle_segments("lecture")
        self.assertEqual(first["segments"], second["segments"])

    def test_legacy_raw_cache_rows_are_split_on_read(self):
        # 模拟拆分功能上线前的旧缓存：source 元数据匹配但行内容是未拆分的整段
        path = self.app.subtitle_file_path("lecture")
        stat = path.stat()
        self.app.learning_store.replace_transcript_segments(
            "lecture",
            source_path=relative_to_project(path),
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
            segments=[{"index": 1, "start_ms": 0, "end_ms": 30_000, "text": LONG_TEXT}],
        )
        result = self.app.subtitle_segments("lecture")
        segments = result["segments"]
        self.assertGreater(len(segments), 1, "旧缓存整段行必须拆分后返回")
        self.assertTrue(all(len(str(item["text"])) <= 44 for item in segments))
        self.assertEqual(result["count"], len(segments))
        # 与 VTT 路由（parse → split）的 cue 单元一致
        expected = split_long_cues(parse_subtitle_text(
            f"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n{LONG_TEXT}\n"
        ))
        self.assertEqual(
            [(item["start_ms"], item["end_ms"], item["text"]) for item in segments],
            [(item["start_ms"], item["end_ms"], item["text"]) for item in expected],
        )


class _RouteService:
    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}

    subtitle_file_path = None  # 由 setUp 按临时文件注入


class SubtitleFileRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache")
        transcript = Path(cls.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n"
            f"00:00:00.000 --> 00:00:30.000\n{LONG_TEXT}\n\n",
            encoding="utf-8",
        )
        service = _RouteService()
        service.subtitle_file_path = lambda sub_id: transcript if sub_id == "lecture" else None
        frontend = PROJECT_ROOT / "frontend"
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(service), frontend)
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def test_file_route_serves_split_vtt_units(self):
        with urlopen(f"{self.base}/api/v3/subtitles/file?sub_id=lecture") as response:
            self.assertEqual(response.headers["Content-Type"], "text/vtt; charset=utf-8")
            body = response.read().decode("utf-8")
        self.assertTrue(body.startswith("WEBVTT"))
        segments = parse_subtitle_text(body)
        self.assertGreater(len(segments), 1)
        self.assertEqual("".join(item["text"] for item in segments), LONG_TEXT)
        # 路由 cue 单元与面板一致（同一 parse → split 管线）
        expected = split_long_cues(parse_subtitle_text(
            f"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n{LONG_TEXT}\n"
        ))
        self.assertEqual(
            [(item["start_ms"], item["end_ms"], item["text"]) for item in segments],
            [(item["start_ms"], item["end_ms"], item["text"]) for item in expected],
        )

    def test_missing_subtitle_still_returns_404(self):
        try:
            urlopen(f"{self.base}/api/v3/subtitles/file?sub_id=missing")
        except HTTPError as exc:
            self.assertEqual(exc.code, 404)
        else:
            self.fail("expected 404 for missing subtitle")


class VttSerializerTests(unittest.TestCase):
    def test_vtt_bytes_round_trips_through_parser(self):
        segments = [
            {"index": 1, "start_ms": 0, "end_ms": 3_500, "text": "第一句。"},
            {"index": 2, "start_ms": 3_500, "end_ms": 3_721_000, "text": "第二句。"},
        ]
        parsed = parse_subtitle_text(_vtt_bytes(segments).decode("utf-8"))
        self.assertEqual(
            [(item["start_ms"], item["end_ms"], item["text"]) for item in parsed],
            [(0, 3_500, "第一句。"), (3_500, 3_721_000, "第二句。")],
        )


if __name__ == "__main__":
    unittest.main()
