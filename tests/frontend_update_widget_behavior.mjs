import assert from "node:assert/strict";

/* FEATURE-FRONTEND-UPDATE-DATA-1 + UPDATE-CHAIN-HYGIENE-1 行为 harness：顶栏更新小组件。
   冻结合同 = 台账 §1（C1-C4/D1-D3/E1）+ UX-1 A1-A4。覆盖：
   ambient 隐藏闭集 / available 编排动作 fire+≤2s 轮询 / busy disabled+aria-busy /
   ready_to_restart 终态停轮 / E1 armed 失败可见-ambient 失败不可见 /
   failed 单击跳设置更新组 / 播报与 pagehide 停轮。
   响应形状按真实流建模（L3 §A/W2 证据）：update_now 是单请求同步编排，
   POST 只返回终态快照，中间态（downloading/verifying/applying）只能经 GET
   快照到达；fire 即启动有界快照轮询（W2），202 携带 failed 终态须武装可见（W1），
   陈旧快照按请求代际丢弃（W6），409 update_busy 保持原态（W7）。 */

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
  const text = String(selector);
  for (const part of text.split(",").map((item) => item.trim())) {
    const attrMatch = part.match(/^([a-z]+)(?:\[([a-z-]+)="([^"]*)"\])?(?:\[([a-z-]+)\])?$/);
    if (attrMatch) {
      const [, tag, attrName, attrValue, bareAttr] = attrMatch;
      if (node.tagName !== tag.toUpperCase()) continue;
      if (attrName && String(node.dataset?.[datasetKey(attrName)] ?? node.attributes?.get(attrName)) !== attrValue) continue;
      if (bareAttr && bareAttr === "hidden" && !node.hidden) continue;
      return true;
    }
    if (part.startsWith(".")) {
      if (String(node.className || "").split(/\s+/).includes(part.slice(1))) return true;
      continue;
    }
    if (part.startsWith("[") && node.dataset) {
      const name = part.slice(1, -1);
      if (name === "hidden") { if (node.hidden) return true; continue; }
      if (node.dataset?.[datasetKey(name)] !== undefined) return true;
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
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this._hidden = false;
    this.disabled = false;
    this.open = false;
    this.title = "";
    this._listeners = new Map();
  }
  get hidden() { return this._hidden === true; }
  set hidden(value) {
    this._hidden = value === true;
    if (this._hidden) this.attributes.set("hidden", "true");
    else this.attributes.delete("hidden");
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
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
  focus() { document.activeElement = this; }
  scrollIntoView() { this.scrollIntoViewCalls = (this.scrollIntoViewCalls || 0) + 1; }
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

const byId = {};
const register = (tag, id) => { byId[id] = new FakeElement(tag, id); return byId[id]; };

/* 顶栏小组件真实结构（index.html 镜像） */
const toggle = register("button", "update-toggle");
toggle.hidden = true;
toggle.setAttribute("data-update-state", "idle");
const installIcon = register("svg", "created-install-icon");
installIcon.dataset.updateIcon = "install";
const restartIcon = register("svg", "created-restart-icon");
restartIcon.dataset.updateIcon = "restart";
restartIcon.hidden = true;
const dot = register("span", "created-update-dot");
dot.className = "update-dot";
dot.dataset.updateDot = "";
dot.hidden = true;
toggle.append(installIcon, restartIcon, dot);
register("span", "update-live");
register("div", "toast-region");
register("section", "settings-update-group");
register("div", "update-state");
register("p", "update-error");
register("p", "update-recovery");
/* UPDATE-UX-1：mac 检查道按钮（widget 绑定面） */
register("button", "update-mac-check");

globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${Math.random().toString(36).slice(2)}`),
  querySelectorAll: () => [],
  querySelector: () => null,
  activeElement: null,
  documentElement: { dataset: {} },
  addEventListener() {},
  removeEventListener() {},
};

const windowTarget = new EventTarget();
const intervals = new Map();
let intervalSeq = 0;
windowTarget.setInterval = (fn) => { intervalSeq += 1; intervals.set(intervalSeq, fn); return intervalSeq; };
windowTarget.clearInterval = (id) => intervals.delete(id);
const timeoutCallbacks = [];
windowTarget.setTimeout = (fn) => { timeoutCallbacks.push(fn); return timeoutCallbacks.length; };
windowTarget.clearTimeout = () => {};
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.localStorage = { setItem() {}, getItem: () => null };
/* UPDATE-UX-1：UA 可切换（mac 检查道块用 MAC_UA；缺省空 UA=Windows 现行为） */
const MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
const setNavigator = (userAgent) => {
  Object.defineProperty(globalThis, "navigator", { value: { userAgent, onLine: true }, configurable: true });
};
setNavigator("");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 4; i += 1) await nextTurn(); };
const tickPoll = async () => { for (const fn of [...intervals.values()]) { fn(); await settle(); } };

/* ---- 网络桩：快照序列（数组=逐次弹出，末元素驻留）+ 编排动作记录 ----
   GET 在请求时点捕获负载；holdGets 挂起已捕获的响应以模拟乱序后到（W6）。
   POST 在 holdPost 时挂起，releasePost 放行终态——真实 update_now 的
   202 只携带终态，中间态经 GET 到达。 */
let snapshotQueue = [{ state: "idle", current_version: "1.0.0", channel: "stable", available_version: "", package_size: 0, release_notes: "", error_code: "", actions: [] }];
let macReleasesBody = null; /* UPDATE-UX-1：mac Releases 列表桩（null=失败） */
let actionPost = [];
let actionError = "";
let actionResponse = null;
let fetches = 0;
let getFailures = 0;
let holdGets = false;
let heldGets = [];
let holdPost = false;
let releasePost = null;

const ok = (data) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), { status: 200, headers: { "Content-Type": "application/json" } });
const fail = (code, status) => new Response(JSON.stringify({ error: "synthetic", error_code: code }), { status, headers: { "Content-Type": "application/json" } });

globalThis.fetch = async (path, options = {}) => {
  fetches += 1;
  const route = String(path);
  const method = String(options.method || "GET");
  /* UPDATE-UX-1：mac 检查道（update-mac.js）路由——macReleasesBody=null=网络失败 */
  if (route === "https://api.github.com/repos/gualtier-xu/Fudan-CourseLens/releases?per_page=30") {
    if (macReleasesBody === null) throw new Error("mac release list unavailable");
    return new Response(macReleasesBody, { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (route === "/api/v3/client-update" && method === "GET") {
    if (getFailures > 0) { getFailures -= 1; return fail("snapshot_fetch_failed", 503); }
    const payload = snapshotQueue.length > 1 ? snapshotQueue.shift() : snapshotQueue[0];
    if (holdGets) return new Promise((resolve) => { heldGets.push({ payload, resolve }); });
    return ok(payload);
  }
  if (route === "/api/v3/client-update/actions" && method === "POST") {
    actionPost.push(JSON.parse(options.body || "{}"));
    if (actionError) return fail(actionError, 409);
    const payload = actionResponse || snapshotQueue[0];
    if (holdPost) {
      return new Promise((resolve) => {
        releasePost = () => { holdPost = false; releasePost = null; resolve(ok(payload)); };
      });
    }
    return ok(payload);
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

const releaseHeldGets = () => {
  const pending = heldGets.splice(0);
  for (const item of pending) item.resolve(ok(item.payload));
};

const listeners = new Map();
const store = {
  update: null,
  set(key, value) { this[key] = value; for (const listener of listeners.get(key) || []) listener(value); },
  subscribe(key, listener) {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key).add(listener);
    return () => listeners.get(key)?.delete(listener);
  },
};

const base = { current_version: "1.0.0", channel: "stable", available_version: "", package_size: 1024, release_notes: "", error_code: "", actions: [] };
const snapshotOf = (state, extra = {}) => ({ ...base, state, ...extra });

const { installUpdateWidget } = await import("../frontend/modules/update-widget.js");

async function freshInstall() {
  const cleanup = installUpdateWidget(store);
  await settle();
  return cleanup;
}

/* 干净基线：up_to_date 隐藏 + 解除 armed + 复位播报去重（跨块模块态归零） */
async function disarmBaseline() {
  snapshotQueue = [snapshotOf("up_to_date")];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "基线 up_to_date 隐藏");
}

const refreshAvailable = async () => {
  snapshotQueue = [snapshotOf("available", { available_version: "1.1.0", actions: ["update_now"] })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
};

/* 1) ambient 态闭集恒隐藏：idle / up_to_date / offline / checking / policy_blocked / healthy */
{
  const cleanup = await freshInstall();
  assert.equal(toggle.hidden, true, "ambient idle 隐藏");
  assert.equal(store.update?.state, "idle", "安装期快照已发布");
  for (const [state, label] of [
    ["up_to_date", null], ["offline", null], ["checking", null], ["policy_blocked", null], ["healthy", null],
  ]) {
    snapshotQueue = [snapshotOf(state, label || {})];
    windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
    await settle();
    assert.equal(toggle.hidden, true, `ambient ${state} 隐藏`);
  }
  assert.equal(byId["update-live"].textContent, "", "ambient 迁移不播报（checking 安静）");
  cleanup();
}

/* 2) E1：从未激活操作时 ambient failed/rolled_back 不进顶栏 */
{
  const cleanup = await freshInstall();
  snapshotQueue = [snapshotOf("failed", { error_code: "download_http_error" })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "ambient failed 不进顶栏（E1）");
  assert.equal(store.update?.state, "failed", "store 仍发布完整事实（设置卡全真面）");
  snapshotQueue = [snapshotOf("rolled_back")];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "ambient rolled_back 不进顶栏（E1）");
  cleanup();
}

/* 3) available 可见 + 单击 fire：POST 挂起期间中间态经 GET 到达（W2 真实形状），
   POST 终态 ready_to_restart 发布后停轮 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  assert.equal(toggle.hidden, false, "available 可见");
  assert.equal(toggle.getAttribute("aria-label"), "发现新版本 1.1.0，点击开始更新");
  assert.equal(toggle.getAttribute("title"), "发现新版本 1.1.0，点击开始更新");
  assert.equal(toggle.disabled, false, "available 可点击");
  assert.equal(dot.hidden, false, "状态点呈现");
  assert.equal(byId["update-live"].textContent, "发现可信更新，可开始安装", "可见态迁移播报（ANNOUNCE_TEXT 等值钉）");

  actionResponse = snapshotOf("ready_to_restart", { available_version: "1.1.0" });
  holdPost = true;
  toggle.click();
  /* 不 await：同步先行进入过渡态 */
  assert.equal(toggle.disabled, true, "单击后同步 disabled（渲染不等响应）");
  assert.equal(toggle.getAttribute("aria-busy"), "true", "单击后同步 aria-busy");
  await settle();
  assert.deepEqual(actionPost, [{ action: "update_now", confirmed: true }], "编排动作闭集体");
  assert.equal(store.update?.state, "available", "POST 返回前保持操作前快照（单请求编排无中间态响应）");

  /* ≤2s 轮询：click 与 POST resolve 之间 GET ≥1，阶段词经 GET 推进 */
  const getsBefore = fetches;
  snapshotQueue = [
    snapshotOf("downloading", { available_version: "1.1.0" }),
    snapshotOf("verifying"),
    snapshotOf("applying"),
  ];
  await tickPoll();
  assert.ok(fetches - getsBefore >= 1, "fire 在途期间已有快照轮询（W2：click 与 POST resolve 之间 GET ≥1）");
  assert.equal(store.update?.state, "downloading", "中间态经 GET 快照到达");
  assert.equal(toggle.getAttribute("aria-label"), "正在下载更新…");
  assert.equal(toggle.disabled, true, "busy disabled");
  assert.equal(toggle.getAttribute("aria-busy"), "true", "busy aria-busy");
  await tickPoll();
  assert.equal(store.update?.state, "verifying", "轮询推进到 verifying");

  releasePost();
  await settle();
  assert.equal(store.update?.state, "ready_to_restart", "POST 终态发布（等待重启）");
  assert.equal(toggle.disabled, true, "ready_to_restart disabled");
  assert.equal(toggle.getAttribute("aria-busy"), null, "终态清除 aria-busy（先清再算）");
  assert.equal(restartIcon.hidden, false, "重启 glyph 换装");
  assert.equal(installIcon.hidden, true, "安装 glyph 隐藏");
  const pollFetches = fetches;
  await tickPoll();
  await tickPoll();
  assert.equal(fetches, pollFetches, "终态后轮询停止（无孤儿定时器）");
  assert.equal(intervals.size, 0, "定时器表已清空");

  /* healthy 收尾：隐藏 + armed 复位 */
  snapshotQueue = [snapshotOf("healthy")];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "healthy 隐藏");
  cleanup();
}

/* 4) W1：202 携带 failed 终态 → armed + 顶栏可见 + live 播报 + 失败 toast，停轮 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  actionResponse = snapshotOf("failed", { error_code: "download_http_error" });
  toggle.click();
  await settle();
  assert.equal(toggle.hidden, false, "202 failed 终态顶栏可见（W1/E1）");
  assert.equal(toggle.dataset.updateState, "failed", "失败终态呈现");
  assert.equal(toggle.getAttribute("aria-label"), "更新失败，点击进入恢复");
  assert.equal(byId["update-live"].textContent, "更新失败，请打开设置查看恢复入口", "live region 播报失败（W1，ANNOUNCE_TEXT 等值钉）");
  assert.ok(byId["toast-region"].children.length >= 1, "失败 toast（对齐 catch 路径可见性）");
  assert.equal(intervals.size, 0, "失败终态停轮");
  cleanup();
}

/* 5) W6：乱序快照按请求代际丢弃——陈旧 up_to_date 后到不覆盖新鲜 available */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  /* 陈旧请求先发（捕获 up_to_date）但挂起 */
  snapshotQueue = [snapshotOf("up_to_date")];
  holdGets = true;
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  /* 新鲜请求后发、先回 */
  snapshotQueue = [snapshotOf("available", { available_version: "1.1.0" })];
  holdGets = false;
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, false, "新鲜 available 已渲染");
  assert.equal(store.update?.state, "available");
  releaseHeldGets();
  await settle();
  assert.equal(toggle.hidden, false, "陈旧 up_to_date 后到被丢弃（W6）：顶栏不隐藏");
  assert.equal(store.update?.state, "available", "渲染保持新鲜快照（真失败不被陈旧快照永久覆盖的同代际语义）");
  cleanup();
}

/* 6) W7：409 update_busy 并发冲突 → 保持原态 + 人话 toast，不渲染为更新失败、不武装 */
{
  const cleanup = await freshInstall();
  await disarmBaseline();
  await refreshAvailable();
  actionError = "update_busy";
  actionResponse = null;
  toggle.click();
  await settle();
  assert.equal(toggle.hidden, false, "busy 冲突不隐藏");
  assert.equal(toggle.dataset.updateState, "available", "保持当前态（W7）");
  assert.notEqual(toggle.getAttribute("aria-label"), "更新失败，点击进入恢复", "不渲染为本地更新失败");
  assert.equal(toggle.disabled, false, "保持可点击");
  const toasts = byId["toast-region"].children.map((child) => child.textContent).join("|");
  /* G2 硬化：busy 提示等值钉——UPDATE_ACTION_BUSY_TEXT 闭集原句，不再二选一散匹 */
  assert.ok(toasts.includes("另一个更新操作仍在进行。请稍候再检查。"), "人话 busy 提示（闭集原句等值钉）");
  assert.ok(!toasts.includes("update_busy"), "toast 不含闭集码原文");
  actionError = "";
  snapshotQueue = [snapshotOf("available", { available_version: "1.1.0" })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.dataset.updateState, "available", "纠正快照后仍为 available");
  assert.equal(toggle.disabled, false, "冲突后可重试（D1 widget 侧）");
  snapshotQueue = [snapshotOf("failed", { error_code: "download_http_error" })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "busy 未武装 armed：后续 ambient failed 仍隐藏（E1）");
  cleanup();
}

/* 7) D4：fire 失败且纠正快照取数也失败 → live region 不滞留「正在下载更新」 */
{
  const cleanup = await freshInstall();
  await disarmBaseline();
  await refreshAvailable();
  actionError = "download_http_error";
  actionResponse = null;
  getFailures = 1; /* 纠正快照取数也失败 */
  toggle.click();
  await settle();
  assert.equal(toggle.dataset.updateState, "failed", "失败本地呈现");
  const live = byId["update-live"].textContent;
  assert.ok(live.includes("更新失败"), "失败纠正路径补播报（D4）");
  assert.ok(!live.includes("正在下载更新"), "不滞留下载播报（D4）");
  actionError = "";
  getFailures = 0;
  cleanup();
}

/* 8) D3：状态离开可见集后复位播报去重——failed 回归时重新播报 */
{
  const cleanup = await freshInstall();
  await disarmBaseline();
  await refreshAvailable();
  actionResponse = snapshotOf("failed", { error_code: "download_http_error" });
  toggle.click();
  await settle();
  assert.ok(byId["update-live"].textContent.includes("更新失败"), "首次失败播报");
  byId["update-live"].textContent = "consumed"; /* 手动消费，验证重播报 */
  snapshotQueue = [snapshotOf("healthy")];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "healthy 隐藏（离开可见集）");
  await refreshAvailable();
  assert.equal(toggle.hidden, false, "available 重新可见");
  toggle.click(); /* 第二次用户激活操作，202 再次携带 failed（W1 通道） */
  await settle();
  assert.equal(toggle.hidden, false, "二次失败可见");
  assert.ok(byId["update-live"].textContent.includes("更新失败"), "离开可见集后回归重播报（D3）");
  actionResponse = null;
  cleanup();
}

/* 9) D5：轮询失败预算按会话重置——上一会话的失败不清零会让新会话第一拍停轮 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  /* 会话 A：POST 挂起期间吃 4 次取数失败（预算内不停轮） */
  actionResponse = snapshotOf("healthy");
  holdPost = true;
  toggle.click();
  await settle();
  getFailures = 4;
  await tickPoll();
  await tickPoll();
  await tickPoll();
  await tickPoll();
  assert.equal(intervals.size, 1, "会话 A：4 次失败不停轮");
  getFailures = 0;
  releasePost();
  await settle();
  assert.equal(intervals.size, 0, "healthy 终态停轮");
  assert.equal(store.update?.state, "healthy");
  /* 会话 B：新 fire 后第一拍即失败——预算重置则不停轮（无 D5 则累计到 5 停轮） */
  await refreshAvailable();
  assert.equal(toggle.disabled, false, "healthy 复位后 available 可点击");
  actionResponse = snapshotOf("ready_to_restart");
  holdPost = true;
  toggle.click();
  await settle();
  getFailures = 1;
  await tickPoll();
  assert.equal(intervals.size, 1, "新会话失败预算重置（D5）：1 次失败不停轮");
  getFailures = 0;
  releasePost();
  await settle();
  cleanup();
}

/* 10) pagehide 停轮：fire 在途（busy 经 GET 到达）中 pagehide 后不再轮询 */
{
  const cleanup = await freshInstall();
  await disarmBaseline();
  await refreshAvailable();
  actionResponse = snapshotOf("ready_to_restart");
  holdPost = true;
  toggle.click();
  await settle();
  snapshotQueue = [snapshotOf("downloading")];
  await tickPoll();
  assert.equal(store.update?.state, "downloading", "busy 经 GET 到达");
  assert.ok(intervals.size >= 1, "busy 中存在轮询定时器");
  windowTarget.dispatchEvent(new Event("pagehide"));
  const before = fetches;
  await tickPoll();
  await tickPoll();
  assert.equal(fetches, before, "pagehide 后轮询停止");
  releasePost();
  await settle();
  cleanup();
}

/* 11) E1 armed：用户激活过的操作失败（409 update_restart_blocked）可见；
       单击跳设置更新组；rolled_back 同通道 */
{
  const cleanup = await freshInstall();
  await disarmBaseline();
  await refreshAvailable();
  actionError = "update_restart_blocked";
  actionResponse = null;
  toggle.click();
  await settle();
  /* 409 update_restart_blocked：闭集 toast、无 raw error、回退快照仍可用可重试（D1） */
  snapshotQueue = [snapshotOf("failed", { error_code: "update_restart_blocked" })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, false, "激活过的失败可见");
  assert.equal(toggle.getAttribute("aria-label"), "更新失败，点击进入恢复");
  assert.equal(toggle.disabled, false, "失败后可重试进入恢复");
  assert.ok(byId["toast-region"].children.length >= 1, "动作失败 toast 辅助");
  assert.ok(
    !JSON.stringify(byId["toast-region"].children.map((child) => child.textContent)).includes("update_restart_blocked"),
    "toast 不含闭集码原文",
  );

  const group = byId["settings-update-group"];
  let pageEvent = null;
  const onPage = (event) => { pageEvent = event.detail; };
  windowTarget.addEventListener("courselens:select-page", onPage);
  toggle.click();
  await settle();
  windowTarget.removeEventListener("courselens:select-page", onPage);
  assert.equal(pageEvent, "settings", "failed 单击跳设置页");
  assert.ok((group.scrollIntoViewCalls || 0) >= 1, "定位设置更新组");

  /* rolled_back 同 failed 通道 */
  snapshotQueue = [snapshotOf("rolled_back", { rollback: { available: true, state: "rolled_back", version: "1.0.0" } })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.getAttribute("aria-label"), "已自动回滚，点击查看详情");
  actionError = "";
  cleanup();
}

/* 夜10-A N10B-1 尾巴（#15）：index.html 初始态具名——动态渲染覆写前的可访问名。
   读真实 index.html 静态断言（此 harness 的 DOM 桦不渲染顶栏标记）。 */
{
  const { readFile } = await import("node:fs/promises");
  const html = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
  const anchor = html.indexOf('id="update-toggle"');
  assert.ok(anchor > 0, "update-toggle must be declared in index.html");
  const tag = html.slice(html.lastIndexOf("<button", anchor), html.indexOf(">", anchor) + 1);
  assert.match(tag, /aria-label="客户端更新"/, "initial accessible name required (N10B-1)");
  assert.match(tag, /title="客户端更新"/, "initial tooltip required (N10B-1)");
}

/* 12) D-20261009-04：编排在途门 + 过渡词防闪 + 结果态落地。
     真实形状 A（>300ms 编排）：fire → 300ms 防闪窗后过渡词上脸；在途期间非 busy
     快照（合成壳 available/竞态陈旧快照）不得翻转在途呈现（修前=瞬时复按钮+
     文案回退，学生视角「点了没反应」）；终态发布收口（结果 toast + 正确可读名）。 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  const toastTexts = () => byId["toast-region"].children.map((child) => child.textContent).join("|");
  const mark = timeoutCallbacks.length; /* fire 后首个 setTimeout = 过渡词防闪定时器 */
  actionResponse = snapshotOf("available", { available_version: "1.1.0" });
  holdPost = true;
  toggle.click();
  assert.equal(toggle.disabled, true, "单击同步过渡态（A3 既有）");
  assert.equal(toggle.getAttribute("aria-label"), "发现新版本 1.1.0，点击开始更新", "防闪窗内不提前亮过渡词");
  /* >300ms 防闪窗后：过渡词上脸（诚实前置词，阶段事实仍以快照为准） */
  timeoutCallbacks.splice(mark).forEach((fn) => fn());
  assert.equal(toggle.getAttribute("aria-label"), "正在准备更新…", "防闪窗后过渡词上脸（D-20261009-04）");
  assert.equal(toggle.getAttribute("title"), "正在准备更新…", "tooltip 同步过渡词");
  /* 在途期间 GET 快照仍报 available（合成壳形状/竞态陈旧快照）：不得复按钮、
     不得回退可读名——在途呈现保持，直到编排终态收口 */
  snapshotQueue = [snapshotOf("available", { available_version: "1.1.0" })];
  await tickPoll();
  assert.equal(toggle.disabled, true, "在途门：非 busy 快照不复按钮（D-20261009-04）");
  assert.equal(toggle.getAttribute("aria-busy"), "true", "在途门：aria-busy 保持");
  assert.equal(toggle.hidden, false, "在途门：按钮不因 ambient 快照隐藏");
  assert.equal(toggle.getAttribute("aria-label"), "正在准备更新…", "在途门：过渡词不被 ambient 快照回退");
  /* 编排终态（available=没进安装）：摘在途门 → 结果态人话 + 复按钮 + 正确可读名 */
  releasePost();
  await settle();
  assert.equal(toggle.disabled, false, "终态后复按钮（可重试）");
  assert.equal(toggle.getAttribute("aria-busy"), null, "终态清除 aria-busy");
  assert.equal(toggle.getAttribute("aria-label"), "发现新版本 1.1.0，点击开始更新", "终态可读名回归动作语义");
  assert.ok(toastTexts().includes("更新没有开始，请稍后重新检查。"), "available 终态结果句（闭集原句等值钉）");
  assert.equal(intervals.size, 0, "终态停轮");
  cleanup();
}

/* 13) D-20261009-04：快速完成不闪词（防闪）+ up_to_date 结果句。
     编排毫秒返回（无可装编排/测试桩形状）时过渡词从未上脸，终态直接落地。 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  actionResponse = snapshotOf("up_to_date");
  toggle.click();
  await settle();
  assert.equal(toggle.hidden, true, "up_to_date ambient 隐藏（无可点更新）");
  assert.equal(store.update?.state, "up_to_date", "终态快照发布");
  const toasts = byId["toast-region"].children.map((child) => child.textContent).join("|");
  assert.ok(toasts.includes("已是最新版本，暂时不需要更新。"), "up_to_date 结果句（闭集原句等值钉）");
  assert.ok(!toasts.includes("正在准备更新"), "过渡词不进 toast（快速完成不闪词）");
  assert.notEqual(toggle.getAttribute("aria-label"), "正在准备更新…", "毫秒返回不闪过渡词（防闪）");
  assert.equal(intervals.size, 0, "终态停轮");
  cleanup();
}

/* 14) D-20261009-04：healthy 终态结果句（成功也有人话回音，不再无声隐藏）。 */
{
  const cleanup = await freshInstall();
  await refreshAvailable();
  actionResponse = snapshotOf("healthy");
  toggle.click();
  await settle();
  assert.equal(toggle.hidden, true, "healthy ambient 隐藏（既有）");
  const toasts = byId["toast-region"].children.map((child) => child.textContent).join("|");
  assert.ok(toasts.includes("更新成功。"), "healthy 结果句（闭集原句等值钉）");
  cleanup();
}

/* 15) UPDATE-UX-1 三律：下载在途可读名带百分比（≤2s 快照节拍刷新）；
     无总量（旧快照形状）时诚实回退阶段词，绝不编造百分比。 */
{
  const cleanup = await freshInstall();
  snapshotQueue = [snapshotOf("downloading", { available_version: "1.1.0", download_bytes: 420, download_total: 1000 })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, false, "downloading 可见（既有）");
  assert.equal(toggle.getAttribute("aria-label"), "正在下载更新… 42%", "下载可读名带百分比（三律）");
  assert.equal(toggle.getAttribute("title"), "正在下载更新… 42%", "tooltip 同步百分比");
  snapshotQueue = [snapshotOf("downloading", { available_version: "1.1.0" })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.getAttribute("aria-label"), "正在下载更新…", "无总量诚实回退阶段词");
  cleanup();
}

/* 16) UPDATE-UX-1 mac：检查道发现新版本→顶栏点亮（与 Windows available 同款
     醒目态）+ 单击定位设置更新组（绝不 fire Windows 的 update_now 编排）。 */
{
  setNavigator(MAC_UA);
  actionPost = []; /* 前块编排动作出账：mac 面从零断言 */
  macReleasesBody = JSON.stringify([{ draft: false, tag_name: "client-v1.1.0-macos-test" }]);
  const selectEvents = [];
  const onSelect = (event) => selectEvents.push(event.detail);
  windowTarget.addEventListener("courselens:select-page", onSelect);
  const cleanup = await freshInstall();
  await settle();
  await settle();
  assert.equal(toggle.hidden, false, "mac available 顶栏点亮（醒目提醒）");
  assert.equal(toggle.dataset.updateState, "available", "mac 复用 available dataset（金点样式零新 CSS）");
  assert.equal(toggle.getAttribute("aria-label"), "发现新版本 1.1.0，点击查看下载", "mac 可读名闭集");
  assert.equal(toggle.disabled, false, "mac 按钮可点击");
  assert.equal(dot.hidden, false, "mac 状态点呈现");
  toggle.click();
  await settle();
  assert.deepEqual(selectEvents, ["settings"], "mac 单击=定位设置页");
  assert.deepEqual(actionPost, [], "mac 单击绝不 fire Windows 编排");
  assert.ok(byId["settings-update-group"].scrollIntoViewCalls >= 1, "定位滚动到更新组");
  windowTarget.removeEventListener("courselens:select-page", onSelect);
  cleanup();
  setNavigator("");
}

/* 17) UPDATE-UX-1 mac：无新版/检查失败=顶栏安静；Windows 快照在 mac 宿主不
     点亮（检查道唯一事实源）；手动「检查更新」点击即反馈+闭集结果句。 */
{
  setNavigator(MAC_UA);
  actionPost = [];
  macReleasesBody = JSON.stringify([{ draft: false, tag_name: "client-v1.0.0-macos-test" }]);
  const cleanup = await freshInstall();
  await settle();
  await settle();
  assert.equal(toggle.hidden, true, "mac up_to_date 顶栏安静");
  snapshotQueue = [snapshotOf("available", { available_version: "9.9.9", actions: ["update_now"] })];
  windowTarget.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, true, "Windows available 快照在 mac 宿主不点亮");
  const checkButton = byId["update-mac-check"];
  checkButton.disabled = false;
  checkButton.click();
  const toasts = () => byId["toast-region"].children.map((child) => child.textContent).join("|");
  assert.ok(toasts().includes("正在检查更新…"), "点击即反馈（≤100ms 三律）");
  await settle();
  await settle();
  assert.ok(toasts().includes("检查完成：已是最新版本。"), "up_to_date 结果句闭集");
  macReleasesBody = null;
  checkButton.click();
  await settle();
  await settle();
  assert.ok(toasts().includes("检查没有完成：稍后再试，或直接打开下载页确认。"), "失败诚实降级句闭集");
  assert.equal(toggle.hidden, true, "失败态顶栏保持安静");
  assert.deepEqual(actionPost, [], "mac 检查道零后端编排");
  cleanup();
  setNavigator("");
}

console.log("frontend update widget behavior passed");
