/* course-review/render.js —— 渲染（ARCH-DEBT-2 拆分）。
   原 course-review.js :978-1633 区间纯移动；DOM 结构与事件接线零变化。
   循环边（显式豁免）：import data.js 的取数器（runTermCandidateAction/
   renderBody 内的按需补数与视图取数、闪卡刷新回调）—— 全部事件期调用，
   求值期零调用；data.js 反向 import 本模块的渲染回调同性质。 */

import { postV3 } from "../api.js";
import { ensureFlashcardsDeck, renderFlashcardsPanel } from "../course-flashcards.js";
import { renderTermCandidateRegion, submitTermCandidateAction } from "../quality-chip.js";
import { $, clear, textElement, toast } from "../ui.js";
import { ANSWER_SOURCE_NONE, ASSESSMENT_AI_ACTION_LABEL, ASSESSMENT_AI_LONG_WAIT_MS, ASSESSMENT_AI_LONG_WAIT_TEXT, ASSESSMENT_AI_STATE_TEXT, ASSESSMENT_ANSWER_ERROR_TEXT, ASSESSMENT_BATCH, LECTURE_BATCH, LOCAL_PRACTICE_SOURCE_LABEL, anchorTimeText, asArray, kindLabel, str } from "./constants.js";
import { ensureLectureDetails, loadAssessment, loadCourseReview, requestReviewRefresh } from "./data.js";
import { coverageText, hasReviewContent, refreshResultText, reviewStatusPresentation, staleReasonText } from "./display-state.js";
import { answerSourceLabel, evidenceJumpPlan, reviewSlice } from "./evidence.js";
import { normalizeEvidenceRef, sourceIndex } from "./normalize.js";
import { nav, reviewState, storeRef } from "./state.js";

/* ---------------- 渲染 ---------------- */

function el(tag, className, text) {
  return textElement(tag, text, className);
}

/* EMPTY-STATES-1：面板空态=一句话+动作钮（.empty-state/.empty-action 既有族，
   学习签名面 div>p+钮 同款，零新增设计语言）。动作只挂真实可达链路：这里统一走既有
   「更新课程知识」请求（requestReviewRefresh，busy 防重在 data.js），绝不造死按钮。 */
function emptyStateWithAction(panel, message, actionLabel, onAction) {
  const box = el("div", "empty-state");
  box.append(el("p", "", message));
  const action = el("button", "empty-action", actionLabel);
  action.type = "button";
  action.addEventListener("click", onAction);
  box.append(action);
  panel.append(box);
}

/* 来源索引按 overview 缓存：renderTopics/renderLectures 会查很多次。 */
let sourceIndexCache = { key: null, index: new Map() };
function overviewSourceIndex() {
  const sources = reviewState.overview?.sources || [];
  if (sourceIndexCache.key !== sources) {
    sourceIndexCache = { key: sources, index: sourceIndex(sources) };
  }
  return sourceIndexCache.index;
}

function sourceOf(ref) {
  return overviewSourceIndex().get(ref?.sourceId) || null;
}

/* 讲次标题：合同的 course_overview / lecture_detail 都不带 sub_title，只有 sub_id；
   人话标题在课程目录里。这是「同一身份的显示名」，不是内容推断 —— 目录里没有
   才退回 sub_id 本身。 */
function courseLectureTitle(subId) {
  const key = str(subId);
  if (!key) return "";
  const found = (storeRef?.activeCourse?.lectures || [])
    .find((item) => String(item.sub_id || "") === key);
  return str(found?.sub_title) || key;
}

function lectureTitle(lecture) {
  const direct = str(lecture?.subTitle);
  if (direct && direct !== str(lecture?.subId)) return direct;
  return courseLectureTitle(lecture?.subId);
}

export function renderSurface() {
  const surface = $("course-review-surface");
  if (!surface) return;
  surface.hidden = !reviewState.open;
  if (!reviewState.open) return;
  const title = $("course-review-course");
  if (title) title.textContent = reviewState.courseTitle || "";
  renderStatus();
  renderTermCandidates();
  renderBody();
}

/* THINK-LADDER-2：课程记忆复核区渲染 + 动作接线。动作成功后重读概览
   （term_candidates 视图随响应刷新）；失败的人话由 quality-chip 闭集码
   文案表承担（toast），绝不静默吞掉学生的点击。 */
function renderTermCandidates() {
  renderTermCandidateRegion(
    $("term-candidate-region"),
    reviewState.termCandidates,
    { onAction: (kind, row) => runTermCandidateAction(kind, row) },
  );
}

async function runTermCandidateAction(kind, row) {
  const ok = await submitTermCandidateAction(reviewState.courseId, kind, row);
  if (ok && reviewState.open) loadCourseReview();
}

function renderStatus(explicit) {
  const node = $("course-review-status");
  if (!node) return;
  const overview = reviewState.overview;
  const hasContent = hasReviewContent(overview);
  if (reviewState.errorCode && !explicit) {
    // VA-P3-06：无内容可看≠出错。danger 红只留给「有上一版内容但这次没读
    // 出来」的降级展示；第一次还没内容时用 caution 金（警告语义），不把
    // 「还没就绪」吓成「出错」。
    node.dataset.tone = hasContent ? "danger" : "caution";
    node.dataset.state = "unavailable";
    node.textContent = reviewState.loading
      ? "正在读取课程知识…"
      : hasContent
        ? "这次的内容没能读出来，先给你看上一版可用的复习内容。稍后再试一次。"
        : "暂时没读到这门课的复习内容。可以点「更新课程知识」重新整理，或稍后再试。";
    node.hidden = false;
  } else {
    const presentation = reviewStatusPresentation(overview || {}, { refreshing: reviewState.refreshing });
    node.dataset.tone = explicit ? "busy" : presentation.tone;
    node.dataset.state = presentation.state;
    node.textContent = explicit || presentation.message || "";
    node.hidden = !node.textContent;
  }
  const refresh = $("course-review-refresh");
  if (refresh) {
    const presentation = reviewStatusPresentation(overview || {}, { refreshing: reviewState.refreshing });
    refresh.hidden = Boolean(reviewState.errorCode) ? false : !presentation.showRefresh;
  }
  renderRefreshResult();
}

function renderRefreshResult() {
  const node = $("course-review-refresh-result");
  if (!node) return;
  let text = "";
  if (reviewState.refreshResult?.failed) {
    text = "这次更新没有安排成功。稍后再试一次；如果一直这样，先看看网络是否正常。";
  } else if (reviewState.refreshResult) {
    text = refreshResultText(reviewState.refreshResult);
  }
  node.textContent = text;
  node.hidden = !text;
}

function renderCoverage() {
  const node = $("course-review-coverage");
  if (!node) return;
  clear(node);
  const rows = (reviewState.overview?.coverage || []).filter((row) => row.key !== "lectures_total");
  const memoryCount = reviewState.courseMemory?.examples || 0;
  if (!rows.length && !(memoryCount > 0)) { node.hidden = true; return; }
  node.hidden = false;
  rows.forEach((row) => {
    const item = el("span", "cr-coverage-item");
    item.append(el("strong", "", row.label), el("span", "", String(row.have)));
    node.append(item);
  });
  /* COURSEMEM-1：课程记忆芯片——真实计数，积累后才出现（0/缺省不渲染）。 */
  if (memoryCount > 0) {
    const memory = el("span", "cr-coverage-item");
    memory.title = "这门课的字幕修正会自动记下来，下次转写更准。";
    memory.append(el("strong", "", "术语记忆"), el("span", "", String(memoryCount)));
    node.append(memory);
  }
}

/* 来源按钮：有引用就一定可展开；能跳的给跳转，不能跳的显示来源文本。 */
function citationBlock(citations, lecture) {
  const wrap = el("div", "cr-citations");
  citations.forEach((ref) => {
    const source = sourceOf(ref);
    const plan = evidenceJumpPlan(ref, lecture, overviewSourceIndex());
    const row = el("div", "cr-citation-row");
    row.append(el("span", "cr-citation-label", ref.label || kindLabel(ref.kind)));
    const scope = [kindLabel(ref.kind), source?.label].filter(Boolean).join(" · ");
    if (scope) row.append(el("span", "cr-citation-kind", scope));
    if (plan.navigable) {
      const button = el("button", "cr-citation-jump", plan.target.kind === "transcript" ? "定位到这段" : "打开出处");
      button.type = "button";
      button.addEventListener("click", () => runJump(plan));
      row.append(button);
    } else {
      row.append(el("span", "cr-citation-note", plan.reason));
    }
    wrap.append(row);
  });
  return wrap;
}

function sourceButton(citations, lecture) {
  if (!citations.length) return null;
  const wrap = el("span", "cr-source-wrap");
  const button = el("button", "cr-source-button", "来源");
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  const key = citations.map((ref) => ref.citationId).join("|");
  const block = citationBlock(citations, lecture);
  block.hidden = !reviewState.openCitations.has(key);
  button.setAttribute("aria-expanded", String(!block.hidden));
  button.addEventListener("click", () => {
    const nowOpen = block.hidden;
    block.hidden = !nowOpen;
    button.setAttribute("aria-expanded", String(nowOpen));
    if (nowOpen) reviewState.openCitations.add(key);
    else reviewState.openCitations.delete(key);
  });
  wrap.append(button, block);
  return wrap;
}

function keyPointNode(point) {
  const item = el("li", "cr-point");
  item.append(el("p", "cr-point-text", point.text));
  /* RR-ANCHORFE-1：锚点芯片——回看这个结论在讲里的位置。无锚不渲染（降级）；
     跳转走既有 runJump/nav 链，跨讲次自动切换，失败时诚实 toast。 */
  if (point.anchorMs != null) {
    const timeText = anchorTimeText(point.anchorMs);
    const chip = el("button", "cr-point-anchor timestamp-button", timeText);
    chip.type = "button";
    chip.setAttribute("aria-label", `跳转到 ${timeText}`);
    chip.addEventListener("click", () => {
      runJump({
        navigable: true,
        kind: "transcript",
        target: {
          kind: "transcript",
          subId: point.subId || storeRef?.activeLecture?.sub_id || "",
          startMs: point.anchorMs,
          endMs: null,
        },
      });
    });
    item.append(chip);
  }
  /* 回看热点只说发生过的事：有重复观看计数才写「你反复看过」，否则退到
     合同 v1 确实提供的书签信号「你标过书签」。绝不说「你不会」。 */
  if (point.rewatched) {
    const count = point.rewatchCount ? `${point.rewatchCount} 次` : "多次";
    item.append(el("span", "cr-rewatch", `你反复看过（${count}）`));
  } else if ((point.citations || []).some((ref) => ref.kind === "bookmark")) {
    item.append(el("span", "cr-rewatch", "你标过书签"));
  }
  /* 引用所属讲次取主张自己的归属（合同 locator 不含讲次）；退到当前活动讲次。 */
  const source = sourceButton(point.citations, { subId: point.subId || storeRef?.activeLecture?.sub_id || "" });
  if (source) item.append(source);
  return item;
}

/* 一个 topic 的来源：按来源标签归并相同者，不同者并列 —— 前端不挑“正确”的那一个。 */
function topicSourceTags(topic, lectureTopic) {
  const citations = (lectureTopic?.citations?.length ? lectureTopic.citations : topic.citations) || [];
  const labels = new Map();
  citations.forEach((ref) => {
    const source = sourceOf(ref);
    const label = source?.label || kindLabel(ref.kind);
    if (!label) return;
    labels.set(label, (labels.get(label) || 0) + 1);
  });
  return Array.from(labels.entries()).map(([label, count]) => ({ label, count }));
}

/* 知识点主张的归属：合同自带的 linkage 就是 citation 交集 —— 课次详情里
   topic.citation_ids 与 key_point.citation_ids 共用同一套 citation_id。 */
function topicPoints(topicId) {
  const points = [];
  reviewState.details.forEach((detail) => {
    const topic = (detail.topics || []).find((item) => item.topicId === topicId);
    if (!topic) return;
    const ids = new Set(topic.citations.map((ref) => ref.citationId));
    detail.keyPoints.forEach((point) => {
      if (point.citations.some((ref) => ids.has(ref.citationId))) points.push(point);
    });
  });
  return points;
}

function renderTopics() {
  const panel = $("cr-panel-topics");
  if (!panel) return;
  clear(panel);
  const topics = reviewState.overview?.topics || [];
  if (!topics.length) {
    emptyStateWithAction(
      panel,
      "这门课还没有形成知识脉络。资料齐了之后会自动出现。",
      "更新课程知识",
      () => { void requestReviewRefresh(); },
    );
    return;
  }
  const filter = reviewState.topicFilter;
  const shown = filter ? topics.filter((topic) => topic.topicId === filter) : topics;
  if (topics.length > 1) {
    const bar = el("div", "cr-filter-bar");
    const all = el("button", "cr-chip" + (filter ? "" : " active"), "全部");
    all.type = "button";
    all.addEventListener("click", () => { reviewState.topicFilter = ""; renderBody(); });
    bar.append(all);
    topics.forEach((topic) => {
      const chip = el("button", "cr-chip" + (filter === topic.topicId ? " active" : ""), topic.title);
      chip.type = "button";
      chip.addEventListener("click", () => { reviewState.topicFilter = topic.topicId; renderBody(); });
      bar.append(chip);
    });
    panel.append(bar);
  }
  shown.forEach((topic) => {
    const section = el("section", "cr-topic");
    section.append(el("h3", "cr-topic-title", topic.title));
    if (topic.aliases.length) section.append(el("p", "cr-topic-alias", `又称：${topic.aliases.join("、")}`));
    if (topic.lectureIds.length) section.append(el("p", "cr-topic-scope", `涉及 ${topic.lectureIds.length} 个讲次`));
    /* 课程级 topic 只有引用计数；具体主张在各讲课次详情里（合同如此）。 */
    const lectureTopics = [];
    topic.lectureIds.forEach((subId) => {
      const detail = reviewState.details.get(subId);
      (detail?.topics || []).forEach((item) => {
        if (item.topicId === topic.topicId) lectureTopics.push(item);
      });
    });
    const points = topicPoints(topic.topicId);
    if (points.length) {
      const list = el("ul", "cr-point-list");
      points.forEach((point) => list.append(keyPointNode(point)));
      section.append(list);
    } else if (topic.citationCount) {
      section.append(el("p", "cr-hint", `这个知识点有 ${topic.citationCount} 条依据，展开「按课次」视图可以看到具体主张。`));
    } else {
      section.append(el("p", "cr-hint", "这个知识点还没有整理出具体主张。"));
    }
    const tags = topicSourceTags(topic, lectureTopics[0]);
    if (tags.length) {
      const bar = el("div", "cr-source-labels");
      tags.forEach((tag) => bar.append(el("span", "cr-source-tag", tag.count > 1 ? `${tag.label}（${tag.count}）` : tag.label)));
      section.append(bar);
    }
    panel.append(section);
  });
}

function lectureStatusLabel(status) {
  const map = { ready: "已就绪", partial: "部分就绪", stale: "待更新", error: "未成功" };
  return map[status] || "";
}

export function renderLectures() {
  const panel = $("cr-panel-lectures");
  if (!panel) return;
  clear(panel);
  const lectures = reviewState.overview?.lectures || [];
  if (!lectures.length) {
    emptyStateWithAction(
      panel,
      "还没有整理到讲次。生成字幕后这里会出现每一讲的要点。",
      "更新课程知识",
      () => { void requestReviewRefresh(); },
    );
    return;
  }
  const { visible, remaining } = reviewSlice(lectures, LECTURE_BATCH, reviewState.expandLectures);
  visible.forEach((lecture) => {
    const card = el("article", "cr-lecture");
    const head = el("header", "cr-lecture-head");
    head.append(el("h3", "cr-lecture-title", lectureTitle(lecture)));
    const statusText = lectureStatusLabel(lecture.status);
    if (statusText) head.append(el("span", "cr-lecture-status", statusText));
    card.append(head);
    if (lecture.coverage.length) card.append(el("p", "cr-coverage-inline", coverageText(lecture.coverage)));
    const detail = reviewState.details.get(lecture.subId);
    if (reviewState.detailLoading.has(lecture.subId)) {
      card.append(el("p", "cr-hint", "正在读取这一讲的要点…"));
    } else if (detail) {
      if (detail.topics.length) {
        card.append(el("p", "cr-lecture-topics", `知识点：${detail.topics.map((topic) => topic.title).join("、")}`));
      }
      if (detail.keyPoints.length) {
        const list = el("ul", "cr-point-list");
        detail.keyPoints.forEach((point) => list.append(keyPointNode(point)));
        card.append(list);
      } else {
        card.append(el("p", "cr-hint", "这一讲还没有要点。"));
      }
    } else if (lecture.keyPointCount) {
      card.append(el("p", "cr-hint", `这一讲有 ${lecture.keyPointCount} 条要点，暂时没能展开。`));
    } else if (reviewState.detailError === "course_review_contract_mismatch") {
      /* EMPTY-STATES-1 负面核对：原句带「后端/不受支持/版本」技术词，改为人话——
         原因说清（整理结果比客户端新），出路说清（更新客户端），零责备。 */
      card.append(el("p", "cr-hint", "这一讲的整理结果比当前客户端新，暂时展开不了要点；更新客户端后就能看到。"));
    } else {
      card.append(el("p", "cr-hint", lecture.status === "ready" ? "这一讲还没有要点。" : "这一讲还在整理中。"));
    }
    const reasons = staleReasonText(lecture.staleReasons);
    if (reasons) card.append(el("p", "cr-hint", `待更新原因：${reasons}`));
    panel.append(card);
  });
  if (remaining) {
    const more = el("button", "cr-more", `显示更多（还有 ${remaining} 讲）`);
    more.type = "button";
    more.addEventListener("click", () => {
      reviewState.expandLectures = true;
      renderLectures();
      void ensureLectureDetails();
    });
    panel.append(more);
  }
  if (remaining === 0 && !visible.every((lecture) => reviewState.details.has(lecture.subId))
    && reviewState.detailError !== "course_review_contract_mismatch") {
    /* 首屏就绪后再补齐剩余批次；不阻塞渲染。 */
    void ensureLectureDetails();
  }
}

/* 题目 AI 解答/解析（N8A）：状态优先取用户刚点出来的结果，其次后端视图。
   只有用户显式点击才会触发请求；重渲染不自动重试、不自动重问。 */
function assessmentAiOf(item) {
  const live = reviewState.assessmentAi.get(item.itemId);
  if (live) {
    return {
      state: str(live.ai_state).toLowerCase(),
      explanation: str(live.ai_explanation),
      citations: asArray(live.ai_citations).map(normalizeEvidenceRef).filter(Boolean),
      errorCode: str(live.ai_error_code),
    };
  }
  return {
    state: item.aiState || "",
    explanation: item.aiExplanation || "",
    citations: item.aiCitations || [],
    errorCode: item.aiErrorCode || "",
  };
}

function assessmentAiNode(item) {
  const ai = assessmentAiOf(item);
  const wrap = el("div", "cr-actions");
  if (ai.state === "ready") {
    /* 完成态就是内容本身：不再摆按钮，重复点也不该重复烧云。
       正文由答案/解析块负责显示，这里只补可跳转的引用。 */
    if (ai.citations.length) wrap.append(citationBlock(ai.citations, { subId: item.subId }));
    else wrap.append(el("span", "cr-hint", ASSESSMENT_AI_STATE_TEXT.ready));
    return wrap;
  }
  if (ai.state === "queued" || ai.state === "running") {
    const button = el("button", "cr-chip", "正在生成 AI 解答…");
    button.type = "button";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    /* WAIT-UX-1 缺进度 E6：排队/生成中按等待时长两态——超阈值仍没收口就换
       「可先离开」的长等提示（render 期按提交时刻现算，读屏与 sighted 同源），
       避免学生盯屏干等。 */
    const startedAt = assessmentAiStartedAt.get(item.itemId);
    const longWait = Number.isFinite(startedAt) && Date.now() - startedAt > ASSESSMENT_AI_LONG_WAIT_MS;
    wrap.append(button, el("span", "cr-hint", longWait ? ASSESSMENT_AI_LONG_WAIT_TEXT : ASSESSMENT_AI_STATE_TEXT[ai.state]));
    return wrap;
  }
  const button = el("button", "cr-chip", ASSESSMENT_AI_ACTION_LABEL[ai.state] || ASSESSMENT_AI_ACTION_LABEL[""]);
  button.type = "button";
  button.disabled = reviewState.assessmentAiBusy.has(item.itemId);
  /* 失败/资料不足要重试必须用户再点一次：这里带上 retry，让后端允许重跑。 */
  button.addEventListener("click", () => { void requestAssessmentAnswer(item, Boolean(ai.state)); });
  wrap.append(button);
  if (ai.state) wrap.append(el("span", "cr-hint", ASSESSMENT_AI_STATE_TEXT[ai.state] || ""));
  return wrap;
}

function renderAssessmentItems(panel, items) {
  if (!items.length) {
    emptyStateWithAction(
      panel,
      "还没有整理到题目。导入真题或在测验里生成的题目会汇总到这里。",
      "更新课程知识",
      () => { void requestReviewRefresh(); },
    );
    return;
  }
  const kinds = Array.from(new Set(items.map((item) => item.kind).filter(Boolean)));
  if (kinds.length > 1) {
    const bar = el("div", "cr-filter-bar");
    [["all", "全部"], ...kinds.map((kind) => [kind, kindLabel(kind)])].forEach(([value, label]) => {
      const chip = el("button", "cr-chip" + (reviewState.assessmentFilter === value ? " active" : ""), label);
      chip.type = "button";
      chip.addEventListener("click", () => { reviewState.assessmentFilter = value; renderBody(); });
      bar.append(chip);
    });
    panel.append(bar);
  }
  const filtered = reviewState.assessmentFilter === "all"
    ? items
    : items.filter((item) => item.kind === reviewState.assessmentFilter);
  const { visible, remaining } = reviewSlice(filtered, ASSESSMENT_BATCH, reviewState.expandAssessment);
  visible.forEach((item) => {
    const card = el("article", "cr-question");
    const head = el("header", "cr-question-head");
    head.append(el("h3", "cr-question-text", item.label));
    head.append(el("span", "cr-answer-source", answerSourceLabel(item)));
    card.append(head);
    /* 只报区分度信息：讲次与题号；题目类型由筛选 chips 承担，标题里不重复。 */
    const scope = [courseLectureTitle(item.subId), item.questionNo == null ? "" : `第 ${item.questionNo} 题`].filter(Boolean).join(" · ");
    if (scope) card.append(el("p", "cr-question-scope", scope));
    /* 合同 v1 不含答案字段：默认「暂无官方答案」，也不展开「标准答案」区域。
       N7A 的 assessment_view 才带答案；有答案时按来源给准确措辞——AI 的绝不写成
       「参考答案」，教师资料与我的作答也各说各的。 */
    if (item.hasAnswer && item.answerText) {
      const isAi = item.answerSource === "ai_generated" || item.answerSource === "ai";
      const details = el("details", isAi ? "cr-answer cr-ai" : "cr-answer");
      details.append(el("summary", "", isAi ? "查看 AI 解答（非官方，仅供理解）" : `查看${answerSourceLabel(item)}`));
      details.append(el("p", "cr-answer-text", item.answerText));
      card.append(details);
    } else {
      card.append(el("p", "cr-answer-none", ANSWER_SOURCE_NONE));
    }
    /* AI 解析单独成块，但正文与答案相同时不重复显示（没有材料答案时后端会把 AI
       文本升格成答案，那种情况下只留上面那一块）。 */
    if (item.aiExplanation && item.aiExplanation !== item.answerText) {
      const details = el("details", "cr-answer cr-ai");
      details.append(el("summary", "", "AI 解析（非官方，仅供理解）"));
      details.append(el("p", "cr-answer-text", item.aiExplanation));
      card.append(details);
    }
    if (item.canExplainAi) card.append(assessmentAiNode(item));
    panel.append(card);
  });
  if (remaining) {
    const more = el("button", "cr-more", `显示更多（还有 ${remaining} 道）`);
    more.type = "button";
    more.addEventListener("click", () => { reviewState.expandAssessment = true; renderBody(); });
    panel.append(more);
  }
}

/* 「本课程练习」分组（N8A）：讲次级本地回忆题的汇总入口。
   题目与作答统计都复用既有 quiz 数据；这里只给数量、按课次进入，答案提交前不下发。 */
function renderLocalPractice(panel) {
  const practice = reviewState.assessment?.localPractice;
  if (!practice) return;
  const section = el("section", "cr-lecture");
  const head = el("header", "cr-lecture-head");
  head.append(el("h3", "cr-lecture-title", practice.label));
  const counts = practice.counts || {};
  head.append(el("span", "cr-lecture-status", counts.total ? `共 ${counts.total} 题` : "还没有题"));
  section.append(head);
  if (!counts.total) {
    section.append(el("p", "cr-hint", practice.emptyAction?.hint || "还没有本课程练习。"));
    if (practice.emptyAction) {
      const wrap = el("div", "cr-actions");
      const button = el("button", "cr-chip", practice.emptyAction.label);
      button.type = "button";
      /* 复用既有生成入口：去这一讲的「测验与复习」里生成，不在这里另造一套。
         没有讲次可去时就不摆按钮（不造死按钮）。 */
      const subId = practice.emptyAction.subId;
      if (subId) {
        button.addEventListener("click", () => runJump({
          navigable: true, kind: "assessment_item", label: practice.label,
          target: { kind: "assessment", subId, sourceId: subId, questionNo: null },
        }));
        wrap.append(button);
        section.append(wrap);
      }
    }
    panel.append(section);
    return;
  }
  /* 来源说清是课程字幕、不是官方答案：与答案来源闭集同一套诚实标准。 */
  section.append(el("p", "cr-hint",
    `来源：${practice.sourceLabel}。作答记录里做错的排在最前面，提交后才显示字幕依据。`));
  const answered = counts.answered || 0;
  const wrong = counts.wrong || 0;
  section.append(el("p", "cr-question-scope",
    `已作答 ${answered} 题 · 错题 ${wrong} 道 · 未作答 ${counts.unanswered || 0} 题`));
  (practice.lectures || []).forEach((lecture) => {
    const row = el("div", "cr-lecture");
    const rowHead = el("header", "cr-lecture-head");
    rowHead.append(el("h4", "cr-lecture-title", lecture.label));
    rowHead.append(el("span", "cr-lecture-status",
      lecture.wrong ? `${lecture.count} 题 · 错 ${lecture.wrong}` : `${lecture.count} 题`));
    if (lecture.subId) {
      const button = el("button", "cr-chip", "去这一讲练");
      button.type = "button";
      button.addEventListener("click", () => runJump({
        navigable: true, kind: "assessment_item", label: lecture.label,
        target: { kind: "assessment", subId: lecture.subId, sourceId: lecture.subId, questionNo: null },
      }));
      rowHead.append(button);
    }
    row.append(rowHead);
    row.append(el("p", "cr-hint", LOCAL_PRACTICE_SOURCE_LABEL));
    section.append(row);
  });
  panel.append(section);
}

export function renderAssessment() {
  const panel = $("cr-panel-assessment");
  if (!panel) return;
  clear(panel);
  if (reviewState.assessmentError) {
    /* 失败态走共享 .error-note 紧凑卡（与闪卡失败同配方，VA-P2-03 统一），
       不再借用空态灰字大空盒——失败语义与空态可区分（VISUAL-BACKLOG-1）。 */
    panel.append(el("p", "error-note", "题目暂时没能取回来，其他视图不受影响。稍后再试一次。"));
    return;
  }
  if (reviewState.assessmentLoading || !reviewState.assessment) {
    panel.append(el("p", "cr-hint", "正在读取题目…"));
    return;
  }
  /* 真题与本地练习是两个来源，各自如实显示：一方为空不遮住另一方。 */
  renderAssessmentItems(panel, reviewState.assessment.items || []);
  renderLocalPractice(panel);
}

/* 生成 AI 解答/解析：唯一触发点是用户点击。排队/生成中/完成/资料不足/失败五种
   状态都由后端返回的 ai 视图驱动；失败不会自动重试。 */
/* WAIT-UX-1 E6：提交时刻表（itemId→epoch ms）——长等两态在 render 期按它现算；
   超时补偿重渲染让空闲等待的学生也能看到两态切换。终态即清，绝不跨题残留。 */
const assessmentAiStartedAt = new Map();
let assessmentAiLongWaitTimer = 0;
async function requestAssessmentAnswer(item, retry) {
  if (reviewState.assessmentAiBusy.has(item.itemId)) return;
  reviewState.assessmentAiBusy.add(item.itemId);
  reviewState.assessmentAi.set(item.itemId, {
    ai_state: "queued", ai_explanation: "", ai_citations: [], ai_error_code: "",
  });
  assessmentAiStartedAt.set(item.itemId, Date.now());
  if (!assessmentAiLongWaitTimer) {
    assessmentAiLongWaitTimer = setTimeout(() => {
      assessmentAiLongWaitTimer = 0;
      if (reviewState.open && reviewState.tab === "assessment") renderAssessment();
    }, ASSESSMENT_AI_LONG_WAIT_MS + 500);
  }
  renderAssessment();
  try {
    const result = await postV3("course-review/actions", {
      action: "explain_assessment",
      course_id: reviewState.courseId,
      item_id: item.itemId,
      content_hash: item.contentHash,
      retry: Boolean(retry),
    });
    const ai = result && typeof result.ai === "object" && result.ai ? result.ai : {};
    reviewState.assessmentAi.set(item.itemId, {
      ai_state: str(ai.ai_state) || str(result?.status) || "failed",
      ai_explanation: str(ai.ai_explanation),
      ai_citations: asArray(ai.ai_citations),
      ai_error_code: str(ai.ai_error_code) || str(result?.error_code),
    });
  } catch (error) {
    const code = str(error?.code || error?.message) || "course_review_answer_failed";
    reviewState.assessmentAi.set(item.itemId, {
      ai_state: "failed", ai_explanation: "", ai_citations: [], ai_error_code: code,
    });
    toast(ASSESSMENT_ANSWER_ERROR_TEXT[code] || "这次没能生成解答，稍后再点一次。", "caution");
  } finally {
    reviewState.assessmentAiBusy.delete(item.itemId);
    /* WAIT-UX-1 E6：后端 202 可先回「queued」再异步收敛——只要视图态仍在
       排队/生成中，提交时刻就保留（长等两态持续可判）；进入终态才清。 */
    const aiView = reviewState.assessmentAi.get(item.itemId);
    const stillPending = Boolean(aiView)
      && (aiView.ai_state === "queued" || aiView.ai_state === "running");
    if (!stillPending) assessmentAiStartedAt.delete(item.itemId);
    /* 全部在途收口即撤长等补偿定时器，不留悬挂计时器 */
    const anyPendingView = [...reviewState.assessmentAi.values()]
      .some((view) => view && (view.ai_state === "queued" || view.ai_state === "running"));
    if (!reviewState.assessmentAiBusy.size && !anyPendingView && assessmentAiLongWaitTimer) {
      clearTimeout(assessmentAiLongWaitTimer);
      assessmentAiLongWaitTimer = 0;
    }
    if (reviewState.open && reviewState.tab === "assessment") renderAssessment();
  }
}

export function selectReviewTab(name, { focus = false } = {}) {
  reviewState.tab = name;
  document.querySelectorAll("[role='tab'][data-review-tab]").forEach((node) => {
    const active = node.dataset.reviewTab === name;
    node.classList.toggle("active", active);
    node.setAttribute("aria-selected", String(active));
    node.tabIndex = active ? 0 : -1;
    if (active && focus && typeof node.focus === "function") node.focus({ preventScroll: true });
  });
  document.querySelectorAll("[role='tabpanel'][data-review-panel]").forEach((node) => {
    node.hidden = node.dataset.reviewPanel !== name;
  });
  const surface = $("course-review-surface");
  if (surface) surface.dataset.tab = name;
}

export function renderBody() {
  if (!reviewState.open) return;
  selectReviewTab(reviewState.tab);
  renderCoverage();
  if (reviewState.tab === "topics") {
    renderTopics();
    /* topic 视图也要依据才能显示主张与来源标签：按需补齐课次详情。 */
    void ensureLectureDetails().then(() => {
      if (reviewState.open && reviewState.tab === "topics") renderTopics();
    });
  } else if (reviewState.tab === "lectures") {
    renderLectures();
    void ensureLectureDetails();
  } else if (reviewState.tab === "assessment") {
    renderAssessment();
    void loadAssessment();
  } else if (reviewState.tab === "flashcards") {
    /* 闪卡（RR-P4FSRS-1）：数据归 course-flashcards.js，本处只挂载与注入跳转。 */
    const panel = $("cr-panel-flashcards");
    renderFlashcardsPanel(panel, {
      onJump: (target) => runJump({
        navigable: true,
        kind: "transcript",
        target: { kind: "transcript", subId: target.subId, startMs: target.startMs, endMs: target.endMs },
      }),
      onRefresh: () => { void requestReviewRefresh(); },
    });
    void ensureFlashcardsDeck(reviewState.courseId).then(() => {
      if (reviewState.open && reviewState.tab === "flashcards") {
        renderFlashcardsPanel($("cr-panel-flashcards"), {
          onJump: (target) => runJump({
            navigable: true,
            kind: "transcript",
            target: { kind: "transcript", subId: target.subId, startMs: target.startMs, endMs: target.endMs },
          }),
          onRefresh: () => { void requestReviewRefresh(); },
        });
      }
    });
  }
  const surface = $("course-review-surface");
  if (surface && typeof surface.scrollTop === "number") reviewState.scrollTop = surface.scrollTop;
}

function runJump(plan) {
  const target = plan?.target;
  if (!target) return;
  let handled = false;
  if (target.kind === "transcript") handled = nav.toTranscript(target) !== false;
  else if (target.kind === "documents") handled = nav.toDocuments(target) !== false;
  else if (target.kind === "assessment") handled = nav.toAssessment(target) !== false;
  if (!handled) toast("这个来源暂时打不开，已经把出处标在上面了。", "caution");
}
