/* 课程总体复习（CourseKnowledge v1 消费层）— N7F
 *
 * 职责边界：只做 fetch / render / state。
 *  - 不复制后端业务推断：topic 合并、状态判定、覆盖度、来源归属一律取后端字段。
 *  - 未知字段忽略；未知枚举诚实降级（不假装 ready，也不清空可用内容）。
 *  - 不做前端裁决：一个 topic 的多个来源标签原样并列呈现，绝不挑一个当“正确”。
 *  - 导航出口由 study.js 经 provideReviewNavigation 注入 —— 本模块不 import
 *    study.js，避免循环依赖，也保证「证据怎么跳」只有一个所有者。
 *
 * 合同（N7K 2026-09-23 01:24 冻结，courselens.course-knowledge.v1）：
 *  - status 闭集 {ready, partial, stale, error}；stale_reasons 闭集含 snapshot_invalid。
 *  - 三个 view：course_overview / lecture_detail（需 sub_id）/ assessment_workspace。
 *  - EvidenceRef.source_id 指向 Source.external_id（不是 Source.source_id），
 *    因此来源解析以 external_id 为键。
 *  - AssessmentItem **不含任何答案字段**（合同把 assessment_answer_unsupported 列为
 *    拒绝样本），所以练习视图的诚实默认是「暂无官方答案」，不展开标准答案。
 *
 * ARCH-DEBT-2 拆分后门面：常量/归一化/展示态/证据/状态/取数/渲染七族已按
 * 分区表拆至 course-review/ 子目录模块（纯移动、行为零变化）。本门面保留
 * 生命周期（openCourseReview/exitCourseReview/restoreCourseReview）与安装器
 * （installCourseReview），并 re-export 原公共导出面（study.js 与行为测试的
 * 具名导入零变化）。 */


import { resetFlashcards } from "./course-flashcards.js";
import { $, clear, toast } from "./ui.js";
import { str } from "./course-review/constants.js";
import { loadCourseReview, requestReviewRefresh } from "./course-review/data.js";
import { reviewTabTarget } from "./course-review/evidence.js";
import { renderBody, renderSurface, selectReviewTab } from "./course-review/render.js";
import { notifyOpenChange, reviewState, setReviewStore, storeRef } from "./course-review/state.js";

/* 原公共导出面 re-export（study.js 与行为测试的具名导入零变化）。 */
export {
  ANSWER_SOURCES, ANSWER_SOURCE_NONE, ANSWER_SOURCE_UNLABELLED, ASSESSMENT_AI_ACTION_LABEL,
  ASSESSMENT_AI_STATE_TEXT, ASSESSMENT_ANSWER_ERROR_TEXT, CONTRACT_ID, DOCUMENT_STATUSES,
  EVIDENCE_KINDS, LOCAL_PRACTICE_SOURCE_LABEL, REVIEW_DISPLAY_STATES, REVIEW_TABS,
  STALE_REASON_TEXT, kindLabel, reviewTimestamp, reviewUpdatedText,
} from "./course-review/constants.js";
export {
  citationIndex, normalizeAssessmentItem, normalizeAssessmentWorkspace, normalizeCourseMemory,
  normalizeCourseOverview, normalizeEvidenceRef, normalizeLecture, normalizeLectureDetail,
  normalizeLocalPractice, normalizeSourceEntry, normalizeCoverage, unwrapReviewView, sourceIndex,
} from "./course-review/normalize.js";
export {
  coverageText, hasReviewContent, normalizeRefreshResult, refreshResultText,
  reviewDisplayState, reviewStatusPresentation, staleReasonText,
} from "./course-review/display-state.js";
export { answerSourceLabel, evidenceJumpPlan, reviewSlice, reviewTabTarget } from "./course-review/evidence.js";
export { isCourseReviewOpen, provideReviewNavigation, reviewContext } from "./course-review/state.js";

/* ---------------- 打开 / 退出 / 恢复 ---------------- */

export async function openCourseReview(store) {
  const course = store?.activeCourse;
  const courseId = str(course?.course_id);
  if (!courseId) {
    toast("请先选择一门课程，再进入总体复习。", "caution");
    return false;
  }
  const sameCourse = reviewState.courseId === courseId;
  reviewState.open = true;
  reviewState.courseId = courseId;
  reviewState.courseTitle = str(course?.title);
  if (!sameCourse) {
    /* 换课程才清空：同课程重进保留 tab/筛选/滚动位，回到原处。 */
    reviewState.tab = "topics";
    reviewState.topicFilter = "";
    reviewState.assessmentFilter = "all";
    reviewState.scrollTop = 0;
    reviewState.overview = null;
    reviewState.details = new Map();
    reviewState.detailFailed = new Set();
    reviewState.assessment = null;
    reviewState.refreshResult = null;
    reviewState.expandLectures = false;
    reviewState.expandAssessment = false;
    reviewState.openCitations = new Set();
    /* 换课程必须丢掉上一门课的 AI 结果与被点题的忙碌标记：它们按 itemId 认课程。 */
    reviewState.assessmentAi = new Map();
    reviewState.assessmentAiBusy = new Set();
  }
  notifyOpenChange();
  renderSurface();
  await loadCourseReview();
  return true;
}

export function exitCourseReview({ restoreFocus = true } = {}) {
  reviewState.open = false;
  const surface = $("course-review-surface");
  if (surface) surface.hidden = true;
  /* VA-P2-02 不变量「选中 tab⇒面板可见」：surface 藏起后不留悬空的选中
     声明（aria-selected/active 面板可见性随退出全部清位）；重开路径
     renderBody→selectReviewTab(reviewState.tab) 原位复钉，同课程重进
     保留 tab 的既有特性不受影响。 */
  document.querySelectorAll("[role='tab'][data-review-tab]").forEach((node) => {
    node.classList.remove("active");
    node.setAttribute("aria-selected", "false");
    node.tabIndex = -1;
  });
  document.querySelectorAll("[role='tabpanel'][data-review-panel]").forEach((node) => {
    node.hidden = true;
  });
  notifyOpenChange();
  window.dispatchEvent(new CustomEvent("courselens:course-review-exit"));
  if (restoreFocus) {
    const entry = $("course-review-open");
    if (entry && typeof entry.focus === "function") entry.focus({ preventScroll: true });
  }
}

/* 从字幕/讲次回到总体复习：恢复 tab / 筛选 / 滚动位（F1.9）。 */
export function restoreCourseReview(store) {
  if (!reviewState.courseId) return false;
  if (store?.activeCourse && str(store.activeCourse.course_id) !== reviewState.courseId) return false;
  reviewState.open = true;
  notifyOpenChange();
  renderSurface();
  renderBody();
  const target = $("course-review-surface");
  if (target && typeof target.scrollTo === "function" && reviewState.scrollTop) {
    target.scrollTo({ top: reviewState.scrollTop });
  } else if (target && reviewState.scrollTop) {
    target.scrollTop = reviewState.scrollTop;
  }
  return true;
}

/* ---------------- 安装 ---------------- */

export function installCourseReview(store) {
  /* ARCH-DEBT-2 缝合点（setter 形态）：storeRef 声明并驻留 course-review/state.js，
     ESM import 绑定不可跨模块赋值，经 setReviewStore 写入同一 live 变量。 */
  setReviewStore(store);
  const openButton = $("course-review-open");
  const exitButton = $("course-review-exit");
  const refreshButton = $("course-review-refresh");
  const reloadButton = $("course-review-reload");
  const tabsRoot = document.querySelector(".cr-tabs");

  const handleOpen = () => { void openCourseReview(store); };
  const handleExit = () => exitCourseReview();
  const handleRefresh = () => { void requestReviewRefresh(); };
  const handleReload = () => {
    reviewState.detailFailed.clear();
    reviewState.details.clear();
    reviewState.assessment = null;
    reviewState.assessmentError = "";
    /* 闪卡队列随「重新载入」强制刷新（换课/重试共用同一入口）。 */
    resetFlashcards("");
    /* 显式重新载入才清 AI 结果缓存：这是用户主动要一份新的读面。 */
    reviewState.assessmentAi = new Map();
    reviewState.assessmentAiBusy = new Set();
    void loadCourseReview();
  };
  const handleTabClick = (event) => {
    const button = event.target?.closest?.("[role='tab'][data-review-tab]");
    if (!button || !tabsRoot || !tabsRoot.contains(button)) return;
    selectReviewTab(button.dataset.reviewTab);
    renderBody();
  };
  const handleTabKeydown = (event) => {
    const button = event.target?.closest?.("[role='tab'][data-review-tab]");
    if (!button) return;
    const next = reviewTabTarget(button.dataset.reviewTab, String(event.key || ""));
    if (!next) return;
    event.preventDefault();
    selectReviewTab(next, { focus: true });
    renderBody();
  };
  const handleScroll = () => {
    const surface = $("course-review-surface");
    if (surface && typeof surface.scrollTop === "number") reviewState.scrollTop = surface.scrollTop;
  };
  /* Esc 退出：只在总体复习打开时生效，且不与 dialog 抢键。 */
  const handleKeydown = (event) => {
    /* IME 合成态早退（CJK-GUARD-1）：复习面在上、浮层输入框合成中按 Esc=
       取消候选，绝不当「退出复习」消费。 */
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key !== "Escape" || !reviewState.open) return;
    const dialog = document.querySelector?.("dialog[open]");
    if (dialog) return;
    exitCourseReview();
  };

  openButton?.addEventListener("click", handleOpen);
  exitButton?.addEventListener("click", handleExit);
  refreshButton?.addEventListener("click", handleRefresh);
  reloadButton?.addEventListener("click", handleReload);
  tabsRoot?.addEventListener("click", handleTabClick);
  tabsRoot?.addEventListener("keydown", handleTabKeydown);
  $("course-review-surface")?.addEventListener("scroll", handleScroll);
  window.addEventListener("keydown", handleKeydown);
  selectReviewTab(reviewState.tab);

  return () => {
    openButton?.removeEventListener("click", handleOpen);
    exitButton?.removeEventListener("click", handleExit);
    refreshButton?.removeEventListener("click", handleRefresh);
    reloadButton?.removeEventListener("click", handleReload);
    tabsRoot?.removeEventListener("click", handleTabClick);
    tabsRoot?.removeEventListener("keydown", handleTabKeydown);
    $("course-review-surface")?.removeEventListener("scroll", handleScroll);
    window.removeEventListener("keydown", handleKeydown);
  };
}
