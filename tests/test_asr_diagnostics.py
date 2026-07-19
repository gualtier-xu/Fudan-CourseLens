from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from courselens_worker.asr import _decode_chunk
from courselens_worker.runner import safe_worker_error_detail


class ASRError(RuntimeError):
    pass


class ASRDiagnosticsTests(unittest.TestCase):
    def test_reason_codes_are_closed_set_and_do_not_echo_input(self):
        self.assertEqual(
            safe_worker_error_detail(ASRError("ffmpeg could not decode the authorized media stream")),
            "media_decode_failed",
        )
        secret = "https://example.invalid/video?token=secret"
        reason = safe_worker_error_detail(ASRError(secret))
        self.assertEqual(reason, "asr_error")
        self.assertNotIn("secret", reason)

    def test_ffmpeg_text_is_reduced_to_a_fixed_http_reason(self):
        error = ASRError("authorized media request returned HTTP 403")
        self.assertEqual(safe_worker_error_detail(error), "media_http_403")

    @patch("courselens_worker.asr.subprocess.run")
    @patch("courselens_worker.asr.pinned_connect_proxy")
    def test_slice_decode_is_non_seekable_and_discards_after_input_open(self, connect_proxy, run):
        proxy = SimpleNamespace(
            proxy_url="http://127.0.0.1:12345",
            source_url="https://media.example.com/video",
            headers={},
            failure_code="",
        )
        context = Mock()
        context.__enter__ = Mock(return_value=proxy)
        context.__exit__ = Mock(return_value=False)
        connect_proxy.return_value = context

        def complete(command, **_kwargs):
            Path(command[-1]).write_bytes(b"pcm")
            return SimpleNamespace(returncode=0, stderr="")

        run.side_effect = complete
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "slice.f32le"
            _decode_chunk({}, target, offset=600, duration=300)
        command = run.call_args.args[0]
        input_index = command.index("-i")
        seek_index = command.index("-ss")
        self.assertLess(command.index("-seekable"), input_index)
        self.assertLess(input_index, seek_index)
        self.assertEqual(command[command.index("-seekable") + 1], "0")


if __name__ == "__main__":
    unittest.main()
