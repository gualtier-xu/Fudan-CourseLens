"""Path helpers for Fudan CourseLens."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _runtime_data_dir() -> Path:
    """Return the writable client data directory without touching old media."""
    configured = os.environ.get("COURSELENS_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # Runtime state is intentionally separate from the source tree and from
    # the retired local-media directory. Tests and integrations can still
    # inject an explicit temporary directory through COURSELENS_DATA_DIR.
    #
    # MAC-NIGHT-1 接线（平台抽象层 README §2）：macOS 走平台层默认根
    # （~/Library/Application Support/CourseLens），与打包入口的显式
    # COURSELENS_DATA_DIR 同源同值。Windows 开发树默认保持项目内
    # runtime/data 逐位不变（生产数据根由托管启动器经环境变量给定）。
    if sys.platform == "darwin":
        from src.platform.paths import data_dir as _platform_data_dir

        return _platform_data_dir()
    return PROJECT_ROOT / "runtime" / "data"


DEFAULT_DATA_DIR = _runtime_data_dir()

_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACE_RE = re.compile(r"\s+")


def ensure_inside_project(path: str | Path) -> Path:
    """Resolve a managed path inside the project or configured data directory."""
    resolved = Path(path).expanduser().resolve()
    root = PROJECT_ROOT.resolve()
    data_root = DEFAULT_DATA_DIR.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        try:
            resolved.relative_to(data_root)
        except ValueError:
            raise ValueError(
                f"Managed path must stay inside the project or data directory: {root} / {data_root}"
            ) from exc
    return resolved


def safe_segment(value: object, fallback: str = "untitled") -> str:
    """Return a filesystem-safe path segment."""
    text = str(value or "").strip()
    text = _BAD_CHARS_RE.sub("_", text)
    text = _SPACE_RE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    return text[:120]


def lecture_filename(lecture: dict) -> str:
    """Build a stable lecture filename from date/title/sub_id."""
    sub_id = safe_segment(lecture.get("sub_id"), "sub")
    date = safe_segment(lecture.get("date"), "unknown-date")
    title = safe_segment(lecture.get("sub_title"), sub_id)
    return f"{date}_{title}_{sub_id}.mp4"


def course_dir(output_dir: str | Path, course_title: str, course_id: str) -> Path:
    """Return the version-2 directory for a course under *output_dir*."""
    base = ensure_inside_project(output_dir)
    name = f"{safe_segment(course_id)}_{safe_segment(course_title, 'course')}"
    path = base / "courses" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def lecture_dir(
    output_dir: str | Path,
    course_title: str,
    course_id: str,
    lecture: dict,
) -> Path:
    """Return a stable per-lecture directory in the version-2 layout."""
    sub_id = safe_segment(lecture.get("sub_id"), "sub")
    date = safe_segment(lecture.get("date"), "unknown-date")
    title = safe_segment(lecture.get("sub_title"), sub_id)
    path = course_dir(output_dir, course_title, course_id) / "lectures" / f"{sub_id}_{date}_{title}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def subtitle_dir_for_video(video_path: str | Path) -> Path:
    """Return the managed subtitle directory, with a legacy-path fallback."""
    video = ensure_inside_project(video_path)
    if video.parent.name == "media" and video.parent.parent.name:
        return video.parent.parent / "subtitles"
    return video.parent / f"{video.stem}_subtitles"


def merged_dir_for_video(video_path: str | Path) -> Path:
    """Return the managed merged-video directory, with a legacy fallback."""
    video = ensure_inside_project(video_path)
    if video.parent.name == "media" and video.parent.parent.name:
        return video.parent.parent / "merged"
    return video.parent


def relative_to_project(path: str | Path) -> str:
    """Return a readable project-relative path when possible."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(resolved)


def file_size(path: str | Path) -> int:
    """Return file size, or zero when the file does not exist."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
