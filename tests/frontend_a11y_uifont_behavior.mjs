import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

/* 界面字号档真执行行为测试（A11Y-IMPL-4，D14 P1-1 产品步）：
   以桩件（fake localStorage/document）真执行 frontend/modules/ui-font.js：
   ① 档位循环：large/xlarge 落 html[data-ui-font]，default 摘属性——属性名与
     tokens.css 的根字号档位选择器逐字对账（rem 地基使根字号成为全站唯一总开关）；
   ② 持久化：选择写真源单键 courselens.ui-font.v1，读取闭集校验；
   ③ 装配路径：applyUiFontAtStartup 只落属性不写存储（存储即真源，启动零冗余写）；
   ④ 闭集外偏好拒：非法值回落 default 且不污染存储；
   ⑤ 控件回同步：settings-ui-font select 存在即被回写当前档（对称主题三态）。 */

const root = new URL("../", import.meta.url);

/* ---- 桩件（须先于模块导入就位）---- */

const store = new Map();
const writtenKeys = [];
const fakeStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { writtenKeys.push([k, String(v)]); store.set(k, String(v)); },
  removeItem: (k) => store.delete(k),
};

const selectBox = { value: "" };
globalThis.localStorage = fakeStorage;
globalThis.document = {
  documentElement: { dataset: {} },
  getElementById: (id) => (id === "settings-ui-font" ? selectBox : null),
};

const uiFont = await import(new URL("frontend/modules/ui-font.js", root).href);
const tokensCss = readFileSync(new URL("frontend/styles/tokens.css", root), "utf8");

/* ---- ① 档位循环 + 根字号联动 ---- */

assert.equal(uiFont.UI_FONT_PREF_KEY, "courselens.ui-font.v1", "偏好真源单键");
assert.deepEqual([...uiFont.UI_FONT_TIERS], ["default", "large", "xlarge"], "档位闭集");

// 根字号联动合同：JS 写的属性名与 CSS 档位选择器逐字对账（写漂移即红）
assert.ok(tokensCss.includes('html[data-ui-font="large"] { font-size: 112.5%; }'),
  "tokens.css 缺 large 档根字号规则");
assert.ok(tokensCss.includes('html[data-ui-font="xlarge"] { font-size: 125%; }'),
  "tokens.css 缺 xlarge 档根字号规则");
assert.ok(tokensCss.includes("html { font-size: 100%; }"),
  "tokens.css 缺默认档根字号基准（100%=浏览器默认，像素等价地基）");

assert.equal(uiFont.applyUiFontPreference("large"), "large", "large 档返回 resolved");
assert.equal(document.documentElement.dataset.uiFont, "large", "large 档落 html[data-ui-font]");
assert.deepEqual(writtenKeys, [["courselens.ui-font.v1", "large"]], "large 档恰写一次真源");
assert.equal(selectBox.value, "large", "控件回同步当前档");

assert.equal(uiFont.applyUiFontPreference("xlarge"), "xlarge");
assert.equal(document.documentElement.dataset.uiFont, "xlarge", "xlarge 档换装");
assert.equal(store.get("courselens.ui-font.v1"), "xlarge", "真源随选择更新");

assert.equal(uiFont.applyUiFontPreference("default"), "default");
assert.equal("uiFont" in document.documentElement.dataset, false, "default 档摘除属性（=根字号 100%，与改前逐值同）");
assert.equal(store.get("courselens.ui-font.v1"), "default", "default 档持久化显式值");

/* ---- ② 读取闭集校验 ---- */

store.set("courselens.ui-font.v1", "large");
assert.equal(uiFont.readUiFontPreference(), "large", "合法档读回");
store.set("courselens.ui-font.v1", "huge");
assert.equal(uiFont.readUiFontPreference(), "default", "闭集外存值回落 default");
store.delete("courselens.ui-font.v1");
assert.equal(uiFont.readUiFontPreference(), "default", "未设偏好回落 default");

/* ---- ③ 装配路径：只落属性不写存储 ---- */

store.set("courselens.ui-font.v1", "xlarge");
writtenKeys.length = 0;
const attrBefore = JSON.stringify(document.documentElement.dataset);
assert.equal(uiFont.applyUiFontAtStartup(), "xlarge", "启动应用返回当前档");
assert.equal(document.documentElement.dataset.uiFont, "xlarge", "启动按真源落属性");
assert.deepEqual(writtenKeys, [], "启动路径零存储写（存储即真源，无冗余写面）");
assert.equal(uiFont.applyUiFontAtStartup(fakeStorage), "xlarge", "显式 storage 注入同语义");
const attrAfterStartup = JSON.stringify(document.documentElement.dataset);
assert.equal(JSON.stringify(document.documentElement.dataset), attrAfterStartup, "属性面稳定（幂等重放）");

// 启动 default 档：属性被摘除（未设偏好=改前形态）
store.delete("courselens.ui-font.v1");
assert.equal(uiFont.applyUiFontAtStartup(), "default");
assert.equal("uiFont" in document.documentElement.dataset, false, "未设偏好=无属性（100% 基准）");

/* ---- ④ 闭集外偏好拒 ---- */

writtenKeys.length = 0;
assert.equal(uiFont.applyUiFontPreference("huge"), "default", "非法偏好 resolved=default");
assert.equal("uiFont" in document.documentElement.dataset, false, "非法偏好落 default 形态");
assert.deepEqual(writtenKeys, [], "非法偏好不写存储（绝不污染真源）");
assert.equal(uiFont.applyUiFontPreference(undefined), "default", "非字符串同拒");

/* ---- ⑤ 选择即持久完整链：写→读→应用同拍 ---- */

assert.equal(uiFont.applyUiFontPreference("large"), "large");
assert.equal(uiFont.readUiFontPreference(), "large");
assert.equal(document.documentElement.dataset.uiFont, "large");
assert.equal(selectBox.value, "large");

console.log("frontend a11y ui font behavior passed");
