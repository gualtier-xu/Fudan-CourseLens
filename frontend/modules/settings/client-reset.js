/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；一键重置面。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { postV3 } from "../api.js";
import { $, operationId, setBusy, textElement, removeLocalKeys } from "../ui.js";
import { GITHUB_REPO_FULL_NAME_RE, isPlainRecord } from "./shared.js";
import { trustedRemoteAnchor } from "./remote-panel.js";
/* WAIT-UX-1：重置进行中的预计文案单源取自等待预期闭集（数据来源/重测日期见其头注） */
import { RESET_RUNNING_LOCAL, RESET_RUNNING_WITH_REPOS } from "../wait-expectations.js";

/* ---- 客户端重置（设置危险区单入口；冻结合同 2026-09-16） ----
   文案闭集：无 raw 异常、未知错误码不渲染；确认按钮仅在输入恰为「重置」时启用。
   两勾选项默认不勾：派生产物默认保留；GitHub 默认仅本机退出并清除记录。 */
const RESET_TEXT = Object.freeze({
  confirmWord: "重置",
  done: "重置已完成，应用已关闭。请手动重新打开 CourseLens。",
  blocked: "仍有进行中的任务或未完成的清理，重置未执行。请稍后重试。",
  confirmRequired: "请输入「重置」确认。",
  loginRequired: "重置前需要先登录复旦课程平台确认身份。请先登录，再回到这里执行重置。",
  githubFailure: "GitHub 仓库删除未完成，本次重置未执行；请确认连接后重试。",
  failure: "重置暂未完成，请稍后重试。",
  /* 不可删类降级（T3）：应用令牌任何层级都没有删仓权限（架构性），本地重置
     照常完成后，剩余仓库给受信手删链接与删后验证说明 */
  manualDeletion: "以下专属仓库需要你在 GitHub 手动删除（应用令牌没有删除仓库的权限）：点击链接进入仓库 Settings 页，在底部选择 Delete。删除后重新登录 GitHub 并执行一次诊断，连接状态会确认仓库已删除。",
});

const RESET_RESULT_SCHEMA = "courselens.client-reset-action-result.v1";

/* U5③：重置成功即清除浏览器侧持久化小数据——逐键闭集，与写入方一一对应
   （主题 shell.js、界面字号 settings/ui-font.js、课程排序与学期筛选 study.js）。
   清除动作统一走 ui.js 的 removeLocalKeys 通道：本模块带「永不触碰浏览器本地
   存储」隐私钉。 */
const RESET_BROWSER_KEYS = Object.freeze([
  "courselens.theme.v2",
  "courselens.ui-font.v1",
  "courselens.course-order.v1",
  "courselens.catalog-term.v1",
  /* PLAYER-OPT-1（20261008 夜）：player-core 本机偏好三键补入闭集——
     night15 R6 立项卡附录 N 预期「player-core 的 D3/U7① 键经此通道入重置
     闭集」但实际缺席，「客户端重置」后字幕样式/每课倍速/洞察开关残留本机。
     键名逐一对应 player-core.js 的 SUBTITLE_STYLE_KEY / RATE_MEMORY_KEY /
     INSIGHT_SWITCH_KEY。 */
  "courselens:subtitle-style",
  "courselens.playback-rate.v1",
  "courselens:insight",
  /* FIRST-LOGIN-UX-2（U1 继续学习）：store last-lecture 记忆键补入闭集——
     重置后着陆页不得残留上一身份的「继续学习」直达入口。键名对应 store.js
     rememberLastLecture/readLastLecture 写读的同一最小键。 */
  "courselens.last-lecture.v1",
]);

/* 手删回执闭集：只接受后端重置回执披露的 {repo, settings_url} 受信形态，
   链接形态不正则不过、仓库名形状不对即丢弃，绝不本地拼接发明 */
const GITHUB_REPO_SETTINGS_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+\/settings$/;
function validManualDeletions(result) {
  return (Array.isArray(result?.repos_manual_deletion) ? result.repos_manual_deletion : [])
    .filter((item) => isPlainRecord(item)
      && item.reason === "app_token_cannot_delete"
      && typeof item.repo === "string"
      && GITHUB_REPO_FULL_NAME_RE.test(item.repo)
      && (item.settings_url === "" || (
        typeof item.settings_url === "string"
        && GITHUB_REPO_SETTINGS_URL_RE.test(item.settings_url)
      )));
}

function resetDeletedSummary(result) {
  const parts = [];
  const deleted = result?.deleted || {};
  if (deleted.documents) parts.push(`文档 ${deleted.documents}`);
  if (deleted.subtitles) parts.push(`字幕 ${deleted.subtitles}`);
  if (deleted.courseware) parts.push(`课件 ${deleted.courseware}`);
  const repos = Array.isArray(result?.repos_deleted) ? result.repos_deleted.length : 0;
  if (repos) parts.push(`仓库 ${repos} 个`);
  const manual = (Array.isArray(result?.repos_manual_deletion) ? result.repos_manual_deletion : []).length;
  if (manual) parts.push(`待手动删除仓库 ${manual} 个`);
  if (result?.preserved_derived) parts.push("派生产物已保留");
  return parts;
}

function renderResetError(error) {
  const dialogError = $("client-reset-dialog-error");
  if (!dialogError) return;
  const code = String(error?.code || "");
  dialogError.textContent = code === "fudan_login_required" || error?.status === 401 ? RESET_TEXT.loginRequired
    : code === "reset_blocked" ? RESET_TEXT.blocked
    : code === "reset_confirm_required" ? RESET_TEXT.confirmRequired
    : error?.status === 502 || code.startsWith("github") ? RESET_TEXT.githubFailure
    : RESET_TEXT.failure;
  dialogError.hidden = false;
}

export function installClientReset() {
  const dialog = $("client-reset-dialog");
  const input = $("client-reset-confirm-input");
  const confirmButton = $("client-reset-confirm");
  const resultLine = $("client-reset-result");
  const pageError = $("client-reset-error");
  if (!dialog || !input || !confirmButton) return () => {};
  const dialogStatus = $("client-reset-dialog-status");
  const dialogError = $("client-reset-dialog-error");
  /* 打开即恢复默认：两勾选项不勾、确认输入清空（checkbox.checked 复位） */
  const resetDialogState = () => {
    input.value = "";
    confirmButton.disabled = true;
    $("client-reset-delete-derived").checked = false;
    $("client-reset-delete-repos").checked = false;
    if (dialogStatus) dialogStatus.hidden = true;
    if (dialogError) dialogError.hidden = true;
  };
  const handleDialogClose = () => resetDialogState();
  const handleOpen = () => {
    resetDialogState();
    dialog.showModal();
  };
  const handleCancel = () => dialog.close();
  const handleInput = () => {
    confirmButton.disabled = input.value !== RESET_TEXT.confirmWord;
  };
  const handleConfirm = async () => {
    if (confirmButton.disabled) return;
    setBusy(confirmButton, true);
    if (dialogStatus) {
      /* WAIT-UX-1 缺 ETA K1+：按是否勾选删仓给两档诚实预计（本机段实测秒级；
         含 GitHub 网络段给宽区间，绝不写死数），替换原先无预期的静态「正在重置…」。 */
      dialogStatus.hidden = false;
      dialogStatus.textContent = $("client-reset-delete-repos").checked
        ? RESET_RUNNING_WITH_REPOS
        : RESET_RUNNING_LOCAL;
    }
    if (dialogError) dialogError.hidden = true;
    try {
      const receipt = await postV3("client-reset/actions", {
        action: "reset",
        operation_id: operationId("client-reset"),
        confirm_typed: input.value,
        delete_derived: $("client-reset-delete-derived").checked,
        delete_github_repos: $("client-reset-delete-repos").checked,
      });
      dialog.close();
      if (resultLine) {
        const valid = receipt?.schema === RESET_RESULT_SCHEMA && receipt?.status === "accepted";
        if (valid) removeLocalKeys(RESET_BROWSER_KEYS);
        resultLine.hidden = false;
        if (valid) {
          const parts = resetDeletedSummary(receipt.result);
          resultLine.textContent = parts.length
            ? `${RESET_TEXT.done}（${parts.join("，")}）`
            : RESET_TEXT.done;
          /* 不可删类降级：剩余仓库给受信手删链接（闭集形态）+ 删后验证说明。
             仓库名单绝不回显到界面（既有隐私钉）：链接直达对应 Settings 页，
             标签只用序号 */
          const manualRepos = validManualDeletions(receipt.result);
          if (manualRepos.length) {
            resultLine.append(textElement("span", RESET_TEXT.manualDeletion));
            manualRepos.forEach((item, index) => {
              resultLine.append(item.settings_url
                ? trustedRemoteAnchor({ href: item.settings_url, label: `删除专属仓库 ${index + 1}` })
                : textElement("span", `专属仓库 ${index + 1} 需手动删除（暂无受信链接）`));
            });
          }
          if (pageError) pageError.hidden = true;
        } else {
          resultLine.textContent = RESET_TEXT.failure;
        }
      }
    } catch (error) {
      renderResetError(error);
    } finally {
      confirmButton.disabled = input.value !== RESET_TEXT.confirmWord; /* 重放 typed 门 */
      confirmButton.removeAttribute("aria-busy");
      if (dialogStatus) dialogStatus.hidden = true;
    }
  };
  $("client-reset-open").addEventListener("click", handleOpen);
  $("client-reset-cancel").addEventListener("click", handleCancel);
  confirmButton.addEventListener("click", handleConfirm);
  input.addEventListener("input", handleInput);
  dialog.addEventListener("close", handleDialogClose);
  return () => {
    $("client-reset-open").removeEventListener("click", handleOpen);
    $("client-reset-cancel").removeEventListener("click", handleCancel);
    confirmButton.removeEventListener("click", handleConfirm);
    input.removeEventListener("input", handleInput);
    dialog.removeEventListener("close", handleDialogClose);
  };
}
