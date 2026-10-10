import assert from "node:assert/strict";

/* DATA-MIGRATION 前端行为 harness（D12 数据主权 P0，2026-10-07）。
   覆盖：数据管理页两入口接线 / 导出密码双校验与下载链接受信形态（只认后端
   回执披露的 token URL，绝不本地拼接） / 导出完成人话状态 / 导入无文件与
   密码门 / 上传进度回调 / 导入完成「重录三件套」清单 + rebasing 数量 /
   错误码人话化（未知英文后端文本不上屏）。 */

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
}

function matches(node, selector) {
  for (const part of String(selector).split(",").map((item) => item.trim())) {
    if (!part) continue;
    if (part.startsWith(".")) {
      if (String(node.className || "").split(/\s+/).includes(part.slice(1))) return true;
      continue;
    }
    if (part.startsWith("[") && node.dataset) {
      return node.dataset[String(part.slice(1, -1))] !== undefined;
    }
    if (node.tagName === part.toUpperCase()) return true;
  }
  return false;
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
    this.files = null;
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
    if (!Object.prototype.hasOwnProperty.call(event, "currentTarget")) {
      Object.defineProperty(event, "currentTarget", { value: this, configurable: true });
    }
    for (const listener of [...(this._listeners.get(event.type) || [])]) listener.call(this, event);
    return true;
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
    if (String(name) === "id") this.id = String(value);
  }
  getAttribute(name) { return this.attributes.get(String(name)) ?? null; }
  append(...nodes) { for (const node of nodes) { this.parent = this; this.children.push(node); } }
  appendChild(node) { this.append(node); return node; }
  removeChild(node) { this.children = this.children.filter((item) => item !== node); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click", { bubbles: true })); }
  focus() {}
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; this.dispatchEvent(new Event("close")); } }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => { for (const child of node.children || []) { if (matches(child, selector)) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const registry = [];
const byId = {};
const register = (tag, id) => { byId[id] = new FakeElement(tag, id); registry.push(byId[id]); return byId[id]; };

/* 数据页搬家区真实结构镜像（index.html 的 id 全集） */
for (const [tag, id] of [
  ["button", "data-migration-export"], ["button", "data-migration-import"],
  ["p", "data-migration-status"], ["p", "data-migration-error"],
  ["dialog", "data-migration-export-dialog"], ["form", "data-migration-export-form"],
  ["input", "data-migration-export-password"], ["input", "data-migration-export-password-confirm"],
  ["p", "data-migration-export-error"], ["button", "data-migration-export-cancel"],
  ["button", "data-migration-export-confirm"],
  ["dialog", "data-migration-import-dialog"], ["form", "data-migration-import-form"],
  ["input", "data-migration-import-file"], ["input", "data-migration-import-password"],
  ["p", "data-migration-import-status"], ["p", "data-migration-import-error"],
  ["button", "data-migration-import-cancel"], ["button", "data-migration-import-confirm"],
]) {
  register(tag, id);
}

const body = new FakeElement("body");
globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag),
  body,
  documentElement: new FakeElement("html"),
  activeElement: body,
};
globalThis.window = globalThis;

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/* fetch 桩：动作面 v3 信封 */
const calls = [];
let actionsResponder = () => ({ status: 200, payload: { schema: "courselens.api.v3", data: {} } });
globalThis.fetch = async (path, options = {}) => {
  calls.push({ path, options, body: options.body ? JSON.parse(options.body) : null });
  const outcome = actionsResponder();
  return {
    ok: outcome.status >= 200 && outcome.status < 300,
    status: outcome.status,
    headers: { get: () => "application/json" },
    json: async () => outcome.payload,
  };
};

/* XMLHttpRequest 桩：上传面（进度回调 + load 响应） */
class FakeXHR {
  constructor() {
    FakeXHR.instances.push(this);
    this.upload = { addEventListener: (type, listener) => { this._upload = listener; } };
    this._listeners = new Map();
    this.responseText = "";
    this.status = 0;
  }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader() {}
  addEventListener(type, listener) { this._listeners.set(type, listener); }
  send(bodyArg) {
    this.sentBody = bodyArg;
    this._upload?.({ lengthComputable: true, loaded: 40, total: 100 });
    this.status = FakeXHR.nextStatus;
    this.responseText = FakeXHR.nextText;
    this._listeners.get("load")?.();
  }
}
FakeXHR.instances = [];
FakeXHR.nextStatus = 201;
FakeXHR.nextText = JSON.stringify({
  schema: "courselens.api.v3",
  data: { schema: "courselens.data-migration-upload.v1", package_id: "a".repeat(32), bytes: 100, sha256: "f".repeat(64) },
});
globalThis.XMLHttpRequest = FakeXHR;

const { installDataMigration } = await import("../frontend/modules/settings/data-migration.js");
const cleanup = installDataMigration();

const EXPORT_OK_RECEIPT = {
  schema: "courselens.data-migration-action-result.v1",
  action: "export", operation_id: "op-x", status: "accepted",
  result: {
    schema: "courselens.data-migration-export.v1",
    filename: "courselens-data-20261007-080000-abc123.clmig",
    bytes: 12345678,
    download: { url: "/api/v3/data-migration/file?token=tok123", filename: "courselens-data-20261007-080000-abc123.clmig", bytes: 12345678 },
  },
};
const IMPORT_OK_RECEIPT = {
  schema: "courselens.data-migration-action-result.v1",
  action: "import", operation_id: "op-y", status: "accepted",
  result: {
    schema: "courselens.data-migration-import.v1",
    rebased_document_paths: 3,
    credentials_reentry: [
      { id: "fudan_account" }, { id: "github_authorization" }, { id: "deepseek_key" },
    ],
  },
};

/* 1) 入口钉：两按钮接进对话框 */
byId["data-migration-export"].click();
assert.equal(byId["data-migration-export-dialog"].open, true, "导出按钮开导出对话框");
byId["data-migration-export-cancel"].click();
assert.equal(byId["data-migration-export-dialog"].open, false, "取消关对话框");
byId["data-migration-import"].click();
assert.equal(byId["data-migration-import-dialog"].open, true, "导入按钮开导入对话框");
byId["data-migration-import-cancel"].click();

/* 2) 导出密码门：短密码与两次不一致都被本地拦下（零请求） */
byId["data-migration-export"].click();
byId["data-migration-export-password"].value = "short";
byId["data-migration-export-password-confirm"].value = "short";
byId["data-migration-export-form"].dispatchEvent(new Event("submit"));
await settle();
assert.ok(byId["data-migration-export-error"].textContent.includes("8"), "短密码本地拦截");
assert.equal(calls.length, 0, "短密码零请求");

byId["data-migration-export-password"].value = "migrate-2026";
byId["data-migration-export-password-confirm"].value = "migrate-other";
byId["data-migration-export-form"].dispatchEvent(new Event("submit"));
await settle();
assert.ok(byId["data-migration-export-error"].textContent.includes("不一样"), "两次不一致本地拦截");
assert.equal(calls.length, 0, "不一致零请求");

/* 3) 导出成功：请求闭集 + 下载链接只取后端 token URL + 人话状态 */
byId["data-migration-export-password-confirm"].value = "migrate-2026";
calls.length = 0;
let createdLinks = [];
const originalCreate = globalThis.document.createElement;
globalThis.document.createElement = (tag) => {
  const node = originalCreate(tag);
  if (tag === "a") createdLinks.push(node);
  return node;
};
actionsResponder = () => ({ status: 200, payload: { schema: "courselens.api.v3", data: EXPORT_OK_RECEIPT } });
byId["data-migration-export-form"].dispatchEvent(new Event("submit"));
await settle();
assert.equal(calls.length, 1, "导出恰好一次请求");
assert.equal(calls[0].path, "/api/v3/data-migration/actions", "导出动作路由");
assert.equal(calls[0].body.action, "export", "导出动作闭集");
assert.ok(calls[0].body.operation_id.startsWith("data-migration-export:"), "导出 operation_id 前缀");
assert.equal(calls[0].body.password, "migrate-2026", "密码随请求（HTTPS 本地面）");
const link = createdLinks.at(-1);
assert.equal(
  link?.attributes.get("href"),
  "/api/v3/data-migration/file?token=tok123",
  "下载链接=后端回执 token URL",
);
assert.ok(
  String(link?.attributes.get("download") || "").endsWith(".clmig"),
  "下载文件名来自后端回执",
);
assert.ok(
  byId["data-migration-status"].textContent.includes("搬家包已生成")
  && byId["data-migration-status"].textContent.includes("U 盘"),
  "导出完成人话状态（含带走指引）",
);
globalThis.document.createElement = originalCreate;

/* 4) 导出失败：闭集码映射人话，原始英文不上屏 */
byId["data-migration-export"].click();
byId["data-migration-export-password"].value = "migrate-2026";
byId["data-migration-export-password-confirm"].value = "migrate-2026";
calls.length = 0;
actionsResponder = () => ({
  status: 400,
  payload: { error_code: "MIGRATION_E_BUSY", error: "Data migration was not accepted" },
});
byId["data-migration-export-form"].dispatchEvent(new Event("submit"));
await settle();
assert.ok(byId["data-migration-error"].textContent.length > 0, "失败态有文案");
assert.ok(
  !byId["data-migration-error"].textContent.includes("Data migration was not accepted"),
  "原始英文不上屏",
);
byId["data-migration-export-cancel"].click();

/* 4b) D-20261009-01 同族：409 拒绝回执透传——rejected+blockers 信封给具体
   阻塞指引，不再裸「HTTP 409」。 */
byId["data-migration-export"].click();
byId["data-migration-export-password"].value = "migrate-2026";
byId["data-migration-export-password-confirm"].value = "migrate-2026";
calls.length = 0;
actionsResponder = () => ({
  status: 409,
  payload: {
    schema: "courselens.api.v3",
    data: {
      schema: "courselens.data-migration-action-result.v1",
      action: "export", operation_id: "op-409", status: "rejected",
      blockers: [{ code: "active_task", count: 1 }],
    },
  },
});
byId["data-migration-export-form"].dispatchEvent(new Event("submit"));
await settle();
assert.ok(
  byId["data-migration-error"].textContent.includes("搬家包操作没有执行"),
  "409 拒绝回执给具体语义引导",
);
assert.ok(
  byId["data-migration-error"].textContent.includes("有正在进行的生成任务"),
  "闭集阻塞码人话在场",
);
assert.ok(
  !byId["data-migration-error"].textContent.includes("HTTP 409"),
  "裸 HTTP 状态码不再上屏",
);
byId["data-migration-export-cancel"].click();

/* 5) 导入门：无文件本地拦截 */
byId["data-migration-import"].click();
byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
await settle();
assert.ok(byId["data-migration-import-error"].textContent.includes("搬家包文件"), "无文件本地拦截");
assert.equal(calls.length, 1, "无文件零动作请求（仅此前的导出调用）");

/* 6) 导入成功：XHR 上传进度 + 动作请求闭集 + 重录三件套与 rebasing 数量 */
byId["data-migration-import-file"].files = [{ name: "courselens-data.clmig", size: 100 }];
byId["data-migration-import-password"].value = "migrate-2026";
calls.length = 0;
actionsResponder = () => ({ status: 200, payload: { schema: "courselens.api.v3", data: IMPORT_OK_RECEIPT } });
byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
await settle();
const uploadXhr = FakeXHR.instances.at(-1);
assert.equal(uploadXhr.method, "POST", "上传为 POST");
assert.equal(uploadXhr.url, "/api/v3/data-migration/package", "上传路由闭集");
assert.ok(uploadXhr.sentBody && typeof uploadXhr.sentBody === "object", "上传体=文件对象本身（不读进内存整包）");
const importCall = calls.find((item) => item.body?.action === "import");
assert.ok(importCall, "导入动作已发");
assert.equal(importCall.body.package_id, "a".repeat(32), "package_id 来自上传回执");
assert.equal(importCall.body.password, "migrate-2026", "导入密码随请求");
const importStatus = byId["data-migration-status"].textContent;
assert.ok(importStatus.includes("重新登录复旦账号"), "重录清单①复旦账号");
assert.ok(importStatus.includes("GitHub 授权"), "重录清单②GitHub 授权");
assert.ok(importStatus.includes("DeepSeek Key"), "重录清单③DeepSeek Key");
assert.ok(importStatus.includes("3 条资料路径"), "rebasing 数量如实回显");
assert.equal(byId["data-migration-import-dialog"].open, false, "导入成功关对话框");

/* 7) 导入密码错误：后端闭集码人话化，对话框保持打开可重试 */
byId["data-migration-import"].click();
byId["data-migration-import-file"].files = [{ name: "courselens-data.clmig", size: 100 }];
byId["data-migration-import-password"].value = "wrong-pass-1";
FakeXHR.nextStatus = 400;
FakeXHR.nextText = JSON.stringify({ error_code: "MIGRATION_E_PASSWORD_INVALID", error: "Data migration was not accepted" });
actionsResponder = () => ({
  status: 400,
  payload: { error_code: "MIGRATION_E_PASSWORD_INVALID", error: "Data migration was not accepted" },
});
byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
await settle();
assert.equal(byId["data-migration-import-dialog"].open, true, "失败时对话框保持打开");
assert.ok(
  byId["data-migration-import-error"].textContent.length > 0
  && !byId["data-migration-import-error"].textContent.includes("Data migration was not accepted"),
  "导入失败人话文案（无原始英文）",
);
byId["data-migration-import-cancel"].click();

/* 8) WAIT-UX-1 J3/J3+：导入服务段动态进度——按包体档位预计（闭集单源
   wait-expectations.js：344KB 实测锚 P50 1.1s；GB 级无样本只给量级措辞）
   + 已用秒数步进；完成收口回到既有终态语义 */
{
  const baseFetch = globalThis.fetch;
  let releaseImportAction = null;
  globalThis.fetch = async (path, options) => {
    if (String(path).includes("data-migration/actions")) {
      return new Promise((resolve) => { releaseImportAction = resolve; });
    }
    return baseFetch(path, options);
  };
  /* 放行器补齐 fetch 响应形状（与上方 fetch 桩同构），释放挂起的动作请求 */
  const releaseImport = (outcome) => releaseImportAction({
    ok: outcome.status >= 200 && outcome.status < 300,
    status: outcome.status,
    headers: { get: () => "application/json" },
    json: async () => outcome.payload,
  });
  const settleMs = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* 场景 7 把 XHR 桩留在 400 态：先复位上传回执（scenario 6 同款成功形态） */
  FakeXHR.nextStatus = 201;
  FakeXHR.nextText = JSON.stringify({
    schema: "courselens.api.v3",
    data: { schema: "courselens.data-migration-upload.v1", package_id: "a".repeat(32), bytes: 100, sha256: "f".repeat(64) },
  });

  byId["data-migration-import"].click();
  byId["data-migration-import-file"].files = [{ name: "small.clmig", size: 100 }];
  byId["data-migration-import-password"].value = "migrate-2026";
  byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
  await settle();
  const serviceLine = byId["data-migration-import-status"];
  assert.ok(!serviceLine.hidden && serviceLine.textContent.includes("校验、解密并安家"),
    "服务段状态行在位");
  assert.ok(serviceLine.textContent.includes("这个包不大，通常几秒内完成"),
    `小包档位预计在位：${serviceLine.textContent}`);
  await settleMs(1100);
  assert.match(serviceLine.textContent, /已用 [12] 秒/, "服务段逐秒步进（动态进度）");
  releaseImport({ status: 200, payload: { schema: "courselens.api.v3", data: IMPORT_OK_RECEIPT } });
  await settle();
  assert.equal(byId["data-migration-import-dialog"].open, false, "完成关闭对话框（终态语义不变）");

  byId["data-migration-import"].click();
  byId["data-migration-import-file"].files = [{ name: "mid.clmig", size: 100 * 1024 * 1024 }];
  byId["data-migration-import-password"].value = "migrate-2026";
  byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
  await settle();
  assert.ok(byId["data-migration-import-status"].textContent.includes("预计约一两分钟，包越大越久"),
    "中包档位预计在位（带上浮措辞）");
  releaseImport({ status: 200, payload: { schema: "courselens.api.v3", data: IMPORT_OK_RECEIPT } });
  await settle();

  byId["data-migration-import"].click();
  byId["data-migration-import-file"].files = [{ name: "huge.clmig", size: 700 * 1024 * 1024 }];
  byId["data-migration-import-password"].value = "migrate-2026";
  byId["data-migration-import-form"].dispatchEvent(new Event("submit"));
  await settle();
  assert.ok(byId["data-migration-import-status"].textContent.includes("大包预计要几分钟"),
    "大包档位只给量级措辞（无样本不写死数）");
  releaseImport({ status: 200, payload: { schema: "courselens.api.v3", data: IMPORT_OK_RECEIPT } });
  await settle();
  globalThis.fetch = baseFetch;
}

cleanup();
console.log("frontend data migration behavior passed");
