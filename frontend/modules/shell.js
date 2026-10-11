import { apiV3, postV3, UI_HINTS } from "./api.js";
import {
  $, clear, evidenceText, openOverlay, closeOverlay, setGlobalStatus,
  textElement, formatTime, connectionCodeHint,
  CONNECTION_ACTION_LABELS, CONNECTION_DEFAULT_ACTIONS, CONNECTION_STATE_TEXT,
  CONNECTION_SERVICE_LABELS,
  extractConnectionSnapshot, connectionViewFromSnapshot, connectionViewFromAuth,
} from "./ui.js";
import { refreshStatusCapsuleLabel } from "./home-overview.js";

const SESSION_ID = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
let authTimer = 0;
let latestAuth = null;

/* 会话心跳（BACKEND-DEATH-1②）：节拍周期取后端租约（300s）的 1/3。拍点由
   Dedicated Worker 产生——页面退到后台被定时器节流时，Worker 定时器照常运行；
   Worker 不可用（构造抛错/加载错/消息错）降级为页面定时器，且页面重新可见时
   立即补发一次心跳。调度器是纯状态机，定时器注入，便于行为级钉测。 */
export const HEARTBEAT_INTERVAL_MS = 100000;

export function createHeartbeatScheduler({ heartbeat, schedule, cancel, intervalMs = HEARTBEAT_INTERVAL_MS }) {
  let mode = "idle"; // idle | worker | page
  let worker = null;
  let timerId = 0;
  const fire = () => {
    try {
      heartbeat();
    } catch {
      /* 单拍失败交由后端租约兜底，不向页面抛错 */
    }
  };
  const releaseWorker = () => {
    if (!worker) return;
    try {
      worker.onmessage = null;
      worker.onerror = null;
      worker.onmessageerror = null;
    } catch { }
    try {
      worker.terminate();
    } catch { }
    worker = null;
  };
  const degradeToPage = () => {
    if (mode === "page") return;
    releaseWorker();
    mode = "page";
    timerId = schedule(fire, intervalMs);
  };
  return {
    get mode() { return mode; },
    /* candidate：真实 Worker 或测试桩；消息/错误处理器由调度器指派。 */
    attachWorker(candidate) {
      worker = candidate;
      worker.onmessage = (event) => {
        if (mode === "worker" && String(event?.data) === "heartbeat") fire();
      };
      worker.onerror = () => {
        if (mode === "worker") degradeToPage();
      };
      worker.onmessageerror = () => {
        if (mode === "worker") degradeToPage();
      };
      mode = "worker";
      try {
        worker.postMessage({ type: "start", intervalMs });
      } catch {
        degradeToPage();
      }
    },
    startPageFallback() {
      if (mode === "idle") degradeToPage();
    },
    onVisible() {
      if (mode !== "idle") fire();
    },
    stop() {
      if (timerId) {
        cancel(timerId);
        timerId = 0;
      }
      releaseWorker();
      mode = "idle";
    },
  };
}

const CONN_TEXT = {
  fudan: {
    ready: "复旦会话已连接，课程与搜索可用",
    /* AS4-U2：恢复期（含 restoring）说「登录中」——与呼吸点配套，见 renderConn。 */
    checking: "复旦会话登录中…",
    action: "复旦会话需要处理，请重新认证",
    error: "复旦登录未完成，请检查账号状态"
  },
  github: {
    ready: "GitHub 远程连接正常",
    checking: "正在确认远程连接",
    action: "GitHub 需要授权：授权后 Worker 将自动初始化",
    /* COPYUP-1：error=后端如实上报的 GitHub 故障态，才配代理指引；文案进
       api.js UI_HINTS 闭集表（unreachable=本地服务不可达，语义不同——
       不嫁祸 GitHub，也不加代理话，见 fetchGithubConnState 的 O1 纪律）。 */
    error: UI_HINTS.github_conn_error,
    /* O1-F2（N6F 移交）：后端闭集 offline=「从未配置/未连接」阻塞件——对
       首用学生说「失败」是失实指控；安静灰点+开启指引。 */
    off: "GitHub 未连接：在设置里开启远程连接后即可使用",
    /* O1：本机离线与本地服务不可达是另外两种真实态，分开说。 */
    offline: "网络未连接：请检查校园网或本机网络后重试",
    unreachable: "本地服务暂未连上，无法确认远程状态，正在自动重试"
  }
};

/* 断连横幅（BACKEND-DEATH-1③ / AS1）：连续 N 次 /api/health 有界探测失败即
   判定界面暂未连上本地服务——顶部持久横幅诚实轻量提示（自动重试，不责备、
   不指挥重启）并禁用连接动作按钮；任一次成功自动撤销。
   监视器是纯状态机，探测与回调注入，便于行为级钉测。 */
export const SERVICE_DISCONNECT_TEXT = "界面暂时没连上本地服务，正在自动重试…";
export const SERVICE_HEALTH_FAILURE_THRESHOLD = 3;
/* POLISH-O13 O1（零呆等三律·等待可视化）：确认前（连续失败未满阈值）不再静默——
   首拍失败即亮「确认中」轻提示，疑似期加速拍缩短确认窗（最坏 ~30s → ~16s）。
   文案闭集：轻提示不宣称「没连上」（尚未证实），只说正在确认；确认后升级为
   SERVICE_DISCONNECT_TEXT。 */
export const SERVICE_PROBING_TEXT = "本地服务暂时没响应，正在确认连接…";
export const SERVICE_PROBE_FAST_DELAY_MS = 3000;
export const SERVICE_PROBE_IDLE_DELAY_MS = 10000;
export const SERVICE_PROBE_FAST_TICK_MAX = 5;

/* 疑似/断开期共用的拍间隔：前 N 拍加速（对齐 down 期既有 3s 快拍语义），
   之后回常速轮询；恢复健康即清零。纯函数，便于行为级钉测。 */
export function nextServiceProbeDelayMs({ troubled, fastTicks }) {
  if (!troubled) return { delayMs: SERVICE_PROBE_IDLE_DELAY_MS, fastTicks: 0 };
  const nextTicks = fastTicks + 1;
  return {
    delayMs: nextTicks <= SERVICE_PROBE_FAST_TICK_MAX ? SERVICE_PROBE_FAST_DELAY_MS : SERVICE_PROBE_IDLE_DELAY_MS,
    fastTicks: nextTicks,
  };
}

/* DISPATCH-HEALTH-1（第四十案）：一次探测失败不即时计失败，先补探一次。
   现场实证：派发字幕任务前后页面亮「本地服务已断开」，而服务端全程健康、
   零关停痕迹、零 mid-request 断连——浏览器连接池瞬时饥饿（常驻 SSE + 媒体
   连接 + 轮询争用 6 条同源连接）或标签页被节流时，单次 fetch 会在 5s 内被
   abort，三次连续误判即成横幅。补探只针对「瞬时」：真断连（连接被拒）两次
   都是即失败，检出耗时几乎不变；hung 住的服务则两次都吃满超时。 */
export const SERVICE_HEALTH_PROBE_TIMEOUT_MS = 5000;
export const SERVICE_HEALTH_PROBE_RETRY_DELAY_MS = 1200;

export function createRetryingHealthProbe({
  probeOnce,
  retryDelayMs = SERVICE_HEALTH_PROBE_RETRY_DELAY_MS,
  sleep = (ms) => new Promise((resolve) => globalThis.setTimeout(resolve, ms)),
}) {
  return async () => {
    try {
      if (await probeOnce()) return true;
    } catch {
      /* 单次探测的异常按失败处理，交给补探判定 */
    }
    await sleep(retryDelayMs);
    try {
      return Boolean(await probeOnce());
    } catch {
      return false;
    }
  };
}

export function createServiceHealthMonitor({ probe, threshold = SERVICE_HEALTH_FAILURE_THRESHOLD, onDisconnect, onRecover }) {
  let failures = 0;
  let down = false;
  let ticking = false;
  return {
    get failures() { return failures; },
    get down() { return down; },
    /* 补探（上面）让最坏一拍长于 10s 轮询间隔，重叠的 tick 会把同一次饥饿
       记成两次失败；在飞期间直接略过本次拍点，计数语义仍是「每拍至多一次」。 */
    async tick() {
      if (ticking) return;
      ticking = true;
      try {
        let healthy = false;
        try {
          healthy = Boolean(await probe());
        } catch {
          healthy = false;
        }
        if (healthy) {
          failures = 0;
          if (down) {
            down = false;
            if (onRecover) onRecover();
          }
          return;
        }
        failures += 1;
        if (!down && failures >= threshold) {
          down = true;
          if (onDisconnect) onDisconnect();
        }
      } finally {
        ticking = false;
      }
    },
  };
}

/* 三态焦点政策（NIGHT5-U1）：把最近一次真实输入的模态记在 <html data-input>
   （keydown=key / pointerdown=pointer），accessibility.css 据此把指针态聚焦的
   环归零——Chromium 的 focus-visible「键盘用后粘滞」启发式会把键盘后的一次
   指针点击仍判成键盘来源；Tab 恒环不受影响。纯状态机，注入面便于行为级钉测。 */
export function createInputModalityTracker({ surface = window, root = document.documentElement } = {}) {
  const handleKeydown = () => { root.dataset.input = "key"; };
  const handlePointerdown = () => { root.dataset.input = "pointer"; };
  return {
    get mode() { return root.dataset.input || ""; },
    attach() {
      surface.addEventListener("keydown", handleKeydown, { capture: true });
      surface.addEventListener("pointerdown", handlePointerdown, { capture: true });
    },
    detach() {
      surface.removeEventListener("keydown", handleKeydown, { capture: true });
      surface.removeEventListener("pointerdown", handlePointerdown, { capture: true });
    },
  };
}

/* POLISH-O13 O1：三态横幅——down=确认断开（既有语义：danger 色全文+禁用连接
   动作钮）；probing=疑似（首拍失败，轻提示不禁用任何钮）；off=隐藏。
   down/probing 旧调用形态（单布尔参）保持不变。data-state 驱动 CSS 分档，
   文本写挂载点（同串不重写，避免读屏对 aria-live 重复播报）。 */
export function applyServiceDisconnectState(down, { probing = false } = {}) {
  const banner = $("service-banner");
  const state = down ? "down" : (probing ? "probing" : "off");
  if (banner) {
    banner.hidden = state === "off";
    banner.dataset.state = state;
    const text = state === "down" ? SERVICE_DISCONNECT_TEXT : SERVICE_PROBING_TEXT;
    if (state !== "off" && banner.textContent !== text) banner.textContent = text;
  }
  const disableActions = state === "down";
  document.querySelectorAll("button[data-service-action]").forEach((button) => {
    button.disabled = disableActions;
  });
}

/* P3-2（D14）：页题闭集映射——页名词与静态壳页头 h1/账户菜单既有页名词同源
   （禁漂移；onboarding 用菜单既有「新手引导」而非页内长句 h1）。 */
const PAGE_TITLES = Object.freeze({
  study: "学习",
  live: "直播",
  settings: "设置",
  data: "数据管理",
  onboarding: "新手引导",
});

export function selectPage(name) {
  document.querySelectorAll("[data-page]").forEach((node) => {
    const active = node.dataset.page === name;
    node.hidden = !active;
    node.classList.toggle("active", active);
  });
  /* 任务栏/Alt+Tab 可辨识：页题随页更新；未知页回落纯 CourseLens（不开新词） */
  const pageTitle = PAGE_TITLES[String(name || "")];
  document.title = pageTitle ? `${pageTitle} · CourseLens` : "CourseLens";
  $("workspace-main").focus({ preventScroll: true });
  window.dispatchEvent(new CustomEvent("courselens:page", { detail: name }));
}

/* P56-U1（第五十六案）：主题三态（自动/浅色/深色）。courselens.theme.v2=唯一
   偏好真源，旧 v1 一律不再读（现存 v1=dark 自此失效=本 bug 修复面）；v2 未设
   =「自动」：按本机时刻 19:00–07:00 深色、其余浅色，页面装配时解析，纯本地
   时刻零新外联。顶栏快捷钮/设置页显式选择=写 v2 显式值=退出自动。 */
export const THEME_PREF_KEY = "courselens.theme.v2";
export const THEME_AUTO_DARK_FROM_HOUR = 19;
export const THEME_AUTO_DARK_TO_HOUR = 7;

export function resolveThemeForMoment(now = new Date()) {
  const hour = now instanceof Date ? now.getHours() : new Date().getHours();
  return hour >= THEME_AUTO_DARK_FROM_HOUR || hour < THEME_AUTO_DARK_TO_HOUR ? "dark" : "light";
}

export function readThemePreference(storage = globalThis.localStorage) {
  const raw = storage?.getItem(THEME_PREF_KEY);
  return raw === "light" || raw === "dark" ? raw : "auto";
}

export function applyThemePreference(preference, { transition = true, now = new Date() } = {}) {
  const resolved = preference === "light" || preference === "dark" ? preference : resolveThemeForMoment(now);
  localStorage.setItem(THEME_PREF_KEY, preference);
  applyTheme(resolved, { transition });
  return resolved;
}

export function applyTheme(theme, { transition = true } = {}) {
  const apply = () => {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    /* F3（化身走查 20261008）：主题钮是状态钮——可达名固定为状态「深色主题」，
       aria-pressed=深色是否生效；读屏播报「深色主题，已按下/未按下」与视觉
       恒一致。此前可达名=动态动作「切换到浅色主题」+ pressed 随之翻转，播报
       状态与视觉相反。title 保留动作描述给明眼用户悬停（WAI-APG 状态钮口径：
       名词状态+pressed，动作语义走 tooltip）。 */
    const button = $("theme-toggle");
    button?.setAttribute("aria-pressed", String(dark));
    button?.setAttribute("aria-label", "深色主题");
    button?.setAttribute("title", `切换到${dark ? "浅色" : "深色"}主题`);
    const modeSelect = $("settings-theme-mode");
    if (modeSelect) modeSelect.value = readThemePreference();
    syncNativeTitlebarTheme(dark);
  };
  if (transition && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(apply);
  } else apply();
}

/* ---- 夜10-A APP-SHAPE-2：原生窗口壳集成 -------------------------------------
   浏览器形态没有 window.pywebview：全部入口先特性探测，探测不到=整个集成
   静默不装，页面行为与此前完全一致。桥就绪晚于页面装配，所以托盘开关/
   界面缓存行等 UI 都等桥到位后再点亮；失败一律诚实呈现，绝不假装成功。 */

export const NATIVE_WINDOW_FOCUS_EVENT = "courselens:native-window-focused";

function nativeApi() {
  return globalThis.window?.pywebview?.api ?? null;
}

function nativeApiReady(pollTimeoutMs = 8000) {
  return new Promise((resolve) => {
    const first = nativeApi();
    if (first) { resolve(first); return; }
    let settled = false;
    /* pywebviewready = pywebview 官方就绪事件（桥注入完成即派发）。事件监听
       不设时限：慢机器上桥晚到几十秒也能点亮；150ms 轮询只作为「事件在装配
       前已错过」的兜底，8 秒弃权后仍由事件接管。 */
    const onReady = () => settle(nativeApi());
    const settle = (api) => {
      if (settled) return;
      settled = true;
      window.removeEventListener("pywebviewready", onReady);
      resolve(api || null);
    };
    window.addEventListener("pywebviewready", onReady);
    const deadline = Date.now() + pollTimeoutMs;
    const tick = () => {
      if (settled) return;
      const api = nativeApi();
      if (api) { settle(api); return; }
      if (Date.now() > deadline) { settle(null); return; }
      window.setTimeout(tick, 150);
    };
    tick();
  });
}

function syncNativeTitlebarTheme(dark) {
  const api = nativeApi();
  if (!api?.set_titlebar_theme) return;
  try { void Promise.resolve(api.set_titlebar_theme(!!dark)).catch(() => { }); } catch { /* 桥不可达=保持系统默认标题栏 */ }
}

/* 外链闭集（T3）：点击跨源 http(s) 链接 → 系统默认浏览器；同源导航不动。
   window.open/target=_blank 已由 WebView2 NewWindowRequested 平台层放行
   （pywebview OPEN_EXTERNAL_LINKS_IN_BROWSER 默认开），这里补的是同页
   <a href> 跳转——原生窗里绝不让主窗被导航去外部网站。 */
function installExternalLinkCapture(root = document) {
  root.addEventListener(
    "click",
    (event) => {
      if (event.defaultPrevented || event.button !== 0) return;
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!anchor) return;
      let target;
      try { target = new URL(anchor.href, window.location.href); } catch { return; }
      if (!/^https?:$/.test(target.protocol)) return;
      if (target.origin === window.location.origin) return;
      event.preventDefault();
      const api = nativeApi();
      if (api?.open_external) {
        void Promise.resolve(api.open_external(target.href)).catch(() => { });
      } else {
        window.open(target.href, "_blank", "noopener");
      }
    },
    true,
  );
}

async function installTrayPreference(api) {
  // TRAY-FIX-1：关窗默认收进托盘；这里只剩「关闭窗口时退出 CourseLens」
  // 一个显式退出偏好的读写（features_version 2 合同）。
  const toggle = $("settings-exit-on-close");
  const row = $("settings-tray-row");
  if (!toggle || !row) return;
  let features = null;
  try { features = await api.get_window_features(); } catch { features = null; }
  if (!features) return;
  toggle.checked = !!features.exit_on_close;
  row.hidden = false;
  toggle.addEventListener("change", () => {
    void Promise.resolve(api.set_exit_on_close(toggle.checked)).catch(() => { });
  });
}

async function installWebviewCacheRow(api) {
  const section = $("shell-webview-cache");
  const size = $("shell-webview-cache-size");
  const button = $("shell-webview-cache-clear");
  if (!section || !button) return;
  let features = null;
  try { features = await api.get_window_features(); } catch { features = null; }
  if (!features || features.can_clear_webview_cache === false) return;
  const formatBytes = (value) => {
    if (!Number.isFinite(value) || value <= 0) return "";
    if (value >= 1024 * 1024) return `约 ${(value / 1024 / 1024).toFixed(1)} MB`;
    if (value >= 1024) return `约 ${(value / 1024).toFixed(0)} KB`;
    return "";
  };
  section.hidden = false;
  if (size) size.textContent = formatBytes(features.webview_cache_bytes);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api.request_webview_cache_clear();
      button.textContent = result?.ok ? "已登记，重启应用后生效" : "暂时没能登记，稍后再试";
      if (!result?.ok) button.disabled = false;
    } catch {
      button.textContent = "暂时没能登记，稍后再试";
      button.disabled = false;
    }
  });
}

function installWindowFocusReturn() {
  window.addEventListener(NATIVE_WINDOW_FOCUS_EVENT, () => {
    const main = document.getElementById("workspace-main");
    try { main?.focus({ preventScroll: true }); } catch { main?.focus(); }
  });
}

export function installNativeWindowShell() {
  if (typeof window === "undefined") return () => { };
  installExternalLinkCapture();
  installWindowFocusReturn();
  /* 桥就绪面三通道：即刻在位 / pywebviewready 事件（不设时限）/ 8s 轮询兜底。
     幂等闸防双通道重复装配（开关双重监听之类）。 */
  let shellInstalled = false;
  const installWhenReady = (api) => {
    if (shellInstalled || !api) return;
    shellInstalled = true;
    syncNativeTitlebarTheme(document.documentElement.dataset.theme === "dark");
    void installTrayPreference(api);
    void installWebviewCacheRow(api);
  };
  installWhenReady(nativeApi());
  window.addEventListener("pywebviewready", () => installWhenReady(nativeApi()));
  void nativeApiReady().then(installWhenReady);
  return () => { };
}

async function sendSession(action, beacon = false) {
  const body = JSON.stringify({ action, session_id: SESSION_ID });
  if (beacon && navigator.sendBeacon) {
    /* page lifecycle 关闭路径：sendBeacon 排队失败（false）或抛错时，用 keepalive fetch
       兜底一次；不等待结果、不向用户抛错，失败交由后端 lease 兜底。 */
    let queued = false;
    try {
      queued = navigator.sendBeacon("/api/v3/frontend-session", new Blob([body], { type: "application/json" }));
    } catch {
      queued = false;
    }
    if (queued) return;
    void fetch("/api/v3/frontend-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => {});
    return;
  }
  await postV3("frontend-session", { action, session_id: SESSION_ID });
}

export function openLoginDialog(mode = "login") {
  $("login-dialog-title").textContent = mode === "reauth" ? "重新认证复旦会话" : "复旦统一身份认证";
  $("login-dialog-hint").textContent = mode === "reauth"
    ? "重新认证或切换账号后，仅显示该账号授权的课程。"
    : "登录后仅显示当前账号授权的课程。";
  /* 打开即复位：提交可用、状态/错误清空、密码掩码（由 settings.js 消费） */
  window.dispatchEvent(new Event("courselens:login-dialog-open"));
  $("login-dialog").showModal();
  window.requestAnimationFrame(() => $("login-student-id").focus({ preventScroll: true }));
}

/* 连接状态簇：复旦只消费 store.auth；GitHub 只消费 GET /api/v3/remote-connection。
   CLOUD-CONSENT-AUTO-1 U3b（用户拍板）：页眉/连接菜单的「云自动化」段整体退役——
   它指的是定时规则调度器，与「手动点生成字幕确实自动跑」命名撞车，现场实测
   学生照它读成「云端处理没开」。云端状态改由设置页连接卡的连接真值行单一呈现。 */
const connState = { fudan: "checking", github: "checking" };

function githubStateOf(overall) {
  const state = String(overall?.state || "unknown");
  if (state === "ready") return "ready";
  if (state === "checking" || state === "unknown") return "checking";
  if (state === "action_required" || state === "degraded") return "action";
  /* O1-F2：后端 offline=从未配置/未连接的阻塞件——安静 off 态，绝不落「失败」 */
  if (state === "offline") return "off";
  return "error";
}

function fudanStateOf(auth) {
  const state = String(auth?.state || "unknown");
  if (state === "ready") return "ready";
  if (state === "checking") return "checking";
  if (state === "action_required" || state === "degraded") return "action";
  return "error";
}

function renderConn() {
  /* O1：offline/unreachable 两态的点用既有中性视觉（off/checking），文案已精确 */
  const DOT_STATE_FALLBACK = { offline: "off", unreachable: "checking" };
  document.querySelectorAll("[data-conn-dot]").forEach((dot) => {
    const state = connState[dot.dataset.connDot] || "checking";
    dot.dataset.state = DOT_STATE_FALLBACK[state] || state;
    /* AS4-U2：复旦会话登录中的呼吸提示——仅复旦行、仅 checking（含 restoring，
       fudanStateOf 已归并为 checking）期间；ready/失败态类自然摘除即停。
       动画本体在 CSS（layout.css conn-breath），reduced-motion 退化为静态点。 */
    dot.classList.toggle(
      "conn-breathing",
      dot.dataset.connDot === "fudan" && connState.fudan === "checking",
    );
  });
  document.querySelectorAll("[data-conn-text]").forEach((el) => {
    const map = CONN_TEXT[el.dataset.connText];
    if (map) el.textContent = map[connState[el.dataset.connText]] || map.checking;
  });
  const label = "连接状态：" + CONN_TEXT.fudan[connState.fudan] + "；" + CONN_TEXT.github[connState.github];
  document.querySelectorAll("[data-open-conn]").forEach((btn) => {
    btn.setAttribute("aria-label", label);
  });
  const live = $("conn-live");
  if (live) live.textContent = label;
  refreshStatusCapsuleLabel();
}

function updateFudanConn(auth) {
  connState.fudan = fudanStateOf(auth);
  renderConn();
}

/* 取态与上屏分离（CONN-STALE-R2）：周期复验要先比状态再决定是否写 DOM，
   事件驱动路径保持原「取态即上屏」语义。githubStateFetchSeq=在途拍代际——
   复验拍与 SSE 事件拍并发时只让最新一拍上屏，陈读绝不回退已迁移的点。 */
let githubStateFetchSeq = 0;

async function fetchGithubConnState() {
  /* O1（N6F 移交）：三种真实态分开说——本机离线=offline；本地服务不可达=
     unreachable（fetch 失败≠GitHub 故障，绝不把这两种说成「GitHub 连接失败」）；
     只有后端如实上报故障才落 error。离线先判，零外联。 */
  if (!navigator.onLine) return "offline";
  try {
    const value = await apiV3("remote-connection");
    return githubStateOf(value.overall || value);
  } catch {
    return "unreachable";
  }
}

async function refreshGithubConn() {
  const seq = ++githubStateFetchSeq;
  const next = await fetchGithubConnState();
  if (seq !== githubStateFetchSeq) return;
  connState.github = next;
  renderConn();
}

function updateAccountEntry(auth) {
  latestAuth = auth;
  const button = $("account-button");
  const ready = auth?.state === "ready";
  const checking = auth?.state === "checking";
  $("account-text").textContent = ready ? "账户" : checking ? "验证中" : auth?.state === "degraded" ? "重新认证" : "登录";
  button.dataset.state = auth?.state || "unknown";
  /* F8/N5FE-P7：验证中不禁用入口——菜单（设置/数据/引导）必须恒可达 */
  button.disabled = false;
  button.setAttribute("aria-haspopup", "dialog");
  button.setAttribute("aria-label", ready ? "账户与当前会话" : $("account-text").textContent);
  $("account-menu-login").textContent = ready ? "切换或重新认证" : "登录";
  $("account-menu-logout").hidden = !ready;
}

// ---- 校园连接卡（conn-menu 内的唯一连接面；courselens.vpn-connection.v1 消费端） ----
/* 无第二轮询/定时器：随既有 15s auth 轮询与弹层打开重绘。健康时签名不变 → 零 DOM 写入；
   播报只在状态迁移时写入一次 live 区，checking/reauthenticating 保持安静；
   generation 升高且处于确认中 → 显示“网络设置已变化”内联小状态，而非登出弹窗。 */
const CAMPUS_ANNOUNCED_STATES = new Set([
  "ready", "login_required", "network_unavailable", "challenge_required",
  "expired", "degraded", "off", "stale", "unknown_state",
]);
let campusSignature = "";
let campusAnnouncedState = "";
let campusGeneration = null;
/* P2-B：可关闭的 TUN 提示——按「失败episode」记忆关闭（同一 observed_at 的
   同一状态内不再出现；新 episode/恢复健康后重置）。 */
let campusTunDismissedEpisode = "";
let campusTunCurrentEpisode = "";
let campusLatestAuth = null;

function campusPrimaryAction(view) {
  const fromBackend = view.actions.find((action) => CONNECTION_ACTION_LABELS[action]);
  return fromBackend || CONNECTION_DEFAULT_ACTIONS[view.state] || "";
}

/* P1-A：挑战态的受控入口文案与动作标签——动作仍是既有受控登录窗（唯一的
   安全本地身份入口），但标签说清"打开验证"而非泛化的"登录"。 */
function campusActionLabel(action, view) {
  if (action === "login" && view.rawState === "challenge_required") return "打开验证";
  return CONNECTION_ACTION_LABELS[action] || "继续";
}

/* POLISH-1 F4（化身走查 FULL-CLIENT-INSPECT F4）：degraded 只说「校园服务部分
   可用」，与「复旦会话已连接」并列时学生无法分辨哪部分不可用。按快照闭集服务
   标签点名（WebVPN/课程平台），不可用才出句；无证据（无快照/服务态未知）保持
   既有安静，绝不发明。 */
function campusDegradedNaming(snapshot) {
  const services = snapshot && typeof snapshot.services === "object" && snapshot.services
    ? snapshot.services : null;
  if (!services) return "";
  const down = [];
  const up = [];
  for (const key of ["webvpn", "icourse"]) {
    const label = CONNECTION_SERVICE_LABELS[key];
    const service = services[key];
    const state = service && typeof service === "object" ? String(service.state || "") : "";
    if (state === "unavailable") down.push(label);
    else if (state === "ready") up.push(label);
  }
  if (down.length === 0) return "";
  const head = up.length > 0 ? `${up.join("、")}正常；` : "";
  return `${head}${down.join("、")}暂时连不上，稍后会自动恢复，也可以重试。`;
}

function campusPillState(state) {
  if (state === "ready") return "ready";
  if (state === "degraded") return "degraded";
  if (state === "checking" || state === "reauthenticating" || state === "stale") return "checking";
  return "action_required";
}

function runCampusAction(action) {
  if (action === "login") {
    window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "login" }));
    return;
  }
  if (action === "reauthenticate") {
    window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "reauth" }));
    return;
  }
  if (action === "open-settings") {
    closeOverlay($("conn-menu"));
    selectPage("settings");
    return;
  }
  /* check-network / retry / close-tun-and-retry（P2-A/P2-B）：先跑一次按需
     无凭据诊断（复用既有 campus-diagnostics 探测，刷新路由证据），无论成败
     随后走既有 auth 复检。不新增轮询，也绝不读取或切换系统 TUN/代理设置。 */
  const refreshAfterProbe = () => window.dispatchEvent(new Event("courselens:auth-refresh"));
  if (action === "check-network" || action === "retry" || action === "close-tun-and-retry") {
    void runCampusDiagnostics({ fromAction: true }).then(refreshAfterProbe, refreshAfterProbe);
    return;
  }
  window.dispatchEvent(new Event("courselens:auth-refresh"));
}

function renderCampusConnection(auth) {
  campusLatestAuth = auth;
  const root = $("campus-connection");
  if (!root) return;
  const snapshot = extractConnectionSnapshot(auth);
  const view = snapshot
    ? connectionViewFromSnapshot(snapshot, Date.now() / 1000)
    : connectionViewFromAuth(auth);
  if (!view) return;
  /* AS4-U2b：网络层降为复旦卡副行——校园健康（ready）时整块收起，弹层只讲
     「复旦课程平台」与「GitHub 远程连接」两件事；确认中/异常才显具体指引
     （原因行/TUN/检查校园连接随块内既有逻辑出现）。state 在签名首位的
     signature 里，任何状态迁移都会走完整重绘，hidden 不会滞留旧值。 */
  root.hidden = view.state === "ready";
  /* CAMPUS-P3-16 同块相邻修：分态左边框（pages.css [data-state] 色映射）此前
     无任何 JS 写点，data-state 恒为静态 checking——此处按 pill 同款闭集映射
     接线；ready 仍为 navy，健康态零新视觉。 */
  root.dataset.state = campusPillState(view.state);
  const generation = view.generation;
  const regenerated = Boolean(
    generation != null && campusGeneration != null && generation > campusGeneration
    && (view.state === "checking" || view.state === "reauthenticating"),
  );
  if (generation != null) campusGeneration = generation;

  const action = campusPrimaryAction(view);
  const actionLabel = campusActionLabel(action, view);
  /* P1-A：闭集错误码的细分事实文案优先（锁定/维护/挑战/凭据各自不同）；
     健康态与确认中无错误码 → 回退到既有 TUN/中性提示，绝不显示多余文案。 */
  const codeHint = connectionCodeHint(auth?.code);
  /* POLISH-1 F4：degraded 且有快照时点名不可用服务（闭集标签），替代无解释的
     「部分可用」孤句；闭集错误码细分文案与 TUN 提示优先级不变。 */
  const hasSnapshot = Boolean(snapshot);
  const degradedNaming = view.state === "degraded" && hasSnapshot ? campusDegradedNaming(snapshot) : "";
  const reasonText = codeHint && view.state !== "ready" && view.state !== "checking" && view.state !== "reauthenticating"
    ? codeHint
    : (degradedNaming || view.hint);
  /* P2-B：只有后端在证据支持时才会给出 close-tun-and-retry 动作；该动作存在
     且当前原因行确实是 TUN 提示（而非闭集错误码细分文案）时才渲染可关闭
     提示行。健康/确认中动作集不含它 → 行隐藏。 */
  const tunOffered = view.actions.includes("close-tun-and-retry")
    && Boolean(reasonText) && reasonText === view.hint;
  const tunEpisode = tunOffered
    ? `${view.rawState}|${hasSnapshot && view.observedAt != null ? view.observedAt : ""}`
    : "";
  if (!tunOffered) campusTunDismissedEpisode = "";
  campusTunCurrentEpisode = tunEpisode;
  const tunVisible = tunOffered && campusTunDismissedEpisode !== tunEpisode;
  const signature = [
    view.state, reasonText, regenerated ? "1" : "0", action, actionLabel,
    hasSnapshot ? view.pathText : "",
    hasSnapshot && view.observedAt != null ? formatTime(view.observedAt) : "",
    hasSnapshot ? view.servicesText : "",
    tunVisible ? "1" : "0",
  ].join("\u0001");
  if (signature === campusSignature) return;
  campusSignature = signature;

  /* CAMPUS-FIX 剩留清理（POLISH-1）：#campus-connection-pill 已于 AS4-U2b 随
     独立校园卡头退役，此前的 null 守卫写点恒不命中=死代码，整块拆除；
     state 语义由下方 stateLine 与 root.dataset.state（CAMPUS-P3-16 接线）承载。 */
  const stateLine = $("campus-connection-state");
  if (stateLine) stateLine.textContent = CONNECTION_STATE_TEXT[view.state] || CONNECTION_STATE_TEXT.unknown_state;
  const reasonLine = $("campus-connection-reason");
  if (reasonLine) {
    reasonLine.textContent = reasonText;
    /* 关闭 TUN 提示 = 隐藏整条 TUN 原因文案（错误码细分文案不受影响：
       tunOffered 已要求 reasonText 是 TUN 提示本身）。 */
    reasonLine.hidden = !reasonText || (tunOffered && !tunVisible);
  }
  const tunRow = $("campus-connection-tun");
  if (tunRow) tunRow.hidden = !tunVisible;
  const regenLine = $("campus-connection-regenerated");
  if (regenLine) regenLine.hidden = !regenerated;
  const actionsRow = $("campus-connection-actions");
  if (actionsRow) {
    clear(actionsRow);
    if (action) {
      const button = textElement("button", actionLabel);
      button.type = "button";
      button.dataset.campusAction = action;
      button.addEventListener("click", () => runCampusAction(action));
      actionsRow.append(button);
    }
  }
  const pathNode = $("campus-connection-path");
  if (pathNode) pathNode.textContent = hasSnapshot ? view.pathText : "尚未确认";
  const checkedNode = $("campus-connection-checked");
  if (checkedNode) checkedNode.textContent = hasSnapshot && view.observedAt != null ? formatTime(view.observedAt) : "尚未确认";
  const servicesNode = $("campus-connection-services");
  if (servicesNode) servicesNode.textContent = hasSnapshot ? view.servicesText : "尚未确认";

  if (CAMPUS_ANNOUNCED_STATES.has(view.state) && view.state !== campusAnnouncedState) {
    campusAnnouncedState = view.state;
    const live = $("campus-connection-live");
    if (live) live.textContent = CONNECTION_STATE_TEXT[view.state];
  }
}

// ---- P1-D 校园连接诊断抽屉（按需，唯一触发是显式点击） ----
/* 只渲染闭集结论：逻辑路径、上次检查时间、粗粒度延迟档、回退结论与唯一建议
   动作。代理地址、URL、响应体、账号、课程数据与原始延迟数字全部不出后端。
   健康会话零轮询零探测：不点击就永远安静。 */
const CAMPUS_DIAGNOSTICS_ROUTE_TEXT = Object.freeze({ direct: "直连", proxy: "本机代理", unknown: "尚未确认" });
const CAMPUS_DIAGNOSTICS_BAND_TEXT = Object.freeze({ fast: "很快", normal: "正常", slow: "较慢", unavailable: "不可达" });

function renderCampusDiagnostics(value) {
  const services = value && typeof value === "object" && value.services && typeof value.services === "object"
    ? value.services
    : null;
  const perService = (key, labels, fallbackText) => {
    const parts = [];
    for (const name of ["webvpn", "icourse"]) {
      const label = name === "webvpn" ? "WebVPN" : "课程平台";
      const service = services ? services[name] : null;
      const raw = service && typeof service === "object" ? String(service[key] || "") : "";
      parts.push(`${label} ${labels[raw] || fallbackText}`);
    }
    return parts.join(" · ");
  };
  const summary = $("campus-diagnostics-summary");
  if (summary) {
    summary.textContent = value ? perService("route", CAMPUS_DIAGNOSTICS_ROUTE_TEXT, "尚未确认") : "检查未完成，请稍后重试。";
  }
  const checked = $("campus-diagnostics-checked");
  if (checked) {
    const at = value && Number(value.checked_at) > 0 ? Number(value.checked_at) : 0;
    checked.textContent = at ? formatTime(at) : "尚未确认";
  }
  const latency = $("campus-diagnostics-latency");
  if (latency) {
    latency.textContent = value ? perService("latency_band", CAMPUS_DIAGNOSTICS_BAND_TEXT, "未确认") : "尚未确认";
  }
  const fallback = $("campus-diagnostics-fallback");
  if (fallback) {
    let text = "尚未确认";
    if (value) {
      const used = services && Object.values(services).some(
        (service) => service && typeof service === "object" && service.fallback_used === true,
      );
      const known = services && Object.values(services).some(
        (service) => service && typeof service === "object" && service.route && service.route !== "unknown",
      );
      text = used ? "直连失败后备用路径成功" : known ? "未使用备用路径" : "尚未确认";
    }
    fallback.textContent = text;
  }
  const action = $("campus-diagnostics-action");
  if (action) {
    const nextAction = value ? String(value.next_action || "") : "";
    action.textContent = nextAction ? (CONNECTION_ACTION_LABELS[nextAction] || "继续") : "当前无待办动作";
  }
}

let campusDiagnosticsBusy = false;

/* fromAction=true：由连接卡动作（检查网络/重试/关闭 TUN 后重试）触发——
   行为与显式点击完全一致（同一条按需探测路径，零新增轮询）。 */
async function runCampusDiagnostics({ fromAction = false } = {}) {
  const run = $("campus-diagnostics-run");
  if (!run || campusDiagnosticsBusy) return;
  campusDiagnosticsBusy = true;
  run.disabled = true;
  run.setAttribute("aria-busy", "true");
  const summary = $("campus-diagnostics-summary");
  if (summary) summary.textContent = "正在检查校园连接…";
  /* CAMPUS-P3-16：抽屉证据门控展开——未检查时保持 hidden（弹层安静，无
     「尚未检查」占位列），任一次探测（显式点击或恢复动作复用）即展开并
     以「正在检查」即时反馈。f3d6fdd 起字段一直渲染但 hidden 从未摘除，
     探测结果对学生不可见；此处是唯一的摘除点，不点击仍永远安静。 */
  const panel = $("campus-diagnostics");
  if (panel) panel.hidden = false;
  const status = $("campus-diagnostics-status");
  try {
    const value = await apiV3("campus-diagnostics");
    renderCampusDiagnostics(value);
    if (status) status.textContent = "校园连接检查完成";
  } catch {
    renderCampusDiagnostics(null);
    if (status) status.textContent = "检查未完成，请稍后重试";
  } finally {
    campusDiagnosticsBusy = false;
    run.disabled = false;
    run.removeAttribute("aria-busy");
  }
}

export async function installShell(store) {
  applyThemePreference(readThemePreference(), { transition: false });

  /* 夜10-A：原生窗口壳集成（托盘偏好/外链闭集/界面缓存/焦点回归/标题栏主题）。
     浏览器形态探测不到桥=零安装零行为差异；原生形态则在此全部点亮。 */
  installNativeWindowShell();

  /* 三态焦点政策（NIGHT5-U1）：window 捕获段记录最近输入模态到 <html data-input>，
     指针态下 CSS 归零聚焦环（含粘滞启发式），Tab 恒环不变。 */
  const modalityTracker = createInputModalityTracker();
  modalityTracker.attach();

  /* 字标：回学习页并稳定回到课程/讲次选择入口（保留已选课程与讲次） */
  const handleWordmark = () => {
    selectPage("study");
    window.dispatchEvent(new Event("courselens:study-return-select"));
  };
  $("wordmark").addEventListener("click", handleWordmark);
  /* header 常驻「直播」入口钮（N6L S1 §12.2 W4）：一击直达直播页；
     红点=最近观测到 state=live 的课程存在（liveActiveCourses 内存键） */
  const handleLiveEntry = () => selectPage("live");
  $("live-entry").addEventListener("click", handleLiveEntry);
  const liveEntryDot = $("live-entry-dot");
  const unsubscribeLiveActive = store.subscribe("liveActiveCourses", (ids) => {
    if (liveEntryDot) liveEntryDot.hidden = !Array.isArray(ids) || ids.length === 0;
  });
  if (liveEntryDot) liveEntryDot.hidden = !(store.liveActiveCourses || []).length;
  const handleThemeToggle = () => applyThemePreference(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  $("theme-toggle").addEventListener("click", handleThemeToggle);
  $("settings-close").addEventListener("click", () => selectPage("study"));

  const refreshAuth = async () => {
    try {
      const auth = await apiV3("authentication");
      store.set("auth", auth);
      updateAccountEntry(auth);
      updateFudanConn(auth);
      setGlobalStatus(evidenceText(auth), auth.state);
    } catch (error) {
      setGlobalStatus(error.message, "error");
    }
  };

  /* 甲3：菜单箭头键导航——↑↓ 循环移动、Home/End 到两端，焦点态与悬停同款
     （.menu-item:focus-visible / 弹层内按钮）；Esc/点外关闭由 openOverlay 负责。 */
  const installMenuArrowNav = (root) => {
    if (!root || typeof root.addEventListener !== "function") return;
    root.addEventListener("keydown", (event) => {
      const key = String(event.key || "");
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(key)) return;
      const items = [...root.querySelectorAll("button")].filter(
        (item) => !item.hidden && !item.disabled,
      );
      if (!items.length) return;
      const current = typeof document !== "undefined" ? document.activeElement : null;
      const index = items.indexOf(current);
      let next;
      if (key === "Home") next = 0;
      else if (key === "End") next = items.length - 1;
      else if (index < 0) next = key === "ArrowDown" ? 0 : items.length - 1;
      else next = (index + (key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      event.preventDefault();
      items[next].focus({ preventScroll: true });
    });
  };
  installMenuArrowNav($("account-menu"));
  installMenuArrowNav($("conn-menu"));

  $("account-button").addEventListener("click", () => {
    const trigger = $("account-button");
    if (trigger.getAttribute("aria-expanded") === "true") { closeOverlay($("account-menu")); return; }
    /* N5FE-P7：未登录也开菜单而非直接弹登录框——「设置/数据管理/新手引导」
       必须两跳内可达（菜单→设置）；登录动作留在菜单内（account-menu-login）。 */
    const opened = openOverlay({
      root: $("account-menu"),
      trigger,
      onClose: () => { trigger.setAttribute("aria-expanded", "false"); },
      dismissOnOutside: true,
    });
    if (!opened) return;
    trigger.setAttribute("aria-expanded", "true");
  });
  $("account-menu-settings").addEventListener("click", () => {
    closeOverlay($("account-menu"));
    selectPage("settings");
  });
  $("account-menu-login").addEventListener("click", () => {
    closeOverlay($("account-menu"));
    openLoginDialog(latestAuth?.state === "ready" || latestAuth?.state === "degraded" ? "reauth" : "login");
  });
  $("account-menu-logout").addEventListener("click", () => {
    closeOverlay($("account-menu"));
    window.dispatchEvent(new CustomEvent("courselens:logout"));
  });

  $("conn-status").addEventListener("click", () => {
    const trigger = $("conn-status");
    if (trigger.getAttribute("aria-expanded") === "true") { closeOverlay($("conn-menu")); return; }
    const opened = openOverlay({
      root: $("conn-menu"),
      trigger,
      onClose: () => { trigger.setAttribute("aria-expanded", "false"); },
      dismissOnOutside: true,
    });
    if (!opened) return;
    trigger.setAttribute("aria-expanded", "true");
    renderCampusConnection(latestAuth);
    void refreshGithubConn();
  });

  const handlePageRequest = (event) => selectPage(String(event.detail || "study"));
  const handleLoginRequest = (event) => openLoginDialog(event.detail === "reauth" ? "reauth" : "login");
  window.addEventListener("courselens:select-page", handlePageRequest);
  window.addEventListener("courselens:open-login", handleLoginRequest);
  window.addEventListener("courselens:auth-refresh", refreshAuth);
  const diagnosticsRun = $("campus-diagnostics-run");
  const handleDiagnosticsRun = () => void runCampusDiagnostics();
  if (diagnosticsRun) diagnosticsRun.addEventListener("click", handleDiagnosticsRun);
  const tunDismissButton = $("campus-connection-tun-dismiss");
  const handleTunDismiss = () => {
    if (!campusTunCurrentEpisode) return;
    campusTunDismissedEpisode = campusTunCurrentEpisode;
    renderCampusConnection(campusLatestAuth);
  };
  if (tunDismissButton) tunDismissButton.addEventListener("click", handleTunDismiss);
  $("close-login").addEventListener("click", () => $("login-dialog").close());
  $("cancel-login").addEventListener("click", () => $("login-dialog").close());
  document.addEventListener("keydown", (event) => {
    /* IME 合成态早退（CJK-GUARD-1）：登录框输入合成中按 Esc=取消候选，
       绝不当「关闭登录框」消费（学生打到一半的学号/密码不丢）。 */
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape" && $("login-dialog").open) $("login-dialog").close();
  });

  store.subscribe("auth", updateFudanConn);
  store.subscribe("auth", renderCampusConnection);
  void refreshGithubConn();
  /* 连接及时性（CLIENT-STATE，用户实证痛点）：页眉 GitHub 连接点此前只在装配
     与点开连接菜单时读取快照——后台状态翻转要等人为点击才上屏。现订阅任务
     抽屉应用级 SSE 广播的 courselens:remote-connection-changed 即刻复核；
     网络层 online/offline 沿同一入口即时呈现（refreshGithubConn 内先判
     navigator.onLine，离线态零外联）。
     CONN-STALE-R2（2026-10-07 用户实证复发，钉死）：SSE 只覆盖 probe 驱动的
     翻转——快照读时派生的翻转（观测证据 TTL 过期→unknown/stale_evidence、
     令牌过期在读取瞬间推导成 checking/action_required）不落 remote 事件，
     SSE 全程静默；SSE 断线窗口同样无人推动。补低频周期复验兜底：60s 一拍、
     每拍至多一次无参 GET remote-connection（后端本地快照派生，零 GitHub
     外联；传输层重试退避沿用 request 既有语义），离线先判零外联，状态未
     迁移零 DOM 写入（沿 renderCampusConnection 健康签名零写先例）。 */
  const handleRemoteConnectionChanged = () => { void refreshGithubConn(); };
  /* REALRUN-1 旅程6（2026-10-08 真测）：断网恢复边沿即刻取一次复旦会话证据，
     不再干等最长 15s 轮询——断网期间的 degraded/陈读在恢复瞬间换新。离线
     边沿零外联语义不变；在线边沿只是提前一拍既有 GET authentication 轮询，
     不新增任何通道。 */
  const handleNetworkLayerChange = () => {
    void refreshGithubConn();
    if (navigator.onLine) void refreshAuth();
  };
  window.addEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
  window.addEventListener("online", handleNetworkLayerChange);
  window.addEventListener("offline", handleNetworkLayerChange);
  let githubRecheckTimer = 0;
  let githubRecheckInFlight = false;
  const recheckGithubConn = async () => {
    /* 上一拍未收口不叠发：后端挂起时也不堆积在途请求（廉价轮询纪律）。 */
    if (githubRecheckInFlight) return;
    githubRecheckInFlight = true;
    try {
      const seq = ++githubStateFetchSeq;
      const next = await fetchGithubConnState();
      if (seq !== githubStateFetchSeq) return; /* 事件拍/更新拍已定谳：陈读不上屏 */
      if (next !== connState.github) {
        connState.github = next;
        renderConn();
      }
    } finally {
      githubRecheckInFlight = false;
    }
  };
  githubRecheckTimer = window.setInterval(() => { void recheckGithubConn(); }, 60000);

  /* 会话 close 所有权在 installShell（open 的创建者）：pagehide 与正常 cleanup 走同一个
     幂等 close，close 至多发送一次；pagehide 必须先于首次 session 登记安装，堵住
     installer 未完成即关闭的窗口。BFCache 挂起（persisted=true）不关闭，恢复后
     session/heartbeat 原样保留。 */
  const heartbeatScheduler = createHeartbeatScheduler({
    heartbeat: () => { void sendSession("heartbeat").catch(() => { }); },
    schedule: (handler, ms) => window.setInterval(handler, ms),
    cancel: (id) => window.clearInterval(id),
  });
  const handleVisibilityHeartbeat = () => {
    if (!document.hidden) heartbeatScheduler.onVisible();
  };
  document.addEventListener("visibilitychange", handleVisibilityHeartbeat);
  const startHeartbeat = () => {
    try {
      heartbeatScheduler.attachWorker(new Worker("/workers/heartbeat-worker.js"));
    } catch {
      heartbeatScheduler.startPageFallback();
    }
  };
  /* 断连横幅（BACKEND-DEATH-1③ / DISPATCH-HEALTH-1）：10s 有界探测 /api/health
     （5s 超时），连续 3 次失败判服务断开；探测与 auth/session 轮询独立，间隔
     错开 15s 定时器族。单次失败先补探一次（createRetryingHealthProbe），瞬时
     饥饿不再直接计入失败。 */
  const serviceHealthProbeOnce = async () => {
    const probeController = new AbortController();
    const probeTimeout = window.setTimeout(
      () => probeController.abort(), SERVICE_HEALTH_PROBE_TIMEOUT_MS,
    );
    try {
      const response = await fetch("/api/health", { signal: probeController.signal, credentials: "same-origin" });
      if (!response.ok) return false;
      const payload = await response.json();
      return payload?.ok === true;
    } finally {
      window.clearTimeout(probeTimeout);
    }
  };
  const serviceHealthProbe = createRetryingHealthProbe({ probeOnce: serviceHealthProbeOnce });
  let healthTimer = 0;
  const serviceMonitor = createServiceHealthMonitor({
    probe: serviceHealthProbe,
    onDisconnect: () => {
      /* P56-U3③：横幅点亮/自撤各留一条 console 痕（含连续失败计数），
         与服务端 HealthProbeTrace 对得上。 */
      console.info(`[health] 断连横幅点亮（连续失败 ${serviceMonitor.failures} 次）`);
      applyServiceDisconnectState(true);
    },
    onRecover: () => {
      console.info("[health] 服务探测恢复，断连横幅自撤");
      applyServiceDisconnectState(false);
    },
  });
  let sessionClosed = false;
  let sessionOpenSettled = false;
  let closeSent = false;
  let openSession;
  const sendClose = () => {
    if (closeSent) return;
    closeSent = true;
    void sendSession("close", true);
  };
  const closeSession = () => {
    if (sessionClosed) return;
    sessionClosed = true;
    heartbeatScheduler.stop();
    document.removeEventListener("visibilitychange", handleVisibilityHeartbeat);
    window.clearInterval(authTimer);
    window.clearInterval(githubRecheckTimer);
    window.clearTimeout(healthTimer);
    window.removeEventListener("pagehide", handlePageHide);
    // An open request can still be in flight.  Closing before it reaches the
    // server would otherwise allow that late open to resurrect the session.
    if (openSession && !sessionOpenSettled) {
      void openSession.then(sendClose, sendClose);
      return;
    }
    sendClose();
  };
  const handlePageHide = (event) => {
    if (event.persisted) return;
    closeSession();
  };
  window.addEventListener("pagehide", handlePageHide);

  const cleanup = () => {
    modalityTracker.detach();
    unsubscribeLiveActive();
    window.removeEventListener("courselens:select-page", handlePageRequest);
    window.removeEventListener("courselens:open-login", handleLoginRequest);
    window.removeEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
    window.removeEventListener("online", handleNetworkLayerChange);
    window.removeEventListener("offline", handleNetworkLayerChange);
    if (diagnosticsRun) diagnosticsRun.removeEventListener("click", handleDiagnosticsRun);
    if (tunDismissButton) tunDismissButton.removeEventListener("click", handleTunDismiss);
    $("theme-toggle").removeEventListener("click", handleThemeToggle);
    $("live-entry").removeEventListener("click", handleLiveEntry);
    closeOverlay($("account-menu"));
    closeSession();
  };

  /* ⑬b：装配不等会话——openSession 后台自走，数据页立即用本地缓存渲染
     （读族 restoring-trusted 通道放行缓存读），会话就绪后心跳/auth 轮询
     在此接力，快照自然换新。检查点恢复的十秒不再挡住整页装配。
     P56-U3②：断连监视器与 openSession 成败解耦——open 失败（含启动期 503）
     也立即装配监视器并按有界退避重试 open；down 期间前 5 拍 3s 加速恢复
     探测、之后回 10s，后端就绪（/api/health ok:true）后横幅秒级自撤，
     不再依赖 open 成功过。 */
  let healthFastTicks = 0;
  const healthTick = async () => {
    if (sessionClosed) return;
    await serviceMonitor.tick();
    /* POLISH-O13 O1：确认前的疑似态（有失败未满阈值）也即时可见（轻提示，
       不禁用动作钮）；疑似期与断开期同用快拍语义，确认窗最坏 ~30s→~16s。 */
    if (!serviceMonitor.down) {
      applyServiceDisconnectState(false, { probing: serviceMonitor.failures >= 1 });
    }
    const next = nextServiceProbeDelayMs({
      troubled: serviceMonitor.down || serviceMonitor.failures >= 1,
      fastTicks: healthFastTicks,
    });
    healthFastTicks = next.fastTicks;
    if (sessionClosed) return;
    healthTimer = window.setTimeout(healthTick, next.delayMs);
  };
  /* open 有界退避重试：启动期 POST 503 / 瞬时不可达时再试 3 次（2s/4s/6s），
     重试耗尽即放弃——横幅真值交由监视器呈现，会话功能待下次装配。 */
  const openSessionWithRetry = async () => {
    let lastError = null;
    for (let attempt = 0; attempt <= 3; attempt += 1) {
      if (attempt > 0) {
        if (sessionClosed) break;
        await new Promise((resolve) => window.setTimeout(resolve, 2000 * attempt));
      }
      if (sessionClosed) break;
      try {
        await sendSession("open");
        return;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error("frontend session open cancelled");
  };
  openSession = openSessionWithRetry();
  void healthTick();
  void Promise.resolve(openSession).then(async () => {
    sessionOpenSettled = true;
    if (sessionClosed) return;
    startHeartbeat();
    await refreshAuth();
    /* close 可能在 refreshAuth 在途时落地：落地后不得再安装 auth 轮询 */
    if (sessionClosed) return;
    authTimer = window.setInterval(refreshAuth, 15000);
  }).catch(() => {
    sessionOpenSettled = true;
  });
  return cleanup;
}
