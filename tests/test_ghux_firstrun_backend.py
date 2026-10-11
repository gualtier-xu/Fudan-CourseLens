from __future__ import annotations

import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication
from tests.test_github_app import JsonResponse, ManagedRepositoryFake

SCRATCH = PROJECT_ROOT / "runtime" / "cache"

MIRROR_DIGEST = "d" * 64
BUNDLED_WORKER = {
    "mode": "signed-mirror",
    "repository": "gualtier-xu/Fudan-CourseLens-Worker",
    "commit": "a" * 40,
    "tree": "b" * 40,
    "manifest_sha256": MIRROR_DIGEST,
}
INSTALL_INSTALLED_PAYLOAD = {
    "total_count": 1,
    "installations": [{
        "id": 42, "app_slug": "fudan-courselens-student-2026", "target_type": "User",
        "account": {"login": "student", "type": "User"},
        "permissions": {"actions": "write"},
    }],
}
INSTALL_MISSING_PAYLOAD = {"total_count": 0, "installations": []}


class DeviceFlowStub:
    """github.com 设备流端点桩：pending 直至测试侧「用户在网页输码批准」。"""

    def __init__(self):
        self.approved = False
        self.device_code_requests = 0
        self.token_requests = 0

    def __call__(self, method, url, *, expected=(200,), **kwargs):
        body = dict(kwargs.get("data") or {})
        if url.endswith("/login/device/code"):
            self.device_code_requests += 1
            return JsonResponse({
                "device_code": "dev-code", "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900, "interval": 5,
            })
        if url.endswith("/login/oauth/access_token"):
            self.token_requests += 1
            if body.get("grant_type") == "refresh_token":
                return JsonResponse({
                    "access_token": "tok-refreshed", "expires_in": 28800,
                    "refresh_token": "ref-2",
                })
            if not self.approved:
                return JsonResponse({"error": "authorization_pending"})
            self.approved = False
            return JsonResponse({
                "access_token": "tok", "expires_in": 28800,
                "refresh_token": "ref",
            })
        raise AssertionError(f"unexpected external endpoint: {method} {url}")


class FirstRunJourney:
    """模拟前端修正后的首跑驱动：除一次「开始授权」点击与一次网页安装确认外，
    全部推进均为机器自动（poll 链 / 自动 bootstrap / 安装侦测后续跑）。"""

    def __init__(self, app: CourseLensApplication):
        self.app = app
        self.fake = ManagedRepositoryFake(installation_payload=INSTALL_MISSING_PAYLOAD)
        self.device = DeviceFlowStub()
        self.user_clicks = 0
        self.web_logins = 0
        self.poll_latencies: list[float] = []
        self.stage_first_latency: float | None = None
        self.stages_seen: list[str] = []
        self._patches = []
        self._patch_github_surface()

    def _patch_github_surface(self):
        client = self.app.github_app

        def trusted_integrity(*, read_only: bool = False):
            self.app.credentials.save_secret("github_worker_verified_tree", "b" * 40)
            self.app.credentials.save_secret("github_worker_verified_manifest", MIRROR_DIGEST)
            return {"trusted": True}

        def slow_generate(method, path, **kwargs):
            if path.endswith("/generate") or path == "/user/repos":
                time.sleep(0.6)  # 建仓串行链的可测时延
            return self.fake.api(method, path, **kwargs)

        self._patches = [
            patch("src.remote.github_app._bundled_worker_config", return_value=BUNDLED_WORKER),
            patch.object(client, "_api", side_effect=slow_generate),
            patch.object(client, "_request_external", side_effect=self.device),
            patch.object(client, "_sync_managed_mailbox_documents", return_value={}),
            patch.object(client, "_put_environment_secret",
                         side_effect=lambda _repo, name, _value, _token: self.fake.environment_secrets.add(name)),
            patch.object(client, "_put_repo_variable", return_value=None),
            patch.object(client, "check_worker_integrity", side_effect=trusted_integrity),
        ]
        for item in self._patches:
            item.start()

    def stop(self):
        for item in self._patches:
            item.stop()

    def action(self, action: str, operation_id: str, **kwargs):
        return self.app.remote_connection_action(action, operation_id=operation_id, **kwargs)["result"]

    def run(self) -> dict:
        # ---- ① 用户点击「授权并创建专属仓库」（唯一必要点击之一） ----
        self.user_clicks += 1
        started = self.action("start-authorization", "ghux-start-00000001")
        assert started["state"] == "pending", started
        assert started["user_code"] == "ABCD-EFGH"
        assert started["verification_uri"] == "https://github.com/login/device"

        # ---- ② 设备码轮询链（机器自动）：前两拍 pending，网页批准后 authorized ----
        # 授权确认 POST 必须即刻回包（MF-1 拆分）：不给 bootstrap 留内联时间
        poll_round = 0
        authorized_result = None
        while poll_round < 12:
            poll_round += 1
            began = time.perf_counter()
            self.app._github_device_last_poll = 0.0  # 测试压缩 RFC 轮询节拍
            result = self.action("poll-authorization", f"ghux-poll-{poll_round:012d}")
            elapsed = time.perf_counter() - began
            self.poll_latencies.append(elapsed)
            if result.get("state") == "pending":
                assert elapsed < 3.0, f"pending poll 过慢：{elapsed:.2f}s"
                self.device.approved = True  # 学生在 GitHub 网页输码批准（唯一一次网页登录）
                continue
            authorized_result = result
            break
        assert authorized_result is not None, "授权轮询未收口"
        assert authorized_result["state"] == "authorized"
        assert authorized_result["setup_state"] == "pending_bootstrap", authorized_result
        self.web_logins += 1  # 一次设备码批准贯穿全流程

        # ---- ③ authorized 后自动 bootstrap（前端零点击自动续跑）： ----
        # 阶段通道即时可见（MF-1 状态空窗消除：≤3s 出现闭集阶段）
        bootstrap_results: list[dict] = []

        def run_bootstrap(operation_id: str):
            bootstrap_results.append(self.action("bootstrap", operation_id))

        thread = threading.Thread(target=run_bootstrap, args=("ghux-bootstrap-00000001",), daemon=True)
        began = time.perf_counter()
        thread.start()
        deadline = began + 15.0
        while time.perf_counter() < deadline and thread.is_alive():
            snapshot = self.app.remote_connection_snapshot()
            progress = snapshot.get("action_progress") or {}
            if progress.get("action") == "bootstrap":
                self.stage_first_latency = time.perf_counter() - began
                break
            time.sleep(0.05)
        thread.join(timeout=30)
        assert not thread.is_alive(), "首次 bootstrap 未收口"
        awaiting = bootstrap_results[-1]
        assert awaiting["setup_state"] == "awaiting_installation", awaiting
        assert awaiting["installation_setup_url"].startswith(
            "https://github.com/apps/"
        ), awaiting
        assert self.stage_first_latency is not None and self.stage_first_latency <= 3.0, (
            f"bootstrap 阶段通道首拍延迟 {self.stage_first_latency}"
        )

        # ---- ④ 学生在 GitHub 安装页确认安装（唯一必要网页点击之一） ----
        self.user_clicks += 1
        self.fake.installation_payload = INSTALL_INSTALLED_PAYLOAD

        # ---- ⑤ 安装侦测后自动续跑 bootstrap（机器自动，零点击）→ complete ----
        probe = self.app.remote_connection.probe()
        installation = next(
            (item for item in probe["components"] if item["component"] == "installation"),
            {},
        )
        assert installation.get("code") == "installation_present", installation
        thread = threading.Thread(
            target=lambda: run_bootstrap("ghux-bootstrap-00000002"), daemon=True,
        )
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "收口 bootstrap 未收口"
        complete = bootstrap_results[-1]
        assert complete["setup_state"] == "complete", complete
        assert self.app.github_app.snapshot()["bootstrapped"] is True

        return {
            "user_clicks": self.user_clicks,
            "web_logins": self.web_logins,
            "poll_rounds": poll_round,
            "max_poll_latency": max(self.poll_latencies),
            "stage_first_latency": self.stage_first_latency,
        }


class GhuxFirstRunBackendTests(unittest.TestCase):
    """GH-UX-REWORK-1 后端首跑全旅程：真实 Application + 真实 GitHubAppClient
    （GitHub API/设备流双桩），三遍 fresh 全链，三硬指标逐遍实测。"""

    def test_first_run_journey_three_hard_metrics_three_runs(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        runs = []
        for index in range(3):
            with self.subTest(run=index + 1):
                directory = tempfile.TemporaryDirectory(dir=SCRATCH)
                self.addCleanup(directory.cleanup)
                app = CourseLensApplication(Path(directory.name))
                self.addCleanup(app.close)
                journey = FirstRunJourney(app)
                try:
                    metrics = journey.run()
                finally:
                    journey.stop()
                self.assertLessEqual(metrics["user_clicks"], 2, "必要用户点击 = 开始授权 + 安装页确认")
                self.assertEqual(metrics["web_logins"], 1, "一次设备码批准贯穿全流程")
                self.assertLess(metrics["max_poll_latency"], 3.0, "授权确认轮询即刻回包")
                self.assertLessEqual(metrics["stage_first_latency"], 3.0, "初始化阶段通道 ≤3s 可见")
                runs.append(metrics)
        for metrics in runs:
            print(
                "GHUX-RUN clicks={user_clicks} logins={web_logins} "
                "polls={poll_rounds} max_poll={max_poll_latency:.3f}s "
                "stage={stage_first_latency:.3f}s".format(**metrics)
            )


if __name__ == "__main__":
    unittest.main()
