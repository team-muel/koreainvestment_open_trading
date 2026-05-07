"""Intraday ICT liquidity reclaim strategy for KRX day trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from .builder import krx_tick_size
from .models import Candle, ICTSetup, TradePlan, TradeState


@dataclass(frozen=True)
class IntradayReclaimConfig:
    market_open: time = time(9, 0)
    opening_range_end: time = time(9, 15)
    earliest_entry: time = time(9, 15)
    latest_entry: time = time(10, 30)
    force_exit: time = time(14, 50)
    min_price: float = 2000
    max_stop_pct: float = 0.01
    fixed_stop_pct: float = 0.005
    reclaim_bars: int = 5
    min_sweep_pct: float = 0.001
    vwap_reclaim_window: int = 5
    vwap_confirm_closes: int = 2
    min_volume_ratio: float = 1.5
    min_entry_buffer_ticks: int = 1
    partial_r: float = 1.0
    final_r: float = 2.0
    min_rr: float = 1.5


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
        details: dict[str, Any] = {
            "setup": {"watchlist_required": True},
            "trigger": {},
            "execution": {},
            "exit": {
                "partial_r": self.config.partial_r,
                "final_r": self.config.final_r,
                "force_exit_time": self.config.force_exit.strftime("%H:%M"),
            },
        }
        bars = sorted(candles_1m, key=lambda candle: candle.timestamp)
        if len(bars) < 20:
            return self._setup(symbol, "neutral", TradeState.WARMING_UP, notes + ["not enough 1m bars"], details)

        current_day = bars[-1].timestamp.date()
        day_bars = [bar for bar in bars if bar.timestamp.date() == current_day and self.config.market_open <= bar.timestamp.time() <= time(15, 30)]
        if len(day_bars) < 16:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["waiting for opening range"], details, last_price=bars[-1].close)

        last = day_bars[-1]
        details["setup"]["current_time"] = last.timestamp.strftime("%H:%M")
        if last.close < self.config.min_price:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["price below minimum filter"], details, last_price=last.close)
        if last.timestamp.time() < self.config.earliest_entry:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["observe only before 09:15"], details, last_price=last.close)
        if last.timestamp.time() > self.config.latest_entry:
            return self._setup(symbol, "neutral", TradeState.CANCELLED, notes + ["entry window closed"], details, last_price=last.close)

        opening = [bar for bar in day_bars if self.config.market_open <= bar.timestamp.time() < self.config.opening_range_end]
        if not opening:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["opening range missing"], details, last_price=last.close)
        or_high = max(bar.high for bar in opening)
        or_low = min(bar.low for bar in opening)
        prev_low = self._previous_day_low(candles_1d or [], current_day)
        prev_high = self._previous_day_high(candles_1d or [], current_day)
        details["setup"].update({
            "opening_range_high": or_high,
            "opening_range_low": or_low,
            "previous_day_high": prev_high,
            "previous_day_low": prev_low,
            "distance_to_prev_high_pct": None if prev_high is None else (prev_high - last.close) / last.close,
            "distance_to_prev_low_pct": None if prev_low is None else (last.close - prev_low) / last.close,
        })
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
                details=details,
            )

        reclaim_index = self._find_reclaim(day_bars, sweep_index, sweep_level)
        sweep_low = min(bar.low for bar in day_bars[sweep_index: min(len(day_bars), sweep_index + self.config.reclaim_bars + 1)])
        sweep_depth_pct = (sweep_level - sweep_low) / sweep_level if sweep_level > 0 else 0.0
        details["trigger"].update({
            "liquidity_level": sweep_level,
            "sweep_index": sweep_index,
            "sweep_time": day_bars[sweep_index].timestamp.isoformat(),
            "sweep_low": sweep_low,
            "sweep_depth_pct": sweep_depth_pct,
            "sweep_confirmed": sweep_depth_pct >= self.config.min_sweep_pct,
        })
        if sweep_depth_pct < self.config.min_sweep_pct:
            return self._setup(
                symbol,
                "neutral",
                TradeState.WAITING_TRIGGER,
                notes + [f"sweep depth below threshold: {sweep_depth_pct:.2%}"],
                details,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
            )
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
                details=details,
            )
        details["trigger"].update({
            "reclaim_index": reclaim_index,
            "reclaim_minutes": reclaim_index - sweep_index,
            "reclaim_confirmed": True,
        })

        vwap_values = self._intraday_vwap(day_bars)
        vwap_reclaim_index = self._find_vwap_reclaim(day_bars, vwap_values, reclaim_index)
        latest_vwap = vwap_values[-1]
        details["trigger"]["vwap"] = latest_vwap
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
                details=details,
            )
        details["trigger"].update({
            "vwap_reclaim_index": vwap_reclaim_index,
            "vwap_reclaim_time": day_bars[vwap_reclaim_index].timestamp.isoformat(),
            "vwap_reclaim_confirmed": True,
        })

        consecutive_above = self._consecutive_closes_above_vwap(day_bars, vwap_values)
        details["trigger"]["vwap_consecutive_closes"] = consecutive_above
        if consecutive_above < self.config.vwap_confirm_closes:
            return self._setup(
                symbol,
                "neutral",
                TradeState.WAITING_TRIGGER,
                notes + [f"waiting for {self.config.vwap_confirm_closes} consecutive closes above VWAP"],
                details,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                choch_index=vwap_reclaim_index,
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
                details=details,
            )
        details["trigger"]["five_min_vwap_close_confirmed"] = True

        if not self._breaks_previous_1m_high(day_bars, sweep_index=sweep_index):
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for previous 1m high break"],
                details=details,
            )
        details["trigger"]["mss_confirmed"] = True

        volume_ratio = self._volume_ratio(day_bars)
        details["trigger"]["volume_ratio"] = volume_ratio
        if volume_ratio < self.config.min_volume_ratio:
            return ICTSetup(
                symbol=symbol,
                trend="neutral",
                state=TradeState.WAITING_TRIGGER,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                notes=notes + ["waiting for volume confirmation"],
                details=details,
            )

        entry = last.close
        tick = krx_tick_size(entry)
        if entry <= 0:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["invalid entry"], details)

        vwap_stop = vwap_values[-1] - tick
        structural_stop = sweep_low - (tick * 2)
        fixed_stop = entry * (1 - self.config.fixed_stop_pct)
        stop = min(structural_stop, vwap_stop, fixed_stop)
        risk = entry - stop
        details["execution"].update({
            "entry_candidate": entry,
            "stop_candidate": stop,
            "structural_stop": structural_stop,
            "vwap_stop": vwap_stop,
            "fixed_stop": fixed_stop,
            "risk_per_share": risk,
            "risk_pct": risk / entry if entry > 0 else None,
            "minimum_rr": self.config.min_rr,
        })
        if risk <= 0:
            return self._setup(symbol, "neutral", TradeState.WAITING_TRIGGER, notes + ["invalid stop"], details, last_price=last.close)
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
                details=details,
            )

        partial_tp = entry + risk * self.config.partial_r
        final_tp = entry + risk * self.config.final_r
        rr = (final_tp - entry) / risk if risk > 0 else 0.0
        details["execution"].update({
            "partial_take_profit": partial_tp,
            "final_take_profit": final_tp,
            "risk_reward": rr,
        })
        if rr < self.config.min_rr:
            return self._setup(
                symbol,
                "neutral",
                TradeState.WAITING_TRIGGER,
                notes + [f"risk reward below threshold: {rr:.2f}"],
                details,
                last_price=last.close,
                poi_low=or_low,
                poi_high=or_high,
                sweep_index=sweep_index,
                choch_index=vwap_reclaim_index,
            )
        plan = TradePlan(
            symbol=symbol,
            side="buy",
            entry=entry,
            stop=stop,
            take_profit=final_tp,
            partial_take_profit=partial_tp,
            final_take_profit=final_tp,
            force_exit_time=self.config.force_exit.strftime("%H:%M"),
            risk_reward=rr,
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
                f"Scan Reason: OR/previous low sweep depth {sweep_depth_pct:.2%}, VWAP reclaim, MSS, volume {volume_ratio:.2f}x",
                "long setup confirmed",
            ],
            details=details,
        )

    def _previous_day_low(self, candles_1d: list[Candle], current_day) -> float | None:
        previous = [bar for bar in candles_1d if bar.timestamp.date() < current_day]
        if not previous:
            return None
        return previous[-1].low

    def _previous_day_high(self, candles_1d: list[Candle], current_day) -> float | None:
        previous = [bar for bar in candles_1d if bar.timestamp.date() < current_day]
        if not previous:
            return None
        return previous[-1].high

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
        """마지막 완성된 5분봉이 VWAP 위에서 마감했는지 확인.

        minute % 5 == 4 조건 대신 실제 5분봉 resample 후
        완성된(현재 진행 중이 아닌) 마지막 5분봉의 close를 사용한다.
        """
        if len(bars) < 6:
            return False

        # 5분봉 resample: 같은 5분 버킷에 속하는 봉들을 묶어서 OHLCV 합산
        # bucket key = timestamp를 5분 단위로 내림 (floor)
        from collections import defaultdict
        buckets: dict[int, list[tuple[Candle, float]]] = defaultdict(list)
        for bar, v in zip(bars, vwap):
            t = bar.timestamp
            # 5분 버킷: (hour * 60 + minute) // 5
            bucket_key = (t.hour * 60 + t.minute) // 5
            buckets[bucket_key].append((bar, v))

        sorted_keys = sorted(buckets.keys())
        if len(sorted_keys) < 2:
            return False

        # 현재 진행 중인 버킷(마지막)은 제외, 그 직전 완성된 5분봉 사용
        last_complete_key = sorted_keys[-2]
        complete_bars = buckets[last_complete_key]

        # 5분봉 종가 = 버킷 내 마지막 1분봉의 close
        five_min_close = complete_bars[-1][0].close
        # 5분봉 VWAP = 버킷 내 마지막 1분봉 시점의 VWAP
        five_min_vwap = complete_bars[-1][1]

        return five_min_close > five_min_vwap

    @staticmethod
    def _breaks_previous_1m_high(bars: list[Candle], sweep_index: int | None = None) -> bool:
        """sweep 이후 형성된 swing high 돌파 여부 확인 (MSS/CHoCH 기준).

        단순 직전 봉 고가 비교 대신, sweep 이후 형성된
        local swing high (좌우 최소 1봉이 낮은 고점)를 찾아 돌파 여부를 확인한다.
        """
        if len(bars) < 5:
            return False

        # sweep 이후 구간만 대상으로 swing high 탐색
        start = (sweep_index + 1) if sweep_index is not None and sweep_index < len(bars) - 3 else max(0, len(bars) - 20)
        search_bars = bars[start:]

        if len(search_bars) < 4:
            return False

        last = bars[-1]

        # swing high: 좌우 각 1봉 이상이 낮은 고점
        swing_highs: list[float] = []
        for i in range(1, len(search_bars) - 1):
            bar = search_bars[i]
            left = search_bars[i - 1]
            right = search_bars[i + 1]
            if bar.high > left.high and bar.high > right.high:
                swing_highs.append(bar.high)

        if not swing_highs:
            # swing high가 없으면 직전 N봉 중 최고가로 fallback
            fallback_high = max(b.high for b in search_bars[:-1])
            return last.close > fallback_high

        # 가장 최근 swing high 돌파 여부
        recent_swing_high = swing_highs[-1]
        return last.close > recent_swing_high

    def _volume_confirms(self, bars: list[Candle], lookback: int = 5) -> bool:
        return self._volume_ratio(bars, lookback=lookback) >= self.config.min_volume_ratio

    @staticmethod
    def _volume_ratio(bars: list[Candle], lookback: int = 5) -> float:
        """현재 봉의 거래량 비율 계산.

        거래량 데이터가 없으면 999.0(통과) 대신 0.0(차단)을 반환한다.
        자동 진입에서는 데이터 없음과 거래량 0을 동일하게 취급해야 안전하다.
        """
        if len(bars) <= lookback:
            return 0.0
        sample = [bar.volume for bar in bars[-lookback - 1:-1] if bar.volume > 0]
        if not sample:
            return 0.0  # 데이터 없음 → 진입 차단
        average = sum(sample) / len(sample)
        if average <= 0:
            return 0.0
        current_volume = bars[-1].volume
        if current_volume <= 0:
            return 0.0
        return current_volume / average

    @staticmethod
    def _consecutive_closes_above_vwap(bars: list[Candle], vwap: list[float]) -> int:
        count = 0
        for bar, value in zip(reversed(bars), reversed(vwap)):
            if bar.close <= value:
                break
            count += 1
        return count

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

    @staticmethod
    def _setup(
        symbol: str,
        trend: str,
        state: TradeState,
        notes: list[str],
        details: dict[str, Any],
        *,
        last_price: float | None = None,
        poi_low: float | None = None,
        poi_high: float | None = None,
        sweep_index: int | None = None,
        choch_index: int | None = None,
    ) -> ICTSetup:
        return ICTSetup(
            symbol=symbol,
            trend=trend,  # type: ignore[arg-type]
            state=state,
            last_price=last_price,
            poi_type="none",
            poi_low=poi_low,
            poi_high=poi_high,
            sweep_index=sweep_index,
            choch_index=choch_index,
            notes=notes,
            details=details,
        )
