import { apiV3, postV3 } from "./api.js";
import { selectPage } from "./shell.js";
import {
  $, clear, closeOverlay, evidenceDetails, recoveryActionLabel, setBusy, textElement, toast,
} from "./ui.js";

const GUIDE_VERSION = "student-onboarding.v1";
const TOTAL_STEPS = 5;
const STEP_TITLES = Object.freeze({
  1: "先了解工作方式",
  2: "连接复旦课程平台",
  3: "确认课程目录",
  4: "准备生成能力",
  5: "开始学习",
});

/* 每个进程至多自动打开一次；手动入口不受限 */
let autoOpenAttempted = false;

/* GitHub App 安装状态证据仅含仓库全名与官方设置地址，可安全展示。
   两种闭集形态：首次安装（两个专属仓库已在官方安装页自动预选）与范围调整（缺/多选） */
function installationScopeText(evidence) {
  if (evidence?.reason) {
    /* 读取失败时缺少/多选列表不可信，按未知中性呈现 */
    return "GitHub / Worker 连接：暂时无法确认 App 仓库选择，可稍后重试。";
  }
  const missing = Array.isArray(evidence?.missing) ? evidence.missing.filter(Boolean) : [];
  const unexpected = Array.isArray(evidence?.unexpected) ? evidence.unexpected.filter(Boolean) : [];
  const settingsUrl = typeof evidence?.settings_url === "string" && evidence.settings_url
    ? ` 管理入口：${evidence.settings_url}`
    : "";
  if (!missing.length && !unexpected.length) {
    return (
      "GitHub / Worker 连接：需要在 GitHub 确认安装 CourseLens App；"
      + "你的 Worker 与 Mailbox 两个仓库已自动预选，无需手动创建、搜索或勾选仓库。"
      + `${settingsUrl}`
    );
  }
  const parts = [];
  if (missing.length) parts.push(`缺少 ${missing.join("、")}`);
  if (unexpected.length) parts.push(`多选了 ${unexpected.join("、")}`);
  return (
    "GitHub / Worker 连接：需要在 GitHub 安装 CourseLens App，并仅勾选你的 Worker 与 Mailbox 两个仓库。"
    + `（${parts.join("；")}）${settingsUrl}`
  );
}

/* 步骤 4 代理卡闭集文案：检测状态、指引与错误全部闭集，绝不回显自由文本 */
const PROXY_CARD_TEXT = Object.freeze({
  label: "本机代理（GitHub 连接）",
  detecting: "正在检测本机代理端口…",
  found: (port, source) =>
    `检测到 127.0.0.1:${port} 有响应（${source === "system" ? "来自系统代理设置" : "来自常见端口检测"}）。它未必是可用代理，可直接保存试用或手动修改。`,
  notFound: "未检测到可用代理，可手动填写，或稍后在设置中配置。",
  pacDetected: "系统是 PAC 自动代理模式；已尝试常见本地端口未命中，可在上方手动填写 HTTP 代理端口。",
  error: "检测暂时失败，可重新检测或手动填写。",
  guide: "常见代理软件的 HTTP 端口可在其设置中查看（如 Clash 默认 7890、v2RayN 默认 10809）。"
    + "只支持 HTTP(S) 代理端口，不支持 SOCKS 代理。",
  invalidProxy: "只支持 HTTP 或 HTTPS 代理地址。",
  saved: (proxy) => `代理设置已保存：${proxy}。`,
  saveFailed: "代理保存失败，请稍后重试。",
});

export async function installOnboarding(store) {
  let visible = false;
  let originPage = "study";
  let currentStep = 1;
  let defaultStepApplied = false;
  let seq = 0;
  let controller = null;
  let shellSnapshot = null;
  let onboardingPayload = null;
  let guideSnapshot = null;
  let readsPending = true;
  let readsStale = false;
  let prevAuthState = "";
  let prevCatalogState = "";
  let actionBusy = false;
  let settingsJumpPending = false; /* 一次性：仅标记“由引导发起”的设置跳转 */
  let proxyNodes = null; /* 步骤 4 代理卡 DOM（首次进入引导时构建一次） */
  const proxyState = { busy: false, saving: false, token: 0, result: null, savedEcho: "" };

  const announce = (message) => {
    const live = $("onboarding-live");
    if (live) live.textContent = message;
  };

  const coreReadiness = () => {
    const auth = store.auth || shellSnapshot?.authentication || null;
    const catalog = shellSnapshot?.catalog || null;
    return {
      authReady: auth?.state === "ready",
      catalogReady: catalog?.state === "ready",
    };
  };

  /* 重开默认步：completed → 5；dismissed → 第一个未就绪核心步；new/invalid/其他 → 1 */
  const defaultStepFor = (guide) => {
    const disposition = guide?.persistence === "ready" ? guide?.disposition : "";
    if (disposition === "completed") return 5;
    if (disposition === "dismissed") {
      const { authReady, catalogReady } = coreReadiness();
      if (!authReady) return 2;
      if (!catalogReady) return 3;
      return 5;
    }
    return 1;
  };

  function renderNotice() {
    const notice = $("onboarding-pending");
    if (readsPending) {
      notice.hidden = false;
      notice.textContent = "正在读取引导状态…";
    } else if (readsStale) {
      notice.hidden = false;
      notice.textContent = "状态读取暂时失败，以下显示上次确认的状态，可稍后重试。";
    } else {
      notice.hidden = true;
      notice.textContent = "";
    }
    document.querySelectorAll("[data-onboarding-panel]").forEach((node) => {
      node.hidden = readsPending || node.dataset.onboardingPanel !== String(currentStep);
    });
  }

  function renderStep(next, { focus = true } = {}) {
    currentStep = Math.min(Math.max(1, Number(next) || 1), TOTAL_STEPS);
    document.querySelectorAll("[data-onboarding-panel]").forEach((node) => {
      node.hidden = readsPending || node.dataset.onboardingPanel !== String(currentStep);
    });
    document.querySelectorAll("[data-onboarding-step]").forEach((button) => {
      const step = Number(button.dataset.onboardingStep);
      if (step === currentStep) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
      /* A3：走过（早于当前步）的目录项补 ✓ 完成态；回到更早步时后面各步自然摘除 */
      button.classList.toggle("is-done", step < currentStep);
    });
    const title = STEP_TITLES[currentStep] || "";
    $("onboarding-progress").textContent = `第 ${currentStep} 步，共 ${TOTAL_STEPS} 步`;
    $("onboarding-mobile-progress").textContent = `第 ${currentStep} 步，共 ${TOTAL_STEPS} 步 · ${title}`;
    $("onboarding-prev").hidden = currentStep === 1;
    const nextButton = $("onboarding-next");
    nextButton.textContent = currentStep === TOTAL_STEPS ? "完成引导" : "下一步";
    /* A1：主路径恒为主钮（下一步/完成引导同形态），跳过引导降 text-button 次级 */
    nextButton.classList.add("btn-primary");
    renderNotice();
    renderSummary();
    if (currentStep === 4) autoDetectProxyOnce();
    if (focus) {
      /* 步进焦点契约：焦点留在激活的导航/下一步控件上，不聚焦整个步骤面板的
         大标题（tabindex="-1" 容器级大环根因）；步骤变化经既有 onboarding-live
         live region 播报，屏幕阅读器仍能感知分区切换。 */
      announce(`第 ${currentStep} 步：${STEP_TITLES[currentStep] || ""}`);
    }
  }

  function renderAuth() {
    const auth = store.auth || shellSnapshot?.authentication || null;
    const row = $("onboarding-auth-state");
    const text = $("onboarding-auth-text");
    const impact = $("onboarding-auth-impact");
    const login = $("onboarding-open-login");
    const retry = $("onboarding-auth-retry");
    if (!auth) {
      row.dataset.state = "unknown";
      text.textContent = "暂时无法确认";
      impact.textContent = "暂时无法确认登录状态，可重试状态读取。";
      retry.hidden = false;
      login.hidden = false;
      login.disabled = false;
      login.textContent = "登录复旦课程平台";
      return;
    }
    const details = evidenceDetails(auth);
    row.dataset.state = auth.state;
    text.textContent = details.title;
    impact.textContent = details.impact;
    retry.hidden = !readsStale;
    if (auth.state === "ready") {
      login.hidden = true;
    } else {
      login.hidden = false;
      login.disabled = auth.state === "checking";
      login.textContent = recoveryActionLabel("login", auth);
    }
    if (auth.state && auth.state !== prevAuthState) {
      prevAuthState = auth.state;
      announce(`登录状态：${details.title}`);
    }
  }

  function renderCatalog() {
    const catalog = shellSnapshot?.catalog || null;
    const row = $("onboarding-catalog-state");
    const text = $("onboarding-catalog-text");
    const count = $("onboarding-catalog-count");
    const retry = $("onboarding-catalog-retry");
    if (!catalog) {
      row.dataset.state = "unknown";
      text.textContent = "暂时无法确认";
      count.textContent = "暂时无法确认课程目录状态，可重试状态读取。";
      retry.hidden = false;
      return;
    }
    /* N5FE-P2：目录码副句的「登录仍然有效」按会话真值分支——引导页常在
       登录前，失实宣称对学生是信任伤害。 */
    const details = evidenceDetails(catalog, {
      loggedIn: store.auth?.state === "ready" ? true : (store.auth?.state ? false : null),
    });
    row.dataset.state = catalog.state;
    text.textContent = details.title;
    retry.hidden = !readsStale;
    const total = Number(catalog.course_count);
    if (catalog.state === "ready") {
      count.textContent = Number.isFinite(total) && total > 0
        ? `当前账号共有 ${total} 门可访问课程。`
        : "当前账号暂未发现可访问课程。";
    } else {
      count.textContent = details.impact;
    }
    if (catalog.state && catalog.state !== prevCatalogState) {
      prevCatalogState = catalog.state;
      announce(`课程目录：${details.title}`);
    }
  }

  /* 步骤 4 只读三行：不发起任何远程动作/授权/写入，未配置项中性呈现。
     CLOUD-CONSENT-AUTO-1 U2：云端处理开关退役——第一行是固定透明说明
     （数据位置/可见范围/保留期/撤销路径，「连接就绪即可用」），不再读任何
     开关真值；真实连接状态由第二行 GitHub / Worker 证据行呈现。 */
  function renderOptional() {
    const remote = shellSnapshot?.remote?.overall || shellSnapshot?.remote || null;
    const remoteComponents = Array.isArray(shellSnapshot?.remote?.components)
      ? shellSnapshot.remote.components
      : [];
    const installation = remoteComponents.find(
      (item) => item?.component === "installation",
    ) || null;
    const remoteRow = $("onboarding-remote-row");
    if (remote) {
      if (
        installation?.code === "installation_scope_not_exact"
        || installation?.code === "installation_missing"
      ) {
        /* GitHub 步骤要求：安装 CourseLens App 时两个专属仓库已自动预选；
           范围漂移时给出精确两仓库的调整指引，绝不让用户手动搜索或创建仓库 */
        remoteRow.dataset.state = "unknown";
        $("onboarding-remote-text").textContent = installationScopeText(installation.evidence);
      } else {
        const details = evidenceDetails(remote);
        remoteRow.dataset.state = remote.state === "ready" ? "ready" : "unknown";
        $("onboarding-remote-text").textContent = remote.state === "ready"
          ? `GitHub / Worker 连接：${details.title}。`
          : `GitHub / Worker 连接：${details.title} · 可稍后设置。`;
      }
    } else {
      remoteRow.dataset.state = "unknown";
      $("onboarding-remote-text").textContent = "GitHub / Worker 连接：暂时无法确认。";
    }
    const deepseekConfigured = onboardingPayload?.ai?.configured;
    const deepseekRow = $("onboarding-deepseek-row");
    if (deepseekConfigured === true) {
      deepseekRow.dataset.state = "ready";
      $("onboarding-deepseek-text").textContent = "DeepSeek API Key：已配置。";
    } else if (deepseekConfigured === false) {
      deepseekRow.dataset.state = "unknown";
      $("onboarding-deepseek-text").textContent = "DeepSeek API Key：未配置 · 可稍后设置，字幕仍可用。";
    } else {
      deepseekRow.dataset.state = "unknown";
      $("onboarding-deepseek-text").textContent = "DeepSeek API Key：暂时无法确认。";
    }
  }

  /* ---- 步骤 4 代理卡：进入即检测一次；未检测到引导手填；保存走既有网络管道。
     检测是闭集只读动作（不触代际）；保存复用 update-network（P0.3 代际语义），
     成功后经既有 refreshEvidence 刷新全链路状态，不新增任何通道。 ---------- */

  function ensureProxyCard() {
    if (proxyNodes) return proxyNodes;
    const rows = $("onboarding-step-4")?.querySelector(".onboarding-optional-rows");
    if (!rows) return null;
    const card = document.createElement("div");
    card.className = "onboarding-proxy-card";
    const row = document.createElement("div");
    row.id = "onboarding-proxy-row";
    row.className = "onboarding-optional-row";
    row.dataset.state = "idle";
    const text = document.createElement("span");
    text.id = "onboarding-proxy-text";
    text.textContent = `${PROXY_CARD_TEXT.label}：未检测`;
    const redetect = document.createElement("button");
    redetect.id = "onboarding-proxy-redetect";
    redetect.type = "button";
    redetect.textContent = "重新检测";
    const direct = document.createElement("button");
    direct.id = "onboarding-proxy-direct";
    direct.type = "button";
    direct.textContent = "不用代理";
    direct.title = "不用代理、全部直连；适合校园网直连即可使用的同学";
    const guide = document.createElement("p");
    guide.id = "onboarding-proxy-guide";
    guide.className = "hint";
    guide.hidden = true;
    guide.textContent = PROXY_CARD_TEXT.guide;
    const manual = document.createElement("div");
    manual.id = "onboarding-proxy-manual";
    manual.className = "button-row";
    manual.hidden = true;
    const input = document.createElement("input");
    input.id = "onboarding-proxy-input";
    input.type = "url";
    input.placeholder = "http://127.0.0.1:7890";
    input.setAttribute("aria-label", "代理地址");
    const save = document.createElement("button");
    save.id = "onboarding-proxy-save";
    save.type = "button";
    save.textContent = "保存代理";
    const error = document.createElement("p");
    error.id = "onboarding-proxy-error";
    error.className = "field-error";
    error.hidden = true;
    const saved = document.createElement("p");
    saved.id = "onboarding-proxy-saved";
    saved.className = "hint";
    saved.hidden = true;
    row.append(text, redetect, direct);
    manual.append(input, save);
    card.append(row, guide, manual, error, saved);
    rows.append(card);
    redetect.addEventListener("click", handleProxyRedetect);
    direct.addEventListener("click", handleProxyDirect);
    save.addEventListener("click", handleProxySave);
    proxyNodes = { row, text, redetect, direct, guide, manual, input, save, error, saved };
    return proxyNodes;
  }

  function renderProxyCard() {
    const nodes = proxyNodes;
    if (!nodes) return;
    setBusy(nodes.redetect, proxyState.busy);
    setBusy(nodes.direct, proxyState.busy || proxyState.saving);
    nodes.saved.hidden = !proxyState.savedEcho;
    if (proxyState.savedEcho) nodes.saved.textContent = proxyState.savedEcho;
    if (proxyState.busy) {
      nodes.row.dataset.state = "checking";
      nodes.text.textContent = `${PROXY_CARD_TEXT.label}：${PROXY_CARD_TEXT.detecting}`;
      nodes.guide.hidden = true;
      nodes.manual.hidden = true;
      nodes.error.hidden = true;
      return;
    }
    const result = proxyState.result;
    if (!result) {
      nodes.row.dataset.state = "idle";
      nodes.text.textContent = `${PROXY_CARD_TEXT.label}：未检测`;
      nodes.guide.hidden = true;
      nodes.manual.hidden = true;
      return;
    }
    if (result.status === "found") {
      nodes.row.dataset.state = "ready";
      nodes.text.textContent = `${PROXY_CARD_TEXT.label}：${PROXY_CARD_TEXT.found(result.port, result.source)}`;
      nodes.guide.hidden = true;
      nodes.manual.hidden = false;
      nodes.input.value = `http://127.0.0.1:${result.port}`;
      return;
    }
    nodes.row.dataset.state = "unknown";
    nodes.text.textContent = `${PROXY_CARD_TEXT.label}：${
      result.status === "error" ? PROXY_CARD_TEXT.error
      : result.status === "pac_detected" ? PROXY_CARD_TEXT.pacDetected
      : PROXY_CARD_TEXT.notFound
    }`;
    nodes.guide.hidden = false;
    nodes.manual.hidden = false;
    if (!nodes.input.value && onboardingPayload?.network?.proxy_url) {
      nodes.input.value = String(onboardingPayload.network.proxy_url);
    }
  }

  async function runProxyDetect() {
    if (proxyState.busy) return;
    proxyState.busy = true;
    proxyState.token += 1;
    const token = proxyState.token;
    proxyState.savedEcho = "";
    renderProxyCard();
    let result = null;
    try {
      result = await postV3("settings/actions", { action: "detect-proxy" });
    } catch {
      /* 读取失败按 error 闭集呈现，绝不冒充 not_found */
      result = { status: "error" };
    }
    if (!visible || token !== proxyState.token) return;
    proxyState.busy = false;
    const status = result?.status;
    proxyState.result = status === "found" && Number.isFinite(Number(result?.port))
      ? { status: "found", source: result.source === "system" ? "system" : "scan", port: Number(result.port) }
      : status === "error"
        ? { status: "error", source: null, port: null }
        : status === "pac_detected"
          ? { status: "pac_detected", source: "system_pac", port: null }
          : { status: "not_found", source: null, port: null };
    renderProxyCard();
    announce(
      proxyState.result.status === "found"
        ? `本机代理检测完成：检测到 127.0.0.1:${proxyState.result.port} 有响应，未必是可用代理`
        : proxyState.result.status === "pac_detected"
          ? "本机代理检测完成：系统是 PAC 自动代理模式，可手动填写端口"
          : "本机代理检测完成：未检测到可用代理，可手动填写",
    );
  }

  const autoDetectProxyOnce = () => {
    /* 每次打开引导至多自动检测一次；重进步骤与「重新检测」走显式按钮 */
    if (!visible || proxyState.busy || proxyState.result) return;
    void runProxyDetect();
  };

  const handleProxyRedetect = () => {
    if (!visible) return;
    void runProxyDetect();
  };

  /* N5FE-P4：不用代理是一等动作——直连学生一键关闭代理，不必理解模式语义 */
  const handleProxyDirect = async () => {
    if (!visible || proxyState.busy || proxyState.saving) return;
    const nodes = proxyNodes;
    proxyState.saving = true;
    nodes.error.hidden = true;
    try {
      await postV3("settings/actions", { action: "update-network", mode: "direct", proxy_url: "" });
      if (!visible) return;
      proxyState.savedEcho = "已选择不用代理：全部直连。";
      renderProxyCard();
      announce("已选择不用代理，全部直连");
      void refreshEvidence();
    } catch {
      if (!visible) return;
      proxyState.savedEcho = "";
      renderProxyCard();
      nodes.error.textContent = PROXY_CARD_TEXT.saveFailed;
      nodes.error.hidden = false;
    } finally {
      proxyState.saving = false;
      setBusy(nodes.direct, false);
    }
  };

  async function handleProxySave() {
    if (!visible || proxyState.saving) return;
    const nodes = proxyNodes;
    if (!nodes) return;
    const raw = String(nodes.input.value || "").trim();
    const schemeEnd = raw.indexOf("://");
    if (schemeEnd >= 0 && !/^https?:\/\//i.test(raw.slice(0, schemeEnd + 3))) {
      /* SOCKS 等非 HTTP(S) 输入：闭集文案本地拦截，不发请求 */
      nodes.error.textContent = PROXY_CARD_TEXT.invalidProxy;
      nodes.error.hidden = false;
      return;
    }
    proxyState.saving = true;
    nodes.error.hidden = true;
    setBusy(nodes.save, true);
    try {
      const value = await postV3("settings/actions", {
        action: "update-network",
        mode: String(onboardingPayload?.network?.mode || "auto"),
        proxy_url: raw,
      });
      if (!visible) return;
      const proxy = String(value?.proxy_url || "");
      proxyState.savedEcho = proxy ? PROXY_CARD_TEXT.saved(proxy) : "";
      renderProxyCard();
      announce(proxyState.savedEcho || "代理设置已保存");
      void refreshEvidence();
    } catch {
      if (!visible) return;
      /* 失败即撤掉旧成功复述，避免两行矛盾状态并存 */
      proxyState.savedEcho = "";
      renderProxyCard();
      nodes.error.textContent = PROXY_CARD_TEXT.saveFailed;
      nodes.error.hidden = false;
    } finally {
      proxyState.saving = false;
      setBusy(nodes.save, false);
      /* saving 期间的 renderProxyCard 会把 direct 钮一并置忙；流程收尾时
         复位，否则「不用代理」残留在 disabled 态（N5FE-P4 实测踩中） */
      setBusy(nodes.direct, false);
    }
  }

  function renderSummary() {
    const target = $("onboarding-summary");
    if (!target) return;
    clear(target);
    const { authReady, catalogReady } = coreReadiness();
    const remote = shellSnapshot?.remote?.overall || shellSnapshot?.remote || null;
    if (authReady && catalogReady) {
      target.append(textElement("p", "基础学习已可用：复旦会话和课程目录已就绪。", "onboarding-body"));
    } else if (!authReady) {
      target.append(textElement("p", "还需连接复旦账号：登录后即可选择课程。", "onboarding-body"));
    } else {
      target.append(textElement("p", "课程目录仍需处理：可稍后在课程页刷新确认。", "onboarding-body"));
    }
    if (remote?.state === "ready") {
      target.append(textElement("p", "字幕与笔记生成可用：远程处理连接就绪。", "onboarding-body"));
    } else {
      target.append(textElement("p", "字幕与笔记生成尚需准备远程连接（可稍后设置）。", "onboarding-body"));
    }
    target.append(textElement(
      "p",
      onboardingPayload?.ai?.configured === true
        ? "DeepSeek 已配置。"
        : "字幕将使用无大模型验证模式。",
      "onboarding-body",
    ));
    if (readsStale) {
      target.append(textElement("p", "以上为上次确认的状态；本次读取暂时失败，可稍后重试。", "hint"));
    }
  }

  function renderAll() {
    renderNotice();
    renderAuth();
    renderCatalog();
    renderOptional();
    renderSummary();
  }

  function resetTransientPanels() {
    $("onboarding-action-error").hidden = true;
    $("onboarding-action-error").textContent = "";
    $("onboarding-complete-recovery").hidden = true;
    $("onboarding-dismiss-recovery").hidden = true;
    setBusy($("onboarding-skip"), false);
    setBusy($("onboarding-next"), false);
    setBusy($("onboarding-complete-retry"), false);
    setBusy($("onboarding-dismiss-retry"), false);
    actionBusy = false;
  }

  async function refreshEvidence() {
    if (!visible) return;
    seq += 1;
    const requestSeq = seq;
    controller?.abort();
    controller = new AbortController();
    const current = controller;
    try {
      const [shell, onboarding] = await Promise.all([
        apiV3("app-shell", { controller: current }),
        apiV3("onboarding", { controller: current }),
      ]);
      if (!visible || current !== controller || requestSeq !== seq) return;
      shellSnapshot = shell;
      onboardingPayload = onboarding;
      guideSnapshot = onboarding?.guide || guideSnapshot;
      readsStale = false;
      readsPending = false;
      if (!defaultStepApplied) {
        defaultStepApplied = true;
        renderStep(defaultStepFor(guideSnapshot));
      }
      renderAll();
    } catch (error) {
      if (error?.name === "AbortError" || !visible || current !== controller || requestSeq !== seq) return;
      readsStale = true;
      readsPending = false;
      if (!defaultStepApplied) {
        defaultStepApplied = true;
        renderStep(defaultStepFor(guideSnapshot));
      }
      renderAll();
    }
  }

  function closeGuide() {
    visible = false;
    settingsJumpPending = false;
    defaultStepApplied = false;
    seq += 1;
    controller?.abort();
    controller = null;
    resetTransientPanels();
    $("settings-return-guide").hidden = true;
    renderNotice();
    syncHomeResumePrompt(); /* F1：关页后按最新 guide 状态刷新面板提示（完成/跳过=消失，仅退出=保留；宿主见 sync 注） */
  }

  function exitToOrigin({ enterSelect = false } = {}) {
    const target = originPage;
    closeGuide();
    selectPage(target);
    if (target === "study" && enterSelect) {
      window.dispatchEvent(new Event("courselens:study-return-select"));
    }
  }

  function openGuide({ origin = "study" } = {}) {
    if (visible) return;
    visible = true;
    originPage = origin;
    defaultStepApplied = false;
    readsPending = true;
    readsStale = false;
    syncHomeResumePrompt(); /* F1：引导开着时提示行退场（同一恢复链不再双入口并显） */
    resetTransientPanels();
    /* 代理卡会话重置：每次打开引导重新自动检测一次（token 作废在途检测） */
    proxyState.busy = false;
    proxyState.saving = false;
    proxyState.result = null;
    proxyState.savedEcho = "";
    proxyState.token += 1;
    const proxy = ensureProxyCard();
    if (proxy) {
      proxy.input.value = "";
      proxy.error.hidden = true;
      renderProxyCard();
    }
    $("settings-return-guide").hidden = true;
    renderStep(1, { focus: false });
    renderNotice();
    selectPage("onboarding");
    void refreshEvidence();
  }

  async function guideAction(action) {
    try {
      const value = await postV3("onboarding/actions", { action, version: GUIDE_VERSION });
      guideSnapshot = value?.guide || guideSnapshot;
      return true;
    } catch {
      return false;
    }
  }

  async function handleSkip() {
    if (actionBusy || !visible) return;
    actionBusy = true;
    setBusy($("onboarding-skip"), true);
    const saved = await guideAction("dismiss");
    actionBusy = false;
    setBusy($("onboarding-skip"), false);
    if (saved) {
      exitToOrigin();
      return;
    }
    /* 写入失败：留在本页并行内说明（不只 toast） */
    $("onboarding-action-error").hidden = true;
    $("onboarding-dismiss-recovery").hidden = false;
  }

  async function handleComplete() {
    if (actionBusy || !visible) return;
    actionBusy = true;
    setBusy($("onboarding-next"), true);
    const saved = await guideAction("complete");
    actionBusy = false;
    setBusy($("onboarding-next"), false);
    if (!saved) {
      $("onboarding-dismiss-recovery").hidden = true;
      $("onboarding-action-error").textContent = "完成状态未能保存，请重试。";
      $("onboarding-action-error").hidden = false;
      $("onboarding-complete-recovery").hidden = false;
      return;
    }
    resetTransientPanels();
    exitToOrigin({ enterSelect: coreReadiness().catalogReady });
  }

  const handleNext = async () => {
    if (actionBusy || !visible) return;
    if (currentStep < TOTAL_STEPS) {
      renderStep(currentStep + 1);
      return;
    }
    await handleComplete();
  };
  const handlePrev = () => {
    if (actionBusy || !visible || currentStep <= 1) return;
    renderStep(currentStep - 1);
  };
  const handleTocClick = (event) => {
    const button = event.target.closest?.("[data-onboarding-step]");
    if (!button || !visible) return;
    defaultStepApplied = true;
    renderStep(Number(button.dataset.onboardingStep));
  };
  const handleLoginOpen = () => {
    if (!visible) return;
    /* 防重复：既有登录 dialog 已打开时忽略再次点击 */
    if ($("login-dialog")?.open) return;
    window.dispatchEvent(new CustomEvent("courselens:open-login", {
      detail: store.auth?.state === "degraded" ? "reauth" : "login",
    }));
  };
  const handleAuthRetry = () => {
    if (visible) void refreshEvidence();
  };
  const handleCatalogRetry = () => {
    if (visible) void refreshEvidence();
  };
  const handleCatalogRefresh = () => {
    if (!visible) return;
    $("refresh-catalog")?.click(); /* 复用既有 refresh-catalog 动作，不新建刷新通道 */
  };
  const handleCatalogRefreshEvent = () => {
    if (visible) void refreshEvidence();
  };
  const handleCatalogSettled = () => {
    if (visible) void refreshEvidence();
  };
  const handleCatalogTimeout = () => {
    if (!visible || shellSnapshot?.catalog?.state !== "checking") return;
    shellSnapshot = {
      ...shellSnapshot,
      catalog: { ...shellSnapshot.catalog, state: "degraded", code: "catalog_timeout", actions: ["refresh-catalog"] },
    };
    renderAll();
  };
  const handleSettingsJump = (event) => {
    const button = event.target.closest?.("[data-onboarding-settings-jump]");
    if (!button || !visible) return;
    settingsJumpPending = true; /* 消费点在 courselens:page(detail==="settings")：仅引导发起的访问显示返回按钮 */
    selectPage("settings");
    document.querySelector(`[data-settings-target="${button.dataset.onboardingSettingsJump}"]`)?.click();
  };
  const handleReturnToGuide = () => {
    $("settings-return-guide").hidden = true; /* 返回即结束本次引导发起的设置访问 */
    if (visible) selectPage("onboarding");
  };
  /* REALRUN-1 P2-3（2026-10-08 真测 2/2 复现）：引导挂起态（引导发起的设置
     跳转保持 visible=true）下，账户菜单/帮助入口的「新手引导」此前落进
     openGuide 的 visible 早退——静默无动作，学生只能靠设置页「返回引导」钮
     找回。挂起态的正确语义=回到引导页继续（与「返回引导」同义），绝不静默；
     非挂起态维持既有 openGuide（按 origin 开新一次）。 */
  const resumeOrOpenGuide = (origin) => {
    if (visible) {
      selectPage("onboarding");
      void refreshEvidence();
      return;
    }
    openGuide({ origin });
  };
  const handleHelpGuide = () => resumeOrOpenGuide("settings");
  /* F1（化身走查 20261008）→ LANDING-AESTHETIC-1（用户裁决 2026-10-09）：引导
     挂起态（disposition=new，未完成也未跳过）保留可见「继续新手引导」提示行，
     但退出着陆页（诗页零打扰；恢复路径保留账户菜单/设置帮助既有入口=不灭）。
     唯一可见宿主=选课面板概览顶部：挂起且选课面板可见时显示；问候空态/学习桌
     一律退场。auto-open 恰一次的设计取舍不变；刷新/重开后不再静默（面板内有
     提示）。完成/跳过写入后即消失，「仅本次退出」不写状态则提示保留（学生尚未
     决定，被动提示不骚扰也不消失）。节点缺失（精简测试壳）时静默跳过。 */
  const syncHomeResumePrompt = () => {
    const node = $("home-guide-resume");
    if (!node) return;
    const pending = guideSnapshot?.persistence === "ready"
      && guideSnapshot?.disposition === "new"
      && !visible;
    const select = $("study-select");
    const overview = select && typeof select.querySelector === "function"
      ? select.querySelector(".home-overview")
      : null;
    const show = Boolean(pending && overview && select && !select.hidden);
    node.hidden = !show;
    if (!show) return;
    if (node.parentElement !== overview) {
      /* 概览顶部原位（既有视觉位）；精简桩无 prepend 时退化为尾插（可见性不变） */
      if (typeof overview.prepend === "function") overview.prepend(node);
      else overview.append(node);
    }
  };
  const handleHomeGuideResume = () => {
    const origin = document.querySelector(".page.active")?.dataset?.page || "study";
    resumeOrOpenGuide(origin);
  };
  /* 学习空态的新手引导按钮已随视觉批 4 移出（D1-design v2 D12）：入口保留在
     账户菜单与设置帮助，可达性不回退。 */
  const handleDismissLeave = () => exitToOrigin(); /* 仅本次退出：不写任何引导状态 */
  const handleAccountMenuGuide = () => {
    const origin = document.querySelector(".page.active")?.dataset?.page || "study";
    closeOverlay($("account-menu"));
    resumeOrOpenGuide(origin);
  };
  const handlePageEvent = (event) => {
    if (!visible) return;
    const target = String(event.detail || "");
    if (target === "onboarding") return;
    if (target === "settings") {
      /* 临时返回按钮只属于“由引导发起”的这一次设置访问；直接进入设置必须隐藏 */
      const initiatedByGuide = settingsJumpPending;
      settingsJumpPending = false;
      $("settings-return-guide").hidden = !initiatedByGuide;
      if (!initiatedByGuide) closeGuide(); /* 非引导路径进入设置视为放弃引导 */
      return;
    }
    closeGuide();
  };
  const handleFocus = () => {
    if (visible) void refreshEvidence(); /* 有界：仅在引导可见且窗口重新聚焦时读一次 */
  };
  /* GH-UX-REWORK-1（MF-8）：GitHub 连接翻转经应用级 SSE 广播即时上屏——
     引导可见时才取数（零多余外联），装完 App 回引导不再看到陈旧连接行 */
  const handleRemoteConnectionChanged = () => {
    if (visible) void refreshEvidence();
  };

  /* 首次安装自动打开（异步、不阻塞装配）。F-GATE-1（发布门 2026-10-10）根修：
     旧序「先写 mark-opened 再切页，失败不抢占」把首印象押在一次 POST 的传输
     成败上——该 POST 是启动突发里唯一无传输重试的写请求（api.js canRetry 仅
     GET），突发拒载/响应丢失时本轮引导静默不弹（仅 4.2s toast 即逝），而服务
     端可能已写成功，学生永久错过恰一次自动打开。改为开在记前：打开决策只依
     赖带传输重试的 GET，读到候选态的视觉面必弹；mark-opened 随后异步补记，
     失败不没收引导（下一载 auto_opened 仍 false 会再开=自愈；完成/跳过写走
     既有恢复面）。后台第二载（刷新/重开/双窗）仍按恰一次消费不回弹：F1/
     LANDING-AESTHETIC-1 的「不骚扰」取舍原样保留。后端 mark-opened 幂等且
     保留 completed/dismissed 处置（application.py keep_disposition），补记
     与用户动作乱序亦不腐化状态。 */
  async function maybeAutoOpen() {
    if (autoOpenAttempted) return;
    autoOpenAttempted = true;
    let guide = null;
    try {
      guide = (await apiV3("onboarding"))?.guide || null;
    } catch {
      return; /* 读取失败不自动打开；手动入口保持可用 */
    }
    /* F1：读取成功即按 guide 真态刷新主页提示（先于 auto-open 裁决——挂起态
       且不满足自动打开条件时，提示行就是学生的唯一可见恢复面）。 */
    guideSnapshot = guide || guideSnapshot;
    syncHomeResumePrompt();
    if (
      !guide
      || guide.version !== GUIDE_VERSION
      || guide.disposition !== "new"
      || guide.auto_opened !== false
      || guide.persistence !== "ready"
    ) return;
    openGuide({ origin: "study" });
    /* 补记不 await：首绘不等写；失败诚实一句话，不再没收引导 */
    postV3("onboarding/actions", { action: "mark-opened", version: GUIDE_VERSION }).catch(() => {
      toast("新手引导状态暂时没保存上；不影响当前使用，下次启动可能会再显示一次引导。", "error");
    });
  }

  $("onboarding-skip").addEventListener("click", handleSkip);
  $("onboarding-prev").addEventListener("click", handlePrev);
  $("onboarding-next").addEventListener("click", handleNext);
  $("onboarding-toc").addEventListener("click", handleTocClick);
  $("onboarding-open-login").addEventListener("click", handleLoginOpen);
  $("onboarding-auth-retry").addEventListener("click", handleAuthRetry);
  $("onboarding-catalog-refresh").addEventListener("click", handleCatalogRefresh);
  $("onboarding-catalog-retry").addEventListener("click", handleCatalogRetry);
  $("onboarding-complete-retry").addEventListener("click", handleComplete);
  $("onboarding-dismiss-retry").addEventListener("click", handleSkip);
  $("onboarding-dismiss-leave").addEventListener("click", handleDismissLeave);
  document.querySelector(".onboarding-content")?.addEventListener("click", handleSettingsJump);
  $("settings-return-guide").addEventListener("click", handleReturnToGuide);
  $("help-open-guide")?.addEventListener("click", handleHelpGuide);
  $("account-menu-onboarding")?.addEventListener("click", handleAccountMenuGuide);
  $("home-guide-resume-action")?.addEventListener("click", handleHomeGuideResume); /* F1：主页挂起提示行 */
  window.addEventListener("courselens:page", handlePageEvent);
  window.addEventListener("courselens:study-mode", syncHomeResumePrompt); /* F1/D-20261009-09：学习面过场重挂宿主 */
  window.addEventListener("focus", handleFocus);
  window.addEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
  window.addEventListener("courselens:catalog-refresh", handleCatalogRefreshEvent);
  window.addEventListener("courselens:catalog-settled", handleCatalogSettled);
  window.addEventListener("courselens:catalog-timeout", handleCatalogTimeout);
  const unsubscribeAuth = store.subscribe("auth", () => {
    if (!visible) return;
    renderAuth();
    if (currentStep === TOTAL_STEPS) renderSummary();
  });
  const unsubscribeCourses = store.subscribe("courses", () => {
    if (!visible) return;
    renderCatalog();
  });
  void maybeAutoOpen();

  return () => {
    seq += 1;
    controller?.abort();
    controller = null;
    if (proxyNodes) {
      proxyNodes.redetect.removeEventListener("click", handleProxyRedetect);
      proxyNodes.direct.removeEventListener("click", handleProxyDirect);
      proxyNodes.save.removeEventListener("click", handleProxySave);
    }
    $("onboarding-skip").removeEventListener("click", handleSkip);
    $("onboarding-prev").removeEventListener("click", handlePrev);
    $("onboarding-next").removeEventListener("click", handleNext);
    $("onboarding-toc").removeEventListener("click", handleTocClick);
    $("onboarding-open-login").removeEventListener("click", handleLoginOpen);
    $("onboarding-auth-retry").removeEventListener("click", handleAuthRetry);
    $("onboarding-catalog-refresh").removeEventListener("click", handleCatalogRefresh);
    $("onboarding-catalog-retry").removeEventListener("click", handleCatalogRetry);
    $("onboarding-complete-retry").removeEventListener("click", handleComplete);
    $("onboarding-dismiss-retry").removeEventListener("click", handleSkip);
    $("onboarding-dismiss-leave").removeEventListener("click", handleDismissLeave);
    document.querySelector(".onboarding-content")?.removeEventListener("click", handleSettingsJump);
    $("settings-return-guide").removeEventListener("click", handleReturnToGuide);
    $("help-open-guide")?.removeEventListener("click", handleHelpGuide);
    $("account-menu-onboarding")?.removeEventListener("click", handleAccountMenuGuide);
    window.removeEventListener("courselens:page", handlePageEvent);
    window.removeEventListener("focus", handleFocus);
    window.removeEventListener("courselens:remote-connection-changed", handleRemoteConnectionChanged);
    window.removeEventListener("courselens:catalog-refresh", handleCatalogRefreshEvent);
    window.removeEventListener("courselens:catalog-settled", handleCatalogSettled);
    window.removeEventListener("courselens:catalog-timeout", handleCatalogTimeout);
    unsubscribeAuth();
    unsubscribeCourses();
  };
}
