/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；通用闭集小工具层。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { $ } from "../ui.js";

export function closedMapValue(map, key, fallback) {
  if (typeof key !== "string") return fallback;
  const normalized = key.trim();
  return normalized && Object.hasOwn(map, normalized) && typeof map[normalized] === "string"
    ? map[normalized]
    : fallback;
}

export function isPlainRecord(value) {
  try {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

export function closedDiagnosticValue(value, allowed) {
  if (typeof value !== "string") return value == null ? null : "unknown";
  const normalized = value.trim();
  if (!normalized) return null;
  return allowed.has(normalized) ? normalized : "unknown";
}

export function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "—";
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

/* D8：代理地址闭集校验——scheme http/https 且主机非空才算可解析；
   与后端 update-network 路由门同规则（等集），保存前不再盲发 POST。 */
export function proxyUrlParseable(value) {
  if (!/^https?:\/\//.test(value)) return false;
  try {
    return Boolean(new URL(value).hostname);
  } catch {
    return false;
  }
}

export function armDeleteConfirmation(button, confirmLabel, onConfirmed) {
  if (button.dataset.confirming === "true") {
    button.dataset.confirming = "false";
    button.classList.remove("confirming");
    button.textContent = button.dataset.originalLabel || button.textContent;
    void onConfirmed();
    return;
  }
  button.dataset.confirming = "true";
  button.dataset.originalLabel = button.textContent;
  button.classList.add("confirming");
  button.textContent = confirmLabel;
  window.setTimeout(() => {
    if (button.dataset.confirming === "true") {
      button.dataset.confirming = "false";
      button.classList.remove("confirming");
      button.textContent = button.dataset.originalLabel || button.textContent;
    }
  }, 4000);
}

export const GITHUB_REPO_FULL_NAME_RE = /^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/;
