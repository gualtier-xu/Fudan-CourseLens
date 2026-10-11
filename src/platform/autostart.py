"""开机/每日任务后端骨架：Windows schtasks（现逻辑适配）vs macOS launchd。

- Windows：懒加载委托既有 ``src.runtime.scheduler_windows.register_daily_task``
  （零改动复用其 task XML 与 CREATE_NO_WINDOW 口径）。
- macOS：launchd LaunchAgent——纯函数 ``render_launchd_plist`` 可在无真机
  CI 上钉内容；``launchctl load/unload`` 子进程面可用 ``subprocess_runner``
  注入打桩。``base_dir`` 注入替代 ``~``，让 plist 落点可测。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Protocol
from xml.sax.saxutils import escape

from src.platform._core import MACOS, WINDOWS, PlatformNotSupportedError, current_platform

LAUNCHD_LABEL = "com.fudan.courselens.dailysync"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"


class AutostartBackend(Protocol):
    """每日任务注册最小面（骨架；状态查询各后端能力不同）。"""

    name: str

    def register(self, *, command: str, at: str = "07:30", arguments: str = "",
                 working_directory: str = "") -> dict:
        ...

    def unregister(self) -> dict:
        ...


class WindowsSchtasksAutostartBackend:
    """委托既有 scheduler_windows；不复制任何 schtasks 细节。"""

    name = "windows-schtasks"

    def _scheduler(self):
        if current_platform() != WINDOWS:
            raise PlatformNotSupportedError(
                "windows-schtasks autostart backend requires Windows"
            )
        from src.runtime import scheduler_windows

        return scheduler_windows

    def register(self, *, command: str, at: str = "07:30", arguments: str = "",
                 working_directory: str = "") -> dict:
        scheduler = self._scheduler()
        return dict(scheduler.register_daily_task(
            command=command, at=at, enabled=True,
            arguments=arguments, working_directory=working_directory,
        ))

    def unregister(self) -> dict:
        scheduler = self._scheduler()
        result = dict(scheduler.register_daily_task(
            command="", enabled=False,
        ))
        result["registered"] = False
        return result


class MacOSLaunchdAutostartBackend:
    """launchd LaunchAgent 骨架：plist 生成纯函数 + launchctl 子进程面。"""

    name = "macos-launchd"

    def __init__(
        self,
        *,
        base_dir: str | Path | None = None,
        subprocess_runner: Callable[[list[str]], object] | None = None,
    ):
        self._base_dir = Path(base_dir) if base_dir else None
        self._run = subprocess_runner or self._subprocess_run

    @staticmethod
    def _subprocess_run(argv: list[str]):
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    def _require_macos(self) -> None:
        if current_platform() != MACOS:
            raise PlatformNotSupportedError(
                "macos-launchd autostart backend requires macOS"
            )

    def _agents_dir(self) -> Path:
        if self._base_dir is not None:
            return self._base_dir / "Library" / "LaunchAgents"
        return Path.home() / "Library" / "LaunchAgents"

    @property
    def plist_path(self) -> Path:
        return self._agents_dir() / LAUNCHD_PLIST_NAME

    def register(self, *, command: str, at: str = "07:30", arguments: str = "",
                 working_directory: str = "") -> dict:
        self._require_macos()
        if not str(command or "").strip():
            raise ValueError("command is required")
        plist = render_launchd_plist(
            command=command, at=at, arguments=arguments,
            working_directory=working_directory,
        )
        agents_dir = self._agents_dir()
        agents_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_text(plist, encoding="utf-8")
        completed = self._run(["launchctl", "load", str(self.plist_path)])
        if getattr(completed, "returncode", 1) != 0:
            raise RuntimeError(
                f"launchctl load failed: {getattr(completed, 'returncode', '?')}"
            )
        return {"backend": self.name, "task_name": LAUNCHD_LABEL,
                "enabled": True, "time": at, "registered": True}

    def unregister(self) -> dict:
        self._require_macos()
        if self.plist_path.exists():
            self._run(["launchctl", "unload", str(self.plist_path)])
            self.plist_path.unlink(missing_ok=True)
        return {"backend": self.name, "task_name": LAUNCHD_LABEL,
                "enabled": False, "registered": False}

    def status(self) -> dict:
        return {"backend": self.name, "task_name": LAUNCHD_LABEL,
                "plist_exists": self.plist_path.exists()}


def render_launchd_plist(*, command: str, at: str = "07:30", arguments: str = "",
                         working_directory: str = "") -> str:
    """生成 LaunchAgent plist（纯函数，无真机可钉）。

    口径对齐 scheduler_windows.task_xml：每日一次、错过补跑
    （StartCalendarInterval + RunAtLoad=false），最低权限用户会话内执行。
    """
    hour, minute = (int(value) for value in str(at).split(":", 1))
    program_args = [escape(str(command))]
    if str(arguments or "").strip():
        program_args.extend(shlex_split_for_plist(arguments))
    program_args_xml = "\n".join(f"    <string>{arg}</string>" for arg in program_args)
    working_xml = f"\n  <key>WorkingDirectory</key>\n  <string>{escape(str(working_directory))}</string>" if str(working_directory or "").strip() else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args_xml}
    </array>{working_xml}
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>{hour}</integer>
      <key>Minute</key>
      <integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/courselens-dailysync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/courselens-dailysync.err</string>
  </dict>
</plist>
"""


def shlex_split_for_plist(arguments: str) -> list[str]:
    """把参数串按 POSIX 规则拆成 plist 数组项（launchd 不经过 shell）。"""
    import shlex

    return [escape(part) for part in shlex.split(str(arguments))]


def get_autostart_backend(platform: str | None = None) -> AutostartBackend:
    """按平台返回自启后端；未知平台 fail-closed。"""
    resolved = current_platform(platform)
    if resolved == WINDOWS:
        return WindowsSchtasksAutostartBackend()
    if resolved == MACOS:
        return MacOSLaunchdAutostartBackend()
    raise PlatformNotSupportedError(
        f"no autostart backend for platform: {resolved}"
    )


__all__ = [
    "AutostartBackend",
    "LAUNCHD_LABEL",
    "LAUNCHD_PLIST_NAME",
    "MacOSLaunchdAutostartBackend",
    "WindowsSchtasksAutostartBackend",
    "get_autostart_backend",
    "render_launchd_plist",
    "shlex_split_for_plist",
]
