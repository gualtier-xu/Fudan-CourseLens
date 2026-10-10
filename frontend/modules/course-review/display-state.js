/* course-review/display-state.js —— 展示态判定（纯函数）（ARCH-DEBT-2 拆分）。
   原 course-review.js :521-625 区间纯移动；七展示态 + 刷新结果文案零变化。 */

import { STALE_REASON_TEXT, asArray, num, pick, reviewUpdatedText, str } from "./constants.js";

/* ---------------- 展示态判定（纯函数） ---------------- */

export function hasReviewContent(overview) {
  return Boolean((overview?.topics || []).length
    || (overview?.lectures || []).length
    || (overview?.assessment?.total || 0) > 0);
}

/* 七态 + 未知降级。refreshing 由调用方传入（用户点了更新、结果未回）。 */
export function reviewDisplayState(overview, options = {}) {
  const errorCode = str(options.errorCode);
  const refreshing = options.refreshing === true;
  const status = overview?.status || "";
  const reasons = overview?.staleReasons || [];
  if (errorCode === "state_conflict" || reasons.includes("snapshot_invalid")) return "conflict";
  if (status === "error") return "failed";
  if (refreshing) return "processing";
  if (!hasReviewContent(overview)) return "empty";
  if (status === "stale") return "stale";
  if (status === "partial") return "partial";
  if (status === "ready") return "complete";
  return "unknown";
}

export function staleReasonText(reasons) {
  return asArray(reasons).map((reason) => STALE_REASON_TEXT[reason] || "").filter(Boolean).join("；");
}

/* 展示态 → 一条人话说明。语气规则：不责备学生；缺资料是客观状态，不是他的错。 */
export function reviewStatusPresentation(overview, options = {}) {
  const state = reviewDisplayState(overview, options);
  const updated = reviewUpdatedText(overview?.updatedAt, options.nowSeconds ?? null);
  const reasons = staleReasonText(overview?.staleReasons);
  const coverage = overview?.coverage || [];
  const find = (key) => coverage.find((row) => row.key === key);
  const missing = [];
  const partialRow = find("lectures_partial");
  const staleRow = find("lectures_stale");
  const readyRow = find("lectures_ready");
  const totalRow = find("lectures_total");
  if (partialRow && partialRow.have) missing.push(`${partialRow.have} 个讲次部分就绪`);
  if (staleRow && staleRow.have) missing.push(`${staleRow.have} 个讲次待更新`);
  const progress = totalRow && readyRow ? `${readyRow.have}/${totalRow.have} 个讲次已就绪` : "";

  const table = {
    complete: { tone: "ready", message: [updated || "课程知识已就绪。", progress].filter(Boolean).join(" ") },
    partial: {
      tone: "caution",
      message: ["部分内容还没整理完，能看的部分都可以正常使用。", reasons || missing.join("、"), updated].filter(Boolean).join(" "),
    },
    stale: {
      tone: "caution",
      message: ["有新资料待更新，下面仍是上一版可用的内容。", reasons, updated].filter(Boolean).join(" "),
    },
    processing: { tone: "busy", message: "正在整理这门课程的知识，整理好会自动出现在这里。" },
    failed: {
      tone: "danger",
      message: [hasReviewContent(overview) ? "这次整理没有成功，先给你看上一次的可用内容。" : "这次整理没有成功。", "稍后再试一次。", reasons].filter(Boolean).join(""),
    },
    empty: { tone: "quiet", message: "这门课程还没有可整理的内容。导入课件或生成字幕后，这里会自动长出知识脉络。" },
    conflict: {
      tone: "caution",
      message: ["这份课程知识里不同来源的说法不完全一致，已按来源并列呈现，由你自己判断。", reasons].filter(Boolean).join(" "),
    },
    unknown: {
      tone: "caution",
      message: [hasReviewContent(overview)
        ? "暂时无法确认这份课程知识的完整程度，下面是你已经能看的内容。"
        : "暂时没有读到这门课的复习内容。可以先点「更新课程知识」整理一次。", updated].filter(Boolean).join(" "),
    },
  };
  return { state, ...table[state], showRefresh: state !== "processing" };
}

/* 覆盖度文案：只报真实计数，绝不出现百分比或“掌握度”。 */
export function coverageText(rows) {
  return (rows || []).map((row) => (row.total == null ? `${row.label} ${row.have}` : `${row.label} ${row.have}/${row.total}`)).join(" · ");
}

/* 刷新结果：只读后端计数，缺失按 0，不推断。 */
export function normalizeRefreshResult(value) {
  const raw = value && typeof value === "object" ? value : {};
  return {
    counts: {
      queued: num(pick(raw, "queued", "queued_count")) || 0,
      skipped: num(pick(raw, "skipped", "skipped_count")) || 0,
      blocked: num(pick(raw, "blocked", "blocked_count")) || 0,
    },
    reasons: asArray(pick(raw, "reasons", "blocked_reasons", "notes"))
      .map((item) => (typeof item === "string" ? item : str(pick(item, "reason", "message", "text"))))
      .filter(Boolean),
  };
}

/* 不责备学生：缺字幕 / 没配 Key / 没连上都是客观状态，不是学生的错。 */
export function refreshResultText(result) {
  if (!result) return "";
  const { queued, skipped, blocked } = result.counts;
  const parts = [];
  if (queued) parts.push(`已安排 ${queued} 个讲次`);
  if (skipped) parts.push(`${skipped} 个讲次已是最新，跳过`);
  if (blocked) parts.push(`${blocked} 个讲次暂时没法整理`);
  const head = parts.length ? `${parts.join("，")}。` : "这次没有需要更新的讲次。";
  return result.reasons.length ? `${head}${result.reasons.join("；")}` : head;
}
