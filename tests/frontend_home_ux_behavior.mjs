import assert from "node:assert/strict";
import { familySource } from "./frontend_exec_harness.mjs";
import { readFile } from "node:fs/promises";

const nodes = new Map();
const node = () => ({ attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } });
nodes.set("theme-toggle", node());
nodes.set("settings-theme-mode", node());
globalThis.document = {
  documentElement: { dataset: {} },
  getElementById: (id) => nodes.get(id) || null,
};
const saved = new Map();
globalThis.localStorage = { setItem: (key, value) => saved.set(key, value), getItem: (key) => saved.get(key) || null };
globalThis.matchMedia = () => ({ matches: true });

const { THEME_PREF_KEY, resolveThemeForMoment, readThemePreference, applyThemePreference } = await import("../frontend/modules/shell.js");
const { currentCourseTerm, filterCoursesByTerm, renderCourses, studyLandingMode } = await import("../frontend/modules/study.js");
const { friendlyTerm } = await import("../frontend/modules/ui.js");

const courses = [
  { course_id: "fall", term: "2025-2026-1" },
  { course_id: "spring", term: "2025-2026-2" },
  { course_id: "next", term: "2026-2027-1" },
];
assert.equal(currentCourseTerm(courses, new Date(2026, 0, 15)), "2025-2026-1");
assert.equal(currentCourseTerm(courses, new Date(2026, 7, 1)), "2026-2027-1");
assert.deepEqual(filterCoursesByTerm(courses, "current", new Date(2026, 0, 15)).map(({ course_id }) => course_id), ["fall"]);
assert.deepEqual(filterCoursesByTerm(courses, "2025-2026-2").map(({ course_id }) => course_id), ["spring"]);
assert.deepEqual(filterCoursesByTerm(courses, "all").map(({ course_id }) => course_id), ["fall", "spring", "next"]);

/* S06-A：紧凑学期码（YYYY-YYYY1 / YYYY-YYYY2）——秋/春时钟各识别其一，未知后缀不发明当前学期 */
const compactCourses = [
  { course_id: "compact-fall", term: "2026-20271" },
  { course_id: "compact-spring", term: "2026-20272" },
];
assert.equal(currentCourseTerm(compactCourses, new Date(2026, 7, 1)), "2026-20271");
assert.equal(currentCourseTerm(compactCourses, new Date(2027, 2, 1)), "2026-20272");
assert.equal(currentCourseTerm([{ course_id: "prev-fall", term: "2025-20261" }], new Date(2026, 0, 15)), "2025-20261", "1月仍属上学年秋季学期（紧凑码）");
assert.deepEqual(filterCoursesByTerm(compactCourses, "current", new Date(2026, 7, 1)).map(({ course_id }) => course_id), ["compact-fall"]);
assert.deepEqual(filterCoursesByTerm(compactCourses, "current", new Date(2027, 2, 1)).map(({ course_id }) => course_id), ["compact-spring"]);
assert.equal(currentCourseTerm([{ course_id: "odd", term: "2026-2027X1" }], new Date(2026, 7, 1)), "", "未知字母后缀不发明当前学期");
assert.equal(currentCourseTerm([{ course_id: "odd", term: "2026-202711" }], new Date(2026, 7, 1)), "", "超长数字后缀不发明当前学期");
assert.equal(currentCourseTerm([{ course_id: "odd", term: "2026-20272" }], new Date(2026, 7, 1)), "", "秋季时钟不得误认春季紧凑码");

/* P56-U1：主题三态——时刻→明暗闭表（注入时刻防时辰 flake，19:00–07:00 深色） */
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 18, 59)), "light", "18:59 仍浅色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 19, 0)), "dark", "19:00 起深色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 23, 59)), "dark", "23:59 深色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 0, 0)), "dark", "午夜深色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 6, 59)), "dark", "06:59 仍深色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 7, 0)), "light", "07:00 起浅色");
assert.equal(resolveThemeForMoment(new Date(2026, 8, 24, 12, 0)), "light", "正午浅色");

/* v1 一律不再读：现存 v1=dark 不再影响解析（本 bug 修复面）；v2 未设=自动 */
saved.set("courselens.theme.v1", "dark");
assert.equal(readThemePreference(), "auto", "v2 未设=自动，v1=dark 被无视");
assert.equal(applyThemePreference("auto", { now: new Date(2026, 8, 24, 15, 0) }), "light", "自动档下午解析浅色");
assert.equal(saved.get("courselens.theme.v2"), "auto", "自动档也持久 v2=auto");
assert.equal(document.documentElement.dataset.theme, "light");

/* 手动选择持久 v2 显式值=退出自动；三态控件回显偏好 */
applyThemePreference("dark");
assert.equal(saved.get(THEME_PREF_KEY), "dark");
assert.equal(document.documentElement.dataset.theme, "dark");
assert.equal(nodes.get("theme-toggle").attrs["aria-pressed"], "true");
assert.equal(nodes.get("settings-theme-mode").value, "dark", "设置页三态控件回显显式偏好");
/* F3（化身走查 20261008）：主题钮=状态钮——可达名固定状态「深色主题」，
   pressed=深色是否生效，播报与视觉恒一致；动作语义只走 title 悬停。 */
assert.equal(nodes.get("theme-toggle").attrs["aria-label"], "深色主题", "深色态可达名=固定状态词");
assert.match(nodes.get("theme-toggle").attrs["title"], /切换到浅色主题/, "深色态 title=动作描述");
applyThemePreference("light");
assert.equal(nodes.get("settings-theme-mode").value, "light");
assert.equal(nodes.get("theme-toggle").attrs["aria-pressed"], "false", "浅色=深色主题未按下（状态与视觉一致）");
assert.equal(nodes.get("theme-toggle").attrs["aria-label"], "深色主题", "浅色态可达名不变（固定状态词）");
assert.match(nodes.get("theme-toggle").attrs["title"], /切换到深色主题/, "浅色态 title=动作描述");

const [study, accessibility] = await Promise.all([
  familySource("study"),
  readFile(new URL("../frontend/styles/accessibility.css", import.meta.url), "utf8"),
]);
assert.match(study, /localStorage\.setItem\(COURSE_TERM_FILTER_KEY, courseTermFilter\)/);
/* D-20261009-09：学习面三态过场广播 courselens:study-mode——onboarding 挂起恢复行
   据此重挂可见宿主（问候 hero/选课概览）；仅在真实过场派发（区域 hidden 置位后）。 */
assert.match(study, /courselens:study-mode/, "学习面过场广播存在（恢复行宿主联动）");
assert.match(study, /previousMode !== studyMode\)\s*\{[^}]*courselens:study-mode/s, "广播仅在真实过场派发（渲染零扰动）");
assert.match(accessibility, /prefers-reduced-motion: reduce/);
assert.match(accessibility, /::view-transition-new\(root\)/);

/* ---- SMALL-POLISH-1② 启动落地门：自动登录会话恢复完成后落在问候/欢迎面，
 * 绝不自动导航选择面/学习桌；显式导航才解除。纯模型真值表 + 接线源码钉。 */

const bootCourse = { course_id: "c1" };
assert.equal(studyLandingMode({ activeLecture: null, livePlayback: "", activeCourse: bootCourse }, null, false),
  "empty", "启动落地门：目录自动选中已在内存，仍落问候面");
assert.equal(studyLandingMode({ activeLecture: null, livePlayback: "", activeCourse: bootCourse }, "select", false),
  "empty", "落地门优先于选择面覆盖");
assert.equal(studyLandingMode({ activeLecture: { sub_id: "l1" }, livePlayback: "", activeCourse: null }, null, false),
  "empty", "落地门：讲次在内存但未显式进入不亮学习桌");
assert.equal(studyLandingMode({ activeLecture: { sub_id: "l1" }, livePlayback: "", activeCourse: null }, null, true),
  "desk", "显式讲次进入学习桌");
assert.equal(studyLandingMode({ activeLecture: null, livePlayback: "", activeCourse: bootCourse }, "select", true),
  "select", "问候 CTA 解除落地门进选择面");
assert.equal(studyLandingMode({ activeLecture: null, livePlayback: "课程直播", activeCourse: null }, null, true),
  "empty", "直播播放键不再开启学习桌（N6L S1 U3：直播归独立页）");
assert.equal(studyLandingMode({ activeLecture: null, livePlayback: "", activeCourse: null }, null, true),
  "empty", "无课程上下文回问候面");

assert.match(study, /let studyLandingNavigated = false;/);
assert.match(study, /studyMode = studyLandingMode\(store, modeOverride, studyLandingNavigated\);/);
assert.match(study, /let autoSelectingCatalog = false;/);
/* WP1-D4②：非自动选中订阅=解除落地门+完整应用链（courseChosen/按压态/renderLectures） */
assert.match(study, /if \(course && !autoSelectingCatalog\) \{\s*\n\s*studyLandingNavigated = true;/);
/* 解除点计数：课程行点击 / 讲次进入 / 字标返回 / 问候 CTA / 非自动选中订阅 = 5
   （N6L S1 U3：直播进入解除点随 live-only 桌面退役） */
assert.equal([...study.matchAll(/studyLandingNavigated = true;/g)].length, 5, "解除点恰好五处");
/* 自动选中必须包在 autoSelectingCatalog 标记内（内存高亮语义，不解除落地门） */
assert.match(study, /autoSelectingCatalog = true;\s*\n\s*try \{\s*\n\s*store\.set\("activeCourse", selected\);/);

/* ---- P2-1 行为级回归：学期过滤滤掉 active course 时必须清除旧 activeLecture ----
 * 缺陷：过滤结果非空但 active course 被滤掉时，回退高亮第一门课而 activeLecture
 * 仍指向旧课程讲次——课程列表高亮与学习桌/面包屑播放目标指向不同课程。 */

class FakeNode {
  constructor(id = "") {
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      toggle: (name, enabled) => {
        if (enabled === undefined) return this.classList.values.has(name) ? this.classList.values.delete(name) : this.classList.values.add(name);
        return enabled ? (this.classList.values.add(name), true) : (this.classList.values.delete(name), false);
      },
      contains: (name) => this.classList.values.has(name),
    };
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener() {}
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => (node.children || []).forEach((child) => {
      const classNames = String(child.className || "").split(/\s+/);
      if (String(selector).startsWith(".") && classNames.includes(selector.slice(1))) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const renderNodes = new Map();
for (const id of [
  "study-course-list", "study-lecture-list", "study-course-title", "study-course-meta",
  "catalog-term-filter", "catalog-evidence", "catalog-recovery", "catalog-recovery-title",
  "catalog-recovery-impact", "catalog-recovery-actions", "catalog-diagnostic-code",
]) renderNodes.set(id, new FakeNode(id));
globalThis.CSS = { escape: (value) => String(value) };
globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
const baseGetElementById = document.getElementById;
document.getElementById = (id) => renderNodes.get(id) || baseGetElementById(id);
document.createElement = (tag) => new FakeNode(`created-${tag}`);
document.activeElement = null;

const ghostStore = {
  courses: [], activeCourse: null, activeLecture: null,
  set(key, value) { this[key] = value; },
  subscribe() { return () => {}; },
};
const byRow = (container, className) => container.children.filter((node) => String(node.className || "").split(/\s+/).includes(className));

/* 按 currentCourseTerm 已锁定的规则推导"今天"所在学期，构造跨学期目录 */
const today = new Date();
const academicStart = today.getMonth() >= 7 ? today.getFullYear() : today.getFullYear() - 1;
const currentSemester = today.getMonth() >= 7 || today.getMonth() === 0 ? "1" : "2";
const currentTermFixture = `${academicStart}-${academicStart + 1}-${currentSemester}`;
const otherTermFixture = `${academicStart}-${academicStart + 1}-${currentSemester === "1" ? "2" : "1"}`;
const fallCourse = { course_id: "c-fall", title: "秋季课程", term: currentTermFixture, lectures: [{ sub_id: "l-fall", sub_title: "秋季讲次", can_stream: true }] };
const springCourse = { course_id: "c-spring", title: "春季课程", term: otherTermFixture, lectures: [{ sub_id: "l-spring", sub_title: "春季讲次", can_stream: false }] };

/* 场景 1（缺陷分支）：过滤结果非空但 activeCourse（春季课）被滤掉 → 旧讲次必须清除 */
ghostStore.activeCourse = springCourse;
ghostStore.activeLecture = { course_id: "c-spring", sub_id: "l-spring", sub_title: "春季讲次", course_title: "春季课程" };
renderCourses(ghostStore, { state: "ready", code: "authorized_catalog_verified", courses: [springCourse, fallCourse], course_count: 2 });
assert.equal(ghostStore.activeLecture, null, "被滤掉课程的 activeLecture 被清除（学习桌不再指向旧播放目标）");
assert.equal(ghostStore.activeCourse.course_id, "c-fall", "回退高亮过滤结果的第一门课程");
const courseRows = byRow(renderNodes.get("study-course-list"), "course-row");
assert.equal(courseRows.length, 1, "过滤后只渲染当前学期课程");
/* 行壳非交互：aria-current 落在壳内的导航按钮（course-row-main）上 */
const courseMainButton = courseRows[0].children.find((node) => node.className === "course-row-main");
assert.ok(courseMainButton, "课程行壳包含兄弟导航按钮");
assert.equal(courseMainButton.getAttribute("aria-current"), "true", "课程列表高亮与 activeCourse 一致");
assert.ok(
  courseRows[0].children.some((node) => String(node.className || "").includes("course-automation-toggle")),
  "课程行壳包含兄弟自动整理开关（按钮不嵌套按钮）",
);
const lectureRows = byRow(renderNodes.get("study-lecture-list"), "lecture-row");
assert.ok(lectureRows.length >= 1, "回退课程讲次列表已渲染");
assert.ok(lectureRows.every((row) => !row.getAttribute("aria-current")), "讲次列表无旧讲次残留高亮");

/* 场景 2（防过度清除）：activeCourse 仍在过滤结果中 → activeLecture 原样保留且讲次行保持高亮 */
ghostStore.activeCourse = fallCourse;
ghostStore.activeLecture = { course_id: "c-fall", sub_id: "l-fall", sub_title: "秋季讲次", course_title: "秋季课程" };
renderCourses(ghostStore, { state: "ready", code: "authorized_catalog_verified", courses: [fallCourse, springCourse], course_count: 2 });
assert.equal(ghostStore.activeLecture?.sub_id, "l-fall", "activeCourse 未被滤掉时保留播放目标");
const pressedLectureRows = byRow(renderNodes.get("study-lecture-list"), "lecture-row").filter((row) => row.getAttribute("aria-current"));
assert.equal(pressedLectureRows.length, 1, "讲次行高亮唯一");
assert.equal(pressedLectureRows[0].dataset.subId, "l-fall", "高亮落在当前讲次上");

/* 场景 3（S05-A 可见回落）：当前学期视图过滤未命中≠目录为空 → 可见回落全部学期，
 * 选课与讲次状态保留、不渲染空态文案；回落只改内存视图态，绝不落盘。 */
ghostStore.activeCourse = springCourse;
ghostStore.activeLecture = { course_id: "c-spring", sub_id: "l-spring", sub_title: "春季讲次", course_title: "春季课程" };
renderCourses(ghostStore, { state: "ready", code: "authorized_catalog_verified", courses: [springCourse], course_count: 1 });
assert.equal(ghostStore.activeCourse, springCourse, "视图过滤未命中回落全部学期时保留选课");
assert.equal(ghostStore.activeLecture?.sub_id, "l-spring", "视图过滤未命中回落全部学期时保留讲次");
assert.equal(renderNodes.get("catalog-term-filter").value, "all", "回落可见：学期选择器显示全部学期");
const fallbackRows = byRow(renderNodes.get("study-course-list"), "course-row");
assert.equal(fallbackRows.length, 1, "回落视图渲染唯一课程行");
assert.ok(renderNodes.get("study-course-list").children.every((node) => !String(node.textContent).includes("没有课程")), "回落视图不渲染空态文案");

/* ---- U⑬（第廿四案）：学期串人话化 ---- */
{
  assert.equal(friendlyTerm("2026-20271"), "2026-2027 · 第 1 学期", "紧凑码人话化");
  assert.equal(friendlyTerm("2025-20262"), "2025-2026 · 第 2 学期", "紧凑春季学期");
  assert.equal(friendlyTerm("2026-2027-1"), "2026-2027 · 第 1 学期", "连字符形态");
  assert.equal(friendlyTerm("2026-2027学年第2学期"), "2026-2027 · 第 2 学期", "学年学期形态");
  assert.equal(friendlyTerm("2026-2027X1"), "2026-2027X1", "未知形态诚实原样");
  assert.equal(friendlyTerm(""), "", "空串原样");
  /* 匹配器吃原始串，展示层换装不改过滤键（值仍为原始串） */
  assert.equal(currentCourseTerm([{ course_id: "c", term: "2026-20271" }], new Date(2026, 7, 1)), "2026-20271", "人话化不改数据层匹配");
  console.log("ok: U⑬ 学期串人话化");
}

console.log("frontend_home_ux_behavior: all assertions passed");
