"""real 档测试身份凭据装载器（TESTBENCH-DESIGN-1 件②M3）。

把测试身份凭据文件 env（契约权威在 src/runtime/test_mode.py，本文件连
注释都不出现 env 字面量）指向的 Tier A 形态凭据**只读进内存一次**，交给
真正执行真 UIS 登录的驱动（真链自测/real 档车道经产品自身
``WebVPNClient.login`` 出站）。结构性零落盘：

- 本模块只读文件、只打印脱敏审计行；绝不写任何文件、绝不把凭据放进
  日志/异常/结果产物；
- 沙盒数据目录（测试模式强制独立）与产品 DPAPI 凭据信封互不相通——凭据
  在测试进程内生命周期即随进程结束，不进任何持久化路径；
- 接受形态：扁平 ``student_id|username|account|fudan_account + password``，
  或 ``{"credentials": {...同上...}}`` 包裹（工作区 real-bench 配置同形；
  包裹内无关字段一概不触达）。

安全边界：repr/str 全脱敏（前 2 字符+***，与 tests/testbench/github_identity
同约定）；错误消息只描述形态、永不回显字段值。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.runtime import test_mode as tm


def _redact(value: str) -> str:
    """脱敏（前 2 字符+***；与 github_identity.redact 同约定）。"""
    return value[:2] + "***" if value and len(value) > 2 else "***"


@dataclass(frozen=True)
class TestCredentials:
    """一组测试身份凭据（内存形态；repr/str 恒脱敏）。"""

    student_id: str
    password: str
    source_file: str

    def __repr__(self) -> str:  # pragma: no cover - 钉测直接断言形态
        return (
            f"TestCredentials(student_id={_redact(self.student_id)}, "
            "password=***, source_file=...)"
        )

    def __str__(self) -> str:  # pragma: no cover - 同上
        return self.__repr__()


def load_test_credentials(environ=None) -> TestCredentials:
    """读凭据文件进内存（一次性；schema fail-closed，脱敏审计行）。"""
    resolved = tm.resolve_test_credentials_linkage(environ)
    if not resolved:
        raise tm.TestModeContractError(tm.TEST_CREDENTIALS_FILE_MISSING)
    try:
        raw = json.loads(Path(resolved).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise tm.TestModeContractError(tm.TEST_CREDENTIALS_FILE_INVALID) from exc
    if not isinstance(raw, dict):
        raise tm.TestModeContractError(tm.TEST_CREDENTIALS_FILE_INVALID)
    wrapper = raw.get("credentials")
    payload = wrapper if isinstance(wrapper, dict) else raw
    student_id = ""
    for key in ("student_id", "username", "account", "fudan_account"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            student_id = value.strip()
            break
    password = payload.get("password") or payload.get("fudan_password")
    if not student_id or not isinstance(password, str) or not password:
        raise tm.TestModeContractError(tm.TEST_CREDENTIALS_FILE_INVALID)
    print(
        "[test-credentials] 已从凭据文件装载测试身份（仅内存，零落盘；"
        f"student_id={_redact(student_id)}）",
        file=sys.stderr,
        flush=True,
    )
    return TestCredentials(
        student_id=student_id, password=password, source_file=resolved
    )


__all__ = ["TestCredentials", "load_test_credentials", "_redact"]
