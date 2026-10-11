import assert from "node:assert/strict";

/* FEATURE-FRONTEND-UPDATE-DATA-1 专用行为 harness：数据管理工作区。
   冻结合同 = 台账 §2（schema/闭集/Q1-Q5）+ UX-1 B1-B6。覆盖：
   页面装载与 stale 证据 / 孤儿可见与过滤 / 确认梯度（无确认-单确认-typed） / /
   清除全部孤儿映射 remove-copies+include_orphans / 整批拒绝闭集阻塞码 / 讲次分页 ≤50 /
   搜索与类别过滤 / 未登录诚实恢复态 / 幂等 operation_id。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
}

function datasetKey(name) {
  return String(name).replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

function matches(node, selector) {
  for (const part of String(selector).split(",").map((item) => item.trim())) {
    if (!part) continue;
    if (part.startsWith(".")) {
      if (String(node.className || "").split(/\s+/).includes(part.slice(1))) return true;
      continue;
    }
    if (part.startsWith("[") && node.dataset) {
      const name = part.slice(1, -1);
      if (name === "hidden") { if (node.hidden) return true; continue; }
      if (node.dataset[datasetKey(name)] !== undefined) return true;
      continue;
    }
    if (node.tagName === part.toUpperCase()) return true;
  }
  return false;
}

class FakeElement extends EventTarget {
  constructor(tagName = "div", id = "") {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this._text = "";
    this.dataset = {};
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this._hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.value = "";
    this.style = {}; /* POLISH-1 F11：孤儿名单断句用 inline style；真实 DOM 恒有 style */
    this._listeners = new Map();
  }
  get hidden() { return this._hidden === true; }
  set hidden(value) {
    this._hidden = value === true;
    if (this._hidden) this.attributes.set("hidden", "true");
    else this.attributes.delete("hidden");
  }
  /* 真实 DOM textContent 语义镜像（D-20261009-07 防同类漏检）：
     读=有元素子节点时取子孙文本拼接（input 的 value 不算文本）；
     写=抹除全部子节点，元素子节点随之脱离文档（getElementById 此后
     查不到）。此前 textContent 是普通属性、registry 平铺无父子，
     「label.textContent 抹掉 label 内 input」这一真实 DOM 行为在假
     DOM 中不存在，OBS-8 的 P1（typed 确认框全族死亡）因此漏检。 */
  get textContent() {
    if (this.children.length) return this.children.map((child) => child.textContent).join("");
    return this._text;
  }
  set textContent(value) {
    this._text = String(value ?? "");
    if (this.children.length) {
      for (const child of this.children) child._detached = true;
      this.children = [];
    }
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
    if (!Object.prototype.hasOwnProperty.call(event, "currentTarget")) {
      Object.defineProperty(event, "currentTarget", { value: this, configurable: true });
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
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; this.dispatchEvent(new Event("close")); } }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => { for (const child of node.children || []) { if (matches(child, selector)) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const registry = [];
const byId = {};
const register = (tag, id) => { byId[id] = new FakeElement(tag, id); registry.push(byId[id]); return byId[id]; };

/* 数据页真实结构镜像（index.html 的 id 全集） */
for (const [tag, id] of [
  ["input", "data-search"], ["select", "data-category-filter"], ["button", "data-refresh"],
  ["p", "data-summary"], ["button", "data-orphans-filter"], ["section", "data-recovery"],
  ["h2", "data-recovery-title"], ["p", "data-recovery-impact"], ["div", "data-recovery-actions"],
  ["div", "data-bulk-bar"], ["span", "data-selection-count"],
  ["p", "data-bulk-hint"], ["div", "data-bulk-actions"],
  ["button", "data-action-rebuild-search"], ["button", "data-action-purge-derived"],
  ["button", "data-action-remove-copies"], ["button", "data-action-export"],
  ["button", "data-action-export-study-stats"],
  ["button", "data-action-delete-records"], ["button", "data-select-all"],
  ["button", "data-clear-orphans"], ["button", "data-clear-selection"],
  ["p", "data-result"], ["aside", "data-course-list"], ["section", "data-detail"],
  ["dialog", "data-confirm-dialog"], ["form", "data-confirm-form"],
  ["h2", "data-confirm-title"], ["p", "data-confirm-hint"], ["input", "data-confirm-input"],
  ["span", "data-confirm-label-text"],
  ["label", "data-confirm-label"],
  ["button", "data-confirm-cancel"], ["button", "data-confirm-confirm"],
  ["span", "data-evidence"], ["section", "data-page"], ["span", "update-live"],
  ["div", "toast-region"], ["div", "account-menu"], ["button", "account-menu-data"],
  ["span", "account-text"],
]) {
  register(tag, id);
}
/* 静态文案由 index.html 提供（FakeElement 不解析 HTML），逐钮补齐 */
byId["data-action-rebuild-search"].textContent = "重建索引";
byId["data-action-purge-derived"].textContent = "清理派生数据";
byId["data-action-remove-copies"].textContent = "移除本地副本";
byId["data-action-export"].textContent = "导出";
byId["data-action-export-study-stats"].textContent = "导出学习统计";
byId["data-action-delete-records"].textContent = "删除记录…";
byId["data-select-all"].textContent = "全选";
byId["data-clear-orphans"].textContent = "清除全部孤儿";
byId["data-clear-selection"].textContent = "清空选择";
byId["data-refresh"].textContent = "刷新";
byId["data-confirm-cancel"].textContent = "取消";
byId["data-confirm-confirm"].textContent = "确认";
/* D-20261009-07 父子语义补强：确认标签真实结构（index.html）=
   label > (span#data-confirm-label-text 文案 + input#data-confirm-input)。
   registry 必须镜像这一父子结构，textContent 抹除语义才有作用对象。 */
byId["data-confirm-label-text"].textContent = "输入课程名确认";
byId["data-confirm-label"].append(byId["data-confirm-label-text"]);
byId["data-confirm-label"].append(byId["data-confirm-input"]);
byId["data-orphans-filter"].textContent = "孤儿";
byId["data-bulk-hint"].textContent = "先勾选课程，再选下方操作";
byId["data-page"].hidden = true;
byId["data-bulk-hint"].hidden = true;
byId["data-recovery"].hidden = true;
byId["data-result"].hidden = true;
byId["data-orphans-filter"].hidden = true;
byId["data-confirm-confirm"].disabled = true;
byId["account-menu"].hidden = true;

globalThis.document = {
  /* _detached 节点=被某次 textContent 赋值抹掉的元素子节点：真实 DOM 里
     它们已不在文档中，getElementById/querySelector 必须查不到（D-20261009-07）。 */
  getElementById: (id) => {
    const node = byId[id];
    return node && !node._detached ? node : null;
  },
  createElement: (tag) => {
    const node = new FakeElement(tag, `created-${registry.length + 1}`);
    registry.push(node);
    return node;
  },
  querySelectorAll: (selector) => registry.filter((node) => !node._detached && matches(node, selector)),
  querySelector: (selector) => registry.find((node) => !node._detached && matches(node, selector)) || null,
  activeElement: null,
  documentElement: { dataset: {} },
  addEventListener() {},
  removeEventListener() {},
};

const windowTarget = new EventTarget();
windowTarget.setTimeout = (fn) => 0;
windowTarget.clearTimeout = () => {};
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
};
globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
globalThis.CSS = { escape: (value) => String(value) };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
windowTarget.matchMedia = globalThis.matchMedia; /* 浏览器里 window.matchMedia 即全局；Node 桩需显式挂到 window */
globalThis.localStorage = { setItem() {}, getItem: () => null };
Object.defineProperty(globalThis, "navigator", { value: { onLine: true }, configurable: true });

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 4; i += 1) await nextTurn(); };
const deepText = (node) => {
  const parts = [];
  const walk = (current) => { parts.push(String(current.textContent || "")); (current.children || []).forEach(walk); };
  walk(node);
  return parts.join("|");
};

/* ---- 网络桩 ---- */
let summaryPayload = null;
let lecturesPayload = null;
let actionReceipt = null;
let actionError = "";
let summaryError = null;
const actionPost = [];
const lecturesGet = [];

/* D-20261009-01：409 拒绝回执形（真实服务器把 rejected 回执放 409 信封）。 */
let actionRejected409 = false;
const ok = (data) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), { status: 200, headers: { "Content-Type": "application/json" } });
const fail = (code, status) => new Response(JSON.stringify({ error: "synthetic", error_code: code }), { status, headers: { "Content-Type": "application/json" } });

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (route.startsWith("/api/v3/course-data/lectures")) {
    lecturesGet.push(route);
    if (lecturesPayload) return ok(lecturesPayload);
    return fail("fudan_login_required", 401);
  }
  if (route === "/api/v3/course-data" || route.startsWith("/api/v3/course-data?")) {
    if (summaryError) return fail(summaryError, 401);
    return ok(summaryPayload);
  }
  if (route.startsWith("/api/v3/course-data/actions")) {
    actionPost.push(JSON.parse(options.body || "{}"));
    if (actionRejected409) {
      return new Response(JSON.stringify({ schema: "courselens.api.v3", data: actionReceipt }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (actionError) return fail(actionError, 409);
    return ok(actionReceipt);
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

const store = { set() {}, subscribe() { return () => {}; } };

const { installCourseData } = await import("../frontend/modules/course-data.js");
const cleanup = installCourseData(store);
await settle();

/* selectPage 的页面可见性切换属 shell（本 harness 不装 shell），这里只模拟其
   对 data-page 的可见性副作用 + courselens:page 事件派发。 */
const enterDataPage = () => {
  byId["data-page"].hidden = false;
  windowTarget.dispatchEvent(new CustomEvent("courselens:page", { detail: "data" }));
};

const rows = [
  {
    course_id: "c1", in_catalog: true, title: "高等数学", teacher: "张老师",
    lecture_count: 12, file_bytes: { documents: 100, subtitles: 200, courseware: 300 }, total_file_bytes: 600,
    categories: {
      progress: { count: 5, text_bytes: 100, last_updated_at: 1700000000 },
      transcript: { count: 3, text_bytes: 2000, last_updated_at: 1700000000 },
      ppt: { count: 2, text_bytes: 300, last_updated_at: 1700000000 },
      search: { count: 40, text_bytes: 5000, last_updated_at: 1700000000 },
    },
  },
  {
    course_id: "c2", in_catalog: true, title: "线性代数", teacher: "李老师",
    lecture_count: 8, file_bytes: {}, total_file_bytes: 2048,
    categories: { bookmarks: { count: 2, text_bytes: 50, last_updated_at: 0 } },
  },
  { course_id: "c-orphan", in_catalog: false, file_bytes: {}, total_file_bytes: 512, categories: {} },
];

/* 1) 页面装载：摘要条只计在册、证据行 stale、孤儿默认可见（Q2） */
summaryPayload = {
  schema: "courselens.course-data-summary.v1",
  generated_at: 1700000000,
  byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
  database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
  orphan_artifacts: { directories: 1, bytes: 4096 },
  page: { page: 1, page_size: 200, total: 3 },
  rows,
};
enterDataPage();
await settle();
assert.equal(byId["data-page"].hidden, false, "selectPage 泛化点亮数据页");
assert.ok(byId["data-summary"].textContent.includes("课程 2"), "摘要条只计在册课程");
assert.ok(byId["data-summary"].textContent.includes("讲次 20"), "讲次合计来自 lecture_count");
assert.ok(byId["data-summary"].textContent.includes("数据 3 KiB"), "全库字节口径呈现");
assert.ok(byId["data-summary"].textContent.includes("孤儿文件 1"), "孤儿文件计数呈现（#5 词族收敛：孤儿文件）");
assert.ok(!byId["data-summary"].textContent.includes("孤立文件"), "#5 双词并存退役：摘要条不再出现「孤立文件」旧词");
assert.ok(byId["data-orphans-filter"].textContent.includes("孤儿"), "#5 词族一致：过滤芯片与摘要条同用「孤儿」词头");
assert.ok(byId["data-evidence"].textContent.includes("统计生成于"), "stale 证据行（不自动刷新）");
assert.equal(byId["data-orphans-filter"].hidden, false, "孤儿入口可见");
assert.equal(byId["data-course-list"].children.length, 3, "孤儿行随全集渲染");

/* ---- SWEEPFIX-2 S2（化身走查 SWEEP1-S2）：右半屏空态指引与真实交互对齐。
   修前红：占位文案「在左侧挑一门课」指向不明——行首最显眼的勾选框点出的是
   批量操作条，明细要「点课名行」才出现；学生按指引操作得不到承诺结果。
   择小修=改文案：点明「点课名」+ 讲清勾选框的真实作用。 ---- */
{
  const detailEmpty = deepText(byId["data-detail"]);
  assert.ok(detailEmpty.includes("点左侧课名"), "指引点明真实交互=点课名（修前红=「挑一门课」指向不明）");
  assert.ok(detailEmpty.includes("勾选方框是批量操作"), "同时讲清勾选框的真实作用，指引与交互不再错位");
  /* 交互合同钉：勾选只弹批量操作条、明细保持空态；点课名行才出明细 */
  const s2Row = byId["data-course-list"].children[0];
  s2Row.querySelector("input").checked = true;
  s2Row.querySelector("input").dispatchEvent(new Event("change"));
  await settle();
  assert.equal(byId["data-bulk-bar"].hidden, false, "勾选弹出批量操作条");
  assert.ok(deepText(byId["data-detail"]).includes("点左侧课名"), "勾选不打开明细（明细保持空态指引）");
  s2Row.querySelector("button").click();
  await settle();
  assert.ok(!deepText(byId["data-detail"]).includes("点左侧课名"), "点课名行才出明细");
  /* 还原现场：取消勾选，后续小节从干净全集继续 */
  s2Row.querySelector("input").checked = false;
  s2Row.querySelector("input").dispatchEvent(new Event("change"));
  await settle();
  console.log("ok: SWEEPFIX-2 S2 数据页指引与交互对齐（点课名=明细/勾选=批量）");
}

/* 2) 孤儿过滤 + 清除全部孤儿入口 */
byId["data-orphans-filter"].click();
await settle();
assert.equal(byId["data-course-list"].children.length, 1, "孤儿过滤只留孤儿行");
assert.equal(byId["data-clear-orphans"].hidden, false, "孤儿过滤下呈现清除全部孤儿");
byId["data-clear-orphans"].click();
await settle();
assert.equal(byId["data-confirm-dialog"].open, true, "typed 确认 dialog 打开");
assert.equal(byId["data-confirm-title"].textContent, "清除全部孤儿", "Q2 专属标题");
assert.equal(byId["data-confirm-label"].textContent, "输入任意文字确认", "OBS-8 孤儿清理输入标签=任意文字（门禁=非空）");
const orphanHint = deepText(byId["data-confirm-hint"]);
assert.ok(orphanHint.includes("删除后不可恢复"), "Q1 即时删除语义");
assert.ok(orphanHint.includes("自动化规则可能在之后重新生成同类数据"), "Q2 自动化规则警示");
assert.ok(orphanHint.includes("c-orphan"), "清理前孤儿清单先展示");
byId["data-confirm-cancel"].click();
await settle();
assert.equal(byId["data-confirm-dialog"].open, false, "取消关闭");
assert.equal(actionPost.length, 0, "取消不提交");

/* 3) typed 确认：孤儿清理非空确认语 → 映射 remove-copies + include_orphans + confirm */
byId["data-clear-orphans"].click();
await settle();
byId["data-confirm-input"].value = "   ";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
assert.equal(byId["data-confirm-confirm"].disabled, true, "空白确认语禁用");
byId["data-confirm-input"].value = "不匹配";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
assert.equal(byId["data-confirm-confirm"].disabled, false, "孤儿清理非空即启用");
byId["data-confirm-input"].value = "c-orphan";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
assert.equal(byId["data-confirm-confirm"].disabled, false, "任意非空确认语启用");
actionReceipt = {
  schema: "courselens.course-data-action-result.v1",
  action: "remove-copies", operation_id: "op-1", status: "accepted",
};
byId["data-confirm-confirm"].click();
await settle();
assert.equal(actionPost.length, 1, "typed 确认后提交");
assert.equal(actionPost[0].action, "remove-copies", "清除孤儿映射到闭集动作 remove-copies");
assert.equal(actionPost[0].include_orphans, true, "孤儿清理开关随请求");
assert.equal(actionPost[0].confirm, true, "单确认布尔随请求");
assert.equal(actionPost[0].confirm_typed, "c-orphan", "typed 确认语字符串随请求");
assert.deepEqual(actionPost[0].course_ids, ["c-orphan"], "孤儿课程定位");
assert.ok(String(actionPost[0].operation_id).startsWith("course-data:"), "幂等 operation_id");
assert.ok(deepText(byId["data-result"]).includes("已完成"), "接受回执呈现");

/* 4) 整批拒绝：闭集阻塞码 + 计数；未知码不渲染；动作后自动刷新计数 */
byId["data-orphans-filter"].click();
await settle();
const c1Row = byId["data-course-list"].children[0];
assert.equal(c1Row.dataset.courseId, "c1", "全集视图首行是 c1");
c1Row.querySelector("input").checked = true;
c1Row.querySelector("input").dispatchEvent(new Event("change"));
await settle();
byId["data-action-delete-records"].click();
await settle();
assert.equal(byId["data-confirm-label"].textContent, "输入课程名确认", "OBS-8 在册课程输入标签=课程名");
assert.ok(deepText(byId["data-confirm-hint"]).includes("输入课程名「高等数学」确认："), "OBS-8 在册课程提示语=课程名门禁");
byId["data-confirm-input"].value = "高等数学";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
actionReceipt = {
  schema: "courselens.course-data-action-result.v1",
  action: "delete-records", operation_id: "op-2", status: "rejected",
  blockers: [
    { code: "active_task", count: 2 },
    { code: "unknown_future_blocker", count: 9 },
  ],
};
byId["data-confirm-confirm"].click();
await settle();
assert.equal(actionPost[1].action, "delete-records", "删除记录闭集动作");
assert.equal(actionPost[1].confirm_typed, "高等数学", "typed 课程名字符串回执");
assert.ok(deepText(byId["data-result"]).includes("未执行"), "整批拒绝呈现");
assert.ok(deepText(byId["data-result"]).includes("有正在进行的生成任务（2）"), "闭集阻塞码+计数");
assert.ok(!deepText(byId["data-result"]).includes("unknown_future_blocker"), "未知码不渲染");

/* 5) 单确认梯度：清理派生两击臂式；重建索引无确认直发 */
const purgeButton = byId["data-action-purge-derived"];
purgeButton.click();
await settle();
assert.equal(actionPost.length, 2, "首击只换确认文案不提交");
assert.equal(purgeButton.textContent, "确认执行？", "臂式确认文案");
purgeButton.click();
await settle();
assert.equal(actionPost.length, 3, "再击提交一次");
assert.equal(actionPost[2].action, "purge-derived", "闭集动作");
assert.equal(actionPost[2].confirm, true, "单确认布尔 confirm 随请求");
assert.equal(actionPost[2].confirm_typed, undefined, "单确认无 typed 回执");
assert.equal(purgeButton.textContent, "清理派生数据", "提交后文案复位");

const rebuildButton = byId["data-action-rebuild-search"];
rebuildButton.click();
await settle();
assert.equal(actionPost.length, 4, "重建类无确认直发");
assert.equal(actionPost[3].action, "rebuild-search", "重建索引闭集动作");

/* 6) 讲次分页 ≤50：上一页/下一页 + 第 x/y 页 */
lecturesPayload = {
  schema: "courselens.course-data-lecture-page.v1",
  course_id: "c1", in_catalog: true, total: 120,
  page: { limit: 50, offset: 0 },
  lectures: Array.from({ length: 50 }, (_, i) => ({
    sub_id: `s${i + 1}`, in_catalog: true, title: `第${i + 1}讲`,
    date: "2026-09-01", file_bytes: {}, total_file_bytes: 64,
  })),
};
byId["data-course-list"].children[0].querySelector("button").click();
await settle();
const detail = byId["data-detail"];
assert.ok(deepText(detail).includes("第 1 / 3 页"), "分页状态行");
assert.ok(detail.querySelectorAll(".data-lecture-line").length === 50, "讲次表 ≤50/页");
const nextButton = detail.querySelectorAll("button").filter((b) => b.textContent === "下一页")[0];
nextButton.click();
await settle();
assert.ok(lecturesGet.some((route) => route.includes("offset=50")), "下一页携带 offset");
assert.ok(deepText(detail).includes("第 2 / 3 页"), "翻到第 2 页");

/* 7) 搜索与类别过滤（客户端闭集） */
byId["data-search"].value = "高等";
byId["data-search"].dispatchEvent(new Event("input"));
await settle();
assert.equal(byId["data-course-list"].children.length, 1, "搜索过滤课程行");
byId["data-search"].value = "";
byId["data-search"].dispatchEvent(new Event("input"));
byId["data-category-filter"].value = "bookmarks";
byId["data-category-filter"].dispatchEvent(new Event("change"));
await settle();
assert.equal(byId["data-course-list"].children.length, 1, "类别过滤命中含该类课程");
byId["data-category-filter"].value = "";
byId["data-category-filter"].dispatchEvent(new Event("change"));

/* 8) 未登录诚实恢复态：401 → recovery-panel + 登录动作 */
summaryError = "fudan_login_required";
/* stale 不自动刷新：重入页面走缓存；错误路径由显式刷新触发 */
byId["data-refresh"].click();
await settle();
assert.equal(byId["data-recovery"].hidden, false, "错误走恢复面板");
assert.ok(byId["data-recovery-impact"].textContent.includes("登录后显示"), "闭集登录指引");
const loginButton = byId["data-recovery-actions"].querySelectorAll("button")
  .filter((button) => button.textContent === "登录")[0];
assert.ok(loginButton, "未登录提供登录动作");
let loginRequested = false;
const onLogin = () => { loginRequested = true; };
windowTarget.addEventListener("courselens:open-login", onLogin);
loginButton.click();
await settle();
windowTarget.removeEventListener("courselens:open-login", onLogin);
assert.equal(loginRequested, true, "登录动作派发受控登录窗事件");
summaryError = null;

/* 9) 空态可发现性 + W3 多选删除记录禁用 + 禁用提示（DATA 冻结文案） */
const deleteRecordsButton = byId["data-action-delete-records"];
const fullChecks = byId["data-course-list"].querySelectorAll("input");
assert.equal(byId["data-bulk-bar"].hidden, false, "批量条位置常驻（含空态）");
fullChecks[1].checked = true;
fullChecks[1].dispatchEvent(new Event("change"));
await settle();
assert.equal(deleteRecordsButton.disabled, true, "W3 多选时删除记录禁用");
assert.equal(deleteRecordsButton.title, "删除记录一次仅支持一门课程，请单独勾选。", "W3 禁用态人话提示");
deleteRecordsButton.click();
await settle();
assert.equal(byId["data-confirm-dialog"].open, false, "W3 多选点击不进 typed 弹窗");

/* 10) W4 typed 弹窗 Enter=提交：不匹配不关窗不提交；匹配后转发确认按钮 */
fullChecks[1].checked = false;
fullChecks[1].dispatchEvent(new Event("change"));
await settle();
assert.equal(deleteRecordsButton.disabled, false, "单选恢复删除记录可用");
assert.equal(deleteRecordsButton.title.includes("永久删除观看进度"), true, "可用态 title 恢复动作说明");
deleteRecordsButton.click();
await settle();
assert.equal(byId["data-confirm-dialog"].open, true, "单选打开 typed 弹窗");
byId["data-confirm-input"].value = "错误名字";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
const postsBeforeEnter = actionPost.length;
byId["data-confirm-form"].dispatchEvent(new Event("submit"));
await settle();
assert.equal(byId["data-confirm-dialog"].open, true, "W4 输入不匹配时 Enter 不关窗");
assert.equal(actionPost.length, postsBeforeEnter, "W4 输入不匹配时不提交");
byId["data-confirm-input"].value = "高等数学";
byId["data-confirm-input"].dispatchEvent(new Event("input"));
byId["data-confirm-form"].dispatchEvent(new Event("submit"));
await settle();
assert.equal(byId["data-confirm-dialog"].open, false, "W4 Enter 提交后关窗");
assert.equal(actionPost.length, postsBeforeEnter + 1, "W4 Enter 转发确认恰好一次提交");
assert.equal(actionPost.at(-1).action, "delete-records", "W4 提交仍为闭集动作");
assert.deepEqual(actionPost.at(-1).course_ids, ["c1"], "W3 引擎恰 1 课程路径");

/* 11) W5 刷新剪枝：已删课程从选择集消失，计数与批量请求无幽灵 ID */
fullChecks[1].checked = true;
fullChecks[1].dispatchEvent(new Event("change"));
await settle();
assert.equal(byId["data-selection-count"].textContent, "已选 2 门", "刷新前计数含 c2");
summaryPayload = {
  ...summaryPayload,
  rows: rows.filter((row) => row.course_id !== "c2"),
  page: { page: 1, page_size: 200, total: 2 },
};
byId["data-refresh"].click();
await settle();
assert.equal(byId["data-selection-count"].textContent, "已选 1 门", "W5 刷新后按现存行剪枝");
const purgeButtonW5 = byId["data-action-purge-derived"];
purgeButtonW5.click();
await settle();
purgeButtonW5.click();
await settle();
assert.deepEqual(actionPost.at(-1).course_ids, ["c1"], "W5 批量请求无幽灵 ID");

/* 12) W8 讲次 pager 越界钳制：陈旧 offset 按新 total 回钳并重取 */
lecturesPayload = {
  schema: "courselens.course-data-lecture-page.v1",
  course_id: "c1", in_catalog: true, total: 40,
  page: { limit: 50, offset: 0 },
  lectures: Array.from({ length: 40 }, (_, i) => ({
    sub_id: `s${i + 1}`, in_catalog: true, title: `第${i + 1}讲`,
    date: "2026-09-01", file_bytes: {}, total_file_bytes: 64,
  })),
};
byId["data-course-list"].children[0].querySelector("button").click();
await settle();
assert.ok(lecturesGet.at(-2).includes("offset=50"), "W8 陈旧 offset 首取越界");
assert.ok(lecturesGet.at(-1).includes("offset=0"), "W8 按 total 钳回末页重取");
assert.ok(deepText(byId["data-detail"]).includes("第 1 / 1 页"), "W8 pager 无「第 4 / 1 页」陈旧态");

/* 6b) 第十六案：动作成功后，打开中的课程明细（宽布局 data-detail）随强制
   重载同步重渲染——摘要条与行列表更新的同时，明细面板不再停在旧数据 */
const removeButton16 = byId["data-action-remove-copies"];
const firstRow16 = byId["data-course-list"].children[0];
firstRow16.querySelector("input").checked = true;
firstRow16.querySelector("input").dispatchEvent(new Event("change"));
await settle();
actionReceipt = {
  schema: "courselens.course-data-action-result.v1",
  action: "remove-copies", operation_id: "op-16", status: "accepted",
};
/* 后端事实变化：讲次总数 150 → 4（副本已移除后的新清单） */
lecturesPayload = {
  schema: "courselens.course-data-lecture-page.v1",
  course_id: "c1", in_catalog: true, total: 4,
  page: { limit: 50, offset: 0 },
  lectures: Array.from({ length: 4 }, (_, i) => ({
    sub_id: `s${i + 1}`, in_catalog: true, title: `第${i + 1}讲`,
    date: "2026-09-01", file_bytes: {}, total_file_bytes: 0,
  })),
};
removeButton16.click();
await settle();
removeButton16.click();
await settle();
assert.equal(actionPost.at(-1).action, "remove-copies", "移除副本闭集动作");
assert.equal(actionPost.at(-1).confirm, true, "移除副本单确认布尔");
assert.ok(deepText(byId["data-detail"]).includes("第 1 / 1 页"), "动作后明细面板重渲染（讲次表来自新清单，陈旧 offset 被钳制）");


/* 13) W9 孤儿零选禁用批量动作组（清除全部孤儿除外）+ 空态提示行 */
summaryPayload = {
  schema: "courselens.course-data-summary.v1",
  generated_at: 1700000000,
  byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
  database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
  orphan_artifacts: { directories: 1, bytes: 4096 },
  page: { page: 1, page_size: 200, total: 3 },
  rows,
};
byId["data-clear-selection"].click();
await settle();
byId["data-orphans-filter"].click();
await settle();
assert.equal(byId["data-bulk-hint"].hidden, true, "孤儿过滤态不显示空态提示");
assert.equal(byId["data-action-delete-records"].disabled, true, "W9 孤儿零选禁用删除记录");
assert.equal(byId["data-action-purge-derived"].disabled, true, "W9 孤儿零选禁用清理派生");
assert.equal(byId["data-action-rebuild-search"].disabled, true, "W9 孤儿零选禁用重建索引");
assert.equal(byId["data-clear-orphans"].hidden, false, "清除全部孤儿语义独立保持可用");
byId["data-orphans-filter"].click();
await settle();
assert.equal(byId["data-bulk-hint"].hidden, false, "空态显示提示行");
assert.equal(byId["data-bulk-actions"].hidden, true, "空态隐藏动作组");
assert.ok(byId["data-bulk-hint"].textContent.includes("先勾选课程，再选下方操作"), "空态提示行文案");

/* ---- SWEEPFIX-1 S1（化身走查 SWEEP1-S1）：每课/每讲「大小」诚实化。
   0 = 没有可报告的生成文件 →「—」；null = 后端未发布统计 →「未统计」；
   真有文件 → 人话单位。修前红：0/缺席统计面显示「0 B」，与页头数据库
   总体积（「数据 2.4 MiB」级）互相矛盾，学生无法回答「我的数据在哪」。 ---- */
{
  summaryPayload = {
    schema: "courselens.course-data-summary.v1",
    generated_at: 1700000100,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 0, bytes: 0 },
    page: { page: 1, page_size: 200, total: 3 },
    rows: [
      {
        course_id: "c-zero", in_catalog: true, title: "零文件课", lecture_count: 2,
        file_bytes: {}, total_file_bytes: 0,
        categories: { progress: { count: 5, text_bytes: 100, last_updated_at: 1700000000 } },
      },
      {
        course_id: "c-files", in_catalog: true, title: "有文件课", lecture_count: 1,
        file_bytes: { documents: 4096 }, total_file_bytes: 4096,
        categories: { documents: { count: 1, text_bytes: 10, last_updated_at: 1700000000 } },
      },
      {
        course_id: "c-null", in_catalog: true, title: "未统计课", lecture_count: 1,
        file_bytes: null, total_file_bytes: null,
        categories: {},
      },
    ],
  };
  lecturesPayload = {
    schema: "courselens.course-data-lecture-page.v1",
    course_id: "c-zero", in_catalog: true, total: 2,
    page: { limit: 50, offset: 0 },
    lectures: [
      { sub_id: "z1", in_catalog: true, title: "第一讲", date: "2026-10-01", file_bytes: {}, total_file_bytes: 0 },
      { sub_id: "z2", in_catalog: true, title: "第二讲", date: "2026-10-02", file_bytes: null, total_file_bytes: null },
    ],
  };
  byId["data-refresh"].click();
  await settle();
  const rowNodes = [...byId["data-course-list"].querySelectorAll(".data-course-row")];
  const zeroRow = rowNodes.find((row) => deepText(row).includes("零文件课"));
  assert.notEqual(zeroRow, undefined, "S1 零文件行渲染");
  assert.ok(deepText(zeroRow).includes("大小 —"), "S1 零生成文件显示「—」不显示假 0 B");
  assert.ok(!deepText(zeroRow).includes("0 B"), "S1 零文件行不出现 0 B 字样");
  const nullRow = rowNodes.find((row) => deepText(row).includes("未统计课"));
  assert.notEqual(nullRow, undefined, "S1 未统计行渲染");
  assert.ok(deepText(nullRow).includes("大小 未统计"), "S1 null 统计维持「未统计」闭集文案");
  const filesRow = rowNodes.find((row) => deepText(row).includes("有文件课"));
  assert.ok(deepText(filesRow).includes("大小 4 KiB"), "S1 真实文件体积照常人话单位");
  /* 每讲明细面同规则：0 →「—」、null →「未统计」 */
  byId["data-course-list"].children[0].querySelector("button").click();
  await settle();
  const lectureSizes = [...byId["data-detail"].querySelectorAll(".data-lecture-size")]
    .map((node) => String(node.textContent || ""));
  assert.equal(lectureSizes.length, 2, "S1 讲次大小行在位");
  assert.ok(lectureSizes[0].includes("—") && !lectureSizes[0].includes("0 B"), "S1 讲次 0 字节显示「—」");
  assert.ok(lectureSizes[1].includes("未统计"), "S1 讲次 null 统计显示「未统计」");
  console.log("ok: SWEEPFIX-1 S1 数据页大小诚实化（0=—/null=未统计/真值人话单位）");
}

/* ---- POLISH-1 F9/F11（化身走查 FULL-CLIENT-INSPECT F9/F11）：「未统计/—」
   解释面 + 孤儿清除名单断句。修前红：列表行「大小 — · 更新 未统计」无一句
   解释，学生疑为故障；孤儿确认弹层名单为裸 span 序列，与上句视觉粘连成
   「…重新生成同类数据。9000」。 ---- */
{
  summaryPayload = {
    schema: "courselens.course-data-summary.v1",
    generated_at: 1700000200,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 0, bytes: 0 },
    page: { page: 1, page_size: 200, total: 2 },
    rows: [
      {
        course_id: "c-null2", in_catalog: true, title: "未统计课二", lecture_count: 1,
        file_bytes: null, total_file_bytes: null, categories: {},
      },
      {
        course_id: "c-zero2", in_catalog: true, title: "零文件课二", lecture_count: 1,
        file_bytes: {}, total_file_bytes: 0,
        categories: { progress: { count: 1, text_bytes: 10, last_updated_at: 1700000000 } },
      },
      {
        course_id: "c-orphan-2", in_catalog: false, title: "", lecture_count: 0,
        file_bytes: {}, total_file_bytes: 512, categories: {},
      },
    ],
  };
  lecturesPayload = null;
  byId["data-refresh"].click();
  await settle();
  /* F9：悬浮 title 按行状态给原因——null 行讲「未统计」+ 何时会有 */
  const rowNodes = [...byId["data-course-list"].querySelectorAll(".data-course-row")];
  const factsOf = (row) => row.querySelector(".data-row-meta")?.children[0];
  const nullFacts = factsOf(rowNodes.find((row) => deepText(row).includes("未统计课二")));
  assert.notEqual(nullFacts, undefined, "F9 行 meta facts 在场");
  assert.ok(String(nullFacts.title).includes("「未统计」"), "F9 null 行悬浮解释点名「未统计」");
  assert.ok(String(nullFacts.title).includes("自动补上"), "F9 null 行说明何时会有（消除「是不是坏了」）");
  const zeroFacts = factsOf(rowNodes.find((row) => deepText(row).includes("零文件课二")));
  assert.ok(String(zeroFacts.title).includes("不占文件体积"), "F9 零文件行解释「—」= 无生成文件");
  /* F9：明细面板可见一句完整口径（hint 行含「不是故障」） */
  byId["data-course-list"].children[0].querySelector("button").click();
  await settle();
  assert.ok(deepText(byId["data-detail"]).includes("不是故障"), "F9 明细面板可见解释行在场");
  /* F11：孤儿确认名单=引导句 + 逐行点名（display:block 断句，不再与上句粘连） */
  byId["data-orphans-filter"].click();
  await settle();
  byId["data-clear-orphans"].click();
  await settle();
  const hintChildren = byId["data-confirm-hint"].children;
  const lead = hintChildren.find((node) => String(node.textContent || "").includes("将清除以下课程留下的残留数据"));
  assert.notEqual(lead, undefined, "F11 名单引导句在场（C5=「残留数据」人话）");
  assert.ok(String(lead.textContent).includes("残留数据"), "C5 引导句不再内部术语「孤儿数据」直出");
  assert.equal(lead.style.display, "block", "F11 引导句独立成行");
  const names = byId["data-confirm-hint"].querySelectorAll(".data-orphan-name");
  assert.ok(names.length > 0, "F11 孤儿名单逐项点名");
  assert.ok(names.every((node) => node.style.display === "block"), "F11 名单逐行断句不粘连");
  assert.ok(deepText(byId["data-confirm-hint"]).includes("c-orphan-2"), "F11 名单含孤儿课程标识");
  byId["data-confirm-cancel"].click();
  await settle();
  byId["data-orphans-filter"].click();
  await settle();
  console.log("ok: POLISH-1 F9/F11 未统计解释面 + 孤儿名单断句");
}

/* ---- POLISH-1 F8b（化身走查 FULL-CLIENT-INSPECT F8b）：热点核对面——后端
   可选字段 not_understood_count 存在则上屏（行 meta「没听懂 N」+ 明细头一句
   话讲清与书签的从属关系）；零/缺字段行不出现「没听懂」。修前红：化身点完
   「没听懂」后在数据页找不到任何热点计数可核对（进度 1 已确认落库，热点未证）。 ---- */
{
  summaryPayload = {
    schema: "courselens.course-data-summary.v1",
    generated_at: 1700000300,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 0, bytes: 0 },
    page: { page: 1, page_size: 200, total: 2 },
    rows: [
      {
        course_id: "c-hot", in_catalog: true, title: "热点课", lecture_count: 2,
        file_bytes: {}, total_file_bytes: 0,
        categories: { bookmarks: { count: 3, text_bytes: 0, last_updated_at: 1700000000 } },
        not_understood_count: 2,
      },
      {
        course_id: "c-plain", in_catalog: true, title: "普通课", lecture_count: 1,
        file_bytes: {}, total_file_bytes: 0,
        categories: { bookmarks: { count: 1, text_bytes: 0, last_updated_at: 1700000000 } },
      },
    ],
  };
  lecturesPayload = null;
  byId["data-refresh"].click();
  await settle();
  const f8Rows = [...byId["data-course-list"].querySelectorAll(".data-course-row")];
  const hotRow = f8Rows.find((row) => deepText(row).includes("热点课"));
  assert.notEqual(hotRow, undefined, "F8b 热点行渲染");
  assert.ok(deepText(hotRow).includes("没听懂 2"), "F8b 行 meta 热点计数在场");
  const plainRow = f8Rows.find((row) => deepText(row).includes("普通课"));
  assert.ok(!deepText(plainRow).includes("没听懂"), "F8b 零热点行不出现「没听懂」");
  byId["data-course-list"].children[0].querySelector("button").click();
  await settle();
  assert.ok(deepText(byId["data-detail"]).includes("记在书签里"), "F8b 明细头讲清与书签的从属关系");
  assert.ok(deepText(byId["data-detail"]).includes("没听懂 2"), "F8b 明细头计数在场");
  console.log("ok: POLISH-1 F8b 热点核对面（存在则上屏/从属关系讲清）");
}

/* ---- AVATAR-POLISH-1（化身走查 OBS-8 + S1 升级观察 UP-G1）：①无名孤儿行的
   delete-records typed 门禁=课程编号——输入标签与提示语按行内实情改口（键入
   的就是编号「9000」，文案不再声称「课程名」；门禁仍逐字匹配 names[0] 零行为
   变更）；②明细面板「本课文字存量约」随后端可选字段 stored_text_bytes 存在
   则上屏（同 F8b 模式），无字段行诚实不出现。 ---- */
{
  summaryPayload = {
    schema: "courselens.course-data-summary.v1",
    generated_at: 1700000350,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 0, bytes: 0 },
    page: { page: 1, page_size: 200, total: 2 },
    rows: [
      {
        course_id: "c-store", in_catalog: true, title: "存量课", lecture_count: 2,
        file_bytes: {}, total_file_bytes: 0,
        categories: { transcript: { count: 9, text_bytes: 2048, last_updated_at: 1700000000 } },
        stored_text_bytes: 2048,
      },
      {
        course_id: "9000", in_catalog: false, title: "", lecture_count: 0,
        file_bytes: {}, total_file_bytes: 256, categories: {},
      },
    ],
  };
  lecturesPayload = null;
  byId["data-refresh"].click();
  await settle();
  /* UP-G1：明细「本课文字存量约」存在则上屏，人话字节可读 */
  byId["data-course-list"].children[0].querySelector("button").click();
  await settle();
  const storeDetail = deepText(byId["data-detail"]);
  assert.ok(storeDetail.includes("本课文字存量约"), "UP-G1 明细逐课存量行在场");
  assert.ok(storeDetail.includes("2 KiB"), "UP-G1 字节人话可读口径");
  assert.ok(storeDetail.includes("按存进学习库的内容计"), "UP-G1 口径说明在场");
  /* 无字段行（无名孤儿行）不出现存量行（诚实缺省，不虚构） */
  const orphanRowNode = byId["data-course-list"].children[1];
  assert.equal(orphanRowNode.dataset.courseId, "9000", "第二行=无名孤儿行");
  orphanRowNode.querySelector("button").click();
  await settle();
  assert.ok(!deepText(byId["data-detail"]).includes("本课文字存量约"), "UP-G1 无字段行不出现存量行");
  /* OBS-8：孤儿行 delete-records typed 门禁=课程编号（标签+提示语与门禁对齐） */
  byId["data-orphans-filter"].click();
  await settle();
  const obsRow = byId["data-course-list"].children[0];
  assert.equal(obsRow.dataset.courseId, "9000", "孤儿过滤视图首行=9000");
  obsRow.querySelector("input").checked = true;
  obsRow.querySelector("input").dispatchEvent(new Event("change"));
  await settle();
  byId["data-action-delete-records"].click();
  await settle();
  assert.equal(byId["data-confirm-label"].textContent, "输入课程编号确认", "OBS-8 无名行输入标签=课程编号");
  assert.ok(deepText(byId["data-confirm-hint"]).includes("输入课程编号「9000」确认："), "OBS-8 提示语=课程编号，与门禁逐字对齐");
  byId["data-confirm-input"].value = "9000";
  byId["data-confirm-input"].dispatchEvent(new Event("input"));
  assert.equal(byId["data-confirm-confirm"].disabled, false, "OBS-8 键入编号解锁（与提示语一致）");
  const actionCountBeforeCancel = actionPost.length;
  byId["data-confirm-cancel"].click();
  await settle();
  assert.equal(byId["data-confirm-dialog"].open, false, "取消关闭");
  assert.equal(actionPost.length, actionCountBeforeCancel, "取消不提交");
  console.log("ok: AVATAR-POLISH-1 OBS-8 确认词门禁对齐 + UP-G1 逐课文字存量");
}

/* ---- D-20261009-01（化身走查 P2）：409 拒绝回执经 api.js receipt 透传——
   真实服务器把 rejected 回执放 HTTP 409 信封；修前 api.js 非 2xx 一律抛错、
   course-data catch 丢回执 → blockers 渲染分支死代码，学生只见「请稍后重试」
   （重试恒 409 的误导死路）。修后=同一拒绝渲染分支经 409 形照常上屏具体指引。 ---- */
{
  summaryPayload = {
    schema: "courselens.course-data-summary.v1",
    generated_at: 1700000400,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 1, bytes: 4096 },
    page: { page: 1, page_size: 200, total: 1 },
    rows: [
      {
        course_id: "9000", in_catalog: false, title: "", lecture_count: 0,
        file_bytes: {}, total_file_bytes: 256, categories: {},
      },
    ],
  };
  lecturesPayload = null;
  actionReceipt = {
    schema: "courselens.course-data-action-result.v1",
    action: "remove-copies", operation_id: "op-409", status: "rejected",
    blockers: [{ code: "active_task", count: 1 }],
  };
  actionRejected409 = true;
  byId["data-refresh"].click();
  await settle();
  byId["data-orphans-filter"].click();
  await settle();
  byId["data-clear-orphans"].click();
  await settle();
  byId["data-confirm-input"].dispatchEvent(new Event("input"));
  byId["data-confirm-input"].value = "9000";
  byId["data-confirm-input"].dispatchEvent(new Event("input"));
  byId["data-confirm-confirm"].click();
  await settle();
  assert.equal(actionPost[actionPost.length - 1].action, "remove-copies", "孤儿清理闭集动作");
  assert.equal(actionPost[actionPost.length - 1].include_orphans, true, "孤儿清理携带 include_orphans");
  const result409 = deepText(byId["data-result"]);
  assert.ok(result409.includes("未执行"), "409 拒绝回执上屏拒绝语义（死代码复活）");
  assert.ok(result409.includes("有正在进行的生成任务"), "409 形 blockers 具体指引在场");
  assert.ok(!result409.includes("操作未完成"), "不再落「请稍后重试」误导死路");
  actionRejected409 = false;
  console.log("ok: D-20261009-01 409 拒绝回执透传（blockers 指引上屏/误导死路消除）");
}

/* ---- STUDY-STATS-M3：导出学习统计（闭集动作+回执文件下载链接） ---- */
{
  /* 进入数据页并装载摘要（前块已置 summaryPayload；强制刷新一次取现值）。 */
  summaryError = null;
  enterDataPage();
  await settle();
  byId["data-select-all"].click();
  await settle();
  actionPost.length = 0;
  actionReceipt = {
    schema: "courselens.course-data-action-result.v1",
    action: "export-study-stats",
    operation_id: "stats-op-1",
    status: "accepted",
    result: {
      files: [
        { filename: "courselens-study-stats-20261010.json", bytes: 1200 },
        { filename: "courselens-study-stats-20261010-daily.csv", bytes: 120 },
        { filename: "courselens-study-stats-20261010-lectures.csv", bytes: 340 },
      ],
      directory: "study-stats-exports",
    },
  };
  byId["data-action-export-study-stats"].click();
  await settle();
  assert.equal(actionPost[actionPost.length - 1].action, "export-study-stats", "闭集动作名");
  assert.equal(actionPost[actionPost.length - 1].course_ids.length >= 1, true, "与导出同族：需先选课");
  const resultText = deepText(byId["data-result"]);
  assert.ok(resultText.includes("已完成"), "回执完成语义上屏");
  assert.ok(resultText.includes("下载 courselens-study-stats-20261010.json"), "JSON 文件下载链接上屏");
  assert.ok(resultText.includes("下载 courselens-study-stats-20261010-daily.csv"), "daily CSV 下载链接上屏");
  assert.ok(resultText.includes("下载 courselens-study-stats-20261010-lectures.csv"), "lectures CSV 下载链接上屏");
  assert.ok(resultText.includes("study-stats-exports"), "文件在数据目录留有一份的诚实位置说明");
  const links = byId["data-result"].children.filter((n) => n.tagName === "A");
  assert.equal(links.length, 3, "恰三个下载锚");
  for (const link of links) {
    assert.ok(String(link.href).startsWith("/api/v3/study-stats/file?name="), "下载走闭集文件路由");
    assert.equal(link.getAttribute("download"), String(link.textContent).replace("下载 ", ""), "download 属性=文件名");
  }
  console.log("ok: STUDY-STATS-M3 导出学习统计（动作接线+三文件下载链接+目录留档说明）");
}

cleanup();
console.log("frontend course data behavior passed");
