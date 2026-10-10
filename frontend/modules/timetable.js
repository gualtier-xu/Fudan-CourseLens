import { apiV3, postV3 } from "./api.js";
import { syncDropdown } from "./dropdown.js";
import { $, clear, evidenceText, formatTime, operationId, setBusy, textElement, toast } from "./ui.js";
import {
  DEFAULT_SLOTS, WEEK_SLOT_HEIGHT, WEEKDAY_NAMES, friendlyDateRange, renderScheduleNow, scheduleWidgetAria,
  selectNowNext, weekGridModel,
} from "./home-overview.js";

let selectedWeek = 0;       /* 浮层当前浏览周；0 = 未指定（读取服务端默认的真实本周） */
let selectedSemester = "";
let homeSnapshot = null;    /* 主页“今日与本周安排”、Now/Next 与对外广播共享的真实本周快照 */
let browsedSnapshot = null; /* 浮层周标签/网格/冲突当前展示的浏览周快照（可与本周不同） */
let controller = null;
let seq = 0;
/* 缓存过期（timetable_stale）episode 的自动刷新防循环标志：一个 episode 只自动刷新一次，
   仅在渲染到非 stale 快照或登出/未登录时复位；refreshInFlight 防止在途刷新期间重复自动触发 */
let staleEpisodeAutoRefreshed = false;
let refreshInFlight = false;

/* refresh 双源失败列表（partial_failures）里的来源名 → 用户可读名称；未知来源原样展示 */
const TIMETABLE_SOURCE_NAMES = Object.freeze({
  fudan_undergraduate: "本科课程来源",
  fudan_postgraduate: "研究生课程来源",
});

/* 默认读最近一次应用的成功响应（renderSnapshot 先于 renderState 执行）；workbench 钉住本调用点无参形态 */
function partialFailureNames() {
  const failures = Array.isArray(browsedSnapshot?.partial_failures) ? browsedSnapshot.partial_failures : [];
  const names = [];
  failures.forEach((item) => {
    const source = String(item?.source || "");
    const label = TIMETABLE_SOURCE_NAMES[source] || source || "未知来源";
    if (!names.includes(label)) names.push(label);
  });
  return names.join("、");
}

function authReady(store) {
  return store.auth?.state === "ready";
}

function todayIso() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

/* 真实周次真值：优先主页本周快照，浏览快照兜底；都缺失时按第 1 周。
   快照属于其他学期时其周次对本学期无意义（学期切换未收敛）：返回 0，
   让「回到本周」等调用方不带周次走服务端默认视图，绝不拿旧学期周次请求新学期。 */
function currentWeekNumber() {
  const snapshotWeek = (snapshot) => {
    if (!snapshot) return 0;
    const semester = String(snapshot.selected_semester?.semester_id || "");
    if (selectedSemester && semester && semester !== selectedSemester) return 0;
    return Number(snapshot.current_week || 0);
  };
  return snapshotWeek(homeSnapshot) || snapshotWeek(browsedSnapshot) || 1;
}

function meetingTimeRange(meeting) {
  return `${meeting.start_time || ""}–${meeting.end_time || ""}`;
}

function courseForMeeting(store, meeting) {
  const courseId = String(meeting.catalog_course_id || "");
  return (store.courses || []).find((item) => String(item.course_id || "") === courseId);
}

function openMeetingCourse(store, meeting) {
  const course = courseForMeeting(store, meeting);
  if (!course) return false;
  store.set("activeCourse", course);
  const vis = document.querySelector(".page:not([hidden])");
  const pane = vis ? vis.querySelector(".lecture-pane") : null;
  pane?.scrollIntoView({ block: "start" });
  return true;
}

function renderConflicts(days) {
  const target = $("schedule-conflicts");
  if (!target) return;
  clear(target);
  const notes = [];
  const byGroup = new Map();
  days.forEach((day) => {
    (day.meetings || []).forEach((meeting) => {
      if (!meeting.conflict_group) return;
      const list = byGroup.get(meeting.conflict_group) || [];
      list.push({ ...meeting, date: day.date });
      byGroup.set(meeting.conflict_group, list);
    });
  });
  byGroup.forEach((group) => {
    if (group.length < 2) return;
    const label = group.map((m) => `${m.title} ${m.start_time}–${m.end_time}`).join(" 与 ");
    const note = document.createElement("p");
    note.className = "conflict-note";
    note.append(textElement("span", `时间冲突：${label}`, ""));
    target.append(note);
  });
}

/* 周课表网格：时间轴 + 周一至周日七列，按 start_unit/end_unit 绝对定位；冲突分组用并列泳道 */
function weekBlock(store, block) {
  const meeting = block.meeting;
  const course = courseForMeeting(store, meeting);
  const container = document.createElement(course ? "button" : "div");
  const tags = [];
  if (block.isNow) tags.push("now");
  else if (block.isNext) tags.push("next");
  if (block.past) tags.push("past");
  if (meeting.conflict_group) tags.push("conflict");
  container.className = `week-block ${tags.join(" ") || "plain"}`.trim();
  const slotTop = (block.startUnit - 1) * WEEK_SLOT_HEIGHT;
  const slotHeight = (block.endUnit - block.startUnit + 1) * WEEK_SLOT_HEIGHT;
  const laneLeft = (block.lane / block.laneCount) * 100;
  const laneWidth = (1 / block.laneCount) * 100;
  container.setAttribute("style", [
    `top: ${slotTop + 2}px`,
    `height: ${slotHeight - 4}px`,
    `left: calc(${laneLeft.toFixed(2)}% + 2px)`,
    `width: calc(${laneWidth.toFixed(2)}% - 4px)`,
  ].join("; "));
  const label = [`周${WEEKDAY_NAMES[block.dayWeekday - 1]?.slice(1) || ""} ${meeting.start_time}–${meeting.end_time}`, meeting.title, meeting.room]
    .filter(Boolean).join("，");
  container.setAttribute("aria-label", label);
  /* 60s tick 整树重建周网格时的焦点恢复身份：星期+课程+开始时刻在网格内唯一标识一节课 */
  container.setAttribute("data-wb-key", [
    block.dayWeekday, meeting.catalog_course_id || meeting.title || "", meeting.start_time || "",
  ].join("|"));
  container.append(
    textElement("span", meetingTimeRange(meeting), "wb-time"),
    textElement("strong", meeting.title || "课程", "wb-name"),
    textElement("span", meeting.room || "", "wb-room"),
  );
  let tag = null;
  if (meeting.conflict_group) tag = textElement("span", "冲突", "wb-tag conflict");
  else if (block.isNow) tag = textElement("span", "进行中", "wb-tag now");
  else if (block.isNext) tag = textElement("span", "下一节", "wb-tag next");
  else if (block.past) tag = textElement("span", "已结束", "wb-tag done");
  if (tag) container.append(tag);
  if (course) {
    container.type = "button";
    container.setAttribute("title", `打开课程：${course.title || course.course_id}`);
    container.addEventListener("click", () => {
      const dialog = $("schedule-week-dialog");
      dialog?.open && dialog.close();
      openMeetingCourse(store, meeting);
    });
  }
  return container;
}

/* 周网格由 60s tick 整树重建：焦点在网格内时按 data 身份原位恢复，
   绝不把键盘/读屏用户抛回 body（tasks-drawer capture/restoreTaskFocus 先例） */
function captureWeekGridFocus() {
  const grid = $("week-grid");
  const active = document.activeElement;
  if (!grid || !active || !grid.contains(active)) return null;
  const key = String(active.getAttribute("data-wb-key") || "");
  return key ? { key } : null;
}

function restoreWeekGridFocus(capture) {
  if (!capture?.key) return;
  const grid = $("week-grid");
  if (!grid) return;
  for (const node of grid.querySelectorAll("[data-wb-key]")) {
    if (node.getAttribute("data-wb-key") !== capture.key) continue;
    node.focus({ preventScroll: true });
    return;
  }
}

function renderWeekGrid(store, value) {
  const target = $("week-grid");
  if (!target) return;
  clear(target);
  const model = weekGridModel(value, new Date());
  if (!model.days.length) return;
  target.append(textElement("span", "", "week-corner"));
  model.days.forEach((day) => {
    const head = document.createElement("span");
    head.className = "week-day-head" + (day.isToday ? " today" : "");
    head.append(
      textElement("strong", day.name, "wd-name"),
      textElement("span", day.date.slice(5).replace("-", "/"), "wd-date"),
    );
    if (day.isToday) head.append(textElement("span", "今天", "wd-today-pill"));
    target.append(head);
  });
  const axis = document.createElement("span");
  axis.className = "week-axis";
  model.slots.forEach((slot) => axis.append(textElement("span", String(slot.start || ""), "week-slot-label")));
  target.append(axis);
  const canvas = document.createElement("div");
  canvas.className = "week-canvas";
  model.days.forEach((day) => {
    const col = document.createElement("div");
    col.className = "week-day" + (day.isToday ? " today" : "");
    const lines = document.createElement("span");
    lines.className = "week-day-lines";
    lines.setAttribute("aria-hidden", "true");
    model.slots.forEach((slot, index) => {
      const line = document.createElement("span");
      line.className = "week-slot-line";
      line.setAttribute("style", `top: ${(index + 1) * WEEK_SLOT_HEIGHT - 1}px`);
      lines.append(line);
    });
    col.append(lines);
    day.blocks.forEach((block) => col.append(weekBlock(store, { ...block, dayWeekday: day.weekday })));
    canvas.append(col);
  });
  target.append(canvas);
}

/* ⑬b：恢复期诚实标注——检查点恢复在途时先显示上次快照，恢复完成即换新 */
let restoreHint = false;

function renderScheduleWidget(value) {
  const model = value ? selectNowNext(value, new Date()) : null;
  renderScheduleNow($("schedule-now"), model);
  const target = $("schedule-now");
  if (target) {
    target.querySelectorAll(".restore-hint").forEach((node) => node.remove());
    if (restoreHint) {
      const hint = document.createElement("p");
      hint.className = "hint restore-hint";
      hint.setAttribute("role", "status");
      const generatedAt = Number(value?.generated_at || 0);
      hint.textContent = generatedAt > 0
        ? `数据为上次会话快照 · ${formatTime(generatedAt)}；会话恢复后自动更新`
        : "正在恢复会话：先显示上次课表快照，恢复完成后自动更新";
      target.append(hint);
    }
  }
  const widget = $("schedule-widget");
  if (widget) widget.setAttribute("aria-label", scheduleWidgetAria(model));
}

function renderSnapshot(store, value) {
  browsedSnapshot = value;
  /* 非 stale 快照说明缓存已恢复新鲜：自动刷新资格复位，进入新的 stale episode */
  if (String(value.code || "") !== "timetable_stale") staleEpisodeAutoRefreshed = false;
  selectedWeek = Number(value.selected_week || value.current_week || 1);
  selectedSemester = String(value.selected_semester?.semester_id || "");
  const currentWeek = Number(value.current_week || 0);
  /* selected_week 缺省视为服务端默认视图，即真实本周 */
  const isCurrentWeek = !currentWeek || selectedWeek === currentWeek;

  /* 浮层内容（周标签/学期控件/网格/冲突）始终跟随浏览周 */
  const weekTag = $("schedule-week-tag");
  if (weekTag) {
    weekTag.textContent = `第 ${selectedWeek} 周 · ${friendlyDateRange(value.week_start, value.week_end)}`;
  }

  const semester = $("timetable-semester");
  if (semester) {
    const signature = JSON.stringify((value.semesters || []).map((item) => item.semester_id));
    if (semester.dataset.signature !== signature) {
      clear(semester);
      (value.semesters || []).forEach((item) => semester.append(new Option(item.label || item.semester_id, item.semester_id)));
      semester.dataset.signature = signature;
    }
    semester.value = selectedSemester;
    /* WP1-D1：程序化设值不派发 change——周课表学期触发钮要显式同步文案。 */
    syncDropdown(semester);
  }
  const startDate = $("timetable-start-date");
  if (startDate) startDate.value = value.selected_semester?.start_date || "";
  const exportLink = $("export-timetable");
  if (exportLink) {
    exportLink.href = `/api/v3/timetable/export.ics?semester_id=${encodeURIComponent(selectedSemester)}`;
    exportLink.dataset.exportReady = "1";  // N5FE-P8：真实课表就绪才放行导出
  }

  renderWeekGrid(store, value);
  renderConflicts(value.days || []);

  if (!isCurrentWeek) return;
  /* 主页“今日与本周安排”、Now/Next 与对外广播只消费真实本周快照：浏览周不得改写主页真值 */
  homeSnapshot = value;
  const summary = $("schedule-summary");
  if (summary) {
    const todayCount = (value.days || []).find((day) => day.date === todayIso());
    const meetings = todayCount?.meetings || [];
    const next = value.next_meeting;
    summary.textContent = `今天 ${meetings.length} 节 · 下一节 ${
      next ? `${String(next.start_time).slice(0, 5)} ${next.title}` : "无"
    } · 第 ${selectedWeek} 周`;
  }
  renderScheduleWidget(value);
  window.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: value }));
}

function renderState(value, store) {
  const targets = [$("schedule-state"), $("schedule-widget-state")].filter(Boolean);
  if (!targets.length) return;
  const code = String(value?.code || "");
  const openLogin = () => {
    window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "login" }));
  };
  const actionButton = (label, onClick) => {
    targets.forEach((target) => {
      const button = textElement("button", label, "btn-quiet");
      button.addEventListener("click", onClick);
      target.append(button);
    });
  };
  const retryRefresh = () => void refresh(store);
  const backToCurrentWeek = () => {
    selectedWeek = currentWeekNumber();
    void load(store);
  };
  const states = {
    timetable_login_required: () => {
      targets.forEach((target) => { target.textContent = "登录复旦课程平台后显示今日与本周安排。"; });
      actionButton("去登录", openLogin);
    },
    timetable_session_expired: () => {
      targets.forEach((target) => { target.textContent = "课程平台的登录会话已过期，请重新登录后查看课表。"; });
      actionButton("去登录", openLogin);
    },
    timetable_upstream_unavailable: () => {
      targets.forEach((target) => { target.textContent = "课表来源暂时不可用，暂无可显示的课表，可稍后重试刷新。"; });
      actionButton("重试", retryRefresh);
    },
    timetable_payload_invalid: () => {
      targets.forEach((target) => { target.textContent = "课表来源返回的数据无法确认，请重试刷新。"; });
      actionButton("重试", retryRefresh);
    },
    timetable_action_invalid: () => {
      targets.forEach((target) => { target.textContent = "课表请求未被接受，请调整周次后重试。"; });
      actionButton("回到本周", backToCurrentWeek);
    },
    timetable_week_invalid: () => {
      targets.forEach((target) => { target.textContent = "请求的周次未被接受，请调整周次后重试。"; });
      actionButton("回到本周", backToCurrentWeek);
    },
    timetable_not_loaded: () => {
      /* partial_failures 存在说明刚发生的刷新失败：如实给出来源与下一步，不伪称空态是正常周 */
      const names = partialFailureNames();
      const message = names
        ? `课表来源暂时不可用（${names}），暂无可显示的课表，可稍后重试刷新。`
        : "本周没有可显示的课表，可刷新读取。";
      targets.forEach((target) => { target.textContent = message; });
      actionButton("刷新", () => void refresh(store));
    },
    semester_start_required: () => {
      targets.forEach((target) => { target.textContent = "设置学期起始日后才能计算周次。"; });
      actionButton("去课表设置", () => {
        const dialog = $("schedule-week-dialog");
        if (dialog && !dialog.open) openScheduleDialog(store);
        const details = document.querySelector("#schedule-week-dialog .tt-settings");
        if (details) details.open = true;
        $("timetable-start-date")?.focus();
      });
    },
    timetable_stale: () => {
      targets.forEach((target) => { target.textContent = "正在显示上次同步的课表（缓存已过期），可刷新。"; });
      actionButton("刷新", retryRefresh);
    },
    timetable_partial: () => {
      /* partial 只出现在刷新响应里（数据是刚抓取的结果）：被动披露失败来源与已显示范围，
         不催促刷新（SIMPLIFY-AUDIT-1 S2 后也无头部手动钮）；恢复靠下一次 stale
         episode 自动链或状态行内联动作钮。 */
      const names = partialFailureNames();
      const message = names
        ? `部分课表来源暂不可用（${names}），已显示已确认的课程。`
        : "部分课表来源暂不可用，正在显示已确认的课程。";
      targets.forEach((target) => { target.textContent = message; });
    },
    timetable_verified: () => { targets.forEach((target) => clear(target)); },
    empty: () => {
      targets.forEach((target) => { target.textContent = "本周没有安排的课程。"; });
    },
  };
  (states[code] || (() => { targets.forEach((target) => { target.textContent = "课表暂时无法读取，可稍后重试。"; }); }))();
}

async function load(store) {
  if (!authReady(store)) {
    staleEpisodeAutoRefreshed = false;
    renderState({ code: "timetable_login_required" }, store);
    renderScheduleWidget(null);
    const summary = $("schedule-summary");
    if (summary) summary.textContent = "登录后显示今日与本周安排";
    return;
  }
  const local = ++seq;
  controller?.abort();
  controller = new AbortController();
  const summary = $("schedule-summary");
  /* 加载中文案属于主页本周真值：浏览非本周时不占用主页摘要，反馈由浮层周标签/网格承载 */
  const browsingOtherWeek = Boolean(selectedWeek) && selectedWeek !== currentWeekNumber();
  if (summary && !browsingOtherWeek) summary.textContent = "正在读取课表…";
  const query = new URLSearchParams();
  if (selectedSemester) query.set("semester_id", selectedSemester);
  if (selectedWeek) query.set("week", String(selectedWeek));
  try {
    const value = await apiV3(`timetable?${query}`, { controller });
    if (local !== seq) return;
    renderSnapshot(store, value);
    /* 本周加载遇到缓存过期：一个 episode 自动触发一次后台刷新（浏览非本周、episode 已消费、
       已有刷新在途时不触发）。在途只给被动文案；结果由 refresh() 照常渲染——失败或仍 stale
       时回落到下方手动刷新横幅，绝不自动重试。 */
    if (
      String(value.code || "") === "timetable_stale"
      && !browsingOtherWeek && !staleEpisodeAutoRefreshed && !refreshInFlight
    ) {
      staleEpisodeAutoRefreshed = true;
      [$("schedule-state"), $("schedule-widget-state")].filter(Boolean)
        .forEach((target) => { target.textContent = "课表缓存已过期，正在自动更新…"; });
      void refresh(store);
      return;
    }
    renderState(value, store);
  } catch (error) {
    if (error.name === "AbortError" || local !== seq) return;
    renderState({ code: error.code || "fetch_failed" }, store);
  }
}

async function refresh(store) {
  if (!authReady(store)) return;
  const summary = $("schedule-summary");
  const refreshingText = "正在刷新课表…";
  const previousSummary = summary ? summary.textContent : "";
  /* 刷新是阻塞式上游抓取（最坏串行多个 30s 请求）：在途必须可见；
     SIMPLIFY-AUDIT-1 S2 后无头部手动钮，refreshInFlight 守卫防重入
     （触发源=状态行内联动作钮与 stale episode 自动链）。 */
  refreshInFlight = true;
  if (summary) summary.textContent = refreshingText;
  /* 代际守卫：POST 只携带学期，响应语义固定是发起时学期的服务端默认视图；
     在途翻周/换学期后，迟到响应不得改写浏览面（load 的 seq 只守 load 之间） */
  const requestWeek = selectedWeek;
  const requestSemester = selectedSemester;
  try {
    const value = await postV3("timetable/actions", {
      action: "refresh",
      semester_id: selectedSemester,
      operation_id: operationId("timetable"),
    });
    if (value.timetable) {
      if (selectedWeek !== requestWeek || selectedSemester !== requestSemester) {
        /* 浏览意图已漂移：只消费 episode 状态，渲染交由用户浏览语义的下一次 load */
        if (String(value.timetable.code || "") !== "timetable_stale") staleEpisodeAutoRefreshed = false;
      } else {
        renderSnapshot(store, value.timetable);
        renderState(value.timetable, store);
      }
    } else {
      await load(store);
    }
  } catch (error) {
    renderState({ code: error.code || "fetch_failed" }, store);
    toast(error.message, "error");
  } finally {
    refreshInFlight = false;
    /* 成功路径 renderSnapshot/load 已写回真实摘要；仅在失败路径摘要仍停留在刷新文案时还原 */
    if (summary && summary.textContent === refreshingText) summary.textContent = previousSummary;
  }
}

function openScheduleDialog(store) {
  const dialog = $("schedule-week-dialog");
  const widget = $("schedule-widget");
  if (!dialog || dialog.open) return;
  dialog.showModal();
  widget?.setAttribute("aria-expanded", "true");
  void load(store);
  $("close-schedule-dialog")?.focus();
}

export async function installTimetable(store) {
  /* 元素/窗口监听统一登记，cleanup 成对摘除（D8）：单安装虽无实害，卸载路径必须完整 */
  const detach = [];
  const bind = (target, type, handler) => {
    target.addEventListener(type, handler);
    detach.push(() => target.removeEventListener(type, handler));
  };

  bind($("schedule-week-prev"), "click", () => {
    selectedWeek = Math.max(1, selectedWeek - 1);
    void load(store);
  });
  bind($("schedule-week-next"), "click", () => {
    selectedWeek = Math.min(30, selectedWeek + 1);
    void load(store);
  });
  bind($("schedule-week-current"), "click", () => {
    selectedWeek = currentWeekNumber();
    void load(store);
  });
  /* SIMPLIFY-AUDIT-1 S2：常驻手动「刷新」钮已退役——打开即 load、stale
     episode 自动链与状态行内联动作钮承载全部刷新路径。 */
  bind($("timetable-semester"), "change", (event) => {
    /* 学期切换回到真实本周：不带周次走服务端默认视图，绝不拿旧学期周次请求新学期（W6） */
    selectedSemester = event.target.value;
    selectedWeek = 0;
    void load(store);
  });

  /* 浮层与设置页共用的学期起始日提交；busy 落在触发按钮上，POST 结束即返回 */
  const commitSemesterStart = async (button, startDate) => {
    if (!startDate) return toast("请选择学期起始日", "error");
    if (!authReady(store)) return renderState({ code: "timetable_login_required" }, store);
    setBusy(button, true);
    try {
      const value = await postV3("timetable/actions", {
        action: "set-semester-start",
        semester_id: selectedSemester,
        start_date: startDate,
        operation_id: operationId("timetable"),
      });
      if (value.timetable) {
        renderSnapshot(store, value.timetable);
        renderState(value.timetable, store);
      }
      toast("学期起始日已保存", "ready");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  };
  bind($("set-timetable-start"), "click", () => {
    void commitSemesterStart($("set-timetable-start"), $("timetable-start-date").value);
  });

  /* N5FE-P8：未登录/零课表点导出会把整页导航到裸 JSON 错误页——拦截并给
     一句人话指引；exportReady 标记只在真实课表渲染后由上方写入。 */
  const attachIcsGuard = (link) => {
    if (!link) return;
    const handleGuard = (event) => {
      if (link.dataset.exportReady === "1") return;
      event.preventDefault();
      if (store.auth?.state !== "ready") toast("先登录复旦课程平台，再导出课表日历。", "error");
      else toast("课表还没有可导出的内容。先确认课程目录，再导出。", "error");
    };
    link.addEventListener("click", handleGuard);
    detach.push(() => link.removeEventListener("click", handleGuard));
  };
  attachIcsGuard($("export-timetable"));

  /* SIMPLIFY-AUDIT-1 S1：设置页「课表」组已整组退役——学期/起始日/导出 ICS
     唯一入口=周课表弹窗底部「课表设置」，原 MutationObserver 只读镜像垫片
     （settings-timetable-* 双副本同步）一并清除。 */

  const handleScheduleOpen = () => openScheduleDialog(store);
  bind(window, "courselens:open-schedule", handleScheduleOpen);

  /* 课表小组件常驻学习选择页：点击用原生 dialog 打开周课表；关闭（含 Esc）把焦点还给小组件 */
  bind($("schedule-widget"), "click", () => openScheduleDialog(store));
  bind($("close-schedule-dialog"), "click", () => $("schedule-week-dialog")?.close());
  /* 遮罩点击关闭：click 落点在 dialog 盒外（即 ::backdrop）且按压起点也在盒外才关闭；
     在内容上开始或结束的划选/拖拽不误关。Esc 与关闭按钮走下方 close 事件，焦点归还行为一致。 */
  let backdropPress = false;
  const scheduleDialog = $("schedule-week-dialog");
  const pressIsOnBackdrop = (event) => {
    if (event.target !== scheduleDialog) return false;
    const rect = scheduleDialog.getBoundingClientRect?.();
    if (!rect) return false;
    return event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  bind(scheduleDialog, "mousedown", (event) => { backdropPress = pressIsOnBackdrop(event); });
  bind(scheduleDialog, "click", (event) => {
    const onBackdrop = pressIsOnBackdrop(event);
    if (onBackdrop && backdropPress) scheduleDialog?.close();
    backdropPress = onBackdrop;
  });
  bind(scheduleDialog, "close", () => {
    backdropPress = false;
    $("schedule-widget")?.setAttribute("aria-expanded", "false");
    $("schedule-widget")?.focus({ preventScroll: true });
  });

  /* ready 转变检测：登录完成（非 ready → ready）自动加载；小组件常驻可见，无需先展开盒子；
     同一 ready 快照的后续 auth 更新不重复触发。 */
  let wasAuthReady = authReady(store);
  const unsubscribe = store.subscribe("auth", () => {
    const ready = authReady(store);
    const becameReady = ready && !wasAuthReady;
    wasAuthReady = ready;
    /* ⑬b：恢复期标注随 checking 开合；开/关各只重渲染一次小组件 */
    const restoring = String(store.auth?.state || "") === "checking";
    if (restoring !== restoreHint) {
      restoreHint = restoring;
      renderScheduleWidget(homeSnapshot);
    }
    if (!ready) {
      /* 登出即清空主页本周真值与浏览状态：小组件/摘要不得残留登录会话的课表数据 */
      homeSnapshot = null;
      browsedSnapshot = null;
      selectedWeek = 0;
      staleEpisodeAutoRefreshed = false;
      renderState({ code: "timetable_login_required" }, store);
      renderScheduleWidget(null);
      const summary = $("schedule-summary");
      if (summary) summary.textContent = "登录后显示今日与本周安排";
      return;
    }
    if (becameReady || !homeSnapshot) {
      /* 重新登录/首登回到真实本周：不得沿用登出前的浏览周，否则非本周响应永远到不了主页 */
      selectedWeek = 0;
      void load(store);
    }
  });

  /* 桌面时间推进：每分钟重算 Now/Next 剩余时间与周网格“进行中/已结束”高亮；
     仍按状态分域：主页组件用本周快照，浮层网格用浏览周快照；
     网格整树重建前捕获焦点身份，重建后原位恢复（W7） */
  const widgetTimer = window.setInterval(() => {
    if (homeSnapshot) renderScheduleWidget(homeSnapshot);
    if (browsedSnapshot) {
      const focus = captureWeekGridFocus();
      renderWeekGrid(store, browsedSnapshot);
      restoreWeekGridFocus(focus);
    }
  }, 60000);

  if (authReady(store)) void load(store);

  return () => {
    controller?.abort();
    window.clearInterval(widgetTimer);
    unsubscribe();
    for (const off of detach) off();
  };
}
