import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/* N7F 课程总体复习专项矩阵。
 *
 * 覆盖：唯一课程级入口、三视图 tab/aria/键盘、七展示态、更新防重、
 * 证据按 kind 跳转、返回态恢复、答案来源诚实默认、窄屏与长列表结构，
 * 以及「按 N7K 冻结 fixture 逐字段消费」这一条最关键的兼容断言。
 *
 * 夹具说明：本文件自建一棵真树 mini-DOM（支持 [attr='v'] 与后代选择器），
 * 因为 _ui_polish/workspace 那类精简桩的 querySelectorAll 恒返回空数组，
 * 无法覆盖本模块真实用到的属性选择器（夜 6 假 DOM 与真 DOM 不符的教训）。
 * 真浏览器走查另在包专属合成服务器上完成（结果文件有记录）。
 */

/* ---------------- mini-DOM ---------------- */

const ATTR_SELECTOR = /\[([a-zA-Z-]+)(?:([~^$*|]?=)['"]?([^'"\]]*)['"]?)?\]/g;

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
    this.attributes.delete(String(name));
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
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parent = null; });
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((n) => n !== this); this.parent = null; }
  contains(node) { let current = node; while (current) { if (current === this) return true; current = current.parent; } return false; }
  closest(selector) { let current = this; while (current) { if (matchesSelector(current, selector)) return current; current = current.parent; } return null; }
  /* 真 DOM 的 click 会冒泡；模块用事件委托（tabsRoot 上监听），
     所以桩必须真的向上传播，否则委托链永远收不到点击。 */
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  dispatchEvent(event) {
    const path = [];
    let node = this;
    while (node) { path.push(node); node = node.parent; }
    if (!event.bubbles) path.length = 1;
    for (const ancestor of path) {
      /* 真 DOM 传播时 target 恒为最初目标（不随 currentTarget 变）；
         委托处理器靠 event.target.closest 找触发源，必须还原。 */
      Object.defineProperty(event, "target", { value: this, configurable: true, writable: true });
      EventTarget.prototype.dispatchEvent.call(ancestor, event);
      if (event.cancelable && event.defaultPrevented) break;
    }
    return !event.defaultPrevented;
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  scrollTo(options) { if (options && typeof options.top === "number") this.scrollTop = options.top; }
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
    /* 无值的 [attr] 是「存在性」选择器：统一落成空串，与 [attr=''] 区分开由 op 承担 */
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
  const text = String(selector).trim();
  const notMatches = [...text.matchAll(/:not\(([^)]*)\)/g)];
  let base = text;
  for (const [, inner] of notMatches) {
    base = base.replace(inner ? `:not(${inner})` : "", "");
    if (matchesSelector(node, inner)) return false;
  }
  const parts = base.trim().split(/\s+/).filter(Boolean);
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
  getElementById: (id) => registry.get(id) || null,
  createElement: (tag) => new Node(tag),
  createElementNS: (_ns, tag) => new Node(tag),
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
  /* bubbles/cancelable 由 Event 构造器持有（只读），不要再赋值 */
  constructor(type, options = {}) { super(type, { bubbles: options.bubbles, cancelable: options.cancelable }); this.key = options.key; }
};
globalThis.CSS = { escape: (value) => String(value) };
globalThis.matchMedia = (query) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
globalThis.localStorage = { store: new Map(), getItem(k) { return this.store.has(k) ? this.store.get(k) : null; }, setItem(k, v) { this.store.set(k, String(v)); } };
globalThis.Headers = class { constructor(init) { this.map = new Map(Object.entries(init || {})); } has(k) { return this.map.has(k); } set(k, v) { this.map.set(k, v); } get(k) { return this.map.get(k); } };
globalThis.AbortController = class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } };

/* ---------------- 冻结 fixture（N7K 合同产物，只读） ---------------- */

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..");
const FIXTURE = JSON.parse(readFileSync(join(repoRoot, "tests", "fixtures", "course_review_v1.json"), "utf8"));
const VIEWS = FIXTURE.views;
const COURSE_ID = "crs-synthetic-2026a";

/* 网络桩：只认计划内路由；记录调用次数用于防重断言 */
const net = { calls: [], mode: "as-fixture", refreshCalls: 0, failOverview: false };
function envelope(data) { return { schema: "courselens.api.v3", data }; }

globalThis.fetch = async (url, options = {}) => {
  const text = String(url);
  const method = String(options.method || "GET").toUpperCase();
  net.calls.push(`${method} ${text}`);
  const json = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  if (text.includes("/api/v3/course-review/actions")) {
    net.refreshCalls += 1;
    return json(envelope({ status: "queued", queued: 2, skipped: 1, blocked: 1, reasons: ["1 个讲次还没有字幕，先放一放。"] }));
  }
  if (text.includes("/api/v3/course-review/lecture")) {
    if (!text.includes("sub-syn-0001")) return json({ error_code: "lecture_not_found", error: "no detail" }, 404);
    return json(envelope(net.lectureDetail || VIEWS.lecture_detail));
  }
  if (text.includes("/api/v3/course-review/assessment")) return json(envelope(VIEWS.assessment_workspace));
  if (text.includes("/api/v3/course-review")) {
    if (net.failOverview) return json({ error_code: "network_unavailable", error: "offline" }, 503);
    const view = { ...VIEWS.course_overview };
    if (net.mode === "partial") { view.status = "partial"; view.stale_reasons = ["summary_missing"]; }
    else if (net.mode === "stale") { view.status = "stale"; view.stale_reasons = ["input_changed"]; }
    else if (net.mode === "error") { view.status = "error"; view.stale_reasons = ["never_built"]; }
    else if (net.mode === "snapshot_invalid") { view.status = "stale"; view.stale_reasons = ["snapshot_invalid"]; }
    else if (net.mode === "unknown_status") { view.status = "reticulating"; }
    else if (net.mode === "empty") { view.status = "ready"; view.topics = []; view.lectures = []; view.assessment = { total: 0, lectures_with_items: 0 }; }
    /* 真实信封（application.course_review）：概览在 data.view，且同响应已带
       assessment_workspace —— 消费层必须认这个形状，否则会静默渲染成空。 */
    const data = {
      view,
      assessment_workspace: net.mode === "empty" ? { ...VIEWS.assessment_workspace, items: [], counts: { total: 0, lectures: 0, course_level: 0 } } : VIEWS.assessment_workspace,
      snapshot: null,
      state: view.status,
      diagnostics: {},
      observed_at: 0,
    };
    /* THINK-LADDER-2：term_candidates 加性视图（缺席=键缺失=复核区隐藏） */
    if (net.termCandidates) data.term_candidates = net.termCandidates;
    return json(envelope(data));
  }
  return json(envelope({}));
};

/* ---------------- 页面节点（与 index.html 计划标记同构） ---------------- */

const surface = register("section", "course-review-surface", "course-review");
surface.setAttribute("hidden", "hidden");
surface.setAttribute("tabindex", "-1");
const header = document.createElement("header");
const exitButton = register("button", "course-review-exit");
const reloadButton = register("button", "course-review-reload");
const refreshButton = register("button", "course-review-refresh");
const courseLabel = register("span", "course-review-course");
const coverageNode = register("p", "course-review-coverage");
coverageNode.setAttribute("hidden", "hidden");
const statusNode = register("p", "course-review-status");
const refreshResultNode = register("p", "course-review-refresh-result");
refreshResultNode.setAttribute("hidden", "hidden");
/* THINK-LADDER-2：课程记忆候选复核区（候选确认/忽略 + 自动晋升件撤销） */
const termRegion = register("div", "term-candidate-region");
termRegion.setAttribute("hidden", "hidden");
const tabsRoot = document.createElement("div");
tabsRoot.className = "cr-tabs";
tabsRoot.setAttribute("role", "tablist");
const tabNodes = {};
const panelNodes = {};
["topics", "lectures", "assessment"].forEach((name, index) => {
  const tab = register("button", `cr-tab-${name}`);
  tab.className = index === 0 ? "cr-tab active" : "cr-tab";
  tab.setAttribute("role", "tab");
  tab.setAttribute("data-review-tab", name);
  tab.setAttribute("aria-selected", index === 0 ? "true" : "false");
  tab.tabIndex = index === 0 ? 0 : -1;
  tabNodes[name] = tab;
  const panel = register("div", `cr-panel-${name}`);
  panel.className = "cr-panel";
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("data-review-panel", name);
  if (index !== 0) panel.setAttribute("hidden", "hidden");
  panelNodes[name] = panel;
  tabsRoot.append(tab);
  surface.append(panel);
});
surface.append(header, coverageNode, statusNode, refreshResultNode, tabsRoot);
header.append(exitButton, courseLabel, reloadButton, refreshButton);
document.body.append(surface);

const courseLayout = document.createElement("div");
courseLayout.className = "course-layout";
document.body.append(courseLayout);
const entryButton = register("button", "course-review-open");
entryButton.setAttribute("hidden", "hidden");
document.body.append(entryButton);

const toastRegion = register("div", "toast-region");
document.body.append(toastRegion);

/* ---------------- 被测模块 ---------------- */

const review = await import("../frontend/modules/course-review.js");
const { store } = await import("../frontend/modules/store.js");

const settle = () => new Promise((resolve) => setImmediate(resolve));
const nowSeconds = Math.floor(Date.now() / 1000);
const STORE = {
  activeCourse: { course_id: COURSE_ID, title: "小波分析（合成）", lectures: [
    { sub_id: "sub-syn-0001", sub_title: "第 1 讲 小波变换" },
    { sub_id: "sub-syn-0002", sub_title: "第 2 讲 滤波器组" },
  ] },
  activeLecture:null,
};

/* ---------------- 纯函数层 ---------------- */

/* 合同常量与闭集 */
assert.equal(review.CONTRACT_ID, "courselens.course-knowledge.v1", "合同 ID 与冻结值一致");
assert.deepEqual([...review.DOCUMENT_STATUSES], ["ready", "partial", "stale", "error"], "后端 status 闭集 = 冻结原文");
assert.deepEqual([...review.REVIEW_TABS], ["topics", "lectures", "assessment", "flashcards"], "四视图闭集（RR-P4FSRS-1 增补闪卡页签）");
assert.deepEqual([...review.EVIDENCE_KINDS], ["transcript", "slide", "document_page", "assessment_item", "bookmark"], "证据 kind 闭集");
{
  const seven = review.REVIEW_DISPLAY_STATES;
  ["complete", "partial", "stale", "processing", "failed", "empty", "conflict"].forEach((state) => {
    assert.ok(seven.includes(state), `七展示态含 ${state}`);
  });
}

/* 键位模型 */
assert.equal(review.reviewTabTarget("topics", "ArrowRight"), "lectures");
assert.equal(review.reviewTabTarget("flashcards", "ArrowRight"), "topics", "ArrowRight 环绕");
assert.equal(review.reviewTabTarget("topics", "ArrowLeft"), "flashcards", "ArrowLeft 环绕");
assert.equal(review.reviewTabTarget("lectures", "Home"), "topics");
assert.equal(review.reviewTabTarget("topics", "End"), "flashcards");
assert.equal(review.reviewTabTarget("topics", "Tab"), null, "非 tablist 键不拦截");

/* 渐进展开（50 讲 / 500 题不一次塞满 DOM） */
{
  const many = Array.from({ length: 500 }, (_, i) => i);
  assert.equal(review.reviewSlice(many, 25, false).visible.length, 25);
  assert.equal(review.reviewSlice(many, 25, false).remaining, 475);
  assert.equal(review.reviewSlice(many, 25, true).visible.length, 500);
  assert.equal(review.reviewSlice([1, 2], 25, false).remaining, 0);
}

/* 答案来源：合同 v1 无答案字段 → 诚实默认，AI 必显式标识 */
assert.equal(review.answerSourceLabel({ answerSource: "", hasAnswer: false }), "暂无官方答案");
assert.equal(review.answerSourceLabel({ answerSource: "ai", hasAnswer: true }), "AI 解答（非官方）");
assert.equal(review.answerSourceLabel({ answerSource: "official", hasAnswer: true }), "官方答案");
assert.equal(review.answerSourceLabel({ answerSource: "wizard", hasAnswer: true }), "来源未标注（非官方）", "有答案但来源未知：不冒充官方，也不说成没答案");
assert.equal(review.answerSourceLabel({ answerSource: "wizard", hasAnswer: false }), "暂无官方答案");
/* N7A 实际枚举（src/runtime/assessment_ir.py:44）必须逐值认得，否则真实答案会被
   静默降级成「暂无官方答案」——跨组件最危险的一类失配。 */
assert.equal(review.answerSourceLabel({ answerSource: "official", hasAnswer: true }), "官方答案");
assert.equal(review.answerSourceLabel({ answerSource: "teacher_material", hasAnswer: true }), "教师资料");
assert.equal(review.answerSourceLabel({ answerSource: "user_material", hasAnswer: true }), "我的作答");
assert.equal(review.answerSourceLabel({ answerSource: "ai_generated", hasAnswer: true }), "AI 解答（非官方）");
assert.equal(review.answerSourceLabel({ answerSource: "none", hasAnswer: false }), "暂无官方答案");
/* has_answer 以后端为准；缺失才按答案文本推断 */
assert.equal(review.normalizeAssessmentItem({ item_id: "a", answer_source: "ai_generated", has_answer: true, answer: "解释" }).hasAnswer, true);
assert.equal(review.normalizeAssessmentItem({ item_id: "b", answer_source: "ai_generated", has_answer: false, answer: "解释" }).hasAnswer, false, "后端说没有就以没有为准");
assert.equal(review.normalizeAssessmentItem({ item_id: "c", answer_source: "official", answer: "答案" }).hasAnswer, true, "无 has_answer 时按答案文本推断");
assert.equal(review.normalizeAssessmentItem({ item_id: "d", answer_source: "none", answer: "" }).hasAnswer, false);

/* ---------------- 冻结 fixture 逐字段消费 ---------------- */

const overview = review.normalizeCourseOverview(VIEWS.course_overview);
assert.equal(overview.contract, review.CONTRACT_ID);
assert.equal(overview.status, "ready");
assert.equal(overview.topics.length, 3, "三个 topic 全部解析");
assert.deepEqual(overview.topics.map((t) => t.title), ["滤波器组", "小波变换", "多分辨率分析"], "topic 标题取合同值");
assert.deepEqual(overview.topics.find((t) => t.title === "小波变换").aliases, ["Wavelet Transform"], "别名保留");
assert.equal(overview.lectures.length, 2, "两讲全部解析");
assert.equal(overview.lectures[0].keyPointCount, 3, "key_point_count 透传");
assert.equal(overview.coverage.length, 10, "课程级 coverage 十键");
assert.equal(overview.sources.length, 7, "sources 全部解析");
assert.ok(!/%|掌握度/.test(review.coverageText(overview.coverage)), "覆盖度文案不含百分比或掌握度");

/* 来源索引：EvidenceRef.source_id 指向 Source.external_id（合同实测坑） */
{
  const index = review.sourceIndex(overview.sources);
  assert.equal(index.get("seg:111111111111")?.label, "第 1 讲同步字幕", "按 external_id 解析来源");
  assert.equal(index.get("cks:f671a091ca4d")?.label, "第 1 讲同步字幕", "按 source_id 亦可达（兜底）");
  assert.equal(index.get("slevt:333333333333")?.scope, "lecture");
}

/* 信封兼容：概览可以包在 {view: ...} 里（后端实测形状），也可以直接给视图；
   讲次/题目路由直接给视图。两种都必须解析出同样的字段。 */
{
  const wrapped = review.unwrapReviewView({ view: VIEWS.course_overview, assessment_workspace: VIEWS.assessment_workspace }, "course_overview");
  assert.equal(wrapped.view, "course_overview");
  const direct = review.unwrapReviewView(VIEWS.course_overview, "course_overview");
  assert.equal(direct.view, "course_overview");
  assert.equal(review.normalizeCourseOverview({ view: VIEWS.course_overview }).topics.length, 3, "包一层也解析出 topic");
  assert.equal(review.normalizeCourseOverview(VIEWS.course_overview).topics.length, 3, "不包也解析出 topic");
  assert.equal(review.normalizeLectureDetail(VIEWS.lecture_detail).keyPoints.length, 3);
  assert.equal(review.normalizeAssessmentWorkspace(VIEWS.assessment_workspace).items.length, 1);
  /* 未知形状不猜：落回 raw，由后续字段级判定决定（而不是抛错） */
  assert.equal(review.unwrapReviewView(null, "course_overview").view, undefined);
  assert.equal(review.normalizeCourseOverview("nope").topics.length, 0);
}

const detail = review.normalizeLectureDetail(VIEWS.lecture_detail);
assert.equal(detail.subId, "sub-syn-0001");
assert.equal(detail.keyPoints.length, 3);
assert.equal(detail.keyPoints[0].text, "小波变换把信号分解到时频平面", "key_points 保持合同阅读序（有意不排序）");
assert.equal(detail.evidenceRefs.length, 5);
assert.ok(detail.keyPoints.every((point) => point.citations.length > 0), "每条主张的引用都能解析");
assert.equal(detail.keyPoints[0].subId, "sub-syn-0001", "主张自带所属讲次（locator 不含讲次，归属由 detail 给出）");
/* RR-ANCHORFE-1：key_point 锚点毫秒透传（合法锚→数字；缺字段/畸形→null 不猜锚） */
{
  const anchored = JSON.parse(JSON.stringify(VIEWS.lecture_detail));
  anchored.key_points[0].anchor_ms = 125000;
  const anchoredDetail = review.normalizeLectureDetail(anchored);
  assert.equal(anchoredDetail.keyPoints[0].anchorMs, 125000, "锚点毫秒透传");
  assert.ok(anchoredDetail.keyPoints[1].anchorMs == null, "无锚要点不猜锚");
  const malformed = JSON.parse(JSON.stringify(VIEWS.lecture_detail));
  malformed.key_points[0].anchor_ms = "next-week";
  assert.equal(review.normalizeLectureDetail(malformed).keyPoints[0].anchorMs, null, "畸形锚降级为无锚");
}
{
  const transcript = detail.evidenceRefs.find((ref) => ref.kind === "transcript");
  assert.equal(transcript.startMs, 125000, "transcript locator 取毫秒锚");
  assert.equal(detail.evidenceRefs.find((ref) => ref.kind === "slide").page, 7, "slide locator 取页码锚");
  const index = review.sourceIndex(overview.sources);
  const plan = review.evidenceJumpPlan(transcript, detail, index);
  assert.equal(plan.navigable, true);
  assert.deepEqual(plan.target, { kind: "transcript", subId: "sub-syn-0001", startMs: 125000, endMs: 168000 }, "字幕跳转目标");
  const bookmarkPlan = review.evidenceJumpPlan(detail.evidenceRefs.find((ref) => ref.kind === "bookmark"), detail, index);
  assert.equal(bookmarkPlan.target.kind, "transcript", "书签按时间点回到字幕");
  const slidePlan = review.evidenceJumpPlan(detail.evidenceRefs.find((ref) => ref.kind === "slide"), detail, index);
  assert.equal(slidePlan.target.kind, "documents");
  assert.equal(slidePlan.target.page, 7);
}

const workspace = review.normalizeAssessmentWorkspace(VIEWS.assessment_workspace);
assert.equal(workspace.items.length, 1);
assert.equal(workspace.items[0].label, "第 2 题");
assert.equal(workspace.items[0].questionNo, 2, "question_no 透传");
assert.equal(workspace.items[0].hasAnswer, false, "合同不含答案字段 → 不假装有答案");
assert.equal(review.answerSourceLabel(workspace.items[0]), "暂无官方答案");
assert.ok(FIXTURE.rejections.some((r) => r.expected_code === "assessment_answer_unsupported"),
  "合同把「题目伪装官方答案」列为拒绝样本（练习视图的诚实默认因此成立）");

/* 跳转计划：不能跳的必须给理由，绝不造死按钮 */
{
  const index = review.sourceIndex(overview.sources);
  const noTime = review.evidenceJumpPlan({ kind: "transcript", label: "无时间点" }, detail, index);
  assert.equal(noTime.navigable, false);
  assert.match(noTime.reason, /没有时间点/);
  const unknown = review.evidenceJumpPlan({ kind: "hologram", label: "某不明来源" }, detail, index);
  assert.equal(unknown.navigable, false);
  assert.equal(unknown.label, "某不明来源", "不可跳也要保留出处文本");
  assert.match(unknown.reason, /不能跳转/);
  assert.equal(review.evidenceJumpPlan({ label: "无 kind" }, detail, index).navigable, false);
}

/* 七展示态 + 未知降级（全部由后端字段或客户端可见条件判定） */
{
  const base = overview;
  const stateOf = (patch, options) => review.reviewDisplayState({ ...base, ...patch }, options);
  assert.equal(stateOf({}), "complete");
  assert.equal(stateOf({ status: "partial" }), "partial");
  assert.equal(stateOf({ status: "stale" }), "stale");
  assert.equal(stateOf({ status: "error" }), "failed");
  assert.equal(stateOf({}, { refreshing: true }), "processing");
  assert.equal(stateOf({ topics: [], lectures: [], assessment: { total: 0 } }), "empty");
  assert.equal(stateOf({ staleReasons: ["snapshot_invalid"] }), "conflict", "快照不可信→冲突态");
  assert.equal(stateOf({}, { errorCode: "state_conflict" }), "conflict");
  assert.equal(stateOf({ status: "" }), "unknown", "闭集外 status 诚实降级");
}
{
  const presentations = ["complete", "partial", "stale", "processing", "failed", "empty", "conflict", "unknown"]
    .map((state) => review.reviewStatusPresentation({
      ...overview,
      status: state === "complete" ? "ready" : state === "partial" ? "partial" : state === "stale" ? "stale" : state === "failed" ? "error" : "ready",
      staleReasons: state === "conflict" ? ["snapshot_invalid"] : [],
    }, { refreshing: state === "processing" }));
  presentations.forEach((item, index) => {
    assert.ok(item.message.length > 0, `展示态 ${index} 有人话说明`);
    assert.ok(!/失败是你|你没|你不会|错误操作/.test(item.message), `展示态 ${index} 不责备学生`);
    assert.ok(!/%|掌握度/.test(item.message), `展示态 ${index} 不出现掌握度`);
  });
  assert.match(presentations[2].message, /有新资料待更新/, "stale 明说待更新");
  assert.match(presentations[1].message, /可以正常使用/, "partial 仍可操作");
  assert.match(presentations[4].message, /上一次的可用内容/, "failed 保旧内容");
}

/* 归一化：未知字段忽略、缺字段不崩、坏载荷不抛 */
assert.equal("mystery" in review.normalizeCourseOverview({ course_id: "c1", mystery: 1 }), false, "未知字段忽略");
assert.deepEqual(review.normalizeCourseOverview(null).topics, [], "null 载荷安全");
assert.deepEqual(review.normalizeLectureDetail("nope").keyPoints, [], "非对象载荷安全");
assert.equal(review.normalizeCourseOverview({ status: "weird" }).status, "", "闭集外 status 不进内部状态");
assert.deepEqual(review.normalizeAssessmentWorkspace({ items: [{ no_id: 1 }] }).items, [], "无 item_id 的条目丢弃");
assert.equal(review.normalizeCoverage(null).length, 0);
assert.equal(review.normalizeCoverage({ transcript: { nested: true } }).length, 0, "非计数器形状忽略");

/* 刷新结果：只读计数、缺失按 0、文案不责备 */
{
  const result = review.normalizeRefreshResult({ queued: 3, skipped: 5, blocked: 1, reasons: ["1 个讲次还没有字幕，先放一放。"] });
  assert.deepEqual(result.counts, { queued: 3, skipped: 5, blocked: 1 });
  const text = review.refreshResultText(result);
  assert.match(text, /已安排 3 个讲次/);
  assert.match(text, /5 个讲次已是最新/);
  assert.match(text, /1 个讲次暂时没法整理/);
  assert.match(text, /还没有字幕/);
  assert.equal(review.refreshResultText(null), "");
  assert.equal(review.normalizeRefreshResult(null).counts.queued, 0, "缺失计数按 0");
}

/* ---------------- 真树 DOM 层 ---------------- */

let jumpLog = [];
review.provideReviewNavigation({
  onOpenChange() { courseLayout.hidden = review.isCourseReviewOpen(); surface.hidden = !review.isCourseReviewOpen(); },
  toTranscript: (target) => { jumpLog.push(`transcript:${target.subId}@${target.startMs}`); return true; },
  toDocuments: (target) => { jumpLog.push(`documents:${target.sourceId}#${target.page}`); return true; },
  toAssessment: (target) => { jumpLog.push(`assessment:${target.sourceId}`); return true; },
});

const cleanup = review.installCourseReview(STORE);

/* 唯一入口：课程级恰好一个，且不在课次卡里 */
assert.equal(document.querySelectorAll("#course-review-open").length, 1, "全域只有一个总体复习入口");
{
  const indexHtml = readFileSync(join(repoRoot, "frontend", "index.html"), "utf8");
  const head = indexHtml.slice(indexHtml.indexOf('<div class="lecture-head">'), indexHtml.indexOf('id="study-lecture-list"'));
  assert.ok(head.includes('id="course-review-open"'), "入口落在课程头 .lecture-head 内");
  assert.equal((indexHtml.match(/id="course-review-open"/g) || []).length, 1, "index.html 中入口 DOM 唯一");
  assert.equal((indexHtml.match(/id="course-review-surface"/g) || []).length, 1, "surface DOM 唯一");
  const study = familySource("study");
  assert.match(study, /if \(reviewEntry\) reviewEntry\.hidden = !course\?\.course_id;/, "入口仅在选中课程后出现");
  assert.equal((study.match(/studyLandingNavigated = true;/g) || []).length, 5, "启动落地门解除点仍恰好五处（不新增）");
}

/* 三视图 aria 契约 */
assert.equal(document.querySelectorAll("[role='tab'][data-review-tab]").length, 3, "三个复习 tab");
assert.equal(document.querySelectorAll("[role='tabpanel'][data-review-panel]:not([hidden])").length, 1, "同一时刻只露一个 panel");
assert.equal(tabNodes.topics.getAttribute("aria-selected"), "true");
assert.equal(tabNodes.topics.tabIndex, 0);
assert.equal(tabNodes.lectures.tabIndex, -1, "非活动 tab 不在 Tab 序");
assert.equal(panelNodes.lectures.hidden, true);
assert.equal(document.querySelectorAll("[role='tab'][data-material-tab]").length, 0, "复习 tab 不污染既有 data-material-tab 域");

/* 打开：surface 出现、课程布局让位 */
assert.equal(review.isCourseReviewOpen(), false, "初始未打开");
await review.openCourseReview(STORE);
await settle();
assert.equal(review.isCourseReviewOpen(), true);
assert.equal(surface.hidden, false, "打开后 surface 可见");
assert.equal(courseLayout.hidden, true, "打开后课程布局让位");
assert.equal(courseLabel.textContent, "小波分析（合成）", "surface 标注当前课程");

/* 知识脉络：合同 topic + 主张 + 来源按钮可展开 */
{
  const topics = panelNodes.topics.querySelectorAll(".cr-topic");
  assert.equal(topics.length, 3, "三个 topic 渲染");
  const points = panelNodes.topics.querySelectorAll(".cr-point");
  assert.ok(points.length > 0, "知识点主张渲染");
  const sources = panelNodes.topics.querySelectorAll(".cr-source-button");
  assert.ok(sources.length > 0, "有引用的主张带来源按钮");
  assert.equal(sources[0].getAttribute("aria-expanded"), "false", "来源默认收起");
  const disclosure = sources[0].closest(".cr-source-wrap").querySelector(".cr-citations");
  assert.equal(disclosure.hidden, true);
  sources[0].click();
  assert.equal(sources[0].getAttribute("aria-expanded"), "true", "点击展开来源");
  assert.equal(disclosure.hidden, false);
  assert.ok(disclosure.querySelectorAll(".cr-citation-row").length > 0, "展开后列出引用");
  assert.ok(/字幕|课件页|资料页|题目|书签/.test(disclosure.textContent), "引用标注种类与来源");
  const other = sources[1]?.closest(".cr-source-wrap").querySelector(".cr-citations");
  if (other) assert.equal(other.hidden, true, "展开一个不影响另一个");
  assert.ok(!/不会|不懂|落后/.test(surface.textContent), "全程不出现消极标签");
  assert.ok(!/%|掌握度/.test(surface.textContent), "surface 内不出现百分比或掌握度");
}

/* 证据跳转真的走到导航出口 */
{
  jumpLog = [];
  const jump = panelNodes.topics.querySelector(".cr-citation-jump");
  assert.ok(jump, "可跳引用给出跳转按钮");
  jump.click();
  assert.equal(jumpLog.length, 1, "点击触发一次跳转");
  assert.match(jumpLog[0], /^(transcript|documents|assessment):/, "跳转按 kind 分流");
}

/* 切换视图：按课次按需取 lecture_detail 并渲染要点 + coverage */
tabNodes.lectures.click();
await settle();
await settle();
await settle();
{
  const cards = panelNodes.lectures.querySelectorAll(".cr-lecture");
  assert.equal(cards.length, 2, "两讲渲染");
  const points = panelNodes.lectures.querySelectorAll(".cr-point");
  assert.ok(points.length > 0, "按需取回的讲次要点渲染");
  assert.equal(panelNodes.lectures.querySelectorAll(".cr-point-text")[0].textContent, "小波变换把信号分解到时频平面", "要点保持合同序");
  assert.match(panelNodes.lectures.textContent, /字幕片段/, "讲次 coverage 以计数呈现");
  assert.match(panelNodes.lectures.textContent, /第 1 讲 小波变换/, "讲次标题取课程目录的人话名，不显示 sub_id");
  assert.ok(!/sub-syn-0001/.test(panelNodes.lectures.textContent), "按课次视图不暴露原始 sub_id");
  /* 有界取数：可解析的讲次缓存复用；不可解析的讲次失败即止，不随重渲染反复重试 */
  assert.equal(net.calls.filter((c) => c.includes("sub-syn-0001")).length, 1, "可取回的讲次只取一次（缓存复用）");
  assert.equal(net.calls.filter((c) => c.includes("sub-syn-0002")).length, 1, "永久失败的讲次不反复重试（无请求风暴）");
  tabNodes.topics.click();
  tabNodes.lectures.click();
  await settle();
  assert.equal(net.calls.filter((c) => c.includes("course-review/lecture")).length, 2, "反复切视图不再新增取数");
}

/* RR-ANCHORFE-1：要点锚点芯片——有锚才渲染时间芯片，点击走导航出口定位；无锚不渲染 */
{
  const anchored = JSON.parse(JSON.stringify(VIEWS.lecture_detail));
  anchored.key_points[0].anchor_ms = 125000;
  net.lectureDetail = anchored;
  jumpLog = [];
  reloadButton.click();
  await settle();
  await settle();
  await settle();
  const chips = panelNodes.lectures.querySelectorAll(".cr-point-anchor");
  assert.equal(chips.length, 1, "只有带锚要点渲染时间芯片");
  assert.equal(chips[0].textContent, "2:05", "芯片显示 m:ss 时间");
  chips[0].click();
  assert.deepEqual(jumpLog, ["transcript:sub-syn-0001@125000"], "芯片点击走导航出口定位到锚");
  assert.equal(
    panelNodes.lectures.querySelectorAll(".cr-point").length,
    anchored.key_points.length,
    "要点条目数不因锚增减",
  );
  net.lectureDetail = null;
}

/* 练习与真题：答案来源诚实默认、无答案不展开标准答案 */
tabNodes.assessment.click();
await settle();
await settle();
{
  const items = panelNodes.assessment.querySelectorAll(".cr-question");
  assert.equal(items.length, 1, "题目引用视图渲染");
  assert.equal(panelNodes.assessment.querySelector(".cr-answer-source").textContent, "暂无官方答案");
  assert.ok(!/查看参考答案/.test(panelNodes.assessment.textContent), "无答案不展开标准答案区域");
  assert.match(panelNodes.assessment.textContent, /第 2 题/);
}

/* N7A 形状的题目（带答案 + 真枚举）：AI 的绝不写成「参考答案」 */
{
  const original = globalThis.fetch;
  const n7aItems = [
    { item_id: "n7a-1", course_id: COURSE_ID, sub_id: "sub-syn-0001", document_id: "d1", question_no: 5, label: "第 5 题", content_hash: "a".repeat(64), citation_ids: [], answer_source: "ai_generated", has_answer: true, answer: "AI 给的推导", ai_explanation: "", status: "answer_available" },
    { item_id: "n7a-2", course_id: COURSE_ID, sub_id: "", document_id: "d2", question_no: 6, label: "第 6 题", content_hash: "b".repeat(64), citation_ids: [], answer_source: "none", has_answer: false, answer: "", ai_explanation: "", status: "question_only" },
    { item_id: "n7a-3", course_id: COURSE_ID, sub_id: "", document_id: "d3", question_no: 7, label: "第 7 题", content_hash: "c".repeat(64), citation_ids: [], answer_source: "teacher_material", has_answer: true, answer: "教师给的标准解", ai_explanation: "", status: "answer_available" },
  ];
  globalThis.fetch = async (url) => {
    const text = String(url);
    const json = (payload) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => payload });
    if (text.includes("course-review/lecture")) return json({ error_code: "lecture_not_found", error: "no detail" });
    if (text.includes("course-review")) return json(envelope({
      view: VIEWS.course_overview,
      assessment_workspace: { contract: review.CONTRACT_ID, view: "assessment_workspace", course_id: COURSE_ID, items: n7aItems, counts: { total: 3, lectures: 1, course_level: 2 } },
      snapshot: null, state: "ready",
    }));
    return json(envelope({}));
  };
  await review.openCourseReview(STORE);
  reloadButton.click();
  await settle();
  await settle();
  tabNodes.assessment.click();
  await settle();
  await settle();
  const cards = panelNodes.assessment.querySelectorAll(".cr-question");
  assert.equal(cards.length, 3, "N7A 形状的三道题全部渲染");
  const labels = Array.from(panelNodes.assessment.querySelectorAll(".cr-answer-source")).map((n) => n.textContent);
  assert.deepEqual(labels, ["AI 解答（非官方）", "暂无官方答案", "教师资料"], "答案来源逐条按 N7A 枚举标注");
  const aiCard = cards.find((c) => /第 5 题/.test(c.textContent));
  assert.match(aiCard.textContent, /查看 AI 解答（非官方，仅供理解）/, "AI 答案明写非官方");
  assert.ok(!/查看参考答案/.test(aiCard.textContent), "AI 答案绝不写成「参考答案」");
  const teacherCard = cards.find((c) => /第 7 题/.test(c.textContent));
  assert.match(teacherCard.textContent, /查看教师资料/, "教师资料用自己的措辞");
  const noAnswerCard = cards.find((c) => /第 6 题/.test(c.textContent));
  assert.ok(!/查看/.test(noAnswerCard.textContent), "无答案不给展开入口");
  assert.match(noAnswerCard.textContent, /暂无官方答案/);
  globalThis.fetch = original;
}

/* N8A 题目 AI 解答/解析：单按钮、五种状态、引用跳转、失败不自动重试 */
{
  const original = globalThis.fetch;
  const explainCalls = [];
  let answerMode = "queued";
  let reviewStatePractice = null;
  const aiItems = [
    {
      item_id: "ai-1", course_id: COURSE_ID, sub_id: "sub-syn-0001", document_id: "d1",
      question_no: 5, label: "第 5 题 求傅里叶变换", content_hash: "a".repeat(64),
      citation_ids: [], answer_source: "none", has_answer: false, answer: "",
      ai_explanation: "", status: "question_only",
    },
    {
      /* 本地练习（无文档身份）：后端会 fail-closed，前端也不该摆按钮。 */
      item_id: "quiz:abc123", course_id: COURSE_ID, sub_id: "sub-syn-0001", document_id: "",
      question_no: 0, label: "用自己的话复述这一段的要点", content_hash: "b".repeat(32),
      citation_ids: [], answer_source: "none", has_answer: false, answer: "",
      ai_explanation: "", status: "question_only",
    },
  ];
  const practiceWithItems = {
    view: "generated_quiz", label: "本课程练习", course_id: COURSE_ID,
    source_label: review.LOCAL_PRACTICE_SOURCE_LABEL,
    answer_visible_before_submit: false,
    counts: { total: 2, answered: 1, wrong: 1, unanswered: 1, lectures: 1 },
    lectures: [{ sub_id: "sub-syn-0001", label: "第一讲", count: 2, answered: 1, wrong: 1 }],
    items: [
      { quiz_id: "q1", sub_id: "sub-syn-0001", lecture_label: "第一讲", question: "复述要点", difficulty: "medium", answered: true, wrong: true, ungraded: 0, attempts: 1, evidence: { start_ms: 60000, end_ms: 90000 } },
    ],
  };
  reviewStatePractice = practiceWithItems;
  globalThis.fetch = async (url, options = {}) => {
    const text = String(url);
    const json = (payload, status = 200) => ({
      ok: status >= 200 && status < 300, status,
      headers: { get: () => "application/json" },
      json: async () => payload, text: async () => JSON.stringify(payload),
    });
    if (text.includes("course-review/actions")) {
      const body = JSON.parse(String(options.body || "{}"));
      explainCalls.push(body);
      if (answerMode === "failed") {
        return json({ error: "unavailable", error_code: "ai_key_missing" }, 400);
      }
      const ai = answerMode === "ready"
        ? {
          ai_state: "ready",
          ai_explanation: "先写定义式，再逐项积分得到结论。",
          ai_citations: [{
            citation_id: "aia:ai-1:document_page:p1", kind: "document_page", source_id: "d1",
            label: "资料 · 第 1 页", snippet: "定义式", locator: { page: 1 },
          }],
          ai_error_code: "",
        }
        : answerMode === "insufficient"
          ? { ai_state: "insufficient", ai_explanation: "", ai_citations: [], ai_error_code: "assessment_evidence_insufficient" }
          : { ai_state: "queued", ai_explanation: "", ai_citations: [], ai_error_code: "" };
      return json(envelope({
        course_id: COURSE_ID, item_id: body.item_id, action: "explain_assessment",
        status: ai.ai_state, created: true, error_code: "", task_id: "task-answer-1",
        ai, observed_at: 0,
      }), 202);
    }
    if (text.includes("course-review/lecture")) return json({ error_code: "lecture_not_found", error: "no detail" });
    if (text.includes("course-review")) {
      return json(envelope({
        view: VIEWS.course_overview,
        assessment_workspace: {
          contract: review.CONTRACT_ID, view: "assessment_workspace", course_id: COURSE_ID,
          items: aiItems, counts: { total: 2, lectures: 1, course_level: 1 },
          local_practice: reviewStatePractice,
        },
        snapshot: null, state: "ready",
      }));
    }
    return json(envelope({}));
  };
  const reloadAssessment = async () => {
    reloadButton.click();
    await settle();
    await settle();
    tabNodes.assessment.click();
    await settle();
    await settle();
  };
  const cardOf = (pattern) => panelNodes.assessment.querySelectorAll(".cr-question")
    .find((node) => pattern.test(node.textContent));

  await review.openCourseReview(STORE);
  await reloadAssessment();
  {
    const aiCard = cardOf(/第 5 题/);
    const quizCard = cardOf(/复述这一段的要点/);
    assert.ok(aiCard, "作业/真题卡片渲染");
    assert.match(aiCard.textContent, /生成 AI 解答/, "有文档身份的题给单个动作按钮");
    assert.ok(!/生成 AI 解答/.test(quizCard.textContent), "本地练习不摆云端解答按钮");
    assert.equal(explainCalls.length, 0, "渲染本身绝不请求解答");

    /* 点击 → 恰好一次 POST（带 item_id 与内容版本） */
    const button = aiCard.querySelectorAll(".cr-chip").find((node) => /生成 AI 解答/.test(node.textContent));
    button.click();
    await settle();
    assert.equal(explainCalls.length, 1, "点击只发一次请求");
    assert.equal(explainCalls[0].action, "explain_assessment");
    assert.equal(explainCalls[0].item_id, "ai-1");
    assert.equal(explainCalls[0].content_hash, "a".repeat(64), "带上当前内容身份");
    assert.equal(explainCalls[0].retry, false, "首次点击不是重试");
    assert.match(panelNodes.assessment.textContent, /正在生成 AI 解答…/, "排队/生成中给忙碌文案");

    /* 排队期间切视图再回来：不重复派发 */
    tabNodes.lectures.click();
    await settle();
    tabNodes.assessment.click();
    await settle();
    assert.equal(explainCalls.length, 1, "在途期间重渲染不重复派发");

    /* WAIT-UX-1 缺进度 E6：排队/生成中超阈值未收口即换长等两态（>10s 三律档）。
       202 先回 queued 的窗口里视图态停留排队——render 期按提交时刻现算，
       这里用假钟推进 12s 后切视图重渲染，无需真等 10s。 */
    {
      const realNow = Date.now;
      Date.now = () => realNow.call(Date) + 12000;
      try {
        tabNodes.lectures.click();
        await settle();
        tabNodes.assessment.click();
        await settle();
        assert.match(
          panelNodes.assessment.textContent,
          /已经提交一会儿了/,
          "长等两态：超阈值未收口给「可先离开」提示",
        );
        assert.ok(
          !/已排队，等在线计算空出来就开始/.test(panelNodes.assessment.textContent),
          "长等态不再显示首轮排队文案",
        );
      } finally {
        Date.now = realNow;
      }
      /* 真钟恢复后切回：回到排队首轮文案（两态随真实等待时长收敛） */
      tabNodes.lectures.click();
      await settle();
      tabNodes.assessment.click();
      await settle();
      assert.match(
        panelNodes.assessment.textContent,
        /已排队，等在线计算空出来就开始/,
        "等待未超阈值时保持首轮排队文案",
      );
    }
  }

  /* 完成态：结果 + 可跳转引用，且不再摆按钮（重复点不会重复烧云）
     没有材料答案时后端会把 AI 文本升格成答案（answer_source=ai_generated），
     这里就用后端真实产出的形状：正文只出现一次，不重复两块。 */
  {
    const aiCitation = {
      citation_id: "aia:ai-1:document_page:p1", kind: "document_page", source_id: "d1",
      label: "资料 · 第 1 页", snippet: "定义式", locator: { page: 1 },
    };
    aiItems[0] = {
      ...aiItems[0], ai_state: "ready", answer_source: "ai_generated", has_answer: true,
      answer: "先写定义式，再逐项积分得到结论。",
      ai_explanation: "先写定义式，再逐项积分得到结论。",
      ai_citations: [aiCitation],
    };
    await reloadAssessment();
    const done = cardOf(/第 5 题/);
    assert.match(done.textContent, /先写定义式，再逐项积分得到结论/, "AI 解答正文落在本地读面");
    assert.match(done.textContent, /查看 AI 解答（非官方，仅供理解）/, "AI 解答明写非官方");
    assert.equal(
      done.textContent.split("先写定义式").length - 1, 1,
      "同一段 AI 正文只显示一次，不重复成两块",
    );
    assert.ok(!done.querySelectorAll(".cr-chip").some((node) => /生成 AI 解答|重新生成|重试/.test(node.textContent)),
      "完成态不再摆按钮");
    jumpLog = [];
    const jump = done.querySelector(".cr-citation-jump");
    assert.ok(jump, "AI 引用给跳转按钮");
    jump.click();
    assert.equal(jumpLog.length, 1, "引用点击触发一次跳转");
    assert.match(jumpLog[0], /^documents:d1#1$/, "资料页引用按页定位");
    const before = explainCalls.length;
    tabNodes.lectures.click();
    await settle();
    tabNodes.assessment.click();
    await settle();
    assert.equal(explainCalls.length, before, "完成后再重渲染也不请求");

    /* 有材料答案时：原答案与来源不动，AI 只出解析块 */
    aiItems[0] = {
      ...aiItems[0], answer_source: "teacher_material", has_answer: true, answer: "教师给的标准解",
    };
    await reloadAssessment();
    const mixed = cardOf(/第 5 题/);
    assert.match(mixed.textContent, /查看教师资料/, "材料答案的来源照旧标注");
    assert.match(mixed.textContent, /教师给的标准解/, "AI 解答绝不覆盖材料答案");
    assert.match(mixed.textContent, /AI 解析（非官方，仅供理解）/, "有材料答案时 AI 只出解析");
    assert.equal(mixed.querySelector(".cr-answer-source").textContent, "教师资料", "答案来源标签如实");
    assert.ok(!/暂无官方答案/.test(mixed.textContent), "有答案就不说没有答案");
    assert.ok(!/查看 AI 解答（非官方，仅供理解）/.test(mixed.textContent),
      "有材料答案时不把 AI 文本说成答案");
  }

  /* 失败态：如实报错、给重试按钮，但绝不自动重试 */
  {
    aiItems[0] = { ...aiItems[0], ai_state: "failed", ai_error_code: "ai_key_missing", ai_explanation: "", ai_citations: [] };
    await reloadAssessment();
    const failed = cardOf(/第 5 题/);
    const button = failed.querySelectorAll(".cr-chip").find((node) => /重试/.test(node.textContent));
    assert.ok(button, "失败后给重试按钮");
    answerMode = "failed";
    button.click();
    await settle();
    await settle();
    assert.equal(explainCalls[explainCalls.length - 1].retry, true, "重试由用户点击显式带出");
    const failedAgain = cardOf(/第 5 题/);
    assert.match(failedAgain.textContent, /这次没跑成/, "失败给人话");
    const callsAfterFailure = explainCalls.length;
    tabNodes.lectures.click();
    await settle();
    tabNodes.assessment.click();
    await settle();
    tabNodes.lectures.click();
    await settle();
    tabNodes.assessment.click();
    await settle();
    assert.equal(explainCalls.length, callsAfterFailure, "失败绝不因重渲染而自动重试");
    assert.ok(cardOf(/第 5 题/).querySelectorAll(".cr-chip").some((node) => /重试/.test(node.textContent)),
      "重试只能由用户再点一次");
  }

  /* 资料不足：诚实文案，不补外部知识 */
  {
    aiItems[0] = { ...aiItems[0], ai_state: "insufficient", ai_error_code: "assessment_evidence_insufficient", ai_explanation: "", ai_citations: [] };
    await reloadAssessment();
    const declined = cardOf(/第 5 题/);
    const button = declined.querySelectorAll(".cr-chip").find((node) => /补齐后重试/.test(node.textContent));
    assert.ok(button, "资料不足也给一个明确的下一步");
    answerMode = "insufficient";
    button.click();
    await settle();
    await settle();
    assert.match(cardOf(/第 5 题/).textContent, /资料不足，无法根据当前课程资料回答/, "资料不足如实说");
  }

  /* 本课程练习分组：有题按课次进入；空题库给单一动作 */
  {
    assert.match(panelNodes.assessment.textContent, /本课程练习/, "本课程练习分组渲染");
    assert.match(panelNodes.assessment.textContent, /课程字幕依据（非官方）/, "来源说清是课程字幕");
    assert.match(panelNodes.assessment.textContent, /共 2 题/);
    assert.match(panelNodes.assessment.textContent, /已作答 1 题/, "作答统计复用既有 quiz_attempts");
    assert.ok(!/标准答案|参考答案/.test(panelNodes.assessment.textContent), "本地练习不冒充真题答案");
    jumpLog = [];
    const enter = panelNodes.assessment.querySelectorAll(".cr-chip").find((node) => /去这一讲练/.test(node.textContent));
    assert.ok(enter, "按课次进入");
    enter.click();
    assert.equal(jumpLog.length, 1, "进入走导航出口");
    assert.match(jumpLog[0], /^assessment:sub-syn-0001$/);

    reviewStatePractice = {
      view: "generated_quiz", label: "本课程练习", course_id: COURSE_ID,
      source_label: review.LOCAL_PRACTICE_SOURCE_LABEL,
      answer_visible_before_submit: false,
      counts: { total: 0, answered: 0, wrong: 0, unanswered: 0, lectures: 0 },
      lectures: [], items: [],
      empty_action: {
        action: "open_quiz_entry", label: "去生成本课程练习",
        hint: "还没有本课程练习。到某一讲的「测验与复习」里生成一次，题目就会汇总到这里。",
        sub_id: "sub-syn-0001",
      },
    };
    await reloadAssessment();
    assert.match(panelNodes.assessment.textContent, /还没有本课程练习/, "空题库给明确说明");
    jumpLog = [];
    const generate = panelNodes.assessment.querySelectorAll(".cr-chip").find((node) => /去生成本课程练习/.test(node.textContent));
    assert.ok(generate, "空题库给单一动作");
    generate.click();
    assert.equal(jumpLog.length, 1, "单一动作复用既有生成入口");
    assert.match(jumpLog[0], /^assessment:sub-syn-0001$/);
  }
  globalThis.fetch = original;
}

/* 更新：连点三次只发一次 POST，结果按 queued/skipped/blocked 分列 */
net.refreshCalls = 0;
net.calls = [];
refreshButton.click();
refreshButton.click();
refreshButton.click();
await settle();
await settle();
assert.equal(net.refreshCalls, 1, "连点三次只派发一次 refresh");
{
  const text = refreshResultNode.textContent;
  assert.match(text, /已安排 2 个讲次/);
  assert.match(text, /1 个讲次已是最新/);
  assert.match(text, /1 个讲次暂时没法整理/);
  assert.match(text, /还没有字幕/, "阻塞原因用人话给出");
  assert.equal(refreshButton.disabled, false, "完成后按钮恢复可用");
  assert.equal(refreshButton.getAttribute("aria-busy"), "false");
}

/* 键盘：ArrowRight 环绕切视图并移动焦点 */
{
  tabNodes.topics.focus();
  tabNodes.topics.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
  assert.equal(tabNodes.lectures.getAttribute("aria-selected"), "true", "ArrowRight 切到下一视图");
  assert.equal(document.activeElement, tabNodes.lectures, "焦点随选中移动");
  assert.equal(panelNodes.lectures.hidden, false);
  assert.equal(panelNodes.topics.hidden, true);
}

/* 返回态：退出→焦点归还入口；重进恢复原 tab */
{
  tabNodes.assessment.click();
  await settle();
  assert.equal(review.reviewContext().tab, "assessment", "上下文记录当前 tab");
  review.exitCourseReview();
  assert.equal(surface.hidden, true, "退出隐藏 surface");
  assert.equal(courseLayout.hidden, false, "退出恢复课程布局");
  assert.equal(document.activeElement, entryButton, "焦点归还入口按钮");
  assert.equal(review.isCourseReviewOpen(), false);
  /* VA-P2-02 不变量「选中 tab⇒面板可见」：surface 藏后不留悬空选中声明 */
  for (const tab of Object.values(tabNodes)) {
    assert.equal(tab.getAttribute("aria-selected"), "false", "退出后无 tab 仍声明选中");
    assert.ok(!tab.classList.contains("active"), "退出后无 active 残留");
    assert.equal(tab.tabIndex, -1, "退出后 tab 移出 Tab 序");
  }
  for (const panel of Object.values(panelNodes)) {
    assert.equal(panel.hidden, true, "退出后全部面板收起");
  }
  review.restoreCourseReview(STORE);
  assert.equal(review.isCourseReviewOpen(), true, "返回总体复习");
  assert.equal(review.reviewContext().tab, "assessment", "恢复原 tab");
  /* 重开路径原位复钉：aria/active/面板可见性与 reviewState.tab 三面一致 */
  assert.equal(tabNodes.assessment.getAttribute("aria-selected"), "true", "恢复后选中态复钉");
  assert.ok(tabNodes.assessment.classList.contains("active"), "恢复后 active 复钉");
  assert.equal(panelNodes.assessment.hidden, false, "恢复后面板可见性复钉");
  assert.equal(panelNodes.lectures.hidden, true, "恢复后兄弟面板保持收起");
  assert.equal(tabNodes.topics.getAttribute("aria-selected"), "false", "兄弟 tab 不误选");
}

/* 七展示态在真树上逐态走通 */
for (const [mode, expected] of [["partial", "partial"], ["stale", "stale"], ["error", "failed"], ["snapshot_invalid", "conflict"], ["unknown_status", "unknown"], ["empty", "empty"]]) {
  net.mode = mode;
  await review.openCourseReview(STORE);
  await settle();
  assert.equal(statusNode.dataset.state, expected, `展示态 ${mode} → ${expected}`);
  assert.ok(statusNode.textContent.trim().length > 0, `展示态 ${mode} 有说明`);
  assert.equal(surface.hidden, false, `展示态 ${mode} 仍可操作（不清空页面）`);
  if (mode === "stale" || mode === "empty") {
    /* 同课程重开有意保留上次 tab：先切到知识脉络再断言该面板 */
    tabNodes.topics.click();
  }
  if (mode === "stale") {
    assert.match(statusNode.textContent, /有新资料待更新/);
    assert.ok(panelNodes.topics.querySelectorAll(".cr-topic").length > 0, "stale 保留旧内容");
  }
  if (mode === "empty") {
    assert.equal(panelNodes.topics.querySelectorAll(".cr-topic").length, 0, "empty 不再显示旧知识脉络");
    assert.match(panelNodes.topics.textContent, /还没有形成知识脉络/, "empty 给出下一步而不是空白");
    /* EMPTY-STATES-1：三个面板空态都带「更新课程知识」真动作（同一既有 refresh 链路），
       文案零技术词、零责备；点击实证走既有 course-review/actions 请求。 */
    const emptyRefresh = panelNodes.topics.querySelector(".empty-action");
    assert.ok(emptyRefresh, "知识脉络空态带「更新课程知识」动作钮");
    assert.equal(emptyRefresh.textContent, "更新课程知识", "动作钮文案闭集");
    const refreshCallsBefore = net.refreshCalls;
    emptyRefresh.click();
    await settle();
    assert.ok(net.refreshCalls > refreshCallsBefore, "空态动作走既有更新课程知识请求");
    tabNodes.lectures.click();
    await settle();
    assert.match(panelNodes.lectures.textContent, /还没有整理到讲次/, "讲次空态给出来源与出路");
    assert.ok(panelNodes.lectures.querySelector(".empty-action"), "讲次空态带动作钮");
    tabNodes.assessment.click();
    await settle();
    /* assessment 视图按 restore 语义保留上一版可用内容，不强行清空——
       只有真出现空态节点时才钉「一句话+动作钮」形态（source 级文案钉在 py workbench）。 */
    const assessmentEmptyNode = panelNodes.assessment.querySelector(".empty-state");
    if (assessmentEmptyNode) {
      assert.match(assessmentEmptyNode.textContent, /还没有整理到题目/, "题目空态给出来源与出路");
      assert.ok(assessmentEmptyNode.querySelector(".empty-action"), "题目空态带动作钮");
    }
  }
  if (mode === "unknown_status") {
    assert.ok(!/已就绪/.test(statusNode.textContent), "未知 status 不宣称就绪");
  }
}
net.mode = "as-fixture";

/* 取数失败：不清空、不假装成功 */
{
  net.failOverview = false;
  net.mode = "as-fixture";
  await review.openCourseReview(STORE);
  await settle(); /* 先装载有内容的一版，作为「上一版可用」的底 */
  net.failOverview = true;
  await review.openCourseReview(STORE);
  await settle();
  assert.equal(statusNode.dataset.tone, "danger", "有上一版可看：降级展示如实报 danger");
  assert.match(statusNode.textContent, /稍后再试|更新课程知识/);
  assert.ok(!/已就绪/.test(statusNode.textContent), "失败不伪装完成");
  net.failOverview = false;
}

/* VA-P3-06 语气分岔：errorCode + 无内容 = caution 金（还没就绪≠出错） */
{
  net.failOverview = true;
  const originalCourse = STORE.activeCourse;
  STORE.activeCourse = { course_id: "crs-never-built", title: "从未整理过的课" };
  await review.openCourseReview(STORE);
  await settle();
  assert.equal(statusNode.dataset.state, "unavailable");
  assert.equal(statusNode.dataset.tone, "caution", "首次无内容不是错误，不吓学生");
  assert.match(statusNode.textContent, /更新课程知识|稍后再试/);
  STORE.activeCourse = originalCourse;
  net.failOverview = false;
}

/* 长列表：50 讲 / 500 题渐进展开，DOM 不一次塞满 */
{
  const bigLectures = Array.from({ length: 50 }, (_, i) => ({ sub_id: `sub-big-${i}`, sub_title: `第 ${i + 1} 讲 ${"很长的讲次标题".repeat(4)}`, status: "ready", key_point_count: 3, source_coverage: { transcript_segments: "1" } }));
  const bigItems = Array.from({ length: 500 }, (_, i) => ({ item_id: `item-${i}`, label: `第 ${i + 1} 题 ${"较长的题干".repeat(4)}`, question_no: i + 1 }));
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    const text = String(url);
    const json = (payload) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => payload });
    if (text.includes("course-review/lecture")) return json({ error_code: "lecture_not_found", error: "no detail" });
    if (text.includes("course-review/assessment")) return json(envelope({ contract: review.CONTRACT_ID, view: "assessment_workspace", course_id: COURSE_ID, items: bigItems, counts: { total: 500 } }));
    if (text.includes("course-review")) {
      const bigView = { ...VIEWS.course_overview, lectures: bigLectures, topics: [], assessment: { total: 500, lectures_with_items: 0 } };
      return json(envelope({ view: bigView, assessment_workspace: { contract: review.CONTRACT_ID, view: "assessment_workspace", course_id: COURSE_ID, items: bigItems, counts: { total: 500, lectures: 0, course_level: 500 } }, snapshot: null, state: "ready" }));
    }
    return json(envelope({}));
  };
  net.mode = "as-fixture";
  await review.openCourseReview(STORE);
  /* 同课程重开有意复用缓存（题目与讲次详情各取一次）；换载荷必须走显式重新载入。 */
  reloadButton.click();
  await settle();
  await settle();
  tabNodes.lectures.click();
  await settle();
  assert.equal(panelNodes.lectures.querySelectorAll(".cr-lecture").length, 8, "50 讲只渲染首批 8 讲");
  assert.match(panelNodes.lectures.querySelector(".cr-more").textContent, /还有 42 讲/);
  panelNodes.lectures.querySelector(".cr-more").click();
  assert.equal(panelNodes.lectures.querySelectorAll(".cr-lecture").length, 50, "展开后补齐");
  tabNodes.assessment.click();
  await settle();
  await settle();
  assert.equal(panelNodes.assessment.querySelectorAll(".cr-question").length, 25, "500 题只渲染首批 25 道");
  assert.match(panelNodes.assessment.querySelector(".cr-more").textContent, /还有 475 道/);
  globalThis.fetch = original;
}

/* COURSEMEM-1：课程记忆芯片——有积累才出现，计数真实，零积累不渲染 */
{
  const original = globalThis.fetch;
  const json = (payload) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => payload });
  globalThis.fetch = async (url) => {
    const text = String(url);
    if (text.includes("course-review/lecture")) return json({ error_code: "lecture_not_found", error: "no detail" });
    if (text.includes("course-review")) return json(envelope({
      view: VIEWS.course_overview,
      assessment_workspace: VIEWS.assessment_workspace,
      snapshot: null, state: "ready",
      course_memory: { examples: 3 },
    }));
    return json(envelope({}));
  };
  await review.openCourseReview(STORE);
  reloadButton.click();
  await settle();
  await settle();
  const chips = Array.from(coverageNode.querySelectorAll(".cr-coverage-item"));
  const memoryChip = chips.find((n) => /术语记忆/.test(n.textContent));
  assert.ok(memoryChip, "课程记忆芯片随覆盖条渲染");
  assert.match(memoryChip.textContent, /3/, "芯片展示真实计数");
  assert.equal(memoryChip.title.length > 0, true, "芯片带人话提示");

  globalThis.fetch = async (url) => {
    const text = String(url);
    if (text.includes("course-review/lecture")) return json({ error_code: "lecture_not_found", error: "no detail" });
    if (text.includes("course-review")) return json(envelope({
      view: VIEWS.course_overview,
      assessment_workspace: VIEWS.assessment_workspace,
      snapshot: null, state: "ready",
      course_memory: { examples: 0 },
    }));
    return json(envelope({}));
  };
  reloadButton.click();
  await settle();
  await settle();
  assert.ok(
    !Array.from(coverageNode.querySelectorAll(".cr-coverage-item")).some((n) => /术语记忆/.test(n.textContent)),
    "零积累不渲染课程记忆芯片",
  );
  globalThis.fetch = original;
}

/* 窄屏与长标题：结构上可截断，不靠固定宽度 */
{
  const css = readFileSync(join(repoRoot, "frontend", "styles", "pages.css"), "utf8");
  assert.match(css, /@media \(max-width: 640px\)/, "窄屏断点在 640");
  assert.match(css, /\.cr-lecture-title \{[^}]*text-overflow: ellipsis/, "长标题截断而非撑破容器");
  assert.match(css, /\.cr-tab \{ flex: 1 1 33%/, "窄屏三 tab 等分一行");
  assert.ok(!/\.cr-[^}]*\{[^}]*min-width: \d{3,}px/.test(css), "复习面不引入破坏窄屏的固定宽度");
}

/* VISUAL-BACKLOG-1：VA-P2-03/04/05 积压清偿钉 —— 失败态统一配方 / tab 活跃态
 * 收敛主面下划线语言 / 区块小标题档立项。 */
{
  const pages = readFileSync(join(repoRoot, "frontend", "styles", "pages.css"), "utf8");
  const comp = readFileSync(join(repoRoot, "frontend", "styles", "components.css"), "utf8");
  const flashCss = readFileSync(join(repoRoot, "frontend", "styles", "flashcards.css"), "utf8");
  const renderSrc = readFileSync(join(repoRoot, "frontend", "modules", "course-review", "render.js"), "utf8");
  const flashSrc = readFileSync(join(repoRoot, "frontend", "modules", "course-flashcards.js"), "utf8");
  const chipSrc = readFileSync(join(repoRoot, "frontend", "modules", "quality-chip.js"), "utf8");
  /* VA-P2-03：兄弟 tab 失败态同走 .error-note danger 紧凑卡（单源 components.css），
     闪卡侧 .p4f-error 死规则清零，练习失败不再借空态灰字。 */
  assert.match(comp, /\.error-note \{[^}]*border: 1px solid color-mix\(in srgb, var\(--danger\) 42%, transparent\)/, "error-note danger 边与原 p4f-error 配方同值");
  assert.match(comp, /\.error-note \{[^}]*background: var\(--danger-soft\)/, "error-note danger 软底");
  assert.match(comp, /\.error-note p \{ margin: 0; \}/, "error-note 内文 margin 归零");
  assert.match(pages, /#cr-panel-assessment \.error-note \{ margin: var\(--space-2\) 2px var\(--space-3\); \}/, "练习失败卡与 cr-status 同节奏");
  assert.ok(renderSrc.includes('el("p", "error-note"'), "练习失败渲染走 error-note");
  assert.ok(flashSrc.includes('el("div", "error-note"'), "闪卡失败渲染走 error-note");
  assert.ok(!/\.p4f-error\s*[,{]/.test(flashCss), "flashcards.css 无 .p4f-error 残留死规则（注释提及不算）");
  /* VA-P3-04：cr-tab 活跃态=materials 同款下划线配方；基础态 2px 透明下边占位
     （padding-bottom 同步补偿，tab 栏总高不变、切换零跳动）。 */
  assert.match(pages, /\.cr-tab\.active \{ border-bottom-color: var\(--navy\); color: var\(--ink\); font-weight: 600; \}/, "cr-tab active 走 materials 下划线配方");
  assert.match(pages, /\.cr-tab \{[^}]*border-bottom: 2px solid transparent/, "cr-tab 基础态透明下边占位");
  assert.ok(!/\.cr-tab\.active \{[^}]*background: var\(--surface\)/.test(pages), "cr-tab active 不再是描边盒");
  /* VA-P3-05：区块小标题档立项（既有字阶 0.875rem/600/navy-ink，零新视觉语言），
     首消费点=quality-chip 复核区头；调板金/抽屉 h3 记档保留不迁。 */
  assert.match(comp, /\.section-label \{ color: var\(--navy-ink\); font-size: 0\.875rem; font-weight: 600;/, "section-label 档=既有字阶 navy-ink 600");
  assert.ok(chipSrc.includes('header.className = "section-label"'), "复核区头走 section-label 档");
}

/* ---------------- THINK-LADDER-2：课程记忆候选复核面 + 质量 chip 纯函数 ---------------- */

{
  const quality = await import("../frontend/modules/quality-chip.js");

  /* 抽检报告归一：三态派生 + 闭集码人话 + 未知降级 */
  const passed = quality.normalizeQualityArtifact({ artifact: { content: {
    subtitle: { findings: [{ target: "segment", position: 2, dimension: "readability", code: "broken_flow", severity: "info" }] },
    summary: { findings: [] },
  } } });
  assert.equal(passed.state, "passed", "零 warn=抽检通过");
  assert.equal(passed.infoCount, 1);
  const suspect = quality.normalizeQualityArtifact({ artifact: { content: {
    subtitle: { findings: [
      { target: "segment", position: 0, dimension: "term_fidelity", code: "homophone_suspect", severity: "warn" },
      { target: "segment", position: 3, dimension: "term_fidelity", code: "made_up_code", severity: "warn" },
    ] },
    summary: { findings: [{ target: "chapter", position: 1, dimension: "factuality", code: "no_source_support", severity: "warn" }] },
  } } });
  assert.equal(suspect.state, "suspect", "warn>0=存疑");
  assert.equal(suspect.warnCount, 3);
  assert.equal(suspect.findings[0].text, "可能有同音字听错", "闭集码翻译成人话");
  assert.equal(suspect.findings[0].where, "第 1 段", "position 从 0 起算转 1 起人话");
  assert.equal(suspect.findings[1].text, "这一处可能有点问题", "未知 code 中性降级不编含义");
  assert.equal(suspect.findings[2].where, "第 2 章", "summary 目标单位独立");
  assert.equal(quality.normalizeQualityArtifact({ artifact: null }).state, "none", "空载荷=未抽检");
  assert.equal(quality.normalizeQualityArtifact({}).state, "none", "缺 artifact=未抽检");

  /* 复核视图归一：行形状收敛 + 缺席/畸形=null */
  assert.equal(quality.normalizeTermCandidateView({}), null, "缺 term_candidates=null");
  assert.equal(quality.normalizeTermCandidateView({ term_candidates: { rows: "junk" } }).rows.length, 0, "行坏=空表");
  const view = quality.normalizeTermCandidateView({ term_candidates: {
    rows: [
      { wrong: "小波基的选取原里", right: "小波基的选取原理", lecture_count: 2, total_count: 5, signal_count: 2, updated_at: 1 },
      { wrong: "", right: "孤儿", lecture_count: 1, total_count: 1, signal_count: 0 },
    ],
    confirmed_count: 1,
    auto_confirmed_rows: [{ wrong: "费米能及", right: "费米能级", signal_count: 3 }],
  } });
  assert.equal(view.rows.length, 1, "残行丢弃");
  assert.equal(view.autoConfirmedRows[0].signalCount, 3, "自动晋升行带信号数");
}

/* 复核区渲染：自动晋升件带「自动确认 · 信号 ×N」+ 撤销；候选行确认/忽略；
   两清单全空=整区隐藏（无空壳）；动作体=既有 course-review/actions 闭集 */
{
  const actions = [];
  const original = globalThis.fetch;
  net.termCandidates = {
    rows: [
      { wrong: "小波基的选取原里", right: "小波基的选取原理", lecture_count: 2, total_count: 5, signal_count: 2, updated_at: 1 },
    ],
    confirmed_count: 1,
    auto_confirmed_rows: [{ wrong: "费米能及", right: "费米能级", signal_count: 3 }],
  };
  globalThis.fetch = async (url, options = {}) => {
    const text = String(url);
    const method = String(options.method || "GET").toUpperCase();
    net.calls.push(`${method} ${text}`);
    const json = (payload, status = 200) => ({
      ok: status >= 200 && status < 300, status,
      headers: { get: () => "application/json" },
      json: async () => payload, text: async () => JSON.stringify(payload),
    });
    if (text.includes("/api/v3/course-review/actions")) {
      actions.push(JSON.parse(String(options.body || "{}")));
      return json(envelope({ dismissed: true, total_dismissed: 1 }));
    }
    if (text.includes("/api/v3/course-review")) {
      const view = { ...VIEWS.course_overview };
      const data = { view, assessment_workspace: VIEWS.assessment_workspace, snapshot: null, state: view.status, diagnostics: {}, observed_at: 0 };
      if (net.termCandidates) data.term_candidates = net.termCandidates;
      return json(envelope(data));
    }
    return json(envelope({}));
  };
  reloadButton.click();
  await settle(); await settle(); await settle();
  assert.equal(termRegion.hidden, false, "有清单时复核区可见");
  assert.match(termRegion.textContent, /课程记忆复核/, "复核区标题人话");
  assert.match(termRegion.textContent, /费米能及 → 费米能级（自动确认 · 信号 ×3）/, "自动晋升件带 provenance 人话标");
  assert.match(termRegion.textContent, /小波基的选取原里 → 小波基的选取原理（2 讲 · 共 5 次）/, "候选行带真实计数");
  assert.match(termRegion.textContent, /1 个候选写法待确认/, "候选数人话");

  const undo = Array.from(termRegion.querySelectorAll("button")).find((b) => b.textContent === "撤销");
  assert.ok(undo, "自动晋升件有撤销按钮");
  undo.click();
  await settle(); await settle(); await settle();
  assert.equal(actions.length, 1, "撤销走既有动作面恰一次");
  assert.equal(actions[0].action, "dismiss_term_candidate", "撤销=dismiss 动作闭集");
  assert.equal(actions[0].wrong, "费米能及");
  assert.equal(actions[0].course_id, COURSE_ID, "动作携带当前课程身份");
  assert.ok(net.calls.filter((c) => c.includes("course-review?")).length >= 2, "动作成功后复核面随概览刷新");

  const confirmButton = Array.from(termRegion.querySelectorAll("button")).find((b) => b.textContent === "确认");
  confirmButton.click();
  await settle(); await settle(); await settle();
  assert.equal(actions.length, 2);
  assert.equal(actions[1].action, "confirm_term_candidate", "确认=confirm 动作闭集");

  /* 两清单全空：整区隐藏（无空壳） */
  net.termCandidates = { rows: [], confirmed_count: 1, auto_confirmed_rows: [] };
  reloadButton.click();
  await settle(); await settle(); await settle();
  assert.equal(termRegion.hidden, true, "全空=整区隐藏");
  assert.equal(termRegion.textContent, "", "隐藏时不留残文案");

  /* 响应缺键（旧后端形状）：null → 依旧隐藏 */
  net.termCandidates = null;
  reloadButton.click();
  await settle(); await settle(); await settle();
  assert.equal(termRegion.hidden, true, "旧形状响应复核区保持隐藏");
  globalThis.fetch = original;
  net.termCandidates = null;
}

/* 卸载后不再持有监听（幂等收口） */
cleanup();
console.log("frontend course review behavior passed");
