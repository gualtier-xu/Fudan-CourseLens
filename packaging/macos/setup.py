"""py2app 配方（MAC-NIGHT-1 测试构建）。

用法（CI 与本机一致，见 build_macos_app.sh）：
    COURSELENS_MACOS_STAGED_ROOT=<staged>/courselens \
        python packaging/macos/setup.py py2app

布局合同见同目录 bootstrap.py：py2app 捆绑解释器+依赖；产品树作为
``Resources/courselens`` 资源整体入包（零编译，源码级信任模型不变）。
第三方依赖用 ``packages`` 全目录拷贝（C 扩展不进 zip），清单与
requirements-macos.txt 一一对应；Windows 专属模块显式排除。
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from setuptools import setup

_HERE = Path(__file__).resolve().parent

# 构建时由 build_macos_app.sh 阶段化好的产品树（目录名恒为 courselens，
# py2app 原名拷入 Contents/Resources/courselens）。
STAGED_ROOT = os.environ.get("COURSELENS_MACOS_STAGED_ROOT", "")
if not STAGED_ROOT or not Path(STAGED_ROOT).is_dir():
    raise SystemExit(
        "COURSELENS_MACOS_STAGED_ROOT must point at the staged 'courselens' "
        "product tree (run packaging/macos/build_macos_app.sh instead of "
        "invoking setup.py directly)"
    )

# Windows 专属面：mac 包内不得出现（列表同时是防回归钉：出现即构建红）。
_WINDOWS_ONLY_EXCLUDES = [
    "pythonnet",
    "clr",
    "clr_loader",
    "msvcrt",
    "winreg",
    "win32api",
    "win32com",
    "win32ctypes",
    "win32gui",
    "win32process",
    "pywintypes",
    "pythoncom",
]

# 第三方依赖全目录拷贝（与 requirements-macos.txt 对应；pyobjc 由 pywebview
# 的 darwin 依赖标记带入）。源码树（src/shared/path_utils/credentials）
# 不在此列——它们随 staged 产品树整体作为资源入包。
_RUNTIME_PACKAGES = [
    "webview",
    "bottle",
    "psutil",
    "PIL",
    "requests",
    "urllib3",
    "certifi",
    "idna",
    "charset_normalizer",
    "markdown",
    "markdown_it",
    "mdurl",
    "pygments",
    "nacl",
    "Crypto",
    "rich",
    "zstandard",
    "cffi",
    "pycparser",
    "curl_cffi",
    "proxy_tools",
    "pypdf",
    "typing_extensions",
    "tzdata",
    "AppKit",
    "Foundation",
    "objc",
]

with open(_HERE / "Info.plist", "rb") as _plist_file:
    # py2app 现代版本只接受 dict 形态的 ``plist``（``plist_path`` 选项已
    # 移除——首轮 CI 实证）；dict 会合并进 py2app 生成的默认 Info.plist。
    PLIST = plistlib.load(_plist_file)

OPTIONS = {
    "py2app": {
        "argv_emulation": False,
        "packages": list(_RUNTIME_PACKAGES),
        "resources": [STAGED_ROOT],
        "excludes": list(_WINDOWS_ONLY_EXCLUDES),
        "plist": PLIST,
        "optimize": 1,
        "no_strip": True,
        # 图标：仓库现无 .icns（installer/ 为 .ico）。v1 用系统默认图标，
        # 已在结果文件显式申报待晨裁（一次性 iconutil 产出 .icns 后回填）。
    }
}

setup(
    name="CourseLens",
    # py2app 以「当前工作目录」解析 app 脚本路径（round-2 CI 实证：仓根
    # 直跑时相对名会指到仓根而不是本目录），绝对路径 immune to cwd。
    app=[str(_HERE / "bootstrap.py")],
    options=OPTIONS,
)
