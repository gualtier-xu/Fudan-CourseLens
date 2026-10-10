from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.application import (
    ONBOARDING_GUIDE_SCHEMA,
    ONBOARDING_GUIDE_STATE_KEY,
    ONBOARDING_GUIDE_VERSION,
    CourseLensApplication,
    OnboardingGuideVersionError,
)
from src.runtime.http_api import make_handler
from src.runtime.network import DEFAULT_PROXY, NetworkSettings


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "runtime" / "cache"


def build_application(testcase: unittest.TestCase) -> CourseLensApplication:
    """与 tests/test_auth_timeouts.py 相同的离线真实 Application 构造方式。"""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    directory = tempfile.TemporaryDirectory(dir=SCRATCH)
    testcase.addCleanup(directory.cleanup)
    service = CourseLensApplication(Path(directory.name))
    testcase.addCleanup(service.close)
    return service


def services_for(service: CourseLensApplication) -> SimpleNamespace:
    """与 src/app.py 容器同构的最小命名空间；引导操作绑定真实 Application 方法。"""
    return SimpleNamespace(
        lifecycle=None,
        auth_catalog=SimpleNamespace(
            credentials=service.credentials,
            catalog=service.catalog_repository,
            task_repository=service.task_store,
            app_shell_snapshot=service.app_shell_snapshot,
            authentication_snapshot=service.authentication_snapshot,
            authorized_catalog_snapshot=service.authorized_catalog_snapshot,
            onboarding_snapshot=service.onboarding_snapshot,
            onboarding_guide_snapshot=service.onboarding_guide_snapshot,
            onboarding_guide_action=service.onboarding_guide_action,
            set_credentials=service.set_credentials,
            use_saved_credentials=service.use_saved_credentials,
            delete_saved_credentials=service.delete_saved_credentials,
            logout_fudan=service.logout_fudan,
            refresh_authorized_catalog_async=service.refresh_authorized_catalog_async,
            identity_scope=service.identity_scope,
        ),
        tasks=SimpleNamespace(repository=service.task_store),
    )


def seed_record(service: CourseLensApplication, record) -> None:
    service.task_store.set_app_state(ONBOARDING_GUIDE_STATE_KEY, record)


class OnboardingGuideApplicationTests(unittest.TestCase):
    """引导记录语义：缺省/旧用户/损坏/版本升级/幂等动作/恢复出厂。"""

    def setUp(self):
        self.service = build_application(self)

    def test_empty_store_is_new_default_and_read_never_writes(self):
        snapshot = self.service.onboarding_guide_snapshot()
        self.assertEqual(snapshot, {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": "new",
            "auto_opened": False,
            "persistence": "ready",
            "source": "default",
            "updated_at": 0.0,
        })
        # 恢复出厂语义：空 app state 重新得到 new + auto_opened=false（本用例即空 store）
        sentinel = "absent-sentinel"
        self.assertIs(
            self.service.task_store.get_app_state(ONBOARDING_GUIDE_STATE_KEY, sentinel),
            sentinel,
            "读取路径绝不写 app state",
        )

    def test_legacy_existing_user_evidence_normalizes_dismissed_without_writing(self):
        self.service.set_credentials("21300180001", "synthetic-password", remember=False)
        self.service._store_authorized_catalog([{"course_id": "course-1"}])
        snapshot = self.service.onboarding_guide_snapshot()
        self.assertEqual(snapshot["disposition"], "dismissed")
        self.assertTrue(snapshot["auto_opened"])
        self.assertEqual(snapshot["source"], "legacy_existing_user")
        self.assertEqual(snapshot["persistence"], "ready")
        sentinel = "absent-sentinel"
        self.assertIs(
            self.service.task_store.get_app_state(ONBOARDING_GUIDE_STATE_KEY, sentinel),
            sentinel,
            "旧用户规范化是视图行为，不得静默落盘",
        )

    def test_saved_account_evidence_also_counts_as_legacy(self):
        self.service.set_credentials("21300180001", "synthetic-password", remember=True)
        snapshot = self.service.onboarding_guide_snapshot()
        self.assertEqual(snapshot["disposition"], "dismissed")
        self.assertEqual(snapshot["source"], "legacy_existing_user")

    def test_corrupt_record_reports_invalid_and_never_completed(self):
        seed_record(self.service, {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": "unexpected-disposition",
            "auto_opened": "yes",
        })
        snapshot = self.service.onboarding_guide_snapshot()
        self.assertEqual(snapshot["persistence"], "invalid")
        self.assertEqual(snapshot["disposition"], "new", "损坏 disposition 不得被猜成 completed")
        self.assertIs(snapshot["auto_opened"], False)
        # 非字典记录同样 invalid；部分字段损坏时有效字段按原样保留
        seed_record(self.service, "garbage")
        self.assertEqual(self.service.onboarding_guide_snapshot()["persistence"], "invalid")
        seed_record(self.service, {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": "dismissed",
            "auto_opened": True,
            "updated_at": "not-a-number",
        })
        partial = self.service.onboarding_guide_snapshot()
        self.assertEqual(partial["persistence"], "invalid")
        self.assertEqual(partial["disposition"], "dismissed")

    def test_wrong_version_normalizes_to_fresh_new_view(self):
        seed_record(self.service, {
            "schema": ONBOARDING_GUIDE_SCHEMA,
            "version": "student-onboarding.v0",
            "disposition": "completed",
            "auto_opened": True,
            "updated_at": 123.0,
        })
        snapshot = self.service.onboarding_guide_snapshot()
        self.assertEqual(snapshot["version"], ONBOARDING_GUIDE_VERSION)
        self.assertEqual(snapshot["disposition"], "new", "旧版本不得冒充新版本完成")
        self.assertIs(snapshot["auto_opened"], False)
        self.assertEqual(snapshot["source"], "default")
        self.assertEqual(snapshot["persistence"], "ready")
        self.assertEqual(snapshot["updated_at"], 0.0)

    def test_actions_are_idempotent_and_respect_disposition(self):
        def semantics(snapshot):
            return {key: snapshot[key] for key in snapshot if key != "updated_at"}

        first = self.service.onboarding_guide_action("mark-opened", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(first["disposition"], "new")
        self.assertIs(first["auto_opened"], True)
        self.assertEqual(first["source"], "stored")
        self.assertEqual(
            semantics(self.service.onboarding_guide_action("mark-opened", ONBOARDING_GUIDE_VERSION)),
            semantics(first),
            "mark-opened 幂等（updated_at 按规范随写入刷新）",
        )
        dismissed = self.service.onboarding_guide_action("dismiss", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(dismissed["disposition"], "dismissed")
        self.assertIs(dismissed["auto_opened"], True)
        self.assertEqual(
            self.service.onboarding_guide_action("dismiss", ONBOARDING_GUIDE_VERSION)["disposition"],
            "dismissed",
            "dismiss 幂等",
        )
        reopened = self.service.onboarding_guide_action("mark-opened", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(reopened["disposition"], "dismissed", "mark-opened 不得复活 dismissed")
        completed = self.service.onboarding_guide_action("complete", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(completed["disposition"], "completed")
        self.assertEqual(
            self.service.onboarding_guide_action("complete", ONBOARDING_GUIDE_VERSION)["disposition"],
            "completed",
            "complete 幂等",
        )

    def test_invalid_action_and_version_mismatch_are_distinct_signals(self):
        with self.assertRaises(ValueError):
            self.service.onboarding_guide_action("reopen", ONBOARDING_GUIDE_VERSION)
        with self.assertRaises(OnboardingGuideVersionError):
            self.service.onboarding_guide_action("dismiss", "student-onboarding.v0")

    def test_dismiss_repairs_corrupt_record(self):
        seed_record(self.service, "garbage")
        repaired = self.service.onboarding_guide_action("dismiss", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(repaired["disposition"], "dismissed")
        self.assertEqual(repaired["persistence"], "ready")
        self.assertEqual(repaired["source"], "stored")

    def test_factory_reset_returns_to_new(self):
        self.service.onboarding_guide_action("complete", ONBOARDING_GUIDE_VERSION)
        fresh = build_application(self)
        snapshot = fresh.onboarding_guide_snapshot()
        self.assertEqual(snapshot["disposition"], "new")
        self.assertIs(snapshot["auto_opened"], False)
        self.assertEqual(snapshot["source"], "default")

    def test_tutorials_snapshot_carries_guide(self):
        carried = self.service.tutorials_snapshot()["guide"]
        self.assertEqual(carried, self.service.onboarding_guide_snapshot())
        self.service.onboarding_guide_action("dismiss", ONBOARDING_GUIDE_VERSION)
        self.assertEqual(
            self.service.tutorials_snapshot()["guide"],
            self.service.onboarding_guide_snapshot(),
        )


class OnboardingActionsApiTests(unittest.TestCase):
    """POST /api/v3/onboarding/actions 的路由映射：200/400/409 与 envelope。"""

    def setUp(self):
        self.service = build_application(self)
        payload_services = services_for(self.service)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(payload_services, ROOT / "frontend"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        # addCleanup LIFO：注册序 join→server_close→shutdown，使实跑序为
        # shutdown→server_close→join——先停 serve_forever 再关听套接字，否则
        # server_close 与仍在 select 的线程竞态产生 WinError 10038 警告。
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, body, expected=200):
        request = Request(
            f"{self.base}/api/v3/onboarding/actions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            if exc.code != expected:
                raise
            with exc:
                return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def test_actions_return_v3_envelope_with_updated_guide(self):
        _, value = self._post({"action": "dismiss", "version": ONBOARDING_GUIDE_VERSION})
        self.assertEqual(value["schema"], "courselens.api.v3")
        self.assertEqual(value["data"]["guide"]["disposition"], "dismissed")
        self.assertIs(value["data"]["guide"]["auto_opened"], True)

    def test_invalid_action_and_schema_return_400(self):
        for body in (
            {"action": "reopen", "version": ONBOARDING_GUIDE_VERSION},
            {"action": "dismiss"},
            {"action": "dismiss", "version": 1},
            {"version": ONBOARDING_GUIDE_VERSION},
            [1, 2, 3],
        ):
            with self.subTest(body=body):
                status, value = self._post(body, expected=400)
                self.assertEqual(value["error_code"], "onboarding_action_invalid")

    def test_version_mismatch_returns_409_with_current_guide(self):
        status, value = self._post(
            {"action": "dismiss", "version": "student-onboarding.v0"}, expected=409,
        )
        self.assertEqual(value["error_code"], "onboarding_version_conflict")
        self.assertEqual(value["guide"]["version"], ONBOARDING_GUIDE_VERSION)
        self.assertEqual(value["guide"]["disposition"], "new")

    def test_get_onboarding_carries_guide(self):
        with urlopen(f"{self.base}/api/v3/onboarding") as response:
            value = json.loads(response.read())
        self.assertEqual(value["data"]["guide"]["version"], ONBOARDING_GUIDE_VERSION)
        self.assertEqual(value["data"]["consent"]["accepted"], False)


def _probe_healthy_only_7890(url, proxy, *, timeout=None):
    return {"healthy": str(proxy).endswith(":7890"), "latency_ms": 1}


class SettingsDetectProxyApiTests(unittest.TestCase):
    """PROXY-AUTODETECT-1：settings/actions detect-proxy 闭集只读 + 保存走既有管道。

    检测动作幂等只读（不触代际、不写 secret）；保存复用 update-network 的
    P0.3 语义（代际 +1、GitHubAppClient 重建、network_github_proxy 同步）。
    """

    def setUp(self):
        self.service = build_application(self)
        payload_services = services_for(self.service)
        # 与 src/app.py SettingsService 注入面同构的窄替身：检测直连注入的
        # NetworkSettings，保存绑定真实 Application.update_network_settings。
        payload_services.settings = SimpleNamespace(
            network=self.service.network,
            update_network_settings=self.service.update_network_settings,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(payload_services, ROOT / "frontend"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        # addCleanup LIFO：注册序 join→server_close→shutdown，使实跑序为
        # shutdown→server_close→join（避免 WinError 10038 竞态，见上类注释）。
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _post(self, body, expected=200):
        request = Request(
            f"{self.base}/api/v3/settings/actions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            if exc.code != expected:
                raise
            with exc:
                return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def test_detect_proxy_returns_closed_set_and_is_idempotent_read_only(self):
        before_client = self.service.github_app
        before_generation = self.service.network.route_generation()
        with patch.object(NetworkSettings, "_system_proxy_port", return_value=None), \
             patch.object(NetworkSettings, "_probe", side_effect=_probe_healthy_only_7890):
            status, first = self._post({"action": "detect-proxy"})
            _, second = self._post({"action": "detect-proxy"})
        self.assertEqual(status, 200)
        self.assertEqual(first["schema"], "courselens.api.v3")
        self.assertEqual(
            first["data"], {"status": "found", "source": "scan", "port": 7890},
        )
        self.assertEqual(first["data"], second["data"])
        # 幂等只读：代际不动、GitHubAppClient 不重建、不新增 secret。
        self.assertEqual(self.service.network.route_generation(), before_generation)
        self.assertIs(self.service.github_app, before_client)
        self.assertFalse(self.service.credentials.has_secret("network_github_proxy"))

    def test_unknown_settings_action_stays_rejected(self):
        status, value = self._post({"action": "detect-proxies"}, expected=400)
        self.assertEqual(status, 400)
        self.assertEqual(value["error_code"], "settings_action_invalid")

    def test_save_via_update_network_bumps_generation_rebuilds_client_syncs_secret(self):
        before_client = self.service.github_app
        before_generation = self.service.network.route_generation()
        # D8：保存入口只收显式 http/https 地址（缺 scheme 的脏值由路由门
        # proxy_url_invalid 拒绝，见 test_api_v3_identity_settings）——
        # 「静默补 http://」的旧语义已退役，这里回显零改写。
        _, saved = self._post({
            "action": "update-network", "mode": "manual", "proxy_url": "http://127.0.0.1:7890",
        })
        self.assertEqual(saved["schema"], "courselens.api.v3")
        self.assertEqual(saved["data"]["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(saved["data"]["route_generation"], before_generation + 1)
        self.assertEqual(self.service.network.route_generation(), before_generation + 1)
        self.assertIsNot(self.service.github_app, before_client)
        self.assertTrue(self.service.credentials.has_secret("network_github_proxy"))
        # direct 模式保存：代际继续推进，GitHub 代理 secret 收敛删除。
        _, direct = self._post({"action": "update-network", "mode": "direct"})
        self.assertEqual(direct["data"]["proxy_url"], DEFAULT_PROXY)
        self.assertFalse(self.service.credentials.has_secret("network_github_proxy"))


if __name__ == "__main__":
    unittest.main()
