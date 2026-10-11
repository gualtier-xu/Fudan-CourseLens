"""container 反向对账钉（测试台架批 4·件④M3，TESTBENCH-DESIGN-1 §4.2 机制 3）。

适配层（tests/http_services.py）的输出形状必须与真实 CourseLensServices 容器
逐字段对齐：真实容器走 isinstance 早退（http_api 直读真容器），适配层缺键/
拼错键只在合成壳显形——本钉让两边同时受钉。两向覆盖：

1. **命名空间字段对账**：适配层产出的每个命名空间键，必须真实存在于真容器
   对应命名空间对象上（含硬编码 stub：合成壳不重启≠真容器没有
   complete_restart）。source 派生值与硬编码 stub 一并受钉。
2. **顶层键=容器字段闭集**：适配层顶层键集合必须逐名等于容器 dataclass 字段
   集合（防两端任何一方漂移）。

豁免闭集（每条带理由，新增必须在此显式申报）：
- auth_catalog.connection_snapshot：http_api 以 getattr 守卫读取并诚实回退
  （窄桩形态专用；真容器的连接真相在 remote_compute.connection_snapshot）。

**改 src/runtime/http_api.py（合同/路由面）的定向集必含本文件**——新增
service.* 引用经 tests/test_http_service_completeness.py（AST 前向钉）指名后，
白名单行的落点由本钉对回真容器防拼写漂移。两钉合并口径=「改 http_api 必跑」。

读取纪律（A3）+上下文预算（A2）适用；预计请求数 ≈10（容器装配约 2s）。
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services import CourseLensServices
from tests.http_services import http_services

#: 豁免闭集：(命名空间路径, 键) → 理由。http_api 以守卫读或真容器按设计
#: 不载的适配层专用键，全部在此显式申报；出现新漂移先来此对账，不许静默。
EXEMPT_FIELDS: dict[tuple[str, ...], str] = {
    ("auth_catalog", "connection_snapshot"): (
        "http_api:1885 以 getattr 守卫读取+诚实回退（窄桩形态专用）；"
        "真容器的连接快照承载位=remote_compute.connection_snapshot。"
    ),
}


class _Probe:
    """source 派生哨兵：记录适配层触达的 source 属性名（可调用以覆盖透传）。"""

    def __init__(self, source_attr: str) -> None:
        self.source_attr = source_attr

    def __call__(self, *args, **kwargs):  # 适配层可能包一层 lambda 后仍可调用
        return self

    def __repr__(self) -> str:  # pragma: no cover
        return f"Probe(source={self.source_attr})"


class _RecordingSource:
    """探针 source：任何属性触达都返回哨兵（适配层拿不到真值，只留形状）。"""

    def __getattr__(self, name: str) -> _Probe:
        return _Probe(name)


def _is_exempt(path: tuple[str, ...]) -> bool:
    return path in EXEMPT_FIELDS


def reconcile(adapter_ns: SimpleNamespace, container_obj: object) -> list[str]:
    """适配层形状对账真容器：返回缺名清单（路径形如 auth_catalog.xxx）。

    纯函数：真容器与任意替身皆可传入（改名场景红测试指名即由此保证）。
    """
    offenders: list[str] = []
    for top_key, top_value in vars(adapter_ns).items():
        if top_key.startswith("_"):
            continue
        real_top = getattr(container_obj, top_key, None)
        if isinstance(top_value, SimpleNamespace):
            if real_top is None:
                offenders.append(f"{top_key}（整个命名空间在真容器缺失）")
                continue
            for sub_key in vars(top_value):
                if sub_key.startswith("_"):
                    continue
                if not hasattr(real_top, sub_key):
                    path = (top_key, sub_key)
                    if not _is_exempt(path):
                        offenders.append(f"{top_key}.{sub_key}")
        elif real_top is None and not _is_exempt((top_key,)):
            offenders.append(f"（顶层）{top_key}")
    return offenders


class TestAdapterMatchesRealContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import src.app as app_module

        cls._app_module = app_module
        cls._sandbox = tempfile.mkdtemp(dir=str(ROOT / "runtime" / "cache"), prefix="tbw4-recon-")
        cls.container = app_module.compose_services(cls._sandbox)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.container.close()
        finally:
            # compose_services 会登记进程级 client_update 全局；测试卫生归零，
            # 不让本钉的容器泄漏进同进程的其他钉。
            cls._app_module._active_client_update = None

    def test_adapter_keys_equal_container_fields(self) -> None:
        """适配层顶层键集合 = 真容器 dataclass 字段集合（逐名相等，双向漂移即红）。"""
        adapter = http_services(_RecordingSource())
        adapter_keys = {k for k in vars(adapter) if not k.startswith("_")}
        container_fields = {f.name for f in dataclasses.fields(CourseLensServices)}
        self.assertEqual(
            adapter_keys,
            container_fields,
            "适配层顶层键与真容器字段漂移（左多=适配层私加，右多=容器新增未透传）",
        )

    def test_every_adapter_field_exists_on_real_container(self) -> None:
        """命名空间逐字段对账：缺名即红并指名（豁免闭集除外）。"""
        adapter = http_services(_RecordingSource())
        offenders = reconcile(adapter, self.container)
        self.assertEqual(
            offenders,
            [],
            "适配层引用的键在真容器缺失（拼写漂移/新增未透传——按缺名补适配层"
            "或在 EXEMPT_FIELDS 显式申报豁免理由）",
        )

    def test_rename_scenario_red_names_offender(self) -> None:
        """验收判据：容器字段改名场景红测试指名（用替身容器模拟 learning 改名）。"""
        adapter = http_services(_RecordingSource())
        chipped = SimpleNamespace(
            learning=SimpleNamespace(store_renamed=object()),  # repository → store_renamed
        )
        offenders = reconcile(adapter, chipped)
        self.assertTrue(any("learning.repository" in item for item in offenders),
            f"改名场景必须指名缺失键：{offenders[:5]}")

    def test_exempt_closed_set_documented(self) -> None:
        """豁免闭集必须逐条带理由（防豁免腐化）。"""
        for path, reason in EXEMPT_FIELDS.items():
            self.assertTrue(reason.strip(), f"豁免 {path} 必须写明理由")
        self.assertIn(("auth_catalog", "connection_snapshot"), EXEMPT_FIELDS)


if __name__ == "__main__":
    unittest.main()
