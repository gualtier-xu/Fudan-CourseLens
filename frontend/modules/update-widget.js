import { apiV3, postV3 } from "./api.js";
import { macUpdateEntry, macUpdateSnapshot, onMacUpdateChange, runMacUpdateCheck } from "./update-mac.js";
import { $, toast } from "./ui.js";

/* 顶栏更新小组件：client-update 快照的唯一轮询者，经共享 store 发布 `update`
   键（settings 更新卡是订阅方，同 automation「唯一轮询者+订阅方」先例）。
   状态映射冻结自 UX-1 A2 + 台账 §1/E1：
   - ambient 态（idle/up_to_date/offline/checking/policy_blocked/healthy）恒隐藏，
     设置更新卡保持完整事实面；
   - failed/rolled_back 仅在用户激活过的操作之后可见（armed）；
   - 44×44 稳定槽位原位换装；busy 全程 disabled+aria-busy；进度=阶段词，无百分比；
   - available 单击 fire `update_now {confirmed:true}` 不 await 渲染 + ≤2s 快照轮询
     （update_now 是单请求同步编排，期间进度只能经 GET 快照到达，fire 即启轮询）；
   - failed/rolled_back 单击跳设置并定位 #settings-update-group；
   - 播报只覆盖可见态迁移，后台 checking 保持安静。 */

/* 状态词与 settings.js `updateLabels` 同词表（workbench 钉住两表一致）。 */
const UPDATE_STAGE_LABELS = Object.freeze({
  idle: "尚未检查", checking: "正在检查", offline: "离线",
  up_to_date: "已是最新", available: "发现更新", downloading: "正在下载",
  verifying: "正在验证", ready_to_restart: "等待重启", applying: "正在应用",
  healthy: "更新成功", rolled_back: "已自动回滚", failed: "更新失败",
  policy_blocked: "策略阻止",
});

const BUSY_STATES = new Set(["downloading", "verifying", "applying"]);
const USER_FAILURE_STATES = new Set(["failed", "rolled_back"]);
/* 可见闭集（A2 表的可见行）：其余八态一律 hidden。 */
const VISIBLE_STATES = new Set([
  "available", "downloading", "verifying", "applying", "ready_to_restart",
  "failed", "rolled_back",
]);

const UPDATE_POLL_INTERVAL_MS = 2000;
const UPDATE_POLL_MAX_FETCH_FAILURES = 5;

const UPDATE_ACTION_FAILURE_TEXT = "更新操作暂未完成，请稍后重新检查。";
/* 与 settings.js 指导表同句：并发冲突不是操作失败，保持原态即可。 */
const UPDATE_ACTION_BUSY_TEXT = "另一个更新操作仍在进行。请稍候再检查。";
/* D-20261009-04 结果态落地闭集：update_now 是单请求同步编排，202 响应即终态——
   终态是人话结果，不允许「点了没反应」。healthy=编排装完（随后的重启由
   complete_restart 自行编排）；up_to_date=检查后无可装；available=编排结束但
   没有进入安装（真实编排不应停在此态，停了就如实说没开始）。 */
const UPDATE_RESULT_DONE_TEXT = "更新成功。";
const UPDATE_RESULT_UP_TO_DATE_TEXT = "已是最新版本，暂时不需要更新。";
const UPDATE_RESULT_NOT_STARTED_TEXT = "更新没有开始，请稍后重新检查。";
/* D-20261009-04 过渡态进行中文案：编排在途且阶段快照未到达时的诚实前置词；
   真正的阶段词（下载/验证/应用）由 ≤2s 快照接管覆盖。 */
const UPDATE_PENDING_TEXT = "正在准备更新…";
/* N5PR-P4 同款防闪（player-core SPINNER_DELAY 先例）：编排 >300ms 未完成才亮
   过渡词——快速完成（无可装编排/测试桩毫秒返回）不闪词。 */
const UPDATE_PENDING_FACE_DELAY_MS = 300;

function updateAriaLabel(state, availableVersion, downloadPercent = null) {
  if (state === "available") return `发现新版本 ${availableVersion || ""}，点击开始更新`;
  if (state === "downloading") {
    /* UPDATE-UX-1 三律：下载在途的进度进可读名（≤2s 快照节拍刷新） */
    return downloadPercent === null ? "正在下载更新…" : `正在下载更新… ${downloadPercent}%`;
  }
  if (state === "verifying") return "正在验证更新…";
  if (state === "applying") return "正在应用更新…";
  if (state === "ready_to_restart") return "更新已就绪，即将重启并完成健康确认";
  if (state === "failed") return "更新失败，点击进入恢复";
  if (state === "rolled_back") return "已自动回滚，点击查看详情";
  return "";
}

/* failed/rolled_back 的播报词（进入恢复/查看详情是动作提示，不重复 raw 细节） */
const ANNOUNCE_TEXT = Object.freeze({
  available: "发现可信更新，可开始安装",
  downloading: "正在下载更新",
  verifying: "正在验证更新",
  applying: "正在应用更新",
  ready_to_restart: "更新已就绪，即将重启并完成健康确认",
  failed: "更新失败，请打开设置查看恢复入口",
  rolled_back: "已自动回滚，请打开设置查看详情",
});

let armed = false; /* E1：用户激活过的操作在途/失败后，失败态才允许进顶栏 */
let firing = false; /* update_now 在途防重入（后端 mutex 收敛竞争为 update_busy） */
let pollTimer = 0;
let pollFetchFailures = 0;
let announcedState = "";
let snapshotRequestSeq = 0; /* 快照请求代际：乱序后到的陈旧响应直接丢弃 */

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = 0;
}

function startPolling(store) {
  if (pollTimer) return;
  /* 失败预算按轮询会话重置：历史会话的取数失败不清零会让新轮询第一拍就停 */
  pollFetchFailures = 0;
  pollTimer = window.setInterval(() => { void refreshSnapshot(store); }, UPDATE_POLL_INTERVAL_MS);
}

function publish(store, snapshot) {
  /* 唯一发布点：store 键供设置卡订阅；顶栏渲染与轮询同步在此收敛 */
  store.set("update", snapshot ?? null);
  renderWidget(store, snapshot);
}

function announce(state) {
  if (!VISIBLE_STATES.has(state) || announcedState === state) return;
  announcedState = state;
  const live = $("update-live");
  if (live) live.textContent = ANNOUNCE_TEXT[state] || "";
}

/* D-20261009-04：过渡态进行中文案上脸（aria-label/title，图标钮的可读名）。
   只换可读名不动 dataset 终态——阶段事实仍以快照为准。 */
function applyPendingFace(button) {
  if (!button) return;
  button.setAttribute("aria-label", UPDATE_PENDING_TEXT);
  button.setAttribute("title", UPDATE_PENDING_TEXT);
}

/* UPDATE-UX-1（mac 宿主）：顶栏按钮只服务 mac 检查道（update-mac.js 三态）——
   有新版=与 Windows available 同款醒目态（金点+安装 glyph），单击跳设置更新
   组；无新版/未检查/检查失败一律安静隐藏（不打扰）。后端更新链在 mac 上保持
   fail-closed 静默（后台检查不启动、快照 ambient），逐位不参与。 */
function renderMacWidget() {
  const button = $("update-toggle");
  if (!button) return;
  const state = macUpdateSnapshot();
  const visible = state.phase === "available";
  button.hidden = !visible;
  if (!visible) {
    button.removeAttribute("data-update-state");
    announcedState = "";
    return;
  }
  button.dataset.updateState = "available";
  const label = `发现新版本 ${state.version || ""}，点击查看下载`;
  button.setAttribute("aria-label", label);
  button.setAttribute("title", label);
  button.disabled = false;
  button.removeAttribute("aria-busy");
  const installIcon = button.querySelector('svg[data-update-icon="install"]');
  const restartIcon = button.querySelector('svg[data-update-icon="restart"]');
  if (installIcon) installIcon.hidden = false;
  if (restartIcon) restartIcon.hidden = true;
  const dot = button.querySelector("[data-update-dot]");
  if (dot) dot.hidden = false;
}

function renderWidget(store, snapshot) {
  if (macUpdateEntry()) {
    renderMacWidget();
    return;
  }
  const button = $("update-toggle");
  const state = snapshot && typeof snapshot.state === "string" ? snapshot.state : "";
  /* 用户激活判定：busy/ready_to_restart 只会由确认过的操作到达；ambient 快照
     里的失败（后台检查离线/策略阻止）armed=false → 顶栏保持安静。 */
  if (BUSY_STATES.has(state) || state === "ready_to_restart") armed = true;
  if (state === "healthy" || state === "up_to_date" || state === "idle") armed = false;
  if (!button) return;
  /* D-20261009-04：编排在途时非 busy 快照不得翻转在途呈现——编排 mutex 合同
     期间快照不应变态，实测（合成壳/竞态）available 快照会瞬时复按钮+回退文案，
     学生视角=「点了没反应」。在途呈现由 fireUpdateNow 的终态发布或失败路径收口。 */
  if (firing && !BUSY_STATES.has(state) && !USER_FAILURE_STATES.has(state)) {
    button.hidden = false;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    applyPendingFace(button);
    startPolling(store);
    return;
  }
  const visible = VISIBLE_STATES.has(state) && !(USER_FAILURE_STATES.has(state) && !armed);
  button.hidden = !visible;
  if (!visible) {
    button.removeAttribute("data-update-state");
    /* 状态离开可见集后复位播报去重：再回归时重新播报（a11y） */
    announcedState = "";
    if (!firing) stopPolling();
    return;
  }
  button.dataset.updateState = state;
  const busy = BUSY_STATES.has(state);
  const total = Number(snapshot.download_total);
  const bytes = Number(snapshot.download_bytes);
  const downloadPercent = busy && Number.isFinite(total) && Number.isFinite(bytes) && total > 0
    ? Math.round(Math.max(0, Math.min(bytes, total)) / total * 100)
    : null;
  const label = updateAriaLabel(state, snapshot.available_version, downloadPercent);
  button.setAttribute("aria-label", label);
  button.setAttribute("title", label);
  /* aria-busy 先清再算 disabled：先移除再按态重设，避免上一轮的 busy 残留 */
  button.removeAttribute("aria-busy");
  button.disabled = busy || state === "ready_to_restart";
  if (busy) button.setAttribute("aria-busy", "true");
  const installIcon = button.querySelector('svg[data-update-icon="install"]');
  const restartIcon = button.querySelector('svg[data-update-icon="restart"]');
  if (installIcon) installIcon.hidden = state === "ready_to_restart";
  if (restartIcon) restartIcon.hidden = state !== "ready_to_restart";
  const dot = button.querySelector("[data-update-dot]");
  if (dot) dot.hidden = false;
  announce(state);
  /* 操作在途（busy 快照或已 fire 的单请求编排）时 ≤2s 轮询；终态即停 */
  if (busy) {
    startPolling(store);
  } else if (!firing) {
    stopPolling();
  }
}

async function refreshSnapshot(store) {
  const requestId = ++snapshotRequestSeq;
  try {
    const snapshot = await apiV3("client-update");
    if (requestId !== snapshotRequestSeq) return; /* 陈旧响应：已被更新的请求取代 */
    pollFetchFailures = 0;
    publish(store, snapshot);
  } catch {
    if (requestId !== snapshotRequestSeq) return;
    pollFetchFailures += 1;
    if (pollFetchFailures >= UPDATE_POLL_MAX_FETCH_FAILURES) {
      stopPolling();
      publish(store, null);
      return;
    }
    if (!pollTimer) publish(store, null);
  }
}

async function fireUpdateNow(store) {
  if (firing) return;
  firing = true;
  /* A3：立即进入过渡态（disabled+aria-busy）再发起请求，渲染不等响应；
     D-20261009-04：>300ms 防闪窗后过渡词上脸（快速完成不闪词）。 */
  const button = $("update-toggle");
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  announce("downloading");
  const pendingTimer = window.setTimeout(() => {
    if (firing) applyPendingFace($("update-toggle"));
  }, UPDATE_PENDING_FACE_DELAY_MS);
  /* 真实 update_now 是单请求同步编排：202 只带终态，期间进度只能经
     GET 快照到达，fire 即启动有界轮询（终态/连续失败停轮语义不变）。 */
  startPolling(store);
  try {
    const snapshot = await postV3("client-update/actions", { action: "update_now", confirmed: true });
    window.clearTimeout(pendingTimer);
    /* 编排已出终态：先摘在途门再发布结果面——否则在途门会把终态快照
       重新摁回过渡态（合成壳毫秒终态场景里学生看到的仍是「点了没反应」）。 */
    firing = false;
    if (snapshot && USER_FAILURE_STATES.has(snapshot.state)) {
      /* 202 正常返回也可能携带用户失败终态：对齐 catch 路径可见性（E1） */
      armed = true;
    }
    publish(store, snapshot);
    announceUpdateResult(snapshot);
  } catch (error) {
    window.clearTimeout(pendingTimer);
    /* 先摘在途门再走恢复刷新：纠正快照要按真实事实渲染面，不能被在途门
       摁回过渡态（否则 409/失败路径的恢复呈现永远到不了学生眼前）。 */
    firing = false;
    if (error && error.code === "update_busy") {
      /* 409 并发冲突：mutex 被占，不是本次操作失败——保持当前态 + 人话提示 */
      toastActionBusy();
    } else {
      armed = true;
      renderFailurePresentation();
      announce("failed");
      toastActionFailure();
    }
    await refreshSnapshot(store);
  } finally {
    firing = false;
    const state = String($("update-toggle")?.dataset?.updateState || "");
    if (!BUSY_STATES.has(state)) stopPolling();
  }
}

/* D-20261009-04 结果态落地：编排终态人人有回音（toast 区 aria-live 自动播报）。
   ready_to_restart 有可见态+播报，不重复。 */
function announceUpdateResult(snapshot) {
  const state = snapshot && typeof snapshot.state === "string" ? snapshot.state : "";
  if (state === "healthy") {
    appendToast(UPDATE_RESULT_DONE_TEXT, "ready");
  } else if (state === "up_to_date") {
    appendToast(UPDATE_RESULT_UP_TO_DATE_TEXT, "ready");
  } else if (state === "available") {
    appendToast(UPDATE_RESULT_NOT_STARTED_TEXT, "checking");
  } else if (USER_FAILURE_STATES.has(state)) {
    toastActionFailure();
  }
}

function appendToast(text, state) {
  /* 闭集短句（同设置卡动作先例），绝不内联 raw error */
  const region = $("toast-region");
  if (!region) return;
  const node = document.createElement("div");
  node.className = "toast";
  node.dataset.state = state;
  node.textContent = text;
  region.append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function toastActionFailure() {
  appendToast(UPDATE_ACTION_FAILURE_TEXT, "error");
}

function toastActionBusy() {
  appendToast(UPDATE_ACTION_BUSY_TEXT, "checking");
}

/* A3.4：POST 失败 → 小组件本地转 failed 态（E1 操作域可见），不发伪造快照进
   store；随后的快照刷新用真实事实覆盖本地呈现。 */
function renderFailurePresentation() {
  const button = $("update-toggle");
  if (!button) return;
  button.hidden = false;
  button.dataset.updateState = "failed";
  const label = updateAriaLabel("failed", "");
  button.setAttribute("aria-label", label);
  button.setAttribute("title", label);
  button.disabled = false;
  button.removeAttribute("aria-busy");
  const dot = button.querySelector("[data-update-dot]");
  if (dot) dot.hidden = false;
}

function openUpdateGroupInSettings() {
  window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "settings" }));
  window.requestAnimationFrame(() => {
    document.getElementById("settings-update-group")?.scrollIntoView({ block: "start" });
  });
}

/* UPDATE-UX-1 mac 检查编排：启动后自动查一次（自动化缺省：零设置项），手动
   「检查更新」同走此道。当前版本取自后端快照（同源接口，正常毫秒级到达）；
   短暂等待后仍未知则诚实降级——绝不假称「已是最新」。 */
async function runMacCheckForHost(store) {
  if (!macUpdateEntry()) return;
  let version = "";
  for (let attempt = 0; attempt < 10 && !version; attempt += 1) {
    const value = store.update;
    version = typeof value?.current_version === "string" ? value.current_version.trim() : "";
    if (!version && attempt < 9) {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
  }
  await runMacUpdateCheck(version);
}

async function handleMacCheckClick(store) {
  toast("正在检查更新…", "checking");
  await runMacCheckForHost(store);
  const state = macUpdateSnapshot();
  if (state.phase === "available") {
    toast(state.version ? `检查完成：发现新版本 ${state.version}，点「打开下载页」获取。` : "检查完成：发现新版本。", "ready");
  } else if (state.phase === "up_to_date") {
    toast("检查完成：已是最新版本。", "ready");
  } else if (state.phase !== "checking") {
    toast("检查没有完成：稍后再试，或直接打开下载页确认。", "caution");
  }
}

export function installUpdateWidget(store) {
  const button = $("update-toggle");
  const handleClick = () => {
    /* D10：点击即同步锁——双击的第二笔在渲染循环重新启用前到达也会被拦下
       （busy 恢复路径统一由快照渲染/renderFailurePresentation 释放）。 */
    if (button?.disabled) return;
    /* mac 宿主：顶栏按钮只有「查看下载」一个语义，单击定位到设置更新组 */
    if (macUpdateEntry()) {
      openUpdateGroupInSettings();
      return;
    }
    const state = String(button?.dataset.updateState || "");
    if (state === "available") {
      if (button) button.disabled = true;
      void fireUpdateNow(store);
    } else if (state === "failed" || state === "rolled_back") openUpdateGroupInSettings();
  };
  button?.addEventListener("click", handleClick);
  const macCheckButton = $("update-mac-check");
  const handleMacCheck = () => {
    if (macCheckButton?.disabled) return;
    void handleMacCheckClick(store);
  };
  macCheckButton?.addEventListener("click", handleMacCheck);
  const unsubscribeMac = onMacUpdateChange(() => {
    if (macUpdateEntry()) renderMacWidget();
  });
  const handleRefreshRequest = () => { void refreshSnapshot(store); };
  window.addEventListener("courselens:update-refresh", handleRefreshRequest);
  const handlePageHide = () => stopPolling();
  window.addEventListener("pagehide", handlePageHide);
  /* 初始一次快照：ambient 态决定显隐；之后只有操作在途/显式请求才轮询。
     mac 宿主快照到达后自动跑一次 mac 检查（有新版即顶栏点亮）。 */
  void refreshSnapshot(store).then(() => { void runMacCheckForHost(store); });
  return () => {
    button?.removeEventListener("click", handleClick);
    macCheckButton?.removeEventListener("click", handleMacCheck);
    unsubscribeMac();
    window.removeEventListener("courselens:update-refresh", handleRefreshRequest);
    window.removeEventListener("pagehide", handlePageHide);
    stopPolling();
  };
}
