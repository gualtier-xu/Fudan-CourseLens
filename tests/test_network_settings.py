"""网络设置与 P0 路径导演：无凭据探测、TTL 决策缓存、路由代际。

边界闭集（courselens.vpn-connection.v1 + UX 计划 P0.2/P0.3）：
- auto 模式校园服务允许 {直连, 显式本机代理} 两条路径；顺序只由带 TTL 的
  无凭据成对探测改变，默认保持直连优先；
- 决策缓存只在 TTL 内复用；设置变更清空（路由代际递增）；传输失败失效；
- direct/manual 语义不变：只有用户显式允许的一条路径，绝不探测；
- 路由代际持久化且单调递增；legacy app-state（无代际键）读作 0；
- 请求路径只读缓存，绝不发探测（探测只在显式触发点运行）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.network import (
    DEFAULT_PROXY,
    PROXY_DETECT_PROBE_URL,
    PROXY_DETECT_TIMEOUT,
    PROXY_SCAN_PORTS,
    ROUTE_DECISION_TTL_SECONDS,
    ROUTE_HEALTH_COOLDOWN_SECONDS,
    ROUTE_HEALTH_TTL_SECONDS,
    SERVICE_PROBES,
    NetworkSettings,
)
from src.runtime.task_store import TaskStore


def _probe_ok(url, proxy, *, timeout=None):
    return {"healthy": True, "latency_ms": 30 if proxy else 80}


def _probe_dead_direct(url, proxy, *, timeout=None):
    return {"healthy": bool(proxy), "latency_ms": 30 if proxy else None}


def _probe_dead_both(url, proxy, *, timeout=None):
    return {"healthy": False, "latency_ms": None}


class NetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "state.db")
        self.network = NetworkSettings(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_auto_bypass_keeps_direct_default_and_allows_proxy_candidate(self):
        self.assertEqual(SERVICE_PROBES["webvpn"], "https://webvpn.fudan.edu.cn/")
        calls = []

        def probe(url, proxy, *, timeout=None):
            calls.append((url, proxy))
            latency = 30 if proxy else 80
            return {"healthy": True, "latency_ms": latency}

        self.assertEqual(self.network.github_proxy(), DEFAULT_PROXY)
        # auto 模式：校园服务允许 {直连, 显式代理} 两条路径，默认直连优先。
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])
        self.assertEqual(self.network.service_proxies("icourse"), ["", DEFAULT_PROXY])
        self.assertEqual(self.network.service_proxies("github"), [DEFAULT_PROXY, ""])
        self.assertEqual(self.network.service_proxies("deepseek"), [DEFAULT_PROXY, ""])
        with patch.object(NetworkSettings, "_probe", side_effect=probe):
            result = self.network.diagnose()
        self.assertEqual(result["services"]["github"]["route"], "proxy")
        self.assertEqual(result["services"]["webvpn"]["route"], "direct")
        self.assertEqual(result["services"]["icourse"]["route"], "direct")
        self.assertEqual(result["services"]["deepseek"]["route"], "proxy")
        # github/deepseek 各 1 次成对候选探测 + 校园服务各 1 次成对路径探测。
        self.assertEqual(sum(1 for _, proxy in calls if proxy), 4)
        self.assertEqual(sum(1 for _, proxy in calls if not proxy), 4)
        self.assertEqual(self.network.service_proxies("icourse"), ["", DEFAULT_PROXY])

    def test_manual_uses_only_normalized_proxy_and_direct_uses_none(self):
        # C2：盲补 http:// 分支退役，测试改用带 scheme 的合法值（与路由门同一口径）
        settings = self.network.update("manual", "http://127.0.0.1:6268")
        self.assertEqual(settings["proxy_url"], DEFAULT_PROXY)
        calls = []
        with patch.object(
            NetworkSettings,
            "_probe",
            side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
            or {"healthy": True, "latency_ms": 1},
        ):
            self.network.diagnose()
        # manual 模式只有显式代理一条路径：绝不为直连发探测。
        self.assertEqual(calls, [DEFAULT_PROXY] * len(SERVICE_PROBES))

        self.network.update("direct")
        self.assertEqual(self.network.github_proxy(), "")
        self.assertEqual(self.network.service_proxies("icourse"), [""])

    def test_scheme_less_proxy_is_rejected_not_silently_prefixed(self):
        # C2（PB-1）：盲补 http:// 分支退役——保存路径由 v3 路由门
        # （proxy_url_invalid）先拦，update 直呼无 scheme 值也一律拒绝，
        # 绝不静默补前缀（与 test_api_v3_identity_settings D8 钉同口径）。
        for proxy in ("127.0.0.1:6268", "socks5://127.0.0.1:1080", "ftp://x"):
            with self.assertRaises(ValueError):
                self.network.update("manual", proxy)

    def test_non_auto_modes_never_probe_school_paths(self):
        self.network.update("direct")
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok) as probe:
            evidence = self.network.refresh_route_decision("webvpn")
        probe.assert_not_called()
        self.assertEqual(
            evidence, {"decision": "unknown", "direct_ok": None, "proxy_ok": None}
        )
        self.assertEqual(self.network.service_proxies("webvpn"), [""])

    def test_probe_decision_reorders_candidates_and_caches_within_ttl(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok):
            evidence = self.network.refresh_route_decision("webvpn")
        self.assertEqual(
            evidence, {"decision": "direct", "direct_ok": True, "proxy_ok": True}
        )
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])

        # 只有代理可达：决策翻转为代理优先（P0.2 的 TUN 场景证据）。
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            evidence = self.network.refresh_route_decision("icourse")
        self.assertEqual(
            evidence, {"decision": "proxy", "direct_ok": False, "proxy_ok": True}
        )
        self.assertEqual(self.network.service_proxies("icourse"), [DEFAULT_PROXY, ""])

        # TTL 内复用缓存决策：请求路径绝不发探测。
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_both) as probe:
            self.assertEqual(self.network.service_proxies("icourse"), [DEFAULT_PROXY, ""])
            probe.assert_not_called()

        # TTL 过期：缓存失效，回到默认直连优先（无证据时绝不沿用旧决策）。
        with self.network._route_lock:
            self.network._route_decisions["icourse"]["expires_at"] = (
                __import__("time").monotonic() - 0.1
            )
        self.assertEqual(self.network.service_proxies("icourse"), ["", DEFAULT_PROXY])

    def test_ttl_constant_is_bounded_and_shared(self):
        self.assertGreater(ROUTE_DECISION_TTL_SECONDS, 0)
        self.assertLessEqual(ROUTE_DECISION_TTL_SECONDS, 600)

    def test_transport_failure_invalidates_only_the_failed_route(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("webvpn")
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        # 首选的代理路径传输失败：建立在它之上的“代理优先”决策失效，
        # 回到默认直连优先；候选翻转只发生在下一次成对探测。
        self.network.note_route_failure("webvpn", DEFAULT_PROXY)
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])
        # 直连失败不动“代理优先”决策：该决策本就建立在直连已死的证据上。
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("webvpn")
        self.network.note_route_failure("webvpn", "")
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])

    def test_route_generation_is_monotonic_and_legacy_state_reads_zero(self):
        self.assertEqual(self.network.route_generation(), 0)
        self.assertEqual(self.network.snapshot()["route_generation"], 0)
        first = self.network.update("direct")
        second = self.network.update("manual", "http://127.0.0.1:6268")
        third = self.network.update("auto")
        self.assertEqual(first["route_generation"], 1)
        self.assertGreater(second["route_generation"], first["route_generation"])
        self.assertGreater(third["route_generation"], second["route_generation"])
        self.assertEqual(self.network.route_generation(), third["route_generation"])

    def test_update_clears_cached_route_decisions(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("webvpn")
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        self.network.update("auto")
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])

    def test_route_evidence_has_no_url_or_proxy_value(self):
        import json

        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("icourse")
        evidence = json.dumps(self.network.route_evidence("icourse"))
        self.assertNotIn(DEFAULT_PROXY, evidence)
        self.assertNotIn("http", evidence)
        snapshot = json.dumps(self.network.snapshot())
        self.assertNotIn("password", snapshot.casefold())


    def test_route_decision_fresh_gates_the_p2a_preflight(self):
        """P2-A 预检去重门：只有 TTL 内的缓存决策算新鲜；判定路径零 I/O。"""
        self.assertFalse(self.network.route_decision_fresh("webvpn"))
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("webvpn")
        self.assertTrue(self.network.route_decision_fresh("webvpn"))
        # TTL 过期 → 不再新鲜（预检据此重新校准证据）。
        import time as _time

        with self.network._route_lock:
            self.network._route_decisions["webvpn"]["expires_at"] = _time.monotonic() - 0.1
        self.assertFalse(self.network.route_decision_fresh("webvpn"))
        # 不支持的服务名拒绝。
        with self.assertRaises(ValueError):
            self.network.route_decision_fresh("github")

    def test_route_decision_fresh_tracks_each_service_independently(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok):
            self.network.refresh_route_decision("webvpn")
        self.assertTrue(self.network.route_decision_fresh("webvpn"))
        self.assertFalse(self.network.route_decision_fresh("icourse"))


class RouteHealthHysteresisTests(unittest.TestCase):
    """P3.1 路由健康记忆：有界、纯内存、单调钟、冷却迟滞、TTL/代际失效。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "state.db")
        self.network = NetworkSettings(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def _expire_cooldown(self, service: str = "webvpn") -> None:
        import time as _time

        with self.network._route_lock:
            self.network._route_health[service]["cooldown_until"] = (
                _time.monotonic() - 0.1
            )

    def _expire_health_record(self, service: str) -> None:
        import time as _time

        with self.network._route_lock:
            self.network._route_health[service]["updated_at"] = (
                _time.monotonic() - ROUTE_HEALTH_TTL_SECONDS - 1.0
            )

    def test_alternating_transport_failures_flip_at_most_once_per_window(self):
        # 冷态直连传输失败：获准翻转，候选顺序稳定偏向代理。
        self.assertTrue(self.network.note_route_failure("webvpn", ""))
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        # 冷却窗口内反方向的代理失败被抑制：顺序不再翻回直连（迟滞核心）。
        self.assertFalse(self.network.note_route_failure("webvpn", DEFAULT_PROXY))
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        # 冷却过期后回到默认顺序；再次失败才获准下一次（也是一次）翻转。
        self._expire_cooldown()
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])
        self.assertTrue(self.network.note_route_failure("webvpn", ""))
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])

    def test_suppressed_failure_keeps_decision_cache_and_order_stable(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok):
            self.network.refresh_route_decision("webvpn")  # decision=direct
        self.assertTrue(self.network.note_route_failure("webvpn", ""))
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        # 门户探测重新缓存 direct 决策：决策层回到直连优先。探测只在显式
        # 触发点运行，请求路径绝不再探测；冷却仍在压制后续翻转。
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_ok):
            self.network.refresh_route_decision("webvpn")
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])
        # 冷却窗口内的再次直连失败被抑制：决策缓存不被打掉、顺序不再翻转。
        self.assertFalse(self.network.note_route_failure("webvpn", ""))
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])
        self.assertTrue(self.network.route_decision_fresh("webvpn"))
        # 冷却过期后的下一次失败重新获准：清决策并翻回代理优先（迟滞窗口）。
        self._expire_cooldown()
        self.assertTrue(self.network.note_route_failure("webvpn", ""))
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])

    def test_success_records_verified_route_and_clears_cooldown(self):
        self.network.note_route_failure("webvpn", "")
        self.network.note_route_success("webvpn", DEFAULT_PROXY)
        record = self.network._route_health["webvpn"]
        self.assertEqual(record["verified_route"], "proxy")
        self.assertEqual(record["cooldown_until"], 0.0)
        self.assertEqual(record["failed_route"], "")
        # 无探测决策时按最后验证路由排序。
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        # 成功清冷却后，窗口内的下一次失败重新获准（状态清零，不是计数惩罚）。
        self.assertTrue(self.network.note_route_failure("webvpn", ""))

    def test_health_record_expires_after_ttl(self):
        self.network.note_route_failure("icourse", "")
        self._expire_health_record("icourse")
        self.assertEqual(self.network.service_proxies("icourse"), ["", DEFAULT_PROXY])
        # 过期记录被惰性清除；新失败重新武装而不是复活旧记录。
        self.assertNotIn("icourse", self.network._route_health)
        self.assertTrue(self.network.note_route_failure("icourse", ""))
        self.assertEqual(self.network.service_proxies("icourse"), [DEFAULT_PROXY, ""])

    def test_generation_change_invalidates_health_record(self):
        self.network.note_route_failure("webvpn", "")
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        self.network.update("direct")
        # 代际更替（设置变更）清除健康记忆；direct 模式只有一条显式路径。
        self.assertEqual(self.network.service_proxies("webvpn"), [""])
        self.network.update("auto")
        self.assertEqual(self.network._route_health, {})
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])

    def test_note_resume_bumps_generation_and_preserves_settings(self):
        self.network.update("manual", "http://127.0.0.1:6268")
        self.network.note_route_failure("webvpn", DEFAULT_PROXY)
        self.assertEqual(self.network.note_resume(), 2)
        value = self.store.get_app_state("network_settings", {})
        self.assertEqual(value["mode"], "manual")
        self.assertEqual(value["proxy_url"], DEFAULT_PROXY)
        self.assertEqual(self.network.route_generation(), 2)
        self.assertEqual(self.network._route_health, {})
        # 连续恢复事件继续单调递增。
        self.assertEqual(self.network.note_resume(), 3)

    def test_note_resume_clears_cached_probe_decisions(self):
        with patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_direct):
            self.network.refresh_route_decision("webvpn")
        self.assertEqual(self.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""])
        before = self.network.route_generation()
        self.network.note_resume()
        self.assertEqual(self.network.route_generation(), before + 1)
        self.assertEqual(self.network._route_decisions, {})
        self.assertEqual(self.network.service_proxies("webvpn"), ["", DEFAULT_PROXY])

    def test_health_record_is_memory_only_and_closed_to_campus_services(self):
        self.network.note_route_failure("webvpn", "")
        self.network.note_route_success("icourse", "")
        persisted = str(self.store.get_app_state("network_settings", {}))
        self.assertNotIn("cooldown", persisted)
        self.assertNotIn("verified_route", persisted)
        self.assertEqual(set(self.network._route_health), {"webvpn", "icourse"})
        with self.assertRaises(ValueError):
            self.network.note_route_failure("github", "")
        with self.assertRaises(ValueError):
            self.network.note_route_success("github", "")
        with self.assertRaises(ValueError):
            self.network.clear_route_health("github")

    def test_health_constants_are_bounded(self):
        self.assertGreater(ROUTE_HEALTH_COOLDOWN_SECONDS, 0)
        self.assertGreaterEqual(ROUTE_HEALTH_TTL_SECONDS, ROUTE_HEALTH_COOLDOWN_SECONDS)
        self.assertLessEqual(ROUTE_HEALTH_TTL_SECONDS, 3600)


class DetectProxyTests(unittest.TestCase):
    """PROXY-AUTODETECT-1：本机代理自动检测——闭集、有界、只读。

    候选按序（系统代理一处 → 闭集端口 5 个）、每个候选至多一次真实探测
    （healthy=status<500）、首个通过者胜、全败 not_found；返回闭集且绝不
    触碰代际/持久化状态。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "state.db")
        self.network = NetworkSettings(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_port_set_is_closed_and_probe_target_is_github_rate_limit(self):
        self.assertEqual(PROXY_SCAN_PORTS, (6268, 7890, 7891, 10809, 8888))
        self.assertEqual(PROXY_DETECT_PROBE_URL, SERVICE_PROBES["github"])
        self.assertEqual(PROXY_DETECT_PROBE_URL, "https://api.github.com/rate_limit")
        self.assertEqual(PROXY_DETECT_TIMEOUT, (2.0, 3.0))

    def test_scan_candidates_probed_in_order_and_first_healthy_wins(self):
        calls = []

        def probe(url, proxy, *, timeout=None):
            calls.append((url, proxy, timeout))
            return {"healthy": proxy.endswith(":7890"), "latency_ms": 1}

        with patch.object(self.network, "_system_proxy_port", return_value=None), \
             patch.object(NetworkSettings, "_probe", side_effect=probe):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "found", "source": "scan", "port": 7890})
        self.assertEqual(
            [proxy.rsplit(":", 1)[1] for _, proxy, _ in calls], ["6268", "7890"],
        )
        for url, _, timeout in calls:
            self.assertEqual(url, PROXY_DETECT_PROBE_URL)
            self.assertEqual(timeout, PROXY_DETECT_TIMEOUT)

    def test_system_proxy_source_is_probed_first_and_wins(self):
        calls = []
        with patch.object(self.network, "_system_proxy_port", return_value=7891), \
             patch.object(
                 NetworkSettings, "_probe",
                 side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
                 or {"healthy": True, "latency_ms": 5},
             ):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "found", "source": "system", "port": 7891})
        self.assertEqual(calls, ["http://127.0.0.1:7891"])

    def test_system_source_failure_degrades_to_scan_only(self):
        calls = []

        def broken_reader():
            raise RuntimeError("registry unavailable")

        with patch.object(self.network, "_system_proxy_port", side_effect=broken_reader), \
             patch.object(
                 NetworkSettings, "_probe",
                 side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
                 or {"healthy": False, "latency_ms": None},
             ):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "not_found", "source": None, "port": None})
        self.assertEqual(len(calls), len(PROXY_SCAN_PORTS))

    def test_all_candidates_dead_returns_not_found_with_bounded_probes(self):
        calls = []
        with patch.object(self.network, "_system_proxy_port", return_value=6268), \
             patch.object(
                 NetworkSettings, "_probe",
                 side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
                 or {"healthy": False, "latency_ms": None},
             ):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "not_found", "source": None, "port": None})
        # 系统候选 1 次 + 闭集端口 5 次：总数严格有界（至多 6 次探测）。
        self.assertEqual(len(calls), 1 + len(PROXY_SCAN_PORTS))

    def test_own_server_port_is_excluded_from_candidates(self):
        """N5FE-P4：自家 HTTP 服务端口（默认 6268）必须从候选里剔除——
        旧逻辑把 CourseLens 自身对探测请求的响应误判为「检测到代理」。"""
        calls = []
        with patch.object(self.network, "_system_proxy_port", return_value=6268),              patch.object(
                 NetworkSettings, "_probe",
                 side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
                 or {"healthy": True, "latency_ms": 5},
             ):
            result = self.network.detect_proxy(own_port=6268)
        # 自家端口被跳过（system 与 scan 两处都不出现）；探测直接命中下一个候选
        self.assertEqual(result, {"status": "found", "source": "scan", "port": 7890})
        self.assertNotIn("http://127.0.0.1:6268", calls)

    def test_own_port_skip_falls_through_to_real_proxy(self):
        """own_port 与首个候选相同时不应吃掉「全部失败」语义：其余候选照常探测。"""
        calls = []
        with patch.object(self.network, "_system_proxy_port", return_value=None),              patch.object(
                 NetworkSettings, "_probe",
                 side_effect=lambda url, proxy, *, timeout=None: calls.append(proxy)
                 or {"healthy": False, "latency_ms": None},
             ):
            result = self.network.detect_proxy(own_port=6268)
        self.assertEqual(result, {"status": "not_found", "source": None, "port": None})
        self.assertEqual(
            [proxy.rsplit(":", 1)[1] for proxy in calls],
            [str(port) for port in PROXY_SCAN_PORTS if port != 6268],
        )

    def test_pac_configured_with_dead_candidates_reports_pac_detected(self):
        """C3-R6：PAC 自动代理（AutoConfigURL）+ 端口全灭 → pac_detected，
        给 PAC 学生明确指引而非「未找到」；绝不读取或求值 PAC 脚本。"""
        with patch.object(self.network, "_system_proxy_port", return_value=None), \
             patch.object(self.network, "_system_autoconfig_url", return_value=True), \
             patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_both):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "pac_detected", "source": "system_pac", "port": None})

    def test_no_pac_keeps_not_found_within_closed_status_set(self):
        with patch.object(self.network, "_system_proxy_port", return_value=None), \
             patch.object(self.network, "_system_autoconfig_url", return_value=False), \
             patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_both):
            result = self.network.detect_proxy()
        self.assertEqual(result, {"status": "not_found", "source": None, "port": None})
        # 闭集状态白名单：pac_detected 只在既有三值上追加
        self.assertIn(result["status"], {"found", "not_found", "error", "pac_detected"})

    def test_detect_is_idempotent_read_only_and_never_touches_generation(self):
        before = self.store.get_app_state("network_settings", {})
        with patch.object(self.network, "_system_proxy_port", return_value=None), \
             patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_both):
            first = self.network.detect_proxy()
            second = self.network.detect_proxy()
        self.assertEqual(first, second)
        self.assertEqual(first, {"status": "not_found", "source": None, "port": None})
        self.assertEqual(self.network.route_generation(), 0)
        self.assertEqual(self.store.get_app_state("network_settings", {}), before)

    def test_detect_result_is_closed_set_without_url_or_proxy_text(self):
        import json

        with patch.object(self.network, "_system_proxy_port", return_value=7890), \
             patch.object(NetworkSettings, "_probe", side_effect=_probe_dead_both):
            result = self.network.detect_proxy()
        self.assertEqual(set(result), {"status", "source", "port"})
        text = json.dumps(result)
        self.assertNotIn("http", text)
        self.assertNotIn(DEFAULT_PROXY, text)

    def test_system_proxy_server_parsing_accepts_only_local_http_forms(self):
        parse = NetworkSettings._parse_system_proxy_server
        self.assertEqual(parse("127.0.0.1:7890"), 7890)
        self.assertEqual(parse("localhost:8080"), 8080)
        self.assertEqual(parse(" 127.0.0.1:6268 "), 6268)
        # 复合形式：http= 优先；仅 https= 也可作为探测候选。
        self.assertEqual(parse("http=127.0.0.1:7890;https=127.0.0.1:7891"), 7890)
        self.assertEqual(parse("https=localhost:7891"), 7891)
        for bad in (
            "", ";", "=127.0.0.1:7890", "127.0.0.1", "localhost",
            "proxy.example.com:8080", "10.0.0.8:7890", "[::1]:7890",
            "socks5://127.0.0.1:1080", "https=socks://127.0.0.1:1080",
            "http=proxy.lan:8080", "127.0.0.1:not-a-port", "127.0.0.1:0",
            "127.0.0.1:70000",
        ):
            self.assertIsNone(parse(bad), bad)


if __name__ == "__main__":
    unittest.main()
