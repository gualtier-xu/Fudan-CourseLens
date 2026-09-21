import json
import unittest
from unittest.mock import patch

from courselens_worker.llm import PROOFREAD_PAIRING, answer_question, create_summary, proofread_segments


class LLMCheckpointTests(unittest.TestCase):
    def test_answer_question_rejects_unreferenced_or_empty_evidence(self):
        self.assertFalse(answer_question("key", query="q", evidence=[])["grounded"])

    def test_answer_question_preserves_only_known_citation_ids(self):
        with patch("courselens_worker.llm._chat", return_value='{"answer":"回答","grounded":true,"citations":["r1","fake"]}'):
            value = answer_question("key", query="q", evidence=[{"citation_id": "r1", "text": "证据"}])
        self.assertTrue(value["grounded"])
        self.assertEqual(value["citations"], ["r1"])

    def test_answer_prompt_requires_full_answer_and_solution_approach(self):
        """C⑩ 用户拍板：完整答案+每题解题思路；提示词必须承载该要求。"""
        with patch("courselens_worker.llm._chat", return_value='{"answer":"回答","grounded":true,"citations":["r1"]}') as chat:
            answer_question("key", query="q", evidence=[{"citation_id": "r1", "text": "证据"}])
        system = chat.call_args.args[1][0]["content"]
        self.assertIn("完整答案", system)
        self.assertIn("解题思路", system)
        self.assertEqual(chat.call_args.kwargs.get("max_tokens"), 8192,
                         "完整答案+思路需要更大的输出预算（8192）")

    def test_proofread_resumes_after_completed_window(self):
        source = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"文本{index}"}
            for index in range(25)
        ]
        prior_segments = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"已{index}"}
            for index in range(20)
        ]
        response = json.dumps([
            {"id": f"p{index}", "old": f"文本{index}", "new": f"修正{index}"}
            for index in range(20, 25)
        ])
        checkpoints = []
        with patch("courselens_worker.llm._chat", return_value=response) as chat:
            result = proofread_segments(
                "secret",
                source,
                source,
                prior_checkpoint={
                    "proofread_pairing": PROOFREAD_PAIRING,
                    "proofread_completed_windows": 1,
                    "proofread_segments": prior_segments,
                },
                checkpoint=checkpoints.append,
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(len(result), 25)
        self.assertEqual(result[-1]["text"], "修正24")
        self.assertEqual(result[-1]["correction"], "applied")
        self.assertEqual(result[0]["text"], "已0")
        self.assertEqual(checkpoints[-1]["proofread_completed_windows"], 2)
        self.assertEqual(checkpoints[-1]["proofread_pairing"], PROOFREAD_PAIRING)

    def test_proofread_legacy_checkpoint_restarts_without_trusting_old_segments(self):
        source = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"文本{index}"}
            for index in range(5)
        ]
        checkpoints = []
        with patch("courselens_worker.llm._chat", return_value=json.dumps([])) as chat:
            result = proofread_segments(
                "secret",
                source,
                source,
                prior_checkpoint={
                    "proofread_completed_windows": 1,
                    "proofread_segments": [
                        {"start_ms": 0, "end_ms": 1000, "text": "stale-legacy-rewrite"}
                    ],
                },
                checkpoint=checkpoints.append,
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([item["text"] for item in result], [f"文本{index}" for index in range(5)])
        self.assertNotIn("stale-legacy-rewrite", [item["text"] for item in result])
        self.assertEqual(checkpoints[-1]["proofread_pairing"], PROOFREAD_PAIRING)

    def test_proofread_resume_passes_slide_context_for_remaining_windows(self):
        source = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"文本{index}"}
            for index in range(25)
        ]
        prior_segments = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"已{index}"}
            for index in range(20)
        ]
        payloads = []

        def fake_chat(api_key, messages, **kwargs):
            payloads.append(json.loads(messages[1]["content"]))
            return json.dumps([])

        checkpoints = []
        with patch("courselens_worker.llm._chat", side_effect=fake_chat) as chat:
            proofread_segments(
                "secret",
                source,
                source,
                prior_checkpoint={
                    "proofread_pairing": PROOFREAD_PAIRING,
                    "proofread_completed_windows": 1,
                    "proofread_segments": prior_segments,
                },
                checkpoint=checkpoints.append,
                ppt_pages=[{"created_sec": 0, "text": "幻灯片术语"}],
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(payloads[0][0]["id"], "p20")
        self.assertEqual(payloads[0][0]["slide"], "幻灯片术语")
        self.assertEqual(checkpoints[-1]["proofread_pairing"], PROOFREAD_PAIRING)

    def test_summary_resumes_map_windows_before_final_merge(self):
        transcript = [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": f"text-{index}"}
            for index in range(240)
        ]
        first_part = {"markdown": "part one", "chapters": []}
        second_part = {"markdown": "part two", "chapters": []}
        final = {
            "markdown": "combined",
            "chapters": [{"title": "chapter", "start_ms": 120000, "summary": "summary"}],
        }
        checkpoints = []
        with patch(
            "courselens_worker.llm._chat",
            side_effect=[json.dumps(second_part), json.dumps(final)],
        ) as chat:
            result = create_summary(
                "secret",
                title="title",
                transcript=transcript,
                ppt_pages=[],
                prior_checkpoint={
                    "summary_completed_windows": 1,
                    "summary_parts": [first_part],
                },
                checkpoint=checkpoints.append,
            )
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result["markdown"], "combined")
        self.assertEqual(result["chapters"][0]["start_ms"], 120000)
        self.assertEqual(checkpoints[-1]["summary_completed_windows"], 2)


if __name__ == "__main__":
    unittest.main()
