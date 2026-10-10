# CourseLens 发布页部署说明

本页是一个单文件 GitHub Pages 发布页，部署形态固定为 `main` 分支的 `/docs` 目录。页面不依赖 CDN、外部脚本、外部字体或远程图片，截图使用 `docs/assets/readme/` 内与本仓库 README 同源的四张 PNG，`docs/index.html` 可直接作为静态入口部署。

## 发布前回填（GO 包执行）

`docs/index.html` 中恰有两处带 `GO-BACKFILL` 注释标记的占位：

1. `<!--GO-BACKFILL:DOWNLOAD-URL-->`：下一行的下载按钮 `href` 当前指向仓库 Releases 页，发布时替换为正式安装包（`CourseLens-0.1.0-setup.exe`）的直链。
2. `<!--GO-BACKFILL:SHA256-->`：下一行 `<code>` 内的占位文字替换为正式安装包的 SHA256 校验值。

回填后 grep 确认 `GO-BACKFILL` 注释已消除（或按发布流程约定保留标记亦可，以发布单为准）。

## GitHub Pages 设置

1. 打开仓库 **Settings → Pages**。
2. 在 **Build and deployment** 中选择 **Deploy from a branch**。
3. 分支选择 `main`，目录选择 `/docs`。
4. 保存设置，等待 GitHub Pages 完成构建。
5. 打开 GitHub Pages 提供的站点地址，检查首屏渲染、三视口（375 / 768 / 1440）无横向滚动、页内锚点跳转、外链可达与 SHA256 回填结果。

## 本地预览

在仓库根目录运行任意静态文件服务器：

```powershell
python -m http.server 4173 --directory docs
```

访问 `http://127.0.0.1:4173/`。设计语言与客户端 `frontend/styles/tokens.css` 同源（恒暗舞台色族 + 三轨字体）；致谢名单终版由作者在 `docs/index.html` 的致谢节直接增删。
