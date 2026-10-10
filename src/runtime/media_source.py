"""Memory-only authenticated media proxy primitives."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)
MAX_STREAM_RANGE_BYTES = 8 * 1024 * 1024


def parse_upstream_range(value: str) -> tuple[int | None, int | None] | None:
    value = str(value or "").strip()
    if not value:
        return None
    if "," in value:
        raise ValueError("multiple byte ranges are not supported")
    match = _RANGE_RE.fullmatch(value)
    if not match or not any(match.groups()):
        raise ValueError("invalid byte range")
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    if start is not None and end is not None and end < start:
        raise ValueError("invalid byte range")
    return start, end


def bounded_range_header(value: str, *, maximum: int = MAX_STREAM_RANGE_BYTES) -> str:
    """Normalize a browser range to one bounded interval before contacting upstream."""
    if maximum <= 0:
        raise ValueError("maximum range must be positive")
    parsed = parse_upstream_range(value)
    if parsed is None:
        return f"bytes=0-{maximum - 1}"
    start, end = parsed
    if start is None:
        return f"bytes=-{min(int(end or 0), maximum)}"
    bounded_end = start + maximum - 1 if end is None else min(end, start + maximum - 1)
    return f"bytes={start}-{bounded_end}"


def _header_int(headers, name: str) -> int:
    try:
        return max(0, int(headers.get(name) or 0))
    except (TypeError, ValueError):
        return 0


def _content_range_total(headers) -> int:
    value = str(headers.get("content-range") or headers.get("Content-Range") or "")
    if "/" not in value:
        return 0
    tail = value.rsplit("/", 1)[-1].strip()
    try:
        return max(0, int(tail))
    except ValueError:
        return 0


class _ConfirmedServiceResponse(RuntimeError):
    """Upstream answered with a definitive non-auth status (P3-B).

    Terminal for this request: no session rebuild, no second request. The
    message keeps the historical ``HTTP <status>`` shape of raise_for_status.
    """


@dataclass
class RemoteMediaStream:
    status: int
    content_type: str
    content_length: int
    content_range: str
    total_length: int
    start: int
    end: int
    etag: str
    _response: object
    _skip_bytes: int = 0
    _resume: Callable[[int, int, str], "RemoteMediaStream"] | None = None

    def close(self) -> None:
        close = getattr(self._response, "close", None)
        if close:
            close()

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        remaining = self.content_length
        skip = self._skip_bytes
        emitted = 0
        try:
            iterator = self._response.iter_content(chunk_size=min(max(1, chunk_size), 64 * 1024))
            for chunk in iterator:
                if not chunk:
                    continue
                if skip:
                    consumed = min(skip, len(chunk))
                    skip -= consumed
                    chunk = chunk[consumed:]
                    if not chunk:
                        continue
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)
                emitted += len(chunk)
                yield chunk
                if remaining <= 0:
                    break
        except Exception:
            if remaining <= 0 or self._resume is None:
                raise
            resumed = self._resume(self.start + emitted, self.end, self.etag)
            try:
                if self.etag and resumed.etag and resumed.etag != self.etag:
                    raise RuntimeError("upstream media changed during range resume")
                for chunk in resumed.iter_bytes(chunk_size):
                    yield chunk
            finally:
                resumed.close()
            return
        if remaining > 0 and self._resume is not None:
            resumed = self._resume(self.start + emitted, self.end, self.etag)
            try:
                for chunk in resumed.iter_bytes(chunk_size):
                    yield chunk
            finally:
                resumed.close()


def _is_transport_layer_error(exc: BaseException) -> bool:
    """SOAK-F1（LIVE-VALIDATE-2 补充行）：TLS/连接类失败不得触发会话重建。

    一次 ``_refresh`` = 弃掉整个客户端会话 = 下一次请求做一次全量 WebVPN
    重登录。媒体上游夜间 SSL 失败时（实测连续 502，kind=SSLError），旧逻辑
    把每次失败放大成一次重登录，叠加前端 44 秒自动重试形成登录风暴
    （SOAK 实测 1.9h/154 次）。传输层失败重建会话毫无治愈力：立刻终局。
    会话纪元类失败（401/403 与未知解析器失败）保留重建+恰一次重试语义。
    """
    try:
        from requests.exceptions import RequestException
    except ImportError:
        return False
    return isinstance(exc, RequestException)


# MEDIA-001-20261001：媒体开流结局的闭集码。<video> 把路由的一切失败
# （502/404/401）都塌缩成 MediaError code 4，错误卡无法归因；路由把精确
# 结局记进 MediaStreamFailureLedger，前端据此细分恢复卡。码表闭集：
# 空串=最近一次开流成功（清态）。
MEDIA_STREAM_SUCCESS = ""
MEDIA_STREAM_UPSTREAM_UNREACHABLE = "upstream_unreachable"
MEDIA_STREAM_UPSTREAM_REJECTED = "upstream_rejected"
MEDIA_STREAM_AUTH_REQUIRED = "auth_required"
MEDIA_STREAM_MISSING = "missing"
MEDIA_STREAM_INVALID = "invalid"
MEDIA_STREAM_UNKNOWN = "unknown"
MEDIA_STREAM_FAILURE_CODES = frozenset({
    MEDIA_STREAM_SUCCESS,
    MEDIA_STREAM_UPSTREAM_UNREACHABLE,
    MEDIA_STREAM_UPSTREAM_REJECTED,
    MEDIA_STREAM_AUTH_REQUIRED,
    MEDIA_STREAM_MISSING,
    MEDIA_STREAM_INVALID,
    MEDIA_STREAM_UNKNOWN,
})


def classify_open_failure(exc: BaseException) -> str:
    """Map one failed media open to its closed-set class.

    - 上游明确答了确认性拒绝（P3-B 的终局响应）→ upstream_rejected：
      重签/重认证类动作有意义。
    - 传输层失败（校外实测 ConnectionError：媒体主机从该网络直连不到）
      → upstream_unreachable：换网络/校园网/学校 VPN 才是解。
    - 其余未知开流异常 → unknown。
    """
    if isinstance(exc, _ConfirmedServiceResponse):
        return MEDIA_STREAM_UPSTREAM_REJECTED
    if _is_transport_layer_error(exc):
        return MEDIA_STREAM_UPSTREAM_UNREACHABLE
    return MEDIA_STREAM_UNKNOWN


class MediaStreamFailureLedger:
    """In-memory closed-set record of the last media open outcome.

    每个本地服务实例一份（make_handler 内创建）；只存闭集码与单调 seq，
    绝不存 URL、主机名或任何上游文本。开流成功也记账（码=空串）并推进
    seq——错误卡凭 seq 新鲜度避免把陈旧失败安到新请求头上。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failure_code = MEDIA_STREAM_SUCCESS
        self._seq = 0

    def note(self, code: str) -> None:
        if code not in MEDIA_STREAM_FAILURE_CODES:
            return
        with self._lock:
            self._seq += 1
            self._failure_code = code

    def status(self) -> dict:
        with self._lock:
            return {"failure_code": self._failure_code, "seq": self._seq}


class RemoteMediaGateway:
    """Open an upstream stream without retaining signed URLs or credentials."""

    def __init__(
        self,
        resolver: Callable[[], tuple[object, str, dict[str, str]]],
        refresh: Callable[[], None],
    ):
        self._resolver = resolver
        self._refresh = refresh

    def open(self, range_header: str = "", *, head_only: bool = False) -> RemoteMediaStream:
        effective_range = "" if head_only else bounded_range_header(range_header)
        requested = parse_upstream_range(effective_range)
        last_error: Exception | None = None
        refresh_pending = False
        for attempt in range(2):
            if refresh_pending:
                # P3-B: the session-epoch verification runs outside the retry
                # try-block — a failed verification is terminal for this
                # request (exactly one rebuild attempt, no second request).
                refresh_pending = False
                self._refresh()
            response = None
            try:
                session, url, base_headers = self._resolver()
                headers = {
                    str(key): str(value)
                    for key, value in (base_headers or {}).items()
                    if str(key).casefold() not in {"connection", "host", "content-length"}
                }
                if effective_range:
                    headers["Range"] = effective_range
                response = session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(15, 300),
                    allow_redirects=True,
                    proxies=_media_stream_proxies(),
                )
                status = int(getattr(response, "status_code", 0) or 0)
                if status in {401, 403} and attempt == 0:
                    response.close()
                    response = None
                    refresh_pending = True
                    continue
                if status == 416:
                    total = _content_range_total(response.headers)
                    stream = RemoteMediaStream(
                        status=416,
                        content_type="video/mp4",
                        content_length=0,
                        content_range=f"bytes */{total}" if total else "",
                        total_length=total,
                        start=0,
                        end=-1,
                        etag="",
                        _response=response,
                    )
                    return stream
                if status >= 400:
                    # P3-B: a confirmed service response is terminal for this
                    # request — no refresh, no second request.
                    response.close()
                    response = None
                    raise _ConfirmedServiceResponse(f"HTTP {status}")
                response.raise_for_status()
                stream = self._normalize(response, requested, head_only=head_only)
                if not head_only and stream.status in {200, 206} and stream.content_length:
                    stream._resume = self._resume_stream
                return stream
            except _ConfirmedServiceResponse:
                raise
            except Exception as exc:
                last_error = exc
                if response is not None:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                if attempt == 0 and not _is_transport_layer_error(exc):
                    refresh_pending = True
                    continue
                raise
        assert last_error is not None
        raise last_error

    def _resume_stream(self, start: int, end: int, expected_etag: str) -> RemoteMediaStream:
        stream = self.open(f"bytes={max(0, int(start))}-{max(0, int(end))}")
        if expected_etag and stream.etag and stream.etag != expected_etag:
            stream.close()
            raise RuntimeError("upstream media changed during range resume")
        # A resumed stream gets no second body retry, keeping recovery bounded.
        stream._resume = None
        return stream

    @staticmethod
    def _normalize(response, requested, *, head_only: bool) -> RemoteMediaStream:
        headers = response.headers
        status = int(response.status_code)
        content_length = _header_int(headers, "content-length")
        total = _content_range_total(headers)
        content_range = str(headers.get("content-range") or "")
        skip = 0
        if status == 206:
            match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
            if not match:
                raise RuntimeError("upstream returned an invalid Content-Range")
            start, end = int(match.group(1)), int(match.group(2))
            if match.group(3) != "*":
                total = int(match.group(3))
            length = end - start + 1
            if content_length and content_length != length:
                length = min(length, content_length)
                end = start + length - 1
            content_length = length
        else:
            total = total or content_length
            start, end = 0, max(-1, total - 1)
            if requested is not None and total:
                requested_start, requested_end = requested
                if requested_start is None:
                    suffix = min(total, int(requested_end or 0))
                    start = total - suffix
                    end = total - 1
                else:
                    start = requested_start
                    end = min(total - 1, requested_end if requested_end is not None else total - 1)
                if start >= total or end < start:
                    response.close()
                    return RemoteMediaStream(
                        status=416,
                        content_type="video/mp4",
                        content_length=0,
                        content_range=f"bytes */{total}",
                        total_length=total,
                        start=0,
                        end=-1,
                        etag="",
                        _response=response,
                    )
                skip = start
                content_length = end - start + 1
                content_range = f"bytes {start}-{end}/{total}"
                status = 206
        return RemoteMediaStream(
            status=status,
            content_type=str(headers.get("content-type") or "video/mp4").split(";", 1)[0],
            content_length=content_length,
            content_range=content_range,
            total_length=total,
            start=start,
            end=end,
            etag=str(headers.get("etag") or ""),
            _response=response,
            _skip_bytes=0 if head_only else skip,
        )


# ---- MEDIA-VPN-1-20261001：aTrust 在位本地探测 + 媒体流可选系统代理 ----
# 全部纯本机枚举（进程名 / 服务注册表键 / 安装目录 / WinINET 系统代理注册表
# 值 + HTTPS_PROXY 环境变量名），零外联、零遥测；快照一律闭集键——绝不携带
# 进程列表、安装路径、代理主机:端口或任何上游文本（「你的数据在哪里」底线）。
# 「未装」结论只在三腿探测全部干净返回且皆空时才下，否则保守 unknown。

ATRUST_PRESENT = "present"
ATRUST_NOT_INSTALLED = "not_installed"
ATRUST_UNKNOWN = "unknown"
ATRUST_STATES = frozenset({ATRUST_PRESENT, ATRUST_NOT_INSTALLED, ATRUST_UNKNOWN})

MEDIA_PROXY_SOURCE_NONE = "none"
MEDIA_PROXY_SOURCE_SYSTEM_REGISTRY = "system_registry"

MEDIA_STREAM_PROXY_STATE_KEY = "media_stream_proxy"
MEDIA_STREAM_PROXY_STATE_SCHEMA = "courselens.media-stream-proxy.v1"

_ATRUST_CACHE_TTL_SECONDS = 60.0
_atrust_lock = threading.Lock()
_atrust_cache: tuple[float, dict] | None = None


def _windows_process_hit() -> bool | None:
    """深信服 aTrust 常见进程名枚举（Toolhelp32 快照，纯本机系统调用）。

    刻意不走 tasklist 子进程：客户端架构边界合同（零本地媒体/计算子进程）
    的 subprocess 白名单不含媒体面。ctypes 只读系统快照等价达成且更轻。
    None=探测不可用。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            walk = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while walk:
                name = str(entry.szExeFile or "").lower()
                if name.startswith("atrust") or "sangfor" in name:
                    return True
                walk = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel32.CloseHandle(snapshot)
    except (OSError, AttributeError, ValueError):
        return None


def _windows_service_hit() -> bool | None:
    """服务注册表键名包含 atrust/sangfor（在位证据；存在≠运行）。None=探测不可用。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services"
        ) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    return False
                index += 1
                lowered = name.lower()
                if "atrust" in lowered or "sangfor" in lowered:
                    return True
    except OSError:
        return None


def _install_dir_hit() -> bool | None:
    """闭集候选安装目录存在性（Sangfor\\aTrust 与 Sangfor 家族目录）。"""
    candidates = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(os.path.join(root, "Sangfor", "aTrust"))
            candidates.append(os.path.join(root, "Sangfor"))
    if not candidates:
        return None
    return any(os.path.isdir(path) for path in candidates)


def detect_windows_system_proxy() -> tuple[str, str]:
    """只读 Windows 系统代理（WinINET Internet Settings），返回 (url, source)。

    来源=注册表（env/registry 二选一的实现选择，理由记档）：官方 icourse
    网页在 aTrust 环境可看课（MEDIA-002 用户事实），浏览器走的正是 WinINET
    系统代理；GUI 打包应用几乎读不到 env HTTPS_PROXY（Explorer 不传递），
    且既有证据（fudan-icourse-liveroom 已知坑#1）显示 env 代理正是打断学校
    认证链的成因——不把它接进媒体面。ProxyServer 兼容 per-protocol 形式，
    优先 https 条目；无 scheme 时补 http://。绝不写回、不修改任何系统设置。
    """
    if sys.platform != "win32":
        return "", MEDIA_PROXY_SOURCE_NONE
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not int(enabled or 0):
                return "", MEDIA_PROXY_SOURCE_NONE
            raw, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (OSError, ValueError, TypeError):
        return "", MEDIA_PROXY_SOURCE_NONE
    value = str(raw or "").strip()
    if not value:
        return "", MEDIA_PROXY_SOURCE_NONE
    if "=" in value:
        picks = {}
        for part in value.split(";"):
            scheme, _, target = part.strip().partition("=")
            if scheme and target:
                picks[scheme.strip().lower()] = target.strip()
        value = picks.get("https") or picks.get("http") or ""
    if not value:
        return "", MEDIA_PROXY_SOURCE_NONE
    if "://" not in value:
        value = "http://" + value
    return value, MEDIA_PROXY_SOURCE_SYSTEM_REGISTRY


def _env_proxy_configured() -> bool:
    """诊断闭集位：HTTPS_PROXY 环境变量是否配置（只报名，不报值，不作代理来源）。"""
    return any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy"))


def synthesize_atrust_state(
    process_hit: bool | None, service_hit: bool | None, dir_hit: bool | None,
    *, windows: bool,
) -> str:
    """三腿闭集合成：任一在位证据即 present；三腿全空才 not_installed；否则 unknown。"""
    if any(hit is True for hit in (process_hit, service_hit, dir_hit)):
        return ATRUST_PRESENT
    if not windows:
        return ATRUST_UNKNOWN
    if process_hit is False and service_hit is False and dir_hit is False:
        return ATRUST_NOT_INSTALLED
    return ATRUST_UNKNOWN


def _atrust_signal_snapshot() -> dict:
    process_hit = _windows_process_hit()
    service_hit = _windows_service_hit()
    dir_hit = _install_dir_hit()
    return {
        "state": synthesize_atrust_state(
            process_hit, service_hit, dir_hit, windows=sys.platform == "win32",
        ),
        "signals": {
            "process": process_hit is True,
            "service": service_hit is True,
            "directory": dir_hit is True,
        },
    }


def reset_atrust_presence_cache() -> None:
    global _atrust_cache
    with _atrust_lock:
        _atrust_cache = None


def atrust_presence_snapshot(*, force: bool = False) -> dict:
    """aTrust 在位三态闭集快照。

    昂贵腿（进程枚举子进程）TTL 缓存；系统代理面每次实读（连上 VPN 后
    代理可能才出现，不得拿旧值误报）。闭集键：state/signals/proxy，
    无路径、无主机:端口、无环境变量值。
    """
    global _atrust_cache
    now = time.monotonic()
    with _atrust_lock:
        cached = None if force else _atrust_cache
        if cached is not None and now - cached[0] < _ATRUST_CACHE_TTL_SECONDS:
            signals = cached[1]
        else:
            signals = _atrust_signal_snapshot()
            _atrust_cache = (now, signals)
    proxy_url, _ = detect_windows_system_proxy()
    return {
        **signals,
        "proxy": {
            "system_configured": bool(proxy_url),
            "env_configured": _env_proxy_configured(),
        },
    }


def _normalized_media_proxy_enabled(raw: object) -> bool:
    return isinstance(raw, dict) and raw.get("enabled") is True


def media_stream_proxy_enabled(store: object) -> bool:
    """持久化偏好「媒体流走系统代理」，默认关；坏值一律按关处理（不写回）。"""
    if store is None:
        return False
    try:
        raw = store.get_app_state(MEDIA_STREAM_PROXY_STATE_KEY, None)
    except Exception:
        return False
    return _normalized_media_proxy_enabled(raw)


def media_stream_proxy_snapshot(store: object) -> dict:
    """设置面闭集快照：偏好 + 代理检测面（只报来源与有无，绝不报地址值）。"""
    proxy_url, proxy_source = detect_windows_system_proxy()
    return {
        "enabled": media_stream_proxy_enabled(store),
        "proxy_detected": bool(proxy_url),
        "proxy_source": proxy_source,
    }


def set_media_stream_proxy_setting(store: object, request: object) -> dict:
    """闭集校验并持久化「媒体流走系统代理」偏好；脏请求一律 ValueError。"""
    if store is None or not isinstance(request, dict) or not isinstance(request.get("enabled"), bool):
        raise ValueError("media_stream_proxy_request_invalid")
    store.set_app_state(MEDIA_STREAM_PROXY_STATE_KEY, {
        "schema": MEDIA_STREAM_PROXY_STATE_SCHEMA,
        "enabled": request["enabled"],
    })
    return media_stream_proxy_snapshot(store)


# 开流代理供给（每服务实例注册一次，缺省未注册=关）：偏好开且检测到
# Windows 系统代理时，仅媒体字节流这一跳走该代理；授权链/目录/WebVPN 面
# 不经此处，维持现状。供给器抛错一律退回显式直连（绝不因诊断面炸开流）。
_media_proxy_enabled_provider: Callable[[], bool] | None = None


def configure_media_stream_proxy_provider(provider: Callable[[], bool] | None) -> None:
    global _media_proxy_enabled_provider
    _media_proxy_enabled_provider = provider


def _media_stream_proxies() -> dict:
    provider = _media_proxy_enabled_provider
    try:
        enabled = bool(provider()) if provider is not None else False
    except Exception:
        enabled = False
    if not enabled:
        return {"http": None, "https": None}
    proxy_url, source = detect_windows_system_proxy()
    if not proxy_url or source != MEDIA_PROXY_SOURCE_SYSTEM_REGISTRY:
        return {"http": None, "https": None}
    return {"http": proxy_url, "https": proxy_url}


__all__ = [
    "ATRUST_NOT_INSTALLED",
    "ATRUST_PRESENT",
    "ATRUST_STATES",
    "ATRUST_UNKNOWN",
    "MAX_STREAM_RANGE_BYTES",
    "MEDIA_PROXY_SOURCE_NONE",
    "MEDIA_PROXY_SOURCE_SYSTEM_REGISTRY",
    "MEDIA_STREAM_PROXY_STATE_KEY",
    "MEDIA_STREAM_PROXY_STATE_SCHEMA",
    "MediaStreamFailureLedger",
    "RemoteMediaGateway",
    "RemoteMediaStream",
    "atrust_presence_snapshot",
    "bounded_range_header",
    "classify_open_failure",
    "configure_media_stream_proxy_provider",
    "detect_windows_system_proxy",
    "media_stream_proxy_enabled",
    "media_stream_proxy_snapshot",
    "parse_upstream_range",
    "reset_atrust_presence_cache",
    "set_media_stream_proxy_setting",
    "synthesize_atrust_state",
]
