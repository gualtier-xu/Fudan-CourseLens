"""RANGE-FIX-1：WebVPN 中转腿「start=0 大段 Range」瞬态 403 两段式取流。

全合成：零网络、零凭据、零真实上游。E1 瞬态窗由 BlockWindowSession 仿真：
仅「Range start=0 且窗长>1KiB」返回 403 拦截页，其余 Range 恒 206（与
MEDIAWEBVPN-3 E1 实测矩阵一致）；分块序列经 iter_bytes 小 chunk 逐字节
断言。拦截窗记忆（模块级 TTL 态）在每个用例前后强制归零，避免污染同进程
后续测试。
"""

import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from path_utils import PROJECT_ROOT
from src.application import (
    CourseLensApplication,
    _media_relay_block_window,
    _media_relay_block_window_active,
    _media_relay_note_block_window,
    _media_relay_transient_block_failure,
    _media_relay_twostage_shape,
    _media_system_proxy_leg_active,
)
from src.runtime.media_source import (
    MAX_STREAM_RANGE_BYTES,
    MEDIA_STREAM_UPSTREAM_REJECTED,
    _ConfirmedServiceResponse,
    classify_open_failure,
    configure_media_stream_proxy_provider,
)

MEDIA_URL = "https://icourse.fudan.edu.cn/media/lecture1.mp4?Expires=123&Signature=abc"
PROBE_END = 1023
TOTAL = 20_000
FILE = bytes((index * 31 + 7) % 251 for index in range(TOTAL))
BLOCK_PAGE = b"<html>" + b"x" * 1251 + b"</html>"
FULL_WINDOW = f"bytes=0-{MAX_STREAM_RANGE_BYTES - 1}"
CONT_WINDOW = f"bytes=1024-{MAX_STREAM_RANGE_BYTES - 1}"
PROBE_WINDOW = f"bytes=0-{PROBE_END}"


def _reset_block_window():
    _media_relay_block_window["until"] = 0.0


def _relay_ranges(session):
    """中转腿逐请求的 Range 头序列（闭集断言用，零 URL 细节）。"""
    return [
        str((kwargs.get("headers") or {}).get("Range") or "")
        for url, kwargs in session.requests
        if "webvpn" in url
    ]


class FakeResponse:
    def __init__(self, *, status=206, body=b"", headers=None):
        self.status_code = status
        self.body = body
        merged = {"content-length": str(len(body))}
        merged.update({str(key).lower(): str(value) for key, value in (headers or {}).items()})
        self.headers = merged
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class BlockWindowSession:
    """E1 瞬态窗仿真：直连腿恒不可达；中转腿按请求形状应答。

    拦截形状（start=0 且 end>1KiB 探针界）→ 403 拦截页（计数 blocked_served）；
    其余 → 206 精确切片（clamp 到 total；start 越界 → 416）。
    ``block_all=True`` 退化成会话级真拒绝（连探针也 403），用于验证两段式
    失败仍按既有分类上抛。``block=False`` 为自愈后的健康面。
    """

    def __init__(self, *, total=TOTAL, block=True, block_all=False, etag='"etag-v1"'):
        self.total = total
        self.block = block
        self.block_all = block_all
        self.etag = etag
        self.requests = []
        self.blocked_served = []
        self.responses = []

    def get(self, url, **kwargs):
        range_header = str((kwargs.get("headers") or {}).get("Range") or "")
        self.requests.append((url, kwargs))
        if "webvpn" not in url:
            raise requests.exceptions.ConnectionError("off-campus direct unreachable")
        if not range_header:
            # HEAD/无 Range：上游整档 200（网关 _normalize 原样透传状态）。
            response = FakeResponse(
                status=200,
                body=FILE[:self.total],
                headers={
                    "content-type": "video/mp4",
                    "etag": self.etag,
                },
            )
            self.responses.append(response)
            return response
        start, end = self._parse(range_header)
        if self.block_all or (self.block and start == 0 and end > PROBE_END):
            self.blocked_served.append(range_header)
            response = FakeResponse(
                status=403, body=BLOCK_PAGE, headers={"content-type": "text/html"},
            )
            self.responses.append(response)
            return response
        if start >= self.total:
            response = FakeResponse(
                status=416, headers={"content-range": f"bytes */{self.total}"},
            )
            self.responses.append(response)
            return response
        end = min(end, self.total - 1)
        headers = {
            "content-type": "video/mp4",
            "content-range": f"bytes {start}-{end}/{self.total}",
        }
        etag = self.cont_etag if start == PROBE_END + 1 else self.etag
        if etag:
            headers["etag"] = etag
        response = FakeResponse(body=FILE[start:end + 1], headers=headers)
        self.responses.append(response)
        return response

    cont_etag = None

    @staticmethod
    def _parse(range_header):
        head, _, tail = range_header.partition("=")
        assert head.strip().lower() == "bytes", f"unexpected range {range_header!r}"
        start_text, _, end_text = tail.partition("-")
        assert start_text, f"suffix range unsupported in fake: {range_header!r}"
        return int(start_text), int(end_text)


def _raw_headers():
    return "User-Agent: synthetic\r\nCookie: session=secret\r\n"


class MediaRangeTwostageTests(unittest.TestCase):
    def setUp(self):
        from src.runtime import media_source

        media_source.reset_atrust_presence_cache()
        configure_media_stream_proxy_provider(_media_system_proxy_leg_active)
        _reset_block_window()
        self._temps = []
        self._services = []
        self._stdout = io.StringIO()

    def tearDown(self):
        _reset_block_window()
        configure_media_stream_proxy_provider(None)
        # Windows 文件锁：先关服务（DB 连接池收口），再删 temp 目录。
        for service in self._services:
            try:
                service.close()
            except Exception:
                pass
        for temp in self._temps:
            temp.cleanup()

    def _service(self, session):
        # 受管路径合同：temp 应用目录必须在项目内（path_utils.ensure_inside_project）。
        temp = tempfile.TemporaryDirectory(
            dir=PROJECT_ROOT / "runtime" / "cache", prefix="rangefix1-test-",
        )
        self._temps.append(temp)
        service = CourseLensApplication(Path(temp.name))
        self._services.append(service)
        service.catalog_repository = SimpleNamespace(
            get_lecture=lambda sub_id: {"course_id": "c1", "has_playback": True},
            close=lambda: None,
        )
        service._credentials = SimpleNamespace(get=lambda key: "saved")
        fake_client = SimpleNamespace(
            get_video_url=lambda course_id, sub_id: MEDIA_URL,
            get_stream_params=lambda url: (url, _raw_headers()),
            vpn=SimpleNamespace(session=session),
        )
        service.client = lambda: fake_client
        service._refresh_client_session = lambda: None
        return service

    def _open(self, service, range_header="", *, head_only=False):
        with redirect_stdout(self._stdout):
            return service.open_remote_media("sub-1", range_header, head_only=head_only)

    def test_transient_403_first_open_recovers_via_twostage(self):
        session = BlockWindowSession()
        service = self._service(session)
        stream = self._open(service)
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, f"bytes 0-{TOTAL - 1}/{TOTAL}")
        self.assertEqual(stream.content_length, TOTAL)
        self.assertEqual(stream.total_length, TOTAL)
        self.assertEqual(stream.start, 0)
        self.assertEqual(stream.etag, '"etag-v1"')
        # 分块序列逐字节断言：探针段 + 续段拼接 == 原窗口字节。
        self.assertEqual(b"".join(stream.iter_bytes(chunk_size=7)), FILE)
        stream.close()
        self.assertTrue(all(response.closed for response in session.responses))
        # 请求形状：首发单发被拦（网关 refresh 恰一次重试 → 同形状两次），
        # 两段式两段均非拦截形状。
        relay_requests = _relay_ranges(session)
        self.assertEqual(relay_requests, [FULL_WINDOW, FULL_WINDOW, PROBE_WINDOW, CONT_WINDOW])
        self.assertEqual(session.blocked_served, [FULL_WINDOW] * 2)
        # 判别闭集遥测行 + 拦截窗记忆置位。
        logs = self._stdout.getvalue()
        self.assertIn("[media] relay=webvpn route=open", logs)
        self.assertIn("[media] relay=webvpn transient403=detected route=twostage", logs)
        self.assertTrue(_media_relay_block_window_active())

    def test_window_active_opens_probe_first_without_blocked_request(self):
        session = BlockWindowSession()
        service = self._service(session)
        _media_relay_note_block_window()
        stream = self._open(service)
        self.assertEqual(b"".join(stream.iter_bytes()), FILE)
        stream.close()
        self.assertEqual(_relay_ranges(session), [PROBE_WINDOW, CONT_WINDOW])
        self.assertEqual(session.blocked_served, [])
        self.assertIn(
            "[media] relay=webvpn twostage=window route=open",
            self._stdout.getvalue(),
        )

    def test_healed_single_shot_clears_block_window(self):
        session = BlockWindowSession(block=False)
        service = self._service(session)
        # TTL 已过期的残留窗：单发放行成功后记忆必须清零回稳态。
        _media_relay_block_window["until"] = time.monotonic() - 1.0
        stream = self._open(service)
        self.assertEqual(b"".join(stream.iter_bytes()), FILE)
        stream.close()
        self.assertEqual(_relay_ranges(session), [FULL_WINDOW])
        self.assertFalse(_media_relay_block_window_active())
        self.assertEqual(_media_relay_block_window["until"], 0.0)

    def test_offset_and_small_ranges_stay_single_shot(self):
        session = BlockWindowSession()
        service = self._service(session)
        stream = self._open(service, "bytes=1024-65535")
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, f"bytes 1024-{TOTAL - 1}/{TOTAL}")
        self.assertEqual(b"".join(stream.iter_bytes()), FILE[1024:])
        stream.close()
        self.assertEqual(_relay_ranges(session), ["bytes=1024-65535"])
        self.assertEqual(session.blocked_served, [])
        self.assertNotIn("twostage", self._stdout.getvalue())

        session.requests.clear()
        session.responses.clear()
        stream = self._open(service, "bytes=0-1023")
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, f"bytes 0-{PROBE_END}/{TOTAL}")
        self.assertEqual(b"".join(stream.iter_bytes()), FILE[:PROBE_END + 1])
        stream.close()
        self.assertFalse(_media_relay_block_window_active())

    def test_head_only_skips_twostage(self):
        session = BlockWindowSession()
        service = self._service(session)
        stream = self._open(service, head_only=True)
        # HEAD 无 Range：上游整档 200，网关原样透传（不进两段式）。
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.content_length, TOTAL)
        self.assertEqual(_relay_ranges(session), [""])
        self.assertEqual(session.blocked_served, [])

    def test_small_file_returns_probe_alone(self):
        session = BlockWindowSession(total=800)
        service = self._service(session)
        stream = self._open(service)
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, "bytes 0-799/800")
        self.assertEqual(b"".join(stream.iter_bytes()), FILE[:800])
        stream.close()
        # 首发单发被拦两次（WAF 按 Range 形状拦截，不知文件大小），
        # 两段式探针即覆盖全窗 → 无续段请求。
        self.assertEqual(_relay_ranges(session), [FULL_WINDOW, FULL_WINDOW, PROBE_WINDOW])
        self.assertEqual(session.blocked_served, [FULL_WINDOW] * 2)

    def test_probe_rejection_raises_confirmed_without_window(self):
        session = BlockWindowSession(block_all=True)
        service = self._service(session)
        with self.assertRaises(_ConfirmedServiceResponse) as ctx:
            self._open(service)
        self.assertEqual(str(ctx.exception), "HTTP 403")
        self.assertEqual(classify_open_failure(ctx.exception), MEDIA_STREAM_UPSTREAM_REJECTED)
        self.assertFalse(_media_relay_block_window_active())
        # 单发（被拦 ×2）+ 探针恰一次重试（也被拒 ×2）= 中转腿 4 次请求。
        self.assertEqual(
            _relay_ranges(session),
            [FULL_WINDOW, FULL_WINDOW, PROBE_WINDOW, PROBE_WINDOW],
        )

    def _patch_proxy_detection(self, url, source):
        for target in ("src.application.detect_windows_system_proxy",
                       "src.runtime.media_source.detect_windows_system_proxy"):
            patcher = patch(target, return_value=(url, source))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_twostage_unreachable_falls_through_to_system_proxy(self):
        session = BlockWindowSession()

        def get(url, **kwargs):
            range_header = str((kwargs.get("headers") or {}).get("Range") or "")
            session.requests.append((url, kwargs))
            if "webvpn" in url:
                # 单发被拦后，两段式探针传输层失败：换面证据 → 第三级。
                if range_header == PROBE_WINDOW:
                    raise requests.exceptions.ConnectionError("probe transport down")
                session.blocked_served.append(range_header)
                return FakeResponse(
                    status=403, body=BLOCK_PAGE, headers={"content-type": "text/html"},
                )
            raise requests.exceptions.ConnectionError("off-campus direct unreachable")

        session.get = get
        service = self._service(session)
        service.set_media_stream_proxy({"enabled": True})
        self._patch_proxy_detection("http://127.0.0.1:8899", "system_registry")
        with self.assertRaises(requests.exceptions.ConnectionError):
            self._open(service)
        third_url, third_kwargs = session.requests[-1]
        self.assertEqual(third_url, MEDIA_URL)
        self.assertEqual(third_kwargs["proxies"], {
            "http": "http://127.0.0.1:8899", "https": "http://127.0.0.1:8899",
        })
        self.assertFalse(_media_system_proxy_leg_active())
        # 探针失败=窗口证据未成立，拦截窗记忆不得置位。
        self.assertFalse(_media_relay_block_window_active())
        self.assertIn("transient403=detected", self._stdout.getvalue())

    def test_etag_mismatch_between_stages_closes_and_raises(self):
        session = BlockWindowSession()
        session.cont_etag = '"etag-v2"'
        service = self._service(session)
        with self.assertRaises(RuntimeError):
            self._open(service)
        # 探针与续段两支响应体都必须收口，零泄漏。
        self.assertTrue(all(response.closed for response in session.responses))
        self.assertFalse(_media_relay_block_window_active())


class TwostageShapeGateTests(unittest.TestCase):
    def test_shape_gate_closed_matrix(self):
        self.assertEqual(
            _media_relay_twostage_shape("", head_only=False),
            (0, MAX_STREAM_RANGE_BYTES - 1),
        )
        self.assertEqual(
            _media_relay_twostage_shape("bytes=0-65535", head_only=False),
            (0, 65535),
        )
        self.assertIsNone(_media_relay_twostage_shape("bytes=0-1023", head_only=False))
        self.assertEqual(_media_relay_twostage_shape("bytes=0-1024", head_only=False), (0, 1024))
        self.assertIsNone(_media_relay_twostage_shape("bytes=1024-", head_only=False))
        self.assertIsNone(_media_relay_twostage_shape("bytes=-500", head_only=False))
        self.assertIsNone(_media_relay_twostage_shape("", head_only=True))
        self.assertIsNone(_media_relay_twostage_shape("bytes=1-2,3-4", head_only=False))
        self.assertIsNone(_media_relay_twostage_shape("not a range", head_only=False))

    def test_transient_block_failure_closed_discrimination(self):
        shape = (0, MAX_STREAM_RANGE_BYTES - 1)
        self.assertTrue(
            _media_relay_transient_block_failure(_ConfirmedServiceResponse("HTTP 403"), shape),
        )
        self.assertFalse(
            _media_relay_transient_block_failure(_ConfirmedServiceResponse("HTTP 401"), shape),
        )
        self.assertFalse(
            _media_relay_transient_block_failure(_ConfirmedServiceResponse("HTTP 403"), None),
        )
        self.assertFalse(
            _media_relay_transient_block_failure(RuntimeError("HTTP 403"), shape),
        )


if __name__ == "__main__":
    unittest.main()
