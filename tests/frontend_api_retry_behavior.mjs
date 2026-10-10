import assert from "node:assert/strict";

/* NIGHT2-W20 GET-only 传输层重试行为测试：裸网关瞬态响应（无应用 error_code
   信封）按 full jitter 指数退避至多重试 2 次；应用信封的 5xx 是终态零重试；
   POST 永不重试；Retry-After 取大者并 5s 封顶。sleep 用立即触发的桩并记录
   延迟值做 clamp 断言。 */

const realFetch = globalThis.fetch;
const realSetTimeout = globalThis.setTimeout;
const recordedDelays = [];
let scripted = [];

globalThis.setTimeout = (fn, ms) => {
  recordedDelays.push(Number(ms) || 0);
  fn();
  return 0;
};

function jsonResponse(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function rawResponse(status, headers = {}) {
  return new Response("<html>bad gateway</html>", {
    status,
    headers: { "Content-Type": "text/html", ...headers },
  });
}

const { request, ApiError } = await import("../frontend/modules/api.js");

/* 1) GET 裸 503 → 重试一次 → 200 成功；恰 2 次调用、1 段抖动延迟 */
{
  recordedDelays.length = 0;
  scripted = [rawResponse(503), jsonResponse(200, { schema: "x", data: { ok: 1 } })];
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return scripted.shift();
  };
  const payload = await request("/api/v3/automation");
  assert.equal(payload.data.ok, 1, "重试后拿到正常载荷");
  assert.equal(calls, 2, "裸 503 恰重试一次");
  assert.equal(recordedDelays.length, 1, "一段抖动延迟");
  assert.ok(recordedDelays[0] >= 0 && recordedDelays[0] < 500, `首拍抖动 <500ms：${recordedDelays[0]}`);
}

/* 2) 应用信封的 503 是终态：零重试、错误码原样透出 */
{
  scripted = [jsonResponse(503, { error: "down", error_code: "task_not_found" })];
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return scripted.shift();
  };
  await assert.rejects(
    () => request("/api/v3/tasks"),
    (error) => error instanceof ApiError && error.code === "task_not_found",
    "应用信封 503 透出闭集码",
  );
  assert.equal(calls, 1, "应用信封 5xx 不在传输层重试");
}

/* 3) 裸 503 ×3 → 至多重试 2 次 → 以最后一次响应抛 http_error */
{
  recordedDelays.length = 0;
  scripted = [rawResponse(503), rawResponse(503), rawResponse(503)];
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return scripted.shift();
  };
  await assert.rejects(
    () => request("/api/v3/automation"),
    (error) => error instanceof ApiError && error.code === "http_error" && error.status === 503,
  );
  assert.equal(calls, 3, "重试上限=2（共 3 次调用）");
  assert.equal(recordedDelays.length, 2, "两段退避延迟");
  assert.ok(recordedDelays[0] < 500 && recordedDelays[1] < 1000, `抖动封顶对：${recordedDelays}`);
}

/* 4) POST 裸 503 → 零重试 */
{
  scripted = [rawResponse(503)];
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return scripted.shift();
  };
  await assert.rejects(() => request("/api/v3/automation/actions", { method: "POST", body: "{}" }));
  assert.equal(calls, 1, "POST 不重试");
}

/* 5) Retry-After 取大者且 5s 封顶 */
{
  scripted = [rawResponse(503, { "Retry-After": "3" }), jsonResponse(200, { ok: 1 })];
  globalThis.fetch = async () => scripted.shift();
  recordedDelays.length = 0;
  await request("/api/v3/automation");
  assert.ok(
    recordedDelays[0] >= 3000 && recordedDelays[0] <= 5000,
    `Retry-After 3s 生效且 5s 封顶：${recordedDelays[0]}`,
  );
}

/* 6) 网络层 TypeError → 重试后成功 */
{
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    if (calls === 1) throw new TypeError("fetch failed");
    return jsonResponse(200, { ok: 1 });
  };
  const payload = await request("/api/v3/automation");
  assert.equal(payload.ok, 1, "网络层瞬态失败重试自愈");
  assert.equal(calls, 2, "网络层失败恰重试一次");
}

/* 7) N10B-5：网络层失败重试耗尽 → 人话 ApiError（码表 network_unavailable），
   不再把裸 "Failed to fetch" 抛给学生；原始信息折进 detail。 */
{
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(
    request("/api/v3/automation"),
    (error) => error instanceof ApiError
      && error.code === "network_unavailable"
      && error.message === "网络暂不可用"
      && error.retriable === true
      && error.detail === "Failed to fetch",
    "网络层终态失败转人话 ApiError",
  );
}

/* 8) N10B-5：AbortError（调用方主动取消）保持原样上抛，不被改写成网络错误 */
{
  const abortError = new Error("The operation was aborted.");
  abortError.name = "AbortError";
  globalThis.fetch = async () => {
    throw abortError;
  };
  await assert.rejects(
    request("/api/v3/automation"),
    (error) => error === abortError,
    "AbortError 原样上抛",
  );
}

globalThis.fetch = realFetch;
globalThis.setTimeout = realSetTimeout;
console.log("frontend api retry behavior passed");
