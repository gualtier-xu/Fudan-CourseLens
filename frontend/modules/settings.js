/* settings.js —— 设置页组合根（ARCH-DEBT-1 拆分后门面）。
   各词干族已按 R3-27/R7-#27 设计稿拆至 settings/ 子目录模块（纯移动、
   行为零变化）：shared 通用小工具 / update-panel 隐私与更新 / ai-usage
   AI 密钥用量 / auto-connect 自动连接 / login-dialog 登录对话框 /
   remote-panel 远程面板 / client-reset 一键重置。本门面保留账户核心
   （loadAccounts/authenticate/renderAccountSession）、页面渲染枢纽
   （renderSettings/loadSettings）与安装器（installSettings），并 re-export
   原公共导出面（app.js 与行为测试的具名导入零变化）。 */

import { apiV3, postV3 } from "./api.js";
import { applyThemePreference, readThemePreference } from "./shell.js";
import { $, clear, evidenceDetails, evidenceText, operationId, setBusy, textElement, toast } from "./ui.js";
/* 组合根（app.js 保持单一入口不扩）：顶栏更新小组件与数据管理页的安装器由
   settings 安装（台账 §4 冻结的 10 条路径内唯一可行组合点）。 */
import { installUpdateWidget } from "./update-widget.js";
import { installCourseData } from "./course-data.js";
import { armDeleteConfirmation, isPlainRecord, proxyUrlParseable } from "./settings/shared.js";
import { renderPrivacyDoc, renderUpdate, safeUpdateDiagnostics, updateAction, updateValue } from "./settings/update-panel.js";
import { ensureAiUsageSiblingLine, refreshDeepseekBalance, renderDeepseekSaveState, setDeepseekSaveState, syncDeepseekBudgetPresets, taskUsageMonthText } from "./settings/ai-usage.js";
import { AUTO_CONNECT_ERROR_TEXT, postAutoConnectPreference, renderAutoConnectAccounts, renderAutoConnectPreference, setAutoConnectStatus, setAutoConnectValue } from "./settings/auto-connect.js";
import { installWaitActive, installWaitAdvanced, installWaitPollTick, loadRemote, remoteAction, remoteComponentMissingSecrets, remoteSnapshotValue, renderCloudRevokeRow, renderRemote, runCloudRevoke, runMailboxReconcile, teardownRemoteActionState, tightenWaitActive, tightenWaitAdvanced, tightenWaitPollTick } from "./settings/remote-panel.js";
import { applyPasswordVisibility, handleLoginAutoConnect, handleLoginBackSaved, handleLoginManualMode, handleLoginSubmit, handleLoginUseSaved, refreshLoginAutoConnectPreference, refreshLoginSavedAccounts, resetLoginDialog, stopLoginPoll } from "./settings/login-dialog.js";
import { installClientReset } from "./settings/client-reset.js";
/* A11Y-IMPL-4：界面字号偏好写入方居 modules/ 顶层（settings 家族零浏览器存储
   隐私钉，见 ui-font.js 头注）；settings 只做装配接线。 */
import { applyUiFontAtStartup, applyUiFontPreference, readUiFontPreference } from "./ui-font.js";
import { loadDataMap } from "./settings/data-map.js";
import { installDataMigration } from "./settings/data-migration.js";
export { ownDataValue, safeUpdateDiagnostics, updateErrorGuidance, updateRecoveryGuidance } from "./settings/update-panel.js";
export { installClientReset } from "./settings/client-reset.js";

let settingsValue = null;

let currentStore = null;

const NETWORK_MODE_LABELS = Object.freeze({
  auto: "自动（复旦服务直连）",
  direct: "关闭代理（全部直连）",
  manual: "手动代理（所有服务）",
});

const NETWORK_SERVICE_LABELS = Object.freeze({
  icourse: "课程平台",
  github: "GitHub",
  deepseek: "DeepSeek",
});

export async function loadAccounts() {
  const value = await apiV3("accounts");
  const target = $("account-list");
  clear(target);
  (value.accounts || []).forEach((account) => {
    /* F13（N5FE-P3）：旧版本可能留下缺学号的异常记录（如字面 "undefined"）。
       渲染守卫：不再把脏值当学号展示，一键使用禁用；保留移除作为清理出口。 */
    const studentId = String(account.student_id ?? "").trim();
    const dirtyRecord = !studentId || studentId === "undefined" || studentId === "null";
    const row = document.createElement("div");
    row.className = "account-row";
    if (dirtyRecord) {
      row.title = "该记录缺少有效学号，可能是旧版本残留，可放心移除。";
      row.append(
        textElement("strong", "记录异常（缺少学号）"),
        textElement("span", "旧版本残留，可移除"),
      );
    } else {
      row.append(textElement("strong", studentId), textElement("span", account.requires_rotation ? "需要更新密码" : "已保存在本机"));
    }
    const use = document.createElement("button");
    use.type = "button";
    use.textContent = "使用";
    use.disabled = dirtyRecord || Boolean(account.requires_rotation);
    use.addEventListener("click", () => authenticate({ action: "use-saved", student_id: account.student_id }));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "移除";
    remove.addEventListener("click", async () => {
      armDeleteConfirmation(remove, "确认移除该账户？", async () => {
        setBusy(remove, true);
        try {
          await postV3("accounts/actions", { action: "delete", student_id: dirtyRecord ? studentId : account.student_id });
          toast("已保存账户已移除", "ready");
          await loadAccounts();
        } catch (error) {
          toast(error.message, "error");
          setBusy(remove, false);
        }
      });
    });
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.append(use, remove);
    row.append(actions);
    target.append(row);
  });
  if (!(value.accounts || []).length) {
    const empty = textElement(
      "p",
      currentStore?.auth?.state === "ready" ? "当前会话已验证，但没有保存在本机的账号。" : "暂无已保存账户",
      "empty-state account-empty-state",
    );
    target.append(empty);
  }
  renderAutoConnectAccounts(value.accounts);
  renderDeepseekSaveState(value.deepseek);
  $("delete-deepseek").disabled = !value.deepseek?.configured;
}

function renderAccountSession(auth) {
  const details = evidenceDetails(auth);
  $("account-session-state").textContent = `${details.title}。${details.impact}`;
  const pill = $("fudan-connection-state");
  pill.textContent = details.title;
  pill.dataset.state = String(auth?.state || "unknown");
  const ready = auth?.state === "ready";
  const checking = auth?.state === "checking";
  $("open-login").textContent = ready ? "切换或重新认证" : checking ? "验证中" : "登录";
  /* F8（N5FE-P1）：验证中不再禁用入口——后端卡在 checking 时它是唯一逃生门；
     「验证中」字样保留作诚实状态，点击可重开登录框重试 */
  $("open-login").disabled = false;
  $("logout-button").disabled = !(auth?.actions || []).includes("logout");
  const empty = $("account-list").querySelector(".account-empty-state");
  if (empty) empty.textContent = ready ? "当前会话已验证，但没有保存在本机的账号。" : "暂无已保存账户";
}

async function authenticate(body) {
  try {
    const value = await postV3("authentication/actions", { ...body, operation_id: operationId("auth") });
    $("login-dialog").close();
    $("login-password").value = "";
    toast(`认证状态：${value.authentication?.state || "checking"}`, "checking");
    window.dispatchEvent(new Event("courselens:auth-refresh"));
    await loadAccounts();
  } catch (error) {
    toast(error.message, "error");
  }
}

// ---- F7 本地统计开启（隐私节证据行 adjacent 动作） ----

/* F7（化身走查 20261008）：本地统计开启动作——analytics/actions 既有 enable
   通道（operation_id 幂等），成功后整页证据行原路重读（同一 loadSettings，
   行内按钮随 enabled 闭集退场）；失败诚实 toast 且按钮复位可重试。 */
async function enableLocalAnalytics(button) {
  setBusy(button, true);
  try {
    await postV3("analytics/actions", { action: "enable", operation_id: operationId("analytics") });
    toast("已开启本地学习统计；数据只保存在这台电脑上，随时可以关闭。", "ready");
    await loadSettings();
  } catch (error) {
    toast(error?.message || "开启没有成功，请稍后再试一次。", "error");
    setBusy(button, false);
  }
}

// ---- 登录 dialog：防重复提交 + 忙态/阶段反馈 + 闭集失败文案 + 密码可见性 ----

function renderSettings(value) {
  settingsValue = value;
  $("settings-evidence").textContent = evidenceText(value);
  $("network-mode").value = value.network?.mode || "auto";
  $("proxy-url-row").hidden = $("network-mode").value !== "manual";
  $("proxy-url").value = value.network?.proxy_url || "http://127.0.0.1:6268";
  /* 隐私区三行已退役（CLOUD-TOGGLE-1 U3）：隐私节只留透明说明 + 证据行，
     不再回写复选框；evidenceText 仍合并凭据与本地统计两源。
     UIAUDIT-1 F16：证据行补主语——裸状态「已连接 · 需要完成操作」让学生
     无从判断指哪个连接，逐段冠明来源。
     F7（化身走查 20261008）：证据行由纯文本改为轻拼装——本地统计未开启时
     行内附具名动作「开启本地学习统计」（analytics/actions 既有 enable 通道，
     operation_id 幂等），学生不再面对「需要完成操作」却无处可点；开启后
     行内不出现按钮（自动化缺省：无开关面，只有状态行+按需动作）。 */
  const credentialEvidence = evidenceText(value.credentials);
  const analyticsValue = isPlainRecord(value.analytics) ? value.analytics : null;
  const analyticsEvidence = evidenceText(analyticsValue);
  const privacyNode = $("privacy-evidence");
  clear(privacyNode);
  const privacyLine = document.createElement("p");
  privacyLine.textContent = credentialEvidence === analyticsEvidence
    ? `复旦会话：${credentialEvidence}`
    : `复旦会话：${credentialEvidence} · 本地统计：${analyticsEvidence}`;
  privacyNode.append(privacyLine);
  const analyticsActions = Array.isArray(analyticsValue?.actions) ? analyticsValue.actions : [];
  if (analyticsActions.includes("enable-analytics")) {
    const analyticsEnable = document.createElement("button");
    analyticsEnable.type = "button";
    analyticsEnable.className = "text-button";
    analyticsEnable.textContent = "开启本地学习统计";
    analyticsEnable.addEventListener("click", () => { void enableLocalAnalytics(analyticsEnable); });
    privacyNode.append(analyticsEnable);
  }
  const remote = value.remote?.overall || value.remote;
  $("security-evidence").textContent = evidenceText(remote);
  /* 后台检查偏好（冻结键 update_background_checks，缺省=开） */
  $("update-background-checks").checked = value.update_background_checks !== false;
  /* 媒体流系统代理偏好（MEDIA-VPN-1，缺省=关） */
  $("media-stream-proxy").checked = value.media_stream_proxy?.enabled === true;
  setAutoConnectValue(isPlainRecord(value.auto_connect) ? value.auto_connect : null);
  renderAutoConnectPreference();
  /* AS3：解释日条数闸整链退役（解释/解答永无条数门）。
     ⑨（C⑧ 修订+AS3 不限档）：日 token 总量档位卡回显 + Z2 本月用量（本地聚合） */
  syncDeepseekBudgetPresets(value.max_deepseek_tokens);
  {
    const tokens = Number(value.max_deepseek_tokens);
    const usage = value.ai_usage_month;
    const usageNode = $("ai-usage-month");
    if (usageNode) {
      const questions = Number(usage?.questions);
      const summaries = Number(usage?.summaries);
      /* U⑩：裸 token 数字收进用量视图（当前上限随用量一行呈现；0=不限档） */
      const limitText = tokens > 0
        ? `当前上限 ${Math.round(tokens / 10000)} 万 tokens/日`
        : "每日上限不限";
      usageNode.textContent = Number.isFinite(questions) && Number.isFinite(summaries)
        ? `本月 AI 用量：解释 ${questions} 次 · 总结 ${summaries} 讲 · ${limitText}（本地统计，仅供参考）`
        : "";
    }
  }
  /* AS6 消耗透镜：本机累计行（同读取面 settings GET，零额外外联） */
  {
    const node = ensureAiUsageSiblingLine("task-usage-month");
    if (node) node.textContent = taskUsageMonthText(value.task_usage_month);
  }
}

async function loadSettings() {
  try {
    renderSettings(await apiV3("settings"));
    await loadAccounts();
  } catch (error) {
    $("settings-evidence").textContent = error.message;
  }
  /* AS6：余额单独一次读取（打开设置页至多一次；后端另有缓存 TTL，无 key 零外联）。
     不 await——余额慢/失败绝不拖住设置页主数据。 */
  void refreshDeepseekBalance();
}

// ---- 远程连接动作（自任务区迁入：请求语义不变，入口归设置） ----

export async function installSettings(store) {
  currentStore = store;
  const loadSettingsPage = () => {
    void loadSettings();
    void loadRemote();
    /* DATAMAP-P1：数据地图随设置页展示即读（本机只读聚合，零外联；
       独立读取不 await——地图慢/失败绝不拖住设置页主数据）。 */
    void loadDataMap();
  };
  const handlePageRequest = (event) => {
    if (event.detail === "settings") loadSettingsPage();
  };
  const handleLogoutRequest = () => authenticate({ action: "logout" });
  /* 安装等待期窗口聚焦立即重探（单元A）：学生从 GitHub 安装页切回即最快侦测；
     收紧等待期对称同享（单元D） */
  const handleWindowFocusForInstallWait = () => {
    if (installWaitActive() && !installWaitAdvanced) void installWaitPollTick();
    if (tightenWaitActive() && !tightenWaitAdvanced) void tightenWaitPollTick();
  };
  /* 连接及时性（CLIENT-STATE，用户实证痛点）：连接卡此前只在进入设置页或
     动作后刷新——后台状态翻转（自动连接推进、云端初始化收口、修复完成）
     要等人为重新进页才上屏。现订阅任务抽屉应用级 SSE 广播的
     courselens:remote-connection-changed，设置页正在前台即刷新连接卡；
     不在前台不取数（零多余外联，进页路径照常刷新）。 */
  const handleRemoteConnectionChanged = () => {
    const page = $("settings-page");
    if (!page || page.hidden) return;
    void loadRemote();
  };
  window.addEventListener("courselens:page", handlePageRequest);
  window.addEventListener("courselens:logout", handleLogoutRequest);
  window.addEventListener("focus", handleWindowFocusForInstallWait);
  window.addEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
  const applyProxyDisclosure = () => {
    $("proxy-url-row").hidden = $("network-mode").value !== "manual";
  };
  $("network-mode").addEventListener("change", applyProxyDisclosure);
  /* 指针与键盘激活都把焦点留在被点的导航按钮上（激活的 nav/tab 本身就是紧凑键盘
     目标）；分组大容器/标题不再成为编程焦点目标（容器级大环根因）。键盘用户仍能
     从导航按钮用 Tab 进入该分组内的第一个控件。 */
  const handleNavClick = (event) => {
    const button = event.target.closest?.("[data-settings-target]");
    if (!button) return;
    const group = document.getElementById(button.dataset.settingsTarget);
    if (!group) return;
    const nav = document.querySelector(".settings-nav");
    nav?.querySelectorAll("button").forEach((item) => {
      if (item === button) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
    group.scrollIntoView({ block: "start" });
  };
  document.querySelector(".settings-nav").addEventListener("click", handleNavClick);
  /* 初始态高亮第一组：页面加载即展示顶部分组，无 aria-current 时导航没有任何激活呈现 */
  document.querySelector(".settings-nav button[data-settings-target]")?.setAttribute("aria-current", "true");
  /* SETTINGS-UX-1 A1（化身走查 F1）scrollspy：滚动时 aria-current 跟随视口顶部
     分组，消除「点击导航后高亮与滚动内容脱节」。点击导航的即时高亮保持现状
     （scrollIntoView 瞬时跳转落定后 observer 收敛到同一组）；页底时兜底高亮
     末组（末组矮，可能永远进不了顶部激活带）。零新增开关、零行为变化。 */
  const spyNav = document.querySelector(".settings-nav");
  const spyButtons = new Map(
    [...document.querySelectorAll(".settings-content .settings-group")].map((group) => [
      group.id,
      spyNav?.querySelector(`[data-settings-target="${group.id}"]`) || null,
    ]),
  );
  let spyCurrentId = "";
  let spyAtBottom = false;
  const setCurrentBySpy = (id) => {
    if (!id || id === spyCurrentId || !spyButtons.has(id)) return;
    spyCurrentId = id;
    spyButtons.forEach((button, groupId) => {
      if (!button) return;
      if (groupId === id) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    });
  };
  const spyObserver = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => {
      if (spyAtBottom) return; /* 页底兜底已定末组；交叠带结论不覆盖（scroll 先于 IO 回调记账） */
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) setCurrentBySpy(visible[0].target.id);
    }, { rootMargin: "-72px 0px -66% 0px" })
    : null;
  if (spyObserver) spyButtons.forEach((_, groupId) => {
    const group = document.getElementById(groupId);
    if (group) spyObserver.observe(group);
  });
  /* 页底兜底挂在真实滚动容器上：layout 的滚动面是 .page（overflow:auto），
     window 永不滚动——末组矮，页底时可能永远进不了顶部激活带。 */
  const spyPage = document.getElementById("settings-page");
  const handleSpyScroll = () => {
    if (!spyPage) return;
    spyAtBottom = spyPage.scrollTop + spyPage.clientHeight >= spyPage.scrollHeight - 2;
    if (spyAtBottom) {
      const lastId = [...spyButtons.keys()].pop();
      if (lastId) setCurrentBySpy(lastId);
    }
  };
  if (spyObserver && spyPage) spyPage.addEventListener("scroll", handleSpyScroll, { passive: true });
  const handleFudanAutoConnect = (event) => {
    const checkbox = event.target;
    const accountId = String($("fudan-auto-connect-account").value || "").trim();
    if (checkbox.checked && !accountId) {
      /* 前置条件本地拦截：未选账号不发起请求 */
      checkbox.checked = false;
      setAutoConnectStatus("fudan", AUTO_CONNECT_ERROR_TEXT.auto_connect_account_required, "error");
      return;
    }
    void postAutoConnectPreference(
      { fudan: { enabled: checkbox.checked, account_id: accountId } }, checkbox, "fudan",
    );
  };
  const handleGithubAutoConnect = (event) => {
    const checkbox = event.target;
    void postAutoConnectPreference({ github: { enabled: checkbox.checked } }, checkbox, "github");
  };
  const handleAutoConnectAccountChange = () => renderAutoConnectPreference();
  $("fudan-auto-connect").addEventListener("change", handleFudanAutoConnect);
  $("github-auto-connect").addEventListener("change", handleGithubAutoConnect);
  $("fudan-auto-connect-account").addEventListener("change", handleAutoConnectAccountChange);
  /* P56-U1：三态主题控件（自动/浅色/深色），直接消费 shell 的偏好闭集；
     「自动」档按本机时刻解析，选择即持久（v2）。 */
  const handleThemeModeChange = (event) => applyThemePreference(event.target.value);
  $("settings-theme-mode").addEventListener("change", handleThemeModeChange);
  $("settings-theme-mode").value = readThemePreference();
  /* A11Y-IMPL-4（D14 P1-1 产品步）：界面字号三态（默认/大/特大），直接消费
     ui-font 偏好闭集；启动即应用（装配期落 html[data-ui-font]，rem 地基使
     根字号全站生效），选择即持久。低视力学生的真决策项，例外面成档。 */
  applyUiFontAtStartup();
  const handleUiFontChange = (event) => applyUiFontPreference(event.target.value);
  $("settings-ui-font").addEventListener("change", handleUiFontChange);
  $("settings-ui-font").value = readUiFontPreference();
  $("check-update").addEventListener("click", () => updateAction("check", $("check-update")));
  $("download-update").addEventListener("click", () => updateAction("download", $("download-update"), true));
  $("install-update").addEventListener("click", () => updateAction("install", $("install-update"), true));
  /* 后台检查偏好开关（update_background_checks）：与 auto-connect 同走 settings
     动作闭集，重读快照渲染，无本地持久化 */
  const handleUpdateBackgroundChecks = async (event) => {
    const checkbox = event.target;
    setBusy(checkbox, true);
    try {
      await postV3("settings/actions", { action: "set-update-background-checks", enabled: checkbox.checked });
      await loadSettings();
      toast("更新设置已保存", "ready");
    } catch (error) {
      toast(error.message, "error");
      await loadSettings().catch(() => {});
    } finally {
      checkbox.disabled = false;
      checkbox.removeAttribute("aria-busy");
      checkbox.focus({ preventScroll: true });
    }
  };
  $("update-background-checks").addEventListener("change", handleUpdateBackgroundChecks);
  /* 媒体流系统代理开关（MEDIA-VPN-1）：与后台检查偏好同构——闭集设置动作、
     重读快照渲染、无本地持久化 */
  const handleMediaStreamProxy = async (event) => {
    const checkbox = event.target;
    setBusy(checkbox, true);
    try {
      await postV3("settings/actions", { action: "set-media-stream-proxy", enabled: checkbox.checked });
      await loadSettings();
      toast("媒体流设置已保存", "ready");
    } catch (error) {
      toast(error.message, "error");
      await loadSettings().catch(() => {});
    } finally {
      checkbox.disabled = false;
      checkbox.removeAttribute("aria-busy");
      checkbox.focus({ preventScroll: true });
    }
  };
  $("media-stream-proxy").addEventListener("change", handleMediaStreamProxy);
  const openLogin = () => window.dispatchEvent(new CustomEvent("courselens:open-login", {
    detail: store.auth?.state === "ready" ? "reauth" : "login",
  }));
  $("open-login").addEventListener("click", openLogin);
  const handlePasswordToggle = () => applyPasswordVisibility($("login-password").type === "password");
  const handleLoginDialogOpen = () => {
    resetLoginDialog();
    /* 每次打开都重新读取已保存账号：保存/删除发生在设置页或上次登录，打开即最新 */
    void refreshLoginSavedAccounts();
    /* 自动登录复选初始态对齐 auto_connect 真值（设置页未访问过也成立） */
    void refreshLoginAutoConnectPreference();
  };
  $("toggle-password-visibility").addEventListener("click", handlePasswordToggle);
  $("login-use-saved").addEventListener("click", handleLoginUseSaved);
  $("login-auto-connect").addEventListener("change", handleLoginAutoConnect);
  $("login-manual-mode").addEventListener("click", handleLoginManualMode);
  $("login-back-saved").addEventListener("click", handleLoginBackSaved);
  window.addEventListener("courselens:login-dialog-open", handleLoginDialogOpen);
  $("login-dialog").addEventListener("close", () => resetLoginDialog());
  $("login-form").addEventListener("submit", handleLoginSubmit);
  $("logout-button").addEventListener("click", () => authenticate({ action: "logout" }));
  /* U⑩：点档位卡即保存（一次点击一次生效，不再有「选了忘保存」的落差）。
     保存链路中三张卡一起禁用，成功后按后端回读值同步选中态。 */
  const budgetContainer = $("deepseek-budget-presets");
  budgetContainer?.addEventListener("click", async (event) => {
    const button = event.target?.closest?.(".budget-preset");
    if (!button || button.disabled) return;
    const state = $("max-deepseek-tokens-state");
    const tokens = Number(button.dataset.budget);
    const cards = [...(budgetContainer.querySelectorAll(".budget-preset") || [])];
    cards.forEach((node) => { node.disabled = true; });
    try {
      const value = await postV3("settings/actions", {
        action: "set-max-deepseek-tokens",
        tokens,
        operation_id: operationId("max-deepseek-tokens"),
      });
      syncDeepseekBudgetPresets(value.max_deepseek_tokens);
      if (state) {
        state.hidden = false;
        const saved = Number(value.max_deepseek_tokens);
        state.textContent = saved > 0
          ? `已保存：每天最多 ${Math.round(saved / 10000)} 万 tokens。`
          : "已保存：不限制每日用量。";
      }
    } catch (error) {
      if (state) {
        state.hidden = false;
        state.textContent = "保存未完成：上限须是 10 万-100 万之间的整十万档位。";
      }
    } finally {
      cards.forEach((node) => { node.disabled = false; });
    }
  });
  $("save-deepseek").addEventListener("click", async () => {
    const button = $("save-deepseek");
    const keyInput = $("deepseek-key");
    /* C1：保存前 trim——粘贴带首尾空白的 key 原样入库会静默失效 */
    const apiKey = String(keyInput.value || "").trim();
    if (!apiKey) {
      setDeepseekSaveState("请输入 API Key"); /* 空 key：inline 提示且不发请求 */
      return;
    }
    setBusy(button, true);
    const remember = $("remember-deepseek").checked;
    let saved;
    try {
      saved = await postV3("secrets/actions", { action: "set-deepseek", api_key: apiKey, remember });
    } catch (error) {
      setDeepseekSaveState("保存失败，请稍后重试", "error");
      toast(error.message, "error");
      setBusy(button, false);
      return;
    }
    keyInput.value = "";
    /* P58 反馈分岔：按「保存在本机」勾选如实播报存放去向；不声称当前会话
       一定使用本机 key（会话 key 可覆盖，口径同 :401 状态行） */
    toast(remember ? "DeepSeek Key 已更新，并已保存到本机" : "DeepSeek Key 已更新，仅本次启动有效", "ready");
    renderDeepseekSaveState(saved);
    try {
      await loadAccounts();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("delete-deepseek").addEventListener("click", async () => {
    const button = $("delete-deepseek");
    armDeleteConfirmation(button, "确认移除 DeepSeek Key？", async () => {
      setBusy(button, true);
      try {
        await postV3("secrets/actions", { action: "delete-deepseek" });
        toast("DeepSeek Key 已移除", "ready");
        setDeepseekSaveState("未保存在本机");
        await loadAccounts();
      } catch (error) {
        toast(error.message, "error");
        setBusy(button, false);
      }
    });
  });
  $("save-network").addEventListener("click", async () => {
    const button = $("save-network");
    const mode = $("network-mode").value;
    const proxyValue = $("proxy-url").value.trim();
    /* D8：手动代理必填且要能解析（http/https + 主机非空）；带值时同样校验，
       不再静默补 http://。校验失败内联人话指路、零 POST。 */
    if ((mode === "manual" && !proxyValue) || (proxyValue && !proxyUrlParseable(proxyValue))) {
      const evidence = $("network-evidence");
      clear(evidence);
      evidence.append(textElement("span", "代理地址要写成 http://主机:端口 的样子（比如 http://127.0.0.1:7890），改好后重新保存"));
      toast("代理地址还没写对，这次没有保存", "error");
      return;
    }
    setBusy(button, true);
    try {
      const value = await postV3("settings/actions", { action: "update-network", mode, proxy_url: proxyValue });
      const evidence = $("network-evidence");
      clear(evidence);
      const services = value?.routes && typeof value.routes === "object" ? Object.keys(value.routes) : [];
      evidence.append(textElement("span", `网络设置已保存 · 模式：${NETWORK_MODE_LABELS[mode] || "未知"}${services.length ? ` · 已确认 ${services.length} 条路由` : ""}`));
      toast("网络设置已保存", "ready");
      /* 网络设置变化即路由代际事件：立即复用既有 auth 复检确认校园连接（无新增轮询），
         连接卡在后端 generation 升高的快照到达时显示“网络设置已变化”内联状态。 */
      window.dispatchEvent(new Event("courselens:auth-refresh"));
      await loadSettings();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("diagnose-network").addEventListener("click", async () => {
    const button = $("diagnose-network");
    setBusy(button, true);
    try {
      const value = await postV3("settings/actions", { action: "diagnose-network" });
      const evidence = $("network-evidence");
      clear(evidence);
      Object.entries(value.services || {}).forEach(([name, item]) => {
        const label = NETWORK_SERVICE_LABELS[name] || name;
        evidence.append(textElement("span", `${label}：${item.healthy ? "可用" : "暂不可用"}`));
      });
      toast("网络诊断已完成", "ready");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("remote-action-error").setAttribute("role", "alert"); /* 动作失败内联区：announced live region */
  const reconcileDialog = $("mailbox-reconcile-dialog");
  $("remote-reconcile-mailbox").addEventListener("click", () => reconcileDialog.showModal());
  $("mailbox-reconcile-cancel").addEventListener("click", () => reconcileDialog.close());
  $("mailbox-reconcile-confirm").addEventListener("click", () => {
    reconcileDialog.close();
    void runMailboxReconcile();
  });
  /* AS5 U3：内置《隐私与数据说明》查看入口（设置页「隐私」节按钮） */
  const privacyDocDialog = $("privacy-doc-dialog");
  const privacyDocOpen = $("privacy-doc-open");
  if (privacyDocOpen && privacyDocDialog) {
    privacyDocOpen.addEventListener("click", () => {
      const body = $("privacy-doc-body");
      if (body) renderPrivacyDoc(body);
      privacyDocDialog.showModal();
    });
  }
  /* 轮换 Worker 密钥（单元②）：一次确认弹层后复用既有 rotate-worker-keys 动作；
     可见性由 renderRemote 按 environment 组件证据决定 */
  const rotateDialog = $("remote-rotate-dialog");
  $("remote-rotate-keys").addEventListener("click", () => rotateDialog.showModal());
  $("remote-rotate-cancel").addEventListener("click", () => rotateDialog.close());
  $("remote-rotate-confirm").addEventListener("click", () => {
    rotateDialog.close();
    void remoteAction("rotate-worker-keys");
  });
  /* 云端处理状态行（CLOUD-CONSENT-AUTO-1 U2）：只读陈述，不再有点击动作——
     开关退役后云端处理默认允许，连接就绪即可派发。 */
  /* 撤销云端授权（CLOUD-TOGGLE-1 U3：自隐私区迁入连接卡「高级操作与诊断」，
     云生命周期动作归唯一的云端处理控制面）。
     可见性复用共享 store 的自动化快照 actions 闭集（零新增轮询者）；
     危险动作两击确认走 armDeleteConfirmation 既有模式。 */
  const cloudRevokeButton = $("cloud-revoke-credentials");
  cloudRevokeButton.addEventListener("click", () => {
    armDeleteConfirmation(cloudRevokeButton, "确认撤销云端授权？", async () => {
      await runCloudRevoke(cloudRevokeButton);
    });
  });
  currentStore.subscribe("automation", renderCloudRevokeRow);
  $("copy-diagnostics").addEventListener("click", async () => {
    /* 连接诊断（单元③）：remote 字段以卡面同一连接快照为准（同源，消除
       「应用级 authorization_missing vs 卡面 valid」的陈旧视图矛盾）；
       组件层只复制闭集状态码与环境缺钥清单，绝不复制 evidence 自由字段 */
    const diagnosticSource = remoteSnapshotValue || settingsValue?.remote || null;
    const diagnosticOverall = diagnosticSource?.overall || {};
    const diagnosticComponents = (Array.isArray(diagnosticSource?.components) ? diagnosticSource.components : [])
      .map((component) => {
        const entry = {
          component: String(component?.component || component?.name || ""),
          state: String(component?.state || ""),
          code: String(component?.code || ""),
        };
        const missing = remoteComponentMissingSecrets(component);
        if (missing.length) entry.missing_secrets = missing;
        return entry;
      });
    const safe = {
      authentication: store.auth?.code || "unknown",
      settings: settingsValue?.code || "unknown",
      remote: String(diagnosticOverall.code || "unknown"),
      remote_state: String(diagnosticOverall.state || "unknown"),
      client_update: safeUpdateDiagnostics(updateValue),
      components: diagnosticComponents,
    };
    try {
      await navigator.clipboard.writeText(JSON.stringify(safe, null, 2));
      toast("连接诊断已复制", "ready");
    } catch {
      toast("连接诊断暂时无法复制，请稍后重试。", "error");
    }
  });
  const unsubscribeAuth = store.subscribe("auth", renderAccountSession);
  renderAccountSession(store.auth);
  /* 顶栏更新小组件与数据管理页：settings 作为组合根安装（路径冻结所限），
     cleanup 一并解除；更新卡改为订阅 store `update` 键（唯一轮询者在
     update-widget.js）。 */
  const cleanupUpdateWidget = await installUpdateWidget(store);
  const cleanupCourseData = await installCourseData(store);
  const cleanupClientReset = installClientReset();
  const cleanupDataMigration = installDataMigration();
  const unsubscribeUpdate = store.subscribe("update", renderUpdate);
  renderUpdate(store.update);
  return () => {
    teardownRemoteActionState();
    stopLoginPoll();
    unsubscribeAuth();
    unsubscribeUpdate();
    cleanupUpdateWidget?.();
    cleanupCourseData?.();
    cleanupClientReset?.();
    cleanupDataMigration?.();
    window.removeEventListener("courselens:page", handlePageRequest);
    window.removeEventListener("courselens:logout", handleLogoutRequest);
    window.removeEventListener("focus", handleWindowFocusForInstallWait);
    window.removeEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
    window.removeEventListener("courselens:login-dialog-open", handleLoginDialogOpen);
    $("toggle-password-visibility").removeEventListener("click", handlePasswordToggle);
    $("login-use-saved").removeEventListener("click", handleLoginUseSaved);
    $("login-auto-connect").removeEventListener("change", handleLoginAutoConnect);
    $("login-manual-mode").removeEventListener("click", handleLoginManualMode);
    $("login-back-saved").removeEventListener("click", handleLoginBackSaved);
    $("network-mode").removeEventListener("change", applyProxyDisclosure);
    document.querySelector(".settings-nav").removeEventListener("click", handleNavClick);
    /* SETTINGS-UX-1 A1：scrollspy 观察器与滚动兜底监听对称拆除（pagehide 拆绑
       纪律：任一清理抛错会吞掉余下清理） */
    if (spyObserver) {
      spyObserver.disconnect();
      spyPage?.removeEventListener("scroll", handleSpyScroll);
    }
    $("fudan-auto-connect").removeEventListener("change", handleFudanAutoConnect);
    $("github-auto-connect").removeEventListener("change", handleGithubAutoConnect);
    $("fudan-auto-connect-account").removeEventListener("change", handleAutoConnectAccountChange);
    /* P56 主题三态换装后旧 id settings-theme-toggle 已不存在：清理必须对称
       拆现行绑定（settings-theme-mode），否则 pagehide 拆绑中途抛错吞掉余下清理 */
    $("settings-theme-mode").removeEventListener("change", handleThemeModeChange);
    /* A11Y-IMPL-4：界面字号绑定对称拆绑（同上 pagehide 纪律） */
    $("settings-ui-font").removeEventListener("change", handleUiFontChange);
    $("update-background-checks").removeEventListener("change", handleUpdateBackgroundChecks);
    $("media-stream-proxy").removeEventListener("change", handleMediaStreamProxy);
  };
}
