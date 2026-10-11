"""Direct, bearer-authenticated iCourse catalog session."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from urllib.parse import quote, unquote, urljoin, urlparse

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from src.api.webvpn import encrypt_idp_password
from src.runtime import config
from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed


_TOKEN_RE = re.compile(
    r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}'
)
_TICKET_PATTERNS = (
    re.compile(r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"'),
    re.compile(r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)'),
)


class DirectICourseError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class DirectICourseSession:
    """Authenticate the identity-scoped catalog without exposing its bearer."""

    REQUEST_TIMEOUT_SECONDS = 15.0
    AUTH_DEADLINE_SECONDS = 45.0
    MAX_REDIRECTS = 8

    def __init__(
        self,
        step_callback: Callable[[str], None] | None = None,
        *,
        proxy_url: str = "",
    ):
        self.session = curl_requests.Session(
            impersonate="chrome",
            trust_env=False,
            # curl_cffi only treats an empty proxy as an explicit bypass; None
            # may still inherit a user environment proxy.
            proxies={"http": proxy_url, "https": proxy_url} if proxy_url else {"http": "", "https": ""},
        )
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self._deadline: float | None = None
        self._bearer = ""
        self._userinfo: dict = {}
        self._step_callback = step_callback
        self._allowed_hosts = frozenset({
            str(urlparse(config.IDP_BASE).hostname or "").casefold(),
            str(urlparse(config.ICOURSE_BASE).hostname or "").casefold(),
        })

    @property
    def userinfo(self) -> dict:
        return dict(self._userinfo)

    def begin_request_deadline(self, timeout_seconds: float) -> None:
        duration = float(timeout_seconds)
        if duration <= 0:
            raise ValueError("request deadline must be positive")
        self._deadline = time.monotonic() + duration

    def end_request_deadline(self) -> None:
        self._deadline = None

    def _report(self, step: str) -> None:
        if self._step_callback is not None:
            self._step_callback(step)

    def _timeout(self) -> float:
        if self._deadline is None:
            return self.REQUEST_TIMEOUT_SECONDS
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise DirectICourseError("timeout")
        return min(self.REQUEST_TIMEOUT_SECONDS, remaining)

    def _validate_url(self, url: str, *, icourse_only: bool = False) -> str:
        try:
            parsed = urlparse(str(url or ""))
        except ValueError as exc:
            raise DirectICourseError("catalog_target_invalid") from exc
        hostname = str(parsed.hostname or "").casefold()
        expected = str(urlparse(config.ICOURSE_BASE).hostname or "").casefold()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or hostname not in self._allowed_hosts
            or (icourse_only and hostname != expected)
        ):
            raise DirectICourseError("catalog_target_invalid")
        return str(url)

    def _request(self, method: str, url: str, **kwargs):
        target = self._validate_url(url)
        requested_timeout = kwargs.get("timeout")
        if isinstance(requested_timeout, tuple):
            requested_timeout = max(float(value) for value in requested_timeout)
        kwargs["timeout"] = min(
            float(requested_timeout or self._timeout()),
            self._timeout(),
        )
        kwargs.setdefault("max_redirects", self.MAX_REDIRECTS)
        try:
            # 测试模式出站门（src/runtime/test_mode.py）：拒绝翻译成本族闭集
            # 传输失败 network_unavailable（审计行已携 egress_blocked+host）。
            try:
                ensure_egress_allowed(target, purpose="icourse_direct")
            except EgressBlockedError as exc:
                raise CurlRequestException(str(exc)) from exc
            response = self.session.request(method, target, **kwargs)
            self._validate_redirect_chain(response)
            response.raise_for_status()
            return response
        except DirectICourseError:
            raise
        except CurlTimeout as exc:
            raise DirectICourseError("timeout") from exc
        except CurlRequestException as exc:
            raise DirectICourseError("network_unavailable") from exc

    def _validate_redirect_chain(self, response) -> None:
        """Reject any redirect that leaves the closed IDP/iCourse host set."""
        chain = [*(getattr(response, "history", None) or []), response]
        try:
            for item in chain:
                item_url = self._validate_url(
                    str(getattr(item, "url", "") or "")
                )
                location = str(
                    (getattr(item, "headers", None) or {}).get("Location") or ""
                )
                if location:
                    self._validate_url(urljoin(item_url, location))
        except DirectICourseError:
            try:
                response.close()
            finally:
                raise

    @staticmethod
    def _json(response, code: str) -> dict:
        try:
            value = response.json()
        except (TypeError, ValueError) as exc:
            raise DirectICourseError(code) from exc
        if not isinstance(value, dict):
            raise DirectICourseError(code)
        return value

    @staticmethod
    def _extract_ticket(value: str) -> str:
        for pattern in _TICKET_PATTERNS:
            match = pattern.search(value)
            if match is not None:
                return html.unescape(match.group(1))
        raise DirectICourseError("catalog_ticket_missing")

    def _extract_bearer(self) -> str:
        encoded = ""
        for name, value in self.session.cookies.items():
            if str(name) == "_token":
                encoded = str(value or "")
                break
        for _ in range(3):
            decoded = unquote(encoded)
            match = _TOKEN_RE.search(decoded)
            if match is not None:
                token = match.group(1)
                if 16 <= len(token) <= 4096 and "\r" not in token and "\n" not in token:
                    return token
            if decoded == encoded:
                break
            encoded = decoded
        raise DirectICourseError("catalog_bearer_missing")

    def login(
        self,
        student_id: str,
        password: str,
        *,
        deadline_seconds: float | None = None,
    ) -> None:
        if not student_id or not password:
            raise DirectICourseError("fudan_credentials_missing")
        self.begin_request_deadline(
            max(1.0, float(deadline_seconds or self.AUTH_DEADLINE_SECONDS))
        )
        try:
            self._report("catalog_context")
            login_url = (
                f"{config.ICOURSE_BASE}/casapi/index.php"
                f"?r=auth/login&school_login=1"
                f"&tenant_code={config.TENANT_CODE}"
                f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
            )
            response = self._request("GET", login_url, allow_redirects=True)
            sources = [str(response.url)] + [
                str(item.headers.get("Location") or "")
                for item in response.history or []
            ]
            sources.append(str(response.text or "")[:5000])
            lck = ""
            for source in sources:
                match = re.search(r'lck=([^&#"\s]+)', source)
                if match is not None:
                    lck = match.group(1)
                    break
            if not lck:
                raise DirectICourseError("catalog_auth_context_missing")

            self._report("catalog_challenge")
            entity_id = config.ICOURSE_BASE
            methods = self._json(
                self._request(
                    "POST",
                    f"{config.IDP_BASE}/idp/authn/queryAuthMethods",
                    json={"lck": lck, "entityId": entity_id},
                    headers={
                        "Content-Type": "application/json",
                        "Referer": f"{config.IDP_BASE}/ac/",
                        "Origin": config.IDP_BASE,
                    },
                ),
                "catalog_auth_challenge_invalid",
            )
            method = next(
                (
                    item for item in methods.get("data") or []
                    if isinstance(item, dict) and item.get("moduleCode") == "userAndPwd"
                ),
                {},
            )
            chain = str(method.get("authChainCode") or "")
            public_key = str(
                self._json(
                    self._request(
                        "GET",
                        f"{config.IDP_BASE}/idp/authn/getJsPublicKey",
                        headers={"Referer": f"{config.IDP_BASE}/ac/"},
                    ),
                    "catalog_auth_key_invalid",
                ).get("data")
                or ""
            )
            if not chain or not public_key:
                raise DirectICourseError("catalog_auth_challenge_invalid")

            self._report("catalog_credentials")
            authentication = self._json(
                self._request(
                    "POST",
                    f"{config.IDP_BASE}/idp/authn/authExecute",
                    json={
                        "authModuleCode": "userAndPwd",
                        "authChainCode": chain,
                        "entityId": entity_id,
                        "requestType": methods.get("requestType", "chain_type"),
                        "lck": lck,
                        "authPara": {
                            "loginName": student_id,
                            "password": encrypt_idp_password(password, public_key),
                            "verifyCode": "",
                        },
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Referer": f"{config.IDP_BASE}/ac/",
                        "Origin": config.IDP_BASE,
                    },
                ),
                "catalog_auth_rejected",
            )
            if str(authentication.get("code")) != "200":
                raise DirectICourseError("catalog_auth_rejected")
            login_token = str(authentication.get("loginToken") or "")
            if not login_token:
                raise DirectICourseError("catalog_auth_rejected")

            self._report("catalog_ticket")
            engine = self._request(
                "POST",
                f"{config.IDP_BASE}/idp/authCenter/authnEngine",
                data={"loginToken": login_token},
                headers={
                    "Referer": f"{config.IDP_BASE}/ac/",
                    "Origin": config.IDP_BASE,
                },
            )
            ticket = self._validate_url(self._extract_ticket(str(engine.text or "")), icourse_only=True)
            accepted = self._request("GET", ticket, allow_redirects=True, stream=True)
            accepted.close()
            self._bearer = self._extract_bearer()

            self._report("catalog_identity")
            identity = self._json(
                self.get(
                    f"{config.ICOURSE_BASE}/userapi/v1/infosimple",
                    headers=self.authorization_headers(),
                ),
                "catalog_identity_invalid",
            )
            if identity.get("code") not in (0, 200):
                raise DirectICourseError("catalog_identity_invalid")
            self._userinfo = dict(identity.get("params") or identity.get("data") or {})
            if not self._userinfo.get("id") or not self._userinfo.get("account"):
                raise DirectICourseError("catalog_identity_invalid")
        finally:
            self.end_request_deadline()

    def authorization_headers(self) -> dict[str, str]:
        if not self._bearer:
            raise DirectICourseError("catalog_bearer_missing")
        return {"Authorization": f"Bearer {self._bearer}"}

    def adopt_authorization(
        self,
        headers: dict[str, str],
        expected_userinfo: dict,
    ) -> None:
        """Adopt one already verified in-memory bearer without replaying CAS."""
        authorization = str(headers.get("Authorization") or "")
        scheme, separator, bearer = authorization.partition(" ")
        expected_id = str(expected_userinfo.get("id") or "")
        expected_account = str(expected_userinfo.get("account") or "")
        if (
            scheme.casefold() != "bearer"
            or not separator
            or not (16 <= len(bearer) <= 4096)
            or "\r" in bearer
            or "\n" in bearer
        ):
            raise DirectICourseError("catalog_bearer_missing")
        if not expected_id or not expected_account:
            raise DirectICourseError("catalog_identity_invalid")

        self._bearer = bearer
        try:
            response = self.get(
                f"{config.ICOURSE_BASE}/userapi/v1/infosimple",
                headers=self.authorization_headers(),
            )
            try:
                identity = self._json(response, "catalog_identity_invalid")
            finally:
                response.close()
            if identity.get("code") not in (0, 200):
                raise DirectICourseError("catalog_session_expired")
            observed = dict(identity.get("params") or identity.get("data") or {})
            if (
                str(observed.get("id") or "") != expected_id
                or str(observed.get("account") or "") != expected_account
            ):
                raise DirectICourseError("catalog_identity_mismatch")
            self._userinfo = observed
        except Exception:
            self._bearer = ""
            self._userinfo = {}
            raise

    def get(self, url: str, **kwargs):
        self._validate_url(url, icourse_only=True)
        return self._request("GET", url, **kwargs)

    def check_alive(self) -> bool:
        try:
            response = self.get(
                f"{config.ICOURSE_BASE}/userapi/v1/infosimple",
                headers=self.authorization_headers(),
                timeout=8,
            )
            value = self._json(response, "catalog_session_expired")
            return value.get("code") in (0, 200)
        except DirectICourseError:
            return False

    def close(self) -> None:
        self._bearer = ""
        self._userinfo = {}
        self.session.close()
