import { apiV3 } from "./api.js";
import { $, clear, textElement } from "./ui.js";
/* N6L S1 U2：闭集文案直接同源 live-state.js；主页直播卡进入动作=跳转
   直播页（第二份 grants/sessions 进入链整体退役，§19.2 还债点）。 */
import {
  LIVE_DISCLOSURE_TEXT,
  LIVE_RECHECK_INTERVAL_MS,
  expirePastMeetingObservation,
  liveCapabilityText,
  liveRecheckDecision,
  liveRetryVisible,
  liveStateDetail,
  mergeLiveObservation,
  reconcileMeetingEndedState,
  schedulePhase,
} from "./live-state.js";

/* 主页概览：Now/Next 课表小组件、主页直播卡、顶栏状态胶囊的纯模型与渲染。
 * 时间选择/中文时间格式/直播目标优先级/胶囊摘要都是可测试纯函数；DOM 渲染只做拼装。 */

export const WEEKDAY_NAMES = Object.freeze(["周一", "周二", "周三", "周四", "周五", "周六", "周日"]);

/* 节次 → 时刻映射（与 src/runtime/timetable.py 的 SLOT_STARTS/SLOT_ENDS 一致；快照自带 slots 时优先用快照） */
export const DEFAULT_SLOTS = Object.freeze([
  { unit: 1, start: "08:00", end: "08:45" },
  { unit: 2, start: "08:55", end: "09:40" },
  { unit: 3, start: "09:55", end: "10:40" },
  { unit: 4, start: "10:50", end: "11:35" },
  { unit: 5, start: "11:45", end: "12:30" },
  { unit: 6, start: "13:30", end: "14:15" },
  { unit: 7, start: "14:25", end: "15:10" },
  { unit: 8, start: "15:25", end: "16:10" },
  { unit: 9, start: "16:20", end: "17:05" },
  { unit: 10, start: "17:15", end: "18:00" },
  { unit: 11, start: "18:30", end: "19:15" },
  { unit: 12, start: "19:25", end: "20:10" },
  { unit: 13, start: "20:20", end: "21:05" },
  { unit: 14, start: "21:15", end: "22:00" },
  { unit: 15, start: "22:10", end: "22:55" },
]);

export const WEEK_SLOT_HEIGHT = 52;

// ---- 基础时间纯函数 ----

export function isoDate(now = new Date()) {
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function minutesOfDay(now = new Date()) {
  return now.getHours() * 60 + now.getMinutes();
}

export function parseHm(text) {
  const [hours, minutes] = String(text || "").split(":").map(Number);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return NaN;
  return hours * 60 + minutes;
}

function dayDiff(fromIso, toIso) {
  const [fy, fm, fd] = String(fromIso).split("-").map(Number);
  const [ty, tm, td] = String(toIso).split("-").map(Number);
  if (![fy, fm, fd, ty, tm, td].every(Number.isFinite)) return NaN;
  return Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86400000);
}

function weekdayOfIso(dateIso) {
  const [year, month, day] = String(dateIso).split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return NaN;
  const native = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return native === 0 ? 7 : native;
}

function shiftIso(dateIso, days) {
  const [year, month, day] = String(dateIso).split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return "";
  const base = new Date(Date.UTC(year, month - 1, day) + days * 86400000);
  return base.toISOString().slice(0, 10);
}

/* 今天 / 明天 / 周三 / 下周一：一周内只用星期名；相对本周推后一周的加“下”前缀；不输出 ISO */
export function friendlyDayLabel(dateIso, now = new Date()) {
  if (!dateIso) return "";
  const today = isoDate(now);
  const diff = dayDiff(today, dateIso);
  if (diff === 0) return "今天";
  if (diff === 1) return "明天";
  const name = WEEKDAY_NAMES[weekdayOfIso(dateIso) - 1];
  if (!name) return "";
  const mondayOfThisWeek = shiftIso(today, -(weekdayOfIso(today) - 1));
  if (dayDiff(mondayOfThisWeek, dateIso) >= 7) return `下周${name.slice(1)}`;
  return name;
}

export function friendlyDateTime(dateIso, hhmm, now = new Date()) {
  const label = friendlyDayLabel(dateIso, now);
  const time = String(hhmm || "").slice(0, 5);
  if (!label && !time) return "";
  return [label, time].filter(Boolean).join(" ");
}

export function friendlyDateRange(startIso, endIso, now = new Date()) {
  const monthText = (dateIso) => {
    const [, month, day] = String(dateIso || "").split("-").map(Number);
    return Number.isFinite(month) && Number.isFinite(day) ? `${month}月${day}日` : "";
  };
  return [monthText(startIso), monthText(endIso)].filter(Boolean).join("–");
}

function humanSpan(minutes) {
  const total = Math.max(0, Math.round(minutes));
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  if (hours && rest) return `${hours} 小时 ${rest} 分钟`;
  if (hours) return `${hours} 小时`;
  return `${rest} 分钟`;
}

// ---- Now/Next 选择 ----

function meetingWindow(meeting) {
  const start = parseHm(meeting?.start_time);
  const end = parseHm(meeting?.end_time);
  return { start, end };
}

/* 优先级：正在上课（本地时间核验过的快照 current_meeting 或今天课表扫描）
 * → 今天稍后 → 今天之后的第一节（明天/本周稍后/下周，来自跨周 next_meeting）。 */
export function selectNowNext(snapshot, now = new Date()) {
  const today = isoDate(now);
  const nowMin = minutesOfDay(now);
  const days = Array.isArray(snapshot?.days) ? snapshot.days : [];
  const todayMeetings = (days.find((day) => day.date === today)?.meetings || [])
    .slice()
    .sort((left, right) => String(left.start_time).localeCompare(String(right.start_time)));

  const backendCurrent = snapshot?.current_meeting;
  const base = (meeting) => ({
    meeting,
    title: String(meeting?.title || "课程"),
    room: String(meeting?.room || ""),
    teachersText: (meeting?.teachers || []).flat().filter(Boolean).join("、"),
  });

  const localNow = todayMeetings.find((meeting) => {
    const { start, end } = meetingWindow(meeting);
    return Number.isFinite(start) && Number.isFinite(end) && start <= nowMin && nowMin < end;
  });
  const backendNow = !localNow && backendCurrent?.date === today
    && (() => {
      const { start, end } = meetingWindow(backendCurrent);
      return Number.isFinite(start) && Number.isFinite(end) && start <= nowMin && nowMin < end;
    })() ? backendCurrent : null;
  const current = localNow || backendNow;
  if (current) {
    const { start, end } = meetingWindow(current);
    const model = { ...base(current), mode: "now", dayLabel: "今天", startHm: current.start_time, endHm: current.end_time };
    model.timeText = `${current.start_time}–${current.end_time}`;
    model.progress = end > start ? Math.min(1, Math.max(0, (nowMin - start) / (end - start))) : 0;
    model.remainingText = `还剩 ${humanSpan(end - nowMin)}`;
    return model;
  }

  const nextToday = todayMeetings.find((meeting) => {
    const { start } = meetingWindow(meeting);
    return Number.isFinite(start) && start > nowMin;
  });
  if (nextToday) {
    const { start } = meetingWindow(nextToday);
    const model = { ...base(nextToday), mode: "next", dayLabel: "今天", startHm: nextToday.start_time, endHm: nextToday.end_time };
    model.timeText = `${friendlyDateTime(today, nextToday.start_time, now)}–${nextToday.end_time}`;
    model.remainingText = `${humanSpan(start - nowMin)}后开始`;
    model.progress = NaN;
    return model;
  }

  const upcoming = snapshot?.next_meeting;
  if (upcoming && upcoming.date && upcoming.date > today) {
    const model = { ...base(upcoming), mode: "later", dayLabel: friendlyDayLabel(upcoming.date, now), startHm: upcoming.start_time, endHm: upcoming.end_time };
    model.timeText = `${model.dayLabel} ${upcoming.start_time}–${upcoming.end_time}`;
    model.remainingText = "";
    model.progress = NaN;
    return model;
  }
  return { mode: "empty", meeting: null, title: "", room: "", teachersText: "", dayLabel: "", startHm: "", endHm: "", timeText: "", remainingText: "", progress: NaN };
}

export function scheduleWidgetAria(model) {
  if (!model || model.mode === "empty" || !model.meeting) return "周课表：今天没有更多安排，点按打开";
  const lead = model.mode === "now" ? "正在上课" : model.mode === "next" ? "今天稍后" : "下一节";
  const parts = [`${lead} ${model.title}`, model.timeText, model.remainingText].filter(Boolean);
  return `周课表：${parts.join("，")}；点按打开`;
}

// ---- 周课表网格模型 ----

export function weekGridModel(value, now = new Date()) {
  const slots = Array.isArray(value?.slots) && value.slots.length ? value.slots : DEFAULT_SLOTS;
  const today = isoDate(now);
  const nowMin = minutesOfDay(now);
  const nextId = String(value?.next_meeting?.meeting_id || "");
  const days = (Array.isArray(value?.days) ? value.days : []).map((day) => {
    const isToday = day.date === today;
    const blocks = [...(day.meetings || [])]
      .sort((left, right) => Number(left.start_unit) - Number(right.start_unit)
        || Number(left.end_unit) - Number(right.end_unit)
        || String(left.title).localeCompare(String(right.title)))
      .map((meeting) => {
        const { start, end } = meetingWindow(meeting);
        return {
          meeting,
          startUnit: Number(meeting.start_unit),
          endUnit: Number(meeting.end_unit),
          lane: Math.max(0, Number(meeting.conflict_lane) || 0),
          laneCount: Math.max(1, Number(meeting.conflict_count) || 1),
          isToday,
          past: isToday && Number.isFinite(end) && end <= nowMin,
          isNow: isToday && Number.isFinite(start) && Number.isFinite(end) && start <= nowMin && nowMin < end,
          isNext: Boolean(nextId) && String(meeting.meeting_id) === nextId,
        };
      });
    return {
      weekday: Number(day.weekday),
      date: String(day.date || ""),
      name: WEEKDAY_NAMES[(Number(day.weekday) - 1) % 7] || "",
      isToday,
      blocks,
    };
  });
  return { slots, days };
}

// ---- 直播目标选择 ----

/* 主页直播卡目标：当前 meeting 的目录课程 > 下一 meeting 的目录课程 > activeCourse 兜底；
 * 只有目录里真实存在（catalog_course_id 命中）的课程才可作目标。 */
export function homeLiveTarget(snapshot, { courses = [], activeCourse = null } = {}, now = new Date()) {
  const byId = (courseId) => {
    const id = String(courseId || "");
    if (!id) return null;
    return courses.find((course) => String(course?.course_id || "") === id) || null;
  };
  const { meeting } = selectNowNext(snapshot, now);
  const fromCurrent = byId(meeting?.catalog_course_id);
  if (fromCurrent) return { course: fromCurrent, meeting, reason: "current" };
  const fromNext = byId(snapshot?.next_meeting?.catalog_course_id);
  if (fromNext) return { course: fromNext, meeting: snapshot?.next_meeting, reason: "next" };
  if (activeCourse?.course_id) return { course: activeCourse, meeting: null, reason: "active" };
  return { course: null, meeting: null, reason: "none" };
}

// ---- 顶栏状态胶囊摘要 ----

const CONN_SHORT = Object.freeze({ ready: "已连接", checking: "确认中", action: "需要处理", error: "连接异常" });

export function statusCapsuleLabel({ fudan = "checking", github = "checking", running = 0, failed = 0 } = {}) {
  const parts = [
    `复旦${CONN_SHORT[fudan] || "确认中"}`,
    `远程${CONN_SHORT[github] || "确认中"}`,
  ];
  parts.push(Number(running) > 0 ? `任务 ${Number(running)} 个进行中` : "无进行中任务");
  if (Number(failed) > 0) parts.push(`失败 ${Number(failed)}`);
  return `状态：${parts.join("；")}`;
}

export function readCapsuleParts(doc) {
  const capsule = doc.getElementById("status-capsule");
  if (!capsule) return null;
  const dots = [...capsule.querySelectorAll("[data-conn-dot]")];
  const stateOf = (node) => node?.getAttribute?.("data-state") || "checking";
  const countNode = doc.getElementById("task-chip-count");
  const failedNode = doc.getElementById("task-chip-failed");
  const running = Number(String(countNode?.textContent || "").trim());
  return {
    fudan: stateOf(dots[0]),
    github: stateOf(dots[1]),
    running: Number.isFinite(running) && running > 0 ? running : 0,
    failed: failedNode && failedNode.getAttribute?.("hidden") !== "true" && failedNode.hidden !== true ? 1 : 0,
  };
}

/* 由 shell.js 的连接渲染与 home-overview 的任务订阅共同调用：读当前 DOM，聚合写回 aria-label */
export function refreshStatusCapsuleLabel(doc = document) {
  const capsule = doc.getElementById?.("status-capsule");
  if (!capsule) return;
  const parts = readCapsuleParts(doc);
  if (parts) capsule.setAttribute("aria-label", statusCapsuleLabel(parts));
}

// ---- 主页小组件渲染 ----

export function renderScheduleNow(target, model) {
  if (!target) return;
  clear(target);
  if (!model || model.mode === "empty" || !model.meeting) {
    target.append(textElement("span", "今天没有更多安排", "sw-empty"));
    return;
  }
  const modeText = model.mode === "now" ? "正在上课" : model.mode === "next" ? "今天稍后" : model.dayLabel || "下一节";
  target.append(textElement("span", modeText, "sw-mode" + (model.mode === "now" ? " sw-live" : "")));
  target.append(textElement("strong", model.title || "课程", "sw-title-line"));
  const meta = [model.room, model.teachersText].filter(Boolean).join(" · ");
  if (meta) target.append(textElement("span", meta, "sw-meta"));
  if (model.timeText) target.append(textElement("span", model.timeText, "sw-time"));
  if (model.mode === "now" && Number.isFinite(model.progress)) {
    const bar = document.createElement("span");
    bar.className = "sw-progress";
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(Math.round(model.progress * 100)));
    bar.setAttribute("aria-label", "本节进度");
    const fill = document.createElement("span");
    fill.className = "sw-progress-fill";
    fill.setAttribute("style", `width: ${Math.round(model.progress * 100)}%`);
    bar.append(fill);
    target.append(bar);
  }
  if (model.remainingText) target.append(textElement("span", model.remainingText, "sw-remaining"));
}

/* 主页直播卡时间线：只述课表事实，绝不写成已确认的直播时间；今天窗口已过
 * （LIVEEXP-1）如实改述「已结束」。SWEEPFIX-2（SWEEP1-01）：状态后缀
 * （「，直播状态待确认」）退役——待确认属能力行/原因行，时间行复读整行
 * 退场（渲染端与标题同文即不渲染）。 */
export function homeLiveScheduleText(meeting, mode, now = new Date()) {
  if (!meeting?.start_time) return "";
  if (mode === "now") return `按课表 ${meeting.start_time}–${meeting.end_time} 进行中`;
  if (schedulePhase(meeting, now) === "past") return `按课表 ${meeting.start_time}–${meeting.end_time} 已结束`;
  const dayText = friendlyDayLabel(meeting.date, now);
  return `按课表 ${[dayText, meeting.start_time].filter(Boolean).join(" ")} 开始`;
}

/* 主页直播卡标题行 = 课表事实（按课表进行中 / 按课表 …… 开始 / 已结束）；无课表事实时
   由调用方回退到能力标题。能力（直播入口可用 / 待确认）永远走独立的能力行。 */
export function homeLiveHeadline(meeting, mode, now = new Date()) {
  if (!meeting?.start_time) return "";
  if (mode === "now") return "按课表进行中";
  if (schedulePhase(meeting, now) === "past") return `按课表 ${meeting.start_time}–${meeting.end_time} 已结束`;
  const dayText = friendlyDayLabel(meeting.date, now);
  return `按课表 ${[dayText, meeting.start_time].filter(Boolean).join(" ")} 开始`;
}

// ---- 安装：状态胶囊 + 主页直播卡 ----

/* clock/recheckMs 可注入（LIVEEXP-1 测试钉）：时钟驱动重估与过窗翻转
 * 都走注入时钟，默认真实时钟与 30s 周期（LIVE_RECHECK_INTERVAL_MS）。 */
export function installHomeOverview(store, { clock = () => new Date(), recheckMs = LIVE_RECHECK_INTERVAL_MS } = {}) {
  refreshStatusCapsuleLabel();
  const unsubscribeTasks = store.subscribe("tasks", () => refreshStatusCapsuleLabel());

  const card = $("home-live-card");
  const labelNode = $("home-live-label");
  const capabilityNode = $("home-live-capability");
  const timeNode = $("home-live-time");
  const reasonNode = $("home-live-reason");
  const targetNode = $("home-live-target");
  const actionNode = $("home-live-action");
  const retryNode = $("home-live-recheck");
  /* LIVE-DISCLOSURE-1：早期功能小字静态装一次（闭集同源 live-state.js，
     不进状态机、不随渲染翻转） */
  const disclosureNode = $("home-live-disclosure");
  if (disclosureNode) disclosureNode.textContent = LIVE_DISCLOSURE_TEXT;

  let lastSnapshot = null;
  let disposed = false;
  let requestEpoch = 0;
  let controller = null;
  let currentTarget = null;
  /* LIVEEXP-1：上次渲染所依据的课表相位/目标身份/目标课程——tick 据此
   * 判定相位翻转（过窗自愈）并收敛旧目标红点。 */
  let lastPhase = "";
  let lastMeetingKey = "";
  let lastTargetCourseId = "";

  const renderIdle = (text) => {
    currentTarget = null;
    lastPhase = "";
    lastMeetingKey = "";
    lastTargetCourseId = "";
    if (!card || !labelNode) return;
    card.dataset.state = "unknown";
    labelNode.textContent = "暂无直播目标";
    if (capabilityNode) capabilityNode.textContent = "直播状态待确认";
    if (timeNode) timeNode.textContent = "";
    if (reasonNode) reasonNode.textContent = text;
    if (targetNode) targetNode.textContent = "";
    if (actionNode) {
      actionNode.hidden = true;
      actionNode.disabled = true;
      actionNode.dataset.action = "";
    }
    if (retryNode) retryNode.hidden = true;
  };

  const renderStatus = (target, value) => {
    const rawState = String(value?.state || "unknown");
    const canEnter = value?.can_enter === true;
    if (!card || !labelNode) return;
    const mode = target.meeting ? selectNowNext(lastSnapshot, clock()).mode : "";
    const headline = homeLiveHeadline(target.meeting, mode, clock());
    /* SWEEPFIX-2（SWEEP1-01）：单源一致态——观测先经「meeting 事实对账」再上卡，
       ended 观测撞上未开始的 meeting（说的是上一场）归位 upcoming。 */
    const state = reconcileMeetingEndedState({ state: rawState, meeting: target.meeting || null, mode, now: clock() });
    const detail = liveStateDetail(state);
    card.dataset.state = state;
    labelNode.textContent = headline || detail.label;
    if (capabilityNode) {
      capabilityNode.textContent = liveCapabilityText(state, canEnter);
      /* 与标题同文时退场（同 live-room 面板去重规则）：复读行不产生信息 */
      capabilityNode.hidden = capabilityNode.textContent === labelNode.textContent;
    }
    if (targetNode) targetNode.textContent = target?.course?.title || "";
    if (timeNode) {
      const scheduleText = homeLiveScheduleText(target.meeting, mode, clock());
      /* SWEEPFIX-2（SWEEP1-01）：时间行只在信息量超出标题时出场——与标题同文
         （前缀复读）即退场，四行收敛为「一个状态 + 至多一行补充」。 */
      timeNode.textContent = headline && scheduleText.startsWith(headline) ? "" : scheduleText;
    }
    if (reasonNode) reasonNode.textContent = detail.reason;
    if (actionNode) {
      /* 唯一主 CTA 是「进入直播」；普通课表态不再展示「重新确认」大按钮 */
      actionNode.hidden = !(state === "live" && canEnter);
      actionNode.dataset.action = state === "live" && canEnter ? "enter" : "";
      actionNode.textContent = "进入直播";
      actionNode.disabled = !(state === "live" && canEnter);
      /* W8: 与 live-room 同型——渲染路径成对收敛 disabled 与 aria-busy，清在途跨目标切换残留 */
      actionNode.setAttribute("aria-busy", "false");
    }
    if (retryNode) {
      const retryVisible = liveRetryVisible(state);
      retryNode.hidden = !retryVisible;
      retryNode.disabled = !retryVisible;
    }
  };

  const chooseTarget = () => homeLiveTarget(lastSnapshot, { courses: store.courses || [], activeCourse: store.activeCourse }, clock());

  const refresh = async () => {
    if (disposed) return;
    const found = chooseTarget();
    const target = found?.course ? found : null;
    currentTarget = target;
    if (!target) {
      renderIdle("打开课表或选择课程后，这里会预选直播目标。");
      return;
    }
    /* LIVEEXP-1：记录本次渲染依据的相位/目标身份，供 tick 判定翻转 */
    lastPhase = schedulePhase(target.meeting || null, clock());
    lastMeetingKey = String(target.meeting?.meeting_id || "");
    lastTargetCourseId = String(target.course.course_id || "");
    const epoch = ++requestEpoch;
    controller?.abort();
    const requestController = new AbortController();
    controller = requestController;
    card.dataset.state = "loading";
    const pendingMode = target.meeting ? selectNowNext(lastSnapshot, clock()).mode : "";
    labelNode.textContent = homeLiveHeadline(target.meeting, pendingMode, clock()) || "直播状态待确认";
    if (capabilityNode) {
      capabilityNode.textContent = "直播状态待确认";
      capabilityNode.hidden = capabilityNode.textContent === labelNode.textContent;
    }
    if (reasonNode) reasonNode.textContent = "确认完成前不会开放直播入口。";
    if (actionNode) {
      actionNode.hidden = true;
      actionNode.dataset.action = "";
      actionNode.disabled = true;
    }
    if (retryNode) retryNode.hidden = true;
    try {
      const value = await apiV3(`live-room/status?course_id=${encodeURIComponent(target.course.course_id)}`, {
        controller: requestController,
      });
      if (!disposed && epoch === requestEpoch && currentTarget === target) {
        /* 红点观测回写：state=live 的课程进 header 直播钮（零新增轮询） */
        store.set("liveActiveCourses", mergeLiveObservation(store.liveActiveCourses, target.course.course_id, value));
        renderStatus(target, value);
      }
    } catch (error) {
      if (!disposed && epoch === requestEpoch && currentTarget === target && error.name !== "AbortError") {
        renderStatus(target, { state: "offline", can_enter: false });
      }
    }
  };

  const handleAction = () => {
    if (disposed || !currentTarget?.course) return;
    if (actionNode?.dataset.action !== "enter") return;
    /* N6L S1 U2：进入直播=跳转独立直播页（预选本课）；主页第二份
       grants/sessions 会话链退役——会话只在直播页内建立。
       F2（化身走查 20261008）：CTA 文案即「进入直播」=明示进入意图，
       live-enter 事件让直播页落定后状态可进入时自动建立会话，
       「再点一次同文按钮」的无解释二次确认合并为一次点击。 */
    store.set("liveTarget", String(currentTarget.course.course_id));
    window.dispatchEvent(new CustomEvent("courselens:live-enter"));
    window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "live" }));
  };
  const handleRecheck = () => {
    if (!disposed) void refresh();
  };

  const handleSnapshot = (event) => {
    lastSnapshot = event?.detail || lastSnapshot;
    void refresh();
  };
  const handleLiveRefresh = () => void refresh();
  actionNode?.addEventListener("click", handleAction);
  retryNode?.addEventListener("click", handleRecheck);
  window.addEventListener("courselens:timetable-snapshot", handleSnapshot);
  window.addEventListener("courselens:live-refresh", handleLiveRefresh);
  const unsubscribeActiveCourse = store.subscribe("activeCourse", () => void refresh());

  /* LIVEEXP-1 时间驱动重估（与状态胶囊聚合同拍一个定时器）：页面静止跨过
   * 课表窗口后直播卡/红点自愈。裁决走 liveRecheckDecision 同源纯函数——
   * 相位翻转或临近边界才全量 refresh；相位不变时卡片文案是静态的，零请求
   * 零渲染（请求风暴禁令）。相位退出「进行中」时旧目标红点同步收敛。 */
  const handleRecheckTick = () => {
    if (disposed) return;
    refreshStatusCapsuleLabel();
    if (!lastSnapshot) return;
    const meeting = chooseTarget()?.meeting || null;
    const decision = liveRecheckDecision({
      meeting,
      meetingKey: String(meeting?.meeting_id || ""),
      now: clock(),
      lastPhase,
      lastKey: lastMeetingKey,
    });
    if (decision.action !== "full") return;
    if (lastPhase === "now" && decision.phase !== "now" && lastTargetCourseId) {
      store.set("liveActiveCourses", expirePastMeetingObservation(store.liveActiveCourses, lastTargetCourseId));
    }
    void refresh();
  };

  const recheckTimer = window.setInterval(handleRecheckTick, recheckMs);

  void refresh();

  return () => {
    if (disposed) return;
    disposed = true;
    controller?.abort();
    controller = null;
    unsubscribeTasks();
    unsubscribeActiveCourse();
    actionNode?.removeEventListener("click", handleAction);
    retryNode?.removeEventListener("click", handleRecheck);
    window.removeEventListener("courselens:timetable-snapshot", handleSnapshot);
    window.removeEventListener("courselens:live-refresh", handleLiveRefresh);
    window.clearInterval(recheckTimer);
  };
}
