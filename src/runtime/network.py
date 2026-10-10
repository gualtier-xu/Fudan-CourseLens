"""Per-service network routing for mainland-China desktop clients."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import requests


DEFAULT_PROXY = "http://127.0.0.1:6268"
SERVICE_PROBES = {
    "webvpn": "https://webvpn.fudan.edu.cn/",
    "icourse": "https://icourse.fudan.edu.cn/",
    "github": "https://api.github.com/rate_limit",
    "deepseek": "https://api.deepseek.com/",
}

# --- P0.2 路径导演（courselens.vpn-connection.v1 的生产侧路由证据） ----------
# auto 模式下校园服务允许 {直连, 显式本机代理} 两条路径；探测只在显式触发点
# 运行（启动探测 / 诊断 / 设置变更后），请求路径只读取带 TTL 的缓存决策。
ROUTE_DECISION_TTL_SECONDS = 300.0
ROUTE_PROBE_TIMEOUT = (2.0, 3.0)
_ROUTE_SERVICES = frozenset({"webvpn", "icourse"})

# --- 本机代理自动检测（onboarding 引导卡；无凭据、闭集、有界） ---------------
# 候选来源按序：① Windows 系统代理（HKCU Internet Settings 的 ProxyEnable/
# ProxyServer，仅接受 localhost/127.x 的 HTTP 代理形式）→ ② 闭集常见 HTTP
# 端口（6268, 7890, 7891, 10809, 8888）。每个候选至多一次 _probe 真实验证
# （healthy = status<500），首个通过者胜；全部失败 not_found。总共至多 6 次
# 探测、每次 (2,3) 超时；返回闭集 {status, source, port}，绝不返回或记录
# 自由文本；不做 SOCKS（_normalize_proxy 语义不变），不动 trust_env=False
# 纪律，不新增任何常驻线程。
PROXY_DETECT_PROBE_URL = SERVICE_PROBES["github"]
PROXY_DETECT_TIMEOUT = (2.0, 3.0)
PROXY_SCAN_PORTS = (6268, 7890, 7891, 10809, 8888)
_WINDOWS_INTERNET_SETTINGS_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# --- P3.1 路由健康记忆（有界、纯内存、单调钟；绝不持久化、绝不进合同快照） --
# 每个校园服务恰好一条记录（固定大小 2）；传输失败进入短冷却并让候选顺序
# 稳定地偏向另一条路径（迟滞：直连/代理交替失败不再逐请求翻转）；记录在
# TTL、代际更替或显式清除后失效。冷却优先级高于探测决策：票据级传输失败
# 是比门户探测更强的路径证据（门户可达 ≠ 目录身份可用）。
ROUTE_HEALTH_TTL_SECONDS = 900.0
ROUTE_HEALTH_COOLDOWN_SECONDS = 60.0

# --- WebVPN 登录 last-success 候选记忆（跨重启持久化的闭集类别） ------------
# 独立 app-state 键（不扩展 network_settings 的已验证 schema）；只存 route/
# transport 类别，绝不含代理 URL、凭据或账号数据。transport 闭集镜像
# src/api/webvpn.py 的 TICKET_TRANSPORTS（runtime 层不反向依赖 api 层）；
# 代际取自 network_settings 的路由代际，代际推进后旧记忆惰性弃用（不清理）。
_WEBVPN_TICKET_MEMORY_KEY = "webvpn_ticket_memory"
_WEBVPN_TICKET_MEMORY_ROUTES = frozenset({"direct", "proxy"})
_WEBVPN_TICKET_MEMORY_TRANSPORTS = frozenset({"curl_h2", "curl_h1"})

# --- courselens.vpn-connection.v1 参考校验器（自合同门测试逐字提升） --------
# 来源：tests/test_vpn_connection_contract.py（2026-09-13 冻结 v1）。
# 标准库实现、确定性、不读墙钟；本文档为 docs/vpn-connection-contract-v1.md。

VPN_CONNECTION_SCHEMA = "courselens.vpn-connection.v1"

VPN_CONNECTION_STATES = frozenset(
    {
        "off",
        "checking",
        "ready",
        "login_required",
        "reauthenticating",
        "network_unavailable",
        "challenge_required",
        "expired",
        "degraded",
    }
)
VPN_CONNECTION_NETWORK_PATHS = frozenset({"direct", "local_proxy", "unknown"})
VPN_CONNECTION_SCHOOL_ROUTES = frozenset({"webvpn", "icourse_direct", "mixed", "unknown"})
VPN_CONNECTION_REASONS = frozenset(
    {
        "cold_start",
        "direct_ok",
        "proxy_fallback",
        "session_expired",
        "possible_tun_interference",
        "credentials_rejected",
        "challenge",
        "service_unavailable",
        "unknown",
    }
)
VPN_CONNECTION_ACTIONS = frozenset(
    {
        "login",
        "reauthenticate",
        "check-network",
        "retry",
        "open-settings",
        "close-tun-and-retry",
    }
)
VPN_CONNECTION_SERVICE_NAMES = ("webvpn", "icourse")
VPN_CONNECTION_SERVICE_STATES = frozenset({"unknown", "checking", "ready", "unavailable"})
VPN_CONNECTION_SERVICE_ROUTES = frozenset({"direct", "local_proxy", "unknown"})
VPN_CONNECTION_SERVICE_FIELDS = ("state", "route", "verified")

VPN_CONNECTION_TOP_FIELDS = (
    "schema",
    "state",
    "network_path",
    "school_route",
    "reason",
    "observed_at",
    "expires_at",
    "retry_after",
    "actions",
    "generation",
    "services",
)
_VPN_CONNECTION_READY_LIKE_STATES = frozenset({"ready", "degraded"})

_VPN_CONNECTION_MIN_EPOCH = 1
_VPN_CONNECTION_MAX_EPOCH = 4102444800  # 2100-01-01T00:00:00Z
_VPN_CONNECTION_MAX_SAFE_INTEGER = 2**53 - 1


def _vpn_connection_check_epoch_field(errors, path, value):
    """Validate one epoch-seconds field. Returns (is_valid_int, value_or_none)."""
    if isinstance(value, bool):
        errors.append(("bad_type", path))
        return False, None
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(("non_finite_number", path))
        else:
            errors.append(("non_integer_number", path))
        return False, None
    if not isinstance(value, int):
        errors.append(("bad_type", path))
        return False, None
    if value < 0:
        errors.append(("negative_number", path))
        return False, None
    if value == 0 or value > _VPN_CONNECTION_MAX_EPOCH:
        errors.append(("bad_time", path))
        return False, None
    return True, value


def _vpn_connection_check_enum_field(errors, path, value, allowed):
    if not isinstance(value, str):
        errors.append(("bad_type", path))
        return False
    if value not in allowed:
        errors.append(("bad_enum", path))
        return False
    return True


def validate_vpn_connection_snapshot(snapshot):
    """Return all contract violations as (code, json_path) pairs, in fixed order."""
    errors = []
    if not isinstance(snapshot, dict):
        errors.append(("not_an_object", "$"))
        return errors

    present = set(snapshot)
    for field in VPN_CONNECTION_TOP_FIELDS:
        if field not in present:
            errors.append(("missing_field", "$." + field))
    for field in sorted(present - set(VPN_CONNECTION_TOP_FIELDS)):
        errors.append(("unknown_field", "$." + field))

    # Per-field checks, canonical field order.
    if "schema" in present:
        schema_value = snapshot["schema"]
        if not isinstance(schema_value, str):
            errors.append(("bad_type", "$.schema"))
        elif schema_value != VPN_CONNECTION_SCHEMA:
            errors.append(("bad_schema", "$.schema"))

    state_value = snapshot.get("state")
    state_ok = False
    if "state" in present:
        state_ok = _vpn_connection_check_enum_field(
            errors, "$.state", state_value, VPN_CONNECTION_STATES
        )

    if "network_path" in present:
        _vpn_connection_check_enum_field(
            errors, "$.network_path", snapshot["network_path"], VPN_CONNECTION_NETWORK_PATHS
        )

    if "school_route" in present:
        _vpn_connection_check_enum_field(
            errors, "$.school_route", snapshot["school_route"], VPN_CONNECTION_SCHOOL_ROUTES
        )

    if "reason" in present:
        _vpn_connection_check_enum_field(
            errors, "$.reason", snapshot["reason"], VPN_CONNECTION_REASONS
        )

    observed_ok = False
    observed_value = None
    if "observed_at" in present:
        observed_ok, observed_value = _vpn_connection_check_epoch_field(
            errors, "$.observed_at", snapshot["observed_at"]
        )

    expires_ok = False
    expires_value = snapshot.get("expires_at")
    if "expires_at" in present and expires_value is not None:
        expires_ok, expires_value = _vpn_connection_check_epoch_field(
            errors, "$.expires_at", expires_value
        )

    retry_ok = False
    retry_value = snapshot.get("retry_after")
    if "retry_after" in present and retry_value is not None:
        retry_ok, retry_value = _vpn_connection_check_epoch_field(
            errors, "$.retry_after", retry_value
        )

    actions_value = snapshot.get("actions")
    actions_ok = False
    if "actions" in present:
        if not isinstance(actions_value, list):
            errors.append(("bad_type", "$.actions"))
        else:
            actions_ok = True
            seen = set()
            for index, item in enumerate(actions_value):
                path = "$.actions.%d" % index
                if not isinstance(item, str):
                    errors.append(("bad_type", path))
                    continue
                if item not in VPN_CONNECTION_ACTIONS:
                    errors.append(("bad_enum", path))
                    continue
                if item in seen:
                    errors.append(("duplicate_action", path))
                    continue
                seen.add(item)

    if "generation" in present:
        generation_value = snapshot["generation"]
        if isinstance(generation_value, bool) or not isinstance(generation_value, int):
            errors.append(("bad_type", "$.generation"))
        elif generation_value < 0:
            errors.append(("negative_number", "$.generation"))
        elif generation_value > _VPN_CONNECTION_MAX_SAFE_INTEGER:
            errors.append(("number_too_large", "$.generation"))

    if "services" in present:
        services = snapshot["services"]
        if not isinstance(services, dict):
            errors.append(("bad_type", "$.services"))
        else:
            for name in VPN_CONNECTION_SERVICE_NAMES:
                if name not in services:
                    errors.append(("missing_field", "$.services." + name))
            for name in sorted(services):
                if name not in VPN_CONNECTION_SERVICE_NAMES:
                    errors.append(("unknown_service", "$.services." + name))
            for name in VPN_CONNECTION_SERVICE_NAMES:
                if name not in services:
                    continue
                service = services[name]
                if not isinstance(service, dict):
                    errors.append(("bad_type", "$.services." + name))
                    continue
                for key in sorted(service):
                    if key not in VPN_CONNECTION_SERVICE_FIELDS:
                        errors.append(("unknown_field", "$.services.%s.%s" % (name, key)))
                if "state" in service:
                    _vpn_connection_check_enum_field(
                        errors, "$.services.%s.state" % name, service["state"],
                        VPN_CONNECTION_SERVICE_STATES,
                    )
                if "route" in service:
                    _vpn_connection_check_enum_field(
                        errors, "$.services.%s.route" % name, service["route"],
                        VPN_CONNECTION_SERVICE_ROUTES,
                    )
                if "verified" in service and not isinstance(service["verified"], bool):
                    errors.append(("bad_type", "$.services.%s.verified" % name))

    # Cross-field rules, guarded so invalid prerequisites add no cascade errors.
    if state_ok:
        if state_value in _VPN_CONNECTION_READY_LIKE_STATES:
            if "expires_at" in present and expires_value is None:
                errors.append(("expires_at_required", "$.expires_at"))
            elif expires_ok and observed_ok and expires_value < observed_value:
                errors.append(("expires_before_observed", "$.expires_at"))
        else:
            if "expires_at" in present and expires_value is not None:
                errors.append(("expires_at_forbidden", "$.expires_at"))

    if "retry_after" in present and retry_value is not None and retry_ok:
        if actions_ok and "retry" not in actions_value:
            errors.append(("retry_after_without_retry", "$.retry_after"))
        if observed_ok and retry_value < observed_value:
            errors.append(("retry_before_observed", "$.retry_after"))

    return errors


class NetworkSettings:
    def __init__(self, task_store):
        self.task_store = task_store
        self._route_lock = threading.Lock()
        # service -> {"decision": "direct"|"proxy", "direct_ok": bool,
        #             "proxy_ok": bool, "expires_at": monotonic}
        self._route_decisions: dict[str, dict[str, Any]] = {}
        # P3.1：service -> {"generation", "updated_at", "verified_route",
        #                      "failed_route", "cooldown_until"}（纯内存，见上）
        self._route_health: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        saved = dict(self.task_store.get_app_state("network_settings", {}) or {})
        mode = str(saved.get("mode") or "auto")
        if mode not in {"auto", "direct", "manual"}:
            mode = "auto"
        return {
            "mode": mode,
            "proxy_url": str(saved.get("proxy_url") or DEFAULT_PROXY),
            "routes": dict(saved.get("routes") or {}),
            "route_generation": self.route_generation(),
        }

    def route_generation(self) -> int:
        """Persisted, monotonic route generation (P0.3); 0 on legacy state."""
        saved = self.task_store.get_app_state("network_settings", {}) or {}
        try:
            generation = int(saved.get("route_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        return max(0, generation)

    def load_ticket_memory(self) -> dict[str, str] | None:
        """读取登录 last-success 候选记忆；闭集校验 + 代际匹配，否则 None。

        惰性失效：坏值或代际不匹配一律当作无记忆处理，不写回、不清理。
        """
        saved = self.task_store.get_app_state(_WEBVPN_TICKET_MEMORY_KEY, {}) or {}
        if not isinstance(saved, dict):
            return None
        route = saved.get("route")
        transport = saved.get("transport")
        if (
            route not in _WEBVPN_TICKET_MEMORY_ROUTES
            or transport not in _WEBVPN_TICKET_MEMORY_TRANSPORTS
        ):
            return None
        try:
            generation = int(saved.get("generation"))
        except (TypeError, ValueError):
            return None
        if generation != self.route_generation():
            return None
        return {"route": route, "transport": transport}

    def save_ticket_memory(self, route: str, transport: str) -> None:
        """持久化一次已验证的 last-success 候选（类别闭集，调用方尽力而为）。"""
        if route not in _WEBVPN_TICKET_MEMORY_ROUTES:
            raise ValueError("ticket memory route is unsupported")
        if transport not in _WEBVPN_TICKET_MEMORY_TRANSPORTS:
            raise ValueError("ticket memory transport is unsupported")
        self.task_store.set_app_state(
            _WEBVPN_TICKET_MEMORY_KEY,
            {
                "route": route,
                "transport": transport,
                "generation": self.route_generation(),
                "saved_at": time.time(),
            },
        )

    def update(self, mode: str, proxy_url: str = "") -> dict[str, Any]:
        mode = str(mode or "").strip().lower()
        if mode not in {"auto", "direct", "manual"}:
            raise ValueError("网络模式必须是自动、直连或手动代理")
        proxy = self._normalize_proxy(proxy_url or DEFAULT_PROXY)
        saved = self.task_store.get_app_state("network_settings", {}) or {}
        try:
            generation = int(saved.get("route_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        # P0.3：mode/proxy 变更是路由代际事件——单调递增并清除缓存决策。
        with self._route_lock:
            self._route_decisions.clear()
            self._route_health.clear()
        value = {
            "mode": mode,
            "proxy_url": proxy,
            "routes": {},
            "route_generation": max(0, generation) + 1,
        }
        self.task_store.set_app_state("network_settings", value)
        return self.snapshot()

    def github_proxy(self) -> str:
        return self.service_proxies("github")[0]

    def service_proxies(self, service: str) -> list[str]:
        if service not in SERVICE_PROBES:
            raise ValueError("network service is unsupported")
        value = self.snapshot()
        if value["mode"] == "direct":
            return [""]
        if value["mode"] == "manual":
            return [value["proxy_url"]]
        if service in _ROUTE_SERVICES:
            # P0.2：auto 模式校园服务允许 {直连, 显式本机代理} 两条路径；
            # 顺序只由带 TTL 的无凭据探测决策改变，默认保持直连优先的既有
            # 绕过语义（也避免预热票据传输先消耗一次代理连接超时）。
            # P3.1：无探测决策时按路由健康记忆排序——活跃冷却优先（迟滞），
            # 其次最后一次验证过的路由；请求路径只读内存，绝不发探测。
            decision = self._route_decision_cached(service)
            if not decision:
                decision = self._route_health_preference(service, value["route_generation"])
            return (
                [value["proxy_url"], ""]
                if decision == "proxy"
                else ["", value["proxy_url"]]
            )
        default_route = "proxy" if service in {"github", "deepseek"} else "direct"
        route = str(value.get("routes", {}).get(service) or default_route)
        return ["", value["proxy_url"]] if route == "direct" else [value["proxy_url"], ""]

    # --- 路径导演：无凭据探测、TTL 缓存、失效（P0.2/P0.3） -------------------

    def refresh_route_decision(self, service: str) -> dict[str, Any]:
        """Probe both allowed school paths without credentials and cache it."""
        if service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        value = self.snapshot()
        if value["mode"] != "auto":
            return self._empty_route_evidence()
        proxy_url = value["proxy_url"]
        url = SERVICE_PROBES[service]
        direct_result = self._probe(url, "", timeout=ROUTE_PROBE_TIMEOUT)
        proxy_result = self._probe(url, proxy_url, timeout=ROUTE_PROBE_TIMEOUT)
        direct_ok = bool(direct_result.get("healthy"))
        proxy_ok = bool(proxy_result.get("healthy"))
        decision = "proxy" if (proxy_ok and not direct_ok) else "direct"
        with self._route_lock:
            self._route_decisions[service] = {
                "decision": decision,
                "direct_ok": direct_ok,
                "proxy_ok": proxy_ok,
                "expires_at": time.monotonic() + ROUTE_DECISION_TTL_SECONDS,
            }
        return self.route_evidence(service)

    def route_evidence(self, service: str) -> dict[str, Any]:
        """Closed-set probe evidence for the connection snapshot; no URLs."""
        if service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        with self._route_lock:
            cached = self._route_decisions.get(service)
        if not cached or cached.get("expires_at", 0.0) <= time.monotonic():
            return self._empty_route_evidence()
        return {
            "decision": "proxy" if cached.get("decision") == "proxy" else "direct",
            "direct_ok": bool(cached.get("direct_ok")),
            "proxy_ok": bool(cached.get("proxy_ok")),
        }

    def note_route_failure(self, service: str, proxy_url: str = "") -> bool:
        """Invalidate a cached decision whose route just failed at transport level.

        P3.1 迟滞：返回 True 表示本次失败获准翻转（清决策 + 进入短冷却）；
        返回 False 表示冷却窗口内的重复失败被抑制——只计数，不再翻转候选
        顺序，直连/代理交替失败因此在窗口内至多翻转一次。
        """
        if service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        failed = "proxy" if proxy_url else "direct"
        now = time.monotonic()
        generation = self.route_generation()
        with self._route_lock:
            self._expire_route_health_locked(now)
            record = self._route_health.get(service)
            if (
                record is not None
                and record.get("generation") == generation
                and now < float(record.get("cooldown_until") or 0.0)
            ):
                return False
            self._route_health[service] = {
                "generation": generation,
                "updated_at": now,
                "verified_route": "",
                "failed_route": failed,
                "cooldown_until": now + ROUTE_HEALTH_COOLDOWN_SECONDS,
            }
            cached = self._route_decisions.get(service)
            if cached and cached.get("decision") == failed:
                self._route_decisions.pop(service, None)
        return True

    def note_route_success(self, service: str, proxy_url: str = "") -> None:
        """Record the last verified route (P3.1); clears any active cooldown."""
        if service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        route = "proxy" if proxy_url else "direct"
        with self._route_lock:
            self._route_health[service] = {
                "generation": self.route_generation(),
                "updated_at": time.monotonic(),
                "verified_route": route,
                "failed_route": "",
                "cooldown_until": 0.0,
            }

    def clear_route_health(self, service: str = "") -> None:
        """Drop the health record (identity mismatch / terminal auth failure)."""
        if service and service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        with self._route_lock:
            if service:
                self._route_health.pop(service, None)
            else:
                self._route_health.clear()

    def note_resume(self) -> int:
        """Treat host sleep/resume as a route-generation event (P3.1).

        复用 P0.3 代际钩子：单调递增持久化代际并清空缓存的探测决策与路由
        健康记忆；模式/代理/诊断路由等其他键原样保留。旧客户端只在下一个
        受保护动作被重建，在途请求绝不被打断。
        """
        saved = dict(self.task_store.get_app_state("network_settings", {}) or {})
        try:
            generation = int(saved.get("route_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        value = dict(saved)
        value["route_generation"] = max(0, generation) + 1
        with self._route_lock:
            self._route_decisions.clear()
            self._route_health.clear()
        self.task_store.set_app_state("network_settings", value)
        return max(0, generation) + 1

    def _expire_route_health_locked(self, now: float | None = None) -> None:
        """Lazy TTL expiry for health records; must hold _route_lock."""
        moment = time.monotonic() if now is None else now
        for name in list(self._route_health):
            record = self._route_health[name]
            updated_at = float(record.get("updated_at") or 0.0)
            if record.get("generation") != self.route_generation() or (
                moment - updated_at >= ROUTE_HEALTH_TTL_SECONDS
            ):
                self._route_health.pop(name, None)

    def _route_health_preference(self, service: str, generation: int) -> str:
        """Ordering preference from the health record: "" / "direct" / "proxy"."""
        with self._route_lock:
            self._expire_route_health_locked()
            record = self._route_health.get(service)
        if not record or record.get("generation") != generation:
            return ""
        now = time.monotonic()
        cooldown_until = float(record.get("cooldown_until") or 0.0)
        if now < cooldown_until:
            failed = str(record.get("failed_route") or "")
            if failed in {"direct", "proxy"}:
                return "proxy" if failed == "direct" else "direct"
            return ""
        verified = str(record.get("verified_route") or "")
        return verified if verified in {"direct", "proxy"} else ""

    def _route_decision_cached(self, service: str) -> str:
        with self._route_lock:
            cached = self._route_decisions.get(service)
        if cached and cached.get("expires_at", 0.0) > time.monotonic():
            return "proxy" if cached.get("decision") == "proxy" else "direct"
        return ""

    def route_decision_fresh(self, service: str) -> bool:
        """Whether a TTL-valid cached decision exists（P2-A 按需预检的去重门）."""
        if service not in _ROUTE_SERVICES:
            raise ValueError("network service is unsupported")
        return bool(self._route_decision_cached(service))

    @staticmethod
    def _empty_route_evidence() -> dict[str, Any]:
        return {"decision": "unknown", "direct_ok": None, "proxy_ok": None}

    def diagnose(self) -> dict[str, Any]:
        settings = self.snapshot()
        routes: dict[str, str] = {}
        services: dict[str, Any] = {}
        for service, url in SERVICE_PROBES.items():
            candidates = self.service_proxies(service)
            if service in _ROUTE_SERVICES and settings["mode"] == "auto":
                # 校园服务：一次成对探测同时喂决策缓存与诊断结果（避免双份探测）。
                evidence = self.refresh_route_decision(service)
                best: dict[str, Any] | None = None
                for proxy in candidates:
                    result = {
                        "healthy": bool(evidence["proxy_ok"] if proxy else evidence["direct_ok"]),
                        "latency_ms": None,
                    }
                    if result["healthy"] and best is None:
                        best = {**result, "route": "proxy" if proxy else "direct"}
                if best is None:
                    best = {"healthy": False, "latency_ms": None, "route": "unavailable"}
            else:
                best = None
                for proxy in candidates:
                    result = self._probe(url, proxy)
                    if result["healthy"] and best is None:
                        best = {**result, "route": "proxy" if proxy else "direct"}
                if best is None:
                    best = {"healthy": False, "latency_ms": None, "route": "unavailable"}
            services[service] = best
            routes[service] = str(best["route"])
        settings["routes"] = routes
        self.task_store.set_app_state("network_settings", settings)
        return {"settings": self.snapshot(), "services": services, "checked_at": time.time()}

    # --- 本机代理自动检测（闭集、有界、只读，绝不触碰代际与持久化状态） -----

    def detect_proxy(self, *, own_port: int | None = None) -> dict[str, Any]:
        """Probe system proxy then a closed port set; return a closed-status dict.

        候选按序：系统代理一处（仅 localhost/127.x 的 HTTP 代理形式）→ 闭集
        常见端口 5 个；每个候选用既有 _probe 真实验证，首个通过者胜；全部
        失败 not_found。系统配置了 PAC 自动代理（AutoConfigURL）而端口探测
        全灭时返回 pac_detected（C3-R6：给 PAC 学生明确指引而非「未找到」；
        绝不做 PAC 求值）。``own_port`` 是本客户端自身 HTTP 服务端口：
        它在候选里会被跳过（N5FE-P4：6268 既是默认候选端口也是自家端口，
        旧逻辑曾把自家响应误判为「检测到代理」——自指假阳性）。只读：不改
        mode/proxy/代际，不写任何 app state。
        """
        candidates: list[tuple[str, int]] = []
        try:
            system_port = self._system_proxy_port()
        except Exception:
            system_port = None
        if system_port is not None and system_port != own_port:
            candidates.append(("system", system_port))
        candidates.extend(
            ("scan", port) for port in PROXY_SCAN_PORTS if port != own_port
        )
        try:
            for source, port in candidates:
                result = self._probe(
                    PROXY_DETECT_PROBE_URL,
                    f"http://127.0.0.1:{port}",
                    timeout=PROXY_DETECT_TIMEOUT,
                )
                if result.get("healthy"):
                    return {"status": "found", "source": source, "port": int(port)}
        except Exception:
            return {"status": "error", "source": None, "port": None}
        try:
            if self._system_autoconfig_url():
                return {"status": "pac_detected", "source": "system_pac", "port": None}
        except Exception:
            pass
        return {"status": "not_found", "source": None, "port": None}

    def _system_autoconfig_url(self) -> bool:
        """Read HKCU AutoConfigURL presence (PAC auto-proxy configured?).

        坏值/缺项/空串/非 Windows 一律 False（容错对齐 _system_proxy_port）。
        只判定「配置了 PAC」，绝不读取、保存或求值 PAC 脚本内容。
        """
        try:
            import winreg
        except ImportError:
            return False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_INTERNET_SETTINGS_PATH)
            try:
                value, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            finally:
                key.Close()
        except OSError:
            return False
        return bool(str(value or "").strip())

    def _system_proxy_port(self) -> int | None:
        """Read HKCU ProxyEnable/ProxyServer into a local HTTP proxy port.

        坏值/缺项/非 Windows 一律 None（容错，绝不抛出给调用方之外的层）。
        """
        try:
            import winreg
        except ImportError:
            return None
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_INTERNET_SETTINGS_PATH)
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            finally:
                key.Close()
        except OSError:
            return None
        if not enabled:
            return None
        return self._parse_system_proxy_server(str(server or ""))

    @staticmethod
    def _parse_system_proxy_server(server: str) -> int | None:
        """Parse a ProxyServer value into a localhost/127.x HTTP proxy port.

        支持 "127.0.0.1:7890"、"localhost:7890" 与 "http=…;https=…" 复合形式；
        只接受 localhost/127.x 主机与可解析端口，其余（含 socks 形式）一律 None。
        """
        chosen = ""
        for part in str(server or "").split(";"):
            part = part.strip()
            if not part:
                continue
            protocol, separator, address = part.partition("=")
            if separator:
                protocol = protocol.strip().lower()
                address = address.strip()
                if protocol == "http":
                    chosen = address
                    break
                if protocol == "https" and not chosen:
                    chosen = address
            else:
                chosen = part
                break
        if "://" in chosen:
            scheme, _, chosen = chosen.partition("://")
            if scheme.lower() not in {"http", "https"}:
                return None
        host, separator, port_text = chosen.rpartition(":")
        if not separator:
            return None
        host = host.strip("[]").lower()
        if host != "localhost" and not host.startswith("127."):
            return None
        try:
            port = int(port_text)
        except ValueError:
            return None
        if not 1 <= port <= 65535:
            return None
        return port

    @staticmethod
    def _probe(url: str, proxy: str, *, timeout: tuple[float, float] = (3, 6)) -> dict[str, Any]:
        session = requests.Session()
        session.trust_env = False
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        started = time.monotonic()
        try:
            response = session.get(url, timeout=timeout, allow_redirects=False, stream=True)
            healthy = int(response.status_code) < 500
            response.close()
            return {"healthy": healthy, "latency_ms": round((time.monotonic() - started) * 1000)}
        except requests.RequestException:
            return {"healthy": False, "latency_ms": None}
        finally:
            session.close()

    @staticmethod
    def _normalize_proxy(value: str) -> str:
        """C2：盲补 http:// 分支退役——v3 路由门（_proxy_url_parseable）已在
        保存路径拒绝无 scheme 脏值；这里只做合法值的规范化，无 scheme 一律拒绝。"""
        proxy = str(value or "").strip()
        if not proxy:
            return DEFAULT_PROXY
        if not proxy.startswith(("http://", "https://")):
            raise ValueError("只支持 HTTP 或 HTTPS 代理")
        return proxy.rstrip("/")


__all__ = [
    "DEFAULT_PROXY",
    "NetworkSettings",
    "PROXY_DETECT_PROBE_URL",
    "PROXY_SCAN_PORTS",
    "ROUTE_DECISION_TTL_SECONDS",
    "ROUTE_HEALTH_COOLDOWN_SECONDS",
    "ROUTE_HEALTH_TTL_SECONDS",
    "VPN_CONNECTION_SCHEMA",
    "validate_vpn_connection_snapshot",
]
