// D-20261009-07 typed 危险确认全族真实 DOM 回归钉（CONFIRM-DIALOG-FIX-1）。
//
// 缺陷（AVATAR-REPLAY-2 化身重放实锤，P1）：setConfirmInputLabel 对含
// <input> 子元素的 label 赋 textContent 抹除 #data-confirm-input →
// openTypedConfirm 抛 TypeError、弹窗永不打开——删除记录/清除全部孤儿
// 全族不可达。单测假 DOM registry 平铺无父子语义故漏检。
//
// 本钉以真实 chromium 驱动合成壳前端（tests/test_typed_confirm_realdom.py
// 起壳），逐门禁走完 开窗→键入→确认 全链：
//   - A 在册有名课程删除记录：门=课程名逐字精确匹配；
//   - B 无名孤儿行删除记录：门=课程编号逐字精确匹配；
//   - C 清除全部孤儿：门=非空确认语 + lead 点名清单。
// 「不应发生」断言：全程零 console 错误/页面异常；每次 label 更新后
// #data-confirm-input 仍在 DOM。
// 运行契约与 tests/visual-baseline 同源：playwright 包/chromium 缺席时
// exit 3（包装器转 SKIP），断言失败 exit 1。
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import process from "node:process";
import { mkdirSync } from "node:fs";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const args = { out: path.join(here, ".typed-confirm-out") };
for (let i = 2; i < process.argv.length; i += 1) {
  if (process.argv[i] === "--base-url") args.baseUrl = process.argv[i + 1];
  else if (process.argv[i] === "--out") args.out = process.argv[i + 1];
}
if (!args.baseUrl) {
  console.error("usage: node typed_confirm_realdom.mjs --base-url URL [--out DIR]");
  process.exit(2);
}

let chromium;
try {
  ({ chromium } = require(path.join(here, "visual-baseline", "node_modules", "playwright")));
} catch (error) {
  console.error(`[typed-confirm-realdom] playwright 包缺失：${error.message.split("\n")[0]}`);
  process.exit(3);
}

mkdirSync(args.out, { recursive: true });

const NAMED_COURSE_ID = "90000"; // 合成壳默认目录首课：线性代数 1
const NAMED_COURSE_TITLE = "线性代数 1";
const ORPHAN_A_ID = "8888"; // 种子孤儿一（delete-records 门=课程编号）
const ORPHAN_B_ID = "9000"; // 种子孤儿二（留给清除全部孤儿点名）

const results = [];
const check = (name, ok, detail = "") => {
  results.push(Boolean(ok));
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " :: " + detail : ""}`);
  return ok;
};

let browser;
try {
  browser = await chromium.launch({ headless: true });
} catch (error) {
  console.error(`[typed-confirm-realdom] chromium 未安装：${error.message.split("\n")[0]}`);
  process.exit(3);
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "zh-CN" });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200)); });
  page.on("pageerror", (error) => consoleErrors.push(`pageerror: ${String(error.message).slice(0, 200)}`));

  const dialogState = () => page.evaluate(() => ({
    open: Boolean(document.getElementById("data-confirm-dialog")?.open),
    inputInDom: Boolean(document.getElementById("data-confirm-input")),
    label: document.getElementById("data-confirm-label")?.textContent || "",
    prompt: document.querySelector("#data-confirm-hint .data-confirm-prompt")?.textContent || "",
    lead: [...document.querySelectorAll("#data-confirm-hint span")]
      .map((node) => node.textContent).find((text) => text.includes("残留数据")) || "",
    orphanNames: [...document.querySelectorAll("#data-confirm-hint .data-orphan-name")]
      .map((node) => node.textContent),
    confirmDisabled: document.getElementById("data-confirm-confirm")?.disabled ?? null,
  }));
  const waitDialogClosed = () => page.waitForFunction(
    () => !document.getElementById("data-confirm-dialog")?.open,
    { timeout: 15000 },
  );

  // ---- 进入数据管理页 ----
  await page.goto(args.baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#account-button", { state: "visible", timeout: 30000 });
  const greeting = page.locator("#study-start-select");
  if (await greeting.count() && await greeting.isVisible().catch(() => false)) await greeting.click();
  await page.locator("#account-button").click();
  await page.locator("#account-menu-data").waitFor({ state: "visible", timeout: 5000 });
  await page.locator("#account-menu-data").click();
  await page.waitForSelector("#data-page", { state: "visible", timeout: 5000 });
  await page.locator(`input[data-course-pick="${NAMED_COURSE_ID}"]`).waitFor({ state: "visible", timeout: 15000 });

  // ---- 门 A：在册有名课程（线性代数 1）删除记录，门=课程名 ----
  await page.locator(`input[data-course-pick="${NAMED_COURSE_ID}"]`).check();
  await page.locator("#data-action-delete-records").click();
  await page.waitForTimeout(400);
  let st = await dialogState();
  check("A1 typed 弹窗打开且输入框在 DOM", st.open && st.inputInDom, JSON.stringify(st));
  check("A2 标签=输入课程名确认", st.label === "输入课程名确认", st.label);
  check("A3 提示语=输入课程名「线性代数 1」确认：", st.prompt === `输入课程名「${NAMED_COURSE_TITLE}」确认：`, st.prompt);
  check("A4 空输入确认禁用", st.confirmDisabled === true);
  await page.fill("#data-confirm-input", "错的课程名");
  check("A5 名字不匹配仍禁用", await page.locator("#data-confirm-confirm").isDisabled());
  await page.fill("#data-confirm-input", NAMED_COURSE_TITLE);
  check("A6 逐字匹配后启用", !(await page.locator("#data-confirm-confirm").isDisabled()));
  await page.screenshot({ path: path.join(args.out, "gate-a-named.png") });
  await page.locator("#data-confirm-confirm").click();
  await waitDialogClosed();
  check("A7 确认后弹窗关闭且输入框仍在 DOM", await dialogState().then((s) => s.inputInDom && !s.open));
  /* delete-records 只删学习记录：合成课程 90000 无学习记录时行保留（引擎
     accepted/deleted 空也回执已完成）——链路完成信号=已完成回执上屏 */
  await page.waitForFunction(
    () => {
      const row = document.getElementById("data-result");
      return Boolean(row && !row.hidden && row.textContent.includes("「删除记录」已完成"));
    },
    { timeout: 15000 },
  );
  check("A8 已完成回执上屏", true);

  // ---- 门 B：无名孤儿（8888）删除记录，门=课程编号 ----
  // 先清掉门 A 的选择（90000 行仍存活）：不清则勾 8888 后成双选，
  // W3 恰-1-课程规则禁用删除钮（REPLAY-2 同款纪律）
  const namedPick = page.locator(`input[data-course-pick="${NAMED_COURSE_ID}"]`);
  if (await namedPick.count() && await namedPick.isChecked().catch(() => false)) {
    await namedPick.uncheck();
    await page.waitForTimeout(300);
  }
  await page.locator("#data-orphans-filter").click();
  await page.locator(`input[data-course-pick="${ORPHAN_A_ID}"]`).waitFor({ state: "visible", timeout: 15000 });
  await page.locator(`input[data-course-pick="${ORPHAN_A_ID}"]`).check();
  await page.locator("#data-action-delete-records").click();
  await page.waitForTimeout(400);
  st = await dialogState();
  check("B1 typed 弹窗打开且输入框在 DOM", st.open && st.inputInDom, JSON.stringify(st));
  check("B2 标签=输入课程编号确认", st.label === "输入课程编号确认", st.label);
  check("B3 提示语=输入课程编号「8888」确认：", st.prompt === `输入课程编号「${ORPHAN_A_ID}」确认：`, st.prompt);
  await page.fill("#data-confirm-input", "888");
  check("B4 编号不匹配仍禁用", await page.locator("#data-confirm-confirm").isDisabled());
  await page.fill("#data-confirm-input", ORPHAN_A_ID);
  check("B5 逐字匹配后启用", !(await page.locator("#data-confirm-confirm").isDisabled()));
  await page.screenshot({ path: path.join(args.out, "gate-b-orphan-id.png") });
  /* 孤儿行 delete-records 的引擎接受面=另一域语义（引擎闭集拒绝未知课程，
     前端诚实降级「操作未完成，请稍后重试。」）；本钉按 REPLAY-2 清单口径
     断言 门禁链+取消关闭——接受链由门 A/门 C（引擎支持面）覆盖 */
  await page.locator("#data-confirm-cancel").click();
  await waitDialogClosed();
  check("B6 取消关闭且输入框仍在 DOM", await dialogState().then((s) => s.inputInDom && !s.open));

  // ---- 门 C：清除全部孤儿（9000），门=非空确认语 + 点名清单 ----
  await page.locator("#data-clear-orphans").click();
  await page.waitForTimeout(400);
  st = await dialogState();
  check("C1 typed 弹窗打开且输入框在 DOM", st.open && st.inputInDom, JSON.stringify(st));
  check("C2 标签=输入任意文字确认", st.label === "输入任意文字确认", st.label);
  check("C3 lead=将清除以下课程留下的残留数据：", st.lead === "将清除以下课程留下的残留数据：", st.lead);
  check("C4 点名清单含孤儿 9000", st.orphanNames.includes(ORPHAN_B_ID), JSON.stringify(st.orphanNames));
  check("C5 空确认语禁用", st.confirmDisabled === true);
  await page.fill("#data-confirm-input", "确认清理");
  check("C6 任意非空文字启用", !(await page.locator("#data-confirm-confirm").isDisabled()));
  await page.screenshot({ path: path.join(args.out, "gate-c-clear-orphans.png") });
  await page.locator("#data-confirm-confirm").click();
  await waitDialogClosed();
  check("C7 确认后弹窗关闭且输入框仍在 DOM", await dialogState().then((s) => s.inputInDom && !s.open));
  /* 清除全部孤儿映射 remove-copies+include_orphans：接受=「移除本地副本」已完成；
     引擎闭集拒绝时=「未执行」+闭集阻塞码——两条都是合法回执呈现 */
  await page.waitForFunction(
    () => {
      const row = document.getElementById("data-result");
      if (!row || row.hidden) return false;
      const text = row.textContent;
      return text.includes("移除本地副本」已完成") || text.includes("未执行");
    },
    { timeout: 15000 },
  );
  check("C8 清除孤儿回执上屏（已完成或闭集拒绝）", true);

  // ---- 全局「不应发生」负断言 ----
  check("Z1 全程零 console 错误/页面异常（TypeError 家族绝迹）", consoleErrors.length === 0, consoleErrors.join(" | "));
  check("Z2 输入框最终仍在 DOM", await page.evaluate(() => Boolean(document.getElementById("data-confirm-input"))));
  await page.screenshot({ path: path.join(args.out, "final.png") });

  await browser.close();
  const pass = results.filter(Boolean).length;
  const fail = results.length - pass;
  console.log(`SUMMARY pass=${pass} fail=${fail}`);
  process.exit(fail === 0 ? 0 : 1);
} catch (error) {
  check("流程未中断", false, String(error && error.message ? error.message : error).slice(0, 300));
  const pass = results.filter(Boolean).length;
  const fail = results.length - pass;
  console.log(`SUMMARY pass=${pass} fail=${fail}`);
  try { await browser.close(); } catch { /* 已退出即达成 */ }
  process.exit(1);
}
