import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/* 壳层无障碍行为测试（A11Y-IMPL-2，D14 审计 P2-4/P3-3）：
   读真件（frontend/index.html + styles/accessibility.css）做行为合同钉。
   覆盖：
   ① P2-4 skip link——键盘 Tab 的首停点=「跳到主内容」：标记在 body 首位
     （topbar 之前）、href 落焦目标 main#workspace-main 既有 tabindex="-1"；
     CSS 基态视觉隐藏、:focus-visible 显形，且显形块完整解除每一个隐藏
     性质（clip/width/height/overflow/margin/padding 逐项对账——日后有人在
     基态新增隐藏性质而忘记在显形块解除，此处即红）；显形位置钉在顶栏
     正下方（--topbar-height 唯一源 tokens.css），z 高于全站最高浮层
     （.toast-region 60）。
   ② P3-3 批量操作分组——数据页 data-action-group span 全部带 role="group"：
     span 上只有 aria-label 而无 role 时读屏会忽略该名；审计时 3 处，
     「选择管理」manage 组为审计后新增，同类缺陷一并补齐=4 处。 */

const root = new URL("../", import.meta.url);
const html = readFileSync(new URL("frontend/index.html", root), "utf8");
const a11yCss = readFileSync(new URL("frontend/styles/accessibility.css", root), "utf8");

/* ---- P2-4：skip link 在场、文档序、Tab 序首停 ---- */

const bodyAt = html.indexOf("<body>");
assert.ok(bodyAt > 0, "index.html 缺 <body>");
const body = html.slice(bodyAt);

const skipAt = body.indexOf('<a class="skip-link" href="#workspace-main">跳到主内容</a>');
assert.ok(skipAt >= 0, "skip link 标记缺席（文案/类名/href 任一漂移即红）");
const topbarAt = body.indexOf('<header class="topbar">');
const mainAt = body.indexOf('<main id="workspace-main" tabindex="-1">');
assert.ok(topbarAt > skipAt, "skip link 必须在 topbar 之前（否则首停仍是顶栏控件）");
assert.ok(mainAt > topbarAt, "main 应在 topbar 之后（页面壳文档序漂移）");
assert.ok(mainAt > 0, 'main#workspace-main 缺 tabindex="-1"，skip link 无法落焦');

/* Tab 序=文档序：body 内可聚焦元素（a[href]/button/input/select/textarea）
   按出现顺序枚举，第一个必须就是 skip link——「每页 Tab 先穿顶栏 9 控件」
   的审计病灶以此钉死。index.html 无内联脚本（CSP script-src 'self'），
   标签扫描不受脚本字符串干扰。 */
const focusableRe = /<(a|button|input|select|textarea)\b[^>]*>/g;
const firstFocusable = focusableRe.exec(body);
assert.ok(firstFocusable, "body 内未枚举到任何可聚焦元素（扫描器失效）");
assert.ok(
  firstFocusable[0].startsWith('<a class="skip-link"'),
  `Tab 首停点不是 skip link：${firstFocusable[0].slice(0, 80)}`,
);

/* ---- P2-4：accessibility.css 显形合同 ---- */

function cssBlock(css, selector) {
  const at = css.indexOf(selector);
  assert.ok(at >= 0, `accessibility.css 缺 ${selector}`);
  const open = css.indexOf("{", at);
  const close = css.indexOf("}", open);
  assert.ok(open > 0 && close > open, `accessibility.css 的 ${selector} 块不完整`);
  return css.slice(open + 1, close);
}

const baseBlock = cssBlock(a11yCss, ".skip-link {");
const revealBlock = cssBlock(a11yCss, ".skip-link:focus-visible {");

/* 基态=视觉隐藏（.sr-only 同款剪裁），显形走 :focus-visible（三态焦点政策） */
assert.ok(baseBlock.includes("clip: rect(0 0 0 0)"), "skip link 基态未剪裁隐藏");
assert.ok(baseBlock.includes("position: fixed"), "skip link 基态应脱离文档流（显形不推移布局）");

/* 显形块必须逐项解除基态的每一个隐藏性质：任一遗留（如 clip 仍 rect、
   width 仍 1px）都会让「聚焦显形」名存实亡 */
for (const required of ["clip: auto", "width: auto", "height: auto", "overflow: visible", "margin: 0"]) {
  assert.ok(revealBlock.includes(required), `skip-link 显形块缺「${required}」——隐藏性质未解除`);
}
assert.ok(/padding:\s*\d/.test(revealBlock), "skip-link 显形块缺正 padding（基态 padding:0 未解除）");

/* 显形位置：顶栏正下方（不遮顶栏，--topbar-height 唯一源）；
   z 高于全站最高浮层 .toast-region 的 60 */
assert.ok(
  baseBlock.includes("top: calc(var(--topbar-height) + 8px)"),
  "skip link 显形位置未钉在顶栏正下方",
);
const zMatch = baseBlock.match(/z-index:\s*(\d+)/);
assert.ok(zMatch, "skip link 缺 z-index（浮层开着时可能被盖住）");
assert.ok(
  Number(zMatch[1]) > 60,
  `skip link z-index=${zMatch[1]} 须高于全站最高浮层 .toast-region 的 60`,
);

/* ---- P3-3：批量操作分组全部带 role="group" ---- */

const groupTags = [...body.matchAll(/<span class="data-action-group"[^>]*>/g)].map((m) => m[0]);
assert.equal(groupTags.length, 4, `data-action-group 分组应为 4 处，实测 ${groupTags.length}`);
const expected = [
  ["safe", "安全操作"], ["space", "释放空间"],
  ["irreversible", "不可逆操作"], ["manage", "选择管理"],
];
const seen = [];
for (const tag of groupTags) {
  assert.ok(
    /role="group"/.test(tag),
    `分组 span 缺 role="group"，其 aria-label 会被读屏忽略：${tag}`,
  );
  const group = tag.match(/data-bulk-group="([a-z]+)"/)?.[1];
  const label = tag.match(/aria-label="([^"]+)"/)?.[1];
  assert.ok(label, `分组 span 缺非空 aria-label：${tag}`);
  seen.push([group, label]);
}
assert.deepEqual(seen, expected, "分组 data-bulk-group/aria-label 名册漂移");

console.log("frontend a11y shell behavior passed");
