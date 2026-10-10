import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFileSync } from "node:fs";

/* 更新卡「本版说明」折叠区行为钉：
   1. index.html 结构 = details 折叠区 + 保留 update-notes 钉 id；
   2. settings.js 空态人话文案；renderUpdate 区块零 innerHTML（XSS 面冻结）；
   3. 真模块体（剥 import + 桩件）驱动 renderUpdate：有 notes → 逐字 textContent
      （HTML 片段作为纯文本保留 = textContent 防注入属性）；无 notes → 人话空态；
      null 快照 → 诚实「正在读取」+通用指引。 */

const settingsSource = familySource("settings");
const html = readFileSync(
  new URL("../frontend/index.html", import.meta.url), "utf8");

function testStructurePins() {
  assert.ok(html.includes('<details id="update-notes-details" class="update-notes-details">'),
    "collapsed notes region missing");
  assert.ok(html.includes("<summary>本版说明</summary>"), "notes summary missing");
  assert.ok(html.includes('<p id="update-notes" class="update-notes">本版说明暂未提供。</p>'),
    "static empty-state copy missing");
  assert.ok(settingsSource.includes('value.release_notes || "本版说明暂未提供。"'),
    "renderUpdate empty-state copy missing");
}

function testRenderBlockHasNoHtmlInjection() {
  const start = settingsSource.indexOf("function renderUpdate");
  const end = settingsSource.indexOf("async function updateAction");
  assert.ok(start > 0 && end > start, "renderUpdate block not found");
  const block = settingsSource.slice(start, end);
  assert.ok(!block.includes("innerHTML"), "renderUpdate must never inject HTML");
  assert.ok(block.includes('$("update-notes").textContent = value.release_notes'),
    "notes must be assigned via textContent");
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
  }
}

const byId = {};
for (const id of [
  "update-state", "update-error", "update-recovery", "update-facts-line",
  "update-last-check",
  "update-notes", "check-update", "download-update", "install-update",
]) {
  byId[id] = new FakeElement(id);
}

function loadRenderUpdate() {
  const body = settingsSource
    .replace(/^import [^\n]*\n/gm, "")
    /* ARCH-DEBT-1：门面 re-export 行（export {...} from）是导入面非模块体 */
    .replace(/^export\s*\{[^}]*\}\s*from\s*["'][^"']+["'];[ \t]*$/gm, "")
    .replace(/^export (?=async function|function|const|let|var|\{)/gm, "");
  const factory = new Function(
    "apiV3", "postV3", "applyThemePreference", "readThemePreference",
    "$", "clear", "evidenceDetails", "evidenceText", "operationId",
    "setBusy", "textElement", "toast", "removeLocalKeys",
    "installUpdateWidget", "installCourseData",
    /* UPDATE-UX-1：update-panel 家族成员新增 update-mac.js 三符号（本测钉
       Windows notes 面，桩恒 Windows/空态） */
    "macUpdateEntry", "macUpdateSnapshot", "onMacUpdateChange",
    `${body}\nreturn { renderUpdate };`,
  );
  const missing = [];
  const $stub = (id) => {
    if (!byId[id]) missing.push(id);
    return byId[id];
  };
  const noop = () => {};
  const exports = factory(
    noop, noop, noop, noop,
    $stub, noop, noop, noop, noop,
    noop, noop, noop, noop, noop, noop,
    () => false, () => ({ phase: "idle", version: "", currentVersion: "", error: "" }), noop,
  );
  assert.deepEqual(missing, [], `renderUpdate touched unknown ids: ${missing}`);
  return exports.renderUpdate;
}

function testNotesBehavior() {
  const renderUpdate = loadRenderUpdate();

  renderUpdate({
    state: "available", current_version: "0.0.9", channel: "stable",
    available_version: "0.1.0", package_size: 1115748,
    last_checked_at: 1790000000,
    release_notes: "修复了字幕断句 <script>alert(1)</script>",
    error_code: "", actions: ["check", "download"],
  });
  assert.equal(byId["update-notes"].textContent,
    "修复了字幕断句 <script>alert(1)</script>");

  renderUpdate({
    state: "idle", current_version: "0.1.0", channel: "stable",
    available_version: "", package_size: 0, last_checked_at: 0,
    release_notes: "", error_code: "", actions: ["check"],
  });
  assert.equal(byId["update-notes"].textContent, "本版说明暂未提供。");

  renderUpdate(null);
  assert.equal(byId["update-state"].textContent, "正在读取");
  assert.equal(byId["update-notes"].textContent, "本版说明暂未提供。");
  assert.equal(byId["update-error"].hidden, false);
}

function testCheckActionFeedback() {
  /* POLISH-1 F6（化身走查 FULL-CLIENT-INSPECT F6）：「检查更新」点击此前零
     可见反馈（「上次检查」停「尚未检查」、无 toast）。修后合同：点击即出
     in-flight toast；check 是同步动作，202 响应自带终态快照 → 完成即按闭集
     终态出结果句（available 带版本号）；按钮 busy 释放不变。 */
  const toasts = [];
  const posts = [];
  const body = settingsSource
    .replace(/^import [^\n]*\n/gm, "")
    .replace(/^export\s*\{[^}]*\}\s*from\s*["'][^"']+["'];[ \t]*$/gm, "")
    .replace(/^export (?=async function|function|const|let|var|\{)/gm, "");
  const factory = new Function(
    "apiV3", "postV3", "applyThemePreference", "readThemePreference",
    "$", "clear", "evidenceDetails", "evidenceText", "operationId",
    "setBusy", "textElement", "toast", "removeLocalKeys",
    "installUpdateWidget", "installCourseData",
    /* UPDATE-UX-1：update-panel 家族成员新增 update-mac.js 三符号 */
    "macUpdateEntry", "macUpdateSnapshot", "onMacUpdateChange",
    `${body}\nreturn { updateAction };`,
  );
  const noop = () => {};
  globalThis.window = { dispatchEvent() {} };
  globalThis.Event = class { constructor(type) { this.type = type; } };
  const { updateAction } = factory(
    noop,
    async (route, payload) => {
      posts.push([route, payload]);
      return { state: "available", available_version: "0.2.0" };
    },
    noop, noop, noop, noop, noop, noop, noop,
    (node, busy) => { node.disabled = Boolean(busy); },
    noop,
    (message, state) => { toasts.push([String(message), String(state)]); },
    noop, noop, noop,
    () => false, () => ({ phase: "idle", version: "", currentVersion: "", error: "" }), noop,
  );
  const button = new FakeElement("check-update");
  return updateAction("check", button).then(() => {
    assert.deepEqual(posts, [["client-update/actions", { action: "check", confirmed: false }]],
      "F6 请求合同不变（client-update/actions + action=check）");
    assert.equal(toasts.length, 2, "F6 in-flight + 结果两条反馈");
    assert.equal(toasts[0][0], "正在检查更新…", "F6 点击即反馈（修前红：零反馈）");
    assert.equal(toasts[1][0], "检查完成：发现新版本 0.2.0，可在下方下载。",
      "F6 终态结果句带版本号（available）");
    assert.equal(button.disabled, false, "F6 完成后按钮释放");
  });
}

testStructurePins();
testRenderBlockHasNoHtmlInjection();
testNotesBehavior();
await testCheckActionFeedback();
console.log("update-notes pins: 3/3 passed + F6 check feedback passed");
