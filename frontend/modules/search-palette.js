import { apiV3, friendlyError, postV3 } from "./api.js";

import { DEEP_ANSWER_EXPECTATION, REMOTE_CHANNEL_WARMING_HINT } from "./wait-expectations.js";

import { taskEtaText } from "./tasks-drawer/task-cards.js";

import { $, clear, evidenceText, friendlyTerm, openOverlay, closeOverlay, operationId, setBusy, textElement, toast } from "./ui.js";

let controller = null;
let quickTimer = 0;
let requestSeq = 0;
let paletteMode = "compact";
let paletteTrigger = null;
let indexState = null;
let currentItems = [];
/* 键盘激活的解析序=渲染序（renderOptions 按 GROUP_ORDER 重排 DOM），
   与 currentItems 的拼装序不同序——按拼装序取激活项会错激活别的条目（WP1-D4①）。 */
let renderedItems = [];
let activeIndex = -1;
/* “针对本讲提问”范围：只影响状态提示文案；回答的讲次范围始终来自
   store.activeLecture（runAnswer 的 sub_id），不新增问答通道。 */
let lectureScope = null;

/* P3 深度回答（P3-CONTRACT-1 §④）：opt-in 云端问答链——点击一次 = 一次调用。
   轮询节律与上限冻结：3s 一询、10 分钟封顶（与云端 job expires 600s 同窗）；
   浮层关闭即停轮询并 abort，任务在服务端继续（任务抽屉可见）。 */
/* U1 实测估计律：数字只从 wait-expectations.js 取（量源与重测日期见该文件锚点注） */
const DEEP_ANSWER_COST_CAVEAT = `深度回答会使用你的云端计算与 DeepSeek 额度（每次点击一次调用），${DEEP_ANSWER_EXPECTATION}。`;
const DEEP_RESULT_CAVEAT = "回答由模型基于检索证据生成，不作为检索事实；引用可跳回原视频位置核对。";
const QUICK_ANSWER_CAVEAT = "回答由本地拼装生成，不作为检索事实；证据不足时会明确提示。";
const DEEP_POLL_INTERVAL_MS = 3000;
const DEEP_POLL_DEADLINE_MS = 600000;
/* U3 就绪窗守卫上限：观测就绪窗 ~25s 的约 2 倍工程停线（非实测 ETA，
   文案数字见 wait-expectations.js REMOTE_CHANNEL_WARMING_HINT 锚点注）。 */
const REMOTE_CHANNEL_WARMING_DEADLINE_MS = 60000;
let deepTimer = 0;
let deepTaskId = "";
/* U3 可唤醒睡眠句柄：浮层关闭/换道时 stopDeepPolling 立即放行等待中的守卫 */
let deepWaitWake = null;
/* DeepSeek 配置态（null=未知）：读不到设置时按钮保持可用（fail-open），
   未配 key 时点击会收到 question_explanation_not_configured 既有文案。 */
let deepseekConfigured = null;

const SEARCH_INDEX_LABELS = Object.freeze({
  ready: "索引已就绪",
  indexing: "正在建立索引",
  action_required: "索引需要处理",
  disabled: "索引未启用",
});

const GROUP_ORDER = ["课程", "字幕时间点", "课程讲次", "笔记 · 书签 · 资料", "操作"];

function authReady(store) {
  return store.auth?.state === "ready";
}

/* 讲次范围提示只在范围与活动讲次一致时显示；讲次已切换即静默失效 */
function lectureScopeHint(store) {
  if (!lectureScope) return "";
  const lecture = store.activeLecture;
  if (!lecture || String(lecture.sub_id || "") !== lectureScope.sub_id) return "";
  return lectureScope.title ? `将针对本讲「${lectureScope.title}」回答与检索。` : "将针对当前讲次回答与检索。";
}

function renderAuthState(store) {
  const status = $("palette-status");
  if (status) status.textContent = evidenceText(store.auth || { state: "action_required", code: "fudan_login_required" }) + "。登录后可搜索课程内容。";
}

function renderResultState(message) {
  const state = $("palette-status");
  state.hidden = !message;
  state.textContent = message || "";
}

/* 目的地解析：搜索结果 → 目录内可映射的最近可达位置 */
function jumpTarget(store, result) {
  const course = (store.courses || []).find((item) => String(item.course_id || "") === String(result.course_id || ""));
  if (!course) return { kind: "select" };
  const lecture = (course.lectures || []).find((item) => String(item.sub_id || "") === String(result.sub_id || ""));
  if (result.sub_id && lecture) {
    return { kind: "desk", course, lecture };
  }
  return { kind: "lectures", course };
}

function jumpTo(store, target) {
  if (target.kind === "desk") {
    store.set("activeCourse", target.course);
    store.set("activeLecture", {
      ...target.lecture,
      course_id: target.course.course_id,
      course_title: target.course.title,
    });
  } else if (target.kind === "lectures") {
    store.set("activeCourse", target.course);
  }
  window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));
}

/* 引用 → 可映射的依据跳转：目录内课程与讲次都可解析（desk）且锚点是合法的
   绝对秒值。其余一律 null，引用保持纯文本标签，绝不猜测课程/讲次/时间。 */
export function citationSeekTarget(store, citation) {
  const target = jumpTarget(store, citation);
  if (target.kind !== "desk") return null;
  const startSeconds = Number(citation?.start_seconds);
  if (!Number.isFinite(startSeconds) || startSeconds < 0) return null;
  return { target, startSeconds };
}

function catalogJumpItems(store, query) {
  const items = [];
  const needle = query.trim().toLowerCase();
  (store.courses || []).forEach((course) => {
    const courseHit = String(course.title || "").toLowerCase().includes(needle)
      || String(course.teacher || "").toLowerCase().includes(needle);
    /* 课程级条目：唯一搜索入口下，按课程名/教师命中时提供落在讲次列表的课程跳转（每课程至多 1 条） */
    if (needle && courseHit && items.length < 8) {
      items.push({
        group: "课程",
        title: course.title || `课程 ${course.course_id}`,
        /* U⑬：检索上下文的学期串人话化 */
        context: [course.teacher, friendlyTerm(course.term)].filter(Boolean).join(" · "),
        dest: "学习 · 讲次列表",
        closes: true,
        run: (storeRef) => {
          storeRef.set("activeCourse", course);
          window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));
        },
      });
    }
    (course.lectures || []).forEach((lecture) => {
      if (items.length >= 8) return;
      if (needle && !courseHit && !String(lecture.sub_title || "").toLowerCase().includes(needle)) return;
      items.push({
        group: "课程讲次",
        title: `${course.title || "课程"} · ${lecture.sub_title || "讲次"}`,
        /* dest 已标「学习桌」：context 再写一遍「跳转到学习桌」是逐行复读
           （夜4 审美四问），留空即不渲染次行 */
        context: "",
        dest: "学习桌",
        closes: true,
        run: (storeRef) => {
          storeRef.set("activeCourse", course);
          storeRef.set("activeLecture", { ...lecture, course_id: course.course_id, course_title: course.title });
          window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));
        },
      });
    });
  });
  return items;
}

function renderOptions(items) {
  const listbox = $("palette-listbox");
  clear(listbox);
  activeIndex = -1;
  renderedItems = [];
  let index = 0;
  GROUP_ORDER.forEach((groupName) => {
    const groupItems = items.filter((item) => item.group === groupName);
    if (!groupItems.length) return;
    const wrap = document.createElement("li");
    wrap.setAttribute("role", "group");
    const labelId = "palette-group-" + GROUP_ORDER.indexOf(groupName);
    wrap.setAttribute("aria-labelledby", labelId);
    const label = textElement("span", groupName, "palette-group-label");
    label.id = labelId;
    const inner = document.createElement("ul");
    groupItems.forEach((item) => {
      item._index = index;
      renderedItems.push(item);
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = "palette-opt-" + index;
      li.className = "palette-option";
      li.setAttribute("aria-selected", "false");
      li.append(
        textElement("span", item.title, "o-title"),
        textElement("span", item.dest, "o-dest"),
        textElement("span", item.context, "o-context"),
      );
      li.addEventListener("click", () => executeItem(item));
      inner.append(li);
      index += 1;
    });
    wrap.append(label, inner);
    listbox.append(wrap);
  });
  if (!items.length) {
    listbox.append(textElement("li", "没有匹配结果。可调整关键词，或在全部结果中检索。", "palette-empty"));
  }
}

function setActive(index) {
  const nodes = document.querySelectorAll("#palette-listbox [role='option']");
  nodes.forEach((node) => {
    node.classList.remove("active");
    node.setAttribute("aria-selected", "false");
  });
  activeIndex = index;
  const el = index >= 0 ? document.getElementById("palette-opt-" + index) : null;
  if (el) {
    el.classList.add("active");
    el.setAttribute("aria-selected", "true");
    $("palette-input").setAttribute("aria-activedescendant", el.id);
    el.scrollIntoView({ block: "nearest" });
  } else {
    $("palette-input").setAttribute("aria-activedescendant", "");
  }
}

function setMode(mode) {
  paletteMode = mode;
  const panel = $("palette-panel");
  panel.dataset.mode = mode;
  panel.classList.toggle("mode-full", mode !== "compact");
  $("palette-head").hidden = mode === "compact";
  $("palette-mode-title").textContent = mode === "answer" ? "证据回答" : "全部结果";
  const answerVisible = mode === "answer";
  $("palette-answer-card").hidden = !answerVisible;
  /* 双动作 IA（P3-CONTRACT-1 §④）：快速/深度两按钮随 palette-head 显隐，
     answer 模式下并存——快速是零成本默认面，深度是显式 opt-in。 */
  $("palette-answer").hidden = false;
  $("palette-answer-deep").hidden = false;
  $("palette-collapse").hidden = mode === "compact";
}

function executeItem(item) {
  /* 跳转类条目先关闭 palette（恢复 inert 与焦点到触发器），再执行导航 */
  if (item.closes !== false) closePalette();
  item.run(storeRef);
  /* D11：课程类条目的 run 会同步触发目录/页面渲染订阅链——跳转类条目执行
     收口时面板必须保持关闭；同步链里任何路径把面板带回，这里兜底再关一次。 */
  if (item.closes !== false && !$("palette-root").hidden) closePalette();
}

function resultItem(store, value) {
  const target = jumpTarget(store, value);
  const dest = target.kind === "desk" ? "学习桌 · 字幕"
    : target.kind === "lectures" ? "学习 · 讲次列表" : "学习 · 课程选择";
  const contextParts = [value.course_title, value.lecture_title || value.document_title, value.snippet];
  return {
    group: value.source === "summary" ? "笔记 · 书签 · 资料" : "字幕时间点",
    title: value.lecture_title || value.document_title || value.course_title || "搜索结果",
    context: contextParts.filter(Boolean).join(" · "),
    dest,
    run: () => jumpTo(store, target),
    closes: true,
  };
}

async function runCompact(store) {
  const seq = ++requestSeq;
  setMode("compact");
  const query = $("palette-input").value.trim();
  const items = catalogJumpItems(store, query);
  const actions = [
    {
      group: "操作", title: "在全部结果中检索" + (query ? `「${query}」` : ""),
      context: "完整检索 · 最多返回 50 条全文", dest: "完整检索", /* SAR-P3-15：参数字样人话化（夜批15 R5 E12） */
      run: () => void runFull(store), closes: false,
    },
    {
      group: "操作", title: "生成证据回答" + (query ? `「${query}」` : ""),
      context: "基于检索证据的本地拼装", dest: "证据回答",
      run: () => void runAnswer(store), closes: false,
    },
    {
      group: "操作", title: "打开今日与本周安排",
      context: "课表上下文在学习选择页", dest: "学习 · 课表",
      closes: true,
      run: () => {
        window.dispatchEvent(new CustomEvent("courselens:select-page", { detail: "study" }));
        window.dispatchEvent(new CustomEvent("courselens:open-schedule"));
      },
    },
  ];
  const cap = Math.max(0, 8 - actions.length);
  const merged = items.slice(0, cap);
  if (!authReady(store)) {
    currentItems = [...actions.slice(0, 1), {
      group: "操作", title: "登录复旦课程平台",
      context: "登录后可搜索课程内容", dest: "登录",
      closes: true,
      run: () => { window.dispatchEvent(new CustomEvent("courselens:open-login", { detail: "login" })); },
    }];
    renderOptions(currentItems);
    renderAuthState(store);
    setActive(currentItems.length ? 0 : -1);
    return;
  }
  currentItems = [...actions, ...merged];
  renderOptions(currentItems);
  if (query.length < 2) {
    renderResultState("继续输入以搜索全文，或直接跳转。" + lectureScopeHint(store));
    setActive(0);
    return;
  }
  if (controller) controller.abort();
  controller = new AbortController();
  const local = controller;
  renderResultState("正在搜索…");
  try {
    const value = await apiV3(`search?q=${encodeURIComponent(query)}&limit=8`, { controller: local });
    if (seq !== requestSeq || paletteMode !== "compact") return;
    const extra = (value.results || value.items || [])
      .map((row) => resultItem(store, row))
      .slice(0, Math.max(0, 8 - currentItems.length));
    currentItems = [...currentItems, ...extra];
    renderOptions(currentItems);
    renderResultState((currentItems.length > actions.length
      ? `目录与全文共 ${currentItems.length - actions.length} 条结果（首屏至多 8 项）`
      : "没有匹配结果，可调整关键词或在全部结果中检索。") + lectureScopeHint(store));
    setActive(0);
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq) return;
    /* AVATAR-POLISH-1 OBS-10（化身走查 OBS-10）：超长关键词会被服务端闭集
       拒绝（search_request_invalid，归一化后至多 100 字），这种情况重试没有
       意义——直接说清原因和出路，不再让「稍后重试」误导；其余失败维持既有
       兜底文案。 */
    renderResultState(
      String(error?.code || "") === "search_request_invalid" && query.length > 100
        ? "关键词太长了，试试更短的词。"
        : "搜索暂时无法完成，可稍后重试或进入完整检索。",
    );
    setActive(0);
  }
}

async function runFull(store) {
  const seq = ++requestSeq;
  setMode("full");
  const query = $("palette-input").value.trim();
  $("palette-answer").disabled = true;
  const pill = $("palette-index-pill");
  if (query.length < 2) {
    renderOptions([]);
    renderResultState("继续输入以搜索全文，或直接跳转。");
    setActive(-1);
    return;
  }
  if (!authReady(store)) {
    renderOptions([]);
    renderAuthState(store);
    setActive(-1);
    return;
  }
  if (controller) controller.abort();
  controller = new AbortController();
  const indexController = controller;
  try {
    indexState = await apiV3("search-index", { controller: indexController });
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "full") return;
    indexState = null;
    renderOptions([]);
    renderResultState(error.message);
    setActive(-1);
    return;
  }
  if (seq !== requestSeq || paletteMode !== "full") return;
  const indexLabel = SEARCH_INDEX_LABELS[indexState.state] || "索引状态未知";
  pill.textContent = `${indexLabel}${Number(indexState.total) ? ` · ${indexState.processed || 0}/${indexState.total}` : ""}`;
  if (indexState.state !== "ready") {
    renderOptions([]);
    renderResultState(`${indexLabel}，暂时无法检索。`);
    setActive(-1);
    return;
  }
  if (controller) controller.abort();
  controller = new AbortController();
  const local = controller;
  renderResultState("正在检索…");
  try {
    const value = await apiV3(`search?q=${encodeURIComponent(query)}&limit=50`, { controller: local });
    if (seq !== requestSeq || paletteMode !== "full") return;
    const rows = value.results || value.items || [];
    currentItems = rows.map((row) => resultItem(store, row));
    renderOptions(currentItems);
    if (!currentItems.length) {
      renderResultState("没有匹配结果。");
      setActive(-1);
      return;
    }
    renderResultState(`全文结果 ${currentItems.length} 条 · ${indexLabel}`); /* SAR-P3-15：去「（limit=50）」参数字样（夜批15 R5 E12） */
    $("palette-answer").disabled = !currentItems.length;
    setActive(0);
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "full") return;
    renderOptions([]);
    renderResultState("检索失败：网络或索引暂不可用，可稍后重试。");
    setActive(-1);
  }
}

/* 引用渲染（快速/深度同构复用，P3-CONTRACT-1 §④「渲染复用」）：可映射引用=
   citation-action 按钮 seek 回原位；不可映射=纯文本标签，绝不猜测。
   deep=true 时 chip 点击补一发 H1 埋点（fire-and-forget，遥测绝不挡主链）。 */
function renderCitations(store, citations, { deep = false } = {}) {
  const cites = $("palette-answer-cites");
  cites.textContent = "";
  (citations || []).forEach((citation) => {
    const label = `${citation.source || "evidence"} · ${citation.label || citation.timestamp || ""}`;
    const mapped = citationSeekTarget(store, citation);
    if (!mapped) {
      cites.append(textElement("span", label));
      return;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "citation-action";
    button.textContent = label;
    button.addEventListener("click", () => {
      if (deep) {
        postV3("analytics/study-events", {
          kind: "palette_citation_click",
          course_id: String(citation?.course_id || ""),
          sub_id: String(citation?.sub_id || ""),
          dwell_ms: 0,
        }).catch(() => {});
      }
      /* 与目录跳转条目同一模式：先关浮层（焦点归还触发器）再导航，并 seek 到引用锚点 */
      executeItem({
        closes: true,
        run: () => {
          jumpTo(store, mapped.target);
          $("player-stage").currentTime = mapped.startSeconds;
        },
      });
    });
    cites.append(button);
  });
}

function setAnswerCardFace({ label, body, caveat = "", showCancel = false }) {
  $("palette-answer-label").textContent = label;
  $("palette-answer-body").textContent = body;
  $("palette-answer-cites").textContent = "";
  $("palette-answer-caveat").textContent = caveat;
  $("palette-answer-cancel").hidden = !showCancel;
}

function renderAnswerResult(store, value, { deep = false, caveat = "" } = {}) {
  $("palette-answer-label").textContent = deep ? "深度回答 · 模型生成" : "证据回答 · 本地拼装";
  $("palette-answer-body").textContent = String(
    value?.answer || value?.message || (value?.task ? "回答任务已提交" : "证据不足，未生成回答"),
  );
  renderCitations(store, value?.citations, { deep });
  $("palette-answer-caveat").textContent = caveat;
  $("palette-answer-cancel").hidden = true;
}

function renderAnswerBackAction(store, seq) {
  if (seq !== requestSeq || paletteMode !== "answer") return;
  currentItems = [{
    group: "操作", title: "返回全部结果", context: "回到完整检索。", dest: "返回",
    run: () => void runFull(store), closes: false,
  }];
  renderOptions(currentItems);
  setActive(0);
}

async function runAnswer(store) {
  const seq = ++requestSeq;
  if (controller) controller.abort();
  controller = new AbortController();
  const local = controller;
  stopDeepPolling();
  setMode("answer");
  const query = $("palette-input").value.trim();
  if (!query || query.length < 2) {
    renderResultState("继续输入以搜索全文，或直接跳转。");
    setActive(0);
    return;
  }
  if (!authReady(store)) {
    renderResultState(evidenceText(store.auth || { state: "action_required", code: "fudan_login_required" }) + "。登录后可生成证据回答。");
    setActive(-1);
    return;
  }
  setAnswerCardFace({ label: "证据回答 · 本地拼装", body: "正在生成证据回答…" });
  renderResultState("正在生成证据回答…" + lectureScopeHint(store));
  try {
    const value = await postV3("search/answer", {
      query,
      course_ids: store.activeCourse ? [String(store.activeCourse.course_id)] : [],
      sub_id: store.activeLecture?.sub_id || "",
    }, { controller: local });
    if (seq !== requestSeq || paletteMode !== "answer") return;
    renderAnswerResult(store, value, { deep: false, caveat: QUICK_ANSWER_CAVEAT });
    window.dispatchEvent(new Event("courselens:tasks-refresh"));
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "answer") return;
    setAnswerCardFace({ label: "证据回答 · 本地拼装", body: "证据回答暂时无法生成，可稍后重试。" });
    toast(error.message, "error");
  }
  renderAnswerBackAction(store, seq);
}

/* ---- P3 深度回答（opt-in，每次点击一次调用；合同 §①/§④ 冻结流程） ---- */

function updateDeepGate() {
  const deep = $("palette-answer-deep");
  if (!deep) return;
  const configured = deepseekConfigured !== false;
  deep.disabled = !configured;
  deep.title = configured ? "" : friendlyError("question_explanation_not_configured");
}

/* 每次开 palette 刷新一次 DeepSeek 配置态（本地 GET，廉价）：未配置 key
   → 深度按钮置灰 + 提示走 question_explanation_not_configured 文案。 */
async function refreshDeepReadiness() {
  try {
    const value = await apiV3("settings");
    deepseekConfigured = Boolean(value?.deepseek?.configured);
  } catch {
    /* 读不到配置态保持 fail-open：点击后由后端闭集码兜底 */
  }
  updateDeepGate();
}

function stopDeepPolling() {
  clearTimeout(deepTimer);
  deepTimer = 0;
  if (deepWaitWake) {
    const wake = deepWaitWake;
    deepWaitWake = null;
    wake();
  }
}

/* U3 通道就绪窗守卫（根因：登录后 ~25s 就绪窗内派发，后端 preflight 以
   "github not ready" 失败并映射 remote_authorization_required——学生看到
   「深度回答没有成功（可在任务中心重试）」，可预防）。修法=受理前读本地
   remote-connection 缓存快照（廉价 GET，后台 supervisor 自愈）：
   已就绪 → 照旧派发（零行为变化）；
   就绪中（证据未知/探针在途=inconclusive）→ 诚实等待替代必败任务，就绪即自动续发；
   结论性未就绪（未配置/需操作，如 authorization_missing）→ 照旧受理由后端闭集码裁决（等待永不会来）；
   读不到连通态 → fail-open 照旧派发，既有行为零回退。 */
function remoteChannelReadiness(local) {
  return apiV3("remote-connection", { controller: local }).then((value) => {
    const overall = value?.overall || {};
    const ready = Boolean(overall.ready_for_dispatch);
    const state = String(overall.state || "");
    const code = String(overall.code || "");
    const inconclusive = !ready && (state === "unknown" || code === "status_unknown" || Boolean(value?.probing));
    return { ready, inconclusive };
  }, (error) => {
    if (error?.name === "AbortError") throw error;
    return { ready: true, inconclusive: true }; /* fail-open：后端派发门闭集码兜底 */
  });
}

async function waitRemoteChannelReady(store, seq, local) {
  const startedAt = Date.now();
  let readiness = await remoteChannelReadiness(local);
  while (!readiness.ready && readiness.inconclusive) {
    if (seq !== requestSeq || paletteMode !== "answer") return "aborted";
    if (Date.now() >= startedAt + REMOTE_CHANNEL_WARMING_DEADLINE_MS) return "timeout";
    const waitedSeconds = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
    setAnswerCardFace({
      label: "深度回答 · 模型生成",
      body: `${REMOTE_CHANNEL_WARMING_HINT} · 已等待 ${waitedSeconds} 秒`,
      caveat: DEEP_ANSWER_COST_CAVEAT,
    });
    renderResultState("远程通道就绪中…" + lectureScopeHint(store));
    /* 可唤醒睡眠：关闭浮层/换道时 stopDeepPolling 立即放行，不悬挂 */
    await new Promise((resolve) => {
      deepWaitWake = resolve;
      deepTimer = window.setTimeout(() => { deepWaitWake = null; resolve(); }, DEEP_POLL_INTERVAL_MS);
    });
    if (seq !== requestSeq) return "aborted";
    readiness = await remoteChannelReadiness(local);
  }
  if (readiness.ready) return "ready";
  return "proceed"; /* 结论性未就绪：照旧受理，后端闭集码裁决 */
}

async function runDeepAnswer(store) {
  const seq = ++requestSeq;
  if (controller) controller.abort();
  controller = new AbortController();
  const local = controller;
  stopDeepPolling();
  setMode("answer");
  const query = $("palette-input").value.trim();
  if (!query || query.length < 2) {
    renderResultState("继续输入以搜索全文，或直接跳转。");
    setActive(0);
    return;
  }
  if (!authReady(store)) {
    renderResultState(evidenceText(store.auth || { state: "action_required", code: "fudan_login_required" }) + "。登录后可发起深度回答。");
    setActive(-1);
    return;
  }
  setAnswerCardFace({
    label: "深度回答 · 模型生成",
    body: "正在排队深度回答…",
    caveat: DEEP_ANSWER_COST_CAVEAT,
  });
  renderResultState("正在发起深度回答…" + lectureScopeHint(store));
  try {
    /* U3 通道就绪窗守卫：就绪中不受理不失败（诚实等待替代必败任务）。
       置于既有 try 内：AbortError 由既有 catch 过滤，零新增 catch 面。 */
    const readiness = await waitRemoteChannelReady(store, seq, local);
    if (seq !== requestSeq || paletteMode !== "answer") return;
    if (readiness === "timeout") {
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: "远程通道暂时还没就绪，这次没有发起（未使用云端调用）。稍等半分钟再点「深度回答」就好。",
      });
      renderAnswerBackAction(store, seq);
      return;
    }
    const value = await postV3("search/answer", {
      query,
      course_ids: store.activeCourse ? [String(store.activeCourse.course_id)] : [],
      sub_id: store.activeLecture?.sub_id || "",
      mode: "deep",
    }, { controller: local });
    if (seq !== requestSeq || paletteMode !== "answer") return;
    if (value?.mode === "declined") {
      /* 空 packet 诚实即时降级（合同 §① 步骤 2）：零任务零计费零 LLM */
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: String(value.answer || "资料不足，无法根据当前课程资料回答。"),
        caveat: "未使用云端调用。",
      });
    } else if (value?.mode === "deep" && value.task_id) {
      deepTaskId = String(value.task_id);
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: "正在排队深度回答…",
        caveat: DEEP_ANSWER_COST_CAVEAT,
        showCancel: true,
      });
      /* 任务抽屉照发刷新（subject=深度回答） */
      window.dispatchEvent(new Event("courselens:tasks-refresh"));
      const deadlineAt = Date.now() + DEEP_POLL_DEADLINE_MS;
      deepTimer = window.setTimeout(() => void pollDeepAnswer(store, seq, deadlineAt), DEEP_POLL_INTERVAL_MS);
    } else {
      /* 合同外响应形状=路由 mode 分支未就绪（存储读出包 PKG-B 在途窗口）：
         本次如实按快速回答呈现，绝不给本地拼装挂「模型生成」名头 */
      renderAnswerResult(store, value, {
        deep: false,
        caveat: "深度入口还没接上后端，本次返回的是本地拼装的快速回答。",
      });
    }
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "answer") return;
    setAnswerCardFace({
      label: "深度回答 · 模型生成",
      body: error.code === "question_explanation_not_configured"
        ? friendlyError("question_explanation_not_configured")
        : "深度回答暂时无法发起，可稍后重试。",
    });
    toast(error.message, "error");
  }
  renderAnswerBackAction(store, seq);
}

async function pollDeepAnswer(store, seq, deadlineAt) {
  if (seq !== requestSeq || paletteMode !== "answer") return;
  if (Date.now() >= deadlineAt) {
    /* 10 分钟封顶（与云端 job expires 600s 同窗）：任务在服务端继续 */
    setAnswerCardFace({
      label: "深度回答 · 模型生成",
      body: "深度回答还在后台计算。可以先关闭浮层，稍后在任务中心查看进度与结果。",
    });
    return;
  }
  try {
    const value = await apiV3(`search/answer?task_id=${encodeURIComponent(deepTaskId)}`, { controller });
    if (seq !== requestSeq || paletteMode !== "answer") return;
    const task = value?.task || null;
    const state = String(task?.state || "");
    const finish = () => window.dispatchEvent(new Event("courselens:tasks-refresh"));
    if (state === "completed") {
      const answer = value?.answer || null;
      if (answer && String(answer.state || "ready") !== "insufficient") {
        renderAnswerResult(store, answer, { deep: true, caveat: DEEP_RESULT_CAVEAT });
      } else if (answer) {
        setAnswerCardFace({
          label: "深度回答 · 模型生成",
          body: String(answer.answer || "资料不足，无法根据当前课程资料回答。"),
        });
      } else {
        setAnswerCardFace({
          label: "深度回答 · 模型生成",
          body: "深度回答已完成，但结果记录还没就绪。可以在任务中心查看。",
        });
      }
      finish();
      return;
    }
    if (state === "failed") {
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: `${friendlyError(String(task?.error_code || ""), "深度回答没有成功。")}（可在任务中心重试）`,
      });
      finish();
      return;
    }
    if (state === "canceled") {
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: "已取消深度回答。可以随时用「快速回答」。",
      });
      finish();
      return;
    }
    const label = String(task?.label || "").trim();
    /* U2 等待可视化：轮询期答案卡与任务抽屉共用同一 ETA 闭集（task-cards
       单源映射；读出路由本就回 public_task 形状，字段已在手上）。 */
    const eta = task ? String(taskEtaText(task) || "").trim() : "";
    $("palette-answer-body").textContent = eta
      ? `${label || "正在排队深度回答…"} · ${eta}`
      : (label || "正在排队深度回答…");
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "answer") return;
    if (error.status === 404 && String(error.code || "") === "task_task_unknown") {
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: `${friendlyError("task_task_unknown")}（可在任务中心重试）`,
      });
      return;
    }
    if (error.status === 404) {
      /* 读出路由未就绪（search_answers 读出属存储包 PKG-B，在途窗口）：
         停轮询诚实降级，任务仍在服务端继续 */
      setAnswerCardFace({
        label: "深度回答 · 模型生成",
        body: "深度回答任务已提交（可在任务中心查看），结果读取面还没就绪，暂无法在这里显示结果。请稍后再来。",
      });
      return;
    }
    /* 其余瞬态（网络/5xx）不终止轮询：继续按 3s 节奏等到上限 */
  }
  deepTimer = window.setTimeout(() => void pollDeepAnswer(store, seq, deadlineAt), DEEP_POLL_INTERVAL_MS);
}

async function cancelDeepAnswer(store) {
  const seq = ++requestSeq;
  if (controller) controller.abort();
  controller = new AbortController();
  stopDeepPolling();
  const taskId = deepTaskId;
  deepTaskId = "";
  $("palette-answer-cancel").hidden = true;
  if (!taskId) return;
  try {
    await postV3("tasks/actions", {
      task_id: taskId,
      action: "cancel",
      operation_id: operationId("task"),
    }, { controller });
    if (seq !== requestSeq || paletteMode !== "answer") return;
    setAnswerCardFace({
      label: "证据回答 · 本地拼装",
      body: "已取消深度回答。可以随时用「快速回答」。",
    });
    window.dispatchEvent(new Event("courselens:tasks-refresh"));
  } catch (error) {
    if (error.name === "AbortError" || seq !== requestSeq || paletteMode !== "answer") return;
    setAnswerCardFace({
      label: "证据回答 · 本地拼装",
      body: "取消请求没有完成，任务可能已经结束。可以到任务中心查看。",
    });
    toast(error.message, "error");
  }
}

let storeRef = null;

/* 关闭归锚按当页解析（打开经「针对本讲提问」等非触发钮入口时尤其关键）：
   播放视图=讲次在位且视频可见 → 归还视频（空格回播放控制，而非滞留头部
   搜索钮被空格误重开浮层）；其余页面回落搜索触发钮。 */
function paletteFocusAnchor() {
  const page = document.querySelector(".page:not([hidden])");
  if (page?.dataset?.page === "study") {
    const stage = $("player-stage");
    if (stage && !stage.hidden) return stage;
  }
  return currentSearchTrigger();
}

function currentSearchTrigger() {
  if (document.activeElement?.matches?.("[data-open-search]")) return document.activeElement;
  const vis = document.querySelector(".page:not([hidden])");
  return (vis && vis.querySelector("[data-open-search]")) || $("search-trigger");
}

function openPalette(trigger, scope = null) {
  paletteTrigger = trigger || null;
  lectureScope = scope && scope.sub_id ? scope : null;
  const input = $("palette-input");
  const opened = openOverlay({
    root: $("palette-root"),
    trigger: paletteTrigger,
    returnFocus: () => paletteFocusAnchor(),
    onEscape: () => {
      if (paletteMode !== "compact") {
        setMode("compact");
        void runCompact(storeRef);
        return true;
      }
      return false;
    },
    onClose: () => {
      clearTimeout(quickTimer);
      stopDeepPolling();
      if (controller) controller.abort();
      requestSeq += 1;
      paletteTrigger = null;
      const t = $("search-trigger");
      if (t) t.setAttribute("aria-expanded", "false");
    },
  });
  if (!opened) return; /* 被其他浮层阻挡：零搜索、零 timer、零 DOM 副作用 */
  input.value = "";
  setMode("compact");
  renderCompactInitial();
  void refreshDeepReadiness();
  const t = $("search-trigger");
  if (t) t.setAttribute("aria-expanded", "true");
  window.requestAnimationFrame(() => input.focus({ preventScroll: true }));
}

function renderCompactInitial() {
  void runCompact(storeRef);
}

function closePalette() {
  if ($("palette-root").hidden) return;
  closeOverlay($("palette-root"));
}

export async function installSearchPalette(store) {
  storeRef = store;
  const paletteRoot = $("palette-root");
  const input = $("palette-input");

  const handleTriggers = (event) => {
    const trigger = event.target.closest?.("[data-open-search]");
    if (!trigger) return;
    event.preventDefault();
    if (!paletteRoot.hidden) { closePalette(); return; }
    openPalette(trigger);
  };
  document.addEventListener("click", handleTriggers);

  const handleGlobalKeydown = (event) => {
    if ((event.metaKey || event.ctrlKey) && String(event.key || "").toLowerCase() === "k") {
      event.preventDefault();
      if (!paletteRoot.hidden) closePalette();
      else openPalette(currentSearchTrigger());
    }
  };

  const handleInput = () => {
    clearTimeout(quickTimer);
    quickTimer = window.setTimeout(() => {
      if (paletteMode === "compact") void runCompact(store);
    }, 150);
  };
  const handleInputKeydown = (event) => {
    /* IME 合成态早退（CJK-GUARD-1，MAC-3 交付③实锤）：拼音候选导航/上屏的
       箭头与 Enter 是输入法的键，绝不翻结果列表或执行条目（keyCode 229 兼容
       合成键上报形态）。守卫在最前，preventDefault 一律不抢。 */
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (activeIndex < renderedItems.length - 1) setActive(activeIndex + 1);
      else setActive(0);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (activeIndex > 0) setActive(activeIndex - 1);
      else setActive(Math.max(0, renderedItems.length - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      /* WP1-D4①：激活项按渲染序（renderedItems）解析——面板按 GROUP_ORDER
         重排展示，课程类条目排在操作组之前，按拼装序取 currentItems 会错
         激活「完整检索」（Enter 不切课不关面板的断链根因）。 */
      const item = renderedItems[activeIndex];
      if (item) executeItem(item);
    }
  };
  input.addEventListener("input", handleInput);
  input.addEventListener("keydown", handleInputKeydown);
  $("palette-collapse").addEventListener("click", () => {
    setMode("compact");
    void runCompact(store);
  });
  $("palette-answer").addEventListener("click", () => void runAnswer(store));
  $("palette-answer-deep").addEventListener("click", () => void runDeepAnswer(store));
  $("palette-answer-cancel").addEventListener("click", () => void cancelDeepAnswer(store));
  paletteRoot.addEventListener("click", (event) => {
    if (event.target.dataset?.paletteScrim !== undefined) closePalette();
  });
  const handleAskLecture = (event) => {
    const detail = event.detail || {};
    if (!paletteRoot.hidden) closePalette();
    openPalette(currentSearchTrigger(), { sub_id: String(detail.sub_id || ""), title: String(detail.title || "") });
  };
  window.addEventListener("courselens:ask-lecture", handleAskLecture);
  window.addEventListener("keydown", handleGlobalKeydown);

  const unsubscribe = store.subscribe("auth", () => {
    if (!$("palette-root").hidden && paletteMode === "compact" && !authReady(store)) renderAuthState(store);
  });

  return () => {
    controller?.abort();
    clearTimeout(quickTimer);
    stopDeepPolling();
    unsubscribe();
    document.removeEventListener("click", handleTriggers);
    window.removeEventListener("keydown", handleGlobalKeydown);
    window.removeEventListener("courselens:ask-lecture", handleAskLecture);
    input.removeEventListener("input", handleInput);
    input.removeEventListener("keydown", handleInputKeydown);
  };
}
