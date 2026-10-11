/* ARCH-DEBT-1：本文件由 tasks-drawer.js 按 R7-#28 任务族设计稿拆分而来，
   纯移动重组、行为零变化；任务卡渲染族（task x23 词干族：文案闭集/进度/证据/聚合/动作按钮/阶段轨）。tasks-drawer.js 门面保留抽屉
   生命周期核心（轮询/SSE/刷新调度/安装器）并 re-export 原公共导出面。 */
import { postV3 } from "../api.js";
import { $, clear, evidenceText, formatRelativeTime, formatTime, operationId, setBusy, slidesSkippedText, textElement, toast } from "../ui.js";
import { armTwoStepButton, renderAutomationRunsSection, restoreTwoStepArm } from "./automation-runs.js";
import { loadTasks } from "../tasks-drawer.js"; /* ESM 函数声明提升循环边：仅事件期调用（动作后刷新），求值期零调用，语义安全 */

export let lastTasksLoadedAt = 0;
/* ETA 只在友好分钟值/档位变化时更新 DOM（合同 §5.1）：task_id → 上一次渲染的文案 */
const lastEvidenceByTask = new Map();

/* 失败处置文案闭集：键 = 后端 TASK_ERROR_CODES（src/runtime/http_api.py），不发明新代码。
   U2（用户 09-22 走查拍板）：值按真实死因分组映射（网络与等待 / 复旦登录与授权 /
   云端算力 / 本地运行 / 恢复与状态机 / 字幕资料对齐 / 讲解与资料导入 / 量保护与数据页）；
   未知码落 GENERIC 诚实「未能确认」，禁一切通用猜测兜底语句（test_tasks_center_records 源码钉）。
   R3-09 指路面：本表覆盖=任务死因族；api.js ERROR_MESSAGES 覆盖登录/目录/传输/深度回答族；
   cloud_setup_required 两表逐字一致（同码同话）。 */
const TASK_FAILURE_GUIDANCE = Object.freeze({
  /* —— 网络与等待 —— */
  timeout: "任务等待后端响应超时。请确认网络后重试。",
  network_unavailable: "网络暂时不可用，任务已中断。请恢复网络后重试。",
  proxy_unavailable: "代理暂时不可用，任务已中断。可在设置中检查网络后重试。",
  rate_limited: "请求被临时限流。请等待几分钟后重试。",
  /* —— 复旦登录与授权 —— */
  authorization_required: "访问授权已过期，任务无法继续。请重新登录后重试。",
  authorization_revoked: "课程访问授权已被撤销。请重新登录确认授权后重试。",
  fudan_login_required: "还没登录复旦课程平台。请先登录，再重试这个任务。",
  permission_denied: "后端拒绝了本次访问。请重新登录后再重试。",
  /* B3 挑战识别器（§T21 模板）：学校风控挑战页需要人工点一次确认，程序不重试；
     词汇与登录侧 challenge_required 家族对齐，禁「检测到异常行为」类责备措辞 */
  platform_challenge_required: "学校登录页临时多了一道人机确认，程序没法替你点。稍后再试通常没事；若反复出现，去学校登录页手动登录一次即可解除。",
  /* —— 云端算力 —— */
  /* ⑫（LOG1）：云端机器被 infra 中途杀掉不是学生操作问题；客户端已自动重试一次 */
  remote_runner_lost: "这次失败是云端机器临时掉线，不是你的操作问题；我们已自动重试一次。",
  /* CLOUD-VERIFY-CHAIN-1 → CLOUD-FINAL-1 单元C：指向云端连接步骤（主入口，
     设置页）；每日自动化开关不在手动任务的门集内，不再指去学习页课程行。
     CLOUD-CONSENT-AUTO-1 U1：云端处理开关退役，cloud_disabled 分码随之退役——
     拒绝面只剩「连接/授权未完成」，文案统一指路连接步骤 */
  cloud_setup_required: "云端处理还没完成 GitHub 授权连接。请到「设置 → 网络与远程连接」完成连接步骤，再重试这个任务。",
  /* U2：不再猜「最常见原因是学校服务繁忙」类通用兜底——remote_failed 的真实来源是云端
     Runner/管线层失败，文案如实指向云端处理并给可行动建议 */
  remote_failed: "云端处理没有完成。稍等几分钟后重试通常可以恢复；反复出现时，可展开「技术详情」核对诊断信息。",
  /* AS2（CLOSED-RESULT-IMPORT-1）U3：本地 Worker 校验没通过是可修复的——
     指路「修复 Worker」，不提任何技术性责备措辞 */
  worker_tree_drifted: "云端处理的本地组件校验没通过：Worker 组件和云端版本没对上。到「设置」里点一次「修复 Worker」，完成后再重试这个任务。",
  /* —— 本地运行 —— */
  integrity_rejected: "结果未通过完整性校验，已被丢弃。请重试；若再次失败，可在「设置 → 更新与帮助」重新检查更新。",
  runtime_failed: "本地处理出现异常，请重试一次；若持续失败请重启客户端，你的数据不受影响。",
  task_failed: "任务失败，原因未确认。请稍后重试。",
  operation_failed: "任务执行失败。请重试；若持续失败，请稍后再试。",
  /* —— 恢复与任务状态机 —— */
  remote_recovery_material_unavailable: "重启后未自动恢复：本地恢复材料不可用，可取消后重新发起。",
  remote_recovery_cancel_not_safe: "当前不满足安全取消条件，取消未执行。请稍后再试。",
  task_already_active: "该讲次已有未完成任务。请先在列表中取消旧任务，再重新发起。",
  task_retry_not_supported: "该任务不支持续跑。请直接重新发起一次任务。",
  task_not_found: "任务记录已不存在。请重新发起任务。",
  operation_already_running: "同一任务已在进行中。请等待其完成后再操作。",
  operation_id_conflict: "操作与进行中的请求冲突。请稍后重试。",
  task_action_invalid: "当前任务状态不支持该操作。请刷新列表后重试。",
  task_action_rejected: "后端拒绝了该操作。请刷新列表后按最新状态操作。",
  task_retry_requires_failed_state: "只有失败的任务可以重试。",
  task_cancel_requires_active_state: "只有进行中的任务可以取消。",
  /* AS2/U2：显式「导入远端结果」的两种诚实拒绝——没有成品/钥匙、超期限，
     都直接告诉学生下一步该怎么办 */
  remote_import_unavailable: "云端没有找到可以导入的结果（可能本地已没有解密这份结果的钥匙）。可以直接重新发起一次任务。",
  remote_import_expired: "这个任务的云端结果已经超过可导入期限（生成后 7 天内可导入），取不回来了。重新发起一次任务即可。",
  /* P11 质量抽检：云端 worker 镜像版本较旧，不识别新任务类型——如实指路更新 */
  worker_kind_unsupported: "云端组件版本较旧，还认不得这种检查任务。请在「设置 → 更新与帮助」检查更新后再试一次。",
  /* P11：检查结果没通过闭集校验被整份丢弃——诚实失败，重发即重试 */
  judge_output_invalid: "这次质量检查的结果没有通过完整性校验，已被丢弃。重新发起一次检查通常就能恢复。",
  /* P11：排队期间讲次材料变了（字幕/总结缺失）——先重新生成再质检 */
  quality_no_material: "这一讲的字幕或总结材料暂时不完整，没法开始检查。先重新生成字幕或总结，再发起一次质量抽检。",
  task_cancel_not_supported: "该任务当前不支持取消。",
  /* DEAD-TASK-PURGE：A 启动僵尸清扫 / B 清除卡住任务的诚实死因码
     （与 http_api TASK_ERROR_CODES 等集钉 test_automation 严校，同笔落地） */
  zombie_session_cleanup: "上次客户端没有正常退出，这个任务没能继续。已把它标记为失败；需要的话重新发起一次即可，你的数据不受影响。",
  user_cleared_stuck: "这个任务卡住后被清除了，已生成的文件不受影响。需要的话可以重新发起一次任务。",
  /* APP500-CLOSE（MEDIA-RETRY-1 停车场同族）：讲次不在授权目录的显式码
     （enqueue 目录守卫/progress 读写面 404）入词表闭集；文案与 api.js
     ERROR_MESSAGES 同码同话逐字一致（等集钉 test_automation 严校）。 */
  lecture_not_found: "这门讲次不在当前课程目录里了。刷新目录即可同步。",
  /* —— 字幕、资料与对齐 —— */
  timeline_transcript_unavailable: "暂无可用字幕，无法生成时间线。请先生成字幕再试。",
  timeline_classification_failed: "时间线归类失败。请重试；若持续失败，请先生成字幕。",
  alignment_transcript_unavailable: "暂无可用字幕，无法对齐资料。请先生成字幕再试。",
  alignment_failed: "资料与字幕对齐失败。请确认资料内容后重试。",
  /* —— 讲解与课程分析 —— */
  concept_analysis_requires_two_courses: "课程关系分析需要两门及以上课程。请先添加更多课程。",
  question_explanation_not_configured: "讲解功能需要 DeepSeek Key。请在设置中保存后再试。",
  /* —— 资料导入 —— */
  document_import_failed: "资料导入失败。请确认文件后重试。",
  document_type_unsupported: "暂不支持该文件类型。请改用 PDF、PPTX、TXT 或 Markdown。",
  document_payload_invalid: "文件内容无法读取。请确认文件未损坏后重试。",
  document_empty: "文件内容为空，无法导入。",
  document_too_large: "文件超过大小限制。请压缩或拆分后再导入。",
  document_format_invalid: "文件格式不符合要求。请重新导出后再导入。",
  document_password_required: "文件已加密，无法导入。请解除密码保护后重试。",
  document_payload_not_recoverable: "文件内容无法恢复读取。请重新导出一份后再试。",
  /* —— 课程数据校验（预算门已移除，budget 两键随码表同笔退役）—— */
  course_data_action_invalid: "该数据操作未被接受。请刷新数据页后按最新状态重试。",
  course_data_page_invalid: "请求的页码超出范围。请返回列表后重试。",
  course_data_confirm_required: "该操作需要先按页面提示确认。请确认后重试。",
  course_data_target_invalid: "目标课程或讲次已不存在。请刷新列表后重试。",
});
/* U2：未知码如实「未能确认 + 诊断详情」，不再伪装成已查明的原因 */
const GENERIC_TASK_FAILURE = "这次失败的原因未能确认。可展开下方「技术详情」核对诊断信息后重试。";

function taskFailureGuidance(task) {
  return TASK_FAILURE_GUIDANCE[task.error_code] || GENERIC_TASK_FAILURE;
}

/* 任务类型闭集：键 = 后端 task.kind（src/runtime 下任务编排闭集）；未知 kind 诚实回退原始字符串 */
const TASK_KIND_LABELS = Object.freeze({
  subtitle: "字幕",
  summary: "摘要",
  question: "随堂问答",
  document_import: "资料导入",
  search_answer: "检索问答",
  quiz: "测验",
  review_plan: "复习计划",
  document_alignment: "资料对齐",
  timeline_classification: "时间线整理",
  concept_analysis: "概念分析",
  courseware_pdf: "课件 PDF",
  quality_judge: "质量抽检",
});

const TASK_STATE_LABELS = Object.freeze({
  queued: "排队中",
  running: "进行中",
  pausing: "正在暂停",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  canceled: "已取消",
});

const TASK_ACTION_LABELS = Object.freeze({
  pause: "暂停",
  resume: "继续",
  retry: "重试",
  cancel: "取消",
  import_result: "导入远端结果",
});

function taskNumber(value) {
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function taskProgressValues(task) {
  const completed = taskNumber(task.completed);
  const total = taskNumber(task.total);
  if (completed === null || total === null || total <= 0) return null;
  return { completed, total };
}

function taskDuration(seconds) {
  const value = Math.round(seconds);
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ""}`;
}

/* U1 终态卡瘦身：终态卡运行时长证据行文本。优先 elapsed_seconds>0（已运行）；
   次选 runner_seconds>0（云端运行——O2：卡面此前漏用 runner 秒，终态卡只剩
   误导性的「已运行 0 秒」）；再次回退 started_at（无则 created_at）→finished_at
   差值。全缺或 ≤0 = 空串不渲染该行：绝不编造时长，也不再渲染 0 秒。 */
function taskTerminalRuntimeText(task) {
  const elapsed = taskNumber(task.elapsed_seconds);
  if (elapsed !== null && elapsed > 0) return `已运行 ${taskDuration(elapsed)}`;
  const runner = taskNumber(task.runner_seconds);
  if (runner !== null && runner > 0) return `云端运行 ${taskDuration(runner)}`;
  const finished = Number(task.finished_at || 0);
  const started = Number(task.started_at || 0) || Number(task.created_at || 0);
  if (finished > 0 && started > 0 && finished > started) {
    return `已运行 ${taskDuration(finished - started)}`;
  }
  return "";
}

/* AS6 消耗透镜：任务行落库的 DeepSeek 消耗（人话单位）。仅 tokens>0 渲染；
   0=该任务无 LLM 参与（如实），历史任务无字段=不渲染，绝不编造。 */
function taskTokenUsageText(task) {
  const tokens = taskNumber(task.deepseek_tokens);
  if (tokens === null || tokens <= 0) return "";
  if (tokens < 10000) return `${tokens} tokens`;
  return `≈${Math.round(tokens / 1000) / 10} 万 tokens`;
}

function taskProgressText(task) {
  const completed = taskNumber(task.completed);
  const total = taskNumber(task.total);
  const percent = taskNumber(task.percent);
  if (completed !== null && total !== null && total > 0) {
    return task.progress_unit === "seconds"
      ? `${taskDuration(completed)} / ${taskDuration(total)}`
      : `${completed} / ${total} 项`;
  }
  return percent !== null && percent <= 100 ? `${percent}%` : null;
}

/* 友好分钟舍入：ETA/等待只按分钟档位展示，不做秒级倒计时（合同 §5.1 第 7 条） */
function friendlyMinutes(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.max(1, Math.ceil(value / 60));
}

/* estimate_basis 闭集（src/runtime/http_api.py public_task）：仅进技术详情，不发明新键 */
function taskEstimateDetail(task) {
  const basis = String(task.estimate_basis || "");
  const confidence = String(task.progress_confidence || "");
  if (!basis && !confidence) return "";
  return `${basis ? ` · estimate_basis: ${basis}` : ""}${confidence ? ` · confidence: ${confidence}` : ""}`;
}

/* SWEEPFIX-3 T7（化身走查 SWEEP1-T7）：技术详情补用量原始值行。人话用量卡面
   已有（AS6/O2 行：云端运行 X · ≈N 万 tokens）；此处沿 estimate_basis 同款
   raw-key 诊断面补精确值核对。键 = public_task 闭集；缺键不渲染不编造
   （S09-C 律）——runner_seconds 现被 public_task 省略（http_api.py 只发布
   deepseek_tokens），后端补发布后本行自动点亮，前端零再改。 */
function taskUsageDetail(task) {
  const parts = [];
  const tokens = taskNumber(task.deepseek_tokens);
  if (tokens !== null) parts.push(`deepseek_tokens: ${Math.round(tokens)}`);
  const runner = taskNumber(task.runner_seconds);
  if (runner !== null) parts.push(`runner_seconds: ${Math.round(runner)}`);
  return parts.join(" · ");
}

/* 排队等待锚点：优先 elapsed_queued_seconds，缺失回退 created_at 差值（合同 §5.1 第 9 条） */
function taskQueueWaitedMinutes(task) {
  const queued = taskNumber(task.elapsed_queued_seconds);
  if (queued !== null) return friendlyMinutes(queued);
  const created = taskNumber(task.created_at);
  if (created !== null) return friendlyMinutes(Math.max(0, Date.now() / 1000 - created));
  return null;
}

/* S09-C 后端新增组（queue/processing/completion/phases）只读闭集访问：
   缺键 = 后端按「省略而非编造」未发布，前端不猜数值。 */
function taskQueueGroup(task) {
  const group = task.queue;
  return group && typeof group === "object" ? group : null;
}

function taskProcessingGroup(task) {
  const group = task.processing;
  return group && typeof group === "object" ? group : null;
}

function taskRangeText(lower, upper, template) {
  const from = taskNumber(lower);
  const to = taskNumber(upper);
  if (from === null || to === null) return "";
  const a = friendlyMinutes(from);
  const b = friendlyMinutes(to);
  if (b > a) return template(a, b);
  return `约 ${b} 分钟`;
}

/* ETA 闭集映射（合同 §5.2 + S09-C）：
   stale → 等待重新确认；排队 → 已等待 X · 通常还需 A–B 开始 · 开始后预计 C–D；
   运行中 → 区间 → 点估计 → 重新估算 → 正在估算。排队等待不显示整体百分比。
   WAIT-UX-2（U2）：导出为闭集单源——搜索浮层答案卡与任务抽屉共用同一映射，
   禁止消费面另立第二套 ETA 文案。 */
export function taskEtaText(task) {
  const basis = String(task.estimate_basis || "");
  if (task.stale || basis === "stale") return "等待重新确认";
  if (task.state === "queued" || basis === "queue") {
    const parts = [];
    let queueRange = false;
    const waited = taskQueueWaitedMinutes(task);
    if (waited !== null) parts.push(`已等待 ${waited} 分钟`);
    const queue = taskQueueGroup(task);
    if (queue) {
      const range = taskRangeText(queue.lower_seconds, queue.upper_seconds, (a, b) => `通常还需 ${a}–${b} 分钟开始`);
      if (range) { parts.push(range); queueRange = true; }
    }
    if (!queueRange) parts.push("等待 GitHub Runner");
    const processing = taskProcessingGroup(task);
    if (processing) {
      const after = taskRangeText(processing.lower_seconds, processing.upper_seconds, (a, b) => `开始后预计 ${a}–${b} 分钟`);
      if (after) parts.push(after);
    }
    return parts.join(" · ");
  }
  const recalibrating = taskProcessingGroup(task)?.recalibrating;
  if (recalibrating) return "重新估算中";
  const remaining = taskNumber(task.remaining_seconds);
  const lower = taskNumber(task.remaining_lower_seconds);
  const upper = taskNumber(task.remaining_upper_seconds);
  if (lower !== null && upper !== null) {
    const from = friendlyMinutes(lower);
    const to = friendlyMinutes(upper);
    return to > from ? `预计还需 ${from}–${to} 分钟` : `预计约 ${to} 分钟`;
  }
  if (remaining === 0) return "预计剩余 0 秒"; /* 后端明确 0 剩余：不向上捏造「约 1 分钟」 */
  if (remaining !== null) {
    return `预计约 ${friendlyMinutes(remaining)} 分钟`;
  }
  const elapsed = taskNumber(task.elapsed_seconds);
  return elapsed === null ? "正在估算" : `正在估算 · 已运行 ${friendlyMinutes(elapsed)} 分钟`;
}

/* SWEEPFIX-2 T5（化身走查 SWEEP1-T5）：stale 进行中卡唯一态行（文案闭集）。
   修前双行=「等待重新确认」（t-evidence）+「证据已过期，等待重新确认」
   （t-stale-note）同卡复读，且「证据」是内部术语；收敛为唯一一行人话，
   t-evidence 复读行退役（taskStableEvidence 的 stale 分支返回空）。 */
const STALE_ACTIVE_NOTE_TEXT = "进度信息已过期，等待重新确认";

/* 证据行闭集（S09-C）：键 = processing/queue 组的 basis 闭集；
   sample_count 由后端随 estimate 提供，缺失时不编造「N 次」。 */
const EVIDENCE_BASIS_TEXT = Object.freeze({
  live_blend: (n) => (n > 0 ? `本次实时速度 + ${n} 次同类任务` : "本次实时速度"),
  history_median: (n) => (n > 0 ? `${n} 次同类任务` : "同类任务历史"),
  phase_history: () => "阶段历史进度",
  bootstrap: () => "同类任务冷启动基准",
});
const EVIDENCE_CONFIDENCE_LABELS = Object.freeze({ high: "高", medium: "中", low: "低" });

function taskEvidenceLine(task) {
  if (task.stale || String(task.estimate_basis || "") === "stale") return "";
  const basis = String(task.estimate_basis || "");
  if (task.state === "queued" || basis === "queue") {
    const queue = taskQueueGroup(task);
    if (!queue) return "";
    const count = Number(queue.sample_count) || 0;
    const confidence = EVIDENCE_CONFIDENCE_LABELS[String(queue.confidence || "")] || "";
    /* 排队区间来自受信 Worker 运行历史：措辞用「同类运行」区分任务样本 */
    return `依据：${count > 0 ? `${count} 次同类运行` : "同类运行历史"}${confidence ? ` · 可信度${confidence}` : ""}`;
  }
  const processing = taskProcessingGroup(task);
  if (!processing) return "";
  const builder = EVIDENCE_BASIS_TEXT[String(processing.basis || "")];
  if (!builder) return "";
  const confidence = EVIDENCE_CONFIDENCE_LABELS[String(processing.confidence || "")] || "";
  return `依据：${builder(Number(processing.sample_count) || 0)}${confidence ? ` · 可信度${confidence}` : ""}`;
}

/* 终态校准（S09-C）：prediction_outcome.delta_minutes 为正 = 比最初预计早。 */
function taskCalibrationText(task) {
  const outcome = task.prediction_outcome;
  if (!outcome || typeof outcome !== "object") return "";
  const delta = Number(outcome.delta_minutes);
  if (!Number.isFinite(delta)) return "";
  const rounded = Math.round(Math.abs(delta));
  if (rounded < 1) return "与最初预计用时相符";
  return delta > 0 ? `比最初预计早 ${rounded} 分钟` : `比最初预计晚 ${rounded} 分钟`;
}

/* 进度证据仅用于进行中/完成行；失败/取消行不渲染进度条与「无进度证据」。
   ETA 部分带“上次渲染字符串”守卫：同一分钟档位内字符串不变，DOM 不因心跳抖动（合同 §5.1 第 7 条）。 */
function taskProgressEvidence(task) {
  const key = String(task.task_id || "");
  const text = taskStableEvidence(task);
  if (lastEvidenceByTask.get(key) !== text) lastEvidenceByTask.set(key, text);
  return text;
}

function taskStableEvidence(task) {
  const progress = taskProgressText(task);
  const elapsed = taskNumber(task.elapsed_seconds);
  if (task.state === "completed") {
    if (task.stale) return "完成状态确认中";
    const duration = elapsed === null ? "" : ` · 用时 ${taskDuration(elapsed)}`;
    return progress ? `进度 ${progress}${duration}` : "任务已完成";
  }
  /* SWEEPFIX-2 T5：stale 进行中卡不再出本行——与 stale 注记行双行复读；
     唯一 stale 行=STALE_ACTIVE_NOTE_TEXT（渲染端空串即不渲染）。 */
  if (task.stale || String(task.estimate_basis || "") === "stale") return "";
  const eta = taskEtaText(task);
  return progress ? `进度 ${progress} · ${eta}` : `进度未知 · ${eta}`;
}

function taskResultNotice(task) {
  const notices = task.result_notices;
  if (!notices || typeof notices !== "object") return "";
  const skipped = slidesSkippedText(notices.slides_skipped);
  if (skipped) return `部分幻灯片已跳过（${skipped}）`;
  if (Array.isArray(notices.warnings) && notices.warnings.includes("slides_skipped")) {
    return "部分幻灯片已跳过";
  }
  /* G7（ASRBENCH P1）：AI 校对失败降级交付原始识别结果时诚实标注 */
  if (Array.isArray(notices.warnings) && notices.warnings.includes("proofread_degraded")) {
    return "AI 校对失败，已交付未经校订的原始字幕";
  }
  return "";
}

/* ---- S09-C 语义阶段轨：状态闭集来自后端 phases.items（completed/active/waiting/skipped），
   failed/stale 是前端基于任务状态的呈现派生（同一证据，不发明新后端状态）。
   形状 + 文本 + 颜色三通道：完成=实心、活动=双环、等待=空心、跳过=斜线，绝不只靠颜色。 ---- */

const PHASE_STATE_TEXT = Object.freeze({
  completed: "已完成",
  active: "进行中",
  waiting: "等待中",
  skipped: "已跳过",
  failed: "失败",
  stale: "待确认",
});

export function taskPhaseStates(task) {
  const phases = task.phases;
  const items = phases && typeof phases === "object" ? phases.items : null;
  if (!Array.isArray(items)) return [];
  const failed = task.state === "failed";
  const resolved = items.map((item) => {
    const state = PHASE_STATE_TEXT[item.state] ? String(item.state) : "waiting";
    /* 失败任务：后端仍报告的 active 阶段在卡片上按失败呈现 */
    if (failed && state === "active") return { id: String(item.id || ""), label: String(item.label || ""), state: "failed" };
    return { id: String(item.id || ""), label: String(item.label || ""), state };
  });
  /* U⑨ 诊断腿结论（第廿五案族）：阶段轨是串行序，进度进入后段后，前段
     最后一次上报可能不足 100%，后端按百分比判状态会把已越过的阶段滞留
     「等待中」。任何「等待中」阶段之后存在非等待阶段 → 它必然已被越过，
     按已完成如实呈现（已完成/进行中/等待中三态不虚报）。 */
  let lastNonWaiting = -1;
  resolved.forEach((phase, index) => {
    if (phase.state !== "waiting") lastNonWaiting = index;
  });
  resolved.forEach((phase, index) => {
    if (phase.state === "waiting" && index < lastNonWaiting) phase.state = "completed";
  });
  return resolved;
}

function renderPhaseRail(task) {
  const states = taskPhaseStates(task);
  if (!states.length) return null;
  const stale = task.stale || String(task.estimate_basis || "") === "stale";
  const rail = document.createElement("ol");
  rail.className = "t-rail";
  if (stale) rail.dataset.stale = "true";
  rail.setAttribute("aria-label", "任务阶段");
  states.forEach((phase) => {
    const item = document.createElement("li");
    item.className = "t-rail-phase";
    item.dataset.state = stale && phase.state === "active" ? "stale" : phase.state;
    const node = document.createElement("span");
    node.className = "t-rail-node";
    node.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "t-rail-label";
    label.textContent = phase.label || phase.id;
    const stateWord = PHASE_STATE_TEXT[item.dataset.state] || PHASE_STATE_TEXT.waiting;
    const stateNode = document.createElement("span");
    stateNode.className = "sr-only";
    stateNode.textContent = `：${stateWord}`;
    item.append(node, label, stateNode);
    rail.append(item);
  });
  return rail;
}

/* 学习材料输出闭集：键 = 后端 requested_outputs 值（question/enqueue 任务编排闭集） */
const LEARNING_OUTPUT_LABELS = Object.freeze({
  subtitle: "字幕",
  ocr: "课件OCR",
  summary: "总结",
  chapters: "章节",
  answer: "问答",
});

function outputChipLabels(task) {
  const outputs = Array.isArray(task.requested_outputs) ? task.requested_outputs : [];
  const labels = outputs
    .map((value) => LEARNING_OUTPUT_LABELS[String(value)] || String(value))
    .filter(Boolean);
  return labels.length ? labels : [taskKindLabel(task) || "任务"];
}

function updateTaskChip(value) {
  const active = value?.counts?.active;
  const failed = Number(value?.counts?.failed ?? 0);
  const count = $("task-chip-count");
  const failedVisible = (active ?? 0) === 0 && failed > 0;
  if (active == null) {
    count.hidden = true;
  } else {
    count.hidden = !(active > 0);
    count.textContent = String(active);
  }
  $("task-chip-dot").hidden = !failedVisible;
  $("task-chip-failed").hidden = !failedVisible;
  const chip = $("task-chip");
  chip.setAttribute("aria-label", active > 0
    ? `任务：${active} 个活动`
    : failedVisible ? "任务：存在失败任务" : "任务");
}

function courseTitleOf(store, task) {
  const courseId = String(task.course_id || "");
  if (!courseId) return "";
  const courses = store.courses || [];
  const course = courses.find(
    (item) => String(item.course_id || "") === courseId,
  );
  if (course) return String(course.title || "");
  /* AS10 U2：目录未就绪≠未关联——先给稳定占位「课程信息加载中」，就绪后
     随重渲染一次换到真名（无闪烁跳变）；目录已就绪仍找不到=真未关联，如实呈现。
     REALFULL-1 真测修正：登录就绪≠课程列表已装载（学生未进过课程选择页时
     store.courses 仍为空，已关联课程会被失实标成「未关联课程」）。判据改为
     列表本体：空列表=加载占位；非空且找不到=真未关联。 */
  return courses.length ? "未关联课程" : "课程信息加载中";
}

/* 讲次定位与 courseTitleOf 同法：course_id → sub_id 精确映射，不猜测 */
function lectureOf(store, task) {
  const courseId = String(task.course_id || "");
  const subId = String(task.sub_id || "");
  if (!courseId || !subId) return null;
  const course = (store.courses || []).find(
    (item) => String(item.course_id || "") === courseId,
  );
  if (!course) return null;
  return (course.lectures || []).find(
    (item) => String(item.sub_id || "") === subId,
  ) || null;
}

/* U2 日期恰一次：任务卡次行只放讲次名，日期锚点由相对时间槽唯一承载
   （讲次的课表日期不再另占一处）。学习材料父卡仍走 taskContextText（含日期）。 */
function lectureTitleOf(store, task) {
  const lecture = lectureOf(store, task);
  return lecture ? String(lecture.sub_title || "") : "";
}

function taskKindLabel(task) {
  const kind = String(task.kind || "");
  if (!kind) return "";
  const label = TASK_KIND_LABELS[kind];
  return label ? `${label}任务` : kind;
}

function taskContextText(store, task) {
  /* 次级小字只放讲次上下文：类型在主行、课程与时间在 t-meta，不重复 */
  const lecture = lectureOf(store, task);
  return lecture ? [lecture.date, lecture.sub_title].filter(Boolean).map(String).join(" ") : "";
}

/* 三层结构：正在处理（活动态，非终态一律归入此层，未知状态以「状态确认中」呈现）
   → 需要处理（失败，紧凑可操作）→ 历史记录（完成 + 取消，抽屉级折叠）
   AVATAR-POLISH-1 OBS-3（化身走查 OBS-3）：层名「进行中」与卡上「排队中/
   已暂停」徽章并存时读起来矛盾——层名改「正在处理」作活动统称，卡上状态
   徽章仍逐字用后端状态词表（TASK_STATE_LABELS 闭集零改动）。 */
const FAILED_VISIBLE_LIMIT = 5;
const TERMINAL_TASK_STATES = Object.freeze(new Set(["completed", "failed", "canceled"]));

function isActiveTask(task) {
  return !TERMINAL_TASK_STATES.has(task.state);
}

/* DEAD-TASK-PURGE B：卡住判定（前端提示层）= 在途态（queued/running/
   pausing）且 30 分钟无任何进展（updated_at）；远端仍在跑的
   （display_state=remote_running）提示层先排除，后端另有远端活跃白名单
   双门兜底，宁漏勿杀。 */
const STUCK_TASK_STATES = Object.freeze(new Set(["queued", "running", "pausing"]));
const STUCK_TASK_STALE_SECONDS = 30 * 60;

export function isStuckTask(task, nowSeconds = Date.now() / 1000) {
  if (!STUCK_TASK_STATES.has(String(task.state || ""))) return false;
  if (String(task.display_state || "") === "remote_running") return false;
  const updated = Number(task.updated_at || 0);
  return updated > 0 && nowSeconds - updated >= STUCK_TASK_STALE_SECONDS;
}

function taskRecency(task) {
  return Number(task.updated_at || 0) || 0;
}

function compareTaskRecency(a, b) {
  return taskRecency(b) - taskRecency(a);
}

/* 层内按 course_id 精确映射分组：缺失/未知 → 未关联课程，不猜测标题；
   组间按最近任务、组内按任务 updated_at 倒序。
   同一讲次同时存在字幕+摘要两类学习任务时合并为一张「学习材料」父卡
   （仅展示层合并，不创建/不改写任何任务记录）；单一学习任务保持独立卡片。 */
const LEARNING_PACK_KINDS = Object.freeze(new Set(["subtitle", "summary"]));
const LEARNING_PIPELINE_ORDER = Object.freeze({ subtitle: 0, summary: 1 });

function packLearningTasks(tasks) {
  const byLecture = new Map();
  const items = [];
  tasks.forEach((task) => {
    if (!LEARNING_PACK_KINDS.has(String(task.kind || ""))) {
      items.push({ kind: "task", task, recency: taskRecency(task) });
      return;
    }
    const key = `${String(task.course_id || "")}:${String(task.sub_id || "")}`;
    if (!byLecture.has(key)) byLecture.set(key, []);
    byLecture.get(key).push(task);
  });
  byLecture.forEach((groupTasks) => {
    const kinds = new Set(groupTasks.map((task) => String(task.kind || "")));
    if (groupTasks.length > 1 && kinds.size > 1) {
      items.push({
        kind: "pack", tasks: groupTasks,
        recency: Math.max(...groupTasks.map(taskRecency)),
      });
    } else {
      groupTasks.forEach((task) => items.push({ kind: "task", task, recency: taskRecency(task) }));
    }
  });
  items.sort((a, b) => b.recency - a.recency);
  return items;
}

function appendCourseGroups(parent, store, items, maxVisiblePerGroup = 0) {
  const groups = new Map();
  items.forEach((item) => {
    const anchor = item.kind === "pack" || item.kind === "aggregate" ? item.tasks[0] : item.task;
    /* OBS-2（化身走查 20261008）：courseTitleOf 空串=任务本来就不挂课程
       （学籍/自动化类任务种），分组标题用中性「不挂课程的任务」——「未关联
       课程」只留给有 course_id 却找不到真身的真异常，不再让学生误以为配置缺失。 */
    const key = courseTitleOf(store, anchor) || "不挂课程的任务";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  groups.forEach((groupItems, courseName) => {
    const group = document.createElement("section");
    group.className = "task-group";
    group.append(textElement("h3", courseName));
    const visible = maxVisiblePerGroup > 0 ? groupItems.slice(0, maxVisiblePerGroup) : groupItems;
    visible.forEach((item) => {
      group.append(
        item.kind === "pack" ? renderLearningPack(store, item.tasks)
          : item.kind === "aggregate" ? renderAggregatedTask(store, item.tasks)
            : renderTask(store, item.task),
      );
    });
    if (maxVisiblePerGroup > 0 && groupItems.length > maxVisiblePerGroup) {
      const overflow = document.createElement("details");
      overflow.className = "task-overflow";
      overflow.append(textElement("summary", `还有 ${groupItems.length - maxVisiblePerGroup} 个失败任务`));
      groupItems.slice(maxVisiblePerGroup).forEach((item) => {
        overflow.append(
          item.kind === "pack" ? renderLearningPack(store, item.tasks)
            : item.kind === "aggregate" ? renderAggregatedTask(store, item.tasks)
              : renderTask(store, item.task),
        );
      });
      group.append(overflow);
    }
    parent.append(group);
  });
}

/* 条目分层：学习材料父卡跟随其最重要任务所在层（活动 > 失败 > 历史），
   同一张父卡不会在三层重复出现。 */
/* 第十九案（U⑧）：同一 (course, sub_id, kind, 终态) 的任务在展示层聚合——
   重试 N 次的失败不再刷屏成 N 张同卡：一行「×N」徽标 + 最新一次错误与时间，
   details 展开看历次。已完成/已取消同样聚合。只改展示，不动任务记录。 */
function aggregateRepeatedItems(items) {
  const groups = new Map();
  const out = [];
  items.forEach((item) => {
    if (item.kind !== "task") { out.push(item); return; }
    const state = String(item.task.state || "");
    if (!TERMINAL_TASK_STATES.has(state)) { out.push(item); return; }
    const key = [
      String(item.task.course_id || ""), String(item.task.sub_id || ""),
      String(item.task.kind || ""), state,
    ].join(":");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  groups.forEach((group) => {
    if (group.length > 1) {
      out.push({
        kind: "aggregate",
        tasks: group.map((item) => item.task).sort(compareTaskRecency),
        recency: Math.max(...group.map((item) => item.recency)),
      });
    } else {
      out.push(group[0]);
    }
  });
  out.sort((a, b) => b.recency - a.recency);
  return out;
}

function renderAggregatedTask(store, tasks) {
  const ordered = [...tasks].sort(compareTaskRecency);
  const latest = ordered[0];
  const row = document.createElement("article");
  row.className = "task task-aggregate";
  row.dataset.state = String(latest.state || "unknown");
  const body = taskCardHeader(store, latest);
  const count = textElement("span", `×${ordered.length} 条同类记录`, "t-count");
  count.setAttribute("aria-label", `同一讲次同类任务共 ${ordered.length} 条记录，此处显示最新一条`);
  body.append(count);
  if (String(latest.state || "") === "failed") {
    body.append(textElement("p", taskFailureGuidance(latest), "t-failure"));
  }
  const details = document.createElement("details");
  details.className = "task-aggregate-details";
  details.append(textElement("summary", `历次记录 ${ordered.length} 条`));
  [...ordered].reverse().forEach((task) => details.append(renderTask(store, task)));
  body.append(details);
  /* 聚合头卡不带删除动作：×N 栈逐条记录在历次明细内各自删除 */
  row.append(body, renderTaskActions(store, latest, { allowDelete: false }));
  return row;
}

function tieredTaskItems(tasks) {
  const items = packLearningTasks(tasks);
  const isActiveItem = (item) => (item.kind === "pack" ? item.tasks.some((task) => isActiveTask(task)) : isActiveTask(item.task));
  const isFailedItem = (item) => (item.kind === "pack" ? item.tasks.some((task) => task.state === "failed") : item.task.state === "failed");
  const active = items.filter(isActiveItem);
  const failed = items.filter((item) => !isActiveItem(item) && isFailedItem(item));
  const failedKeys = new Set([...active, ...failed]);
  const history = items.filter((item) => !failedKeys.has(item));
  return { active, failed, history };
}

/* 顶部紧凑摘要：仅当每个活动任务都有后端完成上界时，才给「预计全部完成还需 ≤ N 分钟」；
   任一任务缺上界（冷启动/无预测）就诚实省略，不拼一个假总数。 */
function renderCompletionSummary(tasks) {
  const node = $("task-completion-summary");
  if (!node) return;
  const active = tasks.filter((task) => isActiveTask(task));
  const uppers = active
    .map((task) => Number(task.completion?.latest_seconds))
    .filter((value) => Number.isFinite(value) && value >= 0);
  if (!active.length || uppers.length !== active.length) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = `预计全部完成还需 ≤ ${friendlyMinutes(Math.max(...uppers))} 分钟`;
}

/* 语义状态播报：每次刷新至多一条、只在任务状态发生有意义跃迁时更新唯一
   live region；同一状态内的分钟档位重绘不播报（合同 §3.3 + S09-C）。 */
const MEANINGFUL_TRANSITION_STATES = Object.freeze(
  new Set(["queued", "running", "paused", "completed", "failed", "canceled"]),
);
const lastTaskSemantic = new Map();

function announceTaskTransitions(tasks) {
  const live = $("task-live");
  if (!live) return;
  for (const task of tasks) {
    const key = String(task.task_id || "");
    if (!key) continue;
    const semantic = `${String(task.state || "")}:${String(task.display_state || "")}`;
    const previous = lastTaskSemantic.get(key);
    lastTaskSemantic.set(key, semantic);
    if (previous === undefined || previous === semantic) continue;
    if (!MEANINGFUL_TRANSITION_STATES.has(String(task.state || ""))) continue;
    live.textContent = `「${taskKindLabel(task) || "任务"}」${TASK_STATE_LABELS[task.state] || "状态已更新"}`;
    return;
  }
}

/* SSE 刷新会整体重建任务列表；焦点若在列表内部，必须在重建后原位恢复，
   绝不把键盘用户抛回页面根部（合同 §6 无焦点窃取）。 */
function captureTaskFocus() {
  const active = document.activeElement;
  if (!active || !active.closest) return null;
  const host = active.closest("[data-task-key]");
  if (!host) return null;
  return { key: String(host.dataset.taskKey || ""), tag: active.tagName, text: String(active.textContent || "") };
}

function restoreTaskFocus(capture) {
  if (!capture || !capture.key) return;
  const candidates = Array.from(document.querySelectorAll("[data-task-key]"));
  for (const node of candidates) {
    if (String(node.dataset.taskKey || "") !== capture.key) continue;
    if (node.tagName !== capture.tag) continue;
    if (String(node.textContent || "") !== capture.text) continue;
    if (typeof node.focus === "function") node.focus({ preventScroll: true });
    return;
  }
}

/* PF1 稳态空闲循环降载（2026-10-07）：SSE/30s 轮询在任务零变化时不再全量
   重建。renderTasks 的 DOM 输出与副作用是 (value 语义字段, store.courses,
   store.auth.state) 的纯函数——三者皆同则重建必产出等价 DOM、等价计数行/
   chip/摘要、等价播报（无跃迁可播）、等价 store.tasks 内容，整体跳过即执行
   等价。指纹构成：
   - value 全量 JSON，唯一易变字段 observed_at 归零后进指纹（GET /tasks 每
     次响应都带新 observed_at，渲染零消费）；
   - store.courses 以「数组引用变化才重算」的缓存 JSON 进指纹（courseTitleOf/
     lectureOf 的目录读取；AS10 U2「目录就绪后随重渲染换真名」依赖它）；
   - store.auth.state（目录缺失时占位文案由 auth 态决定）。
   跳过路径仍更新 lastTasksLoadedAt（数据确已成功对账，新鲜度记账保持）。 */
let lastTasksFingerprint = "";
let lastTasksFingerprintStore = null;
/* DEAD-TASK-PURGE B：「清除卡住的任务」按钮引用——PF1 跳过路径仍要对钟
   （卡住判定含 30 分钟墙钟窗，载荷零变化也可能跨线），只翻可见性不做
   全量重建；每次全量重建先置空、仅在活动层存在时重挂。 */
let lastClearStuckButton = null;
let coursesFingerprintCache = { ref: null, text: "[]" };

function coursesFingerprint(courses) {
  if (coursesFingerprintCache.ref !== courses) {
    coursesFingerprintCache = { ref: courses, text: JSON.stringify(courses || []) };
  }
  return coursesFingerprintCache.text;
}

function tasksFingerprint(store, value) {
  const semantic = value && typeof value === "object" ? { ...value, observed_at: 0 } : value;
  return JSON.stringify([semantic, coursesFingerprint(store.courses), String(store.auth?.state || "")]);
}

export function renderTasks(store, value) {
  const tasks = value.tasks || [];
  const fingerprint = tasksFingerprint(store, value);
  const unchangedRender = fingerprint === lastTasksFingerprint && store === lastTasksFingerprintStore;
  lastTasksFingerprint = fingerprint;
  lastTasksFingerprintStore = store;
  lastTasksLoadedAt = Date.now();
  const stuckVisible = tasks.some((task) => isStuckTask(task));
  if (unchangedRender) {
    /* PF1 跳过路径的对钟补丁：跨过 30 分钟判定线时只翻按钮可见性。 */
    if (lastClearStuckButton) lastClearStuckButton.hidden = !stuckVisible;
    return;
  }
  lastClearStuckButton = null;
  store.set("tasks", tasks);
  const liveIds = new Set(tasks.map((task) => String(task.task_id || "")));
  for (const key of [...lastEvidenceByTask.keys()]) {
    if (!liveIds.has(key)) lastEvidenceByTask.delete(key);
  }
  for (const key of [...lastTaskSemantic.keys()]) {
    if (!liveIds.has(key)) lastTaskSemantic.delete(key);
  }
  updateTaskChip(value);
  $("task-counts").textContent = value.counts?.active == null
    ? evidenceText(value)
    : `${value.counts.active} 活动 · ${value.counts.failed} 失败 · ${value.counts.completed} 完成`;
  renderCompletionSummary(tasks);
  announceTaskTransitions(tasks);
  const focus = captureTaskFocus();
  const target = $("task-list");
  clear(target);
  if (!tasks.length) {
    target.append(textElement("p", "暂无任务", "empty-state"));
    /* CLOUD-CONSENT-AUTO-1 U3c（真浏览器走查发现）：本地任务为空时也必须重放
       自动材料运行段——否则 30s 任务轮询 / SSE 刷新把整段抹掉，学生再也看不到
       记录（现场实证：空任务装机下运行段每拍消失）。 */
    renderAutomationRunsSection();
    return;
  }
  const failedTaskCount = tasks.filter((task) => task.state === "failed").length;
  const tiers = tieredTaskItems(tasks);
  if (tiers.active.length) {
    const tier = document.createElement("section");
    tier.className = "task-tier";
    /* DEAD-TASK-PURGE B：层头部常驻挂载、按卡住存在性翻可见性——可见性
       是墙钟函数，PF1 跳过路径靠 lastClearStuckButton 引用补翻。 */
    const header = document.createElement("div");
    header.className = "task-tier-header";
    header.append(textElement("h3", "正在处理"));
    lastClearStuckButton = buildClearStuckButton(store);
    lastClearStuckButton.hidden = !stuckVisible;
    header.append(lastClearStuckButton);
    tier.append(header);
    appendCourseGroups(tier, store, tiers.active);
    target.append(tier);
  }
  if (tiers.failed.length) {
    const tier = document.createElement("section");
    tier.className = "task-tier task-tier-failed";
    /* 失败总数始终在层标题可见；超出上限的旧失败折叠进「还有 N 个」入口；
       U4：层头部同行提供「清理全部失败记录」显式清理动作 */
    const header = document.createElement("div");
    header.className = "task-tier-header";
    header.append(textElement("h3", `需要处理 · ${failedTaskCount} 个失败任务`));
    header.append(buildClearFailedButton(store));
    tier.append(header);
    appendCourseGroups(tier, store, aggregateRepeatedItems(tiers.failed), FAILED_VISIBLE_LIMIT);
    target.append(tier);
  }
  if (tiers.history.length) target.append(renderTaskHistory(store, tiers.history));
  renderAutomationRunsSection();
  restoreTaskFocus(focus);
}

/* ---- 自动学习材料运行记录（cloud-automation.v3）：消费既有
   GET /api/v3/automation 快照证据（runs），把自动材料运行并入任务抽屉统一
   呈现为任务记录；不发明新接口、不改后端 contract。无快照（读取失败）或
   「未开启且无运行记录」时整段隐藏，抽屉其余契约不变。 ---- */

function renderTaskHistory(store, items) {
  let completed = 0;
  let canceled = 0;
  let latest = 0;
  items.forEach((item) => {
    (item.kind === "pack" ? item.tasks : [item.task]).forEach((task) => {
      if (task.state === "completed") completed += 1;
      else if (task.state === "canceled") canceled += 1;
      latest = Math.max(latest, Number(task.finished_at || 0) || Number(task.updated_at || 0) || 0);
    });
  });
  const history = document.createElement("details");
  history.className = "task-history";
  history.append(
    textElement("summary", `历史记录 · ${completed} 已完成 · ${canceled} 已取消 · 最近 ${formatTime(latest)}`),
  );
  appendCourseGroups(history, store, aggregateRepeatedItems(items));
  return history;
}

/* 状态徽标闭集：键 = TASK_STATE_LABELS + 排队/恢复呈现派生（仅文案与形状强化） */
function taskStateWord(task) {
  return TASK_STATE_LABELS[task.state] || "状态确认中";
}

/* F10（化身走查 20261008）：重试授权闸闭集=失败文案指引「重新登录」的四码
   （键同 TASK_FAILURE_GUIDANCE，不发明新码）。会话未就绪时重试预拦截，
   与「生成字幕」的 401 预拦截同一语义；会话就绪照常放行。 */
const RETRY_AUTH_GATE_ERROR_CODES = Object.freeze(new Set([
  "fudan_login_required",
  "authorization_required",
  "authorization_revoked",
  "permission_denied",
]));

function retryAuthGateMessage(authState) {
  if (String(authState || "") === "checking") return "复旦会话还在登录中，等连上后再点重试";
  return "这门任务卡在了复旦登录：先重新登录复旦课程平台，连上后再点重试";
}

function appendActionButton(row, store, task, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = TASK_ACTION_LABELS[action] || action;
  if (action === "cancel") button.className = "danger";
  button.dataset.taskKey = String(task.task_id || "");
  button.addEventListener("click", async () => {
    /* F10（化身走查 20261008）：失败原因是复旦会话缺失族（卡片文案本就指引
       「重新登录」的闭集）时，重试与「生成字幕」的预拦截同一语义——会话未
       就绪就不把任务放回进行中空挂（挂起于授权缺失），先给人话引导；会话
       已就绪照常重试，不复读历史失败码。 */
    if (
      action === "retry"
      && RETRY_AUTH_GATE_ERROR_CODES.has(String(task.error_code || ""))
      && String(store.auth?.state || "") !== "ready"
    ) {
      toast(retryAuthGateMessage(store.auth?.state), "checking");
      return;
    }
    setBusy(button, true);
    try {
      await postV3("tasks/actions", { task_id: task.task_id, action, operation_id: operationId("task") });
      await loadTasks(store);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  row.append(button);
}

/* AS2/U2：显式「导入远端结果」只在失败/暂停卡且后端标记 remote_import.possible
   时出现；点击才触发（渲染/读面绝不派发导入），请求期间按钮置 busy。 */
function canImportRemoteResult(task) {
  const state = String(task.state || "");
  return (state === "failed" || state === "paused")
    && task.remote_import?.possible === true;
}

function appendImportResultButton(actions, store, task) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = TASK_ACTION_LABELS.import_result;
  button.dataset.taskKey = String(task.task_id || "");
  button.addEventListener("click", async () => {
    setBusy(button, true);
    try {
      const value = await postV3("tasks/actions", {
        task_id: task.task_id,
        action: "import_result",
        operation_id: operationId("task"),
      });
      const state = String(value?.task?.state || "");
      if (state === "completed") toast("云端结果已导入这门课");
      else if (state === "failed") toast("云端确认这次没有成功；卡片保留了失败原因", "error");
      else toast("云端还没跑完：结果一到就会自动核对导入");
      await loadTasks(store);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  actions.append(button);
}

function renderTaskActions(store, task, { allowDelete = true } = {}) {
  const actions = document.createElement("div");
  actions.className = "row-actions";
  if (canImportRemoteResult(task)) appendImportResultButton(actions, store, task);
  (task.actions || []).forEach((action) => appendActionButton(actions, store, task, action));
  /* U4 删除记录：仅终态卡携带；聚合卡头部不带（历次记录在各自卡内删除，
     避免对 ×N 栈出现「只删最新一条」的歧义入口）。 */
  if (allowDelete && TERMINAL_TASK_STATES.has(String(task.state || ""))) {
    appendDeleteButton(actions, store, task);
  }
  return actions;
}

function appendDeleteButton(actions, store, task) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "danger";
  button.textContent = "删除记录";
  button.dataset.taskKey = String(task.task_id || "");
  /* T2：确认态按任务身份跨重渲存活——数据轮询重建列表后，臂态在新按钮上延续 */
  button.dataset.confirmKey = `task-delete:${String(task.task_id || "")}`;
  restoreTwoStepArm(button);
  button.addEventListener("click", async () => {
    armTwoStepButton(button, "再点一次，删除这条记录", async () => {
      setBusy(button, true);
      try {
        await postV3("tasks/delete", { task_id: task.task_id });
        toast("已删除这条任务记录；已生成的字幕和文件不受影响");
        await loadTasks(store);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
  });
  actions.append(button);
}

/* U4：一键清理全部失败记录（失败层头部显式动作，无任何自动清理）。
   确认人话点明边界：只删任务记录，已生成的字幕/PDF 不受影响。 */
function buildClearFailedButton(store) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "清理全部失败记录";
  button.dataset.confirmKey = "tasks-clear-failed";
  restoreTwoStepArm(button);
  button.addEventListener("click", async () => {
    armTwoStepButton(button, "确认清理：只删任务记录，已生成的字幕/PDF 不受影响", async () => {
      setBusy(button, true);
      try {
        const value = await postV3("tasks/delete-failed", {});
        toast(`已清理 ${Number(value?.deleted) || 0} 条失败记录`);
        await loadTasks(store);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
  });
  return button;
}

/* DEAD-TASK-PURGE B：清除卡住的任务（进行中层头部显式动作，仅存在卡住
   任务时可见）。两击臂式确认人话点明边界；端点幂等（30 分钟无进展且
   远端无活跃 run 才算卡住），清 0 也诚实回报，绝不静默。 */
function buildClearStuckButton(store) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "清除卡住的任务";
  button.dataset.confirmKey = "tasks-clear-stuck";
  restoreTwoStepArm(button);
  button.addEventListener("click", async () => {
    armTwoStepButton(button, "这些任务可能因为异常退出而卡住，清除后不影响已完成的任务", async () => {
      setBusy(button, true);
      try {
        const value = await postV3("tasks/clear-stuck", {});
        const cleared = Number(value?.cleared) || 0;
        toast(cleared > 0 ? `已清除 ${cleared} 个卡住的任务` : "现在没有卡住的任务");
        await loadTasks(store);
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(button, false);
      }
    });
  });
  return button;
}

/* 可测量工作才有 3–5px 进度轨；排队等待没有可测量进度，绝不显示进度条或整体百分比。 */
function appendProgressTrack(body, task) {
  const progress = taskProgressValues(task);
  if (!progress) return;
  const bar = document.createElement("div");
  bar.className = "t-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", "任务进度");
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", String(Math.round(progress.total)));
  bar.setAttribute("aria-valuenow", String(Math.round(progress.completed)));
  const fill = document.createElement("div");
  fill.className = "fill";
  fill.style.width = `${Math.min(100, Math.round((progress.completed / progress.total) * 100))}%`;
  bar.append(fill);
  body.append(bar);
}

function taskCardHeader(store, task) {
  const body = document.createElement("div");
  body.className = "task-body";
  body.dataset.taskKey = String(task.task_id || "");
  const active = isActiveTask(task);
  const stateWord = taskStateWord(task);
  /* 主标识重排（用户 09-22 走查拍板）：首行 = 课程名视觉锚（无课程回退类型），
     次行 = 类型 · 讲次名（各出现恰一次），时间槽 = 人话相对时间。 */
  const course = courseTitleOf(store, task);
  const strong = textElement("strong", course || taskKindLabel(task) || task.label || task.kind || "任务", "t-name");
  strong.dataset.taskKey = String(task.task_id || "");
  const badge = textElement("span", stateWord, "t-state");
  badge.dataset.taskKey = String(task.task_id || "");
  badge.dataset.badgeState = String(task.display_state || task.state || "unknown");
  body.append(strong, badge);
  /* SWEEPFIX-3 T6（化身走查 SWEEP1-T6）：进行中卡附后端发布的当前阶段人话
     label（public_task 顶层 label 源自 progress.label；旧读法 task.progress?.label
     在 API 形状里不存在=死路，走查种子「正在转写音频」因此永不显示） */
  const progressLabel = active ? String(task.label || "") : "";
  if (progressLabel && progressLabel !== stateWord) {
    body.append(textElement("span", progressLabel, "t-phase"));
  }
  /* 日期恰一次：讲次只放名字；类型在有课程时退到次行，无课程时已在主行不重复 */
  const context = [
    ...(course ? [taskKindLabel(task)] : []),
    lectureTitleOf(store, task),
  ].filter(Boolean).join(" · ");
  if (context) body.append(textElement("span", context, "t-context"));
  if (
    String(task.kind || "") === "subtitle"
    && task.payload?.subtitle_proofread === false
  ) {
    body.append(textElement("span", "无大模型校对（自动降级）", "t-mode-note"));
  }
  const at = active ? taskRecency(task)
    : Number(task.finished_at || 0) || Number(task.updated_at || 0) || 0;
  const meta = at ? formatRelativeTime(at) : "";
  if (meta) body.append(textElement("span", meta, "t-meta"));
  return body;
}

function renderTask(store, task) {
  const row = document.createElement("article");
  row.className = "task";
  row.dataset.state = task.state || "unknown";
  const body = taskCardHeader(store, task);
  const active = isActiveTask(task);
  const stale = Boolean(task.stale) || String(task.estimate_basis || "") === "stale";
  /* 进度轨与证据仅进行中卡直出（三态阶段板保留）；终态卡瘦身（用户 09-22
     走查拍板）：六阶段表默认收起进「阶段详情」，失败定位仍可展开核对，
     卡面只保留一行运行时长——进度条/ETA 证据/校准行不再上终态卡。 */
  if (active) {
    appendProgressTrack(body, task);
    const rail = renderPhaseRail(task);
    if (rail) body.append(rail);
    const evidenceText = taskProgressEvidence(task);
    /* SWEEPFIX-2 T5：stale 进行中卡证据行为空串 → 不渲染（唯一 stale 行见下） */
    if (evidenceText) body.append(textElement("span", evidenceText, "t-evidence"));
    const evidence = taskEvidenceLine(task);
    if (evidence && !stale) body.append(textElement("span", evidence, "t-basis"));
  } else if (task.state === "completed" || task.state === "failed") {
    const rail = renderPhaseRail(task);
    if (rail) {
      const phases = document.createElement("details");
      phases.className = "task-phases";
      phases.append(textElement("summary", "阶段详情"), rail);
      body.append(phases);
    }
    const runtime = taskTerminalRuntimeText(task);
    if (runtime) {
      /* AS6：消耗紧跟运行时长同行呈现；无消耗记录时行文案不变。 */
      const usage = taskTokenUsageText(task);
      body.append(textElement("span", `${runtime}${usage ? ` · ${usage}` : ""}`, "t-evidence"));
    }
  }
  if (stale && active) body.append(textElement("span", STALE_ACTIVE_NOTE_TEXT, "t-stale-note"));
  if (task.state === "failed") {
    body.append(textElement("p", taskFailureGuidance(task), "t-failure"));
  }
  const technical = document.createElement("details");
  technical.className = "task-technical";
  technical.append(
    textElement("summary", "技术详情"),
    textElement("code", `state: ${task.state || "unknown"}${task.error_code ? ` · error_code: ${task.error_code}` : ""}${taskEstimateDetail(task)}`),
  );
  /* SWEEPFIX-3 T7：用量精确值行（键缺省整行不渲染，见 taskUsageDetail） */
  const usageDetail = taskUsageDetail(task);
  if (usageDetail) technical.append(textElement("code", usageDetail));
  body.append(technical);
  const notice = taskResultNotice(task);
  if (notice) body.append(textElement("span", notice, "task-result-notices"));
  /* 校准行是过程证据：终态卡瘦身（U1）后只在进行中卡呈现 */
  const calibration = taskCalibrationText(task);
  if (calibration && active) body.append(textElement("span", calibration, "t-calibration"));
  row.append(body, renderTaskActions(store, task));
  return row;
}

/* 学习材料父卡：同一讲次「字幕 + 摘要」管线合并展示。
   产出 chips 用后端 requested_outputs 闭集；未完成的下一个产出在视觉上优先
   （完整阶段轨 + ETA），已完成产出折叠为单行结论。 */
function renderLearningPack(store, tasks) {
  const row = document.createElement("article");
  row.className = "task task-pack";
  row.dataset.state = "pack";
  const ordered = [...tasks].sort(
    (a, b) => (LEARNING_PIPELINE_ORDER[String(a.kind)] ?? 9) - (LEARNING_PIPELINE_ORDER[String(b.kind)] ?? 9),
  );
  const body = document.createElement("div");
  body.className = "task-body";
  const anchorTask = ordered[0];
  const anchorId = String(anchorTask.task_id || "");
  body.dataset.taskKey = anchorId;
  const strong = textElement("strong", "学习材料", "t-name");
  strong.dataset.taskKey = anchorId;
  body.append(strong);
  const primary = ordered.find((task) => isActiveTask(task)) || ordered[0];
  const primaryBadge = textElement("span", taskStateWord(primary), "t-state");
  primaryBadge.dataset.badgeState = String(primary.display_state || primary.state || "unknown");
  body.append(primaryBadge);
  const context = taskContextText(store, anchorTask);
  if (context) body.append(textElement("span", context, "t-context"));
  const chips = document.createElement("div");
  chips.className = "t-chips";
  chips.setAttribute("aria-label", "产出内容");
  const seen = new Set();
  ordered.forEach((task) => {
    outputChipLabels(task).forEach((label) => {
      if (seen.has(label)) return;
      seen.add(label);
      const chip = document.createElement("span");
      chip.className = "t-chip";
      chip.textContent = label;
      chips.append(chip);
    });
  });
  body.append(chips);
  ordered.forEach((task) => {
    const isPrimary = task === primary;
    const item = document.createElement("div");
    item.className = "pack-item";
    item.dataset.taskKey = String(task.task_id || "");
    item.dataset.primary = String(isPrimary);
    const kindLabel = outputChipLabels(task)[0] || taskKindLabel(task) || "任务";
    const head = document.createElement("div");
    head.className = "pack-item-head";
    head.append(textElement("span", kindLabel, "pack-item-kind"));
    head.append(textElement("span", taskStateWord(task), "pack-item-state"));
    item.append(head);
    if (isPrimary) {
      const rail = renderPhaseRail(task);
      if (rail) item.append(rail);
      appendProgressTrack(item, task);
      const evidenceText = taskProgressEvidence(task);
      /* SWEEPFIX-2 T5：同主卡律——stale 复读行退役，唯一 stale 行见下 */
      if (evidenceText) item.append(textElement("span", evidenceText, "t-evidence"));
      const evidence = taskEvidenceLine(task);
      if (evidence) item.append(textElement("span", evidence, "t-basis"));
      const staleNote = Boolean(task.stale) || String(task.estimate_basis || "") === "stale";
      if (staleNote) item.append(textElement("span", STALE_ACTIVE_NOTE_TEXT, "t-stale-note"));
      const failure = task.state === "failed" ? taskFailureGuidance(task) : "";
      if (failure) item.append(textElement("p", failure, "t-failure"));
    } else {
      const line = [taskProgressText(task), taskCalibrationText(task)].filter(Boolean).join(" · ");
      if (line) item.append(textElement("span", line, "pack-item-line"));
    }
    item.append(renderTaskActions(store, task));
    body.append(item);
  });
  const notice = taskResultNotice(primary);
  if (notice) body.append(textElement("span", notice, "task-result-notices"));
  row.append(body);
  return row;
}

/* NIGHT2-G-B（P32 形态 2）：字幕就绪轻播报——首帧拉取只建基线不播报；
   任务由进行中态走到 completed 且尚未播报过时 toast 恰一次。不跳转、
   不打断播放，学生不用再开抽屉「碰」到字幕好了。 */
const TASK_ACTIVE_STATES = new Set(["queued", "awaiting_payload", "running", "canceling", "pausing", "paused"]);
const lastTaskStates = new Map();
const announcedCompletedTasks = new Set();
let taskBaselineSeeded = false;

export function announceNewlyCompletedSubtitleTasks(tasks) {
  for (const task of tasks) {
    const id = String(task?.task_id || "");
    const state = String(task?.state || "");
    const previous = lastTaskStates.get(id);
    lastTaskStates.set(id, state);
    if (!taskBaselineSeeded) continue;
    /* 甲-1b（MATERIALS-DECLUTTER-1）：课件 PDF 任务活跃→完成的跃迁广播给
       学习页资料区——覆盖生成入口不在资料页（如任务中心重试）时状态行
       滞留旧页数/旧时间戳的缺口；资料页按 sub_id 定向决定是否刷新。 */
    if (task?.kind === "courseware_pdf" && state === "completed"
      && previous && TASK_ACTIVE_STATES.has(previous) && !announcedCompletedTasks.has(id)) {
      announcedCompletedTasks.add(id);
      window.dispatchEvent(new CustomEvent("courselens:materials-refresh", {
        detail: { course_id: String(task.course_id || ""), sub_id: String(task.sub_id || "") },
      }));
    }
    if (task?.kind !== "subtitle" || state !== "completed") continue;
    if (announcedCompletedTasks.has(id)) continue;
    if (previous && TASK_ACTIVE_STATES.has(previous)) {
      announcedCompletedTasks.add(id);
      /* SWEEPFIX-R2 W1（D-20261009-15①）：完成跃迁同帧广播 transcript-refresh
         （甲-1b courseware_pdf 同族手法）——学生停在本讲时文稿热重读、笔记
         按钮随 transcriptHasTiming 热启用；toast 保留给不在本讲的学生指路。 */
      window.dispatchEvent(new CustomEvent("courselens:transcript-refresh", {
        detail: { course_id: String(task.course_id || ""), sub_id: String(task.sub_id || "") },
      }));
      toast("字幕已就绪，回到这一讲播放即可开启");
    }
  }
  taskBaselineSeeded = true;
}
