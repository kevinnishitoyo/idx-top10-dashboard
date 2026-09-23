import unittest

import pandas as pd

from pipeline import (
    calculate_rsi,
    classify_flow_check,
    is_valid_idx_price,
    rising_over_sessions,
    round_idx_price,
    trade_scenario,
)


class TradeScenarioTests(unittest.TestCase):
    def test_ma20_test_uses_structural_stop_and_resistance_cap(self):
        result = trade_scenario(
            "Bullish", 1000, 940, 1200, 100, 980, 900, True
        )
        self.assertEqual(result["setup"], "Pullback long (test MA20 from above)")
        self.assertEqual(result["setup_status"], "At trigger")
        self.assertLess(result["stop"], 940)
        self.assertEqual(result["resistance_target"], 1200)
        self.assertGreater(result["target"], result["resistance_target"])
        self.assertGreaterEqual(result["reward_to_resistance"], 1.0)
        self.assertAlmostEqual(result["reward_to_risk"], 2.0)

    def test_pending_reclaim_is_not_counted_as_at_trigger(self):
        result = trade_scenario(
            "Mixed", 960, 940, 1200, 80, 980, 900, True
        )
        self.assertEqual(result["setup"], "Pullback long (pending MA20 reclaim)")
        self.assertEqual(result["setup_status"], "Pending")
        self.assertGreater(result["entry"], 960)

    def test_weak_regime_rejects_pullback(self):
        result = trade_scenario(
            "Mixed", 960, 940, 1200, 80, 980, 990, False
        )
        self.assertEqual(result["setup"], "No long setup (uptrend regime not confirmed)")
        self.assertEqual(result["setup_status"], "Watch only")
        self.assertIsNotNone(result["entry"])

    def test_support_test_requires_half_atr_proximity(self):
        near = trade_scenario("Mixed", 950, 940, 1200, 40, 1020, 900, True)
        far = trade_scenario("Mixed", 970, 940, 1200, 40, 1020, 900, True)
        self.assertEqual(near["setup"], "Pullback long (test support)")
        self.assertEqual(far["setup"], "No setup (mid-range, no trigger nearby)")

    def test_poor_reward_to_resistance_is_rejected(self):
        result = trade_scenario(
            "Bullish", 1000, 900, 1100, 100, 980, 900, True
        )
        self.assertEqual(result["setup"], "No setup (insufficient room to resistance)")
        self.assertEqual(result["setup_status"], "Watch only")
        self.assertIsNotNone(result["target"])
        self.assertIsNone(result["reward_to_risk"])

    def test_realistic_ammn_pullback_qualifies(self):
        result = trade_scenario(
            "Bullish", 4710, 4270, 5275, 220, 4647, 4355, True, 4610
        )
        self.assertEqual(result["setup"], "Pullback long (test MA20 from above)")
        self.assertEqual(result["setup_status"], "At trigger")
        self.assertLess(result["stop"], 4610)
        self.assertGreaterEqual(result["reward_to_resistance"], 1.0)

    def test_extended_breakout_is_watch_only(self):
        result = trade_scenario(
            "Bullish", 1300, 1000, 1200, 100, 1100, 1000, True, 1180
        )
        self.assertEqual(result["setup"], "No setup (extended above breakout)")
        self.assertEqual(result["setup_status"], "Watch only")
        self.assertIsNone(result["reward_to_risk"])

    def test_corporate_action_blocks_setup(self):
        result = trade_scenario(
            "Bullish", 1000, 900, 1200, 80, 980, 900, True, 950, True
        )
        self.assertEqual(
            result["setup"], "No setup (corporate action in 50-session window)"
        )
        self.assertEqual(result["setup_status"], "Watch only")

    def test_mid_range_row_keeps_watch_levels(self):
        result = trade_scenario(
            "Bullish", 1050, 900, 1200, 80, 1000, 900, True
        )
        self.assertEqual(result["setup"], "No setup (mid-range, no trigger nearby)")
        self.assertEqual(result["setup_status"], "Watch only")
        for key in ("entry", "stop", "target"):
            self.assertIsNotNone(result[key])
        self.assertIsNone(result["reward_to_risk"])

    def test_breakout_levels_are_valid_ticks(self):
        result = trade_scenario(
            "Bullish", 1180, 1000, 1200, 100, 1050, 950, True
        )
        self.assertEqual(result["setup"], "Breakout long")
        for key in ("entry", "stop", "target"):
            self.assertTrue(is_valid_idx_price(result[key]), (key, result[key]))

    def test_tick_boundary_rounding(self):
        self.assertEqual(round_idx_price(4995, "up"), 5000)
        self.assertEqual(round_idx_price(5010, "down"), 5000)
        self.assertEqual(round_idx_price(199.5, "up"), 200)

    def test_zero_loss_rsi_is_100(self):
        values = pd.Series(range(1, 31), dtype=float)
        self.assertEqual(calculate_rsi(values).iloc[-1], 100.0)

    def test_rising_slope_uses_five_sessions(self):
        self.assertTrue(rising_over_sessions(pd.Series([100, 100, 100, 100, 100, 101]), 5))
        self.assertFalse(rising_over_sessions(pd.Series([101, 100, 100, 100, 100, 100.5]), 5))

    def test_flow_check_requires_five_percent_on_both_windows(self):
        self.assertEqual(classify_flow_check("Pullback long", 0.06, 0.07), "Flow confirms")
        self.assertEqual(classify_flow_check("Pullback long", 0.001, 0.07), "Flow neutral/mixed")
        self.assertEqual(classify_flow_check("Pullback long", -0.06, -0.05), "Flow diverges")


if __name__ == "__main__":
    unittest.main()
