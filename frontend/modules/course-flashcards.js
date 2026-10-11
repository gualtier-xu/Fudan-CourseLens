/* 闪卡复习（RR-P4FSRS-1）—— AIRESEARCH 提案 P4 的零 LLM 落地（本地 Minimal FSRS）。
 *
 * 职责边界（对齐 course-review.js / review-multiview.js 纪律）：只做 fetch /
 * render / state；组合根归 course-review.js（复习页第 4 个页签）。
 *  - 数据源 = 既有知识快照：卡片全部由后端从 course-knowledge.v1 快照确定性
 *    派生（key_points = AI 总结/讲次 IR/章节的既有产物），FSRS 调度也是纯
 *    本地算法。本模块不新增任何模型调用、不派生新事实。
 *  - 样式全部走本包新增 p4f- 前缀类（本包独占新文件 flashcards.css，由本
 *    模块注入 <link>），复用全局 token；零 pages.css 改动。
 *  - 降级：无产物 → 诚实空态 + 指向「更新课程知识」（动作由 course-review
 *    注入，本模块不反向依赖它）；取数失败 → 诚实错误行，不动已渲染内容。
 *  - 诚实纪律：卡面绝不携带答案文本（后端保证，前端不猜）；证据条目只在
 *    有合法毫秒锚时可跳转，无锚保持可读不可点（study.js quizEvidenceAnchor
 *    家规）。
 *  - 键盘：空格/回车翻面，翻面后 1-4 评分；监听挂在面板元素上，面板未
 *    获焦时不抢全局键。焦点在按钮上时空格/回车归按钮本义，面板不抢。
 */

import { apiV3, postV3 } from "./api.js";
import { $, clear, textElement, toast } from "./ui.js";

const CSS_NODE_ID = "p4f-flashcards-style";
const CSS_HREF = "/styles/flashcards.css";

const CARD_TYPE_LABELS = { cloze: "填空", topic_cue: "主题", anchor_recall: "回忆" };
const RATING_LABELS = { 1: "忘了", 2: "困难", 3: "良好", 4: "简单" };
const RATINGS = [1, 2, 3, 4];
const CARD_STATES = ["new", "learning", "review", "relearning"];
const CONTRACT_VIEW = "course_flashcards";

/* ---------------- 纯归一化（可单测；未知形状一律降级为空，绝不猜测） ---------------- */

function str(value) {
  return String(value ?? "").trim();
}

function intMs(value) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) return null;
  return value;
}

export function normalizeEvidenceRef(value) {
  if (!value || typeof value !== "object") return null;
  const kind = str(value.kind);
  const label = str(value.label);
  const startMs = intMs(value.start_ms);
  const endMs = intMs(value.end_ms);
  if (!kind && !label && startMs == null) return null;
  return { kind, label, startMs, endMs };
}

export function normalizeFlashcard(value, index) {
  if (!value || typeof value !== "object") return null;
  const cardId = str(value.card_id);
  const front = str(value.front);
  const back = str(value.back);
  if (!cardId || !front || !back) return null;
  const state = str(value.state);
  return {
    cardId,
    subId: str(value.sub_id),
    cardType: CARD_TYPE_LABELS[str(value.card_type)] ? str(value.card_type) : "anchor_recall",
    state: CARD_STATES.includes(state) ? state : "",
    front,
    back,
    hint: str(value.hint),
    lectureLabel: str(value.lecture_label) || "这一讲",
    due: Boolean(value.due),
    evidence: Array.isArray(value.evidence)
      ? value.evidence.map(normalizeEvidenceRef).filter(Boolean)
      : [],
    order: index,
  };
}

export function normalizeDeck(value) {
  if (!value || typeof value !== "object" || str(value.view) !== CONTRACT_VIEW) {
    throw new Error("flashcards_contract_mismatch");
  }
  const counts = value.counts && typeof value.counts === "object" ? value.counts : {};
  const cards = Array.isArray(value.cards)
    ? value.cards.map(normalizeFlashcard).filter(Boolean)
    : [];
  const caughtUp = value.all_caught_up && typeof value.all_caught_up === "object" ? value.all_caught_up : null;
  return {
    courseId: str(value.course_id),
    sourceLabel: str(value.source_label) || "来自本课总结与知识要点（非官方）",
    counts: {
      total: Number(counts.total) || 0,
      due: Number(counts.due) || 0,
      new: Number(counts.new) || 0,
      learning: Number(counts.learning) || 0,
      reviewedToday: Number(counts.reviewed_today) || 0,
    },
    cards,
    emptyAction: value.empty_action && typeof value.empty_action === "object"
      ? { action: str(value.empty_action.action), label: str(value.empty_action.label), hint: str(value.empty_action.hint) }
      : null,
    caughtUp: caughtUp
      ? { nextDueText: str(caughtUp.next_due_text) }
      : null,
  };
}

/* ---------------- 模块状态 ---------------- */

const deckState = {
  courseId: "",
  deck: null,
  loading: false,
  errorCode: "",
  cursor: 0,
  flipped: false,
  busy: false,
  reviewedInSession: 0,
  lastIntervalText: "",
};

export function resetFlashcards(courseId = "") {
  deckState.courseId = str(courseId);
  deckState.deck = null;
  deckState.loading = false;
  deckState.errorCode = "";
  deckState.cursor = 0;
  deckState.flipped = false;
  deckState.busy = false;
  deckState.reviewedInSession = 0;
  deckState.lastIntervalText = "";
  restoreFocusPending = false;
}

function el(tag, className, text) {
  return textElement(tag, text, className);
}

/* ---------------- 取数 ---------------- */

export async function ensureFlashcardsDeck(courseId, { force = false } = {}) {
  const key = str(courseId);
  if (!key) return null;
  if (!force && deckState.courseId === key && (deckState.deck || deckState.loading)) {
    return deckState.deck;
  }
  resetFlashcards(key);
  deckState.loading = true;
  try {
    const value = await apiV3(`course-review/flashcards?course_id=${encodeURIComponent(key)}`);
    // P1-7：await 期间用户可能已换课——响应回来先复查归属，旧课的数据、
    // 错误、收尾都不许写进新课的状态（deck/errorCode/loading 三处守卫）。
    if (deckState.courseId !== key) return null;
    deckState.deck = normalizeDeck(value);
  } catch (error) {
    if (deckState.courseId !== key) return null;
    deckState.errorCode = str(error?.code || error?.message) || "flashcards_unavailable";
  } finally {
    if (deckState.courseId === key) deckState.loading = false;
  }
  return deckState.deck;
}

async function rateCurrent(rating, rerender) {
  const cards = deckState.deck?.cards || [];
  const card = cards[deckState.cursor];
  if (!card || deckState.busy) return;
  deckState.busy = true;
  rerender();
  try {
    const result = await postV3("course-review/actions", {
      action: "review_flashcard",
      course_id: deckState.courseId,
      card_id: card.cardId,
      rating,
    });
    deckState.lastIntervalText = str(result?.interval_text);
    deckState.reviewedInSession += 1;
    if (deckState.deck) {
      const counts = deckState.deck.counts;
      if (card.due) counts.due = Math.max(0, counts.due - 1);
      // P2-9：评掉的卡按调度结果同步计数——新卡要走、进学习/毕业要挪组，
      // 头部数字不再停留在评分前的旧账。
      if (card.state === "new") counts.new = Math.max(0, counts.new - 1);
      const afterState = str(result?.state);
      const beforeLearning = card.state === "learning" || card.state === "relearning";
      const afterLearning = afterState === "learning" || afterState === "relearning";
      if (afterState) {
        if (afterLearning && !beforeLearning) counts.learning += 1;
        if (!afterLearning && beforeLearning) counts.learning = Math.max(0, counts.learning - 1);
      }
      counts.reviewedToday += 1;
    }
    deckState.cursor += 1;
    deckState.flipped = false;
  } catch {
    /* 评分失败：光标不动、旧间隔提示清掉（绝不拿上一张的排期充数）。 */
    deckState.lastIntervalText = "";
    toast("这张卡这次没记上，稍后再试一次。", "caution");
  } finally {
    deckState.busy = false;
    rerender();
  }
}

/* ---------------- 渲染 ---------------- */

function evidenceBlock(card, ctx) {
  if (!card.evidence.length) return null;
  const wrap = el("div", "p4f-evidence");
  card.evidence.forEach((ref) => {
    const row = el("div", "p4f-evidence-row");
    row.append(el("span", "p4f-evidence-kind", ref.label || "来源"));
    if (ref.startMs != null && card.subId) {
      const jump = el("button", "p4f-evidence-jump", "定位到这段");
      jump.type = "button";
      jump.addEventListener("click", (event) => {
        event.stopPropagation();
        ctx.onJump?.({ subId: card.subId, startMs: ref.startMs, endMs: ref.endMs });
      });
      row.append(jump);
    }
    wrap.append(row);
  });
  return wrap;
}

function cardNode(card, ctx) {
  const node = el("div", "p4f-card");
  node.setAttribute("role", "button");
  node.setAttribute("aria-label", deckState.flipped ? "闪卡答案面" : "闪卡问题面，点击或按空格翻面");
  node.setAttribute("tabindex", "0");
  node.append(el("span", "p4f-card-kind", CARD_TYPE_LABELS[card.cardType] || "回忆"));
  node.append(el("span", "p4f-card-lecture", card.lectureLabel));
  node.append(el("p", "p4f-card-text", card.front));
  if (deckState.flipped) {
    node.append(el("p", "p4f-card-text is-back", card.back));
    if (card.hint) node.append(el("p", "p4f-card-hint", card.hint));
    const evidence = evidenceBlock(card, ctx);
    if (evidence) node.append(evidence);
  } else if (card.hint) {
    node.append(el("p", "p4f-flip-hint", card.hint));
  }
  const flip = () => {
    deckState.flipped = !deckState.flipped;
    renderInto(ctx.panelRef, ctx);
  };
  node.addEventListener("click", flip);
  node.addEventListener("keydown", (event) => {
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      // P2-10：卡片自己翻这一次就够了，别再冒泡给面板翻第二次（双翻=没翻）。
      event.stopPropagation();
      flip();
    }
  });
  return node;
}

function actionsNode(ctx) {
  const wrap = el("div", "p4f-actions");
  if (!deckState.flipped) {
    const flip = el("button", "p4f-button is-flip", "显示答案");
    flip.type = "button";
    flip.addEventListener("click", () => {
      deckState.flipped = true;
      renderInto(ctx.panelRef, ctx);
    });
    wrap.append(flip);
    return wrap;
  }
  RATINGS.forEach((rating) => {
    const button = el("button", "p4f-button", RATING_LABELS[rating]);
    button.type = "button";
    button.dataset.rating = String(rating);
    button.disabled = deckState.busy;
    // 评分在途：全局 button[aria-busy=true] 提供 cursor:progress 的「提交中」线索。
    if (deckState.busy) button.setAttribute("aria-busy", "true");
    button.addEventListener("click", () => { void rateCurrent(rating, () => renderInto(ctx.panelRef, ctx)); });
    wrap.append(button);
  });
  return wrap;
}

function headNode() {
  const counts = deckState.deck?.counts || {};
  const wrap = el("div", "p4f-head");
  wrap.append(el("h3", "p4f-head-title", "闪卡复习"));
  const countsNode = el("p", "p4f-counts");
  countsNode.append(el("span", "", "今日已复习 "));
  countsNode.append(el("strong", "", String(counts.reviewedToday ?? 0)));
  countsNode.append(el("span", "", " · 到期 "));
  countsNode.append(el("strong", "", String(counts.due ?? 0)));
  countsNode.append(el("span", "", " · 新卡 "));
  countsNode.append(el("strong", "", String(counts.new ?? 0)));
  countsNode.append(el("span", "", " · 共 "));
  countsNode.append(el("strong", "", String(counts.total ?? 0)));
  countsNode.append(el("span", "", " 张"));
  wrap.append(countsNode);
  return wrap;
}

function emptyNode(ctx) {
  const wrap = el("div", "p4f-empty");
  const action = deckState.deck?.emptyAction;
  wrap.append(el("p", "", action?.hint
    || "这一课还没有可制卡的内容：先在讲次里生成一次总结，再回到这里复习。"));
  if (ctx.onRefresh) {
    const button = el("button", "p4f-button is-flip", action?.label || "更新课程知识");
    button.type = "button";
    button.addEventListener("click", () => ctx.onRefresh());
    wrap.append(button);
  }
  return wrap;
}

function doneNode(ctx) {
  const wrap = el("div", "p4f-done");
  const total = deckState.deck?.cards.length || 0;
  const parts = [`这一轮 ${deckState.reviewedInSession} 张都过完了。`];
  if (deckState.lastIntervalText) parts.push(`最后一张下次再见：${deckState.lastIntervalText}。`);
  const caught = deckState.deck?.caughtUp;
  if (!deckState.lastIntervalText && caught?.nextDueText) parts.push(`下一次到期：${caught.nextDueText}。`);
  const summary = el("p", "", parts.join(""));
  summary.setAttribute("aria-live", "polite");
  wrap.append(summary);
  const refresh = el("button", "p4f-button is-flip", "看看有没有新的到期卡");
  refresh.type = "button";
  refresh.addEventListener("click", async () => {
    await ensureFlashcardsDeck(deckState.courseId, { force: true });
    renderInto(ctx.panelRef, ctx);
  });
  wrap.append(refresh);
  return wrap;
}

function errorNode() {
  /* 失败态走共享 .error-note 紧凑卡（components.css，值=原 .p4f-error danger
     配方逐值同源）：VA-P2-03 兄弟 tab 失败态统一配方（VISUAL-BACKLOG-1）。 */
  const wrap = el("div", "error-note");
  const code = deckState.errorCode;
  wrap.append(el("p", "", code === "flashcards_contract_mismatch"
    ? "闪卡的数据格式和这一版应用对不上，更新应用后再试。"
    : "闪卡暂时没能取回来。其他复习视图不受影响，稍后再点一次。"));
  return wrap;
}

/* 焦点恢复：翻面/评分会把面板子树整体重建，焦点随之跌落 body，面板级键盘
 * 监听从此收不到事件（键盘流第二次交互即断）。规则=交互触发的重绘若丢失了
 * 焦点位，下一次有卡面的渲染把焦点还给卡面（或完成/空态的续作按钮）；装载/
 * 错误等无焦点位的中间态只记账不还原。冷渲染（页签进入，焦点在页签按钮上，
 * 在面板外）绝不抢焦点。 */
let restoreFocusPending = false;

function panelContainsFocus(panel) {
  let node = document.activeElement;
  while (node) {
    if (node === panel) return true;
    node = node.parentElement || node.parent || null;
  }
  return false;
}

function renderInto(panel, ctx) {
  if (!panel) return;
  ctx.panelRef = panel;
  injectCss();
  if (panelContainsFocus(panel)) restoreFocusPending = true;
  clear(panel);
  if (deckState.loading && !deckState.deck) {
    panel.append(el("p", "cr-hint", "正在准备闪卡…"));
    return;
  }
  if (deckState.errorCode) {
    panel.append(errorNode());
    return;
  }
  const deck = deckState.deck;
  if (!deck) {
    panel.append(el("p", "cr-hint", "正在准备闪卡…"));
    return;
  }
  const wrap = el("div", "p4f-deck");
  wrap.append(headNode());
  if (!deck.counts.total) {
    wrap.append(emptyNode(ctx));
  } else {
    const cards = deck.cards;
    if (deckState.cursor >= cards.length) {
      wrap.append(doneNode(ctx));
    } else {
      const card = cards[deckState.cursor];
      const progress = el("p", "p4f-progress",
        `第 ${deckState.cursor + 1} / ${cards.length} 张 · 来源：${deck.sourceLabel}`);
      progress.setAttribute("aria-live", "polite");
      wrap.append(progress);
      wrap.append(cardNode(card, ctx));
      wrap.append(actionsNode(ctx));
      if (deckState.flipped) {
        wrap.append(el("p", "p4f-next-note",
          "凭记忆作答后再评分：「忘了」很快再见，「良好」按计划排期。键盘 1-4 可直接评分。"));
      }
    }
  }
  panel.append(wrap);
  installKeyboard(panel, ctx);
  if (restoreFocusPending) {
    const target = wrap.querySelector(".p4f-card") || wrap.querySelector(".p4f-button");
    if (target) {
      restoreFocusPending = false;
      target.focus();
    }
  }
}

function installKeyboard(panel, ctx) {
  if (!panel || panel.dataset.p4fKeyboard === "1") return;
  panel.dataset.p4fKeyboard = "1";
  panel.addEventListener("keydown", (event) => {
    if (deckState.busy) return;
    // P2-10：焦点在按钮上时，空格/回车归按钮本义（激活评分/翻面按钮），
    // 面板不抢来翻卡；数字键评分保留。
    const target = event.target;
    if ((event.key === " " || event.key === "Enter")
        && target && typeof target.closest === "function" && target.closest("button")) {
      return;
    }
    const cards = deckState.deck?.cards || [];
    if (deckState.cursor >= cards.length) return;
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      deckState.flipped = !deckState.flipped;
      renderInto(ctx.panelRef, ctx);
      return;
    }
    if (deckState.flipped && ["1", "2", "3", "4"].includes(event.key)) {
      event.preventDefault();
      void rateCurrent(Number(event.key), () => renderInto(ctx.panelRef, ctx));
    }
  });
}

function injectCss() {
  if (document.getElementById(CSS_NODE_ID)) return;
  const head = document.head;
  if (!head || typeof head.appendChild !== "function") return;
  const link = document.createElement("link");
  link.id = CSS_NODE_ID;
  link.setAttribute("rel", "stylesheet");
  link.setAttribute("href", CSS_HREF);
  head.appendChild(link);
}

/* ---------------- 入口（course-review.js 调用） ---------------- */

export function renderFlashcardsPanel(panel, ctx = {}) {
  renderInto(panel, ctx);
}

export function flashcardsBusy() {
  return deckState.loading || deckState.busy;
}
