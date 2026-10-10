import assert from "node:assert/strict";

/* 统一文件中心行为测试（DATA-DELETE-REPAIR-1）：加载真实 study.js，用最小假
   DOM 与可注入网络桩覆盖资料 tab 三类条目（导入/课件/总结）按讲次聚合渲染、
   每条「导出/删除」两钮契约（a[download] 流式导出、两击臂危险确认、总结删除
   人话 toast）、空态与失败降级。 */

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
    this.value = "";
    this.tabIndex = -1;
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
      if (!node) continue;
      node.parent = this;
      this.children.push(node);
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

/* ---- 可注入网络桩 ---- */

const ok = (data, status = 200) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
  status, headers: { "content-type": "application/json" },
});
const errorResponse = (code, status = 404) => new Response(JSON.stringify({ error: code, error_code: code }), {
  status, headers: { "content-type": "application/json" },
});

let materialsEntries = [];
let materialsShouldFail = false;
const requestMeta = []; /* { route, method, body } */

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options?.method || "GET");
  const body = typeof options?.body === "string" ? options.body : "";
  requestMeta.push({ route, method, body });
  if (route.startsWith("/api/v3/materials/actions")) {
    const parsed = JSON.parse(body || "{}");
    if (parsed.action !== "delete") return errorResponse("materials_action_invalid", 400);
    const before = materialsEntries.length;
    materialsEntries = materialsEntries.filter((entry) => entry.id !== parsed.id);
    if (materialsEntries.length === before) return errorResponse("materials_entry_missing", 404);
    return ok({ deleted: true, kind: parsed.kind, id: parsed.id });
  }
  if (route.startsWith("/api/v3/materials")) {
    if (materialsShouldFail) return errorResponse("runtime_failed", 500);
    return ok({ course_id: "c1", entries: materialsEntries, scan_truncated: false });
  }
  if (route.startsWith("/api/v3/subtitles/segments")) return ok({ segments: [] });
  if (route.startsWith("/api/v3/artifacts")) return ok({ artifact: null }); /* C3：合同改 200 空载荷 */
  if (route.startsWith("/api/v3/documents")) return ok({ documents: [] });
  if (route.startsWith("/api/v3/quizzes")) return ok({ items: [] });
  if (route === "/api/v3/review-plans") return ok({ plans: [] });
  if (route.startsWith("/api/v3/concepts")) return ok({ concepts: [], edges: [] });
  if (route === "/api/v3/analytics") return ok({ summary: {} });
  if (route.startsWith("/api/v3/bookmarks")) return ok({ bookmarks: [] });
  if (route.startsWith("/api/v3/courseware-pdf")) return errorResponse("courseware_pdf_unavailable");
  if (route.startsWith("/api/v3/tasks")) return ok({ tasks: [], counts: { active: 0, failed: 0, completed: 0 } });
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

/* ---- 安装真实模块 ---- */

const { store } = await import("../frontend/modules/store.js");
const { installStudy } = await import("../frontend/modules/study.js");

const LECTURE = { course_id: "c1", sub_id: "s1", course_title: "课程甲·高分子化学导论", sub_title: "第一讲" };
store.courses = [
  { course_id: "c1", title: "课程甲·高分子化学导论", lectures: [LECTURE] },
];
await installStudy(store);
await settle();

const el = (id) => document.getElementById(id);
const textOf = (node) => {
  if (!node || node.hidden) return "";
  return [String(node.textContent || ""), ...(node.children || []).map((child) => textOf(child))].join("");
};
const rowsOf = () => (el("document-list").children || []).filter((node) => node.className === "item-row");
const lastBody = () => JSON.parse(requestMeta.filter((item) => item.method === "POST").at(-1)?.body || "{}");

/* 1) 三类条目按讲次聚合：讲次头 + 行（名称+类型小标签+大小），导出带
   a[download] 流式地址，删除为 danger 档按钮。 */
materialsEntries = [
  { kind: "summary", id: "sum-aaaaaaaaaaaaaaaa", sub_id: "s1", course_id: "c1", name: "总结-2026-09-01-第一讲", download_name: "总结-2026-09-01-第一讲.md", size: 2048, updated_at: 3, lecture_title: "第一讲", lecture_date: "2026-09-01" },
  { kind: "document", id: "doc-round-trip", sub_id: "s1", course_id: "c1", name: "课堂讲义", download_name: "课堂讲义.pdf", size: 512000, updated_at: 2, lecture_title: "第一讲", lecture_date: "2026-09-01" },
  { kind: "courseware", id: "lec-0123456789abcdef", sub_id: "s1", course_id: "c1", name: "2026-09-01-第一讲.pdf", download_name: "2026-09-01-第一讲.pdf", size: 1048576, updated_at: 1, lecture_title: "第一讲", lecture_date: "2026-09-01" },
];
store.set("activeLecture", { ...LECTURE });
await settle();

const rows = rowsOf();
assert.equal(rows.length, 3, "三条资料各占一行（不卡片化）");
const rowText = rows.map((row) => textOf(row)).join("|");
assert.match(rowText, /课堂讲义/, "导入资料行可见");
assert.match(rowText, /2026-09-01-第一讲\.pdf/, "课件行可见");
assert.match(rowText, /总结-2026-09-01-第一讲/, "总结行可见");
assert.match(rowText, /导入/, "类型标签：导入");
assert.match(rowText, /课件/, "类型标签：课件");
assert.match(rowText, /总结/, "类型标签：总结");
assert.match(textOf(el("document-list")), /2026-09-01 第一讲/, "讲次聚合头可见");
const exportLink = rows[0].children.flatMap((node) => node.children || []).find((node) => node.tagName === "A");
assert.ok(exportLink, "导出为链接（系统另存对话框）");
assert.equal(exportLink.getAttribute("download"), downloadNameOf(rows, 0), "导出文件名随条目");
assert.ok(String(exportLink.getAttribute("href")).startsWith("/api/v3/materials/file?kind="), "导出走流式文件路由");
const deleteButton = rows[0].children.flatMap((node) => node.children || []).find((node) => node.tagName === "BUTTON");
assert.ok(deleteButton, "删除按钮存在");
assert.equal(deleteButton.className, "danger", "删除按钮 danger 危险分级");
assert.equal(deleteButton.textContent, "删除");

function downloadNameOf(rows, index) {
  const link = rows[index].children.flatMap((node) => node.children || []).find((node) => node.tagName === "A");
  return link?.getAttribute("download") || "";
}

/* 2) 两击臂删除：首击进入确认态，再击提交 delete，总结删除落人话 toast，
   删除后局部刷新（不整页重载）。 */
const summaryRow = rows.find((row) => textOf(row).includes("总结-"));
const summaryDelete = summaryRow.children.flatMap((node) => node.children || []).find((node) => node.tagName === "BUTTON");
summaryDelete.click();
assert.equal(summaryDelete.textContent, "再点一次确认", "首击进入确认态");
summaryDelete.click();
await settle();
const deleteBody = lastBody();
assert.equal(deleteBody.action, "delete");
assert.equal(deleteBody.kind, "summary");
assert.equal(deleteBody.id, "sum-aaaaaaaaaaaaaaaa");
assert.match(textOf(el("toast-region")), /已删除总结文件，学习数据不受影响/, "总结删除 toast 讲清学习数据不受影响");
assert.equal(rowsOf().length, 2, "删除后局部刷新，剩余两条");
assert.ok(!textOf(el("document-list")).includes("总结-2026-09-01-第一讲"), "被删条目从列表消失");

/* 3) 课件删除 toast 语义：可再生。 */
const coursewareRow = rowsOf().find((row) => textOf(row).includes("lec-") || textOf(row).includes("2026-09-01-第一讲.pdf"));
const coursewareDelete = coursewareRow.children.flatMap((node) => node.children || []).find((node) => node.tagName === "BUTTON");
coursewareDelete.click();
coursewareDelete.click();
await settle();
assert.match(textOf(el("toast-region")), /之后可以重新生成/, "课件删除提示可重新生成");
assert.equal(rowsOf().length, 1, "仅剩导入资料一条");

/* 4) 空态：讲人话，告诉学生资料会自动出现。 */
materialsEntries = [];
store.set("activeLecture", null);
await settle();
store.set("activeLecture", { ...LECTURE });
await settle();
assert.match(textOf(el("document-list")), /自动出现在这里/, "空态解释资料从哪来");

/* 5) 失败降级：materials 读失败落错误空态，不滞留旧列表。 */
materialsShouldFail = true;
store.set("activeLecture", null);
await settle();
store.set("activeLecture", { ...LECTURE });
await settle();
assert.match(textOf(el("document-list")), /课程资料暂时无法加载/, "失败闭集文案");

/* 5b（EMPTY-STATES-1）：失败空态带「重试」真动作——点击重跑同一条装载链路，成功即恢复。 */
{
  const materialsRetry = el("document-list").querySelector(".empty-action");
  assert.ok(materialsRetry, "资料失败空态带「重试」动作钮");
  assert.equal(String(materialsRetry.textContent || ""), "重试", "重试钮文案闭集");
  materialsShouldFail = false;
  materialsEntries = [
    { kind: "document", id: "doc-retry-a", sub_id: "s1", course_id: "c1", name: "重试讲义", download_name: "重试讲义.pdf", size: 100, lecture_title: "第一讲", lecture_date: "2026-09-01" },
  ];
  materialsRetry.click();
  await settle();
  assert.equal(rowsOf().length, 1, "重试成功后资料列表恢复");
}

/* 6) 甲-2 控件归位：筛选默认「全部类型」；按类型筛选只留对应行；空态
   收形（data-empty=true：筛选与课程级选项退场，导入钮升为主操作）。 */
assert.equal(el("document-kind-filter").value || "all", "all", "筛选默认全部类型");
materialsShouldFail = false;
materialsEntries = [
  { kind: "document", id: "doc-filter-a", sub_id: "s1", course_id: "c1", name: "筛选讲义", download_name: "筛选讲义.pdf", size: 100, lecture_title: "第一讲", lecture_date: "2026-09-01" },
  { kind: "courseware", id: "cw-filter-a", sub_id: "s1", course_id: "c1", name: "筛选课件.pdf", download_name: "筛选课件.pdf", size: 200, lecture_title: "第一讲", lecture_date: "2026-09-01" },
];
store.set("activeLecture", null);
await settle();
store.set("activeLecture", { ...LECTURE });
await settle();
assert.equal(el("imported-documents").dataset.empty, "false", "有资料时导入流程全量在位");
el("document-kind-filter").value = "courseware";
el("document-kind-filter").dispatchEvent(new Event("change"));
await settle();
const filteredRows = rowsOf();
assert.equal(filteredRows.length, 1, "课件筛选只剩课件行");
assert.match(textOf(filteredRows[0]), /筛选课件\.pdf/, "筛选结果正确");
el("document-kind-filter").value = "summary";
el("document-kind-filter").dispatchEvent(new Event("change"));
await settle();
assert.match(textOf(el("document-list")), /这个类型下暂无资料/, "筛选空类给短句空态");
assert.equal(el("imported-documents").dataset.empty, "true", "筛选空类同样收形");

/* 6b（EMPTY-STATES-1）：筛选空类带「查看全部类型」真动作——原位切回并复渲染，不重新拉网。 */
{
  const emptyKindAction = el("document-list").querySelector(".empty-action");
  assert.ok(emptyKindAction, "筛选空类空态带「查看全部类型」动作钮");
  assert.equal(String(emptyKindAction.textContent || ""), "查看全部类型", "动作钮文案闭集");
  emptyKindAction.click();
  await settle();
  assert.equal(el("document-kind-filter").value, "all", "动作原位切回全部类型");
  assert.equal(rowsOf().length, 2, "切回后两条资料俱在");
  assert.equal(el("imported-documents").dataset.empty, "false", "切回后收形标记复原");
}
el("document-kind-filter").value = "all";
el("document-kind-filter").dispatchEvent(new Event("change"));
await settle();
assert.equal(rowsOf().length, 2, "回到全部类型两行俱在");
materialsEntries = [];
el("document-kind-filter").dispatchEvent(new Event("change"));
await settle();
assert.equal(el("imported-documents").dataset.empty, "true", "空态收形标记同步");

console.log("frontend materials center behavior passed");
