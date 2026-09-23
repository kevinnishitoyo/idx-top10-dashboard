import unittest

from pipeline import is_valid_idx_price, trade_scenario


class TradeScenarioTests(unittest.TestCase):
    def test_ma20_test_uses_structural_stop_and_resistance_cap(self):
        result = trade_scenario(
            "Bullish", 1000, 940, 1200, 100, 980, 900, True
        )
        self.assertEqual(result["setup"], "Pullback long (test MA20 from above)")
        self.assertEqual(result["setup_status"], "At trigger")
        self.assertLess(result["stop"], 940)
        self.assertLessEqual(result["target"], 1200)
        self.assertGreaterEqual(result["reward_to_risk"], 1.5)

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
        self.assertIsNone(result["entry"])

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
        self.assertIsNone(result["target"])

    def test_breakout_levels_are_valid_ticks(self):
        result = trade_scenario(
            "Bullish", 1180, 1000, 1200, 100, 1050, 950, True
        )
        self.assertEqual(result["setup"], "Breakout long")
        for key in ("entry", "stop", "target"):
            self.assertTrue(is_valid_idx_price(result[key]), (key, result[key]))


if __name__ == "__main__":
    unittest.main()
