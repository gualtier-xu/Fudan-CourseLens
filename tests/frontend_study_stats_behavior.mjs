import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

/* STUDY-STATS-M1 行为钉测：本周学习面貌默认层——周条形/到期待办/掌握度 top3
 * 的纯模型与渲染（首跑引导态/数据不足灰档/分钟数零假 0），死格零残留
 * （index.html/study.js/components.css 三面 grep 钉），播放心跳接线
 * （30s 步长 + insight 开关同源门 + play/pause/ended 挂卸配对）。 */

class FakeElement {
  constructor(tag, id = "") {
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.attributes = new Map();
    this.style = { setProperty() {}, removeProperty() {} };
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  append(...nodes) {
    for (const node of nodes) {
      if (!node) continue;
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  addEventListener(type, fn) {
    this.listeners = this.listeners || new Map();
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) {
    this.listeners?.get(type)?.delete(fn);
  }
  /* M2-b：入口/关闭钮走真实监听分发——桩件 click 即派发已登记监听。 */
  click() {
    for (const fn of this.listeners?.get("click") || []) fn({ preventScroll() {} });
  }
  querySelectorAll(selector) {
    const found = [];
    const match = (node) => {
      const text = String(selector);
      if (text.startsWith(".")) return String(node.className || "").split(/\s+/).includes(text.slice(1));
      if (text.startsWith("[")) return node.attributes.has(text.slice(1, -1).split("=")[0]);
      /* M2-b：renderStudyDetail 按 #id 取 dialog 内挂载点，桩件同法支持。 */
      if (text.startsWith("#")) return node.id === text.slice(1);
      return node.tagName === text.toUpperCase();
    };
    const walk = (node) => (node.children || []).forEach((child) => {
      if (match(child)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get text() {
    const collect = (node) => String(node.textContent || "") + (node.children || []).map(collect).join("");
    return collect(this);
  }
}

let createdSeq = 0;
/* F1（STUDY-STATS-GFIX-1）三态宿主/区域桩注册表：installStudyStats 的宿主重挂按
 * id 取节点，钉测可装配诗页/选课面板/学习桌三态（空注册表=精简壳语义，卡留守）。 */
const domRegistry = new Map();
globalThis.document = {
  getElementById: (id) => (id === "study-week-card" ? mount : domRegistry.get(id) ?? null),
  createElement: (tag) => new FakeElement(tag, `created-${createdSeq += 1}`),
  querySelector: () => null,
  querySelectorAll: () => [],
  activeElement: null,
};
globalThis.window = Object.assign(new EventTarget(), {
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
});
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
};
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null; },
  setItem(key, value) { this.store.set(key, String(value)); },
};
globalThis.fetch = async () => { throw new Error("no network in test"); };

const mount = new FakeElement("section", "study-week-card");
const stats = await import("../frontend/modules/study-stats.js");

const WEEK_READY = {
  view: "study_overview",
  first_run: false,
  today: "2026-10-07",
  week: {
    days: [
      { date: "2026-10-05", seconds: 1800, interactions: 2, active: true },
      { date: "2026-10-06", seconds: 0, interactions: 3, active: true },
      { date: "2026-10-07", seconds: 600, interactions: 0, active: true },
      { date: "2026-10-08", seconds: 0, interactions: 0, active: false },
      { date: "2026-10-09", seconds: 0, interactions: 0, active: false },
      { date: "2026-10-10", seconds: 0, interactions: 0, active: false },
      { date: "2026-10-11", seconds: 0, interactions: 0, active: false },
    ],
    study_days: 3,
    course_count: 2,
    avg_completion_percent: 62,
    minutes_total: 40,
  },
  due: {
    flashcards_due: 12,
    flashcards_top_course_id: "course-1",
    next_exam: { title: "期中考试", course_id: "course-1", days_left: 5, source: "assessment" },
  },
  mastery: {
    all_clear: false,
    courses: [
      { course_id: "course-1", tier: "薄弱", gray: false, basis: ["quiz"],
        evidence: "最近测验对题率偏低，错题值得回看一遍" },
      { course_id: "course-2", tier: "数据不足", gray: true, basis: [],
        evidence: "学习信号还太少，暂时看不出掌握情况" },
    ],
  },
};

/* ---- 纯模型：周条形 ---- */
const bar = stats.weekBarModel(WEEK_READY.week, WEEK_READY.today);
assert.equal(bar.length, 7, "周条形恒 7 格");
assert.equal(bar[0].label, "周一");
assert.equal(bar[0].state, "active");
assert.equal(bar[1].state, "active", "纯互动日（0 秒）也点亮=有学习互动的诚实口径");
assert.equal(bar[2].isToday, true);
for (const index of [3, 4, 5, 6]) {
  assert.equal(bar[index].state, "future", "今天之后的格子是 future");
}
assert.ok(bar[0].intensity > bar[2].intensity, "时长决定亮度");
assert.match(bar[0].title, /30 分钟/);
const idleBar = stats.weekBarModel({
  days: WEEK_READY.week.days.map((day) =>
    day.date === "2026-10-05" ? { ...day, seconds: 0, interactions: 0, active: false } : day),
}, "2026-10-08");
assert.equal(idleBar[0].state, "idle", "今天之前没学习的日子=idle（空档诚实显示）");
assert.equal(idleBar[1].state, "active");

/* F4（STUDY-STATS-GFIX-1）：周日格标签——getUTCDay() 周日=0，旧写法 -1 下标
 * 取到 undefined（周日格无标签无标题、aria 双逗号）；(+6)%7 归一后周日=下标 6。 */
const sundayBar = stats.weekBarModel({
  days: [
    { date: "2026-10-11", seconds: 1200, interactions: 0, active: true },
    { date: "2026-10-05", seconds: 0, interactions: 0, active: false },
  ],
}, "2026-10-11");
assert.equal(sundayBar[0].label, "周日", "周日格必须有标签（旧缺陷=空串）");
assert.equal(sundayBar[0].title, "周日 · 20 分钟", "周日格 title 完整（tooltip 与 aria 同源）");
assert.equal(sundayBar[1].label, "周一", "周一格不回归");

/* ---- 纯模型：主行文案（分钟数只在有数据时出现，绝不显示 0） ---- */
const summary = stats.weekSummaryModel(WEEK_READY.week);
assert.equal(summary.empty, false);
assert.match(summary.text, /本周学了 3 天/);
assert.match(summary.text, /2 门课/);
assert.match(summary.text, /平均看到 62%/);
assert.match(summary.text, /约 40 分钟/);
const emptyWeek = stats.weekSummaryModel({
  days: WEEK_READY.week.days.map((day) => ({ ...day, seconds: 0, interactions: 0, active: false })),
  study_days: 0, course_count: 0, avg_completion_percent: null, minutes_total: null,
});
assert.equal(emptyWeek.empty, true);
const noMinutesSummary = stats.weekSummaryModel({
  ...WEEK_READY.week, minutes_total: null, avg_completion_percent: null,
});
assert.doesNotMatch(noMinutesSummary.text, /分钟/, "无时长数据绝不显示 0 分钟");

/* ---- 纯模型：到期待办（每行一动作；空=人话不渲染骨架） ---- */
const dueRows = stats.dueRowsModel(WEEK_READY.due);
assert.equal(dueRows.length, 2);
assert.equal(dueRows[0].key, "flashcards");
assert.match(dueRows[0].text, /12 张/);
assert.equal(dueRows[0].action, "去复习");
assert.equal(dueRows[1].key, "exam");
assert.match(dueRows[1].text, /期中考试还有 5 天/);
assert.equal(stats.dueRowsModel({ flashcards_due: 0, next_exam: null }).length, 0, "无到期=零行");
assert.equal(stats.dueRowsModel({}).length, 0);

/* ---- 纯模型：掌握度行（标题解析+灰档） ---- */
const titles = new Map([["course-1", "线性代数"], ["course-2", "数据结构"]]);
const mastery = stats.masteryRowsModel(WEEK_READY.mastery, titles);
assert.equal(mastery.rows[0].title, "线性代数");
assert.equal(mastery.rows[0].gray, false);
assert.equal(mastery.rows[1].title, "数据结构");
assert.equal(mastery.rows[1].gray, true, "数据不足=灰档诚实显示");
assert.equal(mastery.allClear, false);
assert.equal(stats.masteryRowsModel({ all_clear: true, courses: [] }, titles).allClear, true);

/* ---- 渲染：first-run=引导态（非空图表） ---- */
mount.replaceChildren(new FakeElement("span", "loading"));
stats.renderStudyWeekCard(mount, { view: "study_overview", first_run: true }, {});
assert.match(mount.text, /学完第一讲，这里会告诉你学得怎么样/, "首跑=引导态文案");
assert.doesNotMatch(mount.text, /到期待办/, "首跑不渲染三块骨架");

/* ---- 渲染：ready=三块纵排 + 灰档 data-tier ---- */
mount.replaceChildren();
stats.renderStudyWeekCard(mount, WEEK_READY, { courseTitles: titles });
assert.match(mount.text, /本周学习面貌/);
assert.match(mount.text, /学了什么/);
assert.match(mount.text, /到期待办/);
assert.match(mount.text, /掌握度/);
assert.match(mount.text, /线性代数/);
const tierNodes = mount.querySelectorAll(".p5s-tier");
assert.equal(tierNodes.length, 2, "档位徽章成对渲染");
assert.equal(tierNodes[1].dataset.tier, "数据不足", "data-tier 钩子（读屏与样式按五档可辨）");

/* ---- 渲染：due 空态与掌握度全绿态 ---- */
mount.replaceChildren();
stats.renderStudyWeekCard(mount, {
  ...WEEK_READY,
  due: { flashcards_due: 0, flashcards_top_course_id: "", next_exam: null },
  mastery: { all_clear: true, courses: [] },
}, { courseTitles: titles });
assert.ok(mount.text.includes(stats.DUE_EMPTY_TEXT), "到期待办空态=一句人话");
assert.ok(mount.text.includes(stats.MASTERY_CLEAR_TEXT), "全扎实=都在正轨上");

/* ---- 死格零残留（三面 grep 钉） ---- */
const repoRoot = new URL("../", import.meta.url);
const indexHtml = await readFile(new URL("frontend/index.html", repoRoot), "utf8");
const studyJs = await readFile(new URL("frontend/modules/study.js", repoRoot), "utf8");
const componentsCss = await readFile(new URL("frontend/styles/components.css", repoRoot), "utf8");
assert.ok(!indexHtml.includes("analytics-summary"), "index.html 死格宿主已删");
assert.ok(!indexHtml.includes("metrics-row"), "index.html 不再引用 metrics-row");
assert.ok(!/function loadAnalytics|loadAnalytics\(/.test(studyJs), "study.js loadAnalytics 函数与调用点已删");
assert.ok(!studyJs.includes("analytics-summary"), "study.js 不再消费 analytics-summary");
assert.ok(!/^\.metrics-row/m.test(componentsCss), "components.css 死格样式规则已清（仅留墓碑注释不算残留）");
assert.ok(!componentsCss.includes("minmax(90px, 1fr)"), "metrics-row 网格规则实体已删");
assert.ok(indexHtml.includes('id="study-week-card"'), "新默认层卡在着陆页");

/* ---- 心跳接线钉：30s 步长 + insight 开关同源门 + 播放事件配对 ---- */
const playerCore = await readFile(new URL("frontend/modules/player-core.js", repoRoot), "utf8");
assert.match(playerCore, /STUDY_HEARTBEAT_STEP_SECONDS = 30/, "心跳步长 30s");
assert.match(
  playerCore,
  /function studyHeartbeatAllowed\(\)\s*\{\s*return insightEnabled\(\) && playbackKind === "lecture";\s*\}/,
  "心跳沿用 courselens:insight 开关（同一隐私级，无第二开关）",
);
assert.match(playerCore, /player\.addEventListener\("play", startStudyHeartbeat\)/, "play 启动心跳");
assert.match(playerCore, /stopStudyHeartbeat\(\); \/\* 暂停即停表/, "暂停停表");
assert.match(playerCore, /player\.removeEventListener\("play", startStudyHeartbeat\)/, "teardown 卸载配对");
assert.match(playerCore, /postV3\("study\/heartbeat"/, "心跳落本地 /api/v3/study/heartbeat");
assert.ok(!playerCore.match(/study\/heartbeat[\s\S]{0,200}analytics/i), "心跳不进 analytics 遥测面");

/* ---- 行动作兜底：无课程身份时退回学习页（单事件导航） ---- */
{
  const store = { courses: [], set() {}, activeCourse: null };
  let pageEvent = null;
  globalThis.window.addEventListener("courselens:select-page", (event) => { pageEvent = event.detail; });
  await stats.handleDueAction(store, { key: "exam", course_id: "" });
  assert.equal(pageEvent, "study", "安排行动作=跳学习页复习区");
}

/* ---- F1/F3（STUDY-STATS-GFIX-1）：三态宿主重挂 + 目录后到重渲染 ---- */
{
  /* 装配学习页三态区域（初始=着陆态：empty 可见，select/desk hidden）。 */
  const regionEmpty = new FakeElement("div", "study-empty");
  const regionSelect = new FakeElement("div", "study-select");
  const regionDesk = new FakeElement("div", "study-desk");
  regionSelect.hidden = true;
  regionDesk.hidden = true;
  const overview = new FakeElement("div", "home-overview");
  overview.className = "home-overview"; /* 构造器二参=id；类选择器匹配走 className */
  regionSelect.append(overview);
  const hostEmpty = new FakeElement("div", "study-week-card-host-empty");
  const hostDesk = new FakeElement("div", "study-week-card-host-desk");
  for (const [id, node] of Object.entries({
    "study-empty": regionEmpty, "study-select": regionSelect, "study-desk": regionDesk,
    "study-week-card-host-empty": hostEmpty, "study-week-card-host-desk": hostDesk,
  })) domRegistry.set(id, node);
  /* 可水合 store 桩（subscribe/set 同 store.js 合同）；fetch 桩回 overview 载荷。 */
  const listeners = new Map();
  const store = {
    courses: [],
    set(key, value) {
      this[key] = value;
      (listeners.get(key) || []).forEach((fn) => fn(value));
    },
    subscribe(key, fn) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(fn);
      return () => listeners.get(key)?.delete(fn);
    },
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify({ schema: "courselens.api.v3", data: WEEK_READY }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
  const dispose = stats.installStudyStats(store);
  try {
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(mount.parent, hostEmpty, "F1 诗页着陆态卡挂 empty 宿主（到卡 0 点击）");
    assert.match(mount.text, /这门课/, "F3 boot 水合时目录未载入=匿名占位");
    store.set("courses", [
      { course_id: "course-1", title: "线性代数", lectures: [] },
      { course_id: "course-2", title: "数据结构", lectures: [] },
    ]);
    assert.doesNotMatch(mount.text, /这门课/, "F3 目录就绪重渲染后无匿名行");
    assert.match(mount.text, /线性代数/, "F3 掌握度行显示真实课程名");
    window.dispatchEvent(new CustomEvent("courselens:study-mode", { detail: "select" }));
    assert.equal(mount.parent, overview, "F1 select 态卡回今日概览原位");
    window.dispatchEvent(new CustomEvent("courselens:study-mode", { detail: "desk" }));
    assert.equal(mount.parent, hostDesk, "F1 续学着陆态卡挂桌首宿主（0 点击）");
    window.dispatchEvent(new CustomEvent("courselens:study-mode", { detail: "empty" }));
    assert.equal(mount.parent, hostEmpty, "F1 empty 态卡回诗页宿主");
  } finally {
    dispose();
    globalThis.fetch = realFetch;
    for (const id of regionSelect ? [
      "study-empty", "study-select", "study-desk",
      "study-week-card-host-empty", "study-week-card-host-desk",
    ] : []) domRegistry.delete(id);
    mount.replaceChildren();
  }
}

/* ---- F5（STUDY-STATS-GFIX-1）：标签与填充条成对取色 grep 钉 ---- */
{
  const pagesCss = await readFile(new URL("frontend/styles/pages.css", repoRoot), "utf8");
  assert.match(
    pagesCss,
    /\.p5s-day-label \{ position: relative; color: var\(--accent-contrast\)/,
    "叠填充条标签配恒亮对比色（暗色 active 日 AA ≥4.5）",
  );
  assert.ok(
    !/\.p5s-day-label \{ position: relative; color: var\(--surface-strong\)/.test(pagesCss),
    "表面色不再直接作填充条上标签色（暗色 2.1:1 缺陷态不回归）",
  );
}

/* ==== STUDY-STATS-M2-b 展开层：预测条/课程→讲明细/单入口 dialog ==== */

/* ---- 纯模型：FSRS 7 日预测条 ---- */
{
  const forecast = stats.forecastBarModel([
    { date: "2026-10-07", due: 12 },
    { date: "2026-10-08", due: 4 },
    { date: "2026-10-09", due: 0 },
    { date: "2026-10-10", due: 0 },
    { date: "2026-10-11", due: 0 },
    { date: "2026-10-12", due: 0 },
    { date: "2026-10-13", due: 6 },
  ]);
  assert.equal(forecast.length, 7);
  assert.equal(forecast[0].label, "今天", "预测条首格=今天");
  assert.equal(forecast[0].due, 12);
  assert.ok(forecast[0].intensity > forecast[1].intensity, "到期张数决定亮度");
  assert.equal(forecast[2].due, 0);
  assert.match(forecast[0].title, /到期 12 张/);
  assert.match(forecast[2].title, /没有到期的闪卡/);
  assert.equal(stats.forecastBarModel([]).length, 0);
  assert.equal(stats.forecastBarModel(undefined).length, 0);
}

/* ---- 纯模型：课程→讲明细行（诚实三态） ---- */
{
  const rows = stats.detailCourseRowsModel([
    {
      course_id: "course-1", tier: "待巩固", gray: false, basis: ["quiz"],
      evidence: "最近测验对题率偏低，错题值得回看一遍",
      completion_percent: 62, week_interactions: 5,
      lectures: [
        { sub_id: "lec-1", label: "10-01 第一讲", percent: 80, completed: false, seconds: 1500,
          replays: 3, open_bookmarks: 2, quiz_graded: 4, quiz_correct: 1, quiz_ungraded: 1,
          last_correct: false, flashcards_due: 5 },
        { sub_id: "lec-2", label: "", percent: null, seconds: 0, replays: 0,
          open_bookmarks: 0, quiz_graded: 0, quiz_ungraded: 0, flashcards_due: 0 },
      ],
    },
  ], new Map([["course-1", "计算机网络"]]));
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.title, "计算机网络");
  assert.equal(row.partialNote, "仅基于测验", "单路数据必须带「仅基于 X」标注");
  assert.equal(row.flashcardsDue, 5, "课程行到期数=讲行合计");
  assert.equal(row.completionText, "平均看到 62%");
  const [seen, untouched] = row.lectures;
  assert.equal(seen.label, "10-01 第一讲");
  assert.match(seen.percentText, /看到 80%/);
  assert.equal(seen.quizGraded, 4);
  assert.equal(untouched.label, "lec-2", "无标签讲回退 sub_id（绝不空白行）");
  assert.equal(untouched.percentText, stats.LECTURE_UNTOUCHED_TEXT, "没看过=诚实「还没看过」，绝不显示 0%");
}

/* ---- 渲染：展开层 dialog（预测条+课程明细+空态） ---- */
{
  const dialog = new FakeElement("dialog", "study-detail-dialog");
  const forecastMount = new FakeElement("div", "study-detail-forecast");
  const coursesMount = new FakeElement("div", "study-detail-courses");
  dialog.append(forecastMount, coursesMount);
  stats.renderStudyDetail(dialog, null, new Map());
  assert.ok(coursesMount.text.includes(stats.DETAIL_EMPTY_TEXT), "无载荷=引导态一句人话");
  forecastMount.replaceChildren();
  coursesMount.replaceChildren();
  stats.renderStudyDetail(dialog, {
    today: "2026-10-07",
    forecast: [
      { date: "2026-10-07", due: 12 }, { date: "2026-10-08", due: 0 }, { date: "2026-10-09", due: 0 },
      { date: "2026-10-10", due: 0 }, { date: "2026-10-11", due: 0 }, { date: "2026-10-12", due: 0 },
      { date: "2026-10-13", due: 6 },
    ],
    courses: [
      {
        course_id: "course-1", tier: "待巩固", gray: false, basis: ["quiz"],
        evidence: "最近测验对题率偏低", completion_percent: 62, week_interactions: 5,
        lectures: [
          { sub_id: "lec-1", label: "10-01 第一讲", percent: 80, seconds: 1500, replays: 3,
            open_bookmarks: 2, quiz_graded: 4, quiz_correct: 1, quiz_ungraded: 1, flashcards_due: 5 },
          { sub_id: "lec-2", label: "10-08 第二讲", percent: null, seconds: 0, replays: 0,
            open_bookmarks: 0, quiz_graded: 0, quiz_ungraded: 0, flashcards_due: 0 },
        ],
      },
      { course_id: "course-2", tier: "数据不足", gray: true, basis: [], evidence: "学习信号还太少",
        completion_percent: null, week_interactions: 0, lectures: [] },
    ],
  }, new Map([["course-1", "计算机网络"]]));
  assert.match(forecastMount.text, /未来 7 天到期闪卡/, "预测条块标题");
  assert.match(forecastMount.text, /今天/, "今天格点名");
  assert.match(coursesMount.text, /计算机网络/);
  assert.match(coursesMount.text, /仅基于测验/, "单路标注进 dialog");
  assert.match(coursesMount.text, /看到 80%/);
  assert.match(coursesMount.text, /回看 3 次/, "回看热点计数列");
  assert.match(coursesMount.text, /没听懂 2 处/, "未解决书签列");
  assert.match(coursesMount.text, /测验对 1\/4/, "测验对错列");
  assert.match(coursesMount.text, /到期 5 张/, "到期卡列");
  assert.match(coursesMount.text, /还没看过/, "无信号讲=诚实零行");
  const tiers = dialog.querySelectorAll(".p5s-tier");
  assert.equal(tiers.length, 2, "dialog 内档位徽章成对");
  assert.equal(tiers[1].dataset.tier, "数据不足");
  /* 零到期预测不渲染空预测块（诚实零骨架）。 */
  forecastMount.replaceChildren();
  coursesMount.replaceChildren();
  stats.renderStudyDetail(dialog, { forecast: [{ date: "2026-10-07", due: 0 }], courses: [] }, new Map());
  assert.equal(forecastMount.children.length, 0, "全零预测=不渲染空图表");
  assert.ok(coursesMount.text.includes(stats.DETAIL_EMPTY_TEXT));
}

/* ---- 默认层掌握度块：单入口按钮（首跑无按钮）+「仅基于 X」标注 ---- */
{
  mount.replaceChildren();
  let opened = 0;
  stats.renderStudyWeekCard(mount, WEEK_READY, { onOpenDetail: () => { opened += 1; } });
  assert.ok(mount.text.includes(stats.DETAIL_ENTRY_TEXT), "ready 态掌握度块带展开层单入口");
  const entry = mount.querySelectorAll(".p5s-detail-entry")[0];
  assert.ok(entry, "入口是按钮（键盘可达）");
  assert.match(mount.text, /仅基于测验/, "单路课程在默认层也带诚实标注");
  entry.click();
  assert.equal(opened, 1, "单入口点击回调");
  /* 首跑引导态：无入口按钮（没有明细可看）。 */
  mount.replaceChildren();
  stats.renderStudyWeekCard(mount, { view: "study_overview", first_run: true }, { onOpenDetail: () => {} });
  assert.equal(mount.querySelectorAll(".p5s-detail-entry").length, 0, "首跑不渲染明细入口");
}

/* ---- installStudyStats：dialog 单入口接线（打开即 loading→fetch 渲染→可关闭） ---- */
{
  const dialog = new FakeElement("dialog", "study-detail-dialog");
  let showModalCalls = 0;
  let closeCalls = 0;
  dialog.showModal = () => { showModalCalls += 1; dialog.open = true; };
  dialog.close = () => { closeCalls += 1; dialog.open = false; };
  const forecastMount = new FakeElement("div", "study-detail-forecast");
  const coursesMount = new FakeElement("div", "study-detail-courses");
  dialog.append(forecastMount, coursesMount);
  const stateLine = new FakeElement("p", "study-detail-state");
  const closeBtn = new FakeElement("button", "close-study-detail-dialog");
  domRegistry.set("study-detail-dialog", dialog);
  domRegistry.set("study-detail-forecast", forecastMount);
  domRegistry.set("study-detail-courses", coursesMount);
  domRegistry.set("study-detail-state", stateLine);
  domRegistry.set("close-study-detail-dialog", closeBtn);
  const listeners = new Map();
  const store = {
    courses: [{ course_id: "course-1", title: "计算机网络", lectures: [] }],
    set() {},
    subscribe(key, fn) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(fn);
      return () => listeners.get(key)?.delete(fn);
    },
  };
  const realFetch = globalThis.fetch;
  let detailFetches = 0;
  globalThis.fetch = async (url) => {
    if (String(url).includes("study/detail")) {
      detailFetches += 1;
      return new Response(JSON.stringify({
        schema: "courselens.api.v3",
        data: {
          view: "study_detail", today: "2026-10-07",
          forecast: [{ date: "2026-10-07", due: 3 }, { date: "2026-10-08", due: 0 }],
          courses: [{ course_id: "course-1", tier: "待巩固", gray: false, basis: ["quiz"],
            evidence: "最近测验对题率偏低", completion_percent: 62, week_interactions: 5,
            lectures: [] }],
        },
      }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return new Response(JSON.stringify({ schema: "courselens.api.v3", data: WEEK_READY }),
      { status: 200, headers: { "content-type": "application/json" } });
  };
  const dispose = stats.installStudyStats(store);
  try {
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(stateLine.textContent, "", "boot 期状态行安静");
    /* 默认层 ready 渲染带单入口（install 水合已完成，mount 内即有按钮）。 */
    const entry = mount.querySelectorAll(".p5s-detail-entry")[0];
    assert.ok(entry, "install 水合后的卡带展开层入口");
    entry.click();
    assert.equal(showModalCalls, 1, "单入口打开 dialog");
    assert.match(stateLine.textContent, /正在载入/, "打开即 loading 态（零呆等·即时反馈）");
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(detailFetches, 1, "打开触发一次 study/detail 取数");
    assert.match(coursesMount.text, /计算机网络/, "明细渲染进 dialog");
    assert.match(forecastMount.text, /未来 7 天到期闪卡/, "预测条渲染进 dialog");
    assert.equal(stateLine.textContent, "", "取数成功后 loading 撤除");
    /* 关闭钮：只进不出——close 即回默认层。 */
    closeBtn.click();
    assert.equal(closeCalls, 1, "关闭钮走 dialog.close");
    /* 再开一次：缓存明细即时上屏 + 后台刷新（stale-while-revalidate）。 */
    coursesMount.replaceChildren();
    entry.click();
    assert.equal(showModalCalls, 2);
    assert.match(coursesMount.text, /计算机网络/, "二次打开即时渲染缓存明细");
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(detailFetches, 2, "二次打开仍后台刷新保新鲜");
  } finally {
    dispose();
    globalThis.fetch = realFetch;
    for (const id of ["study-detail-dialog", "study-detail-forecast", "study-detail-courses", "study-detail-state", "close-study-detail-dialog"]) {
      domRegistry.delete(id);
    }
    mount.replaceChildren();
  }
}

console.log("frontend_study_stats_behavior: all pins green");
