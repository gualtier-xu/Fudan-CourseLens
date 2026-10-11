/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；自动连接与 GitHub 相位面。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { postV3 } from "../api.js";
import { $, clear, setBusy, toast } from "../ui.js";
import { isPlainRecord } from "./shared.js";

export let autoConnectValue = null;

const AUTO_CONNECT_STATUS_TEXT = Object.freeze({
  account_missing: "自动登录所选账号已不在本机保存列表中，请重新选择",
  rotation_required: "自动登录所选账号的已保存密码需要更新，更新后才能继续",
  grant_missing: "尚未完成 GitHub 授权，请先授权再开启自动连接",
});

const AUTO_CONNECT_RESUME_TEXT = Object.freeze({
  fudan_resume_started: "本次启动已尝试自动登录，进度以登录状态为准",
  fudan_resume_account_missing: "上次启动未自动登录：已保存账号不可用",
  fudan_resume_rotation_required: "上次启动未自动登录：已保存密码需要更新",
  fudan_resume_failed: "上次启动未自动登录：请手动登录一次",
  github_resume_verified: "本次启动已确认 GitHub 连接",
  github_resume_grant_missing: "上次启动未自动连接：尚未完成授权",
  github_resume_failed: "上次启动未自动连接：请重新授权",
  github_resume_unavailable: "上次启动未自动连接（网络暂不可用），可稍后重试",
});

export const AUTO_CONNECT_ERROR_TEXT = Object.freeze({
  auto_connect_account_required: "请先选择要自动登录的已保存账号。",
  auto_connect_account_missing: "该账号已不在本机保存列表中，请重新选择。",
  auto_connect_account_rotation_required: "该账号的已保存密码需要更新，更新后才能开启自动登录。",
  auto_connect_github_grant_missing: "请先完成 GitHub 授权，再开启自动连接。",
  auto_connect_request_invalid: "自动连接设置未被接受，请重试。",
});
export const AUTO_CONNECT_ERROR_GENERIC = "自动连接设置暂未保存，请重试。";

// ---- GitHub 连接阶段闭集：授权 → App 安装 → 专属仓库 → 加密通道（流程序重排） ----

export function setAutoConnectStatus(key, text, state = "") {
  const status = $(key === "fudan" ? "fudan-auto-connect-status" : "github-auto-connect-status");
  if (!status) return;
  status.hidden = !text;
  status.textContent = text;
  if (state) status.dataset.state = state;
  else delete status.dataset.state;
}

export function renderAutoConnectPreference() {
  const fudanSwitch = $("fudan-auto-connect");
  const accountSelect = $("fudan-auto-connect-account");
  const githubSwitch = $("github-auto-connect");
  if (!isPlainRecord(autoConnectValue)) {
    [fudanSwitch, accountSelect, githubSwitch].forEach((node) => { node.disabled = true; });
    setAutoConnectStatus("fudan", "自动连接状态待确认。");
    setAutoConnectStatus("github", "自动连接状态待确认。");
    return;
  }
  const fudan = isPlainRecord(autoConnectValue.fudan) ? autoConnectValue.fudan : {};
  const github = isPlainRecord(autoConnectValue.github) ? autoConnectValue.github : {};
  const lastResume = isPlainRecord(autoConnectValue.last_resume) ? autoConnectValue.last_resume : {};
  fudanSwitch.disabled = false;
  accountSelect.disabled = false;
  githubSwitch.disabled = false;
  fudanSwitch.checked = Boolean(fudan.enabled);
  if (String(fudan.account_id || "")) accountSelect.value = String(fudan.account_id);
  githubSwitch.checked = Boolean(github.enabled);
  const fudanStatus = AUTO_CONNECT_STATUS_TEXT[String(fudan.status || "")] || "";
  const fudanResume = AUTO_CONNECT_RESUME_TEXT[String(
    isPlainRecord(lastResume.fudan) ? lastResume.fudan.code : "",
  )] || "";
  setAutoConnectStatus(
    "fudan",
    [fudanResume, fudanStatus].filter(Boolean).join("；"),
    fudanStatus ? "action_required" : "",
  );
  const githubStatus = AUTO_CONNECT_STATUS_TEXT[String(github.status || "")] || "";
  const githubResume = AUTO_CONNECT_RESUME_TEXT[String(
    isPlainRecord(lastResume.github) ? lastResume.github.code : "",
  )] || "";
  setAutoConnectStatus(
    "github",
    [githubResume, githubStatus].filter(Boolean).join("；"),
    githubStatus ? "action_required" : "",
  );
}

/* 账号下拉由本机已保存账号驱动；需要更新密码的账号可见但不可选 */
export function renderAutoConnectAccounts(accounts) {
  const select = $("fudan-auto-connect-account");
  if (!select) return;
  clear(select);
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "选择账号";
  select.append(placeholder);
  (Array.isArray(accounts) ? accounts : []).forEach((account) => {
    const option = document.createElement("option");
    option.value = String(account.student_id || "");
    option.textContent = account.requires_rotation
      ? `${account.student_id}（需要更新密码）`
      : String(account.student_id || "");
    option.disabled = Boolean(account.requires_rotation);
    select.append(option);
  });
  renderAutoConnectPreference();
}

export async function postAutoConnectPreference(payload, checkbox, key) {
  setBusy(checkbox, true);
  checkbox.setAttribute("aria-busy", "true");
  try {
    const value = await postV3("settings/actions", { action: "set-auto-connect", ...payload });
    autoConnectValue = isPlainRecord(value) ? value : null;
    renderAutoConnectPreference();
    toast("自动连接设置已保存", "ready");
    return "";
  } catch (error) {
    renderAutoConnectPreference(); /* 从后端真值复位开关，不保留本地假状态 */
    const code = typeof error?.code === "string" ? error.code : "";
    setAutoConnectStatus(key, AUTO_CONNECT_ERROR_TEXT[code] || AUTO_CONNECT_ERROR_GENERIC, "error");
    return code || "error"; /* 闭集错误码供其他入口（登录框）镜像同一文案 */
  } finally {
    setBusy(checkbox, false);
    checkbox.removeAttribute("aria-busy");
    checkbox.focus({ preventScroll: true });
  }
}

// ---- 自动学习材料（cloud-automation.v3）：设置页不再有管理板块。
//     按课程开关在课程目录（study.js，快照唯一轮询者）；运行记录并入任务抽屉
//     （tasks-drawer.js 消费同一份后端快照）。 ----

/* ARCH-DEBT-1 缝合点：跨族赋值者（login-dialog 刷新偏好、门面 renderSettings）
   经此唯一入口写 autoConnectValue；ESM 导入绑定只读，setter 保持行为恒等。 */
export function setAutoConnectValue(value) {
  autoConnectValue = value;
}
