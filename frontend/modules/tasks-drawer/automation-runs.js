/* ARCH-DEBT-1：本文件由 tasks-drawer.js 按 R7-#28 任务族设计稿拆分而来，
   纯移动重组、行为零变化；自动化执行记录族（含通用两步确认助手 armTwoStepButton）。tasks-drawer.js 门面保留抽屉
   生命周期核心（轮询/SSE/刷新调度/安装器）并 re-export 原公共导出面。 */
import { apiV3, postV3 } from "../api.js";
import { $, formatTime, setBusy, textElement, toast } from "../ui.js";
import { loadTasks } from "../tasks-drawer.js"; /* ESM 函数声明提升循环边：仅事件期调用（动作后刷新），求值期零调用，语义安全 */

const AUTOMATION_RUNS_VISIBLE_LIMIT = 5;
/* 第卅六案③：无启用规则且全部记录都已终态超过该窗 → 整段隐藏（安静抽屉） */
const AUTOMATION_RUNS_STALE_MS = 7 * 24 * 60 * 60 * 1000;
const AUTOMATION_RUN_OUTCOME_TEXT = Object.freeze({
  cloud_daily_completed: "已完成",
  budget_exhausted: "已延后（用量保护）",
  cloud_dispatch_paused: "已暂停",
  cloud_protocol_mismatch: "需要处理",
  cloud_config_mismatch: "需要处理",
  deepseek_key_missing: "需要处理",
  /* 第卅六案②：超过收口窗无结论的行由后端收敛；卡片如实说「长时间没有回音」 */
  run_abandoned: "已中止（长时间没有回音）",
});
let automationRunsValue = null;

/* 第卅六案①：运行时刻只认 run 自身的时刻。观测时刻（observed_at）只在陈旧度
   判据里兜底，绝不冒充运行时间——现场实证五张卡同秒显示，正是拿刷新时刻当
   运行时间。两个时刻都缺就什么都不显示。 */
function automationRunTimeLabel(run) {
  /* 标签必须与所显示的时间戳同源：有结束时刻才敢说「结束于」，否则只说
     「开始于」（收敛的 ghost 只有开始时刻，此前被误标成结束时刻）。 */
  const concluded = Number(run.concluded_at || 0) || 0;
  if (concluded > 0) return { label: "结束于", at: concluded };
  const started = Number(run.started_at || 0) || 0;
  if (started > 0) return { label: "开始于", at: started };
  return null;
}

/* 7 天隐藏判据（第卅六案③）：「记录已终态超过 7 天」以记录的结束面为准——
   concluded_at → observed_at（最后一次有证据的观测）→ started_at（最后兜底）。 */
function automationRunStalenessAt(run) {
  return Number(run.concluded_at || run.observed_at || run.started_at || 0) || 0;
}

function automationRunIsTerminal(run) {
  return Boolean(String(run.conclusion || "")) || String(run.state || "") === "completed";
}

/* 第卅六案②：非空 conclusion 一律判终态（此前只有 success/failure 两态，
   cancelled/timed_out 等远端结论被显示成「进行中」），本地收敛的 abandoned
   单独呈现为「已中止」。 */
function automationRunOutcome(run) {
  const conclusion = String(run.conclusion || "");
  if (conclusion === "success") return { state: "completed", text: "已完成" };
  if (conclusion === "abandoned") return { state: "failed", text: "已中止" };
  if (conclusion) return { state: "failed", text: "未完成" };
  if (String(run.state || "") === "completed") return { state: "failed", text: "已中止" };
  return { state: "running", text: "进行中" };
}

function automationRunsVisible() {
  if (!automationRunsValue) return false;
  const runs = Array.isArray(automationRunsValue.runs) ? automationRunsValue.runs : [];
  const rules = Array.isArray(automationRunsValue.rules) ? automationRunsValue.rules : [];
  if (rules.length > 0 || automationRunsValue.enabled === true) return true;
  /* 无启用规则：留最近 7 天内还有动静的记录；全终态超期的旧账整段隐藏 */
  const now = Date.now();
  return runs.some((run) => {
    const at = automationRunStalenessAt(run);
    if (!at) return !automationRunIsTerminal(run);
    return now - at * 1000 < AUTOMATION_RUNS_STALE_MS;
  });
}

/* 第卅六案③：单条运行记录删除（仅终态）。只删记录，不触已生成的字幕/PDF。 */
function appendAutomationRunDeleteButton(row, run) {
  const runKey = String(run.run_key || "");
  if (!runKey) return;
  const actions = document.createElement("div");
  actions.className = "row-actions";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "danger";
  button.textContent = "删除记录";
  button.dataset.confirmKey = `automation-run-delete:${runKey}`;
  restoreTwoStepArm(button);
  button.addEventListener("click", async () => {
    armTwoStepButton(button, "再点一次，删除这条记录", async () => {
      setBusy(button, true);
      try {
        await postV3("automation/runs-delete", { run_key: runKey });
        toast("已删除这条运行记录；已生成的字幕和文件不受影响");
        await loadAutomationRuns();
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
  });
  actions.append(button);
  row.append(actions);
}

function renderAutomationRunItem(run) {
  const outcome = automationRunOutcome(run);
  const row = document.createElement("article");
  row.className = "task automation-run";
  row.dataset.state = outcome.state;
  const body = document.createElement("div");
  body.className = "task-body";
  const title = run.trigger_kind === "schedule" ? "自动材料 · 计划窗口"
    : run.trigger_kind === "manual" ? "自动材料 · 手动检查" : "自动材料运行";
  body.append(textElement("strong", title, "t-name"));
  const badge = textElement("span", outcome.text, "t-state");
  badge.dataset.badgeState = outcome.state;
  body.append(badge);
  const counts = run.counts && typeof run.counts === "object" ? run.counts : {};
  const parts = [];
  if (counts.processed !== undefined) parts.push(`生成 ${counts.processed} · 延后 ${counts.deferred || 0}`);
  const code = String(run.error_code || "");
  if (code) parts.push(AUTOMATION_RUN_OUTCOME_TEXT[code] || `代码 ${code}`);
  if (parts.length) body.append(textElement("span", parts.filter(Boolean).join(" · "), "t-evidence"));
  const time = automationRunTimeLabel(run);
  if (time) {
    body.append(textElement("span", `${time.label} ${formatTime(time.at)}`, "t-meta"));
  }
  row.append(body);
  if (automationRunIsTerminal(run)) appendAutomationRunDeleteButton(row, run);
  return row;
}

export function renderAutomationRunsSection() {
  const target = $("task-list");
  if (!target) return;
  const existing = Array.from(target.children || [])
    .find((child) => String(child.className || "").split(/\s+/).includes("automation-runs-tier"));
  if (existing) existing.remove?.();
  if (!automationRunsVisible()) return;
  const runs = (Array.isArray(automationRunsValue.runs) ? automationRunsValue.runs : [])
    .slice(0, AUTOMATION_RUNS_VISIBLE_LIMIT);
  const section = document.createElement("section");
  section.className = "task-tier automation-runs-tier";
  section.append(textElement("h3", "自动学习材料"));
  /* AS11（第五十一案）唯一总开关状态行：目录「自动整理」勾选=唯一开关；
     N=0 时如实说「未勾选课程」，绝不假装自动处理还在跑。时刻来自快照
     schedule（SMART-SCHED 网格：工作日课后约半小时 + 每日 22:00 兜底），
     快照缺新键则诚实退回 times 并集，再缺才降级措辞。 */
  const rules = Array.isArray(automationRunsValue.rules) ? automationRunsValue.rules : [];
  const weekdayTimes = (Array.isArray(automationRunsValue.schedule?.weekday_times) ? automationRunsValue.schedule.weekday_times : [])
    .map((item) => String(item || "").trim()).filter(Boolean);
  const dailyTimes = (Array.isArray(automationRunsValue.schedule?.daily_times) ? automationRunsValue.schedule.daily_times : [])
    .map((item) => String(item || "").trim()).filter(Boolean);
  const times = (Array.isArray(automationRunsValue.schedule?.times) ? automationRunsValue.schedule.times : [])
    .map((item) => String(item || "").trim()).filter(Boolean);
  const scheduleText = weekdayTimes.length && dailyTimes.length
    ? `工作日课后约半小时自动处理 · 每日 ${dailyTimes.join("、")} 兜底`
    : times.length ? `每日 ${times.join("、")} 自动处理`
    : "自动处理按既有固定窗口运行";
  const countText = rules.length ? `已勾选 ${rules.length} 门` : "未勾选课程";
  section.append(textElement("p", `${scheduleText} · ${countText}。`, "hint automation-run-status"));
  section.append(textElement("p", "固定包：字幕 ASR · 课件 OCR · AI 总结与章节；结果按时限自动删除。", "hint automation-run-hint"));
  if (runs.length) {
    runs.forEach((run) => section.append(renderAutomationRunItem(run)));
  } else {
    section.append(textElement("p", "尚无运行记录。", "empty-state"));
  }
  target.append(section);
}

export async function loadAutomationRuns() {
  try {
    const value = await apiV3("automation");
    automationRunsValue = value && typeof value === "object" ? value : null;
  } catch {
    automationRunsValue = null; /* 快照不可用：整段隐藏，不渲染原始错误 */
  }
  renderAutomationRunsSection();
}

/* 历史记录：抽屉级单个 details，默认折叠；completed + canceled 混合按 updated_at 倒序 */
/* 两击臂式单确认（course-data armConfirmButton / settings armDeleteConfirmation
   同一先例）：首击换确认文案，再击执行，超时自动复位。 */

/* SWEEPFIX-1 T2（化身走查 SWEEP1-T2）：两击确认态此前只活在按钮 DOM 上，
   SSE/30s 轮询带来任何数据变化就整体重建列表，臂态随旧按钮一起销毁——
   学生按阅读节奏（≈1.5s）回头点第二击时按钮已复原，删除必落空。
   现在臂态按 confirmKey 身份挂到模块级注册表：重渲后新按钮经
   restoreTwoStepArm 继承臂态文案与剩余窗口，一个臂态一个时钟（重渲不重排），
   第二击执行/超时/记录消失才解除。无 confirmKey 的调用方维持旧单实例语义。 */
const TWO_STEP_ARM_MS = 6000;
const armedTwoStep = new Map();

function resetTwoStepButton(button, entry) {
  button.dataset.confirming = "false";
  button.textContent = entry?.originalLabel || button.dataset.originalLabel || button.textContent;
}

function scheduleTwoStepExpiry(key, entry) {
  if (entry.timer) return; /* 恢复路径不重复排程：一个臂态一个时钟 */
  entry.timer = window.setTimeout(() => {
    if (armedTwoStep.get(key) !== entry) return; /* 已执行或已被新臂接替 */
    armedTwoStep.delete(key);
    const button = entry.button;
    if (button && button.dataset.confirming === "true") resetTwoStepButton(button, entry);
  }, Math.max(0, entry.expiresAt - Date.now()));
}

export function armTwoStepButton(button, confirmLabel, run) {
  const key = String(button.dataset.confirmKey || "");
  if (button.dataset.confirming === "true") {
    const entry = armedTwoStep.get(key);
    if (entry) armedTwoStep.delete(key);
    resetTwoStepButton(button, entry);
    void run();
    return;
  }
  button.dataset.originalLabel = button.textContent;
  button.dataset.confirming = "true";
  button.textContent = confirmLabel;
  if (!key) {
    /* 无身份键（未接注册表）的调用方：维持旧的单实例 6s 复位语义 */
    window.setTimeout(() => {
      if (button.dataset.confirming === "true") resetTwoStepButton(button, null);
    }, TWO_STEP_ARM_MS);
    return;
  }
  const entry = {
    confirmLabel,
    originalLabel: button.dataset.originalLabel,
    expiresAt: Date.now() + TWO_STEP_ARM_MS,
    timer: 0,
    button,
  };
  armedTwoStep.set(key, entry);
  scheduleTwoStepExpiry(key, entry);
}

/* 重渲后的臂态继承：新按钮顶替旧按钮时按 confirmKey 认领仍在窗口内的
   臂态（文案 + 剩余时间），并把到期时钟指向自己。 */
export function restoreTwoStepArm(button) {
  const key = String(button.dataset.confirmKey || "");
  if (!key || button.dataset.confirming === "true") return;
  const entry = armedTwoStep.get(key);
  if (!entry) return;
  if (entry.expiresAt - Date.now() <= 0) {
    armedTwoStep.delete(key);
    return;
  }
  button.dataset.originalLabel = entry.originalLabel;
  button.dataset.confirming = "true";
  button.textContent = entry.confirmLabel;
  entry.button = button;
  scheduleTwoStepExpiry(key, entry);
}

/* U4：单条删除（终态卡）。只删任务记录，绝不触已生成的字幕/PDF 产物。 */
