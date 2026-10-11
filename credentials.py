"""Local encrypted credential storage for Fudan CourseLens.

Windows seals every payload with user-scope DPAPI exactly as before. On
macOS the same envelope rides the Keychain backend from
``src/platform/credentials.py`` (resolved lazily at call time); wherever
no backend exists the layer fails closed with a closed-set platform error.
Every Windows-only import stays inside a function so this module — and
therefore the whole client — imports cleanly on every platform.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from path_utils import DEFAULT_DATA_DIR, ensure_inside_project


DEFAULT_CREDENTIALS_FILE = DEFAULT_DATA_DIR / "credentials.json"
_LOCK_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.05

# ---------------------------------------------------------------------------
# Session checkpoint (V5): one DPAPI-protected, account-bound, short-TTL
# WebVPN cookie snapshot per account. Same store, same DPAPI boundary — no
# second credential store. The plaintext envelope holds only non-reversible
# binding hashes and TTL timestamps; cookie values exist only inside the
# DPAPI ciphertext. Every content doubt fails closed (discard + return None).
# ---------------------------------------------------------------------------

SESSION_CHECKPOINT_SCHEMA_VERSION = 1
SESSION_CHECKPOINT_TTL_SECONDS = 1800.0
_CHECKPOINT_MAX_COOKIES = 16
_CHECKPOINT_NAME_MAX = 128
_CHECKPOINT_VALUE_MAX = 4096
_CHECKPOINT_PATH_MAX = 256
_CHECKPOINT_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)
_CHECKPOINT_BINDING_PREFIX = "courselens.session-checkpoint.v1:"


def _checkpoint_binding(student_id: str) -> str:
    """Non-reversible account binding token; never the raw student id."""
    return hashlib.sha256(
        (_CHECKPOINT_BINDING_PREFIX + student_id).encode("utf-8")
    ).hexdigest()


def _validated_checkpoint_cookies(raw: object) -> list[dict[str, str]] | None:
    """Enforce the closed checkpoint-cookie shape; None means discard.

    Only name/value/domain/path strings within hard caps are accepted; the
    WebVPN capture layer is responsible for the single-host scoping, and this
    validator re-checks generic syntax so a hostile or corrupted payload can
    never smuggle in an unrestricted jar.
    """
    if not isinstance(raw, list) or not 0 < len(raw) <= _CHECKPOINT_MAX_COOKIES:
        return None
    cookies: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"name", "value", "domain", "path"}:
            return None
        name, value, domain, path = entry["name"], entry["value"], entry["domain"], entry["path"]
        if not all(isinstance(part, str) for part in (name, value, domain, path)):
            return None
        if not name or len(name) > _CHECKPOINT_NAME_MAX or not set(name) <= _CHECKPOINT_NAME_CHARS:
            return None
        if not value or len(value) > _CHECKPOINT_VALUE_MAX or "\r" in value or "\n" in value:
            return None
        if name != name.strip() or value != value.strip():
            return None
        domain = domain.casefold().lstrip(".")
        if (
            not domain or len(domain) > 253
            or any(mark in domain for mark in (":", "/", "?", "@", "#"))
        ):
            return None
        if (
            not path.startswith("/") or len(path) > _CHECKPOINT_PATH_MAX
            or any(mark in path for mark in ("?", "#", "\r", "\n"))
        ):
            return None
        cookies.append({"name": name, "value": value, "domain": domain, "path": path})
    return cookies


_DATA_BLOB_TYPE: type | None = None


def _data_blob_type() -> type:
    """Resolve the DPAPI BLOB structure lazily and cache it.

    ``ctypes.wintypes`` raises ValueError at import time on non-Windows
    platforms, so it must never be touched at import scope: the previous
    module-level structure definition broke every non-Windows import of
    the whole client. The first call imports wintypes and caches a
    structure whose field layout is identical to that old definition.
    """
    global _DATA_BLOB_TYPE
    if _DATA_BLOB_TYPE is None:
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        _DATA_BLOB_TYPE = _DataBlob
    return _DATA_BLOB_TYPE


def _blob_from_bytes(data: bytes) -> tuple[ctypes.Structure, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _data_blob_type()(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    )
    return blob, buffer


def _blob_to_bytes(blob: ctypes.Structure) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


# ---------------------------------------------------------------------------
# Ciphertext format versions (R7 additional entropy)
# ---------------------------------------------------------------------------
# v1 (legacy): bare base64 of the CurrentUser DPAPI blob, sealed without any
# optional entropy. v2 (current): "cp:v2:" prefix + base64 of the blob sealed
# with the constant application-bound entropy below. v1 tokens stay readable
# and are lazily re-saved as v2 after a successful read (see
# CredentialStore._migrate_legacy_v1_token).
#
# The entropy constant is not a secret and not a key: it only raises the bar
# against generic same-user dump scripts that expect bare CurrentUser blobs
# (a targeted attacker with process access can capture it). It MUST stay
# byte-stable for the life of v2 ciphertexts, and its literal value must
# never be copied into docs or result files.

CIPHERTEXT_V2_PREFIX = "cp:v2:"
_CIPHERTEXT_ENTROPY_V2 = (
    b"Fudan CourseLens saved-credential seal v2 :: CurrentUser-scope DPAPI "
    b"bound to this application (remembered logins, integration secrets, "
    b"session checkpoints). Rotating this constant invalidates every saved "
    b"credential on this machine."
)


def _is_legacy_v1_token(token: str) -> bool:
    """True when a stored token predates the v2 prefix (v1 read-compat path)."""
    return not token.startswith(CIPHERTEXT_V2_PREFIX)


_KEYCHAIN_TOKEN_PREFIX = "kc:v1:"  # src.platform.credentials.KEYCHAIN_TOKEN_PREFIX
_NON_KEYCHAIN_PLATFORM_MESSAGE = (
    "Saved credential storage requires Windows DPAPI or the macOS Keychain "
    "backend, which is unavailable in this build"
)


def _macos_keychain_backend():
    """Lazily resolve the macOS Keychain backend, or None when unavailable.

    The backend lives in ``src/platform/credentials.py`` (security CLI,
    standard library only). Resolving it lazily at call time keeps this
    module importable everywhere and keeps Windows entirely on its
    unchanged DPAPI path. Callers must treat None as fail-closed (the
    closed-set platform error above), never as a silent plaintext
    fallback.
    """
    if sys.platform != "darwin":
        return None
    try:
        from src.platform.credentials import MacOSKeychainCredentialsBackend
    except Exception:
        return None
    return MacOSKeychainCredentialsBackend()


def _dpapi_protect(data: bytes, entropy: bytes | None) -> str:
    """One CryptProtectData call with optional entropy; returns bare base64."""
    if os.name != "nt":
        raise RuntimeError("Saved credentials require Windows DPAPI on this platform")
    _DataBlob = _data_blob_type()  # lazy: wintypes must never load at import scope
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _blob_from_bytes(data)
    entropy_ref = None
    entropy_buffer = None
    if entropy is not None:
        entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
        entropy_ref = ctypes.byref(entropy_blob)
    out_blob = _DataBlob()
    _ = in_buffer
    _ = entropy_buffer
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        entropy_ref,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return base64.b64encode(_blob_to_bytes(out_blob)).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(token: str, entropy: bytes | None) -> bytes:
    """One CryptUnprotectData call with optional entropy; fails closed."""
    if os.name != "nt":
        raise RuntimeError("Saved credentials require Windows DPAPI on this platform")
    _DataBlob = _data_blob_type()  # lazy: wintypes must never load at import scope
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = base64.b64decode(token.encode("ascii"))
    in_blob, in_buffer = _blob_from_bytes(raw)
    entropy_ref = None
    entropy_buffer = None
    if entropy is not None:
        entropy_blob, entropy_buffer = _blob_from_bytes(entropy)
        entropy_ref = ctypes.byref(entropy_blob)
    out_blob = _DataBlob()
    _ = in_buffer
    _ = entropy_buffer
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        entropy_ref,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_to_bytes(out_blob)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _is_windows() -> bool:
    """One shared Windows predicate for the sealing-format dispatch."""
    return os.name == "nt"


def _protect(data: bytes) -> str:
    """Seal plaintext into the current storage format for this platform.

    Windows keeps the v2 DPAPI format byte-for-byte; macOS routes to the
    Keychain backend token, and any other platform fails closed.
    """
    if not _is_windows():
        backend = _macos_keychain_backend()
        if backend is None:
            raise RuntimeError(_NON_KEYCHAIN_PLATFORM_MESSAGE)
        return backend.protect(data)
    return CIPHERTEXT_V2_PREFIX + _dpapi_protect(data, _CIPHERTEXT_ENTROPY_V2)


def _protect_legacy_v1(data: bytes) -> str:
    """Construct the legacy v1 storage format (bare base64, no entropy).

    Production writes are v2-only; this exists so the v1 read-compat and
    lazy-migration paths can be tested against an authentic old-format token.
    """
    return _dpapi_protect(data, None)


def _unprotect(token: str) -> bytes:
    """Open one stored token; v1 decrypts entropy-free, v2 with app entropy.

    Keychain tokens route to the macOS backend before the DPAPI format
    dispatch: the bare-base64 v1 alphabet can never contain the ``:``
    separator, so a ``kc:v1:`` token can only come from a Keychain-minted
    envelope. Unknown formats and any entropy mismatch raise (fail closed).
    """
    if token.startswith(_KEYCHAIN_TOKEN_PREFIX):
        backend = _macos_keychain_backend()
        if backend is None:
            raise RuntimeError(_NON_KEYCHAIN_PLATFORM_MESSAGE)
        return backend.unprotect(token)
    if _is_legacy_v1_token(token):
        return _dpapi_unprotect(token, None)
    return _dpapi_unprotect(token[len(CIPHERTEXT_V2_PREFIX):], _CIPHERTEXT_ENTROPY_V2)


class CredentialStore:
    """Stores remembered accounts without exposing passwords to the frontend."""

    def __init__(self, path: str | Path = DEFAULT_CREDENTIALS_FILE):
        self.path = ensure_inside_project(Path(path))

    @contextmanager
    def _envelope_lock(self):
        """Serialize an envelope transaction across threads and processes."""
        identity = str(self.path.resolve()).casefold() if os.name == "nt" else str(self.path.resolve())
        with _LOCK_GUARD:
            local = _PATH_LOCKS.setdefault(identity, threading.RLock())
        if not local.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise RuntimeError("credential_envelope_lock_unavailable")
        handle = None
        locked = False
        mutex = None
        mutex_locked = False
        try:
            if os.name == "nt":
                mutex_name = "Local\\CourseLensCredential-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
                mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
                if not mutex or ctypes.windll.kernel32.WaitForSingleObject(mutex, int(_LOCK_TIMEOUT_SECONDS * 1000)) != 0:
                    raise RuntimeError("credential_envelope_lock_unavailable")
                mutex_locked = True
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a+b")
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("credential_envelope_lock_unavailable")
                    time.sleep(_LOCK_RETRY_SECONDS)
            yield
        finally:
            try:
                if locked and handle is not None:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()
                if mutex_locked:
                    ctypes.windll.kernel32.ReleaseMutex(mutex)
                if mutex:
                    ctypes.windll.kernel32.CloseHandle(mutex)
                local.release()

    def list_accounts(self) -> list[dict[str, str]]:
        data = self._read()
        accounts = data.get("accounts", {})
        return [
            {
                "student_id": student_id,
                "requires_rotation": bool(account.get("requires_rotation", False)),
            }
            for student_id, account in sorted(accounts.items())
            if isinstance(student_id, str) and isinstance(account, dict)
        ]

    def _mutate_envelope(self, mutation):
        """Run one complete read-modify-write transaction under the envelope lock."""
        with self._envelope_lock():
            data = self._read()
            result, changed = mutation(data)
            if changed:
                self._write(data)
            return result

    def _migrate_legacy_v1_token(self, holder, field: str, old_token: str, plaintext: bytes) -> None:
        """Lazily re-save one legacy v1 ciphertext in the current v2 format.

        Best-effort compare-and-swap under the envelope lock: the stored token
        is replaced only while it is still the exact v1 token that was just
        decrypted, so a concurrent save is never clobbered. Any failure leaves
        the still-readable v1 token in place; the next successful read retries.
        """
        if (
            not _is_legacy_v1_token(old_token)
            or old_token.startswith(_KEYCHAIN_TOKEN_PREFIX)
            or not plaintext
        ):
            # v1→v2 is a DPAPI-only concern: a Keychain handle is final, and
            # re-sealing one would mint a fresh Keychain entry (and orphan
            # the old secret) on every successful read.
            return
        try:
            new_token = _protect(plaintext)
        except Exception:
            return
        def mutate(data):
            target = holder(data)
            if not isinstance(target, dict) or target.get(field) != old_token:
                return None, False
            target[field] = new_token
            return None, True
        try:
            self._mutate_envelope(mutate)
        except Exception:
            pass

    def save(self, student_id: str, password: str) -> None:
        student_id = (student_id or "").strip()
        if not student_id or not password:
            raise ValueError("student_id and password are required")
        payload = json.dumps(
            {"student_id": student_id, "password": password},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        def mutate(data):
            data.setdefault("accounts", {})[student_id] = {
                "password": _protect(payload), "saved_at": time.time(), "requires_rotation": False,
            }
            return None, True
        self._mutate_envelope(mutate)

    def mark_account_rotation_required(self, student_id: str) -> bool:
        """Prevent reuse of a saved password known or suspected to be exposed."""
        student_id = (student_id or "").strip()
        if not student_id:
            raise ValueError("student_id is required")
        def mutate(data):
            account = data.setdefault("accounts", {}).get(student_id)
            if not isinstance(account, dict) or account.get("requires_rotation") is True:
                return False, False
            account["requires_rotation"] = True
            # Rotation discards this account's session checkpoint, if any.
            checkpoints = data.get("session_checkpoints")
            if isinstance(checkpoints, dict):
                checkpoints.pop(_checkpoint_binding(student_id), None)
            return True, True
        return self._mutate_envelope(mutate)

    def delete_account(self, student_id: str) -> bool:
        student_id = (student_id or "").strip()
        if not student_id:
            raise ValueError("student_id is required")
        def mutate(data):
            accounts = data.setdefault("accounts", {})
            existed = student_id in accounts; accounts.pop(student_id, None)
            if existed:
                # Deleting the account discards its session checkpoint, if any.
                checkpoints = data.get("session_checkpoints")
                if isinstance(checkpoints, dict):
                    checkpoints.pop(_checkpoint_binding(student_id), None)
            return existed, existed
        return self._mutate_envelope(mutate)

    # --- Session checkpoint: one DPAPI cookie snapshot per account (V5) ---

    def save_session_checkpoint(self, student_id: str, cookies: list[dict[str, str]]) -> None:
        """Persist one account-bound, short-TTL session checkpoint.

        `cookies` must already be host-scoped by the WebVPN capture layer;
        this store re-validates the closed shape and stores values only
        inside a DPAPI ciphertext. A shape violation raises instead of
        writing a weak entry.
        """
        student_id = (student_id or "").strip()
        if not student_id:
            raise ValueError("student_id is required")
        validated = _validated_checkpoint_cookies(cookies)
        if not validated:
            raise ValueError("session checkpoint cookies failed the closed shape")
        binding = _checkpoint_binding(student_id)
        payload = json.dumps(
            {
                "schema": SESSION_CHECKPOINT_SCHEMA_VERSION,
                "binding": binding,
                "cookies": validated,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        now = time.time()
        envelope = {
            "version": SESSION_CHECKPOINT_SCHEMA_VERSION,
            "created_at": now,
            "expires_at": now + SESSION_CHECKPOINT_TTL_SECONDS,
            "payload": _protect(payload),
        }
        def mutate(data):
            checkpoints = data.setdefault("session_checkpoints", {})
            if not isinstance(checkpoints, dict):
                data["session_checkpoints"] = checkpoints = {}
            checkpoints[binding] = envelope
            for expired in [
                key for key, entry in checkpoints.items()
                if isinstance(entry, dict)
                and float(entry.get("expires_at") or 0.0) <= now
            ]:
                checkpoints.pop(expired, None)
            return None, True
        self._mutate_envelope(mutate)

    def load_session_checkpoint(self, student_id: str) -> list[dict[str, str]] | None:
        """Return the account's checkpoint cookies, or None on any doubt.

        Expiry, unknown schema, a missing or rotation-flagged account,
        decryption uncertainty, payload/schema/binding mismatch or any
        cookie outside the closed shape each delete the checkpoint and
        return None, so the caller falls back to ordinary login.
        """
        student_id = (student_id or "").strip()
        if not student_id:
            return None
        binding = _checkpoint_binding(student_id)
        try:
            data = self._read()
        except Exception:
            return None
        checkpoints = data.get("session_checkpoints")
        entry = checkpoints.get(binding) if isinstance(checkpoints, dict) else None
        if not isinstance(entry, dict):
            return None
        invalid = False
        cookies: list[dict[str, str]] | None = None
        legacy_token = ""
        raw_payload = b""
        try:
            if entry.get("version") != SESSION_CHECKPOINT_SCHEMA_VERSION:
                invalid = True
            elif float(entry.get("expires_at") or 0.0) <= time.time():
                invalid = True
            else:
                account = data.get("accounts", {}).get(student_id)
                if not isinstance(account, dict) or account.get("requires_rotation") is True:
                    invalid = True
                else:
                    legacy_token = str(entry["payload"])
                    raw_payload = _unprotect(legacy_token)
                    payload = json.loads(raw_payload.decode("utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or payload.get("schema") != SESSION_CHECKPOINT_SCHEMA_VERSION
                        or payload.get("binding") != binding
                    ):
                        invalid = True
                    else:
                        cookies = _validated_checkpoint_cookies(payload.get("cookies"))
                        if not cookies:
                            invalid = True
        except Exception:
            invalid = True
        if invalid or cookies is None:
            self._discard_checkpoint_entry(binding)
            return None
        self._migrate_legacy_v1_token(
            lambda data: (data.get("session_checkpoints") or {}).get(binding),
            "payload", legacy_token, raw_payload,
        )
        return cookies

    def clear_session_checkpoint(self, student_id: str) -> bool:
        student_id = (student_id or "").strip()
        if not student_id:
            return False
        return self._discard_checkpoint_entry(_checkpoint_binding(student_id))

    def clear_all_session_checkpoints(self) -> int:
        def mutate(data):
            checkpoints = data.get("session_checkpoints")
            if not isinstance(checkpoints, dict) or not checkpoints:
                return 0, False
            removed = len(checkpoints)
            data["session_checkpoints"] = {}
            return removed, True
        return self._mutate_envelope(mutate)

    def _discard_checkpoint_entry(self, binding: str) -> bool:
        def mutate(data):
            checkpoints = data.get("session_checkpoints")
            if not isinstance(checkpoints, dict) or binding not in checkpoints:
                return False, False
            checkpoints.pop(binding, None)
            return True, True
        try:
            return self._mutate_envelope(mutate)
        except Exception:
            return False

    def save_deepseek_key(self, api_key: str) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        payload = json.dumps(
            {"api_key": api_key},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        def mutate(data):
            data.setdefault("secrets", {})["deepseek_api_key"] = {
                "value": _protect(payload), "saved_at": time.time(), "requires_rotation": False,
            }
            return None, True
        self._mutate_envelope(mutate)

    def mark_deepseek_key_rotation_required(self) -> bool:
        def mutate(data):
            secret = data.setdefault("secrets", {}).get("deepseek_api_key")
            if not isinstance(secret, dict) or secret.get("requires_rotation") is True:
                return False, False
            secret["requires_rotation"] = True
            return True, True
        return self._mutate_envelope(mutate)

    def deepseek_key_requires_rotation(self) -> bool:
        secret = self._read().get("secrets", {}).get("deepseek_api_key")
        return bool(isinstance(secret, dict) and secret.get("requires_rotation") is True)

    def save_secret(self, name: str, value: str) -> None:
        """Store a named integration secret under the current Windows user.

        Names are deliberately restricted so callers cannot create nested
        shapes or collide with account entries.  The JSON file contains only
        a DPAPI ciphertext, never the supplied value.
        """
        name = (name or "").strip()
        value = value if isinstance(value, str) else str(value)
        if not name or not all(char.isalnum() or char in "._-:" for char in name):
            raise ValueError("secret name contains unsupported characters")
        if not value:
            raise ValueError("secret value is required")
        def mutate(data):
            secrets = data.setdefault("secrets", {})
            payload = json.dumps(
                {"name": name, "value": value},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            secrets[name] = {"value": _protect(payload)}
            return None, True
        self._mutate_envelope(mutate)

    def update_secrets(
        self,
        updates: dict[str, str],
        *,
        deletes: tuple[str, ...] = (),
    ) -> None:
        """Atomically replace a related set of DPAPI-protected secrets.

        The complete JSON replacement contains ciphertext only.  This is used
        for trust-boundary transitions where a repository identifier and its
        matching public keys must never become visible as a mixed generation.
        """
        protected: dict[str, dict[str, str]] = {}
        for raw_name, raw_value in updates.items():
            name = (raw_name or "").strip()
            value = raw_value if isinstance(raw_value, str) else str(raw_value)
            if not name or not all(char.isalnum() or char in "._-:" for char in name):
                raise ValueError("secret name contains unsupported characters")
            if not value:
                raise ValueError("secret value is required")
            payload = json.dumps(
                {"name": name, "value": value},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            protected[name] = {"value": _protect(payload)}

        def mutate(data):
            secrets = data.setdefault("secrets", {})
            secrets.update(protected)
            for name in deletes:
                secrets.pop(str(name), None)
            return None, True
        self._mutate_envelope(mutate)

    def load_secret(self, name: str) -> str:
        name = (name or "").strip()
        data = self._read()
        secret = data.get("secrets", {}).get(name)
        if not secret:
            raise KeyError(f"Saved secret not found: {name}")
        raw = _unprotect(secret["value"])
        payload = json.loads(raw.decode("utf-8"))
        if str(payload.get("name") or "") != name:
            raise RuntimeError("Saved secret name does not match its DPAPI payload")
        self._migrate_legacy_v1_token(
            lambda data: data.get("secrets", {}).get(name), "value", secret["value"], raw,
        )
        return str(payload["value"])

    def has_secret(self, name: str) -> bool:
        return bool(self._read().get("secrets", {}).get((name or "").strip()))

    def list_secret_names(self, *, prefix: str = "") -> list[str]:
        """Return integration secret names only, without decrypting any value."""
        normalized = str(prefix or "")
        names = (
            str(name)
            for name in self._read().get("secrets", {})
            if isinstance(name, str)
        )
        return sorted(name for name in names if not normalized or name.startswith(normalized))

    def delete_secret(self, name: str) -> bool:
        def mutate(data):
            secrets = data.setdefault("secrets", {})
            existed = name in secrets
            secrets.pop(name, None)
            return existed
        def wrapped(data):
            result = mutate(data)
            return result, result
        return self._mutate_envelope(wrapped)

    def load_deepseek_key(self) -> str:
        data = self._read()
        secret = data.get("secrets", {}).get("deepseek_api_key")
        if not secret:
            raise KeyError("Saved DeepSeek API key not found")
        if secret.get("requires_rotation") is True:
            raise RuntimeError("保存的 DeepSeek API Key 需要更新")
        raw = _unprotect(secret["value"])
        payload = json.loads(raw.decode("utf-8"))
        self._migrate_legacy_v1_token(
            lambda data: data.get("secrets", {}).get("deepseek_api_key"),
            "value", secret["value"], raw,
        )
        return str(payload["api_key"])

    def has_deepseek_key(self) -> bool:
        data = self._read()
        secret = data.get("secrets", {}).get("deepseek_api_key")
        return bool(isinstance(secret, dict) and secret.get("requires_rotation") is not True)

    def delete_deepseek_key(self) -> bool:
        def mutate(data):
            secrets = data.setdefault("secrets", {})
            existed = "deepseek_api_key" in secrets; secrets.pop("deepseek_api_key", None)
            return existed, existed
        return self._mutate_envelope(mutate)

    def load(self, student_id: str) -> tuple[str, str]:
        student_id = (student_id or "").strip()
        data = self._read()
        account = data.get("accounts", {}).get(student_id)
        if not account:
            raise KeyError("Saved user not found")
        if account.get("requires_rotation") is True:
            raise RuntimeError("保存的密码需要更新，请重新输入新密码")
        raw = _unprotect(account["password"])
        payload = json.loads(raw.decode("utf-8"))
        self._migrate_legacy_v1_token(
            lambda data: data.get("accounts", {}).get(student_id),
            "password", account["password"], raw,
        )
        return str(payload["student_id"]), str(payload["password"])

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "accounts": {}, "secrets": {}, "session_checkpoints": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Saved credentials file is not valid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Saved credentials file has an invalid shape")
        data.setdefault("version", 1)
        data.setdefault("accounts", {})
        data.setdefault("secrets", {})
        data.setdefault("session_checkpoints", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent,
        )
        temporary = Path(raw_temporary)
        original_error: BaseException | None = None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(_LOCK_RETRY_SECONDS)
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            # Cleanup failure must never replace the write/replace failure.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                if original_error is None:
                    raise
