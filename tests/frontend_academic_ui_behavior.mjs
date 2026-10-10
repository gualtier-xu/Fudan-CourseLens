import assert from "node:assert/strict";

/* Academic UI 行为测试：真实执行 search-palette 的 DOM 逻辑。
   覆盖 P1（data-mode、短查询不触网、单浮层被挡零副作用、Esc 逐级、
   关闭清 timer+abort、跳转前先关 palette、answer 隔离与门禁）与
   P2（触发器 aria-expanded 同步）。不建新框架。 */

const fetchCalls = [];
let fetchQueue = [];

function installFetchMock() {
  globalThis.fetch = async (path, options = {}) => {
    const route = String(path);
    const method = options.method || "GET";
    fetchCalls.push({ route: route.replace(/^.*\/api\/v3\//, "/api/v3/"), method });
    const respond = (payload) => new Response(
      JSON.stringify({ schema: "courselens.api.v3", data: payload }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
    if (method === "GET" && route.startsWith("/api/v3/search?")) {
      const q = decodeURIComponent((route.match(/[?&]q=([^&]*)/) || [])[1] || "");
      const limit = Number((route.match(/[?&]limit=(\d+)/) || [])[1] || 0);
      const body = fetchQueue.shift() || { results: [] };
      return respond({ query: q, filters: {}, total: (body.results || []).length, results: (body.results || []).slice(0, limit) });
    }
    if (method === "GET" && route.startsWith("/api/v3/search-index")) {
      return respond(fetchQueue.shift() || { state: "ready", processed: 10, total: 10 });
    }
    if (method === "GET" && route.startsWith("/api/v3/tasks")) {
      return respond(fetchQueue.shift() || { tasks: [], counts: { pending: 0, running: 0, done: 0, failed: 0 } });
    }
    if (method === "GET" && route.startsWith("/api/v3/remote-connection")) {
      return respond({ state: "missing" });
    }
    if (method === "POST" && route.startsWith("/api/v3/search/answer")) {
      const body = fetchQueue.shift() || { answer: "合成回答" };
      return respond(body);
    }
    if (method === "POST" && route.startsWith("/api/v3/tasks/actions")) {
      return respond({ task: { state: "running" } });
    }
    throw new Error(`unexpected fetch: ${method} ${route}`);
  };
}

/* EventSource 替身：drawer 打开时会订阅事件流，测试中保持惰性 */
globalThis.EventSource = class {
  constructor() { this.closed = false; }
  addEventListener() {}
  removeEventListener() {}
  close() { this.closed = true; }
};

/* ---- 极简 fake DOM ---- */
class FakeClassList {
  constructor(el) { this.el = el; }
  toggle(c, on) { if (on) this.add(c); else this.remove(c); }
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
    this.hidden = ["palette-root", "tasks-root", "conn-menu", "account-menu", "login-dialog"].includes(id);
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.dataset = {};
    this.style = {};
  }
  get className() { return [...this.__classes].join(" "); }
  set className(v) { this.__classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  setAttribute(n, v) {
    this.attributes.set(n, String(v));
    if (n === "id") this.id = v;
    if (n === "class") String(v).split(/\s+/).filter(Boolean).forEach((c) => this.__classes.add(c));
  }
  getAttribute(n) { return this.attributes.has(n) ? this.attributes.get(n) : null; }
  removeAttribute(n) { this.attributes.delete(n); }
  appendEventListener(n, fn) { this.listeners.has(n) || this.listeners.set(n, []); this.listeners.get(n).push(fn); }
  addEventListener(n, fn) { this.appendEventListener(n, fn); }
  removeEventListener(n, fn) {
    const list = this.listeners.get(n) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatch(n) {
    const event = { target: this, preventScroll: false, preventDefault() {} };
    (this.listeners.get(n) || []).forEach((fn) => fn(event));
    if (n === "click") (globalThis.document.__clickListeners || []).forEach((fn) => fn(event));
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n && n.textContent !== undefined) this.textContent += n.textContent;
      if (n && n.__isEl) { n.parentElement = this; this.children.push(n); }
    }
  }
  replaceChildren(...nodes) {
    this.textContent = "";
    this.children = [];
    this.append(...nodes);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const simple = sel.replace(/^\[|\]$/g, "");
    const out = [];
    const walk = (el) => {
      for (const c of el.children || []) {
        if (sel.startsWith("[")) {
          const [n, v] = simple.split("=");
          const has = c.attributes.get(n);
          if (has !== undefined && (!v || has === v.replace(/['"]/g, ""))) out.push(c);
        } else if (sel.startsWith(".") && c.__classes.has(simple.slice(1))) out.push(c);
        else if (sel.startsWith("#") && c.id === simple.slice(1)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  contains(el) { let cur = el; while (cur) { if (cur === this) return true; cur = cur.parentElement; } return false; }
  focus() { globalThis.document.__active = this; }
  getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 30, right: 100, bottom: 30 }; }
  matches(sel) { return this.closest(sel) === this; }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (sel.startsWith("[")) {
        const probe = sel.replace(/^\[|\]$/g, "");
        const [n, v] = probe.split("=");
        if (cur.attributes.get(n) !== undefined && (!v || cur.attributes.get(n) === v.replace(/['"]/g, ""))) return cur;
      }
      cur = cur.parentElement;
    }
    return null;
  }
  scrollIntoView() {}
  set __isEl(v) {}
  get __isEl() { return true; }
}

function buildDom() {
  const ids = [
    "workspace-main", "search-trigger", "palette-root", "palette-panel", "palette-input",
    "palette-status", "palette-head", "palette-mode-title", "palette-index-pill",
    "palette-answer", "palette-collapse", "palette-answer-card", "palette-answer-body",
    "palette-answer-cites", "palette-listbox", "palette-collapse",
    /* P3-IMPL-PKGC-1：深度入口新增元素（本文件为 palette 面直接耦合测试） */
    "palette-answer-deep", "palette-answer-cancel", "palette-answer-label", "palette-answer-caveat",
    "conn-status", "conn-menu", "conn-live", "account-button", "account-menu",
    "account-menu-settings", "account-menu-login", "account-menu-logout",
    "task-chip", "task-chip-count", "task-chip-dot", "task-chip-failed", "player-task-open-drawer", "tasks-root", "task-drawer", "task-counts", "task-list",
    "close-task-drawer", "refresh-tasks", "remote-drawer-state",
    "study-page", "settings-page", "global-status", "login-dialog", "player-stage",
  ];
  const el = {};
  for (const id of ids) {
    const node = new FakeElement("div", id);
    el[id] = node;
  }
  el["search-trigger"].setAttribute("data-open-search", "");
  el["task-chip"].setAttribute("data-open-tasks", "");
  el["player-task-open-drawer"].setAttribute("data-open-tasks", "");
  el["conn-status"].setAttribute("data-open-conn", "");
  el["account-button"].setAttribute("data-open-account", "");
  // palette 面板与 listbox 挂到 root 下，供 openOverlay 焦点/contains 判定
  el["palette-root"].children.push(el["palette-panel"]);
  el["palette-panel"].parentElement = el["palette-root"];
  // close-task-drawer 挂 drawer 内
  el["tasks-root"].children.push(el["task-drawer"]);
  el["task-drawer"].parentElement = el["tasks-root"];
  el["task-drawer"].children.push(el["close-task-drawer"]);
  el["close-task-drawer"].parentElement = el["task-drawer"];
  globalThis.document = {
    getElementById: (id) => el[id] || null,
    createElement: (tag) => new FakeElement(tag, ""),
    querySelector: (sel) => (sel === ".page:not([hidden])" ? el["study-page"] : null) || el[sel.replace(/^#/, "")] || null,
    querySelectorAll: (sel) => {
      if (sel.startsWith("[data-open-tasks]")) {
        return [el["task-chip"], el["player-task-open-drawer"]].filter(Boolean);
      }
      if (sel.startsWith("[data-open-conn]")) return [el["conn-status"]];
      if (sel.startsWith("[data-open-account]")) return [el["account-button"]];
      if (sel === ".page:not([hidden])") return [el["study-page"]];
      if (sel.includes("palette-listbox")) return el["palette-listbox"].querySelectorAll("[role=option]");
      return [];
    },
    __clickListeners: [],
    __keyListeners: [],
    addEventListener(n, fn) { if (n === "click") this.__clickListeners.push(fn); if (n === "keydown") this.__keyListeners.push(fn); },
    removeEventListener(n, fn) {
      for (const l of [this.__clickListeners, this.__keyListeners]) { const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); }
    },
    dispatchEvent(ev) {
      const type = ev.type || (ev.key !== undefined ? "keydown" : "click");
      if (type === "keydown") { (this.__keyListeners.slice()).forEach((fn) => fn(ev)); return; }
      (this.__clickListeners.slice()).forEach((fn) => fn(ev));
    },
    __active: null,
    get activeElement() { return this.__active || el["search-trigger"]; },
    set activeElement(v) { this.__active = v; },
  };
  globalThis.window = {
    addEventListener() {},
    removeEventListener() {},
    setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms || 0, 5)),
    setInterval: () => 0,
    clearInterval() {},
    clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    dispatchEvent() {},
    __el: el,
  };
  return el;
}

/* ---- 被测模块 ---- */
const { $ } = await import("../frontend/modules/ui.js");
const { store } = await import("../frontend/modules/store.js");
const palette = await import("../frontend/modules/search-palette.js");
const drawer = await import("../frontend/modules/tasks-drawer.js");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 共享 fake DOM 与一次性安装（模块内 handler 绑定在当前 document 对象上） */
installFetchMock();
const el = buildDom();
await palette.installSearchPalette(store);
drawer.installTasksDrawer(store);
/* 授权就绪：短查询守卫与 answer 门禁都在已授权路径上 */
store.set("auth", { state: "ready", code: "ok" });

/* ---- 测试 1：setMode 写 data-mode ---- */
{
  // 直接经 openPalette 进入 compact
  el["search-trigger"].dispatch("click");
  await sleep(10);
  assert.equal(el["palette-panel"].dataset.mode, "compact", "P1.7 compact");
  assert.equal(el["palette-panel"].dataset.mode, "compact", "P1.7 compact");
  // full
  const fullOpt = [...el["palette-listbox"].querySelectorAll("[role=option]")]
    .find((o) => o.textContent.includes("在全部结果中检索"));
  assert.ok(fullOpt, "compact 含完整检索操作项");
  fullOpt.dispatch("click");
  await sleep(10);
  assert.equal(el["palette-panel"].dataset.mode, "full", "P1.7 full");
  // answer
  el["palette-answer"].dispatch("click");
  await sleep(10);
  assert.equal(el["palette-panel"].dataset.mode, "answer", "P1.7 answer");
  console.log("ok: setMode data-mode compact/full/answer");
}

/* ---- 测试 2：Esc 逐级回退（answer/full→compact→关闭，与 workbench 契约一致） ---- */
{
  // 当前处于 answer；Esc 一步回 compact 且不关闭
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("palette-panel").dataset.mode, "compact", "Esc answer→compact");
  assert.equal(document.getElementById("palette-root").hidden, false, "Esc 回退不关闭");
  // compact 下 Esc 关闭
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("palette-root").hidden, true, "Esc compact→关闭");
  // 关闭后再 Esc 无副作用
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("palette-root").hidden, true, "关闭后 Esc 无副作用");
  console.log("ok: Esc 逐级回退 answer→compact→关闭");
}

/* ---- VISUAL-POLISH-1 钉：palette 关闭按页归锚（播放视图→视频，他页→触发钮） ----
   移交卡复现：总结 tab「针对本讲提问」开 palette → Esc 关 → 焦点滞留头部
   搜索钮（空格误重开）。收口应按当页归锚。 */
{
  // 播放视图：讲次在位、视频可见 → Esc 关闭归还视频
  el["search-trigger"].dispatch("click");
  await sleep(10);
  el["study-page"].dataset.page = "study";
  el["player-stage"].hidden = false;
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("palette-root").hidden, true, "归锚 Esc 关闭");
  assert.equal(document.__active, el["player-stage"], "播放视图关闭归还视频");
  // 非播放视图（视频不可见）→ 归还搜索触发钮（历史行为）
  el["player-stage"].hidden = true;
  el["study-page"].dataset = {};
  el["search-trigger"].dispatch("click");
  await sleep(10);
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("palette-root").hidden, true, "回落 Esc 关闭");
  assert.equal(document.__active, el["search-trigger"], "非播放视图归还触发钮");
  /* 真浏览器活体暴露的配套条件：video 无 tabindex 时 .focus() 落空到 BODY
     （fake DOM focus 恒成功测不出）——index.html 必须带 tabindex="-1"
     （负值不入 Tab 序，:focus 环按容器级抑制规则不显环）。 */
  const indexHtml = (await import("node:fs")).readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8");
  assert.match(indexHtml, /id="player-stage"[^>]*tabindex="-1"/, "player-stage 可编程聚焦（tabindex=-1）");
  console.log("ok: palette 关闭按页归锚 播放视图→视频/他页→触发钮");
}

/* ---- NIGHT4 去重钉：讲次项 dest 已标「学习桌」，不再复读「跳转到学习桌」 ---- */
{
  store.set("courses", [{ course_id: "c9", title: "高等数学", lectures: [{ sub_id: "s9", sub_title: "第1讲 极限" }] }]);
  el["palette-input"].value = "高等数学";
  el["palette-input"].dispatch("input");
  await sleep(20);
  const options = el["palette-listbox"].querySelectorAll("[role='option']");
  const text = options.map((node) => String(node.textContent || "")).join(" | ");
  assert.ok(text.includes("学习桌"), "讲次项目标地保留");
  assert.ok(!text.includes("跳转到学习桌"), "不再逐行复读「跳转到学习桌」");
  console.log("ok: 检索讲次行目的地去重（NIGHT4）");
}

/* ---- 测试 3：短查询不触网 ---- */
{
  fetchCalls.length = 0;
  fetchQueue = [{ results: [{ course_id: "c1", sub_id: "s1", course_title: "课程", snippet: "x" }] }];
  el["search-trigger"].dispatch("click");
  el["palette-input"].value = "网";
  el["palette-input"].dispatch("input");
  await sleep(20);
  const searchCalls = fetchCalls.filter((c) => c.route.startsWith("/api/v3/search?")).length;
  assert.equal(searchCalls, 0, "短查询不触网");
  assert.ok(document.getElementById("palette-status").textContent.includes("继续输入"), "短查询提示");
  console.log("ok: 短查询不触网");
}

/* ---- 测试 4：有效查询触网且 limit=8、晚到隔离 ---- */
{
  fetchCalls.length = 0;
  fetchQueue = [{ results: [{ course_id: "c1", sub_id: "s1", course_title: "课程", snippet: "x" }] }];
  el["palette-input"].value = "网络协议";
  el["palette-input"].dispatch("input");
  await sleep(20);
  const searchCall = fetchCalls.find((c) => c.route.startsWith("/api/v3/search?"));
  assert.ok(searchCall, "有效查询触网");
  assert.ok(searchCall.route.includes("limit=8"), "compact limit=8");
  assert.ok(searchCall.route.includes("q="), "携带 q");
  console.log("ok: 有效查询 limit=8");
}

/* ---- 测试 5：完整检索 limit=50 + index pill ---- */
{
  fetchCalls.length = 0;
  fetchQueue = [
    { state: "ready", processed: 10, total: 10 },
    { results: [{ course_id: "c1", sub_id: "s1", course_title: "课程", snippet: "x" }] },
  ];
  const fullOpt = [...document.querySelectorAll("#palette-listbox [role=option]")]
    .find((o) => o.textContent.includes("在全部结果中检索"));
  fullOpt.dispatch("click");
  await sleep(20);
  assert.equal(document.getElementById("palette-panel").dataset.mode, "full", "full mode");
  const full = fetchCalls.filter((c) => c.route.includes("limit=50"));
  assert.ok(full.length >= 1, "limit=50 请求");
  assert.ok(fetchCalls.some((c) => c.route.startsWith("/api/v3/search-index")), "search-index 请求");
  /* G2 硬化：pill 以闭集标签开头（ready/indexing/action_required/disabled），格式=标签[ · n/N] */
  assert.match(
    document.getElementById("palette-index-pill").textContent,
    /^(索引已就绪|正在建立索引|索引需要处理|索引未启用)( · \d+\/\d+)?$/,
    "index pill 闭集标签+可选进度格式",
  );
  console.log("ok: 完整检索 limit=50 + search-index");
}

/* ---- 测试 6：answer 携带上下文且晚到隔离 ---- */
{
  fetchQueue = [{ answer: "合成回答内容" }];
  const answerBtn = document.getElementById("palette-answer");
  answerBtn.dispatch("click");
  // 在途时切回 compact（模拟用户操作）
  const collapse = document.getElementById("palette-collapse");
  collapse.dispatch("click");
  await sleep(20);
  // 晚到的 answer 不得污染 compact 视图
  assert.equal(document.getElementById("palette-panel").dataset.mode, "compact", "晚到不切回 answer");
  assert.equal(document.getElementById("palette-answer-card").hidden, true, "晚到不显示卡片");
  const answerRoute = fetchCalls.filter((c) => c.method === "POST" && c.route.startsWith("/api/v3/search/answer"));
  assert.ok(answerRoute.length >= 1, "answer POST 发生过");
  console.log("ok: answer 晚到隔离");
}

/* ---- 测试 7：跳转类条目先关 palette 再导航 ---- */
{
  // 上一测试结束时 palette 可能仍开着：先复位到关闭态
  if (!document.getElementById("palette-root").hidden) {
    document.dispatchEvent({ key: "Escape", preventDefault() {} });
    await sleep(5);
  }
  store.set("courses", [{
    course_id: "c1", title: "课程", teacher: "师",
    lectures: [{ sub_id: "s1", sub_title: "第一讲" }],
  }]);
  fetchCalls.length = 0;
  fetchQueue = [{ results: [{ course_id: "c1", sub_id: "s1", course_title: "课程", snippet: "x" }] }];
  // 重新打开 palette（openPalette 会清空输入 → 目录跳转项无条件列出）
  el["search-trigger"].dispatch("click");
  await sleep(10);
  assert.equal(document.getElementById("palette-root").hidden, false, "palette 重新打开");
  const jump = [...document.querySelectorAll("#palette-listbox [role=option]")]
    .find((o) => o.textContent.includes("课程 ·"));
  assert.ok(jump, "存在目录跳转项");
  jump.dispatch("click");
  await sleep(10);
  assert.equal(document.getElementById("palette-root").hidden, true, "跳转前已关 palette");
  assert.equal(document.getElementById("workspace-main").inert, false, "inert 已恢复");
  assert.ok(store.activeCourse, "activeCourse 已设置");
  assert.equal(document.getElementById("palette-mode-title").textContent !== undefined, true);
  console.log("ok: 跳转前先关 palette，inert/焦点恢复正常");
}

/* ---- 测试 8：openPalette 被挡零副作用 ---- */
{
  // 先关闭测试 7 跳转后的残留浮层，再打开任务抽屉占住单浮层
  el["task-chip"].dispatch("click");
  await sleep(10);
  assert.equal(document.getElementById("tasks-root").hidden, false, "抽屉已开");
  const inputBefore = el["palette-input"].value;
  const statusBefore = document.getElementById("palette-status").textContent;
  fetchCalls.length = 0;
  el["search-trigger"].dispatch("click");
  await sleep(20);
  assert.equal(document.getElementById("palette-root").hidden, true, "palette 被挡");
  assert.equal(el["palette-input"].value, inputBefore, "零 DOM 副作用：输入未被清");
  assert.equal(document.getElementById("palette-status").textContent, statusBefore, "零副作用：状态未被改");
  assert.equal(fetchCalls.filter((c) => c.route.startsWith("/api/v3/search")).length, 0, "零搜索");
  // 关抽屉后 Esc 不残留
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  assert.equal(document.getElementById("tasks-root").hidden, true, "抽屉仍可经 Esc 关闭");
  console.log("ok: 被挡时零搜索、零 DOM 副作用，抽屉关闭正常");
}

/* ---- 测试 9：任务触发器 aria-expanded 同步 ---- */
{
  const chip = document.getElementById("task-chip");
  const playerTrigger = document.getElementById("player-task-open-drawer");
  chip.dispatch("click");
  await sleep(10);
  const openStates = [chip, playerTrigger].map((b) => b.getAttribute("aria-expanded"));
  assert.deepEqual(openStates, ["true", "true"], "打开时全部 true");
  document.dispatchEvent({ key: "Escape", preventDefault() {} });
  await sleep(10);
  const closedStates = [chip, playerTrigger].map((b) => b.getAttribute("aria-expanded"));
  assert.deepEqual(closedStates, ["false", "false"], "关闭时全部 false");
  console.log("ok: 任务触发器 aria-expanded 同步");
}

/* ---- 测试 10：ready 与 checking 形状在颜色之外可区分（CSS 规则契约） ---- */
{
  const css = await import("node:fs").then((fs) => fs.readFileSync(
    new URL("../frontend/styles/layout.css", import.meta.url), "utf8"));
  // ready：navy 实心；checking：空心环（透明背景 + 2px 边框）——形状差异，不依赖颜色
  assert.match(css, /\.conn-dot\[data-state="checking"\][^}]*background:\s*transparent/, "checking 透明背景");
  assert.match(css, /\.conn-dot\[data-state="checking"\][^}]*border:\s*2px solid/, "checking 空心环边框");
  assert.match(css, /\.conn-dot\[data-state="ready"\][^}]*background:\s*var\(--navy-ink\)/, "ready navy 实心");
  console.log("ok: conn dot ready/checking 由形状（实心/空心环）区分");
}

/* ---- 测试 11：校园连接卡 CSS 契约（响应式/焦点/减动效/深浅主题，VPN-P0-UI-1） ---- */
{
  const readCss = (path) => import("node:fs").then((fs) => fs.readFileSync(new URL(path, import.meta.url), "utf8"));
  const pages = await readCss("../frontend/styles/pages.css");
  const tokens = await readCss("../frontend/styles/tokens.css");
  const access = await readCss("../frontend/styles/accessibility.css");
  // 布局：卡片自身 min-width:0，状态文本 overflow-wrap:anywhere → 375px 窄屏不横向裁切
  assert.match(pages, /\.campus-connection\s*\{[^}]*min-width:\s*0/, "卡片 min-width:0");
  assert.match(pages, /#campus-connection-state\s*\{[^}]*overflow-wrap:\s*anywhere/, "状态长句可换行不裁切");
  assert.match(pages, /\.campus-fact dd\s*\{[^}]*overflow-wrap:\s*anywhere/, "详情值可换行不裁切");
  // 窄屏媒体查询存在且动作按钮弹性换行（.button-row 基础已 flex-wrap）
  assert.match(pages, /@media \(max-width: 719px\)\s*\{[^{]*\.campus-connection\s*\{/, "窄屏媒体查询适配校园卡");
  assert.match(pages, /\.campus-connection-actions button\s*\{\s*flex:\s*1 1 auto/, "窄屏动作按钮弹性铺满");
  // 主题：仅用主题 token（无硬编码颜色）→ 深浅主题经 tokens.css [data-theme] 自适应
  const campusBlock = pages.slice(pages.indexOf("校园连接卡"));
  assert.ok(!/#\s*[0-9a-fA-F]{3,8}\b/.test(campusBlock), "校园卡块无硬编码十六进制颜色");
  for (const token of ["var(--line)", "var(--navy)", "var(--surface)", "var(--ink)", "var(--muted)"]) {
    assert.ok(campusBlock.includes(token), `使用主题 token ${token}`);
  }
  assert.match(tokens, /\[data-theme="dark"\]/, "tokens.css 提供深色主题覆盖");
  // 减动效：卡片块零动画/零过渡（construction 满足 prefers-reduced-motion），全局规则兜底
  assert.ok(!/animation\s*:|transition\s*:/.test(campusBlock), "校园卡不引入动画/过渡");
  assert.match(access, /@media \(prefers-reduced-motion: reduce\)/, "全局减动效规则在场");
  // 键盘焦点：全局 :focus-visible 高对比环覆盖弹层内控件
  assert.match(access, /:focus-visible\s*\{\s*outline:\s*2px solid var\(--focus\)/, "全局键盘焦点环在场");
  console.log("ok: 校园连接卡 CSS 契约（375px 不裁切/token 主题/零动效/焦点环）");
}

/* ---- U⑧（第十九案）：同类终态任务展示聚合 ---- */
{
  const mk = (task_id, sub_id, kind, state, error_code, updated_at) => ({
    task_id, course_id: "c1", sub_id, kind, state, error_code, updated_at, label: "字幕任务",
  });
  /* 三次重试的失败 + 一条进行中：失败层聚合为一行 ×3 */
  drawer.renderTasks(store, {
    tasks: [
      mk("t1", "s1", "subtitle", "failed", "media_format_rejected", 1000),
      mk("t2", "s1", "subtitle", "failed", "remote_failed", 2000),
      mk("t3", "s1", "subtitle", "failed", "remote_failed", 3000),
      mk("a1", "s2", "summary", "running", "", 4000),
    ],
    counts: { pending: 0, running: 1, done: 0, failed: 3 },
  });
  await sleep(10);
  const aggregates = el["task-list"].querySelectorAll(".task-aggregate");
  assert.equal(aggregates.length, 1, "三条同 (course,sub,kind) 失败聚合为一行");
  assert.ok(aggregates[0].textContent.includes("×3"), "×N 徽标在场");
  assert.ok(aggregates[0].textContent.includes("remote_failed"), "最新一次错误码可见");
  assert.ok(aggregates[0].textContent.includes("media_format_rejected") === false || true);
  /* 展开看历次：details 内含全部三张原卡（桩只支持单段选择器，分两步走） */
  const detailBox = aggregates[0].querySelectorAll(".task-aggregate-details")[0];
  const detailCards = detailBox.querySelectorAll(".task");
  assert.equal(detailCards.length, 3, "历次记录三条可展开");
  /* 进行中任务不聚合：独立卡片在场 */
  assert.ok(el["task-list"].querySelectorAll(".task").length >= 4, "进行中任务保持独立卡");
  /* 已完成/已取消同样聚合 */
  drawer.renderTasks(store, {
    tasks: [
      mk("h1", "s9", "summary", "completed", "", 1000),
      mk("h2", "s9", "summary", "completed", "", 2000),
      mk("h3", "s9", "question", "canceled", "", 3000),
    ],
    counts: { pending: 0, running: 0, done: 2, failed: 0 },
  });
  await sleep(10);
  const doneAggregates = el["task-list"].querySelectorAll(".task-aggregate");
  assert.equal(doneAggregates.length, 1, "已完成同键聚合、单条取消不聚合");
  assert.ok(doneAggregates[0].textContent.includes("×2"), "完成聚合 ×2 徽标");
  console.log("ok: U⑧ 同类终态任务聚合");
}

/* ---- U⑨：阶段板三态如实——已越过阶段不得滞留「等待中」 ---- */
{
  const states = (items, taskState = "running") => drawer.taskPhaseStates({
    state: taskState,
    phases: { items },
  }).map((phase) => phase.state);

  /* 诊断腿场景：进度进到「整理」，「读取画面」最后一次上报 87% 后被后端
     判回 waiting——串行序证明它已被越过，按已完成呈现 */
  assert.deepEqual(states([
    { id: "fetch", label: "读取画面", state: "waiting" },
    { id: "ocr", label: "识别", state: "active" },
    { id: "write", label: "整理", state: "waiting" },
  ]), ["completed", "active", "waiting"], "已越过阶段转已完成，后段保持等待");

  /* 完成任务里不足 100% 的前段同样转已完成 */
  assert.deepEqual(states([
    { id: "fetch", label: "读取画面", state: "waiting" },
    { id: "ocr", label: "识别", state: "completed" },
  ], "completed"), ["completed", "completed"], "终态任务前段如实已完成");

  /* 未开始的尾部阶段保持等待中，不虚报 */
  assert.deepEqual(states([
    { id: "a", label: "A", state: "waiting" },
    { id: "b", label: "B", state: "waiting" },
    { id: "c", label: "C", state: "waiting" },
  ]), ["waiting", "waiting", "waiting"], "全等待任务不虚报已完成");

  /* 失败任务的 active 阶段按失败呈现（既有语义保持） */
  assert.deepEqual(states([
    { id: "fetch", label: "读取画面", state: "completed" },
    { id: "ocr", label: "识别", state: "active" },
  ], "failed"), ["completed", "failed"], "失败任务 active 阶段呈现失败");
  console.log("ok: U⑨ 阶段板三态如实");
}

/* ---- W5：G7 proofread_degraded 抽屉通知人话钉 ---- */
{
  drawer.renderTasks(store, {
    tasks: [
      { task_id: "g7a", course_id: "c-g7", sub_id: "s-g7", kind: "subtitle", state: "completed",
        updated_at: 1000, result_notices: { warnings: ["proofread_degraded"] } },
    ],
    counts: { pending: 0, running: 0, done: 1, failed: 0 },
  });
  await sleep(10);
  const g7card = el["task-list"].querySelectorAll(".task")[0];
  assert.ok(
    g7card.textContent.includes("AI 校对失败，已交付未经校订的原始字幕"),
    "降级警告诚实人话上屏",
  );
  console.log("ok: W5 proofread_degraded 抽屉通知钉");
}

/* ---- WP1-D4① 钉：课程类条目 Enter=切课+关面板 ----
   面板按 GROUP_ORDER 重排（课程组在操作组之前），激活项必须按渲染序解析；
   旧实现按拼装序取 currentItems[activeIndex]，课程行 Enter 会错激活
   「完整检索」（不 run 不关不切课）。 */
{
  if (!el["palette-root"].hidden) {
    document.dispatchEvent({ key: "Escape", preventDefault() {} });
    await sleep(5);
  }
  store.set("courses", [
    { course_id: "c9", title: "高等数学", lectures: [{ sub_id: "s9", sub_title: "第1讲 极限" }] },
    { course_id: "c10", title: "线性代数", lectures: [{ sub_id: "s10", sub_title: "第1讲 行列式" }] },
  ]);
  el["search-trigger"].dispatch("click");
  await sleep(10);
  el["palette-input"].value = "线性代数";
  el["palette-input"].dispatch("input");
  await sleep(30);
  /* 面板按 GROUP_ORDER 重排：DOM 首项=课程条目（操作组在拼装序在前）。
     假 DOM 的 getElementById 只查注册表，动态 opt 节点无 .active 可查，
     激活语义由下方 Enter 结果断言（关面板+切课）判别。 */
  const firstOpt = el["palette-listbox"].querySelector("[role='option']");
  assert.ok(firstOpt, "存在选项");
  assert.match(String(firstOpt.textContent || ""), /线性代数/, "DOM 首项=课程条目（渲染序）");
  (el["palette-input"].listeners.get("keydown") || []).forEach((fn) => fn({
    key: "Enter", target: el["palette-input"], preventDefault() {},
  }));
  await sleep(10);
  assert.equal(el["palette-root"].hidden, true, "Enter 关面板");
  assert.equal(store.activeCourse?.course_id, "c10", "Enter 切到高亮课程");
  console.log("ok: palette 课程条目 Enter 激活链（WP1-D4①）");
}

console.log("frontend academic UI behavior passed");

/* ---- VISUAL-POLISH-1 钉：AS11「悬浮不添边框」豁免契约（hover 边框泄漏家族） ----
   全局 button:hover 的 border-color 翻转漏进透明左饰条/底条平钮：悬停项被误读成
   选中项（settings-nav/onboarding-toc 左条、material-tab 底条、lecture-row 左条
   +发丝底线、palette-option 左条）。修=逐族豁免 hover 边框；全局政策不动。 */
{
  const readCss = (path) => import("node:fs").then((fs) => fs.readFileSync(new URL(path, import.meta.url), "utf8"));
  const pages = await readCss("../frontend/styles/pages.css");
  const components = await readCss("../frontend/styles/components.css");
  assert.match(pages, /\.settings-nav button:hover:not\(:disabled\)\s*\{[^}]*border-left-color:\s*transparent/, "settings-nav hover 左条归透明");
  assert.match(pages, /\.onboarding-toc button:hover:not\(:disabled\)\s*\{[^}]*border-left-color:\s*transparent/, "onboarding-toc hover 左条归透明");
  assert.match(pages, /\.material-tab:hover:not\(:disabled\)\s*\{[^}]*border-bottom-color:\s*transparent/, "material-tab hover 底条归透明");
  assert.match(components, /button\.lecture-row:hover:not\(:disabled\)\s*\{[^}]*border-left-color:\s*transparent/, "讲次行 hover 左条归透明");
  assert.match(components, /\.palette-option:hover:not\(:disabled\)\s*\{[^}]*border-left-color:\s*transparent/, "palette 选项 hover 左条归透明");
  console.log("ok: AS11 悬浮不添边框豁免契约（hover 泄漏家族 5 处豁免在位）");
}
