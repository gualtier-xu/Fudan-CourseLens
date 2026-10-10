"""GO 门禁内的有界目录授权探针 runner（本脚本不得在未获 fresh GO 前运行）。

合同：docs/catalog-session-recovery-handoff.md §4C/§8。真实判别实验：复用已验证
WebVPN 会话，对月度目录索引做一次**不带 Authorization** 的只读请求；索引成功再对
至多一个详情做同类验证，并用 infosimple 实时确认账号身份一致。

请求预算（硬上限）：1 次登录（webvpn + iCourse CAS 各一套腿，max_attempts=1，
失败即停）+ 2 次身份 + 1 次索引 + 至多 1 次详情。绝不触发生产目录刷新
（refresh_authorized_catalog_async）、媒体、任务派发或任何 GitHub/远程操作。

凭据仅内存：--credentials-helper 指向仓库外既有 DPAPI/session driver（以
``python <PATH> --emit-credentials`` 执行，stdout 恰好两行：学号、密码），或由
用户本人以 --credentials-stdin 在自己的终端输入两行。脚本绝不回显、记录或持久化
凭据。stdout 为既有登录路径的进度行（闭集文本，无任何值）加最后一行闭集 JSON
（布尔/类别/有界计数）；解析结果时只读最后一行 JSON。

用法（仅在取得本任务内 fresh 用户 GO + risk gate 通过后）：
    python -m scripts.catalog_session_probe --go \
        --operation-id <fresh-id> \
        --credentials-helper "C:/Users/<user>/.courselens-secrets/<driver>.py"

退出码：0 = 探针完成（结果见 JSON 的 stopped/各节）；3 = 有界失败（fail closed）；
2 = 用法/门禁拒绝。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# 闭集失败类别：只按异常类型/既有 code 属性归桶，绝不携带消息文本。
_CLOSED_FAILURE_KINDS = frozenset({
    "fudan_credentials_rejected",
    "fudan_credentials_missing",
    "fudan_login_failed",
    "webvpn_ticket_transport_failed",
    "icourse_ticket_transport_failed",
    "timeout",
    "network_unavailable",
    "probe_transport_failed",
    "probe_unavailable",
    "usage_rejected",
})


def _failure_kind(exc: BaseException, phase: str) -> str:
    explicit = str(getattr(exc, "code", "") or "")
    if explicit in _CLOSED_FAILURE_KINDS:
        return explicit
    from requests import RequestException, Timeout

    if isinstance(exc, Timeout):
        return "timeout"
    if isinstance(exc, RequestException):
        return "network_unavailable"
    return "fudan_login_failed" if phase == "login" else "probe_unavailable"


def _load_credentials_helper(helper_path: str) -> tuple[str, str]:
    completed = subprocess.run(
        [sys.executable, helper_path, "--emit-credentials"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if completed.returncode != 0 or len(lines) != 2 or not all(lines):
        raise SystemExit(3)
    return lines[0], lines[1]


def _load_credentials_stdin() -> tuple[str, str]:
    lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    if len(lines) != 2 or not all(lines):
        raise SystemExit(3)
    return lines[0], lines[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--go", action="store_true",
                        help="显式确认已取得本任务内 fresh 用户 GO + risk gate")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--month", default="")
    parser.add_argument("--network-mode", choices=("auto", "direct", "manual"),
                        default="auto",
                        help="覆盖本次运行的网络候选（只写 temp store，不影响真实配置）")
    parser.add_argument("--credentials-helper", default="")
    parser.add_argument("--credentials-stdin", action="store_true")
    args = parser.parse_args(argv)

    def bail(kind: str) -> int:
        print(json.dumps({"stopped": kind}, sort_keys=True, ensure_ascii=True))
        return 3

    if not args.go:
        return bail("usage_rejected")
    if not OPERATION_ID_RE.fullmatch(args.operation_id):
        return bail("usage_rejected")
    if args.month and not MONTH_RE.fullmatch(args.month):
        return bail("usage_rejected")
    if bool(args.credentials_helper) == args.credentials_stdin:
        return bail("usage_rejected")

    try:
        if args.credentials_helper:
            student_id, password = _load_credentials_helper(args.credentials_helper)
        else:
            student_id, password = _load_credentials_stdin()
    except SystemExit:
        return bail("usage_rejected")

    from src.application import CourseLensApplication

    cache_root = PROJECT_ROOT / "runtime" / "cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    exit_code = 0
    phase = "startup"
    with tempfile.TemporaryDirectory(
        dir=cache_root, prefix=f"catalog-probe-{args.operation_id}-"
    ) as temp_dir:
        service = None
        try:
            service = CourseLensApplication(Path(temp_dir))
            # 网络候选只写 temp store：现行 auto 模式校园服务默认直连优先，
            # 顺序只由带 TTL 的无凭据探测/路由健康记忆改变（network.py P0.2）；
            # 显式 direct 仍可钉死单一路径，消除代理路径的偶发超时变量。
            if args.network_mode != "auto":
                service.network.update(args.network_mode)
            # remember=False：凭据只存在于本进程内存，绝不写盘。
            service.set_credentials(student_id, password, remember=False)
            del student_id, password
            phase = "login"
            client = service._login_with_retry(max_attempts=1)
            phase = "probe"
            from src.api.catalog_probe import run_closed_catalog_probe

            result = run_closed_catalog_probe(client, month=args.month or None)
        except SystemExit:
            raise
        except Exception as exc:  # 闭集失败：只输出类别，不输出任何消息文本
            result = {"stopped": _failure_kind(exc, phase)}
            exit_code = 3
        finally:
            if service is not None:
                try:
                    service.close()
                except Exception:
                    pass

    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
