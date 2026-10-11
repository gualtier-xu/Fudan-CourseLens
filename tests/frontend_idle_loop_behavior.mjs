import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";

/* PF1 稳态空闲循环降载：行为钉 + 空闲负载量化（IDLE-LOOP，2026-10-07）。
   桩件法与 tests/frontend_tasks_center_behavior.mjs 同源：真实 store /
   tasks-drawer / task-cards / ui 模块 + FakeElement / fetch 路由桩。
   两种模式：
     node tests/frontend_idle_loop_behavior.mjs           行为钉模式（断言，进套件）
     node tests/frontend_idle_loop_behavior.mjs --bench   量化模式（只打印 BENCH_JSON，改前/改后各跑一次）
   量化口径：空闲 30 拍（每拍 GET /tasks 返回同内容载荷 + 新 observed_at，
   与生产一致——GET 响应 observed_at 每次必变），统计全量重建拍数、
   createElement 次数、tasks 订阅者扇出、通配扇出、CPU/墙钟。 */

const BENCH = process.argv.includes("--bench");

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
  removeEventListener(type, listener) { this._listeners.get(type)?.delete(listener); }
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
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof FakeElement) {
        node.parent = this;
        this.children.push(node);
      }
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...nodes);
  }
  remove() {
    if (!(this.parent instanceof FakeElement)) return;
    const index = this.parent.children.indexOf(this);
    if (index >= 0) this.parent.children.splice(index, 1);
    this.parent = null;
  }
  querySelectorAll(selector) {
    return deepElements(this).filter((node) => node !== this && elementMatches(node, selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.focused = true; }
  closest(selector) {
    let node = this;
    while (node instanceof FakeElement) {
      if (elementMatches(node, selector)) return node;
      node = node.parent;
    }
    return null;
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

function deepElements(element, into = []) {
  if (!element) return into;
  into.push(element);
  for (const child of element.children || []) deepElements(child, into);
  return into;
}

const BUTTON_IDS = ["task-chip", "close-task-drawer", "refresh-tasks"];
const IDS = [
  ...BUTTON_IDS,
  "workspace-main", "toast-region", "tasks-root", "task-drawer", "task-counts", "task-list",
  "task-chip-count", "task-chip-dot", "task-chip-failed", "remote-drawer-state",
  "task-completion-summary", "task-live",
];
const byId = Object.fromEntries(IDS.map((id) => [id, new FakeElement(BUTTON_IDS.includes(id) ? "button" : "div", id)]));
byId["tasks-root"].hidden = true;
byId["task-completion-summary"].hidden = true;

const registry = [...Object.values(byId)];
const createdSeq = { value: 0 };
let createElementCalls = 0;
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => {
    createdSeq.value += 1;
    createElementCalls += 1;
    const node = new FakeElement(tag, `created-${createdSeq.value}`);
    registry.push(node);
    return node;
  },
  querySelectorAll: (selector) => registry.filter((node) => elementMatches(node, selector)),
  querySelector: (selector) => registry.find((node) => elementMatches(node, selector)) || null,
  activeElement: null,
  documentElement: { dataset: {} },
});

globalThis.window = Object.assign(new EventTarget(), {
  setTimeout: (fn) => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => fn(),
});
globalThis.EventSource = class {
  constructor() { this.closed = false; }
  close() { this.closed = true; }
};
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};

/* 网络桩：GET /tasks 每次返回「同内容 + 新 observed_at」的可变载荷——
   与生产一致（GET /tasks 响应 observed_at=time.time() 每拍必变）。 */
let tasksPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
let tasksGetCalls = 0;
const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route === "/api/v3/tasks") {
    tasksGetCalls += 1;
    return okEnvelope({ ...tasksPayload, observed_at: performance.now() / 1000 });
  }
  if (method === "GET" && route === "/api/v3/remote-connection") {
    return okEnvelope({ overall: { state: "ready" } });
  }
  if (method === "GET" && route === "/api/v3/automation") {
    return okEnvelope({ schema: "courselens.automation.v2", state: "disabled", runs: [] });
  }
  throw new Error(`unexpected synthetic route: ${route}`);
};

const { store } = await import("../frontend/modules/store.js");
const drawer = await import("../frontend/modules/tasks-drawer.js");

const makeTask = (task_id, state, extra = {}) => ({
  task_id, course_id: "c1", sub_id: "s1", kind: "subtitle", state,
  updated_at: 1000, ...(extra || {}),
});
const payloadOf = (tasks) => ({
  tasks,
  counts: {
    active: tasks.filter((task) => ["queued", "running", "pausing", "paused"].includes(String(task.state))).length,
    failed: tasks.filter((task) => task.state === "failed").length,
    completed: tasks.filter((task) => task.state === "completed").length,
  },
});
const setPayload = (tasks) => { tasksPayload = payloadOf(tasks); };
const cardsOf = () => [...byId["task-list"].querySelectorAll(".task")];
const groupHeaders = () => deepElements(byId["task-list"])
  .filter((node) => node.tagName === "H3").map((node) => String(node.textContent));

/* 扇出探针（两种模式共用）：tasks 订阅者与通配订阅者各一计数。 */
const probes = { tasks: 0, wildcard: 0 };
store.subscribe("tasks", () => { probes.tasks += 1; });
store.subscribe("*", () => { probes.wildcard += 1; });

/* ---- 行为钉模式 ---- */
if (!BENCH) {
  /* 钉 1（负例钉·基线）：任务在场，首次渲染全量建卡。 */
  setPayload([makeTask("t1", "queued")]);
  await drawer.loadTasks(store);
  assert.equal(cardsOf().length >= 1, true, "首次渲染任务卡在场");
  assert.equal(
    String(byId["task-counts"].textContent),
    "1 活动 · 0 失败 · 0 完成",
    "任务计数行首渲染",
  );

  /* 钉 2（空闲钉）：同内容 + 新 observed_at 反复刷新 → 零重建零扇出。
     GET /tasks 的 observed_at 每拍必变——指纹必须对它免疫。 */
  const beforeIdleCreates = createElementCalls;
  const probesBeforeIdle = { ...probes };
  for (let tick = 0; tick < 3; tick += 1) await drawer.loadTasks(store);
  assert.equal(createElementCalls - beforeIdleCreates, 0, "空闲三拍零 DOM 重建");
  assert.deepEqual(
    { tasks: probes.tasks - probesBeforeIdle.tasks, wildcard: probes.wildcard - probesBeforeIdle.wildcard },
    { tasks: 0, wildcard: 0 },
    "空闲三拍零扇出",
  );

  /* 钉 3（负例钉·真变化即时上屏）：queued→running 必须当拍重建、
     计数行与语义播报同步更新——去重绝不吞掉真变化。 */
  const beforeChangeCreates = createElementCalls;
  setPayload([makeTask("t1", "running")]);
  await drawer.loadTasks(store);
  assert.ok(createElementCalls > beforeChangeCreates, "真变化当拍重建");
  assert.equal(
    String(byId["task-counts"].textContent),
    "1 活动 · 0 失败 · 0 完成",
    "真变化计数行仍更新",
  );
  assert.ok(
    String(byId["task-live"].textContent).includes("字幕"),
    "语义播报随状态跃迁更新",
  );

  /* 钉 4（目录就绪钉）：tasks 不变、store.courses 换新 → 必须重渲染换真名
     （AS10 U2：课程信息加载中 → 真名，靠下一次渲染收口）。 */
  setPayload([makeTask("t1", "running")]);
  await drawer.loadTasks(store); /* 冻结当前指纹 */
  const beforeCoursesCreates = createElementCalls;
  store.set("courses", [{ course_id: "c1", title: "高等数学（上）", lectures: [{ sub_id: "s1", title: "第1讲" }] }]);
  await drawer.loadTasks(store);
  assert.ok(createElementCalls > beforeCoursesCreates, "courses 变化触发重渲染");
  assert.ok(
    groupHeaders().includes("高等数学（上）"),
    "课程组标题换真名",
  );

  /* 钉 5（auth 态钉）：占位文案由目录列表本体决定（REALFULL-1 判据：空列表=
     加载占位，与 auth.state 解耦——登录就绪但学生未进课程选择页时列表仍空，
     不得失实标「未关联课程」）；auth 变化仍必须触发重渲染（指纹含 auth.state）。
     非空列表找不到 course_id = 真未关联。 */
  store.set("courses", []);
  store.auth = { state: "ready" };
  await drawer.loadTasks(store);
  assert.ok(
    groupHeaders().some((text) => text === "课程信息加载中"),
    "目录缺失 + auth ready → 课程信息加载中占位（REALFULL-1：不得失实标未关联）",
  );
  store.set("courses", [{ course_id: "c-other", title: "半导体器件原理", lectures: [] }]);
  await drawer.loadTasks(store);
  assert.ok(
    groupHeaders().some((text) => text === "未关联课程"),
    "列表已装载仍找不到 → 未关联课程如实兜底",
  );
  store.set("courses", []);
  const beforeAuthCreates = createElementCalls;
  store.auth = { state: "checking" };
  await drawer.loadTasks(store);
  assert.ok(createElementCalls > beforeAuthCreates, "auth 变化触发重渲染");
  assert.ok(
    groupHeaders().some((text) => text === "课程信息加载中"),
    "auth 未就绪 → 课程信息加载中占位",
  );
  store.auth = { state: "ready" };

  /* 钉 6（store.set 同引用去重）：对象同引用重设不再扇出；新引用（即使
     内容相同）照常扇出——引用级语义，原语键不判重。 */
  const shared = ["c1"];
  let liveNotifies = 0;
  const unsubscribeLive = store.subscribe("liveActiveCourses", () => { liveNotifies += 1; });
  store.set("liveActiveCourses", shared);
  store.set("liveActiveCourses", shared);
  assert.equal(liveNotifies, 1, "同引用重设去重（1 次扇出）");
  store.set("liveActiveCourses", ["c1"]);
  assert.equal(liveNotifies, 2, "新引用照常扇出");
  unsubscribeLive();

  /* 钉 7（原语键不判重·W9 险点保护）：transcriptHasTiming 跨讲次 true→true
     必须仍逐次扇出（player-core.js:2559 每拍重绑 transcriptTimedSubId，
     值级判重会漏掉跨讲次重绑——这是不做值级去重的根因钉）。 */
  let timingNotifies = 0;
  const unsubscribeTiming = store.subscribe("transcriptHasTiming", () => { timingNotifies += 1; });
  store.set("transcriptHasTiming", true);
  store.set("activeLecture", { sub_id: "L2" });
  store.set("transcriptHasTiming", true);
  assert.equal(timingNotifies, 2, "原语键同值重设仍逐次扇出（跨讲次重绑语义）");
  unsubscribeTiming();

  console.log("frontend idle loop behavior passed");
  process.exit(0);
}

/* ---- 量化模式（不断言：改前/改后同一把尺） ---- */
const TICKS = 30;
setPayload([makeTask("t1", "running"), makeTask("t2", "completed"), makeTask("t3", "failed")]);
await drawer.loadTasks(store); /* 预热：首拍建卡不计入空闲段 */
const probesBefore = { ...probes };
const createsBefore = createElementCalls;
const getsBefore = tasksGetCalls;
const cpuBefore = process.cpuUsage();
const wallBefore = performance.now();

for (let tick = 0; tick < TICKS; tick += 1) await drawer.loadTasks(store);

const cpuDelta = process.cpuUsage(cpuBefore);
const metrics = {
  ticks: TICKS,
  /* 空闲段重建证据：createElement 调用次数（0=零重建；改前=每拍数十次）。 */
  createElementCallsIdle: createElementCalls - createsBefore,
  tasksFanOutIdle: probes.tasks - probesBefore.tasks,
  wildcardFanOutIdle: probes.wildcard - probesBefore.wildcard,
  tasksGetCallsIdle: tasksGetCalls - getsBefore,
  cpuUserMs: +(cpuDelta.user / 1000).toFixed(2),
  cpuSysMs: +(cpuDelta.system / 1000).toFixed(2),
  wallMs: +(performance.now() - wallBefore).toFixed(2),
};
console.log(`BENCH_JSON:${JSON.stringify(metrics)}`);
