"""Shared AST extraction of the product HTTP surface (TB-ADAPT-M1).

从 src/runtime/http_api.py 的 AST 提取两类面，供对账钉
（tests/test_http_service_completeness.py）与生成式路由探针矩阵
（tests/test_http_route_probe_matrix.py）共用：

1. ``service_chains()``——全部以 ``service`` 为根的属性链（``service.a.b``
   Attribute 形态 + ``getattr(service[.a...], "字面量")`` 形态）。这是
   http_api 对服务容器的**全部**引用面。
2. ``route_table()``——按 HTTP 方法分组的路由注册面（``_handle_api_v3_*``
   分发器内的 ``route ==``/``route in`` 分支 + do_* 入口的
   ``parsed.path ==``/``startswith``/``in`` 特判）。

纯 AST 读取，零产品行为改动；产品路由/服务面新增即自动出现在提取结果里，
对账与探针随之自动扩展（人肉对账退役的机制基础）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTTP_API_PATH = ROOT / "src" / "runtime" / "http_api.py"

# do_* 入口 / v3 分发器函数名 → HTTP 方法（路由归属面）。
_METHOD_DISPATCHERS: dict[str, str] = {
    "do_GET": "GET",
    "do_HEAD": "HEAD",
    "do_POST": "POST",
    "do_PUT": "PUT",
    "do_DELETE": "DELETE",
    "_handle_api_v3_get": "GET",
    "_handle_api_v3_post": "POST",
    "_handle_api_v3_put": "PUT",
    "_handle_api_v3_delete": "DELETE",
}


def _http_api_tree() -> ast.Module:
    return ast.parse(HTTP_API_PATH.read_text(encoding="utf-8"))


def _service_root_chain(node: ast.AST) -> list[str] | None:
    """若节点是 ``service.a.b`` Attribute 链或 ``getattr(<service 根链>, "x")``
    调用，返回属性名链（["a","b"] / ["a","x"]）；否则 None。"""
    if isinstance(node, ast.Name):
        if node.id == "service":
            return []
        return None
    if isinstance(node, ast.Attribute):
        base = _service_root_chain(node.value)
        if base is None:
            return None
        return [*base, node.attr]
    if isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            return None
        if len(node.args) < 2:
            return None
        base = _service_root_chain(node.args[0])
        if base is None:
            return None
        name_arg = node.args[1]
        if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
            return None
        return [*base, name_arg.value]
    return None


def service_chains(tree: ast.Module | None = None) -> set[tuple[str, ...]]:
    """http_api 全部 ``service.*`` 引用链（去重）。含 getattr 字面量形态。"""
    tree = tree or _http_api_tree()
    chains: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        chain = _service_root_chain(node)
        if chain:
            chains.add(tuple(chain))
    return chains


@dataclass(frozen=True)
class RouteEntry:
    method: str
    route: str
    # 注册形态：dispatch=v3 分发器分支 / path=do_* 入口特判 / prefix=前缀匹配
    kind: str
    # 源码行号（探针失败时的归因线索）。
    lineno: int


@dataclass
class RouteTable:
    entries: list[RouteEntry] = field(default_factory=list)

    def by_method(self, method: str) -> list[RouteEntry]:
        return [e for e in self.entries if e.method == method]

    def unique_routes(self, method: str) -> list[str]:
        return sorted({e.route for e in self.by_method(method)})


def _constant_strings(node: ast.AST) -> set[str]:
    """Constant 字符串集合（单值或 Set/Tuple/List 字面量）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
        return out
    return set()


def _routes_in_body(body: list[ast.stmt], method: str, entries: list[RouteEntry]) -> None:
    """提取一个函数体内的路由注册：

    - ``route == "a"`` / ``route in {"a", "b"}`` → kind=dispatch
    - ``parsed.path == "/api/..."`` / ``in {...}`` → kind=path
    - ``route.startswith("a")`` / ``parsed.path.startswith("a")`` → kind=prefix
    """
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Compare):
            left = node.left
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.In)):
                    rights = _constant_strings(comparator)
                    if isinstance(left, ast.Name) and left.id == "route":
                        entries.extend(
                            RouteEntry(method, r, "dispatch", node.lineno) for r in rights
                        )
                    elif isinstance(left, ast.Attribute) and left.attr == "path":
                        entries.extend(
                            RouteEntry(method, r, "path", node.lineno) for r in rights
                        )
        elif isinstance(node, ast.Call):
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and len(node.args) == 1
            ):
                continue
            rights = _constant_strings(node.args[0])
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == "route":
                entries.extend(
                    RouteEntry(method, r, "prefix", node.lineno) for r in rights
                )
            elif isinstance(target, ast.Attribute) and target.attr == "path":
                entries.extend(
                    RouteEntry(method, r, "prefix", node.lineno) for r in rights
                )


def route_table(tree: ast.Module | None = None) -> RouteTable:
    """按 HTTP 方法提取 http_api 的路由注册面。"""
    tree = tree or _http_api_tree()
    table = RouteTable()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method = _METHOD_DISPATCHERS.get(node.name)
        if method is None:
            continue
        _routes_in_body(node.body, method, table.entries)
    return table
