import assert from "node:assert/strict";

/* GH-UX-REWORK-1 首跑 GitHub 链接全链行为测试（用户实测痛点：反复点击/反复
   登录网页/状态不及时）。桩件法与 tests/frontend_state_timeliness_behavior.mjs
   同源：真实 store + settings（remote-panel）模块 + FakeElement / fetch 桩，
   直接驱动点击与轮询链，不建新框架。钉六件事：
   ① worker_setup_incomplete（授权在案而初始化未完成，崩溃/重启遗留）渲染期
     自动续跑 bootstrap 恰一次（此前须手点「创建专属仓库并完成初始化」）；
   ② 设备码授权点击即本地反馈（「正在向 GitHub 请求验证码…」，不等 POST）；
   ③ 授权确认（pending_bootstrap）后自动发起 bootstrap 恰一次——零点击进入
     初始化，设备码区即刻呈现「正在初始化专属仓库…」（此前整个 bootstrap
     期间 UI 停在「等待 GitHub 授权确认」）；
   ④ 安装引导 setup URL 自动打开即对称布防安装等待（3s 新鲜轮询），检测到
     安装自动继续初始化、初始化收口即自动加密测试——除输码与点「授权」外
     全链零必要点击；
   ⑤ 验证码过期自动重发恰 2 次（新码呈现后直接输码即可，不再落入
     「expired + 手动重授权」死端），第 3 次过期后停手；
   ⑥ pending 呈现剩余有效期（「分钟内有效」），倒计时来自后端闭集 expires_at。 */

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, enabled) {
    if (enabled === undefined) {
      if (this.values.has(value)) this.values.delete(value);
      else if (enabled) this.values.add(value);
      else this.values.delete(value);
      return this.values.has(value);
    }
    if (enabled) this.values.add(value);
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

  prepend(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.unshift(node);
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
  /* 任务抽屉/页眉面（settings 组合根可能触达的最小桩集） */
  "workspace-main", "toast-region", "tasks-root", "task-drawer", "task-counts", "task-list",
  "task-chip-count", "task-chip-dot", "task-chip-failed", "remote-drawer-state",
  "task-completion-summary", "task-live",
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
  "settings-ui-font",
  /* 数据管理页（settings 组合根安装） */
  "data-page", "data-evidence", "data-summary", "data-recovery", "data-recovery-title",
  "data-recovery-impact", "data-recovery-actions", "data-bulk-bar", "data-selection-count",
  "data-result", "data-course-list", "data-detail", "data-search", "data-category-filter",
  "data-bulk-hint", "data-bulk-actions", "data-confirm-dialog", "data-confirm-form",
  "data-confirm-title", "data-confirm-hint", "data-confirm-input", "data-orphans-filter",
  "client-reset-dialog", "client-reset-form", "client-reset-title", "client-reset-result",
  "client-reset-error", "client-reset-status", "client-reset-confirm-input",
  "client-reset-delete-derived", "client-reset-delete-repos", "datamap-error",
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
byId["conn-status"].dataset.openConn = "";

const settingsNav = new FakeElement("nav", "settings-nav-fake");
settingsNav.className = "settings-nav";
const SETTINGS_GROUP_IDS = [
  "settings-account-group", "settings-ai-group", "settings-automation-group",
  "settings-network-group", "settings-timetable-group", "settings-update-group",
  "settings-reset-group",
];
const registry = [...Object.values(byId), settingsNav];
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
/* 轮询链/等待窗定时器加速：产品语义间隔原样传入，测试以 2ms 心跳推进 */
windowTarget.setTimeout = (fn, ms) => setTimeout(fn, Math.min(Number(ms) || 0, 2));
windowTarget.clearTimeout = (id) => clearTimeout(id);
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
windowTarget.requestAnimationFrame = (fn) => fn();
const openedUrls = [];
windowTarget.open = (url) => {
  openedUrls.push(String(url));
  return { closed: false };
};
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
if (!globalThis.Event) globalThis.Event = Event;
if (!globalThis.CustomEvent) globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
};

const okEnvelope = (data) => new Response(
  JSON.stringify({ schema: "courselens.api.v3", data }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);

/* ---- 可编程后端：快照提供者 + 动作响应队列 ---- */
let snapshotProvider = () => ({});
let actionResponder = (action, body, index) => ({ operation: { state: "accepted", result: {} } });
const postedActions = [];
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route.startsWith("/api/v3/remote-connection")) {
    return okEnvelope(snapshotProvider());
  }
  if (method === "POST" && route === "/api/v3/remote-connection/actions") {
    const body = JSON.parse(options.body || "{}");
    const action = String(body.action || "");
    const index = postedActions.filter((item) => item === action).length;
    postedActions.push(action);
    return okEnvelope(actionResponder(action, body, index));
  }
  if (method === "GET" && route === "/api/v3/settings") return okEnvelope({});
  if (method === "GET" && route === "/api/v3/accounts") return okEnvelope({ accounts: [] });
  if (method === "GET" && route === "/api/v3/client-update") return okEnvelope({ state: "idle" });
  if (method === "GET" && route === "/api/v3/automation") {
    return okEnvelope({ enabled: false, rules: [], runs: [], schedule: {} });
  }
  if (method === "GET" && route === "/api/v3/data-map") {
    return okEnvelope({ schema: "unknown" });
  }
  if (method === "GET" && route === "/api/v3/deepseek-balance") return okEnvelope({});
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

const settle = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setTimeout(resolve, 3));
  }
};
/* 仅排空微任务（POST 响应处理），不推进任何定时器——用于在加速轮询链
   的下一拍到来前钉住「pending 呈现」的中间断言 */
const drainMicrotasks = async () => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
};
/* 桩件 textContent 不含子节点文本（与真实 DOM 不同）：递归收集整棵子树 */
const collectTextTree = (node, depth = 0) => `${node.textContent ?? ""}\n${
  (node.children || []).map((child) => collectTextTree(child, depth + 1)).join("")
}`;
const deviceAreaText = () => collectTextTree(deviceArea());

/* ---- 闭集快照形态 ---- */
const PRESELECTED_URL = "https://github.com/apps/fudan-courselens/installations/new/permissions?suggested_target_id=7&repository_ids[]=11&repository_ids[]=12";
const VERIFICATION_URI = "https://github.com/login/device";
const component = (name, state, code, evidence = {}) => ({
  component: name, name, state, code, actions: [], evidence, stale: false,
});
const SNAP_AUTH_MISSING = {
  overall: { state: "action_required", code: "authorization_missing", activity: "idle", ready_for_dispatch: false },
  components: [component("authorization", "action_required", "authorization_missing")],
  configured_repositories: { worker: "", mailbox: "" },
};
const SNAP_SETUP_INCOMPLETE = {
  overall: { state: "unknown", code: "worker_setup_incomplete", activity: "idle", ready_for_dispatch: false },
  components: [component("authorization", "ready", "authorization_valid")],
  configured_repositories: { worker: "", mailbox: "" },
};
const SNAP_INSTALL_MISSING = {
  overall: { state: "action_required", code: "installation_missing", activity: "idle", ready_for_dispatch: false },
  components: [
    component("authorization", "ready", "authorization_valid"),
    component("installation", "action_required", "installation_missing", { installation_setup_url: PRESELECTED_URL }),
  ],
  configured_repositories: { worker: "student/Fudan-CourseLens-Worker", mailbox: "student/Fudan-CourseLens-Mailbox" },
};
const SNAP_INSTALL_PRESENT = {
  overall: { state: "unknown", code: "worker_setup_incomplete", activity: "idle", ready_for_dispatch: false },
  components: [
    component("authorization", "ready", "authorization_valid"),
    component("installation", "ready", "installation_present"),
  ],
  configured_repositories: { worker: "student/Fudan-CourseLens-Worker", mailbox: "student/Fudan-CourseLens-Mailbox" },
};
const SNAP_CHANNEL_REQUIRED = {
  overall: { state: "action_required", code: "channel_test_required", activity: "idle", ready_for_dispatch: false },
  components: [
    component("authorization", "ready", "authorization_valid"),
    component("installation", "ready", "installation_present"),
    component("channel_test", "action_required", "channel_test_required"),
  ],
  configured_repositories: { worker: "student/Fudan-CourseLens-Worker", mailbox: "student/Fudan-CourseLens-Mailbox" },
};
const SNAP_READY = {
  overall: { state: "ready", code: "ready_for_dispatch", activity: "idle", ready_for_dispatch: true },
  components: [
    component("authorization", "ready", "authorization_valid"),
    component("installation", "ready", "installation_present"),
    component("channel_test", "ready", "channel_test_valid"),
  ],
  configured_repositories: { worker: "student/Fudan-CourseLens-Worker", mailbox: "student/Fudan-CourseLens-Mailbox" },
};

/* ---- 装配（真实模块） ---- */
const { store } = await import("../frontend/modules/store.js");
const settings = await import("../frontend/modules/settings.js");

const deviceArea = () => byId["remote-device-authorization"];
const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON") || null;
const countAction = (action) => postedActions.filter((item) => item === action).length;

/* ---- 幕一：worker_setup_incomplete 渲染期自动续跑（恰一次，零点击） ---- */
snapshotProvider = () => SNAP_SETUP_INCOMPLETE;
actionResponder = (action) => {
  if (action === "bootstrap") {
    snapshotProvider = () => SNAP_AUTH_MISSING;
    return { operation: { state: "accepted", result: { setup_state: "complete" } } };
  }
  return { operation: { state: "accepted", result: {} } };
};
const settingsCleanup = await settings.installSettings(store);
byId["settings-page"].hidden = false; /* 真实壳 selectPage 即解除 hidden（SSE 刷新门的前提） */
windowTarget.dispatchEvent(new CustomEvent("courselens:page", { detail: "settings" }));
await settle();
assert.equal(countAction("bootstrap"), 1, "幕一：授权在案而初始化未完成 → 渲染期自动续跑 bootstrap 恰一次");
assert.equal(countAction("start-authorization"), 0, "幕一：不触碰授权（零新增登录面）");

/* ---- 幕二：授权→初始化→安装→初始化收口→加密测试 全链零必要点击 ---- */
let pollPhase = 0;
let holdPollsAfterAuthorized = false;
let releaseHeldPoll = null;
let secondBootstrapFired = false;
actionResponder = (action) => {
  if (action === "start-authorization") {
    return {
      operation: {
        state: "accepted",
        result: {
          state: "pending", reused: false,
          user_code: "ABCD-1234",
          verification_uri: VERIFICATION_URI,
          expires_at: Date.now() / 1000 + 600,
          interval: 5,
        },
      },
    };
  }
  if (action === "poll-authorization") {
    pollPhase += 1;
    if (pollPhase === 1) {
      return {
        operation: {
          state: "accepted",
          result: {
            state: "authorized", login: "student", account_id: 7,
            installed: false, installation_status: "missing",
            setup_state: "pending_bootstrap",
          },
        },
      };
    }
    if (holdPollsAfterAuthorized && pollPhase === 2) {
      /* 钉住第二拍：在授权确认已上屏、自动 bootstrap 已发起的断言点暂停轮询链 */
      return new Promise((resolve) => { releaseHeldPoll = resolve; });
    }
    return { operation: { state: "accepted", result: { state: "pending", user_code: "ABCD-1234", expires_at: Date.now() / 1000 + 600, interval: 5 } } };
  }
  if (action === "bootstrap") {
    if (!secondBootstrapFired) {
      /* 幕二首次 bootstrap：建仓完成、等待安装 */
      secondBootstrapFired = true;
      snapshotProvider = () => SNAP_INSTALL_MISSING;
      return {
        operation: {
          state: "accepted",
          result: {
            setup_state: "awaiting_installation",
            installation_status: "missing",
            installation_setup_url: PRESELECTED_URL,
          },
        },
      };
    }
    /* 安装侦测后的收口 bootstrap：初始化完成、只剩加密通道 */
    snapshotProvider = () => SNAP_CHANNEL_REQUIRED;
    return { operation: { state: "accepted", result: { setup_state: "complete" } } };
  }
  if (action === "test-channel") {
    snapshotProvider = () => SNAP_READY;
    return { operation: { state: "accepted", result: {} } };
  }
  return { operation: { state: "accepted", result: {} } };
};

const primary = primaryButton();
assert.ok(primary, "幕二：authorization_missing 快照呈现主推荐按钮");
assert.match(primary.textContent, /授权并创建专属仓库/);
primary.click();
assert.match(
  deviceAreaText(),
  /正在向 GitHub 请求验证码/,
  "幕二：点击即本地反馈（不等 POST 返回，点击同步拍即可见）",
);
await drainMicrotasks();
assert.match(deviceAreaText(), /ABCD-1234/, "幕二：验证码呈现");
assert.match(deviceAreaText(), /分钟内有效/, "幕二：剩余有效期呈现（expires_at 闭集披露）");
/* REALRUN-1 P1-1：设备码页前提说明：真 GitHub 授权页打开前把「需在浏览器
   登录 GitHub 账号」说在人前面（真测：无 Web 会话直撞英文登录墙）；
   C3-FOLLOWUP-1 按审计 C3 配方由名词化改写为动词句，钉随改零弱化。 */
assert.match(
  deviceAreaText(),
  /请先在浏览器登录你的 GitHub 账号/,
  "幕二：设备码页呈现 GitHub 登录前提说明（REALRUN-1 P1-1）",
);
assert.ok(openedUrls.includes(VERIFICATION_URI), "幕二：验证页自动打开（一次登录面）");
holdPollsAfterAuthorized = true;
await settle(10); /* 加速心跳推进 poll 链：authorized → 自动 bootstrap */
assert.ok(countAction("poll-authorization") >= 1, "幕二：授权轮询自动续排");
assert.equal(countAction("bootstrap"), 2, "幕二：authorized 后自动发起初始化（零点击；含幕一续跑 1 次）");
await drainMicrotasks();
assert.match(
  deviceAreaText(),
  /正在初始化专属仓库/,
  "幕二：授权确认即刻呈现初始化中形态（MF-1 状态空窗消除）",
);
releaseHeldPoll?.({}); /* 放行轮询链 */
await settle(40);
assert.ok(openedUrls.includes(PRESELECTED_URL), "幕二：预选安装页自动打开");
const waitLine = byId["remote-install-wait"];
assert.equal(waitLine.hidden, false, "幕二：自动打开即布防安装等待（与锚点点击对称）");
assert.match(waitLine.textContent, /等待你在 GitHub 完成安装/);
assert.ok(waitLine.textContent.includes("自动继续"), "幕二：等待行自述自动推进");
snapshotProvider = () => SNAP_INSTALL_PRESENT;
await settle(16); /* 安装等待新鲜轮询：检测到安装 → 自动 bootstrap 收口 → 自动加密测试 */
assert.equal(countAction("bootstrap"), 3, "幕二：检测到安装自动继续初始化（零点击）");
await settle(24);
assert.equal(countAction("test-channel"), 1, "幕二：初始化收口自动加密测试（零点击）");
await settle(16);
assert.match(
  `${byId["remote-evidence"].textContent}\n${
    byId["remote-evidence"].children.map((node) => collectTextTree(node)).join("\n")
  }`,
  /初始化完成/,
  "幕二：全绿总结态登场",
);
assert.equal(
  postedActions.filter((item) => item === "start-authorization").length, 1,
  "幕二：全链仅一次授权发起（一次登录面）",
);

/* ---- 幕三：验证码过期自动重发（恰 2 次，第 3 次停手） ---- */
snapshotProvider = () => SNAP_AUTH_MISSING;
actionResponder = (action) => {
  if (action === "start-authorization") {
    return {
      operation: {
        state: "accepted",
        result: {
          state: "pending", reused: false,
          user_code: "WXYZ-5678",
          verification_uri: VERIFICATION_URI,
          expires_at: Date.now() / 1000 + 60,
          interval: 5,
        },
      },
    };
  }
  if (action === "poll-authorization") {
    return { operation: { state: "accepted", result: { state: "expired" } } };
  }
  return { operation: { state: "accepted", result: {} } };
};
windowTarget.dispatchEvent(new Event("courselens:remote-connection-changed"));
await settle();
const reissueButton = primaryButton();
assert.ok(reissueButton, "幕三：授权缺失主按钮复现");
const startCountBeforeThirdAct = countAction("start-authorization");
reissueButton.click();
await settle(40); /* expired → 自动重发 ×2 → 第 3 次过期停手 */
assert.equal(
  countAction("start-authorization") - startCountBeforeThirdAct, 3,
  "幕三：1 次手动 + 恰 2 次过期自动重发（第 3 次过期后停手）",
);
assert.equal(
  countAction("poll-authorization") >= 4, true,
  "幕三：重发后轮询链自动续排",
);
assert.equal(
  countAction("poll-authorization") >= 4
    && (deviceArea().textContent || "").includes("验证码已过期")
    && !(deviceArea().textContent || "").includes("GitHub 授权状态："),
  true,
  "幕三：第 3 次过期兜底行人话给下一步（清单 #2，不上英文裸码）",
);

teardown();
function teardown() {
  settingsCleanup?.();
}

console.log("frontend ghux first-run behavior passed");
