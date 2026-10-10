import assert from "node:assert/strict";

/* CJK-GUARD-1 输入法合成态守卫行为钉。
   缺陷（MAC-3 交付③实锤 + playwright CDP 合成态复现）：拼音合成中按
   Enter/箭头会误触发搜索提交/翻列表/关浮层。守卫=各 keydown 处理器最前
   `isComposing || keyCode===229` 早退。
   桩件法与 frontend_palette_deep_answer_behavior.mjs 同源（真实执行模块，
   假 DOM + fetch 路由桩 + 手动定时器队列）。覆盖：
   ① palette 输入框：合成中 Enter 不执行条目、合成中箭头不翻激活项、
     旧式 keyCode 229 上报形态同样拦下；合成结束后 Enter 才执行。
   ② 负断言：正常英文/数字输入防抖搜索逐位不变；非合成箭头照常翻页、
     Enter 照常执行（守卫绝不可伤正常键路）。
   ③ ui.js 浮层 Esc 栈：合成中 Esc 不关浮层，正常 Esc 照关。
   ④ course-review Esc：合成中 Esc 不退出复习，正常 Esc 照退。 */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---- fetch 路由桩 ---- */
const fetchCalls = [];
let searchQueue = [];
let deepAnswerQueue = [];

const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  let body = null;
  if (options.body) { try { body = JSON.parse(options.body); } catch { body = null; } }
  fetchCalls.push({ route: route.replace(/^.*\/api\/v3\//, ""), method, body });
  if (method === "GET" && route.startsWith("/api/v3/settings")) {
    return okEnvelope({ deepseek: { configured: true, saved: true } });
  }
  if (method === "GET" && route.startsWith("/api/v3/search?")) {
    const value = searchQueue.shift() || { results: [] };
    return okEnvelope({ query: "", filters: {}, total: (value.results || []).length, results: value.results || [] });
  }
  if (method === "GET" && route.startsWith("/api/v3/search-index")) return okEnvelope({ state: "ready", processed: 4, total: 4 });
  if (method === "POST" && route.startsWith("/api/v3/search/answer")) {
    const value = deepAnswerQueue.shift();
    if (!value) throw new Error("deepAnswer queue empty");
    return okEnvelope(value);
  }
  if (method === "POST" && route.startsWith("/api/v3/analytics/study-events")) return okEnvelope({ recorded: true });
  if (method === "GET" && route.startsWith("/api/v3/tasks")) return okEnvelope({ tasks: [], counts: { active: 0, failed: 0, completed: 0 } });
  throw new Error(`unexpected fetch: ${method} ${route}`);
};

globalThis.EventSource = class {
  constructor() { this.closed = false; }
  addEventListener() {}
  removeEventListener() {}
  close() { this.closed = true; }
};

/* ---- 极简 fake DOM（palette 桩同源） ---- */
class FakeClassList {
  constructor(el) { this.el = el; }
  add(c) { this.el.__classes.add(c); }
  remove(c) { this.el.__classes.delete(c); }
  contains(c) { return this.el.__classes.has(c); }
  toggle(c, on) {
    if (on === undefined) { this.el.__classes.has(c) ? this.remove(c) : this.add(c); return this.el.__classes.has(c); }
    if (on) this.add(c); else this.remove(c);
    return this.el.__classes.has(c);
  }
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
    const event = { target: this, preventDefault() {}, stopPropagation() {}, ...extra };
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

const IDS = [
  "workspace-main", "global-status", "toast-region", "search-trigger",
  "palette-root", "palette-panel", "palette-input", "palette-status",
  "palette-head", "palette-mode-title", "palette-index-pill",
  "palette-answer", "palette-answer-deep", "palette-answer-cancel",
  "palette-collapse", "palette-answer-card", "palette-answer-label",
  "palette-answer-body", "palette-answer-cites", "palette-answer-caveat",
  "palette-listbox", "player-stage", "study-page",
  "course-review-open", "course-review-exit", "course-review-refresh",
  "course-review-reload", "course-review-surface", "ime-pin-overlay",
];
const BUTTON_IDS = new Set([
  "search-trigger", "palette-answer", "palette-answer-deep", "palette-collapse",
  "palette-answer-cancel", "course-review-open", "course-review-exit",
  "course-review-refresh", "course-review-reload",
]);
const el = {};
for (const id of IDS) el[id] = new FakeElement(BUTTON_IDS.has(id) ? "button" : id === "palette-input" ? "input" : "div", id);
el["palette-root"].hidden = true;
el["search-trigger"].setAttribute("data-open-search", "");
el["palette-root"].children.push(el["palette-panel"]);
el["palette-panel"].parentElement = el["palette-root"];
el["palette-panel"].append(el["palette-head"], el["palette-listbox"]); /* 真实层级：getElementById 走 palette-root 子树 */
el["study-page"].dataset.page = "study";
el["ime-pin-overlay"].hidden = true;
el["course-review-surface"].hidden = true;
const crTabs = new FakeElement("div", "cr-tabs-fake");
crTabs.__classes.add("cr-tabs");

/* ---- 手动定时器队列 ---- */
const timerQueue = new Map();
let timerSeq = 0;
async function fireTimers() {
  const batch = [...timerQueue.values()];
  timerQueue.clear();
  for (const t of batch) t.fn();
  await sleep(5);
}

const windowKeyListeners = [];
const windowEvents = [];
/* 动态节点检索：renderOptions 产出的 #palette-opt-N 挂在 listbox 子树，
   setActive 经 document.getElementById 命中它们（真 DOM 语义）。 */
const findInTree = (node, id) => {
  for (const c of node.children || []) {
    if (c.id === id) return c;
    const hit = findInTree(c, id);
    if (hit) return hit;
  }
  return null;
};
globalThis.window = {
  addEventListener(n, fn) { if (n === "keydown") windowKeyListeners.push(fn); },
  removeEventListener(n, fn) {
    const i = windowKeyListeners.indexOf(fn);
    if (i >= 0) windowKeyListeners.splice(i, 1);
  },
  setTimeout: (fn, ms) => { timerSeq += 1; timerQueue.set(timerSeq, { fn, ms }); return timerSeq; },
  clearTimeout: (handle) => { timerQueue.delete(handle); },
  setInterval: () => 0,
  clearInterval() {},
  requestAnimationFrame: (fn) => { fn(); return 0; },
  dispatchEvent: (ev) => {
    const type = String(ev?.type || "");
    windowEvents.push(type);
    if (type === "keydown") { windowKeyListeners.slice().forEach((fn) => fn(ev)); }
    return true;
  },
};
globalThis.clearTimeout = (handle) => { timerQueue.delete(handle); };

globalThis.document = {
  __clickListeners: [],
  __keyListeners: [],
  __active: null,
  getElementById: (id) => el[id] || findInTree(el["palette-root"], id) || null,
  createElement: (tag) => new FakeElement(tag, ""),
  querySelector: (sel) => {
    if (sel === ".page:not([hidden])") return el["study-page"].hidden ? null : el["study-page"];
    if (sel === ".cr-tabs") return crTabs;
    if (sel === "dialog[open]") return null;
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
  contains: () => true,
  get activeElement() { return this.__active || null; },
  set activeElement(v) { this.__active = v; },
};

/* ---- 被测模块 ---- */
const { store } = await import("../frontend/modules/store.js");
const palette = await import("../frontend/modules/search-palette.js");
const ui = await import("../frontend/modules/ui.js");
const review = await import("../frontend/modules/course-review.js");
const reviewState = (await import("../frontend/modules/course-review/state.js")).reviewState;

store.set("auth", { state: "ready", code: "ok" });
store.set("courses", [{
  course_id: "c1", title: "高等数学", teacher: "师",
  lectures: [{ sub_id: "s1", sub_title: "第1讲 极限" }],
}]);
store.set("activeCourse", store.courses[0]);
store.set("activeLecture", { sub_id: "s1", sub_title: "第1讲 极限", course_id: "c1", course_title: "高等数学" });

await palette.installSearchPalette(store);

const input = el["palette-input"];
const kd = (extra) => input.dispatch("keydown", extra);
const activeDescendant = () => input.getAttribute("aria-activedescendant");
const mode = () => el["palette-panel"].dataset.mode;

async function openPalette() {
  if (!el["palette-root"].hidden) {
    document.dispatchEvent({ key: "Escape", preventDefault() {} });
    await sleep(5);
    if (!el["palette-root"].hidden) {
      document.dispatchEvent({ key: "Escape", preventDefault() {} });
      await sleep(5);
    }
  }
  fetchCalls.length = 0;
  el["search-trigger"].dispatch("click");
  await sleep(10);
}

/* ---- 测试 1：合成中 Enter/箭头被拦（缺陷主战场） ---- */
{
  await openPalette();
  await fireTimers();
  assert.equal(el["palette-root"].hidden, false, "面板打开");
  assert.equal(activeDescendant(), "palette-opt-0", "初始激活第 1 项");
  assert.equal(mode(), "compact", "compact 模式");
  fetchCalls.length = 0;

  kd({ key: "Enter", isComposing: true });
  kd({ key: "ArrowDown", isComposing: true });
  kd({ key: "ArrowUp", isComposing: true });
  kd({ key: "Process", keyCode: 229 }); /* 旧式合成上报形态（key=Process+229） */
  kd({ key: "ArrowDown", keyCode: 229 });
  await sleep(10);

  assert.equal(activeDescendant(), "palette-opt-0", "合成中箭头不翻激活项");
  assert.equal(mode(), "compact", "合成中 Enter 不切模式");
  assert.equal(el["palette-root"].hidden, false, "合成中 Enter 不关面板");
  assert.equal(fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("search/answer")).length, 0, "合成中 Enter 不发回答请求");
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("search-index")).length, 0, "合成中 Enter 不触发完整检索");
  console.log("ok: 合成中 Enter/箭头/229 全拦（不提交不翻页不关面板）");
}

/* ---- 测试 2：合成结束后 Enter 才执行（且防抖搜索拿上屏词） ---- */
{
  fetchCalls.length = 0;
  searchQueue.push({ results: [] });
  input.value = "高数";
  input.dispatch("input");
  await fireTimers();
  const search = fetchCalls.find((c) => c.method === "GET" && c.route.startsWith("search?"));
  assert.ok(search, "上屏后防抖搜索发生");
  assert.ok(search.route.includes(encodeURIComponent("高数")), "查询串=上屏词");
  fetchCalls.length = 0;
  kd({ key: "Enter" }); /* 非合成 Enter：执行第 1 项「在全部结果中检索」 */
  await sleep(10);
  assert.equal(mode(), "full", "合成结束后 Enter 执行条目（完整检索模式）");
  console.log("ok: 合成结束后 Enter 才执行（防抖拿上屏词）");
}

/* ---- 测试 3：非合成正常键路逐位不变（负断言：守卫不伤正常输入） ---- */
{
  await openPalette(); /* full 模式经 Esc 收口重开（产品语义：full 下输入不自动回 compact） */
  await fireTimers();
  fetchCalls.length = 0;
  searchQueue.length = 0;
  searchQueue.push({ results: [] });
  input.value = "calculus2"; /* 英文+数字混合逐位进防抖 */
  input.dispatch("input");
  await fireTimers();
  assert.equal(mode(), "compact", "普通输入回 compact");
  const search = fetchCalls.find((c) => c.method === "GET" && c.route.startsWith("search?"));
  assert.ok(search, "普通输入防抖搜索发生");
  assert.ok(search.route.includes(encodeURIComponent("calculus2")), "英文数字查询串逐位不变");

  assert.equal(activeDescendant(), "palette-opt-0", "重渲染后激活复位第 1 项");
  kd({ key: "ArrowDown" });
  assert.equal(activeDescendant(), "palette-opt-1", "非合成箭头照常翻页（下）");
  kd({ key: "ArrowUp" });
  assert.equal(activeDescendant(), "palette-opt-0", "非合成箭头照常翻页（上）");
  kd({ key: "ArrowDown" });
  fetchCalls.length = 0;
  deepAnswerQueue.length = 0;
  deepAnswerQueue.push({ answer: "拼装回答", citations: [], grounded: false });
  kd({ key: "Enter" }); /* 第 2 项=生成证据回答 */
  await sleep(10);
  const post = fetchCalls.find((c) => c.method === "POST" && c.route.startsWith("search/answer"));
  assert.ok(post, "非合成 Enter 照常执行条目");
  assert.equal(mode(), "answer", "证据回答模式进入");
  console.log("ok: 正常键路逐位不变（防抖/翻页/执行全通）");
}

/* ---- 收口：关掉 palette（Esc 逐级）再测浮层栈 ---- */
{
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  await sleep(5);
  if (!el["palette-root"].hidden) {
    document.dispatchEvent({ key: "Escape", preventDefault() {} });
    await sleep(5);
  }
  assert.equal(el["palette-root"].hidden, true, "palette 收口");
  assert.equal(el["workspace-main"].inert, false, "主区 inert 复位");
}

/* ---- 测试 4：ui.js 浮层 Esc 栈——合成中 Esc 不关浮层 ---- */
{
  const root = el["ime-pin-overlay"];
  ui.openOverlay({ root });
  assert.equal(root.hidden, false, "浮层打开");
  document.dispatchEvent({ key: "Escape", isComposing: true });
  assert.equal(root.hidden, false, "合成中 Esc 不关浮层");
  document.dispatchEvent({ key: "Process", keyCode: 229 });
  assert.equal(root.hidden, false, "229 形态 Esc 不关浮层");
  document.dispatchEvent({ key: "Escape" });
  assert.equal(root.hidden, true, "正常 Esc 照常关浮层");
  assert.equal(el["workspace-main"].inert, false, "主区 inert 复位");
  console.log("ok: 浮层 Esc 栈合成态早退、正常路径不变");
}

/* ---- 测试 5：course-review Esc——合成中 Esc 不退出复习 ---- */
{
  review.installCourseReview({ subscribe: () => () => {} });
  reviewState.open = true;
  window.dispatchEvent({ type: "keydown", key: "Escape", isComposing: true });
  assert.equal(reviewState.open, true, "合成中 Esc 不退出复习");
  window.dispatchEvent({ type: "keydown", key: "Process", keyCode: 229 });
  assert.equal(reviewState.open, true, "229 形态 Esc 不退出复习");
  window.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(reviewState.open, false, "正常 Esc 照常退出复习");
  console.log("ok: 复习 Esc 合成态早退、正常路径不变");
}

console.log("frontend ime composition behavior passed");
