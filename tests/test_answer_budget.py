"""AS3 回归钉：解释/解答条数门（answer_budget）整链退役 + 日 token 档位含「不限」。

原 N5A-P4 日预算链已按用户拍板移除（每条解答成本厘级、无批量触发面、从无
设置 UI）；本文件钉三件事：
① 两链（书签解释 explain_question_bookmark / 考核解答 explain_assessment_item）
   方法体内无任何 budget 拦截；
② answer_budget 在后端源码、HTTP 快照动作面、前端码表零残留；
③ max_deepseek_tokens 既有网格钉保留 + 哨兵 0（不限档）新钉。
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from src.application import (
    MAX_DEEPSEEK_TOKENS_DEFAULT,
    MAX_DEEPSEEK_TOKENS_STEP,
    CourseLensApplication,
)
from src.runtime.task_store import TaskStore
from tests.frontend_family import family_text

ROOT = Path(__file__).resolve().parent.parent


class _BudgetApplication:
    """窄适配器：复用真实预算方法与归一化，不装配整个应用。"""

    max_deepseek_tokens_limit = CourseLensApplication.max_deepseek_tokens_limit
    set_max_deepseek_tokens = CourseLensApplication.set_max_deepseek_tokens
    ai_usage_month = CourseLensApplication.ai_usage_month
    _normalize_query_text = staticmethod(CourseLensApplication._normalize_query_text)
    _bookmark_input_hash = staticmethod(CourseLensApplication._bookmark_input_hash)

    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")


class AnswerBudgetRetiredTests(unittest.TestCase):
    """AS3：条数门退役回归钉——两链无拦截、全表面零残留。"""

    def test_budget_seams_are_gone_from_the_class(self):
        for seam in ("answer_budget_limit", "set_answer_budget", "_answer_budget_allows"):
            self.assertFalse(hasattr(CourseLensApplication, seam), f"{seam} 应已整链退役")

    def test_both_answer_chains_have_no_budget_gate(self):
        for chain in (CourseLensApplication.explain_question_bookmark,
                      CourseLensApplication.explain_assessment_item):
            source = inspect.getsource(chain)
            self.assertNotIn("budget", source.lower(), f"{chain.__name__} 方法体内不得再有 budget 拦截")

    def test_answer_budget_leaves_zero_residue_in_product_surfaces(self):
        surfaces = (
            "src/application.py", "src/app.py", "src/services/container.py",
            "src/services/domains.py", "src/runtime/http_api.py",
            "frontend/modules/api.js", "frontend/modules/course-review.js",
            "frontend/index.html", "frontend/modules/settings.js",
        )
        for relative in surfaces:
            if relative in ("frontend/modules/settings.js", "frontend/modules/course-review.js"):
                # 家族并集：拆分后残留检查必须覆盖门面+子模块
                # （ARCH-DEBT-1 settings / ARCH-DEBT-2 course-review）
                text = family_text("settings" if relative.endswith("settings.js") else "course-review")
            else:
                text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("answer_budget", text, f"{relative} 不得残留 answer_budget")
            self.assertNotIn("answer-budget", text, f"{relative} 不得残留 answer-budget 动作名")

    def test_retired_kv_key_stays_inert_without_migration(self):
        """旧键 answer_budget.v1 留库惰性：无读路径、无迁移；写入不复活任何门。"""
        self.assertNotIn("answer_budget.v1", (ROOT / "src" / "application.py").read_text(encoding="utf-8"))

    def test_query_normalization_unifies_width_and_space(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = _BudgetApplication(Path(tmp.name))
        normalize = app._normalize_query_text
        self.assertEqual(normalize("ｆｕｌｌｗｉｄｔｈ"), "fullwidth")
        self.assertEqual(normalize("多次　  空白 与，全角"), "多次 空白 与,全角")
        self.assertEqual(normalize("  两边  "), "两边")
        a = app._bookmark_input_hash("ａｂｃ　测试", [{"e": 1}])
        b = app._bookmark_input_hash("abc 测试", [{"e": 1}])
        self.assertEqual(a, b, "全半角与空白差异应归一到同一哈希")


class MaxDeepSeekTokensTests(unittest.TestCase):
    """⑨（C⑧ 修订+AS3 不限档）：日 token 总量上限——闭集网格校验、读取回整、
    哨兵 0=不限照实透传。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = _BudgetApplication(Path(tmp.name))

    def test_default_is_100k(self):
        self.assertEqual(self.app.max_deepseek_tokens_limit(), MAX_DEEPSEEK_TOKENS_DEFAULT)

    def test_grid_values_round_trip(self):
        for tokens in (100_000, 500_000, 1_000_000):
            self.assertEqual(self.app.set_max_deepseek_tokens(tokens)["max_deepseek_tokens"], tokens)
            self.assertEqual(self.app.max_deepseek_tokens_limit(), tokens)
        self.assertEqual(
            self.app.set_max_deepseek_tokens(str(300_000))["max_deepseek_tokens"], 300_000,
            "字符串数字同样接受（与原条数闸同口径）",
        )

    def test_zero_sentinel_means_unlimited(self):
        """AS3：哨兵 0=不限——写入/回读/快照字段照实透传 0。"""
        self.assertEqual(self.app.set_max_deepseek_tokens(0)["max_deepseek_tokens"], 0)
        self.assertEqual(self.app.max_deepseek_tokens_limit(), 0)
        self.assertEqual(self.app.set_max_deepseek_tokens("0")["max_deepseek_tokens"], 0)
        self.assertEqual(self.app.max_deepseek_tokens_limit(), 0)

    def test_off_grid_values_are_rejected(self):
        for bad in (50_000, 150_000, 1_100_000, -100_000, "abc", None):
            with self.assertRaises(ValueError):
                self.app.set_max_deepseek_tokens(bad)

    def test_out_of_range_stored_value_falls_back_to_default(self):
        self.app.task_store.set_app_state("max_deepseek_tokens.v1", 7)
        self.assertEqual(self.app.max_deepseek_tokens_limit(), MAX_DEEPSEEK_TOKENS_DEFAULT)
        self.assertEqual(MAX_DEEPSEEK_TOKENS_STEP, 100_000)


class AiUsageMonthTests(unittest.TestCase):
    """Z2：本月 AI 用量视图的数据面（本地聚合，零外联；AS3 保留展示）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = _BudgetApplication(Path(tmp.name))

    def test_month_counts_only_ai_kinds(self):
        self.app.task_store.add_task("question", "c1", "s1", {})
        self.app.task_store.add_task("question", "c1", "s2", {})
        self.app.task_store.add_task("summary", "c1", "s3", {})
        self.app.task_store.add_task("subtitle", "c1", "s4", {})
        usage = self.app.ai_usage_month()
        self.assertEqual(usage["questions"], 2)
        self.assertEqual(usage["summaries"], 1)
        self.assertRegex(usage["month"], "[0-9]{4}-[0-9]{2}")


if __name__ == "__main__":
    unittest.main()
