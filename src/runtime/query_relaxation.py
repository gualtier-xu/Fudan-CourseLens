"""查询松弛的内容词提取（D-14-RELAX R1）：中文功能词闭集 + 最长匹配切分。

缺陷 D-20261009-14：学生自然问句（「光刻胶的作用是什么？」）以原句走 FTS5
trigram 逐字 AND 检索恒零命中，深度问答即时诚实降级「资料不足」。本模块把
规范化查询切成内容词块，供检索松弛阶梯（R2）降阶重试：

    tier1 exact       原句逐字（现状行为，恒先试）
    tier2 content_and 全部内容词 AND
    tier3 content_or  逐内容词检索合并（按每条结果的命中内容词数排序）

设计来源：AI-SWEEP D-14 松弛阶梯方案（product-aiqualitysweep1-result-20261010.md，
用户已批零依赖版）。纯标准库、零新依赖；闭集常量可审可扩展。松弛只影响
「检索给不给证据」，不改任何索引/存储行为；全阶零命中仍走既有诚实降级。

闭集边界纪律：
- 只收「单独出现时不含课程内容语义」的功能词/问句框架词（含任务点名的
  「的 / 是什么 / 这节 / 核心 / 结论 / 作用」类）——领域词一律不进表。
- 子串误伤（如「作用力」被剥出「力」）由阶梯顺序兜底：tier1 恒先以原句
  精确命中，只有原句已零命中（今天=彻底拒绝）才会走松弛阶，松弛阶的最坏
  结果与现状相同（零命中→诚实降级），不存在回归面。
"""

from __future__ import annotations

import re

from src.runtime.search_index import normalize_search_text

# 中文功能词闭集（标准库常量，零依赖）。扩展时保持上面两条边界纪律；
# 匹配按表内最长短语优先，故长短语与构成它的单字可并存。
CHINESE_FUNCTION_WORDS: tuple[str, ...] = (
    # -- 问句框架（长→短） ------------------------------------------------
    "是不是", "有没有", "能不能", "会不会", "行不行", "对不对",
    "是什么", "什么是", "为什么", "咋回事", "怎么回事",
    "怎么样", "怎样", "怎么", "咋", "如何", "为何",
    "哪个", "哪些", "哪里", "哪儿", "什么样",
    "什么", "啥", "哪", "多少",
    "什么作用", "什么意思", "什么区别", "什么关系", "什么影响", "什么用",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "嘛", "啦", "呗", "咯",
    # -- 指代与课堂话语 ----------------------------------------------------
    "这节课", "这一节", "这一章", "这一堂", "本节课", "本节", "本章节",
    "上一节", "下一节", "课程里", "课堂上", "这堂课",
    "这个", "这些", "那个", "那些", "这里", "那里", "这儿", "那儿",
    "这边", "那边", "该", "此", "这", "那",
    "老师", "同学", "大家", "咱们", "我们", "你们", "他们", "它们", "自己",
    "请问", "帮我", "告诉", "介绍一下", "解释一下", "说明一下", "总结一下",
    "看一下", "说说", "讲讲",
    # -- 话语连接与虚词 ----------------------------------------------------
    "也就是说", "换句话说", "简单来说", "简单地说", "意思是", "就是说",
    "所以", "因此", "因为", "由于", "但是", "可是", "然而", "不过",
    "并且", "而且", "或者", "还是", "以及", "和", "与", "或", "及",
    "然后", "接着", "其次", "首先", "最后", "另外", "此外", "总之",
    "关于", "对于", "至于", "通过", "根据", "按照", "沿着",
    "如果", "假如", "要是", "既然", "只要", "只有", "无论", "不管",
    "一下", "一些", "有点", "一样", "时候", "的时候", "的话", "等等",
    # -- 问句套话与评价性框架词（任务点名类：核心/结论/作用等） ------------
    "核心结论", "主要结论", "关键结论", "重点内容", "主要内容",
    "核心", "结论", "作用", "重点", "意思", "要点", "大意",
    # -- 单字虚词 ----------------------------------------------------------
    "的", "了", "着", "过", "是", "有", "在", "也", "都", "就", "还",
    "才", "更", "太", "最", "再", "又", "被", "对", "让", "向", "往",
    "从", "到", "给", "跟", "同", "等", "各", "每", "把", "将", "而",
    "之", "其", "即", "很", "挺", "蛮",
)

# 按长度降序分桶，最长匹配扫描用。
_FUNCTION_WORDS_BY_LEN: dict[int, tuple[str, ...]] = {}
for _word in CHINESE_FUNCTION_WORDS:
    _FUNCTION_WORDS_BY_LEN.setdefault(len(_word), []).append(_word)
_FUNCTION_WORDS_BY_LEN = {
    _len: tuple(words)
    for _len, words in sorted(_FUNCTION_WORDS_BY_LEN.items(), reverse=True)
}
_MAX_PHRASE_LEN = max(_FUNCTION_WORDS_BY_LEN) if _FUNCTION_WORDS_BY_LEN else 0

# 内容词形态约束：与 search_index 查询上限对齐（≤8 词；≥2 字避免 q<2 报错）。
MAX_CONTENT_TERMS = 8
MIN_CONTENT_TERM_LEN = 2
# tier3 独立检索的词数上限（本地 FTS 毫秒级，仅作工作量硬帽）。
MAX_OR_TERMS = 6

_CJK_ALNUM_RUN = re.compile(r"[0-9a-z\u4e00-\u9fff]+")


def extract_content_terms(query: str) -> list[str]:
    """从（未规范化的）用户查询提取内容词块，保序去重，封顶 MAX_CONTENT_TERMS。

    流程：normalize_search_text（NFKC+casefold+空白折叠，与索引同源）→
    取 CJK/字母数字连续段（标点=分隔）→ 最长匹配剥离功能词 → 过滤
    <MIN_CONTENT_TERM_LEN_LEN 的碎块。全部被剥离或无内容时返回空表。
    """
    text = normalize_search_text(query)
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for segment in _CJK_ALNUM_RUN.findall(text):
        for piece in _split_function_words(segment):
            if len(piece) < MIN_CONTENT_TERM_LEN or piece in seen:
                continue
            seen.add(piece)
            terms.append(piece)
            if len(terms) >= MAX_CONTENT_TERMS:
                return terms
    return terms


def or_tier_terms(terms: list[str]) -> list[str]:
    """tier3 析取阶实际要逐词检索的词表：去重后按长度降序截到 MAX_OR_TERMS。"""
    unique = list(dict.fromkeys(terms))
    unique.sort(key=len, reverse=True)
    return unique[:MAX_OR_TERMS]


def _split_function_words(segment: str) -> list[str]:
    """对单个连续段做最长匹配功能词剥离，返回剩余内容块（保序）。"""
    pieces: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(segment)
    while index < length:
        matched_len = 0
        for phrase_len in range(min(_MAX_PHRASE_LEN, length - index), 0, -1):
            if segment[index : index + phrase_len] in _FUNCTION_WORDS_BY_LEN.get(phrase_len, ()):
                matched_len = phrase_len
                break
        if matched_len:
            if buffer:
                pieces.append("".join(buffer))
                buffer = []
            index += matched_len
        else:
            buffer.append(segment[index])
            index += 1
    if buffer:
        pieces.append("".join(buffer))
    return pieces


__all__ = [
    "CHINESE_FUNCTION_WORDS",
    "MAX_CONTENT_TERMS",
    "MAX_OR_TERMS",
    "MIN_CONTENT_TERM_LEN",
    "extract_content_terms",
    "or_tier_terms",
]
