import assert from "node:assert/strict";

/* P3 深度回答 palette 行为测试（P3-IMPL-PKGC-1，合同 P3-CONTRACT-1 §①/§④）。
   桩件法与 tests/frontend_academic_ui_behavior.mjs 同源：真实执行
   search-palette.js，假 DOM + fetch 路由桩 + 手动定时器队列（轮询节律可控、
   零真实等待）。覆盖：双动作 IA、快速回答零变化、deep 分派/轮询/终态渲染、
   cancelled/超时/降级、H1 埋点、key 置灰门、seq 门与关停纪律。 */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- fetch 路由桩 ---- */
const fetchCalls = [];
let settingsPayload = { deepseek: { configured: true, saved: true } };
let searchQueue = [];
let deepAnswerQueue = []; /* POST search/answer 的响应载荷 */
let pollQueue = []; /* GET search/answer?task_id 的响应载荷 */
let pollOverride = null; /* () => Response，注入 404 等 */
/* U3 就绪守卫桩：默认已就绪（既有用例零变化）；竞态窗用例改写 payload/override */
let remoteConnectionPayload = { overall: { state: "ready", ready_for_dispatch: true, code: "ready_for_dispatch" }, probing: false };
let remoteConnectionOverride = null;

const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);
const statusResponse = (status, payload) => new Response(JSON.stringify(payload), {
  status, headers: { "Content-Type": "application/json" },
});

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  let body = null;
  if (options.body) { try { body = JSON.parse(options.body); } catch { body = null; } }
  fetchCalls.push({ route: route.replace(/^.*\/api\/v3\//, ""), method, body });
  if (method === "GET" && route.startsWith("/api/v3/settings")) return okEnvelope(settingsPayload);
  if (method === "GET" && route.startsWith("/api/v3/search?")) {
    const value = searchQueue.shift() || { results: [] };
    return okEnvelope({ query: "", filters: {}, total: (value.results || []).length, results: value.results || [] });
  }
  if (method === "GET" && route.startsWith("/api/v3/search-index")) return okEnvelope({ state: "ready", processed: 4, total: 4 });
  if (method === "GET" && route.startsWith("/api/v3/remote-connection")) {
    if (remoteConnectionOverride) return remoteConnectionOverride();
    return okEnvelope(remoteConnectionPayload);
  }
  if (method === "GET" && route.startsWith("/api/v3/search/answer")) {
    if (pollOverride) return pollOverride();
    const value = pollQueue.shift();
    if (!value) throw new Error("poll queue empty");
    return okEnvelope(value);
  }
  if (method === "POST" && route.startsWith("/api/v3/search/answer")) {
    const value = deepAnswerQueue.shift();
    if (!value) throw new Error("deepAnswer queue empty");
    if (value.__status) return statusResponse(value.__status, value.payload || value);
    return okEnvelope(value);
  }
  if (method === "POST" && route.startsWith("/api/v3/tasks/actions")) {
    return okEnvelope({
      operation: { operation_id: String(body?.operation_id || ""), action: body?.action, state: "accepted" },
      task: { task_id: String(body?.task_id || ""), state: "canceling" },
    });
  }
  if (method === "POST" && route.startsWith("/api/v3/analytics/study-events")) {
    return okEnvelope({ recorded: true });
  }
  if (method === "GET" && route.startsWith("/api/v3/tasks")) {
    return okEnvelope({ tasks: [], counts: { active: 0, failed: 0, completed: 0 } });
  }
  throw new Error(`unexpected fetch: ${method} ${route}`);
};

/* EventSource 替身（store/shell 邻面保持惰性） */
globalThis.EventSource = class {
  constructor() { this.closed = false; }
  addEventListener() {}
  removeEventListener() {}
  close() { this.closed = true; }
};

/* ---- 极简 fake DOM（academic 桩同源） ---- */
class FakeClassList {
  constructor(el) { this.el = el; }
  toggle(c, on) { if (on === undefined) { this.el.__classes.has(c) ? this.remove(c) : this.add(c); return this.el.__classes.has(c); } if (on) this.add(c); else this.remove(c); return this.el.__classes.has(c); }
  add(c) { this.el.__classes.add(c); }
  remove(c) { this.el.__classes.delete(c); }
  contains(c) { return this.el.__classes.has(c); }
}

class FakeElement {
  constructor(tag, id) {
    this.tagName = tag.toUpperCase();
    this.id = id || "";
    this.__classes = new Set();
    this.classList = new FakeClassList(this);
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.__text = "";
    this.dataset = {};
    this.style = {};
    this.title = "";
    this.inert = false;
  }
  /* 真实 DOM 语义：置 textContent 清空全部子节点（cites 复用前的清屏依赖它） */
  get textContent() { return this.__text; }
  set textContent(v) { this.__text = String(v ?? ""); this.children = []; }
  get className() { return [...this.__classes].join(" "); }
  set className(v) { this.__classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  setAttribute(n, v) {
    this.attributes.set(n, String(v));
    if (n === "id") this.id = v;
    if (n === "class") String(v).split(/\s+/).filter(Boolean).forEach((c) => this.__classes.add(c));
  }
  getAttribute(n) { return this.attributes.has(n) ? this.attributes.get(n) : null; }
  removeAttribute(n) { this.attributes.delete(n); }
  addEventListener(n, fn) { (this.listeners.get(n) || this.listeners.set(n, []).get(n)).push(fn); }
  removeEventListener(n, fn) {
    const list = this.listeners.get(n) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatch(n, extra = {}) {
    const event = { target: this, preventDefault() {}, ...extra };
    /* 真实 DOM 语义：disabled 按钮不派发 click */
    if (n === "click" && this.disabled) return event;
    (this.listeners.get(n) || []).slice().forEach((fn) => fn(event));
    if (n === "click") (globalThis.document.__clickListeners || []).slice().forEach((fn) => fn(event));
    return event;
  }
  append(...nodes) {
    for (const n of nodes) {
      if (!n) continue;
      if (n.parentElement) n.parentElement.children = n.parentElement.children.filter((c) => c !== n);
      n.parentElement = this;
      this.children.push(n);
    }
  }
  replaceChildren(...nodes) {
    for (const c of this.children) c.parentElement = null;
    this.children = [];
    this.append(...nodes);
  }
  remove() { this.replaceChildren(); if (this.parentElement) { this.parentElement.children = this.parentElement.children.filter((c) => c !== this); this.parentElement = null; } }
  querySelectorAll(sel) { return this.#walk(sel); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  #walk(sel) {
    const simple = String(sel).replace(/^\[|\]$/g, "");
    const out = [];
    const visit = (el) => {
      for (const c of el.children || []) {
        if (sel.startsWith("[")) {
          const [n, v] = simple.split("=");
          const has = c.attributes.get(n);
          if (has !== undefined && (!v || has === v.replace(/['"]/g, ""))) out.push(c);
        } else if (sel.startsWith(".")) {
          if (c.__classes.has(simple.slice(1))) out.push(c);
        } else if (sel.startsWith("#")) {
          if (c.id === simple.slice(1)) out.push(c);
        }
        visit(c);
      }
    };
    visit(this);
    return out;
  }
  contains(el) { let cur = el; while (cur) { if (cur === this) return true; cur = cur.parentElement; } return false; }
  focus() { globalThis.document.__active = this; }
  blur() { if (globalThis.document.__active === this) globalThis.document.__active = null; }
  scrollIntoView() {}
  matches(sel) { return this.closest(sel) === this; }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (String(sel).startsWith("[")) {
        const probe = String(sel).replace(/^\[|\]$/g, "");
        const [n, v] = probe.split("=");
        if (cur.attributes.get(n) !== undefined && (!v || cur.attributes.get(n) === v.replace(/['"]/g, ""))) return cur;
      } else if (String(sel).startsWith(".")) {
        if (cur.__classes.has(String(sel).slice(1))) return cur;
      } else if (String(sel).startsWith("#")) {
        if (cur.id === String(sel).slice(1)) return cur;
      } else if (cur.tagName === String(sel).toUpperCase()) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
}

const PALETTE_IDS = [
  "workspace-main", "global-status", "toast-region", "search-trigger",
  "palette-root", "palette-panel", "palette-input", "palette-status",
  "palette-head", "palette-mode-title", "palette-index-pill",
  "palette-answer", "palette-answer-deep", "palette-answer-cancel",
  "palette-collapse", "palette-answer-card", "palette-answer-label",
  "palette-answer-body", "palette-answer-cites", "palette-answer-caveat",
  "palette-listbox", "player-stage", "study-page",
];
const BUTTON_IDS = new Set(["search-trigger", "palette-answer", "palette-answer-deep", "palette-answer-cancel", "palette-collapse"]);
const el = {};
for (const id of PALETTE_IDS) el[id] = new FakeElement(BUTTON_IDS.has(id) ? "button" : id === "palette-input" ? "input" : "div", id);
el["palette-root"].hidden = true; /* 浮层初始关闭态（与 index.html hidden 属性一致） */
el["search-trigger"].setAttribute("data-open-search", "");
el["palette-root"].children.push(el["palette-panel"]);
el["palette-panel"].parentElement = el["palette-root"];
el["study-page"].dataset.page = "study";

const windowEvents = [];

/* ---- 手动定时器队列（轮询节律可控） ---- */
const timerQueue = new Map();
let timerSeq = 0;
globalThis.window = {
  addEventListener() {},
  removeEventListener() {},
  setTimeout: (fn, ms) => { timerSeq += 1; timerQueue.set(timerSeq, { fn, ms }); return timerSeq; },
  clearTimeout: (handle) => { timerQueue.delete(handle); },
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => { fn(); return 0; },
  dispatchEvent: (ev) => { windowEvents.push(String(ev?.type || "")); },
};
globalThis.clearTimeout = (handle) => { timerQueue.delete(handle); };

async function fireTimers() {
  const batch = [...timerQueue.values()];
  timerQueue.clear();
  for (const t of batch) t.fn();
  await sleep(5);
}
const pendingTimerCount = () => timerQueue.size;

globalThis.document = {
  __clickListeners: [],
  __keyListeners: [],
  __active: null,
  getElementById: (id) => el[id] || null,
  createElement: (tag) => new FakeElement(tag, ""),
  querySelector: (sel) => {
    if (sel === ".page:not([hidden])") return el["study-page"].hidden ? null : el["study-page"];
    return el[String(sel).replace(/^#/, "")] || null;
  },
  querySelectorAll: (sel) => {
    if (String(sel).includes("palette-listbox")) return el["palette-listbox"].querySelectorAll("[role='option']");
    if (String(sel).startsWith("[data-open-search]")) return [el["search-trigger"]];
    if (String(sel) === ".page:not([hidden])") return el["study-page"].hidden ? [] : [el["study-page"]];
    return [];
  },
  addEventListener(n, fn) { if (n === "click") this.__clickListeners.push(fn); if (n === "keydown") this.__keyListeners.push(fn); },
  removeEventListener(n, fn) {
    for (const list of [this.__clickListeners, this.__keyListeners]) {
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    }
  },
  dispatchEvent(ev) {
    const type = ev.type || (ev.key !== undefined ? "keydown" : "click");
    if (type === "keydown") { this.__keyListeners.slice().forEach((fn) => fn(ev)); return; }
    this.__clickListeners.slice().forEach((fn) => fn(ev));
  },
  body: new FakeElement("body", ""),
  get activeElement() { return this.__active || null; },
  set activeElement(v) { this.__active = v; },
};

/* ---- 被测模块 ---- */
const { store } = await import("../frontend/modules/store.js");
const palette = await import("../frontend/modules/search-palette.js");

store.set("auth", { state: "ready", code: "ok" });
store.set("courses", [{
  course_id: "c1", title: "高等数学", teacher: "师",
  lectures: [{ sub_id: "s1", sub_title: "第1讲 极限" }],
}]);
/* 学生在学习桌上下文发起提问：活动课程/讲次决定回答范围（既有合同） */
store.set("activeCourse", store.courses[0]);
store.set("activeLecture", { sub_id: "s1", sub_title: "第1讲 极限", course_id: "c1", course_title: "高等数学" });

await palette.installSearchPalette(store);

const $id = (id) => document.getElementById(id);
async function openPalette() {
  /* 前一测试残留浮层先收干净（Esc 逐级：answer/full → compact → 关闭） */
  if (!$id("palette-root").hidden) {
    document.dispatchEvent({ key: "Escape", preventDefault() {} });
    await sleep(5);
    if (!$id("palette-root").hidden) {
      document.dispatchEvent({ key: "Escape", preventDefault() {} });
      await sleep(5);
    }
  }
  fetchCalls.length = 0;
  windowEvents.length = 0;
  el["search-trigger"].dispatch("click");
  await sleep(10);
}
const citationChip = () => $id("palette-answer-cites").children.find((c) => c.__classes.has("citation-action"));

/* ---- 测试 1：双动作 IA——两按钮并存、key 门默认放行（文案钉在 py wrapper 静态检查） ---- */
{
  await openPalette();
  assert.equal($id("palette-answer").disabled, false, "key 已配置→快速可用");
  assert.equal($id("palette-answer-deep").disabled, false, "key 已配置→深度可用");
  assert.equal($id("palette-answer-card").hidden, true, "compact 不显示卡片");
  console.log("ok: 双动作 IA 两按钮并存 + key 门默认放行");
}

/* ---- 测试 2：快速回答零变化——POST 不带 mode、本地拼装诚实标签 ---- */
{
  deepAnswerQueue.length = 0;
  pollQueue.length = 0;
  deepAnswerQueue.push({
    answer: "拼装回答",
    citations: [{ source: "subtitle", label: "00:01", course_id: "c1", sub_id: "s1", start_seconds: 5, end_seconds: 9, snippet: "x", source_hash: "h" }],
  });
  $id("palette-input").value = "极限怎么求";
  $id("palette-answer").dispatch("click");
  await sleep(10);
  const post = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(post, "快速 POST 发生");
  assert.equal(post.body.mode, undefined, "快速回答不带 mode（逐字保持现状）");
  assert.equal(post.body.sub_id, "s1", "快速回答带活动讲次");
  assert.equal($id("palette-answer-label").textContent, "证据回答 · 本地拼装", "本地拼装诚实标签");
  assert.equal($id("palette-answer-body").textContent, "拼装回答");
  assert.ok($id("palette-answer-caveat").textContent.includes("本地拼装"), "快速 caveat 诚实");
  assert.equal($id("palette-answer-cancel").hidden, true, "快速态无取消钮");
  /* 快速引用 chip 点击不发 H1 埋点 */
  fetchCalls.length = 0;
  const chip = citationChip();
  assert.ok(chip, "快速引用渲染为 citation-action chip");
  chip.dispatch("click");
  await sleep(5);
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("analytics/study-events")).length, 0, "快速 chip 零埋点");
  assert.equal(el["player-stage"].currentTime, 5, "快速 chip seek 生效");
  console.log("ok: 快速回答零变化（无 mode/诚实标签/零埋点/seek）");
}

/* ---- 测试 3：深度分派——mode=deep 入参、排队态、取消钮、tasks-refresh ---- */
{
  deepAnswerQueue.length = 0;
  pollQueue.length = 0;
  windowEvents.length = 0;
  deepAnswerQueue.push({
    mode: "deep", task_id: "t-123", task_state: "queued", created: true,
    query: "极限怎么求", input_hash: "abc123",
  });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  const post = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(post, "深度 POST 发生");
  assert.equal(post.body.mode, "deep", "深度 POST 携带 mode=deep");
  assert.equal(post.body.sub_id, "s1", "深度沿用活动讲次范围");
  assert.equal($id("palette-answer-label").textContent, "深度回答 · 模型生成", "深度标签");
  assert.equal($id("palette-answer-body").textContent, "正在排队深度回答…", "排队态文案");
  assert.ok($id("palette-answer-caveat").textContent.includes("DeepSeek 额度"), "成本透明句");
  assert.equal($id("palette-answer-cancel").hidden, false, "轮询期取消钮可见");
  assert.ok(windowEvents.includes("courselens:tasks-refresh"), "任务抽屉刷新照发");
  assert.equal(pendingTimerCount(), 1, "3s 轮询已排程");
  console.log("ok: 深度分派入参/排队态/取消钮/tasks-refresh");
}

/* ---- 测试 4：轮询进行态跟随 task.label（+ ETA 闭集尾巴，U2） ---- */
{
  pollQueue.push({ task: { task_id: "t-123", state: "running", label: "深度回答·云端生成中" }, answer: null });
  await fireTimers();
  const poll = fetchCalls.find((c) => c.method === "GET" && c.route.startsWith("search/answer"));
  assert.ok(poll, "GET 读出发生");
  assert.ok(poll.route.includes("task_id=t-123"), "按 task_id 轮询");
  assert.equal($id("palette-answer-body").textContent, "深度回答·云端生成中 · 正在估算", "标签跟随 task.label + 无估值时诚实「正在估算」（U2 等待可视化）");
  assert.equal(pendingTimerCount(), 1, "继续 3s 轮询");
  console.log("ok: 轮询进行态跟随 task.label + ETA 尾巴");
}

/* ---- 测试 4b：轮询排队态消费 ETA 闭集（U2——数据已在 tasks 面，仅答案卡未消费） ---- */
{
  pollQueue.push({
    task: {
      task_id: "t-123", state: "queued", label: "深度回答等待在线计算",
      estimate_basis: "queue", stale: false,
      elapsed_queued_seconds: 45,
      queue: { lower_seconds: 30, upper_seconds: 90 },
      processing: { lower_seconds: 90, upper_seconds: 150 },
    },
    answer: null,
  });
  await fireTimers();
  const body = $id("palette-answer-body").textContent;
  assert.ok(body.startsWith("深度回答等待在线计算 · "), "排队标签在前（U2）");
  assert.ok(body.includes("已等待 1 分钟"), "已等待段（与任务抽屉同一闭集）");
  assert.ok(body.includes("通常还需"), "排队预计段");
  assert.ok(body.includes("开始后预计"), "开始后预计段");
  assert.equal(pendingTimerCount(), 1, "继续 3s 轮询");
  console.log("ok: 排队态答案卡 ETA 闭集（task-cards 单源复用）");
}

/* ---- 测试 5：完成渲染 + 引用 chip + H1 埋点 ---- */
{
  pollQueue.push({
    task: { task_id: "t-123", state: "completed", label: "任务已完成" },
    answer: {
      answer: "根据课件，极限的定义是……（含解题思路）",
      citations: [{
        citation_id: "e1", course_id: "c1", sub_id: "s1", start_seconds: 42, end_seconds: 60,
        snippet: "极限定义", source_hash: "hh", source: "subtitle", label: "00:42",
      }],
      grounded: true, mode: "remote", query: "极限怎么求", input_hash: "abc123",
      model: "deepseek-flash", prompt_version: "bookmark-answer-v1",
    },
  });
  fetchCalls.length = 0;
  await fireTimers();
  assert.equal($id("palette-answer-label").textContent, "深度回答 · 模型生成", "完成仍为深度标签");
  assert.ok($id("palette-answer-body").textContent.includes("解题思路"), "答案正文渲染");
  assert.equal($id("palette-answer-cancel").hidden, true, "终态取消钮收起");
  const chip = citationChip();
  assert.ok(chip, "深度引用渲染为 chip");
  assert.equal(chip.textContent.includes("subtitle"), true, "chip 文案含来源");
  fetchCalls.length = 0;
  windowEvents.length = 0;
  chip.dispatch("click");
  await sleep(5);
  const telemetry = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("analytics/study-events"));
  assert.ok(telemetry, "H1 埋点 fire-and-forget 发生");
  assert.equal(telemetry.body.kind, "palette_citation_click", "埋点 kind 闭集");
  assert.equal(telemetry.body.course_id, "c1", "埋点 course_id");
  assert.equal(telemetry.body.sub_id, "s1", "埋点 sub_id");
  assert.equal(telemetry.body.dwell_ms, 0, "埋点 dwell_ms=0");
  assert.equal(el["player-stage"].currentTime, 42, "chip seek 回原位");
  assert.equal($id("palette-root").hidden, true, "chip 点击先关浮层再导航");
  console.log("ok: 深度完成渲染 + chip seek + H1 埋点（kind/course/sub/dwell 闭集）");
}

/* ---- 测试 6：insufficient 固定文案 / failed 闭集码人话 / canceled ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-ins", task_state: "queued", created: true, query: "q", input_hash: "h1" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  pollQueue.push({ task: { task_id: "t-ins", state: "completed" }, answer: { state: "insufficient", answer: "资料不足，无法根据当前课程资料回答。", citations: [], grounded: false } });
  await fireTimers();
  assert.equal($id("palette-answer-body").textContent, "资料不足，无法根据当前课程资料回答。", "insufficient 固定文案逐字");
  assert.equal(pendingTimerCount(), 0, "终态后停止轮询");

  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-fail", task_state: "queued", created: true, query: "q", input_hash: "h2" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  pollQueue.push({ task: { task_id: "t-fail", state: "failed", error_code: "deepseek_rate_limited", label: "任务失败" }, answer: null });
  await fireTimers();
  assert.ok($id("palette-answer-body").textContent.includes("限流"), "失败码人话文案");
  assert.ok($id("palette-answer-body").textContent.includes("任务中心重试"), "失败态带任务中心重试提示");
  assert.equal(pendingTimerCount(), 0, "失败后停止轮询");

  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-cancel", task_state: "queued", created: true, query: "q", input_hash: "h3" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  pollQueue.push({ task: { task_id: "t-cancel", state: "canceled", label: "任务已取消" }, answer: null });
  await fireTimers();
  assert.ok($id("palette-answer-body").textContent.includes("已取消"), "canceled 态文案");
  assert.equal(pendingTimerCount(), 0, "取消终态后停止轮询");
  console.log("ok: insufficient/failed/canceled 三终态渲染");
}

/* ---- 测试 7：取消流——POST tasks/actions cancel + 回快速回答态 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-user", task_state: "queued", created: true, query: "q", input_hash: "h4" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal(pendingTimerCount(), 1, "取消前排程在");
  fetchCalls.length = 0;
  $id("palette-answer-cancel").dispatch("click");
  await sleep(10);
  const cancel = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("tasks/actions"));
  assert.ok(cancel, "取消 POST 发生");
  assert.equal(cancel.body.action, "cancel", "取消动作");
  assert.equal(cancel.body.task_id, "t-user", "取消目标 task_id");
  assert.ok(String(cancel.body.operation_id || "").length >= 8, "operation_id 满足 8-128 规则");
  assert.ok($id("palette-answer-body").textContent.includes("已取消深度回答"), "取消后诚实文案");
  assert.equal($id("palette-answer-label").textContent, "证据回答 · 本地拼装", "取消后回快速回答态");
  assert.equal(pendingTimerCount(), 0, "取消后轮询停止");
  console.log("ok: 取消流（POST cancel + 回快速回答态 + 停轮询）");
}

/* ---- 测试 8：declined 即时降级——零轮询、未使用云端调用 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({
    answer: "资料不足，无法根据当前课程资料回答。", citations: [], grounded: false,
    mode: "declined", query: "量子化学", error_code: "deep_answer_evidence_unavailable",
  });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal($id("palette-answer-body").textContent, "资料不足，无法根据当前课程资料回答。", "declined 固定判词逐字");
  assert.ok($id("palette-answer-caveat").textContent.includes("未使用云端调用"), "零成本如实告知");
  assert.equal(pendingTimerCount(), 0, "declined 零轮询");
  assert.equal(fetchCalls.filter((c) => c.method === "GET" && c.route.startsWith("search/answer")).length, 0, "declined 零读出");
  console.log("ok: declined 即时降级（零任务零轮询零成本提示）");
}

/* ---- 测试 9：合同外形状（PKG-B 在途窗口）→ 如实按快速回答呈现 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ answer: "本地拼装结果", citations: [] });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal($id("palette-answer-label").textContent, "证据回答 · 本地拼装", "不给本地拼装挂模型名头");
  assert.ok($id("palette-answer-caveat").textContent.includes("深度入口还没接上后端"), "窗口期诚实说明");
  assert.equal(pendingTimerCount(), 0, "零轮询");
  console.log("ok: PKG-B 在途窗口降级为诚实快速回答");
}

/* ---- 测试 10：读出 404 两态——路由未就绪降级 vs task_task_unknown ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-404", task_state: "queued", created: true, query: "q", input_hash: "h5" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  pollOverride = () => statusResponse(404, { error: "route not found" });
  fetchCalls.length = 0;
  await fireTimers();
  assert.ok($id("palette-answer-body").textContent.includes("读取面还没就绪"), "读出未就绪诚实降级");
  assert.ok($id("palette-answer-body").textContent.includes("任务中心"), "降级文案指向任务中心");
  fetchCalls.length = 0;
  await fireTimers();
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("search/answer")).length, 0, "降级后停止轮询");

  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-gone", task_state: "queued", created: true, query: "q", input_hash: "h6" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  pollOverride = () => statusResponse(404, { error: "task unknown", error_code: "task_task_unknown" });
  await fireTimers();
  assert.ok($id("palette-answer-body").textContent.includes("已经不在了"), "task_task_unknown 人话文案");
  pollOverride = null;
  console.log("ok: 读出 404 两态（路由未就绪 vs 任务不存在）");
}

/* ---- 测试 11：10 分钟封顶 + 瞬态错误继续轮询 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-dead", task_state: "queued", created: true, query: "q", input_hash: "h7" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  /* 瞬态错误：不终止轮询（普通 Error 原样上抛，与网络瞬态同路） */
  pollOverride = () => { throw new Error("synthetic transient"); };
  fetchCalls.length = 0;
  await fireTimers();
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("search/answer")).length, 1, "瞬态错误发生了一次读出");
  assert.equal(pendingTimerCount(), 1, "瞬态错误后继续排程");
  /* 时间推进越过 10 分钟上限 → 封顶文案，停轮询 */
  const realNow = Date.now;
  Date.now = () => realNow() + 601000;
  try {
    pollOverride = null;
    pollQueue.push({ task: { task_id: "t-dead", state: "running", label: "x" }, answer: null });
    await fireTimers();
    assert.ok($id("palette-answer-body").textContent.includes("后台计算"), "封顶文案");
    assert.ok($id("palette-answer-body").textContent.includes("任务中心"), "封顶指向任务中心");
    assert.equal(pendingTimerCount(), 0, "封顶后停止轮询");
  } finally {
    Date.now = realNow;
  }
  console.log("ok: 瞬态续轮询 + 10 分钟封顶停轮询");
}

/* ---- 测试 12：浮层关闭停轮询 + seq 门隔离 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-close", task_state: "queued", created: true, query: "q", input_hash: "h8" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal(pendingTimerCount(), 1, "关停前排程在");
  /* Esc 逐级返回：answer→compact（浮层仍在），再 compact→关闭（onClose 停轮询） */
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  await sleep(5);
  assert.equal($id("palette-root").hidden, false, "Esc 第一步 answer→compact 不关闭");
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  await sleep(5);
  assert.equal($id("palette-root").hidden, true, "Esc 第二步 compact→关闭");
  assert.equal(pendingTimerCount(), 0, "onClose 停轮询清定时器");
  fetchCalls.length = 0;
  await fireTimers();
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("search/answer")).length, 0, "关闭后轮询零触发");

  /* seq 门：深度在途时点快速回答，深度轮询被隔离 */
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ mode: "deep", task_id: "t-race", task_state: "queued", created: true, query: "q", input_hash: "h9" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  deepAnswerQueue.push({ answer: "快速回答", citations: [] });
  $id("palette-answer").dispatch("click");
  await sleep(10);
  pollQueue.push({ task: { task_id: "t-race", state: "completed" }, answer: { answer: "晚到深度答案", citations: [] } });
  await fireTimers();
  assert.equal($id("palette-answer-body").textContent, "快速回答", "晚到深度结果不污染快速态");
  assert.equal($id("palette-answer-label").textContent, "证据回答 · 本地拼装", "快速标签未被晚到深度覆盖");
  console.log("ok: 浮层关停轮询 + seq 门隔离晚到深度结果");
}

/* ---- 测试 13：key 未配置置灰 + 提示走既有文案 ---- */
{
  settingsPayload = { deepseek: { configured: false, saved: false } };
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  assert.equal($id("palette-answer-deep").disabled, true, "无 key→深度置灰");
  assert.ok(String($id("palette-answer-deep").title).includes("DeepSeek Key"), "置灰提示走既有文案");
  assert.equal($id("palette-answer").disabled, false, "快速回答不受 key 门影响");
  deepAnswerQueue.push({ mode: "deep", task_id: "t-nokey", task_state: "queued", created: true, query: "q", input_hash: "ha" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal(fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("search/answer")).length, 0, "置灰态点击零请求");
  settingsPayload = { deepseek: { configured: true, saved: true } };
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  assert.equal($id("palette-answer-deep").disabled, false, "恢复配置→深度可用");
  console.log("ok: key 门置灰/恢复 + 置灰态零请求");
}

/* ---- 测试 14：declined/失败等不误发 tasks-refresh；派发才发 ---- */
{
  deepAnswerQueue.length = 0; /* 清掉测试 13 置灰态未消费的载荷 */
  pollQueue.length = 0;
  windowEvents.length = 0;
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  deepAnswerQueue.push({ answer: "资料不足，无法根据当前课程资料回答。", citations: [], grounded: false, mode: "declined", query: "q", error_code: "deep_answer_evidence_unavailable" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal(windowEvents.filter((t) => t === "courselens:tasks-refresh").length, 0, "declined 无任务不刷抽屉");
  deepAnswerQueue.push({ mode: "deep", task_id: "t-ev", task_state: "queued", created: true, query: "q", input_hash: "hb" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.ok(windowEvents.includes("courselens:tasks-refresh"), "派发任务才刷抽屉");
  console.log("ok: tasks-refresh 仅任务派发时发");
}

/* ---- 测试 15：U3 竞态窗注入 N 轮——就绪中不受理不失败，就绪即自动续发 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  remoteConnectionPayload = { overall: { state: "unknown", ready_for_dispatch: false, code: "status_unknown" }, probing: true };
  deepAnswerQueue.push({ mode: "deep", task_id: "t-warm", task_state: "queued", created: true, query: "q", input_hash: "hw" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  assert.equal(fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("search/answer")).length, 0, "就绪中零派发（不制造必败任务）");
  assert.ok($id("palette-answer-body").textContent.includes("远程通道就绪中"), "就绪中诚实等待面");
  assert.ok($id("palette-answer-body").textContent.includes("已等待"), "等待可视化（已等待 N 秒）");
  /* 注入 N=2 轮未就绪（每轮=一次可唤醒睡眠+一次就绪读），全程零失败 */
  await fireTimers();
  await fireTimers();
  assert.equal(fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("search/answer")).length, 0, "N 轮未就绪零派发零失败");
  remoteConnectionPayload = { overall: { state: "ready", ready_for_dispatch: true, code: "ready_for_dispatch" }, probing: false };
  await fireTimers();
  const post = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(post, "就绪后自动续发");
  assert.equal(post.body.mode, "deep", "续发仍 mode=deep");
  assert.equal($id("palette-answer-body").textContent, "正在排队深度回答…", "就绪续发回到排队面");
  /* 收口：任务终态停轮询 */
  pollQueue.push({ task: { task_id: "t-warm", state: "canceled", label: "任务已取消" }, answer: null });
  await fireTimers();
  console.log("ok: U3 竞态窗 N 轮注入零失败+就绪自动续发");
}

/* ---- 测试 16：U3 守卫窗到顶——诚实不发起零成本 ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  remoteConnectionPayload = { overall: { state: "unknown", ready_for_dispatch: false, code: "status_unknown" }, probing: true };
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  const realNow = Date.now;
  Date.now = () => realNow() + 61000;
  try {
    await fireTimers();
    assert.equal(fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("search/answer")).length, 0, "守卫窗到顶零派发");
    assert.ok($id("palette-answer-body").textContent.includes("没有发起"), "到顶诚实文案");
    assert.ok($id("palette-answer-body").textContent.includes("未使用云端调用"), "零成本如实告知");
    assert.equal(pendingTimerCount(), 0, "到顶后零挂起定时器");
  } finally {
    Date.now = realNow;
  }
  console.log("ok: U3 守卫窗到顶诚实停（零任务零成本零挂起）");
}

/* ---- 测试 17：U3 结论性未就绪照旧受理 + 读失败 fail-open（既有行为零回退） ---- */
{
  await openPalette();
  $id("palette-input").value = "极限怎么求";
  remoteConnectionPayload = { overall: { state: "action_required", ready_for_dispatch: false, code: "authorization_missing" }, probing: false };
  deepAnswerQueue.push({ mode: "deep", task_id: "t-concl", task_state: "queued", created: true, query: "q", input_hash: "hx" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  const post = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(post, "结论性未就绪（未配置/需操作）照旧派发，后端闭集码裁决");
  assert.ok($id("palette-answer-body").textContent.includes("正在排队深度回答"), "受理即排队面");
  pollQueue.push({ task: { task_id: "t-concl", state: "canceled", label: "任务已取消" }, answer: null });
  await fireTimers();

  await openPalette();
  $id("palette-input").value = "极限怎么求";
  remoteConnectionOverride = () => { throw new Error("readiness read down"); };
  deepAnswerQueue.push({ mode: "deep", task_id: "t-open", task_state: "queued", created: true, query: "q", input_hash: "hy" });
  $id("palette-answer-deep").dispatch("click");
  await sleep(10);
  const postOpen = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(postOpen, "就绪读失败 fail-open 照旧派发（既有行为零回退）");
  remoteConnectionOverride = null;
  remoteConnectionPayload = { overall: { state: "ready", ready_for_dispatch: true, code: "ready_for_dispatch" }, probing: false };
  pollQueue.push({ task: { task_id: "t-open", state: "canceled", label: "任务已取消" }, answer: null });
  await fireTimers();
  console.log("ok: U3 结论性未就绪照旧受理 + fail-open 零回退");
}

console.log("frontend palette deep answer behavior passed");
