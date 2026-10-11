"""CourseLens 合成档正式标准测试后端（测试模式契约件⑥，TESTBENCH-DESIGN-1）。

本入口已收编为 ``COURSELENS_TEST_MODE=synthetic`` 的标准后端（契约面见
``src/runtime/test_mode.py``：``SYNTHETIC_BACKEND_MODULE`` +
``synthetic_backend_recipe()``）。车道经本入口起合成环境（配合
``tests/testbench/instances.py`` 实例管理器认领端口与数据目录），不再自拷
自改 serve 变体。运行语义：未设 env 结构性自设 synthetic（兼容期既有命令行
原样可用）；本机 loopback 之外出站默认全拒（产品内 egress 门）；
确定性时钟唯一接入点 = ``--seed-clock real|frozen``（种子层）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application import (
    ONBOARDING_GUIDE_SCHEMA,
    ONBOARDING_GUIDE_STATE_KEY,
    ONBOARDING_GUIDE_VERSION,
    CourseLensApplication,
)
from src.runtime.http_api import make_handler
from src.runtime.live_room import ProxyResponse
from src.runtime.media_source import parse_upstream_range
from src.runtime.test_mode import (
    ONBOARDING_GUIDE_MODES,
    SEED_CLOCK_MODES,
    TEST_MODE_ENV,
    TEST_MODE_SYNTHETIC,
    TestModeContractError,
    resolve_synthetic_backend_mode,
)
from tests.http_services import http_services


_MEDIA_SECONDS = 10
_LIVE_SECONDS = 6
_LIVE_SEGMENT_SECONDS = 2


class _LocalMediaStream:
    """本地合成媒体流对象。

    复刻 src/runtime/media_source.RemoteMediaStream 中被 http_api._stream_remote_media
    消费的最小契约：status / content_type / content_length / content_range /
    iter_bytes(chunk_size) / close()，200/206/416 与 Content-Range 语义对齐。
    """

    def __init__(
        self, status: int, body: bytes, content_range: str = "", content_length: int | None = None
    ):
        self.status = status
        self.content_type = "video/mp4"
        self.content_length = len(body) if content_length is None else content_length
        self.content_range = content_range
        self._body = body

    def iter_bytes(self, chunk_size: int = 64 * 1024):
        step = max(1, int(chunk_size))
        for offset in range(0, len(self._body), step):
            yield self._body[offset:offset + step]

    def close(self) -> None:
        self._body = b""


def _open_local_media(data: bytes, range_header: str, *, head_only: bool) -> _LocalMediaStream:
    """按产品 RemoteMediaGateway 的 200/206/416 语义切分本地合成媒体。"""
    total = len(data)
    if head_only:
        return _LocalMediaStream(200, b"", content_length=total)
    requested = parse_upstream_range(range_header)
    if requested is None:
        return _LocalMediaStream(200, data)
    start, end = requested
    if start is None:
        suffix = min(total, int(end or 0))
        start, end = max(0, total - suffix), total - 1
    else:
        end = min(total - 1, end if end is not None else total - 1)
    if total <= 0 or start >= total or end < start:
        return _LocalMediaStream(416, b"", content_range=f"bytes */{total}")
    return _LocalMediaStream(206, data[start:end + 1], content_range=f"bytes {start}-{end}/{total}")


def _run_ffmpeg(args: list[str]) -> bool:
    """仅在本机存在 ffmpeg 时同步执行一次生成；失败打印闭集警告并返回 False。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[synthetic-shell] ffmpeg 生成失败: {exc}", file=sys.stderr, flush=True)
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:400]
        print(f"[synthetic-shell] ffmpeg 生成失败: {detail}", file=sys.stderr, flush=True)
        return False
    return True


def _generate_synthetic_media(path: Path) -> bool:
    # 画面为 lavfi testsrc2 合成测试图案，音轨为静音，完全本地生成、无下载内容。
    return _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(_MEDIA_SECONDS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "32k", "-shortest", "-movflags", "+faststart",
        str(path),
    ])


def _generate_synthetic_live_hls(directory: Path) -> bool:
    # 本地 VOD HLS：2s 强制关键帧切片，约 3 个 MPEG-TS 分段，合计约 6s，含 ENDLIST。
    directory.mkdir(parents=True, exist_ok=True)
    return _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(_LIVE_SECONDS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "32k", "-shortest",
        "-force_key_frames", f"expr:gte(t,n_forced*{_LIVE_SEGMENT_SECONDS})",
        "-f", "hls", "-hls_time", str(_LIVE_SEGMENT_SECONDS), "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(directory / "segment%d.ts"),
        str(directory / "index.m3u8"),
    ])


def build_service(root: Path, onboarding_profile: str = "ready") -> CourseLensApplication:
    service = CourseLensApplication(root)
    # 合成字幕：绝对 vtt_path 位于 output_dir 内，字幕阅读区可在浏览器中验收。
    # 时间轴设计（合成媒体时长 10s，所有窗口都落在媒体时长内，可在播放器内拖动验证）：
    #   0-1.5 第一句；1.5-2.5 第二句（顺序 cue）；
    #   2.5-4.5 第三句 与 3.5-5.5 第四句 → 3.5-4.5 两条 cue 同时活跃（重叠竞争）；
    #   5.5-8.0 超长带标点 cue（>120 字，服务端 split_long_cues 按中文标点拆分）；
    #   8.0-10.0 无标点超长 cue（标点逻辑无切点，由 split_long_cues 按定长硬切）。
    transcript_path = root / "synthetic-transcript.vtt"
    long_cue = (
        "本段为超长合成字幕，用于验收长句自动拆分效果。"
        "它模拟真实转写里一个三十秒窗口输出一整段内容的情况，"
        "包含逗号、顿号与句号等中文标点，"
        "因此系统会按标点把它拆分成多个较短的展示单元，"
        "每个单元的时间按字符比例分配，"
        "阅读面板与视频轨道使用同一套拆分结果。"
    )
    unsplit_cue = (
        "合成字幕无标点长句用于验收浏览器换行"
        "整段不包含任何中文句读标点因此拆分逻辑只能按定长硬切"
        "每一块子段仍然较长需要按容器宽度字体与行高实测换行表现"
        "同时用于核对最小显示时长与按字符比例的时间分配是否成立"
        "所有内容均为合成测试文本不含任何真实课程信息"
        "若在窄屏下出现横向溢出则属于需要修复的缺陷"
        "截图矩阵会在三种宽度与两种主题下核对该行为"
    )
    assert len(long_cue) > 120
    assert len(unsplit_cue) >= 150
    assert not any(char in "。！？；，、" for char in unsplit_cue)
    transcript_path.write_text(
        "WEBVTT\n\n"
        "00:00.000 --> 00:01.500\n合成字幕第一句，用于验收阅读区。\n\n"
        "00:01.500 --> 00:02.500\n合成字幕第二句，验证当前行高亮。\n\n"
        "00:02.500 --> 00:04.500\n合成字幕第三句，与下一条同时活跃，验收重叠字幕。\n\n"
        "00:03.500 --> 00:05.500\n合成字幕第四句，与上一条同时活跃，验收遮挡表现。\n\n"
        f"00:05.500 --> 00:08.000\n{long_cue}\n\n"
        f"00:08.000 --> 00:10.000\n{unsplit_cue}\n",
        encoding="utf-8",
    )
    courses = []
    for index in range(28):
        course_id = f"90{index:03d}"
        title = ["线性代数", "数据结构", "概率论", "计算机网络"][index % 4]
        teacher = ["陈老师", "林老师", "周老师"][index % 3]
        service.catalog_repository.upsert_course(
            course_id,
            f"{title} {index + 1}",
            teacher,
            term="2026-2027-1" if index < 18 else "2025-2026-2",
            department="计算机科学技术学院" if index % 2 else "数学科学学院",
            authorization_state="verified",
        )
        for lecture_index in range(3):
            service.catalog_repository.upsert_lecture(course_id, {
                "sub_id": f"{course_id}{lecture_index + 1}",
                "sub_title": f"第 {lecture_index + 1} 讲",
                "date": f"2026-0{9 - lecture_index}-1{lecture_index}",
                "has_playback": True,
                "vtt_path": str(transcript_path),
            })
        courses.append({"course_id": course_id})
    service.set_credentials("synthetic-student", "synthetic-password", remember=False)
    with service._lock:
        service._client = object()
        # COURSELENS_SYNTHETIC_SESSION_TTL：合成会话有效期秒数（默认 3600）。
        # 长走查/普查驱动可经环境变量调长——夜批14 R2 教训：>1h 的 drive 会被
        # 硬编码 1h TTL 的 401 降级打断（其结果文件 CP2 使用注意项）。零产品
        # 语义变化，仅测试基建。
        try:
            session_ttl = max(1.0, float(os.environ.get("COURSELENS_SYNTHETIC_SESSION_TTL") or 3600))
        except ValueError:
            session_ttl = 3600.0
        service._client_last_verified_at = time.monotonic() + session_ttl

    # 合成回放媒体：本机存在 ffmpeg 时生成本地可解码 H.264/AAC MP4（lavfi 全合成画面），
    # open_remote_media 按产品 _stream_remote_media 的流契约回源该文件（200/206/416）。
    # ffmpeg 缺失或生成失败时保持旧 FileNotFoundError → 404 行为不变。
    media_data = b""
    media_path = root / "synthetic-media.mp4"
    if shutil.which("ffmpeg") and _generate_synthetic_media(media_path):
        media_data = media_path.read_bytes()
    if media_data:
        def _synthetic_open_remote_media(sub_id, range_header="", *, head_only=False):
            # 合成环境不得触发真实会话刷新/登录：只回放本地合成媒体，
            # 未登记的 sub_id 与产品路径一致保持 404 "Media is unavailable"。
            row = service.catalog_repository.get_lecture(str(sub_id))
            if not row or not bool(row.get("has_playback", True)):
                raise FileNotFoundError(str(sub_id))
            return _open_local_media(media_data, range_header, head_only=head_only)
    else:
        def _synthetic_open_remote_media(sub_id, range_header="", *, head_only=False):
            raise FileNotFoundError(str(sub_id))
    service.open_remote_media = _synthetic_open_remote_media
    service._set_login_status("ready", "courses", "合成身份已验证", connected=True, course_total=len(courses))
    service._store_authorized_catalog(courses)
    today = date.today()
    semester_start = today - timedelta(days=today.weekday() + 7 * 7)
    semester = {
        "semester_id": "2026-2027-1", "label": "2026-2027 第一学期",
        "start_date": semester_start.isoformat(), "source": "fixture",
        "is_default": True, "selectable": True,
    }
    timetable_courses = []
    for index in range(10):
        course_id = f"90{index:03d}"
        timetable_courses.append({
            "timetable_course_id": f"fixture:{course_id}",
            "source": "fudan_undergraduate", "semester_id": semester["semester_id"],
            "semester_label": semester["label"], "semester_start_date": semester["start_date"],
            "lesson_id": str(index), "course_code": f"TEST{index:04d}",
            "title": f"{['线性代数', '数据结构', '概率论', '计算机网络'][index % 4]} {index + 1}",
            "teachers": [["陈老师", "林老师", "周老师"][index % 3]],
            "room": ["H3101", "HGX502", "江湾二号楼 B201"][index % 3],
            "week_indexes": list(range(1, 19)),
            "meetings": [{
                "weekday": index % 7 + 1,
                "start_unit": index % 10 + 1,
                "end_unit": min(15, index % 10 + 2),
            }],
        })
    scope = service._identity_scope()
    service.timetable.store.put(scope, "fudan_undergraduate", semester["semester_id"], {
        "source": "fudan_undergraduate", "semester": semester, "semesters": [semester],
        "courses": timetable_courses, "observed_at": time.time(), "partial_failures": [],
    })
    service.timetable.store.update_preferences(scope, selected_semester_id=semester["semester_id"])
    def synthetic_discover_course(course_id: str) -> dict:
        course = next(
            item for item in service.catalog_repository.courses()
            if str(item.get("course_id") or "") == str(course_id)
        )
        detail = {
            **course,
            "course_id": str(course_id),
            "lectures": service.catalog_repository.lectures_for_course(str(course_id)),
        }
        return service.timetable.enrich_catalog([detail])[0]
    service.discover_course = synthetic_discover_course
    live_states = ("live", "upcoming", "ended", "denied", "offline", "stale", "unknown")
    service.live_room_status = lambda course_id: {
        "state": live_states[max(0, int(str(course_id)[-1]) - 1) % len(live_states)],
        "can_enter": live_states[max(0, int(str(course_id)[-1]) - 1) % len(live_states)] == "live",
        "observed_at": time.time(),
        "expires_at": time.time() + 20,
        "starts_at": "2026-07-28T19:00:00+08:00",
    }
    service.issue_live_room_grant = lambda course_id: {
        "grant": f"synthetic-{course_id}", "expires_at": time.time() + 30,
    }
    # C2-2（N15-W1 收梯）：产品端 consume_grant(grant, view=)（直播二期乙1
    # 契约）——stub 签名同步收 view，缺形参会让合成壳直播间 500。
    service.consume_live_room_grant = lambda grant, view=None: {
        "session_id": "s" * 32,
        "manifest_id": "m" * 24,
        "manifest_path": f"/api/v3/live-room/play/{'s' * 32}/manifest/{'m' * 24}",
        "expires_at": time.time() + 300,
    }
    # 合成直播 HLS：本机存在 ffmpeg 时生成本地 VOD 清单 + 约 3 个 2s MPEG-TS 分段
    # （合计约 6s，含 #EXT-X-ENDLIST），并把清单内的分片行改写为
    # /api/v3/live-room/play/{session}/resource/{id} 绝对路径。清单/分段字节常驻内存，
    # fetch_live_room_resource 的签名与 ProxyResponse 契约保持不变；
    # 生成失败时回退到旧的空 ENDLIST 清单，未知 session/资源诚实 404。
    live_session_id = "s" * 32
    live_manifest_id = "m" * 24
    live_manifest_body = b"#EXTM3U\n#EXT-X-ENDLIST\n"
    live_segment_bodies: dict[str, bytes] = {}
    live_dir = root / "live-manifest"
    if shutil.which("ffmpeg") and _generate_synthetic_live_hls(live_dir):
        playlist_lines: list[str] = []
        for raw_line in (live_dir / "index.m3u8").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                playlist_lines.append(line)
                continue
            resource_id = f"synthetic-segment-{len(live_segment_bodies):02d}-" + "0" * 16
            live_segment_bodies[resource_id] = (live_dir / line).read_bytes()
            playlist_lines.append(
                f"/api/v3/live-room/play/{live_session_id}/resource/{resource_id}"
            )
        live_manifest_body = ("\n".join(playlist_lines) + "\n").encode("utf-8")

    def _synthetic_fetch_live_room_resource(
        session_id, resource_id, *, cookie_session="", range_header=""
    ):
        if str(session_id) != live_session_id:
            return ProxyResponse(404, "text/plain", b"synthetic live session not found\n")
        if str(resource_id) == live_manifest_id:
            return ProxyResponse(200, "application/vnd.apple.mpegurl", live_manifest_body)
        segment = live_segment_bodies.get(str(resource_id))
        if segment is None:
            return ProxyResponse(404, "text/plain", b"synthetic live resource not found\n")
        return ProxyResponse(200, "video/mp2t", segment)

    service.fetch_live_room_resource = _synthetic_fetch_live_room_resource
    # onboarding-profile fixture：partial 用于验收“可选能力未配置”的诚实呈现
    # （consent/ai 关闭但保留版本字段；fudan/github 等其余证据保持就绪不变）。
    consent_accepted = True
    ai_configured = True
    if onboarding_profile == "partial":
        consent_accepted = False
        ai_configured = False
    service.onboarding_snapshot = lambda: {
        "network": {
            "mode": "auto",
            "proxy_url": "",
            "routes": {
                "icourse": {"state": "ready"},
                "github": {"state": "ready"},
            },
        },
        "github": {
            "app_configured": True,
            "installed": True,
            "authorized": True,
            "bootstrapped": True,
            "worker_trusted": True,
        },
        "fudan": {"configured": True, "connected": True},
        "ai": {"configured": ai_configured},
        "consent": {"accepted": consent_accepted, "version": "synthetic", "accepted_at": time.time()},
        "approvals": {"github_terms": True, "fudan_processing": True},
        "release_mode": "student-pilot",
    }
    return service


def _seed_onboarding_guide(service: CourseLensApplication, mode: str) -> None:
    """引导记录fixture：合成证据本身会被规范化为旧用户，因此除 legacy/auto 外都显式播种。"""
    if mode in {"legacy", "auto"}:
        # 不播种记录：GET 时按旧用户证据（已授权目录 app state / 已保存账号 / 历史）规范化
        return
    if mode == "corrupt":
        # 当前版本 + 字段损坏 → persistence=invalid，禁止自动打开
        service.task_store.set_app_state(ONBOARDING_GUIDE_STATE_KEY, {
            "version": ONBOARDING_GUIDE_VERSION,
            "disposition": "unexpected-disposition",
            "auto_opened": "yes",
        })
        return
    service.task_store.set_app_state(ONBOARDING_GUIDE_STATE_KEY, {
        "schema": ONBOARDING_GUIDE_SCHEMA,
        "version": ONBOARDING_GUIDE_VERSION,
        "disposition": mode,
        "auto_opened": mode != "new",
        "updated_at": time.time(),
    })


# ---- 任务/连接态种子接口（SHELL-SEED-1，N9-F）----
# 语义约束：种子一律走产品写路径（add_task / mark_terminal / record_task_usage /
# upsert_automation_run / set_deepseek_key），绝不裸 SQL——墓碑抑制、任务行去重、
# AS6 绝对值语义只有经过唯一入口才被真实行使。字段表全部闭集。

SEED_PRESETS = (
    "tasks-active",
    "tasks-failed",
    "tasks-completed",
    "github-missing-secret",
    "deepseek-saved",
)

_TASK_SEED_FIELDS = frozenset({
    "kind", "course_id", "sub_id", "config_key", "state", "payload", "progress",
    "error", "deepseek_tokens", "runner_seconds",
})

_MISSING_ENVIRONMENT_SECRETS = ("WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY")

# SWEEPFIX-1 T3（化身走查 SWEEP1-T3）：普通任务记录的删除权威性删账。
# 产品侧任务删除是硬删（task_store.delete_task），P57 墓碑表只盖自动化运行
# 记录——同 --seed 重启壳重播种时，被学生删除的 failed/completed 种子记录
# 曾以新 task_id 复活（SWEEP1 实例 B 实证）。本删账以壳命名空间的 app-state
# 键持久化删除事实（随持久 state-dir 存活，=「重启壳不复现」的物理前提），
# seed_task_rows 重播种时据此干净抑制。产品路径零改动（src/** 非本车道恰域）。
_SEED_DELETE_LEDGER_KEY = "synthetic-seed-deleted-identities"


def _seed_identity(task: dict) -> str:
    """种子身份键：kind/course_id/sub_id/state 闭集四元组（与 spec 字段表同源）。"""
    return ":".join([
        str(task.get("kind") or ""),
        str(task.get("course_id") or ""),
        str(task.get("sub_id") or ""),
        str(task.get("state") or ""),
    ])


def _deleted_seed_identities(service: CourseLensApplication) -> set[str]:
    value = service.task_store.get_app_state(_SEED_DELETE_LEDGER_KEY)
    return {str(item) for item in value} if isinstance(value, list) else set()


def _install_seed_delete_capture(service: CourseLensApplication) -> None:
    """把产品删除路径的删除事实记入持久删账（幂等装饰，种子应用时安装）。

    单条删除与批量清理失败两条产品路径都覆盖；删除事实只增不删（删账本身
    不提供撤销——删除权威性原则）。非种子任务的删除也会记账，但只有与种子
    身份同键的条目才会被重播种消费。"""
    store = service.task_store
    if getattr(store, "_synthetic_seed_capture_installed", False):
        return
    store._synthetic_seed_capture_installed = True

    def _record(task: dict) -> None:
        current = store.get_app_state(_SEED_DELETE_LEDGER_KEY)
        ledger = [str(item) for item in current] if isinstance(current, list) else []
        identity = _seed_identity(task)
        if identity not in ledger:
            ledger.append(identity)
            store.set_app_state(_SEED_DELETE_LEDGER_KEY, ledger)

    original_delete = store.delete_task

    def delete_task_with_capture(task_id: str) -> dict:
        task = original_delete(task_id)
        _record(task)
        return task

    store.delete_task = delete_task_with_capture

    original_bulk = store.delete_failed_tasks

    def delete_failed_tasks_with_capture() -> int:
        doomed = store.list_tasks(states=("failed",))
        removed = original_bulk()
        for task in doomed:
            _record(task)
        return removed

    store.delete_failed_tasks = delete_failed_tasks_with_capture


# ---- HARNESS-FIX-1（VISBASE-2-R2 移交配方）：种子冻结钟 ----
# 视觉基线 harness（tests/visual-baseline/capture.mjs）把浏览器时钟冻结在
# 本地 2026-10-20 10:08，而 public_task 的 stale 判定用真实钟
# （src/runtime/http_api.py：current=time.time()，running TTL=20s）——种子以
# 真实钟落 updated_at 时，「服务启动→抽屉采集」墙钟间隔跨 20s 即 fresh/stale
# 混切（同一次全量运行内四组合两态共存，任何单一形态的钉都无法恒绿）；
# running 卡「已运行 X 分钟」走 elapsed 回退链 real_now-started_at 同样随间隔
# 漂移；失败卡「已运行 0 秒」行有无 flicker=created_at(add_task) 与
# finished_at(mark_terminal) 连续写时的时钟刻度相等性竞速（相等→行缺席）。
# --seed-clock frozen 把种子时间戳锚定为相对冻结时刻的定值（opt-in，默认
# real 保持产品 staleness 语义测试不受影响），渲染形状成为种子常数的纯函数。
# 冻结面与 capture.mjs FROZEN_EPOCH_SEC 同源（本地壁钟 2026-10-20 10:08:00）。
FROZEN_SEED_EPOCH = float(datetime(2026, 10, 20, 10, 8, 0).timestamp())

# 相对 FROZEN_SEED_EPOCH 的偏移（秒）。设计约束：
# - running：updated=FROZEN → observed=FROZEN → expires=FROZEN+20，对任何
#   早于冻结时刻的真实钟恒 fresh；started=FROZEN-60 + progress 追加
#   elapsed_active_seconds=60 → 「正在估算 · 已运行 1 分钟」恒定（0.1.1 文档
#   意图形态）；created 更早，排队锚不参与非 queued 卡渲染。
# - failed：created=updated=finished 同值、started=NULL → elapsed 链 None，
#   前端 runtime 行确定性缺席（U1「绝不渲染 0 秒」意图态，杜绝刻度竞速）。
# - completed：started=NULL → elapsed None → runtime 行走 runner_seconds
#   「云端运行 12 分钟」分支（既有像素保持）；终态 stale 恒 False。
_FROZEN_SEED_ANCHORS: dict[str, dict[str, float | None]] = {
    "queued": {"created_at": -60.0, "started_at": None, "updated_at": -60.0, "finished_at": None},
    "running": {"created_at": -3600.0, "started_at": -60.0, "updated_at": 0.0, "finished_at": None},
    "failed": {"created_at": -660.0, "started_at": None, "updated_at": -660.0, "finished_at": -660.0},
    "canceled": {"created_at": -660.0, "started_at": None, "updated_at": -660.0, "finished_at": -660.0},
    "completed": {"created_at": -720.0, "started_at": None, "updated_at": -600.0, "finished_at": -600.0},
}

_FROZEN_RUNNING_ELAPSED_SECONDS = 60.0


def _pin_seed_task_clock(
    service: CourseLensApplication, task_id: str, *, clock: dict[str, float | None]
) -> None:
    """把种子行时间戳钉到冻结锚点（update_task 恒以真实钟盖 updated_at 且不收
    updated_at/created_at 字段，产品路径无此语义——合成壳种子面专属直写）。
    clock 值为相对 FROZEN_SEED_EPOCH 的偏移（秒），此处换算为绝对 epoch。"""
    store = service.task_store
    with store._lock, store._connect() as db:
        db.execute(
            "UPDATE tasks SET created_at=?, updated_at=?, started_at=?, finished_at=? WHERE task_id=?",
            (
                FROZEN_SEED_EPOCH + float(clock["created_at"]),
                FROZEN_SEED_EPOCH + float(clock["updated_at"]),
                None if clock["started_at"] is None else FROZEN_SEED_EPOCH + float(clock["started_at"]),
                None if clock["finished_at"] is None else FROZEN_SEED_EPOCH + float(clock["finished_at"]),
                str(task_id),
            ),
        )


def seed_task_rows(
    service: CourseLensApplication, specs: list[dict], *, clock: str = "real"
) -> list[str]:
    """按产品路径播种任务行，返回 task_id 列表。

    state 闭集：queued（原样）/ running（补 started_at+progress）/ 终态
    （mark_terminal 落 error）；deepseek_tokens/runner_seconds 经
    record_task_usage 落绝对值（缺省不写=NULL=前端不渲染）。
    clock="frozen"（视觉 harness 专用）：时间戳全部锚定为相对 FROZEN_SEED_EPOCH
    的定值（见 _FROZEN_SEED_ANCHORS），running 的 progress 追加
    elapsed_active_seconds 定值——stale/elapsed 渲染形状与「播种→采集」墙钟
    间隔彻底解耦；clock="real"（默认）保持真实钟，产品 staleness 语义可测。
    """
    from src.runtime.task_store import TERMINAL_STATES

    if clock not in {"real", "frozen"}:
        raise ValueError(f"unsupported seed clock: {clock!r}")
    frozen = clock == "frozen"

    seeded: list[str] = []
    for spec in specs:
        unknown = sorted(set(str(key) for key in spec) - _TASK_SEED_FIELDS)
        if unknown:
            raise ValueError(f"unsupported task seed fields: {unknown}")
        state = str(spec.get("state") or "queued").strip()
        if state not in {"queued", "running", *TERMINAL_STATES}:
            raise ValueError(f"unsupported task seed state: {state!r}")
        # SWEEPFIX-1 T3：删除权威性——被学生删除过的种子身份，重播种不再复活。
        if _seed_identity({**spec, "state": state}) in _deleted_seed_identities(service):
            continue
        task, _created = service.task_store.add_task(
            str(spec.get("kind") or "subtitle"),
            str(spec.get("course_id") or "9000"),
            str(spec.get("sub_id") or "90001"),
            dict(spec.get("payload") or {}),
            config_key=str(spec.get("config_key") or ""),
        )
        task_id = str(task["task_id"])
        now = time.time()
        if state == "running":
            progress = dict(spec.get("progress") or {})
            if frozen:
                progress.setdefault("elapsed_active_seconds", _FROZEN_RUNNING_ELAPSED_SECONDS)
            service.task_store.update_task(
                task_id, state="running", started_at=now,
                progress=progress,
            )
        elif state in TERMINAL_STATES:
            service.task_store.mark_terminal(task_id, state, error=str(spec.get("error") or ""))
        if spec.get("progress") and state != "running":
            service.task_store.update_task(task_id, progress=dict(spec["progress"]))
        if frozen:
            _pin_seed_task_clock(service, task_id, clock=_FROZEN_SEED_ANCHORS[state])
        usage: dict = {}
        if spec.get("deepseek_tokens") is not None:
            usage["deepseek_tokens"] = max(0, int(spec["deepseek_tokens"]))
        if spec.get("runner_seconds") is not None:
            usage["runner_seconds"] = max(0.0, float(spec["runner_seconds"]))
        if usage:
            service.task_store.record_task_usage(task_id, **usage)
        seeded.append(task_id)
    return seeded


def _seed_github_missing_secret(service: CourseLensApplication) -> None:
    """包装 remote_connection_snapshot：把 environment 组件替换为探针在缺钥
    场景下会产出的同一形状（connection.py 探针分支的真值拷贝），其余组件
    与 overall 派生零触碰。必须在 http_services 绑定前赋值才会生效。"""
    real_snapshot = service.remote_connection_snapshot

    def snapshot_with_missing_secret(*, fresh: bool = False) -> dict:
        value = real_snapshot(fresh=fresh)
        components = value.get("components")
        if not isinstance(components, list):
            return value
        now = time.time()
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            if str(component.get("component") or "") != "environment":
                continue
            evidence = component.get("evidence")
            evidence = dict(evidence) if isinstance(evidence, dict) else {}
            evidence["job_token_present"] = bool(evidence.get("job_token_present"))
            evidence["missing_secrets"] = list(_MISSING_ENVIRONMENT_SECRETS)
            components[index] = {
                **component,
                "state": "action_required",
                "source": "github_api",
                "code": "environment_incomplete",
                "stale": False,
                "actions": ["rotate-worker-keys"],
                "evidence": evidence,
                "observed_at": now,
                "expires_at": now + 90.0,
            }
        return value

    service.remote_connection_snapshot = snapshot_with_missing_secret


def apply_synthetic_seeds(
    service: CourseLensApplication,
    presets: list[str] | None = None,
    seed_tasks_path: Path | str | None = None,
    *,
    seed_clock: str = "real",
) -> dict:
    """应用 --seed / --seed-tasks 种子；返回闭集摘要供结果核对。

    seed_clock 透传 seed_task_rows（"real" 默认 / "frozen" 视觉 harness 专用
    冻结钟锚定）；tasks-failed 附带的自动化运行记录在 frozen 档同锚冻结。"""
    applied: dict = {"presets": [], "seeded_task_ids": [], "automation_run_keys": []}
    frozen = seed_clock == "frozen"
    for preset in list(presets or []):
        if preset == "tasks-active":
            seeded = seed_task_rows(service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
                "state": "running",
                # SWEEPFIX-3 T6（化身走查 SWEEP1-T6）：progress 只用产品合同键
                # （public_task 从 media_duration_seconds/processed_media_seconds
                # 派生 progress_unit="seconds"；此前种子用非合同键 progress_unit，
                # 派生落 items，「转写 96 秒」被前端渲染成「96 / 600 项」）。
                "progress": {
                    "label": "正在转写音频",
                    "media_duration_seconds": 600,
                    "processed_media_seconds": 96,
                },
            }], clock=seed_clock)
            applied["seeded_task_ids"] += seeded
        elif preset == "tasks-failed":
            seeded = seed_task_rows(service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90002",
                "state": "failed", "error": "GitHubAppError: worker_tree_drifted",
            }], clock=seed_clock)
            applied["seeded_task_ids"] += seeded
            # 终态失败自动材料运行：经 P57 upsert 唯一入口；deletion 后重启
            # 再播种会在此处被墓碑抑制（upsert 返回 None），可视化「不复现」。
            run = service.task_store.upsert_automation_run(
                "synthetic-automation-run-0001",
                workflow="cloud-daily.yml", trigger_kind="manual",
                state="completed", conclusion="failure",
                counts={"processed": 1, "deferred": 0},
                started_at=(FROZEN_SEED_EPOCH - 600.0) if frozen else time.time() - 600,
                concluded_at=(FROZEN_SEED_EPOCH - 540.0) if frozen else time.time() - 540,
            )
            if run is not None:
                applied["automation_run_keys"].append(str(run["run_key"]))
        elif preset == "tasks-completed":
            seeded = seed_task_rows(service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90003",
                "state": "completed",
                "deepseek_tokens": 120000, "runner_seconds": 754.0,
            }], clock=seed_clock)
            applied["seeded_task_ids"] += seeded
        elif preset == "github-missing-secret":
            _seed_github_missing_secret(service)
        elif preset == "deepseek-saved":
            # 产品路径保存（DPAPI 入服务根凭据库；产品保存流无网络校验）。
            service.set_deepseek_key("sk-synthetic-shell-000000000000", remember=True)
        else:
            raise ValueError(f"unsupported seed preset: {preset!r}")
        applied["presets"].append(preset)
    if seed_tasks_path:
        raw = Path(seed_tasks_path).read_text(encoding="utf-8")
        specs = json.loads(raw)
        if not isinstance(specs, list):
            raise ValueError("--seed-tasks JSON must be a list of task seed specs")
        applied["seeded_task_ids"] += seed_task_rows(service, specs, clock=seed_clock)
    _install_seed_delete_capture(service)
    return applied


def main() -> None:
    # TB-MODE-M1 收编：本进程 = 测试模式 synthetic 档的正式标准后端
    # （src/runtime/test_mode.py SYNTHETIC_BACKEND_MODULE，契约成文+旗标闭集）。
    # 未设 COURSELENS_TEST_MODE 时结构性自设 synthetic（既有车道命令行原样可用
    # =兼容期语义）；显式 real / 非法值 = 契约违例即拒（合成壳绝不充当 real
    # 后端）。进程内产品代码（egress 门、数据目录面）由此统一看到 synthetic 档。
    try:
        shell_mode = resolve_synthetic_backend_mode()
    except TestModeContractError as exc:
        raise SystemExit(f"[synthetic-shell] {exc.human_message}")
    os.environ[TEST_MODE_ENV] = shell_mode
    parser = argparse.ArgumentParser(
        description=(
            "CourseLens synthetic-mode formal test backend "
            f"(COURSELENS_TEST_MODE={TEST_MODE_SYNTHETIC}; see "
            "src/runtime/test_mode.py for the test-mode contract)"
        )
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--onboarding-guide",
        choices=ONBOARDING_GUIDE_MODES,
        default="new",
        help="seed the student_onboarding_guide app-state fixture (legacy/auto seed nothing)",
    )
    parser.add_argument(
        "--onboarding-fault",
        choices=("none", "actions"),
        default="none",
        help="make POST /api/v3/onboarding/actions fail so recovery paths are exercisable",
    )
    parser.add_argument(
        "--onboarding-profile",
        choices=("ready", "partial"),
        default="ready",
        help="onboarding evidence profile: ready = all configured (default); "
             "partial = consent.accepted=false and ai.configured=false for optional-capability views",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "cache",
        help="scratch root for the synthetic shell's TemporaryDirectory "
             "(default: PROJECT_ROOT/runtime/cache; must stay inside the project)",
    )
    parser.add_argument(
        "--seed",
        action="append",
        choices=SEED_PRESETS,
        default=[],
        help="seed a synthetic fixture preset (repeatable): "
             + ", ".join(SEED_PRESETS),
    )
    parser.add_argument(
        "--seed-tasks",
        type=Path,
        default=None,
        help="JSON file with a closed-set list of task seed specs "
             "(kind/course_id/sub_id/state/payload/progress/error/"
             "deepseek_tokens/runner_seconds)",
    )
    parser.add_argument(
        "--seed-clock",
        choices=SEED_CLOCK_MODES,
        default="real",
        help="task seed timestamps: real (default, wall clock) or frozen "
        "(visual-baseline harness: anchor seeds to 2026-10-20 10:08 local "
        "so drawer fresh/stale/elapsed rendering is wall-clock independent)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="persistent service data root (must stay inside the project); "
             "enables delete-then-restart walkthroughs. Default: an ephemeral "
             "temporary directory under --cache-root",
    )
    args = parser.parse_args()
    print(
        f"[synthetic-shell] COURSELENS_TEST_MODE={shell_mode} "
        "(formal synthetic backend; egress default-deny beyond loopback)",
        flush=True,
    )
    cache_root = Path(args.cache_root)
    try:
        cache_root.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        # 合成壳凭据库走 ensure_inside_project 门（credentials.py），项目外根会在
        # CredentialStore 深处炸出裸 traceback；此处提前闭集报错。
        parser.error(
            "--cache-root must resolve inside the project: the synthetic shell's "
            "credential store only accepts in-project paths"
        )
    cache_root.mkdir(parents=True, exist_ok=True)
    state_dir: Path | None = None
    if args.state_dir is not None:
        state_dir = Path(args.state_dir)
        try:
            state_dir.resolve().relative_to(PROJECT_ROOT)
        except ValueError:
            parser.error(
                "--state-dir must resolve inside the project: the synthetic shell's "
                "credential store only accepts in-project paths"
            )
        state_dir.mkdir(parents=True, exist_ok=True)

    def _build_seeded_service(root: Path) -> CourseLensApplication:
        service = build_service(root, onboarding_profile=args.onboarding_profile)
        _seed_onboarding_guide(service, args.onboarding_guide)
        # 种子先于 http_services 装配：命名空间绑定捕获当时的可调用对象，
        # github-missing-secret 的快照包装必须在此前落到 service 上；
        # onboarding-fault 注入同理（TB-MODE-M1 收编：适配层已显式透传
        # onboarding_guide_snapshot/action，装配前注入即可生效，不再事后补挂）。
        if args.onboarding_fault == "actions":
            def _synthetic_onboarding_fault(_action: str, _version: str) -> dict:
                raise RuntimeError("synthetic onboarding action fault")
            service.onboarding_guide_action = _synthetic_onboarding_fault
        summary = apply_synthetic_seeds(
            service, presets=args.seed, seed_tasks_path=args.seed_tasks,
            seed_clock=args.seed_clock,
        )
        print(f"[synthetic-shell] seeds applied: {json.dumps(summary, ensure_ascii=False)}", flush=True)
        return service

    def _serve(service: CourseLensApplication) -> None:
        services = http_services(service)
        server = ThreadingHTTPServer(
            ("127.0.0.1", args.port),
            make_handler(services, PROJECT_ROOT / "frontend"),
        )
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()

    if state_dir is not None:
        # 持久根：数据跨进程存续（删除→重启→不复现类走查依赖这一点）；
        # 退出只关服务，绝不清理 state_dir 内容。
        service = _build_seeded_service(state_dir)
        try:
            _serve(service)
        finally:
            service.close()
    else:
        with tempfile.TemporaryDirectory(dir=cache_root, prefix="synthetic-shell-") as temp:
            service = _build_seeded_service(Path(temp))
            try:
                _serve(service)
            finally:
                service.close()


if __name__ == "__main__":
    main()
