from pathlib import Path
import unittest

from tests.frontend_family import family_text


ROOT = Path(__file__).resolve().parents[1]


class RemoteComputeFrontendContractTests(unittest.TestCase):
    def test_remote_controls_use_v3_operations_and_signed_worker_actions(self):
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        settings = family_text("settings")
        for element_id in (
            "remote-evidence", "remote-device-authorization", "remote-primary-action",
        ):
            self.assertIn(f'id="{element_id}"', html)
        # UX-REMOTE-SINGLE-ACTION-1：静态四按钮排退场，状态驱动主按钮是唯一连接动作入口
        for removed_id in (
            "remote-authorize-github", "remote-bootstrap-worker",
            "remote-repair-worker", "remote-test-channel",
        ):
            self.assertNotIn(f'id="{removed_id}"', html)
            self.assertNotIn(f'$("{removed_id}")', settings)
        self.assertIn('postV3("remote-connection/actions"', settings)
        for action in (
            "diagnose", "start-authorization", "poll-authorization", "bootstrap",
            "repair-worker", "test-channel",
        ):
            self.assertIn(f'"{action}"', settings)
        # 普通入口的调用形态经主按钮路径：recommended.action 按状态发起
        self.assertIn('primaryButton.addEventListener("click", () => remoteAction(', settings)
        self.assertIn(
            'authorization_missing: { action: "start-authorization", label: "授权并创建专属仓库" }',
            settings,
        )
        self.assertNotIn('remoteAction("start-authorization")', settings)
        self.assertNotIn('remoteAction("start-authorization", { force: true })', settings)
        self.assertIn(
            'authorization_revoked: { action: "start-authorization", '
            'label: "重新授权并创建专属仓库", force: true }',
            settings,
        )
        self.assertIn("recommended.force === true ? { force: true } : {}", settings)
        self.assertNotIn("/api/remote-compute", html + settings)
        self.assertNotIn("downloadCourseBtn", html + settings)

    def test_saved_account_rotation_is_fail_closed(self):
        """rotation 与脏记录（F13/N5FE-P3）双路 fail-closed：一键使用都禁用。"""
        settings = family_text("settings")
        self.assertIn("use.disabled = dirtyRecord || Boolean(account.requires_rotation)", settings)
        self.assertIn('!studentId || studentId === "undefined" || studentId === "null"', settings)
        self.assertIn('authenticate({ action: "use-saved"', settings)
        self.assertNotIn("localStorage", settings)

    def test_launcher_uses_only_the_client_runtime(self):
        launcher = (ROOT / "start_fudan_courselens.ps1").read_text(encoding="utf-8")
        self.assertIn('.venv-client-py310\\Scripts\\python.exe', launcher)
        self.assertIn("Get-RemoteComputeSummary", launcher)
        self.assertNotIn("requirements-main-py310", launcher)
        self.assertNotIn("requirements-learning-py310", launcher)
        self.assertNotIn("COURSELENS_ONLINE_ONLY", launcher)
        self.assertNotIn("$env:HTTP_PROXY =", launcher)
        self.assertNotIn("$env:HTTPS_PROXY =", launcher)


if __name__ == "__main__":
    unittest.main()
