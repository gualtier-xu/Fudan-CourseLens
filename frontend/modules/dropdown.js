/* 共享自绘下拉（全站弹窗精致化 甲2/甲3，Prompt 42）。
 * 原生 <select> 的 OS 级下拉是「粗制滥造」的重灾区：不吃主题、无过渡、
 * 无悬停/选中态、无键盘导航样式。本模块把 select 原地替换为
 * 触发钮 + 玻璃面板（role=listbox），**原生 select 保留在 DOM 内
 * （视觉隐藏）作为 value/change 事件唯一真相源**——全部既有监听、
 * 测试桩与程序化赋值零改动。
 * 面板每次展开时从 select 现行 options 全量同步（免 MutationObserver，
 * 假 DOM 兼容）；禁用态在展开时与显式 sync 点双保险。
 * 播放器内用 skin="media"（恒暗 stage token、向上展开）；页面用默认
 * page 皮（双主题 token、向下展开）。键盘：↑↓/Home/End 移动、Enter/Space
 * 选定、Esc/点外关闭、焦点全程留在触发钮（aria-activedescendant 模式）。
 * 过渡走 --motion 档（prefers-reduced-motion 由 accessibility.css 全局压平）。 */

const attached = new WeakMap();
let openDropdown = null; /* 全站同一时刻至多一个展开的下拉 */

function optionLabel(option) {
  return String(option?.textContent || option?.value || "").trim();
}

function buildOption(select, state, option, index) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "dropdown-option";
  item.setAttribute("role", "option");
  item.dataset.dropdownValue = String(option.value ?? "");
  item.textContent = optionLabel(option);
  if (option.disabled) item.disabled = true;
  if (String(option.value) === String(select.value)) {
    item.setAttribute("aria-selected", "true");
    item.classList.add("active");
  } else {
    item.setAttribute("aria-selected", "false");
  }
  item.addEventListener("click", () => {
    if (item.disabled) return;
    commitValue(select, state, String(option.value));
  });
  item.addEventListener("mousemove", () => setActive(state, index));
  return item;
}

function setActive(state, index) {
  if (!state.items.length) return;
  state.active = Math.min(Math.max(index, 0), state.items.length - 1);
  state.items.forEach((item, i) => item.classList.toggle("active", i === state.active));
  const current = state.items[state.active];
  if (current && state.panel.setAttribute) {
    state.panel.setAttribute("aria-activedescendant", current.id);
  }
}

function activeValue(state) {
  const item = state.items[state.active];
  return item ? String(item.dataset.dropdownValue || "") : null;
}

function commitValue(select, state, value) {
  closeDropdown(state);
  /* D5（焦点归还家族）：鼠标点选后焦点落在面板 option 钮上，面板随即 hidden
     → 焦点掉 BODY；键盘路径焦点从未离开触发钮。统一归还触发钮（幂等无害）。 */
  if (typeof state.trigger.focus === "function") state.trigger.focus({ preventScroll: true });
  if (String(select.value) !== value) {
    select.value = value;
    /* 真相源仍是 select：既有 change 监听原样生效 */
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  syncDropdown(select);
}

function closeDropdown(state) {
  if (openDropdown !== state) return;
  openDropdown = null;
  state.panel.dataset.open = "false";
  state.panel.hidden = true;
  state.trigger.setAttribute("aria-expanded", "false");
  state.trigger.removeAttribute("aria-activedescendant");
  document.removeEventListener("pointerdown", state.onDocPointer, true);
  document.removeEventListener("keydown", state.onDocKey, true);
}

function openUp(select, state) {
  if (openDropdown === state) {
    closeDropdown(state);
    return;
  }
  if (openDropdown) closeDropdown(openDropdown);
  if (select.disabled || select.options.length === 0) return;
  /* 展开即全量同步：动态填充/程序化赋值的最新状态免观察者直接生效 */
  state.items = [];
  clearNode(state.panel);
  const options = [...select.options].filter((option) => String(option.value) !== "");
  options.forEach((option, index) => {
    const item = buildOption(select, state, option, index);
    state.items.push(item);
    state.panel.append(item);
  });
  if (!state.items.length) return;
  const currentIndex = state.items.findIndex(
    (item) => item.getAttribute("aria-selected") === "true",
  );
  setActive(state, currentIndex >= 0 ? currentIndex : 0);
  state.trigger.setAttribute("aria-expanded", "true");
  state.panel.hidden = false;
  state.panel.dataset.open = "true";
  document.addEventListener("pointerdown", state.onDocPointer, true);
  document.addEventListener("keydown", state.onDocKey, true);
  openDropdown = state;
}

function clearNode(node) {
  if (typeof node.replaceChildren === "function") {
    node.replaceChildren();
    return;
  }
  if (Array.isArray(node.children)) {
    node.children.length = 0; /* 假 DOM：children 是普通数组 */
    return;
  }
  while (node.firstChild) node.removeChild(node.firstChild);
}

/* contains 的手写兼容版（假 DOM 无 contains/closest 属性选择器） */
function within(node, root) {
  let current = node;
  while (current) {
    if (current === root) return true;
    current = current.parent || current.parentElement || null;
  }
  return false;
}

function handleDocKey(select, state, event) {
  const key = String(event.key || "");
  if (key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeDropdown(state);
    return;
  }
  if (key === "ArrowDown" || key === "ArrowUp") {
    event.preventDefault();
    setActive(state, state.active + (key === "ArrowDown" ? 1 : -1));
    return;
  }
  if (key === "Home") {
    event.preventDefault();
    setActive(state, 0);
    return;
  }
  if (key === "End") {
    event.preventDefault();
    setActive(state, state.items.length - 1);
    return;
  }
  if (key === "Enter" || key === " ") {
    const value = activeValue(state);
    if (value !== null) {
      event.preventDefault();
      const option = [...select.options].find((item) => String(item.value) === value);
      if (!option || !option.disabled) commitValue(select, state, value);
    }
    return;
  }
  if (key === "Tab") closeDropdown(state);
}

/* P1-2（D14 无障碍 backlog）：读屏聚焦的是触发钮而非面板，aria-label 只给
   面板=全站下拉「听不出这是哪个字段」。触发钮可访问名取值顺序：select 的
   aria-label → 包裹 label 的字段文案（剔除 select 自身子树，假 DOM 兼容）
   → 空串（空名回落既有「当前值文本」行为，media 皮等无字段名下拉不受影响）。 */
function fieldLabelText(select) {
  const own = select.getAttribute("aria-label");
  if (own) return String(own).trim();
  let node = select.parentElement || select.parent || null;
  while (node) {
    if (String(node.tagName || "").toLowerCase() === "label") {
      const parts = [];
      for (const child of node.childNodes || node.children || []) {
        if (!child || child === select || within(select, child)) continue;
        const text = String(child.textContent || "").replace(/\s+/g, " ").trim();
        if (text) parts.push(text);
      }
      return parts.join(" ");
    }
    node = node.parentElement || node.parent || null;
  }
  return "";
}

/* 把 select 原地替换为触发钮+面板；同一 select 重复调用=幂等（返回既有句柄）。 */
export function attachDropdown(select, { skin = "page" } = {}) {
  if (!select || typeof select.addEventListener !== "function") return null;
  const existing = attached.get(select);
  if (existing) return existing;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = skin === "media" ? "dropdown-trigger dropdown-trigger-media" : "dropdown-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-label", fieldLabelText(select));
  const label = document.createElement("span");
  label.className = "dropdown-trigger-label";
  trigger.append(label);
  if (document.createElementNS) {
    const ns = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (ns && typeof ns.setAttribute === "function") {
      ns.setAttribute("viewBox", "0 0 24 24");
      ns.setAttribute("aria-hidden", "true");
      ns.setAttribute("class", "dropdown-chevron");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      if (path && typeof path.setAttribute === "function") {
        path.setAttribute("d", "m6 9 6 6 6-6");
        ns.append(path);
        trigger.append(ns);
      }
    }
  }
  const panel = document.createElement("div");
  panel.className = skin === "media" ? "dropdown-panel dropdown-panel-media" : "dropdown-panel";
  panel.setAttribute("role", "listbox");
  panel.setAttribute("aria-label", select.getAttribute("aria-label") || "");
  panel.hidden = true;
  panel.dataset.open = "false";

  /* 触发钮+面板同壳（position:relative）：面板 absolute 锚定壳而非
     远祖先，页面任意布局下展开位置都贴着触发钮。 */
  const wrap = document.createElement("span");
  wrap.className = skin === "media" ? "dropdown-wrap dropdown-wrap-media" : "dropdown-wrap";
  wrap.append(trigger, panel);

  const state = {
    trigger,
    panel,
    items: [],
    active: 0,
    onDocPointer: (event) => {
      const target = event.target;
      if (target === trigger || target === panel) return;
      if (within(target, panel) || within(target, trigger)) return;
      closeDropdown(state);
    },
    onDocKey: (event) => handleDocKey(select, state, event),
  };

  trigger.addEventListener("click", () => openUp(select, state));
  trigger.addEventListener("keydown", (event) => {
    /* 触发钮聚焦时 ↓/↑ 直接展开（桌面下拉肌肉记忆）。FOCUS-DRIFT-1：已消费的
       方向键 stopPropagation——焦点守卫细化后（button/a 仅激活键让位），方向键
       会同时透传页面级快捷键（播放器 ↑↓=音量步进），这里展开即消费，绝不允许
       双触发。 */
    const key = String(event.key || "");
    if ((key === "ArrowDown" || key === "ArrowUp") && openDropdown !== state) {
      event.preventDefault();
      event.stopPropagation();
      openUp(select, state);
    }
  });
  /* 真相源换值（键盘原生/程序化）→ 面板选中态与触发钮文案同步 */
  select.addEventListener("change", () => syncDropdown(select));

  const parent = select.parentElement || select.parent; /* 假 DOM 兼容：parent 即父节点 */
  if (parent && typeof parent.insertBefore === "function") {
    parent.insertBefore(wrap, select);
  } else if (parent && Array.isArray(parent.children)) {
    /* 假 DOM 兼容：无 insertBefore 时按 children 数组原地插入 */
    wrap.parent = parent;
    const index = parent.children.indexOf(select);
    parent.children.splice(index >= 0 ? index : parent.children.length, 0, wrap);
  } else {
    return null;
  }
  select.classList.add("dropdown-source");
  attached.set(select, state);
  syncDropdown(select);
  return state;
}

/* 依 select 现值刷新触发钮文案/禁用态/选中标记；未附着的 select 是无害空操作。 */
export function syncDropdown(select) {
  const state = attached.get(select);
  if (!state) return;
  const option = select.selectedOptions
    ? select.selectedOptions[0]
    : [...(select.options || [])].find((item) => String(item.value) === String(select.value));
  const text = option ? optionLabel(option) : String(select.value || "");
  const label = state.trigger.querySelector ? state.trigger.querySelector(".dropdown-trigger-label") : null;
  if (label) label.textContent = text;
  else state.trigger.textContent = text;
  if (select.disabled) state.trigger.setAttribute("aria-disabled", "true");
  else state.trigger.removeAttribute("aria-disabled");
}

/* 页面级初始化：index.html 里标注 data-dropdown 的 select 全部换装；
   skin 值=media 走播放器恒暗皮（倍速），其余走双主题页面皮。 */
export function initPageDropdowns(root = document) {
  if (!root || typeof root.querySelectorAll !== "function") return;
  const selects = root.querySelectorAll("select[data-dropdown]");
  for (const select of selects) {
    const skin = String(select.getAttribute("data-dropdown") || "") === "media" ? "media" : "page";
    attachDropdown(select, { skin });
  }
}
