import assert from "node:assert/strict";

/* CLIENT-STATE 状态及时性行为测试（用户实证痛点：「某些连接状态要人为点击/
   刷新才能显示正确状态」）。桩件法与 tests/frontend_tasks_center_behavior.mjs
   同源：真实 store / tasks-drawer / shell / settings 模块 + FakeElement /
   fetch / EventSource 桩，直接驱动事件，不建新框架。钉三件事：
   ① 任务抽屉应用级 SSE 订阅 automation 主题——自动材料运行记录事件即时刷新
     抽屉（此前只靠其它主题顺带触发）；
   ② remote-connection 主题向窗口广播 courselens:remote-connection-changed——
     页眉 GitHub 连接点随事件即时复核快照（此前只在装配与点开连接菜单时读）；
   ③ 网络层 online/offline 即时呈现；设置页正在前台时连接卡随同一事件刷新
     （此前只在进入设置页或动作后刷新），页面不在前台不取数；
   ④ 读屏面（D14 P2-1 钉）：连接触发钮 aria-label 每次渲染按 CONN_TEXT 闭集
     同步两服务人话态，GitHub 广播/复旦 auth 刷新的翻转即换话，sr-only live
     区（index.html #conn-live）同拍同步——纯视觉圆点的状态变化对读屏可闻。
   ⑤ 周期复验兜底（CONN-STALE-R2 钉）：无 SSE 广播的后台翻转（快照读时派生：
     TTL 过期/令牌过期推导）在 60s 复验拍自动上屏；健康签名不变零 DOM 写入；
     离线拍零外联；拆绑后复验定时器对称拆除。 */

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
    if (this.parent) {
      const index = this.parent.children.indexOf(this);
      if (index >= 0) this.parent.children.splice(index, 1);
      this.parent = null;
    }
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  scrollIntoView() {}
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

function datasetKey(name) {
  return String(name).replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

/* 支持 TAG / .class / [attr] / [attr="value"] 与一层空格后代组合 */
function elementMatches(node, selector) {
  const text = String(selector).trim();
  const parts = text.split(/\s+/);
  const last = parts[parts.length - 1];
  if (parts.length > 1) {
    const ancestorSelector = parts.slice(0, -1).join(" ");
    let ancestor = node.parent;
    while (ancestor) {
      if (elementMatches(ancestor, ancestorSelector)) break;
      ancestor = ancestor.parent;
    }
    if (!ancestor) return false;
  }
  const attrMatch = last.match(/^([a-zA-Z]*)\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/);
  if (attrMatch) {
    const [, tag, attrName, attrValue] = attrMatch;
    if (tag && node.tagName !== tag.toUpperCase()) return false;
    const key = attrName.startsWith("data-") ? node.dataset?.[datasetKey(attrName)] : node.attributes.get(attrName);
    if (attrValue === undefined) return key !== undefined && key !== null;
    return String(key) === attrValue;
  }
  if (last.startsWith(".")) {
    return String(node.className || "").split(/\s+/).includes(last.slice(1));
  }
  return node.tagName === last.toUpperCase();
}

/* ---- 页面装配：tasks-drawer + shell + settings 安装面所需的最小 id 集 ---- */
const BUTTON_IDS = new Set([
  "task-chip", "close-task-drawer", "refresh-tasks",
  "wordmark", "live-entry", "theme-toggle", "settings-close",
  "account-button", "account-menu-settings", "account-menu-login", "account-menu-logout",
  "conn-status", "campus-diagnostics-run", "campus-connection-tun-dismiss",
  "close-login", "cancel-login",
  "check-update", "download-update", "install-update",
  "open-login", "logout-button", "save-deepseek", "delete-deepseek", "save-network",
  "diagnose-network", "copy-diagnostics", "remote-reconcile-mailbox", "remote-rotate-keys",
  "remote-rotate-cancel", "remote-rotate-confirm", "cloud-revoke-credentials",
  "mailbox-reconcile-cancel", "mailbox-reconcile-confirm", "login-submit", "login-use-saved",
  "login-manual-mode", "login-back-saved", "toggle-password-visibility", "privacy-doc-open",
  "client-reset-open", "client-reset-cancel", "client-reset-confirm",
  "data-refresh", "data-select-all", "data-clear-selection", "data-clear-orphans",
  "data-action-rebuild-search", "data-action-purge-derived", "data-action-remove-copies",
  "data-action-export", "data-action-delete-records", "data-action-release-stuck",
  "data-confirm-cancel", "data-confirm-confirm", "client-reset-dialog-status",
]);
const IDS = [
  /* 任务抽屉面 */
  "workspace-main", "toast-region", "tasks-root", "task-drawer", "task-counts", "task-list",
  "task-chip-count", "task-chip-dot", "task-chip-failed", "remote-drawer-state",
  "task-completion-summary", "task-live",
  /* 页眉连接/账户/菜单面 */
  "service-banner", "conn-menu", "conn-live", "account-menu", "account-text",
  "account-session-state", "account-list", "account-menu-data",
  "live-entry-dot", "global-status", "login-dialog", "login-student-id", "login-dialog-title",
  "login-dialog-hint", "login-status", "login-error", "login-form", "login-saved",
  "login-saved-options", "login-saved-status", "login-auto-connect", "login-password",
  "remember-account", "login-manual-actions", "login-manual-fields",
  /* 校园连接卡（title/pill 已随 AS4-U2b 退役，fake 同步拆除——CAMPUS-FIX 剩留清理） */
  "campus-connection", "campus-connection-state",
  "campus-connection-reason", "campus-connection-regenerated", "campus-connection-actions",
  "campus-connection-details", "campus-connection-path", "campus-connection-checked",
  "campus-connection-services", "campus-connection-live", "campus-connection-tun",
  "campus-diagnostics", "campus-diagnostics-summary", "campus-diagnostics-checked",
  "campus-diagnostics-latency", "campus-diagnostics-fallback", "campus-diagnostics-action",
  "campus-diagnostics-status",
  /* 设置页 */
  "settings-page", "settings-evidence", "proxy-url-row", "proxy-url", "network-mode",
  "privacy-evidence", "privacy-doc-body", "privacy-doc-dialog", "deepseek-key",
  "remember-deepseek", "deepseek-save-state", "deepseek-budget-presets",
  "max-deepseek-tokens-label", "max-deepseek-tokens-state", "network-evidence",
  "settings-timetable-evidence", "settings-theme-mode", "update-background-checks",
  "media-stream-proxy", "fudan-auto-connect", "fudan-auto-connect-account",
  "fudan-auto-connect-status", "github-auto-connect", "github-auto-connect-status",
  "fudan-connection-state", "github-connection-state", "github-phases", "github-connection-title",
  "security-evidence", "remote-evidence", "remote-primary-action", "remote-component-evidence",
  "remote-action-error", "remote-action-progress", "remote-install-wait", "remote-tighten-wait",
  "remote-mailbox-reconcile", "remote-mailbox-reconcile-hint", "remote-mailbox-reconcile-state",
  "mailbox-reconcile-dialog", "remote-rotate-row", "remote-rotate-dialog", "remote-rotate-hint",
  "remote-rotate-dialog-title", "remote-device-authorization", "remote-compute-row",
  "remote-compute-state", "remote-compute-hint", "cloud-revoke-row", "cloud-revoke-hint",
  "update-state", "update-facts-line", "update-last-check", "update-notes", "update-error", "update-recovery", "update-live",
  "update-toggle", "ai-usage-month",
  /* A11Y-IMPL-4 在途产品步（界面字号三态）要求的最小桩件：settings 装配面
     现引用 settings-ui-font（CONN-STALE-R2 同桩解堵，属机械桩非行为钉）。 */
  "settings-ui-font",
  /* 数据管理页（settings 组合根安装） */
  "data-page", "data-evidence", "data-summary", "data-recovery", "data-recovery-title",
  "data-recovery-impact", "data-recovery-actions", "data-bulk-bar", "data-selection-count",
  "data-result", "data-course-list", "data-detail", "data-search", "data-category-filter",
  "data-bulk-hint", "data-bulk-actions", "data-confirm-dialog", "data-confirm-form",
  "data-confirm-title", "data-confirm-hint", "data-confirm-input", "data-orphans-filter",
  "client-reset-dialog", "client-reset-form", "client-reset-title", "client-reset-result",
  "client-reset-error", "client-reset-status", "client-reset-confirm-input",
  "client-reset-delete-derived", "client-reset-delete-repos",
];
const byId = Object.fromEntries([...IDS, ...BUTTON_IDS].map((id) => [id, new FakeElement(BUTTON_IDS.has(id) ? "button" : "div", id)]));
byId["settings-page"].hidden = true;
byId["tasks-root"].hidden = true;
byId["task-completion-summary"].hidden = true;
byId["conn-menu"].hidden = true;
byId["account-menu"].hidden = true;
byId["update-toggle"].hidden = true;
byId["data-bulk-bar"].hidden = true;
byId["data-recovery"].hidden = true;
byId["data-result"].hidden = true;
byId["data-orphans-filter"].hidden = true;
byId["task-chip"].dataset.openTasks = "";
byId["task-chip"].setAttribute("aria-expanded", "false");
byId["conn-status"].setAttribute("aria-expanded", "false");
byId["conn-status"].dataset.serviceAction = "";
/* P2-1 钉测面：真实壳 index.html 的连接触发钮带 data-open-conn（renderConn 以
   querySelectorAll("[data-open-conn]") 落动态 aria-label）——桩件与真实壳同形。 */
byId["conn-status"].dataset.openConn = "";

/* 连接点：页眉 fudan/github 两颗（renderConn 用 querySelectorAll("[data-conn-dot]") 消费） */
const fudanDot = new FakeElement("span", "conn-dot-fudan");
fudanDot.dataset.connDot = "fudan";
const githubDot = new FakeElement("span", "conn-dot-github");
githubDot.dataset.connDot = "github";
const connText = new FakeElement("span", "conn-text-fudan");
connText.dataset.connText = "fudan";
const connTextGithub = new FakeElement("span", "conn-text-github");
connTextGithub.dataset.connText = "github";

const settingsNav = new FakeElement("nav", "settings-nav-fake");
settingsNav.className = "settings-nav";
const SETTINGS_GROUP_IDS = [
  "settings-account-group", "settings-ai-group", "settings-automation-group",
  "settings-network-group", "settings-timetable-group", "settings-update-group",
  "settings-reset-group",
];
const registry = [
  ...Object.values(byId), fudanDot, githubDot, connText, connTextGithub, settingsNav,
];
SETTINGS_GROUP_IDS.forEach((groupId) => {
  const group = new FakeElement("section", groupId);
  group.className = "settings-group";
  const heading = new FakeElement("h2", `${groupId}-heading`);
  heading.setAttribute("tabindex", "-1");
  group.append(heading);
  const navButton = new FakeElement("button", `settings-nav-${groupId}`);
  navButton.type = "button";
  navButton.dataset.settingsTarget = groupId;
  settingsNav.append(navButton);
  registry.push(group, heading, navButton);
});
/* 任务抽屉最小结构 */
const drawerScrim = new FakeElement("div", "tasks-scrim");
drawerScrim.dataset.tasksScrim = "";
byId["tasks-root"].append(drawerScrim, byId["task-drawer"]);
byId["task-drawer"].append(byId["close-task-drawer"], byId["refresh-tasks"], byId["task-list"]);
/* 更新小组件的内联图标容器（renderWidget querySelector 探测） */
byId["update-toggle"].append(new FakeElement("svg", "update-icon-install"), new FakeElement("svg", "update-icon-restart"));

let createdSeq = 0;
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => {
    createdSeq += 1;
    const node = new FakeElement(tag, `created-${createdSeq}`);
    registry.push(node);
    return node;
  },
  querySelectorAll: (selector) => registry.filter((node) => elementMatches(node, selector)),
  querySelector: (selector) => registry.find((node) => elementMatches(node, selector)) || null,
  activeElement: null,
  documentElement: { dataset: {} },
});

const windowTarget = new EventTarget();
windowTarget.setTimeout = () => 0;
windowTarget.clearTimeout = () => {};
/* CONN-STALE-R2：setInterval 记录回调但不自动发火——既有场景行为不变（装上即
   静默），新⑤幕按行为（驱动 remote 取数）定位周期复验定时器并直驱其拍。 */
const intervalEntries = [];
let intervalSeq = 0;
windowTarget.setInterval = (fn) => {
  intervalSeq += 1;
  intervalEntries.push({ id: intervalSeq, fn });
  return intervalSeq;
};
windowTarget.clearInterval = (id) => {
  const index = intervalEntries.findIndex((entry) => entry.id === id);
  if (index >= 0) intervalEntries.splice(index, 1);
};
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});

/* ---- SSE 桩：记录 URL 与监听，测试直驱 emit ---- */
let eventSourceInstance = null;
globalThis.EventSource = class {
  constructor(url) {
    this.url = String(url);
    this.listeners = new Map();
    this.closed = false;
    eventSourceInstance = this;
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  emit(type) {
    (this.listeners.get(type) || []).forEach((listener) => listener({ type }));
  }

  close() { this.closed = true; }
};

/* ---- 网络桩：按路由计数，remote-connection 载荷可变 ---- */
const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);
const fetchCounts = new Map();
const countFetch = (route) => fetchCounts.set(route, (fetchCounts.get(route) || 0) + 1);
const fetchCount = (route) => fetchCounts.get(route) || 0;
let remotePayload = { overall: { state: "unknown", code: "" } };
let authPayload = { state: "ready", code: "fudan_session_verified", actions: ["logout"], connected: true, configured: true };
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (route === "/api/health") {
    countFetch("health");
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }
  if (method === "POST" && route === "/api/v3/frontend-session") return okEnvelope({ session_id: "s" });
  if (method === "GET" && route === "/api/v3/authentication") {
    countFetch("auth");
    return okEnvelope(authPayload);
  }
  if (method === "GET" && route.startsWith("/api/v3/remote-connection")) {
    countFetch("remote");
    return okEnvelope(remotePayload);
  }
  if (method === "GET" && route === "/api/v3/tasks") {
    countFetch("tasks");
    return okEnvelope({ tasks: [], counts: { active: 0, failed: 0, completed: 0 } });
  }
  if (method === "GET" && route === "/api/v3/automation") {
    countFetch("automation");
    return okEnvelope({ enabled: false, rules: [], runs: [], schedule: {} });
  }
  if (method === "GET" && route === "/api/v3/settings") return okEnvelope({});
  if (method === "GET" && route === "/api/v3/accounts") return okEnvelope({ accounts: [] });
  if (method === "GET" && route === "/api/v3/client-update") {
    countFetch("update");
    return okEnvelope({ state: "idle" });
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

const settle = async () => {
  for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

/* ---- 装配（真实模块） ---- */
const { store } = await import("../frontend/modules/store.js");
const drawer = await import("../frontend/modules/tasks-drawer.js");
const shell = await import("../frontend/modules/shell.js");
const settings = await import("../frontend/modules/settings.js");

const relayEvents = [];
windowTarget.addEventListener("courselens:remote-connection-changed", () => {
  relayEvents.push("courselens:remote-connection-changed");
});

/* ---- ① 任务抽屉：SSE automation 主题 + remote-connection 广播 ---- */
await drawer.installTasksDrawer(store);
await settle();
assert.ok(eventSourceInstance, "任务抽屉装配即建立应用级 SSE");
assert.equal(
  eventSourceInstance.url,
  "/api/v3/events?topics=remote-connection,remote-runs,tasks,automation",
  "SSE 订阅含 automation 主题（自动材料运行记录即时性）",
);
assert.equal(fetchCount("automation"), 1, "装配时拉一次自动材料快照");

fetchCounts.set("automation", 0);
fetchCounts.set("tasks", 0);
fetchCounts.set("remote", 0);
eventSourceInstance.emit("automation");
await settle();
assert.equal(fetchCount("automation"), 1, "automation 事件即时刷新自动材料快照");
assert.equal(fetchCount("tasks"), 1, "automation 事件同拍走既有抽屉刷新编排");

relayEvents.length = 0;
const remoteBaseDrawer = fetchCount("remote");
eventSourceInstance.emit("remote-connection");
await settle();
assert.equal(fetchCount("remote"), remoteBaseDrawer + 1, "remote-connection 事件刷新抽屉连接行");
assert.deepEqual(relayEvents, ["courselens:remote-connection-changed"], "remote-connection 主题向窗口广播");

/* ---- ② 页眉 GitHub 连接点：随广播即时复核 ---- */
const shellCleanup = await shell.installShell(store);
await settle();
const githubDotState = () => String(githubDot.dataset.state || "");
assert.equal(githubDotState(), "checking", "装配后未知态呈现 checking");

remotePayload = { overall: { state: "offline", code: "" } };
const remoteBase = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteBase + 1, "广播到达即复核连接快照（零人为点击）");
assert.equal(githubDotState(), "off", "offline 态即时上屏（安静 off）");

remotePayload = { overall: { state: "ready", code: "" } };
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(githubDotState(), "ready", "ready 态即时上屏");

/* 网络层 online/offline 同享即时呈现；offline 判定零外联 */
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: false, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
remotePayload = { overall: { state: "ready", code: "" } };
const remoteBeforeOffline = fetchCount("remote");
windowTarget.dispatchEvent(new Event("offline"));
await settle();
assert.equal(fetchCount("remote"), remoteBeforeOffline, "offline 判定零外联");
assert.equal(githubDotState(), "off", "本机离线即时呈现");

Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
remotePayload = { overall: { state: "degraded", code: "" } };
/* REALRUN-1 旅程6：恢复在线边沿即刻取一次复旦会话证据（提前一拍既有
   GET authentication 轮询），断网期的 degraded/陈读即时换新；离线边沿零外联。 */
const authBeforeOnline = fetchCount("auth");
windowTarget.dispatchEvent(new Event("online"));
await settle();
assert.equal(fetchCount("auth"), authBeforeOnline + 1, "恢复在线边沿即刻刷复旦会话证据（REALRUN-1 旅程6）");
assert.equal(githubDotState(), "action", "恢复在线即时复核（degraded→action）");
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: false, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
windowTarget.dispatchEvent(new Event("offline"));
await settle();
assert.equal(fetchCount("auth"), authBeforeOnline + 1, "离线边沿零外联（auth 不取数）");
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
windowTarget.dispatchEvent(new Event("online"));
await settle();
assert.equal(fetchCount("auth"), authBeforeOnline + 2, "再次恢复在线再刷一拍");

/* ---- ③ 设置页连接卡：前台随广播刷新，后台不取数；cleanup 对称拆绑 ---- */
const remoteBeforeSettings = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteBeforeSettings + 1, "页眉仍在监听（shell 未拆绑时广播仍生效）");

const settingsCleanup = await settings.installSettings(store);
await settle();

/* 后台（设置页隐藏）：广播到达不取数 */
remotePayload = { overall: { state: "action_required", code: "" } };
byId["settings-page"].hidden = true;
let remoteBaseHidden = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteBaseHidden + 1, "广播仅页眉复核（设置页后台不取数）");
assert.notEqual(String(byId["github-connection-state"].textContent), "已连接", "设置页后台不渲染连接卡");

/* 前台（设置页可见）：广播到达即刷新连接卡 */
byId["settings-page"].hidden = false;
remotePayload = { overall: { state: "ready", code: "" } };
remoteBaseHidden = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteBaseHidden + 2, "页眉 + 设置页各恰一次取数");
assert.equal(String(byId["github-connection-state"].textContent), "已连接", "连接卡前台即时翻绿");

/* cleanup 对称拆绑：拆绑后广播不再驱动设置页 */
settingsCleanup();
remotePayload = { overall: { state: "degraded", code: "" } };
remoteBaseHidden = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteBaseHidden + 1, "设置页拆绑后不再消费广播（页眉仍恰一次）");

/* ---- ④ 读屏面（D14 P2-1 钉）：连接触发钮 aria-label 随状态翻转同步两服务人话态 ----
   renderConn 每次渲染以 CONN_TEXT 闭集组装「连接状态：复旦…；GitHub…」写入
   [data-open-conn] 触发钮可访问名，并同步 sr-only live 区 #conn-live——顶栏
   圆点纯视觉，读屏学生从触发钮名听到两服务真实状态。 */
const connLabel = () => String(byId["conn-status"].getAttribute("aria-label") || "");
assert.ok(connLabel().startsWith("连接状态："), "触发钮可访问名按闭集组装（非静态裸名「连接状态」）");
assert.ok(connLabel().includes("GitHub 需要授权：授权后 Worker 将自动初始化"), "aria-label 含 GitHub 当前态人话（degraded→action）");
assert.equal(byId["conn-live"].textContent, connLabel(), "sr-only live 区与触发钮名同拍同步");

remotePayload = { overall: { state: "ready", code: "" } };
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.ok(connLabel().includes("GitHub 远程连接正常"), "GitHub 翻 ready 后触发钮名即时换话（渲染链读屏可达）");
assert.ok(connLabel().includes("复旦"), "复旦话仍在同一可访问名内（两服务合一）");

authPayload = { state: "action_required", code: "", actions: ["login"], connected: false, configured: true };
windowTarget.dispatchEvent(new Event("courselens:auth-refresh"));
await settle();
assert.ok(connLabel().includes("复旦会话需要处理，请重新认证"), "复旦翻转（store.auth 渲染链）后触发钮名即时换话");
assert.ok(connLabel().includes("GitHub 远程连接正常"), "复旦翻转不动 GitHub 话（两服务各自独立成句）");
assert.equal(byId["conn-live"].textContent, connLabel(), "live 区随复旦翻转同步（状态变化读屏可闻）");

/* shell cleanup 对称拆绑 */

/* ---- ⑤ 周期复验兜底（CONN-STALE-R2，2026-10-07 用户实证复发钉）：SSE 只覆盖
   probe 驱动的翻转——快照读时派生的翻转（观测证据 TTL 过期、令牌过期在读取
   瞬间推导）不落 remote 事件、SSE 静默，此后台翻转必须在复验拍自动上屏；
   健康签名不变时零 DOM 写入；离线拍零外联；拆绑后复验定时器对称拆除。 ---- */
remotePayload = { overall: { state: "action_required", code: "" } };
const remoteBeforeScan = fetchCount("remote");
let recheckEntry = null;
for (const entry of [...intervalEntries]) {
  await entry.fn();
  await settle();
  if (fetchCount("remote") > remoteBeforeScan) {
    recheckEntry = entry;
    break;
  }
}
assert.ok(recheckEntry, "装配面存在周期复验定时器（直驱可产生 remote 取数）");
assert.equal(githubDotState(), "action", "无 SSE 广播的后台翻转在复验拍自动上屏（免人为点击）");

/* 健康签名不变：复验拍照常取数但零 DOM 写入（renderConn 未触发） */
let connLabelWrites = 0;
const connStatusNode = byId["conn-status"];
const origConnSetAttribute = connStatusNode.setAttribute;
connStatusNode.setAttribute = function (name, value) {
  connLabelWrites += 1;
  return origConnSetAttribute.call(this, name, value);
};
const remoteBeforeIdleTick = fetchCount("remote");
await recheckEntry.fn();
await settle();
assert.equal(fetchCount("remote"), remoteBeforeIdleTick + 1, "状态未迁移时复验拍仍照常取数（活性证明）");
assert.equal(connLabelWrites, 0, "状态未迁移零 DOM 写入（沿 renderCampusConnection 零写先例）");
connStatusNode.setAttribute = origConnSetAttribute;

/* 离线拍零外联，恢复在线后下一拍回迁移 */
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: false, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
const remoteBeforeOfflineTick = fetchCount("remote");
await recheckEntry.fn();
await settle();
assert.equal(fetchCount("remote"), remoteBeforeOfflineTick, "离线拍零外联（navigator.onLine 先判）");
assert.equal(githubDotState(), "off", "离线态随复验拍上屏");
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true, clipboard: { writeText: async () => {} }, sendBeacon: () => true },
  configurable: true,
});
await recheckEntry.fn();
await settle();
assert.equal(githubDotState(), "action", "恢复在线后复验拍回迁移（offline→action）");

shellCleanup();
await settle();
const remoteAfterShellCleanup = fetchCount("remote");
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
assert.equal(fetchCount("remote"), remoteAfterShellCleanup, "shell 拆绑后广播零消费（零泄漏监听）");

/* 复验定时器随 closeSession 拆除：剩余装配面定时器（抽屉/更新小组件等）任拍零 remote 取数 */
const remoteAfterTeardownBase = fetchCount("remote");
for (const entry of [...intervalEntries]) {
  await entry.fn();
  await settle();
}
assert.equal(fetchCount("remote"), remoteAfterTeardownBase, "shell 拆绑后周期复验已拆除（剩余定时器零 remote 取数）");

console.log("frontend state timeliness behavior passed");
