# CourseLens 数据流（文字版）

用一段话回答「一次点击之后发生了什么」。本文只描述当前已实现并验证的行为，每节给出代码锚点；概念定义见文末[术语表](#术语表)。安全边界的完整陈述见 README[安全与隐私架构](../README.md#安全与隐私架构)。

## 三个域

```
┌─────────── 本机域 ───────────┐   ┌─── 校园域 ────┐   ┌─────── 云域 ───────┐
│ 浏览器壳 ←→ 本地后端(127.0.0.1) │ ←→ │ webvpn/icourse │   │ GitHub(两仓)        │
│    ↕ runtime/data  ↕ DPAPI    │   │ id/fdjwgl      │   │ Worker Actions      │
│    学习记录/任务/检索索引       │   │ (短时会话)      │   │ api.deepseek.com    │
└──────────────────────────────┘   └────────────────┘   └────────────────────┘
```

- **本机域**：前端是静态壳（`frontend/`），只与 `127.0.0.1` 上的本地后端说话（`src/app.py:410-414` 绑定锁死回环）。目录、课表、字幕、任务与学习记录都落在 `runtime/data`；学号/密码/API Key 经 Windows DPAPI 加密后存盘（`src/remote/github_app.py:4`、`src/application.py:4387`）。
- **校园域**：本地后端持复旦会话（WebVPN/iCourse），按需读目录、课表、直播流与平台文稿；会话检查点是最小化、主机限定、DPAPI 持久的 cookie 子集（`src/api/webvpn.py:1326-1334`）。浏览器拿不到上游 Cookie 或永久媒体地址，只拿短期不透明播放会话。
- **云域**：只在两类动作里出场——①显式发起的生成任务（GitHub 两受管仓 + 签名 Worker 镜像 + 可选 DeepSeek）；②显式开启的「自动学习材料」（每日 13:00/22:00 定窗）。全部外联主机=10 个的固定闭集（`src/runtime/network.py:14-19` SERVICE_PROBES + 会话客户端主机常量，含研究生课表数据源 `yjsxktest.fudan.sh.cn` 与更新下载资产主机，`src/distribution.py:25-30` allowed_hosts）。

## 一条生成任务的一生

1. **派发**：学生点「生成字幕 / AI 处理」（或勾选了自动整理）。后端把任务落库（`privacy_state="sealed"`，`src/application.py:7812`），经 GitHub App 令牌向你的专属仓派发 workflow（`src/remote/github_app.py:2057`）。
2. **执行**：你自己的 Worker Runner 拉起加密载荷，跑识别→校对→打包；期间凭据只存在于 Worker 环境密钥里，任务结果不落明文。
3. **回传**：结果作为加密 Artifact 上传；客户端侧 `import_remote_task_result`（`src/application.py:1598`）走本地验签+事务导入后生效，进入可验证清理流程（云端副本随后删除；未导入的云端数据最长保留 30 天）。
4. **可见**：导入后字幕/总结进入讲次面板与复习视图，任务卡记录耗时与 token 消耗，全程只有本机能看。

## 学习闭环（全程本机）

播放进度、没听懂标记、回看热点只进本地库；检索走本地索引（`src/runtime/search_index.py:416`）；AI 问答/解释是「检索证据 + 你的 DeepSeek Key」的组合，回答页始终标注「由模型生成，不作为检索事实」。

## 信任链（为什么能相信跑的是被审计的那份代码）

- 公开 Worker 仓是 CI 用 Ed25519 签名生成的只读镜像；发布核验 `document_sha256` 在签名 manifest、本地导出与公共仓三方逐字节恒等。
- 客户端更新链 fail-closed：安装清单必须过 Ed25519 验签（`src/update/service.py:302-322`），验不过一律拒绝；生产自动更新当前关闭。
- 错误一律闭集状态码+人话文案，前端不从自由文本猜状态（`frontend/modules/api.js` 码表）。

## 术语表

| 术语 | 含义 |
| --- | --- |
| 本地后端 | 随客户端启动的 Python 服务，唯一持有复旦会话与数据库的进程 |
| Worker / Worker 镜像 | 在你的 GitHub 仓里执行生成任务的 Actions；「镜像」指其代码由 CI 签名发布、客户端只认签名版本 |
| Mailbox | 你的私有资料仓，与公共 Worker 仓配对的两个受管仓库之一 |
| 密封载荷（sealed） | 任务派发时加密打包的课程材料，密钥只在你自己的 Worker 环境密钥里 |
| 闭集状态码 | 后端只会返回一张固定错误码表里的代码；前端为每一码准备了人话文案 |
| 熔断 | 自动学习材料的按日用量保护（讲次/runner 分钟/token 三上限），触顶后延后而不是失控重跑 |
| DPAPI | Windows 提供的按用户数据加密；密文只有当前 Windows 用户能解 |
