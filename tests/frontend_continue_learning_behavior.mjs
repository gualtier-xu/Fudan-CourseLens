import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { familySource } from "./frontend_exec_harness.mjs";

/* FIRST-LOGIN-UX-2（U1「继续学习」直达语义）+ LANDING-AESTHETIC-1（用户裁决：
   诗页复原，「继续学习」降级为主钮下极轻弱化链）行为钉：
   ①store 记忆面：最小键读写/形坏自愈/空 ID 拒写/多讲次取最近/存储不可得静默；
   ②解析纯函数：目录闭集守卫（陈旧/换账号/讲次不在=无历史）；
   ③着陆接线静态钉：单一漏斗/诗页复原形态（主钮原样+弱化链）/DOM 序=Tab 序/键盘落点锚/订阅与清理配对。 */

const saved = new Map();
let storageBroken = false;
globalThis.localStorage = {
  setItem: (key, value) => {
    if (storageBroken) throw new Error("quota");
    saved.set(key, value);
  },
  getItem: (key) => {
    if (storageBroken) throw new Error("quota");
    return saved.get(key) ?? null;
  },
  removeItem: (key) => {
    if (storageBroken) throw new Error("quota");
    saved.delete(key);
  },
};

const { store } = await import("../frontend/modules/store.js");
const { resolveContinueTarget, studyLandingMode } = await import("../frontend/modules/study.js");

const LAST_KEY = "courselens.last-lecture.v1";

/* ---- ① store 记忆面 ---- */
assert.equal(store.readLastLecture(), null, "无历史=null（卡不出现的存储前提）");

store.rememberLastLecture({ course_id: "90001", sub_id: "900011", ts: 111 });
assert.deepEqual(store.readLastLecture(), { course_id: "90001", sub_id: "900011", ts: 111 }, "往返保形");

/* 多讲次取最近：单一槽位，后开覆盖先开 */
store.rememberLastLecture({ course_id: "90002", sub_id: "900021", ts: 222 });
assert.equal(store.readLastLecture().sub_id, "900021", "多讲次取最近打开");
assert.equal(JSON.parse(saved.get(LAST_KEY)).course_id, "90002", "键内只有最新一条（零历史堆栈）");

/* 空 ID 拒写（垃圾条目绝不落键，N5PR-P2 同纪律） */
store.rememberLastLecture({ course_id: "", sub_id: "900031", ts: 333 });
assert.equal(store.readLastLecture().sub_id, "900021", "空 course_id 拒写");
store.rememberLastLecture({ course_id: "90003", sub_id: "  ", ts: 333 });
assert.equal(store.readLastLecture().sub_id, "900021", "空白 sub_id 拒写");
store.rememberLastLecture(null);
assert.equal(store.readLastLecture().sub_id, "900021", "null 入参拒写");

/* 键内容闭集：只有 ID+时刻，零课程名/零个人数据 */
const raw = JSON.parse(saved.get(LAST_KEY));
assert.deepEqual(Object.keys(raw).sort(), ["course_id", "sub_id", "ts"], "键形状=纯 ID 级最小键");

/* 非有限 ts 归 0（形状稳定，不发明 NaN） */
store.rememberLastLecture({ course_id: "90004", sub_id: "900041", ts: "x" });
assert.equal(store.readLastLecture().ts, 0, "非有限 ts 归 0");

/* 形坏/损坏键自愈清除 */
saved.set(LAST_KEY, "{not json");
assert.equal(store.readLastLecture(), null, "损坏 JSON=无历史");
assert.equal(saved.has(LAST_KEY), false, "损坏键自愈清除");
saved.set(LAST_KEY, JSON.stringify({ foo: 1 }));
assert.equal(store.readLastLecture(), null, "形坏键=无历史");
assert.equal(saved.has(LAST_KEY), false, "形坏键自愈清除");

/* 存储不可得（隐私模式/配额）：读写全静默，绝不报错 */
storageBroken = true;
store.rememberLastLecture({ course_id: "90005", sub_id: "900051", ts: 5 }); /* 不得抛 */
assert.equal(store.readLastLecture(), null, "存储不可得读=无历史");
storageBroken = false;

/* ---- ② 解析纯函数：目录闭集守卫 ---- */
const catalog = [
  {
    course_id: "90001", title: "数据结构 1",
    lectures: [
      { sub_id: "900011", sub_title: "第一讲：线性表" },
      { sub_id: "900012", sub_title: "第二讲：二叉树" },
    ],
  },
  {
    course_id: "90002", title: "线性代数 9",
    lectures: [{ sub_id: "900021", sub_title: "第一讲：行列式" }],
  },
];

const hit = resolveContinueTarget(catalog, { course_id: "90001", sub_id: "900012", ts: 1 });
assert.equal(hit.course_id, "90001", "解析命中带 course_id");
assert.equal(hit.course_title, "数据结构 1", "解析命中带 course_title（卡文字来自实时目录）");
assert.equal(hit.sub_title, "第二讲：二叉树", "解析命中带讲次标题");

assert.equal(resolveContinueTarget(catalog, null), null, "无历史=不出现");
assert.equal(resolveContinueTarget(catalog, {}), null, "空记忆=不出现");
assert.equal(resolveContinueTarget(catalog, { course_id: "99999", sub_id: "999991" }), null, "课程不在目录=不出现（陈旧/换账号守卫）");
assert.equal(resolveContinueTarget(catalog, { course_id: "90001", sub_id: "dead" }), null, "讲次不在课程=不出现（目录刷新守卫）");
assert.equal(resolveContinueTarget([], { course_id: "90001", sub_id: "900011" }), null, "空目录=不出现（未登录/清理后守卫）");
assert.equal(resolveContinueTarget(null, { course_id: "90001", sub_id: "900011" }), null, "目录缺位=不出现");
assert.equal(resolveContinueTarget([{ course_id: "90003", title: "x" }], { course_id: "90003", sub_id: "900031" }), null, "课程无讲次表=不出现");

/* 记忆×目录联合语义：记忆指向 900021（最近打开）→ 解析=线性代数（多讲次取最近端到端） */
store.rememberLastLecture({ course_id: "90001", sub_id: "900011", ts: 1 });
store.rememberLastLecture({ course_id: "90002", sub_id: "900021", ts: 2 });
assert.equal(resolveContinueTarget(catalog, store.readLastLecture()).course_title, "线性代数 9", "端到端：最近讲次胜出");

/* 落地门回归：记忆存在不改变三态模型（显式导航才解锁） */
assert.equal(studyLandingMode({ activeLecture: null, activeCourse: null }, null, false), "empty", "有记忆无显式导航=仍问候面");
assert.equal(studyLandingMode({ activeLecture: { sub_id: "900011" }, activeCourse: null }, null, true), "desk", "显式进入=学习桌（继续学习链的落点）");

/* ---- ③ 着陆接线静态钉 ---- */
const studySource = familySource("study");
assert.equal((studySource.match(/store\.rememberLastLecture\(/g) || []).length, 1, "记忆写入单一漏斗（handleActiveLecture 订阅）");
const funnelSlice = studySource.slice(studySource.indexOf("const handleActiveLecture"), studySource.indexOf("const handleBookmarkAction"));
assert.match(funnelSlice, /if \(lecture\) \{[\s\S]*?rememberLastLecture/, "记忆写入在讲次打开分支内（清空不写）");

const indexHtml = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
const continueIndex = indexHtml.indexOf('id="study-continue"');
const startIndex = indexHtml.indexOf('id="study-start-select"');
assert.ok(continueIndex > 0, "继续学习弱化链在着陆 DOM 中");
assert.ok(startIndex > 0 && startIndex < continueIndex, "DOM 序=视觉序=Tab 序：选择课程主钮原位在前，弱化链紧随其后（用户裁决）");
assert.match(indexHtml, /id="study-continue"[^>]*hidden/, "初始 hidden（无历史不出现=纯诗页+主钮）");
assert.equal((indexHtml.match(/id="study-continue"/g) || []).length, 1, "唯一实例");
assert.match(indexHtml, /id="study-start-select" class="empty-action"/, "选择课程保持原胶囊主钮原样原位（诗页复原）");
assert.doesNotMatch(indexHtml, /id="study-actions"/, "着陆无功能行容器（一行化方案已由用户裁决取代）");
const landingSlice = indexHtml.slice(indexHtml.indexOf('id="study-empty"'), indexHtml.indexOf('id="study-select"'));
assert.doesNotMatch(landingSlice, /home-guide-resume/, "挂起提示行不占着陆页（用户裁决；唯一宿主=选课面板概览）");

assert.match(studySource, /focusWithin\(\$\("study-empty"\)\)/, "问候面过场焦点捕获在位");
assert.match(studySource, /focusLeftEmpty && keyboardModality && studyMode === "desk"/, "继续学习键盘链落点=播放钮（指针态零变化）");
assert.match(studySource, /focusLeftEmpty && keyboardModality && studyMode === "select"/, "选择课程键盘链落点=首门课程行");
assert.match(studySource, /\$\("study-continue"\)\?\.addEventListener\("click", handleContinueLearning\)/, "直达点击已接线（精简夹具缺席安全）");
assert.match(studySource, /\$\("study-continue"\)\?\.removeEventListener\("click", handleContinueLearning\)/, "清理与接线配对");
assert.match(studySource, /unsubscribeContinueLearning\(\)/, "目录订阅清理配对");
assert.match(studySource, /renderContinueLearning\(store\); \/\* U1：着陆/, "安装时首渲染");
assert.match(studySource, /const text = `上次：\$\{label\}`;/, "弱化链可见文字=「上次：课程·讲次」（近乎隐身形态，用户裁决）");
assert.match(studySource, /setAttribute\("aria-label", `继续学习 \$\{label\}`\)/, "可访问名保留「继续学习」动作语义（弱化视觉不弱化语义）");
assert.doesNotMatch(studySource, /continue-lecture-title/, "旧两行卡结构已退役（单文字节点弱化链）");

const css = await readFile(new URL("../frontend/styles/components.css", import.meta.url), "utf8");
assert.match(css, /\.continue-lecture \{/, "弱化链样式在位（极轻文字链）");
assert.match(css, /\.continue-lecture \{[^}]*white-space: normal/, "长讲次名不入紧凑标签 nowrap 禁令");
assert.doesNotMatch(css, /\.study-actions \{/, "功能行容器样式已随用户裁决退役");

console.log("frontend continue learning behavior passed");
