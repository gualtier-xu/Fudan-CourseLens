import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

/* RR-P4FSRS-1 闪卡复习专项矩阵。
 *
 * 覆盖：零 LLM 闪卡视图的渲染（计数头/进度/卡面）、翻面交互（点击+键盘）、
 * 评分推进与下一间隔提示、评分失败不推进、证据锚点跳转（无锚可读不可点）、
 * 会话完成视图、无产物诚实空态+行动指引（含缺引用锚分型）、取数失败诚实错误、
 * 合同不符诚实错误、换课重取、CSS 注入、畸形单元防御；以及 QA-SWEEP-1 定向钉：
 * P1-7 换课乱序三守卫、P2-9 评分结果 state 计数同步、P2-10 键盘按钮让位+防双翻；
 * CSS-FIX-1 定向钉：焦点恢复（翻面/评分/完成视图不断键盘流，冷渲染不抢焦点）、
 * 评分在途 aria-busy 提交中线索、进度/完成 aria-live 播报位；FLASH-FIX-2 定向钉：
 * is-flip 悬停字色压回全局 hover 翻写（静态级联钉，深底白字对比回归门）。
 *
 * 夹具说明：沿用 N7F course-review 矩阵的自建真树 mini-DOM（支持属性选择器
 * 与事件冒泡），apiV3 走计划内路由网络桩。 */
/* eslint-disable no-unused-vars */

/* ---------------- mini-DOM ---------------- */

const ATTR_SELECTOR = /\[([a-zA-Z][a-zA-Z0-9-]*)(?:([~^$*|]?=)['"]?([^'"\]]*)['"]?)?\]/g;

class Node extends EventTarget {
  constructor(tag, id = "") {
    super();
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this.className = "";
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this.disabled = false;
    this.tabIndex = 0;
    this.value = "";
    this.open = false;
    this.scrollTop = 0;
    this._text = "";
    this.classList = {
      add: (...names) => { const s = new Set(this._classes()); names.forEach((n) => s.add(n)); this.className = [...s].join(" "); },
      remove: (...names) => { const drop = new Set(names); this.className = this._classes().filter((n) => !drop.has(n)).join(" "); },
      contains: (name) => this._classes().includes(name),
      toggle: (name, enabled) => {
        if (enabled === undefined) { this.classList.contains(name) ? this.classList.remove(name) : this.classList.add(name); return this.classList.contains(name); }
        enabled ? this.classList.add(name) : this.classList.remove(name);
        return Boolean(enabled);
      },
    };
  }
  _classes() { return String(this.className || "").split(/\s+/).filter(Boolean); }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  setAttribute(name, value) {
    const key = String(name);
    this.attributes.set(key, String(value));
    if (key === "hidden") this.hidden = true;
    if (key === "disabled") this.disabled = true;
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
  }
  getAttribute(name) {
    const key = String(name);
    if (key === "hidden") return this.hidden ? "" : null;
    return this.attributes.has(key) ? this.attributes.get(key) : null;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "hidden") this.hidden = false;
  }
  append(...nodes) {
    for (const node of nodes) {
      if (!node) continue;
      if (node.parent) node.parent.children = node.parent.children.filter((n) => n !== node);
      node.parent = this;
      this.children.push(node);
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parent = null; });
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((n) => n !== this); this.parent = null; }
  closest(selector) { let current = this; while (current) { if (matchesSelector(current, selector)) return current; current = current.parent; } return null; }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  dispatchEvent(event) {
    const path = [];
    let node = this;
    while (node) { path.push(node); node = node.parent; }
    if (!event.bubbles) path.length = 1;
    for (const ancestor of path) {
      Object.defineProperty(event, "target", { value: this, configurable: true, writable: true });
      /* 真实 DOM 的 stopPropagation 会阻断后续祖先监听器；Node 的 Event
         不暴露该标志，这里包一层可读标记让冒泡语义与浏览器一致。 */
      let stopped = false;
      const nativeStop = typeof event.stopPropagation === "function" ? event.stopPropagation.bind(event) : () => {};
      Object.defineProperty(event, "stopPropagation", {
        value: () => { stopped = true; nativeStop(); },
        configurable: true, writable: true,
      });
      EventTarget.prototype.dispatchEvent.call(ancestor, event);
      if (stopped || (event.cancelable && event.defaultPrevented)) break;
    }
    return !event.defaultPrevented;
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  scrollTo() {}
  get firstElementChild() { return this.children[0] || null; }
  descendants() {
    const out = [];
    const walk = (node) => node.children.forEach((child) => { out.push(child); walk(child); });
    walk(this);
    return out;
  }
  querySelectorAll(selector) { return document._select(selector, [this]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function parseCompound(text) {
  const spec = { tag: "", classes: [], id: "", attrs: [] };
  const rest = String(text).replace(ATTR_SELECTOR, (_, name, op, value) => {
    spec.attrs.push({ name, op: op || "", value: value == null ? "" : String(value) });
    return "";
  });
  const idMatch = rest.match(/#([\w-]+)/);
  if (idMatch) spec.id = idMatch[1];
  const classMatches = rest.match(/\.([\w-]+)/g) || [];
  spec.classes = classMatches.map((item) => item.slice(1));
  const tagMatch = rest.replace(/#[\w-]+/g, "").replace(/\.[\w-]+/g, "").trim();
  spec.tag = tagMatch;
  return spec;
}

function matchesCompound(node, compound) {
  const spec = parseCompound(compound);
  if (spec.tag && spec.tag !== "*" && node.tagName !== spec.tag.toUpperCase()) return false;
  if (spec.id && node.id !== spec.id) return false;
  if (spec.classes.some((name) => !node._classes().includes(name))) return false;
  return spec.attrs.every(({ name, value }) => {
    if (name === "hidden") return node.hidden === true;
    const actual = node.attributes.has(name) ? node.attributes.get(name) : null;
    if (actual == null) return false;
    return value === "" ? true : actual === value;
  });
}

function matchesSelector(node, selector) {
  const parts = String(selector).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return true;
  if (!matchesCompound(node, parts[parts.length - 1])) return false;
  let ancestor = node.parent;
  for (let i = parts.length - 2; i >= 0; i -= 1) {
    let found = false;
    while (ancestor) {
      if (matchesCompound(ancestor, parts[i])) { found = true; ancestor = ancestor.parent; break; }
      ancestor = ancestor.parent;
    }
    if (!found) return false;
  }
  return true;
}

const registry = new Map();
function register(tag, id, className = "") {
  const node = new Node(tag, id);
  if (className) node.className = className;
  registry.set(id, node);
  return node;
}

globalThis.document = {
  activeElement: null,
  getElementById: (id) => registry.get(id)
    || [document.head, document.body].filter(Boolean).flatMap((root) => root.descendants()).find((node) => node.id === id)
    || null,
  createElement: (tag) => new Node(tag),
  _select(selector, roots) {
    const pool = [];
    roots.forEach((root) => pool.push(...root.descendants()));
    return pool.filter((node) => matchesSelector(node, selector));
  },
  querySelectorAll(selector) { return document._select(selector, [document.body]); },
  querySelector(selector) { return document.querySelectorAll(selector)[0] || null; },
  documentElement: { dataset: {} },
};
globalThis.document.body = new Node("body");
globalThis.document.head = new Node("head");

globalThis.window = Object.assign(new EventTarget(), {
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => fn(),
});
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
};
globalThis.KeyboardEvent = class extends Event {
  constructor(type, options = {}) { super(type, { bubbles: options.bubbles, cancelable: options.cancelable }); this.key = options.key; }
};
globalThis.CSS = { escape: (value) => String(value) };
globalThis.matchMedia = (query) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
globalThis.localStorage = { store: new Map(), getItem(k) { return this.store.has(k) ? this.store.get(k) : null; }, setItem(k, v) { this.store.set(k, String(v)); } };
globalThis.Headers = class { constructor(init) { this.map = new Map(Object.entries(init || {})); } has(k) { return this.map.has(k); } set(k, v) { this.map.set(k, v); } get(k) { return this.map.get(k); } };
globalThis.AbortController = class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } };

/* ---------------- 网络桩 ---------------- */

function envelope(data) { return { schema: "courselens.api.v3", data }; }

const DECK = {
  view: "course_flashcards",
  label: "闪卡复习",
  course_id: "crs-a",
  source_label: "来自本课总结与知识要点（非官方）",
  fsrs_version: "fsrs-4.5-default",
  counts: { total: 3, due: 1, new: 2, learning: 0, reviewed_today: 0 },
  cards: [
    {
      card_id: "card-1", sub_id: "sub-a", card_type: "cloze", state: "review",
      front: "____是电子在绝对零度时的最高占据能级。",
      back: "费米能级决定填充秩序。", hint: "填空：这个词是什么？",
      lecture_label: "第 1 讲", due: true,
      evidence: [
        { citation_id: "cite-1", kind: "transcript", label: "同步字幕 01:00–01:30", start_ms: 60000, end_ms: 90000 },
        { citation_id: "cite-2", kind: "slide", label: "第 3 页" },
      ],
    },
    {
      card_id: "card-2", sub_id: "sub-a", card_type: "topic_cue", state: "new",
      front: "关于「载流子浓度」，这一讲讲了什么要点？",
      back: "温度升高时按指数规律上升。", hint: "与「载流子浓度」有关的结论。",
      lecture_label: "第 1 讲", due: false,
      evidence: [{ citation_id: "cite-1", kind: "transcript", label: "同步字幕 01:00–01:30", start_ms: 60000, end_ms: 90000 }],
    },
    {
      card_id: "card-3", sub_id: "sub-b", card_type: "anchor_recall", state: "new",
      front: "讲次进行到 02:00 附近时，老师强调的一个要点是什么？",
      back: "带隙决定吸收阈值。", hint: "",
      lecture_label: "第 2 讲", due: false,
      evidence: [],
    },
  ],
};

/* P1-7 乱序守卫专用：crs-b 的队列形状不同，断言「渲染的是哪一课」用。 */
const DECK_B = {
  ...DECK,
  course_id: "crs-b",
  cards: [
    {
      card_id: "b-card-1", sub_id: "sub-b2", card_type: "anchor_recall", state: "new",
      front: "B 课专属卡面：这一讲讲了什么？",
      back: "B 课答案。", hint: "",
      lecture_label: "B 课第 1 讲", due: false,
      evidence: [],
    },
  ],
  counts: { total: 1, due: 0, new: 1, learning: 0, reviewed_today: 0 },
};

let deckMode = "full"; // full | empty | empty-anchors | http-error | contract-invalid
let ratingMode = "ok"; // ok | fail
let forcedResultState = ""; // P2-9 定向：非空时评分响应固定返回该 state
let holdFlashcards = null; // P1-7 定向：{ course } 命中时 GET 挂起，直到 resolve(response)
let holdActions = null; // CSS-FIX-1 定向：非空时评分 POST 挂起，直到 resolve(response)
const net = { gets: [], posts: [] };

globalThis.fetch = async (url, options = {}) => {
  const text = String(url);
  const json = (payload) => ({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  if (text.includes("/api/v3/course-review/flashcards")) {
    net.gets.push(text);
    if (holdFlashcards && text.includes(`course_id=${encodeURIComponent(holdFlashcards.course)}`)) {
      return holdFlashcards.promise;
    }
    if (deckMode === "http-error") {
      return {
        ok: false, status: 500,
        headers: { get: () => "application/json" },
        json: async () => ({ error_code: "flashcards_unavailable" }),
        text: async () => JSON.stringify({ error_code: "flashcards_unavailable" }),
      };
    }
    if (deckMode === "contract-invalid") return json(envelope({ view: "something_else", counts: {}, cards: [] }));
    if (deckMode === "empty") {
      return json(envelope({
        view: "course_flashcards", course_id: "crs-a",
        source_label: "来自本课总结与知识要点（非官方）",
        counts: { total: 0, due: 0, new: 0, learning: 0, reviewed_today: 0 },
        cards: [],
        empty_action: { action: "refresh_course_knowledge", label: "更新课程知识", hint: "先去生成一次总结。" },
      }));
    }
    if (deckMode === "empty-anchors") {
      return json(envelope({
        view: "course_flashcards", course_id: "crs-a",
        source_label: "来自本课总结与知识要点（非官方）",
        counts: { total: 0, due: 0, new: 0, learning: 0, reviewed_today: 0 },
        cards: [],
        empty_action: {
          action: "refresh_course_knowledge", label: "更新课程知识",
          hint: "这一课的要点都还没带可核对出处的引用，闪卡只做有出处的卡。",
        },
      }));
    }
    if (text.includes("course_id=crs-b")) return json(envelope(DECK_B));
    return json(envelope(DECK));
  }
  if (text.includes("/api/v3/course-review/actions")) {
    net.posts.push(JSON.parse(options.body || "{}"));
    if (holdActions) return holdActions;
    if (ratingMode === "fail") {
      return {
        ok: false, status: 500,
        headers: { get: () => "application/json" },
        json: async () => ({ error_code: "flashcard_rating_failed" }),
        text: async () => JSON.stringify({ error_code: "flashcard_rating_failed" }),
      };
    }
    return json(envelope({
      card_id: JSON.parse(options.body || "{}").card_id, rating: JSON.parse(options.body || "{}").rating,
      state: forcedResultState || "learning", due_at: 1800000600, interval_seconds: 600,
      interval_text: "10 分钟后", stability: 1.4, difficulty: 5.1, lapses: 0,
    }));
  }
  return json(envelope({}));
};

/* ---------------- 页面节点 ---------------- */

const panel = register("div", "cr-panel-flashcards", "cr-panel");
panel.setAttribute("role", "tabpanel");
document.body.append(panel);
document.body.append(register("div", "toast-region"));

/* ---------------- 被测模块 ---------------- */

const flashcards = await import(
  pathToFileURL(join(dirname(fileURLToPath(import.meta.url)), "..", "frontend", "modules", "course-flashcards.js")).href
);

const settle = async () => { for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const ctx = {
  jumps: [],
  refreshes: 0,
  onJump: (target) => { ctx.jumps.push(target); },
  onRefresh: () => { ctx.refreshes += 1; },
};
const render = () => flashcards.renderFlashcardsPanel(panel, ctx);
const text = () => panel.textContent;
const ratingButtons = () => [...panel.querySelectorAll(".p4f-actions .p4f-button")].filter((node) => node.dataset.rating);

/* 1. 装载与首渲染：CSS 注入、计数头、进度与卡面（题面不含答案） */
flashcards.resetFlashcards("");
const deckPromise = flashcards.ensureFlashcardsDeck("crs-a");
render();
await deckPromise;
render();
const cssLink = document.getElementById("p4f-flashcards-style");
assert.ok(cssLink, "1a: 应注入样式 link");
assert.equal(cssLink.getAttribute("href"), "/styles/flashcards.css", "1b: 样式 href 指向本包独占文件");
assert.ok(text().includes("闪卡复习"), "1c: 计数头标题");
assert.ok(text().includes("第 1 / 3 张"), "1d: 会话进度");
assert.ok(text().includes("____"), "1e: 填空卡面带掩码");
assert.ok(!text().includes("费米能级决定填充秩序"), "1f: 翻面前绝不出现答案");
const kinds = [...panel.querySelectorAll(".p4f-card-kind")].map((node) => node.textContent);
assert.deepEqual(kinds, ["填空"], "1g: 卡型徽章");

/* 2. 翻面（点击卡片）：答案+提示+证据；有锚可点、无锚不可点 */
panel.querySelector(".p4f-card").click();
render();
assert.ok(text().includes("费米能级决定填充秩序"), "2a: 翻面后答案可见");
const jumps = panel.querySelectorAll(".p4f-evidence-jump");
assert.equal(jumps.length, 1, "2b: 只有带毫秒锚的来源可跳（slide 无锚不可点）");
jumps[0].click();
assert.deepEqual(ctx.jumps, [{ subId: "sub-a", startMs: 60000, endMs: 90000 }], "2c: 跳转锚逐字段下发");

/* 3. 评分推进：良好 → 下一张；请求体闭集；下一间隔提示 */
ratingButtons().find((node) => node.dataset.rating === "3").click();
await settle();
render();
assert.equal(net.posts.length, 1, "3a: 一次评分请求");
assert.equal(net.posts[0].action, "review_flashcard");
assert.equal(net.posts[0].card_id, "card-1");
assert.equal(net.posts[0].rating, 3);
assert.ok(text().includes("第 2 / 3 张"), "3b: 推进到第二张");
assert.ok(text().includes("载流子浓度"), "3c: 第二张卡面");
assert.ok(!text().includes("费米能级决定填充秩序"), "3d: 前一张答案不再展示");
assert.ok(text().includes("新卡 2"), "3e: 评复习态卡不动新卡计数（P2-9）");
assert.ok(text().includes("到期 0"), "3f: 到期卡评走减账");

/* 4. 键盘：空格翻面 + 数字评分 */
panel.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
render();
assert.ok(text().includes("温度升高时按指数规律上升"), "4a: 空格翻面");
panel.dispatchEvent(new KeyboardEvent("keydown", { key: "4", bubbles: true, cancelable: true }));
await settle();
render();
assert.equal(net.posts.length, 2, "4b: 数字键评分入账");
assert.equal(net.posts[1].rating, 4);
assert.ok(text().includes("第 3 / 3 张"), "4c: 推进到第三张");
assert.ok(text().includes("新卡 1"), "4d: 评新卡后新卡计数走减（P2-9）");

/* 5. 评分失败：不推进、不假报成功 */
ratingMode = "fail";
panel.querySelector(".p4f-card").click();
render();
ratingButtons().find((node) => node.dataset.rating === "1").click();
await settle();
render();
assert.equal(net.posts.length, 3, "5a: 失败请求发出过");
assert.ok(text().includes("第 3 / 3 张"), "5b: 失败不推进光标");
ratingMode = "ok";

/* 6. 会话完成视图 */
ratingButtons().find((node) => node.dataset.rating === "2").click();
await settle();
render();
assert.ok(text().includes("这一轮 3 张都过完了"), "6a: 完成视图");
assert.ok(text().includes("最后一张下次再见"), "6b: 最后间隔提示");
assert.ok(text().includes("新卡 0"), "6e: 最后一张新卡评完新卡计数归零（P2-9）");
const again = panel.querySelectorAll(".p4f-done .p4f-button");
assert.equal(again.length, 1, "6c: 完成视图提供刷新队列按钮");
deckMode = "full";
again[0].click();
await settle();
render();
assert.equal(net.gets.length, 2, "6d: 强制重取队列");

/* 7. 无产物诚实空态 + 行动指引（注入 onRefresh） */
deckMode = "empty";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
assert.ok(text().includes("先去生成一次总结"), "7a: 空态人话提示");
const refreshButton = panel.querySelector(".p4f-empty .p4f-button");
refreshButton.click();
assert.equal(ctx.refreshes, 1, "7b: 空态按钮走注入的更新课程知识动作");
deckMode = "empty-anchors";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
assert.ok(text().includes("闪卡只做有出处的卡"), "7c: 缺引用锚分型空态透传（P2-16）");

/* 8. 取数失败与合同不符：诚实错误行 */
deckMode = "http-error";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
assert.ok(text().includes("闪卡暂时没能取回来"), "8a: HTTP 失败人话错误");
deckMode = "contract-invalid";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
assert.ok(text().includes("数据格式和这一版应用对不上"), "8b: 合同不符诚实错误");

/* 9. 换课重取（增量断言：前面用例已各自取过数） */
deckMode = "full";
flashcards.resetFlashcards("");
const getsBefore = net.gets.length;
await flashcards.ensureFlashcardsDeck("crs-a");
assert.equal(net.gets.length, getsBefore + 1, "9a: 重置后重取");
await flashcards.ensureFlashcardsDeck("crs-a");
assert.equal(net.gets.length, getsBefore + 1, "9b: 同课不重复取");
await flashcards.ensureFlashcardsDeck("crs-b");
assert.equal(net.gets.length, getsBefore + 2, "9c: 换课重取");
assert.ok(net.gets[net.gets.length - 1].includes("course_id=crs-b"), "9d: 取的是新课");

/* 10. 畸形数据防御：normalizeDeck/normalizeFlashcard 逐条降级 */
assert.throws(() => flashcards.normalizeDeck({ view: "nope" }), "10a: 合同视图不符抛错");
const deck = flashcards.normalizeDeck({
  view: "course_flashcards", course_id: "x",
  counts: { total: "bogus", due: 2 },
  cards: [
    null,
    { card_id: "ok-1", sub_id: "s", card_type: "weird-type", front: "F", back: "B", evidence: "not-a-list" },
    { card_id: "", front: "F", back: "B" },
    { card_id: "ok-2", front: "F", back: "B", evidence: [{ kind: "transcript", label: "L", start_ms: -5 }, "junk"] },
  ],
});
assert.equal(deck.counts.total, 0, "10b: 非法计数降级为 0");
assert.equal(deck.cards.length, 2, "10c: 畸形卡逐条丢弃");
assert.equal(deck.cards[0].cardType, "anchor_recall", "10d: 未知卡型归到回忆兜底");
assert.deepEqual(
  deck.cards[1].evidence,
  [{ kind: "transcript", label: "L", startMs: null, endMs: null }],
  "10e: 非法锚降级为无锚可读（家规：可读不可点），纯垃圾条目丢弃",
);
const deckEmpty = flashcards.normalizeDeck({ view: "course_flashcards", counts: {}, cards: [] });
assert.equal(deckEmpty.cards.length, 0, "10f: 缺 cards 键降级为空数组");
assert.equal(
  flashcards.normalizeFlashcard({ card_id: "s1", front: "F", back: "B", state: "relearning" }, 0).state,
  "relearning",
  "10g: 调度 state 闭集内透传（P2-9）",
);
assert.equal(
  flashcards.normalizeFlashcard({ card_id: "s2", front: "F", back: "B", state: "corrupted" }, 1).state,
  "",
  "10h: 未知 state 降级为空（计数不误迁）",
);

/* 11. P2-10 键盘：按钮上的空格/回车归按钮本义，面板不抢翻；卡片翻面不冒泡双翻 */
deckMode = "full";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
const flipButton = panel.querySelector(".p4f-actions .p4f-button");
flipButton.focus();
flipButton.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
render();
assert.ok(!text().includes("费米能级决定填充秩序"), "11a: 翻面按钮上的回车不被面板抢去翻卡");
flipButton.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
render();
assert.ok(!text().includes("费米能级决定填充秩序"), "11b: 翻面按钮上的空格同理");
panel.querySelector(".p4f-card").click();
render();
ratingButtons()[0].focus();
ratingButtons()[0].dispatchEvent(new KeyboardEvent("keydown", { key: "4", bubbles: true, cancelable: true }));
await settle();
render();
assert.equal(net.posts.length, 5, "11c: 焦点在评分按钮上数字键仍可评分");
assert.equal(net.posts[4].rating, 4);
let panelKeydowns = 0;
const probe = (event) => { if (event.key === " ") panelKeydowns += 1; };
panel.addEventListener("keydown", probe);
panel.querySelector(".p4f-card").dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: false }));
panel.removeEventListener("keydown", probe);
assert.equal(panelKeydowns, 0, "11d: 卡片翻面 stopPropagation，不冒泡给面板");
render();
assert.ok(text().includes("温度升高时按指数规律上升"), "11e: 空格在卡上恰翻面一次");

/* 12. P2-9 定向：毕业结果（new→review）同样走减新卡、推进正常 */
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
render();
forcedResultState = "review";
panel.querySelector(".p4f-card").click();
render();
ratingButtons().find((node) => node.dataset.rating === "3").click();
await settle();
render();
assert.ok(text().includes("新卡 2"), "12a: 复习态卡评分不动新卡计数");
panel.querySelector(".p4f-card").click();
render();
ratingButtons().find((node) => node.dataset.rating === "3").click();
await settle();
render();
assert.ok(text().includes("新卡 1"), "12b: 新卡评成 review（毕业）同样走减");
assert.ok(text().includes("第 3 / 3 张"), "12c: 计数同步不干扰推进");
forcedResultState = "";

/* 13. P1-7 换课乱序守卫：A 课响应迟到，不得污染 B 课状态 */
deckMode = "full";
flashcards.resetFlashcards("");
let releaseA;
const staleA = new Promise((resolve) => { releaseA = resolve; });
holdFlashcards = { course: "crs-a", promise: staleA };
const slowA = flashcards.ensureFlashcardsDeck("crs-a"); // 挂起
const gotB = flashcards.ensureFlashcardsDeck("crs-b"); // 用户此刻换课
await gotB;
render();
assert.ok(text().includes("B 课专属卡面"), "13a: 换课后 B 课正常渲染");
assert.equal(flashcards.flashcardsBusy(), false, "13b: B 课收尾 loading 复位");
releaseA({
  ok: false, status: 500,
  headers: { get: () => "application/json" },
  json: async () => ({ error_code: "stale_course_response" }),
  text: async () => JSON.stringify({ error_code: "stale_course_response" }),
});
await slowA;
await settle();
render();
assert.ok(!text().includes("闪卡暂时没能取回来"), "13c: 迟到的 A 课错误不落到 B 课状态");
assert.ok(text().includes("B 课专属卡面"), "13d: B 课数据原样保留");
assert.equal(flashcards.flashcardsBusy(), false, "13e: 迟到响应不复活 loading");
holdFlashcards = null;

/* 14. CSS-FIX-1 定向：焦点恢复不断键盘流、在途 aria-busy、aria-live 播报位 */
deckMode = "full";
flashcards.resetFlashcards("");
await flashcards.ensureFlashcardsDeck("crs-a");
document.activeElement = null; // 冷渲染：焦点在面板外（页签进入态）
render();
assert.equal(document.activeElement, null, "14a: 冷渲染不抢焦点");
const coldCard = panel.querySelector(".p4f-card");
coldCard.focus();
coldCard.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
const flippedCard = panel.querySelector(".p4f-card");
assert.notEqual(flippedCard, coldCard, "14b: 翻面重建了卡节点（焦点原位已销毁）");
assert.equal(document.activeElement, flippedCard, "14c: 翻面后焦点回到新卡，键盘流不断");
assert.equal(flippedCard.getAttribute("aria-label"), "闪卡答案面", "14d: 焦点卡即答案面（读屏位置上下文）");
assert.equal(panel.querySelector(".p4f-progress").getAttribute("aria-live"), "polite", "14e: 进度行 aria-live=polite");
const postsBefore14 = net.posts.length;
let releasePost;
holdActions = new Promise((resolve) => { releasePost = resolve; });
panel.dispatchEvent(new KeyboardEvent("keydown", { key: "3", bubbles: true, cancelable: true }));
const busyButtons = ratingButtons();
assert.equal(busyButtons.length, 4, "14f: 翻面态四枚评分钮");
assert.ok(busyButtons.every((node) => node.disabled), "14g: 评分在途四钮禁用");
assert.ok(busyButtons.every((node) => node.getAttribute("aria-busy") === "true"), "14h: 在途钮带 aria-busy 提交中线索");
releasePost({
  ok: true, status: 200,
  headers: { get: () => "application/json" },
  json: async () => envelope({ card_id: "card-1", rating: 3, state: "learning", due_at: 1800000600, interval_seconds: 600, interval_text: "10 分钟后" }),
  text: async () => JSON.stringify(envelope({ card_id: "card-1", rating: 3, state: "learning", due_at: 1800000600, interval_seconds: 600, interval_text: "10 分钟后" })),
});
await settle();
render();
assert.equal(net.posts.length, postsBefore14 + 1, "14i: 评分恰好入账一次");
assert.ok(text().includes("第 2 / 3 张"), "14j: 评分后推进到第二张");
assert.equal(document.activeElement, panel.querySelector(".p4f-card"), "14k: 评分后焦点回到下一张卡");
panel.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
panel.dispatchEvent(new KeyboardEvent("keydown", { key: "4", bubbles: true, cancelable: true }));
await settle();
render();
assert.ok(text().includes("第 3 / 3 张"), "14l: 推进到第三张");
panel.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
panel.dispatchEvent(new KeyboardEvent("keydown", { key: "2", bubbles: true, cancelable: true }));
await settle();
render();
assert.ok(text().includes("这一轮 3 张都过完了"), "14m: 键盘流直达完成视图");
assert.equal(document.activeElement, panel.querySelector(".p4f-done .p4f-button"), "14n: 完成视图焦点落续作按钮");
assert.equal(panel.querySelector(".p4f-done p").getAttribute("aria-live"), "polite", "14o: 完成文案 aria-live=polite");
holdActions = null;

/* 15. FLASH-FIX-2 定向钉：is-flip 悬停不交给全局 hover 翻字色（深底白字对比门）。
 * mini-DOM 无级联引擎，对比度按静态规则钉：悬停块必须重申 --accent-contrast
 * （(0,4,0) 压全局 button:hover (0,3,1)），否则深底 2.10:1 回归。 */
const flashcardsCss = await (await import("node:fs/promises")).readFile(
  new URL("../frontend/styles/flashcards.css", import.meta.url), "utf8",
);
const flipHoverRule = flashcardsCss.match(/\.p4f-button\.is-flip:not\(\[disabled\]\):hover\s*\{[^}]*\}/)?.[0] || "";
assert.ok(flipHoverRule, "15a: is-flip 悬停规则在场");
assert.ok(flipHoverRule.includes("color: var(--accent-contrast)"), "15b: 悬停重申白字，不被全局 hover 翻成 navy-ink");
assert.ok(flipHoverRule.includes("background: var(--navy-hover)"), "15c: 悬停底保持 navy-hover");

console.log("frontend course flashcards behavior passed");
