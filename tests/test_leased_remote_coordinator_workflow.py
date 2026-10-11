"""B31（N15-R4/OV-4a）：``_leased_remote_coordinator`` workflow 重建机制直接钉。

此前该机制被全部测试 fake 掉（捕获钉只断 lease 的 workflows 值）——
``replace`` 形态构造调用、等值短路不重建、独立实例参数透传零直接钉。
若 RemoteCoordinator 构造签名漂移或短路失效（等值也重建 → 并发任务设置
互踩），全量测试不红。本件以录制类替换 RemoteCoordinator 构造点，钉：

1. workflow 差异 → 重建恰一次，新实例携带 replace 后的 settings
   （workflow 换新、其余字段零触碰）且 task_store/credentials/github 四参透传；
2. 等值 workflow → 零重建（yield 共享协调器原实例，零额外开销）；
3. workflow=None → 永不重建（共享协调器 process.yml 现状字节不变）；
4. 重建全程包裹在 job_token_lease(task_id, task_store) 内。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication


@dataclass(frozen=True)
class _FakeRemoteSettings:
    """``replace()`` 需要真 dataclass；字段形状对齐 RemoteSettings 消费面。"""

    workflow: str = "process.yml"
    enabled: bool = True
    poll_seconds: int = 3


class _FakeCoordinator:
    def __init__(self, workflow: str = "process.yml"):
        self.settings = _FakeRemoteSettings(workflow=workflow)
        self.task_store = object()
        self.credentials = object()
        self.github = object()


class _RecordingJobTokenLease:
    def __init__(self):
        self.entered: list[tuple[str, object]] = []

    @contextmanager
    def job_token_lease(self, *, task_id, task_store):
        self.entered.append((task_id, task_store))
        yield


class _RecordingRemoteCoordinator:
    """构造录制器：钉 RemoteCoordinator(replace(settings, ...), store, cred, github=…) 形态。"""

    def __init__(self, settings, task_store, credentials, *, github=None):
        self.settings = settings
        RECORDED_CALLS.append({
            "settings": settings,
            "task_store": task_store,
            "credentials": credentials,
            "github": github,
        })


RECORDED_CALLS: list[dict] = []


class LeasedRemoteCoordinatorWorkflowTests(unittest.TestCase):
    def setUp(self):
        RECORDED_CALLS.clear()
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.lease = _RecordingJobTokenLease()
        self.shared = _FakeCoordinator(workflow="process.yml")
        self.app.github_app = self.lease
        self.app._prepare_remote_coordinator = lambda: self.shared

    def tearDown(self):
        self.app.close()
        self.temporary.cleanup()

    def test_different_workflow_rebuilds_independent_instance_with_replaced_settings(self):
        with patch(
            "src.remote.coordinator.RemoteCoordinator", _RecordingRemoteCoordinator
        ):
            with self.app._leased_remote_coordinator("task-1", workflow="llm.yml") as coordinator:
                self.assertIsInstance(coordinator, _RecordingRemoteCoordinator)
        self.assertEqual(len(RECORDED_CALLS), 1, "workflow 差异必须恰重建一次")
        call = RECORDED_CALLS[0]
        self.assertEqual(call["settings"].workflow, "llm.yml")
        # replace 语义：其余字段零触碰（防整包重建静默改默认）。
        self.assertEqual(call["settings"].enabled, self.shared.settings.enabled)
        self.assertEqual(call["settings"].poll_seconds, self.shared.settings.poll_seconds)
        # 四参透传：共享协调器的 store/credentials/github 原样进入独立实例。
        self.assertIs(call["task_store"], self.shared.task_store)
        self.assertIs(call["credentials"], self.shared.credentials)
        self.assertIs(call["github"], self.shared.github)
        # 重建发生在 job_token_lease 内：lease 以 task_id+应用自身 task_store 进入恰一次。
        self.assertEqual(self.lease.entered, [("task-1", self.app.task_store)])

    def test_equal_workflow_short_circuits_without_rebuild(self):
        with patch(
            "src.remote.coordinator.RemoteCoordinator", _RecordingRemoteCoordinator
        ):
            with self.app._leased_remote_coordinator("task-2", workflow="process.yml") as coordinator:
                self.assertIs(coordinator, self.shared, "等值请求必须 yield 共享协调器原实例")
        self.assertEqual(RECORDED_CALLS, [], "等值 workflow 绝不重建")

    def test_workflow_none_never_rebuilds(self):
        with patch(
            "src.remote.coordinator.RemoteCoordinator", _RecordingRemoteCoordinator
        ):
            with self.app._leased_remote_coordinator("task-3") as coordinator:
                self.assertIs(coordinator, self.shared)
        self.assertEqual(RECORDED_CALLS, [], "workflow=None 永不重建")

    def test_rebuild_failure_leaves_shared_coordinator_untouched(self):
        # 独立实例构造失败（如签名漂移）不得污染共享协调器的 workflow 现状。
        class _ExplodingCoordinator(_RecordingRemoteCoordinator):
            def __init__(self, settings, task_store, credentials, *, github=None):
                raise RuntimeError("coordinator construction refused")

        with patch("src.remote.coordinator.RemoteCoordinator", _ExplodingCoordinator):
            with self.assertRaises(RuntimeError):
                with self.app._leased_remote_coordinator("task-4", workflow="llm.yml"):
                    pass
        self.assertEqual(self.shared.settings.workflow, "process.yml", "共享协调器设置零触碰")


if __name__ == "__main__":
    unittest.main()
