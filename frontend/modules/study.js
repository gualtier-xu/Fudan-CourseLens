import { UI_HINTS, apiV3, postV3, putV3, deleteV3 } from "./api.js";
import { buttonTextFor } from "./greeting.js";
import { syncDropdown } from "./dropdown.js";
/* N7F：课程总体复习是课程级 surface，组合根归学习页（app.js 不在本包路径内）。
   导航出口由此处注入 —— course-review.js 不反向 import 本模块。 */
import {
  installCourseReview, isCourseReviewOpen, provideReviewNavigation, restoreCourseReview,
} from "./course-review.js";
/* THINK-LADDER-2：总结卡质量 chip（三态+findings 人话展开），随讲次切换刷新。 */
import { refreshQualityChip } from "./quality-chip.js";
/* STUDY-STATS-M1：着陆页「本周学习面貌」卡（学习统计 v2 默认层），
   取代复习页签恒零死格；组合根在 installStudy（app.js 组合根不在本道恰域）。 */
import { installStudyStats } from "./study-stats.js";
import {
  $, clear, evidenceDetails, formatTime, friendlyTerm, operationId, recoveryActionLabel, setBusy, slidesSkippedText, textElement, toast,
} from "./ui.js";
/* WAIT-UX-1（零呆等三律）：预计/预期文案闭集单源——数据来源与重测日期见该文件头注 */
import { COURSEWARE_PDF_QUEUED_HINT } from "./wait-expectations.js";

let controller = null;
let bookmarkController = null;
let bookmarkEpoch = 0;
let bookmarkActionEpoch = 0;
const bookmarkActionControllers = new Set();
let bookmarkItems = [];
let transcriptController = null;
let transcriptEpoch = 0;
let studyInstanceEpoch = 0;
let coursewarePdfTimer = 0;
let quizGenerateTimer = 0; /* WAIT-UX-1 E4：组卷进度行步进定时器（自链 setTimeout） */
let coursewarePdfPollEpoch = 0;
let transcriptSubId = "";
let activeTranscriptIndex = -1;
let transcriptRows = [];
let transcriptManualScrollUntil = 0;
/* 窗口化渲染（VTT-PERF）的当前加载上下文：跟随高亮滑窗保行需要 store/lecture */
let transcriptContext = null;
/* 文稿检索 Phase A（G5/P10 缺口④）处于激活态（检索视图替换列表视图） */
let transcriptSearchActive = false;
const TRANSCRIPT_SCROLL_KEYS = new Set(["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "]);

/* 动作失败 toast 分流（AUTOLOGIN-LOCAL-FIRST-1③）：恢复期本地可信上下文的
   闭集新码 fudan_session_restoring 呈现为瞬态非错误提示（checking 形态，
   文案来自 api.js 闭集映射「正在登录，请稍候…」）；其余维持既有错误 toast。 */
function toastActionError(error) {
  const restoring = String(error?.code || "") === "fudan_session_restoring";
  toast(error?.message || "操作未完成", restoring ? "checking" : "error");
}

export function transcriptSegmentIndex(segments, timeMs, transcriptSubId = "", playerSubId = "") {
  const current = Number(timeMs);
  if (!Number.isFinite(current) || current < 0 || (transcriptSubId && transcriptSubId !== playerSubId)) return -1;
  return segments.findIndex(({ startMs, endMs }) => current >= startMs && current < endMs);
}

function transcriptTiming(segment) {
  const startMs = Number(segment?.start_ms);
  const endMs = Number(segment?.end_ms);
  return Number.isFinite(startMs) && Number.isFinite(endMs) && startMs >= 0 && endMs > startMs
    ? { startMs, endMs }
    : null;
}

/* evidence.v1 身份契约（src/runtime/subtitle_reader.py）：前缀:12位十六进制。
   缺失或畸形的 evidence_id 一律按旧行处理，绝不发明或展示身份。 */
const EVIDENCE_ID_PATTERN = /^(src|seg|cor|cue|slent|slevt|unit|chk|mtr):[0-9a-f]{12}$/;

export function isContractEvidenceId(value) {
  return typeof value === "string" && EVIDENCE_ID_PATTERN.test(value);
}

export function segmentEvidenceId(segment) {
  const value = segment?.evidence_id;
  return isContractEvidenceId(value) ? value : "";
}

export function evidenceAnchorMs(evidence) {
  const ms = Number(evidence?.start_ms);
  return Number.isFinite(ms) && ms >= 0 ? Math.floor(ms) : null;
}

function resetTranscriptLoading() {
  transcriptEpoch += 1;
  transcriptController?.abort();
  transcriptController = null;
}

export function isCurrentTranscriptLoad(requestEpoch, currentEpoch, requestSubId, activeSubId, requestInstanceEpoch, currentInstanceEpoch) {
  return requestEpoch === currentEpoch
    && String(requestSubId) === String(activeSubId || "")
    && requestInstanceEpoch === currentInstanceEpoch;
}

function clearTranscriptFollow() {
  if (activeTranscriptIndex >= 0) {
    const row = transcriptRows[activeTranscriptIndex]?.row;
    row?.classList.remove("active");
    row?.removeAttribute("aria-current");
  }
  activeTranscriptIndex = -1;
}

function resetTranscriptFollow() {
  clearTranscriptFollow();
  transcriptSubId = "";
  transcriptRows = [];
  transcriptManualScrollUntil = 0;
}

export function isTranscriptScrollIntent(event) {
  if (event.type === "keydown" && !TRANSCRIPT_SCROLL_KEYS.has(event.key)) return false;
  if (event.type !== "keydown" && !["wheel", "touchstart", "pointerdown"].includes(event.type)) return false;
  return !event.target?.closest?.("button");
}

function followTranscript(subId, timeMs) {
  if (!subId || subId !== transcriptSubId) return clearTranscriptFollow();
  const nextIndex = transcriptSegmentIndex(transcriptRows, timeMs, transcriptSubId, subId);
  if (nextIndex === activeTranscriptIndex) return;
  clearTranscriptFollow();
  if (nextIndex < 0) return;
  activeTranscriptIndex = nextIndex;
  /* 窗口化路径：目标行未上屏时先滑窗保行（全量路径 transcriptVirtual=null 直通） */
  const context = transcriptContext;
  if (context) ensureTranscriptRowRendered(context.store, context.lecture, nextIndex);
  const row = transcriptRows[nextIndex].row;
  if (!row) return; /* 理论不可达：ensure 后行必在 */
  row.classList.add("active");
  row.setAttribute("aria-current", "true");
  if (Date.now() >= transcriptManualScrollUntil) {
    row.scrollIntoView?.({ block: "nearest" });
  }
}

// ---- 文稿检索 Phase A（G5/P10 缺口④）：单课 cues 内存子串匹配，零依赖、
// ---- 零出域、大小写折叠对英文有效、中文天然子串命中。检索视图替换列表视图；
// ---- 命中行复用既有 timestamp-button seek 通道（与行内点击同一语义，含手动
// ---- 滚动保护与 followTranscript 高亮链的既有共存规则）。输入框聚焦时
// ---- 播放器快捷键让位（player-core 的焦点收权合同），跟随滚动不移动焦点。
let transcriptSearchCursor = 0;
let transcriptSearchLastQuery = "";

function transcriptSearchElements() {
  return {
    input: $("transcript-search-input"),
    state: $("transcript-search-state"),
    results: $("transcript-search-results"),
    list: $("transcript-list"),
  };
}

function resetTranscriptSearch() {
  transcriptSearchActive = false;
  const { input, state, results, list } = transcriptSearchElements();
  if (input) input.value = "";
  if (state) {
    state.hidden = true;
    state.textContent = "";
  }
  if (results) {
    results.hidden = true;
    results.replaceChildren();
  }
  if (list) list.hidden = false;
}

/* 把命中文本写入命中段：所有出现处包 <mark>（大小写不敏感字面量），其余原文直出 */
function appendTranscriptHitText(parent, text, needle) {
  const lower = text.toLowerCase();
  let cursor = 0;
  while (cursor <= text.length) {
    const at = lower.indexOf(needle, cursor);
    if (at < 0) {
      if (cursor < text.length) parent.append(text.slice(cursor));
      break;
    }
    if (at > cursor) parent.append(text.slice(cursor, at));
    const mark = document.createElement("mark");
    mark.textContent = text.slice(at, at + needle.length);
    parent.append(mark);
    cursor = at + needle.length;
  }
}

function renderTranscriptSearch() {
  const { input, state, results, list } = transcriptSearchElements();
  if (!input || !state || !results || !list) return;
  const query = String(input.value || "").trim();
  transcriptSearchActive = query.length > 0;
  results.replaceChildren();
  if (!transcriptSearchActive) {
    state.hidden = true;
    state.textContent = "";
    results.hidden = true;
    list.hidden = false;
    return;
  }
  list.hidden = true;
  const needle = query.toLowerCase();
  const hits = transcriptRows.filter(({ text }) => String(text || "").toLowerCase().includes(needle));
  if (!hits.length) {
    results.hidden = true;
    state.textContent = `文稿里没找到「${query}」。试试更短一点的词？`;
    state.hidden = false;
    return;
  }
  /* U9 检索导航：光标=当前命中位；新查询从第 1 处开始，越界回卷 */
  if (query !== transcriptSearchLastQuery) {
    transcriptSearchCursor = 0;
    transcriptSearchLastQuery = query;
  }
  if (transcriptSearchCursor >= hits.length) transcriptSearchCursor = 0;
  state.textContent = `第 ${transcriptSearchCursor + 1} 处，共 ${hits.length} 处（Enter 下一个 / Shift+Enter 上一个）`;
  state.hidden = false;
  hits.forEach(({ startMs, text }, hitIndex) => {
    const row = document.createElement("div");
    row.className = "transcript-row transcript-hit";
    if (hitIndex === transcriptSearchCursor && typeof row.classList?.add === "function") {
      row.classList.add("transcript-hit-active");
    }
    const timeText = `${Math.floor(startMs / 60000)}:${String(Math.floor(startMs / 1000) % 60).padStart(2, "0")}`;
    const time = document.createElement("button");
    time.type = "button";
    time.className = "timestamp-button";
    time.textContent = timeText;
    time.setAttribute("aria-label", `跳转并播放到 ${timeText}`);
    time.addEventListener("click", () => {
      const player = $("player-stage");
      if (!player) return;
      player.currentTime = startMs / 1000;
      void player.play();
    });
    const body = textElement("p", "");
    appendTranscriptHitText(body, String(text || ""), needle);
    row.append(time, body);
    results.append(row);
  });
  results.hidden = false;
}

/* ---- 文稿窗口化渲染（VTT-PERF）：10MB 级讲次的字幕行惰性上屏 ----
   小讲次（≤ TRANSCRIPT_CHUNK_ROWS × TRANSCRIPT_KEEP_CHUNKS 行）走原全量
   路径：DOM 与既有行为逐字节一致（无垫片/无监听）。大讲次只把视口±缓冲
   窗内的分块行建进 DOM，远端块以等高垫片占位、滚动时滑动窗口；点时间戳
   跳转/检索定位/跟随高亮/书签/快捷键语义零回退——检索视图独立于列表 DOM，
   跟随高亮经 ensureTranscriptRowRendered 先保行再高亮。行内结构与样式类
   与全量路径同源（buildTranscriptRow 单一出处）。 */
const TRANSCRIPT_CHUNK_ROWS = 200;
const TRANSCRIPT_KEEP_CHUNKS = 6;
const TRANSCRIPT_ROW_ESTIMATE_PX = 57; /* 与 .transcript-row contain-intrinsic-size 同源（56px 行+1px 分隔） */
const TRANSCRIPT_WINDOW_BUFFER_PX = 1400;
let transcriptVirtual = null;

function teardownTranscriptVirtual() {
  const previous = transcriptVirtual;
  transcriptVirtual = null;
  transcriptContext = null;
  if (!previous) return;
  if (previous.onScroll) {
    document.removeEventListener("scroll", previous.onScroll, true);
    window.removeEventListener("resize", previous.onScroll);
  }
}

function transcriptScrollerOf(list) {
  /* 主布局=列表自身滚动（.transcript-list max-height+overflow），必须先查自身
     再上溯祖先；窄屏 max-height:none 时上溯命中页滚容器或落到页滚分支 */
  let node = list;
  while (node && node !== document.body && node !== document.documentElement) {
    const style = typeof getComputedStyle === "function" ? getComputedStyle(node) : null;
    const overflowY = String(style?.overflowY || "").toLowerCase();
    if ((overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay")
      && (node.scrollHeight || 0) > (node.clientHeight || 0) + 1) {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

function transcriptChunkRowCount(totalRows, chunk) {
  const start = chunk * TRANSCRIPT_CHUNK_ROWS;
  return Math.max(0, Math.min(TRANSCRIPT_CHUNK_ROWS, totalRows - start));
}

function transcriptChunkEstimate(v, chunk) {
  const measured = v.estimates[chunk];
  if (measured != null) return measured;
  return transcriptChunkRowCount(v.totalRows, chunk) * TRANSCRIPT_ROW_ESTIMATE_PX;
}

function transcriptUpdateSpacers(v) {
  const pad = (count) => `${Math.max(0, Math.round(count))}px`;
  let top = 0;
  for (let chunk = 0; chunk < v.lo; chunk += 1) top += transcriptChunkEstimate(v, chunk);
  let bottom = 0;
  for (let chunk = v.hi + 1; chunk < v.totalChunks; chunk += 1) bottom += transcriptChunkEstimate(v, chunk);
  if (v.topSpacer) v.topSpacer.style.setProperty("height", pad(top));
  if (v.bottomSpacer) v.bottomSpacer.style.setProperty("height", pad(bottom));
}

function transcriptApplyActiveClass(v) {
  if (activeTranscriptIndex < 0) return;
  const entry = v.rows[activeTranscriptIndex];
  if (entry?.row) {
    entry.row.classList.add("active");
    entry.row.setAttribute("aria-current", "true");
  }
}

function transcriptMeasureChunk(v, chunk) {
  const start = chunk * TRANSCRIPT_CHUNK_ROWS;
  const end = Math.min(v.totalRows, start + TRANSCRIPT_CHUNK_ROWS);
  let measured = 0;
  for (let index = start; index < end; index += 1) {
    const rect = v.rows[index].row?.getBoundingClientRect?.();
    if (!rect || !Number.isFinite(rect.height)) return; /* 无布局环境：估值兜底 */
    measured += rect.height;
  }
  v.estimates[chunk] = measured;
}

function transcriptApplyWindow(store, lecture, v, lo, hi) {
  const maxLo = Math.max(0, Math.min(Math.max(0, lo), v.totalChunks - 1));
  const minHi = Math.min(v.totalChunks - 1, Math.max(hi, maxLo));
  const keep = Math.min(TRANSCRIPT_KEEP_CHUNKS, v.totalChunks);
  /* 窗口恒扩到 keep 块（末端收敛）：向下钉住请求首块、向上补满帽宽——
     只钳边界不钳请求跨度，滚动滑窗才能持续前进（垫片区请求常为单块） */
  const cappedLo = Math.min(maxLo, v.totalChunks - keep);
  const cappedHi = Math.min(v.totalChunks - 1, Math.max(minHi, cappedLo + keep - 1));
  for (const chunk of [...v.rendered]) {
    if (chunk < cappedLo || chunk > cappedHi) {
      const start = chunk * TRANSCRIPT_CHUNK_ROWS;
      const end = Math.min(v.totalRows, start + TRANSCRIPT_CHUNK_ROWS);
      for (let index = start; index < end; index += 1) {
        const row = v.rows[index].row;
        if (row?.parentNode) row.parentNode.removeChild(row);
        v.rows[index].row = null;
      }
      v.rendered.delete(chunk);
    }
  }
  const missing = [];
  for (let chunk = cappedLo; chunk <= cappedHi; chunk += 1) {
    if (!v.rendered.has(chunk)) missing.push(chunk);
  }
  if (missing.length) {
    const fragment = document.createDocumentFragment();
    for (const chunk of missing) {
      const start = chunk * TRANSCRIPT_CHUNK_ROWS;
      const end = Math.min(v.totalRows, start + TRANSCRIPT_CHUNK_ROWS);
      for (let index = start; index < end; index += 1) {
        const entry = v.rows[index];
        entry.row = buildTranscriptRow(store, lecture, v.segments[index], entry);
        fragment.append(entry.row);
      }
      v.rendered.add(chunk);
    }
    const anchor = missing[0] < v.lo && v.topSpacer.nextSibling
      ? v.topSpacer.nextSibling
      : v.bottomSpacer;
    if (typeof v.container.insertBefore === "function") {
      v.container.insertBefore(fragment, anchor);
    } else {
      v.container.append(fragment);
    }
    for (const chunk of missing) transcriptMeasureChunk(v, chunk);
  }
  v.lo = cappedLo;
  v.hi = cappedHi;
  transcriptUpdateSpacers(v);
  transcriptApplyActiveClass(v);
}

function transcriptReconcileWindow(store, lecture) {
  const v = transcriptVirtual;
  if (!v || !v.container || v.container.hidden) return;
  const rect = typeof v.container.getBoundingClientRect === "function"
    ? v.container.getBoundingClientRect()
    : null;
  if (!rect) return;
  const scroller = transcriptScrollerOf(v.container);
  let viewStart;
  let viewEnd;
  if (scroller) {
    viewStart = scroller.scrollTop || 0;
    viewEnd = viewStart + (scroller.clientHeight || 0);
  } else {
    viewStart = Math.max(0, -rect.top);
    viewEnd = viewStart + (window.innerHeight || rect.height || 0);
  }
  let lo = null;
  let hi = null;
  let offset = 0;
  for (let chunk = 0; chunk < v.totalChunks; chunk += 1) {
    const height = transcriptChunkEstimate(v, chunk);
    if (offset + height >= viewStart - TRANSCRIPT_WINDOW_BUFFER_PX && offset <= viewEnd + TRANSCRIPT_WINDOW_BUFFER_PX) {
      if (lo === null) lo = chunk;
      hi = chunk;
    }
    offset += height;
    if (lo !== null && offset > viewEnd + TRANSCRIPT_WINDOW_BUFFER_PX) break;
  }
  if (lo === null) return;
  if (lo !== v.lo || hi !== v.hi) transcriptApplyWindow(store, lecture, v, lo, hi);
}

function ensureTranscriptRowRendered(store, lecture, index) {
  const v = transcriptVirtual;
  if (!v) return; /* 全量路径：行恒在 DOM */
  const chunk = Math.floor(index / TRANSCRIPT_CHUNK_ROWS);
  if (chunk < 0 || chunk >= v.totalChunks) return;
  if (v.rendered.has(chunk)) return;
  transcriptApplyWindow(store, lecture, v, chunk, chunk + TRANSCRIPT_KEEP_CHUNKS - 1);
}

function buildTranscriptRow(store, lecture, segment, entry) {
  const row = document.createElement("div");
  row.className = "transcript-row";
  row.setAttribute("role", "listitem"); /* P2-3（D14）：读屏播报「N 条列表」 */
  if (entry.timing) {
    row.dataset.startMs = String(entry.startMs);
    row.dataset.endMs = String(entry.endMs);
  }
  const evidenceId = segmentEvidenceId(segment);
  if (evidenceId) row.dataset.evidenceId = evidenceId;
  const time = document.createElement("button");
  time.type = "button";
  time.className = "timestamp-button";
  const timeText = `${Math.floor(Number(segment.start_ms || 0) / 60000)}:${String(Math.floor(Number(segment.start_ms || 0) / 1000) % 60).padStart(2, "0")}`;
  time.textContent = timeText;
  time.setAttribute("aria-label", `跳转并播放到 ${timeText}`);
  time.addEventListener("click", () => {
    const player = $("player-stage");
    player.currentTime = Number(segment.start_ms || 0) / 1000;
    void player.play();
  });
  const text = textElement("p", segment.text || "");
  const bookmark = document.createElement("button");
  bookmark.type = "button";
  bookmark.className = "icon-button";
  bookmark.title = "加入书签";
  bookmark.setAttribute("aria-label", `在 ${timeText} 加入书签`);
  bookmark.append(renderBookmarkIcon());
  bookmark.addEventListener("click", async () => {
    try {
      await postV3("bookmarks", {
        course_id: lecture.course_id,
        sub_id: lecture.sub_id,
        start_ms: segment.start_ms,
        end_ms: segment.end_ms,
      });
      toast("书签已保存", "ready");
      await loadBookmarks(store);
    } catch (error) {
      /* F2（化身走查 20261008）：无字幕讲次的「加入书签」此前借道通用码表
         ERROR_MESSAGES，弹出提问链的责备文案（「再重新提问」）——学生此刻的
         动作是收藏不是提问，且 toast 区全局跨页存活，跳转直播页时呈现为
         「落页即弹的无来由报错」。按码就地给中性人话：指路先生成字幕，
         不责备、不借提问语义（player-core 没听懂标记路径 REALFULL-1 同款）。 */
      if (String(error?.code || "") === "bookmark_evidence_unavailable") {
        toast("这一讲还没有字幕或讲义可作依据，暂时存不了这个书签；先生成本讲字幕，就可以在这里收藏了。", "checking");
        return;
      }
      toastActionError(error);
    }
  });
  row.append(time, text, bookmark);
  return row;
}

/* 结构化 artifact.content 优先（overview / key_takeaways / chapters），legacy
   markdown/text 只是安全回退：任何路径都不 JSON.stringify 原始载荷、不暴露哈希。 */
const IR_CONTRACT = "evidence.v1";

function chapterAnchorMs(chapter) {
  return evidenceAnchorMs(chapter);
}

/* 非负整数毫秒或 null：与生产端 fail-closed 对齐（锚错比锚缺更伤信任）。
   注意 null/"" 显式判无锚——Number(null)===0 会把「无锚」伪装成「0ms」。 */
function takeawayAnchorMs(value) {
  if (value == null || value === "") return null;
  const ms = Number(value);
  return Number.isFinite(ms) && ms >= 0 ? Math.floor(ms) : null;
}

function structuredNotes(artifact) {
  const content = artifact?.content;
  if (!content || typeof content !== "object" || Array.isArray(content)) return null;
  const overview = typeof content.overview === "string" ? content.overview.trim() : "";
  /* RR-ANCHORFE-1：takeaway_anchors 与 key_takeaways 等长对齐（毫秒或 null）；
     缺字段/畸形/负值一律按无锚处理——只影响锚芯片，不隐藏条目。 */
  const anchorList = Array.isArray(content.takeaway_anchors) ? content.takeaway_anchors : [];
  const takeaways = (Array.isArray(content.key_takeaways) ? content.key_takeaways : [])
    .map((item, index) => ({ text: String(item ?? "").trim(), anchorMs: takeawayAnchorMs(anchorList[index]) }))
    .filter((item) => item.text);
  const chapters = (Array.isArray(content.chapters) ? content.chapters : [])
    .map((chapter) => ({
      title: String(chapter?.title || "").trim(),
      summary: String(chapter?.summary || "").trim(),
      startMs: chapterAnchorMs(chapter),
    }));
  if (!overview && !takeaways.length && !chapters.length) return null;
  return { overview, takeaways, chapters };
}

function legacyNotesText(artifact) {
  const markdown = typeof artifact?.content_markdown === "string" ? artifact.content_markdown.trim() : "";
  if (markdown) return markdown;
  const content = artifact?.content ?? artifact?.result ?? artifact;
  if (typeof content === "string" && content.trim()) return content;
  for (const key of ["summary", "markdown", "text"]) {
    const value = content?.[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function artifactStatusNote(artifact) {
  const status = String(artifact?.status || "");
  if (["running", "pending", "checking", "started"].includes(status)) return "总结正在生成，稍后可刷新查看。";
  if (status && status !== "ready") return "总结生成未完成。";
  return "";
}

/* 合法 Lecture IR 增强视图：contract 必须精确匹配 evidence.v1，key_moments 只保留
   带合法毫秒锚点的条目；缺失/为空/畸形一律返回 null，仅隐藏增强而不报错。 */
function validLectureIr(content) {
  if (!content || typeof content !== "object" || Array.isArray(content)) return null;
  if (content.contract !== IR_CONTRACT) return null;
  const keyMoments = (Array.isArray(content.key_moments) ? content.key_moments : [])
    .map((moment) => {
      const startMs = evidenceAnchorMs(moment?.time);
      const pageNum = Number(moment?.content?.page_num);
      return { startMs, page: Number.isInteger(pageNum) && pageNum > 0 ? pageNum : null };
    })
    .filter((moment) => moment.startMs != null);
  return keyMoments.length ? { keyMoments } : null;
}

function bookmarkTime(startMs) {
  const milliseconds = Number(startMs);
  const seconds = Number.isFinite(milliseconds) && milliseconds >= 0 ? Math.floor(milliseconds / 1000) : 0;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/* 字幕行书签按钮图标：手绘 inline SVG（书签丝带），不引入资源文件 */
function renderBookmarkIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M7 4h10v16l-5-3.5L7 20z");
  svg.append(path);
  return svg;
}

export function sortBookmarks(bookmarks) {
  return (Array.isArray(bookmarks) ? bookmarks : [])
    .map((bookmark, index) => ({ bookmark, index }))
    .sort((left, right) => {
      const leftStart = Number(left.bookmark?.start_ms);
      const rightStart = Number(right.bookmark?.start_ms);
      const startDifference = (Number.isFinite(leftStart) ? leftStart : Number.MAX_SAFE_INTEGER)
        - (Number.isFinite(rightStart) ? rightStart : Number.MAX_SAFE_INTEGER);
      if (startDifference) return startDifference;
      const leftEnd = Number(left.bookmark?.end_ms);
      const rightEnd = Number(right.bookmark?.end_ms);
      const endDifference = (Number.isFinite(leftEnd) ? leftEnd : Number.MAX_SAFE_INTEGER)
        - (Number.isFinite(rightEnd) ? rightEnd : Number.MAX_SAFE_INTEGER);
      return endDifference || left.index - right.index;
    })
    .map(({ bookmark }) => bookmark);
}

export function bookmarkReviewAction(bookmark) {
  const bookmarkId = String(bookmark?.bookmark_id || "");
  if (!bookmarkId) return null;
  if (bookmark?.resolution_status === "open") {
    return { bookmark_id: bookmarkId, action: "resolve", label: "标记已复习" };
  }
  if (bookmark?.resolution_status === "resolved") {
    return { bookmark_id: bookmarkId, action: "reopen", label: "重新加入复习" };
  }
  return null;
}

export function bookmarkActionIsCurrent(expectedEpoch, currentEpoch, requestSubId, activeSubId) {
  return expectedEpoch === currentEpoch && String(requestSubId) === String(activeSubId || "");
}

export function confirmedBookmarkReview(bookmark, bookmarkId, subId, action) {
  const expectedStatus = action === "resolve" ? "resolved" : action === "reopen" ? "open" : "";
  return expectedStatus
    && String(bookmark?.bookmark_id || "") === String(bookmarkId)
    && String(bookmark?.sub_id || "") === String(subId)
    && bookmark?.resolution_status === expectedStatus
    ? bookmark
    : null;
}

function resetBookmarkLoading() {
  bookmarkEpoch += 1;
  bookmarkController?.abort();
  bookmarkController = null;
}

function resetBookmarkActions() {
  bookmarkActionEpoch += 1;
  bookmarkActionControllers.forEach((actionController) => actionController.abort());
  bookmarkActionControllers.clear();
  resetBookmarkDeleteArms(); /* PLAYER-UX-1④：换讲次/卸载时撤销未确认的删除武装 */
}

function renderBookmarks(target, bookmarks) {
  clear(target);
  if (!bookmarks.length) {
    /* EMPTY-STATES-1：纯「暂无书签」升级为下一步引导——动作（没听懂小旗）在播放器里，
       这里只指路不摆钮，不造无法到达的死按钮。 */
    target.append(textElement(
      "p",
      "暂无书签。听课时在没听懂的地方点「没听懂」，就会自动记下时间点，方便回来重听。",
      "empty-state",
    ));
    return;
  }
  bookmarks.forEach((bookmark) => {
    const row = document.createElement("article");
    row.className = "bookmark-row";
    const timestamp = document.createElement("button");
    timestamp.type = "button";
    timestamp.className = "timestamp-button";
    timestamp.textContent = bookmarkTime(bookmark.start_ms);
    timestamp.setAttribute("aria-label", `跳转到 ${timestamp.textContent}`);
    timestamp.addEventListener("click", () => {
      const startMs = Number(bookmark.start_ms);
      $("player-stage").currentTime = Number.isFinite(startMs) && startMs >= 0 ? startMs / 1000 : 0;
    });
    const details = document.createElement("div");
    details.className = "bookmark-details";
    details.append(
      textElement("p", bookmark.note ? `备注：${bookmark.note}` : "未添加备注"),
      textElement(
        "span",
        bookmark.resolution_status === "resolved" ? "已解决" : bookmark.resolution_status === "open" ? "未解决" : "复习状态待确认",
        "bookmark-status",
      ),
    );
    const reviewAction = bookmarkReviewAction(bookmark);
    if (reviewAction) {
      const review = document.createElement("button");
      review.type = "button";
      review.className = "bookmark-review-button";
      review.dataset.bookmarkId = reviewAction.bookmark_id;
      review.dataset.bookmarkAction = reviewAction.action;
      review.dataset.bookmarkLabel = reviewAction.label;
      review.textContent = reviewAction.label;
      details.append(review);
    }
    /* PLAYER-UX-1④：行级删除钮（两步轻确认：首击变「确认删除」，4s 不动回弹，
       防误删；删除后时间轴标记与列表经 bookmarks-changed 双双即时消失） */
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "bookmark-delete-button";
    removeButton.dataset.bookmarkId = String(bookmark.bookmark_id || "");
    removeButton.dataset.bookmarkTime = bookmarkTime(bookmark.start_ms);
    removeButton.textContent = "删除";
    details.append(removeButton);
    row.append(timestamp, details);
    target.append(row);
  });
}

/* 乙-2（#22）：书签动作状态行空文案时不占位（hidden 收起，写值即亮） */
function setBookmarkActionState(node, text) {
  if (!node) return;
  node.textContent = text;
  node.hidden = !text;
}

async function loadBookmarks(store, { preserveOnFailure = false } = {}) {
  const target = $("bookmark-list");
  const subId = String(store.activeLecture?.sub_id || "");
  resetBookmarkLoading();
  const epoch = bookmarkEpoch;
  target.dataset.subId = subId;
  const actionState = $("bookmark-action-state");
  setBookmarkActionState(actionState, "");
  if (!preserveOnFailure) {
    bookmarkItems = [];
    clear(target);
  }
  if (!subId) {
    bookmarkItems = [];
    target.append(textElement("p", "选择讲次后显示书签", "empty-state"));
    return true;
  }
  bookmarkController = new AbortController();
  if (!preserveOnFailure) target.append(textElement("p", "正在加载书签", "empty-state"));
  try {
    const value = await apiV3(`bookmarks?sub_id=${encodeURIComponent(subId)}`, { controller: bookmarkController });
    if (epoch !== bookmarkEpoch || String(store.activeLecture?.sub_id || "") !== subId) return false;
    bookmarkItems = sortBookmarks(value.bookmarks);
    renderBookmarks(target, bookmarkItems);
    return true;
  } catch (error) {
    if (error.name === "AbortError" || epoch !== bookmarkEpoch || String(store.activeLecture?.sub_id || "") !== subId) return false;
    if (preserveOnFailure) {
      setBookmarkActionState(actionState, "状态已更新，列表暂未刷新");
    } else {
      clear(target);
      /* EMPTY-STATES-1：错误空态补「重试」真动作（与失败前同一条装载链路）。 */
      emptyStateWithAction(target, "书签暂时无法加载", "重试", () => void loadBookmarks(store), "error-text");
    }
    return false;
  }
}

async function updateBookmarkReview(store, button) {
  const bookmarkId = String(button.dataset.bookmarkId || "");
  const action = String(button.dataset.bookmarkAction || "");
  const subId = String(store.activeLecture?.sub_id || "");
  if (button.disabled || !bookmarkId || !subId || $("bookmark-list").dataset.subId !== subId || !["resolve", "reopen"].includes(action)) return;
  const actionState = $("bookmark-action-state");
  const label = button.dataset.bookmarkLabel || button.textContent;
  const epoch = bookmarkActionEpoch;
  const actionController = new AbortController();
  bookmarkActionControllers.add(actionController);
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "处理中";
  setBookmarkActionState(actionState, "正在更新复习状态");
  try {
    const result = await postV3("bookmarks/actions", { bookmark_id: bookmarkId, action }, { controller: actionController });
    if (bookmarkActionIsCurrent(epoch, bookmarkActionEpoch, subId, store.activeLecture?.sub_id)) {
      const confirmed = confirmedBookmarkReview(result.bookmark, bookmarkId, subId, action);
      if (!confirmed) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = label;
        setBookmarkActionState(actionState, "状态可能已更新，请重新加载");
        return;
      }
      const existingIndex = bookmarkItems.findIndex((item) => String(item?.bookmark_id || "") === bookmarkId);
      bookmarkItems = sortBookmarks(existingIndex >= 0
        ? bookmarkItems.map((item, index) => index === existingIndex ? confirmed : item)
        : [...bookmarkItems, confirmed]);
      renderBookmarks($("bookmark-list"), bookmarkItems);
      setBookmarkActionState(actionState, "状态已更新");
      await loadBookmarks(store, { preserveOnFailure: true });
    }
  } catch (error) {
    if (error.name !== "AbortError" && bookmarkActionIsCurrent(epoch, bookmarkActionEpoch, subId, store.activeLecture?.sub_id)) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = label;
      setBookmarkActionState(actionState, "复习状态暂时无法更新");
    }
  } finally {
    bookmarkActionControllers.delete(actionController);
  }
}

/* PLAYER-UX-1④：行级删除（两步轻确认）。首击只进入「确认删除」武装态（4s
   回弹，防误删），再击才真删；成功后本地列表即时收敛并广播 bookmarks-changed
   （时间轴标记/assess 刻度由播放器侧监听同步消失）。 */
const BOOKMARK_DELETE_CONFIRM_MS = 4000;
const bookmarkDeleteArmTimers = new Map();

function resetBookmarkDeleteArms() {
  for (const timer of bookmarkDeleteArmTimers.values()) window.clearTimeout(timer);
  bookmarkDeleteArmTimers.clear();
}

function disarmBookmarkDelete(button) {
  const timer = bookmarkDeleteArmTimers.get(button);
  if (timer) {
    window.clearTimeout(timer);
    bookmarkDeleteArmTimers.delete(button);
  }
  button.classList.remove("confirming");
  button.textContent = "删除";
}

async function updateBookmarkDelete(store, button) {
  const bookmarkId = String(button.dataset.bookmarkId || "");
  const subId = String(store.activeLecture?.sub_id || "");
  if (button.disabled || !bookmarkId || !subId || $("bookmark-list").dataset.subId !== subId) return;
  const actionState = $("bookmark-action-state");
  if (!button.classList.contains("confirming")) {
    button.classList.add("confirming");
    button.textContent = "确认删除";
    const timer = window.setTimeout(() => disarmBookmarkDelete(button), BOOKMARK_DELETE_CONFIRM_MS);
    bookmarkDeleteArmTimers.set(button, timer);
    setBookmarkActionState(actionState, "再点一次确认删除；只是看错就直接忽略");
    return;
  }
  const epoch = bookmarkActionEpoch;
  const actionController = new AbortController();
  bookmarkActionControllers.add(actionController);
  button.disabled = true;
  button.removeAttribute("aria-busy");
  button.textContent = "正在删除";
  setBookmarkActionState(actionState, "正在删除这条书签");
  try {
    await deleteV3("bookmarks", { bookmark_id: bookmarkId }, { controller: actionController });
    if (bookmarkActionIsCurrent(epoch, bookmarkActionEpoch, subId, store.activeLecture?.sub_id)) {
      bookmarkDeleteArmTimers.delete(button);
      bookmarkItems = bookmarkItems.filter((item) => String(item?.bookmark_id || "") !== bookmarkId);
      renderBookmarks($("bookmark-list"), bookmarkItems);
      /* 先广播（播放器侧修剪时间轴标记+列表经事件链刷新），再落人话确认——
         loadBookmarks 同步清状态行，确认语必须等刷新收口后再写 */
      window.dispatchEvent(new Event("courselens:bookmarks-changed"));
      await loadBookmarks(store, { preserveOnFailure: true });
      setBookmarkActionState(actionState, "已删除这条书签");
    }
  } catch (error) {
    if (error.name !== "AbortError" && bookmarkActionIsCurrent(epoch, bookmarkActionEpoch, subId, store.activeLecture?.sub_id)) {
      disarmBookmarkDelete(button);
      button.disabled = false;
      setBookmarkActionState(actionState, error?.message || "书签暂时无法删除，稍后再试一次");
    }
  } finally {
    bookmarkActionControllers.delete(actionController);
  }
}

async function loadTranscript(store, instanceEpoch = studyInstanceEpoch) {
  const lecture = store.activeLecture;
  const target = $("transcript-list");
  const subId = String(lecture?.sub_id || "");
  resetTranscriptLoading();
  teardownTranscriptVirtual();
  const requestEpoch = transcriptEpoch;
  resetTranscriptFollow();
  /* 新讲次/重载即重置检索：命中视图属于旧文稿，绝不跨讲次残留 */
  resetTranscriptSearch();
  clear(target);
  /* P2-3（D14）：非行态一律摘除 list 语义——空段落/错误提示不挂进空列表 */
  target.removeAttribute("role");
  transcriptSubId = subId;
  if (!lecture) {
    store.set("transcriptHasTiming", false);
    target.append(textElement("p", "选择讲次后显示字幕", "empty-state"));
    return;
  }
  transcriptController = new AbortController();
  try {
    const value = await apiV3(`subtitles/segments?sub_id=${encodeURIComponent(subId)}`, { controller: transcriptController });
    const segments = value.segments || [];
    if (!isCurrentTranscriptLoad(requestEpoch, transcriptEpoch, subId, store.activeLecture?.sub_id, instanceEpoch, studyInstanceEpoch)) return;
    /* 行数据数组全量建（检索/跟随高亮的数据源零变化）；行元素按窗口惰性上屏 */
    const entries = segments.map((segment) => {
      const timing = transcriptTiming(segment);
      return {
        timing,
        startMs: timing ? timing.startMs : null,
        endMs: timing ? timing.endMs : null,
        row: null,
        text: String(segment.text || ""),
      };
    });
    const rows = entries.filter((entry) => entry.timing);
    transcriptRows = rows;
    transcriptSubId = subId;
    transcriptContext = { store, lecture };
    /* P2-3（D14）：容器 list 语义只随真实行态存在（纯 aria 属性，样式零变化） */
    if (segments.length) target.setAttribute("role", "list");
    else target.removeAttribute("role");
    if (entries.length <= TRANSCRIPT_CHUNK_ROWS * TRANSCRIPT_KEEP_CHUNKS) {
      /* 全量路径：小讲次 DOM 与既有行为逐字节一致（无垫片、无窗口状态） */
      const fragment = document.createDocumentFragment();
      if (!segments.length) fragment.append(textElement("p", "字幕尚未生成，生成后即可在这里边听边看", "empty-state"));
      entries.forEach((entry, index) => {
        entry.row = buildTranscriptRow(store, lecture, segments[index], entry);
        fragment.append(entry.row);
      });
      target.replaceChildren(fragment);
    } else {
      /* 窗口化路径（VTT-PERF）：视口±缓冲内的分块行建进 DOM，其余垫片占位 */
      const spacer = () => {
        const node = document.createElement("div");
        node.setAttribute("aria-hidden", "true");
        node.style.setProperty("width", "100%");
        return node;
      };
      const totalChunks = Math.ceil(entries.length / TRANSCRIPT_CHUNK_ROWS);
      const onScroll = () => transcriptReconcileWindow(store, lecture);
      transcriptVirtual = {
        container: target,
        segments,
        rows: entries,
        totalRows: entries.length,
        totalChunks,
        rendered: new Set(),
        estimates: new Array(totalChunks).fill(null),
        lo: 0,
        hi: -1,
        topSpacer: spacer(),
        bottomSpacer: spacer(),
        onScroll,
      };
      const fragment = document.createDocumentFragment();
      fragment.append(transcriptVirtual.topSpacer, transcriptVirtual.bottomSpacer);
      target.replaceChildren(fragment);
      transcriptApplyWindow(store, lecture, transcriptVirtual, 0, TRANSCRIPT_KEEP_CHUNKS - 1);
      document.addEventListener("scroll", onScroll, true);
      window.addEventListener("resize", onScroll);
    }
    store.set("transcriptHasTiming", rows.length > 0);
    /* 文稿落定后归位检索视图：加载期间输入的查询一并清空（新文稿新检索） */
    resetTranscriptSearch();
  } catch (error) {
    if (error.name !== "AbortError" && isCurrentTranscriptLoad(requestEpoch, transcriptEpoch, subId, store.activeLecture?.sub_id, instanceEpoch, studyInstanceEpoch)) {
      store.set("transcriptHasTiming", false);
      target.removeAttribute("role");
      target.replaceChildren(textElement("p", "字幕暂时无法加载", "empty-state error-text"));
      resetTranscriptSearch();
    }
  }
}

// ---- 课程目录（含恢复面板与防抖动） ----

let lastAuthFingerprint = "";
let lastCatalogFingerprint = "";
let lastLectureFingerprint = "";
let lastCatalogValue = null;
/* 当前渲染的讲次卡元数据（sub_id → lecture）：进度文案异步落位时重建行内副文本 */
const lectureCardMeta = new Map();
const COURSE_TERM_FILTER_KEY = "courselens.catalog-term.v1";
const COURSE_ORDER_KEY = "courselens.course-order.v1";
const CURRENT_TERM = "current";
const ALL_TERMS = "all";

/* ---- 课程拖拽排序（NIGHT2-W13）：只存本机视图顺序，不动目录真值 ---- */
/* 顺序存 localStorage（workbench 存储写入清单已同步钉住）；新课程按目录
   原序追加，消失的课程自动遗忘。键盘=行内按钮 Alt+↑/↓；拖拽=行壳
   draggable；重排带 FLIP 动画（真实 DOM 且未开启「减少动态效果」时）。 */
function loadCourseOrder() {
  try {
    const value = JSON.parse(localStorage.getItem(COURSE_ORDER_KEY) || "[]");
    return Array.isArray(value) ? value.map((item) => String(item || "")).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function saveCourseOrder(order) {
  try {
    localStorage.setItem(COURSE_ORDER_KEY, JSON.stringify(order));
  } catch {}
}

function orderedCourses(courses) {
  const saved = loadCourseOrder();
  if (!saved.length) return courses;
  const rank = new Map(saved.map((courseId, index) => [courseId, index]));
  return [...courses].sort((a, b) => {
    const ra = rank.get(String(a.course_id || ""));
    const rb = rank.get(String(b.course_id || ""));
    return (ra === undefined ? Number.MAX_SAFE_INTEGER : ra) - (rb === undefined ? Number.MAX_SAFE_INTEGER : rb);
  });
}

function courseRowIds(container) {
  return [...container.querySelectorAll(".course-row")]
    .map((row) => row.querySelector(".course-row-main")?.dataset.courseId)
    .filter(Boolean);
}

function flipCourseRows(container, mutate) {
  const rows = [...container.querySelectorAll(".course-row")];
  const before = new Map(rows.map((row) => [row, row.getBoundingClientRect ? row.getBoundingClientRect() : null]));
  mutate();
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  rows.forEach((row) => {
    const firstRect = before.get(row);
    const lastRect = row.getBoundingClientRect ? row.getBoundingClientRect() : null;
    if (!firstRect || !lastRect || typeof row.animate !== "function") return;
    const dy = firstRect.top - lastRect.top;
    if (!dy) return;
    row.animate(
      [{ transform: `translateY(${dy}px)` }, { transform: "translateY(0)" }],
      { duration: 180, easing: "ease-out" },
    );
  });
}

function persistCourseOrder(container) {
  saveCourseOrder(courseRowIds(container));
}

function moveCourseRow(container, row, offset) {
  const sibling = offset < 0 ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling || !String(sibling.className || "").split(/\s+/).includes("course-row")) return;
  flipCourseRows(container, () => {
    container.insertBefore(row, offset < 0 ? sibling : sibling.nextElementSibling);
  });
  persistCourseOrder(container);
}

let draggedCourseRow = null;
let courseTermFilter = CURRENT_TERM;

export function currentCourseTerm(courses, now = new Date()) {
  const year = now.getFullYear();
  const academicStart = now.getMonth() >= 7 ? year : year - 1;
  const semester = now.getMonth() >= 7 || now.getMonth() === 0 ? "1" : "2";
  const expected = `${academicStart}-${academicStart + 1}`;
  return (courses || []).map((course) => String(course?.term || "")).find((term) => {
    const normalized = term.replace(/[–—]/g, "-").replace(/\s+/g, "");
    return normalized.includes(expected) && (normalized === `${expected}${semester}`
      || new RegExp(`(?:-|学年)${semester}(?:学期)?$`).test(normalized)
      || (semester === "1" ? /秋/.test(normalized) : /春/.test(normalized)));
  }) || "";
}

export function filterCoursesByTerm(courses, selection, now = new Date()) {
  const list = Array.isArray(courses) ? courses : [];
  if (selection === ALL_TERMS) return list;
  const term = selection === CURRENT_TERM ? currentCourseTerm(list, now) : selection;
  return term ? list.filter((course) => String(course?.term || "") === term) : [];
}

function renderCourseTermFilter(courses) {
  const select = $("catalog-term-filter");
  const terms = [...new Set((courses || []).map((course) => String(course?.term || "")).filter(Boolean))];
  if (![CURRENT_TERM, ALL_TERMS, ...terms].includes(courseTermFilter)) courseTermFilter = CURRENT_TERM;
  const signature = JSON.stringify(terms);
  if (select.dataset.signature !== signature) {
    clear(select);
    select.append(new Option("当前学期", CURRENT_TERM), new Option("全部学期", ALL_TERMS));
    /* U⑬：下拉展示人话学期，值仍是原始串（过滤键不变） */
    terms.forEach((term) => select.append(new Option(friendlyTerm(term), term)));
    select.dataset.signature = signature;
  }
  select.value = courseTermFilter;
  /* WP1-D1：程序化设值不派发 change——目录学期筛选触发钮要显式同步文案
     （player-core.js 同款先例口径），否则初始只显示箭头没有当前值。 */
  syncDropdown(select);
}
let latestCatalogDiagnostics = null;
const CATALOG_REFRESH_INTERVAL_MS = 1000;
/* 初始请求后每秒至多重试 N 次（总请求 = 1 + N）：第 N 次重试仍 checking/refreshing 才降态 */
const CATALOG_REFRESH_MAX_RETRIES = 20;
/* O3-F4（N6F 移交）：单次目录 fetch 死线 20s——fetch 永挂起时不再静默等待，
   超时异常走既有 catalogRefreshTimedOut 收口（TimeoutError ≠ AbortError）。 */
const CATALOG_FETCH_DEADLINE_MS = 20000;
/* WAIT-UX-1：等待进度行步进节拍（秒级已用时间；诚实动态，不发明预计） */
const QUIZ_STATUS_TICK_MS = 1000;

const RECOVERY_ACTIONS = new Set(["login", "refresh-catalog", "diagnose-network"]);

/* EMPTY-STATES-1：ready 真空目录的空态句。单列常量供渲染处判别「这句要带动作钮」。 */
const CATALOG_EMPTY_READY_TEXT = "暂无可显示课程，可尝试刷新目录。";

/* EMPTY-STATES-1：空态统一形态=一句话+动作钮（components.css .empty-state/.empty-action
   既有族，学习签名面 div>p+钮 同款），零新增设计语言。动作只挂真实可达链路，绝不造死按钮。
   extraClass 沿用既有 error-text（错误空态保持 danger 墨色，视觉合同不变）。 */
function emptyStateWithAction(target, message, actionLabel, onAction, extraClass = "") {
  const box = document.createElement("div");
  box.className = extraClass ? `empty-state ${extraClass}` : "empty-state";
  box.append(textElement("p", message));
  const action = document.createElement("button");
  action.type = "button";
  action.className = "empty-action";
  action.textContent = actionLabel;
  action.addEventListener("click", onAction);
  box.append(action);
  target.append(box);
}

/* WAIT-UX-1（零呆等三律）：等待状态行动态元素的唯一创建口。形态族=既有
   .hint + role=status + aria-live=polite（courseware-pdf-state 同款），
   零新增设计语言。真 DOM 插在锚点之前作兄弟节点（不被列表重渲染清掉）；
   桩测试假 DOM 无 parentNode 时退化为挂进 fallbackHost（仅测试路径，
   该宿主须是不会被整体重建的稳定 id 节点）。 */
function ensureWaitStatusLine(id, anchorNode, fallbackHost) {
  let node = $(id);
  if (!node) {
    node = document.createElement("p");
    node.id = id;
    node.className = "hint";
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    const parent = anchorNode?.parentNode;
    if (parent && typeof parent.insertBefore === "function") parent.insertBefore(node, anchorNode);
    else if (fallbackHost && typeof fallbackHost.append === "function") fallbackHost.append(node);
  }
  return node;
}

function emptyCatalogMessage(value) {
  if (value.refreshing) {
    /* AS4-U1：恢复期空缓存=还没登录上，不是目录为空——文案说清「登录后显示」；
       ready 态刷新窗仍用「正在确认课程授权」，两种在途不混用。 */
    return String(value.code || "") === "fudan_session_checking"
      ? "正在登录复旦账号，课程将在登录完成后显示。"
      : "正在确认课程授权";
  }
  if (value.state !== "ready") return "课程目录恢复后将在这里显示。";
  return CATALOG_EMPTY_READY_TEXT;
}

function diagnosticValue(value) {
  return {
    state: String(value?.state || "unknown"),
    code: String(value?.code || "unknown"),
    actions: (value?.actions || []).filter((action) => RECOVERY_ACTIONS.has(action)),
  };
}

function performRecoveryAction(action) {
  if (action === "login") {
    window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "reauth" }));
    return;
  }
  if (action === "refresh-catalog") {
    $("refresh-catalog").click();
    return;
  }
  if (action === "diagnose-network") {
    window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "settings" }));
    window.setTimeout(() => $("diagnose-network")?.click(), 0);
  }
}

function renderRecovery(value, { loggedIn = null } = {}) {
  const panel = $("catalog-recovery");
  /* N5FE-P2 尾巴：会话真值随行——未登录时目录码不再宣称「登录仍然有效」 */
  const details = evidenceDetails(value, { loggedIn });
  const actions = (value?.actions || []).filter((action) => RECOVERY_ACTIONS.has(action));
  const visible = value?.state !== "ready" || String(value?.code || "") === "authorized_catalog_stale";
  $("catalog-evidence").textContent = `${details.title}${value?.course_count == null ? "" : ` · ${value.course_count} 门课程`}`;
  panel.hidden = !visible;
  panel.dataset.state = value?.state || "unknown";
  $("catalog-recovery-title").textContent = details.title;
  $("catalog-recovery-impact").textContent = details.impact;
  latestCatalogDiagnostics = diagnosticValue(value);
  $("catalog-diagnostic-code").textContent = [
    `state: ${latestCatalogDiagnostics.state}`,
    `code: ${latestCatalogDiagnostics.code}`,
    `actions: ${latestCatalogDiagnostics.actions.join(", ") || "none"}`,
  ].join("\n");
  clear($("catalog-recovery-actions"));
  actions.forEach((action) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = recoveryActionLabel(action, value);
    button.addEventListener("click", () => performRecoveryAction(action));
    $("catalog-recovery-actions").append(button);
  });
}

// ---- 讲次观看进度（/api/v3/progress contract 的只读消费端）----
/* 讲次卡显示简洁可扫描的观看进度：按 sub_id GET 一次并缓存；播放器每次
   保存成功广播 courselens:watch-progress，卡片即时更新且零新增请求。
   读取失败/错讲次/跨课程/零值行一律诚实降级为无进度文案，绝不串讲次。 */
const watchProgressBySubId = new Map();
const watchProgressInFlight = new Set();
let watchProgressAuthEpoch = 0;

/* 登出/账号切换（auth 非 ready 的确认态）即清空进度缓存：sub_id 跨账号稳定，
   残留会让新账号直接命中旧进度且无重取路径；在途迟到的旧账号响应一并作废。 */
function resetWatchProgressCache() {
  watchProgressAuthEpoch += 1;
  watchProgressBySubId.clear();
  watchProgressInFlight.clear();
}

function watchClock(seconds) {
  const total = Math.floor(Number(seconds) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}`
    : `${minutes}:${secs}`;
}

function watchProgressText(row) {
  if (!row || typeof row !== "object") return "";
  const position = Number(row.position_seconds);
  if (!Number.isFinite(position) || position <= 0) return "";
  if (row.completed === true) return "已看完";
  const duration = Number(row.duration_seconds);
  if (Number.isFinite(duration) && duration > 0) {
    if (position >= duration) return "已看完";
    return `看到 ${Math.min(100, Math.max(1, Math.floor((position / duration) * 100)))}%`;
  }
  return `看到 ${watchClock(position)}`;
}

function lectureRowSubText(lecture) {
  const progress = watchProgressText(watchProgressBySubId.get(String(lecture.sub_id || "")));
  return [lecture.date, progress, lecture.can_stream ? "" : "需要登录"].filter(Boolean).join(" · ");
}

function applyLectureProgressText(subId) {
  const rows = $("study-lecture-list")?.querySelectorAll?.(".lecture-row") || [];
  for (const row of rows) {
    if (String(row.dataset?.subId || "") !== String(subId)) continue;
    const span = row.querySelector("span");
    const lecture = lectureCardMeta.get(String(subId));
    if (span && lecture) span.textContent = lectureRowSubText(lecture);
    return;
  }
}

function hydrateLectureProgress(course) {
  for (const lecture of course?.lectures || []) {
    const subId = String(lecture.sub_id || "");
    if (!subId) continue;
    lectureCardMeta.set(subId, lecture);
    if (watchProgressBySubId.has(subId) || watchProgressInFlight.has(subId)) {
      applyLectureProgressText(subId);
      continue;
    }
    watchProgressInFlight.add(subId);
    const requestedAuthEpoch = watchProgressAuthEpoch;
    apiV3(`progress?sub_id=${encodeURIComponent(subId)}`).then((value) => {
      watchProgressInFlight.delete(subId);
      if (requestedAuthEpoch !== watchProgressAuthEpoch) return; /* 身份已重置：迟到响应不回填 */
      const row = value?.progress && typeof value.progress === "object" ? value.progress : null;
      if (!row || String(row.sub_id || "") !== subId) return;
      if (lecture.course_id && row.course_id
        && String(row.course_id) !== String(lecture.course_id)) return;
      watchProgressBySubId.set(subId, row);
      applyLectureProgressText(subId);
    }).catch(() => {
      watchProgressInFlight.delete(subId);
    });
  }
}

function setPressed(container, selector, selectedId, key) {
  container.querySelectorAll(selector).forEach((node) => {
    const selected = String(node.dataset[key] || "") === String(selectedId || "");
    node.classList.toggle("active", selected);
    if (selected) node.setAttribute("aria-current", "true");
    else node.removeAttribute("aria-current");
  });
}

function renderLectures(store, course) {
  const target = $("study-lecture-list");
  const selectedSubId = store.activeLecture?.course_id === course?.course_id ? store.activeLecture?.sub_id : "";
  const fingerprint = JSON.stringify({
    courseId: course?.course_id || "",
    title: course?.title || "",
    teacher: course?.teacher || "",
    term: course?.term || "",
    department: course?.department || "",
    lectures: course?.lectures || [],
  });
  if (fingerprint === lastLectureFingerprint) {
    setPressed(target, ".lecture-row", selectedSubId, "subId");
    return;
  }
  lastLectureFingerprint = fingerprint;
  const focusedSubId = document.activeElement?.closest?.("[data-sub-id]")?.dataset.subId || "";
  clear(target);
  $("study-course-title").textContent = course?.title || "选择课程";
  $("study-course-meta").textContent = [course?.teacher, friendlyTerm(course?.term), course?.department].filter(Boolean).join(" · ");
  /* N7F：唯一的课程级「总体复习」入口 —— 选中课程才出现；课次卡不新增入口排。
     元素缺失时静默跳过（精简夹具的 mjs 不带本节点）。 */
  const reviewEntry = $("course-review-open");
  if (reviewEntry) reviewEntry.hidden = !course?.course_id;
  const lectures = course?.lectures || [];
  if (!lectures.length) {
    /* 「暂无已授权讲次」只属于真实选中的课程；未选课程时给选择引导，不得暗示讲次为零。
       EMPTY-STATES-1：选了课程却没有讲次时，给「刷新课程目录」真动作（与目录刷新同链路）。 */
    if (course) {
      emptyStateWithAction(
        target,
        "暂无已授权讲次",
        "刷新课程目录",
        () => performRecoveryAction("refresh-catalog"),
      );
    } else {
      target.append(textElement("p", "选择课程后显示讲次", "empty-state"));
    }
    store.set("activeLecture", null);
    return;
  }
  lectures.forEach((lecture) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lecture-row";
    button.dataset.subId = String(lecture.sub_id || "");
    button.setAttribute("aria-controls", "player-stage");
    button.append(
      textElement("strong", lecture.sub_title || "未命名讲次"),
      textElement("span", lectureRowSubText(lecture)),
    );
    button.addEventListener("click", () => {
      setPressed(target, ".lecture-row", lecture.sub_id, "subId");
      const active = store.activeLecture;
      /* 同讲次重点不重发全量 loadAll（9 请求重发 + abort 在途轮询）；其余点击照常切换。
         选择面上的点击永远是显式进入：退出选择态后重进同讲次必须响应（M14）。 */
      if (active && String(active.sub_id || "") === String(lecture.sub_id || "")
        && String(active.course_id || "") === String(course.course_id)
        && modeOverride !== "select") return;
      store.set("activeLecture", { ...lecture, course_id: course.course_id, course_title: course.title });
    });
    target.append(button);
  });
  hydrateLectureProgress(course);
  setPressed(target, ".lecture-row", selectedSubId, "subId");
  if (focusedSubId) {
    target.querySelector(`[data-sub-id="${CSS.escape(String(focusedSubId))}"]`)?.focus({ preventScroll: true });
  }
}

export function renderCourses(store, value) {
  const target = $("study-course-list");
  /* 瞬时 checking 且载荷不带课程时，保留内存中同身份已验证列表（绝不入库/落盘）；
     确认失败（action_required/degraded）载荷原样渲染，课程立即清空。 */
  const rendered = String(value?.state || "") === "checking" && !(value?.courses || []).length && store.courses.length
    ? { ...value, courses: store.courses, course_count: store.courses.length }
    : value;
  lastCatalogValue = rendered;
  renderRecovery(rendered, {
    loggedIn: store.auth?.state === "ready" ? true : (store.auth?.state ? false : null),
  });
  const fingerprint = JSON.stringify({
    state: rendered?.state || "unknown",
    code: rendered?.code || "",
    actions: rendered?.actions || [],
    courses: rendered?.courses || [],
  });
  if (fingerprint === lastCatalogFingerprint) return;
  lastCatalogFingerprint = fingerprint;
  const focusedCourseId = document.activeElement?.closest?.("[data-course-id]")?.dataset.courseId || "";
  target.setAttribute("aria-busy", "true");
  clear(target);
  const allCourses = rendered.courses || [];
  let courses = filterCoursesByTerm(allCourses, courseTermFilter);
  /* 视图过滤未命中≠目录为空：非空目录在所选/当前学期无行时，可见地回落全部学期并保留
     选课与讲次状态；回落只改内存视图态（select 同步显示全部学期），绝不写入 localStorage。 */
  if (!courses.length && allCourses.length && courseTermFilter !== ALL_TERMS) {
    courseTermFilter = ALL_TERMS;
    courses = filterCoursesByTerm(allCourses, courseTermFilter);
  }
  renderCourseTermFilter(allCourses);
  courses = orderedCourses(courses);
  store.set("courses", allCourses);
  /* O3-F3（N6F 移交）：计数随所见——学期筛选使可见行少于全量时，
     证据行如实补「当前显示 M 门」；renderRecovery 每轮重置基线，幂等。 */
  if (value?.course_count != null && courses.length !== value.course_count) {
    const countNode = $("catalog-evidence");
    if (countNode) countNode.textContent = `${countNode.textContent} · 当前显示 ${courses.length} 门`;
  }
  if (!courses.length) {
    /* 走到这里只剩真实已验证空目录（allCourses 为空）：维持既有清空语义。
       NAV-HANG-1④：非 ready 瞬态且本地凭据仍在（上游抖动+空缓存）不清选择
       ——恢复后选择面原样回来；ready 空目录或凭据已消失（显式登出/切号）
       照旧清空（隐私红线）。legacy 调用（无 state/configured）视同 ready。 */
    const catalogEmptyMessage = emptyCatalogMessage(rendered);
    if (catalogEmptyMessage === CATALOG_EMPTY_READY_TEXT) {
      /* EMPTY-STATES-1：ready 真空目录给可点的下一步（与右上「刷新」同一链路）；
         非 ready 态的下一步在同屏恢复面板（login/refresh/diagnose），不重复摆钮。 */
      emptyStateWithAction(
        target,
        catalogEmptyMessage,
        "刷新课程目录",
        () => performRecoveryAction("refresh-catalog"),
      );
    } else {
      target.append(textElement("p", catalogEmptyMessage, "empty-state"));
    }
    const transientFlap = String(rendered?.state || "ready") !== "ready" && rendered?.configured === true;
    if (!transientFlap) {
      store.set("activeCourse", null);
      store.set("activeLecture", null);
      renderLectures(store, null);
    }
    target.setAttribute("aria-busy", "false");
    target.removeAttribute("role"); /* P2-3（D14）：空目录不挂空列表语义 */
    return;
  }
  /* P2-3（D14）：课程列表容器 list 语义 + 行 listitem（纯 aria，样式零变化）；
     P2-2（D14）：键盘排序能力可见化——行 ≥2 时列表头部给一行快捷键提示
     （文案取 api.js 闭集表），课程钮 title 同文案，读屏与纯键盘学生可发现；
     P2-2 残余（A11Y-IMPL-5）：draggable 行同门控挂 aria-roledescription 行述。 */
  target.setAttribute("role", "list");
  const orderHint = UI_HINTS.course_order_shortcut;
  if (courses.length > 1) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.id = "course-order-hint";
    hint.textContent = orderHint;
    target.append(hint);
  }
  courses.forEach((course) => {
    /* 行壳（div，非交互）+ 两个兄弟控件：课程导航按钮与课程自动整理开关。
       按钮永不嵌套按钮；aria-current 落在真正的导航按钮上。 */
    const row = document.createElement("div");
    row.className = "course-row";
    row.setAttribute("role", "listitem"); /* P2-3（D14）：行语义 */
    /* P2-2 残余（D14/A11Y-IMPL-5）：行 >1 才有可排序语义——与排序提示同门控，
       单行不挂；文案取 api.js 闭集表，读屏行述不再让 draggable 语义不可见。 */
    if (courses.length > 1) row.setAttribute("aria-roledescription", UI_HINTS.course_row_reorderable);
    row.draggable = true;
    row.addEventListener("dragstart", (event) => {
      draggedCourseRow = row;
      row.classList.add("dragging");
      if (event?.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        try { event.dataTransfer.setData("text/plain", String(course.course_id || "")); } catch {}
      }
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      if (draggedCourseRow === row) draggedCourseRow = null;
    });
    row.addEventListener("dragover", (event) => {
      if (draggedCourseRow && draggedCourseRow !== row) event?.preventDefault?.();
    });
    row.addEventListener("drop", (event) => {
      event?.preventDefault?.();
      if (!draggedCourseRow || draggedCourseRow === row) return;
      const source = draggedCourseRow;
      draggedCourseRow = null;
      source.classList.remove("dragging");
      const rows = [...target.querySelectorAll(".course-row")];
      const from = rows.indexOf(source);
      const to = rows.indexOf(row);
      if (from < 0 || to < 0 || from === to) return;
      flipCourseRows(target, () => {
        if (from < to) target.insertBefore(source, row.nextElementSibling);
        else target.insertBefore(source, row);
      });
      persistCourseOrder(target);
    });
    const button = document.createElement("button");
    button.type = "button";
    button.className = "course-row-main";
    button.dataset.courseId = String(course.course_id || "");
    button.setAttribute("aria-controls", "study-lecture-list");
    if (courses.length > 1) button.title = orderHint; /* P2-2（D14）：同文案 */
    button.addEventListener("keydown", (event) => {
      const key = String(event?.key || "");
      if (!event?.altKey || (key !== "ArrowUp" && key !== "ArrowDown")) return;
      event.preventDefault();
      moveCourseRow(target, row, key === "ArrowUp" ? -1 : 1);
    });
    button.append(
      textElement("strong", course.title || `课程 ${course.course_id}`),
      textElement("span", [course.teacher, friendlyTerm(course.term), `${(course.lectures || []).length} 讲`].filter(Boolean).join(" · ")),
    );
    button.addEventListener("click", () => {
      studyLandingNavigated = true; /* 显式选课：解除启动落地门 */
      setPressed(target, ".course-row-main", course.course_id, "courseId");
      /* D-20261009-03 复点补链：先判重再写——重复点选当前已选课程时 store.set
         同引用去重（store.js PF1），订阅面不通知，直播卡就地失去重查机会；
         显式再点仍是学生的「就它了/再确认一次」手势，此时补发 live-refresh
         让学习页直播卡（与主页卡/直播页既有监听面）重查当前课程。换课程路径
         set 已通知订阅正常刷新，不补发（避免双请求）。 */
      const alreadyActive = store.activeCourse === course;
      store.set("activeCourse", course);
      if (alreadyActive) {
        window.dispatchEvent(new CustomEvent("courselens:live-refresh"));
      }
      courseChosen = true;
      renderSelectLayout();
      renderLectures(store, course);
    });
    row.append(button, courseAutomationToggle(course));
    target.append(row);
  });
  storeCoursesForToggles = courses;
  refreshAutomationToggles();
  maybeAskCourseAutomation(courses);
  const previous = courses.find((item) => item.course_id === store.activeCourse?.course_id);
  /* 学期过滤把原 active course 滤掉时，回退高亮第一门课，但必须同步清除旧讲次：
     否则课程列表高亮与学习桌/面包屑的播放目标指向不同课程（ghost playback target）。 */
  if (!previous && store.activeLecture) store.set("activeLecture", null);
  const selected = previous || courses[0];
  setPressed(target, ".course-row-main", selected.course_id, "courseId");
  /* 目录渲染的自动选中是内存高亮语义，不是显式导航：不解除启动落地门 */
  autoSelectingCatalog = true;
  try {
    store.set("activeCourse", selected);
  } finally {
    autoSelectingCatalog = false;
  }
  renderLectures(store, selected);
  target.setAttribute("aria-busy", "false");
  if (focusedCourseId) {
    target.querySelector(`[data-course-id="${CSS.escape(String(focusedCourseId))}"]`)?.focus({ preventScroll: true });
  }
}

// ---- 课程自动整理开关（cloud-automation.v3 目录入口）----
/* 课程目录是唯一的按课程自动整理入口：点击直接走既有保存链（披露记录版本
   随链上传），不再弹隐私对话框；失败闭集文案 + 重试落在触发行内。
   状态闭集：off / saving / on / attention / unavailable。快照由本模块唯一
   轮询（refreshAutomationSnapshot）并经共享 store 发布，订阅方只读。 */

let automationSnapshot = null;
let automationSavingCourseId = "";
let automationAttention = ""; /* 最近一次保存失败的 course_id（attention 态） */
let automationAttentionCode = "";
let automationSaveSeq = 0; /* 陈旧响应保护：只有最新一次点击可以落状态 */

const COURSE_AUTOMATION_ERROR_TEXT = Object.freeze({
  cloud_catalog_unavailable: "课程目录尚未确认，无法建立选择基线。请先确认课程目录后重试。",
  cloud_course_not_verified: "该课程尚未通过授权确认，暂不能开启自动整理。",
  cloud_baseline_too_large: "该课程可选讲次超出云端上限，无法开启自动整理。",
  cloud_budget_invalid: "自动化预算参数超出允许范围，暂无法保存；请稍后重试，若持续出现请更新客户端。",
  cloud_rule_invalid: "课程选择未被接受，请稍后重试。",
  cloud_account_missing: "尚无已保存账号。请先在「设置 → 账户与连接」保存账号。",
  cloud_account_required: "请先在「设置 → 账户与连接」选择唯一的已保存账号，再开启自动整理。",
  cloud_account_rotation_required: "该账号的已保存密码需要更新，更新后才能开启自动整理。",
  cloud_disclosure_required: "需要先确认加密托管说明。",
  cloud_secret_upload_incomplete: "GitHub 未确认全部托管条目，请稍后重试。",
  cloud_config_incomplete: "托管配置未完成，请稍后重试。",
  deepseek_key_missing: "自动整理需要 DeepSeek API Key。请先在「设置 → AI 与处理」保存。",
  worker_tree_drifted: "Worker 版本发生变化，请先在「设置 → 网络与远程连接」完成修复。",
  cloud_binding_invalid: "托管绑定已失效（账号或 Worker 变化），请重新确认托管。",
  cloud_verification_failed: "云端验证未通过，请稍后重试。",
  cloud_verification_required: "云端托管配置尚未通过验证，请稍等片刻后再点一次本课程的云开关重新验证。",
  cloud_verification_timeout: "云端验证还在进行中，暂时没等到结果。请等一两分钟后再点一次本课程的云开关。",
  github_unreachable: "暂时无法连接 GitHub，请稍后重试。",
  rate_limited: "GitHub 请求受限，请稍后重试。",
  timeout: "请求超时，请稍后重试。",
  network_unavailable: "网络暂时不可用，请稍后重试。",
  cloud_run_cancel_pending: "云端任务仍在停止中，请稍后重试。",
  /* W1 补全：后端 automation 面已有的六个闭集码——此前落 generic 兜底，
     现在给出可行动文案（不出现原始错误文本与任何账号/仓库标识） */
  automation_action_invalid: "自动整理操作未被接受。请刷新页面后重试；若持续出现，请更新客户端。",
  operation_id_invalid: "本次操作未被接受，请重试一次；若持续出现，请更新客户端。",
  cloud_artifact_empty: "云端返回的学习材料为空。请重新运行一次自动整理。",
  cloud_result_invalid: "云端产物未通过完整性校验。请重新运行一次自动整理；若反复出现，请更新客户端。",
  cloud_result_key_missing: "云端结果暂时无法取回。请稍后重新运行一次自动整理。",
  cloud_cleanup_pending: "云端仍有待清理的旧数据。请稍后重试，清理完成后即可正常使用。",
  automation_failed: "自动整理执行失败，请稍后重试。",
  http_error: "服务暂时无法连接，请稍后重试。",
});
const COURSE_AUTOMATION_ERROR_GENERIC = "自动整理设置暂未保存，请稍后重试。";

/* ---- U⑦（用户拍板）：新课程云端自动处理一次性询问。
   隐私边界（场景 5 守卫）：课程身份绝不落 localStorage——询问记忆只在
   本页面会话内存里。seed=本会话第一次非空目录（升级用户/重开应用不问
   存量课）；只对「本会话中新出现」的课程问一次；会话内恰一次，答过的
   不再问。设置页/目录开关可随时改。 ---- */
const autoAskSeen = new Set();
const autoAskAsked = new Set();
let autoAskSeeded = false;

function maybeAskCourseAutomation(courses) {
  const target = $("study-course-list");
  if (!target || !Array.isArray(courses) || !courses.length) return;
  target.querySelector(".course-auto-ask")?.remove();
  if (!autoAskSeeded) {
    /* 种子：本会话首次目录视为存量，不问 */
    for (const course of courses) autoAskSeen.add(String(course?.course_id || ""));
    autoAskSeeded = true;
    return;
  }
  const pending = courses.find((course) => {
    const id = String(course?.course_id || "");
    return id && !autoAskSeen.has(id) && !autoAskAsked.has(id) && courseAutomationState(id) === "off";
  });
  if (!pending) return;
  const pendingId = String(pending.course_id || "");
  autoAskSeen.add(pendingId);
  autoAskAsked.add(pendingId);
  const card = document.createElement("div");
  card.className = "course-auto-ask";
  card.append(textElement("p", `要为《${pending.title || pending.course_id}》开启云端自动处理吗？`));
  const hint = textElement(
    "p",
    "开启后新讲次会自动生成字幕和笔记：用的是你自己的 DeepSeek 额度，数据只进你的私有仓。",
  );
  hint.className = "hint";
  card.append(hint);
  const row = document.createElement("div");
  row.className = "course-auto-ask-actions";
  const yes = document.createElement("button");
  yes.type = "button";
  yes.textContent = "开启";
  yes.addEventListener("click", () => {
    card.remove();
    requestCourseAutomation(pending);
  });
  const no = document.createElement("button");
  no.type = "button";
  no.className = "btn-quiet";
  no.textContent = "不用了";
  no.addEventListener("click", () => {
    card.remove();
  });
  row.append(yes, no);
  card.append(row);
  target.prepend(card);
}

function courseAutomationAnnounce(text) {
  const region = $("course-automation-live");
  if (region) region.textContent = text;
}

function courseAutomationState(courseId) {
  if (!automationSnapshot) return "unavailable";
  if (automationSavingCourseId === courseId) return "saving";
  if (automationAttention === courseId) return "attention";
  const selected = Boolean((automationSnapshot.rules || []).some(
    (rule) => String(rule.course_id || "") === String(courseId),
  ));
  return selected ? "on" : "off";
}

function courseAutomationToggleLabel(course, state) {
  const title = course.title || `课程 ${course.course_id}`;
  const map = {
    on: `自动整理已开启：${title}`,
    off: `开启自动整理：${title}`,
    saving: `正在保存自动整理设置：${title}`,
    attention: `自动整理需要处理：${title}`,
    unavailable: `自动整理暂不可用：${title}`,
  };
  return map[state] || map.off;
}

function renderCourseAutomationToggle(button, course) {
  const state = courseAutomationState(course.course_id);
  button.dataset.state = state;
  button.setAttribute("aria-pressed", state === "on" ? "true" : "false");
  button.setAttribute("aria-label", courseAutomationToggleLabel(course, state));
  /* A11：纯视觉圆圈对 sighted 学生不可发现；title 与 aria-label 同源同态 */
  button.title = courseAutomationToggleLabel(course, state);
  if (state === "saving") button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
  button.disabled = state === "saving" || state === "unavailable";
  const mark = button.querySelector(".course-automation-mark");
  if (mark) mark.textContent = state === "on" ? "✓" : state === "attention" ? "!" : "";
}

/* 行内开关只负责渲染与点击路由；真实状态永远来自快照回读（陈旧响应不落状态）。
   可见方块无汉字：完整语义在 aria-label/aria-pressed，填充与记号只作视觉冗余。 */
function courseAutomationToggle(course) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "course-automation-toggle";
  button.dataset.courseId = String(course.course_id || "");
  const box = document.createElement("span");
  box.className = "course-automation-box";
  box.setAttribute("aria-hidden", "true");
  const mark = document.createElement("span");
  mark.className = "course-automation-mark";
  box.append(mark);
  button.append(box);
  renderCourseAutomationToggle(button, course);
  button.addEventListener("click", () => requestCourseAutomation(course));
  return button;
}

/* 保存链（与设置页同一 API 面，不发明第二接口）：
   规则合并 → 加密上传（既有披露记录版本）→ 云端验证 → 启用；
   关闭到空选择时以 disable-cloud 收口。任一步失败 → attention，快照回读为准。 */
async function runCourseAutomationChain(accountId, courseIds) {
  const disclosureVersion = String(automationSnapshot?.disclosure_version || "cloud-custody-disclosure.v1");
  const rules = courseIds.map((courseId) => ({ course_id: String(courseId) }));
  await putV3("automation/config", { config: { account_id: accountId, rules }, operation_id: operationId("course-auto-cfg") });
  if (rules.length) {
    /* 云端加密上传与保存配置同一 PUT 幂等通道：必带闭集形状 operation_id
       （⑦ 真相：后端只在 PUT 分支实现本路由，POST 直落 404） */
    await putV3("automation/cloud-secrets", {
      account_id: accountId,
      disclosure_version: disclosureVersion,
      confirmed: true,
      operation_id: operationId("cloud-secrets"),
    });
    await postV3("automation/actions", { action: "verify-cloud-credentials", operation_id: operationId("course-auto-v") });
    await postV3("automation/actions", { action: "enable-cloud", operation_id: operationId("course-auto-e") });
  } else {
    await postV3("automation/actions", { action: "disable-cloud", operation_id: operationId("course-auto-d") });
  }
}

/* 快照未绑定账号时按既有「恰一个可用账号预选」规则就地解析；
   零个或多个可用账号不猜测：空账号交由后端闭集失败 → attention 行内指引。 */
async function resolveAutomationAccountId() {
  const snapshotAccountId = String(automationSnapshot?.account_id || "");
  if (snapshotAccountId) return snapshotAccountId;
  try {
    const value = await apiV3("accounts");
    const usable = (Array.isArray(value?.accounts) ? value.accounts : [])
      .filter((item) => !item.requires_rotation);
    return usable.length === 1 ? String(usable[0].student_id || "") : "";
  } catch {
    return "";
  }
}

async function runCourseAutomationOnce(course, enable) {
  const courseId = String(course.course_id || "");
  if (!automationSnapshot || !courseId) return;
  const seq = ++automationSaveSeq;
  const existing = (automationSnapshot.rules || [])
    .map((rule) => String(rule.course_id || ""))
    .filter((id) => id && id !== courseId);
  const courseIds = enable ? [...existing, courseId] : existing;
  automationSavingCourseId = courseId;
  automationAttention = "";
  automationAttentionCode = "";
  refreshAutomationToggles();
  courseAutomationAnnounce(`${enable ? "正在开启" : "正在关闭"}自动整理：${course.title || courseId}`);
  try {
    const accountId = await resolveAutomationAccountId();
    await runCourseAutomationChain(accountId, courseIds);
    if (seq !== automationSaveSeq) return; /* 已有更新的点击：丢弃陈旧结果 */
    automationSavingCourseId = "";
    await refreshAutomationSnapshot();
    if (seq !== automationSaveSeq) return;
    courseAutomationAnnounce(`自动整理已${enable ? "开启" : "关闭"}：${course.title || courseId}`);
  } catch (error) {
    if (seq !== automationSaveSeq) return;
    automationSavingCourseId = "";
    automationAttention = courseId;
    automationAttentionCode = String(error?.code || "");
    courseAutomationAnnounce(
      `自动整理需要处理：${course.title || courseId}。${COURSE_AUTOMATION_ERROR_TEXT[automationAttentionCode] || COURSE_AUTOMATION_ERROR_GENERIC}`,
    );
  }
  refreshAutomationToggles();
}

/* 整体替换 PUT 不允许并发：链在途时新点击排队（最新一次为准），前链完成并
   刷新快照后按新快照串行执行——双课快速连点不再用陈旧快照互相覆盖规则。 */
let automationPendingSave = null;
let automationSaveQueueRunning = false;

async function saveCourseAutomation(course, enable) {
  if (automationSaveQueueRunning) {
    automationPendingSave = { course, enable };
    courseAutomationAnnounce(`将在当前保存完成后${enable ? "开启" : "关闭"}自动整理：${course.title || course.course_id}`);
    return;
  }
  automationSaveQueueRunning = true;
  try {
    await runCourseAutomationOnce(course, enable);
    while (automationPendingSave) {
      const pending = automationPendingSave;
      automationPendingSave = null;
      await runCourseAutomationOnce(pending.course, pending.enable);
    }
  } finally {
    automationSaveQueueRunning = false;
  }
}

function refreshAutomationToggles() {
  /* 行内开关只在课程列表容器内查找：避免全局复杂选择器，也便于最小 DOM 复用 */
  const container = $("study-course-list");
  if (!container) return;
  container.querySelectorAll(".course-automation-toggle").forEach((button) => {
    const courseId = String(button.dataset.courseId || "");
    const course = (storeCoursesForToggles || []).find((item) => String(item.course_id || "") === courseId);
    if (course) renderCourseAutomationToggle(button, course);
  });
  syncCourseAutomationNotes();
}

/* attention 行内恢复说明：闭集失败文案 + 重试动作，直接呈现在触发行内。
   说明元素是行壳的附加子节点（换行落到控件行下方），成功/新快照即清除。 */
function syncCourseAutomationNotes() {
  const container = $("study-course-list");
  if (!container) return;
  Array.from(container.children || []).forEach((row) => {
    if (String(row.className || "") !== "course-row") return;
    const toggle = row.querySelector(".course-automation-toggle");
    const courseId = String(toggle?.dataset?.courseId || "");
    const existing = Array.from(row.children || [])
      .find((child) => String(child.className || "").split(/\s+/).includes("course-automation-note"));
    if (automationAttention && courseId && courseId === String(automationAttention)) {
      const text = COURSE_AUTOMATION_ERROR_TEXT[automationAttentionCode] || COURSE_AUTOMATION_ERROR_GENERIC;
      if (existing) {
        const message = existing.querySelector("span");
        if (message) message.textContent = text;
        return;
      }
      const note = document.createElement("p");
      note.className = "course-automation-note";
      note.setAttribute("role", "status");
      const message = document.createElement("span");
      message.textContent = text;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重试";
      retry.addEventListener("click", () => {
        const course = (storeCoursesForToggles || []).find(
          (item) => String(item.course_id || "") === courseId,
        );
        if (course) requestCourseAutomation(course);
      });
      note.append(message, retry);
      row.append(note);
    } else if (existing) {
      row.replaceChildren(...Array.from(row.children || []).filter((child) => child !== existing));
    }
  });
}

let storeCoursesForToggles = [];

function requestCourseAutomation(course) {
  const state = courseAutomationState(course.course_id);
  if (state === "saving" || state === "unavailable") return;
  void saveCourseAutomation(course, state !== "on");
}

/* 快照单一轮询者：设置页管理卡退场后，课程目录（本模块）拥有唯一的
   GET /api/v3/automation 快照拉取，并经共享 store 对外发布（契约不变，
   只是发布者从 settings.js 换成这里）。快照不可用时诚实 null。 */
let automationStoreRef = null;

async function refreshAutomationSnapshot() {
  let value = null;
  try {
    value = await apiV3("automation");
  } catch {
    value = null;
  }
  automationSnapshot = value && typeof value === "object" ? value : null;
  if (automationSnapshot) {
    automationAttention = "";
    automationAttentionCode = "";
  }
  refreshAutomationToggles();
  renderCloudControlCard();
  if (automationStoreRef) automationStoreRef.set("automation", automationSnapshot);
}

/* ---- 云控制面（NIGHT2-W9 / A26）：既有后端动作经快照 actions 闭集上 UI ---- */
/* 只渲染后端快照 actions 里的动作（键面=automation.py CLOUD_ACTIONS），未知键
   一律忽略、行动集为空整卡隐藏；撤销授权/抹除云端数据为危险动作，走两击确认
   （与 settings armDeleteConfirmation 同款模式）；动作零新增后端。 */
const CLOUD_CONTROL_STATE_TEXT = Object.freeze({
  ready: "云自动化运行中",
  running: "云自动化运行中",
  /* N5FE-P6：配置在而未启用=未开启终态，不再宣称「生效中」 */
  configuring: "云自动化未开启",
  verifying: "云端验证中",
  disabled: "云自动化已停用",
  degraded: "云自动化降级运行",
  circuit_open: "保护闸已跳开",
  cleanup_pending: "等待云端清理",
  unknown: "云自动化状态未知",
});
const CLOUD_CONTROL_ACTION_TEXT = Object.freeze({
  "run-now": "立即运行",
  "retry-import": "重试导入",
  "reset-circuit": "重置保护闸",
  "update-account": "更新账号",
  /* CLOUD-VERIFY-CHAIN-1：补 verify/enable 两键——链失败后的独立重入面，
     此前六键映射不含它们，验证/启用只能靠课程开关整链重跑 */
  "verify-cloud-credentials": "重新云端验证",
  "enable-cloud": "启用云端自动整理",
  "revoke-cloud-credentials": "撤销云端授权",
  "erase-cloud-data": "抹除云端数据",
});
const CLOUD_CONTROL_DANGEROUS = new Set(["revoke-cloud-credentials", "erase-cloud-data"]);

function armCloudControlConfirmation(button, onConfirmed) {
  if (button.dataset.confirming === "true") {
    button.dataset.confirming = "false";
    button.classList.remove("confirming");
    button.textContent = button.dataset.originalLabel || button.textContent;
    void onConfirmed();
    return;
  }
  button.dataset.confirming = "true";
  button.dataset.originalLabel = button.textContent;
  button.classList.add("confirming");
  button.textContent = "再点一次确认";
  window.setTimeout(() => {
    if (button.dataset.confirming === "true") {
      button.dataset.confirming = "false";
      button.classList.remove("confirming");
      button.textContent = button.dataset.originalLabel || button.textContent;
    }
  }, 4000);
}

async function runCloudControlAction(action, button) {
  if (button.disabled) return;
  setBusy(button, true);
  try {
    await postV3("automation/actions", { action, operation_id: operationId(`cloud-${action}`) });
    courseAutomationAnnounce(`已执行：${CLOUD_CONTROL_ACTION_TEXT[action] || action}`);
  } catch (error) {
    const code = String(error?.code || "");
    courseAutomationAnnounce(
      `云端操作未完成：${COURSE_AUTOMATION_ERROR_TEXT[code] || COURSE_AUTOMATION_ERROR_GENERIC}`,
    );
  } finally {
    setBusy(button, false);
    await refreshAutomationSnapshot(); /* 结果以刷新后的快照为准（单一轮询者） */
  }
}

function renderCloudControlCard() {
  const card = $("cloud-control-card");
  if (!card) return;
  const actions = (Array.isArray(automationSnapshot?.actions) ? automationSnapshot.actions : [])
    .map((item) => String(item || ""))
    .filter((item) => Object.prototype.hasOwnProperty.call(CLOUD_CONTROL_ACTION_TEXT, item));
  if (!actions.length) {
    card.hidden = true;
    clear($("cloud-control-actions"));
    return;
  }
  /* 撤销授权撤出显眼动作位（用户拍板）：不再渲染成卡片按钮，只留一行指路
     文案指向设置隐私区；动作本体迁移后仍走同一后端动作闭集。 */
  const rowActions = actions.filter((item) => item !== "revoke-cloud-credentials");
  const revokeNote = $("cloud-control-revoke-note");
  if (revokeNote) revokeNote.hidden = !actions.includes("revoke-cloud-credentials");
  const state = String(automationSnapshot?.state || "unknown");
  const pill = $("cloud-control-state");
  pill.textContent = CLOUD_CONTROL_STATE_TEXT[state] || CLOUD_CONTROL_STATE_TEXT.unknown;
  pill.dataset.state = state;
  const row = $("cloud-control-actions");
  clear(row);
  for (const action of rowActions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = CLOUD_CONTROL_ACTION_TEXT[action];
    if (CLOUD_CONTROL_DANGEROUS.has(action)) button.className = "danger";
    button.addEventListener("click", () => {
      if (CLOUD_CONTROL_DANGEROUS.has(action)) {
        armCloudControlConfirmation(button, () => void runCloudControlAction(action, button));
      } else {
        void runCloudControlAction(action, button);
      }
    });
    row.append(button);
  }
  card.hidden = false;
}

// ---- 学习内容：笔记 / 资料 / 复习 / 课程关系 / 回顾 ----

function artifactNotices(artifact) {
  const skipped = slidesSkippedText(artifact?.metrics?.slides_skipped);
  return skipped ? `部分幻灯片已跳过（${skipped}）` : "";
}

/* ---- 总结面板：结构化渲染（概览/核心结论/章节）+ 可选 Lecture IR 关键时刻 ---- */

let artifactController = null;
let artifactEpoch = 0;
let irController = null;
let irEpoch = 0;
let latestArtifact = null;
let latestIr = null;

function hideNotesEnhancements() {
  const container = $("artifact-structured");
  if (container) container.hidden = true;
  ["artifact-overview-title", "artifact-overview", "artifact-takeaways-title", "artifact-takeaways",
    "artifact-chapters-title", "artifact-chapters", "artifact-source"].forEach((id) => {
    const node = $(id);
    if (!node) return;
    node.hidden = true;
    if (id === "artifact-takeaways" || id === "artifact-chapters") clear(node);
  });
  const keyMoments = $("artifact-key-moments");
  if (keyMoments) keyMoments.hidden = true;
  clear($("artifact-key-moment-list"));
  const target = $("artifact-content");
  if (target) target.hidden = false;
}

function renderArtifactSourceLine() {
  const line = $("artifact-source");
  if (!line) return;
  const artifact = latestArtifact;
  const model = String(artifact?.model || artifact?.content?.generation?.model || "").trim();
  const updated = formatTime(artifact?.updated_at);
  const parts = [];
  if (model) parts.push(`生成模型 ${model}`);
  if (updated && updated !== "尚未确认") parts.push(`更新于 ${updated}`);
  if (!parts.length) {
    line.textContent = "";
    line.hidden = true;
    return;
  }
  line.textContent = parts.join(" · ");
  line.hidden = false;
}

function renderChapterRows(store, target, rows) {
  clear(target);
  rows.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "chapter-row";
    if (entry.startMs != null) {
      const time = document.createElement("button");
      time.type = "button";
      time.className = "timestamp-button";
      time.textContent = bookmarkTime(entry.startMs);
      time.setAttribute("aria-label", `跳转到 ${entry.title || time.textContent}`);
      time.addEventListener("click", () => {
        const lecture = store.activeLecture;
        if (!lecture) return; /* 讲次已切换：锚点不再属于活动讲次 */
        seekToTranscriptAnchor(lecture.sub_id, entry.startMs);
      });
      row.append(time);
    }
    const body = document.createElement("div");
    body.className = "chapter-body";
    if (entry.title) body.append(textElement("p", entry.title, "chapter-title"));
    if (entry.summary) body.append(textElement("p", entry.summary, "chapter-summary"));
    row.append(body);
    target.append(row);
  });
}

/* 关键时刻只展示已知时间与页码；不从 ID/跨度猜标题，锚点非法的条目整体丢弃 */
function renderKeyMoments(store) {
  const section = $("artifact-key-moments");
  const target = $("artifact-key-moment-list");
  if (!section || !target) return;
  const ir = validLectureIr(latestIr);
  if (!ir) {
    section.hidden = true;
    clear(target);
    return;
  }
  renderChapterRows(store, target, ir.keyMoments.map((moment) => ({
    title: moment.page ? `课件第 ${moment.page} 页` : "",
    summary: "",
    startMs: moment.startMs,
  })));
  section.hidden = false;
}

/* takeaway 条目：有合法锚才带时间戳按钮（复用章节行同款），点击回字幕对应
   位置；锚=None 只显示文本——降级纪律：不渲染锚也不报错。 */
function takeawayItem(store, item) {
  const li = document.createElement("li");
  if (item.anchorMs != null) {
    li.className = "takeaway-anchored";
    const time = document.createElement("button");
    time.type = "button";
    time.className = "timestamp-button";
    time.textContent = bookmarkTime(item.anchorMs);
    time.setAttribute("aria-label", `跳转到 ${time.textContent}`);
    time.addEventListener("click", () => {
      const lecture = store.activeLecture;
      if (!lecture) return; /* 讲次已切换：锚点不再属于活动讲次 */
      seekToTranscriptAnchor(lecture.sub_id, item.anchorMs);
    });
    li.append(time);
  }
  li.append(textElement("span", item.text, "takeaway-text"));
  return li;
}

function renderNotesView(store) {
  const target = $("artifact-content");
  if (!target) return;
  /* 结构化容器缺失时（部分 DOM 宿主）只保留 legacy 文本路径 */
  const structuredBox = $("artifact-structured");
  const structured = structuredBox ? structuredNotes(latestArtifact) : null;
  hideNotesEnhancements();
  if (structured && structuredBox) {
    target.hidden = true;
    if (structured.overview) {
      const overview = $("artifact-overview");
      overview.textContent = structured.overview;
      $("artifact-overview-title").hidden = false;
      overview.hidden = false;
    }
    if (structured.takeaways.length) {
      const list = $("artifact-takeaways");
      structured.takeaways.forEach((item) => list.append(takeawayItem(store, item)));
      $("artifact-takeaways-title").hidden = false;
      list.hidden = false;
    }
    if (structured.chapters.length) {
      renderChapterRows(store, $("artifact-chapters"), structured.chapters);
      $("artifact-chapters-title").hidden = false;
      $("artifact-chapters").hidden = false;
    }
    /* U7③：章节挂进度条（player-core 消费；start_ms 锚） */
    window.dispatchEvent(new CustomEvent("courselens:chapters", {
      detail: { chapters: structured.chapters.map((chapter) => ({ title: chapter.title, start_ms: chapter.startMs })) },
    }));
    renderArtifactSourceLine();
    structuredBox.hidden = false;
  } else {
    const statusNote = artifactStatusNote(latestArtifact);
    const text = statusNote || legacyNotesText(latestArtifact);
    target.textContent = text || (latestArtifact ? "总结内容为空" : "");
    window.dispatchEvent(new CustomEvent("courselens:chapters", { detail: { chapters: [] } }));
  }
  renderKeyMoments(store);
}

async function loadArtifact(store) {
  const lecture = store.activeLecture;
  const target = $("artifact-content");
  const notices = $("artifact-notices");
  artifactEpoch += 1;
  artifactController?.abort();
  artifactController = new AbortController();
  const epoch = artifactEpoch;
  target.textContent = "";
  clear(target);
  notices.hidden = true;
  notices.textContent = "";
  hideNotesEnhancements();
  if (!lecture) return;
  /* 考核雷达与总结 artifact 解耦（BROWSERWALK-3 F3-P1-1）：讲次打开即渲染
     课程台账，无总结讲次（首跑学生=大多数态）同样可见可点；desk 雷达是
     课程级数据，不依赖 artifact 内容。 */
  void renderAssessmentRadar(store);
  const kind = String($("artifact-kind").value || "lecture_summary");
  /* UIAUDIT-1 F7：加载态与空态同走居中子元素形态（容器写法统一，不再混用） */
  clear(target);
  target.append(textElement("p", "正在加载总结", "empty-state"));
  try {
    const value = await apiV3(`artifacts?sub_id=${encodeURIComponent(lecture.sub_id)}&kind=${encodeURIComponent(kind)}`, { controller: artifactController });
    if (epoch !== artifactEpoch) return;
    latestArtifact = value.artifact || null;
    if (!latestArtifact) {
      /* C3：空载荷即空态（合同改 200+artifact:null，404 码退役）。
         UIAUDIT-1 F7：空态与其他页签统一为居中带引导语，不再左对齐裸句 */
      hideNotesEnhancements();
      clear(target);
      target.append(textElement("p", "本讲总结尚未生成，字幕就绪后会自动整理出来", "empty-state"));
      return;
    }
    const notice = artifactNotices(latestArtifact);
    if (notice) {
      notices.textContent = notice;
      notices.hidden = false;
    }
    renderNotesView(store);
  } catch (error) {
    if (error.name === "AbortError" || epoch !== artifactEpoch) return;
    latestArtifact = null;
    hideNotesEnhancements();
    target.textContent = error.message;
  }
}

/* Lecture IR 是纯增强：缺失（200 空载荷）/空/畸形都只隐藏关键时刻，不打扰总结阅读。 */
async function loadLectureIr(store) {
  const lecture = store.activeLecture;
  irEpoch += 1;
  irController?.abort();
  irController = new AbortController();
  const epoch = irEpoch;
  latestIr = null;
  renderKeyMoments(store);
  if (!lecture) return;
  try {
    const value = await apiV3(`artifacts?sub_id=${encodeURIComponent(lecture.sub_id)}&kind=lecture_ir`, { controller: irController });
    if (epoch !== irEpoch) return;
    latestIr = value.artifact?.content ?? null;
    renderKeyMoments(store);
  } catch (error) {
    if (error.name === "AbortError" || epoch !== irEpoch) return;
    latestIr = null;
    renderKeyMoments(store);
  }
}

/* 测验/复习条目的可用依据锚点：证据毫秒锚点合法，且条目身份与活动讲次一致。
   其余（无证据、无锚点、讲次不符）一律 null，条目保持可读不可点，绝不猜测。 */
export function quizEvidenceAnchor(item, lecture) {
  const courseId = String(item?.course_id || "");
  const subId = String(item?.sub_id || "");
  if (!courseId || !subId) return null;
  if (courseId !== String(lecture?.course_id || "") || subId !== String(lecture?.sub_id || "")) return null;
  return evidenceAnchorMs(item?.evidence);
}

export function reviewPlanEvidenceAnchor(plan, lecture) {
  const courseId = String(plan?.scope?.course_id || "");
  const subId = String(plan?.scope?.sub_id || "");
  if (!courseId || !subId) return null;
  if (courseId !== String(lecture?.course_id || "") || subId !== String(lecture?.sub_id || "")) return null;
  const steps = Array.isArray(plan?.steps) ? plan.steps : [];
  return steps.map((step) => evidenceAnchorMs(step?.evidence)).find((anchor) => anchor != null) ?? null;
}

/* 单个复习步骤的依据锚点：新合同步骤自带 course_id/sub_id/start_ms，legacy 步骤
   回落 plan.scope 与 evidence.start_ms。讲次不符或锚点非法一律 null（不可点）。 */
export function reviewStepEvidenceAnchor(plan, step, lecture) {
  const courseId = String(step?.course_id ?? plan?.scope?.course_id ?? "");
  const subId = String(step?.sub_id ?? plan?.scope?.sub_id ?? "");
  if (!courseId || !subId) return null;
  if (courseId !== String(lecture?.course_id || "") || subId !== String(lecture?.sub_id || "")) return null;
  const stepMs = Number(step?.start_ms);
  return evidenceAnchorMs(step?.evidence) ?? (Number.isFinite(stepMs) && stepMs >= 0 ? Math.floor(stepMs) : null);
}

/* 依据跳转：切到字幕面板并 seek 到绝对锚点（不自动播放）；随后派发既有
   transcript-time 事件让该行高亮滚动（沿用 follow 的手动滚动让位守卫）。 */
function seekToTranscriptAnchor(subId, anchorMs) {
  selectMaterialTab("transcript");
  $("player-stage").currentTime = anchorMs / 1000;
  window.dispatchEvent(new CustomEvent("courselens:transcript-time", {
    detail: { sub_id: String(subId || ""), time_ms: anchorMs },
  }));
}

function evidenceJumpButton(store, subId, anchorMs) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "evidence-jump-button";
  button.textContent = "查看依据";
  button.setAttribute("aria-label", `查看依据，跳转到 ${bookmarkTime(anchorMs)}`);
  button.addEventListener("click", () => {
    const lecture = store.activeLecture;
    if (String(lecture?.sub_id || "") !== String(subId)) return; /* 讲次已切换：锚点不再属于活动讲次 */
    seekToTranscriptAnchor(subId, anchorMs);
  });
  return button;
}

/* ---- N7F：总体复习的证据出口 ----
   从课程级 surface 跳到具体讲次时，媒体与字幕都还没就绪，直接写 currentTime 会被
   播放器忽略。这里做有界重试（≤15s），到点仍未就绪就如实说出时间点让学生自己找，
   绝不假装定位成功。 */

const REVIEW_SEEK_TIMEOUT_MS = 15000;
const REVIEW_SEEK_POLL_MS = 400;
let pendingTranscriptSeek = null;
let pendingSeekTimer = 0;

function clearPendingTranscriptSeek() {
  pendingTranscriptSeek = null;
  if (pendingSeekTimer) {
    window.clearInterval(pendingSeekTimer);
    pendingSeekTimer = 0;
  }
}

function runPendingTranscriptSeek(store) {
  const pending = pendingTranscriptSeek;
  if (!pending) { clearPendingTranscriptSeek(); return; }
  const lecture = store.activeLecture;
  if (String(lecture?.sub_id || "") !== pending.subId) {
    /* 学生已经切到别的讲次：锚点不再属于活动讲次，安静收手。 */
    clearPendingTranscriptSeek();
    return;
  }
  const player = $("player-stage");
  if (Date.now() > pending.deadline) {
    clearPendingTranscriptSeek();
    toast(`已切到这一讲，但播放器还没准备好自动定位。这段在 ${bookmarkTime(pending.startMs)}，可以在字幕里按时间找。`, "caution");
    return;
  }
  if (!player || Number(player.readyState || 0) < 1) return;
  if (store.transcriptHasTiming === false) return;
  clearPendingTranscriptSeek();
  seekToTranscriptAnchor(pending.subId, pending.startMs);
}

function beginPendingTranscriptSeek(store, subId, startMs) {
  clearPendingTranscriptSeek();
  pendingTranscriptSeek = { subId: String(subId || ""), startMs: Number(startMs), deadline: Date.now() + REVIEW_SEEK_TIMEOUT_MS };
  pendingSeekTimer = window.setInterval(() => runPendingTranscriptSeek(store), REVIEW_SEEK_POLL_MS);
  runPendingTranscriptSeek(store);
}

function findCourseLecture(store, subId) {
  const course = store.activeCourse;
  const lecture = (course?.lectures || []).find((item) => String(item.sub_id || "") === String(subId || ""));
  if (!lecture) return null;
  return { ...lecture, course_id: course.course_id, course_title: course.title };
}

/* 进入学习桌看某个讲次：只写 activeLecture —— 解除启动落地门、清 modeOverride、
   切学习面、按讲次重载都由既有 activeLecture 订阅链（handleActiveLecture）负责，
   本模块不再新增第二个解除点（SMALL-POLISH-1② 解除点计数保持五处）。 */
function enterDeskForLecture(store, lecture) {
  if (!lecture) return false;
  store.set("activeLecture", lecture);
  return true;
}

/* 讲次归属解析：优先引用自带 subId，其次活动讲次，最后本课程第一讲（课程级资料）。 */
function targetLectureFor(store, target) {
  const lecture = store.activeLecture;
  if (target?.subId && String(lecture?.sub_id || "") !== String(target.subId)) {
    return findCourseLecture(store, target.subId) || lecture || null;
  }
  if (lecture) return lecture;
  const first = (store.activeCourse?.lectures || [])[0];
  return first ? findCourseLecture(store, first.sub_id) : null;
}

function installReviewNavigation(store) {
  provideReviewNavigation({
    /* 打开/退出总体复习：课程布局与课程级 surface 互斥，另需同步学习桌返回键文案。 */
    onOpenChange() {
      renderSelectLayout();
      syncDeskReturnLabel();
    },
    toTranscript(target) {
      const startMs = Number(target?.startMs);
      if (!Number.isFinite(startMs) || startMs < 0) return false;
      const subId = String(target?.subId || "");
      const lecture = store.activeLecture;
      if (subId && String(lecture?.sub_id || "") !== subId) {
        const found = findCourseLecture(store, subId);
        if (!found || !enterDeskForLecture(store, found)) return false;
        beginPendingTranscriptSeek(store, subId, startMs);
        return true;
      }
      if (!lecture) return false;
      seekToTranscriptAnchor(subId || lecture.sub_id, startMs);
      return true;
    },
    toDocuments(target) {
      const lecture = targetLectureFor(store, target);
      if (!lecture) return false;
      if (String(store.activeLecture?.sub_id || "") !== String(lecture.sub_id)) {
        if (!enterDeskForLecture(store, lecture)) return false;
      }
      selectMaterialTab("documents");
      return true;
    },
    toAssessment(target) {
      const lecture = targetLectureFor(store, target);
      if (!lecture) return false;
      if (String(store.activeLecture?.sub_id || "") !== String(lecture.sub_id)) {
        if (!enterDeskForLecture(store, lecture)) return false;
      }
      selectMaterialTab("review");
      return true;
    },
  });
}

let documentsLoadEpoch = 0;

/* 统一文件中心（DATA-DELETE-REPAIR-1）：导入资料、生成的课件 PDF 与 AI 总结
   导出按讲次聚合在资料 tab；每条只带「导出/删除」两个动作，不卡片化重排。
   导出 = 现有文件直交系统另存对话框（a[download] 流式），绝不在应用目录造
   第二份副本；删除按类分流——总结只删导出文件，学习数据（时间轴/搜索）
   不受影响。讲次退出目录（学期滚动）后条目仍按 key 可见可删。 */
const MATERIALS_KIND_LABELS = Object.freeze({ document: "导入", courseware: "课件", summary: "总结" });
const MATERIALS_DELETE_TOASTS = Object.freeze({
  document: "已删除资料",
  courseware: "已删除课件，之后可以重新生成",
  summary: "已删除总结文件，学习数据不受影响",
});

function formatMaterialsBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function groupMaterialsEntries(entries) {
  const groups = new Map();
  for (const entry of entries || []) {
    const key = String(entry?.sub_id || "");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  }
  return [...groups.entries()].map(([subId, items]) => {
    const first = items[0] || {};
    const label = [first.lecture_date, first.lecture_title].filter(Boolean).join(" ")
      || (subId ? "本课程的其他讲次" : "未识别讲次");
    return { label, entries: items };
  }).sort((left, right) => {
    const dateLeft = String(left.entries[0]?.lecture_date || "");
    const dateRight = String(right.entries[0]?.lecture_date || "");
    if (dateLeft && dateRight) return dateRight.localeCompare(dateLeft);
    if (dateLeft) return -1;
    if (dateRight) return 1;
    return 0;
  });
}

function materialsEntryRow(store, entry) {
  const kind = String(entry?.kind || "");
  const kindLabel = MATERIALS_KIND_LABELS[kind] || "资料";
  const downloadName = String(entry?.download_name || entry?.name || "资料");
  const row = document.createElement("article");
  row.className = "item-row";
  const title = textElement("strong", String(entry?.name || "未命名资料"));
  title.append(textElement("span", kindLabel, "data-tag"));
  const metaParts = [formatMaterialsBytes(entry?.size)];
  const lectureLabel = [entry?.lecture_date, entry?.lecture_title].filter(Boolean).join(" ");
  if (lectureLabel) metaParts.push(lectureLabel);
  row.append(title, textElement("span", metaParts.join(" · ")));
  const actions = document.createElement("div");
  const exportLink = document.createElement("a");
  exportLink.className = "text-button";
  exportLink.textContent = "导出";
  exportLink.setAttribute("href", `/api/v3/materials/file?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(String(entry?.id || ""))}`);
  exportLink.setAttribute("download", downloadName);
  exportLink.setAttribute("aria-label", `导出${kindLabel}资料（${downloadName}）`);
  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "danger";
  deleteButton.textContent = "删除";
  deleteButton.setAttribute("aria-label", `删除${kindLabel}资料（${String(entry?.name || "")}）`);
  deleteButton.addEventListener("click", () => {
    armCloudControlConfirmation(deleteButton, async () => {
      setBusy(deleteButton, true);
      try {
        await postV3("materials/actions", { action: "delete", kind, id: String(entry?.id || "") });
        toast(MATERIALS_DELETE_TOASTS[kind] || "已删除", "ready");
      } catch (error) {
        toastActionError(error);
        setBusy(deleteButton, false);
        return;
      }
      await loadDocuments(store);
    });
  });
  actions.append(exportLink, deleteButton);
  row.append(actions);
  return row;
}

/* 走查拍板（甲-2）：列表筛选默认「全部类型」；空态收形——筛选与课程级
   选项隐藏、导入钮升为主操作（data-empty 同步给 CSS，见 pages.css）。 */
const MATERIALS_KIND_FILTERS = new Set(["document", "courseware", "summary"]);

function materialsKindFilter() {
  const value = String($("document-kind-filter")?.value || "all");
  return MATERIALS_KIND_FILTERS.has(value) ? value : "all";
}

function syncImportControlsVisibility(entryCount) {
  const section = $("imported-documents");
  if (section) section.dataset.empty = entryCount > 0 ? "false" : "true";
}

function renderMaterialsEntries(target, entries, store) {
  const kindFilter = materialsKindFilter();
  const visible = (Array.isArray(entries) ? entries : []).filter(
    (entry) => kindFilter === "all" || String(entry?.kind || "") === kindFilter,
  );
  syncImportControlsVisibility(visible.length);
  clear(target);
  if (visible.length === 0) {
    if (kindFilter !== "all") {
      /* EMPTY-STATES-1：筛选空类给一条真动作——原位切回「全部类型」，不重新拉网。 */
      emptyStateWithAction(target, "这个类型下暂无资料。", "查看全部类型", () => {
        const kindSelect = $("document-kind-filter");
        if (kindSelect) {
          kindSelect.value = "all";
          syncDropdown(kindSelect);
        }
        renderMaterialsEntries(target, entries, store);
      });
      return;
    }
    target.append(textElement(
      "p",
      /* 走查拍板（甲-3）：空态一句短引导；教学长文只在引导/整理说明出现一次 */
      "这门课还没有资料，生成或导入后会自动出现在这里。",
      "empty-state",
    ));
    return;
  }
  for (const group of groupMaterialsEntries(visible)) {
    target.append(textElement("p", group.label, "hint"));
    for (const entry of group.entries) target.append(materialsEntryRow(store, entry));
  }
}

async function loadDocuments(store, instanceEpoch = studyInstanceEpoch) {
  const lecture = store.activeLecture;
  const target = $("document-list");
  const epoch = ++documentsLoadEpoch;
  const requestCourseId = String(lecture?.course_id || "");
  const isCurrent = () => epoch === documentsLoadEpoch && instanceEpoch === studyInstanceEpoch
    && String(store.activeLecture?.course_id || "") === requestCourseId;
  if (!lecture) {
    clear(target);
    syncImportControlsVisibility(0);
    target.append(textElement("p", "选择一门课程后，这门课的资料会显示在这里。", "empty-state"));
    return;
  }
  clear(target);
  try {
    const value = await apiV3(`materials?course_id=${encodeURIComponent(lecture.course_id)}`, { controller });
    if (!isCurrent()) return;
    renderMaterialsEntries(target, value.entries || [], store);
  } catch (error) {
    /* 与 transcript/bookmarks 同型守卫：过期/中止静默让位；当前课程失败落
       错误空态，不把上一门课的资料滞留在列表里。EMPTY-STATES-1：补「重试」真动作。 */
    if (error.name === "AbortError" || !isCurrent()) return;
    clear(target);
    emptyStateWithAction(target, "课程资料暂时无法加载", "重试", () => void loadDocuments(store), "error-text");
  }
}

/* 课件 PDF：唯一入口是资料页本讲课件区。后端返回两个独立事实：artifact
   （最近一次原子发布的成品）与 operation（当前一次生成尝试）。前端从不从
   点击推断成功，也从不编造百分比；百分比只在后端给出 measured 计数时显示。
   状态文案只在含义变化时写入 live 区域，纯计数的轮询不会反复播报。 */
const COURSEWARE_PDF_ERROR_TEXT = Object.freeze({
  no_records: "本讲没有可用的课堂画面",
  no_pages: "没有可保留的课堂画面",
  ppt_payload_invalid: "课堂画面清单返回异常，请稍后重试",
  ppt_response_too_large: "课堂画面清单超出本地安全上限，已停止",
  ppt_pagination_stalled: "课堂画面清单分页停滞，已停止",
  ppt_record_storm: "课堂画面密度异常，已暂停待确认",
  lecture_not_found: "讲次不在已授权目录中",
  no_records_or_unavailable: "本讲没有可用的课堂画面",
});

const COURSEWARE_PDF_SKIP_TEXT = Object.freeze({
  duplicate_exact: "完全相同的重复画面（已合并）",
  html_body: "返回了网页而非图片",
  json_body: "返回了接口数据而非图片",
  empty: "空画面",
  unidentified_image: "无法识别的图像",
  decode_failed: "无法解码的图像",
  oversized_image: "超过尺寸上限的图像",
  fetch_failed: "下载失败",
  record_invalid: "这条课堂画面记录读不出来，已跳过",
  record_url_missing: "缺少画面地址",
});

const COURSEWARE_PDF_ACTIVE_STATES = new Set(["queued", "running", "pausing"]);

let coursewarePdfLastAnnounce = "";
let coursewarePdfLastSubId = "";
let coursewarePdfLastStatus = null;
const COURSEWARE_PDF_POLL_INTERVAL_MS = 2000;
const COURSEWARE_PDF_RETRY_BASE_MS = 2000;
const COURSEWARE_PDF_RETRY_MAX_MS = 30000;
/* 活跃操作期状态 GET 连续失败上限：超过即停止自动重试，保留「重新检查」手动入口 */
const COURSEWARE_PDF_MAX_STATUS_RETRIES = 10;
let coursewarePdfStatusRetries = 0;

function stopCoursewarePdfPolling() {
  coursewarePdfPollEpoch += 1;
  if (coursewarePdfTimer) window.clearTimeout(coursewarePdfTimer);
  coursewarePdfTimer = 0;
}

function coursewarePdfAnnounce(node, text) {
  /* 同一含义不重复写 live 区域：两秒一次的计数轮询不会反复播报 */
  if (coursewarePdfLastAnnounce === text) return;
  coursewarePdfLastAnnounce = text;
  node.textContent = text;
}

function renderCoursewarePdf(status, { stale = false, manualRetry = false } = {}) {
  const stateNode = $("courseware-pdf-state");
  const detailNode = $("courseware-pdf-detail");
  const infoNode = $("courseware-info");
  const downloadNode = $("courseware-pdf-download");
  const actionButton = $("generate-courseware-pdf");
  const notes = $("courseware-notes");
  const notesCounts = $("courseware-notes-counts");
  const surface = $("courseware-surface");
  if (!stateNode || !detailNode || !downloadNode || !actionButton) return;
  /* 走查拍板（甲-1）：生成动作恒为主按钮；class 由 JS 保证，HTML 初始态同款 */
  actionButton.classList.add("btn-primary");

  const artifact = status?.artifact && typeof status.artifact === "object" ? status.artifact : null;
  const operation = status?.operation && typeof status.operation === "object" ? status.operation : null;
  const opState = String(operation?.state || "");
  const opActive = COURSEWARE_PDF_ACTIVE_STATES.has(opState);
  const subId = String(status?.sub_id || coursewarePdfLastSubId || "");
  const downloadHref = `/api/v3/courseware-pdf/file?sub_id=${encodeURIComponent(subId)}`;
  const savedCounts = operation?.counts && typeof operation.counts === "object" ? operation.counts : null;

  let announce = "";
  let detail = "";
  let tone = "";
  let buttonLabel = "生成课件 PDF";
  let buttonEnabled = true;
  let buttonHidden = false;
  let downloadVisible = false;
  let notesVisible = false;

  if (!status && !stale) {
    announce = "请先选择讲次，再生成本讲课件 PDF";
    buttonEnabled = false;
  } else if (artifact) {
    const pages = Number(artifact.pages);
    const events = Number(artifact.events_total);
    const readyCopy = `已从 ${Number.isFinite(events) ? events : "?"} 个课堂画面整理为 ${Number.isFinite(pages) ? pages : "?"} 页，批注版本已保留`;
    if (opActive) {
      announce = `正在生成新版：${String(operation.label || "正在处理课堂画面")}`;
      detail = coursewarePdfDetailText(operation) || (opState === "queued" ? COURSEWARE_PDF_QUEUED_HINT : "");
      buttonLabel = "生成中…";
      buttonEnabled = false;
      /* 第十七案 a：新版生成完成前不出现下载入口——旧版极易被当成新成果
         误下载（活体报实证）；文件端点只在原子发布后可用，这里同语义。 */
      downloadVisible = false;
      tone = "";
    } else if (opState === "paused") {
      announce = `正在生成的新版已暂停：${String(operation.label || "已达到资源护栏")}`;
      detail = coursewarePdfDetailText(operation);
      buttonLabel = "继续生成";
      downloadVisible = true;
      tone = "paused";
    } else if (opState === "failed") {
      /* USEROPS-1 P3：兜底 reason 不再复读「生成未完成」，避免拼出
         「生成未完成：生成未完成，请重试」式重复（活体走查实证）。 */
      const reason = COURSEWARE_PDF_ERROR_TEXT[String(operation.error_code || "")] || "请稍后重试";
      announce = `新版生成未完成：${reason}；原版本仍可下载`;
      buttonLabel = "重试";
      downloadVisible = true;
      tone = "failed";
    } else {
      announce = readyCopy;
      buttonLabel = "重新生成";
      buttonHidden = false;
      downloadVisible = true;
      const parts = [];
      if (Number.isFinite(pages) && pages > 0) parts.push(`${pages} 页`);
      if (Number.isFinite(events) && events > 0) parts.push(`${events} 个课堂画面`);
      const generatedAt = Number(artifact.generated_at);
      /* 第十七案 d：generated_at 已是秒；formatTime 内部负责 ×1000，
         外层再乘会把时间推到公元 58691 年（活体报：日期全乱码）。 */
      if (Number.isFinite(generatedAt) && generatedAt > 0) parts.push(formatTime(generatedAt));
      detail = parts.join(" · ");
      notesVisible = true;
      if (notesCounts) {
        const skipLines = coursewarePdfSkipLines(artifact);
        notesCounts.textContent = skipLines ? `本次整理：${skipLines}` : "";
        notesCounts.hidden = !skipLines;
      }
    }
  } else if (opActive) {
    /* WAIT-UX-1 缺 ETA E5+：排队窗（无画面计数上报前）给诚实过程措辞——
       全链时长无实测分布，按纪律只写因果提示不写死数（文案源=wait-expectations.js）。 */
    announce = String(operation.label || "正在生成课件 PDF");
    detail = coursewarePdfDetailText(operation) || (opState === "queued" ? COURSEWARE_PDF_QUEUED_HINT : "");
    buttonLabel = "生成中…";
    buttonEnabled = false;
    tone = "";
  } else if (opState === "paused") {
    announce = `${String(operation.label || "已暂停：已达到资源护栏")}，可继续`;
    detail = coursewarePdfDetailText(operation);
    buttonLabel = "继续生成";
    tone = "paused";
  } else if (opState === "failed") {
    const reason = COURSEWARE_PDF_ERROR_TEXT[String(operation.error_code || "")] || "请稍后重试";
    announce = `生成未完成：${reason}`;
    buttonLabel = "重试";
    tone = "failed";
  } else {
    announce = "从本讲课堂画面整理为可下载的课件 PDF；有改动或批注的版本会全部保留。";
  }
  /* 瞬时状态错误：保留已知成品与动作，仅把含义文案替换为未确认提示 */
  if (stale) announce = status ? "状态暂时无法确认" : "课件状态暂时无法确认";
  /* 连续失败超限的收口：不再自动轮询，保留手动重查动作——点击只重新拉取状态，
     活跃操作存在则恢复展示与轮询，绝不产生第二个生成操作 */
  if (manualRetry) {
    announce = "生成状态暂时无法确认，可点「重新检查」手动确认";
    buttonLabel = "重新检查";
    buttonEnabled = true;
  }

  coursewarePdfLastAnnounce = coursewarePdfLastSubId === subId ? coursewarePdfLastAnnounce : "\u0000";
  coursewarePdfAnnounce(stateNode, announce);
  coursewarePdfLastSubId = subId;
  coursewarePdfLastStatus = stale ? coursewarePdfLastStatus : status;
  stateNode.hidden = false;
  detailNode.textContent = detail;
  detailNode.hidden = !detail;
  if (infoNode) infoNode.dataset.tone = tone;
  if (surface) surface.setAttribute("aria-busy", opActive ? "true" : "false");

  actionButton.textContent = buttonLabel;
  actionButton.disabled = !buttonEnabled;
  actionButton.hidden = buttonHidden;
  /* 走查拍板（MATERIALS-DECLUTTER-1 甲-1）：生成/继续/重试/重新生成恒为
     本区主按钮（btn-primary），不再因成品存在降级为次级文字钮；次级动作
     由「下载 PDF」承担，且仅在成品存在时出现。 */
  if (downloadVisible) {
    downloadNode.hidden = false;
    downloadNode.setAttribute("href", downloadHref);
    const name = String(artifact?.download_name || "").trim();
    if (name && /^[\w\u4e00-\u9fff.-]+$/.test(name) && !name.includes("..")) {
      downloadNode.setAttribute("download", name);
      downloadNode.setAttribute("aria-label", `下载课件 PDF（${name}）`);
    } else {
      downloadNode.setAttribute("aria-label", "下载课件 PDF");
    }
  } else {
    downloadNode.hidden = true;
    downloadNode.removeAttribute("href");
  }
  if (notes) notes.hidden = !notesVisible;
}

function coursewarePdfDetailText(operation) {
  if (!operation) return "";
  const counts = operation.counts && typeof operation.counts === "object" ? operation.counts : null;
  const parts = [];
  if (counts) {
    const processed = Number(counts.processed);
    const kept = Number(counts.kept);
    const total = Number(counts.total);
    if (Number.isFinite(processed) && Number.isFinite(kept) && Number.isFinite(total) && total > 0) {
      parts.push(`已读取 ${processed} 个画面，已保留 ${kept} 张`);
    }
  }
  if (operation.percent_measured && Number.isFinite(Number(operation.percent))) {
    parts.push(`${Math.round(Number(operation.percent))}%`);
  }
  return parts.join(" · ");
}

function coursewarePdfSkipLines(artifact) {
  const skipped = artifact?.skipped && typeof artifact.skipped === "object" ? artifact.skipped : null;
  if (!skipped) return "";
  const lines = Object.entries(skipped)
    .filter(([, count]) => Number(count) > 0)
    .map(([code, count]) => `${COURSEWARE_PDF_SKIP_TEXT[code] || "其他未采用画面"}：${Number(count)}`)
    .join("；");
  if (Number(artifact.duplicates) > 0 && !skipped.duplicate_exact) {
    return lines ? `${lines}；完全相同的重复画面（已合并）：${Number(artifact.duplicates)}` : `完全相同的重复画面（已合并）：${Number(artifact.duplicates)}`;
  }
  return lines;
}

async function loadCoursewarePdf(targetStore) {
  const activeStore = targetStore;
  const lecture = activeStore?.activeLecture;
  if (!lecture) {
    stopCoursewarePdfPolling();
    coursewarePdfLastStatus = null;
    coursewarePdfStatusRetries = 0;
    renderCoursewarePdf(null);
    return;
  }
  const subKey = String(lecture.sub_id || "");
  if (coursewarePdfLastSubId !== subKey) coursewarePdfStatusRetries = 0;
  let value = null;
  let fetchFailed = false;
  try {
    value = await apiV3(`courseware-pdf?sub_id=${encodeURIComponent(lecture.sub_id)}`, { controller });
  } catch (error) {
    if (String(error?.code || "") === "lecture_not_found") return;
    fetchFailed = true;
  }
  const current = activeStore.activeLecture;
  if (!current || String(current.sub_id || "") !== subKey) return;
  if (fetchFailed) {
    /* 瞬时错误：保留已知成品与动作，只提示状态未确认；首次加载给中性文案 */
    renderCoursewarePdf(coursewarePdfLastSubId === subKey ? coursewarePdfLastStatus : null, { stale: true });
    retryCoursewarePdfAfterFailure(activeStore, subKey);
    return;
  }
  coursewarePdfStatusRetries = 0;
  /* 甲-1b：活跃操作在此轮拿到终态（新成品落地）——同帧驱动「全部资料」
     重渲染，让新课件/新总结即刻入列，不等下一次手动进入资料 tab。 */
  const wasOperationActive = lastKnownCoursewareOperationActive();
  renderCoursewarePdf({ ...value, sub_id: value.sub_id || lecture.sub_id });
  if (wasOperationActive && !lastKnownCoursewareOperationActive()) void loadDocuments(targetStore);
  const operation = value.operation && typeof value.operation === "object" ? value.operation : null;
  if (operation && COURSEWARE_PDF_ACTIVE_STATES.has(String(operation.state || ""))) {
    pollCoursewarePdf(activeStore, String(lecture.sub_id || ""));
  }
}

function lastKnownCoursewareOperationActive() {
  const operation = coursewarePdfLastStatus?.operation;
  return Boolean(operation && typeof operation === "object"
    && COURSEWARE_PDF_ACTIVE_STATES.has(String(operation.state || "")));
}

function scheduleCoursewarePdfPoll(targetStore, subId, delay) {
  const epoch = ++coursewarePdfPollEpoch;
  if (coursewarePdfTimer) window.clearTimeout(coursewarePdfTimer);
  coursewarePdfTimer = window.setTimeout(async () => {
    if (epoch !== coursewarePdfPollEpoch) return;
    const lecture = targetStore?.activeLecture;
    if (!lecture || String(lecture.sub_id || "") !== String(subId)) return;
    await loadCoursewarePdf(targetStore);
  }, delay);
}

function pollCoursewarePdf(targetStore, subId) {
  scheduleCoursewarePdfPoll(targetStore, subId, COURSEWARE_PDF_POLL_INTERVAL_MS);
}

/* 第卅案：生成提交后的乐观轮询——POST 已受理但状态 GET 可能竞态回旧成品
   （旧页数/旧时间戳且无 operation，正常轮询链不会启动，状态行从此冻结到
   切讲次）。此处以 1s 短轮询有界续跑（默认 12 轮），直到状态事实出现
   活跃操作（其后台 2s 正典轮询自然接管）或轮数耗尽。 */
const COURSEWARE_PDF_OPTIMISTIC_POLL_MS = 1000;
const COURSEWARE_PDF_OPTIMISTIC_POLL_ROUNDS = 12;
let coursewarePdfOptimisticEpoch = 0;
function pollCoursewarePdfUntilVisible(targetStore, subId, roundsLeft) {
  if (roundsLeft <= 0) return;
  const epoch = ++coursewarePdfOptimisticEpoch;
  window.setTimeout(async () => {
    if (epoch !== coursewarePdfOptimisticEpoch) return;
    const lecture = targetStore?.activeLecture;
    if (!lecture || String(lecture.sub_id || "") !== String(subId)) return;
    /* optimistic 哨兵态不算「事实已接管」——它就是本轮询要替换掉的对象 */
    if (!coursewarePdfLastStatus?.optimistic && lastKnownCoursewareOperationActive()) return;
    await loadCoursewarePdf(targetStore);
    if (epoch !== coursewarePdfOptimisticEpoch) return;
    if (lastKnownCoursewareOperationActive()) return;
    pollCoursewarePdfUntilVisible(targetStore, subId, roundsLeft - 1);
  }, COURSEWARE_PDF_OPTIMISTIC_POLL_MS);
}

/* 活跃操作期一次状态 GET 抖动曾让轮询永久停摆（「生成中」disabled 到切讲次为止）：
   已知操作仍活跃时有界退避续轮询；终态照旧不轮询，连续失败超限后保留手动重查入口。 */
function retryCoursewarePdfAfterFailure(targetStore, subId) {
  if (!lastKnownCoursewareOperationActive()) return;
  if (coursewarePdfStatusRetries >= COURSEWARE_PDF_MAX_STATUS_RETRIES) {
    renderCoursewarePdf(coursewarePdfLastStatus, { stale: true, manualRetry: true });
    return;
  }
  coursewarePdfStatusRetries += 1;
  scheduleCoursewarePdfPoll(targetStore, subId, Math.min(COURSEWARE_PDF_RETRY_BASE_MS * 2 ** (coursewarePdfStatusRetries - 1), COURSEWARE_PDF_RETRY_MAX_MS));
}

/* 测验行：先回忆、后核对——答案默认折叠（原生按钮 + aria-expanded），揭示后不自动打分 */
function renderQuizItems(store, target, items, lecture) {
  clear(target);
  if (!items.length) {
    if (!lecture) {
      /* EMPTY-STATES-1：未选讲次时不再误报「尚未生成」——先给选择引导（讲次在左侧列表）。 */
      target.append(textElement("p", "选择讲次后，这讲的练习题会显示在这里。", "empty-state"));
      return;
    }
    /* EMPTY-STATES-1：已选讲次还没有题，给「生成练习题」真动作（复用面板头部的
       生成测验链路，含 busy/错误 toast，绝不另起一套生成通道）。 */
    emptyStateWithAction(
      target,
      "练习题尚未生成，生成后会出现在这里。",
      "生成练习题",
      () => $("generate-quiz")?.click(),
    );
    return;
  }
  /* C⑨：先自答、提交后揭示——答案与原文不再随点随看（检索练习优先于
     被动阅读）。题干由后端保证不含原文；此处揭示答案+证据原文。 */
  items.forEach((item, index) => {
    const row = document.createElement("article");
    row.className = "item-row";
    row.append(textElement("strong", item.question || "复习题"));
    const selfAnswer = document.createElement("textarea");
    selfAnswer.className = "quiz-self-answer";
    selfAnswer.rows = 2;
    selfAnswer.placeholder = "先自己作答（可不填），再提交核对";
    selfAnswer.setAttribute("aria-label", `第 ${index + 1} 题我的作答`);
    const reveal = document.createElement("button");
    reveal.type = "button";
    reveal.className = "answer-reveal-button";
    reveal.textContent = "提交并核对";
    reveal.setAttribute("aria-expanded", "false");
    reveal.setAttribute("aria-controls", `quiz-answer-${index}`);
    const answer = textElement("p", "", "quiz-answer");
    answer.hidden = true;
    answer.id = `quiz-answer-${index}`;
    reveal.addEventListener("click", () => {
      if (reveal.getAttribute("aria-expanded") === "true") return;
      const parts = [item.answer ? `答案：${item.answer}` : "后端未提供答案文本"];
      const original = String(item.evidence?.text || "").trim();
      if (original && original !== item.answer) parts.push(`原文：${original}`);
      answer.textContent = parts.join(" ");
      answer.hidden = false;
      reveal.textContent = "已核对";
      reveal.setAttribute("aria-expanded", "true");
      reveal.disabled = true;
    });
    row.append(selfAnswer, reveal, answer);
    const anchor = quizEvidenceAnchor(item, lecture);
    const action = anchor == null ? null : evidenceJumpButton(store, lecture.sub_id, anchor);
    if (action) row.append(action);
    target.append(row);
  });
}

/* 复习计划：有序步骤、预计分钟、状态（后端原词）、理由与逐条依据动作；
   前端不重排、不打分，status/reason 原样呈现。 */
const REVIEW_STEP_KIND_LABELS = Object.freeze({
  watch: "章节回顾",
  quiz: "题目回顾",
  key_moment: "关键时刻回看",
  coverage: "基础覆盖",
  /* P13-B 联动合同冻结件2：已确认考核注入步（reason=考核临近）的第 5 个 kind */
  assessment: "考核准备",
});

function reviewStepKindLabel(kind) {
  const value = String(kind || "").trim();
  return REVIEW_STEP_KIND_LABELS[value] || (value || "复习步骤");
}

function reviewStepMinutes(step) {
  const value = Number(step?.estimated_minutes);
  if (Number.isFinite(value) && value > 0) return Math.round(value);
  const legacy = Number(step?.minutes);
  return Number.isFinite(legacy) && legacy > 0 ? Math.round(legacy) : null;
}

/* 课程/讲次标题只按目录精确映射；解析不了就省略，绝不猜测 */
function lectureLabel(store, courseId, subId) {
  const course = (store.courses || []).find((item) => String(item.course_id || "") === String(courseId || ""));
  const lecture = (course?.lectures || []).find((item) => String(item.sub_id || "") === String(subId || ""));
  return [course?.title || "", lecture?.sub_title || ""].filter(Boolean).join(" · ");
}

function renderReviewSteps(store, container, plan, lecture) {
  const steps = Array.isArray(plan?.steps) ? plan.steps : [];
  clear(container);
  steps.forEach((step) => {
    const row = document.createElement("div");
    row.className = "review-step";
    row.append(textElement("span", reviewStepKindLabel(step?.kind), "review-step-kind"));
    if (step?.title) row.append(textElement("strong", String(step.title), "review-step-title"));
    const metaParts = [];
    const label = lectureLabel(store, step?.course_id ?? plan?.scope?.course_id ?? "", step?.sub_id ?? plan?.scope?.sub_id ?? "");
    if (label) metaParts.push(label);
    const minutes = reviewStepMinutes(step);
    if (minutes != null) metaParts.push(`预计 ${minutes} 分钟`);
    if (step?.status) metaParts.push(String(step.status));
    if (metaParts.length) row.append(textElement("span", metaParts.join(" · "), "review-step-meta"));
    if (step?.reason) row.append(textElement("span", `理由：${step.reason}`, "review-step-reason"));
    const anchor = reviewStepEvidenceAnchor(plan, step, lecture);
    const action = anchor == null ? null : evidenceJumpButton(store, lecture.sub_id, anchor);
    if (action) row.append(action);
    container.append(row);
  });
}

function renderReviewPlans(store, target, plans, lecture) {
  clear(target);
  if (!plans.length) {
    target.append(textElement("p", "暂无复习计划，课程进行一段后会自动生成", "empty-state"));
    return;
  }
  plans.forEach((plan) => {
    const row = document.createElement("article");
    row.className = "item-row review-plan-row";
    row.append(textElement("strong", plan.title || "复习计划"));
    const steps = Array.isArray(plan?.steps) ? plan.steps : [];
    const totalMinutes = steps.reduce((sum, step) => sum + (reviewStepMinutes(step) || 0), 0);
    const metaParts = [];
    if (steps.length) metaParts.push(`${steps.length} 个步骤`);
    if (totalMinutes > 0) metaParts.push(`共 ${totalMinutes} 分钟`);
    const updated = formatTime(plan?.updated_at);
    if (updated !== "尚未确认") metaParts.push(`更新于 ${updated}`);
    if (metaParts.length) row.append(textElement("span", metaParts.join(" · ")));
    if (String(plan?.exam_state || "") === "passed") {
      row.append(textElement("span", "考试已结束，保留历史步骤。", "review-plan-note"));
    }
    const stepsBox = document.createElement("div");
    stepsBox.className = "review-steps";
    renderReviewSteps(store, stepsBox, plan, lecture);
    row.append(stepsBox);
    target.append(row);
  });
}

/* 考试上下文是复习的只读附注：仅 exam_state=active 且后端已验证非空 course_scope
   才渲染；窗口/一致性不符（exam_at 缺失、已过、>720h）一律 fail closed 隐藏。 */
const EXAM_SOURCE_LABELS = Object.freeze({
  fudan_jwgl: "来源：复旦教务系统",
  user_confirmed: "来源：你确认的考试时间",
});

function examAtDate(plan) {
  const raw = plan?.exam_at;
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) return new Date(raw * 1000);
  if (typeof raw === "string" && raw.trim()) {
    const parsed = new Date(raw);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  return null;
}

function examRemainingText(examAt, precision, now = Date.now()) {
  const diffMs = examAt.getTime() - now;
  if (diffMs < 0 || diffMs > 720 * 3600 * 1000) return null;
  const totalHours = Math.floor(diffMs / 3600000);
  const days = Math.floor(totalHours / 24);
  if (String(precision) === "date") return days > 0 ? `距考试 ${days} 天` : "距考试 1 天内";
  if (days > 0) return `距考试 ${days} 天 ${totalHours % 24} 小时`;
  return `距考试 ${totalHours} 小时`;
}

/* exam-context 节显隐统一裁决（BROWSERWALK-3 F3-P1-2）：active 计划内容
   ∨ desk 考核雷达内容，其一即开、全空即藏。renderExamContext 与
   renderAssessmentRadar 是两条独立异步流（loadQuiz / loadArtifact），各自
   渲染完子件后都走这里收口显隐——雷达不再被「无 active 计划」态恒隐
   （一键建的考核计划 exam_state=None 也能可见），重渲染也不会把已显示的
   雷达闪没。 */
function syncExamContextVisibility() {
  const section = $("exam-context");
  if (!section) return;
  const row = $("exam-context-row");
  const list = $("exam-today-list");
  const state = $("exam-today-state");
  const desk = $("assessment-desk-radar");
  const hasContent = Boolean(
    (row && row.textContent.trim())
    || (list && list.children.length > 0)
    || (state && !state.hidden && state.textContent.trim())
    || (desk && !desk.hidden && desk.children.length > 0),
  );
  section.hidden = !hasContent;
}

function renderExamContext(store, plans) {
  const section = $("exam-context");
  const row = $("exam-context-row");
  const list = $("exam-today-list");
  const state = $("exam-today-state");
  if (!section || !row || !list || !state) return;
  row.textContent = "";
  clear(list);
  state.hidden = true;
  state.textContent = "";
  const lecture = store.activeLecture;
  if (!lecture) {
    syncExamContextVisibility();
    return;
  }
  const active = (Array.isArray(plans) ? plans : []).find((plan) => {
    if (String(plan?.exam_state || "") !== "active") return false;
    return Array.isArray(plan?.course_scope) && plan.course_scope.length > 0;
  });
  if (!active) {
    syncExamContextVisibility();
    return;
  }
  const examAt = examAtDate(active);
  const remaining = examAt ? examRemainingText(examAt, String(active?.exam_precision || ""), Date.now()) : null;
  if (!remaining) {
    syncExamContextVisibility();
    return;
  }
  const parts = [remaining];
  const source = EXAM_SOURCE_LABELS[String(active?.exam_source || "")];
  if (source) parts.push(source);
  const daily = Number(active?.daily_minutes);
  if (Number.isFinite(daily) && daily > 0) parts.push(`今日预算 ${Math.round(daily)} 分钟`);
  row.textContent = parts.join(" · ");
  /* 今天建议：只消费后端顺序的前缀（不重排、不打分），按 daily_minutes 预算截取 */
  const steps = Array.isArray(active?.steps) ? active.steps : [];
  const budget = Number(active?.daily_minutes);
  const suggested = [];
  let used = 0;
  for (const step of steps) {
    const minutes = reviewStepMinutes(step) || 0;
    if (suggested.length && Number.isFinite(budget) && budget > 0 && used + minutes > budget) break;
    suggested.push(step);
    used += minutes;
    if (suggested.length >= 8) break;
  }
  if (!suggested.length) {
    state.textContent = "今天建议将在计划步骤生成后显示。";
    state.hidden = false;
    syncExamContextVisibility();
    return;
  }
  suggested.forEach((step) => {
    const item = document.createElement("article");
    item.className = "item-row exam-suggestion";
    item.append(textElement("strong", String(step?.title || "") || reviewStepKindLabel(step?.kind)));
    const metaParts = [reviewStepKindLabel(step?.kind)];
    const label = lectureLabel(store, step?.course_id ?? "", step?.sub_id ?? "");
    if (label) metaParts.push(label);
    const minutes = reviewStepMinutes(step);
    if (minutes != null) metaParts.push(`预计 ${minutes} 分钟`);
    item.append(textElement("span", metaParts.join(" · ")));
    if (step?.reason) item.append(textElement("span", `理由：${step.reason}`));
    const anchor = reviewStepEvidenceAnchor(active, step, lecture);
    const action = anchor == null ? null : evidenceJumpButton(store, lecture.sub_id, anchor);
    if (action) item.append(action);
    list.append(item);
  });
  syncExamContextVisibility();
}

/* 课程关系：证据状态永远来自后端数据；无证据/未知一律“证据状态未提供”，绝不默认“有证据” */
function conceptEvidenceState(edge) {
  if (String(edge?.status || "") === "stale") return "证据待确认";
  if (Array.isArray(edge?.evidence) && edge.evidence.length) return "有证据";
  return "证据状态未提供";
}

function renderConceptEdges(target, edges, conceptNames, courseTitles) {
  clear(target);
  if (!edges.length) {
    target.append(textElement("p", "课程概念关系尚未建立，生成后会显示在这里", "empty-state"));
    return;
  }
  edges.forEach((edge) => {
    const row = document.createElement("article");
    row.className = "item-row";
    const conceptId = String(edge?.concept_id || "");
    row.append(textElement("strong", conceptNames.get(conceptId) || String(edge?.relation || "").trim() || "课程关系"));
    const fromTitle = courseTitles.get(String(edge?.from_course_id || "")) || String(edge?.from_course_id || "");
    const toTitle = courseTitles.get(String(edge?.to_course_id || "")) || String(edge?.to_course_id || "");
    const metaParts = [];
    if (fromTitle && toTitle) metaParts.push(`${fromTitle} ↔ ${toTitle}`);
    metaParts.push(conceptEvidenceState(edge));
    row.append(textElement("span", metaParts.join(" · ")));
    target.append(row);
  });
}

let quizLoadEpoch = 0;

async function loadQuiz(store, instanceEpoch = studyInstanceEpoch) {
  const lecture = store.activeLecture;
  const quizTarget = $("quiz-list");
  const reviewTarget = $("review-list");
  renderExamContext(store, []);
  const epoch = ++quizLoadEpoch;
  const requestSubId = String(lecture?.sub_id || "");
  const requestCourseId = String(lecture?.course_id || "");
  const isCurrent = () => epoch === quizLoadEpoch && instanceEpoch === studyInstanceEpoch
    && String(store.activeLecture?.sub_id || "") === requestSubId
    && String(store.activeLecture?.course_id || "") === requestCourseId;
  if (!lecture) {
    renderQuizItems(store, quizTarget, [], lecture);
    renderReviewPlans(store, reviewTarget, [], lecture);
    return;
  }
  clear(quizTarget);
  clear(reviewTarget);
  try {
    const value = await apiV3(`quizzes?course_id=${encodeURIComponent(lecture.course_id)}&sub_id=${encodeURIComponent(lecture.sub_id)}`, { controller });
    if (!isCurrent()) return;
    renderQuizItems(store, quizTarget, value.items || [], lecture);
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) return;
    /* EMPTY-STATES-1：错误空态补「重试」真动作（重跑整段装载，与自动装载同链路）。 */
    emptyStateWithAction(quizTarget, "测验暂时无法加载", "重试", () => void loadQuiz(store), "error-text");
    emptyStateWithAction(reviewTarget, "复习计划暂时无法加载", "重试", () => void loadQuiz(store), "error-text");
    throw error; /* 保留既有调用方错误面（生成测验按钮的失败 toast） */
  }
  try {
    const plans = await apiV3("review-plans", { controller });
    if (!isCurrent()) return;
    renderReviewPlans(store, reviewTarget, plans.plans || [], lecture);
    renderExamContext(store, plans.plans || []);
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) return;
    emptyStateWithAction(reviewTarget, "复习计划暂时无法加载", "重试", () => void loadQuiz(store), "error-text");
    throw error;
  }
}

let conceptsLoadEpoch = 0;

async function loadConcepts(store, instanceEpoch = studyInstanceEpoch) {
  const course = store.activeCourse;
  const target = $("concept-list");
  const epoch = ++conceptsLoadEpoch;
  const requestCourseId = String(course?.course_id || "");
  const isCurrent = () => epoch === conceptsLoadEpoch && instanceEpoch === studyInstanceEpoch
    && String(store.activeCourse?.course_id || "") === requestCourseId;
  if (!course) {
    clear(target);
    return;
  }
  clear(target);
  try {
    const value = await apiV3(`concepts?course_id=${encodeURIComponent(course.course_id)}`, { controller });
    if (!isCurrent()) return;
    const edges = value.edges || value.relationships || [];
    const conceptNames = new Map((Array.isArray(value.concepts) ? value.concepts : [])
      .map((item) => [String(item?.concept_id || ""), String(item?.name || "")])
      .filter(([id, name]) => id && name));
    const courseTitles = new Map((Array.isArray(value.courses) ? value.courses : [])
      .map((item) => [String(item?.course_id || ""), String(item?.title || "")])
      .filter(([id, title]) => id && title));
    renderConceptEdges(target, edges, conceptNames, courseTitles);
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) return;
    clear(target);
    target.append(textElement("p", "课程关系暂时无法加载", "empty-state error-text"));
    throw error; /* 保留「分析课程关系」的失败 toast */
  }
}

/* STUDY-STATS-M1：旧「学习回顾」死格（loadAnalytics 三字段错配恒显 0）已移除，
   其数据被着陆页「本周学习面貌」卡取代（modules/study-stats.js 渲染，安装于
   installStudy 尾部）。此处不留转发桩——死格零残留由钉测 grep 钉死。 */

const MATERIAL_TAB_ORDER = ["transcript", "notes", "documents", "review"];

function selectMaterialTab(name, { focus = false } = {}) {
  document.querySelectorAll("[role='tab'][data-material-tab]").forEach((node) => {
    const active = node.dataset.materialTab === name;
    node.classList.toggle("active", active);
    node.setAttribute("aria-selected", String(active));
    node.tabIndex = active ? 0 : -1;
    if (active && focus) node.focus();
  });
  document.querySelectorAll("[role='tabpanel'][data-material-panel]").forEach((node) => {
    node.hidden = node.dataset.materialPanel !== name;
  });
}

function handleMaterialTabKeydown(event) {
  const tab = event.target.closest?.("[role='tab'][data-material-tab]");
  if (!tab) return;
  const current = MATERIAL_TAB_ORDER.indexOf(tab.dataset.materialTab);
  let next = null;
  if (event.key === "ArrowRight") next = MATERIAL_TAB_ORDER[(current + 1) % MATERIAL_TAB_ORDER.length];
  else if (event.key === "ArrowLeft") next = MATERIAL_TAB_ORDER[(current - 1 + MATERIAL_TAB_ORDER.length) % MATERIAL_TAB_ORDER.length];
  else if (event.key === "Home") next = MATERIAL_TAB_ORDER[0];
  else if (event.key === "End") next = MATERIAL_TAB_ORDER[MATERIAL_TAB_ORDER.length - 1];
  if (next == null) return;
  event.preventDefault();
  selectMaterialTab(next, { focus: true });
}

async function loadAll(store, instanceEpoch = studyInstanceEpoch) {
  controller?.abort();
  controller = new AbortController();
  stopCoursewarePdfPolling();
  const lecture = store.activeLecture;
  await Promise.allSettled([
    loadTranscript(store, instanceEpoch), loadArtifact(store), loadLectureIr(store), loadDocuments(store),
    loadQuiz(store), loadConcepts(store), loadBookmarks(store),
    loadCoursewarePdf(store),
    /* THINK-LADDER-2：质量 chip 与总结面板解耦（读取失败=隐藏，绝不挡面板） */
    refreshQualityChip(store),
  ]);
}

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let result = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    result += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(result);
}

// ---- 学习页三态：未选课 / 已选课未选讲次 / 学习桌面 ----

let studyMode = "empty";
let modeOverride = null;
let courseChosen = false;
let drilldownQuery = null;
/* UIAUDIT-1 F15：顶栏面包屑只在学习页展示——切到直播/设置等页面时收起，
   不再残留上一讲上下文。当前页由 courselens:page 事件同步（初始=学习页）。 */
let topbarActivePage = "study";
/* SMALL-POLISH-1② 启动落地门：本次启动首次落地固定在问候/欢迎面——自动登录
   会话恢复完成也不自动进入选择面/学习桌；目录自动选中只留在内存（显式进入后
   照常高亮）。显式导航（问候 CTA、课程/讲次点击、搜索/课表跳转、字标返回、
   直播进入）即解除。仅改落地视图：登录/恢复请求链与目录选择语义零变化。 */
let studyLandingNavigated = false;
let autoSelectingCatalog = false; /* 目录渲染的自动选中不解除落地门 */

/* 学习页三态纯模型（可测试）：navigated=false 时恒为问候面（启动落地门）。
   N6L S1 U3：直播播放移交独立直播页，学习桌条件只剩讲次（直播播放键退役）。 */
export function studyLandingMode(store, modeOverride, navigated) {
  if (store.activeLecture && modeOverride !== "select" && navigated) return "desk";
  if ((modeOverride === "select" || store.activeCourse) && navigated) return "select";
  return "empty";
}

/* U1「继续学习」直达语义（FIRST-LOGIN-UX-2，可测试纯函数）：把 last-lecture
   记忆解析进当前目录。闭集守卫：course_id+sub_id 必须同时命中现目录——陈旧
   记忆/换账号/目录刷新后讲次不在，一律视为无历史（卡不出现，维持现路径）。
   返回讲次对象已带 course_id/course_title（与讲次行点击写 activeLecture 同形）。 */
export function resolveContinueTarget(courses, last) {
  const courseId = String(last?.course_id ?? "");
  const subId = String(last?.sub_id ?? "");
  if (!courseId || !subId) return null;
  const pool = Array.isArray(courses) ? courses : [];
  const course = pool.find((item) => String(item?.course_id || "") === courseId);
  const lecture = (course?.lectures || []).find((item) => String(item?.sub_id || "") === subId);
  if (!course || !lecture) return null;
  return { ...lecture, course_id: course.course_id, course_title: course.title };
}

function renderSelectLayout() {
  /* N7F：总体复习打开时，课程/讲次布局整体让位给课程级 surface。
     判定归 course-review.js（唯一所有者），本处只做可见性联动，元素缺失时静默跳过
     —— 精简夹具的 mjs 不会带这组节点。 */
  const reviewOpen = isCourseReviewOpen();
  const layout = document.querySelector(".course-layout");
  const surface = $("course-review-surface");
  if (layout) layout.hidden = reviewOpen;
  if (surface) surface.hidden = !reviewOpen;
  if (reviewOpen) return;
  const drilldown = Boolean(drilldownQuery?.matches);
  $("study-course-list").hidden = drilldown && courseChosen;
  document.querySelector(".course-layout > .lecture-pane").hidden = drilldown && !courseChosen;
  $("study-back-courses").hidden = !(drilldown && courseChosen);
}

/* 学习桌返回键文案随来源变化：从总体复习跳出去看字幕的，返回的是总体复习。
   只改文案，不改既有返回行为（暂停/落选择态/清 drilldown 全走原链）。 */
function syncDeskReturnLabel() {
  const button = $("study-back-select");
  if (!button) return;
  const label = isCourseReviewOpen() ? "‹ 总体复习" : "‹ 课程与讲次";
  if (button.textContent !== label) button.textContent = label;
}

function renderStudyMode(store) {
  /* 学习桌条件 = 讲次播放（直播已移交独立直播页，N6L S1 U3）；
     modeOverride "select" 仍是显式用户意图，优先于讲次。启动落地门见上。 */
  const previousMode = studyMode;
  studyMode = studyLandingMode(store, modeOverride, studyLandingNavigated);
  $("study-empty").hidden = studyMode !== "empty";
  const selectRegion = $("study-select");
  const deskRegion = $("study-desk");
  /* D4（焦点归还家族）：区域过场会把焦点所在子树整体 hidden——焦点掉 BODY。
     切换前捕获焦点位置：选择面→播放桌，焦点还给播放桌首控件（返回键）；
     播放桌→选择面，焦点还给新 active 讲次行。仅真实过场且焦点确在让位区域
     时动作，其余渲染零扰动。 */
  const focusWithin = (region) => {
    const active = document.activeElement;
    return Boolean(active && region && typeof region.contains === "function" && region.contains(active));
  };
  const focusLeftSelect = focusWithin(selectRegion);
  const focusLeftDesk = focusWithin(deskRegion);
  /* U1（FIRST-LOGIN-UX-2）：问候面过场焦点捕获——「继续学习」一击直达/「选择
     课程」进选择面两链的键盘落点补档。指针模态零行为变化（焦点照旧落 BODY，
     绝不为指针驻留焦点）；仅键盘模态（data-input="key"）补锚，遵守焦点政策
     「键盘态零抢夺之外的切页落点」类。 */
  const focusLeftEmpty = focusWithin($("study-empty"));
  const keyboardModality = document.documentElement?.dataset?.input === "key";
  selectRegion.hidden = studyMode !== "select";
  deskRegion.hidden = studyMode !== "desk";
  if (previousMode !== studyMode) {
    /* F1 恢复行容器联动（D-20261009-09）：学习面三态过场广播，onboarding.js
       据此把挂起提示行重挂到当前可见宿主（问候 hero / 选课概览）。区域 hidden
       已先落位，监听方读到的是过场后的真态。 */
    window.dispatchEvent(new CustomEvent("courselens:study-mode", { detail: studyMode }));
  }
  /* NAV-HANG-1②（幽灵音频补档）：任何离开学习桌的过场都显式叫停播放器。
     显式返回链本就自带 pause（幂等无害）；此前 auth 翻转弹回等旁路链漏档
     —— desk 隐藏后 video 不可见地继续出声（R2 E1 实证）。pause 存在性
     守卫：合成测试壳的桩件无该方法时静默跳过。 */
  if (previousMode === "desk" && studyMode !== "desk") {
    const player = $("player-stage");
    if (player && typeof player.pause === "function" && player.paused === false) {
      player.pause();
    }
  }
  if (previousMode !== studyMode && focusLeftSelect && studyMode === "desk") {
    /* WP1-D3：过场锚=播放钮（Space 即播放/暂停）。播放钮暂不可用（控制台
       未就绪）时不动作、不锚返回钮——返回钮吃空格会把键盘生直接弹回目录；
       焦点落 BODY 时播放器页面级快捷键照常接管（PLAYER-INTERACT-REPAIR-1
       归属语义：学习桌可见时空格/方向键全部可用）。 */
    const playButton = $("player-ctrl-play");
    if (playButton && !playButton.disabled) playButton.focus?.({ preventScroll: true });
  } else if (previousMode !== studyMode && focusLeftDesk && studyMode === "select") {
    const activeSubId = String(store.activeLecture?.sub_id || "");
    const rowSelector = activeSubId
      ? `#study-lecture-list [data-sub-id="${CSS.escape(activeSubId)}"]`
      : "#study-lecture-list .lecture-row";
    document.querySelector(rowSelector)?.focus?.({ preventScroll: true });
  } else if (previousMode !== studyMode && focusLeftEmpty && keyboardModality && studyMode === "desk") {
    /* U1「继续学习」键盘链落点：与 WP1-D3 同锚（播放钮），同一守卫（禁用不锚）。 */
    const playButton = $("player-ctrl-play");
    if (playButton && !playButton.disabled) playButton.focus?.({ preventScroll: true });
  } else if (previousMode !== studyMode && focusLeftEmpty && keyboardModality && studyMode === "select") {
    /* U1「选择课程」键盘链落点：锚首门课程行（选择面的主选面）；目录未就绪
       （无课程行）不动作，绝不回顶栏、绝不抢焦点。 */
    $("study-course-list")?.querySelector(".course-row-main")?.focus?.({ preventScroll: true });
  }
  /* 主按钮文案（三态+degraded）由 greeting.js 拥有：订阅 store.auth 响应式切换。 */
  renderSelectLayout();
  syncDeskReturnLabel();

  /* 顶栏面包屑：仅在学习桌展示真实层级位置 */
  renderTopbarCrumbs(store);
}

/* UIAUDIT-1 F15：面包屑渲染唯一入口——内容渲染与页面切换两处共用同一真相
   （studyMode=desk 且有讲次且正在学习页才展示；否则收起，不残留旧讲次） */
function renderTopbarCrumbs(store) {
  const crumbs = $("topbar-crumbs");
  if (!crumbs) return;
  const lecture = store.activeLecture;
  if (topbarActivePage === "study" && studyMode === "desk" && lecture) {
    crumbs.textContent = [lecture.course_title || (store.activeCourse || {}).title, lecture.sub_title]
      .filter(Boolean).join(" · ");
    crumbs.hidden = false;
  } else {
    crumbs.textContent = "";
    crumbs.hidden = true;
  }
}

/* U1「继续学习」弱化链渲染（FIRST-LOGIN-UX-2 实现；LANDING-AESTHETIC-1 用户
   裁决降级=选择课程主钮下一行极轻文字链）：last-lecture 记忆经
   resolveContinueTarget 闭集解析进当前目录，解析成功才显示（否则 hidden=
   纯诗页+主钮）。可见文字=「上次：课程 · 讲次」（13px muted 近乎隐身），
   可访问名=「继续学习 课程·讲次」（弱化视觉不弱化语义）；文字全部来自实时
   目录渲染，localStorage 只有 ID。渲染零焦点扰动（不 focus、不抢 Tab 位置），
   按钮缺席（精简夹具）静默跳过。 */
function renderContinueLearning(store) {
  const button = $("study-continue");
  if (!button) return;
  const target = resolveContinueTarget(store.courses, store.readLastLecture());
  if (!target) {
    button.hidden = true;
    return;
  }
  const label = [target.course_title || target.course_id, target.sub_title || target.sub_id]
    .filter(Boolean).join(" · ");
  const text = `上次：${label}`;
  if (button.textContent !== text) button.textContent = text;
  button.setAttribute("aria-label", `继续学习 ${label}`);
  if (button.hidden) button.hidden = false;
}

export async function installStudy(store) {
  const transcriptList = $("transcript-list");
  const bookmarkList = $("bookmark-list");
  const materialsTabs = document.querySelector(".materials-tabs");
  const instanceEpoch = ++studyInstanceEpoch;
  resetTranscriptLoading();
  let disposed = false;
  let catalogController = null;
  let catalogRefreshTimer = 0;
  let catalogRefreshRetries = 0;
  /* WAIT-UX-1 缺进度 D1+：刷新长尾窗（1s 轮询×N，fetch 死线 20s/次）此前只有
     一次性 toast+aria-busy，慢网学生无从分辨「在推进」还是「卡死」。进度行随
     既有轮询节拍步进已等待秒数；settle/超时/异常即收。起点懒记：手动刷新以
     提交时刻起算，挂载期自动确认循环以首次重试起算。 */
  let catalogRefreshStartedAt = 0;
  const setCatalogRefreshProgress = (text) => {
    const node = ensureWaitStatusLine("catalog-refresh-progress", $("study-course-list"), $("study-select"));
    node.hidden = !text;
    node.textContent = text || "";
  };
  const stopCatalogRefreshProgress = () => {
    catalogRefreshStartedAt = 0;
    setCatalogRefreshProgress("");
  };
  const catalogRefreshElapsedText = () => {
    if (!catalogRefreshStartedAt) return "";
    const elapsed = Math.floor((Date.now() - catalogRefreshStartedAt) / 1000);
    return elapsed > 0 ? `（已等待 ${elapsed} 秒）` : "";
  };
  /* AVATAR-POLISH-1 OBS-4（化身走查 OBS-4）：「刷新」提交后目录确认要走完整
     重试/死线循环（降级网下可达数十秒），期间 POST 早已返回、按钮恢复可点，
     再点会重复提交并叠出同文 toast。提交即在途：在途内再点不重复提交，只给
     一句人话；在途收口挂既有的 settled/timeout 事件，另设死线兜底，防
     AbortError 静默收尾等事件缺席场景把在途态挂死。 */
  let catalogRefreshSubmitInFlight = false;
  let catalogRefreshSubmitGuardTimer = 0;
  const releaseCatalogRefreshSubmitGuard = () => {
    catalogRefreshSubmitInFlight = false;
    if (catalogRefreshSubmitGuardTimer) window.clearTimeout(catalogRefreshSubmitGuardTimer);
    catalogRefreshSubmitGuardTimer = 0;
  };
  const handleMaterialTabClick = (event) => {
    const button = event.target.closest?.("[role='tab'][data-material-tab]");
    if (button && materialsTabs.contains(button)) selectMaterialTab(button.dataset.materialTab);
  };
  const handleBackToSelect = () => {
    /* PLAYER-INTERACT-REPAIR-1 单元三：退出学习桌即暂停。本路径是学习桌内
       返回（不走 selectPage，无 courselens:page 广播），必须显式叫停播放器；
       pause 事件链在 player-core 持久化进度，音频绝不跟到选择面/主界面。 */
    const player = $("player-stage");
    if (player && player.paused === false) player.pause();
    studyLandingNavigated = true; /* 字标/返回链是显式导航 */
    modeOverride = "select";
    /* N7F：若是从总体复习跳出去看字幕/资料的，返回即回到总体复习并恢复
       tab/筛选/滚动位；普通路径下 restoreCourseReview 返回 false，行为不变。 */
    restoreCourseReview(store);
    renderStudyMode(store);
  };
  /* 字标返回：与“课程与讲次”同一条路径，仅切回选择态，不清空已选课程/讲次 */
  const handleStudyReturnSelect = () => handleBackToSelect();
  const handleBackToCourses = () => {
    courseChosen = false;
    renderSelectLayout();
    $("study-course-list").querySelector("button")?.focus({ preventScroll: true });
  };
  /* 第卅三案：自动登录在途窗口内，选课入口进等待态——绝不把在途当失败跳
     登录。会话就绪自动续接原操作（进入课程目录）；明确失败（degraded/
     未登录/需操作）或 30s 有界超时才转登录引导。
     第卅九案：等待仅是无信号时的兜底——恢复期「本机可信缓存」上下文（后端
     restoring_trusted）直接进目录，不再靠超时赌验证快慢；30s 保持不改：它
     只覆盖无该信号的 checking（账号不可自动恢复），此时拉长只会延长一条没有
     用户出口的死等。 */
  const COURSE_SELECT_WAIT_TIMEOUT_MS = 30000;
  let courseSelectAwaiting = false;
  let courseSelectWaitTimer = 0;
  const enterCourseSelect = () => {
    studyLandingNavigated = true; /* 问候面唯一主按钮：显式进入选择面 */
    modeOverride = "select";
    renderStudyMode(store);
  };
  const abandonCourseSelectWait = (fallbackToLogin) => {
    courseSelectAwaiting = false;
    if (courseSelectWaitTimer) {
      window.clearTimeout(courseSelectWaitTimer);
      courseSelectWaitTimer = 0;
    }
    const button = $("study-start-select");
    if (button) {
      button.disabled = false;
      button.textContent = buttonTextFor(String(store.auth?.state || ""));
    }
    if (fallbackToLogin) {
      window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "login" }));
    }
  };
  const handleStartSelect = () => {
    const state = String(store.auth?.state || "");
    if (state === "ready") {
      enterCourseSelect();
      return;
    }
    if (state === "checking") {
      /* 第卅九案：恢复期本地可信上下文（后端闭集增量键 restoring_trusted =
         checking 且保存账号可自动恢复）——此刻课程目录/课表读族本就放行本机
         快照，入口直接进目录，绝不让用户在夜间慢验证里空等再被甩去登录。 */
      if (store.auth?.restoring_trusted === true) {
        enterCourseSelect();
        return;
      }
      /* 自动登录在途=等待态（轻提示，绝不跳登录） */
      if (courseSelectAwaiting) return;
      courseSelectAwaiting = true;
      const button = $("study-start-select");
      if (button) {
        button.textContent = "正在登录…";
        button.disabled = true;
      }
      if (courseSelectWaitTimer) window.clearTimeout(courseSelectWaitTimer);
      courseSelectWaitTimer = window.setTimeout(() => {
        courseSelectWaitTimer = 0;
        if (courseSelectAwaiting) abandonCourseSelectWait(true);
      }, COURSE_SELECT_WAIT_TIMEOUT_MS);
      return;
    }
    abandonCourseSelectWait(true);
  };
  /* U1「继续学习」一击直达（FIRST-LOGIN-UX-2）：点击即写 activeLecture 走既有
     订阅链（解除落地门/切学习桌/按讲次重载全复用，零新增解除点）；续播位置
     由播放器既有恢复链（GET progress+L96' 自动续播）接管。每次点击前重解析
     ——记忆陈旧（目录已变）时静默不动作，绝不跳错讲次。 */
  const handleContinueLearning = () => {
    const target = resolveContinueTarget(store.courses, store.readLastLecture());
    if (!target) return;
    enterDeskForLecture(store, target);
  };
  const handleTranscriptUserIntent = (event) => {
    if (isTranscriptScrollIntent(event)) transcriptManualScrollUntil = Date.now() + 2500;
  };
  const handleTranscriptTime = (event) => {
    followTranscript(event.detail?.sub_id, Number(event.detail?.time_ms));
  };
  /* 播放器保存进度成功后的即时回写：缓存 + 讲次卡文案原地更新（零新增请求） */
  const handleWatchProgress = (event) => {
    const detail = event.detail || {};
    const subId = String(detail.sub_id || "");
    if (!subId) return;
    watchProgressBySubId.set(subId, detail);
    applyLectureProgressText(subId);
  };
  const handleActiveLecture = (lecture) => {
    if (lecture) {
      studyLandingNavigated = true; /* 显式讲次进入（含搜索/课表跳转） */
      /* U1（FIRST-LOGIN-UX-2）：最后打开的讲次入记忆——本订阅是全应用讲次打开
         的单一漏斗（行点击/搜索/课表/复习跳转全链复用），着陆页据此解析
         「继续学习」一击直达。只写 ID 级最小键（零课程名/个人数据）。 */
      store.rememberLastLecture({ course_id: lecture.course_id, sub_id: lecture.sub_id, ts: Date.now() });
    }
    modeOverride = null;
    resetBookmarkActions();
    renderStudyMode(store);
    void loadAll(store, instanceEpoch);
  };
  const handleBookmarkAction = (event) => {
    const deleteButton = event.target.closest?.("button[data-bookmark-id].bookmark-delete-button");
    if (deleteButton && bookmarkList.contains(deleteButton)) {
      void updateBookmarkDelete(store, deleteButton);
      return;
    }
    const button = event.target.closest?.("button[data-bookmark-action]");
    if (button && bookmarkList.contains(button)) void updateBookmarkReview(store, button);
  };
  const handleTranscriptSearchInput = () => renderTranscriptSearch();
  /* U9 检索导航：Enter=下一个命中，Shift+Enter=上一个（到第 1 处后再按回卷
     到末尾）；Esc=清空回到文稿列表，焦点留在框内；命中项仍可 Tab 到达 */
  const jumpTranscriptSearchHit = () => {
    const { results, state } = transcriptSearchElements();
    const row = results ? results.children[transcriptSearchCursor] : null;
    if (!row) return;
    [...(results.children || [])].forEach((node, index) => {
      node.classList.toggle("transcript-hit-active", index === transcriptSearchCursor);
    });
    const button = row.children ? row.children[0] : null;
    if (button && typeof button.click === "function") button.click();
    if (typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
    if (state) {
      state.textContent = `第 ${transcriptSearchCursor + 1} 处，共 ${results.children.length} 处（Enter 下一个 / Shift+Enter 上一个）`;
    }
  };
  const handleTranscriptSearchKeydown = (event) => {
    /* IME 合成态早退（CJK-GUARD-1）：合成中的 Esc=取消候选、Enter=上屏，
       绝不清空检索框或跳命中项。 */
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape") {
      const { input } = transcriptSearchElements();
      if (input && input.value) {
        input.value = "";
        renderTranscriptSearch();
      }
      return;
    }
    if (event.key !== "Enter") return;
    const { results } = transcriptSearchElements();
    const total = results ? results.children.length : 0;
    if (!total) return;
    event.preventDefault();
    /* 光标=当前命中位（渲染时已高亮第 1 处）：Enter 前进，Shift+Enter 后退 */
    transcriptSearchCursor = event.shiftKey
      ? (transcriptSearchCursor - 1 + total) % total
      : (transcriptSearchCursor + 1) % total;
    jumpTranscriptSearchHit();
  };
  const handleArtifactKind = () => void loadArtifact(store);
  /* 针对本讲提问：只打开既有搜索浮层并带当前讲次范围，不新建聊天/问答通道 */
  const handleAskLecture = () => {
    const lecture = store.activeLecture;
    if (!lecture) return toast("请先选择讲次", "error");
    window.dispatchEvent(new CustomEvent("courselens:ask-lecture", {
      detail: {
        course_id: String(lecture.course_id || ""),
        sub_id: String(lecture.sub_id || ""),
        title: String(lecture.sub_title || ""),
      },
    }));
  };
  /* WAIT-UX-1 缺进度 E4：组卷是同步 POST 全程，此前只有按钮 busy。状态行点击
     即落「正在组卷…」，并按秒步进已用时间（诚实动态进度；AI 生成时长无实测
     分布，按 WAIT-UX-1 纪律不发明预计数字）。 */
  const handleGenerateQuiz = async () => {
    const lecture = store.activeLecture;
    if (!lecture) return toast("请先选择讲次", "error");
    setBusy($("generate-quiz"), true);
    const quizStatus = ensureWaitStatusLine("quiz-generate-status", $("quiz-list"), $("panel-review"));
    const quizStartedAt = Date.now();
    const setQuizStatus = () => {
      const elapsed = Math.floor((Date.now() - quizStartedAt) / 1000);
      quizStatus.hidden = false;
      quizStatus.textContent = elapsed > 0 ? `正在组卷…（已用 ${elapsed} 秒）` : "正在组卷…";
    };
    setQuizStatus();
    if (quizGenerateTimer) window.clearTimeout(quizGenerateTimer);
    const quizTick = () => {
      setQuizStatus();
      quizGenerateTimer = window.setTimeout(quizTick, QUIZ_STATUS_TICK_MS);
    };
    quizGenerateTimer = window.setTimeout(quizTick, QUIZ_STATUS_TICK_MS);
    try {
      await postV3("quizzes", { course_id: lecture.course_id, sub_id: lecture.sub_id });
      await loadQuiz(store);
      toast("测验已生成", "ready");
    } catch (error) {
      toastActionError(error);
    } finally {
      if (quizGenerateTimer) window.clearTimeout(quizGenerateTimer);
      quizGenerateTimer = 0;
      quizStatus.hidden = true;
      quizStatus.textContent = "";
      setBusy($("generate-quiz"), false);
    }
  };
  /* 课件 PDF：唯一入口是本讲课件区。动作语义由后端 operation 事实决定
     （暂停 → 续跑，否则新生成）；整个活动操作期间按钮保持禁用，重复点击
     不会产生第二个操作。轮询有界，讲次切换/卸载即停。 */
  const handleGenerateCoursewarePdf = async () => {
    const lecture = store.activeLecture;
    if (!lecture) return toast("请先选择讲次", "error");
    const button = $("generate-courseware-pdf");
    if (button.disabled) return;
    setBusy(button, true);
    /* 第十七案 b：点击即有回应，不等状态链路（POST 往返可能数秒） */
    const immediateState = $("courseware-pdf-state");
    if (immediateState) {
      coursewarePdfLastAnnounce = "";
      coursewarePdfAnnounce(immediateState, "正在开始生成本讲课件 PDF…");
    }
    button.textContent = "正在开始…";
    try {
      let status = null;
      try {
        status = await apiV3(`courseware-pdf?sub_id=${encodeURIComponent(lecture.sub_id)}`, { controller });
      } catch (error) {
        status = null;
      }
      const operation = status?.operation && typeof status.operation === "object" ? status.operation : null;
      if (operation && COURSEWARE_PDF_ACTIVE_STATES.has(String(operation.state || ""))) {
        /* 活动操作已存在：不发送第二个 POST，直接回到状态展示 */
        await loadCoursewarePdf(store);
        return;
      }
      const action = operation?.state === "paused" ? "resume" : "generate";
      await postV3("courseware-pdf/actions", {
        action,
        course_id: lecture.course_id,
        sub_id: lecture.sub_id,
      });
      toast(action === "resume" ? "课件 PDF 已继续" : "课件 PDF 已开始生成", "ready");
      /* 第卅案：提交成功的即时反馈双落点——
         ①状态行就地换「已提交排队」态（丢旧成品，旧页数/旧时间戳立即退场，
           不等状态链路）；②任务中心立刻拉一次（不等 30s 周期轮询）。
         随后乐观短轮询把行交给后端事实；状态 GET 竞态回旧成品也不再冻结。 */
      coursewarePdfLastAnnounce = "";
      renderCoursewarePdf({
        sub_id: lecture.sub_id,
        artifact: null,
        optimistic: true,
        operation: { state: "queued", label: "已提交，正在等待整理开始" },
      });
      window.dispatchEvent(new Event("courselens:tasks-refresh"));
      pollCoursewarePdfUntilVisible(store, String(lecture.sub_id || ""), COURSEWARE_PDF_OPTIMISTIC_POLL_ROUNDS);
    } catch (error) {
      toastActionError(error);
    } finally {
      setBusy(button, false);
      /* 恢复由操作事实决定的禁用态：活动操作期间按钮保持禁用；
         状态连续失败超限时保留「重新检查」入口 */
      renderCoursewarePdf(
        coursewarePdfLastStatus,
        lastKnownCoursewareOperationActive() && coursewarePdfStatusRetries >= COURSEWARE_PDF_MAX_STATUS_RETRIES
          ? { stale: true, manualRetry: true }
          : {},
      );
    }
  };
  const handleAnalyzeConcepts = async () => {
    const courseIds = store.courses.map((item) => String(item.course_id));
    try {
      await postV3("concepts/actions", { action: "analyze", course_ids: courseIds, operation_id: operationId("concept") });
      await loadConcepts(store);
      toast("课程关系已更新", "ready");
    } catch (error) {
      toastActionError(error);
    }
  };
  /* U8 资料分类学：类型闭集选择 + 课程级 scope；类型记忆上次选择（入口默认） */
  let lastDocumentType = "other";
  /* WAIT-UX-1 缺反馈 I1：选文件后此前全程零指示（大文件 POST 期全盲）。现在
     同步先落「正在导入」行内指示（与 change 事件同拍，≤100ms 三律），逐文件
     步进进度；完成后/被大小守卫拦下时收行，toast 语义不变。 */
  const handleDocumentInput = async (event) => {
    const lecture = store.activeLecture;
    const typeSelect = $("document-type-select");
    const courseScope = $("document-scope-course");
    const docType = String(typeSelect?.value || "other");
    lastDocumentType = docType;
    const asCourse = Boolean(courseScope?.checked);
    if (!asCourse && !lecture) return toast("请先选择讲次", "error");
    const course = store.activeCourse;
    const files = Array.from(event.target.files || []);
    const importStatus = files.length
      ? ensureWaitStatusLine("document-import-status", $("document-list"), $("imported-documents"))
      : null;
    const setImportStatus = (text) => {
      if (!importStatus) return;
      importStatus.hidden = !text;
      importStatus.textContent = text || "";
    };
    let fileIndex = 0;
    for (const file of files) {
      fileIndex += 1;
      if (file.size > 32 * 1024 * 1024) {
        setImportStatus("");
        toast(`${file.name} 超过大小限制`, "error");
        continue;
      }
      setImportStatus(files.length > 1
        ? `正在导入（${fileIndex}/${files.length}）${file.name}…`
        : `正在导入 ${file.name}…`);
      try {
        await postV3("documents", {
          action: "import",
          course_id: String(course?.course_id || lecture?.course_id || ""),
          sub_id: asCourse ? "" : String(lecture?.sub_id || ""),
          title: file.name,
          original_name: file.name,
          media_type: file.type,
          content_base64: bytesToBase64(await file.arrayBuffer()),
          doc_type: docType,
          scope: asCourse ? "course" : "lecture",
        });
        toast(`${file.name} 已导入`, "ready");
      } catch (error) {
        toastActionError(error);
      }
    }
    setImportStatus("");
    event.target.value = "";
    /* 甲-2：导入后筛选复位「全部类型」——刚导入的条目立刻可见，不被筛选藏住 */
    const kindFilterSelect = $("document-kind-filter");
    if (kindFilterSelect) kindFilterSelect.value = "all";
    await loadDocuments(store);
  };
  const handleDocumentKindFilter = () => void loadDocuments(store);
  const stopCatalogRefresh = () => {
    if (catalogRefreshTimer) window.clearTimeout(catalogRefreshTimer);
    catalogRefreshTimer = 0;
  };
  const catalogRefreshTimedOut = () => {
    stopCatalogRefresh();
    /* WAIT-UX-1：超时收口同时收掉长尾进度行（降级横幅+toast 接管语义） */
    stopCatalogRefreshProgress();
    /* 超时收口即复位重试计数：之后的任何 load（重新登录、手动刷新）重新获得完整重试窗口 */
    catalogRefreshRetries = 0;
    renderCourses(store, {
      state: "degraded", code: "catalog_timeout", actions: ["refresh-catalog"],
      courses: store.courses, course_count: store.courses.length,
    });
    toast("课程目录仍在确认，请稍后重试刷新", "error");
    window.dispatchEvent(new Event("courselens:catalog-timeout"));
  };
  const load = async ({ restoreProbe = false } = {}) => {
    catalogController?.abort();
    catalogController = new AbortController();
    void refreshAutomationSnapshot(); /* 目录读取同时刷新自动化快照（单一轮询者） */
    $("study-course-list").setAttribute("aria-busy", "true");
    try {
      const value = await apiV3("catalog?page_size=100", {
        controller: catalogController,
        /* O3-F4：挂起 fetch 20s 后由死线 signal 打断，收口进 catalogRefreshTimedOut */
        signal: AbortSignal.any([
          catalogController.signal,
          AbortSignal.timeout(CATALOG_FETCH_DEADLINE_MS),
        ]),
      });
      renderCourses(store, value);
      if (value.refreshing || value.state === "checking") {
        /* AS4-U1：恢复期单发探测不进 ready 态刷新窗的 1s 重试/超时降级——重复
           轮询轰炸本地服务，且慢验证尾段会伪造 catalog_timeout 失败；登录收敛
           时 auth 订阅自会用完整 load 换新目录。 */
        if (restoreProbe) {
          stopCatalogRefresh();
          stopCatalogRefreshProgress();
          catalogRefreshRetries = 0;
        } else if (catalogRefreshRetries >= CATALOG_REFRESH_MAX_RETRIES) catalogRefreshTimedOut();
        else {
          /* WAIT-UX-1 D1+：1s 轮询节拍即进度步进锚点——按在途含义给阶段文案
             （授权确认 vs 目录拉取）+已等待秒数，长尾窗不再只靠一次性 toast。 */
          if (!catalogRefreshStartedAt) catalogRefreshStartedAt = Date.now();
          catalogRefreshRetries += 1;
          setCatalogRefreshProgress(value.refreshing
            ? `正在拉取课程目录…${catalogRefreshElapsedText()}`
            : `正在确认课程授权…${catalogRefreshElapsedText()}`);
          stopCatalogRefresh();
          catalogRefreshTimer = window.setTimeout(load, CATALOG_REFRESH_INTERVAL_MS);
        }
      } else {
        stopCatalogRefresh();
        stopCatalogRefreshProgress();
        catalogRefreshRetries = 0;
        window.dispatchEvent(new Event("courselens:catalog-settled"));
      }
    } catch (error) {
      if (error.name !== "AbortError") catalogRefreshTimedOut();
    } finally {
      $("study-course-list").setAttribute("aria-busy", "false");
    }
  };
  const handleRefreshCatalog = async () => {
    /* OBS-4 在途闸：目录确认循环没跑完前再点不重复提交（文案话术同 F8a 先例） */
    if (catalogRefreshSubmitInFlight) {
      toast("目录已经在刷新了，等一下就好", "checking");
      return;
    }
    catalogRefreshSubmitInFlight = true;
    catalogRefreshSubmitGuardTimer = window.setTimeout(
      releaseCatalogRefreshSubmitGuard,
      CATALOG_REFRESH_MAX_RETRIES * CATALOG_REFRESH_INTERVAL_MS + CATALOG_FETCH_DEADLINE_MS + 5000,
    );
    setBusy($("refresh-catalog"), true);
    /* WAIT-UX-1 D1+：提交即落长尾进度行（后续由确认循环逐秒步进） */
    catalogRefreshStartedAt = Date.now();
    setCatalogRefreshProgress("正在刷新课程目录…");
    try {
      await postV3("authentication/actions", {
        action: "refresh-catalog",
        operation_id: operationId("catalog"),
      });
      toast("课程授权刷新已提交", "checking");
      window.dispatchEvent(new Event("courselens:auth-refresh"));
      catalogRefreshRetries = 0;
      window.dispatchEvent(new Event("courselens:catalog-refresh"));
    } catch (error) {
      releaseCatalogRefreshSubmitGuard();
      stopCatalogRefreshProgress();
      toastActionError(error);
    } finally {
      setBusy($("refresh-catalog"), false);
    }
  };
  const handleCopyCatalogDiagnostics = async () => {
    if (!latestCatalogDiagnostics) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(latestCatalogDiagnostics, null, 2));
      toast("脱敏诊断已复制", "ready");
    } catch {
      toast("诊断信息暂时无法复制，请稍后重试", "error");
    }
  };
  const handleCourseTermFilter = (event) => {
    courseTermFilter = event.target.value;
    localStorage.setItem(COURSE_TERM_FILTER_KEY, courseTermFilter);
    lastCatalogFingerprint = "";
    renderCourses(store, lastCatalogValue || { state: "ready", courses: store.courses, course_count: store.courses.length });
  };
  const unsubscribeAuth = store.subscribe("auth", (auth) => {
    /* 等待态裁决先于渲染去重指纹：restoring_trusted 不参与渲染形状（故不在指纹
       内），但它在中途到达时必须把等待中的用户立即接进目录（第卅九案）；
       abandon 先清等待标志，重复求值天然幂等。 */
    if (courseSelectAwaiting) {
      const trustedEntry = auth?.state === "checking" && auth?.restoring_trusted === true;
      if (auth?.state === "ready" || trustedEntry) {
        /* 就绪续接 / 可信快照续接：直接进入课程目录，绝不跳登录页 */
        abandonCourseSelectWait(false);
        enterCourseSelect();
      } else if (auth?.state && auth.state !== "checking") {
        abandonCourseSelectWait(true); /* 明确失败：立即转登录引导 */
      }
    }
    const fingerprint = JSON.stringify({
      state: auth?.state || "unknown",
      code: auth?.code || "",
      connected: Boolean(auth?.connected),
      configured: Boolean(auth?.configured),
      actions: auth?.actions || [],
    });
    if (fingerprint === lastAuthFingerprint) return;
    lastAuthFingerprint = fingerprint;
    renderStudyMode(store);
    if (auth?.state === "ready") void load();
    else if (auth?.state === "checking") {
      /* 校验在途/复用窗口重验是瞬时态：保留同身份已验证列表，等待确认结果；
         账号切换/登出会先落 confirmed 快照（client 丢弃 → action_required），走下方清空分支。 */
      /* AS4-U1：恢复期目录先行——checking（含 restoring_trusted）也照常发出装
         载，后端 P2-C 随 checking 信封回放本机身份域缓存，缓存课程即刻上屏；
         空缓存时保持「登录后显示」的诚实空态。冒充已验证语义零产生：信封的
         state/code 由后端闭集给出。 */
      renderCourses(store, {
        ...auth,
        courses: store.courses,
        course_count: store.courses.length,
        refreshing: true,
      });
      void load({ restoreProbe: true });
    } else {
      /* NAV-HANG-1①：非 ready/checking 态不再一刀切清目录——上游会话抖动
         （configured=true，本地凭据仍在）保留目录与讲次选择，登录前置门照常
         挡住需要身份的动作，auth 回 ready 后原桌原讲次直接回来；只有本地
         凭据消失（显式登出/切号 → fudan_credentials_missing）才清空，防上一
         位学生的选择态泄给下一位。 */
      const keepCatalog = auth?.configured === true;
      resetWatchProgressCache();
      renderCourses(store, {
        ...auth,
        courses: keepCatalog ? store.courses : [],
        course_count: keepCatalog ? store.courses.length : null,
      });
    }
  });
  const unsubscribeActiveCourseMode = store.subscribe("activeCourse", (course) => {
    /* 非自动选中的 activeCourse 写入=显式导航（搜索/课表/目录点击）解除落地门。
       WP1-D4②：程序化选课（搜索面板/课表跳转）与目录点行走同一条应用链——
       只解除落地门不渲染讲次面板，学生看到的就是「没切过去」。目录点行处理
       器随后重复这些步骤时，renderLectures 指纹守卫会短路，行为幂等。 */
    if (course && !autoSelectingCatalog) {
      studyLandingNavigated = true;
      courseChosen = true;
      setPressed($("study-course-list"), ".course-row-main", course.course_id, "courseId");
      renderLectures(store, course);
    }
    renderStudyMode(store);
    renderSelectLayout();
  });
  const unsubscribeActiveLecturePress = store.subscribe("activeLecture", (lecture) => {
    setPressed($("study-lecture-list"), ".lecture-row", lecture?.sub_id || "", "subId");
  });
  const unsubscribeActiveLecture = store.subscribe("activeLecture", handleActiveLecture);
  /* U1：目录到达/刷新/清空都重解析「继续学习」卡（换账号/目录瘦身即时收卡）。 */
  const unsubscribeContinueLearning = store.subscribe("courses", () => renderContinueLearning(store));
  /* 课程目录开关消费共享 store 的自动化快照；本模块是唯一轮询者并回写发布 */
  automationStoreRef = store;
  void refreshAutomationSnapshot();
  const unsubscribeAutomation = store.subscribe("automation", (value) => {
    automationSnapshot = value && typeof value === "object" ? value : null;
    if (automationSnapshot) {
      automationAttention = "";
      automationAttentionCode = "";
    }
    refreshAutomationToggles();
  });
  transcriptList.addEventListener("wheel", handleTranscriptUserIntent, { passive: true });
  transcriptList.addEventListener("touchstart", handleTranscriptUserIntent, { passive: true });
  transcriptList.addEventListener("pointerdown", handleTranscriptUserIntent, { passive: true });
  transcriptList.addEventListener("keydown", handleTranscriptUserIntent);
  bookmarkList.addEventListener("click", handleBookmarkAction);
  materialsTabs.addEventListener("click", handleMaterialTabClick);
  materialsTabs.addEventListener("keydown", handleMaterialTabKeydown);
  $("study-back-courses").addEventListener("click", handleBackToCourses);
  $("study-back-select").addEventListener("click", handleBackToSelect);
  $("study-start-select").addEventListener("click", handleStartSelect);
  $("study-continue")?.addEventListener("click", handleContinueLearning);
  $("refresh-catalog").addEventListener("click", handleRefreshCatalog);
  $("copy-catalog-diagnostics").addEventListener("click", handleCopyCatalogDiagnostics);
  courseTermFilter = localStorage.getItem(COURSE_TERM_FILTER_KEY) || CURRENT_TERM;
  $("catalog-term-filter").addEventListener("change", handleCourseTermFilter);
  window.addEventListener("courselens:transcript-time", handleTranscriptTime);
  window.addEventListener("courselens:watch-progress", handleWatchProgress);
  window.addEventListener("courselens:study-return-select", handleStudyReturnSelect);
  /* UIAUDIT-1 F15：页面切换同步面包屑——离开学习页即收起，回到学习页按
     当前 studyMode/讲次恢复，不残留其他页面上的上一讲上下文 */
  const handleTopbarPageForCrumbs = (event) => {
    topbarActivePage = String(event?.detail || "study");
    renderTopbarCrumbs(store);
  };
  window.addEventListener("courselens:page", handleTopbarPageForCrumbs);
  /* D2：播放器「没听懂」标记落库后刷新书签列表（零新增请求面，复用 loadBookmarks） */
  const handleBookmarksChanged = () => {
    void loadBookmarks(store, { preserveOnFailure: true });
  };
  window.addEventListener("courselens:bookmarks-changed", handleBookmarksChanged);
  /* 甲-1b：课件 PDF 任务完成事件（任务中心发现）→ 资料区即刻换新，不再
     滞留旧页数/旧时间戳等手动刷新。sub_id 定向：非当前讲次静默忽略。 */
  const handleMaterialsRefresh = (event) => {
    const detail = event?.detail && typeof event.detail === "object" ? event.detail : {};
    const lecture = store.activeLecture;
    if (!lecture) return;
    if (String(detail.sub_id || "") && String(detail.sub_id) !== String(lecture.sub_id || "")) return;
    void loadCoursewarePdf(store);
    void loadDocuments(store);
  };
  window.addEventListener("courselens:materials-refresh", handleMaterialsRefresh);
  /* SWEEPFIX-R2 W1（D-20261009-15①）：字幕任务完成跃迁（任务中心广播）→
     文稿热重读。学生停在讲次上字幕生成完成时，文稿与笔记按钮即刻可用，
     不再要求「重进讲次」。sub_id 定向不误刷（同 handleMaterialsRefresh 口径）；
     时轴事实已在本场（transcriptHasTiming=true）不重读——重进/手动刷新链
     零扰动。loadTranscript 内部 epoch 守卫自照旧。 */
  const handleTranscriptRefresh = (event) => {
    const detail = event?.detail && typeof event.detail === "object" ? event.detail : {};
    const lecture = store.activeLecture;
    if (!lecture) return;
    if (String(detail.sub_id || "") && String(detail.sub_id) !== String(lecture.sub_id || "")) return;
    if (store.transcriptHasTiming === true) return;
    void loadTranscript(store, instanceEpoch);
  };
  window.addEventListener("courselens:transcript-refresh", handleTranscriptRefresh);
  const handleCatalogRefresh = () => {
    catalogRefreshRetries = 0;
    void load();
  };
  window.addEventListener("courselens:catalog-refresh", handleCatalogRefresh);
  /* OBS-4：在途闸随目录确认循环收口释放（成功 settled / 超时 timeout 两既有事件） */
  window.addEventListener("courselens:catalog-settled", releaseCatalogRefreshSubmitGuard);
  window.addEventListener("courselens:catalog-timeout", releaseCatalogRefreshSubmitGuard);
  drilldownQuery = matchMedia("(max-width: 719px)");
  drilldownQuery.addEventListener("change", renderSelectLayout);
  /* SIMPLIFY-AUDIT-1 S2：手动「刷新字幕」图标钮已退役——进讲次自动载入 +
     字幕生成完成事件链热重读（上方 courselens:transcript-refresh 订阅）已覆盖。 */
  const transcriptSearchInput = $("transcript-search-input");
  if (transcriptSearchInput) {
    transcriptSearchInput.addEventListener("input", handleTranscriptSearchInput);
    transcriptSearchInput.addEventListener("keydown", handleTranscriptSearchKeydown);
  }
  $("artifact-kind").addEventListener("change", handleArtifactKind);
  $("ask-lecture")?.addEventListener("click", handleAskLecture);
  $("generate-quiz").addEventListener("click", handleGenerateQuiz);
  $("generate-courseware-pdf")?.addEventListener("click", handleGenerateCoursewarePdf);
  $("analyze-concepts").addEventListener("click", handleAnalyzeConcepts);
  $("document-input").addEventListener("change", handleDocumentInput);
  $("document-kind-filter")?.addEventListener("change", handleDocumentKindFilter);
  selectMaterialTab("transcript");
  /* N7F：总体复习 surface 的组合根在本模块（app.js 不在本包路径内）；
     证据出口在安装前注入，避免 course-review.js 反向 import 本模块。 */
  installReviewNavigation(store);
  const disposeCourseReview = installCourseReview(store) || (() => {});
  const disposeStudyStats = installStudyStats(store) || (() => {});
  renderSelectLayout();
  renderStudyMode(store);
  renderContinueLearning(store); /* U1：着陆「继续学习」弱化链首渲染（目录后到由订阅补渲染） */
  if (store.auth?.state === "ready") await load();
  else if (store.auth?.state === "checking") void load({ restoreProbe: true }); /* AS4-U1：恢复期挂载也先行探测 */
  return () => {
    if (disposed) return;
    disposed = true;
    clearPendingTranscriptSeek();
    disposeCourseReview();
    disposeStudyStats();
    catalogController?.abort();
    stopCatalogRefresh();
    controller?.abort();
    artifactController?.abort();
    irController?.abort();
    if (instanceEpoch === studyInstanceEpoch) {
      studyInstanceEpoch += 1;
      resetTranscriptLoading();
    }
    resetBookmarkLoading();
    resetBookmarkActions();
    resetTranscriptFollow();
    resetTranscriptSearch();
    transcriptList.removeEventListener("wheel", handleTranscriptUserIntent);
    transcriptList.removeEventListener("touchstart", handleTranscriptUserIntent);
    transcriptList.removeEventListener("pointerdown", handleTranscriptUserIntent);
    transcriptList.removeEventListener("keydown", handleTranscriptUserIntent);
    bookmarkList.removeEventListener("click", handleBookmarkAction);
    materialsTabs.removeEventListener("click", handleMaterialTabClick);
    materialsTabs.removeEventListener("keydown", handleMaterialTabKeydown);
    $("study-back-courses").removeEventListener("click", handleBackToCourses);
    drilldownQuery?.removeEventListener("change", renderSelectLayout);
    $("study-back-select").removeEventListener("click", handleBackToSelect);
    $("study-start-select").removeEventListener("click", handleStartSelect);
    $("study-continue")?.removeEventListener("click", handleContinueLearning);
    $("refresh-catalog").removeEventListener("click", handleRefreshCatalog);
    $("copy-catalog-diagnostics").removeEventListener("click", handleCopyCatalogDiagnostics);
    $("catalog-term-filter").removeEventListener("change", handleCourseTermFilter);
    window.removeEventListener("courselens:transcript-time", handleTranscriptTime);
    window.removeEventListener("courselens:watch-progress", handleWatchProgress);
    window.removeEventListener("courselens:page", handleTopbarPageForCrumbs);
    window.removeEventListener("courselens:bookmarks-changed", handleBookmarksChanged);
  window.removeEventListener("courselens:study-return-select", handleStudyReturnSelect);
    window.removeEventListener("courselens:materials-refresh", handleMaterialsRefresh);
    window.removeEventListener("courselens:transcript-refresh", handleTranscriptRefresh);
    window.removeEventListener("courselens:catalog-refresh", handleCatalogRefresh);
    window.removeEventListener("courselens:catalog-settled", releaseCatalogRefreshSubmitGuard);
    window.removeEventListener("courselens:catalog-timeout", releaseCatalogRefreshSubmitGuard);
    if (catalogRefreshSubmitGuardTimer) window.clearTimeout(catalogRefreshSubmitGuardTimer);
    unsubscribeAutomation();
    unsubscribeAuth();
    if (courseSelectWaitTimer) {
      window.clearTimeout(courseSelectWaitTimer);
      courseSelectWaitTimer = 0;
    }
    courseSelectAwaiting = false;
    unsubscribeActiveCourseMode();
    unsubscribeActiveLecturePress();
    unsubscribeActiveLecture();
    unsubscribeContinueLearning();
    if (transcriptSearchInput) {
      transcriptSearchInput.removeEventListener("input", handleTranscriptSearchInput);
      transcriptSearchInput.removeEventListener("keydown", handleTranscriptSearchKeydown);
    }
    $("artifact-kind").removeEventListener("change", handleArtifactKind);
    $("ask-lecture")?.removeEventListener("click", handleAskLecture);
    $("generate-quiz").removeEventListener("click", handleGenerateQuiz);
    $("generate-courseware-pdf")?.removeEventListener("click", handleGenerateCoursewarePdf);
    stopCoursewarePdfPolling();
    $("analyze-concepts").removeEventListener("click", handleAnalyzeConcepts);
    $("document-input").removeEventListener("change", handleDocumentInput);
    $("document-kind-filter")?.removeEventListener("change", handleDocumentKindFilter);
  };
}

/* ---- 考核雷达（N5A-P1 规则版）：老师课上口头提到的考试/作业等本地事件，
   学生确认/忽略两钮定去留；与校方 #exam-context 并列，互不覆写。 ---- */

const ASSESSMENT_LABELS = Object.freeze({
  exam: "考试", resit: "补考", quiz: "小测", assignment: "作业", project: "大作业/项目",
  lab: "实验", computer_lab: "上机", attendance: "考勤", rollcall: "点名",
  schedule_change: "调课", qa_session: "答疑",
});

function assessmentDueText(event, nowSeconds = Date.now() / 1000) {
  const stamp = Number(event.due_at);
  if (!Number.isFinite(stamp) || stamp <= 0) return "时间待定";
  const day = new Date(stamp * 1000);
  const base = `时间 ${day.getMonth() + 1}月${day.getDate()}日`;
  /* P13-B 合同③3：仅已确认且未过期的行升级「N 天后」；due_at 是当日 09:00
     的日粒度口径，不做小时级承诺；N≤0 不显示后缀。conflict_note 行不动。 */
  if (event.status !== "confirmed" || event.expired || event.conflict_note) return base;
  const days = Math.ceil((stamp - nowSeconds) / 86400);
  return days > 0 ? `${base} · ${days} 天后` : base;
}

function assessmentRow(event, onChange) {
  const row = document.createElement("div");
  row.className = "assessment-event";
  row.dataset.eventId = String(event.event_id || "");
  if (event.status === "confirmed") row.classList.add("confirmed");
  const label = ASSESSMENT_LABELS[event.category] || String(event.category || "");
  row.append(textElement("span", `${label} · ${String(event.title || "")} ${assessmentDueText(event)}`));
  if (event.conflict_note) row.append(textElement("span", "时间有出入，以校方考试安排为准"));
  if (event.status === "unconfirmed" || event.status === "active") {
    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className = "text-button";
    confirmButton.textContent = "确认";
    confirmButton.addEventListener("click", () => void assessmentAction(event.event_id, "confirm", onChange));
    const dismissButton = document.createElement("button");
    dismissButton.type = "button";
    dismissButton.className = "text-button";
    dismissButton.textContent = "忽略";
    dismissButton.addEventListener("click", () => void assessmentAction(event.event_id, "dismiss", onChange));
    row.append(confirmButton, dismissButton);
  } else if (event.status === "confirmed") {
    row.append(textElement("span", "已确认"));
  }
  return row;
}

async function assessmentAction(eventId, action, onChange) {
  try {
    await postV3("assessment/actions", {
      action, event_id: eventId, operation_id: operationId("assessment"),
    });
    if (typeof onChange === "function") onChange();
  } catch (error) {
    toast(error.message || "操作没有完成，请稍后再试", "error");
  }
}

/* 「排进计划」候选（P13-B 合同③4）：确认门硬闸——只有已确认且带日期的
   未过期事件参与联动；时间待定不计数，dismissed/expired 永不出现。 */
function assessmentPlanCandidates(events) {
  return (Array.isArray(events) ? events : []).filter((event) => {
    if (String(event?.status || "") !== "confirmed" || event?.expired) return false;
    const stamp = Number(event?.due_at);
    return Number.isFinite(stamp) && stamp > 0;
  });
}

/* 排进计划成功后的刷新：与 loadQuiz 的 plans 装载段同式（review-plans GET →
   renderReviewPlans + renderExamContext），不重跑 quiz 装载；失败落既有错误
   空态文本（POST 本身已有人话 toast，不叠加双份报错）。 */
async function refreshReviewPlans(store) {
  const reviewTarget = $("review-list");
  if (!reviewTarget) return;
  const requestCourseId = String(store.activeLecture?.course_id || "");
  const requestSubId = String(store.activeLecture?.sub_id || "");
  try {
    const plans = await apiV3("review-plans", { controller });
    if (String(store.activeLecture?.course_id || "") !== requestCourseId
      || String(store.activeLecture?.sub_id || "") !== requestSubId) return;
    renderReviewPlans(store, reviewTarget, plans.plans || [], store.activeLecture);
    renderExamContext(store, plans.plans || []);
  } catch (error) {
    if (error?.name === "AbortError") return;
    emptyStateWithAction(reviewTarget, "复习计划暂时无法加载", "重试", () => void loadQuiz(store), "error-text");
  }
}

function assessmentPlanButton(store, events) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "text-button assessment-plan-action";
  button.textContent = "把已确认的考核排进复习计划";
  button.addEventListener("click", () => {
    const candidates = assessmentPlanCandidates(events);
    const courseId = String(store.activeCourse?.course_id || "");
    if (button.disabled || !candidates.length || !courseId) return;
    button.disabled = true; /* 防抖冻结：在途禁用直到响应返回（在途闸的 UX 消化） */
    void (async () => {
      try {
        /* 载荷按合同③4 冻结：单课计划走 course_id（不传 course_scope），
           exam_at=最临近已确认考核；成功后保持禁用至下次重渲染，不诱导重复建计划。 */
        await postV3("review-plans", {
          title: "考核复习计划",
          exam_at: Number(candidates[0].due_at),
          available_minutes: 60,
          course_id: courseId,
          strategy: "coverage",
        });
        toast("已生成复习计划，已确认的考核已排进步骤", "ready");
        await refreshReviewPlans(store);
      } catch (error) {
        button.disabled = false;
        toast(error.message || "操作没有完成，请稍后再试", "error");
      }
    })();
  });
  return button;
}

async function renderAssessmentRadar(store) {
  const course = store.activeCourse;
  const lecture = store.activeLecture;
  const desk = $("assessment-desk-radar");
  const summaryMount = $("assessment-summary-radar");
  if ((!desk && !summaryMount) || !course) return;
  let events = [];
  try {
    const payload = await apiV3(`assessment?course_id=${encodeURIComponent(String(course.course_id || ""))}`);
    events = Array.isArray(payload?.events) ? payload.events : [];
  } catch {
    events = [];
  }
  const upcoming = events.filter((event) => event.status !== "dismissed" && !event.expired);
  /* 联动激活通道按「desk 数据」（本课未忽略、未过期全量）判定，不受 top5 渲染帽
     影响——渲染帽只是显示截断，不该让已确认考核失去一键入口（P13-B 合同③4）；
     按钮只落 desk，summary 挂载不渲染。 */
  const planActionReady = String(course?.course_id || "") !== ""
    && assessmentPlanCandidates(upcoming).length > 0;
  const render = (mount, list, hiddenWhenEmpty, options = {}) => {
    if (!mount) return;
    clear(mount);
    if (!list.length) {
      mount.hidden = hiddenWhenEmpty;
      return;
    }
    mount.append(textElement("p", "这门课最近要交要考（老师课上提到）"));
    list.forEach((event) => mount.append(assessmentRow(event, () => void renderAssessmentRadar(store))));
    if (options.planAction && planActionReady) mount.append(assessmentPlanButton(store, upcoming));
    mount.hidden = false;
  };
  render(desk, upcoming.slice(0, 5), true, { planAction: true });
  const currentSub = lecture ? String(lecture.sub_id) : "";
  render(summaryMount, upcoming.filter((event) => event.last_seen_sub_id === currentSub).slice(0, 4), true);
  /* desk 渲染后收口 exam-context 节显隐（F3-P1-2）：雷达有内容即开节；
     全部确认/忽略后雷达空，节随 row/list 同谓词裁决自然收起。 */
  syncExamContextVisibility();
}
