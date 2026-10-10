"""RR-QWIN-1 Q2 回归钉：提问链（疑问解释/考核解答）客户端面健壮性。

C2 同族位点闭集化（AI-RESEARCH-1 P5 客户端半边）：
① 出站载荷预检——超长 query 安全降级（截断出站文本）、证据超限本地
   fail-fast 闭集码 question_payload_oversized，不再裸奔上云空跑一整轮；
② LLM 瞬态失败（坏形状/超时）自动重试恰一次，429 限流绝不入重试集合；
③ worker LLM 失败的实际到达形态（GitHubRemoteError+签名码）落瞬态集合。
"""

from __future__ import annotations

import inspect
import unittest

from src.application import (
    QUESTION_EVIDENCE_MAX_CHARS,
    QUESTION_EVIDENCE_MAX_ITEMS,
    QUESTION_PAYLOAD_OVERSIZED,
    QUESTION_QUERY_MAX_CHARS,
    _QUESTION_TRANSIENT_RETRY_CODES,
    _question_payload_preflight,
    CourseLensApplication,
)
from src.remote.github_client import GitHubRemoteError


class QuestionPayloadPreflightTests(unittest.TestCase):
    def test_normal_payload_passes_unchanged(self):
        evidence = [{"citation_id": "c1", "text": "链式聚合的要点"}]
        query, code = _question_payload_preflight("为什么链式聚合怕水？", evidence)
        self.assertEqual((query, code), ("为什么链式聚合怕水？", ""))

    def test_oversized_query_is_truncated_not_failed(self):
        """学生问题文本超长=安全降级：只截出站文本（书签笔记本体不动），
        不给学生吃失败。"""
        query, code = _question_payload_preflight("问" * (QUESTION_QUERY_MAX_CHARS + 500), [])
        self.assertEqual(code, "")
        self.assertEqual(len(query), QUESTION_QUERY_MAX_CHARS)

    def test_evidence_over_item_cap_fails_fast_locally(self):
        evidence = [
            {"citation_id": f"c{index}", "text": "一句话"} for index in range(QUESTION_EVIDENCE_MAX_ITEMS + 1)
        ]
        _, code = _question_payload_preflight("问", evidence)
        self.assertEqual(code, QUESTION_PAYLOAD_OVERSIZED)

    def test_evidence_over_char_cap_fails_fast_locally(self):
        evidence = [
            {"citation_id": f"c{index}", "text": "字" * 7000} for index in range(10)
        ]
        _, code = _question_payload_preflight("问", evidence)
        self.assertEqual(code, QUESTION_PAYLOAD_OVERSIZED)

    def test_worker_inadmissible_items_are_not_counted(self):
        """预检口径与 worker answer_question 准入过滤一致：citation_id/text
        缺失的条目根本不会被发出去，不参与计数（不误伤合法载荷）。"""
        evidence = [
            {"citation_id": "", "text": "无引用"} for _ in range(QUESTION_EVIDENCE_MAX_ITEMS + 10)
        ] + [{"citation_id": "c1", "text": ""} for _ in range(10)]
        _, code = _question_payload_preflight("问", evidence)
        self.assertEqual(code, "")


class _StubTaskStore:
    def __init__(self):
        self.states: dict[str, object] = {}

    def get_app_state(self, key, default=None):
        return self.states.get(key, default)

    def set_app_state(self, key, value):
        self.states[key] = value


class _StubApp:
    def __init__(self):
        self.task_store = _StubTaskStore()
        self.controls: list[tuple[str, str]] = []

    def control_task(self, task_id, action):
        self.controls.append((str(task_id), str(action)))
        return {}


class TransientAutoRetryTests(unittest.TestCase):
    def test_transient_codes_are_a_closed_set_without_rate_limit(self):
        self.assertEqual(
            _QUESTION_TRANSIENT_RETRY_CODES,
            {"remote_answer_failed", "deepseek_answer_failed", "remote_answer_timeout"},
        )
        # 429 限流绝不自动重试（不 hammer 学生自己的配额）；
        # 授权/setup 类显式码也绝不越权重试。
        self.assertNotIn("deepseek_rate_limited", _QUESTION_TRANSIENT_RETRY_CODES)
        self.assertNotIn("cloud_setup_required", _QUESTION_TRANSIENT_RETRY_CODES)

    def test_retry_happens_exactly_once_per_task(self):
        app = _StubApp()
        self.assertTrue(
            CourseLensApplication._auto_retry_question_transient_once(app, "task-1")
        )
        self.assertEqual(app.controls, [("task-1", "retry")])
        # 同一任务第二次瞬态失败：标记已用，放行终态失败。
        self.assertFalse(
            CourseLensApplication._auto_retry_question_transient_once(app, "task-1")
        )
        self.assertEqual(app.controls, [("task-1", "retry")])
        # 其他任务仍有自己的恰好一次。
        self.assertTrue(
            CourseLensApplication._auto_retry_question_transient_once(app, "task-2")
        )

    def test_worker_llm_failure_reaches_the_transient_set(self):
        """worker LLMError 的实际到达形态：run failure + 签名码 worker_failed →
        remote_answer_failed（在瞬态集合里→拿自动重试）。"""
        coded = CourseLensApplication._question_error_code(
            GitHubRemoteError("GitHub runner concluded with failure", code="worker_failed")
        )
        self.assertEqual(coded, "remote_answer_failed")
        self.assertIn(coded, _QUESTION_TRANSIENT_RETRY_CODES)
        self.assertIn(
            CourseLensApplication._question_error_code(Exception("request timed out")),
            _QUESTION_TRANSIENT_RETRY_CODES,
        )
        self.assertNotIn(
            CourseLensApplication._question_error_code(Exception("HTTP 429 rate limited")),
            _QUESTION_TRANSIENT_RETRY_CODES,
        )


class PreflightWiringPins(unittest.TestCase):
    def test_both_answer_chains_preflight_before_dispatch(self):
        bookmark_body = inspect.getsource(CourseLensApplication.explain_question_bookmark)
        assessment_body = inspect.getsource(CourseLensApplication.explain_assessment_item)
        self.assertIn("_question_payload_preflight", bookmark_body)
        self.assertIn("_question_payload_preflight", assessment_body)
        # 预检都落在 payload 构造之前（书签链=入队 payload；考核链=出站载荷）。
        self.assertLess(
            bookmark_body.index("_question_payload_preflight"),
            bookmark_body.index("payload = {"),
        )
        self.assertLess(
            assessment_body.index("_question_payload_preflight"),
            assessment_body.index("payload = {"),
        )

    def test_queue_except_branch_has_bounded_transient_retry(self):
        queue_body = inspect.getsource(CourseLensApplication._run_question_queue)
        self.assertIn("_QUESTION_TRANSIENT_RETRY_CODES", queue_body)
        self.assertIn("_auto_retry_question_transient_once", queue_body)


if __name__ == "__main__":
    unittest.main()
