import json
import sys
import unittest
import unittest.mock

from src.runtime.media_source import (
    MAX_STREAM_RANGE_BYTES,
    RemoteMediaGateway,
    bounded_range_header,
    parse_upstream_range,
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


class BrokenResponse(FakeResponse):
    def iter_content(self, chunk_size):
        yield self.body
        raise ConnectionResetError("synthetic reset")


class RemoteMediaTests(unittest.TestCase):
    def test_browser_ranges_are_bounded_before_upstream_request(self):
        self.assertEqual(bounded_range_header(""), f"bytes=0-{MAX_STREAM_RANGE_BYTES - 1}")
        self.assertEqual(bounded_range_header("bytes=10-"), f"bytes=10-{10 + MAX_STREAM_RANGE_BYTES - 1}")
        self.assertEqual(bounded_range_header("bytes=10-999999999"), f"bytes=10-{10 + MAX_STREAM_RANGE_BYTES - 1}")
        self.assertEqual(bounded_range_header("bytes=-999999999"), f"bytes=-{MAX_STREAM_RANGE_BYTES}")

    def test_range_validation_rejects_multiple_ranges(self):
        self.assertEqual(parse_upstream_range("bytes=10-20"), (10, 20))
        self.assertEqual(parse_upstream_range("bytes=-50"), (None, 50))
        with self.assertRaises(ValueError):
            parse_upstream_range("bytes=0-1,5-6")

    def test_normalizes_upstream_partial_response(self):
        response = FakeResponse(
            b"2345",
            status=206,
            headers={
                "content-type": "video/mp4",
                "content-length": "4",
                "content-range": "bytes 2-5/10",
            },
        )
        session = FakeSession([response])
        gateway = RemoteMediaGateway(lambda: (session, "private-url", {}), lambda: None)
        stream = gateway.open("bytes=2-5")
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, "bytes 2-5/10")
        self.assertEqual(b"".join(stream.iter_bytes()), b"2345")

    def test_slices_server_that_ignores_range_without_disk_cache(self):
        response = FakeResponse(b"0123456789", headers={"content-length": "10"})
        session = FakeSession([response])
        gateway = RemoteMediaGateway(lambda: (session, "private-url", {}), lambda: None)
        stream = gateway.open("bytes=3-6")
        self.assertEqual(stream.status, 206)
        self.assertEqual(stream.content_range, "bytes 3-6/10")
        self.assertEqual(b"".join(stream.iter_bytes()), b"3456")

    def test_refreshes_once_after_auth_failure(self):
        session = FakeSession([
            FakeResponse(status=403),
            FakeResponse(b"ok", headers={"content-length": "2"}),
        ])
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {"Authorization": "secret"}),
            lambda: refreshes.append(True),
        )
        stream = gateway.open()
        self.assertEqual(b"".join(stream.iter_bytes()), b"ok")
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(all(request[1]["headers"]["Range"].startswith("bytes=0-") for request in session.requests))

    def test_refreshes_once_after_401_with_same_bounded_range(self):
        session = FakeSession([
            FakeResponse(status=401),
            FakeResponse(b"abcd", status=206, headers={
                "content-length": "4", "content-range": "bytes 8-11/4096",
            }),
        ])
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        stream = gateway.open("bytes=8-11")
        self.assertEqual(stream.status, 206)
        self.assertEqual(b"".join(stream.iter_bytes()), b"abcd")
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(
            session.requests[1][1]["headers"]["Range"], "bytes=8-11",
            "刷新后必须以同一有界区间重试，绝不整文件下载",
        )

    def test_416_is_forwarded_without_body(self):
        response = FakeResponse(status=416, headers={"content-range": "bytes */10"})
        session = FakeSession([response])
        stream = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: None
        ).open("bytes=20-")
        self.assertEqual(stream.status, 416)
        self.assertEqual(stream.content_range, "bytes */10")
        self.assertEqual(stream.content_length, 0)

    def test_confirmed_service_response_is_terminal(self):
        response = FakeResponse(status=500)
        session = FakeSession([response])
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(RuntimeError) as raised:
            gateway.open("bytes=0-99")
        self.assertIn("HTTP 500", str(raised.exception))
        self.assertEqual(refreshes, [], "确认的服务响应不重建会话")
        self.assertEqual(len(session.requests), 1, "确认的服务响应不二次请求")
        self.assertTrue(response.closed)

    def test_confirmed_not_found_is_terminal_too(self):
        session = FakeSession([FakeResponse(status=404)])
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(RuntimeError):
            gateway.open()
        self.assertEqual(refreshes, [])
        self.assertEqual(len(session.requests), 1)

    def test_second_auth_failure_is_terminal(self):
        session = FakeSession([FakeResponse(status=401), FakeResponse(status=401)])
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(RuntimeError):
            gateway.open()
        self.assertEqual(len(refreshes), 1, "恰一次会话纪元验证")
        self.assertEqual(len(session.requests), 2, "401 刷新后恰重试一次，随后终局")

    def test_transport_layer_failure_does_not_rebuild_session(self):
        # SOAK-F1（LIVE-VALIDATE-2）：requests 传输类失败（SSLError 族）重建
        # 会话毫无治愈力，且每次重建=一次全量 WebVPN 重登录（44s 风暴实测
        # 根因）。必须零重建、单请求、立刻终局。
        from requests.exceptions import SSLError

        session = FakeSession([])
        session.get = lambda url, **kwargs: (
            session.requests.append((url, kwargs)),
            (_ for _ in ()).throw(SSLError("nightly tls failure")),
        )[0]
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(SSLError):
            gateway.open()
        self.assertEqual(refreshes, [], "传输类失败不重建会话（零重登录）")
        self.assertEqual(len(session.requests), 1, "单请求立刻终局，不重试")

    def test_unknown_resolver_failure_keeps_refresh_and_single_retry(self):
        # 非 transport 类失败（既有语义）：重建会话恰一次并重试一次。
        from requests.exceptions import SSLError

        session = FakeSession([])
        flips = {"n": 0}

        def flaky_get(url, **kwargs):
            session.requests.append((url, kwargs))
            flips["n"] += 1
            if flips["n"] == 1:
                raise RuntimeError("synthetic resolver hiccup")
            raise SSLError("second attempt surfaces transport truth")

        session.get = flaky_get
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(SSLError):
            gateway.open()
        self.assertEqual(len(refreshes), 1, "未知失败保留恰一次会话纪元验证")
        self.assertEqual(len(session.requests), 2, "重建后恰重试一次")

    def test_epoch_verification_failure_is_terminal(self):
        session = FakeSession([FakeResponse(status=403)])
        refresh_calls = []

        def failing_refresh():
            refresh_calls.append(True)
            raise RuntimeError("synthetic credential rejection: epoch not verified")

        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), failing_refresh,
        )
        with self.assertRaises(RuntimeError) as raised:
            gateway.open()
        self.assertIn("credential rejection", str(raised.exception))
        self.assertEqual(len(refresh_calls), 1, "验证失败绝不二次重建会话")
        self.assertEqual(len(session.requests), 1, "会话纪元未验证绝不二次请求")

    def test_transport_failure_retries_same_range_exactly_once(self):
        class TransportFailSession:
            def __init__(self):
                self.requests = 0
                self.ranges = []

            def get(self, url, **kwargs):
                self.requests += 1
                self.ranges.append((kwargs.get("headers") or {}).get("Range"))
                if self.requests == 1:
                    raise ConnectionError("synthetic transport failure")
                return FakeResponse(b"ok", headers={"content-length": "2"})

        session = TransportFailSession()
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        stream = gateway.open("bytes=0-1")
        self.assertEqual(b"".join(stream.iter_bytes()), b"ok")
        self.assertEqual(refreshes, [True])
        self.assertEqual(
            session.ranges, ["bytes=0-1", "bytes=0-1"],
            "传输恢复前后区间语义逐字节一致，绝不整文件下载",
        )
        self.assertEqual(session.requests, 2)

    def test_repeated_transport_failure_is_bounded(self):
        class AlwaysFailSession:
            def __init__(self):
                self.requests = 0

            def get(self, url, **kwargs):
                self.requests += 1
                raise ConnectionError("synthetic outage")

        session = AlwaysFailSession()
        refreshes = []
        gateway = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: refreshes.append(True),
        )
        with self.assertRaises(ConnectionError):
            gateway.open()
        self.assertEqual(session.requests, 2, "持续传输失败至多两次请求（恰一次重试）")
        self.assertEqual(len(refreshes), 1)

    def test_body_reset_resumes_remaining_range_once(self):
        session = FakeSession([
            BrokenResponse(
                b"abc", status=206,
                headers={"content-length": "6", "content-range": "bytes 0-5/6", "etag": "v1"},
            ),
            FakeResponse(
                b"def", status=206,
                headers={"content-length": "3", "content-range": "bytes 3-5/6", "etag": "v1"},
            ),
        ])
        stream = RemoteMediaGateway(
            lambda: (session, "private-url", {}), lambda: None
        ).open("bytes=0-5")
        self.assertEqual(b"".join(stream.iter_bytes()), b"abcdef")
        self.assertEqual(session.requests[1][1]["headers"]["Range"], "bytes=3-5")


class FakeStore:
    """app_state 鸭子型替身（media_source 设置面只依赖 get/set 两个方法）。"""

    def __init__(self, state=None):
        self.state = dict(state or {})

    def get_app_state(self, key, default=None):
        return self.state.get(key, default)

    def set_app_state(self, key, value):
        self.state[key] = value


class _ProxyProbeSession(FakeSession):
    """记录 proxies 面的会话替身（断言开流请求实际携带的代理配置）。"""


class MediaStreamProxySettingTests(unittest.TestCase):
    def test_default_is_off_and_dirty_values_stay_off(self):
        from src.runtime.media_source import media_stream_proxy_enabled

        self.assertFalse(media_stream_proxy_enabled(FakeStore()))
        self.assertFalse(media_stream_proxy_enabled(FakeStore({"media_stream_proxy": {"enabled": "yes"}})))
        self.assertFalse(media_stream_proxy_enabled(FakeStore({"media_stream_proxy": {"enabled": 1}})))
        self.assertFalse(media_stream_proxy_enabled(None))
        self.assertTrue(media_stream_proxy_enabled(FakeStore({"media_stream_proxy": {"enabled": True}})))
        self.assertFalse(media_stream_proxy_enabled(None))

    def test_set_rejects_dirty_requests_without_persisting(self):
        from src.runtime.media_source import set_media_stream_proxy_setting

        store = FakeStore()
        for request in (None, {}, {"enabled": "on"}, {"enabled": 1}, "on"):
            with self.assertRaises(ValueError):
                set_media_stream_proxy_setting(store, request)
        self.assertEqual(store.state, {})

    def test_set_persists_schema_and_returns_closed_snapshot(self):
        from src.runtime.media_source import (
            MEDIA_STREAM_PROXY_STATE_KEY,
            MEDIA_STREAM_PROXY_STATE_SCHEMA,
            set_media_stream_proxy_setting,
        )

        store = FakeStore()
        snapshot = set_media_stream_proxy_setting(store, {"enabled": True})
        self.assertEqual(store.state[MEDIA_STREAM_PROXY_STATE_KEY], {
            "schema": MEDIA_STREAM_PROXY_STATE_SCHEMA,
            "enabled": True,
        })
        self.assertEqual(sorted(snapshot.keys()), ["enabled", "proxy_detected", "proxy_source"])
        self.assertIn(snapshot["proxy_source"], {"none", "system_registry"})


class MediaGatewayProxyInjectionTests(unittest.TestCase):
    def setUp(self):
        from src.runtime import media_source

        media_source.reset_atrust_presence_cache()
        media_source.configure_media_stream_proxy_provider(None)
        self._media_source = media_source
        self._patches = []

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._media_source.configure_media_stream_proxy_provider(None)

    def _patch_detect(self, url, source):
        patch = unittest.mock.patch.object(
            self._media_source, "detect_windows_system_proxy", return_value=(url, source),
        )
        patch.start()
        self._patches.append(patch)

    def _opened_session(self):
        session = _ProxyProbeSession([FakeResponse(b"ok", headers={"content-length": "2"})])
        gateway = RemoteMediaGateway(lambda: (session, "private-url", {}), lambda: None)
        stream = gateway.open()
        self.assertEqual(b"".join(stream.iter_bytes()), b"ok")
        return session.requests[0][1]["proxies"]

    def test_without_provider_media_stream_stays_explicit_direct(self):
        self.assertEqual(self._opened_session(), {"http": None, "https": None})

    def test_disabled_provider_stays_explicit_direct(self):
        self._media_source.configure_media_stream_proxy_provider(lambda: False)
        self.assertEqual(self._opened_session(), {"http": None, "https": None})

    def test_enabled_with_detected_system_proxy_routes_media_hop_only(self):
        self._media_source.configure_media_stream_proxy_provider(lambda: True)
        self._patch_detect("http://127.0.0.1:8899", "system_registry")
        self.assertEqual(self._opened_session(), {
            "http": "http://127.0.0.1:8899", "https": "http://127.0.0.1:8899",
        })

    def test_enabled_without_detected_proxy_falls_back_to_explicit_direct(self):
        self._media_source.configure_media_stream_proxy_provider(lambda: True)
        self._patch_detect("", "none")
        self.assertEqual(self._opened_session(), {"http": None, "https": None})

    def test_broken_provider_degrades_to_explicit_direct(self):
        def broken():
            raise RuntimeError("synthetic provider failure")

        self._media_source.configure_media_stream_proxy_provider(broken)
        self.assertEqual(self._opened_session(), {"http": None, "https": None})


class ATrustStateTests(unittest.TestCase):
    """三腿合成纯逻辑跨平台可测；快照形状用本机真探（win32 才有意义）。"""

    def test_any_present_signal_wins(self):
        from src.runtime.media_source import ATRUST_PRESENT, synthesize_atrust_state

        for hits in ((True, False, False), (False, True, None), (None, None, True)):
            self.assertEqual(
                synthesize_atrust_state(*hits, windows=True), ATRUST_PRESENT,
            )

    def test_all_clean_legs_conclude_not_installed(self):
        from src.runtime.media_source import ATRUST_NOT_INSTALLED, synthesize_atrust_state

        self.assertEqual(
            synthesize_atrust_state(False, False, False, windows=True),
            ATRUST_NOT_INSTALLED,
        )

    def test_broken_leg_or_non_windows_stays_unknown(self):
        from src.runtime.media_source import ATRUST_UNKNOWN, synthesize_atrust_state

        self.assertEqual(synthesize_atrust_state(None, False, False, windows=True), ATRUST_UNKNOWN)
        self.assertEqual(synthesize_atrust_state(False, False, False, windows=False), ATRUST_UNKNOWN)
        self.assertEqual(synthesize_atrust_state(None, None, None, windows=False), ATRUST_UNKNOWN)

    @unittest.skipUnless(sys.platform == "win32", "本机真探仅在 Windows 有意义")
    def test_live_snapshot_is_closed_set_without_path_leakage(self):
        from src.runtime.media_source import ATRUST_STATES, atrust_presence_snapshot

        snapshot = atrust_presence_snapshot(force=True)
        self.assertEqual(sorted(snapshot.keys()), ["proxy", "signals", "state"])
        self.assertIn(snapshot["state"], ATRUST_STATES)
        self.assertEqual(sorted(snapshot["signals"].keys()), ["directory", "process", "service"])
        self.assertEqual(sorted(snapshot["proxy"].keys()), ["env_configured", "system_configured"])
        blob = json.dumps(snapshot)
        self.assertNotIn("\\", blob, "闭集快照不得携带路径类文本")
        self.assertNotIn("sangfor", blob.lower(), "闭集快照不得携带探测目标明文")
        self.assertNotIn("atrust", blob.lower(), "闭集快照不得携带探测目标明文")

    def test_snapshot_caches_expensive_legs_within_ttl(self):
        from src.runtime import media_source

        media_source.reset_atrust_presence_cache()
        calls = {"process": 0}

        def counting_process():
            calls["process"] += 1
            return False

        with unittest.mock.patch.object(media_source, "_windows_process_hit", counting_process), \
                unittest.mock.patch.object(media_source, "_windows_service_hit", lambda: False), \
                unittest.mock.patch.object(media_source, "_install_dir_hit", lambda: False):
            media_source.atrust_presence_snapshot(force=True)
            media_source.atrust_presence_snapshot()
            self.assertEqual(calls["process"], 1, "TTL 内进程枚举腿必须复用缓存")
        media_source.reset_atrust_presence_cache()


if __name__ == "__main__":
    unittest.main()
