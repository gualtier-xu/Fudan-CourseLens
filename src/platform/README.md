# src/platform — CourseLens 平台抽象层

一个目录收拢全部 OS 特定依赖，按 `sys.platform` 分发后端。目标：macOS 端
（以及未来其他平台）与 Windows 共用同一套业务形状，只换后端。

**纪律**：本层就绪可用，但**现调用点零改动**——`credentials.py`（根）、
`lifecycle.InstanceLock`、`scheduler_windows`、`tray_manager`、
`window_shell` 等既有模块保持原样；接线由后续 UI/托盘/打包车道完成。

## 后端矩阵

| 能力 | Windows 后端 | macOS 后端 | 模块 | CI 可测性 |
|---|---|---|---|---|
| 凭据封印 | `WindowsDPAPICredentialsBackend`（复用根 `credentials.py` 现役 DPAPI，`cp:v2:` 令牌字节不变） | `MacOSKeychainCredentialsBackend`（`security add/find/delete-generic-password`，令牌 `kc:v1:<handle>`，数据本体在 Keychain） | `credentials.py` | Win 真跑；macOS 桩 `subprocess_runner` |
| 数据目录 | `%LOCALAPPDATA%\CourseLens` | `~/Library/Application Support/CourseLens` | `paths.py` | 双平台可真跑（`COURSELENS_DATA_DIR` 覆盖优先，对齐根 `path_utils`） |
| 单实例互斥 | `WindowsMutexSingleInstanceLock`（`CreateMutexW` 命名互斥） | `MacOSFlockSingleInstanceLock`（锁文件 `flock(LOCK_EX\|LOCK_NB)`，flock 原语可注入） | `single_instance.py` | Win mutex 真跑；macOS 注入桩跑逻辑 |
| 每日任务/自启 | `WindowsSchtasksAutostartBackend`（委托既有 `src.runtime.scheduler_windows`，零复制） | `MacOSLaunchdAutostartBackend`（LaunchAgent plist + `launchctl load/unload`；`render_launchd_plist` 纯函数） | `autostart.py` | Win 委托可真跑；plist 内容/launchctl 桩可钉 |

未知平台一律 `PlatformNotSupportedError`（fail-closed，绝不静默降级）。

## 接线指引（后续车道）

1. **凭据**：根 `credentials.py` 的 `_protect`/`_unprotect` 是唯一封印边界。
   接线时把这两处改为经 `get_credentials_backend()` 分发（或先让新平台
   入口直接持有 backend 实例传入 `CredentialStore`）。注意：根模块顶部
   `from ctypes import wintypes` 在 macOS **导入即失败**，因此 macOS 进程
   不得 import 根模块；本层的 Windows 后端已做「先守卫后懒加载」。
2. **数据目录**：平台入口（app 启动最早期）用 `platform_paths()` 决定数据
   根；`COURSELENS_DATA_DIR` 覆盖语义与根 `path_utils` 一致，两处最终应
   合一（先接本层，再让 `path_utils` 读取本层结果）。
3. **单实例**：`lifecycle.InstanceLock` 的证据文件（server-instance.json、
   PID 重用检测）与 `app.py:1099` 的接线保持不变，只把「拿锁原语」换成
   `acquire_single_instance()`；`window_shell.focus_running_instance_window`
   在 macOS 需要等价实现（EnumWindows → NSWindow 列表，属托盘/UI 车道）。
4. **托盘/任务栏**：`tray_manager`（Shell_NotifyIcon）与 `window_shell`
   （ITaskbarList3/FlashWindowEx）是纯 Win32 实现，macOS 等价物
   （NSStatusItem / Dock badge / NSApp activate）属后续 UI 车道，建议落在
   本层新模块（如 `tray.py`），沿用本矩阵的分发骨架。
5. **自启**：`application.py:10722` 的每日同步开关改经
   `get_autostart_backend()`；macOS LaunchAgent 的 plist 落点为
   `~/Library/LaunchAgents/com.fudan.courselens.dailysync.plist`。

## 测试口径

- Windows 后端：Windows CI 真跑（DPAPI roundtrip、mutex 争用）。
- macOS 后端：无真机也验证逻辑——`subprocess_runner` / `flock_fn` /
  `base_dir` 注入桩钉 argv 形状、令牌格式、plist 内容与错误映射。
- 分发钉：`patch("sys.platform", ...)` 钉 `current_platform` 与三个
  `get_*_backend` 工厂；守卫钉：错误平台必须抛
  `PlatformNotSupportedError`。
