import assert from "node:assert/strict";

/* 共享自绘下拉行为测试（Prompt 42 甲2/甲3）。桩件法与既有 mjs 同源：
   最小 FakeElement + 可检视 document 监听器；原生 select 保持 value/change
   真相源是本模块的第一契约。覆盖：换装/展开同步/选定回写 change/Esc 与
   点外关闭/键盘导航/禁用门/initPageDropdowns 双皮分发。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  toggle(value, enabled) {
    if (enabled === undefined) { this.values.has(value) ? this.values.delete(value) : this.values.add(value); }
    else if (enabled) this.values.add(value);
    else this.values.delete(value);
    return this.values.has(value);
  }
  contains(value) { return this.values.has(value); }
}

class FakeElement extends EventTarget {
  constructor(tagName = "div", id = "") {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
  }

  get firstChild() { return this.children[0] || null; }
  get parentElement() { return this.parent || null; }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
}

function makeSelect({ options, value, disabled = false, label = "测试选择" }) {
  const select = new FakeElement("select", "probe-select");
  select.disabled = disabled;
  select.value = value;
  select.setAttribute("aria-label", label);
  select.options = options.map((option) => {
    const node = new FakeElement("option");
    node.value = option;
    node.textContent = `${option}×`;
    return node;
  });
  return select;
}

/* 可检视 document：记录 addEventListener 供测试手动触发文档级事件 */
const docListeners = new Map();
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: undefined, /* 假 DOM 无 NS：dropdown 须静默跳过 chevron svg */
  addEventListener: (type, listener) => {
    if (!docListeners.has(type)) docListeners.set(type, new Set());
    docListeners.get(type).add(listener);
  },
  removeEventListener: (type, listener) => { docListeners.get(type)?.delete(listener); },
  activeElement: null,
};

const { attachDropdown, syncDropdown, initPageDropdowns } = await import("../frontend/modules/dropdown.js");

function fireDoc(type, target) {
  for (const listener of [...(docListeners.get(type) || [])]) listener({ target });
}

function findPanel(wrap) {
  return wrap.children.find((node) => String(node.className || "").includes("dropdown-panel"));
}
function findTrigger(wrap) {
  return wrap.children.find((node) => String(node.className || "").includes("dropdown-trigger"));
}

/* 1) 换装：触发钮+面板生成、select 加 dropdown-source、文案=现行值 */
{
  const select = makeSelect({ options: ["0.5", "1", "2"], value: "1" });
  const parent = new FakeElement("div");
  parent.append(select);
  const state = attachDropdown(select, {});
  assert.ok(state, "attach 返回句柄");
  assert.ok(select.classList.contains("dropdown-source"), "原生 select 视觉隐藏");
  const wrap = parent.children.find((node) => String(node.className || "") === "dropdown-wrap");
  assert.ok(wrap, "触发钮+面板同壳插入");
  const trigger = findTrigger(wrap);
  assert.equal(trigger.getAttribute("aria-haspopup"), "listbox");
  assert.equal(trigger.textContent, "1×", "触发钮文案=现行值");
  syncDropdown(select);
  assert.equal(findTrigger(wrap).textContent, "1×", "sync 幂等");
  console.log("ok: 换装结构/真相源隐藏/文案同步");
}

/* 2) 展开同步+选定回写 change+面板收起 */
{
  const select = makeSelect({ options: ["0.5", "1", "2"], value: "1" });
  const parent = new FakeElement("div");
  parent.append(select);
  attachDropdown(select, {});
  const wrap = parent.children[0];
  const trigger = findTrigger(wrap);
  const panel = findPanel(wrap);
  let changes = 0;
  select.addEventListener("change", () => { changes += 1; });

  trigger.dispatchEvent(new Event("click"));
  assert.equal(panel.hidden, false, "展开可见");
  assert.equal(panel.dataset.open, "true");
  assert.equal(trigger.getAttribute("aria-expanded"), "true");
  assert.equal(panel.children.length, 3, "九档快捷行=option 全量（此例 3 档）");
  assert.equal(panel.children[1].getAttribute("aria-selected"), "true", "现行值带选中标记");

  panel.children[2].dispatchEvent(new Event("click"));
  assert.equal(select.value, "2", "选定写回 select（真相源）");
  assert.equal(changes, 1, "change 恰一次（既有监听契约）");
  assert.equal(panel.hidden, true, "选定即收起");
  assert.equal(trigger.getAttribute("aria-expanded"), "false");
  assert.equal(trigger.textContent, "2×", "触发钮文案跟随");
  console.log("ok: 展开同步/选定回写 change/收起");
}

/* 3) Esc 与点外关闭；重开时选中标记跟随程序化赋值 */
{
  const select = makeSelect({ options: ["1", "1.25"], value: "1" });
  const parent = new FakeElement("div");
  parent.append(select);
  attachDropdown(select, {});
  const wrap = parent.children[0];
  const trigger = findTrigger(wrap);
  const panel = findPanel(wrap);

  trigger.dispatchEvent(new Event("click"));
  const escHandler = [...(docListeners.get("keydown") || [])][0];
  escHandler({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.equal(panel.hidden, true, "Esc 关闭");

  select.value = "1.25"; /* 外部程序化赋值（无 change） */
  trigger.dispatchEvent(new Event("click"));
  assert.equal(panel.children[1].getAttribute("aria-selected"), "true", "重开即同步最新值");

  fireDoc("pointerdown", { parent: null });
  assert.equal(panel.hidden, true, "点外关闭");
  console.log("ok: Esc/点外关闭/重开全量同步");
}

/* 4) 禁用门：disabled select 点开无效 */
{
  const select = makeSelect({ options: ["1"], value: "1", disabled: true });
  const parent = new FakeElement("div");
  parent.append(select);
  attachDropdown(select, {});
  const trigger = findTrigger(parent.children[0]);
  const panel = findPanel(parent.children[0]);
  trigger.dispatchEvent(new Event("click"));
  assert.equal(panel.hidden, true, "禁用不展开");
  assert.equal(trigger.getAttribute("aria-disabled"), "true", "触发钮带禁用态");
  console.log("ok: 禁用门");
}

/* 5) 键盘：↓ 移动、Enter 选定 */
{
  const select = makeSelect({ options: ["1", "1.25", "1.5"], value: "1" });
  const parent = new FakeElement("div");
  parent.append(select);
  attachDropdown(select, {});
  const wrap = parent.children[0];
  findTrigger(wrap).dispatchEvent(new Event("click"));
  const panel = findPanel(wrap);
  const keyHandler = [...(docListeners.get("keydown") || [])][0];
  keyHandler({ key: "ArrowDown", preventDefault() {}, stopPropagation() {} });
  assert.equal(panel.children[1].classList.contains("active"), true, "↓ 移动高亮");
  keyHandler({ key: "Enter", preventDefault() {}, stopPropagation() {} });
  assert.equal(select.value, "1.25", "Enter 选定高亮项");
  assert.equal(findPanel(wrap).hidden, true, "选定收起");
  console.log("ok: 键盘导航与选定");
}

/* 6) initPageDropdowns：data-dropdown 分发 page 皮、="media" 分发 media 皮 */
{
  const pageSelect = makeSelect({ options: ["a", "b"], value: "a" });
  pageSelect.setAttribute("data-dropdown", "");
  const mediaSelect = makeSelect({ options: ["1", "2"], value: "1" });
  mediaSelect.setAttribute("data-dropdown", "media");
  const native = makeSelect({ options: ["x"], value: "x" });
  const root = new FakeElement("div");
  root.append(pageSelect, mediaSelect, native);
  root.querySelectorAll = () => [pageSelect, mediaSelect];
  initPageDropdowns(root);
  /* wrap 是 select 的前一个兄弟（同在真实父容器里） */
  const wrapOf = (select) => {
    const siblings = select.parentElement.children;
    return siblings[siblings.indexOf(select) - 1] || null;
  };
  assert.equal(String(findPanel(wrapOf(pageSelect)).className).includes("dropdown-panel-media"), false, "默认页面皮");
  assert.equal(String(findPanel(wrapOf(mediaSelect)).className).includes("dropdown-panel-media"), true, "media 皮");
  assert.equal(String(native.className).includes("dropdown-source"), false, "未标注的 select 保持原生");
  console.log("ok: initPageDropdowns 双皮分发/白名单边界");
}

/* 7) P1-2（D14 无障碍 backlog）：触发钮可访问名=字段名——读屏聚焦触发钮而非
   面板，命名缺口是全站性的。取值顺序：select aria-label → 包裹 label 文案
   （剔除 select 自身子树）→ 空串（回落现行值文本既有行为）。 */
{
  /* 7a) select 自带 aria-label：触发钮与面板同取该名 */
  const labeled = makeSelect({ options: ["a", "b"], value: "a", label: "课程目录学期" });
  const parentA = new FakeElement("div");
  parentA.append(labeled);
  attachDropdown(labeled, {});
  assert.equal(findTrigger(parentA.children[0]).getAttribute("aria-label"), "课程目录学期", "触发钮可访问名取 select 的 aria-label");
  assert.equal(findPanel(parentA.children[0]).getAttribute("aria-label"), "课程目录学期", "面板既有 aria-label 保留");

  /* 7b) 无 aria-label：包裹 label 的字段文案兜底（真实形态=设置页「模式」「自动登录账号」） */
  const bare = new FakeElement("select", "bare-select");
  bare.value = "auto";
  const autoOption = new FakeElement("option");
  autoOption.value = "auto";
  autoOption.textContent = "自动×";
  bare.options = [autoOption];
  const fieldLabel = new FakeElement("label");
  const fieldText = new FakeElement("span");
  fieldText.textContent = "模式";
  fieldLabel.append(fieldText);
  fieldLabel.append(bare);
  attachDropdown(bare, {});
  const bareWrap = fieldLabel.children.find((node) => String(node.className || "") === "dropdown-wrap");
  assert.ok(bareWrap, "包裹 label 内同样完成换装");
  assert.equal(findTrigger(bareWrap).getAttribute("aria-label"), "模式", "无 aria-label 时触发钮名=包裹 label 字段文案");
  assert.equal(String(findTrigger(bareWrap).getAttribute("aria-label")).includes("自动"), false, "select 自身子树文案不混入字段名");

  /* 7c) 两无（无 aria-label、无包裹 label）：回落空串，可访问名回到既有现行值文本 */
  const nameless = new FakeElement("select", "nameless-select");
  nameless.value = "x";
  const xOption = new FakeElement("option");
  xOption.value = "x";
  xOption.textContent = "X×";
  nameless.options = [xOption];
  const namelessParent = new FakeElement("div");
  namelessParent.append(nameless);
  attachDropdown(nameless, {});
  assert.equal(findTrigger(namelessParent.children[0]).getAttribute("aria-label"), "", "两无下拉回落空串（不发明字段名）");
  console.log("ok: 触发钮可访问名三态（aria-label/包裹label/空串）");
}

console.log("POPUP-POLISH: all green");
