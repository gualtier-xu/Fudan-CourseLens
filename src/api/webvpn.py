"""
WebVPN URL encoding and authentication for Fudan University.

Handles:
- AES-128-CFB URL encoding/decoding for WebVPN proxy URLs
- Full 7-step IDP authentication flow against id.fudan.edu.cn
"""

import html as html_mod
import re
import time
from binascii import hexlify, unhexlify
from collections.abc import Callable
from urllib.parse import urlparse, quote, urljoin

import requests
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from curl_cffi import CurlHttpVersion, requests as curl_requests
from curl_cffi.const import CurlOpt
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from curl_cffi.requests.exceptions import Timeout as CurlTimeout
import base64

from src.runtime import config
from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed


# 身份源明确拒绝凭据的闭集标记：只匹配响应 msg/message/error/error_description 文本
CREDENTIALS_REJECTED_MARKERS = ("密码错误", "用户名或密码", "账号或密码", "密码不正确", "用户不存在", "账号不存在")
CREDENTIALS_REJECTED_CODE = "fudan_credentials_rejected"

# 身份源要求交互式安全验证的闭集标记（CAPTCHA/2FA/验证码类）；命中即终止，
# 绝不自动重试或绕过（P0.2：挑战类失败不换路由、不重试）。
CHALLENGE_MARKERS = ("验证码", "安全验证", "两步验证", "双因素", "captcha")
CHALLENGE_CODE = "fudan_challenge_required"

# 账号被锁定/冻结的闭集标记（P1-A）：与密码错误、挑战都不同——重试与换路
# 由都无意义，用户必须先在复旦账号服务解锁。只匹配 msg/message/error/
# error_description 闭集字段。
ACCOUNT_LOCKED_MARKERS = ("账号已锁定", "账号锁定", "账户已锁定", "账户锁定", "已被锁定", "已冻结", "密码连续错误")
ACCOUNT_LOCKED_CODE = "fudan_account_locked"

# 校园服务维护/升级的闭集标记（P1-A）：服务端确认的维护态，与网络路径故障
# 区分开——唯一的正确动作是稍后重试，换路径或改网络设置都无意义。
SERVICE_MAINTENANCE_MARKERS = ("系统维护", "正在维护", "服务维护", "维护中", "系统升级", "维修", "maintenance")
SERVICE_MAINTENANCE_CODE = "fudan_service_maintenance"

# 闭集：票链已消费后的确认拒绝/异常响应（绝不能重试或重放）。
TICKET_REJECTED_CODE = "fudan_ticket_rejected"
REDIRECT_UNSAFE_CODE = "fudan_redirect_unsafe"

# 闭集：ticket 腿 transport 类别（curl_cffi chrome 模拟，h2 主 / 强制 h1 备）。
TICKET_TRANSPORTS = ("curl_h2", "curl_h1")

# 闭集：_DeadlineSession 每请求 elapsed 记录的传输层 outcome。
# 只描述传输结果，绝不含 URL、headers、cookies 或正文。
STAGE_OUTCOMES = frozenset({"ok", "timeout", "connection_error", "request_error"})

# ticket 腿传输失败的闭集错误码：会话验证未能确认提交结果时携带。
WEBVPN_TICKET_TRANSPORT_CODE = "webvpn_ticket_transport_failed"
ICOURSE_TICKET_TRANSPORT_CODE = "icourse_ticket_transport_failed"

# 会话检查点（V5）：唯一允许持久化会话 cookie 的主机。WebVPN 与被代理的
# iCourse 的全部会话状态都落在该主机名下，因此这是最小且充分的闭集。
CHECKPOINT_COOKIE_HOST = "webvpn.fudan.edu.cn"
CHECKPOINT_MAX_COOKIES = 16


class FudanCredentialsRejected(RuntimeError):
    """IdP explicitly rejected the supplied credentials (wrong user/password)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = CREDENTIALS_REJECTED_CODE


class FudanChallengeRequired(RuntimeError):
    """IdP demanded an interactive security challenge (CAPTCHA/2FA/consent)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = CHALLENGE_CODE


class FudanAccountLocked(RuntimeError):
    """IdP reported the account as locked/frozen; unlock is a user action."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ACCOUNT_LOCKED_CODE


class FudanServiceMaintenance(RuntimeError):
    """School service is under maintenance; retrying later is the only action."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = SERVICE_MAINTENANCE_CODE


class TicketRejectionError(RuntimeError):
    """票链已消费后服务端给出的确认拒绝/异常响应；绝不重试、绝不重放。"""

    def __init__(self, message: str, code: str = TICKET_REJECTED_CODE) -> None:
        super().__init__(message)
        self.code = code


class UnsafeTicketRedirectError(TicketRejectionError):
    """Ticket 跳转目标未通过闭集校验（malformed/unsafe redirect）。"""

    def __init__(self, message: str = "Ticket redirect target is not allowed") -> None:
        super().__init__(message, REDIRECT_UNSAFE_CODE)


class TicketTransportError(RuntimeError):
    """一次性 ticket 的传输腿失败（验证未确认会话建立）；code 属于闭集。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _payload_text(data: dict) -> str:
    return " ".join(
        str(data.get(key) or "")
        for key in ("msg", "message", "error", "error_description")
    )


def credentials_rejected(data: dict) -> bool:
    """True when the IdP response text explicitly marks a credential mismatch."""
    return any(marker in _payload_text(data) for marker in CREDENTIALS_REJECTED_MARKERS)


def challenge_required(data: dict) -> bool:
    """True when the IdP response text marks an interactive security challenge."""
    return any(marker in _payload_text(data).casefold() for marker in CHALLENGE_MARKERS)


def account_locked(data: dict) -> bool:
    """True when the IdP response text marks the account as locked/frozen."""
    return any(marker in _payload_text(data) for marker in ACCOUNT_LOCKED_MARKERS)


def service_maintenance(data: dict) -> bool:
    """True when the IdP response text marks a service maintenance window."""
    return any(marker in _payload_text(data).casefold() for marker in SERVICE_MAINTENANCE_MARKERS)


def encrypt_idp_password(password: str, public_key_b64: str) -> str:
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + public_key_b64
        + "\n-----END PUBLIC KEY-----"
    )
    rsa_key = RSA.import_key(pem)
    cipher = PKCS1_v1_5.new(rsa_key)
    encrypted = cipher.encrypt(password.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def encrypt_host(hostname: str) -> str:
    """Encrypt hostname using AES-128-CFB for WebVPN URL encoding.

    Returns the hex-encoded ciphertext of the hostname.
    """
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    plaintext = hostname.encode("utf-8")
    encrypted = cipher.encrypt(plaintext)
    return hexlify(encrypted).decode("ascii")


def decrypt_host(ciphertext_hex: str) -> str:
    """Decrypt a WebVPN-encoded hostname."""
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    decrypted = cipher.decrypt(unhexlify(ciphertext_hex))
    return decrypted.decode("utf-8")


def get_vpn_url(url: str) -> str:
    """Convert a regular URL to its WebVPN proxy URL.

    Example:
        https://icourse.fudan.edu.cn/courseapi/v3/...
        ->
        https://webvpn.fudan.edu.cn/https/77726476706e69737468656265737421f9f44e.../courseapi/v3/...
    """
    parsed = urlparse(url)
    protocol = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment

    # Remove leading slash from path for concatenation
    path = path.lstrip("/")

    encrypted = encrypt_host(hostname)
    iv_hex = hexlify(config.WEBVPN_AES_IV).decode("ascii")

    # Include non-standard port
    port_suffix = ""
    if port and not (
        (protocol == "http" and port == 80)
        or (protocol == "https" and port == 443)
    ):
        port_suffix = f"-{port}"

    vpn_url = f"{config.WEBVPN_BASE}/{protocol}{port_suffix}/{iv_hex}{encrypted}"
    if path:
        vpn_url += f"/{path}"
    return vpn_url


def get_ordinary_url(vpn_url: str) -> str:
    """Convert a WebVPN URL back to the original URL."""
    parsed = urlparse(vpn_url)
    path_parts = parsed.path.strip("/").split("/", 2)
    if len(path_parts) < 2:
        raise ValueError(f"Invalid WebVPN URL: {vpn_url}")

    protocol_part = path_parts[0]  # e.g. "https" or "https-8080"
    encoded_host = path_parts[1]  # IV + ciphertext
    rest = path_parts[2] if len(path_parts) > 2 else ""

    # Parse protocol and optional port
    if "-" in protocol_part:
        protocol, port_str = protocol_part.rsplit("-", 1)
        port = f":{port_str}"
    else:
        protocol = protocol_part
        port = ""

    # Strip the 32-char IV hex prefix
    iv_hex_len = 32
    ciphertext_hex = encoded_host[iv_hex_len:]
    hostname = decrypt_host(ciphertext_hex)

    original = f"{protocol}://{hostname}{port}"
    if rest:
        original += f"/{rest}"
    if parsed.query:
        original += f"?{parsed.query}"
    return original


class _DeadlineSession(requests.Session):
    """Clamp every redirect/request to one shared authentication deadline."""

    def __init__(
        self,
        deadline: Callable[[], float | None],
        proxy_url: str = "",
        elapsed_callback: Callable[[str, int, str, int | None], None] | None = None,
    ):
        super().__init__()
        self.trust_env = False
        if proxy_url:
            self.proxies.update({"http": proxy_url, "https": proxy_url})
        self._deadline = deadline
        self.stage_label = ""
        self._elapsed_callback = elapsed_callback

    def send(self, request, **kwargs):
        # 测试模式出站门（src/runtime/test_mode.py）：未设 env 恒放行（发布版
        # 唯一分支）；拒绝 = egress_blocked 审计行 + 本族既有闭集传输失败形态
        # （ConnectionError），产品错误码集合零扩张。
        try:
            ensure_egress_allowed(getattr(request, "url", ""), purpose="webvpn")
        except EgressBlockedError as exc:
            raise requests.ConnectionError(str(exc)) from exc
        deadline = self._deadline()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("authentication deadline exceeded")
            requested = kwargs.get("timeout")
            if isinstance(requested, tuple):
                connect, read = requested
                kwargs["timeout"] = (
                    min(float(connect), remaining),
                    min(float(read), remaining),
                )
            elif requested is None:
                kwargs["timeout"] = remaining
            else:
                kwargs["timeout"] = min(float(requested), remaining)
        started = time.monotonic()
        try:
            response = super().send(request, **kwargs)
        except requests.Timeout:
            self._record_elapsed(started, "timeout", None)
            raise
        except requests.ConnectionError:
            self._record_elapsed(started, "connection_error", None)
            raise
        except requests.RequestException:
            self._record_elapsed(started, "request_error", None)
            raise
        self._record_elapsed(started, "ok", getattr(response, "status_code", None))
        return response

    def _record_elapsed(self, started: float, outcome: str, status: int | None) -> None:
        """Report per-request elapsed under the explicit stage label only."""
        callback = self._elapsed_callback
        if callback is None or outcome not in STAGE_OUTCOMES:
            return
        stage = str(self.stage_label or "")
        if not stage:
            return
        callback(stage, int(round((time.monotonic() - started) * 1000)), outcome, status)

    def ticket_session(self, transport: str = "curl_h2"):
        """Create a browser-compatible transport with the same cookies.

        代理环境显式化：直连 route 使用空串代理（libcurl 语义：CURLOPT_PROXY=""
        明确禁用代理查找，包括 http_proxy/HTTPS_PROXY 等环境变量）；proxy route
        只使用当前显式代理参数。curl_cffi 0.15.0 的 trust_env 只影响 CA bundle
        环境，不隔离代理，因此两者都设置。
        """
        if transport not in TICKET_TRANSPORTS:
            raise ValueError("ticket transport is unsupported")
        explicit_proxies = {key: value for key, value in self.proxies.items() if value}
        if not explicit_proxies:
            explicit_proxies = {"http": "", "https": ""}
        session = curl_requests.Session(
            impersonate="chrome",
            trust_env=False,
            proxies=explicit_proxies,
        )
        if transport == "curl_h1":
            session.curl_options[CurlOpt.HTTP_VERSION] = int(CurlHttpVersion.V1_1)
        session.headers.update(dict(self.headers))
        self.sync_ticket_cookies(session)
        return session

    def sync_ticket_cookies(self, session) -> None:
        """把 requests 主会话的当前 cookie 刷新进持久 ticket transport。

        每条票腿开始前调用：WebVPN 腿之后 requests 会话可能经 Set-Cookie
        轮换过 cookie（如 iCourse 腿前的 6+ 个请求），持久 curl jar 若只保留
        创建时的快照会带着过期 cookie 消费下一张 ticket。方向始终显式：
        腿前 requests→curl，腿后 curl→requests（merge_ticket_cookies）。
        """
        for cookie in self.cookies:
            session.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain or None,
                path=cookie.path or "/",
            )

    def merge_ticket_cookies(self, session) -> None:
        jar = getattr(getattr(session, "cookies", None), "jar", ())
        for cookie in jar:
            self.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain or None,
                path=cookie.path or "/",
            )


class WebVPNSession:
    """Manages a WebVPN session with full IDP authentication."""

    TICKET_TIMEOUT = (5, 12)
    VERIFY_TIMEOUT = (3, 5)
    AUTH_TIMEOUT = (5, 15)
    AUTH_DEADLINE_SECONDS = 45
    MAX_AUTH_REDIRECTS = 8
    MAX_TICKET_REDIRECTS = 6
    TICKET_REDIRECT_HOSTS = frozenset({
        "id.fudan.edu.cn",
        "icourse.fudan.edu.cn",
        "webvpn.fudan.edu.cn",
    })
    ALLOWED_TARGET_HOSTS = frozenset({
        "fdjwgl.fudan.edu.cn",
        "yjsxktest.fudan.sh.cn",
    })

    def __init__(
        self,
        step_callback: Callable[[str, dict], None] | None = None,
        *,
        proxy_url: str = "",
        transport: str = "curl_h2",
    ):
        if transport not in TICKET_TRANSPORTS:
            raise ValueError("ticket transport is unsupported")
        self._proxy_url = proxy_url or ""
        self._auth_deadline: float | None = None
        self._stage_label = ""
        self.session = _DeadlineSession(
            lambda: self._auth_deadline,
            proxy_url,
            elapsed_callback=self._report_request_elapsed if step_callback else None,
        )
        self.session.max_redirects = self.MAX_AUTH_REDIRECTS
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.logged_in = False
        self._step_callback = step_callback
        self._transport_class = transport
        self._ticket_transport = None
        # P3.1 附件：只读安全 GET 的恢复钩子（应用持有本会话时绑定）；
        # None 时 get_allowed 保持既有 fail-closed 行为。
        self.safe_get_recovery = None

    def begin_request_deadline(self, timeout_seconds: float) -> None:
        duration = float(timeout_seconds)
        if duration <= 0:
            raise ValueError("request deadline must be positive")
        self._auth_deadline = time.monotonic() + duration

    def end_request_deadline(self) -> None:
        self._auth_deadline = None

    def begin_authentication(self, timeout_seconds: float | None = None) -> None:
        self.begin_request_deadline(float(timeout_seconds or self.AUTH_DEADLINE_SECONDS))

    def end_authentication(self) -> None:
        self.end_request_deadline()

    # --- 持久 ticket transport：创建一次、预热一次、owner 关闭时恰好关闭一次 ---

    def _transport_fields(self) -> dict:
        """闭集遥测字段：route/transport 类别。绝不包含代理 URL 或账号数据。"""
        return {
            "route": "proxy" if self._proxy_url else "direct",
            "transport": self._transport_class,
        }

    def _ensure_ticket_transport(self):
        """Return the persistent curl ticket transport, creating and warming it once.

        测试/替代 session（非 _DeadlineSession）返回 None：票腿沿用该 session 本体，
        不注入预热请求（既有 scripted 序列保持不变）。
        """
        if not isinstance(self.session, _DeadlineSession):
            return None
        if self._ticket_transport is None:
            transport = self.session.ticket_session(self._transport_class)
            try:
                self._warm_ticket_transport(transport)
            except Exception:
                self._close_transport_quietly(transport)
                raise
            self._ticket_transport = transport
        return self._ticket_transport

    def prepare_ticket_transport(self):
        """Warm the ticket transport before any ticket exists.

        预热失败发生在任何 ticket 产生之前：同 attempt 内换备用 transport 重试
        恰好一轮；仍失败则向上抛出，由外层重试换下一个候选（route, transport）。
        """
        if not isinstance(self.session, _DeadlineSession):
            return None
        try:
            return self._ensure_ticket_transport()
        except requests.RequestException:
            if self._transport_class == TICKET_TRANSPORTS[0]:
                self._transport_class = TICKET_TRANSPORTS[1]
                return self._ensure_ticket_transport()
            raise

    def _warm_ticket_transport(self, transport) -> None:
        """对 WebVPN 根路径做一次不携带任何 ticket/凭据的传输预热。

        任何 HTTP 响应（含认证前预期的 302→/login）都算预热成功；只有传输层
        异常才算失败。预热绝不被解释为认证成功。与 _verify_* 探针一致，
        结束后恢复前一阶段标签，不污染后续 IdP 请求的 per-request elapsed。
        """
        started = time.monotonic()
        previous = self._stage_label
        self._set_stage("webvpn_ticket_warmup")
        response = None
        try:
            # 测试模式出站门（curl 票腿预热；src/runtime/test_mode.py）——
            # 拒绝翻译成本族传输失败闭集（CurlRequestException→connection_error）。
            try:
                ensure_egress_allowed(config.WEBVPN_BASE, purpose="webvpn_ticket_warmup")
            except EgressBlockedError as exc:
                raise CurlRequestException(str(exc)) from exc
            # 非流式：只有会话 handle 的持久连接缓存能让真实票腿复用预热连接
            # （stream=True 走 duphandle 独立缓存）。预热响应正文只用于传输，
            # 绝不解析；总时长受 VERIFY_TIMEOUT 的 connect+read 硬上限约束。
            response = transport.get(
                config.WEBVPN_BASE + "/",
                allow_redirects=False,
                stream=False,
                timeout=self.VERIFY_TIMEOUT,
            )
            status = int(getattr(response, "status_code", 0) or 0)
        except CurlTimeout as exc:
            self._report_warmup_elapsed(started, "timeout")
            raise requests.ReadTimeout("ticket transport warmup timed out") from exc
        except CurlRequestException as exc:
            self._report_warmup_elapsed(started, "connection_error")
            raise requests.ConnectionError("ticket transport warmup failed") from exc
        except requests.RequestException as exc:
            self._report_warmup_elapsed(
                started,
                "timeout" if isinstance(exc, requests.Timeout) else "connection_error",
            )
            raise
        else:
            self._report_warmup_elapsed(started, "ok", status)
        finally:
            self._set_stage(previous)
            if response is not None:
                response.close()

    def _report_warmup_elapsed(self, started: float, outcome: str, status: int | None = None) -> None:
        details: dict = {
            "duration_ms": int(round((time.monotonic() - started) * 1000)),
            "outcome": outcome,
        }
        if isinstance(status, int) and not isinstance(status, bool) and status > 0:
            details["status"] = status
        self._report_step("webvpn_ticket_warmup", **details, **self._transport_fields())

    @staticmethod
    def _close_transport_quietly(transport) -> None:
        try:
            transport.close()
        except Exception:
            pass  # 关闭失败绝不掩盖登录失败的主因

    def close(self) -> None:
        """Close both transports exactly once; never raises.

        调用方（登录失败清理、客户端弃用）只守 (AttributeError, OSError)，
        因此这里必须吞掉一切内部异常，避免掩盖 last_error 或跳过
        credentials_rejected 分支。
        """
        transport, self._ticket_transport = self._ticket_transport, None
        if transport is not None:
            self._close_transport_quietly(transport)
        try:
            self.session.close()
        except Exception:
            pass

    def _report_step(self, step: str, **details) -> None:
        """Report sanitized handshake timing without exposing request data."""
        callback = self._step_callback
        if callback is not None:
            callback(step, details)

    def _set_stage(self, stage: str) -> None:
        """Label the current authentication step for per-request elapsed only."""
        self._stage_label = stage
        session = self.session
        if session is not None:
            try:
                session.stage_label = stage
            except AttributeError:
                pass

    def _report_request_elapsed(
        self, stage: str, duration_ms: int, outcome: str, status: int | None
    ) -> None:
        """Forward transport elapsed as a closed-set stage record; no request data."""
        if self._step_callback is None or not stage:
            return
        details: dict = {"duration_ms": int(duration_ms), "outcome": outcome}
        if isinstance(status, int) and not isinstance(status, bool):
            details["status"] = status
        self._report_step(stage, **details)

    def login(self, student_id: str = None, password: str = None) -> bool:
        """Execute the full 7-step IDP authentication flow.

        Returns True on success, raises on failure.
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        if not student_id or not password:
            raise ValueError(
                "Student ID and password are required. "
                "Set STUID and UISPsw environment variables."
            )

        # 传输预热：在任何凭据提交或 ticket 产生之前完成 DNS/TCP/TLS/代理握手，
        # 让真实 ticket 请求复用已建立连接。预热失败不消耗任何 ticket，
        # 同 attempt 内先换备用 transport 一轮，仍失败向上抛出。
        print("[*] Warming ticket transport...")
        self.prepare_ticket_transport()

        print("[1/7] Getting authentication context...")
        self._set_stage("webvpn_auth_context")
        lck, entity_id = self._get_auth_context()

        print("[2/7] Querying authentication methods...")
        self._set_stage("webvpn_auth_methods")
        auth_chain_code, request_type = self._query_auth_methods(lck, entity_id)

        print("[3/7] Getting RSA public key...")
        self._set_stage("webvpn_public_key")
        pub_key_pem = self._get_public_key()

        print("[4/7] Encrypting password...")
        encrypted_password = self._encrypt_password(password, pub_key_pem)

        print("[5/7] Executing authentication...")
        self._set_stage("webvpn_auth_execute")
        login_token = self._auth_execute(
            student_id,
            encrypted_password,
            lck,
            entity_id,
            auth_chain_code,
            request_type,
        )

        print("[6/7] Getting CAS ticket...")
        self._set_stage("webvpn_cas_ticket")
        ticket_url = self._get_cas_ticket(login_token)

        print("[7/7] Establishing WebVPN session...")
        self._set_stage("")
        self._establish_session(ticket_url)

        self.logged_in = True
        print("[*] WebVPN login successful!")
        return True

    def authenticate_icourse(
        self, student_id: str = None, password: str = None
    ) -> bool:
        """Authenticate to iCourse via CAS/IDP through WebVPN.

        Mimics the browser flow:
        1. Access casapi login URL (like clicking "校内用户登录")
        2. Follow redirect to IDP authenticate (casapi generates correct
           service URL with forward param and r=auth/login)
        3. IDP auth steps through WebVPN
        4. Follow ticket back to iCourse through WebVPN
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        print("[*] Starting iCourse CAS authentication through WebVPN...")

        # Pre-flight: probe the WebVPN portal.  A cold session redirects to
        # /login instantly (status 302); a hot session returns 200.  Fail
        # fast on cold — login_with_retry() in main.py will re-login.
        if not self._verify_webvpn_session(stage="icourse_preflight_probe"):
            raise RuntimeError("WebVPN session cold — re-login needed")

        idp_vpn_base = get_vpn_url(config.IDP_BASE)

        # Step 1: Initiate CAS login via casapi.  Use allow_redirects=True
        # so requests follows the full redirect chain like a browser.
        print("[1/7] Initiating CAS login via casapi...")
        self._set_stage("icourse_casapi")
        casapi_url = (
            f"{config.ICOURSE_BASE}/casapi/index.php"
            f"?r=auth/login&school_login=1"
            f"&tenant_code={config.TENANT_CODE}"
            f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
        )
        vpn_url = get_vpn_url(casapi_url)

        resp = self.session.get(vpn_url, allow_redirects=True, timeout=self.AUTH_TIMEOUT)
        lck = None
        for source in [resp.url] + [
            h.headers.get("Location", "") for h in (resp.history or [])
        ]:
            m = re.search(r'lck=([^&#"]+)', source)
            if m:
                lck = m.group(1)
                break
        if not lck:
            m = re.search(r'lck=([^&#"]+)', resp.text[:5000])
            if m:
                lck = m.group(1)
        if not lck:
            raise RuntimeError(
                "Failed to extract lck from CAS redirect chain "
                f"(status={resp.status_code})"
            )

        entity_id = config.ICOURSE_BASE
        print("    lck: OK")

        # Step 2: Query auth methods (through WebVPN)
        print("[2/7] Querying auth methods (via WebVPN)...")
        self._set_stage("icourse_auth_methods")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/queryAuthMethods")
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break
        if not auth_chain_code:
            raise RuntimeError("No authChainCode found in response")
        print("    authChainCode: OK")

        # Step 3: Get RSA public key (through WebVPN)
        print("[3/7] Getting RSA public key (via WebVPN)...")
        self._set_stage("icourse_public_key")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/getJsPublicKey")
        resp = self.session.get(
            url,
            headers={"Referer": f"{idp_vpn_base}/ac/"},
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()
        pub_key_b64 = data.get("data", "")
        if not pub_key_b64:
            raise RuntimeError("Failed to get public key via WebVPN")
        print("    Got RSA public key")

        # Step 4: Encrypt password
        print("[4/7] Encrypting password...")
        encrypted_password = self._encrypt_password(password, pub_key_b64)

        # Step 5: Execute authentication (through WebVPN)
        print("[5/7] Executing authentication (via WebVPN)...")
        self._set_stage("icourse_auth_execute")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/authExecute")
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()

        if str(data.get("code")) != "200":
            if service_maintenance(data):
                raise FudanServiceMaintenance(
                    f"iCourse CAS auth hit a maintenance window (code={data.get('code')})"
                )
            if account_locked(data):
                raise FudanAccountLocked(
                    f"iCourse CAS auth reports the account as locked (code={data.get('code')})"
                )
            if credentials_rejected(data):
                raise FudanCredentialsRejected(
                    f"iCourse CAS auth failed (code={data.get('code')})"
                )
            if challenge_required(data):
                raise FudanChallengeRequired(
                    f"iCourse CAS auth requires a security challenge (code={data.get('code')})"
                )
            raise RuntimeError(
                f"iCourse CAS auth failed (code={data.get('code')})"
            )

        login_token = data.get("loginToken", "")
        if not login_token:
            raise RuntimeError("No loginToken in iCourse CAS response")
        print("    loginToken: OK")

        # Step 6: Get CAS ticket (through WebVPN)
        print("[6/7] Getting CAS ticket (via WebVPN)...")
        self._set_stage("icourse_cas_ticket")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authCenter/authnEngine")
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )
        html = resp.text

        # Extract ticket URL from the authnEngine response
        # The URL may already be rewritten to a WebVPN URL by the proxy
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )
        if not ticket_match:
            raise RuntimeError(
                f"Failed to extract iCourse ticket URL (response length: {len(html)})"
            )

        ticket_url = self._validate_ticket_redirect_url(
            html_mod.unescape(ticket_match.group(1))
        )
        print("    Ticket extracted.")

        # Step 7: Follow ticket to iCourse (through WebVPN)
        print("[7/7] Following ticket to iCourse (via WebVPN)...")
        self._set_stage("")
        if not ticket_url.startswith(config.WEBVPN_BASE):
            ticket_url = get_vpn_url(ticket_url)

        self._establish_icourse_session(ticket_url)
        print("    Verified: login OK")
        print("[*] iCourse authentication successful!")
        return True

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.get(vpn_url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """POST request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.post(vpn_url, **kwargs)

    @classmethod
    def validate_allowed_target(cls, url: str) -> str:
        """Validate a private-client platform URL before WebVPN conversion.

        Timetable acquisition has a deliberately closed host set.  The
        postgraduate service still publishes an HTTP origin, but all calls are
        converted to the HTTPS WebVPN endpoint before they leave this client.
        """
        try:
            parsed = urlparse(str(url or ""))
        except ValueError as exc:
            raise ValueError("Platform target URL is invalid") from exc
        hostname = str(parsed.hostname or "").casefold()
        if (
            parsed.scheme not in {"http", "https"}
            or hostname not in cls.ALLOWED_TARGET_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Platform target is not allowed")
        return hostname

    def get_allowed(self, url: str, **kwargs) -> requests.Response:
        """GET an allowlisted teaching-platform URL through WebVPN only.

        P3.1 附件：只读安全 GET 共享有界恢复——传输失败或落在登录页的响应，
        通过应用侧已验证的会话纪元（safe_get_recovery 钩子）恰重试一次；
        钩子缺失或失败时保持既有 fail-closed 行为。凭据/挑战/身份/维护/
        不安全跳转/已消费 ticket 等终止性失败在应用航班内即终止，绝不重试。
        同一会话仅传输类瞬时失败重试（纪元已验证）；过期响应只在纪元真正
        重建后重试。state-changing 请求（post/post_raw）绝不继承自动重放；
        上游主机仍只由 ALLOWED_TARGET_HOSTS 显式闭集放行。
        """
        self.validate_allowed_target(url)
        transport_error = None
        response = None
        try:
            response = self.get(url, **kwargs)
        except requests.RequestException as exc:
            transport_error = exc
        if transport_error is None and not self._is_login_target(
            str(getattr(response, "url", "") or "")
        ):
            return response
        recovery = getattr(self, "safe_get_recovery", None)
        recovered = recovery() if callable(recovery) else None
        if recovered is None:
            if transport_error is not None:
                raise transport_error
            return response
        if recovered is self and transport_error is None:
            # 纪元已验证但未重建：过期响应不会改变，按原样交还（fail-closed）。
            return response
        return recovered.get(url, **kwargs)

    def get_raw(self, url: str, **kwargs) -> requests.Response:
        """GET request without URL conversion (for already-converted URLs)."""
        kwargs.setdefault("timeout", 30)
        return self.session.get(url, **kwargs)

    def post_raw(self, url: str, **kwargs) -> requests.Response:
        """POST request without URL conversion."""
        kwargs.setdefault("timeout", 30)
        return self.session.post(url, **kwargs)

    # --- Private authentication steps ---

    def _get_auth_context(self) -> tuple[str, str]:
        """Step 1: GET authenticate endpoint, extract lck from redirect."""
        service_url = f"{config.WEBVPN_BASE}/login?cas_login=true"
        url = (
            f"{config.IDP_BASE}/idp/authCenter/authenticate"
            f"?service={quote(service_url, safe='')}"
        )
        resp = self.session.get(url, allow_redirects=False, timeout=self.AUTH_TIMEOUT)

        # Follow redirects manually to extract lck
        location = resp.headers.get("Location", "")
        redirects = 0
        while resp.status_code in (301, 302) and "lck=" not in location:
            if redirects >= self.MAX_AUTH_REDIRECTS:
                raise RuntimeError("Authentication redirect limit exceeded")
            resp = self.session.get(location, allow_redirects=False, timeout=self.AUTH_TIMEOUT)
            location = resp.headers.get("Location", "")
            redirects += 1

        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")

        # Extract lck parameter
        lck_match = re.search(r"[?&]lck=([^&]+)", location)
        if not lck_match:
            raise RuntimeError(
                f"Failed to extract lck from redirect (status={resp.status_code})"
            )

        lck = lck_match.group(1)
        entity_id = config.WEBVPN_BASE
        print("    lck: OK")
        return lck, entity_id

    def _query_auth_methods(
        self, lck: str, entity_id: str
    ) -> tuple[str, str]:
        """Step 2: Query available authentication methods."""
        url = f"{config.IDP_BASE}/idp/authn/queryAuthMethods"
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()

        # data["data"] is a list of auth methods; pick the userAndPwd one
        # authChainCode for userAndPwd is in the list items;
        # requestType is at the top level
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break

        if not auth_chain_code:
            raise RuntimeError("Failed to get authChainCode")

        print("    authChainCode: OK")
        return auth_chain_code, request_type

    def _get_public_key(self) -> str:
        """Step 3: Get RSA public key for password encryption."""
        url = f"{config.IDP_BASE}/idp/authn/getJsPublicKey"
        resp = self.session.get(
            url,
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
            },
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()
        pub_key_b64 = data.get("data", "")
        if not pub_key_b64:
            raise RuntimeError("Failed to get public key")

        print("    Got RSA public key")
        return pub_key_b64

    def _encrypt_password(self, password: str, pub_key_b64: str) -> str:
        """Step 4: RSA-encrypt the password with PKCS1_v1_5."""
        return encrypt_idp_password(password, pub_key_b64)

    def _auth_execute(
        self,
        student_id: str,
        encrypted_password: str,
        lck: str,
        entity_id: str,
        auth_chain_code: str,
        request_type: str,
    ) -> str:
        """Step 5: Execute authentication and get loginToken."""
        url = f"{config.IDP_BASE}/idp/authn/authExecute"
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )
        data = resp.json()

        if str(data.get("code")) != "200":
            details = {
                key: data.get(key)
                for key in ("code", "msg", "message", "error", "error_description")
                if data.get(key)
            }
            if service_maintenance(data):
                raise FudanServiceMaintenance(
                    f"Authentication hit a maintenance window: {details or {'code': data.get('code')}}"
                )
            if account_locked(data):
                raise FudanAccountLocked(
                    f"Authentication reports the account as locked: {details or {'code': data.get('code')}}"
                )
            if credentials_rejected(data):
                raise FudanCredentialsRejected(
                    f"Authentication failed: {details or {'code': data.get('code')}}"
                )
            if challenge_required(data):
                raise FudanChallengeRequired(
                    f"Authentication requires a security challenge: {details or {'code': data.get('code')}}"
                )
            raise RuntimeError(
                f"Authentication failed: {details or {'code': data.get('code')}}"
            )

        # loginToken is at top level, not nested under "data"
        login_token = data.get("loginToken", "")
        if not login_token:
            raise RuntimeError("No loginToken in response")

        print("    loginToken: OK")
        return login_token

    def _get_cas_ticket(self, login_token: str) -> str:
        """Step 6: Exchange loginToken for a CAS ticket URL."""
        url = f"{config.IDP_BASE}/idp/authCenter/authnEngine"
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=self.AUTH_TIMEOUT,
        )

        # The response is HTML containing a JS redirect with the ticket URL
        html = resp.text

        # Extract the locationValue from the JavaScript
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            # Fallback: any URL with ticket= parameter
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )

        if not ticket_match:
            raise RuntimeError(
                f"Failed to extract ticket URL (response length: {len(html)})"
            )

        ticket_url = ticket_match.group(1)
        # Unescape HTML entities (e.g., &amp; -> &)
        ticket_url = html_mod.unescape(ticket_url)
        print("    Ticket extracted.")
        return ticket_url

    @staticmethod
    def _is_login_target(value: str) -> bool:
        try:
            path = urlparse(str(value or "")).path.rstrip("/").lower()
        except ValueError:
            return False
        return path == "/login" or path.endswith("/login")

    @classmethod
    def _validate_ticket_redirect_url(cls, value: str) -> str:
        try:
            parsed = urlparse(str(value or ""))
            port = parsed.port
        except ValueError as exc:
            raise UnsafeTicketRedirectError() from exc
        if (
            parsed.scheme != "https"
            or str(parsed.hostname or "").casefold() not in cls.TICKET_REDIRECT_HOSTS
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UnsafeTicketRedirectError()
        return str(value)

    def _follow_ticket_redirects(self, ticket_url: str) -> tuple[int, int]:
        """Consume a single-use CAS ticket without parsing response bodies.

        票腿使用 WebVPNSession 持有的持久 curl transport（正常路径已在 ticket
        产生前由 login()/prepare_ticket_transport() 创建并预热；此处兜底懒创建）。
        请求为非流式：curl_cffi 0.15.0 的 stream=True 走 duphandle 独立连接缓存，
        会破坏预热连接复用；非流式让整条腿复用同一条已预热连接，且 (connect,
        read) 二元 timeout 直接下发给 curl_cffi（非流式 = connect+read 的硬性
        总上限）。正文只读取 status/Location/URL，绝不解析或记录。
        cookie 在票腿完成后回并到 requests 会话；transport 由 owner close() 关闭。
        """
        current = self._validate_ticket_redirect_url(ticket_url)
        redirects = 0
        redirect_statuses = {301, 302, 303, 307, 308}
        ticket_transport = None
        transport = self.session
        if isinstance(self.session, _DeadlineSession):
            ticket_transport = self._ensure_ticket_transport()
            transport = ticket_transport or self.session
            if ticket_transport is not None:
                self.session.sync_ticket_cookies(ticket_transport)
        try:
            while True:
                try:
                    # 测试模式出站门（curl 票腿；src/runtime/test_mode.py）——
                    # 拒绝翻译成本族传输失败闭集（requests.ConnectionError）。
                    try:
                        ensure_egress_allowed(current, purpose="webvpn_ticket")
                    except EgressBlockedError as exc:
                        raise requests.ConnectionError(str(exc)) from exc
                    response = transport.get(
                        current,
                        allow_redirects=False,
                        stream=False,
                        timeout=self.TICKET_TIMEOUT,
                    )
                except CurlTimeout as exc:
                    raise requests.ReadTimeout("ticket transport timed out") from exc
                except CurlRequestException as exc:
                    raise requests.ConnectionError("ticket transport failed") from exc
                try:
                    status = int(response.status_code)
                    location = response.headers.get("Location", "")
                    response_url = str(getattr(response, "url", "") or current)
                finally:
                    response.close()
                if status not in redirect_statuses or not location:
                    if self._is_login_target(response_url):
                        raise TicketRejectionError("Ticket redirect returned a login page")
                    return status, redirects
                if redirects >= self.MAX_TICKET_REDIRECTS:
                    raise TicketRejectionError("Ticket redirect limit exceeded")
                current = self._validate_ticket_redirect_url(urljoin(current, location))
                redirects += 1
        finally:
            if ticket_transport is not None:
                self.session.merge_ticket_cookies(ticket_transport)

    def _verify_webvpn_session(self, stage: str = "webvpn_portal_probe") -> bool:
        """Probe only response headers so login-page bodies cannot block startup."""
        previous = self._stage_label
        if stage:
            self._set_stage(stage)
        response = None
        try:
            response = self.session.get(
                config.WEBVPN_BASE + "/",
                allow_redirects=False,
                stream=True,
                timeout=self.VERIFY_TIMEOUT,
            )
            location = response.headers.get("Location", "")
            response_url = str(getattr(response, "url", "") or config.WEBVPN_BASE)
            return (
                response.status_code == 200
                and not self._is_login_target(response_url)
                and not self._is_login_target(urljoin(response_url, location))
            )
        except requests.RequestException:
            return False
        finally:
            if stage:
                self._set_stage(previous)
            if response is not None:
                response.close()

    def _verify_icourse_session(self, stage: str = "icourse_portal_probe") -> bool:
        previous = self._stage_label
        if stage:
            self._set_stage(stage)
        response = None
        try:
            test_url = get_vpn_url(
                f"{config.ICOURSE_BASE}/userapi/v1/infosimple"
            )
            response = self.session.get(
                test_url,
                stream=True,
                timeout=self.VERIFY_TIMEOUT,
            )
            if response.status_code != 200 or self._is_login_target(
                str(getattr(response, "url", ""))
            ):
                return False
            data = response.json()
            return isinstance(data, dict) and data.get("code") in (0, 200)
        except (requests.RequestException, ValueError):
            return False
        finally:
            if stage:
                self._set_stage(previous)
            if response is not None:
                response.close()

    def _establish_icourse_session(self, ticket_url: str) -> None:
        """Consume one iCourse ticket and recover only from observed session state."""
        started = time.monotonic()
        try:
            status, redirects = self._follow_ticket_redirects(ticket_url)
        except requests.RequestException as exc:
            # The final redirect can commit the session before its response
            # stalls. Verify the resulting state, but never replay the ticket.
            if self._verify_icourse_session():
                elapsed = round(time.monotonic() - started, 3)
                self._report_step(
                    "icourse_ticket_complete",
                    elapsed_seconds=elapsed,
                    redirects=None,
                    recovered_after=type(exc).__name__,
                )
                print(f"    iCourse session verified after {type(exc).__name__} ({elapsed:.1f}s).")
                return
            self._report_step(
                "icourse_ticket_error",
                elapsed_seconds=round(time.monotonic() - started, 3),
                error_type=type(exc).__name__,
            )
            raise TicketTransportError(
                f"iCourse ticket exchange failed ({type(exc).__name__})",
                ICOURSE_TICKET_TRANSPORT_CODE,
            ) from exc
        if not 200 <= status < 300:
            raise TicketRejectionError(
                f"iCourse ticket exchange failed (status={status})"
            )
        # 决策记录低风险辅项：走到这里即票链正常完成（上方 except 的恢复路径已由
        # _verify_icourse_session 裁决并提前返回/抛出）。正常完成自身 2xx 校验 +
        # cookie 回并已证明会话状态，不再发确认探针；若会话仍异常，首次真实使用
        # 会以既有诚实错误路径暴露。
        elapsed = round(time.monotonic() - started, 3)
        self._report_step(
            "icourse_ticket_complete",
            elapsed_seconds=elapsed,
            redirects=redirects,
        )
        print(f"    Ticket accepted in {elapsed:.1f}s ({redirects} redirects).")

    def _establish_session(self, ticket_url: str):
        """Step 7: consume the ticket once, then verify the resulting session."""
        started = time.monotonic()
        try:
            status, redirects = self._follow_ticket_redirects(ticket_url)
        except requests.RequestException as exc:
            # A read timeout can happen after the server has already set the
            # session cookie.  Verify that state, but never replay the ticket.
            if self._verify_webvpn_session():
                elapsed = round(time.monotonic() - started, 3)
                self._report_step(
                    "webvpn_ticket_complete",
                    elapsed_seconds=elapsed,
                    redirects=None,
                    recovered_after=type(exc).__name__,
                )
                print(f"    Session verified after {type(exc).__name__} ({elapsed:.1f}s).")
                return
            self._report_step(
                "webvpn_ticket_error",
                elapsed_seconds=round(time.monotonic() - started, 3),
                error_type=type(exc).__name__,
            )
            raise TicketTransportError(
                f"WebVPN ticket exchange failed ({type(exc).__name__})",
                WEBVPN_TICKET_TRANSPORT_CODE,
            ) from exc
        if not 200 <= status < 300:
            raise TicketRejectionError(
                f"Failed to establish WebVPN session (status={status})"
            )
        # 决策记录低风险辅项：走到这里即票链正常完成（上方 except 的恢复路径已由
        # _verify_webvpn_session 裁决并提前返回/抛出）。正常完成自身 2xx 校验 +
        # cookie 回并已证明会话状态，不再发确认探针；若会话仍异常，首次真实使用
        # 会以既有诚实错误路径暴露。
        elapsed = round(time.monotonic() - started, 3)
        self._report_step(
            "webvpn_ticket_complete",
            elapsed_seconds=elapsed,
            redirects=redirects,
        )
        print(f"    Session established in {elapsed:.1f}s ({redirects} redirects).")

    # --- 会话检查点（V5）：最小、主机限定、可 DPAPI 持久化的 cookie 子集 ---

    def capture_session_checkpoint_cookies(self) -> list[dict[str, str]]:
        """Extract the minimal host-scoped cookie set able to restore this session.

        只收集 WebVPN 会话主机名下的 cookie——所有已认证请求（WebVPN 与被代理
        的 iCourse）的会话状态都落在这一台主机——绝不收集其它域，绝不含 URL、
        header、页面状态或整只跨域 cookie jar。返回值只能交给
        CredentialStore.save_session_checkpoint 做 DPAPI 加密持久化；调用方
        负责在保存失败时静默放弃（检查点纯尽力而为）。
        """
        if not self.logged_in:
            return []
        jar = getattr(self.session, "cookies", None)
        if jar is None:
            return []
        captured: list[dict[str, str]] = []
        # requests 的 RequestsCookieJar 直接迭代即产出 Cookie 对象。
        for cookie in jar:
            domain = str(getattr(cookie, "domain", "") or "").casefold().lstrip(".")
            if domain != CHECKPOINT_COOKIE_HOST:
                continue
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            path = str(getattr(cookie, "path", "") or "/")
            if not name or not value or "\r" in value or "\n" in value:
                continue
            captured.append({
                "name": name[:128],
                "value": value[:4096],
                "domain": domain,
                "path": (path[:256] or "/"),
            })
            if len(captured) >= CHECKPOINT_MAX_COOKIES:
                break
        return captured

    def restore_session_checkpoint(self, cookies: list[dict[str, str]]) -> bool:
        """Install a saved cookie set and verify both sessions accept it.

        绝不抛出：形状、安装或服务端探针的任何问题都返回 False，让调用方
        丢弃检查点并退回普通登录。绝不提交任何凭据、绝不重试；两个探针
        复用既有闭集阶段标签（webvpn/icourse_portal_probe），遥测零新增。
        """
        jar = getattr(self.session, "cookies", None)
        if jar is None or not isinstance(cookies, list) or not cookies:
            return False
        try:
            for cookie in cookies:
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if not name or not value:
                    return False
                domain = str(cookie.get("domain") or "") or None
                path = str(cookie.get("path") or "") or "/"
                jar.set(name, value, domain=domain, path=path)
        except Exception:
            return False
        # 短路：WebVPN 探针已拒绝时不再发 iCourse 探针（会话已死，少一个请求）。
        return self._verify_webvpn_session() and self._verify_icourse_session()
