/* 数据搬家包（D12 数据主权 P0）：数据管理页「搬家与备份」区。
   导出 = 设密码 → 后端打包 → a[download] 直交系统另存（与资料导出同法，
   绝不在应用目录留第二份给用户）；导入 = 选包上传（XHR 带进度）→ 校验+
   落位 → 应用自动关闭，重开后给「重录三件套」清单（凭据 DPAPI 不可迁移
   =安全特性，只给清单不做迁移）。
   文案闭集：错误码走 api.js 码表 + 后端闭集码，未知码回退兜底句；零 raw
   异常上屏；下载文件名/URL 只取后端回执披露的受信形态，绝不本地拼接。 */
import { postV3 } from "../api.js";
import { $, operationId, setBusy } from "../ui.js";
/* WAIT-UX-1：导入服务段档位预计单源取自等待预期闭集（数据来源/重测日期见其头注） */
import { migrationImportExpectation } from "../wait-expectations.js";

const MIGRATION_ACTION_RESULT_SCHEMA = "courselens.data-migration-action-result.v1";
const MIGRATION_PASSWORD_MIN = 8;
/* WAIT-UX-1 缺进度 J3/缺 ETA J3+：服务段秒级步进节拍 */
const MIGRATION_IMPORT_TICK_MS = 1000;

const MIGRATION_TEXT = Object.freeze({
  exporting: "正在打包…（数据多的话可能要几分钟，先别关窗口）",
  exportDone: (filename, sizeText) => (
    `搬家包已生成（${filename}${sizeText ? `，${sizeText}` : ""}）。正在交给浏览器保存——选 U 盘、网盘或任何你想放的位置即可带走。密码请自己记牢，CourseLens 无法找回。`
  ),
  uploading: (pct) => (pct == null ? "正在上传搬家包…" : `正在上传搬家包… ${pct}%`),
  /* WAIT-UX-1 J3/J3+：服务段此前是纯静态文案。现在按包体给档位预计（闭集单源）
     并逐秒步进已用时间——上传完成后点击即见动态进度，大包不再像卡死。 */
  importing: (expectation, elapsedText) => (
    `上传完成，正在校验、解密并安家…（${elapsedText ? `${elapsedText}；` : ""}${expectation}；完成后应用会自动关闭）`
  ),
  importDone: (rebased) => {
    const base = "导入完成，应用即将关闭。重新打开 CourseLens 后完成 3 件重录：① 重新登录复旦账号；② 重新连接 GitHub 授权；③ 重新填写 DeepSeek Key。";
    return rebased > 0 ? `${base}（已自动修正 ${rebased} 条资料路径）` : base;
  },
  passwordShort: `密码至少 ${MIGRATION_PASSWORD_MIN} 位，再想一个吧。`,
  passwordMismatch: "两次输入的密码不一样。",
  noFile: "先选一个搬家包文件（.clmig）。",
  failure: "搬家包操作没能完成，请重试一次。",
});

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function showLine(node, text) {
  if (!node) return;
  node.hidden = !text;
  node.textContent = text || "";
}

/* D-20261009-01 同族（本道恰域审计新增发现）：搬家 export/import 的
   rejected 回执同样走 HTTP 409 信封（status=rejected+blockers 闭集码），
   修前落 migrationFailure(error.message)="HTTP 409" 裸英文——C3-3 精神违背。
   码表=后端 _CLIENT_RESET_BLOCKER_CODES 同族闭集，未知码不渲染绝不发明。 */
const MIGRATION_BLOCKER_TEXT = Object.freeze({
  active_task: "有正在进行的生成任务",
  active_remote_run: "有远程计算进行中",
  active_automation_import: "有等待导入的自动化结果",
  automation_rule: "有覆盖相关课程的自动化规则",
  cleanup_pending: "有待完成的安全清理",
  active_token_lease: "有云端计算还在进行",
});

function migrationFailure(error) {
  const receipt = error?.receipt;
  if (receipt?.status === "rejected" && Array.isArray(receipt.blockers)) {
    const parts = [];
    for (const blocker of receipt.blockers) {
      const hint = MIGRATION_BLOCKER_TEXT[String(blocker?.code || "")];
      if (!hint) continue; /* 未知码不渲染，绝不发明 */
      const count = Number(blocker.count || 0);
      parts.push(hint + (count > 1 ? `（${count}）` : ""));
    }
    if (parts.length) {
      return `搬家包操作没有执行：${parts.join("；")}。处理完再试一次就好。`;
    }
  }
  const message = String(error?.message || "").trim();
  return message || MIGRATION_TEXT.failure;
}

/* 上传走 XHR 才有逐字节进度（fetch 上传进度 WebView2 支持不齐）；包体
   不读进内存，直接交浏览器流式发送。响应闭集：只认 v3 信封 JSON。 */
function uploadPackage(file, onProgress) {
  return new Promise((resolve, reject) => {
    let request;
    try {
      request = new XMLHttpRequest();
    } catch {
      reject(new Error(MIGRATION_TEXT.failure));
      return;
    }
    request.open("POST", "/api/v3/data-migration/package");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    if (request.upload) {
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && event.total > 0) {
          onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
        }
      });
    }
    request.addEventListener("load", () => {
      let payload = null;
      try {
        payload = JSON.parse(request.responseText);
      } catch {
        payload = null;
      }
      if (request.status >= 200 && request.status < 300 && payload?.schema === "courselens.api.v3") {
        resolve(payload.data);
        return;
      }
      const code = String(payload?.error_code || "http_error");
      const failure = new Error(MIGRATION_TEXT.failure);
      failure.code = code;
      failure.instruction = String(payload?.instruction || "");
      reject(failure);
    });
    request.addEventListener("error", () => reject(new Error("网络暂不可用")));
    try {
      request.send(file);
    } catch {
      reject(new Error(MIGRATION_TEXT.failure));
    }
  });
}

export function installDataMigration() {
  const exportButton = $("data-migration-export");
  const importButton = $("data-migration-import");
  const statusLine = $("data-migration-status");
  const errorLine = $("data-migration-error");
  const exportDialog = $("data-migration-export-dialog");
  const importDialog = $("data-migration-import-dialog");
  if (!exportButton || !importButton || !exportDialog || !importDialog) return () => {};

  const resetExportDialog = () => {
    $("data-migration-export-password").value = "";
    $("data-migration-export-password-confirm").value = "";
    showLine($("data-migration-export-error"), "");
  };
  const resetImportDialog = () => {
    $("data-migration-import-file").value = "";
    $("data-migration-import-password").value = "";
    showLine($("data-migration-import-status"), "");
    showLine($("data-migration-import-error"), "");
  };

  const handleExport = () => {
    resetExportDialog();
    exportDialog.showModal();
  };
  const handleImport = () => {
    resetImportDialog();
    importDialog.showModal();
  };
  const handleExportCancel = () => exportDialog.close();
  const handleImportCancel = () => importDialog.close();

  const handleExportConfirm = async () => {
    const password = $("data-migration-export-password").value;
    const confirm = $("data-migration-export-password-confirm").value;
    const dialogError = $("data-migration-export-error");
    if (password.length < MIGRATION_PASSWORD_MIN) {
      showLine(dialogError, MIGRATION_TEXT.passwordShort);
      return;
    }
    if (password !== confirm) {
      showLine(dialogError, MIGRATION_TEXT.passwordMismatch);
      return;
    }
    showLine(dialogError, "");
    showLine(errorLine, "");
    showLine(statusLine, MIGRATION_TEXT.exporting);
    setBusy(exportButton, true);
    try {
      const receipt = await postV3("data-migration/actions", {
        action: "export",
        operation_id: operationId("data-migration-export"),
        password,
      });
      const valid = receipt?.schema === MIGRATION_ACTION_RESULT_SCHEMA
        && receipt?.status === "accepted"
        && typeof receipt?.result?.download?.url === "string"
        && receipt.result.download.url.startsWith("/api/v3/data-migration/file?token=");
      if (!valid) {
        showLine(statusLine, "");
        showLine(errorLine, MIGRATION_TEXT.failure);
        return;
      }
      const download = receipt.result.download;
      /* 与资料导出同法：现有文件直交系统另存，应用目录不留第二份。 */
      const link = document.createElement("a");
      link.setAttribute("href", download.url);
      link.setAttribute("download", String(download.filename || ""));
      (document.body || document.documentElement).appendChild?.(link);
      link.click();
      link.remove?.();
      showLine(statusLine, MIGRATION_TEXT.exportDone(
        String(download.filename || ""),
        formatBytes(download.bytes),
      ));
      exportDialog.close();
    } catch (error) {
      showLine(statusLine, "");
      showLine(errorLine, migrationFailure(error));
    } finally {
      setBusy(exportButton, false);
    }
  };

  const handleImportConfirm = async () => {
    const fileInput = $("data-migration-import-file");
    const password = $("data-migration-import-password").value;
    const dialogStatus = $("data-migration-import-status");
    const dialogError = $("data-migration-import-error");
    const file = fileInput?.files?.[0] || null;
    if (!file) {
      showLine(dialogError, MIGRATION_TEXT.noFile);
      return;
    }
    if (password.length < MIGRATION_PASSWORD_MIN) {
      showLine(dialogError, MIGRATION_TEXT.passwordShort);
      return;
    }
    showLine(dialogError, "");
    setBusy(importButton, true);
    try {
      showLine(dialogStatus, MIGRATION_TEXT.uploading(null));
      const staged = await uploadPackage(file, (pct) => showLine(dialogStatus, MIGRATION_TEXT.uploading(pct)));
      if (!staged?.package_id) {
        throw new Error(MIGRATION_TEXT.failure);
      }
      /* WAIT-UX-1 J3/J3+：服务段动态进度——按包体档位预计 + 已用秒数步进。
         终局两态（本行「安家中」→ 页级完成行）保持既有语义不变。 */
      const importExpectation = migrationImportExpectation(file.size);
      const importStartedAt = Date.now();
      const showImportingStatus = () => {
        const elapsed = Math.floor((Date.now() - importStartedAt) / 1000);
        showLine(dialogStatus, MIGRATION_TEXT.importing(importExpectation, elapsed > 0 ? `已用 ${elapsed} 秒` : ""));
      };
      showImportingStatus();
      const importTimer = setInterval(showImportingStatus, MIGRATION_IMPORT_TICK_MS);
      let receipt;
      try {
        receipt = await postV3("data-migration/actions", {
          action: "import",
          operation_id: operationId("data-migration-import"),
          package_id: String(staged.package_id),
          password,
        });
      } finally {
        clearInterval(importTimer);
      }
      const valid = receipt?.schema === MIGRATION_ACTION_RESULT_SCHEMA && receipt?.status === "accepted";
      if (!valid) {
        throw new Error(MIGRATION_TEXT.failure);
      }
      importDialog.close();
      showLine(statusLine, MIGRATION_TEXT.importDone(Number(receipt?.result?.rebased_document_paths || 0)));
    } catch (error) {
      showLine(dialogStatus, "");
      showLine(dialogError, migrationFailure(error));
    } finally {
      setBusy(importButton, false);
    }
  };

  const handleExportSubmit = (event) => {
    event.preventDefault();
    void handleExportConfirm();
  };
  const handleImportSubmit = (event) => {
    event.preventDefault();
    void handleImportConfirm();
  };

  exportButton.addEventListener("click", handleExport);
  importButton.addEventListener("click", handleImport);
  $("data-migration-export-cancel")?.addEventListener("click", handleExportCancel);
  $("data-migration-import-cancel")?.addEventListener("click", handleImportCancel);
  $("data-migration-export-form")?.addEventListener("submit", handleExportSubmit);
  $("data-migration-import-form")?.addEventListener("submit", handleImportSubmit);
  exportDialog.addEventListener("close", resetExportDialog);
  importDialog.addEventListener("close", resetImportDialog);

  return () => {
    exportButton.removeEventListener("click", handleExport);
    importButton.removeEventListener("click", handleImport);
    $("data-migration-export-cancel")?.removeEventListener("click", handleExportCancel);
    $("data-migration-import-cancel")?.removeEventListener("click", handleImportCancel);
    $("data-migration-export-form")?.removeEventListener("submit", handleExportSubmit);
    $("data-migration-import-form")?.removeEventListener("submit", handleImportSubmit);
    exportDialog.removeEventListener("close", resetExportDialog);
    importDialog.removeEventListener("close", resetImportDialog);
  };
}
