/* 学生面错误文案表（R3-09 指路面：本表覆盖=复旦登录/目录族+传输族+深度回答族
   +通用动作兜底；任务中心任务死因族见 tasks-drawer.js 的 TASK_FAILURE_GUIDANCE；
   复习面审查态文案见 course-review.js。cloud_setup_required 与
   TASK_FAILURE_GUIDANCE 逐字一致——同一码两处同话，防措辞漂移）。 */
const ERROR_MESSAGES = Object.freeze({
  fudan_login_required: "请先登录复旦课程平台",
  fudan_session_expired: "复旦课程平台会话已过期，请重新登录",
  /* 恢复期本地可信上下文的瞬态码（AUTOLOGIN-LOCAL-FIRST-1③）：非错误形态，
     study 页以 checking 形态呈现瞬态提示 */
  fudan_session_restoring: "正在登录，请稍候…",
  timeout: "连接超时，后端尚未确认结果",
  network_unavailable: "网络暂不可用",
  catalog_timeout: "课程目录连接超时，请稍后重试",
  catalog_route_unavailable: "登录已成功，但课程目录网络暂不可用",
  catalog_session_expired: "课程目录会话已过期，请重新登录",
  catalog_additional_verification_required: "课程目录需要额外身份验证，请重新登录；无需关闭两步验证",
  catalog_bearer_missing: "课程授权信息暂无法读取，请稍后重试刷新；若持续出现请更新客户端",
  catalog_identity_invalid: "课程目录身份无法确认，请重新登录",
  catalog_identity_mismatch: "课程目录身份与当前账号不一致，请重新登录",
  catalog_payload_invalid: "课程目录返回了无法确认的数据，请重试",
  catalog_detail_partial: "部分课程授权暂时无法确认，请重试",
  catalog_target_invalid: "课程目录跳转被安全策略拒绝",
  worker_manifest_invalid: "Worker 镜像签名无效",
  worker_signing_key_revoked: "Worker 签名密钥已撤销",
  worker_public_tree_drift: "公开 Worker 版本发生漂移",
  worker_protocol_incompatible: "Worker 协议版本不兼容",
  worker_release_unavailable: "批准的 Worker 镜像不可用",
  task_not_found: "任务不存在",
  /* R3-09 收敛：通用任务死因码与 tasks-drawer TASK_FAILURE_GUIDANCE 逐字同话 */
  task_failed: "任务失败，原因未确认。请稍后重试。",
  /* C3（PB-1）：学习产物缺失码已随「artifacts 空载荷 200」合同退役——
     后端零产出，码表同笔删除（空态文案由 study.js 直接呈现）。 */
  /* 审计 A5-P3-2：会话探针 400 学生可达，不再落英文后端文本 */
  frontend_session_invalid: "前端会话状态已失效。请刷新页面或重启客户端后重试",
  /* NIGHT2-W11 码表对账：后端可达但前端缺映射的失败码，逐码补学生文案 */
  /* F2（化身走查 20261008）：码的语义=本讲缺字幕/讲义依据（书签收藏族动作
     也会踩到），文案去掉「再重新提问」的提问链尾巴与责备感——学生动作未必
     是提问，中性指路即可；提问族面（解释/没听懂）有各自的就地文案。 */
  bookmark_evidence_unavailable: "这一讲暂时没有可作依据的字幕或讲义；先生成本讲字幕，就可以继续了。",
  assessment_event_missing: "这条考核提醒已经不存在了（可能被删除）。刷新一下就好。",
  bookmark_not_found: "这条书签已经不在了。刷新学习页即可看到最新列表。",
  concept_edge_not_found: "这个概念关联已经失效了。刷新后重新查看即可。",
  courseware_pdf_resume_unavailable: "这份课件 PDF 的上次合成没有可续的进度，请重新生成一次。",
  document_not_found: "这份资料已经不在本机了。可能已被删除，刷新资料列表看看。",
  /* FUZZ-INPUT-1 F4/F3：导入拒绝族具名化——修前这组闭集码全落「请刷新页面
     重试」通用兜底（刷新对确定性拒绝无意义，学生不知道文件本身有问题）。
     码表=documents 路由 allowed_codes 闭集，按因给策。 */
  document_empty: "这个文件是空的，换一个有内容的文件再导入一次。",
  document_payload_invalid: "这个文件的内容读不出来，可能上传时出了问题；换一个文件试试。",
  document_text_encoding_invalid: "这个文件的文字内容读不出来（可能损坏或编码不对）；换一个文件试试。",
  document_format_invalid: "这个文件的内容和扩展名对不上，文件可能损坏了；换一个试试。",
  document_type_unsupported: "只收 PDF、PPTX、TXT、MD 四类文件；其他格式先转个格式再导入。",
  document_too_large: "这个文件太大，超出导入上限；压缩或拆分后再试。",
  document_password_required: "这个 PDF 设了密码，先解除密码再导入。",
  document_has_no_pages: "这个文件里没有可读的页面；换一个试试。",
  grounded_answer_unavailable: "暂时没法基于本讲内容回答。等字幕和资料就绪后再试一次。",
  lecture_not_found: "这门讲次不在当前课程目录里了。刷新目录即可同步。",
  materials_entry_missing: "这条资料已经不存在了（可能刚被删除）。刷新列表即可。",
  onboarding_version_conflict: "引导状态在另一个窗口被更新了。刷新页面拿最新进度。",
  operation_id_conflict: "这个操作刚刚已经提交过了，不用重复点。刷新确认结果即可。",
  question_explanation_not_configured: "还没有配置 DeepSeek Key，暂时无法生成解释。可在设置里添加。",
  quiz_generation_failed: "测验生成没有成功。稍等片刻再点一次「生成测验」。",
  request_body_too_large: "这次提交的内容太大了。请拆小一点再试。",
  reset_blocked: "还有进行中的任务没结束，重置暂时不能执行。稍后再试一次。",
  /* AS2（CLOSED-RESULT-IMPORT-1）U2：显式「导入远端结果」的两种诚实拒绝 */
  remote_import_unavailable: "云端没有找到可以导入的结果，可以直接重新发起一次任务",
  remote_import_expired: "云端结果已超过可导入期限（生成后 7 天内可导入），请重新发起任务",
  runtime_failed: "本地处理出现异常，请重试一次；若持续失败请重启客户端，你的数据不受影响。",
  courseware_plan_mismatch: "云端学习计划与本机课程对不上。请刷新课程目录后重试。",
  cloud_secret_empty: "加密托管条目还没就绪。请重新开启一次云自动化后再试。",
  identity_unavailable: "暂时无法确认 GitHub 身份。请稍后在「设置 → 账户与连接」重试。",
  personal_worker_migration_required: "专属仓库需要迁移到新结构。请按「设置 → 账户与连接」的指引完成迁移。",
  cloud_verification_evidence_missing: "云端验证证据暂缺。请重新完成一次验证后再试。",
  mailbox_repository_missing: "专属资料仓暂时不可用。请在「设置 → 账户与连接」重新连接后再试。",
  mailbox_repository_unavailable: "专属资料仓暂时不可用。请在「设置 → 账户与连接」重新连接后再试。",
  authorization_not_fresh: "GitHub 授权信息需要刷新。请重新完成一次连接确认。",
  repos_access_denied: "GitHub App 还没有仓库访问权限。请按指引完成仓库授权。",
  installation_scope_unavailable: "仓库授权范围暂时无法确认。请稍后在「设置 → 账户与连接」重试。",
  installation_selection_unknown: "仓库授权范围暂时无法确认。请稍后在「设置 → 账户与连接」重试。",
  /* 直播二期（Prompt 42 乙1c）：平台原生文稿端点闭集码，文案与侧板提示同源 */
  transcript_lecture_unknown: "这个讲次没有可用的文稿。",
  transcript_unavailable: "文稿暂时取不到，稍后再试一次。",
  /* O4（夜8 等集扫）：后端会发出的失败码补齐学生文案（健康态探针串刻意不映射） */
  cloud_artifact_invalid: "云端返回的学习材料没有通过完整性校验。请重新运行一次自动整理。",
  cloud_artifact_too_large: "这次云端返回的材料超出本机可接收的大小，已安全拒收。请重新运行一次。",
  worker_repository_missing: "专属 Worker 仓库不存在或还未初始化。请按「设置 → 网络与远程连接」的指引完成修复。",
  job_token_lease_active: "上一个云端任务还在收尾，稍等片刻再试一次。",
  remote_supervisor_busy: "这个任务已经有一个处理在进行中了，不用重复操作。",
  /* D8：手动代理地址没写对（缺 http(s):// 前缀或主机为空），保存被后端拒绝 */
  proxy_url_invalid: "代理地址要写成 http://主机:端口 的样子，改好后重新保存",
  /* D12 数据主权 P0：搬家包面闭集码（文案与后端 instruction 语义同源）。
     MIGRATION_E_* 为引擎稳定码（大小写敏感直查本表）；HTTP 面附加两个
     小写码（上传/下载路由自身的诚实拒绝）。 */
  MIGRATION_E_PASSWORD_INVALID: "密码不对，或者包被改动过；确认密码后重试。",
  MIGRATION_E_PACKAGE_INVALID: "这个搬家包读不了或已损坏；重新导出或重新选择文件试试。",
  MIGRATION_E_PACKAGE_TOO_LARGE: "这个包超过了 8GB 的导入上限。",
  MIGRATION_E_SCHEMA_TOO_NEW: "这个搬家包来自更新版本的 CourseLens；先把这台电脑的客户端升级到最新再导入。",
  MIGRATION_E_WRITE_FAILED: "写不进数据目录（可能有文件被占用）；稍后重试一次。",
  MIGRATION_E_BUSY: "上一个导出/导入还没结束；稍等一下再试。",
  migration_action_invalid: "这个请求没通过校验；刷新页面后重试。",
  migration_download_unavailable: "下载链接已失效或已被使用；重新导出一次即可。",
  /* STUDY-STATS-M3：学习统计导出文件不存在/已过 24h 清剪（错误码对账审计行） */
  study_stats_export_unavailable: "这份导出文件已经不在了（导出文件保留 24 小时）；重新导出一次即可。",
  /* D3：派发被远程门拦下时的学生面文案（此前行内/toast 拼英文 payload.error） */
  remote_failed: "远程连接还没就绪，任务没有发出去；连接好后再试一次",
  /* P3 深度回答链（P3-CONTRACT-1 PKG-C）：palette 深度入口可达的闭集码。
     deep_answer_evidence_unavailable 同时是对账审计钉的补文案行。 */
  deep_answer_evidence_unavailable: "当前课程资料里还找不到能回答这个问题的依据。换个说法再问，或先在完整检索里确认相关内容已就绪。",
  question_payload_oversized: "这个问题连同资料超出了单次问答的容量。把问题缩短一点再试。",
  task_task_unknown: "这条任务记录已经不在了。刷新任务中心看看最新状态。",
  /* 深度任务终态失败码（_question_error_code 闭集族）——深度回答失败态的人话文案 */
  remote_answer_timeout: "云端计算超时了。",
  remote_answer_failed: "云端计算没有成功。",
  deepseek_answer_failed: "AI 生成答案没有成功，请稍后再试。",
  deepseek_rate_limited: "请求太频繁被限流了，等几分钟再试。",
  remote_authorization_required: "GitHub 连接还没就绪，云端任务无法继续。请到「设置」重新连接后再试。",
  cloud_setup_required: "云端处理还没完成 GitHub 授权连接。请到「设置 → 网络与远程连接」完成连接步骤，再重试这个任务。",
});

/* 界面操作提示文案闭集表（COPY-SWEEP 纪律：非错误类学生面提示同样进表再
   引用，禁散落模块模板串；与 ERROR_MESSAGES 语义分层——提示不是错误）。 */
export const UI_HINTS = Object.freeze({
  course_order_shortcut: "Alt+↑/↓ 调整课程顺序",
  /* A11Y-IMPL-5（D14 P2-2 残余）：draggable 课程行的读屏行述——与排序提示同
     门控（行 >1 才挂），文案进闭集表再引用，禁散落模块模板串。 */
  course_row_reorderable: "课程项，可调整顺序",
  /* COPYUP-1：GitHub 连接失败态的代理指引（shell.js CONN_TEXT.github.error
     同源引用）——云端任务信封经 GitHub 流转，直连不稳时开代理即可重试。 */
  github_conn_error: "GitHub 连接失败：云端任务需要能访问 GitHub，可检查代理或稍后重试",
});

/* 深度回答等任务面按码取人话文案：未知码回退调用方给定的兜底句 */
export function friendlyError(code, fallback = "") {
  return ERROR_MESSAGES[String(code || "")] || String(fallback || "");
}

/* C3-3（兜底层中文化）：未知 error_code 不再直显英文 payload.error——学生面
   统一人话；原文连码保留在 ApiError.detail，供技术详情折叠按需复用。 */
const GENERIC_ACTION_FAILURE = "这次操作没能完成，请刷新页面重试";

export class ApiError extends Error {
  constructor(message, { code = "http_error", status = 0, actions = [], retriable = false, detail = "", receipt = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.actions = actions;
    this.retriable = retriable;
    this.detail = detail;
    /* D-20261009-01：拒绝回执（course-data 家族 409 信封）——body 本就是
       完整合法回执（status=rejected+blockers），透传给页面层渲染具体阻塞
       指引；非信封错误恒 null。 */
    this.receipt = receipt;
  }
}

/* NIGHT2-W20 GET-only 传输层重试（弱网读自愈，R17 收窄版）：
   仅 GET、至多 2 次重试、仅针对裸网关瞬态响应（429/502/503/504 且响应体
   不携带应用 error_code 信封——应用语义的 5xx 是终态，由调用方/轮询层自愈，
   绝不在传输层重复提交）；延迟=full jitter 指数退避，Retry-After 取大者
   并 5s 封顶；每次重试前检查中止信号。 */
const TRANSIENT_STATUS = new Set([429, 502, 503, 504]);
const RETRY_MAX_ATTEMPTS = 2;

function retryDelayMs(retryAfterValue, attempt) {
  const retryAfter = Number(retryAfterValue);
  const ceiling = Math.min(500 * 2 ** attempt, 2000);
  const jittered = Math.random() * ceiling;
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    return Math.min(Math.max(jittered, retryAfter * 1000), 5000);
  }
  return jittered;
}

const sleep = (ms) => new Promise((resolve) => {
  setTimeout(resolve, ms);
});

async function responseCarriesAppError(response) {
  try {
    const type = response.headers.get("content-type") || "";
    if (!type.includes("json")) return false;
    const payload = await response.clone().json();
    return Boolean(payload && typeof payload === "object" && payload.error_code);
  } catch {
    return false;
  }
}

export async function request(path, options = {}) {
  const controller = options.controller || new AbortController();
  const headers = new Headers(options.headers || {});
  if (options.body != null && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const canRetry = options.method == null || String(options.method).toUpperCase() === "GET";
  for (let attempt = 0; ; attempt += 1) {
    let response;
    try {
      response = await fetch(path, {
        cache: "no-store",
        credentials: "same-origin",
        ...options,
        headers,
        /* 调用方可显式传 signal（如目录 fetch 的挂起死线）——优先于内部 controller */
        signal: options.signal ?? controller.signal,
      });
    } catch (error) {
      /* 只有网络层失败（真实 fetch 抛 TypeError）值得传输层重试；
         应用层/桩层抛出的其他异常原样上抛，绝不被吞成重试 */
      if (
        canRetry && attempt < RETRY_MAX_ATTEMPTS && !controller.signal.aborted
        && String(error?.name || "") === "TypeError"
      ) {
        await sleep(retryDelayMs("", attempt));
        continue;
      }
      /* N10B-5：网络层失败转人话——学生看到码表里的「网络暂不可用」，
         不再是裸 "Failed to fetch"；原始信息折进 detail。AbortError 不是
         错误（调用方主动取消），保持原样上抛由调用方静默处理。 */
      if (String(error?.name || "") === "TypeError") {
        throw new ApiError(ERROR_MESSAGES.network_unavailable, {
          code: "network_unavailable",
          status: 0,
          actions: [],
          retriable: true,
          detail: String(error?.message || ""),
        });
      }
      throw error;
    }
    if (
      canRetry && attempt < RETRY_MAX_ATTEMPTS && !controller.signal.aborted
      && TRANSIENT_STATUS.has(response.status)
      && !(await responseCarriesAppError(response))
    ) {
      await sleep(retryDelayMs(response.headers.get("retry-after"), attempt));
      continue;
    }
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const code = String(payload?.error_code || "http_error");
      const rawText = typeof payload?.error === "string" ? payload.error : "";
      const mapped = ERROR_MESSAGES[code];
      /* C3-3：带应用码但码表未收录 → 通用人话，英文原文折叠进 detail；
         无应用码的非 JSON/网关错误维持既有 raw/HTTP 兜底。 */
      const message = mapped
        || (code !== "http_error" && rawText ? GENERIC_ACTION_FAILURE : rawText || `HTTP ${response.status}`);
      throw new ApiError(message, {
        code,
        status: response.status,
        actions: Array.isArray(payload?.actions) ? payload.actions : [],
        retriable: Boolean(payload?.retriable),
        detail: !mapped && code !== "http_error" && rawText ? `${code} · ${rawText}` : "",
        receipt: payload?.schema === "courselens.api.v3"
          && payload?.data && typeof payload.data === "object"
          && payload.data.status === "rejected"
          ? payload.data
          : null,
      });
    }
    return payload;
  }
}

export async function apiV3(route, options = {}) {
  const payload = await request(`/api/v3/${String(route).replace(/^\/+/, "")}`, options);
  if (payload?.schema !== "courselens.api.v3") {
    throw new ApiError("后端返回了不受支持的协议", { code: "api_protocol_invalid" });
  }
  return payload.data;
}

export function postV3(route, body, options = {}) {
  return apiV3(route, { ...options, method: "POST", body: JSON.stringify(body ?? {}) });
}

export function putV3(route, body, options = {}) {
  return apiV3(route, { ...options, method: "PUT", body: JSON.stringify(body ?? {}) });
}

export function deleteV3(route, body, options = {}) {
  return apiV3(route, { ...options, method: "DELETE", body: JSON.stringify(body ?? {}) });
}
