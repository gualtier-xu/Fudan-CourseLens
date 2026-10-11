import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFile } from "node:fs/promises";
import { createInputModalityTracker } from "../frontend/modules/shell.js";
import { installMediaGuard } from "../frontend/modules/media-guard.js";
import { UI_HINTS } from "../frontend/modules/api.js";

/* BUGFIX-UI-CONSOLIDATION-1 专项矩阵：课程目录行壳（无嵌套交互控件）、自动整理
 * 开关状态机（off/saving/on/attention/unavailable + aria-pressed + 陈旧响应保护）、
 * 无对话框直存链（首次/重试均不再弹隐私披露；失败闭集文案 + 重试落在触发行内）、
 * 快照单一轮询者=课程目录、任务抽屉并入自动材料运行记录的结构契约，以及
 * 目录对齐/方形开关/排版反转/设备码可用性/焦点与命中区 CSS 契约。 */

class FakeElement extends EventTarget {
  constructor(tag, id = "") {
    super();
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.value = "";
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.attributes = new Map();
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      toggle: (name, enabled) => {
        if (enabled === undefined) return this.classList.values.has(name) ? this.classList.values.delete(name) : this.classList.values.add(name);
        return enabled ? (this.classList.values.add(name), true) : (this.classList.values.delete(name), false);
      },
      contains: (name) => this.classList.values.has(name),
    };
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) {
    for (const node of nodes) {
      if (!node) continue;
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
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
  querySelectorAll(selector) {
    const found = [];
    const match = (node) => {
      const text = String(selector);
      if (text.startsWith(".")) return String(node.className || "").split(/\s+/).includes(text.slice(1));
      return node.tagName === text.toUpperCase();
    };
    const walk = (node) => (node.children || []).forEach((child) => {
      if (match(child)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  /* NIGHT2-W13 拖拽排序需要兄弟定位与插拔 */
  get previousElementSibling() {
    const list = this.parent?.children || [];
    return list[list.indexOf(this) - 1] || null;
  }
  get nextElementSibling() {
    const list = this.parent?.children || [];
    return list[list.indexOf(this) + 1] || null;
  }
  insertBefore(node, ref) {
    if (node.parent) node.parent.children = node.parent.children.filter((item) => item !== node);
    node.parent = this;
    const index = ref ? this.children.indexOf(ref) : this.children.length;
    if (index < 0) this.children.push(node);
    else this.children.splice(index, 0, node);
    return node;
  }
}

const BUTTON_IDS = [
  "refresh-catalog", "copy-catalog-diagnostics", "study-back-courses", "study-back-select", "study-start-select",
  "reload-transcript", "generate-quiz", "analyze-concepts",
];
const IDS = [
  ...BUTTON_IDS,
  "study-empty", "study-select", "study-desk", "study-course-list", "study-lecture-list",
  "study-course-title", "study-course-meta", "catalog-evidence", "catalog-recovery",
  "catalog-recovery-title", "catalog-recovery-impact", "catalog-recovery-actions",
  "catalog-diagnostic-code", "catalog-term-filter", "transcript-list", "bookmark-list",
  "bookmark-action-state", "artifact-content", "artifact-notices", "artifact-kind",
  "document-list", "quiz-list", "review-list", "concept-list", "analytics-summary",
  "topbar-crumbs", "player-stage", "document-input",
  "course-automation-live", "live-room-capability",
];

const byId = Object.fromEntries([
  ...IDS.map((id) => [id, new FakeElement(BUTTON_IDS.includes(id) ? "button" : id === "catalog-term-filter" || id === "artifact-kind" ? "select" : "div", id)]),
]);
const createdSeq = { value: 0 };
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${createdSeq.value += 1}`),
  createElementNS: (_ns, tag) => new FakeElement(tag, `created-ns-${createdSeq.value += 1}`),
  createDocumentFragment: () => new FakeElement("#document-fragment", `created-fragment-${createdSeq.value += 1}`),
  querySelector: (selector) => {
    const text = String(selector);
    if (text.includes(".materials-tabs")) return named.materialsTabs;
    if (text.includes(".lecture-pane")) return named.lecturePane;
    return null;
  },
  querySelectorAll: () => [],
  activeElement: null,
  documentElement: { dataset: {} },
});
const named = {
  materialsTabs: new FakeElement("div", "materials-tabs-fake"),
  lecturePane: new FakeElement("section", "lecture-pane-fake"),
};
named.materialsTabs.className = "materials-tabs";
named.lecturePane.className = "lecture-pane";
globalThis.window = Object.assign(new EventTarget(), {
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => fn(),
});
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
};
globalThis.CSS = { escape: (value) => String(value) };
globalThis.Option = class {
  constructor(text, value) { this.text = text; this.value = value; }
};
globalThis.matchMedia = (query) => ({
  matches: false, media: query,
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
});
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};

/* ---- 网络桩：请求日志 + 可注入快照/账号/失败码 ---- */

const requests = [];
let automationSnapshotPayload = null;
let accountsPayload = { accounts: [{ student_id: "2026001", requires_rotation: false }], deepseek: { configured: true, saved: true } };
let catalogPayload = null;
/* 讲次卡观看进度桩（sub_id → 持久进度行）与读取计数 */
let progressGetCalls = 0;
const progressRows = new Map();
const failures = {}; /* stage -> error_code */
const delayed = {}; /* stage -> deferred */

function ok(data, status = 200) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function errorResponse(errorCode, status = 400) {
  return new Response(JSON.stringify({ error: "synthetic", error_code: errorCode }), {
    status, headers: { "Content-Type": "application/json" },
  });
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  const body = options.body ? JSON.parse(options.body) : {};
  requests.push({ route, method, body });
  if (method === "GET" && route.startsWith("/api/v3/automation")) return ok(automationSnapshotPayload);
  if (method === "GET" && route.startsWith("/api/v3/accounts")) return ok(accountsPayload);
  if (method === "GET" && route.startsWith("/api/v3/catalog")) return ok(catalogPayload);
  if (method === "PUT" && route === "/api/v3/automation/config") {
    if (delayed.config) { await delayed.config.promise; }
    if (failures.config) return errorResponse(failures.config);
    /* 仿后端 update_config：规则合并成功但验证作废、回到 disabled 草稿 */
    automationSnapshotPayload = {
      ...automationSnapshotPayload,
      rules: body.config.rules,
      selected_courses: body.config.rules.length,
      account_id: body.config.account_id,
      enabled: false,
      state: "disabled",
      config_hash: "",
      verified: false,
    };
    return ok(automationSnapshotPayload);
  }
  if (method === "PUT" && route === "/api/v3/automation/cloud-secrets") {
    if (failures.secrets) return errorResponse(failures.secrets);
    return ok(automationSnapshotPayload);
  }
  if (method === "POST" && route === "/api/v3/automation/actions") {
    if (failures[body.action]) return errorResponse(failures[body.action]);
    if (body.action === "enable-cloud") {
      automationSnapshotPayload = {
        ...automationSnapshotPayload,
        state: "ready", enabled: true, verified: true, config_hash: "e".repeat(64),
      };
    }
    if (body.action === "disable-cloud") {
      automationSnapshotPayload = { ...automationSnapshotPayload, state: "disabled", enabled: false, verified: false, config_hash: "" };
    }
    return ok({ operation: { state: "completed" } });
  }
  if (method === "GET" && route.startsWith("/api/v3/progress")) {
    progressGetCalls += 1;
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ progress: progressRows.get(subId) || null });
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

const { store } = await import("../frontend/modules/store.js");
const { installStudy, renderCourses } = await import("../frontend/modules/study.js");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 5; index += 1) await nextTurn();
};

/* 课表：今天学期 + 其他学期（默认当前学期过滤） */
const now = new Date();
const academicStart = now.getMonth() >= 7 ? now.getFullYear() : now.getFullYear() - 1;
const semester = now.getMonth() >= 7 || now.getMonth() === 0 ? "1" : "2";
const currentTerm = `${academicStart}-${academicStart + 1}-${semester}`;
const otherTerm = `${academicStart}-${academicStart + 1}-${semester === "1" ? "2" : "1"}`;
const courses = [
  { course_id: "c1", title: "算法课程甲", teacher: "师一", term: currentTerm, lectures: [{ sub_id: "s1", sub_title: "第1讲", can_stream: true }] },
  { course_id: "c2", title: "历史课程乙", teacher: "师二", term: currentTerm, lectures: [] },
  { course_id: "old", title: "往年课程丙", teacher: "师三", term: otherTerm, lectures: [] },
];

const rows = () => byId["study-course-list"].children.filter((node) => node.className === "course-row");
const toggleOf = (row) => row.children.find((node) => String(node.className || "").includes("course-automation-toggle"));
const mainOf = (row) => row.querySelector(".course-row-main");

const baseSnapshot = (overrides = {}) => ({
  schema: "courselens.automation.v3",
  protocol: "cloud-automation.v3",
  disclosure_version: "cloud-custody-disclosure.v1",
  state: "disabled", enabled: false, account_id: "",
  schedule: { times: ["13:00", "22:00"], timezone: "Asia/Shanghai", next_two: [] },
  rules: [], selected_courses: 0,
  retention: { result_days: 30, state_days: 90, ceiling_days: 90 },
  target: { repository: "synthetic-owner/courselens-worker-synthetic" },
  binding_confirmed: true, verification: {}, config_hash: "", verified: false,
  ai_key_available: true, circuits: [], last_cloud_run: null, runs: [], imports: [],
  observed_at: 0, expires_at: 0, stale: false, actions: [],
  ...overrides,
});

/* 渲染目录：默认当前学期 → 只有 c1/c2 获得行壳与开关；历史课程不作为可选范围 */
const studyCleanup = await installStudy(store); /* 订阅共享 store 的自动化快照 */
catalogPayload = { state: "ready", code: "authorized_catalog_verified", courses, course_count: courses.length };
renderCourses(store, catalogPayload);
await settle();
assert.equal(rows().length, 2, "目录默认当前学期：历史学期课程不渲染自动开关");
for (const row of rows()) {
  assert.equal(row.tagName, "DIV", "课程行壳是非交互容器");
  const main = mainOf(row);
  assert.ok(main && main.tagName === "BUTTON", "行壳内是课程导航按钮");
  assert.ok(!main.children.some((node) => node.tagName === "BUTTON" || node.tagName === "INPUT"), "按钮内不嵌套交互控件");
  const toggle = toggleOf(row);
  assert.ok(toggle && toggle.tagName === "BUTTON", "行壳内有兄弟自动整理开关");
  assert.equal(toggle.getAttribute("aria-pressed"), "false", "开关 aria-pressed 初始 false");
  assert.ok(toggle.getAttribute("aria-label").includes(main.textContent || "") || toggle.children.length >= 2, "开关带课程专属可读标签");
}
assert.equal(mainOf(rows()[0]).getAttribute("aria-current"), "true", "高亮落在导航按钮上");

/* A11Y-IMPL-1（D14 P2-2/P2-3）：列表语义 + 键盘排序提示可见化。
   容器 role=list、行 role=listitem（纯 aria，样式零变化）；行 ≥2 时列表头部
   一行快捷键提示（文案=api.js 闭集表 UI_HINTS 原文）+ 课程钮 title 同文案。
   A11Y-IMPL-5（D14 P2-2 残余）：draggable 行同门控挂 aria-roledescription 行述。 */
{
  const courseList = byId["study-course-list"];
  assert.equal(courseList.getAttribute("role"), "list", "课程列表容器带 list 语义");
  assert.ok(rows().every((row) => row.getAttribute("role") === "listitem"), "每个课程行带 listitem 语义");
  assert.ok(rows().every((row) => row.getAttribute("aria-roledescription") === UI_HINTS.course_row_reorderable), "行 >1 时每个课程行带读屏行述（=闭集表原文联动，非字面）");
  const hintNode = () => courseList.children.find((node) => node.id === "course-order-hint");
  const hint = hintNode();
  assert.ok(hint, "行 ≥2 时课程列表头部有快捷键提示行");
  assert.equal(courseList.children.indexOf(hint), 0, "提示行在列表头部（首位）");
  assert.equal(hint.textContent, "Alt+↑/↓ 调整课程顺序", "提示文案=api.js 闭集表原文");
  assert.ok(String(hint.className || "").split(/\s+/).includes("hint"), "提示复用既有 hint 皮（零新增样式面）");
  assert.equal(mainOf(rows()[0]).title, "Alt+↑/↓ 调整课程顺序", "课程钮 title 同文案");
  /* 单门课：排序无意义——提示行与 title 都不出现（不给学生无用提示） */
  renderCourses(store, { ...catalogPayload, courses: [courses[0]], course_count: 1 });
  await settle();
  assert.ok(!hintNode(), "仅一门课不渲染排序提示行");
  assert.equal(String(mainOf(rows()[0]).title || ""), "", "仅一门课课程钮不带排序 title");
  assert.ok(rows().every((row) => row.getAttribute("aria-roledescription") === null), "仅一门课课程行不带读屏行述（无可排序语义）");
  assert.equal(courseList.getAttribute("role"), "list", "单行仍是合法列表");
  /* 空目录：list 语义摘除（空段落不挂空列表），恢复后语义随行回来 */
  renderCourses(store, { state: "ready", code: "authorized_catalog_verified", courses: [], course_count: 0 });
  await settle();
  assert.equal(courseList.getAttribute("role"), null, "空目录摘除 list 语义");
  assert.ok(!hintNode(), "空目录无排序提示行");
  renderCourses(store, catalogPayload);
  await settle();
  assert.equal(courseList.getAttribute("role"), "list", "目录恢复即恢复 list 语义");
  assert.ok(hintNode(), "目录恢复提示行随行回来");
}

/* unavailable：快照不可用 → 开关禁用、诚实不可用态 */
store.set("automation", null);
await settle();
assert.equal(toggleOf(rows()[0]).dataset.state, "unavailable", "无快照时开关不可用");
assert.equal(toggleOf(rows()[0]).disabled, true, "无快照时开关禁用");

/* off → 直接保存链（无任何隐私披露对话框）：账号按「恰一个可用」就地解析 */
requests.length = 0;
automationSnapshotPayload = baseSnapshot();
store.set("automation", baseSnapshot());
await settle();
const toggleC1 = toggleOf(rows()[0]);
assert.equal(toggleC1.dataset.state, "off", "快照存在且未选中 → off");
assert.equal(toggleC1.querySelector(".course-automation-text") || null, null, "开关不再携带「自动」汉字文本");
assert.ok(toggleC1.querySelector(".course-automation-box"), "可见状态是文字无关的圆角方形块");
toggleC1.click();
await settle();
const configPut = requests.find((request) => request.method === "PUT" && request.route === "/api/v3/automation/config");
assert.ok(configPut, "首次开启直接走保存链（PUT 开始），不弹任何对话框");
assert.deepEqual(configPut.body.config.rules, [{ course_id: "c1" }], "PUT 只带课程标识（基线由后端捕获）");
assert.equal(configPut.body.config.account_id, "2026001", "恰一个可用账号就地解析");
assert.match(String(configPut.body.operation_id || ""), /^course-auto-cfg:[A-Za-z0-9._:-]{8,128}$/, "PUT 带闭集形状的幂等 operation_id（后端 PUT 通道必填，缺它每次开云必拒）");
const secretsPost = requests.find((request) => request.route === "/api/v3/automation/cloud-secrets");
assert.ok(secretsPost, "链中包含加密上传（既有披露记录版本，不新增同意层）");
assert.equal(secretsPost.method, "PUT", "加密上传走与保存配置同一 PUT 幂等通道（后端仅 PUT 分支，POST 落 404）");
assert.equal(secretsPost.body.disclosure_version, "cloud-custody-disclosure.v1");
assert.equal(secretsPost.body.confirmed, true);
assert.match(String(secretsPost.body.operation_id || ""), /^cloud-secrets:[A-Za-z0-9._:-]{8,128}$/, "上传同样携带闭集形状的幂等 operation_id");
assert.ok(!JSON.stringify(secretsPost.body).includes("password"), "上传载荷不含任何秘密字段");
const verifyPost = requests.find((request) => request.body.action === "verify-cloud-credentials");
const enablePost = requests.find((request) => request.body.action === "enable-cloud");
assert.ok(verifyPost && enablePost, "保存链以验证+启用收口");
assert.ok(byId["course-automation-live"].textContent.includes("自动整理已开启"), "状态变化有上下文播报");
assert.equal(toggleC1.getAttribute("aria-pressed"), "true", "保存成功后开关为 on");
assert.equal(toggleC1.dataset.state, "on", "开关状态来自快照回读");

/* on → off：同一条链（禁用到空选择以 disable-cloud 收口），全程零对话框 */
requests.length = 0;
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, config_hash: "d".repeat(64), account_id: "2026001", rules: [{ course_id: "c1", baseline_count: 3 }], selected_courses: 1 });
store.set("automation", automationSnapshotPayload);
await settle();
toggleC1.click();
await settle();
const putOff = requests.find((request) => request.method === "PUT");
assert.ok(putOff && putOff.body.config.rules.length === 0, "关闭到空选择 PUT 空规则");
assert.ok(requests.some((request) => request.body.action === "disable-cloud"), "空选择以 disable-cloud 收口");
assert.equal(toggleC1.dataset.state, "off", "关闭后回到 off");

/* enabled 快照下的再次开启：静默链（同一保存链，无任何确认层） */
requests.length = 0;
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, config_hash: "d".repeat(64), account_id: "2026001" });
store.set("automation", automationSnapshotPayload);
await settle();
toggleC1.click();
await settle();
assert.ok(requests.some((request) => request.method === "PUT"), "已托管时直接走保存链");
assert.equal(toggleC1.dataset.state, "on", "再次开启成功");

/* saving：进行中禁用重复激活（陈旧激活防护的入口） */
requests.length = 0;
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "2026001" });
store.set("automation", automationSnapshotPayload);
await settle();
const gate = deferred();
delayed.config = gate;
toggleC1.click();
await nextTurn();
assert.equal(toggleC1.dataset.state, "saving", "保存中显示 saving");
assert.equal(toggleC1.disabled, true, "保存中禁用重复激活");
toggleC1.click();
await nextTurn();
const putCountDuringSave = requests.filter((request) => request.method === "PUT").length;
gate.resolve();
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "2026001", rules: [{ course_id: "c1" }], selected_courses: 1 });
await settle();
assert.equal(toggleC1.dataset.state, "on", "保存完成后落定 on");
assert.ok(putCountDuringSave <= 1, "进行中重复点击不产生第二条规则请求");

/* attention：fail-closed 闭集文案 + 触发行内恢复说明与重试动作 */
const inlineNote = () => rows()[0].children.find((node) => String(node.className || "").includes("course-automation-note"));
requests.length = 0;
failures.config = "cloud_catalog_unavailable";
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "2026001" });
store.set("automation", automationSnapshotPayload);
await settle();
toggleC1.click();
await settle();
assert.equal(toggleC1.dataset.state, "attention", "保存失败进入 attention");
assert.ok(byId["course-automation-live"].textContent.includes("课程目录尚未确认"), "attention 播报闭集处置文案");
assert.equal(toggleC1.disabled, false, "attention 可再次操作");
const note = inlineNote();
assert.ok(note, "attention 在触发行内给出失败说明");
assert.ok((note.querySelector("span")?.textContent || "").includes("课程目录尚未确认"), "行内说明为闭集处置文案");
const retry = note.children.find((node) => node.tagName === "BUTTON");
assert.ok(retry, "行内提供重试动作");
delete failures.config;
requests.length = 0;
retry.click();
await settle();
assert.ok(requests.some((request) => request.method === "PUT"), "重试直接回到保存链");
assert.equal(toggleC1.dataset.state, "on", "重试成功后落定 on");
assert.ok(!inlineNote(), "成功后行内说明清除");

/* attention：AI key 缺失发生在加密上传阶段（同样行内呈现） */
failures.secrets = "deepseek_key_missing";
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "2026001" });
store.set("automation", automationSnapshotPayload);
await settle();
toggleC1.click();
await settle();
assert.equal(toggleC1.dataset.state, "attention", "上传失败进入 attention");
assert.ok(byId["course-automation-live"].textContent.includes("DeepSeek API Key"), "AI key 缺失给准备指引");
assert.ok((inlineNote()?.querySelector("span")?.textContent || "").includes("DeepSeek API Key"), "AI key 缺失行内说明");
delete failures.secrets;
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "2026001", rules: [{ course_id: "c1" }], selected_courses: 1 });
store.set("automation", automationSnapshotPayload);
await settle();
assert.equal(toggleC1.dataset.state, "on", "新快照落定 on 并清除行内说明");
assert.ok(!inlineNote(), "快照确认成功后行内说明不再残留");

/* 账号不可解析（零可用账号）：仍直走保存链，不猜测账号，交由后端闭集裁决 */
requests.length = 0;
accountsPayload = { accounts: [], deepseek: { configured: false, saved: false } };
automationSnapshotPayload = baseSnapshot({ state: "ready", enabled: true, verified: true, account_id: "" });
store.set("automation", automationSnapshotPayload);
await settle();
toggleC1.click();
await settle();
const putNoAccount = requests.find((request) => request.method === "PUT");
assert.ok(putNoAccount, "零可用账号仍直接走保存链（不弹对话框、不本地捏造失败）");
assert.equal(putNoAccount.body.config.account_id, "", "不猜测账号：空账号交由后端闭集裁决");
accountsPayload = { accounts: [{ student_id: "2026001", requires_rotation: false }], deepseek: { configured: true, saved: true } };

/* ---- 讲次卡观看进度：按 sub_id 恢复持久进度、简洁可扫描文案、事件即时回写、
   错讲次/无进度诚实降级；“可播放”成功态文案退场，“需要登录”保留。 ---- */
const lectureSpanText = (subId) => byId["study-lecture-list"].children
  .find((node) => String(node.dataset?.subId || "") === subId)
  ?.children.find((node) => node.tagName === "SPAN")?.textContent || "";
progressRows.set("s1", { sub_id: "s1", course_id: "c1", position_seconds: 900, duration_seconds: 3600, progress_percent: 25, completed: false });
renderCourses(store, {
  state: "ready", code: "authorized_catalog_verified", course_count: 3,
  courses: [
    {
      ...courses[0],
      lectures: [
        { sub_id: "s1", sub_title: "第1讲", can_stream: true, date: "9月10日" },
        { sub_id: "s2", sub_title: "第2讲", can_stream: false },
      ],
    },
    ...courses.slice(1),
  ],
});
await settle();
assert.ok(lectureSpanText("s1").includes("看到 25%"), "讲次卡显示简洁观看进度");
assert.ok(lectureSpanText("s1").includes("9月10日"), "讲次卡日期保留");
assert.equal(lectureSpanText("s1").includes("可播放"), false, "成功态“可播放”不再展示");
assert.ok(lectureSpanText("s2").includes("需要登录"), "不可播放讲次保留登录提示");
assert.equal(lectureSpanText("s2").includes("看到"), false, "无进度不虚构进度文案");
const progressGetsBeforeEvent = progressGetCalls;
window.dispatchEvent(new CustomEvent("courselens:watch-progress", {
  detail: { sub_id: "s1", course_id: "c1", position_seconds: 1800, duration_seconds: 3600, completed: false },
}));
assert.ok(lectureSpanText("s1").includes("看到 50%"), "保存进度事件后讲次卡即时更新");
window.dispatchEvent(new CustomEvent("courselens:watch-progress", {
  detail: { sub_id: "s2", course_id: "c1", position_seconds: 3600, duration_seconds: 3600, completed: true },
}));
assert.ok(lectureSpanText("s2").includes("已看完"), "已完成讲次显示已看完");
window.dispatchEvent(new CustomEvent("courselens:watch-progress", {
  detail: { sub_id: "s-elsewhere", course_id: "c1", position_seconds: 99, duration_seconds: 100, completed: false },
}));
assert.equal(lectureSpanText("s1").includes("99"), false, "错讲次进度事件不污染");
assert.equal(progressGetCalls, progressGetsBeforeEvent, "事件回写走缓存，零新增读取请求");

/* CSS 契约：44px 命中区、方形状态块金填、2px 焦点环、无全局 outline:none、
   scroll-padding、caret 纪律、forced-colors、排版反转、目录对齐、设备码可用性 */
const [componentsCss, accessibilityCss, tokensCss, pagesCss, indexHtml, settingsJs, studyJs, drawerJs] = await Promise.all([
  readFile(new URL("../frontend/styles/components.css", import.meta.url), "utf8"),
  readFile(new URL("../frontend/styles/accessibility.css", import.meta.url), "utf8"),
  readFile(new URL("../frontend/styles/tokens.css", import.meta.url), "utf8"),
  readFile(new URL("../frontend/styles/pages.css", import.meta.url), "utf8"),
  readFile(new URL("../frontend/index.html", import.meta.url), "utf8"),
  familySource("settings"),
  familySource("study"),
  familySource("tasks-drawer"),
]);
assert.match(componentsCss, /\.course-automation-toggle\s*\{[^}]*min-height:\s*44px[^}]*\}/, "开关 44px 命中区");
assert.match(componentsCss, /\.course-automation-toggle\s*\{[^}]*min-width:\s*44px[^}]*\}/, "开关 44px 宽度");
assert.match(componentsCss, /\.course-automation-toggle\[aria-pressed="true"\] \.course-automation-box\s*\{[^}]*var\(--gold\)/, "开启态主题金色实填在方形状态块上");
assert.match(accessibilityCss, /:focus-visible\s*\{\s*outline:\s*2px solid var\(--focus\)/, "2px token 焦点环保留");
assert.doesNotMatch(accessibilityCss, /\*\s*\{[^}]*outline:\s*none/, "无全局 outline:none");
assert.match(accessibilityCss, /scroll-padding-block-start/, "滚动预留焦点不被遮挡");
assert.match(pagesCss, /caret-color:\s*transparent/, "静态壳面光标抑制");
assert.match(accessibilityCss, /body\s*\{\s*caret-color:\s*transparent/, "全局静态面光标纪律（根治非输入面 caret）");
assert.match(accessibilityCss, /input,\s*textarea,\s*\[contenteditable\][^{]*\{\s*caret-color:\s*auto/, "真实输入显式恢复光标");
assert.match(accessibilityCss, /\.course-automation-toggle\[aria-pressed="true"\] \.course-automation-box\s*\{[^}]*Highlight/, "forced-colors 下开关状态不只靠颜色");
/* 三态焦点政策（NIGHT5-U1）：鼠标点击零环 / Tab 恒环 / 键盘用后点击粘滞环归零。 */
assert.match(accessibilityCss, /:focus:not\(:focus-visible\)\s*\{\s*outline:\s*none/, "控件级无环兜底（非键盘来源聚焦归零）");
assert.match(accessibilityCss, /:root\[data-input="pointer"\]\s*:focus,\s*\n:root\[data-input="pointer"\]\s*:focus-visible\s*\{\s*outline:\s*none/, "指针态聚焦环归零（粘滞启发式兜底）");
assert.match(accessibilityCss, /:root\[data-input="pointer"\] \.player-ctrl-timeline:focus-visible::-webkit-slider-thumb/, "指针态滑杆键盘强调形同步归零（时间轴）");
assert.match(accessibilityCss, /:root\[data-input="pointer"\] \.player-ctrl-volume:focus-visible::-webkit-slider-thumb/, "指针态滑杆键盘强调形同步归零（音量）");
assert.doesNotMatch(accessibilityCss, /\.settings-group > h2:focus,/, "分组标题不再保留普通 :focus 环（鼠标点击零环）");
/* D-20261009-08：交互标记层（章节/没听懂/难点刻度）提至 seek input 之上——
   三层容器 pointer-events:none、命中面=子标记自身；缺 z-index 时 DOM 后序的
   input#player-ctrl-timeline（inset:0 恒 32px）整带盖住标记，鼠标点按被夺为
   seek（键盘路径完好=当时仅键盘钉绿漏检的真机缺陷）。 */
assert.match(pagesCss, /\.player-timeline-markers,\s*\n\.player-timeline-flags,\s*\n\.player-timeline-assess\s*\{[^}]*z-index:\s*2/, "标记层提至 seek input 之上（D-20261009-08）");
for (const layer of ["player-timeline-markers", "player-timeline-flags", "player-timeline-assess"]) {
  assert.match(pagesCss, new RegExp(`\\.${layer}\\s*\\{[^}]*pointer-events:\\s*none`), `${layer} 容器零命中面（命中面=子标记，seek 带不收缩）`);
}
{
  const root = { dataset: {} };
  const surface = new EventTarget();
  const tracker = createInputModalityTracker({ surface, root });
  assert.equal(tracker.mode, "", "初始无模态标记（CSS 走默认 :focus-visible 判别）");
  tracker.attach();
  surface.dispatchEvent(new Event("keydown"));
  assert.equal(tracker.mode, "key", "键盘输入→key 态（Tab 恒环的前提）");
  surface.dispatchEvent(new Event("pointerdown"));
  assert.equal(tracker.mode, "pointer", "指针点击→pointer 态（环归零）");
  surface.dispatchEvent(new Event("keydown"));
  assert.equal(tracker.mode, "key", "指针后再键盘→回到 key 态");
  tracker.detach();
  surface.dispatchEvent(new Event("pointerdown"));
  assert.equal(tracker.mode, "key", "detach 后不再追踪（清理完整）");
}
/* 页面级防泄露护栏（NIGHT5-U2）：媒体面右键/拖拽/Ctrl+S/U 拦截，可编辑字段
   （右键与 Ctrl+S/U，D14-P34 两处豁免一致）与课程行拖拽保留。 */
{
  const editable = { closest: (sel) => (sel.startsWith("input") ? "input-root" : null) };
  const media = { closest: (sel) => (sel === "video, img, .player-stage-shell" ? "media-root" : null) };
  const plain = { closest: () => null };
  const mediaNode = { draggable: true };
  const videoNode = { draggable: true };
  const root = {
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    removeEventListener(type) { delete this.listeners[type]; },
    querySelectorAll: (sel) => (sel === "video, img" ? [mediaNode, videoNode] : []),
  };
  const surface = {
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    removeEventListener(type) { delete this.listeners[type]; },
  };
  const cleanup = installMediaGuard(null, root, surface);
  assert.equal(mediaNode.draggable, false, "静态图片安装即失去拖拽");
  assert.equal(videoNode.draggable, false, "视频安装即失去拖拽");
  const ctxEvent = (target) => { const e = { target, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; } }; root.listeners.contextmenu(e); return e.defaultPrevented; };
  const dragEvent = (target) => { const e = { target, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; } }; root.listeners.dragstart(e); return e.defaultPrevented; };
  const keyEvent = (extra) => { const e = { defaultPrevented: false, preventDefault() { e.defaultPrevented = true; }, ...extra }; surface.listeners.keydown(e); return e.defaultPrevented; };
  assert.equal(ctxEvent(media), true, "媒体面右键拦截");
  assert.equal(ctxEvent(editable), false, "可编辑字段右键保留（复制粘贴是学习动作）");
  assert.equal(ctxEvent(plain), false, "普通文本面右键不拦（转录复制不受影响）");
  assert.equal(dragEvent(media), true, "媒体拖拽拦截");
  assert.equal(dragEvent(plain), false, "课程行拖拽换位保留");
  assert.equal(keyEvent({ ctrlKey: true, key: "s" }), true, "Ctrl+S 拦截");
  assert.equal(keyEvent({ ctrlKey: true, key: "U" }), true, "Ctrl+U 拦截（大写键名归一）");
  assert.equal(keyEvent({ metaKey: true, key: "s" }), true, "Cmd+S 同拦（mac 键位）");
  assert.equal(keyEvent({ ctrlKey: true, key: "k" }), false, "Ctrl+K 搜索面板不受影响");
  assert.equal(keyEvent({ key: "s" }), false, "纯 s 键不拦");
  /* D14-P34：keydown 豁免与 contextmenu 一致——输入态恢复浏览器默认，非输入态照旧拦。 */
  assert.equal(keyEvent({ ctrlKey: true, key: "s", target: editable }), false, "输入框聚焦时 Ctrl+S 不拦（豁免与 contextmenu 一致）");
  assert.equal(keyEvent({ ctrlKey: true, key: "u", target: editable }), false, "输入框聚焦时 Ctrl+U 不拦（恢复浏览器默认）");
  assert.equal(keyEvent({ ctrlKey: true, key: "s", target: plain }), true, "非输入态 Ctrl+S 照旧拦截");
  cleanup();
  assert.equal(root.listeners.contextmenu, undefined, "清理摘除右键监听");
  assert.equal(surface.listeners.keydown, undefined, "清理摘除快捷键监听");
}
/* 字体轨升级（NIGHT5-U3）：UI 轨中文面退役（Georgia+楷体承载控件与小字），
   kbd 固定 mono 轨；全表面 font-family 声明扫闭集。 */
{
  const layoutCss = await readFile(new URL("../frontend/styles/layout.css", import.meta.url), "utf8");
  assert.match(tokensCss, /--font-ui:[^;]*Georgia[^;]*"KaiTi"/, "UI 轨西文 Georgia + 中文楷体");
  assert.doesNotMatch(tokensCss, /--font-ui:[^;]*(YaHei|PingFang|Microsoft)/, "雅黑/苹方自 UI 轨退役");
  assert.match(tokensCss, /kbd\s*\{\s*font-family:\s*var\(--font-mono\)/, "kbd 键帽固定 mono 轨");
  for (const css of [componentsCss, accessibilityCss, tokensCss, pagesCss, layoutCss]) {
    const decls = [...css.matchAll(/\bfont-family:\s*([^;]+);/g)].map((m) => m[1].trim());
    for (const decl of decls) {
      const ok = /^var\(--font-(display|editorial|ui|mono)\)$/.test(decl) || decl.startsWith('Georgia, "Noto Serif SC"');
      assert.ok(ok, `字体轨闭集越界: ${decl}`);
    }
    /* U6 视觉归并：面板/区块标题对齐合同字阶（h3=1rem 唯一面板档；明细头
       1.15rem 留第二档候选，晨间可调）+ 小安静钮配方收编 tokens。 */
    assert.match(pagesCss, /\.courseware-surface h3, \.imported-documents h3 \{ margin: 0 0 var\(--space-2\); font-size: 1rem;/, "资料面板标题对齐 1rem 档");
    assert.match(pagesCss, /\.exam-context h3 \{ margin: 0 0 var\(--space-1\); font-size: 1rem;/, "考试上下文标题对齐 1rem 档");
    assert.match(tokensCss, /--quiet-pad-y: 4px;/, "安静钮配方 tokens 落 tokens.css");
    assert.match(componentsCss, /\.text-button \{[^}]*var\(--quiet-pad-y\) var\(--quiet-pad-x\)/, "text-button 消费安静钮 tokens");
    /* 对比度复核：标题墨色 #1E3A5F 对画布 #F7F3EB ≈10.4 ≥4.5（WCAG AA） */
    assert.match(tokensCss, /--navy-ink: #1E3A5F;/, "标题墨色 token 在位（对比度 ≈10.4≥4.5）");
    const shorthands = [...css.matchAll(/\bfont:\s*([^;]+);/g)].map((m) => m[1].trim());
    for (const decl of shorthands) {
      const ok = decl.includes("var(--font-") || decl === "inherit"; // inherit=表单控件重置，不设轨
      assert.ok(ok, `font 简写越界: ${decl}`);
    }
  }
}
/* 目录对齐与选中态（item 1/2） */
assert.doesNotMatch(pagesCss, /\.course-layout\s*\{[^}]*max-width/, "目录列与上方卡片同一内容网格（无独立 1080px 收窄）");
assert.match(componentsCss, /\.course-row-main\s*\{\s*padding:\s*0 0 0 var\(--space-4\);\s*\}/, "激活金线落在文字左侧沟槽，不贴挤课程名");
/* 排版反转（item 8） */
assert.match(tokensCss, /body\s*\{[^}]*font-family:\s*var\(--font-editorial\)/, "正文默认本地宋体/衬线体系");
assert.ok(tokensCss.includes("font-family: var(--font-ui);"), "控件与状态文字保留无衬线例外");
assert.match(tokensCss, /\.course-row strong,\s*\.lecture-row strong,\s*\.s-title,[^}]*font-family:\s*var\(--font-editorial\)/, "课程/讲次/课表名衬线");
/* 设置页自动化板块退场；快照单一轮询者=课程目录；运行记录并入任务抽屉（item 4） */
assert.ok(!indexHtml.includes('id="settings-automation-group"'), "设置页自动化管理板块已移除");
assert.ok(!indexHtml.includes('id="course-automation-dialog"'), "重复隐私披露对话框已移除");
assert.ok(studyJs.includes("async function refreshAutomationSnapshot()"), "课程目录是快照唯一轮询者");
assert.ok(!settingsJs.includes('currentStore.set("automation"'), "设置页不再发布自动化快照");
assert.ok(drawerJs.includes("automation-runs-tier"), "自动材料运行记录并入任务抽屉");
assert.ok(drawerJs.includes('apiV3("automation")'), "抽屉消费既有自动化快照证据");
assert.ok(drawerJs.includes("字幕 ASR · 课件 OCR · AI 总结与章节"), "固定包如实展示");
assert.ok(!indexHtml.includes("home-overview-hint"), "低价值功能自述文案退场（item 10）");
/* GitHub 设备码可用性（item 13） */
assert.match(componentsCss, /\.device-code-value\s*\{[^}]*font-family:\s*var\(--font-mono\)/, "设备码等宽字体");
assert.match(componentsCss, /\.device-code-value\s*\{[^}]*user-select:\s*text/, "设备码显式可选中");
assert.ok(settingsJs.includes("function renderDeviceAuthorization"), "设备码带复制动作的专门渲染");
assert.ok(settingsJs.includes("复制验证码"), "设备码提供清晰复制动作");

/* NIGHT2-W13 课程拖拽排序：键盘移动+拖拽重排落本机存储，重渲染保持顺序 */
{
  renderCourses(store, catalogPayload);
  await settle();
  const idOf = (index) => mainOf(rows()[index]).dataset.courseId;
  assert.equal(idOf(0), "c1", "初始顺序=目录原序");
  const keyEvent = (key) => {
    const event = new Event("keydown");
    event.altKey = true;
    event.key = key;
    event.preventDefault = () => {};
    return event;
  };
  mainOf(rows()[0]).dispatchEvent(keyEvent("ArrowDown"));
  await settle();
  assert.equal(idOf(0), "c2", "Alt+↓ 后 c2 在前");
  assert.equal(idOf(1), "c1", "Alt+↓ 后 c1 在后");
  assert.deepEqual(
    JSON.parse(globalThis.localStorage.store.get("courselens.course-order.v1")),
    ["c2", "c1"],
    "键盘移动落本机存储（视图偏好，不动目录真值）",
  );
  renderCourses(store, catalogPayload);
  await settle();
  assert.equal(idOf(0), "c2", "重渲染保持已存顺序");
  /* 拖拽：c2（第一行）拖回 c1 之后 → 顺序复原 */
  const source = rows()[0];
  const targetRow = rows()[1];
  const dragEvent = new Event("dragstart");
  dragEvent.dataTransfer = { effectAllowed: "", setData() {} };
  source.dispatchEvent(dragEvent);
  await settle();
  assert.ok(source.classList.contains("dragging"), "拖拽中的行带 dragging 态");
  targetRow.dispatchEvent(new Event("drop"));
  await settle();
  assert.equal(idOf(0), "c1", "拖拽放回后 c1 在前");
  assert.deepEqual(
    JSON.parse(globalThis.localStorage.store.get("courselens.course-order.v1")),
    ["c1", "c2"],
    "拖拽重排同样落存储",
  );
  assert.ok(!source.classList.contains("dragging"), "拖拽结束清理拖拽态");
  /* 清场：恢复目录原序，不影响后续场景 */
  globalThis.localStorage.store.delete("courselens.course-order.v1");
  renderCourses(store, catalogPayload);
  await settle();
  assert.equal(idOf(0), "c1", "清场后回到目录原序");
}

/* NIGHT4-W3（M14）同课次重进：退出选择态后重进同讲次必须响应。
   选择面上的点击永远是显式进入；学习桌内的同讲次重点仍不重发 loadAll。 */
{
  const fixtureFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => ok({});
  try {
    store.set("activeCourse", null);
    store.set("activeLecture", null);
    renderCourses(store, catalogPayload);
    await settle();
    mainOf(rows()[0]).click();
    await settle();
    const lectureRows = () => byId["study-lecture-list"].children.filter((node) => node.className === "lecture-row");
    assert.ok(lectureRows().length >= 1, "进入课程后讲次行渲染");
    let activeSets = 0;
    const offActive = store.subscribe("activeLecture", () => { activeSets += 1; });
    lectureRows()[0].click();
    await settle();
    assert.equal(activeSets, 1, "首次点击进入讲次");
    /* PLAYER-INTERACT-REPAIR-1 单元三：退出学习桌（返回课程与讲次）必须暂停
       播放（pause 事件链随即持久化进度）——绝不让音频在主界面继续后台播放。 */
    const playerStage = byId["player-stage"];
    playerStage.paused = false;
    playerStage.pause = () => { playerStage.paused = true; };
    byId["study-back-select"].click();
    await settle();
    assert.equal(playerStage.paused, true, "返回选择面即暂停（退出不停播）");
    /* M14 修复点：同讲次再次点击必须重新进入（修复前守卫直接 return，无响应） */
    lectureRows()[0].click();
    await settle();
    assert.equal(activeSets, 2, "退出选择态后重进同讲次必须响应（M14）");
    offActive();
  } finally {
    globalThis.fetch = fixtureFetch;
  }
  store.set("activeCourse", null);
  store.set("activeLecture", null);
  renderCourses(store, catalogPayload);
  await settle();
}

/* D-20261009-03 复点补链：重复点选当前已选课程（store.set 同引用去重、订阅面
   不通知）必须仍触发直播卡就地重查（courselens:live-refresh 补发）；换课程
   路径不补发（set 已通知订阅刷新，避免双请求）。FIRST-RUN-FINAL D-03 在
   「重复点选自动选中行」上把诚实 unknown 陈述误判为卡片死线——本钉同时
   保护：重查链不因去重而断、换课不双查、四类点击转移全部有确定语义。 */
{
  store.set("activeCourse", null);
  store.set("activeLecture", null);
  renderCourses(store, catalogPayload);
  await settle();
  let liveRefreshEvents = 0;
  const onLiveRefresh = () => { liveRefreshEvents += 1; };
  window.addEventListener("courselens:live-refresh", onLiveRefresh);
  try {
    mainOf(rows()[0]).click(); /* 首次点选 c1（此前 activeCourse=null）：订阅正常刷新，不补发 */
    await settle();
    assert.equal(liveRefreshEvents, 0, "首次选课不补发 live-refresh（订阅路径已刷新）");
    assert.equal(String(store.activeCourse?.course_id || ""), "c1", "首次选课写入 activeCourse");
    mainOf(rows()[0]).click(); /* 重复点选当前已选 c1：PF1 去重 → 补发重查 */
    await settle();
    assert.equal(liveRefreshEvents, 1, "重复点选已选课程补发 live-refresh（D-20261009-03 复点补链）");
    mainOf(rows()[1]).click(); /* 换选 c2：订阅正常刷新，不补发 */
    await settle();
    assert.equal(liveRefreshEvents, 1, "换课程路径不补发 live-refresh（避免双请求）");
    assert.equal(String(store.activeCourse?.course_id || ""), "c2", "换课写入新 activeCourse");
    mainOf(rows()[1]).click(); /* 换课后再重复点选 c2：同样补发（同一手势同一语义） */
    await settle();
    assert.equal(liveRefreshEvents, 2, "换课后的重复点选同样补发");
  } finally {
    window.removeEventListener("courselens:live-refresh", onLiveRefresh);
  }
  store.set("activeCourse", null);
  store.set("activeLecture", null);
  renderCourses(store, catalogPayload);
  await settle();
}
console.log("frontend course UI polish behavior passed");
