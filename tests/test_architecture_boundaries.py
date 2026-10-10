from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path

from src.application import CourseLensApplication
from src.services import CourseLensServices


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_retired_local_compute_modules_are_absent(self):
        for relative in (
            "archive.py",
            "manifest.py",
            "src/subtitle",
            "src/ai",
            "src/learning",
            "src/pipeline",
            "src/runtime/player.py",
            "src/runtime/reporter.py",
            "src/runtime/scheduler.py",
            "src/runtime/subtitle_runtime.py",
            "src/runtime/toolchain.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_application_exposes_no_local_media_or_compute_commands(self):
        retired = {
            "download_lecture",
            "enqueue_lecture",
            "lecture_files",
            "browser_media_path",
            "media_info",
            "handle_file_action",
            "_run_ffmpeg_download",
            "_run_subtitle_worker",
            "_run_summary_worker",
        }
        self.assertEqual(retired.intersection(dir(CourseLensApplication)), set())

    def test_remote_task_commands_remain_instance_bound(self):
        self.assertNotIsInstance(
            inspect.getattr_static(CourseLensApplication, "enqueue_subtitle"),
            staticmethod,
        )

    def test_client_source_does_not_import_retired_packages(self):
        forbidden = {"src.subtitle", "src.ai", "src.learning", "src.pipeline"}
        findings = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {str(node.module or "")}
                else:
                    continue
                if any(any(name == value or name.startswith(value + ".") for value in forbidden) for name in names):
                    findings.append(str(path.relative_to(ROOT)))
        self.assertEqual(findings, [])

    def test_client_has_no_local_media_writer_or_compute_subprocess(self):
        icourse = (ROOT / "src" / "api" / "icourse.py").read_text(encoding="utf-8")
        self.assertNotIn("download_video", icourse)
        self.assertNotIn('open(tmp_path, "wb")', icourse)
        subprocess_imports = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, (ast.Import, ast.ImportFrom))
                and (
                    any(alias.name == "subprocess" for alias in node.names)
                    if isinstance(node, ast.Import)
                    else node.module == "subprocess"
                )
                for node in ast.walk(tree)
            ):
                subprocess_imports.append(path.relative_to(ROOT).as_posix())
        # 客户端零本地媒体/计算子进程合同不放松；白名单只收平台集成 CLI
        # （schtasks 任务计划 / macOS security 钥匙串 / launchctl LaunchAgent，
        # 均为 OS 服务管理面，非媒体/计算）。
        self.assertEqual(
            subprocess_imports,
            [
                "src/platform/autostart.py",
                "src/platform/credentials.py",
                "src/runtime/scheduler_windows.py",
            ],
        )

    def test_application_does_not_mutate_python_import_paths(self):
        source = (ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertNotIn("sys.path", source)
        self.assertNotIn("_ensure_icourse_source", source)

    def test_http_adapter_depends_only_on_domain_services(self):
        fields = set(CourseLensServices.__dataclass_fields__)
        self.assertNotIn("application", fields)
        self.assertEqual(
            fields,
            {
                "lifecycle", "auth_catalog", "media_session", "learning", "tasks",
                "live_room", "remote_compute", "automation", "timetable",
                "settings", "client_update",
                # U⑩/G8/AS3：AI 用量读数与写入（settings GET/保存链的一等注入面；
                # answer_budget 两缝已整链退役）
                "max_deepseek_tokens_limit", "ai_usage_month",
                "set_max_deepseek_tokens",
                # AS6：本机累计读数（settings GET）+ 余额读数（设置页显式拉取），
                # 同走 U⑩ 顶层读数契约。
                "task_usage_month", "deepseek_balance_snapshot",
            },
        )
        source = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        self.assertNotIn("from src.application", source)
        self.assertNotIn("service._", source)
        self.assertNotIn("hasattr(service", source)
        app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("make_handler(services,", app)
        self.assertNotIn("services.application", app)

    def test_application_has_no_mode_switch_or_manifest_sidecar(self):
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                self.assertFalse(hasattr(service, "online_only"))
                self.assertFalse((Path(directory) / "manifest.json").exists())
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
