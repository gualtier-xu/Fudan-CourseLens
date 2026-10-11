"""VTT-PERF · 展示层 cue 落盘缓存钉（subtitle_segments 读链优化层）。

钉七件事：
1. 首读计算并落盘：display-cache 文件出现在 output_dir 内，响应形状与
   旧链路逐字段一致（sub_id/segments/source/count）。
2. 二读走缓存：源指纹（path/mtime/size）与整形口径未变时，store 读链与
   解析链整链跳过（store 桩抛错也照常供段），内容逐行相等。
3. 源变化失效：VTT 内容或 mtime/size 变化 → 重算并换新缓存文件，旧文件
   被收敛删除（增量失效=指纹换名，不读旧值）。
4. 缓存损坏回落：缓存文件损坏/口径 tag 不符一律按 miss 处理，重算出与
   首读完全一致的行（缓存只是纯优化层，绝不影响正确性）。
5. store 登记缺失自愈保持：display cache 命中也不跳过 store 登记核对——
   登记缺失时走原链路重存（搜索索引/证据链依赖 store，语义零回退）。
6. storage schema 零变化：缓存只是 output_dir 下新增派生文件，learning
   store 表结构与行内容不受影响。
7. 折叠函数提速等价：memo+必要条件快径与旧算法在边角+随机语料上逐案
   相等（零行为漂移），memo 命中/未命中结果一致。
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.application import CourseLensApplication
from src.runtime.learning_store import LearningStore
from src.runtime.subtitle_reader import (
    _COLLAPSE_MEMO,
    _collapse_repeated_tokens,
    display_cache_path,
    display_cache_tag,
)

VTT_TEXT = "\n".join([
    "WEBVTT",
    "",
    "00:00:00.000 --> 00:00:04.000",
    "这一节我们讨论动态规划的核心思路，先从定义出发再逐步推进。",
    "",
    "00:00:04.000 --> 00:00:08.000",
    "注意状态转移方程在考试中的常见变形，课本例题之后有两道随堂练习。",
    "",
    "00:00:08.000 --> 00:00:12.000",
    "这个这个例子说明边界条件的处理，请大家先暂停视频尝试自己推导。",
    "",
])


class _SegmentsApplication:
    """窄适配器：真实 subtitle_segments + 真实 learning store + 桩目录行。"""

    subtitle_segments = CourseLensApplication.subtitle_segments
    _remote_subtitle_segments = CourseLensApplication._remote_subtitle_segments
    subtitle_file_path = CourseLensApplication.subtitle_file_path

    def __init__(self, root: Path, vtt: Path):
        self.output_dir = root
        self.learning_store = LearningStore(root / "learning.db")
        self.catalog_repository = Mock()
        self.catalog_repository.get_lecture.return_value = {"vtt_path": str(vtt)}


class DisplayCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vtt = self.root / "lecture.vtt"
        self.vtt.write_text(VTT_TEXT, encoding="utf-8")
        self.app = _SegmentsApplication(self.root, self.vtt)
        self.cache_dir = self.root / "artifacts" / "subtitles" / "display-cache"
        stat = self.vtt.stat()
        self.cache_path = display_cache_path(
            self.cache_dir, "sub1",
            source_path=os.path.abspath(str(self.vtt)),
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
        )

    def _read(self):
        return self.app.subtitle_segments("sub1")

    def test_first_read_computes_and_writes_cache(self):
        result = self._read()
        self.assertTrue(self.cache_path.is_file(), "首读必须落盘展示缓存")
        self.assertEqual(result["sub_id"], "sub1")
        self.assertEqual(result["count"], len(result["segments"]))
        self.assertTrue(result["segments"], "合法 VTT 必须解析出段")
        # 产品合同=落盘的是「解析后的绝对路径」（darwin 上 /var 是
        # /private/var 的符号链接，realpath 会展开；Windows 上
        # realpath==abspath，期望值零漂移）。
        self.assertEqual(result["source"], os.path.realpath(str(self.vtt)))
        # 缓存文件体内嵌源指纹与口径 tag（升级失效语义）
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["cache_format"], 1)
        self.assertEqual(payload["tag"], display_cache_tag())
        self.assertEqual(payload["key"][0], "sub1")

    def test_second_read_serves_cache_without_store_chain(self):
        first = self._read()
        # store 读链与解析链整链失效也必须照常供段（证明缓存命中）
        self.app.learning_store.get_transcript_segments = Mock(
            side_effect=AssertionError("cache hit must not read store"))
        second = self._read()
        self.assertEqual(
            [dict(row) for row in second["segments"]],
            [dict(row) for row in first["segments"]],
        )
        self.assertEqual(second["count"], first["count"])

    def test_source_change_invalidates_and_prunes_old_file(self):
        self._read()
        old_path = self.cache_path
        self.assertTrue(old_path.is_file())
        grown = VTT_TEXT + "00:00:12.000 --> 00:00:16.000\n新增的收尾一句。\n\n"
        self.vtt.write_text(grown, encoding="utf-8")
        # Windows mtime 粒度粗（立即重写可能同 ns）：显式推进源指纹，模拟真实源更新
        bumped = (self.vtt.stat().st_mtime_ns + 1_000_000_000)
        os.utime(self.vtt, ns=(bumped, bumped))
        stat = self.vtt.stat()
        self.cache_path = display_cache_path(
            self.cache_dir, "sub1",
            source_path=os.path.abspath(str(self.vtt)),
            source_mtime_ns=stat.st_mtime_ns,
            source_size=stat.st_size,
        )
        result = self._read()
        texts = [row["text"] for row in result["segments"]]
        self.assertTrue(any("新增的收尾一句" in text for text in texts), "源变化必须重算")
        self.assertNotEqual(self.cache_path, old_path, "指纹变化=换新文件")
        self.assertTrue(self.cache_path.is_file())
        self.assertFalse(old_path.exists(), "旧指纹缓存必须被收敛删除")

    def test_corrupted_cache_falls_back_to_recompute(self):
        first = self._read()
        self.cache_path.write_text("{corrupted json", encoding="utf-8")
        result = self._read()
        self.assertEqual(
            [dict(row) for row in result["segments"]],
            [dict(row) for row in first["segments"]],
        )

    def test_wrong_tag_cache_falls_back(self):
        self._read()
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        payload["tag"] = "stale-tag-000000"
        self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        result = self._read()
        self.assertTrue(result["segments"], "口径不符必须重算而非供旧值")

    def test_missing_store_registration_restores_through_full_chain(self):
        """display cache 命中绝不替代 store 登记核对：登记缺失走原链路重存。"""
        self._read()
        with self.app.learning_store._connect() as db:
            db.execute("DELETE FROM transcript_sources WHERE sub_id=?", ("sub1",))
        self.app.learning_store.get_transcript_segments = Mock(
            wraps=self.app.learning_store.get_transcript_segments)
        result = self._read()
        self.assertTrue(result["segments"], "登记缺失必须重算")
        self.assertTrue(
            self.app.learning_store.transcript_source_matches(
                "sub1",
                source_path=os.path.realpath(str(self.vtt)),
                source_mtime_ns=self.vtt.stat().st_mtime_ns,
                source_size=self.vtt.stat().st_size,
            ),
            "原链路重存必须恢复登记（搜索索引/证据链依赖 store）",
        )

    def test_store_rows_and_schema_untouched_by_cache(self):
        """缓存层绝不改写证据层：首读合法落库后，缓存命中读不再动 store 行/表。"""
        self._read()  # 首读：原链路合法落库（与旧行为一致）
        with self.app.learning_store._connect() as db:
            schema_before = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            rows_before = db.execute(
                "SELECT segment_index,start_ms,end_ms,text FROM transcript_segments"
            ).fetchall()
        self.assertTrue(rows_before, "首读必须已落库证据层")
        self.app.learning_store.get_transcript_segments = Mock(
            side_effect=AssertionError("cache hit must not read store"))
        self._read()  # 缓存命中读
        with self.app.learning_store._connect() as db:
            schema_after = db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            rows_after = db.execute(
                "SELECT segment_index,start_ms,end_ms,text FROM transcript_segments"
            ).fetchall()
        self.assertEqual([tuple(r) for r in schema_after], [tuple(r) for r in schema_before])
        self.assertEqual([tuple(r) for r in rows_after], [tuple(r) for r in rows_before],
                         "缓存命中读绝不改写证据层行")


class _FoldReference:
    """旧版 _collapse_repeated_tokens 逐字复刻（等价性 oracle）。"""

    @staticmethod
    def fold(sr, text):
        matches = list(sr._SHAPE_TOKEN_RE.finditer(text))
        if len(matches) < 2:
            return text, 0
        values = [m.group(0) for m in matches]
        drop = [False] * len(values)
        folds = 0
        changed = True
        while changed:
            changed = False
            live = [i for i, d in enumerate(drop) if not d]
            vals = [values[i] for i in live]
            for k in range(len(vals) - 1):
                a, b = vals[k], vals[k + 1]
                if len(a) == 1 and len(b) >= 2 and b.startswith(a):
                    drop[live[k]] = True
                    folds += 1
                    changed = True
                    break
            if changed:
                continue
            for k in range(len(vals)):
                run = 1
                while k + run < len(vals) and vals[k + run] == vals[k]:
                    run += 1
                if run >= 2:
                    tok = vals[k]
                    if not (len(tok) == 1 and run == 2 and (tok + tok) in sr._SHAPE_LEGIT_REDUP):
                        for j in range(k + 1, k + run):
                            drop[live[j]] = True
                        folds += 1
                        changed = True
                        break
            if changed:
                continue
            for length in (2, 3, 4, 6, 8):
                limit = len(vals) - length
                for k in range(limit + 1):
                    block = vals[k:k + length]
                    if len(set(block)) == 1:
                        continue
                    j = k + length
                    reps = 1
                    while j + length <= len(vals) and vals[j:j + length] == block:
                        reps += 1
                        j += length
                    if reps < 2:
                        continue
                    partial = vals[j:len(vals)]
                    if partial and len(partial) < length and partial == block[:len(partial)]:
                        j += len(partial)
                    for t in range(k + length, j):
                        drop[live[t]] = True
                    folds += reps - 1
                    changed = True
                    break
                if changed:
                    break
        pieces = []
        last = 0
        for m, d in zip(matches, drop):
            if d:
                pieces.append(text[last:m.start()])
                last = m.end()
        pieces.append(text[last:])
        joined = "".join(pieces)
        joined = sr._SHAPE_FOLD_PUNCT_RUN.sub(r"\1", joined)
        joined = sr._SHAPE_FOLD_LEADING_PUNCT.sub("", joined)
        return joined, folds


class CollapseEquivalenceTests(unittest.TestCase):
    """折叠提速（memo+必要条件快径）与旧算法逐案等价。"""

    def test_equivalence_on_edge_and_random_corpus(self):
        import src.runtime.subtitle_reader as sr

        reference = _FoldReference.fold
        alphabet = ["微", "分", "方", "程", "然后", "那么", "这个", "参数",
                    "AB", "ABCD", "慢慢", "好", "行啊", "a1"]
        cases = [
            "", "微", "微微分", "微微分方程", "这个这个", "慢慢", "慢慢慢慢",
            "微微分微分方程", "ABABAB", "ABCDABCD", "这个这个这样", "嗯，慢慢",
            "微，微分", "a1a1a1", "程序程序程序",
        ]
        rng = random.Random(7)
        for _ in range(800):
            cases.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))))
        mismatches = []
        for text in cases:
            _COLLAPSE_MEMO.clear()
            got = _collapse_repeated_tokens(text)
            _COLLAPSE_MEMO.clear()
            want = reference(sr, text)
            if got != want:
                mismatches.append((text, got, want))
        self.assertEqual(mismatches, [], f"折叠语义漂移 {len(mismatches)} 案")

    def test_memo_hit_returns_identical_result(self):
        text = "这个这个例子说明边界条件的处理"
        _COLLAPSE_MEMO.clear()
        first = _collapse_repeated_tokens(text)
        second = _collapse_repeated_tokens(text)  # memo 命中
        self.assertEqual(first, second)
        _COLLAPSE_MEMO.clear()


if __name__ == "__main__":
    unittest.main()
