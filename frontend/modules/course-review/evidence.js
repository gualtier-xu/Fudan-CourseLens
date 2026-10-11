/* course-review/evidence.js —— 证据跳转计划（纯函数）（ARCH-DEBT-2 拆分）。
   原 course-review.js :627-680 区间纯移动；navigable 闭集与渐进切片零变化。 */

import { ANSWER_SOURCES, ANSWER_SOURCE_NONE, ANSWER_SOURCE_UNLABELLED, REVIEW_TABS, asArray, kindLabel, str } from "./constants.js";

/* ---------------- 证据跳转计划（纯函数） ---------------- */

/* 返回 {navigable, kind, label, target, reason}。navigable=false 时调用方只显示
   来源文本 —— 不造死按钮（F1.8）。 */
export function evidenceJumpPlan(ref, lecture, sources) {
  const kind = str(ref?.kind);
  const label = ref?.label || kindLabel(kind);
  if (!kind) {
    return { navigable: false, kind, label: label || "来源", target: null, reason: "这条引用没有可跳转的定位信息。" };
  }
  if (kind === "transcript" || kind === "bookmark") {
    if (ref.startMs == null) {
      return { navigable: false, kind, label, target: null, reason: "这条依据没有时间点，暂时无法定位。" };
    }
    return { navigable: true, kind, label, target: { kind: "transcript", subId: str(lecture?.subId), startMs: ref.startMs, endMs: ref.endMs } };
  }
  if (kind === "slide" || kind === "document_page") {
    const source = sources?.get?.(ref.sourceId) || null;
    return {
      navigable: true,
      kind,
      label,
      target: { kind: "documents", sourceId: ref.sourceId, page: ref.page, scope: source?.scope || "", subId: source?.subId || str(lecture?.subId) },
    };
  }
  if (kind === "assessment_item") {
    return { navigable: true, kind, label, target: { kind: "assessment", sourceId: ref.sourceId, questionNo: ref.questionNo } };
  }
  return { navigable: false, kind, label, target: null, reason: "这个来源暂时不能跳转，已为你标出出处。" };
}

export function answerSourceLabel(item) {
  const label = ANSWER_SOURCES[str(item?.answerSource).toLowerCase()];
  if (label) return label;
  /* 未知来源不猜：真有答案就说「来源未标注」，没有答案才说暂无官方答案。 */
  return item?.hasAnswer ? ANSWER_SOURCE_UNLABELLED : ANSWER_SOURCE_NONE;
}

/* 渐进展开切片：不改动原数组。 */
export function reviewSlice(list, limit, expanded) {
  const items = asArray(list);
  if (expanded || items.length <= limit) return { visible: items, remaining: 0 };
  return { visible: items.slice(0, limit), remaining: items.length - limit };
}

export function reviewTabTarget(current, key) {
  const index = REVIEW_TABS.indexOf(current);
  if (index < 0) return null;
  if (key === "ArrowRight") return REVIEW_TABS[(index + 1) % REVIEW_TABS.length];
  if (key === "ArrowLeft") return REVIEW_TABS[(index - 1 + REVIEW_TABS.length) % REVIEW_TABS.length];
  if (key === "Home") return REVIEW_TABS[0];
  if (key === "End") return REVIEW_TABS[REVIEW_TABS.length - 1];
  return null;
}
