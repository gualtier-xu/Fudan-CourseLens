import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { assemble, FakeElement } from "./frontend_exec_harness.mjs";
import {
  compareVersions,
  macUpdateSnapshot,
  macUpdateEntry,
  onMacUpdateChange,
  runMacUpdateCheck,
} from "../frontend/modules/update-mac.js";

/* UPDATE-PANEL-1（MAC-3 决策 C 落地）+ UPDATE-UX-1（2026-10-09 用户令：mac 有
   新版本必须醒目提醒，手动下载体验也要好）更新面板平台分发钉：
   1. 结构钉：index.html 更新组 = Windows 面收进 update-windows-face（原控件
      逐位不动 + 下载进度行）+ mac 入口块三态检查面（消息/版本行/三步安装/
      检查钮/打开下载页锚）；
   2. 分发钉：update-panel.js renderUpdate 以 macUpdateEntry() 早分支，
      darwin 宿主一律检查面（不看 Windows 快照、不写 Windows 面）；
   3. 真执行钉（new Function 装配 update-panel.js 模块体，注入 update-mac.js
      真模块 + navigator 桩）：
      - mac UA：三态各就位——available=胶囊「发现更新」+新版本句+三步可见+
        版本行（当前版本+钥匙串提示）；up_to_date=安静「已是最新」+三步隐藏；
        unavailable=诚实降级（绝不出现「已是最新」字样）；
      - mac 检查道状态→卡面即时重绘（真 onMacUpdateChange 订阅活线钉：
        runMacUpdateCheck 完成后不调 renderUpdate 面也已更新）；
      - Windows UA（WebView2）：现行为逐位不变——null 诚实「正在读取」+通用
        指引、available 按 actions 启停三钮、policy_blocked 仍走 Windows 指导
        句、下载进度行按 download_bytes/download_total 渲染（>2s 进度+>10s
        ETA 三律），mac 面 id 在 Windows 快照下零写入。 */

const html = readFileSync(new URL("../frontend/index.html", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const panelSource = readFileSync(
  new URL("../frontend/modules/settings/update-panel.js", import.meta.url), "utf8").replace(/\r\n/g, "\n");

const MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
const WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const GENERIC_UPDATE_GUIDANCE = "更新暂未继续。请稍后重新检查；若持续出现，可重新安装客户端。";
const WIN_MISMATCH_GUIDANCE = "此设备不符合受管理 Windows 更新条件。请使用受支持的 Windows 客户端。";
const PAGES_DOWNLOAD_HREF = "https://gualtier-xu.github.io/Fudan-CourseLens/#download";

function setNavigator(userAgent) {
  Object.defineProperty(globalThis, "navigator", {
    value: { userAgent, onLine: true }, configurable: true,
  });
}

function testStructurePins() {
  assert.ok(html.includes('<div id="update-windows-face">'), "windows face wrapper missing");
  assert.ok(html.includes('<div id="update-mac-entry" hidden>'), "mac entry block missing");
  /* Windows 面控件逐位不动（原串原位） */
  assert.ok(html.includes('<button id="check-update" type="button" class="btn-primary">检查更新</button>'),
    "windows check button drifted");
  assert.ok(html.includes('<button id="download-update" type="button" disabled>确认并下载</button>'),
    "windows download button drifted");
  assert.ok(html.includes('<button id="install-update" type="button" disabled>确认安装</button>'),
    "windows install button drifted");
  assert.ok(html.includes('<input id="update-background-checks" type="checkbox" role="switch">自动检查更新'),
    "windows background-checks row drifted");
  /* SIMPLIFY-AUDIT-1 S5：五事实行收敛为一行人话+上次检查提示，「通道」行话退役 */
  assert.ok(html.includes('<p id="update-facts-line" class="update-facts-line">'),
    "windows facts line missing");
  assert.ok(html.includes('<p id="update-last-check" class="hint"></p>'),
    "windows last-check hint missing");
  assert.ok(!html.includes("update-channel"), "channel jargon must not resurface");
  assert.ok(html.includes("首次运行若看到 Windows 安全提示"), "windows hint drifted");
  /* UPDATE-UX-1：Windows 面下载进度行（零呆等三律） */
  assert.ok(html.includes('<p id="update-progress" class="hint" hidden></p>'),
    "windows download progress line missing");
  /* mac 入口块：三步安装 + 检查钮 + Pages 下载锚（文案闭集 + 人情味） */
  assert.ok(html.includes("macOS 测试版：点「检查更新」看看有没有新版本；安装包在下载页手动获取。"),
    "mac entry idle copy missing");
  assert.ok(html.includes("到下载页下载新版 zip"), "mac step 1 (download zip) missing");
  assert.ok(html.includes("把它拖进「应用程序」"), "mac step 2 (drag to Applications) missing");
  assert.ok(html.includes("访达里右键点 CourseLens 选「打开」"), "mac step 3 (Gatekeeper right-click open) missing");
  assert.ok(html.includes('<button id="update-mac-check" type="button" class="btn-primary">检查更新</button>'),
    "mac check button missing");
  assert.ok(html.includes(`<a id="update-mac-download" class="button-link primary" href="${PAGES_DOWNLOAD_HREF}" rel="noreferrer">打开下载页</a>`),
    "mac download anchor missing or drifted");
  assert.ok(html.includes("应用内自动更新暂时只支持 Windows"), "mac auto-update honesty note missing");
  /* 分发钉：renderUpdate 以 macUpdateEntry() 早分支，先于快照判定 */
  assert.ok(panelSource.includes("if (macUpdateEntry()) {\n    renderMacUpdateEntry();\n    return;\n  }"),
    "renderUpdate mac dispatch branch missing");
}

const byId = new Map();
for (const id of [
  "update-state", "update-windows-face", "update-mac-entry", "update-error",
  "update-recovery", "update-facts-line", "update-last-check",
  "update-notes", "check-update",
  "download-update", "install-update", "update-progress",
  "update-mac-message", "update-mac-version", "update-mac-steps", "update-mac-check",
]) byId.set(id, new FakeElement(id));

function resetElements() {
  for (const node of byId.values()) {
    node.textContent = "";
    node.hidden = false;
    node.disabled = false;
  }
  /* index.html 静态属性的镜像：入口块与进度行带 hidden 属性出生，行为断言
     才有「谁写了它」的意义。 */
  byId.get("update-mac-entry").hidden = true;
  byId.get("update-progress").hidden = true;
  byId.get("update-mac-version").hidden = true;
  byId.get("update-mac-steps").hidden = true;
}

const noop = () => {};
const closedMapStub = (map, key, fallback) =>
  key != null && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : fallback;

function loadRenderUpdate() {
  /* 直接装配 update-panel.js（与他道在途的 settings 家族文件解耦）；
     mac 三件套注入 update-mac.js 真模块（真状态机+真订阅活线），navigator
     由 globalThis 注入桩供给（update-mac.js 真模块读全局，两者同源）。 */
  const factory = assemble(
    "../frontend/modules/settings/update-panel.js",
    ["postV3", "$", "clear", "setBusy", "textElement", "toast",
      "macUpdateEntry", "macUpdateSnapshot", "onMacUpdateChange",
      "closedDiagnosticValue", "closedMapValue", "formatBytes", "isPlainRecord",
      "navigator"],
    ["renderUpdate"],
  );
  const missing = [];
  const $stub = (id) => {
    const node = byId.get(id);
    if (!node) missing.push(id);
    return node;
  };
  const { renderUpdate } = factory(
    noop, $stub, noop, noop, noop, noop,
    macUpdateEntry, macUpdateSnapshot, onMacUpdateChange,
    noop, closedMapStub, () => "—", noop, globalThis.navigator,
  );
  assert.deepEqual(missing, [], `renderUpdate touched unknown ids: ${missing}`);
  return { renderUpdate };
}

async function withMacFetch(stub, run) {
  const realFetch = globalThis.fetch;
  globalThis.fetch = stub;
  try {
    await run();
  } finally {
    globalThis.fetch = realFetch;
  }
}

const releaseListOf = (version) => new Response(JSON.stringify([
  { draft: false, tag_name: `client-v${version}-macos-test` },
]), { status: 200, headers: { "Content-Type": "application/json" } });

async function testMacEntryStates() {
  setNavigator(MAC_UA);
  const { renderUpdate } = loadRenderUpdate();
  const entry = byId.get("update-mac-entry");
  const message = byId.get("update-mac-message");
  const versionLine = byId.get("update-mac-version");
  const steps = byId.get("update-mac-steps");
  const check = byId.get("update-mac-check");
  const pill = byId.get("update-state");

  /* 快照未到达：入口态（idle 文案），绝不闪 Windows 面 */
  renderUpdate(undefined);
  assert.equal(pill.textContent, "手动下载", "mac pill must read 手动下载 on idle");
  assert.equal(byId.get("update-windows-face").hidden, true, "windows face must hide on mac");
  assert.equal(entry.hidden, false, "mac entry must show on mac");
  assert.ok(message.textContent.includes("点「检查更新」看看有没有新版本"), "idle copy drifted");
  assert.equal(steps.hidden, true, "steps must hide before a check");
  assert.equal(versionLine.hidden, true, "version line must hide before a check");
  assert.equal(check.disabled, false, "mac check button enabled on idle");

  /* fail-closed 快照到达：入口态不变，且零写入 Windows 面（notes 保持空） */
  renderUpdate({
    state: "policy_blocked", error_code: "host_platform_mismatch",
    current_version: "0.1.0", channel: "stable", available_version: "",
    package_size: 0, last_checked_at: 1790000000, release_notes: "不应被写入",
    actions: ["check"],
  });
  assert.equal(pill.textContent, "手动下载", "mac pill must stay 手动下载");
  assert.equal(byId.get("update-notes").textContent, "", "mac path must not write windows face");
  assert.equal(byId.get("update-error").textContent, "", "mac path must not write windows guidance");

  /* 快照失败（null）：仍是入口态，不落「正在读取」通用指引 */
  renderUpdate(null);
  assert.equal(entry.hidden, false, "mac entry stays visible on null");

  /* available：真检查道跑通（0.2.0 > 0.1.0），三步可见 + 版本行带钥匙串提示。
     活线钉：await 后不调 renderUpdate，订阅已重绘卡面。 */
  await withMacFetch(async () => releaseListOf("0.2.0"), async () => {
    await runMacUpdateCheck("0.1.0");
  });
  assert.equal(pill.textContent, "发现更新", "mac pill must read 发现更新 on available");
  assert.equal(message.textContent, "发现新版本 0.2.0，可以更新了。", "available copy drifted");
  assert.equal(steps.hidden, false, "steps must show on available");
  assert.ok(versionLine.textContent.includes("当前版本 0.1.0"), "version line must carry current version");
  assert.ok(versionLine.textContent.includes("钥匙串"), "version line must carry keychain note");
  assert.equal(check.disabled, false, "mac check button re-enabled after check");

  /* up_to_date：安静句 + 三步隐藏 */
  await withMacFetch(async () => releaseListOf("0.2.0"), async () => {
    await runMacUpdateCheck("0.2.0");
  });
  assert.equal(pill.textContent, "已是最新", "mac pill must read 已是最新 on up_to_date");
  assert.equal(message.textContent, "已是最新版本。", "up_to_date copy drifted");
  assert.equal(steps.hidden, true, "steps must hide on up_to_date");

  /* unavailable：诚实降级——绝不误导成「已是最新」 */
  await withMacFetch(async () => { throw new Error("network down"); }, async () => {
    await runMacUpdateCheck("0.1.0");
  });
  assert.equal(pill.textContent, "手动下载", "mac pill must stay 手动下载 on unavailable");
  assert.ok(message.textContent.includes("检查没有完成"), "unavailable copy drifted");
  assert.ok(!message.textContent.includes("已是最新"), "unavailable must never claim up to date");
  assert.equal(steps.hidden, true, "steps must hide on unavailable");
}

async function testMacCheckingFeedback() {
  setNavigator(MAC_UA);
  const { renderUpdate } = loadRenderUpdate();
  const pill = byId.get("update-state");
  const check = byId.get("update-mac-check");
  let releaseFetch;
  const pending = new Promise((resolve) => { releaseFetch = resolve; });
  await withMacFetch(async () => pending, async () => {
    const running = runMacUpdateCheck("0.1.0");
    await Promise.resolve();
    /* ≤100ms 即时反馈（三律）：发起即 checking 态上脸 + 按钮禁用 */
    assert.equal(pill.textContent, "正在检查", "mac pill must flip to 正在检查 immediately");
    assert.equal(byId.get("update-mac-message").textContent, "正在检查更新…", "checking copy drifted");
    assert.equal(check.disabled, true, "mac check button must disable while checking");
    releaseFetch(releaseListOf("0.3.0"));
    await running;
  });
  assert.equal(pill.textContent, "发现更新", "mac pill lands on 发现更新 after release");
}

async function testWindowsFaceUnchanged() {
  setNavigator(WINDOWS_UA);
  const { renderUpdate } = loadRenderUpdate();
  const entry = byId.get("update-mac-entry");
  const message = byId.get("update-mac-message");
  const progress = byId.get("update-progress");

  /* null 快照：现合同逐位保留（诚实「正在读取」+通用指引），入口块恒隐藏 */
  renderUpdate(null);
  assert.equal(byId.get("update-state").textContent, "正在读取", "windows null contract drifted");
  assert.equal(byId.get("update-windows-face").hidden, false, "windows face must stay visible on windows");
  assert.equal(entry.hidden, true, "mac entry must hide on windows");
  assert.equal(byId.get("update-error").textContent, GENERIC_UPDATE_GUIDANCE,
    "windows null guidance drifted");
  assert.equal(progress.hidden, true, "progress line must hide on null");

  /* available 快照：三钮按 actions 启停（现行为）+ mac 面 id 零写入 */
  renderUpdate({
    state: "available", current_version: "0.1.0", channel: "stable",
    available_version: "0.2.0", package_size: 1115748, last_checked_at: 1790000000,
    release_notes: "修复了字幕断句", error_code: "",
    actions: ["check", "download", "install"],
  });
  assert.equal(byId.get("update-state").textContent, "发现更新", "windows available pill drifted");
  /* S5 事实行：格式化桩（formatBytes→"—"）下大小注记诚实省略 */
  assert.equal(byId.get("update-facts-line").textContent,
    "当前版本 0.1.0 → 可用版本 0.2.0", "facts line drifted");
  assert.equal(byId.get("update-last-check").textContent,
    `上次检查：${new Date(1790000000 * 1000).toLocaleString()}`, "last-check hint drifted");
  assert.equal(byId.get("check-update").disabled, false, "check button enablement drifted");
  assert.equal(byId.get("download-update").disabled, false, "download button enablement drifted");
  assert.equal(byId.get("install-update").disabled, false, "install button enablement drifted");
  assert.equal(entry.hidden, true, "mac entry must stay hidden on windows");
  assert.equal(message.textContent, "", "windows snapshots must not write mac face");
  assert.equal(progress.hidden, true, "progress line must hide when not downloading");

  /* 下载进度行（三律 >2s 显进度）：42% 字节进度上脸，进度行 aria-free（SR
     用户由顶栏可读名与状态迁移播报服务，避免逐拍播报噪声） */
  renderUpdate({
    state: "downloading", current_version: "0.1.0", channel: "stable",
    available_version: "0.2.0", package_size: 1000, download_bytes: 420,
    download_total: 1000, last_checked_at: 1790000000, release_notes: "",
    error_code: "", actions: [],
  });
  assert.equal(progress.hidden, false, "progress line must show while downloading");
  assert.ok(progress.textContent.startsWith("正在下载 42%（"), "progress percent drifted");

  /* download_total 缺失/为 0（旧快照形状）：进度行诚实隐藏，绝不显示编造百分比 */
  renderUpdate({
    state: "downloading", current_version: "0.1.0", channel: "stable",
    available_version: "0.2.0", package_size: 1000, last_checked_at: 1790000000,
    release_notes: "", error_code: "", actions: [],
  });
  assert.equal(progress.hidden, true, "progress line must hide without a total");

  /* host_platform_mismatch（Windows 语义 fail-closed 原样）：Windows 指导句逐位不变 */
  renderUpdate({
    state: "policy_blocked", error_code: "host_platform_mismatch",
    current_version: "0.1.0", channel: "stable", available_version: "",
    package_size: 0, last_checked_at: 0, release_notes: "", actions: ["check"],
  });
  assert.equal(byId.get("update-state").textContent, "策略阻止", "windows policy_blocked pill drifted");
  assert.equal(byId.get("update-windows-face").hidden, false, "windows face must stay visible");
  assert.equal(entry.hidden, true, "mac entry must hide on windows");
  assert.equal(byId.get("update-error").textContent, WIN_MISMATCH_GUIDANCE,
    "windows host_platform_mismatch guidance drifted");
}

async function testDownloadEtaClause() {
  setNavigator(WINDOWS_UA);
  const { renderUpdate } = loadRenderUpdate();
  const progress = byId.get("update-progress");
  const total = 10 * 1024 * 1024;
  const mebi = 1024 * 1024;
  const realNow = Date.now;
  let now = 1_000_000;
  try {
    Date.now = () => now;
    renderUpdate({
      state: "downloading", current_version: "0.1.0", channel: "stable",
      available_version: "0.2.0", package_size: total, download_bytes: mebi,
      download_total: total, last_checked_at: 0, release_notes: "",
      error_code: "", actions: [],
    });
    assert.ok(!progress.textContent.includes("预计还需"), "first sample must not invent an ETA");
    now += 2000; /* 2s 内 1MiB → 0.5 MiB/s → 剩 8MiB ≈ 16s（>10s 才示 ETA） */
    renderUpdate({
      state: "downloading", current_version: "0.1.0", channel: "stable",
      available_version: "0.2.0", package_size: total, download_bytes: 2 * mebi,
      download_total: total, last_checked_at: 0, release_notes: "",
      error_code: "", actions: [],
    });
    assert.ok(progress.textContent.includes("预计还需约 16 秒"), `ETA clause drifted: ${progress.textContent}`);
  } finally {
    Date.now = realNow;
  }
}

function testVersionComparePins() {
  assert.ok(compareVersions("0.10.0", "0.9.0") > 0, "numeric compare must not be lexicographic");
  assert.ok(compareVersions("1.0.0-rc.1", "1.0.0") < 0, "prerelease sorts below release");
  assert.equal(compareVersions("0.2.0", "0.2.0"), 0, "equal versions compare equal");
  assert.equal(compareVersions("garbage", "0.1.0"), 0, "garbage compares as unknown");
}

setNavigator(WINDOWS_UA);
testStructurePins();
await testMacEntryStates();
await testMacCheckingFeedback();
resetElements();
await testWindowsFaceUnchanged();
await testDownloadEtaClause();
testVersionComparePins();
console.log("update platform entry pins: dual-state (windows unchanged / macos tri-state) passed");
