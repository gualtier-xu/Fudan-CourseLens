"""路由→测试覆盖门（夜14-R7 积压 1 清偿：防新增路由零测试漏防）。

形状：从 ``src/runtime/http_api.py`` 静态抽取 v3 路由清单（``route == "x"``
字面量 ∪ ``route in {…}`` 清单成员），然后要求**每个路由字符串至少被 tests/
下一个 .py/.mjs 文件提及**——新增路由不挂任何测试即红。静态扫描是第一序
绊线（「提及」弱于「行为断言」），关键路由的行为合同由既有专测承载
（tasks/enqueue、tasks/actions、course-review 族、client-reset 等）。

三齿：
- 齿1 清单哨兵：抽取规模下限 + 关键路由必在（防抽取器退化为空集假绿）；
- 齿2 零豁免提及门：ALLOWLIST 恒空表；豁免表卫生反向腿（清单外残留=红）；
- 齿3 残留名与薄覆盖探针：钉死「门清单在册、无分发 handler」的四个残留路由
  名现行为（404 闭集兜底）——未来接上真 handler 时本钉红=强制升级为真合同
  测试；并为 references/validate、schedules/run 两条薄覆盖路由补路由装配腿。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from tests.http_services import http_services

HTTP_API_SOURCE = Path(__file__).resolve().parents[1] / "src" / "runtime" / "http_api.py"
TESTS_ROOT = Path(__file__).resolve().parent

# 齿2：零豁免表。2026-10-09 清查时曾检出 6 条零提及路由，其中
# references/validate、schedules/run 由本文件探针腿转为真覆盖；
# smart-timeline 族与 documents/actions、quizzes/actions 为门清单残留名
# （齿3 钉死 404 现行为，字符串由本文件探针自身提及）。新增路由禁止进表。
ALLOWLIST: set[str] = set()

# 齿3：门清单在册但无分发 handler 的残留路由名（现役时间轴路由=timeline /
# timeline/classify；documents 与 quizzes 的写面走 documents、quizzes 本名）。
VESTIGIAL_POST_ROUTES = ("documents/actions", "quizzes/actions", "smart-timeline/actions")
VESTIGIAL_GET_ROUTE = "smart-timeline"


def extract_route_inventory(text: str) -> set[str]:
    eq = set(re.findall(r'route == "([a-z0-9/_-]+)"', text))
    inset: set[str] = set()
    for block in re.findall(r"route in \{([^}]+)\}", text):
        inset |= set(re.findall(r'"([a-z0-9/_-]+)"', block))
    return eq | inset


def collect_tests_corpus() -> str:
    parts: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".mjs"):
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


class _GateReadyService:
    """最小会话门桩：authentication ready 放行动作族门，其余面零承载。"""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}


class _ScheduleRunService(_GateReadyService):
    """schedules/run 探针桩：run_daily_schedule 挂服务本体（http_services 适配
    层按 _op(source, name) 顶层取方法，与既有 _TaskService 同形），并记录 force 透传。"""

    def __init__(self, root: Path):
        super().__init__(root)
        self.runs: list[dict] = []
        self.task_store = None  # facade repository 槽位（本腿零任务面）

    def run_daily_schedule(self, force=False):
        self.runs.append({"force": force})
        return {"triggered": 0, "force": force}


class _ServerHarness:
    """每个用例独立端口/独立服务的 ThreadingHTTPServer 装配（context 式）。"""

    def __init__(self, service, tmp: str):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(service), Path(tmp))
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RouteInventoryGateTests(unittest.TestCase):
    """齿1+齿2：清单哨兵与零提及门（纯静态扫描，零产品行为触碰）。"""

    def setUp(self):
        self.inventory = extract_route_inventory(HTTP_API_SOURCE.read_text(encoding="utf-8"))

    def test_inventory_extraction_keeps_scale_and_anchor_routes(self):
        """齿1：抽取器退化（空集/丢锚点路由）即红，防哨兵假绿。"""
        self.assertGreaterEqual(
            len(self.inventory), 85,
            f"v3 路由清单抽取规模异常（现抽 {len(self.inventory)} 条，2026-10-09 基线 95 条）",
        )
        for anchor in (
            "tasks/enqueue", "tasks/actions", "timeline", "client-reset/actions",
            "course-data", "references/validate", "schedules/run", "bookmarks",
        ):
            self.assertIn(anchor, self.inventory, f"锚点路由 {anchor} 不在抽取清单中")

    def test_extractor_rejects_non_route_shapes(self):
        """齿1 反向：非路由比较形状（action==/括号集合）不得被抽取出（边界卫生）。"""
        noise = (
            'action == "documents/actions"; view == "stage"; route in ("timetable",);'
        )
        self.assertEqual(extract_route_inventory(noise), set(), "非 route ==/route in{} 形状零抽取")

    def test_every_route_is_mentioned_by_at_least_one_test_file(self):
        """齿2：每个 v3 路由至少被一个测试文件提及——新增路由零测试即红。"""
        corpus = collect_tests_corpus()
        unmentioned = sorted(
            route for route in self.inventory
            if route not in corpus and route not in ALLOWLIST
        )
        self.assertEqual(
            unmentioned, [],
            f"以下 v3 路由零测试提及（新增路由请同步挂至少一条行为钉或最小探针）：{unmentioned}",
        )

    def test_allowlist_hygiene_reverse_leg(self):
        """齿2 卫生反向：ALLOWLIST 出现清单外残留条目=红（删路由后清表）。"""
        stale = sorted(ALLOWLIST - self.inventory)
        self.assertEqual(stale, [], f"豁免表含清单外陈旧条目（路由已删请同步清表）：{stale}")


class VestigialRouteProbeTests(unittest.TestCase):
    """齿3：残留路由名的 404 闭集兜底现行为钉（绝不 500 runtime_failed）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = _ServerHarness(_GateReadyService(Path(self._tmp.name)), self._tmp.name)
        self.addCleanup(self.harness.close)

    def _post(self, route: str, payload: dict):
        return Request(
            f"{self.harness.base}/api/v3/{route}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def test_vestigial_post_routes_degrade_to_closed_404(self):
        """POST 残留名 → 404 「v3 route not found」兜底；接真 handler 时本钉红。"""
        for route in VESTIGIAL_POST_ROUTES:
            with self.assertRaises(HTTPError) as rejected:
                urlopen(self._post(route, {"action": "probe"}))
            self.assertEqual(rejected.exception.code, 404, route)
            self.assertEqual(
                json.loads(rejected.exception.read()), {"error": "v3 route not found"}, route
            )

    def test_vestigial_get_route_degrades_to_closed_404(self):
        """GET 残留名（本地优先门内）→ 404 闭集兜底（现役路由=timeline）。"""
        with self.assertRaises(HTTPError) as rejected:
            urlopen(f"{self.harness.base}/api/v3/{VESTIGIAL_GET_ROUTE}?sub_id=s1")
        self.assertEqual(rejected.exception.code, 404)
        self.assertEqual(json.loads(rejected.exception.read()), {"error": "v3 route not found"})

    def test_dispatched_reference_validate_route_serves_envelope(self):
        """references/validate 覆盖腿：合法体 happy envelope 形状（校验器直测在
        test_api_v3_validators.py，本腿钉路由装配=envelope 包裹层）。"""
        payload = {
            "course_id": "90007", "sub_id": "900071",
            "start_ms": 0, "end_ms": 1500,
            "source_hash": "a" * 64, "source": "transcript", "source_ref": " cue-1 ",
        }
        with urlopen(self._post("references/validate", payload)) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["schema"], "courselens.api.v3")
        reference = body["data"]["reference"]
        self.assertEqual(reference["course_id"], "90007")
        self.assertEqual(reference["source_hash"], "a" * 64)
        self.assertEqual(reference["source_ref"], "cue-1", "source_ref strip 规范化随 envelope 出")

    def test_dispatched_reference_validate_route_rejects_invalid_body(self):
        """references/validate 拒绝腿：非法体 400 直出校验错误（现行为=裸 str(exc) 文本）。"""
        with self.assertRaises(HTTPError) as rejected:
            urlopen(self._post("references/validate", {"course_id": "abc", "sub_id": "1"}))
        self.assertEqual(rejected.exception.code, 400)

    def test_dispatched_schedules_run_route_serves_envelope_and_forces(self):
        """schedules/run 覆盖腿：200 envelope {"run": …} + 手动触发恒 force=True。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = _ScheduleRunService(Path(tmp))
            harness = _ServerHarness(service, tmp)
            try:
                request = Request(
                    f"{harness.base}/api/v3/schedules/run",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read())
                self.assertEqual(payload["schema"], "courselens.api.v3")
                self.assertIn("run", payload["data"])
                self.assertEqual(service.runs, [{"force": True}], "手动触发恒 force=True（语义钉）")
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()
