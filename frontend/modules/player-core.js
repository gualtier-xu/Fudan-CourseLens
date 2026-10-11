import { apiV3, postV3, deleteV3 } from "./api.js";
import { $, bumpLocalMetric, clear, setBusy, toast, hasOpenDialog, pageShortcutBlockedByTarget, releasePointerActivatedFocus } from "./ui.js";
import { attachDropdown, syncDropdown } from "./dropdown.js";

let saveTimer = 0;
/* 进度写侧讲次绑定（B1 根修）：saveProgress 只承认「播放器里当前加载媒体」
   的进度。读侧 hydratePersistedProgress 有四重归属守卫，写侧对称面由本值承担：
   store.set 同步通知 loadLecture，本值与其同帧更新；切讲间隙到点的遗留保存
   （旧 timer、load() 引发的杂散 pause/ended）在此被归属拒绝。 */
let mediaSubId = "";
/* N6L S1 U3（还债减法）：直播播放整体移交独立直播页（live-player.js）——
   本模块只服务回放（lecture）语义，直播播放状态键/live-play 事件/追帧层/
   直播恢复卡全部退役。 */
let playbackKind = "none";
let transcriptTimed = false;
/* W9：transcriptTimed 已确认所属的讲次身份。字幕读取按讲次发生：讲次切换后、
   新讲次的字幕读取返回前，旧讲次的确认值绝不生效（生成笔记先按未确认禁用）。 */
let transcriptTimedSubId = "";
let subtitleTextTrack = null;
let subtitlesOn = true;
let lastOverlayText = "";
let subtitleWatchActive = false;
let subtitleWatchTimer = 0;
/* loadLecture 是模块级订阅回调（无闭包 store），安装时保存引用 */
let playerStore = null;

// ---- P1-C 动作/播放连续性（跨一次重认证，恰一次） ----
/* pendingProtectedEnqueue：被闭集 401（fudan_login_required）拒绝的受保护提交；
   只保留确定未被后端受理的失败（401），timeout/网络错误绝不保留（可能已受理，
   自动重试会重复提交任务）。重认证成功后续提恰一次。 */
let pendingProtectedEnqueue = null;
/* resumePlaybackState：恢复重载前捕获的播放位置（一次性）；恢复面板的
   “重新获取播放授权”与重认证后的自动续播共用。 */
let resumePlaybackState = null;
/* ---- 观看进度恢复（GET /api/v3/progress）：讲次加载后在元数据可 seek 时
   一次性回到持久化位置。只接受同 sub_id/course_id 的成功读取；零值、未知
   时长、已完成、错讲次与失败读取一律诚实降级为从头播放。重认证恢复
   （resumePlaybackState）与用户加载期的主动 seek 都会让位，绝不二次 seek。 ---- */
let persistedRestore = null;
let restorePendingForLoad = false;
/* 每个失败回合至多一次自动续播：新失败（showMediaFailure）重新武装。
   只有授权类失败（授权需更新/来源授权可能过期）可自动续播——重认证对
   网络中断/解码失败无意义；auth 轮询持续返回 ready 时也绝不成循环。 */
let autoContinuityArmed = false;
const AUTO_CONTINUITY_CODES = Object.freeze(new Set([
  "media_authorization_required",
  "media_source_unavailable",
]));
/* MEDIA-001-20261001：code=4 兜底卡的闭集细分映射——本地服务把每次媒体开流
   结局记成闭集码（GET /api/v3/media/stream-status，成功清空）。前端先按既有
   合成卡渲染，闭集证据到场后同回合原位替换。只有拿到独立处方的原因才细分：
   校外不可达（换网络/学校 VPN 才是解）与授权类（重认证是解）；其余闭集码
   与取不到证据时一律保留通用兜底卡。 */
const MEDIA_STREAM_FAILURE_REFINEMENTS = Object.freeze({
  upstream_unreachable: "media_source_unreachable",
  auth_required: "media_authorization_required",
});
let mediaFailureRound = 0;
let autoContinuityEligible = false;
/* P3-B/W1：进行中的 seek 意图。错误可能把 currentTime 回卷到 seek 前的位置，
   恢复重载时以 seek 目标优先，绝不丢用户的跳转意图；意图带新鲜度边界——
   播放一旦真实推进（timeupdate）即过期，绝不回卷到早已越过的陈旧 seek 点，
   只有 seek 进行中被错误打断（无推进）时才存活到恢复重载。 */
let pendingSeekTarget = null;
/* P3-B：身份边界闭集——这些 auth 快照码表示目录身份无法确认/与原账号不一致；
   属于另一身份作用域的待办动作必须立即清除，绝不跨账号续提任务或续播。 */
const IDENTITY_MISMATCH_CODES = Object.freeze(new Set([
  "catalog_identity_mismatch",
  "catalog_identity_invalid",
]));

function clearContinuityState() {
  pendingProtectedEnqueue = null;
  resumePlaybackState = null;
  pendingSeekTarget = null;
  autoContinuityArmed = false;
  autoContinuityEligible = false;
}

// ---- 一方控制台瞬时状态（安装闭包内的监听负责读写；loadLecture / 清理统一复位） ----
let timelineScrubbing = false;
let rightHoldTimer = 0;
/* 长按右方向的待决短按（B 站式 keyup 裁决）：按下只武装计时器不 seek——
   未达阈值松开 = 短按，松开那一刻才快进 5 秒；达到阈值进入临时倍速即消费
   这次按压。绝不「先跳一次再倍速」。 */
let pendingRightSeek = false;
let holdRateActive = false;
let userRateBeforeHold = 1;
/* 安装时探测控制台 DOM：真实页面恒有（workbench 锁定）；无控制台的合成壳
   （如可用性行为 harness 的精简 fixture）跳过 deck 接线与同步，不致命 */
let controlsDeckPresent = false;

// ---- S09-D 沉浸式控制台瞬时状态（安装时复位；loadLecture / 清理统一复位） ----
/* 自动隐藏：指针静止 PLAYER_IDLE_HIDE_MS 后收起控制台；播放与暂停同一空闲
   语义（PLAYER-UX-1①：暂停不再永久钉住，超时同样收起）；钉住条件
   （结束/缓冲/恢复面板/拖动/键盘焦点在播放器内/音量面展开/悬停控制台）恒定可见 */
let idleTimer = 0;
/* 指针悬停在控制台甲板上（仅鼠标；触控/笔无悬停语义）：悬停期间恒可见 */
let deckHover = false;
/* 只有 :focus-visible（键盘来源）的焦点才钉住控制台；鼠标点击导致的焦点
   移动不算键盘焦点，不会把控制台永久钉住 */
let keyboardFocusInsidePlayer = false;
let mediaBuffering = false;
/* 音量面三输入源（悬停/焦点/点击钉住）派生展开态；收起绝不移动焦点 */
let volumeHover = false;
let volumeFocus = false;
let volumeClickPin = false;
let volumeOpen = false;
/* 静音不得改写真实音量：记忆最后一个非零音量，取消静音时恢复；
   从无非零值时才用保守默认 0.5 */
let lastAudibleVolume = 0;

const PLAYER_SEEK_SECONDS = 5;
/* NIGHT2-G-A（P16 键位包）：J/L 大步跳转与 </> 倍速档——对照 B 站/VLC 肌肉记忆 */
const PLAYER_ALT_SEEK_SECONDS = 10;
/* N5PR-P2：步进集与 select 档位集恒等（九档 0.5-3.0，含 1.75——旧五档缺 1.75
   已实测实锤：select 可选而 </> 三连后直跳 2）。 */
const PLAYER_SPEED_STEPS = Object.freeze([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3]);
/* 按住右方向进入临时快进的小常量阈值：短按（<阈值）只是 5 秒 seek。
   N5PR-P2：临时倍速 2→3，对齐 B 站长按肌肉记忆。 */
const PLAYER_HOLD_THRESHOLD_MS = 400;
const PLAYER_HOLD_RATE = 3;
/* 播放中指针静止多久后收起控制台（单一计时器，指针活动重置；
   ~1.5s 是 S10-A 契约值，mjs 行为 harness 与 workbench 源码锁同步此值） */
const PLAYER_IDLE_HIDE_MS = 1500;
/* 快捷键让位选择器（FOCUS-DRIFT-1 细化，政策全文见 docs/technical/focus-policy.md）：
   「自带编辑/方向语义」的面（字段/滑杆/下拉源/菜单/标签页/选项）整块让位；
   button/a 不再整块让位——聚焦时仅激活键（Space/Enter）留给原生激活（键盘可达
   硬门），其余键透传播放快捷键，点击按钮后快捷键即刻存活。滚轮悬停让位保留
   原全集（指针悬停语义与焦点无关：悬在按钮上滚轮不该动音量，悬在面板/滑杆上
   更不该动）。 */
const PLAYER_SHORTCUT_EDITABLE = "input, select, textarea, [contenteditable='true'], [role='menu'], [role='listbox'], [role='tab'], [role='option']";
const PLAYER_HOVER_YIELD_SELECTOR = "input, select, textarea, button, a, [contenteditable='true'], [role='menu'], [role='listbox'], [role='tab'], [role='option']";

const RECOVERY_DETAILS = Object.freeze({
  media_authorization_required: Object.freeze({
    title: "播放授权需要更新",
    impact: "重新认证后可以再次获取这次回放的播放授权。",
    actions: Object.freeze(["login"]),
  }),
  media_network_interrupted: Object.freeze({
    title: "播放连接已中断",
    impact: "可以重试并重新获取这次回放的播放授权。若代理工具开启了 TUN 模式，请关闭 TUN 后再试。",
    actions: Object.freeze(["retry-media"]),
  }),
  media_decode_failed: Object.freeze({
    title: "当前媒体无法继续解码",
    impact: "可以重新加载媒体；若仍失败，请稍后再试。",
    actions: Object.freeze(["retry-media"]),
  }),
  media_source_unavailable: Object.freeze({
    title: "当前媒体暂不可用",
    impact: "播放授权可能已过期。请先重试，仍失败时重新认证。若代理工具开启了 TUN 模式，请关闭 TUN 后再试。",
    actions: Object.freeze(["retry-media", "login"]),
  }),
  /* MEDIA-001-20261001：校外场景细分卡——服务端闭集结局码 upstream_unreachable
     （校外实测 ConnectionError：媒体主机从该网络直连不到）。诚实处方=换网络，
     重登录与重签对它无意义，动作只留 retry。 */
  media_source_unreachable: Object.freeze({
    title: "这个网络看不了这节回放",
    impact: "回放视频放在学校的媒体服务器上，你现在的网络直连不到它（校外网络通常也连不到）。请换到校园网，或先连上学校提供的 VPN/WebVPN，然后点「重新获取播放授权」再试。用着代理工具的话，请先关闭 TUN 模式；想确认网络状况，也可以在设置页的「复旦课程平台」卡里运行一次连接诊断。",
    actions: Object.freeze(["retry-media"]),
  }),
  /* MEDIA-VPN-1-20261001：第三细分（同一 stream-status 回读的 atrust 三态）。
     未装→官方入口引导；在位→确认接入+设置开关指路；未知→保留上行既有卡。 */
  media_source_unreachable_vpn_missing: Object.freeze({
    title: "还没检测到学校 VPN 客户端",
    impact: "回放视频放在只有校园网络能直达的服务器上，校外看课的官方方式是学校的 VPN 客户端（深信服 aTrust）。你这台电脑上暂时没检测到它：请到学校官方入口 vpn.fudan.edu.cn 下载安装，用学号登录并连接后，点「重新获取播放授权」再试。",
    actions: Object.freeze(["retry-media"]),
  }),
  media_source_unreachable_vpn_present: Object.freeze({
    title: "检测到学校 VPN，请确认已连接",
    impact: "检测到学校 VPN（深信服 aTrust）：请确认客户端已登录接入后重试；若已接入仍看不了，可在设置开启「媒体流走系统代理」后再试一次。",
    actions: Object.freeze(["retry-media"]),
  }),
  media_format_unsupported: Object.freeze({
    title: "当前环境不支持此媒体",
    impact: "请使用支持该媒体格式的浏览器，或稍后再试。",
    actions: Object.freeze([]),
  }),
});

const RECOVERY_LABELS = Object.freeze({
  "retry-media": "重新获取播放授权",
  login: "重新认证",
});

// ---- 一方控制台：时间与状态格式化（绝不向用户显示 NaN/Infinity） ----

function finiteDuration(player) {
  const value = Number(player.duration);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function seekableEnd(player) {
  try {
    const seekable = player.seekable;
    if (seekable && seekable.length > 0) {
      const end = Number(seekable.end(seekable.length - 1));
      if (Number.isFinite(end) && end >= 0) return end;
    }
  } catch {
    /* seekable 不可用时退回 duration 钳制 */
  }
  return finiteDuration(player);
}

function formatPlayerTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "0:00";
  const total = Math.floor(value);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}`
    : `${minutes}:${secs}`;
}

function announcePlayerStatus(message, osdMs = PLAYER_OSD_HIDE_MS) {
  if (!controlsDeckPresent) return;
  $("player-ctrl-status").textContent = message;
  /* N5PR-P3：一处文案双通道——SR 状态区与视觉轻提示同源，绝不各写一份
     （视觉层 aria-hidden，不产生第二次 SR 播报）。 */
  showPlayerOsdHint(message, osdMs);
}

// ---- N5PR-P3 操作反馈 OSD：中央播放/暂停 pulse + 轻提示。纯展示层：
// ---- 无焦点、不进 Tab 序、pointer-events:none，显隐绝不参与控制台钉住
// ---- （chromePinned 语义不动，1500ms 恒等钉不受影响）。
const PLAYER_OSD_HIDE_MS = 600;
/* U②：按住 3× 的提示多留 40%（840ms）——按住期间它是唯一状态读数 */
const PLAYER_OSD_HOLD_HINT_MS = Math.round(PLAYER_OSD_HIDE_MS * 1.4);
let osdPulseTimer = 0;
let osdHintTimer = 0;

function clearOsdTimers() {
  if (osdPulseTimer) {
    window.clearTimeout(osdPulseTimer);
    osdPulseTimer = 0;
  }
  if (osdHintTimer) {
    window.clearTimeout(osdHintTimer);
    osdHintTimer = 0;
  }
}

function hidePlayerOsd() {
  const center = $("player-osd-center");
  const hint = $("player-osd-hint");
  if (center) center.hidden = true;
  if (hint) hint.hidden = true;
}

/* 中央 pulse：playing=true 显示播放图标（刚按下播放）；连击重置计时。 */
function osdPulse(playing) {
  if (!controlsDeckPresent) return;
  const center = $("player-osd-center");
  if (!center) return;
  center.dataset.state = playing ? "play" : "pause";
  center.hidden = false;
  if (osdPulseTimer) window.clearTimeout(osdPulseTimer);
  osdPulseTimer = window.setTimeout(() => {
    osdPulseTimer = 0;
    const node = $("player-osd-center");
    if (node) node.hidden = true;
  }, PLAYER_OSD_HIDE_MS);
}

function showPlayerOsdHint(message, osdMs = PLAYER_OSD_HIDE_MS) {
  if (!controlsDeckPresent) return;
  const hint = $("player-osd-hint");
  if (!hint) return;
  hint.textContent = message;
  hint.hidden = false;
  if (osdHintTimer) window.clearTimeout(osdHintTimer);
  osdHintTimer = window.setTimeout(() => {
    osdHintTimer = 0;
    const node = $("player-osd-hint");
    if (node) node.hidden = true;
  }, osdMs);
}

// ---- N5PR-P4 中央缓冲指示：waiting >300ms 仍未恢复才显示（防闪）；
// ---- 恢复（playing/canplay）即收；恢复面板可见时互斥不叠加；
// ---- 计时器随 loadLecture/清理确定性复位（对齐 idleTimer 纪律）。
const PLAYER_SPINNER_DELAY_MS = 300;
let spinnerTimer = 0;

function clearBufferSpinner() {
  if (spinnerTimer) {
    window.clearTimeout(spinnerTimer);
    spinnerTimer = 0;
  }
  const spinner = $("player-stage-spinner");
  if (spinner) spinner.hidden = true;
}

// ---- N5PR-P4 进度条时间气泡：悬停/拖动目标时刻（只读几何，绝不写
// ---- currentTime）；rAF 节流 ≤1 帧；直播/不可 seek 退避隐藏。
let bubbleRaf = 0;
let pendingBubbleRatio = null;

function hideTimelineBubble() {
  if (bubbleRaf) {
    window.cancelAnimationFrame?.(bubbleRaf);
    bubbleRaf = 0;
  }
  pendingBubbleRatio = null;
  const bubble = $("player-timeline-bubble");
  if (bubble) bubble.hidden = true;
}

function syncPlayerTimeline() {
  if (!controlsDeckPresent) return;
  const player = $("player-stage");
  const timeline = $("player-ctrl-timeline");
  const fill = $("player-timeline-fill");
  const duration = finiteDuration(player);
  const current = Math.max(Number(player.currentTime) || 0, 0);
  /* 直播优先：直播态即使短暂报告有限时长也不可拖动进度（诚实禁用） */
  if (duration <= 0) {
    $("player-ctrl-elapsed").textContent = "0:00";
    $("player-ctrl-duration").textContent = "0:00";
    timeline.disabled = true;
    timeline.max = "0";
    timeline.value = "0";
    timeline.setAttribute("aria-valuetext", "0:00");
    fill.style.setProperty("--play-ratio", "0");
    const liveBuffered = $("player-timeline-buffered");
    if (liveBuffered) liveBuffered.style.setProperty("--buffer-ratio", "0");
    return;
  }
  $("player-ctrl-elapsed").textContent = formatPlayerTime(current);
  $("player-ctrl-duration").textContent = formatPlayerTime(duration);
  timeline.disabled = playbackKind === "none";
  timeline.max = String(duration);
  timeline.setAttribute("aria-valuetext", `${formatPlayerTime(current)} / 共 ${formatPlayerTime(duration)}`);
  const clamped = Math.min(current, duration);
  if (!timelineScrubbing) timeline.value = String(clamped);
  /* 单一几何源：只写 0..1 进度比；填充条的端点内缩（thumb 半径）与宽度
     全部由 CSS 的 --player-thumb-size 模型推导，0%/100% 处填充右缘与
     thumb 圆心精确重合（1440px 与 390px、指针与键盘一致） */
  fill.style.setProperty("--play-ratio", String(Math.min(Math.max(clamped / duration, 0), 1)));
  /* U7②：缓冲段可视化——取最靠后的已缓冲区间终点，宽度随 --buffer-ratio */
  const bufferedBar = $("player-timeline-buffered");
  if (bufferedBar) {
    const ranges = player.buffered;
    const bufferedEnd = ranges && ranges.length ? Number(ranges.end(ranges.length - 1)) || 0 : 0;
    bufferedBar.style.setProperty("--buffer-ratio", String(Math.min(Math.max(bufferedEnd / duration, 0), 1)));
  }
  syncPlayerTimelineChaptersFlush();
}

function setPlayerControlsVisible(visible) {
  if (!controlsDeckPresent) return;
  $("player-controls").hidden = !visible;
  [
    "player-ctrl-play", "player-ctrl-mute", "player-ctrl-volume",
    "player-ctrl-speed", "player-ctrl-theatre", "player-ctrl-fullscreen",
  ].forEach((id) => {
    $(id).disabled = !visible;
  });
  syncDropdown($("player-ctrl-speed")); /* 甲2：自绘触发钮禁用态跟随 */
}

function syncPlayButton() {
  if (!controlsDeckPresent) return;
  const playing = $("player-stage").paused === false;
  const button = $("player-ctrl-play");
  button.dataset.state = playing ? "playing" : "paused";
  button.setAttribute("aria-label", playing ? "暂停" : "播放");
}

function volumeLevelState(player) {
  const volume = Number(player.volume) || 0;
  if (player.muted === true || volume === 0) return "muted";
  return volume <= 0.5 ? "low" : "sound-on";
}

function syncVolumeControls() {
  if (!controlsDeckPresent) return;
  const player = $("player-stage");
  const volume = Math.min(Math.max(Number(player.volume) || 0, 0), 1);
  if (player.muted !== true && volume > 0) lastAudibleVolume = volume;
  const state = volumeLevelState(player);
  const muteButton = $("player-ctrl-mute");
  muteButton.dataset.state = state;
  muteButton.setAttribute("aria-label", state === "muted" ? "取消静音" : "静音");
  /* 滑杆恒显示真实媒体音量：静音不改写显示（记忆不丢失）；
     可达值是诚实的 0-100 百分比 */
  const slider = $("player-ctrl-volume");
  slider.value = String(volume);
  slider.setAttribute("aria-valuetext", `${Math.round(volume * 100)}%`);
}

// ---- S09-D 沉浸式控制台：单一自动隐藏引擎 ----
/* 钉住条件：结束/缓冲中/恢复面板可见/时间轴拖动/焦点在播放器内/音量面展开/
   悬停控制台。PLAYER-UX-1①：暂停不再属于钉住条件——暂停与播放共用同一空闲
   计时（超时收起；任意指针/键盘输入唤回），拖动/悬停/焦点豁免面保住可操作性。
   无媒体（deck 整体 hidden）时不参与。 */
function chromePinned() {
  const player = $("player-stage");
  if (player.ended === true || mediaBuffering) return true;
  if (timelineScrubbing || keyboardFocusInsidePlayer || volumeOpen || deckHover) return true;
  const recovery = $("player-recovery");
  if (recovery && recovery.hidden === false) return true;
  return false;
}

function setChromeVisible(visible) {
  if (!controlsDeckPresent) return;
  $("player-stage-shell").dataset.chrome = visible ? "visible" : "hidden";
  $("player-controls").dataset.chromeState = visible ? "visible" : "hidden";
}

function chromeVisible() {
  if (!controlsDeckPresent) return true;
  return $("player-stage-shell").dataset.chrome !== "hidden";
}

function clearIdleTimer() {
  if (idleTimer) {
    window.clearTimeout(idleTimer);
    idleTimer = 0;
  }
}

/* 依据钉住状态收敛：钉住 → 立即可见；否则（播放中）装一个 2.5s 收起计时器。
   指针进入/移动/离开/取消与焦点、拖动、音量面变化都走这里，任何路径都不会把
   控制台滞留在错误状态。 */
function refreshChrome() {
  if (!controlsDeckPresent) return;
  clearIdleTimer();
  if (chromePinned()) {
    setChromeVisible(true);
    return;
  }
  idleTimer = window.setTimeout(() => {
    idleTimer = 0;
    /* 到点复核钉住：武装期间发生暂停/聚焦/缓冲等变化时不误收起 */
    setChromeVisible(chromePinned());
  }, PLAYER_IDLE_HIDE_MS);
}

function pokeChrome() {
  setChromeVisible(true);
  refreshChrome();
}

function syncVolumeOpen() {
  if (!controlsDeckPresent) return;
  volumeOpen = volumeHover || volumeFocus || volumeClickPin;
  $("player-volume").dataset.open = volumeOpen ? "true" : "false";
  refreshChrome();
  armVolumeAutoClose();
}

/* 补充F：音量面 3s 无交互自动收口。考据结论：click/焦点钉住路径此前没有
   任何超时解除（面板常开到切讲次为止），且点击后焦点残留在滑杆上（input
   编辑态让位），空格/左右键全被拦截——U③（渐隐 150→300ms）只是过渡时长，
   与本缺陷无关。任何音量组状态变化（含滑杆拖动/进出/焦点）都会重臂计时；
   到点先归还焦点（组内 activeElement blur，空格/方向键即刻回到页面级
   快捷键），再解除三路钉住并重算 chrome 钉住。 */
const VOLUME_AUTO_CLOSE_MS = 3000;
let volumeAutoCloseTimer = 0;

/* 补充G：控制块焦点自动归还（F 模式全覆盖，一笔扫清全部可聚焦控件）——
   播放/静音/音量滑杆/倍速 select/没听懂/字幕/字幕 chip/画中画/影院/全屏/
   时间轴滑杆捕获的指针焦点，3s 无交互自动归还页面级（编辑态让位不再拦截
   空格/左右键）。任何控件状态变化（focusin/change/input/pointermove/
   pointerdown）重臂。键盘来源焦点（<html data-input="key">，Tab 导航/
   方向键调滑杆）绝不自动归还——三态焦点政策的键盘态不抢。 */
const CONTROLS_FOCUS_RELEASE_MS = 3000;
let controlsFocusReleaseTimer = 0;
/* 补充I：选定即完成——倍速选定（change）与时间轴拖动释放（指针路径）
   立即归还焦点，不等 3s；3s 计时器只管「展开未选/悬停未动」场景。
   键盘态（data-input=key）不归还：方向键连续调速/scrub 绝不打断。 */
function releaseControlsFocusNow() {
  if (controlsFocusReleaseTimer) {
    window.clearTimeout(controlsFocusReleaseTimer);
    controlsFocusReleaseTimer = 0;
  }
  const root = document.documentElement;
  if (root && root.dataset && root.dataset.input === "key") return;
  const current = document.activeElement;
  const deck = $("player-controls");
  if (deck && current && typeof deck.contains === "function" && deck.contains(current)) {
    current.blur?.();
  }
}
function armControlsFocusRelease() {
  if (!controlsDeckPresent) return;
  if (controlsFocusReleaseTimer) {
    window.clearTimeout(controlsFocusReleaseTimer);
    controlsFocusReleaseTimer = 0;
  }
  const root = document.documentElement;
  if (root && root.dataset && root.dataset.input === "key") return; /* 键盘导航不抢 */
  const active = document.activeElement;
  const deck = $("player-controls");
  if (!deck || !active || typeof deck.contains !== "function" || !deck.contains(active)) return;
  controlsFocusReleaseTimer = window.setTimeout(() => {
    controlsFocusReleaseTimer = 0;
    const current = document.activeElement;
    const deckNow = $("player-controls");
    if (deckNow && current && typeof deckNow.contains === "function" && deckNow.contains(current)) {
      current.blur?.(); /* 焦点归还：页面级快捷键即刻可用 */
    }
  }, CONTROLS_FOCUS_RELEASE_MS);
}
function armVolumeAutoClose() {
  if (!controlsDeckPresent) return;
  if (volumeAutoCloseTimer) {
    window.clearTimeout(volumeAutoCloseTimer);
    volumeAutoCloseTimer = 0;
  }
  if (!volumeHover && !volumeFocus && !volumeClickPin) return;
  volumeAutoCloseTimer = window.setTimeout(() => {
    volumeAutoCloseTimer = 0;
    const group = $("player-volume");
    if (group && group.contains(document.activeElement)) {
      document.activeElement.blur?.();
    }
    volumeClickPin = false;
    volumeHover = false;
    volumeFocus = false;
    syncVolumeOpen();
  }, VOLUME_AUTO_CLOSE_MS);
}

function syncFullscreenButton() {
  if (!controlsDeckPresent) return;
  const shell = $("player-stage-shell");
  const active = Boolean(shell) && document.fullscreenElement === shell;
  const button = $("player-ctrl-fullscreen");
  button.setAttribute("aria-pressed", String(active));
  button.setAttribute("aria-label", active ? "退出全屏" : "进入全屏");
}

function syncPipButton() {
  if (!controlsDeckPresent) return;
  const active = document.pictureInPictureElement === $("player-stage");
  const button = $("player-ctrl-pip");
  button.setAttribute("aria-pressed", String(active));
  button.setAttribute("aria-label", active ? "退出画中画" : "进入画中画");
}

function syncSubtitleButton() {
  if (!controlsDeckPresent) return;
  const button = $("player-ctrl-subtitle");
  const visible = Boolean(subtitleTextTrack) && playbackKind === "lecture";
  const pressed = subtitlesOn === true;
  const signature = `${visible ? "1" : "0"}${pressed ? "1" : "0"}`;
  if (button.dataset.sync === signature) return;
  button.dataset.sync = signature;
  button.hidden = !visible;
  button.setAttribute("aria-pressed", String(pressed));
  button.setAttribute("aria-label", pressed ? "关闭字幕" : "开启字幕");
}

/* N5PR-P3 补记（D8）：字幕生成中 chip——数据源=共享 store 的 tasks 快照
   （tasks-drawer 经 SSE 对账后 store.set("tasks")，播放器零新请求零新协议）。
   只认当前讲次的 subtitle 活动任务；无可靠进度=诚实只写「字幕生成中」。 */
const SUBTITLE_TASK_ACTIVE_STATES = Object.freeze(new Set([
  "queued", "awaiting_payload", "running", "canceling", "pausing", "paused",
]));

function syncSubtitleChip() {
  if (!controlsDeckPresent) return;
  const chip = $("player-subtitle-chip");
  if (!chip) return;
  const lecture = playerStore?.activeLecture;
  const subId = String(lecture?.sub_id || "");
  const tasks = Array.isArray(playerStore?.tasks) ? playerStore.tasks : [];
  const active = tasks.find((task) => task?.kind === "subtitle"
    && SUBTITLE_TASK_ACTIVE_STATES.has(String(task?.state || ""))
    && Boolean(subId)
    && String(task?.sub_id || "") === subId);
  if (!active) {
    chip.hidden = true;
    return;
  }
  /* SWEEPFIX-R2 W6b（SWEEPFIX-3 跨域移交单，T6 同族死路读法）：public_task
     把 completed/total 平铺在任务顶层（http_api.py public_task），无嵌套
     progress 对象——旧 `active?.progress?.completed` 恒 undefined，「字幕生成中
     X%」分支不可达，生产 chip 恒无百分比（等待可视化降级）。改顶层合同键。 */
  const completed = Number(active?.completed);
  const total = Number(active?.total);
  chip.textContent = Number.isFinite(completed) && Number.isFinite(total) && total > 0
    ? `字幕生成中 ${Math.min(100, Math.round((completed / total) * 100))}%`
    : "字幕生成中";
  chip.hidden = false;
}

/* ---- D2「没听懂」一键难点标记：当前时刻落既有 bookmarks 底座
   （POST /api/v3/bookmarks, note=「没听懂」）；同 5 秒窗口去重提示；
   时间轴独立标记层（.player-timeline-flags），点击跳回，讲次切换清空。
   学习桌书签列表经 courselens:bookmarks-changed 通知刷新（study.js 消费）。 */
const NOT_UNDERSTOOD_DEDUPE_MS = 5000;
let notUnderstoodLastAt = 0;
let pendingNotUnderstoodFlags = [];
/* U⑤：解释链降级闸——explain 接口形状不符预期/未配置时置位，此后悬停
   菜单只出「懂了」（留痕=本模块标记，结果文件记档，不静默恢复）。 */
let notUnderstoodExplainBroken = false;
/* PLAYER-UX-1④：书签变更收敛去抖计时（安装/清理/loadLecture 复位） */
let bookmarkSyncTimer = 0;

function syncNotUnderstoodButton() {
  if (!controlsDeckPresent) return;
  const button = $("player-ctrl-bookmark");
  if (button) button.hidden = playbackKind !== "lecture";
  const insightOpen = $("player-insight-open");
  if (insightOpen) insightOpen.hidden = playbackKind !== "lecture";
  /* 乙-2（#33）：洞察说明行跟随入口钮的可用态，不再常驻 */
  const insightHint = $("insight-hint");
  if (insightHint) insightHint.hidden = playbackKind !== "lecture";
  /* AS5 U2：抹除钮只在讲次态且洞察未显式关闭时出现 */
  const insightErase = $("insight-erase");
  if (insightErase) insightErase.hidden = playbackKind !== "lecture" || !insightEnabled();
}

/* SWEEPFIX-1 C2（化身走查 SWEEP1-C2）：play() 与紧随的 pause()/load() 之间的
   浏览器级竞态（「The play() request was interrupted by a call to pause()」族）
   是预期无害结局——后到的用户意图（暂停/切讲）本就应当赢，媒体元素规范以
   AbortError 拒绝先到的 play()。此前「点击标记/标签/刻度/章节跳回并续播」四条
   路径把 play() Promise 裸抛，竞态窗口落在上面就产生未捕获拒绝（pageerror
   噪音，SWEEP1 风暴实测 ×2）。本封装只消音 AbortError 族；其余拒绝（如自动
   播放政策 NotAllowedError）维持既有未处理语义，零行为变化。 */
function playQuietly(player) {
  const attempt = typeof player?.play === "function" ? player.play() : undefined;
  if (!attempt || typeof attempt.catch !== "function") return;
  attempt.catch((error) => {
    if (String(error?.name || "") === "AbortError") return;
    throw error;
  });
}

function renderNotUnderstoodFlag(startMs, bookmarkId = "") {
  const mount = $("player-timeline-flags");
  const player = $("player-stage");
  const duration = finiteDuration(player);
  if (!mount) return;
  if (!(duration > 0)) {
    pendingNotUnderstoodFlags.push(startMs);
    return;
  }
  const marker = document.createElement("div");
  marker.className = "player-flag-marker";
  marker.style.setProperty("--chapter-ratio", String(Math.min(Math.max(startMs / (duration * 1000), 0), 1)));
  const dot = document.createElement("button");
  dot.type = "button";
  dot.className = "player-flag-dot";
  dot.title = `没听懂 · ${formatPlayerTime(startMs / 1000)}`;
  dot.setAttribute("aria-label", `跳回没听懂的位置 ${formatPlayerTime(startMs / 1000)}`);
  dot.addEventListener("click", () => {
    player.currentTime = startMs / 1000;
    playQuietly(player);
  });
  marker.append(dot);
  if (bookmarkId) {
    /* PLAYER-UX-1④：标记携带书签身份——学习桌列表删除后按 id 修剪会话标记 */
    marker.dataset.bookmarkId = String(bookmarkId);
    marker.append(buildNotUnderstoodMenu(bookmarkId, marker));
  }
  mount.append(marker);
  return marker;
}

/* U⑤：悬停两小钮——「解释」走既有书签解释 AI 链（N5A-P4 日预算闸复用）、
   「懂了」= resolution resolved 后标记从热度带收起（书签数据本机保留，
   数据页一键抹除通道不变）。explain 形状不符预期 → 降级仅「懂了」并留痕。 */
function buildNotUnderstoodMenu(bookmarkId, marker) {
  const menu = document.createElement("span");
  menu.className = "player-flag-menu";
  menu.setAttribute("role", "group");
  menu.setAttribute("aria-label", "没听懂标记操作");
  if (!notUnderstoodExplainBroken) {
    const explain = document.createElement("button");
    explain.type = "button";
    explain.className = "player-flag-action";
    explain.textContent = "解释";
    explain.addEventListener("click", () => {
      void postV3("bookmarks/explain", { bookmark_id: bookmarkId }).then((value) => {
        const bookmark = value?.bookmark;
        if (!bookmark || typeof bookmark !== "object") {
          notUnderstoodExplainBroken = true;
          menu.remove();
          toast("解释暂时用不了，标记还在，回看时可在书签区重试", "error");
          return;
        }
        if (String(bookmark.explanation?.mode || "") === "declined") {
          /* 车道C卡9实锤：declined 唯一生产者=资料不足分支（额度门已随
             AS3/43案整链移除）——旧「额度用完，明天恢复」双重失实：真实
             原因=缺字幕依据，恢复方式=生成本讲字幕。 */
          toast("这条讲解暂时缺少可依据的字幕内容。请先为本讲生成字幕，再重新提问。", "checking");
          return;
        }
        toast("正在生成解释，稍后在讲次的疑问解释里看", "ready");
        window.dispatchEvent(new Event("courselens:bookmarks-changed"));
      }).catch(() => {
        notUnderstoodExplainBroken = true;
        menu.remove();
        toast("解释暂时用不了，标记还在，回看时可在书签区重试", "error");
      });
    });
    menu.append(explain);
  }
  const mastered = document.createElement("button");
  mastered.type = "button";
  mastered.className = "player-flag-action";
  mastered.textContent = "懂了";
  mastered.addEventListener("click", () => {
    void postV3("bookmarks/actions", { bookmark_id: bookmarkId, action: "resolve" }).then(() => {
      marker.remove();
      toast("已收起这个标记；书签数据还在你的本机", "ready");
      window.dispatchEvent(new Event("courselens:bookmarks-changed"));
    }).catch((error) => {
      toast(error?.message || "没能收起标记，稍后再试一次", "error");
    });
  });
  menu.append(mastered);
  /* PLAYER-UX-1④：删除=彻底移除这条书签（服务层物理删）；成功后时间轴标记
     即时消失，学习桌列表经 bookmarks-changed 同步刷新 */
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "player-flag-action";
  remove.textContent = "删除";
  remove.addEventListener("click", () => {
    void deleteV3("bookmarks", { bookmark_id: bookmarkId }).then(() => {
      marker.remove();
      toast("已删除这条标记", "ready");
      window.dispatchEvent(new Event("courselens:bookmarks-changed"));
    }).catch((error) => {
      toast(error?.message || "没能删除标记，稍后再试一次", "error");
    });
  });
  menu.append(remove);
  return menu;
}

function flushNotUnderstoodFlags() {
  if (!pendingNotUnderstoodFlags.length) return;
  const duration = finiteDuration($("player-stage"));
  if (!(duration > 0)) return;
  const queued = pendingNotUnderstoodFlags;
  pendingNotUnderstoodFlags = [];
  for (const startMs of queued) renderNotUnderstoodFlag(startMs);
}

function clearNotUnderstoodFlags() {
  pendingNotUnderstoodFlags = [];
  const mount = $("player-timeline-flags");
  if (mount) clear(mount);
}

/* F8a（化身走查 20261008）：点击当下就要有确认——旗标态即变。点击先在
   时间轴落旗（乐观渲染），学生立刻看到「这一刻已插旗」；服务端确认后补挂
   操作菜单；失败撤旗并按码给人话，不做假成功。在途防重：请求未回时再点
   只提示「正在记」，不重复发请求。 */
let notUnderstoodMarkInFlight = false;

function markNotUnderstood() {
  const lecture = playerStore?.activeLecture;
  const player = $("player-stage");
  if (playbackKind !== "lecture" || !lecture?.sub_id || !player) return;
  const now = Date.now();
  if (now - notUnderstoodLastAt < NOT_UNDERSTOOD_DEDUPE_MS) {
    toast("这个位置已标记过", "checking");
    return;
  }
  if (notUnderstoodMarkInFlight) {
    toast("正在记这个标记，马上就好", "checking");
    return;
  }
  const startMs = Math.max(0, Math.round(Number(player.currentTime) * 1000) || 0);
  /* 时长未知时不乐观落旗（旧路径：确认后渲染，仍会进挂起队列），其余当下可见 */
  const optimisticMarker = finiteDuration(player) > 0 ? renderNotUnderstoodFlag(startMs) : null;
  notUnderstoodMarkInFlight = true;
  try {
    void postV3("bookmarks", {
      course_id: lecture.course_id,
      sub_id: lecture.sub_id,
      start_ms: startMs,
      end_ms: startMs,
      note: "没听懂",
    }).then((value) => {
      notUnderstoodLastAt = now;
      notUnderstoodMarkInFlight = false;
      const bookmarkId = String(value?.bookmark?.bookmark_id || "");
      if (optimisticMarker) {
        optimisticMarker.dataset.bookmarkId = bookmarkId;
        optimisticMarker.append(buildNotUnderstoodMenu(bookmarkId, optimisticMarker));
      } else {
        renderNotUnderstoodFlag(startMs, bookmarkId);
      }
      toast("已标记，回看时在你的书签里找它", "ready");
      window.dispatchEvent(new Event("courselens:bookmarks-changed"));
    }).catch((error) => {
      notUnderstoodMarkInFlight = false;
      /* F8a：失败撤旗——乐观旗只代表「正在记」，服务端拒绝就不留痕 */
      if (optimisticMarker) optimisticMarker.remove();
      /* REALFULL-1：没听懂标记路径缺证据时，不能借用解释链的「再重新提问」
         文案（此刻没有提问动作，学生会找不到落点）。按码给人话：标记需要
         字幕/讲义作依据，先去生成字幕。 */
      if (String(error?.code || "") === "bookmark_evidence_unavailable") {
        toast("这一讲还没有字幕或讲义可作依据，暂时记不了这个标记；先生成字幕，就能在没听懂的地方插小旗了。", "checking");
        return;
      }
      toast(error?.message || "标记没有成功，稍后再试一次", "error");
    });
  } catch {
    notUnderstoodMarkInFlight = false;
    if (optimisticMarker) optimisticMarker.remove();
    toast("标记没有成功，稍后再试一次", "error");
  }
}

/* ---- D3 字幕样式面板：字号 4 档/背景 4 档/贴底 4 档闭集，localStorage 单键
   （courselens:subtitle-style）；应用链=纯 CSS 变量（renderSubtitleOverlay
   零改动，cue 逻辑不触碰）；畸形持久值整体回退默认；本机 only。
   夜批10-B 第七遍（用户试用反馈）：字号下扩 xs=14px；贴底下扩 bottom 档。
   N10B-SUP-1（用户实证两态跳变）：贴边档不再用负偏移抵消 safe（旧 -104px 按
   visible 态 104px 校准，hidden 态 safe=20px → calc(20-104)=-84px 把字幕推出
   壳底缘，两态跳变 104px>deck）。改为 data-sub-edge 分态规则：--sub-bottom 归
   零、overlay 标记 data-sub-edge="1"，CSS 按态给 bottom——visible=var(--sub
  title-overlay-safe)（恰在控制台甲板上方），hidden=0（真触底）；两态差=104px
   =deck 高，观感连续。旧持久值 sm/md/lg/low/mid/high/bottom 全兼容。 ---- */
const SUBTITLE_STYLE_KEY = "courselens:subtitle-style";
const SUBTITLE_STYLE_DEFAULTS = Object.freeze({ size: "md", opacity: "85", offset: "low" });
const SUBTITLE_STYLE_SETS = Object.freeze({
  size: Object.freeze(["xs", "sm", "md", "lg"]),
  opacity: Object.freeze(["0", "25", "50", "85"]),
  offset: Object.freeze(["bottom", "low", "mid", "high"]),
});
const SUBTITLE_STYLE_VARS = Object.freeze({
  size: Object.freeze({ xs: "14px", sm: "18px", md: "22px", lg: "26px" }),
  opacity: Object.freeze({ "0": "0", "25": "0.25", "50": "0.5", "85": "0.85" }),
  /* 贴底位置四档：bottom=贴边档（--sub-bottom 归零+data-sub-edge 标记，分态
     bottom 由 CSS 规则给：visible=甲板上方/hidden=壳底缘），低=0%（控制台安全
     区=既有默认视觉），中/高各上移 4%。 */
  offset: Object.freeze({ bottom: "0%", low: "0%", mid: "4%", high: "8%" }),
});

function readSubtitleStyle() {
  try {
    const raw = JSON.parse(localStorage.getItem(SUBTITLE_STYLE_KEY) || "{}");
    const value = {};
    for (const key of Object.keys(SUBTITLE_STYLE_SETS)) {
      const candidate = String(raw?.[key] ?? "");
      value[key] = SUBTITLE_STYLE_SETS[key].includes(candidate) ? candidate : SUBTITLE_STYLE_DEFAULTS[key];
    }
    return value;
  } catch {
    return { size: SUBTITLE_STYLE_DEFAULTS.size, opacity: SUBTITLE_STYLE_DEFAULTS.opacity, offset: SUBTITLE_STYLE_DEFAULTS.offset };
  }
}

function applySubtitleStyle() {
  const overlay = $("player-subtitle-overlay");
  if (!overlay || !overlay.style || typeof overlay.style.setProperty !== "function") return;
  const style = readSubtitleStyle();
  overlay.style.setProperty("--sub-font-size", SUBTITLE_STYLE_VARS.size[style.size]);
  overlay.style.setProperty("--sub-bg-alpha", SUBTITLE_STYLE_VARS.opacity[style.opacity]);
  overlay.style.setProperty("--sub-bottom", SUBTITLE_STYLE_VARS.offset[style.offset]);
  /* N10B-SUP-1：贴边档走 data-sub-edge 分态规则（CSS 按 chrome 给 bottom：
     visible=甲板上方/hidden=壳底缘），非贴边档移除标记回退加算公式。 */
  if (style.offset === "bottom") overlay.setAttribute("data-sub-edge", "1");
  else overlay.removeAttribute("data-sub-edge");
}

function syncSubtitleStyleDialog() {
  const dialog = $("player-subtitle-style-dialog");
  if (!dialog || typeof dialog.querySelectorAll !== "function") return;
  const style = readSubtitleStyle();
  for (const group of dialog.querySelectorAll("[data-style-key]")) {
    const key = String(group.dataset.styleKey || "");
    const current = style[key];
    for (const option of group.querySelectorAll("[data-style-value]")) {
      option.classList.toggle("active", option.dataset.styleValue === current);
      option.setAttribute("aria-pressed", String(option.dataset.styleValue === current));
    }
  }
}

function writeSubtitleStyle(patch) {
  const next = { ...readSubtitleStyle(), ...patch };
  try {
    localStorage.setItem(SUBTITLE_STYLE_KEY, JSON.stringify(next));
  } catch { /* 存储不可用时静默放弃：本次会话内仍生效 */ }
  applySubtitleStyle();
  syncSubtitleStyleDialog();
  announcePlayerStatus("字幕样式已更新");
}

/* ---- D5 智能时间轴标签轨：数据源=GET /api/v3/timeline（evidence-rules
   分类器已在树，规则版零 token）。默认仅渲染 exam/homework/roll_call 三类
   （信息密度纪律）；悬停=标签名+秒数；点击=跳段首。仅讲次回放；讲次切换
   清空；无分类数据/读取失败=诚实无轨。 ---- */
const TIMELINE_DEFAULT_LABELS = new Set(["exam", "homework", "roll_call"]);
const TIMELINE_LABEL_TITLES = { exam: "考点", homework: "作业", roll_call: "点名" };
let pendingTimelineSegments = null;
let timelineSegmentsCache = []; /* D6 复用：最近一次成功拉取的全量段 */
let timelineFetchToken = 0;

function clearTimelineLabels() {
  pendingTimelineSegments = null;
  const layer = $("player-timeline-labels");
  if (layer) clear(layer);
}

function renderTimelineLabels(segments) {
  const layer = $("player-timeline-labels");
  const duration = finiteDuration($("player-stage"));
  if (!layer) return;
  if (!(duration > 0)) {
    pendingTimelineSegments = segments;
    return;
  }
  clear(layer);
  for (const segment of segments) {
    const label = String(segment?.label || "");
    if (!TIMELINE_DEFAULT_LABELS.has(label)) continue;
    const startMs = Math.max(0, Number(segment?.start_ms) || 0);
    const endMs = Math.max(startMs, Number(segment?.end_ms) || startMs);
    const item = document.createElement("button");
    item.type = "button";
    item.className = `player-label-segment player-label-${label}`;
    item.title = `${TIMELINE_LABEL_TITLES[label] || label} · 约 ${Math.max(1, Math.round((endMs - startMs) / 1000))} 秒`;
    item.setAttribute("aria-label", `${item.title}，点击跳转段首`);
    item.style.setProperty("--seg-from", String(Math.min(Math.max(startMs / (duration * 1000), 0), 1)));
    item.style.setProperty("--seg-to", String(Math.min(Math.max(endMs / (duration * 1000), 0), 1)));
    item.addEventListener("click", () => {
      const player = $("player-stage");
      player.currentTime = startMs / 1000;
      playQuietly(player);
    });
    layer.append(item);
  }
  /* 人话播报（有据才说）：闲聊/事务段合计足 1 分钟才提示快进机会 */
  let skippedMs = 0;
  for (const segment of segments) {
    const label = String(segment?.label || "");
    if (label === "chat" || label === "administrative") {
      skippedMs += Math.max(0, (Number(segment?.end_ms) || 0) - (Number(segment?.start_ms) || 0));
    }
  }
  if (skippedMs >= 60000) {
    announcePlayerStatus(`本章约 ${Math.round(skippedMs / 60000)} 分钟是闲聊和事务，时间轴已标出可跳的段`);
  }
}

function flushTimelineLabels() {
  if (!pendingTimelineSegments) return;
  const queued = pendingTimelineSegments;
  pendingTimelineSegments = null;
  renderTimelineLabels(queued);
}

async function hydrateTimelineLabels(lecture) {
  const subId = String(lecture?.sub_id || "");
  if (!subId || playbackKind !== "lecture") return;
  const token = timelineFetchToken += 1;
  try {
    const value = await apiV3(`timeline?sub_id=${encodeURIComponent(subId)}`);
    if (token !== timelineFetchToken) return; /* 讲次已再切：旧响应作废 */
    const active = playerStore?.activeLecture;
    if (!active || String(active.sub_id || "") !== subId) return;
    const segments = Array.isArray(value?.segments) ? value.segments : [];
    timelineSegmentsCache = segments;
    renderTimelineLabels(segments);
  } catch {
    /* 诚实退避：分类数据暂不可用就不渲染轨，绝不假装有 */
  }
}

/* ---- D6 考核标记三源一轨：①exam 段（复用 D5 拉取的分类缓存）=底色微
   高亮带；②quiz difficulty=hard；③书签（学生自己标的难点）。②③按八分桶
   密度聚合 ≤8 枚刻度（「重点都在这，按需跳读」视觉克制，不做红色轰炸）。
   点击刻度跳点；稀疏=自动退化零标记不报错；不新增任何数据采集。 ---- */
let assessFetchToken = 0;
let assessQuizItems = [];
let assessBookmarks = [];

function clearAssessMarkers() {
  assessQuizItems = [];
  assessBookmarks = [];
  const layer = $("player-timeline-assess");
  if (layer) clear(layer);
}

function renderAssessMarkers() {
  const layer = $("player-timeline-assess");
  const duration = finiteDuration($("player-stage"));
  if (!layer || !(duration > 0)) return;
  clear(layer);
  for (const segment of timelineSegmentsCache) {
    if (String(segment?.label || "") !== "exam") continue;
    const startMs = Math.max(0, Number(segment?.start_ms) || 0);
    const endMs = Math.max(startMs, Number(segment?.end_ms) || startMs);
    const band = document.createElement("span");
    band.className = "player-exam-band";
    band.title = `考点段 · 约 ${Math.max(1, Math.round((endMs - startMs) / 1000))} 秒`;
    band.style.setProperty("--seg-from", String(Math.min(Math.max(startMs / (duration * 1000), 0), 1)));
    band.style.setProperty("--seg-to", String(Math.min(Math.max(endMs / (duration * 1000), 0), 1)));
    layer.append(band);
  }
  const buckets = new Map();
  const consider = (startMs, kind) => {
    const ratio = startMs / (duration * 1000);
    if (!Number.isFinite(ratio) || ratio < 0) return;
    const key = Math.min(7, Math.floor(ratio * 8));
    if (buckets.has(key)) {
      const existing = buckets.get(key);
      existing.count += 1;
      return;
    }
    buckets.set(key, { startMs, kind, count: 1 });
  };
  for (const item of assessQuizItems) {
    if (String(item?.difficulty || "") !== "hard") continue;
    const raw = item?.start_ms ?? item?.evidence?.start_ms;
    const startMs = Number(raw);
    if (Number.isFinite(startMs)) consider(startMs, "quiz");
  }
  for (const bookmark of assessBookmarks) {
    const startMs = Number(bookmark?.start_ms);
    if (Number.isFinite(startMs)) consider(startMs, "bookmark");
  }
  for (const [key, info] of buckets) {
    const ratio = (key + 0.5) / 8;
    const tick = document.createElement("button");
    tick.type = "button";
    tick.className = `player-assess-tick player-assess-${info.kind}`;
    tick.title = info.count > 1
      ? `这一带有 ${info.count} 处难点，点击跳过去`
      : info.kind === "quiz" ? "难点测验，点击跳过去" : "你标记过的难点，点击跳回去";
    tick.setAttribute("aria-label", tick.title);
    tick.style.setProperty("--tick-ratio", String(ratio));
    tick.addEventListener("click", () => {
      const player = $("player-stage");
      player.currentTime = info.startMs / 1000;
      playQuietly(player);
    });
    layer.append(tick);
  }
}

function flushAssessMarkers() {
  renderAssessMarkers();
}

async function hydrateAssessMarkers(lecture) {
  const subId = String(lecture?.sub_id || "");
  const courseId = String(lecture?.course_id || "");
  if (!subId || playbackKind !== "lecture") return;
  const token = assessFetchToken += 1;
  const [quizResult, bookmarkResult] = await Promise.allSettled([
    apiV3(`quizzes?course_id=${encodeURIComponent(courseId)}&sub_id=${encodeURIComponent(subId)}`),
    apiV3(`bookmarks?sub_id=${encodeURIComponent(subId)}`),
  ]);
  if (token !== assessFetchToken) return;
  const active = playerStore?.activeLecture;
  if (!active || String(active.sub_id || "") !== subId) return;
  assessQuizItems = quizResult.status === "fulfilled" && Array.isArray(quizResult.value?.items)
    ? quizResult.value.items : [];
  assessBookmarks = bookmarkResult.status === "fulfilled" && Array.isArray(bookmarkResult.value?.bookmarks)
    ? bookmarkResult.value.bookmarks : [];
  renderAssessMarkers();
}

/* ---- D7 学习洞察（回看热点）：默认开启（AS5 拍板：热点要默认记才有用）；
   曾显式关闭过的本地偏好（courselens:insight="off"）继续被尊重=零采集零请求。
   采集事件闭集 {pause, seek_back, replay, slow_rate}，2s 桶对齐、内存排队、10s
   批落库（也由暂停/讲次切换触发收口）；进度条热度带只渲染回看类信号，
   悬停讲人话（「你在 12:30 附近回看了 3 次」，不判难点只陈述事实）。
   数据只到本地后端 /api/v3/watch-events*，零出域。 ---- */
const INSIGHT_SWITCH_KEY = "courselens:insight";
const INSIGHT_BUCKET_MS = 2000;
const INSIGHT_MAX_BATCH = 50;
let insightQueue = [];
let insightFlushTimer = 0;
let insightLastPositionMs = null;
let insightLastSeekBackMs = null;
let insightHydrateToken = 0;

function insightEnabled() {
  try {
    /* 只有显式写过 "off" 才关；默认（含存储不可用）都开 */
    return localStorage.getItem(INSIGHT_SWITCH_KEY) !== "off";
  } catch {
    return true;
  }
}

function clearInsightTimer() {
  if (insightFlushTimer) {
    window.clearTimeout(insightFlushTimer);
    insightFlushTimer = 0;
  }
}

function discardInsightEvents() {
  clearInsightTimer();
  insightQueue = [];
  insightLastPositionMs = null;
  insightLastSeekBackMs = null;
}

function recordInsightEvent(eventType) {
  if (!insightEnabled() || playbackKind !== "lecture") return;
  const player = $("player-stage");
  if (!player) return;
  const positionMs = Math.max(0, Math.floor((Number(player.currentTime) || 0) * 1000));
  const bucketMs = Math.floor(positionMs / INSIGHT_BUCKET_MS) * INSIGHT_BUCKET_MS;
  insightQueue.push({
    event: eventType,
    position_ms: bucketMs,
    playback_rate: Number(player.playbackRate) || 1,
  });
  if (insightQueue.length >= INSIGHT_MAX_BATCH) {
    flushInsightEvents();
    return;
  }
  if (!insightFlushTimer) {
    insightFlushTimer = window.setTimeout(() => {
      insightFlushTimer = 0;
      flushInsightEvents();
    }, 10000);
  }
}

function flushInsightEvents() {
  clearInsightTimer();
  if (!insightQueue.length || !insightEnabled()) return;
  const lecture = playerStore?.activeLecture;
  if (!lecture?.sub_id) return;
  const batch = insightQueue.splice(0, INSIGHT_MAX_BATCH);
  void postV3("watch-events", {
    course_id: lecture.course_id,
    sub_id: lecture.sub_id,
    events: batch,
  }).catch(() => { /* 本地落库失败：静默放弃这批，绝不打扰播放 */ });
}

function clearInsightHeat() {
  const layer = $("player-timeline-heat");
  if (layer) clear(layer);
}

/* AS5 U2：抹除入口藏在每个课次的操作区，只抹当前讲次；两击臂模式
   （同 tasks-drawer 的 armTwoStepButton 语义：第一击挂臂、6s 无确认自动解除）。 */
const INSIGHT_ERASE_LABEL = "抹掉本讲热点";
const INSIGHT_ERASE_CONFIRM_LABEL = "再点一次，抹掉本讲热点";
let insightEraseArmTimer = 0;

function resetInsightEraseArm() {
  if (insightEraseArmTimer) {
    window.clearTimeout(insightEraseArmTimer);
    insightEraseArmTimer = 0;
  }
  const button = $("insight-erase");
  if (!button) return;
  delete button.dataset.confirming;
  button.textContent = INSIGHT_ERASE_LABEL;
}

function renderInsightHeat(events) {
  const layer = $("player-timeline-heat");
  const duration = finiteDuration($("player-stage"));
  if (!layer || !(duration > 0)) return;
  clear(layer);
  const weights = { replay: 3, seek_back: 2, pause: 1, slow_rate: 1 };
  const buckets = new Map();
  for (const item of events) {
    const type = String(item?.event || "");
    const positionMs = Number(item?.position_ms) || 0;
    const bucket = Math.floor(positionMs / 10000) * 10000;
    const entry = buckets.get(bucket) || { weight: 0, revisits: 0 };
    entry.weight += weights[type] || 1;
    if (type === "replay" || type === "seek_back") entry.revisits += 1;
    buckets.set(bucket, entry);
  }
  for (const [bucket, info] of buckets) {
    if (info.revisits === 0) continue; /* 只标回看信号：单纯停顿不算热点 */
    const from = Math.min(Math.max(bucket / (duration * 1000), 0), 1);
    const to = Math.min(Math.max((bucket + 10000) / (duration * 1000), 0), 1);
    const band = document.createElement("span");
    band.className = "player-heat-band";
    band.style.setProperty("--seg-from", String(from));
    band.style.setProperty("--seg-to", String(to));
    band.style.opacity = String(Math.min(0.35 + info.revisits * 0.15, 0.8));
    band.title = `你在 ${formatPlayerTime(bucket / 1000)} 附近回看了 ${info.revisits} 次`;
    layer.append(band);
  }
}

async function hydrateInsightHeat(lecture) {
  clearInsightHeat();
  if (!insightEnabled()) return; /* 显式关闭过的本地偏好=零请求 */
  const subId = String(lecture?.sub_id || "");
  if (!subId || playbackKind !== "lecture") return;
  const token = insightHydrateToken += 1;
  try {
    const value = await apiV3(`watch-events?sub_id=${encodeURIComponent(subId)}`);
    if (token !== insightHydrateToken) return;
    const active = playerStore?.activeLecture;
    if (!active || String(active.sub_id || "") !== subId) return;
    renderInsightHeat(Array.isArray(value?.events) ? value.events : []);
  } catch {
    /* 诚实退避：读不到就不渲染 */
  }
}

/* ---- STUDY-STATS-M1 本周学习面貌心跳：playing 状态每 30s 一跳，把 wall-clock
   秒数累加进本地 study_daily_seconds（一行/日/讲，零内容——无讲次名/无位置/
   无文本）。门沿用 courselens:insight 开关（同一隐私级：默认开、显式 off 才关
   =零请求零采集，不为它新增第二开关）。跳失败静默放弃，绝不打扰播放；暂停/
   结束即停表，不足一跳的零头诚实丢弃（不估算）。 ---- */
const STUDY_HEARTBEAT_STEP_SECONDS = 30;
let studyHeartbeatTimer = 0;

function studyHeartbeatAllowed() {
  return insightEnabled() && playbackKind === "lecture";
}

function studyHeartbeatTick() {
  if (!studyHeartbeatAllowed()) return;
  const player = $("player-stage");
  if (!player || player.paused || player.ended) return;
  const lecture = playerStore?.activeLecture;
  if (!lecture?.sub_id || !lecture.course_id) return;
  void postV3("study/heartbeat", {
    course_id: lecture.course_id,
    sub_id: lecture.sub_id,
    seconds: STUDY_HEARTBEAT_STEP_SECONDS,
  }).catch(() => { /* 本地落库失败：丢这一跳，绝不打扰播放 */ });
}

function startStudyHeartbeat() {
  stopStudyHeartbeat();
  if (!studyHeartbeatAllowed()) return;
  studyHeartbeatTimer = window.setInterval(studyHeartbeatTick, STUDY_HEARTBEAT_STEP_SECONDS * 1000);
}

function stopStudyHeartbeat() {
  if (studyHeartbeatTimer) {
    window.clearInterval(studyHeartbeatTimer);
    studyHeartbeatTimer = 0;
  }
}

/* U7①：每课倍速记忆（本地 localStorage，键=课程 id；档位闭集内才回放） */
const RATE_MEMORY_KEY = "courselens.playback-rate.v1";
const RATE_CHOICES = new Set(["0.5", "0.75", "1", "1.25", "1.5", "1.75", "2", "2.5", "3"]);

function currentCourseId() {
  const course = playerStore?.activeCourse ?? null;
  return String(course?.course_id ?? "");
}

function savedRateFor(courseId) {
  try {
    const map = JSON.parse(localStorage.getItem(RATE_MEMORY_KEY) || "{}");
    const rate = Number(map?.[String(courseId)]);
    return RATE_CHOICES.has(String(rate)) ? rate : 1;
  } catch {
    return 1;
  }
}

function rememberRateFor(courseId, rate) {
  /* N5PR-P2：无课程身份（activeCourse 未定）绝不落记忆——空键是垃圾条目，
     会被后续任意无课程讲次误应用 */
  if (!String(courseId)) return;
  try {
    const map = JSON.parse(localStorage.getItem(RATE_MEMORY_KEY) || "{}");
    map[String(courseId)] = rate;
    localStorage.setItem(RATE_MEMORY_KEY, JSON.stringify(map));
  } catch { /* 存储不可用时静默放弃，不影响播放 */ }
}

function applySavedRate() {
  let rate = savedRateFor(currentCourseId());
  if (rate === 1) return;
  const player = $("player-stage");
  if (player) player.playbackRate = rate;
  const select = $("player-ctrl-speed");
  if (select && RATE_CHOICES.has(String(rate))) {
    select.value = String(rate);
    syncDropdown(select); /* 甲2：触发钮文案跟随 */
  }
}

/* U7③：章节标记挂进度条——study.js 经 courselens:chapters 事件投递既有
   chapters（title/start_ms）；悬停显示章节名，点击跳转该章节起点。
   时长未知时先挂起，等 syncPlayerTimeline 拿到有限时长再落位。 */
let pendingChapterMarkers = null;

function renderChapterMarkers(chapters) {
  const mount = $("player-timeline-markers");
  if (!mount) return;
  clear(mount);
  pendingChapterMarkers = Array.isArray(chapters) && chapters.length ? chapters : null;
  if (syncPlayerTimelineChaptersFlush) syncPlayerTimelineChaptersFlush();
}

let syncPlayerTimelineChaptersFlush = () => flushChapterMarkersWithDuration();

function flushChapterMarkersWithDuration() {
  if (!pendingChapterMarkers) return;
  const player = $("player-stage");
  const duration = finiteDuration(player);
  const mount = $("player-timeline-markers");
  if (!player || duration <= 0 || !mount) return;
  const chapters = pendingChapterMarkers;
  pendingChapterMarkers = null;
  for (const chapter of chapters) {
    const startMs = Number(chapter?.start_ms ?? chapter?.startMs);
    if (!Number.isFinite(startMs) || startMs < 0) continue;
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = "player-chapter-marker";
    marker.title = String(chapter?.title || "章节");
    marker.style.setProperty("--chapter-ratio", String(Math.min(startMs / (duration * 1000), 1)));
    marker.setAttribute("aria-label", `跳转到章节 ${marker.title}`);
    marker.addEventListener("click", () => {
      player.currentTime = startMs / 1000;
      playQuietly(player);
    });
    mount.append(marker);
  }
}

function handleChaptersEvent(event) {
  const detail = event?.detail;
  renderChapterMarkers(Array.isArray(detail?.chapters) ? detail.chapters : []);
}

function resetPlayerRate() {
  const player = $("player-stage");
  if (rightHoldTimer) {
    window.clearTimeout(rightHoldTimer);
    rightHoldTimer = 0;
  }
  holdRateActive = false;
  pendingRightSeek = false;
  userRateBeforeHold = 1;
  player.playbackRate = 1;
  if (!controlsDeckPresent) return;
  $("player-ctrl-speed").value = "1";
  syncDropdown($("player-ctrl-speed")); /* 甲2：触发钮文案跟随 */
}

function clearPlaybackRecovery() {
  const panel = $("player-recovery");
  panel.hidden = true;
  panel.dataset.state = "unknown";
  clear($("player-recovery-actions"));
}

/* 一次性位置恢复：媒体重载完成（元数据就绪）后回到失败前的位置；
   字幕 cue 随 currentTime 自动跟随。恢复失败不致命：后台已按 10s 粒度
   保存进度，绝不静音错误。重认证恢复定位后，持久进度恢复立即让位。 */
function restoreResumePosition() {
  if (!resumePlaybackState) return;
  const player = $("player-stage");
  const state = resumePlaybackState;
  resumePlaybackState = null;
  pendingSeekTarget = null;
  restorePendingForLoad = false;
  persistedRestore = null;
  if (!player) return;
  /* P2-E：一次恢复后位置成功回放（去标识纯计数，仅本机）。 */
  bumpLocalMetric("playback_position_retained");
  const duration = Number(player.duration);
  const limit = Number.isFinite(duration) && duration > 0 ? duration : state.seconds;
  try {
    player.currentTime = Math.min(Math.max(state.seconds, 0), limit);
  } catch {
    /* 元数据刚就绪时个别引擎拒绝 seek：放弃本次恢复 */
  }
  if (state.wasPlaying) Promise.resolve(player.play?.()).catch(() => {});
  announcePlayerStatus("已回到上次播放位置");
}

/* ---- L96'（PLAYER-OPT-1 20261008 夜，功能类项·晨裁待定）自动续播+「从头看」
   逃生浮层。痛点证据：night15 R6 Q33「跨会话续播位置记忆」效率缺口实锤卡
   （L96 立项）+ 用户 20261008 夜点名「重进自动续播+轻量浮层」；持久化本身
   已在（后端 progress API+B1 四重守卫），增量=定位成功后的自动续播与逃生。
   业界惯例（Roku Continue Watching/Instant Resume、Netflix、Alexa resume
   语义「无 marker 回退从头」）：自动续播必须配一键 Start Over；近开头
   （<5s，与从头无感知差）与近结尾（距末尾 <15s，马上看完）不自动播——
   位置信息本身没有感知差；自动播放被浏览器政策拒绝时静默让位（位置已
   就位，学生按播放即续）。浮层=非模态提示条，动态创建（零 index.html
   触碰）：出现不抢焦点、「从头看」按钮 Tab 可达、Esc/按钮/12s 超时三路
   收口、点击后焦点归还由学习桌 capture 接线覆盖。 ---- */
const RESUME_AUTO_PLAY_MIN_SECONDS = 5;
const RESUME_AUTO_PLAY_TAIL_GUARD_SECONDS = 15;
const RESUME_BANNER_TIMEOUT_MS = 12000;
let resumeBannerNode = null;
let resumeBannerTimer = 0;

function dismissResumeBanner() {
  if (resumeBannerTimer) {
    window.clearTimeout(resumeBannerTimer);
    resumeBannerTimer = 0;
  }
  if (resumeBannerNode) {
    resumeBannerNode.remove();
    resumeBannerNode = null;
  }
}

function showResumeBanner(seconds) {
  const shell = $("player-stage-shell");
  if (!shell || typeof shell.append !== "function") return;
  dismissResumeBanner();
  const banner = document.createElement("div");
  banner.className = "player-resume-banner";
  banner.setAttribute("aria-label", "续播提示");
  const text = document.createElement("span");
  text.className = "player-resume-text";
  text.textContent = `已从上次位置 ${formatPlayerTime(seconds)} 继续播放`;
  const restart = document.createElement("button");
  restart.type = "button";
  restart.className = "player-resume-restart";
  restart.textContent = "从头看";
  restart.setAttribute("aria-label", `从头看（当前已从 ${formatPlayerTime(seconds)} 继续）`);
  restart.addEventListener("click", () => {
    const player = $("player-stage");
    dismissResumeBanner();
    if (!player) return;
    try {
      player.currentTime = 0;
    } catch { /* 罕见引擎拒绝 seek：位置保持，浮层照常收口 */ }
    playQuietly(player);
    announcePlayerStatus("从头开始播放");
  });
  banner.append(text);
  banner.append(restart);
  shell.append(banner);
  resumeBannerNode = banner;
  announcePlayerStatus(`已从上次位置 ${formatPlayerTime(seconds)} 继续播放，想从头看可点浮层按钮`);
  resumeBannerTimer = window.setTimeout(() => {
    resumeBannerTimer = 0;
    dismissResumeBanner();
  }, RESUME_BANNER_TIMEOUT_MS);
}

/* 持久观看进度恢复：元数据可 seek（时长已知）后钳制到合法区间一次性定位；
   L96' 起定位成功即自动续播（配「从头看」逃生浮层），近开头/近结尾保持
   原定位播报语义。时长未知时等待 loadedmetadata，GET 先到就先挂起。 */
function applyPersistedRestore() {
  if (!restorePendingForLoad || !persistedRestore) return;
  const player = $("player-stage");
  const duration = Number(player?.duration);
  if (!player || !Number.isFinite(duration) || duration <= 0) return;
  const lecture = playerStore?.activeLecture;
  if (!lecture || String(lecture.sub_id || "") !== persistedRestore.subId) return;
  const state = persistedRestore;
  persistedRestore = null;
  restorePendingForLoad = false;
  const limit = Math.min(duration, seekableEnd(player) || duration);
  const target = Math.min(Math.max(state.seconds, 0), limit);
  if (!(target > 0)) return;
  try {
    player.currentTime = target;
  } catch {
    return; /* 元数据刚就绪时个别引擎拒绝 seek：放弃本次恢复 */
  }
  /* L96'：定位成功即自动续播+「从头看」逃生浮层（契约升级依据=night15 R6 L96
     立项+用户 20261008 夜点名；旧「绝不自动播放」契约由本卡升格）。近开头/
     近结尾不自动播：位置与从头/马上看完没有感知差，保持诚实定位播报。 */
  if (target >= RESUME_AUTO_PLAY_MIN_SECONDS
    && (duration - target) >= RESUME_AUTO_PLAY_TAIL_GUARD_SECONDS) {
    Promise.resolve(player.play?.()).catch(() => {}); /* 政策拒绝=静默让位：位置已就位 */
    /* L96' 证据设计（R6 原案「本地自动续播触发计数」）：去标识纯计数，仅本机。 */
    bumpLocalMetric("playback_auto_resume");
    showResumeBanner(target);
    return;
  }
  announcePlayerStatus(`已恢复到上次观看位置 ${formatPlayerTime(target)}`);
}

/* 读取持久观看进度。失败/错讲次/跨课程/零值/已完成都静默降级为从头播放；
   绝不让读取错误打扰播放，也绝不把别的讲次的进度恢复到当前媒体上。 */
async function hydratePersistedProgress(lecture) {
  const subId = String(lecture?.sub_id || "");
  if (!subId) return;
  try {
    const value = await apiV3(`progress?sub_id=${encodeURIComponent(subId)}`);
    const row = value?.progress && typeof value.progress === "object" ? value.progress : null;
    if (!row || String(row.sub_id || "") !== String(lecture.sub_id || "")) return;
    if (lecture.course_id && row.course_id
      && String(row.course_id) !== String(lecture.course_id)) return;
    const seconds = Number(row.position_seconds);
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    if (row.completed === true) return;
    const rowDuration = Number(row.duration_seconds);
    if (Number.isFinite(rowDuration) && rowDuration > 0 && seconds >= rowDuration) return;
    /* 读取期间讲次已切换：这行进度属于旧讲次，绝不落到新媒体上 */
    const active = playerStore?.activeLecture;
    if (!active || String(active.sub_id || "") !== String(lecture.sub_id || "")) return;
    persistedRestore = { subId, seconds };
    applyPersistedRestore();
  } catch {
    /* 读取失败：诚实降级，不恢复也不提示 */
  }
}

function renderPlaybackRecovery(code, performAction, diagnostic = "") {
  const detail = RECOVERY_DETAILS[code] || RECOVERY_DETAILS.media_source_unavailable;
  const panel = $("player-recovery");
  panel.hidden = false;
  panel.dataset.state = "degraded";
  $("player-recovery-title").textContent = detail.title;
  $("player-recovery-impact").textContent = diagnostic
    ? `${detail.impact}${diagnostic}`
    : detail.impact;
  clear($("player-recovery-actions"));
  detail.actions.forEach((action) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = RECOVERY_LABELS[action];
    button.addEventListener("click", () => performAction(action));
    $("player-recovery-actions").append(button);
  });
}

function updateLectureActions(lecture) {
  const available = Boolean(lecture?.sub_id);
  const timed = transcriptTimed === true
    && transcriptTimedSubId === String(lecture?.sub_id || "");
  /* U⑥（云自动为主流·用户拍板）：本课程已开启自动整理 → 按钮位让给
     「已排入自动处理」小字；已有产物（字幕=确认含时间轴、笔记=已完成
     summary 任务证据）时按钮消失，状态交给任务 chip/状态行。 */
  const courseId = String(lecture?.course_id || "");
  const subId = String(lecture?.sub_id || "");
  const autoOn = Array.isArray(playerStore?.automation?.rules)
    && playerStore.automation.rules.some((rule) => String(rule?.course_id || "") === courseId);
  const notesDone = (playerStore?.tasks || []).some((task) => (
    String(task?.kind || "") === "summary"
    && String(task?.state || "") === "completed"
    && String(task?.course_id || "") === courseId
    && String(task?.sub_id || "") === subId
  ));
  const subtitleButton = $("generate-subtitle");
  const notesButton = $("generate-notes");
  const hint = $("player-action-hint");
  if (subtitleButton) {
    subtitleButton.hidden = autoOn || timed;
    subtitleButton.disabled = !available;
  }
  if (notesButton) {
    notesButton.hidden = autoOn || notesDone;
    notesButton.disabled = !available || !timed;
  }
  if (hint) {
    /* 乙-2（#14）：提示只在有解释义务的态出现——无讲次/自动接管/缺时间轴
       三态各承其责；按钮可用后双引擎教学长文退场（首用引导覆盖）。 */
    hint.hidden = Boolean(available) && !autoOn && timed;
    hint.textContent = !available
      ? "选择讲次后可生成字幕和笔记。"
      : autoOn
        ? "已排入自动处理：本课程的字幕与 AI 处理由云自动化完成，不用手动发起。"
        : !timed
          ? "先生成字幕，再生成笔记。"
          : "字幕自动选择处理方式：配置 DeepSeek 后双引擎识别并自动校对；未配置时用高精度识别（无大模型验证）。";
  }
}

// ---- 自绘字幕 overlay（合成 Chromium 验证 video::cue 完全不可控，原生 cue 渲染
// ---- 会出现多块纯白底；track 保持 hidden 模式复用原生解析与时间轴，唯一渲染者是
// ---- #player-subtitle-overlay：多个同时 active 的 cue 合并为一段文本，CSS 两行封顶）。
function clearSubtitleOverlay() {
  const overlay = $("player-subtitle-overlay");
  overlay.textContent = "";
  overlay.hidden = true;
  lastOverlayText = "";
}

function activeSubtitleText(textTrack, nowSeconds) {
  const parts = [];
  const cues = textTrack.cues;
  for (let index = 0; index < (cues ? cues.length : 0); index += 1) {
    const cue = cues[index];
    if (nowSeconds >= cue.startTime && nowSeconds < cue.endTime) {
      const text = String(cue.text || "").replace(/<[^>]*>/g, "").trim();
      if (text) parts.push(text);
    }
  }
  return parts.join(" ");
}

function renderSubtitleOverlay() {
  if (!subtitlesOn || playbackKind !== "lecture" || !subtitleTextTrack) return;
  const text = activeSubtitleText(subtitleTextTrack, $("player-stage").currentTime);
  if (text === lastOverlayText) return;
  if (!text) {
    clearSubtitleOverlay();
    return;
  }
  const overlay = $("player-subtitle-overlay");
  lastOverlayText = text;
  overlay.textContent = text;
  overlay.hidden = false;
}

// ---- 模式看护：真实 Chromium 中 TextTrack 模式变化不派发任何事件（无 modechange），
// ---- 字幕开关（一方控制台按钮）只能轮询 track.mode 感知。选 rAF 而非 200ms
// ---- interval：页面可见时逐帧运行且暂停/播放都持续，控制条只在可见页面可点，
// ---- 不会漏检；隐藏页签里 rAF 暂停也无妨，回到可见后的第一帧即对齐状态。
// ---- 无 rAF 的引擎退化为 200ms 轮询。showing→强制回 hidden+字幕开；
// ---- disabled→字幕关+清空；hidden→字幕开+逐 tick 重入渲染（cues=null 时安全清空）。
function applySubtitleMode() {
  if (!subtitleTextTrack) {
    syncSubtitleButton();
    return;
  }
  if (subtitleTextTrack.mode === "showing") {
    subtitleTextTrack.mode = "hidden";
    subtitlesOn = true;
    renderSubtitleOverlay();
    syncSubtitleButton();
    return;
  }
  if (subtitleTextTrack.mode === "disabled") {
    if (subtitlesOn) {
      subtitlesOn = false;
      clearSubtitleOverlay();
    }
    syncSubtitleButton();
    return;
  }
  subtitlesOn = true;
  renderSubtitleOverlay();
  syncSubtitleButton();
}

function watchSubtitleModeStep() {
  if (!subtitleWatchActive) return;
  applySubtitleMode();
  subtitleWatchTimer = window.requestAnimationFrame(watchSubtitleModeStep);
}

function startSubtitleWatcher() {
  if (subtitleWatchActive || !subtitleTextTrack) return;
  subtitleWatchActive = true;
  if (typeof window.requestAnimationFrame === "function") {
    subtitleWatchTimer = window.requestAnimationFrame(watchSubtitleModeStep);
  } else {
    subtitleWatchTimer = window.setInterval(applySubtitleMode, 200);
  }
}

function stopSubtitleWatcher() {
  subtitleWatchActive = false;
  if (subtitleWatchTimer) {
    window.cancelAnimationFrame?.(subtitleWatchTimer);
    window.clearInterval?.(subtitleWatchTimer);
  }
  subtitleWatchTimer = 0;
}

function wireSubtitleTrack() {
  const textTrack = $("player-subtitle-track").track;
  if (!textTrack) return;
  if (textTrack !== subtitleTextTrack) {
    if (subtitleTextTrack) subtitleTextTrack.removeEventListener("cuechange", renderSubtitleOverlay);
    subtitleTextTrack = textTrack;
    textTrack.addEventListener("cuechange", renderSubtitleOverlay);
  }
  if (textTrack.mode !== "hidden") textTrack.mode = "hidden";
  startSubtitleWatcher();
}

/* N10B-2：字幕文件轨按需挂载。事实源=共享 store 的 transcriptHasTiming
   （study.js 字幕 segments 读取落地/失败/清场都重设本键，set 无同值去重，
   连续两个有字幕讲次也会各自触发）。真=挂 /subtitles/file 并接 cue 链；
   假=卸载并清 overlay。挂载态用模块变量记账，读回不做属性探询。 */
let mountedSubtitleFileSrc = "";

function detachSubtitleFileTrack() {
  if (mountedSubtitleFileSrc) {
    mountedSubtitleFileSrc = "";
    $("player-subtitle-track")?.removeAttribute("src");
  }
  if (subtitleTextTrack) {
    subtitleTextTrack.removeEventListener("cuechange", renderSubtitleOverlay);
    subtitleTextTrack.mode = "disabled";
    subtitleTextTrack = null;
  }
  clearSubtitleOverlay();
  stopSubtitleWatcher();
}

function mountSubtitleTrackFromFile(hasSubtitles) {
  const track = $("player-subtitle-track");
  if (!track) return;
  if (!hasSubtitles) {
    detachSubtitleFileTrack();
    syncSubtitleButton();
    return;
  }
  const subId = encodeURIComponent(playerStore?.activeLecture?.sub_id || "");
  if (!subId) return;
  const wanted = `/api/v3/subtitles/file?sub_id=${subId}`;
  if (mountedSubtitleFileSrc === wanted) return;
  mountedSubtitleFileSrc = wanted;
  track.src = wanted;
  wireSubtitleTrack();
  syncSubtitleButton();
}

function setTaskEvidence(message, state = "unknown") {
  const target = $("player-task-text");
  target.textContent = message;
  const taskStateRow = $("player-task-state");
  taskStateRow.dataset.state = state;
  /* 乙-2（#15/AS5-F1）：占位行不常驻——状态未明保持沉默，真实证据到场才亮行 */
  if (!message) {
    taskStateRow.hidden = true;
    return;
  }
  taskStateRow.hidden = false;
}

async function saveProgress(store, completed = false) {
  const lecture = store.activeLecture;
  const player = $("player-stage");
  const subId = String(lecture?.sub_id || "");
  /* 写侧讲次绑定守卫（读侧 hydrate 四重守卫的对称面，B1 根修）：进度落库
     必须绑定产生它的讲次，三元任一不满足即静默放弃——宁少存一步，绝不把
     切讲间隙的 0 位置写进新讲次、清掉「继续播放」。
     ① 归属：store 当前讲次必须就是播放器里加载的媒体（遗留 timer、杂散
        pause/ended 的归属拒绝）；
     ② 元数据就绪：duration 有效（VOD load() 复位 duration=NaN，≙
        readyState≥HAVE_METADATA；新媒体未就绪时 currentTime=0 不是真实位置）；
     ③ 位置有效：currentTime 有限。
     载荷与广播在入口一次性捕获、await 前无重入：切讲后才收尾的在途保存
     携带的仍是发起讲次的正确数据，不需要写后复核。 */
  if (!subId || subId !== mediaSubId) return;
  const duration = Number(player?.duration);
  if (!Number.isFinite(duration) || duration <= 0) return;
  if (!Number.isFinite(player.currentTime)) return;
  const payload = {
    sub_id: lecture.sub_id,
    position_seconds: player.currentTime,
    duration_seconds: duration,
    playback_rate: player.playbackRate,
    completed,
  };
  const value = await postV3("progress", payload);
  /* 保存成功即广播（讲次卡消费同一事实）：优先后端回读行，缺省用请求载荷 */
  const row = value?.progress && typeof value.progress === "object" ? value.progress : null;
  window.dispatchEvent(new CustomEvent("courselens:watch-progress", {
    detail: {
      sub_id: String(lecture.sub_id || ""),
      course_id: String(lecture.course_id || ""),
      position_seconds: Number(row?.position_seconds ?? payload.position_seconds) || 0,
      duration_seconds: Number(row?.duration_seconds ?? payload.duration_seconds) || 0,
      completed: Boolean(row?.completed ?? completed),
    },
  }));
}

async function enqueue(store, kind) {
  const lecture = store.activeLecture;
  if (!lecture) {
    toast("请先选择讲次", "error");
    return;
  }
  const button = kind === "subtitle" ? $("generate-subtitle") : $("generate-notes");
  setBusy(button, true);
  try {
    const value = await postV3("tasks/enqueue", {
      kind,
      course_id: lecture.course_id,
      sub_id: lecture.sub_id,
      include_ppt: true,
    });
    pendingProtectedEnqueue = null; /* 手动提交成功：待办已受理，无续提必要 */
    setTaskEvidence(`已提交 · ${value.task?.task_id || ""}`, "checking");
    $("player-task-open-drawer").hidden = false;
    toast("任务已由后端接受", "ready");
    window.dispatchEvent(new Event("courselens:tasks-refresh"));
  } catch (error) {
    if (error?.code === "fudan_login_required") {
      /* P1-C/P3-B：闭集 401 = 请求确定未被受理 → 保留这次待办，重认证成功后
         自动续提恰一次。保留的身份三元组 (kind, course_id, sub_id) 就是这个
         动作的幂等键：续提载荷与原提交逐字节一致，绝不另造新键、绝不因重复
         点击叠加第二份待办。timeout/网络等含糊失败绝不保留（可能已受理）。 */
      const duplicate = pendingProtectedEnqueue
        && pendingProtectedEnqueue.kind === kind
        && String(pendingProtectedEnqueue.sub_id) === String(lecture.sub_id);
      pendingProtectedEnqueue = { kind, course_id: lecture.course_id, sub_id: lecture.sub_id };
      if (!duplicate) {
        setTaskEvidence("等待重新认证后自动继续这次提交", "checking");
        toast("请先重新认证；完成后会自动继续这次提交", "checking");
      }
      return;
    }
    pendingProtectedEnqueue = null;
    /* D3：行内证据行走人话（映射见 api.js 码表），不再拼「码 · 英文 payload」 */
    setTaskEvidence(error.message || "这次操作没能完成，请稍后再试", "error");
    toast(
      error.code === "task_already_active"
        ? "该讲次已有暂停任务且无法自动恢复，请在任务抽屉取消后重新发起"
        : error.message,
      "error",
    );
  } finally {
    setBusy(button, false);
    updateLectureActions(store.activeLecture);
  }
}

function loadLecture(lecture) {
  playbackKind = "none";
  clearPlaybackRecovery();
  clearOsdTimers();
  hidePlayerOsd();
  clearBufferSpinner();
  hideTimelineBubble();
  dismissResumeBanner();
  clearNotUnderstoodFlags();
  notUnderstoodLastAt = 0;
  notUnderstoodMarkInFlight = false;
  clearTimelineLabels();
  clearAssessMarkers();
  discardInsightEvents();
  updateLectureActions(lecture);
  resetPlayerRate();
  setPlayerControlsVisible(Boolean(lecture));
  /* 讲次切换确定性复位：计时器、焦点/缓冲/音量面/悬停瞬时状态全部清零，
     控制台回到可见（随后按当前钉住状态重新裁决，未播放同样进空闲收起） */
  mediaBuffering = false;
  keyboardFocusInsidePlayer = false;
  deckHover = false;
  volumeHover = false;
  volumeClickPin = false;
  /* 持久进度恢复属于旧讲次：随切换一并失效 */
  restorePendingForLoad = false;
  persistedRestore = null;
  /* 旧讲次遗留的 10s 进度防抖随切换失效（同 insightFlushTimer 的
     discardInsightEvents 纪律）：到点也绝不以新讲次身份写旧位置或 0 位置。
     loadLecture 由 store.set 同步调用，本行与 mediaSubId 重写是切讲间隙内
     唯一的两步，任何跨间隙的保存都逃不过写侧守卫 + 本取消。 */
  if (saveTimer) window.clearTimeout(saveTimer);
  saveTimer = 0;
  mediaSubId = lecture ? String(lecture.sub_id || "") : "";
  syncVolumeOpen();
  pokeChrome();
  const player = $("player-stage");
  const placeholder = $("player-placeholder");
  if (!lecture) {
    player.removeAttribute("src");
    detachSubtitleFileTrack();
    player.load();
    player.hidden = true;
    placeholder.hidden = false;
    stopSubtitleWatcher();
    clearSubtitleOverlay();
    syncPlayerTimeline();
    syncSubtitleButton();
    syncSubtitleChip();
    syncNotUnderstoodButton();
    resetInsightEraseArm();
    setTaskEvidence("");
    $("player-title").textContent = "选择讲次";
    $("player-evidence").textContent = "未选择媒体";
    return;
  }
  playbackKind = "lecture";
  const subId = encodeURIComponent(lecture.sub_id);
  $("player-title").textContent = lecture.sub_title || "课程讲次";
  /* 普通成功态保持安静：不再展示“可在线播放”；登录要求是真实状态，保留 */
  $("player-evidence").textContent = lecture.can_stream ? "" : "需要登录";
  player.src = `/api/v3/media?sub_id=${subId}`;
  /* N10B-2：字幕文件轨不再开讲即挂——等 transcriptHasTiming 事实到场
     （store 订阅）才发 /subtitles/file 请求，无字幕讲次不再制造必然 404
     的 <track> 资源错误。此处只负责清干净上一讲的轨状态。 */
  detachSubtitleFileTrack();
  player.load();
  player.hidden = true;
  placeholder.hidden = false;
  clearSubtitleOverlay();
  syncPlayerTimeline();
  /* 播放/暂停图标从媒体真实 paused 态派生：load() 复位 paused 不派发事件 */
  syncPlayButton();
  wireSubtitleTrack();
  syncSubtitleButton();
  syncSubtitleChip();
  syncNotUnderstoodButton();
  resetInsightEraseArm();
  /* 恢复持久观看进度：在元数据可 seek 后一次性定位，不自动播放 */
  restorePendingForLoad = true;
  void hydratePersistedProgress(lecture);
  void hydrateTimelineLabels(lecture);
  void hydrateAssessMarkers(lecture);
  void hydrateInsightHeat(lecture);
  /* AS5-F1：任务行不亮占位——加载中保持沉默，真实任务证据到场才亮行 */
  setTaskEvidence("");
}

export async function installPlayerCore(store) {
  playerStore = store;
  controlsDeckPresent = Boolean($("player-controls"));
  /* FOCUS-DRIFT-1②：学习桌指针激活焦点归还监听（接线于下方 wiring，清理对称） */
  let handleDeskClickFocusRelease = null;
  /* 安装时复位全部 S09-D 瞬时状态（同一进程内重复安装互不污染） */
  clearIdleTimer();
  /* 上一轮安装的进度防抖与讲次绑定一并失效（saveProgress 写侧守卫的基线） */
  if (saveTimer) window.clearTimeout(saveTimer);
  saveTimer = 0;
  mediaSubId = "";
  keyboardFocusInsidePlayer = false;
  mediaBuffering = false;
  deckHover = false;
  volumeHover = false;
  volumeFocus = false;
  volumeClickPin = false;
  volumeOpen = false;
  lastAudibleVolume = 0;
  clearOsdTimers();
  hidePlayerOsd();
  clearBufferSpinner();
  hideTimelineBubble();
  let disposed = false;
  const performRecoveryAction = (action) => {
    if (disposed) return;
    if (action === "retry-media" && store.activeLecture?.sub_id) {
      /* P1-C：重载前捕获播放位置与播放态，元数据就绪后一次性恢复。
         P3-B：恢复期间的 seek 意图优先于（可能已被错误回卷的）currentTime。 */
      const player = $("player-stage");
      const seconds = pendingSeekTarget != null ? pendingSeekTarget : Number(player.currentTime);
      pendingSeekTarget = null;
      resumePlaybackState = Number.isFinite(seconds) && seconds > 0
        ? { seconds, wasPlaying: player.paused === false }
        : null;
      loadLecture(store.activeLecture);
      return;
    }
    if (action === "login") {
      window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "reauth" }));
      return;
    }
  };
  /* U17 B2：恢复面板已可见时后续失败直接丢弃——同回合先渲染者胜，
     绝不让后到的猜测面板覆盖真实闭集原因卡。 */
  const recoveryPanelVisible = () => {
    const panel = $("player-recovery");
    return Boolean(panel) && panel.hidden === false;
  };
  /* MEDIA-001-20261001：细分证据晚到不闪换——只有同失败回合且恢复面板仍在
     展示时才原位替换；证据取不到（合成桩/本地服务异常）保留既有合成卡。 */
  const refineMediaSourceFailure = async (failureRound) => {
    try {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 1500);
      let status;
      try {
        status = await apiV3("media/stream-status", { signal: controller.signal });
      } finally {
        window.clearTimeout(timeout);
      }
      const refined = MEDIA_STREAM_FAILURE_REFINEMENTS[String(status?.failure_code || "")];
      /* MEDIA-VPN-1-20261001：upstream_unreachable 内再按 aTrust 三态细分
         （同一回读载荷 atrust.state 闭集）；未知/缺字段=保留既有细分卡。 */
      let card = refined;
      if (refined === "media_source_unreachable") {
        const atrustState = String(status?.atrust?.state || "");
        if (atrustState === "not_installed") card = "media_source_unreachable_vpn_missing";
        else if (atrustState === "present") card = "media_source_unreachable_vpn_present";
      }
      if (!card || disposed) return;
      const panel = $("player-recovery");
      if (!panel || panel.hidden === true || mediaFailureRound !== failureRound) return;
      autoContinuityEligible = AUTO_CONTINUITY_CODES.has(card);
      renderPlaybackRecovery(card, performRecoveryAction);
    } catch {
      /* 细分证据取不到：既有合成卡即兜底，绝不闪换 */
    }
  };
  const showMediaFailure = () => {
    if (disposed || playbackKind === "none") return;
    if (recoveryPanelVisible()) return;
    autoContinuityArmed = false; /* 新失败回合：重认证成功后允许恰一次自动续播 */
    clearBufferSpinner(); /* 恢复面板即将可见：缓冲指示互斥收口 */
    dismissResumeBanner(); /* 恢复面板互斥纪律（对齐 spinner）：失败面归恢复面板说 */
    const mediaCode = Number($("player-stage").error?.code || 0);
    let code;
    if (store.activeLecture?.can_stream === false) {
      code = "media_authorization_required";
    } else {
      code = mediaCode === 2 ? "media_network_interrupted"
        : mediaCode === 3 ? "media_decode_failed"
          : "media_source_unavailable";
    }
    autoContinuityEligible = AUTO_CONTINUITY_CODES.has(code);
    syncPlayerTimeline();
    const failureRound = ++mediaFailureRound;
    renderPlaybackRecovery(code, performRecoveryAction);
    pokeChrome(); /* 错误/恢复态钉住控制台可见 */
    /* MEDIA-001-20261001：code=4 合成卡是即时兜底；同回合用服务端闭集结局
       码原位细分（<video> 把一切开流失败塌缩成 code 4，归因只能在服务端）。 */
    if (code === "media_source_unavailable") {
      void refineMediaSourceFailure(failureRound);
    }
  };
  /* P1-C：一次重认证成功（auth ready）后的自动继续——待办提交优先，其次
     授权类恢复面板恰一次自动重取；两者都只重试失败的那个请求。挑战/凭据
     类失败不会让 auth 变成 ready，因此这里永远不会绕过挑战暂停。 */
  const handleAuthContinuity = (auth) => {
    if (disposed) return;
    /* P3-B：目录身份无法确认/不一致 = 另一身份作用域 → 清全部连续性待办
       （待办、位置、seek 意图、自动续播），绝不跨账号续提或续播。 */
    if (IDENTITY_MISMATCH_CODES.has(String(auth?.code || ""))) {
      clearContinuityState();
      return;
    }
    if (auth?.state !== "ready") return;
    const pending = pendingProtectedEnqueue;
    if (pending) {
      pendingProtectedEnqueue = null;
      const lecture = store.activeLecture;
      if (lecture && String(lecture.sub_id) === String(pending.sub_id)) {
        void enqueue(store, pending.kind);
      }
      return;
    }
    if (autoContinuityArmed) return;
    const recovery = $("player-recovery");
    if (!recovery || recovery.hidden === true) return;
    autoContinuityArmed = true;
    if (autoContinuityEligible && playbackKind === "lecture" && store.activeLecture?.sub_id) {
      announcePlayerStatus("正在恢复播放授权并继续播放");
      performRecoveryAction("retry-media");
    }
  };
  /* P3-B：退出登录即清除全部连续性待办（本地内存状态；不动任何用户文件）。
     已被 settings.js 的退出流程消费的同一事件，这里只做播放器侧收口。 */
  const handleLogoutClear = () => {
    if (disposed) return;
    clearContinuityState();
  };
  /* 退出学习桌即暂停（courselens:page 由 shell 的每次页面选择广播，含字标
     返回/设置关闭重选 study——那时学习桌已不可见，音频不得继续后台播放）。
     暂停事件本身会保存进度；这里只收敛只属于活动播放的瞬时状态。 */
  const handlePageChanged = () => {
    if (disposed) return;
    const activePlayer = $("player-stage");
    if (activePlayer && activePlayer.paused === false) activePlayer.pause();
    restoreHoldRate(false);
    timelineScrubbing = false;
    volumeHover = false;
    volumeFocus = false;
    volumeClickPin = false;
    syncVolumeOpen();
    clearIdleTimer();
    setChromeVisible(true);
  };
  const player = $("player-stage");
  const shell = $("player-stage-shell");
  const showVideo = () => {
    $("player-placeholder").hidden = true;
    player.hidden = false;
  };
  const handleLoadedMetadata = () => {
    clearPlaybackRecovery();
    mediaBuffering = false;
    wireSubtitleTrack();
    showVideo();
    syncPlayerTimeline();
    syncSubtitleButton();
    flushNotUnderstoodFlags();
    flushTimelineLabels();
    flushAssessMarkers();
    restoreResumePosition();
    applyPersistedRestore();
    renderSubtitleOverlay();
  };
  // ---- 一方控制台：原生 controls 已从 index.html 移除，这里再做属性级兜底。
  // ---- 全屏由按钮在用户手势内直接对 shell 请求（等待 video 全屏后退出重进会
  // ---- 丢失瞬态用户激活），fullscreenchange 只负责同步按钮状态。
  player.controls = false;
  const pipSupported = document.pictureInPictureEnabled === true && player.disablePictureInPicture !== true;
  if (controlsDeckPresent) $("player-ctrl-pip").hidden = !pipSupported;
  /* N5PR-P3：viaShortcut=Space/K/画面点击时中央 pulse（按钮点击有直接视觉
     反馈，不再叠加中央图标）；意图在动作前捕获，异步 play 不影响图标方向。 */
  const togglePlayback = (viaShortcut = false) => {
    const willPlay = player.paused === true;
    if (willPlay) {
      Promise.resolve(player.play?.()).catch(() => announcePlayerStatus("暂时无法开始播放"));
    } else {
      player.pause();
    }
    if (viaShortcut) osdPulse(willPlay);
  };
  const handlePlayButtonClick = () => togglePlayback();
  /* N5PR-P1：静音切换核心（M 键与按钮共用）；键盘路径不展开音量面 */
  const applyMuteToggle = () => {
    const silenced = player.muted === true || Number(player.volume) === 0;
    if (silenced) {
      player.muted = false;
      if (Number(player.volume) === 0) {
        player.volume = lastAudibleVolume > 0 ? lastAudibleVolume : 0.5;
      }
    } else {
      /* muted 与 volume 分离：静音不改写真实音量，记忆原样保留 */
      player.muted = true;
    }
  };
  const handleMuteToggle = () => {
    /* 图标与标签的口径是「已静默」（muted 或音量为零）：静默态点击一律恢复声音；
       恢复记忆的非零音量，从未有过非零值时才给保守默认 */
    applyMuteToggle();
    /* 明确点击展开音量面并钉住控制台（触屏也由此可操作滑杆） */
    volumeClickPin = true;
    syncVolumeOpen();
  };
  /* N5PR-P1：音量 ±5% 步进（↑/↓ 键；P3 滚轮复用）。以整数百分比步进避免
     二进制浮点漂移；↓ 触底只归零不改写 lastAudibleVolume（记忆音量原样
     保留）；↑ 自静默恢复：先解除静音，真零时回到记忆音量再叠加步进。 */
  const stepPlayerVolume = (delta) => {
    if (delta > 0) {
      if (player.muted) player.muted = false;
      if (Number(player.volume) === 0) {
        player.volume = lastAudibleVolume > 0 ? lastAudibleVolume : 0.5;
        announcePlayerStatus(`音量 ${Math.round(player.volume * 100)}%`);
        return;
      }
    }
    const percent = Math.round(Number(player.volume) * 100) + Math.round(delta * 100);
    const clamped = Math.min(Math.max(percent, 0), 100) / 100;
    player.volume = clamped;
    announcePlayerStatus(`音量 ${Math.round(clamped * 100)}%`);
  };
  /* 补充E①：长按 ↑/↓ 连续音量——复用长按右的 keyup 裁决同款：短按一步
     （keydown 即步进），按住跨过 PLAYER_HOLD_THRESHOLD_MS 后进入连续步进
     （OSD 随每步播报，沿用 U③ 渐显渐隐节奏的同一 announce 通道）；松开
     即停；触底/满格收口（0/100 自停，OSD 停在终值）。OS 自动重复忽略——
     连续步进由本模块计时器驱动，绝不双倍速。 */
  const VOLUME_HOLD_INTERVAL_MS = 120;
  let volumeHoldDirection = 0;
  let volumeHoldTimer = 0;
  let volumeHoldInterval = 0;
  const stopVolumeHold = () => {
    if (volumeHoldTimer) {
      window.clearTimeout(volumeHoldTimer);
      volumeHoldTimer = 0;
    }
    if (volumeHoldInterval) {
      window.clearInterval(volumeHoldInterval);
      volumeHoldInterval = 0;
    }
    volumeHoldDirection = 0;
  };
  const startVolumeHold = (direction) => {
    stopVolumeHold();
    volumeHoldDirection = direction;
    volumeHoldTimer = window.setTimeout(() => {
      volumeHoldTimer = 0;
      volumeHoldInterval = window.setInterval(() => {
        const before = Math.round(Number(player.volume) * 100);
        stepPlayerVolume(volumeHoldDirection * 0.05);
        const after = Math.round(Number(player.volume) * 100);
        if (after === before && (after === 0 || after === 100)) {
          stopVolumeHold(); /* 边界收口：0/100 已到头 */
        }
      }, VOLUME_HOLD_INTERVAL_MS);
    }, PLAYER_HOLD_THRESHOLD_MS);
  };
  const handleVolumeInput = () => {
    const requested = Number($("player-ctrl-volume").value);
    if (!Number.isFinite(requested)) return; /* 畸形值防御：不动媒体音量 */
    const clamped = Math.min(Math.max(requested, 0), 1);
    if (clamped > 0) {
      /* 拖到非零：解除静音并写真实音量（>0 的值同时成为新的记忆） */
      volumeClickPin = true;
      syncVolumeOpen();
      if (player.muted) player.muted = false;
      player.volume = clamped;
    } else {
      /* 拖到零：静默态；记忆的非零值不被改写 */
      player.volume = 0;
    }
  };
  const handleVolumeSurfacePointerDown = () => {
    volumeClickPin = true;
    syncVolumeOpen();
  };
  const handleVolumeGroupEnter = (event) => {
    if (event.pointerType === "touch") return; /* 触屏走点击展开 */
    volumeHover = true;
    syncVolumeOpen();
  };
  const handleVolumeGroupLeave = () => {
    volumeHover = false;
    syncVolumeOpen();
  };
  const handleVolumeGroupFocusIn = () => {
    volumeFocus = true;
    syncVolumeOpen();
  };
  const handleVolumeGroupFocusOut = (event) => {
    const to = event.relatedTarget;
    if (to && typeof to.closest === "function" && to.closest(".player-volume")) return;
    volumeFocus = false;
    syncVolumeOpen();
  };
  const handleVolumeGroupEscape = (event) => {
    if (event.key !== "Escape") return;
    volumeClickPin = false;
    syncVolumeOpen();
  };
  const handleDocPointerDown = (event) => {
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest(".player-volume")) return;
    if (volumeClickPin) {
      volumeClickPin = false;
      syncVolumeOpen();
    }
  };
  const handleSpeedChange = () => {
    const rate = Number($("player-ctrl-speed").value);
    if (!Number.isFinite(rate) || rate <= 0) return;
    if (holdRateActive) {
      /* 按住快进期间改选速度：记录为用户所选，松开右方向时恢复成它 */
      userRateBeforeHold = rate;
      return;
    }
    player.playbackRate = rate;
    rememberRateFor(currentCourseId(), rate);
    if (rate < 1) recordInsightEvent("slow_rate");
    announcePlayerStatus(`播放速度 ${rate}×`);
    releaseControlsFocusNow(); /* 补充I：选定=交互完成，焦点立即归还（左右键即刻可用） */
  };
  const handleRateChange = () => {
    /* 任何来源的倍速变化都回写速度选择器 */
    const rate = Number(player.playbackRate);
    if (Number.isFinite(rate) && rate > 0) {
      $("player-ctrl-speed").value = String(rate);
      syncDropdown($("player-ctrl-speed")); /* 甲2：触发钮文案跟随（含追帧层改速） */
    }
  };
  const handleSubtitleToggle = () => {
    if (!subtitleTextTrack || playbackKind !== "lecture") return;
    subtitleTextTrack.mode = subtitlesOn ? "disabled" : "hidden";
    applySubtitleMode();
  };
  /* D3：字幕按钮长按 400ms 打开样式面板；面板打开后松开产生的那次 click
     被吞掉，绝不二次触发字幕开关。滑动抬手/取消一律撤销武装。 */
  let subtitleHoldTimer = 0;
  let subtitleHoldFired = false;
  const openSubtitleStyleDialog = () => {
    const dialog = $("player-subtitle-style-dialog");
    if (!dialog) return;
    syncSubtitleStyleDialog();
    dialog.showModal();
  };
  const handleSubtitlePointerDown = () => {
    if (!subtitleTextTrack || playbackKind !== "lecture") return;
    subtitleHoldFired = false;
    subtitleHoldTimer = window.setTimeout(() => {
      subtitleHoldTimer = 0;
      subtitleHoldFired = true;
      openSubtitleStyleDialog();
    }, 400);
  };
  const handleSubtitleHoldCancel = () => {
    if (subtitleHoldTimer) {
      window.clearTimeout(subtitleHoldTimer);
      subtitleHoldTimer = 0;
    }
  };
  const handleSubtitleButtonClick = () => {
    if (subtitleHoldFired) {
      subtitleHoldFired = false;
      return;
    }
    handleSubtitleToggle();
  };
  const handleSubtitleStyleDialogClick = (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const group = target.closest(".subtitle-style-group");
    const value = target?.dataset?.styleValue;
    if (!group || !value) return;
    writeSubtitleStyle({ [String(group.dataset.styleKey)]: String(value) });
  };
  const handlePipToggle = () => {
    if (!pipSupported) return;
    if (document.pictureInPictureElement === player) {
      Promise.resolve(document.exitPictureInPicture?.()).catch(() => announcePlayerStatus("退出画中画未完成"));
      return;
    }
    Promise.resolve(player.requestPictureInPicture?.())
      .then(() => announcePlayerStatus("已进入画中画"))
      .catch(() => announcePlayerStatus("浏览器拒绝了画中画请求"));
  };
  const handleTheatreToggle = () => {
    const button = $("player-ctrl-theatre");
    const enable = button.getAttribute("aria-pressed") !== "true";
    shell.classList.toggle("theatre", enable);
    button.setAttribute("aria-pressed", String(enable));
    button.setAttribute("aria-label", enable ? "关闭影院模式" : "开启影院模式");
    announcePlayerStatus(enable ? "影院模式已开启" : "影院模式已关闭");
  };
  const handleFullscreenToggle = () => {
    if (document.fullscreenElement === shell) {
      Promise.resolve(document.exitFullscreen?.()).catch(() => announcePlayerStatus("退出全屏未完成，可再次尝试"));
      return;
    }
    if (typeof shell.requestFullscreen !== "function") {
      announcePlayerStatus("当前浏览器不支持全屏");
      return;
    }
    /* 直接在本次用户手势内对 shell 请求全屏；异步等待或二次请求会丢失瞬态激活 */
    Promise.resolve(shell.requestFullscreen()).catch(() => announcePlayerStatus("浏览器拒绝了全屏请求，控制台仍可使用"));
  };
  const handleFullscreenChange = () => {
    syncFullscreenButton();
    /* 全屏切换收敛：清掉切换前武装的 idle 计时器，按当前钉住状态重新裁决 */
    refreshChrome();
  };
  /* PLAYER-INTERACT-REPAIR-1 单元一：画面点击切换播放、双击切换全屏（未聚焦态
     老交互回归）。控制台与直播 pill 上的点击让位——它们各有自己的动作。 */
  const shellSurfaceOwnsPointer = (event) => {
    if (disposed || playbackKind === "none") return false;
    const target = event.target;
    if (target && typeof target.closest === "function"
      && target.closest(".player-controls")) return false;
    return true;
  };
  const handleShellSurfaceClick = (event) => {
    if (shellSurfaceOwnsPointer(event)) togglePlayback(true);
  };
  const handleShellSurfaceDoubleClick = (event) => {
    if (shellSurfaceOwnsPointer(event)) handleFullscreenToggle();
  };
  /* 单元二（选中态政策）：控制器内鼠标按下不落焦点——点击后零焦点环、零
     「空格误触发按钮」；键盘 Tab 聚焦与 :focus-visible 环不受影响。滑杆让位
     （拖动语义属原生 input，指针焦点本就不匹配 :focus-visible，无环可现）。
     甲2 后倍速已是自绘下拉（click 展开不受 mousedown preventDefault 影响），
     原生 select 让位条款随之退役。 */
  const handleShellSurfaceMouseDown = (event) => {
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest("input, select")) return;
    event.preventDefault();
  };
  const handleTimelineInput = () => {
    const duration = finiteDuration(player);
    if (!duration) return;
    const requested = Number($("player-ctrl-timeline").value);
    if (!Number.isFinite(requested)) return;
    /* SEEK-DRAG-1（PLAYER-OPT-1 20261008 夜，痛点项）：拖动中逐格写 currentTime
       在慢流（校外 WebVPN/WebVPN 中转是明确支持场景）下=每格一次 range 请求风暴，
       拖一下卡一路。业界标准（B 站/YouTube）：拖动中只预览、释放才提交。
       显示层保持实时跟随：thumb=原生 value，fill 比率与时间气泡同步刷新；
       媒体 seek 在释放（handleTimelineScrubEnd→本函数，此时已不在拖动态）一次性
       提交。键盘/程序化路径（无拖动态，input+change 同步到）保持即时 seek，
       方向键 scrub 的画面跟随不受影响。 */
    if (timelineScrubbing) {
      const ratio = String(Math.min(Math.max(requested / duration, 0), 1));
      const fill = $("player-timeline-fill");
      if (fill) fill.style.setProperty("--play-ratio", ratio);
      const bubble = $("player-timeline-bubble");
      if (bubble) {
        bubble.textContent = formatPlayerTime(requested);
        bubble.style.setProperty("--bubble-ratio", ratio);
        bubble.hidden = false;
      }
      return;
    }
    player.currentTime = Math.min(Math.max(requested, 0), duration);
  };
  const handleTimelineScrubStart = () => {
    timelineScrubbing = true;
    const timelineWrap = $("player-timeline");
    if (timelineWrap) timelineWrap.dataset.scrubbing = "true";
    pokeChrome(); /* 拖动中钉住控制台 */
  };
  const handleTimelineScrubEnd = () => {
    timelineScrubbing = false;
    releaseControlsFocusNow(); /* 补充I：拖动释放即归还（键盘 change 由 data-input=key 豁免） */
    const timelineWrap = $("player-timeline");
    if (timelineWrap) timelineWrap.dataset.scrubbing = "false";
    handleTimelineInput();
    syncPlayerTimeline();
    announcePlayerStatus(`已跳转到 ${formatPlayerTime(player.currentTime)}`);
    pokeChrome(); /* 收口后恢复常规 idle 处理 */
  };
  /* N10B-SUP-1 卡③（用户实证「点击组件后快捷键失效」）：音量滑杆是 deck 内唯一
   * 原生聚焦元素（handleShellSurfaceMouseDown 对按钮 preventDefault 不夺焦），
   * 点击滑杆后焦点永久滞留 input → 后续空格/方向键全部让位失效。两修：
   * ①pointerup 指针释放即归还（与 timeline 补充 I 同族；data-input=key 键盘
   *   调节豁免，不打断方向键调音量）；
   * ②滑杆空格缝隙：input[type=range] 原生不消费空格，让位纯损失——滑杆上
   *   空格直接 togglePlayback（实证成员误让位的局部补语义，不改 yield 选择器）。 */
  const handleVolumePointerRelease = () => {
    releaseControlsFocusNow();
  };
  const handleSliderSpacekick = (event) => {
    if (event.key !== " " || event.repeat) return;
    event.preventDefault();
    togglePlayback(true);
  };
  /* N5PR-P4：气泡跟随（容器接 pointermove——真实浏览器里 input 上的指针
     事件冒泡到此；rAF 合帧，拖动与悬停同一路径）。 */
  const flushTimelineBubble = () => {
    bubbleRaf = 0;
    if (pendingBubbleRatio == null) return;
    const duration = finiteDuration(player);
    const bubble = $("player-timeline-bubble");
    if (!bubble || duration <= 0) return;
    const ratio = Math.min(Math.max(pendingBubbleRatio, 0), 1);
    bubble.textContent = formatPlayerTime(ratio * duration);
    bubble.style.setProperty("--bubble-ratio", String(ratio));
    bubble.hidden = false;
  };
  const handleTimelinePointerMove = (event) => {
    if (!canSeekPlayer()) {
      hideTimelineBubble();
      return;
    }
    const hit = $("player-ctrl-timeline");
    const rect = typeof hit.getBoundingClientRect === "function" ? hit.getBoundingClientRect() : null;
    const width = Number(rect?.width) || 0;
    if (!(width > 0)) return;
    const x = Number(event.clientX) - Number(rect?.left || 0);
    pendingBubbleRatio = x / width;
    if (!bubbleRaf) bubbleRaf = window.requestAnimationFrame(flushTimelineBubble);
  };
  /* PLAYER-UX-1④：标记悬停由时间轴指针驱动（N5PR-P4 同哲学——容器接
     pointermove，标记点与 input 上的指针事件都冒泡到此；按指针 x 就近点亮
     data-hover，与 ：hover/:focus-within 同权开操作菜单。D-20261009-08 后标记
     层已提 z-index 可直接 ：hover，本邻近点亮路径保留=悬停语义单源不变）。
     合成壳无 getBoundingClientRect 时静默跳过（行为钉走 classList 直驱）。 */
  const syncTimelineFlagHover = (event) => {
    const mount = $("player-timeline-flags");
    if (!mount || typeof mount.querySelectorAll !== "function") return;
    const markers = [...mount.querySelectorAll(".player-flag-marker")];
    if (!markers.length) return;
    const hit = $("player-ctrl-timeline");
    const rect = typeof hit?.getBoundingClientRect === "function" ? hit.getBoundingClientRect() : null;
    if (!rect) return;
    const pointerX = Number(event.clientX) - Number(rect.left || 0);
    let best = null;
    let bestDist = Infinity;
    for (const marker of markers) {
      if (typeof marker.getBoundingClientRect !== "function") return;
      const box = marker.getBoundingClientRect();
      const distance = Math.abs(Number(box.left || 0) + Number(box.width || 0) / 2 - Number(event.clientX));
      if (distance < bestDist) { bestDist = distance; best = marker; }
    }
    for (const marker of markers) {
      const active = marker === best && bestDist <= 10;
      if (String(marker.dataset.hover || "") !== (active ? "1" : "")) {
        if (active) marker.dataset.hover = "1";
        else delete marker.dataset.hover;
      }
    }
  };
  const clearTimelineFlagHover = () => {
    const mount = $("player-timeline-flags");
    if (!mount || typeof mount.querySelectorAll !== "function") return;
    for (const marker of mount.querySelectorAll(".player-flag-marker")) {
      delete marker.dataset.hover;
    }
  };
  /* N5PR-P3：shell 悬停滚轮音量 ±5%。passive 不阻塞滚动；焦点在滑杆/输入等
     可交互元素上时让位（不抢滑杆的滚轮语义）；步进/触底/复响语义全部复用
     stepPlayerVolume（volumechange 同步链 + 轻提示 OSD 同帧跟随）。 */
  const handleShellWheel = (event) => {
    if (disposed || playbackKind === "none") return;
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest(PLAYER_HOVER_YIELD_SELECTOR)) return;
    const delta = Number(event.deltaY);
    if (!Number.isFinite(delta) || delta === 0) return;
    stepPlayerVolume(delta < 0 ? 0.05 : -0.05);
  };
  const shortcutOwnsEvent = (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return false;
    /* FOCUS-DRIFT-1 细化：目标让位语义统一走 ui.js 焦点政策 chokepoint——
       字段/菜单/标签页整块让位；button/a 仅激活键（Space/Enter）让位，其余键
       （方向/J/K/L/字母）透传播放快捷键，点击按钮后快捷键即刻存活。 */
    return !pageShortcutBlockedByTarget(event);
  };
  /* PLAYER-INTERACT-REPAIR-1 修正（原 S09-D 快捷键收权）＋ FOCUS-DRIFT-1 细化：
     归属按「焦点在哪」判定，绝不要求播放器先被聚焦——学习桌可见（讲次/直播
     任一在播）时，页面级未聚焦态的点击/空格/方向键/长按/双击全部可用。让位面
     三层：①字段/滑杆/菜单/标签页等自带编辑或方向语义的元素整块让位；②聚焦
     button/a 时仅激活键（Space/Enter）让位给原生激活（键盘可达硬门），方向/
     J/K/L 等透传快捷键——点击按钮后快捷键即刻存活；③任一模态 dialog 打开
     期间一律让位（绝不隔模态操控背景播放器），唯 `?` 豁免——它是键位帮助
     弹窗自身的管理键（开/关都走它）。学习桌不可见（返回选择面/切页）
     后快捷键让位，绝不隔空操控不可见播放器。 */
  const playerOwnsKeyEvent = (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return false;
    if (target.closest(PLAYER_SHORTCUT_EDITABLE)) return false;
    if (pageShortcutBlockedByTarget(event)) return false;
    if (hasOpenDialog() && event.key !== "?") return false;
    const desk = $("study-desk");
    return Boolean(desk) && desk.hidden === false;
  };
  const canSeekPlayer = () => playbackKind === "lecture" && finiteDuration(player) > 0;
  const enterHoldRate = () => {
    rightHoldTimer = 0;
    pendingRightSeek = false; /* 已进入倍速：这次按压被消费，不再是待执行短按 */
    if (disposed || holdRateActive || playbackKind === "none") return;
    holdRateActive = true;
    userRateBeforeHold = Number(player.playbackRate) || 1;
    player.playbackRate = PLAYER_HOLD_RATE;
    announcePlayerStatus(`${PLAYER_HOLD_RATE.toFixed(1)}×播放中`, PLAYER_OSD_HOLD_HINT_MS);
  };
  const restoreHoldRate = (announce) => {
    if (rightHoldTimer) {
      window.clearTimeout(rightHoldTimer);
      rightHoldTimer = 0;
    }
    /* 失焦/切页/切讲次等旁路收口：未决短按一并丢弃，绝不隔空 seek */
    pendingRightSeek = false;
    if (!holdRateActive) return;
    holdRateActive = false;
    player.playbackRate = userRateBeforeHold;
    if (announce) announcePlayerStatus(`已恢复 ${userRateBeforeHold}× 播放速度`);
  };
  const seekByPlayerSeconds = (delta) => {
    /* 仅讲次回放可 seek；直播（时间轴禁用）与无媒体一律不动 */
    if (!canSeekPlayer()) return false;
    const duration = finiteDuration(player);
    const limit = Math.min(duration, seekableEnd(player) || duration);
    const target = Math.min(Math.max(Number(player.currentTime) + delta, 0), limit);
    player.currentTime = target;
    announcePlayerStatus(`${delta > 0 ? "快进" : "快退"} ${Math.abs(delta)} 秒 · ${formatPlayerTime(player.currentTime)}`);
    return true;
  };
  const handlePlayerKeydown = (event) => {
    if (disposed || playbackKind === "none") return;
    if (!playerOwnsKeyEvent(event) || !shortcutOwnsEvent(event)) return;
    if (event.key === "ArrowRight") {
      /* B 站式长按裁决：按下不立即 seek——短按在松开时快进 5 秒，按住超过
         阈值进入临时 3 倍速（消费按压）。绝不「先跳一次再倍速」；
         OS 自动重复 keydown 忽略（倍速由计时器接管）。 */
      if (event.repeat) return;
      if (!canSeekPlayer()) return;
      event.preventDefault(); /* 仅在实际消费快捷键时阻止默认 */
      pendingRightSeek = true;
      rightHoldTimer = window.setTimeout(enterHoldRate, PLAYER_HOLD_THRESHOLD_MS);
      return;
    }
    if (event.key === "ArrowLeft") {
      if (seekByPlayerSeconds(-PLAYER_SEEK_SECONDS)) event.preventDefault();
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      togglePlayback(true);
      return;
    }
    if (event.key === "j" || event.key === "J") {
      if (event.repeat) return;
      if (seekByPlayerSeconds(-PLAYER_ALT_SEEK_SECONDS)) event.preventDefault();
      return;
    }
    if (event.key === "l" || event.key === "L") {
      if (event.repeat) return;
      if (seekByPlayerSeconds(PLAYER_ALT_SEEK_SECONDS)) event.preventDefault();
      return;
    }
    if (event.key === ">" || event.key === "<") {
      if (event.repeat) return;
      const rates = PLAYER_SPEED_STEPS;
      const current = Number(player.playbackRate) || 1;
      let index = rates.findIndex((rate) => Math.abs(rate - current) < 0.001);
      if (index < 0) index = rates.indexOf(1);
      const next = rates[Math.min(Math.max(index + (event.key === ">" ? 1 : -1), 0), rates.length - 1)];
      if (next !== current) {
        player.playbackRate = next;
        /* N5PR-P2：</> 步进=用户显式选速，与 select 同写每课记忆 */
        rememberRateFor(currentCourseId(), next);
        if (next < 1) recordInsightEvent("slow_rate");
        announcePlayerStatus(`播放速度 ${next}×`);
      }
      event.preventDefault();
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      const dialog = $("player-keys-dialog");
      if (!dialog) return;
      if (dialog.open) dialog.close();
      else dialog.showModal();
      return;
    }
    if (event.key === "Escape") {
      /* L96'：续播浮层 Esc 收口（模态 dialog 打开时上面 hasOpenDialog 已让位，
         Esc 归浏览器关闭 dialog——浮层只在无模态时被收）。无浮层时 Esc 无操作。 */
      if (resumeBannerNode) {
        event.preventDefault();
        dismissResumeBanner();
      }
      return;
    }
    /* N5PR-P1（P27/P30/P31/P32/P40 缺键补齐）：K/M/F/C/↑↓ 对齐 B 站/YouTube
       肌肉记忆；全部复用既有动作核心，编辑态让位与 repeat 忽略与 J/L 一致。 */
    if (event.key === "k" || event.key === "K") {
      if (event.repeat) return;
      event.preventDefault();
      togglePlayback(true);
      return;
    }
    if (event.key === "m" || event.key === "M") {
      if (event.repeat) return;
      event.preventDefault();
      applyMuteToggle();
      return;
    }
    if (event.key === "f" || event.key === "F") {
      if (event.repeat) return;
      event.preventDefault();
      handleFullscreenToggle();
      return;
    }
    if (event.key === "c" || event.key === "C") {
      if (event.repeat) return;
      /* 无字幕轨/非讲次回放=静默忽略（与字幕按钮 hidden 语义一致） */
      if (!subtitleTextTrack || playbackKind !== "lecture") return;
      event.preventDefault();
      handleSubtitleToggle();
      return;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      if (event.repeat) return; /* OS 自动重复忽略：连续步进由长按计时器驱动 */
      event.preventDefault();
      const direction = event.key === "ArrowUp" ? 1 : -1;
      stepPlayerVolume(direction * 0.05);
      startVolumeHold(direction);
      return;
    }
    /* D2：X=没听懂，一键标记当前时刻（非讲次回放静默忽略） */
    if (event.key === "x" || event.key === "X") {
      if (event.repeat) return;
      if (playbackKind !== "lecture") return;
      event.preventDefault();
      markNotUnderstood();
      return;
    }
  };
  const handlePlayerKeyup = (event) => {
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      stopVolumeHold(); /* 松开即停（长按连续音量的 keyup 裁决收口） */
      return;
    }
    if (event.key !== "ArrowRight") return;
    if (holdRateActive) {
      restoreHoldRate(true);
      return;
    }
    if (rightHoldTimer) {
      window.clearTimeout(rightHoldTimer);
      rightHoldTimer = 0;
    }
    /* 短按（未达阈值）：松开才执行这一次 5 秒快进 */
    if (pendingRightSeek) {
      pendingRightSeek = false;
      seekByPlayerSeconds(PLAYER_SEEK_SECONDS);
    }
  };
  const handlePlayerWindowBlur = () => {
    restoreHoldRate(false);
    stopVolumeHold(); /* 失焦确定性收敛：连续音量步进一并停 */
    /* 失焦确定性收敛：清 idle、控制台可见、音量面瞬时态复位（不动焦点） */
    volumeHover = false;
    volumeClickPin = false;
    syncVolumeOpen();
    clearIdleTimer();
    setChromeVisible(true);
  };
  // ---- S10-A 控制台显隐状态机：指针悬停（仅鼠标）揭示并武装/重置同一 idle 计时器；
  // ---- 指针离开/取消只收敛（不揭示、不武装）；触控/笔保持点按切换；键盘来源
  // ---- （:focus-visible）焦点钉住控制台，鼠标点击引发的焦点移动不钉住。 ----
  const handleShellPointerHover = (event) => {
    if (event.pointerType !== "mouse") return; /* 触控/笔无悬停语义：点按切换负责 */
    pokeChrome();
  };
  const handleShellPointerExit = () => {
    if (!controlsDeckPresent) return;
    if (chromePinned()) {
      refreshChrome(); /* 钉住条件成立：收敛为可见，不武装收起计时器 */
      return;
    }
    clearIdleTimer();
    setChromeVisible(false); /* 离开即按正常过渡收起，绝不重新揭示/武装 */
  };
  const handleShellPointerUp = (event) => {
    /* 粗指针点按视频区切换控制台（不依赖悬停）；点在控制台上不算切换 */
    if (event.pointerType !== "touch" && event.pointerType !== "pen") return;
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest(".player-controls")) return;
    if (chromeVisible() && !chromePinned()) {
      clearIdleTimer();
      setChromeVisible(false);
    } else {
      pokeChrome();
    }
  };
  const handleShellFocusIn = (event) => {
    const target = event.target;
    keyboardFocusInsidePlayer = Boolean(
      target && typeof target.matches === "function" && target.matches(":focus-visible"),
    );
    refreshChrome(); /* 键盘焦点钉住；指针来源焦点不钉住（恢复常规 idle 处理） */
    armControlsFocusRelease(); /* 补充G：控制块内焦点 3s 无交互自动归还 */
  };
  /* 补充G：控件交互（change/input/pointer）重臂同一归还计时 */
  const handleDeckInteraction = () => armControlsFocusRelease();
  /* PLAYER-UX-1①：deck 悬停豁免三入口（enter 仅鼠标；leave/cancel 一律解除） */
  const handleDeckHoverEnter = (event) => {
    if (event.pointerType !== "mouse") return;
    deckHover = true;
    refreshChrome();
  };
  const handleDeckHoverLeave = () => {
    if (!deckHover) return;
    deckHover = false;
    refreshChrome();
  };
  const handleDeckHoverCancel = () => {
    deckHover = false;
    refreshChrome();
  };
  const handleShellFocusOut = (event) => {
    const to = event.relatedTarget;
    if (to && typeof to.closest === "function" && to.closest(".player-stage-shell")) return;
    keyboardFocusInsidePlayer = false;
    refreshChrome(); /* 焦点离开整个播放器后恢复常规 idle 处理 */
  };
  const handleBufferingStart = () => {
    mediaBuffering = true;
    refreshChrome();
    /* N5PR-P4：300ms 防闪窗后才亮缓冲指示；恢复面板可见则互斥不叠加 */
    if (spinnerTimer) return;
    spinnerTimer = window.setTimeout(() => {
      spinnerTimer = 0;
      const recovery = $("player-recovery");
      if (!mediaBuffering || (recovery && recovery.hidden === false)) return;
      const spinner = $("player-stage-spinner");
      if (spinner) spinner.hidden = false;
    }, PLAYER_SPINNER_DELAY_MS);
  };
  const handleBufferingEnd = () => {
    mediaBuffering = false;
    clearBufferSpinner();
    refreshChrome();
  };
  const handleDocVisibilityChange = () => {
    volumeHover = false;
    volumeClickPin = false;
    syncVolumeOpen();
    /* D11：重武装 idle——回标签页立即揭示并按钉住状态重新裁决收起；
       播放中控制台不再滞留可见到下一次指针事件。 */
    pokeChrome();
  };
  /* PLAYER-UX-1④：书签变更（任一表面删除/新建/收起）后时间轴双形态收敛——
     会话 flag 按服务端存活 id 修剪，持久 assess 刻度重取。150ms 去抖合并同拍
     多次派发；列表暂时不可得时标记保持现状（下次变更再收敛），绝不误删。 */
  const handleBookmarksChanged = () => {
    if (bookmarkSyncTimer) return;
    bookmarkSyncTimer = window.setTimeout(() => {
      bookmarkSyncTimer = 0;
      const lecture = playbackKind === "lecture" ? playerStore?.activeLecture : null;
      const subId = String(lecture?.sub_id || "");
      if (!subId) return;
      void apiV3(`bookmarks?sub_id=${encodeURIComponent(subId)}`).then((value) => {
        if (String(playerStore?.activeLecture?.sub_id || "") !== subId) return;
        const ids = new Set((Array.isArray(value?.bookmarks) ? value.bookmarks : [])
          .map((item) => String(item?.bookmark_id || "")));
        const mount = $("player-timeline-flags");
        if (mount && typeof mount.querySelectorAll === "function") {
          for (const marker of [...mount.querySelectorAll(".player-flag-marker")]) {
            const id = String(marker.dataset.bookmarkId || "");
            if (id && !ids.has(id)) marker.remove();
          }
        }
        void hydrateAssessMarkers(lecture);
      }).catch(() => {});
    }, 150);
  };
  window.addEventListener("courselens:logout", handleLogoutClear);
  window.addEventListener("courselens:page", handlePageChanged);
  window.addEventListener("courselens:bookmarks-changed", handleBookmarksChanged);
  player.addEventListener("error", showMediaFailure);
  player.addEventListener("loadedmetadata", handleLoadedMetadata);
  player.addEventListener("loadedmetadata", applySavedRate);
  window.addEventListener("courselens:chapters", handleChaptersEvent);
  player.addEventListener("playing", clearPlaybackRecovery);
  document.addEventListener("fullscreenchange", handleFullscreenChange);
  const unsubscribeTranscriptTiming = store.subscribe("transcriptHasTiming", (value) => {
    transcriptTimed = value === true;
    /* 确认值绑定当前讲次（study.js 每次字幕读取落地/失败都会重设本键） */
    transcriptTimedSubId = String(store.activeLecture?.sub_id || "");
    /* N10B-2：字幕文件轨随事实挂/卸（无字幕讲次零 /subtitles/file 请求） */
    mountSubtitleTrackFromFile(value === true);
    updateLectureActions(store.activeLecture);
  });
  const handleGenerateSubtitle = () => void enqueue(store, "subtitle");
  const handleGenerateNotes = () => void enqueue(store, "summary");
  const handleOpenTaskDrawer = () => {
    window.dispatchEvent(new CustomEvent("courselens:open-tasks", { detail: { trigger: $("player-task-open-drawer") } }));
  };
  /* D8：chip 点击开同一任务抽屉（触发元素=chip 本身，供抽屉焦点归还） */
  const handleSubtitleChipClick = () => {
    window.dispatchEvent(new CustomEvent("courselens:open-tasks", { detail: { trigger: $("player-subtitle-chip") } }));
  };
  const publishTranscriptTime = () => {
    const subId = String(store.activeLecture?.sub_id || "");
    if (subId) window.dispatchEvent(new CustomEvent("courselens:transcript-time", {
      detail: { sub_id: subId, time_ms: Math.max(0, player.currentTime * 1000) },
    }));
  };
  /* P3-B：seek 意图捕获——seeking 事件的 currentTime 即用户目标位置；
     错误回卷后恢复重载仍回到这里。恢复/讲次切换时随之清除。 */
  const handleSeekingIntent = () => {
    /* 加载期用户主动 seek：取消持久进度恢复，尊重用户自己的定位意图 */
    if (restorePendingForLoad) {
      restorePendingForLoad = false;
      persistedRestore = null;
    }
    /* E（20261008 夜走查实锤）：用户主动改道=续播浮层使命结束——「已从上次
       位置 X 继续播放」的陈述在 seek 后即刻失真，滞留到 12s 超时是误导；
       「从头看」路径自身先 dismiss 再 seek，与此收口无互扰。 */
    dismissResumeBanner();
    const target = Number(player.currentTime);
    if (Number.isFinite(target) && target >= 0) pendingSeekTarget = target;
    /* D7：回看信号——向回跳 >2s 记 seek_back；落在上一个回看点 ±15s 内
       =「又回来听」记 replay（不判难点，只陈述事实）。 */
    if (insightEnabled() && playbackKind === "lecture" && Number.isFinite(target)) {
      const targetMs = target * 1000;
      if (Number.isFinite(insightLastPositionMs) && targetMs < insightLastPositionMs - 2000) {
        recordInsightEvent("seek_back");
        if (insightLastSeekBackMs != null && Math.abs(targetMs - insightLastSeekBackMs) <= 15000) {
          recordInsightEvent("replay");
        }
        insightLastSeekBackMs = targetMs;
      }
    }
  };
  const handleTimeUpdate = () => {
    /* W1：播放位置已从 seek 目标真实推进——seek 意图立即过期，currentTime 即
       权威位置，恢复重载绝不再回卷到陈旧 seek 点；seek 进行中被错误打断
       （无推进）时意图存活，retry-media 仍优先消费。 */
    pendingSeekTarget = null;
    insightLastPositionMs = (Number(player.currentTime) || 0) * 1000;
    publishTranscriptTime();
    renderSubtitleOverlay();
    syncPlayerTimeline();
    if (!saveTimer) saveTimer = window.setTimeout(() => {
      saveTimer = 0;
      void saveProgress(store).catch(() => {});
    }, 10000);
  };
  const handlePause = () => {
    syncPlayButton();
    syncPlayerTimeline();
    refreshChrome(); /* PLAYER-UX-1①：暂停与播放同语义——重裁决进空闲计时（豁免面保可见） */
    recordInsightEvent("pause");
    flushInsightEvents(); /* 暂停=自然收口点：未满 10s 的批随之落库 */
    stopStudyHeartbeat(); /* 暂停即停表：零头诚实丢弃 */
    void saveProgress(store).catch(() => {});
  };
  const handleEnded = () => {
    syncPlayButton();
    syncPlayerTimeline();
    refreshChrome(); /* 结束钉住 */
    stopStudyHeartbeat();
    void saveProgress(store, true).catch(() => {});
  };
  const handlePlayStateChange = () => refreshChrome();
  $("generate-subtitle").addEventListener("click", handleGenerateSubtitle);
  $("generate-notes").addEventListener("click", handleGenerateNotes);
  $("player-task-open-drawer").addEventListener("click", handleOpenTaskDrawer);
  /* D8/P4：chip 与时间轴容器在精简 fixture（可用性行为 harness）里不存在，
     接线一律先探测（控制台完整时恒存在）。 */
  const subtitleChip = $("player-subtitle-chip");
  const timelineWrapEl = $("player-timeline");
  const bookmarkButton = $("player-ctrl-bookmark");
  if (subtitleChip) subtitleChip.addEventListener("click", handleSubtitleChipClick);
  if (bookmarkButton) bookmarkButton.addEventListener("click", markNotUnderstood);
  /* D7：学习洞察对话框（存在性守卫同 chip；精简 fixture 无这些节点）。
     AS5：开关条目退役（默认开，显式 off 的本地偏好仍在读取链被尊重）；
     抹除移到每个课次的操作区，两击臂 + 只抹当前讲次。 */
  const insightOpen = $("player-insight-open");
  const insightDialog = $("player-insight-dialog");
  const insightErase = $("insight-erase");
  const handleInsightOpen = () => {
    if (insightDialog) insightDialog.showModal();
  };
  const handleInsightErase = () => {
    const button = $("insight-erase");
    if (!button) return;
    if (button.dataset.confirming !== "true") {
      /* 第一击只挂臂：讲清删什么，6s 无确认自动解除 */
      button.dataset.confirming = "true";
      button.textContent = INSIGHT_ERASE_CONFIRM_LABEL;
      if (insightEraseArmTimer) window.clearTimeout(insightEraseArmTimer);
      insightEraseArmTimer = window.setTimeout(() => resetInsightEraseArm(), 6000);
      return;
    }
    const subId = String(playerStore?.activeLecture?.sub_id || "");
    resetInsightEraseArm();
    if (!subId) return;
    void postV3("watch-events/clear", { sub_id: subId }).then(() => {
      insightHydrateToken += 1;
      clearInsightHeat();
      toast("已抹掉本讲回看热点；字幕、课件和学习记录都不受影响", "ready");
    }).catch(() => {
      toast("抹除没有完成，稍后再试一次", "error");
    });
  };
  if (insightOpen) insightOpen.addEventListener("click", handleInsightOpen);
  if (insightErase) insightErase.addEventListener("click", handleInsightErase);
  if (timelineWrapEl) {
    timelineWrapEl.addEventListener("pointermove", handleTimelinePointerMove);
    timelineWrapEl.addEventListener("pointermove", syncTimelineFlagHover);
    timelineWrapEl.addEventListener("pointerleave", hideTimelineBubble);
    timelineWrapEl.addEventListener("pointerleave", clearTimelineFlagHover);
  }
  const subtitleStyleDialog = $("player-subtitle-style-dialog");
  if (controlsDeckPresent) {
    const controlsDeck = $("player-controls");
    controlsDeck.addEventListener("change", handleDeckInteraction);
    controlsDeck.addEventListener("input", handleDeckInteraction);
    controlsDeck.addEventListener("pointerdown", handleDeckInteraction);
    controlsDeck.addEventListener("pointermove", handleDeckInteraction);
    /* PLAYER-UX-1①：悬停控制台=钉住豁免面（仅鼠标；触控/笔无悬停语义，
       与 shell 悬停揭示同一判别）。离开/取消一律解除并重裁决。 */
    controlsDeck.addEventListener("pointerenter", handleDeckHoverEnter);
    controlsDeck.addEventListener("pointerleave", handleDeckHoverLeave);
    controlsDeck.addEventListener("pointercancel", handleDeckHoverCancel);
    $("player-ctrl-play").addEventListener("click", handlePlayButtonClick);
    $("player-ctrl-mute").addEventListener("click", handleMuteToggle);
    $("player-ctrl-volume").addEventListener("input", handleVolumeInput);
    /* N10B-SUP-1 卡③：滑杆指针释放归还 + 滑杆空格补播放语义 */
    $("player-ctrl-volume").addEventListener("pointerup", handleVolumePointerRelease);
    $("player-ctrl-volume").addEventListener("keydown", handleSliderSpacekick);
    $("player-ctrl-timeline").addEventListener("keydown", handleSliderSpacekick);
    $("player-ctrl-speed").addEventListener("change", handleSpeedChange);
    attachDropdown($("player-ctrl-speed"), { skin: "media" }); /* 甲2：原生倍速下拉退役 */
    $("player-ctrl-subtitle").addEventListener("click", handleSubtitleButtonClick);
    $("player-ctrl-subtitle").addEventListener("pointerdown", handleSubtitlePointerDown);
    $("player-ctrl-subtitle").addEventListener("pointerup", handleSubtitleHoldCancel);
    $("player-ctrl-subtitle").addEventListener("pointercancel", handleSubtitleHoldCancel);
    $("player-ctrl-subtitle").addEventListener("pointerleave", handleSubtitleHoldCancel);
    if (subtitleStyleDialog) subtitleStyleDialog.addEventListener("click", handleSubtitleStyleDialogClick);
    $("player-ctrl-pip").addEventListener("click", handlePipToggle);
    $("player-ctrl-theatre").addEventListener("click", handleTheatreToggle);
    $("player-ctrl-fullscreen").addEventListener("click", handleFullscreenToggle);
    $("player-ctrl-timeline").addEventListener("input", handleTimelineInput);
    $("player-ctrl-timeline").addEventListener("change", handleTimelineScrubEnd);
    $("player-ctrl-timeline").addEventListener("pointerdown", handleTimelineScrubStart);
    $("player-ctrl-timeline").addEventListener("pointerup", handleTimelineScrubEnd);
    $("player-ctrl-timeline").addEventListener("pointercancel", handleTimelineScrubEnd);
    window.addEventListener("keydown", handlePlayerKeydown);
    window.addEventListener("keyup", handlePlayerKeyup);
    window.addEventListener("blur", handlePlayerWindowBlur);
    /* FOCUS-DRIFT-1②：学习桌全域指针激活焦点归还——鼠标点掉的按钮不驻留焦点，
       页面级快捷键即刻回到未聚焦态（capture 段先跑：下拉 D5 等后续 handler
       仍可重新落焦点；键盘模态 data-input=key 绝不归还）。滑杆（input）不在
       button/a 集内，拖动中的焦点不受影响，释放仍走既有 pointerup/3s 归还。 */
    const studyDesk = $("study-desk");
    if (studyDesk) {
      handleDeskClickFocusRelease = () => releasePointerActivatedFocus(studyDesk);
      studyDesk.addEventListener("click", handleDeskClickFocusRelease, true);
    }
    /* S10-A 控制台显隐与音量面：shell 指针悬停/离开、音量组件三输入源、
       文档级外点与可见性收敛 */
    shell.addEventListener("pointerenter", handleShellPointerHover);
    shell.addEventListener("pointermove", handleShellPointerHover);
    shell.addEventListener("pointerleave", handleShellPointerExit);
    shell.addEventListener("pointercancel", handleShellPointerExit);
    shell.addEventListener("pointerup", handleShellPointerUp);
    shell.addEventListener("click", handleShellSurfaceClick);
    shell.addEventListener("dblclick", handleShellSurfaceDoubleClick);
    shell.addEventListener("mousedown", handleShellSurfaceMouseDown);
    shell.addEventListener("wheel", handleShellWheel, { passive: true });
    shell.addEventListener("focusin", handleShellFocusIn);
    shell.addEventListener("focusout", handleShellFocusOut);
    $("player-volume").addEventListener("pointerenter", handleVolumeGroupEnter);
    $("player-volume").addEventListener("pointerleave", handleVolumeGroupLeave);
    $("player-volume").addEventListener("pointerdown", handleVolumeSurfacePointerDown);
    $("player-volume").addEventListener("focusin", handleVolumeGroupFocusIn);
    $("player-volume").addEventListener("focusout", handleVolumeGroupFocusOut);
    $("player-volume").addEventListener("keydown", handleVolumeGroupEscape);
    document.addEventListener("pointerdown", handleDocPointerDown, true);
    document.addEventListener("visibilitychange", handleDocVisibilityChange);
  }
  player.addEventListener("timeupdate", handleTimeUpdate);
  player.addEventListener("seeked", publishTranscriptTime);
  player.addEventListener("seeked", renderSubtitleOverlay);
  player.addEventListener("seeking", handleSeekingIntent);
  player.addEventListener("pause", handlePause);
  player.addEventListener("pause", syncPlayButton);
  player.addEventListener("play", syncPlayButton);
  player.addEventListener("play", handlePlayStateChange);
  player.addEventListener("play", startStudyHeartbeat);
  player.addEventListener("ended", handleEnded);
  player.addEventListener("durationchange", syncPlayerTimeline);
  player.addEventListener("seeking", syncPlayerTimeline);
  player.addEventListener("volumechange", syncVolumeControls);
  player.addEventListener("ratechange", handleRateChange);
  player.addEventListener("waiting", handleBufferingStart);
  player.addEventListener("playing", handleBufferingEnd);
  player.addEventListener("canplay", handleBufferingEnd);
  player.addEventListener("enterpictureinpicture", syncPipButton);
  player.addEventListener("leavepictureinpicture", syncPipButton);
  wireSubtitleTrack();
  syncPlayButton();
  syncVolumeControls();
  syncFullscreenButton();
  setPlayerControlsVisible(false);
  syncSubtitleButton();
  syncPlayerTimeline();
  setChromeVisible(true);
  const unsubscribeActiveLecture = store.subscribe("activeLecture", (lecture) => {
    /* 讲次切换：连续性状态全部失效（待办属于旧讲次，位置与 seek 意图属于
       旧媒体）；P3-B 起统一走同一收口，与登出/身份不一致清除完全一致。 */
    clearContinuityState();
    loadLecture(lecture);
  });
  const unsubscribeAuthContinuity = store.subscribe("auth", handleAuthContinuity);
  const unsubscribeTasksChip = store.subscribe("tasks", syncSubtitleChip);
  /* U⑥：自动化快照与任务证据变化都会改写按钮可见性 */
  const unsubscribeAutomationActions = store.subscribe("automation", () => updateLectureActions(store.activeLecture));
  const unsubscribeTasksActions = store.subscribe("tasks", () => updateLectureActions(store.activeLecture));
  updateLectureActions(store.activeLecture);
  syncSubtitleChip();
  syncNotUnderstoodButton();
  applySubtitleStyle();
  return () => {
    disposed = true;
    restoreHoldRate(false);
    timelineScrubbing = false;
    clearIdleTimer();
    mediaBuffering = false;
    keyboardFocusInsidePlayer = false;
    volumeHover = false;
    volumeFocus = false;
    volumeClickPin = false;
    volumeOpen = false;
    setChromeVisible(true); /* 清理后确定性可见，不滞留隐藏态 */
    clearOsdTimers();
    hidePlayerOsd();
    clearBufferSpinner();
    dismissResumeBanner();
    handleSubtitleHoldCancel();
    if (saveTimer) window.clearTimeout(saveTimer);
    saveTimer = 0;
    mediaSubId = "";
    unsubscribeActiveLecture();
    unsubscribeAuthContinuity();
    clearContinuityState();
  window.removeEventListener("courselens:logout", handleLogoutClear);
  window.removeEventListener("courselens:page", handlePageChanged);
  window.removeEventListener("courselens:bookmarks-changed", handleBookmarksChanged);
  if (bookmarkSyncTimer) {
    window.clearTimeout(bookmarkSyncTimer);
    bookmarkSyncTimer = 0;
  }
  player.removeEventListener("error", showMediaFailure);
  player.removeEventListener("loadedmetadata", handleLoadedMetadata);
  player.removeEventListener("loadedmetadata", applySavedRate);
  window.removeEventListener("courselens:chapters", handleChaptersEvent);
  player.removeEventListener("playing", clearPlaybackRecovery);
  document.removeEventListener("fullscreenchange", handleFullscreenChange);
  unsubscribeAutomationActions();
  unsubscribeTasksActions();
  $("generate-subtitle").removeEventListener("click", handleGenerateSubtitle);
  $("generate-notes").removeEventListener("click", handleGenerateNotes);
  $("player-task-open-drawer").removeEventListener("click", handleOpenTaskDrawer);
  if (subtitleChip) subtitleChip.removeEventListener("click", handleSubtitleChipClick);
  if (bookmarkButton) bookmarkButton.removeEventListener("click", markNotUnderstood);
  if (insightOpen) insightOpen.removeEventListener("click", handleInsightOpen);
  if (insightErase) insightErase.removeEventListener("click", handleInsightErase);
  resetInsightEraseArm();
  discardInsightEvents();
  stopStudyHeartbeat();
  insightHydrateToken += 1;
  clearInsightHeat();
  if (timelineWrapEl) {
    timelineWrapEl.removeEventListener("pointermove", handleTimelinePointerMove);
    timelineWrapEl.removeEventListener("pointermove", syncTimelineFlagHover);
    timelineWrapEl.removeEventListener("pointerleave", hideTimelineBubble);
    timelineWrapEl.removeEventListener("pointerleave", clearTimelineFlagHover);
  }
  unsubscribeTasksChip();
  if (controlsDeckPresent) {
    $("player-ctrl-play").removeEventListener("click", handlePlayButtonClick);
    $("player-ctrl-mute").removeEventListener("click", handleMuteToggle);
    $("player-ctrl-volume").removeEventListener("input", handleVolumeInput);
    $("player-ctrl-speed").removeEventListener("change", handleSpeedChange);
    $("player-ctrl-subtitle").removeEventListener("click", handleSubtitleButtonClick);
    $("player-ctrl-subtitle").removeEventListener("pointerdown", handleSubtitlePointerDown);
    $("player-ctrl-subtitle").removeEventListener("pointerup", handleSubtitleHoldCancel);
    $("player-ctrl-subtitle").removeEventListener("pointercancel", handleSubtitleHoldCancel);
    $("player-ctrl-subtitle").removeEventListener("pointerleave", handleSubtitleHoldCancel);
    if (subtitleStyleDialog) subtitleStyleDialog.removeEventListener("click", handleSubtitleStyleDialogClick);
    $("player-ctrl-pip").removeEventListener("click", handlePipToggle);
    $("player-ctrl-theatre").removeEventListener("click", handleTheatreToggle);
    $("player-ctrl-fullscreen").removeEventListener("click", handleFullscreenToggle);
    $("player-ctrl-timeline").removeEventListener("input", handleTimelineInput);
    $("player-ctrl-timeline").removeEventListener("change", handleTimelineScrubEnd);
    $("player-ctrl-timeline").removeEventListener("pointerdown", handleTimelineScrubStart);
    $("player-ctrl-timeline").removeEventListener("pointerup", handleTimelineScrubEnd);
    $("player-ctrl-timeline").removeEventListener("pointercancel", handleTimelineScrubEnd);
    $("player-ctrl-volume").removeEventListener("pointerup", handleVolumePointerRelease);
    $("player-ctrl-volume").removeEventListener("keydown", handleSliderSpacekick);
    $("player-ctrl-timeline").removeEventListener("keydown", handleSliderSpacekick);
    hideTimelineBubble();
    window.removeEventListener("keydown", handlePlayerKeydown);
    window.removeEventListener("keyup", handlePlayerKeyup);
    window.removeEventListener("blur", handlePlayerWindowBlur);
    const studyDeskForCleanup = $("study-desk");
    if (studyDeskForCleanup && handleDeskClickFocusRelease) {
      studyDeskForCleanup.removeEventListener("click", handleDeskClickFocusRelease, true);
      handleDeskClickFocusRelease = null;
    }
    shell.removeEventListener("pointerenter", handleShellPointerHover);
    shell.removeEventListener("pointermove", handleShellPointerHover);
    shell.removeEventListener("pointerleave", handleShellPointerExit);
    shell.removeEventListener("pointercancel", handleShellPointerExit);
    shell.removeEventListener("pointerup", handleShellPointerUp);
    shell.removeEventListener("click", handleShellSurfaceClick);
    shell.removeEventListener("dblclick", handleShellSurfaceDoubleClick);
    shell.removeEventListener("mousedown", handleShellSurfaceMouseDown);
    shell.removeEventListener("wheel", handleShellWheel);
    shell.removeEventListener("focusin", handleShellFocusIn);
    shell.removeEventListener("focusout", handleShellFocusOut);
    $("player-volume").removeEventListener("pointerenter", handleVolumeGroupEnter);
    $("player-volume").removeEventListener("pointerleave", handleVolumeGroupLeave);
    $("player-volume").removeEventListener("pointerdown", handleVolumeSurfacePointerDown);
    $("player-volume").removeEventListener("focusin", handleVolumeGroupFocusIn);
    $("player-volume").removeEventListener("focusout", handleVolumeGroupFocusOut);
    $("player-volume").removeEventListener("keydown", handleVolumeGroupEscape);
    document.removeEventListener("pointerdown", handleDocPointerDown, true);
    document.removeEventListener("visibilitychange", handleDocVisibilityChange);
  }
  player.removeEventListener("timeupdate", handleTimeUpdate);
  player.removeEventListener("seeked", publishTranscriptTime);
  player.removeEventListener("seeked", renderSubtitleOverlay);
  player.removeEventListener("seeking", handleSeekingIntent);
  player.removeEventListener("pause", handlePause);
  player.removeEventListener("pause", syncPlayButton);
  player.removeEventListener("play", syncPlayButton);
  player.removeEventListener("play", handlePlayStateChange);
  player.removeEventListener("play", startStudyHeartbeat);
  player.removeEventListener("ended", handleEnded);
  player.removeEventListener("durationchange", syncPlayerTimeline);
  player.removeEventListener("seeking", syncPlayerTimeline);
  player.removeEventListener("volumechange", syncVolumeControls);
  player.removeEventListener("ratechange", handleRateChange);
  player.removeEventListener("waiting", handleBufferingStart);
  player.removeEventListener("playing", handleBufferingEnd);
  player.removeEventListener("canplay", handleBufferingEnd);
  player.removeEventListener("enterpictureinpicture", syncPipButton);
  player.removeEventListener("leavepictureinpicture", syncPipButton);
  setPlayerControlsVisible(false);
  if (subtitleTextTrack) {
    subtitleTextTrack.removeEventListener("cuechange", renderSubtitleOverlay);
    subtitleTextTrack = null;
  }
  stopSubtitleWatcher();
  clearSubtitleOverlay();
  unsubscribeTranscriptTiming();
    playbackKind = "none";
    stopVolumeHold();
    if (volumeAutoCloseTimer) {
      window.clearTimeout(volumeAutoCloseTimer);
      volumeAutoCloseTimer = 0;
    }
    if (controlsFocusReleaseTimer) {
      window.clearTimeout(controlsFocusReleaseTimer);
      controlsFocusReleaseTimer = 0;
    }
    const controlsDeckCleanup = $("player-controls");
    if (controlsDeckCleanup) {
      controlsDeckCleanup.removeEventListener("change", handleDeckInteraction);
      controlsDeckCleanup.removeEventListener("input", handleDeckInteraction);
      controlsDeckCleanup.removeEventListener("pointerdown", handleDeckInteraction);
      controlsDeckCleanup.removeEventListener("pointermove", handleDeckInteraction);
      controlsDeckCleanup.removeEventListener("pointerenter", handleDeckHoverEnter);
      controlsDeckCleanup.removeEventListener("pointerleave", handleDeckHoverLeave);
      controlsDeckCleanup.removeEventListener("pointercancel", handleDeckHoverCancel);
    }
  };
}
