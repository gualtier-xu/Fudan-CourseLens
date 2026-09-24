from __future__ import annotations

import unittest
import sys
from contextlib import contextmanager
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"numpy": Mock(), "sherpa_onnx": Mock()}):
    from courselens_worker import asr

# 与 asr 同源取类，禁止跨模块直接 import platform_session：asr 是在
# patch.dict 窗口内导入的，platform_session 若此前未加载会在窗口内首执行、
# 窗口退出时被逐出，之后再 import 会二次执行出第二个类对象——梯的
# except 咬不住测试抛的异常（P55 自检实测踩中）。
PlatformSessionError = asr.PlatformSessionError


class ASRProxyLifecycleTests(unittest.TestCase):
    def test_all_pcm_chunks_share_one_pinned_media_session(self):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, _backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": "test",
        }]
        progress = Mock()

        def create_pcm(_url, target, *, offset, duration):
            self.assertGreaterEqual(offset, 0)
            self.assertGreater(duration, 0)
            target.write_bytes(b"pcm")

        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy") as media_proxy,
            patch.object(asr, "_decode_chunk_from_url", side_effect=create_pcm) as decode,
        ):
            proxy = media_proxy.return_value.__enter__.return_value
            proxy.url = "http://127.0.0.1/session"
            result = asr.transcribe(
                {
                    "payload": {
                        "mode": "automatic",
                        "media": {
                            "url": "https://media.example.com/lecture.mp4",
                            "duration_seconds": 1250,
                        },
                    },
                },
                sensevoice_dir=Mock(),
                proofread=None,
                progress=progress,
            )

        media_proxy.assert_called_once()
        self.assertEqual(proxy.refresh_source.call_count, 2)
        self.assertEqual(decode.call_count, 3)
        self.assertEqual(
            [item.args[0] for item in decode.call_args_list],
            ["http://127.0.0.1/session"] * 3,
        )
        self.assertEqual(
            [item.kwargs["offset"] for item in decode.call_args_list],
            [0.0, 600.0, 1200.0],
        )
        self.assertEqual([item.args for item in progress.call_args_list], [
            ("asr", 1, 3),
            ("asr", 2, 3),
            ("asr", 3, 3),
        ])
        self.assertEqual(result["metrics"]["chunks"], 3)


class ASREvidenceIdentityTests(unittest.TestCase):
    """Evidence provenance stamping over the mocked transcribe flow."""

    def _pool(self):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": f"{backend}@{int(offset_seconds)}",
        }]
        return pool

    def _run(self, pool, *, mode="automatic", prior=None, capture=None, media_seconds=1250, proofread="mock"):
        def create_pcm(_url, target, *, offset, duration):
            self.assertGreaterEqual(offset, 0)
            target.write_bytes(b"pcm-bytes")

        payload = {
            "mode": mode,
            "media": {
                "url": "https://media.example.com/lecture.mp4",
                "duration_seconds": media_seconds,
            },
        }
        if prior is not None:
            payload["checkpoint"] = prior
        proofread_fn = (
            Mock(return_value=[{"start_ms": 0, "end_ms": 1000, "text": "校对后"}])
            if proofread == "mock" else proofread
        )
        if proofread is None:
            proofread_fn = None
        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy"),
            patch.object(asr, "_decode_chunk_from_url", side_effect=create_pcm),
        ):
            return asr.transcribe(
                {"payload": payload},
                sensevoice_dir=Mock(),
                proofread=proofread_fn,
                progress=Mock(),
                checkpoint=capture,
            )

    def test_fresh_run_stamps_contract_provenance(self):
        checkpoints = []
        result = self._run(self._pool(), capture=checkpoints.append, proofread=None)
        segments = result["segments"]
        self.assertEqual(len(segments), 3)
        for segment in segments:
            self.assertRegex(segment["segment_id"], r"^seg:[0-9a-f]{12}$")
            self.assertRegex(segment["source_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(segment["provenance"]["producer"], asr.PRODUCER_ID)
            self.assertEqual(segment["provenance"]["model"], "paraformer")
            self.assertRegex(segment["provenance"]["config_hash"], r"^[0-9a-f]{12,64}$")
        # 每个 chunk 的检查点都携带可续跑的非秘密指纹状态
        self.assertEqual(len(checkpoints), 3)
        for checkpoint in checkpoints:
            self.assertRegex(checkpoint["pcm_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotRegex(str(checkpoints), r"media\.example\.com")

    def test_resumed_run_reproduces_the_fresh_run_identity(self):
        pool = self._pool()
        checkpoints = []
        fresh = self._run(pool, capture=checkpoints.append, proofread=None)
        interrupted = checkpoints[0]
        resumed = self._run(pool, prior=interrupted, proofread=None)
        self.assertEqual(
            [(item["start_ms"], item["end_ms"], item["text"]) for item in fresh["segments"]],
            [(item["start_ms"], item["end_ms"], item["text"]) for item in resumed["segments"]],
        )
        self.assertEqual(
            [item["segment_id"] for item in fresh["segments"]],
            [item["segment_id"] for item in resumed["segments"]],
        )
        self.assertEqual(
            fresh["segments"][0]["source_hash"],
            resumed["segments"][0]["source_hash"],
        )

    def test_checkpoint_without_current_chain_raw_fails_explicitly(self):
        # M4-ENABLE-1 U4：缺当前链精修 raw 的检查点（如退役链产物）续跑必须
        # 显式失败——静默混链会丢前段输出。
        pool = self._pool()
        prior = {
            "completed_chunks": 1,
            "total_chunks": 3,
            "mode": "automatic",
            "raw_sensevoice": [{"start_ms": 0, "end_ms": 1000, "text": "legacy@0"}],
        }
        with self.assertRaises(asr.ASRError):
            self._run(pool, prior=prior, proofread=None)

    def test_legacy_checkpoint_omits_unverifiable_provenance(self):
        pool = self._pool()
        checkpoints = []
        prior = {
            "completed_chunks": 1,
            "total_chunks": 3,
            "mode": "automatic",
            "raw_sensevoice": [{"start_ms": 0, "end_ms": 1000, "text": "legacy@0"}],
            "raw_paraformer": [{"start_ms": 0, "end_ms": 1000, "text": "legacy-fire@0"}],
        }
        result = self._run(pool, prior=prior, capture=checkpoints.append, proofread=None)
        self.assertEqual(len(result["segments"]), 3)
        for segment in result["segments"]:
            self.assertNotIn("segment_id", segment)
            self.assertNotIn("evidence_id", segment)
            self.assertNotIn("source_hash", segment)
            self.assertNotIn("provenance", segment)
        for checkpoint in checkpoints:
            self.assertNotIn("pcm_fingerprint", checkpoint)

    def test_automatic_proofread_stamps_final_and_raw_segments(self):
        pool = self._pool()
        result = self._run(pool, media_seconds=600)
        self.assertEqual(result["segments"][0]["text"], "校对后")
        self.assertEqual(
            result["segments"][0]["provenance"]["model"],
            "sensevoice+paraformer:proofread",
        )
        self.assertEqual(result["raw_sensevoice"][0]["provenance"]["model"], "sensevoice")
        self.assertEqual(result["raw_paraformer"][0]["provenance"]["model"], "paraformer")
        # 校对后的文本是独立证据：final ID 不与 raw ID 共享
        final_id = result["segments"][0]["segment_id"]
        raw_ids = {
            result["raw_sensevoice"][0]["segment_id"],
            result["raw_paraformer"][0]["segment_id"],
        }
        self.assertNotIn(final_id, raw_ids)


class ASRMediaRetryTests(unittest.TestCase):
    """第二十案：秒败族媒体获取先重取会话材料再重试当前块，界内不整单失败。"""

    FORMAT_REJECTED = "authorized media format was rejected by ffmpeg"

    def _pool(self):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": backend,
        }]
        return pool

    @contextmanager
    def _media(self, decode_side_effect):
        pool = self._pool()
        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy") as media_proxy,
            patch.object(asr, "_decode_chunk_from_url", side_effect=decode_side_effect) as decode,
            patch.object(asr.time, "sleep") as sleep,
        ):
            proxy = media_proxy.return_value.__enter__.return_value
            proxy.url = "http://127.0.0.1/session"

            def invoke():
                return asr.transcribe(
                    {
                        "payload": {
                            "mode": "automatic",
                            "media": {
                                "url": "https://media.example.com/lecture.mp4",
                                "duration_seconds": 1250,
                            },
                        },
                    },
                    sensevoice_dir=Mock(),
                    proofread=None,
                    progress=Mock(),
                )

            yield invoke, proxy, decode, sleep

    def test_fast_fail_family_recovers_after_bounded_refresh(self):
        # 块0 连续两败（0 字节/秒败签名），每次先刷新会话材料再重试同块
        outcome = [
            asr.ASRError(self.FORMAT_REJECTED),
            asr.ASRError(self.FORMAT_REJECTED),
        ]

        def decode(_url, target, *, offset, duration):
            if outcome:
                raise outcome.pop(0)
            target.write_bytes(b"pcm")

        with self._media(decode) as (invoke, proxy, decode_mock, sleep):
            result = invoke()
        # 3 块全部完成；块0 额外解码 2 次
        self.assertEqual(decode_mock.call_count, 5)
        # 刷新 = 2 次重试 + 块间轮换 2 次
        self.assertEqual(proxy.refresh_source.call_count, 4)
        self.assertEqual(
            [item.args[0] for item in sleep.call_args_list],
            [2.0, 5.0],
        )
        self.assertEqual(result["metrics"]["chunks"], 3)

    def test_retry_budget_exhaustion_keeps_closed_set_reason(self):
        def decode(_url, target, *, offset, duration):
            raise asr.ASRError(self.FORMAT_REJECTED)

        with self._media(decode) as (invoke, proxy, decode_mock, sleep):
            with self.assertRaises(asr.ASRError) as caught:
                invoke()
        # 穷尽后如实透传最后一个闭集原因，不改写码面
        self.assertEqual(str(caught.exception), self.FORMAT_REJECTED)
        self.assertEqual(decode_mock.call_count, 3)
        self.assertEqual(proxy.refresh_source.call_count, 2)
        self.assertEqual(
            [item.args[0] for item in sleep.call_args_list],
            [2.0, 5.0],
        )

    def test_deterministic_rejection_skips_retry_family(self):
        deterministic = "authorized media is missing a readable MP4 index"

        def decode(_url, target, *, offset, duration):
            raise asr.ASRError(deterministic)

        with self._media(decode) as (invoke, proxy, decode_mock, sleep):
            with self.assertRaises(asr.ASRError) as caught:
                invoke()
        self.assertEqual(str(caught.exception), deterministic)
        self.assertEqual(decode_mock.call_count, 1)
        self.assertEqual(proxy.refresh_source.call_count, 0)
        self.assertEqual(sleep.call_count, 0)


class P55ChunkBoundaryRefreshLadderTests(unittest.TestCase):
    """P55：块边界授权刷新从裸死变有界梯（真机事故 2026-09-24：117s 死窗无痕）。

    事故链：chunk 边界 refresh 抛 platform_auth_context_missing 在 decode-start
    遥测之前裸传播——本组钉「瞬态入梯重试、梯尽闭集诚实失败、逐次遥测留痕」。
    """

    @contextmanager
    def _media(self, decode_side_effect, telemetry_lines):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": backend,
        }]
        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy") as media_proxy,
            patch.object(asr, "_decode_chunk_from_url", side_effect=decode_side_effect),
            patch.object(asr, "_emit_telemetry", side_effect=telemetry_lines.append),
            patch.object(asr.time, "sleep"),
        ):
            proxy = media_proxy.return_value.__enter__.return_value
            proxy.url = "http://127.0.0.1/session"

            def invoke():
                return asr.transcribe(
                    {
                        "payload": {
                            "mode": "automatic",
                            "media": {
                                "url": "https://media.example.com/lecture.mp4",
                                "duration_seconds": 1250,
                            },
                        },
                    },
                    sensevoice_dir=Mock(),
                    proofread=None,
                    progress=Mock(),
                )

            yield invoke, proxy

    def test_boundary_refresh_recovers_after_transient_platform_error(self):
        refreshes = {"count": 0}

        def refresh():
            refreshes["count"] += 1
            if refreshes["count"] == 1:
                raise PlatformSessionError("platform_auth_context_missing")

        def decode(_url, target, *, offset, duration):
            target.write_bytes(b"pcm")

        lines = []
        with self._media(decode, lines) as (invoke, proxy):
            proxy.refresh_source.side_effect = refresh
            result = invoke()
        self.assertEqual(result["metrics"]["chunks"], 3)
        # 块1 边界首发1败+梯内重试1胜；块2 边界轮换1胜
        self.assertEqual(refreshes["count"], 3)
        retry_lines = [
            line for line in lines if line.startswith("stage=source-refresh-retry")
        ]
        self.assertEqual(len(retry_lines), 1)
        self.assertTrue(retry_lines[0].startswith(
            "stage=source-refresh-retry chunk=1 attempt=1 "
            "reason=platform_auth_context_missing"
        ), retry_lines[0])

    def test_boundary_refresh_exhausts_ladder_and_fails_closed_set(self):
        calls = {"count": 0}

        def refresh():
            calls["count"] += 1
            raise PlatformSessionError("platform_session_rejected")

        def decode(_url, target, *, offset, duration):
            target.write_bytes(b"pcm")

        lines = []
        with self._media(decode, lines) as (invoke, proxy):
            proxy.refresh_source.side_effect = refresh
            with self.assertRaises(PlatformSessionError) as caught:
                invoke()
        # 梯尽（3 次尝试）按最后闭集码如实失败，worker_failed 语义不变
        self.assertEqual(str(caught.exception), "platform_session_rejected")
        self.assertEqual(calls["count"], 3)
        refresh_lines = [
            line for line in lines if line.startswith("stage=source-refresh-")
        ]
        self.assertEqual(len(refresh_lines), 3)
        self.assertTrue(refresh_lines[0].startswith(
            "stage=source-refresh-retry chunk=1 attempt=1 reason=platform_session_rejected"))
        self.assertTrue(refresh_lines[1].startswith(
            "stage=source-refresh-retry chunk=1 attempt=2 reason=platform_session_rejected"))
        self.assertTrue(refresh_lines[2].startswith(
            "stage=source-refresh-failed chunk=1 attempt=3 reason=platform_session_rejected"))

    def test_boundary_refresh_deterministic_error_fails_without_ladder(self):
        def refresh():
            raise PlatformSessionError("platform_media_missing")

        def decode(_url, target, *, offset, duration):
            target.write_bytes(b"pcm")

        lines = []
        with self._media(decode, lines) as (invoke, proxy):
            proxy.refresh_source.side_effect = refresh
            with self.assertRaises(PlatformSessionError) as caught:
                invoke()
        # 确定性拒绝不进梯：一次即败，无重试遥测、无退避放大
        self.assertEqual(str(caught.exception), "platform_media_missing")
        self.assertEqual(proxy.refresh_source.call_count, 1)
        self.assertEqual(
            [line for line in lines if line.startswith("stage=source-refresh-")],
            [],
        )

    def test_media_retry_refresh_also_walks_the_ladder(self):
        """P55 邻面：媒体重取路径内的刷新动作同一梯，不留第二个裸抛缺口。"""
        outcome = [asr.ASRError("authorized media request returned HTTP 5xx")]
        refreshes = {"count": 0}

        def refresh():
            refreshes["count"] += 1
            if refreshes["count"] == 1:
                raise PlatformSessionError("platform_connection_failed")

        def decode(_url, target, *, offset, duration):
            if outcome:
                raise outcome.pop(0)
            target.write_bytes(b"pcm")

        lines = []
        with self._media(decode, lines) as (invoke, proxy):
            proxy.refresh_source.side_effect = refresh
            result = invoke()
        self.assertEqual(result["metrics"]["chunks"], 3)
        self.assertTrue(any(
            line.startswith(
                "stage=source-refresh-retry chunk=0 attempt=1 reason=platform_connection_failed"
            )
            for line in lines
        ))
        self.assertTrue(any(
            line.startswith("stage=media-retry chunk=0") for line in lines
        ))


class ProofreadDegradationTests(unittest.TestCase):
    """G7（ASRBENCH P1）：AI 校对失败降级交付原始识别，不整单带崩。"""

    def test_proofread_failure_degrades_to_raw_segments_with_warning(self):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": backend,
        }]
        # 注意：用 asr.LLMError 保证与生产 except 引用同一类对象
        # （双路径导入下 courselens_worker.llm 可能存在两个模块实例）。
        flaky = Mock(side_effect=asr.LLMError("boom"))

        def create_pcm(_url, target, *, offset, duration):
            target.write_bytes(b"pcm")

        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy"),
            patch.object(asr, "_decode_chunk_from_url", side_effect=create_pcm),
        ):
            result = asr.transcribe(
                {
                    "payload": {
                        "mode": "automatic",
                        "media": {
                            "url": "https://media.example.com/lecture.mp4",
                            "duration_seconds": 1250,
                        },
                    },
                },
                sensevoice_dir=Mock(),
                proofread=flaky,
                progress=Mock(),
            )
        self.assertEqual(result["warnings"], ["proofread_degraded"])
        # 交付的是原始精修识别结果（三条 chunk 段），不是空手而归
        self.assertEqual(len(result["segments"]), 3)
        self.assertNotIn(":proofread", str(result))

    def test_proofread_success_has_no_warning(self):
        pool = Mock()
        pool.transcribe_pcm.side_effect = lambda _path, backend, *, offset_seconds: [{
            "start_ms": int(offset_seconds * 1000),
            "end_ms": int(offset_seconds * 1000) + 1000,
            "text": backend,
        }]
        good = Mock(return_value=[{
            "start_ms": 0, "end_ms": 1000, "text": "校对后",
        }])

        def create_pcm(_url, target, *, offset, duration):
            target.write_bytes(b"pcm")

        with (
            patch.object(asr, "RecognizerPool", return_value=pool),
            patch.object(asr, "pinned_media_proxy"),
            patch.object(asr, "_decode_chunk_from_url", side_effect=create_pcm),
        ):
            result = asr.transcribe(
                {
                    "payload": {
                        "mode": "automatic",
                        "media": {
                            "url": "https://media.example.com/lecture.mp4",
                            "duration_seconds": 1250,
                        },
                    },
                },
                sensevoice_dir=Mock(),
                proofread=good,
                progress=Mock(),
            )
        self.assertNotIn("warnings", result)


class DurationProbeRetryTests(unittest.TestCase):
    """A5 邻接扫：时长探测与分块同族——秒败先重取会话材料再试一次。"""

    def test_probe_recovers_after_one_refresh_retry(self):
        calls = []

        def fake_run(source, command, *, timeout, capture_stdout):
            calls.append(dict(source))
            if len(calls) == 1:
                return 1, b"", b"err"
            return 0, b"1250.5", b""

        with patch.object(asr, "_run_media_proxy", side_effect=fake_run):
            self.assertEqual(asr._probe_duration({"url": "https://media.example.com/l.mp4"}), 1250.5)
        self.assertEqual(len(calls), 2)

    def test_probe_fails_closed_after_bounded_budget(self):
        with patch.object(asr, "_run_media_proxy", return_value=(1, b"", b"e")):
            with self.assertRaises(asr.ASRError) as caught:
                asr._probe_duration({"url": "https://media.example.com/l.mp4"})
        self.assertEqual(
            str(caught.exception),
            "authorized media duration could not be determined",
        )

    def test_probe_timeout_fails_fast_without_second_attempt(self):
        import subprocess

        def fake_run(source, command, *, timeout, capture_stdout):
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=120)

        with patch.object(asr, "_run_media_proxy", side_effect=fake_run):
            with self.assertRaises(asr.ASRError) as caught:
                asr._probe_duration({"url": "https://media.example.com/l.mp4"})
        self.assertEqual(
            str(caught.exception),
            "authorized media duration probe timed out",
        )


if __name__ == "__main__":
    unittest.main()
