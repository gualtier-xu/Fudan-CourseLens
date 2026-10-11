/* 多视图复习包（RR-P2MULTI-1 零 LLM 三视图 + P2-CONTRACT-1 LLM 四档）。
 *
 * 职责边界（对齐 course-review.js 纪律）：只做 fetch / render / state。
 *  - 数据源 = 既有产物：lecture_chapters / timestamp_summary / lecture_ir /
 *    review_views（走既有 apiV3 artifacts 端点，200 + artifact:null 空载荷
 *    合同，C3）。零 LLM 三视图全靠既有产物重编排；LLM 四档（提纲/FAQ/考前
 *    简报 + 时间轴事件线）来自 review_views 产物（schema v1 冻结），产物
 *    缺席时四档 tab 不出现、零 LLM 版照常——学生无感降级，绝不出现失败轰炸。
 *  - 挂载：学习页「复习」页签面板顶部的独立 section（既有导航位，零
 *    index.html 改动）；组合根归 app.js installers。本模块不 import study.js
 *    （避免循环依赖）：seek 直取播放器媒体元素，与转写行同法。
 *  - 锚点纪律（沿用 study.js quizEvidenceAnchor 家规）：毫秒锚非合法非负整数
 *    一律 null，条目保持可读不可点，绝不猜测。
 *  - 降级：全源缺（或全部加载失败）→ 整节隐藏，零噪音；部分缺 → 只渲染
 *    有数据的视图，单视图时隐藏切换条；无锚条目不渲染跳转控件。
 *  - 样式全部走本包 p2m- 前缀类（本包独占新文件 review-multiview.css，
 *    由本模块注入 <link>），复用全局 token；零 pages.css 改动。
 */

import { apiV3 } from "./api.js";
import { $, clear, textElement, toast } from "./ui.js";

const PACK_SECTION_ID = "p2m-review-pack";
const CSS_NODE_ID = "p2m-review-pack-style";
const CSS_HREF = "/styles/review-multiview.css";
const VIEW_ORDER = ["timeline", "chapters", "qa", "study_guide", "faq", "briefing"];
const VIEW_LABELS = {
  timeline: "时间轴",
  chapters: "章节",
  qa: "关键问答",
  study_guide: "提纲",
  faq: "FAQ",
  briefing: "考前简报",
};
/* 考核事件类别闭集（与 worker ASSESSMENT_CATEGORIES / study.js 台账同源）；
   纯显示映射，未知类别回退原文，绝不新造类别。 */
const ASSESSMENT_LABELS = Object.freeze({
  exam: "考试", resit: "补考", quiz: "小测", assignment: "作业", project: "大作业/项目",
  lab: "实验", computer_lab: "上机", attendance: "考勤", rollcall: "点名",
  schedule_change: "调课", qa_session: "答疑",
});

/* ---------------- 纯归一化（可单测；未知形状一律降级为空，绝不猜测） ---------------- */

function str(value) {
  return String(value ?? "").trim();
}

function intMs(value) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) return null;
  return value;
}

/* 与转写行同法（study.js）：分钟不封顶，超过 1 小时呈现 75:30 形态。 */
export function mmss(ms) {
  const total = Math.floor(Number(ms) / 1000);
  if (!Number.isFinite(total) || total < 0) return "";
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function normalizeChapters(content) {
  const rows = Array.isArray(content?.chapters) ? content.chapters : [];
  return rows
    .map((row) => ({
      title: str(row?.title),
      summary: str(row?.summary),
      startMs: intMs(row?.start_ms),
      endMs: intMs(row?.end_ms),
    }))
    .filter((row) => row.title || row.summary || row.startMs != null);
}

/* takeaway 锚与文本等长对齐由 learning_store 入库保证；旧工件缺锚按 null 降级。 */
export function normalizeTakeaways(content) {
  const texts = Array.isArray(content?.key_takeaways) ? content.key_takeaways : [];
  const anchors = Array.isArray(content?.takeaway_anchors) ? content.takeaway_anchors : [];
  return texts
    .map((text, index) => ({
      text: str(text),
      anchorMs: index < anchors.length ? intMs(anchors[index]) : null,
    }))
    .filter((row) => row.text);
}

/* Lecture IR 知识单元（evidence.v1）：title=主题名，content.text=要点正文；
   两者构成自测卡的问与答，全部取自既有产物，零编造。畸形单元跳过。 */
export function normalizeUnits(content) {
  const units = Array.isArray(content?.knowledge_units) ? content.knowledge_units : [];
  return units
    .map((unit) => {
      const detail = unit?.content && typeof unit.content === "object" ? unit.content : {};
      const time = unit?.time && typeof unit.time === "object" ? unit.time : {};
      return {
        title: str(unit?.title),
        text: str(detail.text || detail.summary),
        startMs: intMs(time.start_ms),
        endMs: intMs(time.end_ms),
      };
    })
    .filter((unit) => unit.title || unit.text);
}

/* P2-CONTRACT-1 ①：LLM 四档归一化（schema v1 冻结）。单档畸形整档省略
   （该档 tab 不出现），四档全缺 = null（零 LLM 三视图照常）。锚点同家规：
   非法毫秒锚 → null 可读不可点；引用 id 逐个去空去假，非法清空=诚实降级。 */
export function normalizeReviewViews(content) {
  const views = content && typeof content === "object" ? content.views : null;
  if (!views || typeof views !== "object") return null;
  const rowsOf = (tier, key) => {
    const body = views[tier];
    return body && typeof body === "object" && Array.isArray(body[key]) ? body[key] : [];
  };
  const citationIds = (row) => (Array.isArray(row?.citation_ids) ? row.citation_ids : [])
    .map((id) => str(id))
    .filter(Boolean);
  const out = {};
  const guide = rowsOf("study_guide", "items")
    .map((row) => ({
      question: str(row?.question),
      hint: str(row?.hint),
      anchorMs: intMs(row?.anchor_ms),
      citationIds: citationIds(row),
    }))
    .filter((row) => row.question);
  if (guide.length) out.study_guide = guide;
  const faq = rowsOf("faq", "items")
    .map((row) => ({
      question: str(row?.question),
      answer: str(row?.answer),
      anchorMs: intMs(row?.anchor_ms),
      citationIds: citationIds(row),
    }))
    .filter((row) => row.question && row.answer);
  if (faq.length) out.faq = faq;
  const events = rowsOf("timeline", "events")
    .map((row) => ({
      startMs: intMs(row?.start_ms),
      title: str(row?.title),
      detail: str(row?.detail),
    }))
    .filter((row) => row.startMs != null && row.title)
    .sort((a, b) => a.startMs - b.startMs);
  if (events.length) out.timeline = events;
  const briefing = views.briefing && typeof views.briefing === "object" ? views.briefing : null;
  const speedRead = str(briefing?.speed_read);
  const mustKnow = (Array.isArray(briefing?.must_know) ? briefing.must_know : [])
    .map((row) => str(row))
    .filter(Boolean);
  const examAlerts = (Array.isArray(briefing?.exam_alerts) ? briefing.exam_alerts : [])
    .map((row) => ({
      category: str(row?.category),
      title: str(row?.title),
      dueHint: str(row?.due_hint),
    }))
    .filter((row) => row.category || row.title);
  if (speedRead || mustKnow.length || examAlerts.length) {
    out.briefing = { speedRead, mustKnow, examAlerts };
  }
  return Object.keys(out).length ? out : null;
}

/* 全源可用性 → 视图闭集；空数组 = 本讲没有可呈现的复习材料（整节隐藏）。
   LLM 四档数据在档位在：产物缺席 = 三视图照常，无第二入口。 */
export function packViews(data) {
  const views = [];
  if (data.chapters.length || data.takeaways.length) views.push("timeline");
  if (data.chapters.length) views.push("chapters");
  if (data.units.length) views.push("qa");
  if (data.views?.study_guide?.length) views.push("study_guide");
  if (data.views?.faq?.length) views.push("faq");
  if (data.views?.briefing) views.push("briefing");
  return views;
}

/* ---------------- 渲染 ---------------- */

function timeButton(anchorMs, extraClass) {
  const label = mmss(anchorMs);
  const button = textElement("button", label, `p2m-time${extraClass ? ` ${extraClass}` : ""}`);
  button.type = "button";
  button.setAttribute("data-p2m-anchor", String(anchorMs));
  button.setAttribute("aria-label", `跳到 ${label}`);
  button.setAttribute("title", `跳到 ${label}`);
  return button;
}

function renderTimeline(panel, data) {
  clear(panel);
  const events = data.views?.timeline;
  if (events?.length) {
    /* P2-CONTRACT-1 ③：LLM 事件线在档即升级本 tab（复用 p2m-timeline* 与
       timeButton）；schema 保证升序，归一化再排一次只作保险。 */
    const rail = document.createElement("ol");
    rail.className = "p2m-timeline";
    events.forEach((event) => {
      const item = document.createElement("li");
      item.className = "p2m-timeline-item";
      item.append(timeButton(event.startMs, "p2m-timeline-time"));
      const body = document.createElement("div");
      body.className = "p2m-timeline-body";
      if (event.title) body.append(textElement("strong", event.title, "p2m-timeline-title"));
      if (event.detail) body.append(textElement("p", event.detail, "p2m-timeline-summary"));
      item.append(body);
      rail.append(item);
    });
    panel.append(rail);
  } else if (data.chapters.length) {
    const rail = document.createElement("ol");
    rail.className = "p2m-timeline";
    data.chapters.forEach((chapter) => {
      const item = document.createElement("li");
      item.className = "p2m-timeline-item";
      if (chapter.startMs != null) item.append(timeButton(chapter.startMs, "p2m-timeline-time"));
      const body = document.createElement("div");
      body.className = "p2m-timeline-body";
      if (chapter.title) body.append(textElement("strong", chapter.title, "p2m-timeline-title"));
      if (chapter.summary) body.append(textElement("p", chapter.summary, "p2m-timeline-summary"));
      item.append(body);
      rail.append(item);
    });
    panel.append(rail);
  }
  if (data.takeaways.length) {
    const wrap = document.createElement("section");
    wrap.className = "p2m-takeaways";
    wrap.append(textElement("h4", "关键要点", "p2m-subhead"));
    const list = document.createElement("ul");
    list.className = "p2m-takeaway-list";
    data.takeaways.forEach((item) => {
      const li = document.createElement("li");
      li.className = "p2m-takeaway";
      li.append(textElement("span", item.text, "p2m-takeaway-text"));
      if (item.anchorMs != null) li.append(timeButton(item.anchorMs, "p2m-takeaway-time"));
      list.append(li);
    });
    wrap.append(list);
    panel.append(wrap);
  }
}

function renderChapters(panel, data) {
  clear(panel);
  const wrap = document.createElement("div");
  wrap.className = "p2m-chapters";
  data.chapters.forEach((chapter) => {
    const card = document.createElement("article");
    card.className = "p2m-chapter";
    const headRow = document.createElement("header");
    headRow.className = "p2m-chapter-head";
    if (chapter.startMs != null) headRow.append(timeButton(chapter.startMs, "p2m-chapter-time"));
    headRow.append(textElement("h4", chapter.title || "未命名章节", "p2m-chapter-title"));
    card.append(headRow);
    if (chapter.summary) card.append(textElement("p", chapter.summary, "p2m-chapter-summary"));
    wrap.append(card);
  });
  panel.append(wrap);
}

function renderQa(panel, data) {
  clear(panel);
  panel.append(textElement("p", "先自己想想，再展开对照；点时间可回到老师原话。", "p2m-qa-hint"));
  const wrap = document.createElement("div");
  wrap.className = "p2m-qa-list";
  data.units.forEach((unit) => {
    const expandable = Boolean(unit.text) || unit.startMs != null;
    const question = unit.title || "本讲关键点";
    if (!expandable) {
      /* 无正文无锚的单元：诚实呈现为静态条目，不做可展开的空卡。 */
      wrap.append(textElement("div", question, "p2m-qa-static"));
      return;
    }
    const card = document.createElement("details");
    card.className = "p2m-qa-card";
    const summary = document.createElement("summary");
    summary.className = "p2m-qa-q";
    summary.append(textElement("span", question, "p2m-qa-q-text"));
    card.append(summary);
    const body = document.createElement("div");
    body.className = "p2m-qa-a";
    if (unit.text) body.append(textElement("p", unit.text, "p2m-qa-a-text"));
    if (unit.startMs != null) body.append(timeButton(unit.startMs, "p2m-qa-time"));
    card.append(body);
    wrap.append(card);
  });
  panel.append(wrap);
}

/* ---------------- P2-CONTRACT-1 LLM 四档渲染（全复用既有卡式） ---------------- */

/* 引用 chip：条目锚合法 → 可点跳 seek（handleSeekClick 依 data-p2m-anchor
   委派）；无锚 → 纯文本（可读不可点）。序号是条目内位置，不外指任何
   引用清单——诚实呈现「这句话有出处」，不虚构标签。 */
function citationChips(item) {
  const row = document.createElement("div");
  row.className = "p2m-cite-row";
  item.citationIds.forEach((id, index) => {
    const label = `引用 ${index + 1}`;
    if (item.anchorMs != null) {
      const chip = timeButton(item.anchorMs, "p2m-cite");
      chip.textContent = label;
      const stamp = mmss(item.anchorMs);
      chip.setAttribute("aria-label", `跳到出处 ${stamp}`);
      chip.setAttribute("title", `跳到出处 ${stamp}`);
      row.append(chip);
    } else {
      row.append(textElement("span", label, "p2m-cite-text"));
    }
  });
  return row;
}

/* 提纲 = 自测卡（复用 p2m-qa-*）：问句在卡头，思路提示在展开体——是提示
   不是答案，不作答案直显；无提示无引用的卡诚实呈现静态条目。 */
function renderStudyGuide(panel, data) {
  clear(panel);
  panel.append(textElement("p", "先自己想想，再展开看思路提示；点「引用」可回到老师原话。", "p2m-qa-hint"));
  const wrap = document.createElement("div");
  wrap.className = "p2m-qa-list";
  data.views.study_guide.forEach((item) => {
    const expandable = Boolean(item.hint) || item.citationIds.length || item.anchorMs != null;
    if (!expandable) {
      wrap.append(textElement("div", item.question, "p2m-qa-static"));
      return;
    }
    const card = document.createElement("details");
    card.className = "p2m-qa-card";
    const summary = document.createElement("summary");
    summary.className = "p2m-qa-q";
    summary.append(textElement("span", item.question, "p2m-qa-q-text"));
    card.append(summary);
    const body = document.createElement("div");
    body.className = "p2m-qa-a";
    if (item.hint) body.append(textElement("p", item.hint, "p2m-qa-a-text"));
    if (item.citationIds.length) body.append(citationChips(item));
    else if (item.anchorMs != null) body.append(timeButton(item.anchorMs, "p2m-qa-time"));
    card.append(body);
    wrap.append(card);
  });
  panel.append(wrap);
}

/* FAQ = 问答卡（复用 p2m-chapter* 头体结构）：问为头、答为体。 */
function renderFaq(panel, data) {
  clear(panel);
  const wrap = document.createElement("div");
  wrap.className = "p2m-chapters";
  data.views.faq.forEach((item) => {
    const card = document.createElement("article");
    card.className = "p2m-chapter";
    const headRow = document.createElement("header");
    headRow.className = "p2m-chapter-head";
    headRow.append(textElement("h4", item.question, "p2m-chapter-title"));
    card.append(headRow);
    if (item.answer) card.append(textElement("p", item.answer, "p2m-chapter-summary"));
    if (item.citationIds.length) card.append(citationChips(item));
    wrap.append(card);
  });
  panel.append(wrap);
}

/* 考前简报 = 速览段 + must_know 列表 + exam_alerts 徽标行（复用 p2m-takeaway*）。 */
function renderBriefing(panel, data) {
  clear(panel);
  const view = data.views.briefing;
  if (view.speedRead) panel.append(textElement("p", view.speedRead, "p2m-brief-speed"));
  if (view.mustKnow.length) {
    const wrap = document.createElement("section");
    wrap.className = "p2m-takeaways";
    wrap.append(textElement("h4", "必须掌握", "p2m-subhead"));
    const list = document.createElement("ul");
    list.className = "p2m-takeaway-list";
    view.mustKnow.forEach((row) => {
      const li = document.createElement("li");
      li.className = "p2m-takeaway";
      li.append(textElement("span", row, "p2m-takeaway-text"));
      list.append(li);
    });
    wrap.append(list);
    panel.append(wrap);
  }
  if (view.examAlerts.length) {
    const wrap = document.createElement("section");
    wrap.className = "p2m-takeaways";
    wrap.append(textElement("h4", "考核提醒", "p2m-subhead"));
    const list = document.createElement("ul");
    list.className = "p2m-takeaway-list";
    view.examAlerts.forEach((alert) => {
      const li = document.createElement("li");
      li.className = "p2m-takeaway";
      const label = ASSESSMENT_LABELS[alert.category] || alert.category;
      if (label) li.append(textElement("span", label, "p2m-alert-badge"));
      const detail = [alert.title, alert.dueHint].filter(Boolean).join(" · ");
      if (detail) li.append(textElement("span", detail, "p2m-takeaway-text"));
      list.append(li);
    });
    wrap.append(list);
    panel.append(wrap);
  }
}

/* ---------------- 安装 ---------------- */

function injectCss() {
  if (document.getElementById(CSS_NODE_ID)) return;
  const head = document.head;
  if (!head || typeof head.appendChild !== "function") return;
  const link = document.createElement("link");
  link.id = CSS_NODE_ID;
  link.setAttribute("rel", "stylesheet");
  link.setAttribute("href", CSS_HREF);
  head.appendChild(link);
}

export async function installReviewMultiView(store) {
  injectCss();
  const host = $("panel-review");
  const root = document.createElement("section");
  root.id = PACK_SECTION_ID;
  root.className = "p2m-pack";
  root.hidden = true;

  const head = document.createElement("header");
  head.className = "p2m-pack-head";
  head.append(textElement("h3", "多视图复习包", "p2m-pack-title"));
  const tabs = document.createElement("div");
  tabs.className = "p2m-view-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "复习包视图");
  head.append(tabs);
  root.append(head);

  const panels = {};
  VIEW_ORDER.forEach((name) => {
    const panelNode = document.createElement("div");
    panelNode.className = "p2m-view-panel";
    panelNode.id = `p2m-panel-${name}`;
    panelNode.setAttribute("role", "tabpanel");
    panelNode.setAttribute("aria-labelledby", `p2m-view-${name}`);
    panelNode.setAttribute("data-p2m-panel", name);
    panelNode.tabIndex = 0;
    panelNode.hidden = true;
    panels[name] = panelNode;
    root.append(panelNode);
  });
  root.append(textElement("p", "内容来自本讲已生成的总结与检索索引，全部在本机；点时间可跳到老师原话。", "p2m-pack-note"));

  tabs.addEventListener("click", (event) => {
    const tab = event.target?.closest?.("[data-p2m-view]");
    if (tab && tabs.contains(tab)) selectView(tab.dataset.p2mView);
  });
  tabs.addEventListener("keydown", handleViewKeydown);
  root.addEventListener("click", handleSeekClick);

  if (host) {
    const header = host.querySelector(".section-header");
    if (header?.parentNode === host) host.insertBefore(root, header.nextSibling);
    else host.appendChild(root);
  }

  const state = {
    hasLecture: false,
    view: "",
    data: { chapters: [], takeaways: [], units: [], views: null },
  };
  let epoch = 0;
  let controller = null;
  let reloadTimer = 0;

  function renderView(name) {
    if (name === "timeline") renderTimeline(panels.timeline, state.data);
    else if (name === "chapters") renderChapters(panels.chapters, state.data);
    else if (name === "qa") renderQa(panels.qa, state.data);
    else if (name === "study_guide") renderStudyGuide(panels.study_guide, state.data);
    else if (name === "faq") renderFaq(panels.faq, state.data);
    else if (name === "briefing") renderBriefing(panels.briefing, state.data);
  }

  function selectView(name, { focus = false } = {}) {
    if (!VIEW_ORDER.includes(name)) return;
    state.view = name;
    tabs.querySelectorAll("[data-p2m-view]").forEach((tab) => {
      const active = tab.dataset.p2mView === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus?.();
    });
    VIEW_ORDER.forEach((other) => {
      panels[other].hidden = other !== name;
    });
    renderView(name);
  }

  function handleViewKeydown(event) {
    const tab = event.target?.closest?.("[data-p2m-view]");
    if (!tab) return;
    const views = packViews(state.data);
    const current = views.indexOf(tab.dataset.p2mView);
    if (current < 0) return;
    let next = null;
    if (event.key === "ArrowRight") next = views[(current + 1) % views.length];
    else if (event.key === "ArrowLeft") next = views[(current - 1 + views.length) % views.length];
    else if (event.key === "Home") next = views[0];
    else if (event.key === "End") next = views[views.length - 1];
    if (next == null) return;
    event.preventDefault?.();
    selectView(next, { focus: true });
  }

  function handleSeekClick(event) {
    const button = event.target?.closest?.("[data-p2m-anchor]");
    if (!button || !root.contains(button)) return;
    const anchorMs = intMs(Number(button.dataset.p2mAnchor));
    if (anchorMs == null) return;
    const player = document.getElementById("player-stage");
    if (!player || typeof player.currentTime !== "number") return;
    try {
      player.currentTime = anchorMs / 1000;
    } catch {
      return;
    }
    toast(`已跳到 ${mmss(anchorMs)}`);
  }

  function renderPack() {
    const views = packViews(state.data);
    if (!state.hasLecture || !views.length) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    if (!views.includes(state.view)) state.view = views[0];
    clear(tabs);
    views.forEach((name) => {
      const tab = textElement("button", VIEW_LABELS[name] || name, "p2m-view-tab");
      tab.type = "button";
      tab.id = `p2m-view-${name}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("data-p2m-view", name);
      tab.setAttribute("aria-controls", `p2m-panel-${name}`);
      const active = name === state.view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      tabs.append(tab);
    });
    /* 单视图无需选择：隐藏切换条，面板直接呈现。 */
    tabs.hidden = views.length < 2;
    VIEW_ORDER.forEach((name) => {
      const available = views.includes(name);
      /* 可用视图全部重绘：隐藏面板不留上一讲陈旧 DOM。 */
      if (available) renderView(name);
      panels[name].hidden = !available || name !== state.view;
    });
  }

  async function load() {
    const lecture = store.activeLecture;
    epoch += 1;
    const myEpoch = epoch;
    controller?.abort();
    controller = new AbortController();
    if (!lecture) {
      state.hasLecture = false;
      state.data = { chapters: [], takeaways: [], units: [], views: null };
      renderPack();
      return;
    }
    const subId = encodeURIComponent(String(lecture.sub_id || ""));
    const kinds = ["lecture_chapters", "timestamp_summary", "lecture_ir", "review_views"];
    const results = await Promise.allSettled(
      kinds.map((kind) => apiV3(`artifacts?sub_id=${subId}&kind=${kind}`, { controller })),
    );
    if (myEpoch !== epoch) return;
    const contentOf = (result) => (result.status === "fulfilled" ? result.value?.artifact?.content ?? null : null);
    const chaptersContent = contentOf(results[0]);
    let summaryContent = contentOf(results[1]);
    const irContent = contentOf(results[2]);
    const viewsContent = contentOf(results[3]);
    /* 旧讲次只有 legacy lecture_summary：chapters/takeaways 的兜底源，取不到就保持空。 */
    if (!chaptersContent && !summaryContent) {
      try {
        const legacy = await apiV3(`artifacts?sub_id=${subId}&kind=lecture_summary`, { controller });
        if (myEpoch === epoch) summaryContent = legacy?.artifact?.content ?? null;
      } catch {
        /* 空态兜底：与 loadLectureIr 同纪律，缺失不打扰 */
      }
    }
    if (myEpoch !== epoch) return;
    state.hasLecture = true;
    state.data = {
      chapters: normalizeChapters(chaptersContent || summaryContent),
      takeaways: normalizeTakeaways(summaryContent),
      units: normalizeUnits(irContent),
      views: normalizeReviewViews(viewsContent),
    };
    renderPack();
  }

  function scheduleLoad() {
    window.clearTimeout(reloadTimer);
    reloadTimer = window.setTimeout(() => {
      void load();
    }, 30);
  }

  const unsubscribe = store.subscribe("activeLecture", scheduleLoad);
  /* 总结（重新）生成完成时 study.js 会广播 chapters 明细——顺带刷新复习包，
     让「刚生成完就切到复习页签」能看到材料。 */
  window.addEventListener("courselens:chapters", scheduleLoad);
  scheduleLoad();

  return function cleanup() {
    epoch += 1;
    window.clearTimeout(reloadTimer);
    window.removeEventListener("courselens:chapters", scheduleLoad);
    unsubscribe();
    controller?.abort();
  };
}
