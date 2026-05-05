import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "strategy_builder"))
sys.path.insert(0, root)

from ict_core.models import Candle, TradeState
from strategy_builder.backend.ict_engine import ICTTradingEngine, ManagedOrder
from strategy_builder.core.ict_cache import MinuteBarCache


def candle_at(day: int, minute: int = 0) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(days=day, minutes=minute)
    return Candle(timestamp=ts, open=100, high=101, low=99, close=100, volume=10)


class ICTEngineTests(unittest.TestCase):
    def test_ready_cache_requires_dense_intraday_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = MinuteBarCache(os.path.join(tmp, "bars.sqlite3"))
            cache.upsert_bars("005930", [candle_at(day) for day in range(20)])

            self.assertEqual(cache.coverage_days("005930"), 20)
            self.assertEqual(cache.ready_coverage_days("005930"), 0)

            dense = [
                candle_at(day, minute)
                for day in range(20)
                for minute in range(cache.MIN_READY_BARS_PER_DAY)
            ]
            cache.upsert_bars("000660", dense)

            self.assertEqual(cache.ready_coverage_days("000660"), 20)

    def test_poll_source_does_not_satisfy_ready_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = MinuteBarCache(os.path.join(tmp, "bars.sqlite3"))
            for day in range(20):
                for minute in range(cache.MIN_READY_BARS_PER_DAY):
                    ts = datetime(2026, 1, 1) + timedelta(days=day, minutes=minute)
                    cache.upsert_tick_as_minute("005930", ts, 100, source="poll")

            self.assertEqual(cache.coverage_days("005930"), 20)
            self.assertEqual(cache.ready_coverage_days("005930"), 0)

    def test_tp_fill_removes_closed_position(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))
        engine._positions["005930"] = ManagedOrder(
            symbol="005930",
            side="buy",
            order_no="1",
            quantity=3,
            status=TradeState.ENTERED,
            entry=100,
            stop=90,
            take_profit=120,
        )

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_current_price") as current_price,
        ):
            engine._manage_position("005930")

        self.assertNotIn("005930", engine._positions)
        current_price.assert_not_called()

    def test_stop_promotes_partial_fill_to_managed_position(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))
        engine._pending["005930"] = ManagedOrder(
            symbol="005930",
            side="buy",
            order_no="10",
            org_no="001",
            quantity=10,
            entry=100,
            stop=90,
            take_profit=120,
        )
        pending_orders = pd.DataFrame([{
            "order_no": "10",
            "order_qty": 10,
            "filled_qty": 4,
            "unfilled_qty": 6,
        }])
        holdings = pd.DataFrame([{"stock_code": "005930", "quantity": 4}])
        tp_result = pd.DataFrame([{"ODNO": "20", "KRX_FWDG_ORD_ORGNO": "002"}])

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pending_orders, True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(holdings, True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.cancel_order", return_value={"success": True}),
            patch("strategy_builder.backend.ict_engine.OrderExecutor") as executor_cls,
        ):
            executor_cls.return_value.execute_signal.return_value = tp_result
            engine.stop(cancel_pending=True)

        self.assertNotIn("005930", engine._pending)
        self.assertIn("005930", engine._positions)
        self.assertEqual(engine._positions["005930"].quantity, 4)
        self.assertEqual(engine._positions["005930"].tp_order_no, "20")

    def test_synthetic_stop_does_not_market_sell_when_tp_cancel_fails(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))
        engine._positions["005930"] = ManagedOrder(
            symbol="005930",
            side="buy",
            order_no="1",
            quantity=3,
            status=TradeState.ENTERED,
            entry=100,
            stop=95,
            take_profit=120,
            tp_order_no="20",
            tp_org_no="002",
        )
        holdings = pd.DataFrame([{"stock_code": "005930", "quantity": 3}])

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(holdings, True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_current_price", return_value={"price": 94}),
            patch("strategy_builder.backend.ict_engine.data_fetcher.cancel_order", return_value={"success": False}),
            patch("strategy_builder.backend.ict_engine.OrderExecutor") as executor_cls,
        ):
            engine._manage_position("005930")

        executor_cls.return_value.execute_signal.assert_not_called()
        self.assertIn("005930", engine._positions)
        self.assertTrue(engine.status()["degraded"])

    def test_market_exit_remains_managed_until_holdings_close(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))
        engine._positions["005930"] = ManagedOrder(
            symbol="005930",
            side="buy",
            order_no="1",
            quantity=3,
            status=TradeState.ENTERED,
            entry=100,
            stop=95,
            take_profit=120,
        )
        holdings = pd.DataFrame([{"stock_code": "005930", "quantity": 3}])
        exit_result = pd.DataFrame([{"ODNO": "30", "KRX_FWDG_ORD_ORGNO": "003"}])

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(holdings, True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_current_price", return_value={"price": 94}),
            patch("strategy_builder.backend.ict_engine.OrderExecutor") as executor_cls,
        ):
            executor_cls.return_value.execute_signal.return_value = exit_result
            engine._manage_position("005930")

        self.assertIn("005930", engine._positions)
        self.assertEqual(engine._positions["005930"].status, TradeState.EXITING)
        self.assertEqual(engine._positions["005930"].exit_order_no, "30")

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(holdings, True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_current_price", return_value={"price": 93}),
            patch("strategy_builder.backend.ict_engine.OrderExecutor") as next_executor_cls,
        ):
            engine._manage_position("005930")

        next_executor_cls.return_value.execute_signal.assert_not_called()
        self.assertIn("005930", engine._positions)

    def test_pending_order_is_cancelled_after_repeated_missing_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))
        engine._pending["005930"] = ManagedOrder(
            symbol="005930",
            side="buy",
            order_no="10",
            quantity=3,
            entry=100,
            stop=95,
            take_profit=120,
        )

        with (
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_pending_orders", return_value=(pd.DataFrame(), True)),
            patch("strategy_builder.backend.ict_engine.data_fetcher.get_holdings_checked", return_value=(pd.DataFrame(), True)),
        ):
            engine._manage_position("005930")
            self.assertIn("005930", engine._pending)
            engine._manage_position("005930")

        self.assertNotIn("005930", engine._pending)

    def test_market_exception_blocks_entries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = ICTTradingEngine(cache=MinuteBarCache(os.path.join(tmp.name, "bars.sqlite3")))

        with patch("strategy_builder.backend.ict_engine.data_fetcher.get_current_price", return_value={
            "price": 100,
            "upper_limit": 100,
            "lower_limit": 50,
            "halted": False,
        }):
            self.assertFalse(engine._market_exception_ok("005930"))


if __name__ == "__main__":
    unittest.main()
