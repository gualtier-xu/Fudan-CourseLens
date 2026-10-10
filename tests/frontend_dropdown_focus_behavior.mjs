import assert from "node:assert/strict";

/* 共享自绘下拉「焦点归还族」行为钉（夜14-R7 积压 28 清偿：WP/CUI 历史缺陷
   高发区此前仅字符串钉）。契约（dropdown.js D5 注记）：鼠标点选后面板隐藏、
   焦点归还触发钮（幂等无害）；键盘路径焦点全程留在触发钮；Esc/Tab/点外
   关闭绝不移动焦点；全站至多一个展开面；关闭即摘文档级监听（重开重挂）。 */

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
    this.focusCalls = [];
  }

  /* D5 被测面：focus({ preventScroll }) 调用录制器 */
  focus(options = {}) { this.focusCalls.push(options); }

  get parentElement() { return this.parent || null; }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
}

const docListeners = new Map();
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: undefined,
  addEventListener: (type, listener) => {
    if (!docListeners.has(type)) docListeners.set(type, new Set());
    docListeners.get(type).add(listener);
  },
  removeEventListener: (type, listener) => { docListeners.get(type)?.delete(listener); },
  activeElement: null,
};

const { attachDropdown } = await import("../frontend/modules/dropdown.js");

function makeSelect({ options, value }) {
  const select = new FakeElement("select", "focus-probe");
  select.value = value;
  select.setAttribute("aria-label", "焦点探针");
  select.options = options.map((option) => {
    const node = new FakeElement("option");
    node.value = option;
    node.textContent = `${option}×`;
    return node;
  });
  return select;
}

function fireDocKey(payload) {
  for (const listener of [...(docListeners.get("keydown") || [])]) {
    listener({ preventDefault() {}, stopPropagation() {}, ...payload });
  }
}
function fireDocPointer(target) {
  for (const listener of [...(docListeners.get("pointerdown") || [])]) listener({ target });
}

function setup() {
  const select = makeSelect({ options: ["0.5", "1", "2"], value: "1" });
  const parent = new FakeElement("div");
  parent.append(select);
  attachDropdown(select, {});
  const wrap = parent.children[0];
  const trigger = wrap.children.find((node) => String(node.className || "").includes("dropdown-trigger"));
  const panel = wrap.children.find((node) => String(node.className || "").includes("dropdown-panel"));
  return { select, trigger, panel };
}

/* 1) 鼠标点选 commit：面板收起 + 焦点归还触发钮（preventScroll）+ 关闭卫生活 */
{
  const { select, trigger, panel } = setup();
  let changes = 0;
  select.addEventListener("change", () => { changes += 1; });
  trigger.dispatchEvent(new Event("click"));
  assert.equal(panel.hidden, false, "前置：面板已展开");
  panel.children[2].dispatchEvent(new Event("click"));
  assert.equal(panel.hidden, true, "点选后面板收起");
  assert.equal(trigger.focusCalls.length, 1, "D5：焦点归还触发钮恰一次");
  assert.deepEqual(trigger.focusCalls[0], { preventScroll: true }, "归还走 preventScroll（不滚动页面）");
  assert.equal(trigger.getAttribute("aria-expanded"), "false", "aria-expanded 同步收起");
  assert.equal(trigger.getAttribute("aria-activedescendant"), null, "关闭即摘 activedescendant（读屏不悬空）");
  assert.equal(select.value, "2", "真相源写回");
  assert.equal(changes, 1, "change 恰一次");
  console.log("ok: 鼠标点选归还焦点");
}

/* 2) 键盘 Enter commit：焦点全程留在触发钮（键盘路径永不掉 BODY） */
{
  const { select, trigger, panel } = setup();
  trigger.dispatchEvent(new Event("click"));
  const activeBefore = trigger.focusCalls.length;
  fireDocKey({ key: "ArrowDown" });
  fireDocKey({ key: "Enter" });
  assert.equal(panel.hidden, true, "Enter 选定即收起");
  assert.equal(select.value, "2", "键盘选定写回真相源");
  assert.equal(trigger.focusCalls.length, activeBefore + 1, "commit 归还触发钮（幂等无害：焦点从未离开）");
  assert.equal(trigger.getAttribute("aria-expanded"), "false", "键盘关闭同步 aria");
  console.log("ok: 键盘 Enter 焦点留在触发钮");
}

/* 3) Esc / Tab / 点外关闭：绝不移动焦点 + 关闭摘文档监听（二次事件无副作用） */
{
  const { trigger, panel } = setup();
  trigger.dispatchEvent(new Event("click"));
  fireDocKey({ key: "Escape" });
  assert.equal(panel.hidden, true, "Esc 关闭");
  assert.equal(trigger.focusCalls.length, 0, "Esc 关闭不移动焦点（键盘焦点从未离开）");
  fireDocKey({ key: "Escape" }); /* 关闭后监听已摘：二次 Esc 零副作用 */
  assert.equal(panel.hidden, true, "二次 Esc 幂等（监听已摘）");

  trigger.dispatchEvent(new Event("click"));
  assert.equal(panel.hidden, false, "重开正常（Esc 后监听重挂）");
  fireDocKey({ key: "Tab" });
  assert.equal(panel.hidden, true, "Tab 关闭（焦点交还给 Tab 序，模块不拦截）");
  assert.equal(trigger.focusCalls.length, 0, "Tab 关闭同样不移动焦点");

  trigger.dispatchEvent(new Event("click"));
  fireDocPointer({ parent: null }); /* 面板外 pointerdown */
  assert.equal(panel.hidden, true, "点外关闭");
  assert.equal(trigger.focusCalls.length, 0, "点外关闭不移动焦点（诚实：不抢焦点）");
  console.log("ok: Esc/Tab/点外关闭焦点零位移+监听卫生");
}

/* 4) 全站单开：A 面板展开时开 B → A 自动收起（互斥不叠加焦点归还） */
{
  const a = setup();
  const b = setup();
  a.trigger.dispatchEvent(new Event("click"));
  b.trigger.dispatchEvent(new Event("click"));
  assert.equal(a.panel.hidden, true, "开 B 即收 A（全站至多一个展开面）");
  assert.equal(b.panel.hidden, false, "B 保持展开");
  assert.equal(a.trigger.focusCalls.length, 0, "互斥收起不触发 A 的焦点归还（非用户 commit）");
  fireDocKey({ key: "Escape" });
  assert.equal(b.panel.hidden, true, "Esc 收当前展开面");
  assert.equal(a.panel.hidden, true, "A 保持收起");
  console.log("ok: 全站单开互斥");
}

console.log("frontend dropdown focus behavior passed");
