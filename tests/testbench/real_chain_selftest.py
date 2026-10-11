"""real 档真链自测（TESTBENCH-DESIGN-1 件②M3）：内存注入真 UIS 登录 + gh_login 组合。

夜里无人值守时验证「真实身份链」的最短真路：
  学校腿 = COURSELENS_TEST_CREDENTIALS_FILE（Tier A 形态 JSON）→ 内存装载
           （src/runtime/test_credentials.py，零落盘）→ 产品自身
           WebVPNClient.login() 七步真 UIS/WebVPN 认证（出站经
           school:readonly egress 白名单档，每跳可事后复核）。
  GitHub 腿 = tests/testbench/github_identity.ensure_session（既有 gh_login
           链：Tier A 账密 → Tier B storageState，失效自愈）。

用法（正典 venv，工作区根执行；凭据值永不进命令行/环境值/日志/结果——
命令行只出现 Tier A 文件路径）::

    COURSELENS_TEST_MODE=real \\
    COURSELENS_TEST_EGRESS_ALLOW=school:readonly,github:api \\
    COURSELENS_TEST_CREDENTIALS_FILE=<工作区>/.local-secrets/<测试身份>.json \\
    .venv-client-py310/Scripts/python.exe private/main/tests/testbench/real_chain_selftest.py \\
        [--legs school,github] [--owner-lane TB-W4]

语义：全部试图执行的腿 PASS → 退出 0；任一腿 FAIL → 退出 1（人话原因，
异常只报类型+闭集码，不回显任何凭据材料）；凭据缺位/依赖缺位 → 该腿 SKIP
（不失败，诚实上报）。CAPTCHA/2FA/审批挑战 = 腿 FAIL 即停，绝不代批、绝不重试。
本脚本零写盘（GitHub storageState 由既有身份链自管，不归本脚本）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import test_credentials as tc
from src.runtime import test_mode as tm


def _fail(message: str) -> None:
    print(f"[real-chain] FAIL {message}", file=sys.stderr, flush=True)


def _skip(message: str) -> None:
    print(f"[real-chain] SKIP {message}", flush=True)


def run_school_leg() -> str:
    """真 UIS 登录腿：PASS / SKIP / FAIL。"""
    if not tm.test_credentials_file():
        return "SKIP"
    try:
        credentials = tc.load_test_credentials()
    except tm.TestModeContractError as exc:
        _fail(f"school 凭据装载被契约拒绝：{exc.code}")
        return "FAIL"
    try:
        from src.api.webvpn import WebVPNSession

        client = WebVPNSession()
        client.login(credentials.student_id, credentials.password)
    except tm.EgressBlockedError as exc:
        _fail(f"school 登录被 egress 门拒绝（host={exc.host}）——检查 school:readonly 档")
        return "FAIL"
    except Exception as exc:  # noqa: BLE001 - 诚实失败：只报类型+闭集码，不回显细节
        code = getattr(exc, "code", "") or ""
        _fail(f"school 真登录链失败：{type(exc).__name__} {code}"
              "（若为 CAPTCHA/2FA/设备验证类挑战=暂停留待用户处理，不代批）")
        return "FAIL"
    cookie_count = len(getattr(getattr(client, "session", None), "cookies", {}) or {})
    print(
        f"[real-chain] school PASS：七步真 UIS/WebVPN 登录成功"
        f"（logged_in=True，会话 cookie {cookie_count} 枚——只报计数不报内容）",
        flush=True,
    )
    return "PASS"


def run_github_leg(owner_lane: str) -> str:
    """既有 gh_login 链腿：PASS / SKIP / FAIL。"""
    tiers: frozenset[str] = frozenset()
    try:
        tiers = tm.allowed_egress_tiers()
    except tm.TestModeContractError:
        pass
    if "github:api" not in tiers:
        _skip("github 腿需 COURSELENS_TEST_EGRESS_ALLOW 含 github:api（未在册）")
        return "SKIP"
    try:
        from tests.testbench import github_identity as gi

        state_path = gi.ensure_session(purpose="api", owner_lane=owner_lane)
    except gi.ChallengeEncountered as exc:  # type: ignore[attr-defined]
        _fail(f"github 链遇挑战即停（不代批）：{type(exc).__name__}")
        return "FAIL"
    except gi.OutboundUnavailable:  # type: ignore[attr-defined]
        _skip("github 外联不可达（诚实 SKIP，不断网重登）")
        return "SKIP"
    except Exception as exc:  # noqa: BLE001 - 同上：类型+闭集码，不回显
        code = getattr(exc, "code", "") or ""
        _fail(f"github 真链失败：{type(exc).__name__} {code}")
        return "FAIL"
    size = state_path.stat().st_size if Path(state_path).is_file() else 0
    print(
        f"[real-chain] github PASS：gh_login 链就绪（storageState {size} 字节，只报大小不报内容）",
        flush=True,
    )
    return "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="real 档真链自测（学校+GitHub）")
    parser.add_argument("--legs", default="school,github", help="要执行的腿（默认全跑）")
    parser.add_argument("--owner-lane", default="TB-W4-REALCHAIN", help="车道 ID（审计留痕）")
    args = parser.parse_args(argv)
    wanted = {leg.strip() for leg in args.legs.split(",") if leg.strip()}
    unknown = wanted - {"school", "github"}
    if unknown:
        print(f"[real-chain] 未知腿 {sorted(unknown)}（只允许 school/github）", file=sys.stderr)
        return 2
    mode = tm.resolve_test_mode()
    if mode != tm.TEST_MODE_REAL:
        print(
            "[real-chain] 本脚本只做真链验证：请设 COURSELENS_TEST_MODE=real"
            "（synthetic 档请用合成后端入口，真凭据在合成档结构性被拒）。",
            file=sys.stderr,
        )
        return 2
    try:
        tm.resolve_test_credentials_linkage()
    except tm.TestModeContractError as exc:
        if "school" in wanted:
            print(f"[FudanCourseLens] {exc.human_message}", file=sys.stderr)
            return 2
        print(
            f"[real-chain] 注意：凭据文件联动未通过（{exc.code}）；school 腿未请求，继续 github 腿。",
            flush=True,
        )
    print(f"[real-chain] 开始：legs={sorted(wanted)} owner_lane={args.owner_lane}", flush=True)
    results: dict[str, str] = {}
    if "school" in wanted:
        results["school"] = run_school_leg()
    if "github" in wanted:
        results["github"] = run_github_leg(args.owner_lane)
    failed = [leg for leg, status in results.items() if status == "FAIL"]
    attempted = [leg for leg, status in results.items() if status == "PASS"]
    print(
        f"[real-chain] 总结：PASS={sorted(attempted)} SKIP={sorted(leg for leg, s in results.items() if s == 'SKIP')}"
        f" FAIL={sorted(failed)}（零凭据明文出入本脚本）",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
