import assert from "node:assert/strict";
import { readModuleSource, familySource } from "./frontend_exec_harness.mjs";
import {
  SHORTCUT_FIELD_SELECTOR,
  pageShortcutBlockedByTarget,
  hasOpenDialog,
  releasePointerActivatedFocus,
} from "../frontend/modules/ui.js";

/* FOCUS-DRIFT-1：全应用焦点政策 chokepoint 单元矩阵 + 接线钉。
 *
 * 政策全文见 docs/technical/focus-policy.md；这里钉三件事：
 * ① pageShortcutBlockedByTarget 让位语义矩阵（字段/roving 角色整块让位；
 *    button/a 仅激活键 Space/Enter 让位，其余键透传页面快捷键）；
 * ② hasOpenDialog 模态闸 + releasePointerActivatedFocus 指针焦点归还
 *    （键盘模态 data-input=key 绝不归还=键盘可达硬门；button/a 之外不归还）；
 * ③ 消费方接线钉：player-core 双守卫+dialog 闸+学习桌归还、dropdown 触发钮
 *    方向键 stopPropagation（防双触发）、ui.js 圈闭循环 preventScroll、
 *    shell/study/task-cards 焦点落点 preventScroll。
 * Tab 可达（正 tabindex=0）与 :focus-visible 环（accessibility.css data-input
 * 政策）由 workbench 源码钉与既有 a11y 套件承担，此处不重复。 */

/* —— 最小事件/元素桩：closest 只需支持 #id/.class/标签名/[role='x'] —— */
function stubElement({ id = "", tag = "div", role = null, classes = [] } = {}) {
  const ancestors = [];
  const node = {
    id,
    tagName: tag,
    attributes: new Map(role ? [["role", role]] : []),
    classList: { values: new Set(classes) },
    parent: null,
    blurred: 0,
    blur() { node.blurred += 1; },
    matches(selector) {
      /* 自匹配语义（releasePointerActivatedFocus 只对 activeElement.matches 求值） */
      const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
      return tokens.some((token) => {
        const roleMatch = /^\[role='([^']+)'\]$/.exec(token);
        if (roleMatch) return node.attributes.get("role") === roleMatch[1];
        if (token.startsWith("#")) return node.id === token.slice(1);
        if (token.startsWith(".")) return node.classList.values.has(token.slice(1));
        return String(node.tagName).toLowerCase() === token.toLowerCase();
      });
    },
    closest(selector) {
      const tokens = String(selector).split(",").map((token) => token.trim()).filter(Boolean);
      const check = (candidate) => {
        for (const token of tokens) {
          const roleMatch = /^\[role='([^']+)'\]$/.exec(token);
          if (roleMatch) {
            if (candidate.attributes?.get("role") === roleMatch[1]) return true;
          } else if (token.startsWith("#")) {
            if (candidate.id === token.slice(1)) return true;
          } else if (token.startsWith(".")) {
            if (candidate.classList?.values?.has(token.slice(1))) return true;
          } else if (String(candidate.tagName || "").toLowerCase() === token.toLowerCase()) {
            return true;
          }
        }
        return false;
      };
      let cursor = node;
      while (cursor) {
        if (check(cursor)) return cursor;
        cursor = cursor.parent;
      }
      return null;
    },
  };
  return node;
}

function stubEvent(key, target, extra = {}) {
  return { key, target, defaultPrevented: false, ctrlKey: false, metaKey: false, altKey: false, repeat: false, ...extra };
}

/* —— ① 让位语义矩阵 —— */
function verifyGuardMatrix() {
  const field = stubElement({ id: "transcript-search-input", tag: "input" });
  const textarea = stubElement({ tag: "textarea" });
  const select = stubElement({ tag: "select" });
  const contentEditable = stubElement({ tag: "div", classes: [] });
  for (const element of [field, textarea, select]) {
    for (const key of [" ", "Enter", "ArrowRight", "j"]) {
      assert.equal(pageShortcutBlockedByTarget(stubEvent(key, element)), true,
        `${element.tagName} 聚焦时 ${JSON.stringify(key)} 整块让位（编辑/滑杆语义）`);
    }
  }
  assert.equal(pageShortcutBlockedByTarget(stubEvent("ArrowRight", contentEditable)), false,
    "非 contenteditable div 不让位（未命中选择器）");

  const tab = stubElement({ tag: "button", role: "tab" });
  const option = stubElement({ tag: "button", role: "option" });
  const menu = stubElement({ tag: "div", role: "menu" });
  const listbox = stubElement({ tag: "div", role: "listbox" });
  for (const element of [tab, option, menu, listbox]) {
    for (const key of ["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"]) {
      assert.equal(pageShortcutBlockedByTarget(stubEvent(key, element)), true,
        `role=${element.attributes.get("role")} 方向/Home/End 整块让位（roving 语义不双触发）`);
    }
  }

  const button = stubElement({ id: "player-ctrl-play", tag: "button" });
  const link = stubElement({ tag: "a" });
  for (const element of [button, link]) {
    for (const key of [" ", "Enter"]) {
      assert.equal(pageShortcutBlockedByTarget(stubEvent(key, element)), true,
        `${element.tagName} 聚焦时激活键 ${JSON.stringify(key)} 让位（键盘可达硬门：原生激活保留）`);
    }
    for (const key of ["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "j", "J", "k", "l", "m", "f", "c", "x", "<", ">", "?"]) {
      assert.equal(pageShortcutBlockedByTarget(stubEvent(key, element)), false,
        `${element.tagName} 聚焦时 ${JSON.stringify(key)} 透传页面快捷键（点击按钮后快捷键即刻存活）`);
    }
  }

  /* 祖先链归属：聚焦面在按钮内（如图标 span）同样命中 */
  const icon = stubElement({ tag: "span" });
  icon.parent = button;
  assert.equal(pageShortcutBlockedByTarget(stubEvent(" ", icon)), true, "按钮内子元素聚焦时激活键仍让位");
  assert.equal(pageShortcutBlockedByTarget(stubEvent("j", icon)), false, "按钮内子元素聚焦时字母键仍透传");

  /* 非元素 target / 缺 target：不拦截（页面级未聚焦态全可用） */
  assert.equal(pageShortcutBlockedByTarget(stubEvent(" ", null)), false, "无 target 不让位");
  assert.equal(pageShortcutBlockedByTarget({ key: " ", target: "text-target" }), false, "非元素 target 不让位");
}

/* —— ② 模态闸 + 指针焦点归还 —— */
function verifyDialogGateAndRelease() {
  assert.equal(hasOpenDialog({ querySelector: () => ({}) }), true, "dialog[open] 命中=有模态");
  assert.equal(hasOpenDialog({ querySelector: () => null }), false, "无 dialog[open]=无模态");
  assert.equal(hasOpenDialog({}), false, "query 缺失=无模态（假 DOM 兼容）");

  const button = stubElement({ tag: "button" });
  const scope = { contains: (node) => node === button };
  const rootPointer = { documentElement: { dataset: { input: "pointer" } }, activeElement: button };
  assert.equal(releasePointerActivatedFocus(scope, rootPointer), true, "指针模态+按钮聚焦=归还");
  assert.equal(button.blurred, 1, "归还=blur 一次");

  const rootKey = { documentElement: { dataset: { input: "key" } }, activeElement: button };
  assert.equal(releasePointerActivatedFocus(scope, rootKey), false, "键盘模态绝不归还（Tab 焦点与 :focus-visible 环零回退）");
  assert.equal(button.blurred, 1, "键盘模态未 blur");

  const slider = stubElement({ tag: "input" });
  const rootSlider = { documentElement: { dataset: { input: "pointer" } }, activeElement: slider };
  assert.equal(releasePointerActivatedFocus(scope, rootSlider), false, "滑杆/输入面聚焦不归还（编辑态让位，释放走既有路径）");
  assert.equal(slider.blurred, 0, "滑杆未被 blur");

  const rootOutside = { documentElement: { dataset: { input: "pointer" } }, activeElement: button };
  assert.equal(releasePointerActivatedFocus({ contains: () => false }, rootOutside), false, "scope 外不归还");
  const rootNone = { documentElement: { dataset: { input: "pointer" } }, activeElement: null };
  assert.equal(releasePointerActivatedFocus(scope, rootNone), false, "无 activeElement 无操作");
}

/* —— ③ 消费方接线钉（源码级：防止守卫被绕开/回退成散点选择器） —— */
function verifyWiringPins() {
  const playerCore = familySource("player-core");
  const ui = readModuleSource("../frontend/modules/ui.js");
  const dropdown = readModuleSource("../frontend/modules/dropdown.js");
  const shell = readModuleSource("../frontend/modules/shell.js");
  const study = readModuleSource("../frontend/modules/study.js");
  const taskCards = readModuleSource("../frontend/modules/tasks-drawer/task-cards.js");

  /* player-core：双守卫走 chokepoint、选择器收窄、模态闸豁免 ?、学习桌归还 */
  assert.ok(playerCore.includes("pageShortcutBlockedByTarget(event)"), "player-core 双守卫统一走焦点政策 chokepoint");
  assert.ok(playerCore.includes('import { $, bumpLocalMetric, clear, setBusy, toast, hasOpenDialog, pageShortcutBlockedByTarget, releasePointerActivatedFocus } from "./ui.js";'),
    "player-core 显式引入 chokepoint 三函数");
  assert.ok(playerCore.includes('const PLAYER_SHORTCUT_EDITABLE = "input, select, textarea, [contenteditable=\'true\'], [role=\'menu\'], [role=\'listbox\'], [role=\'tab\'], [role=\'option\']";'),
    "守卫字段集已收窄（button,a 移出）");
  assert.ok(playerCore.includes("if (target.closest(PLAYER_SHORTCUT_EDITABLE)) return false;"), "字段让位行留任（PLAYER-INTERACT-REPAIR-1 钉面延续）");
  assert.ok(playerCore.includes('hasOpenDialog() && event.key !== "?"'), "模态闸在位且豁免 ? 键位开关");
  assert.ok(playerCore.includes("releasePointerActivatedFocus(studyDesk)"), "学习桌全域指针焦点归还已接线");
  assert.ok(playerCore.includes('studyDesk.addEventListener("click", handleDeskClickFocusRelease, true)'), "归还走捕获段（后续 handler 仍可重新落焦点）");
  assert.ok(playerCore.includes('studyDeskForCleanup.removeEventListener("click", handleDeskClickFocusRelease, true)'), "归还监听清理对称");
  assert.ok(playerCore.includes("target.closest(PLAYER_HOVER_YIELD_SELECTOR)"), "滚轮悬停让位保留原全集");
  assert.ok(!/"input, select, textarea, button, a, \[contenteditable='true'\]"\s*;/.test(playerCore) || playerCore.includes("PLAYER_HOVER_YIELD_SELECTOR = \"input, select, textarea, button, a"),
    "悬停全集常量在位（wheel 与键盘守卫分离）");

  /* dropdown：触发钮方向键消费即 stopPropagation（防播放器 ↑↓ 双触发） */
  assert.ok(dropdown.includes("event.stopPropagation()"), "dropdown 触发钮已消费方向键 stopPropagation");
  assert.ok(/ArrowDown" \|\| key === "ArrowUp"/.test(dropdown), "触发钮 ↑ 与 ↓ 同样展开");

  /* ui.js：圈闭循环 preventScroll（焦点循环绝不跳滚） */
  assert.ok(ui.includes("last.focus({ preventScroll: true })") && ui.includes("first.focus({ preventScroll: true })"),
    "浮层圈闭循环 preventScroll 双向在位");

  /* 切页/回目录/登录框/任务卡/设置族焦点落点 preventScroll */
  assert.ok(shell.includes('$("workspace-main").focus({ preventScroll: true })'), "selectPage 焦点落点 preventScroll（既有）");
  assert.ok(shell.includes('$("login-student-id").focus({ preventScroll: true })'), "登录框首字段 preventScroll");
  assert.ok(study.includes('$("study-course-list").querySelector("button")?.focus({ preventScroll: true })'), "回课程目录首行 preventScroll");
  assert.ok(taskCards.includes("node.focus({ preventScroll: true })"), "任务卡重渲染焦点归还 preventScroll");
  const settingsFamily = familySource("settings");
  assert.ok(!/\.focus\(\);/.test(settingsFamily.replace(/focus\(\{ preventScroll: true \}\);/g, "")),
    "设置族程序化聚焦全部带 preventScroll（FOCUS-DRIFT-1 审计⑦收口）");
  assert.ok(settingsFamily.includes("focus({ preventScroll: true })"), "设置族 preventScroll 在位");

  /* 键盘可达三样之 Tab 序与 focus-visible 的静态面（行为钉在各自套件） */
  assert.ok(!/tabIndex = [1-9]/.test(study) && !/tabindex="[1-9]"/.test(readModuleSource("../frontend/modules/dropdown.js")),
    "免费面零正 tabindex（Tab 序 Sanity）");
  assert.ok(ui.includes('root?.dataset?.input === "key"') || ui.includes('dataset?.input === "key"'),
    "归还判定认 data-input 键盘模态（三态焦点政策 chokepoint 内一致）");
}

verifyGuardMatrix();
verifyDialogGateAndRelease();
verifyWiringPins();
console.log("frontend focus policy behavior passed");
