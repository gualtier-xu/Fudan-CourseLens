"""Signed, user-confirmed updates for managed Windows installations.

The transport is deliberately untrusted.  A detached Ed25519 signature covers
the canonical manifest, while the manifest binds one package to one release,
channel, platform, architecture and exact byte sequence.  Release assets are
distributed anonymously through the public repository's GitHub Releases
(ADR 0006); public download adds no trust — every request is restricted to an
exact host allowlist with per-hop validation and no Authorization header.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import inspect
import ipaddress
import json
import os
import platform
import random
import re
import shutil
import socket
import ssl
import stat
import threading
import time
import urllib.parse
import uuid
import zipfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .durability import (
    FaultHook,
    append_audit,
    atomic_json,
    cleanup_atomic_temps,
    durable_replace,
    remove_durable,
    sync_directory,
)
from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed


SCHEMA = "courselens.client-update.v1"
STATES = {
    "idle", "checking", "offline", "up_to_date", "available", "downloading",
    "verifying", "ready_to_restart", "applying", "healthy", "rolled_back",
    "failed", "policy_blocked",
}
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_SOURCE_ID = "Fudan-CourseLens-Source"
DISTRIBUTION_REPOSITORY = "gualtier-xu/Fudan-CourseLens"
UPDATE_CHANNEL_ENV = "COURSELENS_UPDATE_CHANNEL"
RELEASE_TAG_NAMESPACE = "client-v"
MANIFEST_ASSET = "courselens-windows-manifest.json"
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}
WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 20_000
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024
MAX_REDIRECT_HOPS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
PACKAGE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REQUIRED_PRODUCTION_GATES = {
    "public_release_repository", "protected_monorepo_ci", "offline_update_root",
    "protected_update_release_key", "public_download_safety_implemented",
    "release_artifact_sha256_published", "clean_machine_acceptance",
    "real_restart_acceptance", "power_loss_recovery_acceptance",
}
DISTRIBUTION_KEYS = {
    "repository", "visibility", "auth_model", "tag_namespace",
    "manifest_asset", "source_repository_access",
}
BACKGROUND_FIRST_DELAY = (5.0 * 60.0, 15.0 * 60.0)
BACKGROUND_PERIOD = 24.0 * 3600.0
BACKGROUND_OFFLINE_BASE = 3600.0


def _distribution_repository() -> str:
    """Effective distribution channel repository.

    An unset or blank ``COURSELENS_UPDATE_CHANNEL`` keeps the pinned default
    verbatim.  The override never relaxes validation: the trust policy's
    ``manifest_url`` and ``distribution.repository`` must still equal the
    effective value exactly (:meth:`UpdateService._policy`), so a channel the
    installed policy does not pin fails closed as ``distribution_policy_invalid``.
    """
    override = os.environ.get(UPDATE_CHANNEL_ENV, "").strip()
    return override or DISTRIBUTION_REPOSITORY


def _stable_manifest_url() -> str:
    """The policy-pinned anonymous stable-channel manifest URL."""
    return (
        f"https://github.com/{_distribution_repository()}/releases"
        f"/latest/download/{MANIFEST_ASSET}"
    )


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _version(value: str) -> tuple[int, int, int, int, str]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise UpdateError("manifest_version_invalid")
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    suffix = match.group(4)
    return major, minor, patch, 0 if suffix is None else -1, suffix or ""


def _utc(value: str, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise UpdateError(code) from exc
    if parsed.tzinfo is None:
        raise UpdateError(code)
    return parsed.astimezone(timezone.utc)


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return path.is_symlink()


def _archive_name(value: str) -> tuple[PurePosixPath, str]:
    if (
        not value or value.startswith("/") or value.endswith("/")
        or "\\" in value or "//" in value
        or value != unicodedata.normalize("NFC", value)
    ):
        raise UpdateError("package_archive_unsafe")
    parts = value.split("/")
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or WINDOWS_FORBIDDEN_RE.search(part)
        or unicodedata.normalize("NFKC", part).casefold().split(".", 1)[0]
        in WINDOWS_RESERVED_NAMES
        for part in parts
    ):
        raise UpdateError("package_archive_unsafe")
    path = PurePosixPath(*parts)
    collision_key = "/".join(
        unicodedata.normalize("NFKC", part).casefold() for part in parts
    )
    return path, collision_key


def _valid_source(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"repository", "commit", "tree"}
        and value.get("repository") == MANIFEST_SOURCE_ID
        and HEX40_RE.fullmatch(str(value.get("commit") or "")) is not None
        and HEX40_RE.fullmatch(str(value.get("tree") or "")) is not None
    )


def _validate_hop_url(
    value: str, *, allowed_hosts: set[str], resolver: Callable | None = None
) -> tuple[urllib.parse.SplitResult, str]:
    """Validate one request target before any connection is opened.

    HTTPS only, port 443 only, hostname an exact allowlist member (casefolded,
    trailing dot stripped), no username/password, no fragment, and every
    resolved address globally routable (anti-SSRF / DNS-rebinding).  The
    resolver is injectable so tests never touch the network.
    """
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("source_url_invalid") from exc
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme != "https" or not hostname or hostname not in allowed_hosts
        or parsed.username is not None or parsed.password is not None
        or port not in (None, 443) or parsed.fragment
    ):
        raise UpdateError("source_url_blocked")
    resolve = resolver if resolver is not None else socket.getaddrinfo
    try:
        addresses = resolve(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UpdateError("network_unavailable") from exc
    if not addresses:
        raise UpdateError("network_unavailable")
    approved: list[str] = []
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise UpdateError("source_address_blocked")
        rendered = str(address)
        if rendered not in approved:
            approved.append(rendered)
    return parsed, approved[0]


def _validate_package_url(value: str) -> None:
    """Package start URLs must be anonymous GitHub release assets of the
    configured distribution repository with a ``client-v<semver>`` tag."""
    prefix = f"https://github.com/{_distribution_repository()}/releases/download/"
    if not value.startswith(prefix):
        raise UpdateError("source_url_blocked")
    tag, separator, asset = value[len(prefix):].partition("/")
    if (
        not separator
        or not tag.startswith(RELEASE_TAG_NAMESPACE)
        or VERSION_RE.fullmatch(tag[len(RELEASE_TAG_NAMESPACE):]) is None
        or not asset
        or "/" in asset
        or PACKAGE_ASSET_RE.fullmatch(asset) is None
    ):
        raise UpdateError("source_url_blocked")


def _request_headers(destination: Path | None) -> dict[str, str]:
    # Public anonymous distribution: no Authorization header is ever attached,
    # so cross-host redirect following cannot leak credentials.  Auth stripping
    # is structural (there is nothing to strip), not a per-hop rewrite.
    return {
        "Accept": "application/json" if destination is None else "application/octet-stream",
        "User-Agent": "CourseLens-Updater/1",
        "Connection": "close",
    }


def _transport_accepts_progress(transport: Callable) -> bool:
    """Whether an injected transport declared the optional ``progress`` kwarg.

    Legacy test transports keep the exact ``(url, destination, limit)`` shape
    and are called verbatim; only transports that opted in receive progress.
    """
    try:
        parameters = inspect.signature(transport).parameters.values()
    except (TypeError, ValueError):
        return False
    for parameter in parameters:
        if parameter.name == "progress" and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


@dataclass(frozen=True)
class AuthorizedReleaseKey:
    public_key: str
    key_epoch: int


@dataclass(frozen=True)
class TrustPolicy:
    channel: str
    platform: str
    architecture: str
    minimum_version: str
    minimum_key_epoch: int
    keys: dict[str, AuthorizedReleaseKey]
    allowed_hosts: set[str]
    manifest_url: str
    enabled: bool
    distribution: dict[str, Any]
    production_gates: dict[str, bool]

    @classmethod
    def load(cls, path: Path, *, now: float) -> "TrustPolicy":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError) as exc:
            # BOM/截断/UTF-16 等本地配置损坏必须走闭集码（N6CP 边界族），
            # 绝不裸抛或被下游宽网误标成 manifest 问题。
            raise UpdateError("trust_config_invalid") from exc
        if raw.get("schema") != "courselens.client-update-trust.v2":
            raise UpdateError("trust_schema_invalid")
        keys = cls._authorized_release_keys(raw, now=now)
        return cls(
            channel=str(raw.get("channel") or "stable"),
            platform=str(raw.get("platform") or "windows"),
            architecture=str(raw.get("architecture") or "x86_64"),
            minimum_version=str(raw.get("minimum_version") or "0.0.0"),
            minimum_key_epoch=int(raw.get("minimum_key_epoch") or 0),
            keys=keys,
            allowed_hosts={str(v).rstrip(".").casefold() for v in raw.get("allowed_hosts") or []},
            manifest_url=str(raw.get("manifest_url") or ""),
            enabled=bool(raw.get("enabled", False)),
            distribution=dict(raw.get("distribution") or {}),
            production_gates={
                # N6CP#11 fail-closed：非真布尔（如字符串 "false"）一律视为
                # 未通过，绝不许字符串洗绿生产门。
                str(k): v is True for k, v in (raw.get("production_gates") or {}).items()
            },
        )

    @staticmethod
    def _authorized_release_keys(
        raw: dict[str, Any], *, now: float
    ) -> dict[str, AuthorizedReleaseKey]:
        roots = {str(k): str(v) for k, v in (raw.get("root_keys") or {}).items()}
        result: dict[str, AuthorizedReleaseKey] = {}
        identities: dict[str, AuthorizedReleaseKey] = {}
        revoked: set[str] = set()
        observed = datetime.fromtimestamp(now, timezone.utc)
        for envelope in raw.get("release_key_authorizations") or []:
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"schema", "authorization", "signature"}
                or envelope.get("schema") != "courselens.update-key-authorization.v1"
            ):
                raise UpdateError("release_key_authorization_invalid")
            authorization = envelope.get("authorization")
            signature = envelope.get("signature")
            if not isinstance(authorization, dict) or not isinstance(signature, dict):
                raise UpdateError("release_key_authorization_invalid")
            required = {
                "release_key_id", "public_key", "key_epoch", "valid_from",
                "expires_at", "status", "channels", "platforms",
            }
            if set(authorization) != required or set(signature) != {"root_key_id", "value"}:
                raise UpdateError("release_key_authorization_invalid")
            root_key_id = str(signature["root_key_id"])
            if root_key_id not in roots:
                raise UpdateError("update_root_untrusted")
            try:
                VerifyKey(base64.b64decode(roots[root_key_id], validate=True)).verify(
                    _canonical(authorization),
                    base64.b64decode(str(signature["value"]), validate=True),
                )
            except (BadSignatureError, ValueError, TypeError) as exc:
                raise UpdateError("release_key_authorization_invalid") from exc
            valid_from = _utc(str(authorization["valid_from"]), "release_key_authorization_invalid")
            expires_at = _utc(str(authorization["expires_at"]), "release_key_authorization_invalid")
            if not valid_from <= observed < expires_at:
                continue
            if str(raw.get("channel") or "") not in {
                str(value) for value in authorization["channels"]
            }:
                continue
            if str(raw.get("platform") or "") not in {
                str(value) for value in authorization["platforms"]
            }:
                continue
            key_id = str(authorization["release_key_id"])
            if not KEY_ID_RE.fullmatch(key_id):
                raise UpdateError("release_key_authorization_invalid")
            try:
                key_epoch = int(authorization["key_epoch"])
                public_key = str(authorization["public_key"])
                VerifyKey(base64.b64decode(public_key, validate=True))
            except (ValueError, TypeError) as exc:
                raise UpdateError("release_key_authorization_invalid") from exc
            candidate = AuthorizedReleaseKey(public_key=public_key, key_epoch=key_epoch)
            existing = identities.get(key_id)
            if existing is not None and existing != candidate:
                raise UpdateError("release_key_authorization_conflict")
            identities[key_id] = candidate
            status = str(authorization["status"])
            if status == "revoked":
                revoked.add(key_id)
                result.pop(key_id, None)
                continue
            if (
                status != "active"
                or key_id in revoked
                or key_epoch < int(raw.get("minimum_key_epoch") or 0)
            ):
                continue
            result[key_id] = candidate
        return result


class UpdateService:
    """Backend-authoritative update state machine.

    ``install_root`` is managed only when ``state/install-layout.json`` exists.
    Source checkouts remain inspectable but cannot be overwritten.
    """

    def __init__(
        self,
        *,
        current_version: str,
        trust_path: Path,
        state_root: Path,
        install_root: Path,
        transport: Callable[[str, Path | None, int], bytes | None] | None = None,
        now: Callable[[], float] = time.time,
        fault: FaultHook | None = None,
        has_active_work: Callable[[], bool] | None = None,
        request_shutdown: Callable[[str], bool] | None = None,
        background_checks_enabled: Callable[[], bool] | None = None,
        background_first_delay: tuple[float, float] = BACKGROUND_FIRST_DELAY,
        background_period: float = BACKGROUND_PERIOD,
    ):
        self.current_version = current_version
        self.trust_path = trust_path
        self.state_root = state_root.resolve()
        self.install_root = install_root.resolve()
        self.transport = transport
        self.now = now
        self.fault = fault
        # Lifecycle callbacks are injected by the composition root; this module
        # never imports the lifecycle layer.
        self._has_active_work = has_active_work
        self._request_shutdown = request_shutdown
        self._background_checks_enabled = background_checks_enabled
        self._background_first_delay = (
            max(0.0, float(background_first_delay[0])),
            max(0.0, float(background_first_delay[1])),
        )
        self._background_period = max(0.01, float(background_period))
        self._restart_owed = False
        self._background_stop: threading.Event | None = None
        self._background_thread: threading.Thread | None = None
        self._mutex = threading.Lock()
        self._state_path = self.state_root / "state.json"
        self._manifest_path = self.state_root / "verified-manifest.json"
        self._package_path = self.state_root / "staging" / "package.zip"
        self._audit_path = self.state_root / "transition-audit.jsonl"
        self._state = self._load_state()
        self.recover()

    def _acquire_process_lock(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        stream = (self.state_root / "update.lock").open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise UpdateError("update_busy") from exc
        return stream

    @staticmethod
    def _release_process_lock(stream) -> None:
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            if value.get("state") in STATES:
                return value
        except (OSError, ValueError, TypeError):
            pass
        return self._base("idle")

    def _base(self, state: str, **values: Any) -> dict[str, Any]:
        result = {
            "schema": "courselens.client-update-state.v1",
            "state": state,
            "error_code": "",
            "current_version": self.current_version,
            "channel": "stable",
            "available_version": "",
            "package_size": 0,
            "release_notes": "",
            "last_checked_at": 0,
            "observed_at": self.now(),
            # UPDATE-UX-1 零呆等三律：下载期字节进度（快照闭集新成员；非下载
            # 态恒 0，前端只在 downloading 态消费）。
            "download_bytes": 0,
            "download_total": 0,
            "rollback": {"available": False, "version": "", "state": "none"},
            "actions": ["check"],
        }
        result.update(values)
        return result

    def _save(self, state: str, **values: Any) -> dict[str, Any]:
        if state not in STATES:
            raise AssertionError(state)
        previous = str(self._state.get("state") or "")
        self._state = {**self._state, "state": state, "observed_at": self.now(), **values}
        atomic_json(self._state_path, self._state, boundary="state.replace", fault=self.fault)
        append_audit(
            self._audit_path, "state_transition", previous=previous, state=state,
            observed_at=self._state["observed_at"],
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        allowed = {
            "schema", "state", "error_code", "current_version", "channel",
            "available_version", "package_size", "release_notes", "last_checked_at",
            "observed_at", "download_bytes", "download_total", "rollback", "actions",
        }
        view = {key: self._state.get(key) for key in allowed}
        if not isinstance(view["available_version"], str):
            # Foreign/legacy state files may lack the key or carry null; the
            # envelope must never advertise a non-string version.
            view["available_version"] = ""
        for key in ("download_bytes", "download_total"):
            value = view[key]
            # Foreign/legacy state files may lack the keys or carry junk; the
            # envelope keeps the progress pair numeric or absent-safe at 0.
            view[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        return view

    def _policy(self) -> TrustPolicy:
        policy = TrustPolicy.load(self.trust_path, now=self.now())
        _version(policy.minimum_version)
        if not policy.enabled:
            raise UpdateError("update_not_configured")
        actual_platform, actual_architecture = host_platform()
        if (
            policy.platform != actual_platform
            or policy.architecture != actual_architecture
        ):
            raise UpdateError("host_platform_mismatch")
        missing_gates = REQUIRED_PRODUCTION_GATES - {
            name for name, passed in policy.production_gates.items() if passed is True
        }
        if missing_gates:
            raise UpdateError("production_gates_incomplete")
        distribution = policy.distribution
        if (
            set(distribution) != DISTRIBUTION_KEYS
            or distribution.get("repository") != _distribution_repository()
            or distribution.get("visibility") != "public"
            or distribution.get("auth_model") != "none"
            or distribution.get("tag_namespace") != RELEASE_TAG_NAMESPACE
            or distribution.get("manifest_asset") != MANIFEST_ASSET
            or distribution.get("source_repository_access") is not False
            or policy.manifest_url != _stable_manifest_url()
        ):
            raise UpdateError("distribution_policy_invalid")
        if not policy.manifest_url or not policy.keys:
            raise UpdateError("update_not_configured")
        return policy

    @staticmethod
    def _redirect_target(response) -> str:
        # Follow only absolute https redirects; anything else is untrusted.
        location = response.getheader("Location") or ""
        candidate = urllib.parse.urlsplit(location)
        if candidate.scheme != "https" or not candidate.hostname:
            raise UpdateError("download_redirect_untrusted")
        return location

    def _fetch(
        self, url: str, destination: Path | None, limit: int, policy: TrustPolicy,
        on_progress: Callable[[int], None] | None = None,
    ) -> bytes | None:
        # The start URL is policy-pinned before any connection is opened.
        if destination is None:
            if url != policy.manifest_url:
                raise UpdateError("source_url_blocked")
        else:
            _validate_package_url(url)
        # 测试模式出站门（src/runtime/test_mode.py）：先于注入 transport 与任何
        # 连接；拒绝翻译成本族既有闭集码 source_url_blocked（审计行已携
        # egress_blocked+host）。未设 env 时此调用恒放行零行为。
        try:
            ensure_egress_allowed(url, purpose="update_check")
        except EgressBlockedError as exc:
            raise UpdateError("source_url_blocked") from exc
        if self.transport:
            # Injected transport (tests/offline paths) performs the transfer
            # itself; the manual redirect loop below is bypassed in this mode.
            # The start URL is still validated before delegating.
            _validate_hop_url(url, allowed_hosts=policy.allowed_hosts)
            # UPDATE-UX-1 零呆等三律：进度回调按可选 keyword 递给声明了
            # ``progress`` 形参的注入 transport（既有 (url, destination, limit)
            # 签名的测试桩零改动、零行为漂移）；进度语义由传输方自证。
            kwargs: dict[str, Any] = {}
            if on_progress is not None and _transport_accepts_progress(self.transport):
                kwargs["progress"] = on_progress
            result = self.transport(url, destination, limit, **kwargs)
            if destination is not None:
                sync_directory(destination.parent)
                if self.fault:
                    self.fault("download.after_replace")
            return result
        headers = _request_headers(destination)
        current_url = url
        for hop in range(MAX_REDIRECT_HOPS + 1):
            # Every hop — including hop 0 — is re-validated and re-resolved
            # before contact; the pinned IP is never reused across hops.
            parsed, pinned_ip = _validate_hop_url(current_url, allowed_hosts=policy.allowed_hosts)
            for attempt in range(3):
                partial = destination.with_suffix(".partial") if destination is not None else None
                if partial is not None:
                    partial.unlink(missing_ok=True)
                connection = http.client.HTTPSConnection(
                    parsed.hostname, 443, timeout=20, context=ssl.create_default_context()
                )
                connection._create_connection = lambda _address, timeout=None, source_address=None, _ip=pinned_ip: socket.create_connection(  # type: ignore[method-assign]
                    (_ip, 443), timeout=timeout, source_address=source_address
                )
                try:
                    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                    connection.request("GET", target, headers=headers)
                    response = connection.getresponse()
                    if response.status in REDIRECT_STATUSES:
                        if hop == MAX_REDIRECT_HOPS:
                            raise UpdateError("download_redirect_untrusted")
                        current_url = self._redirect_target(response)
                        break
                    if response.status != 200:
                        raise UpdateError("download_http_error")
                    if response.getheader("Content-Encoding") not in (None, "", "identity"):
                        raise UpdateError("download_encoding_unsupported")
                    declared = response.getheader("Content-Length")
                    if declared and int(declared) > limit:
                        raise UpdateError("download_too_large")
                    if destination is None:
                        content = response.read(limit + 1)
                        if len(content) > limit:
                            raise UpdateError("download_too_large")
                        return content
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    total = 0
                    assert partial is not None
                    if on_progress is not None:
                        on_progress(0)
                    with partial.open("wb") as stream:
                        while True:
                            block = response.read(min(CHUNK, limit + 1 - total))
                            if not block:
                                break
                            total += len(block)
                            if total > limit:
                                raise UpdateError("download_too_large")
                            stream.write(block)
                            if on_progress is not None:
                                on_progress(total)
                        stream.flush()
                        os.fsync(stream.fileno())
                    durable_replace(partial, destination)
                    if self.fault:
                        self.fault("download.after_replace")
                    return None
                except UpdateError:
                    raise
                except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
                    if attempt == 2:
                        raise UpdateError("network_unavailable") from exc
                finally:
                    connection.close()
        # Unreachable while every attempt either returns, redirects or raises;
        # kept fail-closed in case the loop invariant ever changes.
        raise UpdateError("download_redirect_untrusted")

    def _verify_manifest(self, envelope: dict[str, Any], policy: TrustPolicy) -> dict[str, Any]:
        if set(envelope) != {"schema", "manifest", "signature"} or envelope.get("schema") != SCHEMA:
            raise UpdateError("manifest_schema_invalid")
        manifest = envelope.get("manifest")
        signature = envelope.get("signature")
        if not isinstance(manifest, dict) or not isinstance(signature, dict):
            raise UpdateError("manifest_schema_invalid")
        required = {
            "release_id", "version", "channel", "platform", "architecture", "published_at",
            "expires_at", "minimum_security_version", "package", "release_notes", "source",
        }
        if set(manifest) != required or set(signature) != {"key_id", "key_epoch", "value"}:
            raise UpdateError("manifest_schema_invalid")
        key_id = str(signature["key_id"])
        epoch = int(signature["key_epoch"])
        authorized_key = policy.keys.get(key_id)
        if (
            not KEY_ID_RE.fullmatch(key_id)
            or authorized_key is None
            or epoch != authorized_key.key_epoch
        ):
            raise UpdateError("manifest_key_untrusted")
        try:
            VerifyKey(base64.b64decode(authorized_key.public_key, validate=True)).verify(
                _canonical(manifest), base64.b64decode(str(signature["value"]), validate=True)
            )
        except (BadSignatureError, ValueError, TypeError) as exc:
            raise UpdateError("manifest_signature_invalid") from exc
        package = manifest["package"]
        if (
            not isinstance(package, dict)
            or set(package) != {"url", "size", "sha256", "format"}
            or not _valid_source(manifest["source"])
        ):
            raise UpdateError("manifest_schema_invalid")
        version = str(manifest["version"])
        minimum = str(manifest["minimum_security_version"])
        _version(version)
        _version(minimum)
        if policy.channel == "stable" and VERSION_RE.fullmatch(version).group(4) is not None:
            raise UpdateError("manifest_prerelease_blocked")
        if manifest["channel"] != policy.channel:
            raise UpdateError("manifest_channel_mismatch")
        if manifest["platform"] != policy.platform or manifest["architecture"] != policy.architecture:
            raise UpdateError("manifest_platform_mismatch")
        if str(package["format"]) != "zip-v1":
            raise UpdateError("package_format_unsupported")
        size = int(package["size"])
        if size <= 0 or size > MAX_PACKAGE_BYTES or not HEX64_RE.fullmatch(str(package["sha256"])):
            raise UpdateError("manifest_package_invalid")
        package_url = str(package["url"])
        _validate_package_url(package_url)
        _validate_hop_url(package_url, allowed_hosts=policy.allowed_hosts)
        now = datetime.fromtimestamp(self.now(), timezone.utc)
        published = _utc(str(manifest["published_at"]), "manifest_time_invalid")
        expires = _utc(str(manifest["expires_at"]), "manifest_time_invalid")
        if published > now.replace(microsecond=0) or expires <= now or expires <= published:
            raise UpdateError("manifest_expired_or_future")
        if _version(self.current_version) < _version(max(minimum, policy.minimum_version, key=_version)):
            raise UpdateError("current_version_below_security_floor")
        accepted = self._accepted_history()
        if accepted:
            if epoch < int(accepted.get("key_epoch") or 0):
                raise UpdateError("manifest_key_epoch_rollback")
            if _version(version) < _version(str(accepted.get("version") or "0.0.0")):
                raise UpdateError("manifest_replay_or_downgrade")
            if (
                version == accepted.get("version")
                and str(manifest["release_id"]) != str(accepted.get("release_id"))
            ):
                raise UpdateError("manifest_mix_and_match")
        return manifest

    def _accepted_history(self) -> dict[str, Any]:
        history = self.state_root / "accepted.json"
        try:
            accepted = json.loads(history.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            raise UpdateError("accepted_history_invalid") from exc
        if (
            not isinstance(accepted, dict)
            or set(accepted) != {"version", "release_id", "key_epoch"}
            or not isinstance(accepted.get("version"), str)
            or not isinstance(accepted.get("release_id"), str)
            or not accepted["release_id"]
            or isinstance(accepted.get("key_epoch"), bool)
            or not isinstance(accepted.get("key_epoch"), int)
            or accepted["key_epoch"] < 0
        ):
            raise UpdateError("accepted_history_invalid")
        try:
            _version(accepted["version"])
        except UpdateError as exc:
            raise UpdateError("accepted_history_invalid") from exc
        return accepted

    def check(self) -> dict[str, Any]:
        if not self._mutex.acquire(blocking=False):
            raise UpdateError("update_busy")
        process_lock = None
        try:
            process_lock = self._acquire_process_lock()
            return self._check_unlocked()
        finally:
            self._release_process_lock(process_lock)
            self._mutex.release()

    def _check_unlocked(self) -> dict[str, Any]:
        try:
            self._save("checking", error_code="", actions=[])
            policy = self._policy()
            raw = self._fetch(policy.manifest_url, None, MAX_MANIFEST_BYTES, policy)
            envelope = json.loads((raw or b"").decode("utf-8"))
            manifest = self._verify_manifest(envelope, policy)
            atomic_json(
                self._manifest_path,
                {"schema": SCHEMA, "manifest": manifest, "verified_at": self.now()},
                boundary="verified_manifest.replace", fault=self.fault,
            )
            common = {
                "channel": policy.channel,
                "available_version": str(manifest["version"]),
                "package_size": int(manifest["package"]["size"]),
                "release_notes": str(manifest["release_notes"])[:8000],
                "last_checked_at": self.now(),
            }
            if _version(str(manifest["version"])) <= _version(self.current_version):
                return self._save("up_to_date", **common, actions=["check"])
            return self._save("available", **common, actions=["check", "download"])
        except UpdateError as exc:
            if exc.code == "update_busy":
                raise
            state = "offline" if exc.code == "network_unavailable" else "policy_blocked"
            return self._save(state, error_code=exc.code, actions=["check"])
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return self._save("failed", error_code="manifest_invalid", actions=["check"])

    def download(self) -> dict[str, Any]:
        if not self._mutex.acquire(blocking=False):
            raise UpdateError("update_busy")
        process_lock = None
        try:
            process_lock = self._acquire_process_lock()
            return self._download_unlocked()
        finally:
            self._release_process_lock(process_lock)
            self._mutex.release()

    def _download_unlocked(self) -> dict[str, Any]:
        try:
            verified = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            manifest = verified["manifest"]
            policy = self._policy()
            # Revalidate persisted data before every use; never trust state alone.
            envelope_raw = self._fetch(policy.manifest_url, None, MAX_MANIFEST_BYTES, policy)
            envelope = json.loads((envelope_raw or b"").decode("utf-8"))
            current = self._verify_manifest(envelope, policy)
            if _canonical(current) != _canonical(manifest):
                raise UpdateError("manifest_changed")
            self._save("downloading", error_code="", actions=[], download_bytes=0, download_total=0)
            package = manifest["package"]
            expected_size = int(package["size"])
            usage = shutil.disk_usage(self.state_root)
            if usage.free < expected_size * 3 + 64 * 1024 * 1024:
                raise UpdateError("insufficient_disk_space")

            def _report_download_progress(done: int) -> None:
                # In-memory only (the poller reads the same process snapshot);
                # durable state keeps its transition-time values so a crash
                # leaves no misleading persisted progress behind.
                self._state["download_bytes"] = max(0, min(int(done), expected_size))

            self._state["download_total"] = expected_size
            self._fetch(str(package["url"]), self._package_path, expected_size, policy, on_progress=_report_download_progress)
            self._save("verifying", actions=[])
            if self._package_path.stat().st_size != expected_size:
                raise UpdateError("package_size_mismatch")
            digest_state = hashlib.sha256()
            with self._package_path.open("rb") as stream:
                while block := stream.read(CHUNK):
                    digest_state.update(block)
            digest = digest_state.hexdigest()
            if digest != package["sha256"]:
                raise UpdateError("package_hash_mismatch")
            if self.fault:
                self.fault("verification.after_complete")
            slot = self._extract(manifest)
            atomic_json(self.state_root / "accepted.json", {
                "version": manifest["version"], "release_id": manifest["release_id"],
                "key_epoch": envelope["signature"]["key_epoch"],
            }, boundary="accepted.replace", fault=self.fault)
            return self._save(
                "ready_to_restart", actions=["check", "install"],
                rollback=self._rollback_snapshot(), staged_slot=slot.name,
            )
        except UpdateError as exc:
            if exc.code == "update_busy":
                raise
            remove_durable(self._package_path)
            return self._save("failed", error_code=exc.code, actions=["check"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
            remove_durable(self._package_path)
            return self._save("failed", error_code="package_invalid", actions=["check"])

    def _active_work_blocks(self) -> bool:
        gate = self._has_active_work
        if gate is None:
            return False
        try:
            return bool(gate())
        except Exception:
            return True  # fail closed over work we cannot verify

    def update_now(self) -> dict[str, Any]:
        """One confirmed click: check → download → install, then a restart.

        Reuses the existing per-stage paths serially inside a single
        mutex/process-lock hold.  Active work blocks the whole orchestration
        up front (pinned contract: no state change while work is in flight);
        the in-lock re-check below then guards work that starts during the
        download window.  The shutdown request itself is deferred to
        :meth:`complete_restart` so the HTTP adapter can flush the 202 reply
        first.
        """
        if not self._mutex.acquire(blocking=False):
            raise UpdateError("update_busy")
        process_lock = None
        try:
            if self._active_work_blocks():
                raise UpdateError("update_restart_blocked")
            process_lock = self._acquire_process_lock()
            self._restart_owed = False
            snapshot = self._check_unlocked()
            if str(snapshot.get("state") or "") == "available":
                snapshot = self._download_unlocked()
            if str(snapshot.get("state") or "") == "ready_to_restart":
                # Re-check inside the same hold: work started during the
                # download window must not be shut down by the restart.
                if self._active_work_blocks():
                    raise UpdateError("update_restart_blocked")
                snapshot = self._install_unlocked()
            self._restart_owed = (
                str(snapshot.get("state") or "") == "ready_to_restart"
                and (self.install_root / "state" / "pending.json").exists()
            )
            return snapshot
        finally:
            self._release_process_lock(process_lock)
            self._mutex.release()

    def complete_restart(self) -> None:
        """Request the orchestrated shutdown once the HTTP reply is written.

        The HTTP adapter calls this after the 202 response has been sent, so
        the dying process never races its own reply.  Firing is one-shot per
        successful orchestration.
        """
        if not self._restart_owed:
            return
        callback = self._request_shutdown
        if callback is None:
            return
        self._restart_owed = False
        callback("update_restart")

    def start_background_checks(self) -> None:
        """Start the low-frequency background update timer.

        Only managed installs background-check; source checkouts stay silent.
        The persisted preference is read every cycle, so toggling
        ``update_background_checks`` takes effect without rewiring.  The check
        itself is never gated by ``has_active_work``.
        """
        if self._background_checks_enabled is None or self._background_thread is not None:
            return
        if not (self.install_root / "state" / "install-layout.json").is_file():
            return
        self._background_stop = threading.Event()
        self._background_thread = threading.Thread(
            target=self._background_loop, name="update-background-check", daemon=True,
        )
        self._background_thread.start()

    def stop_background_checks(self) -> None:
        stop, self._background_stop = self._background_stop, None
        thread, self._background_thread = self._background_thread, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _background_loop(self) -> None:
        stop = self._background_stop
        if stop is None:
            return
        period = self._background_period
        grace = min(60.0, period / 10.0)
        offline_base = min(BACKGROUND_OFFLINE_BASE, period / 4.0)
        first_low, first_high = self._background_first_delay
        wait_seconds = random.uniform(first_low, first_high)
        offline_wait = offline_base
        while not stop.wait(wait_seconds):
            wait_seconds = period
            snapshot: dict[str, Any] | None = None
            try:
                enabled = self._background_checks_enabled
                if enabled is not None and enabled():
                    last_checked = float(self.snapshot().get("last_checked_at") or 0.0)
                    if not last_checked or self.now() - last_checked >= period - grace:
                        try:
                            snapshot = self.check()
                        except UpdateError as exc:
                            # A user-initiated operation owns the lock; the
                            # next cycle retries against refreshed state.
                            if exc.code != "update_busy":
                                snapshot = {"state": "offline"}
                            else:
                                continue
            except Exception:  # the timer must never die
                snapshot = {"state": "offline"}
            if snapshot is not None and snapshot.get("state") == "offline":
                wait_seconds = offline_wait
                offline_wait = min(offline_wait * 2, period)
            else:
                offline_wait = offline_base

    def _extract(self, manifest: dict[str, Any]) -> Path:
        version = str(manifest["version"])
        slot_root = self.install_root / "versions"
        slot = (slot_root / version).resolve()
        if slot.parent != slot_root.resolve():
            raise UpdateError("package_path_invalid")
        temporary = slot_root / f".{version}.{uuid.uuid4().hex}.staging"
        if temporary.exists() or _is_link(slot_root):
            raise UpdateError("package_path_invalid")
        temporary.mkdir(parents=True)
        total = 0
        count = 0
        try:
            with zipfile.ZipFile(self._package_path) as archive:
                entries: dict[str, zipfile.ZipInfo] = {}
                collision_keys: set[str] = set()
                for info in archive.infolist():
                    count += 1
                    total += info.file_size
                    path, collision_key = _archive_name(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        count > MAX_FILES or total > MAX_EXPANDED_BYTES or info.file_size > MAX_PACKAGE_BYTES
                        or info.is_dir() or info.flag_bits & 0x1 or stat.S_ISLNK(mode)
                        or info.filename in entries or collision_key in collision_keys
                    ):
                        raise UpdateError("package_archive_unsafe")
                    entries[info.filename] = info
                    collision_keys.add(collision_key)
                metadata_info = entries.get("courselens-package.json")
                if metadata_info is None or metadata_info.file_size > MAX_MANIFEST_BYTES:
                    raise UpdateError("package_inventory_mismatch")
                try:
                    metadata = json.loads(archive.read(metadata_info).decode("utf-8"))
                except (KeyError, UnicodeError, ValueError) as exc:
                    raise UpdateError("package_identity_mismatch") from exc
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("schema") != "courselens.client-package.v1"
                    or metadata.get("version") != version
                    or metadata.get("release_id") != manifest["release_id"]
                    or metadata.get("platform") != manifest["platform"]
                    or metadata.get("architecture") != manifest["architecture"]
                    or metadata.get("source") != manifest["source"]
                    or not _valid_source(metadata.get("source"))
                ):
                    raise UpdateError("package_identity_mismatch")
                files = metadata.get("files")
                if not isinstance(files, dict) or not files:
                    raise UpdateError("package_identity_mismatch")
                inventory_keys = {_archive_name(str(name))[1] for name in files}
                if len(inventory_keys) != len(files) or _archive_name("courselens-package.json")[1] in inventory_keys:
                    raise UpdateError("package_archive_unsafe")
                if set(entries) != {"courselens-package.json", *files}:
                    raise UpdateError("package_inventory_mismatch")
                for raw_name, expected_hash in files.items():
                    if not HEX64_RE.fullmatch(str(expected_hash)):
                        raise UpdateError("package_archive_unsafe")
                for info in archive.infolist():
                    path, _ = _archive_name(info.filename)
                    target = temporary.joinpath(*path.parts)
                    resolved_parent = target.parent.resolve()
                    if temporary.resolve() not in (resolved_parent, *resolved_parent.parents):
                        raise UpdateError("package_archive_unsafe")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        copied = 0
                        while True:
                            block = source.read(CHUNK)
                            if not block:
                                break
                            copied += len(block)
                            if copied > info.file_size:
                                raise UpdateError("package_archive_unsafe")
                            output.write(block)
                    if copied != info.file_size:
                        raise UpdateError("package_archive_unsafe")
            for raw_name, expected_hash in files.items():
                name, _ = _archive_name(str(raw_name))
                file_path = temporary.joinpath(*name.parts)
                if not file_path.is_file() or _is_link(file_path) or file_path.stat().st_nlink != 1:
                    raise UpdateError("package_archive_unsafe")
                digest = hashlib.sha256()
                with file_path.open("rb") as stream:
                    while block := stream.read(CHUNK):
                        digest.update(block)
                if digest.hexdigest() != expected_hash:
                    raise UpdateError("package_file_hash_mismatch")
            extracted = {
                path.relative_to(temporary).as_posix()
                for path in temporary.rglob("*") if path.is_file()
            }
            if extracted != {"courselens-package.json", *files}:
                raise UpdateError("package_inventory_mismatch")
            if slot.exists():
                if _is_link(slot):
                    raise UpdateError("package_archive_unsafe")
                try:
                    existing_metadata = json.loads((slot / "courselens-package.json").read_text(encoding="utf-8-sig"))
                except (UnicodeError, ValueError) as exc:
                    # 既有槽位身份文件损坏=身份无法确认，同族闭集码，不裸抛。
                    raise UpdateError("package_identity_mismatch") from exc
                if existing_metadata != metadata:
                    raise UpdateError("package_identity_mismatch")
                shutil.rmtree(temporary)
            else:
                durable_replace(temporary, slot)
                if self.fault:
                    self.fault("slot.after_replace")
            return slot
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def install(self) -> dict[str, Any]:
        if not self._mutex.acquire(blocking=False):
            raise UpdateError("update_busy")
        process_lock = None
        try:
            process_lock = self._acquire_process_lock()
            return self._install_unlocked()
        finally:
            self._release_process_lock(process_lock)
            self._mutex.release()

    def _install_unlocked(self) -> dict[str, Any]:
        layout_path = self.install_root / "state" / "install-layout.json"
        try:
            layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self._save("policy_blocked", error_code="managed_install_required", actions=["check"])
        if layout.get("schema") != "courselens.managed-install.v1":
            return self._save("policy_blocked", error_code="managed_install_required", actions=["check"])
        staged = str(self._state.get("staged_slot") or "")
        target = (self.install_root / "versions" / staged).resolve()
        if not VERSION_RE.fullmatch(staged) or target.parent != (self.install_root / "versions").resolve() or not target.is_dir():
            return self._save("failed", error_code="staged_update_missing", actions=["check"])
        current_path = self.install_root / "state" / "current.json"
        try:
            current = json.loads(current_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            current = {"version": self.current_version}
        pending = {
            "schema": "courselens.update-pending.v1",
            "target_version": staged,
            "previous_version": str(current.get("version") or self.current_version),
            "created_at": self.now(),
        }
        atomic_json(
            self.install_root / "state" / "pending.json", pending,
            boundary="pending.replace", fault=self.fault,
        )
        # Advertise only actions the server's own closed set accepts here;
        # the restart itself belongs to the trusted launcher, never to a
        # client action.  ``update_now`` re-runs the orchestration safely.
        return self._save("ready_to_restart", actions=["check", "update_now"], rollback=self._rollback_snapshot())

    def apply_pending(self) -> dict[str, Any]:
        """Called by the installed, trusted launcher before starting a version."""
        pending_path = self.install_root / "state" / "pending.json"
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            current = self._read_current()
            if current.get("awaiting_health"):
                return self._save("applying", actions=[], rollback=self._rollback_snapshot())
            if current.get("version") == self.current_version:
                return self.snapshot()
            raise UpdateError("pending_update_missing")
        target_version = str(pending["target_version"])
        target = (self.install_root / "versions" / target_version).resolve()
        if target.parent != (self.install_root / "versions").resolve() or not target.is_dir():
            raise UpdateError("staged_update_missing")
        previous = str(pending["previous_version"])
        atomic_json(self.install_root / "state" / "current.json", {
            "schema": "courselens.current-version.v1", "version": target_version,
            "previous_version": previous, "awaiting_health": True, "switched_at": self.now(),
        }, boundary="current.apply_replace", fault=self.fault)
        remove_durable(pending_path)
        if self.fault:
            self.fault("pending.after_remove")
        return self._save("applying", actions=[], rollback={
            "available": True, "version": previous, "state": "awaiting_health",
        })

    def confirm_health(self, version: str) -> dict[str, Any]:
        current_path = self.install_root / "state" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8-sig"))
        if current.get("version") == version and not current.get("awaiting_health"):
            self.current_version = version
            return self._save("healthy", current_version=version, actions=["check"],
                              rollback=self._rollback_snapshot())
        if current.get("version") != version or not current.get("awaiting_health"):
            raise UpdateError("health_confirmation_invalid")
        current["awaiting_health"] = False
        current["healthy_at"] = self.now()
        atomic_json(current_path, current, boundary="current.health_replace", fault=self.fault)
        self.current_version = version
        return self._save("healthy", current_version=version, actions=["check"], rollback=self._rollback_snapshot())

    def rollback_if_unhealthy(self) -> dict[str, Any]:
        current_path = self.install_root / "state" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8-sig"))
        if not current.get("awaiting_health"):
            if current.get("rolled_back_at"):
                self.current_version = str(current.get("version") or self.current_version)
                return self._save("rolled_back", current_version=self.current_version,
                                  error_code="startup_health_failed", actions=["check"],
                                  rollback=self._rollback_snapshot())
            return self.snapshot()
        previous = str(current.get("previous_version") or "")
        target = (self.install_root / "versions" / previous).resolve()
        if not VERSION_RE.fullmatch(previous) or target.parent != (self.install_root / "versions").resolve() or not target.is_dir():
            raise UpdateError("rollback_unavailable")
        atomic_json(current_path, {
            "schema": "courselens.current-version.v1", "version": previous,
            "previous_version": str(current.get("version") or ""), "awaiting_health": False,
            "rolled_back_at": self.now(),
        }, boundary="current.rollback_replace", fault=self.fault)
        self.current_version = previous
        return self._save("rolled_back", current_version=previous, error_code="startup_health_failed",
                          actions=["check"], rollback={"available": True, "version": str(current.get("version") or ""), "state": "rolled_back"})

    def recover(self) -> None:
        cleanup_atomic_temps(self.state_root)
        cleanup_atomic_temps(self.install_root / "state")
        cleanup_atomic_temps(self.install_root / "versions")
        for partial in (self.state_root / "staging").glob("*.partial") if (self.state_root / "staging").exists() else []:
            remove_durable(partial)
        # UPDATE-UX-1 摩擦修复：半程编排态无法跨进程存活——带着上一进程的
        # checking/downloading/verifying 快照重启，会让顶栏/面板永久卡在 busy
        # （actions 为空、无任何恢复入口）。干净回到待检查；半包由上方 partial
        # 清理兜底（accepted 未写、无 staged 槽位），不影响既有断电恢复语义。
        if self._state.get("state") in {"checking", "downloading", "verifying"}:
            self._save("idle", error_code="", actions=["check"], download_bytes=0, download_total=0)
        pending_path = self.install_root / "state" / "pending.json"
        current_path = self.install_root / "state" / "current.json"
        try:
            current = self._read_current()
            # UPDATE-UX-1（真实用户模拟发现 W-2，2026-10-09）：换装已完成
            # （launcher 已 confirm：pending 消失、awaiting_health 清零、当前
            # 槽位=运行槽位）而上一进程的 ready_to_restart 残留在状态文件里
            # ——启动即落 healthy 终态，不让学生看着「等待重启」旧脸和旧版本
            # 号（真实链路里 confirm 只写文件、进程内存态不自愈）。
            if (
                str(self._state.get("state")) == "ready_to_restart"
                and not pending_path.exists()
                and str(current.get("version") or "") == self.current_version
                and not current.get("awaiting_health")
            ):
                self._save("healthy", current_version=self.current_version,
                           actions=["check"], rollback=self._rollback_snapshot())
            if pending_path.exists() and current.get("awaiting_health"):
                pending = json.loads(pending_path.read_text(encoding="utf-8-sig"))
                if pending.get("target_version") == current.get("version"):
                    remove_durable(pending_path)
            if current.get("awaiting_health") and self.now() - float(current.get("switched_at") or 0) > 120:
                self.rollback_if_unhealthy()
        except (OSError, ValueError, UpdateError):
            return

    def _read_current(self) -> dict[str, Any]:
        return json.loads(
            (self.install_root / "state" / "current.json").read_text(encoding="utf-8-sig")
        )

    def _rollback_snapshot(self) -> dict[str, Any]:
        try:
            current = json.loads((self.install_root / "state" / "current.json").read_text(encoding="utf-8-sig"))
            previous = str(current.get("previous_version") or "")
            available = bool(previous and (self.install_root / "versions" / previous).is_dir())
            return {"available": available, "version": previous if available else "", "state": "available" if available else "none"}
        except (OSError, ValueError):
            return {"available": False, "version": "", "state": "none"}

    def action(self, action: str, *, confirmed: bool = False) -> dict[str, Any]:
        action = action.strip().casefold()
        if action == "check":
            return self.check()
        if action == "download":
            if not confirmed:
                raise UpdateError("confirmation_required")
            return self.download()
        if action == "install":
            if not confirmed:
                raise UpdateError("confirmation_required")
            return self.install()
        if action == "update_now":
            if not confirmed:
                raise UpdateError("confirmation_required")
            return self.update_now()
        raise UpdateError("update_action_invalid")


def host_platform() -> tuple[str, str]:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    return ("windows" if system == "windows" else system, "x86_64" if machine in {"amd64", "x86_64"} else machine)
