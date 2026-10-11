/* BACKEND-DEATH-1②：会话心跳调度器行为钉测。
   拍点由 Dedicated Worker 产生（Node 无 Worker 时 installShell 走页面定时器回退），
   调度器是纯状态机——定时器与心跳全部注入，worker 以桩替身驱动。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const { HEARTBEAT_INTERVAL_MS, createHeartbeatScheduler } = await import("../frontend/modules/shell.js");

/* 工厂：收集调度器注册的页面定时器，手工驱动拍点。 */
const makeHarness = () => {
  const heartbeats = [];
  const intervals = new Map();
  let intervalSeq = 0;
  const scheduler = createHeartbeatScheduler({
    heartbeat: () => { heartbeats.push(heartbeats.length + 1); },
    schedule: (fn, ms) => {
      intervalSeq += 1;
      intervals.set(intervalSeq, { fn, ms: Number(ms) });
      return intervalSeq;
    },
    cancel: (id) => intervals.delete(id),
  });
  const tickIntervals = async (ms) => {
    for (const [, timer] of [...intervals.entries()].filter(([, t]) => t.ms === ms)) {
      timer.fn();
      await Promise.resolve();
    }
  };
  return { scheduler, heartbeats, intervals, tickIntervals };
};

const makeWorkerStub = () => ({
  posted: [],
  terminated: false,
  postMessage(data) { this.posted.push(data); },
  terminate() { this.terminated = true; },
  onmessage: null,
  onerror: null,
  onmessageerror: null,
});

/* 1) 契约常量：拍点周期 = 后端租约 300s 的 1/3 */
assert.equal(HEARTBEAT_INTERVAL_MS, 100000);

/* 2) attachWorker：worker 模式下 start 闭集消息、拍点驱动心跳、不建页面定时器 */
{
  const { scheduler, heartbeats, intervals } = makeHarness();
  const worker = makeWorkerStub();
  scheduler.attachWorker(worker);
  assert.equal(scheduler.mode, "worker");
  assert.deepEqual(worker.posted, [{ type: "start", intervalMs: 100000 }]);
  assert.equal(intervals.size, 0, "worker 模式不占页面定时器");
  worker.onmessage({ data: "heartbeat" });
  assert.equal(heartbeats.length, 1, "worker 拍点驱动一次心跳");
  worker.onmessage({ data: "other" });
  assert.equal(heartbeats.length, 1, "非拍点消息不驱动心跳");
  scheduler.stop();
  assert.equal(worker.terminated, true, "stop 终止 worker");
}

/* 3) worker 出错降级：终止 worker + 建 100s 页面定时器；降级后旧 worker 拍点失效 */
{
  const { scheduler, heartbeats, intervals, tickIntervals } = makeHarness();
  const worker = makeWorkerStub();
  scheduler.attachWorker(worker);
  worker.onerror(new Event("error"));
  assert.equal(scheduler.mode, "page");
  assert.equal(worker.terminated, true, "降级时终止失败 worker");
  assert.equal(intervals.size, 1);
  assert.equal(worker.onmessage, null, "降级时旧 worker 处理器已摘除");
  assert.equal(heartbeats.length, 0, "降级后旧 worker 拍点不再驱动心跳");
  await tickIntervals(100000);
  assert.equal(heartbeats.length, 1, "页面回退定时器按拍驱动心跳");
  scheduler.stop();
}

/* 4) postMessage 抛错 → 同样降级页面定时器 */
{
  const { scheduler, intervals } = makeHarness();
  const worker = makeWorkerStub();
  worker.postMessage = () => { throw new Error("synthetic postMessage boom"); };
  scheduler.attachWorker(worker);
  assert.equal(scheduler.mode, "page");
  assert.equal(intervals.size, 1, "postMessage 抛错降级为页面定时器");
  scheduler.stop();
}

/* 5) 构造抛错路径：startPageFallback 直接进入页面回退 */
{
  const { scheduler, heartbeats, tickIntervals } = makeHarness();
  scheduler.startPageFallback();
  assert.equal(scheduler.mode, "page");
  await tickIntervals(100000);
  assert.equal(heartbeats.length, 1);
  scheduler.stop();
}

/* 6) visible 即时补发：worker/page 模式立即心跳，idle 不发 */
{
  const { scheduler, heartbeats } = makeHarness();
  scheduler.onVisible();
  assert.equal(heartbeats.length, 0, "未启动时不发心跳");
  scheduler.startPageFallback();
  scheduler.onVisible();
  assert.equal(heartbeats.length, 1, "页面回退下可见立即补发");
  scheduler.stop();
  scheduler.onVisible();
  assert.equal(heartbeats.length, 1, "stop 后可见不补发");
}

/* 7) stop 幂等收口：取消页面定时器并终止 worker */
{
  const { scheduler, intervals } = makeHarness();
  const worker = makeWorkerStub();
  scheduler.attachWorker(worker);
  scheduler.stop();
  assert.equal(scheduler.mode, "idle");
  assert.equal(intervals.size, 0);
  scheduler.stop();
  assert.equal(scheduler.mode, "idle", "stop 幂等");
}

/* 8) 静态钉：Worker 节拍器文件存在且协议闭集（防 404/协议漂移） */
const workerSource = await readFile(new URL("../frontend/workers/heartbeat-worker.js", import.meta.url), "utf8");
assert.match(workerSource, /type === "start"/);
assert.match(workerSource, /postMessage\("heartbeat"\)/);
assert.match(workerSource, /clearInterval/);

console.log("frontend_session_heartbeat_behavior: all assertions passed");
