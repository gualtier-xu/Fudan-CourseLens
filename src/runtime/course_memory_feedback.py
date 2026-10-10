"""课程记忆反哺质量回路（RR-P6MEM-1 · AIRESEARCH P6）。

把 course_memory 的沉淀从「派发时注入」升级为「生成后校验反哺」的本地
闭环，全程零 LLM、零外联：

1. **推导**：从课程 few-shot 示例（LLM 已应用的整段修正）里提取术语级
   写法映射（wrong→right，difflib 替换块）——记忆文件只存整段对，注入与
   检查需要的是词对。
2. **注入**：``course_memory_terms`` 把 right 侧术语排成派发表，有偏差
   信号的排前（反哺的生效点： Leak 越多的写法越优先挤进 200 条帽），
   随总结/问答 job payload 的 ``glossary`` 键进生成链。
3. **反哺**：``record_product_deviations`` 在总结/回答落库后对照记忆做
   轻量一致性检查——产物文本命中 wrong 变体即记偏差信号（计数/来源/
   末次时间，按文本哈希幂等），绝不改写产物本身。
4. **标注**：``memory_annotation`` 产出学生可见的人话标注行（真注入过
   记忆才有；N=生成侧实报的注入条数）。

全部 fail-closed：记忆缺席/损坏/条目不成形一律按空/0 收口，绝不抛给
调用方，绝不挡总结与问答的导入主链。
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time
from typing import Any

from src.runtime.course_memory import (
    COURSE_MEMORY_MAX_EXAMPLES,
    _conforming_example,
    _load_document,
    _write_document,
    load_course_examples,
    memory_path,
)

# 与 worker glossary 的词长口径一致：过短的替换块是噪声，过长的不是术语。
_MIN_TERM_CHARS = 2
_MAX_TERM_CHARS = 8
# 单课程偏差信号帽：信号是排序依据不是账本，够排序用即可。
_MAX_SIGNALS = 100
# P10 确认/忽略台账帽：与示例帽同值——确认词对是长期资产（进 ASR 热词），
# 帽只防膨胀；按时间序 append、超帽丢最早。
_MAX_TERM_LEDGER = 200
# AIRESEARCH H4 埋点（TELEMETRY-H64-1）：偏差信号按讲次闭集分桶——首讲/第二讲/
# 第三讲及以后，支撑「示例注入后第二讲术语错误率低于首讲基线」的零 LLM 基线
# 记账。越界/缺席桶值一律不记账（fail-closed，绝不抛）；发射端（总结/回答
# 调用点传讲次桶）尚未接线，属「余量待合同」，记账口径先行稳定。
LECTURE_BUCKETS = ("first", "second", "later")
# 每信号保留的最近文本哈希数（有界环）：A→B→A 交替导入时单哈希会被 B 洗掉
# A 的记忆而重复计数，环内命中一律跳过；8 个足够覆盖回放窗口且有界。
_RECENT_TEXT_HASHES = 8
# THINK-LADDER-1 确定性自动晋升阈值：同错→对词对偏差信号累计 ≥3 次自动确认
# （复用 confirm_term_mapping 同落点；带 provenance 可经 dismiss 撤销）。
_AUTO_CONFIRM_THRESHOLD = 3
# 不等长替换块（单字错字类）的左扩窗口：中文错字替换的词头在左侧，
# 借等量左邻等文扩出词级窗口（如「…费米能及很重要」→「…费米能级很重要，」
# 的最小替换块是 及→级，左扩 3 字得 费米能及→费米能级）。
_EXPAND_CONTEXT = 3
_EDGE_JUNK_RE = re.compile(r"^[\W_]+|[\W_]+$")
_CONTENT_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]")


def _content_fragment(value: str) -> str:
    """替换块按内容收口：两端去标点/空白后落在词长窗口内才算术语候选。"""
    stripped = _EDGE_JUNK_RE.sub("", str(value or ""))
    if not (_MIN_TERM_CHARS <= len(stripped) <= _MAX_TERM_CHARS):
        return ""
    return stripped if _CONTENT_RE.search(stripped) else ""


def _replacement_pairs(old: str, new: str) -> list[tuple[str, str]]:
    """一对整段改前/改后文本 → 术语级 (wrong, right) 候选。

    等长替换块直收（塌缩→坍缩、直博生→直播生类）；不等长块（单字错字类
    及/级）借左邻等文等量左扩成词级窗口。两侧仍落在词长窗口内才收。
    """
    pairs: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        direct = (
            _content_fragment(old[i1:i2]),
            _content_fragment(new[j1:j2]),
        )
        if all(direct):
            pairs.append(direct)
            continue
        expand = min(i1, j1, _EXPAND_CONTEXT)
        widened = (
            _content_fragment(old[i1 - expand:i2]),
            _content_fragment(new[j1 - expand:j2]),
        )
        if all(widened) and widened[0] != widened[1]:
            pairs.append(widened)
    return pairs


def derive_term_mappings(examples: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """从课程示例提取术语级写法映射（wrong→right），保序去重。

    示例的 ops 是整段改前/改后（v4 提示词允许整段重写形态）；difflib 的
    replace 块给出真正被改写的片段，双边都落在词长窗口内才收。同一个
    wrong 映到多个不同 right=无证据不成对（与 worker glossary 配对门同
    判），整组丢弃——信号与注入都不吃歧义词对。
    """
    paired: dict[str, set[str]] = {}
    order: list[str] = []
    for item in examples or []:
        example = _conforming_example(item)
        if example is None:
            continue
        for op in example.get("ops") or []:
            old = str(op.get("old") or "")
            new = str(op.get("new") or "")
            if not old or not new:
                continue
            for wrong, right in _replacement_pairs(old, new):
                if wrong not in paired:
                    paired[wrong] = set()
                    order.append(wrong)
                paired[wrong].add(right)
    mappings: list[tuple[str, str]] = []
    for wrong in order:
        rights = paired[wrong]
        if len(rights) != 1:
            continue
        mappings.append((wrong, next(iter(rights))))
    return mappings


def _recent_text_hashes(item: dict[str, Any]) -> list[str]:
    """信号的去重回放环：``recent_text_hashes`` 优先，旧文档从单哈希迁移。"""
    raw = item.get("recent_text_hashes")
    hashes = [str(value) for value in raw if str(value)] if isinstance(raw, list) else []
    if not hashes:
        legacy = str(item.get("last_text_hash") or "")
        if legacy:
            hashes = [legacy]
    return hashes[:_RECENT_TEXT_HASHES]


def _load_signals(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    raw = document.get("signals")
    if not isinstance(raw, list):
        return signals
    for item in raw:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if not wrong or not right:
            continue
        signals[f"{wrong}\u0000{right}"] = {
            "wrong": wrong,
            "right": right,
            "count": max(0, int(item.get("count") or 0)),
            "first_seen": float(item.get("first_seen") or 0.0),
            "last_seen": float(item.get("last_seen") or 0.0),
            "sources": dict(item.get("sources") or {}),
            "lecture_buckets": _load_lecture_buckets(item),
            "recent_text_hashes": _recent_text_hashes(item),
            "last_text_hash": str(item.get("last_text_hash") or ""),
        }
    return signals


def _load_lecture_buckets(item: dict[str, Any]) -> dict[str, int]:
    """H4 讲次桶只收闭集键、只收非负整数——畸形桶一律丢弃（fail-closed）。"""
    raw = item.get("lecture_buckets")
    if not isinstance(raw, dict):
        return {}
    buckets: dict[str, int] = {}
    for name in LECTURE_BUCKETS:
        try:
            value = max(0, int(raw.get(name) or 0))
        except (TypeError, ValueError):
            continue
        if value:
            buckets[str(name)] = value
    return buckets


def course_memory_terms(output_dir: str | Path, course_id: str) -> tuple[str, ...]:
    """注入用课程术语表：right 侧术语，有偏差信号的排前，封顶 200。

    排序即反哺闭环的生效点——偏差信号说明「这个写法反复漏进产物」，它
    的正确写法优先占用注入名额。缺席/损坏 → 空元组（调用方省键=旧行为）。
    """
    mappings = derive_term_mappings(load_course_examples(output_dir, str(course_id or "")))
    if not mappings:
        return ()
    signals = _load_signals(_load_document(memory_path(output_dir, str(course_id)), str(course_id)))

    def signal_count(wrong: str, right: str) -> int:
        return int(signals.get(f"{wrong}\u0000{right}", {}).get("count") or 0)

    ordered_terms: list[str] = []
    seen: set[str] = set()
    ranked = [
        (-signal_count(wrong, right), index, right)
        for index, (wrong, right) in enumerate(mappings)
    ]
    for _, _, right in sorted(ranked):
        if right and right not in seen:
            seen.add(right)
            ordered_terms.append(right)
    return tuple(ordered_terms[:COURSE_MEMORY_MAX_EXAMPLES])


def record_product_deviations(
    output_dir: str | Path,
    course_id: str,
    text: str,
    *,
    source: str,
    lecture_bucket: str = "",
) -> tuple[int, int]:
    """生成后一致性检查：产物文本命中 wrong 变体即记偏差信号。

    返回 ``(hits, recorded)`` 两个口径（P2-12 分开报，旧单值把两者混成
    「recorded」是虚账）：``hits``=文本里 wrong 变体的命中处数（纯观测，
    回放/环内命中也如实计）；``recorded``=本次实际写进信号账的处数
    （回放幂等跳过、超帽淘汰不记账时小于 hits）。

    H4 讲次桶（TELEMETRY-H64-1）：``lecture_bucket`` 取 ``LECTURE_BUCKETS``
    闭集值（first/second/later）时，实记账处数同步累计进该信号的
    ``lecture_buckets`` 桶账（与 count 同口径=只记 recorded，回放幂等跳过
    不重复计）；缺席/越界桶值=不记桶账，绝不抛——调用点尚未传桶（余量待
    合同）时零行为差异。

    幂等：每信号存最近 ``_RECENT_TEXT_HASHES`` 个文本哈希的有界环
    （P1-5，单哈希会被 A→B→A 交替导入洗掉记忆而重复计数），环内命中
    一律跳过。只记账不改写产物；写失败如实返回已计数但零副作用。任何
    读取失败按 (0, 0) 收口，绝不抛。
    """
    course_id = str(course_id or "").strip()
    body = str(text or "")
    if not course_id or not body:
        return (0, 0)
    bucket = str(lecture_bucket or "").strip()
    if bucket not in LECTURE_BUCKETS:
        bucket = ""
    try:
        mappings = derive_term_mappings(load_course_examples(output_dir, course_id))
        if not mappings:
            return (0, 0)
        path = memory_path(output_dir, course_id)
        document = _load_document(path, course_id)
        signals = _load_signals(document)
        text_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = time.time()
        hits = 0
        recorded = 0
        changed = False
        for wrong, right in mappings:
            occurrences = body.count(wrong)
            if not occurrences:
                continue
            hits += occurrences
            key = f"{wrong}\u0000{right}"
            signal = signals.get(key)
            if signal is None:
                signal = {
                    "wrong": wrong, "right": right, "count": 0,
                    "first_seen": now, "last_seen": now,
                    "sources": {}, "lecture_buckets": {},
                    "recent_text_hashes": [], "last_text_hash": "",
                }
                signals[key] = signal
            recent = list(signal.get("recent_text_hashes") or [])
            if text_hash in recent:
                continue
            signal["count"] = int(signal.get("count") or 0) + occurrences
            signal["last_seen"] = now
            sources = dict(signal.get("sources") or {})
            sources[str(source or "unknown")] = int(sources.get(str(source or "unknown")) or 0) + 1
            signal["sources"] = sources
            if bucket:
                buckets = _load_lecture_buckets(signal)
                buckets[bucket] = int(buckets.get(bucket) or 0) + occurrences
                signal["lecture_buckets"] = buckets
            recent.insert(0, text_hash)
            signal["recent_text_hashes"] = recent[:_RECENT_TEXT_HASHES]
            signal["last_text_hash"] = text_hash
            recorded += occurrences
            changed = True
        if not changed:
            return (hits, recorded)
        # P2-13：超帽淘汰按 last_seen 最旧（信号是排序依据，刚发生的信号
        # 绝不因插入序靠后被挤掉），不是按插入序截断。
        ordered = sorted(
            signals.values(),
            key=lambda item: float(item.get("last_seen") or 0.0),
            reverse=True,
        )
        document["signals"] = ordered[:_MAX_SIGNALS]
        document["updated_at"] = time.time()
        _write_document(path, document)
        # THINK-LADDER-1：信号 ≥3 的词对确定性自动晋升（确认台账同落点，
        # 带 provenance；已忽略/人工确认的终态对在 confirm 幂等门自然跳过）。
        promoted = _auto_confirm_high_signal_mappings(output_dir, course_id)
        if promoted:
            print(
                f"[FudanCourseLens] Course memory auto-confirmed {promoted} term "
                f"mapping(s) (signal >= {_AUTO_CONFIRM_THRESHOLD}) for course {course_id}",
                flush=True,
            )
        return (hits, recorded)
    except Exception:  # noqa: BLE001 - 反哺是增值面，绝不挡导入主链
        return (0, 0)


def memory_annotation(count: int, *, kind: str = "answer") -> str:
    """学生可见的人话标注行；count<=0 返回空串（宁缺毋滥）。

    kind="answer" → 本回答（书签解释/题目解答）；kind="summary" → 本笔记
    （时间戳总结）。措辞与学习页既有「术语记忆」chip 同一词汇。
    """
    try:
        total = int(count)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    subject = "本笔记" if str(kind or "") == "summary" else "本回答"
    return f"\n\n—— {subject}已应用课程记忆 {total} 条"


# ---- P10 课园词汇候选流水（P10-CONTRACT-1 冻结件 2/3/4）--------------------
# 跨讲映射记账（mapping_stats 由 course_memory.sink_course_examples 挂账）→
# 候选清单 → 人工确认 → 字幕链 payload.glossary。与 P6 自动注入面的信任
# 边界：只有人工确认的词才进字幕链（热词偏置所有后讲转写，信任级别必须
# 高于生成侧提示注入）。零 LLM、零外联、零迁移；读侧全部 fail-closed。


class TermCandidateUnknown(Exception):
    """P10 资格门：确认/忽略对象不在当前推导候选集（闭集拒绝，非自由词输入面）。"""


def _load_mapping_stats(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """mapping_stats 读侧：键 ``wrong\\0right`` → 条目；畸形条目整条丢弃。"""
    raw = document.get("mapping_stats")
    if not isinstance(raw, list):
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        subs_raw = item.get("subs")
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
            "first_seen": float(item.get("first_seen") or 0.0),
            "updated_at": float(item.get("updated_at") or 0.0),
        }
    return stats


def _load_term_ledger(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """confirmed/dismissed 台账读侧：缺席/畸形=空表，坏条目整条丢弃。"""
    raw = document.get(key)
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if not wrong or not right:
            continue
        rows.append({"wrong": wrong, "right": right})
    return rows


def term_candidates(
    output_dir: str | Path, course_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """课程词汇候选清单（P10 冻结件 2，全部只读）。

    候选推导=聚合 mapping_stats（主）∪ 全量 ``derive_term_mappings`` 补遗
    （stats 键缺席的旧文档也出候选），两源按词对归并；已确认/已忽略的
    终态对不再出现。行形状（冻结）：wrong/right/lecture_count（不同讲次
    数）/total_count（累计出现）/signal_count（同对偏差信号）/updated_at。
    排序=lecture_count 降序 → signal_count 降序 → updated_at 新前；帽
    limit。缺席/损坏=空列表，绝不抛。
    """
    try:
        course_id = str(course_id or "").strip()
        cap = max(0, int(limit))
        if not course_id or cap <= 0:
            return []
        document = _load_document(memory_path(output_dir, course_id), course_id)
        terminal = {
            f"{row['wrong']}\u0000{row['right']}"
            for key in ("confirmed_mappings", "dismissed_mappings")
            for row in _load_term_ledger(document, key)
        }
        signals = _load_signals(document)
        merged: dict[str, dict[str, Any]] = {}
        for entry in _load_mapping_stats(document).values():
            merged[f"{entry['wrong']}\u0000{entry['right']}"] = {
                "wrong": entry["wrong"],
                "right": entry["right"],
                "subs": dict(entry["subs"]),
                "updated_at": entry["updated_at"],
            }
        for wrong, right in derive_term_mappings(load_course_examples(output_dir, course_id)):
            merged.setdefault(
                f"{wrong}\u0000{right}",
                {"wrong": wrong, "right": right, "subs": {}, "updated_at": 0.0},
            )
        rows: list[dict[str, Any]] = []
        for key, entry in merged.items():
            if key in terminal:
                continue
            subs = entry["subs"]
            rows.append({
                "wrong": str(entry["wrong"]),
                "right": str(entry["right"]),
                "lecture_count": len(subs),
                "total_count": int(sum(subs.values())),
                "signal_count": int((signals.get(key) or {}).get("count") or 0),
                "updated_at": float(entry["updated_at"]),
            })
        rows.sort(key=lambda row: (-row["lecture_count"], -row["signal_count"], -row["updated_at"]))
        return rows[:cap]
    except Exception:  # noqa: BLE001 - 候选是增值面，读不出=空列表
        return []


def confirmed_term_list(output_dir: str | Path, course_id: str) -> tuple[tuple[str, str], ...]:
    """已确认 (wrong,right) 对，confirmed_at 升序（确认序）；缺席/损坏=空元组。"""
    try:
        course_id = str(course_id or "").strip()
        if not course_id:
            return ()
        document = _load_document(memory_path(output_dir, course_id), course_id)
        raw = document.get("confirmed_mappings")
        if not isinstance(raw, list):
            return ()
        rows: list[tuple[float, str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            wrong = str(item.get("wrong") or "")
            right = str(item.get("right") or "")
            if not wrong or not right:
                continue
            try:
                stamp = float(item.get("confirmed_at") or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            rows.append((stamp, wrong, right))
        rows.sort(key=lambda row: row[0])
        return tuple((wrong, right) for _, wrong, right in rows)
    except Exception:  # noqa: BLE001
        return ()


def auto_confirmed_term_rows(
    output_dir: str | Path, course_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """THINK-LADDER-1 只读视图：自动晋升件清单（wrong/right/信号次数/确认时刻）。

    消费点=课程复习读面的 ``auto_confirmed_rows`` 加性键（前端「自动确认 ·
    信号 ×N」标+一键撤销的合同源）。只收带 ``auto_confirmed`` 真值的确认行
    （人工确认件不出列）；confirmed_at 升序、帽 limit；缺席/损坏=空列表，
    绝不抛。
    """
    try:
        course_id = str(course_id or "").strip()
        cap = max(0, int(limit))
        if not course_id or cap <= 0:
            return []
        document = _load_document(memory_path(output_dir, course_id), course_id)
        raw = document.get("confirmed_mappings")
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("auto_confirmed"):
                continue
            wrong = str(item.get("wrong") or "")
            right = str(item.get("right") or "")
            if not wrong or not right:
                continue
            try:
                signal_count = max(0, int(item.get("auto_signal_count") or 0))
            except (TypeError, ValueError):
                signal_count = 0
            try:
                stamp = float(item.get("confirmed_at") or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            rows.append({
                "wrong": wrong, "right": right,
                "signal_count": signal_count, "confirmed_at": stamp,
            })
        rows.sort(key=lambda row: row["confirmed_at"])
        return rows[:cap]
    except Exception:  # noqa: BLE001 - 读侧增值面，读不出=空列表
        return []


def confirmed_memory_terms(output_dir: str | Path, course_id: str) -> tuple[str, ...]:
    """字幕链注入词表（P10 冻结件 4）：confirmed 对的 right 词（确认序、去重、帽 200）。

    与 ``course_memory_terms``（P6 自动注入面，全量）严格分流：本函数只
    出人工确认过的词，消费点是字幕 job payload 的 ``glossary`` 键（worker
    ASR 热词 + v4 术语校对）。无确认词=空元组=省键=旧行为。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for _, right in confirmed_term_list(output_dir, course_id):
        if right in seen:
            continue
        seen.add(right)
        terms.append(right)
        if len(terms) >= COURSE_MEMORY_MAX_EXAMPLES:
            break
    return tuple(terms)


def confirm_term_mapping(
    output_dir: str | Path, course_id: str, *, wrong: str, right: str
) -> dict[str, Any]:
    """人工确认一个术语映射（P10 冻结件 3）：进 ``confirmed_mappings``，一次性终态。

    资格门：词对必须命中当前推导候选集（mapping_stats ∪ 全量推导），否则
    抛 ``TermCandidateUnknown``（HTTP 层译为 term_candidate_unknown）——
    确认门只对推导候选生效，不是自由词输入面。重复确认（或对已忽略的
    终态对再确认）=幂等 no-op 成功。写失败如实返回 confirmed=False。
    """
    return _term_mapping_action(
        output_dir, course_id,
        ledger_key="confirmed_mappings", stamp_key="confirmed_at",
        flag_key="confirmed", total_key="total_confirmed",
        wrong=wrong, right=right,
    )


def _auto_confirm_high_signal_mappings(output_dir: str | Path, course_id: str) -> int:
    """THINK-LADDER-1：偏差信号 ≥ 阈值的词对确定性自动晋升（返回晋升对数）。

    与人工 ``confirm_term_mapping`` 同一落点与同一资格门（推导候选集），
    附加 provenance（``auto_confirmed``+``auto_signal_count``，加性键、旧
    形状缺省兼容）；晋升件仍可经 ``dismiss_term_mapping`` 撤销（撤销后
    dismissed 终态挡住再晋升——用户的最终裁决不因后续信号翻案）。任何
    异常按对跳过，绝不抛给反哺主链。
    """
    try:
        document = _load_document(memory_path(output_dir, str(course_id or "")), str(course_id or ""))
    except Exception:  # noqa: BLE001
        return 0
    promoted = 0
    for signal in _load_signals(document).values():
        count = int(signal.get("count") or 0)
        if count < _AUTO_CONFIRM_THRESHOLD:
            continue
        try:
            result = _term_mapping_action(
                output_dir, course_id,
                ledger_key="confirmed_mappings", stamp_key="confirmed_at",
                flag_key="confirmed", total_key="total_confirmed",
                wrong=str(signal.get("wrong") or ""), right=str(signal.get("right") or ""),
                provenance={"auto_confirmed": True, "auto_signal_count": count},
            )
        except Exception:  # noqa: BLE001 - 资格门外/写失败=该对跳过
            continue
        if result.get("confirmed"):
            promoted += 1
    return promoted


def dismiss_term_mapping(
    output_dir: str | Path, course_id: str, *, wrong: str, right: str
) -> dict[str, Any]:
    """人工忽略一个术语候选（P10 冻结件 3）：进 ``dismissed_mappings``，终态出列。"""
    return _term_mapping_action(
        output_dir, course_id,
        ledger_key="dismissed_mappings", stamp_key="dismissed_at",
        flag_key="dismissed", total_key="total_dismissed",
        wrong=wrong, right=right,
    )


def _raw_ledger(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """台账原始行读侧：保形（保留 confirmed_at/provenance 等全部附加键）。

    `_load_term_ledger` 是消费侧投影（只取 wrong/right），不得用于回写——
    以它重建整本台账会把既有行的确认时刻/晋升溯源剥掉（写丢数据）。
    """
    raw = document.get(key)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _pair_key(row: dict[str, Any]) -> str:
    return f"{row.get('wrong')}\u0000{row.get('right')}"


def _term_mapping_action(
    output_dir: str | Path,
    course_id: str,
    *,
    ledger_key: str,
    stamp_key: str,
    flag_key: str,
    total_key: str,
    wrong: str,
    right: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wrong = str(wrong or "").strip()
    right = str(right or "").strip()
    course_id = str(course_id or "").strip()
    key = f"{wrong}\u0000{right}"
    if not course_id or not wrong or not right:
        raise TermCandidateUnknown(key)
    try:
        path = memory_path(output_dir, course_id)
        document = _load_document(path, course_id)
        raw_confirmed = _raw_ledger(document, "confirmed_mappings")
        raw_dismissed = _raw_ledger(document, "dismissed_mappings")
        if ledger_key == "dismissed_mappings" and any(
            _pair_key(row) == key for row in raw_dismissed
        ):
            # 已忽略再忽略=幂等 no-op 成功。
            return {flag_key: False, total_key: len(raw_dismissed)}
        if ledger_key == "dismissed_mappings" and any(
            _pair_key(row) == key for row in raw_confirmed
        ):
            matched = next(row for row in raw_confirmed if _pair_key(row) == key)
            if matched.get("auto_confirmed") or matched.get("judge_confirmed"):
                # THINK-LADDER-1/2：自动晋升件与 judge 裁决件的撤销通道——移出
                # 确认台账、进忽略台账（dismissed 终态同时挡住后续自动再晋升）。
                remaining = [row for row in raw_confirmed if _pair_key(row) != key]
                raw_dismissed.append({"wrong": wrong, "right": right, "dismissed_at": time.time()})
                raw_dismissed = raw_dismissed[-_MAX_TERM_LEDGER:]
                document["confirmed_mappings"] = remaining
                document["dismissed_mappings"] = raw_dismissed
                document["updated_at"] = time.time()
                if not _write_document(path, document):
                    return {flag_key: False, total_key: len(raw_dismissed)}
                return {flag_key: True, total_key: len(raw_dismissed)}
            # 人工确认件保持一次性终态：dismiss=幂等 no-op 成功（total 口径=
            # 目标台账 dismissed 计数，与既有合同一致）。
            return {flag_key: False, total_key: len(raw_dismissed)}
        if ledger_key == "confirmed_mappings" and (
            any(_pair_key(row) == key for row in raw_dismissed)
            or any(_pair_key(row) == key for row in raw_confirmed)
        ):
            # 对任一台账已有终态的词对再确认=幂等 no-op 成功。
            return {flag_key: False, total_key: len(raw_confirmed)}
        candidates = {
            f"{entry['wrong']}\u0000{entry['right']}"
            for entry in _load_mapping_stats(document).values()
        }
        candidates.update(
            f"{w}\u0000{r}"
            for w, r in derive_term_mappings(load_course_examples(output_dir, course_id))
        )
        if key not in candidates:
            raise TermCandidateUnknown(key)
        row: dict[str, Any] = {"wrong": wrong, "right": right, stamp_key: time.time()}
        if provenance:
            row.update(provenance)
        ledger = _raw_ledger(document, ledger_key)
        ledger.append(row)
        ledger = ledger[-_MAX_TERM_LEDGER:]  # 帽 200：时间序 append，最早的先出
        document[ledger_key] = ledger
        document["updated_at"] = time.time()
        if not _write_document(path, document):
            return {flag_key: False, total_key: len(ledger)}
        return {flag_key: True, total_key: len(ledger)}
    except TermCandidateUnknown:
        raise
    except Exception as error:  # noqa: BLE001 - 读不出=无本地证据，同样按资格门闭集拒绝
        raise TermCandidateUnknown(key) from error


# ---- THINK-LADDER-2 设计 B：judge 裁决的客户端组装与导入面 ------------------
# 组装：quality_judge 载荷加性键 ``term_boundary``=signal<3 候选词对（帽 10，
# 旧客户端不发此键、旧 Worker 忽略未知键，两侧均字节恒等）；导入：user-
# adjudication-supreme 三路——valid∧signal≥2 自动确认（judge 溯源、可经
# dismiss 撤销）、invalid 仅注记折叠（绝不写 dismissed 终态）、unsure/缺位
# no-op。worker 只对良构词对做一次 flash 裁决，永不写用户记忆终态；
# mapping_stats 挂账仍由字幕导入 sink 既有帽 100 面承担，本节零新增记账路径。

# 组装帽：与 worker 侧 _JUDGE_BOUNDARY_MAX_PAIRS 同值（worker 再各自复核）。
_JUDGE_BOUNDARY_PAIR_CAP = 10
# valid 裁决自动确认的信号下限（合同 valid+signal≥2；≥3 本有确定性晋升）。
_JUDGE_CONFIRM_SIGNAL_MIN = 2
# judge 裁决注记帽：注记是折叠依据不是账本，同 signals/mapping_stats 帽族。
_MAX_JUDGE_ANNOTATIONS = 100
# 与 worker llm.py 闭集同表（客户端按名消费，越集条目=skipped）。
JUDGE_BOUNDARY_RULINGS = frozenset({"valid", "invalid", "unsure"})
JUDGE_BOUNDARY_REASON_CODES = frozenset({
    "glossary_match", "glossary_conflict", "not_in_glossary", "insufficient_context",
})


def _load_judge_annotations(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """judge 裁决注记读侧：键 ``wrong\\0right`` → 行；畸形行整条丢弃。"""
    raw = document.get("judge_annotations")
    if not isinstance(raw, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if not wrong or not right:
            continue
        rows[f"{wrong}\u0000{right}"] = item
    return rows


def judge_boundary_pairs(
    output_dir: str | Path, course_id: str, *, limit: int = _JUDGE_BOUNDARY_PAIR_CAP
) -> list[dict[str, Any]]:
    """``term_boundary`` 载荷组装（THINK-LADDER-2 设计 B 客户端面）。

    词对源=``term_candidates`` 视图（确认/忽略终态对天然出列）；signal≥3 的
    对已被确定性晋升排除，按合同再显式过滤 signal<3 双保险；已有 judge 裁决
    注记的对不再重问（invalid=标注折叠语义）。行形状=wrong/right/signal_count
    （signal_count 属客户端导入语义数据，worker 不消费不校验）；帽 limit。
    缺席/损坏=空列表，绝不抛。
    """
    try:
        course_id = str(course_id or "").strip()
        cap = max(0, int(limit))
        if not course_id or cap <= 0:
            return []
        document = _load_document(memory_path(output_dir, course_id), course_id)
        asked = _load_judge_annotations(document)
        rows: list[dict[str, Any]] = []
        for row in term_candidates(output_dir, course_id):
            try:
                signal = max(0, int(row.get("signal_count") or 0))
            except (TypeError, ValueError):
                signal = 0
            if signal >= _AUTO_CONFIRM_THRESHOLD:
                continue  # 合同 signal<3 筛选（≥3 晋升件正常已终态出列，双保险）
            key = f"{row['wrong']}\u0000{row['right']}"
            if key in asked:
                continue  # 已有 judge 裁决的对不重问
            rows.append({
                "wrong": str(row["wrong"]),
                "right": str(row["right"]),
                "signal_count": signal,
            })
            if len(rows) >= cap:
                break
        return rows
    except Exception:  # noqa: BLE001 - 组装是增值面，读不出=省键=旧行为
        return []


def _annotate_judge_invalid(
    output_dir: str | Path,
    course_id: str,
    *,
    wrong: str,
    right: str,
    reason_code: str,
    signal_count: int,
) -> bool:
    """invalid 裁决注记（幂等）：进顶层加性键 ``judge_annotations``（帽 100
    时间序 append，最早先出）。已注记/确认·忽略终态对=幂等 False——终态行
    是用户裁决，不再折叠（裁决至上）；本面绝不写 dismissed。"""
    course_id = str(course_id or "").strip()
    wrong = str(wrong or "").strip()
    right = str(right or "").strip()
    key = f"{wrong}\u0000{right}"
    if not course_id or not wrong or not right:
        return False
    path = memory_path(output_dir, course_id)
    document = _load_document(path, course_id)
    if key in _load_judge_annotations(document):
        return False
    terminal = {
        _pair_key(row)
        for ledger in ("confirmed_mappings", "dismissed_mappings")
        for row in _load_term_ledger(document, ledger)
    }
    if key in terminal:
        return False
    raw = document.get("judge_annotations")
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    rows.append({
        "wrong": wrong, "right": right, "ruling": "invalid",
        "reason_code": str(reason_code or ""),
        "signal_count": max(0, int(signal_count or 0)),
        "judged_at": time.time(),
    })
    document["judge_annotations"] = rows[-_MAX_JUDGE_ANNOTATIONS:]
    document["updated_at"] = time.time()
    return bool(_write_document(path, document))


def import_boundary_rulings(
    output_dir: str | Path,
    course_id: str,
    *,
    pairs: list[Any],
    rulings: Any,
) -> dict[str, int]:
    """judge 裁决导入漏斗（user-adjudication-supreme 三路）。

    ``pairs``=派发时 ``term_boundary`` 载荷（signal_count 真值源）；
    ``rulings``=worker ``term_boundary_rulings`` 输出（rulings 恰回显请求
    对、按请求序，缺位=unruled）。逐对三路：valid∧signal≥2 →
    ``confirm_term_mapping``（judge 溯源，可经 dismiss 撤销）；invalid →
    ``judge_annotations`` 注记折叠（绝不写 dismissed 终态）；unsure/缺位 →
    no-op。越集 ruling/未请求的对=skipped（worker 闭集校验后不应出现，如实
    计数不静默吞）。返回闭集计数 {confirmed, annotated, no_op, skipped}；
    任何异常按对跳过，绝不抛给质检导入主链。
    """
    receipt = {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0}
    try:
        course_id = str(course_id or "").strip()
        signal_by_pair: dict[str, int] = {}
        for item in pairs or []:
            if not isinstance(item, dict):
                continue
            wrong = str(item.get("wrong") or "").strip()
            right = str(item.get("right") or "").strip()
            if not wrong or not right:
                continue
            try:
                signal = max(0, int(item.get("signal_count") or 0))
            except (TypeError, ValueError):
                signal = 0
            signal_by_pair[f"{wrong}\u0000{right}"] = signal
        if not course_id or not signal_by_pair:
            return receipt
        raw = (rulings or {}).get("rulings") if isinstance(rulings, dict) else None
        if not isinstance(raw, list):
            return receipt
        for item in raw:
            if not isinstance(item, dict):
                receipt["skipped"] += 1
                continue
            wrong = str(item.get("wrong") or "").strip()
            right = str(item.get("right") or "").strip()
            ruling = item.get("ruling")
            reason = item.get("reason_code")
            key = f"{wrong}\u0000{right}"
            if (
                ruling not in JUDGE_BOUNDARY_RULINGS
                or reason not in JUDGE_BOUNDARY_REASON_CODES
                or key not in signal_by_pair
            ):
                receipt["skipped"] += 1
                continue
            if ruling == "unsure":
                receipt["no_op"] += 1
                continue
            if ruling == "valid":
                if signal_by_pair[key] < _JUDGE_CONFIRM_SIGNAL_MIN:
                    receipt["no_op"] += 1  # valid 但信号不足：仍留候选，下轮信号涨够再确认
                    continue
                try:
                    # 与 `_auto_confirm_high_signal_mappings` 同款直调：provenance
                    # 溯源只经模块内 `_term_mapping_action`（公开 confirm 包装
                    # 不收溯源参数，人工确认面签名不动）。
                    result = _term_mapping_action(
                        output_dir, course_id,
                        ledger_key="confirmed_mappings", stamp_key="confirmed_at",
                        flag_key="confirmed", total_key="total_confirmed",
                        wrong=wrong, right=right,
                        provenance={
                            "judge_confirmed": True,
                            "judge_reason_code": str(reason),
                            "judge_signal_count": signal_by_pair[key],
                        },
                    )
                except TermCandidateUnknown:
                    # 派发后已出候选集（用户裁决/账目淘汰）：用户裁决至上，如实跳过。
                    receipt["skipped"] += 1
                    continue
                except Exception:  # noqa: BLE001 - 单对写失败不拖垮整批
                    receipt["skipped"] += 1
                    continue
                receipt["confirmed" if result.get("confirmed") else "no_op"] += 1
                continue
            # invalid：注记折叠（幂等；已注记/已终态= no-op）。
            try:
                annotated = _annotate_judge_invalid(
                    output_dir, course_id, wrong=wrong, right=right,
                    reason_code=str(reason), signal_count=signal_by_pair[key],
                )
            except Exception:  # noqa: BLE001 - 注记失败不拖垮整批
                annotated = False
                receipt["skipped"] += 1
                continue
            receipt["annotated" if annotated else "no_op"] += 1
        return receipt
    except Exception:  # noqa: BLE001 - 导入漏斗绝不抛给质检导入本体
        return {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0}


def judge_annotation_rows(output_dir: str | Path, course_id: str) -> list[dict[str, Any]]:
    """只读视图：judge 裁决注记行（wrong/right/ruling/reason_code/signal_count/
    judged_at），judged_at 新前。消费点=复习读面候选行的加性键覆盖与
    ``judge_boundary_pairs`` 的重问过滤。缺席/损坏=空列表，绝不抛。"""
    try:
        course_id = str(course_id or "").strip()
        if not course_id:
            return []
        document = _load_document(memory_path(output_dir, course_id), course_id)
        rows: list[dict[str, Any]] = []
        for item in document.get("judge_annotations") or []:
            if not isinstance(item, dict):
                continue
            wrong = str(item.get("wrong") or "")
            right = str(item.get("right") or "")
            if not wrong or not right:
                continue
            try:
                signal = max(0, int(item.get("signal_count") or 0))
            except (TypeError, ValueError):
                signal = 0
            try:
                judged_at = float(item.get("judged_at") or 0.0)
            except (TypeError, ValueError):
                judged_at = 0.0
            rows.append({
                "wrong": wrong, "right": right,
                "ruling": str(item.get("ruling") or ""),
                "reason_code": str(item.get("reason_code") or ""),
                "signal_count": signal, "judged_at": judged_at,
            })
        rows.sort(key=lambda row: -row["judged_at"])
        return rows
    except Exception:  # noqa: BLE001 - 读侧增值面，读不出=空列表
        return []


def judge_confirmed_term_rows(
    output_dir: str | Path, course_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """THINK-LADDER-2 只读视图：judge 裁决自动确认件清单（wrong/right/信号
    次数/裁决理由码/确认时刻）。消费点=课程复习读面 term_candidates 视图的
    ``judge_confirmed_rows`` 加性键（旧消费者按名取键忽略未知键，schema 不
    变）；前端可据其一键撤销（dismiss 即撤销通道，judge 溯源件与自动晋升件
    同规）。只收带 ``judge_confirmed`` 真值的确认行（人工确认件不出列）；
    confirmed_at 升序、帽 limit；缺席/损坏=空列表，绝不抛。"""
    try:
        course_id = str(course_id or "").strip()
        cap = max(0, int(limit))
        if not course_id or cap <= 0:
            return []
        document = _load_document(memory_path(output_dir, course_id), course_id)
        raw = document.get("confirmed_mappings")
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("judge_confirmed"):
                continue
            wrong = str(item.get("wrong") or "")
            right = str(item.get("right") or "")
            if not wrong or not right:
                continue
            try:
                signal_count = max(0, int(item.get("judge_signal_count") or 0))
            except (TypeError, ValueError):
                signal_count = 0
            try:
                stamp = float(item.get("confirmed_at") or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            rows.append({
                "wrong": wrong, "right": right,
                "signal_count": signal_count,
                "reason_code": str(item.get("judge_reason_code") or ""),
                "confirmed_at": stamp,
            })
        rows.sort(key=lambda row: row["confirmed_at"])
        return rows[:cap]
    except Exception:  # noqa: BLE001 - 读侧增值面，读不出=空列表
        return []
