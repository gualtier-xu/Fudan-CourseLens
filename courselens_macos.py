"""CourseLens macOS 入口（MAC-NIGHT-1）。

职责（全部发生在 ``src.app`` 导入之前，``src/app.py`` 零改动）：

1. 数据目录接线：无 ``COURSELENS_DATA_DIR`` 时以平台层默认根
   （``~/Library/Application Support/CourseLens``）显式落 env——数据红线：
   学生数据永不写进 .app 包内。开发树上两处最终同源（root ``path_utils``
   的 darwin 分支读同一平台层）。
2. 下载放行（MAC-2 R1，今晚必修钉）：pywebview 6.2.1 的
   ``ALLOW_DOWNLOADS`` 默认 False——不显式放行，课表 ICS 导出/资料导出/
   搬家包/字幕导出在 mac 原生窗内会全部静默失败。必须在 ``webview.start``
   前写入；``src.app`` 的既有 ``import webview`` 拿到同一模块对象。
   本地服务架构不变：窗口只承载同一 127.0.0.1 源，放行的是「用户点导出」
   这一本机下载面，不改变任何外联闭集。
3. 单实例互斥：``src.app.main`` 内建 ``InstanceLock``（双平台分支已就绪）
   与二次实例聚焦（``window_shell``→``src.platform.tray`` macOS 腿），
   入口零重复上锁。

托盘：mac v1 由 Dock 激活语义兜底；``src/platform/tray.py`` 的
NSStatusItem 层已就绪并可桩测，app.py 托盘链（今晚冻结）解冻后的接线
属晨间后续。本入口不强行挂托盘。

用法：开发树 ``python courselens_macos.py [--no-open] [--window|--no-window]``；
py2app 打包把它作为应用主脚本（见 packaging/macos/setup.py）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_data_dir() -> None:
    """显式落平台数据根 env（保持既有 COURSELENS_DATA_DIR 覆盖优先）。"""
    if os.environ.get("COURSELENS_DATA_DIR", "").strip():
        return
    try:
        from src.platform.paths import data_dir as _platform_data_dir

        os.environ["COURSELENS_DATA_DIR"] = str(_platform_data_dir())
    except Exception:
        # 平台层缺席的极端构建：path_utils 的 darwin 分支会再兜一次；
        # 两处都不可用的构建是坏安装，交给下游诚实报错。
        pass


def _allow_native_downloads() -> None:
    """MAC-2 R1：放行原生窗内下载（导出族功能的 macOS 生命线）。

    webview 缺席（未安装/打包裁剪）=诚实跳过——``src.app`` 的原生窗路径
    本就会降级浏览器，浏览器下载不经此设置。
    """
    try:
        import webview
    except Exception:
        return
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        # pywebview 未来改面时宁可带钉红测试也不要静默丢下载。
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_data_dir()
    _allow_native_downloads()
    from src.app import main as app_main

    return app_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
