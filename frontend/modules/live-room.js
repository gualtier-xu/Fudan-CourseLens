import { apiV3 } from "./api.js";
import { $, formatRelativeTime } from "./ui.js";
/* N6L S1 U2：状态/能力/失败文案闭集收敛到 live-state.js 唯一出处，
   本模块只剩学习页直播行的渲染与刷新；进入动作=跳转直播页（会话链
   由 live-page.js 独立建立，学习页不再保留第二份 grants/sessions 链）。 */
import {
  LIVE_DISCLOSURE_TEXT,
  LIVE_RECHECK_INTERVAL_MS,
  liveCapabilityText,
  liveObservationTimeText,
  liveRetryVisible,
  liveStateDetail,
  reconcileMeetingEndedState,
  schedulePhase,
} from "./live-state.js";

export {
  liveCapabilityText, liveRetryVisible, liveStateDetail,
  liveFailureReason, ENTRY_FAILURES,
} from "./live-state.js";

let lastStatus = null;
/* LIVEEXP-1：上次渲染所依据的课表事实行——tick 据此判定相位翻转（过窗自愈） */
let lastScheduleHeadline = null;

/* D2（CUI-1）：课表 starts_at/ends_at 的 ISO 串 → 人话（「今晚 19:00」家族，
   ui.js formatRelativeTime 同源）；空值/不可解析值诚实回退。 */
function humanizeIsoTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const epoch = Date.parse(raw);
  return Number.isFinite(epoch) ? formatRelativeTime(epoch / 1000) : raw;
}

function ensureStatusRegion() {
  const row = $("live-room-row");
  row.setAttribute("role", "status");
  row.setAttribute("aria-live", "polite");
  row.setAttribute("aria-atomic", "true");
}

function setRowBusy(busy) {
  $("live-room-row").setAttribute("aria-busy", busy ? "true" : "false");
}

function setCompactRetry(visible) {
  const retry = $("live-room-recheck");
  if (!retry) return;
  retry.textContent = "重新确认";
  retry.hidden = !visible;
  retry.disabled = !visible || $("live-room-row").getAttribute("aria-busy") === "true";
}

function render(state, canEnter, value, hasCourse = true) {
  lastStatus = value;
  /* SWEEPFIX-2（SWEEP1-01）：与主页直播卡同律——ended 观测撞上未开始的
     今天 meeting（说的是上一场）先对账归位 upcoming，再取闭集文案，
     整行只剩一个一致陈述。 */
  const presented = reconcileMeetingEndedState({
    state,
    meeting: hasCourse ? findCourseMeeting(liveRoomCourseId) : null,
    now: liveClock(),
  });
  const detail = liveStateDetail(presented);
  const scheduleText = hasCourse ? liveRoomScheduleHeadline() : "";
  lastScheduleHeadline = scheduleText;
  const headline = scheduleText || detail.label;
  const action = hasCourse ? detail.action : "";
  ensureStatusRegion();
  $("live-room-row").dataset.state = presented;
  $("live-room-label").textContent = headline;
  const capabilityNode = $("live-room-capability");
  const capabilityText = liveCapabilityText(presented, canEnter);
  capabilityNode.textContent = capabilityText;
  /* 能力行与标题同文案时整行退场（unknown/pending 两闭集同文）：重复一行不产生
     信息，只产生「这面板在复读」的噪音（夜4 审美四问：删了更好吗）。 */
  capabilityNode.hidden = capabilityText === headline;
  /* SWEEPFIX-2（SWEEP1-02）：时间行闭集标签化——裸「7/28」无单位无标签改为
     「上次直播 …」（观测窗口属于上一场）/ live 态照述本场窗口；格式仍走
     ui.js 人话时间家族。 */
  $("live-room-time").textContent = liveObservationTimeText({
    startsAt: value?.starts_at,
    endsAt: value?.ends_at,
    state: presented,
    hasCourse,
    format: humanizeIsoTime,
  });
  $("live-room-reason").textContent = hasCourse ? detail.reason : liveStateDetail("idle").reason;
  /* 普通待确认/未开始/已结束不放 CTA；可行动错误只有紧凑重试；直播可用保持主 CTA */
  $("enter-live-room").textContent = "进入直播";
  $("enter-live-room").dataset.action = action === "enter" ? "enter" : "";
  $("enter-live-room").hidden = action !== "enter";
  $("enter-live-room").disabled = !(action === "enter" && canEnter === true);
  /* W8: 渲染路径成对收敛 disabled 与 aria-busy（对齐 ui.js setBusy 对偶）——
     在途动作跨目标切换时 epoch 守卫会跳过 finally 的 setBusy(false)，残留 aria-busy 由渲染复位 */
  $("enter-live-room").setAttribute("aria-busy", "false");
  setRowBusy(false); /* 先清 busy 再渲染重试按钮：disabled 计算依赖 aria-busy */
  setCompactRetry(action === "refresh");
}

/* 课表事实（按课表进行中/开始/已结束）来自共享周课表快照事件；无快照时不发明。
 * LIVEEXP-1：相位判定同源 live-state.js schedulePhase——今天窗口已过如实改述
 * 「已结束」，绝不把结束的课写成「开始」。 */
let timetableSnapshotValue = null;
let liveRoomCourseId = "";
let liveClock = () => new Date(); /* install 可注入（LIVEEXP-1 测试钉） */

function liveRoomScheduleHeadline() {
  const meeting = findCourseMeeting(liveRoomCourseId);
  if (!meeting) return "";
  const now = liveClock();
  const phase = schedulePhase(meeting, now);
  if (phase === "now") return `按课表进行中 · ${meeting.start_time}–${meeting.end_time}`;
  if (phase === "past") return `按课表 ${meeting.start_time}–${meeting.end_time} 已结束`;
  if (phase === "future") return `按课表 ${meeting.start_time} 开始`;
  return "";
}

function findCourseMeeting(courseId) {
  const id = String(courseId || "");
  if (!id || !timetableSnapshotValue) return null;
  const today = liveClock();
  const todayIso = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const days = Array.isArray(timetableSnapshotValue?.days) ? timetableSnapshotValue.days : [];
  for (const day of days) {
    if (String(day?.date || "") !== todayIso) continue;
    const meeting = (Array.isArray(day?.meetings) ? day.meetings : []).find(
      (item) => String(item?.catalog_course_id || "") === id,
    );
    if (meeting) return meeting;
  }
  return null;
}

export async function installLiveRoom(store, { clock = () => new Date(), recheckMs = LIVE_RECHECK_INTERVAL_MS } = {}) {
  ensureStatusRegion();
  /* LIVE-DISCLOSURE-1：状态条下方早期功能小字，静态装一次（闭集同源
     live-state.js，不进状态机） */
  const disclosureNode = $("live-room-disclosure");
  if (disclosureNode) disclosureNode.textContent = LIVE_DISCLOSURE_TEXT;
  liveClock = clock;
  let controller = null;
  let disposed = false;
  let requestEpoch = 0;
  const isCurrentCourse = (courseId, epoch) => !disposed
    && epoch === requestEpoch
    && store.activeCourse?.course_id === courseId;
  const handleTimetableSnapshot = (event) => {
    timetableSnapshotValue = event?.detail || timetableSnapshotValue;
    if (lastStatus && store.activeCourse?.course_id) {
      /* 课表快照到达后仅重述课表事实，不改变能力结论 */
      render(String(lastStatus.state || "unknown"), lastStatus.can_enter === true, lastStatus);
    }
  };
  window.addEventListener("courselens:timetable-snapshot", handleTimetableSnapshot);
  const refresh = async (course) => {
    if (disposed) return;
    const epoch = ++requestEpoch;
    const courseId = course?.course_id;
    controller?.abort();
    if (!courseId) {
      if (!disposed && epoch === requestEpoch && !store.activeCourse?.course_id) {
        liveRoomCourseId = "";
        render("idle", false, { state: "idle", can_enter: false }, false);
      }
      return;
    }
    const requestController = new AbortController();
    controller = requestController;
    ensureStatusRegion();
    setRowBusy(true);
    liveRoomCourseId = String(courseId);
    const scheduleHeadline = liveRoomScheduleHeadline();
    lastScheduleHeadline = scheduleHeadline;
    $("live-room-row").dataset.state = "loading";
    $("live-room-label").textContent = scheduleHeadline || "直播状态待确认";
    const capabilityNode = $("live-room-capability");
    capabilityNode.textContent = "直播状态待确认";
    capabilityNode.hidden = !scheduleHeadline; /* 与标题同文时退场（同 render 去重规则） */
    $("live-room-time").textContent = "仅显示新的后端观测";
    $("live-room-reason").textContent = "确认完成前不会开放直播入口。";
    $("enter-live-room").dataset.action = "";
    $("enter-live-room").hidden = true;
    setCompactRetry(false);
    try {
      const value = await apiV3(`live-room/status?course_id=${encodeURIComponent(courseId)}`, {
        controller: requestController,
      });
      if (isCurrentCourse(courseId, epoch)) render(String(value?.state || "unknown"), value?.can_enter === true, value);
    } catch (error) {
      if (isCurrentCourse(courseId, epoch) && error.name !== "AbortError") {
        render("offline", false, { state: "offline", can_enter: false });
      }
    }
  };
  const unsubscribe = store.subscribe("activeCourse", (course) => void refresh(course));
  const handleRefreshRequest = () => {
    if (!disposed) void refresh(store.activeCourse);
  };
  window.addEventListener("courselens:live-refresh", handleRefreshRequest);
  const handleAction = () => {
    if (disposed) return;
    const course = store.activeCourse;
    if (!course?.course_id) return;
    if (lastStatus?.state !== "live" || lastStatus?.can_enter !== true) return;
    /* N6L S1 U2：进入直播=跳转独立直播页（预选本课）；会话建立改由
       live-page.js 在页内进行，学习页不再内嵌播放也不保留进入链。
       F2（化身走查 20261008）：同主页卡——live-enter 携带进入意图，
       直播页落定后可进入即自动建会，双重确认合并为一次点击。 */
    store.set("liveTarget", String(course.course_id));
    window.dispatchEvent(new CustomEvent("courselens:live-enter"));
    window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "live" }));
  };
  const handleRecheck = () => {
    if (!disposed) void refresh(store.activeCourse);
  };
  /* LIVEEXP-1 时间驱动重估：页面静止跨过课表窗口后直播行自愈。只有课表
   * 事实行变化（进行中→已结束/开始→进行中）才全量重查收敛能力行；无快照
   * 时 headline 恒空、无观测时无相位语义，都零请求（请求风暴禁令）。 */
  const handleRecheckTick = () => {
    if (disposed) return;
    if (!lastStatus || !store.activeCourse?.course_id) return;
    const headline = liveRoomScheduleHeadline();
    if (headline !== lastScheduleHeadline) void refresh(store.activeCourse);
  };
  const recheckTimer = window.setInterval(handleRecheckTick, recheckMs);
  $("enter-live-room").addEventListener("click", handleAction);
  $("live-room-recheck")?.addEventListener("click", handleRecheck);
  if (store.activeCourse) await refresh(store.activeCourse);
  else render("idle", false, { state: "idle", can_enter: false }, false);
  return () => {
    disposed = true;
    controller?.abort();
    controller = null;
    unsubscribe();
    window.clearInterval(recheckTimer);
    window.removeEventListener("courselens:live-refresh", handleRefreshRequest);
    window.removeEventListener("courselens:timetable-snapshot", handleTimetableSnapshot);
    $("enter-live-room").removeEventListener("click", handleAction);
    $("live-room-recheck")?.removeEventListener("click", handleRecheck);
  };
}
