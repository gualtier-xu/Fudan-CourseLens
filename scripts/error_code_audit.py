#!/usr/bin/env python
"""错误码前后端等集对账（NIGHT5-U11，只读）。

用法：python scripts/error_code_audit.py
- 前端文案闭集：frontend/modules/api.js 顶层映射的 `code: "中文"` 键。
- 后端可达码：src/ 中 error_code="..." 字面量 + worker 云任务闭集码
  （cloud_automation.py 的 code = "..." 赋值字面量）。
- 输出：后端出现而前端无文案的码（学生可见面缺口）与各自计数；有缺口时退出码 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COPY_RE = re.compile(r'^\s{2}([a-z][a-z0-9_]+):\s+"', re.MULTILINE)
ERROR_CODE_RE = re.compile(r'"error_code":\s*"([a-z][a-z0-9_]+)"')
WORKER_CODE_RE = re.compile(r'^\s*code = "([a-z][a-z0-9_]+)"', re.MULTILINE)


def frontend_copy_keys() -> set[str]:
    text = (ROOT / "frontend" / "modules" / "api.js").read_text(encoding="utf-8")
    return set(COPY_RE.findall(text))


def backend_codes() -> set[str]:
    codes: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        codes.update(ERROR_CODE_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    worker_auto = ROOT / "worker" / "courselens_worker" / "cloud_automation.py"
    if worker_auto.exists():
        codes.update(WORKER_CODE_RE.findall(worker_auto.read_text(encoding="utf-8", errors="ignore")))
    return codes


# 机面豁免闭集：请求校验族（*_invalid/*_required）与自动化内部熔断族
# （deepseek_*/cloud_*，由自动化 UI 专门映射）不要求 api.js 中文文案；
# 除此之外的后端码必须有人话文案（fail 的即真缺口）。
_EXEMPT_SUFFIXES = ("_invalid", "_required", "_action_invalid")
_EXEMPT_PREFIXES = ("deepseek_", "cloud_")
_EXEMPT_EXACT = frozenset({
    "budget_exhausted", "cloud_processing_failed", "cloud_daily_completed",
    "platform_session_failed", "assessment_action_invalid",
})


def main() -> int:
    copy = frontend_copy_keys()
    codes = backend_codes()
    def exempt(code: str) -> bool:
        return (
            code in _EXEMPT_EXACT
            or any(code.endswith(suffix) for suffix in _EXEMPT_SUFFIXES)
            or any(code.startswith(prefix) for prefix in _EXEMPT_PREFIXES)
        )

    missing = sorted(
        code for code in codes
        if code not in copy and not code.startswith("courselens") and not exempt(code)
    )
    print(f"frontend copy keys: {len(copy)}; backend codes: {len(codes)}; missing copy: {len(missing)}")
    for code in missing:
        print(f"  MISSING-COPY {code}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
