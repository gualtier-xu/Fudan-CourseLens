/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；远程计算面板（远程安装等待/动作/状态族）。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { apiV3, postV3 } from "../api.js";
import { $, clear, evidenceText, operationId, setBusy, textElement, toast } from "../ui.js";
import { GITHUB_REPO_FULL_NAME_RE, isPlainRecord } from "./shared.js";
/* WAIT-UX-1：按动作分布的等待预期闭集（数据来源/重测日期见 wait-expectations.js 头注） */
import { REMOTE_ACTION_EXPECTATION_FALLBACK, REMOTE_ACTION_EXPECTATIONS } from "../wait-expectations.js";

const REMOTE_COMPONENT_LABELS = Object.freeze({
  app: "客户端",
  github: "GitHub",
  worker: "Worker",
  channel: "加密通道",
  network: "网络",
  mailbox_history: "历史记录",
});

const REMOTE_STATE_LABELS = Object.freeze({
  ready: "正常",
  checking: "确认中",
  action_required: "需要操作",
  degraded: "降级",
  error: "异常",
  failed: "失败",
  unknown: "状态未知",
});

/* Mailbox 历史记录闭集：组件 code → 恢复建议（不含可修复态，可修复态显示修复卡） */
const MAILBOX_HISTORY_GUIDANCE = Object.freeze({
  mailbox_open_unconsumed: "存在未收口的 open 任务载荷：请先在任务中心恢复或取消对应任务，历史修复不适用。",
  mailbox_closed_active_conflict: "已关闭的历史记录与活动任务冲突：请先处理活动 run、Artifact 或租约，再重新诊断。",
  mailbox_metadata_drift: "Mailbox 记录的标题或标签发生漂移：为避免误判，请先在 GitHub 人工核对受管记录。",
  mailbox_issue_missing_active: "本地活动任务引用的 Mailbox 记录不存在：已停止新派发，请走任务恢复门禁。",
  mailbox_history_missing: "部分历史记录已在 GitHub 被删除：无法恢复原记录，仅保留审计警告。",
  mailbox_consumed_reopened: "已消费的历史记录被重新打开：请先人工确认后重新关闭，修复动作不会代改。",
  github_state_unknown: "Mailbox 历史状态暂无法确认（限流、超时或分页不完整）：请稍后重新诊断。",
});

/* P58 诚实降噪：历史盘点是非必需诊断，连接本身已验证时，盘点未完成
   （限流/超时/分页不完整，后端 connection.py fail-closed 记 unknown）降级为
   中性一行——不再把整个健康卡拖成「请稍后重新诊断」待办观感；
   连接未验证时保留原指引（此时它属于连接诊断语境）。 */
const MAILBOX_HISTORY_NEUTRAL_UNCHECKED_TEXT = "历史记录本次未能检查（限流或超时），不影响正常使用。";

function mailboxHistoryGuidanceText(value, history) {
  const code = String(history?.code || "");
  if (!code || code === "mailbox_closed_unconsumed") return ""; /* 可修复态走修复卡 */
  if (code === "github_state_unknown" && String(value?.overall?.state || "") === "ready") {
    return MAILBOX_HISTORY_NEUTRAL_UNCHECKED_TEXT;
  }
  return MAILBOX_HISTORY_GUIDANCE[code] || "";
}

/* 修复动作闭集失败文案：键 = 后端 error_code / operation result.error_code */
const MAILBOX_RECONCILE_FAILURE_TEXT = Object.freeze({
  mailbox_reconcile_failed: "修复未完成：GitHub 拒绝了本次写入或回读不一致。请稍后重试；若持续失败请先诊断连接。",
  mailbox_reconcile_stopped_activity: "修复中途检测到活动任务，已安全停止。请处理活动任务后重新诊断。",
  active_local_work_present: "仍有本地或远程任务活动。请等待任务结束后重试。",
  active_worker_run_present: "Worker 仍有活动 run。请等待其结束后重试。",
  worker_artifacts_present: "Worker 仍有关联 Artifact。请先完成结果导入或清理。",
  remote_cleanup_pending: "仍有待清理的 token、密钥或结果。请先完成清理。",
  worker_trust_unavailable: "Worker 签名模板校验未通过。请先修复 Worker。",
  installation_scope_not_exact: "CourseLens 遵循最小权限：请在 GitHub 安装设置中把仓库范围调整为仅选择它创建的两个仓库。",
  mailbox_reconcile_not_configured: "GitHub Worker 或 Mailbox 尚未初始化，无法修复历史记录。",
  mailbox_inventory_unavailable: "Mailbox 状态暂无法完整确认（限流、超时或分页不完整）。请稍后重新诊断后再试。",
  mailbox_open_unconsumed: "存在未收口的 open 任务载荷，历史修复不适用。",
  mailbox_metadata_drift: "Mailbox 元数据发生漂移，需要人工审计后再试。",
});
const MAILBOX_RECONCILE_GENERIC_FAILURE = "修复暂未完成，请稍后重新诊断后再试。";

export let authorizationTimer = 0;
let authorizationPollInterval = 5; /* 最近一次 pending 响应的轮询间隔（秒），失败续排按它起步 */
let authorizationExpiresAt = 0; /* 设备流程有效期（epoch 秒）：失败续排仅在该窗口内进行 */
let authorizationPollFailures = 0; /* poll-authorization 连续失败次数：任何成功响应即归零 */
const AUTHORIZATION_POLL_MAX_FAILURES = 3; /* 连续失败续排上限：超过即诚实报错并停排 */
const AUTHORIZATION_POLL_BACKOFF_CAP_SECONDS = 30; /* 退避上限：指数增长封顶，避免长等待 */
/* GH-UX-REWORK-1 首跑自动推进状态（全部恰一次守卫，失败即落回既有手动入口） */
let authorizationStartRequested = false; /* start-authorization 在途：点击即本地反馈（MF-5） */
let authorizationAutoReissues = 0; /* 验证码过期自动重发次数（≤2），用户手动发起即归零（MF-4） */
const AUTHORIZATION_AUTO_REISSUE_MAX = 2;
let autoBootstrapFiredForAuthorization = false; /* authorized 后自动初始化恰一次（MF-1） */
let bootstrapAutoChannelFired = false; /* bootstrap 收口后自动加密测试恰一次（MF-6） */
let bootstrapAutoContinued = false; /* worker_setup_incomplete 渲染期自动续跑恰一次（MF-7） */
export let remoteSnapshotValue = null; /* 最近一次后端快照：受信恢复链接的唯一来源 */
let remoteEvidenceContextPending = false; /* diagnose 完成后下一次证据摘要带「诊断结果：」上下文 */
let authorizationFlowActive = false; /* 设备码授权进行中：进度行仅在此时可见 */
/* 安装引导（待装 App 状态）：bootstrap 结果 awaiting_installation 置闩，
   installation 组件回到 ready 时解除；去重集合保证每个 setup URL 恰自动打开一次 */
let remoteInstallGuidanceArmed = false;
const remoteInstallAutoOpenedUrls = new Set();
/* 账号级安装页链接（遗留状态降级专用）：只认后端 awaiting 结果披露值，绝不本地发明 */
let remoteInstallAccountSetupUrl = "";
/* 最近一次后端证据披露的授权账号（授权/继续初始化结果携带），绝不本地发明 */
let remoteKnownGithubLogin = "";

/* 异步动作反馈（FLOW-ORDER-FIX-1 单元②）：提交后按钮进入进行中态 + 有界轮询
   连接快照，终态恰一次通知——杜绝「已提交后零反馈、用户盲点多次点击」
   （活体原话入档的痛点）。请求缝复用既有 GET remote-connection 读模型。 */
const ASYNC_FEEDBACK_ACTIONS = new Set(["bootstrap", "rotate-worker-keys", "test-channel", "repair-worker"]);
const REMOTE_ASYNC_PROGRESS_LABEL = Object.freeze({
  bootstrap: "初始化进行中…",
  "rotate-worker-keys": "轮换 Worker 密钥进行中…",
  "test-channel": "加密测试进行中…",
  "repair-worker": "修复 Worker 进行中…",
});
const REMOTE_ASYNC_DONE_TEXT = Object.freeze({
  bootstrap: "初始化完成，连接状态已更新",
  "rotate-worker-keys": "Worker 密钥已轮换，连接状态已更新",
  "test-channel": "加密测试完成，连接状态已更新",
  "repair-worker": "修复完成，连接状态已更新",
});
const REMOTE_ASYNC_TIMEOUT_TEXT = "操作仍在进行，可稍后手动诊断";
/* 分步进度（单元D）：后端连接快照 action_progress 的闭集阶段文案白名单——
   只认这张表的值，形状之外的标签一律回退到点击空窗补位首标签（单元B） */
const REMOTE_ACTION_STAGE_TEXT = Object.freeze(new Set([
  "正在创建仓库", "正在同步文档", "正在校验 Worker", "正在配置密钥",
  "正在校验加密通道", "正在确认连接状态",
]));
/* WAIT-UX-1 连接面板（用户点名面）：预期句按动作实测分布分档（WAIT-MEASURE-1
   R4/E2E2 锚点，闭集单源=wait-expectations.js，数据来源与重测日期见其头注）——
   bootstrap R4 实测 20.5s、加密测试 9s~60s、修复 Worker 约 113s，替代原先
   全动作一刀切的「约需一至两分钟」。 */
const REMOTE_ASYNC_EXPECTATION_TEXT = (action) => (
  REMOTE_ACTION_EXPECTATIONS[action] || REMOTE_ACTION_EXPECTATION_FALLBACK
);
/* 点击空窗补位（单元B/发现⑩b）：后端阶段字段到达前，进度行以闭集首标签
   （REMOTE_ACTION_STAGE_LABELS 既有值）+ 预期句呈现；通用「操作进行中」
   角标退役 */
const REMOTE_ACTION_FIRST_STAGE_TEXT = "正在确认连接状态";
/* 中途错误浮现纪律（单元D/发现⑦）：异步动作族的可自愈中间失败闭集——
   静默自动重试恰一次，终局结论恰一次通知，绝不「弹错→又成功」 */
const REMOTE_ACTION_SELFHEAL_CODES = new Set(["timeout", "rate_limited", "github_unreachable"]);
const REMOTE_ACTION_SELFHEAL_RETRY_MS = 4000;
const REMOTE_ACTION_SELFHEAL_NOTICE = "遇到临时限制，正在自动重试";
const REMOTE_ASYNC_POLL_INTERVAL_MS = 2000;
const REMOTE_ASYNC_POLL_DEADLINE_MS = 120000;
let remoteActionInProgress = ""; /* 进行中的异步动作名：进行中态随每次渲染重放 */
let asyncActionLocalStage = ""; /* 点击空窗补位首标签：后端阶段字段到达前常显 */
let asyncActionBaseline = ""; /* 提交时快照指纹：code|state 变化即终态 */
let asyncActionDeadline = 0;
export let asyncActionTimer = 0;

/* 分步进度闭集判读（单元D）：快照 action_progress 必须匹配当前动作且文案
   在白名单内，否则空串回退点击空窗补位首标签（单元B） */
function remoteActionStageText(value) {
  const progress = isPlainRecord(value?.action_progress) ? value.action_progress : null;
  if (!progress || String(progress.action || "") !== remoteActionInProgress) return "";
  const label = typeof progress.label === "string" ? progress.label.trim() : "";
  return REMOTE_ACTION_STAGE_TEXT.has(label) ? label : "";
}

/* 中途错误浮现纪律（单元D/发现⑦）：可自愈中间失败静默重试恰一次 */
function shouldSelfHealRetry(action, code, options) {
  return ASYNC_FEEDBACK_ACTIONS.has(action)
    && REMOTE_ACTION_SELFHEAL_CODES.has(String(code || ""))
    && options?._selfhealRetry !== true;
}

function scheduleSelfHealRetry(action, options) {
  toast(REMOTE_ACTION_SELFHEAL_NOTICE, "checking"); /* 非错误形态提示 */
  window.setTimeout(
    () => void remoteAction(action, { ...options, _selfhealRetry: true }),
    REMOTE_ACTION_SELFHEAL_RETRY_MS,
  );
}

/* 安装完成侦测（FRONTEND-SMOOTH-1 单元A）：安装页锚点点出后卡面进入
   「等待安装…」态——秒级轮询 remote-connection + 窗口聚焦立即重探；
   探针证据显示已安装（任意范围）即收口等待并自动推进主按钮到建仓
   （bootstrap 恰一次）。分钟级窗口到点静默收口，绝不发明错误。 */
const INSTALL_WAIT_POLL_MS = 3000;
const INSTALL_WAIT_WINDOW_MS = 300000; /* 5 分钟有界窗口 */
const INSTALL_WAIT_TEXT = "等待你在 GitHub 完成安装…安装完成后会自动继续";
/* 自动推进自述（单元B/发现⑩）：闭集文案，toast 与卡面状态行双通道同文播报 */
const INSTALL_ADVANCE_TEXT = "检测到 CourseLens App 已安装，自动继续初始化（约需半分钟到两分钟）";
export let installWaitTimer = 0;
export let installWaitDeadline = 0;
export let installWaitAdvanced = false;
let installWaitPolling = false;
let installWaitAdvanceAnnounced = false; /* 自述行常显窗口：异步动作收口即隐藏 */

/* 「已安装（任意范围）」闭集判定：与阶段行/主按钮同一份探针证据（单元B 单源） */
function installationEstablished(value) {
  const installation = installationComponent(value);
  if (!installation) return false;
  const code = String(installation.code || "");
  return String(installation.state || "") === "ready"
    || code === "installation_present"
    || code === "installation_scope_not_exact";
}

export function installWaitActive() {
  return installWaitDeadline > 0 && Date.now() < installWaitDeadline;
}

function renderInstallWait() {
  const line = $("remote-install-wait");
  if (!line) return;
  if (installWaitActive() && !installWaitAdvanced) {
    line.hidden = false;
    line.textContent = INSTALL_WAIT_TEXT;
    return;
  }
  /* 自动推进自述（单元B/发现⑩）：播报后、异步动作收口前状态行常显，
     绝不「静默开始」让学生误认卡死 */
  if (installWaitAdvanced && installWaitAdvanceAnnounced && remoteActionInProgress) {
    line.hidden = false;
    line.textContent = INSTALL_ADVANCE_TEXT;
    return;
  }
  line.hidden = true;
}

function finishInstallWait() {
  clearTimeout(installWaitTimer);
  installWaitTimer = 0;
  installWaitDeadline = 0;
  renderInstallWait();
}

function scheduleInstallWaitPoll(delay) {
  clearTimeout(installWaitTimer);
  installWaitTimer = window.setTimeout(() => void installWaitPollTick(), delay);
}

export async function installWaitPollTick() {
  if (installWaitPolling) return;
  if (!installWaitActive() || installWaitAdvanced) {
    finishInstallWait();
    return;
  }
  installWaitPolling = true;
  try {
    /* 等待窗新鲜语义（INIT-PATH-POLISH-1 单元A/发现⑨）：轮询强制新鲜探针，
       学生装完 App 后下一拍即见新证据；焦点重探同走本函数，同享新鲜语义 */
    await loadRemoteFresh();
    if (installWaitAdvanced) return;
    if (installationEstablished(remoteSnapshotValue)) {
      finishInstallWait();
      await autoAdvanceAfterInstallDetected();
      return;
    }
  } finally {
    installWaitPolling = false;
  }
  if (installWaitActive()) scheduleInstallWaitPoll(INSTALL_WAIT_POLL_MS);
  else finishInstallWait();
}

/* 检测到安装：自动推进主按钮到建仓（bootstrap 幂等、恰一次）；
   推进前显眼自述（toast + 卡面状态行同文，恰一次）——发现⑩ */
async function autoAdvanceAfterInstallDetected() {
  if (installWaitAdvanced || remoteActionInProgress) return;
  installWaitAdvanced = true;
  installWaitAdvanceAnnounced = true;
  toast(INSTALL_ADVANCE_TEXT, "ready");
  renderInstallWait();
  await remoteAction("bootstrap");
}

function beginInstallWait() {
  installWaitDeadline = Date.now() + INSTALL_WAIT_WINDOW_MS;
  installWaitAdvanced = false;
  installWaitAdvanceAnnounced = false;
  renderInstallWait();
  scheduleInstallWaitPoll(INSTALL_WAIT_POLL_MS);
}

/* 安装/范围锚点等待触发（单元A+单元D）：官方安装页锚点（预选/账号级形态）
   点出即进入安装等待；范围调整页锚点（settings_url 形态）点出即对称进入
   收紧等待。 */
function attachRemoteWaitTrigger(anchor, href) {
  if (!anchor) return;
  const target = String(href || "");
  if (INSTALLATION_SETTINGS_URL_RE.test(target)) {
    anchor.addEventListener("click", () => {
      beginTightenWait();
    });
    return;
  }
  if (!INSTALLATION_SETUP_URL_RE.test(target) && !INSTALLATION_SETUP_ACCOUNT_URL_RE.test(target)) return;
  anchor.addEventListener("click", () => {
    beginInstallWait();
  });
}

/* 收紧等待（INIT-PATH-POLISH-1 单元D）：范围调整页锚点点出后对称进入
   「等待收紧…」轮询——复用安装等待的节拍/窗口与单元A的新鲜语义；探针
   证据显示安装范围已精确（probe 仅在精确范围记录 installation_present）
   即显眼自述（toast + 状态行同文，恰一次）并自动继续初始化，消灭最后
   一次手动诊断点击。分钟级窗口到点静默收口，绝不发明错误。 */
const TIGHTEN_WAIT_TEXT = "等待你在 GitHub 收紧安装范围…收紧后会自动继续";
const TIGHTEN_ADVANCE_TEXT = "检测到安装范围已收紧，自动继续初始化（约需半分钟到两分钟）";
export let tightenWaitTimer = 0;
export let tightenWaitDeadline = 0;
export let tightenWaitAdvanced = false;
let tightenWaitPolling = false;
let tightenWaitAdvanceAnnounced = false;

/* 安装范围已精确闭集判定：与安装等待同一份探针证据（单源） */
function installationScopeExact(value) {
  const installation = installationComponent(value);
  if (!installation) return false;
  return String(installation.code || "") === "installation_present"
    || String(installation.state || "") === "ready";
}

export function tightenWaitActive() {
  return tightenWaitDeadline > 0 && Date.now() < tightenWaitDeadline;
}

function renderTightenWait() {
  const line = $("remote-tighten-wait");
  if (!line) return;
  if (tightenWaitActive() && !tightenWaitAdvanced) {
    line.hidden = false;
    line.textContent = TIGHTEN_WAIT_TEXT;
    return;
  }
  /* 自动推进自述（单元B 同框架）：播报后、异步动作收口前状态行常显 */
  if (tightenWaitAdvanced && tightenWaitAdvanceAnnounced && remoteActionInProgress) {
    line.hidden = false;
    line.textContent = TIGHTEN_ADVANCE_TEXT;
    return;
  }
  line.hidden = true;
}

function finishTightenWait() {
  clearTimeout(tightenWaitTimer);
  tightenWaitTimer = 0;
  tightenWaitDeadline = 0;
  renderTightenWait();
}

function scheduleTightenWaitPoll(delay) {
  clearTimeout(tightenWaitTimer);
  tightenWaitTimer = window.setTimeout(() => void tightenWaitPollTick(), delay);
}

export async function tightenWaitPollTick() {
  if (tightenWaitPolling) return;
  if (!tightenWaitActive() || tightenWaitAdvanced) {
    finishTightenWait();
    return;
  }
  tightenWaitPolling = true;
  try {
    await loadRemoteFresh(); /* 与安装等待同享等待窗新鲜语义（单元A） */
    if (tightenWaitAdvanced) return;
    if (installationScopeExact(remoteSnapshotValue)) {
      finishTightenWait();
      await autoAdvanceAfterTightenDetected();
      return;
    }
  } finally {
    tightenWaitPolling = false;
  }
  if (tightenWaitActive()) scheduleTightenWaitPoll(INSTALL_WAIT_POLL_MS);
  else finishTightenWait();
}

/* 检测到范围收紧：自动推进主按钮到初始化（bootstrap 幂等、恰一次）；
   推进前显眼自述（toast + 卡面状态行同文，恰一次）——发现⑩同框架 */
async function autoAdvanceAfterTightenDetected() {
  if (tightenWaitAdvanced || remoteActionInProgress) return;
  tightenWaitAdvanced = true;
  tightenWaitAdvanceAnnounced = true;
  toast(TIGHTEN_ADVANCE_TEXT, "ready");
  renderTightenWait();
  await remoteAction("bootstrap");
}

function beginTightenWait() {
  tightenWaitDeadline = Date.now() + INSTALL_WAIT_WINDOW_MS;
  tightenWaitAdvanced = false;
  tightenWaitAdvanceAnnounced = false;
  renderTightenWait();
  scheduleTightenWaitPoll(INSTALL_WAIT_POLL_MS);
}

/* 轮换 Worker 密钥次级按钮（单元②接线；INIT-PATH-POLISH-1 单元C 条件化上移）：
   仅当 environment 组件 action_required、缺钥清单非空，且探针已把
   rotate-worker-keys 计算进 repair action 时可见；位置上移至卡面主按钮区下方 */
function remoteRotateAction(value) {
  const environment = remoteComponentByName(value, "environment");
  if (!environment || String(environment.state || "") !== "action_required") return "";
  if (!remoteComponentMissingSecrets(environment).length) return "";
  const actions = Array.isArray(environment.actions) ? environment.actions : [];
  return actions.includes("rotate-worker-keys") ? "rotate-worker-keys" : "";
}

/* environment 关键证据上屏（单元③）：缺钥清单只认闭集密钥名（后端
   evidence.missing_secrets ⊆ {WORKER_INPUT_PRIVATE_KEY, WORKER_SIGNING_PRIVATE_KEY}），
   形状之外的值一律丢弃，绝不复制自由文本 */
const ENVIRONMENT_SECRET_NAMES = new Set(["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"]);

export function remoteComponentMissingSecrets(component) {
  const evidence = isPlainRecord(component?.evidence) ? component.evidence : {};
  if (!Array.isArray(evidence.missing_secrets)) return [];
  return evidence.missing_secrets
    .map((item) => String(item))
    .filter((item) => ENVIRONMENT_SECRET_NAMES.has(item));
}

/* GitHub 连接摘要闭集：state → 一行状态（唯一摘要行，替代通用默认块重复） */
const REMOTE_SUMMARY_TEXT = Object.freeze({
  ready: "GitHub 连接正常",
  checking: "正在确认 GitHub 连接",
  action_required: "GitHub 连接需要处理",
  degraded: "GitHub 连接部分受限",
  offline: "暂时无法连接 GitHub",
  error: "GitHub 连接出现异常",
  failed: "GitHub 连接出现异常",
  unknown: "GitHub 连接状态尚未确认",
});

/* overall code → 一行处置指引；未知代码回退为中性指引，不发明原因。
   首跑顺序：授权（含创建两个专属仓库的明示同意）→ 确认 App 安装（仓库已预选）→ 完成初始化 → 加密测试。
   installation_scope_not_exact 不在本表：走下方「最小权限+具名清单」锚点文案（单元E）。 */
const REMOTE_OVERALL_GUIDANCE = Object.freeze({
  installation_missing: "尚未安装 CourseLens App。专属仓库就绪后，点击下方按钮打开官方安装页：两个仓库已自动预选，只需确认安装一次，再回到客户端继续初始化。",
  authorization_missing: "尚未完成 GitHub 授权。请先在浏览器登录你的 GitHub 账号，再点下方按钮完成设备验证；按引导安装 App 并创建专属仓库，就能继续。",
  authorization_revoked: "GitHub 授权已失效。请点击下方按钮重新授权并创建专属仓库；已验证的 Worker 会保留。",
  permission_denied: "部分请求暂时未通过（可能为限流）。请先重新诊断；若反复出现，再重新授权。CourseLens 只访问它自己创建的两个仓库。",
  app_not_configured: "GitHub App 尚未配置。请先完成客户端的 App 配置。",
  worker_setup_incomplete: "GitHub 已授权，专属仓库尚未创建。点击下方按钮创建并完成初始化。",
  worker_tree_drifted: "Worker 版本发生漂移，需要修复后才能继续。",
  workflow_missing_or_disabled: "Worker 工作流缺失或被禁用，需要修复 Worker。",
  actions_disabled: "仓库 Actions 已被禁用，需要在 GitHub 上重新启用。",
  environment_incomplete: "Worker 环境配置不完整，需要初始化或轮换密钥。",
  channel_test_required: "加密通道尚未验证。点击下方按钮执行加密测试。",
  legacy_channel_test: "加密通道需要重新验证。点击下方按钮执行加密测试。",
  rate_limit_low: "GitHub API 配额偏低，连接确认可能延迟。",
});
const REMOTE_OVERALL_GUIDANCE_FALLBACK = "请使用下方推荐操作；详细信息可展开「高级操作与诊断」查看。";
/* 锚点文案（用户拍板草案，单元E）：权限类提示统一「最小权限+具名清单」框架。
   仓库名只取后端快照 configured_repositories（受管记录闭集插值），形状不对或
   缺失时退回无名单变体——绝不本地发明仓库名。 */
const REMOTE_SCOPE_EXACT_ANONYMOUS_GUIDANCE =
  "CourseLens 遵循最小权限：除了它自己创建的两个仓库，不会访问你账号里的其他任何仓库。请在 GitHub 安装设置中把仓库范围调整为仅选择这两个仓库。";
function managedRepositoryNames(value) {
  const repos = isPlainRecord(value?.configured_repositories) ? value.configured_repositories : {};
  const name = (item) => (typeof item === "string" && GITHUB_REPO_FULL_NAME_RE.test(item.trim()) ? item.trim() : "");
  return [name(repos.worker), name(repos.mailbox)];
}
function remoteScopeExactNamedGuidance(value) {
  const [worker, mailbox] = managedRepositoryNames(value);
  return worker && mailbox
    ? `CourseLens 遵循最小权限：除了它自己创建的两个仓库，不会访问你账号里的其他任何仓库。请在 GitHub 安装设置中把仓库范围调整为仅选择这两个仓库：${worker} 和 ${mailbox}。`
    : REMOTE_SCOPE_EXACT_ANONYMOUS_GUIDANCE;
}
/* 流程序重排（FLOW-ORDER-FIX-1 单元①）：仓库未建时的安装指引前置——旧序
   「先创建两个专属仓库再安装」文案退役。正确学生路径=授权→装 App（仓库范围
   先选 All repositories）→建仓→收紧为仅两仓→初始化→加密测试。 */
const REMOTE_INSTALLATION_MISSING_INSTALL_FIRST_GUIDANCE =
  "尚未安装 CourseLens App。点击下方按钮打开官方安装页：仓库范围先选 All repositories（专属仓库尚未建立，无法精确指定）；创建完成后我们会引导你收紧到仅两个仓库。";
/* 已安装但范围暂宽且仓库未建：先建仓再收紧，绝不先要求收紧（此时还无仓可圈）。
   同框架（单元E）：最小权限正向框架，不使用技术性责备措辞 */
const REMOTE_SCOPE_NOT_EXACT_PRE_REPOS_GUIDANCE =
  "CourseLens 遵循最小权限：专属仓库尚未创建。点击下方按钮先创建两个专属仓库；创建完成后再把安装范围收紧到仅这两个仓库。";

/* permission_denied 细分闭集：authorization 组件 evidence.endpoint_class → 指引。
   后端（github_app.ENDPOINT_CLASSES）只为每个 API 错误携带闭集端点类：
   只有身份/令牌端点被拒才指向重新授权；安装与仓库范围类 403（含未安装中间态、
   二级限流）保留授权，引导安装确认或稍后诊断，避免「重新授权还是报错」循环。 */
const REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT = Object.freeze({
  user_identity: "CourseLens 的授权已暂时失效，无法确认你的身份。请点击重新授权；CourseLens 只访问它自己创建的两个仓库。",
  token_endpoint: "CourseLens 的授权已暂时失效，无法确认安装状态。请点击重新授权；CourseLens 只访问它自己创建的两个仓库。",
  user_installations: "安装状态暂时无法确认（可能为网络波动，或安装尚未完成）。请稍后重新诊断；若持续出现，请在 GitHub 确认 CourseLens App 已安装。",
  user_repos: "专属仓库暂时无法创建。请确认当前账号允许创建仓库后重试；若反复出现，请重新诊断。",
  repos_detail: "两个专属仓库暂时无法读取：通常是 CourseLens App 尚未获得这两个仓库的访问权限。请完成 App 安装（两个仓库已预选），或在安装设置中仅选择这两个仓库。",
  repos_actions: "CourseLens 只需要这两个仓库的 Actions 权限来运行学习任务。请在安装设置中确认仓库范围为仅这两个仓库。",
});

/* 远程动作失败闭集：ApiError.code → 中文处置文案；未知代码走通用兜底。
   文案不含账号标识、仓库名、token 或原始异常文本 */
const REMOTE_ACTION_FAILURE_TEXT = Object.freeze({
  installation_scope_not_exact: "CourseLens 遵循最小权限：只访问它自己创建的两个仓库。请在 GitHub 安装设置中把仓库范围调整为仅选择这两个仓库，然后重试。",
  installation_missing: "授权未完成：当前 GitHub 账号尚未安装 CourseLens App。专属仓库就绪后请通过官方安装页确认安装（仓库已自动预选），然后重试。",
  managed_repository_name_conflict: "创建专属仓库未完成：GitHub 上已有同名但非 CourseLens 托管的仓库。请在 GitHub 重命名或删除该无关仓库后重试；不要将无关仓库选入 App。",
  managed_repository_invalid: "专属仓库读取未通过校验，已停止初始化。请重新诊断后再试。",
  authorization_missing: "授权未完成：尚未开始设备验证。请重新发起授权。",
  authorization_revoked: "GitHub 授权已失效。请重新发起授权。",
  permission_denied: "本次操作暂时未完成（可能为限流）。请先重新诊断连接；若诊断显示授权失效，再重新授权。CourseLens 只访问它自己创建的两个仓库。",
  rate_limited: "GitHub 请求过于频繁。请稍候片刻再重试。",
  github_unreachable: "暂时无法连接 GitHub。请确认网络后重试。",
  timeout: "操作等待超时。后端可能仍在处理，请稍后重新诊断。",
  network_unavailable: "网络暂时不可用。请确认网络后重试。",
  worker_trust_unavailable: "Worker 签名模板校验未通过。请先修复 Worker 再重试。",
  remote_cleanup_pending: "仍有待清理的临时凭据。请稍后重试。",
  operation_failed: "操作未完成。请稍后重试。",
});
const REMOTE_ACTION_FAILURE_GENERIC = "操作暂未完成。请稍后重试；若持续失败，可展开「高级操作与诊断」查看组件状态。";

/* 动作成功文案闭集：per-action 人话反馈，替代裸 operation.state。
   poll-authorization 的进度本就由设备码区域逐拍呈现，成功不另发 toast。 */
const REMOTE_ACTION_SUCCESS_TEXT = Object.freeze({
  diagnose: "已重新诊断，结果见下方连接状态",
  "start-authorization": "已发起授权，请在打开的 GitHub 页面完成验证",
  bootstrap: "初始化已提交，进度以下方连接状态为准",
  "repair-worker": "修复已提交，进度以下方连接状态为准",
  "test-channel": "加密测试已执行，结果见下方连接状态",
});

/* 云端处理状态行（CLOUD-CONSENT-AUTO-1 U1/U2）：开关退役——云端处理默认允许，
   控制面只剩「连接真值 + 一句透明说明」。三态与入队门同源（快照
   remote_compute 的 configured/verified），无该字段时整行不渲染
   （omit-not-fabricate），绝不发明状态。 */
const REMOTE_COMPUTE_STATUS_TEXT = Object.freeze({
  ready: "就绪",
  checking: "验证中",
  offline: "未连接",
});
const REMOTE_COMPUTE_STATUS_HINTS = Object.freeze({
  ready: "已连接：字幕、AI 总结等重活由你的专属云端仓库加密代算（只处理你勾选的课程），结果加密回传，云端最长保留 30 天。",
  checking: "连接已建立，加密通道还在确认；确认期间手动生成字幕 / AI 总结已经可以派发。",
  offline: "还没连接云端。完成上方 GitHub 连接后，手动生成字幕 / AI 总结即可用。",
});

function remoteComputeStatus(value) {
  const compute = isPlainRecord(value?.remote_compute) ? value.remote_compute : null;
  if (!compute) return null;
  if (compute.configured && compute.verified) return "ready";
  if (compute.configured) return "checking";
  return "offline";
}

function renderRemoteCompute(value) {
  const row = $("remote-compute-row");
  const pill = $("remote-compute-state");
  if (!row || !pill) return;
  const state = remoteComputeStatus(value);
  if (!state) {
    row.hidden = true;
    return;
  }
  row.hidden = false;
  pill.textContent = REMOTE_COMPUTE_STATUS_TEXT[state];
  pill.dataset.state = state === "ready" ? "ready" : state === "checking" ? "checking" : "unknown";
  const hint = $("remote-compute-hint");
  if (hint) hint.textContent = REMOTE_COMPUTE_STATUS_HINTS[state];
}

/* 撤销云端授权行：只在后端快照 actions 闭集给出该动作时出现 */
export function renderCloudRevokeRow(snapshot) {
  const row = $("cloud-revoke-row");
  if (!row) return;
  const actions = Array.isArray(snapshot?.actions)
    ? snapshot.actions.map((item) => String(item || ""))
    : [];
  row.hidden = !actions.includes("revoke-cloud-credentials");
}

export async function runCloudRevoke(button) {
  setBusy(button, true);
  try {
    await postV3("automation/actions", {
      action: "revoke-cloud-credentials",
      operation_id: operationId("cloud-revoke"),
    });
    toast("云端授权已撤销：云自动化已停止，凭据与状态会随之清理", "ready");
  } catch {
    toast("云端授权这次没有撤销成功，通常是网络临时波动；稍后在这一页再点一次。", "error");
  } finally {
    setBusy(button, false);
  }
}

// ---- 自动连接：偏好闭集文案（设置快照 auto_connect；不显示任何秘密） ----

const GITHUB_PHASE_LABELS = Object.freeze({
  authorization: "账号授权",
  installation: "App 安装",
  repositories: "专属仓库",
  channel: "加密通道",
});
const GITHUB_PHASE_STATE_TEXT = Object.freeze({
  done: "已完成", action_required: "需要操作", pending: "待进行",
});
const GITHUB_PILL_TEXT = Object.freeze({
  ready: "已连接", checking: "确认中", action_required: "需要操作", degraded: "部分受限",
  offline: "离线", error: "异常", failed: "异常", unknown: "待确认",
});

function githubPhaseStates(value) {
  const overall = (value && (value.overall || value)) || {};
  const byComponent = {};
  (Array.isArray(value?.components) ? value.components : []).forEach((component) => {
    if (component && typeof component === "object") {
      byComponent[String(component.component || component.name || "")] = component;
    }
  });
  const code = String(overall.code || "");
  const state = String(overall.state || "unknown");
  const auth = byComponent.authorization || {};
  const installation = byComponent.installation || {};
  const worker = byComponent.worker_repository || {};
  const mailbox = byComponent.mailbox_repository || {};
  const channel = byComponent.channel_test || {};
  let authorization = "pending";
  if (["authorization_missing", "authorization_revoked", "app_not_configured"].includes(code)) {
    authorization = "action_required";
  } else if (auth.state === "ready" || state === "ready") {
    authorization = "done";
  }
  let repositories = "pending";
  if (worker.state === "ready" && mailbox.state === "ready") repositories = "done";
  else if (
    /* 新序下专属仓库排在安装之后：安装未就绪时仓库阶段保持「待进行」，
       绝不与「安装 App」主按钮抢「需要操作」焦点 */
    ["installation_present", "installation_scope_not_exact"].includes(String(installation.code || ""))
    && (worker.state === "action_required" || mailbox.state === "action_required")
  ) {
    repositories = "action_required";
  }
  let installationPhase = "pending";
  if (installation.code === "installation_present" || installation.state === "ready") {
    installationPhase = "done";
  } else if (
    ["installation_missing", "installation_scope_not_exact", "installation_selection_unknown"]
      .includes(String(installation.code || ""))
  ) {
    installationPhase = "action_required";
  }
  let channelPhase = "pending";
  if (channel.code === "channel_test_valid") channelPhase = "done";
  else if (["channel_test_required", "legacy_channel_test"].includes(String(channel.code || ""))) {
    channelPhase = "action_required";
  }
  return {
    authorization,
    repositories,
    installation: installationPhase,
    channel: channelPhase,
  };
}

/* 阶段机单源化（FRONTEND-SMOOTH-1 单元B）：卡面标题与阶段行同源——同一份
   探针证据派生的阶段状态驱动；加密通道为唯一剩余动作时标题=「加密通道测试」，
   与主按钮（加密测试）同拍，消除「标题与阶段行数据源分裂」的矛盾态。 */
function githubConnectionTitleText(phases) {
  const othersDone = phases.authorization === "done"
    && phases.installation === "done"
    && phases.repositories === "done";
  return othersDone && phases.channel === "action_required" ? "加密通道测试" : "GitHub 连接";
}

function renderGithubPhases(value, states = githubPhaseStates(value)) {
  const list = $("github-phases");
  if (!list) return;
  clear(list);
  Object.entries(GITHUB_PHASE_LABELS).forEach(([key, label]) => {
    const state = states[key] || "pending";
    const item = document.createElement("li");
    item.dataset.phase = key;
    item.dataset.phaseState = state;
    item.append(
      textElement("span", label, "phase-label"),
      textElement("span", GITHUB_PHASE_STATE_TEXT[state] || "待进行", "phase-state-text"),
    );
    list.append(item);
  });
}

function remoteComponentText(component) {
  const label = REMOTE_COMPONENT_LABELS[component.name || component.component] || component.name || component.component || "连接";
  const state = REMOTE_STATE_LABELS[component.state] || "状态未知";
  return `${label}：${state}${component.code ? ` · ${component.code}` : ""}`;
}

function remoteSummaryText(overall) {
  const state = String(overall?.state || "unknown");
  return REMOTE_SUMMARY_TEXT[state] || REMOTE_SUMMARY_TEXT.unknown;
}

function remoteOverallGuidance(overall, value) {
  const code = String(overall?.code || "");
  if (code === "permission_denied") {
    /* 端点类细分：安装/范围类 403 绝不再引导重新授权 */
    const endpointClass = authorizationEndpointClass(value);
    if (endpointClass) return REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT[endpointClass];
  }
  if (code === "installation_missing") {
    /* 流程序重排：仓库未建 → 装 App 前置（All repositories 指引）；
       仓库已在（预装/部分建成）→ 既有安装语义，绝不再引导「先创建仓库」 */
    if (installationReposDenied(value) && dedicatedRepositoriesExist(value)) {
      return REMOTE_INSTALLATION_MISSING_REPOS_DENIED_GUIDANCE;
    }
    if (dedicatedRepositoriesExist(value)) {
      /* 预选链接只在两个专属仓库就绪后存在：无链接时文案绝不出现「已预选」 */
      return trustedRemoteInstallLink(value)
        ? REMOTE_OVERALL_GUIDANCE.installation_missing
        : REMOTE_INSTALLATION_MISSING_REPOS_DENIED_GUIDANCE;
    }
    return REMOTE_INSTALLATION_MISSING_INSTALL_FIRST_GUIDANCE;
  }
  if (code === "installation_scope_not_exact") {
    /* 仓库未建时先建仓、后收紧（新序）；仓库已建走「最小权限+具名清单」锚点文案（单元E） */
    return dedicatedRepositoriesExist(value)
      ? remoteScopeExactNamedGuidance(value)
      : REMOTE_SCOPE_NOT_EXACT_PRE_REPOS_GUIDANCE;
  }
  if (code && Object.hasOwn(REMOTE_OVERALL_GUIDANCE, code)) return REMOTE_OVERALL_GUIDANCE[code];
  const state = String(overall?.state || "unknown");
  return ["action_required", "degraded", "error", "failed", "offline", "unknown"].includes(state)
    ? REMOTE_OVERALL_GUIDANCE_FALLBACK
    : "";
}

/* permission_denied 的失败端点类（闭集）：只认后端证据里的 authorization 组件
   evidence.endpoint_class（github_app.ENDPOINT_CLASSES），其余一律空串 */
function authorizationEndpointClass(value) {
  const source = isPlainRecord(value) ? value : remoteSnapshotValue;
  if (!isPlainRecord(source) || !Array.isArray(source.components)) return "";
  const authorization = source.components.find(
    (component) => isPlainRecord(component) && (component.component === "authorization" || component.name === "authorization"),
  ) || null;
  const evidence = isPlainRecord(authorization?.evidence) ? authorization.evidence : {};
  const endpointClass = typeof evidence.endpoint_class === "string" ? evidence.endpoint_class : "";
  return Object.hasOwn(REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT, endpointClass) ? endpointClass : "";
}

/* T6 深分类：installation 组件 evidence.repos_denied（闭集端点类）=「未安装
   期间仓库读取被拒」——仓库已存在，指引走安装语义而不是创建/重新授权 */
function installationReposDenied(value) {
  const installation = installationComponent(value);
  const evidence = isPlainRecord(installation?.evidence) ? installation.evidence : {};
  const denied = typeof evidence.repos_denied === "string" ? evidence.repos_denied : "";
  return Object.hasOwn(REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT, denied) ? denied : "";
}

/* 主推荐动作闭集：overall.code → 动作与文案，反映后端首跑状态机的当前顺序：
   授权（并创建专属仓库）→ 打开官方安装页（仓库已预选）→ 完成初始化 → 加密测试 */
const REMOTE_PRIMARY_BY_CODE = Object.freeze({
  /* force 仅保留给明确的恢复语义：授权已被证实失效时才允许清除旧授权 */
  authorization_missing: { action: "start-authorization", label: "授权并创建专属仓库" },
  /* U⑫ 分流：令牌过期（可刷新/刷新失败）只处方「重新授权」——专属仓库
     已就绪时绝不把用户拽回全套首跑向导重演建仓 */
  authorization_refresh_required: { action: "start-authorization", label: "重新授权", force: true },
  authorization_revoked: { action: "start-authorization", label: "重新授权并创建专属仓库", force: true },
  /* permission_denied：仅身份/令牌端点被拒才与 revoked 同族 force 重新授权
     （后端也只在身份端点 403/401 时清除授权，github_app.py poll_device_authorization）；
     安装/范围类 403 保留授权，引导安装确认或诊断，避免「重新授权还是报错」循环 */
  permission_denied: { action: "start-authorization", label: "重新授权并创建专属仓库", force: true },
  worker_setup_incomplete: { action: "bootstrap", label: "创建专属仓库并完成初始化" },
  worker_tree_drifted: { action: "repair-worker", label: "修复 Worker" },
  workflow_missing_or_disabled: { action: "repair-worker", label: "修复 Worker" },
  environment_incomplete: { action: "bootstrap", label: "初始化 Worker" },
  channel_test_required: { action: "test-channel", label: "加密测试" },
  legacy_channel_test: { action: "test-channel", label: "加密测试" },
});

/* 专属仓库存在性证据（闭集）：至少一个仓库组件出现后端存在性代码
   （*_awaiting_installation/*_ready/*_invalid），或受信预选安装链接在场
   （后端仅在两仓库就绪后合成预选 id）——两者任一即认定已建；缺失/未初始化
   汇聚态（*_missing/worker_setup_incomplete/unknown）不算存在。
   单元B（终验发现②）：范围调整页链接（settings_url）不是建仓证据——它随
   「已安装但范围暂宽」出现，与两仓是否已建无关；误当证据会让「已装+未建仓」
   卡面提前引导收紧（活体事故复现路径），故只认预选形态链接。 */
function trustedRemotePreselectedSetupLink(snapshot) {
  const installation = installationComponent(snapshot);
  const evidence = isPlainRecord(installation?.evidence) ? installation.evidence : {};
  const setupUrl = evidence.installation_setup_url;
  return typeof setupUrl === "string" && INSTALLATION_SETUP_URL_RE.test(setupUrl)
    ? { href: setupUrl, label: "打开 CourseLens App 安装页（已预选两个专属仓库）" }
    : null;
}

function dedicatedRepositoriesExist(value) {
  if (trustedRemotePreselectedSetupLink(value)) return true;
  return ["worker_repository", "mailbox_repository"].some((name) => {
    const component = remoteComponentByName(value, name);
    if (!component) return false;
    const code = String(component.code || "");
    return [
      `${name}_awaiting_installation`, `${name}_ready`, `${name}_invalid`,
    ].includes(code);
  });
}

/* 受信账号级官方安装页（无预选仓库 id）：优先快照顶层 installation_url
   （后端捆绑配置披露），退化用 awaiting 证据收录值；两个闭集来源都没有就空串 */
function trustedRemoteAccountInstallUrl(value) {
  const disclosed = typeof value?.installation_url === "string" ? value.installation_url.trim() : "";
  if (INSTALLATION_SETUP_ACCOUNT_URL_RE.test(disclosed)) return disclosed;
  return INSTALLATION_SETUP_ACCOUNT_URL_RE.test(remoteInstallAccountSetupUrl)
    ? remoteInstallAccountSetupUrl
    : "";
}

function recommendedRemoteAction(value) {
  const overall = value?.overall || value;
  const code = String(overall?.code || "");
  const state = String(overall?.state || "unknown");
  /* P58 条件显示：全部就绪不再是「需要操作」——加密通道由后端指纹自动复核
     （connection.py _record_channel_test），正常态主按钮不占版面；
     仅 channel_test_required/legacy_channel_test 两个需要态经由下方映射显示加密测试 */
  if (state === "ready") return null;
  if (code === "installation_missing") {
    /* 流程序重排（FLOW-ORDER-FIX-1 单元①）：仓库未建 → 主按钮=安装 App
       （受信账号级安装页，All repositories 指引）；仓库已在（预装 403 证实
       存在）→ 安装语义，绝不是「先创建仓库」 */
    if (dedicatedRepositoriesExist(value)) {
      return trustedRemoteInstallLink(value)
        ? { action: "bootstrap", label: "完成 App 安装后继续初始化", withInstallLink: true }
        : { action: "bootstrap", label: "完成 App 安装后继续初始化" };
    }
    const accountUrl = trustedRemoteAccountInstallUrl(value);
    if (accountUrl) return { installAccountUrl: accountUrl, label: "安装 CourseLens App" };
    /* 无受信安装链接（App 未配置/零证据降级前）：保留建仓路径，不发明链接 */
    return { action: "bootstrap", label: "创建专属仓库并完成初始化" };
  }
  if (code === "installation_scope_not_exact") {
    /* 已安装（任意范围）且仓库未建 → 先建仓；仓库已建 → 收紧引导（现状） */
    if (!dedicatedRepositoriesExist(value)) {
      return { action: "bootstrap", label: "创建专属仓库并完成初始化" };
    }
    return { action: "bootstrap", label: "调整安装范围后继续初始化", withInstallLink: true };
  }
  if (code === "permission_denied") {
    /* 端点类细分：只有身份/令牌端点被拒才落到 force 重新授权映射；
       其余端点类（安装/范围/限流）保留授权——有受信安装链接就引导安装，
       否则仅诊断，绝不把用户推进重新授权循环 */
    const endpointClass = authorizationEndpointClass(value);
    if (endpointClass && endpointClass !== "user_identity" && endpointClass !== "token_endpoint") {
      const link = trustedRemoteInstallLink(value);
      if (link) return { action: "bootstrap", label: "完成 App 安装后继续初始化", withInstallLink: true };
      return { action: "diagnose", label: "诊断连接" };
    }
  }
  const mapped = REMOTE_PRIMARY_BY_CODE[code];
  if (mapped) return mapped;
  if (code.includes("auth")) return { action: "start-authorization", label: "授权并创建专属仓库" };
  if (code.includes("worker") || code.includes("bootstrap")) return { action: "bootstrap", label: "初始化 Worker" };
  return { action: "diagnose", label: "诊断连接" };
}

/* 受信恢复链接：只接受后端证据提供的闭集形态 URL，渲染为链接，绝不自动跳转。
   优先级：安装设置页（范围调整）> 预选两个仓库的官方安装页。
   通用无预选安装页不再作为回退：仓库不存在时绝不引导用户手动选择仓库 */
const INSTALLATION_SETTINGS_URL_RE = /^https:\/\/github\.com\/settings\/installations\/\d+$/;
const INSTALLATION_SETUP_URL_RE = /^https:\/\/github\.com\/apps\/[A-Za-z0-9.-]+\/installations\/new\/permissions\?suggested_target_id=\d+&repository_ids\[\]=\d+&repository_ids\[\]=\d+$/;
/* 账号级官方安装页（无预选仓库 id）：仅接受后端 awaiting 证据（复用路径 403
   零证据降级）披露的精确形态；仓库不存在时绝不出现（probe 不产生此形态） */
const INSTALLATION_SETUP_ACCOUNT_URL_RE = /^https:\/\/github\.com\/apps\/[A-Za-z0-9.-]+\/installations\/new$/;

function remoteComponentByName(snapshot, name) {
  const source = isPlainRecord(snapshot) ? snapshot : remoteSnapshotValue;
  if (!isPlainRecord(source) || !Array.isArray(source.components)) return null;
  return source.components.find(
    (component) => isPlainRecord(component) && (component.component === name || component.name === name),
  ) || null;
}

/* 跨账号陈旧绑定闭集提示（账号混淆事故根因）：仅当 worker/mailbox 组件
   evidence 携带 binding_owner_mismatch 布尔真值时渲染一行；无标记的既有
   场景按构造零受扰。文案为闭集常量，绝不拼接账号名或仓库名。 */
const BINDING_OWNER_MISMATCH_TEXT = "检测到其他账号的仓库绑定，已忽略；请为当前账号创建专属仓库";

function remoteBindingOwnerMismatch(value) {
  return ["worker_repository", "mailbox_repository"].some((name) => {
    const component = remoteComponentByName(value, name);
    const evidence = isPlainRecord(component?.evidence) ? component.evidence : {};
    return evidence.binding_owner_mismatch === true;
  });
}

function installationComponent(snapshot) {
  return remoteComponentByName(snapshot, "installation");
}

function trustedRemoteInstallLink(snapshot) {
  const installation = installationComponent(snapshot);
  const evidence = isPlainRecord(installation?.evidence) ? installation.evidence : {};
  const settingsUrl = evidence.settings_url;
  if (typeof settingsUrl === "string" && INSTALLATION_SETTINGS_URL_RE.test(settingsUrl)) {
    return { href: settingsUrl, label: "打开 GitHub App 安装设置" };
  }
  const setupUrl = evidence.installation_setup_url;
  if (typeof setupUrl === "string" && INSTALLATION_SETUP_URL_RE.test(setupUrl)) {
    return { href: setupUrl, label: "打开 CourseLens App 安装页（已预选两个专属仓库）" };
  }
  return null;
}

export function trustedRemoteAnchor(link) {
  const anchor = document.createElement("a");
  anchor.setAttribute("href", link.href);
  anchor.textContent = link.label;
  anchor.setAttribute("target", "_blank");
  anchor.setAttribute("rel", "noopener noreferrer");
  return anchor;
}

/* 卡面头部账号行（账号混淆事故驱动）：login 只取 authorization 组件 evidence
   既有字段与最近一次后端结果披露值，两个闭集来源都没有就如实显示「账号未知」，
   绝不发明值。GitHub login 形状之外的脏值一律按未知处理。 */
const GITHUB_LOGIN_RE = /^[A-Za-z0-9-]{1,39}$/;

function remoteAuthorizedLogin(value) {
  const authorization = remoteComponentByName(value, "authorization");
  const evidence = isPlainRecord(authorization?.evidence) ? authorization.evidence : {};
  const candidate = typeof evidence.login === "string" ? evidence.login.trim() : "";
  if (GITHUB_LOGIN_RE.test(candidate)) return candidate;
  return remoteKnownGithubLogin;
}

function renderGithubAccountLine(value) {
  const head = $("github-connection-state")?.parentElement || null;
  if (!head) return;
  let line = head.querySelector("#github-account-line");
  if (!line) {
    line = document.createElement("span");
    line.id = "github-account-line";
    line.className = "connection-account-line";
    head.append(line);
  }
  const login = remoteAuthorizedLogin(value);
  line.textContent = login ? `当前 GitHub 账号：${login}` : "当前 GitHub 账号：账号未知";
}

/* 安装引导块状态机：仅首装缺失态（installation_missing）在证据区渲染；
   范围暂宽（scope_not_exact）走既有路径不变。bootstrap 结果 awaiting_installation
   置闩后，installation 仍 action_required 也显示（快照可能滞后一步）。
   setupUrl 只接受后端证据里的闭集预选形态，供「每个新 URL 恰自动打开一次」。 */
function remoteInstallGuidanceState(value) {
  const installation = installationComponent(value);
  if (!installation || String(installation.state || "") !== "action_required") return null;
  if (String(installation.code || "") !== "installation_missing") return null;
  const link = trustedRemoteInstallLink(value);
  if (!link && !remoteInstallGuidanceArmed) return null;
  const evidence = isPlainRecord(installation.evidence) ? installation.evidence : {};
  const setupUrl = typeof evidence.installation_setup_url === "string"
    && INSTALLATION_SETUP_URL_RE.test(evidence.installation_setup_url)
    ? evidence.installation_setup_url
    : "";
  /* 账号级安装页仅在无预选链接时兜底使用：预选形态信息量更大，永远优先 */
  const accountUrl = !link && !setupUrl
    && INSTALLATION_SETUP_ACCOUNT_URL_RE.test(remoteInstallAccountSetupUrl)
    ? remoteInstallAccountSetupUrl
    : "";
  const reposDenied = Boolean(installationReposDenied(value));
  return { link, setupUrl, accountUrl, reposDenied };
}

function remoteInstallGuidanceBlock(state) {
  const block = document.createElement("div");
  block.className = "status-row";
  block.dataset.role = "install-guidance";
  const body = document.createElement("div");
  if (state.reposDenied) {
    /* T6 深分类：仓库已在，安装前读不了属正常——如实解释，绝口不提预选 */
    body.append(
      textElement("strong", "需要在 GitHub 确认安装 CourseLens App"),
      textElement("span", "两个专属仓库已就绪。安装完成前，GitHub 会拒绝读取它们，这是正常现象。请在 GitHub 完成安装后回到这里继续初始化。"),
    );
  } else if (state.accountUrl) {
    body.append(
      textElement("strong", "需要在 GitHub 确认安装 CourseLens App"),
      textElement("span", "这一步是 GitHub 平台要求的一次人工确认：专属仓库已就绪但暂无预选链接，请在安装页中勾选两个受管仓库（公共 Worker 与私有 Mailbox），不会触及你的其他仓库。"),
      textElement("span", "在新标签页完成安装后，回到这里点击「完成 App 安装后继续初始化」。"),
    );
  } else {
    body.append(
      textElement("strong", "需要在 GitHub 确认安装 CourseLens App"),
      textElement("span", "这一步是 GitHub 平台要求的一次人工确认：安装页已预选两个受管仓库（公共 Worker 与私有 Mailbox），不会触及你的其他仓库。"),
      textElement("span", "在新标签页完成安装后，回到这里点击「完成 App 安装后继续初始化」。"),
    );
  }
  block.append(body);
  if (state.link) {
    const guidanceAnchor = trustedRemoteAnchor(state.link);
    attachRemoteWaitTrigger(guidanceAnchor, state.link.href);
    block.append(guidanceAnchor);
  } else if (state.accountUrl) {
    const fallbackAnchor = trustedRemoteAnchor({ href: state.accountUrl, label: "打开 CourseLens App 安装页" });
    attachRemoteWaitTrigger(fallbackAnchor, state.accountUrl);
    block.append(fallbackAnchor);
  }
  return block;
}

/* 安装缺失且仓库未建的失败文案变体（流程序重排后唯一缺装处方）：与指引同族
   ——装 App 前置，绝口不提「先创建仓库」与「已预选」 */
const REMOTE_ACTION_INSTALLATION_MISSING_INSTALL_FIRST_TEXT =
  "授权未完成：当前 GitHub 账号尚未安装 CourseLens App。请先点击「安装 CourseLens App」完成安装（仓库范围先选 All repositories），安装完成后再继续初始化。";

/* 「仓库已建」门配套（单元B）：已安装但两仓未建时，范围类失败处方=先建仓，
   与主按钮同拍；绝不提前引导收紧（此时还无仓可圈）。（单元E 同框架） */
const REMOTE_ACTION_SCOPE_NOT_EXACT_PRE_REPOS_TEXT =
  "CourseLens 遵循最小权限：专属仓库尚未创建。点击「创建专属仓库并完成初始化」先建仓；创建完成后再把安装范围收紧到仅这两个仓库。";

/* permission_denied 的缺装处方（证据分流）：建仓/动作失败且当前证据含安装缺失
   或仓库预装 403 时，处方改为「请先安装 CourseLens App」路径——重新授权在此
   场景永远无效（活体事故：学生必撞 403 后被推去反复重授权）。授权组件确已
   失效时保留既有重新授权处方（见 remoteActionAuthorizationInvalid）。 */
const REMOTE_ACTION_INSTALL_FIRST_PRESCRIPTION =
  "请先安装 CourseLens App：点击「安装 CourseLens App」打开官方安装页，仓库范围先选 All repositories；安装完成后再回到客户端继续初始化。";

/* T6 深分类：仓库级 403 且未安装——仓库在、只是安装前读不了。
   专用指引解释这一现象，绝不引导「先创建仓库」或「重新授权」 */
const REMOTE_INSTALLATION_MISSING_REPOS_DENIED_GUIDANCE =
  "尚未安装 CourseLens App：安装完成前，GitHub 会拒绝读取两个专属仓库，这是正常现象。请在 GitHub 完成 App 安装后，回到这里继续初始化。";

/* 动作失败时的端点类证据：扫描当前快照全部组件的闭集 evidence.endpoint_class
   （_record_probe_error 落 authorization，_record_repo_denial_unknown 落仓库组件），
   只认 REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT 的键（闭集校验），其余一律空串 */
function remoteActionEndpointClass() {
  if (!isPlainRecord(remoteSnapshotValue) || !Array.isArray(remoteSnapshotValue.components)) return "";
  for (const component of remoteSnapshotValue.components) {
    const evidence = isPlainRecord(component?.evidence) ? component.evidence : {};
    const endpointClass = typeof evidence.endpoint_class === "string" ? evidence.endpoint_class : "";
    if (Object.hasOwn(REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT, endpointClass)) return endpointClass;
  }
  return "";
}

/* 授权组件确已失效（action_required）才保留「重新授权」处方；缺装证据优先于
   端点类表，但绝不覆盖真实授权失效 */
function remoteActionAuthorizationInvalid() {
  const authorization = remoteComponentByName(remoteSnapshotValue, "authorization");
  return Boolean(authorization) && String(authorization.state || "") === "action_required";
}

/* 当前快照是否携带缺装证据（闭集）：安装组件 installation_missing，或安装
   证据带仓库预装 403 端点类（repos_denied） */
function remoteActionInstallMissingEvidence() {
  if (remoteActionAuthorizationInvalid()) return false;
  const installation = installationComponent(remoteSnapshotValue);
  if (!installation) return false;
  if (String(installation.code || "") === "installation_missing") return true;
  return Boolean(installationReposDenied(remoteSnapshotValue));
}

function remoteActionFailureText(code) {
  const normalized = typeof code === "string" ? code.trim() : "";
  if (normalized === "installation_missing" && !trustedRemoteInstallLink()) {
    return REMOTE_ACTION_INSTALLATION_MISSING_INSTALL_FIRST_TEXT;
  }
  if (normalized === "installation_scope_not_exact" && !dedicatedRepositoriesExist()) {
    /* 「仓库已建」门（单元B）：两仓未建时范围类失败绝不引导收紧——
       处方与主按钮同拍（先建仓），收紧引导仅在两仓存在时登场 */
    return REMOTE_ACTION_SCOPE_NOT_EXACT_PRE_REPOS_TEXT;
  }
  if (normalized === "permission_denied") {
    /* 处方随证据分流：缺装证据 → 安装处方（绝不推重新授权）；
       其余保持按端点文案表（身份/令牌类才指向重新授权） */
    if (remoteActionInstallMissingEvidence()) {
      return REMOTE_ACTION_INSTALL_FIRST_PRESCRIPTION;
    }
    const endpointClass = remoteActionEndpointClass();
    if (endpointClass) return REMOTE_PERMISSION_GUIDANCE_BY_ENDPOINT[endpointClass];
  }
  return (normalized && Object.hasOwn(REMOTE_ACTION_FAILURE_TEXT, normalized))
    ? REMOTE_ACTION_FAILURE_TEXT[normalized]
    : REMOTE_ACTION_FAILURE_GENERIC;
}

/* 动作失败内联呈现：闭集文案 +（安装范围类）受信链接 + 重试；toast 仅作辅助。
   观测透码：闭集失败码（及已知端点类）以 chip 原样可见，人话文案不吞掉可诊断性 */
function showRemoteActionError(code, retryAction, retryOptions) {
  const region = $("remote-action-error");
  if (!region) return;
  clear(region);
  region.append(textElement("span", remoteActionFailureText(code)));
  const normalized = typeof code === "string" ? code.trim() : "";
  if (normalized === "installation_scope_not_exact" || normalized === "installation_missing") {
    const link = trustedRemoteInstallLink();
    if (link) {
      const linkAnchor = trustedRemoteAnchor(link);
      attachRemoteWaitTrigger(linkAnchor, link.href); /* 范围调整页锚点→收紧等待（单元D） */
      region.append(linkAnchor);
    }
  }
  if (normalized) {
    const chip = document.createElement("code");
    chip.className = "remote-error-code";
    const endpointClass = normalized === "permission_denied" ? remoteActionEndpointClass() : "";
    chip.textContent = endpointClass ? `${normalized} · ${endpointClass}` : normalized;
    region.append(chip);
  }
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重试";
  retry.addEventListener("click", () => void remoteAction(retryAction, retryOptions));
  region.append(retry);
  region.hidden = false;
}

/* 初始化完成总结态（INIT-PATH-POLISH-1 单元D）：全绿时一次性登场——闭集
   文案总结成果与下一步；确认后归档为常规连接卡；断开再达成的红→绿沿才
   重新登场（绝不反复打扰） */
const REMOTE_COMPLETION_TEXT = Object.freeze({
  title: "初始化完成",
  body: "两个专属仓库已就绪，加密通道已验证。",
  next: "现在回到课程页即可开始学习。",
  confirm: "知道了",
});
let remoteCompletionSummaryShown = false; /* 本次达成是否已登场（确认后归档） */
let remoteCompletionPrevReady = false; /* 红绿沿检测：新达成才重新登场 */

function remoteCompletionSummaryBlock() {
  const block = document.createElement("div");
  block.className = "status-row remote-completion-summary";
  block.dataset.role = "completion-summary";
  const body = document.createElement("div");
  body.append(
    textElement("strong", REMOTE_COMPLETION_TEXT.title),
    textElement("span", REMOTE_COMPLETION_TEXT.body),
    textElement("span", REMOTE_COMPLETION_TEXT.next),
  );
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.textContent = REMOTE_COMPLETION_TEXT.confirm;
  confirm.addEventListener("click", () => {
    remoteCompletionSummaryShown = false;
    renderRemote(remoteSnapshotValue); /* 确认后归档为常规连接卡 */
  });
  /* CLOUD-CONSENT-AUTO-1 U2：原「去开启云端处理」次级入口随开关退役删除——
     云端处理默认允许、连接就绪即可用，总结态只留「知道了」，不再指向不存在的控件。 */
  block.append(body, confirm);
  return block;
}

/* ---- GH-UX-REWORK-1 首跑自动推进助手（全部恰一次守卫；失败落回既有
   手动入口，绝不循环重试） ---- */

/* 验证码过期自动重发（MF-4）：经 recommendedRemoteAction 状态驱动映射发起
   （与主按钮同一入口形态，不另立静态授权入口）；快照缺失/陈旧时回退到与
   authorization_missing 同一动作形态（非 force：仅签发新设备码，绝不清除
   既有授权）。≤2 次守卫，用户手动发起（无 _autoReissue 标记）即归零 */
function autoFireStartAuthorization() {
  const recommended = recommendedRemoteAction(remoteSnapshotValue || {});
  const resolved = recommended && recommended.action === "start-authorization"
    ? recommended
    : { action: "start-authorization", label: "授权并创建专属仓库" };
  void remoteAction(resolved.action, {
    ...(resolved.force === true ? { force: true } : {}),
    _autoReissue: true,
  });
}

/* bootstrap 收口即自动加密测试（MF-6）：echo 为既有同意面的最后一门；
   仅在无在途动作且快照明确 channel_test_required 时恰一次触发。
   返回是否已触发：触发时收口面的「点击下方按钮」指引行与之矛盾，应省略 */
function maybeAutoTestChannel() {
  if (bootstrapAutoChannelFired || remoteActionInProgress) return false;
  const overall = remoteSnapshotValue?.overall || {};
  if (String(overall.code || "") !== "channel_test_required") return false;
  bootstrapAutoChannelFired = true;
  toast("初始化已完成，正在自动校验加密通道…", "checking");
  void remoteAction("test-channel");
  return true;
}

/* 授权在案但初始化未完成（崩溃/重启遗留）自动续跑恰一次（MF-7）：与后端
   跨会话收口（poll 无设备对象路径）同一同意语义——bootstrap 已随授权
   披露征得同意；非瞬态失败经既有错误面呈现，本守卫不重试 */
function maybeAutoContinueBootstrap() {
  if (bootstrapAutoContinued || remoteActionInProgress) return;
  const overall = remoteSnapshotValue?.overall || {};
  if (String(overall.code || "") !== "worker_setup_incomplete") return;
  bootstrapAutoContinued = true;
  void remoteAction("bootstrap");
}

export function renderRemote(value) {
  remoteSnapshotValue = isPlainRecord(value) ? value : null;
  const overall = value?.overall || value || {};
  const pill = $("github-connection-state");
  pill.textContent = GITHUB_PILL_TEXT[String(overall.state || "unknown")] || GITHUB_PILL_TEXT.unknown;
  pill.dataset.state = String(overall.state || "unknown");
  const phases = githubPhaseStates(value); /* 阶段状态单源：标题与阶段行共用（单元B） */
  renderGithubPhases(value, phases);
  const titleNode = $("github-connection-title");
  if (titleNode) titleNode.textContent = githubConnectionTitleText(phases);
  renderGithubAccountLine(value);
  renderInstallWait(); /* 等待安装行随每次渲染重放（单元A） */
  renderTightenWait(); /* 收紧等待行随每次渲染重放（单元D） */
  /* 初始化完成总结态（单元D）：红→绿沿触发一次性登场 */
  const allGreen = String(overall.state || "") === "ready";
  if (allGreen && !remoteCompletionPrevReady) remoteCompletionSummaryShown = true;
  remoteCompletionPrevReady = allGreen;
  /* 「操作进行中」阶段行（单元B）：主按钮正下方常显——后端闭集阶段标签
     优先，点击空窗期以闭集首标签补位，预期句全程在场；通用角标文案退役 */
  const actionProgressLine = $("remote-action-progress");
  if (actionProgressLine) {
    if (remoteActionInProgress) {
      const stageText = remoteActionStageText(value)
        || asyncActionLocalStage
        || REMOTE_ACTION_FIRST_STAGE_TEXT;
      actionProgressLine.hidden = false;
      actionProgressLine.textContent = `${stageText} · ${REMOTE_ASYNC_EXPECTATION_TEXT(remoteActionInProgress)}`;
    } else {
      actionProgressLine.hidden = true;
    }
  }
  /* 诊断同源（单元③）：卡内安全证据行与应用级诊断一律以本次连接快照为准，
     消除「应用级 authorization_missing vs 卡面 authorization_valid」矛盾 */
  const securityEvidence = $("security-evidence");
  if (securityEvidence) securityEvidence.textContent = evidenceText(overall);
  renderRemoteCompute(value); /* 云端处理状态行随快照重放（U2：只读陈述） */
  const target = $("remote-evidence");
  clear(target);
  /* diagnose 完成后的第一次渲染给摘要行加上下文前缀，随后恢复正常摘要 */
  const summaryLine = remoteSummaryText(overall);
  target.append(textElement("strong", remoteEvidenceContextPending ? `诊断结果：${summaryLine}` : summaryLine));
  remoteEvidenceContextPending = false;
  const guidance = remoteOverallGuidance(overall, value);
  if (guidance) target.append(textElement("span", guidance));
  if (remoteBindingOwnerMismatch(value)) {
    target.append(textElement("span", BINDING_OWNER_MISMATCH_TEXT));
  }
  const installedNow = installationComponent(value);
  if (installedNow && String(installedNow.state || "") === "ready") {
    remoteInstallGuidanceArmed = false;
    remoteInstallAccountSetupUrl = ""; /* 安装就绪：账号级兜底链接随之失效 */
  }
  const installGuidance = remoteInstallGuidanceState(value);
  if (installGuidance) {
    target.append(remoteInstallGuidanceBlock(installGuidance));
    /* 每个新 setup URL 恰自动打开一次（模块级去重）；弹出被拦截时退化为
       块内受信链接与主行按钮，绝不重试弹、绝不自动跳转非受信形态 */
    const autoOpenUrl = installGuidance.setupUrl || installGuidance.accountUrl;
    if (autoOpenUrl && !remoteInstallAutoOpenedUrls.has(autoOpenUrl)) {
      remoteInstallAutoOpenedUrls.add(autoOpenUrl);
      try {
        window.open(autoOpenUrl, "_blank", "noopener,noreferrer");
      } catch {
        /* 弹出被拦截：保持已渲染链接按钮 */
      }
      /* GH-UX-REWORK-1（MF-2）：自动打开与锚点点击对称布防等待——3s 新鲜
         轮询 + 检测到安装/收紧即自动继续初始化，用户装完回客户端零点击 */
      if (INSTALLATION_SETTINGS_URL_RE.test(autoOpenUrl)) beginTightenWait();
      else beginInstallWait();
    }
  }
  const recommended = recommendedRemoteAction(value);
  const primary = $("remote-primary-action");
  clear(primary);
  if (!recommended) {
    /* P58：就绪态无常驻主按钮（正常态不占版面）；进行中反馈不受影响——
       阶段行 remote-action-progress 独立于主按钮常显 */
    primary.hidden = true;
  } else {
    primary.hidden = false;
    if (recommended.withInstallLink) {
      const link = trustedRemoteInstallLink(value);
      if (link) {
        const linkAnchor = trustedRemoteAnchor(link);
        attachRemoteWaitTrigger(linkAnchor, link.href); /* 范围调整页锚点→收紧等待（单元D） */
        primary.append(linkAnchor);
      }
    }
    if (recommended.installAccountUrl) {
      /* 流程序重排：主按钮=安装 App（受信账号级安装页锚，主按钮样式）；
         推荐安装锚不发远程动作请求——打开安装页是外部动作 */
      const installAnchor = trustedRemoteAnchor({
        href: recommended.installAccountUrl,
        label: recommended.label,
      });
      installAnchor.className = "button-link primary";
      attachRemoteWaitTrigger(installAnchor, recommended.installAccountUrl);
      primary.append(installAnchor);
    } else {
      const primaryButton = document.createElement("button");
      primaryButton.type = "button";
      primaryButton.className = "primary";
      primaryButton.textContent = recommended.label;
      if (remoteActionInProgress) {
        /* 异步反馈进行中态（随每次渲染重放，轮询刷新后不丢）：
           disabled + aria-busy +「进行中…」文案 */
        primaryButton.disabled = true;
        primaryButton.setAttribute("aria-busy", "true");
        primaryButton.textContent = REMOTE_ASYNC_PROGRESS_LABEL[remoteActionInProgress]
          || `${recommended.label} · 进行中…`;
      } else {
        primaryButton.addEventListener("click", () => remoteAction(
          recommended.action,
          recommended.force === true ? { force: true } : {},
        ));
      }
      primary.append(primaryButton);
    }
  }
  const rotateAction = remoteRotateAction(value);
  const rotateRow = $("remote-rotate-row");
  rotateRow.hidden = !rotateAction || Boolean(remoteActionInProgress);
  const history = (value?.components || []).find(
    (component) => (component.component || component.name) === "mailbox_history" && !component.stale,
  ) || null;
  const reconcilable = Boolean(history && history.code === "mailbox_closed_unconsumed");
  const reconcileCard = $("remote-mailbox-reconcile");
  reconcileCard.hidden = !reconcilable;
  if (!reconcilable) {
    /* 非可修复态：复位交互（修复进行中除外），恢复建议走证据区，不显示执行按钮 */
    if (!mailboxReconcileInFlight) $("remote-reconcile-mailbox").disabled = false;
    const stateLine = $("remote-mailbox-reconcile-state");
    stateLine.hidden = true;
    stateLine.textContent = "";
    const mailboxGuidance = history ? mailboxHistoryGuidanceText(value, history) : "";
    if (mailboxGuidance) target.append(textElement("span", mailboxGuidance));
  }
  const components = $("remote-component-evidence");
  clear(components);
  (value?.components || []).forEach((component) => {
    components.append(textElement("span", remoteComponentText(component)));
    /* 证据上屏（单元③）：environment 缺钥清单以「缺什么」摘要行可见，
       只认闭集密钥名，零自由文本 */
    const missing = remoteComponentMissingSecrets(component);
    if (missing.length) {
      components.append(textElement("span", `缺少密钥：${missing.join("、")}`));
    }
  });
  /* 设备码进度行仅在授权流程进行中可见，不再常驻默认块 */
  $("remote-device-authorization").hidden = !authorizationFlowActive;
  /* GH-UX-REWORK-1（MF-7）：渲染期自动续跑授权在案而初始化未完成的首次流程 */
  maybeAutoContinueBootstrap();
  /* 全绿总结态（单元D）：登场的达成总结置于证据区顶部，确认后归档 */
  if (allGreen && remoteCompletionSummaryShown) {
    target.prepend(remoteCompletionSummaryBlock());
  }
}

/* 设备码进度（item 13）：pending 时渲染等宽大字验证码 + 一键复制；复制失败
   按钮保持可点（验证码始终可选中手动复制），授权完成/失败回到单行文本。
   验证码只进 DOM，绝不写日志或任何持久位置。 */
function renderDeviceAuthorization(result) {
  const authorization = $("remote-device-authorization");
  if (!authorization) return;
  clear(authorization);
  if (result.state === "requesting") {
    /* GH-UX-REWORK-1（MF-5）：验证码请求在途的本地即时反馈——慢网最长 30s
       也不再是点击空窗；POST 返回后按真实状态重绘 */
    authorization.append(textElement("span", "正在向 GitHub 请求验证码…"));
    return;
  }
  if (result.state === "pending") {
    authorization.append(textElement("span", "等待 GitHub 授权确认"));
    if (result.user_code) {
      const code = textElement("code", String(result.user_code), "device-code-value");
      code.id = "remote-device-code";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "device-code-copy";
      copy.textContent = "复制验证码";
      copy.setAttribute("aria-label", "复制 GitHub 设备验证码");
      copy.addEventListener("click", async () => {
        const value = String($("remote-device-code")?.textContent || code.textContent || "");
        if (!value) return;
        try {
          await navigator.clipboard.writeText(value);
          copy.textContent = "已复制";
        } catch {
          copy.textContent = "复制失败，点击重试";
        }
        window.setTimeout(() => { copy.textContent = "复制验证码"; }, 2500);
      });
      const line = document.createElement("div");
      line.className = "device-auth-line";
      line.append(code, copy);
      authorization.append(line);
    }
    /* GH-UX-REWORK-1（MF-4）：剩余有效期逐拍可见——expires_at 为后端闭集
       披露值，向上取整到分钟；过期自动更新（见 remoteAction 过期分支） */
    const remainingSeconds = Math.round(authorizationExpiresAt - Date.now() / 1000);
    if (remainingSeconds > 0) {
      const minutes = Math.max(1, Math.ceil(remainingSeconds / 60));
      authorization.append(textElement("span", `验证码约 ${minutes} 分钟内有效，过期会自动更新`));
    }
    /* REALRUN-1 P1-1（2026-10-08 真测）：设备码页零前提说明——测试浏览器无
       GitHub Web 会话直撞英文登录墙，目标人群多数无 GitHub 账号。真 GitHub
       授权页打开前把前提说在人前面（人情味+讲人话，不责备），PAT 不能用于
       GitHub 网页登录，只说「登录你的 GitHub 账号」。 */
    authorization.append(textElement(
      "span",
      "请先在浏览器登录你的 GitHub 账号；如果打开的页面要求登录，登录后回来输入上面的验证码。",
    ));
    return;
  }
  authorization.textContent = result.state === "authorized"
    ? (result.setup_state === "awaiting_installation"
      ? "GitHub 已授权 · 专属 Worker 与 Mailbox 已就绪，请在 GitHub 确认安装 CourseLens App"
      : result.setup_state === "bootstrap_failed"
        ? `GitHub 已授权 · 专属仓库创建未完成（${result.setup_error_code || "operation_failed"}），请按推荐操作继续`
        : result.setup_state === "pending_bootstrap"
          ? "GitHub 已授权 · 正在初始化专属仓库…"
          : "GitHub 已确认授权")
    /* REALRUN-1 P3-5 / 清单 #2：兜底行不上英文裸码——过期与失败都给下一步 */
    : result.state === "expired"
      ? "验证码已过期。点击下方「授权并创建专属仓库」可获取新验证码；已完成的授权不会丢失。"
      : "GitHub 授权暂时没有完成。请点击下方「授权并创建专属仓库」重新发起；若反复出现，请稍后再试。";
}

export async function loadRemote() {
  /* 动作期快照不塌（单元C）：空响应/瞬态失败不覆盖已知好快照——保留上一好
     卡面（stale-while-revalidate 消费端），仅有从未拿到好快照时才渲染诚实
     未知摘要；进行中状态由「操作进行中」角标说明。 */
  try {
    const value = await apiV3("remote-connection");
    if (isPlainRecord(value)) {
      renderRemote(value);
      return;
    }
  } catch {
    /* 落入下方守卫：有好快照则原样保留 */
  }
  if (!remoteSnapshotValue) renderRemote(null); /* 快照不可用：一行诚实摘要，不渲染原始错误文本 */
}

/* 等待窗新鲜读（INIT-PATH-POLISH-1 单元A/发现⑨）：?fresh=1 让后端强制一次
   新探针（窗口内重复轮询被后端合并）——等待安装/收紧等待轮询与焦点重探
   专用；普通 loadRemote 保持无参读模型，常规请求不受影响。失败守卫与
   loadRemote 同款：有好快照原样保留，绝不清卡。 */
async function loadRemoteFresh() {
  try {
    const value = await apiV3("remote-connection?fresh=1");
    if (isPlainRecord(value)) {
      renderRemote(value);
      return;
    }
  } catch {
    /* 落入下方守卫：有好快照则原样保留 */
  }
  if (!remoteSnapshotValue) renderRemote(null);
}

/* D1 动作后自动对账：poll 授权确认 / bootstrap 成功后恰一次同步探测
   （复用 diagnose 动作管道），再刷新卡面——消灭「动作成功、卡面 stale、
   让用户自点诊断」的悬空态。探测失败静默退化为既有快照刷新，绝不连环重试；
   diagnose 自身不再触发（无递归），也不借用显式诊断的「诊断结果：」前缀。 */
async function remoteAutoReconcileOnce() {
  try {
    await postV3("remote-connection/actions", {
      action: "diagnose",
      operation_id: operationId("remote"),
      force: false,
    });
  } catch {
    /* 对账失败不阻塞：下方 loadRemote 仍按既有快照刷新 */
  }
}

export async function remoteAction(action, options = {}) {
  $("remote-action-error").hidden = true; /* 新动作开始：清除旧错误 */
  if (action === "start-authorization") {
    /* GH-UX-REWORK-1（MF-5）点击即本地反馈：验证码请求慢网最长 30s，先呈现
       请求中形态，不等 POST 返回；新授权会话复位自动推进守卫与重发额度 */
    authorizationStartRequested = true;
    autoBootstrapFiredForAuthorization = false;
    if (!options._autoReissue) authorizationAutoReissues = 0;
    const authorization = $("remote-device-authorization");
    authorization.hidden = false;
    authorization.dataset.state = "checking";
    renderDeviceAuthorization({ state: "requesting" });
  }
  const feedbackAction = ASYNC_FEEDBACK_ACTIONS.has(action);
  if (feedbackAction && remoteActionInProgress !== action) {
    /* 点击即反馈（单元B/发现⑩b）：提交空窗期按钮立即 busy + 闭集首标签，
       绝不等 POST 返回才开始反馈 */
    beginRemoteActionFeedback(action, true);
  }
  try {
    const value = await postV3("remote-connection/actions", {
      action,
      operation_id: operationId("remote"),
      force: options.force === true,
    });
    const result = value.operation?.result || {};
    /* 后端结果披露的授权账号与待装状态：账号行的第二闭集来源；
       awaiting_installation 是安装引导闩的唯一置位点（快照可能滞后一步） */
    const disclosedLogin = typeof result.login === "string" ? result.login.trim() : "";
    if (GITHUB_LOGIN_RE.test(disclosedLogin)) remoteKnownGithubLogin = disclosedLogin;
    if (result.setup_state === "awaiting_installation") remoteInstallGuidanceArmed = true;
    /* 遗留状态降级：后端 awaiting 证据披露的账号级安装页为第二受信形态，
       仅此刻收录（有预选链接时预选形态优先），不发明仓库 id */
    const awaitingNow = result.setup_state === "awaiting_installation";
    const disclosedAccountSetupUrl = typeof result.installation_setup_url === "string"
      ? result.installation_setup_url.trim() : "";
    if (awaitingNow && INSTALLATION_SETUP_ACCOUNT_URL_RE.test(disclosedAccountSetupUrl)) {
      remoteInstallAccountSetupUrl = disclosedAccountSetupUrl;
    }
    if (result.verification_uri && action === "start-authorization") {
      window.open(result.verification_uri, "_blank", "noopener,noreferrer");
    }
    if (["start-authorization", "poll-authorization"].includes(action)) {
      const authorization = $("remote-device-authorization");
      authorizationStartRequested = false;
      authorizationFlowActive = result.state === "pending";
      if (result.state === "pending") {
        /* 有效期先入状态再渲染（GH-UX-REWORK-1）：首拍 pending 即呈现剩余
           有效期，不等下一拍 */
        authorizationPollInterval = Math.max(5, Number(result.interval || authorizationPollInterval || 5));
        authorizationExpiresAt = Number(result.expires_at || 0);
      }
      authorization.dataset.state = result.state === "authorized" ? "ready" : result.state === "pending" ? "checking" : "error";
      renderDeviceAuthorization(result);
      clearTimeout(authorizationTimer);
      authorizationPollFailures = 0;
      if (result.state === "pending") {
        /* RFC 8628 §3.5：slow_down 后后端回传 +5s 的 interval；缺省时保持
           既有间隔（绝不静默重置回 5s 重新触发慢放） */
        authorizationTimer = window.setTimeout(
          () => void remoteAction("poll-authorization"),
          authorizationPollInterval * 1000,
        );
      }
      if (action === "poll-authorization" && result.state === "expired"
        && authorizationAutoReissues < AUTHORIZATION_AUTO_REISSUE_MAX) {
        /* GH-UX-REWORK-1（MF-4）：过期自动重发（≤2 次守卫）——新码呈现后
           用户直接输码即可，不再落入「expired + 手动重授权」死端 */
        authorizationAutoReissues += 1;
        toast("验证码已过期，正在自动更新验证码", "checking");
        autoFireStartAuthorization();
      } else if (action === "poll-authorization" && result.state === "authorized"
        && result.setup_state === "pending_bootstrap" && !autoBootstrapFiredForAuthorization) {
        /* GH-UX-REWORK-1（MF-1）：授权确认即刻呈现（设备码区「正在初始化…」），
           初始化自动续跑——bootstrap 走异步反馈+分步阶段+自愈重试通道，
           零用户点击 */
        autoBootstrapFiredForAuthorization = true;
      }
      /* pending/初始化自动续跑期间设备码区保持可见（MF-1 状态常显） */
      const showInitializing = action === "poll-authorization" && result.state === "authorized"
        && result.setup_state === "pending_bootstrap";
      authorization.hidden = !authorizationFlowActive && !showInitializing;
      if (showInitializing && !remoteActionInProgress) {
        /* MF-1：初始化自动续跑——bootstrap 走异步反馈+分步阶段+自愈重试通道，
           零用户点击（恰一次：autoBootstrapFiredForAuthorization 已置位） */
        void remoteAction("bootstrap");
      }
    }
    /* 成功反馈走 per-action 人话闭集；操作真失败（HTTP 200 + operation failed）
       仍给闭集错误文案；poll 的进度由设备码区域呈现，不重复 toast */
    const operationState = String(value.operation?.state || "unknown");
    if (operationState === "failed") {
      const failureCode = String(value.operation?.error_code || "operation_failed");
      if (shouldSelfHealRetry(action, failureCode, options)) {
        /* 可自愈中间失败（限流/超时/瞬断）：不弹错误窗，静默重试恰一次 */
        scheduleSelfHealRetry(action, options);
        await loadRemote();
        return;
      }
      /* 操作真失败：闭集 error_code 走同一按码文案表 + 内联区（含透码 chip 与重试），
         不再一律落 operation_failed 兜底文案；终局失败先解除进行中态 */
      finishRemoteActionFeedback();
      showRemoteActionError(failureCode, action, options);
      toast(remoteActionFailureText(failureCode), "error");
    } else if (ASYNC_FEEDBACK_ACTIONS.has(action)) {
      /* 异步反馈（单元②）：进行中态已在动作入口建立（点击即反馈），此处仅在
         反馈被提前收口（超时竞态等）时重启，绝不重置死线 */
      if (remoteActionInProgress !== action) beginRemoteActionFeedback(action);
    } else {
      const successText = REMOTE_ACTION_SUCCESS_TEXT[action];
      if (successText) toast(successText, "ready");
    }
    if (action === "diagnose") remoteEvidenceContextPending = true;
    /* 动作后自动对账恰一次（D1）：授权确认与 bootstrap 完成即同步探测再刷新 */
    if (
      (action === "poll-authorization" && result.state === "authorized")
      || (action === "bootstrap" && operationState !== "failed")
    ) {
      await remoteAutoReconcileOnce();
    }
    await loadRemote();
    /* GH-UX-REWORK-1（MF-6）：bootstrap 收口后快照只剩加密通道一门即自动
       执行恰一次（echo 为既有同意面的最后一门） */
    if (action === "bootstrap" && operationState !== "failed") maybeAutoTestChannel();
  } catch (error) {
    const code = typeof error?.code === "string" ? error.code : "";
    if (action === "poll-authorization" && authorizationFlowActive
      && authorizationPollFailures < AUTHORIZATION_POLL_MAX_FAILURES
      && (!authorizationExpiresAt || Date.now() / 1000 < authorizationExpiresAt)) {
      /* 单次瞬态失败不终止在途设备流程：按 max(interval, 5s) 起步的有界退避续排；
         超过连续失败上限或设备流程已过期时走下方既有诚实报错 + 重试 */
      authorizationPollFailures += 1;
      clearTimeout(authorizationTimer);
      const backoffSeconds = Math.min(
        authorizationPollInterval * 2 ** (authorizationPollFailures - 1),
        AUTHORIZATION_POLL_BACKOFF_CAP_SECONDS,
      );
      authorizationTimer = window.setTimeout(
        () => void remoteAction("poll-authorization"),
        backoffSeconds * 1000,
      );
      toast(`授权确认暂时失败，正在重试（${authorizationPollFailures}/${AUTHORIZATION_POLL_MAX_FAILURES}）`, "checking");
      await loadRemote().catch(() => {});
      return;
    }
    if (shouldSelfHealRetry(action, code, options)) {
      /* 提交被拒的可自愈中间失败：不弹错误窗，静默重试恰一次 */
      scheduleSelfHealRetry(action, options);
      await loadRemote().catch(() => {});
      return;
    }
    if (action === "start-authorization") {
      /* MF-5：请求验证码终局失败——撤掉本地「请求中」形态再呈现错误，
         卡面回到既有推荐动作入口 */
      authorizationStartRequested = false;
      authorizationFlowActive = false;
      const authorization = $("remote-device-authorization");
      authorization.hidden = true;
    }
    if (feedbackAction) finishRemoteActionFeedback(); /* 提交被拒终局：解除进行中态再呈现错误 */
    showRemoteActionError(code, action, options);
    toast(remoteActionFailureText(code), "error");
    await loadRemote().catch(() => {}); /* 失败后刷新证据：恢复链接以后端快照为准 */
  }
}

/* ---- 异步动作反馈：进行中态 + 有界轮询 + 终态恰一次通知（单元②） ----
   点击即反馈（单元B/发现⑩b）：beginRemoteActionFeedback(action, true) 在
   remoteAction 入口调用——提交空窗期主按钮立即 busy、进度行立即显示闭集
   首标签，不再等 POST 返回才开始反馈。 */

function beginRemoteActionFeedback(action, immediate = false) {
  const overall = remoteSnapshotValue?.overall || {};
  asyncActionBaseline = `${String(overall.code || "")}|${String(overall.state || "")}`;
  remoteActionInProgress = action;
  asyncActionLocalStage = immediate ? REMOTE_ACTION_FIRST_STAGE_TEXT : "";
  asyncActionDeadline = Date.now() + REMOTE_ASYNC_POLL_DEADLINE_MS;
  clearTimeout(asyncActionTimer);
  asyncActionTimer = window.setTimeout(
    () => void pollRemoteActionOutcome(),
    REMOTE_ASYNC_POLL_INTERVAL_MS,
  );
  /* 点击瞬间即重放：busy + 首标签不等任何网络往返 */
  if (immediate && remoteSnapshotValue) renderRemote(remoteSnapshotValue);
}

/* 终态判定（闭集）：快照 ready，或 code|state 指纹相对提交时发生变化 */
function asyncActionSettled(value) {
  const overall = value?.overall || value || {};
  if (String(overall.state || "") === "ready") return true;
  const current = `${String(overall.code || "")}|${String(overall.state || "")}`;
  return current !== asyncActionBaseline;
}

function finishRemoteActionFeedback() {
  remoteActionInProgress = "";
  asyncActionLocalStage = "";
  clearTimeout(asyncActionTimer);
}

async function pollRemoteActionOutcome() {
  const action = remoteActionInProgress;
  if (!action) return;
  let snapshot = null;
  try {
    snapshot = await apiV3("remote-connection");
  } catch {
    snapshot = null; /* 单次轮询失败不终止：继续有界轮询 */
  }
  if (snapshot && asyncActionSettled(snapshot)) {
    const overall = snapshot.overall || {};
    const code = String(overall.code || "");
    finishRemoteActionFeedback();
    await loadRemote();
    /* MF-6：收口即自动加密测试恰一次；触发时收口指引行省略（自述已同义） */
    const autoAdvanced = action === "bootstrap" ? maybeAutoTestChannel() : false;
    if (String(overall.state || "") === "ready") {
      toast(REMOTE_ASYNC_DONE_TEXT[action] || "连接状态已更新", "ready");
    } else if (code === "permission_denied" || Object.hasOwn(REMOTE_ACTION_FAILURE_TEXT, code)) {
      /* 失败终态：闭集码 + 按单元①规则分流的处方（缺装证据 → 安装处方） */
      toast(remoteActionFailureText(code), "error");
    } else if (!autoAdvanced) {
      /* 推进到下一步要求：给该状态的一行指引，不再是零反馈悬空 */
      toast(remoteOverallGuidance(overall, snapshot) || "连接状态已更新", "checking");
    }
    return;
  }
  if (snapshot) renderRemote(snapshot); /* 进行中态随最新快照重放 */
  if (Date.now() >= asyncActionDeadline) {
    finishRemoteActionFeedback();
    await loadRemote();
    toast(REMOTE_ASYNC_TIMEOUT_TEXT, "checking");
    return;
  }
  asyncActionTimer = window.setTimeout(
    () => void pollRemoteActionOutcome(),
    REMOTE_ASYNC_POLL_INTERVAL_MS,
  );
}

// ---- Mailbox 历史记录修复：确认框 + 防双击 + 以后端证据归零为准 ----

let mailboxReconcileInFlight = false;

function mailboxReconcileFailureText(code) {
  return MAILBOX_RECONCILE_FAILURE_TEXT[String(code || "")] || MAILBOX_RECONCILE_GENERIC_FAILURE;
}

export async function runMailboxReconcile() {
  if (mailboxReconcileInFlight) return; /* 防双击：进行中忽略重复触发 */
  const button = $("remote-reconcile-mailbox");
  const stateLine = $("remote-mailbox-reconcile-state");
  mailboxReconcileInFlight = true;
  setBusy(button, true);
  button.setAttribute("aria-busy", "true");
  stateLine.hidden = false;
  stateLine.textContent = "正在修复历史记录，请稍候…";
  try {
    const value = await postV3("remote-connection/actions", {
      action: "reconcile-mailbox-history",
      operation_id: operationId("mailbox-reconcile"),
    });
    const result = value.operation?.result || {};
    await loadRemote(); /* 重新读取后端证据，前端不自行宣称完成 */
    const remaining = Number(result.remaining_count || 0);
    if (value.operation?.state === "failed" || result.reconcile_complete !== true) {
      const text = mailboxReconcileFailureText(result.error_code);
      stateLine.hidden = false;
      stateLine.textContent = remaining > 0 ? `${text}（剩余 ${remaining} 条待修复）` : text;
      toast("历史记录修复未全部完成", "error");
    } else if ($("remote-mailbox-reconcile").hidden) {
      toast("历史记录修复完成", "ready");
    } else {
      stateLine.hidden = false;
      stateLine.textContent = "修复已执行，但后端仍报告待修复记录；请重新诊断。";
      toast("后端仍报告待修复记录", "checking");
    }
  } catch (error) {
    const text = mailboxReconcileFailureText(error?.code);
    stateLine.hidden = false;
    stateLine.textContent = text;
    toast(text, "error");
    await loadRemote().catch(() => {});
  } finally {
    mailboxReconcileInFlight = false;
    setBusy(button, false);
    button.removeAttribute("aria-busy");
    if (!$("remote-mailbox-reconcile").hidden) button.focus({ preventScroll: true });
  }
}

/* ARCH-DEBT-1 缝合点：installSettings 卸载清理的唯一入口（原文六行原样搬入，
   顺序保持）；ESM 导入绑定只读，故以具名拆卸助手替代门面直写两个 deadline。 */
export function teardownRemoteActionState() {
  clearTimeout(authorizationTimer);
  clearTimeout(asyncActionTimer);
  clearTimeout(installWaitTimer);
  installWaitDeadline = 0;
  clearTimeout(tightenWaitTimer); /* 收紧等待与安装等待同款卸载清理（单元D） */
  tightenWaitDeadline = 0;
}
