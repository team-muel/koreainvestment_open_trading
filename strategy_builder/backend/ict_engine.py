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
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import pandas as pd

from ict_core import Candle, ICTReplayBacktester, IntradayLiquidityReclaimBuilder, TradePlan, TradeState
from ict_core.builder import krx_tick_size

from core import data_fetcher
from core.ict_cache import MinuteBarCache, build_minute_bar_cache
from core.ict_journal import ICTJournal
from core.gcal_reporter import publish_gcal_signal_if_configured
from core.ict_realtime import RealtimeTickCollector
from core.order_executor import OrderExecutor
from core.signal import Action, Signal
from core.fill_reconciler import FillReconciler

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 실전 모드 진입을 위한 이중 잠금.
# vps 모드 확인 외에 이 플래그도 True여야 실전 주문이 허용됨.
# 실계좌 전환 시에만 True로 변경할 것.
LIVE_TRADING_ENABLED: bool = False


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
    max_exit_retry: int = 2                   # 시장가 매도 최대 재시도 횟수
    exit_retry_delay_sec: float = 2.0         # 재시도 대기 시간(초)
    market_index_filter_pct: float = -0.007   # 지수 -0.7% 이하 진입 금지
    symbol_cooldown_after_loss: bool = True   # 손절 종목 당일 재진입 금지
    entry_fee_rate: float = 0.00015           # 매수 수수료율 (0.015%)
    exit_fee_rate: float = 0.00015            # 매도 수수료율 (0.015%)
    exit_tax_rate: float = 0.0018             # 증권거래세 (0.18%)


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
    submitted_at: str = field(default_factory=lambda: datetime.now(KST).isoformat())

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
        self.cache = cache or build_minute_bar_cache()
        self.realtime = RealtimeTickCollector(self.cache, env_dv="vps")
        self.config = config or ICTConfig()
        self.builder = IntradayLiquidityReclaimBuilder()
        self.journal = self._build_journal()
        self.fill_reconciler = FillReconciler(self.journal)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._notif_thread: threading.Thread | None = None
        self._notif_queue: queue.Queue = queue.Queue()
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
        self._premarket_ready: bool = False  # 장전 스캔 완료 전까지 주문 금지
        self._loop_count: int = 0
        self._symbol_cooldown: dict[str, str] = {}  # symbol → "loss"/"win" (당일 거래 결과)
        self._today_entry_ids: set[str] = set()      # idempotency: 오늘 진입한 setup_id들

    @staticmethod
    def _build_journal():
        try:
            from core.supabase_journal import SupabaseJournal
            journal = SupabaseJournal()
            if journal.enabled:
                return journal
        except Exception:
            logger.exception("Supabase journal initialization failed; falling back to SQLite journal")
        return ICTJournal()

    def start(self, symbols: list[str]) -> dict[str, Any]:
        # 실전 모드 삼중 잠금
        if LIVE_TRADING_ENABLED:
            raise RuntimeError("코드 상수 LIVE_TRADING_ENABLED=True — 실계좌 진입 차단")

        import os
        if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true":
            raise RuntimeError("환경변수 LIVE_TRADING_ENABLED=true — 실계좌 진입 차단")

        try:
            from core.supabase_journal import SupabaseJournal
            _sb = SupabaseJournal()
            if _sb.enabled and _sb.get_runtime_config("live_trading_enabled", "false").lower() == "true":
                raise RuntimeError("Supabase runtime_config live_trading_enabled=true — 실계좌 진입 차단")
        except RuntimeError:
            raise
        except Exception:
            pass  # Supabase 연결 실패는 무시

        clean_symbols = [s for s in dict.fromkeys(symbols) if len(s) == 6 and s.isdigit()]
        if not clean_symbols:
            raise ValueError("at least one 6-digit domestic stock symbol is required")

        # 재시작 시 상태 복구
        self._restore_state()

        with self._lock:
            self._symbols = clean_symbols
            self._running = True
            self._degraded = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run_loop, name="ict-trading-engine", daemon=True)
                self._thread.start()
            if self._notif_thread is None or not self._notif_thread.is_alive():
                self._notif_thread = threading.Thread(
                    target=self._notif_worker, name="ict-notif-worker", daemon=True
                )
                self._notif_thread.start()
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
        # Drain notification queue
        self._notif_queue.put(None)
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
                "premarket_ready": self._premarket_ready,
                "engine_state": "ACTIVE" if self._premarket_ready else "PREMARKET_WAIT",
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

    def _restore_state(self) -> None:
        """재시작 시 KIS API + SQLite에서 상태 복구. 신규 주문은 복구 완료 후에만 허용."""
        logger.info("ICT engine: restoring state from API + journal...")

        # 1. Load today's daily_risk from SQLite
        today = datetime.now(KST).strftime("%Y-%m-%d")
        daily = self.journal.load_daily_risk(today)
        if daily:
            with self._lock:
                self._daily_entries = int(daily.get("entries", 0))
                self._daily_loss = float(daily.get("loss", 0.0))
                self._daily_realized = float(daily.get("realized", 0.0))
                self._last_total_eval = int(daily.get("total_eval", 0))

        # 2. Load pending orders from KIS API
        pending_orders, pending_ok = data_fetcher.get_pending_orders("vps")

        # 3. Load current holdings from KIS API
        holdings, holdings_ok = data_fetcher.get_holdings_checked("vps")

        # 4. Load saved orders from SQLite
        saved_orders = self.journal.load_orders(status_filter=["LIMIT_SUBMITTED", "ENTERED"])

        # 5. Reconstruct _pending and _positions from saved orders + API state
        for saved in saved_orders:
            symbol = saved.get("symbol", "")
            order_no = saved.get("order_no", "")
            if not symbol or not order_no:
                continue

            # Check if still pending in API
            is_api_pending = False
            if pending_ok and not pending_orders.empty and "order_no" in pending_orders.columns:
                matched = pending_orders[pending_orders["order_no"].astype(str) == str(order_no)]
                is_api_pending = not matched.empty

            # Check if in holdings
            holding_qty = 0
            if holdings_ok and not holdings.empty and "stock_code" in holdings.columns:
                matched = holdings[holdings["stock_code"] == symbol]
                if not matched.empty:
                    holding_qty = int(matched.iloc[0].get("quantity", 0))

            order = ManagedOrder(
                symbol=symbol,
                side=saved.get("side", "buy"),
                order_no=order_no,
                org_no=saved.get("org_no", ""),
                quantity=saved.get("quantity", 0),
                filled_quantity=saved.get("filled_qty", 0),
                price=saved.get("price", 0.0),
                entry=saved.get("entry", 0.0),
                stop=saved.get("stop", 0.0),
                take_profit=saved.get("take_profit", 0.0),
                tp_order_no=saved.get("tp_order_no", ""),
                tp2_order_no=saved.get("tp2_order_no", ""),
                exit_order_no=saved.get("exit_order_no", ""),
                submitted_at=saved.get("submitted_at", datetime.now(KST).isoformat()),
            )

            if holding_qty > 0:
                order.status = TradeState.ENTERED
                order.quantity = holding_qty
                with self._lock:
                    self._positions[symbol] = order
                logger.info("ICT restore: %s -> position (qty=%d)", symbol, holding_qty)
            elif is_api_pending:
                order.status = TradeState.LIMIT_SUBMITTED
                with self._lock:
                    self._pending[symbol] = order
                logger.info("ICT restore: %s -> pending", symbol)
            else:
                logger.info("ICT restore: %s order %s not found in API -> skipping", symbol, order_no)

        with self._lock:
            pending_count = len(self._pending)
            position_count = len(self._positions)

        logger.info(
            "ICT engine restore complete: %d pending, %d positions, daily_entries=%d, loss=%.0f, realized=%.0f",
            pending_count,
            position_count,
            self._daily_entries,
            self._daily_loss,
            self._daily_realized,
        )

    def update_symbols(self, symbols: list[str]) -> dict[str, Any]:
        """장전 스캔 완료 후 감시 종목을 동적으로 업데이트하고 ACTIVE 상태로 전환.

        PREMARKET_WAIT → ACTIVE 전환.
        이미 ACTIVE 상태여도 종목 목록 갱신 가능.
        """
        clean = [s for s in dict.fromkeys(symbols) if len(s) == 6 and s.isdigit()]
        if not clean:
            logger.warning("update_symbols: 유효한 종목 없음 — 기존 목록 유지")
            return self.status()

        with self._lock:
            old_symbols = list(self._symbols)
            self._symbols = clean
            self._premarket_ready = True

        # 새 종목 realtime 구독 추가
        new_symbols = [s for s in clean if s not in old_symbols]
        if new_symbols:
            self.realtime.start(clean)

        logger.info(
            "update_symbols: PREMARKET_WAIT → ACTIVE | 종목 %s → %s",
            old_symbols, clean,
        )
        self.journal.record_trade_event(
            "PREMARKET_READY",
            {"symbol": "ENGINE", "side": "none", "order_no": ""},
            {"symbols": clean, "previous_symbols": old_symbols},
        )
        return self.status()

    def _run_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                symbols = list(self._symbols)
                premarket_ready = self._premarket_ready

            self._loop_count += 1

            # PREMARKET_WAIT 상태: warmup만 실행, 주문/평가 금지
            if not premarket_ready:
                now = datetime.now(KST)
                for symbol in symbols:
                    try:
                        self._warmup_today(symbol)
                        self._collect_current_price(symbol)
                    except Exception:
                        logger.exception("warmup error for %s", symbol)

                # 09:10 이후에도 아직 PREMARKET_WAIT이면 자동으로 ACTIVE 전환
                # (장전 스캔이 실패한 경우 안전망)
                if now.hour >= 9 and now.minute >= 10:
                    logger.warning(
                        "PREMARKET_WAIT 09:10 초과 — 현재 종목으로 자동 ACTIVE 전환: %s",
                        symbols,
                    )
                    with self._lock:
                        self._premarket_ready = True

                # signal_log 주기적 정리
                if self._loop_count % 1000 == 0:
                    self.journal.cleanup_signal_log()

                time.sleep(self.config.loop_interval_seconds)
                continue

            # ACTIVE 상태: 정상 루프
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

            # signal_log 주기적 정리
            if self._loop_count % 1000 == 0:
                self.journal.cleanup_signal_log()

            time.sleep(self.config.loop_interval_seconds)

    def _warmup_today(self, symbol: str) -> None:
        today = datetime.now(KST).strftime("%Y%m%d")
        if self._warmup_dates.get(symbol) == today:
            return
        df = data_fetcher.get_intraday_minute_prices(symbol, env_dv="vps", max_pages=3)
        if df.empty:
            # 데이터 실패 시 캐시하지 않음 → 다음 루프에서 재시도 가능
            logger.warning("_warmup_today: empty data for %s, will retry next loop", symbol)
            return
        self._warmup_dates[symbol] = today
        self.cache.upsert_bars(symbol, self._df_to_candles(symbol, df))

    def _collect_current_price(self, symbol: str) -> None:
        data = data_fetcher.get_current_price(symbol, env_dv="vps")
        price = float(data.get("price", 0) or 0)
        volume = int(data.get("volume", 0) or 0)
        if price > 0:
            self.cache.upsert_tick_as_minute(symbol, datetime.now(KST), price, volume, source="poll")

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
            # max_pending_per_symbol 실제 적용
            pending_count_for_symbol = sum(1 for s in self._pending if s == symbol)
            if pending_count_for_symbol >= self.config.max_pending_per_symbol:
                return
            total_pending = len(self._pending)
            if total_pending >= self.config.max_pending_per_symbol * len(self._symbols):
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
            self._record_entry_block(setup.to_dict(), "MARKET_EXCEPTION")
            return
        if not self._spread_ok(symbol, setup.trade_plan.entry):
            self._record_entry_block(setup.to_dict(), "SPREAD_TOO_WIDE")
            return

        # 시장 지수 필터
        if not self._market_index_ok():
            self._record_entry_block(setup.to_dict(), "MARKET_INDEX_WEAK")
            return

        # 종목 cooldown (손절 후 당일 재진입 금지)
        if self.config.symbol_cooldown_after_loss:
            with self._lock:
                cooldown = self._symbol_cooldown.get(symbol)
            if cooldown == "loss":
                self._record_entry_block(setup.to_dict(), "SYMBOL_COOLDOWN_AFTER_LOSS")
                return

        # Idempotency: 같은 setup 중복 진입 방지
        setup_id = self._build_setup_id(setup.trade_plan)
        with self._lock:
            if setup_id in self._today_entry_ids:
                self._record_entry_block(setup.to_dict(), "DUPLICATE_SETUP_ID")
                return

        quantity = self._calculate_quantity(setup.trade_plan)
        if quantity <= 0:
            self._record_entry_block(setup.to_dict(), "QUANTITY_ZERO")
            return
        if not self._order_feasible(setup.trade_plan.symbol, setup.trade_plan, quantity):
            self._record_entry_block(setup.to_dict(), "ORDER_NOT_FEASIBLE")
            return
        self._record_signal_if_changed(setup.to_dict(), action_taken=True)

        # Risk Review Agent: 진입 근거 설명 생성 (비동기, 주문은 코드가 결정)
        import threading
        plan_dict = {
            "symbol": setup.trade_plan.symbol,
            "entry": setup.trade_plan.entry,
            "stop": setup.trade_plan.stop,
            "take_profit": setup.trade_plan.take_profit,
            "quantity": quantity,
            "reason": setup.trade_plan.reason,
        }
        threading.Thread(
            target=self._run_risk_review,
            args=(plan_dict, self.status(), setup.to_dict().get("details", {}).get("trigger")),
            daemon=True,
        ).start()

        self._submit_entry(setup.trade_plan, quantity, setup_id=setup_id)

    def _build_setup_from_1m(self, symbol: str, bars_1m: list[Candle]):
        bars_1d = MinuteBarCache.resample(bars_1m, 390, "1d")
        return self.builder.build_long_setup(symbol, bars_1m, bars_1d)

    def _submit_entry(self, plan: TradePlan, quantity: int, setup_id: str = "") -> None:
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
            if setup_id:
                self._today_entry_ids.add(setup_id)
        self.journal.record_trade_event("ENTRY_SUBMITTED", order.to_dict(), {"entry_reason": plan.reason})
        self.journal.save_order(order.to_dict())
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
                with self._lock:
                    pending.missing_pending_checks += 1
                    should_cancel = pending.missing_pending_checks >= 2
                if should_cancel:
                    with self._lock:
                        pending.status = TradeState.CANCELLED
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
            self.journal.save_position(symbol, pending.to_dict())
            self._submit_take_profit(pending)

        with self._lock:
            position = self._positions.get(symbol)
        if not position:
            return
        if holdings_ok and holding_qty <= 0:
            position.status = TradeState.CLOSED
            # Use FillReconciler for actual exit fill
            fill_result = self.fill_reconciler.reconcile(
                order_no=position.exit_order_no or position.order_no,
                symbol=position.symbol,
                side="sell",
                quantity=position.quantity,
                entry_price=position.entry,
                env_dv="vps",
            )
            if fill_result.is_complete:
                net_pnl = self._calc_net_pnl(fill_result.realized_pnl, fill_result.avg_price or position.entry, position.quantity)
                with self._lock:
                    self._daily_realized += net_pnl
                    if net_pnl < 0:
                        self._daily_loss += abs(net_pnl)
                    # 손절/수익 cooldown 기록
                    if self.config.symbol_cooldown_after_loss:
                        self._symbol_cooldown[symbol] = "loss" if net_pnl < 0 else "win"
            else:
                logger.warning(
                    "FillReconciler incomplete for %s; closing position state without estimated PnL",
                    position.symbol,
                )
            with self._lock:
                self._positions.pop(symbol, None)
            self.journal.record_trade_event(
                "POSITION_CLOSED",
                position.to_dict(),
                {"exit_reason": "holding quantity is zero; reconciled from broker holdings check"},
            )
            self.journal.save_position(symbol, None)
            today = datetime.now(KST).strftime("%Y-%m-%d")
            self.journal.save_daily_risk(
                today,
                self._daily_entries,
                self._daily_loss,
                self._daily_realized,
                self._last_total_eval,
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
        """시장가 손절/강제청산. 실제 보유수량 기준, 실패 시 재시도."""
        # broker 실잔고 기준 (TP 부분체결로 잔여수량이 달라질 수 있음)
        holdings, holdings_ok = data_fetcher.get_holdings_checked("vps")
        actual_qty = 0
        if holdings_ok and not holdings.empty and "stock_code" in holdings.columns:
            matched = holdings[holdings["stock_code"] == order.symbol]
            if not matched.empty:
                actual_qty = int(matched.iloc[0].get("quantity", 0))

        if actual_qty <= 0:
            logger.warning("_submit_market_exit: %s 보유수량 0 — 이미 청산됨", order.symbol)
            with self._lock:
                self._positions.pop(order.symbol, None)
            return

        for attempt in range(self.config.max_exit_retry + 1):
            signal = Signal(
                stock_code=order.symbol,
                stock_name=order.symbol,
                action=Action.SELL,
                strength=1.0,
                reason=reason,
                quantity=actual_qty,
            )
            result = OrderExecutor(env_dv="vps").execute_signal(signal)
            if not result.empty:
                row = result.iloc[0]
                with self._lock:
                    order.status = TradeState.EXITING
                    order.exit_order_no = str(row.get("ODNO", ""))
                    order.exit_org_no = str(row.get("KRX_FWDG_ORD_ORGNO", ""))
                self.journal.record_trade_event("EXIT_SUBMITTED", order.to_dict(), {"exit_reason": reason, "actual_qty": actual_qty})
                return

            if attempt < self.config.max_exit_retry:
                logger.warning("시장가 매도 실패 %s (시도 %d/%d) — %.1fs 후 재시도",
                    order.symbol, attempt + 1, self.config.max_exit_retry, self.config.exit_retry_delay_sec)
                time.sleep(self.config.exit_retry_delay_sec)
                # 재조회: 아직 보유 중인지
                h2, ok2 = data_fetcher.get_holdings_checked("vps")
                if ok2 and not h2.empty and "stock_code" in h2.columns:
                    m2 = h2[h2["stock_code"] == order.symbol]
                    actual_qty = int(m2.iloc[0].get("quantity", 0)) if not m2.empty else 0
                if actual_qty <= 0:
                    logger.info("재시도 전 %s 이미 청산 확인", order.symbol)
                    return

        self._emergency_stop(f"시장가 매도 {self.config.max_exit_retry + 1}회 실패: {order.symbol}")

    def _cancel_take_profit_orders(self, position: ManagedOrder) -> bool:
        """TP 주문 취소 후 실제로 미체결 목록에서 사라졌는지 재확인."""
        targets = [
            (position.tp_order_no, position.tp_org_no),
            (position.tp2_order_no, position.tp2_org_no),
        ]
        for order_no, org_no in targets:
            if not order_no:
                continue
            if not self._cancel_order(
                position, "vps",
                order_no=order_no, org_no=org_no,
                remove_pending=False,
            ):
                self._emergency_stop("take-profit cancel failed before exit")
                return False

        # 취소 요청 성공만으로 부족 — 재조회로 실제 사라졌는지 확인
        time.sleep(self.config.exit_retry_delay_sec)
        pending_orders, pending_ok = data_fetcher.get_pending_orders("vps")
        if not pending_ok:
            self._emergency_stop("take-profit cancel status could not be verified before exit")
            return False
        for order_no, _org_no in targets:
            if order_no and self._order_still_pending(order_no, pending_orders):
                self._emergency_stop("take-profit remains pending after cancel response")
                return False
        return True

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

    @staticmethod
    def _pending_snapshot(order: ManagedOrder, pending_orders: pd.DataFrame) -> dict[str, int] | None:
        """Return broker-side filled/unfilled quantities for a pending order."""
        if pending_orders is None or pending_orders.empty or "order_no" not in pending_orders.columns:
            return None

        matched = pending_orders[pending_orders["order_no"].astype(str) == str(order.order_no)]
        if matched.empty:
            return None

        row = matched.iloc[0]

        def as_int(value: Any, default: int = 0) -> int:
            try:
                if pd.isna(value):
                    return default
                return int(float(value))
            except (TypeError, ValueError):
                return default

        order_qty = as_int(row.get("order_qty"), order.quantity)
        filled_qty = as_int(row.get("filled_qty"), 0)
        unfilled_qty = as_int(row.get("unfilled_qty"), max(order_qty - filled_qty, 0))
        if order_qty <= 0:
            order_qty = max(order.quantity, filled_qty + unfilled_qty)
        if unfilled_qty < 0:
            unfilled_qty = max(order_qty - filled_qty, 0)

        return {
            "order_quantity": order_qty,
            "filled_quantity": max(filled_qty, 0),
            "unfilled_quantity": max(unfilled_qty, 0),
        }

    @staticmethod
    def _order_still_pending(order_no: str, pending_orders: pd.DataFrame) -> bool:
        if pending_orders is None or pending_orders.empty or "order_no" not in pending_orders.columns:
            return False
        return not pending_orders[pending_orders["order_no"].astype(str) == str(order_no)].empty

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

    def _order_feasible(self, symbol: str, plan: TradePlan, quantity: int) -> bool:
        """거래대금 + 호가 깊이 기반 주문 체결 가능성 사전 필터링.

        주문하려는 수량이 매도 호가 잔량에 비해 과도하게 크면
        체결 슬리피지가 발생하거나 부분체결 후 잔량이 남을 위험이 있다.
        """
        if quantity <= 0:
            return False

        orderbook = data_fetcher.get_orderbook(symbol, "vps")
        if not orderbook:
            return False

        ask_prices = orderbook.get("ask_prices") or []
        ask_volumes = orderbook.get("ask_volumes") or []

        if not ask_prices or not ask_volumes:
            return False

        # 1차 호가 잔량 대비 주문 수량 비율
        best_ask_volume = int(ask_volumes[0]) if ask_volumes else 0
        if best_ask_volume > 0 and quantity > best_ask_volume * 0.5:
            # 1차 호가 잔량의 50% 초과 주문은 시장 충격 위험
            logger.info(
                "_order_feasible: %s qty=%d > 50%% of best ask volume=%d → skip",
                symbol, quantity, best_ask_volume
            )
            return False

        # 상위 3개 호가 합산 잔량 vs 주문 수량
        total_ask_volume = sum(int(v) for v in ask_volumes[:3] if v)
        if total_ask_volume > 0 and quantity > total_ask_volume * 0.3:
            logger.info(
                "_order_feasible: %s qty=%d > 30%% of top-3 ask volume=%d → skip",
                symbol, quantity, total_ask_volume
            )
            return False

        return True

    def _spread_ok(self, symbol: str, entry: float) -> bool:
        """spread가 tick cap AND % cap 동시 만족 시에만 통과.

        max() 기준 단일 임계값은 tick이 작은 저가주에서 % cap이 무력화되거나
        고가주에서 tick cap이 무력화될 수 있어 위험하다.
        두 조건을 동시 만족해야 스프레드가 안전한 수준임을 보장한다.
        """
        orderbook = data_fetcher.get_orderbook(symbol, "vps")
        if not orderbook:
            return False
        ask_prices = orderbook.get("ask_prices") or []
        bid_prices = orderbook.get("bid_prices") or []
        if not ask_prices or not bid_prices:
            return False
        best_ask = float(ask_prices[0])
        best_bid = float(bid_prices[0])
        if best_ask <= 0 or best_bid <= 0:
            return False
        spread = best_ask - best_bid
        tick = krx_tick_size(entry)

        # tick cap: 호가단위의 3배 이하
        tick_cap = tick * 3
        # % cap: entry 기준 0.3% 이하
        pct_cap = entry * 0.003

        # 두 조건 동시 만족 필요 (AND)
        return spread <= tick_cap and spread <= pct_cap

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
        return datetime.now(KST).time() >= deadline

    def _cache_status(self, symbol: str) -> dict[str, Any]:
        summary = self.cache.coverage_summary(symbol)
        ready_days = int(summary["ready_coverage_days"])
        summary["state"] = "READY" if ready_days >= self.config.min_cache_days else "WARMING_UP"
        return summary

    def _engine_state_dict(self) -> dict[str, Any]:
        """Return current engine state for persistence."""
        return {
            "updated_at": datetime.now(KST).isoformat(),
            "running": self._running,
            "degraded": self._degraded,
            "daily_entries": self._daily_entries,
            "daily_loss": self._daily_loss,
            "daily_realized": self._daily_realized,
            "last_total_eval": self._last_total_eval,
            "last_error": self._last_error or "",
            "symbols_json": json.dumps(self._symbols),
            "warmup_dates_json": json.dumps(self._warmup_dates),
        }

    def _notif_worker(self) -> None:
        """Notification worker - runs in separate thread, never blocks trading loop."""
        while True:
            try:
                item = self._notif_queue.get(timeout=5)
                if item is None:
                    break
                task = item.get("task")
                try:
                    if task == "gcal_signal":
                        publish_gcal_signal_if_configured(
                            ticker=item["ticker"],
                            title=item["title"],
                            description=item["description"],
                            event_dt=item["event_dt"],
                        )
                except Exception:
                    logger.exception("notif_worker: failed to process %s", task)
                finally:
                    self._notif_queue.task_done()
            except queue.Empty:
                with self._lock:
                    if not self._running:
                        break

    def _emergency_stop(self, reason: str) -> None:
        """치명적 오류 시 엔진 즉시 정지 + 알림."""
        logger.critical("ICT ENGINE EMERGENCY STOP: %s", reason)
        with self._lock:
            self._running = False
            self._degraded = True
            self._last_error = f"[EMERGENCY STOP] {reason}"
        # journal에 기록
        self.journal.record_trade_event(
            "EMERGENCY_STOP",
            {"symbol": "ENGINE", "side": "none", "order_no": ""},
            {"exit_reason": reason},
        )
        # notification queue에 알림 요청
        self._notif_queue.put({
            "task": "gcal_signal",
            "ticker": "ENGINE",
            "title": "EMERGENCY STOP",
            "description": f"ICT engine stopped: {reason}",
            "event_dt": datetime.now(KST),
        })

    def _mark_degraded(self, message: str) -> None:
        logger.error("ICT engine degraded: %s", message)
        with self._lock:
            self._degraded = True
            self._last_error = message
        self.journal.save_engine_state(self._engine_state_dict())

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

    def _publish_signal_calendar_event(self, plan: TradePlan, quantity: int) -> None:
        """매매 루프를 블록하지 않도록 notification worker에 위임."""
        self._notif_queue.put({
            "task": "gcal_signal",
            "ticker": plan.symbol,
            "title": "Buy submitted",
            "description": (
                "Setup: " + plan.reason + "\n"
                "Entry: " + f"{plan.entry:,.0f}" + "\n"
                "Stop: " + f"{plan.stop:,.0f}" + "\n"
                "Target: " + f"{plan.take_profit:,.0f}" + "\n"
                "Quantity: " + str(quantity) + "\n"
                "Action: monitor fill, TP, VWAP reclaim failure, and force-exit rules."
            ),
            "event_dt": datetime.now(KST),
        })



    def _build_setup_id(self, plan: TradePlan) -> str:
        """주문 idempotency key: 날짜+종목+방향+진입가."""
        today = datetime.now(KST).strftime("%Y%m%d")
        return f"{today}-{plan.symbol}-{plan.side.upper()}-{int(plan.entry)}"

    def _market_index_ok(self) -> bool:
        """KOSPI 당일 등락률이 임계값 이상인지 확인. 조회 실패 시 True(허용)."""
        try:
            kospi = data_fetcher.get_current_price("0001", "vps")
            change_rate = float(kospi.get("change_rate", 0) or 0)
            if change_rate <= self.config.market_index_filter_pct:
                logger.info("시장 지수 필터: KOSPI %+.2f%% → 신규 진입 금지", change_rate * 100)
                return False
        except Exception:
            pass
        return True

    def _record_entry_block(self, setup: dict, reason: str) -> None:
        """진입 차단 사유를 journal에 기록."""
        symbol = str(setup.get("symbol") or "")
        if not symbol:
            return
        logger.info("진입 차단: %s — %s", symbol, reason)
        try:
            self.journal.record_signal(
                {**setup, "entry_block_reason": reason},
                action_taken=False,
                reason_not_taken=reason,
            )
        except Exception:
            pass

    def _calc_net_pnl(self, gross_pnl: float, avg_price: float, qty: int) -> float:
        """수수료 + 세금 차감 후 실현손익 계산."""
        if qty <= 0 or avg_price <= 0:
            return gross_pnl
        fees = avg_price * qty * (self.config.entry_fee_rate + self.config.exit_fee_rate)
        tax = avg_price * qty * self.config.exit_tax_rate
        return gross_pnl - fees - tax


    def _run_risk_review(self, plan_dict: dict, engine_status: dict, signal_trigger: dict | None = None) -> None:
        """Risk Review Agent 비동기 실행 (주문 결정과 무관한 설명 생성)."""
        try:
            from agents.risk_review_agent import RiskReviewAgent
            agent = RiskReviewAgent()
            result = agent.review(plan_dict, engine_status, signal_detail=signal_trigger)
            logger.info(agent.format_for_log(result))
        except Exception:
            logger.debug("RiskReviewAgent 실행 실패 (비필수)")

ict_engine = ICTTradingEngine()
