"""生成式路由探针矩阵（TB-ADAPT-M1，TESTBENCH-DESIGN-1 件④M2）。

把 tests/test_facade_route_wiring.py 的手工三钉升格为**全路由生成面**，
三层对账一次跑齐：

1. **路由在册**：AST 提取 http_api 的路由注册面（本文件锚定其不缩水）；
2. **handler 在位**：每路由发最小探针，断言不是「route not found」壳 404
   （路由在册但 handler 缺位=BROWSERWALK-3 F3-P1-3 同形）；
3. **适配透传**：断言非 5xx——AttributeError/接线断线家族（缺透传假 500）
   在此红指名路由（与 AST 对账钉 tests/test_http_service_completeness.py
   的名字级互补：钉管静态引用面，矩阵兜动态形态与运行时装配）。

探针载荷策略：GET 直打；POST/PUT/DELETE 打空 JSON 载荷——4xx=校验器说话
（合法），5xx=接线断线（红）。豁免清单闭集+具名理由（禁无名扩容）。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from tests._http_surface_ast import route_table
from tests.http_services import http_services


class _UniversalStub(dict):
    """万能合成源：任何属性访问/调用返回自身（空 dict 子类）。

    - 属性面：适配层 ``_op``/getattr 取任何名字都得到非 None（透传全在位）；
    - 数据面：空 dict=JSON 安全（handlers 对快照做 .get/迭代/序列化都走
      「无数据」合法路径），下标取值经 ``__missing__`` 返回自身继续解析；
    - 真值面：空 dict=假值 → 会话门判 rejected/无身份 → 绝大多数数据路由
      走 401/4xx 校验路径（正是矩阵要的「校验器说话」）。
    """

    def __getattr__(self, name: str) -> "_UniversalStub":
        return _STUB

    def __call__(self, *args, **kwargs) -> "_UniversalStub":
        return _STUB

    def __missing__(self, key: Any) -> "_UniversalStub":
        return _STUB

    def __iter__(self):
        return iter(())

    def __int__(self) -> int:
        return 0

    def __float__(self) -> float:
        return 0.0


_STUB = _UniversalStub()


def _mutated_http_services(removal: str) -> Any:
    """文本级删除适配层一条显式条目后重载（件④M2 断线注入算子）。

    与静态钉 tests/test_http_service_completeness.py 的变异算子同形：
    断线形态=适配层缺透传（产物 SimpleNamespace 缺属性 → 路由 handler
    AttributeError 500），源替身用 _UniversalStub 不变。
    """
    source_path = Path(__file__).resolve().parent / "http_services.py"
    text = source_path.read_text(encoding="utf-8")
    count = text.count(removal)
    assert count == 1, f"变异算子目标行不唯一（{count} 处），算子失效"
    namespace: dict[str, Any] = {"__name__": "tests._http_services_mutated_probe"}
    exec(compile(text.replace(removal, "", 1), str(source_path), "exec"), namespace)
    return namespace["http_services"]


# 豁免档案（闭集+具名理由，禁无名扩容）——不探针的路由：
# - (*, "/api/")、(*, "/api/v3/")：分发器兜底前缀而非路由，404 route-not-found
#   即其正确行为（前缀探针无被测面）；
# - (GET, "/api/v3/data-migration/file")：搬家包下载需真实 staged 包 + token
#   （handler 无 token 预校验直入门面调用并解包二元组，stub 返回值形状不可
#   满足）；接线由 AST 对账钉 settings.migration_download_file 名字级覆盖。
# - (GET, "data-map")/(GET, "assessment")：handler 把 learning.repository/
#   auth_catalog.catalog 直传产品函数读本地 sqlite/目录缓存（真实 store 形状
#   不可 stub）；接线已由 AST 钉 learning.repository + auth_catalog.catalog
#   名字级覆盖，运行时面归 USER-SCENARIO-SWEEP 真实数据规模走查。
# - (POST, "live-room/sessions")：真实 consume_grant 对空 grant 抛
#   LiveRoomError → 400（闭集校验在服务内，http_api 注释即契约）；stub 不抛
#   域错误 → KeyError 500 属 stub 形状而非接线断线；AST 钉覆盖
#   live_room.consume_grant 名字级。
EXEMPT_PROBES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/"),
    ("GET", "/api/v3/"),
    ("POST", "/api/v3/"),
    ("PUT", "/api/v3/"),
    ("DELETE", "/api/v3/"),
    ("GET", "/api/v3/data-migration/file"),
    ("GET", "data-map"),
    ("GET", "assessment"),
    ("POST", "live-room/sessions"),
})


class RouteProbeMatrixTests(unittest.TestCase):
    def test_ast_route_surface_does_not_shrink(self):
        """第 1 层·路由在册锚：AST 提取的路由注册面不缩水（今日探针面
        118 条，含 dispatch/path/prefix 三形态）。提取器失效（面骤减）=
        矩阵静默空转，先红在此。"""
        table = route_table()
        self.assertGreaterEqual(len(table.unique_routes("GET")), 50, "GET 路由面骤减")
        self.assertGreaterEqual(len(table.unique_routes("POST")), 50, "POST 路由面骤减")
        self.assertGreaterEqual(len(table.unique_routes("PUT")), 2, "PUT 路由面骤减")
        self.assertGreaterEqual(len(table.unique_routes("DELETE")), 1, "DELETE 路由面骤减")
        # 本轮 5 处实测缺口的宿主路由必须仍在册（防回归锚）。
        for method, route in (
            ("GET", "onboarding"),
            ("POST", "onboarding/actions"),
            ("POST", "client-update/actions"),
            ("POST", "/api/v3/lifecycle/shutdown"),
            ("GET", "greeting-context"),
        ):
            self.assertIn(route, table.unique_routes(method), f"{method} {route} 在册锚")

    def test_every_registered_route_answers_without_shell_404_or_5xx(self):
        """第 2+3 层：每个在册路由的最小探针必须 ①非「route not found」壳 404
        （handler 在位）②非 5xx（适配透传在位）。4xx=校验器说话=合法。"""
        table = route_table()
        probes = self._build_probes(table)
        self.assertGreaterEqual(
            len(probes), 100, "探针面骤减（应 >100，今日约 110）"
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = http_services(_UniversalStub())
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(service, Path(tmp) / "frontend")
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                failures: list[str] = []
                for method, route in probes:
                    status, body = self._probe(base, method, route)
                    label = f"{method} /api/v3/{route}" if not route.startswith("/") else f"{method} {route}"
                    if status >= 500:
                        failures.append(f"{label} → {status}（5xx=接线断线/适配缺口）")
                        continue
                    if status == 404 and isinstance(body, dict) and str(
                        body.get("error", "")
                    ).endswith("route not found"):
                        failures.append(f"{label} → 壳 404（handler 缺位）")
                self.assertEqual(
                    failures,
                    [],
                    "路由探针矩阵红（路由在册→handler/适配必须接得上）:\n  "
                    + "\n  ".join(failures),
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_matrix_catches_broken_adapter_and_names_route_and_attribute(self):
        """M2 验收钉·永久注入自检：人工注入一个断线（删适配层显式条目
        auth_catalog.onboarding_guide_snapshot）→ 探针矩阵必须红并指名
        路由+属性。注入宿主=GET /api/v3/onboarding：登录前路由无会话门，
        handler 在响应写出前读该属性，缺失=确定性 500 AttributeError
        （矩阵子集路径若给了假绿=矩阵失效，先红在此）。"""
        mutated_http_services = _mutated_http_services(
            'onboarding_guide_snapshot=_op(source, "onboarding_guide_snapshot"),'
        )
        with tempfile.TemporaryDirectory() as tmp:
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(mutated_http_services(_STUB), Path(tmp) / "frontend"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                status, _body = self._probe(base, "GET", "onboarding")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertGreaterEqual(
            status, 500,
            "注入断线（适配层缺 auth_catalog.onboarding_guide_snapshot）后 "
            f"GET /api/v3/onboarding 应 5xx=接线断线红，实得 {status}——矩阵失效",
        )

    # ------------------------------------------------------------------
    def _build_probes(self, table) -> list[tuple[str, str]]:
        """AST 路由面 → 探针清单（(method, path) 对）。

        - dispatch 形态：/api/v3/<route> 直拼；
        - path 特判形态：绝对路径直用；
        - prefix 形态（/api/v3/live-room/play/）：补无谓后缀成具体路由。
        """
        probes: list[tuple[str, str]] = []
        for method in ("GET", "POST", "PUT", "DELETE"):
            seen: set[str] = set()
            for entry in table.by_method(method):
                if entry.route in seen or (method, entry.route) in EXEMPT_PROBES:
                    continue
                seen.add(entry.route)
                if entry.kind == "dispatch":
                    probes.append((method, entry.route))
                elif entry.kind == "path":
                    probes.append((method, entry.route))
                elif entry.kind == "prefix":
                    probes.append((method, entry.route.rstrip("/") + "/probe"))
        return probes

    def _probe(self, base: str, method: str, route: str) -> tuple[int, Any]:
        path = route if route.startswith("/") else f"/api/v3/{route}"
        data = None if method == "GET" else json.dumps({}).encode()
        request = Request(
            f"{base}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                content_type = response.headers.get("Content-Type", "")
                if content_type.startswith("text/event-stream"):
                    # SSE 长连路由（events）：响应头即健康证据，读体会挂起在
                    # 永续心跳流上——不读体，即刻收口（关闭时 handler 写回失败
                    # 自然退出）。
                    return response.status, None
                raw = response.read()
                return response.status, self._safe_json(raw)
        except HTTPError as error:
            return error.code, self._safe_json(error.read())

    @staticmethod
    def _safe_json(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None


if __name__ == "__main__":
    unittest.main()
