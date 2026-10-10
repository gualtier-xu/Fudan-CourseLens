/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；登录对话框（登录对话框族）。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { apiV3, postV3 } from "../api.js";
import { $, clear, operationId, textElement, toast } from "../ui.js";
import { isPlainRecord } from "./shared.js";
import { AUTO_CONNECT_ERROR_GENERIC, AUTO_CONNECT_ERROR_TEXT, autoConnectValue, postAutoConnectPreference, renderAutoConnectPreference, setAutoConnectValue } from "./auto-connect.js";
import { loadAccounts } from "../settings.js"; /* ESM 函数声明提升循环边：仅事件期调用（finishLoginSuccess 刷新账户），求值期零调用，语义安全 */

const LOGIN_POLL_MS = 2000;
const GENERIC_LOGIN_FAILURE = "登录暂时未完成。请稍候重试；若持续失败，可在设置中复制连接诊断。";
/* 键 = authentication_snapshot（state/code）与提交被拒时的 error_code 闭集；未知代码走通用兜底 */
const LOGIN_STAGE_TEXT = Object.freeze({
  checking: "后端正在连接并验证复旦会话，请稍候。",
  action_required: "登录已提交，等待后端开始处理。",
});
const LOGIN_FAILURE_TEXT = Object.freeze({
  fudan_login_failed: "登录未完成：可能是网络或校园服务波动，系统已自动重试仍未成功。请稍候约 90 秒后再试。",
  fudan_credentials_rejected: "学号或密码不正确，请核对后重新输入。",
  fudan_challenge_required: "需要在复旦页面完成安全验证。请重新登录并按页面提示完成验证；系统不会自动重试。",
  fudan_account_locked: "账号已被锁定或冻结，请先在复旦账号服务解除锁定，再回来登录。",
  fudan_service_maintenance: "校园服务暂时维护，请稍后重试。",
  timeout: "登录请求超时。后端会自动退避重试，请稍等片刻，或稍后重新提交。",
  network_unavailable: "网络暂时不可用，登录未完成。请确认网络后重试。",
  fudan_session_expired: "复旦会话已过期。请重新提交登录。",
  fudan_login_required: "需要登录复旦课程平台。请重新提交登录。",
  fudan_credentials_missing: "尚未填写学号或密码。请填写后再提交。",
  saved_account_unavailable: "已保存的账号暂时不可用。请直接输入学号与密码登录。",
  authentication_request_invalid: "登录请求未被接受。请检查输入后重试。",
});

/* 阶段闭集：键 = authentication_snapshot.step；未知 step 按缺失处理 */
const LOGIN_STEP_TEXT = Object.freeze({
  webvpn: "校园网关",
  icourse: "课程平台",
});

function loginStageText(value) {
  const attempt = Number(value?.attempt || 0);
  const maxAttempts = Number(value?.max_attempts || 0);
  if (attempt < 1 || maxAttempts < 1) return "";
  const step = LOGIN_STEP_TEXT[String(value?.step || "")];
  return `正在验证 · 第 ${attempt}/${maxAttempts} 次${step ? ` · ${step}` : ""}`;
}

let loginInFlight = false;
let loginPollTimer = 0;
/* F8（N5FE-P1）：登录链路硬超时——后端卡死（如 webvpn 挂起）时学生曾面对
   永不恢复的「验证中」禁用态；10s 后按钮回落可重试，后台流程照常收口 */
const LOGIN_DEADLOCK_TIMEOUT_MS = 10000;
let loginDeadlockTimer = 0;

function armLoginDeadlockTimer() {
  window.clearTimeout(loginDeadlockTimer);
  loginDeadlockTimer = window.setTimeout(() => {
    loginDeadlockTimer = 0;
    if (!loginInFlight) return;
    loginInFlight = false;
    setLoginBusy(false);
    const submit = $("login-submit");
    if (submit) submit.textContent = "重新登录";
    setLoginStatus("登录用时较长：流程仍在后台继续，完成后会自动更新。也可以核对网络后重新登录。", "checking");
  }, LOGIN_DEADLOCK_TIMEOUT_MS);
}

function clearLoginDeadlockTimer() {
  window.clearTimeout(loginDeadlockTimer);
  loginDeadlockTimer = 0;
}

function loginFailureText(code) {
  return LOGIN_FAILURE_TEXT[String(code || "")] || GENERIC_LOGIN_FAILURE;
}

function setLoginStatus(text, state = "") {
  const status = $("login-status");
  if (!status) return;
  status.hidden = !text;
  status.textContent = text;
  if (state) status.dataset.state = state;
  else delete status.dataset.state;
}

function setLoginError(text) {
  const error = $("login-error");
  if (!error) return;
  error.hidden = !text;
  error.textContent = text;
}

function setLoginBusy(busy) {
  const submit = $("login-submit");
  if (submit) {
    submit.disabled = busy;
    submit.setAttribute("aria-busy", String(busy));
  }
  const form = $("login-form");
  if (form) form.setAttribute("aria-busy", String(busy));
}

/* 密码显示/隐藏：仅翻转 type，不改值、光标与 autocomplete；默认掩码 */
export function applyPasswordVisibility(visible) {
  const input = $("login-password");
  const toggle = $("toggle-password-visibility");
  if (!input || !toggle) return;
  const start = input.selectionStart;
  const end = input.selectionEnd;
  input.type = visible ? "text" : "password";
  try {
    input.setSelectionRange(start, end);
  } catch {
    /* 个别引擎在切换后不允许恢复光标，忽略即可 */
  }
  toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  toggle.setAttribute("aria-pressed", String(visible));
  [...toggle.children].forEach((node) => {
    const on = node.dataset?.eyeIcon === "on";
    node.hidden = visible ? !on : on;
  });
}

export function stopLoginPoll() {
  if (!loginPollTimer) return;
  window.clearInterval(loginPollTimer);
  loginPollTimer = 0;
}

const LOGIN_SUBMIT_LABEL = "登录";

/* 打开即复位（shell.js openLoginDialog 派发），关闭/终态同样复位 */
export function resetLoginDialog() {
  loginInFlight = false;
  stopLoginPoll();
  clearLoginDeadlockTimer();
  const submitReset = $("login-submit");
  if (submitReset) submitReset.textContent = LOGIN_SUBMIT_LABEL;
  setLoginBusy(false);
  setLoginStatus("", "");
  setLoginError("");
  $("login-password").value = "";
  applyPasswordVisibility(false);
  /* 已保存账号选择器：复位选择与手动模式标记；分区在刷新期间诚实可见 */
  loginSavedAccounts = [];
  loginSavedLoaded = false;
  loginSavedSelectedId = "";
  loginManualMode = false;
  renderLoginMode();
  setLoginSavedStatus("");
  syncLoginAutoConnectCheckbox(); /* 复位后未选账号：复选诚实未勾 */
  const savedSection = $("login-saved");
  if (savedSection) savedSection.hidden = true;
  const manualButton = $("login-manual-mode");
  if (manualButton) manualButton.setAttribute("aria-pressed", "false");
}

function startLoginPoll() {
  stopLoginPoll();
  loginPollTimer = window.setInterval(() => void pollLoginProgress(), LOGIN_POLL_MS);
}

function finishLoginSuccess(value) {
  loginInFlight = false;
  stopLoginPoll();
  clearLoginDeadlockTimer();
  setLoginBusy(false);
  setLoginStatus("", "");
  setLoginError("");
  $("login-dialog").close();
  $("login-password").value = "";
  /* REALRUN-1 P3-2 / 清单 #1：内部状态词不上学生屏——成功语义直接说人话 */
  toast("登录成功，已连接复旦课程平台", "ready");
  window.dispatchEvent(new Event("courselens:auth-refresh"));
  void loadAccounts();
}

function finishLoginFailure(message) {
  loginInFlight = false;
  stopLoginPoll();
  clearLoginDeadlockTimer();
  setLoginBusy(false);
  setLoginStatus("", "");
  setLoginError(message);
}

async function pollLoginProgress() {
  try {
    const value = await apiV3("authentication");
    /* 守卫看轮询器存活而非 loginInFlight：F8 硬超时回落 loginInFlight 后，
       终态仍要能自动收口（ready 关框 / degraded 报错） */
    if (!loginPollTimer) return;
    if (value?.state === "ready") {
      finishLoginSuccess(value);
      return;
    }
    if (value?.state === "degraded") {
      finishLoginFailure(loginFailureText(value?.code));
      return;
    }
    setLoginStatus(
      loginStageText(value) || LOGIN_STAGE_TEXT[value?.state] || "正在登录，请稍候。",
      "checking",
    );
  } catch {
    if (loginInFlight) setLoginStatus("暂时无法读取登录进度，仍在等待后端结果。", "checking");
  }
}

export const handleLoginSubmit = (event) => {
  event.preventDefault();
  if (loginInFlight) return; /* 防重复提交：进行中忽略再次提交 */
  const studentId = $("login-student-id").value.trim();
  const password = $("login-password").value;
  const remember = $("remember-account").checked;
  loginInFlight = true;
  setLoginBusy(true);
  setLoginError("");
  const submitLabel = $("login-submit");
  if (submitLabel) submitLabel.textContent = LOGIN_SUBMIT_LABEL;
  setLoginStatus("正在登录，请稍候。", "checking");
  armLoginDeadlockTimer();
  void (async () => {
    try {
      await postV3("authentication/actions", {
        action: "login",
        student_id: studentId,
        password,
        remember,
        operation_id: operationId("auth"),
      });
      if (!loginPollTimer && $("login-dialog").open) startLoginPoll(); /* D-20261009-10：F8 硬超时回落后的迟到受理也要启动轮询（轮询器自带 stop 守卫；对话框已被手动关闭则不重启） */
    } catch (error) {
      finishLoginFailure(loginFailureText(error?.code));
    }
  })();
};

// ---- 登录 dialog 已保存账号选择器：只消费 GET /api/v3/accounts 的非秘密元数据
// ---- （student_id + requires_rotation）。密码/token/cookie 任何形态都不进入 DOM、
// ---- 日志或快照；学号是既有的非秘密账户标签，可以选择性展示。
// ---- 语义：勾选过「保存在本机」的账号经既有 use-saved 动作一键免密登录；
// ---- 需要更新密码的账号可见但不可一键使用（引导到手动输入）；多个可用账号
// ---- 只列出不代选；「手动输入密码」是显式手动模式。删除/移除仍只在设置页。 ----

const LOGIN_SAVED_OPTION_STATE = Object.freeze({
  ok: "已保存密码",
  rotation: "需要更新密码",
});
const LOGIN_SAVED_STATUS_TEXT = Object.freeze({
  loading: "正在读取已保存账号…",
  empty: "本机暂无已保存账号；登录时勾选「保存在本机」即可下次免密。",
  error: "已保存账号暂时无法读取，可直接输入学号与密码登录。",
  rotation_selected: "该账号的已保存密码需要更新：请输入新密码登录一次。",
});

let loginSavedAccounts = [];
let loginSavedSelectedId = "";
let loginSavedLoaded = false;
/* 一张身份卡两个状态（条目1合并）：false=已保存身份卡（状态A），true=手动态（状态B） */
let loginManualMode = false;

/* 状态机渲染：有可用已保存账号且非手动态 → 身份卡（手动区收拢禁用）；
   其余（无可用账号/loading/读取失败/手动态）→ 手动区诚实可用 */
function renderLoginMode() {
  const section = $("login-saved");
  const manualFields = $("login-manual-fields");
  const manualActions = $("login-manual-actions");
  const backLink = $("login-back-saved");
  const useButton = $("login-use-saved");
  if (!section || !manualFields || !manualActions || !useButton) return;
  const hasUsable = loginSavedAccounts.some((item) => !item.requires_rotation);
  const identityMode = hasUsable && !loginManualMode;
  /* 手动态＝同卡展开：身份卡分区收起，旋转提示等状态随分区隐藏 */
  section.hidden = loginManualMode;
  manualFields.hidden = identityMode;
  manualActions.hidden = identityMode;
  /* 隐藏的必填输入必须同步禁用：HTML5 校验不检查 disabled 输入 */
  for (const id of ["login-student-id", "login-password", "remember-account"]) {
    const node = $(id);
    if (node) node.disabled = identityMode;
  }
  if (backLink) backLink.hidden = !loginManualMode;
  const manualButton = $("login-manual-mode");
  if (manualButton) {
    manualButton.hidden = loginManualMode || !hasUsable;
    manualButton.setAttribute("aria-pressed", String(loginManualMode));
  }
}

function setLoginSavedStatus(text) {
  const status = $("login-saved-status");
  if (!status) return;
  status.hidden = !text;
  status.textContent = text;
}

function loginSavedAccountState(account) {
  return account?.requires_rotation ? "rotation" : "ok";
}

function renderLoginSavedOptions() {
  const section = $("login-saved");
  const target = $("login-saved-options");
  const useButton = $("login-use-saved");
  if (!section || !target || !useButton) return;
  clear(target);
  loginSavedAccounts.forEach((account) => {
    const studentId = String(account.student_id || "");
    if (!studentId) return;
    const selected = loginSavedSelectedId === studentId;
    const option = document.createElement("button");
    option.type = "button";
    option.className = "login-saved-option";
    option.dataset.studentId = studentId;
    option.setAttribute("role", "radio");
    option.setAttribute("aria-pressed", String(selected));
    option.setAttribute("aria-checked", String(selected));
    const state = loginSavedAccountState(account);
    option.append(
      textElement("strong", studentId, "login-saved-option-id"),
      textElement("span", LOGIN_SAVED_OPTION_STATE[state], "login-saved-option-state"),
    );
    option.addEventListener("click", () => selectLoginSavedAccount(studentId));
    target.append(option);
  });
  const selectedAccount = loginSavedAccounts.find(
    (item) => String(item.student_id || "") === loginSavedSelectedId,
  );
  const selectedUsable = Boolean(selectedAccount) && !selectedAccount.requires_rotation;
  useButton.disabled = !selectedUsable || loginInFlight;
  $("login-manual-mode").setAttribute("aria-pressed", String(loginManualMode));
  syncLoginAutoConnectCheckbox();
}

function selectLoginSavedAccount(studentId) {
  if (loginInFlight) return;
  loginSavedSelectedId = loginSavedSelectedId === studentId ? "" : studentId;
  const account = loginSavedAccounts.find(
    (item) => String(item.student_id || "") === studentId,
  );
  if (account?.requires_rotation) {
    /* 需要更新密码：一键不可用，转到手动输入（学号是非秘密标签，可以预填） */
    loginManualMode = true;
    renderLoginMode();
    $("login-student-id").value = studentId;
    $("login-password").focus({ preventScroll: true });
    setLoginSavedStatus(LOGIN_SAVED_STATUS_TEXT.rotation_selected);
  } else {
    setLoginSavedStatus("");
  }
  renderLoginSavedOptions();
}

export async function refreshLoginSavedAccounts() {
  const section = $("login-saved");
  if (section) section.hidden = false; /* loading/empty/rotation/error 全部诚实可见 */
  setLoginSavedStatus(LOGIN_SAVED_STATUS_TEXT.loading);
  try {
    const value = await apiV3("accounts");
    loginSavedAccounts = Array.isArray(value?.accounts)
      ? value.accounts.filter((item) => item && typeof item === "object")
      : [];
    loginSavedLoaded = true;
    /* 恰一个可用账号 → 预选（重开客户端一次点击即可）；多个可用账号不代选；
       需要更新密码的账号不算可用，永不猜测。 */
    const usable = loginSavedAccounts.filter((item) => !item.requires_rotation);
    loginSavedSelectedId = usable.length === 1 ? String(usable[0].student_id || "") : "";
    setLoginSavedStatus(loginSavedAccounts.length ? "" : LOGIN_SAVED_STATUS_TEXT.empty);
  } catch {
    loginSavedAccounts = [];
    loginSavedLoaded = false;
    loginSavedSelectedId = "";
    setLoginSavedStatus(LOGIN_SAVED_STATUS_TEXT.error);
  }
  renderLoginSavedOptions();
  renderLoginMode();
}

export const handleLoginUseSaved = () => {
  if (loginInFlight) return;
  const account = loginSavedAccounts.find(
    (item) => String(item.student_id || "") === loginSavedSelectedId,
  );
  if (!account || account.requires_rotation) return; /* 空密码绝不发送 */
  loginInFlight = true;
  setLoginBusy(true);
  setLoginError("");
  setLoginStatus("正在使用已保存的密码登录，请稍候。", "checking");
  void (async () => {
    try {
      await postV3("authentication/actions", {
        action: "use-saved",
        student_id: loginSavedSelectedId,
        operation_id: operationId("auth"),
      });
      if (loginInFlight) startLoginPoll(); /* 与手动登录同一进度轮询与终态收敛 */
    } catch (error) {
      finishLoginFailure(loginFailureText(error?.code));
    }
  })();
};

export const handleLoginManualMode = () => {
  if (loginInFlight) return;
  loginManualMode = true;
  renderLoginMode();
  $("login-manual-mode").setAttribute("aria-pressed", "true");
  const prefilled = $("login-student-id").value.trim();
  (prefilled ? $("login-password") : $("login-student-id")).focus({ preventScroll: true });
};

/* 状态B → 状态A：返回身份卡（不改变任何请求语义） */
export const handleLoginBackSaved = () => {
  if (loginInFlight) return;
  loginManualMode = false;
  renderLoginMode();
  renderLoginSavedOptions();
};

// ---- 登录框自动登录选项（SMALL-POLISH-1①）：身份卡内复选与设置页开关
// ---- 同源（auto_connect.fudan），勾选即走既有 set-auto-connect 闭集动作，
// ---- 语义=「当前选中的可用账号」开启自动登录；快照/账号两条数据到齐即对齐。

function syncLoginAutoConnectCheckbox() {
  const checkbox = $("login-auto-connect");
  if (!checkbox) return;
  const fudan = isPlainRecord(autoConnectValue?.fudan) ? autoConnectValue.fudan : {};
  const selected = loginSavedAccounts.find(
    (item) => String(item.student_id || "") === loginSavedSelectedId,
  );
  const selectedUsable = Boolean(selected) && !selected.requires_rotation;
  checkbox.checked = Boolean(fudan.enabled)
    && selectedUsable
    && String(fudan.account_id || "") === loginSavedSelectedId;
}

/* 打开登录框即取设置快照：设置页未访问过也有初始真值（与 greeting 同一读取面） */
export async function refreshLoginAutoConnectPreference() {
  try {
    const value = await apiV3("settings");
    setAutoConnectValue(isPlainRecord(value?.auto_connect) ? value.auto_connect : null);
    renderAutoConnectPreference();
  } catch {
    /* 不可得：复选保持未勾，设置页开关仍由设置页加载驱动 */
  }
  syncLoginAutoConnectCheckbox();
}

export const handleLoginAutoConnect = (event) => {
  const checkbox = event.target;
  const accountId = loginSavedSelectedId;
  if (checkbox.checked && !accountId) {
    /* 前置条件本地拦截：无可用的已保存账号不发起请求 */
    checkbox.checked = false;
    setLoginSavedStatus(AUTO_CONNECT_ERROR_TEXT.auto_connect_account_required);
    return;
  }
  void (async () => {
    const failureCode = await postAutoConnectPreference(
      { fudan: { enabled: checkbox.checked, account_id: accountId } }, checkbox, "fudan",
    );
    /* 成功清空身份卡状态行；被拒镜像同一闭集文案，两入口看到同一真值 */
    setLoginSavedStatus(failureCode ? AUTO_CONNECT_ERROR_TEXT[failureCode] || AUTO_CONNECT_ERROR_GENERIC : "");
    syncLoginAutoConnectCheckbox(); /* 成功/被拒都从后端真值复位登录框复选 */
  })();
};
