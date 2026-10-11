/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；AI 密钥与用量面（AI 密钥用量族）。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { apiV3 } from "../api.js";
import { $ } from "../ui.js";

/* DeepSeek 本机保存状态行（P58 升级为显眼确认条，复用 .status-pill 语言）：
   saved×configured 闭集文案；字段缺失时容忍（待确认，样式降级），不得崩溃。
   文案保持中性——不声称“当前使用的就是已保存的 key”（会话 key 可覆盖本机 key） */
const DEEPSEEK_SAVE_PENDING_TEXT = "保存状态待确认";

export function setDeepseekSaveState(text, tone = "") {
  const node = $("deepseek-save-state");
  if (!node) return;
  node.hidden = !text;
  node.textContent = text;
  if (tone) node.dataset.state = tone;
  else delete node.dataset.state;
}

export function renderDeepseekSaveState(deepseek) {
  const saved = deepseek?.saved;
  const configured = deepseek?.configured;
  if (typeof saved !== "boolean" || typeof configured !== "boolean") {
    setDeepseekSaveState(DEEPSEEK_SAVE_PENDING_TEXT);
    return;
  }
  if (saved && configured) setDeepseekSaveState("已保存在本机", "ready");
  else if (configured) setDeepseekSaveState("仅本次启动使用，未保存在本机");
  else if (saved) setDeepseekSaveState("保存状态异常，本机保存不可用", "error");
  else setDeepseekSaveState("未保存在本机");
}

/* U⑩：三档人话预设卡（省着用/日常够用/放开用）+AS3 第四档「不限」（哨兵 0）。
   回显只同步 aria-checked 与当前上限行，绝不在用户点击保存链路中途改写选择；
   缺省档=后端缺省值。 */
export function syncDeepseekBudgetPresets(rawTokens) {
  const raw = Number(rawTokens);
  const tokens = Number.isFinite(raw) && raw >= 0 ? raw : 100000;
  const container = $("deepseek-budget-presets");
  if (container) {
    for (const button of container.querySelectorAll(".budget-preset")) {
      button.setAttribute("aria-checked", String(Number(button.dataset.budget) === tokens));
    }
  }
  const label = $("max-deepseek-tokens-label");
  if (label) {
    label.textContent = tokens > 0
      ? `当前上限：${Math.round(tokens / 10000)} 万 tokens/日`
      : "当前上限：不设每日上限";
  }
}

/* AS6 消耗透镜：本机累计行与余额行住在 AI 卡 DeepSeek 区（ai-usage-month
   之后）。节点运行时建一次（守卫存在性，不解析 HTML）；后续只更新文案。
   测试壳的静态桩元素可能未连父节点——此时仅建节点保行为钉可跑。 */
export function ensureAiUsageSiblingLine(id) {
  const anchor = $("ai-usage-month");
  if (!anchor) return null;
  let node = $(id);
  if (!node) {
    node = document.createElement("p");
    node.id = id;
    node.className = "hint";
    if (anchor.parentNode) anchor.parentNode.append(node);
  }
  return node;
}

export function taskUsageMonthText(usage) {
  const tokens = Number(usage?.deepseek_tokens);
  const minutes = Number(usage?.runner_minutes);
  if (!Number.isFinite(tokens) || !Number.isFinite(minutes)) return "";
  const tokensText = tokens > 0
    ? (tokens < 10000 ? `${tokens} tokens` : `≈${Math.round(tokens / 1000) / 10} 万 tokens`)
    : "";
  const minutesText = minutes > 0
    ? (minutes < 1 ? "不到 1 分钟" : `${Math.round(minutes)} 分钟`)
    : "";
  const parts = [minutesText && `云端机器运行 ${minutesText}`, tokensText && `消耗 ${tokensText}`].filter(Boolean);
  return parts.length ? `本月云端消耗：${parts.join(" · ")}（本机累计，仅供参考）` : "本月暂无云端任务消耗记录";
}

function deepseekBalanceText(value) {
  const state = String(value?.state || "");
  if (state === "no_key") return "未配置 DeepSeek Key，暂无余额可查";
  if (state === "ok") {
    const amount = String(value?.total_balance || "").trim();
    if (!amount) return "暂时查不到余额";
    const suffix = value?.is_available === false ? "（Key 当前不可用）" : "";
    return `余额 ${amount}${value?.currency ? ` ${value.currency}` : ""}${suffix}`;
  }
  return "暂时查不到余额";
}

export async function refreshDeepseekBalance() {
  const node = ensureAiUsageSiblingLine("deepseek-balance-state");
  if (!node) return;
  try {
    node.textContent = deepseekBalanceText(await apiV3("deepseek-balance"));
  } catch {
    /* 请求失败不重试轰炸：如实呈现，下次打开设置页再取 */
    node.textContent = "暂时查不到余额";
  }
}
