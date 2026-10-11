"""P3-CONTRACT-1 PKG-A 回归钉：palette「深度回答」后端链（mode="deep"）。

覆盖判据（全部合成夹具，零真实账号、零外呼；mode="" 全路径现状逐字等价由
tests/test_grounded_answer_scope.py 全量护航）：

1. mode 闭集 {"", "deep"}：其余值 ValueError(deep_answer_mode_invalid)，零任务；
2. 空 packet 诚实即时降级：零任务零计费零 LLM，闭集码
   deep_answer_evidence_unavailable（且先于 key 判据——无 key 也照常诚实降级）；
3. 分派入队：question 任务 payload.deep_answer=True、config_key=palette:<hash>、
   v3 metadata sealed、进度标签「深度回答等待在线计算」、响应含任务身份；
4. 活跃态去重：排队中同问同证据二连点=同一任务 created=false 零双扣；
   终态失败后重发=新任务（重发即重试，无隐藏状态机）；
5. 第三目标分支：deep 任务不落 bookmark_task_invalid；同一 build_job 零改复用
   （job_kind/requested_outputs/pipeline 逐字）；subject 标签=深度回答；
6. import_result palette 分支：validate_grounded_answer 双层闭集，
   ready/insufficient 落 store 回调（写入点唯一）；不碰书签面；
7. 终态失败落 store failed+error_code（可复看）；瞬态自动重试恰一次对
   palette 同权（重试在途不落 store）；
8. 消耗记账：import_result 落任务行绝对消耗；ai_usage_month 加性双键
   deep_answers/quick_answers；count_tasks_created_since 前缀参缺省逐字。
"""

from __future__ import annotations

import contextlib
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.application import CourseLensApplication
from src.runtime import student_features as student_features_module
from src.runtime.learning_store import LearningStore
from src.runtime.search_index import LearningSearchIndex
from src.runtime.task_store import TaskStore

COURSE = "crs-p3deep"
SUB_A = "sub-p3deep-1"
INSUFFICIENT_ANSWER = "资料不足，无法根据当前课程资料回答。"
DEEPSEEK_USAGE = {"deepseek_tokens": 120, "elapsed_seconds": 3.0}


def _synthetic_packet() -> list[dict]:
    """一份能通过 validate_grounded_answer 闭环校验的证据包（hash 逐字节对上）。"""
    text = "链式聚合的收敛性由压缩映射原理保证。"
    return [{
        "citation_id": "c1",
        "course_id": COURSE,
        "sub_id": SUB_A,
        "start_ms": 15000,
        "end_ms": 21000,
        "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "source": "transcript",
        "label": "第一讲 · 字幕",
    }]


class _FakeCoordinator:
    """替掉远端协调器：按次回放合成结果或抛合成错误，零网络。"""

    def __init__(self, outputs=None, *, metrics=None, failures=()):
        self.outputs = dict(outputs or {})
        self.metrics = dict(metrics or {})
        self.failures = list(failures)
        self.jobs: list[dict] = []

    def execute(self, *, task_id, build_job, import_result, cancel_requested=None, progress=None):
        self.jobs.append(build_job("synthetic-public-key"))
        if self.failures:
            raise self.failures.pop(0)
        result = {"outputs": dict(self.outputs), "metrics": dict(self.metrics)}
        import_result(result)
        return result


class _DeepHarness(unittest.TestCase):
    """合成 CourseLensApplication 壳：只填问答链真正会碰到的运行时状态。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = LearningStore(self.root / "learning.db")
        self.tasks = TaskStore(self.root / "tasks.db")
        self.app = CourseLensApplication.__new__(CourseLensApplication)
        self.app.learning_store = self.store
        self.app.task_store = self.tasks
        self.app.output_dir = self.root
        self.app.search_index = LearningSearchIndex(
            self.store, catalog_snapshot=lambda: {}, subtitle_sync=lambda _sub_id: {},
        )
        self.app.start_search_index = lambda: None
        self.app._lock = threading.RLock()
        self.app._paused = False
        self.app._question_queue = []
        self.app._question_current = None
        self.app._question_cancel = threading.Event()
        self.app._generation_workers_stop = threading.Event()
        self.app._active_question_task_id = ""
        self.app._cloud_run_condition = threading.Condition()
        self.app._cloud_runs_active = 0
        self.app._deepseek_api_key = "sk-synthetic"
        self.save_calls: list[dict] = []

        def _fake_save(path, **record):
            self.save_calls.append({"path": str(path), **record})
            return {"task_id": record.get("task_id"), "state": record.get("state")}

        self.coordinator = _FakeCoordinator()
        self.captured_lease_workflows: list[str | None] = []
        self._patchers = [
            mock.patch.object(CourseLensApplication, "_ensure_question_worker", lambda self: None),
            mock.patch.object(
                CourseLensApplication, "_leased_remote_coordinator", self._fake_coordinator_lease,
            ),
            mock.patch.object(CourseLensApplication, "_cloud_run_slot", self._no_cloud_slot),
            mock.patch.object(
                student_features_module, "save_search_answer", _fake_save, create=True,
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)
        self.addCleanup(self.store.close)

    def _stop_patchers(self) -> None:
        for patcher in self._patchers:
            patcher.stop()

    # -- 远端替身 -----------------------------------------------------------

    @contextlib.contextmanager
    def _fake_coordinator_lease(self, _task_id: str, *, workflow: str | None = None):
        self.captured_lease_workflows.append(workflow)
        yield self.coordinator

    @contextlib.contextmanager
    def _no_cloud_slot(self, _task_id: str, *, cancel_requested=None, on_wait=None):
        yield

    # -- 夹具 ---------------------------------------------------------------

    def seed_lecture(self, sub_id: str, course_id: str, keyword: str) -> None:
        """与 test_grounded_answer_scope 同款夹具：目录行 + 单条字幕文档。"""
        self.store.sync_search_catalog([{
            "sub_id": sub_id,
            "course_id": course_id,
            "course_title": f"course-{course_id}",
            "lecture_title": f"lecture-{sub_id}",
            "teacher": "teacher",
            "catalog_version": "v1",
        }], prune=False)
        self.store.replace_search_documents(
            sub_id,
            [{
                "doc_key": f"{sub_id}:transcript:15000",
                "source": "transcript",
                "source_ref": "",
                "document_title": f"lecture-{sub_id}",
                "start_ms": 15000,
                "display_text": f"the {keyword} rule explains the second law",
                "search_text": f"the {keyword} rule explains the second law",
                "source_version": "v1",
            }],
            indexed_version="v1",
        )

    def question_tasks(self) -> list[dict]:
        return [task for task in self.tasks.list_tasks(limit=200) if task.get("kind") == "question"]

    def search_answer_tasks(self) -> list[dict]:
        return [task for task in self.tasks.list_tasks(limit=200) if task.get("kind") == "search_answer"]

    def enqueue_palette_task(self, *, packet=None, course_id: str = COURSE, sub_id: str = SUB_A) -> dict:
        payload = {
            "deep_answer": True,
            "query": "链式聚合为什么收敛？",
            "evidence": packet if packet is not None else _synthetic_packet(),
            "input_hash": "a" * 64,
            "prompt_version": "bookmark-answer-v1",
            "cancel_requested": False,
        }
        task, _created = self.tasks.add_task(
            "question", course_id, sub_id, payload, config_key="palette:" + payload["input_hash"],
        )
        # 生产分派（_deep_answer_dispatch）在入队时写 v3 metadata；夹具对齐。
        self.tasks.upsert_v3_metadata(
            task["task_id"], dedupe_key=f"question:palette:{payload['input_hash']}",
            stage="queued", privacy_state="sealed", requested_outputs=["answer"],
            input_hash=payload["input_hash"],
        )
        with self.app._lock:
            self.app._queue_persisted_task(task)
        return task

    def drain_question_queue(self) -> None:
        """同步跑完队列（生产是后台线程，测试里就地跑）。"""
        self.app._run_question_queue()
        # N1-ROUTING + D-20261009-13：learning_pack 在 worker N20 profile 合同
        # 闭集内（必须 process-v1）→ 白名单恒 process.yml。
        self.assertTrue(all(w == "process.yml" for w in self.captured_lease_workflows))


class DeepModeContractTests(_DeepHarness):
    """mode 闭集 + 分派流程（P3-CONTRACT-1 §①）。"""

    def test_mode_closed_set_rejects_other_values(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        for mode in ("fast", "DEEP", "deep ", "local"):
            with self.assertRaises(ValueError) as caught:
                self.app.answer_from_evidence("gradient", mode=mode)
            self.assertEqual(str(caught.exception), "deep_answer_mode_invalid")
        self.assertEqual(self.question_tasks(), [], "闭集外的 mode 不得产生任何任务")
        self.assertEqual(self.search_answer_tasks(), [])

    def test_default_and_empty_mode_stay_local_assembly(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        explicit = self.app.answer_from_evidence("gradient", mode="")
        self.assertTrue(explicit["grounded"])
        recorded = self.search_answer_tasks()
        self.assertEqual(len(recorded), 1, "mode=\"\" 走既有 _recorded_operation 记录面")
        self.assertNotIn("deep_answer", recorded[0]["payload"])
        self.assertEqual(self.question_tasks(), [])

    def test_empty_packet_declines_before_key_check_with_zero_tasks(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            declined = self.app.answer_from_evidence("qqqq", mode="deep")
        self.assertEqual(
            declined,
            {
                "answer": INSUFFICIENT_ANSWER,
                "citations": [], "grounded": False, "mode": "declined",
                "query": "qqqq", "error_code": "deep_answer_evidence_unavailable",
                # D-14-RELAX：全阶零命中=none（诚实降级可观测）。
                "match_tier": "none",
            },
        )
        self.assertEqual(self.question_tasks(), [], "空证据零任务")
        self.assertEqual(self.search_answer_tasks(), [], "深度分派不入 recorded_operation")

    def test_missing_key_blocks_dispatch_without_task(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            with self.assertRaises(RuntimeError):
                self.app.answer_from_evidence("gradient", mode="deep")
        self.assertEqual(self.question_tasks(), [])

    def test_deep_dispatch_enqueues_shared_question_task(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        receipt = self.app.answer_from_evidence("gradient", course_ids=[COURSE], sub_id=SUB_A, mode="deep")
        tasks = self.question_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(receipt["mode"], "deep")
        self.assertEqual(receipt["task_id"], task["task_id"])
        self.assertTrue(receipt["created"])
        self.assertEqual(receipt["task_state"], "queued")
        self.assertEqual(receipt["input_hash"], task["payload"]["input_hash"])
        self.assertTrue(task["payload"]["deep_answer"] is True)
        self.assertEqual(task["payload"]["prompt_version"], "bookmark-answer-v1")
        self.assertTrue(str(task["config_key"]).startswith("palette:"))
        self.assertEqual(task["sub_id"], SUB_A)
        packet = task["payload"]["evidence"]
        self.assertTrue(packet and packet[0]["citation_id"])
        metadata = self.tasks.get_v3_metadata(task["task_id"]) or {}
        # dedupe_key 逐字公式因库级 UNIQUE 索引与「终态失败重发=新任务」冲突，
        # 取最小偏差：保留 question:palette:<hash> 前缀家族身份 + task_id 后缀。
        self.assertEqual(
            metadata.get("dedupe_key"),
            f"question:palette:{receipt['input_hash']}:{task['task_id']}",
        )
        self.assertEqual(metadata.get("privacy_state"), "sealed")
        self.assertEqual(metadata.get("requested_outputs"), ["answer"])
        self.assertEqual((task.get("progress") or {}).get("label"), "深度回答等待在线计算")

    def test_active_dedup_keeps_single_task_for_double_click(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        first = self.app.answer_from_evidence("gradient", mode="deep")
        second = self.app.answer_from_evidence("gradient", mode="deep")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertFalse(second["created"], "排队中同问同证据零双扣")
        self.assertEqual(len(self.question_tasks()), 1)

    def test_terminal_failure_redispatch_creates_new_task(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        first = self.app.answer_from_evidence("gradient", mode="deep")
        self.tasks.mark_terminal(first["task_id"], "failed", error="deepseek_answer_failed")
        second = self.app.answer_from_evidence("gradient", mode="deep")
        self.assertTrue(second["created"], "终态失败不拦截重发：重发即重试")
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.question_tasks()), 2)


class DeepQueueChainTests(_DeepHarness):
    """第三目标分支 + import_result palette 分支（P3-CONTRACT-1 §①/§③）。"""

    def test_palette_task_flows_through_shared_chain_and_lands_ready(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": "收敛性由压缩映射原理保证。", "grounded": True, "citations": ["c1"]},
        }
        self.coordinator.metrics = dict(DEEPSEEK_USAGE)
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed")
        self.assertEqual(final.get("error"), "")
        self.assertEqual((final.get("progress") or {}).get("label"), "深度回答已导入")
        self.assertEqual(final.get("deepseek_tokens"), 120, "消耗透镜对 palette 任务自动同权")
        # build_job 零改复用：同一条云端链、同 pipeline 版本（诚实记账）。
        job = self.coordinator.jobs[0]
        self.assertEqual(job["job_kind"], "learning_pack")
        self.assertEqual(job["requested_outputs"], ["answer"])
        self.assertEqual(job["pipeline"], {"version": "bookmark-answer-v1", "llm": "deepseek-flash"})
        self.assertEqual(job["payload"]["evidence"], _synthetic_packet())
        self.assertEqual(job["secrets"], {"deepseek_api_key": "sk-synthetic"})
        # 结果唯一落点=search_answers store 回调：ready 态全字段。
        self.assertEqual(len(self.save_calls), 1)
        record = self.save_calls[0]
        self.assertEqual(record["task_id"], task["task_id"])
        self.assertEqual(record["state"], "ready")
        self.assertTrue(record["grounded"])
        self.assertEqual(record["model"], "deepseek-flash")
        self.assertEqual(record["prompt_version"], "bookmark-answer-v1")
        self.assertEqual(record["error_code"], "")
        self.assertEqual(record["input_hash"], "a" * 64)
        self.assertEqual(record["course_ids"], [COURSE])
        self.assertEqual(record["sub_id"], SUB_A)
        self.assertEqual(record["citations"][0]["citation_id"], "c1")
        self.assertEqual(record["citations"][0]["start_seconds"], 15.0)
        self.assertEqual(record["citations"][0]["source_hash"], _synthetic_packet()[0]["source_hash"])

    def test_palette_insufficient_answer_lands_insufficient_state(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": INSUFFICIENT_ANSWER, "grounded": False, "citations": []},
        }
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed", "降级答案也是成功导入（宁缺勿假）")
        self.assertEqual(len(self.save_calls), 1)
        record = self.save_calls[0]
        self.assertEqual(record["state"], "insufficient")
        self.assertFalse(record["grounded"])
        self.assertEqual(record["answer"], INSUFFICIENT_ANSWER)
        self.assertEqual(record["citations"], [])

    def test_citation_outside_packet_is_rejected_by_client_gate(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": "编造引用的回答。", "grounded": True, "citations": ["fabricated"]},
        }
        self.drain_question_queue()
        self.assertEqual(len(self.save_calls), 1)
        record = self.save_calls[0]
        self.assertEqual(record["state"], "insufficient", "闭集外引用=双层门第二层拒绝")
        self.assertEqual(record["answer"], INSUFFICIENT_ANSWER)

    def test_terminal_failure_lands_failed_record_for_review(self):
        task = self.enqueue_palette_task()
        self.coordinator.failures = [RuntimeError("DeepSeek 429 rate limited")]
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "failed")
        self.assertEqual(final.get("error"), "deepseek_rate_limited")
        self.assertEqual(len(self.save_calls), 1)
        record = self.save_calls[0]
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_code"], "deepseek_rate_limited")
        self.assertEqual(record["answer"], "")
        self.assertEqual(record["model"], "")
        self.assertFalse(record["grounded"])

    def test_transient_failure_auto_retries_exactly_once(self):
        task = self.enqueue_palette_task()
        self.coordinator.failures = [RuntimeError("request timed out")]
        self.coordinator.outputs = {
            "answer": {"answer": "重试后答上了。", "grounded": True, "citations": ["c1"]},
        }
        controls: list[tuple[str, str]] = []

        def fake_control_task(_app, control_task_id, action):
            controls.append((str(control_task_id), str(action)))
            self.tasks.update_task(
                str(control_task_id), state="queued", resume_requested=False, error="", finished_at=None,
            )
            with self.app._lock:
                self.app._queue_persisted_task(self.tasks.get_task(str(control_task_id)))
            return {}

        with mock.patch.object(CourseLensApplication, "control_task", fake_control_task):
            self.drain_question_queue()
        self.assertEqual(controls, [(task["task_id"], "retry")], "瞬态失败自动重试恰一次")
        self.assertEqual(len(self.coordinator.jobs), 2)
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed")
        self.assertEqual(len(self.save_calls), 1, "重试在途不落 store，只有成功结果落 ready")
        self.assertEqual(self.save_calls[0]["state"], "ready")

    def test_memory_annotation_follows_bookmark_branch_shape(self):
        task = self.enqueue_palette_task()
        self.coordinator.outputs = {
            "answer": {"answer": "收敛性由压缩映射原理保证。", "grounded": True, "citations": ["c1"]},
        }
        self.coordinator.metrics = {"deepseek_tokens": 30, "course_memory_terms": 2}
        self.drain_question_queue()
        final = self.tasks.get_task(task["task_id"]) or {}
        self.assertEqual(final.get("state"), "completed")
        record = self.save_calls[0]
        self.assertEqual(record["state"], "ready")
        self.assertTrue(
            str(record["answer"]).startswith("收敛性由压缩映射原理保证。"),
            "记忆标注追加在答案正文之后（照书签分支同式）",
        )
        self.assertGreater(len(str(record["answer"])), len("收敛性由压缩映射原理保证。"))


class DeepAccountingTests(_DeepHarness):
    """H1 月计数双键 + count 前缀参（P3-CONTRACT-1 §⑤）。"""

    def test_ai_usage_month_counts_deep_and_quick_additively(self):
        self.seed_lecture(SUB_A, COURSE, "gradient")
        self.app.answer_from_evidence("gradient", mode="deep")
        self.app.answer_from_evidence("gradient")
        usage = self.app.ai_usage_month()
        self.assertEqual(usage["schema"], "courselens.ai-usage-month.v1", "schema 逐字不变")
        self.assertEqual(usage["deep_answers"], 1)
        self.assertEqual(usage["quick_answers"], 1)
        self.assertEqual(usage["questions"], 1)

    def test_count_tasks_created_since_prefix_defaults_to_old_behavior(self):
        month_start = time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d"))
        self.tasks.add_task("question", COURSE, "", {"n": 1}, config_key="palette:abc")
        self.tasks.add_task("question", COURSE, "", {"n": 2}, config_key="bookmark:def")
        self.tasks.add_task("search_answer", COURSE, "", {"n": 3}, config_key="legacy")
        self.assertEqual(self.tasks.count_tasks_created_since("question", month_start), 2)
        self.assertEqual(
            self.tasks.count_tasks_created_since("question", month_start, config_key_prefix=""),
            2,
            "缺省与显式空串=旧行为逐字",
        )
        self.assertEqual(
            self.tasks.count_tasks_created_since("question", month_start, config_key_prefix="palette:"),
            1,
        )
        self.assertEqual(
            self.tasks.count_tasks_created_since("question", month_start, config_key_prefix="bookmark:"),
            1,
        )
        self.assertEqual(
            self.tasks.count_tasks_created_since("search_answer", month_start, config_key_prefix="palette:"),
            0,
        )


if __name__ == "__main__":
    unittest.main()

class DeepQueryRelaxationTests(_DeepHarness):
    """D-14-RELAX 三阶检索阶梯（缺陷 D-20261009-14）。

    红钉已复现：自然问句原句 FTS 逐字 AND 零命中（修前光刻胶包裹问 0 命中、
    单词 5 命中，见 scratch .tmp-d14relax1/redpin.py）。本类钉修后行为：
    包裹问分派、单词 exact 零回归、全阶零命中诚实降级、析取合并排序。
    """

    def _seed_cjk_lecture(self, sub_id: str, texts: list[str]) -> None:
        """中文转写文档夹具：trigram 索引与 evidence_packet 时间戳全可用。"""
        self.store.sync_search_catalog([{
            "sub_id": sub_id,
            "course_id": COURSE,
            "course_title": f"course-{COURSE}",
            "lecture_title": f"lecture-{sub_id}",
            "teacher": "teacher",
            "catalog_version": "v1",
        }], prune=False)
        docs = []
        for index, text in enumerate(texts):
            docs.append({
                "doc_key": f"{sub_id}:transcript:{index * 15000}",
                "source": "transcript",
                "source_ref": "",
                "document_title": f"lecture-{sub_id}",
                "start_ms": index * 15000,
                "display_text": text,
                "search_text": text,
                "source_version": "v1",
            })
        self.store.replace_search_documents(sub_id, docs, indexed_version="v1")

    def test_wrapped_question_dispatches_with_content_and_tier(self):
        self._seed_cjk_lecture(SUB_A, ["光刻胶是光刻工艺的核心材料，分辨率决定线宽。"])
        receipt = self.app.answer_from_evidence(
            "光刻胶的作用是什么？", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
        )
        self.assertEqual(receipt.get("mode"), "deep", "包裹问不再 declined")
        self.assertEqual(receipt.get("match_tier"), "content_and")
        self.assertEqual(len(self.question_tasks()), 1, "派发到 task，成本仍走每次点击一调用帽")

    def test_single_word_query_stays_exact_tier(self):
        self._seed_cjk_lecture(SUB_A, ["光刻胶是光刻工艺的核心材料，分辨率决定线宽。"])
        receipt = self.app.answer_from_evidence(
            "光刻胶", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
        )
        self.assertEqual(receipt.get("mode"), "deep")
        self.assertEqual(receipt.get("match_tier"), "exact", "单词精确查询零回归")

    def test_disjunction_tier_merges_rows_without_cooccurrence(self):
        # 光刻胶与运用不同行出现：tier2 合取零命中 → tier3 析取两词皆回。
        self._seed_cjk_lecture(SUB_A, [
            "光刻胶是光刻工艺的核心材料。",
            "运用这批参数时要先做均匀性校准。",
        ])
        receipt = self.app.answer_from_evidence(
            "光刻胶是怎么运用的", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
        )
        self.assertEqual(receipt.get("mode"), "deep")
        self.assertEqual(receipt.get("match_tier"), "content_or")

    def test_topicless_question_stays_honestly_declined(self):
        self._seed_cjk_lecture(SUB_A, ["光刻胶是光刻工艺的核心材料。"])
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            declined = self.app.answer_from_evidence(
                "这节课的核心结论是什么？", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
            )
        self.assertEqual(declined.get("mode"), "declined", "无主题词问句=无内容可松弛，诚实降级保留")
        self.assertEqual(declined.get("match_tier"), "none")
        self.assertEqual(declined.get("error_code"), "deep_answer_evidence_unavailable")
        self.assertEqual(self.question_tasks(), [])

    def test_irrelevant_word_all_tiers_zero_still_declined(self):
        self._seed_cjk_lecture(SUB_A, ["光刻胶是光刻工艺的核心材料。"])
        declined = self.app.answer_from_evidence(
            "qqqq", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
        )
        self.assertEqual(declined.get("mode"), "declined", "误报护栏：转写完全不含的内容词仍拒绝")
        self.assertEqual(declined.get("match_tier"), "none")
        self.assertEqual(self.question_tasks(), [])

    def test_exact_tier_wins_when_original_sentence_hits(self):
        # 空格分词原句可命中时=exact，不降阶不误标。
        self._seed_cjk_lecture(SUB_A, ["光刻胶 显影 保真度 决定 图形质量。"])
        receipt = self.app.answer_from_evidence(
            "光刻胶 显影", course_ids=[COURSE], sub_id=SUB_A, mode="deep",
        )
        self.assertEqual(receipt.get("match_tier"), "exact")
