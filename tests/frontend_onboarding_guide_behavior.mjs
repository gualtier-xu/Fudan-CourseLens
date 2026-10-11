import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/* 新手引导执行型行为测试：加载真实 frontend/modules/onboarding.js（仿 tests/frontend_usability_auth_behavior.mjs 的桩件法）。
   只安装 onboarding.js；shell.js 仅复用其 selectPage 导出（不安装）。 */
import { selectPage } from "../frontend/modules/shell.js";

class FakeClassList {
  constructor(node) {
    this.node = node;
    this.values = new Set();
  }

  _sync() { this.node.className = [...this.values].join(" "); }
  add(...values) { values.forEach((value) => this.values.add(value)); this._sync(); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); this._sync(); }
  toggle(value, enabled) {
    if (enabled === undefined) {
      if (this.values.has(value)) this.values.delete(value);
      else this.values.add(value);
    } else if (enabled) this.values.add(value);
    else this.values.delete(value);
    this._sync();
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
    this.classList = new FakeClassList(this);
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
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
    for (const node of [...nodes].reverse()) {
      node.parent = this;
      this.children.unshift(node);
    }
  }

  get parentElement() {
    return this.parent;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true }));
  }

  focus() {
    const doc = this.ownerDocument || globalThis.document;
    if (doc) doc.activeElement = this;
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

const deepElements = (element, into = []) => {
  if (!element) return into;
  into.push(element);
  for (const child of element.children || []) deepElements(child, into);
  return into;
};

function collectText(element, into) {
  if (!element || element.hidden) return into;
  into.push(String(element.textContent || ""));
  for (const child of element.children || []) collectText(child, into);
  return into;
}

/* ---- 合成环境（每组场景重建 DOM/window） ---- */

const GUIDE_SCHEMA = "courselens.onboarding-guide.v1";
const GUIDE_VERSION = "student-onboarding.v1";

let byId = {};
let registry = [];
let currentDocument = null;
const localStorageLog = [];

function buildDom() {
  const el = (tag, id) => new FakeElement(tag, id);
  const created = { seq: 0 };
  byId = {};
  registry = [];
  const add = (node) => { byId[node.id] = node; registry.push(node); return node; };

  for (const id of ["workspace-main", "onboarding-progress", "onboarding-skip", "onboarding-prev",
    "onboarding-next", "onboarding-toc", "onboarding-mobile-progress", "onboarding-pending",
    "onboarding-auth-state", "onboarding-auth-text", "onboarding-auth-impact", "onboarding-open-login",
    "onboarding-auth-retry", "onboarding-catalog-state", "onboarding-catalog-text", "onboarding-catalog-count",
    "onboarding-catalog-refresh", "onboarding-catalog-retry", "onboarding-cloud-row", "onboarding-cloud-text",
    "onboarding-cloud-open", "onboarding-remote-row", "onboarding-remote-text",
    "onboarding-deepseek-row", "onboarding-deepseek-text", "onboarding-deepseek-open", "onboarding-summary",
    "onboarding-action-error", "onboarding-complete-recovery", "onboarding-complete-retry",
    "onboarding-dismiss-recovery", "onboarding-dismiss-retry", "onboarding-dismiss-leave", "onboarding-live",
    "onboarding-page", "settings-page", "study-page", "settings-return-guide", "help-open-guide",
    "account-menu-onboarding", "account-menu", "login-dialog", "refresh-catalog",
    "toast-region"]) {
    add(el("button", id));
  }

  const main = byId["workspace-main"];
  byId["study-page"].dataset.page = "study";
  byId["settings-page"].dataset.page = "settings";
  byId["onboarding-page"].dataset.page = "onboarding";
  byId["study-page"].classList.add("page", "active");
  byId["settings-page"].classList.add("page");
  byId["onboarding-page"].classList.add("page");
  byId["settings-page"].hidden = true;
  byId["onboarding-page"].hidden = true;
  byId["settings-return-guide"].hidden = true;
  main.append(byId["study-page"], byId["settings-page"], byId["onboarding-page"]);

  /* D-20261009-09：学习面三态区域（index.html 同构）——问候 hero=刷新落地必经
     面（恢复行默认宿主），选课面板隐藏、内含 .home-overview（面板开时宿主）。 */
  const studyEmpty = add(el("div", "study-empty"));
  studyEmpty.className = "empty-state";
  const studySelect = add(el("div", "study-select"));
  studySelect.className = "study-select";
  studySelect.hidden = true;
  const homeOverview = add(el("div", "home-overview"));
  homeOverview.className = "home-overview";
  studySelect.append(homeOverview);
  byId["study-page"].append(studyEmpty, studySelect);

  /* F1（化身走查 20261008）→ LANDING-AESTHETIC-1（用户裁决）：引导挂起提示行
     （index.html 同构：button-row + hint + text-button；宿主=选课面板概览顶部
     原位，不占着陆页） */
  const homeGuideResume = add(el("div", "home-guide-resume"));
  homeGuideResume.hidden = true;
  const homeGuideResumeHint = add(el("span", "home-guide-resume-hint"));
  homeGuideResumeHint.textContent = "新手引导还没完成，随时可以继续。";
  const homeGuideResumeAction = add(el("button", "home-guide-resume-action"));
  homeGuideResumeAction.textContent = "继续新手引导";
  homeGuideResume.append(homeGuideResumeHint, homeGuideResumeAction);
  homeOverview.append(homeGuideResume);

  const content = add(el("div", "onboarding-content"));
  content.className = "onboarding-content";
  byId["onboarding-page"].append(byId["onboarding-progress"], byId["onboarding-skip"],
    byId["onboarding-toc"], content);
  for (const id of ["onboarding-mobile-progress", "onboarding-pending", "onboarding-summary",
    "onboarding-action-error", "onboarding-complete-recovery", "onboarding-dismiss-recovery", "onboarding-live"]) {
    byId[id].hidden = id !== "onboarding-summary";
    content.append(byId[id]);
  }
  byId["onboarding-complete-recovery"].append(byId["onboarding-complete-retry"]);
  const completeRecoveryText = add(el("p", "onboarding-complete-recovery-text"));
  completeRecoveryText.textContent = "引导完成状态未能保存，请重试。";
  byId["onboarding-complete-recovery"].append(completeRecoveryText);
  byId["onboarding-dismiss-recovery"].append(byId["onboarding-dismiss-retry"], byId["onboarding-dismiss-leave"]);
  const dismissRecoveryText = add(el("p", "onboarding-dismiss-recovery-text"));
  dismissRecoveryText.textContent = "引导状态未能保存，下次启动仍会显示。";
  byId["onboarding-dismiss-recovery"].append(dismissRecoveryText);
  content.append(byId["onboarding-prev"], byId["onboarding-next"]);

  const tocButtons = [];
  for (const step of ["1", "2", "3", "4", "5"]) {
    const button = add(el("button", `onboarding-toc-${step}`));
    button.dataset.onboardingStep = step;
    byId["onboarding-toc"].append(button);
    tocButtons.push(button);
    const panel = add(el("section", `onboarding-step-${step}`));
    panel.dataset.onboardingPanel = step;
    panel.hidden = true;
    const heading = add(el("h2", `onboarding-heading-${step}`));
    heading.setAttribute("tabindex", "-1");
    panel.append(heading);
    content.append(panel);
  }

  /* 步骤 2/3/4 的行结构归位（真实 DOM 由 index.html 提供） */
  byId["onboarding-auth-state"].append(byId["onboarding-auth-text"]);
  byId["onboarding-catalog-state"].append(byId["onboarding-catalog-text"]);
  byId["onboarding-cloud-row"].append(byId["onboarding-cloud-text"], byId["onboarding-cloud-open"]);
  /* 静态文案由 index.html 提供（U2 起开关退役，本行不再由 JS 改写） */
  byId["onboarding-cloud-text"].textContent = "云端处理：连接就绪即可用 · 加密代算，云端最长保留 30 天";
  byId["onboarding-cloud-open"].dataset.onboardingSettingsJump = "settings-network-group";
  /* SIMPLIFY-AUDIT-1 S3：同目的地双钮合并——remote 行只剩状态文本 */
  byId["onboarding-remote-row"].append(byId["onboarding-remote-text"]);
  byId["onboarding-deepseek-row"].append(byId["onboarding-deepseek-text"], byId["onboarding-deepseek-open"]);
  byId["onboarding-deepseek-open"].dataset.onboardingSettingsJump = "settings-ai-group";
  byId["onboarding-step-2"].append(byId["onboarding-auth-state"], byId["onboarding-auth-impact"],
    byId["onboarding-open-login"], byId["onboarding-auth-retry"]);
  byId["onboarding-step-3"].append(byId["onboarding-catalog-state"], byId["onboarding-catalog-count"],
    byId["onboarding-catalog-refresh"], byId["onboarding-catalog-retry"]);
  const optionalRows = el("div", "onboarding-optional-rows");
  optionalRows.className = "onboarding-optional-rows";
  optionalRows.append(byId["onboarding-cloud-row"], byId["onboarding-remote-row"], byId["onboarding-deepseek-row"]);
  byId["onboarding-step-4"].append(optionalRows);
  byId["onboarding-step-5"].append(byId["onboarding-summary"]);

  currentDocument = Object.assign(new EventTarget(), {
    ownerDocumentOf: (node) => { node.ownerDocument = currentDocument; },
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => {
      created.seq += 1;
      const node = new FakeElement(tag, `created-${created.seq}`);
      node.ownerDocument = currentDocument;
      registry.push(node);
      return node;
    },
    querySelectorAll: (selector) => registry.filter((node) => elementMatches(node, selector)),
    querySelector: (selector) => registry.find((node) => elementMatches(node, selector)) || null,
    activeElement: null,
    documentElement: { dataset: {} },
  });
  for (const node of registry) node.ownerDocument = currentDocument;
  globalThis.document = currentDocument;
}

function buildWindow() {
  const timeouts = new Map();
  let timeoutSeq = 0;
  const windowTarget = new EventTarget();
  windowTarget.setTimeout = (fn) => {
    timeoutSeq += 1;
    timeouts.set(timeoutSeq, fn);
    return timeoutSeq;
  };
  windowTarget.clearTimeout = (id) => timeouts.delete(id);
  windowTarget.requestAnimationFrame = (fn) => fn();
  globalThis.window = windowTarget;
  return { windowTarget, runTimeouts: () => { for (const [, fn] of [...timeouts.entries()]) fn(); } };
}

/* ---- 网络桩：fixture 可变；仅允许 app-shell/onboarding/onboarding/actions ---- */

const fixture = {
  guide: {
    schema: GUIDE_SCHEMA, version: GUIDE_VERSION, disposition: "new",
    auto_opened: false, persistence: "ready", source: "default", updated_at: 0,
  },
  auth: { state: "action_required", code: "fudan_login_required", actions: ["login"], connected: false, configured: false },
  catalog: { state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"], course_count: 5 },
  remote: { overall: { state: "degraded", code: "remote_channel_test_required", actions: [] }, remote_compute: { enabled: false } },
  network: { mode: "auto", proxy_url: "http://127.0.0.1:6268", route_generation: 0 },
  consentAccepted: false,
  aiConfigured: false,
  appShellFail: false,
  markOpenFail: false,
  dismissFail: false,
  completeFail: false,
  proxyDetect: { status: "not_found", source: null, port: null },
  proxyDetectFail: false,
  proxySaveFail: false,
};
const calls = { appShellGet: 0, onboardingGet: 0, actionsPost: [], settingsPost: [], other: [] };
let pendingAppShell = null;

function resetFixture() {
  fixture.guide = {
    schema: GUIDE_SCHEMA, version: GUIDE_VERSION, disposition: "new",
    auto_opened: false, persistence: "ready", source: "default", updated_at: 0,
  };
  fixture.auth = { state: "action_required", code: "fudan_login_required", actions: ["login"], connected: false, configured: false };
  fixture.catalog = { state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"], course_count: 5 };
  fixture.remote = { overall: { state: "degraded", code: "remote_channel_test_required", actions: [] }, remote_compute: { enabled: false } };
  fixture.network = { mode: "auto", proxy_url: "http://127.0.0.1:6268", route_generation: 0 };
  fixture.consentAccepted = false;
  fixture.aiConfigured = false;
  fixture.appShellFail = false;
  fixture.markOpenFail = false;
  fixture.dismissFail = false;
  fixture.completeFail = false;
  fixture.proxyDetect = { status: "not_found", source: null, port: null };
  fixture.proxyDetectFail = false;
  fixture.proxySaveFail = false;
  calls.appShellGet = 0;
  calls.onboardingGet = 0;
  calls.actionsPost = [];
  calls.settingsPost = [];
  calls.other = [];
  pendingAppShell = null;
}

function ok(data) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (route === "/api/v3/app-shell" && method === "GET") {
    calls.appShellGet += 1;
    if (pendingAppShell) return pendingAppShell.promise;
    if (fixture.appShellFail) return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
    return ok({
      authentication: fixture.auth,
      catalog: fixture.catalog,
      remote: fixture.remote,
      tutorials: { guide: fixture.guide },
    });
  }
  if (route === "/api/v3/onboarding" && method === "GET") {
    calls.onboardingGet += 1;
    return ok({
      consent: { accepted: fixture.consentAccepted },
      ai: { configured: fixture.aiConfigured },
      network: fixture.network,
      guide: fixture.guide,
    });
  }
  if (route === "/api/v3/onboarding/actions" && method === "POST") {
    const body = JSON.parse(options.body || "{}");
    calls.actionsPost.push(body);
    if (body.action === "mark-opened" && fixture.markOpenFail) {
      return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
    }
    if (body.action === "dismiss" && fixture.dismissFail) {
      return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
    }
    if (body.action === "complete" && fixture.completeFail) {
      return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
    }
    const dispositionByAction = { "mark-opened": fixture.guide.disposition === "completed" ? "completed" : "new", dismiss: "dismissed", complete: "completed" };
    fixture.guide = {
      schema: GUIDE_SCHEMA, version: GUIDE_VERSION,
      disposition: dispositionByAction[body.action] || fixture.guide.disposition,
      auto_opened: true, persistence: "ready", source: "stored", updated_at: 1,
    };
    return ok({ guide: fixture.guide });
  }
  if (route === "/api/v3/settings/actions" && method === "POST") {
    /* PROXY-AUTODETECT-1：代理卡闭集动作（detect-proxy 只读 / update-network 保存）；
       经此分支的动作是引导页被许可的请求，不计入 calls.other。 */
    const body = JSON.parse(options.body || "{}");
    calls.settingsPost.push(body);
    if (body.action === "detect-proxy") {
      if (fixture.proxyDetectFail) {
        return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
      }
      return ok(fixture.proxyDetect);
    }
    if (body.action === "update-network") {
      if (fixture.proxySaveFail) {
        return new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
      }
      fixture.network = {
        mode: String(body.mode || "auto"),
        proxy_url: String(body.proxy_url || "").replace(/\/+$/, "") || "http://127.0.0.1:6268",
        route_generation: (fixture.network?.route_generation || 0) + 1,
      };
      return ok({ ...fixture.network, routes: {} });
    }
    calls.other.push(`${method} ${route}`);
    throw new Error(`unexpected synthetic route: ${method} ${route}`);
  }
  calls.other.push(`${method} ${route}`);
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

globalThis.CustomEvent = FakeCustomEvent;
globalThis.matchMedia = (query) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { localStorageLog.push(String(key)); this.store.set(key, String(value)); },
};

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  for (let index = 0; index < 6; index += 1) await nextTurn();
};

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
    tasks: [],
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

const pageActive = (name) => byId[`${name}-page`].classList.contains("active")
  && byId[`${name}-page`].hidden === false;
const visiblePanel = () => registry
  .filter((node) => node.dataset?.onboardingPanel !== undefined && node.hidden === false)
  .map((node) => node.dataset.onboardingPanel)[0] || "";
const guideText = () => collectText(byId["onboarding-page"], []).join("\n");
const toastText = () => collectText(byId["toast-region"], []).join("\n");
const click = (node) => node.click();
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

/* ================= 场景组 A：自动打开 + 五步交互 + 状态分支 + 生命周期 ================= */

{
  buildDom();
  const { windowTarget } = buildWindow();
  resetFixture();
  const store = createStore();
  const { installOnboarding } = await import("../frontend/modules/onboarding.js");
  const openLoginEvents = [];
  let studyReturnEvents = 0;
  windowTarget.addEventListener("courselens:open-login", () => {
    openLoginEvents.push(1);
    byId["login-dialog"].open = true; /* 仿真 shell：同步 showModal */
  });
  windowTarget.addEventListener("courselens:study-return-select", () => { studyReturnEvents += 1; });

  /* 1) guide=new：首次 bootstrap 自动进入引导且只进入一次（F-GATE-1 根修后：
     开在记前——打开不等 mark-opened 往返，POST 随后补记） */
  const cleanup = await installOnboarding(store);
  await settle();
  assert.equal(pageActive("onboarding"), true, "guide=new 时自动进入引导页");
  assert.equal(pageActive("study"), false, "自动打开切换到引导页");
  assert.equal(calls.actionsPost.length, 1, "自动打开后补记 mark-opened");
  assert.deepEqual(calls.actionsPost[0], { action: "mark-opened", version: GUIDE_VERSION });
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  await settle();
  assert.equal(calls.actionsPost.length, 1, "自动打开每个进程至多一次，focus 不重复 mark-opened");
  assert.equal(calls.other.length, 0, "打开引导只产生 app-shell/onboarding 读取，无其他路由");
  assert.equal(localStorageLog.length, 0, "onboarding 不写 localStorage");

  /* 2) new → 默认步骤 1；步骤导航 + TOC + 焦点 */
  /* UIAUDIT-1 F1 回归钉：目录高亮不变量——任一时刻恰有一个 aria-current=step
     且与可见面板步号一致；aria-current 与 is-done 两态互斥（审计曾在中间树上
     截到旧步残留高亮+✓并存、新步无指示的切片，此处把 DOM 不变量钉死）。 */
  const assertTocInvariant = (expectedStep, label) => {
    const steps = [1, 2, 3, 4, 5].map((n) => byId[`onboarding-toc-${n}`]);
    const current = steps.filter((b) => b.getAttribute("aria-current") === "step");
    assert.equal(current.length, 1, `${label}：aria-current 恰有一个`);
    assert.equal(current[0].dataset.onboardingStep, String(expectedStep), `${label}：aria-current 跟随当前步`);
    assert.equal(visiblePanel(), String(expectedStep), `${label}：面板与 aria-current 同步`);
    for (const b of steps) {
      assert.equal(
        (b.getAttribute("aria-current") === "step") && b.classList.contains("is-done"),
        false,
        `${label}：步骤 ${b.dataset.onboardingStep} aria-current 与 is-done 互斥`,
      );
    }
  };
  assert.equal(visiblePanel(), "1", "new 记录默认落在步骤 1");
  assert.ok(guideText().includes("第 1 步，共 5 步"), "进度文字可见");
  assert.equal(byId["onboarding-prev"].hidden, true, "第一步隐藏上一步");
  assert.equal(byId["onboarding-next"].textContent, "下一步", "第五步之前主按钮为下一步");
  click(byId[`onboarding-toc-4`]);
  assert.equal(visiblePanel(), "4", "步骤目录允许跳到任一步");
  assert.equal(byId["onboarding-toc-4"].getAttribute("aria-current"), "step", "当前步骤 aria-current=step");
  assert.equal(byId["onboarding-toc-1"].getAttribute("aria-current"), null, "非当前步骤无 aria-current");
  assert.equal(byId["onboarding-prev"].hidden, false, "非第一步显示上一步");
  assertTocInvariant(4, "目录跳步后");
  /* S10-A 焦点契约：步骤变化不再聚焦步骤 h2（容器级大环根因）——焦点留在
     激活的导航控件上（此处由浏览器原生点击聚焦模拟），步骤切换经既有
     onboarding-live live region 播报 */
  document.activeElement = byId["onboarding-toc-4"]; /* 浏览器原生行为：点击聚焦按钮 */
  click(byId[`onboarding-toc-1`]);
  assertTocInvariant(1, "目录跳回第 1 步后");
  document.activeElement = byId["onboarding-toc-4"];
  click(byId[`onboarding-toc-4`]);
  assert.equal(document.activeElement?.id, "onboarding-toc-4", "步骤变化后焦点留在激活的 TOC 按钮");
  assert.ok(String(byId["onboarding-live"].textContent).includes("第 4 步"), "步骤变化经 live region 播报");
  click(byId["onboarding-next"]);
  assert.equal(visiblePanel(), "5", "下一步进入步骤 5");
  assert.equal(byId["onboarding-next"].textContent, "完成引导", "第五步主按钮为完成引导");
  assert.equal(byId["onboarding-next"].classList.contains("btn-primary"), true, "完成引导为主按钮样式");
  assertTocInvariant(5, "下一步到第 5 步后");
  click(byId["onboarding-prev"]);
  assert.equal(visiblePanel(), "4", "上一步回到步骤 4");
  assertTocInvariant(4, "上一步回第 4 步后");
  click(byId["onboarding-toc-1"]);
  assert.equal(byId["onboarding-prev"].hidden, true, "回到第一步再次隐藏上一步");
  assertTocInvariant(1, "回到第 1 步后");

  /* 3) 步骤 2 登录：复用 courselens:open-login，双击不重复提交；不复制凭据输入 */
  click(byId["onboarding-toc-2"]);
  assert.ok(guideText().includes("需要登录复旦课程平台"), "action_required 显示既有闭集文案");
  assert.equal(byId["onboarding-open-login"].hidden, false, "未就绪时显示登录按钮");
  click(byId["onboarding-open-login"]);
  click(byId["onboarding-open-login"]);
  assert.equal(openLoginEvents.length, 1, "双击只派发一次 courselens:open-login（dialog 已打开守卫）");
  assert.equal(calls.other.length, 0, "引导不直接提交认证请求");
  byId["login-dialog"].open = false;
  store.set("auth", { state: "checking", code: "fudan_session_checking", actions: [], connected: false, configured: true });
  assert.equal(byId["onboarding-open-login"].disabled, true, "checking 时不可重复提交");
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true });
  assert.ok(guideText().includes("复旦课程平台已登录"), "ready 显示闭集已连接文案");
  assert.equal(byId["onboarding-open-login"].hidden, true, "ready 后隐藏登录按钮");
  assert.equal(visiblePanel(), "2", "状态变 ready 不自动跳步");
  assert.ok(byId["onboarding-live"].textContent.includes("复旦课程平台已登录"), "状态翻转写入 polite live region");

  /* 4) 步骤 3 目录分支：ready-nonzero / ready-zero / checking / stale / action_required */
  click(byId["onboarding-toc-3"]);
  assert.ok(guideText().includes("5 门可访问课程"), "ready 且有课程只显示数量");
  assert.equal(guideText().includes("90"), false, "不显示课程标识等真实内容");
  fixture.catalog = { state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"], course_count: 0 };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("当前账号暂未发现可访问课程"), "ready-zero 诚实显示，不判失败");
  fixture.catalog = { state: "checking", code: "authorized_catalog_refreshing", actions: [], course_count: null };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("正在刷新课程目录"), "checking 复用闭集解释");
  fixture.catalog = { state: "degraded", code: "authorized_catalog_stale", actions: ["refresh-catalog"], course_count: 2 };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("课程目录需要刷新"), "stale 显示闭集标题");
  assert.ok(guideText().includes("正在显示同一账号上次验证的课程"), "stale 显示闭集影响说明");
  fixture.catalog = { state: "action_required", code: "catalog_session_expired", actions: ["login"], course_count: null };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("课程目录授权已过期"), "action_required 复用闭集文案");
  /* 读取失败：保留上次确认状态并标记，可重试 */
  fixture.appShellFail = true;
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("状态读取暂时失败"), "读取失败显示 stale 提示而非未配置");
  assert.ok(guideText().includes("课程目录授权已过期"), "失败保留上次已确认状态");
  assert.equal(byId["onboarding-catalog-retry"].hidden, false, "失败时显示重试状态读取");
  fixture.appShellFail = false;
  click(byId["onboarding-catalog-retry"]);
  await settle();
  assert.equal(byId["onboarding-catalog-retry"].hidden, true, "重试成功后隐藏重试按钮");
  /* 刷新走既有 refresh-catalog 动作 */
  let refreshClicks = 0;
  byId["refresh-catalog"].addEventListener("click", () => { refreshClicks += 1; });
  click(byId["onboarding-catalog-refresh"]);
  assert.equal(refreshClicks, 1, "目录刷新委托既有 refresh-catalog 动作");

  /* 5) 步骤 4 三行只读证据 + 代理卡（自动检测走闭集 detect-proxy 动作，
        PROXY-AUTODETECT-1）+ 跳设置 + 返回引导；除代理闭集动作外不发起远程动作 */
  click(byId["onboarding-toc-4"]);
  /* CLOUD-CONSENT-AUTO-1 U2：云端处理开关退役——首行是固定透明说明
     （连接就绪即可用 + 保留期），不再读任何开关真值 */
  assert.ok(guideText().includes("云端处理：连接就绪即可用"), "首行=连接就绪即可用的透明说明");
  assert.ok(guideText().includes("云端最长保留 30 天"), "透明说明含保留期");
  assert.ok(guideText().includes("可稍后设置"), "未配置项显示可稍后设置");
  assert.equal(guideText().includes("暂时无法确认"), false, "三项均已确认时不显示暂时无法确认");
  assert.equal(guideText().includes("已开启（按现有设置）"), false, "开关真值文案退役");
  assert.equal(guideText().includes("未开启 · 可稍后设置"), false, "开关未开文案退役");
  fixture.remote = { ...fixture.remote, remote_compute: { configured: true, verified: true } };
  fixture.aiConfigured = false;
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("云端处理：连接就绪即可用"), "开关快照变化不再改写该行（行文与开关无关）");
  /* 5b) GitHub 步骤：安装范围证据 = 安装 App + 仅勾选 Worker 与 Mailbox 两个仓库 */
  fixture.remote = {
    overall: { state: "action_required", code: "installation_scope_not_exact", actions: [] },
    components: [
      {
        component: "installation", state: "action_required",
        code: "installation_scope_not_exact",
        actions: ["restrict-github-app-installation"],
        evidence: {
          missing: ["fudan-courselens-mailbox"],
          unexpected: ["student/unrelated-repository"],
          settings_url: "https://github.com/settings/installations/42",
        },
      },
    ],
  };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("安装 CourseLens App"), "GitHub 步骤呈现安装 App 要求");
  assert.ok(guideText().includes("Worker 与 Mailbox 两个仓库"), "GitHub 步骤呈现精确勾选要求");
  assert.ok(guideText().includes("缺少 fudan-courselens-mailbox"), "缺少仓库进入证据文案");
  assert.ok(guideText().includes("student/unrelated-repository"), "多选仓库进入证据文案");
  assert.ok(guideText().includes("settings/installations/42"), "官方安装设置入口进入证据文案");
  /* 5c) 首次安装（App 未装、仓库已预选）：文案反映预选流程，绝不要求手动创建/搜索/勾选 */
  fixture.remote = {
    overall: { state: "action_required", code: "installation_missing", actions: [] },
    components: [
      {
        component: "installation", state: "action_required",
        code: "installation_missing",
        actions: ["bootstrap"],
        evidence: {
          installation_setup_url: "https://github.com/apps/fudan-courselens/installations/new/permissions?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        },
      },
    ],
  };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.ok(guideText().includes("已自动预选"), "首次安装文案说明仓库已自动预选");
  assert.ok(guideText().includes("无需手动创建、搜索或勾选仓库"), "首次安装文案明确无需手动操作仓库");
  assert.equal(guideText().includes("缺少"), false, "首次安装不显示缺少仓库清单");
  fixture.remote = { overall: { state: "degraded", code: "remote_channel_test_required", actions: [] } };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.equal(guideText().includes("settings/installations/42"), false, "范围恢复后不再显示安装证据");
  assert.ok(guideText().includes("可稍后设置"), "非就绪远程仍按既有闭集呈现");
  const postsBeforeJump = calls.actionsPost.length;
  click(byId["onboarding-cloud-open"]);
  assert.equal(pageActive("settings"), true, "跳到既有设置页");
  assert.equal(pageActive("onboarding"), false, "设置页期间引导页隐藏");
  assert.equal(byId["settings-return-guide"].hidden, false, "显示返回新手引导临时按钮");
  assert.equal(calls.other.length, 0, "打开引导/跳设置不触发远程连接等动作路由");
  click(byId["settings-return-guide"]);
  assert.equal(pageActive("onboarding"), true, "从设置返回引导");
  assert.equal(pageActive("settings"), false);
  assert.equal(byId["settings-return-guide"].hidden, true, "返回引导即清除临时返回按钮");

  /* (a) 返回引导后的“直接进入设置”（账户菜单路径）：不得再显示临时按钮 */
  selectPage("settings");
  assert.equal(byId["settings-return-guide"].hidden, true, "非引导发起的设置入口不显示返回按钮");
  assert.equal(pageActive("onboarding"), false, "非引导路径进入设置视为放弃引导");
  selectPage("study");

  /* (c) 重新打开引导并再次由引导发起跳设置：按钮重新出现 */
  click(byId["account-menu-onboarding"]);
  await settle();
  click(byId["onboarding-toc-4"]);
  click(byId["onboarding-cloud-open"]);
  assert.equal(pageActive("settings"), true, "引导再次跳到设置页");
  assert.equal(byId["settings-return-guide"].hidden, false, "引导发起的设置访问再次显示返回按钮");

  /* (c2) REALRUN-1 P2-3：引导挂起态（引导发起的设置跳转，visible 保持 true）
     下账户菜单「新手引导」必须回到引导页继续——此前落进 openGuide 的 visible
     早退=静默无动作（真测 2/2 复现）。 */
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "挂起态账户菜单新手引导回到引导页（不静默）");
  assert.equal(pageActive("settings"), false, "挂起态不再停留设置页");
  assert.equal(byId["settings-return-guide"].hidden, false, "挂起态恢复后临时返回按钮语义保持");

  /* (b) 从设置直接离开（非引导返回路径）：按钮隐藏且引导关闭 */
  selectPage("study");
  assert.equal(byId["settings-return-guide"].hidden, true, "离开设置到其他页隐藏返回按钮");
  assert.equal(calls.actionsPost.length, postsBeforeJump, "只读展示不产生任何动作 POST");

  /* 6) 步骤 5 总结 + 完成失败行内恢复 + 完成成功返回 */
  click(byId["account-menu-onboarding"]);
  await settle();
  fixture.catalog = { state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"], course_count: 6 };
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  click(byId["onboarding-toc-5"]);
  assert.ok(guideText().includes("基础学习已可用"), "核心就绪总结");
  assert.ok(guideText().includes("尚需准备远程连接"), "远程未就绪诚实呈现");
  assert.ok(guideText().includes("字幕将使用无大模型验证模式"), "无 DeepSeek 时诚实标注");
  assert.equal(guideText().includes("所有设置已完成"), false, "不得伪称全部完成");
  fixture.completeFail = true;
  click(byId["onboarding-next"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "complete 写入失败留在引导页");
  assert.equal(byId["onboarding-complete-recovery"].hidden, false, "行内恢复面板可见");
  assert.equal(byId["onboarding-action-error"].hidden, false, "role=alert 错误行可见");
  assert.equal(fixture.guide.disposition, "new", "失败不得在前端假装 completed");
  fixture.completeFail = false;
  click(byId["onboarding-complete-retry"]);
  await settle();
  assert.equal(pageActive("study"), true, "完成成功返回来源页（study）");
  assert.equal(studyReturnEvents, 1, "catalog ready 时完成引导进入课程选择态");
  assert.equal(fixture.guide.disposition, "completed", "完成写入 completed");
  assert.equal(byId["settings-return-guide"].hidden, true, "引导关闭后隐藏返回按钮");

  /* 7) completed 重开默认步骤 5；随后 dismiss 失败行内恢复 + 仅本次退出 */
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "账户菜单入口可重开（学习空态按钮已移除）");
  assert.equal(visiblePanel(), "5", "completed 重开默认落在步骤 5");
  fixture.dismissFail = true;
  const postsBeforeDismiss = calls.actionsPost.length;
  click(byId["onboarding-skip"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "dismiss 失败留在引导页");
  assert.equal(byId["onboarding-dismiss-recovery"].hidden, false, "dismiss 失败显示行内面板");
  assert.ok(guideText().includes("引导状态未能保存，下次启动仍会显示"), "行内说明文案");
  click(byId["onboarding-dismiss-retry"]);
  await settle();
  assert.equal(byId["onboarding-dismiss-recovery"].hidden, false, "重试保存仍失败时面板保持");
  assert.equal(calls.actionsPost.length, postsBeforeDismiss + 2, "重试保存重新 POST dismiss");
  click(byId["onboarding-dismiss-leave"]);
  await settle();
  assert.equal(pageActive("study"), true, "仅本次退出回来源页");
  assert.equal(calls.actionsPost.length, postsBeforeDismiss + 2, "仅本次退出不持久化");
  assert.equal(fixture.guide.disposition, "completed", "仅本次退出不改变已确认状态");

  /* 8) dismiss 成功 → 恢复刷新后不再自动打开；dismissed 重开默认第一个未就绪核心步 */
  fixture.dismissFail = false;
  click(byId["account-menu-onboarding"]);
  await settle();
  click(byId["onboarding-skip"]);
  await settle();
  assert.equal(pageActive("study"), true, "dismiss 成功退出到来源页");
  assert.equal(fixture.guide.disposition, "dismissed", "dismiss 写入 dismissed");
  store.set("auth", { state: "degraded", code: "fudan_session_expired", actions: ["login"], connected: false, configured: true });
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(visiblePanel(), "2", "dismissed 重开默认落在第一个未就绪核心步（认证）");
  selectPage("study");
  assert.equal(pageActive("onboarding"), false, "离开引导页即关闭并清理");
  assert.equal(byId["settings-return-guide"].hidden, true, "引导关闭后返回按钮保持隐藏");
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true });
  fixture.catalog = { state: "degraded", code: "authorized_catalog_stale", actions: ["refresh-catalog"], course_count: 3 };
  click(byId["help-open-guide"]);
  await settle();
  assert.equal(visiblePanel(), "3", "认证就绪而目录未就绪时 dismiss 重开落在步骤 3");

  /* 9) 设置入口打开 → 退出回设置来源；账户菜单入口回当前页 */
  selectPage("settings");
  store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"], connected: true, configured: true });
  fixture.catalog = { state: "ready", code: "authorized_catalog_verified", actions: ["refresh-catalog"], course_count: 4 };
  click(byId["help-open-guide"]);
  await settle();
  assert.equal(visiblePanel(), "5", "核心全部就绪时 dismiss 重开落在步骤 5");
  click(byId["onboarding-next"]); /* 再次完成（幂等） */
  await settle();
  assert.equal(pageActive("settings"), true, "设置来源退出回设置页");
  byId["study-page"].classList.add("active");
  byId["study-page"].hidden = false;
  byId["settings-page"].classList.remove("active");
  byId["settings-page"].hidden = true;
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "已登录账户菜单入口可重开");
  selectPage("study");
  assert.equal(pageActive("study"), true, "关闭后回到账户菜单所在页面");

  /* 10) 迟到响应按序号丢弃；快速开关不覆盖新状态 */
  pendingAppShell = deferred();
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "重开引导");
  pendingAppShell.resolve(ok({ authentication: fixture.auth, catalog: fixture.catalog, remote: fixture.remote }));
  pendingAppShell = null; /* 恢复正常桩件，避免复用已消费的 Response */
  await settle();
  assert.equal(pageActive("onboarding"), true, "迟到响应不破坏已关闭状态");
  selectPage("study");
  assert.equal(pageActive("study"), true, "关闭回到 study");

  /* 11) 双 focus 快速刷新：最新序号胜出 */
  click(byId["account-menu-onboarding"]);
  await settle();
  fixture.catalog = { state: "ready", code: "authorized_catalog_verified", actions: [], course_count: 9 };
  windowTarget.dispatchEvent(new Event("focus"));
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  click(byId["onboarding-toc-3"]);
  assert.ok(guideText().includes("9 门可访问课程"), "并发刷新后渲染最新序号结果");
  selectPage("study");

  /* 12) 引导 DOM 输入框闭集：仅步骤 4 代理地址一处（绝不收集学号/密码/Key）+ 无课程内容 */
  const guideInputs = deepElements(byId["onboarding-page"]).filter((node) => node.tagName === "INPUT");
  assert.equal(guideInputs.length, 1, "引导输入框闭集：仅代理地址一处输入");
  assert.equal(guideInputs[0].id, "onboarding-proxy-input", "唯一输入框为代理地址");
  assert.notEqual(String(guideInputs[0].type || "").toLowerCase(), "password", "不收集密码类输入");
  store.set("courses", [{ course_id: "c1", title: "机密课程标题一", teacher: "某教师" }]);
  await settle();
  assert.equal(guideText().includes("机密课程标题一"), false, "课程名不进入引导 DOM");

  /* 13) 清理：移除 listener 后事件不再触发请求 */
  cleanup();
  const shellGetsBefore = calls.appShellGet;
  windowTarget.dispatchEvent(new Event("focus"));
  await settle();
  assert.equal(calls.appShellGet, shellGetsBefore, "cleanup 后 focus 不再触发读取");
  assert.equal(localStorageLog.length, 0, "整个场景未写 localStorage");
  console.log("scenario group A passed");
}

/* ================= 场景组 B：F-GATE-1 竞态窗注入钉 =================
   开在记前（根修合同）：mark-opened POST 拒绝/挂起（boot 突发拒载、响应丢失
   两形态）都不得没收引导——读到候选态的视觉面必弹，失败仅诚实一句话；
   下一载 auto_opened 仍 false 会再开（自愈语义钉）。 */

{
  buildDom();
  buildWindow();
  resetFixture();
  fixture.markOpenFail = true;
  const store = createStore();
  /* 新模块实例：autoOpenAttempted 每进程（实例）至多一次 */
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=b");
  await installOnboarding(store);
  await settle();
  assert.equal(calls.actionsPost.length, 1, "mark-opened POST 已尝试");
  assert.equal(pageActive("onboarding"), true, "写入失败不没收引导：候选态必弹");
  assert.ok(toastText().includes("新手引导状态暂时没保存上"), "失败给出诚实 toast（不再静默放弃）");
  /* 手动入口仍然可用 */
  click(byId["account-menu-onboarding"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "手动入口不受补记失败影响");
  assert.equal(visiblePanel(), "1", "记录仍为 new 时手动打开从步骤 1 开始");
}

/* 场景组 B2：mark-opened 挂起（响应永不返回=传输丢包形态）→ 引导仍打开。
   旧实现 await 挂起=整轮静默不弹；根修后挂起只拖延补记，不拖累首绘。 */
{
  buildDom();
  buildWindow();
  resetFixture();
  const store = createStore();
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=b2");
  /* 注入挂起：仅在 mark-opened 动作上永不 settle */
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/api/v3/onboarding/actions")) {
      return new Promise(() => { /* never settles */ });
    }
    return originalFetch(input, init);
  };
  try {
    await installOnboarding(store);
    await settle();
    assert.equal(pageActive("onboarding"), true, "mark-opened 挂起不没收引导");
  } finally {
    globalThis.fetch = originalFetch;
  }
}

/* ================= 场景组 C：auth 载荷附带校园连接快照 → 引导步骤 2 行为不变 ================= */

{
  buildDom();
  buildWindow();
  resetFixture();
  const fixtureConnection = JSON.parse(
    readFileSync(new URL("./fixtures/vpn_connection_contract_v1.json", import.meta.url), "utf8"),
  ).valid;
  const store = createStore();
  /* 新模块实例：每个 case 一份安装期状态 */
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=c");
  await installOnboarding(store);
  await settle();
  assert.equal(pageActive("onboarding"), true, "引导已打开");
  click(byId["onboarding-toc-2"]);
  /* auth 附加快照 + 未知字段：闭集证据文案与登录按钮行为与无快照完全一致 */
  store.set("auth", {
    state: "ready", code: "fudan_session_verified", actions: ["refresh-catalog", "logout"],
    connected: true, configured: true,
    connection: { ...fixtureConnection.ready_direct, future_field: { detail: "ignored" } },
  });
  await settle();
  assert.ok(guideText().includes("复旦课程平台已登录"), "附加快照不影响 ready 闭集文案");
  assert.equal(byId["onboarding-open-login"].hidden, true, "附加快照不影响登录按钮隐藏");
  store.set("auth", {
    state: "action_required", code: "fudan_credentials_missing", actions: ["login"],
    connected: false, configured: false,
    connection: fixtureConnection.login_required,
  });
  await settle();
  assert.ok(guideText().includes("尚未登录复旦课程平台"), "action_required 按既有证据闭集显示文案");
  assert.equal(byId["onboarding-open-login"].hidden, false, "需要登录时按钮在场");
  /* 快照里的连接文案绝不渗入引导页（引导只消费既有 auth 证据闭集） */
  assert.equal(guideText().includes("校园连接正常"), false, "连接卡文案不渗入引导页");
}

/* ================= 场景组 D：步骤 4 代理卡（自动检测/手填/保存，PROXY-AUTODETECT-1） ================= */

{
  buildDom();
  buildWindow();
  resetFixture();
  const store = createStore();
  /* 代理卡由 onboarding.js 动态 createElement：经 registry 深遍历按 id 取节点 */
  const proxyEl = (id) => deepElements(byId["onboarding-page"]).find((node) => node.id === id);
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=d");
  const cleanup = await installOnboarding(store);
  await settle();
  assert.equal(pageActive("onboarding"), true, "引导已打开");

  /* 1) 首次进入步骤 4：自动检测一次（闭集动作），not_found → 指引 + 手填 */
  click(byId["onboarding-toc-4"]);
  await settle();
  assert.equal(calls.settingsPost.length, 1, "进入步骤 4 自动检测恰好一次");
  assert.deepEqual(calls.settingsPost[0], { action: "detect-proxy" });
  assert.ok(guideText().includes("未检测到可用代理"), "not_found 显示闭集文案");
  assert.ok(guideText().includes("Clash 默认 7890"), "指引给出常见代理端口查法");
  assert.ok(guideText().includes("不支持 SOCKS 代理"), "指引明示仅支持 HTTP(S)");
  assert.equal(proxyEl("onboarding-proxy-manual").hidden, false, "检测失败显示手填行");
  assert.equal(proxyEl("onboarding-proxy-input").value, "http://127.0.0.1:6268", "手填输入以当前保存代理预填");

  /* 2) 重进步骤 4 不重复自动检测 */
  click(byId["onboarding-toc-1"]);
  click(byId["onboarding-toc-4"]);
  await settle();
  assert.equal(calls.settingsPost.length, 1, "每会话至多自动检测一次");

  /* 3) 「重新检测」显式触发；首个通过者胜 → found 预填检测端口 */
  fixture.proxyDetect = { status: "found", source: "scan", port: 7890 };
  click(proxyEl("onboarding-proxy-redetect"));
  await settle();
  assert.equal(calls.settingsPost.length, 2, "重新检测显式发起一次检测");
  assert.ok(guideText().includes("127.0.0.1:7890"), "found 显示检测端口");
  assert.equal(proxyEl("onboarding-proxy-input").value, "http://127.0.0.1:7890", "found 预填检测到的代理");

  /* 4) SOCKS 输入：闭集文案本地拦截，不发请求 */
  proxyEl("onboarding-proxy-input").value = "socks5://127.0.0.1:1080";
  click(proxyEl("onboarding-proxy-save"));
  await settle();
  assert.equal(calls.settingsPost.length, 2, "SOCKS 输入本地拦截不发送");
  assert.ok(guideText().includes("只支持 HTTP 或 HTTPS 代理地址"), "SOCKS 闭集错误文案");

  /* 4b) C3-R6：PAC 自动代理 → 闭集专用指引文案（绝不求值 PAC 脚本） */
  fixture.proxyDetect = { status: "pac_detected", source: "system_pac", port: null };
  click(proxyEl("onboarding-proxy-redetect"));
  await settle();
  assert.ok(guideText().includes("PAC 自动代理模式"), "pac_detected 显示专用指引而非「未找到」");
  assert.equal(proxyEl("onboarding-proxy-manual").hidden, false, "PAC 态仍显示手填行");

  /* 5) 保存走既有 update-network 管道（当前 mode + 输入值），成功复述 */
  proxyEl("onboarding-proxy-input").value = "127.0.0.1:7890";
  click(proxyEl("onboarding-proxy-save"));
  await settle();
  assert.equal(calls.settingsPost.length, 4, "保存发起一次 update-network（4b 的 PAC 重新检测占 1 次）");
  assert.deepEqual(
    calls.settingsPost[3],
    { action: "update-network", mode: "auto", proxy_url: "127.0.0.1:7890" },
  );
  assert.ok(guideText().includes("代理设置已保存"), "保存成功显示复述");
  assert.equal(proxyEl("onboarding-proxy-error").hidden, true, "成功后无错误行");

  /* 5b) N5FE-P4：不用代理是一等动作——一键 update-network mode=direct，
     直连学生不必理解模式语义；保存态复述与错误行为直连语义服务 */
  click(proxyEl("onboarding-proxy-direct"));
  await settle();
  assert.deepEqual(
    calls.settingsPost[4],
    { action: "update-network", mode: "direct", proxy_url: "" },
    "不用代理一键直连（闭集动作+空代理地址）",
  );
  assert.ok(guideText().includes("已选择不用代理"), "直连保存态诚实复述");
  assert.equal(proxyEl("onboarding-proxy-error").hidden, true, "直连成功无错误行");
  assert.equal(proxyEl("onboarding-proxy-direct").getAttribute("aria-busy"), "false", "直连钮收尾解除忙态（可再次点击）");

  /* 6) 保存失败：行内闭集提示，不留成功复述 */
  fixture.proxySaveFail = true;
  proxyEl("onboarding-proxy-input").value = "http://127.0.0.1:7891";
  click(proxyEl("onboarding-proxy-save"));
  await settle();
  assert.equal(calls.settingsPost.length, 6, "失败后可再次尝试保存（5b 直连占 1 次）");
  assert.ok(guideText().includes("代理保存失败"), "保存失败显示行内提示");

  /* 7) 检测服务失败：error 闭集文案（可重新检测或手填） */
  fixture.proxyDetectFail = true;
  fixture.proxyDetect = { status: "not_found", source: null, port: null };
  click(proxyEl("onboarding-proxy-redetect"));
  await settle();
  assert.ok(guideText().includes("检测暂时失败"), "检测失败显示 error 闭集文案");
  assert.equal(proxyEl("onboarding-proxy-manual").hidden, false, "error 下手填行仍可用");

  cleanup();
  assert.equal(calls.other.length, 0, "代理卡只使用闭集 settings/actions 动作");
  console.log("scenario group D passed");
}

assert.equal(calls.other.length, 0, "全程未触碰认证/远程/设置等其它路由");
assert.equal(localStorageLog.length, 0, "localStorage 零写入（theme 也不由引导负责）");

/* N5FE-P2：目录码副句「登录仍然有效」按会话真值分支（引导页常在登录前） */
{
  const { evidenceDetails } = await import("../frontend/modules/ui.js");
  const timeout = { code: "catalog_timeout", state: "degraded" };
  const notLoggedIn = evidenceDetails(timeout, { loggedIn: false });
  assert.match(notLoggedIn.impact, /尚未登录复旦课程平台/, "未登录副句改为先登录指引");
  assert.doesNotMatch(notLoggedIn.impact, /登录仍然有效/, "未登录不再出现失实宣称");
  assert.equal(notLoggedIn.title, "课程目录连接超时", "标题不变");
  assert.match(evidenceDetails(timeout, { loggedIn: true }).impact, /登录仍然有效/, "已登录保持既有副句");
  assert.match(evidenceDetails(timeout).impact, /登录仍然有效/, "未知会话态保持既有文案（向后兼容）");
  for (const code of ["catalog_route_unavailable", "catalog_bearer_missing", "catalog_payload_invalid"]) {
    assert.doesNotMatch(evidenceDetails({ code }, { loggedIn: false }).impact, /登录仍然有效/, `${code} 同族分支`);
  }
}

/* A11Y-IMPL-1（D14 P3-2）：页题随页更新——任务栏/Alt+Tab 可辨识。页名=闭集表
   既有页名词（静态壳页头 h1/账户菜单同源），未知页回落纯 CourseLens 不开新词。 */
{
  selectPage("settings");
  assert.equal(document.title, "设置 · CourseLens", "设置页页题更新");
  selectPage("data");
  assert.equal(document.title, "数据管理 · CourseLens", "数据管理页页题更新");
  selectPage("onboarding");
  assert.equal(document.title, "新手引导 · CourseLens", "引导页页题用既有菜单页名词");
  selectPage("live");
  assert.equal(document.title, "直播 · CourseLens", "直播页页题更新");
  selectPage("mystery-page");
  assert.equal(document.title, "CourseLens", "未知页回落纯 CourseLens（不开新词）");
  selectPage("study");
  assert.equal(document.title, "学习 · CourseLens", "学习页页题更新");
}

/* ================= 场景组 E（F1 → LANDING-AESTHETIC-1 用户裁决）：引导挂起 →
   提示行退出着陆页（诗页零打扰），唯一宿主=选课面板概览顶部 ================= */
/* auto-open 恰一次是设计取舍；挂起态（disposition=new + 已 mark-opened）刷新/
   重开后着陆页恒静（恢复路径=账户菜单/设置帮助既有入口不灭）；学生进选课面板
   时提示行在概览顶部可见，一次点击直达 resumeOrOpenGuide；完成/跳过后消失。 */
{
  buildDom();
  const { windowTarget } = buildWindow();
  resetFixture();
  fixture.guide = {
    schema: GUIDE_SCHEMA, version: GUIDE_VERSION, disposition: "new",
    auto_opened: true, persistence: "ready", source: "stored", updated_at: 1,
  };
  const store = createStore();
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=e");
  await installOnboarding(store);
  await settle();
  /* 1) 挂起态：auto-open 早退（不再抢占），着陆页提示行恒隐（用户裁决：诗页零打扰） */
  assert.equal(calls.actionsPost.length, 0, "auto_opened=true 不再自动打开");
  assert.equal(pageActive("onboarding"), false, "挂起态留在主页");
  assert.equal(byId["study-empty"].hidden, false, "着陆页可见（刷新场景前提）");
  assert.equal(byId["home-guide-resume"].hidden, true, "着陆页不出现提示行（诗页零打扰）");
  assert.equal(byId["home-guide-resume-action"].textContent, "继续新手引导", "提示行动作文案闭集");

  /* 2) 进选课面板：提示行在概览顶部现身（唯一宿主），一次点击直达恢复链 */
  byId["study-empty"].hidden = true;
  byId["study-select"].hidden = false;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:study-mode", { detail: "select" }));
  assert.equal(byId["home-guide-resume"].hidden, false, "面板开=提示行在概览顶部可见");
  assert.equal(byId["home-guide-resume"].parentElement, byId["home-overview"], "宿主=概览顶部原位");
  click(byId["home-guide-resume-action"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "提示行一键回引导（resumeOrOpenGuide）");
  assert.equal(byId["home-guide-resume"].hidden, true, "引导开着时提示行退场");

  /* 3) 跳过写入 dismissed → 回面板提示行不再出现 */
  click(byId["onboarding-skip"]);
  await settle();
  assert.equal(fixture.guide.disposition, "dismissed", "dismiss 写入生效");
  assert.equal(pageActive("study"), true, "跳过回到来源页");
  byId["study-select"].hidden = false;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:study-mode", { detail: "select" }));
  assert.equal(byId["home-guide-resume"].hidden, true, "跳过后提示行消失");
}

/* F1 补面：已完成/已跳过档案落主页，提示行恒不可见（不给已完成的学生无用提示） */
{
  buildDom();
  buildWindow();
  resetFixture();
  fixture.guide = {
    schema: GUIDE_SCHEMA, version: GUIDE_VERSION, disposition: "completed",
    auto_opened: true, persistence: "ready", source: "stored", updated_at: 2,
  };
  const store = createStore();
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=e2");
  await installOnboarding(store);
  await settle();
  assert.equal(pageActive("onboarding"), false, "completed 不自动打开");
  assert.equal(byId["home-guide-resume"].hidden, true, "已完成档案提示行不可见");
}

/* F1 补面：读取失败不猜状态——提示行保持隐藏（诚实缺省） */
{
  buildDom();
  buildWindow();
  resetFixture();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "synthetic", error_code: "runtime_failed" }), { status: 500 });
  try {
    const store = createStore();
    const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=e3");
    await installOnboarding(store);
    await settle();
    assert.equal(byId["home-guide-resume"].hidden, true, "读取失败不显示提示行（状态未知不挂提示）");
  } finally {
    globalThis.fetch = realFetch;
  }
}

/* D-20261009-09 → LANDING-AESTHETIC-1（用户裁决 2026-10-09）：刷新场景——挂起行
   不再上着陆页（诗页零打扰；「刷新场景整行 0×0」的宿主问题随问候 hero 宿主
   退役而消失）；唯一宿主=选课面板概览顶部，学习桌态行自身也退场（桌面零新增
   浮面）。过场广播=courselens:study-mode（study.js renderStudyMode 模式变更时
   派发）。 */
{
  buildDom();
  const { windowTarget } = buildWindow();
  resetFixture();
  fixture.guide = {
    schema: GUIDE_SCHEMA, version: GUIDE_VERSION, disposition: "new",
    auto_opened: true, persistence: "ready", source: "stored", updated_at: 3,
  };
  const store = createStore();
  const { installOnboarding } = await import("../frontend/modules/onboarding.js?case=e4");
  await installOnboarding(store);
  await settle();
  const row = byId["home-guide-resume"];
  /* 1) 刷新落地（empty 态，选课面板未开）：着陆页恒静（用户裁决：诗页零打扰）。 */
  assert.equal(byId["study-empty"].hidden, false, "落地问候 hero 可见（empty 态前提）");
  assert.equal(byId["study-select"].hidden, true, "选课面板未开（刷新场景前提）");
  assert.equal(row.hidden, true, "刷新场景着陆页不出现提示行（诗页零打扰）");
  assert.equal(row.parentElement, byId["home-overview"], "宿主=选课面板概览（静态原位）");
  /* 2) 打开选课面板（过场广播）：行在概览顶部可见。 */
  byId["study-empty"].hidden = true;
  byId["study-select"].hidden = false;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:study-mode", { detail: "select" }));
  assert.equal(row.parentElement, byId["home-overview"], "面板开=概览宿主原位");
  assert.equal(row.hidden, false, "面板开行可见");
  assert.equal(byId["home-overview"].children[0], row, "概览顶部原位");
  /* 3) 进学习桌（面板隐藏）：行随容器不可见且自身退场——桌面零新增浮面。 */
  byId["study-select"].hidden = true;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:study-mode", { detail: "desk" }));
  assert.equal(row.parentElement, byId["home-overview"], "桌态行留在概览宿主内（随面板隐藏）");
  assert.equal(row.hidden, true, "桌态提示行退场");
  /* 4) 回选择面：行恢复可见。 */
  byId["study-select"].hidden = false;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:study-mode", { detail: "select" }));
  assert.equal(row.hidden, false, "回选择面行可见");
  /* 5) 点击恢复行动作：直达引导，行退场（宿主固定不伤既有恢复链）。 */
  click(byId["home-guide-resume-action"]);
  await settle();
  assert.equal(pageActive("onboarding"), true, "恢复链一次点击直达");
  assert.equal(row.hidden, true, "引导开着时行退场");
}

console.log("frontend onboarding guide behavior passed");
