# CourseLens 发布前改进执行计划

> **历史记录（2026-09-10）：** 本计划为 2026-09-05 的发布前执行计划，其各项已由后续批次完成或被后续决策取代（见 [docs/handoff.md](../handoff.md)）。本文件仅作历史记录，正文不再更新，现状以 handoff 为准。

状态：待执行  
编写日期：2026-09-05  
适用仓库：`private/main`  
目标读者：接手实现与验证的 Codex/自动化模型  
唯一动态状态入口：`docs/handoff.md`

## 1. 任务目标

在不重做已经验收完成的生产拓扑和 P0 运行时链路的前提下，完成以下四项工作：

1. 恢复本地测试、文档和发布工具的可信基线。
2. 重做 CourseLens 桌面工作台的信息架构和视觉层，但保持现有后端、API v3、元素契约和安全语义不变。
3. 为 Worker 幻灯片源增加脱敏诊断能力，查清“1353 个非图片响应、19 个唯一有效页”的原因。
4. 在前三项完成后，为受控试用打包和干净 Windows 验收准备可审核证据；任何实际发布仍需另行授权。

本计划不是生产发布授权、真实课程操作授权或真实取消授权。

## 2. 执行前必须读取

开始任何命令或编辑前，按顺序完整读取：

1. 工作区根目录 `AGENTS.md`。
2. `private/main/docs/handoff.md`。该文件是唯一执行入口，内容优先于本计划中的历史快照。
3. 本计划。

如果 handoff 与本计划冲突，以 handoff 为准，并停止冲突步骤，不要自行调和。

## 3. 已完成事项，禁止重复

以下状态已经验收，不得重新迁移、重新发布、重新跑真实演练或以“确认”为由重复执行：

- 四仓库生产拓扑和签名直连镜像已经完成。
- 当前生产 Worker 镜像固定到 `166ac5f`，`remote_enabled=1`。
- process canary `33930989104` 已通过。
- 五分钟链路、故障注入、取消、暂停续跑、重启接管和诚实降级已经完成。
- 6290 秒完整讲次已经完成，运行 36.6 分钟，RTF 0.35，导入 210 段字幕，零残留。
- PR #89 至 #93、#95 和 #99 已完成，不要重做这些缺陷或文案改动。
- macOS 产品发布已经关闭，只保留 CI 冒烟。

## 4. 不可越过的边界

### 4.1 仓库边界

- 只允许编辑 `private/main`。
- `public/`、`managed/` 只读。不得手工改文件、提交、重写历史或 force push。
- 不得修改 `private/main/runtime/data` 下的任何内容。
- 不得读取、移动、删除或备份真实运行时数据。

### 4.2 凭据与课程数据

- 不得读取或打印凭据明文。
- 不得把凭据、Cookie、课程 URL、课程标题、课程正文、媒体内容或学生标识写入命令、日志、报告、测试快照或截图。
- 凭据只允许在获得独立授权后，从既定安全目录进行会话内注入，必须 `remember=false`。
- 每次登录必须使用全新的 `operation_id`。
- 不得把安全目录加入仓库、测试夹具或命令输出。

### 4.3 外部写入与真实操作

以下动作必须逐项取得用户明确授权：

- push、创建或更新 PR、merge、tag、GitHub Release、Actions dispatch。
- 修改公开镜像、个人 Worker、Mailbox、Secrets 或受保护环境。
- 打包、签名、上传、分发或安装试用版。
- 真实课程 OCR 诊断、真实作业运行、真实取消和任何清理动作。

真实取消始终需要独立 GO，不得包含在任何“继续”“测试”“发布前验收”授权中。

### 4.4 默认允许的本地动作

在用户要求实现本计划后，可以执行：

- 读取源码和文档。
- 编辑 `private/main` 中与当前阶段明确相关的文件。
- 使用系统临时目录、`runtime/cache` 或专门的合成输出目录运行测试。
- 使用合成数据启动本地合成 UI 服务和浏览器验收。
- 运行不会访问真实凭据、课程数据或远程写接口的静态检查和单元测试。

## 5. 总体实施原则

- 每次只执行一个阶段。当前阶段未通过，不进入下一阶段。
- 先修根因，不在每个调用点追加同类补丁。
- 优先复用现有函数、CSS token、原生 HTML 控件和既有 API。
- 不增加前端框架、CSS 框架、在线字体、CDN、图标依赖、OCR 引擎或新的远程服务。
- 不因为文件较大而拆分 `src/application.py`、`src/runtime/http_api.py` 或其他模块。
- UI 可以彻底改变信息架构和视觉呈现，但后端行为、闭集状态码、授权门禁和安全承诺不可弱化。
- 测试失败时报告首个根因和影响范围，不通过删除测试、放宽断言或吞掉异常使测试变绿。

## 6. 阶段 0：建立干净执行基线

### 6.1 只读预检

在 `private/main` 中运行：

```powershell
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

通过条件：

- 当前仓库、分支和提交与 handoff 一致。
- 工作树中没有与当前阶段重叠的用户改动。

如果存在未知改动，停止，不得覆盖、暂存、还原或删除。

### 6.2 测试命令约束

客户端测试必须使用：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q
```

Worker 测试必须使用：

```powershell
$env:PYTHONPATH = 'worker;.'
.\.venv-client-py310\Scripts\python.exe -m unittest discover -s worker/tests -p 'test_*.py' -q
```

执行结束后可以恢复本进程中的 `PYTHONPATH`，不得把它写入用户级或系统级环境变量。

## 7. 阶段 1：可信基线修复

目标：消除当前 5 个本地失败的根因，并让文档不再冒充动态状态来源。

### 7.1 修复 Windows Authenticode 子进程环境

主要文件：

- `scripts/sign_client_release.py`
- `tests/test_sign_client_release.py`

已知问题：`_powershell()` 启动 Windows PowerShell 时继承父进程的 `PSModulePath`。当其中含 PowerShell 7 或 Codex runtime 模块目录时，`Get-AuthenticodeSignature` 可能加载不兼容的 `Microsoft.PowerShell.Security`，导致 4 个测试在真正验证签名逻辑前失败。

实施要求：

1. 在 `_powershell()` 中复制当前环境。
2. 只从传给 `powershell.exe` 的子进程环境中移除 `PSModulePath`。
3. 不修改父进程环境，不硬编码本机模块路径。
4. 保持 `-NoProfile`、`-NonInteractive`、错误截断和 fail-closed 行为。
5. 增加一个最小回归测试：父环境具有无效或不兼容的 `PSModulePath` 时，inventory 仍能得到结构化结果。
6. 不导入真实证书，不调用时间戳服务器，不执行真实签名。

阶段内检查：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q tests/test_sign_client_release.py
```

通过条件：Windows 上相关测试全部通过；非 Windows 跳过语义保持不变。

### 7.2 移除测试对 `runtime/data` 的写入

主要文件：

- `tests/test_architecture_boundaries.py`
- `tests/test_online_only_mode.py`
- `tests/test_subtitle_duration.py`

实施要求：

1. 删除对 `PROJECT_ROOT / "runtime" / "data" / "tests"` 的创建和引用。
2. pytest 测试优先使用 `tmp_path`；unittest 测试直接使用不带 `dir=` 的 `tempfile.TemporaryDirectory()`。
3. 不创建新的测试目录管理器或夹具层。
4. 保持原测试语义和断言不变。

阶段内检查：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q tests/test_architecture_boundaries.py tests/test_online_only_mode.py tests/test_subtitle_duration.py
rg -n 'runtime[\\/]data[\\/]tests|runtime" / "data" / "tests' tests
```

通过条件：相关测试通过，第二条命令无匹配。

不要删除现有 `runtime/data/tests` 目录。删除属于运行时数据操作，需要另行确认。

### 7.3 收敛动态文档

主要文件：

- `README.md`
- `docs/process-canary.md`
- `docs/student-timetable.md`
- `docs/student-onboarding.md`
- `docs/windows-sandbox-acceptance.md`

实施要求：

1. `docs/handoff.md` 保持唯一动态状态入口。
2. `docs/process-canary.md` 删除或改写“Current gate status”中的旧提交、旧 run 和 `remote_enabled=false`；保留稳定的流程、失败语义和真实取消边界。
3. README 保留“受控试用、非公开生产发布”，但移除“完整长视频尚未通过”和 macOS 产品发布目标。
4. `docs/student-timetable.md` 改为 Windows 产品目标、macOS CI 冒烟，并把 2026-07-20 固定提交信息明确标成历史证据或移至历史段落。
5. `docs/student-onboarding.md` 说明 Windows Sandbox 仍被环境故障阻塞，当前干净机路径是已获准的 Hyper-V 方案，但不得宣称验收已完成。
6. `docs/windows-sandbox-acceptance.md` 保留当时“不自动转用 Hyper-V”的历史事实，并追加带日期的后续决策，不改写历史记录。
7. 不把凭据位置、课程信息或动态敏感状态复制到更多文档。

阶段内检查：

```powershell
.\.venv-client-py310\Scripts\python.exe scripts/check_markdown_links.py
.\.venv-client-py310\Scripts\python.exe scripts/check_text_encoding.py
rg -n 'remote_enabled.*false|Windows 与 macOS|Windows and macOS|完整长视频.*尚未' README.md docs
```

对最后一条命令逐项判断：历史段落可以保留，但必须有清楚的历史标签，不能被写成当前状态。

### 7.4 本地公开镜像一致性门禁

当前 `public/worker-mirror` 本地副本缺少生产固定提交 `166ac5f`，会导致 `test_public_template_reconstruction` 失败。

默认动作：只报告，不执行同步。

得到用户对“只读获取并快进本地公开镜像”的明确授权后，才可以：

1. 确认公开镜像工作树干净。
2. GitHub 直连失败时先重试，再使用 handoff 规定的代理回退。
3. 仅 fetch/fast-forward，不修改文件、不产生提交、不 push。
4. 重新运行：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q tests/test_public_template_reconstruction.py
```

如果仍失败，停止并报告缺失对象、实际 HEAD 和期望固定提交，不要修改 `runtime-assets.json` 来迎合本地旧副本。

### 7.5 阶段 1 完整验收

完成 7.1 至 7.3，并在 7.4 获授权且完成同步后，运行：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q
$env:PYTHONPATH = 'worker;.'
.\.venv-client-py310\Scripts\python.exe -m unittest discover -s worker/tests -p 'test_*.py' -q
node --check frontend\app.js
Get-ChildItem frontend\modules\*.js | ForEach-Object { node --check $_.FullName }
.\.venv-client-py310\Scripts\python.exe -m compileall -q src worker\courselens_worker scripts
```

通过条件：

- 客户端全套测试零失败。
- Worker 测试零失败。
- 前端语法和 Python compileall 通过。
- `git status --short` 只包含计划内文件。

## 8. 阶段 2：UI/UX 全面重设计

### 8.1 设计目标

产品类型：面向复旦学生的桌面学习工作台。  
核心使用场景：选课与选讲次、播放与字幕学习、生成资料、查看后台任务、处理账户和连接问题。  
设计方向：克制、内容优先、低干扰、可信状态、适合长时间阅读。  
目标不是营销落地页，也不是运维控制台。

“全面重设计”指信息架构、布局、层级、交互反馈和视觉系统全面更新；不指更换前端技术栈或重写后端。

### 8.2 必须保留的技术契约

- 保留 vanilla HTML、CSS 和 ES modules。
- 保留 `frontend/index.html` 中所有被 JavaScript、测试或后端依赖的元素 ID。
- 保留 `/api/v3` 路由、请求字段、闭集错误码、`operation_id` 和后端权威状态。
- 保留登录对话框的密码管理器和粘贴能力。
- 保留深色模式、`prefers-reduced-motion`、forced-colors 和键盘操作。
- 不把内部错误详情、URL、路径或敏感字段暴露给普通界面。
- 不在视觉层伪造成功、进度、在线状态或可恢复性。

### 8.3 视觉系统

主要文件：

- `frontend/styles/tokens.css`
- `frontend/styles/layout.css`
- `frontend/styles/components.css`
- `frontend/styles/components.css`
- `frontend/styles/accessibility.css`

实施要求：

1. 继续使用语义 token，不在页面组件中散落颜色值。
2. 保留当前绿色品牌方向；调整具体颜色前必须验证深浅色正文对比度至少 4.5:1、非文本控件边界至少 3:1。
3. 使用系统字体栈，例如 `Segoe UI`、`Microsoft YaHei UI` 和通用 sans-serif；不得加载网络字体。
4. 使用 4/8 像素节奏，减少随机间距。
5. 控件在桌面保持紧凑；在 `pointer: coarse` 或窄屏环境下最小点击区域提高到 44×44 像素。
6. 普通正文不小于 14px，主要阅读正文建议 16px、行高至少 1.5。
7. 不使用 emoji 作为结构图标。优先复用现有 SVG；确有缺口时新增少量同笔画 SVG 资产，不引入图标包。
8. 动画只表达状态或空间变化，默认 120 至 200ms；减少动态效果时取消非必要动画。
9. 不使用大面积玻璃拟态、渐变装饰、悬浮卡片墙或 landing-page hero。

在开始页面实现前，将最终采用的 token、字号、间距、圆角、阴影、断点和交互状态记录在 `docs/ui-design-system.md`。该文件只记录实际采用的规则，不复制技能生成器中不适用的落地页内容。

### 8.4 信息架构

保留六个现有工作区：课程、学习、搜索、任务、课表、设置。不要新增路由或“首页”，除非现有工作区无法承载明确需求。

#### 课程

- 保持课程列表、讲次列表和播放器的主任务链。
- 视觉顺序为：选择课程、选择讲次、确认可播放/任务状态、播放或生成资料。
- 把实时课堂、同步状态和低频诊断收进次级区域。
- 空状态必须给出下一步，不显示无含义的大块空白。

#### 学习

- 顶部固定显示当前课程和讲次上下文。
- 未选择讲次时显示单一空状态和“返回课程选择”动作。
- 选择讲次后，以原生按钮/标签式分段呈现字幕、笔记、资料、复习。
- 书签靠近字幕；课程关系和学习回顾放在次级“更多”区域。
- 切换分段不得重新请求或丢失已加载内容，除非现有业务逻辑明确要求刷新。

#### 搜索

- 主搜索框保持唯一主操作。
- “生成回答”在有搜索结果后才进入主视觉层级。
- 清楚区分索引未准备、无结果、请求失败和已得到结果。
- 结果摘要保持可读行宽，不用全屏长行。

#### 任务

- 任务列表和进度是页面主体。
- 每个任务优先显示用户可理解的名称、状态、进度、ETA 和可执行动作。
- 不直接把 `task.state`、`error_code`、组件英文名和内部 code 当正文展示。
- 原始闭集代码只能位于可展开的“技术详情/复制诊断”区域。
- 远程连接改为折叠的“连接诊断”，默认只显示一句总体状态和唯一推荐动作。
- 暂停、继续、重试和取消仍由后端返回的 actions 决定，前端不得推测。
- 真实取消门禁不因按钮重设计而改变。

#### 课表

- 桌面宽屏保留周视图。
- 窄屏改为按天分段或纵向日程，不允许依靠水平滚动查看整周。
- 保留原生日期输入和 ICS 导出。
- 课程颜色不能成为唯一识别方式，必须同时显示文本。

#### 设置

将现有同权卡片重组为四组：

1. 账户与隐私：复旦账户、会话、云端处理、本地统计。
2. AI 与处理：DeepSeek Key 和相关说明。
3. 网络与诊断：网络模式、代理、连接安全、脱敏诊断。
4. 更新与帮助：客户端更新、版本、帮助入口。

要求：

- 使用原生 `<details>` 或现有 `<dialog>` 做渐进披露，不新增路由框架。
- 删除已保存账户和删除 DeepSeek Key 前必须二次确认。
- 保存、删除、诊断和隐私开关必须有 busy、成功和失败反馈。
- 隐私开关请求失败时恢复为后端确认状态，并把焦点留在相关控件或错误提示上。
- 网络保存结果不得直接显示 `JSON.stringify(...)`；只显示闭集、已本地化摘要，原始安全诊断仍只通过脱敏复制功能提供。

### 8.5 UI 实现顺序

不要一次修改全部页面。按以下纵向切片执行，每个切片完成测试和截图审查后再继续。

#### 切片 A：设计系统与应用壳

范围：tokens、顶部栏、导航、页面宽度、通用按钮/表单/状态/空状态、响应式断点。  
不改业务模块。

#### 切片 B：任务与设置

范围：`frontend/index.html`、`frontend/modules/tasks-drawer.js`、`frontend/modules/settings.js` 和相关 CSS。  
原因：这两个页面当前内部状态暴露最多，也包含删除、隐私和诊断操作。

#### 切片 C：课程与学习

范围：课程选择、播放器周边层级、学习分段导航、空状态。  
不修改播放、字幕、书签、任务创建和资料加载协议。

#### 切片 D：搜索与课表

范围：搜索状态层级、结果阅读体验、桌面周视图和窄屏日视图。

### 8.6 UI 测试要求

每个切片至少执行：

```powershell
.\.venv-client-py310\Scripts\python.exe -m pytest -q tests/test_frontend_workbench.py tests/test_frontend_shell_security.py tests/test_frontend_playback_recovery_behavior.py tests/test_documentation.py
node --check frontend\app.js
Get-ChildItem frontend\modules\*.js | ForEach-Object { node --check $_.FullName }
```

使用现有 `tests/synthetic_shell_server.py` 和合成数据完成浏览器检查。不得启动真实登录或真实课程会话。

检查尺寸：

- 375×812
- 768×1024
- 1024×768
- 1440×900

每个尺寸检查：

- 六个工作区均无意外水平滚动。
- 375px 下导航和主要动作可触达，内容不被固定栏遮挡。
- 键盘 Tab 顺序与视觉顺序一致，焦点环清晰。
- 对话框打开后焦点进入对话框，关闭后返回触发按钮。
- 深色和浅色模式均可读。
- reduced-motion 下没有必要之外的动画。
- 空、加载、成功、失败、需操作、禁用状态可区分，且不只依赖颜色。
- 所有图标按钮有 `aria-label` 或等价可访问名称。
- 删除操作有确认，失败时不会错误显示已删除。

截图只能使用合成数据。截图文件进入临时验收目录，不默认提交仓库。

### 8.7 UI 阶段通过条件

- 现有 API v3 和后端测试未因 UI 改造变化。
- 六个工作区完成上述桌面和窄屏验收。
- 普通界面不显示未本地化内部状态码。
- 设置页的删除和隐私操作具有确认/失败恢复。
- 无新增运行时依赖、在线字体或 CDN。
- 全套客户端与 Worker 测试继续通过。

## 9. 阶段 3：Worker 幻灯片 OCR 脱敏诊断

### 9.1 当前证据和待证假设

现有完整讲次记录中，1497 个候选记录产生：

- 19 个唯一有效图像页。
- 125 个已解码但重复的图像。
- 1353 个非图片响应。

这表明首要问题在幻灯片源获取或会话生命周期，不足以证明 OCR 引擎质量有问题。

当前只能提出以下待证假设：

- HTTP 200 返回登录/授权 HTML。
- HTTP 200 返回 JSON 错误或占位内容。
- 长批次过程中幻灯片能力或会话过期。
- direct/WebVPN 路由对部分图片地址不适用。
- 上游记录中确实包含大量非图片或重复条目。

不得在没有分类证据前选择其中一个结论。

### 9.2 第一步：只增加正文类型分类

主要文件：

- `worker/courselens_worker/ocr.py`
- 相应 Worker 测试文件

实施要求：

1. 在调用 Pillow 前，仅检查内存中响应字节的短前缀。
2. 至少区分：`empty`、`html_body`、`json_body`、`unidentified_image`、`decode_failed`、`ocr_failed`、`duplicate`。
3. HTML/JSON 判断必须保守；无法确定时归入 `unidentified_image`，不能猜测具体上游错误。
4. 只输出闭集原因和计数，不输出正文片段、URL、host、path、headers、Cookie、课程字段或哈希以外的来源信息。
5. 继续允许单页失败后处理后续页面。
6. 保持 checkpoint 每 5 项、恢复语义、去重和现有返回结构兼容。
7. 不改变 OCR 引擎、并发数、prefetch 或图片阈值。

最小测试：

- 空正文。
- 以空白/BOM 开头的 HTML 登录页。
- JSON 对象和数组。
- 正常图片。
- 不支持的二进制。
- 两张重复图片。
- checkpoint 恢复后分类计数不重复累加。

### 9.3 第二步：合成会话与源生命周期测试

主要文件：

- `worker/courselens_worker/platform_session.py`
- `worker/courselens_worker/source.py`
- 现有对应测试

本步骤先写测试，不立即添加刷新实现。需要覆盖：

- slide direct source 的 header 处理。
- WebVPN 包装路径。
- HTTP 200 HTML 被分类而不是送入 OCR。
- 枚举幻灯片后连接器的关闭时机。
- 媒体已有 refresh/fallback 行为不回归。

如果现有代码已经满足某项，保留测试，不增加实现。

阶段内检查：

```powershell
$env:PYTHONPATH = 'worker;.'
.\.venv-client-py310\Scripts\python.exe -m unittest discover -s worker/tests -p 'test_*.py' -q
```

### 9.4 阶段 3 通过条件

- 合成输入可以把 1353 类问题拆成可行动的闭集计数。
- 报告中没有敏感正文和来源定位信息。
- 不需要真实凭据即可完成全部测试。
- Worker 全套测试通过。
- 尚未改变生产 slide URL 路由、刷新或登录行为。

## 10. 阶段 4：受控真实 OCR 小样本诊断

本阶段默认暂停，只有用户给出独立、明确的真实只读 OCR GO 后才能执行。

### 10.1 授权范围必须明确

授权至少要明确：

- 允许访问一个已授权讲次的幻灯片源。
- 仅抽样 20 至 50 个候选，不运行完整讲次。
- 凭据会话内注入，`remember=false`。
- 使用全新 `operation_id`。
- 不创建真实取消、不修改课程、不发布 Worker。
- 只保存闭集计数和耗时，不保存图片、正文、URL、路径、Cookie 或课程标识。

缺少任何一项，保持暂停。

### 10.2 诊断顺序

1. 进行登录前只读 preflight，确认零活动任务和精确目标。
2. 建立新会话和 keepalive。
3. 获取有限候选并运行分类，不做完整 OCR 生成。
4. 记录 direct/WebVPN 的聚合成功数、闭集失败数、刷新次数和耗时；不得记录单条来源。
5. 完成后确认无任务、Artifact、lease、临时结果和本地敏感残留。

### 10.3 根据证据选择最小修复

- 如果多数为 HTML/JSON 授权响应：复用媒体源已有的 bounded relogin/refresh/fallback，为 slide source 添加最小等价能力。
- 如果只在 direct 路径失败：根据既有安全校验切换到已认证 WebVPN fallback，不放宽 URL 或公共地址验证。
- 如果多数为真实占位记录：在枚举阶段用稳定字段过滤；没有稳定字段时继续在下载后分类跳过。
- 如果有效图片很多但文字为空：这时才评估 OCR 参数或引擎。
- 如果证据混合：优先修占比最高且可稳定复现的一类，不同时重写路由和 OCR。

任何生产 Worker 修改、镜像生成、PR、merge、canary 或真实完整讲次复验都进入新的授权步骤，不属于本阶段自动动作。

## 11. 阶段 5：受控试用收口

本阶段只在阶段 1 至 4 的必要部分完成后规划，不自动执行。

### 11.1 用户必须先决定

- 版本号，当前建议候选为 `0.2.0-beta.1`，但不得替用户决定。
- 更新通道，handoff 中当前候选为 `stable`，仍需用户确认。
- 分发方式和接收对象。
- 是否进行本地打包、签名、Hyper-V 干净机安装和外部发布。

### 11.2 发布前必备证据

- 客户端、Worker、前端语法、compileall 全绿。
- UI 合成截图矩阵和可访问性检查完成。
- OCR 分类测试通过；如果修了生产源链路，有对应授权和真实小样本证据。
- 依赖审计无已知漏洞，或每项例外有明确说明。
- Authenticode 工具在干净 Windows 环境可确定运行。
- Hyper-V 干净机启动、安装、首次登录前界面、卸载/回滚按授权完成。
- 发布门禁脚本只允许在所需配置真实就绪后通过，不得修改门禁结果来适配当前环境。

### 11.3 明确不包含

- macOS 安装包或产品验收。
- Windows Sandbox 修复。
- 真实取消演练。
- 删除旧 Worker 或运行时历史。
- 公共正式发布，除非用户另行明确授权。

## 12. 执行报告格式

每完成一个阶段，向用户提交以下内容：

### 结果

- 完成了什么。
- 尚未完成什么，以及是否被门禁阻止。

### 修改

- 修改文件列表。
- 每个文件的一句话目的。

### 验证

- 实际运行的命令。
- 通过、跳过和失败数量。
- 失败的首个根因，不粘贴可能含敏感信息的完整日志。

### 边界确认

- 是否接触 `runtime/data`：必须为否，除非用户另有明确授权。
- 是否读取或输出凭据：必须为否。
- 是否访问真实课程：默认必须为否。
- 是否产生外部写入：默认必须为否。
- 工作树中是否存在非计划改动。

### 下一门禁

- 下一步需要的具体授权；若不需要，说明下一阶段名称。

不要提交“仍在运行”“没有变化”之类例行状态，只报告完成、失败、真实阻塞、高风险门禁或里程碑。

## 13. 最终完成定义

只有同时满足以下条件，才可以称为“发布前改进完成”：

- 本地完整测试和 Worker 测试零失败。
- 测试不再写入 `runtime/data`。
- 当前状态文档不再与 handoff 冲突。
- 六个工作区完成新的信息架构和视觉体系，并通过合成响应式与可访问性验收。
- 普通 UI 不直接展示内部状态码或原始 JSON。
- 删除和隐私设置具有确认、busy、失败恢复和后端确认状态。
- OCR 非图片响应已经得到闭集分类。
- 若执行过真实 OCR 小样本，过程有独立授权、脱敏报告和零残留确认。
- 没有新增不必要依赖、框架或远程服务。
- 没有重做既有 P0、真实取消或生产拓扑。
- 所有外部写入、打包、签名和发布动作均有逐项授权。

如果只完成其中一部分，必须按实际阶段报告，不能把“代码已准备”写成“已发布”或“已上线”。
