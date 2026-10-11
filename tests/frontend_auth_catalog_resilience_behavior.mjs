import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/* S04-B 前端行为锁（仿 tests/frontend_usability_auth_behavior.mjs 桩件法）：
 * 1. 手动刷新（courselens:catalog-refresh → 真实 load() 轮询）收到瞬时 checking
 *    空载荷时不得跳空屏；
 * 2. 瞬时 checking（auth 或 catalog 载荷）保留内存中同身份已验证列表；
 * 3. 确认过期（action_required/fudan_login_required）立即清空；
 * 4. 课程内容绝不落 localStorage；
 * 5. S05-A：就绪非空目录的学期视图过滤未命中 → 可见回落全部学期并保留
 *    选课/讲次/学习桌，绝不塌回开始页；识别到当前学期标签时保持精确过滤；
 *    真实空目录与视图过滤未命中是两个状态；自动回落不落盘。 */

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
    this.open = false;
    this.style = {};
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
    return true;
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  prepend(...nodes) { for (const node of nodes) { node.parent = this; this.children.unshift(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() {
    if (this.parent) {
      const index = this.parent.children.indexOf(this);
      if (index >= 0) this.parent.children.splice(index, 1);
      this.parent = null;
    }
  }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
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
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function elementMatches(node, selector) {
  const text = String(selector);
  if (text.startsWith(".")) {
    return String(node.className || "").split(/\s+/).includes(text.slice(1));
  }
  if (text.startsWith("[")) {
    const name = text.slice(1, -1);
    if (name.startsWith("data-")) {
      const key = String(name).replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
      return node.dataset?.[key] !== undefined;
    }
    return node.attributes.has(name);
  }
  return node.tagName === text.toUpperCase();
}

const BUTTON_IDS = [
  "study-back-courses", "study-back-select", "study-start-select", "refresh-catalog",
  "copy-catalog-diagnostics", "reload-transcript", "generate-quiz", "analyze-concepts",
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
];
const byId = Object.fromEntries([
  ...IDS.map((id) => [id, new FakeElement(BUTTON_IDS.includes(id) ? "button" : "div", id)]),
  ["catalog-term-filter", new FakeElement("select", "catalog-term-filter")],
  ["artifact-kind", new FakeElement("select", "artifact-kind")],
  ["document-input", new FakeElement("input", "document-input")],
]);
const named = {
  lecturePane: new FakeElement("section", "lecture-pane-fake"),
  materialsTabs: new FakeElement("div", "materials-tabs-fake"),
};
named.lecturePane.className = "lecture-pane";
named.materialsTabs.className = "materials-tabs";

let createdSeq = 0;
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${createdSeq += 1}`),
  createElementNS: (_ns, tag) => new FakeElement(tag, `created-ns-${createdSeq += 1}`),
  querySelector: (selector) => {
    if (String(selector).includes(".lecture-pane")) return named.lecturePane;
    if (String(selector).includes(".materials-tabs")) return named.materialsTabs;
    return null;
  },
  querySelectorAll: () => [],
  activeElement: null,
  documentElement: { dataset: {} },
});
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

/* ---- 网络桩：catalog 载荷可由用例改写 ---- */

let catalogPayload = null;
let catalogFetches = 0;
globalThis.fetch = async (_path) => {
  catalogFetches += 1;
  return {
    ok: true,
    headers: { get: () => "application/json" },
    json: async () => ({ schema: "courselens.api.v3", data: catalogPayload }),
  };
};

const { store } = await import("../frontend/modules/store.js");
const { installStudy } = await import("../frontend/modules/study.js");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 4; index += 1) await nextTurn();
};

const courseRows = () => byId["study-course-list"].children.filter((node) => node.className === "course-row");
const courseIds = () => courseRows().map((row) => row.querySelector(".course-row-main")?.dataset.courseId);

const cleanup = await installStudy(store);

const twoCourses = [
  {
    course_id: "c1", title: "课程一", teacher: "教师甲", term: "2026-2027学年1",
    department: "系A", authorization_state: "verified",
    lectures: [{ sub_id: "s1", sub_title: "第1讲", date: "09-01", can_stream: true }],
  },
  {
    course_id: "c2", title: "课程二", teacher: "教师乙", term: "2026-2027学年1",
    department: "系B", authorization_state: "verified",
    lectures: [{ sub_id: "s2", sub_title: "第2讲", date: "09-02", can_stream: true }],
  },
];

/* 场景 0：就绪登录 → 已验证列表渲染 */
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 2, courses: twoCourses,
};
store.set("auth", {
  state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"],
  connected: true, configured: true,
});
await settle();
assert.equal(courseRows().length, 2, "就绪态必须渲染已验证课程");
assert.deepEqual(store.courses.map((course) => course.course_id), ["c1", "c2"]);

/* 场景 1：auth 瞬时 checking（校验在途/复用窗口重验）→ 保留已验证列表。
   AS4-U1：恢复期目录探测照常发出——后端 checking 信封回放本机缓存（P2-C 真实
   形状），缓存课程即刻上屏；恢复面板副文案改说「先显示上次缓存的课程」。 */
catalogPayload = {
  state: "checking", code: "fudan_session_checking", actions: [],
  refreshing: true, course_count: 2, courses: twoCourses,
};
const fetchesBeforeChecking = catalogFetches;
store.set("auth", {
  state: "checking", code: "fudan_session_checking", actions: [],
  connected: false, configured: true,
});
await settle();
assert.ok(catalogFetches > fetchesBeforeChecking, "AS4-U1：checking 期目录装载照常发出（不再等 ready 门）");
assert.equal(courseRows().length, 2, "瞬时 checking 不得清空已验证课程列表");
assert.equal(byId["catalog-recovery"].hidden, false, "checking 态恢复面板可见");
assert.equal(byId["catalog-recovery-title"].textContent, "正在验证复旦会话");
assert.match(
  byId["catalog-recovery-impact"].textContent,
  /先显示上次缓存的课程/,
  "缓存上屏时的诚实副文案（AS4-U1）",
);
assert.equal(byId["catalog-recovery-actions"].children.length, 0, "瞬时态不引导重登");

/* 场景 2：手动刷新走真实 load() 轮询：checking 空载荷不跳空屏 */
catalogPayload = {
  state: "checking", code: "fudan_session_checking", actions: [],
  refreshing: true, course_count: null, courses: [],
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(courseRows().length, 2, "刷新返回瞬时 checking 空载荷时必须保留已验证列表");
assert.deepEqual(courseIds(), ["c1", "c2"]);
assert.equal(store.courses.length, 2, "store 内存列表保持已验证课程");
assert.equal(
  byId["study-course-list"].children.some((node) => String(node.textContent || "").includes("正在确认课程授权")),
  false,
  "有已验证列表时不得渲染空屏文案",
);

/* 场景 3：确认过期（NAV-HANG-1 合同修订）——auth 翻转（configured=true，
   本地凭据仍在的上游抖动）不再清列表（2026-10-02 16:52 案例根因：旧链在
   此弹桌+按钮换名失灵）；列表清空改由 server 确认载荷（catalog 轮询返回
   action_required+空）承担——见下方 catalog-refresh 双路径。 */
catalogPayload = {
  state: "action_required", code: "fudan_login_required", actions: ["login"],
  refreshing: false, course_count: null, courses: [],
};
store.set("auth", {
  state: "action_required", code: "fudan_login_required", actions: ["login"],
  connected: false, configured: true,
});
await settle();
assert.equal(courseRows().length, 2, "确认过期 auth 翻转（configured=true）保留已验证列表");
assert.equal(store.courses.length, 2, "store 课程列表保持（上游抖动不清）");
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(courseRows().length, 0, "server 确认载荷（action_required+空）到达即清空");
assert.equal(byId["catalog-recovery-title"].textContent, "需要登录复旦课程平台");
assert.ok(byId["catalog-recovery-actions"].children.length >= 1, "确认态提供重登动作");

/* 场景 4：降级确认（会话过期）——auth 翻转保留、server 载荷清空（同场景 3 双路径） */
catalogPayload = {
  state: "action_required", code: "fudan_session_expired", actions: ["login"],
  refreshing: false, course_count: null, courses: [],
};
store.set("auth", {
  state: "degraded", code: "fudan_session_expired", actions: ["login"],
  connected: false, configured: true,
});
await settle();
assert.equal(courseRows().length, 0, "场景 3 已清空后保持空列表（auth 翻转不复活）");
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(courseRows().length, 0, "会话过期 server 载荷到达仍清空");
store.set("courses", [
  { course_id: "c1", title: "课程一", lectures: [] },
  { course_id: "c2", title: "课程二", lectures: [] },
]);
await settle();

/* 场景 5：课程内容绝不写入 localStorage（仅允许主题/学期过滤键） */
for (const key of globalThis.localStorage.store.keys()) {
  assert.match(String(key), /^courselens\.(theme|catalog-term|last-lecture)/, `localStorage 仅允许主题/学期过滤/继续学习记忆键，实际: ${key}`);
}

/* ---- S05-A：就绪非空目录的学期视图稳定性 ----
 * 学期夹具按运行期日期推导（与 frontend_home_ux_behavior 同法）：
 * 过去学年标签恒不含当前学年字符串，因此对 currentCourseTerm 恒不可识别；
 * currentTermFixture 恒被识别为当前学期。 */

const now = new Date();
const academicStart = now.getMonth() >= 7 ? now.getFullYear() : now.getFullYear() - 1;
const currentSemester = now.getMonth() >= 7 || now.getMonth() === 0 ? "1" : "2";
const currentTermFixture = `${academicStart}-${academicStart + 1}-${currentSemester}`;
const otherTermFixture = `${academicStart}-${academicStart + 1}-${currentSemester === "1" ? "2" : "1"}`;
const pastTerms = [`${academicStart - 1}-${academicStart}-1`, `${academicStart - 1}-${academicStart}-2`];

const twentyOneCourses = Array.from({ length: 21 }, (_, index) => ({
  course_id: `p${String(index + 1).padStart(2, "0")}`,
  title: `历史学期课程${index + 1}`,
  teacher: `教师${(index % 3) + 1}`,
  term: pastTerms[index % 2],
  department: "历史学期",
  authorization_state: "verified",
  lectures: [{ sub_id: `p${String(index + 1).padStart(2, "0")}-s1`, sub_title: "第1讲", date: "03-01", can_stream: true }],
}));
const termCourses = [
  { course_id: "cur-1", title: "当前学期课程一", teacher: "教师甲", term: currentTermFixture, department: "系A", authorization_state: "verified", lectures: [{ sub_id: "cur-1-s1", sub_title: "第1讲", can_stream: true }] },
  { course_id: "cur-2", title: "当前学期课程二", teacher: "教师乙", term: currentTermFixture, department: "系B", authorization_state: "verified", lectures: [{ sub_id: "cur-2-s1", sub_title: "第1讲", can_stream: true }] },
  { course_id: "other-1", title: "另一学期课程", teacher: "教师丙", term: otherTermFixture, department: "系C", authorization_state: "verified", lectures: [{ sub_id: "other-1-s1", sub_title: "第1讲", can_stream: true }] },
];

const termSelect = byId["catalog-term-filter"];
const deepText = (node) => [String(node.textContent || ""), ...(node.children || []).map((child) => deepText(child))].join("");
const lecturePaneText = () => byId["study-lecture-list"].children.map((node) => deepText(node)).join("\n");
const pickTermFilter = (value) => {
  termSelect.value = value;
  termSelect.dispatchEvent(new Event("change"));
};

/* 场景 6：auth ready + 21 门非空课程 + 当前学期标签不可识别 → 可见自动回落全部学期 */
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 21, courses: twentyOneCourses,
};
store.set("auth", {
  state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog"],
  connected: true, configured: true,
});
await settle();
assert.equal(courseRows().length, 21, "视图未命中必须回落全部学期并渲染全部课程");
assert.equal(termSelect.value, "all", "自动回落必须可见：select 显示全部学期");
assert.equal(termSelect.children.find((option) => option.value === "all")?.text, "全部学期", "回落选项文案为全部学期");
assert.deepEqual(store.courses.map((course) => course.course_id), twentyOneCourses.map((course) => course.course_id), "store 保留完整已验证目录");
assert.equal(
  byId["study-course-list"].children.some((node) => String(node.textContent || "").includes("没有课程")),
  false,
  "视图未命中不得渲染空态文案",
);
/* SMALL-POLISH-1② 启动落地门：auth ready + 目录到达仍落问候/欢迎面（学习页
 * 起始面），课程列表已在背后按全部学期渲染完毕；显式进入选择面后回落可见。 */
assert.equal(byId["study-empty"].hidden, false, "启动落地门：auth ready 目录到达不自动离开问候面");

/* 场景 7：已有选课/讲次 → 多次相同 ready 刷新幂等（不重建、不丢选择、不跳开始页） */
courseRows()[6].querySelector(".course-row-main").click(); /* 行壳非交互：点导航按钮 */
byId["study-lecture-list"].children.find((node) => node.className === "lecture-row")?.click();
await settle();
const selectedCourse = store.activeCourse;
const selectedLecture = store.activeLecture;
assert.equal(selectedCourse?.course_id, "p07", "点击课程后选中生效");
assert.ok(selectedLecture, "点击讲次后讲次生效");
assert.equal(byId["study-desk"].hidden, false, "选中讲次后进入学习桌");
assert.equal(byId["study-empty"].hidden, true);
const rowSnapshot = courseRows();
for (let index = 0; index < 3; index += 1) {
  window.dispatchEvent(new Event("courselens:catalog-refresh"));
  await settle();
}
assert.equal(courseRows()[6], rowSnapshot[6], "相同 ready 载荷不得重建课程列表（无焦点破坏）");
assert.equal(store.activeCourse, selectedCourse, "相同 ready 刷新不得丢失选课");
assert.equal(store.activeLecture?.sub_id, selectedLecture.sub_id, "相同 ready 刷新不得丢失讲次");
assert.equal(byId["study-desk"].hidden, false, "相同 ready 刷新不得离开学习桌");
assert.equal(byId["study-empty"].hidden, true, "相同 ready 刷新不得塌回开始页");
assert.equal(termSelect.value, "all");
/* 载荷变化（code 不同）触发重建：选择/讲次/学习桌/视图仍必须保留 */
catalogPayload = { ...catalogPayload, code: "authorized_catalog_stale" };
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.notEqual(courseRows()[6], rowSnapshot[6], "载荷变化允许重建列表");
assert.equal(courseRows().length, 21);
assert.equal(store.activeCourse, selectedCourse, "重建后选课保留");
assert.equal(store.activeLecture?.sub_id, selectedLecture.sub_id, "重建后讲次保留");
assert.equal(byId["study-desk"].hidden, false, "重建后仍在学习桌");
assert.equal(termSelect.value, "all", "重建后回落视图保持全部学期");

/* 场景 8：用户显式切回当前学期（仍无可识别标签）→ 再次自动回落且选课/讲次/学习桌保留 */
pickTermFilter("current");
await settle();
assert.equal(termSelect.value, "all", "再次自动回落可见：select 显示全部学期");
assert.equal(store.activeCourse, selectedCourse, "视图回落不得清除选课");
assert.equal(store.activeLecture?.sub_id, selectedLecture.sub_id, "视图回落不得清除讲次");
assert.equal(byId["study-desk"].hidden, false, "视图回落不得塌回开始页");
assert.equal(courseRows().length, 21);

/* 场景 9：识别到当前学期标签 → 保持当前学期精确过滤，不触发回落 */
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 3, courses: termCourses,
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(courseRows().length, 3, "全部学期视图渲染全部课程");
pickTermFilter("current");
await settle();
assert.equal(termSelect.value, "current", "识别到当前学期时保持当前学期视图");
assert.deepEqual(courseIds(), ["cur-1", "cur-2"], "当前学期过滤精确保留");
assert.equal(store.activeCourse?.course_id, "cur-1", "当前学期视图高亮过滤结果首门课");

/* ---- S06-A：紧凑学期码（YYYY-YYYY1 / YYYY-YYYY2，如 2026-20271）---- */
const compactCurrentTerm = `${academicStart}-${academicStart + 1}${currentSemester}`;
const compactOtherTerm = `${academicStart}-${academicStart + 1}${currentSemester === "1" ? "2" : "1"}`;

/* 场景 9a：同学年紧凑码但学期后缀不匹配当前时钟 → 不发明匹配，显式选当前学期仍可见回落 */
const compactOtherOnly = [
  { course_id: "cs-1", title: "紧凑另一学期课程", teacher: "教师丁", term: compactOtherTerm, department: "系D", authorization_state: "verified", lectures: [{ sub_id: "cs-1-s1", sub_title: "第1讲", can_stream: true }] },
];
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 1, courses: compactOtherOnly,
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
pickTermFilter("current");
await settle();
assert.equal(termSelect.value, "all", "未知紧凑后缀不发明当前学期：可见回落全部学期");
assert.deepEqual(courseIds(), ["cs-1"], "回落视图渲染紧凑另一学期课程");

/* 场景 9b：紧凑当前学期码被识别 → 选择器保持当前学期并精确过滤，不触发回落 */
const compactCourses = [
  { course_id: "cf-1", title: "紧凑当前学期课程一", teacher: "教师甲", term: compactCurrentTerm, department: "系E", authorization_state: "verified", lectures: [{ sub_id: "cf-1-s1", sub_title: "第1讲", can_stream: true }] },
  { course_id: "cf-2", title: "紧凑当前学期课程二", teacher: "教师乙", term: compactCurrentTerm, department: "系F", authorization_state: "verified", lectures: [{ sub_id: "cf-2-s1", sub_title: "第2讲", can_stream: true }] },
  { course_id: "co-1", title: "紧凑另一学期课程", teacher: "教师丙", term: compactOtherTerm, department: "系G", authorization_state: "verified", lectures: [{ sub_id: "co-1-s1", sub_title: "第1讲", can_stream: true }] },
];
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 3, courses: compactCourses,
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
pickTermFilter("current");
await settle();
assert.equal(termSelect.value, "current", "紧凑当前学期码被识别：选择器保持当前学期");
assert.deepEqual(courseIds(), ["cf-1", "cf-2"], "当前学期视图只渲染紧凑学期码课程");
assert.equal(store.activeCourse?.course_id, "cf-1", "紧凑学期码视图同样回退高亮首门课");

/* 场景 10：真实已验证空目录 ≠ 视图过滤未命中：清空 + 开始引导 + 讲次选择引导 */
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 0, courses: [],
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(store.courses.length, 0, "真实空目录 store 清空");
assert.equal(store.activeCourse, null, "真实空目录清空选课");
assert.equal(store.activeLecture, null, "真实空目录清空讲次");
assert.equal(byId["study-empty"].hidden, false, "真实空目录显示开始引导");
assert.ok(
  byId["study-course-list"].children.some((node) => deepText(node).includes("暂无可显示课程")),
  "真实空目录文案区别于视图过滤未命中",
);
assert.equal(lecturePaneText().includes("暂无已授权讲次"), false, "未选课程不得暗示讲次为零");
assert.match(lecturePaneText(), /选择课程后显示讲次/, "未选课程给选择引导");
assert.equal(termSelect.value, "current", "空目录不触发学期回落");

/* 场景 10a（EMPTY-STATES-1）：ready 空目录空态带「刷新课程目录」真动作，
   点击走既有 recovery 链路（refresh-catalog 同一入口），不另造刷新通道。 */
{
  const catalogEmptyAction = byId["study-course-list"].querySelector(".empty-action");
  assert.ok(catalogEmptyAction, "真实空目录空态带「刷新课程目录」动作钮");
  assert.equal(String(catalogEmptyAction.textContent || ""), "刷新课程目录", "空态动作钮文案闭集");
  const catalogRefreshSpy = [];
  byId["refresh-catalog"].addEventListener("click", () => catalogRefreshSpy.push(1));
  catalogEmptyAction.click();
  assert.equal(catalogRefreshSpy.length, 1, "空目录动作触发既有目录刷新链路");
}

/* 场景 10b（EMPTY-STATES-1）：已选课程但讲次为零 → 诚实空态 + 同一刷新动作；
   点击实证复用 refresh-catalog 链路。 */
catalogPayload = {
  state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
  refreshing: false, course_count: 1,
  courses: [
    { course_id: "cx-1", title: "空讲次课程", teacher: "教师戊", term: compactCurrentTerm, department: "系H", authorization_state: "verified", lectures: [] },
  ],
};
window.dispatchEvent(new Event("courselens:catalog-refresh"));
await settle();
assert.equal(store.activeCourse?.course_id, "cx-1", "单门课自动回退高亮");
assert.match(lecturePaneText(), /暂无已授权讲次/, "已选课程讲次为零给诚实空态");
{
  const lectureEmptyAction = byId["study-lecture-list"].querySelector(".empty-action");
  assert.ok(lectureEmptyAction, "讲次空态带「刷新课程目录」动作钮");
  assert.equal(String(lectureEmptyAction.textContent || ""), "刷新课程目录", "讲次空态动作钮文案闭集");
  const lectureRefreshSpy = [];
  byId["refresh-catalog"].addEventListener("click", () => lectureRefreshSpy.push(1));
  lectureEmptyAction.click();
  assert.equal(lectureRefreshSpy.length, 1, "讲次空态动作触发既有目录刷新链路");
}

/* 场景 11：自动回落不落盘——存储里只有用户显式选择（场景 8/9 的 change 事件写入） */
assert.equal(localStorage.getItem("courselens.catalog-term.v1"), "current", "自动回落绝不写 localStorage，仅保留用户显式选择");
for (const key of globalThis.localStorage.store.keys()) {
  assert.match(String(key), /^courselens\.(theme|catalog-term|last-lecture)/, `localStorage 仅允许主题/学期过滤/继续学习记忆键，实际: ${key}`);
}

/* 场景 12：auth 载荷附带 courselens.vpn-connection.v1 连接快照（附加字段）→
   目录行为不变；消费端纯函数对 V0 fixture/未知字段/未知 schema 的处理闭集 */
{
  const fixture = JSON.parse(
    readFileSync(new URL("./fixtures/vpn_connection_contract_v1.json", import.meta.url), "utf8"),
  ).valid;
  const { extractConnectionSnapshot, connectionViewFromSnapshot, connectionViewFromAuth,
    connectionCodeHint, CONNECTION_STATE_TEXT, CONNECTION_ACTION_LABELS } = await import("../frontend/modules/ui.js");

  catalogPayload = {
    state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"],
    refreshing: false, course_count: 2, courses: twoCourses,
  };
  /* 附加快照（含未知字段——后端向前演进时前端目录行为不得被扰动） */
  store.set("auth", {
    state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"],
    connected: true, configured: true,
    connection: { ...fixture.ready_direct, future_field: { detail: "ignored" } },
  });
  await settle();
  assert.equal(courseRows().length, 2, "附加连接快照不改变已验证课程列表");
  assert.deepEqual(store.courses.map((course) => course.course_id), ["c1", "c2"], "课程数据不变");

  /* 消费端纯函数：按 schema 字符串定位快照（不发明字段名），v2/缺失按无快照 */
  const payload = { state: "ready", connection: fixture.degraded_proxy_fallback };
  assert.equal(extractConnectionSnapshot(payload), payload.connection, "按 schema 找到附加快照");
  assert.equal(extractConnectionSnapshot({ state: "ready" }), null, "无快照返回 null");
  assert.equal(
    extractConnectionSnapshot({ connection: { ...fixture.ready_direct, schema: "courselens.vpn-connection.v2" } }),
    null,
    "v2 快照被 v1 消费端拒绝",
  );

  const view = connectionViewFromSnapshot(fixture.degraded_proxy_fallback, 1789200000 + 30);
  assert.equal(view.state, "degraded", "fixture degraded 状态映射");
  assert.deepEqual([...view.actions], ["retry", "open-settings"], "动作按后端优先级原序保留");
  assert.equal(view.hint, "", "proxy_fallback 无 TUN 提示");
  assert.equal(
    connectionViewFromSnapshot(fixture.login_required, 1789200000 + 30).state,
    "login_required",
    "fixture login_required 状态映射",
  );

  /* 九个契约状态全部有中性中文文案；六个动作全部有标签（文案闭集锁） */
  for (const state of ["off", "checking", "ready", "login_required", "reauthenticating",
    "network_unavailable", "challenge_required", "expired", "degraded"]) {
    assert.ok(CONNECTION_STATE_TEXT[state], `状态 ${state} 有文案`);
    assert.ok(!CONNECTION_STATE_TEXT[state].includes("TUN"), `状态 ${state} 文案中性无 TUN`);
  }
  for (const action of ["login", "reauthenticate", "check-network", "retry", "open-settings", "close-tun-and-retry"]) {
    assert.ok(CONNECTION_ACTION_LABELS[action], `动作 ${action} 有标签`);
  }

  /* 消费端时效与降级：过期 → 中性复检态；未知枚举 → 中性兜底 */
  const staleView = connectionViewFromSnapshot(fixture.ready_direct, 1789200000 + 400);
  assert.equal(staleView.state, "stale", ">300s 快照视为过期");
  assert.equal(staleView.stale, true, "stale 标记");
  const unknownView = connectionViewFromSnapshot({
    ...fixture.ready_direct, state: "maintenance", reason: "warp_field", actions: ["purge-cache", "retry"],
  }, 1789200000 + 30);
  assert.equal(unknownView.state, "unknown_state", "未知 state 降级");
  assert.deepEqual([...unknownView.actions], ["retry"], "未知动作被过滤，已知动作保留原序");
  assert.equal(unknownView.hint, "", "未知 reason 无提示");

  /* 后端未发布快照时的 auth 派生闭集 */
  assert.equal(connectionViewFromAuth({ state: "ready" }).state, "ready", "派生 ready");
  assert.equal(connectionViewFromAuth({ state: "checking" }).state, "checking", "派生 checking");
  assert.equal(connectionViewFromAuth({ state: "degraded" }).state, "degraded", "派生 degraded");
  assert.equal(connectionViewFromAuth({ state: "action_required", code: "fudan_session_expired" }).state, "expired", "派生 expired");
  assert.equal(connectionViewFromAuth({ state: "action_required", code: "fudan_credentials_missing" }).state, "login_required", "派生 login_required");
  assert.equal(connectionViewFromAuth(null).state, "login_required", "空 auth 诚实落入需登录");

  /* VPN-P1-RECOVERY-1（P1-A）：闭集错误码细分文案；未知/健康代码一律空串 */
  assert.equal(
    connectionCodeHint("fudan_challenge_required"),
    "需要在复旦页面完成安全验证：请登录并按页面提示完成验证。",
    "挑战专属文案",
  );
  assert.ok(connectionCodeHint("fudan_account_locked").includes("解除锁定"), "锁定文案指向解锁");
  assert.ok(connectionCodeHint("fudan_service_maintenance").includes("稍后重试"), "维护文案指向稍后重试");
  assert.ok(connectionCodeHint("fudan_credentials_rejected").includes("核对"), "凭据文案指向核对");
  for (const safe of ["fudan_session_verified", "fudan_session_checking", "mystery_future_code", ""]) {
    assert.equal(connectionCodeHint(safe), "", `非失败代码 ${JSON.stringify(safe)} 无提示（健康安静）`);
  }
}

cleanup();

console.log("frontend_auth_catalog_resilience_behavior: all assertions passed (S04-B preserve/clear + S05-A term-filter fallback semantics)");
