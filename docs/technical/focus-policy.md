# 焦点政策（Focus Policy）

> 一页纸规则，防新代码复发。来源：用户报障 D-20261007-03（点击任意按钮后播放页快捷键全灭）升格的全项目焦点纪律工程（FOCUS-DRIFT-1，2026-10-07）。
> 实现唯一入口：`frontend/modules/ui.js` 焦点政策 chokepoint（`SHORTCUT_FIELD_SELECTOR` / `pageShortcutBlockedByTarget` / `hasOpenDialog` / `releasePointerActivatedFocus`）。

## 三类聚焦来源，三条规则

| 来源 | 判别 | 规则 |
|------|------|------|
| **鼠标/触摸**（指针） | `<html data-input="pointer">`（`shell.js` 输入模态追踪器） | 点击激活 button/a 后**焦点即刻归还页面级**（`releasePointerActivatedFocus`，播放器在学习桌捕获段接线）。指针点击不留焦点驻留——驻留的焦点会拦住页面级快捷键。滑杆/输入框例外：拖动与输入需要焦点，释放仍走各自既有路径（滑杆 pointerup 归还 + 3s 无交互自动归还）。 |
| **键盘**（Tab/方向） | `<html data-input="key">` | **焦点零抢夺**：绝不自动 blur（Tab 位置与 `:focus-visible` 环是键盘用户的生命线）。快捷键可达性由守卫语义保证（见下），不靠移走焦点。 |
| **程序化聚焦** | `element.focus()` 调用 | 一律 `{ preventScroll: true }`（浮层圈闭循环、切页落点、重渲染归还、登录框首字段）。聚焦目标必须是仍在文档中的节点（归还锚离场时回落触发钮，见 ui.js closeOverlay）。 |

## 快捷键让位语义（页面级快捷键 vs 聚焦元素）

页面级快捷键（播放器空格/方向/J/K/L/M/F/C/X/`<`/`>`/`?` 等）按下时按「焦点在哪」三级让位，统一走 `pageShortcutBlockedByTarget`：

1. **字段与自带方向语义面**（`input, select, textarea, [contenteditable='true'], [role='menu'], [role='listbox'], [role='tab'], [role='option']`）→ **整块让位**。它们要么在编辑文本，要么方向键/Home/End 已是自己的导航语义（roving tab、listbox、菜单），快捷键叠加就是双触发。
2. **button/a（点击激活面）**→ **仅激活键（Space/Enter）让位**给原生激活——键盘用户 Tab 到按钮后必须仍能激活它（键盘可达硬门）；其余按键（方向/J/K/L/字母）**透传页面快捷键**。点击任意按钮后快捷键即刻存活，不再被焦点驻留拦死。自绘下拉触发钮这类「方向键自有语义」的特例，由组件自己消费键并 `stopPropagation`（dropdown.js）。
3. **任一模态 `dialog[open]` 期间**→ **一律让位**（`hasOpenDialog`），绝不隔模态操控背景播放器；唯 `?` 豁免——它是键位帮助弹窗自身的管理键。

## 键盘可达三样硬门（任何焦点改动不得回退）

1. **Tab 可达**：全部交互元素 Tab 序可达；零正 `tabindex`；隐藏用 `hidden` 属性（不可聚焦），不用裸 `aria-hidden` 藏可聚焦元素；roving tabindex 容器（资料区 tab、复习视图 tab）活动页 `tabindex=0` 其余 `-1`，方向键循环 + Tab 出口。
2. **:focus-visible 环**：键盘来源聚焦必有环（`accessibility.css` 三态政策：`data-input="pointer"` 时指针态环归零，Tab 恒环）。
3. **激活键**：聚焦 button/a 时 Space/Enter 永远是原生激活，页面快捷键绝不抢消费。

## 新代码检查单

- 要加页面级快捷键？守卫必须走 `pageShortcutBlockedByTarget`，禁止自造 EDITABLE 选择器散点。
- 要在点击后管理焦点？先判模态（`data-input`），指针态才归还，键盘态不抢。
- 要 `focus()`？带 `preventScroll: true`，且目标在文档中。
- 要开模态弹层？优先 `openOverlay`（焦点圈闭+Esc 栈+触发钮归还+主区 inert 一体）；原生 `<dialog>` 依赖浏览器自动归还，关闭后焦点落 body 是合法态（页面级快捷键待命）。
- 改了焦点行为？`tests/frontend_focus_policy_behavior.mjs`（chokepoint 矩阵+接线钉）与 `tests/frontend_playback_recovery_behavior.mjs`（播放页复现剧本重放）两处钉必须绿。
