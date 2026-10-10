"""适配层完备性契约·AST 静态对账钉（TB-ADAPT-M1，TESTBENCH-DESIGN-1 件④M1）。

终结「合成壳适配层缺透传 → 合成壳假 500」家族（USEROPS/FUZZ/WAIT-MEASURE
反复命中；TESTBENCH-DESIGN-1 实测 5 处 live 缺口含 client_update.complete_restart）：

1. **生成式正向钉**：AST 提取 src/runtime/http_api.py 全部 ``service.*`` 引用链
   （Attribute 形态 + getattr 字面量形态），断言 tests/http_services.py 适配层
   产物对每条链逐级 getattr 可解析且非 None。产品路由/服务面新增引用即自动
   进钉，人肉白名单对账退役——红信息直接给缺名。
2. **死字段反向钉**：适配层显式字段表里不被 http_api 任何链引用、也不在具名
   内部消费档案的条目=红（防白名单腐化）。
3. **永久变异自检**：文本级删除适配层显式条目 → 正向钉必须红且指名
   （钉子自身失效=红）——防「钉子悄悄失效」的自我回归。

既有先例 tests/test_service_facade_wiring.py（SWEEPFIX 对账钉形态）保留；
本钉把其手工钉点推广为全量生成面。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from tests._http_surface_ast import ROOT, service_chains
from tests.http_services import http_services


class _Anything:
    """万能解析对象：任何属性访问/调用返回自身。对账钉只做属性解析、不调用，
    因此源替身只需「任何名字都解析到非 None」。"""

    def __getattr__(self, name: str) -> "_Anything":
        return _ANYTHING

    def __call__(self, *args, **kwargs) -> "_Anything":
        return _ANYTHING


_ANYTHING = _Anything()


# 合法缺省档案（闭集+具名理由，禁无名扩容）：http_api 对这些链自带 None 守卫，
# 适配层不提供属设计意图——
# - resources：媒体流资源回收管理器（_stream_remote_media，http_api 内
#   getattr(service, "resources", None) 守卫，None → nullcontext 兜底）。
LEGAL_MISSING: frozenset[str] = frozenset({
    "resources",
})

# 反向钉内部消费档案（具名+理由，禁无名扩容）：适配层显式字段、http_api 无
# service.* 链引用、但属命名空间完整性契约保留——
# - (lifecycle, accepting_requests)：http_api 经 getattr(<lifecycle 局部>,
#   "accepting_requests") 守卫消费（_lifecycle_allows_request，http_api:1463），
#   局部变量形态非 service 根链，AST 捕捉不到，档案钉住；
# - (live_room, revoke_all)：与产品装配内部消费档案对齐（app.py 关闭路径
#   services.live_room.revoke_all()，domains.LiveRoomDomainService.revoke_all），
#   http_api 不引用但壳内关闭路径直通保留。
INTERNAL_CONSUMED: frozenset[tuple[str, str]] = frozenset({
    ("lifecycle", "accepting_requests"),
    ("live_room", "revoke_all"),
})


def _adapted_with_anything_source() -> SimpleNamespace:
    return http_services(_Anything())


def _broken_chains(adapt: object = None) -> list[str]:
    """返回适配层解析失败的 service.* 链（点串形态），全绿=[]。"""
    adapted = adapt if adapt is not None else _adapted_with_anything_source()
    broken: list[str] = []
    for chain in sorted(service_chains()):
        dotted = ".".join(chain)
        if dotted in LEGAL_MISSING:
            continue
        node: object = adapted
        for depth, name in enumerate(chain):
            node = getattr(node, name, None)
            if node is None:
                broken.append(f"{dotted}（第 {depth + 1} 级 .{name} 缺失）")
                break
    return broken


def _adapter_field_table(adapted: SimpleNamespace) -> list[tuple[str, str]]:
    """适配层显式字段表 [(命名空间, 字段)]（顶层字段命名空间位记 ""）。"""
    table: list[tuple[str, str]] = []
    for name, value in vars(adapted).items():
        if isinstance(value, SimpleNamespace):
            for child in vars(value):
                table.append((name, child))
        else:
            table.append(("", name))
    return table


def _dead_adapter_fields(adapted: SimpleNamespace) -> list[str]:
    """适配层显式字段中不被 http_api 引用且不在内部消费档案的条目。"""
    chains = service_chains()
    referenced_pairs = {chain[:2] for chain in chains if len(chain) >= 2}
    referenced_toplevel = {
        chain[0] for chain in chains
    }  # 任何以该名开头的链（含命名空间链）都算引用。
    dead: list[str] = []
    for ns, field_name in _adapter_field_table(adapted):
        if ns == "":
            if field_name not in referenced_toplevel:
                dead.append(f"顶层 {field_name}")
        elif (ns, field_name) not in referenced_pairs and (ns, field_name) not in INTERNAL_CONSUMED:
            dead.append(f"{ns}.{field_name}")
    return dead


def _mutated_adapter(mutation: str, removal: str) -> object:
    """文本级删除适配层一条显式条目后重载模块（永久变异自检的破坏算子）。"""
    source_path = Path(__file__).resolve().parent / "http_services.py"
    text = source_path.read_text(encoding="utf-8")
    count = text.count(removal)
    assert count == 1, f"变异算子 {mutation} 目标行不唯一（{count} 处），算子失效"
    namespace: dict[str, object] = {"__name__": "tests._http_services_mutated"}
    exec(compile(text.replace(removal, "", 1), str(source_path), "exec"), namespace)
    return namespace["http_services"](_Anything())


class HttpServiceCompletenessTests(unittest.TestCase):
    def test_every_http_api_service_chain_resolves_through_adapter(self):
        """生成式正向钉：http_api 全部 service.* 引用链必须经适配层逐级解析到
        非 None（合法缺省档案除外）。缺透传=合成壳对应路由恒 500 假 500 家族，
        从此 CI 即红且红信息直接给缺名——TESTBENCH-DESIGN-1 实测 5 处 live 缺口
        （onboarding_guide_snapshot/action、complete_restart、lifecycle.*、
        timetable.runtime）即本钉的首批战果，修适配后转绿。"""
        broken = _broken_chains()
        self.assertEqual(
            broken,
            [],
            "http_api 引用了适配层解析不到的 service.* 链（缺透传=合成壳假 500 家族）: "
            + ", ".join(broken),
        )
        # 普查锚：被对账的引用面不缩水（今日 152 条链）。
        total = len(service_chains())
        self.assertGreaterEqual(total, 140, "http_api service.* 引用面骤减，AST 提取可能失效")
        # 具名防回归锚：5 处实测缺口的修复必须逐条在位（生成扫已覆盖，此处点名）。
        adapted = _adapted_with_anything_source()
        for chain in (
            ("auth_catalog", "onboarding_guide_snapshot"),
            ("auth_catalog", "onboarding_guide_action"),
            ("client_update", "complete_restart"),
            ("lifecycle", "request_shutdown"),
            ("lifecycle", "snapshot"),
            ("timetable", "runtime"),
        ):
            node: object = adapted
            for name in chain:
                node = getattr(node, name, None)
            self.assertIsNotNone(node, f"{'.'.join(chain)} 透传回潮（实测缺口家族）")

    def test_adapter_has_no_dead_fields_beyond_named_archive(self):
        """死字段反向钉：适配层显式字段表里不被 http_api 任何链引用、也不在
        具名内部消费档案的条目=白名单腐化，红指名。TB-ADAPT-M1 首批清理：
        media_session.catalog / automation.repository / automation.integration /
        settings.credentials（仓内零引用实读后删除）。"""
        dead = _dead_adapter_fields(_adapted_with_anything_source())
        self.assertEqual(
            dead,
            [],
            "适配层死字段（http_api 不引用且不在内部消费档案=白名单腐化）: "
            + ", ".join(dead),
        )

    def test_pin_self_mutation_still_red(self):
        """永久变异自检：故意删适配层显式条目 → 正向钉必须红且指名缺链
        （钉子自身失效=红）。三处变异=本轮实测缺口中抽样的三种形态：
        _op 透传 / lambda stub / getattr 命名空间位。"""
        for mutation, removal, expected in (
            (
                "onboarding_guide_snapshot",
                'onboarding_guide_snapshot=_op(source, "onboarding_guide_snapshot"),',
                "auth_catalog.onboarding_guide_snapshot",
            ),
            (
                "complete_restart",
                "complete_restart=lambda: None,",
                "client_update.complete_restart",
            ),
            (
                "timetable.runtime",
                'runtime=getattr(source, "timetable", None),',
                "timetable.runtime",
            ),
        ):
            with self.subTest(mutation=mutation):
                broken = _broken_chains(_mutated_adapter(mutation, removal))
                self.assertTrue(
                    any(entry.startswith(expected) for entry in broken),
                    f"变异 {mutation} 后钉未红或未指名（broken={broken}）——钉子失效",
                )


if __name__ == "__main__":
    unittest.main()
