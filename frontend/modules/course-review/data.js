/* course-review/data.js —— 取数（ARCH-DEBT-2 拆分）。
   原 course-review.js :837-976 区间纯移动；seq 竞态守卫/合同校验/busy 防重零变化。
   循环边（显式豁免）：import render.js 的 renderSurface/renderLectures/renderAssessment
   —— 全部为异步取数完成后的渲染回调（事件期调用，求值期零调用；ESM 函数声明
   链接期初始化保证合法），render.js 反向 import 本模块的取数器同性质。 */

import { apiV3, postV3 } from "../api.js";
import { normalizeTermCandidateView } from "../quality-chip.js";
import { $, setBusy } from "../ui.js";
import { CONTRACT_ID, DETAIL_FETCH_LIMIT, LECTURE_BATCH, str } from "./constants.js";
import { normalizeRefreshResult } from "./display-state.js";
import { reviewSlice } from "./evidence.js";
import { normalizeAssessmentWorkspace, normalizeCourseMemory, normalizeCourseOverview, normalizeLectureDetail } from "./normalize.js";
import { renderAssessment, renderLectures, renderSurface } from "./render.js";
import { reviewState } from "./state.js";

/* ---------------- 取数 ---------------- */

function contractOk(value) {
  const contract = str(value?.contract);
  return !contract || contract === CONTRACT_ID;
}

export async function loadCourseReview() {
  const seq = ++reviewState.loadSeq;
  reviewState.loading = true;
  reviewState.errorCode = "";
  reviewState.courseMemory = null;
  reviewState.termCandidates = null;
  renderSurface();
  try {
    let value;
    try {
      value = await apiV3(`course-review?course_id=${encodeURIComponent(reviewState.courseId)}`);
    } catch (error) {
      if (seq !== reviewState.loadSeq) return;
      reviewState.errorCode = str(error?.code || error?.message) || "course_review_unavailable";
      return;
    }
    if (seq !== reviewState.loadSeq) return;
    if (!contractOk(value)) {
      /* 合同不匹配：不猜字段语义，按未知状态诚实展示并保留旧内容。 */
      reviewState.errorCode = "course_review_contract_mismatch";
      return;
    }
    try {
      reviewState.overview = normalizeCourseOverview(value);
      /* COURSEMEM-1：课程记忆计数随概览响应一并取，缺省/坏值按 null 处理
         （芯片不渲染；绝不因计数坏而挡概览）。 */
      reviewState.courseMemory = normalizeCourseMemory(value);
      /* THINK-LADDER-2：术语候选复核视图随概览响应一并取（缺席/坏值=null，
         复核区整区隐藏；绝不因候选坏而挡概览）。 */
      reviewState.termCandidates = normalizeTermCandidateView(value);
      /* 后端在概览响应里已带 assessment_workspace：直接复用，省一次请求
         （合同「一个 course-review 响应足够就不要多端点」的最小 API 取向）。 */
      const inline = value && typeof value === "object" ? value.assessment_workspace : null;
      if (inline && typeof inline === "object" && !reviewState.assessment) {
        try {
          reviewState.assessment = normalizeAssessmentWorkspace(inline);
        } catch {
          /* 内联视图坏掉不影响概览：练习视图按需再取一次 */
        }
      }
    } catch {
      reviewState.errorCode = "course_review_payload_invalid";
    }
  } finally {
    if (seq === reviewState.loadSeq) {
      reviewState.loading = false;
      renderSurface();
    }
  }
}

/* 讲次详情：仅在「按课次」视图可见批次内按需取，最多 DETAIL_FETCH_LIMIT 个。 */
async function loadLectureDetail(subId) {
  const key = str(subId);
  if (!key || reviewState.details.has(key) || reviewState.detailLoading.has(key)) return;
  if (reviewState.detailFailed.has(key)) return;
  reviewState.detailLoading.add(key);
  renderLectures();
  try {
    const value = await apiV3(`course-review/lecture?course_id=${encodeURIComponent(reviewState.courseId)}&sub_id=${encodeURIComponent(key)}`);
    if (!contractOk(value)) {
      reviewState.detailError = "course_review_contract_mismatch";
      reviewState.detailFailed.add(key);
      return;
    }
    reviewState.details.set(key, normalizeLectureDetail(value));
  } catch (error) {
    reviewState.detailError = str(error?.code || error?.message) || "course_review_lecture_unavailable";
    reviewState.detailFailed.add(key);
  } finally {
    reviewState.detailLoading.delete(key);
    if (reviewState.open && reviewState.tab === "lectures") renderLectures();
  }
}

export async function ensureLectureDetails() {
  const lectures = reviewState.overview?.lectures || [];
  const { visible } = reviewSlice(lectures, LECTURE_BATCH, reviewState.expandLectures);
  const pending = visible
    .filter((lecture) => !reviewState.details.has(lecture.subId) && !reviewState.detailFailed.has(lecture.subId))
    .slice(0, DETAIL_FETCH_LIMIT);
  for (const lecture of pending) {
    if (reviewState.detailError === "course_review_contract_mismatch") return;
    await loadLectureDetail(lecture.subId);
  }
}

export async function loadAssessment() {
  if (reviewState.assessment || reviewState.assessmentLoading) return;
  reviewState.assessmentLoading = true;
  reviewState.assessmentError = "";
  renderAssessment();
  try {
    const value = await apiV3(`course-review/assessment?course_id=${encodeURIComponent(reviewState.courseId)}`);
    if (!contractOk(value)) {
      reviewState.assessmentError = "course_review_contract_mismatch";
      return;
    }
    reviewState.assessment = normalizeAssessmentWorkspace(value);
  } catch (error) {
    reviewState.assessmentError = str(error?.code || error?.message) || "course_review_assessment_unavailable";
  } finally {
    reviewState.assessmentLoading = false;
    if (reviewState.open && reviewState.tab === "assessment") renderAssessment();
  }
}

/* 「更新课程知识」：单击一次 POST refresh，busy 防重（F2.10）。 */
export async function requestReviewRefresh() {
  if (reviewState.refreshBusy || !reviewState.courseId) return;
  reviewState.refreshBusy = true;
  reviewState.refreshing = true;
  reviewState.refreshResult = null;
  const button = $("course-review-refresh");
  if (button) setBusy(button, true);
  renderSurface();
  try {
    const result = await postV3("course-review/actions", {
      action: "refresh",
      course_id: reviewState.courseId,
    });
    reviewState.refreshResult = normalizeRefreshResult(result);
  } catch (error) {
    reviewState.refreshResult = { counts: { queued: 0, skipped: 0, blocked: 0 }, reasons: [], failed: true };
    reviewState.errorCode = "";
    reviewState.refreshErrorCode = str(error?.code || error?.message) || "course_review_refresh_failed";
  } finally {
    reviewState.refreshBusy = false;
    reviewState.refreshing = false;
    if (button) setBusy(button, false);
    renderSurface();
  }
}
