"""MEDIAWEBVPN-3：媒体流三级取流层级（直连 → WebVPN 中转 → 系统代理）。

全合成：零网络、零凭据、零真实上游。CourseLensApplication 以 temp 目录
构造；client/catalog/凭据门替身注入；FakeSession 记录每个请求的
``(url, kwargs)`` 供逐断言。失败账本语义与网关内部重试均走真实实现。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from path_utils import PROJECT_ROOT
from src.application import (
    MEDIA_WEBVPN_RELAY_ALLOWED_HOSTS,
    MEDIA_WEBVPN_RELAY_STATE_KEY,
    CourseLensApplication,
    _media_system_proxy_leg_active,
    _media_webvpn_relay_headers,
    _media_webvpn_relay_target,
)
from src.runtime.media_source import (
    MAX_STREAM_RANGE_BYTES,
    _ConfirmedServiceResponse,
    configure_media_stream_proxy_provider,
    media_stream_proxy_enabled,
)

MEDIA_URL = "https://icourse.fudan.edu.cn/media/lecture1.mp4?Expires=123&Signature=abc"
OFF_SET_URL = "https://cdn.example.com/media/lecture1.mp4?Expires=123&Signature=abc"
STREAM_BODY = b"0123456789"


class FakeResponse:
    def __init__(self, body=STREAM_BODY, *, status=200, headers=None):
        self.body = body
        self.status_code = status
        merged = {"content-length": str(len(body))}
        merged.update({str(k).lower(): str(v) for k, v in (headers or {}).items()})
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


class ScriptedSession:
    """按剧本应答：Exception 实例=抛出（传输类失败归 upstream_unreachable）。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _raw_headers():
    return "User-Agent: synthetic\r\nCookie: session=secret\r\n"


class MediaWebvpnRelayCascadeTests(unittest.TestCase):
    def setUp(self):
        from src.runtime import media_source

        media_source.reset_atrust_presence_cache()
        configure_media_stream_proxy_provider(_media_system_proxy_leg_active)
        self._media_source = media_source
        self._temps = []
        self._services = []

    def tearDown(self):
        configure_media_stream_proxy_provider(None)
        # Windows 文件锁：先关服务（DB 连接池收口），再删 temp 目录。
        for service in self._services:
            try:
                service.close()
            except Exception:
                pass
        for temp in self._temps:
            temp.cleanup()

    def _service(self, session, *, media_url=MEDIA_URL):
        # 受管路径合同：temp 应用目录必须在项目内（path_utils.ensure_inside_project）。
        temp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache",
                                          prefix="mwebvpn3-test-")
        self._temps.append(temp)
        service = CourseLensApplication(Path(temp.name))
        self._services.append(service)
        service.catalog_repository = SimpleNamespace(
            get_lecture=lambda sub_id: {"course_id": "c1", "has_playback": True},
            close=lambda: None,
        )
        service._credentials = SimpleNamespace(get=lambda key: "saved")
        fake_client = SimpleNamespace(
            get_video_url=lambda course_id, sub_id: media_url,
            get_stream_params=lambda url: (url, _raw_headers()),
            vpn=SimpleNamespace(session=session),
        )
        service.client = lambda: fake_client
        service._refresh_client_session = lambda: None
        return service

    def test_direct_success_keeps_single_request_without_relay(self):
        session = ScriptedSession([FakeResponse()])
        service = self._service(session)
        stream = service.open_remote_media("sub-1")
        self.assertEqual(b"".join(stream.iter_bytes()), STREAM_BODY)
        self.assertEqual(len(session.requests), 1)
        url, kwargs = session.requests[0]
        self.assertEqual(url, MEDIA_URL)
        # 第 1 级恒显式直连：系统代理偏好即使开着也不作用于首跳（语义收窄守卫）。
        self.assertEqual(kwargs["proxies"], {"http": None, "https": None})

    def test_direct_unreachable_rewrites_through_webvpn_exactly_once(self):
        session = ScriptedSession([
            requests.exceptions.ConnectionError("off-campus unreachable"),
            FakeResponse(),
        ])
        service = self._service(session)
        stream = service.open_remote_media("sub-1")
        self.assertEqual(b"".join(stream.iter_bytes()), STREAM_BODY)
        self.assertEqual(len(session.requests), 2)
        relay_url, kwargs = session.requests[1]
        # 改写面：webvpn 主机 + 密文主机段；明文媒体主机名不得出现。
        self.assertTrue(relay_url.startswith("https://webvpn.fudan.edu.cn/https/"))
        self.assertNotIn("icourse.fudan.edu.cn", relay_url)
        # 签名透传：path 尾段与 query 逐字节保留（验签输入不变性）。
        self.assertTrue(relay_url.split("?", 1)[0].endswith("/media/lecture1.mp4"))
        self.assertIn("Expires=123&Signature=abc", relay_url)
        # 最小头集：jar 接管 Cookie，手拼 Cookie 串不进 webvpn 腿。
        headers = kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertIn("User-Agent", headers)
        self.assertTrue(headers.get("Referer", "").startswith("https://icourse.fudan.edu.cn"))
        # Range 仍由网关按请求边界下发。
        self.assertEqual(headers["Range"], f"bytes=0-{MAX_STREAM_RANGE_BYTES - 1}")
        # 第 2 级同样显式直连。
        self.assertEqual(kwargs["proxies"], {"http": None, "https": None})

    def test_relay_disabled_by_app_state_stops_after_direct_failure(self):
        # WEBVPN-AUTO-1 后无设置动作：app-state 文件仍是无 UI 兜底开关，
        # 直写 task_store 验证运行时门语义不变。
        session = ScriptedSession([requests.exceptions.ConnectionError("unreachable")])
        service = self._service(session)
        service.task_store.set_app_state(MEDIA_WEBVPN_RELAY_STATE_KEY, {"enabled": False})
        self.assertFalse(service.media_webvpn_relay_enabled())
        with self.assertRaises(requests.exceptions.ConnectionError):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 1)

    def test_out_of_set_host_never_rewrites(self):
        session = ScriptedSession([requests.exceptions.ConnectionError("unreachable")])
        service = self._service(session, media_url=OFF_SET_URL)
        with self.assertRaises(requests.exceptions.ConnectionError):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 1)

    def test_confirmed_rejection_does_not_enter_relay(self):
        # 网关语义：401/403 首试→refresh 恰一次重试；再 403=确认性终局。
        session = ScriptedSession([FakeResponse(status=403), FakeResponse(status=403)])
        service = self._service(session)
        with self.assertRaises(_ConfirmedServiceResponse):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 2)

    def test_both_unreachable_without_proxy_preference_stops_after_relay(self):
        session = ScriptedSession([
            requests.exceptions.ConnectionError("direct unreachable"),
            requests.exceptions.ConnectionError("relay unreachable"),
        ])
        service = self._service(session)
        with self.assertRaises(requests.exceptions.ConnectionError):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 2)
        self.assertFalse(_media_system_proxy_leg_active())

    def _patch_proxy_detection(self, url, source):
        # 两处消费点同补：application 层级闸 + media_source._media_stream_proxies。
        for target in ("src.application.detect_windows_system_proxy",
                       "src.runtime.media_source.detect_windows_system_proxy"):
            patcher = patch(target, return_value=(url, source))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_third_tier_routes_original_url_through_system_proxy(self):
        session = ScriptedSession([
            requests.exceptions.ConnectionError("direct unreachable"),
            requests.exceptions.ConnectionError("relay unreachable"),
            requests.exceptions.ConnectionError("proxy unreachable"),
        ])
        service = self._service(session)
        service.set_media_stream_proxy({"enabled": True})
        self._patch_proxy_detection("http://127.0.0.1:8899", "system_registry")
        with self.assertRaises(requests.exceptions.ConnectionError):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 3)
        third_url, third_kwargs = session.requests[2]
        self.assertEqual(third_url, MEDIA_URL)
        self.assertEqual(third_kwargs["proxies"], {
            "http": "http://127.0.0.1:8899", "https": "http://127.0.0.1:8899",
        })
        # 三级位按请求收口：线程local 不得残留。
        self.assertFalse(_media_system_proxy_leg_active())

    def test_third_tier_requires_detected_proxy(self):
        session = ScriptedSession([
            requests.exceptions.ConnectionError("direct unreachable"),
            requests.exceptions.ConnectionError("relay unreachable"),
        ])
        service = self._service(session)
        service.set_media_stream_proxy({"enabled": True})
        self._patch_proxy_detection("", "none")
        with self.assertRaises(requests.exceptions.ConnectionError):
            service.open_remote_media("sub-1")
        self.assertEqual(len(session.requests), 2)


class MediaWebvpnRelayPreferenceTests(unittest.TestCase):
    """WEBVPN-AUTO-1 后：设置面 GET/动作已移除，偏好域仅剩 app-state
    无 UI 兜底开关与运行时默认语义。"""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(
            dir=PROJECT_ROOT / "runtime" / "cache", prefix="mwebvpn3-test-",
        )
        self.service = CourseLensApplication(Path(self._temp.name))

    def tearDown(self):
        try:
            self.service.close()
        except Exception:
            pass
        self._temp.cleanup()

    def test_default_is_on(self):
        self.assertTrue(self.service.media_webvpn_relay_enabled())

    def test_app_state_fallback_switch_without_ui(self):
        self.service.task_store.set_app_state(MEDIA_WEBVPN_RELAY_STATE_KEY, {"enabled": False})
        self.assertFalse(self.service.media_webvpn_relay_enabled())
        # 非 dict 脏值容错：视为缺省开。
        self.service.task_store.set_app_state(MEDIA_WEBVPN_RELAY_STATE_KEY, "garbage")
        self.assertTrue(self.service.media_webvpn_relay_enabled())
        self.service.task_store.set_app_state(MEDIA_WEBVPN_RELAY_STATE_KEY, {"enabled": True})
        self.assertTrue(self.service.media_webvpn_relay_enabled())

    def test_snapshot_no_longer_exposes_setting_key(self):
        snapshot = self.service.settings_privacy_snapshot()
        self.assertNotIn("media_webvpn_relay", snapshot)


class MediaWebvpnRelayTargetTests(unittest.TestCase):
    def test_rewrite_roundtrip_preserves_signature_bytes(self):
        url = (
            "https://icourse.fudan.edu.cn/media/lecture1.mp4"
            "?Expires=123&Signature=ab%2Bc==&t=1-2-3&clientUUID=x"
        )
        relay = _media_webvpn_relay_target(url)
        self.assertIsNotNone(relay)
        from src.api.webvpn import get_ordinary_url

        self.assertEqual(get_ordinary_url(relay), url)

    def test_closed_set_gates(self):
        self.assertEqual(
            MEDIA_WEBVPN_RELAY_ALLOWED_HOSTS, frozenset({"icourse.fudan.edu.cn"}),
        )
        for dirty in (
            "http://icourse.fudan.edu.cn/a.mp4?x=1",
            "https://cdn.example.com/a.mp4?x=1",
            "https://icourse.fudan.edu.cn.evil.test/a.mp4",
            "https://u:p@icourse.fudan.edu.cn/a.mp4",
            "not a url",
            "",
        ):
            self.assertIsNone(_media_webvpn_relay_target(dirty))

    def test_relay_headers_are_minimal_and_credential_free(self):
        headers = _media_webvpn_relay_headers()
        self.assertEqual(set(headers), {"User-Agent", "Referer", "Accept"})
        self.assertNotIn("Cookie", {key.casefold() for key in headers})


if __name__ == "__main__":
    unittest.main()
