"""Intraday ICT liquidity reclaim strategy for KRX day trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from .builder import krx_tick_size
from .models import Candle, ICTSetup, TradePlan, TradeState


@dataclass(frozen=True)
class IntradayReclaimConfig:
    market_open: time = time(9, 0)
    opening_range_end: time = time(9, 15)
    earliest_entry: time = time(9, 15)
    latest_entry: time = time(14, 30)
    force_exit: time = time(14, 50)
    min_price: float = 2000
    max_stop_pct: float = 0.01
    fixed_stop_pct: float = 0.005
    reclaim_bars: int = 3
    vwap_reclaim_window: int = 5
    min_volume_ratio: float = 1.0
    min_entry_buffer_ticks: int = 1
    partial_r: float = 1.0
    final_r: float = 2.0


class IntradayLiquidityReclaimBuilder:
    """Builds long-only setups from 1-minute KRX intraday bars.

    Rules:
    - premarket scanner supplies the watchlist; live entry waits until after 09:15
    - opening range is 09:00-09:15 high/low
    - long setup needs a sweep below OR low or previous day low, quick reclaim,
      VWAP reclaim, 5-minute candle close above VWAP, break of the previous 1m high,
      and volume confirmation
    - stop is below sweep low, VWAP reclaim, or fixed -0.5%; skip if risk > 1%
    - targets are +1R partial and +2R final; engine may execute according to
      its current order-management capability
    """

    def __init__(self, config: IntradayReclaimConfig | None = None):
        self.config = config or IntradayReclaimConfig()

    def build_long_setup(
        self,
        symbol: str,
        candles_1m: list[Candle],
        candles_1d: list[Candle] | None = None,
    ) -> ICTSetup:
        notes: list[str] = ["strategy=KIS ICT Intraday Liquidity Reclaim"]
        bars = sorted(candles_1m, key=lambda candle: candle.timestamp)
        if len(bars) < 20:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WARMING_UP, notes=notes + ["not enough 1m bars"])

        current_day = bars[-1].timestamp.date()
        day_bars = [bar for bar in bars if bar.timestamp.date() == current_day and self.config.market_open <= bar.timestamp.time() <= time(15, 30)]
        if len(day_bars) < 16:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, last_price=bars[-1].close, notes=notes + ["waiting for opening range"])

        last = day_bars[-1]
        if last.close < self.config.min_price:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, last_price=last.close, notes=notes + ["price below minimum filter"])
        if last.timestamp.time() < self.config.earliest_entry:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, last_price=last.close, notes=notes + ["observe only before 09:15"])
        if last.timestamp.time() > self.config.latest_entry:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.CANCELLED, last_price=last.close, notes=notes + ["entry window closed"])

        opening = [bar for bar in day_bars if self.config.market_open <= bar.timestamp.time() < self.config.opening_range_end]
        if not opening:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, last_price=last.close, notes=notes + ["opening range missing"])
        or_high = max(bar.high for bar in opening)
        or_low = min(bar.low for bar in opening)
        prev_low = self._previous_day_low(candles_1d or [], current_day)
        sweep_levels = [or_low]
        if prev_low is not None:
            sweep_levels.append(prev_low)

        sweep = self._find_long_sweep(day_bars, sweep_levels)
        sweep_index = None if sweep is None else sweep[0]
        sweep_level = or_low if sweep is None else sweep[1]
        if sweep_index is None:
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_type="none",
                poi_low=or_low,
                poi_high=or_high,
                notes=notes + ["waiting for OR low or previous low sweep"],
            )

        reclaim_index = self._find_reclaim(day_bars, sweep_index, sweep_level)
        if reclaim_index is None:
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["sweep found; waiting for quick reclaim"],
            )

        vwap_values = self._intraday_vwap(day_bars)
        vwap_reclaim_index = self._find_vwap_reclaim(day_bars, vwap_values, reclaim_index)
        if vwap_reclaim_index is None:
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for VWAP reclaim"],
            )

        if not self._latest_5m_closes_above_vwap(day_bars, vwap_values):
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for 5m close above VWAP"],
            )

        if not self._breaks_previous_1m_high(day_bars):
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for previous 1m high break"],
            )

        if not self._volume_confirms(day_bars):
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for volume confirmation"],
            )

        entry = last.close
        tick = krx_tick_size(entry)
        if entry <= 0:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, notes=notes + ["invalid entry"])

        sweep_low = min(bar.low for bar in day_bars[sweep_index:reclaim_index + 1])
        vwap_stop = vwap_values[-1] - tick
        structural_stop = sweep_low - (tick * 2)
        fixed_stop = entry * (1 - self.config.fixed_stop_pct)
        stop = min(structural_stop, vwap_stop, fixed_stop)
        risk = entry - stop
        if risk <= 0:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WAITING_TRIGGER, last_price=last.close, notes=notes + ["invalid stop"])
        if risk / entry > self.config.max_stop_pct:
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + [f"stop width too large: {risk / entry:.2%}"],
            )

        partial_tp = entry + risk * self.config.partial_r
        final_tp = entry + risk * self.config.final_r
        plan = TradePlan(
            symbol=symbol,
            side="buy",
            entry=entry,
            stop=stop,
            take_profit=final_tp,
            partial_take_profit=partial_tp,
            final_take_profit=final_tp,
            force_exit_time=self.config.force_exit.strftime("%H:%M"),
            risk_reward=self.config.final_r,
            reason="Intraday OR/previous-low sweep, VWAP reclaim, 5m VWAP close, 1m high break",
        )
        return ICTSetup(
            symbol=symbol,
            trend="bullish",
            state=TradeState.WATCHING_POI,
            last_price=last.close,
            poi_type="none",
            poi_low=or_low,
            poi_high=or_high,
            sweep_index=sweep_index,
            choch_index=vwap_reclaim_index,
            trade_plan=plan,
            daily_context="neutral",
            liquidity_score=7,
            notes=notes + [
                f"OR High={or_high:.2f}",
                f"OR Low={or_low:.2f}",
                f"VWAP={vwap_values[-1]:.2f}",
                "long setup confirmed",
            ],
        )

    def _previous_day_low(self, candles_1d: list[Candle], current_day) -> float | None:
        previous = [bar for bar in candles_1d if bar.timestamp.date() < current_day]
        if not previous:
            return None
        return previous[-1].low

    def _find_long_sweep(self, bars: list[Candle], levels: list[float]) -> tuple[int, float] | None:
        start = next((i for i, bar in enumerate(bars) if bar.timestamp.time() >= self.config.opening_range_end), 0)
        for i in range(start, len(bars)):
            for level in levels:
                if bars[i].low < level and bars[i].close > level:
                    return i, level
        return None

    def _find_reclaim(self, bars: list[Candle], sweep_index: int, level: float) -> int | None:
        end = min(len(bars), sweep_index + self.config.reclaim_bars + 1)
        for i in range(sweep_index, end):
            if bars[i].close > level:
                return i
        return None

    def _find_vwap_reclaim(self, bars: list[Candle], vwap: list[float], start_index: int) -> int | None:
        end = min(len(bars), start_index + self.config.vwap_reclaim_window + 1)
        for i in range(start_index, end):
            if bars[i].close > vwap[i] and bars[max(0, i - 1)].close <= vwap[max(0, i - 1)]:
                return i
        return None

    def _latest_5m_closes_above_vwap(self, bars: list[Candle], vwap: list[float]) -> bool:
        if len(bars) < 5:
            return False
        recent = bars[-5:]
        recent_vwap = vwap[-5:]
        return recent[-1].timestamp.minute % 5 == 4 and recent[-1].close > recent_vwap[-1]

    @staticmethod
    def _breaks_previous_1m_high(bars: list[Candle]) -> bool:
        if len(bars) < 2:
            return False
        return bars[-1].close > bars[-2].high

    def _volume_confirms(self, bars: list[Candle], lookback: int = 5) -> bool:
        if len(bars) <= lookback:
            return False
        sample = [bar.volume for bar in bars[-lookback - 1:-1] if bar.volume > 0]
        if not sample:
            return True
        return bars[-1].volume >= (sum(sample) / len(sample)) * self.config.min_volume_ratio

    @staticmethod
    def _intraday_vwap(bars: list[Candle]) -> list[float]:
        values: list[float] = []
        cumulative_pv = 0.0
        cumulative_volume = 0
        for bar in bars:
            volume = max(int(bar.volume), 0)
            typical = (bar.high + bar.low + bar.close) / 3
            cumulative_pv += typical * volume
            cumulative_volume += volume
            values.append(cumulative_pv / cumulative_volume if cumulative_volume > 0 else typical)
        return values
