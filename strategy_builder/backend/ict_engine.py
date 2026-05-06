"""ICT v1 paper-trading engine.

The engine is intentionally conservative:
- vps paper mode only
- long-only domestic stock entries
- local 1m cache is required for 4h/1h/5m resampling
- order execution is gated by strict risk checks
"""

from __future__ import annotations

import logging
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from ict_core import Candle, ICTReplayBacktester, IntradayLiquidityReclaimBuilder, TradePlan, TradeState
from ict_core.builder import krx_tick_size

from core import data_fetcher
from core.ict_cache import MinuteBarCache
from core.ict_journal import ICTJournal
from core.gcal_reporter import publish_gcal_signal_if_configured
from core.ict_realtime import RealtimeTickCollector
from core.order_executor import OrderExecutor
from core.signal import Action, Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ICTConfig:
    config_id: str = "default_ict_v1"
    min_cache_days: int = 20
    loop_interval_seconds: int = 15
    risk_per_trade_pct: float = 0.003
    max_position_pct: float = 0.10
    daily_loss_limit_pct: float = 0.005
    max_daily_entries: int = 2
    max_open_positions: int = 1
    max_pending_per_symbol: int = 1
    daily_profit_target_pct: float = 0.005
    force_exit_time: str = "14:50"


@dataclass
class ManagedOrder:
    symbol: str
    side: str
    order_no: str
    org_no: str = ""
    quantity: int = 0
    filled_quantity: int = 0
    price: float = 0.0
    status: TradeState = TradeState.LIMIT_SUBMITTED
    entry: float = 0.0
    stop: float = 0.0
    take_profit: float = 0.0
    tp_order_no: str = ""
    tp_org_no: str = ""
    tp2_order_no: str = ""
    tp2_org_no: str = ""
    partial_take_profit: float = 0.0
    final_take_profit: float = 0.0
    exit_order_no: str = ""
    exit_org_no: str = ""
    missing_pending_checks: int = 0
    submitted_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "order_no": self.order_no,
            "org_no": self.org_no,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "price": self.price,
            "status": self.status.value,
            "entry": self.entry,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "tp_order_no": self.tp_order_no,
            "tp_org_no": self.tp_org_no,
            "tp2_order_no": self.tp2_order_no,
            "tp2_org_no": self.tp2_org_no,
            "partial_take_profit": self.partial_take_profit,
            "final_take_profit": self.final_take_profit,
            "exit_order_no": self.exit_order_no,
            "exit_org_no": self.exit_org_no,
            "submitted_at": self.submitted_at,
        }


class ICTTradingEngine:
    def __init__(self, cache: MinuteBarCache | None = None, config: ICTConfig | None = None):
        self.cache = cache or MinuteBarCache()
        self.realtime = RealtimeTickCollector(self.cache, env_dv="vps")
        self.config = config or ICTConfig()
        self.builder = IntradayLiquidityReclaimBuilder()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._degraded = False
        self._symbols: list[str] = []
        self._setups: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, ManagedOrder] = {}
        self._positions: dict[str, ManagedOrder] = {}
        self._warmup_dates: dict[str, str] = {}
        self._daily_entries = 0
        self._daily_loss = 0.0
        self._daily_realized = 0.0
        self._last_total_eval = 0
        self._last_error: str | None = None
        self._last_signal_fingerprint: dict[str, str] = {}
        self.journal = ICTJournal()

    def start(self, symbols: list[str]) -> dict[str, Any]:
        clean_symbols = [s for s in dict.fromkeys(symbols) if len(s) == 6 and s.isdigit()]
        if not clean_symbols:
            raise ValueError("at least one 6-digit domestic stock symbol is required")

        with self._lock:
            self._symbols = clean_symbols
            self._running = True
            self._degraded = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run_loop, name="ict-trading-engine", daemon=True)
                self._thread.start()
            self.realtime.start(clean_symbols)
        return self.status()

    def stop(self, cancel_pending: bool = True, env_dv: str = "vps") -> dict[str, Any]:
        with self._lock:
            self._running = False
            pending_orders = list(self._pending.values())
        self.realtime.stop()
        if cancel_pending:
            for order in pending_orders:
                self._cancel_pending_entry(order, env_dv)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            symbols = list(self._symbols)
            cache_status = {
                symbol: self._cache_status(symbol)
                for symbol in symbols
            }
            running = self._running
            degraded = self._degraded
            active_setups = list(self._setups.values())
            pending_orders = [o.to_dict() for o in self._pending.values()]
            positions = [o.to_dict() for o in self._positions.values()]
            daily_entries = self._daily_entries
            daily_loss = self._daily_loss
            last_error = self._last_error
            loss_limit_reached = self._daily_loss_limit_reached(fetch_account=False)
            return {
                "running": running,
                "degraded": degraded,
                "symbols": symbols,
                "cache": cache_status,
                "active_setups": active_setups,
                "pending_orders": pending_orders,
                "positions": positions,
                "realtime": self.realtime.status(),
                "daily_risk": {
                    "state": self._daily_state(),
                    "entries": daily_entries,
                    "max_entries": self.config.max_daily_entries,
                    "loss": daily_loss,
                    "realized": self._daily_realized,
                    "profit_target_pct": self.config.daily_profit_target_pct,
                    "profit_target_reached": self._daily_profit_target_reached(),
                    "loss_limit_pct": self.config.daily_loss_limit_pct,
                    "loss_limit_reached": loss_limit_reached,
                },
                "last_error": last_error,
            }

    def setups(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._setups.values())

    def chart_data(self, symbol: str, timeframe: str = "5m", limit: int = 160) -> dict[str, Any]:
        if timeframe not in {"1m", "5m", "1h", "4h"}:
            raise ValueError("timeframe must be one of: 1m, 5m, 1h, 4h")
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("symbol must be a 6-digit domestic stock code")

        limit = max(20, min(int(limit or 160), 300))
        interval = {"1m": 1, "5m": 5, "1h": 60, "4h": 240}[timeframe]
        raw_limit = min(max(limit * interval + interval * 2, 800), 80000)
        bars_1m = self.cache.get_1m_bars(symbol, limit=raw_limit)
        if timeframe == "1m":
            all_bars = bars_1m
            bars = all_bars[-limit:]
        else:
            all_bars = MinuteBarCache.resample(bars_1m, interval, timeframe)
            bars = all_bars[-limit:]

        setup = self._setup_for_chart(symbol, bars_1m)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "visible_start_index": max(0, len(all_bars) - len(bars)),
            "candles": [self._candle_to_dict(bar) for bar in bars],
            "setup": setup,
            "orders": {
                "pending": self._pending.get(symbol).to_dict() if symbol in self._pending else None,
                "position": self._positions.get(symbol).to_dict() if symbol in self._positions else None,
            },
        }

    def backtest_from_cache(self, symbols: list[str], start: str, end: str) -> dict[str, Any]:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        results = []
        replay = ICTReplayBacktester(builder=self.builder)
        for symbol in symbols:
            bars = [
                b for b in self.cache.get_1m_bars(symbol)
                if start_dt <= b.timestamp <= end_dt
            ]
            ready_dates = self.cache.ready_dates(symbol)
            result = replay.run(symbol, bars, ready_dates=ready_dates)
            result_dict = result.to_dict()
            result_dict["cache_state"] = (
                "READY"
                if self.cache.ready_coverage_days(symbol) >= self.config.min_cache_days
                else "WARMING_UP"
            )
            results.append(result_dict)
        return {"status": "success", "results": results}

    def _setup_for_chart(self, symbol: str, bars_1m: list[Candle]) -> dict[str, Any] | None:
        with self._lock:
            cached = self._setups.get(symbol)
        if cached is not None:
            return cached
        if not bars_1m:
            return None
        return self._build_setup_from_1m(symbol, bars_1m).to_dict()

    def _run_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                symbols = list(self._symbols)
            for symbol in symbols:
                try:
                    self._warmup_today(symbol)
                    self._collect_current_price(symbol)
                    self._evaluate_symbol(symbol)
                    self._manage_position(symbol)
                except Exception as e:
                    logger.exception("ICT engine loop error for %s", symbol)
                    with self._lock:
                        self._degraded = True
                        self._last_error = str(e)
            time.sleep(self.config.loop_interval_seconds)

    def _warmup_today(self, symbol: str) -> None:
        today = datetime.now().strftime("%Y%m%d")
        if self._warmup_dates.get(symbol) == today:
            return
        df = data_fetcher.get_intraday_minute_prices(symbol, env_dv="vps", max_pages=3)
        self._warmup_dates[symbol] = today
        if df.empty:
            return
        self.cache.upsert_bars(symbol, self._df_to_candles(symbol, df))

    def _collect_current_price(self, symbol: str) -> None:
        data = data_fetcher.get_current_price(symbol, env_dv="vps")
        price = float(data.get("price", 0) or 0)
        volume = int(data.get("volume", 0) or 0)
        if price > 0:
            self.cache.upsert_tick_as_minute(symbol, datetime.now(), price, volume, source="poll")

    def _evaluate_symbol(self, symbol: str) -> None:
        bars_1m = self.cache.get_1m_bars(symbol, limit=8000)
        setup = self._build_setup_from_1m(symbol, bars_1m)
        with self._lock:
            self._setups[symbol] = setup.to_dict()

        self._record_signal_if_changed(setup.to_dict(), action_taken=False)
        if setup.trade_plan is None:
            return
        if self.cache.ready_coverage_days(symbol) < self.config.min_cache_days:
            return
        with self._lock:
            if symbol in self._pending or symbol in self._positions:
                return
            if len(self._positions) >= self.config.max_open_positions:
                return
            if self._daily_entries >= self.config.max_daily_entries:
                return
            if self._daily_profit_target_reached():
                return
            if self._daily_loss_limit_reached(fetch_account=True):
                return

        if not self._market_exception_ok(symbol):
            return
        if not self._spread_ok(symbol, setup.trade_plan.entry):
            return
        quantity = self._calculate_quantity(setup.trade_plan)
        if quantity <= 0:
            return
        self._record_signal_if_changed(setup.to_dict(), action_taken=True)
        self._submit_entry(setup.trade_plan, quantity)

    def _build_setup_from_1m(self, symbol: str, bars_1m: list[Candle]):
        bars_1d = MinuteBarCache.resample(bars_1m, 390, "1d")
        return self.builder.build_long_setup(symbol, bars_1m, bars_1d)

    def _submit_entry(self, plan: TradePlan, quantity: int) -> None:
        signal = Signal(
            stock_code=plan.symbol,
            stock_name=plan.symbol,
            action=Action.BUY,
            strength=0.7,
            reason=plan.reason,
            target_price=int(plan.entry),
            quantity=quantity,
        )
        result = OrderExecutor(env_dv="vps").execute_signal(signal)
        if result.empty:
            self._mark_degraded("entry order failed")
            return
        row = result.iloc[0]
        order = ManagedOrder(
            symbol=plan.symbol,
            side="buy",
            order_no=str(row.get("ODNO", "")),
            org_no=str(row.get("KRX_FWDG_ORD_ORGNO", "")),
            quantity=quantity,
            price=plan.entry,
            entry=plan.entry,
            stop=plan.stop,
            take_profit=plan.take_profit,
            partial_take_profit=plan.partial_take_profit or 0.0,
            final_take_profit=plan.final_take_profit or plan.take_profit,
        )
        with self._lock:
            self._pending[plan.symbol] = order
            self._daily_entries += 1
        self.journal.record_trade_event("ENTRY_SUBMITTED", order.to_dict(), {"entry_reason": plan.reason})
        self._publish_signal_calendar_event(plan, quantity)

    def _manage_position(self, symbol: str) -> None:
        with self._lock:
            has_managed_state = symbol in self._pending or symbol in self._positions
        if not has_managed_state:
            return

        pending_orders, pending_ok = data_fetcher.get_pending_orders("vps")
        holdings, holdings_ok = data_fetcher.get_holdings_checked("vps")
        holding_qty = 0
        if holdings_ok and not holdings.empty and "stock_code" in holdings.columns:
            matched = holdings[holdings["stock_code"] == symbol]
            if not matched.empty:
                holding_qty = int(matched.iloc[0].get("quantity", 0))

        with self._lock:
            pending = self._pending.get(symbol)
            position = self._positions.get(symbol)

        if pending:
            pending_snapshot = self._pending_snapshot(pending, pending_orders) if pending_ok else None
            if pending_snapshot:
                pending.missing_pending_checks = 0
                pending.filled_quantity = max(pending.filled_quantity, pending_snapshot["filled_quantity"])
                if pending.filled_quantity > 0 and pending_snapshot["unfilled_quantity"] > 0:
                    if not self._cancel_order(pending, "vps", remove_pending=False):
                        return
            elif pending_ok and holding_qty <= 0:
                pending.missing_pending_checks += 1
                if pending.missing_pending_checks >= 2:
                    pending.status = TradeState.CANCELLED
                    with self._lock:
                        self._pending.pop(symbol, None)
                    return

        if pending and holding_qty > 0:
            pending.status = TradeState.ENTERED
            pending.filled_quantity = min(
                pending.quantity,
                max(pending.filled_quantity, holding_qty),
            )
            pending.quantity = pending.filled_quantity
            with self._lock:
                self._positions[symbol] = pending
                self._pending.pop(symbol, None)
            self._submit_take_profit(pending)

        with self._lock:
            position = self._positions.get(symbol)
        if not position:
            return
        if holdings_ok and holding_qty <= 0:
            position.status = TradeState.CLOSED
            estimated_exit = position.take_profit if position.take_profit > 0 else position.entry
            estimated_pnl = (estimated_exit - position.entry) * position.quantity
            with self._lock:
                self._daily_realized += estimated_pnl
                self._positions.pop(symbol, None)
            self.journal.record_trade_event(
                "POSITION_CLOSED",
                position.to_dict(),
                {"exit_reason": "holding quantity is zero; estimated close from broker holdings check"},
            )
            return
        if position.status == TradeState.EXITING:
            return

        price_data = data_fetcher.get_current_price(symbol, "vps")
        current_price = float(price_data.get("price", 0) or 0)
        if self._force_exit_due(position):
            position.status = TradeState.EXITING
            if not self._cancel_take_profit_orders(position):
                return
            self._submit_market_exit(position, reason="ICT intraday force exit")
            return
        if current_price > 0 and current_price <= position.stop:
            position.status = TradeState.EXITING
            if not self._cancel_take_profit_orders(position):
                return
            self._submit_market_exit(position)

    def _submit_take_profit(self, order: ManagedOrder) -> None:
        tp1 = order.partial_take_profit or order.take_profit
        tp2 = order.final_take_profit or order.take_profit
        qty1 = max(1, order.quantity // 2)
        qty2 = max(0, order.quantity - qty1)
        signal = Signal(
            stock_code=order.symbol,
            stock_name=order.symbol,
            action=Action.SELL,
            strength=0.7,
            reason="ICT intraday partial TP at +1R",
            target_price=int(tp1),
            quantity=qty1,
        )
        result = OrderExecutor(env_dv="vps").execute_signal(signal)
        if not result.empty:
            row = result.iloc[0]
            order.tp_order_no = str(row.get("ODNO", ""))
            order.tp_org_no = str(row.get("KRX_FWDG_ORD_ORGNO", ""))
        else:
            self._mark_degraded("take-profit order failed")
            return
        if qty2 <= 0 or tp2 <= 0 or tp2 == tp1:
            return
        signal2 = Signal(
            stock_code=order.symbol,
            stock_name=order.symbol,
            action=Action.SELL,
            strength=0.7,
            reason="ICT intraday final TP at +2R",
            target_price=int(tp2),
            quantity=qty2,
        )
        result2 = OrderExecutor(env_dv="vps").execute_signal(signal2)
        if not result2.empty:
            row2 = result2.iloc[0]
            order.tp2_order_no = str(row2.get("ODNO", ""))
            order.tp2_org_no = str(row2.get("KRX_FWDG_ORD_ORGNO", ""))
        else:
            self._mark_degraded("final take-profit order failed")

    def _submit_market_exit(self, order: ManagedOrder, reason: str = "ICT synthetic stop loss") -> None:
        signal = Signal(
            stock_code=order.symbol,
            stock_name=order.symbol,
            action=Action.SELL,
            strength=1.0,
            reason=reason,
            quantity=order.quantity,
        )
        result = OrderExecutor(env_dv="vps").execute_signal(signal)
        if not result.empty:
            row = result.iloc[0]
            with self._lock:
                order.status = TradeState.EXITING
                order.exit_order_no = str(row.get("ODNO", ""))
                order.exit_org_no = str(row.get("KRX_FWDG_ORD_ORGNO", ""))
                self._daily_loss += max(0.0, (order.entry - order.stop) * order.quantity)
            self.journal.record_trade_event("EXIT_SUBMITTED", order.to_dict(), {"exit_reason": reason})
        else:
            self._mark_degraded("synthetic stop market exit failed")

    def _cancel_take_profit_orders(self, position: ManagedOrder) -> bool:
        targets = [
            (position.tp_order_no, position.tp_org_no),
            (position.tp2_order_no, position.tp2_org_no),
        ]
        for order_no, org_no in targets:
            if not order_no:
                continue
            if not self._cancel_order(
                position,
                "vps",
                order_no=order_no,
                org_no=org_no,
                remove_pending=False,
            ):
                self._mark_degraded("take-profit cancel failed before exit")
                return False
        pending_orders, pending_ok = data_fetcher.get_pending_orders("vps")
        if not pending_ok:
            self._mark_degraded("take-profit cancel status could not be verified before exit")
            return False
        for order_no, _org_no in targets:
            if order_no and self._order_still_pending(order_no, pending_orders):
                self._mark_degraded("take-profit remains pending after cancel response")
                return False
        return True

    def _cancel_order(
        self,
        order: ManagedOrder,
        env_dv: str,
        order_no: str | None = None,
        org_no: str | None = None,
        remove_pending: bool = True,
    ) -> bool:
        target_order_no = order_no or order.order_no
        if not target_order_no:
            return False
        result = data_fetcher.cancel_order(
            order_no=target_order_no,
            stock_code=order.symbol,
            qty=order.quantity,
            org_no=org_no if org_no is not None else order.org_no,
            env_dv=env_dv,
        )
        if not result.get("success"):
            self._mark_degraded(f"cancel order failed: {target_order_no}")
            return False
        with self._lock:
            if remove_pending:
                order.status = TradeState.CANCELLED
                self._pending.pop(order.symbol, None)
        return True

    def _cancel_pending_entry(self, order: ManagedOrder, env_dv: str) -> None:
        pending_orders, pending_ok = data_fetcher.get_pending_orders(env_dv)
        holdings, holdings_ok = data_fetcher.get_holdings_checked(env_dv)
        if pending_ok:
            pending_snapshot = self._pending_snapshot(order, pending_orders)
            if pending_snapshot:
                order.filled_quantity = max(order.filled_quantity, pending_snapshot["filled_quantity"])

        if not self._cancel_order(order, env_dv, remove_pending=False):
            return

        holding_qty = 0
        if holdings_ok and not holdings.empty and "stock_code" in holdings.columns:
            matched = holdings[holdings["stock_code"] == order.symbol]
            if not matched.empty:
                holding_qty = int(matched.iloc[0].get("quantity", 0))
        filled_qty = min(order.quantity, max(order.filled_quantity, holding_qty))
        with self._lock:
            self._pending.pop(order.symbol, None)
        if filled_qty <= 0:
            order.status = TradeState.CANCELLED
            return

        order.status = TradeState.ENTERED
        order.filled_quantity = filled_qty
        order.quantity = filled_qty
        with self._lock:
            self._positions[order.symbol] = order
        self._submit_take_profit(order)

    def _calculate_quantity(self, plan: TradePlan) -> int:
        deposit = data_fetcher.get_deposit("vps")
        total_eval = int(deposit.get("total_eval") or deposit.get("deposit") or 0)
        with self._lock:
            self._last_total_eval = total_eval
        if total_eval <= 0:
            return 0
        risk_amount = total_eval * self.config.risk_per_trade_pct
        risk_per_share = plan.entry - plan.stop
        if risk_per_share <= 0:
            return 0
        risk_qty = int(risk_amount // risk_per_share)
        max_position_qty = int((total_eval * self.config.max_position_pct) // plan.entry)
        buyable = data_fetcher.get_buyable_amount(plan.symbol, int(plan.entry), "vps")
        buyable_qty = int(buyable.get("quantity", 0) or 0)
        return max(0, min(risk_qty, max_position_qty, buyable_qty))

    def _spread_ok(self, symbol: str, entry: float) -> bool:
        orderbook = data_fetcher.get_orderbook(symbol, "vps")
        if not orderbook:
            return False
        ask_prices = orderbook.get("ask_prices") or []
        bid_prices = orderbook.get("bid_prices") or []
        if not ask_prices or not bid_prices:
            return False
        spread = float(ask_prices[0]) - float(bid_prices[0])
        threshold = max(krx_tick_size(entry) * 3, entry * 0.003)
        return spread <= threshold

    def _market_exception_ok(self, symbol: str) -> bool:
        price_data = data_fetcher.get_current_price(symbol, "vps")
        price = float(price_data.get("price", 0) or 0)
        if price <= 0:
            return False
        if price_data.get("halted"):
            return False
        upper_limit = float(price_data.get("upper_limit", 0) or 0)
        lower_limit = float(price_data.get("lower_limit", 0) or 0)
        if upper_limit > 0 and price >= upper_limit:
            return False
        if lower_limit > 0 and price <= lower_limit:
            return False
        return True

    def _daily_loss_limit_reached(self, fetch_account: bool = False) -> bool:
        if self._daily_loss <= 0:
            return False
        total_eval = self._last_total_eval
        if fetch_account or total_eval <= 0:
            deposit = data_fetcher.get_deposit("vps")
            total_eval = int(deposit.get("total_eval") or deposit.get("deposit") or 0)
            with self._lock:
                self._last_total_eval = total_eval
        if total_eval <= 0:
            return False
        return self._daily_loss >= total_eval * self.config.daily_loss_limit_pct

    def _daily_profit_target_reached(self) -> bool:
        total_eval = self._last_total_eval
        if total_eval <= 0 or self.config.daily_profit_target_pct <= 0:
            return False
        return self._daily_realized >= total_eval * self.config.daily_profit_target_pct

    def _daily_state(self) -> str:
        if self._daily_profit_target_reached():
            return "STOPPED_BY_TARGET"
        if self._daily_loss_limit_reached(fetch_account=False):
            return "STOPPED_BY_LOSS"
        if self._daily_entries >= self.config.max_daily_entries:
            return "ENTRY_LIMIT_REACHED"
        return "ACTIVE"

    def _force_exit_due(self, order: ManagedOrder) -> bool:
        if not self.config.force_exit_time:
            return False
        try:
            deadline = datetime.strptime(self.config.force_exit_time, "%H:%M").time()
        except ValueError:
            return False
        return datetime.now().time() >= deadline

    def _cache_status(self, symbol: str) -> dict[str, Any]:
        summary = self.cache.coverage_summary(symbol)
        ready_days = int(summary["ready_coverage_days"])
        summary["state"] = "READY" if ready_days >= self.config.min_cache_days else "WARMING_UP"
        return summary

    @staticmethod
    def _pending_snapshot(order: ManagedOrder, pending_orders: pd.DataFrame) -> dict[str, int] | None:
        if pending_orders.empty or "order_no" not in pending_orders.columns:
            return None
        matched = pending_orders[pending_orders["order_no"].astype(str) == str(order.order_no)]
        if matched.empty:
            return None
        row = matched.iloc[0]
        order_qty = int(row.get("order_qty", order.quantity) or order.quantity)
        filled_quantity = int(row.get("filled_qty", 0) or 0)
        unfilled_quantity = int(row.get("unfilled_qty", max(order_qty - filled_quantity, 0)) or 0)
        return {
            "order_quantity": order_qty,
            "filled_quantity": filled_quantity,
            "unfilled_quantity": unfilled_quantity,
        }

    @staticmethod
    def _order_still_pending(order_no: str, pending_orders: pd.DataFrame) -> bool:
        if not order_no or pending_orders.empty or "order_no" not in pending_orders.columns:
            return False
        matched = pending_orders[pending_orders["order_no"].astype(str) == str(order_no)]
        if matched.empty:
            return False
        if "unfilled_qty" not in matched.columns:
            return True
        return int(matched.iloc[0].get("unfilled_qty", 0) or 0) > 0

    def _mark_degraded(self, message: str) -> None:
        logger.error("ICT engine degraded: %s", message)
        with self._lock:
            self._degraded = True
            self._last_error = message

    def _record_signal_if_changed(self, setup: dict[str, Any], action_taken: bool) -> None:
        symbol = str(setup.get("symbol") or "")
        if not symbol:
            return
        fingerprint_payload = {
            "state": setup.get("state"),
            "trade_plan": setup.get("trade_plan"),
            "notes": setup.get("notes", [])[-3:],
            "trigger": (setup.get("details") or {}).get("trigger", {}),
        }
        fingerprint = json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            if self._last_signal_fingerprint.get(symbol) == fingerprint and not action_taken:
                return
            self._last_signal_fingerprint[symbol] = fingerprint
        reason_not_taken = "" if action_taken else "; ".join(setup.get("notes", [])[-3:])
        self.journal.record_signal(setup, action_taken=action_taken, reason_not_taken=reason_not_taken)

    @staticmethod
    def _publish_signal_calendar_event(plan: TradePlan, quantity: int) -> None:
        try:
            publish_gcal_signal_if_configured(
                ticker=plan.symbol,
                title="Buy submitted",
                description=(
                    f"Setup: {plan.reason}\n"
                    f"Entry: {plan.entry:,.0f}\n"
                    f"Stop: {plan.stop:,.0f}\n"
                    f"Target: {plan.take_profit:,.0f}\n"
                    f"Quantity: {quantity}\n"
                    "Action: monitor fill, TP, VWAP reclaim failure, and force-exit rules."
                ),
                event_dt=datetime.now(),
            )
        except Exception:
            logger.exception("failed to publish ICT signal calendar event")

    @staticmethod
    def _df_to_candles(symbol: str, df: pd.DataFrame) -> list[Candle]:
        bars: list[Candle] = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            bars.append(Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume", 0)),
                symbol=symbol,
                timeframe="1m",
            ))
        return bars

    @staticmethod
    def _candle_to_dict(bar: Candle) -> dict[str, Any]:
        return {
            "time": bar.timestamp.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }


ict_engine = ICTTradingEngine()
