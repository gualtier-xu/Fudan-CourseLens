/* BACKEND-DEATH-1③ / AS1：前端断连横幅行为钉测。
   连续 3 次 /api/health 探测失败 → 诚实轻量横幅 + 连接动作按钮禁用；任一次成功自动撤销。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const bannerNode = { hidden: true, textContent: "", dataset: {} };
const actionButtons = [
  { id: "conn-status", disabled: false },
  { id: "account-button", disabled: false },
];
globalThis.document = {
  documentElement: { dataset: {} },
  getElementById: (id) => (id === "service-banner" ? bannerNode : null),
  querySelectorAll: (selector) => (String(selector).includes("data-service-action") ? actionButtons : []),
};

const {
  SERVICE_DISCONNECT_TEXT,
  SERVICE_PROBING_TEXT,
  SERVICE_PROBE_FAST_DELAY_MS,
  SERVICE_PROBE_IDLE_DELAY_MS,
  SERVICE_PROBE_FAST_TICK_MAX,
  SERVICE_HEALTH_FAILURE_THRESHOLD,
  SERVICE_HEALTH_PROBE_RETRY_DELAY_MS,
  createRetryingHealthProbe,
  createServiceHealthMonitor,
  nextServiceProbeDelayMs,
  applyServiceDisconnectState,
} = await import("../frontend/modules/shell.js");

/* 1) 闭集文案与阈值契约（AS1：诚实轻量，不指挥重启；POLISH-O13 O1：确认前轻提示） */
assert.equal(SERVICE_DISCONNECT_TEXT, "界面暂时没连上本地服务，正在自动重试…");
assert.equal(SERVICE_PROBING_TEXT, "本地服务暂时没响应，正在确认连接…");
assert.equal(SERVICE_HEALTH_FAILURE_THRESHOLD, 3);
assert.equal(SERVICE_HEALTH_PROBE_RETRY_DELAY_MS, 1200);
assert.equal(SERVICE_PROBE_FAST_DELAY_MS, 3000);
assert.equal(SERVICE_PROBE_IDLE_DELAY_MS, 10000);
assert.equal(SERVICE_PROBE_FAST_TICK_MAX, 5);

/* 2) 连续失败满阈值弹横幅；期间不重复；恢复撤销且计数清零 */
{
  const events = [];
  let healthy = false;
  const monitor = createServiceHealthMonitor({
    probe: async () => healthy,
    onDisconnect: () => { events.push("disconnect"); },
    onRecover: () => { events.push("recover"); },
  });
  await monitor.tick();
  await monitor.tick();
  assert.deepEqual(events, [], "不足 3 次失败不弹横幅");
  assert.equal(monitor.down, false);
  await monitor.tick();
  assert.deepEqual(events, ["disconnect"], "连续 3 次失败判服务断开");
  assert.equal(monitor.down, true);
  await monitor.tick();
  assert.deepEqual(events, ["disconnect"], "已断开期间不重复回调");
  healthy = true;
  await monitor.tick();
  assert.deepEqual(events, ["disconnect", "recover"], "任一次成功即自动撤销");
  assert.equal(monitor.down, false);
  healthy = false;
  await monitor.tick();
  await monitor.tick();
  assert.deepEqual(events, ["disconnect", "recover"], "恢复后失败计数清零");
  await monitor.tick();
  assert.deepEqual(events, ["disconnect", "recover", "disconnect"], "再次满 3 次重新断开");
}

/* 3) 探测抛错按失败计 */
{
  const events = [];
  const monitor = createServiceHealthMonitor({
    probe: async () => { throw new Error("synthetic probe boom"); },
    onDisconnect: () => { events.push("disconnect"); },
    onRecover: () => { events.push("recover"); },
  });
  await monitor.tick();
  await monitor.tick();
  assert.deepEqual(events, []);
  await monitor.tick();
  assert.deepEqual(events, ["disconnect"], "探测异常计为失败");
}

/* 4) 横幅应用/撤销：banner hidden 翻转 + 连接动作按钮 disabled 翻转 */
assert.equal(bannerNode.hidden, true, "初始无横幅");
applyServiceDisconnectState(true);
assert.equal(bannerNode.hidden, false, "断开时显示横幅");
assert.equal(bannerNode.textContent, SERVICE_DISCONNECT_TEXT, "断开态写确认文案");
assert.equal(bannerNode.dataset.state, "down");
assert.deepEqual(actionButtons.map((button) => button.disabled), [true, true], "断开时禁用连接动作按钮");
applyServiceDisconnectState(false);
assert.equal(bannerNode.hidden, true, "恢复时撤销横幅");
assert.deepEqual(actionButtons.map((button) => button.disabled), [false, false], "恢复时重新启用按钮");

/* 4b) POLISH-O13 O1（零呆等·等待可视化）：确认前的疑似态即时可见——轻提示
   横幅点亮但不禁用任何动作钮；确认后升级为断开文案；撤销即隐藏。 */
applyServiceDisconnectState(false, { probing: true });
assert.equal(bannerNode.hidden, false, "疑似态轻提示可见（不再静默窗）");
assert.equal(bannerNode.dataset.state, "probing");
assert.equal(bannerNode.textContent, SERVICE_PROBING_TEXT, "疑似态写确认中文案（不宣称已断开）");
assert.deepEqual(actionButtons.map((button) => button.disabled), [false, false], "疑似态不禁用连接动作按钮");
applyServiceDisconnectState(true);
assert.equal(bannerNode.hidden, false);
assert.equal(bannerNode.textContent, SERVICE_DISCONNECT_TEXT, "确认后升级为断开文案");
assert.deepEqual(actionButtons.map((button) => button.disabled), [true, true]);
applyServiceDisconnectState(false);
assert.equal(bannerNode.hidden, true, "撤销后隐藏");
assert.deepEqual(actionButtons.map((button) => button.disabled), [false, false]);

/* 4c) POLISH-O13 O1：疑似/断开期共用快拍语义——前 5 拍 3s 加速确认（最坏
   ~30s→~16s），之后回 10s 常速；恢复健康即清零。 */
{
  let fastTicks = 0;
  const delays = [];
  for (let i = 0; i < 7; i += 1) {
    const next = nextServiceProbeDelayMs({ troubled: true, fastTicks });
    delays.push(next.delayMs);
    fastTicks = next.fastTicks;
  }
  assert.deepEqual(delays, [3000, 3000, 3000, 3000, 3000, 10000, 10000], "疑似期前 5 拍 3s、之后回常速");
  const healed = nextServiceProbeDelayMs({ troubled: false, fastTicks: 4 });
  assert.deepEqual(healed, { delayMs: 10000, fastTicks: 0 }, "恢复健康清零回常速");
}

/* 5) DISPATCH-HEALTH-1：单次失败先补探一次——瞬时饥饿（连接池/标签页节流）不再
   直接计入失败；两次都失败才计一次，真断连的检出语义不变 */
{
  const stamps = [];
  const sleeps = [];
  let now = 0;
  const transient = createRetryingHealthProbe({
    probeOnce: async () => { stamps.push(now); return stamps.length > 1; },
    retryDelayMs: 1200,
    sleep: async (ms) => { sleeps.push(ms); now += ms; },
  });
  assert.equal(await transient(), true, "首次失败 + 补探成功 = 服务健康");
  assert.deepEqual(sleeps, [1200], "补探前等待固定退避");
  assert.equal(stamps.length, 2, "补探恰一次");

  let attempts = 0;
  const dead = createRetryingHealthProbe({
    probeOnce: async () => { attempts += 1; return false; },
    sleep: async () => {},
  });
  assert.equal(await dead(), false, "两次都失败才是失败");
  assert.equal(attempts, 2);

  let thrown = 0;
  const throwing = createRetryingHealthProbe({
    probeOnce: async () => { thrown += 1; throw new Error("synthetic probe boom"); },
    sleep: async () => {},
  });
  assert.equal(await throwing(), false, "探测异常按失败处理");
  assert.equal(thrown, 2, "异常也要补探一次");

  let healthyCalls = 0;
  const healthy = createRetryingHealthProbe({
    probeOnce: async () => { healthyCalls += 1; return true; },
    sleep: async () => { throw new Error("healthy probe must not sleep"); },
  });
  assert.equal(await healthy(), true);
  assert.equal(healthyCalls, 1, "健康时不得多打一次探测");
}

/* 6) 补探让最坏一拍（5s+1.2s+5s）长于 10s 轮询：重叠 tick 不得把同一次饥饿记两次 */
{
  const events = [];
  let resolveProbe = null;
  const monitor = createServiceHealthMonitor({
    probe: () => new Promise((resolve) => { resolveProbe = resolve; }),
    onDisconnect: () => { events.push("disconnect"); },
    onRecover: () => { events.push("recover"); },
  });
  const first = monitor.tick();
  const overlapped = monitor.tick();
  resolveProbe(false);
  await first;
  await overlapped;
  assert.equal(monitor.failures, 1, "在飞期间的重复 tick 只记一次失败");
  assert.deepEqual(events, [], "一拍不得凑满阈值");

  const third = monitor.tick();
  resolveProbe(true);
  await third;
  assert.equal(monitor.failures, 0, "恢复后计数清零");
}

/* 7) 静态钉：横幅元素与闭集文案在 index.html；连接动作按钮已标注；shell 探测接线 */
const indexSource = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
assert.match(indexSource, /id="service-banner"/);
assert.match(indexSource, /界面暂时没连上本地服务，正在自动重试…/);
assert.match(indexSource, /id="conn-status"[^>]*data-service-action/);
assert.match(indexSource, /id="account-button"[^>]*data-service-action/);
const shellSource = await readFile(new URL("../frontend/modules/shell.js", import.meta.url), "utf8");
assert.match(shellSource, /fetch\("\/api\/health"/);
/* P56-U3②③：监视器装配与 openSession 成败解耦 + down 期加速恢复拍（前 5 拍 3s、
   之后回 10s）+ open 有界退避重试 + 横幅点亮/自撤 console 留痕 */
assert.match(shellSource, /void healthTick\(\);/, "监视器装配与 openSession 成败解耦（open 失败也装配）");
assert.match(shellSource, /healthTimer = window\.setTimeout\(healthTick, next\.delayMs\);/, "恢复拍自调度（疑似/断开期 3s 加速、之后回 10s）");
assert.match(shellSource, /troubled: serviceMonitor\.down \|\| serviceMonitor\.failures >= 1/, "疑似态与断开态共用快拍判定");
/* POLISH-O13 O1：确认前疑似态即时可见——装配层把「有失败未满阈值」接进轻提示 */
assert.match(shellSource, /probing: serviceMonitor\.failures >= 1/, "首拍失败即亮轻提示（静默窗根除）");
assert.match(indexSource, /#service-banner\[data-state="probing"\]/, "疑似态轻横幅样式在壳内联样式");
assert.match(
  shellSource,
  /export function nextServiceProbeDelayMs/,
  "拍间隔为可钉测纯函数",
);
assert.match(shellSource, /const openSessionWithRetry/, "open 有界退避重试");
assert.match(shellSource, /\[health\] 断连横幅点亮（连续失败/, "横幅点亮 console 留痕（含连续失败计数）");
assert.match(shellSource, /服务探测恢复，断连横幅自撤/, "横幅自撤 console 留痕");
assert.match(shellSource, /export function createRetryingHealthProbe/);
assert.match(
  shellSource,
  /const serviceHealthProbe = createRetryingHealthProbe\(\{ probeOnce: serviceHealthProbeOnce \}\)/,
  "10s 探测必须接补探包装（DISPATCH-HEALTH-1）",
);

console.log("frontend_service_disconnect_behavior: all assertions passed");
