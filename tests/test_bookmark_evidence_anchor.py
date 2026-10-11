"""FINALWRAP-C4 回归钉：书签解释证据窗的锚点段优先。

实测缺陷（2026-09-30 真机）：30s 回看窗在密段讲次里先填满 8 帽，
按时间序从旧端截断后，被「没听懂」的锚段自身落在证据之外，
解释链恒 needs_context/bookmark_evidence_insufficient。
钉三件事：
① 与锚点相交的段必须入选（即使回看窗里还有更旧的候选）；
② 帽仍为 8，超出部分按离锚距离就近淘汰（最近邻优先）；
③ 出口 evidence_packet 合同不变形（citation_id=bookmark:{id}:transcript:{index}，
   index=全量段序；text/label/source 键与现网一致）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.application import CourseLensApplication


def _segment(index: int, start_ms: int, end_ms: int, text: str) -> dict:
    return {"index": index, "start_ms": start_ms, "end_ms": end_ms, "text": text}


class _EvidenceApplication:
    """窄适配器：真实 _bookmark_evidence + 桩段源，不装配整个应用。"""

    _bookmark_evidence = CourseLensApplication._bookmark_evidence

    def __init__(self, root: Path, segments: list[dict]):
        self._segments = segments
        self.learning_store = SimpleNamespace(
            path=root / "learning.db",
            find_ai_artifact=lambda *args, **kwargs: None,
        )

    def subtitle_segments(self, sub_id: str) -> dict:
        return {"segments": self._segments}


class BookmarkEvidenceAnchorTests(unittest.TestCase):
    def test_anchor_segment_survives_dense_lookback_window(self):
        """密段回看窗：锚段（时间序第 9 个匹配）必须挤进 8 帽。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 8 段 4s 短段填满锚前 30s 回看窗，锚段 225 恰是提问所指。
        segments = [
            _segment(i, 900_000 + i * 4_000, 900_000 + i * 4_000 + 3_800, f"锚前铺垫第{i}句")
            for i in range(206, 214)
        ]
        segments += [
            _segment(222, 986_072, 990_745, "要去思维这个世界背后怎么工作呢？"),
            _segment(223, 990_258, 996_674, "因为我们不可能把这个世界当做原本的方式去记录下来"),
            _segment(224, 996_674, 997_743, "然后完全。"),
            _segment(225, 993_233, 994_768, "进行一些抽象的概念的提。"),
            _segment(226, 997_743, 1_002_000, "锚后收尾一句"),
        ]
        app = _EvidenceApplication(Path(tmp.name), segments)
        evidence = app._bookmark_evidence("27738", "452298", 993_233, 993_233)
        self.assertLessEqual(len(evidence), 8)
        # citation 序号=全量段列表的 enumerate 位置（0 基，旧行为同款）：
        # 段 222-226 位于位置 8-12，锚段 225=位置 11、跨锚长段 223=位置 9。
        citations = {item["citation_id"] for item in evidence}
        texts = {item["text"] for item in evidence}
        self.assertIn("bookmark:pending:transcript:11", citations, "锚段必须入选证据")
        self.assertIn("bookmark:pending:transcript:9", citations, "跨锚长段同样相交锚窗，应入选")
        self.assertIn("进行一些抽象的概念的提。", texts)
        # 8 帽下最旧的纯回看段被就近淘汰。
        self.assertNotIn("bookmark:pending:transcript:0", citations)

    def test_sparse_window_keeps_all_candidates(self):
        """稀疏窗：候选不足 8 时全部入选，锚段在其中（旧行为不劣化）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        segments = [
            _segment(10, 900_000, 905_000, "前文一句"),
            _segment(11, 990_000, 995_000, "锚段正文"),
            _segment(12, 1_010_000, 1_015_000, "后文一句"),
        ]
        app = _EvidenceApplication(Path(tmp.name), segments)
        evidence = app._bookmark_evidence("27738", "452298", 993_233, 993_233)
        citations = [item["citation_id"] for item in evidence]
        # 段 10（900s）在锚前 88s，本就在 30s 回看窗之外——只收窗内 2 段。
        self.assertEqual(len(citations), 2)
        self.assertIn("bookmark:pending:transcript:1", citations)

    def test_packet_contract_is_unchanged(self):
        """证据身份合同不变形：citation 消费方（LLM 引用/前端跳转）零感知。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        segments = [_segment(7, 990_000, 995_000, "锚段")]
        app = _EvidenceApplication(Path(tmp.name), segments)
        evidence = app._bookmark_evidence("27738", "452298", 993_233, 993_233, bookmark_id="abc123")
        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(item["citation_id"], "bookmark:abc123:transcript:0")
        self.assertEqual(item["text"], "锚段")
        self.assertEqual(item["source"], "transcript")
        self.assertEqual(item["label"], "同步字幕")
        self.assertEqual(item["text"], "锚段")
        self.assertEqual(item["start_ms"], 990_000)
        self.assertIn("source_hash", item)


if __name__ == "__main__":
    unittest.main()
