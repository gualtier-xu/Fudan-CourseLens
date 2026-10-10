"""Fail-closed local live-room status and secure HLS proxy primitives."""

from __future__ import annotations

import requests
import ipaddress
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed


LIVE_STATES = frozenset({"live", "upcoming", "ended", "denied", "offline", "stale", "unknown"})
PLAYABLE_CONTENT_TYPES = frozenset({
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "audio/mpegurl", "audio/x-mpegurl", "video/mp2t", "video/mp4",
    "audio/mp4", "application/octet-stream",
})
MANIFEST_CONTENT_TYPES = frozenset({
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "audio/mpegurl", "audio/x-mpegurl",
})
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "origin", "referer", "proxy-authorization"})
REQUEST_HEADER_ALLOWLIST = frozenset({"accept", "accept-language", "user-agent"})
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MEDIA_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_RANGE_BYTES = 8 * 1024 * 1024
# Playback session lifetime: PLAYBACK_IDLE_TTL is a sliding idle window that
# restarts on every successful fetch (a live class polls its manifest every
# few seconds, so a watched session stays alive), PLAYBACK_ABSOLUTE_TTL caps
# the total lifetime counted from session creation and renewal never crosses
# it, and a session that has crossed either deadline is never revived —
# re-entry goes through a fresh one-time grant.
PLAYBACK_IDLE_TTL = 5 * 60.0
PLAYBACK_ABSOLUTE_TTL = 6 * 3600.0
# 平台直播四路视角（LIVESTUDY-1 A2 账本定案，与 icourse 的 live_url 嵌套键
# 同源）：output=教师画面分支、output_student=学生画面分支，各含 m3u8（视频）
# 与 m3u8_audio（纯音频）。view 维度只承载 URL-free 闭集 id，绝不携带流地址；
# 默认视角=学生画面，与既有默认流解析序的首选一致，既有语义不变。
LIVE_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("teacher", "output", "m3u8"),
    ("student", "output_student", "m3u8"),
    ("teacher_audio", "output", "m3u8_audio"),
    ("student_audio", "output_student", "m3u8_audio"),
)
DEFAULT_LIVE_VIEW = "student"
LIVE_VIEW_IDS = frozenset(view for view, _, _ in LIVE_VIEWS)
_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)


class LiveRoomError(RuntimeError):
    """Closed error carrying only a browser-safe code."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = int(status)


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    session: Any
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _Grant:
    course_id: str
    identity_scope: str
    expires_at: float
    consumed: bool = False


@dataclass(slots=True)
class _Resource:
    url: str
    kind: str
    expires_at: float


@dataclass(slots=True)
class _Playback:
    course_id: str
    identity_scope: str
    upstream: UpstreamRequest
    expires_at: float
    absolute_deadline: float
    view: str = DEFAULT_LIVE_VIEW
    resources: dict[str, _Resource] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    status: int
    content_type: str
    body: bytes
    content_range: str = ""
    accept_ranges: str = ""


def _global_addresses(hostname: str, resolver: Callable[..., Iterable] = socket.getaddrinfo) -> tuple[str, ...]:
    try:
        rows = resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise LiveRoomError("live_dns_failed", 502) from exc
    values: list[str] = []
    for row in rows:
        address = str(row[4][0]).split("%", 1)[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise LiveRoomError("live_dns_invalid", 502) from exc
        if not parsed.is_global:
            raise LiveRoomError("live_private_address_rejected", 403)
        if address not in values:
            values.append(address)
    if not values:
        raise LiveRoomError("live_dns_empty", 502)
    return tuple(values)


def validate_https_url(url: str, resolver: Callable[..., Iterable] = socket.getaddrinfo) -> tuple[str, tuple[str, ...]]:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise LiveRoomError("live_https_required", 403)
    if parsed.username is not None or parsed.password is not None:
        raise LiveRoomError("live_userinfo_rejected", 403)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LiveRoomError("live_port_rejected", 403) from exc
    if port not in (None, 443):
        raise LiveRoomError("live_port_rejected", 403)
    hostname = parsed.hostname.rstrip(".").casefold()
    return hostname, _global_addresses(hostname, resolver)


def _response_peer_ip(response: Any) -> str:
    direct = str(getattr(response, "peer_ip", "") or getattr(response, "primary_ip", "") or "")
    if direct:
        return direct.split("%", 1)[0]
    infos = getattr(response, "infos", None)
    if isinstance(infos, dict):
        for key, value in infos.items():
            if str(key).casefold().endswith("primary_ip") and value:
                return str(value).split("%", 1)[0]
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None)
    sock = getattr(connection, "sock", None)
    if sock is not None:
        try:
            return str(sock.getpeername()[0]).split("%", 1)[0]
        except OSError:
            pass
    raise LiveRoomError("live_peer_unverified", 502)


def bounded_range(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "," in raw:
        raise LiveRoomError("live_range_rejected", 416)
    match = _RANGE.fullmatch(raw)
    if not match or not any(match.groups()):
        raise LiveRoomError("live_range_rejected", 416)
    start_text, end_text = match.groups()
    if not start_text:
        size = min(int(end_text), MAX_RANGE_BYTES)
        if size <= 0:
            raise LiveRoomError("live_range_rejected", 416)
        return f"bytes=-{size}"
    start = int(start_text)
    end = int(end_text) if end_text else start + MAX_RANGE_BYTES - 1
    if end < start:
        raise LiveRoomError("live_range_rejected", 416)
    return f"bytes={start}-{min(end, start + MAX_RANGE_BYTES - 1)}"


def available_views_from_live_url(value: object) -> tuple[str, ...]:
    """Derive URL-free available view ids from a platform ``live_url`` dict.

    A view counts as available only when its nested branch carries a
    non-empty ``.m3u8`` manifest URL — the same closed-set rule the default
    stream resolution applies. The result never contains URLs, only view
    ids, and always in the canonical four-view order.
    """
    if not isinstance(value, dict):
        return ()
    views: list[str] = []
    for view, branch_key, kind in LIVE_VIEWS:
        branch = value.get(branch_key)
        if not isinstance(branch, dict):
            continue
        url = str(branch.get(kind) or "").strip()
        if url and urlsplit(url).path.casefold().endswith(".m3u8"):
            views.append(view)
    return tuple(views)


class LiveRoomService:
    """Identity-scoped status, one-time entry grants and opaque HLS resources."""

    def __init__(
        self,
        *,
        authorize: Callable[[str], bool],
        identity_scope: Callable[[], str],
        observe: Callable[[str], dict[str, Any]],
        open_upstream: Callable[..., UpstreamRequest],
        clock: Callable[[], float] = time.time,
        resolver: Callable[..., Iterable] = socket.getaddrinfo,
        observation_ttl: float = 20.0,
        grant_ttl: float = 30.0,
        playback_ttl: float = PLAYBACK_IDLE_TTL,
        playback_absolute_ttl: float = PLAYBACK_ABSOLUTE_TTL,
    ):
        self._authorize = authorize
        self._identity_scope = identity_scope
        self._observe = observe
        self._open_upstream = open_upstream
        self._clock = clock
        self._resolver = resolver
        self._observation_ttl = max(1.0, float(observation_ttl))
        self._grant_ttl = max(1.0, float(grant_ttl))
        self._playback_ttl = max(5.0, float(playback_ttl))
        self._playback_absolute_ttl = max(self._playback_ttl, float(playback_absolute_ttl))
        self._lock = threading.RLock()
        self._grants: dict[str, _Grant] = {}
        self._playbacks: dict[str, _Playback] = {}

    def status(self, course_id: str) -> dict[str, Any]:
        course_id = str(course_id or "").strip()
        now = self._clock()
        scope = self._identity_scope()
        if not course_id or not scope or not self._authorize(course_id):
            return self._public_status("denied", now, 0.0, "live_authorization_denied")
        try:
            raw = dict(self._observe(course_id) or {})
        except (OSError, TimeoutError):
            return self._public_status("offline", now, 0.0, "live_observation_offline")
        except Exception:
            # SRC-SYNDROME-1 U2：观测层真异常留一行闭集证据（此前此处零打印=
            # 晨间日志盲区）；原始异常绝不外泄，closed-set code 照旧。
            print("[live] failure_code=live_observation_failed status=unknown route=observe", flush=True)
            return self._public_status("unknown", now, 0.0, "live_observation_failed")
        state = str(raw.get("state") or "unknown").strip().casefold()
        if state not in LIVE_STATES:
            state = "unknown"
        # 夜10-C T18：上游观测负载的畸形时间戳曾以裸 ValueError 炸出 status()
        #（观测回调的 try 只护获取不护解析）；与畸形行免疫同纪律 fail-closed，
        # 坏值按缺失处理（观测过期 → stale 语义自然接管）。
        try:
            observed_at = float(raw.get("observed_at") or 0.0)
            expires_at = float(raw.get("expires_at") or (observed_at + self._observation_ttl if observed_at else 0.0))
        except (TypeError, ValueError):
            observed_at = 0.0
            expires_at = 0.0
        if state == "live" and (not observed_at or now > expires_at):
            state = "stale"
        result = self._public_status(
            state, observed_at, expires_at,
            str(raw.get("code") or f"live_{state}"),
        )
        for key in ("starts_at", "ends_at"):
            value = str(raw.get(key) or "").strip()
            if value:
                result[key] = value
        # available_views 透传（URL-free 闭集元数据）：只放行已知 view id 并
        # 重排为正典序；未提供则不出键，显式空表如实透出（=一路都解析不出）。
        if "available_views" in raw:
            provided = raw["available_views"]
            seen = {
                str(view) for view in (provided if isinstance(provided, (list, tuple, set, frozenset)) else ())
                if str(view) in LIVE_VIEW_IDS
            }
            result["available_views"] = [view for view, _, _ in LIVE_VIEWS if view in seen]
        result["can_enter"] = state == "live"
        return result

    @staticmethod
    def _public_status(state: str, observed_at: float, expires_at: float, code: str) -> dict[str, Any]:
        return {
            "state": state,
            "source": "platform" if observed_at else "local",
            "observed_at": observed_at,
            "expires_at": expires_at,
            "stale": state == "stale",
            "code": re.sub(r"[^a-z0-9_]", "_", code.casefold())[:80],
            "can_enter": False,
        }

    def issue_grant(self, course_id: str) -> dict[str, Any]:
        status = self.status(course_id)
        if status["state"] != "live" or not status["can_enter"]:
            raise LiveRoomError(str(status["code"]), 409 if status["state"] != "denied" else 403)
        token = secrets.token_urlsafe(32)
        expires_at = self._clock() + self._grant_ttl
        with self._lock:
            self._purge_locked()
            self._grants[token] = _Grant(str(course_id), self._identity_scope(), expires_at)
        return {"grant": token, "expires_at": expires_at}

    def consume_grant(self, grant: str, *, view: str = DEFAULT_LIVE_VIEW) -> dict[str, Any]:
        view = str(view or "").strip()
        if view not in LIVE_VIEW_IDS:
            # 闭集外视角在消费授予前即拒：grant 未被标记已消费，调用方可携
            # 合法视角原地重试，不必重走一次发授予。
            raise LiveRoomError("live_view_unknown", 400)
        now = self._clock()
        with self._lock:
            self._purge_locked()
            item = self._grants.get(str(grant or ""))
            if item is None or item.consumed or now > item.expires_at:
                raise LiveRoomError("live_grant_invalid", 403)
            if not item.identity_scope or item.identity_scope != self._identity_scope():
                raise LiveRoomError("live_grant_identity_changed", 403)
            if not self._authorize(item.course_id):
                raise LiveRoomError("live_authorization_revoked", 403)
            item.consumed = True
            try:
                upstream = self._open_upstream(item.course_id, view=view)
            except LiveRoomError:
                raise
            except Exception:
                # SRC-SYNDROME-1 U2：取流会话航班的裸异常（登录失败族）收编为
                # 闭集码，不再逃逸成 500 runtime_failed（前端 unknown 的上游半边）。
                print("[live] failure_code=live_session_unavailable status=503 route=upstream-open", flush=True)
                raise LiveRoomError("live_session_unavailable", 503)
            validate_https_url(upstream.url, self._resolver)
            session_id = secrets.token_urlsafe(32)
            playback = _Playback(
                item.course_id, item.identity_scope, upstream,
                now + self._playback_ttl,
                now + self._playback_absolute_ttl,
                view=view,
            )
            manifest_id = self._register_resource(playback, upstream.url, "manifest")
            self._playbacks[session_id] = playback
        return {
            "session_id": session_id,
            "manifest_id": manifest_id,
            "manifest_path": f"/api/v3/live-room/play/{session_id}/manifest/{manifest_id}",
            "expires_at": playback.expires_at,
            "view": view,
        }

    def fetch(self, session_id: str, resource_id: str, *, cookie_session: str, range_header: str = "") -> ProxyResponse:
        now = self._clock()
        if not secrets.compare_digest(str(session_id or ""), str(cookie_session or "")):
            raise LiveRoomError("live_session_cookie_required", 403)
        with self._lock:
            self._purge_locked()
            playback = self._playbacks.get(session_id)
        if playback is None or now > playback.expires_at:
            raise LiveRoomError("live_session_expired", 403)
        if playback.identity_scope != self._identity_scope() or not self._authorize(playback.course_id):
            raise LiveRoomError("live_authorization_revoked", 403)
        with playback.lock:
            resource = playback.resources.get(resource_id)
            if resource is None or now > resource.expires_at:
                raise LiveRoomError("live_resource_expired", 404)
            response = self._request(playback, resource.url, range_header=range_header)
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if content_type not in PLAYABLE_CONTENT_TYPES:
                response.close()
                raise LiveRoomError("live_content_type_rejected", 415)
            is_manifest = resource.kind == "manifest" or content_type in MANIFEST_CONTENT_TYPES
            limit = MAX_MANIFEST_BYTES if is_manifest else MAX_MEDIA_BYTES
            body = self._read_bounded(response, limit)
            if is_manifest:
                body = self._rewrite_manifest(playback, resource.url, body)
                body = body.replace(b"__SESSION__", session_id.encode("ascii"))
                content_type = "application/vnd.apple.mpegurl"
            self._touch_session(session_id, playback)
            return ProxyResponse(
                int(response.status_code), content_type, body,
                str(response.headers.get("Content-Range") or ""),
                "bytes" if response.headers.get("Accept-Ranges") else "",
            )

    def _touch_session(self, session_id: str, playback: _Playback) -> None:
        """Slide the idle window forward after one successful upstream fetch.

        Renewal extends a still-alive session only: it never crosses the
        absolute deadline set at creation, and a session that has crossed
        its own expiry mid-flight — or already been purged by another
        request — stays dead and must be re-entered through a fresh grant.
        """
        now = self._clock()
        renewed = min(now + self._playback_ttl, playback.absolute_deadline)
        with self._lock:
            if self._playbacks.get(session_id) is not playback:
                return
            if now > playback.expires_at:
                return
            playback.expires_at = renewed
            for resource in playback.resources.values():
                resource.expires_at = renewed

    def revoke_all(self) -> None:
        with self._lock:
            self._grants.clear()
            self._playbacks.clear()

    def _request(self, playback: _Playback, url: str, *, range_header: str):
        try:
            return self._request_follow(playback, url, range_header=range_header)
        except LiveRoomError:
            raise
        except EgressBlockedError as exc:
            # 测试模式出站门（src/runtime/test_mode.py）：拒绝收编进既有闭集码
            # live_upstream_unreachable（合成环境本就不提供真直播上游；
            # 审计行已携 egress_blocked+host）。
            print("[live] failure_code=live_upstream_unreachable status=502 route=play reason=egress_blocked", flush=True)
            raise LiveRoomError("live_upstream_unreachable", 502) from exc
        except requests.RequestException as exc:
            # U17③/A2：requests 裸异常（连接/超时/SSL）收编闭集码 + tee 可见行
            print("[live] failure_code=live_upstream_unreachable status=502 route=play", flush=True)
            raise LiveRoomError("live_upstream_unreachable", 502) from exc

    def _request_follow(self, playback: _Playback, url: str, *, range_header: str):
        current = url
        # 测试模式出站门（首跳，先于任何 resolver/DNS 工作；src/runtime/test_mode.py）
        # ——EgressBlockedError 由 _request 收编为 live_upstream_unreachable。
        ensure_egress_allowed(urlsplit(current).hostname or "", purpose="live_media")
        base_host, _ = validate_https_url(current, self._resolver)
        headers = {
            key: value for key, value in playback.upstream.headers.items()
            if key.casefold() in REQUEST_HEADER_ALLOWLIST or key.casefold() in SENSITIVE_HEADERS
        }
        selected_range = bounded_range(range_header)
        if selected_range:
            headers["Range"] = selected_range
        for _hop in range(MAX_REDIRECTS + 1):
            # 测试模式出站门（逐跳重定向续查，先于 resolver/DNS）。
            ensure_egress_allowed(urlsplit(current).hostname or "", purpose="live_media")
            host, expected_addresses = validate_https_url(current, self._resolver)
            if host != base_host:
                headers = {k: v for k, v in headers.items() if k.casefold() not in SENSITIVE_HEADERS}
                raise LiveRoomError("live_cross_origin_redirect_rejected", 403)
            response = playback.upstream.session.get(
                current, headers=headers, timeout=(5, 20), stream=True,
                allow_redirects=False,
            )
            peer_ip = _response_peer_ip(response)
            if peer_ip not in expected_addresses:
                response.close()
                raise LiveRoomError("live_dns_rebinding_rejected", 403)
            if int(response.status_code) not in {301, 302, 303, 307, 308}:
                if int(response.status_code) not in {200, 206}:
                    response.close()
                    raise LiveRoomError("live_upstream_rejected", 502)
                return response
            location = str(response.headers.get("Location") or "")
            response.close()
            current = urljoin(current, location)
        raise LiveRoomError("live_redirect_limit", 502)

    @staticmethod
    def _read_bounded(response: Any, limit: int) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    response.close()
                    raise LiveRoomError("live_response_too_large", 413)
            except ValueError:
                response.close()
                raise LiveRoomError("live_content_length_invalid", 502)
        output = bytearray()
        try:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                output.extend(chunk)
                if len(output) > limit:
                    raise LiveRoomError("live_response_too_large", 413)
        finally:
            response.close()
        return bytes(output)

    def _rewrite_manifest(self, playback: _Playback, base_url: str, body: bytes) -> bytes:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise LiveRoomError("live_manifest_encoding_rejected", 415) from exc
        if not text.lstrip().startswith("#EXTM3U"):
            raise LiveRoomError("live_manifest_invalid", 415)
        output: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                output.append("")
                continue
            if line.startswith("#"):
                def replace(match: re.Match[str]) -> str:
                    absolute = urljoin(base_url, match.group(1))
                    kind = "key" if line.startswith("#EXT-X-KEY") else "media"
                    resource_id = self._register_resource(playback, absolute, kind)
                    return f'URI="/api/v3/live-room/play/__SESSION__/resource/{resource_id}"'
                output.append(_URI_ATTRIBUTE.sub(replace, raw_line))
                continue
            absolute = urljoin(base_url, line)
            kind = "manifest" if urlsplit(absolute).path.casefold().endswith(".m3u8") else "media"
            resource_id = self._register_resource(playback, absolute, kind)
            output.append(f"/api/v3/live-room/play/__SESSION__/resource/{resource_id}")
        return ("\n".join(output) + "\n").encode("utf-8")

    def _register_resource(self, playback: _Playback, url: str, kind: str) -> str:
        validate_https_url(url, self._resolver)
        if urlsplit(url).path.casefold().endswith(".flv"):
            raise LiveRoomError("live_flv_rejected", 415)
        resource_id = secrets.token_urlsafe(24)
        playback.resources[resource_id] = _Resource(url, kind, playback.expires_at)
        return resource_id

    def _purge_locked(self) -> None:
        now = self._clock()
        self._grants = {
            key: value for key, value in self._grants.items()
            if not value.consumed and value.expires_at >= now
        }
        self._playbacks = {
            key: value for key, value in self._playbacks.items()
            if value.expires_at >= now
        }


__all__ = [
    "LIVE_STATES", "LIVE_VIEW_IDS", "LIVE_VIEWS", "PLAYBACK_ABSOLUTE_TTL",
    "PLAYBACK_IDLE_TTL", "DEFAULT_LIVE_VIEW", "LiveRoomError",
    "LiveRoomService", "ProxyResponse", "UpstreamRequest", "bounded_range",
    "available_views_from_live_url", "validate_https_url",
]
