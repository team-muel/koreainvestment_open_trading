"""Data models for the ICT v1 long-only strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

Direction = Literal["bullish", "bearish"]
SwingKind = Literal["high", "low"]


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    symbol: str = ""
    timeframe: str = ""

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2


@dataclass(frozen=True)
class SwingPoint:
    index: int
    timestamp: datetime
    price: float
    kind: SwingKind


@dataclass(frozen=True)
class FVG:
    direction: Direction
    start_index: int
    end_index: int
    lower: float
    upper: float
    created_at: datetime
    mitigations: int = 0

    @property
    def midpoint(self) -> float:
        return (self.lower + self.upper) / 2

    @property
    def is_stale(self) -> bool:
        return self.mitigations >= 2

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper


@dataclass(frozen=True)
class OrderBlock:
    direction: Direction
    index: int
    timestamp: datetime
    low: float
    high: float
    source_timeframe: str
    displacement_index: int
    mitigations: int = 0

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2

    @property
    def is_stale(self) -> bool:
        return self.mitigations >= 2

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


@dataclass(frozen=True)
class LiquidityPool:
    side: Literal["buy_side", "sell_side"]
    price: float
    timestamp: datetime
    source_timeframe: str
    source_index: int
    score: int = 1


class TradeState(str, Enum):
    WATCHING_POI = "WATCHING_POI"
    WAITING_TRIGGER = "WAITING_TRIGGER"
    LIMIT_SUBMITTED = "LIMIT_SUBMITTED"
    ENTERED = "ENTERED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    WARMING_UP = "WARMING_UP"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: Literal["buy"]
    entry: float
    stop: float
    take_profit: float
    risk_reward: float
    quantity: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ICTSetup:
    symbol: str
    trend: Literal["bullish", "bearish", "neutral"]
    state: TradeState
    last_price: float | None = None
    poi_type: Literal["order_block", "fvg", "none"] = "none"
    poi_low: float | None = None
    poi_high: float | None = None
    sweep_index: int | None = None
    choch_index: int | None = None
    entry_ob: OrderBlock | None = None
    trade_plan: TradePlan | None = None
    daily_context: Literal["bullish", "bearish", "neutral"] = "neutral"
    trap_30m: bool = False
    liquidity_score: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "trend": self.trend,
            "state": self.state.value,
            "last_price": self.last_price,
            "poi_type": self.poi_type,
            "poi_low": self.poi_low,
            "poi_high": self.poi_high,
            "sweep_index": self.sweep_index,
            "choch_index": self.choch_index,
            "entry_ob": None if self.entry_ob is None else {
                "low": self.entry_ob.low,
                "high": self.entry_ob.high,
                "midpoint": self.entry_ob.midpoint,
                "timestamp": self.entry_ob.timestamp.isoformat(),
                "timeframe": self.entry_ob.source_timeframe,
            },
            "trade_plan": None if self.trade_plan is None else {
                "symbol": self.trade_plan.symbol,
                "side": self.trade_plan.side,
                "entry": self.trade_plan.entry,
                "stop": self.trade_plan.stop,
                "take_profit": self.trade_plan.take_profit,
                "risk_reward": self.trade_plan.risk_reward,
                "quantity": self.trade_plan.quantity,
                "reason": self.trade_plan.reason,
            },
            "daily_context": self.daily_context,
            "trap_30m": self.trap_30m,
            "liquidity_score": self.liquidity_score,
            "notes": list(self.notes),
        }
