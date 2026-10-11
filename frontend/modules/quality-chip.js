/* THINK-LADDER-2：质量抽检 chip + 课程记忆候选复核面。
 *
 * 职责边界：纯消费层（fetch / normalize / render），零业务推断——
 *  - 抽检态三态只由报告有无与 warn 计数派生：passed（零 warn）/ suspect（N 处
 *    存疑）/ none（还没有报告）。读取失败=隐藏，绝不装「未抽检」糊弄断网。
 *  - findings 人话只做闭集码翻译（与 worker judge 的冻结闭集一一对应），
 *    未知 code 降级为中性措辞，绝不编造含义。
 *  - 候选复核面只渲染后端 term_candidates 加性视图；两清单全空=整区隐藏
 *    （无空壳）。自动晋升件的「撤销」走既有 dismiss 动作（THINK-LADDER-1
 *    的撤销通道），人工确认件不出现在该清单里。
 *
 * 样式红线：只复用既有类（cr-coverage-item 芯片形、text-button、hint、
 * item-list），零新增 CSS；双主题经既有 token 同源生效。例外（VISUAL-BACKLOG-1 ·
 * VA-P3-05 立项）：复核区头升 .section-label 区块小标题档（hint 档弱于下方
 * cr-tabs 的实证弱点，night15 候选①），档本体在 components.css。
 */

import { apiV3, postV3 } from "./api.js";
import { clear, setBusy, textElement, toast } from "./ui.js";

const str = (value) => String(value ?? "").trim();
const num = (value) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const asArray = (value) => (Array.isArray(value) ? value : []);
const pick = (object, ...keys) => {
  for (const key of keys) {
    if (object && typeof object === "object" && object[key] != null) return object[key];
  }
  return undefined;
};

/* judge findings 冻结闭集 → 人话（像同学解释，不像系统日志）。
   未知 code 一律「这一处可能有点问题」，不编含义。 */
const FINDING_CODE_TEXT = Object.freeze({
  glossary_violation: "术语写法和课程记忆对不上",
  homophone_suspect: "可能有同音字听错",
  term_inconsistent: "同一个词前后写法不一样",
  broken_flow: "这句话读起来不太通顺",
  garbled: "这段文字可能没听清",
  no_source_support: "这句在字幕里找不到依据",
  contradicts_source: "这句和字幕内容对不上",
  chapter_mislabel: "章节划分可能不准",
  duplicate_content: "这里的内容重复了",
  empty_section: "这一节是空的",
});
const FINDING_FALLBACK_TEXT = "这一处可能有点问题";
const FINDING_TARGET_TEXT = Object.freeze({
  segment: "段", chapter: "章", takeaway: "结论", body: "正文",
});

export function findingText(finding) {
  const code = str(finding?.code);
  return FINDING_CODE_TEXT[code] || FINDING_FALLBACK_TEXT;
}

export function findingWhere(finding) {
  const target = str(finding?.target);
  const position = num(finding?.position);
  const unit = FINDING_TARGET_TEXT[target] || "";
  if (!unit) return "";
  if (target === "body" || !Number.isFinite(position)) return unit;
  return `第 ${position + 1} ${unit}`;
}

/* 抽检报告 → 三态 chip 视图（纯函数）。报告缺失/畸形按未抽检收口；
   findings 只收闭集 severity（warn 显眼、info 收进次级）。 */
export function normalizeQualityArtifact(value) {
  const content = value?.artifact?.content;
  if (!content || typeof content !== "object") {
    return { state: "none", warnCount: 0, infoCount: 0, findings: [] };
  }
  const findings = [];
  let warnCount = 0;
  let infoCount = 0;
  for (const group of ["subtitle", "summary"]) {
    for (const finding of asArray(content[group]?.findings)) {
      if (!finding || typeof finding !== "object") continue;
      const severity = str(finding.severity);
      if (severity === "warn") warnCount += 1;
      else if (severity === "info") infoCount += 1;
      else continue;
      findings.push({
        severity,
        text: findingText(finding),
        where: findingWhere(finding),
      });
    }
  }
  return {
    state: warnCount > 0 ? "suspect" : "passed",
    warnCount,
    infoCount,
    findings,
  };
}

const CHIP_TEXT = Object.freeze({
  passed: "抽检通过",
  suspect: "",
  none: "未抽检",
});

function chipLabel(quality) {
  if (quality.state === "suspect") return `抽检完成 · ${quality.warnCount} 处存疑`;
  return CHIP_TEXT[quality.state] || CHIP_TEXT.none;
}

const CHIP_TITLE = Object.freeze({
  passed: "这一讲的内容自动抽检过，没有发现要提醒的问题。",
  suspect: "点开看具体是哪几处。提醒仅供参考，内容本身可以直接用。",
  none: "这一讲还没有做过质量抽检。",
});

/* 渲染 chip 与 findings 展开块（挂载点：总结卡头部）。quality.state==="none"
   也渲染——「未抽检」是诚实态，不装完成。 */
export function renderQualityChip(mount, findingsMount, quality) {
  if (!mount) return;
  clear(mount);
  if (findingsMount) clear(findingsMount);
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "text-button quality-chip";
  chip.textContent = chipLabel(quality);
  chip.title = CHIP_TITLE[quality.state] || CHIP_TITLE.none;
  const expandable = quality.findings.length > 0;
  chip.setAttribute("aria-expanded", "false");
  if (!expandable) {
    chip.disabled = true;
    mount.append(chip);
    mount.hidden = false;
    return;
  }
  chip.addEventListener("click", () => {
    const open = chip.getAttribute("aria-expanded") === "true";
    chip.setAttribute("aria-expanded", open ? "false" : "true");
    if (findingsMount) findingsMount.hidden = open;
    else mount.hidden = open;
    if (open) {
      if (findingsMount) clear(findingsMount);
      return;
    }
    const target = findingsMount || mount;
    clear(target);
    target.append(textElement(
      "p",
      `自动抽检提醒 ${quality.warnCount} 处${quality.infoCount ? `（另有 ${quality.infoCount} 处次要备注）` : ""}。提醒仅供参考，内容本身可以直接用。`,
      "hint",
    ));
    quality.findings.forEach((finding) => {
      const where = finding.where ? `${finding.where}：` : "";
      const row = textElement(
        "p",
        `${finding.severity === "warn" ? "" : "（次要）"}${where}${finding.text}`,
        "hint",
      );
      target.append(row);
    });
  });
  mount.append(chip);
  mount.hidden = false;
}

/* 学习页总结卡的质量 chip 读取面：随讲次切换取 quality_report。
   读取失败=整枚隐藏（诚实：网络不可用≠未抽检）。 */
let qualityEpoch = 0;
let qualityController = null;

export async function refreshQualityChip(store) {
  const mount = document.getElementById("quality-chip-mount");
  const findingsMount = document.getElementById("quality-findings");
  if (!mount) return;
  const lecture = store?.activeLecture;
  const subId = str(lecture?.sub_id);
  qualityEpoch += 1;
  const epoch = qualityEpoch;
  qualityController?.abort?.();
  qualityController = typeof AbortController === "function" ? new AbortController() : null;
  clear(mount);
  mount.hidden = true;
  if (findingsMount) {
    clear(findingsMount);
    findingsMount.hidden = true;
  }
  if (!subId) return;
  try {
    const value = await apiV3(
      `artifacts?sub_id=${encodeURIComponent(subId)}&kind=quality_report`,
      { controller: qualityController || undefined },
    );
    if (epoch !== qualityEpoch) return;
    renderQualityChip(mount, findingsMount, normalizeQualityArtifact(value));
  } catch (error) {
    if (epoch !== qualityEpoch) return;
    if (str(error?.name) === "AbortError") return;
    /* 读取失败：保持隐藏（不装未抽检、不弹打扰） */
  }
}

/* ---------------- 课程记忆候选复核面（复习页） ---------------- */

/* course-review 响应的 term_candidates 加性视图 → 复核面视图（纯函数）。
   缺席/畸形=null（整区隐藏，与「无空壳」口径一致）。 */
export function normalizeTermCandidateView(value) {
  const raw = value?.term_candidates;
  if (!raw || typeof raw !== "object") return null;
  const rows = asArray(raw.rows)
    .map((row) => ({
      wrong: str(row?.wrong),
      right: str(row?.right),
      lectureCount: num(row?.lecture_count) || 0,
      totalCount: num(row?.total_count) || 0,
      signalCount: num(row?.signal_count) || 0,
    }))
    .filter((row) => row.wrong && row.right);
  const autoConfirmedRows = asArray(raw.auto_confirmed_rows)
    .map((row) => ({
      wrong: str(row?.wrong),
      right: str(row?.right),
      signalCount: num(row?.signal_count) || 0,
    }))
    .filter((row) => row.wrong && row.right);
  return {
    rows,
    confirmedCount: num(raw.confirmed_count) || 0,
    autoConfirmedRows,
  };
}

/* 渲染复核区（挂载点：course-review-surface 内既有锚点）。
   onAction(kind, row) 由 course-review.js 注入（kind=confirm|dismiss，同一
   动作面同时服务候选行与自动晋升件撤销）。busy 以行级 disabled 表达。 */
export function renderTermCandidateRegion(mount, view, { onAction } = {}) {
  if (!mount) return;
  clear(mount);
  const hasCandidates = (view?.rows?.length || 0) > 0;
  const hasAuto = (view?.autoConfirmedRows?.length || 0) > 0;
  if (!view || (!hasCandidates && !hasAuto)) {
    mount.hidden = true;
    return;
  }
  mount.hidden = false;
  const header = textElement("p", "课程记忆复核", "");
  /* 区块头升 .section-label 档（VA-P3-05 立项首消费点）：带交互内容的区块头
     不弱于其内容（原 hint 档 13.5px muted 弱于下方 13.5px cr-tabs）。 */
  header.className = "section-label";
  header.setAttribute("role", "heading");
  header.setAttribute("aria-level", "3");
  mount.append(header);

  if (hasAuto) {
    mount.append(textElement(
      "p",
      "这些写法已经根据反复出现的修正自动记住了：",
      "hint",
    ));
    const autoList = document.createElement("div");
    autoList.className = "item-list";
    view.autoConfirmedRows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "cr-citation-row";
      item.append(textElement(
        "span",
        `${row.wrong} → ${row.right}（自动确认 · 信号 ×${row.signalCount}）`,
        "hint",
      ));
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "text-button";
      undo.textContent = "撤销";
      undo.title = "如果这个自动记忆不对，撤销后它不会再被自动使用。";
      undo.addEventListener("click", async () => {
        setBusy(undo, true);
        try {
          await onAction?.("dismiss", row);
        } finally {
          setBusy(undo, false);
        }
      });
      item.append(undo);
      autoList.append(item);
    });
    mount.append(autoList);
  }

  if (hasCandidates) {
    mount.append(textElement(
      "p",
      `${view.rows.length} 个候选写法待确认：确认后转写会更准，拿不准就先忽略。`,
      "hint",
    ));
    const list = document.createElement("div");
    list.className = "item-list";
    view.rows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "cr-citation-row";
      const scope = [
        row.lectureCount ? `${row.lectureCount} 讲` : "",
        row.totalCount ? `共 ${row.totalCount} 次` : "",
      ].filter(Boolean).join(" · ");
      item.append(textElement(
        "span",
        `${row.wrong} → ${row.right}${scope ? `（${scope}）` : ""}`,
        "hint",
      ));
      const confirmButton = document.createElement("button");
      confirmButton.type = "button";
      confirmButton.className = "text-button";
      confirmButton.textContent = "确认";
      const dismissButton = document.createElement("button");
      dismissButton.type = "button";
      dismissButton.className = "text-button";
      dismissButton.textContent = "忽略";
      const run = (kind, button) => async () => {
        setBusy(confirmButton, true);
        setBusy(dismissButton, true);
        try {
          await onAction?.(kind, row);
        } finally {
          setBusy(confirmButton, false);
          setBusy(dismissButton, false);
        }
      };
      confirmButton.addEventListener("click", run("confirm", confirmButton));
      dismissButton.addEventListener("click", run("dismiss", dismissButton));
      item.append(confirmButton, dismissButton);
      list.append(item);
    });
    mount.append(list);
  }
}

/* 动作面人话（闭集码 → 像同学解释的文案；与 course-review.js 同词汇）。 */
const TERM_ACTION_ERROR_TEXT = Object.freeze({
  term_candidate_unknown: "这个词对已经不在候选清单里了，刷新一下就好。",
  course_review_action_invalid: "这个请求没能通过校验，刷新后重新点一次。",
  course_review_course_unknown: "这门课不在已授权的课程目录里，先刷新课程目录。",
});
export function termActionErrorText(error) {
  const code = str(error?.code || error?.message);
  return TERM_ACTION_ERROR_TEXT[code] || "这次没有成功，稍后再试一次。";
}

/* course-review.js 的动作收口：成功后由调用方刷新复核区。 */
export async function submitTermCandidateAction(courseId, kind, row) {
  const action = kind === "confirm" ? "confirm_term_candidate" : "dismiss_term_candidate";
  try {
    await postV3("course-review/actions", {
      course_id: str(courseId),
      action,
      wrong: str(row?.wrong),
      right: str(row?.right),
    });
    return true;
  } catch (error) {
    toast(termActionErrorText(error), "error");
    return false;
  }
}
