import { apiV3 } from "./api.js";
import { $, clear, textElement } from "./ui.js";
import { openCourseReview } from "./course-review.js";
/* STUDY-STATS-M1 本周学习面貌（学习统计 v2 默认层）：着陆页一张卡三块纵排——
 * ①周条形「学了什么」（study_daily_seconds 按日聚合，时长定亮度）
 * ②到期待办（FSRS 到期闪卡 + 最近考核/复习计划，每行一个动作）
 * ③掌握度 top3 行动建议（五档诚实合成简版：数据不足=灰档诚实显示）。
 * 纯模型函数与 DOM 拼装分离（home-overview 同法）；首跑零数据=引导态文案，
 * 绝不渲染空图表；分钟数只在真有时长数据时出现，绝不显示 0。 */

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export const FIRST_RUN_GUIDE = "学完第一讲，这里会告诉你学得怎么样。";
export const DUE_EMPTY_TEXT = "今天没有到期的复习，学新课去吧";
export const MASTERY_CLEAR_TEXT = "都在正轨上";
/* M2-b 展开层文案闭集（单入口 dialog；只进不出）。 */
export const DETAIL_ENTRY_TEXT = "看全部课程明细";
export const DETAIL_LOADING_TEXT = "正在载入明细…";
export const DETAIL_FAILURE_TEXT = "明细暂时读不出来，关掉再试一次就好";
export const DETAIL_EMPTY_TEXT = "还没有可展示的学习明细，先去学一讲吧";
export const DETAIL_FORECAST_TITLE = "未来 7 天到期闪卡";
export const DETAIL_PARTIAL_PREFIX = "仅基于";
export const LECTURE_UNTOUCHED_TEXT = "还没看过";

/* MASTERY_PATH_LABELS 与后端 study_stats.py 同源闭集（basis → 学生可读名）。 */
const BASIS_LABELS = { quiz: "测验", fsrs: "闪卡复习", bookmark: "没听懂标记" };

function basisText(basis) {
  const paths = Array.isArray(basis) ? basis.map((path) => BASIS_LABELS[String(path) || ""]).filter(Boolean) : [];
  if (!paths.length || paths.length === 3) return "";
  return `${DETAIL_PARTIAL_PREFIX}${paths.join("和")}`;
}

function weekdayLabel(dateIso) {
  const [year, month, day] = String(dateIso || "").split("-").map(Number);
  if (![year, month, day].every(Number.isFinite)) return "";
  /* STUDY-STATS-GFIX-1 F4：getUTCDay() 周日=0，旧写法 -1 取到 undefined=周日格
     无标签无标题；(+6)%7 归一为周一=0..周日=6。 */
  return WEEKDAY_LABELS[(new Date(Date.UTC(year, month - 1, day)).getUTCDay() + 6) % 7] || "";
}

function minutesText(seconds) {
  const minutes = Math.round(Number(seconds || 0) / 60);
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分钟`;
}

/* ①周条形：7 格（周一..周日），有学习的日子点亮，时长决定亮度（0..1 归一）。
 * today 取 payload 顶层（study_overview.today），不入 week 子对象。 */
export function weekBarModel(week, today = "") {
  const days = Array.isArray(week?.days) ? week.days : [];
  const todayText = String(today || week?.today || "");
  const maxSeconds = Math.max(0, ...days.map((day) => Number(day.seconds) || 0));
  return days.map((day) => {
    const seconds = Number(day.seconds) || 0;
    const label = weekdayLabel(day.date);
    return {
      date: String(day.date || ""),
      label,
      state: !day.active ? (day.date > todayText ? "future" : "idle") : "active",
      isToday: day.date === todayText,
      intensity: maxSeconds > 0 ? Math.max(0.35, seconds / maxSeconds) : 0,
      title: day.active
        ? `${label}${seconds > 0 ? ` · ${minutesText(seconds)}` : " · 有互动"}`
        : label,
    };
  });
}

/* 主行文案：本周 N 天 · M 门课 · 看到 P%（平均完成度）· 约 X 分钟（仅有数据时）。 */
export function weekSummaryModel(week) {
  const studyDays = Number(week?.study_days) || 0;
  const courseCount = Number(week?.course_count) || 0;
  const completion = week?.avg_completion_percent;
  const minutes = week?.minutes_total;
  if (!studyDays && !minutes) {
    return { empty: true, text: "本周还没开始，选一讲开始学吧" };
  }
  const parts = [`本周学了 ${studyDays} 天`];
  if (courseCount > 0) parts.push(`${courseCount} 门课`);
  if (Number.isFinite(Number(completion)) && completion !== null) parts.push(`平均看到 ${completion}%`);
  if (Number(minutes) > 0) parts.push(`约 ${minutes} 分钟`);
  return { empty: false, text: parts.join(" · ") };
}

/* ②到期待办：只列真实到期的行（每行一个动作）；空=一句人话，不渲染空骨架。 */
export function dueRowsModel(due) {
  const rows = [];
  const flashcardsDue = Number(due?.flashcards_due) || 0;
  if (flashcardsDue > 0) {
    rows.push({
      key: "flashcards",
      text: `今天到期闪卡 ${flashcardsDue} 张`,
      action: "去复习",
      course_id: String(due?.flashcards_top_course_id || ""),
    });
  }
  const exam = due?.next_exam;
  if (exam && Number(exam.days_left) >= 0) {
    const title = String(exam.title || "").trim() || "下次考核";
    rows.push({
      key: "exam",
      text: Number(exam.days_left) > 0 ? `${title}还有 ${exam.days_left} 天` : `${title}就是明天`,
      action: "看安排",
      course_id: String(exam.course_id || ""),
    });
  }
  return rows;
}

/* ③掌握度 top3：行=课程+档位+一句计数证据；数据不足=灰档诚实显示。 */
export function masteryRowsModel(mastery, courseTitles = new Map()) {
  const rows = (Array.isArray(mastery?.courses) ? mastery.courses : []).map((course) => ({
    course_id: String(course.course_id || ""),
    title: courseTitles.get(String(course.course_id || "")) || "这门课",
    tier: String(course.tier || ""),
    gray: course.gray === true,
    partialNote: basisText(course.basis),
    evidence: String(course.evidence || ""),
  }));
  return { rows, allClear: mastery?.all_clear === true && rows.length === 0 };
}

/* ---- M2-b 展开层：FSRS 7 日预测条 + 课程→讲明细（纯模型与 DOM 分离） ---- */

/* 预测条 7 格：今天..+6 天，到期张数定亮度（同周条形语言）；今天格单独点名。 */
export function forecastBarModel(forecast, today = "") {
  const days = Array.isArray(forecast) ? forecast : [];
  const maxDue = Math.max(0, ...days.map((day) => Number(day.due) || 0));
  return days.map((day, index) => {
    const due = Number(day.due) || 0;
    const label = index === 0 ? "今天" : weekdayLabel(day.date);
    return {
      date: String(day.date || ""),
      label,
      due,
      isToday: index === 0 || day.date === today,
      intensity: maxDue > 0 ? Math.max(0.35, due / maxDue) : 0,
      title: `${label}${due > 0 ? `到期 ${due} 张` : "没有到期的闪卡"}`,
    };
  });
}

/* 课程行模型：档位徽章+单路标注+完成度/周互动/到期卡汇总+讲明细行。 */
export function detailCourseRowsModel(courses, courseTitles = new Map()) {
  return (Array.isArray(courses) ? courses : []).map((course) => {
    const lectures = (Array.isArray(course.lectures) ? course.lectures : []).map((row) => ({
      sub_id: String(row.sub_id || ""),
      label: String(row.label || "") || String(row.sub_id || ""),
      percent: row.percent,
      percentText: row.percent == null ? LECTURE_UNTOUCHED_TEXT : `看到 ${row.percent}%`,
      completed: row.completed === true,
      seconds: Number(row.seconds) || 0,
      replays: Number(row.replays) || 0,
      openBookmarks: Number(row.open_bookmarks) || 0,
      quizGraded: Number(row.quiz_graded) || 0,
      quizCorrect: Number(row.quiz_correct) || 0,
      quizUngraded: Number(row.quiz_ungraded) || 0,
      lastCorrect: row.last_correct,
      flashcardsDue: Number(row.flashcards_due) || 0,
    }));
    const flashcardsDue = lectures.reduce((sum, row) => sum + row.flashcardsDue, 0);
    const completion = course.completion_percent;
    return {
      course_id: String(course.course_id || ""),
      title: courseTitles.get(String(course.course_id || "")) || "这门课",
      tier: String(course.tier || ""),
      gray: course.gray === true,
      partialNote: basisText(course.basis),
      evidence: String(course.evidence || ""),
      completionText:
        completion == null ? "" : `平均看到 ${completion}%`,
      weekInteractions: Number(course.week_interactions) || 0,
      flashcardsDue,
      lectures,
    };
  });
}

function forecastNode(bar) {
  const wrap = document.createElement("div");
  wrap.className = "p5s-week p5s-forecast";
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", `${DETAIL_FORECAST_TITLE}：${bar.map((cell) => cell.title).join("，")}`);
  for (const cell of bar) {
    const day = document.createElement("span");
    day.className = `p5s-day${cell.isToday ? " p5s-day-today" : ""}`;
    day.dataset.state = cell.due > 0 ? "active" : "idle";
    if (cell.due > 0) {
      const fill = document.createElement("span");
      fill.className = "p5s-day-fill";
      fill.style.height = `${Math.round(cell.intensity * 100)}%`;
      day.append(fill);
      const count = textElement("span", String(cell.due), "p5s-day-label p5s-forecast-count");
      day.append(count);
    }
    day.title = cell.title;
    day.append(textElement("span", cell.label, "p5s-day-label p5s-forecast-label"));
    wrap.append(day);
  }
  return wrap;
}

/* 讲明细行：讲次+时长/完成度/热点/书签/测验/到期卡，六列计数全部诚实三态。 */
function lectureRowNode(row) {
  const line = document.createElement("div");
  line.className = "p5s-lecture";
  line.append(textElement("span", row.label, "p5s-lecture-label"));
  const facts = document.createElement("span");
  facts.className = "p5s-lecture-facts";
  const parts = [];
  if (row.seconds > 0) parts.push(minutesText(row.seconds));
  parts.push(row.percentText);
  parts.push(row.replays > 0 ? `回看 ${row.replays} 次` : "");
  parts.push(row.openBookmarks > 0 ? `没听懂 ${row.openBookmarks} 处` : "");
  if (row.quizGraded > 0) {
    let quiz = `测验对 ${row.quizCorrect}/${row.quizGraded}`;
    if (row.quizUngraded > 0) quiz += `（${row.quizUngraded} 题未判）`;
    parts.push(quiz);
  } else if (row.quizUngraded > 0) {
    parts.push(`${row.quizUngraded} 题未判`);
  } else {
    parts.push("还没做测验");
  }
  parts.push(row.flashcardsDue > 0 ? `到期 ${row.flashcardsDue} 张` : "");
  for (const part of parts.filter(Boolean)) {
    facts.append(textElement("span", part, "p5s-lecture-fact"));
  }
  line.append(facts);
  return line;
}

function detailCourseNode(row) {
  const details = document.createElement("details");
  details.className = "p5s-course";
  const summary = document.createElement("summary");
  summary.className = "p5s-course-summary";
  summary.append(textElement("span", row.title, "p5s-mastery-course"));
  const tier = textElement("span", row.tier, `p5s-tier${row.gray ? " p5s-tier-gray" : ""}`);
  tier.dataset.tier = row.tier;
  summary.append(tier);
  if (row.partialNote) {
    summary.append(textElement("span", `（${row.partialNote}）`, "p5s-course-partial"));
  }
  const meta = [row.completionText, row.weekInteractions > 0 ? `本周互动 ${row.weekInteractions} 次` : "", row.flashcardsDue > 0 ? `到期 ${row.flashcardsDue} 张` : ""]
    .filter(Boolean)
    .join(" · ");
  if (meta) summary.append(textElement("span", meta, "p5s-course-meta"));
  details.append(summary);
  if (row.evidence) {
    details.append(textElement("p", row.evidence, "p5s-mastery-evidence p5s-course-evidence"));
  }
  const list = document.createElement("div");
  list.className = "p5s-lectures";
  for (const lecture of row.lectures) list.append(lectureRowNode(lecture));
  details.append(list);
  return details;
}

/* 渲染展开层 dialog 内容：预测条+课程列表（首次=引导态，绝不渲染空骨架）。 */
export function renderStudyDetail(dialog, payload, courseTitles = new Map()) {
  if (!dialog) return;
  const forecastMount = dialog.querySelector("#study-detail-forecast");
  const coursesMount = dialog.querySelector("#study-detail-courses");
  if (!forecastMount || !coursesMount) return;
  clear(forecastMount);
  clear(coursesMount);
  if (!payload || !Array.isArray(payload.courses)) {
    coursesMount.append(textElement("p", DETAIL_EMPTY_TEXT, "p5s-empty"));
    return;
  }
  const hasForecast = Array.isArray(payload.forecast) && payload.forecast.some((day) => Number(day.due) > 0);
  if (hasForecast) {
    const block = document.createElement("div");
    block.className = "p5s-block";
    block.append(textElement("span", DETAIL_FORECAST_TITLE, "p5s-block-title"));
    block.append(forecastNode(forecastBarModel(payload.forecast, payload.today || "")));
    forecastMount.append(block);
  }
  const rows = detailCourseRowsModel(payload.courses, courseTitles);
  if (!rows.length) {
    coursesMount.append(textElement("p", DETAIL_EMPTY_TEXT, "p5s-empty"));
    return;
  }
  for (const row of rows) coursesMount.append(detailCourseNode(row));
}

export function studyStatsModel(payload) {
  if (!payload || payload.first_run === true || !payload.week || !Object.keys(payload.week).length) {
    return { mode: "first-run" };
  }
  return {
    mode: "ready",
    bar: weekBarModel(payload.week, payload.today),
    summary: weekSummaryModel(payload.week),
    due: dueRowsModel(payload.due),
    masteryRaw: payload.mastery || null,
  };
}

function weekBarNode(bar) {
  const wrap = document.createElement("div");
  wrap.className = "p5s-week";
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", bar.map((cell) => cell.title).join("，"));
  for (const cell of bar) {
    const day = document.createElement("span");
    day.className = `p5s-day${cell.isToday ? " p5s-day-today" : ""}`;
    day.dataset.state = cell.state;
    if (cell.state === "active") {
      /* 亮度=底部锚定填充条高度（同 schedule-now 进度条语言；S09-D 全站唯一
         gradient 合同保留给播放器 scrim，此处零渐变纯高度）。 */
      const fill = document.createElement("span");
      fill.className = "p5s-day-fill";
      fill.style.height = `${Math.round(cell.intensity * 100)}%`;
      day.append(fill);
    }
    day.title = cell.title;
    day.append(textElement("span", cell.label.slice(1), "p5s-day-label"));
    wrap.append(day);
  }
  return wrap;
}

function dueNode(due, handlers) {
  const wrap = document.createElement("div");
  wrap.className = "p5s-due";
  if (!due.length) {
    wrap.append(textElement("p", DUE_EMPTY_TEXT, "p5s-empty"));
    return wrap;
  }
  for (const row of due) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "p5s-row";
    button.dataset.dueKey = row.key;
    button.append(textElement("span", `▸ ${row.text}`, "p5s-row-text"));
    button.append(textElement("span", row.action, "p5s-row-action"));
    button.addEventListener("click", () => handlers?.onDue?.(row));
    wrap.append(button);
  }
  return wrap;
}

function masteryNode(mastery, handlers = {}) {
  const wrap = document.createElement("div");
  wrap.className = "p5s-mastery";
  if (mastery.allClear) {
    wrap.append(textElement("p", MASTERY_CLEAR_TEXT, "p5s-empty"));
  } else if (mastery.rows.length) {
    for (const row of mastery.rows) {
      const line = document.createElement("p");
      line.className = "p5s-mastery-row";
      line.append(textElement("span", row.title, "p5s-mastery-course"));
      const tier = textElement("span", row.tier, `p5s-tier${row.gray ? " p5s-tier-gray" : ""}`);
      tier.dataset.tier = row.tier; /* 档位进无障碍树与样式钩子（五档可辨） */
      line.append(tier);
      /* M2-a 单路标注：仅一路数据时诚实注明「仅基于 X」（与后端 basis 同源）。 */
      if (row.partialNote) {
        line.append(textElement("span", `（${row.partialNote}）`, "p5s-course-partial"));
      }
      line.append(textElement("span", row.evidence, "p5s-mastery-evidence"));
      wrap.append(line);
    }
  }
  /* M2-b 展开层单入口：掌握度块底部一个按钮进 dialog，只进不出、零新增常驻导航。 */
  if (typeof handlers.onOpenDetail === "function") {
    const entry = document.createElement("button");
    entry.type = "button";
    entry.className = "p5s-detail-entry";
    entry.textContent = DETAIL_ENTRY_TEXT;
    entry.addEventListener("click", () => handlers.onOpenDetail());
    wrap.append(entry);
  }
  return wrap;
}

/* 渲染整卡：first-run=引导态；ready=三块纵排。mount=#study-week-card。 */
export function renderStudyWeekCard(mount, payload, handlers = {}) {
  if (!mount) return;
  clear(mount);
  const model = studyStatsModel(payload);
  mount.append(textElement("span", "本周学习面貌", "s-title p5s-title"));
  if (model.mode === "first-run") {
    mount.append(textElement("p", FIRST_RUN_GUIDE, "p5s-guide"));
    return;
  }
  const courseTitles = handlers.courseTitles instanceof Map ? handlers.courseTitles : new Map();
  mount.append(textElement("span", model.summary.text, "p5s-summary"));
  const week = document.createElement("div");
  week.className = "p5s-block";
  week.append(textElement("span", "学了什么", "p5s-block-title"));
  week.append(weekBarNode(model.bar));
  if (model.summary.empty) week.append(textElement("p", model.summary.text, "p5s-empty"));
  mount.append(week);
  const due = document.createElement("div");
  due.className = "p5s-block";
  due.append(textElement("span", "到期待办", "p5s-block-title"));
  due.append(dueNode(model.due, handlers));
  mount.append(due);
  const mastery = document.createElement("div");
  mastery.className = "p5s-block";
  mastery.append(textElement("span", "掌握度", "p5s-block-title"));
  mastery.append(masteryNode(masteryRowsModel(payload.mastery, courseTitles), handlers));
  mount.append(mastery);
}

/* 行动作：闪卡行=直达到期最多课程的闪卡复习；安排行=回学习页复习区。 */
export async function handleDueAction(store, row) {
  if (row?.key === "flashcards") {
    const course = (store.courses || []).find(
      (item) => String(item?.course_id || "") === String(row.course_id || ""),
    );
    if (course) {
      store.set("activeCourse", course);
      await openCourseReview(store);
      document.querySelector("[role='tab'][data-review-tab='flashcards']")?.click();
      return;
    }
  }
  window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));
  document.querySelector("[role='tab'][data-material-tab='review']")?.click();
}

export function installStudyStats(store) {
  const mount = $("study-week-card");
  if (!mount) return () => {};
  let disposed = false;
  let controller = null;
  let lastPayload = null;
  const courseTitles = () => new Map(
    (store.courses || []).map((course) => [String(course?.course_id || ""), String(course?.title || "")]),
  );
  const handlers = { onDue: (row) => void handleDueAction(store, row) };
  /* M2-b 展开层：单入口 dialog（只进不出）。打开即现缓存明细（零呆等），
     后台重取一次保新鲜；失败保留已渲染内容并给人话提示。 */
  let lastDetail = null;
  let detailController = null;
  const stateLine = () => $("study-detail-state");
  const showDetailState = (text) => {
    const line = stateLine();
    if (line) line.textContent = String(text || "");
  };
  const renderDetail = (value) => {
    renderStudyDetail($("study-detail-dialog"), value, courseTitles());
  };
  const fetchDetail = async () => {
    detailController?.abort();
    const controller = new AbortController();
    detailController = controller;
    try {
      const value = await apiV3("study/detail", { controller });
      if (disposed || detailController !== controller) return;
      lastDetail = value;
      renderDetail(value);
      showDetailState("");
    } catch {
      if (!disposed && !lastDetail) showDetailState(DETAIL_FAILURE_TEXT);
      else if (!disposed) showDetailState("");
    }
  };
  handlers.onOpenDetail = () => {
    const dialog = $("study-detail-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return;
    if (lastDetail) renderDetail(lastDetail);
    else showDetailState(DETAIL_LOADING_TEXT);
    dialog.showModal();
    void fetchDetail();
  };
  const render = (value) => {
    lastPayload = value;
    renderStudyWeekCard(mount, value, { ...handlers, courseTitles: courseTitles() });
  };
  const hydrate = async () => {
    if (disposed) return;
    controller?.abort();
    const requestController = new AbortController();
    controller = requestController;
    try {
      const value = await apiV3("study/overview", { controller: requestController });
      if (!disposed && controller === requestController) render(value);
    } catch {
      /* 本地读面失败：保持引导态占位，绝不打扰着陆页 */
    }
  };
  /* STUDY-STATS-GFIX-1 F3：目录后到重渲染。boot 水合时 store.courses 常未载入
     （掌握度行曾全匿名「这门课」），旧重水合只挂 courselens:page==="study"——
     选课默认流不经过。订阅 courses：已有 payload 就地重渲染（零重复请求），
     尚无 payload 则补一次水合。 */
  const unsubscribeCourses = typeof store.subscribe === "function"
    ? store.subscribe("courses", () => {
      if (disposed) return;
      if (lastPayload) render(lastPayload);
      else void hydrate();
    })
    : () => {};
  /* STUDY-STATS-GFIX-1 F1：三态宿主重挂（D-20261009-09 home-guide-resume 同法）。
     卡原挂 #study-select（选课面板独占区）内——首跑诗页与续学学习桌两着陆态皆
     不可见，「首跑引导态」与「到卡 0 点击」验收判据在真实流程不可达。卡保持
     唯一节点，随学习面三态迁移宿主：empty=诗页、select=今日概览原位、desk=桌首。 */
  const syncHost = (mode) => {
    if (disposed) return;
    let host = null;
    if (mode === "empty") {
      host = $("study-week-card-host-empty");
    } else if (mode === "desk") {
      host = $("study-week-card-host-desk");
    } else {
      const select = $("study-select");
      host = select && typeof select.querySelector === "function"
        ? select.querySelector(".home-overview")
        : null;
    }
    if (!host || typeof host.append !== "function") return; /* 精简壳无宿主：卡留守原位 */
    const current = mount.parentElement || mount.parent;
    if (current !== host) host.append(mount);
  };
  const handleStudyMode = (event) => syncHost(event?.detail);
  const currentMode = () => {
    const select = $("study-select");
    const desk = $("study-desk");
    if (select && !select.hidden) return "select";
    if (desk && !desk.hidden) return "desk";
    return "empty";
  };
  const handlePage = (event) => {
    if (event?.detail === "study") void hydrate();
  };
  const dialog = $("study-detail-dialog");
  const closeDialog = $("close-study-detail-dialog");
  const handleDialogClose = () => {
    const target = $("study-detail-dialog");
    if (target && typeof target.close === "function" && target.open) target.close();
  };
  closeDialog?.addEventListener("click", handleDialogClose);
  window.addEventListener("courselens:page", handlePage);
  window.addEventListener("courselens:study-mode", handleStudyMode);
  syncHost(currentMode());
  void hydrate();
  return () => {
    if (disposed) return;
    disposed = true;
    controller?.abort();
    controller = null;
    detailController?.abort();
    detailController = null;
    closeDialog?.removeEventListener("click", handleDialogClose);
    window.removeEventListener("courselens:page", handlePage);
    window.removeEventListener("courselens:study-mode", handleStudyMode);
    unsubscribeCourses();
  };
}
