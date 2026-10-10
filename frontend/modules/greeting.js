/* 学习空态问候/诗联（视觉 Stage 2 批 4；D1-design v2 + 视觉合同 §9.3）。
   课量尾句两级来源：boot 拉取本地缓存闭集键 /api/v3/greeting-context
   （AUTOLOGIN-LOCAL-FIRST-1①，恢复期本地优先）+ 会话就绪后既有
   courselens:timetable-snapshot 广播精确覆盖（无快照/异常时诚实退回基础
   问候）；时段/星期/节日取客户端本地时钟，
   诗联池来自本地 frontend/data/verses.json（chinese-poetry © JackeyGao, MIT）。
   本模块由外置装载器 greeting-boot.js 以 installGreeting(store) 装配
   （产品 CSP script-src 'self' 禁内联）；mjs 行为测试只导入纯函数。
   VT-1 情境标签：节令/季节/时段/心境 → 激活标签 → 候选集（未标注句恒在
   池 ∪ 标注命中句；标注句仍按语境门控），确定性轮换基式不变
   （hash(日期, 时段档) mod 候选集长度）。 */

import { store } from "./store.js";

const WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

/* 节日小集（公历 MM-DD；D1 §2.1，总控裁决①维持三元）。命中时整句替换，
   不再叠加课量尾句。 */
const HOLIDAY_GREETINGS = new Map([
  ["01-01", "元旦快乐。"],
  ["05-04", "青年节快乐。"],
  ["09-10", "教师节快乐。"],
]);

/* 七档时段（分钟区间 [from, to)）。深夜档不叠加课量尾句（凌晨「今日」指代歧义）。 */
const TIME_BANDS = [
  { from: 0, to: 300, suffix: "，夜深了。", deepNight: true },
  { from: 300, to: 540, suffix: "清晨好。" },
  { from: 540, to: 690, suffix: "上午好。" },
  { from: 690, to: 810, suffix: "中午好。" },
  { from: 810, to: 1050, suffix: "下午好。" },
  { from: 1050, to: 1170, suffix: "傍晚好。" },
  { from: 1170, to: 1440, suffix: "晚上好。" },
];

/* 主按钮三态+degraded 扩展（D1-design v2 三态 + 总控裁决②措辞）。
   AUTOLOGIN-LOCAL-FIRST-1② + FA1⑬c：checking 恒不占主按钮十秒——自动登录
   恢复期给常规主文案；未启用自动登录给登录主文案（恢复状态由顶栏连接
   状态簇轻提示承载，BUTTON_TEXT_RESUMING 通道保留不删）。degraded 恒提示
   重新登录。 */
export const BUTTON_TEXT_READY = "选择课程";
export const BUTTON_TEXT_LOGIN = "登录后选择课程";
export const BUTTON_TEXT_RESUMING = "正在恢复会话…";
export const BUTTON_TEXT_DEGRADED = "重新登录并选择课程";

export function buttonTextFor(authState, { autoLoginResume = false } = {}) {
  if (authState === "ready") return BUTTON_TEXT_READY;
  if (authState === "checking") return autoLoginResume ? BUTTON_TEXT_READY : BUTTON_TEXT_LOGIN;
  if (authState === "degraded") return BUTTON_TEXT_DEGRADED;
  return BUTTON_TEXT_LOGIN;
}

export function bandIndexFor(minutesOfDay) {
  const minutes = ((Number(minutesOfDay) % 1440) + 1440) % 1440;
  for (let index = 0; index < TIME_BANDS.length; index += 1) {
    if (minutes >= TIME_BANDS[index].from && minutes < TIME_BANDS[index].to) return index;
  }
  return 0;
}

export function weekdayName(date) {
  return WEEKDAY_NAMES[(date.getDay() + 6) % 7];
}

export function holidayGreeting(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return HOLIDAY_GREETINGS.get(`${month}-${day}`) || null;
}

const TIME_PATTERN = /^(\d{1,2}):(\d{2})$/;

function timeToMinutes(value) {
  const match = TIME_PATTERN.exec(String(value || ""));
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return Number.isFinite(minutes) && minutes >= 0 && minutes < 1440 ? minutes : null;
}

/* 课量语义尾句（D1 §2.2，轻文言零数字）。meetings 为今日 meetings[]
   （{start_time, end_time}）：null = 快照缺位（退回基础问候），[] = 快照在而
   今日 0 节（「今日无课。」）；条目时间全坏视为异常退回。深夜档不叠加。 */
export function courseLoadTail(meetings, now) {
  if (!Array.isArray(meetings)) return null;
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  if (TIME_BANDS[bandIndexFor(nowMinutes)].deepNight) return null;
  const marks = [];
  for (const meeting of meetings) {
    if (!meeting || typeof meeting !== "object") continue;
    const end = timeToMinutes(meeting.end_time);
    if (end === null) continue;
    marks.push(end);
  }
  if (!marks.length) return meetings.length === 0 ? "今日无课。" : null;
  const total = marks.length;
  const lastEnd = Math.max(...marks);
  if (nowMinutes > lastEnd) return total >= 5 ? "今日课毕，辛苦了。" : "今日课毕。";
  const finished = marks.filter((end) => end < nowMinutes).length;
  if (total <= 4) return null; /* 1–4 节未开始/进行中：诚实留白 */
  if (finished > total / 2) return "今日课满，已过半。";
  return "今日课满。";
}

/* 节日整句替换；其余 = 星期 + 时段 + 可选课量尾句。fallbackTail 仅在快照
   缺位时生效（AUTOLOGIN-LOCAL-FIRST-1①：boot 本地优先尾句，真实快照到达
   后被精确覆盖）；深夜档不叠加任何尾句。 */
export function composeGreeting(date, todayMeetings, fallbackTail = null) {
  const holiday = holidayGreeting(date);
  if (holiday) return holiday;
  const minutes = date.getHours() * 60 + date.getMinutes();
  const band = TIME_BANDS[bandIndexFor(minutes)];
  const base = `${weekdayName(date)}${band.suffix}`;
  let tail = null;
  try {
    tail = courseLoadTail(todayMeetings, date);
  } catch {
    tail = null;
  }
  if (!tail && todayMeetings === null && !band.deepNight) tail = String(fallbackTail || "") || null;
  if (!tail) return base;
  return `${base.slice(0, -1)}，${tail}`;
}

/* 课量尾句闭集键 → 尾句文本（与 src/runtime/http_api.py _GREETING_TAIL_KEYS
   同源闭集）。未知键/无数据一律 null——退回基础问候，绝不自造文案。 */
const TAIL_TEXT_BY_KEY = new Map([
  ["none", "今日无课。"],
  ["done", "今日课毕。"],
  ["done-long", "今日课毕，辛苦了。"],
  ["full", "今日课满。"],
  ["full-half", "今日课满，已过半。"],
]);

export function tailTextForKey(key) {
  return TAIL_TEXT_BY_KEY.get(String(key ?? "")) || null;
}

/* U3（FA1⑬ 同款「快照先行+无感换新」）：把上一会话的闭集尾键缓存在浏览器
   本地，冷启动同日先出全句，greeting-context 新鲜值到达后原位换新。
   缓存只有闭集枚举键+本地日期（零课程名/ID/数字/个人数据，属纯视觉偏好
   的 localStorage 通道）；跨日不种子，深夜档不种子（「今日」指代歧义，
   与 courseLoadTail 同一守卫）。键内含当日时刻语义，会漂移——种子只求
   ≤300ms 出全句，新鲜值始终随后纠正，绝不阻止精确覆盖。 */
const TAIL_SNAPSHOT_KEY = "courselens:greeting-tail";

function readTailSnapshot(now) {
  try {
    const raw = window.localStorage?.getItem(TAIL_SNAPSHOT_KEY);
    if (!raw) return null;
    let value;
    try {
      value = JSON.parse(raw);
    } catch {
      window.localStorage?.removeItem(TAIL_SNAPSHOT_KEY);
      return null;
    }
    if (!value || value.date !== dateKeyFor(now)) {
      /* 跨日陈旧快照自愈清除，避免过期尾键常驻本地 */
      window.localStorage?.removeItem(TAIL_SNAPSHOT_KEY);
      return null;
    }
    if (TIME_BANDS[bandIndexFor(now.getHours() * 60 + now.getMinutes())].deepNight) return null;
    return tailTextForKey(value.tail);
  } catch {
    return null;
  }
}

function writeTailSnapshot(now, key) {
  const tail = String(key ?? "");
  if (!tailTextForKey(tail) || tail === "no-data") return;
  try {
    window.localStorage?.setItem(
      TAIL_SNAPSHOT_KEY,
      JSON.stringify({ date: dateKeyFor(now), tail }),
    );
  } catch {
    /* 存储不可得（隐私模式/配额）：静默跳过，只慢一个冷启动 */
  }
}

/* 确定性轮换（D1 §4）：index = hash(本地日期, 时段档) mod 池大小；
   同日同档稳定，跨档/跨日换句。 */
export function fnv1a(text) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

export function dateKeyFor(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

export function deterministicVerseIndex(poolSize, dateKey, band) {
  if (!poolSize) return 0;
  return fnv1a(`${dateKey}|${band}`) % poolSize;
}

export function formatSource(verse) {
  const author = String(verse?.author || "").trim();
  const work = String(verse?.work || "").trim();
  if (author && work) return `${author} · ${work}`;
  return author || work || "";
}

/* 视觉②（GREETING-EMPTY-STATE-FIX-1）：显示层剥结尾全角句号。纯函数只动
   结尾「。」（含连续），问号/感叹号与句中逗号、分号一律保留；语料与
   composeGreeting 原文零改动，仅在 textContent 写入时套用。 */
export function stripTrailingPeriod(text) {
  return String(text ?? "").replace(/。+$/, "");
}

/* ===== 情境标签（VT-1 总控裁决；tags 闭集见 verses.json 标注）===== */

/* 季节按月粗分：3–5 春 / 6–8 夏 / 9–11 秋 / 12–2 冬。 */
export function seasonTagFor(date) {
  const month = date.getMonth() + 1;
  if (month >= 3 && month <= 5) return "spring";
  if (month >= 6 && month <= 8) return "summer";
  if (month >= 9 && month <= 11) return "autumn";
  return "winter";
}

/* 节令：清明/冬至用固定公历窗口（±1 天，零依赖）；其余农历节令用内置
   2026–2035 十年公历日期表（纯数据）。表外年份返回 null，退回季节匹配。 */
const SOLAR_TERM_WINDOWS = [
  ["qingming", 4, 4, 6],   /* 清明（4/5 ±1 天） */
  ["dongzhi", 12, 21, 23], /* 冬至（12/22 ±1 天） */
];
const SOLAR_TERM_DATES = {
  /* [month, day]，按节令所在公历年份归位（除夕归公历当年）。 */
  "new-year": { 2026: [2, 17], 2027: [2, 6], 2028: [1, 26], 2029: [2, 13], 2030: [2, 3], 2031: [1, 23], 2032: [2, 11], 2033: [1, 31], 2034: [2, 19], 2035: [2, 8] },
  "lantern": { 2026: [3, 3], 2027: [2, 20], 2028: [2, 9], 2029: [2, 27], 2030: [2, 17], 2031: [2, 6], 2032: [2, 25], 2033: [2, 14], 2034: [3, 5], 2035: [2, 22] },
  "duanwu": { 2026: [6, 19], 2027: [6, 9], 2028: [5, 28], 2029: [6, 16], 2030: [6, 5], 2031: [6, 24], 2032: [6, 12], 2033: [6, 1], 2034: [6, 20], 2035: [6, 10] },
  "qixi": { 2026: [8, 19], 2027: [8, 8], 2028: [8, 26], 2029: [8, 16], 2030: [8, 5], 2031: [8, 24], 2032: [8, 12], 2033: [8, 1], 2034: [8, 20], 2035: [8, 10] },
  "mid-autumn": { 2026: [9, 25], 2027: [9, 15], 2028: [10, 3], 2029: [9, 22], 2030: [9, 12], 2031: [10, 1], 2032: [9, 19], 2033: [9, 8], 2034: [9, 27], 2035: [9, 16] },
  "chongyang": { 2026: [10, 18], 2027: [10, 8], 2028: [10, 26], 2029: [10, 16], 2030: [10, 5], 2031: [10, 24], 2032: [10, 12], 2033: [10, 1], 2034: [10, 20], 2035: [10, 9] },
  "newyear-eve": { 2026: [2, 16], 2027: [2, 5], 2028: [1, 25], 2029: [2, 12], 2030: [2, 2], 2031: [1, 22], 2032: [2, 10], 2033: [1, 30], 2034: [2, 18], 2035: [2, 7] },
};

export function solarTermTagFor(date) {
  const month = date.getMonth() + 1;
  const day = date.getDate();
  for (const [tag, termMonth, from, to] of SOLAR_TERM_WINDOWS) {
    if (month === termMonth && day >= from && day <= to) return tag;
  }
  const year = String(date.getFullYear());
  for (const [tag, dates] of Object.entries(SOLAR_TERM_DATES)) {
    const md = dates[year];
    if (md && md[0] === month && md[1] === day) return tag;
  }
  return null;
}

/* 时段：七档 → 时段标签（清晨→dawn、夜晚→night、深夜→late-night；其余档无）。 */
const BAND_CONTEXT_TAGS = { 0: "late-night", 1: "dawn", 6: "night" };

/* 心境：无课→leisure；课满/课毕→diligent；课满已过半→exam-prep；深夜档→
   exam-prep（惜时/夜读）；快照缺位或时间全坏→无心境标签。与 courseLoadTail
   同一判定骨架。 */
export function courseMoodTag(meetings, now) {
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  if (TIME_BANDS[bandIndexFor(nowMinutes)].deepNight) return "exam-prep";
  if (!Array.isArray(meetings)) return null;
  const marks = [];
  for (const meeting of meetings) {
    if (!meeting || typeof meeting !== "object") continue;
    const end = timeToMinutes(meeting.end_time);
    if (end === null) continue;
    marks.push(end);
  }
  if (!marks.length) return meetings.length === 0 ? "leisure" : null;
  const total = marks.length;
  const lastEnd = Math.max(...marks);
  if (nowMinutes > lastEnd) return "diligent";
  if (total <= 4) return "diligent";
  const finished = marks.filter((end) => end < nowMinutes).length;
  return finished > total / 2 ? "exam-prep" : "diligent";
}

/* 换句交互（D1 §4 防消耗）：会话内 offset+1 不持久化；两相各 ~700ms 的
   opacity/transform 慢动画（合计 1.4s ∈ [1.2, 1.6]s），动画期 pointer-events:none，
   收尾再留 ~200ms 冷却——冷却独立于动画时长存在，reduced-motion 下仍生效。 */
const SWAP_PHASE_MS = 700;
const SWAP_COOLDOWN_MS = 200;

export function installGreeting(store, options = {}) {
  const greetingEl = document.getElementById("empty-greeting");
  const verseEl = document.getElementById("empty-verse");
  const verseTextEl = verseEl?.querySelector(".empty-verse-text") || null;
  const verseSourceEl = document.getElementById("empty-verse-source");
  const buttonEl = document.getElementById("study-start-select");
  if (!greetingEl || !verseEl || !verseTextEl || !verseSourceEl || !buttonEl) return () => {};

  const swapPhaseMs = Number(options.swapPhaseMs) >= 0 ? Number(options.swapPhaseMs) : SWAP_PHASE_MS;
  /* 测试注入点：默认真实本地时钟（跨档/跨日检测的唯一时间源）。 */
  const clockNow = typeof options.clockNow === "function" ? options.clockNow : (() => new Date());
  const swapCooldownMs = Number(options.swapCooldownMs) >= 0 ? Number(options.swapCooldownMs) : SWAP_COOLDOWN_MS;
  /* 乙-3（#6）：首用淡出——第 fadeStarts 次启动之后收起问候行；
     boot 侧计数（greeting-boot.js），未传 startCount 恒按首用呈现。 */
  const fadeStarts = Number(options.fadeStarts) > 0 ? Number(options.fadeStarts) : 5;
  const startCount = Number(options.startCount) || 0;
  if (startCount > fadeStarts) greetingEl.hidden = true;

  let verses = [];
  let verseOffset = 0;
  let cooldownUntil = 0;
  let swapping = false;
  let todayMeetings = null;
  /* 本地优先课量尾句（AUTOLOGIN-LOCAL-FIRST-1①）：boot 拉取闭集键立即渲染；
     失败/无数据保持基础问候。首个真实快照事件到达即退役本值（精确覆盖）。 */
  let bootTailText = null;
  /* 自动登录启用（AUTOLOGIN-LOCAL-FIRST-1②）：boot 读既有 settings 快照的
     auto_connect 闭集状态（零新端点、零形状变化）；不可得时维持现状文案。 */
  let autoLoginResume = false;
  let lastGreetingText = greetingEl.textContent || "";
  let lastButtonText = "";
  let lastVerseSignature = "";

  const nowMinutes = () => {
    const now = clockNow();
    return now.getHours() * 60 + now.getMinutes();
  };
  const rotationKey = () => `${dateKeyFor(clockNow())}|${bandIndexFor(nowMinutes())}`;

  /* VT-1：上下文 → 激活标签（节令 ∪ 季节 ∪ 时段 ∪ 心境）→ 候选集 =
     未标注句（恒在池，任意语境等概率出现）∪ 标注命中句（仍按语境门控）。
     候选集按 (日期, 时段档) 冻结——心境/快照变化只在跨档、跨日或点击换句时
     参与重算（同档稳定不破）。 */
  let candidatesEpoch = "";
  let activeCandidates = [];
  const contextTags = () => {
    const now = clockNow();
    const tags = new Set();
    const term = solarTermTagFor(now);
    if (term) tags.add(term);
    tags.add(seasonTagFor(now));
    const bandTag = BAND_CONTEXT_TAGS[bandIndexFor(nowMinutes())];
    if (bandTag) tags.add(bandTag);
    const mood = courseMoodTag(todayMeetings, now);
    if (mood) tags.add(mood);
    return tags;
  };
  const ensureCandidates = () => {
    const key = rotationKey();
    if (key === candidatesEpoch && activeCandidates.length) return;
    candidatesEpoch = key;
    const tags = contextTags();
    activeCandidates = [];
    if (tags.size) {
      for (const verse of verses) {
        if (!Array.isArray(verse?.tags) || !verse.tags.length || verse.tags.some((tag) => tags.has(tag))) {
          activeCandidates.push(verse);
        }
      }
    }
    if (!activeCandidates.length) activeCandidates = verses; /* 候选空→全池回退（安全网：未标注恒在池后正常不可达） */
  };

  const renderButton = () => {
    const text = buttonTextFor(store.auth?.state, { autoLoginResume });
    if (text !== lastButtonText) {
      lastButtonText = text;
      buttonEl.textContent = text;
    }
  };

  const renderGreeting = () => {
    const text = composeGreeting(clockNow(), todayMeetings, bootTailText);
    if (text !== lastGreetingText) {
      lastGreetingText = text;
      greetingEl.textContent = stripTrailingPeriod(text);
    }
  };

  const currentVerse = () => {
    if (!verses.length) return null;
    ensureCandidates();
    const [dateKey, band] = rotationKey().split("|");
    const base = deterministicVerseIndex(activeCandidates.length, dateKey, Number(band));
    return activeCandidates[(base + verseOffset) % activeCandidates.length];
  };

  const renderVerse = () => {
    const verse = currentVerse();
    const signature = verse ? `${verse.text}\n${formatSource(verse)}` : "";
    if (signature === lastVerseSignature) return;
    lastVerseSignature = signature;
    verseTextEl.textContent = verse ? stripTrailingPeriod(verse.text) : verseTextEl.textContent;
    verseSourceEl.textContent = verse ? formatSource(verse) : verseSourceEl.textContent;
  };

  /* 装载失败的诚实 UI（GREETING-EMPTY-STATE-FIX-1）：语料缺席时按钮禁用，
     换句提示经 components.css 的 :disabled 规则退场——静态兜底句不可点、
     不暗示可换；装载成功后恢复。 */
  const setVerseAvailable = (available) => {
    verseEl.disabled = !available;
  };

  const loadVerses = async () => {
    try {
      const response = await fetch("/data/verses.json", { credentials: "same-origin" });
      if (!response.ok) return;
      const payload = await response.json();
      const pool = Array.isArray(payload?.verses) ? payload.verses : [];
      if (!pool.length) return;
      verses = pool;
      setVerseAvailable(true);
      renderVerse();
    } catch {
      /* 语料不可得：保留静态兜底诗联，按钮保持禁用，不报错 */
    }
  };

  const handleVerseClick = () => {
    if (swapping || !verses.length || Date.now() < cooldownUntil) return;
    verseOffset += 1;
    candidatesEpoch = ""; /* 点击换句：以当前上下文（含心境）重算候选集 */
    swapping = true;
    verseEl.classList.add("is-swapping");
    window.setTimeout(() => {
      renderVerse();
      verseEl.classList.remove("is-swapping");
      window.setTimeout(() => {
        swapping = false;
        cooldownUntil = Date.now() + swapCooldownMs;
      }, swapPhaseMs);
    }, swapPhaseMs);
  };

  /* 驻留跨档/跨日检测：visibilitychange + 60s 本地时钟（零网络）。仅档位或日期
     变化才重渲染；确定性基线复位会话 offset。 */
  let lastRotationKey = rotationKey();
  const handleClockTick = () => {
    const key = rotationKey();
    if (key === lastRotationKey) return;
    lastRotationKey = key;
    verseOffset = 0;
    renderGreeting();
    renderVerse();
  };
  const handleVisibility = () => {
    if (!document.hidden) handleClockTick();
  };

  const loadGreetingContext = async () => {
    if (todayMeetings !== null) return;
    try {
      const response = await fetch("/api/v3/greeting-context", { credentials: "same-origin", priority: "high" });
      if (!response.ok) return;
      const payload = await response.json();
      const tail = tailTextForKey(payload?.tail_key);
      if (todayMeetings !== null) return;
      writeTailSnapshot(clockNow(), payload?.tail_key);
      if (!tail || tail === bootTailText) return;
      bootTailText = tail;
      renderGreeting();
    } catch {
      /* 端点不可得：保持基础问候，不报错 */
    }
  };

  const loadAutoLoginPreference = async () => {
    try {
      const response = await fetch("/api/v3/settings", { credentials: "same-origin" });
      if (!response.ok) return;
      const payload = await response.json();
      const fudan = payload?.data?.auto_connect?.fudan;
      const enabled = fudan?.enabled === true && fudan?.status === "ready";
      if (enabled === autoLoginResume) return;
      autoLoginResume = enabled;
      renderButton();
    } catch {
      /* 不可得：维持现状文案 */
    }
  };

  const handleTimetableSnapshot = (event) => {
    const value = event?.detail || null;
    const days = Array.isArray(value?.days) ? value.days : [];
    const today = days.find((day) => day?.date === dateKeyFor(clockNow()));
    todayMeetings = today && Array.isArray(today.meetings) ? today.meetings : null;
    bootTailText = null; /* 快照在场后由 meetings 路径精确接管，本地尾句退役 */
    renderGreeting();
  };

  const unsubscribeAuth = store.subscribe("auth", renderButton);
  verseEl.addEventListener("click", handleVerseClick);
  window.addEventListener("courselens:timetable-snapshot", handleTimetableSnapshot);
  document.addEventListener("visibilitychange", handleVisibility);
  const clockTimer = window.setInterval(handleClockTick, 60000);

  setVerseAvailable(false);
  renderButton();
  /* U3 快照先行：种子尾句在首次渲染前入位——冷启动首帧即全句（≤300ms），
     greeting-context 新鲜值到达后原位换新（无感，同一文本节点）。 */
  if (bootTailText === null) {
    const seeded = readTailSnapshot(clockNow());
    if (seeded) bootTailText = seeded;
  }
  renderGreeting();
  renderVerse();
  /* 尾句是唯一可见滞后件：greeting-context 最先出站（启动序+高优先级） */
  void loadGreetingContext();
  void loadVerses();
  void loadAutoLoginPreference();

  return () => {
    unsubscribeAuth();
    verseEl.removeEventListener("click", handleVerseClick);
    window.removeEventListener("courselens:timetable-snapshot", handleTimetableSnapshot);
    document.removeEventListener("visibilitychange", handleVisibility);
    window.clearInterval(clockTimer);
  };
}
