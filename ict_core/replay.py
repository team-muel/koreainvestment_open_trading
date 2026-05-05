"""Replay backtester that uses the same ICT setup builder as live trading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .builder import ICTSetupBuilder
from .models import Candle, ICTSetup, TradePlan


ExitReason = Literal["take_profit", "stop_loss", "end_of_data"]


@dataclass(frozen=True)
class ReplayConfig:
    min_cache_days: int = 20
    max_pending_bars: int | None = None
    conservative_intrabar: bool = True


@dataclass
class ReplayOrder:
    plan: TradePlan
    submitted_at: datetime
    submitted_5m_index: int


@dataclass
class ReplayPosition:
    plan: TradePlan
    entry_at: datetime
    entry_5m_index: int


@dataclass
class ReplayTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    stop: float
    take_profit: float
    exit_price: float
    exit_reason: ExitReason
    pnl_per_share: float
    r_multiple: float
    risk_reward: float
    setup_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry": self.entry,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl_per_share": self.pnl_per_share,
            "r_multiple": self.r_multiple,
            "risk_reward": self.risk_reward,
            "setup_reason": self.setup_reason,
        }


@dataclass
class ReplayResult:
    symbol: str
    bars_1m: int
    bars_5m: int
    coverage_days: int
    setups_seen: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_expired: int = 0
    trades: list[ReplayTrade] = field(default_factory=list)
    last_setup: ICTSetup | None = None

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl_per_share > 0)

    @property
    def losses(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl_per_share < 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def total_r(self) -> float:
        return sum(trade.r_multiple for trade in self.trades)

    @property
    def total_pnl_per_share(self) -> float:
        return sum(trade.pnl_per_share for trade in self.trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars_1m": self.bars_1m,
            "bars_5m": self.bars_5m,
            "coverage_days": self.coverage_days,
            "setups_seen": self.setups_seen,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_expired": self.orders_expired,
            "trades": [trade.to_dict() for trade in self.trades],
            "summary": {
                "trade_count": len(self.trades),
                "wins": self.wins,
                "losses": self.losses,
                "win_rate": self.win_rate,
                "total_r": self.total_r,
                "total_pnl_per_share": self.total_pnl_per_share,
            },
            "last_setup": self.last_setup.to_dict() if self.last_setup else None,
        }


class ICTReplayBacktester:
    def __init__(self, builder: ICTSetupBuilder | None = None, config: ReplayConfig | None = None):
        self.builder = builder or ICTSetupBuilder()
        self.config = config or ReplayConfig()

    def run(self, symbol: str, candles_1m: list[Candle], ready_dates: set[str] | None = None) -> ReplayResult:
        bars_1m = sorted(candles_1m, key=lambda candle: candle.timestamp)
        bars_5m = resample_candles(bars_1m, 5, "5m")
        result = ReplayResult(
            symbol=symbol,
            bars_1m=len(bars_1m),
            bars_5m=len(bars_5m),
            coverage_days=len({bar.timestamp.date() for bar in bars_1m}),
        )

        pending: ReplayOrder | None = None
        position: ReplayPosition | None = None

        for index, bar in enumerate(bars_5m):
            if ready_dates is not None and not self._has_warmup(bar.timestamp, ready_dates):
                continue

            if pending is not None and self._expired(index, pending):
                result.orders_expired += 1
                pending = None

            if pending is not None and self._touches(bar, pending.plan.entry):
                position = ReplayPosition(pending.plan, bar.timestamp, index)
                result.orders_filled += 1
                pending = None

            if position is not None:
                closed = self._maybe_close_position(position, bar)
                if closed is not None:
                    result.trades.append(closed)
                    position = None
                    continue

            if pending is not None or position is not None:
                continue

            setup = self._build_setup_at(symbol, bars_1m, bar.timestamp)
            result.last_setup = setup
            if setup.trade_plan is None:
                continue

            result.setups_seen += 1
            pending = ReplayOrder(setup.trade_plan, bar.timestamp, index)
            result.orders_submitted += 1

        if position is not None and bars_5m:
            result.trades.append(self._force_close_at_end(position, bars_5m[-1]))

        return result

    def _build_setup_at(self, symbol: str, bars_1m: list[Candle], timestamp: datetime) -> ICTSetup:
        prefix_1m = [bar for bar in bars_1m if bar.timestamp <= timestamp]
        bars_5m = resample_candles(prefix_1m, 5, "5m")
        bars_30m = resample_candles(prefix_1m, 30, "30m")
        bars_1h = resample_candles(prefix_1m, 60, "1h")
        bars_4h = resample_candles(prefix_1m, 240, "4h")
        bars_1d = resample_candles(prefix_1m, 1440, "1d")
        return self.builder.build_long_setup(symbol, bars_5m, bars_1h, bars_4h, bars_30m, bars_1d)

    def _expired(self, index: int, pending: ReplayOrder) -> bool:
        if self.config.max_pending_bars is None:
            return False
        return index - pending.submitted_5m_index > self.config.max_pending_bars

    def _has_warmup(self, timestamp: datetime, ready_dates: set[str]) -> bool:
        current_date = timestamp.date().isoformat()
        completed_ready_days = sum(1 for date_text in ready_dates if date_text < current_date)
        return completed_ready_days >= self.config.min_cache_days

    @staticmethod
    def _touches(bar: Candle, price: float) -> bool:
        return bar.low <= price <= bar.high

    def _maybe_close_position(self, position: ReplayPosition, bar: Candle) -> ReplayTrade | None:
        plan = position.plan
        stop_touched = bar.low <= plan.stop
        tp_touched = bar.high >= plan.take_profit
        if not stop_touched and not tp_touched:
            return None
        if stop_touched and (self.config.conservative_intrabar or not tp_touched):
            return self._close(position, bar.timestamp, plan.stop, "stop_loss")
        return self._close(position, bar.timestamp, plan.take_profit, "take_profit")

    def _force_close_at_end(self, position: ReplayPosition, bar: Candle) -> ReplayTrade:
        return self._close(position, bar.timestamp, bar.close, "end_of_data")

    @staticmethod
    def _close(
        position: ReplayPosition,
        exit_time: datetime,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> ReplayTrade:
        plan = position.plan
        risk = plan.entry - plan.stop
        pnl = exit_price - plan.entry
        return ReplayTrade(
            symbol=plan.symbol,
            entry_time=position.entry_at,
            exit_time=exit_time,
            entry=plan.entry,
            stop=plan.stop,
            take_profit=plan.take_profit,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_per_share=pnl,
            r_multiple=pnl / risk if risk > 0 else 0.0,
            risk_reward=plan.risk_reward,
            setup_reason=plan.reason,
        )


def resample_candles(bars: list[Candle], interval_minutes: int, timeframe: str) -> list[Candle]:
    if not bars:
        return []
    buckets: dict[datetime, list[Candle]] = {}
    for bar in bars:
        minute_of_day = bar.timestamp.hour * 60 + bar.timestamp.minute
        bucket_minute = (minute_of_day // interval_minutes) * interval_minutes
        bucket_start = bar.timestamp.replace(
            hour=bucket_minute // 60,
            minute=bucket_minute % 60,
            second=0,
            microsecond=0,
        )
        buckets.setdefault(bucket_start, []).append(bar)

    result: list[Candle] = []
    for bucket_start in sorted(buckets):
        items = sorted(buckets[bucket_start], key=lambda candle: candle.timestamp)
        result.append(Candle(
            timestamp=bucket_start,
            open=items[0].open,
            high=max(item.high for item in items),
            low=min(item.low for item in items),
            close=items[-1].close,
            volume=sum(item.volume for item in items),
            symbol=items[-1].symbol,
            timeframe=timeframe,
        ))
    return result
