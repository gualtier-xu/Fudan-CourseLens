import assert from "node:assert/strict";

/* AI 学习工作台行为测试：加载真实 study.js / search-palette.js，用最小假 DOM 与
   可注入网络桩覆盖结构化总结、Lecture IR 关键时刻、测验答案揭示、复习步骤、
   考试上下文窗口、诚实课程关系与“针对本讲提问”入口。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, enabled) {
    if (enabled === undefined) {
      if (this.values.has(value)) this.values.delete(value);
      else this.values.add(value);
    } else if (enabled) this.values.add(value);
    else this.values.delete(value);
    return this.values.has(value);
  }
  contains(value) { return this.values.has(value); }
}

class FakeElement extends EventTarget {
  constructor(tagName = "div", id = "") {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.inert = false;
    this.value = "";
    this.tabIndex = -1;
    this.currentTime = 0;
    this._listeners = new Map();
  }

  addEventListener(type, listener) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this._listeners.get(type)?.delete(listener);
  }

  dispatchEvent(event) {
    if (!Object.prototype.hasOwnProperty.call(event, "target")) {
      Object.defineProperty(event, "target", { value: this, configurable: true });
    }
    for (const listener of [...(this._listeners.get(event.type) || [])]) listener.call(this, event);
    if (event.bubbles && this.parent instanceof FakeElement) this.parent.dispatchEvent(event);
    return true;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }

  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }

  removeAttribute(name) { this.attributes.delete(String(name)); }

  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }

  prepend(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.unshift(node);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    if (this.parent) {
      const index = this.parent.children.indexOf(this);
      if (index >= 0) this.parent.children.splice(index, 1);
      this.parent = null;
    }
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true }));
  }

  focus() { document.activeElement = this; }

  blur() {
    if (document.activeElement === this) document.activeElement = null;
  }

  scrollIntoView() {}

  contains(node) {
    let current = node;
    while (current) {
      if (current === this) return true;
      current = current.parent;
    }
    return false;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (matchesCompound(node, selector)) return node;
      node = node.parent;
    }
    return null;
  }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.children || []) {
        if (matchesChain(child, String(selector).split(/\s+/).filter(Boolean))) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

const FOCUSABLE_TAGS = new Set(["BUTTON", "INPUT", "SELECT", "TEXTAREA"]);

function datasetKey(name) {
  return String(name).replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

function parseCompound(selector) {
  const conditions = [];
  const re = /(^|\s)([a-zA-Z][a-zA-Z0-9-]*)|\.[A-Za-z_][\w-]*|\[([^\]]+)\]/g;
  let match;
  while ((match = re.exec(selector))) {
    if (match[2]) conditions.push({ tag: match[2].toUpperCase() });
    else if (match[0].startsWith(".")) conditions.push({ className: match[0].slice(1) });
    else if (match[3] !== undefined) {
      const inner = match[3];
      const eq = inner.indexOf("=");
      if (eq < 0) conditions.push({ attr: inner });
      else conditions.push({ attr: inner.slice(0, eq), value: inner.slice(eq + 1).replace(/^['"]|['"]$/g, "") });
    }
  }
  return conditions;
}

function matchesCompound(node, selector) {
  const text = String(selector);
  if (text.includes(":not(") || text.includes(",")) return false;
  return parseCompound(text).every((condition) => {
    if (condition.tag) return node.tagName === condition.tag;
    if (condition.className) return String(node.className || "").split(/\s+/).includes(condition.className);
    if (condition.attr) {
      const key = datasetKey(condition.attr);
      const has = node.attributes.has(condition.attr)
        || (condition.attr.startsWith("data-") && node.dataset?.[key] !== undefined);
      if (condition.value === undefined) return has;
      return node.attributes.get(condition.attr) === condition.value || node.dataset?.[key] === condition.value;
    }
    return false;
  });
}

function matchesChain(node, parts) {
  if (!parts.length || !matchesCompound(node, parts[parts.length - 1])) return false;
  let current = node.parent;
  let index = parts.length - 2;
  while (index >= 0 && current) {
    if (matchesCompound(current, parts[index])) index -= 1;
    current = current.parent;
  }
  return index < 0;
}

/* ---- 全局桩件 ---- */

const allNodes = [];
const register = (node) => {
  allNodes.push(node);
  return node;
};
const named = {
  materialsTabs: register(new FakeElement("div", "materials-tabs-fake")),
  lecturePane: register(new FakeElement("section", "lecture-pane-fake")),
  pageStudy: register(new FakeElement("section", "study-page-fake")),
};
named.materialsTabs.className = "materials-tabs";
named.lecturePane.className = "lecture-pane";
named.pageStudy.className = "page";

const docNode = register(new FakeElement("#document", ""));
globalThis.document = Object.assign(docNode, {
  getElementById: (id) => {
    const existing = allNodes.find((node) => node.id === id);
    if (existing) return existing;
    return register(new FakeElement("div", id));
  },
  createElement: (tag) => register(new FakeElement(tag, `created-${allNodes.length}`)),
  createElementNS: (ns, tag) => register(new FakeElement(tag, `created-ns-${allNodes.length}`)),
  createDocumentFragment: () => register(new FakeElement("#document-fragment", `fragment-${allNodes.length}`)),
  querySelectorAll: (selector) => allNodes.filter((node) => matchesChain(node, String(selector).split(/\s+/).filter(Boolean))),
  querySelector: (selector) => {
    const text = String(selector);
    if (text.includes(".materials-tabs")) return named.materialsTabs;
    if (text.includes(".lecture-pane")) return named.lecturePane;
    if (text.includes(".page:not([hidden])")) return named.pageStudy;
    return allNodes.find((node) => matchesChain(node, text.split(/\s+/).filter(Boolean))) || null;
  },
  activeElement: null,
  documentElement: { dataset: {} },
});

const windowTarget = new EventTarget();
windowTarget.addEventListener = windowTarget.addEventListener.bind(windowTarget);
windowTarget.requestAnimationFrame = (fn) => fn();
windowTarget.setTimeout = (fn, ms) => setTimeout(fn, ms);
windowTarget.clearTimeout = (id) => clearTimeout(id);
globalThis.window = windowTarget;
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
};
/* 保留 node 原生 Event 全局：undici 内部懒加载依赖 globalThis.Event，不可覆盖 */
globalThis.CSS = { escape: (value) => String(value) };
globalThis.matchMedia = (query) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
});
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};
globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 5; index += 1) await nextTurn();
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---- 可注入网络桩 ---- */

const ok = (data, status = 200) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
  status, headers: { "content-type": "application/json" },
});
const errorResponse = (code, status = 404) => new Response(JSON.stringify({ error: code, error_code: code }), {
  status, headers: { "content-type": "application/json" },
});

let routes = new Map(); /* route 前缀 → (path) => Response */
let deferredPrefixes = new Set();
const pendingDeferred = new Map(); /* 完整路径 → resolve */
const requestLog = [];
const requestMeta = []; /* { route, method, body }：需要断言请求体/方法的用例使用 */

function routePayload(path, options) {
  for (const [prefix, producer] of routes.entries()) {
    if (String(path).startsWith(prefix)) return producer(path, options);
  }
  throw new Error(`unexpected synthetic route: ${path}`);
}

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  requestLog.push(route);
  requestMeta.push({
    route,
    method: String(options?.method || "GET"),
    body: typeof options?.body === "string" ? options.body : "",
  });
  for (const prefix of deferredPrefixes) {
    if (route.startsWith(prefix)) {
      if (!pendingDeferred.has(route)) {
        pendingDeferred.set(route, { route });
      }
      const entry = pendingDeferred.get(route);
      if (!entry.promise) {
        entry.promise = new Promise((resolve) => { entry.resolve = resolve; });
      }
      return entry.promise;
    }
  }
  return routePayload(route, options);
};

function defer(prefix) { deferredPrefixes.add(prefix); }
function undefers(prefix) { deferredPrefixes.delete(prefix); }
async function releaseDeferred() {
  const entries = [...pendingDeferred.values()].filter((entry) => entry.promise);
  for (const entry of entries) entry.resolve(routePayload(entry.route));
  await settle();
}
function deferredCount() {
  return [...pendingDeferred.values()].filter((entry) => entry.promise && !entry.settled).length;
}

/* ---- 固定讲次/目录环境 ---- */

const LECTURE = { course_id: "c1", sub_id: "s1", course_title: "课程甲·高分子化学导论", sub_title: "第一讲·聚合反应原理总览" };
const NOW = Date.now();
const HOUR = 3600 * 1000;

const structuredContent = {
  schema_version: 1,
  overview: "本讲总览聚合反应的两条主线：逐步聚合与链式聚合，并给出课程地图。",
  key_takeaways: ["链式聚合对水敏感", "逐步聚合的分子量随转化率上升"],
  /* RR-ANCHORFE-1：锚与 key_takeaways 等长对齐（毫秒或 null）；null=无锚降级 */
  takeaway_anchors: [1500000, null],
  chapters: [
    { chapter_id: "C0001", title: "课程介绍与考纲", summary: "介绍课程目标与考核方式。", start_ms: 0, end_ms: 600000 },
    { chapter_id: "C0002", title: "逐步聚合", summary: "缩聚反应动力学。", start_ms: 600000, end_ms: 1500000 },
  ],
  generation: { model: "deepseek-reasoner", prompt_version: "actions-summary-v1", input_hash: "a".repeat(64) },
};

const validIr = {
  contract: "evidence.v1",
  sections: [],
  knowledge_units: [],
  key_moments: [
    { id: "unit:aaaaaaaaaaaa", kind: "key_moment", title: null, time: { start_ms: 120000, end_ms: 180000 }, spans: [{ kind: "slide_event", id: "slevt:bbbbbbbbbbbb" }], content: { page_num: 3 } },
    { id: "unit:cccccccccccc", kind: "key_moment", title: null, time: { start_ms: -5, end_ms: 0 }, spans: [], content: null },
  ],
};

function setDefaultRoutes() {
  routes = new Map([
    ["/api/v3/artifacts?sub_id=s1&kind=lecture_summary", () => ok({ artifact: { kind: "lecture_summary", status: "ready", model: "deepseek-reasoner", updated_at: 1789000000, metrics: { slides_skipped: { duplicate: 2 } }, content: structuredContent } })],
    ["/api/v3/artifacts?sub_id=s1&kind=lecture_ir", () => ok({ artifact: { kind: "lecture_ir", status: "ready", content: validIr } })],
    ["/api/v3/artifacts?sub_id=s2", () => ok({ artifact: null })], /* C3：合同改 200 空载荷 */
    ["/api/v3/subtitles/segments", () => ok({ segments: [] })],
    ["/api/v3/documents", () => ok({ documents: [] })],
    ["/api/v3/quizzes", () => ok({ items: [] })],
    ["/api/v3/review-plans", () => ok({ plans: [] })],
    ["/api/v3/concepts", () => ok({ concepts: [], edges: [] })],
    ["/api/v3/analytics", () => ok({ summary: { lectures: 2, watched_seconds: 3600, review_due: 3 } })],
    ["/api/v3/bookmarks", () => ok({ bookmarks: [] })],
  ]);
  deferredPrefixes = new Set();
  pendingDeferred.clear();
}
setDefaultRoutes();

/* ---- 安装真实模块 ---- */

const { store } = await import("../frontend/modules/store.js");
const { installStudy, renderCourses } = await import("../frontend/modules/study.js");
const { installSearchPalette } = await import("../frontend/modules/search-palette.js");

store.courses = [
  { course_id: "c1", title: "课程甲·高分子化学导论", lectures: [LECTURE, { sub_id: "s2", sub_title: "第二讲·缩聚动力学" }] },
  { course_id: "c2", title: "课程乙·有机波谱分析", lectures: [] },
];
await installStudy(store);
await installSearchPalette(store);
await settle();

const el = (id) => document.getElementById(id);
const flushNotes = async () => settle();
/* 假 DOM 的 textContent 是叶子属性：树内文本需递归收集 */
const textOf = (node) => {
  if (!node || node.hidden) return "";
  return [String(node.textContent || ""), ...(node.children || []).map((child) => textOf(child))].join("");
};

/* 1) 结构化总结渲染：概览 / 核心结论 / 章节 + 来源行；无 IR 时关键时刻隐藏 */
store.set("activeLecture", { ...LECTURE });
/* 加载器同步进入加载态：IR 未到、结构未到时只显示加载文案 */
assert.equal(textOf(el("artifact-content")), "正在加载总结", "加载态可见");
assert.ok(el("artifact-structured").hidden === true, "结构未到时容器隐藏");
assert.ok(el("artifact-key-moments").hidden === true, "IR 未到时关键时刻隐藏");
await flushNotes();
assert.ok(el("artifact-structured").hidden === false, "结构化总结容器可见");
assert.match(el("artifact-overview").textContent, /逐步聚合与链式聚合/);
assert.ok(el("artifact-overview").hidden === false);
assert.equal(el("artifact-takeaways").children.length, 2);
/* RR-ANCHORFE-1：有锚 takeaway 带时间戳按钮（复用章节行同款），锚=None 不渲染 */
const anchoredTakeaway = el("artifact-takeaways").children[0];
assert.equal(anchoredTakeaway.className, "takeaway-anchored", "有锚 takeaway 挂锚定类");
const takeawayTime = anchoredTakeaway.children.find((node) => node.className === "timestamp-button");
assert.ok(takeawayTime, "有锚 takeaway 带时间戳按钮");
assert.equal(takeawayTime.textContent, "25:00");
assert.match(textOf(anchoredTakeaway), /链式聚合对水敏感/, "条目文本保留");
const plainTakeaway = el("artifact-takeaways").children[1];
assert.equal(plainTakeaway.className, "", "无锚 takeaway 不带锚定类");
assert.ok(!plainTakeaway.children.some((node) => node.className === "timestamp-button"), "锚=None 优雅降级不渲染按钮");
assert.equal(textOf(plainTakeaway), "逐步聚合的分子量随转化率上升");
assert.equal(el("artifact-chapters").children.length, 2);
const chapterRow = el("artifact-chapters").children[0];
const chapterTime = chapterRow.children.find((node) => node.className === "timestamp-button");
assert.ok(chapterTime, "章节行带时间戳按钮");
assert.equal(chapterTime.textContent, "0:00");
const secondTime = el("artifact-chapters").children[1].children.find((node) => node.className === "timestamp-button");
assert.equal(secondTime.textContent, "10:00");
assert.match(el("artifact-source").textContent, /生成模型 deepseek-reasoner/);
assert.ok(el("artifact-content").hidden === true, "结构化模式下旧文本容器隐藏");
assert.ok(el("artifact-notices").hidden === false, "幻灯片跳过提示可见");
assert.match(el("artifact-notices").textContent, /重复页面/);
assert.ok(!el("artifact-content").textContent.includes("{"), "不 JSON.stringify 原始载荷");
assert.ok(!collectAllText().includes("input_hash"), "不暴露技术哈希");

/* 2) 关键时刻：合法 IR 渲染页码与时间，非法锚点丢弃；证据动作 seek 不自动播放 */
await flushNotes();
assert.ok(el("artifact-key-moments").hidden === false, "合法 IR 后关键时刻可见");
assert.equal(el("artifact-key-moment-list").children.length, 1, "负锚点关键时刻被丢弃");
const momentRow = el("artifact-key-moment-list").children[0];
assert.match(textOf(momentRow), /课件第 3 页/);
assert.equal(momentRow.children.find((node) => node.className === "timestamp-button").textContent, "2:00");
el("player-stage").play = () => { throw new Error("自动播放被禁止"); };
const windowEvents = [];
windowTarget.addEventListener("courselens:transcript-time", (event) => windowEvents.push(event.detail));
momentRow.children.find((node) => node.className === "timestamp-button").click();
await flushNotes();
assert.equal(el("player-stage").currentTime, 120, "关键时刻 seek 到绝对秒");
assert.equal(windowEvents.length, 1, "派发 transcript-time 高亮事件");
assert.equal(windowEvents[0].sub_id, "s1");
assert.ok(!collectAllText().includes("slevt:"), "不显示原始证据 ID");

/* 3) legacy markdown 回退 + IR 缺失只隐藏增强 + 空载荷空态 */
routes.set("/api/v3/artifacts?sub_id=s1&kind=lecture_summary", () => ok({ artifact: { kind: "lecture_summary", status: "ready", content: {}, content_markdown: "旧版纯文本总结\n第二行" } }));
routes.set("/api/v3/artifacts?sub_id=s1&kind=lecture_ir", () => ok({ artifact: null })); /* C3：缺失=200 空载荷 */
store.set("activeLecture", { ...LECTURE, sub_id: "s2", sub_title: "第二讲·缩聚动力学" });
await flushNotes();
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.ok(el("artifact-structured").hidden === true, "legacy 内容走回退");
assert.ok(el("artifact-content").hidden === false);
assert.match(el("artifact-content").textContent, /旧版纯文本总结/);
assert.ok(el("artifact-key-moments").hidden === true, "IR 缺失只隐藏增强");
routes.set("/api/v3/artifacts?sub_id=s1&kind=lecture_summary", () => ok({ artifact: null })); /* C3：空载荷即空态 */
store.set("activeLecture", { ...LECTURE, sub_id: "s2", sub_title: "第二讲·缩聚动力学" });
await flushNotes();
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.equal(textOf(el("artifact-content")), "本讲总结尚未生成，字幕就绪后会自动整理出来", "空态居中带引导语（UIAUDIT-1 F7）");
assert.ok(el("artifact-structured").hidden === true);

/* 4) 加载态：请求在途时显示“正在加载总结”，epoch 守卫丢弃过期响应 */
routes.set("/api/v3/artifacts?sub_id=s1&kind=lecture_summary", () => ok({ artifact: { status: "ready", content: structuredContent } }));
defer("/api/v3/artifacts?sub_id=s1&kind=lecture_summary");
el("artifact-kind").dispatchEvent(new Event("change"));
await flushNotes();
assert.equal(textOf(el("artifact-content")), "正在加载总结", "加载态可见");
assert.ok(el("artifact-structured").hidden === true);
/* 切换讲次使在途请求过期，再同时释放新旧响应：旧数据不得覆盖新讲次 */
defer("/api/v3/artifacts?sub_id=s2&kind=lecture_summary");
store.set("activeLecture", { ...LECTURE, sub_id: "s2", sub_title: "第二讲·缩聚动力学" });
await flushNotes();
assert.equal(textOf(el("artifact-content")), "正在加载总结", "新讲次在途仍显示加载态");
undefers("/api/v3/artifacts?sub_id=s1&kind=lecture_summary");
await releaseDeferred();
await settle();
assert.equal(textOf(el("artifact-content")), "本讲总结尚未生成，字幕就绪后会自动整理出来", "s2 404 落地且过期 s1 响应不覆盖");
undefers("/api/v3/artifacts?sub_id=s2&kind=lecture_summary");

/* 5) 测验揭示 + 依据动作只对同讲次合法锚点 */
routes.set("/api/v3/quizzes", () => ok({
  items: [
    { quiz_id: "q1", course_id: "c1", sub_id: "s2", question: "缩聚反应的分子量分布有何特点？", answer: "随转化率升高而变宽。", evidence: { start_ms: 65000 } },
    { quiz_id: "q2", course_id: "c1", sub_id: "s1", question: "链式聚合为什么对水敏感？", answer: "水会终止离子活性种。", evidence: { start_ms: 120000 } },
    { quiz_id: "q3", course_id: "c1", sub_id: "s1", question: "无锚点题", answer: "答案", evidence: {} },
  ],
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
const quizRows = el("quiz-list").children.filter((node) => String(node.className).split(/\s+/).includes("item-row"));
assert.equal(quizRows.length, 3);
for (const row of quizRows) {
  const reveal = row.children.find((node) => node.className === "answer-reveal-button");
  const answer = row.children.find((node) => node.className === "quiz-answer");
  assert.ok(reveal, "每题有提交核对按钮");
  assert.equal(reveal.getAttribute("aria-expanded"), "false", "提交前答案不可见");
  assert.equal(reveal.textContent, "提交并核对", "C⑨：先自答后核对");
  assert.ok(answer.hidden, "答案节点默认隐藏");
  assert.ok(row.children.some((node) => node.className === "quiz-self-answer"), "提供自答输入");
}
const rowWithEvidence = quizRows[1];
const evidenceButton = rowWithEvidence.children.find((node) => node.className === "evidence-jump-button");
assert.ok(evidenceButton, "同讲次合法锚点提供查看依据");
assert.ok(!quizRows[0].children.some((node) => node.className === "evidence-jump-button"), "异讲次不给依据动作");
assert.ok(!quizRows[2].children.some((node) => node.className === "evidence-jump-button"), "无锚点不给依据动作");
const revealButton = rowWithEvidence.children.find((node) => node.className === "answer-reveal-button");
revealButton.click();
assert.equal(revealButton.getAttribute("aria-expanded"), "true");
assert.equal(revealButton.textContent, "已核对");
assert.ok(revealButton.disabled, "C⑨：提交后按钮收口（单向揭示）");
const revealedAnswer = rowWithEvidence.children.find((node) => node.className === "quiz-answer");
assert.ok(!revealedAnswer.hidden, "提交后答案与原文可见");
assert.match(String(revealedAnswer.textContent || ""), /答案：/, "揭示含答案");
windowEvents.length = 0;
evidenceButton.click();
await flushNotes();
assert.equal(windowEvents[0]?.time_ms, 120000, "依据跳转派发字幕锚点");
assert.equal(el("player-stage").currentTime, 120, "依据跳转 seek 不自动播放");

/* 6) 复习计划步骤 + 考试上下文：active + 720h 内显示 */
const ACTIVE_EXAM_AT = (NOW + 96 * HOUR) / 1000;
routes.set("/api/v3/review-plans", () => ok({
  plans: [{
    plan_id: "p1", title: "考前两周计划", exam_at: ACTIVE_EXAM_AT, available_minutes: 60,
    exam_state: "active", deadline_context: "active", exam_source: "fudan_jwgl",
    exam_precision: "datetime", daily_minutes: 30, course_scope: ["c1"],
    scope: { course_id: "c1", sub_id: "s1" },
    steps: [
      { order: 1, kind: "watch", title: "逐步聚合动力学", estimated_minutes: 20, status: "pending", reason: "错题优先", course_id: "c1", sub_id: "s1", start_ms: 600000, evidence_id: null },
      { order: 2, kind: "quiz", title: "缩聚分子量分布题", estimated_minutes: 20, status: "pending", reason: "未答题回顾", course_id: "c1", sub_id: "s1", start_ms: 120000, evidence_id: null },
    ],
    updated_at: 1789000100,
  }],
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.ok(el("exam-context").hidden === false, "active 考试上下文可见");
assert.match(el("exam-context-row").textContent, /距考试 3 天/);
assert.match(el("exam-context-row").textContent, /来源：复旦教务系统/);
assert.match(el("exam-context-row").textContent, /今日预算 30 分钟/);
const todayItems = el("exam-today-list").children.filter((node) => String(node.className).split(/\s+/).includes("item-row"));
assert.equal(todayItems.length, 1, "今天建议按 30 分钟预算只取首步（20+20>30）");
assert.match(textOf(todayItems[0]), /逐步聚合动力学/);
assert.match(textOf(todayItems[0]), /预计 20 分钟/);
assert.match(textOf(todayItems[0]), /理由：错题优先/);
assert.ok(todayItems[0].children.some((node) => node.className === "evidence-jump-button"), "建议步骤带依据动作");
const planRows = el("review-list").children.filter((node) => String(node.className).split(/\s+/).includes("item-row"));
assert.equal(planRows.length, 1);
const stepRows = planRows[0].children.find((node) => node.className === "review-steps").children;
assert.equal(stepRows.length, 2, "完整计划保留两个步骤");
assert.match(textOf(stepRows[0]), /章节回顾/);
assert.match(textOf(stepRows[1]), /题目回顾/);
assert.match(textOf(stepRows[1]), /课程甲·高分子化学导论 · 第一讲·聚合反应原理总览 · 预计 20 分钟/);
assert.match(textOf(stepRows[1]), /理由：未答题回顾/);
assert.ok(!collectAllText().includes("必考"), "绝不出现必考");
assert.ok(!collectAllText().includes("押题"), "绝不出现押题");

/* 7) 考试上下文安全降级：outside_window / unavailable / passed / 不一致 / 空范围 */
const planBase = routes.get("/api/v3/review-plans");
const withPlan = (plan) => ok({ plans: plan ? [plan] : [] });
const basePlan = {
  plan_id: "p1", title: "考前两周计划", exam_at: ACTIVE_EXAM_AT, available_minutes: 60,
  exam_state: "active", deadline_context: "active", exam_source: "fudan_jwgl", exam_precision: "datetime",
  daily_minutes: 30, course_scope: ["c1"], scope: { course_id: "c1", sub_id: "s1" }, steps: [], updated_at: 1789000100,
};
routes.set("/api/v3/review-plans", () => withPlan({ ...basePlan, exam_state: "outside_window", exam_at: (NOW + 800 * HOUR) / 1000 }));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "窗口外隐藏");
routes.set("/api/v3/review-plans", () => withPlan({ ...basePlan, exam_state: "unavailable", exam_at: null }));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "不可用隐藏");
routes.set("/api/v3/review-plans", () => withPlan({ ...basePlan, exam_state: "passed", exam_at: (NOW - 48 * HOUR) / 1000 }));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "已过隐藏");
assert.match(textOf(el("review-list")), /考试已结束，保留历史步骤/, "passed 计划显示明确状态");
routes.set("/api/v3/review-plans", () => withPlan({ ...basePlan, exam_state: "active", exam_at: (NOW - 2 * HOUR) / 1000 }));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "exam_state 与时间不一致时 fail closed");
routes.set("/api/v3/review-plans", () => withPlan({ ...basePlan, course_scope: [] }));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "空课程范围不渲染");
routes.set("/api/v3/review-plans", () => withPlan(null));
el("generate-quiz").click();
await flushNotes();
assert.ok(el("exam-context").hidden === true, "无计划隐藏");
assert.match(textOf(el("review-list")), /暂无复习计划/);

/* 8) 课程关系诚实降级 */
routes.set("/api/v3/concepts", () => ok({
  concepts: [{ concept_id: "k1", name: "缩聚反应" }],
  courses: [{ course_id: "c1", title: "课程甲·高分子化学导论" }, { course_id: "c2", title: "课程乙·有机波谱分析" }],
  edges: [
    { edge_id: "e1", concept_id: "k1", relation: "related", from_course_id: "c1", to_course_id: "c2", status: "active", evidence: [{ sub_id: "s1" }] },
    { edge_id: "e2", concept_id: "k1", relation: "related", from_course_id: "c1", to_course_id: "c2", status: "stale", evidence: [] },
    { edge_id: "e3", concept_id: "k9", relation: "related", from_course_id: "c1", to_course_id: "c2", status: "active", evidence: [] },
  ],
}));
store.set("activeCourse", { course_id: "c1", title: "课程甲·高分子化学导论" });
store.set("activeLecture", { ...LECTURE });
await flushNotes();
const conceptRows = el("concept-list").children.filter((node) => String(node.className).split(/\s+/).includes("item-row"));
assert.equal(conceptRows.length, 3);
assert.match(textOf(conceptRows[0]), /有证据/, "有证据数组才可写有证据");
assert.match(textOf(conceptRows[1]), /证据待确认/);
assert.match(textOf(conceptRows[2]), /证据状态未提供/, "无证据不默认有证据");
assert.match(textOf(conceptRows[0]), /课程甲·高分子化学导论/);

/* 9) 针对本讲提问：事件打开 palette 并带讲次范围提示 */
store.set("activeLecture", { ...LECTURE });
store.auth = { state: "ready", code: "fudan_session_verified" };
await flushNotes();
el("ask-lecture").click();
await flushNotes();
assert.ok(el("palette-root").hidden === false, "ask-lecture 打开搜索浮层");
assert.match(el("palette-status").textContent, /将针对本讲「第一讲·聚合反应原理总览」回答与检索/);
document.dispatchEvent(new (class extends Event {
  constructor(type, options = {}) { super(type); this.key = options.key || ""; }
})("keydown", { key: "Escape" }));
await flushNotes();
assert.ok(el("palette-root").hidden === true, "palette 可关闭");

/* 10) 长中文内容 + 多章节不崩溃、长标题讲次不产生越界文本 */
routes.set("/api/v3/artifacts?sub_id=s1&kind=lecture_summary", () => ok({
  artifact: {
    status: "ready", model: "deepseek-reasoner", updated_at: 1789000000,
    content: {
      overview: "长".repeat(3000),
      key_takeaways: ["结论".repeat(80)],
      chapters: Array.from({ length: 22 }, (_, index) => ({
        chapter_id: `C${index}`, title: `第${index + 1}章·${"很长的章节标题".repeat(6)}`,
        summary: "摘".repeat(200), start_ms: index * 60000, end_ms: (index + 1) * 60000,
      })),
    },
  },
}));
routes.set("/api/v3/review-plans", () => ok({
  plans: [{
    plan_id: "p2", title: "长课程名计划·".repeat(10), exam_at: null, available_minutes: 60,
    scope: { course_id: "c1", sub_id: "s1" },
    steps: Array.from({ length: 9 }, (_, index) => ({
      order: index + 1, kind: "watch", title: `很长的步骤标题·${"细节".repeat(40)}`,
      minutes: 10, reason: "章节重点", evidence: { start_ms: index * 30000 },
    })),
    updated_at: 1789000100,
  }],
}));
el("artifact-kind").dispatchEvent(new Event("change"));
el("artifact-kind").dispatchEvent(new Event("change"));
await flushNotes();
assert.equal(el("artifact-chapters").children.length, 22, "22 个章节全部渲染");
assert.ok(el("artifact-structured").hidden === false);
assert.match(collectAllText(), /很长的章节标题/, "长标题渲染完整");

/* 11) 课件 PDF：artifact 与 operation 两个独立事实驱动闭集状态渲染。
     覆盖：未选讲次/未生成/排队运行(可信计数)/暂停续跑/失败重试/就绪下载与
     重新生成/旧成品+新版共存(运行、暂停、失败)/瞬时状态错误保留成品/
     重复点击在整段活动操作内被阻止/计数轮询不刷 live 区域/讲次切换丢弃
     迟到响应。 */
const COURSEWARE_STATUS_ROUTE = "/api/v3/courseware-pdf?sub_id=s1";
const coursewareEl = (id) => document.getElementById(id);
const readyArtifact = () => ({
  ready: true, pages: 12, events_total: 40, duplicates: 28,
  skipped_total: 0, skipped: {}, generated_at: 1789000000,
  download_name: "2026-09-14-第一讲.pdf",
});
const runningOperation = (processed, kept) => ({
  task_id: "op-1", state: "running", phase: "正在读取课堂画面",
  label: "正在读取课堂画面", percent: Math.round(processed / 40 * 1000) / 10,
  percent_measured: true, resumable: false, error_code: "",
  counts: { processed, kept, total: 40 },
});

routes.set("/api/v3/courseware-pdf/actions", () => ok({ task_id: "task-pdf-1", state: "queued" }));

/* 11a) 未选讲次：中性文案 + 生成禁用（无错误色） */
store.set("activeLecture", null);
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /请先选择讲次/, "未选讲次给中性原因");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "未选讲次生成禁用");
assert.equal(coursewareEl("courseware-info").dataset.tone, "", "普通不可用不用错误色");

/* 11b) 未生成：标题与一句说明 + 主动作 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: null, operation: null }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /从本讲课堂画面整理为可下载的课件 PDF/, "未生成给一句说明");
assert.match(coursewareEl("courseware-pdf-state").textContent, /有改动或批注的版本会全部保留/, "说明标注保留");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, false);
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "生成课件 PDF");
assert.equal(coursewareEl("courseware-pdf-download").hidden, true, "未生成无下载");
assert.equal(coursewareEl("courseware-notes").hidden, true, "无成品时无整理说明");

/* 11c) 点击生成：POST 一次 → 排队文案 + 整段活动操作内按钮禁用（重复点击被拦）。
     actions 桩同时把状态路由切到 queued，模拟后端真实迁移。 */
routes.set("/api/v3/courseware-pdf/actions", () => {
  routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
    sub_id: "s1", artifact: null,
    operation: { task_id: "op-1", state: "queued", phase: "课件 PDF 排队中", label: "课件 PDF 排队中", percent: null, percent_measured: false, resumable: false, error_code: "", counts: null },
  }));
  return ok({ task_id: "task-pdf-1", state: "queued" });
});
const actionsBefore = requestLog.filter((route) => route.startsWith("/api/v3/courseware-pdf/actions")).length;
coursewareEl("generate-courseware-pdf").click();
/* 第十七案 b：点击即有回应，不等 POST 往返与状态链路 */
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "正在开始…", "点击即馈：按钮立即换标签");
assert.match(coursewareEl("courseware-pdf-state").textContent, /正在开始生成本讲课件 PDF/, "点击即馈：live 区域立即播报");
await settle();
assert.equal(requestLog.filter((route) => route.startsWith("/api/v3/courseware-pdf/actions")).length, actionsBefore + 1, "点击恰好一次 POST");
/* 第卅案：提交即乐观排队态；状态事实（含竞态回旧成品的情形）由乐观轮询接管 */
assert.match(coursewareEl("courseware-pdf-state").textContent, /已提交，正在等待整理开始/, "提交即排队应答");
await sleep(1300);
assert.match(coursewareEl("courseware-pdf-state").textContent, /课件 PDF 排队中/, "排队状态可见（乐观轮询≤1.3s 接管）");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "活动操作期间禁用");
assert.equal(coursewareEl("courseware-surface").getAttribute("aria-busy"), "true", "aria-busy 暴露活动操作");
coursewareEl("generate-courseware-pdf").click();
await settle();
assert.equal(requestLog.filter((route) => route.startsWith("/api/v3/courseware-pdf/actions")).length, actionsBefore + 1, "活动操作内重复点击不产生第二个 POST");

/* 11d) 运行中可信计数：阶段进 live 区域，计数进独立节点；纯计数轮询不刷 live */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: null, operation: runningOperation(10, 8) }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /^正在读取课堂画面$/, "live 区域只有阶段含义");
assert.ok(!/\d/.test(coursewareEl("courseware-pdf-state").textContent), "live 区域不含计数数字");
assert.match(coursewareEl("courseware-pdf-detail").textContent, /已读取 10 个画面，已保留 8 张/, "计数在独立节点");
assert.match(coursewareEl("courseware-pdf-detail").textContent, /25%/, "可信测量百分比可见");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "生成中…", "进行态：生成中…标签");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "进行态：生成禁用");
const liveBeforePoll = coursewareEl("courseware-pdf-state").textContent;
/* 用写计数拦截器证明：纯计数轮询对 live 区域零写入（不只是写入相同文本） */
const liveNode = coursewareEl("courseware-pdf-state");
let liveWrites = 0;
Object.defineProperty(liveNode, "textContent", {
  configurable: true,
  get() { return liveBeforePoll; },
  set() { liveWrites += 1; },
});
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: null, operation: runningOperation(12, 9) }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-detail").textContent, /已读取 12 个画面，已保留 9 张/, "计数节点随轮询更新");
assert.equal(liveWrites, 0, "纯计数轮询对 live 区域零写入");
delete liveNode.textContent;
liveNode.textContent = liveBeforePoll;

/* 11e) 资源暂停：金色语义 + 进度已保存 + 继续生成；点击续跑 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
  sub_id: "s1", artifact: null,
  operation: { task_id: "op-1", state: "paused", phase: "正在读取课堂画面", label: "已暂停：已达到本地资源预算；进度已保存", percent: 25, percent_measured: true, resumable: true, error_code: "", counts: { processed: 10, kept: 8, total: 40 } },
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /已暂停/, "暂停含义可见");
assert.match(coursewareEl("courseware-pdf-state").textContent, /进度已保存/, "说明进度已保存");
assert.equal(coursewareEl("courseware-info").dataset.tone, "paused", "暂停用金色语义");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "继续生成", "暂停主动作是继续生成");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, false);
const resumeBefore = requestLog.filter((route) => route.startsWith("/api/v3/courseware-pdf/actions")).length;
coursewareEl("generate-courseware-pdf").click();
await settle();
assert.equal(requestLog.filter((route) => route.startsWith("/api/v3/courseware-pdf/actions")).length, resumeBefore + 1, "继续生成发送 resume");

/* 11f) 失败（无成品）：内联红色语义 + 原因 + 重试 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
  sub_id: "s1", artifact: null,
  operation: { task_id: "op-2", state: "failed", phase: "", label: "", percent: null, percent_measured: false, resumable: false, error_code: "ppt_record_storm", counts: null },
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /生成未完成/, "失败内联可见");
assert.match(coursewareEl("courseware-pdf-state").textContent, /课堂画面密度异常/, "闭集原因文案");
assert.equal(coursewareEl("courseware-info").dataset.tone, "failed", "失败用红色语义");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "重试", "失败主动作是重试");
assert.ok(!collectAllText().includes("ppt_record_storm"), "原始错误码不出现在页面");

/* 11g) 就绪：诚实文案 + M 页/N 个画面 + 下载主动作 + 重新生成次动作 + 整理说明 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: readyArtifact(), operation: null }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /已从 40 个课堂画面整理为 12 页，批注版本已保留/, "诚实就绪文案");
assert.ok(!/最新|完美|原始课件/.test(coursewareEl("courseware-pdf-state").textContent), "不夸大识别与页序");
assert.match(coursewareEl("courseware-pdf-detail").textContent, /12 页/, "页数可见");
assert.match(coursewareEl("courseware-pdf-detail").textContent, /40 个课堂画面/, "来源画面数可见");
assert.match(coursewareEl("courseware-pdf-detail").textContent, /2026/, "第十七案 d：生成时间是真实本地时间（generated_at 秒不再被双重 ×1000）");
assert.ok(!/58691/.test(collectAllText()), "不得再出现公元 58691 年式乱码时间");
assert.equal(coursewareEl("courseware-pdf-download").hidden, false, "就绪显示下载");
assert.equal(coursewareEl("courseware-pdf-download").getAttribute("href"), "/api/v3/courseware-pdf/file?sub_id=s1", "下载走既有安全路由");
assert.match(coursewareEl("courseware-pdf-download").getAttribute("aria-label") || "", /PDF/, "下载可访问名含 PDF");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "重新生成", "重新生成次动作");
assert.ok(coursewareEl("generate-courseware-pdf").classList.contains("btn-primary"), "生成动作恒为主按钮样式");
assert.equal(coursewareEl("courseware-notes").hidden, false, "有成品时出现整理说明");
assert.ok(!collectAllText().includes("sha256"), "不暴露源哈希");
assert.ok(!collectAllText().includes("/courseware/"), "不暴露本地路径");
const downloadName = coursewareEl("courseware-pdf-download").getAttribute("download");
assert.ok(downloadName && downloadName.endsWith(".pdf") && !downloadName.includes(".."), "下载文件名已消毒");

/* 11g-2) 第卅案：重新生成竞态——POST 受理但状态 GET 仍回旧成品（无
     operation，正常轮询链不启动）。验收=提交后 2 秒内两处可见进行态：
     状态行立即换排队态（旧页数/旧时间戳退场），任务中心立即刷新，
     随后乐观轮询把行交给后端事实。 */
{
  let tasksRefreshes = 0;
  const onTasksRefresh = () => { tasksRefreshes += 1; };
  windowTarget.addEventListener("courselens:tasks-refresh", onTasksRefresh);
  routes.set("/api/v3/courseware-pdf/actions", () => ok({ task_id: "task-pdf-2", state: "queued" }));
  coursewareEl("generate-courseware-pdf").click();
  await settle();
  assert.match(coursewareEl("courseware-pdf-state").textContent, /已提交，正在等待整理开始/, "第卅案：提交即排队态（不等状态链路）");
  assert.ok(!/已从 40 个课堂画面/.test(coursewareEl("courseware-pdf-state").textContent), "旧成品文案立即退场");
  assert.equal(coursewareEl("courseware-pdf-download").hidden, true, "排队期不给旧版下载入口");
  assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "排队期按钮禁用");
  assert.ok(tasksRefreshes >= 1, "任务中心立即刷新（不等 30s 周期轮询）");
  /* 乐观轮询（~1s 短轮）把行交给后端事实：此刻才切 queued 路由 */
  routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
    sub_id: "s1", artifact: null,
    operation: { task_id: "op-9", state: "queued", phase: "课件 PDF 排队中", label: "课件 PDF 排队中", percent: null, percent_measured: false, resumable: false, error_code: "", counts: null },
  }));
  await sleep(1500);
  assert.match(coursewareEl("courseware-pdf-state").textContent, /课件 PDF 排队中/, "乐观轮询后状态事实接管");
  windowTarget.removeEventListener("courselens:tasks-refresh", onTasksRefresh);
}

/* 11g-3) 甲-1b 完成事件自动刷新：课件任务活跃→完成的事件 → 状态行换
     新页数/新时间戳（2 秒内），「全部资料」同帧重渲染；sub_id 不匹配的
     完成事件不误刷。 */
{
  routes.set("/api/v3/materials", () => ok({ course_id: "c1", entries: [], scan_truncated: false }));
  const statusGets = () => requestLog.filter((route) => route === COURSEWARE_STATUS_ROUTE).length;
  const materialsGets = () => requestLog.filter((route) => route.startsWith("/api/v3/materials")).length;
  routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: readyArtifact(), operation: null }));
  const beforeStatus = statusGets();
  const beforeMaterials = materialsGets();
  const startedAt = Date.now();
  windowTarget.dispatchEvent(new CustomEvent("courselens:materials-refresh", { detail: { course_id: "c1", sub_id: "s1" } }));
  await settle();
  assert.ok(Date.now() - startedAt < 2000, "完成事件驱动刷新在 2 秒内");
  assert.match(coursewareEl("courseware-pdf-state").textContent, /已从 40 个课堂画面整理为 12 页/, "状态行换新页数");
  assert.match(coursewareEl("courseware-pdf-detail").textContent, /2026/, "新时间戳可见");
  assert.ok(statusGets() > beforeStatus, "课件状态重取");
  assert.ok(materialsGets() > beforeMaterials, "全部资料同步重渲染");
  const beforeMismatch = statusGets();
  windowTarget.dispatchEvent(new CustomEvent("courselens:materials-refresh", { detail: { course_id: "c1", sub_id: "other-lecture" } }));
  await settle();
  assert.equal(statusGets(), beforeMismatch, "非当前讲次的完成事件不误刷");
}

/* 11h) 共存：旧成品 + 新版运行中 → 第十七案 a：生成完成前无下载入口
     （旧版极易被当成新成果误下载，活体报实证；暂停/失败态保留旧版下载） */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: readyArtifact(), operation: runningOperation(20, 15) }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.equal(coursewareEl("courseware-pdf-download").hidden, true, "新版生成完成前不给下载入口");
assert.match(coursewareEl("courseware-pdf-state").textContent, /正在生成新版/, "明示正在生成新版");
assert.match(coursewareEl("courseware-pdf-state").textContent, /正在读取课堂画面/, "新版阶段可见");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "新版运行期间禁止再生成");
assert.equal(coursewareEl("courseware-surface").getAttribute("aria-busy"), "true");

/* 11i) 共存：旧成品 + 新版暂停/失败 → 旧成品保留 + 继续生成/重试 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
  sub_id: "s1", artifact: readyArtifact(),
  operation: { task_id: "op-3", state: "paused", phase: "正在读取课堂画面", label: "已暂停：耗时达到上限；进度已保存", percent: 50, percent_measured: true, resumable: true, error_code: "", counts: { processed: 20, kept: 15, total: 40 } },
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.equal(coursewareEl("courseware-pdf-download").hidden, false, "新版暂停旧成品仍可下载");
assert.equal(coursewareEl("courseware-info").dataset.tone, "paused");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "继续生成");
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({
  sub_id: "s1", artifact: readyArtifact(),
  operation: { task_id: "op-4", state: "failed", phase: "", label: "", percent: null, percent_measured: false, resumable: false, error_code: "no_pages", counts: null },
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.equal(coursewareEl("courseware-pdf-download").hidden, false, "新版失败旧成品仍可下载且不被改标失败");
assert.match(coursewareEl("courseware-pdf-state").textContent, /原版本仍可下载/, "失败文案区分新旧");
assert.equal(coursewareEl("generate-courseware-pdf").textContent, "重试");

/* 11j) 瞬时状态错误：保留已知成品，只提示状态未确认 */
routes.set(COURSEWARE_STATUS_ROUTE, () => errorResponse("courseware_status_unavailable", 503));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-state").textContent, /状态暂时无法确认/, "瞬时错误中性提示");
assert.equal(coursewareEl("courseware-pdf-download").hidden, false, "瞬时错误不擦除下载");
assert.ok(!collectAllText().includes("503"), "不暴露网络异常细节");

/* 11k) 讲次切换：迟到的 s1 就绪响应不得刷新 s2 视图 */
defer(COURSEWARE_STATUS_ROUTE);
store.set("activeLecture", { ...LECTURE });
const s1ReadyText = "已从 40 个课堂画面整理为 12 页，批注版本已保留";
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: readyArtifact(), operation: null }));
store.set("activeLecture", { ...LECTURE, sub_id: "s2", sub_title: "第二讲·缩聚动力学" });
await flushNotes();
await releaseDeferred();
await settle();
assert.ok(!collectAllText().includes(s1ReadyText), "迟到响应不绘制到另一讲次");
deferredPrefixes.delete(COURSEWARE_STATUS_ROUTE);
routes.set("/api/v3/courseware-pdf?sub_id=s2", () => ok({ sub_id: "s2", artifact: null, operation: null }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();

/* 12) W2 课件 PDF 轮询自愈：活跃操作期一次 503 走有界退避重试，恢复后状态重新刷新 */
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: null, operation: runningOperation(10, 8) }));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(coursewareEl("courseware-pdf-detail").textContent, /已读取 10 个画面，已保留 8 张/, "活动操作计数可见");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "活动操作期间禁用");
let flakyStatus = true;
routes.set(COURSEWARE_STATUS_ROUTE, () => {
  if (flakyStatus) return errorResponse("courseware_status_unavailable", 503);
  return ok({ sub_id: "s1", artifact: null, operation: runningOperation(12, 9) });
});
await sleep(2600); /* 2s 轮询发出 → 503：中性提示 + 有界退避（首拍 2s） */
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "抖动期不伪装终态");
assert.match(coursewareEl("courseware-pdf-state").textContent, /状态暂时无法确认/, "抖动期保留未确认提示");
flakyStatus = false;
await sleep(2600); /* 退避重试发出 → 200：状态刷新并恢复 2s 轮询 */
assert.match(coursewareEl("courseware-pdf-detail").textContent, /已读取 12 个画面，已保留 9 张/, "有界重试后状态恢复刷新");
assert.equal(coursewareEl("generate-courseware-pdf").disabled, true, "恢复后仍处于活跃操作");
routes.set(COURSEWARE_STATUS_ROUTE, () => ok({ sub_id: "s1", artifact: readyArtifact(), operation: null }));
await sleep(2400); /* 下一拍轮询落终态：轮询自然收口，不给后续用例留定时器 */
assert.match(coursewareEl("courseware-pdf-state").textContent, /已从 40 个课堂画面整理为 12 页/, "终态渲染且轮询停止");

/* 13) W3 自动整理串行化：A 链在途点 B，B 排队并按刷新后的快照执行，
       最后一次 PUT 同时含 A、B（陈旧快照整体替换不再丢规则） */
const putConfigBodies = [];
routes.set("/api/v3/automation/config", (path, options) => {
  putConfigBodies.push(JSON.parse(String(options?.body || "{}")));
  return ok({ account_id: "a1", rules: putConfigBodies[putConfigBodies.length - 1]?.config?.rules || [] });
});
routes.set("/api/v3/automation/cloud-secrets", () => ok({}));
routes.set("/api/v3/automation/actions", () => ok({ operation: { state: "started" } }));
routes.set("/api/v3/automation", () => ok({
  account_id: "a1",
  disclosure_version: "cloud-custody-disclosure.v1",
  rules: putConfigBodies.length ? (putConfigBodies[putConfigBodies.length - 1]?.config?.rules || []) : [],
}));
routes.set("/api/v3/catalog?page_size=100", () => ok({
  state: "ready", code: "fudan_catalog_verified", refreshing: false, course_count: 2,
  courses: [
    { course_id: "w3c1", title: "自动整理课程甲", lectures: [{ sub_id: "w3s1", sub_title: "甲一讲" }] },
    { course_id: "w3c2", title: "自动整理课程乙", lectures: [{ sub_id: "w3s2", sub_title: "乙一讲" }] },
  ],
}));
routes.set("/api/v3/progress?sub_id=w3s1", () => ok({ progress: null }));
routes.set("/api/v3/progress?sub_id=w3s2", () => ok({ progress: null }));
store.set("auth", { state: "ready", code: "fudan_session_verified", connected: true, configured: true, actions: [] });
await flushNotes();
const automationToggle = (courseId) => el("study-course-list")
  .querySelectorAll(".course-automation-toggle")
  .find((node) => node.dataset.courseId === courseId);
assert.ok(automationToggle("w3c1"), "目录行渲染自动整理开关");
assert.equal(automationToggle("w3c1").disabled, false, "快照可用时开关可点");
defer("/api/v3/automation/cloud-secrets");
automationToggle("w3c1").click();
await settle();
assert.equal(putConfigBodies.length, 1, "A 链 PUT 已发出");
assert.deepEqual(putConfigBodies[0].config.rules, [{ course_id: "w3c1" }]);
assert.match(String(putConfigBodies[0].operation_id || ""), /^course-auto-cfg:[A-Za-z0-9._:-]{8,128}$/, "保存链每次 PUT 都携带闭集形状的幂等 operation_id");
automationToggle("w3c2").click(); /* A 链在途：B 不再以陈旧快照并发 PUT，而是排队 */
await settle();
assert.equal(putConfigBodies.length, 1, "B 在 A 链完成前不并发 PUT");
undefers("/api/v3/automation/cloud-secrets");
await releaseDeferred();
await flushNotes();
await flushNotes();
assert.equal(putConfigBodies.length, 2, "A 链完成后 B 链串行执行");
assert.deepEqual(
  putConfigBodies[1].config.rules,
  [{ course_id: "w3c1" }, { course_id: "w3c2" }],
  "最后一次 PUT 同时包含 A、B（规则不再被陈旧快照覆盖）",
);

/* 13a-2) U⑦：新课程云端自动处理一次性询问——种子只记存量课，新课问一次，
        「不用了」记忆不再问，「开启」走既有保存链，答过的不再问 */
{
  const newCourses = (extra) => [
    { course_id: "w3c1", title: "自动整理课程甲", lectures: [{ sub_id: "w3s1", sub_title: "甲一讲" }] },
    { course_id: "w3c2", title: "自动整理课程乙", lectures: [{ sub_id: "w3s2", sub_title: "乙一讲" }] },
    ...extra,
  ];
  const askCard = () => el("study-course-list").querySelector(".course-auto-ask");
  const askButton = (label) => askCard()?.querySelectorAll("button").find((node) => node.textContent === label);

  /* 存量课（种子内）不问 */
  renderCourses(store, { state: "ready", courses: newCourses([]) });
  await settle();
  assert.equal(askCard(), null, "种子内存量课程不问");

  /* 增量新课问一次：问句带额度与私仓边界说明 */
  renderCourses(store, { state: "ready", courses: newCourses([
    { course_id: "w3c3", title: "自动整理课程丙", lectures: [{ sub_id: "w3s3", sub_title: "丙一讲" }] },
  ]) });
  await settle();
  /* 桩节点 textContent 不聚合子树：textOf 递归聚合子树文本 */
  const askText = textOf(askCard());
  assert.match(askText, /要为《自动整理课程丙》开启云端自动处理吗/, "新课出现一次性问句");
  assert.match(askText, /你自己的 DeepSeek 额度，数据只进你的私有仓/, "问句带额度与私仓边界说明");

  /* 不用了=会话内收起且不再问（隐私边界：课程身份不落 localStorage） */
  askButton("不用了").click();
  await settle();
  assert.equal(askCard(), null, "不用了后问卡收起");
  renderCourses(store, { state: "ready", courses: newCourses([
    { course_id: "w3c3", title: "自动整理课程丙", lectures: [{ sub_id: "w3s3", sub_title: "丙一讲" }] },
  ]) });
  await settle();
  assert.equal(askCard(), null, "已询问课程会话内不再问");

  /* 开启=走既有保存链（快照回读为准） */
  putConfigBodies.length = 0;
  renderCourses(store, { state: "ready", courses: newCourses([
    { course_id: "w3c4", title: "自动整理课程丁", lectures: [{ sub_id: "w3s4", sub_title: "丁一讲" }] },
  ]) });
  await settle();
  assert.match(textOf(askCard()), /自动整理课程丁/, "下一门新课接着问（恰一次语义）");
  askButton("开启").click();
  await settle();
  assert.equal(putConfigBodies.length, 1, "开启走既有保存链（PUT config）");
  assert.deepEqual(putConfigBodies[0].config.rules, [
    { course_id: "w3c1" }, { course_id: "w3c2" }, { course_id: "w3c4" },
  ], "规则合并语义与 W3 一致：新课程并入既有规则");
  assert.equal(askCard(), null, "开启后问卡收起");
}

/* 13b) NIGHT2-W9 云控制面：快照 actions 闭集上 UI——直跑动作带幂等 operation_id，
        危险动作两击确认，未知键忽略，行动集为空整卡隐藏 */
{
  const cloudActions = [];
  routes.set("/api/v3/automation/actions", (path, options) => {
    cloudActions.push(JSON.parse(String(options?.body || "{}")));
    return ok({ operation: { state: "accepted" } });
  });
  routes.set("/api/v3/automation", () => ok({
    account_id: "a1",
    state: "ready",
    actions: ["run-now", "erase-cloud-data", "revoke-cloud-credentials", "mystery-action"],
    rules: [{ course_id: "w3c1" }, { course_id: "w3c2" }],
    disclosure_version: "cloud-custody-disclosure.v1",
  }));
  automationToggle("w3c2").click(); /* 走一次保存链触发快照刷新（卡片随新快照登场） */
  await settle();
  const card = el("cloud-control-card");
  assert.ok(card, "云控制面卡存在");
  assert.equal(card.hidden, false, "行动集非空 → 卡可见");
  assert.equal(el("cloud-control-state").textContent, "云自动化运行中", "状态字闭集文案");
  assert.equal(el("cloud-control-state").dataset.state, "ready", "状态字复用 data-state 色彩系统");
  const actionButtons = el("cloud-control-actions").children.filter((node) => node.tagName === "BUTTON");
  assert.deepEqual(
    actionButtons.map((node) => node.textContent),
    ["立即运行", "抹除云端数据"],
    "只渲染闭集内动作；撤销授权撤出显眼动作位；未知键 mystery-action 被忽略",
  );
  assert.equal(el("cloud-control-revoke-note").hidden, false, "撤销授权在场时显示隐私区指路（文案本体在 index.html，真 DOM 呈现）");
  assert.equal(actionButtons[1].className, "danger", "抹除云端数据沿用危险按钮形态");
  actionButtons[0].click();
  await settle();
  const runNow = cloudActions.find((body) => body.action === "run-now");
  assert.ok(runNow, "直跑动作已发出");
  assert.match(String(runNow.operation_id || ""), /^cloud-run-now:[A-Za-z0-9._:-]{8,128}$/, "直跑动作带闭集形状幂等 operation_id");
  actionButtons[1].click();
  await settle();
  assert.equal(actionButtons[1].textContent, "再点一次确认", "危险动作首次点击只武装确认");
  assert.ok(!cloudActions.some((body) => body.action === "erase-cloud-data"), "武装阶段不发请求");
  actionButtons[1].click();
  await settle();
  const erase = cloudActions.find((body) => body.action === "erase-cloud-data");
  assert.ok(erase, "两击确认后发出危险动作");
  assert.match(String(erase.operation_id || ""), /^cloud-erase-cloud-data:/, "危险动作同样带幂等 operation_id");
  routes.set("/api/v3/automation", () => ok({ account_id: "a1", state: "disabled", actions: [], rules: [] }));
  automationToggle("w3c2").click(); /* 再走一次链触发刷新：行动集空 → 整卡隐藏 */
  await settle();
  assert.equal(el("cloud-control-card").hidden, true, "行动集为空 → 整卡隐藏");
}

/* 13c) CLOUD-VERIFY-CHAIN-1 单元三：云卡动作映射补 verify/enable 两键——后端
        快照闭集提供时渲染独立重入面；未知键依旧忽略；两键走直跑通道
        （幂等 operation_id 前缀 cloud-<action>: + busy 期 disabled/aria-busy）。
        请求体经 requestMeta 断言（releaseDeferred 不回传原 options，route
        录制器收不到被延迟请求的真实 body）。 */
{
  routes.set("/api/v3/automation", () => ok({
    account_id: "a1", state: "configuring",
    actions: ["verify-cloud-credentials", "enable-cloud", "mystery-action"],
    rules: [], disclosure_version: "cloud-custody-disclosure.v1",
  }));
  automationToggle("w3c2").click(); /* 走一次保存链触发快照刷新（卡片随新快照登场） */
  await settle();
  const card2 = el("cloud-control-card");
  assert.equal(card2.hidden, false, "行动集非空 → 卡可见");
  const buttons2 = el("cloud-control-actions").children.filter((node) => node.tagName === "BUTTON");
  assert.deepEqual(
    buttons2.map((node) => node.textContent),
    ["重新云端验证", "启用云端自动整理"],
    "verify/enable 两键按快照闭集渲染，未知键 mystery-action 被忽略",
  );
  assert.ok(buttons2.every((node) => !String(node.className).includes("danger")), "两键均为普通动作形态");
  defer("/api/v3/automation/actions");
  buttons2[0].click();
  await settle();
  assert.equal(buttons2[0].disabled, true, "busy 期按钮禁用");
  assert.equal(buttons2[0].getAttribute("aria-busy"), "true", "busy 期 aria-busy 可达");
  undefers("/api/v3/automation/actions");
  await releaseDeferred();
  await settle();
  assert.equal(buttons2[0].getAttribute("aria-busy"), "false", "完成后 aria-busy 复位");
  buttons2[1].click();
  await settle();
  const actionPosts = requestMeta.filter((item) => item.route === "/api/v3/automation/actions"
    && String(item.method) === "POST");
  const postMatches = (item, action) => {
    try {
      const body = JSON.parse(String(item.body || "{}"));
      return body.action === action
        && new RegExp(`^cloud-${action}:[A-Za-z0-9._:-]{8,128}$`).test(String(body.operation_id || ""));
    } catch {
      return false;
    }
  };
  assert.ok(
    actionPosts.some((item) => postMatches(item, "verify-cloud-credentials")),
    "卡上 verify 动作带闭集形状幂等 operation_id",
  );
  assert.ok(
    actionPosts.some((item) => postMatches(item, "enable-cloud")),
    "卡上 enable 动作带闭集形状幂等 operation_id",
  );
}

/* 14) W4 讲次守卫：B 的测验加载失败时显示错误空态，不残留 A 讲次内容 */
routes.set("/api/v3/quizzes", () => ok({
  items: [{ quiz_id: "w4q1", course_id: "c1", sub_id: "s1", question: "W4 讲次守卫专用题干", answer: "答案", evidence: { start_ms: 1000 } }],
}));
store.set("activeLecture", { ...LECTURE });
await flushNotes();
assert.match(textOf(el("quiz-list")), /W4 讲次守卫专用题干/, "A 讲次题目已渲染");
routes.set("/api/v3/quizzes", () => errorResponse("quiz_unavailable", 500));
store.set("activeLecture", { ...LECTURE, sub_id: "s2", sub_title: "第二讲·缩聚动力学" });
await flushNotes();
assert.match(textOf(el("quiz-list")), /测验暂时无法加载/, "B 失败显示错误空态");
assert.match(textOf(el("review-list")), /复习计划暂时无法加载/, "复习计划同样不留旧内容");
/* collectAllText 含全历史注册节点（卸下节点 hidden 仍 false），“先渲染后清除”场景用活树断言 */
assert.ok(!textOf(el("quiz-list")).includes("W4 讲次守卫专用题干"), "不残留 A 讲次内容");
assert.ok(!textOf(el("review-list")).includes("W4 讲次守卫专用题干"), "复习计划不残留 A 讲次内容");

/* 14b（EMPTY-STATES-1）：错误空态带「重试」真动作；测验真空态给「生成练习题」
   （复用面板头部「生成测验」链路）；未选讲次给选择引导，不误报「尚未生成」。 */
{
  const quizRetry = el("quiz-list").querySelector(".empty-action");
  assert.ok(quizRetry, "测验失败空态带「重试」动作钮");
  assert.equal(String(quizRetry.textContent || ""), "重试", "重试钮文案闭集");
  const reviewRetry = el("review-list").querySelector(".empty-action");
  assert.ok(reviewRetry, "复习计划失败空态带「重试」动作钮");

  routes.set("/api/v3/quizzes", () => ok({ items: [] }));
  store.set("activeLecture", { ...LECTURE });
  await flushNotes();
  const quizEmptyAction = el("quiz-list").querySelector(".empty-action");
  assert.ok(quizEmptyAction, "测验空态带「生成练习题」动作钮");
  assert.equal(String(quizEmptyAction.textContent || ""), "生成练习题", "动作钮文案闭集");
  const generateSpy = [];
  el("generate-quiz").addEventListener("click", () => generateSpy.push(1));
  quizEmptyAction.click();
  await flushNotes();
  assert.equal(generateSpy.length, 1, "空态动作复用「生成测验」既有链路");

  store.set("activeLecture", null);
  await flushNotes();
  assert.match(textOf(el("quiz-list")), /选择讲次后/, "未选讲次给选择引导");
  assert.ok(!textOf(el("quiz-list")).includes("练习题尚未生成"), "未选讲次不误报「尚未生成」");
}

/* 15) 闭集码映射缺口：cloud_account_required 落可操作指引，而非通用「请稍后重试」 */
routes.set("/api/v3/automation/actions", () => errorResponse("cloud_account_required", 400));
await flushNotes();
automationToggle("w3c1").click(); /* 开关当前 on：走关闭链 → disable-cloud 被闭集码拒绝 */
await flushNotes();
await flushNotes();
assert.match(textOf(el("study-course-list")), /账户与连接/, "cloud_account_required 呈现可操作指引");
assert.ok(!textOf(el("study-course-list")).includes("自动整理设置暂未保存，请稍后重试"), "不再退回无用通用文案");

/* 15b) CLOUD-VERIFY-CHAIN-1：验证族文案指向真实入口（本页课程行云开关）——
        不再误导去无验证入口的设置页；新码 cloud_verification_timeout 落可行动文案 */
routes.set("/api/v3/automation/actions", () => errorResponse("cloud_verification_timeout", 400));
automationToggle("w3c1").click();
await flushNotes();
await flushNotes();
assert.match(textOf(el("study-course-list")), /云端验证还在进行中/, "cloud_verification_timeout 落可行动文案");
assert.ok(!textOf(el("study-course-list")).includes("设置 → 账户与连接"), "验证族文案不再指向设置页");
routes.set("/api/v3/automation/actions", () => errorResponse("cloud_verification_required", 400));
automationToggle("w3c1").click();
await flushNotes();
await flushNotes();
assert.match(textOf(el("study-course-list")), /云开关/, "cloud_verification_required 指向本页课程行云开关");
assert.ok(!textOf(el("study-course-list")).includes("设置 → 账户与连接"), "required 码同样不再指向设置页");

/* 16) W10 跨账号进度缓存：登出（auth 非 ready 确认态）即清空，切换账号后重新 GET */
const progressRouteS1 = "/api/v3/progress?sub_id=s1";
const progressGets = () => requestLog.filter((route) => route === progressRouteS1).length;
routes.set(progressRouteS1, () => ok({ progress: { sub_id: "s1", course_id: "c1", position_seconds: 30, duration_seconds: 600, completed: false } }));
routes.set("/api/v3/catalog?page_size=100", () => ok({
  state: "ready", code: "fudan_catalog_verified", refreshing: false, course_count: 1,
  courses: [{ course_id: "c1", title: "课程甲·高分子化学导论", lectures: [LECTURE, { sub_id: "s2", sub_title: "第二讲·缩聚动力学" }] }],
}));
store.set("auth", { state: "ready", code: "fudan_session_verified", connected: true, configured: true, actions: ["refresh-catalog"] });
await flushNotes();
assert.ok(progressGets() >= 1, "账号 A 首拉讲次进度");
window.dispatchEvent(new CustomEvent("courselens:watch-progress", { detail: { sub_id: "s1", course_id: "c1", position_seconds: 120, duration_seconds: 600 } }));
assert.match(textOf(el("study-lecture-list")), /看到 20%/, "播放回写即时更新讲次卡");
const progressGetsBeforeSwitch = progressGets();
/* NAV-HANG-1 合同修订：真实账号切换/登出落 fudan_credentials_missing
   （configured=false，本地凭据消失）——旧模拟用 login_required+configured=true
   的形状如今属「上游抖动保桌」面，不再清缓存。 */
store.set("auth", { state: "action_required", code: "fudan_credentials_missing", connected: false, configured: false, actions: [] });
await flushNotes();
store.set("auth", { state: "ready", code: "fudan_session_verified", connected: true, configured: true, actions: ["refresh-catalog"] });
await flushNotes();
assert.ok(progressGets() > progressGetsBeforeSwitch, "账号切换后重新 GET 进度（跨账号缓存已清空）");

function collectAllText() {
  return allNodes.filter((node) => !node.hidden).map((node) => String(node.textContent || "")).join("|");
}


/* 12) 考核雷达（N5A-P1）：规则事件的确认/忽略、冲突标注、本讲过滤 */
const radarEvents = [
  { event_id: "evt-1", course_id: "c1", category: "assignment", title: "作业#1", title_norm: "作业#1", due_at: (NOW + 72 * HOUR) / 1000, status: "active", conflict_note: "", evidence: ["下周一交作业"], last_seen_sub_id: "s1", expired: false },
  { event_id: "evt-2", course_id: "c1", category: "exam", title: "期中考试", title_norm: "期中考试", due_at: (NOW + 240 * HOUR) / 1000, status: "active", conflict_note: "时间有出入：10月20日", last_seen_sub_id: "s1", expired: false },
  { event_id: "evt-3", course_id: "c1", category: "quiz", title: "小测", title_norm: "小测", due_at: null, status: "confirmed", conflict_note: "", evidence: [], last_seen_sub_id: "s1", expired: false },
];
let radarAction = null;
routes.set("/api/v3/assessment", (path, options) => {
  if (String(options?.method || "GET") === "POST") {
    radarAction = JSON.parse(String(options?.body || "{}"));
    const event = radarEvents.find((item) => item.event_id === radarAction.event_id);
    if (event) event.status = radarAction.action === "confirm" ? "confirmed" : "dismissed";
    return ok({ schema: "courselens.assessment-radar.v1", event_id: radarAction.event_id, status: event ? event.status : "" });
  }
  return ok({ schema: "courselens.assessment-radar.v1", events: radarEvents, count: radarEvents.length });
});
store.set("activeCourse", { course_id: "c1", title: "课程甲·高分子化学导论" });
store.set("activeLecture", { ...LECTURE });
await flushNotes();
const deskMount = el("assessment-desk-radar");
assert.ok(deskMount.hidden === false, "雷达学习桌块可见");
assert.match(textOf(deskMount), /作业 · 作业#1/, "作业事件呈现");
assert.match(textOf(deskMount), /时间有出入/, "冲突标注呈现");
assert.match(textOf(el("assessment-summary-radar")), /作业/, "总结卡尾部呈现本讲事件");
const rowOf = (mount, id) => mount.children.find((node) => node.dataset && node.dataset.eventId === id);
rowOf(deskMount, "evt-1").children.find((node) => node.textContent === "确认").click();
await flushNotes();
assert.equal(radarAction && radarAction.action, "confirm", "确认动作提交到 actions 路由");
assert.match(textOf(rowOf(deskMount, "evt-1")), /已确认/, "确认态呈现且按钮退场");
rowOf(deskMount, "evt-2").children.find((node) => node.textContent === "忽略").click();
await flushNotes();
assert.equal(radarAction && radarAction.action, "dismiss", "忽略动作提交到 actions 路由");
assert.ok(!rowOf(deskMount, "evt-2"), "忽略后事件不再呈现");
console.log("assessment radar scenarios passed");

/* ---- WP1-D4② 钉：程序化选课与目录点行同一条应用链 ----
   搜索面板/课表跳转只写 store.activeCourse（不点行）时，讲次面板也要按
   所选课程渲染——旧链只解除落地门+切换布局，面板停在旧课程内容。 */
{
  store.set("activeCourse", { course_id: "c2", title: "课程乙·有机波谱分析", lectures: [] });
  await flushNotes();
  assert.match(el("study-course-title").textContent, /课程乙/, "程序化选课更新讲次面板标题");
  assert.match(textOf(el("study-lecture-list")), /暂无已授权讲次/, "讲次面板按所选课程渲染");
  console.log("programmatic course apply renders lecture pane (WP1-D4②)");
}

/* ---- PLAYER-UX-1④：学习桌书签行级删除——两步轻确认+列表即时收敛+事件广播 ---- */
{
  let bookmarkRows = [
    { bookmark_id: "bm-del-1", sub_id: "s1", start_ms: 12000, end_ms: 12000, note: "没听懂", resolution_status: "open" },
    { bookmark_id: "bm-del-2", sub_id: "s1", start_ms: 48000, end_ms: 48000, note: "公式没跟上", resolution_status: "resolved" },
  ];
  const deleteCalls = [];
  routes.delete("/api/v3/bookmarks");
  routes.set("/api/v3/bookmarks", (path, options) => {
    if (String(options?.method || "") === "DELETE") {
      const payload = JSON.parse(String(options?.body || "{}"));
      deleteCalls.push(payload);
      bookmarkRows = bookmarkRows.filter((row) => row.bookmark_id !== String(payload.bookmark_id || ""));
      return ok({ bookmark_id: String(payload.bookmark_id || ""), deleted: true });
    }
    return ok({ bookmarks: bookmarkRows });
  });
  store.set("activeLecture", { ...LECTURE });
  await flushNotes(); await flushNotes();
  const list = el("bookmark-list");
  const rowNodes = () => list.querySelectorAll(".bookmark-row");
  assert.equal(rowNodes().length, 2, "书签行按当前讲次渲染");
  const deleteButton = () => list.querySelectorAll(".bookmark-delete-button")[0];
  assert.ok(deleteButton(), "行级删除钮在场");
  deleteButton().click();
  await flushNotes();
  assert.equal(deleteButton().textContent, "确认删除", "首击只进入确认武装态");
  assert.equal(deleteCalls.length, 0, "首击不发起删除请求");
  deleteButton().click();
  await flushNotes(); await flushNotes();
  assert.deepEqual(deleteCalls, [{ bookmark_id: "bm-del-1" }], "再击发起 DELETE /api/v3/bookmarks");
  assert.equal(rowNodes().length, 1, "删除后列表即时收敛");
  assert.equal(
    rowNodes()[0].querySelector(".bookmark-delete-button").dataset.bookmarkId, "bm-del-2",
    "未删除的行原样保留",
  );
  assert.match(String(textOf(el("bookmark-action-state"))), /已删除/, "状态行人话确认");
  console.log("study bookmark row delete (two-step confirm) passed (PLAYER-UX-1④)");
}

/* ---- FUZZ-INPUT-1 F4/F3（SWEEPFIX-N20）：导入拒绝族具名 toast——空/损坏
   文件的确定性拒绝（400 document_* 闭集码）按因给策（「这个文件是空的…」），
   不再落「请刷新页面重试」误导兜底；成功路径「已导入」照旧。 ---- */
{
  store.activeLecture = { course_id: "c1", sub_id: "s1", sub_title: "导入讲次", can_stream: true };
  const inputNode = document.getElementById("document-input");
  const typeSelect = document.getElementById("document-type-select");
  const scopeCheck = document.getElementById("document-scope-course");
  typeSelect.value = "notes";
  scopeCheck.checked = false;
  const postBodies = [];
  let importFailure = null;
  routes.set("/api/v3/documents", (path, options) => {
    if (String(options?.method || "GET") === "POST") {
      postBodies.push(JSON.parse(String(options?.body || "{}")));
      if (importFailure) {
        return new Response(JSON.stringify(importFailure.payload), {
          status: importFailure.status,
          headers: { "Content-Type": "application/json" },
        });
      }
      return ok({ document: { document_id: "doc-ok" } });
    }
    return ok({ documents: [] });
  });
  const toastText = () => {
    const region = document.getElementById("toast-region");
    const children = region ? [...region.children] : [];
    return String(children[children.length - 1]?.textContent || "");
  };
  const driveImport = async (file) => {
    inputNode.files = [file];
    inputNode.dispatchEvent(new Event("change"));
    await settle();
  };

  /* 空文件：document_empty 闭集码 → 具名「这个文件是空的」+ 零误导兜底 + 零请求体外发？ */
  importFailure = { status: 400, payload: { error: "Document could not be imported", error_code: "document_empty" } };
  await driveImport({ name: "f2_empty.txt", size: 0, type: "text/plain", arrayBuffer: async () => new ArrayBuffer(0) });
  assert.equal(postBodies[0]?.action, "import", "导入动作走闭集");
  assert.equal(postBodies[0]?.content_base64, "", "空文件内容照常送达边界（服务端闭集拒绝）");
  assert.ok(toastText().includes("这个文件是空的"), "document_empty 具名 toast 在场");
  assert.ok(!toastText().includes("请刷新页面重试"), "误导兜底不上屏");

  /* 损坏 .pdf：document_format_invalid → 具名「和扩展名对不上」 */
  importFailure = { status: 400, payload: { error: "Document could not be imported", error_code: "document_format_invalid" } };
  await driveImport({ name: "f2_fake.pdf", size: 24, type: "application/pdf", arrayBuffer: async () => new TextEncoder().encode("this is not a pdf at all").buffer });
  assert.ok(toastText().includes("和扩展名对不上"), "document_format_invalid 具名 toast 在场");

  /* 成功路径回归：「已导入」人话 toast 保持 */
  importFailure = null;
  await driveImport({ name: "good.md", size: 12, type: "text/markdown", arrayBuffer: async () => new TextEncoder().encode("# hello").buffer });
  assert.ok(toastText().includes("已导入"), "成功导入人话 toast 保持");
  routes.delete("/api/v3/documents");
  setDefaultRoutes();
  store.activeLecture = null;
  console.log("study document import rejection named toasts passed (FUZZ F4/F3)");
}

/* ---- WAIT-UX-1（零呆等三律）：等待点行为钉 ----
   I1 文档导入选文件即时指示 / E4 组卷动态进度 / D1 目录刷新长尾步进 /
   E5 课件 PDF 排队窗诚实预计。断言口径：反馈与 change/click 同拍（同步断言，
   不等网络）、进度=状态行在场并逐秒步进、预计文案在位（闭集单源
   wait-expectations.js）。 */
{
  const coursewareEl = (id) => document.getElementById(id);

  /* I1：选文件→同步落「正在导入」行（此前 POST 期全盲），多文件带序号步进；
     完成即收行，toast 语义不变 */
  store.set("activeLecture", { ...LECTURE });
  const importPosts = [];
  routes.set("/api/v3/documents", (path, options) => {
    if (String(options?.method || "GET") === "POST") {
      importPosts.push(JSON.parse(String(options?.body || "{}")));
      return ok({ document: { document_id: "doc-wait" } });
    }
    return ok({ documents: [] });
  });
  const importInput = el("document-input");
  importInput.files = [{
    name: "waitux.pdf", size: 24, type: "application/pdf",
    arrayBuffer: async () => new TextEncoder().encode("this is not a pdf at all").buffer,
  }];
  importInput.dispatchEvent(new Event("change"));
  const importLine = el("document-import-status");
  assert.ok(importLine, "导入状态行已创建");
  assert.equal(importLine.hidden, false, "三律第一律：选文件反馈与 change 同拍在位");
  assert.match(importLine.textContent, /正在导入 waitux\.pdf/, "行内即时指示带文件名");
  await settle();
  assert.equal(importLine.hidden, true, "导入完成即收行");
  assert.equal(importPosts.length, 1, "导入 POST 恰一次");

  importInput.files = [
    { name: "a.md", size: 8, type: "text/markdown", arrayBuffer: async () => new TextEncoder().encode("# a").buffer },
    { name: "b.md", size: 8, type: "text/markdown", arrayBuffer: async () => new TextEncoder().encode("# b").buffer },
  ];
  importInput.dispatchEvent(new Event("change"));
  assert.match(
    el("document-import-status").textContent,
    /正在导入（1\/2）a\.md/,
    "多文件导入带序号进度",
  );
  await settle();
  assert.equal(el("document-import-status").hidden, true, "多文件导入完成收行");
  assert.equal(importPosts.length, 3, "两文件各 POST 一次");
  routes.delete("/api/v3/documents");

  /* E4：生成测验点击即「正在组卷…」，秒级步进已用时间（AI 时长无实测分布，
     按纪律不发明预计数字）；完成收行 */
  routes.set("/api/v3/quizzes", (path, options) => {
    if (String(options?.method || "GET") === "POST") return ok({ quiz_id: "q-wait" });
    return ok({ items: [] });
  });
  defer("/api/v3/quizzes");
  el("generate-quiz").click();
  const quizLine = el("quiz-generate-status");
  assert.ok(quizLine, "组卷状态行已创建");
  assert.equal(quizLine.hidden, false, "三律第一律：点击即反馈在位");
  assert.equal(quizLine.textContent, "正在组卷…", "组卷进度文案在位");
  await sleep(1100);
  assert.match(quizLine.textContent, /正在组卷…（已用 [12] 秒）/, `秒级步进：${quizLine.textContent}`);
  undefers("/api/v3/quizzes");
  await releaseDeferred();
  await settle();
  assert.equal(quizLine.hidden, true, "组卷完成收行");

  /* D1+：刷新提交即落长尾进度行；确认循环按 1s 轮询节拍步进已等待秒数；
     settle 即收行 */
  let catalogCalls = 0;
  routes.set("/api/v3/authentication/actions", (path, options) => {
    if (String(options?.method || "GET") === "POST") return ok({ operation_id: "op-wait-cat" });
    return ok({});
  });
  routes.set("/api/v3/catalog?page_size=100", () => {
    catalogCalls += 1;
    if (catalogCalls <= 2) {
      return ok({ refreshing: true, state: "checking", code: "fudan_session_checking", courses: [], course_count: 0 });
    }
    return ok({ refreshing: false, state: "ready", code: "catalog_ready", courses: store.courses, course_count: 2 });
  });
  el("refresh-catalog").click();
  const catalogLine = () => el("catalog-refresh-progress");
  assert.ok(catalogLine(), "刷新进度行已创建");
  assert.equal(catalogLine().hidden, false, "三律第一律：刷新提交即反馈在位");
  assert.match(catalogLine().textContent, /正在刷新课程目录/, "提交即落长尾阶段文案");
  await settle();
  assert.match(
    catalogLine().textContent,
    /正在(拉取课程目录|确认课程授权)/,
    `确认循环接管阶段文案：${catalogLine().textContent}`,
  );
  await sleep(1100);
  assert.match(catalogLine().textContent, /已等待 [12] 秒/, `1s 轮询节拍步进：${catalogLine().textContent}`);
  await sleep(1100);
  await settle();
  assert.equal(catalogLine().hidden, true, "目录 settle 即收行");
  routes.delete("/api/v3/catalog?page_size=100");
  routes.delete("/api/v3/authentication/actions");

  /* E5+：课件 PDF 排队窗诚实预计在位（有实测样本才写数字——此处闭集措辞零数字） */
  routes.set("/api/v3/courseware-pdf?sub_id=s1", () => ok({ sub_id: "s1", artifact: null, operation: null }));
  routes.set("/api/v3/courseware-pdf/actions", () => {
    routes.set("/api/v3/courseware-pdf?sub_id=s1", () => ok({
      sub_id: "s1", artifact: null,
      operation: { task_id: "op-wait-pdf", state: "queued", label: "课件 PDF 排队中", percent: null, percent_measured: false, resumable: false, error_code: "", counts: null },
    }));
    return ok({ task_id: "task-wait-pdf", state: "queued" });
  });
  store.set("activeLecture", { ...LECTURE });
  await settle();
  coursewareEl("generate-courseware-pdf").click();
  await settle();
  await sleep(1300); /* 乐观轮询接管（≤1.3s，同 11c 口径） */
  await settle();
  assert.match(coursewareEl("courseware-pdf-state").textContent, /排队/, "排队状态可见");
  assert.equal(coursewareEl("courseware-pdf-detail").hidden, false, "三律第三律：>10s 档预计文案在位");
  assert.match(coursewareEl("courseware-pdf-detail").textContent, /整理快慢看本讲画面的多少/, "诚实过程措辞在场");
  assert.ok(
    !/[0-9]/.test(coursewareEl("courseware-pdf-detail").textContent),
    `排队窗无实测样本不写死数：${coursewareEl("courseware-pdf-detail").textContent}`,
  );
  routes.delete("/api/v3/courseware-pdf?sub_id=s1");
  routes.delete("/api/v3/courseware-pdf/actions");
  /* 收口卫生：讲次清零 → 课件轮询链经 stopCoursewarePdfPolling 停摆，
     进程不留长退避定时器（活动操作+路由删除组合会让有界退避跑到上限） */
  store.set("activeLecture", null);
  await settle();
  console.log("WAIT-UX-1 waiting-point pins passed (I1/E4/D1+/E5+)");
}

/* ---- SWEEPFIX-R2 W1（D-20261009-15①）：字幕任务完成跃迁事件 → 文稿热重读，
   笔记按钮随 transcriptHasTiming 热启用（学生停在本讲即可点，不再「重进讲次」）。
   sub_id 定向不误刷；时轴事实已在本场（transcriptHasTiming=true）不重读。 ---- */
{
  const segRoute = "/api/v3/subtitles/segments";
  const segGets = () => requestLog.filter((route) => route.startsWith(segRoute)).length;
  routes.set(segRoute, () => ok({ segments: [] }));
  store.set("activeLecture", { ...LECTURE });
  await settle();
  const before = segGets();
  windowTarget.dispatchEvent(new CustomEvent("courselens:transcript-refresh", { detail: { course_id: "c1", sub_id: "s1" } }));
  await settle();
  assert.ok(segGets() > before, "字幕完成跃迁事件触发文稿重读（修前红=零请求，笔记按钮滞留禁用）");
  const afterMatch = segGets();
  windowTarget.dispatchEvent(new CustomEvent("courselens:transcript-refresh", { detail: { course_id: "c1", sub_id: "other-lecture" } }));
  await settle();
  assert.equal(segGets(), afterMatch, "非当前讲次的完成事件不误刷");
  routes.set(segRoute, () => ok({ segments: [{ start_ms: 1000, end_ms: 4000, text: "热启用后的事实行" }] }));
  store.set("activeLecture", null);
  await settle();
  store.set("activeLecture", { ...LECTURE });
  await settle();
  assert.equal(store.transcriptHasTiming, true, "timed 行落地后 transcriptHasTiming 翻真（player-core 订阅同源热启用笔记按钮）");
  const steady = segGets();
  windowTarget.dispatchEvent(new CustomEvent("courselens:transcript-refresh", { detail: { course_id: "c1", sub_id: "s1" } }));
  await settle();
  assert.equal(segGets(), steady, "时轴事实已在本场不重读（重进/手动刷新链零扰动）");
  console.log("ok: SWEEPFIX-R2 W1 文稿热重读（定向+去重+事实守卫）");
}

console.log("frontend ai learning workspace behavior passed");
