import assert from "node:assert/strict";

// ---- 文稿检索 Phase A 行为 harness（G5/P10 缺口④，PLAYER-P0-REMAINDER-1 单元二）
// ---- 与 frontend_playback_recovery_behavior.mjs 同族桩约定：FakeElement 的
// ---- textContent 不聚合子节点、写入 currentTime 即派发 seeking、伪 DOM 须按
// ---- 真实页面标记播种。installStudy 的安装面（目录/快照/资料九路加载）在此
// ---- 全部以闭集路由或诚实 404 承接，只有字幕 segments 路由给出真实数据。

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  toggle(value, enabled) {
    if (enabled) this.values.add(value);
    else this.values.delete(value);
  }
}

class FakeStyle {
  constructor() {
    this.properties = new Map();
  }

  setProperty(name, value) {
    this.properties.set(String(name), String(value));
  }

  getPropertyValue(name) {
    return this.properties.has(String(name)) ? this.properties.get(String(name)) : "";
  }
}

const scrollCalls = [];

class FakeElement extends EventTarget {
  #currentTime = 0;

  constructor(id = "") {
    super();
    this.id = id;
    this.tagName = "";
    this.parent = null;
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.style = new FakeStyle();
  }

  closest(selector) {
    const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
    let node = this;
    while (node) {
      for (const token of tokens) {
        const matched = token.startsWith("#")
          ? node.id === token.slice(1)
          : token.startsWith(".")
            ? Boolean(node.classList && node.classList.values && node.classList.values.has(token.slice(1)))
            : String(node.tagName || "").toLowerCase() === token.toLowerCase();
        if (matched) return node;
      }
      node = node.parent;
    }
    return null;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    /* 真实 DOM 把 DocumentFragment 解包为其子节点（loadTranscript 用 fragment 一次替换） */
    this.children = nodes.flatMap((node) => (node && node.isFragment ? node.children : [node]));
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

  /* 桩面 querySelector 家族：容器内没有子结构树，返回空集/null 即可
     （真实代码路径对空结果全部安全） */
  querySelectorAll() {
    return [];
  }

  querySelector() {
    return null;
  }

  get currentTime() {
    return this.#currentTime;
  }

  set currentTime(value) {
    this.#currentTime = value;
    this.dispatchEvent(new Event("seeking"));
  }

  /* 引擎侧位置写入：播放推进/命中跳转后的真实位置，不派发 seeking */
  writePlaybackTime(value) {
    this.#currentTime = value;
  }

  play() {
    this.paused = false;
    return Promise.resolve();
  }

  pause() {
    this.paused = true;
  }

  scrollIntoView(options) {
    scrollCalls.push({ id: this.id, options });
  }
}

class FakeCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}

function createStudyPage() {
  const ids = [
    // 学习页三态与目录
    "study-empty", "study-select", "study-desk", "study-start-select",
    "study-back-select", "study-back-courses", "study-course-list",
    "study-course-title", "study-course-meta", "study-lecture-list",
    "refresh-catalog", "catalog-term-filter", "copy-catalog-diagnostics",
    "catalog-recovery", "catalog-recovery-title", "catalog-recovery-impact",
    "catalog-recovery-actions", "catalog-evidence", "catalog-diagnostic-code",
    "diagnose-network", "topbar-crumbs",
    // 播放器动作行（study.js 引用）
    "player-stage",
    // 字幕面板与检索
    "transcript-list", "reload-transcript", "transcript-mode-note",
    "transcript-search-input", "transcript-search-state", "transcript-search-results",
    // 书签
    "bookmark-list", "bookmark-action-state",
    // 资料/笔记/课件/试卷/复习/概念/分析
    "artifact-kind", "artifact-overview", "artifact-overview-title",
    "artifact-takeaways", "artifact-takeaways-title", "artifact-chapters",
    "artifact-chapters-title", "artifact-key-moments", "artifact-key-moment-list",
    "artifact-structured", "artifact-content", "artifact-source", "artifact-notices",
    "document-list", "document-input", "courseware-surface", "courseware-info",
    "courseware-notes", "courseware-notes-counts", "courseware-pdf-state",
    "courseware-pdf-detail", "courseware-pdf-download", "generate-courseware-pdf",
    "quiz-list", "generate-quiz", "review-list", "concept-list", "analyze-concepts",
    "analytics-summary", "exam-context", "exam-context-row", "exam-today-list",
    "exam-today-state", "ask-lecture",
    // 云控制面（快照 404 后的降级渲染目标）
    "cloud-control-card", "cloud-control-state", "cloud-control-actions",
    "course-automation-live",
    // 全局
    "toast-region",
  ];
  const byId = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  const tabs = new FakeElement("materials-tabs");
  tabs.classList.values.add("materials-tabs");
  const lecturePane = new FakeElement("lecture-pane");
  const bySelector = new Map([
    [".materials-tabs", tabs],
    [".course-layout > .lecture-pane", lecturePane],
  ]);
  let created = 0;
  const documentTarget = new EventTarget();
  globalThis.document = Object.assign(documentTarget, {
    getElementById: (id) => byId[id] || null,
    createElement: () => new FakeElement(`created-${created += 1}`),
    createElementNS: () => new FakeElement(`created-ns-${created += 1}`),
    createDocumentFragment: () => {
      const fragment = new FakeElement(`fragment-${created += 1}`);
      fragment.isFragment = true;
      return fragment;
    },
    querySelector: (selector) => bySelector.get(String(selector)) || null,
    querySelectorAll: () => [],
    activeElement: null,
  });
  const elements = {
    ...byId,
    transcriptList: byId["transcript-list"],
    transcriptSearchInput: byId["transcript-search-input"],
    transcriptSearchState: byId["transcript-search-state"],
    transcriptSearchResults: byId["transcript-search-results"],
    playerStage: byId["player-stage"],
  };
  return { elements, byId };
}

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
    tasks: [],
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
windowTarget.setTimeout = (callback, ms) => setTimeout(callback, ms);
windowTarget.clearTimeout = (id) => clearTimeout(id);
windowTarget.requestAnimationFrame = (callback) => setImmediate(() => callback(0));
windowTarget.cancelAnimationFrame = () => {};
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
globalThis.window = windowTarget;
globalThis.CustomEvent = FakeCustomEvent;
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.matchMedia = () => ({
  matches: false,
  addEventListener: () => {},
  removeEventListener: () => {},
});
globalThis.CSS = { escape: (value) => String(value) };

/* 字幕 segments 闭集数据：中英混排 + 跨分钟时间戳 */
const segmentSets = new Map([
  ["s-1", [
    { start_ms: 0, end_ms: 8000, text: "今天讲积分上限的两种取法" },
    { start_ms: 8000, end_ms: 20000, text: "Fourier transform is the key idea" },
    { start_ms: 20000, end_ms: 30000, text: "积分上限取法会影响收敛速度" },
  ]],
  ["s-2", [
    { start_ms: 0, end_ms: 5000, text: "第二讲开场" },
  ]],
]);

function ok(data, status = 200) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (path) => {
  const route = String(path);
  if (route.startsWith("/api/v3/subtitles/segments?")) {
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ segments: segmentSets.get(subId) || [] });
  }
  if (route.startsWith("/api/v3/bookmarks?")) {
    return ok({ bookmarks: [] });
  }
  /* 其余九路加载（artifacts/documents/quizzes/concepts/analytics/courseware-pdf/
     automation 等）诚实 404：loaders 各自闭集降级，不得让安装路径崩溃 */
  return new Response(JSON.stringify({ error: "synthetic route omitted" }), {
    status: 404,
    headers: { "Content-Type": "application/json" },
  });
};

const { installStudy } = await import("../frontend/modules/study.js");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  await nextTurn();
  await nextTurn();
  await nextTurn();
};

function keyEvent(key, { type = "keydown", shiftKey = false } = {}) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  event.key = key;
  event.shiftKey = shiftKey;
  return event;
}

async function verifyTranscriptSearchBehavior() {
  const page = createStudyPage();
  const store = createStore();
  const input = page.elements.transcriptSearchInput;
  const state = page.elements.transcriptSearchState;
  const results = page.elements.transcriptSearchResults;
  const list = page.elements.transcriptList;
  const player = page.elements.playerStage;
  /* 与真实页面标记一致：检索框与命中区随 index.html 播种 */
  input.tagName = "input";
  const cleanup = await installStudy(store);
  store.set("activeLecture", { course_id: "c-1", sub_id: "s-1", sub_title: "检索讲次", can_stream: true });
  await settle();
  assert.equal(list.children.length, 3, "文稿三行渲染");
  assert.equal(list.hidden, false);
  assert.equal(state.hidden, true, "未检索时状态行安静");

  // A) 命中：检索视图替换列表视图；命中计数；首命中带 <mark> 高亮
  input.value = "积分";
  input.dispatchEvent(new Event("input"));
  assert.equal(list.hidden, true, "检索激活时列表视图让位");
  assert.equal(results.hidden, false);
  assert.equal(results.children.length, 2, "「积分」命中 2 行");
  assert.equal(state.textContent, "第 1 处，共 2 处（Enter 下一个 / Shift+Enter 上一个）", "U9 计数闭集文案");
  assert.equal(state.hidden, false);
  const firstHitBody = results.children[0].children[1];
  const marks = firstHitBody.children.filter((node) => typeof node !== "string");
  assert.equal(marks.length, 1, "首命中恰一处 <mark>");
  assert.equal(marks[0].textContent, "积分", "mark 承载命中文本");
  assert.equal(results.children[0].children[0].textContent, "0:00", "命中行时间戳格式");
  assert.equal(results.children[1].children[0].textContent, "0:20", "跨分钟时间戳格式");

  // A2) 命中点击 seek：复用 timestamp-button 通道（currentTime=startMs/1000 + 播放）
  results.children[1].children[0].click();
  assert.equal(player.currentTime, 20, "命中点击跳到该句起点");
  assert.equal(player.paused, false, "命中点击按既有语义继续播放");

  // B) 大小写折叠 + Enter 跳第一个命中；再 Enter 巡览、Shift+Enter 回退（U9）
  input.value = "fourier";
  input.dispatchEvent(new Event("input"));
  assert.equal(results.children.length, 1, "英文大小写折叠命中");
  const enterEvent = keyEvent("Enter");
  input.dispatchEvent(enterEvent);
  assert.equal(player.currentTime, 8, "Enter 跳第一个命中（0:08）");
  assert.equal(enterEvent.defaultPrevented, true, "Enter 消费后阻止默认");

  // B2) U9 巡览：多命中时 Enter 前进、Shift+Enter 后退，状态行跟随，活动行高亮
  input.value = "积分";
  input.dispatchEvent(new Event("input"));
  assert.equal(results.children[0].classList.values.has("transcript-hit-active"), true, "渲染即高亮第 1 处");
  input.dispatchEvent(keyEvent("Enter"));  // Enter：前进到第 2 处
  assert.equal(player.currentTime, 20, "Enter 前进到第 2 处（0:20）");
  assert.match(state.textContent, /第 2 处，共 2 处/, "状态行跟随光标");
  assert.equal(results.children[1].classList.values.has("transcript-hit-active"), true, "活动行高亮");
  input.dispatchEvent(keyEvent("Enter", { shiftKey: true }));
  assert.equal(player.currentTime, 0, "Shift+Enter 回退到第 1 处");
  assert.equal(results.children[0].classList.values.has("transcript-hit-active"), true, "高亮跟随回退");
  input.dispatchEvent(keyEvent("Enter"));
  assert.equal(player.currentTime, 20, "Shift 后再 Enter 回到前进方向");

  // C) 空结果诚实态（G6 文案逐字）
  input.value = "量子引力";
  input.dispatchEvent(new Event("input"));
  assert.equal(state.hidden, false);
  assert.equal(state.textContent, "文稿里没找到「量子引力」。试试更短一点的词？", "空结果人话文案");
  assert.equal(results.hidden, true, "零命中不出空命中区");
  assert.equal(list.hidden, true, "检索态下列表保持让位");

  // D) Esc 清空回到文稿列表，查询残留不保留
  input.dispatchEvent(keyEvent("Escape"));
  assert.equal(input.value, "", "Esc 清空检索框");
  assert.equal(list.hidden, false, "Esc 后列表视图恢复");
  assert.equal(state.hidden, true);
  assert.equal(results.hidden, true);

  // E) 与手动滚动保护共存：守卫窗内跟随只高亮不滚动；过期后恢复滚动
  list.dispatchEvent(new Event("wheel"));
  const scrollCallsBefore = scrollCalls.length;
  window.dispatchEvent(new FakeCustomEvent("courselens:transcript-time", {
    detail: { sub_id: "s-1", time_ms: 25000 },
  }));
  assert.equal(list.children[2].classList.values.has("active"), true, "守卫窗内仍高亮当前句");
  assert.equal(scrollCalls.length, scrollCallsBefore, "手动滚动保护期内不强制滚动");
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 4000; /* 越过 2500ms 守卫窗 */
    window.dispatchEvent(new FakeCustomEvent("courselens:transcript-time", {
      detail: { sub_id: "s-1", time_ms: 10000 },
    }));
  } finally {
    Date.now = realNow;
  }
  assert.equal(list.children[1].classList.values.has("active"), true, "守卫过期后跟随新句");
  assert.equal(scrollCalls.length, scrollCallsBefore + 1, "守卫过期恢复滚动跟随");

  // F) 焦点让位：跟随滚动/检索输入都不移动焦点，播放推进时打字不受扰
  globalThis.document.activeElement = input;
  window.dispatchEvent(new FakeCustomEvent("courselens:transcript-time", {
    detail: { sub_id: "s-1", time_ms: 0 },
  }));
  assert.equal(globalThis.document.activeElement, input, "跟随滚动不抢焦点");
  input.value = "收敛";
  input.dispatchEvent(new Event("input"));
  assert.match(state.textContent, /第 1 处，共 1 处/, "播放推进时检索照常工作");
  assert.equal(list.children[0].classList.values.has("active"), true, "高亮链与检索互不覆盖");
  input.dispatchEvent(keyEvent("Escape"));

  // G) 讲次切换重置检索：命中视图属于旧文稿，绝不跨讲次残留
  input.value = "积分";
  input.dispatchEvent(new Event("input"));
  assert.equal(list.hidden, true);
  store.set("activeLecture", { course_id: "c-1", sub_id: "s-2", sub_title: "第二讲", can_stream: true });
  await settle();
  assert.equal(input.value, "", "讲次切换清空检索框");
  assert.equal(list.hidden, false, "讲次切换回到列表视图");
  assert.equal(list.children.length, 1, "新讲次文稿渲染");
  assert.equal(state.hidden, true);
  assert.equal(results.hidden, true);

  // H) 卸载冻结：检索监听全部解除
  cleanup();
  input.value = "积分";
  input.dispatchEvent(new Event("input"));
  assert.equal(list.hidden, false, "卸载后检索不再改变视图");
  assert.equal(state.hidden, true, "卸载后状态行保持安静");

  scrollCalls.length = 0;
  globalThis.document.activeElement = null;
}

await verifyTranscriptSearchBehavior();
console.log("frontend transcript search behavior passed");
