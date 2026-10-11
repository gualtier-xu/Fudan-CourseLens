"""原生窗口壳增强（夜10-A APP-SHAPE-2）：js 桥、关窗决策、任务栏进度、标题栏主题。

窗口仍是薄壳（P68 约定零业务逻辑入窗）：本模块只做「窗口本身」的事——
外链放行（系统默认浏览器，闭集校验 http/https）、关窗时的在飞任务确认与
最小化到托盘决策、任务栏进度映射（ITaskbarList3，正常/暂停态色）、深浅
主题的窗体标题栏同步、二次实例拉起已有窗口（FlashWindow）。全部 Win32
面走 ctypes、全部失败路径诚实降级为「不生效」，绝不阻塞窗口或服务。

前端桥约定（frontend/modules/shell.js）：``window.pywebview.api.<方法>()``
返回 Promise；浏览器形态没有 ``window.pywebview``，前端必须特性探测。
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from src.runtime.window_state import (
    request_webview_cache_clear,
    save_window_state,
    webview_cache_bytes,
)

# --- 在飞任务查询（loopback 闭集，只读 GET /api/v3/tasks） --------------------


@dataclass(frozen=True)
class ActiveTaskSnapshot:
    """任务栏进度与关窗确认共用的最小任务面（失败/未知均诚实标注）。

    SYSTRAY-IMPL-2 扩展（默认值向后兼容，既有构造点零改动）：
    - media_running：媒体长任务（字幕转写/总结）的 running 计数——终态徽章
      与终态通知的下降沿都只看它，不让秒级小任务掺和「长任务」的体感。
    - failed_ids：当前任务列表里 failed 任务的 id（有序闭集）——新失败=
      未在基线里的 id，由消费方自持基线，陈旧失败绝不误报。
    - kind_running：running 任务的 (kind, 数量) 序列，计数降序、kind 名序
      ——tooltip 按类型透出用（P2-9）。
    """

    unknown: bool = True
    active: int = 0
    running: int = 0
    completed_units: float = 0.0
    total_units: float = 0.0
    any_paused: bool = False
    media_running: int = 0
    failed_ids: tuple[str, ...] = ()
    kind_running: tuple[tuple[str, int], ...] = ()

    @property
    def fraction(self) -> float:
        if self.total_units <= 0:
            return 0.0
        return max(0.0, min(1.0, self.completed_units / self.total_units))


# 媒体长任务种类闭集（= course_data_inventory.ACTIVE_TASK_KINDS；这里不 import
# 是为了保持窗口壳零业务依赖——闭集变化时两处同步改，测试钉住一致性）。
_MEDIA_TASK_KINDS = frozenset({"subtitle", "summary"})


def query_active_tasks(base_url: str, *, timeout: float = 1.0) -> ActiveTaskSnapshot:
    """轮询本机 /api/v3/tasks（与任务页同一数据面）。任何失败 → unknown。"""
    parts = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
    try:
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    except (ValueError, OSError):
        return ActiveTaskSnapshot()
    try:
        connection.request("GET", "/api/v3/tasks")
        response = connection.getresponse()
        if response.status != 200:
            return ActiveTaskSnapshot()
        payload = json.loads(response.read())
    except (OSError, ValueError):
        return ActiveTaskSnapshot()
    finally:
        connection.close()
    if not isinstance(payload, dict):
        return ActiveTaskSnapshot()
    data = payload.get("data")
    counts = data.get("counts") if isinstance(data, dict) else None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(counts, dict) or not isinstance(tasks, list):
        return ActiveTaskSnapshot()
    active = counts.get("active")
    if not isinstance(active, int) or isinstance(active, bool):
        # 课程门未过等场景：counts.active 为 None——不是「没有任务」，是未知。
        return ActiveTaskSnapshot()
    running = 0
    media_running = 0
    completed = 0.0
    total = 0.0
    any_paused = False
    failed_ids: list[str] = []
    kind_counts: dict[str, int] = {}
    for item in tasks:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        kind = str(item.get("kind") or "")
        if state == "running":
            running += 1
            if kind in _MEDIA_TASK_KINDS:
                media_running += 1
            if kind:
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
            progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
            done = progress.get("processed_media_seconds")
            full = progress.get("media_duration_seconds")
            if (
                isinstance(done, (int, float)) and not isinstance(done, bool)
                and isinstance(full, (int, float)) and not isinstance(full, bool)
                and full > 0
            ):
                completed += float(done)
                total += float(full)
        if state in {"paused", "pausing"}:
            any_paused = True
        if state == "failed":
            task_id = str(item.get("task_id") or "")
            if task_id:
                failed_ids.append(task_id)
    return ActiveTaskSnapshot(
        unknown=False, active=active, running=running,
        completed_units=completed, total_units=total, any_paused=any_paused,
        media_running=media_running, failed_ids=tuple(sorted(failed_ids)),
        kind_running=tuple(sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


# --- 任务栏进度映射（T4：正常/暂停态色） -------------------------------------

TBPF_NOPROGRESS = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL = 0x2
TBPF_ERROR = 0x4
TBPF_PAUSED = 0x8


def taskbar_state_for(snapshot: ActiveTaskSnapshot) -> tuple[int, int, int] | None:
    """把任务面映射成 (TBPF 状态, completed, total)；None=未知不画条。

    有可算进度的 running 任务 → 正常态按量出条；只在跑却拿不到量 → 不定态；
    只剩暂停中的任务 → 暂停态色（黄色）。failed 不入条：失败是任务页的事。
    """
    if snapshot.unknown:
        return None
    if snapshot.active <= 0:
        return (TBPF_NOPROGRESS, 0, 0)
    if snapshot.running <= 0:
        return (TBPF_PAUSED, 0, 0)
    if snapshot.total_units > 0:
        return (
            TBPF_NORMAL,
            int(round(snapshot.completed_units)),
            int(round(snapshot.total_units)),
        )
    return (TBPF_INDETERMINATE, 0, 0)


class TerminalBadgeTracker:
    """任务栏终态徽章状态机（SYSTRAY-IMPL-2 P2-6）：红点=新失败，绿勾=带媒体
    任务的一轮工作全完成（一次性，每阶段至多一枚）。

    基线语义（陈旧红点防线）：首拍已存在的失败=启动前旧账，不点亮；「窗口
    正被查看」（前台近似）=学生已知悉，此刻清徽章并把旧账就地销账；红点所
    指的失败任务从任务列表消失（学生已处理）同样销账。unknown 恒不动。
    """

    def __init__(self) -> None:
        self._seen_first = False
        self._prev_active = 0
        self._phase_had_media = False
        self._baseline_failed: set[str] = set()
        self._red_ids: frozenset[str] = frozenset()
        self._badge = ""

    def next(self, snapshot: ActiveTaskSnapshot, *, window_viewed: bool = False) -> str:
        """返回徽章动作："failed" / "done" / "clear" / "none"（无变化）。"""
        if snapshot.unknown:
            return "none"
        known = frozenset(snapshot.failed_ids)
        if not self._seen_first:
            # 首拍基线：启动前就躺着的失败=旧账，不点亮红点。
            self._seen_first = True
            self._baseline_failed |= known
        if window_viewed and self._badge:
            # 学生正在看窗口=徽章使命完成；看到的失败就地销账，不复燃。
            self._badge = ""
            self._baseline_failed |= known
            self._red_ids = frozenset()
            self._prev_active = snapshot.active
            self._phase_had_media = snapshot.media_running > 0
            return "clear"
        action = "none"
        new_failures = known - self._baseline_failed
        if new_failures:
            self._baseline_failed |= new_failures
            self._red_ids = frozenset(new_failures)
            if self._badge != "failed":
                self._badge = "failed"
                action = "failed"
        elif self._badge == "failed" and self._red_ids and not (self._red_ids & known):
            # 红点所指的失败任务已被处理掉（不再出现在任务列表）——销账。
            self._badge = ""
            self._red_ids = frozenset()
            action = "clear"
        if snapshot.active > 0:
            if snapshot.media_running > 0:
                self._phase_had_media = True
        elif self._phase_had_media and self._prev_active > 0 and self._badge != "failed":
            self._phase_had_media = False
            self._badge = "done"
            action = "done"
        self._prev_active = snapshot.active
        return action


def _window_is_foreground(hwnd: int) -> bool:
    """窗口是否正被学生查看（前台近似）：非 Windows/拿不到=False。"""
    if hwnd <= 0 or os.name != "nt":
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


class MediaRunWatcher:
    """媒体长任务 running 计数的下降沿观测器（SYSTRAY-IMPL-2 P2-7）。

    媒体阶段从 >0 回到 0 的那一拍恰发一次终态事件（"completed"/"failed"，
    按「本阶段是否出现新失败」判文案）；0→0 不发、unknown 不动状态、无媒体
    阶段不评估。基线语义与徽章追踪器同源：启动前已存在的失败不算本轮新失败。
    """

    def __init__(self) -> None:
        self._seen_first = False
        self._prev_media_running: int | None = None
        self._baseline_failed: set[str] = set()

    def observe(self, snapshot: ActiveTaskSnapshot) -> str | None:
        """消费一拍快照；返回 "completed" / "failed" / None（无终态事件）。"""
        if snapshot.unknown:
            return None
        known = set(snapshot.failed_ids)
        if not self._seen_first:
            self._seen_first = True
            self._baseline_failed = set(known)
        prev, now = self._prev_media_running, snapshot.media_running
        event: str | None = None
        if prev is not None and prev > 0 and now == 0:
            failures = known - self._baseline_failed
            event = "failed" if failures else "completed"
            self._baseline_failed |= known
        self._prev_media_running = now
        return event


# 托盘 tooltip 的任务类型中文标签（P2-9）：与前端 tasks-drawer 的
# TASK_KIND_LABELS 同源闭集，字幕/总结两主类型按任务包口径用全称；未知 kind
# 诚实回退原始字符串（前端同规）。闭集变化时两处同步改。
_TOOLTIP_KIND_LABELS = {
    "subtitle": "字幕转写",
    "summary": "总结",
    "question": "随堂问答",
    "document_import": "资料导入",
    "search_answer": "检索问答",
    "quiz": "测验",
    "review_plan": "复习计划",
    "document_alignment": "资料对齐",
    "timeline_classification": "时间线整理",
    "concept_analysis": "概念分析",
    "courseware_pdf": "课件 PDF",
    "quality_judge": "质量抽检",
}


def tooltip_for(snapshot: ActiveTaskSnapshot, *, base: str = "CourseLens") -> str:
    """托盘悬停提示文案（夜10-A 剩余矩阵 #9）：空闲/未知=纯名字，在飞=带类型。

    P2-9：在飞且知道任务类型时按「字幕转写 ×2、总结 ×1 进行中」透出（计数
    降序、同数按 kind 名序，确定性形态）；拿不到类型=退回纯计数形态。上限
    127 字符（szTip 容量），超长截断。
    """
    if snapshot.unknown or snapshot.active <= 0:
        return base
    if snapshot.kind_running:
        parts = "、".join(
            f"{_TOOLTIP_KIND_LABELS.get(kind, kind)} ×{count}"
            for kind, count in snapshot.kind_running
        )
        text = f"{base}\n{parts} 进行中"
    else:
        text = f"{base}\n{snapshot.active} 个任务进行中"
    return text[:127]


def tray_status_line_for(snapshot: ActiveTaskSnapshot) -> str:
    """托盘右键菜单状态行文案（SYSTRAY-IMPL-2 P1-4）：未知=空串（不显示）。

    菜单打开瞬间查询一次任务面：在飞=「N 个任务在跑」，空闲=「暂无进行中
    任务」，查询失败/课程门未过（unknown）=空串让状态行整行消失——诚实
    未知绝不伪装成「暂无任务」。
    """
    if snapshot.unknown:
        return ""
    if snapshot.active > 0:
        return f"{snapshot.active} 个任务在跑"
    return "暂无进行中任务"


_TASKBAR_CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_TASKBAR_IID = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

# ITaskbarList3 vtbl 槽位（IUnknown 0-2 / ITaskbarList 3-7 / ITaskbarList2 8）：
# HrInit=3、Release=2 既有在用；下面三个是本文件用到的任务栏面。
_ITASKBAR3_VTBL_HRINIT = 3
_ITASKBAR3_VTBL_SET_PROGRESS_VALUE = 9
_ITASKBAR3_VTBL_SET_PROGRESS_STATE = 10
_ITASKBAR3_VTBL_SET_OVERLAY_ICON = 11

_OVERLAY_ICON_SIZE = 16


def _on_check_stroke(x: float, y: float) -> bool:
    """绿勾两段线段的带宽度量（16px 网格，像素中心 +0.5）；命中=勾上白点。"""
    for ax, ay, bx, by in ((4.0, 8.5, 6.8, 11.2), (6.8, 11.2, 11.8, 4.8)):
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length_sq))
        px, py = ax + t * dx, ay + t * dy
        if ((x - px) ** 2 + (y - py) ** 2) ** 0.5 <= 1.25:
            return True
    return False


def _overlay_icon_pixels(kind: str) -> list[list[tuple[int, int, int, int]]]:
    """终态徽章的 16×16 BGRA 像素矩阵（纯函数，可单测）。

    done=绿圆白勾、failed=红点；颜色取中饱和度（浅/深任务栏都立得住），
    圆边按到圆心距离做 1px 羽化。零资产依赖：不往 installer 加新图标文件。
    """
    size = _OVERLAY_ICON_SIZE
    center = (size - 1) / 2
    if kind == "done":
        radius, base, check = 6.5, (86, 158, 32), (255, 255, 255)
    elif kind == "failed":
        radius, base, check = 5.5, (66, 80, 232), None
    else:
        return []  # 未知种类 fail-closed：不画、不伪造回退形态
    rows: list[list[tuple[int, int, int, int]]] = []
    for y in range(size):
        row: list[tuple[int, int, int, int]] = []
        for x in range(size):
            dist = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            alpha = max(0.0, min(1.0, radius - dist + 0.5))
            if alpha <= 0.0:
                row.append((0, 0, 0, 0))
                continue
            b, g, r = base
            if check is not None and _on_check_stroke(x + 0.5, y + 0.5):
                b, g, r = check
            row.append((b, g, r, round(alpha * 255)))
        rows.append(row)
    return rows


def create_terminal_overlay_icon(kind: str) -> int:
    """GDI 现画终态徽章 HICON（P2-6）；失败/非 Windows=0=不画徽章。"""
    if os.name != "nt":
        return 0
    try:
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        # 句柄一律走默认 c_int 往返：USER/GDI 句柄在 Win64 上 32 位有效
        # （微软口径），设 restype=c_void_p 反而回传 64 位大整数，喂给下一个
        # 未设 argtypes 的调用会 OverflowError（SYSTRAY-IMPL-2 实测踩坑）。
        size = _OVERLAY_ICON_SIZE
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hdc = user32.GetDC(None)
        if not hdc:
            return 0
        try:
            bitmap = gdi32.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        finally:
            user32.ReleaseDC(None, hdc)
        if not bitmap or not bits.value:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            return 0
        buffer = (ctypes.c_ubyte * (size * size * 4))()
        index = 0
        pixels = _overlay_icon_pixels(kind)
        if not pixels:
            gdi32.DeleteObject(bitmap)
            return 0  # 未知种类：绝不伪造一枚空徽章
        for row in pixels:
            for blue, green, red, alpha in row:
                buffer[index] = blue
                buffer[index + 1] = green
                buffer[index + 2] = red
                buffer[index + 3] = alpha
                index += 4
        ctypes.memmove(bits.value, buffer, ctypes.sizeof(buffer))
        # AND 掩码全 0（不裁剪任何像素）：透明度全权交给 32bpp alpha。
        mask_bits = (ctypes.c_ubyte * (size * 2 * size))()
        mask = gdi32.CreateBitmap(size, size, 1, 1, ctypes.byref(mask_bits))
        if not mask:
            gdi32.DeleteObject(bitmap)
            return 0
        icon = user32.CreateIconIndirect(ctypes.byref(_ICONINFO(1, 0, 0, mask, bitmap)))
        gdi32.DeleteObject(mask)
        gdi32.DeleteObject(bitmap)
        return int(icon) if icon else 0
    except Exception:
        return 0


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", ctypes.c_long),
        ("xHotspot", ctypes.c_ulong),
        ("yHotspot", ctypes.c_ulong),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


class TaskbarProgress:
    """ITaskbarList3 的最小 ctypes 包装：只在 Windows 上生效，失败全静默。

    COM 方法一律两步调用（``_vtbl_method`` 取函数地址再以 this 指针调用，
    与 HrInit/Release 同构）——SYSTRAY-IMPL-2 实证此前的「原型类型直调」
    形态在 ctypes 里不可调（TypeError 被吞、恒 False），进度条从未真正
    画出来过；本类内全部方法已改为可验证的两步形态。
    """

    def __init__(self) -> None:
        self._ptr: ctypes.c_void_p | None = None
        self._hwnd = 0
        self._last_state: int | None = None
        self._overlay_icons: dict[str, int] = {}

    @staticmethod
    def _guid(spec: str) -> Any:
        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        raw = spec.strip("{}").split("-")
        data4 = bytes.fromhex(raw[3] + raw[4])
        return _GUID(int(raw[0], 16), int(raw[1], 16), int(raw[2], 16), data4)

    @staticmethod
    def _vtbl_method(ptr: Any, index: int, proto: Any) -> Any:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        address = ctypes.cast(
            vtbl + index * ctypes.sizeof(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)
        ).contents.value
        return proto(address)

    def _call(self, index: int, argtypes: tuple, *args: Any) -> int:
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, *argtypes)
        method = self._vtbl_method(self._ptr, index, proto)
        return int(method(self._ptr, *args))

    def bind(self, hwnd: int) -> bool:
        """CoCreateInstance + HrInit；非 Windows/COM 失败 → False（永不画条）。"""
        if hwnd <= 0:
            return False
        try:
            ctypes.oledll.ole32.CoInitialize(None)
            ptr = ctypes.c_void_p()
            ctypes.oledll.ole32.CoCreateInstance(
                ctypes.byref(self._guid(_TASKBAR_CLSID)), None, 1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(self._guid(_TASKBAR_IID)), ctypes.byref(ptr),
            )
            if not ptr.value:
                return False
            self._ptr = ptr
            self._hwnd = int(hwnd)
            if self._call(_ITASKBAR3_VTBL_HRINIT, (ctypes.c_void_p,)) != 0:
                self._ptr = None
                return False
            return True
        except Exception:
            self._ptr = None
            return False

    def set_state(self, flags: int) -> bool:
        if self._ptr is None:
            return False
        try:
            return self._call(
                _ITASKBAR3_VTBL_SET_PROGRESS_STATE,
                (ctypes.c_void_p, ctypes.c_ulong), self._hwnd, flags,
            ) == 0
        except Exception:
            return False

    def set_value(self, completed: int, total: int) -> bool:
        if self._ptr is None or total <= 0:
            return False
        try:
            return self._call(
                _ITASKBAR3_VTBL_SET_PROGRESS_VALUE,
                (ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong),
                self._hwnd, completed, total,
            ) == 0
        except Exception:
            return False

    def set_overlay(self, hicon: int, description: str = "") -> bool:
        """SetOverlayIcon（P2-6）：hicon=0 清除徽章；失败静默（不画=现状）。"""
        if self._ptr is None:
            return False
        try:
            return self._call(
                _ITASKBAR3_VTBL_SET_OVERLAY_ICON,
                (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p),
                self._hwnd, ctypes.c_void_p(hicon), description,
            ) == 0
        except Exception:
            return False

    def set_overlay_badge(self, badge: str) -> bool:
        """终态徽章动作面（P2-6）："done"=绿勾 / "failed"=红点 / "clear"=清除。

        图标 GDI 现画、按需缓存、release 时销毁；画不出来=不画徽章（现状）。
        """
        if badge == "clear":
            return self.set_overlay(0)
        if badge not in ("done", "failed"):
            return False
        hicon = self._overlay_icons.get(badge, 0)
        if not hicon:
            hicon = create_terminal_overlay_icon(badge)
            if hicon:
                self._overlay_icons[badge] = hicon
        if not hicon:
            return False
        return self.set_overlay(hicon, "全部完成" if badge == "done" else "有失败任务")

    def apply(self, snapshot: ActiveTaskSnapshot) -> bool:
        mapped = taskbar_state_for(snapshot)
        if mapped is None:
            return False
        flags, completed, total = mapped
        changed = flags != self._last_state
        self._last_state = flags
        if not self.set_state(flags):
            return False
        if flags in (TBPF_NORMAL,):
            self.set_value(completed, total)
        return changed

    def release(self) -> None:
        ptr, self._ptr = self._ptr, None
        self._last_state = None
        icons, self._overlay_icons = self._overlay_icons, {}
        if os.name == "nt":
            for hicon in icons.values():
                try:
                    ctypes.windll.user32.DestroyIcon(hicon)
                except Exception:
                    pass
        if ptr is None:
            return
        try:
            prototype = ctypes.WINFUNCTYPE  # type: ignore[attr-defined]
            release_fn = self._vtbl_method(ptr, 2, prototype(ctypes.HRESULT, ctypes.c_void_p))
            release_fn(ptr)
        except Exception:
            pass


class TaskbarProgressPoller:
    """窗口在世期间轮询本机任务面并映射到任务栏（线程自管，停机幂等）。

    COM 对象必须在**使用它的线程**上创建（STA 惯例）：bind 由轮询线程自己
    做，不在 shown 事件的 GUI 线程上做——跨线程直用会 RPC_E_WRONG_THREAD。
    ``on_snapshot``（夜10-A #9）每拍带最新任务面，供托盘 tooltip 等被动呈现；
    回调在轮询线程执行，实现方自行保证线程安全。
    """

    def __init__(self, base_url: str, *, interval: float = 3.0, on_snapshot: Callable[[ActiveTaskSnapshot], None] | None = None) -> None:
        self._base_url = base_url
        self._interval = interval
        self._on_snapshot = on_snapshot
        self._hwnd = 0
        self._progress = TaskbarProgress()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, hwnd: int) -> None:
        if self._thread is not None or hwnd <= 0:
            return
        self._hwnd = int(hwnd)
        self._thread = threading.Thread(
            target=self._run, name="courselens-taskbar-progress", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        if not self._progress.bind(self._hwnd):
            return
        badge_tracker = TerminalBadgeTracker()
        while not self._stop.wait(self._interval):
            try:
                snapshot = query_active_tasks(self._base_url)
                self._progress.apply(snapshot)
                # P2-6：终态徽章（红点=新失败/绿勾=媒体阶段全完成），窗口被
                # 查看即清；动作面在 TaskbarProgress，失败静默不画。
                action = badge_tracker.next(
                    snapshot, window_viewed=_window_is_foreground(self._hwnd)
                )
                if action != "none":
                    self._progress.set_overlay_badge(action)
                if self._on_snapshot is not None:
                    self._on_snapshot(snapshot)
            except Exception:
                continue

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        self._progress.release()


# --- 关窗决策（T1/T5 + TRAY-FIX-1：默认收托盘 / 显式退出 + 在飞任务一次性确认） ---


def confirm_exit_with_active_tasks(count: int, *, parent_hwnd: int = 0) -> bool:
    """原生确认框：True=现在关窗（既有工作保护链接管收尾）；False=先留着。

    Windows 上用 MessageBoxW（pywebview 自身的 confirm_close 也是这条路）；
    默认按钮=「先留着」，少一步误关。非 Windows 永远 False（宁可留着）。
    """
    if count <= 0:
        return True
    try:
        # 文案走查（N15-W1）：按钮是系统「是/否」，正文结尾必须落一个能被
        # 是/否直接回答的问句，别让学生把复合陈述在脑内再翻译一遍。
        text = (
            f"还有 {count} 个任务没跑完。关窗后应用会等任务安全收尾，然后自动"
            f"退出；先留着窗口也可以等它们跑完。\n\n现在就要关窗吗？"
        )
        flags = 0x4 | 0x20 | 0x100  # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
        result = ctypes.windll.user32.MessageBoxW(parent_hwnd or None, text, "CourseLens", flags)
        return int(result) == 6  # IDYES
    except Exception:
        return False


def confirm_tray_exit_with_active_tasks(count: int, *, parent_hwnd: int = 0) -> bool:
    """托盘「退出」的在飞任务确认（TRAY-FIX-1）：True=现在退出；False=先留着。

    收进托盘成为默认关窗语义后，托盘右键「退出」就是学生唯一显式退出入口；
    在飞任务还在跑时直接退场等于替学生做决定。与关窗确认同族（同原生的
    MessageBoxW，默认按钮=「先留着」），但文案说退出而非关窗；非 Windows
    永远 False（宁可留着）。
    """
    if count <= 0:
        return True
    try:
        text = (
            f"还有 {count} 个任务没跑完。退出后 CourseLens 会等它们安全收尾，然后关掉；"
            f"先不退出也可以等它们跑完。\n\n现在就要退出吗？"
        )
        flags = 0x4 | 0x20 | 0x100  # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
        result = ctypes.windll.user32.MessageBoxW(parent_hwnd or None, text, "CourseLens", flags)
        return int(result) == 6  # IDYES
    except Exception:
        return False


class WindowClosePolicy:
    """closing 事件决策：看门狗放行 → 退出请求放行 → 默认收托盘 → 在飞确认 → 放行。

    返回 False=取消本次关窗（pywebview 约定），None=放行。真实关闭时顺手把
    窗态落盘（T2），让更新重启/下次启动回到同一几何。

    TRAY-FIX-1 语义（托盘应用的标准关窗）：默认（exit_on_close=False）点 ×
    只是把窗口收进托盘，任务照常跑、托盘图标常驻；只有学生在设置里勾了
    「关闭窗口时退出 CourseLens」（exit_on_close=True）才走旧关窗链（含在飞
    确认）。托盘起不来（收不进去）时回落普通关窗链——绝不让学生因为一个
    图标丢失窗口入口。
    """

    def __init__(
        self,
        *,
        exit_on_close: Callable[[], bool],
        is_loaded: Callable[[], bool],
        is_exit_requested: Callable[[], bool],
        active_tasks: Callable[[], ActiveTaskSnapshot],
        confirm_exit: Callable[[int], bool] = confirm_exit_with_active_tasks,
        on_hide_to_tray: Callable[[], bool],
        on_save_geometry: Callable[[], None],
    ) -> None:
        self._exit_on_close = exit_on_close
        self._is_loaded = is_loaded
        self._is_exit_requested = is_exit_requested
        self._active_tasks = active_tasks
        self._confirm_exit = confirm_exit
        self._on_hide_to_tray = on_hide_to_tray
        self._on_save_geometry = on_save_geometry

    def on_closing(self, *args: Any) -> bool | None:
        if not self._is_loaded():
            # 加载看门狗/初始化失败的销毁路径：绝不拦截，浏览器兜底必须能走。
            return None
        if self._is_exit_requested():
            return None
        if not self._exit_on_close() and self._on_hide_to_tray():
            return False
        snapshot = self._active_tasks()
        if not snapshot.unknown and snapshot.active > 0:
            if not self._confirm_exit(snapshot.active):
                return False
        self._on_save_geometry()
        return None


# --- 外链闭集（T3） -----------------------------------------------------------

_EXTERNAL_OPEN_TIMEOUT = 1.0


def is_external_http_url(url: str, *, origin: str | None = None) -> bool:
    """只放行 http/https；给了 origin（本应用地址）时同源地址不算外链。"""
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if origin is None:
        return True
    try:
        home = urlsplit(origin)
    except ValueError:
        return True
    return (parsed.scheme, parsed.hostname, parsed.port) != (
        home.scheme, home.hostname, home.port,
    )


# --- 原生标题栏主题（T14） ----------------------------------------------------

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_ALT = 19

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def virtual_screen_bounds() -> tuple[int, int, int, int] | None:
    """全虚拟桌面边界（多显示器钳制用）；非 Windows/失败 → None。"""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
        )
    except Exception:
        return None


def apply_titlebar_theme(hwnd: int, *, dark: bool) -> bool:
    """DwmSetWindowAttribute 深浅标题栏；20 失败再试 19（旧 Win10 口径）。"""
    if hwnd <= 0:
        return False
    try:
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_ALT):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if int(result) == 0:
                return True
        return False
    except Exception:
        return False


# --- 二次实例拉起（T6） -------------------------------------------------------

_SERVER_INSTANCE_FILE = "server-instance.json"


def _flash_and_foreground(hwnd: int) -> bool:
    """还原/显示 + 前台 + 闪烁提醒（FLASHW_ALL|FLASHW_TIMERNOFG）。"""
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        elif not user32.IsWindowVisible(hwnd):
            user32.ShowWindowAsync(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)

        class FLASHW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_ulong),
            ]

        flash = FLASHW(
            ctypes.sizeof(FLASHW), ctypes.c_void_p(hwnd), 0x3 | 0xC, 4, 0
        )
        user32.FlashWindowEx(ctypes.byref(flash))
        return True
    except Exception:
        return False


def focus_running_instance_window(
    data_root: str | Path, *, title: str = "CourseLens", enumerate_windows: Callable[[], list[tuple[int, int, str, bool]]] | None = None
) -> bool:
    """已有实例在服务时把它的窗口拉回前台并闪烁（T6）。

    身份闭集：只认 ``server-instance.json`` 里记录的属主 pid（InstanceLock
    的既有证据文件）+ 恰好等于窗口标题的顶层窗——绝不按标题全桌搜。返回
    是否真的拉起了一个窗口。无头实例（每日任务）不应调用本函数：计划任务
    不许抢学生焦点。
    """
    if enumerate_windows is None:
        if sys.platform == "darwin":
            # MAC-NIGHT-1：macOS 聚焦等价腿——身份闭集不变（同一证据文件的
            # 属主 pid），激活走 NSRunningApplication；AppKit 缺席=诚实 False
            # （「已在运行」提示后安静退出，绝不抢焦点、绝不崩二次实例）。
            # darwin 先判（win32 上恒假=Windows 路径零变化），测试可只钉
            # sys.platform 而不必触碰 os.name/pathlib 的平台解析。
            return _focus_owner_process_macos(Path(data_root))
        if os.name == "nt":
            enumerate_windows = _enumerate_top_level_windows
        else:
            return False
    try:
        payload = json.loads((Path(data_root) / _SERVER_INSTANCE_FILE).read_text(encoding="utf-8"))
        owner_pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if owner_pid <= 0:
        return False
    for hwnd, pid, window_title, _visible in enumerate_windows():
        if pid == owner_pid and window_title == title:
            return _flash_and_foreground(hwnd)
    return False


def _focus_owner_process_macos(data_root: Path) -> bool:
    """读取属主 pid 证据并交平台层聚焦（MAC-NIGHT-1，macOS 专属腿）。

    证据文件解析与 Windows 腿同闭集（server-instance.json 的 pid 字段）；
    任何读取疑点=诚实 False。聚焦实现零 AppKit 导入期依赖（见
    src.platform.tray.focus_owner_process）。
    """
    try:
        payload = json.loads((data_root / _SERVER_INSTANCE_FILE).read_text(encoding="utf-8"))
        owner_pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return False
    if owner_pid <= 0:
        return False
    from src.platform.tray import focus_owner_process

    return focus_owner_process(owner_pid)


def _enumerate_top_level_windows() -> list[tuple[int, int, str, bool]]:
    """EnumWindows → (hwnd, pid, title, visible)；只读查询，绝不动别人的窗。"""
    user32 = ctypes.windll.user32
    results: list[tuple[int, int, str, bool]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def _on_window(hwnd, _lparam):
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(length + 1, 1))
        user32.GetWindowTextW(hwnd, buffer, max(length + 1, 1))
        results.append(
            (hwnd, int(pid.value), buffer.value, bool(user32.IsWindowVisible(hwnd)))
        )
        return True

    callback = WNDENUMPROC(_on_window)
    user32.EnumWindows(callback, 0)
    return results


# --- js 桥（T3/T8/T14/T1：前端特性探测入口） ----------------------------------


class NativeShellApi:
    """暴露给前端的原生窗口桥（pywebview js_api）。

    以下划线开头的方法不会暴露给 JS；公开方法全部返回可 JSON 化的 dict，
    由 pywebview 包成 Promise。失败一律 ``{"ok": False, ...}``——前端按诚实
    失败呈现，绝不假装成功。
    """

    def __init__(self, data_dir: str | Path, *, app_origin: str, initial_exit_on_close: bool = False) -> None:
        self._data_dir = Path(data_dir)
        self._origin = app_origin
        self._exit_on_close = bool(initial_exit_on_close)
        self._hwnd_getter: Callable[[], int] | None = None

    def _bind(self, hwnd_getter: Callable[[], int]) -> None:
        self._hwnd_getter = hwnd_getter

    # 以下为 js 可见面 -------------------------------------------------------

    def open_external(self, url: str) -> dict[str, Any]:
        """系统默认浏览器打开外链；闭集外（非 http/https）拒绝不解释。"""
        if not is_external_http_url(url, origin=self._origin):
            return {"ok": False, "reason": "external_link_unsupported"}
        try:
            opened = webbrowser.open(str(url))
        except Exception:
            opened = False
        return {"ok": bool(opened)}

    def set_exit_on_close(self, enabled: bool) -> dict[str, Any]:
        """TRAY-FIX-1：设置「关闭窗口时退出 CourseLens」。默认 False=点 × 收进
        托盘。托盘常驻与该开关彻底无关（shown 即挂、恒在），因此不再有偏好
        回调——这里只落盘。"""
        self._exit_on_close = bool(enabled)
        saved = save_window_state(self._data_dir, exit_on_close=self._exit_on_close)
        return {"ok": bool(saved), "exit_on_close": self._exit_on_close}

    def get_window_features(self) -> dict[str, Any]:
        return {
            "native_window": True,
            "features_version": 2,
            "exit_on_close": self._exit_on_close,
            "webview_cache_bytes": webview_cache_bytes(self._data_dir),
            "can_clear_webview_cache": True,
        }

    def get_webview_cache_bytes(self) -> dict[str, Any]:
        return {"ok": True, "bytes": webview_cache_bytes(self._data_dir)}

    def request_webview_cache_clear(self) -> dict[str, Any]:
        requested = request_webview_cache_clear(self._data_dir)
        return {"ok": bool(requested), "applies_on_next_start": bool(requested)}

    def set_titlebar_theme(self, dark: bool) -> dict[str, Any]:
        hwnd = self._hwnd()
        return {"ok": apply_titlebar_theme(hwnd, dark=bool(dark))}

    # js 可见面结束 -----------------------------------------------------------

    def _hwnd(self) -> int:
        if self._hwnd_getter is None:
            return 0
        try:
            return int(self._hwnd_getter() or 0)
        except Exception:
            return 0

    @property
    def exit_on_close(self) -> bool:
        return self._exit_on_close


# --- 窗体几何读取（T2 落盘侧；恢复侧在 window_state） -------------------------


def read_window_geometry(window: Any) -> dict[str, int] | None:
    """从 pywebview 窗口读当前几何（含最大化态与还原边界）。

    最大化时取 Form.RestoreBounds（还原态边界），否则取 Bounds；最小化按
    还原态处理。非 WinForms 形态（测试桩/其他平台）返回 None 跳过落盘。
    """
    form = getattr(window, "native", None)
    if form is None:
        return None
    try:
        state = int(form.WindowState)
        bounds = form.RestoreBounds if state == 2 else form.Bounds
        return {
            "x": int(bounds.X),
            "y": int(bounds.Y),
            "width": int(bounds.Width),
            "height": int(bounds.Height),
            "maximized": state == 2,
        }
    except Exception:
        return None
