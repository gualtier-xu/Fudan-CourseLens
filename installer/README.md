# installer/ — CourseLens 桌面端安装外壳（per-user）

本目录是 PKG-MIGRATION-1（卡⑦B / 卡③ / 3.12 迁移）落地的 Inno Setup 安装外壳。
设计出处：N5K 打包调研档 §38.1（iss 骨架）、§39.2（运行实例探测）、§39.4（构建脚本）、
§38.5（卸载数据询问页）；安装根名按卡③钉死：

- 程序负载根：`{localappdata}\Programs\CourseLens`（per-user，免 UAC）
- 受管数据根：`{localappdata}\CourseLens`（`launcher/versions/state/trust/data` 五目录；
  **卸载默认保留**，只有卸载询问页中学生亲口选「否」才删除）

## 构建三分钟

1. 安装 [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php)。中文界面无需再装
   语言包：官方安装器其实**不随发简中**，本目录已自带 `ChineseSimplified.isl`
   （取自 issrc 官方源码仓，.iss 按脚本相对路径引用），拿到仓库即拿到语言包。
2. 构建前确认工作树干净（或临时演练传 `-AllowDirty`）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1
   ```

3. 产物在 `output\installer\`：`CourseLens-<版本>-setup.exe` + 学生可读的
   `CourseLens-<版本>-校验单.txt`（SHA-256 核对指引，语气按产品文案规范）。

只做静态校验与暂存演练（不编译 exe）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 -ValidateOnly
```

## 本目录不做什么（红线）

- **不签名**：Authenticode 属发布域，走试验 runbook R1–R2
  （`scripts/sign_client_release.py`，须单独授权批次）。
- **不自动删数据**：`[UninstallDelete]` 刻意留空；删除仅存在于卸载询问页的
  显式选择之后。
- **不做 venv**：负载内运行时为 `tools/python312`（便携 Python + 就地安装的
  客户端依赖，见 `scripts/install_bundled_runtime.ps1`），没有任何需要
  机器重定位的 venv——中文用户名机与 ASCII 机安装路径完全一致（R-OP7）。

## 首次运行实例探测

安装启动时探测 `127.0.0.1:8765-8775` 与 `6268` 的 `/api/health`，发现运行中的
CourseLens 会先请学生退出再装（PascalScript 无原生 TCP，经 WinHttp COM 实现）。
