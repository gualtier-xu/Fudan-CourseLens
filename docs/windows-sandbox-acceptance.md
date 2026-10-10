# Windows Sandbox 全新用户验收

本流程只用于无秘密、无历史数据的试用候选包验收。源码从当前 Git commit 导出，映射目录只读；沙箱报告目录单独可写。任何凭据输入期间禁止截图、trace、录像和控制台采集。

## 准备

1. 先提交所有代码和文档改动。
2. 以管理员 PowerShell 启用 Windows Sandbox：

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\windows-sandbox\Enable-CourseLensSandbox.ps1
Restart-Computer
```

3. 重启后生成并启动沙箱：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows-sandbox\New-CourseLensSandbox.ps1 -Launch
```

生成器只使用 `git archive HEAD`，不会包含 `.git`、`runtime`、`downloads`、本地环境、凭据或未提交文件。

完整重置包装脚本还会打开一个仅绑定 `127.0.0.1:8765` 的一次性页面，只删除 CourseLens 和旧版 CourseLens 前缀的界面偏好。它不读取值，也不会清空同源下不相关的数据。只有受管浏览器策略阻止打开本地页面时才使用 `-SkipBrowserState`，并必须在清理报告中记录这一例外。

## 验收顺序

- 确认 `C:\CourseLensReports\baseline.json` 中 Python、Node、Conda、FFmpeg 均不存在。
- 等待启动器下载固定 Python、创建轻量环境，并在健康检查成功后打开浏览器。
- 完成 GitHub Device Flow、Worker 修复、可信 tree 检查和加密 echo。
- 只在本地安全表单输入轮换后的凭据。
- 验证授权课程发现、在线播放、Range、拖动、倍速和首个五分钟 fast 任务。
- 确认结果验签导入、Artifact/Issue/token 清理和本地无原媒体。

沙箱关闭后内部 DPAPI、浏览器状态和运行时自动销毁。由于全新客户端会重新建立 Worker 密钥，主机必须在沙箱验收后重新授权并建立最终密钥。

## 2026-07-20 主机阻塞记录

- Windows Sandbox 可选功能已启用，系统已重启，`WindowsSandbox.exe` 存在。
- 只读验收包固定到私有 commit `80bb9e6aeb1b8af9b380a44285eced58a3d8855e`，包内无 `.git`、`runtime`、`downloads` 或凭据。
- `WindowsSandboxRemoteSession.exe` 在 CourseLens bootstrap 运行前崩溃；闭集错误码为 `windows_sandbox_winrt_runtime_missing`。
- 系统事件显示缺少 `WinRT.Runtime, Version=2.2.0.0`，尽管包目录中的 DLL 具有相同程序集版本；依赖清单中的 `Microsoft.Windows.CsWinRT/2.2.0` 目标项为空。
- 官方 AppX 重置后故障可重复，`winget` 当前无更高版本。不得修改受签名的 WindowsApps 文件，不自动转用 Hyper-V。
- 解除条件：Windows Update/Microsoft Store 提供修复后的 Sandbox 包，或在另一台支持 Windows Sandbox 的全新 Windows 主机运行同一包。

## 2026-07-24 复验

- 从私有 commit `ad9e974e6c584082a62da6a24f8ed325b7fc5a2d` 生成新的只读验收包。剪贴板、音视频输入、打印机重定向和源码映射写入均被禁用。
- 通过文件资源管理器使用已安装的 `C:\Windows\System32\WindowsSandbox.exe` 打开 `.wsb`。Sandbox 窗口未能存活，且没有生成 `baseline.json`，证明故障发生在 CourseLens bootstrap 执行前。
- Application Error 事件 `1000` 再次记录 `WindowsSandboxRemoteSession.exe` `0.8.107.0` 和异常 `0xe0434352`；.NET Runtime 事件 `1026` 再次记录缺少 `WinRT.Runtime, Version=2.2.0.0`。
- 后续只读依赖核验确认 `C:\Windows\System32\WindowsSandbox.exe` 版本为 `10.0.26100.8875`；Store 签名的 `MicrosoftWindows.WindowsSandbox` 包版本为 `0.8.107.0`，注册状态为 `Ok`。
- 包内 `WinRT.Runtime.dll` 的程序集全名确为 `WinRT.Runtime, Version=2.2.0.0, Culture=neutral, PublicKeyToken=99ea127f02d97709`，文件版本为 `2.2.0.48161`，且可由 .NET 反射直接加载。
- `WindowsSandboxRemoteSession.runtimeconfig.json` 目标为 .NET 9.0.18；其 `deps.json` 列出 `Microsoft.Windows.CsWinRT/2.2.0`，但 `win-x64` 目标项为空，没有声明 runtime asset。这把故障进一步限定为 Sandbox Store 包的依赖清单/加载上下文问题，而不是 CourseLens、可选功能状态或 DLL 实体缺失。
- 未修改 WindowsApps 签名文件或系统安全设置。本机 clean-Windows onboarding 仍被 Sandbox 系统包外部阻断；下一项允许的回退方案是使用官方 Windows ISO 的隔离 Hyper-V VM。

## 2026-09-05 后续决策

- 上文的“不自动转用 Hyper-V”是当时的决定，作为历史事实保留：它针对的是在未获授权情况下自动切换验收路径，而不是永久禁止 Hyper-V。
- 此后用户已单独批准以隔离 Hyper-V 虚拟机（官方 Windows ISO）作为干净机验收路径；宿主机与镜像准备细节记录在 [handoff](handoff.md)，不在此复制。
- 截至 2026-09-05，Hyper-V 干净机验收尚未完成，属于试用候选发布门禁，待试用打包工作恢复后执行。
- Windows Sandbox 阻塞（`windows_sandbox_winrt_runtime_missing`）本身未解除，仍应在 Windows/Store 更新后择机只读复检。
