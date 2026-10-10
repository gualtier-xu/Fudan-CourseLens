# 复旦登录耗时与稳定性执行交接

## 目标、优先级与前置材料

目标是以最小、安全、可测的改动降低复旦登录到 `auth ready` 的不确定性，并改善失败时的诚实反馈；不是重写认证、绕过 WebVPN/2FA，或把 catalog 失败伪装成登录成功。本文件是执行合同；`docs/fudan-login-stability-prompt.md` 只作为前置调研/假设，冲突时以本文件为准。

先读根 `AGENTS.md`、`docs/handoff.md`、`docs/fudan-login-stability-prompt.md`、`docs/ui-usability-auth-followup-handoff.md`，再做 fresh preflight：`git status --short`、目标路径 diff/hash、认证调用链与全部调用者、现有测试和离线性。保留所有脏基线；不 `reset`、`stash`、`restore`、`checkout` 或覆盖。

单一 bounded worker 写入；设计写入前和真实验证前均需只读 risk_auditor gate；完成后独立 reviewer。不得改协议、凭据格式、DPAPI、账户隔离、2FA、`operation_id` 或任务系统。

## 阶段 0：只读追踪与严格脱敏时序

完整追踪 `webvpn.py`、`application.py`、`settings.js` 的全调用链及调用者。建立阶段时序模型：DNS/connect/TLS/redirect（仅 HTTP 客户端可得范围）、WebVPN 建立、IdP auth context/method/key/execute/ticket、iCourse CAS、session check、catalog refresh、frontend poll。

时序 artifact 只能记录阶段名、开始/结束或 `duration_ms`、闭集 outcome/error category、必要时安全 HTTP status、attempt 序号和 operation_id 的 SHA-256 短投影。禁止请求/响应正文、headers、cookies、URL query、账号、课程或凭据。

## 只读公开研究：待验证前提

联网研究仅限匿名、被动、只读：优先复旦官方公开 VPN/统一认证资料及 Requests/urllib3 官方文档；公开 GitHub 实现只作二级互证，先查 license，绝不复制来源不明代码。禁止访问登录端点、提交表单、探测账户、压测、绕过验证码/2FA、Selenium、MITM/抓包或注册账号。

以下为 2026-09-06 预核、仍待本地代码与合成测试验证的结论：

- 复旦 [WebVPN 使用指南](https://icampus.fudan.edu.cn/42/23/c33377a410147/page.htm)（官方，2020-02-25，一级来源）称 WebVPN 经 UIS 登录，且当时仍在测试阶段。
- 复旦 [新版零信任 VPN 系统开通上线](https://icampus.fudan.edu.cn/f1/a8/c31333a782760/page.htm)（官方，2026-06-10，一级来源）称新版零信任 VPN 改善访问效率；这不等于授权集成客户端或推断现有 WebVPN 协议。
- [Requests advanced usage](https://requests.readthedocs.io/en/stable/user/advanced/) 与 [urllib3 Retry](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html)（官方文档，二级技术依据）说明 session 连接复用和有界重试能力；它们不证明任何 Fudan cookie/domain/ticket 可跨路径复用。
- 公开 iCourse/WebVPN 实现即使找到，也只是二级互证，不能视为官方协议或复制来源。

## 候选方案与决策记录

实施前先输出一页决策记录：证据、基线、最多一个主方案和一个低风险辅项、未选方案、精确文件和验证。候选按最小/安全排序：

1. **A：连接池/Session 复用**，去除可证明冗余 redirect/check；先确认已有能力和 cookie/domain 边界。
2. **B：身份与 catalog 状态分离**，仅在身份认证明确完成后异步加载 catalog；不得提前展示缓存课程，不得将 catalog 失败标作登录成功，保留身份门禁和诚实状态。
3. **C：自动路由实验**，仅当代码证据和合成测试证明 cookie/domain/ticket 隔离安全时，才可考虑公开 IdP 直连优先、整链 WebVPN 一次回退、双 session 隔离、fail closed；失败中不得超过现有凭据提交预算。
4. **D：keepalive** 仅做活动感知和到期校验；不得以高频保活掩盖失效。

默认不做 UI route 设置、不缓存 `getJsPublicKey`、不集成零信任客户端、不盲目缩短 timeout 或增加重试。新证据和 risk_auditor 批准前不偏离默认。

错误 `credentials_rejected` 绝不重试；瞬时错误遵守有界总 deadline/退避；禁止并发或重复登录。保留前端防双击、busy、阶段状态和晚到响应保护。

## allowlist 与实施

从阶段 0 决定最小 allowlist。认证核心 `src/` 改动必须有设计写入 risk gate，并记录精确路径、调用链与不改的替代方案。除已选最小文件、相应离线测试、必要事实同步外不写入。禁止 Git 写、push、PR、发布、Secrets、真实任务/取消、public/managed 修改和 `runtime/data` 人工读写。

## 合成 benchmark 与离线验收

合成 benchmark 必须在同 seed、同故障脚本下比较 baseline/candidate；报告 p50/p95、成功率、请求数、回退次数、总 ready 与 auth-ready 的分开耗时。candidate 不得降低成功率；报告相对变化，不设虚构硬阈值。不得用真实站点反复测成功率。

离线测试至少覆盖：成功、错误密码、WebVPN timeout/5xx、直连失败回退（若实现）、双败保主因、ticket/cookie 隔离、catalog 失败、会话过期、并发双提交。运行 node/mjs、定向 pytest、全量 pytest 前，先检查测试标记、代码和既有约定可确认无真实外联；不能确认则不运行并标 `unavailable`。

## 受控真实认证门禁

本任务已授权最多 **2 次**受控真实认证，但仅在合成验证充分、设计 gate 与真实验证前 risk_auditor 均 PASS、且确有必要时。仅使用 `C:\Users\admin\.courselens-secrets\` 的既有 DPAPI `p0_session.py` 会话内注入、`remember=false`、fresh `operation_id`；安全存储不可用即停止。不得把聊天中的值复制进命令、脚本、环境、日志或报告；失败重试遵守既有退避，非必要不重复。

真实认证期间禁止截图、trace、DOM/network/storage dump；不读写 `runtime/data`。客户端正常运行可能产生运行时写入时，必须在尝试前披露其类别且不读取内容。输出仅可写 Git 忽略 `runtime/cache` 的脱敏 schema：阶段、duration、闭集结果、安全 HTTP status、短 hash operation_id、attempt、success；不得含任何秘密或真实课程内容。

## 验收、停止与报告

验收要求：决策记录符合最多 1+1 方案；离线测试/benchmark 通过或诚实 `unavailable`；无新增失败；真实验证若运行则在两次以内并只给脱敏结论；reviewer 无 P0/P1。停止并报告：需要协议/秘密/真实网络研究以外访问、超出 allowlist、risk gate 不通过、安全存储不可用、需要更多尝试或会损害用户基线。

最终报告模板：

1. 研究来源、日期与证据强度；
2. baseline 时序与待验证假设；
3. 决策、未选方案与最小文件；
4. 合成 before/after benchmark 与测试；
5. 真实验证状态（未运行或脱敏结论）；
6. 风险/reviewer 结论、未验证项、完整 `git status --short`。
