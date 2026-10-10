"""T11（夜14-R7 P2 → N15-R4 P1）：EvolvingEstimate 边界直测。

estimator.py(119 行)此前零直测（仅经 test_task_eta_budget 间接驱动）。
progress.py:336-338 重置直写与 :446-450 调用侧单调守卫依赖本模块的内部
不变量——这里把不变量钉成合同：单调守卫、离群钳制、live 界、EMA 权重、
快照零数据态、融合权重、非负钳制、fallback 外推。
"""

from __future__ import annotations

import unittest

from src.runtime.estimator import EvolvingEstimate, estimate_from_progress


class ObserveMonotonicGuardTests(unittest.TestCase):
    def test_regression_in_units_or_elapsed_is_ignored(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.observe(50.0, 100.0)
        estimator.observe(30.0, 50.0)  # 双双回退：内部计数必须保持非减
        self.assertEqual(estimator.completed_units, 50.0)
        self.assertEqual(estimator.elapsed_seconds, 100.0)
        self.assertEqual(estimator.observations, 1)

    def test_zero_or_negative_delta_counts_no_sample(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.observe(0.0, 0.0)
        estimator.observe(10.0, 10.0)
        estimator.observe(10.0, 50.0)  # 单位无推进：不计样本
        estimator.observe(20.0, 20.0)  # 时间无推进：不计样本
        self.assertEqual(estimator.observations, 1)
        self.assertIsNotNone(estimator.live_cost)

    def test_negative_inputs_are_clamped_to_zero(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.observe(-5.0, -5.0)
        self.assertEqual(estimator.completed_units, 0.0)
        self.assertEqual(estimator.elapsed_seconds, 0.0)
        self.assertEqual(estimator.observations, 0)


class OutlierClampTests(unittest.TestCase):
    def test_sample_outside_prior_octave_band_is_clamped(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.prior_costs = [100.0]
        estimator.observe(10.0, 10.0)  # 边际样本 1.0s/unit，先验 100 → 钳到 100/8
        self.assertAlmostEqual(estimator.live_cost, 12.5)

    def test_sample_above_prior_octave_band_is_clamped(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.prior_costs = [10.0]
        estimator.observe(10.0, 2000.0)  # 200s/unit，先验 10 → 钳到 80
        self.assertAlmostEqual(estimator.live_cost, 80.0)

    def test_live_cost_updates_bounded_to_half_to_one_and_half_band(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.observe(10.0, 100.0)  # live=10
        estimator.observe(11.0, 1000.0)  # 边际 900s/unit → 钳到 15 再进 EMA
        self.assertAlmostEqual(estimator.live_cost, 0.3 * 15.0 + 0.7 * 10.0)
        self.assertEqual(estimator.observations, 2)


class SnapshotContractTests(unittest.TestCase):
    def test_zero_data_snapshot_is_low_confidence_all_none(self):
        snapshot = EvolvingEstimate(profile_key="t", total_units=100.0).snapshot(now=1000.0)
        self.assertIsNone(snapshot["remaining_seconds"])
        self.assertIsNone(snapshot["total_seconds"])
        self.assertIsNone(snapshot["lower_seconds"])
        self.assertIsNone(snapshot["upper_seconds"])
        self.assertEqual(snapshot["confidence"], "low")

    def test_prior_live_fusion_weight_is_n_over_n_plus_three(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.prior_costs = [100.0]
        estimator.observe(1.0, 1.0)  # 样本钳到 12.5 → live=12.5, observations=1
        snapshot = estimator.snapshot(now=1000.0)
        expected_cost = 0.75 * 100.0 + 0.25 * 12.5  # 权重 = 1/(1+3)
        self.assertAlmostEqual(snapshot["remaining_seconds"], round(99.0 * expected_cost, 1))

    def test_consistent_samples_reach_high_confidence(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.prior_costs = [10.0, 10.0]
        for units in (1.0, 2.0, 3.0):
            estimator.observe(units, units * 10.0)
        snapshot = estimator.snapshot(now=1000.0)
        self.assertEqual(snapshot["confidence"], "high")

    def test_remaining_clamps_non_negative_past_total(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=10.0)
        estimator.prior_costs = [10.0]
        estimator.observe(20.0, 100.0)  # completed 超过 total
        snapshot = estimator.snapshot(now=1000.0)
        self.assertGreaterEqual(snapshot["remaining_seconds"], 0.0)
        self.assertGreaterEqual(snapshot["total_seconds"], snapshot["elapsed_seconds"])

    def test_nonpositive_prior_costs_are_ignored_by_median(self):
        estimator = EvolvingEstimate(profile_key="t", total_units=100.0)
        estimator.prior_costs = [0.0, -5.0, 100.0, 200.0]  # 非正先验不进中位数
        snapshot = estimator.snapshot(now=1000.0)
        self.assertAlmostEqual(snapshot["remaining_seconds"], round(100.0 * 150.0, 1))


class EstimateFromProgressFallbackTests(unittest.TestCase):
    def test_percent_elapsed_extrapolation(self):
        value = estimate_from_progress({"percent": 50, "elapsed_seconds": 10})
        self.assertEqual(value["total_seconds"], 20.0)
        self.assertEqual(value["remaining_seconds"], 10.0)
        self.assertEqual(value["confidence"], "medium")

    def test_percent_clamped_and_remaining_non_negative(self):
        value = estimate_from_progress({"percent": 200, "elapsed_seconds": 10})
        self.assertEqual(value["total_seconds"], 10.0)
        self.assertEqual(value["remaining_seconds"], 0.0)
        self.assertEqual(value["lower_seconds"], 0.0)

    def test_prior_seconds_fallback_when_percent_too_low(self):
        value = estimate_from_progress({"percent": 0, "elapsed_seconds": 0}, prior_seconds=90.0)
        self.assertEqual(value["total_seconds"], 90.0)
        self.assertEqual(value["remaining_seconds"], 90.0)
        self.assertEqual(value["confidence"], "low")

    def test_no_signal_yields_all_none(self):
        value = estimate_from_progress({})
        self.assertIsNone(value["total_seconds"])
        self.assertIsNone(value["remaining_seconds"])
        self.assertEqual(value["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
