/* tasks-drawer.js —— 任务抽屉生命周期核心（ARCH-DEBT-1 拆分后门面）。
   任务卡渲染族与自动化执行记录族已按 R7-#28 设计稿拆至 tasks-drawer/ 子目录
   模块（纯移动、行为零变化）。本门面保留轮询定时器/SSE 事件源/刷新调度
   （scheduleDrawerRefresh/openTaskEventSource）、loadTasks/loadDrawerRemote
   与安装器（installTasksDrawer），并 re-export 原公共导出面（app.js 与行为
   测试的具名导入零变化）。 */

import { apiV3 } from "./api.js";
import { $, closeOverlay, evidenceText, openOverlay } from "./ui.js";
import { loadAutomationRuns } from "./tasks-drawer/automation-runs.js";
import { announceNewlyCompletedSubtitleTasks, lastTasksLoadedAt, renderTasks } from "./tasks-drawer/task-cards.js";
export { taskPhaseStates, renderTasks } from "./tasks-drawer/task-cards.js";

let taskTimer = 0;

let eventSource = null;

let disposed = false;



/* 应用级 SSE（合同 §3.3）：安装时建立、随 app 清理释放，抽屉只消费共享快照；
   断线保留最后一次数据，任务新鲜度由 SSE 消息驱动。 */
let storeRef = null;
/* SSE 突发合并状态：queued=微任务窗口内已排程；in-flight=刷新请求在途；dirty=在途期间有新消息 */
let drawerRefreshQueued = false;

let drawerRefreshInFlight = false;

let drawerRefreshDirty = false;

/* 会话再校验门信封守卫（REALTEST-H5 实证：每 ~5 分钟存在 ≤10s 的门信封窗，
   形状=tasks 空 + counts 空）。那一刻后端不是「没有任务」，而是「暂时读不到
   任务」——照常覆盖渲染会把好列表闪成「暂无任务」（观测 11 次的抽屉闪烁）。
   已持有好快照时跳过本轮渲染、保留上一状态；冷启动（从未拿到快照）照常
   渲染，保持门信封的诚实空态（H4）。真实空列表 counts 是对象，不受影响。 */
function isTasksGateEnvelope(value) {
  return Boolean(value)
    && Array.isArray(value.tasks) && value.tasks.length === 0
    && (value.counts == null || typeof value.counts !== "object");
}

let lastGoodTasksValue = false;

export async function loadTasks(store) {
  try {
    const value = await apiV3("tasks");
    if (isTasksGateEnvelope(value)) {
      if (lastGoodTasksValue) return; /* 门信封窗：保留上一渲染 */
    } else {
      lastGoodTasksValue = true;
    }
    announceNewlyCompletedSubtitleTasks(Array.isArray(value?.tasks) ? value.tasks : []);
    renderTasks(store, value);
  } catch (error) {
    /* 顺带小件（CUI-2 P4）：加载失败行不再直显英文/裸码药丸——映射过的人话
       原样呈现；其余（英文 payload/HTTP 裸串）一律换「暂时连不上」诚实指路。 */
    const message = String(error?.message || "");
    $("task-counts").textContent = /[\u4e00-\u9fff]/.test(message)
      ? message
      : "暂时连不上，稍后点「刷新」再试";
  }
}

/* 车道C卡15：任务抽屉连接行不再「状态尚未确认」语焉不详——后端 overall
   闭集（ready/offline/action_required/degraded/unknown）直接给一句人话，
   告诉学生该不该等；形状外才回退 evidenceText 证据摘要。 */
const REMOTE_DRAWER_STATE_TEXT = Object.freeze({
  ready: "云端连接正常，任务可正常派发",
  offline: "云端处理未连接：在设置里开启远程连接后即可使用",
  /* UIAUDIT-1 F11：原句「云端处理需要处理：…」双「处理」拗口；顶栏模板自带
     「连接：」前缀，此处单次表述即可完整成句 */
  action_required: "需要完成授权，完成后任务即可派发",
  degraded: "云端连接部分可用，任务可能排队等待",
  unknown: "正在确认云端连接状态",
});

async function loadDrawerRemote() {
  const target = $("remote-drawer-state");
  try {
    const value = await apiV3("remote-connection");
    const overall = value.overall || value;
    const state = String(overall?.state || "");
    target.textContent = REMOTE_DRAWER_STATE_TEXT[state] || evidenceText(overall);
    target.dataset.state = overall?.state || "unknown";
  } catch (error) {
    target.textContent = error.message;
  }
}

/* SSE 突发合并：同一刷新周期只发一次权威状态请求。连续消息在微任务窗口内
   合并为一次刷新；刷新在途时到达的消息只置一次脏标记，响应完成后补恰好
   一次对账——后续事件不会被吞掉，任意时刻至多一个在途刷新。请求内容与
   语义不变：仍并行拉取 tasks + remote-connection + automation 快照，
   以后端响应为唯一权威。 */
function scheduleDrawerRefresh(store) {
  if (disposed) return;
  if (drawerRefreshInFlight) {
    drawerRefreshDirty = true;
    return;
  }
  if (drawerRefreshQueued) return;
  drawerRefreshQueued = true;
  queueMicrotask(() => {
    drawerRefreshQueued = false;
    if (disposed) return;
    drawerRefreshInFlight = true;
    Promise.all([loadTasks(store), loadDrawerRemote(), loadAutomationRuns()]).finally(() => {
      drawerRefreshInFlight = false;
      if (drawerRefreshDirty && !disposed) {
        drawerRefreshDirty = false;
        scheduleDrawerRefresh(store);
      }
    });
  });
}

function closeTaskEventSource() {
  eventSource?.close();
  eventSource = null;
}

function openTaskEventSource(store) {
  if (eventSource || disposed) return;
  eventSource = new EventSource("/api/v3/events?topics=remote-connection,remote-runs,tasks,automation");
  /* 后端 /api/v3/events 只发命名事件（event: tasks|remote-runs|remote-connection|automation），
     默认 message 通道永不触发——按订阅 topics 逐名挂监听（C8-1 死接线修复）。
     CLIENT-STATE：automation 主题补订——后端运行记录每次落账都发此主题，抽屉
     开着时「自动学习材料」段随事件即时刷新，不再等其它主题顺带触发。
     remote-connection 主题同时向窗口广播 courselens:remote-connection-changed：
     页眉连接点与设置页连接卡订阅同一事件，连接状态翻转即刻上屏（用户实证
     痛点「某些连接状态要人为点击才刷新」），SSE 仍由本模块单点持有（合同 §3.3）。 */
  const scheduleRefresh = () => scheduleDrawerRefresh(store);
  const onRemoteConnectionEvent = () => {
    window.dispatchEvent(new Event("courselens:remote-connection-changed"));
    scheduleRefresh();
  };
  eventSource.addEventListener("tasks", scheduleRefresh);
  eventSource.addEventListener("remote-runs", scheduleRefresh);
  eventSource.addEventListener("remote-connection", onRemoteConnectionEvent);
  eventSource.addEventListener("automation", scheduleRefresh);
  /* 断线不补发网络请求、不手动重连（引擎自行重连）；任务新鲜度由 SSE 消息
     驱动，抽屉打开时只做一次从未加载对账。 */
}

export function installTasksDrawer(store) {
  storeRef = store;
  let closeDrawer = null;
  const drawerRoot = $("tasks-root");
  const syncTaskTriggers = (expanded) => {
    document.querySelectorAll("[data-open-tasks]").forEach((btn) => {
      btn.setAttribute("aria-expanded", String(expanded));
    });
  };
  const openTasksDrawer = (trigger = null) => {
    closeDrawer = openOverlay({
      root: drawerRoot,
      trigger: trigger || $("task-chip"),
      onClose: () => {
        syncTaskTriggers(false);
        closeDrawer = null;
      },
      initialFocus: $("close-task-drawer"),
    });
    if (!closeDrawer) return; /* 单浮层守卫被挡：不产生任何副作用 */
    syncTaskTriggers(true);
    /* SSE 常驻后抽屉打开只在数据过期/从未加载时做一次有界 GET 对账；
       自动材料运行记录随打开刷新一次（窗口运行不依赖 SSE 推送）。 */
    if (!lastTasksLoadedAt) void loadTasks(store);
    else if (coursesDirtyWhileHidden) {
      /* REALFULL-1：抽屉隐藏期间课程列表装载过 → 打开即补一次对账换真名 */
      coursesDirtyWhileHidden = false;
      void loadTasks(store);
    }
    void loadDrawerRemote();
    void loadAutomationRuns();
  };
  const handleOpenTasksRequest = (event) => openTasksDrawer(event.detail?.trigger || null);
  $("task-chip").addEventListener("click", () => openTasksDrawer($("task-chip")));
  window.addEventListener("courselens:open-tasks", handleOpenTasksRequest);
  $("close-task-drawer").addEventListener("click", () => closeOverlay(drawerRoot));
  drawerRoot.addEventListener("click", (event) => {
    if (event.target?.dataset?.tasksScrim !== undefined) closeOverlay(drawerRoot);
  });
  /* SIMPLIFY-AUDIT-1 S2：手动「刷新」钮已退役——SSE 四主题推送 + 打开时
     有界对账已覆盖更新面；courselens:tasks-refresh 程序化事件链保留。 */
  const handleTasksRefresh = () => loadTasks(store);
  window.addEventListener("courselens:tasks-refresh", handleTasksRefresh);
  /* REALFULL-1：课程列表装载后即时把任务卡的「课程信息加载中」占位换成真名
     （此前占位只在下次 30s 轮询/重开抽屉才消失；学生未进课程选择页时占位
     会一直挂着）。抽屉可见时立即重取；不可见只记脏标记，下次打开补一次
     对账（既有「仅在数据过期/从未加载时对账」语义的目录维度扩展，不开新通道）。 */
  let coursesDirtyWhileHidden = false;
  const unsubscribeCoursesRerender = store.subscribe("courses", () => {
    if (disposed) return;
    if (drawerRoot.hidden) {
      coursesDirtyWhileHidden = true;
      return;
    }
    coursesDirtyWhileHidden = false;
    void loadTasks(store);
  });

  void loadTasks(store);
  void loadAutomationRuns();
  taskTimer = window.setInterval(() => void loadTasks(store), 30000);
  /* SSE 提升为应用生命周期：安装时建立唯一订阅，抽屉开合不再创建/关闭（合同 §3.3） */
  openTaskEventSource(store);
  return () => {
    disposed = true;
    window.clearInterval(taskTimer);
    closeTaskEventSource();
    unsubscribeCoursesRerender();
    window.removeEventListener("courselens:open-tasks", handleOpenTasksRequest);
    window.removeEventListener("courselens:tasks-refresh", handleTasksRefresh);
  };
}
