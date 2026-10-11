"""py2app 主脚本（bootstrap）：把解释器交给产品源码树内的 macOS 入口。

布局合同（MAC-NIGHT-1）：
- .app 内产品形态=忠实源码树（与 Windows 发行形态同构）：整个产品树
  （src/frontend/config/shared + 根模块）落在 ``Contents/Resources/courselens/``；
- py2app 只负责 CPython + 第三方依赖的捆绑，产品逻辑零编译、零收集
  （保留源码级泄漏扫描面，对齐既有信任模型）；
- ``Contents/MacOS/CourseLens``（py2app stub）启动本脚本 → 把
  ``Resources/courselens`` 插到 ``sys.path[0]`` → 导入同一 ``courselens_macos``
  入口（数据目录接线 + MAC-2 R1 下载放行 + ``src.app.main``）。

数据红线：学生数据根 = ``~/Library/Application Support/CourseLens``（入口
经平台层显式落 env），永不写进 .app 包内。
"""

from __future__ import annotations

import os
import sys


def _product_root() -> str:
    if getattr(sys, "frozen", False):
        # Contents/MacOS/<stub> → Contents/Resources/courselens
        contents = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        return os.path.join(contents, "Resources", "courselens")
    # 开发树直跑（未冻结）：packaging/macos/bootstrap.py → 仓根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    root = _product_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    import courselens_macos

    return courselens_macos.main(list(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
