import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) { names.forEach((name) => this.values.add(String(name))); }

  remove(...names) { names.forEach((name) => this.values.delete(String(name))); }

  toggle(value, enabled) {
    if (enabled) this.values.add(value);
    else this.values.delete(value);
  }
}

class FakeElement extends EventTarget {
  #currentTime = 0;

  constructor(id = "") {
    super();
    this.id = id;
    this.tagName = "";
    this.parent = null;
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.src = "";
    this.error = null;
    this.duration = 0;
    this.playbackRate = 1;
    this.loadCount = 0;
    this.style = new FakeStyle();
    /* 最小 matches：与真实浏览器对齐，:focus-visible 只在键盘来源聚焦时为真。
       测试用 focusVisible=true 伪造键盘来源焦点（指针点击聚焦保持 false）。 */
    this.focusVisible = false;
    this.controls = true;
    this.paused = true;
    this.muted = false;
    this.volume = 1;
    this.max = "";
    this.value = "";
    this.seekable = { length: 0 };
    this.open = false;
  }

  /* 补充F：最小焦点语义——document.activeElement 跟踪 + focus/blur 派发
     focusin/focusout（音量面焦点钉与归还断言依赖） */
  focus() {
    globalThis.document.activeElement = this;
  }
  blur() {
    if (globalThis.document.activeElement === this) globalThis.document.activeElement = null;
  }
  contains(node) {
    let cursor = node;
    while (cursor) {
      if (cursor === this) return true;
      cursor = cursor.parent;
    }
    return false;
  }

  /* className 与 classList 双向同步（真实 DOM 语义；U⑤ 菜单树查询依赖） */
  get className() { return [...this.classList.values].join(" "); }
  set className(value) {
    this.classList.values.clear();
    String(value).split(/\s+/).filter(Boolean).forEach((name) => this.classList.values.add(name));
  }

  /* 最小 closest：支持 #id / .class / 标签名 及逗号并集；沿 parent 链向上。
     播放器快捷键归属（.player-stage-shell）与让位选择器（input/button…）
     都靠它判定，与真实浏览器语义一致。 */
  closest(selector) {
    const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
    let node = this;
    while (node) {
      for (const token of tokens) {
        /* FOCUS-DRIFT-1：[role='x'] 属性令牌支持——焦点守卫细化后让位选择器
           含 [role='menu'/'listbox'/'tab'/'option']，closest 归属判定必须能
           匹配角色属性（与真实浏览器语义一致）。 */
        const roleMatch = /^\[role='([^']+)'\]$/.exec(token);
        const matched = roleMatch
          ? node.attributes?.get("role") === roleMatch[1]
          : token.startsWith("#")
            ? node.id === token.slice(1)
            : token.startsWith(".")
              ? Boolean(node.classList && node.classList.values && node.classList.values.has(token.slice(1)))
              : String(node.tagName || "").toLowerCase() === token.toLowerCase();
        if (matched) return node;
      }
      node = node.parent;
    }
    return null;
  }

  /* 最小树查询（U⑤ 标记悬停菜单断言用）：支持 #id/.class/标签名，递归子树 */
  _matchesToken(token) {
    const roleMatch = /^\[role='([^']+)'\]$/.exec(token);
    if (roleMatch) return this.attributes?.get("role") === roleMatch[1];
    if (token.startsWith("#")) return this.id === token.slice(1);
    if (token.startsWith(".")) return Boolean(this.classList && this.classList.values && this.classList.values.has(token.slice(1)));
    return String(this.tagName || "").toLowerCase() === token.toLowerCase();
  }

  querySelector(selector) {
    for (const child of this.children) {
      if (child._matchesToken?.(selector)) return child;
      const hit = typeof child.querySelector === "function" ? child.querySelector(selector) : null;
      if (hit) return hit;
    }
    return null;
  }

  querySelectorAll(selector) {
    const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
    const hits = [];
    for (const child of this.children) {
      if (tokens.some((token) => child._matchesToken?.(token))) hits.push(child);
      if (typeof child.querySelectorAll === "function") hits.push(...child.querySelectorAll(selector));
    }
    return hits;
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
    if (name === "src") this.src = "";
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  showModal() {
    this.open = true;
  }

  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  }

  replaceChildren(...nodes) {
    this.children = [...nodes];
  }

  remove() {
    this.hidden = true;
  }

  click() {
    if (!this.disabled) this.dispatchEvent(new Event("click"));
  }

  /* 与真实浏览器对齐：写入 currentTime 即运行 seek 算法 → 派发 seeking。
     旧桩是纯属性，seek 意图捕获路径整体不可见（W1 结构性盲区根源）。 */
  get currentTime() {
    return this.#currentTime;
  }

  set currentTime(value) {
    this.#currentTime = value;
    this.dispatchEvent(new Event("seeking"));
  }

  /* 引擎侧位置写入（播放推进/错误回卷）：不派发 seeking——真实引擎中只有
     seek 才是 seeking 事件源，播放推进只伴随 timeupdate，错误回卷无事件。 */
  writePlaybackTime(value) {
    this.#currentTime = value;
  }

  load() {
    this.loadCount += 1;
  }

  matches(selector) {
    if (selector === ":focus-visible") return this.focusVisible === true;
    /* FOCUS-DRIFT-1：同形令牌自匹配（#id/.class/标签名/[role='x']）——焦点
       政策归还判定 activeElement.matches("button, a") 与真实浏览器语义一致。 */
    const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
    return tokens.some((token) => this._matchesToken(token));
  }

  play() {
    this.paused = false;
    return Promise.resolve();
  }

  pause() {
    this.paused = true;
  }

  canPlayType() {
    return "";
  }

  /* N5PR-P4：时间气泡几何读取桩（真实引擎返回布局矩形；固定 1000px 宽
     便于 ratio 换算断言）。 */
  getBoundingClientRect() {
    return { left: 0, top: 0, right: 1000, bottom: 32, width: 1000, height: 32 };
  }
}

class FakeCustomEvent extends Event {
  constructor(type, options = {}) {
    super(type);
    this.detail = options.detail;
  }
}

/* 最小 CSSStyleDeclaration：时间轴几何只写自定义属性（--play-ratio），
   断言经 getPropertyValue 读取（与真实 computed style 语义一致）。 */
class FakeStyle {
  constructor() {
    this.properties = new Map();
  }

  setProperty(name, value) {
    this.properties.set(String(name), String(value));
  }

  getPropertyValue(name) {
    return this.properties.has(String(name)) ? this.properties.get(String(name)) : "";
  }
}

// 与真实 Chromium 对齐：TextTrack 模式变化不派发任何事件（无 modechange），
// 模式感知只能靠 player-core 的 rAF 看护循环；cuechange 仍随 cue 变化派发。
class FakeTextTrack extends EventTarget {
  constructor() {
    super();
    this.cues = [];
    this.modeValue = "showing";
  }

  get mode() {
    return this.modeValue;
  }

  set mode(value) {
    this.modeValue = value;
  }

  addCue(cue) {
    this.cues.push(cue);
    this.dispatchEvent(new Event("cuechange"));
  }
}

class FakeHls {
  static Events = Object.freeze({ MANIFEST_PARSED: "manifest-parsed", ERROR: "error" });
  static ErrorTypes = Object.freeze({ MEDIA_ERROR: "media-error", NETWORK_ERROR: "network-error" });
  static instances = [];
  static isSupported() {
    return true;
  }

  constructor() {
    this.handlers = new Map();
    this.destroyed = false;
    /* G9-C1：合成断言读取实例化配置（liveSync/maxLatency 配对） */
    this.config = arguments[0] || {};
    /* G9-C2/C3：behind 度量基准=liveSyncPosition；无时间基准的源保持 NaN */
    this.liveSyncPosition = Number.NaN;
    FakeHls.instances.push(this);
  }

  loadSource(path) {
    this.path = path;
  }

  attachMedia(media) {
    this.media = media;
  }

  on(name, callback) {
    this.handlers.set(name, callback);
  }

  emit(name, data) {
    if (this.destroyed) return;
    this.handlers.get(name)?.(name, data);
  }

  destroy() {
    this.destroyed = true;
  }
}

function createPage() {
  const ids = [
    "player-stage", "player-subtitle-track", "player-placeholder",
    "player-stage-shell", "player-stage-fit", "player-subtitle-overlay",
    "player-controls", "player-ctrl-play", "player-ctrl-elapsed",
    "player-ctrl-timeline", "player-timeline", "player-timeline-fill", "player-ctrl-duration",
    "player-timeline-buffered", "player-timeline-bubble", "player-stage-spinner",
    "player-osd-center", "player-osd-hint",
    "player-ctrl-mute", "player-volume", "player-volume-surface",
    "player-ctrl-volume", "player-ctrl-speed",
    "player-ctrl-subtitle", "player-ctrl-pip", "player-ctrl-theatre",
    "player-subtitle-chip", "player-ctrl-bookmark",
    "player-timeline-flags", "player-timeline-labels", "player-timeline-heat",
    "player-timeline-assess",
    "player-insight-open", "player-insight-dialog", "insight-erase",
    "player-ctrl-fullscreen", "player-ctrl-status",
    "study-desk",
    "generate-subtitle", "generate-notes",
    "player-action-hint", "player-task-state", "player-task-text", "player-task-open-drawer",
    "player-title", "player-evidence",
    "player-recovery", "player-recovery-title", "player-recovery-impact", "player-recovery-actions",
    "player-keys-dialog",
    "player-subtitle-style-dialog",
    "live-room-row", "live-room-label", "live-room-capability", "live-room-time", "live-room-reason",
    "enter-live-room", "live-room-recheck",
    "toast-region",
  ];
  const byId = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  // 与真实 DOM 一致的最小父子链：closest 归属判定依赖它（类名也要对齐）
  byId["player-stage-shell"].classList.values.add("player-stage-shell");
  byId["player-stage-fit"].classList.values.add("player-stage-fit");
  byId["player-stage-fit"].parent = byId["player-stage-shell"];
  byId["player-controls"].classList.values.add("player-controls");
  byId["player-volume"].classList.values.add("player-volume");
  byId["player-stage"].tagName = "video";
  byId["player-stage"].parent = byId["player-stage-fit"];
  /* FOCUS-DRIFT-1：与真实 DOM 对齐——stage 链挂在 study-desk 内（学习桌全域
     指针焦点归还的 contains 归属判定依赖这条父子链）。 */
  byId["player-stage-shell"].parent = byId["study-desk"];
  byId["player-keys-dialog"].tagName = "dialog";
  byId["player-subtitle-style-dialog"].tagName = "dialog";
  byId["player-insight-dialog"].tagName = "dialog";
  byId["player-subtitle-track"].parent = byId["player-stage"];
  byId["player-subtitle-overlay"].parent = byId["player-stage-fit"];
  byId["player-controls"].parent = byId["player-stage-fit"];
  byId["player-ctrl-status"].parent = byId["player-stage-shell"];
  byId["player-ctrl-play"].tagName = "button";
  byId["player-ctrl-play"].parent = byId["player-controls"];
  byId["player-ctrl-elapsed"].parent = byId["player-controls"];
  byId["player-ctrl-duration"].parent = byId["player-controls"];
  byId["player-ctrl-timeline"].tagName = "input";
  /* N5PR-P4：与真实 DOM 一致的时间轴容器链（buffered/fill/input/bubble 都在
     .player-timeline 内，指针事件从 input 冒泡到容器） */
  byId["player-timeline"].classList.values.add("player-timeline");
  byId["player-timeline"].parent = byId["player-controls"];
  byId["player-ctrl-timeline"].parent = byId["player-timeline"];
  byId["player-timeline-fill"].parent = byId["player-timeline"];
  byId["player-timeline-buffered"].parent = byId["player-timeline"];
  byId["player-timeline-bubble"].parent = byId["player-timeline"];
  byId["player-stage-spinner"].parent = byId["player-stage-fit"];
  /* N5PR-P3：OSD 两节点挂在 stage-fit 内、controls 之后（真实 DOM 顺序） */
  byId["player-osd-center"].parent = byId["player-stage-fit"];
  byId["player-osd-hint"].parent = byId["player-stage-fit"];
  byId["player-ctrl-mute"].tagName = "button";
  byId["player-ctrl-mute"].parent = byId["player-volume"];
  byId["player-volume"].parent = byId["player-controls"];
  byId["player-ctrl-volume"].tagName = "input";
  byId["player-ctrl-volume"].parent = byId["player-volume-surface"];
  byId["player-volume-surface"].parent = byId["player-volume"];
  byId["player-ctrl-speed"].tagName = "select";
  byId["player-ctrl-speed"].parent = byId["player-controls"];
  for (const control of ["player-ctrl-subtitle", "player-ctrl-pip", "player-ctrl-theatre", "player-ctrl-fullscreen"]) {
    byId[control].tagName = "button";
    byId[control].parent = byId["player-controls"];
  }
  byId["player-subtitle-chip"].tagName = "button";
  byId["player-subtitle-chip"].parent = byId["player-controls"];
  byId["player-ctrl-bookmark"].tagName = "button";
  byId["player-ctrl-bookmark"].parent = byId["player-controls"];
  byId["player-timeline-flags"].parent = byId["player-timeline"];
  byId["player-timeline-labels"].parent = byId["player-timeline"];
  byId["player-timeline-heat"].parent = byId["player-timeline"];
  byId["player-timeline-assess"].parent = byId["player-timeline"];
  byId["player-timeline-heat"].parent = byId["player-timeline"];
  const elements = {
    ...byId,
    playerStage: byId["player-stage"],
    playerSubtitleTrack: byId["player-subtitle-track"],
    playerStageShell: byId["player-stage-shell"],
    playerStageFit: byId["player-stage-fit"],
    playerSubtitleOverlay: byId["player-subtitle-overlay"],
    playerControls: byId["player-controls"],
    playerCtrlPlay: byId["player-ctrl-play"],
    playerCtrlElapsed: byId["player-ctrl-elapsed"],
    playerCtrlTimeline: byId["player-ctrl-timeline"],
    playerTimeline: byId["player-timeline"],
    playerTimelineFill: byId["player-timeline-fill"],
    playerTimelineBubble: byId["player-timeline-bubble"],
    playerStageSpinner: byId["player-stage-spinner"],
    playerOsdCenter: byId["player-osd-center"],
    playerOsdHint: byId["player-osd-hint"],
    playerCtrlDuration: byId["player-ctrl-duration"],
    playerCtrlMute: byId["player-ctrl-mute"],
    playerVolume: byId["player-volume"],
    playerVolumeSurface: byId["player-volume-surface"],
    playerCtrlVolume: byId["player-ctrl-volume"],
    playerCtrlSpeed: byId["player-ctrl-speed"],
    playerCtrlSubtitle: byId["player-ctrl-subtitle"],
    playerSubtitleChip: byId["player-subtitle-chip"],
    playerCtrlBookmark: byId["player-ctrl-bookmark"],
    playerTimelineFlags: byId["player-timeline-flags"],
    playerTimelineLabels: byId["player-timeline-labels"],
    playerTimelineHeat: byId["player-timeline-heat"],
    playerTimelineAssess: byId["player-timeline-assess"],
    playerInsightOpen: byId["player-insight-open"],
    playerInsightDialog: byId["player-insight-dialog"],
    insightErase: byId["insight-erase"],
    playerCtrlPip: byId["player-ctrl-pip"],
    playerCtrlTheatre: byId["player-ctrl-theatre"],
    playerCtrlFullscreen: byId["player-ctrl-fullscreen"],
    playerCtrlStatus: byId["player-ctrl-status"],
    generateSubtitle: byId["generate-subtitle"],
    generateNotes: byId["generate-notes"],
    playerActionHint: byId["player-action-hint"],
    playerTaskState: byId["player-task-state"],
    playerTaskText: byId["player-task-text"],
    playerTaskOpenDrawer: byId["player-task-open-drawer"],
    playerTitle: byId["player-title"],
    playerEvidence: byId["player-evidence"],
    studyDesk: byId["study-desk"],
    playerRecovery: byId["player-recovery"],
    playerRecoveryTitle: byId["player-recovery-title"],
    playerRecoveryImpact: byId["player-recovery-impact"],
    playerRecoveryActions: byId["player-recovery-actions"],
    playerKeysDialog: byId["player-keys-dialog"],
    playerSubtitleStyleDialog: byId["player-subtitle-style-dialog"],
    liveRoomRow: byId["live-room-row"],
    liveRoomLabel: byId["live-room-label"],
    liveRoomCapability: byId["live-room-capability"],
    liveRoomTime: byId["live-room-time"],
    liveRoomReason: byId["live-room-reason"],
    enterLiveRoom: byId["enter-live-room"],
    liveRoomRecheck: byId["live-room-recheck"],
    toastRegion: byId["toast-region"],
  };
  let created = 0;
  const documentTarget = new EventTarget();
  globalThis.document = Object.assign(documentTarget, {
    activeElement: null,
    documentElement: { dataset: {} },
    getElementById: (id) => byId[id] || null,
    createElement: () => new FakeElement(`created-${created += 1}`),
    querySelectorAll: () => [],
    /* FOCUS-DRIFT-1：dialog[open] 解析——播放快捷键的模态闸（hasOpenDialog）
       端到端断言依赖；此前 fake document 无 querySelector，零调用方不受影响。 */
    querySelector: (selector) => {
      if (String(selector) === "dialog[open]") {
        return Object.values(byId).find((node) => node.tagName === "dialog" && node.open) || null;
      }
      return null;
    },
    fullscreenElement: null,
    pictureInPictureEnabled: true,
    pictureInPictureElement: null,
    exitFullscreen: () => {
      documentTarget.fullscreenElement = null;
      return Promise.resolve();
    },
    exitPictureInPicture: () => {
      documentTarget.pictureInPictureElement = null;
      return Promise.resolve();
    },
  });
  const visibleText = () => {
    const collect = (element) => {
      if (!element || element.hidden) return [];
      return [String(element.textContent || ""), ...element.children.flatMap(collect)];
    };
    return Object.values(byId).flatMap(collect).join("\n");
  };
  return { elements, visibleText };
}

function createStore() {
  const listeners = new Map();
  return {
    auth: null,
    courses: [],
    activeCourse: null,
    activeLecture: null,
    tasks: [],
    set(key, value) {
      this[key] = value;
      for (const listener of listeners.get(key) || []) listener(value);
    },
    subscribe(key, listener) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(listener);
      return () => listeners.get(key)?.delete(listener);
    },
  };
}

const windowTarget = new EventTarget();
// 真实定时器：按住右方向的 400ms 临时 3 倍速阈值需要真正触发；
// 10 秒进度保存计时器在毫秒级测试进程内不会到点，行为与旧桩一致。
// 例外：S10-A 的 1.5s 自动隐藏计时器（唯一 1500ms 定时器）整段真实等待会
// 超出 pytest 包装器的 30s 子进程上限，单独映射为 150ms 真实等待；
// 1500 与 PLAYER_IDLE_HIDE_MS 由 workbench 源码锁保持同步。
// 例外②：音量面 3s 自动收口（VOLUME_AUTO_CLOSE_MS）映射 300ms 真实等待；
// 3000 与 VOLUME_AUTO_CLOSE_MS 同源码锁同步（补充F）。
windowTarget.setTimeout = (callback, ms) => setTimeout(callback, ms === 1500 ? 150 : ms === 3000 ? 300 : ms);
windowTarget.clearTimeout = (id) => clearTimeout(id);
// rAF 用 setImmediate 逐帧模拟：暂停/播放都持续运转，与真实引擎一致；
// 看护循环 stop 后链条自然终止，进程可正常退出。
windowTarget.requestAnimationFrame = (callback) => setImmediate(() => callback(0));
windowTarget.cancelAnimationFrame = () => {};
/* 补充E①：长按连续音量需要真实 interval 步进（subtitle watcher 同用，
   幂等重渲染不破坏断言；所有用点均有 keyup/cleanup 收口，进程可退） */
windowTarget.setInterval = (fn, ms) => setInterval(fn, ms);
windowTarget.clearInterval = (id) => clearInterval(id);
windowTarget.Hls = FakeHls;
globalThis.window = windowTarget;
/* N5PR-P2：localStorage 最小桩（player-core 的每课倍速记忆直用全局
   localStorage；后续字幕样式/学习洞察开关共用）。进程内 Map 承载。 */
const localStorageStub = (() => {
  let map = new Map();
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => map.set(String(key), String(value)),
    removeItem: (key) => map.delete(String(key)),
    clear: () => map.clear(),
    reset: () => map.clear(),
  };
})();
globalThis.localStorage = localStorageStub;
globalThis.CustomEvent = FakeCustomEvent;

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => {
  await nextTurn();
  await nextTurn();
};

let liveState = "unknown";
let statusCalls = 0;
let grantCalls = 0;
let sessionCalls = 0;
const statusByCourse = new Map();
const pendingStatus = new Map();
const pendingGrant = new Map();
/* MEDIA-001-20261001：媒体开流结局闭集码桩（错误卡细分证据源） */
let mediaStreamStatus = { failure_code: "", seq: 0 };
const pendingSession = new Map();
let progressCalls = 0;
/* SAVEPROGRESS-GUARD：POST /progress 请求体录制器（写侧讲次绑定钉用） */
const progressPostBodies = [];
/* BUGFIX-PLAYBACK-PROGRESS-1：持久观看进度读取桩（sub_id → 行 | "error"）。 */
let progressGetCalls = 0;
const progressGetRows = new Map();
let taskCalls = 0;
/* VPN-P3-MEDIA-CONTINUITY-1：记录每次 tasks/enqueue 请求体，用于幂等键一致性断言。 */
const enqueueBodies = [];
/* D2：bookmarks 创建桩状态。 */
let bookmarkPostCalls = 0;
/* F8a：bookmarks 创建桩失败/挂起控制（""=成功；"evidence"=缺依据分码；
   "raw"=未知失败；hold=true 时请求在途挂起，用 bookmarkPostHolds 放行）。 */
let bookmarkPostFailure = "";
let bookmarkPostHold = false;
const bookmarkPostHolds = [];
let explainMode = "ok";
let explainPostBodies = [];
let resolvePostBodies = [];
const bookmarkPostBodies = [];
/* PLAYER-UX-1④：bookmarks 删除桩状态（DELETE /api/v3/bookmarks）。 */
let bookmarkDeleteBodies = [];
/* D5/D6：timeline 与 quizzes 读取桩（sub_id → segments/items）。 */
const timelineGetRows = new Map();
const quizzesGetRows = new Map();
const bookmarksGetRows = new Map();
/* D7：watch-events 桩状态。 */
const watchEventsRows = new Map();
let watchEventsGetCalls = 0;
let watchEventsPostBodies = [];
let watchEventsClearCalls = 0;
let watchEventsClearBodies = [];
let failGrant = false;
/* VPN-P1-RECOVERY-1：受保护提交的合成失败（null = 正常受理）。 */
let enqueueFailure = null;
const rawFailure = "RAW transport failure with forbidden details";
const rawManifestPath = "/api/v3/live-room/play/raw-session/manifest/raw-resource";
let manifestFailureCode = "";
let manifestFailureCodeDraft = "";

function ok(data, status = 200) {
  return new Response(JSON.stringify({ schema: "courselens.api.v3", data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  if (route.startsWith("/api/v3/live-room/status?")) {
    statusCalls += 1;
    const courseId = new URL(route, "https://synthetic.invalid").searchParams.get("course_id");
    const pending = pendingStatus.get(courseId);
    if (pending) return pending.promise;
    const state = statusByCourse.get(courseId) || liveState;
    return ok({
      state,
      can_enter: state === "live",
      starts_at: "synthetic-start",
      ends_at: "synthetic-end",
    });
  }
  if (route === "/api/v3/live-room/grants") {
    grantCalls += 1;
    const courseId = JSON.parse(options.body || "{}").course_id;
    const pending = pendingGrant.get(courseId);
    if (pending) return pending.promise;
    if (failGrant) {
      return new Response(JSON.stringify({
        error: rawFailure,
        error_code: "live_grant_invalid",
        manifest_path: rawManifestPath,
      }), { status: 403, headers: { "Content-Type": "application/json" } });
    }
    return ok({ grant: "synthetic-grant" }, 201);
  }
  /* U17 B1：失败响应体闭集码桩（hls fatal 后对 manifest 路径补读一次） */
  if (route === rawManifestPath) {
    manifestFailureCode = manifestFailureCodeDraft;
    return new Response(JSON.stringify(
      manifestFailureCode ? { error_code: manifestFailureCode } : { error: "noop" },
    ), { status: manifestFailureCode ? 403 : 200, headers: { "Content-Type": "application/json" } });
  }
  if (route === "/api/v3/live-room/sessions") {
    sessionCalls += 1;
    const grant = JSON.parse(options.body || "{}").grant;
    const pending = pendingSession.get(grant);
    if (pending) return pending.promise;
    return ok({ manifest_path: rawManifestPath }, 201);
  }
  if (route === "/api/v3/media/stream-status") {
    return ok(mediaStreamStatus);
  }
  if (route === "/api/v3/progress" || route.startsWith("/api/v3/progress?")) {
    if (String(options.method || "GET") === "POST") {
      progressCalls += 1;
      progressPostBodies.push(JSON.parse(options.body || "{}"));
      return ok({ accepted: true });
    }
    progressGetCalls += 1;
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    const row = progressGetRows.get(subId);
    if (row === "error") {
      return new Response(JSON.stringify({ error: "synthetic progress failure", error_code: "runtime_failed" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    return ok({ progress: row || null });
  }
  if (route === "/api/v3/tasks/enqueue") {
    taskCalls += 1;
    enqueueBodies.push(String(options.body || ""));
    if (enqueueFailure) {
      return new Response(JSON.stringify(enqueueFailure.body), {
        status: enqueueFailure.status,
        headers: { "Content-Type": "application/json" },
      });
    }
    return ok({ task: { state: "accepted", task_id: "synthetic-task" } }, 201);
  }
  /* D2：bookmarks 创建桩（「没听懂」标记走既有 POST /api/v3/bookmarks） */
  if (route === "/api/v3/bookmarks" && String(options.method || "POST") === "POST") {
    bookmarkPostCalls += 1;
    bookmarkPostBodies.push(JSON.parse(options.body || "{}"));
    if (bookmarkPostFailure === "evidence") {
      return new Response(JSON.stringify({ error: "synthetic evidence gate", error_code: "bookmark_evidence_unavailable" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (bookmarkPostFailure === "raw") {
      return new Response(JSON.stringify({ error: "raw synthetic", error_code: "synthetic_raw_failure" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (bookmarkPostHold) {
      const pending = deferred();
      bookmarkPostHolds.push(pending);
      return pending.promise;
    }
    return ok({ bookmark: { bookmark_id: `synthetic-${bookmarkPostCalls}`, note: "没听懂" } }, 201);
  }
  /* U⑤：书签解释链桩（「解释」= POST bookmarks/explain；预算闸=declined 形状） */
  if (route === "/api/v3/bookmarks/explain") {
    explainPostBodies.push(JSON.parse(options.body || "{}"));
    if (explainMode === "broken") return ok({ unexpected: true });
    if (explainMode === "declined") {
      return ok({ bookmark: { bookmark_id: "synthetic-x", explanation: { mode: "declined", answer: "预算到线" } } });
    }
    return ok({ bookmark: { bookmark_id: "synthetic-x", explanation: { mode: "queued" } }, task: { state: "queued" }, created: true });
  }
  /* U⑤：懂了=POST bookmarks/actions resolve */
  if (route === "/api/v3/bookmarks/actions") {
    resolvePostBodies.push(JSON.parse(options.body || "{}"));
    return ok({ bookmark: { bookmark_id: "synthetic-x", resolution_status: "resolved" }, accepted_action: "resolve" });
  }
  /* PLAYER-UX-1④：删除=DELETE bookmarks */
  if (route === "/api/v3/bookmarks" && String(options.method || "") === "DELETE") {
    bookmarkDeleteBodies.push(JSON.parse(options.body || "{}"));
    return ok({ bookmark_id: "synthetic-x", deleted: true });
  }
  /* D5/D6：时间轴标签桩（evidence-rules 八标签闭集抽样） */
  if (route.startsWith("/api/v3/timeline?")) {
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ meta: { sub_id: subId, classifier_version: "evidence-rules-zh-v1", segment_count: 4 }, segments: timelineGetRows.get(subId) || [], stale: false });
  }
  /* D6：quiz/bookmark 读取桩 */
  if (route.startsWith("/api/v3/quizzes?")) {
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ items: quizzesGetRows.get(subId) || [] });
  }
  if (route.startsWith("/api/v3/bookmarks?")) {
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ bookmarks: bookmarksGetRows.get(subId) || [] });
  }
  /* D7：学习洞察三路由（GET 读 / POST 批写 / clear 抹除） */
  if (route.startsWith("/api/v3/watch-events?")) {
    watchEventsGetCalls += 1;
    const subId = new URL(route, "https://synthetic.invalid").searchParams.get("sub_id");
    return ok({ events: watchEventsRows.get(subId) || [] });
  }
  if (route === "/api/v3/watch-events" && String(options.method || "POST") === "POST") {
    watchEventsPostBodies.push(JSON.parse(options.body || "{}"));
    return ok({ inserted: (watchEventsPostBodies.at(-1)?.events || []).length });
  }
  if (route === "/api/v3/watch-events/clear" && String(options.method || "POST") === "POST") {
    watchEventsClearCalls += 1;
    watchEventsClearBodies.push(JSON.parse(options.body || "{}"));
    return ok({ deleted: 7 });
  }
  throw new Error(`unexpected synthetic route: ${route} ${options.method || "GET"}`);
};

const { installLiveRoom } = await import("../frontend/modules/live-room.js");
const { installPlayerCore } = await import("../frontend/modules/player-core.js");

async function verifyLiveRoomBehavior() {
  const page = createPage();
  const store = createStore();
  const livePlayEvents = [];
  const onLivePlay = (event) => livePlayEvents.push(event.detail);
  window.addEventListener("courselens:live-play", onLivePlay);
  const cleanup = await installLiveRoom(store);

  // 安装路径即建立原子 status 语义：整个行作为单一 polite 原子区域播报
  assert.equal(page.elements.liveRoomRow.attributes.get("role"), "status");
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-live"), "polite");
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-atomic"), "true");

  /* CLOUD-AUTO-UI-1 CTA 矩阵：唯一主 CTA=进入直播；紧凑重试只属于可行动错误；
     普通课表态/等待态无任何按钮；能力行与状态标题分开陈述 */
  const cases = Object.freeze({
    live: { enter: true, retry: false, capability: "直播入口可用", label: "进入直播", reason: "后端已确认，可以安全进入直播。" },
    upcoming: { enter: false, retry: false, capability: "直播未开始", reason: "开始时间尚未到，开始前会保持安静。" },
    ended: { enter: false, retry: false, capability: "直播已结束", reason: "可以在讲次列表中查看已授权回放。" },
    denied: { enter: false, retry: false, capability: "无直播权限", reason: "当前账号没有这门课程的直播访问权限。" },
    offline: { enter: false, retry: true, capability: "直播服务暂不可用", label: "重新确认", reason: "暂时无法取得新的直播状态，可检查网络后重试。" },
    stale: { enter: false, retry: true, capability: "直播状态待确认", label: "重新确认", reason: "上次观测已经过期，重新确认后才能进入。" },
    unknown: { enter: false, retry: false, capability: "直播状态待确认", reason: "还没拿到这门课的直播状态，拿到后这里会更新。" },
  });

  for (const [state, expected] of Object.entries(cases)) {
    liveState = state;
    store.set("activeCourse", { course_id: `course-${state}`, title: "合成课程" });
    assert.equal(page.elements.liveRoomRow.dataset.state, "loading");
    assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
    await settle();
    assert.equal(page.elements.liveRoomRow.dataset.state, state);
    assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "false");
    /* G2 硬化：reason 等值钉 live-room.js 状态表（无空串兜底，措辞漂移直接红） */
    assert.equal(page.elements.liveRoomReason.textContent, expected.reason, `${state} reason 行等值钉`);
    assert.equal(page.elements.liveRoomCapability.textContent, expected.capability, `${state} 能力行闭集`);
    assert.equal(page.elements.enterLiveRoom.hidden, !expected.enter);
    if (expected.enter) {
      assert.equal(page.elements.enterLiveRoom.dataset.action, "enter");
      assert.equal(page.elements.enterLiveRoom.textContent, expected.label);
    }
    assert.equal(page.elements.liveRoomRecheck.hidden, !expected.retry, `${state} 紧凑重试可见性`);
    if (expected.retry) assert.equal(page.elements.liveRoomRecheck.textContent, expected.label);

    const beforeStatus = statusCalls;
    const beforeGrant = grantCalls;
    if (expected.retry) {
      page.elements.liveRoomRecheck.click();
      await settle();
      assert.ok(statusCalls > beforeStatus, `${state} 紧凑重试触发一次状态确认`);
    } else if (!expected.enter) {
      page.elements.enterLiveRoom.click();
      await settle();
      assert.equal(statusCalls, beforeStatus, `${state} 无 CTA 不发状态请求`);
      assert.equal(grantCalls, beforeGrant, `${state} 无 CTA 不发进入请求`);
    }
  }

  const raceCourseA = { course_id: "race-course-a", title: "Race A" };
  const raceCourseB = { course_id: "race-course-b", title: "Race B" };
  statusByCourse.set(raceCourseA.course_id, "live");
  statusByCourse.set(raceCourseB.course_id, "upcoming");
  const liveUi = () => ({
    state: page.elements.liveRoomRow.dataset.state,
    label: page.elements.liveRoomLabel.textContent,
    capability: page.elements.liveRoomCapability.textContent,
    reason: page.elements.liveRoomReason.textContent,
    action: page.elements.enterLiveRoom.dataset.action,
    disabled: page.elements.enterLiveRoom.disabled,
  });
  const assertBRemainsCurrent = (uiBefore) => {
    assert.equal(page.elements.liveRoomRow.dataset.state, "upcoming");
    assert.equal(page.elements.liveRoomRecheck.hidden, true, "普通 upcoming 不提供重试 CTA");
    assert.deepEqual(liveUi(), uiBefore);
  };

  /* N6L S1 U2：进入直播=跳转直播页（liveTarget 预选+select-page 事件）；
     学习页不再保留 grants/sessions 进入链，点击零进入请求 */
  const selectPageEvents = [];
  const onSelectPage = (event) => selectPageEvents.push(event.detail);
  window.addEventListener("courselens:select-page", onSelectPage);

  const delayedStatusA = deferred();
  pendingStatus.set(raceCourseA.course_id, delayedStatusA);
  store.set("activeCourse", raceCourseA);
  await nextTurn();
  store.set("activeCourse", raceCourseB);
  await settle();
  const bStatusUi = liveUi();
  delayedStatusA.resolve(ok({ state: "live", can_enter: true }));
  pendingStatus.delete(raceCourseA.course_id);
  await settle();
  assertBRemainsCurrent(bStatusUi);

  // busy 生命周期与当前 epoch 绑定：新旧两门课程同时在途时，
  // 过期课程响应既不得渲染也不得清除当前课程查询的 aria-busy
  const deferredBusyA = deferred();
  const deferredBusyB = deferred();
  pendingStatus.set(raceCourseA.course_id, deferredBusyA);
  store.set("activeCourse", raceCourseA);
  await nextTurn();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
  assert.equal(page.elements.liveRoomRow.dataset.state, "loading");
  pendingStatus.set(raceCourseB.course_id, deferredBusyB);
  store.set("activeCourse", raceCourseB);
  await settle();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
  assert.equal(page.elements.liveRoomRow.dataset.state, "loading");
  deferredBusyA.resolve(ok({ state: "live", can_enter: true }));
  pendingStatus.delete(raceCourseA.course_id);
  await settle();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
  assert.equal(page.elements.liveRoomRow.dataset.state, "loading");
  deferredBusyB.resolve(ok({ state: "upcoming", can_enter: false }));
  pendingStatus.delete(raceCourseB.course_id);
  await settle();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "false");
  assertBRemainsCurrent({
    state: "upcoming",
    label: "直播尚未开始",
    capability: "直播未开始",
    reason: "开始时间尚未到，开始前会保持安静。",
    action: "",
    disabled: true,
  });

  // 可见失败（offline）也是落定：busy 必须随当前请求的失败清除
  const failCourse = { course_id: "course-status-fail", title: "失败课程" };
  const deferredFail = deferred();
  pendingStatus.set(failCourse.course_id, deferredFail);
  store.set("activeCourse", failCourse);
  await nextTurn();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
  deferredFail.reject(new Error("synthetic status failure"));
  pendingStatus.delete(failCourse.course_id);
  await settle();
  assert.equal(page.elements.liveRoomRow.dataset.state, "offline");
  assert.equal(page.elements.liveRoomRecheck.hidden, false, "offline 提供紧凑重试");
  assert.equal(page.elements.enterLiveRoom.hidden, true);
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "false");

  // 无课程落定同样清除 busy；其后的过期响应不得让 UI 复活
  const deferredNoCourse = deferred();
  pendingStatus.set(failCourse.course_id, deferredNoCourse);
  store.set("activeCourse", failCourse);
  await nextTurn();
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "true");
  store.set("activeCourse", null);
  await settle();
  assert.equal(page.elements.liveRoomRow.dataset.state, "idle");
  assert.equal(page.elements.enterLiveRoom.dataset.action, "");
  assert.equal(page.elements.enterLiveRoom.hidden, true);
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "false");
  deferredNoCourse.resolve(ok({ state: "live", can_enter: true }));
  pendingStatus.delete(failCourse.course_id);
  await settle();
  assert.equal(page.elements.liveRoomRow.dataset.state, "idle");
  assert.equal(page.elements.liveRoomRow.attributes.get("aria-busy"), "false");

  store.set("activeCourse", raceCourseA);
  await settle();
  /* N6L S1 U2：进入直播=跳转直播页（liveTarget 预选+select-page 事件），
     学习页零进入请求；跨目标切换不再有在途会话竞态 */
  const grantsBeforeJump = grantCalls;
  page.elements.enterLiveRoom.click();
  await settle();
  assert.equal(grantCalls, grantsBeforeJump, "跳转语义零进入请求");
  assert.equal(store.liveTarget, "race-course-a", "跳转前预选 liveTarget");
  assert.equal(selectPageEvents.at(-1), "live", "进入直播派发 select-page live");

  store.set("activeCourse", raceCourseB);
  await settle();
  assertBRemainsCurrent(liveUi());

  liveState = "live";
  store.set("activeCourse", { course_id: "course-live", title: "合成课程" });
  await settle();
  const beforeGrant = grantCalls;
  page.elements.enterLiveRoom.click();
  await settle();
  assert.equal(grantCalls, beforeGrant, "进入直播不再发 grants（会话链归直播页）");
  assert.equal(store.liveTarget, "course-live");
  assert.equal(selectPageEvents.at(-1), "live");
  assert.equal(page.visibleText().includes(rawManifestPath), false);

  const frozenLiveUi = {
    state: page.elements.liveRoomRow.dataset.state,
    label: page.elements.liveRoomLabel.textContent,
    reason: page.elements.liveRoomReason.textContent,
    action: page.elements.enterLiveRoom.dataset.action,
  };
  const callsBeforeCleanup = { statusCalls, grantCalls, sessionCalls };
  cleanup();
  window.dispatchEvent(new Event("courselens:live-refresh"));
  page.elements.enterLiveRoom.click();
  store.set("activeCourse", { course_id: "course-after-cleanup", title: "不应渲染" });
  await settle();
  assert.deepEqual({ statusCalls, grantCalls, sessionCalls }, callsBeforeCleanup);
  assert.deepEqual({
    state: page.elements.liveRoomRow.dataset.state,
    label: page.elements.liveRoomLabel.textContent,
    reason: page.elements.liveRoomReason.textContent,
    action: page.elements.enterLiveRoom.dataset.action,
  }, frozenLiveUi);
  window.removeEventListener("courselens:live-play", onLivePlay);
}

async function verifyPlayerBehavior() {
  const page = createPage();
  const store = createStore();
  let loginEvents = 0;
  let liveRefreshEvents = 0;
  const onLogin = (event) => {
    loginEvents += 1;
    assert.equal(event.detail, "reauth");
  };
  const onLiveRefresh = () => { liveRefreshEvents += 1; };
  window.addEventListener("courselens:open-login", onLogin);
  window.addEventListener("courselens:live-refresh", onLiveRefresh);
  const cleanup = await installPlayerCore(store);
  const player = page.elements.playerStage;
  const panel = page.elements.playerRecovery;
  const actions = page.elements.playerRecoveryActions;

  const lecture = {
    course_id: "course-safe",
    sub_id: "lecture-safe",
    sub_title: "合成讲次",
    can_stream: true,
  };
  store.set("activeLecture", lecture);
  assert.equal(player.src, "/api/v3/media?sub_id=lecture-safe");
  const firstLoadCount = player.loadCount;

  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false);
  assert.equal(page.elements.playerRecoveryTitle.textContent, "当前媒体暂不可用");
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("若代理工具开启了 TUN 模式，请关闭 TUN 后再试。"));
  assert.deepEqual(actions.children.map((button) => button.textContent), [
    "重新获取播放授权", "重新认证",
  ]);
  actions.children[0].click();
  assert.ok(player.loadCount > firstLoadCount);
  assert.equal(player.src, "/api/v3/media?sub_id=lecture-safe");
  assert.equal(panel.hidden, true);

  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false);
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("若代理工具开启了 TUN 模式，请关闭 TUN 后再试。"));
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(panel.hidden, true);

  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  actions.children.find((button) => button.textContent === "重新认证").click();
  assert.equal(loginEvents, 1);

  store.set("activeLecture", { ...lecture, can_stream: false });
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false);
  assert.equal(page.elements.playerRecoveryTitle.textContent, "播放授权需要更新");
  assert.equal(page.elements.playerRecoveryImpact.textContent.includes("关闭 TUN"), false);
  store.set("activeLecture", lecture);
  assert.equal(panel.hidden, true);

  player.error = { code: 3 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false);
  assert.equal(page.elements.playerRecoveryImpact.textContent.includes("关闭 TUN"), false);
  store.set("activeLecture", { ...lecture, sub_id: "lecture-next" });
  assert.equal(panel.hidden, true);
  assert.equal(player.src, "/api/v3/media?sub_id=lecture-next");

  /* 清理后零响应（以讲次载入为载体；直播会话已整体归独立直播页，
     N6L S1 U3——本函数不再承载直播失败卡场景，见 frontend_live_page_behavior.mjs） */
  store.set("activeLecture", { ...lecture, sub_id: "lecture-cleanup", sub_title: "清理前讲次" });
  await settle();
  const frozenPlayerUi = {
    title: page.elements.playerTitle.textContent,
    evidence: page.elements.playerEvidence.textContent,
    recoveryTitle: page.elements.playerRecoveryTitle.textContent,
    recoveryHidden: panel.hidden,
    src: player.src,
    loadCount: player.loadCount,
  };
  const playerCallsBeforeCleanup = { progressCalls, progressGetCalls, taskCalls };
  cleanup();

  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  player.dispatchEvent(new Event("loadedmetadata"));
  player.dispatchEvent(new Event("playing"));
  player.dispatchEvent(new Event("timeupdate"));
  player.dispatchEvent(new Event("pause"));
  player.dispatchEvent(new Event("ended"));
  window.dispatchEvent(new Event("courselens:live-refresh"));
  page.elements.generateSubtitle.click();
  page.elements.generateNotes.click();
  store.set("activeLecture", { ...lecture, sub_id: "lecture-after-cleanup", sub_title: "不应渲染" });
  await settle();
  assert.deepEqual({ progressCalls, progressGetCalls, taskCalls }, playerCallsBeforeCleanup);
  assert.deepEqual({
    title: page.elements.playerTitle.textContent,
    evidence: page.elements.playerEvidence.textContent,
    recoveryTitle: page.elements.playerRecoveryTitle.textContent,
    recoveryHidden: panel.hidden,
    src: player.src,
    loadCount: player.loadCount,
  }, frozenPlayerUi);
  window.removeEventListener("courselens:open-login", onLogin);
  window.removeEventListener("courselens:live-refresh", onLiveRefresh);
}

/* MEDIA-001-20261001：code=4 兜底卡的闭集细分——服务端开流结局码驱动
   校外场景卡/授权卡/通用卡的归因，证据晚到不闪换、不跨回合复活。 */
async function verifyMediaSourceFailureRefinement() {
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  const player = page.elements.playerStage;
  const panel = page.elements.playerRecovery;
  const actions = page.elements.playerRecoveryActions;
  store.set("activeLecture", { course_id: "course-rc", sub_id: "sub-rc", sub_title: "细分讲次", can_stream: true });

  // A) 证据未到前：既有合成卡同步兜底（绝不闪空面板）
  mediaStreamStatus = { failure_code: "upstream_unreachable", seq: 1 };
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false, "错误回合同步渲染兜底卡");
  assert.equal(page.elements.playerRecoveryTitle.textContent, "当前媒体暂不可用");

  // B) 闭集证据到场：同回合原位细分为校外场景卡（重登录无意义 → 单动作）
  await settle();
  assert.equal(page.elements.playerRecoveryTitle.textContent, "这个网络看不了这节回放", "细分卡原位替换");
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("校园网"), "诚实处方=校园网/学校 VPN");
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("TUN"), "保留 TUN 检测线索");
  assert.deepEqual(actions.children.map((button) => button.textContent), ["重新获取播放授权"]);
  const loadsBefore = player.loadCount;
  actions.children[0].click();
  assert.equal(player.loadCount, loadsBefore + 1, "重试动作仍重载媒体");
  assert.equal(panel.hidden, true, "重载后面板收起");

  // C) 授权类结局 → 细分到既有授权卡（重认证是解）
  mediaStreamStatus = { failure_code: "auth_required", seq: 2 };
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  await settle();
  assert.equal(page.elements.playerRecoveryTitle.textContent, "播放授权需要更新");
  assert.ok(actions.children.some((button) => button.textContent === "重新认证"));

  // D) 上游明确拒绝：无独立处方 → 保留通用卡（retry+重新认证都在）
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(panel.hidden, true);
  mediaStreamStatus = { failure_code: "upstream_rejected", seq: 3 };
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  await settle();
  assert.equal(page.elements.playerRecoveryTitle.textContent, "当前媒体暂不可用", "无细分处方时保留通用卡");
  assert.deepEqual(actions.children.map((button) => button.textContent), [
    "重新获取播放授权", "重新认证",
  ]);

  // E) 陈旧证据不跨回合：晚到的细分绝不复活已收起的面板
  player.dispatchEvent(new Event("loadedmetadata"));
  mediaStreamStatus = { failure_code: "upstream_unreachable", seq: 4 };
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(panel.hidden, false);
  store.set("activeLecture", { course_id: "course-rc", sub_id: "sub-rc-next", sub_title: "下一讲", can_stream: true });
  assert.equal(panel.hidden, true);
  await settle();
  assert.equal(panel.hidden, true, "细分证据不复活已收起的面板");

  // F) MEDIA-VPN-1：atrust=not_installed → 引导安装卡（官方入口 vpn.fudan.edu.cn）
  mediaStreamStatus = {
    failure_code: "upstream_unreachable", seq: 6,
    atrust: { state: "not_installed", signals: { process: false, service: false, directory: false }, proxy: { system_configured: false, env_configured: false } },
  };
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  await settle();
  assert.equal(page.elements.playerRecoveryTitle.textContent, "还没检测到学校 VPN 客户端", "未装细分卡原位替换");
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("vpn.fudan.edu.cn"), "官方入口文案指路");
  assert.deepEqual(actions.children.map((button) => button.textContent), ["重新获取播放授权"]);

  // G) atrust=present → 确认接入卡（含设置开关指路文案）
  mediaStreamStatus = {
    failure_code: "upstream_unreachable", seq: 7,
    atrust: { state: "present", signals: { process: true, service: true, directory: true }, proxy: { system_configured: false, env_configured: false } },
  };
  player.dispatchEvent(new Event("loadedmetadata"));
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  await settle();
  assert.equal(page.elements.playerRecoveryTitle.textContent, "检测到学校 VPN，请确认已连接", "在位细分卡原位替换");
  assert.ok(page.elements.playerRecoveryImpact.textContent.includes("媒体流走系统代理"), "设置开关指路文案");
  assert.deepEqual(actions.children.map((button) => button.textContent), ["重新获取播放授权"]);

  mediaStreamStatus = { failure_code: "", seq: 8 };
  cleanup();
}

async function verifySubtitleOverlayBehavior() {
  const page = createPage();
  const store = createStore();
  const doc = globalThis.document;
  const overlay = page.elements.playerSubtitleOverlay;
  const shell = page.elements.playerStageShell;
  const trackElement = page.elements.playerSubtitleTrack;
  const player = page.elements.playerStage;
  const textTrack = new FakeTextTrack();
  trackElement.track = textTrack;
  const cue = (startTime, endTime, text) => ({ startTime, endTime, text });
  const cleanup = await installPlayerCore(store);

  // 讲次载入即把 track 置为 hidden（原生渲染禁用、解析保留），overlay 初始隐藏
  store.set("activeLecture", { course_id: "course-sub", sub_id: "sub-1", sub_title: "字幕讲次", can_stream: true });
  // N10B-2：文件轨延迟挂载——开讲零 /subtitles/file 请求，等 transcriptHasTiming 事实到场
  assert.equal(trackElement.src, "");
  assert.equal(textTrack.mode, "hidden");
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.textContent, "");
  store.set("transcriptHasTiming", true); /* 字幕 segments 落地（有可计时行）→ 挂文件轨 */
  assert.equal(trackElement.src, "/api/v3/subtitles/file?sub_id=sub-1");

  // 过长 cue：完整文本进入 overlay DOM；视觉截断只允许发生在 CSS 两行封顶层
  const longText = "线".repeat(120);
  textTrack.addCue(cue(0, 2.5, longText));
  textTrack.dispatchEvent(new Event("cuechange"));
  player.currentTime = 1;
  player.dispatchEvent(new Event("timeupdate"));
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.children.length, 0);
  assert.equal(overlay.textContent, longText);

  // 无 active cue 时清除
  player.currentTime = 3;
  textTrack.dispatchEvent(new Event("cuechange"));
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.textContent, "");

  // 重叠 active cue 合并为同一个 overlay 盒子，不产生第二块
  textTrack.addCue(cue(3.5, 4.5, "重叠甲"));
  textTrack.addCue(cue(3.5, 4.5, "重叠乙"));
  player.currentTime = 4;
  textTrack.dispatchEvent(new Event("cuechange"));
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.children.length, 0);
  assert.equal(overlay.textContent, "重叠甲 重叠乙");

  // seek 路径（seeked 事件）同样刷新 overlay
  player.currentTime = 1;
  player.dispatchEvent(new Event("seeked"));
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.textContent, longText);

  // 既有字幕开关（原生控制条 CC 按钮）：真实引擎的模式切换不派发任何事件，
  // 由 rAF 看护在一个 tick 内感知。off（mode=disabled）→ 隐藏并停止 cue 刷新
  textTrack.mode = "disabled";
  await settle();
  assert.equal(overlay.hidden, true);
  player.dispatchEvent(new Event("timeupdate"));
  textTrack.dispatchEvent(new Event("cuechange"));
  assert.equal(overlay.hidden, true);

  // on（mode=showing）→ 一个 tick 内强制回 hidden，overlay 恢复且保持唯一渲染者
  player.currentTime = 4;
  textTrack.mode = "showing";
  await settle();
  assert.equal(textTrack.mode, "hidden");
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.children.length, 0);
  assert.equal(overlay.textContent, "重叠甲 重叠乙");

  // 暂停下 CC off：无事件、无 timeupdate，看护仍在一个 tick 内清空（不冻结）
  textTrack.mode = "disabled";
  await settle();
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.textContent, "");

  // 暂停下 CC on：看护强制回 hidden 并立即恢复渲染
  textTrack.mode = "showing";
  await settle();
  assert.equal(textTrack.mode, "hidden");
  assert.equal(overlay.hidden, false);
  assert.equal(overlay.textContent, "重叠甲 重叠乙");

  /* N6L S1 U3：直播播放已整体移交独立直播页（live-player.js 自有 video），
     本播放器不再有「直播清空 overlay」场景——讲次卸载清空仍由下一节覆盖 */

  // 讲次卸载清空 overlay
  store.set("activeLecture", null);
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.textContent, "");

  // 一方全屏：按钮在用户手势内直接对 shell 请求；fullscreenchange 仅同步按钮状态。
  // 旧的原生视频"退出后重进 shell"重定向已删除——即使全屏元素异常为 video 也不得改道。
  store.set("activeLecture", { course_id: "course-sub", sub_id: "sub-1", sub_title: "字幕讲次", can_stream: true });
  let shellFullscreenRequests = 0;
  shell.requestFullscreen = () => {
    shellFullscreenRequests += 1;
    doc.fullscreenElement = shell;
    doc.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  };
  const subtitleButtonBefore = {
    hidden: page.elements.playerCtrlSubtitle.hidden,
    pressed: page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"),
  };
  assert.equal(subtitleButtonBefore.hidden, false);
  assert.equal(subtitleButtonBefore.pressed, "true");
  page.elements.playerCtrlFullscreen.click();
  assert.equal(shellFullscreenRequests, 1);
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"), "true");
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-label"), "退出全屏");
  doc.fullscreenElement = player; /* 模拟异常全屏来源：不得再触发重定向 */
  doc.dispatchEvent(new Event("fullscreenchange"));
  await settle();
  assert.equal(doc.fullscreenElement, player);
  assert.equal(shellFullscreenRequests, 1);
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"), "false");

  // 清理后监听全部解除：cue/mode/fullscreen 事件不再改变 overlay、track 或按钮状态
  cleanup();
  textTrack.dispatchEvent(new Event("cuechange"));
  textTrack.mode = "showing";
  player.dispatchEvent(new Event("timeupdate"));
  const frozenFullscreenButton = {
    pressed: page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"),
    label: page.elements.playerCtrlFullscreen.getAttribute("aria-label"),
  };
  doc.fullscreenElement = shell;
  doc.dispatchEvent(new Event("fullscreenchange"));
  await settle();
  assert.equal(textTrack.mode, "showing");
  assert.equal(overlay.hidden, true);
  assert.equal(overlay.textContent, "");
  assert.equal(doc.fullscreenElement, shell);
  assert.deepEqual({
    pressed: page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"),
    label: page.elements.playerCtrlFullscreen.getAttribute("aria-label"),
  }, frozenFullscreenButton);
}

// ---- 一方控制台行为：控件矩阵、时间轴、快捷键与按住 2 倍速、PiP/影院/全屏 ----

// 键事件从 window 派发（与真实窗口级监听一致）；target 可伪造为焦点元素。
// Node 的 EventTarget 没有祖先链，own-property target 遮蔽原型 getter 即可模拟。
function playerKeyEvent(type, key, { target, repeat = false, modifiers = {} } = {}) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  event.key = key;
  event.repeat = repeat;
  for (const [name, value] of Object.entries(modifiers)) event[name] = value;
  if (target) Object.defineProperty(event, "target", { value: target });
  return event;
}

const holdWait = () => new Promise((resolve) => setTimeout(resolve, 450));
/* N5PR-P3：600ms OSD 自收真实等待（harness 只映射 1500→150） */
const osdHideWait = () => new Promise((resolve) => setTimeout(resolve, 700));

function wheelEvent(deltaY, { target } = {}) {
  const event = new Event("wheel", { bubbles: true });
  event.deltaY = deltaY;
  if (target) Object.defineProperty(event, "target", { value: target });
  return event;
}

function pointerMoveEvent(clientX) {
  const event = new Event("pointermove", { bubbles: true });
  event.clientX = clientX;
  return event;
}

async function verifyPlayerControlsBehavior() {
  const page = createPage();
  const store = createStore();
  const doc = globalThis.document;
  const player = page.elements.playerStage;
  const shell = page.elements.playerStageShell;
  const status = page.elements.playerCtrlStatus;
  const editable = new FakeElement("editable-field");
  editable.closest = () => editable; /* 模拟焦点在字段/滑杆/按钮/菜单内 */
  const cleanup = await installPlayerCore(store);

  // 未选讲次：控制台整体隐藏、原生 controls 属性级兜底关闭、快捷键不生效
  assert.equal(page.elements.playerControls.hidden, true);
  assert.equal(player.controls, false);
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  assert.equal(player.paused, true);

  // 选择讲次：控制台可见；元数据前时间轴诚实禁用，不显示 NaN
  store.set("activeLecture", { course_id: "course-ctl", sub_id: "sub-ctl", sub_title: "控制讲次", can_stream: true });
  assert.equal(page.elements.playerControls.hidden, false);
  assert.equal(page.elements.playerCtrlDuration.textContent, "0:00");
  assert.equal(page.elements.playerCtrlElapsed.textContent, "0:00");
  assert.equal(page.elements.playerCtrlTimeline.disabled, true);

  // 元数据与 timeupdate：elapsed/duration/fill/max 全部同步
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.playerCtrlDuration.textContent, "10:00");
  assert.equal(page.elements.playerCtrlTimeline.max, "600");
  assert.equal(page.elements.playerCtrlTimeline.disabled, false);
  player.currentTime = 60;
  player.dispatchEvent(new Event("timeupdate"));
  assert.equal(page.elements.playerCtrlElapsed.textContent, "1:00");
  assert.equal(page.elements.playerCtrlTimeline.value, "60");
  assert.equal(page.elements.playerTimelineFill.style.getPropertyValue("--play-ratio"), "0.1");
  // 时长格式化跨过 1 小时
  player.duration = 3755;
  player.dispatchEvent(new Event("durationchange"));
  assert.equal(page.elements.playerCtrlDuration.textContent, "1:02:35");
  player.duration = 600;
  player.dispatchEvent(new Event("durationchange"));

  // 播放/暂停：按钮与 Space 双通道，状态由文本与图标状态承载
  page.elements.playerCtrlPlay.click();
  assert.equal(player.paused, false);
  player.dispatchEvent(new Event("play"));
  assert.equal(page.elements.playerCtrlPlay.getAttribute("aria-label"), "暂停");
  assert.equal(page.elements.playerCtrlPlay.dataset.state, "playing");
  const spaceToggle = playerKeyEvent("keydown", " ", { target: player });
  window.dispatchEvent(spaceToggle);
  assert.equal(player.paused, true);
  assert.equal(spaceToggle.defaultPrevented, true);
  player.dispatchEvent(new Event("pause"));
  assert.equal(page.elements.playerCtrlPlay.getAttribute("aria-label"), "播放");
  assert.equal(page.elements.playerCtrlPlay.dataset.state, "paused");

  // 快捷键让位：焦点在字段/滑杆/按钮/菜单上时 Space 与方向键不归播放器
  player.currentTime = 100;
  const blockedSpace = playerKeyEvent("keydown", " ", { target: editable });
  window.dispatchEvent(blockedSpace);
  assert.equal(player.paused, true);
  assert.equal(blockedSpace.defaultPrevented, false);
  const blockedSeek = playerKeyEvent("keydown", "ArrowRight", { target: editable });
  window.dispatchEvent(blockedSeek);
  assert.equal(player.currentTime, 100);
  const blockedModifier = playerKeyEvent("keydown", "ArrowRight", { target: player, modifiers: { ctrlKey: true } });
  window.dispatchEvent(blockedModifier);
  assert.equal(player.currentTime, 100);

  // 左/右 5 秒 seek + 双端钳制 + 播报（B 站式 keyup 裁决：右短按松开才 seek）
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  assert.equal(player.currentTime, 100, "按下不立即 seek（长按不先跳秒）");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, 105);
  assert.ok(status.textContent.includes("快进 5 秒"));
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowLeft", { target: player }));
  assert.equal(player.currentTime, 100);
  assert.ok(status.textContent.includes("快退 5 秒"));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowLeft"));
  player.currentTime = 598;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, 600); /* 钳到时长上界 */
  player.currentTime = 1;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowLeft", { target: player }));
  assert.equal(player.currentTime, 0); /* 钳到 0 */
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowLeft"));
  // 按住右方向的 OS 重复 keydown 不再叠加 seek（由临时 3 倍速逻辑接管）
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player, repeat: true }));
  assert.equal(player.currentTime, 0);
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  // NIGHT2-G-A（P16 键位包）：J/L 大步跳转、</> 倍速档、? 键位帮助
  window.dispatchEvent(playerKeyEvent("keydown", "l", { target: player }));
  assert.equal(player.currentTime, 10, "L 快进 10 秒");
  assert.ok(status.textContent.includes("快进 10 秒"));
  window.dispatchEvent(playerKeyEvent("keydown", "j", { target: player }));
  assert.equal(player.currentTime, 0, "J 快退 10 秒");
  window.dispatchEvent(playerKeyEvent("keyup", "l"));
  player.playbackRate = 1;
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(player.playbackRate, 1.25, "> 提高到 1.25 倍");
  assert.ok(status.textContent.includes("1.25"));
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(player.playbackRate, 1.75, "> 三连到 1.75（N5PR-P2 档位缺陷修复证据）");
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(player.playbackRate, 2, "> 第四档到 2 倍");
  window.dispatchEvent(playerKeyEvent("keydown", "<", { target: player }));
  assert.equal(player.playbackRate, 1.75, "< 降低一档回 1.75");
  const keysDialog = page.elements.playerKeysDialog;
  window.dispatchEvent(playerKeyEvent("keydown", "?", { target: player }));
  assert.equal(keysDialog.open, true, "? 打开键位帮助");
  window.dispatchEvent(playerKeyEvent("keydown", "?", { target: player }));
  assert.equal(keysDialog.open, false, "再按 ? 关闭键位帮助");
  player.playbackRate = 1; /* 复位，避免污染后续倍速场景 */

  // N5PR-P1 键位补齐：K/M/F/C/↑↓（响应同帧内；编辑态让位与 repeat 忽略同既有键）
  player.pause();
  const kLatencyStart = performance.now();
  window.dispatchEvent(playerKeyEvent("keydown", "k", { target: player }));
  assert.equal(player.paused, false, "K 播放");
  assert.ok(performance.now() - kLatencyStart < 5, "K 键状态应用同帧内（<5ms）");
  window.dispatchEvent(playerKeyEvent("keydown", "K", { target: player }));
  assert.equal(player.paused, true, "K 暂停（大小写同键）");
  player.play();
  window.dispatchEvent(playerKeyEvent("keydown", "k", { target: player, repeat: true }));
  assert.equal(player.paused, false, "K 的 OS 重复 keydown 忽略");
  player.pause();
  const blockedK = playerKeyEvent("keydown", "k", { target: editable });
  window.dispatchEvent(blockedK);
  assert.equal(blockedK.defaultPrevented, false, "K 编辑态让位");

  player.dispatchEvent(new Event("volumechange")); /* 安装口径：volume=1 入记忆 */
  window.dispatchEvent(playerKeyEvent("keydown", "m", { target: player }));
  assert.equal(player.muted, true, "M 静音");
  player.dispatchEvent(new Event("volumechange")); /* 引擎镜像：muted 写入即派发 */
  assert.equal(page.elements.playerCtrlMute.dataset.state, "muted", "M 静音图标同步");
  window.dispatchEvent(playerKeyEvent("keydown", "m", { target: player }));
  assert.equal(player.muted, false, "M 再按恢复（记忆音量不动）");
  player.dispatchEvent(new Event("volumechange"));

  let fullscreenRequests = 0;
  shell.requestFullscreen = () => {
    fullscreenRequests += 1;
    return Promise.resolve();
  };
  window.dispatchEvent(playerKeyEvent("keydown", "f", { target: player }));
  assert.equal(fullscreenRequests, 1, "F 请求全屏");
  const repeatF = playerKeyEvent("keydown", "f", { target: player, repeat: true });
  window.dispatchEvent(repeatF);
  assert.equal(repeatF.defaultPrevented, false, "F 的 OS 重复 keydown 忽略不拦截");

  const noTrackC = playerKeyEvent("keydown", "c", { target: player });
  window.dispatchEvent(noTrackC);
  assert.equal(noTrackC.defaultPrevented, false, "C 无字幕轨静默忽略");
  page.elements.playerSubtitleTrack.track = new FakeTextTrack();
  player.dispatchEvent(new Event("loadedmetadata")); /* wireSubtitleTrack 接轨 */
  window.dispatchEvent(playerKeyEvent("keydown", "c", { target: player }));
  assert.equal(page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"), "false", "C 关闭字幕");
  window.dispatchEvent(playerKeyEvent("keydown", "c", { target: player }));
  assert.equal(page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"), "true", "C 开启字幕");

  player.volume = 0.5;
  player.dispatchEvent(new Event("volumechange")); /* 0.5 入记忆 */
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowUp", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowUp"));
  assert.equal(player.volume, 0.55, "↑ 音量 +5%");
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowDown", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowDown"));
  assert.equal(player.volume, 0.5, "↓ 音量 -5%");
  player.volume = 1;
  player.dispatchEvent(new Event("volumechange"));
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowUp", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowUp"));
  assert.equal(player.volume, 1, "↑ 上界 100% 钳制");
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowDown", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowDown"));
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowDown", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowDown"));
  assert.equal(player.volume, 0.9, "↓ 连按两档 -10%");
  player.volume = 0.05;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowDown", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowDown"));
  assert.equal(player.volume, 0, "↓ 触底 0%");
  assert.ok(status.textContent.includes("音量 0%"), "触底播报 0%");
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowDown", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowDown"));
  assert.equal(player.volume, 0, "↓ 已触底保持 0");
  player.volume = 0.5;
  player.dispatchEvent(new Event("volumechange")); /* 0.5 成为最新记忆音量 */
  player.volume = 0; /* 引擎外置零：非音量键路径，不入记忆 */
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowUp", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowUp"));
  assert.equal(player.volume, 0.5, "↑ 自静默恢复到记忆音量");
/* 补充F：音量面 3s 无交互自动收口 + 焦点归还（空格/方向键即刻可用）。
   路径=点击钉住+焦点残留：pointerdown 打开面板、焦点置入组内滑杆。 */
  player.volume = 0.4;
  player.dispatchEvent(new Event("volumechange"));
  page.elements.playerVolume.dispatchEvent(new Event("pointerdown"));
  page.elements.playerCtrlVolume.focus();
  page.elements.playerVolume.dispatchEvent(new Event("focusin"));
  assert.equal(page.elements.playerVolume.dataset.open, "true", "点击展开音量面");
  assert.equal(document.activeElement, page.elements.playerCtrlVolume, "焦点残留在滑杆（复现前提）");
  /* 重臂验证：~200ms 时滑杆一次 input 交互，首个 300ms 到点不关 */
  page.elements.playerCtrlVolume.value = "0.45";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  await new Promise((resolve) => setTimeout(resolve, 120));
  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.equal(page.elements.playerVolume.dataset.open, "true", "交互重臂：首拍到点仍开");
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(page.elements.playerVolume.dataset.open, "false", "3s 无交互自动收口");
  assert.notEqual(document.activeElement, page.elements.playerCtrlVolume, "焦点归还（不再残留滑杆）");
  /* 空格即刻恢复播放/暂停语义 */
  const pausedBeforeSpace = player.paused;
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", " "));
  assert.equal(player.paused, !pausedBeforeSpace, "收口后空格即刻可用");

  /* 补充G：控制块焦点自动归还（全覆盖）——倍速 select 指针焦点残留
     3s 归还；键盘来源焦点（data-input=key）不抢。 */
  page.elements.playerCtrlSpeed.focus();
  document.documentElement.dataset.input = "pointer";
  /* 台架事件不冒泡：shell 的 focusin 监听需直接派发（真实浏览器由冒泡到达） */
  page.elements.playerStageShell.dispatchEvent(new Event("focusin"));
  assert.equal(document.activeElement, page.elements.playerCtrlSpeed, "select 指针焦点残留（复现前提）");
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.notEqual(document.activeElement, page.elements.playerCtrlSpeed, "3s 无交互焦点自动归还（映射 300ms）");
  /* 键盘导航态不抢：Tab 到 select 后静置不被夺焦 */
  page.elements.playerCtrlSpeed.focus();
  document.documentElement.dataset.input = "key";
  page.elements.playerStageShell.dispatchEvent(new Event("focusin"));
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(document.activeElement, page.elements.playerCtrlSpeed, "键盘来源焦点不自动归还");
  document.documentElement.dataset.input = "pointer";
  /* 归还后空格即刻可用 */
  const pausedBeforeG = player.paused;
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", " "));
  assert.equal(player.paused, !pausedBeforeG, "归还后空格即刻可用");

  /* 补充I：选定倍速（change）→ 焦点立即归还 → 左右键立即 seek
     （G 验收缺的「选定后立即按」场景） */
  player.currentTime = 100;
  player.seekable = { length: 1, end: () => 600 };
  page.elements.playerCtrlSpeed.focus();
  document.documentElement.dataset.input = "pointer";
  page.elements.playerStageShell.dispatchEvent(new Event("focusin"));
  page.elements.playerCtrlSpeed.value = "1.5";
  page.elements.playerCtrlSpeed.dispatchEvent(new Event("change"));
  assert.notEqual(document.activeElement, page.elements.playerCtrlSpeed, "选定即归还焦点");
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, 105, "选定倍速后左右键立即 seek");
  /* 时间轴拖动释放即归还（指针路径）；键盘态不归还（连续 scrub 不打断） */
  page.elements.playerCtrlTimeline.focus();
  document.documentElement.dataset.input = "pointer";
  page.elements.playerStageShell.dispatchEvent(new Event("focusin"));
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerup"));
  assert.notEqual(document.activeElement, page.elements.playerCtrlTimeline, "拖动释放即归还");
  page.elements.playerCtrlTimeline.focus();
  document.documentElement.dataset.input = "key";
  page.elements.playerStageShell.dispatchEvent(new Event("focusin"));
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("change"));
  assert.equal(document.activeElement, page.elements.playerCtrlTimeline, "键盘 scrub 不被打断");
  document.documentElement.dataset.input = "pointer";

  /* 补充E①：长按 ↑ 连续步进（keyup 裁决同款：短按一步/按住连发/松开即停/
     满格收口）。真实定时器：400ms 阈值 + 120ms 步进。 */
  player.volume = 0.3;
  player.dispatchEvent(new Event("volumechange"));
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowUp", { target: player }));
  assert.equal(player.volume, 0.35, "长按首拍=短按一步");
  await new Promise((resolve) => setTimeout(resolve, 1100)); /* 跨阈值+多轮步进（≥4 拍） */
  assert.ok(player.volume > 0.5, "按住连续步进（≥+15%）");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowUp"));
  const heldLevel = player.volume;
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(player.volume, heldLevel, "松开即停");
  player.volume = 1;
  player.dispatchEvent(new Event("volumechange"));
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowUp", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowUp"));
  assert.equal(player.volume, 1, "满格单步钳制");
  await new Promise((resolve) => setTimeout(resolve, 650)); /* 若有残留 hold：应已边界自停 */
  assert.equal(player.volume, 1, "满格收口：连续步进在 100% 自停");

  const blockedVolume = playerKeyEvent("keydown", "ArrowUp", { target: editable });
  window.dispatchEvent(blockedVolume);
  assert.equal(blockedVolume.defaultPrevented, false, "音量键编辑态让位");
  page.elements.playerSubtitleTrack.track = null; /* 不污染本函数后续场景 */
  /* P1 块的 loadedmetadata 会应用每课记忆速率（U7 正确行为，</> 已写 1.75）：
     复位供后续 seek/长按场景 */
  player.playbackRate = 1;
  page.elements.playerCtrlSpeed.value = "1";

  // seekable 钳制：右边界取 seekable 末段与时长的较小者（短按松开才 seek）
  player.currentTime = 590;
  player.seekable = { length: 1, end: () => 595 };
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, 595);
  player.seekable = { length: 0 };

  // 按住右方向超过阈值 → 临时 3 倍速（N5PR-P2：2→3 对齐 B 站）；松开恢复所选速度；全程不先跳 5 秒
  player.currentTime = 100;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  assert.equal(player.playbackRate, 1); /* 阈值前仍是所选速度 */
  assert.equal(player.currentTime, 100, "长按期间位置不动（B 站式不先跳秒）");
  await holdWait();
  assert.equal(player.playbackRate, 3);
  /* U②：3× 提示文案=「3.0×播放中」，SR 与视觉同源 */
  assert.ok(status.textContent.includes("3.0×播放中"), `3× 文案已更新：${status.textContent}`);
  /* U②：3× 提示延时 +40%（840ms）——显示 700ms 时仍在场，920ms 内自收 */
  await new Promise((resolve) => setTimeout(resolve, 700));
  assert.equal(page.elements.playerOsdHint.hidden, false, "3× 提示 700ms 仍在场（840ms 延时）");
  await new Promise((resolve) => setTimeout(resolve, 220));
  assert.equal(page.elements.playerOsdHint.hidden, true, "3× 提示 920ms 内自收");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.playbackRate, 1);
  assert.ok(status.textContent.includes("恢复 1× 播放速度"));
  assert.equal(player.currentTime, 100, "倍速收口不追加 seek");
  assert.equal(page.elements.playerCtrlSpeed.value, "1");

  // 武装中的短按因窗口失焦被丢弃：绝不隔空 seek，也不遗留 3 倍速
  player.currentTime = 200;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  window.dispatchEvent(new Event("blur"));
  assert.equal(player.currentTime, 200, "失焦丢弃未决短按");
  assert.equal(player.playbackRate, 1);

  // 按住期间窗口失焦同样恢复（keyup 丢失时不遗留 3 倍速）
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  await holdWait();
  assert.equal(player.playbackRate, 3);
  window.dispatchEvent(new Event("blur"));
  assert.equal(player.playbackRate, 1);

  // 按住期间改选速度：记录为用户所选，松开恢复成它；按住本身绝不写每课记忆
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: player }));
  await holdWait();
  page.elements.playerCtrlSpeed.value = "1.5";
  page.elements.playerCtrlSpeed.dispatchEvent(new Event("change"));
  assert.equal(player.playbackRate, 3);
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.playbackRate, 1.5);
  page.elements.playerCtrlSpeed.value = "1";
  page.elements.playerCtrlSpeed.dispatchEvent(new Event("change"));
  assert.equal(player.playbackRate, 1);

  // 速度选择与 ratechange 回写保持诚实
  page.elements.playerCtrlSpeed.value = "0.75";
  page.elements.playerCtrlSpeed.dispatchEvent(new Event("change"));
  assert.equal(player.playbackRate, 0.75);
  player.playbackRate = 1;
  player.dispatchEvent(new Event("ratechange"));
  assert.equal(page.elements.playerCtrlSpeed.value, "1");

  // 音量/静音：滑杆恒显真实音量（静音不改写显示），可达值为诚实 0-100；
  // 静音/取消静音由文本与三态图标（on/low/off）承载；点击展开音量面
  page.elements.playerCtrlVolume.value = "0.3";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  assert.equal(player.volume, 0.3);
  assert.equal(page.elements.playerVolume.dataset.open, "true"); /* 拖动中展开 */
  page.elements.playerCtrlMute.click();
  assert.equal(player.muted, true);
  player.dispatchEvent(new Event("volumechange"));
  assert.equal(page.elements.playerCtrlMute.getAttribute("aria-label"), "取消静音");
  assert.equal(page.elements.playerCtrlMute.dataset.state, "muted");
  assert.equal(page.elements.playerCtrlVolume.value, "0.3"); /* 记忆不被静音改写 */
  assert.equal(page.elements.playerCtrlVolume.getAttribute("aria-valuetext"), "30%");
  page.elements.playerCtrlMute.click();
  assert.equal(player.muted, false);
  player.dispatchEvent(new Event("volumechange"));
  assert.equal(page.elements.playerCtrlMute.dataset.state, "low"); /* 0.3 ≤ 0.5 → low 图标 */
  assert.equal(page.elements.playerCtrlVolume.value, "0.3");
  assert.equal(page.elements.playerCtrlVolume.getAttribute("aria-valuetext"), "30%");
  /* 外点收起音量面（点击钉住解除），焦点不被移动 */
  globalThis.document.dispatchEvent(new Event("pointerdown"));
  assert.equal(page.elements.playerVolume.dataset.open, "false");

  // 音量拖到零：静默态但记忆保留；再取消静音恢复 0.3
  page.elements.playerCtrlVolume.value = "0";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  assert.equal(player.volume, 0);
  player.dispatchEvent(new Event("volumechange"));
  assert.equal(page.elements.playerCtrlMute.dataset.state, "muted");
  assert.equal(page.elements.playerCtrlVolume.getAttribute("aria-valuetext"), "0%");
  page.elements.playerCtrlVolume.value = "0.3";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  assert.equal(player.muted, false); /* 非零输入解除静音 */
  page.elements.playerCtrlVolume.value = "0";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  player.dispatchEvent(new Event("volumechange"));
  page.elements.playerCtrlMute.click();
  assert.equal(player.volume, 0.3); /* 恢复记忆的非零音量 */
  player.dispatchEvent(new Event("volumechange"));
  assert.equal(page.elements.playerCtrlMute.dataset.state, "low");
  globalThis.document.dispatchEvent(new Event("pointerdown"));

  // 畸形音量输入防御：非有限值不触碰媒体音量
  player.volume = 0.4;
  page.elements.playerCtrlVolume.value = "not-a-number";
  page.elements.playerCtrlVolume.dispatchEvent(new Event("input"));
  assert.equal(player.volume, 0.4);

  // 时间轴：无拖动 input 即 seek（键盘/程序化路径）；拖动中只预览不提交
  //（SEEK-DRAG-1：慢流下逐格 seek=请求风暴，拖一下卡一路——显示层实时跟随、
  // 释放一次性提交）；timeupdate 拖动中不回写滑杆；change 收口并播报
  page.elements.playerCtrlTimeline.value = "300";
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("input"));
  assert.equal(player.currentTime, 300, "非拖动 input 即时 seek（键盘路径画面跟随不变）");
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerdown"));
  page.elements.playerCtrlTimeline.value = "420";
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("input"));
  assert.equal(player.currentTime, 300, "拖动中绝不提交 seek（SEEK-DRAG-1 慢流请求风暴根治）");
  assert.equal(page.elements.playerTimelineFill.style.getPropertyValue("--play-ratio"), "0.7",
    "拖动中填充条实时跟随预览位置");
  assert.equal(page.elements.playerTimelineBubble.hidden, false, "拖动中气泡跟随显示目标时刻");
  assert.equal(page.elements.playerTimelineBubble.textContent, "7:00", "气泡时间即拖动目标");
  player.dispatchEvent(new Event("timeupdate"));
  assert.equal(page.elements.playerCtrlTimeline.value, "420", "拖动中 timeupdate 不回写滑杆");
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerup"));
  assert.equal(player.currentTime, 420, "释放一次性提交 seek 目标");
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("change"));
  assert.ok(status.textContent.includes("已跳转到 7:00"));
  assert.equal(page.elements.playerTimelineFill.style.getPropertyValue("--play-ratio"), "0.7");

  // 非有限时长（无媒体/直播态）：诚实禁用，绝不显示 NaN/Infinity
  player.duration = NaN;
  player.dispatchEvent(new Event("durationchange"));
  assert.equal(page.elements.playerCtrlDuration.textContent, "0:00");
  assert.equal(page.elements.playerCtrlTimeline.disabled, true);
  player.duration = 600;
  player.dispatchEvent(new Event("durationchange"));

  // 恢复路径同样同步时间轴
  player.currentTime = 30;
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false);
  assert.equal(page.elements.playerCtrlElapsed.textContent, "0:30");
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.playerRecovery.hidden, true);

  // 字幕开关：与 rAF 看护共用 track.mode 状态机，按钮状态即时准确
  const textTrack = new FakeTextTrack();
  page.elements.playerSubtitleTrack.track = textTrack;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.playerCtrlSubtitle.hidden, false);
  assert.equal(page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"), "true");
  page.elements.playerCtrlSubtitle.click();
  assert.equal(textTrack.mode, "disabled");
  assert.equal(page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"), "false");
  page.elements.playerCtrlSubtitle.click();
  assert.equal(textTrack.mode, "hidden");
  assert.equal(page.elements.playerCtrlSubtitle.getAttribute("aria-pressed"), "true");

  // 画中画：支持时可进可出，失败诚实播报且不抛未处理拒绝
  assert.equal(page.elements.playerCtrlPip.hidden, false);
  let pipRequests = 0;
  player.requestPictureInPicture = () => {
    pipRequests += 1;
    doc.pictureInPictureElement = player;
    player.dispatchEvent(new Event("enterpictureinpicture"));
    return Promise.resolve();
  };
  page.elements.playerCtrlPip.click();
  assert.equal(pipRequests, 1);
  assert.equal(page.elements.playerCtrlPip.getAttribute("aria-pressed"), "true");
  doc.pictureInPictureElement = null;
  player.dispatchEvent(new Event("leavepictureinpicture"));
  assert.equal(page.elements.playerCtrlPip.getAttribute("aria-pressed"), "false");
  player.requestPictureInPicture = () => Promise.reject(new Error("synthetic pip rejection"));
  page.elements.playerCtrlPip.click();
  await settle();
  assert.ok(status.textContent.includes("拒绝了画中画请求"));

  // 影院模式：页面内放大、可逆、状态由 aria-pressed 承载
  page.elements.playerCtrlTheatre.click();
  assert.equal(page.elements.playerCtrlTheatre.getAttribute("aria-pressed"), "true");
  assert.equal(page.elements.playerCtrlTheatre.getAttribute("aria-label"), "关闭影院模式");
  assert.equal(shell.classList.values.has("theatre"), true);
  page.elements.playerCtrlTheatre.click();
  assert.equal(page.elements.playerCtrlTheatre.getAttribute("aria-pressed"), "false");
  assert.equal(shell.classList.values.has("theatre"), false);

  // 全屏：按钮手势内直接对 shell 请求；拒绝被捕获并播报；退出路径同步状态
  let fsRequests = 0;
  shell.requestFullscreen = () => {
    fsRequests += 1;
    doc.fullscreenElement = shell;
    doc.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  };
  page.elements.playerCtrlFullscreen.click();
  assert.equal(fsRequests, 1);
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"), "true");
  doc.fullscreenElement = null;
  doc.dispatchEvent(new Event("fullscreenchange"));
  shell.requestFullscreen = () => Promise.reject(new Error("synthetic fullscreen denial"));
  page.elements.playerCtrlFullscreen.click();
  await settle();
  assert.ok(status.textContent.includes("拒绝了全屏请求"));
  let exitCalls = 0;
  doc.exitFullscreen = () => {
    exitCalls += 1;
    doc.fullscreenElement = null;
    doc.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  };
  /* 先重新进入全屏（模拟用户重试成功），再验证退出路径 */
  shell.requestFullscreen = () => {
    fsRequests += 1;
    doc.fullscreenElement = shell;
    doc.dispatchEvent(new Event("fullscreenchange"));
    return Promise.resolve();
  };
  page.elements.playerCtrlFullscreen.click();
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"), "true");
  page.elements.playerCtrlFullscreen.click();
  assert.equal(exitCalls, 1);
  assert.equal(page.elements.playerCtrlFullscreen.getAttribute("aria-pressed"), "false");

  /* N6L S1 U3：直播控制台形态（时长=「直播」/时间轴禁用）随直播播放整体
     移交独立直播页（live.css+live-player），本播放器只服务回放 */

  // 讲次切换重置倍速与选择器（诚实复位，不遗留上一次的 2 倍速）
  player.playbackRate = 2;
  store.set("activeLecture", { course_id: "course-ctl", sub_id: "sub-ctl-2", sub_title: "控制讲次二", can_stream: true });
  assert.equal(player.playbackRate, 1);
  assert.equal(page.elements.playerCtrlSpeed.value, "1");

  // 清理后：快捷键、控件、媒体事件全部解除，控制台状态冻结
  const frozenControls = {
    currentTime: player.currentTime,
    elapsed: page.elements.playerCtrlElapsed.textContent,
    volumeValue: page.elements.playerCtrlVolume.value,
    theatrePressed: page.elements.playerCtrlTheatre.getAttribute("aria-pressed"),
    paused: player.paused,
  };
  cleanup();
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight"));
  window.dispatchEvent(playerKeyEvent("keydown", " "));
  page.elements.playerCtrlPlay.click();
  page.elements.playerCtrlMute.click();
  page.elements.playerCtrlTheatre.click();
  page.elements.playerCtrlTimeline.value = "30";
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("input"));
  player.dispatchEvent(new Event("volumechange"));
  player.dispatchEvent(new Event("durationchange"));
  player.dispatchEvent(new Event("ratechange"));
  assert.deepEqual({
    currentTime: player.currentTime,
    elapsed: page.elements.playerCtrlElapsed.textContent,
    volumeValue: page.elements.playerCtrlVolume.value,
    theatrePressed: page.elements.playerCtrlTheatre.getAttribute("aria-pressed"),
    paused: player.paused,
  }, frozenControls);

  // 能力缺失的独立页面：画中画按钮隐藏；requestFullscreen 缺失时诚实播报不抛错
  const page2 = createPage();
  const store2 = createStore();
  const shell2 = page2.elements.playerStageShell;
  globalThis.document.pictureInPictureEnabled = false;
  const cleanupPlayer2 = await installPlayerCore(store2);
  assert.equal(page2.elements.playerCtrlPip.hidden, true);
  store2.set("activeLecture", { course_id: "course-cap", sub_id: "sub-cap", sub_title: "能力讲次", can_stream: true });
  assert.equal(page2.elements.playerControls.hidden, false);
  assert.equal(typeof shell2.requestFullscreen !== "function", true);
  page2.elements.playerCtrlFullscreen.click();
  assert.ok(page2.elements.playerCtrlStatus.textContent.includes("不支持全屏"));
  cleanupPlayer2();
}

// ---- N5PR-P2：每课倍速记忆（select 与 </> 双通道写入；讲次重载恢复；
// ---- 畸形值回退 1x；直播态恢复钳制到 [1, 追帧上限]） ----
async function verifyPlaybackRateMemoryBehavior() {
  localStorageStub.reset();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const speed = page.elements.playerCtrlSpeed;
  const cleanup = await installPlayerCore(store);
  store.set("activeCourse", { course_id: "course-mem" });
  store.set("activeLecture", { course_id: "course-mem", sub_id: "sub-mem", sub_title: "记忆讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.playbackRate, 1, "无记忆=1x");

  speed.value = "1.5";
  speed.dispatchEvent(new Event("change"));
  assert.equal(player.playbackRate, 1.5);
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(player.playbackRate, 1.75);
  const stored = JSON.parse(localStorageStub.getItem("courselens.playback-rate.v1"));
  assert.equal(stored["course-mem"], 1.75, "显式选速落每课记忆（select+</> 双通道）");

  /* 讲次重载：loadLecture 复位 1x，元数据就绪后恢复记忆 */
  store.set("activeLecture", { course_id: "course-mem", sub_id: "sub-mem2", sub_title: "同课第二讲", can_stream: true });
  assert.equal(player.playbackRate, 1, "loadLecture 先复位 1x");
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.playbackRate, 1.75, "同课新讲次恢复记忆速率");
  assert.equal(speed.value, "1.75", "select 同步记忆档");

  /* 畸形持久值：静默回退 1x */
  localStorageStub.setItem("courselens.playback-rate.v1", JSON.stringify({ "course-mem": "abc" }));
  store.set("activeLecture", { course_id: "course-mem", sub_id: "sub-mem3", sub_title: "畸形记忆讲次", can_stream: true });
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.playbackRate, 1, "畸形记忆值回退 1x");
  /* N6L S1 U3：直播态记忆速率钳制随直播播放移交独立直播页（live-player） */
  cleanup();
  localStorageStub.reset();
}

// ---- N5PR-P3：操作反馈 OSD（中央 pulse / 轻提示双通道 / 滚轮音量） ----
async function verifyPlayerOsdBehavior() {
  localStorageStub.reset();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const shell = page.elements.playerStageShell;
  const status = page.elements.playerCtrlStatus;
  const center = page.elements.playerOsdCenter;
  const hint = page.elements.playerOsdHint;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-osd", sub_id: "sub-osd", sub_title: "OSD 讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));

  // 中央 pulse：Space（键盘）触发；播放/暂停方向正确；按钮点击不触发
  player.pause();
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  assert.equal(player.paused, false);
  assert.equal(center.hidden, false, "Space 后中央 pulse 可见");
  assert.equal(center.dataset.state, "play", "pulse 方向=播放");
  await osdHideWait();
  assert.equal(center.hidden, true, "pulse 600ms 自收");
  page.elements.playerCtrlPlay.click();
  assert.equal(center.hidden, true, "按钮点击不叠加中央 pulse");

  // 连击重置计时：两次 Space 间隔 <600ms，第二次后仍需整段 600ms 才收
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  await new Promise((resolve) => setTimeout(resolve, 300));
  window.dispatchEvent(playerKeyEvent("keydown", " ", { target: player }));
  assert.equal(center.dataset.state, "pause", "pulse 方向=暂停");
  await new Promise((resolve) => setTimeout(resolve, 450));
  assert.equal(center.hidden, false, "连击重置计时（<600ms 未收）");
  await osdHideWait();
  assert.equal(center.hidden, true, "重置后到点自收");

  // 轻提示：一处文案双通道（SR 状态区与视觉 hint 同源同文本）
  player.playbackRate = 1;
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(hint.hidden, false, "倍速变化轻提示可见");
  assert.equal(hint.textContent, "播放速度 1.25×");
  assert.equal(status.textContent, hint.textContent, "SR 与视觉同源（单源双通道）");
  await osdHideWait();
  assert.equal(hint.hidden, true, "轻提示自收");

  // 滚轮音量：悬停 shell 滚轮 ±5%；volumechange 链同步；hint 百分比
  player.volume = 0.5;
  player.dispatchEvent(new Event("volumechange")); /* 0.5 入记忆 */
  shell.dispatchEvent(wheelEvent(-120, { target: shell }));
  assert.equal(player.volume, 0.55, "滚轮上 +5%");
  assert.equal(hint.hidden, false);
  assert.ok(hint.textContent.includes("音量 55%"), "hint 显示百分比");
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  assert.equal(player.volume, 0.5, "滚轮下 -5%");
  shell.dispatchEvent(wheelEvent(0, { target: shell }));
  assert.equal(player.volume, 0.5, "deltaY=0 不动");
  shell.dispatchEvent(wheelEvent(Number.NaN, { target: shell }));
  assert.equal(player.volume, 0.5, "畸形 deltaY 防御");

  // 触底不改写记忆；滚轮上自静默恢复
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  assert.equal(player.volume, 0.25, "-5×5 = 0.25");
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  shell.dispatchEvent(wheelEvent(120, { target: shell }));
  assert.equal(player.volume, 0, "滚轮触底 0%");
  shell.dispatchEvent(wheelEvent(-120, { target: shell }));
  assert.equal(player.volume, 0.5, "滚轮上自静默恢复记忆音量（非 0.05）");

  // editable 让位：焦点在滑杆（input）上滚轮不抢
  const sliderTarget = new FakeElement("wheel-slider");
  sliderTarget.tagName = "input";
  player.volume = 0.3;
  shell.dispatchEvent(wheelEvent(-120, { target: sliderTarget }));
  assert.equal(player.volume, 0.3, "input 焦点上滚轮让位");

  // 讲次切换确定性复位：OSD 立即隐藏、计时器清零
  window.dispatchEvent(playerKeyEvent("keydown", ">", { target: player }));
  assert.equal(hint.hidden, false);
  store.set("activeLecture", { course_id: "course-osd", sub_id: "sub-osd2", sub_title: "切换讲次", can_stream: true });
  assert.equal(hint.hidden, true, "loadLecture 复位轻提示");
  assert.equal(center.hidden, true, "loadLecture 复位 pulse");
  cleanup();
  localStorageStub.reset();
}

// ---- N5PR-P4：进度条质感与缓冲可视化（hover 粗化/时间气泡/中央缓冲指示） ----
async function verifyPlayerBufferVisualBehavior() {
  localStorageStub.reset();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const shell = page.elements.playerStageShell;
  const timelineWrap = page.elements.playerTimeline;
  const bubble = page.elements.playerTimelineBubble;
  const spinner = page.elements.playerStageSpinner;
  const recovery = page.elements.playerRecovery;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-buf", sub_id: "sub-buf", sub_title: "缓冲讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));

  // 防闪窗：waiting 起点立即隐藏；>300ms 才显示；canplay 即收
  player.dispatchEvent(new Event("waiting"));
  assert.equal(spinner.hidden, true, "300ms 防闪窗内不亮");
  await new Promise((resolve) => setTimeout(resolve, 360));
  assert.equal(spinner.hidden, false, "waiting >300ms 中央缓冲指示出现");
  player.dispatchEvent(new Event("canplay"));
  assert.equal(spinner.hidden, true, "canplay 立即收口");
  // 防闪窗内提前恢复：计时器被取消，之后也不再亮
  player.dispatchEvent(new Event("waiting"));
  await new Promise((resolve) => setTimeout(resolve, 120));
  player.dispatchEvent(new Event("canplay"));
  await new Promise((resolve) => setTimeout(resolve, 260));
  assert.equal(spinner.hidden, true, "窗内恢复即取消，不再出现");
  // 恢复面板互斥：面板可见时不叠加
  recovery.hidden = false;
  player.dispatchEvent(new Event("waiting"));
  await new Promise((resolve) => setTimeout(resolve, 360));
  assert.equal(spinner.hidden, true, "恢复面板可见时缓冲指示互斥");
  recovery.hidden = true;

  // 时间气泡：rAF 合帧、文本=格式化目标时刻、几何变量、绝不写 currentTime
  player.currentTime = 60;
  timelineWrap.dispatchEvent(pointerMoveEvent(250));
  assert.equal(player.currentTime, 60, "气泡绝不写 currentTime");
  await settle();
  assert.equal(bubble.hidden, false, "悬停气泡可见");
  assert.equal(bubble.textContent, "2:30", "文本=0.25×600s 的格式化时刻");
  assert.equal(bubble.style.getPropertyValue("--bubble-ratio"), "0.25", "几何变量写入");
  timelineWrap.dispatchEvent(pointerMoveEvent(1250));
  await settle();
  assert.equal(bubble.textContent, "10:00", "指针越界钳到片尾时刻");
  timelineWrap.dispatchEvent(new Event("pointerleave"));
  assert.equal(bubble.hidden, true, "pointerleave 收口气泡");
  // 拖动态标记（粗化挂点）：pointerdown/up 翻转 data-scrubbing
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerdown"));
  assert.equal(timelineWrap.dataset.scrubbing, "true", "拖动开始置粗化态");
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerup"));
  assert.equal(timelineWrap.dataset.scrubbing, "false", "拖动结束还原");
  /* N6L S1 U3：直播态气泡退避随直播播放移交独立直播页（本播放器仅回放，
     时间气泡对不可 seek 源的退避逻辑不变） */
  cleanup();
  localStorageStub.reset();
}

// ---- D8：字幕生成中 chip（store tasks 订阅；讲次匹配；诚实无进度；点击开抽屉） ----
async function verifySubtitleChipBehavior() {
  localStorageStub.reset();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const chip = page.elements.playerSubtitleChip;
  const cleanup = await installPlayerCore(store);
  const openRequests = [];
  const captureOpen = (event) => openRequests.push(event.detail?.trigger?.id || "");
  window.addEventListener("courselens:open-tasks", captureOpen);
  store.set("activeLecture", { course_id: "course-chip", sub_id: "sub-chip", sub_title: "chip 讲次", can_stream: true });
  assert.equal(chip.hidden, true, "无任务时 chip 隐藏");

  /* SWEEPFIX-R2 W6b（SWEEPFIX-3 跨域移交单）：夹具改 public_task 顶层合同
     真实形状（completed/total 平铺、零嵌套 progress 对象）——旧夹具嵌套假形状
     与 ：584 死路读法互相抵消才假绿（T6 同款构建者盲区），生产 chip 恒无百分比。 */
  store.set("tasks", [
    { kind: "subtitle", state: "running", sub_id: "sub-chip", completed: 31, total: 100 },
  ]);
  assert.equal(chip.hidden, false, "活动字幕任务 chip 可见");
  assert.equal(chip.textContent, "字幕生成中 31%", "可靠进度带百分比（public_task 顶层合同键）");
  chip.click();
  assert.deepEqual(openRequests, ["player-subtitle-chip"], "chip 点击开任务抽屉");

  store.set("tasks", [{ kind: "subtitle", state: "running", sub_id: "sub-chip" }]);
  assert.equal(chip.textContent, "字幕生成中", "无可靠进度=诚实无百分比");
  store.set("tasks", [{ kind: "subtitle", state: "completed", sub_id: "sub-chip", completed: 100, total: 100 }]);
  assert.equal(chip.hidden, true, "完成态 chip 收起");
  store.set("tasks", [{ kind: "subtitle", state: "running", sub_id: "sub-other", completed: 1, total: 2 }]);
  assert.equal(chip.hidden, true, "其他讲次的任务不亮本讲 chip");
  window.removeEventListener("courselens:open-tasks", captureOpen);
  cleanup();
  localStorageStub.reset();
}

// ---- D2：一键「没听懂」（X 键+按钮；5s 去重；标记点跳回；讲次切换清空）
// ---- + U⑤：悬停两小钮（解释=书签解释链预算闸复用/懂了=resolve 收起；
// ---- 接口形状不符预期 → 降级仅「懂了」）。去重窗靠切讲次重置，零真实等待。
async function verifyNotUnderstoodBehavior() {
  localStorageStub.reset();
  bookmarkPostCalls = 0;
  bookmarkPostBodies.length = 0;
  bookmarkPostFailure = "";
  bookmarkPostHold = false;
  bookmarkPostHolds.length = 0;
  bookmarkDeleteBodies.length = 0;
  explainMode = "ok";
  explainPostBodies.length = 0;
  resolvePostBodies.length = 0;
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const flags = page.elements.playerTimelineFlags;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x", sub_title: "标记讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.playerCtrlBookmark.hidden, false, "讲次态标记按钮可见");
  player.currentTime = 60;

  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(bookmarkPostCalls, 1, "X 键发起标记");
  assert.equal(bookmarkPostBodies[0].note, "没听懂");
  assert.equal(bookmarkPostBodies[0].sub_id, "sub-x");
  assert.equal(bookmarkPostBodies[0].start_ms, 60000, "当前时刻毫秒落库");
  assert.equal(flags.children.length, 1, "时间轴标记点已渲染");
  assert.equal(flags.children[0].style.getPropertyValue("--chapter-ratio"), "0.1", "标记点几何=60s/600s");

  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(bookmarkPostCalls, 1, "5s 窗口内重复标记被去重");
  assert.equal(flags.children.length, 1, "去重不新增标记点");

  /* U⑤：悬停三小钮在场（PLAYER-UX-1④ 增删除） */
  const marker1 = flags.children[0];
  const menu1 = marker1.querySelector(".player-flag-menu");
  assert.notEqual(menu1, null, "悬停菜单在场");
  assert.deepEqual(
    [...menu1.querySelectorAll(".player-flag-action")].map((node) => node.textContent),
    ["解释", "懂了", "删除"],
    "三小钮=解释/懂了/删除",
  );

  player.currentTime = 30;
  marker1.querySelector(".player-flag-dot").click();
  assert.equal(player.currentTime, 60, "点击标记点跳回该时刻");

  /* SWEEPFIX-1 C2（化身走查 SWEEP1-C2）：跳回续播的 play() 竞态消音——
     play() 被 pause() 打断（AbortError「interrupted by a call to pause()」族）
     不再产生未捕获拒绝；其余拒绝（NotAllowedError 等）维持既有未处理语义，
     消音面恰为竞态族（零行为变化）。修前红：裸抛 play() 时第一断言计 1。 */
  {
    const dotNode = marker1.querySelector(".player-flag-dot");
    const originalPlay = player.play;
    let unhandledCount = 0;
    const onUnhandled = () => { unhandledCount += 1; };
    process.on("unhandledRejection", onUnhandled);
    try {
      player.play = () => Promise.reject(
        Object.assign(new Error("The play() request was interrupted by a call to pause()"), { name: "AbortError" }),
      );
      dotNode.click();
      for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
      assert.equal(unhandledCount, 0, "play() 被暂停打断的竞态零未捕获拒绝（C2 消音）");
      player.play = () => Promise.reject(
        Object.assign(new Error("play() can only be initiated by a user gesture"), { name: "NotAllowedError" }),
      );
      dotNode.click();
      for (let index = 0; index < 4; index += 1) await new Promise((resolve) => setImmediate(resolve));
      assert.equal(unhandledCount, 1, "非 AbortError 拒绝维持未处理语义（消音面闭集）");
    } finally {
      player.play = originalPlay;
      process.off("unhandledRejection", onUnhandled);
    }
  }

  /* U⑤：懂了 → resolve 动作后标记从热度带收起（数据本机保留；桩 remove=hidden） */
  menu1.querySelectorAll(".player-flag-action")[1].click();
  await settle();
  assert.deepEqual(resolvePostBodies[0], { bookmark_id: "synthetic-1", action: "resolve" }, "懂了走 resolve 动作");
  assert.equal(marker1.hidden, true, "懂了后标记从时间轴收起");

  /* U⑤：解释走既有书签解释链；预算闸 declined 形状 → 额度人话、不收起标记 */
  await new Promise((resolve) => setTimeout(resolve, 5100)); /* 越过 X 去重窗 */
  page.elements.playerCtrlBookmark.click(); /* 第 2 枚标记 */
  await settle();
  const marker2 = flags.children[1];
  explainMode = "declined";
  marker2.querySelector(".player-flag-action").click();
  await settle();
  assert.equal(explainPostBodies[0].bookmark_id, "synthetic-2", "解释走既有书签解释链");
  assert.equal(marker2.hidden, false, "declined 不收起标记");
  /* 车道C卡9实锤修复：declined=资料不足（额度门已撤），文案引导生成本讲字幕 */
  /* 假 DOM 的 textContent 不聚合子节点：读最后一条 toast 子节点 */
  const toastNodes = page.elements.toastRegion.children;
  const declinedToastText = String(toastNodes[toastNodes.length - 1]?.textContent || "");
  assert.ok(declinedToastText.includes("缺少可依据的字幕内容"), "declined 文案如实说缺字幕依据");
  assert.ok(!declinedToastText.includes("额度"), "declined 文案不再提已移除的额度门");

  /* U⑤：接口形状不符预期 → 降级，该标记菜单撤下 */
  explainMode = "broken";
  marker2.querySelector(".player-flag-action").click();
  await settle();
  const menu2 = marker2.querySelector(".player-flag-menu");
  assert.equal(menu2 === null || menu2.hidden === true, true, "降级后该标记菜单已撤");

  /* 降级后新标记仅出「懂了」（切讲次重置去重窗，零真实等待） */
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x2", sub_title: "下一讲", can_stream: true });
  assert.equal(flags.children.length, 0, "讲次切换清空标记层");
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(bookmarkPostCalls, 3, "切讲次重置去重窗口");
  const menu3 = flags.children[0].querySelector(".player-flag-menu");
  assert.deepEqual(
    [...menu3.querySelectorAll(".player-flag-action")].map((node) => node.textContent),
    ["懂了", "删除"],
    "降级后新标记出「懂了/删除」",
  );
  explainMode = "ok";

  /* PLAYER-UX-1④：菜单「删除」走 DELETE /api/v3/bookmarks，成功后标记即时移除。
     切讲次重置去重窗（零真实等待，守住 pytest 包装器 30s 上限） */
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x4", sub_title: "删除讲次", can_stream: true });
  assert.equal(flags.children.length, 0, "切讲次清空标记层");
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(bookmarkPostCalls, 4, "切讲次重置去重窗口后第 4 枚");
  bookmarkDeleteBodies.length = 0;
  const marker4 = flags.children[0];
  assert.equal(String(marker4.dataset.bookmarkId || ""), "synthetic-4", "标记携带书签身份");
  const menu4 = marker4.querySelector(".player-flag-menu");
  const removeAction4 = [...menu4.querySelectorAll(".player-flag-action")]
    .find((node) => node.textContent === "删除");
  assert.notEqual(removeAction4, null, "菜单含删除钮（解释降级闸粘性不影响删除）");
  removeAction4.click(); /* 删除 */
  await settle();
  assert.deepEqual(
    bookmarkDeleteBodies,
    [{ bookmark_id: "synthetic-4" }],
    "删除走 DELETE /api/v3/bookmarks",
  );
  assert.equal(marker4.hidden, true, "删除后标记从时间轴即时消失");

  const editableField2 = new FakeElement("editable-2");
  editableField2.closest = () => editableField2; /* 模拟焦点在可交互元素内 */
  const blockedX = playerKeyEvent("keydown", "x", { target: editableField2 });
  window.dispatchEvent(blockedX);
  assert.equal(blockedX.defaultPrevented, false, "X 编辑态让位");

  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x3", sub_title: "再下一讲", can_stream: true });
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(bookmarkPostCalls, 5, "切讲次后再标记正常发起（PLAYER-UX-1④ 块已消费第 4 枚）");

  /* F8a（化身走查 20261008）：点击无即时确认反馈——三钉=①点击当下旗标即落
     时间轴（乐观渲染，不等服务端）②在途再点不重复发请求给人话反馈
     ③服务端拒绝即撤旗不留假成功（缺依据分码人话）。切讲次重置去重窗。 */
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x6", sub_title: "即时反馈讲次", can_stream: true });
  bookmarkPostHold = true;
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  assert.equal(bookmarkPostCalls, 6, "在途挂起时标记请求已发出");
  assert.equal(flags.children.length, 1, "点击当下旗标即落时间轴（不等服务端返回）");
  assert.equal(flags.children[0].querySelector(".player-flag-menu"), null, "服务端确认前不挂操作菜单");
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  assert.equal(bookmarkPostCalls, 6, "在途再点不重复发请求");
  const inflightToastText = String(
    page.elements.toastRegion.children[page.elements.toastRegion.children.length - 1]?.textContent || "",
  );
  assert.ok(inflightToastText.includes("正在记"), "在途再点给即时人话反馈");
  bookmarkPostHold = false;
  for (const pending of bookmarkPostHolds.splice(0)) {
    pending.resolve(ok({ bookmark: { bookmark_id: `synthetic-${bookmarkPostCalls}`, note: "没听懂" } }, 201));
  }
  await settle();
  assert.equal(String(flags.children[0].dataset.bookmarkId || ""), "synthetic-6", "确认后旗标补挂书签身份");
  assert.notEqual(flags.children[0].querySelector(".player-flag-menu"), null, "确认后操作菜单挂上");

  /* F8a：缺依据分码（bookmark_evidence_unavailable）→ 撤旗+人话引导先生成字幕 */
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x7", sub_title: "缺依据讲次", can_stream: true });
  bookmarkPostFailure = "evidence";
  page.elements.playerCtrlBookmark.click();
  assert.equal(flags.children.length, 1, "失败路径同样点击当下落旗（反馈一致性）");
  await settle();
  /* 假 DOM remove()=hidden（桩语义），撤旗断言用 hidden */
  assert.equal(flags.children[0].hidden, true, "服务端拒绝后撤旗不留假成功");
  const evidenceToastText = String(
    page.elements.toastRegion.children[page.elements.toastRegion.children.length - 1]?.textContent || "",
  );
  assert.ok(evidenceToastText.includes("先生成字幕"), "缺依据分码人话引导先生成字幕");
  bookmarkPostFailure = "";

  /* F8a：未知失败（raw 5xx）→ 同样撤旗回滚 */
  store.set("activeLecture", { course_id: "course-x", sub_id: "sub-x8", sub_title: "未知失败讲次", can_stream: true });
  bookmarkPostFailure = "raw";
  window.dispatchEvent(playerKeyEvent("keydown", "x", { target: player }));
  await settle();
  assert.equal(flags.children[0].hidden, true, "未知失败同样撤旗回滚");
  bookmarkPostFailure = "";
  cleanup();
  localStorageStub.reset();
}

// ---- D3：字幕样式面板（长按入口；3/4/3 闭集；变量应用链；畸形回退） ----
async function verifySubtitleStyleBehavior() {
  localStorageStub.reset();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const overlay = page.elements.playerSubtitleOverlay;
  const subtitleButton = page.elements.playerCtrlSubtitle;
  const dialog = page.elements.playerSubtitleStyleDialog;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-st", sub_id: "sub-st", sub_title: "样式讲次", can_stream: true });
  player.duration = 600;
  page.elements.playerSubtitleTrack.track = new FakeTextTrack(); /* 长按守卫要求字幕轨在 */
  player.dispatchEvent(new Event("loadedmetadata"));

  /* 安装默认：缺省档=改版前现状（22px/0.85/104px 兜底） */
  assert.equal(overlay.style.getPropertyValue("--sub-font-size"), "22px", "默认字号 md=22px");
  assert.equal(overlay.style.getPropertyValue("--sub-bg-alpha"), "0.85", "默认背景 85%");

  /* 长按入口：400ms 阈值；短点按不弹面板；长按后首次 click 被吞（不切字幕） */
  subtitleButton.dispatchEvent(new Event("pointerdown"));
  await new Promise((resolve) => setTimeout(resolve, 450));
  assert.equal(dialog.open, true, "长按 400ms 打开样式面板");
  subtitleButton.dispatchEvent(new Event("pointerup"));
  subtitleButton.click();
  assert.equal(subtitleButton.getAttribute("aria-pressed"), "true", "面板打开后的 click 不切换字幕");
  dialog.close();
  subtitleButton.dispatchEvent(new Event("pointerdown"));
  subtitleButton.dispatchEvent(new Event("pointerup"));
  subtitleButton.click();
  assert.equal(subtitleButton.getAttribute("aria-pressed"), "false", "短点按仍切换字幕");
  subtitleButton.click();

  /* 事件委托选档：点击 size=lg → 变量写入 + localStorage 持久化 */
  const sizeGroup = new FakeElement("style-group-size");
  sizeGroup.classList.values.add("subtitle-style-group");
  sizeGroup.dataset.styleKey = "size";
  const lgButton = new FakeElement("style-lg");
  lgButton.dataset.styleValue = "lg";
  lgButton.parent = sizeGroup;
  const clickEvent = new Event("click", { bubbles: true });
  Object.defineProperty(clickEvent, "target", { value: lgButton });
  dialog.dispatchEvent(clickEvent);
  assert.equal(overlay.style.getPropertyValue("--sub-font-size"), "26px", "选大字号=26px");
  const stored = JSON.parse(localStorageStub.getItem("courselens:subtitle-style"));
  assert.equal(stored.size, "lg", "选档持久化到本机");

  /* opacity/offset 档位抽样 */
  const opacityGroup = new FakeElement("style-group-opacity");
  opacityGroup.classList.values.add("subtitle-style-group");
  opacityGroup.dataset.styleKey = "opacity";
  const dimButton = new FakeElement("style-25");
  dimButton.dataset.styleValue = "25";
  dimButton.parent = opacityGroup;
  const clickOpacity = new Event("click", { bubbles: true });
  Object.defineProperty(clickOpacity, "target", { value: dimButton });
  dialog.dispatchEvent(clickOpacity);
  assert.equal(overlay.style.getPropertyValue("--sub-bg-alpha"), "0.25", "背景 25% 档");
  const offsetGroup = new FakeElement("style-group-offset");
  offsetGroup.classList.values.add("subtitle-style-group");
  offsetGroup.dataset.styleKey = "offset";
  const highButton = new FakeElement("style-high");
  highButton.dataset.styleValue = "high";
  highButton.parent = offsetGroup;
  const clickOffset = new Event("click", { bubbles: true });
  Object.defineProperty(clickOffset, "target", { value: highButton });
  dialog.dispatchEvent(clickOffset);
  /* 甲4：三变量已接入 CSS（components.css 消费 --sub-bottom）；高档=8%（低档=0%=既有默认视觉） */
  assert.equal(overlay.style.getPropertyValue("--sub-bottom"), "8%", "贴底高档=8%");

  /* 夜批10-B 第七遍（用户试用反馈）：字号下扩 xs=14px；贴底下扩 bottom=-104px
     （恰抵消 components.css 安全区常量 104px=真触底）。旧值兼容：xs/bottom 之外的
     既有档位值与新默认全部不变。 */
  const xsButton = new FakeElement("style-xs");
  xsButton.dataset.styleValue = "xs";
  xsButton.parent = sizeGroup;
  const clickXs = new Event("click", { bubbles: true });
  Object.defineProperty(clickXs, "target", { value: xsButton });
  dialog.dispatchEvent(clickXs);
  assert.equal(overlay.style.getPropertyValue("--sub-font-size"), "14px", "特小字号 xs=14px");
  assert.equal(JSON.parse(localStorageStub.getItem("courselens:subtitle-style")).size, "xs", "xs 档持久化");
  const bottomButton = new FakeElement("style-bottom");
  bottomButton.dataset.styleValue = "bottom";
  bottomButton.parent = offsetGroup;
  const clickBottom = new Event("click", { bubbles: true });
  Object.defineProperty(clickBottom, "target", { value: bottomButton });
  dialog.dispatchEvent(clickBottom);
  /* N10B-SUP-1：贴边档=--sub-bottom 归零 + data-sub-edge 标记（分态 bottom 由
     CSS 规则给：visible=甲板上方/hidden=壳底缘；负偏移按 visible 校准会把
     hidden 态推出壳底缘 -84px）。 */
  assert.equal(overlay.style.getPropertyValue("--sub-bottom"), "0%", "贴边档面板偏移归零");
  assert.equal(overlay.getAttribute("data-sub-edge"), "1", "贴边档标记 data-sub-edge=1");
  assert.equal(JSON.parse(localStorageStub.getItem("courselens:subtitle-style")).offset, "bottom", "bottom 档持久化");
  /* 非贴边档切回：标记移除（回退加算公式） */
  dialog.dispatchEvent(clickOffset);
  assert.equal(overlay.getAttribute("data-sub-edge"), null, "切回高档移除贴边标记");

  /* 畸形持久值整体回退默认（重新安装=真实应用路径） */
  cleanup();
  localStorageStub.setItem("courselens:subtitle-style", "{broken json");
  const pageB = createPage();
  store.tasks = [];
  const cleanupB = await installPlayerCore(store);
  assert.equal(pageB.elements.playerSubtitleOverlay.style.getPropertyValue("--sub-font-size"), "22px", "畸形持久值回退默认 md");
  assert.equal(pageB.elements.playerSubtitleOverlay.style.getPropertyValue("--sub-bg-alpha"), "0.85", "畸形持久值背景回退默认");
  cleanupB();

  /* 旧值兼容：扩展前的持久值（sm/low）在新闭集内原样保留，不炸不回退 */
  localStorageStub.setItem("courselens:subtitle-style", JSON.stringify({ size: "sm", opacity: "50", offset: "low" }));
  const pageC = createPage();
  store.tasks = [];
  const cleanupC = await installPlayerCore(store);
  assert.equal(pageC.elements.playerSubtitleOverlay.style.getPropertyValue("--sub-font-size"), "18px", "旧值 sm 原样生效 18px");
  assert.equal(pageC.elements.playerSubtitleOverlay.style.getPropertyValue("--sub-bottom"), "0%", "旧值 low 原样生效 0%");
  cleanupC();
  localStorageStub.reset();
}

// ---- D5：智能时间轴标签轨（默认三类；点击跳段首；可跳播报；切换清空） ----
async function verifyTimelineLabelsBehavior() {
  localStorageStub.reset();
  timelineGetRows.clear();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const labels = page.elements.playerTimelineLabels;
  const status = page.elements.playerCtrlStatus;
  const cleanup = await installPlayerCore(store);
  timelineGetRows.set("sub-tl", [
    { label: "exam", start_ms: 60000, end_ms: 90000, confidence: 0.9, evidence: { matched_terms: ["考点"] } },
    { label: "homework", start_ms: 120000, end_ms: 150000, confidence: 0.8, evidence: {} },
    { label: "chat", start_ms: 180000, end_ms: 240000, confidence: 0.7, evidence: {} },
    { label: "knowledge", start_ms: 300000, end_ms: 360000, confidence: 0.6, evidence: {} },
    { label: "roll_call", start_ms: 400000, end_ms: 420000, confidence: 0.7, evidence: {} },
  ]);
  store.set("activeLecture", { course_id: "course-tl", sub_id: "sub-tl", sub_title: "标签讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  await settle();
  await settle();

  assert.equal(labels.children.length, 3, "默认仅三类段渲染（chat/knowledge 不上轨）");
  const examSegment = labels.children[0];
  assert.equal(examSegment.style.getPropertyValue("--seg-from"), "0.1", "exam 段起点几何");
  assert.ok(String(examSegment.title).includes("考点"), "悬停标题=标签名+秒数");
  player.currentTime = 10;
  examSegment.click();
  assert.equal(player.currentTime, 60, "点击 exam 段跳到段首");
  assert.ok(status.textContent.includes("闲聊和事务"), "可跳段合计满 1 分钟做人话播报（chat=60s）");

  /* 讲次切换清空；无数据讲次=诚实无轨 */
  store.set("activeLecture", { course_id: "course-tl", sub_id: "sub-tl2", sub_title: "下一讲", can_stream: true });
  player.dispatchEvent(new Event("loadedmetadata"));
  await settle();
  assert.equal(labels.children.length, 0, "切换后旧标签清空（新讲次无分类数据）");
  cleanup();
  localStorageStub.reset();
  timelineGetRows.clear();
}

// ---- D6：考核标记三源一轨（exam 带 + quiz/bookmark 刻度 ≤8；点击跳点；稀疏退化） ----
async function verifyAssessMarkersBehavior() {
  localStorageStub.reset();
  timelineGetRows.clear();
  quizzesGetRows.clear();
  bookmarksGetRows.clear();
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const assess = page.elements.playerTimelineAssess;
  const cleanup = await installPlayerCore(store);
  timelineGetRows.set("sub-as", [
    { label: "exam", start_ms: 60000, end_ms: 120000, confidence: 0.9, evidence: {} },
  ]);
  /* 十处 quiz-hard 集中在前 3/8 区间 → 八桶聚合后刻度 ≤8；另有 bookmark 一处 */
  quizzesGetRows.set("sub-as", [
    { difficulty: "hard", start_ms: 10000 }, { difficulty: "hard", start_ms: 20000 },
    { difficulty: "hard", start_ms: 30000 }, { difficulty: "hard", start_ms: 40000 },
    { difficulty: "hard", start_ms: 50000 }, { difficulty: "hard", start_ms: 60000 },
    { difficulty: "hard", start_ms: 70000 }, { difficulty: "hard", start_ms: 80000 },
    { difficulty: "hard", start_ms: 90000 }, { difficulty: "hard", start_ms: 100000 },
    { difficulty: "easy", start_ms: 110000 },
  ]);
  bookmarksGetRows.set("sub-as", [
    { start_ms: 300000, note: "没听懂" },
  ]);
  store.set("activeLecture", { course_id: "course-as", sub_id: "sub-as", sub_title: "考核讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  await settle();
  await settle();

  const bands = assess.children.filter((node) => node.className === "player-exam-band");
  const ticks = assess.children.filter((node) => String(node.className).includes("player-assess-tick"));
  assert.equal(bands.length, 1, "exam 段渲染微高亮带");
  assert.ok(ticks.length <= 8, "刻度八桶聚合 ≤8");
  assert.equal(ticks.length, 3, "十处 quiz-hard 聚成 2 桶 + bookmark 1 桶 = 3 枚");
  const bookmarkTick = ticks.find((node) => String(node.className).includes("player-assess-bookmark"));
  assert.ok(bookmarkTick, "书签源刻度存在");
  player.currentTime = 0;
  bookmarkTick.click();
  assert.equal(player.currentTime, 300, "点击书签刻度跳到该难点");

  /* 稀疏退化：无任何三源数据的讲次=零标记不报错 */
  store.set("activeLecture", { course_id: "course-as", sub_id: "sub-as2", sub_title: "干净讲次", can_stream: true });
  player.dispatchEvent(new Event("loadedmetadata"));
  await settle();
  await settle();
  assert.equal(assess.children.length, 0, "稀疏讲次零标记");
  cleanup();
  localStorageStub.reset();
  timelineGetRows.clear();
  quizzesGetRows.clear();
  bookmarksGetRows.clear();
}

// ---- D7/AS5：学习洞察（默认开；显式 off 被尊重；两击臂只抹本讲；占位行沉默） ----
async function verifyInsightBehavior() {
  localStorageStub.reset();
  watchEventsRows.clear();
  watchEventsGetCalls = 0;
  watchEventsPostBodies = [];
  watchEventsClearCalls = 0;
  watchEventsClearBodies = [];
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const heat = page.elements.playerTimelineHeat;
  const insightErase = page.elements.insightErase;
  const taskRow = page.elements.playerTaskState;
  const taskText = page.elements.playerTaskText;
  const cleanup = await installPlayerCore(store);
  watchEventsRows.set("sub-in", [
    { event: "seek_back", position_ms: 60000, playback_rate: 1, occurred_at: 1 },
    { event: "replay", position_ms: 60000, playback_rate: 1, occurred_at: 2 },
    { event: "pause", position_ms: 120000, playback_rate: 1, occurred_at: 3 },
  ]);
  store.set("activeLecture", { course_id: "course-in", sub_id: "sub-in", sub_title: "洞察讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.playerInsightOpen.hidden, false, "讲次态洞察入口可见");
  await settle();

  /* AS5 拍板：默认开=加载即拉本讲热点并渲染；抹除钮藏在讲次操作区 */
  assert.equal(watchEventsGetCalls, 1, "默认开：加载讲次即拉取热点");
  assert.equal(heat.children.length, 1, "仅回看类信号渲染热度段（pause-only 桶不上）");
  assert.ok(String(heat.children[0].title).includes("回看了 2 次"), "悬停文案讲人话（seek_back+replay）");
  assert.equal(insightErase.hidden, false, "抹除钮在讲次操作区可见");
  assert.equal(insightErase.textContent, "抹掉本讲热点", "抹除钮默认文案讲清范围");

  /* F1 钉：任务占位行不常驻——加载后保持沉默，无技术腔文案 */
  assert.equal(taskRow.hidden, true, "讲次加载不亮任务占位行");
  assert.equal(taskText.textContent, "", "状态未明不产出占位文案");

  /* 采集照旧：向回跳 >2s 记 seek_back；暂停触发收口落库 */
  player.currentTime = 200;
  player.dispatchEvent(new Event("timeupdate"));
  player.currentTime = 100;
  player.dispatchEvent(new Event("pause"));
  await settle();
  assert.equal(watchEventsPostBodies.length, 1, "暂停收口触发批落库");
  const events = watchEventsPostBodies[0]?.events || [];
  assert.ok(events.some((item) => item.event === "seek_back"), "seek_back 事件在批中");
  assert.ok(events[0].position_ms % 2000 === 0, "2s 桶对齐");

  /* 两击臂：第一击只挂臂，不删任何东西 */
  insightErase.click();
  assert.equal(insightErase.dataset.confirming, "true", "第一击挂臂");
  assert.equal(insightErase.textContent, "再点一次，抹掉本讲热点", "臂上文案讲清删什么");
  assert.equal(watchEventsClearCalls, 0, "挂臂阶段零删除请求");

  /* 第二击：只抹当前讲次（sub_id 域），热度带随之清空 */
  insightErase.click();
  await settle();
  assert.equal(watchEventsClearCalls, 1, "确认后调用 clear 路由");
  assert.equal(watchEventsClearBodies[0]?.sub_id, "sub-in", "只抹当前讲次（sub_id 域）");
  assert.equal(heat.children.length, 0, "热度带随之清空");
  assert.notEqual(insightErase.textContent, "再点一次，抹掉本讲热点", "删除后自动解除臂");

  /* 显式关过的本地偏好继续被尊重：零请求零采集，抹除钮隐藏 */
  localStorageStub.setItem("courselens:insight", "off");
  store.set("activeLecture", { course_id: "course-in", sub_id: "sub-in-2", sub_title: "另一讲", can_stream: true });
  await settle();
  assert.equal(watchEventsGetCalls, 1, "显式 off：切讲零 watch-events 请求");
  assert.equal(heat.children.length, 0, "显式 off：无热度带");
  assert.equal(insightErase.hidden, true, "显式 off：抹除钮隐藏");
  const postsAtOff = watchEventsPostBodies.length;
  player.currentTime = 300;
  player.dispatchEvent(new Event("timeupdate"));
  player.currentTime = 100;
  player.dispatchEvent(new Event("pause"));
  await settle();
  assert.equal(watchEventsPostBodies.length, postsAtOff, "显式 off：零写入");

  /* 清掉显式 off 即回到默认开 */
  localStorageStub.removeItem("courselens:insight");
  store.set("activeLecture", { course_id: "course-in", sub_id: "sub-in", sub_title: "洞察讲次", can_stream: true });
  await settle();
  assert.equal(watchEventsGetCalls, 2, "回到默认开：重新拉取热点");

  cleanup();
  localStorageStub.reset();
  watchEventsRows.clear();
  watchEventsPostBodies = [];
  watchEventsClearBodies = [];
}

// ---- S09-D 沉浸式控制台：显隐引擎、音量面、快捷键收权、清理收敛 ----

// 真实定时器驱动 1.5s idle 阈值（被映射为 150ms 真实等待）；留余量防计时抖动
const idleHideWait = () => new Promise((resolve) => setTimeout(resolve, 300));

function pointerEvent(type, { pointerType = "mouse", target } = {}) {
  const event = new Event(type, { bubbles: true });
  event.pointerType = pointerType;
  if (target) Object.defineProperty(event, "target", { value: target });
  return event;
}

function focusLikeEvent(type, { target, relatedTarget } = {}) {
  const event = new Event(type);
  if (target) Object.defineProperty(event, "target", { value: target });
  if (relatedTarget !== undefined) Object.defineProperty(event, "relatedTarget", { value: relatedTarget });
  return event;
}

async function verifyPlayerChromeBehavior() {
  const page = createPage();
  const store = createStore();
  const shell = page.elements.playerStageShell;
  const player = page.elements.playerStage;
  const controls = page.elements.playerControls;
  const volume = page.elements.playerVolume;
  const cleanup = await installPlayerCore(store);
  const chromeState = () => shell.dataset.chrome;

  // 未播放（暂停态）：进入即揭示（PLAYER-UX-1①：之后与播放同语义进空闲收起）
  store.set("activeLecture", { course_id: "course-chrome", sub_id: "sub-chrome", sub_title: "沉浸讲次", can_stream: true });
  assert.equal(controls.hidden, false);
  assert.equal(chromeState(), "visible");

  // 播放中：指针活动揭示；静止 2.5s 后单一计时器收起；再次移动重新揭示
  player.play(); /* fake 的 paused 只由 play()/pause() 方法翻转 */
  player.dispatchEvent(new Event("play"));
  assert.equal(player.paused, false);
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  assert.equal(chromeState(), "visible");
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  shell.dispatchEvent(pointerEvent("pointerenter", { target: player }));
  assert.equal(chromeState(), "visible");
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  shell.dispatchEvent(pointerEvent("pointermove", { target: player })); /* 重复移动只重置同一计时器 */
  assert.equal(chromeState(), "visible");

  // 焦点钉住（S10-A）：只有键盘来源（:focus-visible）焦点钉住控制台；
  // 鼠标点击引发的焦点移动不钉住（静止后照常收起）；离开整个播放器恢复 idle
  const keyboardTarget = new FakeElement("keyboard-focus");
  keyboardTarget.tagName = "button";
  keyboardTarget.focusVisible = true;
  const pointerTarget = new FakeElement("pointer-focus");
  pointerTarget.tagName = "button";
  pointerTarget.focusVisible = false;
  const outsideButton = new FakeElement("outside-button");
  outsideButton.tagName = "button";
  shell.dispatchEvent(focusLikeEvent("focusin", { target: pointerTarget }));
  await idleHideWait();
  assert.equal(chromeState(), "hidden"); /* 指针点击聚焦：不钉住，不永久按住控制台 */
  shell.dispatchEvent(focusLikeEvent("focusin", { target: keyboardTarget }));
  await idleHideWait();
  assert.equal(chromeState(), "visible"); /* 键盘焦点：钉住，绝不隐藏焦点控件 */
  shell.dispatchEvent(focusLikeEvent("focusout", outsideButton));
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  shell.dispatchEvent(focusLikeEvent("focusin", { target: keyboardTarget }));
  shell.dispatchEvent(focusLikeEvent("focusout", { relatedTarget: page.elements.playerCtrlPlay })); /* 控件间 Tab：relatedTarget 仍在播放器内 */
  await idleHideWait();
  assert.equal(chromeState(), "visible");
  shell.dispatchEvent(focusLikeEvent("focusout", outsideButton));

  // 指针离开只收敛：未钉住时按正常过渡立即收起且不重新武装（无 reveal 回弹）
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  assert.equal(chromeState(), "visible");
  shell.dispatchEvent(pointerEvent("pointerleave", { target: player }));
  assert.equal(chromeState(), "hidden");
  await idleHideWait();
  assert.equal(chromeState(), "hidden"); /* 离开后不被重新武装揭示 */

  // 拖动钉住：时间轴按下期间恒可见，收口恢复 idle
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("pointerdown"));
  await idleHideWait();
  assert.equal(chromeState(), "visible");
  page.elements.playerCtrlTimeline.dispatchEvent(new Event("change"));
  await idleHideWait();
  assert.equal(chromeState(), "hidden");

  // 音量面钉住：静音点击展开并钉住；外点解除后恢复 idle
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  page.elements.playerCtrlMute.click();
  assert.equal(volume.dataset.open, "true");
  await idleHideWait();
  assert.equal(chromeState(), "visible");
  globalThis.document.dispatchEvent(new Event("pointerdown"));
  assert.equal(volume.dataset.open, "false");
  await idleHideWait();
  assert.equal(chromeState(), "hidden");

  // pointercancel 只收敛不武装：未钉住时保持收起（与离开一致）；PLAYER-UX-1①
  // 暂停不再钉住——隐藏态下暂停同样不强行揭示
  shell.dispatchEvent(pointerEvent("pointercancel", { target: player }));
  assert.equal(chromeState(), "hidden");
  player.pause();
  player.dispatchEvent(new Event("pause"));
  assert.equal(chromeState(), "hidden", "暂停不钉住：隐藏态不因暂停揭示");
  shell.dispatchEvent(pointerEvent("pointercancel", { target: player }));
  assert.equal(chromeState(), "hidden");
  player.play();
  player.dispatchEvent(new Event("play"));
  /* 恢复触屏切换块的入口前置态：播放中+揭示态 */
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  assert.equal(chromeState(), "visible");

  // 触屏点按切换（不依赖悬停）；鼠标点按不切换；点在控制台上不是切换
  // （fake EventTarget 无冒泡：统一派发在 shell 上，用 target 伪造真实落点）
  shell.dispatchEvent(pointerEvent("pointerup", { pointerType: "touch", target: player }));
  assert.equal(chromeState(), "hidden");
  shell.dispatchEvent(pointerEvent("pointerup", { pointerType: "touch", target: player }));
  assert.equal(chromeState(), "visible");
  shell.dispatchEvent(pointerEvent("pointerup", { pointerType: "touch", target: page.elements.playerCtrlPlay }));
  assert.equal(chromeState(), "visible");
  shell.dispatchEvent(pointerEvent("pointerup", { pointerType: "mouse", target: player }));
  assert.equal(chromeState(), "visible"); /* 揭示态下鼠标点按不触发切换 */

  // PLAYER-UX-1①：暂停与播放同一空闲语义——收起态下暂停不揭示（不误弹），
  // 任意输入唤回后照常进空闲计时，暂停态超时同样收起
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  player.pause();
  player.dispatchEvent(new Event("pause"));
  assert.equal(chromeState(), "hidden", "隐藏态下暂停不强行揭示");
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  assert.equal(chromeState(), "visible", "暂停态任意输入唤回");
  await idleHideWait();
  assert.equal(chromeState(), "hidden", "暂停态空闲超时同样收起");

  // 悬停控制台=钉住豁免面（PLAYER-UX-1①）：悬停期间播放/暂停都不收起，离开恢复计时
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  assert.equal(chromeState(), "visible");
  const deckNode = page.elements.playerControls;
  deckNode.dispatchEvent(pointerEvent("pointerenter", { target: deckNode }));
  await idleHideWait();
  assert.equal(chromeState(), "visible", "悬停控制台豁免空闲收起");
  player.pause();
  player.dispatchEvent(new Event("pause"));
  await idleHideWait();
  assert.equal(chromeState(), "visible", "悬停控制台时暂停同样豁免");
  deckNode.dispatchEvent(pointerEvent("pointerleave", { target: deckNode }));
  await idleHideWait();
  assert.equal(chromeState(), "hidden", "离开控制台恢复空闲收起");
  player.play();
  player.dispatchEvent(new Event("play"));

  // 讲次切换确定性复位（PLAYER-UX-1①：未播放与播放同语义，复位后进空闲收起）
  store.set("activeLecture", { course_id: "course-chrome", sub_id: "sub-chrome-2", sub_title: "沉浸讲次二", can_stream: true });
  assert.equal(chromeState(), "visible");
  await idleHideWait();
  assert.equal(chromeState(), "hidden");

  // 重新播放后 idle 路径恢复；缓冲钉住；playing 解除
  player.play();
  player.dispatchEvent(new Event("play"));
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  player.dispatchEvent(new Event("waiting"));
  await idleHideWait();
  assert.equal(chromeState(), "visible"); /* 缓冲钉住 */
  player.dispatchEvent(new Event("playing"));
  await idleHideWait();
  assert.equal(chromeState(), "hidden");

  // 窗口失焦与页面可见性变化：确定性回到可见
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  window.dispatchEvent(new Event("blur"));
  assert.equal(chromeState(), "visible");
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  await idleHideWait();
  assert.equal(chromeState(), "hidden");
  globalThis.document.dispatchEvent(new Event("visibilitychange"));
  assert.equal(chromeState(), "visible");

  /* N6L S1 U3：直播切换的确定性复位随直播播放移交独立直播页
     （live-player attach 走自身的 chrome 复位路径）；讲次切换复位已在上文覆盖 */

  // 快捷键让位（负向）：焦点落在可交互元素（搜索框）上——播放器让位，默认不拦
  store.set("activeLecture", { course_id: "course-chrome", sub_id: "sub-chrome-3", sub_title: "沉浸讲次三", can_stream: true });
  player.play();
  player.duration = 600; /* seek 需要有限时长 */
  player.dispatchEvent(new Event("loadedmetadata"));
  const searchField = new FakeElement("global-search");
  searchField.tagName = "input";
  searchField.closest = (selector) => (String(selector).includes("input") ? searchField : null);
  player.currentTime = 100; /* seek 断言的已知基准 */
  const beforeTime = player.currentTime;
  const outsideSpace = playerKeyEvent("keydown", " ", { target: searchField });
  window.dispatchEvent(outsideSpace);
  assert.equal(player.paused, false);
  assert.equal(outsideSpace.defaultPrevented, false);
  const outsideSeek = playerKeyEvent("keydown", "ArrowRight", { target: searchField });
  window.dispatchEvent(outsideSeek);
  assert.equal(player.currentTime, beforeTime);
  assert.equal(outsideSeek.defaultPrevented, false);
  // 未聚焦页面级表面（PLAYER-INTERACT-REPAIR-1）：标题等非可交互元素归播放器
  const headingTarget = new FakeElement("page-heading");
  const headingSeek = playerKeyEvent("keydown", "ArrowRight", { target: headingTarget });
  window.dispatchEvent(headingSeek);
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, beforeTime + 5, "未聚焦页面级方向键归播放器");
  assert.equal(headingSeek.defaultPrevented, true);
  // 播放器内部非可编辑表面（video 元素）正向：快捷键生效并阻止默认
  const ownedSeek = playerKeyEvent("keydown", "ArrowRight", { target: player });
  window.dispatchEvent(ownedSeek);
  assert.ok(player.currentTime > beforeTime);
  assert.equal(ownedSeek.defaultPrevented, true);

  // 清理：显隐引擎完全冻结，后续指针/焦点/音量/计时器事件不再改变状态
  cleanup();
  const frozenChrome = chromeState();
  shell.dispatchEvent(pointerEvent("pointermove", { target: player }));
  shell.dispatchEvent(focusLikeEvent("focusin", { target: keyboardTarget }));
  shell.dispatchEvent(focusLikeEvent("focusout", { relatedTarget: outsideButton }));
  globalThis.document.dispatchEvent(new Event("pointerdown"));
  page.elements.playerCtrlMute.click();
  await idleHideWait();
  assert.equal(chromeState(), frozenChrome);

  // 保守默认：安装时即零音量（无非零历史），取消静音给 0.5
  const page2 = createPage();
  const store2 = createStore();
  page2.elements.playerStage.volume = 0;
  page2.elements.playerStage.muted = true;
  const cleanup2 = await installPlayerCore(store2);
  store2.set("activeLecture", { course_id: "course-vol", sub_id: "sub-vol", sub_title: "音量讲次", can_stream: true });
  assert.equal(page2.elements.playerCtrlVolume.value, "0");
  page2.elements.playerCtrlMute.click();
  page2.elements.playerStage.dispatchEvent(new Event("volumechange"));
  assert.equal(page2.elements.playerStage.muted, false);
  assert.equal(page2.elements.playerStage.volume, 0.5);
  assert.equal(page2.elements.playerCtrlVolume.value, "0.5");
  assert.equal(page2.elements.playerCtrlVolume.getAttribute("aria-valuetext"), "50%");
  cleanup2();
}

/* ---- 校园连接消费端与播放恢复的边界（VPN-P0-UI-1）： ----
   连接快照只进入顶部连接面；播放/直播恢复面板语义不变、绝不因附加字段被合成或改写。
   同时锁 TUN 提示纪律：全 UI 唯一允许 TUN 表述的来源是 reason=possible_tun_interference。 */
async function verifyCampusConnectionConsumerForPlayback() {
  const fixture = JSON.parse(
    readFileSync(new URL("./fixtures/vpn_connection_contract_v1.json", import.meta.url), "utf8"),
  ).valid;
  const { connectionViewFromSnapshot, extractConnectionSnapshot, CONNECTION_ACTION_LABELS } =
    await import("../frontend/modules/ui.js");

  /* 播放相关的连接状态视图：中性文案 + 正确主动作（连接面的行为，供播放恢复理解） */
  const now = Math.floor(Date.now() / 1000);
  const viewOf = (overrides = {}) => connectionViewFromSnapshot({
    schema: "courselens.vpn-connection.v1",
    state: "ready", network_path: "direct", school_route: "webvpn", reason: "direct_ok",
    observed_at: now - 30, expires_at: now + 3600, retry_after: null, actions: [], generation: 3,
    services: {
      webvpn: { state: "ready", route: "direct", verified: true },
      icourse: { state: "ready", route: "direct", verified: true },
    },
    ...overrides,
  }, now);
  const tunView = viewOf({ state: "network_unavailable", reason: "possible_tun_interference", expires_at: null, actions: ["close-tun-and-retry"] });
  assert.ok(tunView.hint.includes("可能"), "TUN 提示保持不确定性措辞");
  assert.ok(tunView.hint.includes("不一定"), "TUN 提示不宣称因果");
  assert.equal(CONNECTION_ACTION_LABELS["close-tun-and-retry"], "关闭 TUN 后重试", "TUN 动作标签");
  for (const reason of ["cold_start", "direct_ok", "proxy_fallback", "session_expired", "credentials_rejected", "challenge", "service_unavailable", "unknown"]) {
    assert.equal(viewOf({ reason, expires_at: null }).hint, "", `reason=${reason} 无任何 TUN/因果文案`);
  }
  assert.equal(viewOf({ state: "expired", reason: "session_expired", expires_at: null, actions: ["reauthenticate"] }).state, "expired", "expired 视图");
  assert.equal(viewOf({ state: "degraded", reason: "proxy_fallback", network_path: "local_proxy", school_route: "mixed", actions: ["retry"], retry_after: now + 60 }).state, "degraded", "degraded 视图");
  /* P3-B：未知未来字段/枚举必须安全降级——状态降级为中性复检态、动作被滤除，
     绝不把未知值渲染进 DOM。 */
  assert.equal(viewOf({ state: "future_state", expires_at: null }).state, "unknown_state", "未知未来状态安全降级为中性复检态");
  assert.deepEqual(viewOf({ state: "ready", actions: ["future-action"], expires_at: now + 60 }).actions, [], "未知未来动作被闭集滤除");
  assert.equal(extractConnectionSnapshot({ campus_connection: fixture.expired_session })?.state, "expired", "任意附加字段名下仍按 schema 定位");

  /* 播放器侧耦合守卫：auth 携带附加快照既不合成也不改写播放恢复面板 */
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  page.elements.playerRecovery.hidden = true; /* 真实 HTML 初始带 hidden 属性 */
  assert.equal(page.elements.playerRecovery.hidden, true, "初始无播放恢复面板");
  store.set("auth", {
    state: "degraded", code: "fudan_session_degraded", connection: fixture.degraded_proxy_fallback,
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(page.elements.playerRecovery.hidden, true, "附加连接快照不合成播放恢复面板");
  assert.equal(page.elements.playerRecoveryImpact.textContent, "", "恢复面板文案未被改写");
  cleanup();
}


/* ---- VPN-P1-RECOVERY-1（P1-C）：一次重认证后的播放/动作连续性 ----
   位置与播放态跨一次恢复重载保留；ready 后自动续播/续提恰一次；
   含糊失败（timeout）绝不自动重提（可能已受理，重提=重复任务）。 */
async function verifyRecoveryContinuity() {
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  page.elements.playerRecovery.hidden = true;
  /* P3-B 场景依赖生成笔记按钮可点：与真实会话一致，先确认字幕含时间轴。 */
  store.set("transcriptHasTiming", true);
  const player = page.elements.playerStage;

  // A) 手动恢复：重载前捕获位置，元数据就绪后一次性恢复位置与播放态
  store.set("activeLecture", { course_id: "c-cont", sub_id: "sub-cont", sub_title: "连续讲次", can_stream: true });
  player.currentTime = 187;
  player.play();
  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false, "网络中断出现恢复面板");
  const loadsBefore = player.loadCount;
  page.elements.playerRecoveryActions.children[0].click();
  assert.equal(player.loadCount, loadsBefore + 1, "恢复动作重载一次媒体");
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.currentTime, 187, "播放位置保留");
  assert.equal(player.paused, false, "播放态恢复");
  assert.equal(page.elements.playerRecovery.hidden, true, "恢复完成面板收起");

  // B) 重认证成功后恰一次自动续播（授权类失败 code=4）；重复 ready 不重复
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false);
  const loadsBeforeAuto = player.loadCount;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  assert.equal(player.loadCount, loadsBeforeAuto + 1, "ready 后自动重取一次");
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.currentTime, 187, "自动续播同样保留位置");
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  assert.equal(player.loadCount, loadsBeforeAuto + 1, "重复 ready 不重复续播");
  /* P2-E：两次位置恢复 → 会话内去标识计数器恰 +2（纯计数，无持久化写入）。 */
  const { localMetric } = await import("../frontend/modules/ui.js");
  assert.equal(localMetric("playback_position_retained"), 2, "位置保留次数去标识计数");
  assert.equal(typeof localMetric("nonexistent_metric"), "number", "未知计数名返回数值零");

  // B2) 新失败回合重新武装：再次 ready 再允许恰一次
  player.error = { code: 4 };
  player.dispatchEvent(new Event("error"));
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  assert.equal(player.loadCount, loadsBeforeAuto + 2, "新失败回合恰一次自动续播");
  player.dispatchEvent(new Event("loadedmetadata"));

  // C) 网络中断/解码失败无重认证语义：ready 绝不触发续播（15s auth 轮询
  //    持续返回 ready 时也不成循环）
  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  const loadsDecode = player.loadCount;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  assert.equal(player.loadCount, loadsDecode, "网络中断不由重认证触发续播");
  player.error = { code: 3 };
  player.dispatchEvent(new Event("error"));
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  assert.equal(player.loadCount, loadsDecode, "解码失败不由重认证触发续播");
  player.dispatchEvent(new Event("loadedmetadata"));

  // D) 讲次切换清空连续性状态：旧待办绝不跨讲次续提
  store.set("activeLecture", { course_id: "c-enq", sub_id: "sub-enq", sub_title: "提交讲次", can_stream: true });
  store.set("transcriptHasTiming", true); /* 字幕读取落地：确认值绑定 sub-enq（W9 后本讲次内 notes 可点） */
  enqueueFailure = { status: 401, body: { error: "Fudan authentication is required", error_code: "fudan_login_required", actions: ["login"] } };
  const submitsBefore = taskCalls;
  page.elements.generateSubtitle.click();
  await settle();
  await settle();
  assert.equal(taskCalls, submitsBefore + 1, "401 只发出一次请求：无自动重复提交");
  assert.ok(page.elements.playerTaskText.textContent.includes("等待重新认证"), "提示将自动继续");
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsBefore + 2, "重认证成功后自动续提恰一次");
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsBefore + 2, "重复 ready 不重复续提");

  // E) 含糊失败（timeout）不保留待办：ready 后绝不自动重提
  enqueueFailure = { status: 500, body: { error: "upstream", error_code: "timeout" } };
  page.elements.generateNotes.click();
  await settle();
  await settle();
  const afterTimeout = taskCalls;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, afterTimeout, "timeout 不自动重提（可能已受理）");

  // F) P3-B：恢复期间的 seek 意图保留 + 字幕 cue 跟随恢复位置。
  //    错误把 currentTime 回卷到 seek 前（30s），恢复仍回到用户目标 260s。
  const seekTrack = new FakeTextTrack();
  page.elements.playerSubtitleTrack.track = seekTrack;
  store.set("activeLecture", { course_id: "c-seek", sub_id: "sub-seek", sub_title: "跳转讲次", can_stream: true });
  seekTrack.addCue({ startTime: 250, endTime: 270, text: "合成字幕" });
  player.duration = 600;
  player.currentTime = 30;
  player.dispatchEvent(new Event("loadedmetadata")); /* 基线：无恢复待办 */
  player.currentTime = 260;
  player.dispatchEvent(new Event("seeking")); /* 用户 seek 到 260：意图被记录 */
  player.writePlaybackTime(30); /* 引擎在错误路径把位置回卷（无 seeking 事件） */
  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false, "seek 后传输失败出现恢复面板");
  const loadsSeek = player.loadCount;
  page.elements.playerRecoveryActions.children[0].click();
  assert.equal(player.loadCount, loadsSeek + 1, "恢复动作重载一次媒体");
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.currentTime, 260, "恢复回到最后一次 seek 目标而非回卷位置");
  assert.equal(page.elements.playerSubtitleOverlay.hidden, false, "恢复后字幕 overlay 可见");
  assert.equal(page.elements.playerSubtitleOverlay.textContent, "合成字幕", "字幕 cue 跟随恢复后的播放位置");

  // F2) W1：seek 完成后播放真实推进，陈旧 seek 意图过期——恢复回到推进后的
  //     位置，绝不回卷到早已越过的旧 seek 点（缺陷形状：seek 100 → 推进 300 →
  //     断网恢复被回卷到 100 且丢弃更新的持久进度）。
  store.set("activeLecture", { course_id: "c-fresh", sub_id: "sub-fresh", sub_title: "推进讲次", can_stream: true });
  player.duration = 600;
  player.currentTime = 100; /* 用户 seek 到 100（currentTime 写入派发 seeking：意图捕获） */
  player.writePlaybackTime(300); /* 之后播放真实推进到 300（引擎侧写入，无 seeking） */
  player.dispatchEvent(new Event("timeupdate")); /* 播放推进：seek 意图过期 */
  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false, "播放推进后断网出现恢复面板");
  const loadsFresh = player.loadCount;
  page.elements.playerRecoveryActions.children[0].click();
  assert.equal(player.loadCount, loadsFresh + 1, "恢复动作重载一次媒体");
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(player.currentTime, 300, "恢复回到推进后的真实位置，绝不回卷到陈旧 seek 点");

  // G) P3-B：退出登录清除全部连续性待办——之后的 ready 绝不续提。
  store.set("activeLecture", { course_id: "c-logout", sub_id: "sub-logout", sub_title: "登出讲次", can_stream: true });
  store.set("transcriptHasTiming", true); /* 字幕读取落地：确认值绑定 sub-logout（H/H2 的 notes 点击需要） */
  enqueueFailure = { status: 401, body: { error: "Fudan authentication is required", error_code: "fudan_login_required", actions: ["login"] } };
  const submitsLogout = taskCalls;
  page.elements.generateSubtitle.click();
  await settle();
  await settle();
  assert.equal(taskCalls, submitsLogout + 1, "401 只发出一次请求");
  assert.ok(page.elements.playerTaskText.textContent.includes("等待重新认证"), "待办等待重认证");
  windowTarget.dispatchEvent(new Event("courselens:logout")); /* 退出登录（shell 派发的同一事件） */
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsLogout + 1, "登出清除待办：ready 后绝不续提");

  // H) P3-B：目录身份不一致/无法确认 = 另一账号作用域 → 待办立即清除。
  const submit401 = () => ({ status: 401, body: { error: "Fudan authentication is required", error_code: "fudan_login_required", actions: ["login"] } });
  enqueueFailure = submit401();
  const submitsIdm = taskCalls;
  page.elements.generateNotes.click();
  await settle();
  await settle();
  assert.equal(taskCalls, submitsIdm + 1, "401 只发出一次请求");
  assert.ok(page.elements.playerTaskText.textContent.includes("等待重新认证"), "待办等待重认证");
  store.set("auth", { state: "degraded", code: "catalog_identity_mismatch" }); /* 身份边界 → 清除 */
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsIdm + 1, "身份不一致清除待办：随后的 ready 不续提");
  // H2) catalog_identity_invalid 同样清除。
  enqueueFailure = submit401();
  page.elements.generateNotes.click();
  await settle();
  await settle();
  assert.equal(taskCalls, submitsIdm + 2, "401 只发出一次请求");
  assert.ok(page.elements.playerTaskText.textContent.includes("等待重新认证"));
  store.set("auth", { state: "degraded", code: "catalog_identity_invalid" }); /* 身份无法确认 → 同样清除 */
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsIdm + 2, "身份无法确认同样清除待办：ready 不续提");

  // I) P3-B：同一动作重复点击不叠加待办；续提载荷与原提交逐字节一致
  //    （保留的身份三元组就是这个动作的幂等键，绝不另造新键）。
  store.set("activeLecture", { course_id: "c-dup", sub_id: "sub-dup", sub_title: "重复讲次", can_stream: true });
  enqueueFailure = { status: 401, body: { error: "Fudan authentication is required", error_code: "fudan_login_required", actions: ["login"] } };
  const submitsDup = taskCalls;
  const bodiesDup = enqueueBodies.length;
  page.elements.generateSubtitle.click();
  await settle();
  await settle();
  page.elements.generateSubtitle.click(); /* 等待重认证期间重复点击同一动作 */
  await settle();
  await settle();
  assert.equal(taskCalls, submitsDup + 2, "两次点击恰两次请求（每次点击真实尝试一次）");
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, submitsDup + 3, "重认证后仍只续提一次：重复点击不叠加待办");
  assert.equal(enqueueBodies[bodiesDup], enqueueBodies[bodiesDup + 1], "重复点击的请求载荷与原提交逐字节一致");
  assert.equal(enqueueBodies[bodiesDup + 1], enqueueBodies[bodiesDup + 2], "续提载荷与原提交逐字节一致（幂等键不变）");

  cleanup();
}

/* ---- C1-LOCATE 防护钉：未授权环境（cloud_setup_required 400）AI 入队的人话闭环 ----
   混沌风暴 SWEEP1-C1 实证形状（生产壳 2026-10-08）：任务入队前置门未就绪 →
   POST tasks/enqueue → 400 闭集 cloud_setup_required。三断言防入队门语义漂移：
   ①证据行/toast 走 api.js 码表人话（绝不拼英文 payload.error、绝不裸码）；
   ②fail-closed：非 401 类失败不保留续提待办、不假开任务抽屉、不广播任务刷新；
   ③按钮 busy 收口（setBusy false，无卡死破相）。服务端同形状钉见
   tests/test_enqueue_gate_fail_closed.py。 */
async function verifyEnqueueCloudSetupGatePin() {
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  /* 与 verifyRecoveryContinuity D 节同序：先定讲次身份、再落字幕确认——
     W9 写侧绑定守卫把确认值绑到讲次，先确认后切讲会被视为旧值不生效。 */
  store.set("activeLecture", { course_id: "c-c1", sub_id: "sub-c1", sub_title: "C1 钉讲次", can_stream: true });
  store.set("transcriptHasTiming", true);
  let refreshEvents = 0;
  windowTarget.addEventListener("courselens:tasks-refresh", () => { refreshEvents += 1; });
  /* 忠实初态：index.html 里「查看进度」按钮自带 hidden（真壳初始不可见）；
     假 DOM 元素缺省 hidden=false 不忠实，本地复位后再钉「失败不打开抽屉」。 */
  page.elements.playerTaskOpenDrawer.hidden = true;
  const humanCopy = "云端处理还没完成 GitHub 授权连接。请到「设置 → 网络与远程连接」完成连接步骤，再重试这个任务。";
  enqueueFailure = {
    status: 400,
    body: { error: "Task could not be queued", error_code: "cloud_setup_required", retriable: false },
  };
  const callsBefore = taskCalls;
  page.elements.generateNotes.click();
  await settle();
  await settle();
  assert.equal(taskCalls, callsBefore + 1, "400 闭集拒绝恰一次请求（POST 零传输层重试）");
  const evidenceText = String(page.elements.playerTaskText.textContent || "");
  assert.ok(evidenceText.includes("GitHub 授权连接"), "证据行走人话指路（云授权没就绪）");
  assert.ok(!evidenceText.includes("Task could not be queued"), "证据行绝不拼英文后端原文");
  const toastNodes = page.elements.toastRegion.children;
  const toastText = String(toastNodes[toastNodes.length - 1]?.textContent || "");
  assert.equal(toastText, humanCopy, "toast 与 api.js 码表逐字一致（防措辞漂移）");
  assert.equal(page.elements.playerTaskOpenDrawer.hidden, true, "失败绝不假开任务抽屉");
  assert.equal(page.elements.generateNotes.disabled, false, "失败后按钮 busy 收口（不卡死）");
  assert.equal(refreshEvents, 0, "失败不广播任务刷新");
  enqueueFailure = null;
  store.set("auth", { state: "ready", code: "fudan_session_verified" });
  await settle();
  await settle();
  assert.equal(taskCalls, callsBefore + 1, "cloud_setup_required 不保留续提待办：ready 后绝不自动重提");
  /* F-GATE-3 复核钉（发布门 2026-10-10）：发布门曾报「点击生成字幕→400→零反馈」，
     本钉在该链上不可复现（notes 面 C1-LOCATE 钉已绿）——补齐 finding 点名的
     生成字幕按钮同形状断言：拒绝面人话上屏+busy 收口与 notes 完全同链。 */
  enqueueFailure = {
    status: 400,
    body: { error: "Task could not be queued", error_code: "cloud_setup_required", retriable: false },
  };
  const subtitleCallsBefore = taskCalls;
  page.elements.generateSubtitle.click();
  await settle();
  await settle();
  assert.equal(taskCalls, subtitleCallsBefore + 1, "生成字幕 400 闭集拒绝恰一次请求");
  const subtitleEvidence = String(page.elements.playerTaskText.textContent || "");
  assert.ok(subtitleEvidence.includes("GitHub 授权连接"), "生成字幕证据行走人话指路");
  const subtitleToasts = page.elements.toastRegion.children;
  const subtitleToast = String(subtitleToasts[subtitleToasts.length - 1]?.textContent || "");
  assert.equal(subtitleToast, humanCopy, "生成字幕 toast 与 api.js 码表逐字一致");
  assert.equal(page.elements.generateSubtitle.disabled, false, "生成字幕失败后按钮 busy 收口");
  enqueueFailure = null;
  cleanup();
}

/* ---- W9：讲次切换后「生成笔记」先按未确认禁用 ----
   transcriptTimed 是按讲次确认的（字幕读取返回后才定）：讲次身份变化后、
   新讲次的字幕读取返回前，旧讲次的确认值绝不生效；同讲次恢复重载
   （retry-media）不清除已确认的可用性。 */
async function verifyLectureSwitchActionGuard() {
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  const player = page.elements.playerStage;

  // 有字幕讲次 A：字幕读取落地（transcriptHasTiming=true）→ 生成笔记可点
  store.set("activeLecture", { course_id: "c-guard", sub_id: "sub-a", sub_title: "有字幕讲次", can_stream: true });
  store.set("transcriptHasTiming", true);
  assert.equal(page.elements.generateNotes.disabled, false, "A 有字幕：生成笔记可点");

  // 切到讲次 B：B 的字幕读取返回前，A 的旧确认值不生效 → 先按未确认禁用
  store.set("activeLecture", { course_id: "c-guard", sub_id: "sub-b", sub_title: "无字幕讲次", can_stream: true });
  assert.equal(page.elements.generateNotes.disabled, true, "B 字幕未确认：生成笔记先禁用");
  store.set("transcriptHasTiming", false); /* B 的字幕读取返回：无时间轴 */
  assert.equal(page.elements.generateNotes.disabled, true, "B 无字幕：生成笔记保持禁用");

  // 有字幕讲次 C 可点；同讲次恢复重载（retry-media）不清除已确认可用性
  store.set("activeLecture", { course_id: "c-guard", sub_id: "sub-c", sub_title: "恢复讲次", can_stream: true });
  store.set("transcriptHasTiming", true);
  assert.equal(page.elements.generateNotes.disabled, false, "C 有字幕：生成笔记可点");
  player.error = { code: 2 };
  player.dispatchEvent(new Event("error"));
  assert.equal(page.elements.playerRecovery.hidden, false, "断网出现恢复面板");
  page.elements.playerRecoveryActions.children[0].click();
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(page.elements.generateNotes.disabled, false, "同讲次恢复重载不清除已确认的笔记可用性");
  cleanup();
}

/* ---- P3-B：媒体恢复静态可达性/响应式锁（无像素引擎时的可重复证据） ----
   恢复面板是 aria-live 区域、播放器有 role=status 播报区（新增的恢复过渡
   播报走既有区域）、间距走设计令牌且无固定像素宽度；既有断点与
   reduced-motion、forced-colors 规则保持——恢复增强不引入任何新视觉面。 */
function verifyMediaContinuityStaticLocks() {
  const indexHtml = readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8");
  const pagesCss = readFileSync(new URL("../frontend/styles/pages.css", import.meta.url), "utf8");
  const accessibilityCss = readFileSync(new URL("../frontend/styles/accessibility.css", import.meta.url), "utf8");
  const recoveryTag = indexHtml.match(/<section id="player-recovery"[^>]*>/)?.[0] || "";
  assert.ok(recoveryTag.includes('aria-live="polite"'), "播放恢复面板是 live 区域（状态转换自动播报）");
  const statusTag = indexHtml.match(/<p id="player-ctrl-status"[^>]*>/)?.[0] || "";
  assert.ok(statusTag.includes('role="status"'), "播放器有 role=status 播报区");
  for (const breakpoint of [
    "@media (max-width: 719px)",
    "@media (max-width: 999px)",
    "@media (min-width: 1000px)",
  ]) {
    assert.ok(pagesCss.includes(breakpoint), `既有响应式断点保持：${breakpoint}`);
  }
  /* 430px 断点原为设置页自动化卡的窄屏 select 适配；该板块退场后断点随之
     移除（components.css 的 430px 任务摘要规则不受影响）。 */
  assert.ok(!pagesCss.includes("@media (max-width: 430px)"), "随自动化卡退场的 430px 孤断点不残留");
  const recoveryCss = pagesCss.match(/\.player-recovery\s*\{[^}]*\}/)?.[0] || "";
  assert.ok(recoveryCss.includes("var(--space-3)"), "恢复面板间距走设计令牌");
  assert.ok(!/width:\s*\d+px/.test(recoveryCss), "恢复面板无固定像素宽度（375-1440 不横向溢出）");
  assert.ok(accessibilityCss.includes("@media (prefers-reduced-motion: reduce)"), "reduced-motion 全局规则保持（恢复无新增动画）");
  assert.ok(pagesCss.includes("@media (forced-colors: active)"), "forced-colors 规则保持");
}

/* ---- 观看进度闭环（BUGFIX-PLAYBACK-PROGRESS-1）：持久进度按 sub_id 恢复、
   讲次边界绝不串扰、加载期 seek 让位、重认证恢复优先、退出学习桌即暂停、
   普通成功态静默、保存成功广播进度事件。 ---- */
const progressRow = (overrides = {}) => ({
  sub_id: "s-restore", course_id: "c-restore", position_seconds: 120,
  duration_seconds: 600, progress_percent: 20, completed: false,
  playback_rate: 1, updated_at: 100, ...overrides,
});

/* L96' 续播浮层存活判据：合成壳 remove()=hidden（不真离树），连续 show/dismiss
   会留下 hidden 旧层；「浮层可见」=最后一个未 hidden 的 .player-resume-banner
   （真实浏览器 remove 即离树，该过滤恒直取唯一存活节点）。 */
function lastResumeBanner(page) {
  const shell = page.elements.playerStageShell;
  const nodes = shell.children.filter((node) => String(node.className || "").includes("player-resume-banner"));
  const visible = nodes.filter((node) => node.hidden !== true);
  return visible.length ? visible[visible.length - 1] : null;
}

async function verifyWatchProgressRestore() {
  // A) GET 先落定、元数据后就绪：可 seek 后一次性恢复；L96' 自动续播+浮层；
  // 时间轴同步
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    progressGetRows.set("s-restore", progressRow());
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-restore", sub_title: "进度讲次", can_stream: true });
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 120, "元数据可 seek 后恢复持久位置");
    assert.equal(player.paused, false, "L96'：定位成功即自动续播（契约升格=20261008 用户点名）");
    {
      const { localMetric } = await import("../frontend/modules/ui.js");
      assert.equal(localMetric("playback_auto_resume"), 1, "L96' 证据链：自动续播去标识计数恰 1（本机 only）");
    }
    const banner = lastResumeBanner(page);
    assert.ok(banner, "续播浮层随自动续播出现");
    assert.equal(banner.hidden, false, "浮层可见");
    assert.equal(banner.querySelector(".player-resume-text").textContent.includes("2:00"), true,
      "浮层讲清续播位置");
    assert.ok(page.elements.playerCtrlStatus.textContent.includes("已从上次位置 2:00"), "恢复有播报");
    player.dispatchEvent(new Event("timeupdate"));
    assert.equal(page.elements.playerTimelineFill.style.getPropertyValue("--play-ratio"), "0.2", "恢复后时间轴同步");
    /* 恢复钳制到 seekable 末段：新讲次位置 120 > seekable 端 100 */
    progressGetRows.set("s-clamp", progressRow({ sub_id: "s-clamp", position_seconds: 120 }));
    player.seekable = { length: 1, end: () => 100 };
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-clamp", sub_title: "钳制讲次", can_stream: true });
    await settle();
    assert.equal(player.currentTime, 100, "恢复位置钳制到 seekable 末段");
    player.seekable = { length: 0 };
    cleanup();
    progressGetRows.delete("s-restore");
    progressGetRows.delete("s-clamp");
  }

  // A2) L96' 边界闭集与逃生浮层三路收口
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    /* 计数基线：ui.js 计数器为模块单例（A 段 s-restore/s-clamp 两例已计入），
       近开头/近结尾的负断言按「相对基线零增量」口径断言。 */
    const { localMetric } = await import("../frontend/modules/ui.js");
    const autoResumeBase = localMetric("playback_auto_resume");

    // 近开头（<5s）：只定位播报，绝不自动播/不出浮层
    progressGetRows.set("s-near-start", progressRow({ sub_id: "s-near-start", position_seconds: 3 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-near-start", sub_title: "近开头", can_stream: true });
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 3, "近开头仍诚实定位");
    assert.equal(player.paused, true, "近开头不自动播（与从头无感知差）");
    assert.equal(lastResumeBanner(page), null, "近开头不出浮层");
    assert.ok(page.elements.playerCtrlStatus.textContent.includes("已恢复到上次观看位置"), "近开头保持定位播报");

    // 近结尾（距末尾 <15s）：只定位播报，绝不自动播
    progressGetRows.set("s-near-end", progressRow({ sub_id: "s-near-end", position_seconds: 590 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-near-end", sub_title: "近结尾", can_stream: true });
    await settle();
    assert.equal(player.currentTime, 590, "近结尾仍诚实定位");
    assert.equal(player.paused, true, "近结尾不自动播（马上看完）");
    assert.equal(lastResumeBanner(page), null, "近结尾不出浮层");
    assert.equal(localMetric("playback_auto_resume"), autoResumeBase, "近开头/近结尾均不触发自动续播（阈值闭集负断言）");
    progressGetRows.delete("s-near-start");
    progressGetRows.delete("s-near-end");

    // 正常区间：自动续播后「从头看」=回零续播+浮层收口
    progressGetRows.set("s-banner", progressRow({ sub_id: "s-banner", position_seconds: 180 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-banner", sub_title: "浮层讲次", can_stream: true });
    await settle();
    assert.equal(player.paused, false, "正常区间自动续播");
    let banner = lastResumeBanner(page);
    assert.ok(banner, "浮层出现");
    const restart = banner.querySelector(".player-resume-restart");
    assert.ok(restart, "「从头看」按钮可达");
    restart.click();
    assert.equal(player.currentTime, 0, "「从头看」回到起点");
    assert.equal(player.paused, false, "「从头看」保持播放");
    assert.equal(lastResumeBanner(page), null, "点击后浮层收口");

    // Esc 收口：浮层再现后按 Esc 收起、播放不受扰
    progressGetRows.set("s-esc", progressRow({ sub_id: "s-esc", position_seconds: 240 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-esc", sub_title: "Esc 讲次", can_stream: true });
    await settle();
    banner = lastResumeBanner(page);
    assert.ok(banner, "浮层再现");
    window.dispatchEvent(playerKeyEvent("keydown", "Escape", { target: player }));
    assert.equal(lastResumeBanner(page), null, "Esc 收口浮层");

    // 讲次切换确定性收口：浮层不跨讲次残留
    progressGetRows.set("s-switch", progressRow({ sub_id: "s-switch", position_seconds: 300 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-switch", sub_title: "切讲讲次", can_stream: true });
    await settle();
    assert.ok(lastResumeBanner(page), "切讲后新浮层跟随新讲次");
    store.set("activeLecture", null);
    await settle();
    assert.equal(lastResumeBanner(page), null, "归空/切讲浮层确定性收口");

    cleanup();
    progressGetRows.delete("s-banner");
    progressGetRows.delete("s-esc");
    progressGetRows.delete("s-switch");
  }

  // A3) L96' 浮层焦点纪律（FOCUS-DRIFT-1 政策适用面，D-20261007-03 剧本延伸）
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    progressGetRows.set("s-focus", progressRow({ sub_id: "s-focus", position_seconds: 150 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-focus", sub_title: "浮层焦点讲次", can_stream: true });
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    const banner = lastResumeBanner(page);
    assert.ok(banner, "浮层出现（前提）");
    assert.equal(document.activeElement, null, "浮层出现零抢焦点（自动续播不劫持 Tab 位）");
    const restart = banner.querySelector(".player-resume-restart");
    /* 桩 append 只 push children 不设 parent（真实 DOM append 建树）：
       补上树语义，否则归还链 scope.contains 断链（循 :345 装配先例）。 */
    banner.parent = page.elements.playerStageShell;
    restart.parent = banner;
    assert.equal(restart.type, "button", "「从头看」type=button（原生按钮激活语义，不随表单提交）");
    assert.equal(restart.attributes.get("tabindex"), undefined, "零 tabindex（可达性硬门：零正零负）");
    // 键盘路径：聚焦驻留 + 激活键硬门（Space/Enter 留原生）+ 方向透传
    //（tagName=button 循矩阵先例：真实 DOM 由解析器给出，合成壳作复现前提设置——
    //  指针归还链 activeElement.matches("button, a") 判定依赖它。）
    restart.tagName = "button";
    restart.focus();
    player.pause(); /* 自动续播先收口：与焦点矩阵同前提（测激活键不双触发） */
    assert.equal(document.activeElement, restart, "键盘 Tab 聚焦驻留（前提）");
    const focusSpace = playerKeyEvent("keydown", " ", { target: restart });
    window.dispatchEvent(focusSpace);
    assert.equal(focusSpace.defaultPrevented, false, "聚焦浮层按钮 Space 留原生激活（激活键硬门）");
    assert.equal(player.paused, true, "聚焦浮层按钮 Space 不触发播放切换（无双触发）");
    window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: restart }));
    window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
    assert.equal(player.currentTime, 155, "聚焦浮层按钮右方向仍透传 seek +5s");
    // 指针路径：点击=动作+浮层收口；学习桌 capture 归还链同参模拟（真实浏览器
    // remove 即离树；合成壳 remove=hidden，lastResumeBanner 过滤 hidden 等价）
    restart.click();
    assert.equal(player.currentTime, 0, "「从头看」动作生效（回零续播）");
    assert.equal(lastResumeBanner(page), null, "点击后浮层收口");
    restart.blur();
    restart.focus();
    const clickOnDesk = (button) => {
      const click = new Event("click", { bubbles: true });
      Object.defineProperty(click, "target", { value: button });
      page.elements.studyDesk.dispatchEvent(click);
    };
    clickOnDesk(restart);
    assert.equal(document.activeElement, null, "指针路径点击后焦点归还页面级（归还链覆盖浮层）");
    // 收口后快捷键即刻存活：Space 切换播放（学习桌可见、无焦点驻留）
    player.pause(); /* 「从头看」的 playQuietly 顶掉先前暂停：归还后重收口再验证存活 */
    const afterSpace = playerKeyEvent("keydown", " ", { target: player });
    window.dispatchEvent(afterSpace);
    assert.equal(afterSpace.defaultPrevented, true, "浮层收口后 Space 被播放快捷键消费（存活）");
    assert.equal(player.paused, false, "浮层收口后 Space 照常切换播放");
    // REPRO E（20261008 夜走查发现）：浮层开着时学生主动 seek——浮层是否失真滞留
    progressGetRows.set("s-seekaway", progressRow({ sub_id: "s-seekaway", position_seconds: 120 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-seekaway", sub_title: "拖走讲次", can_stream: true });
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    // E) 浮层与主动 seek 互斥（20261008 夜走查实锤）：学生拖走=「已从上次位置」
    //    陈述即刻失真，浮层滞留到超时是误导——seeking 意图出现即收口
    progressGetRows.set("s-seekaway", progressRow({ sub_id: "s-seekaway", position_seconds: 120 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-seekaway", sub_title: "拖走讲次", can_stream: true });
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.ok(lastResumeBanner(page), "浮层出现（前提）");
    player.currentTime = 90; /* 学生主动拖走（桩 setter 派发 seeking） */
    assert.equal(lastResumeBanner(page), null, "主动 seek 后浮层即收（续播陈述不失真）");
    progressGetRows.delete("s-seekaway");

    // D) 12s 超时收口路径（假时钟捕获，循焦点矩阵 setTimeout 替换先例）
    const originalSetTimeout = windowTarget.setTimeout;
    const originalClearTimeout = windowTarget.clearTimeout;
    let capturedDelay = 0;
    let capturedCallback = null;
    windowTarget.setTimeout = (callback, delay) => { capturedDelay = delay; capturedCallback = callback; return 424242; };
    windowTarget.clearTimeout = () => {};
    try {
      progressGetRows.set("s-timeout", progressRow({ sub_id: "s-timeout", position_seconds: 200 }));
      store.set("activeLecture", { course_id: "c-restore", sub_id: "s-timeout", sub_title: "超时讲次", can_stream: true });
      await settle();
      assert.ok(lastResumeBanner(page), "浮层再现（前提）");
      assert.equal(capturedDelay, 12000, "超时闭集=12s（idle 计时 1500 先挂被同帧覆盖）");
      capturedCallback();
      assert.equal(lastResumeBanner(page), null, "12s 到点浮层确定性收口");
    } finally {
      windowTarget.setTimeout = originalSetTimeout;
      windowTarget.clearTimeout = originalClearTimeout;
    }
    progressGetRows.delete("s-timeout");
    cleanup();
    progressGetRows.delete("s-focus");
  }

  // B) 元数据先就绪、GET 后落定：读取落地即恢复
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    progressGetRows.set("s-late", progressRow({ sub_id: "s-late", position_seconds: 75 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-late", sub_title: "迟到读取", can_stream: true });
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 0, "读取未到不恢复");
    await settle();
    assert.equal(player.currentTime, 75, "读取落地即恢复到持久位置");
    cleanup();
    progressGetRows.delete("s-late");
  }

  // C) 诚实降级闭集：零值/未知/超时长/已完成/错讲次/跨课程/读取失败
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    progressGetRows.set("s-zero", progressRow({ sub_id: "s-zero", position_seconds: 0 }));
    progressGetRows.set("s-nan", progressRow({ sub_id: "s-nan", position_seconds: Number.NaN }));
    progressGetRows.set("s-unknown-dur", progressRow({ sub_id: "s-unknown-dur", position_seconds: 0, duration_seconds: 0, progress_percent: 0 }));
    progressGetRows.set("s-past-end", progressRow({ sub_id: "s-past-end", position_seconds: 7200 }));
    progressGetRows.set("s-done", progressRow({ sub_id: "s-done", completed: true, progress_percent: 100 }));
    progressGetRows.set("s-other", progressRow({ sub_id: "s-elsewhere" }));
    progressGetRows.set("s-foreign", progressRow({ sub_id: "s-foreign", course_id: "c-elsewhere" }));
    progressGetRows.set("s-fail", "error");
    for (const subId of ["s-zero", "s-nan", "s-past-end", "s-done", "s-other", "s-foreign", "s-fail"]) {
      store.set("activeLecture", { course_id: "c-restore", sub_id: subId, sub_title: subId, can_stream: true });
      await settle();
      player.duration = 600;
      player.dispatchEvent(new Event("loadedmetadata"));
      assert.equal(player.currentTime, 0, `${subId} 诚实降级：不恢复位置`);
    }
    // 未知时长但位置有效：按当前媒体时长钳制后恢复
    progressGetRows.set("s-unknown-dur", progressRow({ sub_id: "s-unknown-dur", position_seconds: 30, duration_seconds: 0 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-unknown-dur", sub_title: "未知时长", can_stream: true });
    await settle();
    assert.equal(player.currentTime, 30, "未知时长行按当前媒体钳制恢复");
    // 失败读取不致命：随后同页面的成功读取照常恢复
    progressGetRows.set("s-fail", progressRow({ sub_id: "s-fail", position_seconds: 45 }));
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-fail", sub_title: "失败后成功", can_stream: true });
    await settle();
    assert.equal(player.currentTime, 45, "失败读取后同会话成功读取照常恢复");
    cleanup();
    for (const subId of ["s-zero", "s-nan", "s-unknown-dur", "s-past-end", "s-done", "s-other", "s-foreign", "s-fail"]) {
      progressGetRows.delete(subId);
    }
  }

  // D) 用户加载期主动 seek 让位；重认证恢复（resume）优先于持久进度
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    page.elements.playerRecovery.hidden = true;
    progressGetRows.set("s-user-seek", progressRow({ sub_id: "s-user-seek", position_seconds: 120 }));
    // D1：GET 落定后、元数据前用户 seek
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-user-seek", sub_title: "用户优先一", can_stream: true });
    await settle();
    player.currentTime = 42;
    player.dispatchEvent(new Event("seeking"));
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 42, "用户加载期 seek 不被持久进度覆盖");
    // D2：重认证恢复（resume）优先于持久进度
    player.currentTime = 187;
    player.dispatchEvent(new Event("seeking")); /* 用户跳转意图被记录 */
    player.play();
    player.error = { code: 2 };
    player.dispatchEvent(new Event("error"));
    assert.equal(page.elements.playerRecovery.hidden, false, "网络中断出现恢复面板");
    page.elements.playerRecoveryActions.children[0].click();
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 187, "重认证恢复的位置优先");
    assert.equal(player.paused, false, "重认证恢复保留播放态");
    await settle();
    assert.equal(player.currentTime, 187, "持久进度绝不覆盖重认证恢复");
    cleanup();
    progressGetRows.delete("s-user-seek");
  }

  // E) 讲次切换：旧讲次的迟到读取绝不落到新讲次
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    const deferredOld = deferred();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (path, options) => {
      if (String(path).includes("sub_id=s-old")) return deferredOld.promise;
      return originalFetch(path, options);
    };
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-old", sub_title: "旧讲次", can_stream: true });
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-new", sub_title: "新讲次", can_stream: true });
    deferredOld.resolve(new Response(JSON.stringify({
      schema: "courselens.api.v3",
      data: { progress: progressRow({ sub_id: "s-old", position_seconds: 300 }) },
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await settle();
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    assert.equal(player.currentTime, 0, "旧讲次迟到读取不落新讲次");
    cleanup();
    progressGetRows.delete("s-old");
  }

  // F) 离开学习桌即暂停 + 瞬时状态收敛；普通成功态静默、登录要求保留
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const player = page.elements.playerStage;
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-page", sub_title: "退出暂停", can_stream: true });
    assert.equal(page.elements.playerEvidence.textContent, "", "普通成功态不再展示“可在线播放”");
    assert.equal(page.visibleText().includes("可在线播放"), false, "成功态文案闭集");
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-page", sub_title: "退出暂停", can_stream: false });
    assert.equal(page.elements.playerEvidence.textContent, "需要登录", "登录要求保留");
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-page", sub_title: "退出暂停", can_stream: true });
    player.play();
    player.dispatchEvent(new Event("play"));
    page.elements.playerCtrlMute.click(); /* 展开音量面 */
    assert.equal(page.elements.playerVolume.dataset.open, "true");
    window.dispatchEvent(new CustomEvent("courselens:page", { detail: "settings" }));
    assert.equal(player.paused, true, "离开学习桌到设置页立即暂停");
    assert.equal(page.elements.playerVolume.dataset.open, "false", "音量面瞬时态收敛");
    assert.equal(page.elements.playerStageShell.dataset.chrome, "visible", "控制台回到可见");
    // 字标返回同样广播 courselens:page（detail=study）：学习桌已隐藏，音频不得续放
    player.play();
    player.dispatchEvent(new Event("play"));
    window.dispatchEvent(new CustomEvent("courselens:page", { detail: "study" }));
    assert.equal(player.paused, true, "字标返回学习页同样立即暂停");
    cleanup();
    window.dispatchEvent(new CustomEvent("courselens:page", { detail: "settings" }));
  }

  // G) 保存成功广播进度事件（讲次卡同源事实）；载荷缺省回退请求值
  {
    const page = createPage();
    const store = createStore();
    const cleanup = await installPlayerCore(store);
    const watchEvents = [];
    const onWatch = (event) => watchEvents.push(event.detail);
    window.addEventListener("courselens:watch-progress", onWatch);
    const player = page.elements.playerStage;
    store.set("activeLecture", { course_id: "c-restore", sub_id: "s-save", sub_title: "保存广播", can_stream: true });
    player.duration = 600;
    player.currentTime = 61;
    player.dispatchEvent(new Event("pause"));
    await settle();
    assert.equal(watchEvents.length, 1, "暂停保存成功恰好广播一次");
    assert.deepEqual(watchEvents[0], {
      sub_id: "s-save", course_id: "c-restore",
      position_seconds: 61, duration_seconds: 600, completed: false,
    }, "广播载荷是讲次卡可直用的闭集字段");
    window.removeEventListener("courselens:watch-progress", onWatch);
    cleanup();
  }
}

/* ---- B1 根修钉（SAVEPROGRESS-GUARD）：进度写侧讲次绑定——切讲清零
   「继续播放」的写侧防御。读侧归属钉在 verifyWatchProgressRestore；
   此处钉写侧三腿：
   ① 切讲即取消旧讲次遗留的 10s 进度防抖（timer 永不在新讲次身份下到点）；
   ② 假设防抖未被取消（旧行为）到点触发：新讲次元数据未就绪（真实 load()
      复位 duration=NaN）、currentTime=0——写侧守卫必须拒绝这次写入，
      绝不把 0 写进新讲次、清掉服务器上的续播点；
   ③ 元数据就绪后同讲次正常暂停保存照常落库（守卫不过度拒真）。 ---- */
async function verifyWatchProgressLectureBinding() {
  const page = createPage();
  const store = createStore();
  const cleanup = await installPlayerCore(store);
  const player = page.elements.playerStage;
  store.set("activeLecture", { course_id: "c-bind", sub_id: "s-bind-a", sub_title: "绑定A", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  /* 捕获（不执行）进度防抖：与桩前提一致——真实 10s 在毫秒级测试进程内不会
     到点，因此捕获后手动裁定其命运，零真实等待。 */
  const scheduled = [];
  const originalSetTimeout = windowTarget.setTimeout;
  const originalClearTimeout = windowTarget.clearTimeout;
  windowTarget.setTimeout = (callback, ms) => {
    const entry = { callback, ms, cleared: false };
    scheduled.push(entry);
    return entry;
  };
  windowTarget.clearTimeout = (id) => {
    if (id && typeof id === "object") id.cleared = true;
  };
  try {
    const postsBefore = progressPostBodies.length;
    player.writePlaybackTime(180);
    player.dispatchEvent(new Event("timeupdate"));
    assert.equal(scheduled.length, 1, "timeupdate 恰好挂一个进度防抖");
    assert.equal(scheduled[0].ms, 10000, "进度防抖周期 10s");
    store.set("activeLecture", { course_id: "c-bind", sub_id: "s-bind-b", sub_title: "绑定B", can_stream: true });
    await settle();
    assert.equal(scheduled[0].cleared, true, "切讲即取消旧讲次遗留的进度防抖");
    /* 旧行为假设（防抖未被取消）下到点触发：此刻新讲次媒体元数据未就绪。
       旧行为会 POST {sub_id: s-bind-b, position_seconds: 0}——正是切讲清零
       「继续播放」的那一笔数据损失写。 */
    player.duration = Number.NaN;
    player.currentTime = 0;
    scheduled[0].callback();
    await settle();
    assert.equal(progressPostBodies.length, postsBefore, "遗留防抖的到点写入被写侧守卫拒绝");
    /* 元数据就绪后同讲次正常暂停保存：守卫放行，载荷闭集 */
    player.duration = 600;
    player.dispatchEvent(new Event("loadedmetadata"));
    player.writePlaybackTime(240);
    player.dispatchEvent(new Event("pause"));
    await settle();
    assert.deepEqual(progressPostBodies.at(-1), {
      sub_id: "s-bind-b", position_seconds: 240, duration_seconds: 600,
      playback_rate: 1, completed: false,
    }, "当前讲次绑定一致+元数据就绪：暂停保存照常落库");
  } finally {
    windowTarget.setTimeout = originalSetTimeout;
    windowTarget.clearTimeout = originalClearTimeout;
  }
  cleanup();
}

/* ---- 直播追帧层（G9-C）：C1 liveSync/maxLatency 配对、C2 「回到直播中」pill、
   C3 渐进追帧与用户让位、C4 lowLatencyMode 现值钉（取舍留档见结果文件）。 ---- */

/* ---- 未聚焦态交互合同（PLAYER-INTERACT-REPAIR-1）----
   播放态（学习桌可见）但播放器未被聚焦时，四条老交互必须可用：
   点击画面暂停、←/→ seek、长按右 2 倍速、双击全屏。守卫只让位
   「输入框/按钮/菜单等可交互元素聚焦」，绝不要求控制器先被聚焦；
   学习桌不可见（返回选择面/切页）时页面级快捷键一律让位。
   同一合同钉控制器选中态政策：鼠标按下不落焦点（零空格误触发）。 */
async function verifyUnfocusedInteractionContract() {
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const shell = page.elements.playerStageShell;
  const status = page.elements.playerCtrlStatus;
  const desk = page.elements.studyDesk;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-unf", sub_id: "sub-unf", sub_title: "未聚焦讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));

  // 页面级未聚焦 target：工作区主体（不在 .player-stage-shell 内、非可交互元素）
  const pageTarget = new FakeElement("workspace-main");
  pageTarget.tagName = "div";

  // ① 空格播放/暂停（未聚焦）
  const spacePlay = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(spacePlay);
  assert.equal(player.paused, false, "未聚焦空格开始播放");
  assert.equal(spacePlay.defaultPrevented, true, "未聚焦空格由播放器消费");
  const spacePause = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(spacePause);
  assert.equal(player.paused, true, "未聚焦空格暂停");

  // ② 点击画面暂停/继续（target=video；控制台点击必须让位）
  shell.dispatchEvent(new Event("click"));
  assert.equal(player.paused, false, "点击画面开始播放");
  shell.dispatchEvent(new Event("click"));
  assert.equal(player.paused, true, "点击画面暂停");
  const controlsClick = new Event("click", { bubbles: true });
  Object.defineProperty(controlsClick, "target", { value: page.elements.playerCtrlPlay });
  shell.dispatchEvent(controlsClick);
  assert.equal(player.paused, true, "控制台上的点击不切换播放");

  // ③ ←/→ seek（未聚焦；B 站式 keyup 裁决：右短按松开才 seek）
  player.currentTime = 100;
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: pageTarget }));
  assert.equal(player.currentTime, 100, "按下不立即 seek");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.currentTime, 105, "未聚焦右方向 seek +5s");
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowLeft", { target: pageTarget }));
  assert.equal(player.currentTime, 100, "未聚焦左方向 seek -5s");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowLeft"));

  // ④ 长按右方向临时 3 倍速（未聚焦；全程不先跳 5 秒）
  window.dispatchEvent(playerKeyEvent("keydown", "ArrowRight", { target: pageTarget }));
  await holdWait();
  assert.equal(player.currentTime, 100, "长按期间位置不动");
  assert.equal(player.playbackRate, 3, "未聚焦长按右进入 3 倍速（N5PR-P2）");
  window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
  assert.equal(player.playbackRate, 1, "松开恢复所选速度");
  assert.equal(player.currentTime, 100, "倍速收口不追加 seek");

  // ⑤ 双击画面全屏（未聚焦；全屏仍由 shell 在手势内请求）
  let unfocusedFsRequests = 0;
  shell.requestFullscreen = () => {
    unfocusedFsRequests += 1;
    return Promise.resolve();
  };
  shell.dispatchEvent(new Event("dblclick"));
  assert.equal(unfocusedFsRequests, 1, "双击画面进入全屏");
  const controlsDblclick = new Event("dblclick", { bubbles: true });
  Object.defineProperty(controlsDblclick, "target", { value: page.elements.playerCtrlPlay });
  shell.dispatchEvent(controlsDblclick);
  assert.equal(unfocusedFsRequests, 1, "控制台上的双击不触发全屏");

  // ⑥ 控制器选中态政策：shell 内鼠标按下不落焦点（零空格误触发的根源）
  const deckMousedown = new Event("mousedown", { cancelable: true });
  Object.defineProperty(deckMousedown, "target", { value: page.elements.playerCtrlPlay });
  shell.dispatchEvent(deckMousedown);
  assert.equal(deckMousedown.defaultPrevented, true, "控制器按钮按下不落焦点");
  const videoMousedown = new Event("mousedown", { cancelable: true });
  shell.dispatchEvent(videoMousedown);
  assert.equal(videoMousedown.defaultPrevented, true, "画面区按下同样不落焦点");
  /* UXP1 走查实锤修复：原生倍速下拉由 mousedown 默认行为打开——select 按下
     必须让位（不 preventDefault），按钮照旧拦截 */
  const speedMousedown = new Event("mousedown", { cancelable: true });
  Object.defineProperty(speedMousedown, "target", { value: page.elements.playerCtrlSpeed });
  shell.dispatchEvent(speedMousedown);
  assert.equal(speedMousedown.defaultPrevented, false, "倍速 select 按下让位（下拉可展开）");
  const speedTag = { closest: (selector) => (String(selector).includes("select") ? {} : null) };
  const speedMousedown2 = new Event("mousedown", { cancelable: true });
  Object.defineProperty(speedMousedown2, "target", {
    value: { closest: (selector) => (String(selector).includes("select") ? speedTag : null) },
  });
  shell.dispatchEvent(speedMousedown2);
  assert.equal(speedMousedown2.defaultPrevented, false, "closest 路径同样让位");

  // ⑦ 学习桌不可见（返回选择面/切页）后，页面级快捷键让位
  desk.hidden = true;
  const deskHiddenBaseline = player.currentTime;
  const hiddenDeskSeek = playerKeyEvent("keydown", "ArrowRight", { target: pageTarget });
  window.dispatchEvent(hiddenDeskSeek);
  assert.equal(player.currentTime, deskHiddenBaseline, "学习桌不可见时方向键不归播放器");
  const hiddenDeskSpace = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(hiddenDeskSpace);
  assert.equal(player.paused, true, "学习桌不可见时空格不唤醒播放");
  desk.hidden = false;

  // ⑧ 可交互元素聚焦仍让位（既有合同不回退）
  const editableTarget = new FakeElement("editable-field");
  editableTarget.closest = () => editableTarget;
  const editableSeek = playerKeyEvent("keydown", "ArrowRight", { target: editableTarget });
  window.dispatchEvent(editableSeek);
  assert.equal(player.currentTime, deskHiddenBaseline, "焦点在可交互元素上时让位");
  assert.equal(editableSeek.defaultPrevented, false, "让位时不阻止默认行为");

  cleanup();
}

/* ---- 时间轴视觉几何契约（静态）：可见轨端点 = 0%/100% thumb 圆心 ----
   单一几何常量 --player-thumb-size 在 pages.css；可见轨与已播填充的内缩规则
   已统一折回 pages.css 基础选择器（components.css 的特异性覆写退场），
   键盘焦点环、forced-colors 与 44px 命中区不得退化。 */
function verifyTimelineGeometryContract() {
  const pagesCss = readFileSync(new URL("../frontend/styles/pages.css", import.meta.url), "utf8");
  const componentsCss = readFileSync(new URL("../frontend/styles/components.css", import.meta.url), "utf8");
  const thumbSize = Number(pagesCss.match(/\.player-timeline\s*\{[^}]*--player-thumb-size:\s*([\d.]+)px/)?.[1]);
  assert.ok(thumbSize > 0, "拇指直径几何常量存在");
  const fill = pagesCss.match(/\.player-timeline-fill\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(fill.includes("left: calc(var(--player-thumb-size) / 2)"), "填充左缘内缩拇指半径");
  assert.ok(fill.includes("width: calc((100% - var(--player-thumb-size)) * var(--play-ratio"), "填充宽度 = 行程 × 进度比");
  const track = pagesCss.match(/\.player-timeline::before\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(track.includes("left: calc(var(--player-thumb-size") && track.includes("/ 2)"), "可见轨左端内缩 thumb 半径（0% 端点=圆心）");
  assert.ok(track.includes("right: calc(var(--player-thumb-size") && track.includes("/ 2)"), "可见轨右端内缩 thumb 半径（100% 端点=圆心）");
  assert.ok(!componentsCss.includes(".player-controls .player-timeline::before"), "几何规则不再以特异性覆写分置两文件");
  assert.ok(pagesCss.includes(".player-ctrl-timeline:focus-visible::-webkit-slider-thumb"), "键盘焦点环保留在 thumb 上");
  assert.ok(componentsCss.includes(".player-timeline::before { background: GrayText; }"), "forced-colors 可见轨保持");
  assert.ok(pagesCss.includes(".player-timeline { height: 44px; }"), "窄宽度 44px 命中区保持");
  assert.ok(pagesCss.includes(".player-ctrl-timeline { height: 44px; }"), "窄宽度时间轴命中区保持");
  /* PLAYER-INTERACT-REPAIR-1：全局 input 规则（padding/border/min-height）若未被
     滑杆承袭复位，thumb 行程比几何模型短 ~2×(padding+border)，已播/未播分界线
     与滑块圆心系统性错位（两端各 ±10px、中点归零的剪切漂移）。 */
  const timelineRule = pagesCss.match(/\.player-ctrl-timeline\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(timelineRule.includes("padding: 0") && timelineRule.includes("border: 0"),
    "时间轴滑杆必须复位全局 input padding/border（分界线重合根因）");
  assert.ok(timelineRule.includes("min-height: 0"), "时间轴滑杆必须复位全局 input min-height");
  const volumeRule = pagesCss.match(/\.player-ctrl-volume\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(volumeRule.includes("padding: 0") && volumeRule.includes("border: 0"),
    "音量滑杆同样复位全局 input padding/border（滑杆族同病同修）");
  const shellRule = pagesCss.match(/\.player-stage-shell\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(shellRule.includes("user-select: none"), "控制面禁文本选中（点击不产生选择高亮）");
}

/* FOCUS-DRIFT-1（D-20261007-03 复现剧本全步重放 + 键盘可达三样硬门钉）：
   ①逐控件点击/聚焦后快捷键存活矩阵——button/a 仅激活键（Space/Enter）让位，
   方向/J/L 透传播放快捷键（点击任意按钮后快捷键即刻存活）；②鼠标指针激活
   （pointer 模态）点击后焦点即刻归还，键盘模态（data-input=key）Tab 焦点
   零回退；③激活键硬门：聚焦按钮上 Space/Enter 绝不被播放器抢消费（原生
   激活保留）；④字段/滑杆守卫不变（编辑态整块让位）；⑤role=tab/option/menu/
   listbox 方向键整块让位（roving 语义不与快捷键双触发）；⑥任一模态 dialog
   打开期间快捷键让位、`?` 键位开关豁免；⑦滚轮悬停让位保持原全集（悬在
   按钮/滑杆上不动音量）。focus-visible 环（accessibility.css data-input
   政策）与 Tab 序（正 tabindex=0+roving tabindex）由 workbench/静态钉承担。 */
async function verifyFocusDriftGuardBehavior() {
  const page = createPage();
  const store = createStore();
  const player = page.elements.playerStage;
  const status = page.elements.playerCtrlStatus;
  const desk = page.elements.studyDesk;
  const cleanup = await installPlayerCore(store);
  store.set("activeLecture", { course_id: "course-fd", sub_id: "sub-fd", sub_title: "焦点纪律讲次", can_stream: true });
  player.duration = 600;
  player.dispatchEvent(new Event("loadedmetadata"));
  assert.equal(desk.hidden, false, "学习桌可见（合成壳默认），快捷键待命");

  const controlButtons = [
    "player-ctrl-play", "player-ctrl-mute", "player-ctrl-subtitle",
    "player-ctrl-pip", "player-ctrl-theatre", "player-ctrl-fullscreen",
    "player-subtitle-chip", "player-ctrl-bookmark", "player-insight-open", "insight-erase",
  ];

  // ① 种子病例矩阵：聚焦任意控件按钮，方向/J/L 透传、激活键走原生
  player.currentTime = 100;
  player.pause();
  for (const buttonId of controlButtons) {
    const button = page.elements[buttonId];
    button.tagName = "button";
    button.focus();
    assert.equal(document.activeElement, button, `${buttonId} 聚焦驻留（复现前置态）`);
    // 方向键透传：右短按 keyup 裁决 +5s
    const arrow = playerKeyEvent("keydown", "ArrowRight", { target: button });
    window.dispatchEvent(arrow);
    assert.equal(arrow.defaultPrevented, true, `${buttonId} 聚焦时右方向被快捷键消费（透传生效）`);
    window.dispatchEvent(playerKeyEvent("keyup", "ArrowRight"));
    assert.equal(player.currentTime, 105, `${buttonId} 聚焦时右方向仍 seek +5s（焦点不再拦快捷键）`);
    // J/L 透传
    window.dispatchEvent(playerKeyEvent("keydown", "l", { target: button }));
    assert.equal(player.currentTime, 115, `${buttonId} 聚焦时 L 仍快进 10s`);
    window.dispatchEvent(playerKeyEvent("keydown", "j", { target: button }));
    assert.equal(player.currentTime, 105, `${buttonId} 聚焦时 J 仍快退 10s`);
    // 激活键硬门：Space/Enter 不被播放器消费（原生激活保留）
    player.pause();
    const space = playerKeyEvent("keydown", " ", { target: button });
    window.dispatchEvent(space);
    assert.equal(space.defaultPrevented, false, `${buttonId} 聚焦时 Space 留给原生激活（激活键硬门）`);
    assert.equal(player.paused, true, `${buttonId} 聚焦时 Space 不触发播放切换（无双触发）`);
    const enter = playerKeyEvent("keydown", "Enter", { target: button });
    window.dispatchEvent(enter);
    assert.equal(enter.defaultPrevented, false, `${buttonId} 聚焦时 Enter 留给原生激活`);
    button.blur();
    player.currentTime = 100;
  }

  // ② 鼠标指针激活后焦点即刻归还；键盘模态 Tab 焦点零回退
  //（fake DOM 无事件传播：学习桌捕获监听用 target 覆写派发，与真实点击
  //  的 desk 捕获路径同参——target=按钮、listener=desk。）
  const clickOnDesk = (button) => {
    const click = new Event("click", { bubbles: true });
    Object.defineProperty(click, "target", { value: button });
    desk.dispatchEvent(click);
  };
  for (const modality of ["pointer", "key"]) {
    document.documentElement.dataset.input = modality;
    for (const buttonId of ["player-ctrl-play", "player-ctrl-fullscreen"]) {
      const button = page.elements[buttonId];
      button.focus();
      clickOnDesk(button);
      if (modality === "pointer") {
        assert.equal(document.activeElement, null, `${buttonId} 指针点击后焦点归还页面级（快捷键即刻存活）`);
      } else {
        assert.equal(document.activeElement, button, `${buttonId} 键盘模态点击不抢 Tab 焦点（键盘可达硬门）`);
      }
      button.blur();
    }
  }
  document.documentElement.dataset.input = "pointer";

  // ③ 字段/滑杆守卫不变：input 聚焦时整块让位（编辑态语义）
  const slider = page.elements.playerCtrlTimeline;
  slider.focus();
  player.currentTime = 100;
  player.pause();
  const sliderSpace = playerKeyEvent("keydown", " ", { target: slider });
  window.dispatchEvent(sliderSpace);
  assert.equal(sliderSpace.defaultPrevented, false, "滑杆聚焦 Space 不被 window 通道消费（spacekick 专属通道接管）");
  const sliderArrow = playerKeyEvent("keydown", "ArrowRight", { target: slider });
  window.dispatchEvent(sliderArrow);
  assert.equal(player.currentTime, 100, "滑杆聚焦方向键整块让位（滑杆自有语义）");
  assert.equal(sliderArrow.defaultPrevented, false, "滑杆方向键不被播放器 preventDefault");
  slider.blur();

  // ④ role=tab/option/menu/listbox 方向键整块让位（roving 语义不双触发）
  for (const role of ["tab", "option", "menu", "listbox"]) {
    const roving = new FakeElement(`roving-${role}`);
    roving.tagName = "button";
    roving.setAttribute("role", role);
    player.currentTime = 100;
    const rovingArrow = playerKeyEvent("keydown", "ArrowRight", { target: roving });
    window.dispatchEvent(rovingArrow);
    assert.equal(player.currentTime, 100, `role=${role} 方向键整块让位（自有导航语义）`);
  }

  // ⑤ 模态 dialog 闸：任一 dialog 打开期间快捷键让位；`?` 键位开关豁免
  const pageTarget = new FakeElement("workspace-main");
  pageTarget.tagName = "div";
  const keysDialog = page.elements.playerKeysDialog;
  player.pause();
  player.currentTime = 100;
  keysDialog.open = true;
  const dialogSpace = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(dialogSpace);
  assert.equal(player.paused, true, "键位弹窗打开时空格不隔模态切播放");
  assert.equal(dialogSpace.defaultPrevented, false, "模态期间快捷键未消费");
  const dialogL = playerKeyEvent("keydown", "l", { target: pageTarget });
  window.dispatchEvent(dialogL);
  assert.equal(player.currentTime, 100, "键位弹窗打开时 L 不隔模态 seek");
  window.dispatchEvent(playerKeyEvent("keydown", "?", { target: pageTarget }));
  assert.equal(keysDialog.open, false, "? 豁免模态闸：键位弹窗可再按 ? 关闭");
  window.dispatchEvent(playerKeyEvent("keydown", "?", { target: pageTarget }));
  assert.equal(keysDialog.open, true, "? 可再打开键位弹窗");
  keysDialog.open = false;
  const styleDialog = page.elements.playerSubtitleStyleDialog;
  styleDialog.open = true;
  const styleSpace = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(styleSpace);
  assert.equal(player.paused, true, "字幕样式弹窗打开时空格不隔模态切播放");
  styleDialog.open = false;
  const afterDialogSpace = playerKeyEvent("keydown", " ", { target: pageTarget });
  window.dispatchEvent(afterDialogSpace);
  assert.equal(afterDialogSpace.defaultPrevented, true, "弹窗全关后空格回到播放器（闸不再拦截）");
  assert.equal(player.paused, false, "弹窗全关后空格恢复播放切换");
  player.pause();

  // ⑥ 滚轮悬停让位保持原全集：悬在按钮/滑杆上滚轮不动音量，悬在画面上动
  const shell = page.elements.playerStageShell;
  player.volume = 0.8;
  shell.dispatchEvent(wheelEvent(-120, { target: page.elements.playerCtrlPlay }));
  assert.equal(player.volume, 0.8, "悬停按钮上滚轮不动音量（悬停让位全集保持）");
  shell.dispatchEvent(wheelEvent(-120, { target: page.elements.playerCtrlTimeline }));
  assert.equal(player.volume, 0.8, "悬停滑杆上滚轮不动音量（滑杆语义保护）");
  shell.dispatchEvent(wheelEvent(-120, { target: player }));
  assert.equal(Math.round(player.volume * 100) / 100, 0.85, "画面上滚轮仍调音量 +5%");

  // ⑦ 归还监听对称清理
  cleanup();
}


await verifyLiveRoomBehavior();
await verifyPlayerBehavior();
await verifyMediaSourceFailureRefinement();
await verifySubtitleOverlayBehavior();
await verifyPlayerControlsBehavior();
await verifyPlaybackRateMemoryBehavior();
await verifyPlayerOsdBehavior();
await verifyPlayerBufferVisualBehavior();
await verifySubtitleChipBehavior();
await verifyNotUnderstoodBehavior();
await verifySubtitleStyleBehavior();
await verifyTimelineLabelsBehavior();
await verifyAssessMarkersBehavior();
await verifyInsightBehavior();
await verifyPlayerChromeBehavior();
await verifyCampusConnectionConsumerForPlayback();
await verifyRecoveryContinuity();
await verifyEnqueueCloudSetupGatePin();
await verifyLectureSwitchActionGuard();
await verifyWatchProgressRestore();
await verifyWatchProgressLectureBinding();
await verifyUnfocusedInteractionContract();
await verifyFocusDriftGuardBehavior();
/* U7①②③：倍速扩档闭集（0.5–3.0）+ 缓冲层 + 章节标记容器 */
{
  const indexHtml = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
  const selectMatch = indexHtml.match(/<select id="player-ctrl-speed"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(selectMatch, "倍速选择器存在");
  const options = [...selectMatch[1].matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(
    options,
    ["0.5", "0.75", "1", "1.25", "1.5", "1.75", "2", "2.5", "3"],
    "倍速档位闭集 0.5-3.0",
  );
  assert.ok(indexHtml.includes('id="player-timeline-buffered"'), "缓冲可视化层存在");
  assert.ok(indexHtml.includes('id="player-timeline-markers"'), "章节标记容器存在");
  /* N5PR-P2：步进集与 select 档位集 JSON.stringify 恒等（九档含 1.75） */
  const playerCoreSource = familySource("player-core");
  const stepsMatch = playerCoreSource.match(/PLAYER_SPEED_STEPS = Object\.freeze\(\[([^\]]*)\]\)/);
  assert.ok(stepsMatch, "PLAYER_SPEED_STEPS 常量存在");
  const steps = stepsMatch[1].split(",").map((value) => String(Number(value.trim())));
  assert.deepEqual(steps, options, "</> 步进集与 select 九档恒等（N5PR-P2）");
  assert.ok(playerCoreSource.includes("PLAYER_HOLD_RATE = 3"), "长按临时倍速 3x");
  /* SWEEPFIX-1 C2：play() 竞态消音面全覆盖——标记点/时间轴标签/考核刻度/
     章节标记四条「跳回并续播」路径统一走 playQuietly（AbortError 族消音，
     其余拒绝原样保留），裸抛 play() 不复存在 */
  assert.equal(/void player\.play/.test(playerCoreSource), false, "player-core 无裸抛 play()（C2）");
  assert.ok((playerCoreSource.match(/playQuietly\(player\)/g) || []).length >= 5, "playQuietly 定义+四调用点在位");
  assert.ok(playerCoreSource.includes('"AbortError"'), "消音面=AbortError 闭集（零行为变化）");
  assert.ok(indexHtml.includes("按住 → 临时 3 倍速"), "帮助文案同步 3 倍速");
  /* N5PR-P3：OSD 静态钉——chromePinned 零 OSD 引用（显隐不参与钉住）；
     动效走 --motion-fast（reduced-motion 全局 .01ms 规则压平）；SR 单源 */
  const chromePinnedBody = playerCoreSource.match(/function chromePinned\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  assert.ok(chromePinnedBody.length > 0 && !chromePinnedBody.toLowerCase().includes("osd"),
    "chromePinned 不读 OSD 状态（1500ms 恒等钉面隔离）");
  assert.ok(playerCoreSource.includes("showPlayerOsdHint(message, osdMs)"), "announce 双通道同源（U② 提示延时可分化）");
  const pagesCssSource = await readFile(new URL("../frontend/styles/pages.css", import.meta.url), "utf8");
  assert.ok(pagesCssSource.includes("player-osd-in var(--motion-fast)"), "pulse 动效 token 绑定 150ms");
  assert.ok(pagesCssSource.includes("player-osd-hint-in var(--motion-fast)"), "hint 动效 token 绑定 150ms");
  const accessibilityCssSource = await readFile(new URL("../frontend/styles/accessibility.css", import.meta.url), "utf8");
  assert.ok(accessibilityCssSource.includes("animation-duration: .01ms"), "reduced-motion 全局压平规则在位");
  assert.ok(indexHtml.includes('id="player-osd-center"') && indexHtml.includes('id="player-osd-hint"'), "OSD 节点存在");
  /* N5PR-P4 静态钉：hover 粗化 4→6px、命中区 32px 恒定、气泡/spinner 节点、
     300ms 防闪阈值、讲人话文案 */
  assert.ok(pagesCssSource.includes(".player-timeline:hover .player-timeline-fill"), "hover 粗化规则");
  assert.ok(pagesCssSource.includes("height: 6px"), "悬停可见轨 6px");
  const timelineHitBlock = pagesCssSource.match(/\.player-ctrl-timeline\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(timelineHitBlock.includes("height: 32px"), "命中区 32px 恒定");
  assert.ok(playerCoreSource.includes("PLAYER_SPINNER_DELAY_MS = 300"), "防闪阈值 300ms");
  assert.ok(indexHtml.includes("正在缓冲，内容没丢"), "缓冲文案讲人话");
  assert.ok(indexHtml.includes('id="player-timeline-bubble"'), "时间气泡节点");
  assert.ok(indexHtml.includes('id="player-stage-spinner"'), "中央缓冲指示节点");
  /* N5PR-P1：键位帮助行 +5（6→11）；D2 +1（X）=12；U① D4 摘除（R）；
     USEROPS-1 +1（长按「字幕」钮=字幕样式入口提示行，13） */
  const keysRows = [...indexHtml.matchAll(/<div class="player-keys-row">/g)].length;
  assert.equal(keysRows, 13, "键位帮助行 13 行（P1 +5、D2 +1、D4 已摘除、USEROPS-1 +1）");
  assert.ok(indexHtml.includes("<b>长按「字幕」钮</b><span>调整字幕样式（字号 / 背景深浅 / 贴底位置）</span>"), "字幕样式长按入口行");
  assert.ok(!indexHtml.includes("player-timeline-ab"), "A-B 区层节点已摘除");
  for (const row of [
    "<b>K</b><span>播放 / 暂停</span>",
    "<b>M</b><span>静音</span>",
    "<b>F</b><span>全屏</span>",
    "<b>C</b><span>字幕开关</span>",
    "<b>↑ / ↓</b><span>提高 / 降低音量</span>",
  ]) {
    assert.ok(indexHtml.includes(row), `帮助行存在：${row}`);
  }
  /* PLAYER-OPT-1 静态钉（20261008 夜）：SEEK-DRAG-1 拖动预览与 L96' 续播浮层
     的源码锁——超时闭集常量、浮层动态创建（零 index.html 静态节点）、
     CSS 同舞台恒暗族落点。 */
  assert.ok(playerCoreSource.includes("SEEK-DRAG-1"), "拖动预览根因注释在位");
  assert.ok(playerCoreSource.includes("RESUME_BANNER_TIMEOUT_MS = 12000"), "浮层 12s 超时闭集常量");
  assert.ok(playerCoreSource.includes("RESUME_AUTO_PLAY_MIN_SECONDS = 5"), "近开头不自动播阈值");
  assert.ok(playerCoreSource.includes("RESUME_AUTO_PLAY_TAIL_GUARD_SECONDS = 15"), "近结尾不自动播阈值");
  assert.ok(!indexHtml.includes("player-resume-banner"), "浮层零 index.html 静态节点（动态创建面）");
  assert.ok(pagesCssSource.includes(".player-resume-banner"), "浮层样式落点 pages.css");
  assert.ok(pagesCssSource.includes("var(--stage-surface)"), "舞台恒暗族 token 复用（零新视觉语言）");
}
verifyMediaContinuityStaticLocks();
verifyTimelineGeometryContract();
console.log("frontend playback/live recovery behavior passed");
