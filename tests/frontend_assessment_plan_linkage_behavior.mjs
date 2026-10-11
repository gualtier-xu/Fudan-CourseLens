import assert from "node:assert/strict";

// ---- P13-B 合同 PKG-B 前端联动行为 harness（考核事件→复习计划联动，前端面）。
// ---- 与 frontend_transcript_search_behavior.mjs 同族桩约定：FakeElement 的
// ---- textContent 不聚合子节点（断言逐走 children），installStudy 的九路加载
// ---- 以闭集路由或诚实 404 承接；考核雷达挂在 artifacts 成功路径之后，故
// ---- artifacts 路由给最小真实载荷。PKG-A（后端注入）未在库：计划步骤按合同
// ---- 冻结件 2 的 schema + 尾部归一（order/estimated_minutes/status/evidence_id）
// ---- 直接构造，作为前端消费面的冻结夹具。

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  toggle(value, enabled) {
    if (enabled) this.values.add(value);
    else this.values.delete(value);
  }
}

class FakeStyle {
  constructor() {
    this.properties = new Map();
  }

  setProperty(name, value) {
    this.properties.set(String(name), String(value));
  }

  getPropertyValue(name) {
    return this.properties.has(String(name)) ? this.properties.get(String(name)) : "";
  }
}

class FakeElement extends EventTarget {
  constructor(id = "") {
    super();
    this.id = id;
    this.tagName = "";
    this.parent = null;
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.style = new FakeStyle();
  }

  /* 真 DOM 语义：className 直赋与 classList 双向同步 */
  get className() {
    return [...this.classList.values].join(" ");
  }

  set className(value) {
    this.classList.values = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  closest(selector) {
    const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
    let node = this;
    while (node) {
      for (const token of tokens) {
        const matched = token.startsWith("#")
          ? node.id === token.slice(1)
          : token.startsWith(".")
            ? Boolean(node.classList && node.classList.values && node.classList.values.has(token.slice(1)))
            : String(node.tagName || "").toLowerCase() === token.toLowerCase();
        if (matched) return node;
      }
      node = node.parent;
    }
    return null;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  /* toast 4.2s 自收口定时器会回调 node.remove() */
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((node) => node !== this);
    this.parent = null;
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

  querySelectorAll() {
    return [];
  }

  querySelector() {
    return null;
  }

  focus() {}
  blur() {}
  scrollIntoView() {}
}

class FakeCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}

function createStudyPage() {
  const ids = [
    // 学习页三态与目录
    "study-empty", "study-select", "study-desk", "study-start-select",
    "study-back-select", "study-back-courses", "study-course-list",
    "study-course-title", "study-course-meta", "study-lecture-list",
    "refresh-catalog", "catalog-term-filter", "copy-catalog-diagnostics",
    "catalog-recovery", "catalog-recovery-title", "catalog-recovery-impact",
    "catalog-recovery-actions", "catalog-evidence", "catalog-diagnostic-code",
    "diagnose-network", "topbar-crumbs",
    // 播放器动作行
    "player-stage",
    // 字幕
    "transcript-list", "reload-transcript", "transcript-mode-note",
    "transcript-search-input", "transcript-search-state", "transcript-search-results",
    // 书签
    "bookmark-list", "bookmark-action-state",
    // 资料/笔记/课件/试卷/复习/概念/分析/考试上下文
    "artifact-kind", "artifact-overview", "artifact-overview-title",
    "artifact-takeaways", "artifact-takeaways-title", "artifact-chapters",
    "artifact-chapters-title", "artifact-key-moments", "artifact-key-moment-list",
    "artifact-structured", "artifact-content", "artifact-source", "artifact-notices",
    "document-list", "document-input", "courseware-surface", "courseware-info",
    "courseware-notes", "courseware-notes-counts", "courseware-pdf-state",
    "courseware-pdf-detail", "courseware-pdf-download", "generate-courseware-pdf",
    "quiz-list", "generate-quiz", "review-list", "concept-list", "analyze-concepts",
    "analytics-summary", "exam-context", "exam-context-row", "exam-today-list",
    "exam-today-state", "ask-lecture",
    // P13-B 考核雷达两挂载点（desk 在 #exam-context 节内，summary 在总结结构化节内）
    "assessment-desk-radar", "assessment-summary-radar",
    // 云控制面与全局
    "cloud-control-card", "cloud-control-state", "cloud-control-actions",
    "course-automation-live",
    "toast-region",
  ];
  const byId = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  const tabs = new FakeElement("materials-tabs");
  tabs.classList.values.add("materials-tabs");
  const lecturePane = new FakeElement("lecture-pane");
  const bySelector = new Map([
    [".materials-tabs", tabs],
    [".course-layout > .lecture-pane", lecturePane],
  ]);
  let created = 0;
  const documentTarget = new EventTarget();
  globalThis.document = Object.assign(documentTarget, {
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => {
      const node = new FakeElement(`created-${created += 1}`);
      node.tagName = String(tag).toUpperCase();
      return node;
    },
    createElementNS: (_ns, tag) => {
      const node = new FakeElement(`created-ns-${created += 1}`);
      node.tagName = String(tag).toUpperCase();
      return node;
    },
    createDocumentFragment: () => new FakeElement(`fragment-${created += 1}`),
    querySelector: (selector) => bySelector.get(String(selector)) || null,
    querySelectorAll: () => [],
    activeElement: null,
  });
  return {
    byId,
    desk: byId["assessment-desk-radar"],
    summary: byId["assessment-summary-radar"],
    reviewList: byId["review-list"],
    todayList: byId["exam-today-list"],
    examSection: byId["exam-context"],
    toastRegion: byId["toast-region"],
  };
}

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
    tasks: [],
    /* FIRST-LOGIN-UX-2：store 记忆面桩（与真实 store.js 同名方法对齐） */
    rememberLastLecture() {},
    readLastLecture() { return null; },
    set(key, value) {
      this[key] = value;
      for (const listener of listeners.get(key) || []) listener(value);
    },
    subscribe(key, listener) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(listener);
      return () => listeners.get(key)?.delete(listener);
    },
  };
}

const windowTarget = new EventTarget();
windowTarget.setTimeout = (callback, ms) => setTimeout(callback, ms);
windowTarget.clearTimeout = (id) => clearTimeout(id);
windowTarget.requestAnimationFrame = (callback) => setImmediate(() => callback(0));
windowTarget.cancelAnimationFrame = () => {};
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
globalThis.window = windowTarget;
globalThis.CustomEvent = FakeCustomEvent;
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.matchMedia = () => ({
  matches: false,
  addEventListener: () => {},
  removeEventListener: () => {},
});
globalThis.CSS = { escape: (value) => String(value) };

/* 时间冻结：所有夹具的 due_at/exam_at 以 BASE 为锚，倒计时逐字可断言 */
const BASE_MS = Date.now();
const BASE_S = Math.floor(BASE_MS / 1000);
const DAY = 86400;
const realDateNow = Date.now;
Date.now = () => BASE_MS;

function dayText(epochSeconds) {
  const day = new Date(epochSeconds * 1000);
  return `${day.getMonth() + 1}月${day.getDate()}日`;
}

/* ---- 网络桩：考核/计划两路可注入，其余九路闭集承接 ---- */
let currentEvents = [];
let currentPlans = [];
let plansGetCount = 0;
const planPosts = [];
let planPostGate = null;
let planPostHandler = null;
/* WIRING-FIX-1：artifacts 载荷可注入（场景 D 钉雷达与 artifact 解耦）；
   确认/忽略动作 POST 记账（场景 C 钉动作流） */
let currentArtifact = { content_markdown: "本讲总结文本。" };
const assessmentActionPosts = [];

function ok(data, status = 200) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorJson(payload, status) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET").toUpperCase();
  if (method === "GET" && route.startsWith("/api/v3/assessment?")) return ok({ events: currentEvents });
  if (method === "POST" && route.startsWith("/api/v3/assessment/actions")) {
    /* WIRING-FIX-1：真路由=POST 分发器（此前误挂 GET 面前端恒 404）。
       shim 按服务层合同改台账并回读形状 */
    const body = JSON.parse(String(options.body || "{}"));
    assessmentActionPosts.push(body);
    const nextStatus = body.action === "confirm" ? "confirmed" : "dismissed";
    currentEvents = currentEvents.map((event) => (
      String(event.event_id) === String(body.event_id) ? { ...event, status: nextStatus } : event
    ));
    return ok({ schema: "courselens.assessment-ledger.v1", event_id: body.event_id, status: nextStatus });
  }
  if (method === "GET" && route.startsWith("/api/v3/review-plans")) {
    plansGetCount += 1;
    return ok({ plans: currentPlans });
  }
  if (method === "POST" && route.startsWith("/api/v3/review-plans")) {
    planPosts.push(JSON.parse(String(options.body || "{}")));
    if (planPostGate) await planPostGate.promise;
    return planPostHandler ? planPostHandler() : ok({ plan: {} });
  }
  if (route.startsWith("/api/v3/subtitles/segments?")) return ok({ segments: [] });
  if (route.startsWith("/api/v3/bookmarks?")) return ok({ bookmarks: [] });
  /* 考核雷达已与 artifacts 解耦（WIRING-FIX-1 F3-P1-1）：载荷可注入，
     场景 D 用空载荷钉「无总结讲次雷达仍渲染」 */
  if (route.startsWith("/api/v3/artifacts?")) return ok({ artifact: currentArtifact });
  if (route.startsWith("/api/v3/quizzes?")) return ok({ items: [] });
  /* 其余九路（documents/ir/concepts/analytics/courseware-pdf/automation 等）
     诚实 404：loaders 各自闭集降级，不得让安装路径崩溃 */
  return errorJson({ error: "synthetic route omitted" }, 404);
};

const { installStudy } = await import("../frontend/modules/study.js");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  /* 雷达链比字幕深一跳（artifacts 成功 → renderAssessmentRadar → assessment GET），
     多留余量避免欠汇合的假失败 */
  for (let index = 0; index < 8; index += 1) await nextTurn();
};

const COURSE = {
  course_id: "c-1",
  title: "概率统计（合成）",
  lectures: [{ sub_id: "s-1", sub_title: "第 1 讲 抽样分布", can_stream: true }],
};
const LECTURE = { course_id: "c-1", sub_id: "s-1", sub_title: "第 1 讲 抽样分布", can_stream: true };

/* 合同冻结件 2 的考核步（尾部归一后形状）：kind/reason/evidence.source 冻结、
   无毫秒锚（sub_id="" → 无跳转钮的既有降级路径） */
function assessmentStepFixture() {
  return {
    order: 1, kind: "assessment", title: "第六章线代考核",
    minutes: 15, estimated_minutes: 15, status: "planned",
    reason: "考核临近", event_id: "ev-1",
    evidence: {
      source: "assessment_event", event_id: "ev-1", category: "exam",
      due_at: BASE_S + 2 * DAY, due_bucket: "synthetic", course_id: "c-1",
    },
    evidence_id: null, course_id: "c-1", sub_id: "",
  };
}

function watchStepFixture() {
  return {
    order: 2, kind: "watch", title: "第二章回顾",
    minutes: 20, estimated_minutes: 20, status: "planned",
    reason: "章节重点",
    evidence: { source: "chapter", start_ms: 60000 },
    evidence_id: null, course_id: "c-1", sub_id: "s-1",
  };
}

function activePlanFixture() {
  return {
    plan_id: "plan-1", title: "复习计划",
    /* +1h：examRemainingText 用 floor，整 48h 会被亚秒误差踩成「1 天内」 */
    exam_at: BASE_S + 2 * DAY + 3600, exam_state: "active", exam_precision: "date",
    exam_source: "user_confirmed", daily_minutes: 30, available_minutes: 60,
    course_scope: ["c-1"],
    steps: [assessmentStepFixture(), watchStepFixture()],
    updated_at: BASE_S - 3600,
  };
}

const deskRows = (page) => page.desk.children.filter((node) => String(node.className || "").includes("assessment-event"));
const planButtonOf = (page) => page.desk.children.find(
  (node) => node.tagName === "BUTTON" && node.textContent === "把已确认的考核排进复习计划",
) || null;
const rowWith = (page, needle) => deskRows(page).find((row) => row.children.some(
  (span) => typeof span.textContent === "string" && span.textContent.includes(needle),
)) || null;

async function verifyAssessmentPlanLinkage() {
  const page = createStudyPage();
  const store = createStore();
  store.courses = [COURSE];
  const cleanup = await installStudy(store);
  store.set("activeCourse", COURSE);

  /* ---- 场景 A：确认门硬闸下的完整联动形态（验收①②③⑤） ---- */
  currentEvents = [
    { event_id: "ev-1", course_id: "c-1", category: "exam", title: "第六章线代考核", status: "confirmed", due_at: BASE_S + 2 * DAY, expired: false, last_seen_sub_id: "s-1" },
    { event_id: "ev-2", course_id: "c-1", category: "assignment", title: "第三章作业提交", status: "confirmed", due_at: BASE_S + 3 * DAY, expired: false },
    { event_id: "ev-unconfirmed", course_id: "c-1", category: "quiz", title: "第五章小测", status: "unconfirmed", due_at: BASE_S + 5 * DAY, expired: false },
    { event_id: "ev-conflict", course_id: "c-1", category: "exam", title: "日期有出入的考试", status: "confirmed", due_at: BASE_S + 6 * DAY, expired: false, conflict_note: "教务与课上说法不同" },
    { event_id: "ev-nodate", course_id: "c-1", category: "project", title: "期末大作业", status: "confirmed", due_at: null, expired: false },
    { event_id: "ev-dismissed", course_id: "c-1", category: "quiz", title: "已忽略的随堂测", status: "dismissed", due_at: BASE_S + DAY, expired: false },
    { event_id: "ev-expired", course_id: "c-1", category: "assignment", title: "已截止作业", status: "confirmed", due_at: BASE_S - DAY, expired: true },
  ];
  currentPlans = [activePlanFixture()];
  store.set("activeLecture", { ...LECTURE });
  await settle();

  /* ⑤ dismissed/expired 永不出现（既有 upcoming 过滤，联动不放宽） */
  assert.equal(deskRows(page).length, 5, "desk 只渲染未忽略未过期的事件行");
  assert.equal(rowWith(page, "已忽略的随堂测"), null, "dismissed 行不出现");
  assert.equal(rowWith(page, "已截止作业"), null, "expired 行不出现");

  /* ② desk 时间线强化：仅 confirmed+due 未过期行升级「N 天后」；conflict_note
     行、unconfirmed 行、时间待定行逐位不动 */
  const row1 = rowWith(page, "第六章线代考核");
  assert.match(row1.children[0].textContent, new RegExp(`时间 ${dayText(BASE_S + 2 * DAY)} · 2 天后`), "confirmed+due 行升级「2 天后」");
  assert.ok(row1.children.some((n) => n.textContent === "已确认"), "已确认 chip 照旧");
  const row2 = rowWith(page, "第三章作业提交");
  assert.match(row2.children[0].textContent, new RegExp(`时间 ${dayText(BASE_S + 3 * DAY)} · 3 天后`));
  const rowUnconfirmed = rowWith(page, "第五章小测");
  assert.match(rowUnconfirmed.children[0].textContent, new RegExp(`时间 ${dayText(BASE_S + 5 * DAY)}(?! ·)`), "unconfirmed 行保持纯日期");
  const rowConflict = rowWith(page, "日期有出入的考试");
  assert.match(rowConflict.children[0].textContent, new RegExp(`时间 ${dayText(BASE_S + 6 * DAY)}(?! ·)`), "conflict_note 行不升级倒计时");
  assert.ok(rowConflict.children.some((n) => n.textContent === "时间有出入，以校方考试安排为准"), "conflict_note 行说明句不动");
  const rowNoDate = rowWith(page, "期末大作业");
  assert.ok(rowNoDate.children.some((n) => n.textContent.includes("时间待定")), "无日期 confirmed 行维持「时间待定」");

  /* ① 新步渲染：考核准备徽标 + 课程 label + 理由行 + 无跳转钮（既有渲染零特判承载） */
  const planRow = page.reviewList.children.find((node) => String(node.className || "").includes("review-plan-row"));
  assert.ok(planRow, "复习计划行渲染");
  const stepsBox = planRow.children.find((node) => String(node.className || "") === "review-steps");
  const stepRows = stepsBox.children.filter((node) => String(node.className || "") === "review-step");
  assert.equal(stepRows.length, 2, "两个步骤照原序渲染（assessment 恒最前由后端保证）");
  const aStep = stepRows[0];
  assert.equal(aStep.children.find((n) => String(n.className || "") === "review-step-kind").textContent, "考核准备", "kind 徽标取词表新值");
  assert.equal(aStep.children.find((n) => String(n.className || "") === "review-step-title").textContent, "第六章线代考核", "步题原样");
  const aMeta = aStep.children.find((n) => String(n.className || "") === "review-step-meta").textContent;
  assert.match(aMeta, /概率统计（合成）/, "考核步课程 label 按目录精确映射");
  assert.match(aMeta, /预计 15 分钟/, "预计分钟渲染");
  assert.equal(aStep.children.find((n) => String(n.className || "") === "review-step-reason").textContent, "理由：考核临近", "冻结理由原样");
  assert.ok(!aStep.children.some((n) => n.tagName === "BUTTON"), "考核步无毫秒锚 → 无跳转钮");
  const wStep = stepRows[1];
  assert.ok(wStep.children.some((n) => n.tagName === "BUTTON"), "对照：带锚 watch 步仍有跳转钮（锚机制未被误伤）");

  /* ① 今天建议：assessment 步恒最前 → 预算内前缀恰好是它，同构形态 */
  const todayItems = page.todayList.children.filter((node) => String(node.className || "").includes("exam-suggestion"));
  assert.equal(todayItems.length, 1, "今日预算 30 分钟下建议=考核步（15）+watch（20）截断");
  const todayItem = todayItems[0];
  assert.equal(todayItem.children.find((n) => n.tagName === "STRONG").textContent, "第六章线代考核");
  const todayTexts = todayItem.children.map((n) => String(n.textContent));
  assert.ok(todayTexts.some((t) => t.includes("考核准备")), "今天建议 meta 带考核准备徽标");
  assert.ok(todayTexts.some((t) => t === "理由：考核临近"), "今天建议理由行冻结原样");
  assert.ok(!todayItem.children.some((n) => n.tagName === "BUTTON"), "今天建议中考核步同样无跳转钮");
  assert.match(page.byId["exam-context-row"].textContent, /距考试 2 天/, "考试窗口行照旧");

  /* ③ 「排进计划」显式动作：显示、防抖、冻结载荷、成功 toast+刷新 */
  const button = planButtonOf(page);
  assert.ok(button, "有 confirmed+due 事件 → desk 渲染排进计划钮");
  assert.ok(button.classList.values.has("text-button"), "复用既有 text-button 设计语言");
  assert.equal(page.summary.children.some((n) => n.tagName === "BUTTON" && /排进复习计划/.test(n.textContent)), false, "summary 挂载不渲染动作钮");
  assert.ok(page.summary.children.some((n) => String(n.className || "").includes("assessment-event")), "summary 仍按既有呈现当前讲次事件行");

  const gate = { promise: null, resolve: null };
  gate.promise = new Promise((done) => { gate.resolve = done; });
  planPostGate = gate;
  const getsBeforeClick = plansGetCount;
  button.click();
  assert.equal(button.disabled, true, "点击即在途禁用（防抖冻结）");
  button.click(); /* 在途连点：disabled 挡下，绝不重复 POST */
  assert.equal(planPosts.length, 1, "在途连点只发一次请求");
  gate.resolve();
  await settle();
  assert.equal(planPosts.length, 1, "响应返回后也不补发第二次");
  assert.deepEqual(planPosts[0], {
    title: "考核复习计划",
    exam_at: BASE_S + 2 * DAY,
    available_minutes: 60,
    course_id: "c-1",
    strategy: "coverage",
  }, "载荷逐字段等于合同③4 冻结形状（无 course_scope，exam_at=最临近已确认考核）");
  assert.ok(page.toastRegion.children.some((n) => n.textContent === "已生成复习计划，已确认的考核已排进步骤"), "成功 toast 人话文案");
  assert.ok(plansGetCount > getsBeforeClick, "成功后刷新 plans 装载段（load 路径同式）");
  assert.equal(page.reviewList.children.some((n) => String(n.className || "").includes("review-plan-row")), true, "刷新后计划列表仍在");
  assert.equal(planButtonOf(page).disabled, true, "成功后保持禁用至下次重渲染（不诱导重复建计划）");

  /* ③ 失败面：服务器闭集错误原样 toast（api.js 人话映射），按钮恢复可重试 */
  planPostHandler = () => errorJson({ error: "operation in flight", error_code: "operation_already_running" }, 400);
  store.set("activeLecture", { ...LECTURE }); /* 触发 desk 重渲染 → 新钮 */
  await settle();
  const retryButton = planButtonOf(page);
  assert.ok(retryButton && !retryButton.disabled, "重渲染后的新钮可用");
  retryButton.click();
  await settle();
  assert.ok(page.toastRegion.children.some((n) => n.textContent === "这次操作没能完成，请刷新页面重试"), "失败 toast 走既有闭集人话纪律");
  assert.equal(retryButton.disabled, false, "失败后按钮恢复，学生可再试");

  /* ---- 场景 B（验收④）：无日期/无事件空态 —— 无数据即无 UI ---- */
  currentEvents = [
    { event_id: "ev-nodate2", course_id: "c-1", category: "project", title: "期末大作业", status: "confirmed", due_at: null, expired: false },
    { event_id: "ev-un2", course_id: "c-1", category: "quiz", title: "第六章小测", status: "unconfirmed", due_at: BASE_S + DAY, expired: false },
  ];
  currentPlans = [];
  store.set("activeLecture", { ...LECTURE });
  await settle();
  assert.ok(deskRows(page).length >= 2, "desk 行照常渲染");
  assert.equal(planButtonOf(page), null, "无 confirmed+due 事件 → 按钮不渲染");
  assert.ok(rowWith(page, "期末大作业").children.some((n) => n.textContent.includes("时间待定")), "无日期 confirmed 行维持时间待定");
  const emptyState = page.reviewList.children.find((n) => String(n.className || "").includes("empty-state"));
  assert.ok(emptyState, "无计划空态节点在");
  assert.equal(emptyState.textContent, "暂无复习计划，课程进行一段后会自动生成", "既有空态文案一字未动");
  /* WIRING-FIX-1 F3-P1-2：desk 有内容 → 节开（旧代码被「无 active 计划」门
     恒隐，合成 DOM 上 children 断言看不见——真机 BROWSERWALK-3 A9 即此病） */
  assert.equal(page.examSection.hidden, false, "雷达有内容即开节：无 active 计划不再恒隐");

  /* ---- 场景 C（WIRING-FIX-1 F3-P1-3+F3-P1-2）：确认动作流 + 节显隐收放 ---- */
  currentEvents = [
    { event_id: "ev-c1", course_id: "c-1", category: "quiz", title: "第五章小测", status: "unconfirmed", due_at: BASE_S + 5 * DAY, expired: false },
    { event_id: "ev-c2", course_id: "c-1", category: "exam", title: "第六章考核", status: "confirmed", due_at: BASE_S + 4 * DAY, expired: false },
  ];
  store.set("activeLecture", { ...LECTURE });
  await settle();
  const unconfirmedRow = rowWith(page, "第五章小测");
  const confirmButton = unconfirmedRow.children.find((n) => n.tagName === "BUTTON" && n.textContent === "确认");
  assert.ok(confirmButton, "unconfirmed 行有确认钮");
  confirmButton.click();
  await settle();
  assert.equal(assessmentActionPosts.length, 1, "确认点击恰好发一次动作 POST");
  assert.equal(assessmentActionPosts[0].action, "confirm", "动作闭集");
  assert.equal(assessmentActionPosts[0].event_id, "ev-c1", "event_id 透传");
  assert.ok(rowWith(page, "第五章小测").children.some((n) => n.textContent === "已确认"), "成功后重渲染为已确认 chip（onChange 链活）");
  assert.equal(page.examSection.hidden, false, "仍有未忽略行 → 节保持开");
  /* 全部忽略 → desk 空 → 节收回（统一谓词的「全空即藏」半边）。
     confirmed 行只有 chip 无钮，故重置为双 unconfirmed 行各点忽略。 */
  currentEvents = [
    { event_id: "ev-c3", course_id: "c-1", category: "quiz", title: "第六章小测", status: "unconfirmed", due_at: BASE_S + 3 * DAY, expired: false },
    { event_id: "ev-c4", course_id: "c-1", category: "assignment", title: "第四章作业", status: "unconfirmed", due_at: BASE_S + 6 * DAY, expired: false },
  ];
  store.set("activeLecture", { ...LECTURE });
  await settle();
  for (const row of [...deskRows(page)]) {
    const dismiss = row.children.find((n) => n.tagName === "BUTTON" && n.textContent === "忽略");
    if (dismiss) dismiss.click();
  }
  await settle();
  assert.equal(deskRows(page).length, 0, "全部忽略后 desk 空");
  assert.equal(page.examSection.hidden, true, "全空 → 节收回（不残留空节）");

  /* ---- 场景 D（WIRING-FIX-1 F3-P1-1）：雷达与总结 artifact 解耦 ---- */
  currentArtifact = null; /* 空载荷=无总结讲次（首跑学生大多数态） */
  currentEvents = [
    { event_id: "ev-d1", course_id: "c-1", category: "exam", title: "期末考试", status: "confirmed", due_at: BASE_S + 2 * DAY, expired: false },
  ];
  store.set("activeLecture", { ...LECTURE });
  await settle();
  assert.equal(deskRows(page).length, 1, "无总结 artifact → 雷达仍渲染（旧代码挂 artifacts 成功路径=整块死）");
  assert.ok(planButtonOf(page), "一键排进按钮对无总结讲次同样可达");
  assert.equal(page.examSection.hidden, false, "节随雷达内容开");
  currentArtifact = { content_markdown: "本讲总结文本。" };

  cleanup();
}

await verifyAssessmentPlanLinkage();
Date.now = realDateNow;
console.log("frontend assessment plan linkage behavior passed");
