"""测试身份目录清理脚本（测试台架件①M3 轮换治理；TB-W2）。

Tier B 会话态=等效凭据，整体删除目录即轮换（设计件①）；本脚本是这件事的
安全封装：**默认 --dry-run** 只打印将要删除什么，`--apply` 才真删。

边界（fail-closed，三层）：
1. 目标恒等于 ``<身份根>/<身份名>``：身份名过 :mod:`tests.testbench.github_identity`
   同一守卫（禁路径逃逸），根默认=工作区 ``.testbench/identity/``；
2. 结构性反目标守卫：解析后的目标路径任一级含 ``.local-secrets``、落在产品仓
   内、或落在实例注册表（``.testbench/instances``）内一律拒绝——清理脚本对
   Tier A 凭据本体（``.local-secrets/``）结构性不可达；
3. 删除动作只对「确实是目录且确实在身份根下」的路径执行（``shutil.rmtree``），
   不碰任何文件级花活。

用法（车道手工轮换/月度重建）::

    python tests/testbench/identity_cleanup.py github-co            # 干跑：打印将删什么
    python tests/testbench/identity_cleanup.py github-co --apply    # 真删（Tier A 不受影响）
    python tests/testbench/identity_cleanup.py --list               # 列出全部身份目录
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from tests.testbench import github_identity as gi
from tests.testbench.instances import product_repo_root

__all__ = [
    "CleanupPlan",
    "CleanupRefused",
    "apply_cleanup",
    "cleanup_plan",
    "list_identities",
    "main",
]

#: 结构性反目标：路径任一级出现这些名字即拒绝（Tier A 凭据本体/实例注册表）。
_FORBIDDEN_COMPONENTS = (".local-secrets", "instances")


class CleanupRefused(Exception):
    """清理被边界守卫拒绝（人话消息；不做任何删除）。"""


@dataclass(frozen=True)
class CleanupPlan:
    """一次清理的完整计划（干跑展示与实际执行共用同一份计算）。"""

    identity: str
    target_dir: Path
    exists: bool
    file_count: int
    total_bytes: int

    def describe(self) -> str:
        if not self.exists:
            return (
                f"身份 {self.identity!r} 没有可轮换的 Tier B 目录（{self.target_dir} 不存在）"
                "——无需清理，Tier A 凭据本体本来就不在这里。"
            )
        return (
            f"将轮换身份 {self.identity!r}：删除 Tier B 目录 {self.target_dir}"
            f"（{self.file_count} 个文件，{self.total_bytes} 字节，含 storageState=等效凭据）。"
            "Tier A 凭据本体（.local-secrets/）不在删除面内，不受影响；"
            "下次 ensure_session 会自动账密重登重建该目录。"
        )


def _count_dir(tree: Path) -> tuple[int, int]:
    files, total = 0, 0
    for path in tree.rglob("*"):
        if path.is_file():
            try:
                files += 1
                total += path.stat().st_size
            except OSError:
                continue
    return files, total


def _guard_target(target: Path) -> Path:
    """结构性反目标守卫：目标路径不安全即拒绝（在算计划之前先挡）。"""
    resolved = target.resolve()
    if any(part in _FORBIDDEN_COMPONENTS for part in resolved.parts):
        raise CleanupRefused(
            f"拒绝清理 {resolved}：路径含受保护目录级（.local-secrets=凭据本体 / "
            "instances=实例注册表）。清理脚本只认身份目录（.testbench/identity/<名字>），"
            "Tier A 凭据本体结构性不在它的删除面里。"
        )
    repo = product_repo_root().resolve()
    if resolved == repo or repo in resolved.parents:
        raise CleanupRefused(
            f"拒绝清理 {resolved}：目标在产品仓（{repo}）之内——身份目录只存在于"
            "工作区级 .testbench/identity/（工作区非 git 仓），产品仓内没有可清理的身份。"
        )
    return resolved


def cleanup_plan(identity_name: str, root: Path | None = None) -> CleanupPlan:
    """计算一次轮换清理的完整计划（不落任何删除动作）。"""
    target = gi.identity_dir(identity_name, root)  # 身份名守卫复用件①同一实现
    _guard_target(target)
    exists = target.is_dir()
    file_count, total_bytes = _count_dir(target) if exists else (0, 0)
    return CleanupPlan(
        identity=identity_name,
        target_dir=target,
        exists=exists,
        file_count=file_count,
        total_bytes=total_bytes,
    )


def apply_cleanup(identity_name: str, root: Path | None = None) -> CleanupPlan:
    """执行轮换：删除 Tier B 身份目录（计划守卫全过才动第一刀）。"""
    plan = cleanup_plan(identity_name, root)
    if plan.exists:
        shutil.rmtree(plan.target_dir)
    return plan


def list_identities(root: Path | None = None) -> list[CleanupPlan]:
    """列出身份根下全部身份目录的计划（--list 用；无秘密内容）。"""
    identity_root = Path(root) if root else gi.default_identity_root()
    if not identity_root.is_dir():
        return []
    return [
        cleanup_plan(child.name, identity_root)
        for child in sorted(identity_root.iterdir())
        if child.is_dir()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="测试身份 Tier B 目录清理（件①M3 轮换治理；默认干跑）"
    )
    parser.add_argument("identity", nargs="?", default=None, help="身份名（例 github-co）")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真删（缺省=干跑，只打印计划不动任何文件）",
    )
    parser.add_argument("--root", default=None, help="身份根目录（默认工作区 .testbench/identity）")
    parser.add_argument("--list", action="store_true", help="列出全部身份目录后退出")
    parser.add_argument("--json", action="store_true", help="--list 时机读输出")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else None

    if args.list:
        plans = list_identities(root)
        if args.json:
            import json

            print(
                json.dumps(
                    [
                        {
                            "identity": p.identity,
                            "target_dir": str(p.target_dir),
                            "exists": p.exists,
                            "file_count": p.file_count,
                            "total_bytes": p.total_bytes,
                        }
                        for p in plans
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if not plans:
                print("身份根下没有任何身份目录（Tier B 未曾落盘或已全部轮换）。")
            for p in plans:
                print(f"{p.identity:<24} files={p.file_count:<5} bytes={p.total_bytes}")
        return 0

    if args.identity is None:
        parser.error("需要身份名（例 github-co），或用 --list 查看现有身份")
        return 2  # 空串不放行到 argparse：让它走下方边界守卫拿人话拒绝。
    try:
        if args.apply:
            plan = apply_cleanup(args.identity, root)
            print(f"已轮换：{plan.describe()}")
        else:
            plan = cleanup_plan(args.identity, root)
            print(f"干跑（默认；加 --apply 才真删）：{plan.describe()}")
        return 0
    except gi.BoundaryViolationError as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 1
    except CleanupRefused as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
