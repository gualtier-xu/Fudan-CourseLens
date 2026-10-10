/* 学习空态问候/诗联装配入口：产品 CSP script-src 'self' 下内联 module 不可用，故外置。 */
/* 学习空态问候/诗联装配：与 app.js 共享同一 store 模块实例。 */
import { store } from "/modules/store.js";
import { installGreeting } from "/modules/greeting.js";
/* 乙-3（#6）：首用淡出计数——启动次数只存本机 UI 偏好键（零课程身份），
   第 GREETING_FADE_STARTS 次启动之后问候行收起（熟手后为常驻噪声；
   课量信息已由「今日与本周安排」承载，问候为纯寒暄，收起零信息损失）。 */
const APP_STARTS_KEY = "courselens:app-starts";
const GREETING_FADE_STARTS = 5;
let startCount = 1;
try {
  startCount = (Number(localStorage.getItem(APP_STARTS_KEY) || "0") || 0) + 1;
  localStorage.setItem(APP_STARTS_KEY, String(startCount));
} catch { /* 存储不可用：本次按首用呈现 */ }
installGreeting(store, { startCount });
