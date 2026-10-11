"""T12（夜14-R7 台账）：media_source 续流腿与传输层分类直测。

既有 test_remote_media.py 已盖 gateway 行为面（auth 重试/传输失败上界/
body reset 续流消费腿）；本件只补直测缺口：
- ``_is_transport_layer_error`` 分类表直测（requests 异族 = 传输层，
  内建 ConnectionError = 会话纪元类，两族分界是 SOAK-F1 登录风暴修复
  的承重语义，此前仅经 gateway 行为测试间接带到）；
- ``_resume_stream`` 签名钳制 / etag 失配 fail-closed / 续流流禁二次
  body 重试（bounded recovery 注释合同）；
- 截断流（声明全量 Content-Length + 部分发送后 FIN、零异常）触发
  iter_bytes 干净耗尽腿——与既有异常腿测试互补；
- classify_open_failure 传输/未知两腿与 MediaStreamFailureLedger
  闭集记账（upstream_rejected 腿归 test_media_range_twostage.py）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests.exceptions

from src.runtime.media_source import (
    MEDIA_STREAM_FAILURE_CODES,
    MEDIA_STREAM_SUCCESS,
    MEDIA_STREAM_UNKNOWN,
    MEDIA_STREAM_UPSTREAM_UNREACHABLE,
    MediaStreamFailureLedger,
    RemoteMediaGateway,
    _ConfirmedServiceResponse,
    _is_transport_layer_error,
    classify_open_failure,
)


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class TransportClassifierTests(unittest.TestCase):
    """requests 异族=传输层（终局）；内建 OSError 族=会话纪元类（可重建）。"""

    def test_requests_transport_family_is_transport_layer(self):
        for exc in (
            requests.exceptions.RequestException("base"),
            requests.exceptions.ConnectionError("conn"),
            requests.exceptions.Timeout("timeout"),
            requests.exceptions.SSLError("tls"),
            requests.exceptions.ChunkedEncodingError("chunk"),
        ):
            self.assertTrue(_is_transport_layer_error(exc), type(exc).__name__)

    def test_builtin_and_domain_errors_are_session_epoch_family(self):
        # 内建 ConnectionError（OSError 子类）不是 requests 异族：旧语义
        # 保留重建+恰一次重试（gateway 行为测试在先，分类器是分界本体）。
        for exc in (
            ConnectionError("builtin"),
            ValueError("bad value"),
            RuntimeError("domain"),
            KeyError("missing"),
            _ConfirmedServiceResponse("HTTP 404"),
        ):
            self.assertFalse(_is_transport_layer_error(exc), type(exc).__name__)


class ClassifyOpenFailureTests(unittest.TestCase):
    def test_transport_failure_maps_to_upstream_unreachable(self):
        exc = requests.exceptions.ConnectionError("off-campus host unreachable")
        self.assertEqual(classify_open_failure(exc), MEDIA_STREAM_UPSTREAM_UNREACHABLE)

    def test_unknown_error_maps_to_unknown_code(self):
        self.assertEqual(classify_open_failure(ValueError("mystery")), MEDIA_STREAM_UNKNOWN)

    def test_success_code_is_member_of_closed_set(self):
        self.assertIn(MEDIA_STREAM_SUCCESS, MEDIA_STREAM_FAILURE_CODES)


class MediaStreamLedgerTests(unittest.TestCase):
    def test_notes_advance_seq_and_status_shape_is_closed(self):
        ledger = MediaStreamFailureLedger()
        self.assertEqual(ledger.status(), {"failure_code": MEDIA_STREAM_SUCCESS, "seq": 0})
        ledger.note(MEDIA_STREAM_UPSTREAM_UNREACHABLE)
        ledger.note(MEDIA_STREAM_SUCCESS)
        status = ledger.status()
        self.assertEqual(status["failure_code"], MEDIA_STREAM_SUCCESS)
        self.assertEqual(status["seq"], 2)

    def test_out_of_set_codes_are_ignored_without_seq_advance(self):
        ledger = MediaStreamFailureLedger()
        ledger.note("not_a_real_code")
        ledger.note("upstream_made_up")
        self.assertEqual(ledger.status(), {"failure_code": MEDIA_STREAM_SUCCESS, "seq": 0})


class _PartialThenFinResponse(FakeResponse):
    """声明全量 Content-Length，只发部分字节后 FIN（零异常截断流）。"""

    def __init__(self, full, sent, **kwargs):
        super().__init__(full, **kwargs)
        self._sent = sent

    def iter_content(self, chunk_size):
        yield self.body[:self._sent]


class ResumeStreamDirectTests(unittest.TestCase):
    def _gateway(self, session):
        return RemoteMediaGateway(lambda: (session, "private-url", {}), lambda: None)

    def test_resume_stream_clamps_negative_bounds_before_request(self):
        session = FakeSession([
            FakeResponse(b"xy", status=206, headers={
                "content-length": "2", "content-range": "bytes 0-1/1", "etag": "v1",
            }),
        ])
        resumed = self._gateway(session)._resume_stream(-5, -1, "")
        self.assertEqual(session.requests[-1][1]["headers"]["Range"], "bytes=0-0")
        self.assertEqual(resumed.status, 206)

    def test_resume_stream_closes_and_fails_closed_on_etag_change(self):
        session = FakeSession([
            FakeResponse(b"abcdef", status=206, headers={
                "content-length": "6", "content-range": "bytes 0-5/6", "etag": "v2",
            }),
        ])
        gateway = self._gateway(session)
        with self.assertRaisesRegex(RuntimeError, "upstream media changed"):
            gateway._resume_stream(0, 5, "v1")

    def test_resumed_stream_carries_no_second_body_retry(self):
        session = FakeSession([
            FakeResponse(b"cdef", status=206, headers={
                "content-length": "4", "content-range": "bytes 2-5/6", "etag": "v1",
            }),
        ])
        resumed = self._gateway(session)._resume_stream(2, 5, "v1")
        self.assertIsNone(resumed._resume, "续流流禁二次 body 重试（bounded recovery 合同）")

    def test_empty_expectation_skips_etag_comparison(self):
        session = FakeSession([
            FakeResponse(b"cdef", status=206, headers={
                "content-length": "4", "content-range": "bytes 2-5/6", "etag": "v9",
            }),
        ])
        resumed = self._gateway(session)._resume_stream(2, 5, "")
        self.assertEqual(resumed.etag, "v9")


class TruncatedStreamResumeTests(unittest.TestCase):
    def test_clean_exhaustion_resumes_remaining_range_once(self):
        session = FakeSession([
            _PartialThenFinResponse(
                b"abcdef", 3, status=206,
                headers={"content-length": "6", "content-range": "bytes 0-5/6", "etag": "v1"},
            ),
            FakeResponse(b"def", status=206, headers={
                "content-length": "3", "content-range": "bytes 3-5/6", "etag": "v1",
            }),
        ])
        stream = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: None
        ).open("bytes=0-5")
        self.assertEqual(b"".join(stream.iter_bytes()), b"abcdef")
        self.assertEqual(session.requests[1][1]["headers"]["Range"], "bytes=3-5")
        # etag 校验由 _resume_stream 本体承载（两腿同享），异常腿 :122 只是
        # 第二道防；上游换源在任一腿都 fail-closed（直测见
        # test_resume_stream_closes_and_fails_closed_on_etag_change）。


if __name__ == "__main__":
    unittest.main()
