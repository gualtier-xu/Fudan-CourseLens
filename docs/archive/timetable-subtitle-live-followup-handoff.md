# 课表、字幕与直播入口后续交接

## 目标与硬边界

本合同只处理三件事：继续定位并修复课表无法显示的根因；改善播放器字幕的遮挡与视觉可读性；使用已有合成直播房间展示入口和界面，供用户检查。先完整阅读根 `AGENTS.md`、`docs/handoff.md`、`docs/student-workspace-followup-handoff.md`、`docs/student-workspace-login-combined-closeout-handoff.md`。若约束冲突，采用更严格的一项。

只允许编辑 `private/main`。先做 fresh preflight：`git status --short`、相关文件 diff/hash、调用链、已有测试入口及其离线性。所有既有已修改和未跟踪文件都是用户基线，绝不覆盖、删除、`reset`、`stash`、`restore` 或 `checkout`。若另一写入任务仍在运行或工作树持续漂移，先停止写入，只做只读调查；其终态后重新 preflight。

单一 worker 顺序处理三项，不并行写入。UI 调查和实现使用 `ui-ux-pro-max`；任何涉及真实认证、会话、票据、账号、凭据或其他信任边界的动作或改动，先由 `risk_auditor` 通过。完成后交由独立 `reviewer` 审阅当前增量。只在可复现根因支持时改动，以实际根因确定最小 allowlist；不新建依赖、页面、路由、协议或后台服务。

禁止读写 `runtime/data`、`public/worker-mirror`、`managed`，禁止 Git add/commit/push/PR/release、外部写入、真实任务或取消。不得输出凭据、cookie、ticket、URL query、账号、课程正文、真实直播内容或任何原始敏感遥测。真实认证、真实直播 grant、真实 HLS 或真实课表刷新均不在本合同授权内；只有课表在本轮另获 fresh 用户 GO 且 risk gate PASS 时，才能做下文严格限定的**一次只读 refresh 诊断**。

## 已知证据与调查纪律

用户截图显示：课表面板同时出现“课程目录暂时无法确认”和“本周没有可显示的课表”。这只确认了学生看到的症状，不能证明课表双源、SSO 或上游的根因。字幕截图确认原生字幕出现多块纯白底、长行和画面遮挡，不能仅凭截图断言是哪条 CSS 或 cue 数据造成。

当前代码锚点须 fresh 验证：

- `GET /api/v3/catalog` 与 timetable snapshot/refresh 是独立链路。catalog 的错误不应被解释为 timetable 错误。
- `GET /api/v3/timetable` 只读取身份作用域下的本地 snapshot；空态可能只是不存在有效快照。只有 `POST /api/v3/timetable/actions` 的 refresh 才经 `src/runtime/timetable.py` 访问双来源。
- `partial_failures` 已有服务端字段和前端呈现路径。因此截图不能证明“前端未显示失败”，须先用合成数据重现调用链。
- `index.html` 的 `<track default>` 与 `frontend/modules/player-core.js` 的 `/api/v3/subtitles/file` 构成原生字幕路径；服务端 `split_long_cues` 当前默认按 44 字拆 cue。`video::cue` 规则存在，但截图的白底表现不足以证明它在 Chromium 实际生效。
- `frontend/modules/live-room.js`、`player-core.js`、`src/runtime/live_room.py` 和 `tests/synthetic_shell_server.py` 已覆盖 `live`、`upcoming`、`ended`、`denied`、`offline`、`stale`、`unknown`，以及 grant/session/HLS `ENDLIST`。优先扩用已有 fixture，不创建真实直播链路。

现有 synthetic fixture 还不足以证明播放器视觉：回放媒体固定返回 404，直播 HLS 只有空 `ENDLIST`，现有 VTT 也只有顺序 cue。不得用这些状态截图声称已经验证字幕渲染或“进入后的直播播放器”。允许在 `tests/synthetic_shell_server.py` 与 Git 忽略的 `runtime/cache` 中补充最小、本地、无敏感信息的可解码测试媒体、HLS 清单/分片和重叠 VTT；不得下载媒体、添加依赖或把二进制测试产物加入 Git。

外部轻调研只作为浏览器能力背景：[MDN 对 `::cue` 的说明](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Selectors/::cue)确认 WebVTT cue 可由 CSS 样式化，但可用属性受限，且背景会按 cue 分别绘制；[MDN WebVTT API](https://developer.mozilla.org/en-US/docs/Web/API/WebVTT_API)说明 `::cue-region` 目前没有浏览器支持。不要由此推断本项目已正确生效；必须在合成 Chromium 中验证。

## 第一项：课表继续定根与最小修复

1. 用 synthetic schema、SSO handoff 和 semester fixture 分别复现：无 snapshot、过期 snapshot、有效双源、单源 partial failure、两源失败、HTML 授权页、JSON/schema 漂移、未登录/会话失效。只记录闭集 `code`、HTTP/content-type 分类、schema shape 与耗时；不记录课程值、正文或身份数据。
2. 沿 `timetable.js`、HTTP 路由、`application.py` 和 `runtime/timetable.py` 追完整调用链与所有错误映射。确认 catalog recovery 面板与 timetable 状态保持分离；空 snapshot 不能被显示成已刷新成功。
3. 根据证据修复一个共享根因，而不是只改文案或截图路径。保持首次 load 只读本地 snapshot，只有用户显式刷新才访问来源。refresh 失败、partial、stale、缺少开学日和成功快照均需给出状态和下一步，但不可伪称上游已恢复。
4. 仅当合成证据无法区分真实 schema/SSO/来源问题、且用户在**本任务中**给出 fresh GO、risk_auditor PASS 后，才允许限次、复用现有 DPAPI 会话的真实只读 refresh 诊断。输出只限闭集 code、schema shape、content-type/redirect 分类和计时；不人工读取或保存 `runtime/data`、课程内容、URL 或会话信息。否则标为 `unverified`，不执行。

验收：合成各状态可以准确进入对应 UI；catalog 故障不覆盖 timetable 诊断；无有效 snapshot 保持诚实空态；`partial_failures` 的来源和可行动建议可见；不以“刷新成功”掩盖失败。

## 第二项：字幕可读性和不遮挡

先让合成播放页实际加载本地可解码媒体，并断言触发 `loadedmetadata`、视频可见、恢复面板隐藏；不满足此前置条件时不得判断 `::cue` 是否可控，也不得进入自绘 overlay。合成 VTT 必须同时包含长 cue 和至少两个时间区间重叠的 active cue，以真实复现多块字幕竞争。随后在 Chromium 验证：`video::cue` 是否实际生效、同时 active 的 cue 数量、长文本的换行与分段、控制条显示时的安全区、全屏与窗口、浏览器缩放。覆盖 375/1000/1440 宽度、200% 缩放、亮/暗主题、键盘字幕开关和屏幕阅读器可达性。不得将固定 44 字当作验收指标，必须按实际容器宽度、字体和行高测量；不得把字体缩到不可读来规避遮挡。

按最小梯子实施：

1. 先修复 cue 的显示分段、重叠与 `::cue` 样式，复用浏览器原生 `<track>`、当前时间轴和字幕开关。
2. 只有在 Chromium 合成验证表明原生 cue 的布局/样式仍不可控时，才做最小自绘 overlay。自绘时必须禁用原生重复渲染，复用同一媒体时间轴和既有字幕开关，最多两行，置于控制条上方的安全区，不能挡住教师和主要画面。视觉为半透明深海军底与暖白字，不出现纯白矩形；全屏、窗口、缩放和窄屏都一致。

不得修改字幕事实、存储格式、任务/Worker 协议或重新生成字幕。为实际修复添加最小离线回归：至少能捕获重叠 cue、过长 cue 和字幕开关回归。

验收：在合成截图和 DOM/行为断言中，长 cue 不会形成多块竞争的白底、最多两行、控制条可操作；375/1000/1440、200%、亮暗与全屏无可见横向溢出；键盘与辅助技术不被覆盖层劫持。

## 第三项：用合成直播房间供用户检查

优先使用或扩展 `tests/synthetic_shell_server.py` 的既有 fixture，通过现有 `live-room.js` 和播放器路径展示直播。禁止真实认证、真实 grant、真实 HLS、外联和读取真实课程数据。

先审美和可用性审查现有入口，不为“模拟”重设计产品。若确有入口发现性或状态表达问题，入口应紧邻已选课程或播放器标题，不埋在动作区；否则只做合成展示，不改产品。每个状态有文字与状态点，`live` 可加红点但绝不能仅靠颜色；只保留一个明确 CTA；状态更新使用 `role=status` 或等价礼貌播报且不抢焦点。

建立并运行截图矩阵：课程选择或学习桌入口、`live` 可进入、进入后的播放器、`upcoming`、`ended`、`offline`、`stale`、`denied`，至少含 375 宽度及亮/暗主题。若要把“进入后的播放器”标为已验证，synthetic fixture 必须提供最小本地可播放 HLS 清单与无敏感分片，并断言 `loadedmetadata`、视频可见且恢复面板隐藏；只有空 `ENDLIST` 时，只能验证入口和诚实恢复态，播放器视觉必须标 `unavailable`。截图必须是合成本地实例，目录放入既有 gitignored cache 约定，不加入 Git。把可供用户检查的启动方式、路径和状态矩阵写入最终报告；若本机无法启动合成服务器或生成可播放测试媒体，报告阻塞和已有离线行为证据，不以静态臆造替代。

验收：入口不会误导为已直播；live CTA 可进入模拟播放器；非 live 状态不出现不可用的进入按钮；移动端状态与 CTA 清晰、无溢出；状态变更不改变键盘焦点。

## 验证、停止与报告

先确认命令完全离线或使用 synthetic fixture，再按变更运行：相关 JS `node --check`、播放行为 mjs harness、timetable、subtitle split、live-room、API/workbench 的定向 pytest；必要时 fresh client/worker 全量 suite。不得把历史的通过数作为本次结果。合成浏览器验收需保存截图并列出环境、场景和结果。

立即停止并报告：工作树漂移；没有可复现根因；需要真实课表却没有 fresh GO/risk PASS；需要真实认证/直播/任务；需要 `runtime/data`、public、managed、协议发布或超范围路径；risk gate 非 PASS；或无法确认测试离线。

最终报告必须区分 `verified` 与 `unverified`，包括：fresh preflight 与脏基线保护；三项各自的根因、最小文件和结果；合成截图矩阵；离线命令与实际计数；任何真实验证是否运行（默认未运行）；reviewer 结论及 P0/P1/P2；剩余风险和完整 `git status --short`。可给提交建议，但不得执行 Git 写操作。
