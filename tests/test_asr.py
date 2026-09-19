from __future__ import annotations

import unittest
import sys
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"numpy": Mock(), "sherpa_onnx": Mock()}):
    from courselens_worker import asr


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
                firered_dir=Mock(),
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
                firered_dir=Mock(),
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
            self.assertEqual(segment["provenance"]["model"], "firered")
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

    def test_legacy_checkpoint_omits_unverifiable_provenance(self):
        pool = self._pool()
        checkpoints = []
        prior = {
            "completed_chunks": 1,
            "total_chunks": 3,
            "mode": "automatic",
            "raw_sensevoice": [{"start_ms": 0, "end_ms": 1000, "text": "legacy@0"}],
            "raw_firered": [{"start_ms": 0, "end_ms": 1000, "text": "legacy-fire@0"}],
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
            "sensevoice+firered:proofread",
        )
        self.assertEqual(result["raw_sensevoice"][0]["provenance"]["model"], "sensevoice")
        self.assertEqual(result["raw_firered"][0]["provenance"]["model"], "firered")
        # 校对后的文本是独立证据：final ID 不与 raw ID 共享
        final_id = result["segments"][0]["segment_id"]
        raw_ids = {
            result["raw_sensevoice"][0]["segment_id"],
            result["raw_firered"][0]["segment_id"],
        }
        self.assertNotIn(final_id, raw_ids)


if __name__ == "__main__":
    unittest.main()
