import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFile } from "node:fs/promises";
import { evidenceDetails, recoveryActionLabel } from "../frontend/modules/ui.js";

/* 合同 docs/catalog-session-recovery-handoff.md §6（梯子1修复后的呈现层锁定）：
 * 有界真实探针（2026-09-07）证明 WebVPN 会话无 _token Cookie 也能原生访问目录
 * （梯子1成立）。修复后 catalog_bearer_missing 只在"webvpn 路由失败 + direct
 * 无 bearer"出现——会话并非失效（失效有独立的 catalog_session_expired 裁决），
 * 因此该 code 必须呈现为诚实的可重试文案，且不得再引导"重新认证"循环。 */

const degraded = evidenceDetails({ state: "degraded", code: "catalog_bearer_missing" });
assert.equal(degraded.title, "课程授权信息暂无法读取");
assert.match(degraded.impact, /重试/);
assert.doesNotMatch(degraded.impact, /重新认证|重新登录/);

// 与真正会话过期（合法的重登场景）呈现不再同形。
const expired = evidenceDetails({ state: "degraded", code: "catalog_session_expired" });
assert.match(expired.impact, /重新认证/);
assert.notEqual(degraded.title, expired.title);
assert.notEqual(degraded.impact, expired.impact);

// 修复后 bearer_missing 的后端动作是 refresh-catalog（重试），标签非"重新认证"。
assert.equal(recoveryActionLabel("refresh-catalog", { code: "catalog_bearer_missing" }), "重试刷新");

// 会话过期仍保留重登路径（动作标签不变）。
assert.equal(recoveryActionLabel("login", { code: "catalog_session_expired" }), "重新认证");

const study = familySource("study");
const onboarding = await readFile(new URL("../frontend/modules/onboarding.js", import.meta.url), "utf8");
assert.match(study, /CATALOG_REFRESH_MAX_RETRIES = 20/);
assert.match(study, /初始请求后每秒至多重试/);
assert.match(study, /value\.refreshing \|\| value\.state === "checking"/);
assert.match(study, /courselens:catalog-settled/);
assert.match(study, /courselens:catalog-timeout/);
assert.match(onboarding, /courselens:catalog-settled/);
assert.match(onboarding, /courselens:catalog-timeout/);

/* P2-2 回归锁：超时收口必须复位重试计数。否则超时后任何 auth fingerprint 变化触发的
 * load()（重新登录、心跳）首个 checking 响应会立即再次降态、零重试。 */
const timedOutBody = study.split("const catalogRefreshTimedOut = () => {", 2)[1]?.split("\n  };", 1)[0] || "";
assert.ok(timedOutBody.includes("catalogRefreshRetries = 0"), "超时收口必须复位 catalogRefreshRetries，让后续 load 重新获得完整重试窗口");

console.log("frontend_catalog_login_loop_behavior: all assertions passed (ladder-1 semantics)");
