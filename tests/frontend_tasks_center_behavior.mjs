import assert from "node:assert/strict";

/* 任务中心焕新行为测试（TASKS-CENTER-1，用户 09-22 走查拍板）。
   桩件法与 tests/frontend_budget_eta_behavior.mjs 同源：加载真实 store 与
   真实 tasks-drawer / ui 模块，FakeElement / fetch 路由桩；直接驱动
   renderTasks / renderTask 与导出的共享函数，不建新框架。
   U1：终态卡瘦身——六阶段表默认收起进「阶段详情」，卡面单行运行时长；
   进行中卡保留三态阶段板。 */

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

/* ---- 页面装配（renderTasks 需要的最小 id 集） ---- */

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
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => {
    createdSeq.value += 1;
    const node = new FakeElement(tag, `created-${createdSeq.value}`);
    registry.push(node);
    return node;
  },
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

const windowTarget = new EventTarget();
windowTarget.setTimeout = (fn) => 0;
windowTarget.clearTimeout = () => {};
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.EventSource = class {
  constructor() { this.closed = false; }
  close() { this.closed = true; }
};
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};

/* ---- 网络桩：GET /tasks 走可变载荷，POST 删除路由记录调用 ---- */
const postCalls = [];
let tasksPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route === "/api/v3/tasks") return okEnvelope(tasksPayload);
  if (method === "POST" && route === "/api/v3/tasks/delete") {
    const body = JSON.parse(options.body || "{}");
    postCalls.push({ route: "tasks/delete", body });
    return okEnvelope({ deleted: true, task_id: String(body.task_id || ""), state: "failed" });
  }
  if (method === "POST" && route === "/api/v3/tasks/delete-failed") {
    postCalls.push({ route: "tasks/delete-failed" });
    return okEnvelope({ deleted: 3 });
  }
  if (method === "POST" && route === "/api/v3/tasks/clear-stuck") {
    postCalls.push({ route: "tasks/clear-stuck" });
    return okEnvelope({ cleared: 2 });
  }
  if (method === "POST" && route === "/api/v3/tasks/actions") {
    const body = JSON.parse(options.body || "{}");
    postCalls.push({ route: "tasks/actions", body });
    return okEnvelope({
      operation: { operation_id: String(body.operation_id || ""), action: body.action, state: "accepted" },
      task: { task_id: String(body.task_id || ""), state: "completed" },
    });
  }
  throw new Error(`unexpected synthetic route: ${route}`);
};

/* 真实 store + 真实 tasks-drawer + ui */
const { store } = await import("../frontend/modules/store.js");
const drawer = await import("../frontend/modules/tasks-drawer.js");
const { formatRelativeTime } = await import("../frontend/modules/ui.js");

const renderTasksValue = (tasks) => {
  drawer.renderTasks(store, {
    tasks,
    counts: {
      active: tasks.filter((task) => !["completed", "failed", "canceled"].includes(String(task.state))).length,
      failed: tasks.filter((task) => task.state === "failed").length,
      completed: tasks.filter((task) => task.state === "completed").length,
    },
  });
};

const cardsOf = () => [...byId["task-list"].querySelectorAll(".task")];

/* ---- U1：终态卡瘦身 ---- */
{
  const base = {
    course_id: "c1", sub_id: "s1", kind: "subtitle",
    phases: { items: [
      { id: "input", label: "读取讲次", state: "completed" },
      { id: "ocr", label: "课件识别", state: "completed" },
      { id: "write", label: "生成字幕", state: "waiting" },
    ] },
  };
  renderTasksValue([
    { ...base, task_id: "f1", state: "failed", error_code: "runtime_failed", elapsed_seconds: 45, updated_at: 100, finished_at: 100 },
    { ...base, sub_id: "s2", task_id: "f2", state: "failed", error_code: "runtime_failed", created_at: 1000, finished_at: 1600, updated_at: 1600 },
    { ...base, task_id: "d1", state: "completed", elapsed_seconds: 900, updated_at: 200, finished_at: 200 },
    { ...base, task_id: "a1", state: "running", updated_at: 300 },
    { ...base, task_id: "x1", state: "canceled", updated_at: 400 },
    /* PB-1 O2：runner_seconds 上卡面（云端运行），elapsed=0 不再渲染「已运行 0 秒」 */
    { ...base, sub_id: "s3", task_id: "r1", state: "completed", runner_seconds: 754, updated_at: 500 },
    { ...base, sub_id: "s4", task_id: "z1", state: "completed", elapsed_seconds: 0, updated_at: 600 },
  ]);

  const f1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "f1"));
  assert.ok(f1, "失败卡在场");

  /* 六阶段表默认收起：阶段轨只存在于 details.task-phases 内，且默认折叠 */
  const phases = f1.querySelectorAll(".task-phases");
  assert.equal(phases.length, 1, "失败卡含一个阶段详情折叠块");
  assert.equal(phases[0].open, false, "阶段详情默认收起");
  assert.equal(phases[0].querySelector("summary")?.textContent, "阶段详情", "折叠摘要文案");
  assert.ok(phases[0].querySelector(".t-rail"), "阶段轨仍在折叠块内可展开核对");
  const directRails = f1.children.flatMap((node) => (String(node.className || "") === "t-rail" ? [node] : []));
  assert.equal(directRails.length, 0, "阶段轨不再直出卡面");

  /* 卡面单行运行时长：elapsed_seconds 优先（45s 走秒档） */
  const f1Evidence = [...f1.querySelectorAll(".t-evidence")].map((node) => String(node.textContent));
  assert.deepEqual(f1Evidence, ["已运行 45 秒"], "失败卡单行：已运行 45 秒");
  /* created_at → finished_at 回退：600s → 10 分钟 */
  const f2 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "f2"));
  const f2Evidence = [...f2.querySelectorAll(".t-evidence")].map((node) => String(node.textContent));
  assert.deepEqual(f2Evidence, ["已运行 10 分钟"], "无 elapsed 时回退 finished-created");
  /* 技术详情维持折叠在场，错误码可见 */
  assert.ok(f1.querySelector(".task-technical"), "技术详情维持折叠在场");
  /* 失败文案在场 */
  assert.ok(f1.querySelector(".t-failure"), "失败卡人话文案在场");

  /* 完成卡同样瘦身 */
  const d1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "d1"));
  assert.equal(d1.querySelectorAll(".task-phases")[0]?.open, false, "完成卡阶段详情默认收起");
  assert.deepEqual(
    [...d1.querySelectorAll(".t-evidence")].map((node) => String(node.textContent)),
    ["已运行 15 分钟"],
    "完成卡单行：已运行 15 分钟",
  );

  /* PB-1 O2：runner_seconds>0 时上卡面（人话时长，云端运行），误导性 0 秒退场 */
  const r1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "r1"));
  assert.deepEqual(
    [...(r1?.querySelectorAll(".t-evidence") || [])].map((node) => String(node.textContent)),
    ["云端运行 12 分钟"],
    "runner_seconds 上卡面：云端运行 12 分钟",
  );
  const z1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "z1"));
  assert.deepEqual(
    [...(z1?.querySelectorAll(".t-evidence") || [])].map((node) => String(node.textContent)),
    [],
    "elapsed=0 且无其他时长来源：不渲染 0 秒误导行",
  );

  /* 进行中卡保留三态阶段板：阶段轨直出、无折叠块、无运行时长行 */
  const a1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "a1"));
  assert.equal(a1.querySelectorAll(".task-phases").length, 0, "进行中卡无阶段折叠块");
  assert.ok(a1.querySelector(".t-rail"), "进行中卡阶段轨直出");
  assert.equal(a1.querySelectorAll(".t-evidence").length > 0, true, "进行中卡证据行保留");

  /* 取消卡：无阶段轨、无折叠块（瘦身后与旧契约一致地安静） */
  const x1 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "x1"));
  assert.equal(x1.querySelectorAll(".task-phases").length, 0, "取消卡无阶段折叠块");
  assert.equal(x1.querySelectorAll(".t-rail").length, 0, "取消卡无阶段轨");

  /* 无任何时间证据的终态卡：诚实省略时长行 */
  renderTasksValue([
    { ...base, task_id: "f3", state: "failed", error_code: "runtime_failed", updated_at: 500 },
  ]);
  const f3 = cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === "f3"));
  assert.equal(f3.querySelectorAll(".t-evidence").length, 0, "无时间证据不编造时长行");
  console.log("ok: U1 终态卡瘦身（阶段详情默认收起 + 单行运行时长 + 进行中三态阶段板不变）");
}

/* ---- U2：formatRelativeTime 共享通道（ui.js） ---- */
{
  /* now = 2026-09-22 15:00 本地时间 */
  const now = new Date(2026, 8, 22, 15, 0, 0).getTime() / 1000;
  const at = (month, day, hour, minute, year = 2026) => new Date(year, month - 1, day, hour, minute, 0).getTime() / 1000;
  assert.equal(formatRelativeTime(at(9, 22, 2, 24), now), "今晨 02:24", "今日凌晨");
  assert.equal(formatRelativeTime(at(9, 22, 9, 5), now), "上午 09:05", "今日上午");
  assert.equal(formatRelativeTime(at(9, 22, 14, 30), now), "下午 14:30", "今日下午");
  assert.equal(formatRelativeTime(at(9, 22, 21, 5), now), "今晚 21:05", "今日晚间");
  assert.equal(formatRelativeTime(at(9, 18, 8, 0), now), "9/18", "跨天 → M/D");
  assert.equal(formatRelativeTime(at(1, 3, 8, 0), now), "1/3", "同年早期 → M/D");
  assert.equal(formatRelativeTime(at(12, 30, 8, 0, 2025), now), "2025/12/30", "跨年 → YYYY/M/D");
  assert.equal(formatRelativeTime(0, now), "", "无时间戳诚实空串");
  assert.equal(formatRelativeTime(undefined, now), "", "缺时间戳诚实空串");
  console.log("ok: U2 formatRelativeTime 人话时间闭集");
}

/* ---- U2：卡片主标识重排——课程名视觉锚 / 讲次名无日期 / 相对时间槽 ---- */
{
  store.courses = [{
    course_id: "c1", title: "高等数学",
    lectures: [{ sub_id: "s1", sub_title: "第一章 极限", date: "9/18" }],
  }];
  const todayAfternoon = new Date(new Date().setHours(14, 30, 0, 0)).getTime() / 1000;
  /* 跨天锚点：4 天前 08:00（同年内稳定），标签按日历现算防跨年漂移 */
  const crossDate = new Date(new Date().setDate(new Date().getDate() - 4));
  crossDate.setHours(8, 0, 0, 0);
  const crossSeconds = crossDate.getTime() / 1000;
  const crossLabel = `${crossDate.getMonth() + 1}/${crossDate.getDate()}`;
  renderTasksValue([
    {
      task_id: "u2a", course_id: "c1", sub_id: "s1", kind: "subtitle",
      state: "failed", error_code: "runtime_failed",
      updated_at: todayAfternoon, finished_at: todayAfternoon, elapsed_seconds: 120,
    },
    {
      /* quiz 不在学习材料合并闭集（subtitle+summary）内，保持独立失败卡 */
      task_id: "u2b", course_id: "c1", sub_id: "s1", kind: "quiz",
      state: "failed", error_code: "runtime_failed",
      updated_at: crossSeconds, finished_at: crossSeconds,
    },
    { task_id: "u2c", course_id: "", sub_id: "", kind: "subtitle", state: "failed", error_code: "runtime_failed", updated_at: todayAfternoon },
  ]);
  const findCard = (taskKey) => cardsOf().find((card) => deepElements(card).some((node) => node.dataset?.taskKey === taskKey));

  /* 首行=课程名视觉锚 */
  const cardA = findCard("u2a");
  assert.equal(cardA.querySelector(".t-name")?.textContent, "高等数学", "首行=课程名视觉锚");
  /* 次行=类型 · 讲次名；讲次课表日期不再出现在次行 */
  assert.equal(cardA.querySelector(".t-context")?.textContent, "字幕任务 · 第一章 极限", "次行=类型 · 讲次名");
  /* 时间槽=人话相对时间（今日下午） */
  assert.equal(cardA.querySelector(".t-meta")?.textContent, "下午 14:30", "时间槽=今日下午 14:30");

  /* 跨天卡：日期恰一次——只出现在相对时间槽，讲次行不再带日期 */
  const cardB = findCard("u2b");
  assert.equal(cardB.querySelector(".t-meta")?.textContent, crossLabel, "跨天相对时间=M/D");
  const dateTokens = deepElements(cardB).filter((node) => node.children.length === 0)
    .map((node) => String(node.textContent || ""))
    .filter((text) => text.includes(crossLabel));
  assert.equal(dateTokens.length, 1, "日期恰一次：只在时间槽出现");

  /* 无课程任务回退类型为主行，次行不再重复类型 */
  const cardC = findCard("u2c");
  assert.equal(cardC.querySelector(".t-name")?.textContent, "字幕任务", "无课程回退类型为主行");
  assert.equal(cardC.querySelector(".t-context"), null, "次行不重复类型");

  /* U2 死因分组映射：remote_failed 如实指向云端处理，禁「学校登录服务」猜测兜底 */
  assert.equal(
    cardA.querySelector(".t-failure")?.textContent.includes("学校登录服务"),
    false,
    "不再出现学校登录服务猜测兜底",
  );
  renderTasksValue([
    { task_id: "u2d", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "failed", error_code: "remote_failed", updated_at: todayAfternoon },
  ]);
  const cardD = findCard("u2d");
  const remoteCopy = String(cardD.querySelector(".t-failure")?.textContent || "");
  assert.ok(remoteCopy.includes("云端处理"), "remote_failed 如实指向云端处理");
  assert.ok(remoteCopy.includes("技术详情"), "remote_failed 指引诊断详情");

  /* 未知码如实「未能确认」+ 技术详情保留原始码 */
  renderTasksValue([
    { task_id: "u2e", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "failed", error_code: "brand_new_future_code", updated_at: todayAfternoon },
  ]);
  const cardE = findCard("u2e");
  assert.ok(String(cardE.querySelector(".t-failure")?.textContent || "").includes("未能确认"), "未知码如实未能确认");
  assert.ok(
    deepElements(cardE.querySelector(".task-technical")).some((node) => String(node.textContent).includes("brand_new_future_code")),
    "技术详情保留原始闭集外代码",
  );

  /* 聚合卡与学习材料父卡共用新表头路径：×N 徽标与课程锚共存 */
  renderTasksValue([
    { task_id: "u2f1", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "failed", error_code: "runtime_failed", updated_at: todayAfternoon - 60 },
    { task_id: "u2f2", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "failed", error_code: "runtime_failed", updated_at: todayAfternoon },
  ]);
  const aggregate = cardsOf().find((card) => String(card.className || "").includes("task-aggregate"));
  assert.ok(aggregate, "同类失败聚合卡在场");
  assert.ok(String(aggregate.querySelector(".t-name")?.textContent || "").includes("高等数学"), "聚合卡首行同为课程名锚");
  const aggregateText = deepElements(aggregate).map((node) => String(node.textContent || "")).join("\n");
  assert.ok(aggregateText.includes("×2 条同类记录"), "×N 徽标共存");

  store.courses = [];
  console.log("ok: U2 卡片主标识重排（课程锚/日期恰一次/相对时间槽/死因分组映射）");
}

/* ---- U3：失败堆叠按课程维度分组，与同类聚合（×N）共存 ---- */
{
  store.courses = [
    { course_id: "c1", title: "高等数学", lectures: [] },
    { course_id: "c2", title: "大学物理", lectures: [] },
  ];
  const failedTask = (task_id, course_id, sub_id, updated_at) => ({
    task_id, course_id, sub_id, kind: "subtitle", state: "failed",
    error_code: "runtime_failed", updated_at,
  });
  renderTasksValue([
    failedTask("g1", "c1", "s1", 100),
    failedTask("g2", "c1", "s2", 200),
    failedTask("g3", "c2", "s9", 300),
    failedTask("g4", "", "sx", 400),
    failedTask("g5", "c1", "s3", 500),
    failedTask("g6", "c1", "s4", 600),
    failedTask("g7", "c1", "s5", 700),
    failedTask("g8", "c1", "s6", 800),
    failedTask("g9", "c1", "s7", 900),
    /* 同课程同讲次同类的两条 → ×N 聚合（与课程分组共存） */
    failedTask("h1", "c2", "s8", 950),
    failedTask("h2", "c2", "s8", 1000),
  ]);
  const failedTier = [...byId["task-list"].children]
    .find((node) => String(node.className || "").includes("task-tier-failed"));
  assert.ok(failedTier, "失败层在场");
  /* U4 起层标题住进 .task-tier-header 行（与清理动作同行） */
  const tierHeader = failedTier.querySelectorAll(".task-tier-header")[0];
  const tierTitle = tierHeader?.children.find((node) => node.tagName === "H3");
  assert.equal(tierTitle?.textContent, "需要处理 · 11 个失败任务", "层标题携带失败总数");
  const groups = [...failedTier.querySelectorAll(".task-group")];
  assert.equal(groups.length, 3, "按课程维度分组：两门课 + 不挂课程的任务");
  const groupTitle = (group) => String(group.querySelector("h3")?.textContent || "");
  /* 组序 = 组内最近失败优先：c2（1000）→ c1（900）→ 不挂课程（400）。
     OBS-2（化身走查 20261008）：无 course_id 任务种中性「不挂课程的任务」，
     「未关联课程」只留给有 course_id 找不到真身的真异常。 */
  assert.deepEqual(groups.map(groupTitle), ["大学物理", "高等数学", "不挂课程的任务"], "组序=最近失败课程优先，不挂课程任务中性兜底");
  const groupHasTask = (group, taskKey) => deepElements(group).some((node) => node.dataset?.taskKey === taskKey);
  assert.ok(groupHasTask(groups[1], "g1") && groupHasTask(groups[1], "g9"), "c1 组收 7 条失败卡");
  assert.ok(groupHasTask(groups[0], "h2"), "c2 组含最新聚合卡");
  /* 溢出折叠：c1 组 7 条 → 5 可见 + 「还有 2 个失败任务」 */
  const overflow = groups[1].querySelector(".task-overflow");
  assert.equal(overflow?.querySelector("summary")?.textContent, "还有 2 个失败任务", "组内溢出折叠入口");
  /* ×N 聚合与课程分组共存：c2 组内 h1+h2 聚合 ×2 */
  const aggregates = groups[0].querySelectorAll(".task-aggregate");
  assert.equal(aggregates.length, 1, "同课程同讲次同类聚合为一张");
  const aggText = deepElements(aggregates[0]).map((node) => String(node.textContent || "")).join("\n");
  assert.ok(aggText.includes("×2 条同类记录"), "×N 徽标在课程组内");
  /* U3 视觉分组 CSS 契约：失败组课程标题升主字色 + 左竖轨 */
  const css = await (await import("node:fs/promises")).readFile(
    new URL("../frontend/styles/components.css", import.meta.url), "utf8",
  );
  assert.match(css, /\.task-tier-failed \.task-group > h3\s*\{[^}]*var\(--ink\)/, "失败组课程标题升主字色");
  assert.match(css, /\.task-tier-failed \.task-group\s*\{[^}]*border-left:\s*2px solid var\(--line\)/, "失败组左竖轨收束");
  store.courses = [];
  console.log("ok: U3 失败按课程分组（组序/未关联兜底/溢出折叠/×N 共存/视觉契约）");
}

/* ---- U4：任务记录删除——终态卡单条删除 + 一键清理全部失败 ---- */
{
  const settle = async () => {
    for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const base = { course_id: "c1", sub_id: "s1", kind: "subtitle", error_code: "runtime_failed" };
  const render = (tasks) => {
    tasksPayload = {
      tasks,
      counts: {
        active: tasks.filter((task) => ["queued", "running"].includes(String(task.state))).length,
        failed: tasks.filter((task) => task.state === "failed").length,
        completed: tasks.filter((task) => task.state === "completed").length,
      },
    };
    renderTasksValue(tasks);
  };
  const clickButtonWithLabel = (label) => deepElements(byId["task-list"])
    .filter((node) => node.tagName === "BUTTON" && String(node.textContent || "") === label);

  /* 终态卡带「删除记录」，进行中卡不带 */
  render([
    { ...base, task_id: "d1", state: "failed", updated_at: 100 },
    { ...base, task_id: "d2", state: "running", updated_at: 200 },
  ]);
  const deleteButtons = clickButtonWithLabel("删除记录");
  assert.equal(deleteButtons.length, 1, "仅终态卡携带删除记录按钮");
  assert.equal(deleteButtons[0].dataset.taskKey, "d1", "删除按钮携带自身任务键");

  /* 两击臂式：首击换确认文案，再击执行 */
  const toastBefore = byId["toast-region"].children.length;
  deleteButtons[0].click();
  assert.equal(deleteButtons[0].textContent, "再点一次，删除这条记录", "首击只换确认文案");
  assert.equal(postCalls.length, 0, "首击零请求");
  deleteButtons[0].click();
  await settle();
  assert.equal(postCalls.length, 1, "再击执行删除请求");
  assert.deepEqual(postCalls[0], { route: "tasks/delete", body: { task_id: "d1" } }, "请求携带任务键");
  assert.equal(byId["toast-region"].children.length, toastBefore + 1, "删除后人话 toast");
  const deleteToast = String(byId["toast-region"].children[byId["toast-region"].children.length - 1]?.textContent || "");
  assert.ok(deleteToast.includes("已生成的字幕和文件不受影响"), "toast 点明产物不受影响边界");

  /* 失败层头部「清理全部失败记录」：两击臂式 + 确认文案点明只删记录 */
  render([{ ...base, task_id: "d3", state: "failed", updated_at: 300 }]);
  const clearButtons = clickButtonWithLabel("清理全部失败记录");
  assert.equal(clearButtons.length, 1, "失败层头部带清理动作");
  clearButtons[0].click();
  assert.equal(
    clearButtons[0].textContent,
    "确认清理：只删任务记录，已生成的字幕/PDF 不受影响",
    "清理确认文案=用户拍板原文",
  );
  assert.equal(postCalls.length, 1, "确认前零请求");
  clearButtons[0].click();
  await settle();
  assert.equal(postCalls.length, 2, "再击执行清理请求");
  assert.deepEqual(postCalls[1], { route: "tasks/delete-failed" }, "清理走闭集路由");
  const clearToast = String(byId["toast-region"].children[byId["toast-region"].children.length - 1]?.textContent || "");
  assert.equal(clearToast, "已清理 3 条失败记录", "清理 toast 带删除计数");

  /* 聚合头卡不带删除（历次明细内逐条删），运行卡亦不带 */
  render([
    { ...base, task_id: "d4", state: "failed", updated_at: 400 },
    { ...base, task_id: "d5", state: "failed", updated_at: 500 },
  ]);
  assert.equal(clickButtonWithLabel("删除记录").length, 2, "聚合头卡无删除按钮，两条历次各带一个");

  /* SWEEPFIX-1 T2（化身走查 SWEEP1-T2）：确认态跨重渲存活——首击臂上后，
     SSE/轮询带来数据变化整体重建列表，新按钮继承确认文案与剩余臂态窗口；
     学生按阅读节奏（≈1.5s）回头点的第二击仍执行删除。修前红：重建后按钮
     回到「删除记录」，第二击无的放矢（postCalls 不增）。 */
  render([
    { ...base, task_id: "t2a", state: "failed", updated_at: 600 },
    { ...base, sub_id: "s2", task_id: "t2b", state: "failed", updated_at: 605 },
  ]);
  const deleteButtonByKey = (key) => deepElements(byId["task-list"])
    .filter((node) => node.tagName === "BUTTON" && String(node.dataset?.taskKey || "") === key)[0];
  const armedFirst = deleteButtonByKey("t2a");
  armedFirst.click();
  assert.equal(armedFirst.textContent, "再点一次，删除这条记录", "T2 首击臂上");
  assert.equal(postCalls.length, 2, "T2 臂上零请求");
  render([
    { ...base, task_id: "t2a", state: "failed", updated_at: 620 },
    { ...base, sub_id: "s2", task_id: "t2b", state: "failed", updated_at: 615 },
  ]);
  const armedAfter = deleteButtonByKey("t2a");
  assert.notEqual(armedAfter, armedFirst, "T2 重渲后是全新按钮节点");
  assert.equal(armedAfter.textContent, "再点一次，删除这条记录", "T2 确认态跨重渲存活");
  assert.equal(armedAfter.dataset.confirming, "true", "T2 臂态标记随重建恢复");
  armedAfter.click();
  await settle();
  assert.equal(postCalls.length, 3, "T2 阅读节奏第二击执行删除");
  assert.deepEqual(postCalls[2], { route: "tasks/delete", body: { task_id: "t2a" } }, "T2 继承臂态的删除携带任务键");
  render([{ ...base, task_id: "t2b", state: "failed", updated_at: 630 }]);
  const afterExecute = clickButtonWithLabel("删除记录");
  assert.equal(afterExecute.length === 1 && afterExecute[0].textContent, "删除记录", "T2 执行后臂态解除，重建回默认文案");
  console.log("ok: U4 记录删除（终态单删两击臂/清全部失败确认文案/聚合头不带删/人话 toast）");
  console.log("ok: SWEEPFIX-1 T2 确认态跨重渲存活（臂上零请求/重建继承/第二击执行/执行后解除）");
}

/* ---- SWEEPFIX-1 T4（化身走查 SWEEP1-T4）保护钉：学生默认视图零英文噪音。
   原始遥测键值（state:/error_code:/estimate_basis:/confidence:）只允许存在于
   默认折叠的「技术详情」诊断面内（frontend_budget_eta_behavior 契约钉保留
   闭集代码于该面）；任何此类文本若出现，必须被一个未展开的 details 覆盖。 ---- */
{
  const base = {
    course_id: "c1", sub_id: "s1", kind: "subtitle",
    error_code: "worker_tree_drifted", estimate_basis: "history_median",
    progress_confidence: "high", updated_at: 700,
  };
  renderTasksValue([
    { ...base, task_id: "t4a", state: "failed" },
    { ...base, task_id: "t4b", state: "completed" },
    { ...base, task_id: "t4c", state: "running" },
  ]);
  const technicalPanels = [...byId["task-list"].querySelectorAll("details")]
    .filter((node) => String(node.className || "").split(/\s+/).includes("task-technical"));
  assert.ok(technicalPanels.length >= 3, "三态卡各带技术详情折叠面");
  for (const panel of technicalPanels) {
    assert.equal(panel.open, false, "技术详情默认折叠（默认学生视图零英文噪音，T4）");
  }
  const rawNodes = deepElements(byId["task-list"])
    .filter((node) => /state: |error_code: |estimate_basis: |confidence: /.test(String(node.textContent || "")));
  assert.ok(rawNodes.length >= 3, "诊断键值在场（契约保留于诊断面）");
  for (const node of rawNodes) {
    let cursor = node;
    let guarded = false;
    while (cursor) {
      if (cursor.tagName === "DETAILS" && cursor.open === false) { guarded = true; break; }
      cursor = cursor.parent;
    }
    assert.equal(guarded, true, "原始遥测键值只出现在默认折叠的技术详情内（T4 保护钉）");
  }
  const cardTexts = deepElements(byId["task-list"]).map((node) => String(node.textContent || ""));
  assert.ok(cardTexts.includes("失败") && cardTexts.includes("已完成"), "卡面状态徽标为人话（T4）");
  console.log("ok: SWEEPFIX-1 T4 学生默认视图零英文噪音（键值仅存于折叠技术详情）");
}

/* ---- AS2：显式「导入远端结果」——失败/暂停卡可见、点击才派发、渲染面零请求 ---- */
{
  const settle = async () => {
    for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const base = { course_id: "c1", sub_id: "s1", kind: "subtitle", error_code: "remote_failed" };
  const render = (tasks) => {
    tasksPayload = {
      tasks,
      counts: {
        active: tasks.filter((task) => ["queued", "running", "paused"].includes(String(task.state))).length,
        failed: tasks.filter((task) => task.state === "failed").length,
        completed: tasks.filter((task) => task.state === "completed").length,
      },
    };
    renderTasksValue(tasks);
  };
  const buttonsWithLabel = (label) => deepElements(byId["task-list"])
    .filter((node) => node.tagName === "BUTTON" && String(node.textContent || "") === label);
  postCalls.length = 0;

  render([
    { ...base, task_id: "i1", state: "failed", remote_import: { possible: true }, updated_at: 100 },
    { ...base, task_id: "i2", state: "failed", updated_at: 200 },
    { ...base, task_id: "i3", state: "paused", remote_import: { possible: true }, updated_at: 300 },
    { ...base, task_id: "i4", state: "queued", remote_import: { possible: true }, updated_at: 400 },
    { ...base, task_id: "i5", state: "completed", remote_import: { possible: true }, updated_at: 500 },
  ]);
  /* 渲染面零派发：按钮在场，但渲染本身不发任何 tasks/actions 请求 */
  assert.equal(
    postCalls.filter((call) => call.route === "tasks/actions").length, 0,
    "渲染面零导入请求（读面不派发）",
  );
  const importButtons = buttonsWithLabel("导入远端结果");
  assert.deepEqual(
    [...importButtons.map((node) => node.dataset.taskKey)].sort(), ["i1", "i3"],
    "按钮只在失败/暂停且 possible 卡出现；无标记、排队、完成卡一律没有",
  );

  /* 点击派发显式动作恰一次，toast 人话，随后刷新任务列表 */
  importButtons.find((node) => node.dataset.taskKey === "i1").click();
  await settle();
  const importCalls = postCalls.filter((call) => call.route === "tasks/actions");
  assert.equal(importCalls.length, 1, "点击恰一次显式导入请求");
  assert.equal(importCalls[0].body.action, "import_result", "动作名走闭集");
  assert.equal(importCalls[0].body.task_id, "i1", "请求携带任务键");
  assert.ok(String(importCalls[0].body.operation_id || ""), "携带 operation_id 幂等键");
  const importToast = String(byId["toast-region"].children[byId["toast-region"].children.length - 1]?.textContent || "");
  assert.ok(importToast.includes("已导入"), "成功 toast 人话且点明结果已进课程");
  console.log("ok: AS2 显式导入按钮（失败/暂停可见/点击才派发/渲染面零请求/toast 人话）");
}

/* ---- F10（化身走查 20261008）：授权缺失族失败的重试预拦截——失败卡因
   「复旦会话缺失」失败时，会话未就绪点「重试」不再把任务放回进行中空挂
   （与「生成字幕」401 预拦截同一语义），先给人话引导重新认证；会话就绪
   照常放行；非授权失败码不受闸。 ---- */
{
  const settle = async () => {
    for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const retryButtons = () => deepElements(byId["task-list"])
    .filter((node) => node.tagName === "BUTTON" && String(node.textContent || "") === "重试");
  const lastToast = () => String(byId["toast-region"].children[byId["toast-region"].children.length - 1]?.textContent || "");
  const authFailBase = { course_id: "c1", sub_id: "s1", kind: "subtitle" };
  postCalls.length = 0;

  store.auth = { state: "degraded" };
  renderTasksValue([
    { ...authFailBase, task_id: "af1", state: "failed", error_code: "fudan_login_required", actions: ["retry"], updated_at: 100 },
    { ...authFailBase, sub_id: "s2", task_id: "af2", state: "failed", error_code: "authorization_required", actions: ["retry"], updated_at: 110 },
  ]);
  const blockedButtons = retryButtons();
  assert.equal(blockedButtons.length, 2, "两张授权缺失失败卡各带重试钮");
  blockedButtons[0].click();
  blockedButtons[1].click();
  await settle();
  assert.equal(
    postCalls.filter((call) => call.route === "tasks/actions").length, 0,
    "会话未就绪：授权缺失族重试零请求（不把任务放回进行中空挂）",
  );
  assert.ok(lastToast().includes("重新登录"), "拦截给人话引导重新登录");

  store.auth = { state: "checking" };
  renderTasksValue([
    { ...authFailBase, task_id: "af1", state: "failed", error_code: "fudan_login_required", actions: ["retry"], updated_at: 100 },
  ]);
  retryButtons()[0].click();
  await settle();
  assert.equal(
    postCalls.filter((call) => call.route === "tasks/actions").length, 0,
    "登录中同样预拦截",
  );
  assert.ok(lastToast().includes("登录中"), "登录中给等待人话");

  store.auth = { state: "ready" };
  renderTasksValue([
    { ...authFailBase, task_id: "af1", state: "failed", error_code: "fudan_login_required", actions: ["retry"], updated_at: 100 },
  ]);
  retryButtons()[0].click();
  await settle();
  let retryCalls = postCalls.filter((call) => call.route === "tasks/actions");
  assert.equal(retryCalls.length, 1, "会话就绪照常放行重试");
  assert.equal(retryCalls[0].body.action, "retry", "放行动作走闭集 retry");
  assert.equal(retryCalls[0].body.task_id, "af1", "放行携带任务键");

  store.auth = { state: "degraded" };
  renderTasksValue([
    { ...authFailBase, task_id: "af3", state: "failed", error_code: "remote_failed", actions: ["retry"], updated_at: 120 },
  ]);
  retryButtons()[0].click();
  await settle();
  retryCalls = postCalls.filter((call) => call.route === "tasks/actions");
  assert.equal(retryCalls.length, 2, "非授权失败码不受闸（会话未就绪也放行）");
  assert.equal(retryCalls[1].body.task_id, "af3", "放行对应非授权失败卡");

  store.auth = null;
  console.log("ok: F10 授权缺失族重试预拦截（未就绪拦截+就绪放行+非授权码不受闸）");
}

/* ---- OBS-2（化身走查 20261008）：不挂课程任务的中性分组标题——
   course_id 缺失（学籍/自动化类任务种）≠「未关联课程」；该失实标签只留给
   有 course_id 却找不到真身的真异常。 ---- */
{
  store.courses = [
    { course_id: "c1", title: "计算机网络 12", lectures: [] },
    { course_id: "c2", title: "概率论 11", lectures: [] },
  ];
  renderTasksValue([
    { task_id: "nc1", sub_id: "s1", kind: "summary", state: "failed", error_code: "runtime_failed", actions: ["retry"], updated_at: 100 },
    { task_id: "nc2", course_id: "c-ghost", sub_id: "s1", kind: "quiz", state: "failed", error_code: "runtime_failed", actions: ["retry"], updated_at: 110 },
  ]);
  const groupTitles = [...byId["task-list"].querySelectorAll(".task-group")]
    .map((group) => String(group.querySelector("h3")?.textContent || ""));
  assert.ok(groupTitles.includes("不挂课程的任务"), "无 course_id 任务=中性「不挂课程的任务」分组");
  assert.ok(groupTitles.includes("未关联课程"), "有 course_id 找不到=保留真异常「未关联课程」");
  const nc1Card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "nc1"));
  assert.ok(!String(nc1Card.textContent || "").includes("未关联课程"), "不挂课程任务卡自身零「未关联」失实");
  store.courses = [];
  console.log("ok: OBS-2 不挂课程任务中性分组（未关联只留真异常）");
}

/* ---- AS6 消耗透镜：终态卡 token 消耗行（仅 tokens>0 渲染） ---- */
{
  const base = {
    course_id: "c1", sub_id: "s1", kind: "summary",
    phases: { items: [] },
  };
  renderTasksValue([
    { ...base, task_id: "u1", state: "completed", elapsed_seconds: 726, deepseek_tokens: 12345, updated_at: 100, finished_at: 100 },
    { ...base, sub_id: "s2", task_id: "u2", state: "completed", elapsed_seconds: 60, deepseek_tokens: 0, updated_at: 100, finished_at: 100 },
    { ...base, sub_id: "s3", task_id: "u3", state: "completed", elapsed_seconds: 60, updated_at: 100, finished_at: 100 },
  ]);
  const evidenceOf = (key) => {
    const card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === key));
    return [...card.querySelectorAll(".t-evidence")].map((node) => String(node.textContent));
  };
  assert.deepEqual(evidenceOf("u1"), ["已运行 12 分钟 · ≈1.2 万 tokens"], "消耗紧跟运行时长同行（人话单位）");
  assert.deepEqual(evidenceOf("u2"), ["已运行 1 分钟"], "tokens=0（无 LLM 参与）不渲染");
  assert.deepEqual(evidenceOf("u3"), ["已运行 1 分钟"], "历史任务无该字段不渲染");
  console.log("ok: AS6 终态卡 token 消耗行（仅 tokens>0；0/无字段诚实不渲染）");
}

/* ---- AS10 U2：课程占位——目录未装载给稳定「课程信息加载中」，装载后真名/未关联 ---- */
{
  store.auth = { state: "checking" };
  store.courses = [];
  renderTasksValue([
    { task_id: "p1", course_id: "c-unknown", sub_id: "s1", kind: "summary", state: "queued", updated_at: 100 },
  ]);
  let card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "p1"));
  let name = card.querySelector(".t-name")?.textContent;
  assert.equal(name, "课程信息加载中", "目录未就绪=稳定占位，不闪「未关联课程」");

  /* REALFULL-1 真测修正：登录就绪≠课程列表已装载（学生未进课程选择页时
     store.courses 仍为空）——空列表一律加载占位，不再失实标「未关联课程」 */
  store.auth = { state: "ready" };
  renderTasksValue([
    { task_id: "p1", course_id: "c-unknown", sub_id: "s1", kind: "summary", state: "queued", updated_at: 100 },
  ]);
  card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "p1"));
  name = card.querySelector(".t-name")?.textContent;
  assert.equal(name, "课程信息加载中", "登录就绪但列表未装载=仍是加载占位（真测：原判据把已关联课程失实标未关联）");

  store.courses = [{ course_id: "c-other", title: "半导体器件原理", lectures: [] }];
  renderTasksValue([
    { task_id: "p1", course_id: "c-unknown", sub_id: "s1", kind: "summary", state: "queued", updated_at: 100 },
  ]);
  card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "p1"));
  name = card.querySelector(".t-name")?.textContent;
  assert.equal(name, "未关联课程", "列表已装载仍找不到=如实未关联");

  store.courses = [{ course_id: "c-unknown", title: "数字集成电路", lectures: [] }];
  renderTasksValue([
    { task_id: "p1", course_id: "c-unknown", sub_id: "s1", kind: "summary", state: "queued", updated_at: 100 },
  ]);
  card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "p1"));
  name = card.querySelector(".t-name")?.textContent;
  assert.equal(name, "数字集成电路", "就绪且命中=真名，一次到位无跳变");

  store.courses = [];
  store.auth = null;
  console.log("ok: AS10 课程占位三态（加载中/未关联/真名，无闪烁跳变；REALFULL-1 空列表判据修正）");
}

/* ---- P11：质量抽检任务卡（标签闭集 + 无动作按钮 + 失败人话）---- */
{
  store.auth = { state: "ready" };
  store.courses = [{
    course_id: "cq", title: "概率论",
    lectures: [{ sub_id: "sq", sub_title: "第五章 大数定律", date: "10/1" }],
  }];
  renderTasksValue([
    { task_id: "pq1", course_id: "cq", sub_id: "sq", kind: "quality_judge", state: "running", updated_at: 100 },
    {
      task_id: "pq2", course_id: "cq", sub_id: "sq", kind: "quality_judge", state: "failed",
      error_code: "worker_kind_unsupported", updated_at: 100, finished_at: 100,
    },
  ]);
  const cardRunning = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "pq1"));
  assert.ok(cardRunning, "质检任务卡在列");
  const runningTexts = deepElements(cardRunning).map((node) => String(node.textContent || ""));
  assert.ok(runningTexts.some((text) => text.includes("质量抽检")), "quality_judge 标签=质量抽检");
  assert.equal(
    deepElements(cardRunning).filter((node) => node.tagName === "BUTTON").length, 0,
    "质检任务卡无动作按钮（不进 per-kind 动作集，既有降级零代码）",
  );
  const cardFailed = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "pq2"));
  const failedTexts = deepElements(cardFailed).map((node) => String(node.textContent || ""));
  assert.ok(
    failedTexts.some((text) => text.includes("云端组件版本较旧")),
    "worker_kind_unsupported 人话指路上卡面",
  );
  store.courses = [];
  store.auth = null;
  console.log("ok: P11 质量抽检任务卡（标签闭集/无动作按钮/失败人话）");
}

/* ---- DEAD-TASK-PURGE B：清除卡住的任务（进行中层头部，仅卡住时可见；
   两击臂式确认=任务书人话原文；幂等闭集路由；PF1 跳过路径对钟翻可见性）---- */
{
  const settle = async () => {
    for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  const base = { course_id: "c1", sub_id: "s1", kind: "subtitle" };
  const nowSeconds = Date.now() / 1000;
  const render = (tasks) => {
    tasksPayload = {
      tasks,
      counts: {
        active: tasks.filter((task) => ["queued", "running", "pausing", "paused"].includes(String(task.state))).length,
        failed: tasks.filter((task) => task.state === "failed").length,
        completed: tasks.filter((task) => task.state === "completed").length,
      },
    };
    renderTasksValue(tasks);
  };
  const clickButtonWithLabel = (label) => deepElements(byId["task-list"])
    .filter((node) => node.tagName === "BUTTON" && String(node.textContent || "") === label);
  postCalls.length = 0;

  render([
    { ...base, task_id: "k1", state: "queued", updated_at: nowSeconds - 3600 },
    { ...base, sub_id: "s2", task_id: "k2", state: "running", display_state: "remote_running", updated_at: nowSeconds - 3600 },
    { ...base, sub_id: "s3", task_id: "k3", state: "running", updated_at: nowSeconds - 60 },
    { ...base, sub_id: "s4", task_id: "k4", state: "paused", updated_at: nowSeconds - 7200 },
    { ...base, sub_id: "s5", task_id: "k5", state: "failed", updated_at: nowSeconds - 7200, finished_at: nowSeconds - 7200 },
  ]);
  const stuckButtons = clickButtonWithLabel("清除卡住的任务");
  assert.equal(stuckButtons.length, 1, "进行中层头部恰一个清除卡住按钮");
  assert.equal(stuckButtons[0].hidden, false, "存在卡住任务（k1）时按钮可见");

  /* 判定闭集（导出纯函数）：30 分钟边界 / 远端运行排除 / 暂停与终态不进集 */
  const { isStuckTask } = await import("../frontend/modules/tasks-drawer/task-cards.js");
  assert.equal(isStuckTask({ state: "queued", updated_at: nowSeconds - 29 * 60 }), false, "29 分钟不算卡住");
  assert.equal(isStuckTask({ state: "queued", updated_at: nowSeconds - 31 * 60 }), true, "31 分钟算卡住");
  assert.equal(isStuckTask({ state: "running", display_state: "remote_running", updated_at: nowSeconds - 3600 }), false, "远端运行不进卡住集");
  assert.equal(isStuckTask({ state: "paused", updated_at: nowSeconds - 3600 }), false, "暂停不进卡住集");
  assert.equal(isStuckTask({ state: "failed", updated_at: nowSeconds - 3600 }), false, "终态不进卡住集");

  /* 两击臂式：首击确认文案（任务书原文），再击执行闭集路由 */
  stuckButtons[0].click();
  assert.equal(
    stuckButtons[0].textContent,
    "这些任务可能因为异常退出而卡住，清除后不影响已完成的任务",
    "首击确认文案=任务书人话原文",
  );
  assert.equal(postCalls.length, 0, "确认前零请求");
  stuckButtons[0].click();
  await settle();
  assert.equal(postCalls.length, 1, "再击执行清除请求");
  assert.deepEqual(postCalls[0], { route: "tasks/clear-stuck" }, "清除走闭集路由");
  /* toast 栈上限 3（旧条目被裁）：断言栈顶=本次人话计数文案 */
  const stuckToast = String(byId["toast-region"].children[byId["toast-region"].children.length - 1]?.textContent || "");
  assert.equal(stuckToast, "已清除 2 个卡住的任务", "清除 toast 带计数");

  /* 无卡住任务：载荷变化全量重建，按钮常驻挂载但隐藏 */
  render([{ ...base, task_id: "k6", state: "running", updated_at: nowSeconds - 60 }]);
  const idleButtons = clickButtonWithLabel("清除卡住的任务");
  assert.equal(idleButtons.length, 1, "活动层存在时按钮常驻挂载");
  assert.equal(idleButtons[0].hidden, true, "无卡住任务时按钮隐藏");

  /* PF1 跳过路径对钟：载荷零变化重复渲染不清 DOM、维持正确可见性
     （跨 30 分钟线的翻起由同一条 lastClearStuckButton 引用路径执行） */
  render([{ ...base, task_id: "k6", state: "running", updated_at: nowSeconds - 60 }]);
  const afterSkip = clickButtonWithLabel("清除卡住的任务");
  assert.equal(afterSkip.length, 1, "跳过路径不清除 DOM");
  assert.equal(afterSkip[0].hidden, true, "跳过路径维持正确可见性");
  console.log("ok: DEAD-TASK-PURGE 清除卡住任务（可见性闭集/判定纯函数/两击臂原文/闭集路由/toast 带计数/PF1 对钟）");
}

/* ---- SWEEPFIX-2 T5（化身走查 SWEEP1-T5）：stale 进行中卡唯一态行 + 「证据」术语软化。
   修前红：t-evidence 行（「等待重新确认」）与 t-stale-note 行（「证据已过期，
   等待重新确认」）同卡双行复读，且「证据」是内部术语。 ---- */
{
  renderTasksValue([
    { task_id: "st1", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "running", stale: true, updated_at: 100 },
    { task_id: "st2", course_id: "c1", sub_id: "s2", kind: "summary", state: "running", estimate_basis: "stale", updated_at: 101 },
  ]);
  const cardOf = (key) => cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === key));
  for (const key of ["st1", "st2"]) {
    const card = cardOf(key);
    assert.notEqual(card, undefined, `stale 卡 ${key} 渲染`);
    const allText = deepElements(card).map((node) => String(node.textContent || "")).join("\n");
    assert.equal((allText.match(/等待重新确认/g) || []).length, 1, `stale 卡 ${key} 唯一「等待重新确认」行（修前红=两行复读）`);
    assert.ok(!allText.includes("证据"), `stale 卡 ${key} 「证据」内部术语不上学生卡面（修前红=「证据已过期」）`);
    const staleNote = deepElements(card).find((node) => String(node.className || "").split(/\s+/).includes("t-stale-note"));
    assert.equal(staleNote?.textContent, "进度信息已过期，等待重新确认", `stale 卡 ${key} 唯一 stale 行=软化后人话闭集`);
    assert.equal(deepElements(card).some((node) => String(node.className || "") === "t-evidence"), false, `stale 卡 ${key} t-evidence 复读行退役`);
  }
  /* 非 stale 进行中卡不受影响：证据行照常在位 */
  renderTasksValue([
    { task_id: "st3", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "running", elapsed_seconds: 120, updated_at: 102 },
  ]);
  const freshCard = cardOf("st3");
  assert.equal(deepElements(freshCard).some((node) => String(node.className || "") === "t-evidence"), true, "非 stale 卡证据行保留");
  console.log("ok: SWEEPFIX-2 T5 stale 卡唯一态行+术语软化（双行复读退役）");
}

/* ---- SWEEPFIX-3 T6（化身走查 SWEEP1-T6）：重启恢复后活动任务进度单位失真
   + 阶段 label 丢失。载荷形状 = public_task 真实发布面（顶层 label /
   progress_unit / completed / total，无嵌套 progress 对象）：
   ① label 钉：进行中卡 t-phase 须显示后端人话阶段「正在转写音频」
   （修前红=旧读法 task.progress?.label 在 API 形状里不存在，死路永不显示）；
   ② seconds 单位钉：progress_unit="seconds" 渲染时长人话（1 分钟 / 10 分钟），
   绝不出「600 项」（转写秒数被说成项数=走查失真原文）；
   ③ items 回归钉：无 progress_unit 时保持既有「N / M 项」闭集不变。 ---- */
{
  renderTasksValue([
    {
      task_id: "t6", course_id: "c1", sub_id: "s1", kind: "subtitle", state: "running",
      label: "正在转写音频", completed: 96, total: 600, progress_unit: "seconds",
      started_at: 40, updated_at: 100,
    },
  ]);
  const t6 = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "t6"));
  const phase = deepElements(t6).find((node) => String(node.className || "") === "t-phase");
  assert.equal(phase?.textContent, "正在转写音频", "T6 进行中卡显示后端人话阶段 label（修前红=死路读法不渲染）");
  const t6Text = deepElements(t6).map((node) => String(node.textContent || "")).join("\n");
  assert.ok(t6Text.includes("1 分钟 / 10 分钟"), "T6 seconds 单位渲染时长人话");
  assert.ok(!t6Text.includes("600 项"), "T6 转写秒数绝不被说成「600 项」");

  renderTasksValue([
    { task_id: "t6i", course_id: "c1", sub_id: "s2", kind: "summary", state: "running", completed: 3, total: 12, updated_at: 101 },
  ]);
  const t6i = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "t6i"));
  const t6iText = deepElements(t6i).map((node) => String(node.textContent || "")).join("\n");
  assert.ok(t6iText.includes("3 / 12 项"), "T6 items 单位回归钉（无 progress_unit 保持「N / M 项」）");
  console.log("ok: SWEEPFIX-3 T6 阶段 label 人话在位+seconds 单位渲染+items 回归");
}

/* ---- SWEEPFIX-3 T7（化身走查 SWEEP1-T7）：完成记录用量（deepseek_tokens/
   runner_seconds）在技术详情折叠区可见。人话用量卡面已有（AS6/O2 行）；
   此处沿 estimate_basis 同款 raw-key 诊断面补精确值核对行（键=public_task
   闭集）：①有用量→两键人话数值精确在位（修前红=技术详情零用量信息）；
   ②无用量字段→零编造（runner_seconds 后端 public_task 现未发布=该键缺省
   不渲染，发布后自动点亮）。 ---- */
{
  renderTasksValue([
    {
      task_id: "t7", course_id: "c1", sub_id: "s3", kind: "subtitle", state: "completed",
      deepseek_tokens: 120000, runner_seconds: 754, updated_at: 100, finished_at: 100,
    },
    {
      task_id: "t7n", course_id: "c1", sub_id: "s4", kind: "subtitle", state: "completed",
      updated_at: 100, finished_at: 100,
    },
  ]);
  const technicalTexts = (key) => {
    const card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === key));
    return [...card.querySelectorAll(".task-technical")].flatMap(
      (node) => [...node.querySelectorAll("code")].map((code) => String(code.textContent)),
    );
  };
  const t7Detail = technicalTexts("t7").join("\n");
  assert.ok(t7Detail.includes("deepseek_tokens: 120000"), "T7 技术详情含 deepseek_tokens 精确值（修前红=零用量信息）");
  assert.ok(t7Detail.includes("runner_seconds: 754"), "T7 技术详情含 runner_seconds 精确值（字段在场即渲染）");
  const t7nDetail = technicalTexts("t7n").join("\n");
  assert.ok(!t7nDetail.includes("deepseek_tokens"), "T7 无用量字段零编造");
  assert.ok(!t7nDetail.includes("runner_seconds"), "T7 无 runner 字段零编造");
  /* O2/AS6 卡面人话行回归守卫：云端运行+≈万 tokens 同行不回退 */
  const t7Card = cardsOf().find((node) => deepElements(node).some((item) => item.dataset?.taskKey === "t7"));
  const t7Evidence = [...t7Card.querySelectorAll(".t-evidence")].map((node) => String(node.textContent));
  assert.deepEqual(t7Evidence, ["云端运行 12 分钟 · ≈12 万 tokens"], "T7 卡面 O2/AS6 人话用量行回归守卫");
  console.log("ok: SWEEPFIX-3 T7 技术详情用量精确值行+无字段零编造+卡面人话回归");
}

/* ---- SWEEPFIX-R2 W1（D-20261009-15①）：字幕任务 active→completed 跃迁
   恰一次广播 courselens:transcript-refresh（甲-1b courseware_pdf 同族手法），
   学习页据此热重读文稿、笔记按钮随 transcriptHasTiming 热启用——学生停在
   本讲就能点，不再「重进讲次」。同任务重复上报不重播；非字幕任务不广播。 ---- */
{
  const { announceNewlyCompletedSubtitleTasks } = await import("../frontend/modules/tasks-drawer/task-cards.js");
  const refreshes = [];
  const onRefresh = (event) => refreshes.push({ ...event.detail });
  windowTarget.addEventListener("courselens:transcript-refresh", onRefresh);
  announceNewlyCompletedSubtitleTasks([{ task_id: "w1a", kind: "subtitle", state: "running", course_id: "c1", sub_id: "s9" }]);
  announceNewlyCompletedSubtitleTasks([{ task_id: "w1a", kind: "subtitle", state: "completed", course_id: "c1", sub_id: "s9" }]);
  assert.deepEqual(refreshes, [{ course_id: "c1", sub_id: "s9" }], "字幕完成跃迁恰一次广播 transcript-refresh（修前红=零广播）");
  announceNewlyCompletedSubtitleTasks([{ task_id: "w1a", kind: "subtitle", state: "completed", course_id: "c1", sub_id: "s9" }]);
  assert.equal(refreshes.length, 1, "同任务完成态重复上报不重播");
  announceNewlyCompletedSubtitleTasks([{ task_id: "w1b", kind: "summary", state: "running", course_id: "c1", sub_id: "s9" }]);
  announceNewlyCompletedSubtitleTasks([{ task_id: "w1b", kind: "summary", state: "completed", course_id: "c1", sub_id: "s9" }]);
  assert.equal(refreshes.length, 1, "非字幕任务完成不广播");
  windowTarget.removeEventListener("courselens:transcript-refresh", onRefresh);
  console.log("ok: SWEEPFIX-R2 W1 字幕完成跃迁广播 transcript-refresh 恰一次");
}
