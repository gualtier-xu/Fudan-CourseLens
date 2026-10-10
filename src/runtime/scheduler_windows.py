"""Windows Task Scheduler integration with a safe, disabled-by-default plan."""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape


TASK_NAME = "FudanCourseLens-DailySync"

# P67 ZERO-CONSOLE-1 U2: schtasks.exe is a console program, and the app that
# registers the task usually runs console-less (pythonw) — without this flag
# Windows allocates a fresh visible console for it and the settings toggle
# flashes a black window. Absent on non-Windows interpreters, where the flag
# is irrelevant anyway.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def task_xml(*, command: str, at: str = "07:30", arguments: str = "", working_directory: str = "") -> str:
    hour, minute = (int(value) for value in at.split(":", 1))
    command_xml = escape(str(command))
    arguments_xml = escape(str(arguments))
    working_xml = escape(str(working_directory))
    return f"""<?xml version=\"1.0\" encoding=\"UTF-16\"?>
<Task version=\"1.4\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">
  <Triggers><CalendarTrigger><StartBoundary>{_dt.date.today().isoformat()}T{hour:02d}:{minute:02d}:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT5H30M</ExecutionTimeLimit></Settings>
  <Actions Context=\"Author\"><Exec><Command>{command_xml}</Command><Arguments>{arguments_xml}</Arguments><WorkingDirectory>{working_xml}</WorkingDirectory></Exec></Actions>
</Task>"""


def register_daily_task(*, command: str, at: str = "07:30", enabled: bool = False,
                        arguments: str = "", working_directory: str = "") -> dict[str, object]:
    if enabled:
        root = Path(working_directory).resolve() if working_directory else Path.cwd().resolve()
        xml_path = root / "runtime" / "daily-sync-task.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(
            task_xml(command=command, at=at, arguments=arguments, working_directory=working_directory),
            encoding="utf-16",
        )
        subprocess.run(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                       check=True, capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW)
    else:
        subprocess.run(["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
                       check=False, capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW)
    return {"task_name": TASK_NAME, "enabled": bool(enabled), "time": at, "registered": bool(enabled)}


__all__ = ["TASK_NAME", "register_daily_task", "task_xml"]
