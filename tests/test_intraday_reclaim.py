import os
import sys
import unittest
from datetime import datetime, timedelta

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

from ict_core.intraday import IntradayLiquidityReclaimBuilder
from ict_core.models import Candle


def bar(minute: int, open_: float, high: float, low: float, close: float, volume: int = 1000) -> Candle:
    return Candle(
        timestamp=datetime(2026, 5, 7, 9, 0) + timedelta(minutes=minute),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        symbol="005930",
        timeframe="1m",
    )


class IntradayReclaimTests(unittest.TestCase):
    def test_long_setup_requires_opening_range_sweep_vwap_reclaim_and_high_break(self):
        bars = []
        for minute in range(15):
            bars.append(bar(minute, 10030, 10050, 10010, 10030, 1000))
        bars.extend([
            bar(15, 10020, 10030, 10005, 10020, 1100),
            bar(16, 10020, 10040, 10015, 10035, 1200),
            bar(17, 10035, 10055, 10025, 10045, 1300),
            bar(18, 10045, 10070, 10040, 10060, 1500),
            bar(19, 10060, 10090, 10055, 10080, 2500),
        ])

        setup = IntradayLiquidityReclaimBuilder().build_long_setup("005930", bars, [])

        self.assertIsNotNone(setup.trade_plan)
        self.assertEqual(setup.trend, "bullish")
        self.assertIn("long setup confirmed", setup.notes)
        self.assertAlmostEqual(setup.trade_plan.partial_take_profit, setup.trade_plan.entry + (setup.trade_plan.entry - setup.trade_plan.stop))
        self.assertAlmostEqual(setup.trade_plan.final_take_profit, setup.trade_plan.entry + (setup.trade_plan.entry - setup.trade_plan.stop) * 2)

    def test_rejects_setup_when_stop_width_is_too_large(self):
        bars = []
        for minute in range(15):
            bars.append(bar(minute, 10000, 10050, 9950, 10000, 1000))
        bars.extend([
            bar(15, 9980, 10000, 9000, 9970, 1100),
            bar(16, 9970, 10020, 9960, 10010, 1200),
            bar(17, 10010, 10040, 10000, 10030, 1300),
            bar(18, 10030, 10080, 10020, 10070, 1500),
            bar(19, 10070, 10120, 10060, 10110, 2500),
        ])

        setup = IntradayLiquidityReclaimBuilder().build_long_setup("005930", bars, [])

        self.assertIsNone(setup.trade_plan)
        self.assertTrue(any("stop width too large" in note for note in setup.notes))


if __name__ == "__main__":
    unittest.main()
