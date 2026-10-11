import { apiV3, postV3 } from "./api.js";
import { $, clear, closeOverlay, formatRelativeTime, formatTime, operationId, textElement } from "./ui.js";
import { selectPage } from "./shell.js";

/* 课程数据管理工作区（数据管理页）：消费冻结合同 §2 的
   courselens.course-data-summary.v1 / …lecture-page.v1 / …action-result.v1。
   - 入口仅账户菜单（E2）；页面经 selectPage 复用 [data-page] 机制；
   - 只读聚合呈现 + 五个闭集动作，凭据/会话/令牌永不出现在本页；
   - 详情面板纯只读（标题+标签+类别栅格+讲次表）；唯一操作面 = 批量条，
     按用户意图三组（安全→释放空间→不可逆），每组动作配大白话 title；
   - Q1 即时删除+强确认（typed dialog 逐课程名精确匹配，引擎恰 1 课程）；
     Q2 孤儿默认保留可见、清除全部孤儿=typed 确认+清单先展示+自动化规则警示；
     Q3 AI 件=派生档；Q4 导出=纯 JSON 清单；Q5 PPT/OCR 恒称「依赖远端」；
   - 阻塞（活动任务/远程 run/待导入/自动化规则/待清理）整批拒绝，闭集码呈现；
   - stale 不自动刷新：证据行显示统计生成时间，刷新按钮显式。 */

const SUMMARY_SCHEMA = "courselens.course-data-summary.v1";
const LECTURE_PAGE_SCHEMA = "courselens.course-data-lecture-page.v1";
const ACTION_RESULT_SCHEMA = "courselens.course-data-action-result.v1";

const SUMMARY_PAGE_SIZE = 200;
const SUMMARY_MAX_PAGES = 5;
const LECTURE_PAGE_LIMIT = 50;

/* 类别闭集（B 冻结序）：徽标顺序固定，缺类别省略不占位。
   export 供 settings/data-map.js（DATAMAP-P1）同源复用，禁两处漂移。 */
export const CATEGORY_LABELS = Object.freeze({
  progress: "进度", transcript: "字幕", ppt: "课件", artifacts: "笔记",
  documents: "文档", references: "引用", timeline: "时间轴", search: "索引",
  bookmarks: "书签", quizzes: "测验", review: "复习", tasks: "任务",
  automation: "自动化",
});
const CATEGORY_ORDER = Object.freeze(Object.keys(CATEGORY_LABELS));

/* 可重建三档（Q3/Q5 冻结口径）：依赖远端 = PPT/OCR 恒用词；可重建 = 派生可再生；
   不可重建 = 用户记录/本地原件 */
const CATEGORY_TIERS = Object.freeze({
  search: "rebuildable", transcript: "rebuildable", artifacts: "rebuildable",
  timeline: "rebuildable", ppt: "remote", documents: "irreversible",
  references: "irreversible", progress: "irreversible", bookmarks: "irreversible",
  quizzes: "irreversible", review: "irreversible", tasks: "irreversible",
  automation: "irreversible",
});
const TIER_LABELS = Object.freeze({ rebuildable: "可重建", remote: "依赖远端", irreversible: "不可重建" });

const ACTION_LABELS = Object.freeze({
  "rebuild-search": "重建索引", "purge-derived": "清理派生数据",
  "remove-copies": "移除本地副本", "delete-records": "删除记录",
  export: "导出", "clear-orphans": "清除全部孤儿",
  "release-stuck": "解锁卡住的记录", "export-study-stats": "导出学习统计",
});

const BLOCKER_TEXT = Object.freeze({
  active_task: "有正在进行的生成任务",
  active_remote_run: "有远程计算进行中",
  active_automation_import: "有等待导入的自动化结果",
  automation_rule: "有覆盖所选课程的自动化规则",
  cleanup_pending: "有待完成的安全清理",
});

/* B4 文案闭集（workbench 钉死；无感叹号、无营销形容、绝不渲染原始异常） */
const DATA_TEXT = Object.freeze({
  loading: "正在统计数据…",
  empty: "暂无课程数据",
  detailEmpty: "选择左侧课程查看数据明细",
  stalePrefix: "统计生成于 ",
  notCounted: "未统计",
  noFileSize: "—",
  registered: "在册",
  orphan: "孤儿",
  /* #5（走查双词并存打磨）：摘要条「孤立文件」与过滤芯片/危险区「孤儿」同族
     收敛为「孤儿文件」——孤儿=无课程归属的残留这一学生概念一个词族，「文件」
     后缀区分文件目录计数（此处）与课程计数（芯片「孤儿 N」）。 */
  orphansFilesPrefix: "孤儿文件 ",
  selectedPrefix: "已选 ",
  selectedSuffix: " 门",
  clearSelection: "清空选择",
  prevPage: "上一页",
  nextPage: "下一页",
  pagerPrefix: "第 ",
  pagerMid: " / ",
  pagerSuffix: " 页",
  actionDoneSuffix: "」已完成",
  actionRejectedSuffix: "」未执行",
  actionFailure: "操作未完成，请稍后重试。",
  confirmHeaderPrefix: "将删除所选 ",
  confirmHeaderMid: " 项，共 ",
  confirmHeaderSuffix: "，删除后不可恢复。",
  confirmPromptPrefix: "输入课程名「",
  /* AVATAR-POLISH-1 OBS-8（化身走查 OBS-8）：孤儿课程无 title，确认期望值退回
     课程编号——提示语按行内实情二选一，文案不再与门禁错位（W3 逐字精确匹配
     语义零改动，names[0] 仍是唯一期望值）。 */
  confirmPromptIdPrefix: "输入课程编号「",
  confirmInputLabelNamed: "输入课程名确认",
  confirmInputLabelId: "输入课程编号确认",
  confirmInputLabelAny: "输入任意文字确认",
  confirmPromptSuffix: "」确认：",
  removeCopiesNote: "移除本地副本后，原件需重新导入。",
  remoteNote: "依赖远端的课件识别内容：远端图片可能已过期，之后可能无法重建。",
  orphanAutomationNote: "自动化规则可能在之后重新生成同类数据。",
  textVolumeSuffix: " 字符",
  loginHint: "登录后显示当前账号授权的课程。",
  bulkHint: "先勾选课程，再选下方操作",
  /* POLISH-1 F9（化身走查 F9）：「大小 — · 更新 未统计」此前无解释，学生
     疑为故障。悬浮 title 逐行给原因；可见的一句完整口径放明细面板。 */
  sizeUntalliedTip: "大小「未统计」= 本次统计还没算到生成文件体积；出现导出文档、课件等文件后会自动补上。",
  sizeNoFileTip: "大小「—」= 没有可报告的生成文件；进度、字幕文本等都在本机存着，不占文件体积。",
  updateNeverTip: "更新「未统计」= 这门课还没有数据写入记录，学起来之后就会显示。",
  detailColumnHint: "「未统计」= 暂无该项数据，不是故障；「大小」统计的是生成文件（导出文档、课件等），进度、字幕文本等都在本机存着，不占文件体积。",
  /* POLISH-1 F11（化身走查 F11）：孤儿名单此前是裸 span 序列，视觉上与上句
     「…重新生成同类数据。」粘连成「同类数据。9000」。补引导句并逐行点名。 */
  /* POLISH-1 F8b（化身走查 F8b）：热点核对面——「没听懂」标记计数随行/明细
     上屏（后端可选字段 not_understood_count，存在则上屏）；学生点完「没听懂」
     能在数据页对上账。 */
  notUnderstoodPrefix: "没听懂 ",
  notUnderstoodDetailSuffix: " 处「没听懂」标记，记在书签里，可在播放器时间轴跳回",
  /* AVATAR-POLISH-1 C5（RELREADY-AUDIT C5）：数据页危险区并无「孤儿」释义
     （筛选片/批量钮均为裸词），确认清单改用学生能直接读懂的「残留数据」；
     批量钮既有闭集词不动。 */
  orphanListLead: "将清除以下课程留下的残留数据：",
  /* AVATAR-POLISH-1 UP-G1（化身走查 S1 升级观察）：页头「数据 X」的逐课去向
     ——明细随行给「本课文字存量约」（后端可选字段 stored_text_bytes，存在则
     上屏，同 F8b 模式）。与页头总数（sqlite 文件字节，含索引/空闲页）不同基，
     表述用「约」，不声称逐字对账。 */
  storedBytesPrefix: "本课文字存量约 ",
  storedBytesSuffix: "（进度、字幕文本等，按存进学习库的内容计）",
});

/* 批量条 per-button 大白话说明（title 呈现；闭集冻结，无感叹号、不渲染原始异常）。
   Q5：课件识别内容属依赖远端档，绝不写成单纯「可重建」。 */
const ACTION_HINTS = Object.freeze({
  "rebuild-search": "重建搜索索引，不改动课程数据。",
  export: "导出全库数据清单（JSON），只读操作。",
  "export-study-stats": "把学习时长、进度和掌握度导出成 JSON 和两个 CSV 表格，只读操作。",
  "purge-derived": "删除可重新生成的字幕文本、笔记与 AI 摘要；课件识别内容依赖远端。",
  "remove-copies": "删除文档、字幕、课件等本地文件，原件需重新导入或重新生成。",
  "delete-records": "永久删除观看进度、书签、测验作答，删除后不可恢复。",
  "release-stuck": "把一直「进行中/暂停中」的记录和卡住的清理强制收尾；不删除任何学习记录。",
});
const DELETE_RECORDS_SINGLE_HINT = "删除记录一次仅支持一门课程，请单独勾选。";

let summaryValue = null;
let summaryLoading = false;
let actionBusy = false;
let selectedCourseIds = new Set();
let selectedCourseId = "";
let orphansOnly = false;
let lectureOffset = 0;
let pendingConfirm = null;
/* AVATAR-POLISH-1：确认提示语 span 每次开窗重建——持当前引用而非按 class 重查，
   免得旧窗残span 被误中（假 DOM registry 与真实 DOM 同样受益） */
let confirmPromptNode = null;
let mediaNarrow = null;
const narrowDetailBodies = new Map();

const courseRows = () => (Array.isArray(summaryValue?.rows) ? summaryValue.rows : []);

const wideLayout = () => Boolean(mediaNarrow && !mediaNarrow.matches);

function formatDataBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes || bytes <= 0) return "0 B";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.ceil(bytes / 1024))} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

/* SWEEPFIX-1 S1（化身走查 SWEEP1-S1）：每课/每讲「大小」面诚实化。该面统计
   的是生成文件体积（导出文档/课件等）；纯数据库行（进度、字幕文本、书签）
   没有生成文件，此前一律显示「0 B」，与页头按数据库字节给出的总数
   （「数据 2.4 MiB」）直接矛盾，学生无法回答「我的数据在哪」。
   诚实修（用户令：无统计的面不显示假 0 B）：值为 0 =「没有可报告的生成
   文件」，显示「—」留白；null = 后端未发布统计，维持既有「未统计」；
   真有文件时照常人话单位。页头与删除确认头的总体积是真实合计，不动。 */
function fileSizeDisplay(value) {
  if (value == null) return DATA_TEXT.notCounted;
  return Number(value) > 0 ? formatDataBytes(value) : DATA_TEXT.noFileSize;
}

function formatTextVolume(value) {
  const count = Number(value || 0);
  if (!count || count <= 0) return "";
  if (count < 10000) return `${count}${DATA_TEXT.textVolumeSuffix}`;
  return `${(count / 10000).toFixed(1)} 万${DATA_TEXT.textVolumeSuffix}`;
}

function rowUpdatedAt(row) {
  let latest = 0;
  for (const value of Object.values(row.categories || {})) {
    const at = Number(value?.last_updated_at || 0);
    if (at > latest) latest = at;
  }
  return latest;
}

const hasOrphanRows = () => courseRows().some((row) => row.in_catalog === false);

function filteredRows() {
  const query = String($("data-search")?.value || "").trim().toLowerCase();
  const category = String($("data-category-filter")?.value || "");
  return courseRows().filter((row) => {
    if (orphansOnly && row.in_catalog !== false) return false;
    if (category && !Object.prototype.hasOwnProperty.call(row.categories || {}, category)) return false;
    if (query) {
      const haystack = `${row.title || ""} ${row.teacher || ""} ${row.course_id || ""}`.toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
}

/* ---- 摘要条 + 证据行 ---- */

function renderEvidence() {
  const evidence = $("data-evidence");
  if (!evidence) return;
  const generatedAt = Number(summaryValue?.generated_at || 0);
  evidence.textContent = summaryValue
    ? `${DATA_TEXT.stalePrefix}${formatTime(generatedAt)}`
    : "尚未统计";
}

function renderSummaryStrip() {
  const strip = $("data-summary");
  if (!strip) return;
  strip.setAttribute("aria-busy", summaryLoading ? "true" : "false");
  if (!summaryValue) {
    strip.textContent = summaryLoading ? DATA_TEXT.loading : DATA_TEXT.empty;
    renderEvidence();
    renderOrphansFilter();
    return;
  }
  const rows = courseRows();
  const catalogRows = rows.filter((row) => row.in_catalog !== false);
  const lectures = catalogRows.reduce((sum, row) => sum + Number(row.lecture_count || 0), 0);
  const orphanFiles = Number(summaryValue.orphan_artifacts?.directories || 0);
  const parts = [
    `课程 ${catalogRows.length}`,
    `讲次 ${lectures}`,
    `数据 ${formatDataBytes(Number(summaryValue.database_bytes?.total || 0))}`,
  ];
  if (orphanFiles > 0) parts.push(`${DATA_TEXT.orphansFilesPrefix}${orphanFiles}`);
  strip.textContent = rows.length === 0 ? DATA_TEXT.empty : parts.join(" · ");
  renderEvidence();
  renderOrphansFilter();
}

function renderOrphansFilter() {
  const orphanFilter = $("data-orphans-filter");
  if (!orphanFilter) return;
  const orphans = courseRows().filter((row) => row.in_catalog === false).length;
  orphanFilter.hidden = orphans === 0;
  orphanFilter.textContent = `${DATA_TEXT.orphan} ${orphans}`;
  orphanFilter.setAttribute("aria-pressed", orphansOnly ? "true" : "false");
}

/* ---- 课程列表（桌面行壳 + 窄窗 details 行内展开，明细共用同一生成器） ---- */

function categoryChips(row) {
  const chips = [];
  for (const key of CATEGORY_ORDER) {
    const value = row.categories?.[key];
    if (!value) continue;
    chips.push(textElement("span", `${CATEGORY_LABELS[key]} ${Number(value.count || 0)}`, "t-chip data-chip"));
  }
  return chips;
}

function buildRowMeta(row) {
  const meta = textElement("span", "", "data-row-meta");
  /* A8：列表行改相对时间降噪（复用 ui.js 共享通道）；悬浮 title 保留绝对时间备查 */
  const updated = rowUpdatedAt(row);
  const notUnderstood = Number(row.not_understood_count || 0);
  const facts = textElement(
    "span",
    `大小 ${fileSizeDisplay(row.total_file_bytes)}`
    + ` · 更新 ${updated ? formatRelativeTime(updated) : DATA_TEXT.notCounted}`
    + (notUnderstood > 0 ? ` · ${DATA_TEXT.notUnderstoodPrefix}${notUnderstood}` : ""),
  );
  /* POLISH-1 F9：悬浮说明按行状态给原因（SWEEPFIX-1 S1 口径：0=无生成文件、
     null=后端未发布统计）；「— / 未统计」不再是无解释的黑箱。 */
  const tips = [];
  if (row.total_file_bytes == null) tips.push(DATA_TEXT.sizeUntalliedTip);
  else if (Number(row.total_file_bytes) <= 0) tips.push(DATA_TEXT.sizeNoFileTip);
  if (!updated) tips.push(DATA_TEXT.updateNeverTip);
  if (updated) tips.push(`更新于 ${formatTime(updated)}`);
  facts.title = tips.join("\n");
  meta.append(facts);
  meta.append(textElement(
    "span",
    row.in_catalog === false ? DATA_TEXT.orphan : DATA_TEXT.registered,
    row.in_catalog === false ? "data-tag data-tag-orphan" : "data-tag",
  ));
  return meta;
}

function buildRowMain(row) {
  const main = textElement("span", "", "data-row-main");
  main.append(textElement("strong", row.title || row.course_id, "data-row-title"));
  const chipsRow = textElement("span", "", "data-row-chips");
  categoryChips(row).forEach((chip) => chipsRow.append(chip));
  main.append(chipsRow);
  main.append(buildRowMeta(row));
  return main;
}

function appendCheckbox(row, into) {
  const pick = textElement("label", "", "data-pick check");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.dataset.coursePick = row.course_id;
  checkbox.checked = selectedCourseIds.has(row.course_id);
  checkbox.setAttribute("aria-label", `选择 ${row.title || row.course_id}`);
  checkbox.addEventListener("click", (event) => event.stopPropagation());
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) selectedCourseIds.add(row.course_id);
    else selectedCourseIds.delete(row.course_id);
    renderSelection();
  });
  pick.append(checkbox);
  into.append(pick);
}

/* A6：宽布局右半屏（data-detail）未选中时的空态；有课可挑时给出引导，
   零课程时沿用列表侧「暂无课程数据」文案，避免误导学生去选不存在的课。
   UIAUDIT-1 F13：空态补一句人话引导，右半屏不再只剩一行孤标题晾着。 */
function renderDetailEmptyState(hasRows) {
  const detail = $("data-detail");
  if (!detail) return;
  clear(detail);
  const empty = textElement("div", "", "empty-state");
  empty.append(textElement("p", hasRows ? DATA_TEXT.detailEmpty : DATA_TEXT.empty, "empty-title"));
  if (hasRows) {
    /* SWEEPFIX-2 S2（化身走查 SWEEP1-S2）：指引与真实交互对齐——明细入口是
       「点课名行」，勾选框弹的是批量操作条；旧文案「挑一门课」指向不明，
       学生按指引先去勾选、得不到承诺的明细。 */
    empty.append(textElement("p", "每门课的统计和清理都分开算；点左侧课名，这里就显示它的数据明细；勾选方框是批量操作。", "hint"));
  }
  detail.append(empty);
}

function renderCourseList() {
  const list = $("data-course-list");
  if (!list) return;
  list.setAttribute("aria-busy", summaryLoading ? "true" : "false");
  clear(list);
  narrowDetailBodies.clear();
  const rows = filteredRows();
  if (wideLayout() && !selectedCourseId) renderDetailEmptyState(rows.length > 0);
  if (rows.length === 0) {
    const empty = textElement("div", "", "empty-state");
    empty.append(textElement("p", DATA_TEXT.empty, "empty-title"));
    list.append(empty);
    renderSelection();
    return;
  }
  const narrow = !wideLayout();
  for (const row of rows) {
    if (narrow) {
      const details = document.createElement("details");
      details.className = "data-course-row data-row-narrow";
      details.dataset.courseDetail = row.course_id;
      const summary = document.createElement("summary");
      summary.className = "data-row-summary";
      appendCheckbox(row, summary);
      summary.append(buildRowMain(row));
      details.append(summary);
      const body = textElement("div", "", "data-row-detail-body");
      details.append(body);
      narrowDetailBodies.set(row.course_id, body);
      details.addEventListener("toggle", () => {
        if (!details.open) return;
        lectureOffset = 0;
        void renderDetailInto(row.course_id, body);
      });
      list.append(details);
    } else {
      const item = textElement("div", "", "data-course-row");
      item.dataset.courseId = row.course_id;
      if (row.course_id === selectedCourseId) item.classList.add("active");
      appendCheckbox(row, item);
      const open = textElement("button", "", "data-row-open");
      open.type = "button";
      open.append(buildRowMain(row));
      open.addEventListener("click", () => {
        selectedCourseId = row.course_id;
        lectureOffset = 0;
        renderCourseList();
        void renderDetailInto(row.course_id, $("data-detail"));
      });
      item.append(open);
      list.append(item);
    }
  }
  renderSelection();
}

/* ---- 明细：类别栅格 + 讲次表（≤50/页，上一页/下一页） ---- */

async function fetchLecturePage(courseId, offset) {
  const payload = await apiV3(
    `course-data/lectures?course_id=${encodeURIComponent(courseId)}&limit=${LECTURE_PAGE_LIMIT}&offset=${offset}`,
  );
  return payload?.schema === LECTURE_PAGE_SCHEMA ? payload : null;
}

function buildCategoryGrid(row) {
  const grid = textElement("div", "", "data-category-grid");
  for (const key of CATEGORY_ORDER) {
    const value = row.categories?.[key];
    if (!value) continue;
    const line = textElement("div", "", "data-category-line");
    line.append(textElement("span", CATEGORY_LABELS[key], "data-category-name"));
    line.append(textElement("span", `${Number(value.count || 0)}`, "data-category-count"));
    line.append(textElement(
      "span",
      formatTextVolume(value.text_bytes) || DATA_TEXT.notCounted,
      "data-category-volume",
    ));
    line.append(textElement(
      "span",
      Number(value.last_updated_at || 0) ? formatTime(value.last_updated_at) : DATA_TEXT.notCounted,
      "data-category-updated",
    ));
    const tier = CATEGORY_TIERS[key] || "irreversible";
    line.append(textElement("span", TIER_LABELS[tier], `data-tier data-tier-${tier}`));
    grid.append(line);
  }
  return grid;
}

function buildLectureTable(page, courseId) {
  const wrap = textElement("div", "", "data-lecture-table");
  const total = Number(page?.total || 0);
  const lectures = Array.isArray(page?.lectures) ? page.lectures : [];
  for (const lecture of lectures) {
    const line = textElement("div", "", "data-lecture-line");
    line.append(textElement("span", lecture.title || lecture.sub_id, "data-lecture-title"));
    line.append(textElement(
      "span",
      `${lecture.date ? `${lecture.date} · ` : ""}${fileSizeDisplay(lecture.total_file_bytes)}`,
      "data-lecture-size",
    ));
    line.append(textElement(
      "span",
      lecture.in_catalog === false ? DATA_TEXT.orphan : DATA_TEXT.registered,
      lecture.in_catalog === false ? "data-tag data-tag-orphan" : "data-tag",
    ));
    wrap.append(line);
  }
  const pageCount = Math.max(1, Math.ceil(total / LECTURE_PAGE_LIMIT));
  const pageIndex = Math.floor(lectureOffset / LECTURE_PAGE_LIMIT) + 1;
  const pager = textElement("div", "", "data-lecture-pager");
  const prev = textElement("button", DATA_TEXT.prevPage, "btn-quiet");
  prev.type = "button";
  prev.disabled = lectureOffset <= 0;
  prev.addEventListener("click", () => {
    lectureOffset = Math.max(0, lectureOffset - LECTURE_PAGE_LIMIT);
    void renderDetailInto(courseId, detailTargetFor(courseId));
  });
  const next = textElement("button", DATA_TEXT.nextPage, "btn-quiet");
  next.type = "button";
  next.disabled = lectureOffset + LECTURE_PAGE_LIMIT >= total;
  next.addEventListener("click", () => {
    lectureOffset += LECTURE_PAGE_LIMIT;
    void renderDetailInto(courseId, detailTargetFor(courseId));
  });
  pager.append(
    prev,
    textElement(
      "span",
      `${DATA_TEXT.pagerPrefix}${pageIndex}${DATA_TEXT.pagerMid}${pageCount}${DATA_TEXT.pagerSuffix}`,
      "data-lecture-status",
    ),
    next,
  );
  wrap.append(pager);
  return wrap;
}

function detailTargetFor(courseId) {
  if (wideLayout()) return $("data-detail");
  return narrowDetailBodies.get(courseId) || $("data-detail");
}

async function renderDetailInto(courseId, container) {
  if (!container) return;
  clear(container);
  const row = courseRows().find((item) => item.course_id === courseId);
  if (!row) return;
  container.setAttribute("aria-busy", "true");
  const head = textElement("header", "", "data-detail-head");
  head.append(textElement("h3", row.title || courseId, "data-detail-title"));
  head.append(textElement(
    "span",
    row.in_catalog === false ? DATA_TEXT.orphan : DATA_TEXT.registered,
    row.in_catalog === false ? "data-tag data-tag-orphan" : "data-tag",
  ));
  container.append(head);
  /* POLISH-1 F8b：明细头热点计数（存在则上屏）——与书签的从属关系一句话讲清 */
  const notUnderstood = Number(row.not_understood_count || 0);
  if (notUnderstood > 0) {
    container.append(textElement(
      "p",
      `${DATA_TEXT.notUnderstoodPrefix}${notUnderstood}${DATA_TEXT.notUnderstoodDetailSuffix}`,
      "hint",
    ));
  }
  container.append(buildCategoryGrid(row));
  /* AVATAR-POLISH-1 UP-G1：页头「数据 X」的逐课去向（存在则上屏，同 F8b
     可选字段模式）——学生点开课程明细就能对上「这门课占了多少」。 */
  const storedBytes = Number(row.stored_text_bytes || 0);
  if (storedBytes > 0) {
    container.append(textElement(
      "p",
      `${DATA_TEXT.storedBytesPrefix}${formatDataBytes(storedBytes)}${DATA_TEXT.storedBytesSuffix}`,
      "hint",
    ));
  }
  /* POLISH-1 F9：可见的一句完整口径（列表行配悬浮 title，此处给正句）——
     学生不再面对「大小 — · 更新 未统计」猜是不是坏了。 */
  container.append(textElement("p", DATA_TEXT.detailColumnHint, "hint"));
  const lectureWrap = textElement("div", "", "data-lecture-wrap");
  container.append(lectureWrap);
  try {
    let page = await fetchLecturePage(courseId, lectureOffset);
    /* W8：列表收缩后陈旧 offset 可能越过末页——按 page.total 钳制并重取一次，
       消灭「第 4 / 1 页」陈旧分页态 */
    if (page) {
      const total = Number(page.total || 0);
      const lastPageIndex = Math.max(1, Math.ceil(total / LECTURE_PAGE_LIMIT));
      const maxOffset = (lastPageIndex - 1) * LECTURE_PAGE_LIMIT;
      if (lectureOffset > maxOffset) {
        lectureOffset = maxOffset;
        page = await fetchLecturePage(courseId, lectureOffset);
      }
    }
    clear(lectureWrap);
    lectureWrap.append(page ? buildLectureTable(page, courseId) : textElement("p", DATA_TEXT.notCounted, "hint"));
  } catch {
    clear(lectureWrap);
    lectureWrap.append(textElement("p", DATA_TEXT.notCounted, "hint"));
  } finally {
    container.removeAttribute("aria-busy");
  }
}

/* ---- 选择与批量条 ---- */

const selectedRows = () => courseRows().filter((row) => selectedCourseIds.has(row.course_id));
const selectedNames = () => selectedRows().map((row) => row.title || row.course_id);

/* 单确认梯度（B3）：两击臂式——首击换确认文案，再击执行；超时自动复位。
   同 settings.js armDeleteConfirmation 先例，语义保持一致。 */
function armConfirmButton(button, confirmLabel, onConfirmed) {
  if (button.dataset.confirming === "true") {
    button.dataset.confirming = "false";
    button.textContent = button.dataset.originalLabel || button.textContent;
    void onConfirmed();
    return;
  }
  button.dataset.confirming = "true";
  button.dataset.originalLabel = button.textContent;
  button.textContent = confirmLabel;
  window.setTimeout(() => {
    if (button.dataset.confirming === "true") {
      button.dataset.confirming = "false";
      button.textContent = button.dataset.originalLabel || button.textContent;
    }
  }, 4000);
}

function renderSelection() {
  const bar = $("data-bulk-bar");
  const count = $("data-selection-count");
  const hint = $("data-bulk-hint");
  const actionRow = $("data-bulk-actions");
  const orphanContext = orphansOnly && hasOrphanRows();
  const size = selectedCourseIds.size;
  if (count) {
    count.textContent = `${DATA_TEXT.selectedPrefix}${size}${DATA_TEXT.selectedSuffix}`;
    count.hidden = false;
  }
  /* 空态可发现性：未选且非孤儿过滤时以提示行替代整条隐藏 */
  const emptyState = size === 0 && !orphanContext;
  if (hint) hint.hidden = !emptyState;
  if (count) count.hidden = emptyState;
  if (actionRow) actionRow.hidden = emptyState;
  if (bar) bar.hidden = false;
  /* W3：delete-records 引擎恰 1 课程——选中数≠1 时禁用并给出人话指引；
     W9：零选（孤儿过滤态）禁用批量动作组，「清除全部孤儿」语义独立保持可用 */
  const singleSelection = size === 1;
  const deleteButton = $("data-action-delete-records");
  if (deleteButton) {
    deleteButton.disabled = size === 0 || !singleSelection;
    deleteButton.title = singleSelection ? ACTION_HINTS["delete-records"] : DELETE_RECORDS_SINGLE_HINT;
  }
  for (const buttonId of ["data-action-rebuild-search", "data-action-purge-derived", "data-action-remove-copies", "data-action-export", "data-action-export-study-stats"]) {
    const button = $(buttonId);
    if (button) button.disabled = size === 0;
  }
  const clearOrphans = $("data-clear-orphans");
  if (clearOrphans) clearOrphans.hidden = !orphanContext;
}

/* ---- 动作执行（闭集 + 确认梯度 + 整批拒绝呈现） ---- */

/* 请求体确认梯度对齐冻结引擎（api_v3.validate_course_data_action +
   application.course_data_perform_action）：purge-derived/remove-copies
   发送布尔 confirm:true；typed 档（delete-records 与孤儿清理变体）发送
   字符串 confirm_typed；孤儿清理是 remove-copies + include_orphans 变体。 */
async function submitAction(action, courseIds, confirmTyped, { includeOrphans = false } = {}) {
  if (actionBusy) return;
  actionBusy = true;
  /* D7（焦点归还家族）：执行会重建表格/结果面，聚焦的执行钮可能被销毁而焦点
     掉 BODY——进场先记语义锚，收口时焦点已丢则归还（原钮仍在文档→回原钮，
     否则回页头稳定钮「刷新」）。 */
  const focusAnchor = document.activeElement;
  const anchorConnected = (el) => (typeof document.contains === "function" ? document.contains(el) : Boolean(el));
  const busyTargets = document.querySelectorAll("[data-data-action], #data-bulk-bar button");
  busyTargets.forEach((button) => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  });
  const body = { action, course_ids: courseIds, operation_id: operationId("course-data") };
  const orphanCleanup = action === "remove-copies" && includeOrphans;
  if (action === "purge-derived" || action === "remove-copies") body.confirm = true;
  if (orphanCleanup) body.include_orphans = true;
  if (action === "delete-records" || orphanCleanup) {
    body.confirm_typed = typeof confirmTyped === "string" ? confirmTyped : "";
  }
  try {
    renderActionResult(await postV3("course-data/actions", body));
  } catch (error) {
    /* D-20261009-01（化身走查 P2）：409 拒绝回执（rejected+blockers 信封）经
       api.js receipt 透传后照常走拒绝渲染分支——学生看到「先处理进行中任务」
       类具体指引，不再落「请稍后重试」死路（重试恒 409）。其余错误维持原兜底。 */
    renderActionResult(error?.receipt || null);
  } finally {
    actionBusy = false;
    /* D7：await 后重取按钮（在途时可能有新渲染面），再按选择态重施禁用矩阵 */
    document.querySelectorAll("[data-data-action], #data-bulk-bar button").forEach((button) => {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    });
    renderSelection();
    const focusNow = document.activeElement;
    if (!focusNow || focusNow === document.body) {
      const anchor = focusAnchor && typeof focusAnchor.focus === "function" && anchorConnected(focusAnchor)
        ? focusAnchor
        : $("data-refresh");
      if (anchor && typeof anchor.focus === "function") anchor.focus({ preventScroll: true });
    }
    if (action !== "export" && action !== "export-study-stats") void loadSummary({ force: true });
  }
}

function renderActionResult(receipt) {
  const row = $("data-result");
  if (!row) return;
  row.hidden = false;
  clear(row);
  const valid = receipt && receipt.schema === ACTION_RESULT_SCHEMA;
  const label = valid ? ACTION_LABELS[receipt.action] || receipt.action : "";
  if (valid && receipt.status === "accepted") {
    row.dataset.state = "ready";
    row.append(textElement("span", `「${label}${DATA_TEXT.actionDoneSuffix}`));
    /* STUDY-STATS-M3：导出学习统计回执带文件清单——每文件一个 a[download]
       直下链接（materials 导出同族：现成文件直交另存对话框）；同批可能生成
       同名文件（同日重复导出覆盖），链接恒指向最新内容。 */
    const files = Array.isArray(receipt.result?.files) ? receipt.result.files : [];
    for (const file of files) {
      const filename = String(file?.filename || "");
      if (!filename) continue;
      const link = document.createElement("a");
      link.className = "text-button";
      link.textContent = `下载 ${filename}`;
      link.href = `/api/v3/study-stats/file?name=${encodeURIComponent(filename)}`;
      link.setAttribute("download", filename);
      link.setAttribute("aria-label", `下载学习统计导出文件 ${filename}`);
      row.append(link);
    }
    if (files.length) {
      row.append(textElement("span", `（文件也在 数据目录/${String(receipt.result?.directory || "study-stats-exports")} 里留有一份）`, "hint"));
    }
    return;
  }
  row.dataset.state = "action";
  if (valid && receipt.status === "rejected" && Array.isArray(receipt.blockers) && receipt.blockers.length) {
    row.append(textElement("span", `「${label}${DATA_TEXT.actionRejectedSuffix}`));
    for (const blocker of receipt.blockers) {
      const hint = BLOCKER_TEXT[String(blocker?.code || "")];
      if (!hint) continue; /* 未知码不渲染，绝不发明 */
      const count = Number(blocker.count || 0);
      row.append(textElement("span", hint + (count > 1 ? `（${count}）` : ""), "data-blocker"));
    }
    return;
  }
  row.append(textElement("span", DATA_TEXT.actionFailure));
}

/* ---- typed 危险确认（Q1：逐课程名精确匹配后确认按钮才启用） ---- */

function confirmBaseHint(action, names, rows) {
  const bytes = rows.reduce((sum, row) => sum + Number(row.total_file_bytes || 0), 0);
  const parts = [
    `${DATA_TEXT.confirmHeaderPrefix}${names.length}${DATA_TEXT.confirmHeaderMid}${formatDataBytes(bytes)}${DATA_TEXT.confirmHeaderSuffix}`,
  ];
  if (action === "remove-copies" || action === "clear-orphans") parts.push(DATA_TEXT.removeCopiesNote);
  if (action === "purge-derived") parts.push(DATA_TEXT.remoteNote);
  if (action === "clear-orphans") parts.push(DATA_TEXT.orphanAutomationNote);
  return parts.join("");
}

function renderConfirmPrompt() {
  const prompt = confirmPromptNode || document.querySelector(".data-confirm-prompt");
  if (!prompt || !pendingConfirm) return;
  if (pendingConfirm.includeOrphans || pendingConfirm.typedValue || !pendingConfirm.names.length) {
    prompt.textContent = "";
    return;
  }
  /* OBS-8：孤儿课程无 title，names[0] 退回课程编号——提示语按行内实情说
     「课程编号」，与键入门禁（W3 逐字精确匹配 names[0]）逐字对齐 */
  const row = courseRows().find((candidate) => candidate.course_id === pendingConfirm.courseIds[0]);
  const prefix = row?.title ? DATA_TEXT.confirmPromptPrefix : DATA_TEXT.confirmPromptIdPrefix;
  prompt.textContent = `${prefix}${pendingConfirm.names[0]}${DATA_TEXT.confirmPromptSuffix}`;
}

function setConfirmInputLabel(includeOrphans, hasNamedRow) {
  const label = $("data-confirm-label");
  if (!label) return;
  const message = includeOrphans
    ? DATA_TEXT.confirmInputLabelAny
    : hasNamedRow
      ? DATA_TEXT.confirmInputLabelNamed
      : DATA_TEXT.confirmInputLabelId;
  /* D-20261009-07：label 的子元素就是确认输入框本身——对 label 赋
     textContent 会连 <input> 一起抹掉，openTypedConfirm 随即命中 null、
     弹窗永不打开（typed 危险确认全族死亡）。文案只写进专用 span；
     markup 漂移缺 span 时退回首个文本节点或补建 span，两条兜底同样
     绝不触碰元素子节点。 */
  const slot = $("data-confirm-label-text");
  if (slot) {
    slot.textContent = message;
    return;
  }
  const textNode = Array.from(label.childNodes || []).find((node) => node.nodeType === 3);
  if (textNode) {
    textNode.nodeValue = message;
    return;
  }
  const span = document.createElement("span");
  span.id = "data-confirm-label-text";
  span.textContent = message;
  if (typeof label.prepend === "function") label.prepend(span);
  else label.append(span);
}

function openTypedConfirm(action, courseIds, names) {
  const dialog = $("data-confirm-dialog");
  if (!dialog) return;
  const rows = courseRows().filter((row) => courseIds.includes(row.course_id));
  /* 清除全部孤儿（Q2）按冻结引擎映射为 remove-copies + include_orphans 提交；
     逐课程名精确回执（Q1）仅 delete-records 需要，孤儿清理用非空确认语 */
  pendingConfirm = {
    action: action === "clear-orphans" ? "remove-copies" : action,
    includeOrphans: action === "clear-orphans",
    typedValue: "",
    label: ACTION_LABELS[action] || action,
    courseIds,
    names: [...names],
  };
  $("data-confirm-title").textContent = pendingConfirm.label;
  /* OBS-8：输入标签随门禁实情对齐（孤儿清理=任意文字；在册课程=课程名；
     无名孤儿行=课程编号）——文案不再声称「课程名」却要求键入编号 */
  setConfirmInputLabel(action === "clear-orphans", Boolean(rows[0]?.title));
  const hint = $("data-confirm-hint");
  clear(hint);
  hint.append(textElement("span", confirmBaseHint(action, names, rows)));
  if (action === "clear-orphans") {
    const lead = textElement("span", DATA_TEXT.orphanListLead);
    lead.style.display = "block";
    hint.append(lead);
    const orphanList = textElement("span", "", "data-orphan-list");
    rows.forEach((row) => {
      const name = textElement("span", row.title || row.course_id, "data-orphan-name");
      name.style.display = "block";
      orphanList.append(name);
    });
    hint.append(orphanList);
  }
  confirmPromptNode = textElement("span", "", "data-confirm-prompt");
  hint.append(confirmPromptNode);
  const input = $("data-confirm-input");
  input.value = "";
  $("data-confirm-confirm").disabled = true;
  renderConfirmPrompt();
  dialog.showModal();
}

function handleConfirmInput() {
  if (!pendingConfirm) return;
  const input = $("data-confirm-input");
  if (pendingConfirm.includeOrphans) {
    /* 孤儿清理回执=非空确认语（引擎仅要求非空，不逐课程名匹配） */
    pendingConfirm.typedValue = input.value;
    $("data-confirm-confirm").disabled = input.value.trim().length === 0;
    return;
  }
  /* W3：delete-records 引擎恰 1 课程——typed 流程只剩单课程逐字精确匹配；
     匹配即消费唯一期望名（names 清空放行提交门）；不匹配时确认键保持禁用
     （W9：names 为空也绝不可能启用） */
  const expected = pendingConfirm.names[0] || "";
  const matched = Boolean(expected) && input.value.trim() === expected;
  if (matched) {
    pendingConfirm.typedValue = input.value.trim();
    pendingConfirm.names = [];
  } else {
    pendingConfirm.typedValue = "";
  }
  $("data-confirm-confirm").disabled = !matched;
  renderConfirmPrompt();
}

/* 动作执行入口（确认梯度）：重建/导出=无确认；清理派生/移除副本=单确认布尔；
   删除记录/清除全部孤儿=typed dialog。清除全部孤儿按 Q2 冻结引擎语义映射为
   闭集动作 remove-copies + include_orphans（非空 typed 确认语）。
   W3：delete-records 引擎恰 1 课程——多名额死路径在入口直接拦截（按钮层已禁用）。 */
async function runCourseAction(action, courseIds, names, trigger) {
  if (actionBusy) return;
  if (action === "delete-records") {
    if (courseIds.length !== 1) return;
    openTypedConfirm(action, courseIds, names);
    return;
  }
  if (action === "clear-orphans") {
    openTypedConfirm(action, courseIds, names);
    return;
  }
  if (action === "purge-derived" || action === "remove-copies") {
    if (trigger) {
      armConfirmButton(trigger, "确认执行？", () => submitAction(action, courseIds, ""));
      return;
    }
    await submitAction(action, courseIds, "");
    return;
  }
  await submitAction(action, courseIds, "");
}

/* ---- 汇总加载（stale 不自动刷新；错误走 recovery-panel 闭集呈现） ---- */

async function loadSummary({ force = false } = {}) {
  if (summaryLoading) return;
  if (summaryValue && !force) {
    renderSummaryStrip();
    renderCourseList();
    return;
  }
  summaryLoading = true;
  renderSummaryStrip();
  renderCourseList();
  try {
    let rows = [];
    let payload = null;
    for (let page = 1; page <= SUMMARY_MAX_PAGES; page += 1) {
      const chunk = await apiV3(
        `course-data?page=${page}&page_size=${SUMMARY_PAGE_SIZE}&include_orphans=true`,
      );
      if (chunk?.schema !== SUMMARY_SCHEMA) break;
      payload = chunk;
      rows = rows.concat(Array.isArray(chunk.rows) ? chunk.rows : []);
      if (rows.length >= Number(chunk.page?.total || 0)) break;
    }
    summaryValue = payload ? { ...payload, rows } : null;
    /* W5：按现存行剪枝选择集，消灭动作后幽灵 ID 进入计数与批量请求 */
    const aliveCourseIds = new Set(courseRows().map((row) => row.course_id));
    selectedCourseIds = new Set([...selectedCourseIds].filter((courseId) => aliveCourseIds.has(courseId)));
    summaryLoading = false;
    renderSummaryStrip();
    renderCourseList();
    /* 第十六案：动作后的强制重载必须连同打开中的课程明细一起重渲染——
       顶部总数与行列表同源更新了，展开的明细面板（类别栅格/讲次表）却
       停在移除前的旧数据（活体报实证）。宽布局刷 data-detail，窄布局刷
       当前行展开体。 */
    if (selectedCourseId) void renderDetailInto(selectedCourseId, detailTargetFor(selectedCourseId));
  } catch (error) {
    summaryLoading = false;
    renderSummaryStrip();
    renderCourseList();
    renderSummaryError(error);
  }
}

function renderSummaryError(error) {
  const recovery = $("data-recovery");
  if (!recovery) return;
  recovery.hidden = false;
  const actions = $("data-recovery-actions");
  clear(actions);
  const code = String(error?.code || "");
  const needsLogin = code === "fudan_login_required" || code === "fudan_session_expired";
  const impact = $("data-recovery-impact");
  if (impact) impact.textContent = needsLogin ? DATA_TEXT.loginHint : DATA_TEXT.actionFailure;
  if (needsLogin) {
    const login = textElement("button", "登录");
    login.type = "button";
    login.addEventListener("click", () => {
      recovery.hidden = true;
      window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "login" }));
    });
    actions.append(login);
  }
  const retry = textElement("button", "重试");
  retry.type = "button";
  retry.addEventListener("click", () => {
    recovery.hidden = true;
    void loadSummary({ force: true });
  });
  actions.append(retry);
}

export function installCourseData(store) {
  const handlePageRequest = (event) => {
    if (event.detail === "data" && !summaryValue && !summaryLoading) void loadSummary({ force: true });
  };
  window.addEventListener("courselens:page", handlePageRequest);

  $("account-menu-data")?.addEventListener("click", () => {
    closeOverlay($("account-menu"));
    selectPage("data");
  });
  /* 批量条分组说明：per-button 大白话 title（闭集 ACTION_HINTS） */
  for (const [elementId, hint] of [
    ["data-action-rebuild-search", "rebuild-search"],
    ["data-action-export", "export"],
    ["data-action-export-study-stats", "export-study-stats"],
    ["data-action-purge-derived", "purge-derived"],
    ["data-action-remove-copies", "remove-copies"],
    ["data-action-delete-records", "delete-records"],
  ]) {
    const button = $(elementId);
    if (button && ACTION_HINTS[hint]) button.title = ACTION_HINTS[hint];
  }
  $("data-refresh")?.addEventListener("click", () => { void loadSummary({ force: true }); });
  $("data-search")?.addEventListener("input", () => renderCourseList());
  const categorySelect = $("data-category-filter");
  if (categorySelect) {
    clear(categorySelect);
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.text = "全部类别";
    categorySelect.append(allOption);
    for (const key of CATEGORY_ORDER) {
      const option = document.createElement("option");
      option.value = key;
      option.text = CATEGORY_LABELS[key];
      categorySelect.append(option);
    }
    categorySelect.addEventListener("change", () => renderCourseList());
  }
  $("data-orphans-filter")?.addEventListener("click", () => {
    orphansOnly = !orphansOnly;
    renderSummaryStrip();
    renderCourseList();
    renderSelection();
  });
  $("data-select-all")?.addEventListener("click", () => {
    filteredRows().forEach((row) => selectedCourseIds.add(row.course_id));
    renderCourseList();
  });
  $("data-clear-selection")?.addEventListener("click", () => {
    selectedCourseIds = new Set();
    renderCourseList();
  });
  const bulkActions = [
    ["data-action-rebuild-search", "rebuild-search"],
    ["data-action-purge-derived", "purge-derived"],
    ["data-action-remove-copies", "remove-copies"],
    ["data-action-export", "export"],
    ["data-action-export-study-stats", "export-study-stats"],
    ["data-action-delete-records", "delete-records"],
  ];
  for (const [elementId, action] of bulkActions) {
    $(elementId)?.addEventListener("click", () => {
      void runCourseAction(action, [...selectedCourseIds], selectedNames(), $(elementId));
    });
  }
  /* U5/M16 解锁路径：与选择态无关（卡住时恰恰什么都选不动），恒可点。 */
  $("data-action-release-stuck")?.addEventListener("click", (event) => {
    void runCourseAction("release-stuck", [], [], event.currentTarget);
  });
  $("data-clear-orphans")?.addEventListener("click", (event) => {
    const orphanRows = courseRows().filter((row) => row.in_catalog === false);
    void runCourseAction(
      "clear-orphans",
      orphanRows.map((row) => row.course_id),
      orphanRows.map((row) => row.title || row.course_id),
      event.currentTarget,
    );
  });
  $("data-confirm-input")?.addEventListener("input", handleConfirmInput);
  /* W4：typed 弹窗内 Enter（隐式 dialog 提交）必须转发确认按钮而非静默关窗；
     不匹配时确认键被禁用，click() 无效，弹窗保持打开 */
  $("data-confirm-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pendingConfirm) return;
    $("data-confirm-confirm")?.click();
  });
  $("data-confirm-cancel")?.addEventListener("click", () => $("data-confirm-dialog")?.close());
  $("data-confirm-confirm")?.addEventListener("click", () => {
    if (!pendingConfirm) return;
    const orphanCleanup = pendingConfirm.includeOrphans === true;
    if (orphanCleanup ? !pendingConfirm.typedValue.trim() : pendingConfirm.names.length > 0) return;
    const { action, courseIds, typedValue } = pendingConfirm;
    $("data-confirm-dialog")?.close();
    void submitAction(action, courseIds, typedValue, { includeOrphans: orphanCleanup });
  });
  const handleConfirmDialogClose = () => {
    pendingConfirm = null;
    const input = $("data-confirm-input");
    if (input) input.value = "";
    const confirm = $("data-confirm-confirm");
    if (confirm) confirm.disabled = true;
  };
  $("data-confirm-dialog")?.addEventListener("close", handleConfirmDialogClose);

  if (typeof window.matchMedia === "function") {
    mediaNarrow = window.matchMedia("(max-width: 999px)");
    const handleModeChange = () => {
      renderCourseList();
      if (selectedCourseId) void renderDetailInto(selectedCourseId, detailTargetFor(selectedCourseId));
    };
    if (typeof mediaNarrow.addEventListener === "function") {
      mediaNarrow.addEventListener("change", handleModeChange);
    }
  }
  return () => {
    window.removeEventListener("courselens:page", handlePageRequest);
  };
}
