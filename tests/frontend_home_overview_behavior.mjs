import assert from "node:assert/strict";

/* 主页概览行为测试（home-ux-03）：Now/Next 选择、中文时间、直播目标优先级、
 * 状态胶囊摘要、主页直播卡安全流（status/grants/sessions + 事件）。
 * 纯函数用固定 now 注入；DOM 部分仿 usability 桩件法。 */

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
    this.type = "";
    this._listeners = new Map();
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  focus() { globalThis.document.activeElement = this; }
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
    for (const listener of [...(this._listeners.get(event.type) || [])]) listener.call(this, event);
    return true;
  }
}

const now = new Date(2026, 8, 9, 10, 15, 0); /* 2026-09-09 周三 10:15 本地时间 */

const day = (weekday, meetings) => ({ weekday, date: `2026-09-${String(6 + weekday).padStart(2, "0")}`, meetings });
const meeting = (over = {}) => ({
  meeting_id: "m1", title: "数据结构", room: "H3101", teachers: [["陈老师"]],
  catalog_course_id: "", date: "2026-09-09", start_time: "09:55", end_time: "10:40", ...over,
});
const daysWith = (todayMeetings) => [1, 2, 3, 4, 5, 6, 7].map((weekday) => day(weekday, weekday === 3 ? todayMeetings : []));

const overview = await import("../frontend/modules/home-overview.js");

/* ---- selectNowNext：正在上课 ---- */
const nowSnapshot = {
  days: daysWith([meeting()]),
  current_meeting: null,
  next_meeting: meeting({ meeting_id: "m2", start_time: "14:25", end_time: "15:10" }),
};
const nowModel = overview.selectNowNext(nowSnapshot, now);
assert.equal(nowModel.mode, "now", "进行中时间段识别为 now");
assert.equal(nowModel.title, "数据结构", "now 显示课程名");
assert.equal(nowModel.timeText, "09:55–10:40", "now 显示起止时间");
assert.match(nowModel.remainingText, /还剩 25 分钟/, "now 显示剩余时间");
assert.ok(Math.abs(nowModel.progress - 20 / 45) < 1e-6, "now 计算进度比例");

/* ---- selectNowNext：今天稍后 ---- */
const nextModel = overview.selectNowNext({
  days: daysWith([meeting({ start_time: "14:25", end_time: "15:10" })]),
  next_meeting: null,
}, now);
assert.equal(nextModel.mode, "next", "今天稍后识别为 next");
assert.equal(nextModel.dayLabel, "今天", "next 目标日是今天");
assert.match(nextModel.remainingText, /后开始/, "next 显示倒计时");

/* ---- selectNowNext：明天 / 本周稍后 / 下周 ---- */
const laterModel = overview.selectNowNext({
  days: daysWith([]),
  next_meeting: meeting({ date: "2026-09-10", start_time: "08:00", end_time: "08:45", meeting_id: "m3" }),
}, now);
assert.equal(laterModel.mode, "later", "今天无课时落到下一节");
assert.equal(laterModel.dayLabel, "明天", "次日显示“明天”");
assert.equal(laterModel.timeText, "明天 08:00–08:45", "later 显示中文日期+时间");

const fridayModel = overview.selectNowNext({
  days: daysWith([]),
  next_meeting: meeting({ date: "2026-09-11", start_time: "08:00", end_time: "08:45" }),
}, now);
assert.equal(fridayModel.dayLabel, "周五", "本周稍后显示星期名");

const mondayModel = overview.selectNowNext({
  days: daysWith([]),
  next_meeting: meeting({ date: "2026-09-14", start_time: "08:00", end_time: "08:45" }),
}, now);
assert.equal(mondayModel.dayLabel, "下周一", "跨周显示“下周一”");

const emptyModel = overview.selectNowNext({ days: daysWith([]), next_meeting: null }, now);
assert.equal(emptyModel.mode, "empty", "无后续课程为空态");

/* ---- 中文时间纯函数 ---- */
assert.equal(overview.friendlyDayLabel("2026-09-09", now), "今天", "friendlyDayLabel 今天");
assert.equal(overview.friendlyDayLabel("2026-09-10", now), "明天", "friendlyDayLabel 明天");
assert.equal(overview.friendlyDayLabel("2026-09-11", now), "周五", "friendlyDayLabel 周五");
assert.equal(overview.friendlyDayLabel("2026-09-14", now), "下周一", "friendlyDayLabel 下周一");
assert.equal(overview.friendlyDateRange("2026-09-07", "2026-09-13", now), "9月7日–9月13日", "周区间中文日期");
const joined = JSON.stringify([nowModel.timeText, laterModel.timeText, overview.friendlyDateRange("2026-09-07", "2026-09-13", now)]);
assert.doesNotMatch(joined, /\d{4}-\d{2}-\d{2}/, "用户可见时间不输出 ISO 日期");

/* ---- homeLiveTarget：当前 meeting 课程 > 下一 meeting 课程 > activeCourse ---- */
const courses = [
  { course_id: "c-current", title: "当前课" },
  { course_id: "c-next", title: "下一课" },
];
const targetCurrent = overview.homeLiveTarget({
  days: daysWith([meeting({ catalog_course_id: "c-current" })]),
  next_meeting: meeting({ catalog_course_id: "c-next" }),
}, { courses, activeCourse: courses[1] }, now);
assert.equal(targetCurrent.course.course_id, "c-current", "优先当前 meeting 的目录课程");
assert.equal(targetCurrent.reason, "current", "目标来源标记 current");

const targetNext = overview.homeLiveTarget({
  days: daysWith([]),
  next_meeting: meeting({ catalog_course_id: "c-next" }),
}, { courses, activeCourse: courses[1] }, now);
assert.equal(targetNext.course.course_id, "c-next", "无当前 meeting 时用下一 meeting 的目录课程");

const targetActive = overview.homeLiveTarget({
  days: daysWith([meeting({ catalog_course_id: "" })]),
  next_meeting: meeting({ catalog_course_id: "" }),
}, { courses, activeCourse: courses[1] }, now);
assert.equal(targetActive.course.course_id, "c-next", "当前/下一节都未关联目录时回退 activeCourse");
assert.equal(targetActive.reason, "active", "来源标记 active");

const targetFallback = overview.homeLiveTarget(null, { courses, activeCourse: courses[1] }, now);
assert.equal(targetFallback.course.course_id, "c-next", "快照缺失时回退 activeCourse");
assert.equal(targetFallback.reason, "active", "快照缺失来源标记 active");

/* ---- 直播卡时间文案：课表时间不冒充已确认直播时间（SWEEPFIX-2：状态后缀
   退役——「待确认」属能力行/原因行，时间行只述课表事实，不再复读） ---- */
assert.equal(
  overview.homeLiveScheduleText(meeting({ date: "2026-09-10", start_time: "08:00" }), "later", now),
  "按课表 明天 08:00 开始",
  "未开始：按课表 HH:mm 开始（修前红=尾缀「，直播状态待确认」复读）",
);
assert.equal(
  overview.homeLiveScheduleText(meeting({ start_time: "09:55", end_time: "10:40" }), "now", now),
  "按课表 09:55–10:40 进行中",
  "进行中：不把课表时间说成已确认直播时间",
);

/* ---- 状态胶囊摘要（连接/任务两段；保护告警链已随预算门退役） ---- */
assert.equal(
  overview.statusCapsuleLabel({ fudan: "ready", github: "checking", running: 2, failed: 1 }),
  "状态：复旦已连接；远程确认中；任务 2 个进行中；失败 1",
  "胶囊聚合连接/任务摘要",
);
assert.equal(
  overview.statusCapsuleLabel({ fudan: "action", github: "error", running: 0, failed: 0 }),
  "状态：复旦需要处理；远程连接异常；无进行中任务",
  "胶囊摘要降态覆盖",
);

/* ---- DOM：胶囊读取与写回 ---- */
const capsule = new FakeElement("div", "status-capsule");
const fudanDot = new FakeElement("span", "dot-fudan");
fudanDot.dataset.connDot = "fudan";
fudanDot.setAttribute("data-state", "ready");
const githubDot = new FakeElement("span", "dot-github");
githubDot.dataset.connDot = "github";
githubDot.setAttribute("data-state", "action");
capsule.append(fudanDot, githubDot);
const chipCount = new FakeElement("span", "task-chip-count");
chipCount.textContent = "2";
const chipFailed = new FakeElement("span", "task-chip-failed");
chipFailed.hidden = true;
const miniDoc = {
  getElementById: (id) => ({ "status-capsule": capsule, "task-chip-count": chipCount, "task-chip-failed": chipFailed }[id] || null),
};
assert.deepEqual(overview.readCapsuleParts(miniDoc), {
  fudan: "ready", github: "action", running: 2, failed: 0,
}, "从 DOM 读取胶囊各段状态");
overview.refreshStatusCapsuleLabel(miniDoc);
assert.match(capsule.getAttribute("aria-label"), /^状态：复旦已连接；远程需要处理；任务 2 个进行中$/, "聚合 aria-label 写回胶囊");

/* ---- DOM：主页直播卡安全流 ---- */
const byId = Object.fromEntries([
  "home-live-card", "home-live-label", "home-live-capability", "home-live-time", "home-live-reason", "home-live-target", "home-live-action", "home-live-recheck", "home-live-disclosure", "toast-region",
].map((id) => [id, new FakeElement("div", id)]));
byId["home-live-action"] = new FakeElement("button", "home-live-action");
byId["home-live-recheck"] = new FakeElement("button", "home-live-recheck");
byId["toast-region"] = new FakeElement("div", "toast-region");
globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${tag}`),
  createDocumentFragment: () => new FakeElement("#document-fragment", "created-fragment"),
  activeElement: null,
  documentElement: { dataset: {} },
  querySelector: () => null,
};
const windowTarget = new EventTarget();
windowTarget.setTimeout = () => 0;
windowTarget.clearTimeout = () => {};
/* LIVEEXP-1：捕获式 interval 桩——重估 tick 手动驱动（注入时钟推进后调 tick） */
let installedTick = null;
let installedRecheckMs = 0;
const clearedIntervals = [];
windowTarget.setInterval = (fn, ms) => { installedTick = fn; installedRecheckMs = Number(ms); return 71; };
windowTarget.clearInterval = (id) => { clearedIntervals.push(id); };
globalThis.window = windowTarget;
if (!globalThis.CustomEvent) {
  globalThis.CustomEvent = class extends Event {
    constructor(type, options = {}) { super(type); this.detail = options.detail; }
  };
}
const settle = async () => { for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

const ok = (data) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
  status: 200, headers: { "Content-Type": "application/json" },
});
const errorResponse = (code, message) => new Response(JSON.stringify({ error: message, error_code: code }), {
  status: 403, headers: { "Content-Type": "application/json" },
});

const store = {
  courses,
  activeCourse: courses[1],
  listeners: new Map(),
  set(key, value) {
    this[key] = value;
    for (const listener of this.listeners.get(key) || []) listener(value);
  },
  subscribe(key, listener) {
    if (!this.listeners.has(key)) this.listeners.set(key, new Set());
    this.listeners.get(key).add(listener);
    return () => this.listeners.get(key)?.delete(listener);
  },
};

let statusGets = 0;
let statusState = "live";
let posts = [];
let livePlayed = null;
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route.includes("/live-room/status")) {
    statusGets += 1;
    return ok({ state: statusState, can_enter: statusState === "live" });
  }
  if (method === "POST" && route.includes("/live-room/grants")) {
    posts.push("grants");
    return ok({ grant: "g1" });
  }
  if (method === "POST" && route.includes("/live-room/sessions")) {
    posts.push("sessions");
    return ok({ manifest_path: "live/index.m3u8" });
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};
windowTarget.addEventListener("courselens:live-play", (event) => { livePlayed = event.detail; });
/* N6L S1 U2：主页卡进入动作=跳转直播页（liveTarget 预选+select-page 事件） */
let selectPageDetail = null;
windowTarget.addEventListener("courselens:select-page", (event) => { selectPageDetail = event.detail; });
/* F2（化身走查 20261008）：CTA 携带进入意图——直播页据以自动建会 */
let liveEnterEvents = 0;
windowTarget.addEventListener("courselens:live-enter", () => { liveEnterEvents += 1; });

const { installHomeOverview } = overview;
const cleanupOverview = await installHomeOverview(store);
await settle();

/* 无快照：activeCourse 兜底成为目标并确认状态 */
assert.equal(statusGets, 1, "安装后按 activeCourse 兜底确认直播状态");
assert.equal(byId["home-live-label"].textContent, "正在直播", "live 状态显示正在直播");
assert.equal(byId["home-live-capability"].textContent, "直播入口可用", "能力行显示直播入口可用");
assert.equal(byId["home-live-target"].textContent, "下一课", "卡片标注目标课程");
assert.equal(byId["home-live-action"].textContent, "进入直播", "live 提供进入直播");
assert.equal(byId["home-live-action"].disabled, false, "can_enter 时进入可用");
/* LIVE-DISCLOSURE-1：卡内早期功能小字——静态装一次，闭集同源、不随状态翻转 */
const liveStateShared = await import("../frontend/modules/live-state.js");
assert.equal(
  byId["home-live-disclosure"].textContent,
  liveStateShared.LIVE_DISCLOSURE_TEXT,
  "主页直播卡早期功能小字装一次即定",
);

/* NIGHT4 去重钉：unknown 态标题与能力行同文 → 能力行退场；回 live 态恢复 */
const statusGetsBeforeDedupe = statusGets;
statusState = "unknown";
store.set("activeCourse", store.activeCourse);
await settle();
assert.equal(byId["home-live-label"].textContent, "直播状态待确认", "unknown 态标题=待确认");
assert.equal(byId["home-live-capability"].textContent, "直播状态待确认", "能力行文本保持闭集");
assert.equal(byId["home-live-capability"].hidden, true, "与标题同文 → 能力行退场");
statusState = "live";
store.set("activeCourse", store.activeCourse);
await settle();
assert.equal(byId["home-live-capability"].hidden, false, "回 live 态能力行恢复显示");
const statusGetsAfterDedupe = statusGets;
assert.equal(byId["home-live-card"].dataset.state, "live", "卡片 data-state=live");

/* 进入直播：跳转直播页（liveTarget 预选+select-page），零会话请求；
   会话链归 live-page.js（N6L S1 U2 还债：主页第二份进入链退役） */
byId["home-live-action"].dispatchEvent(new Event("click"));
await settle();
assert.deepEqual(posts, [], "主页卡跳转语义零 grants/sessions 请求");
assert.equal(selectPageDetail, "live", "跳转直播页");
assert.equal(store.liveTarget, String(courses[1].course_id), "liveTarget 预选当前目标");
assert.equal(livePlayed, null, "主页不再派发 live-play");
assert.equal(liveEnterEvents, 1, "F2：CTA 恰派发一次 live-enter 进入意图");

/* upcoming：普通课表态不展示大 CTA，也不提供常驻重试；能力行如实显示未开始 */
statusState = "upcoming";
windowTarget.dispatchEvent(new Event("courselens:live-refresh"));
await settle();
assert.equal(statusGets, statusGetsAfterDedupe + 1, "live-refresh 触发重新确认");
assert.equal(byId["home-live-action"].hidden, true, "非 live 隐藏主 CTA（无重新确认大按钮）");
assert.equal(byId["home-live-recheck"].hidden, true, "非可行动错误不显示紧凑重试");
assert.equal(byId["home-live-capability"].textContent, "直播未开始", "能力行如实显示直播未开始");

/* 课表快照事件：目标切换到当前 meeting 的目录课程（用真实今天构造进行中的 meeting） */
store.courses = [...courses, { course_id: "c-snapshot", title: "快照课" }];
const pad = (n) => String(n).padStart(2, "0");
const nowMinReal = new Date().getHours() * 60 + new Date().getMinutes();
const hmShiftReal = (minutes) => {
  const total = ((minutes % 1440) + 1440) % 1440;
  return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
};
const todayIsoReal = () => {
  const base = new Date();
  return new Date(base.getTime() - base.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
const isoShiftReal = (days) => {
  const base = new Date();
  base.setDate(base.getDate() + days);
  return new Date(base.getTime() - base.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
const todayWeekday = new Date().getDay() === 0 ? 7 : new Date().getDay();
const snapshot = {
  days: [1, 2, 3, 4, 5, 6, 7].map((weekday) => {
    /* 进行中会议必须落在同一天内：end 在午夜前收敛，避免 23:40 后窗口跨日
       导致 selectNowNext 判不出 now 形态（时间抖动缺陷修复）。 */
    let startMin = Math.max(0, nowMinReal - 40);
    let endMin = nowMinReal + 20;
    if (endMin > 1439) {
      endMin = 1439;
      startMin = Math.max(0, Math.min(startMin, endMin - 40));
    }
    return {
      weekday,
      date: isoShiftReal(weekday - todayWeekday),
      meetings: weekday === todayWeekday
        ? [meeting({
          catalog_course_id: "c-snapshot",
          date: todayIsoReal(),
          start_time: hmShiftReal(startMin),
          end_time: hmShiftReal(endMin),
        })]
        : [],
    };
  }),
  next_meeting: null,
};
windowTarget.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: snapshot }));
await settle();
assert.equal(byId["home-live-target"].textContent, "快照课", "课表快照到达后目标切到当前 meeting 的目录课程");

/* 拒绝态：denied 无进入按钮；课表事实（进行中）与能力（无权限）分开陈述 */
statusState = "denied";
windowTarget.dispatchEvent(new Event("courselens:live-refresh"));
await settle();
assert.equal(byId["home-live-label"].textContent, "按课表进行中", "课表进行中是事实标题，不被探测结果覆盖");
assert.equal(byId["home-live-capability"].textContent, "无直播权限", "能力行显示无直播权限");
assert.equal(byId["home-live-action"].hidden, true, "denied 无动作按钮");

/* 探测在途：绝不把 scheduled 课程写成“直播状态未知”，也不显示大 CTA */
statusState = "unknown";
windowTarget.dispatchEvent(new Event("courselens:live-refresh"));
await settle();
assert.equal(byId["home-live-label"].textContent, "按课表进行中", "pending 探测不覆盖课表事实");
assert.equal(byId["home-live-capability"].textContent, "直播状态待确认", "pending 显示待确认能力");
assert.equal(byId["home-live-action"].hidden, true, "pending 无 CTA");

/* ---- LIVEEXP-1 纯函数钉：过窗措辞闭集（今天窗口已过=已结束，绝不写「开始」） ---- */
const pastMeeting = meeting({ start_time: "09:55", end_time: "10:40" });
const pastNow = new Date(2026, 8, 9, 10, 41, 0);
assert.equal(
  overview.homeLiveScheduleText(pastMeeting, "later", pastNow),
  "按课表 09:55–10:40 已结束",
  "过窗时间线：如实「已结束」",
);
assert.equal(
  overview.homeLiveHeadline(pastMeeting, "later", pastNow),
  "按课表 09:55–10:40 已结束",
  "过窗标题：如实「已结束」",
);
assert.equal(
  overview.homeLiveScheduleText(meeting({ start_time: "14:25", end_time: "15:10" }), "next", pastNow),
  "按课表 今天 14:25 开始",
  "未过窗的未来课仍写「开始」",
);

/* ---- LIVEEXP-1 行为流：时间驱动重估（过窗自愈/节流/红点收敛/dispose）---- */
cleanupOverview();
await settle();
statusState = "live";
/* 注入时钟 + 捕获重估 tick：页面静止时由注入时钟推进 + 手动 tick 驱动 */
let clockNow = new Date(2026, 8, 9, 10, 15, 0); /* 周三 2026-09-09 10:15：09:55–10:40 课中 */
let liveTick = null;
let liveRecheckMs = 0;
const liveCleared = [];
windowTarget.setInterval = (fn, ms) => { liveTick = fn; liveRecheckMs = Number(ms); return 72; };
windowTarget.clearInterval = (id) => { liveCleared.push(id); };
const liveSnapshot = {
  days: daysWith([
    meeting({ catalog_course_id: "c-snapshot", title: "快照课" }),
    meeting({ meeting_id: "m2", title: "下一课", catalog_course_id: "c-next", start_time: "14:25", end_time: "15:10" }),
  ]),
  current_meeting: null,
  next_meeting: null,
};
const cleanupInjected = await installHomeOverview(store, { clock: () => clockNow });
await settle();
assert.equal(liveRecheckMs, 30000, "重估周期默认 30s（≤60s 过窗判据）");
assert.equal(typeof liveTick, "function", "重估 tick 已注册");

/* 基线（课中 10:15 + live 观测）：进行中 + 入口可用 + 红点在册 */
windowTarget.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: liveSnapshot }));
await settle();
assert.equal(byId["home-live-label"].textContent, "按课表进行中", "课中基线：按课表进行中");
assert.equal(byId["home-live-capability"].textContent, "直播入口可用", "课中基线：能力行入口可用");
assert.equal(byId["home-live-action"].hidden, false, "课中基线：进入按钮在位");
assert.ok(store.liveActiveCourses.includes("c-snapshot"), "课中 live 观测回写红点数据源");
const statusGetsAtBaseline = statusGets;

/* 节流钉：相位不变且远离边界 → tick 零请求零翻转（请求风暴禁令） */
clockNow = new Date(2026, 8, 9, 10, 20, 0);
liveTick();
await settle();
assert.equal(statusGets, statusGetsAtBaseline, "相位不变 tick 零请求");
assert.equal(byId["home-live-label"].textContent, "按课表进行中", "相位不变 tick 不翻转");

/* 过窗翻转钉：注入时钟跨 end_time → tick → 全量重查收敛（观测 ended） */
statusState = "ended";
clockNow = new Date(2026, 8, 9, 10, 41, 0); /* 过窗 1 分钟 */
liveTick();
await settle();
assert.equal(statusGets, statusGetsAtBaseline + 1, "过窗相位翻转触发全量重查");
assert.equal(byId["home-live-label"].textContent, "按课表 今天 14:25 开始", "过窗后翻转到下一节课表事实，不残留进行中");
assert.equal(byId["home-live-capability"].textContent, "直播未开始", "SWEEP1-01：ended 观测×未开始 meeting → 单一一致态（修前红=此处钉过矛盾态「直播已结束」）");
assert.equal(byId["home-live-reason"].textContent, "开始时间尚未到，开始前会保持安静。", "SWEEP1-01：原因行与未开始一致，不残留回放话术");
assert.equal(byId["home-live-action"].hidden, true, "过窗后无进入按钮");
assert.equal(byId["home-live-time"].textContent, "", "SWEEP1-01：时间行与标题同文时退场（修前红=「按课表 今天 14:25 开始，直播状态待确认」复读）");
assert.ok(!store.liveActiveCourses.includes("c-snapshot"), "相位退出进行中：旧目标红点同步收敛");

/* 边界临近钉：下一节开始前 3 分钟 → tick 全量重查 */
const statusGetsAfterFlip = statusGets;
clockNow = new Date(2026, 8, 9, 14, 22, 0);
liveTick();
await settle();
assert.equal(statusGets, statusGetsAfterFlip + 1, "临近窗口边界 tick 触发全量重查");

/* 竞态分层钉（判据 2）：观测在途跨窗——课表事实改述「已结束」，
   后端真态 live 并存（能力行/按钮不隐瞒），两事实分层不混淆 */
let releaseStatus = null;
let holdStatus = null;
globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  const method = String(options.method || "GET");
  if (method === "GET" && route.includes("/live-room/status")) {
    statusGets += 1;
    if (holdStatus) await holdStatus;
    return ok({ state: statusState, can_enter: statusState === "live" });
  }
  throw new Error(`unexpected synthetic route: ${method} ${route}`);
};
statusState = "live";
clockNow = new Date(2026, 8, 9, 10, 15, 0);
windowTarget.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: liveSnapshot }));
await settle();
assert.equal(byId["home-live-label"].textContent, "按课表进行中", "竞态前置：回到课中基线");
holdStatus = new Promise((resolve) => { releaseStatus = resolve; });
const statusGetsAtHold = statusGets;
windowTarget.dispatchEvent(new Event("courselens:live-refresh"));
await settle();
assert.equal(statusGets, statusGetsAtHold + 1, "在途观测已发出");
clockNow = new Date(2026, 8, 9, 10, 41, 0); /* await 期间跨窗 */
releaseStatus();
holdStatus = null;
await settle();
assert.equal(byId["home-live-label"].textContent, "按课表 09:55–10:40 已结束", "竞态：课表事实改述已结束");
assert.equal(byId["home-live-time"].textContent, "", "竞态时间线与标题同文时退场（信息不重复）");
assert.equal(byId["home-live-capability"].textContent, "直播入口可用", "后端真态 live 如实陈述能力（两事实分层）");
assert.equal(byId["home-live-action"].hidden, false, "后端真态 live 可进入");

/* ---- SWEEPFIX-2（SWEEP1-01）复现钉：SWEEP-1 现场重放——明天 10:50 目标课
   × 上一场 ended 观测。修前红=四行同屏互相矛盾：标题「按课表 明天 10:50 开始」
   × 能力行「直播已结束」× 时间行「按课表 明天 10:50 开始，直播状态待确认」
   × 原因行「可以在讲次列表中查看已授权回放。」 ---- */
statusState = "ended";
clockNow = new Date(2026, 8, 9, 21, 30, 0); /* 今天课毕，唯一事实=明天的 meeting */
const tomorrowSnapshot = {
  days: [],
  next_meeting: {
    meeting_id: "m-tomorrow", title: "明天的课", catalog_course_id: "c-snapshot",
    date: "2026-09-10", start_time: "10:50", end_time: "11:40",
  },
  current_meeting: null,
};
windowTarget.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: tomorrowSnapshot }));
await settle();
assert.equal(byId["home-live-target"].textContent, "快照课", "复现前置：目标切到明天 meeting 的目录课程");
assert.equal(byId["home-live-label"].textContent, "按课表 明天 10:50 开始", "标题=课表事实");
assert.equal(byId["home-live-capability"].textContent, "直播未开始", "能力行单一一致态（修前红=「直播已结束」）");
assert.equal(byId["home-live-reason"].textContent, "开始时间尚未到，开始前会保持安静。", "原因行不残留回放话术（修前红）");
assert.equal(byId["home-live-time"].textContent, "", "时间行复读退场（修前红=标题+「，直播状态待确认」复读）");

/* dispose 钉：清定时器 + disposed 后 tick 零动作 */
const statusGetsBeforeDispose = statusGets;
cleanupInjected();
await settle();
assert.ok(liveCleared.includes(72), "dispose 清除重估定时器");
clockNow = new Date(2026, 8, 9, 10, 42, 0);
liveTick();
await settle();
assert.equal(statusGets, statusGetsBeforeDispose, "dispose 后 tick 零动作");

console.log("frontend_home_overview_behavior: all assertions passed");
