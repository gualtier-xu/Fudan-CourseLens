/* ARCH-DEBT-1：本文件由 settings.js 按 R3-27/R7-#27 词干族设计稿拆分而来，
   纯移动重组、行为零变化；更新与隐私说明面（隐私与更新族）。settings.js 门面保留组合根与
   公共导出（re-export 门面法，R3-22 手法）。 */
import { postV3 } from "../api.js";
import { $, clear, setBusy, textElement, toast } from "../ui.js";
import { macUpdateEntry, macUpdateSnapshot, onMacUpdateChange } from "../update-mac.js";
import { closedDiagnosticValue, closedMapValue, formatBytes, isPlainRecord } from "./shared.js";

/* AS5 U3：《隐私与数据说明》入仓为 docs/privacy-notice.md，客户端内置查看
   入口渲染同文；workbench 钉两者逐字恒等，不会漂移。改文书请两处同笔改。 */
const PRIVACY_NOTICE_MARKDOWN = `# CourseLens 隐私与数据说明

版本：v1.2.1（2026-10-09 补 macOS 测试版注记与本地学习统计披露；2026-10-02 与 README「安全与隐私架构」同口径更新）
适用范围：CourseLens Windows 客户端及其云端代算组件；macOS 版为测试版，平台差异见「账号与凭据怎么保护」。

## 先说结论

你的学习资料默认只保存在你自己的电脑上。只有你主动发起的生成任务（字幕、AI 总结等）会把必要的课程材料加密送到你自己的云端仓库去处理，结果加密回传；课程材料与结果（密文）在云端最长保留 30 天，任务状态记录最长保留 90 天。CourseLens 没有遥测：不存在统计、埋点或上报，我们这边没有任何你的数据。AI 全部在你的专属云端 Worker 上执行——你的电脑上没有任何 AI 模型，也不做本地推理。

## 你的数据在哪里

- 课程目录、课表、观看进度、回看热点、书签、笔记、测验与复习计划，全部只写入这台电脑上的 CourseLens 本机数据目录，不经任何第三方服务器中转。
- 回看热点默认记录：它只记你反复回看的位置（回拖、重放、暂停、慢速），用来在进度条上标出难点，帮你自己回头找重点。它只存在这台电脑上；不想记某一讲时，在该讲次播放器的操作区点「抹掉本讲热点」，只清那一讲，字幕、课件和学习记录都不受影响。
- 「下次接着播」的续播点与本机偏好（主题、排序等）同样只存在这台电脑上。
- 本地学习统计默认未开启（不影响使用）；开启后在本机加记观看事件（位置、时长、倍速、完成）与引用点击、停留计数，全都只存这台电脑，随时可以在设置页「隐私」节关闭或删除。

## 谁能看到你的数据

- CourseLens 没有遥测：外联目标在代码里是一张固定闭集表，表之外没有任何统计、埋点或上报端点。
- 客户端只会连接以下服务，此外不发起任何网络连接：
  - 复旦课程平台（id.fudan.edu.cn、webvpn.fudan.edu.cn、icourse.fudan.edu.cn、fdjwgl.fudan.edu.cn、yjsxktest.fudan.sh.cn）：登录、课程目录、课表（含研究生课表数据源）、直播与回放；
  - GitHub（github.com 与 api.github.com）：你的专属云端仓库与云端任务；
  - DeepSeek（api.deepseek.com）：你使用 AI 问答等能力时，用你自己配置的 API Key 直接访问；
  - 应用更新下载（release-assets.githubusercontent.com / objects.githubusercontent.com）：检查与下载更新包，走更新链专用验签白名单。
- 错误一律以闭集状态码和中文文案呈现，不会把你的账号信息写进日志或界面。

## 账号与凭据怎么保护

- 学号、密码和 API Key 只在客户端本地的受保护输入框中填写，经 Windows 数据保护接口（DPAPI）加密后存盘：文件里只有密文，日志、界面和诊断输出都不会回显它们。
- 不勾选「保存在本机」的凭据只在本次启动内使用，退出即消失。
- macOS 测试版：凭据经 macOS 钥匙串（Keychain）加密、只存在本机，学习数据保存在「Application Support/CourseLens」目录——同为「只存这台电脑」的本地存储不变式。
- GitHub App 按最小权限安装：授权范围收紧为仅两个受管仓库（你的专属公共 Worker 仓库与私有 Mailbox 仓库），不覆盖账号下其他仓库，也不请求账单或用量读取权限。

## 云端代算：数据什么时候离开你的电脑

- 只有生成字幕、AI 总结这类重活会使用云端代算；在线播放和课程目录始终走你的复旦登录会话，两者相互独立。
- 云端代算发生在你自己的 GitHub 仓库（GitHub Actions 运行环境）里，不经过 CourseLens 作者的任何服务器；只有你勾选的课程会被处理。
- 需要拉取课件或媒体的任务，会在端到端加密的「任务信封」里带上所需的校方会话凭据（Worker 用它以你的身份拉取这门课的资源）；开了 AI 校对的任务会带上你的 DeepSeek Key。信封只密封给你自己的专属 Worker，GitHub 与传输链路上只见密文，CourseLens 作者侧读不到内容。
- 出域的课程材料与回传结果全程加密；课程材料与结果（密文）最长保留 30 天，任务状态记录最长保留 90 天。
- 你随时可以在连接卡的「高级操作与诊断」里撤销云端授权，并删除云端凭据与状态。

## 你随时可以做的事

- 抹掉某一讲的回看热点：该讲次播放器操作区的「抹掉本讲热点」。
- 删除本机学习记录：设置的数据管理页；只删记录，已生成的字幕与课件不受影响。
- 撤销云端授权并删除云端凭据与状态：连接卡的「高级操作与诊断」。
- 一键重置：清空所有本机数据后关闭应用；重置不可恢复，动它之前请想清楚。
- 卸载时选「保留学习数据」（推荐默认），本机数据原地不动，重装回来一切如旧；选「否」才会删除。

## 有疑问

《隐私与数据说明》与 README「安全与隐私架构」章节保持同一口径；发现说明与实际行为不符时，请通过官方仓 Issues 或设置页「帮助」里的问题反馈入口告诉我们。
`;

export function renderPrivacyDoc(target) {
  clear(target);
  let list = null;
  for (const rawLine of PRIVACY_NOTICE_MARKDOWN.split("\n")) {
    const line = rawLine.trim();
    if (!line) { list = null; continue; }
    if (line.startsWith("# ")) continue; /* 文档标题由弹层标题呈现 */
    if (line.startsWith("## ")) {
      list = null;
      target.append(textElement("h3", line.slice(3)));
      continue;
    }
    if (line.startsWith("- ")) {
      if (!list) {
        list = document.createElement("ul");
        target.append(list);
      }
      const li = document.createElement("li");
      li.textContent = line.slice(2);
      list.append(li);
      continue;
    }
    list = null;
    target.append(textElement("p", line));
  }
}

export let updateValue = null;
const updateLabels = {
  idle: "尚未检查", checking: "正在检查", offline: "离线",
  up_to_date: "已是最新", available: "发现更新", downloading: "正在下载",
  verifying: "正在验证", ready_to_restart: "等待重启", applying: "正在应用",
  healthy: "更新成功", rolled_back: "已自动回滚", failed: "更新失败",
  policy_blocked: "策略阻止",
};
const UPDATE_STATES = new Set(Object.keys(updateLabels));
const ROLLBACK_STATES = new Set(["none", "available", "awaiting_health", "rolled_back"]);

const UPDATE_ERROR_GUIDANCE = Object.freeze({
  managed_install_required: "此客户端不是受管理的 Windows 安装。请使用组织提供的受管理安装程序后再检查更新。",
  production_gates_incomplete: "更新发布条件尚未完成。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  update_not_configured: "当前更新服务尚未完成发布配置。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  distribution_policy_invalid: "当前更新策略无法确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  host_platform_mismatch: "此设备不符合受管理 Windows 更新条件。请使用受支持的 Windows 客户端。",
  current_version_below_security_floor: "当前客户端需要通过独立的受管理安装程序恢复。请重新运行安装程序完成恢复。",
  rollback_unavailable: "更新无法安全回滚。请停止继续安装；稍后可重新检查或重新安装客户端。",
  startup_health_failed: "更新后的健康确认未完成，已按安全策略处理。请重新检查；若持续出现，可重新安装客户端。",
  staged_update_missing: "更新准备状态不可用。请重新检查更新。",
  pending_update_missing: "等待重启的更新状态不可用。请重新检查更新。",
  confirmation_required: "下载或安装需要你在页面确认。请确认后再继续。",
  update_busy: "另一个更新操作仍在进行。请稍候再检查。",
  update_restart_blocked: "有任务正在进行，暂时不能开始更新。请等待任务完成后再试。",
  network_unavailable: "暂时无法连接更新服务。请确认网络后重新检查。",
  download_http_error: "暂时无法获取更新包。请稍后重新检查。",
  download_redirect_untrusted: "更新下载被安全策略阻止，请稍后重试；若持续出现，可重新检查更新或重新安装客户端。",
  download_encoding_unsupported: "更新服务返回了不受支持的数据。请稍后重新检查。",
  download_too_large: "更新包大小无法安全确认。请重新检查更新。",
  insufficient_disk_space: "可用磁盘空间不足。请释放空间后重新检查更新。",
  manifest_changed: "更新信息已变化。请重新检查后再确认操作。",
  manifest_invalid: "更新信息未通过安全确认。请稍后重新检查。",
  manifest_schema_invalid: "更新信息未通过安全确认。请稍后重新检查。",
  manifest_signature_invalid: "更新信息未通过安全确认。请稍后重新检查。",
  manifest_key_untrusted: "更新信息未通过安全确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  manifest_key_epoch_rollback: "更新信息未通过安全确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  manifest_channel_mismatch: "当前客户端不适用该更新。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  manifest_platform_mismatch: "当前客户端不适用该更新。请使用受支持的 Windows 客户端。",
  manifest_prerelease_blocked: "当前发布渠道不接受该更新。请稍后重新检查。",
  manifest_time_invalid: "更新信息的时间无法确认。请检查系统时间或稍后重新检查。",
  manifest_expired_or_future: "更新信息的有效期无法确认。请校正系统时间后重新检查。",
  manifest_replay_or_downgrade: "该更新不符合安全版本要求。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  manifest_mix_and_match: "更新信息未通过安全确认。请稍后重新检查。",
  manifest_package_invalid: "更新包信息未通过安全确认。请重新检查更新。",
  manifest_version_invalid: "更新版本信息无法确认。请稍后重新检查。",
  package_invalid: "更新包未通过安全确认。请重新检查更新。",
  package_size_mismatch: "更新包未通过安全确认。请重新检查更新。",
  package_hash_mismatch: "更新包未通过安全确认。请重新检查更新。",
  package_file_hash_mismatch: "更新包未通过安全确认。请重新检查更新。",
  package_format_unsupported: "更新包格式不受支持。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  package_archive_unsafe: "更新包未通过安全确认。请重新检查更新。",
  package_inventory_mismatch: "更新包未通过安全确认。请重新检查更新。",
  package_identity_mismatch: "更新包未通过安全确认。请重新检查更新。",
  package_path_invalid: "更新准备位置无法安全确认。请重新检查更新。",
  accepted_history_invalid: "本地更新记录无法确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  trust_config_invalid: "本地更新信任配置无法确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  trust_schema_invalid: "本地更新信任配置无法确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  release_key_authorization_invalid: "更新信息未通过安全确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  release_key_authorization_conflict: "更新信息未通过安全确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  update_root_untrusted: "本地更新信任配置无法确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  source_url_invalid: "更新服务地址无法安全确认。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  source_url_blocked: "更新服务地址不符合安全策略。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  source_address_blocked: "更新服务地址不符合安全策略。请稍后重新检查；若持续出现，可重新安装客户端后再试。",
  update_action_invalid: "当前操作不可用。请重新检查更新。",
  health_confirmation_invalid: "更新后的健康确认无效。请重新检查；若持续出现，可重新安装客户端。",
});
const UPDATE_ERROR_CODES = new Set(Object.keys(UPDATE_ERROR_GUIDANCE));
const DATA_VALUE_FAILURE = Symbol("data_value_failure");

const GENERIC_UPDATE_GUIDANCE = "更新暂未继续。请稍后重新检查；若持续出现，可重新安装客户端。";

/* POLISH-1 F6（化身走查 FULL-CLIENT-INSPECT F6）：「检查更新」点击此前零可见
   反馈——「上次检查」停在「尚未检查」、无 toast，学生不知道点没点上。修后：
   点击即确认（MF-5 先例），完成后按闭集终态给一句结果——check 是同步动作，
   202 响应本身就带终态快照，不再只靠随后的 GET 刷新面。键=更新状态闭集。 */
const UPDATE_CHECK_DONE_TEXT = Object.freeze({
  up_to_date: "检查完成：已是最新版本。",
  offline: "检查未完成：暂时无法连接更新服务，请确认网络后再试。",
  failed: "检查未完成：更新卡下方说明了原因。",
  policy_blocked: "检查未完成：此设备的更新策略暂不允许，说明见更新卡。",
});

export function updateErrorGuidance(errorCode, state = "") {
  const code = typeof errorCode === "string" ? errorCode.trim() : "";
  if (code) return closedMapValue(UPDATE_ERROR_GUIDANCE, code, GENERIC_UPDATE_GUIDANCE);
  return typeof state === "string" && ["failed", "policy_blocked", "rolled_back"].includes(state)
    ? GENERIC_UPDATE_GUIDANCE
    : "";
}

function confirmedUpdateVersion(value) {
  if (typeof value !== "string" || value.length > 64) return "";
  const version = value.trim();
  return /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$/.test(version) ? version : "";
}

export function ownDataValue(record, key) {
  try {
    if (!isPlainRecord(record)) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(record, key);
    return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
  } catch {
    return DATA_VALUE_FAILURE;
  }
}

export function safeUpdateDiagnostics(value) {
  if (!isPlainRecord(value)) {
    return {
      available: false, state: null, error_code: null, current_version: null, available_version: null,
      rollback: { available: false, state: null, version: null },
    };
  }
  const state = ownDataValue(value, "state");
  const errorCode = ownDataValue(value, "error_code");
  const currentVersion = ownDataValue(value, "current_version");
  const availableVersion = ownDataValue(value, "available_version");
  const rollback = ownDataValue(value, "rollback");
  if ([state, errorCode, currentVersion, availableVersion, rollback].includes(DATA_VALUE_FAILURE)) {
    return safeUpdateDiagnostics(null);
  }
  const rollbackIsRecord = isPlainRecord(rollback);
  const safeRollback = rollbackIsRecord ? rollback : null;
  const rollbackAvailableValue = ownDataValue(safeRollback, "available");
  const rollbackState = ownDataValue(safeRollback, "state");
  const rollbackVersion = ownDataValue(safeRollback, "version");
  const rollbackReadFailed = [rollbackAvailableValue, rollbackState, rollbackVersion].includes(DATA_VALUE_FAILURE);
  const rollbackAvailable = !rollbackReadFailed && rollbackAvailableValue === true;
  return {
    available: true,
    state: closedDiagnosticValue(state, UPDATE_STATES),
    error_code: closedDiagnosticValue(errorCode, UPDATE_ERROR_CODES),
    current_version: confirmedUpdateVersion(currentVersion) || null,
    available_version: confirmedUpdateVersion(availableVersion) || null,
    rollback: {
      available: rollbackAvailable,
      state: rollbackReadFailed || (rollback != null && !rollbackIsRecord)
        ? "unknown"
        : closedDiagnosticValue(rollbackState, ROLLBACK_STATES),
      version: rollbackAvailable ? confirmedUpdateVersion(rollbackVersion) || null : null,
    },
  };
}

export function updateRecoveryGuidance(value) {
  const state = typeof value?.state === "string" ? value.state : "";
  const rollback = value?.rollback && typeof value.rollback === "object" ? value.rollback : {};
  const rollbackState = typeof rollback.state === "string" ? rollback.state : "";
  const rollbackVersion = rollback.available === true ? confirmedUpdateVersion(rollback.version) : "";
  const currentVersion = confirmedUpdateVersion(value?.current_version);
  if (state === "ready_to_restart") return "重启后将开始更新，并进行健康确认。";
  if (state === "applying") {
    return rollbackState === "awaiting_health" && rollbackVersion
      ? `正在等待健康确认；如未通过，可自动恢复到已确认版本 ${rollbackVersion}。`
      : "正在等待健康确认；恢复状态暂无法确认。";
  }
  if (state === "rolled_back") {
    return currentVersion
      ? `已自动恢复到上一已确认版本 ${currentVersion}。`
      : "已自动恢复；恢复版本暂无法确认。";
  }
  return "";
}

/* UPDATE-PANEL-1（MAC-3 决策 C 落地）：mac 测试版没有应用内自动更新链，
   更新组呈「手动下载」入口而非误导性的自动更新控件；自动检查控件一并让位
   （自动化缺省：不加设置项）。Windows 面逐位不动——本分支只在 darwin 宿主
   进入；后端对 mac 的 fail-closed（host_platform_mismatch→policy_blocked）
   语义零改动，快照到不到都不影响入口态。
   UPDATE-UX-1（2026-10-09 用户令：有新版本必须醒目提醒）：入口态升级为
   三态检查面——检查道在 update-mac.js（公开 Releases 列表比对当前版本），
   本卡只渲染闭集状态：有新版=醒目句+三步安装+下载页锚；无新版=安静的
   「已是最新」；检查失败=诚实降级（网络原因，绝不误导成「已是最新」）。 */
const MAC_PHASE_PILL = Object.freeze({
  idle: "手动下载", checking: "正在检查", available: "发现更新",
  up_to_date: "已是最新", unavailable: "手动下载",
});

function macVersionLine(state) {
  if (state.phase === "available") {
    return state.currentVersion
      ? `当前版本 ${state.currentVersion}；装好新版第一次启动，若询问一次钥匙串访问，点「允许」即可。`
      : "装好新版第一次启动，若询问一次钥匙串访问，点「允许」即可。";
  }
  if (state.phase === "up_to_date") {
    return state.currentVersion ? `当前版本 ${state.currentVersion}。` : "";
  }
  return "";
}

function macMessage(state) {
  if (state.phase === "checking") return "正在检查更新…";
  if (state.phase === "available") {
    return state.version ? `发现新版本 ${state.version}，可以更新了。` : "发现新版本，可以更新了。";
  }
  if (state.phase === "up_to_date") return "已是最新版本。";
  if (state.phase === "unavailable") {
    return "检查没有完成：暂时连不上更新检查服务。稍后再试，或直接打开下载页确认——不会因此错过新版本。";
  }
  return "macOS 测试版：点「检查更新」看看有没有新版本；安装包在下载页手动获取。";
}

function renderMacUpdateFace(state) {
  $("update-state").textContent = closedMapValue(MAC_PHASE_PILL, state.phase, "手动下载");
  $("update-windows-face").hidden = true;
  $("update-mac-entry").hidden = false;
  $("update-mac-message").textContent = macMessage(state);
  const versionLine = macVersionLine(state);
  $("update-mac-version").hidden = !versionLine;
  $("update-mac-version").textContent = versionLine;
  $("update-mac-steps").hidden = state.phase !== "available";
  $("update-mac-check").disabled = state.phase === "checking";
}

function renderMacUpdateEntry() {
  renderMacUpdateFace(macUpdateSnapshot());
}

/* mac 检查道状态→卡面即时重绘（面板自订阅；顶栏订阅在 update-widget.js） */
onMacUpdateChange((state) => {
  if (macUpdateEntry()) renderMacUpdateFace(state);
});

/* UPDATE-UX-1 零呆等三律（W 道）：downloading 显示字节进度（>2s 显进度）与
   预计剩余（>10s 才示 ETA；数值=本次传输实测速率的在线外推，无速率不编数）。 */
let downloadSample = null;
let downloadRate = null;

function estimateDownloadEta(bytes, total) {
  const now = Date.now();
  if (downloadSample && bytes > downloadSample.bytes && now > downloadSample.at) {
    const instant = (bytes - downloadSample.bytes) / ((now - downloadSample.at) / 1000);
    downloadRate = downloadRate === null ? instant : downloadRate * 0.5 + instant * 0.5;
  }
  downloadSample = { bytes, at: now };
  if (!downloadRate || downloadRate <= 0) return null;
  return Math.max(0, (total - bytes) / downloadRate);
}

function renderUpdateProgress(value) {
  const target = $("update-progress");
  if (!target) return;
  const active = !!value && value.state === "downloading"
    && Number.isFinite(value.download_bytes) && Number.isFinite(value.download_total)
    && value.download_total > 0;
  if (!active) {
    downloadSample = null;
    downloadRate = null;
    target.hidden = true;
    target.textContent = "";
    return;
  }
  const bytes = Math.max(0, Math.min(value.download_bytes, value.download_total));
  const total = value.download_total;
  const percent = Math.round((bytes / total) * 100);
  let text = `正在下载 ${percent}%（${formatBytes(bytes)} / ${formatBytes(total)}）`;
  const eta = estimateDownloadEta(bytes, total);
  if (eta !== null && eta > 10) {
    text += eta >= 60
      ? ` · 按当前速度预计还需约 ${Math.max(1, Math.round(eta / 60))} 分钟`
      : ` · 按当前速度预计还需约 ${Math.max(1, Math.ceil(eta))} 秒`;
  }
  target.hidden = false;
  target.textContent = text;
}

/* SIMPLIFY-AUDIT-1 S5：五事实行收敛为人话一行——「当前版本 x → 可用版本 y（大小）」；
   无可用更新时只报当前版本。「通道」是开发行话，学生无决策用途，不再上脸。 */
function updateFactsLine(value) {
  const current = value.current_version || "—";
  if (!value.available_version) return `当前版本 ${current}`;
  const size = formatBytes(value.package_size);
  const sizeNote = size && size !== "—" ? `（${size}）` : "";
  return `当前版本 ${current} → 可用版本 ${value.available_version}${sizeNote}`;
}

/* 更新卡是 store `update` 键的订阅方（update-widget.js 是唯一轮询者）。
   value === undefined：快照未到达，保持「正在读取」；value === null：快照获取
   失败，诚实显示通用指引；其余按快照渲染。mac 宿主一律入口态，不看快照。 */
export function renderUpdate(value) {
  if (macUpdateEntry()) {
    renderMacUpdateEntry();
    return;
  }
  renderUpdateProgress(value);
  if (value == null) {
    $("update-state").textContent = "正在读取";
    $("update-error").hidden = value !== null;
    if (value === null) $("update-error").textContent = GENERIC_UPDATE_GUIDANCE;
    return;
  }
  updateValue = value;
  $("update-state").textContent = closedMapValue(updateLabels, value.state, "状态未知");
  $("update-facts-line").textContent = updateFactsLine(value);
  $("update-last-check").textContent = value.last_checked_at
    ? `上次检查：${new Date(value.last_checked_at * 1000).toLocaleString()}`
    : "尚未检查更新";
  $("update-notes").textContent = value.release_notes || "本版说明暂未提供。";
  const guidance = updateErrorGuidance(value.error_code, value.state);
  $("update-error").hidden = !guidance;
  $("update-error").textContent = guidance;
  const recoveryGuidance = updateRecoveryGuidance(value);
  $("update-recovery").hidden = !recoveryGuidance;
  $("update-recovery").textContent = recoveryGuidance;
  const busy = ["checking", "downloading", "verifying", "applying"].includes(value.state);
  $("check-update").disabled = busy;
  $("download-update").disabled = busy || !(value.actions || []).includes("download");
  $("install-update").disabled = busy || !(value.actions || []).includes("install");
}

export async function updateAction(action, button, confirmed = false) {
  /* POLISH-1 F6：检查更新=点击即反馈（in-flight toast），完成按响应闭集终态
     出结果句；其余动作（download/install）反馈面不变。 */
  const checking = action === "check";
  if (checking) toast("正在检查更新…", "checking");
  setBusy(button, true);
  try {
    const snapshot = await postV3("client-update/actions", { action, confirmed });
    if (checking && isPlainRecord(snapshot)) {
      const state = String(ownDataValue(snapshot, "state") ?? "");
      if (state === "available") {
        const version = confirmedUpdateVersion(ownDataValue(snapshot, "available_version"));
        toast(version ? `检查完成：发现新版本 ${version}，可在下方下载。` : "检查完成：发现新版本，可在下方下载。", "ready");
      } else {
        const done = closedMapValue(UPDATE_CHECK_DONE_TEXT, state, "检查完成。");
        toast(done, state === "offline" || state === "failed" || state === "policy_blocked" ? "caution" : "ready");
      }
    }
  } catch {
    toast("更新操作暂未完成，请稍后重新检查。", "error");
  } finally {
    setBusy(button, false);
    /* 唯一轮询者合同：动作后由 update-widget 重新获取并发布，卡面随订阅重绘 */
    window.dispatchEvent(new Event("courselens:update-refresh"));
  }
}
