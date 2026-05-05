import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ict_core import FVGDetector, ICTSetupBuilder, LiquidityDetector, OrderBlockDetector, SwingDetector
from ict_core.models import Candle, SwingPoint


def c(i, open_, high, low, close):
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(minutes=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


class ICTCoreTests(unittest.TestCase):
    def test_detects_bullish_and_bearish_fvg(self):
        candles = [
            c(0, 10, 11, 9, 10),
            c(1, 10, 10.5, 9.5, 10),
            c(2, 13, 14, 12, 13),
            c(3, 13, 13.5, 12.5, 13),
            c(4, 9, 9.5, 8, 8.5),
        ]

        fvgs = FVGDetector.find_fvgs(candles)

        self.assertTrue(any(f.direction == "bullish" and f.lower == 11 and f.upper == 12 for f in fvgs))
        self.assertTrue(any(f.direction == "bearish" and f.lower == 9.5 and f.upper == 12 for f in fvgs))

    def test_order_block_uses_last_bearish_candle_before_displacement(self):
        candles = [
            c(0, 99, 100, 98, 99),
            c(1, 100, 101, 99, 100),
            c(2, 101, 104, 100, 102),
            c(3, 101, 102, 99, 100),
            c(4, 100, 101, 99, 100),
            c(5, 100, 101, 96, 97),
            c(6, 97, 110, 96, 109),
        ]
        swings = SwingDetector.find_swings(candles)

        obs = OrderBlockDetector.find_bullish_order_blocks(candles, swings, "1h")

        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].index, 5)
        self.assertEqual(obs[0].low, 96)
        self.assertEqual(obs[0].high, 101)

    def test_bullish_sweep_requires_wick_break_and_close_reclaim(self):
        candles = [
            c(0, 101, 102, 100, 101),
            c(1, 101, 101.5, 98, 99),
            c(2, 99, 100, 97, 98),
            c(3, 98, 99, 95, 96),
            c(4, 96, 98, 95.5, 97),
            c(5, 97, 99, 96, 98),
            c(6, 98, 100, 94.5, 95.5),
            c(7, 95.5, 100, 94, 96.5),
        ]
        swings = SwingDetector.find_swings(candles)

        sweep = LiquidityDetector.find_bullish_sweep(candles, swings)

        self.assertIsNotNone(sweep)
        self.assertEqual(sweep[0], 7)

    def test_choch_expires_after_12_bars(self):
        builder = ICTSetupBuilder()
        candles = [c(i, 100, 101, 99, 100) for i in range(20)]
        candles[2] = c(2, 100, 106, 99, 101)
        candles[5] = c(5, 100, 101, 94, 96)
        candles[18] = c(18, 96, 112, 95, 111)
        swings = SwingDetector.find_swings(candles)

        choch = builder._find_bullish_choch(candles, swings, 5)

        self.assertIsNone(choch)

    def test_stale_order_block_after_two_mitigations(self):
        candles = [
            c(0, 99, 100, 98, 99),
            c(1, 100, 101, 99, 100),
            c(2, 101, 104, 100, 102),
            c(3, 101, 102, 99, 100),
            c(4, 100, 101, 99, 100),
            c(5, 100, 101, 96, 97),
            c(6, 97, 110, 96, 109),
            c(7, 109, 111, 98, 110),
            c(8, 110, 114, 108, 113),
            c(9, 113, 115, 99, 114),
        ]
        swings = SwingDetector.find_swings(candles)

        obs = OrderBlockDetector.find_bullish_order_blocks(candles, swings, "1h")

        self.assertEqual(obs[0].mitigations, 2)
        self.assertTrue(obs[0].is_stale)

    def test_old_choch_trigger_does_not_create_trade_plan(self):
        builder = ICTSetupBuilder(max_trigger_age_bars=1)
        candles_5m = [c(i, 100, 101, 99, 100) for i in range(30)]
        candles_1h = [c(i, 100, 101, 99, 100) for i in range(20)]
        candles_4h = [c(i, 100, 101, 99, 100) for i in range(10)]
        swing_low = SwingPoint(5, candles_5m[5].timestamp, 95, "low")

        with (
            patch("ict_core.builder.StructureDetector.latest_trend", return_value="bullish"),
            patch.object(builder, "_active_1h_poi", return_value=("order_block", 90, 110)),
            patch("ict_core.builder.SwingDetector.find_swings", return_value=[]),
            patch("ict_core.builder.LiquidityDetector.find_bullish_sweep", return_value=(5, swing_low)),
            patch.object(builder, "_find_bullish_choch", return_value=10),
        ):
            setup = builder.build_long_setup("005930", candles_5m, candles_1h, candles_4h)

        self.assertIsNone(setup.trade_plan)
        self.assertIn("5m CHoCH trigger is stale", setup.notes)

    def test_daily_bearish_context_blocks_long_setup(self):
        builder = ICTSetupBuilder()
        candles_5m = [c(i, 100, 101, 99, 100) for i in range(30)]
        candles_1h = [c(i, 100, 101, 99, 100) for i in range(20)]
        candles_4h = [c(i, 100, 101, 99, 100) for i in range(10)]
        candles_1d = [
            c(0, 100, 105, 99, 104),
            c(1, 104, 110, 103, 109),
            c(2, 109, 111, 102, 103),
            c(3, 103, 104, 96, 97),
            c(4, 97, 98, 90, 91),
            c(5, 91, 92, 84, 85),
        ]

        with patch("ict_core.builder.StructureDetector.latest_trend", side_effect=["bearish"]):
            setup = builder.build_long_setup("005930", candles_5m, candles_1h, candles_4h, candles_1d=candles_1d)

        self.assertIsNone(setup.trade_plan)
        self.assertEqual(setup.daily_context, "bearish")

    def test_30m_trap_filter_blocks_when_missing(self):
        builder = ICTSetupBuilder()
        candles_5m = [c(i, 100, 101, 99, 100) for i in range(30)]
        candles_1h = [c(i, 100, 101, 99, 100) for i in range(20)]
        candles_4h = [c(i, 100, 101, 99, 100) for i in range(10)]
        candles_30m = [c(i, 100, 101, 99, 100) for i in range(12)]

        with (
            patch("ict_core.builder.StructureDetector.latest_trend", return_value="bullish"),
            patch.object(builder, "_active_1h_poi", return_value=("order_block", 90, 110)),
        ):
            setup = builder.build_long_setup("005930", candles_5m, candles_1h, candles_4h, candles_30m=candles_30m)

        self.assertIsNone(setup.trade_plan)
        self.assertIn("waiting for 30m liquidity trap inside POI", setup.notes)


if __name__ == "__main__":
    unittest.main()
