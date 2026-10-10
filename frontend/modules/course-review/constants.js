/* course-review/constants.js —— 闭集与常量 + 小工具纯函数（ARCH-DEBT-2 拆分）。
   原 course-review.js :30-170 区间纯移动；合同闭集（N7K courselens.course-knowledge.v1）
   的唯一源仍以 CONTRACT_ID 为锚。 */

import { formatRelativeTime } from "../ui.js";

/* ---------------- 闭集与常量 ---------------- */

export const CONTRACT_ID = "courselens.course-knowledge.v1";

export const REVIEW_TABS = ["topics", "lectures", "assessment", "flashcards"];

/* 后端文档 status 闭集（合同原文）。 */
export const DOCUMENT_STATUSES = Object.freeze(["ready", "partial", "stale", "error"]);
export const DOCUMENT_STATUS_SET = new Set(DOCUMENT_STATUSES);

/* 展示态七态（F2.11）。前四态由后端直接给出，后三态由「客户端可见条件」判定；
   判定只读后端字段，不做语义推断：
 *   complete   ← status ready
 *   partial    ← status partial
 *   stale      ← status stale
 *   failed     ← status error
 *   processing ← 用户刚点过「更新课程知识」，结果尚未回来
 *   empty      ← 三种内容数组全空（真的还没有可整理的东西）
 *   conflict   ← 后端声明快照不可信（stale_reasons 含 snapshot_invalid）
 *                或返回 state_conflict；前端不自行判定“谁对谁错”。 */
export const REVIEW_DISPLAY_STATES = Object.freeze([
  "complete", "partial", "stale", "processing", "failed", "empty", "conflict", "unknown",
]);

export const STALE_REASON_TEXT = Object.freeze({
  never_built: "这门课还没有整理过",
  input_changed: "有资料发生了变化",
  transcript_missing: "还缺字幕",
  summary_missing: "还缺本讲总结",
  document_changed: "资料有更新",
  assessment_changed: "题目有更新",
  snapshot_invalid: "上一次整理的结果没能通过校验",
});

/* 答案来源闭集。以 N7A `src/runtime/assessment_ir.py:44` 的 ANSWER_SOURCES 为准
   （official / teacher_material / user_material / ai_generated / none）—— 那是真正
   会到达前端的枚举；同时容纳任务书措辞（original/teacher/user/ai），避免两套命名
   各说各话。AI 内容必须显式标识为非官方，绝不冒充官方答案。 */
export const ANSWER_SOURCES = Object.freeze({
  official: "官方答案",
  teacher_material: "教师资料",
  teacher: "教师资料",
  user_material: "我的作答",
  user: "我的作答",
  ai_generated: "AI 解答（非官方）",
  ai: "AI 解答（非官方）",
  original: "原题",
  none: "",
});
export const ANSWER_SOURCE_NONE = "暂无官方答案";
/* 有答案但来源不在闭集内：既不能冒充官方，也不能说成「没有答案」。 */
export const ANSWER_SOURCE_UNLABELLED = "来源未标注（非官方）";

export const EVIDENCE_KINDS = Object.freeze(["transcript", "slide", "document_page", "assessment_item", "bookmark"]);

/* 题目 AI 解答/解析（N8A）的五种状态 + stale。
   闭集与后端 `assessment_ir.AI_ANSWER_STAGES`（queued/running/ready/insufficient/failed）
   对齐；`stale` 不落库，是读面按当前题目身份现算出来的「这份解答对不上现在的题目」。
   空串 = 还没问过。 */
export const ASSESSMENT_AI_STATE_TEXT = Object.freeze({
  queued: "已排队，等在线计算空出来就开始。",
  running: "正在根据本课程资料生成解答…",
  ready: "AI 解答已生成（非官方，仅供理解）。",
  insufficient: "资料不足，无法根据当前课程资料回答。补齐资料后再点一次就好。",
  failed: "这次没跑成，点「重试」再试一次。",
  stale: "题目更新过了，这份解答对不上现在的题目，需要重新生成。",
});
/* WAIT-UX-1 缺进度 E6：排队/生成中的长等两态（>10s 三律档）。AI 生成时长无
   实测样本（外联面，WAIT-MEASURE-1 静态分级），按纪律只给诚实过程措辞与
   「可先离开」指引，不发明预计数字。提交超时仍未收口即换此文案。 */
export const ASSESSMENT_AI_LONG_WAIT_MS = 10000;
export const ASSESSMENT_AI_LONG_WAIT_TEXT
  = "已经提交一会儿了，生成本身要一点时间；可以先回去学习，好了这里会直接显示。";
export const ASSESSMENT_AI_ACTION_LABEL = Object.freeze({
  "": "生成 AI 解答",
  insufficient: "补齐后重试",
  failed: "重试",
  stale: "重新生成",
});
/* 后端闭集码 → 人话。像同学解释，不像系统日志。 */
export const ASSESSMENT_ANSWER_ERROR_TEXT = Object.freeze({
  ai_key_missing: "还没有配置 AI 密钥。到「设置 → AI 与处理」填一个，之后点这里就能生成。",
  assessment_item_unknown: "这道题不在本课程里，刷新一下再来。",
  assessment_item_not_explainable: "本课程练习的参考答案就是课程字幕，不用另外问 AI。",
  assessment_item_source_lost: "这道题的原始资料已经不在本机了，先把资料找回来再问。",
  assessment_item_content_stale: "题目已经更新过了，刷新后重新点一次。",
  course_review_course_unknown: "这门课不在已授权的课程目录里，先刷新课程目录。",
  course_review_action_invalid: "这个请求没能通过校验，刷新后重新点一次。",
});
/* 本地练习分组（N8A）：讲次级回忆题的诚实来源文案。 */
export const LOCAL_PRACTICE_SOURCE_LABEL = "课程字幕依据（非官方）";

export const LECTURE_BATCH = 8;
export const ASSESSMENT_BATCH = 25;
export const DETAIL_FETCH_LIMIT = 8;

/* ---------------- 小工具（纯函数） ---------------- */

export function asArray(value) {
  return Array.isArray(value) ? value : [];
}
export function str(value) {
  if (value == null) return "";
  return String(value);
}
export function num(value) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
export function pick(source, ...keys) {
  for (const key of keys) {
    if (source && source[key] != null && source[key] !== "") return source[key];
  }
  return null;
}

export function reviewTimestamp(value) {
  if (value == null || value === "") return 0;
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  const text = String(value).trim();
  if (!text) return 0;
  if (/^\d+$/.test(text)) return Number(text);
  const parsed = Date.parse(text);
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : 0;
}

export function reviewUpdatedText(updatedAt, nowSeconds = null) {
  const seconds = reviewTimestamp(updatedAt);
  if (!seconds) return "";
  const relative = formatRelativeTime(seconds, nowSeconds);
  return relative ? `知识更新于 ${relative}` : "";
}

/* RR-ANCHORFE-1：毫秒锚 → m:ss 显示，与学习页时间戳按钮同格式 */
export function anchorTimeText(ms) {
  const totalSeconds = Math.max(0, Math.floor(Number(ms) / 1000));
  return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, "0")}`;
}

export function kindLabel(kind) {
  const map = {
    transcript: "字幕", slide: "课件页", document_page: "资料页", assessment_item: "题目", bookmark: "书签",
  };
  return map[str(kind)] || (kind ? `来源（${kind}）` : "来源");
}
