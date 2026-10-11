/* 夜10-A APP-SHAPE-2：原生窗口壳集成行为钉测（browser 形态零行为差异 + 桥形态四面）。
   外链闭集 capture / 焦点回归 / 托盘开关接线 / 界面缓存行——全部用桩桥，零真实 GUI。 */

import assert from "node:assert/strict";

/* ---- 最小 DOM/浏览器桩 ---- */
const recorded = { documentListeners: new Map(), windowListeners: new Map(), windowOpens: [] };

globalThis.Element = class Element {};
function elementStub(extra = {}) {
  // 必须是 globalThis.Element 的实例：产品代码用 instanceof 探测点击目标。
  return Object.assign(new globalThis.Element(), extra);
}

const trayToggle = elementStub({
  checked: false,
  listeners: new Map(),
  addEventListener(type, fn) { this.listeners.set(type, fn); },
});
const trayRow = elementStub({ hidden: true });
const cacheSize = elementStub({ textContent: "" });
const cacheButton = elementStub({
  textContent: "清理界面缓存",
  disabled: false,
  listeners: new Map(),
  addEventListener(type, fn) { this.listeners.set(type, fn); },
});
const cacheSection = elementStub({ hidden: true });
const workspaceMain = elementStub({ focused: 0, focus() { this.focused += 1; } });

function byId(id) {
  switch (id) {
    case "settings-exit-on-close": return trayToggle;
    case "settings-tray-row": return trayRow;
    case "shell-webview-cache": return cacheSection;
    case "shell-webview-cache-size": return cacheSize;
    case "shell-webview-cache-clear": return cacheButton;
    case "workspace-main": return workspaceMain;
    default: return null;
  }
}

globalThis.document = {
  documentElement: { dataset: { theme: "dark" } },
  getElementById: byId,
  addEventListener: (type, fn) => recorded.documentListeners.set(type, fn),
};
globalThis.window = {
  location: { href: "http://127.0.0.1:6269/", origin: "http://127.0.0.1:6269" },
  setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 20)),
  clearTimeout,
  addEventListener: (type, fn) => recorded.windowListeners.set(type, fn),
  removeEventListener: (type) => recorded.windowListeners.delete(type),
  open: (href) => { recorded.windowOpens.push(href); },
};

const bridgeCalls = [];
globalThis.window.pywebview = {
  api: {
    get_window_features: async () => ({
      native_window: true, features_version: 2, exit_on_close: true,
      webview_cache_bytes: 1024 * 1024, can_clear_webview_cache: true,
    }),
    open_external: async (url) => { bridgeCalls.push(["open_external", url]); return { ok: true }; },
    set_exit_on_close: async (enabled) => { bridgeCalls.push(["set_exit_on_close", enabled]); return { ok: true, exit_on_close: enabled }; },
    set_titlebar_theme: async (dark) => { bridgeCalls.push(["set_titlebar_theme", dark]); return { ok: true }; },
    request_webview_cache_clear: async () => { bridgeCalls.push(["request_webview_cache_clear"]); return { ok: true, applies_on_next_start: true }; },
  },
};

const { installNativeWindowShell, NATIVE_WINDOW_FOCUS_EVENT } = await import("../frontend/modules/shell.js");

/* 1) 浏览器形态（无 pywebview）：装得进、零行为、零崩溃 */
{
  const savedPywebview = globalThis.window.pywebview;
  delete globalThis.window.pywebview;
  const cleanup = installNativeWindowShell();
  assert.equal(typeof cleanup, "function");
  assert.ok(recorded.documentListeners.has("click"), "外链 capture 在浏览器形态也安装（点外链接走 window.open 兜底）");
  globalThis.window.pywebview = savedPywebview;
}

/* 2) 桥形态安装：外链 capture + 焦点回归监听在位 */
installNativeWindowShell();
assert.ok(recorded.windowListeners.has(NATIVE_WINDOW_FOCUS_EVENT), "焦点回归监听必须安装");
await new Promise((r) => setTimeout(r, 250)); // 装配沉降（nativeApiReady→标题栏同步等异步尾）
bridgeCalls.length = 0;

/* 3) 外链闭集 capture：跨源 http → preventDefault + 桥 open_external；同源 → 放行 */
{
  const clickHandler = recorded.documentListeners.get("click");
  const prevented = [];
  const anchorFor = (href) => elementStub({ closest: () => elementStub({ href }) });

  clickHandler({
    defaultPrevented: false, button: 0, preventDefault: () => prevented.push(1),
    target: anchorFor("https://www.fudan.edu.cn/"),
  });
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(prevented, [1], "跨源 http 必须拦截");
  assert.deepEqual(bridgeCalls.at(-1), ["open_external", "https://www.fudan.edu.cn/"]);

  clickHandler({
    defaultPrevented: false, button: 0, preventDefault: () => prevented.push(1),
    target: anchorFor("/api/v3/tasks"),
  });
  assert.deepEqual(prevented, [1], "同源链接不得被拦（计数不变）");
  assert.ok(!recorded.windowOpens.includes("/api/v3/tasks"));

  clickHandler({
    defaultPrevented: false, button: 0, preventDefault: () => prevented.push(1),
    target: anchorFor("javascript:alert(1)"),
  });
  assert.deepEqual(prevented, [1], "非 http(s) 不拦截不派发（浏览器默认行为保留）");
}

/* 4) 焦点回归：事件 → 焦点落 workspace-main */
{
  recorded.windowListeners.get(NATIVE_WINDOW_FOCUS_EVENT)({});
  assert.equal(workspaceMain.focused, 1, "还原后焦点必须落主内容区");
}

/* 5) 托盘开关接线：特性面点亮行+勾选态；change → 桥落盘 */
{
  await new Promise((r) => setTimeout(r, 250)); // nativeApiReady 轮询后装配
  assert.equal(trayRow.hidden, false, "桥就绪后托盘开关行必须点亮");
  assert.equal(trayToggle.checked, true, "初始勾选态来自特性面");
  trayToggle.listeners.get("change")({});
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(bridgeCalls.at(-1), ["set_exit_on_close", true]);

  assert.equal(cacheSection.hidden, false, "界面缓存行点亮");
  assert.match(cacheSize.textContent, /1\.0 MB/);
  cacheButton.listeners.get("click")({});
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(bridgeCalls.at(-1), ["request_webview_cache_clear"]);
  assert.match(cacheButton.textContent, /已登记，重启应用后生效/);
  assert.equal(cacheButton.disabled, true, "登记后按钮禁用防重复");
}

/* 6) 主题同步挂钩：applyTheme 走桥 set_titlebar_theme（桩已记录，形状合同） */
{
  const { applyTheme } = await import("../frontend/modules/shell.js");
  const before = bridgeCalls.length;
  applyTheme("light", { transition: false });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(bridgeCalls.length, before + 1, "主题切换必须同步一次标题栏");
  assert.deepEqual(bridgeCalls.at(-1), ["set_titlebar_theme", false]);
}

/* 7) E4：桥晚到（慢机器）——pywebviewready 事件点亮，不限时 */
{
  // 新装一套独立记录器，避免污染前 6 组的全局桩
  const lateListeners = new Map();
  const lateRow = Object.assign(new globalThis.Element(), { hidden: true });
  const lateToggle = Object.assign(new globalThis.Element(), {
    checked: false,
    addEventListener(type, fn) { this[`on_${type}`] = fn; },
  });
  const byIdLate = (id) => (id === "settings-tray-row" ? lateRow
    : id === "settings-exit-on-close" ? lateToggle : null);
  const savedDoc = globalThis.document;
  const savedWin = globalThis.window;
  globalThis.document = {
    documentElement: { dataset: { theme: "dark" } },
    getElementById: byIdLate,
    addEventListener: () => { },
  };
  globalThis.window = Object.assign(Object.create(Object.getPrototypeOf(globalThis.window)), globalThis.window);
  globalThis.window.addEventListener = (type, fn) => lateListeners.set(type, fn);
  globalThis.window.removeEventListener = (type) => lateListeners.delete(type);
  delete globalThis.window.pywebview;

  installNativeWindowShell();
  assert.equal(lateRow.hidden, true, "桥未到前开关行保持隐藏");
  // 桥 30 秒后才到（模拟慢机器）：事件点亮，轮询弃权无关紧要
  globalThis.window.pywebview = {
    api: {
      get_window_features: async () => ({ native_window: true, features_version: 2, exit_on_close: true, webview_cache_bytes: 0, can_clear_webview_cache: true }),
      set_titlebar_theme: async () => ({ ok: true }),
    },
  };
  const onReady = lateListeners.get("pywebviewready");
  assert.equal(typeof onReady, "function", "pywebviewready 监听必须常驻");
  onReady();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(lateRow.hidden, false, "晚到桥必须点亮开关行");
  assert.equal(lateToggle.checked, true);

  globalThis.document = savedDoc;
  globalThis.window = savedWin;
}

console.log("native shell behavior: 7 scenario groups OK");
