# 复旦登录票据传输优化收口事实记录

Date: 2026-09-07 · Branch: `codex/fudan-ticket-transport-20260907`（基点 f7d4cfd = PR #106 合并点）
Contract: `docs/fudan-login-ticket-transport-optimization-handoff.md`（唯一合同）

## 根因与证据

- 真实脱敏遥测（`runtime/cache/fudan-login-stability-20260906/login-stage-1788749694272.jsonl`）：
  attempt 1/2 的 `webvpn_ticket_error/connection_error` 均为 **13094 ms**，attempt 3 整链
  172 ms 成功；另一 artifact 的 iCourse 票腿 6734 ms。与"一次性 WebVPN ticket 的最后
  传输腿冷连接失败"完全一致。
- 代码事实（f7d4cfd）：`login()` 第 1–6 步全部**直连** IdP；`webvpn.fudan.edu.cn` 的
  第一次接触恰恰是持有一次性 ticket 的 curl 腿，且 `ticket_session()` 为每张 ticket
  新建全新 curl session（冷 DNS/TCP/TLS）。
- 超时压平：`_follow_ticket_redirects` 对 curl 腿传 `max(TICKET_TIMEOUT)`=12 标量。
  curl_cffi 0.15.0 `stream=True` 时标量只设 `CONNECTTIMEOUT_MS`（utils.py:559），即
  连接超时被扩大为 12 s——13094 ms 失败正是该值被烧尽的签名。
- 代理隔离缺口：`proxies=dict(self.proxies) or None` 在直连 route 时为 `None`，
  libcurl 会读取 `http_proxy/HTTPS_PROXY` 等环境变量。curl_cffi 0.15.0 的
  `trust_env` 仅被存储、不被消费（CA bundle 环境读取是无条件的），
  不隔离代理。
- 连接复用障碍：curl_cffi 0.15.0 的 `stream=True` 走 `duphandle()`（session.py:626-631），
  克隆 handle 携带独立连接缓存，结构性放弃复用；非流式请求共用会话 handle 的持久缓存。
  （loopback 实证：同 session 3 请求，stream+close=3 连接，非流式=1 连接。）

## 选定方案（四点一体的最小主方案）

1. **预热**：`WebVPNSession.login()` 在任何凭据提交/ticket 产生前，
   `prepare_ticket_transport()` 对 WebVPN 根路径发一次无凭据非流式 GET
   （`VERIFY_TIMEOUT (3,5)`，8 s 硬上限）。任何 HTTP 响应（含 302→/login）算成功；
   仅传输异常算失败。失败时同 attempt 换 h1 备用 transport 重试恰好一轮，再失败上抛。
2. **持久 transport**：`_ensure_ticket_transport()` 惰性创建并预热一次，
   WebVPN 票腿与 iCourse 票腿复用同一 curl 连接池/cookie jar；每条票腿开始前
   `sync_ticket_cookies` 把 requests 主会话当前 jar 刷新进 transport（腿间
   Set-Cookie 轮换不带入过期 cookie），腿完成后 `merge_ticket_cookies` 反向
   回并——双向显式同步；`WebVPNSession.close()` 幂等、吞掉一切内部异常，
   恰好关闭 curl transport 与 requests 会话各一次（`_login_with_retry` 异常
   清理与 `ICourseClient.close()` 均改走 `vpn.close()`）。
3. **代理隔离 + 二元 timeout**：`ticket_session()` 显式空串代理（libcurl
   `CURLOPT_PROXY=""` 语义 = 禁用含环境变量的代理查找）+ `trust_env=False`；
   票腿直接下发 `TICKET_TIMEOUT (5,12)` 元组（非流式 = connect+read 17 s 硬性总限）。
4. **候选多样化**：`_login_with_retry` 的 route 与 transport 两维独立推进——
   ticket 传输失败（闭集 code）→ 下一 attempt 同 route 换备用 transport（一次）；
   其他失败 → 推进 route 回主 transport。进程内 `_LAST_TICKET_SUCCESS`
   只记闭集 (route, transport) 类别用于下次排序，不落盘、不含账号数据。

票腿 hop 由 stream=True 改为非流式：重定向正文极小且有 17 s 总限，
最终门户页一次性下载换取整腿复用预热连接（该改动更新了
`test_ticket_redirects_are_bounded_and_relative` 的 stream 钉死断言，
安全不变量——手工跟随、相对 urljoin、上限、登录页拒绝——全部保留）。

## 未选方案

- 被动 iCourse SSO（合同 §7）：defer。最多省正常路径约 1 s，不解决 13 s 票腿失败。
- Requests 备用 transport：Chrome 模拟握手是站点兼容前提，Requests 无法满足；
  保留 curl 唯一 transport 家族（h2 主 / 强制 HTTP/1.1 备）。
- 自建 IPv4/IPv6 竞速、HTTPAdapter/curl 自动重试、拉长 deadline、增加 attempt：
  合同 §9 明确禁止。

## 错误闭集与遥测

- 新增 `TicketTransportError(code=webvpn_ticket_transport_failed|icourse_ticket_transport_failed)`：
  仅当票腿传输异常**且**会话验证未确认时携带；恢复路径（验证成功）保持无 code。
  timeout 链在 login_status 仍归类 `timeout`（既有 `_exception_chain_contains` 优先级）。
- `LoginStageTelemetry`：`CLOSED_STAGES += webvpn_ticket_warmup`；
  `CLOSED_KEYS += route/transport`，值域闭集 `{"direct","proxy"}` / `{"curl_h2","curl_h1"}`，
  未知值整字段丢弃。前端 `EVIDENCE_DETAILS` 新增两条闭集文案，未知 code 兜底保留。

## 验证

- 执行式测试 `tests/test_ticket_transport.py`（20 用例，旧实现必失败）+ 既有
  `test_webvpn_session/test_login_telemetry/test_auth_timeouts/test_login_resilience`
  定向全绿。
- 同 seed（20260907）合成 A/B benchmark `tests/benchmark_ticket_transport.py`
  （baseline = f7d4cfd detached worktree；loopback IdP+WebVPN；驱动真实
  `_login_with_retry`；故障 = WebVPN 前 F 条连接 RST / 首连接挂起），
  每 profile 40 run：

  | profile | 成功率 | ready p50/p95 (ms) | 凭据提交 | WebVPN 连接 |
  |---|---|---|---|---|
  | F=0 | 1.00 → 1.00 | 344/422 → 328/421 | 80 → 80 | 240 → 120 |
  | F=1 | 0.97 → 1.00 | 437/547 → 344/421 | 117 → 80 | 312 → 160 |
  | F=2 | 0.97 → 1.00 | 421/531 → 375/530 | 117 → 80 | 312 → 200 |
  | F=3 | 0.97 → 0.97 | 500/609 → 250/312 | 156 → 78 | 390 → 239 |
  | hold 探针 | 均 1.00 | 12344 → 8250 ms | — | 8 → 4 |

  硬性验收：成功率不下降 ✓；同 ticket 零重放（每 run 断言）✓；
  凭据提交不增加（各 profile 下降 0–50%）✓。hold 探针复现真实签名：
  baseline 在持票 GET 上烧尽 12 s 压平超时（≈真实 13094 ms），
  candidate 8 s 预热上限且被挂起的是无票预热，同 attempt 内换 h1 成功。

## 受控真实验证

未运行。理由：合成证据已覆盖合同验收（成功率/重放/凭据预算/超时语义），
真实运行消耗凭据提交预算且不改变结论；按合同 §12 留给确有需要的后续验证。

## 文件清单

- `src/api/webvpn.py`：transport 常量/闭集错误/持久 transport/预热/代理隔离/二元 timeout/close 所有权
- `src/application.py`：`_login_with_retry` 候选状态机 + last-success + `vpn.close()`；遥测闭集扩展
- `src/api/icourse.py`：`close()` 走 `vpn.close()`
- `frontend/modules/ui.js`：两条闭集错误文案
- `tests/test_ticket_transport.py`（新）、`tests/benchmark_ticket_transport.py`（新）、
  `tests/test_webvpn_session.py`、`tests/test_login_telemetry.py`（断言同步）
