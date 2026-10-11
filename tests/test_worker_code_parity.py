"""钉稿一阶段1（夜14-R7 T10 → N15-R4 P0 重排第 4 位）：worker 码表 → 前端
文案表 parity。零产品改动（纯静态扫描三源文本）。

- 齿1（emittership 正向）：``worker/courselens_worker/runner.py`` 的
  ``_ASR_ERROR_CODES`` 值集 ⊆ 前端 ``TASK_FAILURE_GUIDANCE`` 键集 ∪ 显式
  ALLOWLIST——worker 新增失败码而前端未给文案也未登记 → 红（学生永不看裸码）。
  干跑基线（N15-R4，2026-10-04）：29 码今日全部落 ``TASK_FAILURE_GUIDANCE[...]
  || GENERIC_TASK_FAILURE`` 通用兜底 → ALLOWLIST 种子＝全部 29 码显式登记。
- 齿2（exactly-one 处置）：每枚 worker 码必须有且只有一种处置——在文案表
  （有学生文案）或在 ALLOWLIST（显式「通用兜底」登记）。两处并存=清单腐化。
- 齿3（allowlist 卫生反向）：ALLOWLIST ⊆ worker 可发集——worker 删码后
  陈旧 allowlist 条目必须同步清理，防清单单向膨胀。
- 方法纪律：码表抽取必须锚定字典边界（``_ASR_ERROR_CODES = {`` 起至配对
  ``}``），禁全文件扫值（N15-R4 方法教训：全文件扫会把阶段名误入码集）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_SOURCE = (ROOT / "worker" / "courselens_worker" / "runner.py").read_text(encoding="utf-8")
TASK_CARDS_SOURCE = (
    ROOT / "frontend" / "modules" / "tasks-drawer" / "task-cards.js"
).read_text(encoding="utf-8")

# 干跑种子（N15-R4）：今日全部落通用兜底的 29 码显式登记。worker 新增码时
# 本集合不动、齿1 即红——须二选一：给文案（同时从这挪走）或显式在此登记。
WORKER_CODE_ALLOWLIST = frozenset({
    "duration_probe_timeout", "duration_probe_failed",
    "model_incomplete", "model_tokens_missing",
    "unsupported_backend", "unsupported_mode",
    "invalid_start", "invalid_duration", "checkpoint_mode_mismatch",
    "media_decode_timeout", "media_decode_failed", "media_prefetch_incomplete",
    "media_http_401", "media_http_403", "media_http_404", "media_http_429",
    "media_http_4xx", "media_http_5xx", "media_http_status_rejected",
    "media_content_html", "media_content_json",
    "media_magic_rejected", "media_index_unreadable", "media_format_rejected",
    "media_redirect_rejected", "media_connection_failed",
    "media_proxy_target_rejected", "media_proxy_request_rejected",
    "media_range_invalid",
})


def _extract_runner_code_table() -> dict[str, str]:
    """锚定 ``_ASR_ERROR_CODES = {`` 字典边界抽取 NL→码映射（非全文件扫）。"""
    anchor = "_ASR_ERROR_CODES = {"
    start = RUNNER_SOURCE.index(anchor) + len(anchor)
    end = RUNNER_SOURCE.index("\n}", start)
    block = RUNNER_SOURCE[start:end]
    pairs = dict(re.findall(r'"([^"]+)":\s*"([a-z0-9_]+)"', block))
    return pairs


def _extract_frontend_guidance_keys() -> set[str]:
    anchor = "const TASK_FAILURE_GUIDANCE = Object.freeze({"
    start = TASK_CARDS_SOURCE.index(anchor) + len(anchor)
    block = TASK_CARDS_SOURCE[start:]
    block = block[:block.index("\n});")]
    return set(re.findall(r"([a-z0-9_]+): \"", block))


class WorkerCodeParityTests(unittest.TestCase):
    def setUp(self):
        self.table = _extract_runner_code_table()
        self.worker_codes = set(self.table.values())
        self.guidance_keys = _extract_frontend_guidance_keys()

    def test_runner_table_extraction_is_anchored_and_populated(self):
        # 抽取器哨兵：锚定提取必须拿到非空表且键值一一对应（29 基线带下限）。
        self.assertGreaterEqual(len(self.table), 25, "码表边界锚定失效（提取数骤减）")
        self.assertEqual(len(self.table), len(set(self.table.values())), "码值必须互异（闭集前提）")
        for natural, code in self.table.items():
            self.assertRegex(code, r"^[a-z0-9_]+$", f"码值形状越闭集: {code}")
            self.assertTrue(natural, "NL 键不得为空")

    def test_every_worker_code_has_guidance_copy_or_explicit_allowlist_entry(self):
        missing = {
            code for code in self.worker_codes
            if code not in self.guidance_keys and code not in WORKER_CODE_ALLOWLIST
        }
        self.assertEqual(
            missing, set(),
            "worker 新增失败码未给前端文案也未登记 ALLOWLIST（学生会看到裸码兜底）",
        )

    def test_each_worker_code_has_exactly_one_disposition(self):
        both = self.worker_codes & self.guidance_keys & set(WORKER_CODE_ALLOWLIST)
        self.assertEqual(
            both, set(),
            "同码两处处置（文案表+ALLOWLIST 并存）＝清单腐化，须二选一",
        )

    def test_allowlist_tracks_worker_codes_without_rot(self):
        stale = set(WORKER_CODE_ALLOWLIST) - self.worker_codes
        self.assertEqual(
            stale, set(),
            "ALLOWLIST 含 worker 已删除的码（陈旧条目须同步清理）",
        )

    def test_frontend_guidance_table_extraction_is_anchored(self):
        # 前端表哨兵：锚定提取必须命中既有键（表被改名/搬走时先红在此，
        # 而不是让齿1 因空集假绿——空 guidance_keys 会让全部码落入 allowlist）。
        self.assertIn("timeout", self.guidance_keys)
        self.assertIn("task_failed", self.guidance_keys)
        self.assertGreaterEqual(len(self.guidance_keys), 20)


if __name__ == "__main__":
    unittest.main()
