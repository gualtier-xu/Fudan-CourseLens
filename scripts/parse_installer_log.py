"""CourseLens 安装日志解析器：从 Inno Setup /LOG 输出提取装机事实。

出处：N9-A/A2 夜批（2026-09-25）。回答四个问题：装了多少文件、带了多少
__pycache__ 死重、装后脚本各自退出码是多少、有没有真错误（区别于
error.py 这类文件名命中）。

用法：
    python scripts/parse_installer_log.py <setup.log> [more.log ...]

已知口径：Inno 日志行以 `<时间戳>   消息` 形态出现，正则一律子串搜索，
不锚定行首。"Dest filename" 行数=安装文件数（重装日志同口径）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEST = re.compile(r"Dest filename: (.+?)\s*$")
EXIT = re.compile(r"Process exit code: (\S+)")
NEED_RESTART = re.compile(r"Need to restart Windows\? (\S+)")
HARD_ERROR = re.compile(r"^\s*(Error|Could not|Failed)\b")


def parse(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    dest_files = [m.group(1) for line in lines if (m := DEST.search(line))]
    exits = [m.group(1) for line in lines if (m := EXIT.search(line))]
    restart = next((m.group(1) for line in lines if (m := NEED_RESTART.search(line))), "?")
    hard_errors = [line.strip() for line in lines if HARD_ERROR.match(line)]
    pyc = sum(1 for f in dest_files if "__pycache__" in f or f.endswith((".pyc", ".pyo")))
    return {
        "log": path.name,
        "dest_files": len(dest_files),
        "pyc_deadweight": pyc,
        "run_exits": exits,
        "need_restart": restart,
        "hard_errors": hard_errors,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    exit_code = 0
    for arg in args:
        info = parse(Path(arg))
        print(info)
        if info["hard_errors"]:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
