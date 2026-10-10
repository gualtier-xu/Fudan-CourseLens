"""evidence.v1 兼容缝：字幕身份/元数据经缓存、API、检索与引用的保留与回退。


覆盖：旧缓存行的确定性回退身份（位置无关、锚点/文本区分）、入站证据元数据
保留与危险值丢弃、远端优化产物优先导入、检索 evidence 身份引用、旧库幂等
升级不丢行。全部合成数据与包内临时路径。
"""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from path_utils import PROJECT_ROOT, relative_to_project
from src.application import CourseLensApplication
from src.runtime.learning_schema import initialize_learning_schema
from src.runtime.learning_store import LearningStore
from src.runtime.search_index import LearningSearchIndex
from src.runtime.subtitle_reader import (

    assign_fallback_evidence_ids,
    is_evidence_id,
)


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
SYNTHETIC_SEG_ID = "seg:0123456789ab"
SYNTHETIC_ALT_ID = "seg:ffffffffffff"


def _rows() -> list[dict]:
    return [
        {"start_ms": 0, "end_ms": 1_000, "text": "第一句"},
        {"start_ms": 1_000, "end_ms": 2_000, "text": "第二句"},
    ]


class FallbackIdentityTests(unittest.TestCase):
    def test_ids_are_contract_shaped_and_deterministic(self):
        first = assign_fallback_evidence_ids(_rows())
        second = assign_fallback_evidence_ids(copy.deepcopy(_rows()))
        self.assertEqual(
            [row["evidence_id"] for row in first],
            [row["evidence_id"] for row in second],
        )
        for row in first:
            self.assertTrue(is_evidence_id(row["evidence_id"]))
            self.assertTrue(row["evidence_id"].startswith("seg:"))

    def test_identity_is_position_independent(self):
        forward = assign_fallback_evidence_ids(_rows())
        backward = assign_fallback_evidence_ids(list(reversed(_rows())))
        self.assertEqual(
            {row["evidence_id"] for row in forward},
            {row["evidence_id"] for row in backward},
        )

    def test_anchor_and_text_changes_change_identity(self):
        # 同一身份域内：锚点不同、文本相同 → 不同身份
        anchored = assign_fallback_evidence_ids([
            {"start_ms": 0, "end_ms": 1_000, "text": "同样的话"},
            {"start_ms": 1_000, "end_ms": 2_000, "text": "同样的话"},
        ])
        self.assertNotEqual(anchored[0]["evidence_id"], anchored[1]["evidence_id"])
        # 同一身份域内：锚点相同、文本不同 → 不同身份
        worded = assign_fallback_evidence_ids([
            {"start_ms": 0, "end_ms": 1_000, "text": "同样的话"},
            {"start_ms": 1_000, "end_ms": 2_000, "text": "别的话"},
        ])
        self.assertNotEqual(anchored[0]["evidence_id"], worded[0]["evidence_id"])
        self.assertNotEqual(anchored[1]["evidence_id"], worded[1]["evidence_id"])

    def test_identified_rows_are_untouched(self):
        rows = [{"start_ms": 0, "end_ms": 1_000, "text": "第一句", "evidence_id": SYNTHETIC_SEG_ID}]
        result = assign_fallback_evidence_ids(copy.deepcopy(rows))
        self.assertEqual(result[0]["evidence_id"], SYNTHETIC_SEG_ID)


class StoreEvidenceSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)

    def _store(self, name: str) -> LearningStore:
        return LearningStore(Path(self.temporary.name) / name)

    def test_incoming_evidence_fields_survive_the_cache(self):
        store = self._store("evidence-in.db")
        store.replace_transcript_segments(
            "lecture",
            source_path="synthetic.vtt",
            source_mtime_ns=1,
            source_size=1,
            segments=[{
                "start_ms": 0, "end_ms": 1_000, "text": "第一句",
                "evidence_id": SYNTHETIC_SEG_ID,
                "source_hash": "b" * 64,
                "provenance": {"producer": "synthetic-worker", "model": "synthetic-asr"},
                "support": [{"kind": "segment", "id": SYNTHETIC_ALT_ID}],
                "correction": {"state": "timing_preserved"},
                "password": "hunter2",
                "api_key": "sk-" + "A" * 24,
                "note": "Bearer abcdefghijklmnopqrstuvwxyz",
            }],
        )
        rows = store.get_transcript_segments("lecture")
        self.assertEqual(rows[0]["evidence_id"], SYNTHETIC_SEG_ID)
        self.assertEqual(rows[0]["source_hash"], "b" * 64)
        self.assertEqual(rows[0]["provenance"], {"producer": "synthetic-worker", "model": "synthetic-asr"})
        self.assertEqual(rows[0]["support"], [{"kind": "segment", "id": SYNTHETIC_ALT_ID}])
        self.assertEqual(rows[0]["correction"], {"state": "timing_preserved"})
        for dropped in ("password", "api_key", "note"):
            self.assertNotIn(dropped, rows[0])

    def test_malformed_identity_falls_back_and_segment_id_alias_promotes(self):
        store = self._store("evidence-fallback.db")
        store.replace_transcript_segments(
            "lecture",
            source_path="synthetic.vtt",
            source_mtime_ns=1,
            source_size=1,
            segments=[
                {"start_ms": 0, "end_ms": 1_000, "text": "坏身份", "evidence_id": "seg:NOPE"},
                {"start_ms": 1_000, "end_ms": 2_000, "text": "别名", "segment_id": SYNTHETIC_ALT_ID},
            ],
        )
        rows = store.get_transcript_segments("lecture")
        self.assertNotEqual(rows[0]["evidence_id"], "seg:NOPE")
        self.assertTrue(is_evidence_id(rows[0]["evidence_id"]))
        self.assertEqual(rows[1]["evidence_id"], SYNTHETIC_ALT_ID)

    def test_legacy_rows_get_stable_ids_across_reads_and_stores(self):
        first = self._store("legacy-a.db")
        second = self._store("legacy-b.db")
        for store in (first, second):
            store.replace_transcript_segments(
                "lecture",
                source_path="synthetic.vtt",
                source_mtime_ns=1,
                source_size=1,
                segments=_rows(),
            )
        one = first.get_transcript_segments("lecture")
        again = first.get_transcript_segments("lecture")
        other = second.get_transcript_segments("lecture")
        self.assertEqual(one, again)
        self.assertEqual(one, other)
        for row in one:
            self.assertTrue(is_evidence_id(row["evidence_id"]))

    def test_old_schema_upgrades_in_place_without_losing_rows(self):
        path = Path(self.temporary.name) / "old-schema.db"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE transcript_sources (
                sub_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                source_mtime_ns INTEGER NOT NULL, source_size INTEGER NOT NULL,
                segment_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            );
            CREATE TABLE transcript_segments (
                sub_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY(sub_id, segment_index)
            );
            """
        )
        db.execute(
            "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,updated_at)"
            " VALUES('legacy','synthetic.vtt',1,1,0)"
        )
        db.executemany(
            "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text) VALUES(?,?,?,?,?)",
            [("legacy", 1, 0, 1_000, "第一句"), ("legacy", 2, 1_000, 2_000, "第二句")],
        )
        db.commit()
        db.close()

        store = LearningStore(path)
        with closing(sqlite3.connect(path)) as probe:
            columns = {row[1] for row in probe.execute("PRAGMA table_info(transcript_segments)")}
        self.assertIn("evidence_json", columns)
        rows = store.get_transcript_segments("legacy")
        self.assertEqual([row["text"] for row in rows], ["第一句", "第二句"])
        self.assertEqual([row["start_ms"] for row in rows], [0, 1_000])
        self.assertTrue(all(is_evidence_id(row["evidence_id"]) for row in rows))
        # 幂等：重复初始化不再改动
        with store._connect() as reopened:
            self.assertTrue(initialize_learning_schema(reopened))
        self.assertEqual(store.get_transcript_segments("legacy"), rows)


class SubtitleSegmentsSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        # addCleanup 后进先出：先关应用释放数据库，再清理临时目录
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.app.close)
        self.app.catalog_repository.upsert_course("course", "Course")

    def _upsert_lecture(self, **fields) -> None:
        self.app.catalog_repository.upsert_lecture(
            "course",
            {"sub_id": "lecture", "sub_title": "Lecture", "has_playback": True, **fields},
        )

    def test_warm_unsplit_legacy_rows_split_into_identified_children(self):
        transcript = Path(self.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n"
            f"00:00:00.000 --> 00:00:30.000\n{LONG_TEXT}\n\n",
            encoding="utf-8",
        )
        self._upsert_lecture(vtt_path=str(transcript))
        # 模拟旧版本写入的未拆分缓存行：源元数据与文件匹配，行内容是整段
        path = self.app.subtitle_file_path("lecture")
        stat = path.stat()
        self.app.learning_store.replace_transcript_segments(
            "lecture",
            source_path=relative_to_project(path),
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
            segments=[{"start_ms": 0, "end_ms": 30_000, "text": LONG_TEXT}],
        )
        first = self.app.subtitle_segments("lecture")["segments"]
        self.assertGreater(len(first), 1)
        ids = [item["evidence_id"] for item in first]
        self.assertEqual(len(set(ids)), len(ids), "同一父段拆出的子 cue 不得共享一个 ID")
        parents = {item["parent_evidence_id"] for item in first}
        self.assertEqual(len(parents), 1)
        parent = parents.pop()
        self.assertTrue(is_evidence_id(parent))
        self.assertNotIn(parent, ids)
        second = self.app.subtitle_segments("lecture")["segments"]
        self.assertEqual(first, second)

    def test_fresh_cache_miss_rows_get_distinct_stable_fallback_ids(self):
        transcript = Path(self.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\n第一句\n\n"
            "00:00:01.000 --> 00:00:02.000\n第二句\n\n",
            encoding="utf-8",
        )
        self._upsert_lecture(vtt_path=str(transcript))
        # AS13：fallback 身份钉在证据层（存储行）验证——落库面只见 split 行
        # （首次面板调用触发 cache-miss 写入，随后读取存储行核对身份）
        self.app.subtitle_segments("lecture")
        stored = self.app.learning_store.get_transcript_segments("lecture")
        self.assertEqual([item["text"] for item in stored], ["第一句", "第二句"])
        ids = [item["evidence_id"] for item in stored]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(is_evidence_id(item) and item.startswith("seg:") for item in ids))
        # 展示面（整形后）近邻短句合并为一条，并携源段锚（两行证据 ID）
        shaped = self.app.subtitle_segments("lecture")["segments"]
        self.assertEqual([item["text"] for item in shaped], ["第一句第二句"])
        self.assertEqual(sorted(shaped[0]["source_evidence_ids"]), sorted(ids))
        self.assertEqual(
            shaped,
            self.app.subtitle_segments("lecture")["segments"],
        )

    def test_remote_optimized_json_is_preferred_and_keeps_evidence(self):
        transcript = Path(self.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n来自VTT文本\n\n",
            encoding="utf-8",
        )
        optimized = Path(self.temporary.name) / "lecture.remote.json"
        optimized.write_text(json.dumps({
            "segments": [
                {
                    "start_ms": 0, "end_ms": 1_000, "text": "来自远端的第一句",
                    "segment_id": SYNTHETIC_SEG_ID,
                    "source_hash": "c" * 64,
                    "provenance": {"producer": "synthetic-worker"},
                },
                {"start_ms": 1_000, "end_ms": 31_000, "text": LONG_TEXT, "segment_id": SYNTHETIC_ALT_ID},
            ],
            "metrics": {},
        }, ensure_ascii=False), encoding="utf-8")
        self._upsert_lecture(vtt_path=str(transcript), optimized_json_path=str(optimized))

        segments = self.app.subtitle_segments("lecture")["segments"]
        self.assertEqual(segments[0]["text"], "来自远端的第一句")
        self.assertEqual(segments[0]["evidence_id"], SYNTHETIC_SEG_ID)
        self.assertEqual(segments[0]["source_hash"], "c" * 64)
        self.assertEqual(segments[0]["provenance"], {"producer": "synthetic-worker"})
        split_children = [item for item in segments if item["start_ms"] >= 1_000]
        self.assertGreater(len(split_children), 1)
        self.assertTrue(all(is_evidence_id(item["evidence_id"]) for item in split_children))
        self.assertEqual(
            {item["parent_evidence_id"] for item in split_children},
            {SYNTHETIC_ALT_ID},
        )
        # 元数据已进入本地缓存行
        cached = self.app.learning_store.get_transcript_segments("lecture")
        self.assertEqual(cached[0]["evidence_id"], SYNTHETIC_SEG_ID)

    def test_corrupt_remote_json_falls_back_to_subtitle_file(self):
        transcript = Path(self.temporary.name) / "lecture.vtt"
        transcript.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n来自VTT文本\n\n",
            encoding="utf-8",
        )
        optimized = Path(self.temporary.name) / "lecture.remote.json"
        optimized.write_text("{not json", encoding="utf-8")
        self._upsert_lecture(vtt_path=str(transcript), optimized_json_path=str(optimized))
        segments = self.app.subtitle_segments("lecture")["segments"]
        self.assertEqual([item["text"] for item in segments], ["来自VTT文本"])


class SearchEvidenceSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.store = LearningStore(Path(self.temporary.name) / "learning.db")
        self.store.replace_transcript_segments(
            "s1",
            source_path="synthetic.vtt",
            source_mtime_ns=1,
            source_size=1,
            segments=[
                {"start_ms": 0, "end_ms": 1_000, "text": "线性代数特征值"},
                {"start_ms": 1_000, "end_ms": 2_000, "text": "第二段内容"},
            ],
        )
        self.rows = self.store.get_transcript_segments("s1")
        self.index = LearningSearchIndex(
            self.store,
            lambda: {"authorization_state": "ready", "courses": {}, "lectures": {}},
            lambda sub_id: {},
            None,
        )

    def test_transcript_documents_use_evidence_identity_as_source_ref(self):
        documents = self.index._documents_for(
            "s1",
            {"course_title": "课程", "lecture_title": "讲次", "teacher": ""},
            "v1",
        )
        transcript = [document for document in documents if document.source == "transcript"]
        self.assertEqual(
            [document.source_ref for document in transcript],
            [row["evidence_id"] for row in self.rows],
        )

    def test_search_results_expose_the_evidence_identity(self):
        evidence_id = self.rows[0]["evidence_id"]
        self.store.sync_search_catalog([{
            "sub_id": "s1", "course_id": "c1", "course_title": "课程",
            "lecture_title": "讲次", "teacher": "", "catalog_version": "v",
        }])
        self.store.replace_search_documents(
            "s1",
            [{
                "doc_key": f"transcript:s1:{evidence_id}",
                "source": "transcript",
                "source_ref": evidence_id,
                "document_title": "同步字幕",
                "start_ms": 0,
                "display_text": "线性代数特征值",
                "search_text": "线性代数特征值",
                "source_version": "v",
            }],
            indexed_version="v",
        )
        result = self.index.search("特征值")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["evidence_id"], evidence_id)
        self.assertEqual(result["results"][0]["result_id"], f"transcript:s1:{evidence_id}")


class TranscriptSegmentStoreImmunityTests(unittest.TestCase):
    """夜10-C T19：证据层写入面免疫——非列表契约闭集化+畸形行跳行。"""

    def _store(self, directory: str) -> LearningStore:
        return LearningStore(Path(directory) / "learning.db")

    def test_non_list_payload_raises_closed_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            for bad in (None, "string", 42, {"segments": []}):
                with self.assertRaises(ValueError) as caught:
                    store.replace_transcript_segments(
                        "s1", source_path="p", source_mtime_ns=1, source_size=1,
                        segments=bad,
                    )
                self.assertIn("invalid", str(caught.exception))
            store.close()

    def test_malformed_rows_are_skipped_and_valid_rows_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.replace_transcript_segments(
                "s1", source_path="p", source_mtime_ns=1, source_size=1,
                segments=[
                    {"start_ms": "bad", "end_ms": 100, "text": "坏时间戳"},
                    None,
                    "not-a-dict",
                    {"start_ms": 0, "end_ms": 1000, "text": "好段"},
                ],
            )
            rows = store.get_transcript_segments("s1")
            self.assertEqual([row["text"] for row in rows], ["好段"])
            store.close()


class StoreFallbackReadChainTests(unittest.TestCase):
    """夜10-C N10B-4 方案②：catalog 行缺 vtt/srt 路径时的读链自愈。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.app.close)
        self.app.catalog_repository.upsert_course("course", "Course")
        self.app.catalog_repository.upsert_lecture(
            "course",
            {"sub_id": "lecture", "sub_title": "Lecture", "has_playback": True},
        )

    def test_missing_catalog_paths_fall_back_to_local_store_segments(self) -> None:
        # 遗留态：源登记+缓存段在，catalog 行无 vtt_path/srt_path（B 车道
        # 实锤的孤儿形态）→ 读链自愈，存量讲次即时受益。
        self.app.learning_store.replace_transcript_segments(
            "lecture",
            source_path="artifacts/subtitles/legacy/subtitle.vtt",
            source_mtime_ns=123,
            source_size=456,
            segments=[{"start_ms": 0, "end_ms": 1_000, "text": "自愈段"}],
        )
        value = self.app.subtitle_segments("lecture")
        self.assertEqual([row["text"] for row in value["segments"]], ["自愈段"])
        self.assertEqual(value["count"], 1)
        self.assertEqual(value["source"], "artifacts/subtitles/legacy/subtitle.vtt")
        self.assertEqual(value["source_state"], "store_fallback")
        # 第七波③：轨文件自愈——vtt 实体重建+catalog vtt_path 回写
        row = self.app.catalog_repository.get_lecture("lecture")
        vtt_path = str(row.get("vtt_path") or "")
        self.assertTrue(vtt_path.startswith("artifacts/subtitles/"), vtt_path)
        rebuilt = Path(str(self.app.output_dir)) / vtt_path
        self.assertTrue(rebuilt.is_file())
        self.assertIn("WEBVTT", rebuilt.read_text(encoding="utf-8"))
        self.assertIn("自愈段", rebuilt.read_text(encoding="utf-8"))

    def test_without_store_registration_response_stays_honestly_empty(self) -> None:
        value = self.app.subtitle_segments("lecture")
        self.assertEqual(value["segments"], [])
        self.assertEqual(value["source"], "")
        self.assertNotIn("source_state", value)

    def test_registration_without_segments_stays_honestly_empty(self) -> None:
        self.app.learning_store.replace_transcript_segments(
            "lecture",
            source_path="some/path.vtt",
            source_mtime_ns=1,
            source_size=1,
            segments=[{"start_ms": "bad", "end_ms": 1, "text": "全坏行"}],
        )
        value = self.app.subtitle_segments("lecture")
        self.assertEqual(value["segments"], [])
        self.assertNotIn("source_state", value)


if __name__ == "__main__":
    unittest.main()
