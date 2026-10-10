"""GitHub App device authorization and per-student repository bootstrap.

The GitHub App private key is never shipped with the desktop application.
All user actions use an expiring user access token protected by Windows DPAPI.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import threading
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import requests
from nacl.public import PublicKey, SealedBox

from credentials import CredentialStore
from src.distribution import DISTRIBUTION_REPOSITORY
from src.runtime.test_mode import EgressBlockedError, ensure_egress_allowed
from shared.protocol.mirror import (
    MirrorVerificationError,
    load_json_bytes,
    verify_manifest_document,
)

from .protocol import (
    PROTOCOL_VERSION,
    generate_box_keypair,
    generate_signing_keypair,
    validate_task_id,
)


API_ROOT = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_VERSION = "2026-03-10"
# PARK-N3：信任门只读 GET 缓存壳（进程内、短 TTL）。仅缓存「按完整 40 位
# commit SHA 寻址」的只读端点——Git 对象不可变性保证同一 URL 恒返回同一
# 字节；每次派发的签名/摘要复核仍在本地照常重跑，信任边界语义零变化。
# 个人 Worker 仓的 commits/main（可变分支，信任判定的活输入）、身份/安装/
# 工作流/secrets/variables 等可变面与一切变更类操作永不入缓存。
READONLY_GET_CACHE_TTL_SECONDS = 120.0
_READONLY_GET_CACHE_MAX_ENTRIES = 32
_IMMUTABLE_COMMIT_PATH = re.compile(r"^/repos/[^/]+/[^/]+/commits/[0-9a-f]{40}$")
_IMMUTABLE_CONTENTS_PATH = re.compile(r"^/repos/[^/]+/[^/]+/contents/.+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
WORKER_TEMPLATE = DISTRIBUTION_REPOSITORY
WORKER_ENVIRONMENT = "courselens-worker"
REQUIRED_INSTALLATION_REPOSITORIES = frozenset({
    "fudan-courselens-worker",
    "fudan-courselens-mailbox",
})
MANAGED_DESCRIPTION = "Managed by Fudan CourseLens desktop client"
# 创建即存的仓库元数据键（闭集字段：id/full_name/owner/private/managed）。
# 绑定与元数据在仓库创建/复用成功的当下立即落盘，使预选安装链接从建仓起
# 即可组装、重跑在 App 安装前可依保存元数据续走（未安装时用户令牌读不了
# 私有 mailbox，403 不得终断首跑动作链）。
MANAGED_REPO_META_KEY = "github_managed_repo_meta"
# P59-WORKER-AUTOREPAIR-1：Worker 自动修复的诚实留痕行（服务器日志恰一行；
# 失败不替换调用方既有闭集报错，「修复 Worker」手动入口保持兜底）。
WORKER_AUTO_SYNC_REPAIRED_LOG = "Worker 已自动同步到最新版"
WORKER_AUTO_SYNC_FALLBACK_LOG = "Worker 自动同步未完成，保留「修复 Worker」手动入口"
_MANAGED_MAILBOX_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "repository-readmes"
    / "mailbox"
)
MANAGED_MAILBOX_DOCUMENTS = {
    "README.md": _MANAGED_MAILBOX_ROOT / "README.md",
    "docs/technical/README.md": _MANAGED_MAILBOX_ROOT / "docs" / "technical" / "README.md",
}


def _same_repository(left: str, right: str) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _bundled_runtime_assets() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "runtime-assets.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bundled_app_config() -> dict[str, str]:
    value = _bundled_runtime_assets().get("github_app") or {}
    return {
        "client_id": str(value.get("client_id") or "").strip(),
        "slug": str(value.get("slug") or "").strip(),
    }


def _bundled_worker_config() -> dict[str, Any]:
    assets = _bundled_runtime_assets()
    mirror = assets.get("worker_mirror") or {}
    value = mirror.get("active") or {}
    return {
        "mode": "signed-mirror",
        "repository": str(value.get("repository") or WORKER_TEMPLATE).strip(),
        "commit": str(value.get("commit") or "").strip().lower(),
        "tree": str(value.get("tree") or "").strip().lower(),
        "manifest_sha256": str(value.get("manifest_sha256") or "").strip().lower(),
        "signing_key_id": str(value.get("signing_key_id") or "").strip().lower(),
        "trust_epoch": int(value.get("trust_epoch") or 0),
        "protocol_versions": list(value.get("protocol_versions") or []),
        "root_keys": dict(mirror.get("root_keys") or {}),
        "paths": dict(mirror.get("paths") or {}),
    }


# Closed-set endpoint classes carried by every GitHub API error so that
# observations and the frontend can tell identity/token refusals apart from
# installation/scope refusals and secondary rate limits — without ever
# exposing GitHub payload text.  Unknown labels collapse to "".
ENDPOINT_CLASSES = frozenset({
    "token_endpoint",
    "user_identity",
    "user_installations",
    "user_repos",
    "repos_detail",
    "repos_actions",
})


class GitHubAppError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "github_error",
        request_id: str = "",
        retry_after: float = 0.0,
        endpoint_class: str = "",
    ):
        super().__init__(message)
        self.code = str(code or "github_error")
        self.request_id = str(request_id or "")
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.endpoint_class = str(endpoint_class or "") if str(endpoint_class or "") in ENDPOINT_CLASSES else ""


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int

    def public(self) -> dict[str, Any]:
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_at": self.expires_at,
            "interval": self.interval,
        }


class GitHubAppClient:
    def __init__(
        self,
        credentials: CredentialStore,
        *,
        client_id: str = "",
        app_slug: str = "",
        proxy_url: str = "",
        timeout: int = 30,
    ):
        self.credentials = credentials
        bundled = _bundled_app_config()
        self.client_id = str(
            client_id or os.environ.get("FUDAN_COURSELENS_GITHUB_APP_CLIENT_ID") or bundled.get("client_id") or ""
        ).strip()
        if not self.client_id and credentials.has_secret("github_app_client_id"):
            self.client_id = credentials.load_secret("github_app_client_id").strip()
        self.app_slug = str(
            app_slug or os.environ.get("FUDAN_COURSELENS_GITHUB_APP_SLUG") or bundled.get("slug") or ""
        ).strip()
        if not self.app_slug and credentials.has_secret("github_app_slug"):
            self.app_slug = credentials.load_secret("github_app_slug").strip()
        self.timeout = max(5, int(timeout))
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Fudan-CourseLens-Student/2",
        })
        self._job_token_lock = threading.RLock()
        self._job_token_leases = 0
        # P59：自动修复「每进程·每 pin 目标 commit 恰一次」的记账（成功或
        # 失败都消耗额度），防止派发重试循环对 GitHub API 形成修复风暴。
        self._auto_repair_lock = threading.Lock()
        self._auto_repair_targets: set[str] = set()
        self._rate_limit: dict[str, Any] = {}
        # PARK-N3：只读 GET 缓存（键→(过期时刻, 响应字节)）。仅白名单端点
        # 入缓存（见模块头注释）；命中时零网络请求，_rate_limit 保持最近
        # 一次真实观测，绝不以缓存数据伪造限流读数。
        self._readonly_get_cache: dict[str, tuple[float, bytes]] = {}
        self._readonly_get_cache_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.app_slug)

    @property
    def installation_url(self) -> str:
        return f"https://github.com/apps/{self.app_slug}/installations/new" if self.app_slug else ""

    def start_device_authorization(self) -> DeviceAuthorization:
        if not self.configured:
            raise GitHubAppError("CourseLens GitHub App 尚未由项目维护者配置")
        response = self._request_external(
            "POST",
            DEVICE_CODE_URL,
            data={"client_id": self.client_id},
            expected=(200,),
        )
        payload = response.json()
        return DeviceAuthorization(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload.get("verification_uri") or "https://github.com/login/device"),
            expires_at=time.time() + int(payload.get("expires_in") or 900),
            interval=max(5, int(payload.get("interval") or 5)),
        )

    def poll_device_authorization(self, authorization: DeviceAuthorization) -> dict[str, Any]:
        if time.time() >= authorization.expires_at:
            return {"state": "expired"}
        response = self._request_external(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "device_code": authorization.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            expected=(200,),
        )
        payload = response.json()
        error = str(payload.get("error") or "")
        if error in {"authorization_pending", "slow_down"}:
            # RFC 8628 §3.5：收到 slow_down 后轮询间隔必须 +5s——此前只置标志
            # 无人消费，客户端会保持原间隔连续触发慢放；interval 随 pending
            # 结果回传，前端既有 result.interval 分支即生效。
            interval = int(authorization.interval or 5) + (5 if error == "slow_down" else 0)
            return {"state": "pending", "slow_down": error == "slow_down", "interval": interval}
        if error:
            return {"state": "expired" if error in {"expired_token", "incorrect_device_code"} else "error"}
        token = str(payload.get("access_token") or "")
        if not token:
            raise GitHubAppError("GitHub 未返回访问令牌")
        # A device code is single-use: the grant the user just approved in the
        # browser is persisted the moment GitHub issues it, before any further
        # network call, so no later hiccup can discard it.
        self._save_tokens(payload)
        result: dict[str, Any] = {
            "state": "authorized",
            "login": "",
            "account_id": 0,
            "installed": False,
            "installation_status": "unavailable",
        }
        try:
            identity = self._api("GET", "/user", token=token).json()
            owner = str(identity.get("login") or "")
            account_id = int(identity.get("id") or 0)
            if not owner or not account_id:
                # A malformed identity response is not evidence the grant is
                # bad; keep the grant and let verification/bootstrap close in.
                return result
            result["login"] = owner
            result["account_id"] = account_id
            # Authorization and installation are separate GitHub states.  A
            # fresh account without an App installation still completes
            # authorization; the installation id is saved only for a fresh
            # matching installation.
            installation, installation_status = self._fresh_installation_state(owner, token=token)
            result["installation_status"] = installation_status
            result["installed"] = installation_status == "bound"
            if result["installed"]:
                self._save_related_secrets(
                    {"github_app_installation_id": str(int(installation["id"]))}
                )
            return result
        except GitHubAppError as exc:
            if exc.code == "authorization_revoked" or (
                exc.code == "permission_denied"
                and exc.endpoint_class == "user_identity"
            ):
                # Only a definitive identity/token verdict (401 anywhere, or a
                # 403 on the identity endpoint itself) proves the fresh grant
                # invalid.  A refused installation/scope call must never
                # discard the single-use grant the user just approved.
                self.clear_user_authorization()
                raise
            # Transient failures (network, 5xx, rate limiting, secondary
            # limits, refused installation payloads) never throw the stored
            # grant away; the closed-set degraded shape lets bootstrap/probe
            # finish later.
            return result
        except Exception:
            return result

    def access_token(self, *, minimum_lifetime_seconds: int = 900, no_refresh: bool = False) -> str:
        if not self.credentials.has_secret("github_app_access_token"):
            raise GitHubAppError("GitHub 尚未授权")
        expires_at = self._float_secret("github_app_access_expires_at")
        fresh = math.isfinite(expires_at) and expires_at - time.time() >= max(60, int(minimum_lifetime_seconds))
        if not fresh:
            if no_refresh:
                raise GitHubAppError("GitHub 授权不够新鲜", code="authorization_not_fresh")
            self.refresh_access_token()
        return self.credentials.load_secret("github_app_access_token")

    def refresh_access_token(self) -> None:
        if not self.credentials.has_secret("github_app_refresh_token"):
            raise GitHubAppError("GitHub 授权已过期，请重新连接")
        response = self._request_external(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.credentials.load_secret("github_app_refresh_token"),
            },
            expected=(200,),
        )
        payload = response.json()
        if payload.get("error"):
            raise GitHubAppError("GitHub 授权刷新失败，请重新连接")
        self._save_tokens(payload)

    def verify_user_authorization(self, *, minimum_lifetime_seconds: int = 900) -> dict[str, bool]:
        """Verify the stored user grant without exposing identity or token data."""
        token = self.access_token(minimum_lifetime_seconds=minimum_lifetime_seconds)
        identity = self._api("GET", "/user", token=token, expected=(200,)).json()
        owner = str(identity.get("login") or "")
        account_bound = bool(owner and int(identity.get("id") or 0))
        _installation, installation_status = (
            self._fresh_installation_state(owner, token=token)
            if account_bound else (None, "missing")
        )
        return {
            "authorized": account_bound,
            "installed": installation_status == "bound",
        }

    def bootstrap_student_repositories(self, *, progress: Any = None) -> dict[str, Any]:
        """Create or reuse the two managed repositories, then finish setup.

        The first-run order is deliberately non-circular: the verified user
        grant alone authorizes repository creation, and App installation is
        demanded only afterwards.  When the installation or its scope is not
        ready, the result is a structured, recoverable intermediate state
        carrying the trusted installation/settings URL instead of a generic
        failure; re-running this method is idempotent at every boundary.

        ``progress`` is an optional closed-set stage reporter called with the
        same stage keys as the application layer's REMOTE_ACTION_STAGE_LABELS
        (creating_repositories/syncing_documents/verifying_worker/
        repairing_worker/configuring_secrets).  Reporter failures never abort
        the bootstrap; this is a live-progress channel only.
        """

        def report(stage: str) -> None:
            if progress is None:
                return
            try:
                progress(stage)
            except Exception:
                pass

        token = self.access_token(minimum_lifetime_seconds=1800)
        identity = self._api("GET", "/user", token=token).json()
        owner = str(identity.get("login") or "")
        account_id = int(identity.get("id") or 0)
        if not owner or not account_id:
            raise GitHubAppError("无法确认 GitHub 账号")

        def create_worker(name: str) -> dict[str, Any]:
            release = _bundled_worker_config()
            template_repository = str(release.get("repository") or WORKER_TEMPLATE)
            template_owner, template_repo = template_repository.split("/", 1)
            response = self._api(
                "POST",
                f"/repos/{template_owner}/{template_repo}/generate",
                token=token,
                expected=(201,),
                json={
                    "owner": owner,
                    "name": name,
                    "description": MANAGED_DESCRIPTION,
                    "private": False,
                    "include_all_branches": False,
                },
            )
            try:
                payload = response.json()
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def create_mailbox(name: str) -> dict[str, Any]:
            response = self._api(
                "POST",
                "/user/repos",
                token=token,
                expected=(201,),
                json={
                    "name": name,
                    "description": MANAGED_DESCRIPTION,
                    "private": True,
                    "has_issues": True,
                    "has_projects": False,
                    "has_wiki": False,
                    "has_downloads": False,
                    "auto_init": True,
                },
            )
            try:
                payload = response.json()
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}

        worker_repo = ""
        mailbox_repo = ""
        worker_meta: dict[str, Any] = {}
        mailbox_meta: dict[str, Any] = {}
        report("creating_repositories")
        try:
            worker_repo, worker_meta = self._ensure_managed_repository(
                owner, "Fudan-CourseLens-Worker", role="worker",
                stored_secret="github_worker_repo", private=False, token=token,
                create=create_worker,
            )
            # 创建即存：Worker 就绪的当下立即落盘属主绑定与创建时元数据。
            self._remember_managed_repository(
                "worker", owner, "github_worker_repo", worker_repo, worker_meta, private=False
            )
            mailbox_repo, mailbox_meta = self._ensure_managed_repository(
                owner, "Fudan-CourseLens-Mailbox", role="mailbox",
                stored_secret="github_mailbox_repo", private=True, token=token,
                create=create_mailbox,
            )
            self._remember_managed_repository(
                "mailbox", owner, "github_mailbox_repo", mailbox_repo, mailbox_meta, private=True
            )
        except GitHubAppError as exc:
            if not getattr(exc, "unverified_reuse_denial", False):
                raise
            # 遗留状态降级：复用路径私有仓读取 403 且零创建证据——仓库已存在
            # 只是 App 未安装读不了。重新分类安装状态后按 awaiting_installation
            # 返回：install-first，缺安装给账号级官方安装页（不发明仓库 id），
            # 范围类给受信设置页链接；已验证的仓库名如实带出，其余留空。
            installation, install_status = self._fresh_installation_state(
                owner, token=token, require_exact_repository_selection=True
            )
            awaiting: dict[str, Any] = {
                **self.snapshot(),
                "setup_state": "awaiting_installation",
                "installation_status": install_status,
                "installation_setup_url": self.installation_url if install_status == "missing" else "",
                "installation_settings_url": "",
                "worker_repo": worker_repo,
                "mailbox_repo": mailbox_repo,
            }
            if install_status in {"scope_invalid", "not_exact"} and installation:
                awaiting["installation_settings_url"] = (
                    f"https://github.com/settings/installations/{int(installation['id'])}"
                )
            if install_status == "missing":
                # 遗留降级同样观察到「安装缺失」：安装就绪后同样需要重发一次
                self.credentials.save_secret("github_token_pre_install", "1")
            return awaiting
        installation, status = self._fresh_installation_state(
            owner, token=token, require_exact_repository_selection=True
        )
        if status != "bound":
            # 安装缺失期间用过令牌：记录「安装 无→有」需重发一次用户令牌（T5）
            self.credentials.save_secret("github_token_pre_install", "1")
            awaiting: dict[str, Any] = {
                **self.snapshot(),
                "setup_state": "awaiting_installation",
                "installation_status": status,
                "installation_setup_url": "",
                "installation_settings_url": "",
                "worker_repo": worker_repo,
                "mailbox_repo": mailbox_repo,
            }
            if status == "missing":
                awaiting["installation_setup_url"] = self._installation_setup_url(
                    owner, account_id, worker_meta, mailbox_meta
                )
            elif status in {"scope_invalid", "not_exact"} and installation:
                awaiting["installation_settings_url"] = (
                    f"https://github.com/settings/installations/{int(installation['id'])}"
                )
            return awaiting
        # 安装即重发令牌（T5）：安装缺失期间用过的用户令牌可能缺少安装后的
        # 作用域——检测到安装已就绪（无→有）时自动刷新恰一次，免去手动
        # Revoke 重授权。刷新失败上抛且标记保留：下次 bootstrap 恰重试一次；
        # 无 refresh token 时只清标记，绝不把可完成的初始化变成授权报错。
        if self.credentials.has_secret("github_token_pre_install"):
            if self.credentials.has_secret("github_app_refresh_token"):
                self.refresh_access_token()
                token = self.credentials.load_secret("github_app_access_token")
            self.credentials.delete_secret("github_token_pre_install")
        self._save_related_secrets({"github_app_installation_id": str(int(installation["id"]))})
        report("syncing_documents")
        self._sync_managed_mailbox_documents(mailbox_repo, token)
        report("verifying_worker")
        integrity = self.check_worker_integrity()
        if not integrity["trusted"]:
            report("repairing_worker")
            integrity = self.repair_worker()
        if not integrity["trusted"]:
            raise GitHubAppError(
                "专属 Worker 版本与 CourseLens 安全模板不一致，请先执行一键修复 Worker"
            )
        self._api(
            "PUT",
            f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}",
            token=token,
            expected=(200, 201,),
            json={},
        )
        report("configuring_secrets")
        # 成对自愈：密钥只在「本地公钥 + 远端环境私钥」两侧齐备时才视为已
        # 建立。仓库删除重建后远端环境 secrets 为空而本地残留旧公钥，只看
        # 本地会误跳过上传并造成 environment_incomplete 死锁；任一侧缺失
        # 即重新生成密钥对并上传/落存（与 inspect 的环境 secrets 列举同形）。
        secrets_response = self._api(
            "GET",
            f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}/secrets",
            token=token,
            expected=(200, 404),
        )
        remote_secret_names = (
            {
                str(item.get("name") or "")
                for item in list(secrets_response.json().get("secrets") or [])
                if str(item.get("name") or "")
            }
            if secrets_response.status_code == 200
            else set()
        )
        needs_regeneration = not (
            all(self.credentials.has_secret(name) for name in (
                "worker_box_public_key", "worker_signing_public_key"
            ))
            and all(
                name in remote_secret_names
                for name in ("WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY")
            )
        )
        if needs_regeneration:
            worker_private, worker_public = generate_box_keypair()
            signing_private, signing_public = generate_signing_keypair()
            self._put_environment_secret(worker_repo, "WORKER_INPUT_PRIVATE_KEY", worker_private, token)
            self._put_environment_secret(worker_repo, "WORKER_SIGNING_PRIVATE_KEY", signing_private, token)
            self._save_related_secrets({
                "worker_box_public_key": worker_public,
                "worker_signing_public_key": signing_public,
            })
            # rotate_worker_keys 语义：密钥换新后旧完整性证据作废；立即重验
            # 以免初始化成功却呈现「Worker 不受信」。重验失败不回滚密钥建立，
            # 快照如实反映未受信态，由既有修复入口接管。
            self.credentials.delete_secret("github_worker_verified_tree")
            self.credentials.delete_secret("github_worker_verified_manifest")
            try:
                self.check_worker_integrity()
            except GitHubAppError:
                pass
        self._put_repo_variable(worker_repo, "COURSELENS_MAILBOX_REPO", mailbox_repo, token)
        self._put_repo_variable(worker_repo, "COURSELENS_ASR_STRATEGY", "sequential", token)
        # N9（夜14-R1 定谳）：OCR 并发钉 2。代码侧帽 min(2, env)（ocr.py）不变，
        # 幻灯多的讲 OCR 墙钟约 −30-50%；worker 侧工作流回退默认同调为 2。
        self._put_repo_variable(worker_repo, "COURSELENS_OCR_CONCURRENCY", "2", token)
        self._put_repo_variable(worker_repo, "COURSELENS_IMAGE_PREFETCH", "16", token)
        # remote_enabled is deliberately NOT written here: bootstrap leaves the
        # global pause on; only the explicit enable-remote-compute connection
        # action (which requires a verified channel test) may lift it.
        return {**self.snapshot(), "setup_state": "complete"}

    def _sync_managed_mailbox_documents(self, mailbox_repo: str, token: str) -> dict[str, bool]:
        """Idempotently synchronize every managed Mailbox document.

        A successful return means every template was read and every remote
        path was confirmed.  If a later document fails after an earlier path
        was checked or updated, the caller receives a partial-sync error and
        must not present the document set as synchronized.
        """
        results: dict[str, bool] = {}
        for repository_path, template_path in MANAGED_MAILBOX_DOCUMENTS.items():
            try:
                content = template_path.read_bytes()
            except OSError as exc:
                raise GitHubAppError(
                    "CourseLens Mailbox managed document template is missing",
                    code=(
                        "mailbox_documents_partial_sync"
                        if results else "mailbox_document_template_missing"
                    ),
                ) from exc
            try:
                response = self._api(
                    "GET",
                    f"/repos/{mailbox_repo}/contents/{repository_path}",
                    token=token,
                    expected=(200, 404),
                )
                existing_sha = ""
                if response.status_code == 200:
                    payload = response.json()
                    existing_sha = str(payload.get("sha") or "").strip()
                    encoded = str(payload.get("content") or "").replace("\n", "")
                    try:
                        if base64.b64decode(encoded, validate=True) == content:
                            results[repository_path] = False
                            continue
                    except (ValueError, TypeError):
                        pass
                body: dict[str, Any] = {
                    "message": f"docs: sync managed CourseLens Mailbox {repository_path}",
                    "content": base64.b64encode(content).decode("ascii"),
                }
                if existing_sha:
                    body["sha"] = existing_sha
                self._api(
                    "PUT",
                    f"/repos/{mailbox_repo}/contents/{repository_path}",
                    token=token,
                    expected=(200, 201),
                    json=body,
                )
                results[repository_path] = True
            except GitHubAppError as exc:
                raise GitHubAppError(
                    "CourseLens Mailbox managed documents could not be fully synchronized",
                    code=(
                        "mailbox_documents_partial_sync"
                        if results else "mailbox_document_sync_failed"
                    ),
                    request_id=exc.request_id,
                    retry_after=exc.retry_after,
                ) from exc
        return results

    def sync_managed_mailbox_documents(self) -> dict[str, bool]:
        """Synchronize the managed Mailbox docs for an established client."""
        if not self.credentials.has_secret("github_mailbox_repo"):
            raise GitHubAppError(
                "尚未创建专属 Mailbox",
                code="mailbox_repository_missing",
            )
        token = self.access_token(minimum_lifetime_seconds=300)
        mailbox_repo = self.credentials.load_secret("github_mailbox_repo")
        return self._sync_managed_mailbox_documents(mailbox_repo, token)

    def rotate_worker_keys(self) -> dict[str, Any]:
        """Rotate Worker private keys after the caller has excluded active jobs."""
        integrity = self.check_worker_integrity()
        if not integrity.get("trusted"):
            raise GitHubAppError(
                "Worker is not trusted; repair it before rotating keys",
                code="worker_tree_drifted",
            )
        if not self.credentials.has_secret("github_worker_repo"):
            raise GitHubAppError("Worker repository is missing", code="worker_repository_missing")
        token = self.access_token(minimum_lifetime_seconds=1800)
        worker_repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "PUT", f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}",
            token=token, expected=(200, 201), json={},
        )
        worker_private, worker_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        self._put_environment_secret(worker_repo, "WORKER_INPUT_PRIVATE_KEY", worker_private, token)
        self._put_environment_secret(worker_repo, "WORKER_SIGNING_PRIVATE_KEY", signing_private, token)
        self.credentials.save_secret("worker_box_public_key", worker_public)
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        self.credentials.delete_secret("github_worker_verified_tree")
        self.credentials.delete_secret("github_worker_verified_manifest")
        verified = self.check_worker_integrity()
        return {**self.snapshot(), "keys_rotated": True, "worker_trusted": bool(verified.get("trusted"))}

    def _public_release_document(
        self, repository: str, commit: str, path: str, token: str
    ) -> dict[str, Any]:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
            raise GitHubAppError(
                "Worker mirror metadata path is invalid", code="worker_manifest_invalid"
            )
        payload = self._api(
            "GET",
            f"/repos/{repository}/contents/{quote(normalized, safe='/')}?ref={quote(commit, safe='')}",
            token=token,
        ).json()
        if str(payload.get("type") or "") != "file" or str(payload.get("encoding") or "").lower() != "base64":
            raise GitHubAppError(
                "Worker mirror metadata is not a regular base64 file",
                code="worker_manifest_invalid",
            )
        try:
            encoded = str(payload.get("content") or "").encode("ascii")
            # GitHub's Contents API wraps Base64 at fixed-width lines. Remove
            # only ASCII whitespace before retaining strict alphabet/padding
            # validation so malformed metadata still fails closed.
            raw = base64.b64decode(b"".join(encoded.split()), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise GitHubAppError(
                "Worker mirror metadata encoding is invalid", code="worker_manifest_invalid"
            ) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise GitHubAppError(
                "Worker mirror metadata is unexpectedly large", code="worker_manifest_invalid"
            )
        try:
            return load_json_bytes(raw, label=normalized)
        except MirrorVerificationError as exc:
            raise GitHubAppError(str(exc), code=exc.code) from exc

    def _validated_worker_release(self, *, token: str | None = None) -> dict[str, Any]:
        expected = _bundled_worker_config()
        expected = {**expected, "mode": str(expected.get("mode") or "signed-mirror")}
        if expected.get("mode") != "signed-mirror":
            raise GitHubAppError(
                "Only the approved signed Worker mirror is supported",
                code="worker_release_unavailable",
            )
        repository = str(expected.get("repository") or "")
        commit = str(expected.get("commit") or "")
        tree = str(expected.get("tree") or "")
        if not repository or len(commit) != 40 or len(tree) != 40:
            raise GitHubAppError(
                "CourseLens has no complete approved Worker release",
                code="worker_release_unavailable",
            )
        access_token = token or self.access_token(minimum_lifetime_seconds=900)
        public_commit = self._api(
            "GET", f"/repos/{repository}/commits/{commit}", token=access_token
        ).json()
        actual_commit = str(public_commit.get("sha") or "").strip().lower()
        actual_tree = str(
            ((public_commit.get("commit") or {}).get("tree") or {}).get("sha") or ""
        ).strip().lower()
        if actual_commit != commit or actual_tree != tree:
            raise GitHubAppError(
                "Approved public Worker commit or tree has drifted",
                code="worker_public_tree_drift",
            )

        paths = dict(expected.get("paths") or {})
        names = {
            "manifest": str(paths.get("manifest") or "worker-mirror.manifest.json"),
            "manifest_signature": str(paths.get("manifest_signature") or "worker-mirror.manifest.sig"),
            "trust": str(paths.get("trust") or "worker-mirror-trust.json"),
            "trust_signature": str(paths.get("trust_signature") or "worker-mirror-trust.sig"),
        }
        documents = {
            name: self._public_release_document(repository, commit, path, access_token)
            for name, path in names.items()
        }
        try:
            verification = verify_manifest_document(
                documents["manifest"],
                documents["manifest_signature"],
                documents["trust"],
                documents["trust_signature"],
                root_keys=dict(expected.get("root_keys") or {}),
                supported_protocol_versions=(PROTOCOL_VERSION,),
            )
        except MirrorVerificationError as exc:
            raise GitHubAppError(str(exc), code=exc.code) from exc
        if verification["manifest_sha256"] != str(expected.get("manifest_sha256") or ""):
            raise GitHubAppError(
                "Approved Worker manifest digest does not match", code="worker_manifest_invalid"
            )
        if verification["key_id"] != str(expected.get("signing_key_id") or ""):
            raise GitHubAppError(
                "Approved Worker signing key does not match", code="worker_manifest_invalid"
            )
        if verification["trust_epoch"] < int(expected.get("trust_epoch") or 0):
            raise GitHubAppError(
                "Worker trust metadata is older than the approved epoch",
                code="worker_release_unavailable",
            )
        configured_protocols = {str(value) for value in expected.get("protocol_versions") or []}
        if configured_protocols and not configured_protocols.intersection(verification["protocol_versions"]):
            raise GitHubAppError(
                "Approved Worker protocol metadata is inconsistent",
                code="worker_protocol_incompatible",
            )
        return {**expected, **verification, "manifest_verified": True}

    def check_worker_integrity(self, *, read_only: bool = False) -> dict[str, Any]:
        """Verify the managed Worker tree before transmitting any authorization."""
        bundled = _bundled_worker_config()
        expected_tree = bundled["tree"]
        if len(expected_tree) != 40:
            raise GitHubAppError("CourseLens 未固定可信 Worker 模板")
        if not self.credentials.has_secret("github_worker_repo"):
            raise GitHubAppError("尚未创建专属 Worker", code="cloud_setup_required")
        worker_repo = self.credentials.load_secret("github_worker_repo")
        if _same_repository(worker_repo, str(bundled.get("repository") or "")):
            # The public template only publishes signed releases; it must never
            # be validated as an executor or dispatched to.
            raise GitHubAppError(
                "CourseLens 公共模板仅用于发布，不再执行任务；请重新初始化你的专属 Worker",
                code="personal_worker_migration_required",
            )
        token = self.access_token(minimum_lifetime_seconds=900, no_refresh=read_only)
        expected = self._validated_worker_release(token=token)
        payload = self._api(
            "GET", f"/repos/{worker_repo}/commits/main", token=token
        ).json()
        actual_commit = str(payload.get("sha") or "").strip().lower()
        actual_tree = str(
            ((payload.get("commit") or {}).get("tree") or {}).get("sha") or ""
        ).strip().lower()
        trusted = actual_tree == expected_tree
        if not read_only:
            if trusted:
                self.credentials.save_secret("github_worker_verified_tree", actual_tree)
                if expected.get("manifest_sha256"):
                    self.credentials.save_secret(
                        "github_worker_verified_manifest", str(expected["manifest_sha256"])
                    )
            else:
                self.credentials.delete_secret("github_worker_verified_tree")
                self.credentials.delete_secret("github_worker_verified_manifest")
            # Retire any legacy direct-dispatch cache value on every write path.
            self.credentials.delete_secret("github_worker_dispatch_sha")
        return {
            "trusted": trusted,
            "repository": worker_repo,
            "actual_commit": actual_commit,
            "actual_tree": actual_tree,
            "expected_commit": expected["commit"],
            "expected_tree": expected_tree,
            "mirror_mode": expected.get("mode"),
            "manifest_verified": bool(expected.get("manifest_verified")),
            "manifest_sha256": str(expected.get("manifest_sha256") or ""),
            "signing_key_id": str(expected.get("signing_key_id") or ""),
            "trust_epoch": int(expected.get("trust_epoch") or 0),
            "dispatch_mode": "personal-worker",
            "dispatch_head_sha": "",
        }

    def inspect_managed_resources(self, *, read_only: bool = False) -> dict[str, Any]:
        """Read live GitHub metadata required for a safe dispatch.

        The result intentionally contains no token, secret value, Issue body,
        workflow log, or repository content.  Missing managed resources are
        represented as booleans so the caller can provide precise remediation.
        """
        token = self.access_token(minimum_lifetime_seconds=900, no_refresh=True) if read_only else self.access_token(minimum_lifetime_seconds=900)
        identity = self._api("GET", "/user", token=token).json()
        owner = str(identity.get("login") or "").strip()
        account_id = int(identity.get("id") or 0)
        if not owner or not account_id:
            raise GitHubAppError("GitHub identity is unavailable", code="identity_unavailable")
        installation = self._find_user_installation(owner, token=token)
        worker_repo = self.credentials.load_secret("github_worker_repo") if self.credentials.has_secret("github_worker_repo") else ""
        mailbox_repo = self.credentials.load_secret("github_mailbox_repo") if self.credentials.has_secret("github_mailbox_repo") else ""

        def repository(repo: str, *, role: str, private: bool) -> dict[str, Any]:
            if not repo:
                return {"exists": False}
            # Read-side twin of the bootstrap ownership rule in
            # _ensure_managed_repository: a stored binding naming another
            # account's repository is treated as absent and is never probed
            # with the current token.  A public foreign Worker would otherwise
            # answer GET /repos with 200 and freeze the deeper sections on 403.
            if str(repo).split("/", 1)[0].casefold() != owner.casefold():
                return {"exists": False, "binding_owner_mismatch": True}
            try:
                response = self._api("GET", f"/repos/{repo}", token=token, expected=(200, 404))
            except GitHubAppError as exc:
                # 预安装降级（D2）：私有仓库在 App 安装前对用户令牌不可读，
                # repo 级 403 + 属主匹配的创建时元数据 = 「已建待安装」——
                # 闭集标记 pre_install，不抛、不死链；无创建时证据仍上抛。
                degraded = self._created_but_unverifiable_meta(
                    exc, owner, repo, role=role, private=private
                )
                if degraded is None:
                    raise
                return {**degraded, "exists": True, "pre_install": True}
            if response.status_code == 404:
                return {"exists": False}
            value = response.json()
            return {
                "exists": True,
                "id": int(value.get("id") or 0),
                "full_name": str(value.get("full_name") or repo),
                "owner": str((value.get("owner") or {}).get("login") or ""),
                "private": bool(value.get("private")),
                "archived": bool(value.get("archived")),
                "disabled": bool(value.get("disabled")),
                "has_issues": bool(value.get("has_issues")),
                "default_branch": str(value.get("default_branch") or ""),
                "managed": str(value.get("description") or "") == MANAGED_DESCRIPTION,
                "is_template": bool(value.get("is_template")),
            }

        worker = repository(worker_repo, role="worker", private=False)
        mailbox = repository(mailbox_repo, role="mailbox", private=True)
        worker_tree = ""
        worker_commit = ""
        workflows: dict[str, dict[str, Any]] = {}
        environment_exists = False
        secret_names: list[str] = []
        variables: dict[str, str] = {}
        actions_enabled = False
        # A repo-level 403 (installation/scope refusal, never an identity
        # verdict) degrades to a closed-set flag instead of killing the whole
        # probe chain: identity and installation evidence stay valid and the
        # refused section is simply unobservable.
        repos_access_denied = ""
        if worker.get("exists"):
            try:
                commit = self._api(
                    "GET", f"/repos/{worker_repo}/commits/main", token=token, expected=(200, 404)
                )
                if commit.status_code == 200:
                    payload = commit.json()
                    worker_commit = str(payload.get("sha") or "").strip().lower()
                    worker_tree = str(
                        ((payload.get("commit") or {}).get("tree") or {}).get("sha") or ""
                    ).strip().lower()
                for workflow_name in ("process.yml", "echo.yml", "cloud-verify.yml", "cloud-daily.yml"):
                    response = self._api(
                        "GET",
                        f"/repos/{worker_repo}/actions/workflows/{workflow_name}",
                        token=token,
                        expected=(200, 404),
                    )
                    workflows[workflow_name] = (
                        {"exists": False, "state": "missing"}
                        if response.status_code == 404
                        else {
                            "exists": True,
                            "state": str(response.json().get("state") or "unknown"),
                        }
                    )
                actions = self._api(
                    "GET", f"/repos/{worker_repo}/actions/permissions", token=token, expected=(200, 404)
                )
                actions_enabled = actions.status_code == 200 and bool(actions.json().get("enabled"))
                environment = self._api(
                    "GET",
                    f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}",
                    token=token,
                    expected=(200, 404),
                )
                environment_exists = environment.status_code == 200
                if environment_exists:
                    secrets = self._api(
                        "GET",
                        f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}/secrets",
                        token=token,
                        expected=(200, 404),
                    )
                    if secrets.status_code == 200:
                        secret_names = sorted(
                            str(item.get("name") or "")
                            for item in list(secrets.json().get("secrets") or [])
                            if str(item.get("name") or "")
                        )
                variable_response = self._api(
                    "GET", f"/repos/{worker_repo}/actions/variables", token=token,
                    expected=(200, 404), params={"per_page": 100},
                )
                if variable_response.status_code == 200:
                    variables = {
                        str(item.get("name") or ""): str(item.get("value") or "")
                        for item in list(variable_response.json().get("variables") or [])
                        if str(item.get("name") or "")
                    }
            except GitHubAppError as exc:
                if exc.code != "permission_denied" or exc.endpoint_class not in {
                    "repos_detail", "repos_actions",
                }:
                    raise
                repos_access_denied = exc.endpoint_class
        expected = _bundled_worker_config()
        installation_view = self._installation_public_view(installation, token=token)
        return {
            "identity": {"login": owner, "account_id": account_id},
            "installation": installation_view,
            # Only composed when the App is not installed and both freshly read
            # managed repositories are exact; otherwise an empty string so no
            # un-preselected installation link can ever be rendered.
            "installation_setup_url": (
                "" if installation_view.get("installed")
                else self._installation_setup_url(owner, account_id, worker, mailbox)
            ),
            # Closed ENDPOINT_CLASSES value naming the refused repo-level
            # section; empty when every repo probe was observable.
            "repos_access_denied": repos_access_denied,
            "worker": worker,
            "mailbox": mailbox,
            "worker_commit": worker_commit,
            "worker_tree": worker_tree,
            "expected_commit": expected["commit"],
            "expected_tree": expected["tree"],
            "worker_dispatch_mode": "personal-worker",
            "workflows": workflows,
            "actions_enabled": actions_enabled,
            "environment_exists": environment_exists,
            "secret_names": secret_names,
            "variables": variables,
            "rate_limit": self.rate_limit_snapshot(),
        }

    def _installation_public_view(
        self, installation: dict[str, Any] | None, *, token: str
    ) -> dict[str, Any]:
        """Summarize installation state plus repository-selection evidence.

        The result intentionally contains no token and no id other than the
        opaque installation id.
        """
        if not installation:
            return {
                "installed": False,
                "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": sorted(
                    REQUIRED_INSTALLATION_REPOSITORIES
                ),
                "unexpected_installation_repositories": [],
            }
        installation_id = int(installation.get("id") or 0)
        view: dict[str, Any] = {
            "installed": True,
            "installation_id": installation_id,
        }
        if installation_id <= 0:
            view.update({
                "repository_selection_exact": False,
                "missing_installation_repositories": sorted(
                    REQUIRED_INSTALLATION_REPOSITORIES
                ),
                "unexpected_installation_repositories": [],
                "repository_selection_error": "installation_repository_inventory_unavailable",
            })
            return view
        discovered_names: set[str] = set()
        unexpected_full_names: list[str] = []
        raw_count = 0
        total_count: int | None = None
        try:
            for page in range(1, 101):
                payload = self._api(
                    "GET",
                    f"/user/installations/{installation_id}/repositories",
                    token=token,
                    expected=(200,),
                    params={"per_page": 100, "page": page},
                ).json()
                repositories = list(payload.get("repositories") or [])
                if total_count is None:
                    value = payload.get("total_count")
                    if type(value) is not int or value < 0:
                        raise ValueError("invalid total")
                    total_count = value
                for repository in repositories:
                    value = dict(repository or {})
                    name = str(value.get("name") or "").casefold()
                    full_name = str(value.get("full_name") or "").strip()
                    if not name or name in discovered_names or not full_name:
                        raise ValueError("duplicate or invalid repository")
                    discovered_names.add(name)
                    if name not in REQUIRED_INSTALLATION_REPOSITORIES:
                        unexpected_full_names.append(full_name)
                    raw_count += 1
                if len(repositories) < 100:
                    break
            else:
                raise ValueError("repository inventory too large")
        except Exception:
            # A transient enumeration failure must not fabricate which
            # repositories are missing; the selection is simply unknown.
            view.update({
                "repository_selection_exact": False,
                "missing_installation_repositories": [],
                "unexpected_installation_repositories": [],
                "repository_selection_error": (
                    "installation_repository_inventory_unavailable"
                ),
            })
            return view
        missing = sorted(REQUIRED_INSTALLATION_REPOSITORIES - discovered_names)
        unexpected = sorted(set(unexpected_full_names))
        view.update({
            "repository_selection_exact": bool(
                total_count == raw_count
                and discovered_names == REQUIRED_INSTALLATION_REPOSITORIES
            ),
            "missing_installation_repositories": missing,
            "unexpected_installation_repositories": unexpected,
        })
        return view

    def rate_limit_snapshot(self) -> dict[str, Any]:
        return dict(self._rate_limit)

    def ensure_worker_trusted(self) -> dict[str, Any]:
        """Dispatch-gate variant of check_worker_integrity with one auto-repair.

        派发/媒体授权前的信任核查：版本不一致时先自动尝试一次 repair_worker
        （每进程对同一 pin 目标 commit 恰一次，成功或失败都记账）。成功→
        返回受信结果、派发照常继续（用户无感）；失败（含全部既有 guard 拒绝）
        →本方法绝不抛、绝不改写调用方的既有闭集报错路径，只原样返回未受信
        结果，由调用方按既有文案报错，手动「修复 Worker」保持兜底。版本
        一致时除既有检查外零额外远程调用。
        """
        integrity = self.check_worker_integrity()
        if integrity.get("trusted"):
            return integrity
        repaired = self._auto_repair_worker_once()
        if repaired is not None and repaired.get("trusted"):
            return repaired
        return integrity

    def _auto_repair_worker_once(self) -> dict[str, Any] | None:
        """Best-effort single repair per process per pinned target commit.

        Never raises：guard 拒绝与网络失败都只写一行闭集留痕后返回 None，
        调用方的既有错误面保持逐字不变。
        """
        target = str(_bundled_worker_config().get("commit") or "")
        with self._auto_repair_lock:
            if target in self._auto_repair_targets:
                return None
            self._auto_repair_targets.add(target)
        try:
            repaired = self.repair_worker()
        except GitHubAppError as exc:
            print(
                f"[FudanCourseLens] {WORKER_AUTO_SYNC_FALLBACK_LOG}"
                f"（{exc.code or 'github_error'}）",
                flush=True,
            )
            return None
        except Exception:
            print(f"[FudanCourseLens] {WORKER_AUTO_SYNC_FALLBACK_LOG}", flush=True)
            return None
        if repaired.get("trusted"):
            print(f"[FudanCourseLens] {WORKER_AUTO_SYNC_REPAIRED_LOG}", flush=True)
        return repaired

    def repair_worker(self) -> dict[str, Any]:
        """Replace the managed Worker main tree with the pinned public template.

        The destination must be the public repository created by CourseLens.
        Every source object is read from the pinned public template commit, and
        the rebuilt tree must have the exact bundled digest before ``main`` is
        advanced.  This deliberately does not accept a caller-supplied repo,
        branch, commit, or path.
        """
        if not self.credentials.has_secret("github_worker_repo"):
            raise GitHubAppError("Worker repository is missing", code="worker_repository_missing")
        worker_repo = self.credentials.load_secret("github_worker_repo")
        bundled = _bundled_worker_config()
        if _same_repository(worker_repo, str(bundled.get("repository") or "")):
            # Never touch template main and never report the template trusted.
            raise GitHubAppError(
                "CourseLens 公共模板仅用于发布，不再执行任务；请重新初始化你的专属 Worker",
                code="personal_worker_migration_required",
            )
        try:
            token = self.access_token(minimum_lifetime_seconds=1800)
        except GitHubAppError as exc:
            # U⑫ 分流：修复漏斗里令牌过期 ≠ 从未配置。上抛闭集码，前端据此
            # 只给「重新授权」处方，绝不把过期令牌误判成需要重演全套首跑。
            raise GitHubAppError("GitHub 授权已过期，请重新授权", code="authorization_refresh_required") from exc
        expected = self._validated_worker_release(token=token)
        template_repo = expected["repository"]
        template_commit = expected["commit"]
        expected_tree = expected["tree"]
        if not template_repo or len(template_commit) != 40 or len(expected_tree) != 40:
            raise GitHubAppError("CourseLens 未固定可信 Worker 模板")
        if not self.credentials.has_secret("github_worker_repo"):
            raise GitHubAppError("尚未创建专属 Worker", code="cloud_setup_required")

        worker_repo = self.credentials.load_secret("github_worker_repo")
        if _same_repository(worker_repo, template_repo):
            raise GitHubAppError(
                "CourseLens 公共模板仅用于发布，不再执行任务；请重新初始化你的专属 Worker",
                code="personal_worker_migration_required",
            )
        identity = self._api("GET", "/user", token=token).json()
        owner = str(identity.get("login") or "").strip().lower()
        repository = self._api("GET", f"/repos/{worker_repo}", token=token).json()
        repo_owner = str((repository.get("owner") or {}).get("login") or "").strip().lower()
        if (
            not owner
            or repo_owner != owner
            or bool(repository.get("private"))
            or str(repository.get("description") or "") != MANAGED_DESCRIPTION
            or str(repository.get("default_branch") or "") != "main"
        ):
            raise GitHubAppError("拒绝修复：目标不是 CourseLens 创建的公开 Worker")

        current = self._api(
            "GET", f"/repos/{worker_repo}/commits/main", token=token
        ).json()
        current_commit = str(current.get("sha") or "").strip().lower()
        current_tree = str(
            ((current.get("commit") or {}).get("tree") or {}).get("sha") or ""
        ).strip().lower()
        if current_tree == expected_tree:
            return self.check_worker_integrity()
        if len(current_commit) != 40:
            raise GitHubAppError("无法确认 Worker 当前版本")

        source = self._api(
            "GET",
            f"/repos/{template_repo}/git/trees/{template_commit}?recursive=1",
            token=token,
        ).json()
        if bool(source.get("truncated")):
            raise GitHubAppError("可信 Worker 模板清单不完整，已停止修复")
        source_tree = list(source.get("tree") or [])
        blobs = [dict(item) for item in source_tree if str(item.get("type") or "") == "blob"]
        if not blobs or len(blobs) > 512:
            raise GitHubAppError("可信 Worker 模板文件数量异常，已停止修复")

        rebuilt_entries: list[dict[str, str]] = []
        allowed_modes = {"100644", "100755"}
        total_size = 0
        for item in blobs:
            path = str(item.get("path") or "")
            mode = str(item.get("mode") or "")
            blob_sha = str(item.get("sha") or "").strip().lower()
            size = int(item.get("size") or 0)
            if (
                not path
                or path.startswith("/")
                or ".." in Path(path).parts
                or mode not in allowed_modes
                or len(blob_sha) != 40
                or size < 0
            ):
                raise GitHubAppError("可信 Worker 模板包含非法文件，已停止修复")
            total_size += size
            if total_size > 8 * 1024 * 1024:
                raise GitHubAppError("可信 Worker 模板体积异常，已停止修复")
            payload = self._api(
                "GET", f"/repos/{template_repo}/git/blobs/{blob_sha}", token=token
            ).json()
            if str(payload.get("sha") or "").strip().lower() != blob_sha:
                raise GitHubAppError("可信 Worker 模板对象校验失败")
            if str(payload.get("encoding") or "").lower() != "base64":
                raise GitHubAppError("可信 Worker 模板对象编码异常")
            created = self._api(
                "POST",
                f"/repos/{worker_repo}/git/blobs",
                token=token,
                expected=(201,),
                json={"content": str(payload.get("content") or ""), "encoding": "base64"},
            ).json()
            if str(created.get("sha") or "").strip().lower() != blob_sha:
                raise GitHubAppError("Worker 文件写入校验失败")
            rebuilt_entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})

        rebuilt = self._api(
            "POST",
            f"/repos/{worker_repo}/git/trees",
            token=token,
            expected=(201,),
            json={"tree": rebuilt_entries},
        ).json()
        if str(rebuilt.get("sha") or "").strip().lower() != expected_tree:
            raise GitHubAppError("Worker 重建结果与可信模板不一致，未切换主分支")

        commit = self._api(
            "POST",
            f"/repos/{worker_repo}/git/commits",
            token=token,
            expected=(201,),
            json={
                "message": f"sync trusted CourseLens worker {template_commit[:12]}",
                "tree": expected_tree,
                "parents": [current_commit],
            },
        ).json()
        repaired_commit = str(commit.get("sha") or "").strip().lower()
        if len(repaired_commit) != 40:
            raise GitHubAppError("GitHub 未返回有效的 Worker 修复版本")
        self._api(
            "PATCH",
            f"/repos/{worker_repo}/git/refs/heads/main",
            token=token,
            expected=(200,),
            json={"sha": repaired_commit, "force": False},
        )
        result = self.check_worker_integrity()
        if not result["trusted"]:
            raise GitHubAppError("Worker 修复后校验失败，已阻止发送授权")
        return {**result, "repaired": True}

    def sync_job_token(self) -> str:
        token = self.access_token(minimum_lifetime_seconds=6 * 60 * 60)
        worker_repo = self.credentials.load_secret("github_worker_repo")
        self._put_environment_secret(worker_repo, "COURSELENS_JOB_TOKEN", token, token)
        self.credentials.save_secret("github_remote_token", token)
        self.credentials.delete_secret("github_job_token_cleanup_pending")
        return token

    def delete_job_token(self) -> None:
        if not self.credentials.has_secret("github_worker_repo"):
            return
        if (
            not self.credentials.has_secret("github_app_installation_id")
            and not self.credentials.has_secret("github_remote_token")
            and not self.credentials.has_secret("github_job_token_cleanup_pending")
        ):
            # 创建即存后，未安装状态也可能已有 Worker 绑定；此时本客户端从未
            # 签发过任务令牌，远端不可能存在由本机写入的 COURSELENS_JOB_TOKEN，
            # 跳过远端清理（安装前用户令牌也无权触达该端点，调用只会 403）。
            return
        token = self.access_token(minimum_lifetime_seconds=60)
        worker_repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "DELETE",
            f"/repos/{worker_repo}/environments/{WORKER_ENVIRONMENT}/secrets/COURSELENS_JOB_TOKEN",
            token=token,
            expected=(204, 404),
        )
        self.credentials.delete_secret("github_remote_token")
        self.credentials.delete_secret("github_job_token_cleanup_pending")

    def acquire_job_token(self, *, task_id: str = "", task_store: Any = None) -> int:
        """Keep the transient runner token alive while at least one job needs it."""
        with self._job_token_lock:
            if task_id and task_store is not None:
                task_store.set_remote_token_lease(
                    task_id, state="acquiring", expires_at=time.time() + 8 * 60 * 60
                )
            if self._job_token_leases == 0:
                try:
                    # P59：派发门自动修复——版本不一致先自动修一次（恰一次
                    # 记账），失败时下方既有闭集报错原样保留。
                    integrity = self.ensure_worker_trusted()
                    if not integrity["trusted"]:
                        raise GitHubAppError(
                            "专属 Worker 已被修改或版本过旧，已阻止发送媒体授权",
                            code="worker_tree_drifted",
                        )
                    self.sync_job_token()
                except Exception:
                    if task_id and task_store is not None:
                        task_store.delete_remote_token_lease(task_id)
                    raise
            self._job_token_leases += 1
            if task_id and task_store is not None:
                task_store.set_remote_token_lease(
                    task_id, state="active", expires_at=time.time() + 8 * 60 * 60
                )
            return self._job_token_leases

    def release_job_token(self, *, task_id: str = "", task_store: Any = None) -> int:
        """Release one job lease and remove the runner secret after the last job."""
        with self._job_token_lock:
            if self._job_token_leases <= 0:
                return 0
            self._job_token_leases -= 1
            remaining = self._job_token_leases
            if task_id and task_store is not None:
                task_store.delete_remote_token_lease(task_id)
            persistent_remaining = bool(
                task_store is not None and task_store.list_remote_token_leases()
            )
            if remaining == 0 and not persistent_remaining:
                try:
                    self.delete_job_token()
                except Exception:
                    # The access token is expiring and the next acquire retries by
                    # overwriting it.  Persist only a boolean repair marker; never
                    # copy the token or the GitHub response into diagnostics.
                    self.credentials.save_secret("github_job_token_cleanup_pending", "1")
            return remaining

    @contextmanager
    def job_token_lease(self, *, task_id: str = "", task_store: Any = None):
        self.acquire_job_token(task_id=task_id, task_store=task_store)
        try:
            yield
        finally:
            self.release_job_token(task_id=task_id, task_store=task_store)

    def cleanup_stale_job_token_lease(self, *, task_id: str, task_store: Any) -> None:
        """Remove one crashed task's durable lease and transient Worker token.

        The caller must first prove the matching GitHub run completed at the
        signed head.  This method additionally refuses any foreign lease,
        active remote task, or live in-process lease before touching the token.
        """
        task_id = validate_task_id(task_id)
        with self._job_token_lock:
            if self._job_token_leases:
                raise GitHubAppError(
                    "A live job-token lease prevents stale cleanup",
                    code="job_token_lease_active",
                )
            leases = list(task_store.list_remote_token_leases())
            lease_ids = {str(item.get("task_id") or "") for item in leases}
            if lease_ids - {task_id}:
                raise GitHubAppError(
                    "Another remote task owns the job-token lease",
                    code="job_token_lease_active",
                )
            if int(task_store.active_remote_run_count()) != 0:
                raise GitHubAppError(
                    "An active remote task prevents stale token cleanup",
                    code="job_token_lease_active",
                )
            worker_secret_names = {
                str(item.get("name") or "") for item in self.list_worker_secrets()
            }
            needs_cleanup = bool(
                leases
                or "COURSELENS_JOB_TOKEN" in worker_secret_names
                or self.credentials.has_secret("github_remote_token")
                or self.credentials.has_secret("github_job_token_cleanup_pending")
            )
            if needs_cleanup:
                self.delete_job_token()
            task_store.delete_remote_token_lease(task_id)

    def finalize_process_canary_token_lease(
        self, *, task_id: str, task_store: Any
    ) -> None:
        """Finish one canary lease in env-token-before-durable-lease order."""
        task_id = validate_task_id(task_id)
        with self._job_token_lock:
            if self._job_token_leases not in {0, 1}:
                raise GitHubAppError(
                    "Another live job-token lease prevents canary cleanup",
                    code="job_token_lease_active",
                )
            leases = list(task_store.list_remote_token_leases())
            lease_ids = {str(item.get("task_id") or "") for item in leases}
            if lease_ids - {task_id}:
                raise GitHubAppError(
                    "Another remote task owns the job-token lease",
                    code="job_token_lease_active",
                )
            if int(task_store.active_remote_run_count()) != 0:
                raise GitHubAppError(
                    "An active remote task prevents canary token cleanup",
                    code="job_token_lease_active",
                )
            worker_secret_names = {
                str(item.get("name") or "") for item in self.list_worker_secrets()
            }
            if (
                "COURSELENS_JOB_TOKEN" in worker_secret_names
                or self.credentials.has_secret("github_remote_token")
                or self.credentials.has_secret("github_job_token_cleanup_pending")
            ):
                self.delete_job_token()
            self._job_token_leases = 0
            task_store.delete_remote_token_lease(task_id)

    def snapshot(self) -> dict[str, Any]:
        def saved(name: str) -> str:
            return self.credentials.load_secret(name) if self.credentials.has_secret(name) else ""
        worker = _bundled_worker_config()
        verified_tree = saved("github_worker_verified_tree")
        verified_manifest = saved("github_worker_verified_manifest")
        mirror_verified = (
            bool(worker.get("manifest_sha256"))
            and verified_manifest == worker.get("manifest_sha256")
        )
        return {
            "app_configured": self.configured,
            "installation_url": self.installation_url,
            "authorized": self.credentials.has_secret("github_app_access_token"),
            "installed": bool(saved("github_app_installation_id")),
            "worker_repo": saved("github_worker_repo"),
            "mailbox_repo": saved("github_mailbox_repo"),
            "bootstrapped": all(self.credentials.has_secret(name) for name in (
                "github_worker_repo", "github_mailbox_repo", "worker_box_public_key", "worker_signing_public_key"
            )),
            "job_token_cleanup_pending": self.credentials.has_secret(
                "github_job_token_cleanup_pending"
            ),
            "worker_trusted": bool(
                worker["tree"] and verified_tree == worker["tree"] and mirror_verified
            ),
            "worker_template_commit": worker["commit"],
            "worker_mirror_mode": worker.get("mode"),
            "worker_dispatch_mode": "personal-worker",
            "worker_manifest_sha256": str(worker.get("manifest_sha256") or ""),
            "worker_signing_key_id": str(worker.get("signing_key_id") or ""),
            "authorization_state": "stored" if self.credentials.has_secret("github_app_access_token") else "missing",
            "access_expires_at": self._float_secret("github_app_access_expires_at"),
            "refresh_available": self.credentials.has_secret("github_app_refresh_token"),
            "rate_limit": self.rate_limit_snapshot(),
        }

    def disconnect(self) -> None:
        for name in (
            "github_app_access_token", "github_app_access_expires_at", "github_app_refresh_token",
            "github_app_refresh_expires_at", "github_remote_token", "github_worker_repo",
            "github_mailbox_repo", "worker_box_public_key", "worker_signing_public_key", "remote_enabled",
            "github_app_installation_id", "github_job_token_cleanup_pending",
            "github_worker_verified_tree", "github_worker_verified_manifest",
            "github_worker_dispatch_sha", "github_token_pre_install", MANAGED_REPO_META_KEY,
        ):
            self.credentials.delete_secret(name)

    def clear_user_authorization(self) -> None:
        """Forget OAuth tokens without deleting managed repositories or keys."""
        for name in (
            "github_app_access_token",
            "github_app_access_expires_at",
            "github_app_refresh_token",
            "github_app_refresh_expires_at",
            "github_remote_token",
            "github_app_installation_id",
            "github_token_pre_install",
        ):
            self.credentials.delete_secret(name)

    def _save_tokens(self, payload: dict[str, Any], *, installation_id: int | None = None) -> None:
        token = str(payload.get("access_token") or "")
        if not token:
            raise GitHubAppError("GitHub 未返回访问令牌")
        updates = {
            "github_app_access_token": token,
            "github_app_access_expires_at": str(time.time() + int(payload.get("expires_in") or 8 * 60 * 60)),
        }
        refresh = str(payload.get("refresh_token") or "")
        if refresh:
            updates["github_app_refresh_token"] = refresh
            updates["github_app_refresh_expires_at"] = str(time.time() + int(payload.get("refresh_token_expires_in") or 180 * 24 * 60 * 60))
        if installation_id is not None:
            updates["github_app_installation_id"] = str(int(installation_id))
        self._save_related_secrets(updates)

    def _readback_managed_repository(
        self, owner: str, repo: str, *, private: bool, token: str
    ) -> dict[str, Any] | None:
        """Fresh single-repository readback with closed-set identity checks.

        Returns ``None`` when the repository does not exist.  A path that
        resolves to a different owner, an unreadable id, a visibility flip, or
        a lost CourseLens managed marker raises instead of being adopted.
        """
        response = self._api("GET", f"/repos/{repo}", token=token, expected=(200, 404))
        if response.status_code == 404:
            return None
        payload = response.json()
        repository_id = int(payload.get("id") or 0)
        repository_owner = str((payload.get("owner") or {}).get("login") or "")
        if repository_id <= 0 or repository_owner.casefold() != str(owner).casefold():
            raise GitHubAppError(
                "CourseLens 专属仓库地址解析到其他账号，已停止初始化",
                code="managed_repository_invalid",
            )
        if bool(payload.get("private")) != bool(private) or str(payload.get("description") or "") != MANAGED_DESCRIPTION:
            raise GitHubAppError(
                "CourseLens 专属仓库名称已被一个非受管同名仓库占用："
                "请在 GitHub 重命名或删除该无关仓库后重试；不要将无关仓库选入 App。",
                code="managed_repository_name_conflict",
            )
        return {
            "id": repository_id,
            "full_name": str(payload.get("full_name") or repo),
            "owner": repository_owner,
            "private": bool(private),
            "managed": True,
        }

    def _managed_repo_meta(self) -> dict[str, Any]:
        """Saved creation-time metadata for the managed repositories (per role)."""
        if not self.credentials.has_secret(MANAGED_REPO_META_KEY):
            return {}
        try:
            value = json.loads(self.credentials.load_secret(MANAGED_REPO_META_KEY))
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _saved_managed_repo_entry(
        self, role: str, owner: str, *, private: bool, full_name: str = ""
    ) -> dict[str, Any]:
        """Owner-gated creation-time metadata for one managed repository role.

        The read-side twin of the binding ownership gate: entries naming
        another account, another visibility, another repository name, or a
        malformed id are ignored.  An empty dict means no trusted evidence.
        """
        stored = self._managed_repo_meta()
        entry = stored.get(role)
        if not isinstance(entry, dict):
            return {}
        if int(entry.get("id") or 0) <= 0 or not entry.get("managed"):
            return {}
        if str(entry.get("owner") or "").casefold() != str(owner).casefold():
            return {}
        if bool(entry.get("private")) != bool(private):
            return {}
        if full_name and not _same_repository(entry.get("full_name"), full_name):
            return {}
        return dict(entry)

    def _created_but_unverifiable_meta(
        self, exc: GitHubAppError, owner: str, full_name: str, *, role: str, private: bool
    ) -> dict[str, Any] | None:
        """「已建待安装」降级证据（D2/D3）。

        App 安装前用户令牌读不了已创建的私有仓库：repo 级 403（闭集
        repos_detail，绝非身份判定）+ 属主匹配的创建时元数据 = 信任创建时
        证据。其余错误、或没有创建时证据时返回 None（调用方保持 fail-closed）。
        """
        if exc.code != "permission_denied" or exc.endpoint_class != "repos_detail":
            return None
        entry = self._saved_managed_repo_entry(role, owner, private=private, full_name=full_name)
        if not entry:
            return None
        return {
            "id": int(entry["id"]),
            "full_name": str(entry.get("full_name") or full_name),
            "owner": str(entry.get("owner") or ""),
            "private": bool(private),
            "managed": True,
        }

    def _creation_meta_from_payload(
        self, payload: dict[str, Any], owner: str, repo: str, *, private: bool
    ) -> dict[str, Any] | None:
        """Creation response as creation-time evidence, with the same closed-set
        identity checks as the readback path (id, owner, visibility, marker)."""
        if not isinstance(payload, dict):
            return None
        repository_id = int(payload.get("id") or 0)
        repository_owner = str((payload.get("owner") or {}).get("login") or "")
        if repository_id <= 0 or repository_owner.casefold() != str(owner).casefold():
            return None
        if bool(payload.get("private")) != bool(private) or str(payload.get("description") or "") != MANAGED_DESCRIPTION:
            return None
        return {
            "id": repository_id,
            "full_name": str(payload.get("full_name") or repo),
            "owner": repository_owner,
            "private": bool(private),
            "managed": True,
        }

    def _remember_managed_repository(
        self,
        role: str,
        owner: str,
        stored_secret: str,
        repo: str,
        meta: dict[str, Any],
        *,
        private: bool,
    ) -> None:
        """创建即存：仓库创建/复用成功的当下原子落盘属主绑定与创建时元数据。

        绑定与元数据经同一次 update_secrets 批量写入；元数据只含闭集字段，
        供安装前降级与预选链接组装使用。属主校验已在 ensure/readback 完成。
        """
        entry = {
            "id": int(meta.get("id") or 0),
            "full_name": repo,
            "owner": str(meta.get("owner") or owner),
            "private": bool(private),
            "managed": True,
        }
        if entry["id"] <= 0:
            return
        stored = self._managed_repo_meta()
        stored[role] = entry
        self._save_related_secrets({
            stored_secret: repo,
            MANAGED_REPO_META_KEY: json.dumps(stored, sort_keys=True, separators=(",", ":")),
        })

    def _ensure_managed_repository(
        self,
        owner: str,
        name: str,
        *,
        role: str,
        stored_secret: str,
        private: bool,
        token: str,
        create,
    ) -> tuple[str, dict[str, Any]]:
        """Create once, or reuse only an exactly matching managed repository.

        A stored binding from an earlier attempt is honored only while its
        owner string still matches the current account; any other same-name
        repository must match owner, visibility, and the CourseLens managed
        marker to be reused.  There is deliberately no suffixed fallback name:
        a conflicting same-name repository is a specific closed-set error.

        D3（重跑容忍未安装）：repo 级读取 403（App 未安装时用户令牌无权读
        私有仓库）按「已建待安装」降级——存储绑定路径用保存元数据、创建路径
        用创建响应证据；无证据时上抛并标记 ``unverified_reuse_denial``（仅
        复用路径 403：仓库已存在只是读不了，与建仓步 403 精确区分），由
        bootstrap 降级为 awaiting_installation，不再让遗留状态死锁首跑。
        """
        stored = (
            self.credentials.load_secret(stored_secret)
            if self.credentials.has_secret(stored_secret) else ""
        )
        if stored and str(stored).split("/", 1)[0].casefold() == str(owner).casefold():
            try:
                meta = self._readback_managed_repository(owner, stored, private=private, token=token)
            except GitHubAppError as exc:
                meta = self._created_but_unverifiable_meta(
                    exc, owner, stored, role=role, private=private
                )
                if meta is None:
                    if exc.code == "permission_denied" and exc.endpoint_class == "repos_detail":
                        exc.unverified_reuse_denial = True
                    raise
                return stored, meta
            if meta:
                return stored, meta
        preferred = f"{owner}/{name}"
        try:
            meta = self._readback_managed_repository(owner, preferred, private=private, token=token)
        except GitHubAppError as exc:
            meta = self._created_but_unverifiable_meta(
                exc, owner, preferred, role=role, private=private
            )
            if meta is None:
                if exc.code == "permission_denied" and exc.endpoint_class == "repos_detail":
                    exc.unverified_reuse_denial = True
                raise
            return preferred, meta
        if meta:
            return preferred, meta
        created_payload = create(name)
        try:
            created = self._readback_managed_repository(owner, preferred, private=private, token=token)
        except GitHubAppError as exc:
            created = self._creation_meta_from_payload(
                created_payload, owner, preferred, private=private
            )
            if created is None:
                raise
        if not created:
            raise GitHubAppError(
                "CourseLens 专属仓库创建后读取失败", code="managed_repository_invalid"
            )
        return preferred, created

    def _installation_setup_url(
        self, owner: str, account_id: int, worker: dict[str, Any], mailbox: dict[str, Any]
    ) -> str:
        """Official installation URL with both managed repository ids preselected.

        Any missing, foreign, or ambiguous id leaves an empty string: no link
        is emitted unless the freshly read back evidence is complete and exact.
        创建即存的仓库元数据（属主校验通过）可作为 id 的第二来源，使预选安装
        链接从建仓起即可组装——安装前的私有仓库读取 403 不再阻断链接。
        """
        slug = str(self.app_slug or "").strip()
        if not slug or re.search(r"[^A-Za-z0-9.-]", slug):
            return ""
        saved_meta = self._managed_repo_meta()

        def resolved(meta: dict[str, Any], *, role: str, private: bool) -> dict[str, Any]:
            value = dict(meta)
            if int(value.get("id") or 0) > 0:
                return value
            entry = saved_meta.get(role)
            entry = entry if isinstance(entry, dict) else {}
            if (
                int(entry.get("id") or 0) > 0
                and bool(entry.get("managed"))
                and str(entry.get("owner") or "").casefold() == str(owner).casefold()
                and bool(entry.get("private")) == bool(private)
                and (
                    not value.get("full_name")
                    or _same_repository(value.get("full_name"), entry.get("full_name") or "")
                )
            ):
                value = {
                    **value,
                    "id": int(entry["id"]),
                    "owner": str(entry.get("owner") or value.get("owner") or ""),
                    "managed": True,
                }
            return value

        def ready(meta: dict[str, Any], *, private: bool) -> bool:
            return bool(
                int(meta.get("id") or 0) > 0
                and str(meta.get("owner") or "").casefold() == str(owner).casefold()
                and bool(meta.get("private")) == bool(private)
                and bool(meta.get("managed"))
                and not meta.get("archived") and not meta.get("disabled")
            )
        if int(account_id or 0) <= 0:
            return ""
        worker_meta = resolved(worker, role="worker", private=False)
        mailbox_meta = resolved(mailbox, role="mailbox", private=True)
        if not ready(worker_meta, private=False) or not ready(mailbox_meta, private=True):
            return ""
        return (
            f"https://github.com/apps/{slug}/installations/new/permissions"
            f"?suggested_target_id={int(account_id)}"
            f"&repository_ids[]={int(worker_meta['id'])}&repository_ids[]={int(mailbox_meta['id'])}"
        )

    def _find_user_installation(self, owner: str, *, token: str = "") -> dict[str, Any] | None:
        """Return this App's installation on the student's personal account."""
        response = self._api("GET", "/user/installations", token=token, expected=(200,)).json()
        for installation in list(response.get("installations") or []):
            account = dict(installation.get("account") or {})
            if str(installation.get("app_slug") or "").lower() != self.app_slug.lower():
                continue
            if str(account.get("login") or "").lower() != str(owner or "").lower():
                continue
            if str(installation.get("target_type") or account.get("type") or "").lower() not in {"user", ""}:
                continue
            return dict(installation)
        return None

    def _scan_fresh_user_installations(
        self, owner: str, *, token: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Freshly page every installation visible to the user grant.

        Returns ``(matches, totals_consistent)`` where matches are this App's
        installations on the given personal account.  Duplicated or invalid
        installation ids raise a closed-set error immediately; a payload total
        that disagrees with the paged raw count is reported as inconsistent so
        callers can fail closed instead of guessing.
        """
        matches: list[dict[str, Any]] = []
        installation_ids: set[int] = set()
        installation_raw_count = 0
        installation_total_count: int | None = None
        for page in range(1, 101):
            installation_payload = self._api(
                "GET", "/user/installations", token=token, expected=(200,),
                params={"per_page": 100, "page": page},
            ).json()
            installations = list(installation_payload.get("installations") or [])
            if installation_total_count is None:
                value = installation_payload.get("total_count")
                if type(value) is not int or value < 0:
                    raise GitHubAppError(
                        "GitHub App installation scope is unavailable",
                        code="installation_scope_unavailable",
                    )
                installation_total_count = value
            for item in installations:
                installation = dict(item or {})
                installation_id = int(installation.get("id") or 0)
                if installation_id <= 0 or installation_id in installation_ids:
                    raise GitHubAppError(
                        "GitHub App installation scope is not exact",
                        code="installation_scope_not_exact",
                    )
                installation_ids.add(installation_id)
                installation_raw_count += 1
                account = dict(installation.get("account") or {})
                if str(installation.get("app_slug") or "").casefold() != self.app_slug.casefold():
                    continue
                if str(account.get("login") or "").casefold() != str(owner or "").casefold():
                    continue
                if str(installation.get("target_type") or account.get("type") or "").casefold() not in {"user", ""}:
                    continue
                if installation_id > 0:
                    matches.append(installation)
            if len(installations) < 100:
                break
        else:
            raise GitHubAppError(
                "GitHub App installation scope is unavailable",
                code="installation_scope_unavailable",
            )
        return matches, installation_total_count == installation_raw_count

    def _demand_exact_installation_selection(
        self, owner: str, *, token: str, installation_id: int
    ) -> None:
        """Raise a closed-set error unless the installation selects exactly
        the managed Worker + Mailbox pair owned by the account."""
        discovered: set[str] = set()
        raw_count = 0
        total_count: int | None = None
        for page in range(1, 101):
            payload = self._api(
                "GET", f"/user/installations/{installation_id}/repositories",
                token=token, expected=(200,), params={"per_page": 100, "page": page},
            ).json()
            repositories = list(payload.get("repositories") or [])
            if total_count is None:
                value = payload.get("total_count")
                if type(value) is not int or value < 0:
                    raise GitHubAppError(
                        "GitHub App installation scope is unavailable",
                        code="installation_scope_unavailable",
                    )
                total_count = value
            for repository in repositories:
                value = dict(repository or {})
                if str(value.get("owner", {}).get("login") or "").casefold() != str(owner).casefold():
                    raise GitHubAppError(
                        "GitHub App installation scope is not exact",
                        code="installation_scope_not_exact",
                    )
                name = str(value.get("name") or "").casefold()
                if not name or name in discovered:
                    raise GitHubAppError(
                        "GitHub App installation scope is not exact",
                        code="installation_scope_not_exact",
                    )
                discovered.add(name)
                raw_count += 1
            if len(repositories) < 100:
                break
        else:
            raise GitHubAppError(
                "GitHub App installation scope is unavailable",
                code="installation_scope_unavailable",
            )
        if (
            total_count != raw_count
            or raw_count != len(REQUIRED_INSTALLATION_REPOSITORIES)
            or discovered != REQUIRED_INSTALLATION_REPOSITORIES
        ):
            raise GitHubAppError(
                "GitHub App installation scope is not exact",
                code="installation_scope_not_exact",
            )

    def _fresh_installation_state(
        self, owner: str, *, token: str, require_exact_repository_selection: bool = False
    ) -> tuple[dict[str, Any] | None, str]:
        """Fresh, never-persisted installation state for the given account.

        Returns ``(installation, status)`` where status is exactly one of:

        - ``bound``: exactly one matching installation with Actions write, and
          (when required) the exact Worker + Mailbox repository selection;
        - ``missing``: no installation of this App on the account;
        - ``scope_invalid``: one matching installation whose Actions
          permission is insufficient;
        - ``not_exact``: matching installation whose repository selection is
          not exactly the two managed repositories;
        - ``unavailable``: pagination or inventory evidence is inconsistent;
          nothing can be trusted, so no installation is bound.

        For ``scope_invalid`` and ``not_exact`` the installation document is
        still returned so callers can compose a trusted settings URL; its id
        is never persisted by this method.
        """
        try:
            matches, totals_consistent = self._scan_fresh_user_installations(owner, token=token)
        except GitHubAppError:
            return None, "unavailable"
        if not totals_consistent or len(matches) > 1:
            return None, "unavailable"
        if not matches:
            return None, "missing"
        installation = matches[0]
        if str(dict(installation.get("permissions") or {}).get("actions") or "").casefold() != "write":
            return installation, "scope_invalid"
        if not require_exact_repository_selection:
            return installation, "bound"
        try:
            self._demand_exact_installation_selection(
                owner, token=token, installation_id=int(installation["id"])
            )
        except GitHubAppError as exc:
            return installation, "not_exact" if exc.code == "installation_scope_not_exact" else "unavailable"
        return installation, "bound"

    def _inspect_fresh_user_installation(
        self, owner: str, *, token: str, require_exact_repository_selection: bool
    ) -> dict[str, Any]:
        """Strict view over :meth:`_fresh_installation_state`.

        Absent, ambiguous, or insufficient installations raise closed-set
        errors instead of returning; callers use this when they cannot make
        progress without a fully exact installation.
        """
        installation, status = self._fresh_installation_state(
            owner, token=token,
            require_exact_repository_selection=require_exact_repository_selection,
        )
        if status == "bound":
            return installation
        if status == "unavailable":
            raise GitHubAppError(
                "GitHub App installation scope is unavailable",
                code="installation_scope_unavailable",
            )
        if status == "scope_invalid":
            raise GitHubAppError(
                "GitHub App Actions permission is insufficient",
                code="installation_scope_not_exact",
            )
        raise GitHubAppError(
            "GitHub App installation scope is not exact",
            code="installation_scope_not_exact",
        )

    def _save_related_secrets(self, updates: dict[str, str]) -> None:
        update_many = getattr(self.credentials, "update_secrets", None)
        if callable(update_many):
            update_many(updates)
            return
        for name, value in updates.items():
            self.credentials.save_secret(name, value)

    def _put_repo_variable(self, repo: str, name: str, value: str, token: str) -> None:
        existing = self._api(
            "GET", f"/repos/{repo}/actions/variables/{name}", token=token, expected=(200, 404)
        )
        if existing.status_code == 404:
            self._api(
                "POST", f"/repos/{repo}/actions/variables", token=token, expected=(201,),
                json={"name": name, "value": value},
            )
        else:
            self._api(
                "PATCH", f"/repos/{repo}/actions/variables/{name}", token=token, expected=(204,),
                json={"name": name, "value": value},
            )

    def put_worker_variable(self, name: str, value: str) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._put_repo_variable(repo, str(name), str(value), token)

    def delete_worker_variable(self, name: str) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "DELETE", f"/repos/{repo}/actions/variables/{name}", token=token,
            expected=(204, 404),
        )

    def list_worker_variables(self) -> dict[str, str]:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        response = self._api(
            "GET", f"/repos/{repo}/actions/variables", token=token,
            expected=(200,), params={"per_page": 100},
        ).json()
        return {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in list(response.get("variables") or [])
            if str(item.get("name") or "")
        }

    def _put_environment_secret(self, repo: str, name: str, value: str, token: str) -> None:
        key = self._api(
            "GET",
            f"/repos/{repo}/environments/{WORKER_ENVIRONMENT}/secrets/public-key",
            token=token,
        ).json()
        public = PublicKey(base64.b64decode(str(key["key"]).encode("ascii"), validate=True))
        encrypted = base64.b64encode(SealedBox(public).encrypt(value.encode("utf-8"))).decode("ascii")
        self._api(
            "PUT",
            f"/repos/{repo}/environments/{WORKER_ENVIRONMENT}/secrets/{name}",
            token=token,
            expected=(201, 204,),
            json={"encrypted_value": encrypted, "key_id": str(key["key_id"])},
        )

    def put_worker_secret(self, name: str, value: str) -> None:
        if not value:
            raise GitHubAppError("Secret value is empty", code="cloud_secret_empty")
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._put_environment_secret(repo, str(name), str(value), token)

    def delete_worker_secret(self, name: str) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "DELETE",
            f"/repos/{repo}/environments/{WORKER_ENVIRONMENT}/secrets/{name}",
            token=token, expected=(204, 404),
        )

    def list_worker_secrets(self) -> list[dict[str, Any]]:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        response = self._api(
            "GET", f"/repos/{repo}/environments/{WORKER_ENVIRONMENT}/secrets",
            token=token, expected=(200,), params={"per_page": 100},
        ).json()
        return [
            {
                "name": str(item.get("name") or ""),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            for item in list(response.get("secrets") or [])
            if str(item.get("name") or "")
        ]

    def set_workflow_enabled(self, workflow: str, enabled: bool) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        current = self._api(
            "GET", f"/repos/{repo}/actions/workflows/{workflow}",
            token=token, expected=(200,),
        ).json()
        current_state = str(current.get("state") or "")
        if (enabled and current_state == "active") or (
            not enabled and current_state == "disabled_manually"
        ):
            return
        suffix = "enable" if enabled else "disable"
        self._api(
            "PUT", f"/repos/{repo}/actions/workflows/{workflow}/{suffix}",
            token=token, expected=(204,),
        )

    def dispatch_workflow(self, workflow: str, *, inputs: dict[str, str] | None = None, ref: str = "main") -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            token=token, expected=(200, 201, 204),
            json={"ref": str(ref), "inputs": dict(inputs or {})},
        )

    def list_workflow_runs(self, workflow: str, *, limit: int = 20) -> list[dict[str, Any]]:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        response = self._api(
            "GET", f"/repos/{repo}/actions/workflows/{workflow}/runs",
            token=token, expected=(200,), params={"per_page": max(1, min(100, int(limit)))},
        ).json()
        return [
            {
                "id": int(item.get("id") or 0),
                "run_attempt": int(item.get("run_attempt") or 1),
                "status": str(item.get("status") or ""),
                "conclusion": str(item.get("conclusion") or ""),
                "event": str(item.get("event") or ""),
                "head_sha": str(item.get("head_sha") or "").strip().lower(),
                "created_at": str(item.get("created_at") or ""),
                "run_started_at": str(item.get("run_started_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "html_url": str(item.get("html_url") or ""),
            }
            for item in list(response.get("workflow_runs") or [])
            if int(item.get("id") or 0) > 0
        ]

    def cancel_workflow_run(self, run_id: int) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "POST", f"/repos/{repo}/actions/runs/{int(run_id)}/cancel",
            token=token, expected=(202, 409),
        )

    def list_worker_artifacts(self, *, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        response = self._api(
            "GET", f"/repos/{repo}/actions/artifacts", token=token, expected=(200,),
            params={"per_page": max(1, min(100, int(limit)))},
        ).json()
        output = []
        for item in list(response.get("artifacts") or []):
            name = str(item.get("name") or "")
            if prefix and not name.startswith(prefix):
                continue
            output.append({
                "id": int(item.get("id") or 0), "name": name,
                "expired": bool(item.get("expired")),
                "created_at": str(item.get("created_at") or ""),
                "expires_at": str(item.get("expires_at") or ""),
                "workflow_run_id": int((item.get("workflow_run") or {}).get("id") or 0),
            })
        return output

    def delete_worker_artifact(self, artifact_id: int) -> None:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        self._api(
            "DELETE", f"/repos/{repo}/actions/artifacts/{int(artifact_id)}",
            token=token, expected=(204, 404),
        )

    def download_worker_artifact_files(self, artifact_id: int) -> dict[str, bytes]:
        token = self.access_token(minimum_lifetime_seconds=900)
        repo = self.credentials.load_secret("github_worker_repo")
        response = self._api(
            "GET", f"/repos/{repo}/actions/artifacts/{int(artifact_id)}/zip",
            token=token, expected=(200,),
        )
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                files = {}
                total = 0
                for info in archive.infolist():
                    if info.is_dir() or info.file_size < 0 or info.file_size > 64 * 1024 * 1024:
                        continue
                    total += info.file_size
                    if total > 128 * 1024 * 1024:
                        raise GitHubAppError("Cloud artifact is too large", code="cloud_artifact_too_large")
                    files[Path(info.filename).name] = archive.read(info)
                return files
        except zipfile.BadZipFile as exc:
            raise GitHubAppError("Cloud artifact is invalid", code="cloud_artifact_invalid") from exc

    def _float_secret(self, name: str) -> float:
        try:
            return float(self.credentials.load_secret(name))
        except (KeyError, TypeError, ValueError, OverflowError):
            return 0.0

    @staticmethod
    def _endpoint_class_for_path(path: str) -> str:
        text = str(path or "")
        if text == "/user":
            return "user_identity"
        if text.startswith("/user/installations"):
            return "user_installations"
        if text.startswith("/user/repos"):
            return "user_repos"
        if text.startswith("/repos/"):
            return "repos_actions" if "/actions/" in text else "repos_detail"
        return ""

    @staticmethod
    def _readonly_cache_key(url: str) -> str:
        """规范化缓存键：GET + scheme/host 小写 + query 参数按名稳定排序。"""
        parts = urlsplit(str(url or ""))
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return f"GET {parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}?{query}"

    @staticmethod
    def _readonly_cacheable(url: str) -> bool:
        """PARK-N3 白名单：仅「完整 40 位 SHA 寻址」的只读 GET 可入缓存。

        - ``/repos/{o}/{r}/commits/{40hex}``（无 query）；
        - ``/repos/{o}/{r}/contents/{path}`` 且 query 恰一个 ``ref={40hex}``。

        ``ref=main`` 等可变 ref、多参数读、可变分支头与一切非 GET 永不入缓存。
        """
        parts = urlsplit(str(url or ""))
        if parts.scheme.lower() != "https" or parts.netloc.lower() != "api.github.com":
            return False
        if _IMMUTABLE_COMMIT_PATH.match(parts.path):
            return parts.query == ""
        if _IMMUTABLE_CONTENTS_PATH.match(parts.path):
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            return (
                len(pairs) == 1
                and pairs[0][0] == "ref"
                and _FULL_SHA.match(pairs[0][1]) is not None
            )
        return False

    def _readonly_cache_replay(self, key: str, url: str):
        """命中则重建一个等价 Response（仅 200 入过缓存）；过期惰性清除。"""
        now = time.time()
        with self._readonly_get_cache_lock:
            entry = self._readonly_get_cache.get(key)
            if entry is None:
                return None
            expires_at, content = entry
            if now >= expires_at:
                self._readonly_get_cache.pop(key, None)
                return None
        response = requests.Response()
        response.status_code = 200
        response._content = content
        response._content_consumed = True
        response.encoding = "utf-8"
        response.url = url
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    def _readonly_cache_store(self, key: str, content: bytes) -> None:
        """仅 200 写入；满帽时先清过期项、仍满则逐出最早项（失效从简）。"""
        now = time.time()
        with self._readonly_get_cache_lock:
            if len(self._readonly_get_cache) >= _READONLY_GET_CACHE_MAX_ENTRIES:
                for stale in [
                    stale_key
                    for stale_key, (expires_at, _content) in self._readonly_get_cache.items()
                    if now >= expires_at
                ]:
                    self._readonly_get_cache.pop(stale, None)
                while len(self._readonly_get_cache) >= _READONLY_GET_CACHE_MAX_ENTRIES:
                    self._readonly_get_cache.pop(next(iter(self._readonly_get_cache)))
            self._readonly_get_cache[key] = (now + READONLY_GET_CACHE_TTL_SECONDS, content)

    def _api(self, method: str, path: str, *, token: str = "", expected=(200,), **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        bearer = token or (self.credentials.load_secret("github_app_access_token") if self.credentials.has_secret("github_app_access_token") else "")
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return self._request_external(
            method, f"{API_ROOT}{path}", headers=headers, expected=expected,
            endpoint_class=self._endpoint_class_for_path(path), **kwargs
        )

    def _request_external(self, method: str, url: str, *, expected=(200,), **kwargs):
        endpoint_class = str(kwargs.pop("endpoint_class", "") or "")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if not endpoint_class and url in {DEVICE_CODE_URL, TOKEN_URL}:
            endpoint_class = "token_endpoint"
        # PARK-N3 缓存壳：仅白名单只读 GET 可命中/可写入（模块头注释）。变更
        # 类方法在首个条件即绝缘；命中时零网络请求、限流快照保持真实观测。
        cache_key = ""
        if str(method).upper() == "GET" and self._readonly_cacheable(url):
            cache_key = self._readonly_cache_key(url)
            cached = self._readonly_cache_replay(cache_key, url)
            if cached is not None:
                return cached
        # 测试模式出站门（src/runtime/test_mode.py）：拒绝 = egress_blocked
        # 审计行 + 既有 github_unreachable 闭集码（detail 仅含闭集码不含主机外信息）。
        try:
            ensure_egress_allowed(url, purpose="github_app")
        except EgressBlockedError as exc:
            raise GitHubAppError(
                f"GitHub 连接失败：{exc.code}", code="github_unreachable"
            ) from exc
        try:
            response = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise GitHubAppError(
                f"GitHub 连接失败：{type(exc).__name__}", code="github_unreachable"
            ) from exc
        now = time.time()
        try:
            reset_at = float(response.headers.get("X-RateLimit-Reset") or 0)
        except ValueError:
            reset_at = 0.0
        try:
            remaining = int(response.headers.get("X-RateLimit-Remaining") or -1)
        except ValueError:
            remaining = -1
        retry_after_header = response.headers.get("Retry-After")
        try:
            retry_after = float(retry_after_header or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        self._rate_limit = {
            "remaining": remaining,
            "reset_at": reset_at,
            "retry_after": retry_after,
            "observed_at": now,
        }
        if response.status_code not in set(expected):
            request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
            # A 403 that carries Retry-After is GitHub's secondary rate limit
            # (abuse detection), never an authorization verdict.
            if response.status_code == 403 and retry_after_header is not None:
                code = "rate_limited"
            else:
                code = {
                    401: "authorization_revoked",
                    403: "permission_denied" if remaining != 0 else "rate_limited",
                    404: "resource_missing",
                    409: "resource_conflict",
                    422: "request_rejected",
                    429: "rate_limited",
                }.get(response.status_code, "github_service_error" if response.status_code >= 500 else "github_request_failed")
            raise GitHubAppError(
                f"GitHub 返回 HTTP {response.status_code}（请求 {request_id}）",
                code=code,
                request_id=request_id,
                retry_after=retry_after or max(0.0, reset_at - now if remaining == 0 else 0.0),
                endpoint_class=endpoint_class,
            )
        if cache_key and response.status_code == 200:
            # 期望校验已通过且仅 200 入缓存；404 等缺失证据每次实读（保守）。
            self._readonly_cache_store(cache_key, response.content)
        return response


__all__ = ["DeviceAuthorization", "ENDPOINT_CLASSES", "GitHubAppClient", "GitHubAppError"]
