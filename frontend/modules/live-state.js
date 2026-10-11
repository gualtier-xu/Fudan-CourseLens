/* 直播闭集同源模块（N6L S1 · 设计纸 §十二/§十七/L2/L6）。
 * 状态语义、能力文案、入口失败码、逐码错误卡与 16 态文案在这里唯一出处；
 * live-page.js / live-player.js 与（过渡期）live-room.js 三消费点同源。
 * 码表等集钉：ENTRY_FAILURES/LIVE_ERROR_CARDS ≡ 后端直播闭集码
 * （src/runtime/live_room.py + src/api/icourse.py；tests 两处同改纪律）。 */

export const STATE_DETAILS = Object.freeze({
  live: Object.freeze({ label: "正在直播", reason: "后端已确认，可以安全进入直播。", action: "enter" }),
  upcoming: Object.freeze({ label: "直播尚未开始", reason: "开始时间尚未到，开始前会保持安静。", action: "" }),
  ended: Object.freeze({ label: "直播已结束", reason: "可以在讲次列表中查看已授权回放。", action: "" }),
  denied: Object.freeze({ label: "无直播访问权限", reason: "当前账号没有这门课程的直播访问权限。", action: "" }),
  offline: Object.freeze({ label: "直播服务暂不可用", reason: "暂时无法取得新的直播状态，可检查网络后重试。", action: "refresh" }),
  stale: Object.freeze({ label: "直播状态已过期", reason: "上次观测已经过期，重新确认后才能进入。", action: "refresh" }),
  unknown: Object.freeze({ label: "直播状态待确认", reason: "还没拿到这门课的直播状态，拿到后这里会更新。", action: "" }),
  loading: Object.freeze({ label: "直播状态待确认", reason: "确认完成前不会开放直播入口。", action: "" }),
  idle: Object.freeze({ label: "暂无直播目标", reason: "尚未选择课程，暂时不能确认直播状态。", action: "" }),
});

/* 能力闭集：课表事实（时间线）与后端能力分开陈述；
   探测未决绝不把课程写成“直播状态未知”。 */
export const CAPABILITY_TEXT = Object.freeze({
  live_ready: "直播入口可用",
  live_pending: "直播状态待确认",
  upcoming: "直播未开始",
  ended: "直播已结束",
  denied: "无直播权限",
  offline: "直播服务暂不可用",
  pending: "直播状态待确认",
});

export function liveCapabilityText(state, canEnter = false) {
  if (state === "live") return canEnter === true ? CAPABILITY_TEXT.live_ready : CAPABILITY_TEXT.live_pending;
  return CAPABILITY_TEXT[state] || CAPABILITY_TEXT.pending;
}

/* 可行动错误才提供紧凑重试；普通课表态/等待态不放常驻 CTA */
export function liveRetryVisible(state) {
  return state === "offline" || state === "stale";
}

export const ENTRY_FAILURES = Object.freeze({
  live_authorization_denied: "当前账号没有这门课程的直播访问权限。",
  live_authorization_revoked: "直播访问权限已变化，请重新确认课程状态。",
  live_grant_invalid: "直播进入凭证已过期，请重新确认后再进入。",
  live_grant_identity_changed: "当前账号状态已变化，请重新确认课程状态。",
  live_session_expired: "直播安全会话已过期，请重新进入。",
  live_upstream_rejected: "学校直播源暂时拒绝，请重新进入。",
  live_upstream_unreachable: "学校直播源暂时连接不上，请稍后重新进入。",
  live_stream_unavailable: "直播源暂时不可用，请稍后重新进入。",
  live_resource_expired: "直播资源会话已过期，请重新进入。",
  live_content_type_rejected: "学校直播源返回了无法播放的内容，请重新进入一次。",
  live_playback_not_open: "平台提示这节课还没到开放时间（学校对新内容有最长 24 小时的审核），稍后再来看看。",
});

export function liveStateDetail(state) {
  const key = STATE_DETAILS[state] ? state : "unknown";
  return STATE_DETAILS[key];
}

/* ---- 直播早期功能小字（LIVE-DISCLOSURE-1）----
 * 直播转写还是早期功能：一句闭集文案，客户端（主页直播卡/学习页直播行）
 * 与 README、Pages 三面同源——语境可各取其形，禁三处三套说法。
 * 静态小字：安装时装一次，不进 16 态状态机、不随任何状态翻转。 */
export const LIVE_DISCLOSURE_TEXT = "直播转写为早期功能，可用性视网络与课程环境而定。";

export function liveFailureReason(code) {
  return ENTRY_FAILURES[String(code || "")] || "直播入口暂不可用，请重新确认状态。";
}

/* 红点数据源合并（纯函数）：任何一次 status 观测顺带回写 store 的
 * liveActiveCourses（header 直播钮消费）；零新增轮询——只在既有的
 * 进页/刷新轮里更新。 */
export function mergeLiveObservation(activeIds, courseId, value) {
  const next = new Set((activeIds || []).map(String));
  if (value?.state === "live") next.add(String(courseId));
  else next.delete(String(courseId));
  return [...next];
}

/* 红点相位过期（纯函数，LIVEEXP-1）：目标退出课表「进行中」相位时，
 * 卡片已不再主张进行中（时间线改述已结束/下一节），旧观测的 live 红点
 * 与卡片同源收敛；若后端流真的还在播，下一次该课观测会经
 * mergeLiveObservation 重新点亮（两事实分层，不互斥）。 */
export function expirePastMeetingObservation(activeIds, courseId) {
  const id = String(courseId || "");
  if (!id) return [...(activeIds || []).map(String)];
  return (activeIds || []).map(String).filter((item) => item !== id);
}

/* ---- 时间驱动重估（LIVEEXP-1）：home 卡/学习页直播行/直播页三面同源 ----
 * 病灶：入口状态只在事件（快照/刷新/选课/进页）时重估，页面静止跨过课表
 * 窗口后「按课表进行中/入口可用/后端已确认」残留（用户报障：过窗两小时
 * 不翻转）。裁决纯函数：相位翻转或目标变化，或临近窗口边界 → "full"（全量
 * 重查）；否则 "skip"（相位不变时卡片文案是静态的，零请求零渲染——
 * 请求风暴禁令）。周期常量供 install 默认值与测试钉共用。 */

export const LIVE_RECHECK_INTERVAL_MS = 30000; /* 最坏 30s 感知过窗（≤60s 判据） */
export const LIVE_RECHECK_NEAR_MINUTES = 5; /* 临近窗口边界 ±5 分钟即全量重查 */

const liveTodayIso = (now) => new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);

const liveParseHm = (text) => {
  const [hours, minutes] = String(text || "").split(":").map(Number);
  return Number.isFinite(hours) && Number.isFinite(minutes) ? hours * 60 + minutes : NaN;
};

/* 课表窗口相位：future/now/past；非今天或缺起止时间返回 ""（不裁决）。
 * meeting.date 缺省视为今天；起止时间兼容两套命名（课表快照
 * start_time/end_time 与 live-page todayLiveMeetings 的 start/end）。 */
export function schedulePhase(meeting, now = new Date()) {
  const start = liveParseHm(meeting?.start_time ?? meeting?.start);
  const end = liveParseHm(meeting?.end_time ?? meeting?.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "";
  const date = String(meeting?.date || "") || liveTodayIso(now);
  if (date !== liveTodayIso(now)) return "";
  const nowMin = now.getHours() * 60 + now.getMinutes();
  if (nowMin < start) return "future";
  if (nowMin >= end) return "past";
  return "now";
}

/* 距最近窗口边界（start/end）的分钟数；非今天/缺时间返回 null。 */
export function scheduleBoundaryDistance(meeting, now = new Date()) {
  const start = liveParseHm(meeting?.start_time ?? meeting?.start);
  const end = liveParseHm(meeting?.end_time ?? meeting?.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const date = String(meeting?.date || "") || liveTodayIso(now);
  if (date !== liveTodayIso(now)) return null;
  const nowMin = now.getHours() * 60 + now.getMinutes();
  return Math.min(Math.abs(nowMin - start), Math.abs(nowMin - end));
}

/* 重估裁决：相位翻转（future→now→past）或目标身份变化，或临近窗口
 * 边界（±LIVE_RECHECK_NEAR_MINUTES）→ 全量重查；其余跳过。 */
export function liveRecheckDecision({
  meeting = null,
  meetingKey = "",
  now = new Date(),
  lastPhase = "",
  lastKey = "",
} = {}) {
  const phase = schedulePhase(meeting, now);
  const key = String(meetingKey || "");
  if (phase !== String(lastPhase || "") || key !== String(lastKey || "")) {
    return { action: "full", phase, key };
  }
  const distance = scheduleBoundaryDistance(meeting, now);
  if (distance !== null && distance <= LIVE_RECHECK_NEAR_MINUTES) {
    return { action: "full", phase, key };
  }
  return { action: "skip", phase, key };
}

/* ---- 单源一致态对账（SWEEPFIX-2 · SWEEP1-01）----
 * 病灶：直播卡/直播行两个状态源（课表事实 headline + 后端观测 detail）
 * 各自渲染、互斥性缺失——目标 meeting 明天开始时，上一场直播的 ended 观测
 * 仍照常上能力行/原因行，四行同屏互相矛盾（「按课表 明天 10:50 开始」×
 * 「直播已结束」×「可以在讲次列表中查看已授权回放」），学生无法判断这门课
 * 能不能看直播。对账规则（闭集）：ended 观测撞上「未开始的 meeting」（今天
 * 未到点或未来日期）→ 归位 upcoming，整卡只剩「还没开始」一个陈述——
 * ended 说的是上一场，不是下一节。其余组合不动：live+can_enter 永远优先
 * （绝不藏真实入口）；窗口内/已过窗的 ended 是后端新知（如提前结束），如实
 * 呈现；unknown/offline/stale 无时间性主张，不参与对账。 */
export function reconcileMeetingEndedState({ state = "", meeting = null, mode = "", now = new Date() } = {}) {
  const raw = String(state || "");
  if (raw !== "ended" || !meeting?.start_time) return raw;
  if (String(mode) === "later") return "upcoming";
  if (schedulePhase(meeting, now) === "future") return "upcoming";
  const date = String(meeting.date || "");
  if (date && date > liveTodayIso(now)) return "upcoming";
  return raw;
}

/* ---- 观测时间行人话化（SWEEPFIX-2 · SWEEP1-02）----
 * 病灶：讲次页直播条时间行裸渲染 starts_at/ends_at 的人话化结果——跨日的
 * 值只剩「7/28」，无单位无标签，学生读到等于没读。闭集改写：live 态照述
 * 本场窗口；其余态加「上次直播」前缀（观测窗口属于上一场，绝不发明
 * 「下一场」时间）；两端文本相同时只述一次；无观测时间诚实回退。
 * 时间格式由调用方注入（ui.js formatRelativeTime 家族），本模块保持零依赖。 */
export function liveObservationTimeText({ startsAt = "", endsAt = "", state = "", hasCourse = true, format = (v) => String(v || "") } = {}) {
  const fmt = typeof format === "function" ? format : (v) => String(v || "");
  const start = fmt(startsAt);
  const end = fmt(endsAt);
  const windowText = [start, end].filter(Boolean).join(" 至 ");
  if (!windowText) return hasCourse ? "时间尚未确认" : "选择已授权课程后自动确认";
  const once = start === end ? start : windowText;
  return String(state || "") === "live" ? once : `上次直播 ${once}`;
}

/* ---- 模糊态 × 课表事实改述（F6 · RR-PARK-1 P4）----
 * 病灶：直播状态未决（unknown/offline/stale）时整卡只会说「服务暂不可用/
 * 状态待确认」；夜里学校侧状态拿不到时，学生看到的是「服务坏了」的卡，
 * 可课表明明写着这节课此刻在进行。改述纪律：有课表事实（今天、有起止
 * 时间）就陈述课表相位，没有才落原中性卡；只陈述课表事实，绝不凭课表
 * 宣称「正在直播/可以进入」（那是后端确认态专属），也不把「状态没确认」
 * 说成「服务坏了」。 */
export function restateAmbiguousLiveState({
  statusState = "",
  meeting = null,
  now = new Date(),
} = {}) {
  const state = String(statusState || "");
  if (state !== "unknown" && state !== "offline" && state !== "stale") return null;
  const phase = schedulePhase(meeting, now);
  if (phase === "now") {
    return {
      title: "按课表正在进行中",
      body: "课表显示这节课此刻正在进行；直播状态还在确认，确认后这里就能进入。",
      actions: state === "unknown" ? Object.freeze([]) : Object.freeze(["recheck"]),
    };
  }
  if (phase === "future") {
    return {
      title: "还没到点",
      body: "课表显示这节课还没开始；到点前后这里会自动重查直播状态。",
      actions: Object.freeze([]),
    };
  }
  return null;
}

/* ---- 直播页逐码错误卡（§12.3 逐码人话文案表；family → 16 态归并） ----
 * expired=会话/凭证过期族；gate=7001 审核门；stream=取流/连接族；
 * denied=授权族；fatal=未闭集兜底。live_content_type_rejected 按 §12.3
 * 并入 live_stream_unavailable 文案渲染，闭集码保留诊断。 */
export const LIVE_ERROR_CARDS = Object.freeze({
  live_session_expired: Object.freeze({ family: "expired", title: "直播会话过期了", body: "挂起太久会话会自动失效，这是保护你的账号安全。点下面重新进入就行，进度不会丢。", actions: Object.freeze(["reenter"]) }),
  live_resource_expired: Object.freeze({ family: "expired", title: "直播需要重新连接", body: "这段直播的资源刚刚换了一版，重新进入就好。", actions: Object.freeze(["reenter"]) }),
  live_grant_invalid: Object.freeze({ family: "expired", title: "进入凭证过期了", body: "直播进入凭证有时效，重新确认一下课程状态即可。", actions: Object.freeze(["recheck"]) }),
  live_grant_identity_changed: Object.freeze({ family: "expired", title: "账号状态有变化", body: "当前账号状态和进入时不一样了，重新确认课程状态。", actions: Object.freeze(["recheck"]) }),
  live_playback_not_open: Object.freeze({ family: "gate", title: "直播还没开放", body: "学校对新内容有最长 24 小时的审核，这节课还没放行。可以先复习之前的讲次，过会儿再来。", actions: Object.freeze(["recheck", "lectures"]) }),
  live_upstream_rejected: Object.freeze({ family: "stream", title: "学校服务器刚拒绝了这次取流", body: "一般重新进入一次就好；如果连续出现，多半是网络路径的问题。", actions: Object.freeze(["reenter"]) }),
  live_upstream_unreachable: Object.freeze({ family: "stream", title: "连不上学校的直播服务器", body: "看起来是网络路径的问题。如果开了代理工具，请关掉 TUN 模式再试。", actions: Object.freeze(["reenter"]) }),
  live_stream_unavailable: Object.freeze({ family: "stream", title: "学校直播源暂时取不到画面", body: "刚取流时学校那边没给出画面，多数情况下重新进入一次就好。", actions: Object.freeze(["reenter"]) }),
  live_content_type_rejected: Object.freeze({ family: "stream", title: "学校直播源暂时取不到画面", body: "刚取流时学校那边没给出画面，多数情况下重新进入一次就好。", actions: Object.freeze(["reenter"]) }),
  live_network_interrupted: Object.freeze({ family: "stream", title: "直播连接已中断", body: "返回直播状态重新进入，可获取新的安全会话。开了代理 TUN 的话先关掉。", actions: Object.freeze(["reenter"]) }),
  live_decode_failed: Object.freeze({ family: "stream", title: "画面解码失败", body: "重新进入一般能恢复；连续出现的话可以稍后再试，或重启客户端。", actions: Object.freeze(["reenter"]) }),
  live_format_unsupported: Object.freeze({ family: "stream", title: "当前环境不支持安全直播", body: "请使用支持 HLS 的浏览器后重新进入直播。", actions: Object.freeze(["recheck"]) }),
  live_authorization_denied: Object.freeze({ family: "denied", title: "这门课没有直播权限", body: "当前账号没有这门课的直播访问权限；如果应该有，请联系老师或教务确认。", actions: Object.freeze(["recheck"]) }),
  live_authorization_revoked: Object.freeze({ family: "denied", title: "直播权限有变化", body: "这门课的直播访问权限发生了变化，重新确认后就能恢复。", actions: Object.freeze(["recheck"]) }),
  runtime_failed: Object.freeze({ family: "fatal", title: "出了个没预料到的状况", body: "重新进入一次试试；还不行请稍后再来，这不是你的操作问题。", actions: Object.freeze(["reenter"]) }),
});

/* 直播失败码解析（第廿六案同款语义，直播页版）：已知闭集码走本卡；
 * 存在但未闭集的码绝不静默吞掉——fatal 卡附诊断摘要行透出真实码；
 * 完全无码按连接中断诚实呈现。 */
export function resolveLiveErrorCard(code) {
  const failureCode = String(code || "").trim();
  const card = LIVE_ERROR_CARDS[failureCode];
  if (card) return { code: failureCode, ...card, diagnostic: "" };
  if (!failureCode) {
    return { code: "live_network_interrupted", ...LIVE_ERROR_CARDS.live_network_interrupted, diagnostic: "" };
  }
  return {
    code: failureCode,
    ...LIVE_ERROR_CARDS.runtime_failed,
    diagnostic: `诊断编码 ${failureCode}：不是你的操作问题，反馈时可以截图这一行。`,
  };
}

/* ---- 16 态中性态文案（L2 文案键 live.<态>.* 全部落此；错误态走
 * LIVE_ERROR_CARDS。{START}/{N} 为占位符，渲染方填充。 ---- */
export const PAGE_STATE_COPY = Object.freeze({
  idle: Object.freeze({ title: "今晚课表上没有直播课", body: "课表快照里今晚没有排直播。可以去课表确认，或看看最近几天的安排。", actions: Object.freeze(["timetable"]) }),
  upcoming: Object.freeze({ title: "还没到点", body: "这节课按课表 {START} 开始，开始前这里会保持安静。", actions: Object.freeze(["lectures"]) }),
  ready: Object.freeze({ title: "现在可以进入直播", body: "后端已确认直播可用。安全会话只在本机建立，随时可以退出。", actions: Object.freeze(["enter"]) }),
  connecting: Object.freeze({ title: "正在建立安全会话", body: "正在本机与直播服务建立会话，稍等一下。", actions: Object.freeze(["cancel"]) }),
  paused: Object.freeze({ title: "直播已暂停", body: "你离开了直播页，画面已暂停。回来点一下就继续。", actions: Object.freeze(["resume"]) }),
  buffering: Object.freeze({ title: "", body: "正在缓冲，内容没丢", actions: Object.freeze([]) }),
  behind: Object.freeze({ title: "", body: "回到直播中", actions: Object.freeze([]) }),
  weaknet: Object.freeze({ title: "网络不太顺", body: "刚才缓冲了几次。画面会自己恢复；持续卡顿的话可以稍后再来。", actions: Object.freeze(["dismiss"]) }),
  recovering: Object.freeze({ title: "正在恢复连接", body: "直播断了一下，正在重新建立会话（{N}）。", actions: Object.freeze(["retry-now"]) }),
  ended: Object.freeze({ title: "本节直播已结束", body: "可以在讲次列表里看已授权的回放。", actions: Object.freeze(["lectures"]) }),
});

/* 直播页 16 态闭集（L2 转移表）：错误面板唯一（D14），
 * 过渡态覆盖播放态，等待态中性视觉。 */
export const LIVE_PAGE_STATES = Object.freeze([
  "idle", "upcoming", "gate", "ready", "connecting", "playing", "paused",
  "buffering", "behind", "weaknet", "recovering", "expired", "stream",
  "ended", "denied", "fatal",
]);

/* ---- 直播二期（乙2）：视角条纯模型 ----
 * 后端 status.available_views 是 URL-free 闭集 id；前端只做闭集映射与
 * 可见性裁决，绝不发明视角。视角切换=新 grant 新会话（恰重建一次），
 * 失败回退默认视角；弱网（③级）时高亮纯声音档供一键切换。 */
export const LIVE_VIEW_META = Object.freeze({
  teacher: Object.freeze({ label: "教师画面", audioOnly: false }),
  student: Object.freeze({ label: "学生画面", audioOnly: false }),
  teacher_audio: Object.freeze({ label: "教师声音", audioOnly: true }),
  student_audio: Object.freeze({ label: "学生声音", audioOnly: true }),
});
export const DEFAULT_LIVE_VIEW = "student";

export function resolveViewBar({
  status = null,
  currentView = DEFAULT_LIVE_VIEW,
  switching = false,
  weaknet = false,
  hasSession = false,
} = {}) {
  const provided = Array.isArray(status?.available_views)
    ? status.available_views.map(String).filter((view) => Boolean(LIVE_VIEW_META[view]))
    : [];
  const active = LIVE_VIEW_META[currentView] ? currentView : DEFAULT_LIVE_VIEW;
  const activeInViews = provided.includes(active) ? active : DEFAULT_LIVE_VIEW;
  if (!hasSession || provided.length < 2) {
    return { visible: false, views: [], activeView: activeInViews, suggestAudio: false, switching: false };
  }
  return {
    visible: true,
    views: provided,
    activeView: activeInViews,
    /* 弱网建议：只有真的存在纯声音档才提示（闭集内裁决，不凭空推荐） */
    suggestAudio: weaknet === true && provided.some((view) => LIVE_VIEW_META[view].audioOnly),
    switching: switching === true,
  };
}

/* ---- 直播二期（乙3）：文稿侧板纯模型 ----
 * 双源抽象 platform|local：platform=平台原生文稿（增量端点，段含 start_ms
 * 尾水位），local=本地字幕文件（既有 subtitles/segments）。两边都归一为
 * {segments, done, hint}；行身份=start_ms（增量尾段按身份替换）。
 * 无文稿是闭集提示，绝不自动轮询（三触发源：进直播/手动开板/换目标）。 */
export function mergeTranscriptSegments(previous, incoming) {
  const merged = new Map((previous || []).map((segment) => [String(segment.start_ms), segment]));
  for (const segment of incoming || []) {
    if (!segment || !Number.isFinite(Number(segment.start_ms))) continue;
    merged.set(String(segment.start_ms), {
      start_ms: Number(segment.start_ms),
      end_ms: Number(segment.end_ms ?? 0),
      text: String(segment.text || ""),
    });
  }
  return [...merged.values()].sort((a, b) => a.start_ms - b.start_ms);
}

export function resolveTranscriptOutcome({ available = null, segments = null, failed = false, notFound = false } = {}) {
  if (notFound) return { retryable: false, segments: [], hint: "这个讲次没有可用的文稿。" };
  if (failed || available === false) return { retryable: true, segments: [], hint: "文稿暂时取不到，稍后再试一次。" };
  const list = Array.isArray(segments) ? segments : [];
  if (!list.length) return { retryable: false, segments: [], hint: "平台还没有生成这节课的文稿。" };
  return { retryable: false, segments: list, hint: "" };
}

