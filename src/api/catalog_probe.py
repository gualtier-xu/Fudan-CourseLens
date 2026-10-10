"""Closed-set catalog authorization probes.

Contract: ``docs/catalog-session-recovery-handoff.md`` §4B/§4C.  Every public
function returns ONLY booleans, closed category labels, and bounded counts —
never cookie names (beyond the exact ``_token`` existence question), values,
hashes, URLs, headers, bodies, accounts, or course fields.  The module never
prints or logs; callers decide what to do with the closed dictionaries.

The probes are diagnostic instruments only.  The index/detail probes
deliberately issue their request WITHOUT an Authorization header; since the
2026-09-07 ladder-1 fix the production WebVPN refresh does the same whenever
the legacy serialized bearer is absent, so probe and production request shape
stay aligned.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from urllib.parse import unquote

import requests

# 与生产提取器相同的 PHP 序列化片段形状（icourse._SESSION_TOKEN_RE）。
_PHP_TOKEN_RE = re.compile(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}')

# 结构性 JWT 形状：三段 base64url，每段至少 4 个字符。只看结构，不解码。
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}$")

# 受限 opaque 形状：可打印 ASCII、无 CR/LF、16..4096。
_OPAQUE_RE = re.compile(r"[ -~]{16,4096}")

PROBE_SCHEMA_VERSION = 1
TOKEN_COOKIE_NAME = "_token"
DECODE_LAYER_CAP = 3

COOKIE_TOTAL_CAP = 1000
DAY_COUNT_CAP = 100
ROW_COUNT_CAP = 1000
LECTURE_COUNT_CAP = 2000
SHAPE_LIST_CAP = 8

# 闭集标签（合同 §4B）。新增标签必须同步更新 tests/test_catalog_probe.py。
BEARER_SHAPES = frozenset({
    "php_serialized", "jwt_like", "json", "opaque", "unknown",
})
TOKEN_SHAPE_SUMMARIES = BEARER_SHAPES | {"missing", "mixed"}
LENGTH_BUCKETS = ("lt16", "16_64", "65_256", "257_1024", "1025_4096", "gt4096")
DOMAIN_KINDS = ("icourse", "idp", "webvpn", "other")
# R3-36 处置注记：原 :52-59 六个探针遥测闭集（PROBE_OUTCOMES/STATUS_KINDS/
# CONTENT_TYPE_KINDS/JSON_TOP_TYPES/CODE_KINDS/IDENTITY_KINDS）零引用已删；
# PROBE_OUTCOMES 与 src/api/webvpn.py STAGE_OUTCOMES 逐元素恒等（R3-49），
# 未来需要活闭集时优先提到共享轻模块两侧 import，勿复活本地双写。
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _clamp_int(value: object, cap: int) -> int:
    try:
        number = int(value)  # bool 是 int 子类，先显式排除
    except (TypeError, ValueError):
        return 0
    if isinstance(value, bool):
        return int(value)
    return max(0, min(number, cap))


def _length_bucket(length: int) -> str:
    if length < 16:
        return "lt16"
    if length <= 64:
        return "16_64"
    if length <= 256:
        return "65_256"
    if length <= 1024:
        return "257_1024"
    if length <= 4096:
        return "1025_4096"
    return "gt4096"


def _decode_layers(value: str) -> tuple[str, int, bool]:
    """URL 解码 0..3 层，返回（最终文本, 实际层数, 是否在某层命中 PHP 形状）。"""
    decoded = value
    layers = 0
    for layer in range(DECODE_LAYER_CAP + 1):
        if _PHP_TOKEN_RE.search(decoded) is not None:
            return decoded, layer, True
        if layer == DECODE_LAYER_CAP:
            break
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
        layers += 1
    return decoded, layers, False


def classify_bearer_shape(value: object) -> dict:
    """把一个候选 Cookie 值归类为闭集形状事实；绝不输出值本身。"""
    text = str(value or "")
    decoded, layers, php_like = _decode_layers(text)
    if php_like:
        shape = "php_serialized"
    elif _JWT_RE.fullmatch(decoded) is not None:
        shape = "jwt_like"
    else:
        try:
            parsed = json.loads(decoded)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            shape = "json"
        elif _OPAQUE_RE.fullmatch(decoded) is not None:
            shape = "opaque"
        else:
            shape = "unknown"
    return {
        "shape": shape,
        "decode_layers": layers,
        "length_bucket": _length_bucket(len(decoded)),
        "has_crlf": ("\r" in decoded) or ("\n" in decoded),
    }


def _domain_kind(domain: object) -> str:
    from src.runtime import config

    host = str(domain or "").casefold().lstrip(".")
    if not host:
        return "other"
    for kind, base in (
        ("icourse", config.ICOURSE_BASE),
        ("idp", config.IDP_BASE),
        ("webvpn", config.WEBVPN_BASE),
    ):
        expected = str(urlparse_host(base) or "").casefold()
        if expected and (host == expected or host.endswith("." + expected)):
            return kind
    return "other"


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(str(url or "")).hostname or ""


def cookie_jar_shape(jar) -> dict:
    """Cookie jar 的闭集计数：总数、``_token`` 存在性与形状、域类别计数。"""
    counts = {kind: 0 for kind in DOMAIN_KINDS}
    total = 0
    token_shapes: list[str] = []
    for cookie in jar or ():
        total += 1
        counts[_domain_kind(getattr(cookie, "domain", ""))] += 1
        if str(getattr(cookie, "name", "") or "") == TOKEN_COOKIE_NAME:
            token_shapes.append(classify_bearer_shape(
                getattr(cookie, "value", "")
            )["shape"])
    unique_shapes = sorted(set(token_shapes))[:SHAPE_LIST_CAP]
    if not token_shapes:
        summary = "missing"
    elif len(unique_shapes) == 1:
        summary = unique_shapes[0]
    else:
        summary = "mixed"
    return {
        "cookie_total": _clamp_int(total, COOKIE_TOTAL_CAP),
        "token_cookie_present": bool(token_shapes),
        "token_cookie_count": _clamp_int(len(token_shapes), COOKIE_TOTAL_CAP),
        "token_shape": summary,
        "token_shapes": unique_shapes,
        "domain_kind_icourse": counts["icourse"],
        "domain_kind_idp": counts["idp"],
        "domain_kind_webvpn": counts["webvpn"],
        "domain_kind_other": counts["other"],
    }


def _status_kind(status: object) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return ""
    if 200 <= code < 300:
        return "ok_2xx"
    if code in (401, 403):
        return "unauthorized"
    if 300 <= code < 400:
        return "redirect"
    if 400 <= code < 500:
        return "client_error"
    if code >= 500:
        return "server_error"
    return ""


def _content_type_kind(headers: object) -> str:
    raw = ""
    try:
        raw = str((headers or {}).get("Content-Type") or "")
    except AttributeError:
        raw = ""
    value = raw.casefold()
    if not value:
        return "missing"
    if "json" in value:
        return "json"
    if "html" in value:
        return "html"
    return "other"


def _code_kind(payload: object) -> str:
    if not isinstance(payload, dict):
        return "absent"
    value = payload.get("code")
    if value == 0:
        return "zero"
    if value == 200 or str(value) == "200":
        return "200"
    return "other"


def _index_rows(payload: object) -> tuple[int, int, bool]:
    """（天数, 行数, 是否被截断）——与生产行遍历相同的结构，仅计数。"""
    if not isinstance(payload, dict):
        return 0, 0, False
    days = payload.get("list") or []
    if not isinstance(days, list):
        return 0, 0, False
    rows = 0
    for day_value in days:
        if isinstance(day_value, dict):
            rows += sum(
                1
                for row in day_value.get("course") or []
                if isinstance(row, dict)
            )
    capped = len(days) > DAY_COUNT_CAP or rows > ROW_COUNT_CAP
    return min(len(days), DAY_COUNT_CAP), min(rows, ROW_COUNT_CAP), capped


def probe_index_without_authorization(
    client, *, month: str, timeout=None
) -> dict:
    """一次不带 Authorization 的月度目录只读请求，仅输出闭集观察。"""
    result, _ = _index_probe_with_candidate(client, month=month, timeout=timeout)
    return result


def _index_probe_with_candidate(
    client, *, month: str, timeout=None
) -> tuple[dict, str | None]:
    """单次索引探针；同一响应的内存里顺带取首个候选 course_id（绝不输出）。"""
    result: dict = {
        "stage": "catalog_probe_index_noauth",
        "route": "webvpn",
        "request_sent": False,
        "outcome": "",
        "status_kind": "",
        "content_type_kind": "",
        "json_top_type": "",
        "code_kind": "",
        "list_present": False,
        "day_count": 0,
        "row_count": 0,
        "counts_capped": False,
        "elapsed_ms": 0,
    }
    if MONTH_RE.fullmatch(str(month or "")) is None:
        result["outcome"] = "request_error"
        return result, None
    started = time.monotonic()
    try:
        response = client.vpn.get(
            f"{client.base_url}/courseapi/v2/course-live/get-my-course-month",
            params={"month": str(month)},
            timeout=timeout or client.CATALOG_TIMEOUT,
        )
    except requests.Timeout:
        result["outcome"] = "timeout"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result, None
    except requests.ConnectionError:
        result["outcome"] = "connection_error"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result, None
    except requests.RequestException:
        result["outcome"] = "request_error"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result, None
    result["request_sent"] = True
    result["outcome"] = "ok"
    result["status_kind"] = _status_kind(getattr(response, "status_code", 0))
    result["content_type_kind"] = _content_type_kind(getattr(response, "headers", None))
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    candidate: str | None = None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        result["json_top_type"] = (
            "not_json" if result["content_type_kind"] != "json" else "invalid"
        )
        return result, None
    if isinstance(payload, dict):
        result["json_top_type"] = "dict"
        for day_value in payload.get("list") or []:
            if candidate is not None:
                break
            if not isinstance(day_value, dict):
                continue
            for row in day_value.get("course") or []:
                if isinstance(row, dict):
                    course_id = str(row.get("id") or row.get("course_id") or "").strip()
                    if course_id:
                        candidate = course_id
                        break
    elif isinstance(payload, list):
        result["json_top_type"] = "list"
    else:
        result["json_top_type"] = "scalar"
    result["code_kind"] = _code_kind(payload)
    days, rows, capped = _index_rows(payload)
    result["list_present"] = isinstance(payload, dict) and isinstance(payload.get("list"), list)
    result["day_count"] = days
    result["row_count"] = rows
    result["counts_capped"] = capped
    return result, candidate


def _detail_counts(payload: object) -> tuple[int, bool]:
    if not isinstance(payload, dict):
        return 0, False
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0, False
    sub_list = data.get("sub_list")
    if not isinstance(sub_list, dict):
        return 0, False
    lectures = 0
    for months in sub_list.values():
        if not isinstance(months, dict):
            continue
        for days in months.values():
            if not isinstance(days, dict):
                continue
            for items in days.values():
                if isinstance(items, list):
                    lectures += sum(1 for item in items if isinstance(item, dict))
    capped = lectures > LECTURE_COUNT_CAP
    return min(lectures, LECTURE_COUNT_CAP), capped


def probe_detail_without_authorization(
    client, course_id: str, *, timeout=None
) -> dict:
    """一次不带 Authorization 的课程详情只读请求，仅输出闭集观察。"""
    result: dict = {
        "stage": "catalog_probe_detail_noauth",
        "route": "webvpn",
        "request_sent": False,
        "outcome": "",
        "status_kind": "",
        "content_type_kind": "",
        "json_top_type": "",
        "code_kind": "",
        "data_present": False,
        "title_present": False,
        "sub_list_present": False,
        "lecture_count": 0,
        "counts_capped": False,
        "elapsed_ms": 0,
    }
    if not str(course_id or "").strip():
        result["outcome"] = "request_error"
        return result
    started = time.monotonic()
    try:
        response = client.vpn.get(
            f"{client.base_url}/courseapi/v3/multi-search/get-course-detail",
            params={"course_id": str(course_id)},
            timeout=timeout or client.CATALOG_TIMEOUT,
        )
    except requests.Timeout:
        result["outcome"] = "timeout"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    except requests.ConnectionError:
        result["outcome"] = "connection_error"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    except requests.RequestException:
        result["outcome"] = "request_error"
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    result["request_sent"] = True
    result["outcome"] = "ok"
    result["status_kind"] = _status_kind(getattr(response, "status_code", 0))
    result["content_type_kind"] = _content_type_kind(getattr(response, "headers", None))
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    try:
        payload = response.json()
    except (ValueError, TypeError):
        result["json_top_type"] = (
            "not_json" if result["content_type_kind"] != "json" else "invalid"
        )
        return result
    result["json_top_type"] = (
        "dict" if isinstance(payload, dict)
        else "list" if isinstance(payload, list)
        else "scalar"
    )
    result["code_kind"] = _code_kind(payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    result["data_present"] = isinstance(data, dict)
    result["title_present"] = result["data_present"] and "title" in data
    result["sub_list_present"] = (
        result["data_present"] and isinstance(data.get("sub_list"), dict)
    )
    lectures, capped = _detail_counts(payload)
    result["lecture_count"] = lectures
    result["counts_capped"] = capped
    return result


def _live_identity(client, timeout) -> tuple[str, dict | None]:
    """一次实时 infosimple（vpn transport、无 Authorization）。返回（闭集类别, 内存身份）。"""
    try:
        response = client.vpn.get(
            f"{client.base_url}/userapi/v1/infosimple", timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return "unavailable", None
    if not isinstance(payload, dict) or payload.get("code") not in (0, 200):
        return "unavailable", None
    identity = dict(payload.get("params") or payload.get("data") or {})
    if not str(identity.get("id") or "") or not str(identity.get("account") or ""):
        return "unavailable", None
    return "baseline", identity


def probe_identity(client, expected: dict | None = None, *, timeout=None) -> tuple[str, dict | None]:
    """实时确认账号身份；输出闭集类别，身份值只留在内存中供比较。"""
    kind, identity = _live_identity(client, timeout or getattr(client, "CATALOG_TIMEOUT", (5, 15)))
    if kind != "baseline" or expected is None:
        return kind, identity
    same = (
        str(identity.get("id") or "") == str(expected.get("id") or "")
        and str(identity.get("account") or "") == str(expected.get("account") or "")
    )
    return ("match" if same else "mismatch"), identity


def run_closed_catalog_probe(client, *, month: str | None = None, today: date | None = None) -> dict:
    """有界探针编排：基线身份 → jar 形状 → 索引 no-auth →（可行时）单详情 → 复核身份。

    请求预算（合同 §8）：至多 2 次身份 + 1 次索引 + 至多 1 次详情。
    任何一步失败即 fail closed，不追加请求。
    """
    chosen_month = str(month or (today or date.today()).strftime("%Y-%m"))
    result: dict = {
        "schema": PROBE_SCHEMA_VERSION,
        "route": "webvpn",
        "month": chosen_month if MONTH_RE.fullmatch(chosen_month) else "",
        "stopped": "",
        "identity": {"identity_kind": ""},
        "cookies": {},
        "index": {},
        "detail": {},
        "identity_after": {"identity_kind": ""},
    }
    if not result["month"]:
        result["stopped"] = "month_invalid"
        return result

    baseline_kind, identity = probe_identity(client)
    result["identity"]["identity_kind"] = baseline_kind
    if baseline_kind != "baseline":
        result["stopped"] = "identity_unavailable"
        return result

    session = getattr(client.vpn, "session", None)
    result["cookies"] = cookie_jar_shape(getattr(session, "cookies", None))

    index, candidate = _index_probe_with_candidate(client, month=chosen_month)
    result["index"] = index
    if not index.get("request_sent") or index.get("outcome") != "ok":
        result["stopped"] = "index_transport_failed"
        return result
    if index.get("code_kind") not in ("zero", "200"):
        result["stopped"] = "index_rejected"
        return result

    if not index.get("list_present"):
        result["detail"] = {"skipped": "index_list_invalid"}
    elif candidate is None:
        result["detail"] = {"skipped": "no_candidates"}
    else:
        result["detail"] = probe_detail_without_authorization(client, candidate)

    after_kind, _ = probe_identity(client, identity)
    result["identity_after"]["identity_kind"] = after_kind
    if after_kind == "mismatch":
        result["stopped"] = "identity_mismatch_after_probe"
    elif after_kind == "unavailable":
        result["stopped"] = "identity_unavailable_after_probe"
    return result
