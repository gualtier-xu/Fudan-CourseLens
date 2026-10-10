export const $ = (id) => document.getElementById(id);

export function operationId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

export function setGlobalStatus(message, state = "unknown") {
  const target = $("global-status");
  if (!target) return;
  target.textContent = message;
  target.dataset.state = state;
}

/* 堆叠上限（夜2审计 V8-P3 处方）：region 内至多 3 枚，超出淘汰最旧一枚；
   每枚本就 4.2s 自收口，上限只是连点兜底，防读屏区被历史提示淹没。 */
const TOAST_STACK_LIMIT = 3;

export function toast(message, state = "ready") {
  const region = $("toast-region");
  if (!region) return;
  while (region.children.length >= TOAST_STACK_LIMIT) {
    const oldest = region.children[0];
    const before = region.children.length;
    if (!oldest || typeof oldest.remove !== "function") break;
    oldest.remove();
    if (region.children.length >= before) break; /* 桩 remove 可能只置 hidden：防空转 */
  }
  const node = document.createElement("div");
  node.className = "toast";
  node.dataset.state = state;
  node.textContent = message;
  region.append(node);
  window.setTimeout(() => node.remove(), 4200);
}

export function clear(node) {
  node?.replaceChildren();
}

const EVIDENCE_DETAILS = Object.freeze({
  fudan_session_verified: ["复旦课程平台已登录", "可以读取当前账号授权的课程。"],
  fudan_session_checking: ["正在验证复旦会话", "课程将在验证完成后显示。"],
  fudan_session_expired: ["复旦会话已过期", "需要重新认证后才能读取课程。"],
  fudan_login_required: ["需要登录复旦课程平台", "登录后仅显示当前账号授权的课程。"],
  fudan_credentials_missing: ["尚未登录复旦课程平台", "登录信息不会显示在页面或诊断中。"],
  fudan_login_failed: ["复旦登录未完成", "可能是网络或校园服务波动，请稍候约 90 秒后再试。"],
  fudan_credentials_rejected: ["复旦登录未完成", "请核对学号与密码后重新登录。"],
  webvpn_ticket_transport_failed: ["校园网关连接未完成", "账号状态正常，多为网关连接波动；系统会换用备用通道重试，请稍后再试。"],
  icourse_ticket_transport_failed: ["课程平台连接未完成", "校园登录仍然有效，多为课程平台连接波动；请稍后重试。"],
  authorized_catalog_verified: ["课程目录已验证", "当前课程来自已验证的账号授权。"],
  authorized_catalog_refreshing: ["正在刷新课程目录", "可以继续使用页面，课程确认后会自动更新。"],
  authorized_catalog_stale: ["课程目录需要刷新", "正在显示同一账号上次验证的课程。"],
  catalog_timeout: ["课程目录连接超时", "登录仍然有效，当前无法确认最新课程。"],
  catalog_route_unavailable: ["课程目录网络暂不可用", "登录仍然有效，可以重试或诊断网络。"],
  catalog_session_expired: ["课程目录授权已过期", "需要重新认证后再读取课程。"],
  catalog_additional_verification_required: ["课程目录需要重新认证", "无需关闭两步验证，请按正常流程重新登录。"],
  catalog_bearer_missing: ["课程授权信息暂无法读取", "登录仍然有效，可稍后重试刷新；若持续出现请更新客户端。"],
  catalog_identity_invalid: ["课程目录身份无法确认", "需要重新认证以保护不同账号的课程边界。"],
  catalog_identity_mismatch: ["课程目录账号不一致", "为避免显示其他账号的课程，需要重新认证。"],
  catalog_payload_invalid: ["课程目录暂时无法确认", "登录仍然有效，请稍后重试刷新。"],
  catalog_detail_partial: ["部分课程暂时无法确认", "已验证课程仍可使用，可以重试补全目录。"],
  catalog_target_invalid: ["课程目录请求已被安全策略阻止", "登录仍然有效，请重试或复制诊断信息。"],
  /* NIGHT2-W11：仓库迁移门是学生可达的关键节点，给具名证据文案 */
  personal_worker_migration_required: ["专属仓库需要迁移", "按设置页的指引完成一次迁移即可，课程数据不受影响。"],
  /* REALRUN-1 N1/N2（2026-10-08 真测）：overall 聚合修复后，未授权首跑的
     引导 GitHub 行读到 overall.code=authorization_missing——给它一行诚实
     可行动的标题，不再落到通用「需要完成操作」兜底。 */
  authorization_missing: ["尚未连接 GitHub", "完成授权并创建专属仓库后，字幕与笔记生成才可用；也可以稍后在设置中连接。"],
  /* F7（化身走查 20261008）：本地统计两态具名——此前未开启时落通用
     「需要完成操作」兜底，学生无从知道要完成什么、也没有 adjacent 动作。
     开启与否都不影响功能：措辞把「未开启」陈述为正常状态而非缺失（隐私
     正向缺省），动作入口由设置页隐私节按 actions 闭集就地提供。 */
  analytics_enabled: ["已开启", "学习统计只保存在这台电脑上，仅记录去标识的使用计数，可随时关闭。"],
  analytics_disabled: ["未开启（不影响使用）", "这只影响是否记录学习统计；想开启时点下方按钮即可，数据只留在本机。"],
});

const STATE_DETAILS = Object.freeze({
  ready: ["已连接", "当前功能可以使用。"],
  checking: ["正在确认状态", "请稍候。"],
  action_required: ["需要完成操作", "按页面提示继续。"],
  degraded: ["部分功能暂不可用", "可以按页面提示恢复。"],
  error: ["操作未完成", "请重试或复制连接诊断。"],
});

/* N5FE-P2：这组目录码的副句宣称「登录仍然有效」——只在确有会话时成立。
   调用方知道会话真值就传 { loggedIn }；未知（null）保持既有文案不动。 */
const LOGIN_VALID_CLAIM_CODES = new Set([
  "catalog_timeout", "catalog_route_unavailable", "catalog_bearer_missing", "catalog_payload_invalid",
]);
const NOT_LOGGED_IN_CATALOG_IMPACT = "尚未登录复旦课程平台；先登录，再刷新即可同步课程。";

export function evidenceDetails(value, { loggedIn = null } = {}) {
  if (!value) return { title: "状态尚未确认", impact: "请稍后重试。" };
  const detail = EVIDENCE_DETAILS[String(value.code || "")] || STATE_DETAILS[String(value.state || "")]
    || ["状态尚未确认", "请重试或复制连接诊断。"];
  /* AS4-U1：恢复期缓存先行——checking 信封确实带着本机缓存课程上屏时，
     「课程将在验证完成后显示」失实；改说清「先显示缓存、登录后换新」。
     空缓存（course_count 为 0/空）保持原句：登录完成后才显示。 */
  const detailImpact = String(value.code || "") === "fudan_session_checking" && Number(value.course_count) > 0
    ? "正在登录复旦账号，先显示上次缓存的课程。"
    : detail[1];
  const impact = loggedIn === false && LOGIN_VALID_CLAIM_CODES.has(String(value.code || ""))
    ? NOT_LOGGED_IN_CATALOG_IMPACT
    : detailImpact;
  return { title: detail[0], impact };
}

export function recoveryActionLabel(action, value = {}) {
  if (action === "login") {
    return String(value.code || "").includes("credentials_missing") ? "登录" : "重新认证";
  }
  return {
    "refresh-catalog": "重试刷新",
    "diagnose-network": "诊断网络",
    logout: "退出登录",
  }[action] || "继续";
}

/* U⑬（第廿四案）：学期串人话化——数据层是「2026-20271」这类无分隔原始码，
   面向学生统一呈现「2026-2027 · 第 1 学期」。仅展示层换装：匹配器与过滤
   键继续吃原始串，绝不改写目录数据。未知形态诚实原样返回。 */
export function friendlyTerm(raw) {
  const value = String(raw || "").trim();
  if (!value) return "";
  let match = value.match(/^(\d{4})-(\d{4})(\d)$/);
  if (match) return `${match[1]}-${match[2]} · 第 ${match[3]} 学期`;
  match = value.match(/^(\d{4})-(\d{4})[-·](\d)$/);
  if (match) return `${match[1]}-${match[2]} · 第 ${match[3]} 学期`;
  match = value.match(/^(\d{4})-(\d{4})学年第?(\d)学期$/);
  if (match) return `${match[1]}-${match[2]} · 第 ${match[3]} 学期`;
  return value;
}

export function textElement(tag, text, className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(text ?? "");
  return node;
}

export function evidenceText(value) {
  return evidenceDetails(value).title;
}

export function formatTime(value) {
  const timestamp = Number(value || 0);
  if (!timestamp) return "尚未确认";
  return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
}

/* U2（TASKS-CENTER-1，用户 09-22 走查拍板）：人话相对时间——同一天给
   「今晨/上午/下午/今晚 HH:MM」，跨天给「M/D」（跨年带年份）；无值诚实空串。
   nowSeconds 供测试注入当前时刻。作为共享通道住在 ui.js，任务卡等面统一换装。 */
export function formatRelativeTime(value, nowSeconds = null) {
  const timestamp = Number(value || 0);
  if (!timestamp) return "";
  const now = nowSeconds === null ? Date.now() / 1000 : Number(nowSeconds);
  if (!Number.isFinite(now)) return "";
  const moment = new Date(timestamp * 1000);
  const today = new Date(now * 1000);
  if (moment.getFullYear() !== today.getFullYear()) {
    return `${moment.getFullYear()}/${moment.getMonth() + 1}/${moment.getDate()}`;
  }
  if (moment.getMonth() !== today.getMonth() || moment.getDate() !== today.getDate()) {
    return `${moment.getMonth() + 1}/${moment.getDate()}`;
  }
  const hour = moment.getHours();
  const clock = `${String(hour).padStart(2, "0")}:${String(moment.getMinutes()).padStart(2, "0")}`;
  if (hour < 6) return `今晨 ${clock}`;
  if (hour < 12) return `上午 ${clock}`;
  if (hour < 18) return `下午 ${clock}`;
  return `今晚 ${clock}`;
}

// ---- P2-E 本地去标识计数器（纯计数、闭集名称、进程内存态） ----
/* 只允许匿名行为计数（如播放位置保留次数）；绝不写入账号、课程、URL 或
   网络数据，也绝不新增本机持久化写入（workbench 钉住前端全部本地存储写入
   行）。会话级计数用于即时诊断；持久化的去标识计数由后端 app_state
   （campus_recovery_metrics.v1）承载。失败静默。 */
const localMetrics = new Map();

export function bumpLocalMetric(name) {
  const key = String(name || "");
  if (!key) return;
  localMetrics.set(key, Math.max(0, Number(localMetrics.get(key)) || 0) + 1);
}

export function localMetric(name) {
  return Math.max(0, Number(localMetrics.get(String(name || ""))) || 0);
}

export function setBusy(node, busy) {
  if (!node) return;
  node.disabled = Boolean(busy);
  node.setAttribute("aria-busy", String(Boolean(busy)));
}

// ---- 校园连接快照消费端（courselens.vpn-connection.v1，契约门 V0 冻结） ----
/* 只认 schema 字符串：后端把快照附着在任何现有 v3 载荷上都能被找到；
   v2/未知 schema 一律视为“无快照”，回退到既有 auth 派生视图。
   消费端永不渲染闭集之外的值：未知字段被忽略，未知枚举降级为中性文案。 */

export const CONNECTION_SCHEMA = "courselens.vpn-connection.v1";

const CONNECTION_STATES = Object.freeze([
  "off", "checking", "ready", "login_required", "reauthenticating",
  "network_unavailable", "challenge_required", "expired", "degraded",
]);
const CONNECTION_REASONS = Object.freeze([
  "cold_start", "direct_ok", "proxy_fallback", "session_expired",
  "possible_tun_interference", "credentials_rejected", "challenge",
  "service_unavailable", "unknown",
]);
const CONNECTION_ACTION_SET = Object.freeze([
  "login", "reauthenticate", "check-network", "retry", "open-settings", "close-tun-and-retry",
]);

/* 展示态是契约九态 + 消费端两个降级态（stale/unknown_state），句子级文案全部中性；
   TUN 表述只允许出现在 CONNECTION_REASON_HINTS（仅 possible_tun_interference，且保持“可能”）。 */
export const CONNECTION_STATE_TEXT = Object.freeze({
  off: "校园连接待确认",
  checking: "正在确认校园连接",
  ready: "校园连接正常",
  login_required: "需要登录复旦课程平台",
  reauthenticating: "正在重新认证校园会话",
  network_unavailable: "校园网络暂不可达",
  challenge_required: "需要在复旦页面完成安全验证",
  expired: "校园会话已过期",
  degraded: "校园服务部分可用",
  stale: "校园连接状态需要重新确认",
  unknown_state: "校园连接状态待确认",
});

/* CAMPUS-FIX 剩留清理（POLISH-1）：页眉胶囊随 AS4-U2b 退役，CONNECTION_PILL_TEXT
   唯一消费点（shell.js pill 死块）已拆，常量随之移除；状态词单一源=
   CONNECTION_STATE_TEXT。 */

export const CONNECTION_ACTION_LABELS = Object.freeze({
  login: "登录",
  reauthenticate: "重新认证",
  "check-network": "检查网络",
  retry: "重试",
  "open-settings": "打开设置",
  "close-tun-and-retry": "关闭 TUN 后重试",
});

/* 快照缺主动作时的兜底（与 state 对应，ready/checking/reauthenticating 保持安静） */
export const CONNECTION_DEFAULT_ACTIONS = Object.freeze({
  off: "check-network",
  checking: "",
  ready: "",
  login_required: "login",
  reauthenticating: "",
  network_unavailable: "check-network",
  challenge_required: "login",
  expired: "reauthenticate",
  degraded: "retry",
  stale: "check-network",
  unknown_state: "check-network",
});

const CONNECTION_PATH_LABELS = Object.freeze({ direct: "直连", local_proxy: "本机代理", unknown: "尚未确认" });
const CONNECTION_ROUTE_LABELS = Object.freeze({ webvpn: "WebVPN", icourse_direct: "课程平台直连", mixed: "混合路由", unknown: "尚未确认" });
/* POLISH-1 F4：导出供 shell.js degraded 点名复用（闭集单一源，禁两处漂移） */
export const CONNECTION_SERVICE_LABELS = Object.freeze({ webvpn: "WebVPN", icourse: "课程平台" });
const CONNECTION_SERVICE_STATE_LABELS = Object.freeze({ ready: "可用", checking: "检查中", unavailable: "不可用", unknown: "未确认" });
/* 消费端时效规则（契约 §6）：now − observed_at > 300s 视为过期，展示为中性复检态 */
const CONNECTION_STALE_SECONDS = 300;

const CONNECTION_REASON_HINTS = Object.freeze({
  possible_tun_interference: "直连暂时失败，可能与 TUN/系统代理有关（不一定）；可关闭后重试。",
});

/* P1-A 挑战感知细分文案：键 = authentication_snapshot.code 闭集（快照聚合态
   受冻结 v1 契约约束，细分事实由闭集错误码承载）。未知代码一律空串——
   回退到既有状态/原因文案，绝不把未知枚举渲染进 DOM。 */
export const CONNECTION_CODE_HINTS = Object.freeze({
  fudan_challenge_required: "需要在复旦页面完成安全验证：请登录并按页面提示完成验证。",
  fudan_account_locked: "账号已被锁定或冻结：请先在复旦账号服务解除锁定，再回来登录。",
  fudan_service_maintenance: "校园服务暂时维护：请稍后重试。",
  fudan_credentials_rejected: "账号或密码不正确：请核对后重新登录。",
});

export function connectionCodeHint(code) {
  return CONNECTION_CODE_HINTS[String(code || "")] || "";
}

function knownEnum(value, set) {
  return typeof value === "string" && set.includes(value) ? value : "";
}

export function extractConnectionSnapshot(payload) {
  if (!payload || typeof payload !== "object") return null;
  const direct = payload.connection;
  if (direct && typeof direct === "object" && !Array.isArray(direct) && direct.schema === CONNECTION_SCHEMA) return direct;
  for (const value of Object.values(payload)) {
    if (value && typeof value === "object" && !Array.isArray(value) && value.schema === CONNECTION_SCHEMA) return value;
  }
  return null;
}

function connectionServicesText(services) {
  const parts = [];
  for (const key of ["webvpn", "icourse"]) {
    const label = CONNECTION_SERVICE_LABELS[key];
    const service = services && typeof services === "object" ? services[key] : null;
    const stateLabel = service && typeof service === "object"
      ? (CONNECTION_SERVICE_STATE_LABELS[service.state] || CONNECTION_SERVICE_STATE_LABELS.unknown)
      : CONNECTION_SERVICE_STATE_LABELS.unknown;
    const verified = service && typeof service === "object" && service.verified === true ? "已验证" : "未验证";
    parts.push(`${label} ${stateLabel}（${verified}）`);
  }
  return parts.join(" · ");
}

const EMPTY_CONNECTION_VIEW = Object.freeze({
  fromBackend: false, state: "unknown_state", rawState: "", stale: false, reason: "",
  hint: "", actions: Object.freeze([]), generation: null, observedAt: null,
  pathText: "", servicesText: "",
});

function connectionView(fields) {
  return Object.freeze({ ...EMPTY_CONNECTION_VIEW, ...fields, actions: Object.freeze(fields.actions || []) });
}

export function connectionViewFromSnapshot(snapshot, nowSeconds = 0) {
  if (!snapshot || typeof snapshot !== "object" || snapshot.schema !== CONNECTION_SCHEMA) return null;
  const rawState = knownEnum(snapshot.state, CONNECTION_STATES);
  const reason = knownEnum(snapshot.reason, CONNECTION_REASONS);
  const actionList = Array.isArray(snapshot.actions) ? snapshot.actions.filter((action) => CONNECTION_ACTION_SET.includes(action)) : [];
  const observedAt = typeof snapshot.observed_at === "number" && Number.isFinite(snapshot.observed_at) && snapshot.observed_at > 0
    ? snapshot.observed_at : null;
  const generation = Number.isInteger(snapshot.generation) && snapshot.generation >= 0 ? snapshot.generation : null;
  const stale = observedAt != null && nowSeconds > 0 && nowSeconds - observedAt > CONNECTION_STALE_SECONDS;
  return connectionView({
    fromBackend: true,
    state: stale ? "stale" : (rawState || "unknown_state"),
    rawState,
    stale,
    reason,
    hint: CONNECTION_REASON_HINTS[reason] || "",
    actions: actionList,
    generation,
    observedAt,
    pathText: `${CONNECTION_PATH_LABELS[snapshot.network_path] || CONNECTION_PATH_LABELS.unknown} · ${CONNECTION_ROUTE_LABELS[snapshot.school_route] || CONNECTION_ROUTE_LABELS.unknown}`,
    servicesText: connectionServicesText(snapshot.services),
  });
}

/* 后端尚未发布快照时的诚实派生：复用既有 auth 快照，绝不虚构路由/时间/服务细节 */
export function connectionViewFromAuth(auth) {
  const state = String(auth?.state || "");
  const code = String(auth?.code || "");
  if (state === "ready") return connectionView({ state: "ready", rawState: "ready" });
  if (state === "checking") return connectionView({ state: "checking", rawState: "checking", actions: ["check-network"] });
  if (state === "degraded") return connectionView({ state: "degraded", rawState: "degraded", actions: ["reauthenticate"] });
  if (state === "expired" || code.includes("session_expired")) return connectionView({ state: "expired", rawState: "expired", actions: ["reauthenticate"] });
  return connectionView({ state: "login_required", rawState: "login_required", actions: ["login"] });
}

/* U5③：按闭集键清除本应用的浏览器侧持久化。住在 ui.js——settings.js 等
   模块带「永不触碰 localStorage」的隐私钉（防凭据入库），清除动作统一走
   这一个通道。 */
export function removeLocalKeys(keys) {
  for (const key of keys) {
    try {
      localStorage.removeItem(String(key));
    } catch { /* 存储不可用时清除失败不阻塞调用方 */ }
  }
}

const SLIDES_SKIPPED_REASONS = Object.freeze({
  empty: "空响应",
  html_body: "网页正文响应",
  json_body: "数据正文响应",
  unidentified_image: "未识别图像",
  decode_failed: "解码失败",
  ocr_failed: "识别失败",
  duplicate: "重复页面",
});

export function slidesSkippedText(skipped) {
  if (!skipped || typeof skipped !== "object") return "";
  return Object.entries(skipped).map(([reason, count]) => {
    const label = SLIDES_SKIPPED_REASONS[reason] || reason;
    const number = Number(count);
    return Number.isFinite(number) && number > 0 ? `${label} ×${number}` : label;
  }).join("、");
}

// ---- 浮层基座：焦点圈闭、Esc 栈、触发元素归还、背景主区 inert ----

const FOCUSABLE_SELECTOR = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
const overlayStack = [];
let overlayKeyListener = null;
let overlayPointerListener = null;

/* ---- 焦点政策 chokepoint（FOCUS-DRIFT-1，全应用统一语义；规则全文见
   docs/technical/focus-policy.md）----

   页面级快捷键（播放器 JKLA/空格/方向等）与「聚焦在交互元素上」的让位规则
   收口在这里，禁止各模块再自造 EDITABLE 选择器散点：

   ① 字段/自带方向语义面（输入框/滑杆/下拉源/菜单/标签页/选项）→ 一律让位：
      它们要么在编辑文本，要么方向键已是自己的导航语义（roving tab/listbox/
      menu），快捷键绝不能叠加双触发。
   ② button/a（点击激活面）→ 仅激活键（Space/Enter）让位给原生激活（键盘可达
      硬门：Tab 聚焦后必须仍能激活）；其余按键（方向/J/K/L/字母等）透传页面级
      快捷键——点击任意按钮后快捷键即刻存活，不再被焦点驻留拦死。
   ③ 模态 dialog 打开期间 → 页面快捷键一律让位（course-review Esc 闸先例），
      绝不隔模态操控背景播放器。 */
export const SHORTCUT_FIELD_SELECTOR = "input, select, textarea, [contenteditable='true'], [role='menu'], [role='listbox'], [role='tab'], [role='option']";
const ACTIVATION_KEYS = new Set([" ", "Enter"]);

export function pageShortcutBlockedByTarget(event) {
  const target = event?.target;
  if (!target || typeof target.closest !== "function") return false;
  if (target.closest(SHORTCUT_FIELD_SELECTOR)) return true;
  /* button/a：激活键走原生（键盘可达硬门），其余键透传播放/页面快捷键 */
  if (ACTIVATION_KEYS.has(String(event?.key)) && target.closest("button, a")) return true;
  return false;
}

export function hasOpenDialog(root = document) {
  return Boolean(root?.querySelector?.("dialog[open]"));
}

/* 指针激活后的焦点归还：鼠标（pointer 模态）点掉的 button/a 不许驻留焦点——
   页面级快捷键即刻回到未聚焦态。键盘模态（data-input="key"）绝不归还（Tab
   焦点与 :focus-visible 环是键盘用户的生命线）；非按钮/链接（输入框/滑杆等
   编辑面）不归还，其释放仍走各自既有路径（滑杆 pointerup/3s 自动归还）。 */
export function releasePointerActivatedFocus(scope, root = document) {
  const modalityRoot = root.documentElement || root;
  if (modalityRoot?.dataset?.input === "key") return false;
  const active = root.activeElement;
  if (!active || typeof active.blur !== "function" || typeof active.matches !== "function") return false;
  if (scope && typeof scope.contains === "function" && !scope.contains(active)) return false;
  if (!active.matches("button, a")) return false;
  active.blur();
  return true;
}

function focusableElements(root) {
  return [...root.querySelectorAll(FOCUSABLE_SELECTOR)]
    .filter((node) => !node.closest("[hidden]"));
}

function handleOverlayKeydown(event) {
  if (event.key !== "Tab") return;
  const root = event.currentTarget;
  const focusable = focusableElements(root);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const contained = root.contains(document.activeElement);
  if (event.shiftKey) {
    if (!contained || document.activeElement === first) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    }
  } else if (!contained || document.activeElement === last) {
    event.preventDefault();
    first.focus({ preventScroll: true });
  }
}

function handleDocumentKeydown(event) {
  /* IME 合成态早退（CJK-GUARD-1）：浮层（搜索面板等）输入框合成中按 Esc=
     取消候选，绝不当「关闭浮层」消费。 */
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Escape" && overlayStack.length) {
    const top = overlayStack[overlayStack.length - 1];
    if (typeof top.onEscape === "function" && top.onEscape() === true) return;
    closeOverlay(top.root);
  }
}

/* capture 在 document：主区 inert 时命中测试穿透到 BODY，仍能观察到关闭意图；只看栈顶，root/trigger 内不关（trigger 交给 click toggle，避免先关后开竞态） */
function handleDocumentPointerDown(event) {
  if (!overlayStack.length) return;
  const top = overlayStack[overlayStack.length - 1];
  if (!top.dismissOnOutside) return;
  if (top.root.contains(event.target) || top.trigger?.contains(event.target)) return;
  closeOverlay(top.root);
}

export function openOverlay({ root, trigger = null, onClose = null, onEscape = null, initialFocus = null, dismissOnOutside = false, returnFocus = null } = {}) {
  if (!root) return () => {};
  /* 单浮层策略：任一浮层已打开时禁止叠开（先 Esc 关闭再打开）；被挡时返回 null */
  if (overlayStack.length) return null;
  if (overlayStack.some((entry) => entry.root === root)) return null;
  const entry = {
    root,
    trigger,
    onClose,
    onEscape,
    /* 关闭归还锚：函数=关闭时刻求值（按当页状态归位），元素=静态锚；空则回落
       trigger（历史行为）。Esc 栈路径同样经 closeOverlay 收口，各关闭路径归锚一致 */
    returnFocus,
    dismissOnOutside: Boolean(dismissOnOutside),
    close: () => closeOverlay(root),
  };
  overlayStack.push(entry);
  root.hidden = false;
  if (!root.dataset.overlayKeybound) {
    root.dataset.overlayKeybound = "true";
    root.addEventListener("keydown", handleOverlayKeydown);
  }
  if (overlayStack.length === 1) {
    const main = $("workspace-main");
    if (main) main.inert = true;
    overlayKeyListener = handleDocumentKeydown;
    document.addEventListener("keydown", overlayKeyListener);
  }
  if (entry.dismissOnOutside && !overlayPointerListener) {
    overlayPointerListener = handleDocumentPointerDown;
    document.addEventListener("pointerdown", overlayPointerListener, { capture: true });
  }
  const target = initialFocus && root.contains(initialFocus) && !initialFocus.hidden
    ? initialFocus
    : focusableElements(root)[0] || root;
  window.requestAnimationFrame(() => target.focus({ preventScroll: true }));
  return entry.close;
}

export function closeOverlay(root) {
  const index = overlayStack.findIndex((entry) => entry.root === root);
  if (index < 0) return;
  const [entry] = overlayStack.splice(index, 1);
  root.hidden = true;
  if (!overlayStack.length) {
    const main = $("workspace-main");
    if (main) main.inert = false;
    if (overlayKeyListener) document.removeEventListener("keydown", overlayKeyListener);
    overlayKeyListener = null;
    if (overlayPointerListener) {
      document.removeEventListener("pointerdown", overlayPointerListener, { capture: true });
      overlayPointerListener = null;
    }
  }
  if (typeof entry.onClose === "function") entry.onClose();
  const anchor = typeof entry.returnFocus === "function" ? entry.returnFocus() : entry.returnFocus;
  /* 锚可能因重渲染离场：不在文档中即回落 trigger，绝不把焦点归还到孤儿节点 */
  const focusTarget = anchor && (typeof document.contains !== "function" || document.contains(anchor)) ? anchor : entry.trigger;
  focusTarget?.focus({ preventScroll: true });
}
