"""窗态持久化（夜10-A APP-SHAPE-2 T2/T1/T8）：几何、托盘偏好、缓存清理标记。

数据根是既有客户端数据域；本模块只新增一个 JSON 状态文件与一个清理标记文件，
不新增信任面、不触碰任何学习数据。全部纯 pathlib/json 逻辑（无 Win32），便于
行为级钉测；调用方（app.py / window_shell.py）负责失败时的诚实降级——持久化
绝不阻塞启动、绝不因状态文件损坏拒绝开窗。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

WINDOW_STATE_SCHEMA = "courselens.window-state.v1"

DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 800

_GEOMETRY_KEYS = ("x", "y", "width", "height")


def window_state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "window-state.json"


def webview_cache_clear_marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "webview-cache-clear.flag"


def webview_profile_dir(data_dir: str | Path) -> Path:
    """WebView2 用户数据目录（P68 的既有约定位置，此处只做单一事实引用）。"""
    return Path(data_dir) / "webview"


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    if number != value:
        return None
    # 位置/尺寸都处在 Win32 短整型量程内；出格值视为损坏。
    if not -(1 << 30) <= number <= (1 << 30):
        return None
    return number


def sanitize_geometry(raw: Any) -> dict[str, int] | None:
    """把任意持久化值整成安全几何；形状不对就 None（调用方回退默认）。"""
    if not isinstance(raw, dict):
        return None
    values: dict[str, int] = {}
    for key in _GEOMETRY_KEYS:
        number = _coerce_int(raw.get(key))
        if number is None:
            return None
        values[key] = number
    width, height = values["width"], values["height"]
    if width <= 0 or height <= 0 or width > 32_000 or height > 32_000:
        return None
    maximized = raw.get("maximized")
    values["maximized"] = bool(maximized) if isinstance(maximized, bool) else False
    return values


def clamp_geometry(
    geometry: dict[str, int], virtual: tuple[int, int, int, int]
) -> dict[str, int]:
    """多显示器边界钳制：窗口与虚拟桌面保持可见交集，否则回虚拟桌面原点。

    ``virtual`` 为 (vx, vy, vw, vh)（GetSystemMetrics SM_XVIRTUALSCREEN 等，
    已是像素）。策略：宽度/高度超过虚拟桌面的按桌面裁；矩形与虚拟桌面完全
    脱离或交集不足最小可见带（80px）时回退到虚拟桌面左上角（Windows 会再按
    工作区微调）。缩放后的窗口仍允许压在任务栏上——那属于正常多窗使用。
    """
    vx, vy, vw, vh = (int(value) for value in virtual)
    if vw <= 0 or vh <= 0:
        return dict(geometry)
    clamped = dict(geometry)
    clamped["width"] = min(clamped["width"], vw)
    clamped["height"] = min(clamped["height"], vh)
    visible_band = 80
    min_x = vx - (clamped["width"] - visible_band)
    max_x = vx + vw - visible_band
    min_y = vy - (clamped["height"] - visible_band)
    max_y = vy + vh - visible_band
    if (
        clamped["x"] < min_x
        or clamped["x"] > max_x
        or clamped["y"] < min_y
        or clamped["y"] > max_y
    ):
        clamped["x"] = vx
        clamped["y"] = vy
    return clamped


def load_window_state(
    data_dir: str | Path, *, fallback_size: tuple[int, int] = (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
) -> dict[str, Any]:
    """读取窗态文件；缺失/损坏一律回退默认值（开窗永远成功）。"""
    path = window_state_path(data_dir)
    raw: Any = None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if isinstance(payload, dict) and payload.get("schema") == WINDOW_STATE_SCHEMA:
        raw = payload
    geometry = sanitize_geometry(raw.get("geometry")) if isinstance(raw, dict) else None
    if geometry is None:
        geometry = {
            "width": int(fallback_size[0]),
            "height": int(fallback_size[1]),
            "maximized": False,
        }
        # 无历史位置时不带 x/y——交由 pywebview/系统居中，避免凭空出现在次屏。
    # TRAY-FIX-1：关闭语义改为「默认收进托盘」，状态键反转为 exit_on_close
    # （勾上=点窗口 × 直接退出程序）。旧键 minimize_to_tray 不再读取：旧值
    # true（收进托盘）恰是新默认行为，旧值 false（退出）正是本次修复要改掉
    # 的行为——两种历史值都正确映射到 exit_on_close=False，无需迁移。
    exit_on_close = bool(raw.get("exit_on_close")) if isinstance(raw, dict) else False
    tray_hint_shown = bool(raw.get("tray_hint_shown")) if isinstance(raw, dict) else False
    return {
        "geometry": geometry,
        "exit_on_close": exit_on_close,
        "tray_hint_shown": tray_hint_shown,
    }


def save_window_state(
    data_dir: str | Path,
    *,
    geometry: dict[str, int] | None = None,
    exit_on_close: bool | None = None,
    tray_hint_shown: bool | None = None,
) -> bool:
    """合并写入窗态；原子替换，任何失败返回 False（不抛出）。"""
    path = window_state_path(data_dir)
    current = load_window_state(data_dir)
    if geometry is not None:
        sanitized = sanitize_geometry(geometry)
        if sanitized is None:
            return False
        current["geometry"] = sanitized
    if exit_on_close is not None:
        current["exit_on_close"] = bool(exit_on_close)
    if tray_hint_shown is not None:
        current["tray_hint_shown"] = bool(tray_hint_shown)
    payload = {
        "schema": WINDOW_STATE_SCHEMA,
        "geometry": current["geometry"],
        "exit_on_close": bool(current["exit_on_close"]),
        "tray_hint_shown": bool(current["tray_hint_shown"]),
    }
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".window-state-", delete=False
        )
        try:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)
    except OSError:
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return False
    return True


def request_webview_cache_clear(data_dir: str | Path) -> bool:
    """落「下次启动清界面缓存」标记（运行期窗口档案被占用，只能延后生效）。"""
    path = webview_cache_clear_marker_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("courselens.webview-cache-clear.v1\n", encoding="utf-8")
        return True
    except OSError:
        return False


def consume_webview_cache_clear_request(data_dir: str | Path) -> bool:
    """启动期读取并取走清理标记（一次性：只有标记真在过才返回 True）。"""
    path = webview_cache_clear_marker_path(data_dir)
    try:
        if not path.exists():
            return False
        path.unlink()
    except OSError:
        return False
    return True


def webview_cache_bytes(data_dir: str | Path) -> int | None:
    """界面缓存体量（best-effort 求和）；目录不存在返回 0，不可读返回 None。"""
    root = webview_profile_dir(data_dir)
    if not root.exists():
        return 0
    total = 0
    try:
        for current, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:
                    continue
    except OSError:
        return None
    return total
