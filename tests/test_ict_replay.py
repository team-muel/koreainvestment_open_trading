import unittest
from datetime import datetime, timedelta

from ict_core.models import Candle, ICTSetup, TradePlan, TradeState
from ict_core.replay import ICTReplayBacktester, resample_candles


def c(i, open_=100, high=101, low=99, close=100):
    return Candle(
        timestamp=datetime(2026, 1, 1, 9, 0) + timedelta(minutes=i),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
        symbol="005930",
        timeframe="1m",
    )


class StubBuilder:
    def __init__(self):
        self.calls = 0

    def build_long_setup(self, symbol, candles_5m, candles_1h, candles_4h, *args):
        self.calls += 1
        if self.calls == 2:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WATCHING_POI,
                trade_plan=TradePlan(
                    symbol=symbol,
                    side="buy",
                    entry=100,
                    stop=95,
                    take_profit=110,
                    risk_reward=2.0,
                    reason="stub setup",
                ),
            )
        return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WATCHING_POI)


class ICTReplayTests(unittest.TestCase):
    def test_resample_candles_builds_5m_bars(self):
        bars = [c(i, high=100 + i, low=90 - i, close=100 + i) for i in range(10)]

        resampled = resample_candles(bars, 5, "5m")

        self.assertEqual(len(resampled), 2)
        self.assertEqual(resampled[0].open, bars[0].open)
        self.assertEqual(resampled[0].high, bars[4].high)
        self.assertEqual(resampled[0].low, bars[4].low)
        self.assertEqual(resampled[0].close, bars[4].close)

    def test_replay_uses_builder_signal_then_fills_and_hits_tp(self):
        bars = [c(i) for i in range(20)]
        for i in range(10, 15):
            bars[i] = c(i, open_=101, high=106, low=99, close=104)
        for i in range(15, 20):
            bars[i] = c(i, open_=104, high=111, low=103, close=110)
        replay = ICTReplayBacktester(builder=StubBuilder())

        result = replay.run("005930", bars)

        self.assertEqual(result.orders_submitted, 1)
        self.assertEqual(result.orders_filled, 1)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "take_profit")
        self.assertEqual(result.trades[0].r_multiple, 2.0)

    def test_replay_uses_stop_first_when_same_bar_touches_tp_and_sl(self):
        bars = [c(i) for i in range(20)]
        for i in range(10, 15):
            bars[i] = c(i, open_=101, high=106, low=99, close=104)
        for i in range(15, 20):
            bars[i] = c(i, open_=100, high=111, low=94, close=96)
        replay = ICTReplayBacktester(builder=StubBuilder())

        result = replay.run("005930", bars)

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")
        self.assertEqual(result.trades[0].r_multiple, -1.0)

    def test_replay_blocks_orders_until_live_warmup_is_available(self):
        bars = [c(i) for i in range(20)]
        for i in range(10, 15):
            bars[i] = c(i, open_=101, high=106, low=99, close=104)
        for i in range(15, 20):
            bars[i] = c(i, open_=104, high=111, low=103, close=110)
        replay = ICTReplayBacktester(builder=StubBuilder())

        result = replay.run("005930", bars, ready_dates=set())

        self.assertEqual(result.orders_submitted, 0)
        self.assertEqual(len(result.trades), 0)

    def test_replay_allows_orders_after_twenty_prior_ready_days(self):
        bars = [c(i) for i in range(20)]
        for i in range(10, 15):
            bars[i] = c(i, open_=101, high=106, low=99, close=104)
        for i in range(15, 20):
            bars[i] = c(i, open_=104, high=111, low=103, close=110)
        ready_dates = {
            (datetime(2025, 12, 1) + timedelta(days=i)).date().isoformat()
            for i in range(20)
        }
        replay = ICTReplayBacktester(builder=StubBuilder())

        result = replay.run("005930", bars, ready_dates=ready_dates)

        self.assertEqual(result.orders_submitted, 1)


if __name__ == "__main__":
    unittest.main()
