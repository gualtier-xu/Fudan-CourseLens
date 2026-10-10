/* 直播独立页（N6L S1 · 设计纸 §五/§六/§九 S1/L2/L6）。
 * 16 态页面状态机 + 今晚在播列表（§二十一扇出策略）+ 会话建立链 + 恢复梯。
 * 入口协议：学习页卡/首页卡/header 钮 → selectPage("live")；卡片跳转先
 * store.set("liveTarget", course_id)，本页消费 liveTarget 预选课程。
 * 数据边界（北极星·安心）：直播不产生学习数据；列表只消费课表快照（本地）
 * 与 status 观测（后端），无直播专属定时器（B12），扇出上限=当日课数。 */

import { apiV3, postV3 } from "./api.js";
import { $, clear, textElement } from "./ui.js";
import {
  DEFAULT_LIVE_VIEW,
  LIVE_RECHECK_INTERVAL_MS,
  LIVE_VIEW_META,
  PAGE_STATE_COPY,
  liveCapabilityText,
  liveRecheckDecision,
  liveRetryVisible,
  liveStateDetail,
  mergeLiveObservation,
  mergeTranscriptSegments,
  resolveLiveErrorCard,
  resolveTranscriptOutcome,
  resolveViewBar,
  restateAmbiguousLiveState,
  schedulePhase,
} from "./live-state.js";
import { createLivePlayer } from "./live-player.js";

/* 页面跳转走既有 courselens:select-page 通用事件（§12.1 机制③，零新机制，
   也不与 shell 形成模块耦合） */
const goStudy = () => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));

/* 恢复梯上限（L2 #11：n/3），每次尝试短退避；3 次后落错误卡 */
const RECONNECT_MAX_ATTEMPTS = 3;
/* §二十一：进页一轮 + live-refresh 事件一轮 + 手动重新确认；60s 内缓存复用 */
const STATUS_CACHE_MS = 60000;

/* ---- 纯模型：今日课表里的直播候选（有目录课程才可作目标） ---- */
export function todayLiveMeetings(snapshot, courses = [], now = new Date()) {
  const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const days = Array.isArray(snapshot?.days) ? snapshot.days : [];
  const todayMeetings = (days.find((day) => String(day?.date || "") === today)?.meetings || []);
  const knownIds = new Set(courses.map((course) => String(course?.course_id || "")));
  const seen = new Set();
  return todayMeetings
    .filter((meeting) => knownIds.has(String(meeting?.catalog_course_id || "")))
    .filter((meeting) => {
      const id = String(meeting.catalog_course_id);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    })
    .map((meeting) => ({
      courseId: String(meeting.catalog_course_id),
      title: String(meeting.title || "课程"),
      start: String(meeting.start_time || ""),
      end: String(meeting.end_time || ""),
    }));
}

/* ---- 纯模型：16 态归并（L2 优先级：错误面板唯一 D14 > 过渡态 >
 * 播放态 > 入口态；等待态中性视觉）。undetermined 入口状态（unknown/
 * loading/stale/offline）按 idle 家族渲染中性待确认卡，文案取
 * liveStateDetail 同源，绝不发明「即将直播」的推算。 ---- */
export function resolveLivePageState({
  hasTarget = false,
  status = null,
  playback = "idle",
  errorCard = null,
  connecting = false,
  reconnectAttempt = 0,
} = {}) {
  if (errorCard) {
    const familyToState = { expired: "expired", gate: "gate", denied: "denied", fatal: "fatal" };
    return { state: familyToState[errorCard.family] || "stream", card: errorCard };
  }
  if (connecting) return { state: "connecting" };
  if (reconnectAttempt > 0) return { state: "recovering", attempt: reconnectAttempt };
  if (playback === "error") return { state: "fatal" };
  if (playback === "weaknet") return { state: "weaknet" };
  if (playback === "paused") return { state: "paused" };
  if (playback === "buffering") return { state: "buffering" };
  if (playback === "behind") return { state: "behind" };
  if (playback === "playing") return { state: "playing" };
  if (!hasTarget) return { state: "idle" };
  const statusState = String(status?.state || "unknown");
  if (statusState === "live" && status?.can_enter === true) return { state: "ready" };
  if (statusState === "upcoming") return { state: "upcoming" };
  if (statusState === "ended") return { state: "ended" };
  if (statusState === "denied") {
    return { state: "denied", card: resolveLiveErrorCard("live_authorization_denied") };
  }
  if (statusState === "offline" || statusState === "stale") return { state: "stream", statusDetail: liveStateDetail(statusState) };
  return { state: "idle", statusDetail: liveStateDetail(statusState) };
}

const ACTION_LABELS = Object.freeze({
  enter: "进入直播",
  reenter: "重新进入直播",
  recheck: "重新确认",
  lectures: "去讲次列表",
  timetable: "打开课表",
  cancel: "取消",
  resume: "继续观看",
  dismiss: "知道了",
  "retry-now": "立即重试",
});

export async function installLivePage(store, { clock = () => new Date(), recheckMs = LIVE_RECHECK_INTERVAL_MS } = {}) {
  const section = $("live-page");
  const cover = $("live-cover");
  const cardTitle = $("live-card-title");
  const cardBody = $("live-card-body");
  const cardDiagnostic = $("live-card-diagnostic");
  const cardActions = $("live-card-actions");
  const stageLine = $("live-status-line");
  const courseTitle = $("live-course-title");
  const courseContext = $("live-context");
  const tonight = $("live-tonight");
  const video = $("live-video");
  const hint = $("live-hint");
  const viewBar = $("live-view-bar");
  const viewHint = $("live-view-hint");
  const transcriptToggle = $("live-transcript-toggle");
  const transcriptStatus = $("live-transcript-status");
  const transcriptList = $("live-transcript-list");

  const player = createLivePlayer({
    onEvent: (event) => {
      if (event.playback === "error") {
        phase.errorCard = event.card;
        phase.playback = "error";
        render();
        return;
      }
      if (event.playback === "reconnect") {
        void runReconnect(event.code);
        return;
      }
      if (event.playback === "weaknet") {
        phase.playback = "weaknet";
        render();
        return;
      }
      if (event.playback === "buffering") {
        if (phase.playback === "playing" || phase.playback === "behind") phase.playback = "buffering";
        render();
        return;
      }
      if (event.playback === "streaming" || event.playback === "stable" || event.playback === "playing") {
        if (hint) hint.hidden = true;
        phase.playback = "playing";
        render();
        return;
      }
      if (event.playback === "behind") {
        phase.playback = "behind";
        render();
      }
    },
  });

  let disposed = false;
  let timetableSnapshot = null;
  let currentTarget = null;
  let connectEpoch = 0;
  let reconnectAttempt = 0;
  let awayPaused = false;
  let silentResumeUsed = false;
  /* 乙2：视角状态（闭集 id；切换=新 grant 新会话，防抖在途无效点） */
  let currentView = DEFAULT_LIVE_VIEW;
  let switchingView = false;
  let sessionActive = false;
  let sessionCourseId = "";
  /* 乙3：文稿侧板（三触发源：进直播/手动开板/换目标；零自动定时器） */
  let transcriptOpen = false;
  let transcriptBusy = false;
  let transcriptSegments = [];
  let transcriptTailMs = 0;
  const statusCache = new Map(); /* courseId -> { at, value } */

  const phase = {
    playback: "idle",
    errorCard: null,
    connecting: false,
    reconnectAttempt: 0,
    status: null,
  };
  /* LIVEEXP-1：上次渲染依据的课表相位——tick 据此判定过窗翻转 */
  let lastSchedulePhase = "";

  /* F2（化身走查 20261008 摩擦清单）：学习页/主页「进入直播」CTA 已明示进入
     意图，落页后再点一次同文按钮是无解释的二次确认。意图经 courselens:live-enter
     一锤传入：落页后首次状态确认若可进入则自动建立会话；不可进入/目标未就绪
     照常落状态卡（诚实回退，绝不凭意图发明可进入）。意图旗 20 秒内有效、
     用后即焚——页面后台刷新轮（快照/红点/tick）绝不携带陈旧意图自动进入。 */
  let pendingAutoEnterAt = 0;
  const AUTO_ENTER_INTENT_TTL_MS = 20000;

  const consumeAutoEnterIntent = (value) => {
    if (!pendingAutoEnterAt) return;
    if (sessionActive || Date.now() - pendingAutoEnterAt > AUTO_ENTER_INTENT_TTL_MS) {
      pendingAutoEnterAt = 0;
      return;
    }
    if (String(value?.state || "") === "live" && value?.can_enter === true) {
      pendingAutoEnterAt = 0;
      void connect();
    }
  };
  const handleLiveEnterIntent = () => {
    pendingAutoEnterAt = Date.now();
  };

  /* 当前目标的今日课表 meeting（无快照/目标未排直播 → null） */
  const targetTodayMeeting = () => todayLiveMeetings(timetableSnapshot, store.courses || [], clock())
    .find((item) => item.courseId === String(currentTarget?.course_id || "")) || null;

  const fillPlaceholders = (text, values) => String(text || "")
    .replace("{START}", values.start || "")
    .replace("{N}", values.attempt || "");

  const renderAction = (action, handler) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = ACTION_LABELS[action] || action;
    button.className = action === "enter" || action === "reenter" ? "btn-primary" : "btn-quiet";
    button.addEventListener("click", handler);
    return button;
  };

  const goStudyAction = () => goStudy();

  const ACTIONS = {
    enter: () => void connect(),
    reenter: () => void connect(),
    recheck: () => { phase.errorCard = null; void refreshStatus(); },
    lectures: goStudyAction,
    timetable: goStudyAction,
    cancel: () => {
      connectEpoch += 1; /* 在途链作废 */
      phase.connecting = false;
      render();
    },
    resume: () => void resumeFromAway(),
    dismiss: () => {
      if (hint) hint.hidden = true;
      phase.playback = "playing";
      render();
    },
    "retry-now": () => {
      reconnectAttempt = 0;
      phase.reconnectAttempt = 0;
      void connect();
    },
  };

  const renderCard = ({ title, body, actions, diagnostic = "" }) => {
    cardTitle.textContent = title || "";
    cardBody.textContent = body || "";
    cardDiagnostic.textContent = diagnostic;
    cardDiagnostic.hidden = !diagnostic;
    clear(cardActions);
    (actions || []).forEach((action) => {
      if (ACTIONS[action]) cardActions.append(renderAction(action, ACTIONS[action]));
    });
  };

  const render = () => {
    if (disposed) return;
    const resolved = resolveLivePageState({
      hasTarget: Boolean(currentTarget?.course_id),
      status: phase.status,
      playback: phase.playback,
      errorCard: phase.errorCard,
      connecting: phase.connecting,
      reconnectAttempt: phase.reconnectAttempt,
    });
    const state = resolved.state;
    section.dataset.state = state;
    /* 乙2：视角条/弱网声音档建议随每次渲染同步（媒体态提前 return 也要刷新） */
    renderViewBar();
    if (transcriptToggle) transcriptToggle.hidden = !sessionActive || Boolean(phase.errorCard);
    if (courseTitle) courseTitle.textContent = currentTarget?.title || "今晚在播";
    if (courseContext) {
      courseContext.textContent = currentTarget
        ? [contextScheduleText(), liveCapabilityText(String(phase.status?.state || "unknown"), phase.status?.can_enter === true)]
          .filter(Boolean).join(" · ") || "直播状态待确认"
        : "课表快照里今晚没有排直播。";
    }

    const mediaInPlace = ["playing", "buffering", "behind", "weaknet"].includes(state);
    video.hidden = !mediaInPlace && state !== "paused";
    cover.hidden = mediaInPlace;
    if (hint) hint.hidden = state !== "weaknet";
    stageLine.textContent = mediaInPlace
      ? [liveCapabilityText("live", true), currentTarget?.title || ""].filter(Boolean).join(" · ")
      : "";

    if (mediaInPlace) return;
    if (state === "paused") {
      renderCard({ ...PAGE_STATE_COPY.paused });
      return;
    }
    if (state === "recovering") {
      renderCard({
        title: PAGE_STATE_COPY.recovering.title,
        body: fillPlaceholders(PAGE_STATE_COPY.recovering.body, { attempt: `${resolved.attempt}/${RECONNECT_MAX_ATTEMPTS}` }),
        actions: PAGE_STATE_COPY.recovering.actions,
      });
      return;
    }
    if (state === "connecting") {
      renderCard({ ...PAGE_STATE_COPY.connecting });
      return;
    }
    if (resolved.card) {
      renderCard({ title: resolved.card.title, body: resolved.card.body, actions: resolved.card.actions, diagnostic: resolved.card.diagnostic });
      return;
    }
    if (resolved.statusDetail) {
      /* F6：模糊态（unknown/offline/stale）+ 今日课表事实并存时改述课表
         相位，替代「服务暂不可用/待确认」整卡；无课表事实才落原中性卡
         （live-state.js 同源纯函数，绝不凭课表宣称可以进入）。 */
      const restate = restateAmbiguousLiveState({
        statusState: String(phase.status?.state || ""),
        meeting: targetTodayMeeting(),
        now: clock(),
      });
      if (restate) {
        renderCard({ title: restate.title, body: restate.body, actions: restate.actions });
        return;
      }
      /* 入口状态未决/暂不可得：中性待确认卡（等待态视觉），文案与
         学习页直播行同源，绝不发明「即将直播」的推算 */
      renderCard({
        title: resolved.statusDetail.label,
        body: resolved.statusDetail.reason,
        actions: liveRetryVisible(String(phase.status?.state || "")) ? ["recheck"] : [],
      });
      return;
    }
    if (state === "upcoming") {
      renderCard({
        title: PAGE_STATE_COPY.upcoming.title,
        body: fillPlaceholders(PAGE_STATE_COPY.upcoming.body, { start: todayTargetStart() }),
        actions: PAGE_STATE_COPY.upcoming.actions,
      });
      return;
    }
    if (state === "idle") {
      renderCard({ ...PAGE_STATE_COPY.idle });
      return;
    }
    renderCard({ ...PAGE_STATE_COPY[state] });
  };

  const todayTargetStart = () => {
    if (!currentTarget?.course_id || !timetableSnapshot) return "";
    return targetTodayMeeting()?.start || "";
  };

  /* LIVEEXP-1：课表事实行相位感知——今天窗口已过如实改述「已结束」，
   * 绝不把结束的课写成「开始」；后端观测（能力文案）独立分层不混淆。 */
  const contextScheduleText = () => {
    const meeting = targetTodayMeeting();
    if (!meeting) return "";
    if (schedulePhase(meeting, clock()) === "past") return `按课表 ${meeting.start}–${meeting.end} 已结束`;
    return `按课表 ${meeting.start} 开始`;
  };

  /* ---- 乙2：视角条（闭集映射+防抖切换+失败回退默认视角） ---- */

  const renderViewBar = () => {
    if (!viewBar) return;
    const resolved = resolveViewBar({
      status: phase.status,
      currentView,
      switching: switchingView,
      weaknet: phase.playback === "weaknet",
      hasSession: sessionActive,
    });
    clear(viewBar);
    viewBar.hidden = !resolved.visible;
    if (viewHint) viewHint.hidden = !(resolved.visible && resolved.suggestAudio);
    if (!resolved.visible) return;
    resolved.views.forEach((view) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "live-view-option" + (LIVE_VIEW_META[view].audioOnly ? " live-view-audio" : "");
      button.textContent = LIVE_VIEW_META[view].label;
      button.setAttribute("aria-pressed", String(view === resolved.activeView));
      if (resolved.suggestAudio && LIVE_VIEW_META[view].audioOnly) button.dataset.suggest = "true";
      button.disabled = resolved.switching;
      button.addEventListener("click", () => { void switchView(view); });
      viewBar.append(button);
    });
  };

  const switchView = async (view) => {
    if (disposed || switchingView || !LIVE_VIEW_META[view]) return;
    const course = currentTarget;
    if (!course?.course_id || view === currentView) return;
    const epoch = connectEpoch;
    switchingView = true; /* 防抖：切换在途期间再点无效 */
    renderViewBar();
    try {
      const session = await establishSession(course.course_id, epoch, view);
      if (disposed || epoch !== connectEpoch) return;
      currentView = LIVE_VIEW_META[String(session?.view || "")] ? String(session.view) : view;
      switchingView = false;
      sessionActive = true;
      phase.playback = "idle";
      player.attach(session.manifest_path); /* 切换=恰重建一次 */
      render();
    } catch (error) {
      if (disposed || epoch !== connectEpoch) return;
      const code = String(error?.code || "");
      console.warn(`[live] 切换视角失败 view=${view} code=${code || "network"}`);
      switchingView = false;
      if (view !== DEFAULT_LIVE_VIEW) {
        /* 失败回退：非默认视角失败→静默回默认视角重建恰一次 */
        await switchView(DEFAULT_LIVE_VIEW);
        return;
      }
      sessionActive = false;
      phase.connecting = false;
      phase.errorCard = resolveLiveErrorCard(code);
      render();
      return;
    }
    renderViewBar();
  };

  const chooseTarget = () => {
    const liveTarget = String(store.liveTarget || "");
    if (liveTarget) {
      const hit = (store.courses || []).find((course) => String(course?.course_id || "") === liveTarget);
      if (hit) return hit;
    }
    if (store.activeCourse?.course_id) return store.activeCourse;
    const meetings = todayLiveMeetings(timetableSnapshot, store.courses || [], clock());
    if (meetings.length) {
      const first = meetings[0];
      return (store.courses || []).find((course) => String(course?.course_id || "") === first.courseId) || null;
    }
    return null;
  };

  const fetchStatus = async (courseId, { force = false } = {}) => {
    const cached = statusCache.get(String(courseId));
    if (!force && cached && Date.now() - cached.at < STATUS_CACHE_MS) return cached.value;
    try {
      const value = await apiV3(`live-room/status?course_id=${encodeURIComponent(courseId)}`);
      statusCache.set(String(courseId), { at: Date.now(), value });
      noteObservation(courseId, value);
      return value;
    } catch (error) {
      /* 第廿六案证据线：状态确认失败码落本机控制台（零外发），取流/授权层排查用 */
      console.warn(`[live] 状态确认失败 course=${courseId} code=${String(error?.code || "network")}`);
      const value = { state: "offline", can_enter: false };
      statusCache.set(String(courseId), { at: Date.now(), value });
      return value;
    }
  };

  /* 红点数据源：任何一次 status 观测都回写 store（header 钮消费）；
     零新增轮询——只在既有的进页/刷新轮里顺带更新。 */
  const noteObservation = (courseId, value) => {
    const next = mergeLiveObservation(store.liveActiveCourses, courseId, value);
    if (JSON.stringify(next) !== JSON.stringify(store.liveActiveCourses || [])) {
      store.set("liveActiveCourses", next);
    }
  };

  const refreshStatus = async ({ force = false } = {}) => {
    if (disposed) return;
    currentTarget = chooseTarget();
    /* LIVEEXP-1：记录本次渲染依据的课表相位，供 tick 判定过窗翻转 */
    lastSchedulePhase = schedulePhase(targetTodayMeeting(), clock());
    /* C10 退场矩阵：换目标=会话/视角/文稿全部回到默认起点（旧会话随
       connectEpoch 作废），绝不把上一门课的视角与文稿带进新课 */
    if (String(currentTarget?.course_id || "") !== sessionCourseId) {
      connectEpoch += 1;
      currentView = DEFAULT_LIVE_VIEW;
      switchingView = false;
      sessionActive = false;
      sessionCourseId = String(currentTarget?.course_id || "");
      transcriptSegments = [];
      transcriptTailMs = 0;
      if (transcriptOpen) renderTranscript({ hint: "" });
    }
    renderTonight();
    phase.status = null;
    if (!currentTarget?.course_id) {
      phase.playback = "idle";
      render();
      return;
    }
    render();
    const value = await fetchStatus(currentTarget.course_id, { force });
    if (disposed) return;
    phase.status = value;
    render();
    consumeAutoEnterIntent(value); /* F2：意图旗在途且状态可进入 → 自动建会（恰一次） */
  };

  const renderTonight = () => {
    if (!tonight) return;
    clear(tonight);
    const meetings = todayLiveMeetings(timetableSnapshot, store.courses || [], clock());
    if (!meetings.length) {
      tonight.append(textElement("p", "课表快照里今晚没有排直播。", "live-tonight-empty"));
      return;
    }
    meetings.forEach((meeting) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "live-tonight-item";
      row.dataset.courseId = meeting.courseId;
      const selected = String(currentTarget?.course_id || "") === meeting.courseId;
      row.setAttribute("aria-pressed", String(selected));
      const cached = statusCache.get(meeting.courseId)?.value;
      const chip = liveCapabilityText(String(cached?.state || "unknown"), cached?.can_enter === true);
      row.append(textElement("span", meeting.title, "lti-title"));
      row.append(textElement("span", [meeting.start, meeting.end].filter(Boolean).join("–"), "lti-time"));
      row.append(textElement("span", chip, "lti-chip"));
      row.addEventListener("click", () => {
        store.set("liveTarget", meeting.courseId);
        phase.errorCard = null;
        void refreshStatus({ force: false });
      });
      tonight.append(row);
    });
  };

  /* ---- 会话建立链与恢复梯 ---- */

  const establishSession = async (courseId, epoch, view = currentView) => {
    const grant = await postV3("live-room/grants", { course_id: courseId });
    if (disposed || epoch !== connectEpoch) return null;
    /* 乙2：视角维度——闭集校验在后端（live_view_unknown 400，grant 可原地重试） */
    const session = await postV3("live-room/sessions", { grant: grant.grant, view });
    if (disposed || epoch !== connectEpoch) return null;
    return session;
  };

  const connect = async ({ silent = false } = {}) => {
    const course = currentTarget;
    if (!course?.course_id) return;
    const epoch = ++connectEpoch;
    reconnectAttempt = 0;
    phase.reconnectAttempt = 0;
    phase.errorCard = null;
    awayPaused = false;
    silentResumeUsed = false;
    phase.connecting = !silent;
    phase.playback = "idle";
    render();
    try {
      const session = await establishSession(course.course_id, epoch);
      if (!session) return;
      phase.connecting = false;
      sessionActive = true;
      sessionCourseId = String(course.course_id);
      if (LIVE_VIEW_META[String(session?.view || "")]) currentView = String(session.view);
      player.attach(session.manifest_path);
      if (transcriptOpen) void loadTranscript({ reset: true }); /* 乙3 触发源①：进直播 */
      render();
    } catch (error) {
      if (disposed || epoch !== connectEpoch) return;
      /* 第廿六案证据线：进入链失败码全透（卡面+控制台）；unknown 只在
         完全无码时出现，且附诊断摘要行 */
      const code = String(error?.code || "");
      console.warn(`[live] 进入直播失败 course=${course.course_id} code=${code || "network"}`);
      phase.connecting = false;
      sessionActive = false;
      phase.errorCard = resolveLiveErrorCard(code);
      render();
    }
  };

  const runReconnect = async (code) => {
    if (disposed) return;
    if (reconnectAttempt >= RECONNECT_MAX_ATTEMPTS) {
      const card = resolveLiveErrorCard(code || player.lastFailureCode);
      console.warn(`[live] 恢复梯尽(${RECONNECT_MAX_ATTEMPTS}) code=${card.code}${card.diagnostic ? " 诊断已透卡" : ""}`);
      phase.reconnectAttempt = 0;
      reconnectAttempt = 0;
      phase.errorCard = card;
      render();
      return;
    }
    reconnectAttempt += 1;
    phase.reconnectAttempt = reconnectAttempt;
    phase.playback = "idle";
    render();
    await new Promise((done) => window.setTimeout(done, 600 * reconnectAttempt));
    const course = currentTarget;
    if (disposed || !course?.course_id) return;
    const epoch = connectEpoch;
    try {
      const session = await establishSession(course.course_id, epoch, currentView);
      if (!session || disposed) return;
      phase.reconnectAttempt = 0;
      reconnectAttempt = 0;
      sessionActive = true;
      player.attach(session.manifest_path);
      render();
    } catch {
      await runReconnect(code);
    }
  };

  const resumeFromAway = async () => {
    const resumed = await player.resume();
    if (resumed) {
      awayPaused = false;
      phase.playback = "playing";
      render();
      return;
    }
    /* B6：静默续建恰一次——失败不再无声重试，落回过期/中断卡 */
    if (silentResumeUsed) {
      phase.errorCard = resolveLiveErrorCard("live_session_expired");
      render();
      return;
    }
    silentResumeUsed = true;
    await connect({ silent: true });
  };

  /* ---- 乙3：文稿侧板（platform|local 双源抽象；无文稿=闭集提示；
     零自动定时器，三触发源=进直播/手动开板/换目标） ---- */

  const todayLectureSubId = (course) => {
    if (!course?.course_id) return "";
    const known = (store.courses || []).find(
      (item) => String(item?.course_id || "") === String(course.course_id),
    );
    const now = clock();
    const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const lecture = (known?.lectures || []).find(
      (item) => String(item?.date || "") === today && String(item?.sub_id || ""),
    );
    return String(lecture?.sub_id || "");
  };

  const renderTranscript = ({ hint = "", loading = false } = {}) => {
    if (!transcriptList) return;
    if (transcriptStatus) {
      const message = hint || (loading ? "正在取文稿…" : "");
      transcriptStatus.textContent = message;
      transcriptStatus.hidden = !message;
    }
    if (transcriptOpen) transcriptList.hidden = false;
    clear(transcriptList);
    transcriptSegments.forEach((segment) => {
      const row = document.createElement("p");
      row.className = "live-transcript-row";
      const seconds = Math.floor(Number(segment.start_ms || 0) / 1000);
      const stamp = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      row.append(textElement("span", stamp, "ltr-stamp"));
      row.append(textElement("span", String(segment.text || ""), "ltr-text"));
      transcriptList.append(row);
    });
    if (transcriptOpen && transcriptSegments.length) {
      transcriptList.scrollTop = transcriptList.scrollHeight; /* 跟读最新一句 */
    }
  };

  const loadTranscript = async ({ reset = false } = {}) => {
    if (disposed || transcriptBusy) return;
    const subId = todayLectureSubId(currentTarget);
    if (!subId) {
      transcriptSegments = [];
      transcriptTailMs = 0;
      renderTranscript({ hint: "这门课今天没有排讲次，直播文稿要等讲次开始后才有。" });
      return;
    }
    transcriptBusy = true;
    if (reset) {
      transcriptSegments = [];
      transcriptTailMs = 0;
    }
    renderTranscript({ loading: transcriptOpen && !transcriptSegments.length });
    try {
      const sinceQuery = transcriptTailMs ? `&since_ms=${transcriptTailMs}` : "";
      const value = await apiV3(`transcript/segments?sub_id=${encodeURIComponent(subId)}${sinceQuery}`);
      const outcome = resolveTranscriptOutcome({
        available: value?.available === true ? true : value?.available === false ? false : null,
        segments: Array.isArray(value?.segments) ? value.segments : null,
      });
      transcriptSegments = mergeTranscriptSegments(transcriptSegments, outcome.segments);
      if (transcriptSegments.length) {
        transcriptTailMs = Number(transcriptSegments[transcriptSegments.length - 1].start_ms || 0);
      }
      renderTranscript({ hint: outcome.hint });
    } catch (error) {
      const code = String(error?.code || "");
      const outcome = resolveTranscriptOutcome({ notFound: code === "transcript_lecture_unknown", failed: true });
      renderTranscript({ hint: outcome.hint });
    }
    transcriptBusy = false;
  };

  const handleTranscriptToggle = () => {
    if (disposed) return;
    transcriptOpen = !transcriptOpen;
    transcriptToggle.setAttribute("aria-expanded", String(transcriptOpen));
    transcriptToggle.textContent = transcriptOpen ? "收起文稿" : "打开文稿";
    if (transcriptOpen) {
      transcriptList.hidden = false;
      void loadTranscript({ reset: !transcriptSegments.length }); /* 乙3 触发源②：手动开板 */
    } else {
      transcriptList.hidden = true;
    }
  };

  /* ---- 页面显隐与刷新事件 ---- */

  /* LIVEEXP-1：页内可见性——时间驱动重估只在直播页当前可见时工作，
   * 后台页零动作（不打扰媒体链，也不做无谓请求）。 */
  let pageVisible = false;

  const handlePageChange = (event) => {
    const name = String(event?.detail || "");
    if (disposed) return;
    pageVisible = name === "live";
    if (name === "live") {
      if (awayPaused) {
        awayPaused = false;
        void resumeFromAway();
      } else {
        void refreshStatus();
      }
      return;
    }
    if (player.isActive) {
      /* 离开直播页即暂停（零后台占用），回页一键续播 */
      player.pauseForAway();
      if (phase.playback !== "error") {
        awayPaused = true;
        silentResumeUsed = false; /* 每个离开周期恰一次静默续建预算 */
        phase.playback = "paused";
        render();
      }
    }
  };

  const handleLiveRefresh = () => {
    if (!disposed) void refreshStatus({ force: true });
  };

  const handleSnapshot = (event) => {
    timetableSnapshot = event?.detail || timetableSnapshot;
    if (!disposed) void refreshStatus();
  };

  const unsubscribeLiveTarget = store.subscribe("liveTarget", () => {
    if (disposed) return;
    phase.errorCard = null;
    void refreshStatus();
  });

  const handleRecheck = () => ACTIONS.recheck();
  /* LIVEEXP-1 时间驱动重估：仅页面可见且非播放/过渡/错误态时裁决（媒体链
   * 自治、错误卡用户主导）。相位翻转或临近窗口边界才 force 重查——tick
   * 本身零请求，B12「无网络轮询」语义保持；文稿侧板仍零自动定时器。 */
  const handleRecheckTick = () => {
    if (disposed || !pageVisible) return;
    if (phase.errorCard) return;
    if (["playing", "buffering", "behind", "weaknet", "connecting", "recovering", "paused"].includes(phase.playback)) return;
    const decision = liveRecheckDecision({
      meeting: targetTodayMeeting(),
      meetingKey: String(currentTarget?.course_id || ""),
      now: clock(),
      lastPhase: lastSchedulePhase,
      lastKey: String(currentTarget?.course_id || ""),
    });
    if (decision.action === "full") void refreshStatus({ force: true });
  };
  const recheckTimer = window.setInterval(handleRecheckTick, recheckMs);
  window.addEventListener("courselens:page", handlePageChange);
  window.addEventListener("courselens:live-refresh", handleLiveRefresh);
  window.addEventListener("courselens:timetable-snapshot", handleSnapshot);
  window.addEventListener("courselens:live-enter", handleLiveEnterIntent); /* F2：进入意图旗 */
  $("live-recheck")?.addEventListener("click", handleRecheck);
  transcriptToggle?.addEventListener("click", handleTranscriptToggle);

  render();
  void refreshStatus();

  return () => {
    disposed = true;
    connectEpoch += 1;
    unsubscribeLiveTarget();
    window.clearInterval(recheckTimer);
    window.removeEventListener("courselens:page", handlePageChange);
    window.removeEventListener("courselens:live-refresh", handleLiveRefresh);
    window.removeEventListener("courselens:timetable-snapshot", handleSnapshot);
    window.removeEventListener("courselens:live-enter", handleLiveEnterIntent);
    $("live-recheck")?.removeEventListener("click", handleRecheck);
    transcriptToggle?.removeEventListener("click", handleTranscriptToggle);
    player.dispose();
  };
}
