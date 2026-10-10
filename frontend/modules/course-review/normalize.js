/* course-review/normalize.js —— 归一化（未知字段忽略）（ARCH-DEBT-2 拆分）。
   原 course-review.js :171-519 区间纯移动；三视图 + item + localPractice +
   coverage + courseMemory 的形状契约零变化。 */

import { DOCUMENT_STATUS_SET, LOCAL_PRACTICE_SOURCE_LABEL, asArray, num, pick, str } from "./constants.js";

/* ---------------- 归一化（未知字段忽略） ---------------- */

/* HTTP 信封实测形状（N7K http_api.py + application.course_review）：
     course-review              → data = { view: <course_overview>, assessment_workspace, snapshot, state, ... }
     course-review/lecture      → data = <lecture_detail>
     course-review/assessment   → data = <assessment_workspace>
   两种形状都接受 —— 只因多包一层就静默渲染成空，是这类消费层最典型的翻车方式。 */
export function unwrapReviewView(value, viewName) {
  const raw = value && typeof value === "object" ? value : {};
  const nested = raw.view;
  if (nested && typeof nested === "object" && str(nested.view) === viewName) return nested;
  if (str(raw.view) === viewName) return raw;
  const named = raw[viewName];
  if (named && typeof named === "object" && str(named.view) === viewName) return named;
  return raw;
}

export function normalizeEvidenceRef(value) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const citationId = str(pick(raw, "citation_id", "citationId", "id"));
  if (!citationId) return null;
  const kind = str(pick(raw, "kind", "type")).toLowerCase();
  const locator = raw.locator && typeof raw.locator === "object" ? raw.locator : {};
  return {
    citationId,
    kind,
    sourceId: str(pick(raw, "source_id", "sourceId")),
    revisionId: str(pick(raw, "revision_id", "revisionId")),
    contentHash: str(pick(raw, "content_hash", "contentHash")),
    label: str(pick(raw, "label", "title")),
    locator,
    /* locator 闭集：transcript|bookmark → {start_ms,end_ms}；slide|document_page → {page}；
       assessment_item → {question_no}。全部整数锚。 */
    startMs: num(pick(locator, "start_ms", "startMs")),
    endMs: num(pick(locator, "end_ms", "endMs")),
    page: num(pick(locator, "page", "page_no", "pageNo")),
    questionNo: num(pick(locator, "question_no", "questionNo")),
  };
}

export function normalizeSourceEntry(value) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const sourceId = str(pick(raw, "source_id", "sourceId"));
  if (!sourceId) return null;
  return {
    sourceId,
    externalId: str(pick(raw, "external_id", "externalId")),
    kind: str(pick(raw, "kind", "type")).toLowerCase(),
    scope: str(raw.scope).toLowerCase(),
    subId: str(pick(raw, "sub_id", "subId")),
    revisionId: str(pick(raw, "revision_id", "revisionId")),
    label: str(pick(raw, "label", "title")),
  };
}

/* 来源索引：EvidenceRef.source_id 指向 Source.external_id（合同实测），
   同时按 source_id 兜底建键，任一命中即可。 */
export function sourceIndex(sources) {
  const index = new Map();
  asArray(sources).forEach((item) => {
    const source = normalizeSourceEntry(item);
    if (!source) return;
    if (source.externalId && !index.has(source.externalId)) index.set(source.externalId, source);
    if (!index.has(source.sourceId)) index.set(source.sourceId, source);
  });
  return index;
}

export function citationIndex(detail) {
  const index = new Map();
  asArray(detail?.evidence_refs ?? detail?.evidenceRefs).forEach((item) => {
    const ref = normalizeEvidenceRef(item);
    if (ref && !index.has(ref.citationId)) index.set(ref.citationId, ref);
  });
  return index;
}

function resolveCitations(ids, index) {
  return asArray(ids)
    .map((id) => index.get(str(id)) || null)
    .filter(Boolean);
}

function normalizeKeyPoint(value, index) {
  const raw = value && typeof value === "object" ? value : (typeof value === "string" ? { text: value } : null);
  if (!raw) return null;
  const text = str(pick(raw, "text", "title", "claim"));
  if (!text) return null;
  /* RR-ANCHORFE-1：takeaway 时间戳锚（非负整数毫秒）；缺字段/畸形 → null 不渲染 */
  const anchorMs = num(pick(raw, "anchor_ms", "anchorMs"));
  return {
    text,
    citations: resolveCitations(pick(raw, "citation_ids", "citationIds", "evidence"), index),
    rewatched: raw.rewatched === true || raw.watched_repeatedly === true,
    rewatchCount: num(pick(raw, "rewatch_count", "rewatchCount", "repeats")),
    anchorMs: anchorMs != null && anchorMs >= 0 ? Math.floor(anchorMs) : null,
  };
}

function normalizeTopic(value, index) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const title = str(pick(raw, "title", "name", "label"));
  if (!title) return null;
  const status = str(raw.status).toLowerCase();
  return {
    topicId: str(pick(raw, "topic_id", "topicId", "id")) || title,
    title,
    aliases: asArray(raw.aliases).map(str).filter(Boolean),
    lectureIds: asArray(pick(raw, "lecture_ids", "lectureIds", "sub_ids")).map(str).filter(Boolean),
    citationCount: num(pick(raw, "citation_count", "citationCount")),
    citations: resolveCitations(pick(raw, "citation_ids", "citationIds", "evidence"), index),
    status: status === "ready" || status === "partial" ? status : "",
  };
}

/* 覆盖度是**扁平计数器**（合同原文），不是嵌套对象；只展示真实计数，绝不换算百分比。 */
const COURSE_COVERAGE_ROWS = Object.freeze([
  ["lectures_ready", "讲次已就绪", ""],
  ["lectures_partial", "讲次部分就绪", ""],
  ["lectures_stale", "讲次待更新", ""],
  ["lectures_total", "讲次总数", ""],
  ["transcript_segments", "字幕片段", ""],
  ["slide_pages", "课件页", ""],
  ["document_pages", "课次资料页", ""],
  ["course_document_pages", "课程资料页", ""],
  ["assessment_items", "题目", ""],
  ["bookmarks", "书签", ""],
]);
/* 讲次级 8 键 = 后 6 个计数器 + 两个布尔（合同原文）。 */
const LECTURE_COVERAGE_ROWS = Object.freeze([
  ["transcript_segments", "字幕片段", ""],
  ["slide_pages", "课件页", ""],
  ["document_pages", "课次资料页", ""],
  ["course_document_pages", "课程资料页", ""],
  ["assessment_items", "题目", ""],
  ["bookmarks", "书签", ""],
  ["lecture_ir", "讲次结构", ""],
  ["summary", "本讲总结", ""],
]);

export function normalizeCoverage(value, rows = COURSE_COVERAGE_ROWS) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return [];
  const result = [];
  rows.forEach(([key, label]) => {
    const raw2 = raw[key];
    if (typeof raw2 === "boolean") {
      result.push({ key, label, have: raw2 ? 1 : 0, total: 1, boolean: true });
      return;
    }
    const count = num(raw2);
    if (count == null) return;
    result.push({ key, label, have: count, total: null });
  });
  return result;
}

export function normalizeLecture(value) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const subId = str(pick(raw, "sub_id", "subId"));
  if (!subId) return null;
  const status = str(raw.status).toLowerCase();
  return {
    subId,
    subTitle: str(pick(raw, "sub_title", "subTitle", "title")) || subId,
    status: DOCUMENT_STATUS_SET.has(status) ? status : "",
    rawStatus: status,
    staleReasons: asArray(pick(raw, "stale_reasons", "staleReasons")).map(str).filter(Boolean),
    topicIds: asArray(pick(raw, "topic_ids", "topicIds")).map(str).filter(Boolean),
    keyPointCount: num(pick(raw, "key_point_count", "keyPointCount")),
    coverage: normalizeCoverage(raw.source_coverage ?? raw.coverage, LECTURE_COVERAGE_ROWS),
    updatedAt: pick(raw, "updated_at", "updatedAt") || "",
  };
}

export function normalizeAssessmentItem(value) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const itemId = str(pick(raw, "item_id", "itemId", "id"));
  if (!itemId) return null;
  const source = str(pick(raw, "answer_source", "answerSource")).toLowerCase();
  const answerText = str(pick(raw, "answer", "official_answer", "answer_text"));
  /* 合同 v1 本身不含答案字段；N7A 的 assessment_view 才额外带 answer/has_answer。
     has_answer 以后端给出为准（N7A：有答案且来源非 none 才为真）；后端没给才按
     答案文本推断 —— 但绝不因为「来源没认出来」就把真实存在的答案说成不存在。 */
  const explicit = typeof raw.has_answer === "boolean" ? raw.has_answer
    : (typeof raw.hasAnswer === "boolean" ? raw.hasAnswer : null);
  const hasAnswer = explicit === null ? (Boolean(answerText) && source !== "none") : explicit;
  return {
    itemId,
    label: str(pick(raw, "label", "title")) || itemId,
    questionNo: num(pick(raw, "question_no", "questionNo")),
    kind: str(pick(raw, "kind", "type")).toLowerCase() || "assessment_item",
    subId: str(pick(raw, "sub_id", "subId")),
    documentId: str(pick(raw, "document_id", "documentId")),
    contentHash: str(pick(raw, "content_hash", "contentHash")),
    citationIds: asArray(pick(raw, "citation_ids", "citationIds")).map(str).filter(Boolean),
    answerSource: source,
    hasAnswer: hasAnswer && Boolean(answerText),
    answerText: hasAnswer ? answerText : "",
    aiExplanation: str(pick(raw, "ai_explanation", "aiExplanation")),
    /* 只有带文档身份的题才问得到 AI 解答：本地练习（quiz:）的「答案」就是课程
       字幕，后端也会 fail-closed 拒掉，所以前端不摆这个按钮。 */
    aiState: str(pick(raw, "ai_state", "aiState")).toLowerCase(),
    aiCitations: asArray(pick(raw, "ai_citations", "aiCitations")).map(normalizeEvidenceRef).filter(Boolean),
    aiErrorCode: str(pick(raw, "ai_error_code", "aiErrorCode")),
    canExplainAi: Boolean(str(pick(raw, "document_id", "documentId"))),
  };
}

/* local_practice：课程级「本课程练习」分组（讲次级本地回忆题）。
   这个视图里**没有答案字段**——答案提交前不下发是后端保证的硬事实，不是前端约定。 */
export function normalizeLocalPractice(value) {
  const raw = value && typeof value === "object" ? value : null;
  if (!raw) return null;
  const counts = raw.counts && typeof raw.counts === "object" ? raw.counts : {};
  return {
    view: str(raw.view),
    label: str(raw.label) || "本课程练习",
    sourceLabel: str(pick(raw, "source_label", "sourceLabel")) || LOCAL_PRACTICE_SOURCE_LABEL,
    counts: {
      total: num(pick(counts, "total")) || 0,
      answered: num(pick(counts, "answered")) || 0,
      wrong: num(pick(counts, "wrong")) || 0,
      unanswered: num(pick(counts, "unanswered")) || 0,
      lectures: num(pick(counts, "lectures")) || 0,
    },
    lectures: asArray(raw.lectures).map((entry) => {
      const item = entry && typeof entry === "object" ? entry : null;
      if (!item) return null;
      const subId = str(pick(item, "sub_id", "subId"));
      return {
        subId,
        label: str(pick(item, "label", "title")) || "这一讲",
        count: num(pick(item, "count")) || 0,
        answered: num(pick(item, "answered")) || 0,
        wrong: num(pick(item, "wrong")) || 0,
      };
    }).filter(Boolean),
    items: asArray(raw.items).map((entry) => {
      const item = entry && typeof entry === "object" ? entry : null;
      if (!item) return null;
      const quizId = str(pick(item, "quiz_id", "quizId"));
      if (!quizId) return null;
      const evidence = item.evidence && typeof item.evidence === "object" ? item.evidence : {};
      return {
        quizId,
        subId: str(pick(item, "sub_id", "subId")),
        lectureLabel: str(pick(item, "lecture_label", "lectureLabel")),
        question: str(item.question),
        difficulty: str(item.difficulty) || "medium",
        answered: item.answered === true,
        wrong: num(pick(item, "wrong")) || 0,
        attempts: num(pick(item, "attempts")) || 0,
        startMs: num(pick(evidence, "start_ms", "startMs")),
      };
    }).filter(Boolean),
    emptyAction: raw.empty_action && typeof raw.empty_action === "object" ? {
      action: str(raw.empty_action.action),
      label: str(raw.empty_action.label) || "去生成本课程练习",
      hint: str(raw.empty_action.hint),
      subId: str(pick(raw.empty_action, "sub_id", "subId")),
    } : null,
  };
}

/* course_overview：合同键 + 视图级 assessment 计数。 */
export function normalizeCourseOverview(value) {
  const raw = unwrapReviewView(value, "course_overview");
  return {
    contract: str(raw.contract),
    view: str(raw.view),
    courseId: str(pick(raw, "course_id", "courseId")),
    courseTitle: str(pick(raw, "course_title", "courseTitle", "title")),
    status: DOCUMENT_STATUS_SET.has(str(raw.status).toLowerCase()) ? str(raw.status).toLowerCase() : "",
    rawStatus: str(raw.status).toLowerCase(),
    inputHash: str(pick(raw, "input_hash", "inputHash")),
    staleReasons: asArray(pick(raw, "stale_reasons", "staleReasons")).map(str).filter(Boolean),
    coverage: normalizeCoverage(raw.coverage),
    topics: asArray(raw.topics).map((item) => normalizeTopic(item, new Map())).filter(Boolean),
    lectures: asArray(raw.lectures).map(normalizeLecture).filter(Boolean),
    sources: asArray(raw.sources).map(normalizeSourceEntry).filter(Boolean),
    assessment: {
      total: num(pick(raw.assessment || {}, "total")) || 0,
      lecturesWithItems: num(pick(raw.assessment || {}, "lectures_with_items", "lecturesWithItems")) || 0,
    },
    updatedAt: pick(raw, "updated_at", "updatedAt") || "",
  };
}

/* lecture_detail：含 evidence_refs 与 key_points。 */
export function normalizeLectureDetail(value) {
  const raw = unwrapReviewView(value, "lecture_detail");
  const index = citationIndex(raw);
  const status = str(raw.status).toLowerCase();
  const subId = str(pick(raw, "sub_id", "subId"));
  return {
    contract: str(raw.contract),
    view: str(raw.view),
    courseId: str(pick(raw, "course_id", "courseId")),
    subId,
    status: DOCUMENT_STATUS_SET.has(status) ? status : "",
    rawStatus: status,
    staleReasons: asArray(pick(raw, "stale_reasons", "staleReasons")).map(str).filter(Boolean),
    /* 合同的 transcript locator 只有 {start_ms,end_ms}，不含讲次；引用所属讲次
       就是本条 detail 自身的 sub_id。这里挂上归属，供跳转定位使用。 */
    keyPoints: asArray(pick(raw, "key_points", "keyPoints"))
      .map((item) => {
        const point = normalizeKeyPoint(item, index);
        return point ? { ...point, subId } : null;
      })
      .filter(Boolean),
    topics: asArray(raw.topics).map((item) => normalizeTopic(item, index)).filter(Boolean),
    evidenceRefs: Array.from(index.values()),
    coverage: normalizeCoverage(raw.source_coverage ?? raw.coverage, LECTURE_COVERAGE_ROWS),
    updatedAt: pick(raw, "updated_at", "updatedAt") || "",
  };
}

/* assessment_workspace：题目引用视图，无答案字段。 */
export function normalizeAssessmentWorkspace(value) {
  const raw = unwrapReviewView(value, "assessment_workspace");
  return {
    contract: str(raw.contract),
    view: str(raw.view),
    courseId: str(pick(raw, "course_id", "courseId")),
    items: asArray(pick(raw, "items", "assessment_items", "assessmentItems")).map(normalizeAssessmentItem).filter(Boolean),
    counts: {
      total: num(pick(raw.counts || {}, "total")) || 0,
      courseLevel: num(pick(raw.counts || {}, "course_level", "courseLevel")) || 0,
      lectures: num(pick(raw.counts || {}, "lectures")) || 0,
    },
    /* 本课程练习是独立分组：题目条目不进 items（那是作业/真题的 AssessmentItem
       引用视图，合同里没有答案字段），也不与真题混算总数。 */
    localPractice: normalizeLocalPractice(pick(raw, "local_practice", "localPractice")),
    updatedAt: pick(raw, "updated_at", "updatedAt") || "",
  };
}

/* COURSEMEM-1：课程记忆计数。只认 course_memory.examples 的非负整数，
   其余一律 null（不渲染芯片）。 */
export function normalizeCourseMemory(value) {
  const count = num(pick(value || {}, "course_memory", "courseMemory")?.examples);
  return count > 0 ? { examples: count } : null;
}
