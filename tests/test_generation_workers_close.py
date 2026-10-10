"""WP-3（LOCALRUNTIME-POLISH-1 U2）：三本地生成 worker 纳入 close 收口面钉。

N9-H close 链全景卡（P63 家族第三例）销项：summary/question/pdf 三
worker 此前无 stop 事件、无 join、不在 close 链，停机途中在途完成项
可能写已关 learning_store。对齐 P63/N9-H 样板：close() 置停先于有界
join，三线程全停、phases 记录完整、线程名退场。
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.application import CourseLensApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GenerationWorkersCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        # 暂停泊位：summary/question 驻留 stop.wait(0.25) 循环，不触发任何生成体。
        self.app._paused = True
        self.release = threading.Event()

    def tearDown(self) -> None:
        self.release.set()
        self.app.close()
        self.temporary.cleanup()

    def test_close_stops_and_joins_generation_worker_trio(self):
        app = self.app
        entered = threading.Event()
        release = self.release
        original_get_task = app.task_store.get_task

        def _parked_get_task(task_id):
            # pdf worker 无暂停泊位：钉在其取件腿上（P63 样板的 parked 腿等价物）。
            if str(task_id) == "park-pdf":
                entered.set()
                release.wait(timeout=10)
                return None
            return original_get_task(task_id)

        app.task_store.get_task = _parked_get_task
        app._summary_queue.append({"task_id": "park-summary"})
        app._question_queue.append({"task_id": "park-question"})
        app._courseware_pdf_queue.append("park-pdf")
        app._ensure_summary_worker()
        app._ensure_question_worker()
        app._ensure_courseware_pdf_worker()
        summary = app._summary_worker
        question = app._question_worker
        pdf = app._courseware_pdf_worker
        self.assertIsNotNone(summary)
        self.assertIsNotNone(question)
        self.assertIsNotNone(pdf)
        self.assertTrue(entered.wait(timeout=15), "pdf worker 应已进入被钉住的取件腿")
        # 暂停泊位是无限驻留（队列非空+暂停），过两个拍子仍在=确实泊住。
        time.sleep(0.6)
        self.assertTrue(summary.is_alive(), "summary worker 应泊在暂停驻留位")
        self.assertTrue(question.is_alive(), "question worker 应泊在暂停驻留位")

        closed: list[dict] = []
        closer = threading.Thread(
            target=lambda: closed.append(app.close()), daemon=True
        )
        closer.start()
        self.assertTrue(
            app._generation_workers_stop.wait(timeout=15),
            "close() 应先置停事件再有界 join",
        )
        release.set()
        closer.join(timeout=30)
        self.assertFalse(closer.is_alive(), "close() 不应被后台线程无限拖住")
        self.assertEqual(len(closed), 1)
        phases = closed[0]["phases"]
        self.assertEqual(phases["summary_worker"], "stopped")
        self.assertEqual(phases["courseware_pdf_worker"], "stopped")
        self.assertEqual(phases["question_worker"], "stopped")
        self.assertFalse(closed[0]["timed_out"])
        self.assertFalse(summary.is_alive())
        self.assertFalse(pdf.is_alive())
        self.assertFalse(question.is_alive())
        alive = {thread.name for thread in threading.enumerate()}
        self.assertNotIn("learning-summary-generator", alive)
        self.assertNotIn("courseware-pdf-generator", alive)
        self.assertNotIn("courselens-question-explainer", alive)


if __name__ == "__main__":
    unittest.main()
