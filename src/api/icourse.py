"""
iCourse API client for Fudan University's smart teaching platform.

Provides access to course details, lecture lists, and short-lived video URLs
through WebVPN.
"""

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import date, datetime
from urllib.parse import unquote, urlparse

import requests

from src.runtime import config
from src.runtime.live_room import (
    DEFAULT_LIVE_VIEW,
    LIVE_VIEW_IDS,
    LIVE_VIEWS,
    LiveRoomError,
    available_views_from_live_url,
)
from src.api.webvpn import WebVPNSession


_DATE_FROM_SUB_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# search-ppt 流式分页的资源护栏。这些是大预算的熔断值而不是页数上限：
# 合法长讲次可以返回任意多事件；只有响应体过大、服务器游标停滞或事件密度
# 明显异常时才停止，停止时向上层报闭集错误码而不是悄悄截断尾部。
PPT_PAGE_SIZE = 100
PPT_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
PPT_RECORD_STORM_LIMIT = 20000

# search-ppt 响应/记录校验失败的闭集类别（无任何字段值）。
PPT_LIST_ERRORS = frozenset({
    "ppt_payload_invalid",
    "ppt_response_too_large",
    "ppt_pagination_stalled",
    "ppt_record_storm",
})


class PptListError(RuntimeError):
    """One bounded search-ppt guard tripped; ``code`` stays in a closed set."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code if code in PPT_LIST_ERRORS else "ppt_payload_invalid"
_SESSION_TOKEN_RE = re.compile(
    r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}'
)

# 闭集目录诊断标签（私有 catalog_reason 属性，不是公开 error code）。
# 只描述失败类别，绝不含 URL、响应正文、账号或课程字段值。
_CATALOG_REASONS = frozenset({
    "userinfo_rejected",
    "identity_incomplete",
    "bearer_unavailable",
    "index_code_rejected",
    "index_payload_shape_invalid",
    "index_row_limit_exceeded",
    "index_course_limit_exceeded",
    "detail_code_rejected",
    "detail_json_invalid",
    "detail_http_error",
})


def ppt_record_order(record: dict) -> tuple:
    """Stable (created_sec, original_id) order key for ppt records.

    ``original_id`` breaks created_sec ties deterministically; the record's
    own dict identity never leaks into the key so equal records from
    separate runs sort identically.
    """
    try:
        created_sec = int(record.get("created_sec", 0) or 0)
    except (TypeError, ValueError):
        created_sec = 0
    return (created_sec, str(record.get("original_id") or record.get("id") or ""))


class BearerUnavailableError(RuntimeError):
    """The iCourse bearer cannot be extracted from the live session cookies.

    Carries the existing closed catalog code ``catalog_bearer_missing`` so
    the failure classifies honestly instead of folding into
    ``catalog_payload_invalid``.
    """

    code = "catalog_bearer_missing"

    def __init__(self, message: str = "iCourse bearer session is unavailable"):
        super().__init__(message)
        self.catalog_reason = "bearer_unavailable"


def _annotated(exc: BaseException, reason: str) -> BaseException:
    """Attach one closed-set catalog_reason label without changing the type."""
    if reason in _CATALOG_REASONS:
        exc.catalog_reason = reason
    return exc


# 公开只读别名：application 层归一化诊断时复用同一闭集。
CATALOG_DIAGNOSTIC_REASONS = _CATALOG_REASONS


def _closed_code_kind(value: object) -> str:
    """Collapse an observed envelope code into a closed kind (no value kept)."""
    if value == 0:
        return "zero"
    if value == 200 or str(value) == "200":
        return "200"
    return "other"


def _extract_date_from_sub(sub_title: str) -> str | None:
    """Extract YYYY-MM-DD from a sub_title like "2026-03-05第6-8节"."""
    if not sub_title:
        return None
    m = _DATE_FROM_SUB_RE.match(sub_title)
    return m.group(1) if m else None


def _is_mp4_url(value: str) -> bool:
    return urlparse(value).path.lower().endswith(".mp4")


# 月度直播行的闭集证据：state 只来自已映射的状态键，时间只来自既有的
# start/end 文本键族。平台真实行形状（LIVE-DIAG-1 考古）以 sub_status 报
# 状态、以 start_at/begin_time/end_at 报时间，且时间可为纯 HH:MM[:SS]，
# 须与日锚（行级 date → 父日 date → sub_title 前缀）合成；旧键族并存向后
# 兼容。上游 day 容器的其余键名不在闭集内，因此扁平化时保留的父日证据
# 是结构顺序（day_index/row_index），绝不猜读未知字段。
_LIVE_STATE_MAP = {
    "1": "live", "live": "live", "living": "live", "started": "live",
    "0": "upcoming", "upcoming": "upcoming", "waiting": "upcoming", "not_started": "upcoming",
    "2": "ended", "ended": "ended", "finished": "ended", "closed": "ended",
    "offline": "offline",
}
_LIVE_STATE_KEYS = ("sub_status", "live_status", "status", "course_status")
_LIVE_TIME_SOURCES = (
    ("start_at", "start_span"), ("begin_time", "start_span"),
    ("start_time", "start_span"), ("starts_at", "start_span"),
    ("end_at", "end_span"),
    ("end_time", "end_span"), ("ends_at", "end_span"),
)
_PURE_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
_DATE_ANCHOR_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _live_row_state(row: dict) -> str:
    raw_state = ""
    for key in _LIVE_STATE_KEYS:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip().casefold()
        if text:
            raw_state = text
            break
    return _LIVE_STATE_MAP.get(raw_state, "unknown")


def _occurrence_date_anchor(row: dict, day_date: object) -> str:
    """Resolve the closed-set date anchor used to ground pure-time rows."""
    for value in (
        row.get("date"), day_date,
        _extract_date_from_sub(str(row.get("sub_title") or "")),
    ):
        text = str(value or "").strip()
        if _DATE_ANCHOR_RE.fullmatch(text):
            return text
    return ""


# 平台直播流不在月度行扁平字段，而在嵌套 live_url 字典（LIVE-DIAG-1 考古，
# 参考实现同型）：output=教师画面、output_student=学生画面，各含 m3u8 与
# m3u8_audio。默认视角按学生画面→教师画面→音频闭集序取首个可解析流；
# 其余键名/形态一概不猜。
_LIVE_URL_RESOLUTION_ORDER = (
    ("output_student", "m3u8"), ("output", "m3u8"),
    ("output_student", "m3u8_audio"), ("output", "m3u8_audio"),
)

# get-sub-info 业务码闭集（直播两跳探测消费面）：7001=「视频未到开放时间」，
# 学校对新发布内容的 24h 预发布审查闸门。这是平台的确定性答复而非探测失败：
# 不吞成泛化 not_observed，也不绕过闸门（合规红线，与参考实现同界），只以
# 闭集码如实上抛；其余业务码维持既有 FileNotFoundError 契约不变。
_PLAYBACK_GATE_CODES = {7001: "live_playback_not_open"}


def _live_url_stream_url(value: object, view: str = DEFAULT_LIVE_VIEW) -> str:
    """Extract the HLS URL for one closed-set view from a platform ``live_url`` dict.

    默认视角保持既有解析序（学生画面→教师画面→音频回退）；其余闭集视角只做
    严格分支匹配——该视角分支缺失或非 .m3u8 即如实不可用，绝不发明回退。
    """
    if not isinstance(value, dict):
        return ""
    if view != DEFAULT_LIVE_VIEW:
        branch_key, kind = next(
            ((branch, stream) for name, branch, stream in LIVE_VIEWS if name == view),
            ("", ""),
        )
        if not branch_key:
            return ""
        branch = value.get(branch_key)
        if not isinstance(branch, dict):
            return ""
        url = str(branch.get(kind) or "").strip()
        return url if urlparse(url).path.casefold().endswith(".m3u8") else ""
    for branch, kind in _LIVE_URL_RESOLUTION_ORDER:
        entry = value.get(branch)
        if not isinstance(entry, dict):
            continue
        url = str(entry.get(kind) or "").strip()
        if url and urlparse(url).path.casefold().endswith(".m3u8"):
            return url
    return ""


def _parse_occurrence_span(value: object, tz, date_anchor: str = "") -> tuple | None:
    """Parse one closed-set time text into an aware (low, high) span.

    Supports ISO date-time, date-only (the whole local day), epoch
    seconds/milliseconds within a sane era, and pure ``HH:MM[:SS]`` time
    grounded by an explicit YYYY-MM-DD date anchor (the platform's monthly
    rows carry bare times). Unparseable text — including a bare time with
    no anchor — contributes no time evidence (None), never a guess.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        seconds = int(text)
        if seconds >= 1_000_000_000_000:
            seconds //= 1000
        if not 1_000_000_000 <= seconds <= 4_000_000_000:
            return None
        try:
            instant = datetime.fromtimestamp(seconds, tz=tz)
        except (OverflowError, OSError, ValueError):
            return None
        return (instant, instant)
    pure_time = _PURE_TIME_RE.fullmatch(text)
    if pure_time is not None:
        if not _DATE_ANCHOR_RE.fullmatch(date_anchor or ""):
            return None
        hour, minute, second = pure_time.groups()
        try:
            instant = datetime.fromisoformat(
                f"{date_anchor}T{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
            )
        except ValueError:
            return None
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=tz)
        return (instant, instant)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    if len(text) == 10:
        return (
            parsed,
            parsed.replace(hour=23, minute=59, second=59, microsecond=999999),
        )
    return (parsed, parsed)


_LIVE_OBSERVE_LOG_LOCK = threading.Lock()
_LIVE_OBSERVE_LOG = {"code": "", "at": 0.0, "suppressed": 0}
_LIVE_OBSERVE_LOG_WINDOW_SECONDS = 60.0


def _log_live_observe(code: str, detail: str) -> None:
    """观测腿节流闭集日志（每码 60s 至多一行+抑制计数，SRC-SYNDROME-1 追加A）。

    第廿六案教训=观测腿零日志使晨间无法定位失败腿；status 被前端轮询，
    节流防刷屏。detail 只含闭集计数（天数/候选数），绝无课程名/账号/原始载荷。
    """
    now = time.monotonic()
    with _LIVE_OBSERVE_LOG_LOCK:
        same_code = _LIVE_OBSERVE_LOG["code"] == code
        if not same_code or now - _LIVE_OBSERVE_LOG["at"] >= _LIVE_OBSERVE_LOG_WINDOW_SECONDS:
            suppressed = _LIVE_OBSERVE_LOG["suppressed"] if same_code else 0
            suffix = f" suppressed={suppressed}" if suppressed else ""
            print(f"[live] observe code={code} {detail}{suffix}", flush=True)
            _LIVE_OBSERVE_LOG.update(code=code, at=now, suppressed=0)
        else:
            _LIVE_OBSERVE_LOG["suppressed"] += 1


def _collect_live_occurrence_candidates(
    value: object, course_id: str, tz
) -> list[dict]:
    """Flatten one monthly index into course-matched occurrence candidates.

    Parent-day evidence survives flattening as (day_index, row_index) order;
    each candidate keeps its row, closed-set state, and any parseable
    start/end time spans.
    """
    days = value.get("list") or [] if isinstance(value, dict) else []
    candidates: list[dict] = []
    for day_index, day_value in enumerate(days):
        if not isinstance(day_value, dict):
            continue
        day_date = day_value.get("date")
        for row_index, row in enumerate(day_value.get("course") or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or row.get("course_id") or "") != str(course_id):
                continue
            anchor = _occurrence_date_anchor(row, day_date)
            candidate = {
                "row": row,
                "day_index": day_index,
                "row_index": row_index,
                "state": _live_row_state(row),
                "start_span": None,
                "end_span": None,
            }
            for source, target in _LIVE_TIME_SOURCES:
                span = _parse_occurrence_span(row.get(source), tz, anchor)
                if span is not None and candidate[target] is None:
                    candidate[target] = span
            candidates.append(candidate)
    return candidates


def _candidate_occurrence_interval(candidate: dict) -> tuple | None:
    start_span, end_span = candidate["start_span"], candidate["end_span"]
    if start_span is None and end_span is None:
        return None
    low = start_span[0] if start_span is not None else end_span[0]
    high = end_span[1] if end_span is not None else start_span[1]
    return (low, high)


def _select_live_occurrence(candidates: list[dict], now: datetime) -> dict | None:
    """Deterministically pick the current occurrence; None means fail closed.

    One live-state row wins over earlier ended rows; several live rows are
    ambiguous. Otherwise selection prefers the occurrence relevant to now by
    its own time evidence (a row whose span brackets now, then minimal
    interval distance), falling back to preserved parent-day order when no
    row carries parseable times. A selected row never gains a live state it
    did not declare.
    """
    live = [candidate for candidate in candidates if candidate["state"] == "live"]
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        return None

    timed: list[tuple[tuple, dict]] = []
    for candidate in candidates:
        interval = _candidate_occurrence_interval(candidate)
        if interval is not None:
            timed.append((interval, candidate))
    if timed:
        bracketing = [item for item in timed if item[0][0] <= now <= item[0][1]]
        if len(bracketing) == 1:
            return bracketing[0][1]
        if len(bracketing) > 1:
            return None

        def distance(interval: tuple) -> float:
            low, high = interval
            if now < low:
                return (low - now).total_seconds()
            if now > high:
                return (now - high).total_seconds()
            return 0.0

        nearest_distance = min(distance(item[0]) for item in timed)
        nearest = [item[1] for item in timed if distance(item[0]) == nearest_distance]
        if len(nearest) == 1:
            return nearest[0]
        return None

    known = [
        candidate
        for candidate in candidates
        if candidate["state"] in ("upcoming", "ended", "offline")
    ]
    pool = known if known else candidates
    if len(pool) == 1:
        return pool[0]
    states = {candidate["state"] for candidate in pool}
    if states == {"upcoming"}:
        return min(pool, key=lambda item: (item["day_index"], item["row_index"]))
    if states in ({"ended"}, {"offline"}):
        return max(pool, key=lambda item: (item["day_index"], item["row_index"]))
    return None

def fetch_ppt_image(client: "ICourseClient", item: dict,
                    max_attempts: int = 2, timeout: int = 30) -> bytes | None:
    """Download a single PPT image. Returns bytes or None on persistent failure.

    Module-level (not a method on ICourseClient) so worker threads in the
    scheduler can call it without binding the function name at import time —
    that way tests can monkey-patch ``src.icourse.fetch_ppt_image`` and the
    scheduler will pick up the replacement on its next worker invocation.
    """
    url = item["pptimgurl"]
    for attempt in range(1, max_attempts + 1):
        try:
            if url.startswith(config.WEBVPN_BASE):
                resp = client.vpn.get_raw(url, timeout=timeout)
            else:
                resp = client.vpn.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            print(f"[PPTFetcher] download failed (attempt "
                  f"{attempt}/{max_attempts}): {type(exc).__name__}")
            if attempt < max_attempts:
                time.sleep(1)
    return None


class SessionUnverifiable(RuntimeError):
    """SOAK-F1-R：探活请求自身传输失败或网关 5xx——会话死活不可判。

    ac0c7be（SUP1-F1）同族纪律的验证边界版：传输类失败与网关 5xx 对
    「会话是否有效」零证据，把它判死会触发全量重建 = 一次全量 WebVPN
    重登录；夜间网络抖动/网关 502 窗口因此被验证边界放大成重登录风暴。
    判死只留给权威证据（401/403、登录页 200 HTML、坏 code、目录会话
    明确过期）。
    """


class ICourseClient:
    """Client for the iCourse API, operating through WebVPN."""

    CATALOG_TIMEOUT = (5, 15)
    CATALOG_DEADLINE_SECONDS = 90
    CATALOG_MAX_COURSES = 100
    CATALOG_MONTHS_BACK = 12
    CATALOG_MONTHS_AHEAD = 1
    CATALOG_MAX_ROWS_PER_MONTH = 1000
    # 原生文稿会话缓存新鲜窗（F4 增量层）：窗内同讲次全量/增量读直接命中
    # 内存、不再打平台；窗过期后下次读重新外联以发现新段。只服务轻轮询去重，
    # 不承诺实时性（S3 首轮=手动/进页刷新拉取）。
    TRANSCRIPT_CACHE_TTL_SECONDS = 2.0

    def __init__(self, vpn_session: WebVPNSession, catalog_session=None):
        self.vpn = vpn_session
        self.catalog_session = catalog_session
        self.base_url = config.ICOURSE_BASE
        self._userinfo = (
            dict(getattr(catalog_session, "userinfo", {}) or {})
            if catalog_session is not None
            else None
        )
        # 闭集目录诊断（只含计数与类别标签，无任何字段值）。
        self.last_catalog_diagnostics: dict = {}
        self._index_months_ok = 0
        self._index_rows = 0
        # 原生文稿会话缓存：sub_id → (抓取时刻 monotonic, 正典段列表)。
        # 仅存内存、随客户端会话生灭，绝不落盘（隐私最小化）。
        self._transcript_cache: dict[str, tuple[float, list[dict]]] = {}

    def _authorization_headers(self, *, required: bool = False) -> dict[str, str]:
        """Return the in-memory iCourse bearer without persisting or logging it."""
        if self.catalog_session is not None:
            return self.catalog_session.authorization_headers()
        session = getattr(self.vpn, "session", None)
        cookies = getattr(session, "cookies", ())
        for cookie in cookies:
            value = unquote(str(getattr(cookie, "value", "") or ""))
            match = _SESSION_TOKEN_RE.search(value)
            if match is None:
                continue
            token = match.group(1)
            if 16 <= len(token) <= 4096 and "\r" not in token and "\n" not in token:
                return {"Authorization": f"Bearer {token}"}
        if required:
            raise BearerUnavailableError()
        return {}

    def get_userinfo(self) -> dict:
        """Get current user info (id, tenant_id, phone, account).

        Caches the result for the session.
        """
        if self._userinfo is not None:
            return self._userinfo

        url = f"{self.base_url}/userapi/v1/infosimple"
        resp = self.vpn.get(url, timeout=self.CATALOG_TIMEOUT)
        try:
            resp.raise_for_status()
            data = resp.json()
        except ValueError as exc:
            raise _annotated(exc, "userinfo_rejected") from exc

        if data.get("code") not in (0, 200):
            raise _annotated(
                RuntimeError(f"Failed to get userinfo: {data.get('msg')}"),
                "userinfo_rejected",
            )

        self._userinfo = data.get("params") or data.get("data", {})
        return self._userinfo

    def check_alive(self) -> bool:
        """Quick session health check (non-cached).

        F1-R 闭集三态：True=权威存活；False=权威死亡（非 200、坏 code、
        登录页 HTML、目录会话明确过期）；探活自身传输失败或网关 5xx 抛
        SessionUnverifiable——调用方必须保留会话而不是重建。
        """
        try:
            resp = self.vpn.get(
                f"{self.base_url}/userapi/v1/infosimple", timeout=10
            )
        except requests.RequestException as exc:
            raise SessionUnverifiable("probe transport failure") from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 500:
            raise SessionUnverifiable(f"probe gateway status {status}")
        try:
            webvpn_ready = status == 200 and resp.json().get("code") in (0, 200)
        except Exception:
            # 200-非 JSON 载荷（登录页 HTML 等）=权威死亡证据。
            webvpn_ready = False
        if self.catalog_session is None:
            return webvpn_ready
        try:
            catalog_ready = bool(self.catalog_session.check_alive())
        except requests.RequestException as exc:
            raise SessionUnverifiable("catalog probe transport failure") from exc
        except Exception:
            catalog_ready = False
        return webvpn_ready and catalog_ready

    def with_catalog_session(self, catalog_session) -> "ICourseClient":
        """Bind a direct transport to this verified session without replaying CAS."""
        catalog_session.adopt_authorization(
            self._authorization_headers(required=True),
            self.get_userinfo(),
        )
        return ICourseClient(self.vpn, catalog_session)

    def sign_video_url(
        self, video_url: str, now: int | None = None
    ) -> str:
        """Sign a video URL with CDN authentication parameters.

        Adds clientUUID and t parameters required for video download.
        The t parameter format: {user_id}-{timestamp}-{md5_hash}
        where md5_hash = md5(pathname + user_id + tenant_id + reversed_phone + timestamp)
        """
        userinfo = self.get_userinfo()
        user_id = userinfo.get("id", "")
        tenant_id = userinfo.get("tenant_id", "")
        phone = str(userinfo.get("phone", ""))

        if now is None:
            now = int(time.time())

        reversed_phone = phone[::-1]
        pathname = urlparse(video_url).path

        hash_input = f"{pathname}{user_id}{tenant_id}{reversed_phone}{now}"
        md5_hash = hashlib.md5(hash_input.encode()).hexdigest()
        t_param = f"{user_id}-{now}-{md5_hash}"

        client_uuid = str(uuid.uuid4())
        sep = "&" if "?" in video_url else "?"
        return f"{video_url}{sep}clientUUID={client_uuid}&t={t_param}"

    def get_course_detail(self, course_id: str, *, student: str = "") -> dict:
        """Get course details including title, teacher, and lecture list.

        Returns dict with keys: title, teacher, lectures
        Each lecture has: sub_id, sub_title, lecturer_name, date, has_playback
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-course-detail"
        params = {"course_id": course_id}
        if student:
            params["student"] = student
        transport = self.catalog_session if student and self.catalog_session is not None else self.vpn
        headers = self._authorization_headers(required=True) if transport is self.catalog_session else None
        request_options = {"params": params, "timeout": self.CATALOG_TIMEOUT}
        if headers is not None:
            request_options["headers"] = headers
        resp = transport.get(url, **request_options)
        try:
            resp.raise_for_status()
        except requests.Timeout:
            raise
        except requests.RequestException as exc:
            raise _annotated(exc, "detail_http_error") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise _annotated(exc, "detail_json_invalid") from exc

        if data.get("code") != 0:
            error = RuntimeError(
                f"API error for course {course_id}: {data.get('msg')}"
            )
            error.catalog_reason = "detail_code_rejected"
            error.catalog_code_kind = _closed_code_kind(data.get("code"))
            raise error

        course_data = data.get("data", {})
        title = course_data.get("title", "Unknown")
        teacher = course_data.get("realname", "Unknown")

        # Parse the nested sub_list: {year: {month: {day: [items]}}}
        lectures = []
        sub_list = course_data.get("sub_list", {})
        for year, months in sub_list.items():
            for month, days in months.items():
                for day, items in days.items():
                    for item in items:
                        if "id" in item:
                            sub_title = item.get("sub_title", "")
                            # Real lecture date is embedded in sub_title
                            # ("2026-03-05第6-8节" → "2026-03-05"); fall back
                            # to the server's year/month/day keys if missing.
                            # Zero-pad the fallback so SQLite ORDER BY works.
                            date = (
                                _extract_date_from_sub(sub_title)
                                or f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                            )
                            lectures.append(
                                {
                                    "sub_id": item["id"],
                                    "sub_title": sub_title,
                                    "lecturer_name": item.get(
                                        "lecturer_name", ""
                                    ),
                                    "date": date,
                                    "has_playback": str(item.get("playback_status")) == "1",
                                }
                            )

        return {"title": title, "teacher": teacher, "lectures": lectures}

    def iter_ppt_records(
        self,
        course_id: str,
        sub_id: str,
        per_page: int = PPT_PAGE_SIZE,
        *,
        max_records: int = PPT_RECORD_STORM_LIMIT,
        max_page_bytes: int = PPT_RESPONSE_MAX_BYTES,
    ):
        """Yield validated pptimgurl records while streaming search-ppt pages.

        Pagination stops on the server's empty or short page. Three bounded
        guards protect against runaway sources without imposing a low page
        cap: each response body is size-checked, a repeated page row-id
        signature (a stuck server cursor) fails closed, and a gross record
        count signals an event storm. Every failure raises
        :class:`PptListError` with a closed-set code — the tail is never
        silently discarded.
        """
        if per_page < 1 or max_records < 1 or max_page_bytes < 1:
            raise PptListError("ppt_payload_invalid", "ppt list bounds are invalid")
        seen_signatures: set[tuple[str, ...]] = set()
        emitted = 0
        page = 1
        while True:
            url = f"{self.base_url}/pptnote/v1/schedule/search-ppt"
            resp = self.vpn.get(
                url,
                params={
                    "course_id": course_id, "sub_id": sub_id,
                    "page": page, "per_page": per_page,
                },
            )
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            body = resp.content if hasattr(resp, "content") else b""
            if (declared and declared.isdigit() and int(declared) > max_page_bytes) or len(body) > max_page_bytes:
                raise PptListError("ppt_response_too_large")
            try:
                data = resp.json()
            except ValueError as exc:
                raise PptListError("ppt_payload_invalid") from exc
            if not isinstance(data, dict) or data.get("code") != 0:
                raise PptListError("ppt_payload_invalid")
            page_items = data.get("list")
            if not isinstance(page_items, list):
                raise PptListError("ppt_payload_invalid")
            if not page_items:
                break
            signature = tuple(
                str(row.get("id") if isinstance(row, dict) else "")
                for row in page_items
            )
            if signature in seen_signatures:
                raise PptListError("ppt_pagination_stalled")
            seen_signatures.add(signature)
            for raw in page_items:
                if not isinstance(raw, dict):
                    continue
                try:
                    content = json.loads(raw.get("content", "{}"))
                except (ValueError, TypeError):
                    continue
                if not isinstance(content, dict):
                    continue
                img_url = content.get("pptimgurl")
                if not img_url:
                    continue
                original_id = str(raw.get("id") or "")
                yield {
                    "id": raw.get("id"),
                    "original_id": original_id,
                    "pptimgurl": img_url,
                    "pptthumb": content.get("pptthumb", ""),
                    "created_sec": int(raw.get("created_sec", 0) or 0),
                    "created_ms": int(content.get("created", 0) or 0),
                    "taskid": content.get("taskid", ""),
                }
                emitted += 1
                if emitted >= max_records:
                    raise PptListError("ppt_record_storm")
            if len(page_items) < per_page:
                break
            page += 1

    def get_ppt_list(self, course_id: str, sub_id: str,
                     per_page: int = PPT_PAGE_SIZE) -> list[dict]:
        """Fetch PPT screenshot list for a lecture.

        Walks pagination via :meth:`iter_ppt_records` until exhausted.
        Returns a flat list of items, each:
            {
              "id": int,                # row id
              "original_id": str,       # stable tie-break key
              "pptimgurl": str,         # full image URL (used for OCR)
              "pptthumb": str,          # thumbnail URL (kept for reference)
              "created_sec": int,       # offset within lecture, in seconds
              "created_ms": int,        # original epoch ms timestamp
              "taskid": str,
            }
        Sorted by (created_sec, original_id) — capture time first, row id
        as the deterministic tie-break.
        """
        items = list(self.iter_ppt_records(course_id, sub_id, per_page))
        items.sort(key=ppt_record_order)
        return items

    def get_course_list(
        self, term: str = "24", page: int = 1, per_page: int = 20
    ) -> dict:
        """Get a paginated list of courses for a given term.

        Returns dict with keys: total, courses (list of course dicts).
        Empty-string filter params are omitted so the API returns all
        courses rather than searching for "".
        """
        url = f"{self.base_url}/portal/courseapi/v3/multi-search/get-course-list"
        # Omitting empty-string params matters — some backends treat
        # ``title=""`` as "search for nothing" rather than "no filter".
        params: dict[str, str | int] = {
            "tenant": config.TENANT_CODE,
            "term": term,
            "page": page,
            "per_page": per_page,
        }
        for key in ("title", "kkxy_code", "course_type", "course_student_type"):
            val = getattr(config, key.upper(), "") if key.isupper() else ""
            if not val:
                continue
            params[key] = val
        resp = self.vpn.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"API error: {data.get('msg')}")

        result = data.get("data", {})
        return {
            "total": int(result.get("total", 0)),
            "courses": result.get("list", []),
        }

    def discover_terms(self, code_min: int = 10,
                       code_max: int = 35) -> list[dict]:
        """Scan term codes to discover all available semesters.

        Returns ``[{code, name, count}]`` sorted by code descending
        (newest first).  Only codes returning >0 courses are included.
        """
        results: list[dict] = []
        for code in range(code_min, code_max + 1):
            try:
                resp = self.get_course_list(
                    term=str(code), page=1, per_page=1,
                )
                total = resp.get("total", 0)
                if not total:
                    continue
                courses = resp.get("courses", [])
                name = (courses[0].get("term_name") if courses else None) or str(code)
                results.append({"code": str(code), "name": name,
                                "count": total})
            except Exception:
                continue
        return sorted(results, key=lambda x: -int(x["code"]))

    def list_semester_courses(self, term: str,
                              per_page: int = 500) -> list[dict]:
        """Walk every page of get-course-list for ``term``.

        Uses ``total`` from the first response to compute the exact page
        count — no hard-coded max.  (Caller must ensure the API hasn't
        silently capped ``per_page`` below the requested value.)

        Returns a flat list of ``{course_id, title, teacher, dept}`` dicts,
        deduped by course_id.
        """
        import math

        out: list[dict] = []
        seen: set[str] = set()

        # Page 1 — discover total
        result = self.get_course_list(
            term=term, page=1, per_page=per_page,
        )
        total_expected = result.get("total") or 0
        if not total_expected:
            return out
        total_pages = max(1, math.ceil(total_expected / per_page))

        def _process(page_items):
            for raw in page_items:
                cid = raw.get("id") or raw.get("course_id")
                if not cid:
                    continue
                cid = str(cid)
                if cid in seen:
                    continue
                seen.add(cid)
                dept = (
                    raw.get("kkxy_name") or raw.get("school_name")
                    or raw.get("dept_name") or raw.get("kkxy") or None
                )
                out.append({
                    "course_id": cid,
                    "title": raw.get("title") or "",
                    "teacher": raw.get("realname") or raw.get("teacher") or "",
                    "dept": dept,
                })

        page_items = result.get("courses", [])
        if not page_items:
            return out
        _process(page_items)

        # Remaining pages 2 .. total_pages
        for page in range(2, total_pages + 1):
            result = self.get_course_list(
                term=term, page=page, per_page=per_page,
            )
            page_items = result.get("courses", [])
            if not page_items:
                break
            _process(page_items)

        return out

    def list_user_courses(
        self,
        max_courses: int | None = None,
        *,
        today: date | None = None,
    ) -> list[dict]:
        """Return courses from the Bearer-scoped rolling personal schedule."""
        course_limit = int(max_courses or self.CATALOG_MAX_COURSES)
        if course_limit < 1 or course_limit > self.CATALOG_MAX_COURSES:
            raise ValueError("user course limit is out of bounds")
        user = self.get_userinfo()
        user_id = user.get("id")
        account = str(user.get("account") or "").strip()
        tenant_id = user.get("tenant_id") or config.TENANT_CODE
        if not user_id or not account or not tenant_id:
            raise _annotated(
                RuntimeError("iCourse user identity is incomplete"),
                "identity_incomplete",
            )

        courses: list[dict] = []
        seen: set[str] = set()
        by_course: dict[str, dict] = {}
        self._index_months_ok = 0
        self._index_rows = 0
        anchor = today or date.today()
        month_index = anchor.year * 12 + anchor.month - 1
        months = [
            f"{value // 12:04d}-{value % 12 + 1:02d}"
            for value in range(
                month_index - self.CATALOG_MONTHS_BACK,
                month_index + self.CATALOG_MONTHS_AHEAD + 1,
            )
        ]
        for month in months:
            transport = self.catalog_session or self.vpn
            # direct 会话的强制点其实在 catalog_session.authorization_headers()
            # （空 bearer 即抛 catalog_bearer_missing）；这里的 required 只决定
            # webvpn jar 扫描缺失时是省略还是抛错。
            response = transport.get(
                f"{self.base_url}/courseapi/v2/course-live/get-my-course-month",
                params={"month": month},
                # WebVPN 会话 cookie 已获上游验证足以访问目录（2026-09-07 有界
                # 探针：索引/详情 no-Authorization 均成功且零 _token Cookie），
                # 故仅 direct 会话（无会话 cookie，只能靠 bearer）强制提取；
                # webvpn 路径在旧格式载体仍存在时照样附带，缺失时省略。
                headers=self._authorization_headers(
                    required=self.catalog_session is not None
                ),
                timeout=self.CATALOG_TIMEOUT,
            )
            try:
                response.raise_for_status()
            except requests.Timeout:
                raise
            except requests.RequestException as exc:
                raise _annotated(exc, "index_code_rejected") from exc
            try:
                value = response.json()
            except ValueError as exc:
                raise _annotated(exc, "index_payload_shape_invalid") from exc
            if not isinstance(value, dict) or value.get("code") not in (0, 200):
                raise _annotated(
                    RuntimeError("iCourse user course index rejected the request"),
                    "index_code_rejected",
                )
            days = value.get("list") or []
            if not isinstance(days, list) or len(days) > 31:
                raise _annotated(
                    RuntimeError("iCourse user course index returned an invalid payload"),
                    "index_payload_shape_invalid",
                )
            rows = [
                row
                for day_value in days
                if isinstance(day_value, dict)
                for row in day_value.get("course") or []
                if isinstance(row, dict)
            ]
            if len(rows) > self.CATALOG_MAX_ROWS_PER_MONTH:
                raise _annotated(
                    RuntimeError("iCourse user course index exceeded the row limit"),
                    "index_row_limit_exceeded",
                )
            self._index_months_ok += 1
            self._index_rows += len(rows)
            for raw in rows:
                course_id = str(raw.get("id") or raw.get("course_id") or "").strip()
                if not course_id:
                    continue
                if course_id not in seen:
                    if len(courses) >= course_limit:
                        raise _annotated(
                            RuntimeError("iCourse user course index exceeded the course limit"),
                            "index_course_limit_exceeded",
                        )
                    seen.add(course_id)
                    candidate = {
                        "course_id": course_id,
                        "title": str(raw.get("title") or ""),
                        "teacher": str(raw.get("realname") or raw.get("lecturer_name") or ""),
                        "department": str(
                            raw.get("kkxy_name") or raw.get("school_name")
                            or raw.get("dept_name") or raw.get("kkxy") or ""
                        ),
                        "term": str(raw.get("term_name") or raw.get("term") or ""),
                        "student": account,
                        "_lecture_durations": {},
                    }
                    courses.append(candidate)
                    by_course[course_id] = candidate
                sub_id = str(raw.get("sub_id") or "").strip()
                try:
                    duration = max(0.0, float(raw.get("sub_duration") or 0.0))
                except (TypeError, ValueError):
                    duration = 0.0
                if sub_id and duration > 0:
                    by_course[course_id]["_lecture_durations"][sub_id] = duration
        return courses

    def get_live_course_observation(self, course_id: str, *, now: datetime | None = None) -> dict:
        """Return a conservative live observation from the authorized monthly index."""
        current = now or datetime.now().astimezone()
        transport = self.catalog_session or self.vpn
        response = transport.get(
            f"{self.base_url}/courseapi/v2/course-live/get-my-course-month",
            params={"month": current.strftime("%Y-%m")},
            headers=self._authorization_headers(
                required=self.catalog_session is not None
            ),
            timeout=self.CATALOG_TIMEOUT,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("code") not in (0, 200):
            _log_live_observe("live_index_rejected", "route=observe")
            raise RuntimeError("iCourse live index rejected the request")
        candidates = _collect_live_occurrence_candidates(
            value, course_id, current.tzinfo
        )
        observed_at = time.time()
        if not candidates:
            days = len(value.get("list") or []) if isinstance(value, dict) else 0
            _log_live_observe("live_schedule_not_observed", f"route=observe days={days}")
            return {
                "state": "unknown", "observed_at": observed_at,
                "expires_at": observed_at + 20, "code": "live_schedule_not_observed",
            }
        selected = _select_live_occurrence(candidates, current)
        if selected is None:
            _log_live_observe(
                "live_occurrence_ambiguous", f"route=observe candidates={len(candidates)}"
            )
            return {
                "state": "unknown", "observed_at": observed_at,
                "expires_at": observed_at + 20, "code": "live_occurrence_ambiguous",
            }
        row = selected["row"]
        state = selected["state"]
        result = {
            "state": state,
            "observed_at": observed_at,
            "expires_at": observed_at + 20,
            "code": f"live_{state}_observed",
        }
        # 视角元数据（URL-free 闭集 id，直播二期乙1）：与取流严格分支同源派生；
        # 行上无嵌套 live_url 时不出键，前端按「仅默认视角」理解。
        views = available_views_from_live_url(row.get("live_url"))
        if views:
            result["available_views"] = list(views)
        for source, target in (
            ("start_at", "starts_at"), ("begin_time", "starts_at"),
            ("start_time", "starts_at"), ("starts_at", "starts_at"),
            ("end_at", "ends_at"),
            ("end_time", "ends_at"), ("ends_at", "ends_at"),
        ):
            text = str(row.get(source) or "").strip()
            if text and target not in result:
                result[target] = text
        return result

    def get_live_stream_params(
        self, course_id: str, *, view: str = DEFAULT_LIVE_VIEW
    ) -> tuple[object, str, dict[str, str]]:
        """Resolve one HLS URL without returning it outside the local process.

        U17③：取流链的裸异常（requests 网络族/FileNotFoundError/PermissionError）
        统一收编进 LiveRoomError 闭集码族——HTTP 层逐字透传 error_code，
        绝不让 500 runtime_failed 吃掉真实原因。7001 闭集码原样穿透。
        视角维度（直播二期乙1）：默认视角保持既有解析序不变（嵌套字典解析序、
        legacy 平键、两跳探测全保留）；其余闭集视角只对月度行嵌套 live_url 做
        严格分支匹配——分支缺失即如实不可用（live_stream_unavailable），
        绝不发明回退，与 available_views 派生同源同界。
        """
        if str(view or "").strip() not in LIVE_VIEW_IDS:
            raise LiveRoomError("live_view_unknown", 400)
        try:
            return self._resolve_live_stream_params(course_id, view=view)
        except LiveRoomError:
            raise
        except FileNotFoundError as exc:
            print("[live] failure_code=live_stream_unavailable status=502 route=stream-params", flush=True)
            raise LiveRoomError("live_stream_unavailable", 502) from exc
        except PermissionError as exc:
            print("[live] failure_code=live_stream_unavailable status=502 route=stream-params", flush=True)
            raise LiveRoomError("live_stream_unavailable", 502) from exc
        except requests.RequestException as exc:
            print("[live] failure_code=live_upstream_unreachable status=502 route=stream-params", flush=True)
            raise LiveRoomError("live_upstream_unreachable", 502) from exc

    def _resolve_live_stream_params(
        self, course_id: str, *, view: str = DEFAULT_LIVE_VIEW
    ) -> tuple[object, str, dict[str, str]]:
        """Resolve one HLS URL without returning it outside the local process.

        Stream-source tiers, most- to least-preferred: the selected monthly
        row's nested ``live_url`` dict (the platform's native shape), the
        legacy flat row keys (backward compatibility), then a read-only
        two-hop probe through course detail -> get-sub-info, where the
        reference platform player actually hangs the live stream. The flat
        keys and the two-hop probe carry a single default-view stream, so
        non-default closed-set views stop after the nested branch match.
        """
        current = datetime.now().astimezone()
        transport = self.catalog_session or self.vpn
        response = transport.get(
            f"{self.base_url}/courseapi/v2/course-live/get-my-course-month",
            params={"month": current.strftime("%Y-%m")},
            headers=self._authorization_headers(
                required=self.catalog_session is not None
            ),
            timeout=self.CATALOG_TIMEOUT,
        )
        response.raise_for_status()
        value = response.json()
        candidates = _collect_live_occurrence_candidates(
            value, course_id, current.tzinfo
        )
        if not candidates:
            raise FileNotFoundError("live course was not observed")
        selected = _select_live_occurrence(candidates, current)
        if selected is None:
            raise FileNotFoundError("live occurrence was ambiguous")
        row = selected["row"]
        url = _live_url_stream_url(row.get("live_url"), view)
        if not url and view == DEFAULT_LIVE_VIEW:
            url_candidates = [
                str(row.get(name) or "").strip()
                for name in ("hls_url", "live_hls_url", "play_url", "stream_url")
            ]
            url = next((item for item in url_candidates if item and urlparse(item).path.casefold().endswith(".m3u8")), "")
        if not url and view == DEFAULT_LIVE_VIEW:
            url = self._live_stream_url_from_sub_info(course_id, current)
        if not url:
            raise FileNotFoundError("live HLS was not observed")
        headers = self._authorization_headers(
            required=self.catalog_session is not None
        )
        headers.update({
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        })
        return transport, url, headers

    def _live_stream_url_from_sub_info(self, course_id: str, current: datetime) -> str:
        """Read-only two-hop fallback: course detail -> get-sub-info.

        The reference platform player resolves live streams from the lecture
        detail (``sub_list`` -> ``get-sub-info``), not the monthly index.
        Exactly one lecture — today's, else the nearest by date — is probed;
        a present ``sub_status`` must map to live before the nested
        ``live_url`` is trusted. Any probe failure resolves to "" so the
        stream jump keeps its closed FileNotFoundError contract.  A platform
        7001 pre-release-gate answer is definitive instead: it surfaces as
        the closed ``live_playback_not_open`` error, never a generic miss.
        """
        try:
            detail = self.get_course_detail(course_id)
            today = current.date()
            dated = []
            for lecture in detail.get("lectures") or []:
                sub_id = str(lecture.get("sub_id") or "").strip()
                try:
                    lecture_date = date.fromisoformat(
                        str(lecture.get("date") or "").strip()
                    )
                except ValueError:
                    continue
                if sub_id:
                    dated.append((abs((lecture_date - today).days), lecture_date, sub_id))
            if not dated:
                return ""
            dated.sort()
            info = self.get_sub_info(course_id, dated[0][2])
            raw_status = info.get("sub_status")
            if raw_status is not None and str(raw_status).strip():
                state = _LIVE_STATE_MAP.get(str(raw_status).strip().casefold(), "unknown")
                if state != "live":
                    return ""
            gate_code = _PLAYBACK_GATE_CODES.get(info.get("_api_code"))
            if gate_code:
                raise LiveRoomError(gate_code, 423)
            return _live_url_stream_url(info.get("live_url"))
        except LiveRoomError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError):
            return ""

    def close(self) -> None:
        try:
            # WebVPNSession.close() 同时关闭 requests 会话与持久 curl ticket
            # transport（幂等且不抛出）；对老式 session 对象退回直接 close。
            vpn_close = getattr(self.vpn, "close", None)
            if callable(vpn_close):
                vpn_close()
            else:
                self.vpn.session.close()
        finally:
            if self.catalog_session is not None:
                self.catalog_session.close()

    def list_authorized_courses(
        self,
        *,
        deadline_seconds: float | None = None,
    ) -> list[dict]:
        """Load the identity-scoped index and verify every returned course detail."""
        authorized: list[dict] = []
        seen: set[str] = set()
        verification_failures = 0
        detail_attempted = 0
        candidates: list[dict] | None = None
        fail_buckets: dict[str, int] = {}
        code_kinds: dict[str, int] = {}
        deadline_targets = [
            item for item in (self.vpn, self.catalog_session) if item is not None
        ]
        for target in deadline_targets:
            begin_deadline = getattr(target, "begin_request_deadline", None)
            if callable(begin_deadline):
                begin_deadline(
                    max(1.0, float(deadline_seconds or self.CATALOG_DEADLINE_SECONDS))
                )
        try:
            candidates = self.list_user_courses()
            for candidate in candidates:
                course_id = str(candidate.get("course_id") or "").strip()
                if not course_id or course_id in seen:
                    continue
                detail_attempted += 1
                try:
                    detail = self.get_course_detail(
                        course_id,
                        student=str(candidate.get("student") or ""),
                    )
                except Exception as exc:
                    verification_failures += 1
                    bucket, kind = self._classify_detail_failure(exc)
                    fail_buckets[bucket] = fail_buckets.get(bucket, 0) + 1
                    if kind:
                        code_kinds[kind] = code_kinds.get(kind, 0) + 1
                    continue
                seen.add(course_id)
                durations = dict(candidate.get("_lecture_durations") or {})
                lectures = [
                    {
                        **lecture,
                        **(
                            {"duration_seconds": durations[str(lecture.get("sub_id"))]}
                            if str(lecture.get("sub_id")) in durations
                            else {}
                        ),
                    }
                    for lecture in detail.get("lectures") or []
                ]
                authorized.append({
                    "course_id": course_id,
                    "title": detail.get("title") or candidate.get("title") or course_id,
                    "teacher": detail.get("teacher") or candidate.get("teacher") or "",
                    "department": candidate.get("department") or "",
                    "term": candidate.get("term") or "",
                    "authorization_state": "verified",
                    "lectures": lectures,
                })
            if candidates and not authorized and verification_failures:
                raise RuntimeError("iCourse course detail verification failed")
        finally:
            self.last_catalog_diagnostics = {
                "months_ok": max(0, int(self._index_months_ok)),
                "months_total": self.CATALOG_MONTHS_BACK + self.CATALOG_MONTHS_AHEAD + 1,
                "rows": max(0, int(self._index_rows)),
                "candidates": len(candidates) if candidates else 0,
                "detail_attempted": detail_attempted,
                "detail_ok": max(0, detail_attempted - verification_failures),
                "detail_failed": verification_failures,
                **dict(sorted(fail_buckets.items())),
                **{
                    f"code_kind_{name}": count
                    for name, count in sorted(code_kinds.items())
                },
            }
            for target in deadline_targets:
                end_deadline = getattr(target, "end_request_deadline", None)
                if callable(end_deadline):
                    end_deadline()
        return authorized

    @staticmethod
    def _classify_detail_failure(exc: BaseException) -> tuple[str, str]:
        """Map one detail verification failure into closed-set buckets."""
        reason = str(getattr(exc, "catalog_reason", "") or "")
        kind = str(getattr(exc, "catalog_code_kind", "") or "")
        if isinstance(exc, requests.Timeout):
            return "detail_fail_transport_timeout", ""
        # Reason before RequestException: requests' JSONDecodeError is both a
        # ValueError (annotated detail_json_invalid) and a RequestException,
        # and must classify by its reason, not as a transport failure.
        if reason == "detail_code_rejected":
            return "detail_fail_code_rejected", kind or "other"
        if reason == "detail_json_invalid":
            return "detail_fail_json", ""
        if reason == "detail_http_error":
            return "detail_fail_http", ""
        if isinstance(exc, requests.RequestException):
            return "detail_fail_transport", ""
        if isinstance(exc, (AttributeError, TypeError, KeyError, IndexError)):
            return "detail_fail_shape", ""
        return "detail_fail_other", ""

    def get_lecture_detail(self, course_id: str, sub_id: str) -> dict:
        """Get details for a specific lecture, including video URL info.

        The video URL is typically embedded in the course detail's sub_list
        items. This method retrieves the full course detail and finds the
        matching lecture by sub_id.
        """
        detail = self.get_course_detail(course_id)
        for lecture in detail["lectures"]:
            if str(lecture["sub_id"]) == str(sub_id):
                return lecture
        raise ValueError(
            f"Lecture {sub_id} not found in course {course_id}"
        )

    def get_transcript(self, sub_id: str) -> str | None:
        """Get the transcript text for a lecture (flat string).

        Returns the full transcript text, empty string if no transcript,
        or None on error.
        """
        segments = self.get_transcript_segments(sub_id)
        if segments is None:
            return None
        if not segments:
            return ""
        return " ".join(s["text"] for s in segments if s["text"])

    def get_transcript_segments(
        self, sub_id: str, *, since_ms: int | None = None
    ) -> list[dict] | None:
        """Get transcript as timed segments.  Returns None on API error,
        empty list if no transcript exists.

        Each segment: {"start_ms": int, "end_ms": int, "text": str}
        Sorted by start_ms ascending.  Every call returns fresh segment
        copies, so callers may mutate the result without touching the
        session cache.

        ``since_ms`` turns the read into an incremental one: only segments
        with ``start_ms >= since_ms`` come back — inclusive on purpose, so a
        still-growing tail segment is re-delivered with its newer text and
        callers replace rows by ``start_ms`` identity.  The platform
        endpoint has no server-side watermark (params stay sub_id+format,
        zero new external hosts); repeated calls with the previous tail's
        ``start_ms`` walk the transcript forward.

        Reads are answered from the in-memory per-sub_id cache while it is
        younger than TRANSCRIPT_CACHE_TTL_SECONDS (full and incremental
        alike), otherwise the endpoint is hit once and the cache refreshed.
        A failed fetch returns None and leaves any previous cache entry
        untouched.  The cache lives only for this client session and is
        never persisted.
        """
        key = str(sub_id)
        now = time.monotonic()
        cached = self._transcript_cache.get(key)
        if cached is not None and now - cached[0] <= self.TRANSCRIPT_CACHE_TTL_SECONDS:
            segments = cached[1]
        else:
            fetched = self._fetch_transcript_segments(key)
            if fetched is None:
                return None
            self._transcript_cache[key] = (now, fetched)
            segments = fetched
        if since_ms is None:
            return [dict(segment) for segment in segments]
        watermark = int(since_ms)
        return [
            dict(segment) for segment in segments
            if segment["start_ms"] >= watermark
        ]

    def _fetch_transcript_segments(self, sub_id: str) -> list[dict] | None:
        url = f"{self.base_url}/courseapi/v3/web-socket/search-trans-result"
        resp = self.vpn.get(
            url, params={"sub_id": sub_id, "format": "json"}
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return None

        result_list = data.get("list", [])
        if not result_list:
            return []

        all_content = result_list[0].get("all_content", [])
        if not all_content:
            return []

        return sorted(
            (
                {
                    "start_ms": int(seg.get("BeginSec", 0)) * 1000,
                    "end_ms": int(seg.get("EndSec", seg.get("BeginSec", 0))) * 1000,
                    "text": seg.get("Text", ""),
                }
                for seg in all_content
                if seg.get("Text", "").strip()
            ),
            key=lambda s: s["start_ms"],
        )

    def get_sub_detail(self, course_id: str, sub_id: str) -> dict:
        """Get detailed info for a specific lecture (unsigned URL).

        Returns the full sub-detail data from the API.
        Note: The video URL returned here is NOT signed for CDN auth.
        Use get_sub_info() instead for a signed/downloadable URL.
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-sub-detail"
        resp = self.vpn.get(url, params={
            "course_id": course_id, "sub_id": sub_id
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"API error for sub {sub_id}: {data.get('msg')}"
            )

        return data.get("data", {})

    def get_sub_info(self, course_id: str, sub_id: str) -> dict:
        """Get lecture info including video URLs and timestamp.

        Returns the data payload from the API.

        Non-zero API codes that still ship a populated data payload
        (notably 7001 "视频未到开放时间", the school's 24h pre-release
        review gate) are returned as partial data so the caller can
        extract the video URL from nested content.playback.url — the
        gate scrubs top-level video_list/playurl but not the nested
        URL.  Only raises on HTTP failure or an entirely empty payload.
        """
        url = (
            f"{self.base_url}"
            f"/courseapi/v3/portal-home-setting/get-sub-info"
        )
        resp = self.vpn.get(url, params={
            "course_id": course_id, "sub_id": sub_id
        })
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("data") or {}

        if data.get("code") != 0 and not payload:
            raise RuntimeError(
                f"API error for sub-info {sub_id}: {data.get('msg')}"
            )

        payload["_api_code"] = data.get("code")
        payload["_api_msg"] = data.get("msg", "")
        return payload

    def get_video_url(self, course_id: str, sub_id: str) -> str | None:
        """Get a signed MP4 video URL for a specific lecture.

        Cascades through URL sources, most- to least-preferred:
          1. info.video_list[*].preview_url     — healthy lecture
          2. info.playurl[*]                    — healthy alternate
          3. info.content.playback.url          — review-gated (no extra call)
          4. get-sub-detail content.playback.url — last resort

        Sources 3 and 4 cover the school's pre-release review gate
        (sub-info code 7001 "视频未到开放时间"), which scrubs top-level
        video_list/playurl but leaves the URL in nested fields.  The
        CDN itself does not enforce the gate, so a signed URL from
        either source downloads successfully.

        Returns the signed video URL string, or None if no source yields one.
        """
        try:
            info = self.get_sub_info(course_id, sub_id)
        except Exception as exc:
            print(
                f"    sub-info unavailable ({type(exc).__name__}); "
                "falling back to sub-detail"
            )
            info = {}

        # Get server timestamp for signing
        now = info.get("now")
        if isinstance(now, str):
            now = int(now)

        # Extract base video URL from playurl dict or video_list
        base_url = None

        # Try video_list first (has preview_url without /0/ prefix)
        video_list = info.get("video_list", {})
        if isinstance(video_list, dict):
            for _, v in video_list.items():
                if isinstance(v, dict):
                    preview = v.get("preview_url")
                    if isinstance(preview, str) and _is_mp4_url(preview):
                        base_url = preview
                        break

        # Fallback: try playurl dict (has /0/ prefix, may need stripping)
        if not base_url:
            playurl = info.get("playurl", {})
            if isinstance(playurl, dict):
                for k, v in playurl.items():
                    if k == "now":
                        continue
                    if isinstance(v, str) and _is_mp4_url(v):
                        base_url = v
                        break

        # Review-gate fallback: nested content.playback.url is preserved
        # even when code == 7001 scrubs the top-level fields above.
        if not base_url:
            playback = (info.get("content") or {}).get("playback") or {}
            nested = playback.get("url")
            if isinstance(nested, str) and _is_mp4_url(nested):
                base_url = nested
                if not now:
                    content_now = (info.get("content") or {}).get("now")
                    if isinstance(content_now, (int, str)):
                        now = int(content_now)

        # Last resort: hit get-sub-detail (gate-free) directly.
        if not base_url:
            try:
                detail = self.get_sub_detail(course_id, sub_id)
                content = detail.get("content", {})
                playback = content.get("playback", {})
                if playback and playback.get("url"):
                    base_url = playback["url"]
            except Exception:
                pass

        if not base_url:
            print("    No video URL found in the authorized media response")
            return None

        return self.sign_video_url(base_url, now=now)

    def get_stream_params(self, video_url: str) -> tuple[str, str]:
        """Get the authorized media URL and HTTP headers for remote streaming.

        Returns:
            ``(media_url, http_headers)`` for the authenticated stream request.
        """
        cookies = "; ".join(
            f"{c.name}={c.value}" for c in self.vpn.session.cookies
        )
        headers = (
            f"Cookie: {cookies}\r\n"
            f"User-Agent: {config.USER_AGENT}\r\n"
            "Accept: */*\r\n"
            "Accept-Encoding: identity;q=1, *;q=0\r\n"
            "Accept-Language: en-GB-oxendict,en;q=0.9,zh-CN;q=0.8,zh;q=0.7\r\n"
            "Cache-Control: no-cache\r\n"
            "Pragma: no-cache\r\n"
            "Range: bytes=0-\r\n"
            "Sec-Fetch-Dest: video\r\n"
            "Sec-Fetch-Mode: cors\r\n"
            "Sec-Fetch-Site: same-origin\r\n"
            "Connection: keep-alive\r\n"
        )
        return video_url, headers
