import assert from "node:assert/strict";
import { readModuleSource } from "./frontend_exec_harness.mjs";
import { installStagePreload } from "../frontend/modules/stage-preload.js";

/* PERF-C23 舞台级预载提示行为钉（夜批15 R2 C2-3 定谳落地）。
 *
 * 钉三面：
 * ① 行为矩阵（真实模块 + 最小 DOM 桩）：
 *    - 防闪 500ms：快网元数据（<500ms 到位）→ 提示永不出现；
 *    - 慢网：500ms 到点亮（恢复面板可见则互斥不亮）；loadedmetadata 平滑让位
 *      （先 .is-leaving 淡出、兜底后摘除，禁瞬跳）；error 同收；
 *    - activeLecture 归空收；切讲重武装（计时器重置）；cleanup 全对称。
 * ② 闭集文案钉：index.html 内「正在准备本讲视频…」「视频就绪前，可先看字幕」
 *    + role="status"（SR 同步获知，讲人话纪律）。
 * ③ 形态钉：pages.css 恒暗族 token 绑定、reduced-motion 压平依赖、forced-colors
 *    收录、app.js 接线；player-core 零引用自证（纯观察者纪律）。 */

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* —— 最小 DOM 桩：EventTarget 基座 + hidden/classList 语义 —— */
class StubElement extends EventTarget {
  constructor(id) {
    super();
    this.id = id;
    this.hidden = true;
    this.classList = {
      set: new Set(),
      add(name) { this.set.add(name); },
      remove(name) { this.set.delete(name); },
      contains(name) { return this.set.has(name); },
    };
    this.rect = { height: 484 };
    this.style = {
      props: new Map(),
      setProperty(name, value) { this.props.set(name, value); },
      getPropertyValue(name) { return this.props.get(name) ?? ""; },
    };
  }
  getBoundingClientRect() { return this.rect; }
}

class StubStore {
  constructor() { this.activeLecture = null; this.listeners = new Set(); }
  subscribe(_key, listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  emit(lecture) {
    this.activeLecture = lecture;
    for (const listener of [...this.listeners]) listener(lecture);
  }
}

/* —— ① 行为矩阵 —— */
async function verifyBehaviorMatrix() {
  const video = new StubElement("player-stage");
  const hint = new StubElement("player-stage-preload");
  const recovery = new StubElement("player-recovery");
  const placeholder = new StubElement("player-placeholder");
  placeholder.hidden = false; /* 真实 DOM：占位盒初始可见（index.html 无 hidden），预载期保持可见 */
  const registry = {
    "player-stage": video,
    "player-stage-preload": hint,
    "player-recovery": recovery,
    "player-placeholder": placeholder,
  };
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  globalThis.document = { getElementById: (id) => registry[id] || null };
  globalThis.window = { setTimeout, clearTimeout };
  try {
    /* 快网：元数据 480ms 就位（<500ms 阈）——提示须永不出现 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      assert.ok(typeof cleanup === "function", "安装返回 cleanup");
      assert.equal(hint.hidden, true, "初始静默");
      store.emit({ sub_id: "A1" });
      await sleep(120);
      video.dispatchEvent(new Event("loadedmetadata"));
      await sleep(480);
      assert.equal(hint.hidden, true, "快网：就绪先于防闪窗，提示永不出现（禁闪烁）");
      assert.equal(hint.classList.contains("is-leaving"), false, "快网：零让位残留");
      cleanup();
    }

    /* 慢网：500ms 到点亮 → loadedmetadata → 平滑让位（先淡出后摘除） */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      store.emit({ sub_id: "A2" });
      await sleep(120);
      assert.equal(hint.hidden, true, "防闪窗内保持静默");
      await sleep(460); /* 越过 500ms 阈 */
      assert.equal(hint.hidden, false, "慢网：防闪 500ms 到点亮预载提示");
      assert.equal(hint.style.props.get("--preload-stage-half"), "242px",
        "亮起前以占位盒实测半高锚定可见舞台中心（预载期 stage-fit 塌缩 0 高）");
      assert.equal(hint.classList.contains("is-leaving"), false, "亮起态不带让位类");
      video.dispatchEvent(new Event("loadedmetadata"));
      assert.equal(hint.classList.contains("is-leaving"), true, "让位先淡出（.is-leaving），禁瞬跳");
      assert.equal(hint.hidden, false, "淡出未完成前不摘除");
      await sleep(300);
      assert.equal(hint.hidden, true, "兜底 240ms 后摘除（平滑让位完成）");
      assert.equal(hint.classList.contains("is-leaving"), false, "摘除后让位类清零");
      cleanup();
    }

    /* 失败：error 即收，不叠加「准备中」（恢复面板接管面） */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      store.emit({ sub_id: "A3" });
      await sleep(560);
      assert.equal(hint.hidden, false, "失败前置态：提示已亮");
      video.dispatchEvent(new Event("error"));
      await sleep(300);
      assert.equal(hint.hidden, true, "error 即收（恢复面板互斥纪律）");
      cleanup();
    }

    /* 互斥：恢复面板可见时防闪窗到点不亮 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      recovery.hidden = false;
      store.emit({ sub_id: "A4" });
      await sleep(560);
      assert.equal(hint.hidden, true, "恢复面板可见=不叠加预载提示");
      recovery.hidden = true;
      cleanup();
    }

    /* 归空：activeLecture null 即收 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      store.emit({ sub_id: "A5" });
      await sleep(560);
      assert.equal(hint.hidden, false, "归空前：提示已亮");
      store.emit(null);
      await sleep(300);
      assert.equal(hint.hidden, true, "讲次归空即收（无讲次无提示）");
      cleanup();
    }

    /* 切讲连续：A 提示已亮时切 B——「正在准备」语义对新讲次仍真，
       提示保持连续（收-亮往返=闪烁，禁止）；B 未就绪期间不得掉灯 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      store.emit({ sub_id: "B1" });
      await sleep(560);
      assert.equal(hint.hidden, false, "A 讲次：提示已亮");
      store.emit({ sub_id: "B2" }); /* 未就绪直接切讲 */
      await sleep(120);
      assert.equal(hint.hidden, false, "切讲提示连续（零闪烁）");
      await sleep(600);
      assert.equal(hint.hidden, false, "B 防闪窗后仍亮（连续不中断）");
      video.dispatchEvent(new Event("loadedmetadata"));
      assert.equal(hint.classList.contains("is-leaving"), true, "B 就绪：平滑让位照常");
      await sleep(300);
      assert.equal(hint.hidden, true, "B 就绪后让位收口");
      cleanup();
    }

    /* 切讲打断让位：让位中途切新讲——取消摘除、回到亮起态，随后照常收口 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      store.emit({ sub_id: "C1" });
      await sleep(560);
      video.dispatchEvent(new Event("loadedmetadata"));
      assert.equal(hint.classList.contains("is-leaving"), true, "C1 就绪进入让位");
      store.emit({ sub_id: "C2" }); /* 让位中途切讲 */
      assert.equal(hint.classList.contains("is-leaving"), false, "打断让位：淡出类即时撤销");
      assert.equal(hint.hidden, false, "回到亮起态（新讲次预载接续）");
      video.dispatchEvent(new Event("loadedmetadata"));
      await sleep(300);
      assert.equal(hint.hidden, true, "C2 就绪后让位收口");
      cleanup();
    }

    /* cleanup 对称：退订+拆监听，之后 store/事件均不再驱动 */
    {
      const store = new StubStore();
      const cleanup = installStagePreload(store);
      cleanup();
      store.emit({ sub_id: "C1" });
      await sleep(560);
      assert.equal(hint.hidden, true, "cleanup 后：store 写入不再武装");
      video.dispatchEvent(new Event("loadedmetadata"));
      await sleep(300);
      assert.equal(hint.hidden, true, "cleanup 后：video 事件不再驱动");
    }

    /* 无舞台面（异常装配）：静默不装，不抛 */
    {
      delete registry["player-stage-preload"];
      const store = new StubStore();
      const bare = installStagePreload(store);
      assert.equal(bare, undefined, "缺舞台面时静默不装");
      registry["player-stage-preload"] = hint;
    }
  } finally {
    globalThis.document = previousDocument;
    globalThis.window = previousWindow;
  }
}

/* —— ②③ 静态钉 —— */
async function verifyStaticLocks() {
  const moduleSource = readModuleSource("../frontend/modules/stage-preload.js");
  const indexHtml = readModuleSource("../frontend/index.html");
  const pagesCss = readModuleSource("../frontend/styles/pages.css");
  const appJs = readModuleSource("../frontend/app.js");
  const playerCore = readModuleSource("../frontend/modules/player-core.js");

  assert.ok(moduleSource.includes("PRELOAD_HINT_DELAY_MS = 500"), "防闪阈值 500ms 常量在位");
  assert.ok(moduleSource.includes("HIDE_FALLBACK_MS = 240"), "让位兜底 240ms 常量在位");
  assert.ok(!/^\s*import[\s\S]*?player-core/m.test(moduleSource.split("*/").pop()), "纯观察者：代码面零 player-core import（播放逻辑面零触碰）");
  assert.ok(!/\bplay\b\(|\.pause\(/.test(moduleSource), "纯观察者：不驱动播放");

  /* 闭集文案（讲人话+不承诺时长） */
  assert.ok(indexHtml.includes('id="player-stage-preload"'), "舞台预载提示节点存在");
  assert.ok(indexHtml.includes("正在准备本讲视频…"), "预载主文案闭集");
  assert.ok(indexHtml.includes("视频就绪前，可先看字幕"), "预载副文案闭集（就把空窗变成可用的字幕时间）");
  const preloadTag = indexHtml.match(/<div id="player-stage-preload"[^>]*>/)?.[0] || "";
  assert.ok(preloadTag.includes('role="status"'), "预载提示 role=status（SR 学生同获安抚）");
  assert.ok(preloadTag.includes("hidden"), "初始 hidden（快网学生永不见）");

  /* 形态：恒暗族 token 绑定 + 让位过渡 + reduced-motion 压平依赖 + forced-colors */
  assert.ok(pagesCss.includes(".player-stage-preload[hidden] { display: none; }"), "hidden 展示规则显式");
  assert.ok(pagesCss.includes(".player-stage-preload.is-leaving { opacity: 0; }"), "让位淡出规则在位");
  const posBlock = pagesCss.match(/\.player-stage-preload\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(posBlock.includes("bottom: var(--preload-stage-half"), "底边锚定（swap 前后 fit 底边恒定，让位不跳位）");
  assert.ok(!posBlock.includes("top:"), "禁 top 锚（video 挂载致 fit 顶边上移，会让位甩出屏外——实测回归）");
  const preloadBlock = pagesCss.match(/\.player-stage-preload\s*\{([^}]*)\}/)?.[1] || "";
  assert.ok(preloadBlock.includes("var(--stage-surface)"), "舞台恒暗面 token（双主题按构造恒常）");
  assert.ok(preloadBlock.includes("var(--stage-ink)"), "舞台恒暗墨 token");
  assert.ok(preloadBlock.includes("opacity var(--motion)"), "让位过渡绑 --motion token");
  assert.ok(pagesCss.includes(".player-stage-preload-ring"), "预载转圈环规则在位");
  assert.ok(pagesCss.includes("animation: player-spinner-rotate 800ms linear infinite;\n}\n.player-stage-preload-primary")
    || [...pagesCss.matchAll(/animation: player-spinner-rotate 800ms linear infinite;/g)].length >= 2,
    "转圈复用 spinner 800ms 动画（reduced-motion 全局压平依赖）");
  assert.ok(
    [...pagesCss.matchAll(/\.player-timeline-bubble, \.player-stage-spinner, \.player-stage-preload \{ border/g)].length === 1,
    "forced-colors 收录预载提示（高对比模式边框/底色回退）",
  );
  const reduceBlock = pagesCss.match(/@media \(prefers-reduced-motion: reduce\)/);
  assert.ok(reduceBlock === null, "reduce 语义单源 accessibility.css 全局规则（不在 pages.css 重复）");
  const accessibilityCss = readModuleSource("../frontend/styles/accessibility.css");
  assert.ok(accessibilityCss.includes("transition-duration: .01ms"), "全局压平规则覆盖 transition（让位瞬时降级依赖）");

  /* 接线：app.js 装配（player-core 之后、cleanup 收集既有机制） */
  assert.ok(appJs.includes('installStagePreload } from "./modules/stage-preload.js"'), "app.js 导入预载提示安装器");
  assert.ok(/installPlayerCore,\s*\n\s*installStagePreload,/.test(appJs), "app.js 装配序：紧随 player-core");
}

await verifyBehaviorMatrix();
await verifyStaticLocks();
console.log("frontend stage preload behavior passed");
