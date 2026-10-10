import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/* RR-P2MULTI-1 多视图复习包专项矩阵。
 *
 * 覆盖：零 LLM 三视图（时间轴摘要/章节/关键问答）的渲染、视图切换（点击+
 * 键盘）、时间锚 seek、锚点缺失降级（可读不可点）、全源缺整节隐藏、
 * 单视图隐藏切换条、legacy lecture_summary 兜底、讲次切换重载、cleanup
 * 退订、CSS 注入、畸形单元防御；P2-CONTRACT-1 LLM 四档（提纲/FAQ/考前
 * 简报 + 时间轴事件线升级与章节线回落）、引用 chip（有锚可点 seek/无锚
 * 纯文本）、单档畸形整档省略、LLM 档缺席三视图无感降级。
 *
 * 夹具说明：沿用 N7F course-review 矩阵的自建真树 mini-DOM（支持属性选择器
 * 与事件冒泡），apiV3 走计划内路由网络桩。 */
/* eslint-disable no-unused-vars */

/* ---------------- mini-DOM ---------------- */

/* 属性名允许数字（data-p2m-* 家族带 2）——老壳 [a-zA-Z-]+ 在此处截断匹配。 */
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
    this.currentTime = 0;
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
  insertBefore(node, ref) {
    if (!node) return node;
    if (node.parent) node.parent.children = node.parent.children.filter((n) => n !== node);
    if (!ref || ref.parent !== this) { this.append(node); return node; }
    const index = this.children.indexOf(ref);
    this.children.splice(index < 0 ? this.children.length : index, 0, node);
    node.parent = this;
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parent = null; });
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((n) => n !== this); this.parent = null; }
  contains(node) { let current = node; while (current) { if (current === this) return true; current = current.parent; } return false; }
  closest(selector) { let current = this; while (current) { if (matchesSelector(current, selector)) return current; current = current.parent; } return null; }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  dispatchEvent(event) {
    const path = [];
    let node = this;
    while (node) { path.push(node); node = node.parent; }
    if (!event.bubbles) path.length = 1;
    for (const ancestor of path) {
      Object.defineProperty(event, "target", { value: this, configurable: true, writable: true });
      EventTarget.prototype.dispatchEvent.call(ancestor, event);
      if (event.cancelable && event.defaultPrevented) break;
    }
    return !event.defaultPrevented;
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  scrollTo() {}
  get firstElementChild() { return this.children[0] || null; }
  get parentNode() { return this.parent; }
  get nextSibling() {
    const siblings = this.parent?.children || [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get previousSibling() {
    const siblings = this.parent?.children || [];
    return siblings[siblings.indexOf(this) - 1] || null;
  }
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

/* ---------------- 夹具数据（合成讲次，零真实内容） ---------------- */

const CHAPTERS = [
  { chapter_id: "C0001", title: "PN 结的形成", summary: "内建电场与扩散运动达到平衡。", start_ms: 0, end_ms: 1200000, source_refs: [] },
  { chapter_id: "C0002", title: "交流小信号模型", summary: "扩散电容与渡越时间决定导纳。", start_ms: 1200000, end_ms: 2400000, source_refs: [] },
  { chapter_id: "C0003", title: "习题讨论", summary: "两道例题的求解思路。", start_ms: 2400000, end_ms: 3000000, source_refs: [] },
];
const SUMMARY = {
  schema_version: 1,
  overview: "本讲从平衡讲到小信号。",
  key_takeaways: ["扩散电容由渡越时间决定", "内建电场阻止进一步扩散"],
  takeaway_anchors: [1200000, null],
  chapters: CHAPTERS,
  generation: { input_hash: "h", prompt_version: "actions-summary-v1", model: "m" },
};
const IR = {
  contract: "evidence.v1",
  sections: [],
  knowledge_units: [
    { unit_id: "unit:a1", kind: "segment", title: "扩散电容", time: { start_ms: 1200500, end_ms: 1500000 }, spans: [{ kind: "segment", id: "seg:a1" }], content: { text: "扩散电容正比于直流电流与渡越时间。" } },
    { unit_id: "unit:a2", kind: "segment", title: "无锚单元", time: {}, spans: [], content: { text: "只有文字没有锚。" } },
    { unit_id: "unit:a3", kind: "segment", title: "纯标题单元", time: {}, spans: [], content: {} },
    "not-a-dict",
    42,
  ],
  key_moments: [],
};
const LEGACY_SUMMARY = { chapters: CHAPTERS.slice(0, 1), overview: "legacy" };

/* P2-CONTRACT-1 ①：LLM 四档夹具（存储合同 content_json 冻结形状）。
   timeline 事件故意乱序——钉归一化的升序重排；末档含未知考核类别——钉
   显示映射回退原文；study_guide 第二条无提示无锚无引用——钉静态降级。 */
const REVIEW_VIEWS = {
  schema_version: 1,
  views: {
    study_guide: {
      items: [
        { question: "内建电场如何形成？", hint: "从扩散与漂移的平衡想起", anchor_ms: 1200000, citation_ids: ["seg:a1", "seg:a2"] },
        { question: "无提示无锚的问句", hint: "", anchor_ms: null, citation_ids: [] },
      ],
    },
    faq: {
      items: [
        { question: "扩散电容和什么有关？", answer: "正比于直流电流与渡越时间。", anchor_ms: 1200500, citation_ids: [] },
      ],
    },
    timeline: {
      events: [
        { start_ms: 1200000, title: "小信号模型", detail: "导纳与渡越时间" },
        { start_ms: 0, title: "PN 结形成", detail: "" },
      ],
    },
    briefing: {
      speed_read: "本讲从平衡讲到小信号。",
      must_know: ["扩散电容由渡越时间决定"],
      exam_alerts: [
        { category: "quiz", title: "第三讲后小测", due_hint: "下周" },
        { category: "mystery_kind", title: "未知类别回退原文", due_hint: "" },
      ],
    },
  },
  generation: { input_hash: "h", prompt_version: "review-views-v1", model: "m" },
};
/* 单档畸形整档省略：broken study_guide 不出 tab，briefing 照常。 */
const REVIEW_VIEWS_PARTIAL = {
  schema_version: 1,
  views: { study_guide: { items: "broken" }, briefing: REVIEW_VIEWS.views.briefing },
  generation: REVIEW_VIEWS.generation,
};

let artifactMode = "full"; // full | no-views | views-partial | ir-only | none | legacy
const net = { calls: [] };
function envelope(data) { return { schema: "courselens.api.v3", data }; }

globalThis.fetch = async (url) => {
  const text = String(url);
  net.calls.push(text);
  const json = (payload) => ({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  if (!text.includes("/api/v3/artifacts")) return json(envelope({}));
  const params = new URLSearchParams(text.split("?")[1] || "");
  const kind = params.get("kind");
  const sub = params.get("sub_id");
  if (artifactMode === "none") return json(envelope({ artifact: null }));
  if (artifactMode === "ir-only") return json(envelope({ artifact: kind === "lecture_ir" ? { kind, content: IR } : null }));
  if (artifactMode === "legacy") {
    if (kind === "lecture_summary" && sub === "sub-a") return json(envelope({ artifact: { kind, content: LEGACY_SUMMARY } }));
    return json(envelope({ artifact: null }));
  }
  if (sub === "sub-a") {
    const table = { lecture_chapters: { chapters: CHAPTERS, anchor_count: 3 }, timestamp_summary: SUMMARY, lecture_ir: IR };
    if (artifactMode === "full") table.review_views = REVIEW_VIEWS;
    else if (artifactMode === "views-partial") table.review_views = REVIEW_VIEWS_PARTIAL;
    const content = table[kind];
    return json(envelope({ artifact: content ? { kind, content } : null }));
  }
  if (sub === "sub-b") {
    const table = {
      lecture_chapters: { chapters: [CHAPTERS[0]], anchor_count: 1 },
      timestamp_summary: { key_takeaways: ["B 讲要点"], takeaway_anchors: [5000], chapters: [CHAPTERS[0]] },
      lecture_ir: { contract: "evidence.v1", sections: [], knowledge_units: [{ title: "B 单元", time: { start_ms: 6000 }, content: { text: "B 讲内容。" } }], key_moments: [] },
    };
    const content = table[kind];
    return json(envelope({ artifact: content ? { kind, content } : null }));
  }
  return json(envelope({ artifact: null }));
};

/* ---------------- 页面节点（与 index.html 同构的最小集） ---------------- */

const panelReview = register("div", "panel-review", "material-panel");
panelReview.setAttribute("role", "tabpanel");
panelReview.setAttribute("data-material-panel", "review");
const sectionHeader = document.createElement("header");
sectionHeader.className = "section-header";
const heading = document.createElement("h2");
heading.textContent = "测验与复习";
const generateQuiz = register("button", "generate-quiz");
generateQuiz.textContent = "生成测验";
sectionHeader.append(heading, generateQuiz);
panelReview.append(sectionHeader);
panelReview.append(register("div", "quiz-list", "item-list"));
panelReview.append(register("section", "exam-context", "exam-context"));
panelReview.append(register("div", "review-list", "item-list"));
document.body.append(panelReview);

const playerStage = register("video", "player-stage");
playerStage.currentTime = 0;
document.body.append(playerStage);
document.body.append(register("div", "toast-region"));

/* ---------------- 被测模块 ---------------- */

const multiview = await import("../frontend/modules/review-multiview.js");
const { store } = await import("../frontend/modules/store.js");

const settle = async () => { for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const pack = () => document.getElementById("p2m-review-pack");

/* 1. 安装：CSS 注入 + 挂载位置（section-header 之后）+ 无讲次时整节隐藏 */
const cleanup = await multiview.installReviewMultiView(store);
const cssLink = document.getElementById("p2m-review-pack-style");
assert.ok(cssLink, "1a: 安装时应注入样式 link");
assert.equal(cssLink.getAttribute("href"), "/styles/review-multiview.css", "1b: 样式 href 应指向本包独占文件");
const section = pack();
assert.ok(section, "1c: 应挂载复习包 section");
assert.equal(section.parent, panelReview, "1d: 应挂在复习面板内");
assert.equal(panelReview.children[0], sectionHeader, "1e: 不得挤掉面板头");
assert.equal(panelReview.children[1], section, "1f: 应紧随面板头");
assert.equal(section.hidden, true, "1g: 无讲次时整节隐藏");

/* 2. 全源齐备（含 LLM 四档）：六视图按序渲染、时间轴升级为 LLM 事件线 */
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a", sub_title: "第 1 讲" });
await settle();
assert.equal(section.hidden, false, "2a: 有产物时整节可见");
const tabs = [...section.querySelectorAll("[data-p2m-view]")];
assert.deepEqual(
  tabs.map((tab) => tab.dataset.p2mView),
  ["timeline", "chapters", "qa", "study_guide", "faq", "briefing"],
  "2b: 视图闭集按序渲染（零 LLM 三视图 + LLM 三档）",
);
const timelinePanel = document.getElementById("p2m-panel-timeline");
assert.equal(timelinePanel.hidden, false, "2c: 默认选中时间轴");
const timelineItems = timelinePanel.querySelectorAll(".p2m-timeline-item");
assert.equal(timelineItems.length, 2, "2d: LLM 事件线升级本 tab（2 事件，非 3 章节）");
assert.equal(
  timelineItems[0].querySelector(".p2m-timeline-title")?.textContent, "PN 结形成",
  "2e: 事件线按 start_ms 升序（夹具故意乱序）",
);
const timelineAnchors = timelinePanel.querySelectorAll("[data-p2m-anchor]");
assert.equal(timelineAnchors.length, 3, "2f: 事件锚 2 + 要点锚 1（无锚要点不渲染控件）");
assert.equal(timelinePanel.querySelectorAll(".p2m-takeaway").length, 2, "2g: 关键要点照常随挂");
assert.equal(net.calls.filter((call) => call.includes("sub_id=sub-a")).length, 4, "2h: 四源并行各一次");

/* 3. seek：点时间锚驱动播放器currentTime；无播放器时不抛错 */
playerStage.currentTime = 0;
timelineAnchors[1].click();
assert.equal(playerStage.currentTime, 1200 / 1, "3a: 点击 1200000ms 锚跳到 1200s");

/* 4. 章节视图与关键问答视图 */
const chaptersTab = tabs.find((tab) => tab.dataset.p2mView === "chapters");
chaptersTab.click();
const chaptersPanel = document.getElementById("p2m-panel-chapters");
assert.equal(chaptersPanel.hidden, false, "4a: 点击切换到章节视图");
assert.equal(timelinePanel.hidden, true, "4b: 时间轴面板让位");
assert.equal(chaptersPanel.querySelectorAll(".p2m-chapter").length, 3, "4c: 章节卡三张");
const qaTab = tabs.find((tab) => tab.dataset.p2mView === "qa");
qaTab.click();
const qaPanel = document.getElementById("p2m-panel-qa");
assert.equal(qaPanel.hidden, false, "4d: 切换到关键问答视图");
assert.equal(qaPanel.querySelectorAll(".p2m-qa-card").length, 2, "4e: 可展开自测卡两张（文本或锚至少其一）");
assert.equal(qaPanel.querySelectorAll(".p2m-qa-static").length, 1, "4f: 纯标题单元降级为静态条目");
assert.equal(qaPanel.querySelectorAll("[data-p2m-anchor]").length, 1, "4g: 仅带锚单元有跳转控件");

/* 4b. 提纲档：自测卡复用 qa 卡式；hint 在展开体不作答案直显；无提示无锚
   无引用的条目诚实呈现静态；引用 chip 有锚可点、无锚纯文本。 */
const guideTab = tabs.find((tab) => tab.dataset.p2mView === "study_guide");
guideTab.click();
const guidePanel = document.getElementById("p2m-panel-study_guide");
assert.equal(guidePanel.hidden, false, "4b-1: 切换到提纲视图");
assert.equal(guidePanel.querySelectorAll(".p2m-qa-card").length, 1, "4b-2: 有提示条目=自测卡一张");
assert.equal(guidePanel.querySelectorAll(".p2m-qa-static").length, 1, "4b-3: 无提示无锚条目=静态条目");
const guideCard = guidePanel.querySelector(".p2m-qa-card");
guideCard.open = true;
assert.equal(guideCard.querySelector(".p2m-qa-a-text")?.textContent, "从扩散与漂移的平衡想起", "4b-4: 展开体是思路提示");
const citeChips = guidePanel.querySelectorAll(".p2m-cite");
assert.equal(citeChips.length, 2, "4b-5: 两条引用各一枚可点 chip");
assert.equal(citeChips[0].getAttribute("data-p2m-anchor"), "1200000", "4b-6: chip 承载条目锚");
playerStage.currentTime = 0;
citeChips[1].click();
assert.equal(playerStage.currentTime, 1200 / 1, "4b-7: 点引用 chip 跳到出处时刻");
assert.equal(guidePanel.querySelectorAll(".p2m-cite-text").length, 0, "4b-8: 有锚条目不产纯文本引用");

/* 4c. FAQ 档：问答卡复用 chapter 头体结构，问为头答为体；无引用不出 chip。 */
tabs.find((tab) => tab.dataset.p2mView === "faq").click();
const faqPanel = document.getElementById("p2m-panel-faq");
assert.equal(faqPanel.hidden, false, "4c-1: 切换到 FAQ 视图");
assert.equal(faqPanel.querySelectorAll(".p2m-chapter").length, 1, "4c-2: 问答卡一张");
assert.equal(faqPanel.querySelector(".p2m-chapter-title")?.textContent, "扩散电容和什么有关？", "4c-3: 问在卡头");
assert.equal(faqPanel.querySelector(".p2m-chapter-summary")?.textContent, "正比于直流电流与渡越时间。", "4c-4: 答在卡体");
assert.equal(faqPanel.querySelectorAll("[data-p2m-anchor]").length, 0, "4c-5: 无引用不出控件");

/* 4d. 考前简报档：速览段 + must_know 列表 + exam_alerts 徽标行（未知类别回退原文）。 */
tabs.find((tab) => tab.dataset.p2mView === "briefing").click();
const briefPanel = document.getElementById("p2m-panel-briefing");
assert.equal(briefPanel.hidden, false, "4d-1: 切换到考前简报视图");
assert.equal(briefPanel.querySelector(".p2m-brief-speed")?.textContent, "本讲从平衡讲到小信号。", "4d-2: 速览段");
assert.equal(briefPanel.querySelectorAll(".p2m-takeaway-text").length, 3, "4d-3: must_know 1 条 + 考核提醒标题 2 条");
const badges = [...briefPanel.querySelectorAll(".p2m-alert-badge")];
assert.deepEqual(badges.map((badge) => badge.textContent), ["小测", "mystery_kind"], "4d-4: 类别映射+未知回退原文");

/* 5. 键盘：ArrowRight 沿六视图闭集前进（qa → study_guide） */
const activeTab = section.querySelector("[data-p2m-view].active");
assert.equal(activeTab.dataset.p2mView, "briefing", "5a-预置: 当前在考前简报");
activeTab.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "ArrowRight" }));
assert.equal(section.querySelector("[data-p2m-view].active").dataset.p2mView, "timeline", "5b: 末档右键回绕到时间轴");
section.querySelector('[data-p2m-view="qa"]').dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "ArrowRight" }));
assert.equal(section.querySelector("[data-p2m-view].active").dataset.p2mView, "study_guide", "5c: qa 右邻是提纲");

/* 6. 讲次切换：重载且内容更新；B 讲无 LLM 档=零 LLM 三视图照常 */
net.calls.length = 0;
store.set("activeLecture", { course_id: "crs-b", sub_id: "sub-b", sub_title: "第 2 讲" });
await settle();
assert.ok(net.calls.some((call) => call.includes("kind=review_views")), "6a: LLM 档随四源并行拉取");
assert.equal(document.getElementById("p2m-panel-timeline").querySelectorAll(".p2m-timeline-item").length, 1, "6b: 内容已更新为 B 讲（章节线回落）");
assert.equal(document.getElementById("p2m-panel-qa").querySelectorAll(".p2m-qa-card").length, 1, "6c: B 讲问答卡");
assert.equal(section.querySelectorAll("[data-p2m-view]").length, 3, "6d: 无 review_views 产物=三视图，LLM 档 tab 不出现");

/* 7. 降级矩阵：LLM 档缺席三视图照常 → 单档畸形整档省略 → 仅 IR 单视图
   隐切换条 → 全缺整节隐藏 */
artifactMode = "no-views";
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a" });
await settle();
assert.equal(section.querySelectorAll("[data-p2m-view]").length, 3, "7a: 产物 null=零 LLM 三视图");
artifactMode = "views-partial";
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a" });
await settle();
const partialTabs = [...section.querySelectorAll("[data-p2m-view]")].map((tab) => tab.dataset.p2mView);
assert.deepEqual(partialTabs, ["timeline", "chapters", "qa", "briefing"], "7b: 畸形档省略、合法档照常");
assert.equal(document.getElementById("p2m-panel-timeline").querySelectorAll(".p2m-timeline-item").length, 3, "7c: 无事件线回落章节线");
artifactMode = "ir-only";
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a" });
await settle();
assert.equal(section.hidden, false, "7d: 仅 IR 时仍呈现");
const viewTabs = section.querySelectorAll("[data-p2m-view]");
assert.equal(viewTabs.length, 1, "7e: 只渲染关键问答一个视图");
assert.equal(document.getElementById("p2m-panel-qa").hidden, false, "7f: 问答面板直接呈现");
artifactMode = "none";
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a" });
await settle();
assert.equal(section.hidden, true, "7g: 全源缺整节隐藏");

/* 8. legacy lecture_summary 兜底 */
artifactMode = "legacy";
store.set("activeLecture", { course_id: "crs-a", sub_id: "sub-a" });
await settle();
assert.ok(net.calls.some((call) => call.includes("kind=lecture_summary")), "8a: 兜底拉取 legacy 总结");
assert.equal(section.hidden, false, "8b: legacy 讲次仍呈现");
assert.equal(document.getElementById("p2m-panel-timeline").querySelectorAll(".p2m-timeline-item").length, 1, "8c: legacy 章节渲染");

/* 9. 清理：退订后讲次变化不再拉取 */
artifactMode = "full";
cleanup();
net.calls.length = 0;
store.set("activeLecture", { course_id: "crs-b", sub_id: "sub-b" });
await settle();
assert.equal(net.calls.length, 0, "9a: cleanup 后零网络调用");

console.log("frontend review multiview behavior passed");
