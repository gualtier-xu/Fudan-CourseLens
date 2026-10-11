import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/* 视觉回归基线采集 harness（0.1.1-A VISUAL-BASELINE；VISUAL-BASELINE-2 扩展状态组合）。

   用 playwright（headless chromium，无 GUI 依赖）对合成壳服务器
   （tests/synthetic_shell_server.py，既有测试基建）服务的真实前端做
   全页截图：5 页面 × 2 主题 × 3 视口 = 30 页面组合，另加 13 个状态组合
   （今日新/变交互面 × 2 主题 × 2 视口，见 STATE_SPECS）。

   确定性三件套（本 harness 的核心技术点）：
   ① 客户端时钟冻结——addInitScript 在任何页面脚本前把 Date 整体替换为
     固定时刻（本地 2026-10-20 10:08 周二，上午好档、非节日）：问候语时段
     档/星期/节日、诗联确定性轮换（hash(日期,时段档)）、课表「今日/本周」
     选择、主题「自动」解析全部归一。服务器时间戳无论真值，经冻结 Date
     格式化后输出恒同（FrozenDate 构造器恒忽略实参→含 epoch 的格式化输出
     恒为 Invalid Date 文案——这是有意保留的确定性归一，非缺陷）。
     VISUAL-BASELINE-2 修复：冻结时刻插值此前误用 JSON 字符串取属性
     （恒 undefined→Invalid Date→诗联/问候落真实壁钟档），现插值对象本身。
   ② 主题经 localStorage（courselens.theme.v2）在页面装配前显式写入
     light/dark——绕开「自动」档对本机时刻的依赖。
   ③ 韵律归一——context 级 reducedMotion + 截图 animations:"disabled"
     关闭过渡/动画；toast 区域截图前遮蔽（瞬态提示与采集时机无关化）。

   状态组合（VISUAL-BASELINE-2）经页面级 route stub 达成确定性（逐条
   登记进 capture-manifest.json 的 stubNotes）：
   - 设备码 pending/到期两态：POST remote-connection/actions 静态 fulfill
     （绝不放行真实动作——服务端会发起真 GitHub 设备码外呼）。
   - degraded 点名：GET authentication 快照的 connection.services 改写。
   - 直播进入两态：GET timetable 投影改写到冻结周 + GET live-room/status
     改写（observed_at 冻结-20s，避 300s stale 墙）。
   - 数据页新元素：GET course-data 行 0 注入 not_understood_count/类别/孤儿。
   - 自动续播浮层（855794a L96'）曾按晨裁候选面试采，因「restore 自身
     seek 触发 E 互斥自dismiss + 讲次页直播卡状态轮询两态竞速」双重瞬态
     无法在本 harness 内确定性钉住而撤下（2026-10-09 VISUAL-BASELINE-2，
     详见车道结果文件）；产品侧修复后可按同法重采。

   退出码：0=成功；2=harness/环境错误（基线或运行参数问题，应 FAIL）；
   3=playwright 浏览器缺失（环境未安装，调用方应 SKIP 而非 FAIL）。

   用法：
     node capture.mjs --out <dir> [--base-url URL] [--python EXE]
       [--pages study,live,settings,data,onboarding]
       [--states <name,...>]            # 缺省=全部状态组合
       [--themes light,dark]
       [--viewports 375x812,768x1024,1440x900]
       [--quality 85] [--max-bytes 307200]

   产物：<dir>/<page|state>--<theme>--<WxH>.jpg + capture-manifest.json。 */

const EXIT_OK = 0;
const EXIT_ERROR = 2;
const EXIT_NO_BROWSER = 3;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(HERE, "..", "..");

/* 固定时刻：本地 2026-10-20（周二）10:08——上午好档、非节日映射表命中、
   学期内（合成学期起点=运行日-52 天，固定日期落在其第 8-11 周，
   week_indexes 1..18 全覆盖，周次漂移不改变可见课集）。 */
const FROZEN_LOCAL = { year: 2026, month: 9, day: 20, hour: 10, minute: 8, second: 0 };
const FROZEN_EPOCH_SEC = Math.floor(
  new Date(FROZEN_LOCAL.year, FROZEN_LOCAL.month, FROZEN_LOCAL.day, FROZEN_LOCAL.hour, FROZEN_LOCAL.minute, FROZEN_LOCAL.second).getTime() / 1000,
);
const TODAY_ISO = `${FROZEN_LOCAL.year}-10-20`;

const DEFAULT_PAGES = ["study", "live", "settings", "data", "onboarding"];
const DEFAULT_THEMES = ["light", "dark"];
const DEFAULT_VIEWPORTS = [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1440, height: 900 },
];
/* 状态组合按面适配视口：移动 + 桌面双档（中档平板由页面组合覆盖）。 */
const STATE_VIEWPORTS = [
  { width: 375, height: 812 },
  { width: 1440, height: 900 },
];

function parseArgs(argv) {
  const args = {
    out: null,
    baseUrl: null,
    python: process.env.COURSELENS_VISUAL_PYTHON || "python",
    pages: DEFAULT_PAGES,
    states: null, /* null = 全部状态组合 */
    themes: DEFAULT_THEMES,
    viewports: DEFAULT_VIEWPORTS,
    quality: 85,
    maxBytes: 307200,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const value = argv[i + 1];
    if (flag === "--out") { args.out = value; i += 1; }
    else if (flag === "--base-url") { args.baseUrl = value; i += 1; }
    else if (flag === "--python") { args.python = value; i += 1; }
    else if (flag === "--pages") { args.pages = value.split(",").map((s) => s.trim()).filter(Boolean); i += 1; }
    else if (flag === "--states") { args.states = value.split(",").map((s) => s.trim()).filter(Boolean); i += 1; }
    else if (flag === "--themes") { args.themes = value.split(",").map((s) => s.trim()).filter(Boolean); i += 1; }
    else if (flag === "--viewports") {
      args.viewports = value.split(",").map((piece) => {
        const [w, h] = piece.trim().toLowerCase().split("x").map((n) => Number(n));
        if (!Number.isInteger(w) || !Number.isInteger(h) || w <= 0 || h <= 0) {
          throw new Error(`invalid viewport: ${piece}`);
        }
        return { width: w, height: h };
      });
      i += 1;
    } else if (flag === "--quality") { args.quality = Number(value); i += 1; }
    else if (flag === "--max-bytes") { args.maxBytes = Number(value); i += 1; }
    else { throw new Error(`unknown flag: ${flag}`); }
  }
  if (!args.out) throw new Error("--out is required");
  for (const page of args.pages) {
    if (!/^[a-z-]+$/.test(page)) throw new Error(`invalid page name: ${page}`);
  }
  for (const theme of args.themes) {
    if (theme !== "light" && theme !== "dark") throw new Error(`theme must be light|dark: ${theme}`);
  }
  if (!Number.isInteger(args.quality) || args.quality < 30 || args.quality > 100) {
    throw new Error(`--quality must be an integer in [30, 100]: ${args.quality}`);
  }
  if (!Number.isInteger(args.maxBytes) || args.maxBytes <= 0) {
    throw new Error(`--max-bytes must be a positive integer: ${args.maxBytes}`);
  }
  return args;
}

/* 合成壳服务器：--port 0 自动分配，stdout 打印 http://127.0.0.1:<port> 即就绪。
   --onboarding-guide completed：引导不自动弹层，新手引导页仍可经
   courselens:select-page 显式导航截图。临时根走 runtime/cache（.gitignore 内）。
   VISUAL-BASELINE-2：服务器档位（profile）可带种子/引导态参数，状态组合
   按需声明所属档位，harness 惰性起停。 */
const SERVER_PROFILES = {
  default: [],
  "onboarding-new": ["--onboarding-guide", "new"],
  /* HARNESS-FIX-1：种子冻结钟（--seed-clock frozen）——种子时间戳锚定相对
     FROZEN_EPOCH 定值，抽屉 fresh/stale/elapsed 渲染与「服务启动→采集」墙钟
     间隔解耦（根修 VISBASE-2-R2 实证的 20s TTL 混切与「已运行 X 分钟」漂移）。 */
  "tasks-seeded": ["--seed-clock", "frozen", "--seed", "tasks-active", "--seed", "tasks-failed", "--seed", "tasks-completed"],
};

async function startSyntheticServer(python, extraArgs = []) {
  const scratch = path.join(PROJECT_ROOT, "runtime", "cache", "visual-baseline", `capture-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`);
  mkdirSync(scratch, { recursive: true });
  const child = spawn(python, [
    "-m", "tests.synthetic_shell_server",
    "--port", "0",
    "--onboarding-guide", "completed",
    "--cache-root", scratch,
    ...extraArgs,
  ], {
    cwd: PROJECT_ROOT,
    env: { ...process.env, PYTHONPATH: PROJECT_ROOT, PYTHONUTF8: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`synthetic shell not ready in 60s\nstderr: ${stderr.slice(-2000)}`)), 60000);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      const match = stdout.match(/http:\/\/127\.0\.0\.1:(\d+)/);
      if (match) {
        clearTimeout(timer);
        resolve({ url: `http://127.0.0.1:${match[1]}`, scratch });
      }
    });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`synthetic shell exited early (code ${code})\nstderr: ${stderr.slice(-2000)}`));
    });
  });
  try {
    const readyInfo = await ready;
    return { child, ...readyInfo };
  } catch (error) {
    child.kill();
    throw error;
  }
}

function stopSyntheticServer(server) {
  try { server.child.kill(); } catch { /* 已退出即达成 */ }
  try { rmSync(server.scratch, { recursive: true, force: true }); } catch { /* gitignored 残留可容忍 */ }
}

/* 页面加载前注入：时钟冻结 + 主题种子。必须在任何产品脚本前执行。 */
function bootNormalizationScript(theme) {
  const themeJson = JSON.stringify(theme);
  /* 冻结时刻逐字段插值对象本身（VISUAL-BASELINE-2 修复：此前误插值
     JSON 字符串的属性→恒 undefined→Invalid Date→冻结从未生效）。 */
  const { year, month, day, hour, minute, second } = FROZEN_LOCAL;
  return `
(() => {
  const RealDate = Date;
  // 以真实本地时区换算固定时刻的 epoch：任何机器上冻结的都是同一本地壁钟面。
  const fixedEpoch = new RealDate(${year}, ${month}, ${day}, ${hour}, ${minute}, ${second}).getTime();
  class FrozenDate extends RealDate {
    constructor(...args) { super(fixedEpoch); }
    static now() { return fixedEpoch; }
  }
  FrozenDate.parse = RealDate.parse;
  FrozenDate.UTC = RealDate.UTC;
  window.Date = FrozenDate;
  window.localStorage.setItem("courselens.theme.v2", ${themeJson});
})();
`;
}

/* 截图前归一：遮蔽瞬态 toast（与采集时机无关化）。产品 CSP 禁内联脚本，
   经 page.evaluate 执行 DOM 归一不落任何产品文件。 */
async function normalizeTransientUi(page) {
  await page.evaluate(() => {
    document.querySelectorAll(".toast-region").forEach((node) => {
      node.style.display = "none";
    });
  });
}

/* 合成壳=已认证就绪态：顶栏账户簇从启动期「登录/正在连接」收敛为「账户」。
   所有页面都依赖 boot 完成，先等这个全局信号再导航/截图；超时=本次尝试
   作废，交由上层重试（合成服务器偶发慢响应不允许落进基线）。 */
async function waitAuthReady(page, timeoutMs = 12000) {
  await page.waitForFunction(
    () => document.querySelector("#account-text")?.textContent === "账户",
    undefined, { timeout: timeoutMs },
  ).catch(() => {});
}

/* onboarding 页内容只在 openGuide 流程加载（selectPage 仅切壳可见性，
   不触发引导状态读取）：走产品真实入口=账户菜单 →「新手引导」。 */
async function openOnboardingViaMenu(page) {
  await page.click("#account-button", { timeout: 5000 });
  await page.click("#account-menu-onboarding", { timeout: 5000 });
}

async function settlePage(page, pageName, { isState = false } = {}) {
  await page.evaluate(() => document.fonts?.ready);
  const waiters = [];
  if (isState) {
    /* 状态组合自行在 run/ready 内等待目标面：这里只做韵律归一。 */
  } else if (pageName === "study") {
    /* 学习页基线拍默认空态（study-select 隐藏）：只等可见面就位。
       隐藏的 live 卡内部状态不进像素、也不校验——其快照依赖链存在
       与像素无关的启动竞态，校验它会把假报警带进正典。 */
    waiters.push(page.waitForSelector(`#study-select:not([hidden]), #study-empty:not([hidden])`, {
      state: "attached", timeout: 8000,
    }).catch(() => {}));
    /* STUDY-STATS-GFIX-1：默认层卡水合竞态钉——#study-week-card 的 boot 异步
       水合（study/overview）晚于区域可见；截图若落在水合前，「正在读取学习
       面貌…」占位与引导态终帧像素不同（实测间歇 7-22% 假报警）。等占位行
       退场（或超时放行由既有 9s 静置兜底）再进双 rAF。 */
    waiters.push(page.waitForFunction(() => {
      const card = document.querySelector("#study-week-card");
      return Boolean(card && !/正在读取学习面貌/.test(card.textContent || ""));
    }, undefined, { timeout: 8000 }).catch(() => {}));
  } else if (pageName === "onboarding") {
    waiters.push(page.waitForSelector(`#onboarding-pending`, {
      state: "hidden", timeout: 8000,
    }).catch(() => {}));
  } else if (pageName === "live") {
    /* live 页课表/状态快照竞速（VISBASE-2-R2 记档既有间歇项）：status
       loading/unknown 瞬态渲染「直播状态待确认」，定态=「课表快照里今晚
       没有排直播。」诚实空态。等瞬态清出像素再拍；状态组合 live-single-*
       自带 run/ready 门，不走此分支。 */
    waiters.push(page.waitForFunction(() => {
      const pageEl = document.querySelector("[data-page='live']");
      return Boolean(pageEl && !pageEl.innerText.includes("直播状态待确认"));
    }, undefined, { timeout: 8000 }).catch(() => {}));
  } else {
    waiters.push(page.waitForSelector(`[data-page="${pageName}"].active`, {
      state: "attached", timeout: 8000,
    }).catch(() => {}));
  }
  await Promise.race([
    Promise.all(waiters),
    new Promise((resolve) => setTimeout(resolve, 9000)),
  ]);
  // 双 rAF + 短静置：让合成壳就绪后的最后一次渲染/字体回退完成。
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await page.waitForTimeout(400);
  await normalizeTransientUi(page);
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
}

/* 期望表面状态校验：只接受「认证就绪 + 目标页激活 + 该页无加载中占位」的
   状态进基线——把启动竞态挡在截图之外（不校验就会把 boot 中途态钉进基线）。
   原则=只校验进像素的可见面。 */
async function surfaceReady(page, pageName) {
  return page.evaluate((name) => {
    const authed = document.querySelector("#account-text")?.textContent === "账户";
    if (!authed) return false;
    const active = document.querySelector(`[data-page="${name}"]`);
    if (!active || active.hidden || !active.classList.contains("active")) return false;
    if (name === "study") {
      const empty = document.querySelector("#study-empty");
      const select = document.querySelector("#study-select");
      return Boolean((empty && !empty.hidden) || (select && !select.hidden));
    }
    if (name === "onboarding") {
      const pending = document.querySelector("#onboarding-pending");
      if (pending && !pending.hidden) return false;
      return Boolean(document.querySelector('[data-onboarding-panel]:not([hidden])'));
    }
    if (name === "live") {
      /* 快照竞速守门：瞬态「直播状态待确认」不许进基线（3 次尝试级重试兜底）。 */
      const text = document.querySelector("[data-page='live']")?.innerText || "";
      return !text.includes("直播状态待确认");
    }
    return true;
  }, pageName);
}

async function captureShot(page, { filePath, quality, maxBytes }) {
  let q = quality;
  for (;;) {
    const buffer = await page.screenshot({
      path: filePath,
      fullPage: true,
      type: "jpeg",
      quality: q,
      animations: "disabled",
      caret: "hide",
    });
    if (buffer.length <= maxBytes || q <= 50) {
      return { bytes: buffer.length, quality: q };
    }
    q = Math.max(50, q - 15);
  }
}

/* ---- VISUAL-BASELINE-2：状态组合的页面级确定性 stub ----
   全部走 page.route（页面内生效，组合间零串扰）；逐条 note 登记进 manifest。
   铁律：动作类 POST 一律静态 fulfill（绝不 route.fetch 放行——服务端会
   对 github.com 发起真实外呼）；读取类 GET 允许 fetch+mutate（本地只读）。 */

const apiEnvelope = (data) => ({ schema: "courselens.api.v3", data });

function makeStub(note, test, fulfill) {
  return {
    note,
    test,
    fulfill,
  };
}

/* 单一 page.route 处理器按序匹配全部 stub。铁律：playwright 多 route
   处理器是 LIFO 且 route.continue() 即终结——逐 stub 注册会让次序在后的
   stub 永远拿不到请求（VISUAL-BASELINE-2 实测：live 态丢课表改写、
   续播态丢进度改写，状态静默降级）。 */
function attachStubs(page, stubs) {
  if (stubs.length === 0) return Promise.resolve();
  return page.route("**/*", async (route) => {
    try {
      const request = route.request();
      const url = request.url();
      if (!url.startsWith("http://127.0.0.1")) return route.abort();
      for (const stub of stubs) {
        if (!stub.test(url, request)) continue;
        const body = await stub.fulfill(url, request, route);
        if (body && body.__passthrough) return route.fulfill({ response: body.response });
        if (body && body.__rawMedia) {
          return route.fulfill({
            status: body.__status || 200,
            contentType: "video/mp4",
            headers: {
              "Accept-Ranges": "bytes",
              ...(body.__contentRange ? { "Content-Range": body.__contentRange } : {}),
            },
            body: body.__rawMedia,
          });
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
      }
      return route.continue();
    } catch {
      return route.abort().catch(() => {});
    }
  });
}

/* 读取类 GET：放行真实请求后按闭集改写响应数据。 */
function mutateGetStub(note, urlTest, mutator) {
  return makeStub(
    note,
    (url, request) => request.method() === "GET" && urlTest(url),
    async (url, request, route) => {
      const response = await route.fetch();
      let body;
      try { body = await response.json(); } catch { return { __passthrough: true, response }; }
      mutator(body?.data ?? body, url);
      return body;
    },
  );
}

/* 设备码授权动作：静态 pending 信封（绝不放行真实服务端动作）。 */
function deviceCodeStub(expiresAtSec) {
  return makeStub(
    "remote-actions->pending(static,无服务端外呼)",
    (url, request) => url.includes("/api/v3/remote-connection/actions") && request.method() === "POST",
    async () => apiEnvelope({
      operation: {
        state: "succeeded",
        result: { state: "pending", user_code: "ABCD-EF12", interval: 30, expires_at: expiresAtSec },
      },
    }),
  );
}

const AUTH_DEGRADED_SERVICES = { webvpn: { state: "unavailable" }, icourse: { state: "ready" } };

/* postSettle 整数锚定滚动：settlePage（字体就绪+双 rAF）之后再滚动，
   并按 getBoundingClientRect 的整数差值校正——scrollIntoView 的子像素
   取整会随后续回流漂移 2-3px，把整页文本推进超差（run11 vs run12 实测）。 */
async function scrollToAnchor(page, selector, topOffset) {
  await page.evaluate(({ selector, topOffset }) => {
    const node = document.querySelector(selector);
    if (!node) return;
    node.scrollIntoView({ block: "start", behavior: "instant" });
    let scroller = node.parentElement;
    while (scroller && scroller !== document.body && scroller.scrollHeight <= scroller.clientHeight + 1) {
      scroller = scroller.parentElement;
    }
    const target = scroller && scroller !== document.body ? scroller : (document.scrollingElement || document.documentElement);
    const currentTop = Math.round(node.getBoundingClientRect().top);
    target.scrollTop += currentTop - topOffset;
  }, { selector, topOffset });
}

const authDegradedStub = mutateGetStub(
  "authentication->connection degraded(WebVPN unavailable/iCourse ready)",
  (url) => url.includes("/api/v3/authentication"),
  (data) => {
    const connection = data?.connection;
    if (!connection) return;
    connection.state = "degraded";
    connection.services = AUTH_DEGRADED_SERVICES;
    connection.observed_at = FROZEN_EPOCH_SEC - 30;
    connection.expires_at = FROZEN_EPOCH_SEC + 300;
  },
);

/* 课表快照投影改写到冻结周：周二（冻结今日）一场 09:55-11:35 会议。
   供需要课表内容入像素的状态使用（概要行/安排卡/直播卡），消除真实周
   数据的按日漂移。 */
const timetableNowStub = mutateGetStub(
  "timetable->frozen-week(2026-10-19..25)+Tue 09:55-11:35 meeting",
  (url) => url.includes("/api/v3/timetable?"),
  (data) => {
    data.current_week = 10;
    data.selected_week = 10;
    data.week_start = "2026-10-19";
    data.week_end = "2026-10-25";
    const base = (data.days || []).find((day) => day.weekday === 2) || (data.days || [])[1];
    if (!base) return;
    const proto = base.meetings?.[0] || {};
    const meeting = {
      ...proto,
      meeting_id: "frozen-live-meeting",
      timetable_course_id: "fixture:90001",
      catalog_course_id: "90001",
      title: "数据结构 2",
      teachers: ["林老师"],
      room: "HGX502",
      course_code: "TEST0001",
      week: 10,
      weekday: 2,
      start_unit: 3,
      end_unit: 4,
      date: TODAY_ISO,
      start_time: "09:55",
      end_time: "11:35",
      starts_at: `${TODAY_ISO}T09:55:00+08:00`,
      ends_at: `${TODAY_ISO}T11:35:00+08:00`,
    };
    base.date = TODAY_ISO;
    base.meetings = [meeting];
    data.current_meeting = meeting;
    data.next_meeting = null;
  },
);

/* 课表快照投影改写到冻结周：周二（冻结今日）一场 09:55-11:35 会议。
   供需要课表内容入像素的状态使用（概要行/安排卡/直播卡/今晚在播），
   消除真实周数据的按日漂移。 */
/* live 页课表快照重播（竞速解耦）：boot 期 timetable 渲染与 app.js 模块
   安装、目录加载存在多重竞速——先激活 live 页，等课表改写渲染落地后，
   反复重播应用自己广播过的同一快照（零改写）直到会场面入像素。
   快照 recorder 走 init 脚本（文档起任何页面脚本前注册）。 */
function timetableReplayInit(page) {
  return page.addInitScript(() => {
    window.__vbTimetable = null;
    window.addEventListener("courselens:timetable-snapshot", (event) => {
      window.__vbTimetable = event?.detail || null;
    });
  });
}

async function timetableReplayUntilPanel(page) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    await page.evaluate(() => {
      if (window.__vbTimetable) {
        window.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: window.__vbTimetable }));
      }
    });
    const hit = await page.waitForFunction(() => {
      const pageEl = document.querySelector("[data-page='live']");
      return Boolean(pageEl && pageEl.innerText.includes("数据结构 2"));
    }, undefined, { timeout: 1500 }).catch(() => false);
    if (hit) return;
  }
}

const liveStatusReadyStub = mutateGetStub(
  "live-room/status->live+can_enter(observed 冻结-20s)",
  (url) => url.includes("/api/v3/live-room/status"),
  (data) => {
    data.state = "live";
    data.can_enter = true;
    data.observed_at = FROZEN_EPOCH_SEC - 20;
    data.expires_at = FROZEN_EPOCH_SEC + 60;
  },
);

const dataNewElementsStub = mutateGetStub(
  "course-data->row0 not_understood_count=7+categories+orphan 2",
  (url) => url.includes("/api/v3/course-data?"),
  (data) => {
    if (data.orphan_artifacts) {
      data.orphan_artifacts.directories = 2;
      data.orphan_artifacts.bytes = 5242880;
    }
    const row = (data.rows || [])[0];
    if (row) {
      row.not_understood_count = 7;
      row.categories = {
        documents: { count: 3, text_bytes: 20480 },
        transcripts: { count: 12, text_bytes: 48000 },
      };
    }
  },
);

/* ---- 状态组合清单（VISUAL-BASELINE-2）----
   每项：profile（服务器档位）、stubs（页面级 stub 工厂）、run（产品真实
   路径驱动到目标态）、ready（进像素前的可见面校验，false=本次尝试作废）。 */
const STATE_SPECS = {
  "settings-device-code": {
    profile: "default",
    stubs: () => [deviceCodeStub(FROZEN_EPOCH_SEC + 15 * 60)],
    async run(page) {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "settings" })));
      /* FREEZE-GATE-1：先显式激活「网络与远程连接」组——侧边栏 active 组高亮
         此前随渲染竞态二变（账户与连接 vs 网络与远程连接，两捕获互差 ~1.1%），
         双捕获互比对拍定位；显式激活后组高亮确定。 */
      await page.evaluate(() => {
        const group = document.querySelector('[data-settings-target="settings-network-group"]');
        if (group) group.click();
      });
      await page.waitForFunction(() => (
        Array.from(document.querySelectorAll("[data-page='settings'] button"))
          .some((b) => b.textContent.trim() === "授权并创建专属仓库")
      ), undefined, { timeout: 8000 }).catch(() => {});
      await page.evaluate(() => {
        const button = Array.from(document.querySelectorAll("[data-page='settings'] button"))
          .find((b) => b.textContent.trim() === "授权并创建专属仓库");
        if (button) button.click();
      });
      await page.waitForFunction(() => {
        const box = document.querySelector("#remote-device-authorization");
        return box && !box.hidden && box.textContent.includes("分钟内有效");
      }, undefined, { timeout: 8000 }).catch(() => {});
    },
    async postSettle(page) {
      /* 设置页是固定高度内滚容器：字体就绪后整数锚定滚动，设备码区进像素。 */
      await scrollToAnchor(page, "#remote-device-authorization", 88);
    },
    async ready(page) {
      return page.evaluate(() => {
        const box = document.querySelector("#remote-device-authorization");
        const rect = box?.getBoundingClientRect();
        return Boolean(box && !box.hidden
          && box.textContent.includes("复制验证码")
          && box.textContent.includes("分钟内有效")
          && box.textContent.includes("请先在浏览器登录你的 GitHub 账号")
          && rect && rect.top >= 0 && rect.bottom <= window.innerHeight);
      });
    },
  },
  "settings-device-code-expiring": {
    profile: "default",
    stubs: () => [deviceCodeStub(FROZEN_EPOCH_SEC - 60)],
    async run(page) {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "settings" })));
      /* FREEZE-GATE-1：同上——显式激活「网络与远程连接」组，消除侧边栏高亮竞态。 */
      await page.evaluate(() => {
        const group = document.querySelector('[data-settings-target="settings-network-group"]');
        if (group) group.click();
      });
      await page.waitForFunction(() => (
        Array.from(document.querySelectorAll("[data-page='settings'] button"))
          .some((b) => b.textContent.trim() === "授权并创建专属仓库")
      ), undefined, { timeout: 8000 }).catch(() => {});
      await page.evaluate(() => {
        const button = Array.from(document.querySelectorAll("[data-page='settings'] button"))
          .find((b) => b.textContent.trim() === "授权并创建专属仓库");
        if (button) button.click();
      });
      await page.waitForFunction(() => {
        const box = document.querySelector("#remote-device-authorization");
        return box && !box.hidden && box.textContent.includes("复制验证码");
      }, undefined, { timeout: 8000 }).catch(() => {});
    },
    async postSettle(page) {
      await scrollToAnchor(page, "#remote-device-authorization", 88);
    },
    async ready(page) {
      return page.evaluate(() => {
        const box = document.querySelector("#remote-device-authorization");
        const rect = box?.getBoundingClientRect();
        return Boolean(box && !box.hidden
          && box.textContent.includes("复制验证码")
          && !box.textContent.includes("分钟内有效")
          && box.textContent.includes("请先在浏览器登录你的 GitHub 账号")
          && rect && rect.top >= 0 && rect.bottom <= window.innerHeight);
      });
    },
  },
  "conn-dialog": {
    profile: "default",
    stubs: () => [],
    async run(page) {
      await page.waitForFunction(() => {
        const pill = document.querySelector("#conn-status");
        return pill && !pill.disabled;
      }, undefined, { timeout: 8000 }).catch(() => {});
      await page.click("#conn-status", { timeout: 5000 });
      await page.waitForTimeout(400);
    },
    async ready(page) {
      return page.evaluate(() => {
        const menu = document.querySelector("#conn-menu");
        return Boolean(menu && !menu.hidden && menu.textContent.includes("复旦课程平台"));
      });
    },
  },
  "conn-dialog-degraded-naming": {
    profile: "default",
    stubs: () => [authDegradedStub],
    async run(page) {
      await page.waitForFunction(() => {
        const pill = document.querySelector("#conn-status");
        return pill && !pill.disabled;
      }, undefined, { timeout: 8000 }).catch(() => {});
      await page.click("#conn-status", { timeout: 5000 });
      await page.waitForTimeout(400);
    },
    async ready(page) {
      return page.evaluate(() => {
        const menu = document.querySelector("#conn-menu");
        return Boolean(menu && !menu.hidden
          && menu.textContent.includes("暂时连不上"));
      });
    },
  },
  "data-course-detail": {
    profile: "default",
    stubs: () => [],
    async run(page) {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "data" })));
      await page.waitForSelector("[data-page='data'] .data-row-title", { state: "visible", timeout: 8000 }).catch(() => {});
      await page.evaluate(() => {
        document.querySelector("[data-page='data'] .data-row-title")?.click();
      });
      /* 窄布局明细渲染进行内 details>.data-row-detail-body，宽布局进
         #data-detail：等待任一容器出现讲次表。 */
      await page.waitForFunction(() => {
        const pageEl = document.querySelector("[data-page='data']");
        return Boolean(pageEl && pageEl.innerText.includes("第 1 讲"));
      }, undefined, { timeout: 10000 }).catch(() => {});
    },
    async postSettle(page) {
      /* 窄布局锚定整个 details 行（summary+明细体同框），宽布局锚 #data-detail。 */
      const hasNarrow = await page.evaluate(() => {
        const details = document.querySelector("[data-page='data'] details[data-course-detail]");
        return Boolean(details && details.textContent.includes("第 1 讲"));
      });
      await scrollToAnchor(page, hasNarrow ? "[data-page='data'] details[data-course-detail]" : "#data-detail", 72);
    },
    async ready(page) {
      return page.evaluate(() => {
        const pageEl = document.querySelector("[data-page='data']");
        if (!pageEl || !pageEl.innerText.includes("第 1 讲")) return false;
        const details = document.querySelector("[data-page='data'] details[data-course-detail]");
        const node = (details && details.textContent.includes("第 1 讲")) ? details : document.querySelector("#data-detail");
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        return rect.top >= 0 && rect.top < window.innerHeight;
      });
    },
  },
  "data-new-elements": {
    profile: "default",
    stubs: () => [dataNewElementsStub],
    async run(page) {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "data" })));
      await page.waitForSelector("[data-page='data'] .data-row-title", { state: "visible", timeout: 8000 }).catch(() => {});
      await page.evaluate(() => {
        document.querySelector("[data-page='data'] .data-row-title")?.click();
      });
      await page.waitForFunction(() => {
        const pageEl = document.querySelector("[data-page='data']");
        return Boolean(pageEl && pageEl.innerText.includes("第 1 讲") && pageEl.innerText.includes("没听懂 7"));
      }, undefined, { timeout: 10000 }).catch(() => {});
    },
    async postSettle(page) {
      /* 窄布局锚定整个 details 行（summary+明细体同框），宽布局锚 #data-detail。 */
      const hasNarrow = await page.evaluate(() => {
        const details = document.querySelector("[data-page='data'] details[data-course-detail]");
        return Boolean(details && details.textContent.includes("第 1 讲"));
      });
      await scrollToAnchor(page, hasNarrow ? "[data-page='data'] details[data-course-detail]" : "#data-detail", 72);
    },
    async ready(page) {
      return page.evaluate(() => {
        const pageEl = document.querySelector("[data-page='data']");
        if (!pageEl || !pageEl.innerText.includes("第 1 讲") || !pageEl.innerText.includes("没听懂 7")) return false;
        const details = document.querySelector("[data-page='data'] details[data-course-detail]");
        const node = (details && details.textContent.includes("第 1 讲")) ? details : document.querySelector("#data-detail");
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        return rect.top >= 0 && rect.top < window.innerHeight;
      });
    },
  },
  "live-single-entry": {
    profile: "default",
    stubs: () => [timetableNowStub, liveStatusReadyStub],
    async init(page) {
      await timetableReplayInit(page);
    },
    async run(page) {
      /* 先激活 live 页再重播冻结周快照（重播直到会场面入像素，绕开
         boot 期课表渲染/目录加载/模块安装三重竞速）；status stub 给
         live+can_enter（observed 冻结-20s 避 300s stale 墙）→ 单次进入
         CTA（进入直播钮）enabled 的进入前态。 */
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "live" })));
      await page.waitForFunction(() => {
        const summary = document.querySelector("#schedule-summary");
        return summary && summary.textContent.includes("第 10 周");
      }, undefined, { timeout: 15000 }).catch(() => {});
      await timetableReplayUntilPanel(page);
      await page.waitForFunction(() => {
        const button = document.querySelector("#enter-live-room");
        return button && !button.hidden && !button.disabled;
      }, undefined, { timeout: 12000 }).catch(() => {});
      await page.waitForTimeout(500);
    },
    async ready(page) {
      return page.evaluate(() => {
        const button = document.querySelector("#enter-live-room");
        const pageEl = document.querySelector("[data-page='live']");
        return Boolean(button && !button.hidden && !button.disabled
          && pageEl && pageEl.innerText.includes("数据结构 2"));
      });
    },
  },
  "live-single-entered": {
    profile: "default",
    stubs: () => [timetableNowStub, liveStatusReadyStub],
    async init(page) {
      await timetableReplayInit(page);
    },
    async run(page) {
      /* 同 live-single-entry：重播冻结周快照后再进入。 */
      await page.evaluate(() => window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "live" })));
      await page.waitForFunction(() => {
        const summary = document.querySelector("#schedule-summary");
        return summary && summary.textContent.includes("第 10 周");
      }, undefined, { timeout: 15000 }).catch(() => {});
      await timetableReplayUntilPanel(page);
      await page.waitForFunction(() => {
        const button = document.querySelector("#enter-live-room");
        return button && !button.hidden && !button.disabled;
      }, undefined, { timeout: 12000 }).catch(() => {});
      await page.evaluate(() => { document.querySelector("#enter-live-room")?.click(); });
      await page.waitForFunction(() => {
        const video = document.querySelector("[data-page='live'] video");
        return video && video.readyState >= 2;
      }, undefined, { timeout: 15000 }).catch(() => {});
      /* 确定性归一：暂停并 seek 到固定帧（合成 HLS 帧随时间变化）。
         产品真实状态机保持「直播中/已播」可见。 */
      await page.evaluate(() => {
        const video = document.querySelector("[data-page='live'] video");
        if (!video) return;
        try {
          video.pause();
          video.currentTime = 1.0;
        } catch { /* 引擎拒绝 seek：按当前帧落基线 */ }
      });
      await page.waitForTimeout(700);
    },
    async ready(page) {
      return page.evaluate(() => {
        const video = document.querySelector("[data-page='live'] video");
        const pageEl = document.querySelector("[data-page='live']");
        return Boolean(video && video.readyState >= 2 && video.paused
          && pageEl && pageEl.innerText.includes("直播中"));
      });
    },
  },
  "onboarding-suspended": {
    profile: "onboarding-new",
    stubs: () => [],
    async run(page) {
      /* 产品真实入口（账户菜单 →「新手引导」；不依赖 boot 一次性 auto-open，
         服务器端 mark-opened 状态不会跨组合漂移）。 */
      await openOnboardingViaMenu(page);
      await page.waitForTimeout(900);
    },
    async ready(page) {
      return page.evaluate(() => {
        const pending = document.querySelector("#onboarding-pending");
        if (pending && !pending.hidden) return false;
        const active = document.querySelector("[data-page='onboarding']");
        return Boolean(active && active.classList.contains("active")
          && document.querySelector('[data-onboarding-panel]:not([hidden])'));
      });
    },
  },
  "home-guide-resume-hint": {
    profile: "onboarding-new",
    stubs: () => [timetableNowStub],
    async run(page) {
      await openOnboardingViaMenu(page);
      await page.waitForTimeout(900);
      /* 仅本次退出：不写引导状态（挂起态保持），主页恢复提示行现身。
         提示行在 .home-overview（#study-select 内）——退出落问候空态后
         经「选择课程」真实入口回到概览，提示行入像素。 */
      await page.evaluate(() => { document.querySelector("#onboarding-dismiss-leave")?.click(); });
      await page.waitForTimeout(700);
      await page.evaluate(() => { document.querySelector("#study-start-select")?.click(); });
      await page.waitForSelector("#study-select:not([hidden])", { state: "attached", timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(500);
    },
    async ready(page) {
      return page.evaluate(() => {
        const hint = document.querySelector("#home-guide-resume");
        const active = document.querySelector("[data-page='study']");
        const rect = hint?.getBoundingClientRect();
        return Boolean(hint && !hint.hidden
          && hint.textContent.includes("继续新手引导")
          && active && active.classList.contains("active")
          && rect && rect.top >= 0 && rect.bottom <= window.innerHeight);
      });
    },
  },
  "tasks-drawer-mixed": {
    profile: "tasks-seeded",
    stubs: () => [],
    async run(page) {
      await page.waitForFunction(() => {
        const chip = document.querySelector("#task-chip");
        return chip && !chip.disabled;
      }, undefined, { timeout: 8000 }).catch(() => {});
      await page.click("#task-chip", { timeout: 5000 });
      await page.waitForTimeout(700);
    },
    async ready(page) {
      /* fresh 形态 tripwire（HARNESS-FIX-1）：单位进度行在像素 + 无 stale 注记。
         种子冻结钟后 fresh 由构造保证；此校验把未来任何回归挡在基线之外，
         而不是把 stale 退化形态静默钉进 jpg（VISBASE-2-R2 实证的旧钉病）。 */
      return page.evaluate(() => {
        const root = document.querySelector("#tasks-root");
        return Boolean(root && !root.hidden
          && root.textContent.includes("失败")
          && root.textContent.includes("正在转写音频")
          && root.textContent.includes("1 分钟 / 10 分钟")
          && !root.textContent.includes("进度信息已过期"));
      });
    },
  },
};

const ALL_STATE_NAMES = Object.keys(STATE_SPECS);

/* 单组合采集：整段重试（重新加载页面）至多 3 次——表面状态不达预期即作废
   本次尝试，绝不把启动竞态写进基线；3 次仍失败=环境性问题，直接报错。 */
async function captureCombo(context, { baseUrl, pageName, theme, viewport, filePath, quality, maxBytes, spec = null }) {
  const viewportLabel = `${viewport.width}x${viewport.height}`;
  let lastProblem = "unknown";
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const page = await context.newPage();
    const attachedStubs = [];
    try {
      if (spec?.init) await spec.init(page);
      await page.addInitScript(bootNormalizationScript(theme));
      for (const stub of (spec ? spec.stubs() : [])) {
        attachedStubs.push(stub.note);
      }
      await attachStubs(page, spec ? spec.stubs() : []);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.goto(`${baseUrl}/index.html`, { waitUntil: "load", timeout: 30000 });
      await waitAuthReady(page);
      if (spec) {
        await spec.run(page);
      } else if (pageName === "onboarding") {
        await openOnboardingViaMenu(page);
      } else if (pageName !== "study") {
        await page.evaluate((name) => {
          window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: name }));
        }, pageName);
      }
      await settlePage(page, pageName, { isState: Boolean(spec) });
      if (spec?.postSettle) await spec.postSettle(page);
      const ready = spec ? await spec.ready(page) : await surfaceReady(page, pageName);
      if (!ready) {
        const dump = await page.evaluate(() => ({
          account: document.querySelector("#account-text")?.textContent,
          activePage: document.querySelector(".page.active")?.dataset?.page,
          liveState: document.querySelector("#home-live-card")?.dataset?.state,
          liveButton: (() => {
            const button = document.querySelector("#enter-live-room");
            return button ? { hidden: button.hidden, disabled: button.disabled } : null;
          })(),
          liveText: (document.querySelector("[data-page='live']")?.innerText || "").slice(0, 220),
          scheduleSummary: document.querySelector("#schedule-summary")?.textContent?.slice(0, 60) || null,
          banner: (() => {
            const banner = document.querySelector(".player-resume-banner");
            const video = document.querySelector("#player-stage");
            return {
              present: Boolean(banner), hidden: banner?.hidden, text: banner?.textContent?.slice(0, 60) || null,
              video: video ? { readyState: video.readyState, paused: video.paused, t: Number(video.currentTime?.toFixed(1)), dur: video.duration } : null,
              ctrl: document.querySelector("#player-ctrl-status")?.textContent?.slice(0, 60) || null,
            };
          })(),
          onboardingPendingHidden: document.querySelector("#onboarding-pending")?.hidden,
          url: location.href,
        })).catch(() => ({}));
        lastProblem = `surface not ready after settle (attempt ${attempt}): ${JSON.stringify(dump)}`;
        continue;
      }
      const shot = await captureShot(page, { filePath, quality, maxBytes });
      return {
        page: pageName, theme, viewport: viewportLabel,
        file: path.basename(filePath), ...shot,
        ...(spec ? { state: pageName, profile: spec.profile, stubs: attachedStubs } : {}),
      };
    } catch (error) {
      lastProblem = `${error?.message || error} (attempt ${attempt})`;
      if (/ oversized|Timeout.*exceeded/i.test(String(error?.message)) && attempt === 3) throw error;
    } finally {
      await page.close().catch(() => {});
    }
  }
  throw new Error(`captureCombo failed for ${pageName}/${theme}/${viewportLabel}: ${lastProblem}`);
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`[visual-baseline] ${error.message}`);
    return EXIT_ERROR;
  }

  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch {
    console.error("[visual-baseline] playwright 未安装：tests/visual-baseline 下执行 npm install");
    return EXIT_ERROR;
  }

  const stateNames = args.states == null ? ALL_STATE_NAMES : args.states;
  for (const name of stateNames) {
    if (!STATE_SPECS[name]) {
      console.error(`[visual-baseline] unknown state: ${name}（可选：${ALL_STATE_NAMES.join(", ")}）`);
      return EXIT_ERROR;
    }
  }

  mkdirSync(args.out, { recursive: true });
  const servers = new Map();
  const started = Date.now();
  try {
    const browser = await chromium.launch({ headless: true });
    try {
      const serverFor = async (profile) => {
        if (!servers.has(profile)) {
          const server = args.baseUrl && profile === "default"
            ? { child: null, url: args.baseUrl, scratch: null }
            : await startSyntheticServer(args.python, SERVER_PROFILES[profile] || []);
          servers.set(profile, server);
        }
        return servers.get(profile);
      };
      const combos = [];
      const context = await browser.newContext({
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 1,
        reducedMotion: "reduce",
        forcedColors: "none",
        locale: "zh-CN",
        timezoneId: "Asia/Shanghai",
      });
      for (const theme of args.themes) {
        for (const viewport of args.viewports) {
          for (const pageName of args.pages) {
            const viewportLabel = `${viewport.width}x${viewport.height}`;
            const filePath = path.join(args.out, `${pageName}--${theme}--${viewportLabel}.jpg`);
            const server = await serverFor("default");
            combos.push(await captureCombo(context, {
              baseUrl: server.url, pageName, theme, viewport, filePath,
              quality: args.quality, maxBytes: args.maxBytes,
            }));
          }
        }
      }
      for (const theme of args.themes) {
        for (const viewport of STATE_VIEWPORTS) {
          for (const stateName of stateNames) {
            const spec = STATE_SPECS[stateName];
            const viewportLabel = `${viewport.width}x${viewport.height}`;
            const filePath = path.join(args.out, `${stateName}--${theme}--${viewportLabel}.jpg`);
            const server = await serverFor(spec.profile);
            combos.push(await captureCombo(context, {
              baseUrl: server.url, pageName: stateName, theme, viewport, filePath,
              quality: args.quality, maxBytes: args.maxBytes, spec,
            }));
          }
        }
      }
      await context.close();
      const overBudget = combos.filter((combo) => combo.bytes > args.maxBytes);
      if (overBudget.length > 0) {
        console.error(`[visual-baseline] 体积超预算（quality 已降到 50 仍超）：${JSON.stringify(overBudget)}`);
        return EXIT_ERROR;
      }
      const manifest = {
        generatedFrom: "tests/synthetic_shell_server.py (onboarding-guide=completed; 状态组合按 SERVER_PROFILES 档位)",
        frozenLocalTime: FROZEN_LOCAL,
        stateNames,
        quality: args.quality,
        maxBytes: args.maxBytes,
        elapsedMs: Date.now() - started,
        combos,
      };
      writeFileSync(path.join(args.out, "capture-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
      console.log(`[visual-baseline] captured ${combos.length} combos in ${manifest.elapsedMs}ms -> ${args.out}`);
      return EXIT_OK;
    } finally {
      await browser.close();
    }
  } catch (error) {
    const message = String(error?.message || error);
    if (/Executable doesn't exist|browserType\.launch|Failed to launch/i.test(message) && /chromium/i.test(message)) {
      console.error("[visual-baseline] chromium 缺失：cd tests/visual-baseline && npx playwright install chromium");
      return EXIT_NO_BROWSER;
    }
    console.error(`[visual-baseline] ${message}`);
    return EXIT_ERROR;
  } finally {
    for (const server of servers.values()) {
      if (server.child) stopSyntheticServer(server);
    }
  }
}

process.exit(await main());
