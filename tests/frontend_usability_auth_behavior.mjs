import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFileSync } from "node:fs";

/* DOM 执行型行为测试：加载真实 frontend 模块（仿 tests/frontend_playback_recovery_behavior.mjs 的桩件法）。 */

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
    this._hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.checked = false;
    this.open = false;
    this.inert = false;
    this.style = {};
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.title = "";
    this.src = "";
    this._listeners = new Map();
  }
  /* hidden 与属性表同步：ui.js 的 focusableElements 用 closest("[hidden]") 判定
     可聚焦性（真实 DOM 语义）；纯属性值的 hidden 会让隐藏控件混进焦点圈。 */
  get hidden() { return this._hidden === true; }
  set hidden(value) {
    this._hidden = value === true;
    if (this._hidden) this.attributes.set("hidden", "true");
    else this.attributes.delete("hidden");
  }

  /* DOM 式派发：记录监听并沿自定义 parent 冒泡，保持 event.target 为最初目标 */
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
    /* 原生 dispatchEvent 会在监听器调用期间暴露 currentTarget；浮层 Tab 圈闭
       （handleOverlayKeydown）依赖它，这里补齐同等保真度。 */
    if (!Object.prototype.hasOwnProperty.call(event, "currentTarget")) {
      Object.defineProperty(event, "currentTarget", { value: this, configurable: true });
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

  /* INIT-PATH-POLISH-1 单元D：真实 DOM 的 prepend 语义（子节点头部插入） */
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

  scrollIntoView() {}

  blur() {
    if (document.activeElement === this) document.activeElement = null;
  }

  showModal() { this.open = true; }

  canPlayType() { return ""; }

  load() {}

  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  }

  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
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

class FakeCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}

class FakeEventSource {
  static all = [];

  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = new Map();
    FakeEventSource.all.push(this);
  }

  /* C8-1：抽屉按订阅 topics 逐名挂监听；emit 供测试派发命名事件 */
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(topic) {
    for (const listener of this.listeners.get(topic) || []) listener(new Event(topic));
  }

  close() { this.closed = true; }
}

/* ---- 页面装配 ---- */

const ELEMENT_TAGS = {
  "login-password": "input", "login-student-id": "input", "remember-account": "input",
  "proxy-url": "input", "deepseek-key": "input",
  /* CLOUD-TOGGLE-1 U3：隐私区三行退役，复选框不再存在；
     P56-U1：主题二态 checkbox 退役，三态 select 取代 */
  "settings-theme-mode": "select",
  "remember-deepseek": "input", "document-input": "input", "network-mode": "select",
  "artifact-kind": "select", "timetable-semester": "select",
  /* batch-02 增量 DOM：顶栏主题切换（shell）与课程学期过滤（study） */
  "theme-toggle": "button", "catalog-term-filter": "select",
  /* CLOUD-CONSENT-AUTO-1 U2：云算力开关退役，状态胶囊取而代之 */
  "remote-compute-state": "span",
  /* S08-A：账户连接卡自动连接控件 */
  "fudan-auto-connect": "input", "github-auto-connect": "input",
  "fudan-auto-connect-account": "select",
  /* CLOUD-AUTO-UI-1：课程目录披露对话框（唯一按课程 opt-in 入口）；零秘密输入 */
  "course-automation-account": "select", "course-automation-ack": "input",
  "course-automation-confirm": "button", "course-automation-cancel": "button",
  /* S09-B→v3：自动学习材料只读管理卡（目录开关持有唯一按课程入口） */
  "automation-pause": "button", "automation-run-now": "button",
  "automation-update-account": "button", "automation-revoke": "button",
  "player-task-open-drawer": "button", "task-chip": "button", "close-task-drawer": "button",
  "refresh-tasks": "button", "wordmark": "button", "settings-close": "button",
  /* N6L S1：header 常驻直播入口钮（shell 硬接线所需） */
  "live-entry": "button", "live-entry-dot": "span",
  "account-button": "button", "account-menu-login": "button", "account-menu-logout": "button",
  "conn-status": "button", "close-login": "button", "cancel-login": "button",
  /* VPN-P2-CALM-1：可关闭 TUN 提示的不再提示按钮 */
  "campus-connection-tun-dismiss": "button",
  "login-submit": "button", "toggle-password-visibility": "button", "open-login": "button",
  /* S10-A：登录 dialog 已保存账号选择器 */
  "login-use-saved": "button", "login-manual-mode": "button",
  /* SMALL-POLISH-1①：登录框自动登录选项（身份卡内复选） */
  "login-auto-connect": "input",
  /* 条目1合并（一张身份卡两个状态）：手动区容器/动作行 + 返回已保存账号链 */
  "login-back-saved": "button", "login-manual-fields": "div", "login-manual-actions": "div",
  "logout-button": "button", "save-deepseek": "button", "delete-deepseek": "button",
  /* ⑨（C⑧ 修订）：日 token 滑条 + Z2 本月用量视图（DeepSeek 配置面） */
  "deepseek-token-budget": "div", "deepseek-budget-presets": "div",
  "max-deepseek-tokens-label": "p", "max-deepseek-tokens-state": "p",
  "ai-usage-month": "p",
  /* A11Y-IMPL-4 在途产品步（界面字号三态）最小机械桩：settings 装配面现引用
     settings-ui-font（CONN-STALE-R2 同桩解堵，非行为钉）。 */
  "settings-ui-font": "select",
  "save-network": "button", "diagnose-network": "button", "check-update": "button",
  "download-update": "button", "install-update": "button", "copy-diagnostics": "button",
  "remote-reconcile-mailbox": "button", "mailbox-reconcile-confirm": "button",
  "mailbox-reconcile-cancel": "button",
  /* FLOW-ORDER-FIX-1 单元②：轮换 Worker 密钥次级按钮 + 一次确认弹层 */
  "remote-rotate-keys": "button", "remote-rotate-cancel": "button", "remote-rotate-confirm": "button",
  "study-back-courses": "button", "study-back-select": "button", "study-start-select": "button",
  "refresh-catalog": "button", "copy-catalog-diagnostics": "button", "reload-transcript": "button",
  "generate-quiz": "button", "analyze-concepts": "button",
  "generate-subtitle": "button", "generate-notes": "button",
  /* FEATURE-FRONTEND-UPDATE-DATA-1：顶栏更新小组件 + 数据管理工作区 */
  "update-toggle": "button", "account-menu-data": "button",
  "update-background-checks": "input",
  /* MEDIA-VPN-1：媒体流系统代理开关（设置网络卡内） */
  "media-stream-proxy": "input",
  /* WEBVPN-AUTO-1：校外媒体 WebVPN 中转用户开关已移除（全自动，静态说明） */
  "data-search": "input", "data-category-filter": "select", "data-refresh": "button",
  "data-select-all": "button", "data-clear-selection": "button", "data-clear-orphans": "button",
  "data-action-rebuild-search": "button", "data-action-purge-derived": "button",
  "data-action-remove-copies": "button", "data-action-export": "button",
  "data-action-delete-records": "button",
  "data-bulk-hint": "p", "data-bulk-actions": "div",
  "data-confirm-input": "input", "data-confirm-cancel": "button", "data-confirm-confirm": "button",
  /* CLIENT-RESET-1：设置危险区重置面（typed 弹窗 + 两勾选项） */
  "client-reset-open": "button", "client-reset-cancel": "button", "client-reset-confirm": "button",
  "client-reset-confirm-input": "input", "client-reset-delete-derived": "input",
  "client-reset-delete-repos": "input",
};

const IDS = [
  ...Object.keys(ELEMENT_TAGS),
  "workspace-main", "global-status", "toast-region", "topbar-crumbs",
  "study-page", "settings-page", "study-empty", "study-select", "study-desk",
  "study-course-list", "study-lecture-list", "study-course-title", "study-course-meta",
  "catalog-evidence", "catalog-recovery", "catalog-recovery-title", "catalog-recovery-impact",
  "catalog-recovery-actions", "catalog-diagnostic-code", "transcript-list", "bookmark-list",
  "bookmark-action-state", "artifact-notices", "artifact-content", "document-list",
  "quiz-list", "review-list", "concept-list", "analytics-summary",
  "tasks-root", "task-drawer", "task-counts", "task-list",
  "task-chip-count", "task-chip-dot", "task-chip-failed", "remote-drawer-state",
  "conn-menu", "conn-live", "account-session-state", "account-list", "account-text",
  "account-menu-settings", "account-menu",
  /* VPN-P0-UI-1：校园连接卡（conn-menu 弹层内单一连接面）；title/pill 已随
     AS4-U2b 退役（CAMPUS-FIX 剩留清理，POLISH-1），fake 同步拆除 */
  "campus-connection", "campus-connection-state",
  "campus-connection-reason", "campus-connection-regenerated", "campus-connection-actions",
  "campus-connection-details", "campus-connection-path", "campus-connection-checked",
  "campus-connection-services", "campus-connection-live",
  /* VPN-P2-CALM-1：证据支持时的可关闭 TUN 提示行 */
  "campus-connection-tun", "campus-connection-tun-text", "campus-connection-tun-dismiss",
  /* VPN-P1-RECOVERY-1：按需校园诊断抽屉 */
  "campus-diagnostics", "campus-diagnostics-summary", "campus-diagnostics-checked",
  "campus-diagnostics-latency", "campus-diagnostics-fallback", "campus-diagnostics-action",
  "campus-diagnostics-run", "campus-diagnostics-status",
  "login-dialog", "login-form", "login-dialog-title", "login-dialog-hint",
  "login-status", "login-error", "settings-evidence", "proxy-url-row", "privacy-evidence",
  "deepseek-save-state",
  /* S10-A：登录 dialog 已保存账号选择器 */
  "login-saved", "login-saved-options", "login-saved-status",
  /* S08-A：连接卡与 GitHub 阶段 */
  "fudan-connection-state", "github-connection-state", "github-phases",
  /* FRONTEND-SMOOTH-1 单元B：卡面标题单源化（探针证据驱动） */
  "github-connection-title",
  "fudan-auto-connect-status", "github-auto-connect-status",
  "security-evidence", "remote-evidence", "remote-primary-action", "remote-component-evidence",
  "remote-action-error",
  /* FRONTEND-SMOOTH-1 单元A：安装等待行（有界轮询 + 聚焦重探 + 自动推进） */
  "remote-install-wait",
  /* FRONTEND-SMOOTH-1 单元C/D：动作进行中角标 + 分步进度行 */
  "remote-action-progress",
  /* INIT-PATH-POLISH-1 单元D：收紧等待行 */
  "remote-tighten-wait",
  "remote-mailbox-reconcile", "remote-mailbox-reconcile-hint", "remote-mailbox-reconcile-state",
  "mailbox-reconcile-dialog", "mailbox-reconcile-form", "mailbox-reconcile-dialog-title",
  "mailbox-reconcile-dialog-hint",
  /* CLOUD-CONSENT-AUTO-1 U2：云端处理状态行（只读陈述，无开关本体） */
  "remote-compute-row", "remote-compute-state", "remote-compute-hint",
  /* NIGHT4-B：撤销云端授权迁隐私区（一跳可达 + 两击确认） */
  "cloud-revoke-row", "cloud-revoke-credentials", "cloud-revoke-hint",
  /* FLOW-ORDER-FIX-1 单元②：轮换 Worker 密钥弹层节点 */
  "remote-rotate-row", "remote-rotate-hint", "remote-rotate-dialog", "remote-rotate-form",
  "remote-rotate-dialog-title", "remote-rotate-dialog-hint",
  /* CLOUD-AUTO-UI-1：自动学习材料节点（v3 只读管理卡）+ 课程目录披露对话框 */
  "automation-card", "automation-state", "automation-facts", "automation-windows", "automation-scope",
  "automation-last-verified", "automation-last-outcome", "automation-retention",
  "automation-primary-action", "automation-live",
  "automation-error", "automation-protection-note", "automation-disclosure-target",
  "automation-selected-list", "automation-selected-empty",
  "automation-actions", "automation-revoke-row", "automation-run-evidence",
  "automation-run-list", "automation-history-empty",
  "course-automation-dialog", "course-automation-form", "course-automation-title",
  "course-automation-status", "course-automation-error", "course-automation-account-row",
  "course-automation-result-days", "course-automation-live",
  "remote-device-authorization", "update-state", "update-facts-line", "update-last-check",
  "update-notes", "update-error",
  "update-recovery", "network-evidence", "settings-timetable-evidence",
  "player-stage", "player-subtitle-track", "player-stage-shell", "player-subtitle-overlay",
  "player-placeholder", "player-title", "player-evidence",
  "player-recovery", "player-recovery-title", "player-recovery-impact", "player-recovery-actions",
  "player-action-hint", "player-task-state", "player-task-text",
  "transcript-mode-note",
  "live-room-row", "live-room-label", "live-room-capability", "live-room-time", "live-room-reason",
  "enter-live-room", "live-room-recheck",
  /* FEATURE-FRONTEND-UPDATE-DATA-1：顶栏更新小组件 + 数据管理工作区 */
  "update-live", "data-page", "data-evidence", "data-summary", "data-recovery",
  "data-recovery-title", "data-recovery-impact", "data-recovery-actions",
  "data-bulk-bar", "data-selection-count", "data-result", "data-course-list",
  "data-detail", "data-confirm-dialog", "data-confirm-form", "data-confirm-title",
  "data-confirm-hint", "data-orphans-filter",
  /* CLIENT-RESET-1：重置面容器与 typed 弹窗节点 */
  "settings-reset-group", "settings-reset-heading", "client-reset-title",
  "client-reset-result", "client-reset-error",
  "client-reset-dialog", "client-reset-form", "client-reset-dialog-title",
  "client-reset-dialog-hint", "client-reset-dialog-status", "client-reset-dialog-error",
];

const byId = Object.fromEntries(IDS.map((id) => [id, new FakeElement(ELEMENT_TAGS[id] || "div", id)]));
const registry = [...Object.values(byId)];
const named = {
  settingsNav: new FakeElement("nav", "settings-nav-fake"),
  lecturePane: new FakeElement("section", "lecture-pane-fake"),
  materialsTabs: new FakeElement("div", "materials-tabs-fake"),
};
named.settingsNav.className = "settings-nav";
named.lecturePane.className = "lecture-pane";
named.materialsTabs.className = "materials-tabs";
registry.push(named.settingsNav, named.lecturePane, named.materialsTabs);

const register = (node) => {
  registry.push(node);
  return node;
};

/* S08-A：设置分组容器 + 分组标题（键盘导航焦点目标）+ 导航按钮（指针焦点留存目标） */
const SETTINGS_GROUP_IDS = [
  "settings-account-group", "settings-ai-group", "settings-automation-group",
  "settings-network-group", "settings-timetable-group", "settings-update-group",
  "settings-reset-group",
];
const settingsGroups = {};
const settingsNavButtons = {};
SETTINGS_GROUP_IDS.forEach((groupId) => {
  const group = register(new FakeElement("section", groupId));
  group.className = "settings-group";
  const heading = register(new FakeElement("h2", `${groupId}-heading`));
  heading.setAttribute("tabindex", "-1");
  group.append(heading);
  const navButton = register(new FakeElement("button", `settings-nav-${groupId}`));
  navButton.type = "button";
  navButton.dataset.settingsTarget = groupId;
  named.settingsNav.append(navButton);
  settingsGroups[groupId] = group;
  settingsNavButtons[groupId] = navButton;
});

/* 任务抽屉最小结构：scrim 是 tasks-root 子节点，抽屉内含可聚焦按钮 */
const drawerScrim = register(new FakeElement("div", "tasks-scrim"));
drawerScrim.dataset.tasksScrim = "";
byId["tasks-root"].append(drawerScrim, byId["task-drawer"]);
byId["task-drawer"].append(byId["close-task-drawer"], byId["refresh-tasks"], byId["task-list"]);

/* 页面归属与 data-open-tasks 触发器：顶栏任务 chip + 学习桌“查看进度” */
byId["study-page"].dataset.page = "study";
byId["settings-page"].dataset.page = "settings";
byId["data-page"].dataset.page = "data";
byId["task-chip"].dataset.openTasks = "";
byId["player-task-open-drawer"].dataset.openTasks = "";
byId["task-chip"].setAttribute("aria-expanded", "false");
byId["player-task-open-drawer"].setAttribute("aria-expanded", "false");
byId["conn-menu"].hidden = true;
byId["account-menu"].hidden = true;
byId["conn-status"].setAttribute("aria-expanded", "false");
byId["account-button"].setAttribute("aria-expanded", "false");
/* 顶栏更新小组件：真实 HTML 初始 hidden；数据页批量条/恢复面板初始隐藏 */
byId["update-toggle"].hidden = true;
byId["data-bulk-bar"].hidden = true;
byId["data-recovery"].hidden = true;
byId["data-result"].hidden = true;
byId["data-orphans-filter"].hidden = true;

/* 密码可见切换按钮的两个 inline SVG（真实结构由 index.html 提供） */
const eyeOn = register(new FakeElement("svg", "eye-on"));
const eyeOff = register(new FakeElement("svg", "eye-off"));
eyeOn.dataset.eyeIcon = "on";
eyeOff.dataset.eyeIcon = "off";
eyeOn.hidden = true;
byId["toggle-password-visibility"].setAttribute("aria-controls", "login-password");
byId["toggle-password-visibility"].append(eyeOn, eyeOff);

/* Mailbox 修复卡与确认框文案（真实结构由 index.html 提供） */
byId["remote-mailbox-reconcile-hint"].textContent =
  "这些记录不会继续运行，但会阻止远程计算安全检查。修复会保留评论和关闭状态，只移除不可再使用的任务载荷。";
byId["remote-reconcile-mailbox"].textContent = "修复历史记录";
byId["remote-reconcile-mailbox"].setAttribute("aria-describedby", "remote-mailbox-reconcile-hint");
byId["remote-mailbox-reconcile"].append(
  register(new FakeElement("div", "remote-mailbox-reconcile-body")),
  byId["remote-reconcile-mailbox"],
  byId["remote-mailbox-reconcile-state"],
);
byId["mailbox-reconcile-dialog-hint"].textContent =
  "将把已关闭且未收口的任务记录标记为已消费。远程计算保持关闭；不会删除任何评论，标题、标签与关闭状态保持不变。";
byId["mailbox-reconcile-dialog"].append(byId["mailbox-reconcile-form"]);

/* 轮换 Worker 密钥（单元②）：确认框文案与父子结构（真实结构由 index.html 提供） */
byId["remote-rotate-hint"].textContent = "环境缺少 Worker 加密密钥时，重新生成并上传一对新密钥。";
byId["remote-rotate-keys"].textContent = "轮换 Worker 密钥";
byId["remote-rotate-dialog-hint"].textContent =
  "将重新生成 Worker 加密密钥，并上传到专属仓库环境；本机不保存私钥。进行中的远程任务需要先结束。";
byId["remote-rotate-dialog"].append(byId["remote-rotate-form"]);

/* GitHub 连接：真实 HTML 中默认隐藏的两块（内联错误区 / 设备码进度行） */
byId["remote-action-error"].hidden = true;
byId["remote-device-authorization"].hidden = true;

/* VPN-P0-UI-1：校园连接卡真实父子结构（外点判定/焦点圈闭/可见文本都依赖它） */
byId["campus-connection-reason"].hidden = true;
byId["campus-connection-regenerated"].hidden = true;
byId["campus-connection-regenerated"].textContent = "网络设置已变化，正在重新确认校园连接"; /* 静态文案由 index.html 提供 */
byId["campus-connection-tun"].append(
  byId["campus-connection-tun-text"],
  byId["campus-connection-tun-dismiss"],
);
byId["campus-connection-tun"].hidden = true;
byId["campus-connection"].append(
  /* CAMPUS-FIX 剩留清理（POLISH-1）：campus-connection-title/pill 已随
     AS4-U2b 退役（workbench assertNotIn 钉死），fake 镜像同步拆除。 */
  byId["campus-connection-state"],
  byId["campus-connection-reason"],
  byId["campus-connection-regenerated"],
  byId["campus-connection-tun"],
  byId["campus-connection-actions"],
  byId["campus-connection-details"],
  byId["campus-diagnostics"],
  byId["campus-diagnostics-run"],
  byId["campus-diagnostics-status"],
  byId["campus-connection-live"],
);
byId["campus-diagnostics-run"].tagName = "button";
/* 诊断抽屉在真实 HTML 中初始 hidden（未检查时收起）——镜像之，抽屉可见性契约才有起点 */
byId["campus-diagnostics"].hidden = true;
const campusFacts = register(new FakeElement("dl", "campus-connection-facts"));
campusFacts.append(
  byId["campus-connection-path"],
  byId["campus-connection-checked"],
  byId["campus-connection-services"],
);
byId["campus-connection-details"].append(
  register(new FakeElement("summary", "campus-connection-summary")),
  campusFacts,
);
byId["conn-menu"].append(byId["campus-connection"]);

/* AS4-U2 探测件：复旦行连接点/文案（index.html 中无 id，台架以探测 id 注册；
   只进 registry 不进 byId，不污染 visibleText）。 */
const fudanConnDot = register(new FakeElement("span", "probe-conn-dot-fudan"));
fudanConnDot.className = "conn-dot";
fudanConnDot.dataset.connDot = "fudan";
const fudanConnText = register(new FakeElement("span", "probe-conn-text-fudan"));
fudanConnText.dataset.connText = "fudan";


/* CLOUD-AUTO-UI-1：自动学习材料只读管理卡静态文案（真实文案由 index.html 提供） */
byId["automation-disclosure-target"].textContent = "目标仓库：读取中…";
byId["automation-state"].textContent = "未开启";
byId["automation-state"].dataset.state = "off";
byId["automation-card"].dataset.state = "off";
const automationFactBundle = register(new FakeElement("li", "automation-fact-bundle"));
const bundleLabel = register(new FakeElement("span", "automation-fact-bundle-label"));
bundleLabel.className = "phase-label";
bundleLabel.textContent = "固定包";
const bundleState = register(new FakeElement("span", "automation-fact-bundle-state"));
bundleState.className = "phase-state-text";
bundleState.textContent = "字幕 ASR · 课件 OCR · AI 总结与章节";
automationFactBundle.append(bundleLabel, bundleState);
byId["automation-facts"].append(
  automationFactBundle,
  register(new FakeElement("li", "automation-fact-windows")),
  register(new FakeElement("li", "automation-fact-scope")),
  register(new FakeElement("li", "automation-fact-verified")),
);
byId["automation-actions"].hidden = true;
byId["automation-revoke-row"].hidden = true;
byId["automation-run-evidence"].hidden = true;
byId["automation-error"].hidden = true;
byId["automation-selected-empty"].hidden = false;
byId["automation-history-empty"].hidden = false;
byId["automation-revoke"].textContent = "删除云端凭据与状态";
byId["automation-pause"].textContent = "暂停自动处理";
byId["automation-run-now"].textContent = "立即检查";
byId["automation-update-account"].textContent = "更新账号";
/* 课程目录披露对话框默认态 */
byId["course-automation-confirm"].disabled = true; /* 未勾选披露前确认禁用 */
byId["course-automation-error"].hidden = true;
byId["course-automation-status"].hidden = true;

const createdSeq = { value: 0 };
globalThis.document = Object.assign(new EventTarget(), {
  getElementById: (id) => byId[id] || registry.find((node) => node.id === id) || null,
  createElement: (tag) => register(new FakeElement(tag, `created-${createdSeq.value += 1}`)),
  createElementNS: (ns, tag) => register(new FakeElement(tag, `created-ns-${createdSeq.value += 1}`)),
  createDocumentFragment: () => register(new FakeElement("#document-fragment", `created-fragment-${createdSeq.value += 1}`)),
  querySelectorAll: (selector) => registry.filter((node) => elementMatches(node, selector)),
  querySelector: (selector) => {
    if (String(selector).includes(".settings-nav")) return named.settingsNav;
    if (String(selector).includes(".lecture-pane")) return named.lecturePane;
    return registry.find((node) => elementMatches(node, selector)) || null;
  },
  activeElement: null,
  documentElement: { dataset: {} },
});

const collectText = (element, into) => {
  if (!element || element.hidden) return into;
  /* 仿真实浏览器：<details> 折叠时仅 summary 进入可见文本 */
  if (String(element.tagName) === "DETAILS" && !element.open) {
    const summary = (element.children || []).find((child) => child.tagName === "SUMMARY");
    if (summary) collectText(summary, into);
    return into;
  }
  into.push(String(element.textContent || ""));
  for (const child of element.children || []) collectText(child, into);
  return into;
};

const deepElements = (element, into = []) => {
  if (!element) return into;
  into.push(element);
  for (const child of element.children || []) deepElements(child, into);
  return into;
};

const visibleText = () => [...Object.values(byId), named.settingsNav, named.lecturePane]
  .flatMap((root) => collectText(root, []))
  .join("\n");

const allDeepElements = () => {
  const seen = new Set();
  const roots = [...Object.values(byId), named.settingsNav, named.lecturePane];
  const all = [];
  for (const root of roots) {
    for (const node of deepElements(root)) {
      if (!seen.has(node)) {
        seen.add(node);
        all.push(node);
      }
    }
  }
  return all;
};

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
    livePlayback: "",
    tasks: [],
    transcriptHasTiming: false,
    /* FIRST-LOGIN-UX-2：store 记忆面桩（与真实 store.js 同名方法对齐） */
    rememberLastLecture() {},
    readLastLecture() { return null; },
    set(key, value) {
      this[key] = value;
      for (const listener of listeners.get(key) || []) listener(value);
    },
    subscribe(key, listener) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(listener);
      return () => listeners.get(key)?.delete(listener);
    },
  };
}

const windowTarget = new EventTarget();
const timeouts = new Map();
const timeoutDelays = new Map(); /* 每个 setTimeout 的延迟（ms）：供 poll 退避续排断言 */
const intervals = new Map();
let timeoutSeq = 0;
let intervalSeq = 0;
windowTarget.setTimeout = (fn, ms) => {
  timeoutSeq += 1;
  timeouts.set(timeoutSeq, fn);
  timeoutDelays.set(timeoutSeq, Number(ms || 0));
  return timeoutSeq;
};
windowTarget.clearTimeout = (id) => { timeouts.delete(id); timeoutDelays.delete(id); }; /* 延迟账目随清除一并注销，避免陈旧延迟污染 roundMaxDelay */
windowTarget.setInterval = (fn, ms) => {
  intervalSeq += 1;
  intervals.set(intervalSeq, { fn, ms: Number(ms) });
  return intervalSeq;
};
windowTarget.clearInterval = (id) => intervals.delete(id);
windowTarget.requestAnimationFrame = (fn) => fn();
globalThis.window = windowTarget;
globalThis.CustomEvent = FakeCustomEvent;
/* batch-02 增量 DOM 路径所需浏览器全局：学期过滤 select 重建（Option）与焦点还原（CSS.escape） */
globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
globalThis.CSS = { escape: (value) => String(value) };
globalThis.EventSource = FakeEventSource;
globalThis.matchMedia = (query) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};
Object.defineProperty(globalThis, "navigator", {
  value: { onLine: true, sendBeacon: () => true },
  configurable: true,
});

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 4; index += 1) await nextTurn();
};
const tickIntervals = async (ms) => {
  for (const [, timer] of [...intervals.entries()].filter(([, t]) => t.ms === ms)) {
    timer.fn();
    await settle();
  }
};
/* 触发全部 delay===ms 的一次性定时器（触发即消费）：异步动作反馈轮询缝排水用 */
const tickTimeouts = async (ms) => {
  const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === ms);
  for (const [key, fn] of entries) {
    timeouts.delete(key);
    timeoutDelays.delete(key);
    fn();
    await settle();
  }
};
/* 异步动作反馈循环排水（单元②）：伪时钟推进 62 拍 × 2s 越过 ~120s 死线，
   走超时收口解除进行中态——供 bootstrap 成功后不改变快照的桩场景复位 */
const drainAsyncFeedback = async () => {
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  try {
    for (let round = 0; round < 62; round += 1) {
      fakeNow += 2000;
      const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 2000);
      for (const [key, fn] of entries) {
        timeouts.delete(key);
        timeoutDelays.delete(key);
        fn();
      }
      await settle();
    }
  } finally {
    Date.now = realNow;
  }
};

/* ---- 网络桩 ---- */

const calls = { authGet: 0, catalogGet: 0, authPost: [], secretsPost: [], accountsGet: 0, tasksGet: 0, frontendSession: 0, remoteGet: 0, remoteGetFresh: 0, remoteActionPost: [], settingsActionsPost: [], automationGet: 0, automationActionPost: [], automationUploadPut: [], automationRunDeletePost: [] };
let authSnapshot = {
  state: "action_required", code: "fudan_credentials_missing", actions: ["login"],
  connected: false, configured: false,
};
/* remote-connection 快照与动作响应可注入：供 Mailbox 历史修复场景切换后端证据 */
let remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
/* AS4-U1：恢复期 catalog 桩可注入 checking+缓存信封（后端 P2-C 真实形状）；null=缺省就绪形状 */
let catalogEnvelopeOverride = null;
let remoteActionResponse = null; /* null=成功 {operation:{state:"accepted",result:{}}}；或 { error: "code" } */
let pendingAuthPost = null;
let authPostErrorCode = "";
let taskPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
let syntheticTranscriptSegments = [];
let accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
let secretsResponse = { configured: false, saved: false, deleted: false };
let secretsPostErrorCode = "";
/* S08-A：设置快照（含 auto_connect）与 set-auto-connect 动作响应可注入
   （AS3：answer_budget 已整链退役，快照无该键；max_deepseek_tokens 0=不限档） */
/* F2（化身走查 20261008）：书签创建证据门可注入——""=成功，evidence_unavailable=400 证据缺码 */
let bookmarkPostBehavior = "";
/* F7（化身走查 20261008）：本地统计开启动作可注入 */
let analyticsActionPost = [];
let analyticsActionError = "";
let settingsSnapshot = {
  state: "ready", code: "fudan_session_verified", network: { mode: "auto", proxy_url: "" },
  onboarding: { consent: { accepted: false } },
  /* F7（化身走查 20261008）：本地统计缺省未开启=action_required 闭集（后端
     settings GET 同款形状），actions 携带 enable-analytics 供行内动作消费 */
  analytics: { state: "action_required", code: "analytics_disabled", actions: ["enable-analytics"], enabled: false },
  credentials: { state: "ready", code: "fudan_session_verified" }, remote: { overall: { state: "ready", code: "remote_verified" } },
  auto_connect: null,
  max_deepseek_tokens: 100000,
  ai_usage_month: { month: "2026-09", questions: 3, summaries: 2 },
  /* AS6 消耗透镜：settings GET 加性键（本机累计） */
  task_usage_month: { schema: "courselens.task-usage-month.v1", month: "2026-09", deepseek_tokens: 12345, runner_minutes: 12.5 },
};
/* AS6：余额读数路由可注入（设置页打开时拉一次） */
let balanceSnapshot = { state: "ok", is_available: true, currency: "CNY", total_balance: "86.50" };
let settingsActionResponse = null; /* null=成功返回注入对象；{ error: "code" }=409 拒绝 */
/* S09-B：自动学习材料快照/动作/上传可注入 */
let automationSnapshotPayload = {
  schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
  disclosure_version: "cloud-custody-disclosure.v1",
  state: "disabled", enabled: false, account_id: "",
  schedule: { times: ["13:00", "22:00"], timezone: "Asia/Shanghai", next_two: [] },
  rules: [], selected_courses: 0,
  budget: { limits: { max_lectures: 20, max_runner_minutes: 2000, max_deepseek_tokens: 2000000 }, used: null },
  retention: { result_days: 30, state_days: 90, ceiling_days: 90 },
  target: null, binding_confirmed: false,
  verification: {}, config_hash: "", verified: false, ai_key_available: false,
  circuits: [], last_cloud_run: null, runs: [], imports: [],
  observed_at: 0, expires_at: 0, stale: false, actions: [],
};
let automationActionError = "";
let automationUploadError = "";
let catalogPayload = { state: "ready", code: "authorized_catalog_verified", courses: [], course_count: 0 };
/* VPN-P1-RECOVERY-1：按需校园诊断载荷可注入（null = 网络失败路径） */
let campusDiagnosticsPayload = null;
let campusDiagnosticsFail = false;
/* FEATURE-FRONTEND-UPDATE-DATA-1：client-update 快照可注入；数组=轮询序列
   （逐次弹出，末元素驻留）；编排动作响应/错误可注入。 */
let clientUpdateSnapshot = { state: "idle", current_version: "0.0.0", channel: "stable", available_version: "", package_size: 0, release_notes: "", error_code: "", actions: [] };
let clientUpdateSequence = null;
let clientUpdateActionResponse = null;
let clientUpdateActionError = "";
let clientUpdateActionPost = [];
/* 数据管理页三路由桩 */
let courseDataSummary = {
  schema: "courselens.course-data-summary.v1", generated_at: 100,
  byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
  database_bytes: { state_db: 10, learning_db: 20, total: 30 },
  orphan_artifacts: { directories: 0, bytes: 0 },
  page: { page: 1, page_size: 200, total: 0 },
  rows: [],
};
let courseDataLectures = {
  schema: "courselens.course-data-lecture-page.v1",
  course_id: "c1", in_catalog: true, total: 0,
  page: { limit: 50, offset: 0 }, lectures: [],
};
let courseDataActionReceipt = {
  schema: "courselens.course-data-action-result.v1",
  action: "rebuild-search", operation_id: "op", status: "accepted",
};
let courseDataActionError = "";
const courseDataActionPost = [];

function ok(data, status = 200) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(errorCode, status = 400) {
  return new Response(JSON.stringify({ error: "synthetic", error_code: errorCode }), {
    status,
    headers: { "Content-Type": "application/json" },
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
  if (route === "/api/v3/authentication") {
    calls.authGet += 1;
    return ok(authSnapshot);
  }
  if (route === "/api/v3/authentication/actions") {
    calls.authPost.push(JSON.parse(options.body || "{}"));
    if (authPostErrorCode) return errorResponse(authPostErrorCode);
    if (pendingAuthPost) return pendingAuthPost.promise;
    return ok({ action: "login", authentication: { state: "checking", code: "fudan_session_checking" }, operation_id: "op-synthetic" }, 202);
  }
  if (route === "/api/v3/campus-diagnostics") {
    calls.campusDiagGet = (calls.campusDiagGet || 0) + 1;
    if (campusDiagnosticsFail) return errorResponse("timeout", 504);
    return ok(campusDiagnosticsPayload);
  }
  if (route === "/api/v3/frontend-session") {
    calls.frontendSession += 1;
    return ok({ accepted: true });
  }
  if (route.split("?")[0] === "/api/v3/remote-connection") {
    /* INIT-PATH-POLISH-1 单元A：等待窗新鲜语义显式承载 ?fresh=1 查询串 */
    if (route.includes("fresh=1")) calls.remoteGetFresh += 1;
    calls.remoteGet += 1;
    return ok(remoteSnapshot);
  }
  if (route === "/api/v3/remote-connection/actions") {
    calls.remoteActionPost.push(JSON.parse(options.body || "{}"));
    if (remoteActionResponse && remoteActionResponse.error) {
      return errorResponse(remoteActionResponse.error);
    }
    const operation = remoteActionResponse && remoteActionResponse.operation
      ? remoteActionResponse.operation
      : { state: "accepted", result: {} };
    return ok({ operation }, 202);
  }
  if (route === "/api/v3/accounts") {
    calls.accountsGet += 1;
    return ok(accountsSnapshot);
  }
  if (route === "/api/v3/secrets/actions") {
    calls.secretsPost.push(JSON.parse(options.body || "{}"));
    if (secretsPostErrorCode) return errorResponse(secretsPostErrorCode);
    return ok(secretsResponse);
  }
  if (route === "/api/v3/tasks") {
    calls.tasksGet += 1;
    return ok(taskPayload);
  }
  if (route === "/api/v3/settings") {
    return ok(settingsSnapshot);
  }
  if (route === "/api/v3/deepseek-balance") {
    calls.balanceGet = (calls.balanceGet || 0) + 1;
    if (balanceSnapshot === "error") return errorResponse("remote_failed");
    return ok(balanceSnapshot);
  }
  if (route === "/api/v3/analytics/actions") {
    /* F7（化身走查 20261008）：本地统计开启动作桩——成功后翻转快照 enabled 闭集 */
    const body = JSON.parse(options.body || "{}");
    analyticsActionPost.push(body);
    if (analyticsActionError) return errorResponse(analyticsActionError);
    if (body.action === "enable") {
      settingsSnapshot.analytics = { state: "ready", code: "analytics_enabled", actions: ["disable-analytics", "delete-analytics"], enabled: true };
    }
    return ok({ settings: settingsSnapshot.analytics, accepted_action: body.action });
  }
  if (route === "/api/v3/settings/actions") {
    calls.settingsActionsPost.push(JSON.parse(options.body || "{}"));
    if (settingsActionResponse && settingsActionResponse.error) {
      return errorResponse(settingsActionResponse.error, 409);
    }
    return ok(settingsActionResponse || {});
  }
  if (route.startsWith("/api/v3/catalog")) {
    calls.catalogGet += 1;
    /* AS4-U1：恢复期场景可注入 checking 信封（后端 P2-C 形状：state=checking
       且携带本机缓存课程），验证目录先行；缺省保持第卅三案就绪形状。 */
    if (catalogEnvelopeOverride) return ok(catalogEnvelopeOverride);
    /* 第卅三案钉：真实形状目录（1 课 1 讲），选课入口等待态验收用 */
    return ok({
      state: "ready",
      courses: [{
        course_id: "course-h-case", title: "第卅三案课程", term: "2026-20271",
        lectures: [{ sub_id: "lecture-h1", sub_title: "第一讲" }],
      }],
      course_count: 1,
    });
  }
  if (route === "/api/v3/automation") {
    calls.automationGet += 1;
    return ok(automationSnapshotPayload);
  }
  if (route === "/api/v3/automation/runs-delete") {
    /* 第卅六案③：终态运行记录删除（闭集路由，只删记录） */
    calls.automationRunDeletePost.push(JSON.parse(options.body || "{}"));
    return ok({ deleted: true, run_key: "" });
  }
  if (route === "/api/v3/automation/actions") {
    calls.automationActionPost.push(JSON.parse(options.body || "{}"));
    if (automationActionError) return errorResponse(automationActionError);
    return ok({ operation: { operation_id: "op", action: "accepted", state: "accepted", error_code: "", updated_at: 1 } }, 202);
  }
  if (route === "/api/v3/automation/cloud-secrets") {
    calls.automationUploadPut.push(JSON.parse(options.body || "{}"));
    if (automationUploadError) return errorResponse(automationUploadError);
    return ok({ operation: { operation_id: "op", state: "accepted", error_code: "" } }, 202);
  }
  if (route === "/api/v3/client-update") {
    if (Array.isArray(clientUpdateSequence) && clientUpdateSequence.length > 1) {
      return ok(clientUpdateSequence.shift());
    }
    if (Array.isArray(clientUpdateSequence)) return ok(clientUpdateSequence[0]);
    return ok(clientUpdateSnapshot);
  }
  if (route === "/api/v3/client-update/actions") {
    clientUpdateActionPost.push(JSON.parse(options.body || "{}"));
    if (clientUpdateActionError) return errorResponse(clientUpdateActionError);
    return ok(clientUpdateActionResponse || clientUpdateSnapshot);
  }
  if (route === "/api/v3/course-data" || route.startsWith("/api/v3/course-data?")) {
    return ok(courseDataSummary);
  }
  if (route.startsWith("/api/v3/course-data/lectures")) {
    return ok(courseDataLectures);
  }
  if (route === "/api/v3/course-data/actions") {
    courseDataActionPost.push(JSON.parse(options.body || "{}"));
    if (courseDataActionError) return errorResponse(courseDataActionError);
    return ok(courseDataActionReceipt);
  }
  if (route.startsWith("/api/v3/catalog")) return ok(catalogPayload);
  if (route.startsWith("/api/v3/subtitles/segments")) {
    return ok({ segments: route.includes("sub_id=l-w2c-labels") ? syntheticTranscriptSegments : [] });
  }
  if (route.startsWith("/api/v3/artifacts")) return ok({ artifact: null });
  if (route.startsWith("/api/v3/documents")) return ok({ documents: [] });
  if (route.startsWith("/api/v3/quizzes")) return ok({ items: [] });
  if (route === "/api/v3/review-plans") return ok({ plans: [] });
  if (route.startsWith("/api/v3/concepts")) return ok({ edges: [] });
  if (route === "/api/v3/analytics") return ok({ summary: {} });
  if (route === "/api/v3/bookmarks" && method === "POST") {
    calls.bookmarksPost = (calls.bookmarksPost || 0) + 1;
    if (bookmarkPostBehavior === "evidence_unavailable") return errorResponse("bookmark_evidence_unavailable");
    return ok({ bookmarks: [] });
  }
  if (route.startsWith("/api/v3/bookmarks")) return ok({ bookmarks: [] });
  if (route === "/api/v3/progress") return ok({ accepted: true });
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

/* ---- 安装真实模块 ---- */

const store = createStore();
const { installShell, selectPage, openLoginDialog } = await import("../frontend/modules/shell.js");
const { installStudy } = await import("../frontend/modules/study.js");
const { installPlayerCore } = await import("../frontend/modules/player-core.js");
const { installTasksDrawer } = await import("../frontend/modules/tasks-drawer.js");
const { installSettings } = await import("../frontend/modules/settings.js");

await installShell(store);
await settle(); /* ⑬b：会话链后台接力后定时器才安装 */
await installStudy(store);
await installPlayerCore(store);
await installTasksDrawer(store);
const settingsCleanup = await installSettings(store);
await settle();

/* 唯一搜索入口合同：study-select 页面内不再有课程过滤输入框，课程/教师检索由顶栏 palette 承担 */
assert.equal(document.getElementById("catalog-query"), null, "页面内无第二搜索输入框");

/* ---- 1) 任务抽屉：统一关闭路径 / 触发器归属 / 失败闭集文案 ---- */

async function verifyTasksDrawerGateEnvelopeGuard() {
  /* REALTEST-H5：每 ~5 分钟 ≤10s 的会话再校验门信封窗（tasks 空+counts 空）
     不得把好列表闪成「暂无任务」；真实空列表（counts 对象）照常渲染。 */
  const chip = byId["task-chip"];
  taskPayload = {
    counts: { active: 1, failed: 0, completed: 0 },
    tasks: [
      /* SWEEPFIX-3 T6：载荷形状对齐 public_task 真实合同——阶段人话 label 在
         顶层（旧夹具嵌套 progress:{label} 与旧死路读法互相抵消才假绿） */
      { task_id: "t9", kind: "subtitle", state: "running", display_state: "remote_running", course_id: "c1", label: "转写中", updated_at: 200 },
    ],
  };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();
  assert.ok(visibleText().includes("转写中"), "前置：好任务列表在场");

  taskPayload = { state: "checking", code: "fudan_session_checking", counts: null, tasks: [], source: "gate" };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();
  assert.ok(visibleText().includes("转写中"), "门信封窗不清空既有任务列表");
  assert.equal(chip.getAttribute("aria-label"), "任务：1 个活动", "门信封窗内 chip 计数保持");

  /* 真实空列表：counts 是对象 → 正常渲染空态 */
  taskPayload = { counts: { active: 0, failed: 0, completed: 0 }, tasks: [] };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();
  assert.ok(visibleText().includes("暂无任务"), "真实空列表照常显示空态");
  assert.equal(chip.getAttribute("aria-label"), "任务", "真实空列表 chip 归零");

  /* 清场：恢复空 payload，不影响后续场景 */
  taskPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();
}

async function verifyTasksDrawer() {
  const chip = byId["task-chip"];
  const playerTrigger = byId["player-task-open-drawer"];
  const root = byId["tasks-root"];
  const triggers = () => [chip, playerTrigger].map((btn) => btn.getAttribute("aria-expanded"));
  assert.deepEqual(triggers(), ["false", "false"], "两个触发器初始 aria-expanded=false");

  store.set("courses", [{ course_id: "c1", title: "课程一" }]);
  const rawDetail = "RAW backend failure with forbidden details";
  taskPayload = {
    counts: { active: 1, failed: 2, completed: 0 },
    tasks: [
      { task_id: "t1", kind: "subtitle", state: "failed", display_state: "failed", course_id: "c1", error_code: "timeout", progress: {}, updated_at: 100, actions: ["retry"] },
      { task_id: "t2", kind: "summary", state: "failed", display_state: "failed", course_id: "", error_code: "brand_new_future_code", raw_error: rawDetail, progress: {}, updated_at: 100 },
      { task_id: "t3", kind: "subtitle", state: "running", display_state: "remote_running", course_id: "c1", label: "转写中", updated_at: 100 },
    ],
  };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();
  assert.ok(visibleText().includes("课程一"), "任务按 course_id 映射到课程标题分组");
  /* OBS-2（SWEEPFIX-N20 1481fb4）：缺失 course_id=本来就不挂课程的任务种，
     分组标题中性「不挂课程的任务」；「未关联课程」只留给有 course_id 找不到真身的真异常。 */
  assert.ok(visibleText().includes("不挂课程的任务"), "缺失映射进入不挂课程任务中性分组");
  assert.equal(calls.tasksGet >= 2, true, "刷新事件触发重新拉取任务");
  assert.equal(chip.getAttribute("aria-label"), "任务：1 个活动", "抽屉外 chip 由轮询/刷新更新");

  /* 打开（chip 路径）→ 两个触发器 expanded、焦点进抽屉；订阅为安装期应用级唯一实例 */
  chip.click();
  await settle();
  assert.equal(root.hidden, false, "抽屉打开");
  assert.deepEqual(triggers(), ["true", "true"], "打开后两个触发器 aria-expanded=true");
  assert.equal(document.activeElement, byId["close-task-drawer"], "初始焦点进入抽屉");
  assert.equal(byId["workspace-main"].inert, true, "背景主区 inert");
  assert.equal(FakeEventSource.all.length, 1, "打开抽屉不新建 EventSource（安装期已建立唯一订阅）");
  assert.equal(FakeEventSource.all.filter((source) => !source.closed).length, 1, "同一时刻只有一个活动订阅");

  /* scrim 点击关闭：焦点归还触发器、隐藏恢复、触发器复位；应用级订阅保持存活 */
  drawerScrim.dispatchEvent(new Event("click", { bubbles: true }));
  await settle();
  assert.equal(root.hidden, true, "scrim 点击关闭抽屉");
  assert.deepEqual(triggers(), ["false", "false"], "关闭后触发器 aria-expanded=false");
  assert.equal(document.activeElement, chip, "焦点归还触发器");
  assert.equal(byId["workspace-main"].inert, false, "关闭后背景主区恢复");
  assert.equal(FakeEventSource.all[0].closed, false, "关闭抽屉不再释放应用级订阅");

  /* 打开→关闭→重开（player 触发器事件路径）：始终复用同一订阅，无重复订阅 */
  window.dispatchEvent(new CustomEvent("courselens:open-tasks", { detail: { trigger: playerTrigger } }));
  await settle();
  assert.equal(root.hidden, false);
  assert.equal(FakeEventSource.all.length, 1, "重开不创建新的 EventSource");
  assert.equal(FakeEventSource.all.filter((source) => !source.closed).length, 1, "不存在重复订阅");
  const escapeKey = new Event("keydown");
  Object.defineProperty(escapeKey, "key", { value: "Escape" });
  document.dispatchEvent(escapeKey);
  await settle();
  assert.equal(root.hidden, true, "Esc 关闭抽屉");
  assert.equal(document.activeElement, playerTrigger, "player 触发器路径焦点归还");
  assert.equal(FakeEventSource.all.filter((source) => !source.closed).length, 1, "应用级订阅随 app 生命周期保持存活");

  /* 失败闭集文案：已知代码 → 映射文案；未知 → 兜底；原文与代码只在技术详情内 */
  const knownCopy = "任务等待后端响应超时。请确认网络后重试。";
  /* U2（TASKS-CENTER-1）：未知码兜底如实「未能确认 + 诊断详情」 */
  const fallbackCopy = "这次失败的原因未能确认。可展开下方「技术详情」核对诊断信息后重试。";
  assert.ok(visibleText().includes(knownCopy), "已知失败代码渲染闭集处置文案");
  assert.ok(visibleText().includes(fallbackCopy), "未知失败代码渲染诚实兜底文案");
  assert.equal(visibleText().includes(rawDetail), false, "原始错误文本永不进入 DOM");
  const technicals = deepElements(byId["task-list"]).filter((node) => String(node.className || "").split(/\s+/).includes("task-technical"));
  assert.ok(technicals.length >= 1, "技术详情 details 存在");
  const insideTechnical = (node) => technicals.some((technical) => technical === node || technical.contains(node));
  const codeOwnerOutsideDetails = deepElements(byId["task-list"]).filter((node) => !insideTechnical(node))
    .filter((node) => typeof node.textContent === "string" && node.children.length === 0 && node.textContent.includes("brand_new_future_code"));
  assert.equal(codeOwnerOutsideDetails.length, 0, "技术代码只出现在折叠的技术详情内");
  assert.ok(technicals.some((technical) => deepElements(technical).some((node) => String(node.textContent).includes("error_code: brand_new_future_code"))), "技术详情保留闭集代码");
}

/* ---- 2) 字标返回：回到选择态且保留已选课程/讲次；设置返回不改变模式 ---- */

async function verifyWordmarkReturn() {
  const lecture = { course_id: "c9", sub_id: "l9", sub_title: "返回讲次", can_stream: true };
  store.set("activeCourse", { course_id: "c9", title: "返回课程" });
  store.set("activeLecture", lecture);
  await settle();
  assert.equal(byId["study-desk"].hidden, false, "先处于学习桌");
  assert.equal(byId["topbar-crumbs"].hidden, false, "学习桌显示面包屑");

  byId["wordmark"].click();
  await settle();
  assert.equal(byId["study-page"].classList.contains("active"), true, "字标回到学习页");
  assert.equal(byId["study-desk"].hidden, true, "字标退出学习桌");
  assert.equal(byId["study-select"].hidden, false, "字标回到课程/讲次选择态");
  assert.equal(store.activeLecture, lecture, "activeLecture 原样保留");
  assert.equal(store.activeCourse?.course_id, "c9", "activeCourse 原样保留");

  /* 设置往返：完成设置后恢复进入设置前的学习模式（选择态） */
  selectPage("settings");
  await settle();
  assert.equal(byId["settings-page"].hidden, false);
  byId["settings-close"].click();
  await settle();
  assert.equal(byId["study-page"].classList.contains("active"), true, "设置返回学习页");
  assert.equal(byId["study-select"].hidden, false, "设置返回保留选择态模式");
  assert.equal(byId["study-desk"].hidden, true);

  /* 回到桌面模式后，设置返回应保留桌面模式 */
  store.set("activeLecture", { ...lecture, sub_id: "l10" });
  await settle();
  assert.equal(byId["study-desk"].hidden, false);
  selectPage("settings");
  await settle();
  byId["settings-close"].click();
  await settle();
  assert.equal(byId["study-desk"].hidden, false, "设置返回保留学习桌模式");
}

/* ---- 2b) 字幕行重复控件可访问名称：时间按钮/书签按钮名称含时间上下文且可区分 ---- */

async function verifyTranscriptAriaContextLabels() {
  syntheticTranscriptSegments = [
    { start_ms: 65000, end_ms: 69000, text: "上下文标签第一段" },
    { start_ms: 200000, end_ms: 204000, text: "上下文标签第二段" },
  ];
  store.set("activeLecture", { course_id: "c-w2c", sub_id: "l-w2c-labels", sub_title: "可访问名称讲次", can_stream: true });
  await settle();
  const rows = byId["transcript-list"].querySelectorAll(".transcript-row");
  assert.equal(rows.length, 2, "合成字幕渲染两行");
  /* A11Y-IMPL-1（D14 P2-3）：字幕面列表语义——容器 role=list + 行 role=listitem */
  assert.equal(byId["transcript-list"].getAttribute("role"), "list", "字幕容器行态带 list 语义");
  assert.ok(rows.every((row) => row.getAttribute("role") === "listitem"), "每条字幕行带 listitem 语义");
  const rowNames = rows.map((row) => {
    const time = row.querySelector(".timestamp-button");
    const bookmarkButton = row.querySelector(".icon-button");
    assert.notEqual(time, null, "每行渲染可见时间按钮");
    assert.notEqual(bookmarkButton, null, "每行渲染书签图标按钮");
    return {
      visible: time.textContent,
      jump: time.getAttribute("aria-label"),
      mark: bookmarkButton.getAttribute("aria-label"),
    };
  });
  assert.equal(rowNames[0].visible, "1:05", "可见时间文本保持 m:ss 原样");
  assert.equal(rowNames[0].jump, "跳转并播放到 1:05", "时间按钮可访问名称含该行时间");
  assert.equal(rowNames[0].mark, "在 1:05 加入书签", "书签按钮可访问名称含该行时间");
  assert.ok(rowNames[0].jump.includes(rowNames[0].visible), "时间按钮名称包含可见时间值");
  assert.ok(rowNames[0].mark.includes(rowNames[0].visible), "书签按钮名称包含可见时间值");
  assert.notEqual(rowNames[0].jump, rowNames[0].mark, "同一行两个控件名称可区分");
  assert.equal(rowNames[1].jump, "跳转并播放到 3:20", "第二行时间按钮名称取自该段时间");
  assert.equal(rowNames[1].mark, "在 3:20 加入书签", "第二行书签按钮名称取自该段时间");
  assert.notEqual(rowNames[1].jump, rowNames[0].jump, "不同行时间按钮名称可区分");
  assert.notEqual(rowNames[1].mark, rowNames[0].mark, "不同行书签按钮名称可区分");

  syntheticTranscriptSegments = [];
  store.set("activeLecture", null);
  await settle();
  assert.equal(store.transcriptHasTiming, false, "复位后字幕计时态清除");
  assert.equal(byId["transcript-list"].getAttribute("role"), null, "空态摘除 list 语义（空段落不挂空列表）");
}

/* ---- 3) 密码显示/隐藏（仅登录 dialog） ---- */

async function verifyPasswordToggle() {
  const input = byId["login-password"];
  const toggle = byId["toggle-password-visibility"];
  input.type = "password"; /* 对应 index.html 初始 type="password" */
  input.setAttribute("autocomplete", "current-password");
  assert.equal(input.type, "password", "默认掩码");

  toggle.click();
  assert.equal(input.type, "text", "点击后显示明文");
  assert.equal(toggle.getAttribute("aria-label"), "隐藏密码", "显示态标签");
  assert.equal(toggle.getAttribute("aria-pressed"), "true", "aria-pressed 同步");
  assert.equal(toggle.getAttribute("aria-controls"), "login-password");
  assert.equal(eyeOn.hidden, false, "睁眼图标显示");
  assert.equal(eyeOff.hidden, true, "闭眼图标隐藏");
  assert.equal(input.getAttribute("autocomplete"), "current-password", "autocomplete 不变");

  toggle.click();
  assert.equal(input.type, "password", "再次点击回到掩码");
  assert.equal(toggle.getAttribute("aria-label"), "显示密码");
  assert.equal(toggle.getAttribute("aria-pressed"), "false");
  assert.equal(eyeOn.hidden, true);
  assert.equal(eyeOff.hidden, false);

  /* 值与光标保持 */
  input.value = "synthetic-secret";
  input.setSelectionRange(4, 9);
  toggle.click();
  assert.equal(input.value, "synthetic-secret", "翻转 type 不改值");
  assert.equal(input.selectionStart, 4, "选区起点保持");
  assert.equal(input.selectionEnd, 9, "选区终点保持");
  assert.equal(input.type, "text");
  toggle.click();
  assert.equal(input.type, "password");

  /* 打开即复位为掩码（openLoginDialog 派发 courselens:login-dialog-open） */
  toggle.click();
  assert.equal(input.type, "text");
  byId["login-status"].hidden = false;
  byId["login-status"].textContent = "过期状态";
  byId["login-error"].hidden = false;
  byId["login-error"].textContent = "过期错误";
  byId["login-submit"].disabled = true;
  byId["login-submit"].setAttribute("aria-busy", "true");
  openLoginDialog();
  assert.equal(byId["login-dialog"].open, true, "dialog 打开");
  assert.equal(input.type, "password", "重开后默认掩码");
  assert.equal(toggle.getAttribute("aria-label"), "显示密码");
  assert.equal(toggle.getAttribute("aria-pressed"), "false");
  assert.equal(eyeOn.hidden, true);
  assert.equal(eyeOff.hidden, false);
  assert.equal(byId["login-submit"].disabled, false, "重开提交可用");
  assert.equal(byId["login-status"].hidden, true, "重开状态行清空");
  assert.equal(byId["login-error"].hidden, true, "重开错误区隐藏");
  assert.equal(byId["login-form"].getAttribute("aria-busy"), "false", "重开解除忙态");
  byId["login-dialog"].close();
  await settle();
}

/* ---- 4) 登录：防重复提交 / 阶段反馈 / 闭集失败与重试 ---- */

async function verifyLoginSubmitFlow() {
  const form = byId["login-form"];
  const submit = byId["login-submit"];
  const status = byId["login-status"];
  const error = byId["login-error"];
  const posts = () => calls.authPost.length;

  /* D-20261009-10 行为钉对齐真实使用：登录提交只发生在对话框开态
     （轮询守卫含 dialog.open 检查），mock 派发前先开框。 */
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  byId["remember-account"].checked = true;

  /* 成功路径：202 后轮询，checking 阶段文案 → ready 终态自动收尾 */
  pendingAuthPost = deferred();
  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(posts(), 1, "提交发出一次登录请求");
  assert.equal(submit.disabled, true, "进行中提交禁用");
  assert.equal(form.getAttribute("aria-busy"), "true", "表单 aria-busy");
  assert.equal(status.hidden, false, "忙态状态行可见");
  assert.equal(status.textContent, "正在登录，请稍候。");
  assert.equal(error.hidden, true, "未失败时不显示错误区");

  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(posts(), 1, "进行中重复提交被忽略");

  pendingAuthPost.resolve(ok({ action: "login", authentication: { state: "checking", code: "fudan_session_checking" }, operation_id: "op-synthetic" }, 202));
  await settle();
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), true, "提交受理后启动 dialog 内轮询");

  authSnapshot = { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true };
  await tickIntervals(2000);
  assert.equal(status.textContent, "后端正在连接并验证复旦会话，请稍候。", "闭集阶段文案");
  assert.equal(error.hidden, true);

  authPostErrorCode = "";
  authSnapshot = { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, false, "ready 终态关闭 dialog");
  assert.equal(byId["login-password"].value, "", "成功后清空密码");
  assert.ok(visibleText().includes("登录成功，已连接复旦课程平台"), "成功 toast（清单 #1：内部状态词不上屏）");
  assert.equal(submit.disabled, false, "收尾后提交恢复");
  assert.equal(status.hidden, true, "状态行收尾隐藏");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, "轮询已停止");

  /* F8（N5FE-P1）：后端卡 checking 超过 10s → 硬超时回落。按钮恢复可点并
     换「重新登录」，live 状态给诚实说明；轮询保持存活，ready 终态仍自动
     收口。设置页入口在 checking 期也不再禁用（唯一逃生门）。 */
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  assert.equal(byId["open-login"].disabled, false, "F8：checking 不禁用设置页登录入口");
  assert.equal(byId["open-login"].textContent, "验证中", "F8：入口保留诚实状态文案");

  pendingAuthPost = deferred();
  openLoginDialog(); /* F8/D-10 钉对齐真实使用：提交发生在对话框开态 */
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  form.dispatchEvent(new Event("submit"));
  await settle();
  pendingAuthPost.resolve(ok({ action: "login", authentication: { state: "checking", code: "fudan_session_checking" }, operation_id: "op-synthetic" }, 202));
  await settle();
  assert.equal(submit.disabled, true, "F8：前 10s 提交仍禁用（防重复提交不变）");
  await tickTimeouts(10000);
  assert.equal(submit.disabled, false, "F8：10s 硬超时回落，提交不再永久禁用");
  assert.equal(submit.textContent, "重新登录", "F8：按钮给出可行动标签");
  assert.match(status.textContent, /登录用时较长/, "F8：诚实说明后台仍在继续");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), true, "F8：轮询仍在，终态照常收口");
  authSnapshot = { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, false, "F8 回落后 ready 终态自动关框");
  assert.equal(submit.disabled, false, "F8 收尾后提交恢复");
  byId["login-dialog"].close();
  await settle();

  /* D-20261009-10（REMOTE-E2E-1-R4 真实慢网发现）：POST 受理迟到——F8 硬超时
     已回落后响应才到（真实 UIS 慢网 12s+ 形态）。修后钉：迟到受理仍启动轮询，
     后端 ready 后 ≤2s 对话框自关 + 成功收尾。「不应发生」=对话框持续 open 拦截。 */
  authSnapshot = { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false };
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  pendingAuthPost = deferred();
  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(submit.disabled, true, "D-10：提交进行中禁用");
  await tickTimeouts(10000);
  assert.equal(submit.textContent, "重新登录", "D-10：10s 硬超时先回落（真实慢网前端形态）");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, "D-10：回落时轮询尚未启动（缺陷前提）");
  pendingAuthPost.resolve(ok({ action: "login", authentication: { state: "checking", code: "fudan_session_checking" }, operation_id: "op-synthetic" }, 202));
  await settle();
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), true, "D-10：迟到受理启动进度轮询（修后行为）");
  authSnapshot = { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, false, "D-10：迟到成功后对话框自动关闭（不应发生=持续拦截）");
  assert.equal(submit.disabled, false, "D-10：收尾后提交恢复");
  byId["login-dialog"].close();
  await settle();

  /* 失败路径（degraded + 已知代码）：dialog 保持打开、闭集文案、可重试 */
  pendingAuthPost = null; /* 恢复默认 202，避免复用已消费的 Response */
  authSnapshot = { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false };
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  form.dispatchEvent(new Event("submit"));
  await settle();
  authSnapshot = { state: "degraded", code: "fudan_login_failed", actions: ["login"], connected: false, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, true, "失败时 dialog 保持打开");
  assert.equal(error.hidden, false, "失败显示内联错误区");
  assert.equal(error.textContent, "登录未完成：可能是网络或校园服务波动，系统已自动重试仍未成功。请稍候约 90 秒后再试。", "已知代码闭集文案（诚实波动措辞）");
  assert.equal(visibleText().includes("fudan_login_failed"), false, "失败代码不进入可见 DOM");
  assert.equal(submit.disabled, false, "失败后提交可重试");
  assert.equal(form.getAttribute("aria-busy"), "false");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, "失败后轮询停止");
  byId["login-dialog"].close();
  await settle();

  /* 阶段行：checking 快照带 attempt/max_attempts/step → 第 N/M 次 + 闭集步骤名；未知 step 省略 */
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  form.dispatchEvent(new Event("submit"));
  await settle();
  authSnapshot = { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true, step: "webvpn", attempt: 2, max_attempts: 3 };
  await tickIntervals(2000);
  assert.equal(status.textContent, "正在验证 · 第 2/3 次 · 校园网关", "阶段行显示尝试次数与校园网关");
  authSnapshot = { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true, step: "icourse", attempt: 3, max_attempts: 3 };
  await tickIntervals(2000);
  assert.equal(status.textContent, "正在验证 · 第 3/3 次 · 课程平台", "icourse 步骤映射课程平台");
  authSnapshot = { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true, step: "mystery_step", attempt: 1, max_attempts: 3 };
  await tickIntervals(2000);
  assert.equal(status.textContent, "正在验证 · 第 1/3 次", "未知 step 省略，仅显示次数");
  authSnapshot = { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true };
  await tickIntervals(2000);
  assert.equal(status.textContent, "后端正在连接并验证复旦会话，请稍候。", "缺失尝试字段回到通用阶段文案");

  /* 凭据被拒（degraded + fudan_credentials_rejected）：闭集新文案，不指责网络 */
  authSnapshot = { state: "degraded", code: "fudan_credentials_rejected", actions: ["login"], connected: false, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, true, "凭据被拒时 dialog 保持打开");
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "学号或密码不正确，请核对后重新输入。", "凭据被拒渲染专属闭集文案");
  assert.equal(visibleText().includes("fudan_credentials_rejected"), false, "失败代码不进入可见 DOM");
  assert.equal(submit.disabled, false, "凭据被拒后可重试");

  /* P1-A 挑战感知：挑战/锁定/维护各自渲染专属闭集文案；失败即暂停等待
     用户（轮询停止、不自动重试、错误代码不进 DOM） */
  const challengeAwareCases = [
    ["fudan_challenge_required", "需要在复旦页面完成安全验证。请重新登录并按页面提示完成验证；系统不会自动重试。"],
    ["fudan_account_locked", "账号已被锁定或冻结，请先在复旦账号服务解除锁定，再回来登录。"],
    ["fudan_service_maintenance", "校园服务暂时维护，请稍后重试。"],
  ];
  for (const [failureCode, expectedText] of challengeAwareCases) {
    openLoginDialog();
    byId["login-student-id"].value = "21300180001";
    byId["login-password"].value = "synthetic-secret";
    form.dispatchEvent(new Event("submit"));
    await settle();
    authSnapshot = { state: "degraded", code: failureCode, actions: ["login"], connected: false, configured: true };
    await tickIntervals(2000);
    assert.equal(byId["login-dialog"].open, true, `${failureCode} dialog 保持打开`);
    assert.equal(error.hidden, false);
    assert.equal(error.textContent, expectedText, `${failureCode} 专属闭集文案`);
    assert.equal(visibleText().includes(failureCode), false, "失败代码不进入可见 DOM");
    assert.equal(submit.disabled, false, `${failureCode} 暂停后由用户决定是否重试`);
    assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, `${failureCode} 失败后轮询停止`);
  }
  authSnapshot = { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false };
  byId["login-dialog"].close();
  await settle();

  /* 提交被拒（未知代码）：诚实兜底文案 */
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  authPostErrorCode = "mystery_future_code";
  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(error.hidden, false);
  assert.equal(error.textContent, "登录暂时未完成。请稍候重试；若持续失败，可在设置中复制连接诊断。", "未知代码走通用兜底");
  assert.equal(submit.disabled, false, "被拒后可重试");
  assert.equal(byId["login-dialog"].open, true);

  /* 已知 400 代码映射闭集文案 */
  authPostErrorCode = "authentication_request_invalid";
  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(error.textContent, "登录请求未被接受。请检查输入后重试。", "已知 400 代码闭集文案");
  authPostErrorCode = "";
  byId["login-dialog"].close();
  await settle();

  /* 关闭于提交进行中：迟到的 POST 响应不得重启轮询；重开为干净掩码态 */
  pendingAuthPost = deferred();
  authSnapshot = { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false };
  openLoginDialog();
  byId["login-student-id"].value = "21300180001";
  byId["login-password"].value = "synthetic-secret";
  const postsBeforeClose = posts();
  form.dispatchEvent(new Event("submit"));
  await settle();
  assert.equal(posts(), postsBeforeClose + 1, "提交已发出且仍在途");
  byId["login-dialog"].close();
  await settle();
  assert.equal(byId["login-dialog"].open, false, "进行中关闭生效");
  assert.equal(byId["login-password"].value, "", "关闭即清空已输入密码");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, "关闭后无 2s 轮询");
  pendingAuthPost.resolve(ok({ action: "login", authentication: { state: "checking", code: "fudan_session_checking" }, operation_id: "op-synthetic" }, 202));
  await settle();
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), false, "迟到的 POST 响应不重启轮询");
  assert.equal(byId["login-dialog"].open, false, "迟到响应不重开 dialog");

  openLoginDialog();
  assert.equal(byId["login-dialog"].open, true, "重开 dialog");
  assert.equal(submit.disabled, false, "重开提交可用");
  assert.equal(status.hidden, true, "重开状态行清空");
  assert.equal(status.textContent, "");
  assert.equal(error.hidden, true, "重开错误区隐藏");
  assert.equal(byId["login-password"].type, "password", "重开默认掩码");
  assert.equal(byId["login-password"].value, "", "重开密码为空");
  assert.equal(byId["toggle-password-visibility"].getAttribute("aria-label"), "显示密码", "重开切换复位");
  assert.equal(byId["toggle-password-visibility"].getAttribute("aria-pressed"), "false", "重开 aria-pressed 复位");
  byId["login-dialog"].close();
  await settle();
}

/* ---- 4b) 登录 dialog 已保存账号选择器（S10-A）：空态/预选一键免密/多账号不代选/
   rotation 引导手动/读取失败诚实未知/手动模式显式标记；任何路径不发送或显示密码 ---- */

async function verifyLoginSavedAccountSelector() {
  const section = byId["login-saved"];
  const options = byId["login-saved-options"];
  const savedStatus = byId["login-saved-status"];
  const useButton = byId["login-use-saved"];
  const manualButton = byId["login-manual-mode"];

  /* 空态：诚实呈现，一键不可用，手动登录不受影响 */
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  openLoginDialog();
  await settle();
  assert.equal(section.hidden, false, "空态分区诚实可见");
  assert.equal(savedStatus.hidden, false, "空态状态行可见");
  assert.ok(savedStatus.textContent.includes("暂无已保存账号"), "空态闭集文案");
  assert.equal(useButton.disabled, true, "无账号不可一键使用");
  byId["login-dialog"].close();
  await settle();

  /* 恰一个可用账号：预选 + 一键 use-saved（请求只带账号标识，无密码字段） */
  accountsSnapshot = { accounts: [{ student_id: "21300180001", requires_rotation: false }], deepseek: { configured: false, saved: false } };
  openLoginDialog();
  await settle();
  assert.equal(section.hidden, false);
  assert.equal(savedStatus.hidden, true, "读取成功无状态文案");
  assert.equal(options.children.length, 1, "选项已渲染");
  const singleOption = options.children[0];
  assert.equal(singleOption.getAttribute("aria-pressed"), "true", "恰一个可用账号预选");
  /* FakeElement 的 textContent 不聚合子节点：选项文案扫描 children */
  const optionText = () => singleOption.children.map((node) => node.textContent).join("");
  assert.equal(optionText().includes("21300180001"), true, "学号是非秘密账户标签可展示");
  assert.equal(optionText().includes("已保存密码"), true, "闭集保存状态");
  assert.equal(optionText().includes("synthetic-secret"), false, "绝不显示密码");
  assert.equal(useButton.disabled, false, "一键使用可用");
  pendingAuthPost = null; /* 恢复默认 202：提交流遗留的已消费 Response 不得复用 */
  const postsBeforeUseSaved = calls.authPost.length;
  useButton.click();
  await settle();
  const useSavedPost = calls.authPost[postsBeforeUseSaved];
  assert.equal(useSavedPost.action, "use-saved", "显式 use-saved 动作");
  assert.equal(useSavedPost.student_id, "21300180001", "只发送账号标识");
  assert.equal("password" in useSavedPost, false, "绝不发送空密码字段");
  assert.equal([...intervals.values()].some((timer) => timer.ms === 2000), true, "use-saved 受理后与手动登录同一轮询");
  /* F13（N5FE-P3）：脏账号记录渲染守卫——字面 undefined 不再当学号展示，
     一键使用禁用；保留「移除」作为清理出口。脏快照在 ready tick 前就位，
     由 finishLoginSuccess 自带的 loadAccounts 消费。 */
  accountsSnapshot = {
    accounts: [{ student_id: "undefined", requires_rotation: false }],
    deepseek: { configured: false, saved: false },
  };
  authSnapshot = { state: "ready", code: "fudan_session_verified", actions: ["logout"], connected: true, configured: true };
  await tickIntervals(2000);
  assert.equal(byId["login-dialog"].open, false, "ready 终态关闭 dialog");
  await settle();
  {
    const dirtyRow = byId["account-list"].children[0];
    const rowText = dirtyRow.children.map((node) => node.textContent).join("|");
    assert.match(rowText, /记录异常/, "脏记录不再展示字面 undefined 学号");
    assert.doesNotMatch(rowText, /undefined/, "字面 undefined 不进入可见 DOM");
    const dirtyButtons = dirtyRow.querySelectorAll("button");
    const dirtyUse = dirtyButtons.find((b) => b.textContent === "使用");
    const dirtyRemove = dirtyButtons.find((b) => b.textContent === "移除");
    assert.equal(dirtyUse.disabled, true, "脏记录一键使用禁用");
    assert.equal(dirtyRemove.disabled, false, "脏记录保留移除清理出口");
  }
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  await settle();


  /* 多个可用账号：只列出不代选；rotation 可见但一键禁用并引导手动更新 */
  accountsSnapshot = { accounts: [
    { student_id: "21300180001", requires_rotation: false },
    { student_id: "21300180002", requires_rotation: false },
    { student_id: "21300180003", requires_rotation: true },
  ], deepseek: { configured: false, saved: false } };
  openLoginDialog();
  await settle();
  assert.equal(options.children.length, 3);
  assert.equal(
    options.children.every((node) => node.getAttribute("aria-pressed") === "false"),
    true,
    "多账号不代选、不猜测",
  );
  assert.equal(useButton.disabled, true, "未选择不可一键使用");
  const rotationOption = options.children.find((node) => node.dataset.studentId === "21300180003");
  assert.equal(rotationOption.children.map((node) => node.textContent).join("").includes("需要更新密码"), true, "rotation 闭集状态可见");
  rotationOption.click();
  await settle();
  assert.equal(useButton.disabled, true, "rotation 不可一键使用");
  assert.equal(savedStatus.textContent, "该账号的已保存密码需要更新：请输入新密码登录一次。", "rotation 引导手动更新");
  assert.equal(byId["login-student-id"].value, "21300180003", "rotation 预填学号转手动路径");
  const usableOption = options.children.find((node) => node.dataset.studentId === "21300180002");
  usableOption.click();
  await settle();
  assert.equal(useButton.disabled, false, "改选可用账号后一键恢复");
  assert.equal(savedStatus.hidden, true, "可用选择不残留 rotation 文案");
  manualButton.click();
  assert.equal(manualButton.getAttribute("aria-pressed"), "true", "手动模式显式 aria-pressed");
  /* 一张身份卡两个状态：手动态收起身份卡分区，返回链恢复（T7 状态机行为钉） */
  assert.equal(byId["login-saved"].hidden, true, "手动态收起身份卡分区");
  assert.equal(byId["login-manual-fields"].hidden, false, "手动态展开手动区");
  assert.equal(byId["login-manual-actions"].hidden, false, "手动态显示取消/登录动作行");
  byId["login-back-saved"].click();
  await settle();
  assert.equal(byId["login-saved"].hidden, false, "返回已保存重新展开身份卡");
  assert.equal(byId["login-manual-fields"].hidden, true, "返回已保存收起手动区");
  assert.equal(byId["login-use-saved"].disabled, false, "返回后已选可用账号的一键按钮保持可用");
  byId["login-dialog"].close();
  await settle();
  assert.equal(manualButton.getAttribute("aria-pressed"), "false", "重开复位手动模式标记");

  /* 读取失败：诚实未知 + 手动登录仍可用 */
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    if (String(path) === "/api/v3/accounts") return errorResponse("synthetic_accounts_unavailable");
    return realFetch(path, options);
  };
  openLoginDialog();
  await settle();
  assert.equal(savedStatus.hidden, false);
  assert.ok(savedStatus.textContent.includes("暂时无法读取"), "错误态诚实呈现");
  assert.equal(useButton.disabled, true, "读取失败不可一键使用");
  assert.equal(byId["login-submit"].disabled, false, "手动登录不受影响");
  byId["login-dialog"].close();
  globalThis.fetch = realFetch;
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  await settle();
}

/* ---- 1b) 任务身份（类型闭集 + 讲次上下文）与已完成折叠 ---- */

async function verifyTaskIdentityAndCollapse() {
  const { formatTime } = await import("../frontend/modules/ui.js");
  store.set("courses", [{
    course_id: "c1", title: "课程一",
    lectures: [{ sub_id: "s1", sub_title: "第6-7节", date: "2026-07-09" }],
  }]);
  taskPayload = {
    counts: { active: 2, failed: 0, completed: 2 },
    tasks: [
      { task_id: "k1", kind: "subtitle", state: "running", display_state: "remote_running", course_id: "c1", sub_id: "s1", progress: { label: "转写中" }, updated_at: 200 },
      { task_id: "k2", kind: "quiz", state: "completed", course_id: "c1", sub_id: "s1", finished_at: 300, updated_at: 300 },
      { task_id: "k3", kind: "brand_new_kind", state: "completed", course_id: "c1", sub_id: "s2", finished_at: 400, updated_at: 400 },
      { task_id: "k4", kind: "summary", state: "running", display_state: "remote_running", course_id: "c-missing", sub_id: "s9", progress: {}, updated_at: 200 },
    ],
  };
  window.dispatchEvent(new Event("courselens:tasks-refresh"));
  await settle();

  /* 主行渲染课程名视觉锚（U2 主标识重排）；次级上下文行只放类型 · 讲次名，
     讲次课表日期不再占一处（日期恰一次，由相对时间槽唯一承载） */
  assert.ok(visibleText().includes("课程一"), "任务卡主行渲染课程名视觉锚");
  assert.ok(visibleText().includes("字幕任务 · 第6-7节"), "上下文行渲染类型 · 讲次名");
  assert.equal(visibleText().includes("2026-07-09"), false, "讲次课表日期不再出现在卡片上（日期恰一次）");
  assert.ok(visibleText().includes("摘要任务"), "未知课程任务仍有类型标签");
  assert.equal(visibleText().includes("s1"), false, "原始 sub_id 不进入 DOM");
  /* 未知 kind 诚实回退原始字符串（作为卡片标题出现）；历史记录为抽屉级单个 details（三层结构） */
  const history = deepElements(byId["task-list"])
    .find((node) => String(node.className || "").split(/\s+/).includes("task-history"));
  assert.ok(history, "历史记录渲染为抽屉级单个 task-history details");
  assert.equal(history.open, false, "task-history 默认折叠");
  const historyCards = deepElements(history).filter((node) => String(node.className || "").split(/\s+/).includes("task"));
  assert.equal(historyCards.length, 2, "历史卡片（含取消）都在 task-history 内");
  assert.equal(deepElements(byId["task-list"]).filter((node) => node.dataset?.state === "completed" && !history.contains(node)).length, 0, "层内不再平铺已完成卡片");
  const summary = history.children.find((node) => node.tagName === "SUMMARY");
  assert.equal(summary.textContent, `历史记录 · 2 已完成 · 0 已取消 · 最近 ${formatTime(400)}`, "折叠摘要 = 历史 · 已完成/已取消计数 + 最近时间");
  assert.equal(visibleText().includes("历史记录 · 2 已完成 · 0 已取消 · 最近"), true, "折叠时摘要可见");
  assert.equal(visibleText().includes("brand_new_kind"), false, "折叠时已完成卡片内容不可见");
  assert.equal(visibleText().includes("quiz"), false, "折叠时不渲染已完成卡片标题");

  /* 展开（toggle）后完整卡片可见 */
  history.open = true;
  assert.equal(visibleText().includes("brand_new_kind"), true, "展开后诚实显示未知 kind 原始字符串");
  assert.equal(visibleText().includes("测验任务"), true, "展开后渲染已完成卡片");
  history.open = false;
  assert.equal(visibleText().includes("brand_new_kind"), false, "再次折叠后内容不可见");
  assert.equal(visibleText().includes("测验任务"), false, "再次折叠后内容不可见");
}

/* U⑩+AS3：四档人话预设卡（省着用/日常够用/放开用/不限）——点卡即存、
   aria-checked 同步、裸数字收进用量视图、哨兵 0=不限档诚实文案 */
async function verifyMaxDeepseekTokensPanel() {
  selectPage("settings");
  await settle();
  const container = byId["deepseek-budget-presets"];
  /* 桩 DOM 按真实 markup 结构补齐四张档位卡（生产结构见 index.html；0=不限） */
  for (const value of [100000, 300000, 1000000, 0]) {
    const card = register(new FakeElement("button", `budget-preset-${value}`));
    card.className = "budget-preset";
    card.dataset.budget = String(value);
    card.setAttribute("role", "radio");
    card.setAttribute("aria-checked", "false");
    container.append(card);
  }
  const cardFor = (value) => container.querySelectorAll(".budget-preset")
    .find((node) => node.dataset.budget === String(value));
  const label = byId["max-deepseek-tokens-label"];
  const usage = byId["ai-usage-month"];
  assert.match(String(label.textContent || ""), /当前上限：10 万 tokens\/日/, "回显当前档位（缺省 10 万）");
  assert.match(String(usage.textContent || ""), /解释 3 次/, "Z2 本月用量含解释计数");
  assert.match(String(usage.textContent || ""), /总结 2 讲/, "Z2 本月用量含总结计数");
  assert.match(String(usage.textContent || ""), /当前上限 10 万 tokens\/日/, "裸 token 数字收进用量视图");
  assert.equal(visibleText().includes("answer_budget"), false, "AS3：设置面无 answer_budget 残留");

  /* 点「日常够用」→ 立即保存，选中态按后端回读同步 */
  settingsActionResponse = { schema: "courselens.max-deepseek-tokens.v1", max_deepseek_tokens: 300000 };
  cardFor(300000).dispatchEvent(new Event("click", { bubbles: true }));
  await settle();
  assert.deepEqual(calls.settingsActionsPost.at(-1),
    { action: "set-max-deepseek-tokens", tokens: 300000, operation_id: calls.settingsActionsPost.at(-1).operation_id },
    "点卡即保存闭集动作");
  assert.equal(cardFor(300000).getAttribute("aria-checked"), "true", "选中态按后端回读同步");
  assert.equal(cardFor(100000).getAttribute("aria-checked"), "false", "旧档位取消选中");
  assert.match(String(byId["max-deepseek-tokens-state"].textContent || ""), /每天最多 30 万/, "保存成功诚实复述");
  assert.match(String(label.textContent || ""), /当前上限：30 万 tokens\/日/, "当前上限行随保存更新");

  /* AS3：点「不限」→ 保存哨兵 0，文案与选中态诚实切到不限 */
  settingsActionResponse = { schema: "courselens.max-deepseek-tokens.v1", max_deepseek_tokens: 0 };
  cardFor(0).dispatchEvent(new Event("click", { bubbles: true }));
  await settle();
  assert.deepEqual(calls.settingsActionsPost.at(-1),
    { action: "set-max-deepseek-tokens", tokens: 0, operation_id: calls.settingsActionsPost.at(-1).operation_id },
    "不限档点卡即保存哨兵 0");
  assert.equal(cardFor(0).getAttribute("aria-checked"), "true", "不限档选中态按回读同步");
  assert.equal(cardFor(300000).getAttribute("aria-checked"), "false", "数字档位取消选中");
  assert.match(String(byId["max-deepseek-tokens-state"].textContent || ""), /不限制每日用量/, "不限档保存文案");
  assert.match(String(label.textContent || ""), /当前上限：不设每日上限/, "当前上限行不限档");

  /* AS3：快照回读 0（不限档）时，回显与用量行都不落「0 万」错数字 */
  selectPage("tasks");
  await settle();
  settingsSnapshot = { ...settingsSnapshot, max_deepseek_tokens: 0 };
  selectPage("settings");
  await settle();
  assert.match(String(byId["max-deepseek-tokens-label"].textContent || ""), /不设每日上限/, "快照 0 回显不限档");
  assert.match(String(byId["ai-usage-month"].textContent || ""), /每日上限不限/, "用量行不限档不带 0 万错数字");

  /* 失败给闭集校验提示 */
  settingsActionResponse = { error: "max_deepseek_tokens_invalid" };
  cardFor(1000000).dispatchEvent(new Event("click", { bubbles: true }));
  await settle();
  assert.match(String(byId["max-deepseek-tokens-state"].textContent || ""), /整十万档位/, "失败给闭集校验提示");
  settingsActionResponse = null;
}

/* AS6 消耗透镜：本机累计行 + 余额行（设置页打开时拉一次；失败/无 key 闭集人话态） */
async function verifyTaskUsageAndBalanceLines() {
  selectPage("settings");
  await settle();
  const usage = document.getElementById("task-usage-month");
  assert.ok(usage, "本机累计行随设置页建立（运行时建节点，不解析 HTML）");
  assert.match(String(usage.textContent || ""), /本月云端消耗/, "本机累计行含消耗合计");
  assert.match(String(usage.textContent || ""), /本机累计/, "诚实标注本机累计口径");
  assert.match(String(usage.textContent || ""), /13 分钟/, "runner 分钟人话呈现（四舍五入）");
  assert.match(String(usage.textContent || ""), /≈1\.2 万 tokens/, "token 人话单位");
  const balance = document.getElementById("deepseek-balance-state");
  assert.ok(balance, "余额行随设置页建立");
  await settle();
  assert.match(String(balance.textContent || ""), /86\.50 CNY/, "余额快照如实渲染");

  /* 无 key：诚实闭集文案，不编造余额 */
  balanceSnapshot = { state: "no_key", schema: "courselens.deepseek-balance.v1" };
  selectPage("tasks");
  await settle();
  selectPage("settings");
  await settle();
  await settle();
  assert.match(String(balance.textContent || ""), /未配置 DeepSeek Key/, "无 key 诚实文案");

  /* 请求失败：暂时查不到，不重试轰炸 */
  balanceSnapshot = "error";
  selectPage("tasks");
  await settle();
  selectPage("settings");
  await settle();
  await settle();
  assert.match(String(balance.textContent || ""), /暂时查不到余额/, "失败态闭集人话文案");
  balanceSnapshot = { state: "ok", is_available: true, currency: "CNY", total_balance: "86.50" };
  console.log("ok: AS6 本机累计+余额行（人话单位/诚实闭集态）");
}

/* ---- 5) DeepSeek 本机保存：中性文案 + inline 状态机（saved×configured 全矩阵） ---- */

async function verifyDeepseekSaveState() {
  selectPage("settings");
  await settle();
  const state = byId["deepseek-save-state"];
  const input = byId["deepseek-key"];
  const remember = byId["remember-deepseek"];
  const save = byId["save-deepseek"];
  const posts = () => calls.secretsPost.length;
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));

  /* 初始 loadAccounts：configured=false, saved=false → 中性“未保存在本机” */
  assert.equal(state.hidden, false, "状态行刷新后可见");
  assert.equal(state.textContent, "未保存在本机", "未保存状态为中性文案");
  assert.equal(byId["delete-deepseek"].disabled, true, "未配置时移除按钮禁用");

  /* 空 key：inline 提示且不发请求 */
  input.value = "   ";
  save.click();
  await settle();
  assert.equal(posts(), 0, "空 key 不发保存请求");
  assert.equal(state.hidden, false);
  assert.equal(state.textContent, "请输入 API Key", "空 key 显示 inline 校验提示");

  /* 保存成功 remember=true：saved=true×configured=true → 已保存在本机（响应与刷新一致） */
  input.value = "sk-synthetic";
  remember.checked = true;
  secretsResponse = { configured: true, saved: true, deleted: false };
  accountsSnapshot = { accounts: [], deepseek: { configured: true, saved: true, requires_rotation: false } };
  save.click();
  await settle();
  assert.equal(posts(), 1, "保存请求已发出");
  assert.deepEqual(calls.secretsPost[0], { action: "set-deepseek", api_key: "sk-synthetic", remember: true });
  assert.equal(input.value, "", "保存成功清空输入");
  assert.equal(state.textContent, "已保存在本机", "saved×configured → 已保存在本机");
  assert.equal(state.dataset.state, "ready", "P58：已保存确认条带 ready 强调态（显眼可辨）");
  assert.equal(toastTexts().at(-1), "DeepSeek Key 已更新，并已保存到本机", "P58：勾选本机 → toast 明示已保存到本机");
  assert.equal(byId["delete-deepseek"].disabled, false, "已配置时移除按钮可用");

  /* 保存成功 remember=false：saved=false×configured=true → 仅本次启动（不得声称已保存） */
  input.value = "sk-synthetic-session";
  remember.checked = false;
  secretsResponse = { configured: true, saved: false, deleted: false };
  accountsSnapshot = { accounts: [], deepseek: { configured: true, saved: false, requires_rotation: false } };
  save.click();
  await settle();
  assert.equal(posts(), 2);
  assert.equal(state.textContent, "仅本次启动使用，未保存在本机", "会话 key 状态如实区分");
  assert.equal(state.dataset.state || "", "", "P58：会话 key 态为中性样式（不带强调色）");
  assert.equal(toastTexts().at(-1), "DeepSeek Key 已更新，仅本次启动有效", "P58：未勾选 → toast 明示仅本次启动有效");

  /* 异常态：saved=true×configured=false → 不得显示“已保存在本机” */
  input.value = "sk-synthetic";
  secretsResponse = { configured: false, saved: true, deleted: false };
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: true, requires_rotation: false } };
  save.click();
  await settle();
  assert.equal(posts(), 3);
  assert.equal(state.textContent, "保存状态异常，本机保存不可用", "异常态诚实提示");
  assert.equal(state.dataset.state, "error", "P58：异常确认条带 error 强调态");
  assert.notEqual(state.textContent, "已保存在本机");

  /* 字段缺失容忍：deepseek 缺 saved → 待确认，不崩溃 */
  accountsSnapshot = { accounts: [], deepseek: { configured: false } };
  selectPage("settings");
  await settle();
  console.error("PROBE settings-evidence:", JSON.stringify(String(byId["settings-evidence"]?.textContent || "")));
  console.error("PROBE accountsSnapshot:", JSON.stringify(accountsSnapshot));
  console.error("PROBE settingsSnapshot.deepseek:", JSON.stringify(settingsSnapshot.deepseek));
  assert.equal(state.textContent, "保存状态待确认", "字段缺失回退待确认且不崩溃");

  /* 保存失败：inline 文案（非只 toast），输入保留可重试 */
  input.value = "sk-synthetic";
  secretsPostErrorCode = "secret_action_invalid";
  save.click();
  await settle();
  assert.equal(posts(), 4);
  assert.equal(state.textContent, "保存失败，请稍后重试", "失败态 inline 可见");
  assert.equal(input.value, "sk-synthetic", "失败后输入保留");
  secretsPostErrorCode = "";

  /* 移除成功：二次确认 → 中性“未保存在本机” */
  accountsSnapshot = { accounts: [], deepseek: { configured: true, saved: true, requires_rotation: false } };
  secretsResponse = { configured: false, saved: false, deleted: true };
  selectPage("settings");
  await settle();
  const del = byId["delete-deepseek"];
  assert.equal(del.disabled, false, "移除前按钮可用");
  del.click();
  await settle();
  assert.equal(posts(), 4, "首次点击仅进入确认态，不发请求");
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  del.click();
  await settle();
  assert.deepEqual(calls.secretsPost[4], { action: "delete-deepseek" });
  assert.equal(state.textContent, "未保存在本机", "移除后回到未保存中性文案");
  secretsResponse = { configured: false, saved: false, deleted: false };
}

/* ---- 6) 直播进入保持学习桌可见：livePlayback 模式键（player-core 写，study 读） ---- */

/* ---- Mailbox 历史记录修复：可修复才出现、确认框、防双击、以后端证据为准 ---- */

async function verifyMailboxReconcileCard() {
  const card = byId["remote-mailbox-reconcile"];
  const button = byId["remote-reconcile-mailbox"];
  const stateLine = byId["remote-mailbox-reconcile-state"];
  const dialog = byId["mailbox-reconcile-dialog"];
  const confirmButton = byId["mailbox-reconcile-confirm"];
  const cancelButton = byId["mailbox-reconcile-cancel"];

  const historyComponent = (code) => ({
    component: "mailbox_history", name: "mailbox_history",
    state: "action_required", code,
    actions: code === "mailbox_closed_unconsumed" ? ["reconcile-mailbox-history"] : [],
    evidence: {}, stale: false,
  });
  const reload = async () => {
    selectPage("settings");
    await settle();
  };

  /* 1) 活动冲突 / unknown / 漂移：不出现修复按钮，只显示恢复建议 */
  const guidanceFragments = {
    mailbox_closed_active_conflict: "冲突",
    mailbox_metadata_drift: "漂移",
    mailbox_issue_missing_active: "不存在",
    github_state_unknown: "重新诊断",
  };
  for (const code of Object.keys(guidanceFragments)) {
    remoteSnapshot = {
      overall: { state: "action_required", code },
      components: [historyComponent(code)],
    };
    await reload();
    assert.equal(card.hidden, true, `${code} 不显示修复卡`);
    const guidance = document.getElementById("remote-evidence").children
      .map((node) => String(node.textContent || "")).join("\n");
    assert.ok(guidance.includes(guidanceFragments[code]), `${code} 有恢复建议文案`);
  }

  /* 1b) P58 诚实降噪：连接已验证 + 盘点未完成（github_state_unknown）→
     中性一行，不再挂「请稍后重新诊断」待办；真异常指引不受降级影响 */
  remoteSnapshot = {
    overall: { state: "ready", code: "remote_verified" },
    components: [historyComponent("github_state_unknown")],
  };
  await reload();
  const neutralGuidance = document.getElementById("remote-evidence").children
    .map((node) => String(node.textContent || "")).join("\n");
  assert.ok(neutralGuidance.includes("不影响正常使用"), "连接已验证：盘点未完成降级为中性一行");
  assert.equal(neutralGuidance.includes("重新诊断"), false, "健康卡不再出现重新诊断待办文案");
  remoteSnapshot = {
    overall: { state: "ready", code: "remote_verified" },
    components: [historyComponent("mailbox_history_missing")],
  };
  await reload();
  const anomalyGuidance = document.getElementById("remote-evidence").children
    .map((node) => String(node.textContent || "")).join("\n");
  assert.ok(anomalyGuidance.includes("被删除"), "真异常在连接已验证时仍显眼保留指引");

  /* 2) 可修复状态：卡片与按钮出现，accessible name 完整 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "mailbox_closed_unconsumed" },
    components: [historyComponent("mailbox_closed_unconsumed")],
  };
  await reload();
  assert.equal(card.hidden, false, "可修复状态显示修复卡");
  assert.equal(button.textContent, "修复历史记录");
  assert.equal(button.getAttribute("aria-describedby"), "remote-mailbox-reconcile-hint");
  assert.ok(byId["remote-mailbox-reconcile-hint"].textContent.includes("保留评论"), "说明包含保留评论承诺");

  /* 3) 点击先弹确认框；取消不产生请求 */
  let posts = calls.remoteActionPost.length;
  button.click();
  await settle();
  assert.equal(dialog.open, true, "确认框打开");
  assert.ok(byId["mailbox-reconcile-dialog-hint"].textContent.includes("不会删除任何评论"), "确认框声明不删评论");
  assert.ok(byId["mailbox-reconcile-dialog-hint"].textContent.includes("保持关闭"), "确认框声明 remote 保持关闭");
  cancelButton.click();
  await settle();
  assert.equal(dialog.open, false, "取消关闭确认框");
  assert.equal(calls.remoteActionPost.length, posts, "取消不发送请求");

  /* 4) 确认后 busy/防双击；成功后重新读取后端且证据归零才完成 */
  posts = calls.remoteActionPost.length;
  const getsBefore = calls.remoteGet;
  button.click();
  await settle();
  confirmButton.click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts + 1, "确认后发出一次修复请求");
  assert.equal(calls.remoteActionPost[posts].action, "reconcile-mailbox-history");
  assert.ok(typeof calls.remoteActionPost[posts].operation_id === "string"
    && calls.remoteActionPost[posts].operation_id.length >= 8, "携带 operation_id");
  assert.ok(calls.remoteGet > getsBefore, "成功后重新 GET remote-connection");
  /* 后端证据归零（clean）→ 卡片隐藏 */
  remoteSnapshot = {
    overall: { state: "ready", code: "mailbox_clean" },
    components: [historyComponent("mailbox_clean")],
  };
  await reload();
  assert.equal(card.hidden, true, "证据归零后卡片消失");
  assert.equal(stateLine.hidden, true, "完成后无失败文案");

  /* 5) 失败闭集文案 + 按钮恢复 + 焦点回到按钮 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "mailbox_closed_unconsumed" },
    components: [historyComponent("mailbox_closed_unconsumed")],
  };
  await reload();
  assert.equal(card.hidden, false, "失败场景前卡片可见");
  remoteActionResponse = { error: "remote_cleanup_pending" };
  document.activeElement = null;
  button.click();
  await settle();
  confirmButton.click();
  await settle();
  assert.equal(stateLine.hidden, false, "失败显示闭集文案");
  assert.ok(stateLine.textContent.includes("清理"), `失败文案为闭集指引：${stateLine.textContent}`);
  assert.equal(button.disabled, false, "失败后按钮恢复可用");
  assert.equal(document.activeElement, button, "焦点恢复到修复按钮");
  remoteActionResponse = null;

  /* 6) 单一主按钮合同不回退：静态四按钮排已移除，动作仍经状态驱动的主按钮触发 */
  for (const id of ["remote-authorize-github", "remote-bootstrap-worker", "remote-repair-worker", "remote-test-channel"]) {
    assert.equal(byId[id], undefined, `${id} 静态动作入口已随主按钮方案移除`);
  }
  /* P58 条件显示：就绪态不再常驻加密测试主按钮——主行动作合同改由需要态验证 */
  remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
  await reload();
  assert.equal(byId["remote-primary-action"].hidden, true, "就绪态无常驻主按钮（正常态不占版面）");
  assert.equal(
    byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON") || null,
    null,
    "就绪态不渲染加密测试按钮",
  );
  /* 需要态（加密通道）：主按钮=加密测试，仍可触发远程动作 */
  remoteSnapshot = { overall: { state: "action_required", code: "channel_test_required" }, components: [] };
  await reload();
  const primaryActionButton = byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  assert.equal(primaryActionButton.textContent, "加密测试", "需要态主按钮=加密测试");
  assert.equal(byId["remote-primary-action"].hidden, false, "需要态主按钮行可见");
  const actionPosts = calls.remoteActionPost.length;
  primaryActionButton.click();
  await settle();
  assert.equal(calls.remoteActionPost.length, actionPosts + 1, "主推荐按钮仍可触发远程动作");
  assert.equal(calls.remoteActionPost[actionPosts].action, "test-channel", "需要态主按钮触发 test-channel");
  /* 异步反馈收尾（单元②）：test-channel 已进入反馈循环——排水一次 2s 轮询，
     探针就绪即终态；P58 起就绪态主按钮收起，反馈由阶段行承担，不向后续场景泄漏 */
  remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
  await tickTimeouts(2000);
  assert.equal(byId["remote-primary-action"].hidden, true, "反馈循环收尾进入就绪态：主按钮收起");
}

/* ---- GitHub 连接：紧凑摘要 / 首跑有序主操作 / 内联闭集恢复 / 设备码进度仅进行中可见 ---- */

async function verifyGitHubConnectionRecovery() {
  const errorRegion = byId["remote-action-error"];
  const deviceRow = byId["remote-device-authorization"];
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryRow = () => byId["remote-primary-action"];
  const primaryAnchor = () => primaryRow().children.find((node) => node.tagName === "A") || null;
  const primaryButton = () => primaryRow().children.find((node) => node.tagName === "BUTTON") || null;

  /* window.open 间谍：失败/恢复路径绝不新增自动打开；安装引导态（INSTALL-STEP-UX-1）
     允许每个新预选 setup URL 恰自动打开一次，间谍记录 URL 并模拟弹出被拦截（返回 undefined） */
  let openCalls = 0;
  const openUrls = [];
  windowTarget.open = (url) => { openCalls += 1; openUrls.push(String(url || "")); return undefined; };

  assert.equal(errorRegion.getAttribute("role"), "alert", "内联错误区为 announced live region");

  /* 1) 就绪态：一行紧凑摘要，无默认“状态尚未确认”重复块；错误区与设备码进度行隐藏 */
  remoteSnapshot = {
    overall: { state: "ready", code: "ready_for_dispatch" },
    components: [],
    installation_url: "https://github.com/apps/fudan-courselens/installations/new",
  };
  await reload();
  /* INIT-PATH-POLISH-1 单元D：全绿总结态沿触发登场——确认后归档为常规
     连接卡（本场景顺带覆盖确认交互，保证后续场景从归档态开始） */
  const readyEvidence = document.getElementById("remote-evidence");
  const armedSummary = [...readyEvidence.children].find(
    (node) => String(node.dataset?.role || "") === "completion-summary",
  ) || null;
  if (armedSummary) {
    deepElements(armedSummary).find((node) => node.tagName === "BUTTON")?.click();
    await settle();
  }
  assert.equal(document.getElementById("remote-evidence").children[0].textContent, "GitHub 连接正常", "就绪态摘要一行");
  assert.equal(document.getElementById("remote-evidence").children.length, 1, "确认后仅一行摘要，无重复默认状态块");
  const regionText = (id) => document.getElementById(id).children
    .map((node) => String(node.textContent || "")).join("\n");
  assert.equal(regionText("remote-evidence").includes("状态尚未确认"), false, "远程证据区无默认未知状态块");
  assert.equal(regionText("security-evidence").includes("状态尚未确认"), false, "连接安全证据区无默认未知状态块");
  assert.equal(errorRegion.hidden, true, "无失败时错误区隐藏");
  assert.equal(deviceRow.hidden, true, "无进行中授权时进度行隐藏");

  /* CLOUD-CONSENT-AUTO-1 U2：完成总结的「去开启云端处理」随开关退役——
     云端处理默认允许、连接就绪即可用，总结态任何云状态下都只留「知道了」 */
  const summaryButtons = () => {
    const summary = [...document.getElementById("remote-evidence").children].find(
      (node) => String(node.dataset?.role || "") === "completion-summary",
    );
    return summary
      ? deepElements(summary).filter((node) => node.tagName === "BUTTON").map((node) => String(node.textContent || ""))
      : [];
  };
  remoteSnapshot = {
    overall: { state: "degraded", code: "rate_limit_low" },
    components: [],
    installation_url: "https://github.com/apps/fudan-courselens/installations/new",
  };
  await reload();
  remoteSnapshot = {
    overall: { state: "ready", code: "ready_for_dispatch" },
    components: [],
    remote_compute: { configured: true, verified: true },
  };
  await reload();
  assert.deepEqual(summaryButtons(), ["知道了"], "连接就绪：总结态只剩「知道了」");
  remoteSnapshot = {
    overall: { state: "degraded", code: "rate_limit_low" },
    components: [],
  };
  await reload();
  remoteSnapshot = {
    overall: { state: "ready", code: "ready_for_dispatch" },
    components: [],
    remote_compute: { configured: true, verified: false },
  };
  await reload();
  assert.ok(!summaryButtons().includes("去开启云端处理"), "退役入口在任何云状态下都不再出现");
  assert.deepEqual(summaryButtons(), ["知道了"], "验证中态同样只留「知道了」");

  /* 2) 授权缺失 → 唯一主按钮 = 授权并创建专属仓库，指引明示将创建两个专属仓库 */
  const authMissingSnapshot = {
    overall: { state: "action_required", code: "authorization_missing" },
    components: [
      {
        component: "authorization", state: "action_required", code: "authorization_missing",
        actions: ["start-authorization"], evidence: {}, stale: false,
      },
    ],
    installation_url: "https://github.com/apps/fudan-courselens/installations/new",
  };
  remoteSnapshot = authMissingSnapshot;
  await reload();
  assert.equal(primaryButton().textContent, "授权并创建专属仓库", "授权缺失时主按钮明示创建专属仓库");
  assert.equal(primaryAnchor(), null, "仓库尚不存在时绝不渲染安装链接");
  assert.ok(
    regionText("remote-evidence").includes("设备验证") && regionText("remote-evidence").includes("安装 App 并创建专属仓库"),
    "授权指引按新流程序明示后续安装与建仓步骤",
  );

  /* 2b) 普通授权入口不携带 force：主按钮在授权缺失态发起 start-authorization，不清除可复用授权 */
  const diagPosts = calls.remoteActionPost.length;
  primaryButton().click();
  await settle();
  assert.equal(calls.remoteActionPost[diagPosts].action, "start-authorization");
  assert.equal(calls.remoteActionPost[diagPosts].force, false, "普通主按钮授权不携带 force");

  /* 2c) 授权已失效（恢复语义）→ 仅该入口允许 force 清除旧授权后重新授权 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "authorization_revoked" },
    components: [
      {
        component: "authorization", state: "action_required", code: "authorization_revoked",
        actions: ["start-authorization"], evidence: {}, stale: false,
      },
    ],
    installation_url: "https://github.com/apps/fudan-courselens/installations/new",
  };
  await reload();
  assert.equal(primaryButton().textContent, "重新授权并创建专属仓库", "失效授权时主按钮为恢复语义");
  const revokedPosts = calls.remoteActionPost.length;
  remoteActionResponse = { error: "authorization_revoked" };
  primaryButton().click();
  await settle();
  assert.equal(calls.remoteActionPost[revokedPosts].action, "start-authorization");
  assert.equal(calls.remoteActionPost[revokedPosts].force, true, "仅恢复语义的重新授权携带 force");
  remoteActionResponse = null;

  /* 3) 授权失败 installation_scope_not_exact：内联闭集文案 + 受信设置链接 + 重试；toast 保留但非唯一 */
  const scopeSnapshot = {
    ...authMissingSnapshot,
    components: [
      ...authMissingSnapshot.components,
      {
        component: "installation", state: "action_required", code: "installation_scope_not_exact",
        actions: ["restrict-github-app-installation"], stale: false,
        evidence: { settings_url: "https://github.com/settings/installations/42" },
      },
    ],
  };
  remoteSnapshot = scopeSnapshot;
  await reload();
  let posts = calls.remoteActionPost.length;
  const toastsBefore = byId["toast-region"].children.length;
  remoteActionResponse = { error: "installation_scope_not_exact" };
  primaryButton().click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts + 1, "失败请求恰好一次");
  assert.equal(calls.remoteActionPost[posts].action, "start-authorization");
  assert.equal(calls.remoteActionPost[posts].force, false, "普通授权并创建入口不携带 force");
  assert.equal(errorRegion.hidden, false, "失败在主操作旁内联呈现");
  const errorText = errorRegion.children.map((node) => String(node.textContent || "")).join("\n");
  /* 单元E：范围类失败文案改「最小权限+具名清单」框架，禁「不精确」类技术措辞 */
  assert.ok(errorText.includes("遵循最小权限"), `失败文案为最小权限框架：${errorText}`);
  assert.ok(errorText.includes("安装范围"), "失败文案保留范围处置指引");
  assert.equal(errorText.includes("不精确"), false, "失败文案不再使用「不精确」技术措辞");
  assert.equal(errorText.includes("保留 Actions"), false, "「保留 Actions 权限」句已删除");
  const settingsAnchor = errorRegion.children.find((node) => node.tagName === "A");
  assert.notEqual(settingsAnchor, null, "提供受信恢复链接");
  assert.equal(settingsAnchor.getAttribute("href"), "https://github.com/settings/installations/42", "优先使用后端证据中的安装设置链接");
  assert.equal(settingsAnchor.getAttribute("target"), "_blank", "链接新窗口打开");
  assert.ok(String(settingsAnchor.getAttribute("rel") || "").includes("noopener"), "链接带 noopener");
  assert.ok(settingsAnchor.textContent.includes("安装设置"), "链接名称描述目的地");
  const retryButton = errorRegion.children.find((node) => node.tagName === "BUTTON");
  assert.notEqual(retryButton, null, "提供明确重试动作");
  assert.equal(retryButton.textContent, "重试");
  assert.ok(String(byId["toast-region"].children.at(-1)?.textContent || "").includes("遵循最小权限"), "toast 保留为辅助反馈（上限语义下新枚驻末位）");
  const failureToast = String(byId["toast-region"].children.at(-1)?.textContent || "");
  assert.equal(failureToast.includes("synthetic"), false, "失败 toast 不含原始后端文本");
  assert.ok(failureToast.includes("遵循最小权限"), "失败 toast 为中文闭集指引（最小权限框架）");
  assert.equal(errorText.includes("synthetic"), false, "内联错误区不含原始后端文本");
  assert.equal(errorText.includes("Remote operation"), false, "内联错误区不以通用英文错误为反馈");
  assert.equal(openCalls, 0, "失败路径不自动打开任何页面");

  /* 3b) 观测透码：permission_denied 按快照端点类证据复用按码文案表；
     闭集失败码与端点类以 chip 原样可见，不引入任何原始后端文本 */
  const permSnapshot = {
    overall: { state: "action_required", code: "permission_denied" },
    components: [
      {
        component: "authorization", state: "action_required", code: "permission_denied",
        actions: ["start-authorization"], stale: false,
        evidence: { endpoint_class: "repos_detail" },
      },
    ],
  };
  remoteSnapshot = permSnapshot;
  await reload();
  posts = calls.remoteActionPost.length;
  remoteActionResponse = { error: "permission_denied" };
  primaryButton().click();
  await settle();
  const permErrorText = errorRegion.children.map((node) => String(node.textContent || "")).join("\n");
  assert.ok(permErrorText.includes("尚未获得这两个仓库的访问权限"), `repos_detail 端点类细分文案：${permErrorText}`);
  assert.equal(permErrorText.includes("重新授权"), false, "安装/仓库类 403 失败不再推重新授权指引");
  const codeChip = errorRegion.children.find((node) => node.tagName === "CODE");
  assert.notEqual(codeChip, null, "透码 chip 存在");
  assert.equal(codeChip.textContent, "permission_denied · repos_detail", "透码 chip 为闭集码 + 端点类");
  const permToast = String(byId["toast-region"].children.at(-1)?.textContent || "");
  assert.ok(permToast.includes("尚未获得这两个仓库的访问权限"), "toast 与行内区复用同一按码文案表");
  remoteActionResponse = null;

  /* 3c) operation.failed 携带闭集 error_code：走同一文案表并进内联区，不再一律兜底。
     （单元D 起 rate_limited/timeout/github_unreachable 属可自愈族走静默重试，
     本场景改用非自愈码钉「operation.failed 按码呈现」既有语义；
     P58 起就绪态无常驻主按钮，动作宿主改用加密通道需要态） */
  remoteSnapshot = { overall: { state: "action_required", code: "channel_test_required" }, components: [] };
  await reload();
  posts = calls.remoteActionPost.length;
  remoteActionResponse = { operation: { state: "failed", error_code: "worker_trust_unavailable" } };
  primaryButton().click();
  await settle();
  assert.equal(errorRegion.hidden, false, "operation.failed 也进内联区");
  assert.ok(
    String(byId["toast-region"].children.at(-1)?.textContent || "").includes("Worker 签名模板校验未通过"),
    "operation.failed 按 error_code 路由文案（worker_trust_unavailable）",
  );
  const opChip = errorRegion.children.find((node) => node.tagName === "CODE");
  assert.notEqual(opChip, null, "operation.failed 透码 chip 存在");
  assert.equal(opChip.textContent, "worker_trust_unavailable", "透码 chip 携带闭集错误码");
  assert.equal(openCalls, 0, "透码路径同样不自动打开任何页面");
  remoteActionResponse = null;

  /* 4) 安装缺失 + 后端预选 URL → 主操作 = 官方安装页链接（已预选两仓库）+ 继续初始化按钮 */
  const setupUrl = "https://github.com/apps/fudan-courselens/installations/new/permissions"
    + "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202";
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_missing" },
    components: [
      {
        component: "installation", state: "action_required", code: "installation_missing",
        actions: ["bootstrap"], stale: false,
        evidence: { installation_setup_url: setupUrl },
      },
    ],
  };
  remoteActionResponse = null;
  await reload();
  const setupAnchor = primaryAnchor();
  assert.notEqual(setupAnchor, null, "安装缺失且仓库就绪时主行提供预选安装链接");
  assert.equal(setupAnchor.getAttribute("href"), setupUrl, "安装 URL 由后端合成并携带两个预选仓库 id");
  assert.ok(setupAnchor.textContent.includes("预选"), "链接名说明仓库已预选");
  assert.equal(setupAnchor.getAttribute("target"), "_blank", "安装链接新窗口打开");
  assert.ok(String(setupAnchor.getAttribute("rel") || "").includes("noopener"), "安装链接带 noopener");
  const continueButton = primaryButton();
  assert.equal(continueButton.textContent, "完成 App 安装后继续初始化", "安装引导态主按钮与引导块共用同一继续语义");
  posts = calls.remoteActionPost.length;
  continueButton.click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts + 2, "继续初始化发出 bootstrap + 恰一次自动对账");
  assert.equal(calls.remoteActionPost[posts].action, "bootstrap", "继续初始化复用既有 bootstrap 动作");
  assert.equal(
    calls.remoteActionPost[posts + 1].action, "diagnose",
    "bootstrap 成功后自动对账恰一次（复用 diagnose 管道，先探测再刷新卡面）",
  );
  assert.equal(errorRegion.hidden, true, "继续初始化成功后无错误残留");
  /* INSTALL-STEP-UX-1：引导态渲染即对每个新预选 setup URL 恰自动打开一次；
     间谍返回 undefined 模拟弹出被拦截 → 退化为已渲染链接按钮，绝不重试弹 */
  assert.equal(openCalls, 1, "引导态每个新 setup URL 恰自动打开一次");
  assert.equal(openUrls[0], setupUrl, "自动打开的正是后端合成的预选安装页");
  assert.ok(
    deepElements(document.getElementById("remote-evidence")).some(
      (node) => String(node.textContent || "").includes("需要在 GitHub 确认安装"),
    ),
    "引导块随态渲染",
  );
  /* 异步反馈收尾（单元②）：bootstrap 成功已进入反馈循环，桩快照不变 →
     按超时路径排水，解除进行中态后继续后续场景 */
  await drainAsyncFeedback();

  /* 4b) 流程序重排：安装缺失且仓库未建（有受信账号级安装页）→ 主按钮=安装 App
     （All repositories 指引）；旧序「先创建仓库再安装」文案退役 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_missing" },
    components: [
      {
        component: "installation", state: "action_required", code: "installation_missing",
        actions: ["bootstrap"], stale: false,
        evidence: {},
      },
    ],
    installation_url: "https://github.com/apps/fudan-courselens/installations/new",
  };
  await reload();
  const installFirstAnchor = primaryAnchor();
  assert.notEqual(installFirstAnchor, null, "仓库未建时主按钮为安装 App（受信账号级安装页锚）");
  assert.equal(
    installFirstAnchor.getAttribute("href"),
    "https://github.com/apps/fudan-courselens/installations/new",
    "安装链接为后端披露的账号级安装页",
  );
  assert.equal(installFirstAnchor.textContent, "安装 CourseLens App", "安装主按钮文案为产品语义");
  assert.ok(
    String(installFirstAnchor.className || "").includes("primary"),
    "安装主按钮以主样式呈现",
  );
  assert.equal(primaryButton(), null, "仓库未建时不提供建仓按钮（装 App 前置）");
  posts = calls.remoteActionPost.length;
  installFirstAnchor.click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts, "安装主按钮为外链，不发远程动作请求");
  const preReposGuidance = regionText("remote-evidence");
  assert.ok(
    preReposGuidance.includes("All repositories"),
    "指引明示仓库范围先选 All repositories（专属仓库尚未建立，无法精确指定）",
  );
  assert.ok(
    preReposGuidance.includes("收紧到仅两个仓库"),
    "指引预告创建完成后收紧到仅两个仓库",
  );
  assert.ok(
    !preReposGuidance.includes("已自动预选") && !preReposGuidance.includes("已预选"),
    "无预选链接时指引文案绝不出现「已预选」",
  );
  assert.ok(
    !preReposGuidance.includes("先创建两个专属仓库"),
    "旧序「先创建仓库再安装」指引文案退役",
  );

  /* 4b2) T6 深分类：未安装 + 仓库级 403（evidence.repos_denied）+ 仓库组件
     显示已建（awaiting_installation）→ 专用指引「安装前读不了仓库属正常」；
     主按钮为安装语义，绝不「先创建仓库」 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_missing" },
    components: [
      {
        component: "installation", state: "action_required", code: "installation_missing",
        actions: ["bootstrap"], stale: false,
        evidence: { repos_denied: "repos_detail" },
      },
      {
        component: "worker_repository", state: "action_required",
        code: "worker_repository_awaiting_installation", actions: ["bootstrap"], stale: false,
      },
      {
        component: "mailbox_repository", state: "action_required",
        code: "mailbox_repository_awaiting_installation", actions: ["bootstrap"], stale: false,
      },
    ],
  };
  await reload();
  const opensBeforeDenied = openCalls;
  assert.equal(primaryButton().textContent, "完成 App 安装后继续初始化", "仓库已建未安装时主按钮为安装语义");
  assert.ok(
    regionText("remote-evidence").includes("安装完成前，GitHub 会拒绝读取两个专属仓库"),
    "深分类指引解释安装前读不了仓库属正常",
  );
  assert.ok(
    !regionText("remote-evidence").includes("先创建两个专属仓库"),
    "仓库已存在时绝不引导先创建仓库",
  );
  assert.equal(primaryAnchor(), null, "无预选链接时不渲染安装链接");
  assert.equal(openCalls, opensBeforeDenied, "深分类指引自身不新增自动打开");

  /* 4b3) 缺装处方分流：permission_denied 失败且当前证据含 installation_missing
     （仓库未建、授权有效）→ 处方改为「请先安装 CourseLens App」，绝不推重新授权。
     （无受信安装链接时保留建仓按钮，作为失败动作的载体） */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_missing" },
    components: [
      {
        component: "installation", state: "action_required", code: "installation_missing",
        actions: ["bootstrap"], stale: false, evidence: {},
      },
      {
        component: "authorization", state: "ready", code: "authorization_valid", actions: [], stale: false,
      },
    ],
  };
  await reload();
  posts = calls.remoteActionPost.length;
  remoteActionResponse = { error: "permission_denied" };
  primaryButton().click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts + 1, "缺装证据下仍可重试动作");
  const installPrescription = errorRegion.children.map((node) => String(node.textContent || "")).join("\n");
  assert.ok(
    installPrescription.includes("请先安装 CourseLens App"),
    `缺装证据下处方指向安装 App：${installPrescription}`,
  );
  assert.equal(
    installPrescription.includes("重新授权"), false,
    "缺装证据下处方绝不引导重新授权",
  );
  remoteActionResponse = null;

  /* 4c) 遗留状态降级（复用路径 403 零证据）：bootstrap 返回 awaiting_installation，
     账号级安装页为第二受信形态——恰自动打开一次 + 引导块兜底链接；安装就绪后兜底失效 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_missing" },
    components: [
      {
        component: "installation", state: "action_required", code: "installation_missing",
        actions: ["bootstrap"], stale: false, evidence: {},
      },
    ],
  };
  await reload();
  assert.equal(primaryButton().textContent, "创建专属仓库并完成初始化", "降级披露前主按钮不变");
  const opensBeforeAccount = openCalls;
  remoteActionResponse = {
    operation: {
      state: "accepted",
      result: {
        setup_state: "awaiting_installation",
        installation_setup_url: "https://github.com/apps/fudan-courselens/installations/new",
      },
    },
  };
  posts = calls.remoteActionPost.length;
  primaryButton().click();
  await settle();
  assert.equal(calls.remoteActionPost[posts].action, "bootstrap", "降级由 bootstrap 动作结果披露");
  const accountAnchor = deepElements(document.getElementById("remote-evidence")).find(
    (node) => node.tagName === "A" && node.getAttribute("href") === "https://github.com/apps/fudan-courselens/installations/new",
  );
  assert.notEqual(accountAnchor, null, "无预选链接时引导块兜底账号级安装页链接");
  assert.ok(
    deepElements(document.getElementById("remote-evidence")).some(
      (node) => String(node.textContent || "").includes("暂无预选链接"),
    ),
    "兜底文案如实说明无预选链接",
  );
  assert.equal(openCalls, opensBeforeAccount + 1, "账号级安装页恰自动打开一次");
  assert.equal(openUrls.at(-1), "https://github.com/apps/fudan-courselens/installations/new", "自动打开的正是后端披露的账号级安装页");
  remoteActionResponse = null;
  /* 安装就绪：引导块消失，账号级兜底随之失效（不残留陈旧链接） */
  remoteSnapshot = {
    overall: { state: "ready", code: "installation_present" },
    components: [
      { component: "installation", state: "ready", code: "installation_present", actions: [], stale: false, evidence: {} },
    ],
  };
  await reload();
  assert.equal(
    deepElements(document.getElementById("remote-evidence")).some(
      (node) => node.dataset && node.dataset.role === "install-guidance",
    ),
    false, "安装就绪后引导块消失",
  );
  assert.equal(openCalls, opensBeforeAccount + 1, "安装就绪后无新增自动打开");
  /* 异步反馈收尾（单元②）：降级 bootstrap 成功进入反馈循环，排水后复位 */
  await drainAsyncFeedback();

  /* 5) 重试成功：错误区隐藏、恰好再发一次、无自动跳转 */
  remoteSnapshot = scopeSnapshot;
  remoteActionResponse = { error: "installation_scope_not_exact" };
  await reload();
  primaryButton().click();
  await settle();
  remoteActionResponse = null;
  posts = calls.remoteActionPost.length;
  const latestRetry = errorRegion.children.find((node) => node.tagName === "BUTTON");
  assert.notEqual(latestRetry, null, "失败后仍提供重试");
  latestRetry.click();
  await settle();
  assert.equal(calls.remoteActionPost.length, posts + 1, "重试恰好再发一次");
  assert.equal(calls.remoteActionPost[posts].action, "start-authorization", "重试复用原动作");
  assert.equal(calls.remoteActionPost[posts].force, false, "重试不携带 force");
  assert.equal(errorRegion.hidden, true, "重试成功后错误区隐藏");
  assert.equal(openCalls, opensBeforeAccount + 1, "恢复路径无新增自动打开（引导态两次：预选 + 账号级兜底）");

  /* 6) 设备码 pending：进度行可见，验证码以等宽大字块呈现且带一键复制；
     剪贴板不可用时复制失败可重试（验证码本身始终可选中手动复制）；
     轮询确认授权后隐藏（不再安排轮询） */
  remoteActionResponse = {
    operation: { state: "accepted", result: { state: "pending", user_code: "ABCD-1234", interval: 5 } },
  };
  const timersBefore = new Set(timeouts.keys());
  primaryButton().click();
  await settle();
  assert.equal(deviceRow.hidden, false, "pending 时设备码进度行可见");
  assert.equal(deviceRow.dataset.state, "checking", "进度行为确认态");
  const codeNode = deepElements(deviceRow).find((node) => String(node.className || "").split(/\s+/).includes("device-code-value"));
  assert.ok(codeNode, "验证码以等宽大字块呈现（显著、可选择）");
  assert.equal(codeNode.textContent, "ABCD-1234", "验证码文本完整展示");
  const copyButton = deepElements(deviceRow).find((node) => node.tagName === "BUTTON");
  assert.ok(copyButton, "提供一键复制动作");
  copyButton.click();
  await settle();
  assert.ok(String(copyButton.textContent).includes("复制失败"), "剪贴板不可用时复制失败提示可恢复（按钮保持可点）");
  copyButton.click();
  await settle();
  assert.ok(copyButton.disabled === false, "复制失败后仍可重试");
  const newTimerFns = [...timeouts.entries()]
    .filter(([key]) => !timersBefore.has(key))
    .map(([, fn]) => fn);
  assert.ok(newTimerFns.length >= 2, "pending 安排轮询与 toast 自清除定时器");
  remoteActionResponse = {
    operation: {
      state: "accepted",
      result: {
        state: "authorized", installed: false, installation_status: "missing",
        setup_state: "awaiting_installation",
        installation_setup_url: setupUrl,
      },
    },
  };
  newTimerFns[0](); /* 触发已安排的 poll-authorization */
  await settle();
  posts = calls.remoteActionPost.length;
  assert.equal(calls.remoteActionPost[posts - 2].action, "poll-authorization", "轮询请求发出");
  assert.equal(
    calls.remoteActionPost[posts - 1].action, "diagnose",
    "授权确认后自动对账恰一次（复用 diagnose 管道，先探测再刷新卡面）",
  );
  assert.equal(deviceRow.hidden, true, "授权确认后进度行隐藏（进度仅在有用时可见）");
  assert.equal(errorRegion.hidden, true, "授权成功后无错误残留");
  assert.equal(openCalls, opensBeforeAccount + 1, "授权确认路径无新增自动打开（同一 setup URL 已去重）");

  /* 7) poll 网络韧性：单次失败流程存活并按有界退避续排（自 max(interval,5s) 起步）；
     连续失败达上限后诚实报错 + 重试 CTA 且停排；设备流程已过期时瞬态失败不再续排 */
  const keysBefore = () => new Set(timeouts.keys());
  /* 触发自快照之后新建且尚未触发过的定时器；触发即消费（与真实一次性定时器一致），
     使 poll 失败 → 重试 的链式定时器可按轮次逐个推进 */
  const fireNewTimers = async (before) => {
    const entries = [...timeouts.entries()].filter(([key]) => !before.has(key));
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };
  const roundMaxDelay = (before) => Math.max(0, ...[...timeoutDelays.entries()]
    .filter(([key]) => !before.has(key))
    .map(([, ms]) => ms));
  const pollPostsCount = () => calls.remoteActionPost
    .filter((post) => post.action === "poll-authorization").length;
  const pollsBeforeResilience = pollPostsCount();

  /* 7a) 单次失败 → 续排存活 → 授权成功：状态与清单照常更新 */
  remoteSnapshot = authMissingSnapshot;
  await reload();
  remoteActionResponse = {
    operation: { state: "accepted", result: { state: "pending", user_code: "ABCD-1234", interval: 5 } },
  };
  const roundA = keysBefore();
  primaryButton().click(); /* start-authorization → pending，安排首个 poll */
  await settle();
  assert.equal(deviceRow.hidden, false, "7a pending 时进度行可见");
  remoteActionResponse = { error: "github_unreachable" };
  await fireNewTimers(roundA); /* 首个 poll 失败 #1 → 有界退避续排 */
  assert.equal(pollPostsCount(), pollsBeforeResilience + 1, "7a 首个 poll 请求恰好发出");
  assert.equal(errorRegion.hidden, true, "7a 单次瞬态失败不立即报错（在途授权未被杀死）");
  assert.equal(deviceRow.hidden, false, "7a 续排期间进度行仍可见");
  assert.equal(deviceRow.dataset.state, "checking", "7a 进度行保持确认态");
  assert.equal(roundMaxDelay(roundA), 5000, "7a 首个重试延迟自 max(interval,5s) 起步");
  remoteActionResponse = {
    operation: {
      state: "accepted",
      result: {
        state: "authorized", installed: false, installation_status: "missing",
        setup_state: "awaiting_installation",
        installation_setup_url: setupUrl,
      },
    },
  };
  await fireNewTimers(roundA); /* 重试 poll → 授权成功 */
  assert.equal(pollPostsCount(), pollsBeforeResilience + 2, "7a 重试 poll 请求恰好发出");
  assert.equal(deviceRow.hidden, true, "7a 授权确认后进度行隐藏");
  assert.equal(errorRegion.hidden, true, "7a 授权成功后无错误残留");

  /* 7b) 连续失败达上限：诚实报错 + 重试 CTA，且不再续排 */
  remoteActionResponse = {
    operation: { state: "accepted", result: { state: "pending", user_code: "ABCD-1234", interval: 5 } },
  };
  const roundB = keysBefore();
  primaryButton().click();
  await settle();
  remoteActionResponse = { error: "github_unreachable" };
  await fireNewTimers(roundB); /* poll #1 → 重试 #1（5s） */
  assert.equal(errorRegion.hidden, true, "7b 第 1 次失败仍续排");
  assert.equal(roundMaxDelay(roundB), 5000, "7b 重试 #1 延迟 5s");
  await fireNewTimers(roundB); /* poll #2 → 重试 #2（10s） */
  assert.equal(errorRegion.hidden, true, "7b 第 2 次失败仍续排");
  assert.equal(roundMaxDelay(roundB), 10000, "7b 重试 #2 延迟翻倍（10s）");
  await fireNewTimers(roundB); /* poll #3 → 重试 #3（20s，仍在上限内） */
  assert.equal(errorRegion.hidden, true, "7b 第 3 次失败仍续排（恰为上限）");
  assert.equal(roundMaxDelay(roundB), 20000, "7b 重试 #3 延迟 20s");
  await fireNewTimers(roundB); /* poll #4 → 超过连续失败上限 → 诚实报错停排 */
  assert.equal(
    pollPostsCount(), pollsBeforeResilience + 6,
    "7b 7a 的 2 次 + 本轮 4 次后停排",
  );
  assert.equal(errorRegion.hidden, false, "7b 超限后内联诚实报错");
  const capRetryButton = errorRegion.children.find((node) => node.tagName === "BUTTON");
  assert.notEqual(capRetryButton, null, "7b 超限后提供重试 CTA");
  assert.equal(capRetryButton.textContent, "重试");
  await fireNewTimers(roundB); /* 触发所有残留定时器（仅 toast 自清除） */
  assert.equal(pollPostsCount(), pollsBeforeResilience + 6, "7b 停排后不再发出任何 poll 请求");

  /* 7c) 设备流程已过期：瞬态失败不续排，直接诚实报错 */
  remoteActionResponse = {
    operation: { state: "accepted", result: { state: "pending", user_code: "ABCD-1234", interval: 5, expires_at: 1 } },
  };
  const roundC = keysBefore();
  primaryButton().click();
  await settle();
  remoteActionResponse = { error: "github_unreachable" };
  await fireNewTimers(roundC); /* 首个 poll 失败，但流程窗口已过 → 不续排 */
  assert.equal(pollPostsCount(), pollsBeforeResilience + 7, "7c 恰好再发一次 poll 请求");
  assert.equal(errorRegion.hidden, false, "7c 流程过期后瞬态失败直接诚实报错");
  await fireNewTimers(roundC);
  assert.equal(pollPostsCount(), pollsBeforeResilience + 7, "7c 过期后不再续排");

  windowTarget.open = undefined;
  remoteActionResponse = null;
  remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
  await reload();
}

/* S08-A：连接卡（自动连接开关/账号绑定/GitHub 四阶段）+ 设置导航焦点根因修复 */
/* ---- FLOW-ORDER-FIX-1 单元②：异步动作反馈（进行中态/有界轮询/终态恰一次通知）
   + 轮换 Worker 密钥次级按钮（environment 证据可见 + 一次确认弹层） ---- */

async function verifyAsyncActionFeedbackAndRotateKeys() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));
  /* 伪时钟：轮询死线用 Date.now 判定——测试内以 2s/拍推进，60 拍即超时 */
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  /* 触发全部 2s 一次性定时器（异步反馈轮询缝），并推进伪时钟一拍 */
  const tickPoll = async () => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 2000);
    fakeNow += 2000;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };

  try {
    /* 0) 无 environment 缺钥证据：轮换按钮隐藏 */
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
    assert.equal(byId["remote-rotate-row"].hidden, true, "无缺钥证据时轮换按钮隐藏");

    /* 0b) INIT-PATH-POLISH-1 单元C 条件矩阵：action_required + 动作在但缺钥
       清单为空 → 隐藏（缺钥非空是第二道门，绝不只看动作名） */
    remoteSnapshot = {
      overall: { state: "action_required", code: "environment_incomplete" },
      components: [
        {
          component: "environment", state: "action_required", code: "environment_incomplete",
          actions: ["rotate-worker-keys"], stale: false,
          evidence: { job_token_present: true, missing_secrets: [] },
        },
      ],
    };
    await reload();
    assert.equal(byId["remote-rotate-row"].hidden, true, "缺钥清单为空时轮换按钮隐藏");

    /* 1) GH-UX-REWORK-1（MF-7）：授权在案而初始化未完成 → 渲染期自动续跑
       bootstrap 恰一次（零点击）；按钮进行中态（disabled+aria-busy+进行中文案），
       2s 有界轮询；探针指纹变化（推进到加密测试）→ 恰一次推进通知，按钮恢复 */
    const setupComponent = (overrides = {}) => ({
      component: "worker_repository", state: "unknown", code: "worker_setup_incomplete",
      actions: ["bootstrap"], stale: false, ...overrides,
    });
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        setupComponent(),
        setupComponent({ component: "mailbox_repository" }),
      ],
      installation_url: "https://github.com/apps/fudan-courselens/installations/new",
    };
    await reload();
    const autoFireCount = calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
    assert.ok(autoFireCount >= 1, "MF-7：渲染期自动续跑 bootstrap（零点击）");
    assert.equal(calls.remoteActionPost.at(-1)?.action, "diagnose", "自动续跑保留恰一次自动对账");
    assert.equal(primaryButton().disabled, true, "自动续跑即进入进行中禁用态");
    assert.equal(primaryButton().getAttribute("aria-busy"), "true", "进行中态带 aria-busy");
    assert.ok(primaryButton().textContent.includes("进行中…"), `进行中文案：${primaryButton().textContent}`);
    await reload();
    assert.equal(
      calls.remoteActionPost.filter((item) => item.action === "bootstrap").length, autoFireCount,
      "自动续跑恰一次（重复渲染不重发）",
    );
    byId["toast-region"].replaceChildren(); /* 上限语义：锚点清零，断言只看本段 */
    let toastsBefore = 0;
    let getsBefore = calls.remoteGet;
    await tickPoll();
    assert.ok(calls.remoteGet > getsBefore, "轮询复用 GET remote-connection 读模型缝");
    assert.equal(primaryButton().disabled, true, "探针未变时保持进行中态");
    assert.equal(toastTexts().length, toastsBefore, "轮询未终态时不发通知");
    remoteSnapshot = {
      ...remoteSnapshot,
      overall: { state: "action_required", code: "channel_test_required" },
    };
    await tickPoll();
    /* GH-UX-REWORK-1（MF-6）：初始化收口即自动加密测试（零点击）——按钮进入
       test-channel 进行中态；自述恰一次（收口指引行与自动推进同义，省略） */
    assert.equal(calls.remoteActionPost.at(-1)?.action, "test-channel", "MF-6：自动加密测试恰一次发出");
    assert.equal(primaryButton().disabled, true, "自动加密测试进入进行中禁用态");
    const autoToasts = toastTexts().slice(toastsBefore);
    assert.equal(autoToasts.length, 1, `自动推进自述恰一次：${JSON.stringify(autoToasts)}`);
    assert.ok(autoToasts[0].includes("自动校验加密通道"), "自述=初始化已完成，正在自动校验加密通道");
    remoteSnapshot = { ...remoteSnapshot, overall: { state: "ready", code: "ready_for_dispatch" } };
    await tickPoll();
    await settle(); /* 收口自动加密测试全链落定，不污染下一幕的计数 */

    /* 2) 有界轮询超时（60 拍 ≈ 120s）：解除进行中态 + 「仍在进行，可手动诊断」 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        setupComponent(),
        setupComponent({ component: "mailbox_repository" }),
      ],
      installation_url: "https://github.com/apps/fudan-courselens/installations/new",
    };
    await reload();
    byId["toast-region"].replaceChildren();
    toastsBefore = 0;
    primaryButton().click();
    await settle();
    assert.equal(primaryButton().disabled, true, "超时场景前置：进行中态在场");
    for (let round = 0; round < 60; round += 1) {
      await tickPoll();
    }
    await settle();
    assert.equal(primaryButton().disabled, false, "超时后解除进行中态");
    const timeoutToasts = toastTexts().slice(toastsBefore);
    assert.equal(timeoutToasts.length, 1, `超时提示恰一次：${JSON.stringify(timeoutToasts)}`);
    assert.ok(timeoutToasts[0].includes("仍在进行"), "超时提示明示操作仍在进行、可手动诊断");

    /* 3) rotate 接线：environment action_required 且探针已算出 rotate-worker-keys
       → 高级操作区次级按钮；确认弹层一次；确认后进入进行中态并终态恰一次通知 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "environment_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
        {
          component: "environment", state: "action_required", code: "environment_incomplete",
          actions: ["rotate-worker-keys"], stale: false,
          evidence: {
            job_token_present: true,
            missing_secrets: ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
          },
        },
      ],
    };
    await reload();
    assert.equal(byId["remote-rotate-row"].hidden, false, "缺钥证据下轮换按钮可见");
    /* 单元③：缺钥证据以「缺什么」摘要行上屏；复制连接诊断以卡面同一快照为准 */
    const componentEvidence = deepElements(byId["remote-component-evidence"])
      .map((node) => String(node.textContent || "")).join("\n");
    assert.ok(
      componentEvidence.includes("缺少密钥：WORKER_INPUT_PRIVATE_KEY、WORKER_SIGNING_PRIVATE_KEY"),
      `environment 缺钥清单上屏：${componentEvidence}`,
    );
    const copiedDiagnostics = [];
    Object.defineProperty(globalThis, "navigator", {
      value: {
        onLine: true, sendBeacon: () => true,
        clipboard: { writeText: async (text) => { copiedDiagnostics.push(String(text)); } },
      },
      configurable: true,
    });
    byId["copy-diagnostics"].click();
    await settle();
    assert.equal(copiedDiagnostics.length, 1, "复制连接诊断恰一次写入剪贴板");
    const diagnosticPayload = JSON.parse(copiedDiagnostics[0]);
    assert.equal(
      diagnosticPayload.remote, "environment_incomplete",
      "remote 字段以卡面连接快照为准（同源，而非陈旧 settings 快照）",
    );
    assert.equal(diagnosticPayload.remote_state, "action_required", "remote_state 同源");
    const environmentEntry = (diagnosticPayload.components || []).find((item) => item.component === "environment");
    assert.deepEqual(
      environmentEntry?.missing_secrets,
      ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
      "缺钥清单以闭集值进入载荷",
    );
    assert.equal(
      JSON.stringify(diagnosticPayload).includes("settings_url"),
      false, "载荷不含 evidence 自由字段",
    );
    byId["remote-rotate-keys"].click();
    await settle();
    assert.equal(byId["remote-rotate-dialog"].open, true, "轮换先弹一次确认层");
    assert.ok(
      byId["remote-rotate-dialog-hint"].textContent.includes("将重新生成 Worker 加密密钥"),
      "确认层明示将重新生成 Worker 加密密钥",
    );
    byId["remote-rotate-cancel"].click();
    await settle();
    assert.equal(byId["remote-rotate-dialog"].open, false, "取消关闭弹层且不发请求");
    let postsAtRotate = calls.remoteActionPost.length;
    byId["remote-rotate-keys"].click();
    await settle();
    byId["remote-rotate-confirm"].click();
    await settle();
    assert.equal(calls.remoteActionPost.length, postsAtRotate + 1, "确认后恰发一次请求");
    assert.equal(calls.remoteActionPost[postsAtRotate].action, "rotate-worker-keys", "动作名正确");
    assert.equal(primaryButton().disabled, true, "轮换进行中主按钮进入进行中态");
    assert.ok(primaryButton().textContent.includes("轮换 Worker 密钥进行中…"), "进行中文案标注轮换");
    assert.equal(byId["remote-rotate-row"].hidden, true, "轮换进行中隐藏次级按钮");
    byId["toast-region"].replaceChildren();
    toastsBefore = 0;
    remoteSnapshot = {
      overall: { state: "ready", code: "ready_for_dispatch" },
      components: [
        { component: "environment", state: "ready", code: "environment_ready", actions: [], stale: false },
      ],
    };
    await tickPoll();
    assert.equal(byId["remote-primary-action"].hidden, true, "P58：轮换完成进入就绪态，主按钮收起");
    const rotateToasts = toastTexts().slice(toastsBefore);
    assert.equal(rotateToasts.length, 1, `轮换完成通知恰一次：${JSON.stringify(rotateToasts)}`);
    assert.ok(rotateToasts[0].includes("已轮换"), "完成通知标注密钥已轮换");
  } finally {
    Date.now = realNow;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
  }
}

/* ---- FRONTEND-SMOOTH-1 单元A：安装完成侦测（等待行/聚焦重探/自动推进恰一次） ---- */

async function verifyInstallCompletionDetection() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryAnchor = () => byId["remote-primary-action"].children.find((node) => node.tagName === "A");
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  const tickInstallWait = async () => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 3000);
    fakeNow += 3000;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };

  try {
    /* 前序场景点出过安装页锚（同模块状态延续）：先把遗留等待窗口按伪时钟
       推到过期并排水，保证本场景从干净基线开始 */
    fakeNow += 360000;
    await tickInstallWait();
    await settle();
    assert.equal(byId["remote-install-wait"].hidden, true, "遗留等待窗口过期后静默收口");

    /* 场景：未安装 + 两仓未建（首跑新序：装 App 前置）→ 主按钮=安装 App
       （账号级锚点）。安装页锚点点出后：等待行可见 + 3s 有界轮询排上 +
       不立即发任何远程动作请求 */
    const installMissingSnapshot = {
      overall: { state: "action_required", code: "installation_missing" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        {
          component: "installation", state: "action_required", code: "installation_missing",
          actions: ["bootstrap"], stale: false,
          evidence: { installation_setup_url: "https://github.com/apps/fudan-courselens/installations/new" },
        },
      ],
      installation_url: "https://github.com/apps/fudan-courselens/installations/new",
    };
    remoteSnapshot = installMissingSnapshot;
    remoteActionResponse = null;
    await reload();
    const anchor = primaryAnchor();
    assert.notEqual(anchor, null, "缺装+两仓未建：主按钮=安装 App 锚点");
    assert.equal(byId["remote-install-wait"].hidden, true, "未点出安装页前等待行隐藏");
    let getsBefore = calls.remoteGet;
    let postsBefore = calls.remoteActionPost.length;
    anchor.click();
    await settle();
    assert.equal(byId["remote-install-wait"].hidden, false, "点出安装页后进入等待安装态");
    assert.ok(
      byId["remote-install-wait"].textContent.includes("等待你在 GitHub 完成安装"),
      `等待行明示等待语义：${byId["remote-install-wait"].textContent}`,
    );
    assert.equal(calls.remoteGet, getsBefore, "进入等待态不立即发探测请求");
    assert.equal(calls.remoteActionPost.length, postsBefore, "进入等待态不发动作请求");
    assert.ok(
      [...timeoutDelays.values()].includes(3000),
      "3 秒有界轮询已排上",
    );

    /* INIT-PATH-POLISH-1 单元A：常规读不受新鲜语义影响（无参读模型不变） */
    let freshGetsBefore = calls.remoteGetFresh;
    getsBefore = calls.remoteGet;
    await reload();
    assert.ok(calls.remoteGet > getsBefore, "普通读仍走无参读模型");
    assert.equal(calls.remoteGetFresh, freshGetsBefore, "常规请求不携带 ?fresh=1");

    /* INIT-PATH-POLISH-1 单元A：等待轮询拍强制新鲜探针（?fresh=1）——学生
       装完 App 后下一拍即见新证据，不再恒读 60s 节拍/90s 时效的旧数据 */
    freshGetsBefore = calls.remoteGetFresh;
    await tickInstallWait();
    assert.equal(calls.remoteGetFresh, freshGetsBefore + 1, "等待轮询拍携带 ?fresh=1");

    /* 窗口聚焦：立即重探（不等 3s 轮询拍），且同样走新鲜语义 */
    getsBefore = calls.remoteGet;
    window.dispatchEvent(new Event("focus"));
    await settle();
    assert.ok(calls.remoteGet > getsBefore, "窗口聚焦立即重探 remote-connection");
    assert.ok(calls.remoteGetFresh > freshGetsBefore, "焦点重探同样携带 ?fresh=1");

    /* 轮询拍到已安装（任意范围）+ 两仓未建：等待行收口 + 自动推进主按钮到建仓恰一次 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
      ],
    };
    byId["toast-region"].replaceChildren(); /* 上限语义：锚点清零，断言只看本段 */
    const toastsBefore = 0;
    await tickInstallWait();
    /* INIT-PATH-POLISH-1 单元B：自动推进先显眼自述——状态行切自述文案
       （bootstrap 收口前常显），toast 同文恰一次，绝不静默开始 */
    assert.equal(byId["remote-install-wait"].hidden, false, "自动推进时状态行自述在场");
    assert.ok(
      byId["remote-install-wait"].textContent.includes("检测到 CourseLens App 已安装"),
      `自述行明示自动继续初始化：${byId["remote-install-wait"].textContent}`,
    );
    assert.equal(calls.remoteActionPost.at(-2)?.action, "bootstrap", "自动推进主按钮到建仓");
    assert.equal(calls.remoteActionPost.at(-1)?.action, "diagnose", "bootstrap 保留恰一次自动对账");
    const advanceToasts = toastTexts().slice(toastsBefore);
    assert.equal(advanceToasts.length, 1, `自动推进通知恰一次：${JSON.stringify(advanceToasts)}`);
    assert.ok(advanceToasts[0].includes("检测到 CourseLens App 已安装"), "推进通知为闭集自述文案");
    const bootstrapPosts = calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
    await tickInstallWait();
    await tickInstallWait();
    assert.equal(
      calls.remoteActionPost.filter((item) => item.action === "bootstrap").length,
      bootstrapPosts,
      "自动推进恰一次：后续轮询拍不重复提交",
    );

    /* 等待进行中主按钮已切到建仓（重渲染跟随探针证据）；
       先排水自动推进 bootstrap 的异步反馈轮询（伪时钟越过死线收口） */
    await drainAsyncFeedback();
    await reload();
    assert.equal(primaryButton()?.textContent, "创建专属仓库并完成初始化", "安装就绪后主按钮=建仓");
    assert.equal(byId["remote-install-wait"].hidden, true, "异步动作收口后自述行归档隐藏");
  } finally {
    Date.now = realNow;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
  }
}

/* ---- NIGHT2-W19 首跑全链合成冒烟：一条链从缺装走到完成总结归档 ----
   等待安装→安装侦测自动推进建仓→主按钮建仓→范围收紧等待→收紧侦测自动推进
   初始化→通道→全绿完成总结→确认归档。每一步的细粒度行为由前面场景分别
   钉死；本场景证明这些环节能按学生真实首跑次序无缝相接（今日八条发现皆
   集成级，此为链级防回归网）。 */
async function verifyFirstRunChain() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryAnchor = () => byId["remote-primary-action"].children.find((node) => node.tagName === "A");
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  const tick3s = async () => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 3000);
    fakeNow += 3000;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };
  const summaryBlocks = () => deepElements(byId["remote-evidence"]).filter(
    (node) => String(node.dataset?.role || "") === "completion-summary",
  );
  const bootstrapCount = () => calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
  try {
    /* 清场：遗留 3s 窗口按伪时钟推到过期 */
    fakeNow += 360000;
    await tick3s();
    await settle();

    /* 第 1 棒：缺装 → 安装锚点 → 等待安装态（零动作请求） */
    remoteSnapshot = {
      overall: { state: "action_required", code: "installation_missing" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "action_required", code: "installation_missing", actions: ["bootstrap"], stale: false,
          evidence: { installation_setup_url: "https://github.com/apps/fudan-courselens/installations/new" } },
      ],
      installation_url: "https://github.com/apps/fudan-courselens/installations/new",
    };
    remoteActionResponse = null;
    await reload();
    assert.notEqual(primaryAnchor(), null, "链·第1棒：主按钮=安装 App 锚点");
    primaryAnchor().click();
    await settle();
    assert.equal(byId["remote-install-wait"].hidden, false, "链·第1棒：进入等待安装态");

    /* 第 2 棒：轮询拍到已安装+两仓未建 → 自述+自动推进建仓恰一次 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
      ],
    };
    await tick3s();
    assert.equal(calls.remoteActionPost.at(-2)?.action, "bootstrap", "链·第2棒：侦测到安装自动推进建仓");
    assert.equal(calls.remoteActionPost.at(-1)?.action, "diagnose", "链·第2棒：建仓保留恰一次自动对账");
    const afterAuto = bootstrapCount();
    await tick3s();
    await tick3s();
    assert.equal(bootstrapCount(), afterAuto, "链·第2棒：自动推进恰一次");
    await drainAsyncFeedback();
    await reload();
    assert.equal(primaryButton()?.textContent, "创建专属仓库并完成初始化", "链·第2棒：主按钮推进到建仓");

    /* 第 3 棒：学生点建仓 → 异步收口 → 快照翻到范围暂宽（两仓已建）→ 收紧引导 */
    primaryButton().click();
    await settle();
    await drainAsyncFeedback();
    remoteSnapshot = {
      overall: { state: "action_required", code: "installation_scope_not_exact" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "action_required", code: "installation_scope_not_exact", actions: ["restrict-github-app-installation"], stale: false,
          evidence: { settings_url: "https://github.com/settings/installations/42" } },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      ],
    };
    await reload();
    assert.notEqual(primaryAnchor(), null, "链·第3棒：范围暂宽主按钮=安装设置链接");
    primaryAnchor().click();
    await settle();
    assert.equal(byId["remote-tighten-wait"].hidden, false, "链·第3棒：进入等待收紧态");

    /* 第 4 棒：收紧拍读到范围已精确（两仓需重建）→ 自动推进初始化恰一次 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
      ],
    };
    await tick3s();
    assert.equal(calls.remoteActionPost.at(-2)?.action, "bootstrap", "链·第4棒：收紧侦测自动推进初始化");
    const afterTighten = bootstrapCount();
    await tick3s();
    assert.equal(bootstrapCount(), afterTighten, "链·第4棒：推进恰一次");
    await drainAsyncFeedback();
    await reload();
    assert.equal(byId["remote-tighten-wait"].hidden, true, "链·第4棒：自述行归档");

    /* 第 5 棒：通道末段 → 全绿沿完成总结登场 → 确认归档 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "channel_test_required" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
        { component: "channel_test", state: "action_required", code: "channel_test_required", stale: false },
      ],
    };
    await reload();
    assert.equal(summaryBlocks().length, 0, "链·第5棒：未达成时无总结块");
    remoteSnapshot = {
      overall: { state: "ready", code: "ready_for_dispatch" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
        { component: "channel_test", state: "ready", code: "channel_test_valid", stale: false },
      ],
    };
    await reload();
    assert.equal(summaryBlocks().length, 1, "链·第5棒：全绿沿完成总结登场");
    deepElements(summaryBlocks()[0]).find((node) => node.tagName === "BUTTON")?.click();
    await settle();
    assert.equal(summaryBlocks().length, 0, "链·第5棒：确认后归档，干净交给常规连接卡");
  } finally {
    Date.now = realNow;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
  }
}

/* ---- FRONTEND-SMOOTH-1 单元B：阶段机单源化 + 「仓库已建」门状态矩阵 ---- */

async function verifyStageMachineSingleSourcing() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const primaryAnchor = () => byId["remote-primary-action"].children.find((node) => node.tagName === "A");
  const regionText = (id) => document.getElementById(id).children
    .map((node) => String(node.textContent || "")).join("\n");
  const phaseState = (key) => byId["github-phases"].children
    .find((item) => item.dataset.phase === key)?.dataset.phaseState;
  const baseComponents = (overrides = []) => [
    { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
    { component: "installation", state: "ready", code: "installation_present", stale: false },
    { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
    { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
    ...overrides,
  ];

  /* 矩阵①「已安装+未建仓」（此前缺失组合）：主按钮=建仓、绝不出现收紧引导；
     阶段行同源——安装=已完成、专属仓库=需要操作；标题保持「GitHub 连接」 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "worker_setup_incomplete" },
    components: baseComponents(),
  };
  await reload();
  assert.equal(primaryButton()?.textContent, "创建专属仓库并完成初始化", "已安装+未建仓：主按钮=建仓");
  assert.equal(primaryAnchor(), undefined, "已安装+未建仓：无安装锚点");
  assert.ok(
    !regionText("remote-evidence").includes("调整安装范围"),
    "已安装+未建仓：收紧引导不登场（仓库已建门）",
  );
  assert.equal(phaseState("installation"), "done", "阶段行同源：安装=已完成");
  assert.equal(phaseState("repositories"), "action_required", "阶段行同源：专属仓库=需要操作");
  assert.equal(byId["github-connection-title"].textContent, "GitHub 连接", "非通道末段标题=GitHub 连接");

  /* 矩阵②通道为唯一剩余动作：标题=「加密通道测试」，与主按钮（加密测试）同拍 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "channel_test_required" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "installation", state: "ready", code: "installation_present", stale: false },
      { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
      { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      { component: "channel_test", state: "action_required", code: "channel_test_required", stale: false },
    ],
  };
  await reload();
  assert.equal(byId["github-connection-title"].textContent, "加密通道测试", "通道为唯一剩余动作：标题=加密通道测试");
  assert.equal(primaryButton()?.textContent, "加密测试", "通道为唯一剩余动作：主按钮=加密测试");
  assert.equal(phaseState("channel"), "action_required", "阶段行同源：加密通道=需要操作");

  /* 矩阵③已安装（范围暂宽）+ 两仓已建：收紧引导登场 + 主按钮带安装设置链接；
     单元E：指引为「最小权限+具名清单」框架——两仓名自受管记录闭集插值上屏 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_scope_not_exact" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      {
        component: "installation", state: "action_required", code: "installation_scope_not_exact",
        actions: ["restrict-github-app-installation"], stale: false,
        evidence: { settings_url: "https://github.com/settings/installations/42" },
      },
      { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
      { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
    ],
    configured_repositories: {
      worker: "student/Fudan-CourseLens-Worker",
      mailbox: "student/Fudan-CourseLens-Mailbox",
    },
  };
  await reload();
  assert.equal(primaryButton()?.textContent, "调整安装范围后继续初始化", "两仓已建+范围暂宽：主按钮=调整后初始化");
  const scopeAnchor = primaryAnchor();
  assert.notEqual(scopeAnchor, undefined, "两仓已建+范围暂宽：主行携带安装设置链接");
  assert.equal(scopeAnchor.getAttribute("href"), "https://github.com/settings/installations/42", "链接为后端受信设置页");
  assert.equal(phaseState("installation"), "action_required", "阶段行同源：安装范围需要操作");
  const scopeGuidanceText = regionText("remote-evidence");
  assert.ok(scopeGuidanceText.includes("遵循最小权限"), "收紧指引为最小权限框架");
  assert.ok(scopeGuidanceText.includes("student/Fudan-CourseLens-Worker"), "具名清单：Worker 仓库名上屏");
  assert.ok(scopeGuidanceText.includes("student/Fudan-CourseLens-Mailbox"), "具名清单：Mailbox 仓库名上屏");
  assert.equal(scopeGuidanceText.includes("不精确"), false, "收紧指引不再使用「不精确」技术措辞");

  /* 矩阵③b：受管记录缺失时退回无名单变体（绝不本地发明仓库名） */
  remoteSnapshot = { ...remoteSnapshot, configured_repositories: {} };
  await reload();
  const anonymousGuidance = regionText("remote-evidence");
  assert.ok(anonymousGuidance.includes("遵循最小权限"), "无名单变体仍为最小权限框架");
  assert.equal(anonymousGuidance.includes("student/"), false, "无受管记录时不出现仓库名");

  /* 矩阵④全绿就绪：标题回归「GitHub 连接」基线 */
  remoteSnapshot = {
    overall: { state: "ready", code: "ready_for_dispatch" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "installation", state: "ready", code: "installation_present", stale: false },
      { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
      { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      { component: "channel_test", state: "ready", code: "channel_test_valid", stale: false },
    ],
  };
  await reload();
  assert.equal(byId["github-connection-title"].textContent, "GitHub 连接", "全绿就绪标题=GitHub 连接");
}

/* ---- FRONTEND-SMOOTH-1 单元C：动作期快照不塌（空响应不覆盖好卡面 + 角标） ---- */

async function verifySnapshotSurvivesTransientFailure() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  const tickPoll = async () => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 2000);
    fakeNow += 2000;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };
  const baseFetch = globalThis.fetch;

  try {
    /* 好快照在卡 + bootstrap 提交：进行中态与角标同时在场 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
      ],
    };
    remoteActionResponse = null;
    await reload();
    assert.equal(byId["remote-action-progress"].hidden, true, "无动作时角标隐藏");
    primaryButton().click();
    await settle();
    assert.equal(primaryButton().disabled, true, "进行中态在场");
    assert.equal(byId["remote-action-progress"].hidden, false, "动作进行中阶段行可见");
    assert.ok(
      byId["remote-action-progress"].textContent.includes("正在确认连接状态"),
      `阶段行首标签补位：${byId["remote-action-progress"].textContent}`,
    );

    /* 动作轮询期间后端瞬态失败：好卡面原样保留（绝不塌成未知摘要） */
    globalThis.fetch = async (path, options) => {
      if (String(path) === "/api/v3/remote-connection") {
        return new Response(
          JSON.stringify({ error: "runtime failed", error_code: "runtime_failed" }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        );
      }
      return baseFetch(path, options);
    };
    byId["toast-region"].replaceChildren(); /* 上限语义：锚点清零，断言只看本段 */
    const toastsBefore = 0;
    await tickPoll();
    assert.equal(primaryButton().disabled, true, "空响应不塌卡：进行中态原样保留");
    assert.ok(primaryButton().textContent.includes("进行中…"), "空响应后仍渲染进行中文案");
    assert.equal(toastTexts().length, toastsBefore, "瞬态失败不弹通知");

    /* 快照恢复且指纹变化：恰一次终态通知，角标随终态收口 */
    globalThis.fetch = baseFetch;
    remoteSnapshot = {
      overall: { state: "action_required", code: "channel_test_required" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      ],
    };
    await tickPoll();
    const settledToasts = toastTexts().slice(toastsBefore);
    assert.equal(settledToasts.length, 1, `终态通知恰一次：${JSON.stringify(settledToasts)}`);
    assert.equal(byId["remote-action-progress"].hidden, true, "终态后角标收口");
    assert.equal(primaryButton().disabled, false, "终态后按钮恢复");
  } finally {
    Date.now = realNow;
    globalThis.fetch = baseFetch;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await drainAsyncFeedback();
    await reload();
  }
}

/* ---- FRONTEND-SMOOTH-1 单元D：分步进度渲染 + 中途错误浮现纪律（自愈恰一次） ---- */

async function verifyActionProgressAndSelfHeal() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryButton = () => byId["remote-primary-action"].children.find((node) => node.tagName === "BUTTON");
  const toastStates = () => byId["toast-region"].children.map((node) => String(node.dataset.state || ""));
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));
  const setupSnapshot = {
    overall: { state: "action_required", code: "worker_setup_incomplete" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "installation", state: "ready", code: "installation_present", stale: false },
      { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
      { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
    ],
  };
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  const tickAt = async (ms) => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === ms);
    fakeNow += ms;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };

  try {
    /* 1) 后端闭集阶段字段驱动进度行：匹配当前动作 → 「阶段 · 预期句」
       （WAIT-UX-1：预期句按动作实测分布分档，bootstrap=R4 实测 20.5s→半分钟到两分钟） */
    remoteSnapshot = setupSnapshot;
    remoteActionResponse = null;
    await reload();
    primaryButton().click();
    await settle();
    assert.equal(byId["remote-action-progress"].hidden, false, "进行中阶段行在场");
    /* INIT-PATH-POLISH-1 单元B：点击空窗期以闭集首标签补位，预期句全程在场 */
    assert.equal(
      byId["remote-action-progress"].textContent,
      "正在确认连接状态 · 约需半分钟到两分钟，请稍候",
      `阶段字段到达前首标签补位：${byId["remote-action-progress"].textContent}`,
    );
    remoteSnapshot = {
      ...setupSnapshot,
      action_progress: { action: "bootstrap", stage: "creating_repositories", label: "正在创建仓库", updated_at: 1 },
    };
    await tickAt(2000);
    assert.equal(
      byId["remote-action-progress"].textContent,
      "正在创建仓库 · 约需半分钟到两分钟，请稍候",
      `分步进度文案：${byId["remote-action-progress"].textContent}`,
    );

    /* 2) 白名单外标签：回退闭集首标签，绝不渲染自由文本（角标文案已退役） */
    remoteSnapshot = {
      ...setupSnapshot,
      action_progress: { action: "bootstrap", stage: "free_form", label: "RAW backend stage text", updated_at: 2 },
    };
    await tickAt(2000);
    assert.equal(
      byId["remote-action-progress"].textContent,
      "正在确认连接状态 · 约需半分钟到两分钟，请稍候",
      "白名单外标签回退首标签补位",
    );

    /* 2b) WAIT-UX-1：预期句按动作分档——加密测试段用其实测分布（9s~60s），
       不再与 bootstrap 共用同一句（置于 bootstrap 动作终局收口之后） */

    /* 3) 终局收口 */
    remoteSnapshot = { overall: { state: "ready", code: "ready_for_dispatch" }, components: [] };
    await tickAt(2000);
    await reload();
    assert.equal(byId["remote-action-progress"].hidden, true, "终态后进度行收口");

    /* 3b) WAIT-UX-1：预期句按动作分档——加密测试段用其实测分布（9s~60s），
       不再与 bootstrap 共用同一句 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "channel_test_required" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      ],
    };
    remoteActionResponse = null;
    await reload();
    primaryButton().click();
    await settle();
    assert.equal(byId["remote-action-progress"].hidden, false, "加密测试进行中阶段行在场");
    assert.equal(
      byId["remote-action-progress"].textContent,
      "正在确认连接状态 · 约需十秒到一分钟，慢网可能更久，请稍候",
      `加密测试段按实测分布给预期：${byId["remote-action-progress"].textContent}`,
    );
    /* 收掉加密测试在途态，给后续自愈段干净起点 */
    remoteSnapshot = { overall: { state: "ready", code: "ready_for_dispatch" }, components: [] };
    await tickAt(2000);
    await reload();

    /* 4) 可自愈中间失败（operation failed + rate_limited）：不弹错误窗，
       静默重试恰一次，重试成功后无任何错误形态通知（绝不「弹错→又成功」） */
    remoteSnapshot = setupSnapshot;
    await reload();
    remoteActionResponse = { operation: { state: "failed", error_code: "rate_limited" } };
    byId["toast-region"].replaceChildren(); /* 上限语义：锚点清零，断言只看本段 */
    let toastsBefore = 0;
    primaryButton().click();
    await settle();
    assert.equal(byId["remote-action-error"].hidden, true, "自愈期不进内联错误区");
    assert.deepEqual(
      toastStates().slice(toastsBefore),
      ["checking"],
      "自愈期仅非错误形态提示",
    );
    assert.ok(
      [...timeoutDelays.values()].includes(4000),
      "自愈重试定时器（4s）已排上",
    );
    let bootstrapPosts = calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
    remoteActionResponse = null;
    await tickAt(4000);
    assert.equal(
      calls.remoteActionPost.filter((item) => item.action === "bootstrap").length,
      bootstrapPosts + 1,
      "自愈重试恰一次",
    );
    await drainAsyncFeedback();
    const healedToasts = toastTexts().slice(toastsBefore);
    assert.equal(
      healedToasts.filter((_, index) => toastStates().slice(toastsBefore)[index] === "error").length,
      0,
      `自愈成功路径零错误通知：${JSON.stringify(healedToasts)}`,
    );
    assert.equal(byId["remote-action-error"].hidden, true, "自愈成功后内联错误区仍隐藏");

    /* 5) 重试后再失败：走既有失败呈现（错误 toast + 内联区），恰一次 */
    remoteActionResponse = { operation: { state: "failed", error_code: "rate_limited" } };
    await reload();
    byId["toast-region"].replaceChildren();
    toastsBefore = 0;
    primaryButton().click();
    await settle();
    await tickAt(4000); /* 重试仍失败（响应保持 failed） */
    await settle();
    remoteActionResponse = null;
    const exhaustedToasts = toastStates().slice(toastsBefore);
    assert.deepEqual(
      exhaustedToasts,
      ["checking", "error"],
      `重试耗尽后按既有失败呈现恰一次：${JSON.stringify(exhaustedToasts)}`,
    );
    assert.equal(byId["remote-action-error"].hidden, false, "耗尽后内联错误区登场");
    const exhaustedErrorText = byId["remote-action-error"].children
      .map((node) => String(node.textContent || "")).join("\n");
    assert.ok(
      exhaustedErrorText.includes("rate_limited"),
      "透码 chip 保留可诊断性",
    );
    await drainAsyncFeedback();

    /* 6) 提交被拒分支（HTTP 400 + timeout 码）：同族自愈，静默重试恰一次 */
    remoteActionResponse = { error: "timeout" };
    await reload();
    byId["toast-region"].replaceChildren();
    toastsBefore = 0;
    primaryButton().click();
    await settle();
    assert.deepEqual(toastStates().slice(toastsBefore), ["checking"], "提交被拒自愈仅非错误提示");
    remoteActionResponse = null;
    bootstrapPosts = calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
    await tickAt(4000);
    assert.equal(
      calls.remoteActionPost.filter((item) => item.action === "bootstrap").length,
      bootstrapPosts + 1,
      "提交被拒自愈重试恰一次",
    );
    await drainAsyncFeedback();
  } finally {
    Date.now = realNow;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
  }
}

async function verifyRemoteComputeStatus() {
  /* CLOUD-CONSENT-AUTO-1 U2：云端处理开关退役——控制面只剩只读状态胶囊
     （三态与入队门同源：configured/verified）与一句透明说明；零点击动作。 */
  const { readFileSync } = await import("node:fs");
  const html = readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8");
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const row = byId["remote-compute-row"];
  const pill = byId["remote-compute-state"];
  const hint = byId["remote-compute-hint"];
  const base = { overall: { state: "ready", code: "remote_verified" }, components: [] };
  try {
    /* 0) 开关本体与动作入口整对下线：HTML 无 toggle 节点、无两击确认文案 */
    assert.ok(!html.includes("remote-compute-toggle"), "开关节点从 index.html 退役");
    assert.ok(!html.includes("确认关闭云端处理"), "关闭确认态从 index.html 退役");
    assert.ok(!html.includes("可随时关闭云端处理"), "「可随时关闭」措辞从隐私说明退役");

    /* 1) 快照无 remote_compute 字段：整行不渲染（omit-not-fabricate） */
    remoteSnapshot = { ...base };
    await reload();
    assert.equal(row.hidden, true, "无 remote_compute 字段：整行不渲染");

    /* 2) 连接就绪：状态=就绪 + 透明说明四项语义在场；零点击、零动作 */
    remoteSnapshot = { ...base, remote_compute: { configured: true, verified: true } };
    await reload();
    assert.equal(row.hidden, false, "有字段才渲染状态行");
    assert.equal(pill.textContent, "就绪", "configured+verified=就绪");
    assert.equal(pill.dataset.state, "ready", "就绪复用 data-state 色彩系统");
    assert.ok(hint.textContent.includes("加密代算"), "说明讲清数据处理方式");
    assert.ok(hint.textContent.includes("你的专属云端仓库"), "说明讲清可见范围");
    assert.ok(hint.textContent.includes("30 天"), "说明讲清云端保留期");
    const posts = calls.remoteActionPost.length;
    row.click();
    await settle();
    assert.equal(calls.remoteActionPost.length, posts, "状态行零点击动作：不再有开关请求");

    /* 3) 连接已建立、通道未验证：验证中（且如实说明期间已可派发） */
    remoteSnapshot = { ...base, remote_compute: { configured: true, verified: false } };
    await reload();
    assert.equal(pill.textContent, "验证中", "configured 未 verified=验证中");
    assert.ok(hint.textContent.includes("可以派发"), "确认期间不谎称不能派发");

    /* 4) 未配置：未连接 + 指路连接步骤 */
    remoteSnapshot = { ...base, remote_compute: { configured: false, verified: false } };
    await reload();
    assert.equal(pill.textContent, "未连接", "未配置=未连接");
    assert.ok(hint.textContent.includes("完成上方 GitHub 连接"), "指路连接步骤");

    /* 5) 撤销授权仍在（隐私北极星：撤销权不删）；自动学习材料规则开关未被连带 */
    assert.ok(byId["cloud-revoke-row"], "撤销云端授权行保留在连接卡高级操作区");
    assert.ok(byId["cloud-revoke-credentials"], "撤销动作仍可达");
    assert.ok(byId["course-automation-ack"], "课程目录披露对话框（按课程 opt-in）未被连带");
    assert.ok(byId["automation-run-now"], "自动学习材料管理卡动作未被连带");
  } finally {
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    await reload();
  }
}

async function verifyCloudConnHeaderState() {
  /* CLOUD-CONSENT-AUTO-1 U3b（用户拍板）：页眉/连接菜单的「云自动化」段整体
     退役——它与「手动生成字幕确实自动跑」命名撞车，现场实测学生照它读成
     「云端处理没开」。任何自动化状态下页眉都不再出现云段；学习页的课程级
     自动处理控制面不在本段范围（由 workspace mjs 继续钉）。 */
  const { readFileSync } = await import("node:fs");
  const html = readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8");
  const shellSource = readFileSync(new URL("../frontend/modules/shell.js", import.meta.url), "utf8");
  const label = () => String(byId["conn-live"].textContent || "");
  try {
    assert.equal(html.includes('data-conn-dot="cloud"'), false, "页眉第三颗点从 index.html 退役");
    assert.equal(html.includes("data-cloud-conn-row"), false, "连接菜单云行从 index.html 退役");
    assert.equal(html.includes("<b>云自动化</b>"), false, "「云自动化」条目文案整段退役");
    assert.equal(shellSource.includes("CONN_TEXT.cloud"), false, "页眉云段文案表退役");
    assert.equal(
      shellSource.includes('store.subscribe("automation"'), false,
      "页眉不再订阅 automation 快照（云端状态单一呈现面在设置页）",
    );
    for (const state of ["configuring", "ready", "degraded", "disabled", "running", "verifying"]) {
      store.set("automation", { state, enabled: true });
      await settle();
      assert.equal(label().includes("云自动化"), false, `${state}：页眉恒不出现云段`);
    }
    assert.ok(!label().includes("云自动化"), "任何自动化状态下页眉都不出现云段");
  } finally {
    store.set("automation", null);
    await settle();
  }
}

async function verifyToastStackCap() {
  /* 夜2审计 V8-P3 处方：toast region 至多 3 枚，超出淘汰最旧（新枚永在末位）。 */
  const { toast } = await import("../frontend/modules/ui.js");
  const region = byId["toast-region"];
  region.replaceChildren();
  try {
    toast("第一条", "ready");
    toast("第二条", "ready");
    toast("第三条", "ready");
    toast("第四条", "ready");
    assert.equal(region.children.length, 3, "连点 4 枚 → region 恒持 3 枚");
    assert.equal(String(region.children.at(-1)?.textContent || ""), "第四条", "最新一枚保留在末位");
    assert.equal(String(region.children[0]?.textContent || ""), "第二条", "最旧一枚被淘汰");
  } finally {
    region.replaceChildren();
  }
}

/* ---- F2（化身走查 20261008）：无字幕讲次「加入书签」失败=中性书签话术 ---- */
/* 此前借道通用码表弹提问链责备文案（「再重新提问」）且 toast 全局跨页存活，
   跳转直播页时呈现为「落页即弹的无来由报错」。就地按码给人话：指路先生成
   字幕，说清是书签动作，不借提问语义。 */
async function verifyBookmarkEvidenceUnavailableToast() {
  bookmarkPostBehavior = "evidence_unavailable";
  /* 段桩按 sub_id=l-w2c-labels 注入（verifyTranscriptAriaContextLabels 同一注入 key） */
  syntheticTranscriptSegments = [
    { start_ms: 65000, end_ms: 69000, text: "书签失败文案场景" },
  ];
  store.set("activeLecture", { course_id: "c-w2c", sub_id: "l-w2c-labels", sub_title: "书签失败讲次", can_stream: true });
  await settle();
  const row = byId["transcript-list"].querySelector(".transcript-row");
  assert.notEqual(row, null, "字幕行在场");
  const bookmarkButton = row.querySelector(".icon-button");
  assert.notEqual(bookmarkButton, null, "书签钮在场");
  const region = byId["toast-region"];
  region.replaceChildren();
  try {
    bookmarkButton.click();
    await settle();
    assert.equal(calls.bookmarksPost, 1, "书签请求恰一次");
    const toastText = String(region.children.at(-1)?.textContent || "");
    assert.ok(toastText.includes("书签"), "文案说清是书签动作（实际：" + toastText + "）");
    assert.ok(toastText.includes("字幕"), "文案指路先生成字幕");
    assert.ok(!toastText.includes("提问"), "不借提问链语义（学生此刻没在提问）");
  } finally {
    region.replaceChildren();
    bookmarkPostBehavior = "";
    syntheticTranscriptSegments = [];
    store.set("activeLecture", null);
    await settle();
  }
}

/* ---- F7（化身走查 20261008）：隐私节本地统计证据行=具名状态+adjacent 动作 ---- */
/* 此前「本地统计：需要完成操作」无处可点；现：未开启=具名行+行内「开启本地
   学习统计」，点击走 analytics/actions enable（幂等），成功后行收敛「已开启」
   且按钮退场；开启与否都不再出现泛化「需要完成操作」。 */
async function verifyPrivacyAnalyticsAction() {
  selectPage("settings");
  await settle();
  const privacy = byId["privacy-evidence"];
  const lineText = () => String(privacy.children.map((node) => String(node.textContent || "")).join(" "));
  assert.match(lineText(), /本地统计：未开启（不影响使用）/, "未开启=具名状态而非泛化兜底");
  assert.doesNotMatch(lineText(), /需要完成操作/, "不再出现无解释泛化文案");
  const enableButton = privacy.querySelectorAll("button").find((node) => node.textContent === "开启本地学习统计");
  assert.notEqual(enableButton, undefined, "行内 adjacent 动作在场");

  enableButton.click();
  await settle();
  assert.equal(analyticsActionPost.length, 1, "开启动作恰一次请求");
  assert.equal(analyticsActionPost[0].action, "enable", "走既有 enable 通道");
  assert.match(String(analyticsActionPost[0].operation_id || ""), /^analytics:/, "operation_id 幂等键");
  assert.match(lineText(), /本地统计：已开启/, "成功后行收敛已开启");
  assert.equal(privacy.querySelectorAll("button").length, 0, "开启后行内按钮退场（无开关面）");

  /* 失败路径：错误诚实呈现且按钮复位可重试 */
  settingsSnapshot.analytics = { state: "action_required", code: "analytics_disabled", actions: ["enable-analytics"], enabled: false };
  analyticsActionError = "runtime_failed";
  await settle();
  selectPage("study");
  await settle();
  selectPage("settings");
  await settle();
  const retryButton = privacy.querySelectorAll("button").find((node) => node.textContent === "开启本地学习统计");
  assert.notEqual(retryButton, undefined, "失败后重进设置页动作恢复在场");
  const region = byId["toast-region"];
  region.replaceChildren();
  try {
    retryButton.click();
    await settle();
    const toastText = String(region.children.at(-1)?.textContent || "");
    assert.match(toastText, /重试|稍后|没有成功/, "失败诚实 toast（实际：" + toastText + "）");
    assert.equal(retryButton.disabled, false, "失败后按钮复位可重试");
  } finally {
    region.replaceChildren();
    analyticsActionError = "";
    analyticsActionPost = [];
  }
}

async function verifyCloudRevokePrivacyRow() {
  /* NIGHT4-B 撤销云端授权迁隐私区：可见性=快照 actions 闭集；两击确认；
     武装阶段不发请求。 */
  const row = byId["cloud-revoke-row"];
  const button = byId["cloud-revoke-credentials"];
  try {
    store.set("automation", { state: "ready", enabled: true, actions: ["run-now", "revoke-cloud-credentials"] });
    await settle();
    assert.equal(row.hidden, false, "快照给出撤销动作 → 隐私区行可见");

    let posts = calls.automationActionPost.length;
    button.click();
    await settle();
    assert.equal(button.textContent, "确认撤销云端授权？", "首次点击只武装确认");
    assert.equal(calls.automationActionPost.length, posts, "武装阶段不发请求");

    button.click();
    await settle();
    const post = calls.automationActionPost[posts];
    assert.ok(post, "两击确认后发出撤销动作");
    assert.equal(post.action, "revoke-cloud-credentials", "动作键为闭集 revoke-cloud-credentials");
    assert.match(String(post.operation_id || ""), /^cloud-revoke:/, "幂等 operation_id 前缀闭集");

    store.set("automation", { state: "disabled", enabled: false, actions: [] });
    await settle();
    assert.equal(row.hidden, true, "快照无撤销动作 → 行隐藏");
  } finally {
    store.set("automation", null);
    await settle();
  }
}

async function verifyConnectionCardsAutoConnectAndFocus() {
  const fudanSwitch = byId["fudan-auto-connect"];
  const githubSwitch = byId["github-auto-connect"];
  const accountSelect = byId["fudan-auto-connect-account"];
  const fudanStatus = byId["fudan-auto-connect-status"];
  const githubStatus = byId["github-auto-connect-status"];
  const reload = async () => {
    selectPage("settings");
    await settle();
  };

  /* 0) 快照缺失 auto_connect：开关禁用、状态诚实为待确认，不渲染蛇形组件行 */
  settingsSnapshot = { ...settingsSnapshot, auto_connect: null };
  await reload();
  assert.equal(fudanSwitch.disabled, true, "偏好不可用时自动登录开关禁用");
  assert.equal(githubSwitch.disabled, true, "偏好不可用时自动连接开关禁用");
  assert.ok(fudanStatus.textContent.includes("待确认"), "偏好不可用时状态为待确认");

  /* 1) 偏好快照：开关态、账号绑定、上次启动结果一行证据；账号下拉含需轮换账号但禁用 */
  accountsSnapshot = {
    accounts: [
      { student_id: "20301080001", requires_rotation: false },
      { student_id: "20301080002", requires_rotation: true },
    ],
    deepseek: { configured: false, saved: false },
  };
  settingsSnapshot = {
    ...settingsSnapshot,
    auto_connect: {
      schema: "courselens.auto-connect.v1",
      fudan: { enabled: true, account_id: "20301080001", status: "ready" },
      github: { enabled: false, status: "off" },
      last_resume: {
        observed_at: 1,
        fudan: { state: "started", code: "fudan_resume_started" },
        github: { state: "", code: "" },
      },
    },
  };
  await reload();
  assert.equal(fudanSwitch.checked, true, "偏好启用时开关反映真值");
  assert.equal(fudanSwitch.disabled, false, "偏好可用时开关可操作");
  assert.equal(accountSelect.value, "20301080001", "开关绑定所选账号");
  const rotationOption = accountSelect.children.find((node) => node.value === "20301080002");
  assert.notEqual(rotationOption, undefined, "需轮换账号出现在下拉中");
  assert.equal(rotationOption.disabled, true, "需轮换账号不可选");
  assert.ok(fudanStatus.textContent.includes("本次启动已尝试自动登录"), "上次启动结果以一行证据呈现");
  assert.equal(githubSwitch.checked, false, "GitHub 开关反映关闭真值");

  /* 2) Fudan 开关未选账号即开启：本地拦截，不发请求，开关复位并给出可操作提示 */
  fudanSwitch.checked = false;
  accountSelect.value = "";
  const postsBefore = calls.settingsActionsPost.length;
  fudanSwitch.checked = true;
  fudanSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(calls.settingsActionsPost.length, postsBefore, "未选账号时不发请求");
  assert.equal(fudanSwitch.checked, false, "未选账号时开关复位");
  assert.ok(
    fudanStatus.textContent.includes("请先选择要自动登录的已保存账号"),
    `未选账号给可操作提示：${fudanStatus.textContent}`,
  );

  /* 3) Fudan 开关带账号开启：恰好一次请求，载荷只含偏好与账号；响应真值回渲染 */
  accountSelect.value = "20301080001";
  settingsActionResponse = {
    schema: "courselens.auto-connect.v1",
    fudan: { enabled: true, account_id: "20301080001", status: "ready" },
    github: { enabled: false, status: "off" },
    last_resume: { observed_at: 1, fudan: { state: "started", code: "fudan_resume_started" }, github: { state: "", code: "" } },
  };
  fudanSwitch.checked = true;
  fudanSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(calls.settingsActionsPost.length, postsBefore + 1, "开启自动登录恰好发一次请求");
  assert.equal(calls.settingsActionsPost[postsBefore].action, "set-auto-connect");
  assert.deepEqual(calls.settingsActionsPost[postsBefore].fudan, { enabled: true, account_id: "20301080001" });
  assert.equal(calls.settingsActionsPost[postsBefore].github, undefined, "请求只携带被修改的连接");
  assert.equal(fudanSwitch.checked, true, "成功后开关保持启用");
  assert.ok(
    String(byId["toast-region"].children.at(-1)?.textContent || "").includes("已保存"),
    "成功保存有明确反馈",
  );

  /* 4) 关闭自动登录：请求只含关闭偏好，不触发任何凭据删除/登出动作 */
  settingsActionResponse = {
    schema: "courselens.auto-connect.v1",
    fudan: { enabled: false, account_id: "20301080001", status: "off" },
    github: { enabled: false, status: "off" },
    last_resume: { observed_at: 1, fudan: { state: "started", code: "fudan_resume_started" }, github: { state: "", code: "" } },
  };
  fudanSwitch.checked = false;
  fudanSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.deepEqual(
    calls.settingsActionsPost[postsBefore + 1].fudan, { enabled: false, account_id: "20301080001" },
    "关闭时载荷仅为偏好",
  );

  /* 5) 后端拒绝（账号需轮换 409）：开关从真值复位，内联闭集错误可操作 */
  settingsActionResponse = { error: "auto_connect_account_rotation_required" };
  fudanSwitch.checked = true;
  fudanSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(fudanSwitch.checked, false, "被拒后开关复位为后端真值");
  assert.ok(
    fudanStatus.textContent.includes("需要更新"),
    `拒绝文案为闭集可操作指引：${fudanStatus.textContent}`,
  );
  assert.equal(fudanStatus.dataset.state, "error", "拒绝态以错误样式呈现");

  /* 6) GitHub 开关在无授权时被后端拒绝：复位 + 指向先授权 */
  settingsActionResponse = { error: "auto_connect_github_grant_missing" };
  githubSwitch.checked = true;
  githubSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(githubSwitch.checked, false, "GitHub 被拒后开关复位");
  assert.ok(
    githubStatus.textContent.includes("先完成 GitHub 授权"),
    `GitHub 拒绝文案指向先授权：${githubStatus.textContent}`,
  );

  /* 7) GitHub 开关成功启用 */
  settingsActionResponse = {
    schema: "courselens.auto-connect.v1",
    fudan: { enabled: false, account_id: "20301080001", status: "off" },
    github: { enabled: true, status: "ready" },
    last_resume: { observed_at: 1, fudan: { state: "", code: "" }, github: { state: "started", code: "github_resume_verified" } },
  };
  githubSwitch.checked = true;
  githubSwitch.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(githubSwitch.checked, true, "GitHub 开关成功启用");
  assert.ok(githubStatus.textContent.includes("已确认 GitHub 连接"), "GitHub 上次启动证据呈现");

  /* 8) GitHub 四阶段（流程序重排）：授权缺失 → 授权阶段“需要操作”，其余待进行；阶段使用中文标签而非蛇形组件行 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "authorization_missing" },
    components: [
      { component: "authorization", state: "action_required", code: "authorization_missing", actions: ["start-authorization"], stale: false },
    ],
  };
  await reload();
  const phases = () => byId["github-phases"].children;
  assert.equal(phases().length, 4, "固定四个连接阶段");
  assert.deepEqual(
    phases().map((item) => item.children.find((node) => node.className === "phase-label").textContent),
    ["账号授权", "App 安装", "专属仓库", "加密通道"],
    "阶段序为新流程序：授权 → App 安装 → 专属仓库 → 加密通道",
  );
  assert.equal(phases()[0].dataset.phaseState, "action_required", "授权缺失阶段需要操作");
  assert.equal(phases()[0].children[1].textContent, "需要操作", "阶段状态以文字承载");
  assert.equal(phases()[3].dataset.phaseState, "pending", "通道阶段默认待进行");

  /* 9) 全就绪 + 通道已验证：四阶段全部完成 */
  remoteSnapshot = {
    overall: { state: "ready", code: "ready_for_dispatch" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
      { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      { component: "installation", state: "ready", code: "installation_present", stale: false },
      { component: "channel_test", state: "ready", code: "channel_test_valid", stale: false },
    ],
  };
  await reload();
  assert.deepEqual(
    phases().map((item) => item.dataset.phaseState),
    ["done", "done", "done", "done"],
    "全就绪时四阶段全部完成",
  );
  assert.equal(byId["github-connection-state"].textContent, "已连接", "GitHub 卡状态徽标就绪");

  /* 10) 安装范围不精确（仓库已建）：安装阶段“需要操作”，授权阶段不受影响；
          专属仓库已就绪 → 阶段完成，主按钮为收紧引导 */
  const primaryRow10 = () => byId["remote-primary-action"];
  const primaryButton10 = () => primaryRow10().children.find((node) => node.tagName === "BUTTON") || null;
  const primaryAnchor10 = () => primaryRow10().children.find((node) => node.tagName === "A") || null;
  const regionText10 = (id) => document.getElementById(id).children
    .map((node) => String(node.textContent || "")).join("\n");
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_scope_not_exact" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
      { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
      { component: "installation", state: "action_required", code: "installation_scope_not_exact", stale: false },
    ],
  };
  await reload();
  assert.equal(phases()[0].dataset.phaseState, "done", "授权阶段完成");
  assert.equal(phases()[1].dataset.phaseState, "action_required", "安装范围不精确阶段需要操作");
  assert.equal(primaryButton10().textContent, "调整安装范围后继续初始化", "仓库已建时主按钮为收紧引导");

  /* 10b) 流程序重排：已安装（任意范围）且仓库未建 → 主按钮=创建专属仓库
     （先建仓、后收紧），指引同步指向先建仓 */
  remoteSnapshot = {
    overall: { state: "action_required", code: "installation_scope_not_exact" },
    components: [
      { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
      { component: "installation", state: "action_required", code: "installation_scope_not_exact", stale: false },
    ],
  };
  await reload();
  assert.equal(primaryButton10().textContent, "创建专属仓库并完成初始化", "仓库未建时主按钮为先建仓");
  assert.equal(
    primaryAnchor10(), null,
    "仓库未建时收紧链接不渲染（无仓可圈，避免矛盾组合）",
  );
  assert.ok(
    regionText10("remote-evidence").includes("先创建两个专属仓库"),
    "范围暂宽且仓库未建时指引指向先建仓",
  );

  /* 11) 高级操作与诊断保留：组件证据、脱敏诊断复制仍在卡内；连接动作只经主按钮 */
  assert.equal(byId["remote-component-evidence"] !== null, true, "组件诊断证据区存在");
  assert.notEqual(byId["copy-diagnostics"], null, "诊断区保留 copy-diagnostics");
  assert.notEqual(byId["remote-primary-action"], null, "状态驱动主按钮为唯一连接动作入口");

  /* 12) 焦点根因（S10-A）：指针与键盘激活导航 → 焦点都留在激活的导航按钮上；
          分组大容器/标题不再成为编程焦点目标（容器级大环根因） */
  remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
  await reload();
  const accountNavButton = settingsNavButtons["settings-account-group"];
  const accountHeading = settingsGroups["settings-account-group"].children[0];
  const pointerEvent = new Event("click", { bubbles: true });
  pointerEvent.detail = 1;
  document.activeElement = accountNavButton; /* 浏览器默认行为：指针点击原生聚焦按钮 */
  accountNavButton.dispatchEvent(pointerEvent);
  assert.equal(
    document.activeElement, accountNavButton,
    "指针激活焦点留在导航按钮，不聚焦大容器",
  );
  assert.notEqual(
    document.activeElement, accountHeading,
    "指针路径不把焦点移到分组容器或标题",
  );
  assert.equal(
    accountNavButton.getAttribute("aria-current"), "true",
    "当前分组导航按钮带 aria-current",
  );
  const keyboardEvent = new Event("click", { bubbles: true });
  keyboardEvent.detail = 0;
  const networkNavButton = settingsNavButtons["settings-network-group"];
  const networkHeading = settingsGroups["settings-network-group"].children[0];
  document.activeElement = networkNavButton; /* 键盘用户先 Tab 到导航按钮 */
  networkNavButton.dispatchEvent(keyboardEvent);
  assert.equal(document.activeElement, networkNavButton, "键盘激活焦点留在激活的导航按钮");
  assert.notEqual(document.activeElement, networkHeading, "键盘激活不再聚焦分组标题");
  assert.equal(
    accountNavButton.getAttribute("aria-current"), null,
    "aria-current 跟随最近一次导航",
  );

  /* 复位，避免污染后续场景 */
  settingsSnapshot = { ...settingsSnapshot, auto_connect: null };
  settingsActionResponse = null;
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  await reload();
}

async function verifyLoginDialogAutoConnectOption() {
  const checkbox = byId["login-auto-connect"];
  const savedStatus = byId["login-saved-status"];
  const fudanSwitch = byId["fudan-auto-connect"];
  const openDialog = async () => {
    openLoginDialog();
    await settle();
  };

  /* 1) 初始态对齐快照：启用+恰一可用账号预选 → 复选勾选；与设置页开关同源 */
  accountsSnapshot = { accounts: [{ student_id: "20301080001", requires_rotation: false }], deepseek: { configured: false, saved: false } };
  settingsSnapshot = {
    ...settingsSnapshot,
    auto_connect: {
      schema: "courselens.auto-connect.v1",
      fudan: { enabled: true, account_id: "20301080001", status: "ready" },
      github: { enabled: false, status: "off" },
      last_resume: { observed_at: 1, fudan: { state: "started", code: "fudan_resume_started" }, github: { state: "", code: "" } },
    },
  };
  await openDialog();
  assert.equal(checkbox.checked, true, "登录框复选初始态对齐 auto_connect 真值");
  assert.equal(fudanSwitch.checked, true, "设置页开关同源反映同一偏好");

  /* 2) 无选中账号勾选：本地拦截零请求，身份卡状态行给可操作提示 */
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  const postsBefore = calls.settingsActionsPost.length;
  await openDialog();
  assert.equal(checkbox.checked, false, "无已保存账号时复选诚实未勾");
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(calls.settingsActionsPost.length, postsBefore, "未选账号时不发请求");
  assert.equal(checkbox.checked, false, "本地拦截后复选复位");
  assert.ok(savedStatus.textContent.includes("请先选择"), `拦截提示可操作：${savedStatus.textContent}`);

  /* 3) 选中账号勾选：恰一次 set-auto-connect，载荷仅偏好+账号；成功后保持勾选 */
  accountsSnapshot = { accounts: [{ student_id: "20301080001", requires_rotation: false }], deepseek: { configured: false, saved: false } };
  settingsActionResponse = {
    schema: "courselens.auto-connect.v1",
    fudan: { enabled: true, account_id: "20301080001", status: "ready" },
    github: { enabled: false, status: "off" },
    last_resume: { observed_at: 1, fudan: { state: "started", code: "fudan_resume_started" }, github: { state: "", code: "" } },
  };
  await openDialog();
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(calls.settingsActionsPost.length, postsBefore + 1, "开启自动登录恰好发一次请求");
  assert.equal(calls.settingsActionsPost[postsBefore].action, "set-auto-connect");
  assert.deepEqual(calls.settingsActionsPost[postsBefore].fudan, { enabled: true, account_id: "20301080001" });
  assert.equal(checkbox.checked, true, "成功后复选保持勾选");

  /* 4) 取消勾选：载荷仅关闭偏好，不触碰凭据/登录链 */
  settingsActionResponse = {
    schema: "courselens.auto-connect.v1",
    fudan: { enabled: false, account_id: "20301080001", status: "off" },
    github: { enabled: false, status: "off" },
    last_resume: { observed_at: 1, fudan: { state: "started", code: "fudan_resume_started" }, github: { state: "", code: "" } },
  };
  checkbox.checked = false;
  checkbox.dispatchEvent(new Event("change"));
  await settle();
  assert.deepEqual(
    calls.settingsActionsPost[postsBefore + 1].fudan, { enabled: false, account_id: "20301080001" },
    "关闭时载荷仅为偏好",
  );

  /* 5) 后端拒绝（账号需轮换 409）：复选从后端真值复位，闭集错误落在身份卡状态行 */
  settingsActionResponse = { error: "auto_connect_account_rotation_required" };
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(checkbox.checked, false, "被拒后复选复位为后端真值");
  assert.ok(savedStatus.textContent.includes("需要更新"), `拒绝文案闭集可操作：${savedStatus.textContent}`);

  /* 6) 多账号诚实性：多可用账号不代选 → 复选不亮（偏好绑定关系未成立） */
  accountsSnapshot = {
    accounts: [
      { student_id: "20301080001", requires_rotation: false },
      { student_id: "20301080002", requires_rotation: false },
    ],
    deepseek: { configured: false, saved: false },
  };
  await openDialog();
  assert.equal(checkbox.checked, false, "多可用账号不代选：复选不亮");

  /* 复位，避免污染后续场景 */
  settingsActionResponse = null;
  accountsSnapshot = { accounts: [], deepseek: { configured: false, saved: false } };
  settingsSnapshot = { ...settingsSnapshot, auto_connect: null };
  await openDialog();
  assert.equal(checkbox.checked, false, "复位后复选未勾");
}

/* N6L S1 U3（还债减法随迁）：live-only 学习桌模式已随直播播放整体退役——
   学习页对 live-play 事件零反应，学习桌只由讲次选择开启；直播动线=
   header 钮/卡片跳转 → data-page=live 独立页（行为钉见 frontend_live_page_behavior.mjs）。 */
async function verifyLiveDeskMode() {
  const manifestPath = "/api/v3/live-room/play/synthetic/manifest/playlist.m3u8";

  /* 仅选课、未进直播、未选讲次：不在学习桌 */
  store.set("activeCourse", { course_id: "c-live", title: "直播课程" });
  await settle();
  assert.equal(byId["study-desk"].hidden, true, "仅选课时不在学习桌");

  /* 直播播放事件（旧协议）：学习页零反应，绝不复活 live-only 桌面 */
  window.dispatchEvent(new CustomEvent("courselens:live-play", {
    detail: { manifestPath, title: "合成直播" },
  }));
  await settle();
  assert.equal(byId["study-desk"].hidden, true, "live-play 不再开启学习桌");
  assert.equal(byId["topbar-crumbs"].hidden, true, "不出现直播面包屑");

  /* 讲次选择仍是学习桌唯一开启路径：面包屑回讲次层级。
     UIAUDIT-1 F15：面包屑归属学习页——早前场景 selectPage 到过其他页，
     此处按真实动线先回到学习页（page 事件先行），再选讲次。 */
  selectPage("study");
  await settle();
  store.set("activeLecture", { course_id: "c-live", sub_id: "l-live", sub_title: "回放讲次", can_stream: true });
  await settle();
  assert.equal(byId["study-desk"].hidden, false, "讲次模式保持学习桌");
  assert.equal(byId["topbar-crumbs"].textContent, "直播课程 · 回放讲次", "面包屑回讲次层级");
  store.set("activeLecture", null);
}

/* ---- 7) 顶栏 transient popover（连接/账户）：共享基座外点关闭（pointerdown capture） ---- */

/* N5FE-P7：未登录点账户钮开菜单而非直接弹登录框——设置两跳内可达；
   checking 期账户钮不再禁用（F8 同款逃生门原则） */
async function verifyAccountMenuReachableWhenLoggedOut() {
  const accountTrigger = byId["account-button"];
  const accountMenu = byId["account-menu"];
  try {
    store.set("auth", { state: "login_required", code: "fudan_login_required", actions: ["login"], connected: false, configured: false });
    await settle();
    assert.equal(accountTrigger.disabled, false, "未登录账户钮可用");
    accountTrigger.click();
    await settle();
    assert.equal(accountMenu.hidden, false, "未登录点账户钮打开菜单（而非登录框）");
    assert.ok(byId["account-menu-settings"], "菜单内保留设置入口（两跳内可达）");
    accountTrigger.click();
    await settle();
    assert.equal(accountMenu.hidden, true, "再点触发器关闭菜单");
  } finally {
    store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["logout"], connected: true, configured: true });
    await settle();
  }
}

async function verifyTopbarPopoverOutsideDismiss() {
  const connTrigger = byId["conn-status"];
  const connMenu = byId["conn-menu"];
  const accountTrigger = byId["account-button"];
  const accountMenu = byId["account-menu"];
  const main = byId["workspace-main"];

  /* 弹层内可命中节点（内部 pointerdown 不关闭）与可聚焦目标 */
  const connLink = register(new FakeElement("button", "conn-menu-link"));
  connMenu.append(connLink);
  accountMenu.append(byId["account-menu-settings"]);

  /* document capture pointerdown 注册计数（真实 openOverlay/closeOverlay 驱动） */
  const docAdd = document.addEventListener.bind(document);
  const docRemove = document.removeEventListener.bind(document);
  let pointerAdds = 0;
  let pointerRemoves = 0;
  document.addEventListener = (type, listener, options) => {
    if (type === "pointerdown") pointerAdds += 1;
    return docAdd(type, listener, options);
  };
  document.removeEventListener = (type, listener, options) => {
    if (type === "pointerdown") pointerRemoves += 1;
    return docRemove(type, listener, options);
  };
  const pointerDownAt = (target) => {
    const event = new Event("pointerdown");
    Object.defineProperty(event, "target", { value: target, configurable: true });
    document.dispatchEvent(event);
  };
  const escapeKey = () => {
    const key = new Event("keydown");
    Object.defineProperty(key, "key", { value: "Escape" });
    document.dispatchEvent(key);
  };

  /* 账户菜单需要 ready 态才能打开（非 ready 走登录 dialog） */
  authSnapshot = { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true };
  window.dispatchEvent(new Event("courselens:auth-refresh"));
  await settle();
  assert.equal(accountTrigger.disabled, false, "ready 态账户按钮可点");

  /* 1) 外点关闭：hidden/aria/inert/焦点归还全部走既有 closeOverlay 合同 */
  connTrigger.click();
  await settle();
  assert.equal(connMenu.hidden, false, "连接弹层打开");
  assert.equal(connTrigger.getAttribute("aria-expanded"), "true", "打开 aria-expanded=true");
  assert.equal(main.inert, true, "打开后主区 inert");
  pointerDownAt(main);
  assert.equal(connMenu.hidden, true, "外部 pointerdown 关闭弹层");
  assert.equal(connTrigger.getAttribute("aria-expanded"), "false", "关闭同步 aria-expanded=false");
  assert.equal(main.inert, false, "关闭解除 inert");
  assert.equal(document.activeElement, connTrigger, "焦点归还触发器");

  /* 2) 弹层内部（root/子节点）pointerdown 不关闭 */
  connTrigger.click();
  await settle();
  pointerDownAt(connLink);
  assert.equal(connMenu.hidden, false, "弹层子节点 pointerdown 保持打开");
  pointerDownAt(connMenu);
  assert.equal(connMenu.hidden, false, "弹层 root pointerdown 保持打开");
  connTrigger.click();
  await settle();
  assert.equal(connMenu.hidden, true, "触发器 click 关闭（前置清理）");

  /* 3) 触发器 pointerdown 不由外点逻辑关闭，click 只切换一次、不重开 */
  connTrigger.click();
  await settle();
  pointerDownAt(connTrigger);
  assert.equal(connMenu.hidden, false, "触发器 pointerdown 不提前关闭");
  connTrigger.click();
  assert.equal(connMenu.hidden, true, "触发器 click 关闭一次");
  assert.equal(connTrigger.getAttribute("aria-expanded"), "false", "无关闭后重开");

  /* 4) 重复开关不累积 listener；关闭后无残留外点行为 */
  for (let cycle = 0; cycle < 3; cycle += 1) {
    connTrigger.click();
    await settle();
    pointerDownAt(main);
    assert.equal(connMenu.hidden, true, `第 ${cycle + 1} 轮外点关闭`);
  }
  assert.equal(pointerAdds, 6, "六次打开各注册一次 pointerdown");
  assert.equal(pointerRemoves, 6, "六次关闭各移除一次，无泄漏");
  pointerDownAt(main);
  assert.equal(connMenu.hidden, true, "关闭态外点无副作用");

  /* 5) Esc 不回退 */
  connTrigger.click();
  await settle();
  escapeKey();
  assert.equal(connMenu.hidden, true, "Esc 仍关闭");
  assert.equal(document.activeElement, connTrigger, "Esc 焦点归还");
  assert.equal(pointerAdds, pointerRemoves, "Esc 路径同样移除 pointerdown listener");

  /* 6) 单浮层守卫被挡：无外点 listener/ARIA 副作用 */
  const addsBefore = pointerAdds;
  connTrigger.click();
  await settle();
  accountTrigger.click();
  assert.equal(accountMenu.hidden, true, "被挡时账户弹层不打开");
  assert.equal(accountTrigger.getAttribute("aria-expanded"), "false", "被挡时 aria-expanded 不变");
  assert.equal(connMenu.hidden, false, "原弹层保持打开");
  assert.equal(pointerAdds, addsBefore + 1, "仅连接弹层自身注册（被挡无注册）");
  pointerDownAt(main);
  assert.equal(connMenu.hidden, true, "清理：外点关闭连接弹层");

  /* 抽屉（未启用外点关闭）打开时连接弹层被挡且不注册外点 listener */
  const chip = byId["task-chip"];
  chip.click();
  await settle();
  assert.equal(byId["tasks-root"].hidden, false, "抽屉打开");
  connTrigger.click();
  assert.equal(connMenu.hidden, true, "抽屉在场时连接弹层被挡");
  assert.equal(connTrigger.getAttribute("aria-expanded"), "false", "被挡不写 aria");
  assert.equal(pointerAdds, addsBefore + 1, "被挡的 openOverlay 不注册 pointerdown listener");
  escapeKey();
  await settle();
  assert.equal(byId["tasks-root"].hidden, true, "清理：Esc 关闭抽屉");

  /* 控件互相切换：连接弹层打开时外点账户控件 → 先关连接，click 再开账户，最终一个浮层 */
  connTrigger.click();
  await settle();
  pointerDownAt(accountTrigger);
  assert.equal(connMenu.hidden, true, "点击另一顶栏控件先关闭当前弹层");
  assert.equal(accountTrigger.getAttribute("aria-expanded"), "false", "pointerdown 阶段账户尚未打开");
  accountTrigger.click();
  await settle();
  assert.equal(accountMenu.hidden, false, "账户弹层随后打开");
  assert.equal(accountTrigger.getAttribute("aria-expanded"), "true", "账户 aria-expanded=true");
  assert.equal(connMenu.hidden, true, "最终仅一个浮层（连接已关）");

  /* 7) 账户弹层外点关闭一次 */
  pointerDownAt(main);
  assert.equal(accountMenu.hidden, true, "账户弹层外点关闭");
  assert.equal(accountTrigger.getAttribute("aria-expanded"), "false", "账户关闭同步 aria");

  assert.equal(pointerAdds, pointerRemoves, "全部周期后 add/remove 平衡");

  /* 复位账户态，避免影响后续断言 */
  authSnapshot = { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false };
  document.addEventListener = docAdd;
  document.removeEventListener = docRemove;
}

/* ---- 8) 校园连接卡：V0 闭集状态/单一主动作/迁移播报/代际内联态/未知与过期降级 ---- */

async function verifyCampusConnectionCard() {
  /* CAMPUS-P3-16：任何交互前抽屉必须仍处真实 HTML 的初始 hidden（证据门控） */
  assert.equal(byId["campus-diagnostics"].hidden, true, "未检查时抽屉收起（安静）");
  const fixture = JSON.parse(
    readFileSync(new URL("./fixtures/vpn_connection_contract_v1.json", import.meta.url), "utf8"),
  ).valid;
  const now = Math.floor(Date.now() / 1000);
  const snapshotOf = (overrides = {}) => ({
    schema: "courselens.vpn-connection.v1",
    state: "ready", network_path: "direct", school_route: "webvpn", reason: "direct_ok",
    observed_at: now - 30, expires_at: now + 3600, retry_after: null, actions: [], generation: 7,
    services: {
      webvpn: { state: "ready", route: "direct", verified: true },
      icourse: { state: "ready", route: "direct", verified: true },
    },
    ...overrides,
  });
  const checkingAuth = { state: "checking", code: "fudan_session_checking", connected: false, configured: true };
  const setAuth = async (payload) => { store.set("auth", payload); await settle(); };
  const stateText = () => byId["campus-connection-state"].textContent;
  const liveText = () => byId["campus-connection-live"].textContent;
  const actionButtons = () => byId["campus-connection-actions"].children.filter((node) => node.tagName === "BUTTON");
  const primaryAction = () => actionButtons()[0]?.dataset.campusAction || "";
  const primaryLabel = () => actionButtons()[0]?.textContent || "";

  /* 0) 无快照回退（后端尚未发布快照）：既有 auth 派生；healthy 安静且上下文保留 */
  store.activeCourse = { course_id: "c-keep", title: "上下文课程" };
  store.activeLecture = { sub_id: "l-keep" };
  await setAuth({ state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"] });
  assert.equal(stateText(), "校园连接正常", "派生 ready 文案");
  assert.equal(primaryAction(), "", "ready 无主动作按钮（健康安静）");
  assert.equal(byId["campus-connection-reason"].hidden, true, "健康态无原因/TUN 提示");
  /* AS4-U2b：校园健康（含无快照的 ready 派生）→ 网络副行整块收起 */
  assert.equal(byId["campus-connection"].hidden, true, "健康时网络副行收起，弹层只讲复旦与 GitHub");
  assert.equal(store.activeCourse?.course_id, "c-keep", "连接卡重绘不清选课上下文");
  assert.equal(store.activeLecture?.sub_id, "l-keep", "连接卡重绘不清讲次上下文");

  /* 1) V0 fixture 五个有效快照逐个驱动（observed_at 刷新为新鲜，隔离消费端 staleness 规则） */
  const fixtureExpectations = {
    ready_direct: ["校园连接正常", ""],
    checking_cold_start: ["正在确认校园连接", "check-network"],
    degraded_proxy_fallback: ["校园服务部分可用", "retry"],
    login_required: ["需要登录复旦课程平台", "login"],
    expired_session: ["校园会话已过期", "reauthenticate"],
  };
  for (const [name, snap] of Object.entries(fixture)) {
    await setAuth({
      ...checkingAuth,
      connection: { ...snap, observed_at: now - 30, expires_at: snap.expires_at ? now + 3600 : null },
    });
    const [expectedText, expectedAction] = fixtureExpectations[name];
    assert.equal(stateText(), expectedText, `fixture ${name} 状态文案`);
    assert.equal(primaryAction(), expectedAction, `fixture ${name} 主动作`);
    /* AS4-U2b：副行可见性=「非 ready 才显」——健康 ready_direct 收起，其余显指引 */
    assert.equal(byId["campus-connection"].hidden, name === "ready_direct", `fixture ${name} 网络副行可见性`);
  }

  /* POLISH-1 F4（化身走查 FULL-CLIENT-INSPECT F4）：degraded 点名哪部分不可用——
     修前红：状态行只说「校园服务部分可用」，与「复旦会话已连接」并列时学生无从
     分辨；修后原因行按闭集服务标签点名（fixture=WebVPN 可用、课程平台不可用），
     正常部分一并交代；无快照/服务态未知时保持既有安静，绝不发明。 */
  await setAuth({
    ...checkingAuth,
    connection: { ...fixture.degraded_proxy_fallback, observed_at: now - 30, expires_at: now + 3600 },
  });
  assert.equal(stateText(), "校园服务部分可用", "F4 degraded 状态文案不变");
  assert.equal(byId["campus-connection-reason"].hidden, false, "F4 点名原因行可见");
  const f4Reason = String(byId["campus-connection-reason"].textContent || "");
  assert.ok(f4Reason.includes("课程平台"), "F4 点名不可用服务（课程平台）");
  assert.ok(f4Reason.includes("暂时连不上"), "F4 用人话说不可用");
  assert.ok(f4Reason.includes("WebVPN正常"), "F4 同时交代正常部分");
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "degraded", reason: "service_unavailable", expires_at: null, actions: ["retry"], generation: 13,
    services: { webvpn: { state: "unknown", route: "unknown", verified: false }, icourse: { state: "unknown", route: "unknown", verified: false } },
  }) });
  assert.equal(byId["campus-connection-reason"].hidden, true, "F4 服务态未知不发明文案（保持安静）");

  /* 2) 合成其余四态（契约有效形状，覆盖九态全闭集） */
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "off", network_path: "unknown", school_route: "unknown", reason: "cold_start",
    expires_at: null, actions: ["check-network"], generation: 0,
    services: { webvpn: { state: "unknown", route: "unknown", verified: false }, icourse: { state: "unknown", route: "unknown", verified: false } },
  }) });
  assert.equal(stateText(), "校园连接待确认", "off 状态文案");
  assert.equal(primaryAction(), "check-network", "off 主动作");

  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "reauthenticating", reason: "session_expired", expires_at: null, actions: [], generation: 8,
  }) });
  assert.equal(stateText(), "正在重新认证校园会话", "reauthenticating 文案");
  assert.equal(primaryAction(), "", "reauthenticating 在途安静：无按钮");

  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "network_unavailable", network_path: "unknown", reason: "service_unavailable",
    expires_at: null, actions: ["check-network", "open-settings"],
  }) });
  assert.equal(stateText(), "校园网络暂不可达", "network_unavailable 中性文案");
  assert.equal(primaryAction(), "check-network", "network_unavailable 主动作");
  assert.equal(byId["campus-connection-reason"].hidden, true, "非 TUN 理由不出现提示行");
  assert.equal(byId["campus-connection"].hidden, false, "网络异常时副行显具体指引（AS4-U2b）");

  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "challenge_required", reason: "challenge", expires_at: null, actions: ["login"],
  }) });
  assert.equal(stateText(), "需要在复旦页面完成安全验证", "challenge_required 文案");
  assert.equal(primaryAction(), "login", "challenge_required 走受控登录入口");

  /* 3) possible_tun_interference：唯一允许 TUN 表述的位置，保持“可能”措辞 */
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "network_unavailable", reason: "possible_tun_interference",
    expires_at: null, actions: ["close-tun-and-retry", "open-settings"],
  }) });
  assert.equal(byId["campus-connection-reason"].hidden, false, "TUN 提示行可见");
  assert.ok(byId["campus-connection-reason"].textContent.includes("可能"), "保持不确定性措辞");
  assert.ok(byId["campus-connection-reason"].textContent.includes("不一定"), "明确不宣称因果");
  assert.equal(primaryLabel(), "关闭 TUN 后重试", "后端动作原样映射标签");
  assert.equal(stateText().includes("possible_tun_interference"), false, "不渲染原始枚举值");

  /* 3b) P2-B：可关闭 TUN 提示行——episode 内可关闭、新 episode 重现、健康态无行 */
  const tunSnapshot = () => snapshotOf({
    state: "network_unavailable", reason: "possible_tun_interference",
    expires_at: null, actions: ["close-tun-and-retry", "open-settings"],
  });
  assert.equal(byId["campus-connection-tun"].hidden, false, "证据支持时提示行可见");
  assert.equal(byId["campus-connection-tun-text"].hidden, false, "提示行文本节点在场");
  byId["campus-connection-tun-dismiss"].click();
  assert.equal(byId["campus-connection-tun"].hidden, true, "不再提示后提示行隐藏");
  assert.equal(byId["campus-connection-reason"].hidden, true, "关闭后 TUN 原因文案一并隐藏");
  await setAuth({ ...checkingAuth, connection: tunSnapshot() });
  assert.equal(byId["campus-connection-tun"].hidden, true, "同 episode 重复快照不复活提示");
  await setAuth({ ...checkingAuth, connection: { ...tunSnapshot(), observed_at: now + 120, generation: 8 } });
  assert.equal(byId["campus-connection-tun"].hidden, false, "新失败 episode 重现提示");
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: snapshotOf({ state: "ready", generation: 8 }) });
  assert.equal(byId["campus-connection-tun"].hidden, true, "健康态无提示行");
  assert.equal(byId["campus-connection-reason"].hidden, true, "健康态无原因文案");
  assert.equal(byId["campus-connection"].hidden, true, "恢复健康后副行再次收起（AS4-U2b）");

  /* 4) 播报只在状态迁移时写入一次；checking/reauthenticating 安静 */
  const announcedBefore = liveText();
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "checking", reason: "cold_start", network_path: "unknown", school_route: "unknown",
    expires_at: null, actions: ["check-network"], generation: 9,
    services: { webvpn: { state: "checking", route: "unknown", verified: false }, icourse: { state: "checking", route: "unknown", verified: false } },
  }) });
  assert.equal(liveText(), announcedBefore, "进入 checking 不播报");
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "login_required", reason: "credentials_rejected", expires_at: null, actions: ["login"], generation: 9,
  }) });
  assert.equal(liveText(), "需要登录复旦课程平台", "迁移到 login_required 播报一次");
  const beforeRepeat = liveText();
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "login_required", reason: "credentials_rejected", expires_at: null, actions: ["login"], generation: 10,
  }) });
  assert.equal(liveText(), beforeRepeat, "同状态重复快照不重复播报");

  /* 5) 动作接线：六个动作全部落到既有本地动作（登录窗/auth 复检/设置页），无新流 */
  const dialog = byId["login-dialog"];
  assert.equal(primaryAction(), "login", "当前主动作为 login");
  actionButtons()[0].click();
  assert.equal(dialog.open, true, "login 打开既有登录窗");
  assert.equal(byId["login-dialog-title"].textContent, "复旦统一身份认证", "登录标题");
  byId["cancel-login"].click();
  assert.equal(dialog.open, false, "登录窗已关");

  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "expired", reason: "session_expired", expires_at: null, actions: ["reauthenticate"], generation: 10,
  }) });
  actionButtons()[0].click();
  assert.equal(dialog.open, true, "reauthenticate 打开既有登录窗");
  assert.equal(byId["login-dialog-title"].textContent, "重新认证复旦会话", "重认证标题");
  byId["cancel-login"].click();

  const authGetBefore = calls.authGet;
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "network_unavailable", reason: "service_unavailable", expires_at: null, actions: ["check-network"], generation: 10,
  }) });
  actionButtons()[0].click();
  await settle();
  assert.ok(calls.authGet > authGetBefore, "check-network 复用既有 auth 复检");
  assert.equal(dialog.open, false, "复检不开登录窗");

  await setAuth({ state: "degraded", code: "fudan_session_degraded", connection: snapshotOf({
    state: "degraded", reason: "proxy_fallback", network_path: "local_proxy", school_route: "mixed",
    actions: ["retry"], retry_after: now + 60, generation: 10,
  }) });
  const authGetBeforeRetry = calls.authGet;
  actionButtons()[0].click();
  await settle();
  assert.ok(calls.authGet > authGetBeforeRetry, "retry 同走既有复检");

  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "network_unavailable", reason: "possible_tun_interference", expires_at: null,
    actions: ["close-tun-and-retry"], generation: 10,
  }) });
  const authGetBeforeTun = calls.authGet;
  actionButtons()[0].click();
  await settle();
  assert.ok(calls.authGet > authGetBeforeTun, "close-tun-and-retry 是有界复检（不触系统 TUN/代理）");

  byId["conn-status"].click();
  await settle();
  assert.equal(byId["conn-menu"].hidden, false, "弹层打开（open-settings 场景）");
  await setAuth({ state: "degraded", code: "fudan_session_degraded", connection: snapshotOf({
    state: "degraded", reason: "proxy_fallback", network_path: "local_proxy", school_route: "mixed",
    actions: ["open-settings", "retry"], retry_after: now + 60, generation: 11,
  }) });
  /* 一状态一主动作：后端列表按优先级排序，卡片只渲染首个动作，次动作走既有设置/账户入口 */
  assert.equal(actionButtons().length, 1, "每状态只渲染一个主动作按钮");
  const settingsButton = actionButtons().find((button) => button.dataset.campusAction === "open-settings");
  assert.ok(settingsButton, "首个动作为 open-settings");
  settingsButton.click();
  assert.equal(byId["conn-menu"].hidden, true, "打开设置前先关弹层");
  assert.equal(byId["settings-page"].hidden, false, "走既有页面切换打开设置");
  selectPage("study");

  /* 6) generation 升高 + 确认中 → “网络设置已变化”内联小状态，而非登出弹窗 */
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: snapshotOf({ state: "ready", generation: 20 }) });
  assert.equal(byId["campus-connection-regenerated"].hidden, true, "健康基线无代际提示");
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "checking", reason: "cold_start", network_path: "unknown", school_route: "unknown",
    expires_at: null, actions: ["check-network"], generation: 21,
    services: { webvpn: { state: "checking", route: "unknown", verified: false }, icourse: { state: "checking", route: "unknown", verified: false } },
  }) });
  assert.equal(byId["campus-connection-regenerated"].hidden, false, "代际升高+确认中 → 显示内联态");
  assert.equal(byId["campus-connection-regenerated"].textContent, "网络设置已变化，正在重新确认校园连接", "内联文案精确");
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: snapshotOf({ state: "ready", generation: 21 }) });
  assert.equal(byId["campus-connection-regenerated"].hidden, true, "确认完成即收起内联态");

  /* 7) 健康安静：同快照重复轮询零 DOM 写入 */
  const stateNode = byId["campus-connection-state"];
  let stateWrites = 0;
  stateNode.__campusText = stateNode.textContent;
  Object.defineProperty(stateNode, "textContent", {
    configurable: true,
    get() { return this.__campusText || ""; },
    set(value) { stateWrites += 1; this.__campusText = value; },
  });
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: snapshotOf({ state: "ready", generation: 21 }) });
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: snapshotOf({ state: "ready", generation: 21 }) });
  assert.equal(stateWrites, 0, "健康同态重复轮询零重绘");
  const restoredState = stateNode.__campusText;
  delete stateNode.textContent;
  stateNode.textContent = restoredState;

  /* 8) 未知字段/未知枚举优雅降级：中性文案 + 兜底动作，原值不进 DOM */
  await setAuth({ ...checkingAuth, connection: {
    ...snapshotOf({ state: "maintenance", expires_at: null, actions: ["check-network"] }),
    future_hint: { detail: "内部字段" },
    services: { webvpn: { state: "degraded", route: "quantum", verified: true }, icourse: { state: "ready", route: "direct", verified: true } },
  } });
  assert.equal(stateText(), "校园连接状态待确认", "未知 state 降级中性文案");
  assert.equal(primaryAction(), "check-network", "未知 state 兜底动作");
  assert.equal(stateText().includes("maintenance"), false, "未知枚举原值不显示");
  assert.equal(byId["campus-connection-services"].textContent.includes("quantum"), false, "未知服务路由不进 DOM");

  /* 9) 消费端时效规则：过期快照（>300s）→ 中性复检态 */
  await setAuth({ ...checkingAuth, connection: snapshotOf({ state: "ready", observed_at: now - 400 }) });
  assert.equal(stateText(), "校园连接状态需要重新确认", "过期快照显示中性复检态");
  assert.equal(primaryAction(), "check-network", "过期快照兜底动作");

  /* 10) v2/未知 schema 拒绝：按无快照处理，回退 auth 派生 */
  await setAuth({ state: "ready", code: "fudan_session_verified", connection: {
    ...snapshotOf({ state: "degraded", reason: "proxy_fallback", actions: ["retry"] }),
    schema: "courselens.vpn-connection.v2",
  } });
  assert.equal(stateText(), "校园连接正常", "v2 快照按无快照处理，回退派生视图");

  /* 11) 详情披露：闭集标签 + 上次检查 + 粗粒度结果；绝无地址/凭据/账号形态 */
  await setAuth({ state: "degraded", code: "fudan_session_degraded", connection: snapshotOf({
    state: "degraded", reason: "proxy_fallback", network_path: "local_proxy", school_route: "mixed", actions: ["retry"],
    services: { webvpn: { state: "ready", route: "direct", verified: true }, icourse: { state: "unavailable", route: "local_proxy", verified: false } },
  }) });
  const details = byId["campus-connection-details"];
  details.open = true;
  assert.equal(byId["campus-connection-path"].textContent, "本机代理 · 混合路由", "逻辑路径闭集标签");
  assert.notEqual(byId["campus-connection-checked"].textContent, "尚未确认", "显示上次检查时间");
  assert.equal(
    byId["campus-connection-services"].textContent,
    "WebVPN 可用（已验证） · 课程平台 不可用（未验证）",
    "粗粒度服务结果",
  );
  const detailText = [byId["campus-connection-path"], byId["campus-connection-checked"], byId["campus-connection-services"]]
    .map((node) => node.textContent).join("|");
  assert.equal(/https?:|cookie|token|lck|@/i.test(detailText), false, "详情不含地址/凭据/账号形态");
  await setAuth({ state: "ready", code: "fudan_session_verified" });
  assert.equal(byId["campus-connection-path"].textContent, "尚未确认", "无快照时详情诚实缺省");
  assert.equal(byId["campus-connection-checked"].textContent, "尚未确认", "无快照不虚构时间");
  details.open = false;

  /* 12) 键盘焦点：弹层内 Tab 圈闭回绕 */
  await setAuth({ ...checkingAuth, connection: snapshotOf({
    state: "network_unavailable", reason: "service_unavailable", expires_at: null,
    actions: ["check-network", "open-settings"],
  }) });
  byId["conn-status"].click();
  await settle();
  assert.equal(byId["conn-menu"].hidden, false, "弹层已打开（焦点圈闭场景）");
  const focusables = byId["conn-menu"]
    .querySelectorAll("button:not([disabled])")
    /* 与 ui.js focusableElements 同口径：[hidden] 子树内的控件不进焦点圈
       （P2-B 可关闭 TUN 提示在失败态之外恒隐藏）。 */
    .filter((node) => !node.hidden && !node.closest("[hidden]"));
  assert.ok(focusables.length >= 2, "弹层内存在可聚焦控件");
  document.activeElement = focusables[focusables.length - 1];
  const tabEvent = new Event("keydown");
  Object.defineProperty(tabEvent, "key", { value: "Tab" });
  Object.defineProperty(tabEvent, "shiftKey", { value: false });
  byId["conn-menu"].dispatchEvent(tabEvent);
  assert.equal(document.activeElement, focusables[0], "Tab 从末尾回绕到第一个可聚焦控件");
  byId["conn-status"].click();
  await settle();
  assert.equal(byId["conn-menu"].hidden, true, "复位：弹层关闭");

  /* 13) 保存网络设置 → 立即复用既有复检（后端将升高 generation，卡片随后显示内联态） */
  settingsActionResponse = null;
  const authGetBeforeSave = calls.authGet;
  byId["save-network"].click();
  await settle();
  assert.ok(calls.settingsActionsPost.length >= 1, "update-network 已提交");
  assert.ok(calls.authGet > authGetBeforeSave, "保存网络设置后立即复检校园连接");

  /* 13b) D8：代理保存前闭集校验——手动模式空/脏地址零 POST+内联人话指路；
           非手动模式的陈旧脏值同样零 POST；合法地址恢复真实 POST。 */
  {
    const postBeforeD8 = calls.settingsActionsPost.length;
    byId["network-mode"].value = "manual";
    byId["proxy-url"].value = "";
    byId["save-network"].click();
    await settle();
    assert.equal(calls.settingsActionsPost.length, postBeforeD8, "手动+空地址：零 POST");
    const d8Evidence = deepElements(byId["network-evidence"]).map((node) => String(node.textContent || "")).join("\n");
    assert.ok(d8Evidence.includes("http://主机:端口"), "空地址内联人话指路");
    byId["proxy-url"].value = "127.0.0.1:8888";
    byId["save-network"].click();
    await settle();
    assert.equal(calls.settingsActionsPost.length, postBeforeD8, "无 scheme 地址：零 POST（不再静默补 http://）");
    byId["proxy-url"].value = "http://127.0.0.1:8888";
    byId["save-network"].click();
    await settle();
    assert.ok(calls.settingsActionsPost.length > postBeforeD8, "合法地址恢复真实 POST");
    byId["network-mode"].value = "auto";
  }

  /* 14) 收尾：连接卡随代际重绘（auth 指纹不变 → study 早退，隔离连接卡自身行为），
          恢复在途期间选课/讲次上下文保留；随后复位基线 */
  store.set("courses", [{ course_id: "c-keep", title: "上下文课程", lectures: [{ sub_id: "l-keep", sub_title: "上下文讲次" }] }]);
  store.activeCourse = { course_id: "c-keep", title: "上下文课程" };
  store.activeLecture = { sub_id: "l-keep" };
  /* AS4-U1：恢复期目录探测照常发出——注入含上下文课程的 checking 信封（真实
     语义：本机缓存里有你的课程），目录重绘走既有自动选中语义保住 previous。 */
  catalogEnvelopeOverride = {
    state: "checking", code: "fudan_session_checking", actions: [],
    refreshing: true, course_count: 1,
    courses: [{
      course_id: "c-keep", title: "上下文课程", term: "2026-2027学年1",
      authorization_state: "verified", lectures: [{ sub_id: "l-keep", sub_title: "上下文讲次" }],
    }],
  };
  const recoveryAuth = { state: "checking", code: "fudan_session_checking", connected: false, configured: true };
  const recoverySnapshot = () => snapshotOf({
    state: "checking", reason: "cold_start", network_path: "unknown", school_route: "unknown",
    expires_at: null, actions: ["check-network"],
    services: { webvpn: { state: "checking", route: "unknown", verified: false }, icourse: { state: "checking", route: "unknown", verified: false } },
  });
  await setAuth({ ...recoveryAuth, connection: { ...recoverySnapshot(), generation: 30 } });
  await setAuth({ ...recoveryAuth, connection: { ...recoverySnapshot(), generation: 31 } });
  assert.equal(store.activeCourse?.course_id, "c-keep", "恢复在途选课上下文保留");
  assert.equal(store.activeLecture?.sub_id, "l-keep", "恢复在途讲次上下文保留");
  catalogEnvelopeOverride = null;
  await setAuth({ state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"] });

  /* 15) P1-A 挑战感知卡片：闭集错误码承载细分事实（文案互异、动作互异、
          唯一主动作）；健康态恢复安静 */
  await setAuth({ ...checkingAuth, code: "fudan_challenge_required", connection: snapshotOf({
    state: "challenge_required", reason: "challenge", expires_at: null, actions: ["login"],
  }) });
  assert.equal(byId["campus-connection-reason"].hidden, false, "挑战态显示细分文案");
  assert.equal(byId["campus-connection-reason"].textContent, "需要在复旦页面完成安全验证：请登录并按页面提示完成验证。", "挑战专属文案");
  assert.equal(primaryAction(), "login", "挑战走受控登录入口");
  assert.equal(primaryLabel(), "打开验证", "挑战动作标签说清“打开验证”");
  actionButtons()[0].click();
  assert.equal(byId["login-dialog"].open, true, "打开验证打开既有受控登录窗");
  byId["cancel-login"].click();

  await setAuth({ ...checkingAuth, code: "fudan_account_locked", connection: snapshotOf({
    state: "login_required", reason: "credentials_rejected", expires_at: null, actions: ["open-settings"],
  }) });
  assert.equal(byId["campus-connection-reason"].textContent, "账号已被锁定或冻结：请先在复旦账号服务解除锁定，再回来登录。", "锁定专属文案");
  assert.equal(primaryAction(), "open-settings", "锁定唯一动作是打开设置");
  assert.equal(primaryLabel(), "打开设置", "锁定动作标签");

  await setAuth({ ...checkingAuth, code: "fudan_service_maintenance", connection: snapshotOf({
    state: "network_unavailable", reason: "service_unavailable", expires_at: null, actions: ["retry"],
  }) });
  assert.equal(byId["campus-connection-reason"].textContent, "校园服务暂时维护：请稍后重试。", "维护专属文案");
  assert.equal(primaryAction(), "retry", "维护唯一动作是稍后重试");
  assert.equal(primaryLabel(), "重试", "维护动作标签");
  assert.equal(primaryLabel().includes("TUN"), false, "维护态绝不出现 TUN 表述");
  assert.equal(byId["campus-connection"].dataset.state, "action_required", "分态边框映射：network_unavailable→action_required");

  await setAuth({ ...checkingAuth, code: "fudan_credentials_rejected", connection: snapshotOf({
    state: "login_required", reason: "credentials_rejected", expires_at: null, actions: ["login"],
  }) });
  assert.equal(byId["campus-connection-reason"].textContent, "账号或密码不正确：请核对后重新登录。", "凭据拒绝专属文案");
  assert.equal(primaryAction(), "login", "凭据拒绝动作是登录");
  await setAuth({ state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"] });
  assert.equal(byId["campus-connection-reason"].hidden, true, "健康态无原因行（安静）");
  assert.equal(byId["campus-connection"].dataset.state, "ready", "健康态边框映射归位");

  /* 16) P1-D 按需诊断抽屉：只在点击时探测；抽屉随检查展开；闭集字段；健康轮询零探测；失败诚实 */
  campusDiagnosticsPayload = {
    schema: "courselens.campus-diagnostics.v1", checked_at: now - 5, mode: "auto",
    services: {
      webvpn: { route: "proxy", direct_ok: false, proxy_ok: true, fallback_used: true, latency_band: "normal" },
      icourse: { route: "direct", direct_ok: true, proxy_ok: true, fallback_used: false, latency_band: "fast" },
    },
    state: "degraded", next_action: "check-network",
  };
  const diagBefore = calls.campusDiagGet || 0;
  byId["campus-diagnostics-run"].click();
  await settle();
  assert.equal(calls.campusDiagGet || 0, diagBefore + 1, "只在点击时探测一次");
  assert.equal(byId["campus-diagnostics"].hidden, false, "检查后抽屉展开（CAMPUS-P3-16：hidden 必须摘除）");
  assert.equal(byId["campus-diagnostics-summary"].textContent, "WebVPN 本机代理 · 课程平台 直连", "闭集路由标签");
  assert.equal(byId["campus-diagnostics-latency"].textContent, "WebVPN 正常 · 课程平台 很快", "粗粒度延迟档");
  assert.equal(byId["campus-diagnostics-fallback"].textContent, "直连失败后备用路径成功", "回退结论");
  assert.equal(byId["campus-diagnostics-action"].textContent, "检查网络", "唯一建议动作");
  assert.notEqual(byId["campus-diagnostics-checked"].textContent, "尚未检查", "显示上次检查时间");
  const diagText = [byId["campus-diagnostics-summary"], byId["campus-diagnostics-latency"], byId["campus-diagnostics-fallback"], byId["campus-diagnostics-action"]]
    .map((node) => node.textContent).join("|");
  assert.equal(/https?:|127\.0\.0\.1|@|cookie|token|lck/i.test(diagText), false, "诊断不含地址/凭据/账号形态");
  await setAuth({ state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"] });
  await setAuth({ state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"] });
  assert.equal(calls.campusDiagGet || 0, diagBefore + 1, "健康轮询零探测（安静）");

  campusDiagnosticsFail = true;
  byId["campus-diagnostics-run"].click();
  await settle();
  assert.equal(byId["campus-diagnostics-summary"].textContent, "检查未完成，请稍后重试。", "诊断失败诚实兜底");
  assert.equal(byId["campus-diagnostics"].hidden, false, "失败证据同样保留在抽屉中");
  campusDiagnosticsFail = false;
  campusDiagnosticsPayload = null;
}

/* BUGFIX-UI-CONSOLIDATION-1：设置页自动化管理板块退场；自动材料运行记录并入
   任务抽屉统一呈现（消费既有 GET /api/v3/automation 快照证据，由应用级 SSE
   合并刷新驱动；未开启且无运行记录时整段隐藏）。 */

async function verifyAutomationRunsInDrawer() {
  const { readFileSync } = await import("node:fs");
  const settingsSource = familySource("settings");
  assert.ok(!settingsSource.includes("runAutomationAction") && !settingsSource.includes("AUTOMATION_STATE_TEXT"),
    "设置页不再有自动化管理卡与动作代码");
  const runsTier = () => deepElements(byId["task-list"])
    .find((node) => String(node.className || "").split(/\s+/).includes("automation-runs-tier"));
  const tierNodesText = (tier) => deepElements(tier).map((node) => String(node.textContent || "")).join("\n");
  const fullRefresh = async () => {
    FakeEventSource.all[0].emit("tasks");
    await settle();
  };

  /* 未开启且无运行记录：整段隐藏（安静抽屉，不渲染空板块） */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], runs: [], imports: [], actions: [],
  };
  await fullRefresh();
  assert.ok(!runsTier(), "未开启且无运行记录时自动材料段隐藏");

  /* 开启 + 运行记录：统一任务记录呈现（类型/状态徽标/闭集计数/闭集结果码） */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, account_id: "2026001",
    rules: [{ course_id: "36941", baseline_count: 3 }], selected_courses: 1,
    runs: [
      { workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: { discovered: 5, processed: 2, deferred: 1 }, observed_at: 1, updated_at: 1 },
      { workflow: "cloud-daily.yml", trigger_kind: "manual", state: "failed", conclusion: "failure", error_code: "deepseek_key_missing", counts: { processed: 0 }, observed_at: 2, updated_at: 2 },
    ],
    imports: [], actions: [],
  };
  await fullRefresh();
  const tier = runsTier();
  assert.ok(tier, "自动材料运行记录并入任务抽屉");
  const tierText = tierNodesText(tier);
  assert.ok(tierText.includes("字幕 ASR · 课件 OCR · AI 总结与章节"), "固定包事实一行如实展示");
  assert.ok(tierText.includes("自动材料 · 计划窗口"), "计划窗口运行为统一任务记录");
  assert.ok(tierText.includes("自动材料 · 手动检查"), "手动检查运行为统一任务记录");
  assert.ok(tierText.includes("生成 2 · 延后 1"), "闭集计数投影");
  assert.ok(tierText.includes("需要处理"), "失败结果码走闭集映射");
  const runRows = deepElements(tier).filter((node) => String(node.className || "").split(/\s+/).includes("automation-run"));
  assert.equal(runRows.length, 2, "每条运行一张任务记录卡");
  const failedRow = runRows.find((node) => node.dataset.state === "failed");
  assert.ok(failedRow, "失败运行呈现失败状态");

  /* 回到未开启且无记录：板块随之移除 */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], runs: [], imports: [], actions: [],
  };
  await fullRefresh();
  assert.ok(!runsTier(), "回到未开启且无记录后自动材料段移除");

  /* ---- CLOUD-CONSENT-AUTO-1 U3：运行记录三修 + 终态删除 ---- */
  const nowSec = Math.floor(Date.now() / 1000);
  const toastText = () => byId["toast-region"].children.map((node) => String(node.textContent || "")).join("\n");
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, rules: [{ course_id: "c1" }], imports: [], actions: [],
    runs: [
      /* ① 终态：按 run 自身结束时刻显示；observed_at 只是刷新时刻 */
      { run_key: "k-done", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: { processed: 1 }, started_at: nowSec - 7200, concluded_at: nowSec - 3600, observed_at: nowSec - 10, updated_at: nowSec - 10 },
      /* ② 真在飞：按 run 自身开始时刻显示 */
      { run_key: "k-live", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "in_progress", conclusion: "", error_code: "", counts: {}, started_at: nowSec - 300, concluded_at: 0, observed_at: nowSec - 5, updated_at: nowSec - 5 },
      /* ② 后端收敛的 ghost：明确终态「已中止」（无 run 时刻 → 不显示时间） */
      { run_key: "k-ghost", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "abandoned", error_code: "run_abandoned", counts: {}, started_at: 0, concluded_at: 0, observed_at: nowSec - 400000, updated_at: nowSec - 400000 },
      /* ② 远端非 success 结论一律终态（不再冒充进行中） */
      { run_key: "k-cancelled", workflow: "cloud-daily.yml", trigger_kind: "manual", state: "completed", conclusion: "cancelled", error_code: "", counts: {}, started_at: nowSec - 90000, concluded_at: nowSec - 80000, observed_at: nowSec - 80000, updated_at: nowSec - 80000 },
      /* ① 历史行没有任何 run 时刻：显示空（绝不拿刷新时刻冒充） */
      { run_key: "k-legacy", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: {}, started_at: 0, concluded_at: 0, observed_at: nowSec - 400000, updated_at: nowSec - 400000 },
    ],
  };
  await fullRefresh();
  const u3Tier = runsTier();
  const u3Text = tierNodesText(u3Tier);
  assert.equal((u3Text.match(/结束于/g) || []).length, 2, "只带 concluded_at 的终态卡显示结束时刻");
  assert.equal((u3Text.match(/开始于/g) || []).length, 1, "在飞卡只显示开始时刻");
  const refreshStamp = new Date((nowSec - 10) * 1000).toLocaleString("zh-CN", { hour12: false });
  assert.equal(u3Text.includes(refreshStamp), false, "刷新时刻（observed_at）不冒充运行时间");
  assert.ok(u3Text.includes("已中止"), "收敛的 ghost 显示明确终态「已中止」");
  assert.ok(u3Text.includes("长时间没有回音"), "收敛原因讲人话（非技术责备）");
  assert.ok(u3Text.includes("未完成"), "cancelled 等远端结论判终态");
  const u3Rows = deepElements(u3Tier).filter((node) => String(node.className || "").split(/\s+/).includes("automation-run"));
  const ghostRow = u3Rows.find((node) => tierNodesText(node).includes("已中止"));
  assert.equal(String(ghostRow.dataset.state), "failed", "ghost 卡呈现终态样式");

  /* U3c（真浏览器走查发现）：本地任务为空时运行段也必须重放——空态短路曾把
     整段抹掉，空任务装机下 30s 任务轮询/SSE 刷新每拍抹一次记录 */
  taskPayload = { tasks: [], counts: { active: 0, failed: 0, completed: 0 } };
  await fullRefresh();
  assert.ok(runsTier(), "本地任务为空：运行记录段仍在（空态短路不再抹掉它）");
  assert.ok(tierNodesText(runsTier()).includes("自动材料 · 计划窗口"), "空态与运行段并存");

  /* ① 收敛记录只有开始时刻：标签必须跟着时间戳走（真浏览器走查发现：此前把
     开始时刻标成了「结束于」） */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, rules: [{ course_id: "c1" }], imports: [], actions: [],
    runs: [{ run_key: "k-abandoned", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "abandoned", error_code: "run_abandoned", counts: {}, started_at: nowSec - 400, concluded_at: 0, observed_at: nowSec - 400000, updated_at: nowSec - 400000 }],
  };
  await fullRefresh();
  const abandonedText = tierNodesText(runsTier());
  assert.ok(abandonedText.includes("开始于"), "只有开始时刻的收敛卡标「开始于」");
  assert.equal(abandonedText.includes("结束于"), false, "不得把开始时刻标成结束时刻");

  /* ③ 陈旧度取记录的结束面：concluded_at → observed_at → started_at（started_at
     只是最后兜底，较新的开始时刻不能把 7 天旧账抵赖成新鲜） */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], imports: [], actions: [],
    runs: [{ run_key: "k-stale", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "abandoned", error_code: "run_abandoned", counts: {}, started_at: nowSec - 300, concluded_at: 0, observed_at: nowSec - 9 * 86400, updated_at: nowSec - 9 * 86400 }],
  };
  await fullRefresh();
  assert.ok(!runsTier(), "终态且最后证据超 7 天：整段隐藏");

  /* ③ 无启用规则且全部记录终态超 7 天：整段隐藏；有启用规则则保留 */
  const staleRun = { run_key: "k-old", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: {}, started_at: nowSec - 9 * 86400, concluded_at: nowSec - 9 * 86400, observed_at: nowSec - 9 * 86400, updated_at: nowSec - 9 * 86400 };
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], imports: [], actions: [], runs: [staleRun],
  };
  await fullRefresh();
  assert.ok(!runsTier(), "无启用规则且全部终态超 7 天：整段隐藏");
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, rules: [{ course_id: "c1" }], imports: [], actions: [], runs: [staleRun],
  };
  await fullRefresh();
  assert.ok(runsTier(), "有启用规则时旧记录仍可见（规则 opt-in 未被连带）");

  /* ③ 终态记录两击删除：只删记录，不触已生成产物 */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, rules: [{ course_id: "c1" }], imports: [], actions: [],
    runs: [{ run_key: "k-done", workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: {}, started_at: nowSec - 7200, concluded_at: nowSec - 3600, observed_at: nowSec - 10, updated_at: nowSec - 10 }],
  };
  await fullRefresh();
  const deleteButton = deepElements(runsTier()).find(
    (node) => node.tagName === "BUTTON" && String(node.textContent || "") === "删除记录",
  );
  assert.ok(deleteButton, "终态卡带「删除记录」入口");
  deleteButton.click();
  await settle();
  assert.equal(calls.automationRunDeletePost.length, 0, "首击只亮确认不发请求");
  assert.equal(deleteButton.textContent, "再点一次，删除这条记录", "首击换确认文案");
  deleteButton.click();
  await settle();
  assert.deepEqual(calls.automationRunDeletePost[0], { run_key: "k-done" }, "第二击发闭集路由并携带 run_key");
  assert.ok(toastText().includes("已生成的字幕和文件不受影响"), "人话确认：只删记录不触产物");

  /* 回到未开启且无记录：板块随之移除 */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], runs: [], imports: [], actions: [],
  };
  await fullRefresh();
  assert.ok(!runsTier(), "收尾回到安静态：整段隐藏");

  console.log("ok: 设置页自动化板块退场；自动材料运行记录并入任务抽屉（统一任务呈现/闭集映射/安静隐藏/run 自身时刻/ghost 收敛/7 天隐藏/终态删除）");
}

/* ---- INIT-PATH-POLISH-1 单元D：收紧等待对称 + 完成总结态 ---- */

async function verifyTightenWaitAndCompletionSummary() {
  const reload = async () => {
    selectPage("settings");
    await settle();
  };
  const primaryAnchor = () => byId["remote-primary-action"].children.find((node) => node.tagName === "A");
  const toastTexts = () => byId["toast-region"].children.map((node) => String(node.textContent || ""));
  const regionText = (id) => document.getElementById(id).children
    .map((node) => String(node.textContent || "")).join("\n");
  const realNow = Date.now;
  let fakeNow = realNow.call(Date);
  Date.now = () => fakeNow;
  const tickWait = async () => {
    const entries = [...timeouts.entries()].filter(([key]) => timeoutDelays.get(key) === 3000);
    fakeNow += 3000;
    for (const [key, fn] of entries) {
      timeouts.delete(key);
      timeoutDelays.delete(key);
      fn();
    }
    await settle();
  };
  const summaryBlocks = () => deepElements(byId["remote-evidence"]).filter(
    (node) => String(node.dataset?.role || "") === "completion-summary",
  );

  try {
    /* 前序场景可能遗留等待窗口：按伪时钟推到过期并排水，从干净基线开始 */
    fakeNow += 360000;
    await tickWait();
    await settle();

    /* 场景一：已安装但范围暂宽 + 两仓已建 → 主按钮带安装设置链接（范围调整页）。
       点出锚点：对称进入「等待收紧…」（3s 有界轮询、不发动作请求）；
       等待拍与聚焦重探走 ?fresh=1（单元A 同款新鲜语义） */
    remoteSnapshot = {
      overall: { state: "action_required", code: "installation_scope_not_exact" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        {
          component: "installation", state: "action_required", code: "installation_scope_not_exact",
          actions: ["restrict-github-app-installation"], stale: false,
          evidence: { settings_url: "https://github.com/settings/installations/42" },
        },
        { component: "worker_repository", state: "action_required", code: "worker_repository_awaiting_installation", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_awaiting_installation", stale: false },
      ],
    };
    remoteActionResponse = null;
    await reload();
    const settingsLink = primaryAnchor();
    assert.notEqual(settingsLink, null, "范围暂宽+两仓已建：主按钮带安装设置链接");
    let postsBefore = calls.remoteActionPost.length;
    settingsLink.click();
    await settle();
    assert.equal(byId["remote-tighten-wait"].hidden, false, "点出范围调整页后进入等待收紧态");
    assert.ok(
      byId["remote-tighten-wait"].textContent.includes("等待你在 GitHub 收紧安装范围"),
      `收紧等待行明示等待语义：${byId["remote-tighten-wait"].textContent}`,
    );
    assert.equal(calls.remoteActionPost.length, postsBefore, "进入收紧等待不发动作请求");
    assert.ok([...timeoutDelays.values()].includes(3000), "3 秒有界轮询已排上");
    let freshBefore = calls.remoteGetFresh;
    await tickWait();
    assert.ok(calls.remoteGetFresh > freshBefore, "收紧等待拍走新鲜语义（?fresh=1）");
    window.dispatchEvent(new Event("focus"));
    await settle();
    assert.ok(calls.remoteGetFresh > freshBefore, "聚焦重探同样走新鲜语义");

    /* 等待拍读到范围已精确：自述（toast+状态行同文恰一次）+ 自动推进
       bootstrap 恰一次，消灭最后一次手动诊断点击 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "worker_setup_incomplete" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "action_required", code: "worker_repository_missing", stale: false },
        { component: "mailbox_repository", state: "action_required", code: "mailbox_repository_missing", stale: false },
      ],
    };
    byId["toast-region"].replaceChildren(); /* 上限语义：锚点清零，断言只看本段 */
    const toastsBefore = 0;
    await tickWait();
    assert.equal(byId["remote-tighten-wait"].hidden, false, "自动推进时状态行自述在场");
    assert.ok(
      byId["remote-tighten-wait"].textContent.includes("检测到安装范围已收紧"),
      `自述行明示自动继续：${byId["remote-tighten-wait"].textContent}`,
    );
    assert.equal(calls.remoteActionPost.at(-2)?.action, "bootstrap", "自动推进主按钮到初始化");
    assert.equal(calls.remoteActionPost.at(-1)?.action, "diagnose", "bootstrap 保留恰一次自动对账");
    const advanceToasts = toastTexts().slice(toastsBefore);
    assert.equal(advanceToasts.length, 1, `自动推进通知恰一次：${JSON.stringify(advanceToasts)}`);
    assert.ok(advanceToasts[0].includes("检测到安装范围已收紧"), "推进通知为闭集自述文案");
    const bootstrapPosts = calls.remoteActionPost.filter((item) => item.action === "bootstrap").length;
    await tickWait();
    await tickWait();
    assert.equal(
      calls.remoteActionPost.filter((item) => item.action === "bootstrap").length,
      bootstrapPosts,
      "自动推进恰一次：后续轮询拍不重复提交",
    );
    await drainAsyncFeedback();
    await reload();
    assert.equal(byId["remote-tighten-wait"].hidden, true, "异步动作收口后自述行归档隐藏");

    /* 场景二：完成总结态——红→绿沿一次性登场，确认后归档，不重复打扰；
       断开再达成才重新登场 */
    remoteSnapshot = {
      overall: { state: "action_required", code: "channel_test_required" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
        { component: "channel_test", state: "action_required", code: "channel_test_required", stale: false },
      ],
    };
    await reload();
    assert.equal(summaryBlocks().length, 0, "未达成时无总结块");
    remoteSnapshot = {
      overall: { state: "ready", code: "ready_for_dispatch" },
      components: [
        { component: "authorization", state: "ready", code: "authorization_valid", stale: false },
        { component: "installation", state: "ready", code: "installation_present", stale: false },
        { component: "worker_repository", state: "ready", code: "worker_repository_ready", stale: false },
        { component: "mailbox_repository", state: "ready", code: "mailbox_repository_ready", stale: false },
        { component: "channel_test", state: "ready", code: "channel_test_valid", stale: false },
      ],
    };
    await reload();
    assert.equal(summaryBlocks().length, 1, "全绿沿：总结块恰一个登场");
    /* FakeElement.textContent 不聚合：逐层深查文案 */
    const summaryText = deepElements(summaryBlocks()[0])
      .map((node) => String(node.textContent || "")).join("\n");
    assert.ok(summaryText.includes("两个专属仓库已就绪，加密通道已验证。"), "总结块闭集成果文案");
    assert.ok(summaryText.includes("现在回到课程页即可开始学习。"), "总结块下一步指引");
    await reload();
    assert.equal(summaryBlocks().length, 1, "持续全绿：确认前常驻，不重复叠加");
    const summaryButtons = deepElements(summaryBlocks()[0]).filter((node) => node.tagName === "BUTTON");
    assert.deepEqual(
      summaryButtons.map((node) => node.textContent),
      ["知道了"],
      "完成态只剩确认归档（云算力次级入口已随开关退役）",
    );
    summaryButtons[0].click();
    await settle();
    assert.equal(summaryBlocks().length, 0, "确认后归档为常规连接卡");
    await reload();
    assert.equal(summaryBlocks().length, 0, "确认后持续全绿不再打扰");
    remoteSnapshot = {
      overall: { state: "action_required", code: "channel_test_required" },
      components: [
        { component: "channel_test", state: "action_required", code: "channel_test_required", stale: false },
      ],
    };
    await reload();
    remoteSnapshot = {
      overall: { state: "ready", code: "ready_for_dispatch" },
      components: [
        { component: "channel_test", state: "ready", code: "channel_test_valid", stale: false },
      ],
      /* 状态行可见：只读陈述，不提供任何入口 */
      remote_compute: { configured: true, verified: true },
    };
    await reload();
    assert.equal(summaryBlocks().length, 1, "断开再达成：红→绿沿重新登场恰一个");
    const reentryButtons = deepElements(summaryBlocks()[0]).filter((node) => node.tagName === "BUTTON");
    /* CLOUD-CONSENT-AUTO-1 U2：次级入口退役——总结态只剩「知道了」，点击即归档 */
    assert.deepEqual(
      reentryButtons.map((node) => String(node.textContent || "")),
      ["知道了"],
      "重登场总结只带「知道了」（云算力入口已随开关退役）",
    );
    reentryButtons[0].click();
    await settle();
    assert.equal(summaryBlocks().length, 0, "确认后归档：干净状态交给后续场景");
  } finally {
    Date.now = realNow;
    remoteSnapshot = { overall: { state: "ready", code: "remote_verified" }, components: [] };
    remoteActionResponse = null;
    await reload();
  }
}

await verifyTasksDrawer();
await verifyTasksDrawerGateEnvelopeGuard();
await verifyTaskIdentityAndCollapse();
await verifyWordmarkReturn();
await verifyTranscriptAriaContextLabels();
await verifyBookmarkEvidenceUnavailableToast();
await verifyPrivacyAnalyticsAction();
await verifyPasswordToggle();
await verifyLoginSubmitFlow();
await verifyLoginSavedAccountSelector();
await verifyDeepseekSaveState();
await verifyMailboxReconcileCard();
await verifyGitHubConnectionRecovery();
await verifyAsyncActionFeedbackAndRotateKeys();
await verifyInstallCompletionDetection();
await verifyFirstRunChain();
await verifyStageMachineSingleSourcing();
await verifySnapshotSurvivesTransientFailure();
await verifyActionProgressAndSelfHeal();
await verifyTightenWaitAndCompletionSummary();
await verifyConnectionCardsAutoConnectAndFocus();
await verifyRemoteComputeStatus();
await verifyCloudConnHeaderState();
await verifyCloudRevokePrivacyRow();
await verifyToastStackCap();
await verifyLoginDialogAutoConnectOption();
await verifyAutomationRunsInDrawer();
await verifyAutomationMasterSwitchStatusLine();
await verifyLiveDeskMode();
await verifyMaxDeepseekTokensPanel();
await verifyTaskUsageAndBalanceLines();
await verifyAccountMenuReachableWhenLoggedOut();
await verifyTopbarPopoverOutsideDismiss();
await verifyCampusConnectionCard();
await verifyGithubConnTextThreeStates();
await verifyN6fP3Closures();

/* ---- 会话生命周期：close 所有权在 installShell + sendBeacon 兜底（执行真实 installShell/sendSession） ---- */

const beaconCalls = [];
const sessionFetchBodies = [];
const sessionFetchOptions = [];
const beaconBodyText = async (entry) => (typeof entry.body === "string" ? entry.body : await entry.body);
const beaconEntry = (url, blob) => ({ url, body: blob && typeof blob.text === "function" ? blob.text() : String(blob ?? "") });
const innerFetch = globalThis.fetch;
globalThis.fetch = async (path, options = {}) => {
  if (String(path) === "/api/v3/frontend-session" && String(options.method || "GET") === "POST") {
    sessionFetchBodies.push(String(options.body ?? ""));
    sessionFetchOptions.push(options);
  }
  return innerFetch(path, options);
};
const setBeacon = (impl) => {
  Object.defineProperty(globalThis, "navigator", {
    value: { onLine: true, sendBeacon: impl },
    configurable: true,
  });
};
setBeacon((url, blob) => {
  beaconCalls.push(beaconEntry(url, blob));
  return true;
});
const sessionIntervalCount = () => [...intervals.values()].filter((timer) => timer.ms === 15000).length;
const heartbeatIntervalCount = () => [...intervals.values()].filter((timer) => timer.ms === 100000).length;
const closeBody = async () => JSON.parse(await beaconBodyText(beaconCalls[beaconCalls.length - 1]));

await verifyShellSessionLifecycle();

async function verifyShellSessionLifecycle() {
  /* 1) beacon 排队成功：pagehide 恰好一次 close、无 fetch 兜底、心跳停止、重复触发幂等 */
  let baseBeacon = beaconCalls.length;
  let baseSessionFetch = sessionFetchBodies.length;
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, baseBeacon + 1, "pagehide 恰好一次 beacon close");
  assert.equal(sessionFetchBodies.length, baseSessionFetch, "beacon 成功时不走 fetch 兜底");
  assert.equal((await closeBody()).action, "close", "close 动作正确");
  assert.notEqual((await closeBody()).session_id, "", "close 携带 session id");
  assert.equal(sessionIntervalCount(), 0, "close 后 15s 心跳/鉴权定时器全部停止");
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, baseBeacon + 1, "重复 pagehide 不重复 close");

  /* 2) sendBeacon=false：cleanup 走一次 keepalive fetch 兜底，body 与 beacon 一致；cleanup 幂等 */
  setBeacon((url, blob) => {
    beaconCalls.push(beaconEntry(url, blob));
    return false;
  });
  let cleanup = await installShell(store);
  await settle(); /* ⑬b：会话链后台接力后定时器才安装 */
  /* BACKEND-DEATH-1②：心跳拍点移入 Worker（Node 无 Worker → 页面定时器回退 100s），
     15s 族只剩 auth 轮询；100s 族恰为心跳回退定时器。 */
  assert.equal(sessionIntervalCount(), 1, "新会话安装 auth 15s 轮询一个");
  assert.equal(heartbeatIntervalCount(), 1, "Worker 不可用时心跳回退为 100s 页面定时器");
  await tickIntervals(15000);
  const heartbeatsBefore = sessionFetchBodies.length;
  const beaconBefore = beaconCalls.length;
  cleanup();
  await settle();
  assert.equal(beaconCalls.length, beaconBefore + 1, "cleanup 尝试一次 beacon");
  assert.equal(sessionFetchBodies.length, heartbeatsBefore + 1, "beacon=false 恰好一次 fetch 兜底");
  assert.equal(
    sessionFetchBodies[sessionFetchBodies.length - 1],
    await beaconBodyText(beaconCalls[beaconCalls.length - 1]),
    "兜底 body 与 beacon body 完全一致（同一 session id、action=close）"
  );
  assert.equal(JSON.parse(sessionFetchBodies[sessionFetchBodies.length - 1]).action, "close");
  assert.equal(sessionFetchOptions[sessionFetchOptions.length - 1].keepalive, true, "兜底使用 keepalive fetch");
  assert.equal(sessionFetchOptions[sessionFetchOptions.length - 1].credentials, "same-origin");
  assert.equal(sessionIntervalCount(), 0, "close 后定时器停止");
  const beaconAfterIdempotent = beaconCalls.length;
  const fetchAfterIdempotent = sessionFetchBodies.length;
  cleanup();
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, beaconAfterIdempotent, "cleanup+pagehide 组合只 close 一次");
  assert.equal(sessionFetchBodies.length, fetchAfterIdempotent, "cleanup+pagehide 组合不重复兜底");

  /* 3) sendBeacon 抛错：同样一次兜底，不向调用方抛异常 */
  setBeacon(() => {
    throw new Error("synthetic beacon boom");
  });
  cleanup = await installShell(store);
  await settle(); /* ⑬b：会话链后台接力后定时器才安装 */
  const fetchBeforeThrow = sessionFetchBodies.length;
  cleanup();
  await settle();
  assert.equal(sessionFetchBodies.length, fetchBeforeThrow + 1, "beacon 抛错仍恰好一次兜底");
  assert.equal(JSON.parse(sessionFetchBodies[sessionFetchBodies.length - 1]).action, "close");

  /* 4) installer 未完成窗口：pagehide 独立关闭已登记 session，cleanup 再执行不重复 */
  setBeacon((url, blob) => {
    beaconCalls.push(beaconEntry(url, blob));
    return true;
  });
  cleanup = await installShell(store);
  await settle(); /* ⑬b：会话链后台接力后定时器才安装 */
  const beaconBeforePageHide = beaconCalls.length;
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, beaconBeforePageHide + 1, "pagehide 关闭已登记 session");
  cleanup();
  await settle();
  assert.equal(beaconCalls.length, beaconBeforePageHide + 1, "pagehide 后 cleanup 不重复 close");

  /* 4b) 真实 pending-installer 窗口：refreshAuth 在途时 pagehide 落地，
     close 后不得再安装 auth 15s 轮询（孤儿定时器回归锁） */
  const innerForDeferred = globalThis.fetch;
  const releaseAuth = deferred();
  globalThis.fetch = async (path, options = {}) => {
    if (String(path) === "/api/v3/authentication") {
      await releaseAuth.promise;
      return ok(authSnapshot);
    }
    return innerForDeferred(path, options);
  };
  const deferredInstall = installShell(store);
  await settle();
  const beaconBeforeDeferredHide = beaconCalls.length;
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, beaconBeforeDeferredHide + 1, "refreshAuth 在途时 pagehide 立即 close");
  releaseAuth.resolve();
  const deferredCleanup = await deferredInstall;
  await settle();
  assert.equal(sessionIntervalCount(), 0, "close 后不安装孤儿 auth 轮询");
  deferredCleanup();
  await settle();
  globalThis.fetch = innerForDeferred;

  /* 5) open 请求在途时 pagehide 先到：必须等 open 落地后再 close，不能让晚到的
     open 在服务端复活 session。 */
  const innerForDeferredOpen = globalThis.fetch;
  const releaseOpen = deferred();
  globalThis.fetch = async (path, options = {}) => {
    if (
      String(path) === "/api/v3/frontend-session"
      && String(options.method || "GET") === "POST"
      && JSON.parse(String(options.body || "{}")).action === "open"
    ) {
      await releaseOpen.promise;
    }
    return innerForDeferredOpen(path, options);
  };
  const pendingOpenInstall = installShell(store);
  await settle();
  const beaconBeforePendingOpenHide = beaconCalls.length;
  windowTarget.dispatchEvent(new Event("pagehide"));
  await settle();
  assert.equal(beaconCalls.length, beaconBeforePendingOpenHide, "open 在途时 close 等待排序");
  releaseOpen.resolve();
  const pendingOpenCleanup = await pendingOpenInstall;
  await settle();
  assert.equal(beaconCalls.length, beaconBeforePendingOpenHide + 1, "open 落地后恰好补一次 close");
  assert.equal((await closeBody()).action, "close", "排序补偿仍发送 close");
  assert.equal(sessionIntervalCount(), 0, "排序补偿后不留下定时器");
  pendingOpenCleanup();
  globalThis.fetch = innerForDeferredOpen;

  /* 6) BFCache 挂起（persisted=true）：不关闭，heartbeat 继续；正常 cleanup 仍收口 */
  cleanup = await installShell(store);
  await settle(); /* ⑬b：会话链后台接力后定时器才安装 */
  const persistedHide = new Event("pagehide");
  persistedHide.persisted = true;
  windowTarget.dispatchEvent(persistedHide);
  await settle();
  assert.equal(sessionIntervalCount(), 1, "persisted pagehide 不拆除 auth 轮询");
  assert.equal(heartbeatIntervalCount(), 1, "persisted pagehide 不拆除心跳回退定时器");
  const heartbeatsAfterPersisted = sessionFetchBodies.length;
  await tickIntervals(100000);
  assert.equal(sessionFetchBodies.length, heartbeatsAfterPersisted + 1, "persisted 后 heartbeat 继续");
  const beaconBeforeCleanup = beaconCalls.length;
  cleanup();
  await settle();
  assert.equal(beaconCalls.length, beaconBeforeCleanup + 1, "persisted 后正常 cleanup 仍 close 一次");
  assert.equal(sessionIntervalCount(), 0, "cleanup 后定时器收口");
}

/* ---- N) FEATURE-FRONTEND-UPDATE-DATA-1：顶栏更新小组件 + 数据管理页 ---- */
{
  const toggle = byId["update-toggle"];
  const updateSnapshotBase = {
    current_version: "1.0.0", channel: "stable", package_size: 2048,
    last_checked_at: 10, release_notes: "", error_code: "", actions: [],
  };

  /* 1) 安装期 ambient 快照（idle）→ 顶栏隐藏；widget 已作为唯一轮询者发布 store `update` */
  await settle();
  assert.equal(toggle.hidden, true, "ambient idle 快照不点亮顶栏");
  assert.equal(store.update?.state, "idle", "widget 唯一轮询者已发布快照");

  /* 2) available：可见 + 版本 aria-label + 设置卡订阅渲染；单击 fire 编排动作 */
  clientUpdateSnapshot = { ...updateSnapshotBase, state: "available", available_version: "1.1.0", actions: ["update_now"] };
  window.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, false, "available 进入顶栏");
  assert.equal(toggle.getAttribute("aria-label"), "发现新版本 1.1.0，点击开始更新");
  assert.equal(toggle.disabled, false, "available 可点击");
  assert.ok(byId["update-state"].textContent.includes("发现更新"), "设置卡订阅同词表渲染");

  clientUpdateActionResponse = { ...updateSnapshotBase, state: "downloading", available_version: "1.1.0" };
  toggle.click();
  await settle();
  assert.equal(clientUpdateActionPost.length, 1, "编排动作恰好提交一次");
  assert.deepEqual(clientUpdateActionPost[0], { action: "update_now", confirmed: true }, "update_now+confirmed 闭集体");
  assert.equal(toggle.disabled, true, "busy 全程 disabled");
  assert.equal(toggle.getAttribute("aria-busy"), "true", "busy aria-busy");
  clientUpdateSequence = [
    { ...updateSnapshotBase, state: "verifying" },
    { ...updateSnapshotBase, state: "applying" },
    { ...updateSnapshotBase, state: "ready_to_restart" },
  ];
  await tickIntervals(2000);
  await tickIntervals(2000);
  await tickIntervals(2000);
  assert.equal(store.update?.state, "ready_to_restart", "≤2s 轮询推进到等待重启");
  assert.equal(toggle.disabled, true, "ready_to_restart disabled（重启由 launcher 收口）");
  assert.equal(toggle.getAttribute("aria-busy"), null, "ready_to_restart 不再 aria-busy");
  assert.ok(
    byId["update-recovery"].textContent.includes("健康确认"),
    "设置卡恢复指引随订阅更新",
  );

  /* 3) 用户激活过的操作失败 → failed 可见（armed），单击跳设置更新组 */
  clientUpdateSequence = null;
  clientUpdateSnapshot = { ...updateSnapshotBase, state: "failed", error_code: "download_http_error" };
  window.dispatchEvent(new Event("courselens:update-refresh"));
  await settle();
  assert.equal(toggle.hidden, false, "用户激活后的失败可见（E1 armed）");
  assert.equal(toggle.getAttribute("aria-label"), "更新失败，点击进入恢复");
  assert.equal(toggle.disabled, false, "failed 可点击进入恢复");
  assert.ok(byId["update-error"].textContent.includes("稍后重新检查"), "设置卡闭集指引");
  toggle.click();
  await settle();
  assert.equal(byId["settings-page"].hidden, false, "failed 单击跳设置页");
  assert.equal(byId["data-page"].hidden, true, "其余页面隐藏");

  /* 4) 偏好开关：消费 update_background_checks 快照，走 settings 动作闭集 */
  settingsSnapshot = { ...settingsSnapshot, update_background_checks: true };
  await settle();
  assert.equal(byId["update-background-checks"].checked, true, "缺省=开的快照渲染");
  byId["update-background-checks"].checked = false;
  byId["update-background-checks"].dispatchEvent(new Event("change"));
  await settle();
  const preferencePost = [...calls.settingsActionsPost].reverse().find(
    (body) => body.action === "set-update-background-checks",
  );
  assert.deepEqual(preferencePost, { action: "set-update-background-checks", enabled: false }, "偏好动作闭集体");

  /* 4b) MEDIA-VPN-1：媒体流系统代理开关（缺省=关；同走 settings 动作闭集） */
  settingsSnapshot = {
    ...settingsSnapshot,
    media_stream_proxy: { enabled: false, proxy_detected: false, proxy_source: "none" },
  };
  await settle();
  assert.equal(byId["media-stream-proxy"].checked, false, "媒体代理缺省=关的快照渲染");
  byId["media-stream-proxy"].checked = true;
  byId["media-stream-proxy"].dispatchEvent(new Event("change"));
  await settle();
  const mediaProxyPost = [...calls.settingsActionsPost].reverse().find(
    (body) => body.action === "set-media-stream-proxy",
  );
  assert.deepEqual(mediaProxyPost, { action: "set-media-stream-proxy", enabled: true }, "媒体代理动作闭集体");

  /* 4c) WEBVPN-AUTO-1：校外媒体区为全自动静态说明（用户开关已移除；
     运行时三级取流不变，设置动作闭集不再含 set-media-webvpn-relay）。
     静态面钉真实 index.html（台架为 FakeElement 壳，不含静态文案）。 */
  const settingsPageHtml = readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8");
  assert.ok(
    settingsPageHtml.includes("会自动尝试经学校 WebVPN 中转"),
    "校外媒体自动中转静态说明在设置页",
  );
  assert.ok(
    !settingsPageHtml.includes('id="media-webvpn-relay"'),
    "校外媒体开关已从 index.html 移除（全自动）",
  );

  /* 5) 数据管理页：账户菜单唯一入口 → 装载 summary → 窄窗行内明细 */
  courseDataSummary = {
    schema: "courselens.course-data-summary.v1", generated_at: 1700000000,
    byte_basis: "sqlite_file_bytes_including_indexes_and_free_pages",
    database_bytes: { state_db: 1024, learning_db: 2048, total: 3072 },
    orphan_artifacts: { directories: 0, bytes: 0 },
    page: { page: 1, page_size: 200, total: 2 },
    rows: [
      {
        course_id: "c1", in_catalog: true, title: "高等数学", teacher: "张老师",
        lecture_count: 12, file_bytes: {}, total_file_bytes: 1024,
        categories: {
          progress: { count: 5, text_bytes: 100, last_updated_at: 1700000000 },
          transcript: { count: 3, text_bytes: 2000, last_updated_at: 1700000000 },
          ppt: { count: 2, text_bytes: 300, last_updated_at: 1700000000 },
        },
      },
      { course_id: "c-orphan", in_catalog: false, file_bytes: {}, total_file_bytes: 0, categories: {} },
    ],
  };
  courseDataLectures = {
    schema: "courselens.course-data-lecture-page.v1",
    course_id: "c1", in_catalog: true, total: 120,
    page: { limit: 50, offset: 0 },
    lectures: [
      { sub_id: "s1", in_catalog: true, title: "第一讲", date: "2026-09-01", file_bytes: {}, total_file_bytes: 512 },
      { sub_id: "s2", in_catalog: true, title: "第二讲", date: "2026-09-08", file_bytes: {}, total_file_bytes: 512 },
    ],
  };
  byId["account-menu-data"].click();
  await settle();
  assert.equal(byId["data-page"].hidden, false, "账户菜单进入数据管理页");
  const deepText = (node) => {
    const parts = [];
    const walk = (current) => {
      parts.push(String(current.textContent || ""));
      (current.children || []).forEach(walk);
    };
    walk(node);
    return parts.join("|");
  };
  assert.ok(byId["data-summary"].textContent.includes("课程 1"), "摘要条只计在册课程");
  assert.ok(byId["data-evidence"].textContent.includes("统计生成于"), "stale 证据行");
  assert.equal(byId["data-orphans-filter"].hidden, false, "孤儿入口可见");
  const list = byId["data-course-list"];
  assert.ok(list.children.length >= 2, "在册与孤儿行都渲染（Q2 可见）");
  const orphanRow = list.children[1];
  assert.equal(orphanRow.dataset.courseDetail, "c-orphan", "第二行是孤儿课程行");
  assert.ok(deepText(orphanRow).includes("孤儿"), "孤儿行带描边标记");

  /* 6) 窄窗 details 展开明细：类别栅格（Q5 ppt=依赖远端）+ 讲次分页 ≤50 */
  const narrowDetails = list.children[0];
  narrowDetails.open = true;
  narrowDetails.dispatchEvent(new Event("toggle"));
  await settle();
  const detailBody = narrowDetails.children[1];
  const detailText = deepText(detailBody);
  assert.ok(detailText.includes("依赖远端"), "ppt 恒为依赖远端档");
  assert.ok(detailText.includes("第 1 / 3 页"), "讲次分页 120→3 页");
  const pagerButtons = detailBody.querySelectorAll("button");
  const nextButton = pagerButtons.filter((button) => button.textContent === "下一页")[0];
  nextButton.click();
  await settle();
  assert.ok(deepText(detailBody).includes("第 2 / 3 页"), "下一页翻页");

  /* 7) 批量选择 → typed 确认逐课程名精确匹配 → delete-records 回执 */
  const checkboxes = list.querySelectorAll("input");
  checkboxes[0].checked = true;
  checkboxes[0].dispatchEvent(new Event("change"));
  await settle();
  assert.equal(byId["data-bulk-bar"].hidden, false, "有选择时批量条呈现");
  byId["data-action-delete-records"].click();
  await settle();
  assert.equal(byId["data-confirm-dialog"].open, true, "typed 确认 dialog 打开");
  assert.equal(byId["data-confirm-confirm"].disabled, true, "未输入课程名前确认禁用");
  byId["data-confirm-input"].value = "错误名字";
  byId["data-confirm-input"].dispatchEvent(new Event("input"));
  assert.equal(byId["data-confirm-confirm"].disabled, true, "名字不匹配仍禁用");
  byId["data-confirm-input"].value = "高等数学";
  byId["data-confirm-input"].dispatchEvent(new Event("input"));
  assert.equal(byId["data-confirm-confirm"].disabled, false, "精确匹配后启用");
  courseDataActionReceipt = {
    schema: "courselens.course-data-action-result.v1",
    action: "delete-records", operation_id: "op", status: "rejected",
    blockers: [{ code: "active_task", count: 2 }],
  };
  byId["data-confirm-confirm"].click();
  await settle();
  assert.equal(byId["data-confirm-dialog"].open, false, "确认后 dialog 关闭");
  const deletePost = courseDataActionPost[courseDataActionPost.length - 1];
  assert.equal(deletePost.action, "delete-records", "闭集动作提交");
  assert.equal(deletePost.confirm_typed, "高等数学", "typed 课程名字符串回执");
  assert.deepEqual(deletePost.course_ids, ["c1"], "课程定位");
  assert.ok(String(deletePost.operation_id).startsWith("course-data:"), "幂等 operation_id");
  assert.ok(deepText(byId["data-result"]).includes("未执行"), "整批拒绝呈现");
  assert.ok(deepText(byId["data-result"]).includes("有正在进行的生成任务（2）"), "闭集阻塞码+计数");

  /* 8) 接受回执：结果行闭集计数 + toast（经 armDelete 风格的闭集短句） */
  courseDataActionReceipt = {
    schema: "courselens.course-data-action-result.v1",
    action: "rebuild-search", operation_id: "op", status: "accepted",
  };
  byId["data-action-rebuild-search"].disabled = false;
  byId["data-bulk-bar"].hidden = false;
  checkboxes[0].checked = true;
  checkboxes[0].dispatchEvent(new Event("change"));
  await settle();
  byId["data-action-rebuild-search"].click();
  await settle();
  const rebuildPost = courseDataActionPost[courseDataActionPost.length - 1];
  assert.equal(rebuildPost.action, "rebuild-search", "重建索引闭集动作");
  assert.equal(rebuildPost.confirm_typed, undefined, "重建类无 typed 确认");
  assert.ok(deepText(byId["data-result"]).includes("已完成"), "接受回执呈现");
}

/* ---- AUTOLOGIN-LOCAL-FIRST-1③：恢复期动作撞门分流（study 页真实装配路径）----
   恢复期本地可信上下文的动作请求被后端以闭集新码 fudan_session_restoring 拒绝
   （401），前端呈现为瞬态非错误提示「正在登录，请稍候…」（checking 形态）；
   恢复失败后的既有 fudan_login_required 文案链不变（error 形态）。 */

/* api.js 闭集映射：新码映射为中文瞬态提示，错误对象携带闭集 code */
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: "Fudan session is restoring", error_code: "fudan_session_restoring", actions: ["login"],
  }), { status: 401, headers: { "Content-Type": "application/json" } });
  const { apiV3, ApiError } = await import("../frontend/modules/api.js?restoring");
  let caught = null;
  try {
    await apiV3("review-plans/actions");
  } catch (error) {
    caught = error;
  }
  globalThis.fetch = realFetch;
  assert.ok(caught instanceof ApiError, "401 新码抛 ApiError");
  assert.equal(caught.code, "fudan_session_restoring", "闭集 code 透传");
  assert.equal(caught.status, 401);
  assert.equal(caught.message, "正在登录，请稍候…", "映射为瞬态中文提示");
}

/* study 页动作：恢复期新码 → checking 形态瞬态提示；恢复失败既有码 → error 形态既有文案 */
{
  const realFetch = globalThis.fetch;
  store.set("activeCourse", { course_id: "c-restore", title: "恢复课程" });
  store.set("activeLecture", { course_id: "c-restore", sub_id: "l-restore", sub_title: "恢复讲次", can_stream: true });
  const toastNodes = () => byId["toast-region"].children;

  globalThis.fetch = async (path, options = {}) => {
    const route = String(path);
    const method = String(options.method || "GET");
    if (method === "POST" && route === "/api/v3/quizzes") {
      return new Response(JSON.stringify({
        error: "Fudan session is restoring", error_code: "fudan_session_restoring", actions: ["login"],
      }), { status: 401, headers: { "Content-Type": "application/json" } });
    }
    return realFetch(path, options);
  };
  byId["generate-quiz"].click();
  await settle();
  assert.ok(toastNodes().length > 0, "恢复期撞门有反馈");
  const restoringToast = toastNodes().at(-1);
  assert.equal(restoringToast.textContent, "正在登录，请稍候…", "瞬态提示精确文案");
  assert.equal(restoringToast.dataset.state, "checking", "非错误形态（checking 色调）");

  globalThis.fetch = async (path, options = {}) => {
    const route = String(path);
    const method = String(options.method || "GET");
    if (method === "POST" && route === "/api/v3/quizzes") {
      return new Response(JSON.stringify({
        error: "Fudan authentication is required", error_code: "fudan_login_required", actions: ["login"],
      }), { status: 401, headers: { "Content-Type": "application/json" } });
    }
    return realFetch(path, options);
  };
  byId["generate-quiz"].click();
  await settle();
  const failedToast = toastNodes().at(-1);
  assert.equal(failedToast.textContent, "请先登录复旦课程平台", "恢复失败后既有文案链不变");
  assert.equal(failedToast.dataset.state, "error", "既有失败为错误形态");

  store.set("activeLecture", null);
  store.set("activeCourse", null);
  globalThis.fetch = realFetch;
  console.log("ok: 恢复期动作分流——新码瞬态提示/失败既有文案（AUTOLOGIN-LOCAL-FIRST-1③）");
}

/* ---- 第卅三案：自动登录在途窗口内选课入口进等待态 ----
   缓存页 + 点击选课 + 会话在途 → 等待（正在登录…，绝不跳登录）→
   就绪自动续接进入课程目录；明确失败/30s 有界超时才转登录引导。 */
async function verifyCourseSelectWaiting() {
  const openLoginEvents = [];
  const onOpenLogin = () => { openLoginEvents.push(1); };
  windowTarget.addEventListener("courselens:open-login", onOpenLogin);
  const button = byId["study-start-select"];

  /* ①在途窗口：checking + 点击选课 → 等待态（不跳登录） */
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  await settle();
  button.click();
  await settle();
  assert.equal(openLoginEvents.length, 0, "在途点击不跳登录界面");
  assert.equal(button.textContent, "正在登录…", "等待态轻提示");
  assert.equal(button.disabled, true, "等待期按钮禁用防重复");

  /* ②就绪续接：auth 翻 ready → 自动进入课程目录（绝不跳登录页） */
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
  assert.equal(openLoginEvents.length, 0, "自动登录成功绝不跳登录页");
  assert.equal(byId["study-select"].hidden, false, "就绪续接进入课程目录");
  assert.equal(byId["study-empty"].hidden, true, "问候面退场");

  /* ③有界超时：再次在途点击 → 30s 到点转登录引导 */
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  await settle();
  button.click();
  await settle();
  assert.equal(button.textContent, "正在登录…", "再次进入等待态");
  await tickTimeouts(30000);
  await settle();
  assert.equal(openLoginEvents.length, 1, "有界超时才转登录引导");
  assert.equal(button.disabled, false, "超时后按钮恢复");
  assert.equal(button.textContent, "登录后选择课程", "超时后文案回登录引导");

  /* ④明确失败快速转登录（不等 30s） */
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  await settle();
  button.click();
  await settle();
  store.set("auth", { state: "degraded", code: "fudan_session_expired", actions: [], connected: false, configured: true });
  await settle();
  assert.equal(openLoginEvents.length, 2, "明确失败立即转登录引导");
  assert.equal(button.disabled, false, "失败路径按钮恢复");
  windowTarget.removeEventListener("courselens:open-login", onOpenLogin);
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
}
await verifyCourseSelectWaiting();

/* ---- 第卅九案：恢复期本机可信缓存信号驱动选课入口（restoring_trusted）----
   后端在 checking 且保存账号可自动恢复时随快照下发闭集增量键
   restoring_trusted（http_api.py:1544，与三态门同一谓词）；入口消费它=
   课程目录由本机缓存直接渲染，夜间慢验证下不再空等 30s 再被甩登录。 */
async function verifyCourseSelectTrustedEntry() {
  const openLoginEvents = [];
  const onOpenLogin = () => { openLoginEvents.push(1); };
  windowTarget.addEventListener("courselens:open-login", onOpenLogin);
  const button = byId["study-start-select"];
  const courseRows = () => byId["study-course-list"].children.filter((node) => node.className === "course-row");

  /* ①checking + restoring_trusted=true：点击即时进目录（不等待/不跳登录/按钮不留禁用态） */
  store.set("courses", [
    { course_id: "c1", title: "课程一", term: "2026-2027学年1", authorization_state: "verified", lectures: [] },
    { course_id: "c2", title: "课程二", term: "2026-2027学年1", authorization_state: "verified", lectures: [] },
  ]);
  /* AS4-U1：恢复期目录探测照常发出——注入 checking+缓存信封（P2-C 真实形状：
     后端随 checking 回放本机缓存课程），缓存行即刻上屏而非被桩的 ready 目录覆盖。 */
  catalogEnvelopeOverride = {
    state: "checking", code: "fudan_session_checking", actions: [],
    refreshing: true, course_count: 2, courses: store.courses,
  };
  store.set("auth", {
    state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true,
    restoring_trusted: true,
  });
  await settle();
  button.click();
  await settle();
  assert.equal(openLoginEvents.length, 0, "可信快照点击绝不跳登录界面");
  assert.equal(byId["study-select"].hidden, false, "可信快照即时进入课程目录");
  assert.equal(byId["study-empty"].hidden, true, "问候面退场");
  assert.notEqual(button.textContent, "正在登录…", "可信快照不进等待文案");
  assert.equal(button.disabled, false, "无需等待，按钮不被留在禁用态");
  assert.equal(courseRows().length, 2, "目录行由本机缓存（store.courses）渲染");
  catalogEnvelopeOverride = null;

  /* ②显式 false 与缺省同判=仍进等待态；信号中途到达则立即接进目录 */
  store.set("auth", {
    state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true,
    restoring_trusted: false,
  });
  await settle();
  button.click();
  await settle();
  assert.equal(button.textContent, "正在登录…", "无信号（显式 false）仍进等待态");
  assert.equal(button.disabled, true, "等待期按钮禁用防重复");
  assert.equal(openLoginEvents.length, 0, "等待期不跳登录");
  store.set("auth", {
    state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true,
    restoring_trusted: true,
  });
  await settle();
  assert.equal(openLoginEvents.length, 0, "信号中途到达绝不跳登录");
  assert.equal(button.disabled, false, "等待态被接走，按钮恢复");
  assert.equal(byId["study-select"].hidden, false, "信号中途到达自动接进课程目录");

  /* ③可信快照进入后的终态收敛：就绪只换数据不换去向；明确失败由目录失败态
     引导（不再叠加登录弹层），等待标志已清 */
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
  assert.equal(openLoginEvents.length, 0, "就绪收敛仍不跳登录");
  assert.equal(byId["study-select"].hidden, false, "就绪后仍在课程目录");
  store.set("auth", { state: "degraded", code: "fudan_session_expired", actions: ["login"], connected: false, configured: true });
  await settle();
  assert.equal(openLoginEvents.length, 0, "已进目录后由目录失败态承载登录动作，不叠加弹层");
  windowTarget.removeEventListener("courselens:open-login", onOpenLogin);
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
}
await verifyCourseSelectTrustedEntry();

/* ---- AS4-U1（第四十四案）：恢复期目录先行 ----
   checking（含 trusted）期间目录装载照常发出（不再被 ready 门拦在挂载/订阅
   两个触发点上）；后端 checking 信封携带本机缓存课程（P2-C）时即刻上屏，恢复
   面板诚实说明「先显示上次缓存的课程」；空缓存=诚实「登录后显示」空态；
   恢复期单发探测不进 ready 态刷新窗的 1s 重试/超时降级（不伪造 catalog_timeout）。 */
async function verifyCatalogCacheFirst() {
  const courseRows = () => byId["study-course-list"].children.filter((node) => node.className === "course-row");
  const cachedCourses = [
    { course_id: "c1", title: "课程一", term: "2026-2027学年1", authorization_state: "verified", lectures: [] },
    { course_id: "c2", title: "课程二", term: "2026-2027学年1", authorization_state: "verified", lectures: [] },
  ];
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
  store.set("courses", []);
  catalogEnvelopeOverride = {
    state: "checking", code: "fudan_session_checking", actions: [],
    refreshing: true, course_count: 2, courses: cachedCourses,
  };
  calls.catalogGet = 0;
  store.set("auth", {
    state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true,
    restoring_trusted: true,
  });
  await settle();
  assert.ok(calls.catalogGet >= 1, "恢复期目录装载照常发出（不再等 ready 门）");
  assert.equal(courseRows().length, 2, "缓存课程随 checking 信封即刻上屏");
  assert.equal(byId["catalog-recovery"].hidden, false, "恢复面板在恢复期可见");
  assert.equal(byId["catalog-recovery-title"].textContent, "正在验证复旦会话", "面板标题仍是验证中");
  assert.equal(
    byId["catalog-recovery-impact"].textContent,
    "正在登录复旦账号，先显示上次缓存的课程。",
    "缓存上屏时的诚实副文案",
  );
  assert.equal(byId["catalog-evidence"].textContent, "正在验证复旦会话 · 2 门课程", "证据行带缓存计数");

  /* H3 兜底：空缓存=诚实「登录后显示」空态；恢复期普通 load（授权刷新事件）
     同样不得把 checking 伪造为 catalog_timeout 失败态 */
  store.set("courses", []);
  catalogEnvelopeOverride = {
    state: "checking", code: "fudan_session_checking", actions: [],
    refreshing: true, course_count: null, courses: [],
  };
  window.dispatchEvent(new Event("courselens:catalog-refresh"));
  await settle();
  const emptyText = byId["study-course-list"].children.map((node) => String(node.textContent || "")).join("");
  assert.ok(emptyText.includes("正在登录复旦账号，课程将在登录完成后显示。"), "空缓存=「登录后显示」诚实空态");
  assert.equal(byId["catalog-recovery-impact"].textContent, "课程将在验证完成后显示。", "空缓存时保留原副文案");
  assert.equal(
    byId["study-course-list"].children.some((node) => String(node.textContent || "").includes("课程目录连接超时")),
    false,
    "恢复期单发探测不进超时降级",
  );

  catalogEnvelopeOverride = null;
  store.set("courses", []);
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
}
await verifyCatalogCacheFirst();

/* ---- AS4-U2：复旦会话登录中呼吸提示 ----
   checking（含 restoring）期复旦行挂 conn-breathing 类；ready/失败即摘（停）；
   行文案=「复旦会话登录中…」；CSS 契约=1.6s 脉动 + reduced-motion 静态点退化。 */
async function verifyFudanBreathing() {
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  await settle();
  assert.equal(fudanConnDot.dataset.state, "checking", "复旦点 checking 形态");
  assert.equal(fudanConnDot.classList.contains("conn-breathing"), true, "checking（含 restoring）期呼吸类在");
  assert.equal(fudanConnText.textContent, "复旦会话登录中…", "恢复期行文案");
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();
  assert.equal(fudanConnDot.classList.contains("conn-breathing"), false, "ready 即停");
  assert.equal(fudanConnText.textContent, "复旦会话已连接，课程与搜索可用", "就绪文案不变");
  store.set("auth", { state: "degraded", code: "fudan_session_expired", actions: ["login"], connected: false, configured: true });
  await settle();
  assert.equal(fudanConnDot.classList.contains("conn-breathing"), false, "失败即停");
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: [], connected: true, configured: true });
  await settle();

  const layoutCss = readFileSync(new URL("../frontend/styles/layout.css", import.meta.url), "utf8");
  assert.match(
    layoutCss,
    /\.conn-dot\.conn-breathing\s*\{[^}]*animation:\s*conn-breath 1\.6s ease-in-out infinite/,
    "呼吸动画约 1.6s 循环",
  );
  assert.match(
    layoutCss,
    /@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.conn-dot\.conn-breathing\s*\{[^}]*animation:\s*none/,
    "reduced-motion 退化为静态点（文案行仍在）",
  );
}
await verifyFudanBreathing();
console.log("frontend usability/auth behavior passed");

/* AS11（第五十一案）：唯一总开关状态行（任务区顶部）——勾选 N 门/未勾选课程 */
async function verifyAutomationMasterSwitchStatusLine() {
  const runsTier = () => deepElements(byId["task-list"])
    .find((node) => String(node.className || "").split(/\s+/).includes("automation-runs-tier"));
  const tierNodesText = (tier) => deepElements(tier).map((node) => String(node.textContent || "")).join("\n");
  const fullRefresh = async () => {
    FakeEventSource.all[0].emit("tasks");
    await settle();
  };

  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "ready", enabled: true, account_id: "2026001",
    schedule: { times: ["13:00", "22:00"], timezone: "Asia/Shanghai", next_two: [] },
    rules: [{ course_id: "36941" }, { course_id: "36942" }], selected_courses: 2,
    runs: [{ workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: { processed: 1 }, observed_at: 1, updated_at: 1 }],
    imports: [], actions: [],
  };
  await fullRefresh();
  const tier = runsTier();
  assert.ok(tier, "勾选态任务区自动材料段在场");
  assert.ok(
    tierNodesText(tier).includes("每日 13:00、22:00 自动处理 · 已勾选 2 门"),
    "唯一总开关状态行：时刻取快照 schedule.times，计数取 rules",
  );

  /* 全部取消勾选（enabled=false+rules=[]）且 7 天内有动静：状态行诚实报「未勾选课程」 */
  automationSnapshotPayload = {
    schema: "courselens.automation.v3", protocol: "cloud-automation.v3",
    state: "disabled", enabled: false, rules: [], selected_courses: 0,
    runs: [{ workflow: "cloud-daily.yml", trigger_kind: "schedule", state: "completed", conclusion: "success", error_code: "", counts: { processed: 1 }, observed_at: Date.now() / 1000 - 3600, updated_at: Date.now() / 1000 - 3600 }],
    imports: [], actions: [],
  };
  await fullRefresh();
  const tierOff = runsTier();
  assert.ok(tierOff, "全部取消后 7 天内仍有记录时段落保留");
  assert.ok(tierNodesText(tierOff).includes("未勾选课程"), "N=0 如实显示未勾选课程");
  console.log("ok: AS11 唯一总开关状态行（勾选 N 门/未勾选课程/时刻取快照）");
}

/* O1（N6F 移交销项）：GitHub 连接文案三态诚实化——offline/unreachable 不再
   失实说「GitHub 连接失败」；error 只留给后端如实上报的 GitHub 故障。 */
async function verifyGithubConnTextThreeStates() {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("../frontend/modules/shell.js", import.meta.url), "utf8");
  /* CONN-STALE-R2 同笔换钉：取态/上屏分离后，闭集态由 fetchGithubConnState
     return 产出、refreshGithubConn 单点 assign——钉新形状，语义不变。 */
  assert.ok(
    source.includes("connState.github = next"),
    "GitHub 连接态单点 assign（取态/上屏分离）",
  );
  assert.ok(
    source.includes("githubStateFetchSeq"),
    "并发拍代际守卫：复验拍与事件拍并发时陈读不上屏",
  );
  assert.ok(/return "offline";/.test(source), "本机离线态闭集");
  assert.ok(/return "unreachable";/.test(source), "本地服务不可达态闭集");
  assert.ok(
    source.includes("网络未连接：请检查校园网或本机网络后重试"),
    "离线文案指本机网络，不嫁祸 GitHub",
  );
  assert.ok(
    source.includes("本地服务暂未连上，无法确认远程状态，正在自动重试"),
    "fetch 失败文案=本地服务未连上，不嫁祸 GitHub",
  );
  assert.ok(
    source.includes("DOT_STATE_FALLBACK"),
    "新态复用既有中性点视觉，不新增告警色",
  );
  /* F2 本体：后端 overall offline（从未配置）归安静 off 态，绝不落「失败」 */
  assert.ok(
    source.includes('if (state === "offline") return "off";'),
    "后端 offline（未配置）归 off，不落 error",
  );
  assert.ok(
    source.includes("GitHub 未连接：在设置里开启远程连接后即可使用"),
    "未配置文案给开启指引，不说「失败」",
  );
  /* 车道C卡15候选a：任务抽屉连接行=一句人话（不再「状态尚未确认」） */
  const drawerSource = familySource("tasks-drawer");
  assert.ok(
    drawerSource.includes("REMOTE_DRAWER_STATE_TEXT"),
    "抽屉连接行走 overall 闭集人话映射",
  );
  assert.ok(
    drawerSource.includes("云端连接正常，任务可正常派发"),
    "ready 态明确告诉学生可以派发",
  );
  /* COPYUP-1：error 态补代理指引——云端任务需能访问 GitHub，直连不稳可检查
     代理；文案进 api.js UI_HINTS 闭集表，shell.js 引用禁散落。unreachable/
     offline 语义不变（本地服务/本机网络，不嫁祸 GitHub）。 */
  const apiSource = readFileSync(new URL("../frontend/modules/api.js", import.meta.url), "utf8");
  assert.ok(
    apiSource.includes("云端任务需要能访问 GitHub，可检查代理"),
    "GitHub error 态代理指引进 api.js UI_HINTS 闭集表",
  );
  assert.ok(
    source.includes("error: UI_HINTS.github_conn_error"),
    "shell.js error 态引用闭集表，不散落模板串",
  );
  console.log("ok: O1 GitHub 连接文案三态诚实化（offline/unreachable/error 分开说）+卡15抽屉连接行人话+error 态代理指引（COPYUP-1）");
}

/* O3（N6F P3 销项）：F3 计数随所见 + F5/F6 销项证据 + F4 备查留档 */
async function verifyN6fP3Closures() {
  const { readFileSync } = await import("node:fs");
  const studySource = familySource("study");
  const updateSource = readFileSync(new URL("../frontend/modules/update-widget.js", import.meta.url), "utf8");
  /* F3：学期筛选使可见行少于全量时，证据行补「当前显示 M 门」 */
  assert.ok(studySource.includes("当前显示 ${courses.length} 门"), "目录计数随所见（F3 销项）");
  /* F5：更新状态首查前即人话「尚未检查」（销项=已达成） */
  assert.ok(updateSource.includes("尚未检查"), "更新状态首查前文案人话（F5 销项）");
  /* F6：未关联闪烁已由 AS10 courseTitleOf 三态占位修复（销项=已达成） */
  const drawerSource = familySource("tasks-drawer");
  assert.ok(drawerSource.includes("课程信息加载中"), "任务卡目录未就绪稳定占位（F6 销项，AS10-U2）");
  /* F4：catalog fetch 永挂起——维持「记录备查」不修（生产兜底=断连横幅；
     20×1s 重试梯语义不因窄边缘改动），此处仅固化其备查判定依据存在。 */
  assert.ok(studySource.includes("catalogRefreshTimedOut") || studySource.includes("renderCatalogTimeout"), "F4 备查：重试梯与超时面在场");
  /* F4 升级收口（夜8 O3）：挂起 fetch 有 20s 死线，TimeoutError 走既有超时收口 */
  assert.ok(studySource.includes("CATALOG_FETCH_DEADLINE_MS = 20000"), "目录 fetch 挂起死线在位");
  assert.ok(studySource.includes("AbortSignal.any("), "死线 signal 与取消 controller 并轨");
  const apiSource = readFileSync(new URL("../frontend/modules/api.js", import.meta.url), "utf8");
  assert.ok(apiSource.includes("signal: options.signal ?? controller.signal"), "request 透传调用方 signal");
  console.log("ok: O3 N6F P3 销项（F3 修/F5 达成/F6 由 AS10 修/F4 维持备查）");
}

/* VISUAL-POLISH-1 钉：settings 卸载清理必须整段零抛（P56 死 id 回归钉）。
   曾经的 bug：清理段引用已不存在的 settings-theme-toggle → pagehide 拆绑
   中途 TypeError，update-background-checks 等余下清理全部被跳过。 */
{
  await assert.doesNotReject(() => Promise.resolve().then(() => settingsCleanup()));
  console.log("ok: settings 卸载清理整段零抛（settings-theme-mode 对称拆绑）");
}

/* ---- NAV-HANG-1（2026-10-02 16:52 案例）：上游会话抖动不再弹桌/失灵/幽灵音频 ----
   旧链：auth 翻离 ready（configured=true，本地凭据仍在）→ 清目录+讲次选择 →
   问候面弹回 + 主按钮换名（locator/坐标/键盘全失灵）+ reload 仍坏 + video 幽灵出声。
   修后合同：configured=true 保目录保选择；configured=false（显式登出/切号）才清；
   离开学习桌的过场必叫停播放器；主按钮可访问名恒定（可见文案随 auth 态变）。 */
async function verifyAuthFlapKeepsDesk() {
  const button = byId["study-start-select"];
  /* 可访问名恒定钉在 test_frontend_workbench（本壳为 FakeElement，不带真实
     index.html 属性）；此处只钉行为面：文案随 auth 态、可访问名不随波。 */

  const course = {
    course_id: "c-navhang", title: "导航课程", term: "2026-2027学年1",
    authorization_state: "verified",
    lectures: [{ sub_id: "sub-navhang-1", sub_title: "第 1 讲" }],
  };
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true });
  store.set("courses", [course]);
  await settle();
  store.set("activeCourse", course);
  await settle();
  store.set("activeLecture", course.lectures[0]);
  await settle();
  assert.equal(byId["study-desk"].hidden, false, "前置：讲次选中进学习桌");

  /* 上游抖动（configured=true）：桌不弹、选择不清、按钮文案变但可访问名不变 */
  store.set("auth", { state: "action_required", code: "fudan_login_required", actions: ["login"], connected: false, configured: true });
  await settle();
  assert.equal(byId["study-desk"].hidden, false, "auth 抖动不清学习桌（NAV-HANG-1①）");
  assert.equal(String(store.activeLecture?.sub_id || ""), "sub-navhang-1", "讲次选择保持");
  assert.equal(byId["study-empty"].hidden, true, "不弹回问候面");
  assert.equal(button.textContent, "登录后选择课程", "可见文案仍随 auth 态（人话引导）");

  /* 上游抖动 + 空缓存（courses=[]）：同样不清选择（renderCourses 空分支 ④） */
  store.set("courses", []);
  store.set("auth", { state: "degraded", code: "fudan_login_failed", actions: ["login"], connected: false, configured: true });
  await settle();
  assert.equal(String(store.activeLecture?.sub_id || ""), "sub-navhang-1", "非 ready 空缓存不清选择（NAV-HANG-1④）");

  /* 幽灵音频补档：离开学习桌的过场叫停播放器（显式登出链验证） */
  const player = byId["player-stage"];
  let pauseCalls = 0;
  player.pause = () => { pauseCalls += 1; player.paused = true; };
  player.paused = false;
  store.set("auth", { state: "action_required", code: "fudan_credentials_missing", actions: ["login"], connected: false, configured: false });
  await settle();
  assert.equal(pauseCalls, 1, "desk 离场过场必叫停播放器（NAV-HANG-1②）");
  assert.equal(store.activeLecture, null, "显式登出（凭据消失）仍清选择（隐私红线不变）");

  /* 恢复 ready 态，不留跨段污染 */
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true });
  delete player.pause;
  await settle();
}
await verifyAuthFlapKeepsDesk();
console.log("ok: NAV-HANG-1——auth 抖动保桌保选择/幽灵音频补档/可访问名恒定");
