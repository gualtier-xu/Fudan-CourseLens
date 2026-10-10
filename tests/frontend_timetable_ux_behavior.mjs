import assert from "node:assert/strict";

/* 课表 UX 行为测试（home-ux-03 更新）：DOM 执行型，仿 tests/frontend_usability_auth_behavior.mjs 桩件法，只装 timetable.js。
 * 小组件 + 周课表浮层新结构下的既有能力保持：
 * UX-1：refresh POST 在途必须有进行中状态（按钮 busy + 摘要文案），finally 恢复，不破坏同步渲染。
 * UX-2：auth 从非 ready 变 ready 自动 load（小组件常驻可见，无需展开盒子）；同一 ready 快照不重复触发。
 * UX-3（新增）：小组件 Now/Next 渲染；点击用原生 dialog 打开周课表（七列网格）；关闭把焦点还给小组件。
 * UX-4（S04-C）：小组件状态区的内联刷新动作只执行刷新，不冒泡触发浮层打开器（状态区在按钮外）。
 * UX-5（S04-C）：点击遮罩关闭周课表浮层；内容点击/划选拖拽不误关；close（Esc/关闭按钮/遮罩共用）归还焦点。
 * UX-6（bugfix-timetable-week-1）：主页“今日与本周安排”/Now/Next/广播只表示真实本周；
 *   浮层 prev/next 只改浏览周（周标签/网格/冲突），开关浮层、refresh、登录切换都不得改写主页真值。
 * UX-7（bugfix-timetable-autorefresh-1）：本周 stale GET 自动触发恰一次 refresh POST（在途被动
 *   文案、无 CTA）；同一 episode 不自动重试；浏览非本周不自动刷新；partial 响应为被动披露无 CTA。
 * fixture 确定性：全部日期/时刻从 BASE_NOW 一次读取推导（不多次读钟），进行中课程窗口钳制在当日内，
 *   消除历史本地 23:40–00:39 跨午夜必假红窗口。 */

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.open = false;
    this.href = "";
    this.type = "";
    this.parent = null;
    this.rect = null;
    this._listeners = new Map();
  }
  setAttribute(name, value) {
    const key = String(name);
    this.attributes.set(key, String(value));
    /* 真实 DOM 中 data-* 属性即 dataset：镜像写入让 [data-*] 选择器按值可查（W7 焦点恢复依赖） */
    if (key.startsWith("data-")) {
      this.dataset[key.replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] = String(value);
    }
  }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  contains(node) {
    for (let current = node; current; current = current.parent) {
      if (current === this) return true;
    }
    return false;
  }
  append(...nodes) {
    for (const node of nodes) node.parent = this;
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    for (const node of this.children) node.parent = null;
    for (const node of nodes) node.parent = this;
    this.children = [...nodes];
  }
  getBoundingClientRect() { return this.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  focus() { globalThis.document.activeElement = this; }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  }
  querySelectorAll(selector) {
    const found = [];
    const matches = (node) => {
      const text = String(selector);
      if (text.startsWith(".")) return String(node.className || "").split(/\s+/).includes(text.slice(1));
      if (text.startsWith("[")) {
        const name = text.slice(1, -1);
        return name.startsWith("data-") ? node.dataset?.[name.replace(/^data-/, "").replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())] !== undefined : node.attributes.has(name);
      }
      return node.tagName === text.toUpperCase();
    };
    const walk = (node) => (node.children || []).forEach((child) => {
      if (matches(child)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this._listeners.get(type)?.delete(listener); }
  dispatchEvent(event) {
    if (!Object.prototype.hasOwnProperty.call(event, "target")) {
      Object.defineProperty(event, "target", { value: this, configurable: true });
    }
    /* 沿父链冒派，镜像真实 DOM 冒泡：内嵌动作按钮误触打开器、浮层内容点击等才有回归意义 */
    for (let node = this; node; node = node.parent) {
      for (const listener of [...(node._listeners.get(event.type) || [])]) listener.call(node, event);
    }
    return true;
  }
}

const byId = Object.fromEntries([
  "schedule-widget", "schedule-week-dialog", "close-schedule-dialog",
  "schedule-summary", "schedule-state", "schedule-widget-state", "schedule-week-tag", "schedule-now",
  "schedule-week-prev", "schedule-week-next", "schedule-week-current", "schedule-refresh",
  "timetable-semester", "timetable-start-date", "set-timetable-start", "export-timetable",
  "week-grid", "schedule-conflicts", "toast-region",
].map((id) => [id, new FakeElement("div", id)]));
byId["schedule-widget"] = new FakeElement("button", "schedule-widget");
byId["schedule-refresh"] = new FakeElement("button", "schedule-refresh");
byId["close-schedule-dialog"] = new FakeElement("button", "close-schedule-dialog");
byId["set-timetable-start"] = new FakeElement("button", "set-timetable-start");

globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${tag}`),
  createDocumentFragment: () => new FakeElement("#document-fragment", "created-fragment"),
  activeElement: null,
  documentElement: { dataset: {} },
  querySelector: () => null,
};
const windowTarget = new EventTarget();
windowTarget.setTimeout = () => 0; /* toast 自动移除不参与断言，避免挂起事件循环 */
windowTarget.clearTimeout = () => {};
let timetableTick = null; /* 捕获 60s 重建 tick，供 W7 焦点恢复用例手动触发 */
windowTarget.setInterval = (fn) => { timetableTick = fn; return 1; };
windowTarget.clearInterval = () => {};
globalThis.window = windowTarget;
if (!globalThis.CustomEvent) {
  globalThis.CustomEvent = class extends Event {
    constructor(type, options = {}) { super(type); this.detail = options.detail; }
  };
}
/* renderSnapshot 的学期下拉会构造 Option：真 Node 无此全局（package F 曾被 try/catch 掩盖） */
globalThis.Option = class Option {
  constructor(text, value) { this.text = String(text ?? ""); this.value = String(value ?? ""); }
};
globalThis.localStorage = { store: new Map(), getItem(k) { return this.store.get(k) ?? null; }, setItem(k, v) { this.store.set(k, String(v)); } };

const ok = (data, status = 200) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
  status, headers: { "Content-Type": "application/json" },
});
const errorResponse = (code, message, status = 502) => new Response(JSON.stringify({ error: message, error_code: code }), {
  status, headers: { "Content-Type": "application/json" },
});
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const settle = async () => { for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const pointerAt = (type, x, y) => {
  const event = new Event(type);
  Object.defineProperty(event, "clientX", { value: x, configurable: true });
  Object.defineProperty(event, "clientY", { value: y, configurable: true });
  return event;
};

/* ---- 确定性时间基准：整个 fixture 只读一次时钟，日期/时刻全部由 BASE_NOW 推导 ----
 * 历史 23:40–00:39 必假红的根因：today 课程用 nowMin±偏移构造，跨午夜时 start/end
 * 各自回绕到不同一日的时刻串，产品按当日区间比较永远匹配不上。liveWindow 把窗口
 * 钳制在 [0,1439] 内且保证 start<end，任何真实基准时刻都不产生跨午夜窗口。 */
const BASE_NOW = new Date();
const pad = (n) => String(n).padStart(2, "0");
const baseIso = (() => {
  const offset = BASE_NOW.getTimezoneOffset() * 60000;
  return new Date(BASE_NOW.getTime() - offset).toISOString().slice(0, 10);
})();
const baseIsoShift = (days) => {
  const shifted = new Date(BASE_NOW.getTime());
  shifted.setDate(shifted.getDate() + days);
  return new Date(shifted.getTime() - shifted.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
const baseMinutes = BASE_NOW.getHours() * 60 + BASE_NOW.getMinutes();
/* 进行中课程窗口：钳制到当日内且必包含 baseMinutes；剩余时间断言不依赖具体分钟数 */
const liveWindow = () => ({
  start: Math.max(0, baseMinutes - 40),
  end: Math.min(baseMinutes + 20, 1439),
});
const hm = (minutes) => `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
const toMinutes = (text) => String(text || "").split(":").reduce((total, part) => total * 60 + Number(part), 0);

/* 快照模板：week=1 为真实本周（days[0]=基准今天）；week=N>1 的浏览周日期整体后移一周，
 * 不包含今天。todayFirst=true 时今天有一节进行中课程 + 明天一节（驱动 Now/Next 与周网格）。
 * stale=true 模拟后端缓存过期（GET 快照的 timetable_stale）；partialFailures 模拟刷新响应里
 * 的双源部分失败（仅在 POST 快照出现，驱动 timetable_partial）。
 * semesterId/currentWeek 供学期切换用例（UX-9）：学期 B（s2）的真实本周为第 3 周。 */
const timetableSnapshot = ({ todayFirst = false, week = 1, stale = false, partialFailures = [], semesterId = "s1", currentWeek = 1 } = {}) => {
  const weekOffset = (week - 1) * 7;
  const days = [1, 2, 3, 4, 5, 6, 7].map((weekday) => ({
    weekday,
    date: baseIsoShift(weekOffset + weekday - 1),
    meetings: [],
  }));
  if (todayFirst) {
    const today = days.find((day) => day.date === baseIso);
    const { start, end } = liveWindow();
    if (today) {
      today.meetings = [{
        meeting_id: "m-now", title: "数据结构", room: "H3101", teachers: [["陈老师"]],
        weekday: today.weekday, start_unit: 1, end_unit: 2, date: today.date,
        start_time: hm(start), end_time: hm(end),
        catalog_course_id: "c1", conflict_group: "", conflict_lane: 0, conflict_count: 1,
      }];
    }
  }
  return {
    selected_week: week, current_week: currentWeek,
    week_start: days[0].date, week_end: days[6].date,
    days,
    semesters: [
      { semester_id: "s1", label: "2026–2027 秋" },
      { semester_id: "s2", label: "2026–2027 春" },
    ],
    selected_semester: { semester_id: semesterId, start_date: "2026-09-07" },
    slots: [{ unit: 1, start: "08:00", end: "08:45" }],
    next_meeting: todayFirst && week === 1
      ? { meeting_id: "m-next", title: "线性代数", room: "HGX502", date: baseIsoShift(1), start_time: "09:00", end_time: "09:45", catalog_course_id: "" }
      : null,
    partial_failures: partialFailures,
    code: stale ? "timetable_stale" : (partialFailures.length ? "timetable_partial" : "timetable_verified"),
  };
};
let snapshotMode = { todayFirst: false };

let timetableGets = 0;
let timetableGetWeeks = [];
let refreshQueue = [];
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route.startsWith("/api/v3/timetable")) {
    timetableGets += 1;
    /* week 参数缺省 = 服务端默认视图（所选学期的真实本周：s1=第1周，s2=第3周） */
    const params = new URL(route, "http://timetable.local").searchParams;
    const requested = params.get("week");
    const semesterId = params.get("semester_id") || "s1";
    const currentWeek = semesterId === "s2" ? 3 : 1;
    const week = requested === null ? currentWeek : Number(requested);
    timetableGetWeeks.push(requested === null ? null : week);
    return ok(timetableSnapshot({ ...snapshotMode, week, semesterId, currentWeek }));
  }
  if (method === "POST" && route.includes("/timetable/actions")) {
    const next = refreshQueue.shift();
    if (!next) throw new Error("unexpected timetable action POST");
    return next.deferred ? next.deferred.promise : next.response;
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
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

const store = createStore();
store.auth = { state: "ready", code: "fudan_session_verified", connected: true, configured: true };
const { installTimetable } = await import("../frontend/modules/timetable.js");
const cleanup = await installTimetable(store);

const widget = byId["schedule-widget"];
const dialog = byId["schedule-week-dialog"];
const summary = byId["schedule-summary"];
/* SIMPLIFY-AUDIT-1 S2：常驻头部刷新钮已退役——refresh 触发源=状态行内联动作钮
   与 stale episode 自动链；下列各节改经这两条触发面驱动。 */
const nowBox = byId["schedule-now"];

/* 镜像 index.html 的真实父子结构（卡片 > 小组件按钮 + 状态区；按钮 > Now/摘要；
   浮层 > 状态区/周网格/头部按钮），让事件冒泡可复现“内嵌动作按钮误触打开器”类回归 */
const scheduleBox = new FakeElement("section", "schedule-box");
scheduleBox.append(widget, byId["schedule-widget-state"]);
widget.setAttribute("aria-expanded", "false"); /* index.html 静态属性 */
widget.setAttribute("aria-haspopup", "dialog");
widget.setAttribute("aria-controls", "schedule-week-dialog");
widget.append(byId["schedule-now"], byId["schedule-summary"]);
dialog.append(
  byId["schedule-state"], byId["week-grid"], byId["schedule-conflicts"],
  byId["schedule-week-prev"], byId["schedule-week-next"], byId["schedule-week-current"],
  byId["close-schedule-dialog"],
);

/* ---- 安装即铺底：小组件常驻可见，ready 状态安装后自动读取 ---- */
await settle();
assert.equal(timetableGets, 1, "ready 安装后自动读取课表（小组件常驻）");
assert.match(summary.textContent, /今天 0 节/, "铺底快照已渲染摘要");
assert.equal(nowBox.children[0]?.textContent, "今天没有更多安排", "空课表小组件显示无安排空态");
assert.equal(byId["week-grid"].children.filter((node) => String(node.className).startsWith("week-day-head")).length, 7, "周网格渲染周一至周日七列");

/* ---- UX-3a：Now/Next 小组件渲染（今天有一节进行中 + 明天一节） ---- */
snapshotMode = { todayFirst: true };
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 2, "回到本周触发重读");
assert.equal(nowBox.children[0]?.textContent, "正在上课", "进行中课程显示“正在上课”模式");
assert.equal(nowBox.children[1]?.textContent, "数据结构", "小组件显示课程名");
const remaining = nowBox.children.find((node) => node.className === "sw-remaining");
assert.match(remaining?.textContent || "", /还剩 .+ 分钟/, "进行中课程显示剩余时间");
assert.match(widget.getAttribute("aria-label") || "", /正在上课 数据结构/, "小组件聚合 aria-label");
const weekTag = byId["schedule-week-tag"].textContent;
assert.match(weekTag, /第 1 周 · \d+月\d+日–\d+月\d+日/, "周标签使用中文日期，不出现 ISO");

/* ---- UX-1a：成功路径——S2 后手动刷新入口=stale 横幅内联动作钮：浏览周
   stale 不自动刷新，经横幅 CTA 刷新成功渲染（常驻头部刷新钮已退役） ---- */
snapshotMode = { todayFirst: false, stale: true };
refreshQueue.push({ response: ok({ timetable: timetableSnapshot({ todayFirst: false, week: 2 }) }) });
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 3, "浏览周 stale GET 只读取不自动刷新");
assert.equal(refreshQueue.length, 1, "浏览周 stale 不触发自动 POST");
assert.match(byId["schedule-state"].textContent, /正在显示上次同步的课表/, "stale 横幅呈现");
const inlineRetry = byId["schedule-state"].children.find((node) => node.tagName === "BUTTON");
assert.ok(inlineRetry, "stale 横幅内联刷新钮在位（S2 后刷新入口）");
inlineRetry.dispatchEvent(new Event("click"));
await settle();
assert.equal(refreshQueue.length, 0, "CTA 发起刷新 POST 并消费");
assert.match(byId["schedule-week-tag"].textContent, /^第 2 周/, "刷新响应立即渲染浏览周");

/* ---- UX-1b：失败路径——状态区文案 + toast（摘要 finally 还原语义由 UX-8 钉）---- */
refreshQueue.push({ response: errorResponse("timetable_upstream_unavailable", "课表来源暂时不可用") });
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 4, "下一浏览周 stale 再次出横幅");
const inlineRetry2 = byId["schedule-state"].children.find((node) => node.tagName === "BUTTON");
assert.ok(inlineRetry2, "stale 横幅再次提供内联动作钮");
inlineRetry2.dispatchEvent(new Event("click"));
await settle();
assert.equal(refreshQueue.length, 0, "失败刷新 POST 已消费");
assert.match(byId["schedule-state"].textContent, /暂时不可用/, "失败写入浮层状态区闭集文案");
assert.match(byId["schedule-widget-state"].textContent, /暂时不可用/, "失败同步写入小组件状态区");
assert.ok(byId["toast-region"].children.length >= 1, "失败有 toast 提示");

/* ---- UX-4：小组件内联动作只执行刷新，不冒泡触发小组件打开器（S04-C）---- */
assert.equal(byId["schedule-widget-state"].parent, scheduleBox, "状态区是卡片的子节点（镜像 index.html）");
assert.notEqual(byId["schedule-widget-state"].parent, widget, "状态区不得嵌在小组件按钮内部（嵌套按钮=冒泡误触打开器）");
const inlineAction = byId["schedule-widget-state"].children.find((node) => node.tagName === "BUTTON");
assert.ok(inlineAction, "失败态在小组件状态区提供内联动作按钮");
assert.equal(dialog.open, false, "前置：浮层处于关闭");
refreshQueue.push({ response: ok({ timetable: timetableSnapshot({ todayFirst: false }) }) });
inlineAction.dispatchEvent(new Event("click"));
await settle();
assert.equal(refreshQueue.length, 0, "内联动作发起了刷新 POST");
assert.equal(dialog.open, false, "内联刷新只执行刷新，不打开周课表浮层");
assert.equal(widget.getAttribute("aria-expanded"), "false", "内联刷新不改变小组件展开态");
assert.match(summary.textContent, /今天 0 节/, "内联刷新成功后摘要照常渲染");

/* ---- UX-2：ready 转变自动加载（安装时已 ready，先过期再登录）---- */
store.set("auth", { state: "action_required", code: "fudan_session_expired", connected: false, configured: true });
await settle();
assert.equal(timetableGets, 4, "ready → 非 ready 不触发 load");
assert.match(byId["schedule-widget-state"].textContent, /登录复旦课程平台/, "未登录时小组件状态区可行动");
assert.equal(summary.textContent, "登录后显示今日与本周安排", "未登录时摘要显示登录提示");
assert.ok(byId["schedule-widget-state"].children.some((node) => node.textContent === "去登录"), "未登录时提供去登录动作");

store.set("auth", { state: "ready", code: "fudan_session_verified", connected: true, configured: true });
await settle();
assert.equal(timetableGets, 5, "非 ready → ready 自动读取课表（无需展开盒子）");

store.set("auth", { state: "ready", code: "fudan_session_verified", actions: ["logout"], connected: true, configured: true });
await settle();
assert.equal(timetableGets, 5, "同一 ready 快照的后续 auth 更新不重复触发");

/* ---- UX-3b：点击小组件用原生 dialog 打开周课表；关闭焦点返回小组件 ---- */
widget.dispatchEvent(new Event("click"));
await settle();
assert.equal(dialog.open, true, "小组件点击打开周课表浮层（showModal）");
assert.equal(widget.getAttribute("aria-expanded"), "true", "打开后 aria-expanded");
assert.equal(timetableGets, 6, "打开浮层触发课表读取");
assert.equal(document.activeElement, byId["close-schedule-dialog"], "打开后焦点进入浮层关闭按钮");

byId["close-schedule-dialog"].dispatchEvent(new Event("click"));
assert.equal(dialog.open, false, "关闭按钮关闭浮层");
assert.equal(widget.getAttribute("aria-expanded"), "false", "关闭后 aria-expanded 复位");
assert.equal(document.activeElement, widget, "关闭后焦点返回课表小组件");

/* ---- UX-5：遮罩点击关闭；内容点击/划选拖拽不误关；Esc 共用 close 焦点归还（S04-C）---- */
dialog.rect = { left: 100, top: 80, right: 900, bottom: 700, width: 800, height: 620 };
const dialogContent = new FakeElement("div", "week-grid-content");
dialog.append(dialogContent);
widget.dispatchEvent(new Event("click"));
await settle();
assert.equal(dialog.open, true, "重新打开浮层");
assert.equal(timetableGets, 7, "打开浮层仍照常加载课表");
dialogContent.dispatchEvent(new Event("click"));
assert.equal(dialog.open, true, "点击浮层内容不关闭");
/* 在内容上按下、在遮罩上释放（真实 DOM 中 click 冒泡落点为 dialog）：不得误关 */
dialogContent.dispatchEvent(pointerAt("mousedown", 400, 300));
dialog.dispatchEvent(pointerAt("click", 50, 300));
assert.equal(dialog.open, true, "起于内容的拖拽释放到遮罩不误关");
/* 起止都在遮罩（dialog 盒外 = ::backdrop）：关闭并归还焦点 */
dialog.dispatchEvent(pointerAt("mousedown", 50, 300));
dialog.dispatchEvent(pointerAt("click", 50, 300));
assert.equal(dialog.open, false, "点击遮罩关闭浮层");
assert.equal(widget.getAttribute("aria-expanded"), "false", "遮罩关闭后 aria-expanded 复位");
assert.equal(document.activeElement, widget, "遮罩关闭后焦点返回小组件");
/* Esc 等价路径：真实浏览器 Esc 走原生 cancel→close，这里直接派发 close 验证同一归还逻辑 */
dialog.open = true;
widget.setAttribute("aria-expanded", "true");
dialog.dispatchEvent(new Event("close"));
assert.equal(widget.getAttribute("aria-expanded"), "false", "close 事件复位 aria-expanded（Esc/关闭按钮/遮罩共用）");
assert.equal(document.activeElement, widget, "close 事件归还焦点到小组件");
dialog.open = false;

/* 周次切换仍走既有读取路径 */
dialog.open = true; /* 模拟浮层再次打开（不经 showModal，直接测周切换） */
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 8, "下一周触发既有读取"); /* UX-5 经小组件重开浮层多触发一次读取 */

/* ---- UX-6（本周真值分离）：浮层浏览非本周不得改写主页摘要/Now/Next/广播 ----
 * 基线切到“今天有课”的本周快照，使主页真值与浏览周内容可区分。 */
snapshotMode = { todayFirst: true };
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 9, "回到本周后重读");
assert.match(summary.textContent, /今天 1 节 · 下一节 09:00 线性代数 · 第 1 周/, "主页摘要表示真实本周");
const homeSummary = summary.textContent;
const widgetAriaBefore = widget.getAttribute("aria-label");
const broadcastWeeks = [];
const onBroadcast = (event) => broadcastWeeks.push(Number(event.detail?.selected_week));
window.addEventListener("courselens:timetable-snapshot", onBroadcast);

/* 浏览下一周：只有浮层（周标签/网格）跟随，主页摘要/小组件/广播不动 */
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 10, "浏览下一周触发读取");
assert.equal(timetableGetWeeks.at(-1), 2, "读取请求携带浏览周参数");
assert.match(byId["schedule-week-tag"].textContent, /^第 2 周 · /, "浮层周标签跟随浏览周（前缀等值钉，格式=第 N 周 · 日期区间）");
assert.equal(summary.textContent, homeSummary, "浏览非本周不改主页摘要");
assert.equal(widget.getAttribute("aria-label"), widgetAriaBefore, "浏览非本周不改小组件 Now/Next");
assert.deepEqual(broadcastWeeks, [], "浏览非本周不广播主页快照");

/* 关闭/重开浮层：主页仍表示本周；重开沿用浏览周也不得触碰主页 */
byId["close-schedule-dialog"].dispatchEvent(new Event("click"));
assert.equal(summary.textContent, homeSummary, "关闭浮层后主页摘要保持本周");
widget.dispatchEvent(new Event("click"));
await settle();
assert.equal(dialog.open, true, "重新打开浮层");
assert.equal(timetableGets, 11, "重开浮层沿用浏览周读取");
assert.equal(summary.textContent, homeSummary, "重开浮层后主页摘要仍是本周");
assert.match(byId["schedule-week-tag"].textContent, /^第 2 周 · /, "重开后浮层保持浏览周（前缀等值钉）");

/* 回到本周：主页与广播恢复一致 */
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGetWeeks.at(-1), 1, "回到本周读取真实周次");
assert.match(byId["schedule-week-tag"].textContent, /^第 1 周 · /, "浮层周标签回到本周（前缀等值钉）");
assert.equal(summary.textContent, homeSummary, "回到本周后主页摘要一致");
assert.deepEqual(broadcastWeeks, [1], "回到本周恰好广播一次本周快照");

/* 刷新响应固定是真实本周：浏览非本周 stale 出横幅 CTA，经 CTA 刷新——
   主页不变、浮层被带回本周（S2 后触发源=横幅内联动作钮） */
snapshotMode = { todayFirst: true, stale: true };
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
const week2Cta = byId["schedule-state"].children.find((node) => node.tagName === "BUTTON");
assert.ok(week2Cta, "浏览周 stale 横幅提供刷新 CTA");
refreshQueue.push({ response: ok({ timetable: timetableSnapshot({ todayFirst: true, week: 1 }) }) });
week2Cta.dispatchEvent(new Event("click"));
await settle();
assert.equal(summary.textContent, homeSummary, "刷新后主页摘要仍是本周");
assert.match(byId["schedule-week-tag"].textContent, /^第 1 周 · /, "刷新响应把浮层带回本周（前缀等值钉）");
assert.deepEqual(broadcastWeeks, [1, 1], "刷新广播本周快照");
snapshotMode = { todayFirst: true }; /* 复位：后续 auth 抖动 GET 回到 verified 基线 */

/* 登录状态切换：浏览状态被重置，重新登录后读取真实本周 */
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
store.set("auth", { state: "action_required", code: "fudan_session_expired", connected: false, configured: true });
await settle();
assert.equal(summary.textContent, "登录后显示今日与本周安排", "登出清空主页真值并显示登录提示");
store.set("auth", { state: "ready", code: "fudan_session_verified", connected: true, configured: true });
await settle();
assert.equal(timetableGetWeeks.at(-1), null, "重新登录后读取不带浏览周参数（真实本周）");
assert.match(summary.textContent, /今天 1 节 · 下一节 09:00 线性代数 · 第 1 周/, "重新登录后主页摘要恢复本周真值");

/* ---- UX-7（bugfix-timetable-autorefresh-1）：本周缓存过期自动刷新，恰一次/防循环 ----
 * stale 只出现在 GET 快照（后端缓存过期）：本周加载自动 POST 一次 refresh（复用在途
 * busy/摘要语义），在途状态为被动文案且无 CTA；浏览非本周不自动刷新、浏览周不被拉回；
 * 失败或仍 stale 回落手动横幅，同一 episode 绝不自动重试。 */
snapshotMode = { todayFirst: false, stale: true };
const autoRefreshGate = deferred();
refreshQueue.push({ deferred: autoRefreshGate });
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 16, "本周 stale GET 只触发一次读取");
assert.equal(refreshQueue.length, 0, "stale 自动触发恰一次 refresh POST");
assert.equal(summary.textContent, "正在刷新课表…", "自动刷新在途复用刷新摘要语义");
assert.equal(byId["schedule-state"].textContent, "课表缓存已过期，正在自动更新…", "在途状态为被动文案");
assert.equal(byId["schedule-widget-state"].textContent, "课表缓存已过期，正在自动更新…", "小组件状态区同步被动文案");
assert.equal(byId["schedule-state"].children.filter((node) => node.tagName === "BUTTON").length, 0, "自动刷新在途无 CTA");
assert.equal(byId["schedule-widget-state"].children.filter((node) => node.tagName === "BUTTON").length, 0, "小组件状态区在途无 CTA");

autoRefreshGate.resolve(ok({ timetable: timetableSnapshot({ todayFirst: false }) }));
await settle();
assert.equal(byId["schedule-state"].children.length, 0, "自动刷新成功后浮层状态区清空");
assert.equal(byId["schedule-widget-state"].children.length, 0, "自动刷新成功后小组件状态区清空");

/* 二次 stale load（同一 episode）：自动刷新结果仍 stale → 手动横幅回退，不再自动 POST */
const stillStaleGate = deferred();
refreshQueue.push({ deferred: stillStaleGate });
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 17, "verified 后的新 stale episode 再次自动刷新");
assert.equal(refreshQueue.length, 0, "新 episode 恰一次自动 POST");
stillStaleGate.resolve(ok({ timetable: timetableSnapshot({ todayFirst: false, stale: true }) }));
await settle();
assert.match(byId["schedule-state"].textContent, /正在显示上次同步的课表（缓存已过期）/, "结果仍 stale 回落既有手动横幅");
assert.ok(byId["schedule-state"].children.some((node) => node.textContent === "刷新"), "回退态保留显式刷新 CTA");

byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 18, "同一 episode 的二次 stale load 照常 GET");
assert.equal(refreshQueue.length, 0, "同一 episode 不再自动 POST（防循环）");
assert.match(byId["schedule-state"].textContent, /正在显示上次同步的课表（缓存已过期）/, "二次 stale load 渲染手动横幅而非错误态");

/* 浏览非本周：stale 只渲染手动横幅，不自动刷新，浏览周不被拉回 */
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 19, "浏览周 stale 照常 GET");
assert.equal(refreshQueue.length, 0, "浏览非本周不触发自动 POST");
assert.ok(byId["schedule-week-tag"].textContent.includes("第 2 周"), "浏览周不被自动刷新拉回本周");
assert.match(byId["schedule-state"].textContent, /正在显示上次同步的课表（缓存已过期）/, "浏览周 stale 保持手动横幅");

/* 刷新返回 partial：被动披露失败来源，去催促措辞与横幅内 CTA
   （S2 后触发源=stale episode 自动链） */
snapshotMode = { todayFirst: false };
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 20, "回到本周恢复 verified 基线（episode 复位）");
assert.equal(byId["schedule-state"].children.length, 0, "verified 清空状态区（partial 断言的干净基线）");
snapshotMode = { todayFirst: false, stale: true };
refreshQueue.push({ response: ok({ timetable: timetableSnapshot({ todayFirst: true, partialFailures: [{ source: "fudan_postgraduate" }] }) }) });
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 21, "stale 自动链发起 partial 刷新");
assert.equal(refreshQueue.length, 0, "partial 刷新 POST 已消费");
assert.match(byId["schedule-state"].textContent, /部分课表来源暂不可用（研究生课程来源），已显示已确认的课程。/, "partial 被动披露失败来源");
assert.equal(byId["schedule-state"].textContent.includes("可稍后刷新重试"), false, "partial 不再催促稍后刷新");
assert.equal(byId["schedule-state"].children.filter((node) => node.tagName === "BUTTON").length, 0, "partial 横幅无刷新 CTA");
assert.equal(byId["schedule-widget-state"].children.filter((node) => node.tagName === "BUTTON").length, 0, "小组件状态区同样无 CTA");
/* partial 渲染（非 stale）复位 episode：再次 stale 自动链仍可刷新恢复 verified */
snapshotMode = { todayFirst: false, stale: true };
refreshQueue.push({ response: ok({ timetable: timetableSnapshot({ todayFirst: false }) }) });
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 22, "恢复 verified 的新 episode 自动刷新已消费");
assert.equal(refreshQueue.length, 0, "自动链解除后仍可再次刷新");
assert.equal(byId["schedule-state"].children.length, 0, "再次刷新成功恢复 verified 清空态");

/* 自动 refresh 抛错：诚实错误回退态含手动 CTA，且同一 episode 不再自动重试 */
snapshotMode = { todayFirst: false, stale: true };
const toastsBefore = byId["toast-region"].children.length;
refreshQueue.push({ response: errorResponse("timetable_upstream_unavailable", "课表来源暂时不可用") });
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 23, "新 stale episode 照常 GET");
assert.equal(refreshQueue.length, 0, "失败 episode 的自动 POST 已消费");
assert.ok(byId["toast-region"].children.length > toastsBefore, "自动刷新失败有 toast 提示");
assert.match(byId["schedule-state"].textContent, /暂时不可用/, "自动刷新失败走既有错误态闭集文案");
assert.ok(byId["schedule-state"].children.some((node) => node.textContent === "重试"), "错误回退态保留手动重试 CTA");

byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGets, 24, "失败后二次 stale load 照常 GET");
assert.equal(refreshQueue.length, 0, "失败后同一 episode 不再自动重试");
assert.match(byId["schedule-state"].textContent, /正在显示上次同步的课表（缓存已过期）/, "失败后回落手动 stale 横幅");
assert.ok(byId["schedule-state"].children.some((node) => node.textContent === "刷新"), "手动 stale 横幅保留刷新 CTA");

/* fixture 确定性：进行中课程窗口钳制在当日内且 start<end，任何基准时刻都不跨午夜 */
const fixtureNow = timetableSnapshot({ todayFirst: true, week: 1 });
const fixtureToday = fixtureNow.days.find((day) => day.date === baseIso);
assert.ok(fixtureToday, "week=1 快照包含基准今天");
const fixtureMeeting = fixtureToday.meetings[0];
assert.ok(fixtureMeeting, "todayFirst 快照在基准今天挂一节进行中课程");
assert.ok(toMinutes(fixtureMeeting.end_time) > toMinutes(fixtureMeeting.start_time), "进行中课程窗口不跨午夜（历史 23:40–00:39 假红根因）");
const fixtureNextWeek = timetableSnapshot({ todayFirst: true, week: 2 });
assert.equal(fixtureNextWeek.days.some((day) => day.date === baseIso), false, "week=2 浏览周不包含今天");
assert.equal(fixtureNextWeek.next_meeting, null, "浏览周不携带本周 next_meeting");

/* ---- UX-8（W5）：refresh 迟到响应不得覆盖已漂移的浏览周 ----
 * 浏览第 5 周 → 点刷新（代际捕获 week=5）→ 立即翻到第 6 周 → refresh 后到：
 * 浏览面保持第 6 周、主页真值与广播不动；busy 仍在 finally 收口。 */
snapshotMode = { todayFirst: true };
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGetWeeks.at(-1), 1, "前置：回到本周恢复 verified 基线");
const ux8HomeSummary = summary.textContent;
const ux8Broadcasts = [];
const onUx8Broadcast = (event) => ux8Broadcasts.push(Number(event.detail?.selected_week));
window.addEventListener("courselens:timetable-snapshot", onUx8Broadcast);
snapshotMode = { todayFirst: true, stale: true }; /* 浏览周 stale：横幅 CTA 承载手动刷新 */
for (let step = 0; step < 4; step += 1) {
  byId["schedule-week-next"].dispatchEvent(new Event("click"));
  await settle();
}
assert.ok(byId["schedule-week-tag"].textContent.includes("第 5 周"), "前置：第 5 周 stale 横幅已渲染");
const lateRefresh = deferred();
refreshQueue.push({ deferred: lateRefresh });
const week5Cta = byId["schedule-state"].children.find((node) => node.tagName === "BUTTON");
assert.ok(week5Cta, "前置：浏览周 stale 横幅提供刷新 CTA");
week5Cta.dispatchEvent(new Event("click"));
await settle();
assert.equal(summary.textContent, "正在刷新课表…", "前置：refresh 在途摘要文案");
byId["schedule-week-next"].dispatchEvent(new Event("click"));
await settle();
assert.ok(byId["schedule-week-tag"].textContent.includes("第 6 周"), "前置：第 6 周已渲染");
lateRefresh.resolve(ok({ timetable: timetableSnapshot({ todayFirst: true }) }));
await settle();
assert.ok(byId["schedule-week-tag"].textContent.includes("第 6 周"), "迟到的 refresh 快照不覆盖已漂移的浏览周（W5）");
assert.equal(summary.textContent, ux8HomeSummary, "漂移的 refresh 响应不改主页真值且摘要由 finally 还原");
assert.deepEqual(ux8Broadcasts, [], "漂移的 refresh 响应不广播主页快照");
window.removeEventListener("courselens:timetable-snapshot", onUx8Broadcast);

/* ---- UX-9（W6）：学期切换后「回到本周」收敛到新学期真实周次 ----
 * 学期 A 浏览第 5 周 → 切学期 B（桩 current_week=3）：切换请求不带周次走服务端默认；
 * 收敛后点「回到本周」必须请求 week=3，而不是拿旧学期周次请求新学期。 */
byId["schedule-week-prev"].dispatchEvent(new Event("click"));
await settle();
assert.ok(byId["schedule-week-tag"].textContent.includes("第 5 周"), "前置：学期 A 浏览第 5 周");
byId["timetable-semester"].value = "s2";
byId["timetable-semester"].dispatchEvent(new Event("change"));
await settle();
assert.equal(timetableGetWeeks.at(-1), null, "学期切换请求不带周次（服务端默认 = 新学期真实本周）");
assert.ok(byId["schedule-week-tag"].textContent.includes("第 3 周"), "切换后浮层收敛到学期 B 第 3 周");
assert.match(summary.textContent, /第 3 周/, "主页摘要表示学期 B 的真实本周");
byId["schedule-week-current"].dispatchEvent(new Event("click"));
await settle();
assert.equal(timetableGetWeeks.at(-1), 3, "回到本周请求新学期真实周次 week=3（W6）");
assert.ok(byId["schedule-week-tag"].textContent.includes("第 3 周"), "回到本周后浮层保持学期 B 第 3 周");

/* ---- UX-10（W7）：60s tick 整树重建周网格不销毁网格内焦点 ----
 * 焦点在课程块按钮上：tick 重建后按 data-wb-key 原位恢复；
 * 焦点在网格外（小组件）：tick 不劫持焦点。 */
store.courses = [{ course_id: "c1", title: "数据结构" }];
byId["timetable-semester"].value = "s1";
byId["timetable-semester"].dispatchEvent(new Event("change"));
await settle();
assert.equal(timetableGetWeeks.at(-1), null, "前置：切回学期 A 走服务端默认");
assert.ok(timetableTick, "前置：60s tick 已注册");
const blockBefore = byId["week-grid"].querySelectorAll("button")[0];
assert.ok(blockBefore, "前置：周网格渲染出可聚焦的课程块按钮");
blockBefore.focus();
assert.equal(document.activeElement, blockBefore, "前置：焦点在周网格课程块上");
timetableTick();
await settle();
const blockAfter = document.activeElement;
assert.notEqual(blockAfter, blockBefore, "tick 确实整树重建了网格");
assert.equal(
  blockAfter?.getAttribute("data-wb-key"),
  blockBefore.getAttribute("data-wb-key"),
  "焦点按 data 身份在重建后的网格中原位恢复（W7）",
);
widget.focus();
timetableTick();
await settle();
assert.equal(document.activeElement, widget, "焦点在网格外时 tick 不劫持焦点（W7）");

window.removeEventListener("courselens:timetable-snapshot", onBroadcast);
cleanup();
await settle();


console.log("frontend_timetable_ux_behavior: all assertions passed");
