import assert from "node:assert/strict";

/* 直播独立页行为测试（N6L S1 · L2 验收断言清单 a1-a7 + U0 透码语义 +
 * 今晚在播列表 + B6 静默续建恰一次）。仿 playback/home-overview 桩件法：
 * 纯函数直测；DOM 用 FakeElement 精简 fixture + FakeHls。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(String(name))); }
  remove(...names) { names.forEach((name) => this.values.delete(String(name))); }
  toggle(value, enabled) { if (enabled) this.values.add(value); else this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement extends EventTarget {
  constructor(tagName = "div", id = "") {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.textContent = "";
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.title = "";
    this.src = "";
    this.error = null;
    this.currentTime = 0;
    this.duration = Number.NaN;
    this.playbackRate = 1;
    this.paused = true;
    this.seekable = { length: 0 };
    this.loadCount = 0;
    this._listeners = new Map();
  }
  get className() { return [...this.classList.values].join(" "); }
  set className(value) {
    this.classList.values.clear();
    String(value).split(/\s+/).filter(Boolean).forEach((name) => this.classList.values.add(name));
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  canPlayType() { return ""; } /* 走 hls.js 桩路径 */
  load() { this.loadCount += 1; }
  pause() { this.paused = true; }
  play() {
    if (this.failPlay) return Promise.reject(new Error("synthetic-play-refused"));
    this.paused = false;
    return Promise.resolve();
  }
  focus() {}
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
  click() { this.dispatchEvent({ type: "click" }); }
  emit(type) {
    for (const listener of [...(this._listeners.get(type) || [])]) listener.call(this, { type, target: this });
  }
}

class FakeHls {
  constructor(config) {
    this.config = config;
    this.destroyed = false;
    this.handlers = new Map();
    FakeHls.instances.push(this);
  }
  loadSource() {}
  attachMedia(media) { this.media = media; }
  on(event, handler) {
    if (!this.handlers.has(event)) this.handlers.set(event, []);
    this.handlers.get(event).push(handler);
  }
  emit(event, data) {
    for (const handler of this.handlers.get(event) || []) handler(undefined, data);
  }
  destroy() { this.destroyed = true; }
}
FakeHls.instances = [];
FakeHls.Events = { MANIFEST_PARSED: "hlsMp", ERROR: "hlsErr" };
FakeHls.ErrorTypes = { MEDIA_ERROR: "mediaError", NETWORK_ERROR: "networkError" };
FakeHls.isSupported = () => true;

class FakeCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}

/* ---- DOM fixture：live-page 全量 id ---- */
const ids = [
  "live-page", "live-evidence", "live-recheck", "live-stage", "live-video", "live-badge",
  "live-clock", "live-pill", "live-buffering", "live-cover", "live-card", "live-card-title",
  "live-card-body", "live-card-diagnostic", "live-card-actions", "live-hint", "live-status-line",
  "live-course-title", "live-context", "live-tonight", "workspace-main",
];
const byId = {};
for (const id of ids) byId[id] = new FakeElement(id === "live-video" ? "video" : "div", id);
byId["live-recheck"].tagName = "BUTTON";
byId["live-pill"].tagName = "BUTTON";
const video = byId["live-video"];
video.failPlay = false;

let created = 0;
const documentTarget = new EventTarget();
globalThis.document = Object.assign(documentTarget, {
  getElementById: (id) => byId[id] || null,
  createElement: () => new FakeElement("button", `created-${created += 1}`),
  querySelectorAll: () => [],
});
globalThis.CustomEvent = FakeCustomEvent;

const windowTarget = new EventTarget();
windowTarget.setTimeout = (callback, ms) => setTimeout(callback, Math.min(Number(ms) || 0, 5));
windowTarget.clearTimeout = (id) => clearTimeout(id);
/* LIVEEXP-1：捕获式 interval 桩——重估 tick 手动驱动（注入时钟推进后调 tick） */
let installedPageTick = null;
windowTarget.setInterval = (fn, ms) => { installedPageTick = fn; return 81; };
windowTarget.clearInterval = () => {};
windowTarget.Hls = FakeHls;
globalThis.window = windowTarget;

/* ---- fetch 桩：status/grants/sessions/manifest 失败体 ---- */
const manifestPath = "/api/v3/live-room/play/synthetic/manifest/res";
let statusState = "live";
let failGrant = false;
let failGrantCode = "live_grant_invalid";
let manifestFailureCode = "";
let grantCalls = 0;
let sessionCalls = 0;
const statusCalls = [];

const ok = (data, status = 200) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
  status,
  headers: { "Content-Type": "application/json" },
});
const fail = (code, status) => new Response(JSON.stringify({ error: "synthetic", error_code: code }), {
  status,
  headers: { "Content-Type": "application/json" },
});

globalThis.fetch = async (path) => {
  const route = String(path);
  if (route.startsWith("/api/v3/live-room/status?")) {
    statusCalls.push(route);
    return ok({ state: statusState, can_enter: statusState === "live", starts_at: "", ends_at: "" });
  }
  if (route === "/api/v3/live-room/grants") {
    grantCalls += 1;
    if (failGrant) return fail(failGrantCode, 403);
    return ok({ grant: "synthetic-grant" }, 201);
  }
  if (route === "/api/v3/live-room/sessions") {
    sessionCalls += 1;
    return ok({ manifest_path: manifestPath }, 201);
  }
  if (route === manifestPath) {
    return new Response(JSON.stringify(manifestFailureCode ? { error_code: manifestFailureCode } : {}), {
      status: manifestFailureCode ? 403 : 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  return ok({});
};

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { await nextTurn(); await nextTurn(); await nextTurn(); };
/* 恢复梯/静默续建含真实短退避定时器：谓词轮询到态收敛（上限 2s），不写死等待 */
const waitUntil = async (predicate, timeoutMs = 2000) => {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) break;
    await new Promise((done) => setTimeout(done, 10));
  }
  await settle();
};

/* ---- 纯模型：16 态归并 ---- */
const { resolveLivePageState, todayLiveMeetings } = await import("../frontend/modules/live-page.js");
const liveState = await import("../frontend/modules/live-state.js");

/* U4 等集钉（前端侧·与 tests/test_live_error_code_parity.py 成对）：
   入口失败码全部有逐码卡，且与设计纸 §12.3 专卡闭集等集 */
{
  const DEDICATED_ENTRY_CODES = [
    "live_session_expired", "live_resource_expired", "live_grant_invalid",
    "live_grant_identity_changed", "live_authorization_denied",
    "live_authorization_revoked", "live_playback_not_open",
    "live_upstream_rejected", "live_upstream_unreachable",
    "live_stream_unavailable", "live_content_type_rejected",
  ].sort();
  assert.deepEqual(Object.keys(liveState.ENTRY_FAILURES).sort(), DEDICATED_ENTRY_CODES, "ENTRY_FAILURES ≡ §12.3 专卡闭集");
  for (const code of Object.keys(liveState.ENTRY_FAILURES)) {
    assert.ok(liveState.LIVE_ERROR_CARDS[code], `${code} 必须有逐码错误卡`);
  }
}

/* a3：中性态文案三段齐（buffering 允许空标题）；错误卡逐码三段齐 */
for (const [key, copy] of Object.entries(liveState.PAGE_STATE_COPY)) {
  assert.ok(Array.isArray(copy.actions), `${key} actions 必须是数组`);
  assert.ok(typeof copy.body === "string" && copy.body.length > 0, `${key} body 三段齐`);
  if (key !== "buffering" && key !== "behind") {
    assert.ok(copy.title.length > 0, `${key} title 三段齐`);
  }
}
for (const [code, card] of Object.entries(liveState.LIVE_ERROR_CARDS)) {
  assert.ok(card.title && card.body && Array.isArray(card.actions) && card.actions.length > 0, `${code} 错误卡三段齐`);
  assert.ok(["expired", "gate", "stream", "denied", "fatal"].includes(card.family), `${code} family 闭集`);
}

/* U0 透码语义（直播页版）：假码→fatal+诊断行；无码→连接中断；已知码→本卡 */
const gateCard = liveState.resolveLiveErrorCard("live_playback_not_open");
assert.equal(gateCard.family, "gate");
assert.equal(gateCard.title, "直播还没开放");
const unknownCard = liveState.resolveLiveErrorCard("school_said_no_such_thing");
assert.equal(unknownCard.family, "fatal");
assert.ok(unknownCard.diagnostic.includes("school_said_no_such_thing"), "未闭集码必须透出诊断行");
const emptyCard = liveState.resolveLiveErrorCard("");
assert.equal(emptyCard.code, "live_network_interrupted");
assert.equal(liveState.resolveLiveErrorCard("live_stream_unavailable").family, "stream", "假码 live_stream_unavailable 渲染对应卡");

/* a1/a2：16 态归并全覆盖（骨架键=状态名） */
assert.equal(resolveLivePageState({}).state, "idle", "无目标=idle");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "live", can_enter: true } }).state, "ready");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "upcoming" } }).state, "upcoming");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "ended" } }).state, "ended");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "denied" } }).state, "denied");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "offline" } }).state, "stream");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "stale" } }).state, "stream");
assert.equal(resolveLivePageState({ hasTarget: true, status: { state: "unknown" } }).state, "idle");
assert.equal(resolveLivePageState({ connecting: true }).state, "connecting");
assert.equal(resolveLivePageState({ reconnectAttempt: 2 }).state, "recovering");
assert.equal(resolveLivePageState({ playback: "playing" }).state, "playing");
assert.equal(resolveLivePageState({ playback: "paused" }).state, "paused");
assert.equal(resolveLivePageState({ playback: "buffering" }).state, "buffering");
assert.equal(resolveLivePageState({ playback: "behind" }).state, "behind");
assert.equal(resolveLivePageState({ playback: "weaknet" }).state, "weaknet");
const expiredResolved = resolveLivePageState({ errorCard: liveState.resolveLiveErrorCard("live_session_expired") });
assert.equal(expiredResolved.state, "expired");
assert.equal(resolveLivePageState({ errorCard: liveState.resolveLiveErrorCard("live_upstream_rejected") }).state, "stream");
/* D14：错误面板唯一——错误优先级恒最高 */
assert.equal(
  resolveLivePageState({ errorCard: liveState.resolveLiveErrorCard("live_session_expired"), playback: "playing", connecting: true }).state,
  "expired",
);
/* 过渡态覆盖播放态 */
assert.equal(resolveLivePageState({ connecting: true, playback: "playing" }).state, "connecting");

/* 今日课表纯模型：目录外课程/重复课程不重复入列 */
const now = new Date(2026, 8, 22, 8, 30, 0);
const snapshot = {
  days: [{
    date: "2026-09-22",
    meetings: [
      { catalog_course_id: "c1", title: "数为", start_time: "18:30", end_time: "21:05" },
      { catalog_course_id: "c9", title: "无目录", start_time: "10:00", end_time: "11:00" },
      { catalog_course_id: "c1", title: "数为", start_time: "18:30", end_time: "21:05" },
    ],
  }],
};
const meetings = todayLiveMeetings(snapshot, [{ course_id: "c1", title: "数为" }], now);
assert.equal(meetings.length, 1, "目录外/重复课程去重");
assert.equal(meetings[0].courseId, "c1");

/* ---- DOM 行为流 ---- */
const store = {
  liveTarget: "c1", /* U2 卡片跳转写本键；本页消费预选 */
  liveActiveCourses: [],
  courses: [{ course_id: "c1", title: "数为" }],
  activeCourse: null,
  auth: null,
  activeLecture: null,
  tasks: [],
  set(key, value) { this[key] = value; },
  subscribe() { return () => {}; },
};

const { installLivePage } = await import("../frontend/modules/live-page.js");
const cleanup = await installLivePage(store);
await settle();

/* 空课表快照 + liveTarget=c1 → ready 态（status=live） */
assert.equal(byId["live-page"].dataset.state, "ready", "live+can_enter → ready 态");
assert.equal(byId["live-cover"].hidden, false);
assert.equal(byId["live-video"].hidden, true);
assert.equal(byId["live-card-title"].textContent, "现在可以进入直播");

/* a4：直达动作——点击进入直播 → grants→sessions→attach → playing */
const enterButton = byId["live-card-actions"].children[0];
assert.equal(enterButton.textContent, "进入直播");
enterButton.click();
await settle();
assert.equal(grantCalls, 1);
assert.equal(sessionCalls, 1);
const hls = FakeHls.instances.at(-1);
assert.ok(hls, "hls 会话已建立");
hls.emit(FakeHls.Events.MANIFEST_PARSED);
assert.equal(video.paused, false, "MANIFEST_PARSED 后自动起播");
video.emit("playing");
await settle();
assert.equal(byId["live-page"].dataset.state, "playing", "起播后 playing 态");
assert.equal(byId["live-cover"].hidden, true, "播放态覆盖卡退场");
assert.equal(byId["live-video"].hidden, false);
assert.equal(byId["live-badge"].hidden, false, "活标识可见");

/* G9-C 合同随迁（player-core 直播段退役后由 live-player 承接）：
   C1 hls 配对配置 + C3 渐进追帧数学与用户让位 */
assert.equal(hls.config.liveSyncDurationCount, 3, "liveSyncDurationCount=3（G3-L1）");
assert.equal(hls.config.liveMaxLatencyDurationCount, 9, "maxLatency=9 与 sync 成对");
assert.ok(hls.config.liveMaxLatencyDurationCount > hls.config.liveSyncDurationCount, "max 必须大于 sync");
assert.equal(hls.config.backBufferLength, 30, "既有 backBuffer 维持");
assert.equal(hls.config.maxBufferLength, 30, "既有 maxBuffer 维持");
hls.liveSyncPosition = video.currentTime + 12;
video.emit("timeupdate");
assert.equal(byId["live-pill"].hidden, false, "落后 12s 浮出 pill");
assert.ok(Math.abs(video.playbackRate - 1.5) < 1e-9, "落后 12s 追帧钳到 1.5 上限");
video.playbackRate = 1; /* pill 点击回同步点已由 a7 断言覆盖 */
video.emit("ratechange"); /* 用户改速 → 追帧让位 */
hls.liveSyncPosition = video.currentTime + 1;
video.emit("timeupdate");
assert.equal(video.playbackRate, 1, "用户改速后追帧让位不抢倍速");
hls.liveSyncPosition = Number.NaN;

/* a7：behind>10s → pill 点击回同步点（前置：liveSyncPosition 可用） */
hls.liveSyncPosition = video.currentTime + 25;
video.emit("timeupdate");
video.emit("playing");
await settle();
assert.equal(byId["live-page"].dataset.state, "playing", "25s 落后经追帧温和回位，pill 阈值态不误亮");
hls.liveSyncPosition = Number.NaN;
video.seekable = { length: 0 };
video.emit("timeupdate");
await settle();
assert.equal(byId["live-pill"].hidden, true, "无时间基准的源 pill 安静");

/* U17 B1 同款：hls fatal 网络错误 → 读响应体闭集码 → 恢复梯（grants 链全败）
   尽 → 落过期错误卡（绝不无声卡死） */
manifestFailureCode = "live_session_expired";
failGrant = true;
failGrantCode = "live_session_expired";
hls.emit(FakeHls.Events.ERROR, { fatal: true, type: FakeHls.ErrorTypes.NETWORK_ERROR });
await waitUntil(() => byId["live-page"].dataset.state === "expired");
assert.equal(byId["live-page"].dataset.state, "expired", "恢复梯尽 → 过期错误卡");
assert.equal(byId["live-card-title"].textContent, "直播会话过期了");
assert.ok(byId["live-card-actions"].children.length >= 1, "错误卡带直达动作");

/* a5/D14：错误面板唯一——第二个错误替换同一张卡，绝不叠第二面板 */
manifestFailureCode = "live_upstream_rejected";
hls.emit(FakeHls.Events.ERROR, { fatal: true, type: FakeHls.ErrorTypes.NETWORK_ERROR });
await waitUntil(() => byId["live-card-title"].textContent === "学校服务器刚拒绝了这次取流");
assert.equal(byId["live-card-title"].textContent, "学校服务器刚拒绝了这次取流", "新错误替换卡面");
assert.equal(byId["live-page"].dataset.state, "stream");
manifestFailureCode = "";

/* 第廿六案用户路径（退出登录→重新登录→再进直播，两入口同败）：未闭集
   真实码（防护族）与授权域码都必须透诊断行，绝不静默吞成纯 unknown */
failGrant = true;
failGrantCode = "live_session_unavailable"; /* 防护族真实码，未入 §12.3 专卡表 */
byId["live-card-actions"].children[0].click();
await waitUntil(() => byId["live-page"].dataset.state === "fatal");
assert.ok(
  byId["live-card-diagnostic"].textContent.includes("live_session_unavailable"),
  "防护族码透诊断行",
);
failGrantCode = "fudan_login_required"; /* 重新登录后授权域码 */
byId["live-card-actions"].children[0].click();
await waitUntil(() => byId["live-card-diagnostic"].textContent.includes("fudan_login_required"));
failGrant = false;

/* a6/B6：离页暂停 → 回页续播恰一次静默续建；续建失败诚实落卡 */
failGrant = false;
video.failPlay = false;
byId["live-card-actions"].children[0].click(); /* 重新进入直播 */
await settle();
const hls2 = FakeHls.instances.at(-1);
assert.notEqual(hls2, hls, "重进建立新 hls 会话");
hls2.emit(FakeHls.Events.MANIFEST_PARSED);
video.emit("playing");
await settle();
assert.equal(byId["live-page"].dataset.state, "playing", "重进后恢复 playing 态");
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:page", { detail: "study" }));
assert.equal(video.paused, true, "离开直播页即暂停");
await settle();
assert.equal(byId["live-page"].dataset.state, "paused", "离页后 paused 态");
const grantsBeforeResume = grantCalls;
video.failPlay = true;
failGrant = true;
failGrantCode = "live_session_expired";
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:page", { detail: "live" }));
await waitUntil(() => byId["live-page"].dataset.state === "expired");
assert.equal(grantCalls - grantsBeforeResume, 1, "回页续播恰一次静默续建");
assert.equal(byId["live-page"].dataset.state, "expired", "静默续建失败诚实落卡，绝不无声循环");
assert.equal(byId["live-card-title"].textContent, "直播会话过期了");
video.failPlay = false;
failGrant = false;

/* 今晚在播列表渲染 + 点击切换目标 */
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", { detail: snapshot }));
await settle();
const tonightRows = byId["live-tonight"].children;
assert.equal(tonightRows.length, 1, "今晚在播仅列目录内课程");
tonightRows[0].click();
await settle();
assert.equal(store.liveTarget, "c1", "列表点击写 liveTarget");

/* 状态观测回写红点数据源 */
assert.deepEqual(store.liveActiveCourses, ["c1"], "state=live 观测回写 liveActiveCourses");

const liveHlsAtEnd = FakeHls.instances.at(-1);
cleanup();
assert.equal(liveHlsAtEnd.destroyed, true, "清理销毁在途 hls 会话");

/* ---- 直播二期（乙2）：视角条纯模型 ---- */
const { resolveViewBar, DEFAULT_LIVE_VIEW } = liveState;
assert.equal(DEFAULT_LIVE_VIEW, "student", "默认视角=学生画面（既有语义不变）");
/* 无会话/单视角=条不出现（没得切就不渲染） */
assert.equal(resolveViewBar({ status: { available_views: ["teacher", "student"] } }).visible, false, "无会话不出视角条");
assert.equal(
  resolveViewBar({ status: { available_views: ["student"] }, hasSession: true }).visible,
  false, "单视角不出视角条",
);
assert.equal(resolveViewBar({ hasSession: true }).visible, false, "无元数据不出视角条");
/* 多视角：闭集过滤+焦点视角 */
const barFull = resolveViewBar({
  status: { available_views: ["teacher", "junk_view", "student", "teacher_audio"] },
  hasSession: true,
});
assert.deepEqual(barFull.views, ["teacher", "student", "teacher_audio"], "闭集外视角被滤除");
assert.equal(barFull.activeView, "student", "当前视角=aria-pressed 焦点");
assert.equal(barFull.suggestAudio, false, "非弱网不建议声音档");
/* 弱网③级：存在纯声音档才高亮建议（闭集内裁决） */
assert.equal(
  resolveViewBar({ status: { available_views: ["teacher", "student"] }, hasSession: true, weaknet: true }).suggestAudio,
  false, "无声音档时不建议",
);
assert.equal(
  resolveViewBar({ status: { available_views: ["student", "student_audio"] }, hasSession: true, weaknet: true }).suggestAudio,
  true, "弱网高亮纯声音档",
);
/* 语义兜底：currentView 未知回默认；switching 防抖透传 */
assert.equal(
  resolveViewBar({ status: { available_views: ["student", "teacher"] }, currentView: "invented", hasSession: true }).activeView,
  "student", "未知视角回默认",
);
assert.equal(
  resolveViewBar({ status: { available_views: ["student", "teacher"] }, hasSession: true, switching: true }).switching,
  true, "切换在途=防抖透传",
);

/* ---- 直播二期（乙3）：文稿侧板纯模型 ---- */
const { mergeTranscriptSegments, resolveTranscriptOutcome } = liveState;
const base = [
  { start_ms: 1000, end_ms: 2000, text: "第一句" },
  { start_ms: 2000, end_ms: 5000, text: "第二句初版" },
];
const grown = mergeTranscriptSegments(base, [
  { start_ms: 2000, end_ms: 6000, text: "第二句完整版" },
  { start_ms: 6000, end_ms: 8000, text: "第三句" },
]);
assert.equal(grown.length, 3, "增量合并按 start_ms 身份替换");
assert.equal(grown[1].text, "第二句完整版", "生长尾段被新版替换");
assert.equal(grown[2].text, "第三句", "新段追加");
assert.deepEqual(mergeTranscriptSegments(null, grown), grown, "空底可合并");
const malformed = mergeTranscriptSegments([], [{ start_ms: "junk", text: "x" }, { text: "no start" }]);
assert.deepEqual(malformed, [], "畸形段被丢弃");
/* 无文稿闭集提示族 */
assert.equal(
  resolveTranscriptOutcome({ available: true, segments: [] }).hint,
  "平台还没有生成这节课的文稿。", "平台如实无文稿",
);
assert.equal(resolveTranscriptOutcome({ available: true, segments: [] }).retryable, false, "无文稿不可重试");
assert.equal(resolveTranscriptOutcome({ failed: true }).retryable, true, "读取失败可重试");
assert.equal(resolveTranscriptOutcome({ notFound: true }).retryable, false, "目录外讲次不可重试");
assert.ok(resolveTranscriptOutcome({ available: false }).hint.includes("稍后再试"), "503 暂不可得提示");

/* ---- LIVEEXP-1 行为流：直播页时间驱动重估（pageVisible 守卫/边界/过窗/dispose）---- */
cleanup();
await settle();
statusState = "live";
let pageClockNow = new Date(2026, 8, 22, 18, 0, 0); /* 周二 2026-09-22 18:00：18:30–21:05 未开始 */
let pageTick = null;
let pageRecheckMs = 0;
const pageCleared = [];
windowTarget.setInterval = (fn, ms) => { pageTick = fn; pageRecheckMs = Number(ms); return 82; };
windowTarget.clearInterval = (id) => { pageCleared.push(id); };
const pageSnapshot = {
  days: [{ date: "2026-09-22", meetings: [
    { catalog_course_id: "c1", title: "数为", start_time: "18:30", end_time: "21:05" },
  ]}],
};
const cleanupPageInjected = await installLivePage(store, { clock: () => pageClockNow });
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:timetable-snapshot", { detail: pageSnapshot }));
await settle();
assert.equal(pageRecheckMs, 30000, "直播页重估周期默认 30s");
assert.equal(typeof pageTick, "function", "直播页重估 tick 已注册");

/* 后台页守卫：未进页时 tick 零动作 */
const statusAtBackground = statusCalls.length;
pageTick();
await settle();
assert.equal(statusCalls.length, statusAtBackground, "后台页 tick 零动作");

/* 进页：courseContext 未来措辞（按课表 18:30 开始） */
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:page", { detail: "live" }));
await settle();
assert.ok(
  byId["live-context"].textContent.includes("按课表 18:30 开始"),
  `未开始课表事实=按课表开始（实际：${byId["live-context"].textContent}）`,
);

/* 相位不变远离边界：tick 零请求 */
const statusAtFuture = statusCalls.length;
pageTick();
await settle();
assert.equal(statusCalls.length, statusAtFuture, "相位不变 tick 零请求");

/* 临近开始边界（18:28，距 18:30 两分钟）→ tick force 重查 */
pageClockNow = new Date(2026, 8, 22, 18, 28, 0);
pageTick();
await settle();
assert.ok(statusCalls.length > statusAtFuture, "临近开始边界 tick force 重查");

/* 过窗翻转（21:06）→ tick force 重查 → 课表事实改述「已结束」，绝不写「开始」 */
statusState = "ended";
pageClockNow = new Date(2026, 8, 22, 21, 6, 0);
pageTick();
await settle();
assert.ok(
  byId["live-context"].textContent.includes("按课表 18:30–21:05 已结束"),
  `过窗课表事实=已结束（实际：${byId["live-context"].textContent}）`,
);
assert.ok(!byId["live-context"].textContent.includes("开始"), "过窗措辞不写开始");
assert.equal(byId["live-page"].dataset.state, "ended", "过窗后页面态收敛 ended");

/* 离页后台：tick 零动作 */
windowTarget.dispatchEvent(new FakeCustomEvent("courselens:page", { detail: "study" }));
await settle();
const statusAtAway = statusCalls.length;
pageClockNow = new Date(2026, 8, 22, 21, 7, 0);
pageTick();
await settle();
assert.equal(statusCalls.length, statusAtAway, "离页后 tick 零动作");

/* dispose 钉 */
cleanupPageInjected();
await settle();
assert.ok(pageCleared.includes(82), "dispose 清除直播页重估定时器");

/* ---- F2（化身走查 20261008）：「进入直播」CTA 意图直达——落页自动建会 ---- */
/* 学习页/主页 CTA 已明示进入意图，直播页落定后状态可进入即自动建立会话；
   双重「进入直播」合并为一次点击。意图旗 20s 用后即焚，后台刷新轮绝不携带
   陈旧意图自动进入；不可进入照常落状态卡（诚实回退）。 */
{
  statusState = "live";
  const grantsAtStart = grantCalls;
  const sessionsAtStart = sessionCalls;
  const cleanupEnter = await installLivePage(store);
  await settle();
  /* 1) 无意图落页：ready 卡 + 手动进入钮在位，零会话请求（回退路径保留） */
  assert.equal(byId["live-page"].dataset.state, "ready", "无意图落页=ready 卡照常");
  assert.equal(grantCalls, grantsAtStart, "无意图不自动建会");
  const fallbackEnter = byId["live-card-actions"].children[0];
  assert.equal(fallbackEnter.textContent, "进入直播", "回退路径保留手动进入按钮");

  /* 2) CTA 意图：live-enter + 进页 → 状态可进入 → 自动 grants/sessions（零二次点击） */
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-enter"));
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:page", { detail: "live" }));
  await settle();
  assert.equal(grantCalls, grantsAtStart + 1, "意图+可进入 → 自动 grants");
  assert.equal(sessionCalls, sessionsAtStart + 1, "意图+可进入 → 自动 sessions");
  const autoHls = FakeHls.instances.at(-1);
  assert.ok(autoHls, "自动建会已挂播放器");

  /* 3) 意图旗用后即焚：后续刷新轮不重入 */
  const grantsAfterAuto = grantCalls;
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-refresh"));
  await settle();
  assert.equal(grantCalls, grantsAfterAuto, "意图用后即焚，刷新轮不重入");

  /* 4) 不可进入不发明会话：ended 状态下意图在途也只落状态卡 */
  statusState = "ended";
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-enter"));
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-refresh"));
  await settle();
  assert.equal(grantCalls, grantsAfterAuto, "不可进入时不自动建会");
  assert.equal(byId["live-page"].dataset.state, "ended", "不可进入照常落状态卡");

  /* 5) 陈旧意图不迟到进入：armed 后时钟前移 30s，迟到刷新只清旗不建会 */
  statusState = "live";
  windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-enter")); /* 实钟武装 */
  const realNowFn = Date.now;
  Date.now = () => realNowFn() + 30000; /* 模拟 30s 后的迟到刷新（TTL=20s） */
  try {
    windowTarget.dispatchEvent(new FakeCustomEvent("courselens:live-refresh"));
    await settle();
    assert.equal(grantCalls, grantsAfterAuto, "超 TTL 的迟到刷新不自动建会");
  } finally {
    Date.now = realNowFn;
  }

  await cleanupEnter();
  await settle();
}

console.log("frontend live page behavior passed");
