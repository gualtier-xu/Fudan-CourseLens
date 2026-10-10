import assert from "node:assert/strict";

/* 学习空态问候/诗联行为测试（视觉 Stage 2 批 4 + VT-1 情境标签）：
   加载真实 frontend/modules/greeting.js；纯函数直测 + installGreeting 桩件
   DOM/window/document 装配（仿 tests/frontend_onboarding_guide_behavior.mjs 桩件法）。
   覆盖：时段分档查表、课量尾句矩阵（含快照缺位/异常退化）、节日三元、
   确定性轮换（同日同档稳定/跨档换句）、点击换句+冷却、三态按钮文案、静态兜底在场、
   stripTrailingPeriod 显示层剥尾句号、语料缺席诚实禁用（装载前禁用/装载后恢复）、
   情境标签候选集池组成（未标注句恒在池 ∪ 标注命中句、同档稳定、点击回绕、
   午夜边界钉、混合语料未标注句多语境可达——全部桩时钟，零真实墙钟依赖）。 */

class FakeClassList {
  constructor(node) { this.node = node; this.values = new Set(); }
  _sync() { this.node.className = [...this.values].join(" "); }
  add(...values) { values.forEach((value) => this.values.add(value)); this._sync(); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); this._sync(); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this.classList = new FakeClassList(this);
    this.textContent = "";
    this.children = [];
    this.parent = null;
    this.hidden = false;
    this.disabled = false;
    this._listeners = new Map();
  }
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
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
  querySelector(selector) {
    const wanted = String(selector).replace(/^\./, "");
    for (const child of this.children) {
      if (String(child.className || "").split(/\s+/).includes(wanted)) return child;
    }
    return null;
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
}

class FakeCustomEvent extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
}

/* ---- 合成环境 ---- */

let byId = {};

function buildDom() {
  byId = {};
  const el = (tag, id) => { const node = new FakeElement(tag, id); byId[id] = node; return node; };
  const greeting = el("p", "empty-greeting");
  greeting.textContent = "你好。"; /* index.html 静态兜底 */
  const verse = el("button", "empty-verse");
  verse.classList.add("empty-verse");
  const verseText = new FakeElement("span");
  verseText.className = "empty-verse-text";
  verseText.textContent = "纸上得来终觉浅，绝知此事要躬行。";
  const verseHint = new FakeElement("span");
  verseHint.className = "empty-verse-hint";
  verseHint.textContent = "换一句";
  verse.append(verseText, verseHint);
  const source = el("p", "empty-verse-source");
  source.textContent = "陆游 · 冬夜读书示子聿";
  const button = el("button", "study-start-select");
  button.textContent = "选择课程";
  const root = el("div", "study-empty");
  root.append(greeting, verse, source, button);

  const documentTarget = new EventTarget();
  documentTarget.hidden = false;
  documentTarget.getElementById = (id) => byId[id] || null;
  globalThis.document = documentTarget;
  return {
    documentTarget,
    greeting, verse, verseText, verseHint, source, button,
  };
}

function buildWindow() {
  const timeouts = new Map();
  let timerSeq = 0;
  const windowTarget = new EventTarget();
  windowTarget.setTimeout = (fn) => {
    timerSeq += 1;
    timeouts.set(timerSeq, fn);
    return timerSeq;
  };
  windowTarget.clearTimeout = (id) => timeouts.delete(id);
  windowTarget.setInterval = () => {
    timerSeq += 1;
    return timerSeq; /* 测试经 visibilitychange 手动驱动时钟 tick */
  };
  windowTarget.clearInterval = () => {};
  globalThis.window = windowTarget;
  return { windowTarget, runTimeouts: () => { for (const [, fn] of [...timeouts.entries()]) fn(); } };
}

function stubFetch(payload) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => payload });
  return () => { globalThis.fetch = original; };
}

const settle = async () => {
  for (let round = 0; round < 4; round += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

const VERSES = [
  { id: 1, text: "海上生明月，天涯共此时。", author: "张九龄", work: "望月怀远", dynasty: "唐", tags: [], len: 10 },
  { id: 2, text: "纸上得来终觉浅，绝知此事要躬行。", author: "陆游", work: "冬夜读书示子聿", dynasty: "宋", tags: [], len: 14 },
  { id: 3, text: "问渠那得清如许？为有源头活水来。", author: "朱熹", work: "观书有感", dynasty: "宋", tags: [], len: 14 },
  { id: 4, text: "会当凌绝顶，一览众山小。", author: "杜甫", work: "望岳", dynasty: "唐", tags: [], len: 10 },
  { id: 5, text: "路漫漫其修远兮，吾将上下而求索。", author: "屈原", work: "离骚", dynasty: "先秦", tags: [], len: 14 },
];

const { bandIndexFor, weekdayName, holidayGreeting, courseLoadTail, composeGreeting,
  fnv1a, dateKeyFor, deterministicVerseIndex, formatSource, buttonTextFor, installGreeting,
  seasonTagFor, solarTermTagFor, courseMoodTag, stripTrailingPeriod, tailTextForKey } =
  await import("../frontend/modules/greeting.js?cases");
const { store } = await import("../frontend/modules/store.js?cases");

/* ================= 纯函数组 ================= */

{
  /* 时段分档查表：七档边界（[from, to)） */
  assert.equal(bandIndexFor(0), 0, "00:00 深夜");
  assert.equal(bandIndexFor(299), 0, "04:59 深夜");
  assert.equal(bandIndexFor(300), 1, "05:00 清晨");
  assert.equal(bandIndexFor(539), 1, "08:59 清晨");
  assert.equal(bandIndexFor(540), 2, "09:00 上午");
  assert.equal(bandIndexFor(689), 2, "11:29 上午");
  assert.equal(bandIndexFor(690), 3, "11:30 午间");
  assert.equal(bandIndexFor(809), 3, "13:29 午间");
  assert.equal(bandIndexFor(810), 4, "13:30 午后");
  assert.equal(bandIndexFor(1049), 4, "17:29 午后");
  assert.equal(bandIndexFor(1050), 5, "17:30 傍晚");
  assert.equal(bandIndexFor(1169), 5, "19:29 傍晚");
  assert.equal(bandIndexFor(1170), 6, "19:30 夜晚");
  assert.equal(bandIndexFor(1439), 6, "23:59 夜晚");
  console.log("band table passed");
}

{
  /* 课量尾句矩阵：now = 10:00（600 分钟） */
  const now = new Date(2026, 8, 16, 10, 0);
  const meeting = (start, end) => ({ start_time: start, end_time: end });
  assert.equal(courseLoadTail(null, now), null, "快照缺位退回");
  assert.equal(courseLoadTail(undefined, now), null, "快照缺位退回");
  assert.equal(courseLoadTail([], now), "今日无课。", "快照在而 0 节");
  assert.equal(courseLoadTail([{ start_time: "08:00", end_time: "bad" }], now), null, "时间全坏视为异常");
  assert.equal(courseLoadTail([meeting("10:30", "11:15")], now), null, "1–4 节未开始诚实留白");
  assert.equal(courseLoadTail([meeting("09:00", "10:30")], now), null, "1–4 节进行中诚实留白");
  assert.equal(courseLoadTail([meeting("08:00", "08:45"), meeting("09:00", "09:45")], now), "今日课毕。", "1–4 节已毕");
  /* ≥5 节：未过半 / 已过半 / 全部已毕（末节 end 均相对 now=10:00） */
  const full = (ends) => ends.map((end) => meeting(`${String(Math.floor(end / 60)).padStart(2, "0")}:00`, `${String(Math.floor(end / 60)).padStart(2, "0")}:${String(end % 60).padStart(2, "0")}`));
  assert.equal(courseLoadTail(full([555, 615, 675, 735, 795]), now), "今日课满。", "≥5 节未开始/未过半");
  assert.equal(courseLoadTail(full([465, 525, 585, 645, 705]), now), "今日课满，已过半。", "≥5 节进行中已过半");
  assert.equal(courseLoadTail(full([345, 405, 465, 525, 585]), now), "今日课毕，辛苦了。", "≥5 节已毕");
  /* 深夜档不叠加课量 */
  const deepNight = new Date(2026, 8, 16, 2, 0);
  assert.equal(courseLoadTail([], deepNight), null, "深夜档不叠加");
  console.log("course-load tail matrix passed");
}

{
  /* 节日三元（总控裁决①）与星期时段问候 */
  assert.equal(holidayGreeting(new Date(2026, 0, 1)), "元旦快乐。");
  assert.equal(holidayGreeting(new Date(2026, 4, 4)), "青年节快乐。");
  assert.equal(holidayGreeting(new Date(2026, 8, 10)), "教师节快乐。");
  assert.equal(holidayGreeting(new Date(2026, 8, 16)), null, "非节日");
  const wednesday = new Date(2026, 8, 16, 14, 0); /* 周三 */
  assert.equal(weekdayName(wednesday), "周三");
  assert.equal(composeGreeting(wednesday, null), "周三下午好。", "缺快照退基础问候");
  assert.equal(composeGreeting(wednesday, []), "周三下午好，今日无课。", "0 节叠加尾句");
  assert.equal(composeGreeting(new Date(2026, 0, 1, 14, 0), []), "元旦快乐。", "节日整句替换不叠课量");
  assert.equal(composeGreeting(new Date(2026, 8, 16, 2, 0), null), "周三，夜深了。", "深夜档");
  console.log("holiday & compose passed");
}

{
  /* 确定性轮换：同日同档稳定、跨档/跨日换句 */
  const first = deterministicVerseIndex(VERSES.length, "2026-09-16", 4);
  assert.equal(deterministicVerseIndex(VERSES.length, "2026-09-16", 4), first, "同日同档稳定");
  assert.notEqual(deterministicVerseIndex(VERSES.length, "2026-09-16", 5), first, "同日跨档换句");
  assert.notEqual(deterministicVerseIndex(VERSES.length, "2026-09-17", 4), first, "跨日换句");
  assert.equal(fnv1a("abc"), fnv1a("abc"), "hash 确定");
  assert.equal(dateKeyFor(new Date(2026, 8, 4)).slice(-2), "04", "日期键零填充");
  assert.equal(formatSource(VERSES[0]), "张九龄 · 望月怀远");
  assert.equal(formatSource({ author: "佚名", work: "" }), "佚名");
  console.log("deterministic rotation passed");
}

{
  /* 视觉②：stripTrailingPeriod 只剥结尾全角句号（含连续），问号/感叹号与
     句中逗号、分号保留；无句号原样返回。composeGreeting 原文零改动。 */
  assert.equal(stripTrailingPeriod("周三，夜深了。"), "周三，夜深了", "问候句剥句号");
  assert.equal(stripTrailingPeriod("纸上得来终觉浅，绝知此事要躬行。"), "纸上得来终觉浅，绝知此事要躬行", "诗联剥句号");
  assert.equal(stripTrailingPeriod("海上生明月，天涯共此时。"), "海上生明月，天涯共此时", "句中逗号保留");
  assert.equal(stripTrailingPeriod("好。。"), "好", "连续句号全剥");
  assert.equal(stripTrailingPeriod("你好"), "你好", "无句号原样");
  assert.equal(stripTrailingPeriod("问渠那得清如许？"), "问渠那得清如许？", "问号保留");
  assert.equal(stripTrailingPeriod("求索！"), "求索！", "感叹号保留");
  assert.equal(stripTrailingPeriod(""), "", "空串原样");
  console.log("strip trailing period passed");
}

{
  /* 按钮三态 + degraded 扩展（总控裁决②） */
  assert.equal(buttonTextFor("ready"), "选择课程");
  /* FA1⑬c：checking 恒不占主按钮——未启用自动登录给登录主文案，恢复信息走顶栏轻提示 */
  assert.equal(buttonTextFor("checking"), "登录后选择课程");
  assert.equal(buttonTextFor("degraded"), "重新登录并选择课程");
  assert.equal(buttonTextFor("action_required"), "登录后选择课程");
  assert.equal(buttonTextFor(undefined), "登录后选择课程");
  console.log("button three-state passed");
}

{
  /* AUTOLOGIN-LOCAL-FIRST-1②：自动登录恢复期文案弱化——checking+自动登录开
     =常规主文案；自动登录关/默认=现状 RESUMING；degraded 两态恒提示重新登录 */
  assert.equal(buttonTextFor("checking", { autoLoginResume: true }), "选择课程", "恢复期不出 RESUMING");
  assert.equal(buttonTextFor("checking", { autoLoginResume: false }), "登录后选择课程", "⑬c：未启用自动登录给登录主文案");
  assert.equal(buttonTextFor("checking"), "登录后选择课程", "⑬c：默认参数同");
  assert.equal(buttonTextFor("ready", { autoLoginResume: true }), "选择课程", "ready 不受影响");
  assert.equal(buttonTextFor("degraded", { autoLoginResume: true }), "重新登录并选择课程", "degraded 恒提示重登");
  assert.equal(buttonTextFor("degraded", { autoLoginResume: false }), "重新登录并选择课程");
  console.log("button resume de-emphasis passed");
}

{
  /* 情境标签纯函数（VT-1，全部显式传日期零真实时钟）：季节窗口 / 节令表与
     表外回退 / 心境映射 */
  assert.equal(seasonTagFor(new Date(2027, 2, 15)), "spring", "3 月春");
  assert.equal(seasonTagFor(new Date(2027, 4, 31)), "spring", "5 月仍春");
  assert.equal(seasonTagFor(new Date(2027, 5, 1)), "summer", "6 月夏");
  assert.equal(seasonTagFor(new Date(2027, 7, 31)), "summer", "8 月仍夏");
  assert.equal(seasonTagFor(new Date(2027, 8, 1)), "autumn", "9 月秋");
  assert.equal(seasonTagFor(new Date(2027, 10, 30)), "autumn", "11 月仍秋");
  assert.equal(seasonTagFor(new Date(2027, 11, 1)), "winter", "12 月冬");
  assert.equal(seasonTagFor(new Date(2027, 0, 15)), "winter", "1 月冬");

  /* 节令：清明/冬至固定公历窗口（±1 天） */
  assert.equal(solarTermTagFor(new Date(2027, 3, 4)), "qingming", "清明窗首日");
  assert.equal(solarTermTagFor(new Date(2027, 3, 5)), "qingming", "清明正日");
  assert.equal(solarTermTagFor(new Date(2027, 3, 6)), "qingming", "清明窗末日");
  assert.equal(solarTermTagFor(new Date(2027, 3, 3)), null, "清明窗前一日不命中");
  assert.equal(solarTermTagFor(new Date(2027, 3, 7)), null, "清明窗后一日不命中");
  assert.equal(solarTermTagFor(new Date(2027, 11, 21)), "dongzhi", "冬至窗首日");
  assert.equal(solarTermTagFor(new Date(2027, 11, 22)), "dongzhi", "冬至正日");
  assert.equal(solarTermTagFor(new Date(2027, 11, 23)), "dongzhi", "冬至窗末日");

  /* 节令：农历日期表（2026–2035）命中 */
  assert.equal(solarTermTagFor(new Date(2026, 1, 17)), "new-year", "2026 春节");
  assert.equal(solarTermTagFor(new Date(2026, 1, 16)), "newyear-eve", "2026 除夕");
  assert.equal(solarTermTagFor(new Date(2026, 8, 25)), "mid-autumn", "2026 中秋");
  assert.equal(solarTermTagFor(new Date(2028, 4, 28)), "duanwu", "2028 端午（闰五月年）");
  assert.equal(solarTermTagFor(new Date(2026, 9, 18)), "chongyang", "2026 重阳");
  assert.equal(solarTermTagFor(new Date(2026, 7, 19)), "qixi", "2026 七夕");
  assert.equal(solarTermTagFor(new Date(2031, 1, 6)), "lantern", "2031 元宵");
  /* 表外年份退回季节匹配（null，由季节标签兜底） */
  assert.equal(solarTermTagFor(new Date(2036, 1, 17)), null, "2036 表外春节不命中");
  assert.equal(solarTermTagFor(new Date(2025, 8, 25)), null, "2025 表外中秋不命中");

  /* 心境映射（与课量尾句同骨架） */
  const noon = new Date(2026, 8, 16, 12, 0);
  const meeting = (start, end) => ({ start_time: start, end_time: end });
  const full = (ends) => ends.map((end) => meeting(`${String(Math.floor(end / 60)).padStart(2, "0")}:00`, `${String(Math.floor(end / 60)).padStart(2, "0")}:${String(end % 60).padStart(2, "0")}`));
  assert.equal(courseMoodTag(null, noon), null, "快照缺位无心境");
  assert.equal(courseMoodTag([], noon), "leisure", "无课→leisure");
  assert.equal(courseMoodTag([meeting("08:00", "08:45"), meeting("09:00", "09:45")], noon), "diligent", "课毕→diligent");
  assert.equal(courseMoodTag([meeting("12:30", "13:00")], noon), "diligent", "1–4 未开始→diligent");
  assert.equal(courseMoodTag(full([960, 1020, 1080, 1140, 1200]), noon), "diligent", "课满未开始→diligent");
  assert.equal(courseMoodTag(full([480, 540, 600, 660, 780]), noon), "exam-prep", "课满已过半→exam-prep");
  const deepNight = new Date(2026, 8, 16, 2, 0);
  assert.equal(courseMoodTag(null, deepNight), "exam-prep", "深夜档→exam-prep");
  console.log("context tags passed");
}

/* ================= 装配行为组 ================= */

{
  const dom = buildDom();
  const { windowTarget, runTimeouts } = buildWindow();
  const restoreFetch = stubFetch({ verses: VERSES });

  /* 静态兜底文案在场：JS 起来前 DOM 已有问候与诗联 */
  assert.equal(dom.greeting.textContent, "你好。");
  assert.equal(dom.verseText.textContent, "纸上得来终觉浅，绝知此事要躬行。");
  assert.equal(dom.source.textContent, "陆游 · 冬夜读书示子聿");

  let clock = new Date(2026, 8, 16, 14, 0); /* 周三 14:00 午后档 */
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  /* 诚实 UI：语料装载前按钮禁用（换句提示经 CSS :disabled 规则退场） */
  assert.equal(dom.verse.disabled, true, "装载前诗联按钮禁用");
  await settle();

  /* 问候按本地时钟渲染；语料加载后诗联被确定性首句替换 */
  assert.equal(dom.greeting.textContent, "周三下午好", "安装即渲染问候（显示层剥尾句号）");
  assert.equal(dom.verse.disabled, false, "装载成功后恢复可点");
  assert.ok(!dom.greeting.textContent.endsWith("。"), "问候渲染层无尾句号");
  const baseIndex = deterministicVerseIndex(VERSES.length, "2026-09-16", 4);
  assert.equal(dom.verseText.textContent, stripTrailingPeriod(VERSES[baseIndex].text), "确定性首句替换兜底");
  assert.ok(!dom.verseText.textContent.endsWith("。"), "诗联渲染层无尾句号");
  assert.equal(dom.source.textContent, formatSource(VERSES[baseIndex]));

  /* 三态按钮：auth 订阅驱动 */
  store.set("auth", { state: "checking", code: "fudan_session_verifying" });
  assert.equal(dom.button.textContent, "登录后选择课程", "⑬c：checking 不再占主按钮");
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  assert.equal(dom.button.textContent, "选择课程", "ready 态");
  store.set("auth", { state: "degraded", code: "fudan_session_expired" });
  assert.equal(dom.button.textContent, "重新登录并选择课程", "degraded 态");
  store.set("auth", { state: "action_required", code: "fudan_login_required" });
  assert.equal(dom.button.textContent, "登录后选择课程", "未登录态");

  /* 课表快照叠加课量尾句；值签名不变不写 DOM。
     快照日必须取桩时钟（clockNow）的日期键——模块按 dateKeyFor(clockNow())
     查今日，此处若用真实 new Date() 会在午夜后与桩日期分叉（跨午夜假红家族，
     CF-1 A/B 在案）。 */
  const today = dateKeyFor(clock);
  const snapshot = (meetings) => new FakeCustomEvent("courselens:timetable-snapshot", {
    detail: { days: [{ date: today, meetings }] },
  });
  const fiveLectures = [
    { start_time: "15:00", end_time: "15:45" }, { start_time: "16:00", end_time: "16:45" },
    { start_time: "17:00", end_time: "17:45" }, { start_time: "18:00", end_time: "18:45" },
    { start_time: "19:00", end_time: "19:45" },
  ];
  windowTarget.dispatchEvent(snapshot(fiveLectures));
  /* G2 硬化：课量尾句闭集等值钉——greeting.js 尾句恰为五变体（无课/课毕×2/课满×2），
     显示层剥结尾「。」（stripTrailingPeriod），故钉剥后形态；措辞漂移直接红 */
  assert.ok(
    ["今日无课", "今日课毕，辛苦了", "今日课毕", "今日课满，已过半", "今日课满"]
      .some((tail) => dom.greeting.textContent.endsWith(tail)),
    "课量尾句为五变体闭集（剥句号后等值钉）",
  );
  assert.ok(/今日课(满|毕)/.test(dom.greeting.textContent), "快照叠加课量尾句");
  const afterFirst = dom.greeting.textContent;
  windowTarget.dispatchEvent(snapshot(fiveLectures));
  assert.equal(dom.greeting.textContent, afterFirst, "同值快照不重渲染（签名门）");

  /* 点击换句：会话内 offset+1 + 慢动画两相 + 冷却 */
  assert.equal(dom.verse.classList.contains("is-swapping"), false, "初始无换句类");
  dom.verse.click();
  assert.equal(dom.verse.classList.contains("is-swapping"), true, "点击进入慢动画");
  const beforeSwap = dom.verseText.textContent;
  runTimeouts(); /* 相位 1：换文本 + 移除类 */
  assert.equal(dom.verse.classList.contains("is-swapping"), false, "相位 1 结束移除类");
  assert.notEqual(dom.verseText.textContent, beforeSwap, "换句文本已更新");
  const swappedText = dom.verseText.textContent;
  runTimeouts(); /* 相位 2：冷却生效 */
  dom.verse.click(); /* 冷却期内点击无效 */
  runTimeouts();
  assert.equal(dom.verseText.textContent, swappedText, "冷却期内连点物理无效");
  assert.equal(dom.verse.classList.contains("is-swapping"), false, "冷却期内不进入动画");

  /* 跨档换句：档位变化 → 确定性基线复位 offset 并重渲染 */
  clock = new Date(2026, 8, 16, 18, 30); /* 同日傍晚档 */
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.ok(dom.greeting.textContent.startsWith("周三傍晚好"), "跨档重渲染问候");
  const eveningBase = deterministicVerseIndex(VERSES.length, "2026-09-16", 5);
  assert.equal(dom.verseText.textContent, stripTrailingPeriod(VERSES[eveningBase].text), "跨档回归确定性基线");

  cleanup();
  restoreFetch();
  console.log("installed behaviors passed");
}

/* ================= 兜底组：语料不可得时静态兜底保持 ================= */

{
  buildDom();
  const { windowTarget } = buildWindow();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError("network unavailable"); };
  try {
    const cleanup = installGreeting(store, {
      clockNow: () => new Date(2026, 8, 16, 14, 0), /* 桩时钟：兜底组不吃真实墙钟 */
      swapPhaseMs: 5, swapCooldownMs: 5,
    });
    await settle();
    assert.equal(byId["empty-verse"].children[0].textContent, "纸上得来终觉浅，绝知此事要躬行。", "语料失败保留静态诗联");
    assert.equal(byId["empty-verse"].disabled, true, "语料失败后按钮保持禁用");
    byId["empty-verse"].click();
    assert.equal(byId["empty-verse"].children[0].textContent, "纸上得来终觉浅，绝知此事要躬行。", "无语料时换句无效");
    windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", { detail: null }));
    /* G2 硬化：异常快照不破坏基础问候——档位问候闭集前缀（周 X + 时段），非裸「周」 */
    const fallbackGreeting = byId["empty-greeting"].textContent;
    assert.equal(/^周[一二三四五六日]/.test(fallbackGreeting), true, `异常快照仍渲染档位问候：${fallbackGreeting}`);
    assert.ok(fallbackGreeting.length >= 5, "基础问候非空串");
    cleanup();
  } finally {
    globalThis.fetch = originalFetch;
  }
  console.log("fallback behaviors passed");
}

/* ================= 标签候选集（VT-1，全部桩时钟） ================= */

{
  const dom = buildDom();
  const { windowTarget, runTimeouts } = buildWindow();
  const TAGGED = [
    { id: 101, text: "孤舟蓑笠翁，独钓寒江雪。", author: "柳宗元", work: "江雪", dynasty: "唐", tags: ["winter"], len: 10 },
    { id: 102, text: "晚来天欲雪，能饮一杯无。", author: "白居易", work: "问刘十九", dynasty: "唐", tags: ["winter", "night"], len: 10 },
    { id: 103, text: "春眠不觉晓，处处闻啼鸟。", author: "孟浩然", work: "春晓", dynasty: "唐", tags: ["spring", "dawn"], len: 10 },
    { id: 104, text: "夜阑卧听风吹雨，铁马冰河入梦来。", author: "陆游", work: "十一月四日风雨大作", dynasty: "宋", tags: ["night", "late-night"], len: 14 },
    { id: 105, text: "纸上得来终觉浅，绝知此事要躬行。", author: "陆游", work: "冬夜读书示子聿", dynasty: "宋", tags: [], len: 14 },
    { id: 106, text: "海上生明月，天涯共此时。", author: "张九龄", work: "望月怀远", dynasty: "唐", tags: ["mid-autumn", "night"], len: 10 },
    { id: 107, text: "清明时节雨纷纷，路上行人欲断魂。", author: "杜牧", work: "清明", dynasty: "唐", tags: ["qingming"], len: 12 },
    { id: 108, text: "采菊东篱下，悠然见南山。", author: "陶渊明", work: "饮酒", dynasty: "魏晋", tags: ["leisure"], len: 10 },
    { id: 109, text: "接天莲叶无穷碧，映日荷花别样红。", author: "杨万里", work: "晓出净慈寺送林子方", dynasty: "宋", tags: ["summer"], len: 14 },
  ];
  const restoreFetch = stubFetch({ verses: TAGGED });
  let clock = new Date(2027, 0, 15, 10, 0); /* 2027-01-15 周五 上午档 → 激活 winter */
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 0, swapCooldownMs: 0 });
  await settle();
  /* VERSE-UNIFORM-ROTATION-1 池组成口径：未标注句恒在池（保序过滤）∪ 标注
     命中句；池内逐句等概率（基式 mod 池长 + 点击 offset 回绕）。「候选空→全池」
     分支为安全网，未标注恒在池后结构性不可达。 */
  const poolFor = (tags) => TAGGED.filter((verse) => !verse.tags.length || verse.tags.some((tag) => tags.includes(tag)));
  const expected = (tags, date, band) => {
    const usePool = poolFor(tags);
    return stripTrailingPeriod(usePool[deterministicVerseIndex(usePool.length, date, band)].text);
  };

  /* ① 冬语境池组成：池 = 全部未标注 ∪ winter 命中；夏句/春句等不在池 */
  const winterPool = poolFor(["winter"]);
  assert.equal(winterPool.length, 3, "①池长 = 未标注 1 + winter 命中 2");
  assert.ok(winterPool.some((verse) => verse.id === 105), "①未标注句在池");
  assert.ok(winterPool.every((verse) => verse.id !== 109 && verse.id !== 103), "①夏句/春句不在池");
  assert.equal(dom.verseText.textContent, expected(["winter"], "2027-01-15", 2), "①季节命中按「未标注 ∪ winter」池取句");
  assert.ok(winterPool.some((verse) => stripTrailingPeriod(verse.text) === dom.verseText.textContent), "①选句确属池");

  /* ④ 点击回绕周期 = 池长：逐击断言 offset 取模回绕，池长击后回基线句 */
  const base = deterministicVerseIndex(winterPool.length, "2027-01-15", 2);
  for (let step = 1; step <= winterPool.length; step += 1) {
    dom.verse.click();
    runTimeouts(); /* 相位 1 */
    runTimeouts(); /* 相位 2 */
    assert.equal(dom.verseText.textContent,
      stripTrailingPeriod(winterPool[(base + step) % winterPool.length].text),
      `④第 ${step} 击 = 池内 offset 回绕取句`);
  }
  assert.equal(dom.verseText.textContent, expected(["winter"], "2027-01-15", 2), "④池长击后回绕到基线句");

  /* ⑤ 同档稳定：无课（leisure）心境变化不重算已选句（epoch 冻结；若重算池会变） */
  const stableText = dom.verseText.textContent;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", {
    detail: { days: [{ date: "2027-01-15", meetings: [] }] },
  }));
  assert.equal(dom.verseText.textContent, stableText, "⑤同档心境变化不换句");
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.equal(dom.verseText.textContent, stableText, "⑤同档 visibility 重入不换句");

  /* ③ 无课 → leisure 并池（跨档后重算：未标注 ∪ winter ∪ leisure） */
  clock = new Date(2027, 0, 15, 14, 0); /* 同日午后档（无时段标签） */
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.ok(dom.greeting.textContent.startsWith("周五"), "③跨档问候重渲染");
  const leisurePool = poolFor(["winter", "leisure"]);
  assert.equal(leisurePool.length, 4, "③池长 = 未标注 1 + winter 2 + leisure 1");
  assert.equal(dom.verseText.textContent, expected(["winter", "leisure"], "2027-01-15", 4), "③无课→leisure 并池重取");
  assert.ok(leisurePool.some((verse) => stripTrailingPeriod(verse.text) === dom.verseText.textContent), "③选句确属并池");

  /* ② 节令命中：2026-09-25 中秋 → 池 = 节令句 + 全部未标注 */
  clock = new Date(2026, 8, 25, 10, 0);
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", { detail: { days: [] } }));
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  const festivalPool = poolFor(["mid-autumn", "autumn"]);
  assert.equal(festivalPool.length, 2, "②中秋池 = 节令命中 1 + 未标注 1");
  assert.ok(festivalPool.some((verse) => verse.id === 106), "②节令句在池");
  assert.ok(festivalPool.every((verse) => !verse.tags.length || verse.tags.includes("mid-autumn")), "②池内标注句仅节令命中");
  assert.equal(dom.verseText.textContent, expected(["mid-autumn", "autumn"], "2026-09-25", 2), "②节令命中按新池取句");
  /* ② 表外年份 2046：无任何标注命中 → 池 = 仅未标注句（回退分支安全网不可达） */
  clock = new Date(2046, 8, 25, 10, 0);
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.equal(poolFor(["autumn"]).length, 1, "②表外年份池 = 仅未标注句");
  assert.equal(dom.verseText.textContent, stripTrailingPeriod(TAGGED[4].text), "②无命中语境选中未标注句");

  cleanup();
  restoreFetch();
  console.log("tagged candidate selection (pool composition) passed");
}

{
  /* ⑦ 全无标签语料 → 池 = 全量（未标注句恒在池，池不缩）：summer 激活但
     语料零标签，基线轮换按全池 5 条取句 */
  const dom = buildDom();
  const { windowTarget } = buildWindow();
  const restoreFetch = stubFetch({ verses: VERSES });
  const clock = new Date(2027, 6, 10, 10, 0); /* 7 月 → summer 激活但池内无任何标签 */
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 0, swapCooldownMs: 0 });
  await settle();
  const index = deterministicVerseIndex(VERSES.length, "2027-07-10", 2);
  assert.equal(dom.verseText.textContent, stripTrailingPeriod(VERSES[index].text), "⑦按全池 5 条基线取句");
  assert.equal(VERSES[index].tags.length, 0, "⑦选中句无标签（池=全量）");
  cleanup();
  restoreFetch();
  console.log("untagged baseline (pool = full set) passed");
}

{
  /* ⑧ 混合语料可达性钉（纯公式枚举，池镜像等价性已由上方装配组逐点钉死）：
     未标注句在两个互斥季节语境下均可被确定性索引选中——「未打标签的诗句也
     应该正常随机出现，并且和打标签的诗句出现的概率不应有区别」。 */
  const MIXED = [
    { id: 201, text: "接天莲叶无穷碧，映日荷花别样红。", tags: ["summer"] },
    { id: 202, text: "孤舟蓑笠翁，独钓寒江雪。", tags: ["winter"] },
    { id: 203, text: "纸上得来终觉浅，绝知此事要躬行。", tags: [] },
  ];
  const mixedPool = (tag) => MIXED.filter((verse) => !verse.tags.length || verse.tags.includes(tag));
  assert.equal(mixedPool("summer").length, 2, "⑧summer 池 = 夏句 + 未标注");
  assert.equal(mixedPool("winter").length, 2, "⑧winter 池 = 冬句 + 未标注");
  const reached = { summer: new Set(), winter: new Set() };
  for (let day = 0; day < 365; day += 1) {
    const probe = new Date(2027, 0, 1);
    probe.setDate(probe.getDate() + day);
    const key = dateKeyFor(probe);
    for (let band = 0; band < 7; band += 1) {
      for (const tag of ["summer", "winter"]) {
        const pool = mixedPool(tag);
        reached[tag].add(pool[deterministicVerseIndex(pool.length, key, band)].id);
      }
    }
  }
  assert.ok(reached.summer.has(203), "⑧summer 语境确定性索引取到未标注句");
  assert.ok(reached.winter.has(203), "⑧winter 语境确定性索引取到未标注句");
  assert.ok(reached.summer.has(201), "⑧标注句在匹配语境（summer）可达");
  assert.ok(reached.winter.has(202), "⑧标注句在匹配语境（winter）可达");
  console.log("mixed-fixture reachability passed");
}

{
  /* 午夜边界钉（桩时钟）：日期键轮换 + 深夜档课量语义，跨午夜确定性 */
  const dom = buildDom();
  const { windowTarget } = buildWindow();
  const restoreFetch = stubFetch({ verses: VERSES });
  let clock = new Date(2026, 8, 16, 23, 59); /* 周三 23:59 夜晚档 */
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 0, swapCooldownMs: 0 });
  await settle();
  const lectures = [
    { start_time: "15:00", end_time: "15:45" }, { start_time: "16:00", end_time: "16:45" },
    { start_time: "17:00", end_time: "17:45" }, { start_time: "18:00", end_time: "18:45" },
    { start_time: "19:00", end_time: "19:45" },
  ];
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", {
    detail: { days: [{ date: dateKeyFor(clock), meetings: lectures }] },
  }));
  assert.ok(dom.greeting.textContent.startsWith("周三晚上好"), "午夜前夜晚档问候");
  assert.ok(dom.greeting.textContent.includes("今日课毕"), "午夜前叠加课毕尾句");
  clock = new Date(2026, 8, 17, 0, 0); /* 恰在午夜边界 */
  dom.documentTarget.dispatchEvent(new Event("visibilitychange"));
  assert.equal(dom.greeting.textContent, "周四，夜深了", "午夜后轮换深夜档且不叠课量（显示层剥尾句号）");
  cleanup();
  restoreFetch();
  console.log("midnight boundary passed");
}

{
  /* AUTOLOGIN-LOCAL-FIRST-1①：本地优先尾句键闭集映射 + 快照缺位回退 +
     快照在场精确覆盖（纯函数组） */
  assert.equal(tailTextForKey("none"), "今日无课。");
  assert.equal(tailTextForKey("done"), "今日课毕。");
  assert.equal(tailTextForKey("done-long"), "今日课毕，辛苦了。");
  assert.equal(tailTextForKey("full"), "今日课满。");
  assert.equal(tailTextForKey("full-half"), "今日课满，已过半。");
  assert.equal(tailTextForKey("no-data"), null, "无数据键不出尾句");
  assert.equal(tailTextForKey("unknown-key"), null, "闭集外键不出尾句");
  assert.equal(tailTextForKey(undefined), null);
  assert.equal(tailTextForKey(null), null);

  const wednesday = new Date(2026, 8, 16, 14, 0); /* 周三 14:00 午后档 */
  assert.equal(composeGreeting(wednesday, null, "今日无课。"), "周三下午好，今日无课。", "快照缺位用本地尾句");
  assert.equal(composeGreeting(wednesday, null, "今日课满。"), "周三下午好，今日课满。", "本地尾句键入文");
  assert.equal(composeGreeting(wednesday, null), "周三下午好。", "无本地尾句退基础问候");
  assert.equal(composeGreeting(wednesday, [], "今日课满。"), "周三下午好，今日无课。", "快照在场即精确覆盖本地尾句");
  assert.equal(
    composeGreeting(wednesday, [{ start_time: "08:00", end_time: "08:45" }], "今日课满。"),
    "周三下午好，今日课毕。",
    "快照课量优先于本地尾句",
  );
  const deepNightFallback = new Date(2026, 8, 16, 2, 0); /* 周三 02:00 深夜档 */
  assert.equal(composeGreeting(deepNightFallback, null, "今日课满。"), "周三，夜深了。", "深夜档不叠加本地尾句");
  console.log("local-first tail key mapping passed");
}

{
  /* AUTOLOGIN-LOCAL-FIRST-1① 装配组：boot 拉取 greeting-context 闭集键立即
     渲染本地尾句；真实快照到达精确覆盖；快照缺 today 行时本地尾句退役，
     诚实退回基础问候（绝不复活）。 */
  const dom = buildDom();
  const { windowTarget } = buildWindow();
  const originalFetch = globalThis.fetch;
  const clock = new Date(2026, 8, 16, 14, 0); /* 周三 14:00 */
  const responses = new Map([
    ["/api/v3/greeting-context", { ok: true, json: async () => ({ tail_key: "none" }) }],
    ["/data/verses.json", { ok: true, json: async () => ({ verses: VERSES }) }],
  ]);
  const requested = [];
  globalThis.fetch = async (url) => {
    requested.push(String(url));
    return responses.get(String(url)) || { ok: false, json: async () => ({}) };
  };
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  assert.equal(dom.greeting.textContent, "周三下午好", "装配即基础问候（本地尾句未到达）");
  await settle();
  assert.ok(requested.includes("/api/v3/greeting-context"), "boot 拉取本地尾句端点");
  assert.equal(dom.greeting.textContent, "周三下午好，今日无课", "本地尾句立即渲染（显示层剥句号）");

  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", {
    detail: { days: [{ date: dateKeyFor(clock), meetings: [
      { start_time: "08:00", end_time: "08:45" },
      { start_time: "09:00", end_time: "09:45" },
    ] }] },
  }));
  assert.equal(dom.greeting.textContent, "周三下午好，今日课毕", "快照在场即真实课量覆盖");

  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", {
    detail: { days: [] },
  }));
  assert.equal(dom.greeting.textContent, "周三下午好", "快照缺 today 行：本地尾句已退役不复活");
  cleanup();
  globalThis.fetch = originalFetch;
  console.log("boot local-first tail passed");
}

{
  /* AUTOLOGIN-LOCAL-FIRST-1① 装配组：端点失败/无数据键=诚实基础问候 */
  const dom = buildDom();
  buildWindow();
  const originalFetch = globalThis.fetch;
  const clock = new Date(2026, 8, 16, 14, 0);
  globalThis.fetch = async (url) => (String(url).includes("greeting-context")
    ? { ok: false, json: async () => ({}) }
    : { ok: true, json: async () => ({ verses: VERSES }) });
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  await settle();
  assert.equal(dom.greeting.textContent, "周三下午好", "端点失败保持基础问候");
  cleanup();
  globalThis.fetch = originalFetch;

  const domB = buildDom();
  buildWindow();
  globalThis.fetch = async (url) => (String(url).includes("greeting-context")
    ? { ok: true, json: async () => ({ tail_key: "no-data" }) }
    : { ok: true, json: async () => ({ verses: VERSES }) });
  const cleanupB = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  await settle();
  assert.equal(domB.greeting.textContent, "周三下午好", "无数据键保持基础问候");
  cleanupB();
  globalThis.fetch = originalFetch;
  console.log("boot local-first tail fallback passed");
}

{
  /* AUTOLOGIN-LOCAL-FIRST-1② 装配组：settings 快照 auto_connect 驱动恢复期
     按钮文案——开=checking 出主文案；关=RESUMING；degraded 恒重登 */
  const dom = buildDom();
  buildWindow();
  const originalFetch = globalThis.fetch;
  const clock = new Date(2026, 8, 16, 14, 0);
  const settingsPayload = (enabled, status) => ({
    ok: true,
    json: async () => ({ schema: "courselens.api.v3", data: { auto_connect: { fudan: { enabled, status } } } }),
  });
  const greetingContextOk = { ok: true, json: async () => ({ tail_key: "no-data" }) };
  const versesOk = { ok: true, json: async () => ({ verses: VERSES }) };
  globalThis.fetch = async (url) => (String(url).includes("greeting-context")
    ? greetingContextOk
    : String(url).includes("settings") ? settingsPayload(true, "ready") : versesOk);
  store.set("auth", { state: "checking" });
  const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  assert.equal(dom.button.textContent, "登录后选择课程", "⑬c：偏好未到达前也不占主按钮");
  await settle();
  assert.equal(dom.button.textContent, "选择课程", "自动登录开：恢复期出常规主文案（按钮可点）");
  store.set("auth", { state: "degraded" });
  assert.equal(dom.button.textContent, "重新登录并选择课程", "恢复失败 degraded 恒提示重登");
  store.set("auth", { state: "checking" });
  assert.equal(dom.button.textContent, "选择课程", "degraded 回到 checking 仍保持主文案");
  cleanup();

  const domOff = buildDom();
  buildWindow();
  globalThis.fetch = async (url) => (String(url).includes("greeting-context")
    ? greetingContextOk
    : String(url).includes("settings") ? settingsPayload(false, "off") : versesOk);
  store.set("auth", { state: "checking" });
  const cleanupOff = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
  await settle();
  assert.equal(domOff.button.textContent, "登录后选择课程", "⑬c：自动登录关同样给登录主文案");
  cleanupOff();
  store.set("auth", null);
  globalThis.fetch = originalFetch;
  console.log("resume de-emphasis assembly passed");
}

/* ================= U3 组：尾句快照先行+无感换新 ================= */

{
  const buildStorage = () => {
    const map = new Map();
    return {
      getItem: (key) => (map.has(key) ? map.get(key) : null),
      setItem: (key, value) => map.set(key, String(value)),
      removeItem: (key) => map.delete(key),
      _map: map,
    };
  };

  /* ① 同日种子：安装同步出全句（≤300ms 目标），新鲜值到达后原位换新+写回 */
  {
    const dom = buildDom();
    const { windowTarget } = buildWindow();
    const storage = buildStorage();
    windowTarget.localStorage = storage;
    const clock = new Date(2026, 8, 16, 14, 0); /* 周三 14:00 午后档 */
    storage.setItem(
      "courselens:greeting-tail",
      JSON.stringify({ date: dateKeyFor(clock), tail: "done" }),
    );
    const originalFetch = globalThis.fetch;
    /* greeting-context 最先出站且返回与种子不同的新鲜值（done → full） */
    let contextFirst = null;
    globalThis.fetch = async (url, options) => {
      if (contextFirst === null) contextFirst = String(url).includes("greeting-context");
      return { ok: true, json: async () => (String(url).includes("greeting-context") ? { tail_key: "full" } : { verses: VERSES }) };
    };
    try {
      const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
      assert.equal(contextFirst, true, "greeting-context 在启动序首位出站");
      assert.equal(dom.greeting.textContent, "周三下午好，今日课毕", "种子先行：安装同步出全句，不等网络");
      await settle();
      assert.equal(dom.greeting.textContent, "周三下午好，今日课满", "新鲜值原位换新（无感，同节点）");
      const stored = JSON.parse(storage.getItem("courselens:greeting-tail"));
      assert.equal(stored.tail, "full", "新鲜尾键写回本地快照");
      assert.equal(stored.date, dateKeyFor(clock), "快照带本地日期");
      cleanup();
    } finally {
      globalThis.fetch = originalFetch;
    }
  }

  /* ② 跨日不种子：昨日尾键无效，等新鲜值；同节点的 no-data 不落快照 */
  {
    const dom = buildDom();
    const { windowTarget } = buildWindow();
    const storage = buildStorage();
    windowTarget.localStorage = storage;
    const clock = new Date(2026, 8, 16, 14, 0);
    storage.setItem(
      "courselens:greeting-tail",
      JSON.stringify({ date: "2026-09-15", tail: "done" }),
    );
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url) => ({ ok: true, json: async () => (String(url).includes("greeting-context") ? { tail_key: "no-data" } : { verses: VERSES }) });
    try {
      const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
      assert.equal(dom.greeting.textContent, "周三下午好", "跨日种子无效：先出基础问候");
      await settle();
      assert.equal(dom.greeting.textContent, "周三下午好", "no-data 诚实退回基础问候");
      assert.equal(storage.getItem("courselens:greeting-tail"), null, "no-data 不写快照");
      cleanup();
    } finally {
      globalThis.fetch = originalFetch;
    }
  }

  /* ③ 深夜档不种子（「今日」指代歧义守卫与 courseLoadTail 同一），坏 JSON 静默 */
  {
    const dom = buildDom();
    const { windowTarget } = buildWindow();
    const storage = buildStorage();
    windowTarget.localStorage = storage;
    const clock = new Date(2026, 8, 16, 2, 0); /* 深夜档 */
    storage.setItem(
      "courselens:greeting-tail",
      JSON.stringify({ date: dateKeyFor(clock), tail: "done" }),
    );
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => { throw new TypeError("network unavailable"); };
    try {
      const cleanup = installGreeting(store, { clockNow: () => clock, swapPhaseMs: 20, swapCooldownMs: 5000 });
      assert.equal(dom.greeting.textContent, "周三，夜深了", "深夜档不叠加种子尾句");
      await settle();
      assert.equal(dom.greeting.textContent, "周三，夜深了", "网络失败仍守深夜档");
      cleanup();
    } finally {
      globalThis.fetch = originalFetch;
    }
    storage.setItem("courselens:greeting-tail", "{broken json");
    const { windowTarget: w2 } = buildWindow();
    w2.localStorage = storage;
    const clock2 = new Date(2026, 8, 16, 14, 0);
    const cleanup2 = installGreeting(store, { clockNow: () => clock2, swapPhaseMs: 20, swapCooldownMs: 5000 });
    await settle();
    assert.equal(byId["empty-greeting"].textContent, "周三下午好", "坏 JSON 静默退基础问候");
    cleanup2();
  }

  console.log("U3 tail snapshot-first passed");
}

console.log("frontend_greeting_behavior: all scenarios passed");
