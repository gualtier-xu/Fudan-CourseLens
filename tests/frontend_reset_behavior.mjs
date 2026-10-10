import assert from "node:assert/strict";

/* CLIENT-RESET-1 行为 harness：设置危险区重置单入口。
   冻结合同 = product-reset-feature-contract-20260916 §1/§5/§6。覆盖：
   弹窗打开即默认态（两勾选项不勾） / typed 门（恰为「重置」才启用） /
   取消复位不提交 / 请求体闭集（confirm_typed 字符串 + 两布尔） /
   接受回执呈现（含保留语义） / reset_blocked 闭集阻塞态 /
   GitHub 失败与未知错误不渲染 raw 异常 / dialog 关闭复位。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
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
    this._hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.value = "";
    this.type = "";
    this._listeners = new Map();
  }
  get hidden() { return this._hidden === true; }
  set hidden(value) {
    this._hidden = value === true;
    if (this._hidden) this.attributes.set("hidden", "true");
    else this.attributes.delete("hidden");
  }
  addEventListener(type, listener) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this._listeners.get(type)?.delete(listener); }
  dispatchEvent(event) {
    if (!Object.prototype.hasOwnProperty.call(event, "target")) {
      Object.defineProperty(event, "target", { value: this, configurable: true });
    }
    for (const listener of [...(this._listeners.get(event.type) || [])]) listener.call(this, event);
    return true;
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; this.dispatchEvent(new Event("close")); } }
}

const registry = [];
const byId = {};
const register = (tag, id) => { byId[id] = new FakeElement(tag, id); registry.push(byId[id]); return byId[id]; };

/* 设置页重置面真实结构镜像（index.html 的 id 闭集） */
for (const [tag, id] of [
  ["button", "client-reset-open"], ["p", "client-reset-result"], ["p", "client-reset-error"],
  ["dialog", "client-reset-dialog"], ["form", "client-reset-form"],
  ["h2", "client-reset-dialog-title"], ["p", "client-reset-dialog-hint"],
  ["input", "client-reset-delete-derived"], ["input", "client-reset-delete-repos"],
  ["input", "client-reset-confirm-input"],
  ["p", "client-reset-dialog-status"], ["p", "client-reset-dialog-error"],
  ["button", "client-reset-cancel"], ["button", "client-reset-confirm"],
  ["div", "toast-region"],
]) {
  register(tag, id);
}
byId["client-reset-result"].hidden = true;
byId["client-reset-error"].hidden = true;
byId["client-reset-dialog-status"].hidden = true;
byId["client-reset-dialog-error"].hidden = true;
byId["client-reset-confirm"].disabled = true;
byId["client-reset-open"].textContent = "重置应用…";
byId["client-reset-cancel"].textContent = "取消";
byId["client-reset-confirm"].textContent = "确认重置";

globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => {
    const node = new FakeElement(tag, `created-${registry.length + 1}`);
    registry.push(node);
    return node;
  },
  querySelectorAll: () => [],
  querySelector: () => null,
  activeElement: null,
  documentElement: { dataset: {} },
  addEventListener() {},
  removeEventListener() {},
};

const windowTarget = new EventTarget();
windowTarget.setTimeout = (fn) => 0;
windowTarget.clearTimeout = () => {};
windowTarget.setInterval = () => 0;
windowTarget.clearInterval = () => {};
globalThis.window = windowTarget;
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
};
globalThis.CSS = { escape: (value) => String(value) };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
windowTarget.matchMedia = globalThis.matchMedia;
const removedLocalKeys = [];
globalThis.localStorage = { setItem() {}, getItem: () => null, removeItem: (key) => removedLocalKeys.push(key) };
Object.defineProperty(globalThis, "navigator", { value: { onLine: true }, configurable: true });

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 4; i += 1) await nextTurn(); };

/* ---- 网络桩 ---- */
let receipt = null;
let errorResponse = null; /* { error_code, status } */
const posts = [];

const ok = (data) => new Response(JSON.stringify({ schema: "courselens.api.v3", data }), { status: 200, headers: { "Content-Type": "application/json" } });
const fail = (code, status) => new Response(JSON.stringify({ error: "synthetic", error_code: code, retriable: status === 409 }), { status, headers: { "Content-Type": "application/json" } });

globalThis.fetch = async (path, options = {}) => {
  const route = String(path);
  if (route === "/api/v3/client-reset/actions") {
    posts.push(JSON.parse(options.body || "{}"));
    if (errorResponse) return fail(errorResponse.error_code, errorResponse.status);
    return ok(receipt);
  }
  throw new Error(`unexpected synthetic route: ${route}`);
};

const { installClientReset } = await import("../frontend/modules/settings.js");
const cleanup = installClientReset();
await settle();

const dialog = byId["client-reset-dialog"];
const input = byId["client-reset-confirm-input"];
const confirmButton = byId["client-reset-confirm"];
const derived = byId["client-reset-delete-derived"];
const repos = byId["client-reset-delete-repos"];

/* 1) 打开即默认态：两勾选项不勾、输入清空、确认禁用 */
dialog.open = false;
byId["client-reset-open"].click();
await settle();
assert.equal(dialog.open, true, "入口打开弹窗");
assert.equal(derived.checked, false, "派生产物勾选项默认不勾");
assert.equal(repos.checked, false, "删仓勾选项默认不勾");
assert.equal(input.value, "", "确认输入初始为空");
assert.equal(confirmButton.disabled, true, "确认按钮初始禁用");

/* 2) typed 门：恰为「重置」才启用 */
input.value = "确认";
input.dispatchEvent(new Event("input"));
assert.equal(confirmButton.disabled, true, "错误确认语不启用");
input.value = "重置 ";
input.dispatchEvent(new Event("input"));
assert.equal(confirmButton.disabled, true, "带空白确认语不启用（精确匹配）");
input.value = "重置";
input.dispatchEvent(new Event("input"));
assert.equal(confirmButton.disabled, false, "恰为「重置」启用");

/* 3) 取消复位：关闭 + 清态 + 不提交 */
byId["client-reset-cancel"].click();
await settle();
assert.equal(dialog.open, false, "取消关闭弹窗");
assert.equal(posts.length, 0, "取消不提交");
assert.equal(derived.checked, false, "取消后勾选复位");
assert.equal(confirmButton.disabled, true, "取消后确认禁用");

/* 4) 接受回执：请求体闭集 + 结果呈现（保留语义） */
byId["client-reset-open"].click();
await settle();
input.value = "重置";
input.dispatchEvent(new Event("input"));
derived.checked = true;
repos.checked = true;
receipt = {
  schema: "courselens.client-reset-action-result.v1",
  action: "reset", operation_id: "op-1", status: "accepted",
  result: {
    deleted: { accounts: 2, documents: 1 },
    bytes_freed: 4096,
    repos_deleted: ["student/worker", "student/mailbox"],
    databases_rebuilt: ["state.db", "learning.db"],
  },
};
confirmButton.click();
await settle();
assert.equal(posts.length, 1, "确认提交一次");
assert.equal(posts[0].action, "reset", "闭集动作 reset");
assert.equal(posts[0].confirm_typed, "重置", "typed 确认语字符串随请求");
assert.equal(posts[0].delete_derived, true, "派生删除开关随请求");
assert.equal(posts[0].delete_github_repos, true, "删仓开关随请求");
assert.ok(String(posts[0].operation_id).startsWith("client-reset:"), "幂等 operation_id");
assert.equal(dialog.open, false, "接受后关闭弹窗");
assert.ok(byId["client-reset-result"].textContent.includes("重置已完成"), "完成文案呈现");
assert.ok(byId["client-reset-result"].textContent.includes("仓库 2 个"), "删除仓库计数呈现");
assert.ok(!byId["client-reset-result"].textContent.includes("student/worker"), "仓库名单不回显到界面");
assert.equal(byId["client-reset-dialog-error"].hidden, true, "无错误态");
assert.deepEqual(removedLocalKeys, ["courselens.theme.v2", "courselens.ui-font.v1", "courselens.course-order.v1", "courselens.catalog-term.v1", "courselens:subtitle-style", "courselens.playback-rate.v1", "courselens:insight", "courselens.last-lecture.v1"], "U5③+P56-U1+A11Y-IMPL-4+PLAYER-OPT-1+FIRST-LOGIN-UX-2：重置成功清浏览器侧闭集键（主题 v2/界面字号 v1/课程排序/学期筛选/字幕样式/每课倍速/洞察开关/继续学习记忆）");

/* 4b) 不可删类降级（T3）：回执带 repos_manual_deletion → 本地重置照常完成文案
   + 受信手删链接（settings 形态正则）+ 删后验证说明；仓库名仍不回显 */
byId["client-reset-open"].click();
await settle();
input.value = "重置";
input.dispatchEvent(new Event("input"));
repos.checked = true;
receipt = {
  schema: "courselens.client-reset-action-result.v1",
  action: "reset", operation_id: "op-2", status: "accepted",
  result: {
    deleted: { accounts: 2 },
    bytes_freed: 2048,
    repos_deleted: ["student/worker"],
    repos_manual_deletion: [{
      repo: "student/mailbox",
      settings_url: "https://github.com/student/mailbox/settings",
      reason: "app_token_cannot_delete",
    }],
  },
};
confirmButton.click();
await settle();
assert.equal(dialog.open, false, "降级回执同样关闭弹窗");
const manualResult = byId["client-reset-result"];
/* FakeElement 的 textContent 不聚合 children：主行 + 追加节点手动聚合 */
const manualText = [manualResult.textContent, ...manualResult.children.map((node) => String(node.textContent || ""))].join("\n");
assert.ok(manualResult.textContent.includes("重置已完成"), "降级时本地重置照常完成的文案呈现");
assert.ok(manualResult.textContent.includes("待手动删除仓库 1 个"), "手删计数呈现");
assert.ok(manualText.includes("手动删除"), "手删说明呈现");
assert.ok(manualText.includes("确认仓库已删除"), "删后验证说明呈现");
const manualAnchors = manualResult.children.filter((node) => node.tagName === "A");
assert.equal(manualAnchors.length, 1, "恰一个受信手删链接");
assert.equal(manualAnchors[0].getAttribute("href"), "https://github.com/student/mailbox/settings", "链接为后端回执的受信 settings 形态");
assert.ok(String(manualAnchors[0].getAttribute("rel") || "").includes("noopener"), "链接带 noopener");
assert.ok(manualAnchors[0].textContent.includes("删除专属仓库"), "链接标签用序号");
assert.equal(manualText.includes("student/mailbox"), false, "手删回执的仓库名同样不回显");
assert.equal(manualText.includes("synthetic"), false, "不渲染原始后端文本");

/* 4c) 不可信手删形态被闭集过滤：非法 settings_url 不渲染为链接 */
byId["client-reset-open"].click();
await settle();
input.value = "重置";
input.dispatchEvent(new Event("input"));
/* 真实 DOM 中 textContent 赋值会清空 children；FakeElement 不会，手动复位模拟 */
byId["client-reset-result"].children = [];
receipt = {
  schema: "courselens.client-reset-action-result.v1",
  action: "reset", operation_id: "op-3", status: "accepted",
  result: {
    repos_manual_deletion: [{
      repo: "student/mailbox",
      settings_url: "https://evil.example/delete",
      reason: "app_token_cannot_delete",
    }],
  },
};
confirmButton.click();
await settle();
assert.equal(byId["client-reset-result"].children.filter((node) => node.tagName === "A").length, 0, "非受信形态绝不渲染为链接");
receipt = null;

/* 5) 阻塞态：reset_blocked 409 → 闭集文案、无 raw 码 */
receipt = null;
errorResponse = { error_code: "reset_blocked", status: 409 };
byId["client-reset-open"].click();
await settle();
input.value = "重置";
input.dispatchEvent(new Event("input"));
const postsBeforeBlocked = posts.length;
confirmButton.click();
await settle();
assert.equal(posts.length, postsBeforeBlocked + 1, "阻塞态仍发出请求");
assert.equal(dialog.open, true, "阻塞态弹窗保持打开");
const blockedText = byId["client-reset-dialog-error"].textContent;
assert.ok(blockedText.includes("重置未执行"), "阻塞闭集文案呈现");
assert.ok(!blockedText.includes("reset_blocked"), "不渲染原始错误码");
errorResponse = null;

/* 6) GitHub 删除失败 → 专属指引文案 */
errorResponse = { error_code: "github_unreachable", status: 502 };
confirmButton.click();
await settle();
assert.ok(byId["client-reset-dialog-error"].textContent.includes("GitHub 仓库删除未完成"), "GitHub 失败闭集文案");
errorResponse = null;

/* 7) 未知失败 → 通用闭集文案，绝不渲染异常文本 */
errorResponse = { error_code: "http_error", status: 500 };
confirmButton.click();
await settle();
assert.ok(byId["client-reset-dialog-error"].textContent.includes("重置暂未完成"), "通用失败文案");
assert.equal(byId["client-reset-dialog-error"].textContent.includes("Error"), false, "不渲染 raw 异常");
errorResponse = null;

/* 7b) N5FE-P9 回归：未登录 401 → 对话框保持打开 + 「先登录」人话指引，
   绝不静默吞掉（636c329 重置可达性在未登录态的补丁） */
errorResponse = { error_code: "fudan_login_required", status: 401 };
confirmButton.click();
await settle();
errorResponse = null;
assert.equal(dialog.open, true, "401 时对话框保持打开（不静默关停）");
assert.ok(!byId["client-reset-dialog-error"].hidden, "错误行可见");
assert.match(byId["client-reset-dialog-error"].textContent, /先登录/, "401 人话指引呈现");
assert.equal(byId["client-reset-dialog-error"].textContent.includes("Error"), false, "不渲染 raw 异常");

/* 8) 关闭即复位：重新打开恢复默认 */
dialog.close();
await settle();
byId["client-reset-open"].click();
await settle();
assert.equal(input.value, "", "重开确认输入为空");
assert.equal(confirmButton.disabled, true, "重开确认禁用");
assert.equal(derived.checked, false, "重开勾选复位");

cleanup();
console.log("frontend reset behavior passed");
