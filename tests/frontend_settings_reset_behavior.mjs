import assert from "node:assert/strict";
import {
  assembleFamily, elementRegistry, missingRecorder, macUpdateStubs,
} from "./frontend_exec_harness.mjs";
/* WAIT-UX-1：重置进行中两档预计文案以真实闭集值注入（断言即测用户所见字符串） */
import { RESET_RUNNING_LOCAL, RESET_RUNNING_WITH_REPOS } from "../frontend/modules/wait-expectations.js";

/* 设置页客户端重置流真执行钉（夜14-R7 TOP10 草案 D7；共享装配骨架首个
   消费者，复用 update_notes 已证形态）：
   1. 两步确认门：未输入「重置」时确认按钮禁用，点击不产生任何 POST；
   2. 确认后 POST client-reset/actions 载荷形状（action/operation_id/
      confirm_typed/delete_derived/delete_github_repos）；
   3. 受信回执（schema+status）才清浏览器侧三键（闭集逐键断言）；
   4. 拒绝回执（reset_blocked）落人话错误行，不吞码、不清键。
   每个子测试独立 registry + 独立装配，防跨子测试监听器串台。 */

const RESET_RESULT_SCHEMA = "courselens.client-reset-action-result.v1";

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 5));
}

function freshContext(postV3) {
  const registry = elementRegistry([
    "client-reset-dialog", "client-reset-confirm-input", "client-reset-confirm",
    "client-reset-result", "client-reset-error", "client-reset-dialog-status",
    "client-reset-dialog-error", "client-reset-open", "client-reset-cancel",
    "client-reset-delete-derived", "client-reset-delete-repos",
  ]);
  const { missing, $stub } = missingRecorder();
  const $ = $stub(registry.lookup);
  const removedKeyBatches = [];
  const removeLocalKeys = (keys) => removedKeyBatches.push([...keys]);
  const setBusy = () => {};
  const operationId = (scope) => `${scope}-op-0001`;
  const textElement = (tag, text) => ({ tag, text });
  const factory = assembleFamily("settings", [
    "apiV3", "postV3", "applyThemePreference", "readThemePreference",
    "$", "clear", "evidenceDetails", "evidenceText", "operationId",
    "setBusy", "textElement", "toast", "removeLocalKeys",
    "installUpdateWidget", "installCourseData",
    /* a3ddf5e（UPDATE-UX-1）起 update-panel 装配期顶层注册 mac 检查道监听——
       桩族退订桩（UPDATE-STUB-FIX-1 收敛到 harness macUpdateStubs） */
    "onMacUpdateChange",
    /* WAIT-UX-1：client-reset 进行中两档预计文案（真实闭集值注入） */
    "RESET_RUNNING_LOCAL", "RESET_RUNNING_WITH_REPOS",
  ], ["installClientReset"]);
  const noop = () => {};
  const exports = factory(
    noop, postV3, noop, noop,
    $, noop, noop, noop, operationId,
    setBusy, textElement, noop, removeLocalKeys, noop, noop,
    macUpdateStubs().onMacUpdateChange,
    RESET_RUNNING_LOCAL, RESET_RUNNING_WITH_REPOS,
  );
  return { registry, missing, removedKeyBatches, install: exports.installClientReset };
}

async function testConfirmGateBlocksBlindPost() {
  const postV3 = async () => {
    throw new Error("disabled confirm must never POST");
  };
  const { registry, missing, install } = freshContext(postV3);
  const dialog = registry.byId.get("client-reset-dialog");
  const input = registry.byId.get("client-reset-confirm-input");
  const confirm = registry.byId.get("client-reset-confirm");

  const teardown = install();
  registry.byId.get("client-reset-open").dispatch("click");
  assert.equal(dialog.open, true, "open must raise the dialog");
  assert.equal(input.value, "", "open must clear the confirm input");
  assert.equal(confirm.disabled, true, "open must disable confirm");
  assert.equal(registry.byId.get("client-reset-delete-derived").checked, false);
  assert.equal(registry.byId.get("client-reset-delete-repos").checked, false);

  await Promise.all(confirm.dispatch("click"));
  input.value = "reset";
  input.dispatch("input");
  assert.equal(confirm.disabled, true, "wrong word must keep confirm disabled");

  input.value = "重置";
  input.dispatch("input");
  assert.equal(confirm.disabled, false, "exact word must enable confirm");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
  teardown();
}

async function testAcceptedReceiptShapeAndLocalKeyClear() {
  const posts = [];
  const postV3 = async (path, payload) => {
    posts.push({ path, payload });
    return {
      schema: RESET_RESULT_SCHEMA, status: "accepted",
      result: { deleted: { documents: 3, subtitles: 5 }, repos_deleted: ["owner/repo"] },
    };
  };
  const { registry, missing, removedKeyBatches, install } = freshContext(postV3);
  const dialog = registry.byId.get("client-reset-dialog");
  const input = registry.byId.get("client-reset-confirm-input");
  const confirm = registry.byId.get("client-reset-confirm");
  const resultLine = registry.byId.get("client-reset-result");

  install();
  registry.byId.get("client-reset-open").dispatch("click");
  registry.byId.get("client-reset-delete-derived").checked = true;
  registry.byId.get("client-reset-delete-repos").checked = true;
  input.value = "重置";
  input.dispatch("input");
  await Promise.all(confirm.dispatch("click"));
  await flush();

  assert.equal(posts.length, 1, "accepted confirm must POST exactly once");
  assert.equal(posts[0].path, "client-reset/actions");
  assert.equal(posts[0].payload.action, "reset");
  assert.equal(posts[0].payload.confirm_typed, "重置");
  assert.equal(posts[0].payload.delete_derived, true);
  assert.equal(posts[0].payload.delete_github_repos, true);
  assert.ok(String(posts[0].payload.operation_id).startsWith("client-reset-"),
    "operation_id must stay in the client-reset namespace");
  assert.equal(dialog.open, false, "accepted reset closes the dialog");
  assert.deepEqual(
    removedKeyBatches,
    [[
      "courselens.theme.v2",
      "courselens.ui-font.v1",
      "courselens.course-order.v1",
      "courselens.catalog-term.v1",
      /* PLAYER-OPT-1（20261008 夜）：player-core 本机偏好三键补入重置闭集
         （字幕样式/每课倍速/洞察开关）——「客户端重置」后不再残留。 */
      "courselens:subtitle-style",
      "courselens.playback-rate.v1",
      "courselens:insight",
      /* FIRST-LOGIN-UX-2（U1 继续学习）：store last-lecture 记忆键入重置闭集。 */
      "courselens.last-lecture.v1",
    ]],
    "accepted receipt must clear exactly the closed browser key set (A11Y-IMPL-4 added ui-font; PLAYER-OPT-1 added player-core preference keys; FIRST-LOGIN-UX-2 added continue-learning memory key)",
  );
  assert.equal(resultLine.hidden, false, "result line must surface the receipt");
  assert.ok(resultLine.textContent.includes("重置已完成"),
    "result line must speak the done copy");
  assert.ok(resultLine.textContent.includes("文档 3"),
    "summary must speak the deleted counts");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

async function testBlockedRejectionSpeaksHumanCopy() {
  const postV3 = async () => {
    const error = new Error("reset blocked");
    error.code = "reset_blocked";
    error.status = 400;
    throw error;
  };
  const { registry, missing, removedKeyBatches, install } = freshContext(postV3);
  const input = registry.byId.get("client-reset-confirm-input");
  const confirm = registry.byId.get("client-reset-confirm");
  const dialogError = registry.byId.get("client-reset-dialog-error");

  install();
  registry.byId.get("client-reset-open").dispatch("click");
  input.value = "重置";
  input.dispatch("input");
  await Promise.all(confirm.dispatch("click"));
  await flush();

  assert.equal(dialogError.hidden, false, "blocked reset must surface the error line");
  assert.ok(dialogError.textContent.includes("重置未执行"),
    "blocked copy must say the reset did not run");
  assert.deepEqual(removedKeyBatches, [],
    "rejected reset must never clear browser keys");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

/* WAIT-UX-1 缺 ETA K1+：进行中状态行按是否勾选删仓给两档诚实预计——本机段
   实测秒级（「通常几秒内清完」），含 GitHub 网络段给宽区间不写死数
   （文案闭集单源=wait-expectations.js，注入的是真实值）。 */
async function testRunningCopyMatchesRepoScope() {
  const posts = [];
  const postV3 = async (path, payload) => {
    posts.push({ path, payload });
    return { schema: RESET_RESULT_SCHEMA, status: "accepted", result: {} };
  };
  const { registry, missing, install } = freshContext(postV3);
  const input = registry.byId.get("client-reset-confirm-input");
  const confirm = registry.byId.get("client-reset-confirm");
  const dialogStatus = registry.byId.get("client-reset-dialog-status");

  install();
  registry.byId.get("client-reset-open").dispatch("click");
  input.value = "重置";
  input.dispatch("input");
  /* 派发后先同步断言在途态，再 await——handleConfirm 的 finally 会收状态行 */
  const firstReset = confirm.dispatch("click");
  assert.equal(dialogStatus.hidden, false, "running status must be visible during reset");
  assert.equal(dialogStatus.textContent, RESET_RUNNING_LOCAL,
    "local-only reset must not promise a network segment");
  await Promise.all(firstReset);
  await flush();

  registry.byId.get("client-reset-open").dispatch("click");
  registry.byId.get("client-reset-delete-repos").checked = true;
  input.value = "重置";
  input.dispatch("input");
  const secondReset = confirm.dispatch("click");
  assert.equal(dialogStatus.textContent, RESET_RUNNING_WITH_REPOS,
    "repo deletion reset must surface the wide network window");
  await Promise.all(secondReset);
  await flush();
  assert.equal(posts.length, 2, "both resets must have POSTed");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

await testConfirmGateBlocksBlindPost();
await testAcceptedReceiptShapeAndLocalKeyClear();
await testBlockedRejectionSpeaksHumanCopy();
await testRunningCopyMatchesRepoScope();
console.log("frontend settings reset behavior passed");
