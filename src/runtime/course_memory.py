"""课程字幕记忆（COURSEMEM-1）：deep_audit 修正 → 课程专属 few-shot 示例库。

同课程反复出现、LLM 已应用过的修正沉淀为本地示例文件（按 course_id 隔离），
后续同课程转写组装 job payload 时经 ``examples`` 键回注——worker 读侧
（``resolve_course_examples``）按合同消费，课程示例恒排通用示例前。全部本地、
零外联、零 LLM 调用；读与写都 fail-closed：审计账缺失/文件损坏/条目不成形
一律跳过，绝不挡住字幕导入与任务派发主链。

幂等口径：按「改前文本」去重（首见保留）——同一结果重复回放产出的审计账
逐条相同，第二次起零新增；同课程示例封顶 200 条（与 worker ``raw[:200]``
切片语义一致，先沉淀的先保留）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

# 封顶与 worker glossary.GLOSSARY_MAX_TERMS 对齐：客户端多存没有意义——
# worker 读侧本来就只消费前 200 条。
COURSE_MEMORY_MAX_EXAMPLES = 200
# 超长段进示例只会撑爆提示词窗口，对 few-shot 无益；直接跳过（fail-closed）。
_MAX_EXAMPLE_CHARS = 400
_MEMORY_VERSION = 1
# P10 跨讲映射记账（mapping_stats）帽：与 feedback._MAX_SIGNALS 同值（合同
# 冻结：帽 100、按 updated_at 最旧淘汰）。feedback 模块顶层已 import 本模块，
# 这里不能反向 import，就地同值。
_MAX_MAPPING_STATS = 100
# 仅标点/空格差异的修正属于通用标点链（v4 提示词恒定职责），不是课程知识，
# 不进课程示例库。
_CONTENT_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]+")


def memory_path(output_dir: str | Path, course_id: str) -> Path:
    """课程记忆文件路径：按 course_id 哈希命名，避开路径不安全字符。"""
    digest = hashlib.sha256(str(course_id).encode("utf-8")).hexdigest()[:16]
    return Path(output_dir) / "course-memory" / f"{digest}.json"


def _empty_document(course_id: str) -> dict[str, Any]:
    return {"version": _MEMORY_VERSION, "course_id": str(course_id), "examples": []}


def _load_document(path: Path, course_id: str) -> dict[str, Any]:
    """读课程记忆文档；缺席/损坏/课程不符一律退空文档（fail-closed）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_document(course_id)
    if not isinstance(raw, dict) or not isinstance(raw.get("examples"), list):
        return _empty_document(course_id)
    return raw


def _example_from_audit_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """一条审计账 → 一条 worker 合同形状的 few-shot 示例；不成形返回 None。

    示例形状与 ``resolve_course_examples`` 逐字段对齐：
    ``{"input": [{"id","text"}], "ops": [{"id","old","new"}]}``，其中 old=改前
    整段、new=改后整段（v4 提示词本身允许整段重写形态），old 恒原样出现在
    text 中。
    """
    before = str(entry.get("before") or "")
    after = str(entry.get("after") or "")
    if not before or not after or before == after:
        return None
    if len(before) > _MAX_EXAMPLE_CHARS or len(after) > _MAX_EXAMPLE_CHARS:
        return None
    if "".join(_CONTENT_RE.findall(before)) == "".join(_CONTENT_RE.findall(after)):
        return None  # 仅标点差异：通用职责，不占课程示例名额
    return {
        "input": [{"id": "e0", "text": before}],
        "ops": [{"id": "e0", "old": before, "new": after}],
    }


def _conforming_example(item: Any) -> dict[str, Any] | None:
    """读侧校验：与 worker ``resolve_course_examples`` 同口径，坏条目整条丢弃。"""
    if not isinstance(item, dict):
        return None
    raw_input = item.get("input")
    raw_ops = item.get("ops")
    if not isinstance(raw_input, list) or not raw_input or not isinstance(raw_ops, list):
        return None
    shaped_input: list[dict[str, str]] = []
    shaped_ops: list[dict[str, str]] = []
    for entry in raw_input:
        if not isinstance(entry, dict):
            return None
        entry_id = str(entry.get("id") or "").strip()
        entry_text = str(entry.get("text") or "").strip()
        if not entry_id or not entry_text:
            return None
        shaped_input.append({"id": entry_id, "text": entry_text})
    for op in raw_ops:
        if not isinstance(op, dict):
            return None
        op_id = str(op.get("id") or "").strip()
        op_old = str(op.get("old") or "")
        op_new = str(op.get("new") or "")
        if not op_id or not op_old or not op_new:
            return None
        shaped_ops.append({"id": op_id, "old": op_old, "new": op_new})
    return {"input": shaped_input, "ops": shaped_ops}


def _write_document(path: Path, document: dict[str, Any]) -> bool:
    """原子写（tmp + os.replace）；任何写失败如实返回 False，不抛。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def sink_course_examples(
    output_dir: str | Path,
    course_id: str,
    deep_audit: Any,
    sub_id: str = "",
) -> int:
    """把一次字幕结果的 deep_audit 审计账沉淀进课程记忆；返回新增条数。

    幂等：按改前文本去重，重复回放同一结果零新增。全程 fail-closed：
    审计账不是列表、条目不成形、读不到写不出，一律按 0/部分成功收口，
    绝不抛给调用方（字幕导入主链绝不因记忆沉淀失败而失败）。

    P10 跨讲映射记账：传了 ``sub_id`` 且本批新增 ≥1 示例时，同一次文档写
    内把该批词对挂账进顶层加性键 ``mapping_stats``（候选流水的主源）；
    缺省空串=byte-identical 旧行为。
    """
    course_id = str(course_id or "").strip()
    if not course_id or not isinstance(deep_audit, list) or not deep_audit:
        return 0
    path = memory_path(output_dir, course_id)
    document = _load_document(path, course_id)
    existing = document.get("examples") or []
    known_inputs: set[str] = set()
    examples: list[dict[str, Any]] = []
    for item in existing[:COURSE_MEMORY_MAX_EXAMPLES]:
        example = _conforming_example(item)
        if example is None:
            continue
        known_inputs.add(example["input"][0]["text"])
        examples.append(example)
    added = 0
    new_examples: list[dict[str, Any]] = []
    for entry in deep_audit:
        if len(examples) >= COURSE_MEMORY_MAX_EXAMPLES:
            break
        if not isinstance(entry, dict):
            continue
        example = _example_from_audit_entry(entry)
        if example is None:
            continue
        text = example["input"][0]["text"]
        if text in known_inputs:
            continue
        known_inputs.add(text)
        examples.append(example)
        new_examples.append(example)
        added += 1
    if not added:
        return 0
    document["version"] = _MEMORY_VERSION
    document["course_id"] = course_id
    document["examples"] = examples[:COURSE_MEMORY_MAX_EXAMPLES]
    document["updated_at"] = time.time()
    sub = str(sub_id or "").strip()
    if sub:
        try:
            _book_mapping_stats(document, new_examples, sub)
        except Exception:  # noqa: BLE001 - 记账是增值面，绝不挡示例沉淀
            document.pop("mapping_stats", None)
    return added if _write_document(path, document) else 0


def _book_mapping_stats(
    document: dict[str, Any], new_examples: list[dict[str, Any]], sub_id: str
) -> None:
    """P10 冻结件 1：把本批新增示例的术语对挂账进顶层 ``mapping_stats``。

    词对来源=``derive_term_mappings``（与注入侧共用歧义门）；出现次数按
    替换块计数。条目 ``{"wrong","right","subs","first_seen","updated_at"}``，
    键= ``wrong\\0right``、同讲累计、帽 100 按 updated_at 最旧淘汰——与
    signals 同一套共存纪律（各写者只重赋自己的键）。
    """
    # 局部 import：feedback 顶层已 import 本模块，反向只能函数内延迟解析。
    from src.runtime.course_memory_feedback import (
        _replacement_pairs,
        derive_term_mappings,
    )

    pairs = derive_term_mappings(new_examples)
    if not pairs:
        return
    counts: dict[tuple[str, str], int] = {}
    for item in new_examples:
        example = _conforming_example(item)
        if example is None:
            continue
        for op in example.get("ops") or []:
            for wrong, right in _replacement_pairs(
                str(op.get("old") or ""), str(op.get("new") or "")
            ):
                counts[(wrong, right)] = counts.get((wrong, right), 0) + 1
    stats: dict[str, dict[str, Any]] = {}
    for entry in document.get("mapping_stats") or []:
        if not isinstance(entry, dict):
            continue
        wrong = str(entry.get("wrong") or "")
        right = str(entry.get("right") or "")
        subs_raw = entry.get("subs")
        if not wrong or not right or not isinstance(subs_raw, dict):
            continue
        subs: dict[str, int] = {}
        for sub_key, value in subs_raw.items():
            try:
                count = int(value or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                subs[str(sub_key)] = count
        stats[f"{wrong}\u0000{right}"] = {
            "wrong": wrong,
            "right": right,
            "subs": subs,
            "first_seen": float(entry.get("first_seen") or 0.0),
            "updated_at": float(entry.get("updated_at") or 0.0),
        }
    now = time.time()
    for wrong, right in pairs:
        key = f"{wrong}\u0000{right}"
        entry = stats.get(key)
        if entry is None:
            entry = {
                "wrong": wrong, "right": right, "subs": {},
                "first_seen": now, "updated_at": now,
            }
            stats[key] = entry
        entry["subs"][sub_id] = int(entry["subs"].get(sub_id) or 0) + max(
            1, int(counts.get((wrong, right), 1))
        )
        entry["updated_at"] = now
    ordered = sorted(
        stats.values(),
        key=lambda item: float(item.get("updated_at") or 0.0),
        reverse=True,
    )
    document["mapping_stats"] = ordered[:_MAX_MAPPING_STATS]


def load_course_examples(
    output_dir: str | Path,
    course_id: str,
    limit: int = COURSE_MEMORY_MAX_EXAMPLES,
) -> list[dict[str, Any]]:
    """读课程示例（payload ``examples`` 注入形状）；缺席/损坏/空 → 空列表。"""
    course_id = str(course_id or "").strip()
    if not course_id:
        return []
    document = _load_document(memory_path(output_dir, course_id), course_id)
    examples: list[dict[str, Any]] = []
    for item in document.get("examples") or []:
        example = _conforming_example(item)
        if example is not None:
            examples.append(example)
        if len(examples) >= max(0, int(limit)):
            break
    return examples


def course_memory_count(output_dir: str | Path, course_id: str) -> int:
    """本课程已积累的示例条数（与注入面同一校验口径）。"""
    return len(load_course_examples(output_dir, course_id))
