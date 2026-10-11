/* course-review/state.js —— 模块状态与导航出口（ARCH-DEBT-2 拆分）。
   原 course-review.js :682-757 区间纯移动 + 缝合点（setReviewStore setter，
   注释钉明）。reviewState/storeRef/nav 为家族共享 live 状态：字段读写与
   重赋值全部留在本模块内，消费方经 live binding 读取。 */

/* ---------------- 模块状态与导航出口 ---------------- */

export const reviewState = {
  open: false,
  courseId: "",
  courseTitle: "",
  tab: "topics",
  topicFilter: "",
  assessmentFilter: "all",
  scrollTop: 0,
  overview: null,
  details: new Map(),
  assessment: null,
  /* COURSEMEM-1：课程记忆计数（后端 course_review 的 course_memory.examples）。
     null=本次响应没带；0=还没有积累；>0 时覆盖条里出芯片。 */
  courseMemory: null,
  /* THINK-LADDER-2：术语候选复核视图（后端 course_review 的 term_candidates
     加性视图）。null=本次响应没带/畸形；两清单全空时复核区整区隐藏。 */
  termCandidates: null,
  loading: false,
  detailLoading: new Set(),
  /* 取过且失败的讲次：不再每次重渲染都重试（否则一次永久失败会变成请求风暴）。
     只有用户显式「重新载入」才清空重试。 */
  detailFailed: new Set(),
  assessmentLoading: false,
  /* 题目 AI 解答（N8A）：itemId → 后端返回的 ai 视图。只由用户点击写入，
     重渲染绝不自动重试——失败是失败，不是待办。 */
  assessmentAi: new Map(),
  assessmentAiBusy: new Set(),
  errorCode: "",
  detailError: "",
  assessmentError: "",
  refreshBusy: false,
  refreshing: false,
  refreshResult: null,
  expandLectures: false,
  expandAssessment: false,
  openCitations: new Set(),
  loadSeq: 0,
};

export let storeRef = null;
export let nav = {
  toTranscript() { return false; },
  toDocuments() { return false; },
  toAssessment() { return false; },
  /* 打开/关闭都要让学习页联动（课程布局与 surface 互斥）。 */
  onOpenChange() {},
};

export function provideReviewNavigation(port) {
  nav = { ...nav, ...(port || {}) };
}

export function notifyOpenChange() {
  /* 通知失败不能影响 surface 自身状态：学习页只是联动方。 */
  try {
    nav.onOpenChange?.(reviewState.open);
  } catch {
    /* 联动方异常不影响课程知识视图本身 */
  }
}

export function isCourseReviewOpen() {
  return reviewState.open;
}

export function reviewContext() {
  return {
    courseId: reviewState.courseId,
    tab: reviewState.tab,
    topicFilter: reviewState.topicFilter,
    assessmentFilter: reviewState.assessmentFilter,
    scrollTop: reviewState.scrollTop,
  };
}

/* ARCH-DEBT-2 缝合点（setter 形态）：storeRef 唯一写入点原为门面安装器首行
   （原文 :1638「storeRef = store」随门面驻留）；拆分后写入经此 setter，
   渲染侧读取方持 live binding 语义不变。 */
export function setReviewStore(store) {
  storeRef = store;
}
