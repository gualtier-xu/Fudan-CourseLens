import assert from "node:assert/strict";

// ---- 文稿窗口化渲染行为 harness（VTT-PERF）----
// 与 frontend_transcript_search_behavior.mjs 同族桩约定；本钉覆盖大讲次
// （> CHUNK×KEEP 行）的窗口化路径语义零回退：
//   1. 初始只渲染 KEEP_CHUNKS 窗口行，远端以垫片占位（DOM 规模有硬帽）；
//   2. 跟随高亮：深位时间戳事件先滑窗保行再高亮（行未上屏也必达）；
//   3. 深位行时间戳点击照常 seek（窗口化路径交互与全量路径同语义）；
//   4. 检索全量数据源：未渲染区段的命中照常出现（检索独立于列表 DOM）；
//   5. 滚动滑窗：滚动事件后窗口滑动，远端块被拆除回收（DOM 帽不破）；
//   6. 小讲次走全量路径：无垫片、children=纯行（与既有 DOM 逐字节一致）。

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

  get parentNode() {
    return this.parent;
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

  #own(node) {
    if (node && node.isFragment) {
      for (const child of node.children) child.parent = this;
      return node.children;
    }
    if (node) node.parent = this;
    return [node];
  }

  append(...nodes) {
    for (const node of nodes) this.children.push(...this.#own(node));
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = nodes.flatMap((node) => this.#own(node));
  }

  insertBefore(node, anchor) {
    const incoming = this.#own(node);
    const at = this.children.indexOf(anchor);
    if (at < 0) {
      this.children.push(...incoming);
      return node;
    }
    this.children.splice(at, 0, ...incoming);
    return node;
  }

  removeChild(node) {
    const at = this.children.indexOf(node);
    if (at >= 0) this.children.splice(at, 1);
    node.parent = null;
    return node;
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

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

let rectTopOf = 0;

function createStudyPage() {
  const ids = [
    "study-empty", "study-select", "study-desk", "study-start-select",
    "study-back-select", "study-back-courses", "study-course-list",
    "study-course-title", "study-course-meta", "study-lecture-list",
    "refresh-catalog", "catalog-term-filter", "copy-catalog-diagnostics",
    "catalog-recovery", "catalog-recovery-title", "catalog-recovery-impact",
    "catalog-recovery-actions", "catalog-evidence", "catalog-diagnostic-code",
    "diagnose-network", "topbar-crumbs",
    "player-stage",
    "transcript-list", "reload-transcript", "transcript-mode-note",
    "transcript-search-input", "transcript-search-state", "transcript-search-results",
    "bookmark-list", "bookmark-action-state",
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
    "cloud-control-card", "cloud-control-state", "cloud-control-actions",
    "course-automation-live",
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
  const list = byId["transcript-list"];
  /* 页面滚动分支桩：视口相对列表内容顶端的位置可滚动推进 */
  list.getBoundingClientRect = () => ({ top: rectTopOf, height: 800, width: 600 });
  const elements = {
    ...byId,
    transcriptList: list,
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
      listeners.get(key)?.add(listener);
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

/* 大讲次闭集：1500 段（= 8 块 > KEEP_CHUNKS×CHUNK 行）+ 小讲次 3 段 */
const CHUNK = 200;
const KEEP = 6;
function bigSegments(count = CHUNK * 8) {
  const segments = [];
  for (let index = 0; index < count; index += 1) {
    segments.push({
      start_ms: index * 4000,
      end_ms: index * 4000 + 3800,
      text: `深位检索哨兵第${index}句`,
    });
  }
  return segments;
}
const segmentSets = new Map([
  ["w-big", bigSegments()],
  ["w-small", [
    { start_ms: 0, end_ms: 4000, text: "小讲次第一句" },
    { start_ms: 4000, end_ms: 8000, text: "小讲次第二句" },
    { start_ms: 8000, end_ms: 12000, text: "小讲次第三句" },
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
  if (route.startsWith("/api/v3/bookmarks?")) return ok({ bookmarks: [] });
  return new Response(JSON.stringify({ error: "synthetic route omitted" }), {
    status: 404,
    headers: { "Content-Type": "application/json" },
  });
};

const { installStudy } = await import("../frontend/modules/study.js");

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async (turns = 4) => {
  for (let index = 0; index < turns; index += 1) await nextTurn();
};

async function verifyWindowedBehavior() {
  const page = createStudyPage();
  const store = createStore();
  const list = page.elements.transcriptList;
  const player = page.elements.playerStage;
  const cleanup = await installStudy(store);
  store.set("activeLecture", { course_id: "c-1", sub_id: "w-big", sub_title: "体量讲次", can_stream: true });
  await settle();

  /* 1) 初始窗口：垫片+窗口行，DOM 帽=KEEP×CHUNK 行 + 双垫片 */
  const total = CHUNK * 8;
  assert.equal(list.children.length, KEEP * CHUNK + 2, "初始只上屏窗口行+双垫片");
  assert.equal(list.children[0].getAttribute("aria-hidden"), "true", "顶部垫片对读屏隐藏");
  assert.equal(list.children[list.children.length - 1].getAttribute("aria-hidden"), "true", "底部垫片对读屏隐藏");
  assert.equal(list.getAttribute("role"), "list", "窗口化保持 list 语义");
  const firstRow = list.children[1];
  assert.equal(firstRow.className, "transcript-row", "窗口行与全量路径同结构");
  assert.ok(firstRow.dataset.startMs != null, "窗口行携带时间戳数据集");

  /* 2) 跟随高亮：深位（第 1400 段，chunk 7 未渲染）时间戳事件必达 */
  const deepIndex = 1400;
  window.dispatchEvent(new FakeCustomEvent("courselens:transcript-time", {
    detail: { sub_id: "w-big", time_ms: deepIndex * 4000 + 100 },
  }));
  await settle();
  const renderedRows = list.children.filter((node) => node.className === "transcript-row");
  assert.ok(renderedRows.length > 0 && renderedRows.length <= KEEP * CHUNK + CHUNK, "滑窗后 DOM 帽不破");
  const activeRow = renderedRows.find((node) => node.classList.values.has("active"));
  assert.ok(activeRow, "深位目标行已滑窗上屏并高亮");
  assert.equal(activeRow.dataset.startMs, String(deepIndex * 4000), "高亮行=时间戳对应行");
  assert.ok(
    scrollCalls.some((call) => call.options && call.options.block === "nearest"),
    "跟随滚动仍走 scrollIntoView nearest",
  );

  /* 3) 深位行时间戳点击照常 seek（窗口化路径语义零回退） */
  scrollCalls.length = 0;
  const seekButton = activeRow.children.find((node) => node.className === "timestamp-button");
  seekButton.click();
  assert.equal(player.currentTime, (deepIndex * 4000) / 1000, "深位行 seek 生效");
  assert.equal(player.paused, false, "seek 后自动播放语义保持");

  /* 4) 检索全量数据源：未渲染区段命中照常出现 */
  const input = page.elements.transcriptSearchInput;
  const results = page.elements.transcriptSearchResults;
  input.tagName = "input";
  input.value = "深位检索哨兵第1499句";
  input.dispatchEvent(new Event("input"));
  assert.equal(list.hidden, true, "检索激活时列表让位");
  assert.equal(results.children.length, 1, "未渲染区段命中可达");
  input.value = "";
  input.dispatchEvent(new Event("input"));
  assert.equal(list.hidden, false, "清空检索恢复列表");

  /* 5) 滚动滑窗：模拟视口推进到列表尾部（chunk 7），远端 chunk 0-1 拆除 */
  rectTopOf = -total * 57 + 400; /* 视口推到近列表尾 */
  document.dispatchEvent(new Event("scroll"));
  await settle(6);
  const tailRows = list.children.filter((node) => node.className === "transcript-row");
  const startMsSet = new Set(tailRows.map((node) => Number(node.dataset.startMs)));
  assert.ok(startMsSet.has((total - 1) * 4000), "尾块行已上屏");
  assert.ok(!startMsSet.has(0), "远端首块行已拆除回收");
  assert.ok(tailRows.length <= KEEP * CHUNK + CHUNK, "滚动滑窗 DOM 帽不破");

  /* 6) 切讲全量路径：小讲次无垫片、children=纯行 */
  store.set("activeLecture", { course_id: "c-1", sub_id: "w-small", sub_title: "小讲次", can_stream: true });
  await settle();
  assert.equal(list.children.length, 3, "小讲次走全量路径");
  assert.equal(list.children[0].className, "transcript-row", "全量路径无垫片");
  assert.equal(list.children[0].getAttribute("aria-hidden"), null, "全量路径零垫片语义");

  await cleanup();
  console.log("frontend transcript window behavior passed");
}

await verifyWindowedBehavior();
process.exit(0);
