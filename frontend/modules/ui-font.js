/* ui-font.js —— 外观·界面字号档（A11Y-IMPL-4，D14 P1-1 产品步）。
   rem 地基（styles/tokens.css：全站 font-size ÷16 精确小数 rem 化 + html 根
   字号档位选择器）使根字号成为全站字号的唯一总开关：本模块只落
   html[data-ui-font] 闭集属性，缩放本身由 CSS 声明完成，零样式注入。
   形态沿 shell.js 主题三态先例（P56-U1）：闭集校验读 / 持久化写 / 应用与
   控件回同步，localStorage 单键。字幕域（video::cue、--sub-font-size 独立
   四档制）不随界面档缩放，是刻意的双轨设计。
   位置纪律：置于 modules/ 顶层而非 settings/ 子目录——settings 家族带
   「永不触碰浏览器本地存储」隐私钉（test_remote_compute_ui fail-closed 钉
   断言家族文本零 localStorage token，凭据毗邻面纪律）；浏览器侧偏好写入方
   依先例居顶层（主题=shell.js、课程排序=study.js、问候=greeting.js），本模块
   同轨；重置清除仍经 settings/client-reset.js 的 ui.js removeLocalKeys 通道。 */

import { $ } from "./ui.js";

/* 唯一偏好真源；重置应用的浏览器侧闭集键之一（settings/client-reset.js） */
export const UI_FONT_PREF_KEY = "courselens.ui-font.v1";

/* 档位闭集：默认=不落属性（根字号恒 100%，与改前逐值相同）；
   大=112.5%（16→18px）；特大=125%（16→20px）。 */
export const UI_FONT_TIERS = Object.freeze(["default", "large", "xlarge"]);

export function readUiFontPreference(storage = globalThis.localStorage) {
  const raw = storage?.getItem(UI_FONT_PREF_KEY);
  return UI_FONT_TIERS.includes(raw) ? raw : "default";
}

/* 装配期应用（每次启动都走，先于用户交互）：只落属性、不写存储——存储里的
   值就是真源，启动重写是冗余面。 */
export function applyUiFontAtStartup(storage = globalThis.localStorage) {
  const preference = readUiFontPreference(storage);
  applyUiFontAttribute(preference);
  return preference;
}

/* 选择即持久（设置 → 外观，同「点选即生效」的档位卡语义）：写真源 +
   落属性 + 控件回同步（对称主题三态的 applyTheme 回同步）。 */
export function applyUiFontPreference(preference, storage = globalThis.localStorage) {
  const resolved = UI_FONT_TIERS.includes(preference) ? preference : "default";
  if (UI_FONT_TIERS.includes(preference)) storage?.setItem(UI_FONT_PREF_KEY, preference);
  applyUiFontAttribute(resolved);
  const select = $("settings-ui-font");
  if (select) select.value = readUiFontPreference(storage);
  return resolved;
}

function applyUiFontAttribute(preference) {
  const root = document.documentElement;
  if (preference === "default") delete root.dataset.uiFont;
  else root.dataset.uiFont = preference;
}
