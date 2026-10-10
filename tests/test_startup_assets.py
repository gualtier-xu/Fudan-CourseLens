import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StartupAssetsTest(unittest.TestCase):
    def test_start_script_uses_only_verified_client_runtime(self):
        script = (ROOT / "start_fudan_courselens.ps1").read_text(encoding="utf-8")
        for token in (
            "Test-PortAvailable", "PYTHONDONTWRITEBYTECODE", "Find-PythonExecutable",
            "Test-PythonDependencies", ".venv-client-py310", "Find-RunningService",
            "server-instance.json", "/api/health", "System.Threading.Mutex",
            "Ensure-LocalRuntime", "RepairRuntime", "CheckRuntime",
        ):
            self.assertIn(token, script)
        self.assertNotIn("Get-Command python -All", script)
        self.assertNotIn("COURSELENS_ONLINE_ONLY", script)
        self.assertNotIn("requirements-main", script)
        self.assertNotIn("requirements-learning", script)
        self.assertNotIn("ffmpeg", script.casefold())
        self.assertNotIn("$env:HTTP_PROXY =", script)
        self.assertNotIn("$env:HTTPS_PROXY =", script)
        self.assertIn("curl_cffi, pypdf", script)
        self.assertIn("$instancePort -ge $StartPort", script)
        self.assertIn("& $PythonExe", script)

    def test_double_click_launchers_are_utf8_and_delegate_to_powershell(self):
        for filename, target in (
            ("OpenFudanCourseLens.cmd", "start_fudan_courselens.ps1"),
            ("SetupFudanCourseLensRuntime.cmd", "setup_fudan_courselens_runtime.ps1"),
        ):
            content = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(target, content)
            self.assertIn("-ExecutionPolicy Bypass", content)
            self.assertIn("chcp 65001", content.casefold())
        self.assertFalse((ROOT / "StartFudanCourseLens.cmd").exists())

    def test_runtime_manifest_has_no_local_compute_assets(self):
        assets = json.loads((ROOT / "runtime-assets.json").read_text(encoding="utf-8"))
        self.assertEqual(assets["client_mode"], "online-only")
        self.assertEqual(assets["catalog"]["repository"], "runtime/data/state.db")
        self.assertEqual(assets["client_environment"]["install_dir"], ".venv-client-py310")
        self.assertEqual(assets["client_environment"]["lock_file"], "requirements-client-py310.lock.txt")
        self.assertEqual(assets["python"]["distribution"], "nuget")
        self.assertTrue(assets["python"]["url"].startswith("https://api.nuget.org/"))
        self.assertTrue(assets["worker_mirror"]["active"]["tree"])
        for retired in (
            "main_environment", "learning_environment", "ffmpeg", "models",
            "worker_template_legacy", "retired_local_compute",
        ):
            self.assertNotIn(retired, assets)

    def test_runtime_setup_is_fail_closed_and_client_only(self):
        setup = (ROOT / "scripts" / "setup_fudan_courselens_runtime.ps1").read_text(encoding="utf-8")
        for token in (
            "Assert-ManagedPath", "Get-VerifiedDownload", "Test-PythonBase",
            "Install-PythonBase", "Test-ClientEnvironment", "Install-ClientEnvironment",
            "$Manifest.client_environment.lock_file",
        ):
            self.assertIn(token, setup)
        for removed in ("requirements-main", "requirements-learning", "ffmpeg", "SenseVoice"):
            self.assertNotIn(removed.casefold(), setup.casefold())
        self.assertIn("curl_cffi, pypdf", setup)
        self.assertIn("ZipFile]::ExtractToDirectory", setup)
        self.assertIn("Python NuGet archive contains an unsafe link", setup)
        self.assertNotIn("InstallAllUsers", setup)

    def test_dependency_audit_covers_current_surfaces_only(self):
        script = (ROOT / "scripts" / "audit_dependencies.ps1").read_text(encoding="utf-8")
        for token in (
            "pip-audit==2.10.1", "requirements-client-py310.lock.txt",
            "requirements-test-py310.lock.txt", 'Name = "worker-source"',
            'Name = "public-worker"',
        ):
            self.assertIn(token, script)
        self.assertNotIn("requirements-main-py310", script)
        self.assertNotIn("requirements-learning-py310", script)

    def test_startup_measurement_owns_and_cleans_its_process(self):
        script = (ROOT / "scripts" / "measure_client_startup.ps1").read_text(encoding="utf-8")
        for token in (
            "Refusing to measure an occupied port",
            "idle_memory_bytes",
            "cleanup_confirmed",
            "service_pid",
            "Stop-Process -Id $servicePid",
        ):
            self.assertIn(token, script)

    def test_managed_launcher_exports_outer_root_and_helper_is_self_contained(self):
        launcher = (ROOT / "scripts" / "start_managed_courselens.ps1").read_text(
            encoding="utf-8"
        )
        helper = (ROOT / "scripts" / "client_update_helper.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"COURSELENS_INSTALL_ROOT" = $InstallRoot', launcher)
        for token in (
            "courselens.launcher-trust.v2", "$expectedFiles", "$actualFiles",
            "Stable launcher contains an untrusted asset", "[int]$health.pid -eq $process.Id",
            "[string]$health.version", "$ExpectedVersion",
        ):
            self.assertIn(token, launcher)
        self.assertNotIn("from src", helper)
        self.assertNotIn("import src", helper)

        installer = (ROOT / "scripts" / "install_managed_client.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('$TrustRoot = Join-Path $InstallRoot "trust"', installer)
        self.assertIn('"config\\client-update-trust.json"', installer)
        self.assertIn("Remove-Item -LiteralPath $versionTrust", installer)
        self.assertIn('schema = "courselens.launcher-trust.v2"', installer)
        self.assertIn("files = $launcherFiles", installer)
        self.assertIn("files = $versionFiles", installer)

    def test_portable_manifest_includes_runtime_and_managed_documents(self):
        manifest = (ROOT / "scripts" / "portable_runtime_manifest.py").read_text(encoding="utf-8")
        for token in (
            '"src"', '"shared"', '"docs/repository-readmes"',
            '"runtime-assets.json"', '"requirements-client-py310.lock.txt"',
            '"OpenFudanCourseLens.cmd"',
        ):
            self.assertIn(token, manifest)
        self.assertNotIn("requirements-main", manifest)
        self.assertNotIn("requirements-learning", manifest)

    def test_frontend_is_modular_and_releases_backend_session(self):
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        shell = (ROOT / "frontend" / "modules" / "shell.js").read_text(encoding="utf-8")
        self.assertLessEqual(len(app.splitlines()), 300)
        self.assertIn('sendSession("open")', shell)
        self.assertIn('sendSession("heartbeat")', shell)
        self.assertIn('sendSession("close", true)', shell)
        self.assertIn("navigator.sendBeacon", shell)
        self.assertIn("pagehide", app)

    def test_private_ci_pins_actions_and_runs_security_gates(self):
        workflow = (ROOT / ".github" / "workflows" / "private-ci.yml").read_text(encoding="utf-8")
        actions = re.findall(r"uses:\s*[^@\s]+@([^\s]+)", workflow)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in actions), actions)
        for token in (
            "pip-audit==2.10.1", "cyclonedx-json", "dependency-audit-and-sbom",
            "gitleaks/gitleaks-action@", "fetch-depth: 0",
        ):
            self.assertIn(token, workflow)


if __name__ == "__main__":
    unittest.main()
