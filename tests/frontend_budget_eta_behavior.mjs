import assert from "node:assert/strict";

/* 任务 ETA 执行型行为测试（§3.3/§5.2 前端规则；触发式计算保护告警链已随
   预算门整体退役，本文件不再覆盖）。桩件法与 tests/frontend_usability_auth_behavior.mjs
   同源：加载真实 store 与真实 tasks-drawer 模块，FakeEventSource / fetch 路由 /
   可检视定时器。本文件只测前端行为。 */

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

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
    this.dataset = {};
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.checked = false;
    this.open = false;
    this.inert = false;
    this.style = {};
    this.title = "";
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
    for (const listener of [...(this._listeners.get(event.type) || [])]) {
      listener.call(this, event);
    }
    if (event.bubbles && this.parent instanceof FakeElement) {
      this.parent.dispatchEvent(event);
    }
    return true;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  removeAttribute(name) { this.attributes.delete(String(name)); }

  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    this.hidden = true;
    if (this.parent) {
      const index = this.parent.children.indexOf(this);
      if (index >= 0) this.parent.children.splice(index, 1);
      this.parent = null;
    }
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

  focus() {
    document.activeElement = this;
  }

  blur() {
    if (document.activeElement === this) document.activeElement = null;
  }

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
      if (elementMatches(node, selector)) return node;
      node = node.parent;
    }
    return null;
  }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.children || []) {
        if (elementMatches(child, selector)) found.push(child);
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

function elementMatches(node, selector) {
  const text = String(selector);
  if (text.includes(":not([disabled])")) {
    return (FOCUSABLE_TAGS.has(node.tagName) && !node.disabled)
      || node.attributes.has("tabindex")
      || (node.tagName === "A" && node.attributes.has("href"));
  }
  if (text.startsWith(".")) {
    return String(node.className || "").split(/\s+/).includes(text.slice(1));
  }
  if (text.startsWith("[")) {
    const name = text.slice(1, -1);
    if (name === "hidden") return Boolean(node.hidden);
    if (name.startsWith("data-")) return node.dataset?.[datasetKey(name)] !== undefined;
    return node.attributes.has(name);
  }
  return node.tagName === text.toUpperCase();
}

class FakeEventSource {
  static all = [];

  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onerror = null;
    this.listeners = new Map();
    FakeEventSource.all.push(this);
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  /* 测试派发后端命名事件（生产 /api/v3/events 只发 event: <topic>，无 message 通道） */
  emit(topic) {
    for (const listener of this.listeners.get(topic) || []) listener(new Event(topic));
  }

  close() { this.closed = true; }
}

/* ---- 页面装配（只装 tasks-drawer 需要的最小结构） ---- */

const BUTTON_IDS = [
  "task-chip", "close-task-drawer", "refresh-tasks",
];

const IDS = [
  ...BUTTON_IDS,
  "workspace-main", "toast-region", "tasks-root", "task-drawer", "task-counts", "task-list",
  "task-chip-count", "task-chip-dot", "task-chip-failed", "remote-drawer-state",
  /* S09-C：抽屉完成摘要 + 任务语义播报 */
  "task-completion-summary", "task-live",
];

const byId = Object.fromEntries(IDS.map((id) => [id, new FakeElement(BUTTON_IDS.includes(id) ? "button" : "div", id)]));
/* 初始 hidden 与 index.html 一致：浮层关闭 */
byId["tasks-root"].hidden = true;
const registry = [...Object.values(byId)];
const register = (node) => {
  registry.push(node);
  return node;
};

/* 抽屉最小结构：scrim 是 tasks-root 子节点，抽屉内含关闭/刷新按钮与列表 */
const drawerScrim = register(new FakeElement("div", "tasks-scrim"));
drawerScrim.dataset.tasksScrim = "";
byId["tasks-root"].append(drawerScrim, byId["task-drawer"]);
byId["task-drawer"].append(
  byId["close-task-drawer"], byId["refresh-tasks"], byId["task-completion-summary"],
  byId["task-list"], byId["task-live"],
);
byId["task-completion-summary"].hidden = true;
byId["task-chip"].dataset.openTasks = "";
byId["task-chip"].setAttribute("aria-expanded", "false");

const createdSeq = { value: 0 };
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => register(new FakeElement(tag, `created-${createdSeq.value += 1}`)),
  querySelectorAll: (selector) => registry.filter((node) => elementMatches(node, selector)),
  querySelector: (selector) => registry.find((node) => elementMatches(node, selector)) || null,
  activeElement: null,
  documentElement: { dataset: {} },
});

const deepElements = (element, into = []) => {
  if (!element) return into;
  into.push(element);
  for (const child of element.children || []) deepElements(child, into);
  return into;
};

const visibleText = (root) => deepElements(root, [])
  .filter((node) => !node.hidden)
  .map((node) => String(node.textContent || ""))
  .join("\n");

/* 真实 store + 真实 tasks-drawer */
const { store } = await import("../frontend/modules/store.js");
const { installTasksDrawer } = await import("../frontend/modules/tasks-drawer.js");

/* ---- 定时器桩：全部可检视、可手动触发（无真实等待） ---- */

const timeouts = new Map();
const intervals = new Map();
let timeoutSeq = 0;
let intervalSeq = 0;
const windowTarget = new EventTarget();
windowTarget.setTimeout = (fn, ms) => {
  timeoutSeq += 1;
  timeouts.set(timeoutSeq, { fn, delay: Number(ms) || 0 });
  return timeoutSeq;
};
windowTarget.clearTimeout = (id) => timeouts.delete(id);
windowTarget.setInterval = (fn, ms) => {
  intervalSeq += 1;
  intervals.set(intervalSeq, { fn, ms: Number(ms) });
  return intervalSeq;
};
windowTarget.clearInterval = (id) => intervals.delete(id);
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.EventSource = FakeEventSource;
globalThis.matchMedia = (query) => ({
  matches: false, media: query,
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
});
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 4; index += 1) await nextTurn();
};
/* 浏览器语义：定时器触发即出队 */
const fireTimeout = (id) => {
  const timer = timeouts.get(id);
  if (!timer) return;
  timeouts.delete(id);
  timer.fn();
};

/* ---- 网络桩 ---- */

const calls = { tasksGet: 0 };
let taskPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
/* 非null时挂起 /api/v3/tasks 响应，用于构造「刷新在途」窗口；resolvers 逐个放行 */
let tasksGate = null;

function ok(data) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (path) => {
  const route = String(path);
  if (route === "/api/v3/tasks") {
    calls.tasksGet += 1;
    if (tasksGate) await new Promise((resolve) => tasksGate.resolvers.push(resolve));
    return ok(taskPayload);
  }
  if (route === "/api/v3/remote-connection") return ok({ overall: { state: "ready", code: "remote_verified" } });
  throw new Error(`unexpected synthetic route: ${route}`);
};

const nowSec = () => Date.now() / 1000;

const tasksWith = (tasks) => ({
  tasks,
  counts: { active: tasks.filter((task) => task.state !== "completed" && task.state !== "failed" && task.state !== "canceled").length, failed: 0, completed: 0 },
});

/* ---- 安装（抽屉全程不打开的前提下完成第一批断言） ---- */

const cleanup = await installTasksDrawer(store);
await settle();

const es = FakeEventSource.all[0];

/* 1) 安装即建立唯一订阅：抽屉从未打开时 EventSource 已存在且存活 */
{
  assert.equal(FakeEventSource.all.length, 1, "安装恰创建一个 EventSource（抽屉未开）");
  assert.equal(es.url, "/api/v3/events?topics=remote-connection,remote-runs,tasks,automation", "订阅 topics 闭集（CLIENT-STATE 补 automation + 连接广播）");
  assert.equal(es.closed, false, "订阅存活");
  assert.equal(byId["tasks-root"].hidden, true, "抽屉未打开");
  assert.equal(calls.tasksGet, 1, "安装时一次有界 GET（应用启动通道）");
  assert.equal([...intervals.values()].filter((t) => t.ms === 30000).length, 1, "30s 轮询保持不变");
  console.log("ok: 安装即建立唯一应用级订阅");
}

/* 2) SSE 消息触发一次 GET（任务新鲜度由消息驱动） */
{
  const before = calls.tasksGet;
  taskPayload = tasksWith([
    { task_id: "t-run", kind: "subtitle", state: "running", updated_at: 100 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(calls.tasksGet, before + 1, "命名事件 tasks 触发一次 GET");
  /* C8-1：后端只发命名事件，订阅 topics 三名各自驱动刷新 */
  es.emit("remote-runs");
  await settle();
  es.emit("remote-connection");
  await settle();
  assert.equal(calls.tasksGet, before + 3, "remote-runs/remote-connection 命名事件同样驱动刷新");
  console.log("ok: SSE 命名事件驱动一次对账 GET（tasks/remote-runs/remote-connection）");
}

/* 6) ETA 闭集映射（合同 §5.2）+ 同档位不抖动 */
function evidenceByTaskName() {
  const result = {};
  const cards = deepElements(byId["task-list"])
    .filter((node) => String(node.className || "").split(/\s+/).includes("task"));
  for (const article of cards) {
    const name = article.querySelectorAll(".t-name")[0]?.textContent;
    const evidence = article.querySelectorAll(".t-evidence")[0]?.textContent;
    if (name && evidence !== undefined) result[name] = evidence;
  }
  return result;
}

{
  const created = nowSec() - 270; /* 4.5 分钟 → ceil 5，容忍毫秒级抖动 */
  taskPayload = tasksWith([
    { task_id: "e1", kind: "eta-queued-elapsed", state: "queued", estimate_basis: "queue", elapsed_queued_seconds: 130, updated_at: 100 },
    { task_id: "e2", kind: "eta-queued-created", state: "queued", estimate_basis: "queue", created_at: created, updated_at: 101 },
    { task_id: "e3", kind: "eta-point-high", state: "running", estimate_basis: "history_median", progress_confidence: "high", remaining_seconds: 540, updated_at: 102 },
    { task_id: "e4", kind: "eta-range-medium", state: "running", estimate_basis: "live_blend", progress_confidence: "medium", remaining_lower_seconds: 480, remaining_upper_seconds: 720, updated_at: 103 },
    { task_id: "e5", kind: "eta-insufficient", state: "running", estimate_basis: "insufficient_data", elapsed_seconds: 180, updated_at: 104 },
    { task_id: "e6", kind: "eta-stale-flag", state: "running", stale: true, updated_at: 105 },
    { task_id: "e7", kind: "eta-stale-basis", state: "running", estimate_basis: "stale", updated_at: 106 },
  ]);
  es.emit("tasks");
  await settle();
  const evidence = evidenceByTaskName();
  assert.equal(evidence["eta-queued-elapsed"].includes("已等待 3 分钟"), true, "排队：elapsed_queued 锚点");
  assert.equal(evidence["eta-queued-elapsed"].includes("等待 GitHub Runner"), true, "排队无后端区间时诚实降级");
  assert.equal(evidence["eta-queued-elapsed"].includes("预计"), false, "无 processing 组时排队卡不输出处理 ETA");
  assert.equal(evidence["eta-queued-created"].includes("已等待 5 分钟"), true, "排队：created_at 回退锚点");
  assert.equal(evidence["eta-point-high"], "进度未知 · 预计约 9 分钟", "高置信点估计");
  assert.equal(evidence["eta-range-medium"].includes("预计还需 8–12 分钟"), true, "中置信区间");
  assert.equal(evidence["eta-insufficient"].includes("正在估算 · 已运行 3 分钟"), true, "资料不足诚实降级");
  /* SWEEPFIX-2 T5：stale 进行中卡唯一 stale 行——t-evidence 复读行退役
     （「等待重新确认」+「证据已过期…」双行收敛为一行人话闭集）。 */
  const staleNotes = deepElements(byId["task-list"])
    .filter((node) => String(node.className || "").split(/\s+/).includes("t-stale-note"))
    .map((node) => String(node.textContent || ""));
  assert.deepEqual(
    staleNotes,
    ["进度信息已过期，等待重新确认", "进度信息已过期，等待重新确认"],
    "stale 标记/basis=stale → 唯一 stale 行（人话软化，修前红=双行复读）",
  );

  /* 技术详情保留 basis/confidence 闭集代码 */
  const technicalText = deepElements(byId["task-list"])
    .filter((node) => String(node.className || "").split(/\s+/).includes("task-technical"))
    .flatMap((node) => deepElements(node).map((child) => String(child.textContent || "")))
    .join("\n");
  assert.ok(technicalText.includes("estimate_basis: history_median"), "技术详情含 estimate_basis");
  assert.ok(technicalText.includes("confidence: high"), "技术详情含 confidence");

  /* 同一分钟档位内字符串不变（不因心跳抖动）；跨档位才更新 */
  taskPayload = tasksWith([
    { task_id: "e3", kind: "eta-point-high", state: "running", estimate_basis: "history_median", progress_confidence: "high", remaining_seconds: 535, updated_at: 102 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(evidenceByTaskName()["eta-point-high"].includes("预计约 9 分钟"), true, "同一分钟档位保持 9 分钟");
  taskPayload = tasksWith([
    { task_id: "e3", kind: "eta-point-high", state: "running", estimate_basis: "history_median", progress_confidence: "high", remaining_seconds: 660, updated_at: 102 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(evidenceByTaskName()["eta-point-high"].includes("预计约 11 分钟"), true, "跨档位更新为 11 分钟");
  console.log("ok: ETA queue/point/range/insufficient/stale 全映射 + 档位守卫");
}

/* 6b) SSE 突发合并：同一刷新周期连续消息只发一次权威 GET；
   在途期间到达的多条消息只补一次对账；后续独立事件仍各自刷新（不吞）。 */
{
  const before = calls.tasksGet;

  /* (a) 微任务窗口内三条连续消息 → 恰一次权威 GET */
  taskPayload = tasksWith([
    { task_id: "co1", kind: "subtitle", state: "running", updated_at: 200 },
  ]);
  es.emit("tasks");
  es.emit("tasks");
  es.emit("tasks");
  await settle();
  assert.equal(calls.tasksGet, before + 1, "连续三条 SSE 消息只触发一次权威 GET");

  /* (b) 周期结束后的独立事件 → 新周期、恰一次新 GET */
  es.emit("tasks");
  await settle();
  assert.equal(calls.tasksGet, before + 2, "后续独立事件仍触发一次新 GET（不被吞）");

  /* (c) 刷新在途期间到达的消息 → 零并发 GET，完成后恰补一次对账 */
  tasksGate = { resolvers: [] };
  es.emit("tasks"); /* 开启在途周期（响应被门挂起） */
  await nextTurn(); /* 微任务已执行：GET 已发出且仍在途 */
  const inFlightGets = calls.tasksGet;
  assert.equal(inFlightGets, before + 3, "在途周期恰发出一次 GET");
  es.emit("tasks"); /* 在途消息 1 → 只置脏 */
  es.emit("tasks"); /* 在途消息 2 → 仍只置脏 */
  assert.equal(calls.tasksGet, inFlightGets, "在途期间零新 GET");
  tasksGate.resolvers.splice(0).forEach((release) => release());
  tasksGate = null;
  await settle();
  assert.equal(calls.tasksGet, inFlightGets + 1, "在途期间多条消息只补一次对账 GET");
  console.log("ok: SSE 突发合并——同周期一次 GET、不并发、不吞后续事件");
}

/* 6c) S09-C 排队卡：queue 组区间 + processing 前瞻短语 + 证据行；
   无样本区间（后端省略）时不发明「通常还需」。 */
{
  taskPayload = tasksWith([
    {
      task_id: "q1", kind: "q-queued-range", state: "queued", estimate_basis: "queue",
      elapsed_queued_seconds: 300, updated_at: 100,
      queue: { elapsed_seconds: 300, lower_seconds: 120, upper_seconds: 300, center_seconds: 180, sample_count: 8, confidence: "medium", basis: "history_median", level: "runner" },
      processing: { elapsed_seconds: null, remaining_seconds: 900, lower_seconds: 780, upper_seconds: 1140, confidence: "medium", basis: "history_median", sample_count: 6 },
    },
    {
      task_id: "q2", kind: "q-queued-bare", state: "queued", estimate_basis: "queue",
      elapsed_queued_seconds: 60, updated_at: 101,
    },
  ]);
  es.emit("tasks");
  await settle();
  const evidence = evidenceByTaskName();
  assert.equal(
    evidence["q-queued-range"].includes("已等待 5 分钟 · 通常还需 2–5 分钟开始 · 开始后预计 13–19 分钟"),
    true, "排队卡三段式：已等待/通常还需/开始后预计",
  );
  assert.equal(evidence["q-queued-bare"].includes("通常还需"), false, "无样本区间时排队卡不发明「通常还需」");
  assert.equal(evidence["q-queued-bare"].includes("等待 GitHub Runner"), true, "无区间排队卡诚实降级");
  /* 证据行闭集：排队卡用「同类运行」，无 processing 组时不编造 */
  const basisNodes = deepElements(byId["task-list"]).filter((node) => String(node.className || "") === "t-basis").map((node) => String(node.textContent || ""));
  assert.ok(basisNodes.some((text) => text.includes("依据：8 次同类运行 · 可信度中")), "排队证据行含运行样本数与可信度");
  assert.equal(basisNodes.length, 1, "无 processing 组的排队卡不编造处理证据行");
  assert.ok(basisNodes.every((text) => !text.includes("undefined")), "证据行不出现未定义插值");
  /* 排队卡没有进度条/百分比：排队等待不是可测量进度 */
  const cards = deepElements(byId["task-list"]).filter((node) => String(node.className || "").split(/\s+/).includes("task"));
  const queuedCards = cards.filter((node) => node.dataset.state === "queued");
  assert.equal(queuedCards.length, 2, "两张排队卡渲染");
  assert.ok(queuedCards.every((node) => node.querySelectorAll(".t-bar").length === 0), "排队卡零进度条");
  assert.ok(queuedCards.every((node) => !visibleText(node).includes("%")), "排队卡零百分比");
  console.log("ok: 排队卡 queue 区间/开始后预计/无样本诚实降级/零进度条");
}

/* 6d) 语义阶段轨：闭集状态渲染 + 形状语义 + 失败/stale 派生 + aria 文本替代 */
{
  taskPayload = tasksWith([
    {
      task_id: "r1", kind: "summary", state: "running", estimate_basis: "live_blend",
      remaining_seconds: 600, updated_at: 100,
      phases: { items: [
        { id: "input", label: "载入字幕", state: "completed" },
        { id: "ocr", label: "PPT OCR", state: "completed" },
        { id: "ai_parts", label: "AI 分段总结", state: "active" },
        { id: "finalize", label: "合并与保存", state: "waiting" },
      ], evidence_updated_at: nowSec() },
    },
    {
      task_id: "r2", kind: "summary", state: "failed", error_code: "remote_failed", updated_at: 101,
      phases: { items: [
        { id: "input", label: "载入字幕", state: "completed" },
        { id: "ocr", label: "PPT OCR", state: "active" },
        { id: "ai_parts", label: "AI 分段总结", state: "waiting" },
      ], evidence_updated_at: nowSec() },
    },
  ]);
  es.emit("tasks");
  await settle();
  const rails = deepElements(byId["task-list"]).filter((node) => node.className === "t-rail");
  assert.equal(rails.length, 2, "两张卡渲染阶段轨");
  const states1 = rails[0].querySelectorAll(".t-rail-phase").map((node) => node.dataset.state);
  assert.deepEqual(states1, ["completed", "completed", "active", "waiting"], "闭集状态原样映射");
  const states2 = rails[1].querySelectorAll(".t-rail-phase").map((node) => node.dataset.state);
  assert.deepEqual(states2, ["completed", "failed", "waiting"], "失败任务的 active 阶段按失败呈现");
  const srText = deepElements(rails[0]).filter((node) => node.className === "sr-only").map((node) => String(node.textContent || "")).join("");
  assert.ok(srText.includes("已完成") && srText.includes("进行中") && srText.includes("等待中"), "阶段状态有文本替代");
  assert.ok(rails[0].querySelectorAll(".t-rail-node").every((node) => node.getAttribute("aria-hidden") === "true"), "形状节点对辅助技术隐藏");
  console.log("ok: 语义阶段轨闭集状态/失败派生/文本替代");
}

/* 6d-3) CLOUD-CONSENT-AUTO-1 U1：云端处理开关退役——cloud_disabled 分码随之
   退役（老库残留行不再指路一个不存在的开关），拒绝面只剩连接/授权未完成
   （cloud_setup_required）一码，文案指路连接步骤 */
{
  taskPayload = tasksWith([
    { task_id: "csr1", kind: "subtitle", state: "failed", error_code: "cloud_disabled", updated_at: 103 },
  ]);
  es.emit("tasks");
  await settle();
  const disabledText = deepElements(byId["task-list"]).map((node) => String(node.textContent || "")).join("\n");
  assert.equal(disabledText.includes("云端处理开关还没打开"), false, "退役码不再有专属文案");
  assert.equal(disabledText.includes("打开「云端处理」开关"), false, "老库残留行不再指路不存在的开关");
  taskPayload = tasksWith([
    { task_id: "csr2", kind: "subtitle", state: "failed", error_code: "cloud_setup_required", updated_at: 104 },
  ]);
  es.emit("tasks");
  await settle();
  const setupText = deepElements(byId["task-list"]).map((node) => String(node.textContent || "")).join("\n");
  assert.ok(setupText.includes("还没完成 GitHub 授权连接"), "cloud_setup_required 指向连接授权步骤");
  console.log("ok: cloud_disabled 退役 / cloud_setup_required 指路连接步骤");
}

/* 6e) 学习材料父卡：同讲次字幕+摘要合并、requested_outputs chips、
   单一任务不合并；未完成产出完整呈现。 */
{
  taskPayload = tasksWith([
    { task_id: "p1", kind: "subtitle", course_id: "c1", sub_id: "s1", state: "completed", updated_at: 100, finished_at: 100, requested_outputs: ["subtitle"] },
    { task_id: "p2", kind: "summary", course_id: "c1", sub_id: "s1", state: "running", estimate_basis: "history_median", remaining_seconds: 600, updated_at: 101, requested_outputs: ["ocr", "summary", "chapters"], phases: { items: [{ id: "input", label: "载入字幕", state: "completed" }, { id: "ocr", label: "PPT OCR", state: "active" }], evidence_updated_at: nowSec() } },
    { task_id: "p3", kind: "subtitle", course_id: "c2", sub_id: "s2", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 102, requested_outputs: ["subtitle"] },
  ]);
  es.emit("tasks");
  await settle();
  const packs = deepElements(byId["task-list"]).filter((node) => String(node.className || "").split(/\s+/).includes("task-pack"));
  assert.equal(packs.length, 1, "同讲次字幕+摘要恰合并为一张学习材料卡");
  const packText = visibleText(packs[0]);
  assert.ok(packText.includes("学习材料"), "父卡标题");
  for (const chip of ["字幕", "课件OCR", "总结", "章节"]) {
    assert.ok(packText.includes(chip), `产出 chip ${chip}`);
  }
  assert.equal(packs[0].querySelectorAll(".pack-item").length, 2, "父卡内两个产出段");
  assert.equal(packs[0].querySelectorAll(".pack-item").filter((node) => node.dataset.primary === "true").length, 1, "恰一个未完成产出主位");
  const primaryItem = packs[0].querySelectorAll(".pack-item").find((node) => node.dataset.primary === "true");
  assert.equal(primaryItem.querySelectorAll(".t-rail").length, 1, "主位产出有阶段轨");
  const singles = deepElements(byId["task-list"]).filter((node) => String(node.className || "").split(/\s+/).includes("task") && !String(node.className || "").split(/\s+/).includes("task-pack"));
  assert.equal(singles.length, 1, "另一讲次单一字幕任务不合并");
  console.log("ok: 学习材料父卡合并/chips/主位产出/独立任务不合并");
}

/* 6f) 抽屉头完成摘要：全部活动任务有上界 → ≤ N 分钟；缺上界诚实隐藏 */
{
  taskPayload = tasksWith([
    { task_id: "h1", kind: "subtitle", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 100, completion: { earliest_seconds: 240, likely_seconds: 300, latest_seconds: 420 } },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(byId["task-completion-summary"].hidden, false, "唯一活动任务有上界 → 显示");
  assert.equal(byId["task-completion-summary"].textContent, "预计全部完成还需 ≤ 7 分钟", "完成摘要取最晚上界");
  taskPayload = tasksWith([
    { task_id: "h1", kind: "subtitle", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 100, completion: { earliest_seconds: 240, likely_seconds: 300, latest_seconds: 420 } },
    { task_id: "h2", kind: "subtitle", state: "queued", estimate_basis: "queue", elapsed_queued_seconds: 30, updated_at: 101 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(byId["task-completion-summary"].hidden, true, "任一任务缺上界 → 摘要隐藏");
  console.log("ok: 完成摘要只在全有界时发布");
}

/* 6g) 抽屉开合：零第二条订阅，打开无 billing/预算副作用 */
{
  byId["task-chip"].click();
  await settle();
  assert.equal(byId["tasks-root"].hidden, false, "抽屉打开");
  assert.equal(FakeEventSource.all.length, 1, "打开不新建订阅");
  document.dispatchEvent(Object.defineProperty(new Event("keydown"), "key", { value: "Escape" }));
  await settle();
  assert.equal(byId["tasks-root"].hidden, true, "抽屉关闭");
  console.log("ok: 抽屉开合零额外订阅与副作用");
}

/* 6h) 任务状态跃迁语义播报：恰一条、只在状态跃迁、分钟档位重绘不播报 */
{
  taskPayload = tasksWith([
    { task_id: "a1", kind: "subtitle", state: "queued", estimate_basis: "queue", elapsed_queued_seconds: 30, updated_at: 100 },
  ]);
  es.emit("tasks");
  await settle();
  byId["task-live"].textContent = "";
  /* 同状态分钟档位重绘：不播报 */
  taskPayload = tasksWith([
    { task_id: "a1", kind: "subtitle", state: "queued", estimate_basis: "queue", elapsed_queued_seconds: 90, updated_at: 100 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(byId["task-live"].textContent, "", "分钟档位重绘零播报");
  /* queued → running：恰好一条播报 */
  taskPayload = tasksWith([
    { task_id: "a1", kind: "subtitle", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 101 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(byId["task-live"].textContent, "「字幕任务」进行中", "状态跃迁一次原子播报");
  console.log("ok: 任务状态跃迁语义播报闭集");
}

/* 6i) 终态卡瘦身单行时长 + 焦点恢复（SSE 重建不夺走键盘焦点） */
{
  taskPayload = tasksWith([
    {
      task_id: "k1", kind: "subtitle", state: "completed", updated_at: 100, finished_at: 100,
      elapsed_seconds: 900, prediction_outcome: { initial_minutes: 20, actual_minutes: 15, delta_minutes: 5 },
    },
  ]);
  es.emit("tasks");
  await settle();
  /* U1 终态卡瘦身：校准行/进度证据退场，完成卡只留单行运行时长 */
  const calibrations = deepElements(byId["task-list"]).filter((node) => node.className === "t-calibration").map((node) => String(node.textContent || ""));
  assert.equal(calibrations.length, 0, "终态卡瘦身：校准行不再上终态卡");
  const terminalEvidence = deepElements(byId["task-list"]).filter((node) => node.className === "t-evidence").map((node) => String(node.textContent || ""));
  assert.ok(terminalEvidence.includes("已运行 15 分钟"), "终态卡单行运行时长：已运行 15 分钟");
  assert.ok(terminalEvidence.every((text) => !text.includes("0.0")), "时长行无假精度");

  byId["task-chip"].click();
  await settle();
  taskPayload = tasksWith([
    { task_id: "k2", kind: "subtitle", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 102, actions: ["pause", "cancel"] },
  ]);
  es.emit("tasks");
  await settle();
  /* SWEEPFIX-2 顺手收口（SWEEPFIX-1 已立案的 HEAD 先在红）：DEAD-TASK-PURGE
     层头「清除卡住的任务」钮常驻 task-list 头部后，裸 BUTTON finder 命中层头钮
     （无 taskKey）——本钉意图是「动作按钮」焦点恢复，finder 收紧为带 taskKey
     的动作按钮，与层头钮零交集。 */
  const firstButton = deepElements(byId["task-list"]).find((node) => node.tagName === "BUTTON" && node.dataset?.taskKey);
  assert.ok(firstButton, "进行中任务卡有动作按钮");
  firstButton.focus();
  const focusBefore = {
    tag: document.activeElement.tagName,
    text: document.activeElement.textContent,
    key: document.activeElement.dataset.taskKey,
  };
  assert.ok(focusBefore.key, "动作按钮携带 task key");
  taskPayload = tasksWith([
    { task_id: "k2", kind: "subtitle", state: "running", estimate_basis: "history_median", remaining_seconds: 300, updated_at: 102, actions: ["pause", "cancel"] },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(document.activeElement.tagName, focusBefore.tag, "重建后焦点回到同标签元素");
  assert.equal(document.activeElement.textContent, focusBefore.text, "重建后焦点回到同文案按钮");
  document.dispatchEvent(Object.defineProperty(new Event("keydown"), "key", { value: "Escape" }));
  await settle();
  assert.equal(byId["tasks-root"].hidden, true, "抽屉关闭");
  console.log("ok: 终态校准行 + SSE 重建焦点原位恢复");
}

/* 6b) NIGHT2-G-B 字幕就绪轻播报：进行中→completed 恰一次 toast；不跳转 */
{
  taskPayload = tasksWith([
    { task_id: "gr1", kind: "subtitle", state: "running", updated_at: 300 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(byId["toast-region"].children.length, 0, "基线帧不播报");
  taskPayload = tasksWith([
    { task_id: "gr1", kind: "subtitle", state: "completed", updated_at: 400 },
  ]);
  es.emit("tasks");
  await settle();
  const readyToasts = () => byId["toast-region"].children
    .filter((node) => String(node.textContent || "").includes("字幕已就绪"));
  assert.equal(readyToasts().length, 1, "就绪 toast 登场恰一次");
  taskPayload = tasksWith([
    { task_id: "gr1", kind: "subtitle", state: "completed", updated_at: 401 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(readyToasts().length, 1, "同一任务不重复播报");
  taskPayload = tasksWith([
    { task_id: "gr2", kind: "subtitle", state: "failed", error_code: "timeout", updated_at: 500 },
  ]);
  es.emit("tasks");
  await settle();
  assert.equal(readyToasts().length, 1, "失败任务不播就绪");
  /* 排掉 toast 自移除定时器（4200ms），不影响场景 7 的零残留清点 */
  for (const [id, timer] of [...timeouts.entries()]) {
    if (timer.delay === 4200) fireTimeout(id);
  }
  await settle();
  console.log("ok: 字幕就绪轻播报恰一次");
}

/* 7) 清理：唯一订阅关闭、定时器全清 */
{
  cleanup();
  await settle();
  assert.equal(es.closed, true, "清理关闭唯一订阅");
  assert.equal(FakeEventSource.all.filter((source) => !source.closed).length, 0, "无残留订阅");
  assert.equal(intervals.size, 0, "轮询与相对时间 interval 全清");
  assert.equal(timeouts.size, 0, "零点/stale 定时器全清");
  console.log("ok: 清理释放订阅与全部定时器");
}

console.log("budget/eta behavior passed");
