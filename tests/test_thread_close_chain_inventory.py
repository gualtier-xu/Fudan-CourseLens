"""夜10-C T8：线程收口清单防漂移钉。

close 链成员完整性复查（P63 家族+WP-3+夜9 self-heal 历次收口的executable
固化）：src/application.py 每一个 ``threading.Thread`` 创建点必须在本测试的
已归类清单内——新线程创建点要么在 ``close()`` 收口面注册（stop 事件+有界
join），要么明确归类为一次性探针/任务并在清单登记。清单按 name=（缺名时
按 target=）多重集匹配，行号漂移不误报。
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SOURCE = PROJECT_ROOT / "src" / "application.py"

# 已归类清单（夜10-C 复查基线）：
#   close-chain = close() 收口面注册（stop 事件 + 有界 join）的循环线程；
#   one-shot-store-write = 完成路径写 task_store/catalog_repository 但尚在
#   收口链外（P63 家族第四例候选，修复面在 application.py，见夜10-C 结果
#   文件）；probe = 一次性只读探针；watch = 停止事件感知的有界观察器。
CLASSIFIED_SITES: dict[str, str] = {
    # close-chain 成员（close() 置停+有界 join）
    "name:courselens-automation-monitor": "close-chain",
    "name:connection-path-probe": "close-chain",
    "name:auto-connect-resume": "close-chain",
    "name:worker-auto-sync": "close-chain",
    "name:session-self-heal": "close-chain",
    "name:learning-summary-generator": "close-chain",
    "name:courseware-pdf-generator": "close-chain",
    "name:courselens-question-explainer": "close-chain",
    "name:courselens-quality-judge": "close-chain",
    # 停止事件感知的有界观察器（_automation_stop 感知/有界窗）
    "target:self._remote_result_watch_loop": "bounded-watch",
    # 一次性探针（只读，无 store 写路径）
    "name:campus-route-preflight": "probe",
    "name:client-keepalive-nudge": "probe",
    # 收口链外的 store 写路径（P63 家族第四例候选，修复面在 application.py）
    "name:courselens-local-quiz": "unchained-store-write",
    "name:courselens-daily-schedule": "unchained-store-write",
    "name:authorized-catalog-refresh": "unchained-store-write",
    "name:session-restore-prime": "unchained-store-write",
    "name:remote-echo": "unchained-store-write",
    "target:self._run_subtitle_queue": "unchained-store-write",
}

# close() 收口面必须持续出现的成员面（stop 事件 + 有界 join 清单）。
REQUIRED_CLOSE_MEMBERS = (
    "_automation_stop",
    "_connection_probe_stop",
    "_auto_connect_resume_stop",
    "_worker_auto_sync_stop",
    "_session_self_heal_stop",
    "_generation_workers_stop",
    '"summary_worker"',
    '"courseware_pdf_worker"',
    '"question_worker"',
    '"session_self_heal"',
)


class ThreadCloseChainInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APPLICATION_SOURCE.read_text(encoding="utf-8")

    def _site_keys(self) -> Counter[str]:
        keys: Counter[str] = Counter()
        for match in re.finditer(r"threading\.Thread\(", self.source):
            window = self.source[match.start():match.start() + 500]
            name = re.search(r'\bname="([^"]+)"', window)
            if name:
                keys[f"name:{name.group(1)}"] += 1
                continue
            target = re.search(r"target=([^,\n)]+)", window)
            if target:
                keys[f"target:{target.group(1).strip()}"] += 1
                continue
            keys["unclassified"] += 1
        return keys

    def test_every_thread_site_is_classified(self) -> None:
        keys = self._site_keys()
        expected = Counter(CLASSIFIED_SITES.keys())
        self.assertEqual(
            keys, expected,
            "threading.Thread 创建点清单漂移：新线程要么在 close() 收口面注册"
            "（stop 事件+有界 join），要么按语义归类并更新本清单；未登记项="
            f"{sorted((keys - expected).elements())}，消失项="
            f"{sorted((expected - keys).elements())}",
        )

    def test_close_chain_registry_keeps_required_members(self) -> None:
        close_index = self.source.index("def close(self, timeout")
        close_body = self.source[close_index:self.source.index("def ", close_index + 10)]
        missing = [member for member in REQUIRED_CLOSE_MEMBERS if member not in close_body]
        self.assertEqual(
            missing, [],
            "close() 收口面缺成员（stop 事件或有界 join 清单被移除）",
        )

    def test_unchained_store_write_family_is_still_known_debt(self) -> None:
        # 家族候选不得无声转正或无声消失：修复时把对应条目改为 close-chain
        # 并同步更新此断言；移除线程时同步移除条目。字幕队列消费者是最重的
        # task_store 写者（P63 家族第四例主候选）。
        self.assertEqual(
            {key for key, kind in CLASSIFIED_SITES.items() if kind == "unchained-store-write"},
            {
                "name:courselens-local-quiz",
                "name:courselens-daily-schedule",
                "name:authorized-catalog-refresh",
                "name:session-restore-prime",
                "name:remote-echo",
                "target:self._run_subtitle_queue",
            },
        )


if __name__ == "__main__":
    unittest.main()
