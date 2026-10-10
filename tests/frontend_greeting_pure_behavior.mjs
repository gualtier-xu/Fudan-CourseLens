import assert from "node:assert/strict";
import { assemble } from "./frontend_exec_harness.mjs";

/* 问候面纯函数真执行钉（夜14-R7 TOP10 草案 D9；greeting.js 唯一外部依赖
   store 仅被 installGreeting 使用，装配注入桩件后不调用安装面）：
   1. 主按钮四态闭集（含 checking 的自动登录分叉）；
   2. 课量尾句闭集：五键逐字 + 未知键/无数据 = null，绝不自造文案；
   3. composeGreeting 组装语义：课量尾句、fallback 尾句（仅快照缺位）、
      深夜档不叠加、节日整句替换；
   4. 隐私钉：尾句闭集零数字零字母（轻文言零数字的课程事实——学生数据
      绝不进问候语）。 */

const factory = assemble("../frontend/modules/greeting.js", ["store"], [
  "BUTTON_TEXT_READY", "BUTTON_TEXT_LOGIN", "BUTTON_TEXT_RESUMING", "BUTTON_TEXT_DEGRADED",
  "buttonTextFor", "bandIndexFor", "weekdayName", "holidayGreeting", "courseLoadTail",
  "composeGreeting", "tailTextForKey", "stripTrailingPeriod",
]);
const exports = factory({ auth: null });

function testButtonTextClosedSet() {
  assert.equal(exports.buttonTextFor("ready"), "选择课程");
  assert.equal(exports.buttonTextFor("checking"), "登录后选择课程");
  assert.equal(exports.buttonTextFor("checking", { autoLoginResume: true }), "选择课程");
  assert.equal(exports.buttonTextFor("degraded"), "重新登录并选择课程");
  assert.equal(exports.buttonTextFor("anything-else"), "登录后选择课程");
  assert.equal(exports.BUTTON_TEXT_RESUMING, "正在恢复会话…");
}

function testTailKeyClosedSet() {
  assert.equal(exports.tailTextForKey("none"), "今日无课。");
  assert.equal(exports.tailTextForKey("done"), "今日课毕。");
  assert.equal(exports.tailTextForKey("done-long"), "今日课毕，辛苦了。");
  assert.equal(exports.tailTextForKey("full"), "今日课满。");
  assert.equal(exports.tailTextForKey("full-half"), "今日课满，已过半。");
  /* 无数据/未知键一律 null：退回基础问候，绝不自造文案 */
  assert.equal(exports.tailTextForKey("no-data"), null);
  assert.equal(exports.tailTextForKey(""), null);
  assert.equal(exports.tailTextForKey("全部课程都结束啦"), null);
  assert.equal(exports.tailTextForKey(null), null);
}

function testTailPrivacyNoDigitsNoLatin() {
  for (const key of ["none", "done", "done-long", "full", "full-half"]) {
    const text = exports.tailTextForKey(key);
    assert.match(text, /^[\u4e00-\u9fff，。]+$/,
      `tail copy for ${key} must stay pure CJK (no digits/identifiers)`);
  }
}

function testCourseLoadTailHonesty() {
  const morning = new Date(2026, 2, 11, 10, 0); // 周三上午（非深夜档）
  assert.equal(exports.courseLoadTail(null, morning), null, "missing snapshot = no tail");
  assert.equal(exports.courseLoadTail([], morning), "今日无课。");
  /* 1–4 节未结束：诚实留白 */
  const four = [
    { end_time: "08:00" }, { end_time: "10:00" }, { end_time: "12:00" }, { end_time: "14:00" },
  ];
  assert.equal(exports.courseLoadTail(four, morning), null);
  /* ≥5 节且已过半：课满尾句（4 节在 10:00 前结束 = 过半） */
  const six = [
    { end_time: "08:00" }, { end_time: "08:30" }, { end_time: "09:00" }, { end_time: "09:30" },
    { end_time: "14:00" }, { end_time: "14:30" },
  ];
  assert.equal(exports.courseLoadTail(six, morning), "今日课满，已过半。");
  assert.equal(exports.courseLoadTail(six, new Date(2026, 2, 11, 15, 0)), "今日课毕，辛苦了。");
  /* 深夜档不叠加任何尾句 */
  assert.equal(exports.courseLoadTail([], new Date(2026, 2, 11, 2, 0)), null);
  /* 条目时间全坏：异常退回，不发明文案 */
  assert.equal(exports.courseLoadTail([{ end_time: "bad" }], morning), null);
}

function testComposeGreetingAssembly() {
  /* 2026-03-11 周三上午十点 */
  const morning = new Date(2026, 2, 11, 10, 0);
  assert.equal(exports.weekdayName(morning), "周三");
  assert.equal(exports.composeGreeting(morning, []), "周三上午好，今日无课。");
  /* 快照缺位（null）才吃 fallback 尾句（调用方经 tailTextForKey 解析后的
     文本；未知键/无数据解析为 null/空串 → 退回基础问候） */
  assert.equal(
    exports.composeGreeting(morning, null, "今日课满。"),
    "周三上午好，今日课满。",
  );
  assert.equal(exports.composeGreeting(morning, null, ""), "周三上午好。");
  assert.equal(exports.composeGreeting(morning, null, null), "周三上午好。");
  /* 快照在而今日 0 节：精确尾句优先，不吃 fallback */
  assert.equal(
    exports.composeGreeting(morning, [], "今日课毕。"),
    "周三上午好，今日无课。",
  );
  /* 深夜档：基础问候，零尾句 */
  const deepNight = new Date(2026, 2, 11, 2, 0);
  assert.equal(exports.composeGreeting(deepNight, null, "今日课满。"), "周三，夜深了。");
  /* 节日整句替换（教师节 09-10） */
  const teachers = new Date(2026, 8, 10, 10, 0);
  assert.equal(exports.holidayGreeting(teachers), "教师节快乐。");
  assert.equal(exports.composeGreeting(teachers, []), "教师节快乐。");
}

function testStripTrailingPeriodIsDisplayOnly() {
  assert.equal(exports.stripTrailingPeriod("今日课毕。"), "今日课毕");
  assert.equal(exports.stripTrailingPeriod("辛苦了。。"), "辛苦了");
  /* 问号/句中逗号一律保留 */
  assert.equal(exports.stripTrailingPeriod("下课了吗？"), "下课了吗？");
  assert.equal(exports.stripTrailingPeriod("已过半，继续。"), "已过半，继续");
}

testButtonTextClosedSet();
testTailKeyClosedSet();
testTailPrivacyNoDigitsNoLatin();
testCourseLoadTailHonesty();
testComposeGreetingAssembly();
testStripTrailingPeriodIsDisplayOnly();
console.log("frontend greeting pure behavior passed");
