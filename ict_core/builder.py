"""ICT setup builder for the fixed v1 long-only strategy."""

from __future__ import annotations

from .detectors import (
    FVGDetector,
    LiquidityDetector,
    OrderBlockDetector,
    StructureDetector,
    SwingDetector,
    atr_at,
    is_displacement,
)
from .models import Candle, ICTSetup, TradePlan, TradeState


def krx_tick_size(price: float) -> int:
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


class ICTSetupBuilder:
    def __init__(
        self,
        min_rr: float = 2.0,
        max_trigger_age_bars: int = 1,
        min_liquidity_score: int = 5,
    ):
        self.min_rr = min_rr
        self.max_trigger_age_bars = max_trigger_age_bars
        self.min_liquidity_score = min_liquidity_score

    def build_long_setup(
        self,
        symbol: str,
        candles_5m: list[Candle],
        candles_1h: list[Candle],
        candles_4h: list[Candle],
        candles_30m: list[Candle] | None = None,
        candles_1d: list[Candle] | None = None,
    ) -> ICTSetup:
        notes: list[str] = []
        if len(candles_5m) < 30 or len(candles_1h) < 20 or len(candles_4h) < 10:
            return ICTSetup(symbol=symbol, trend="neutral", state=TradeState.WARMING_UP, notes=["not enough bars"])

        last_price = candles_5m[-1].close
        daily_context = self._daily_context(candles_1d or [])
        if daily_context == "bearish":
            return ICTSetup(
                symbol=symbol,
                trend="bearish",
                state=TradeState.WATCHING_POI,
                last_price=last_price,
                daily_context=daily_context,
                notes=["daily context is bearish"],
            )

        swings_4h = SwingDetector.find_swings(candles_4h)
        trend = StructureDetector.latest_trend(candles_4h, swings_4h)
        if trend != "bullish":
            return ICTSetup(
                symbol=symbol,
                trend=trend,  # type: ignore[arg-type]
                state=TradeState.WATCHING_POI,
                last_price=last_price,
                daily_context=daily_context,
                notes=["4h trend is not bullish"],
            )

        swings_1h = SwingDetector.find_swings(candles_1h)
        active_poi = self._active_1h_poi(candles_1h, swings_1h, last_price)
        if active_poi is None:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WATCHING_POI,
                last_price=last_price,
                daily_context=daily_context,
                notes=["price is outside fresh 1h bullish POI"],
            )

        poi_type, poi_low, poi_high = active_poi
        trap_30m = self._has_30m_trap(candles_30m or [], poi_low, poi_high)
        if candles_30m is not None and not trap_30m:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                daily_context=daily_context,
                notes=["waiting for 30m liquidity trap inside POI"],
            )

        swings_5m = SwingDetector.find_swings(candles_5m)
        sweep = LiquidityDetector.find_bullish_sweep(candles_5m, swings_5m)
        if sweep is None:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                daily_context=daily_context,
                trap_30m=trap_30m,
                notes=["waiting for 5m sell-side sweep"],
            )

        sweep_index, sweep_target = sweep
        choch_index = self._find_bullish_choch(candles_5m, swings_5m, sweep_index)
        if choch_index is None:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                sweep_index=sweep_index,
                daily_context=daily_context,
                trap_30m=trap_30m,
                notes=["sweep found; waiting for 5m CHoCH"],
            )
        if len(candles_5m) - 1 - choch_index > self.max_trigger_age_bars:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                sweep_index=sweep_index,
                choch_index=choch_index,
                daily_context=daily_context,
                trap_30m=trap_30m,
                notes=["5m CHoCH trigger is stale"],
            )

        entry_ob = OrderBlockDetector.entry_ob_before_choch(candles_5m, sweep_index, choch_index)
        if entry_ob is None:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                sweep_index=sweep_index,
                choch_index=choch_index,
                daily_context=daily_context,
                trap_30m=trap_30m,
                notes=["CHoCH found but entry OB is missing"],
            )

        entry = entry_ob.midpoint
        atr = atr_at(candles_5m, choch_index, 14)
        tick_buffer = krx_tick_size(entry) * 2
        stop_buffer = max(tick_buffer, atr * 0.1)
        stop = min(candles_5m[sweep_index].low, sweep_target.price) - stop_buffer

        pools = LiquidityDetector.pools_from_swings(swings_1h, "1h")
        pools.extend(LiquidityDetector.pools_from_swings(swings_4h, "4h"))
        pools = LiquidityDetector.clustered_pools(pools)
        target_pool = LiquidityDetector.nearest_buy_side_above(entry, pools)
        if target_pool is None:
            return ICTSetup(
                symbol=symbol,
                trend="bullish",
                state=TradeState.WAITING_TRIGGER,
                last_price=last_price,
                poi_type=poi_type,
                poi_low=poi_low,
                poi_high=poi_high,
                sweep_index=sweep_index,
                choch_index=choch_index,
                entry_ob=entry_ob,
                daily_context=daily_context,
                trap_30m=trap_30m,
                notes=["no buy-side liquidity target above entry"],
            )

        liquidity_score = self._liquidity_score(
            daily_context=daily_context,
            trend=trend,
            trap_30m=trap_30m,
            target_pool=target_pool,
            candles_5m=candles_5m,
            choch_index=choch_index,
        )
        if liquidity_score < self.min_liquidity_score:
            notes.append(f"liquidity score below threshold: {liquidity_score}")

        risk = entry - stop
        reward = target_pool.price - entry
        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr:
            notes.append(f"risk reward below threshold: {rr:.2f}")
            trade_plan = None
        elif liquidity_score < self.min_liquidity_score:
            trade_plan = None
        else:
            trade_plan = TradePlan(
                symbol=symbol,
                side="buy",
                entry=entry,
                stop=stop,
                take_profit=target_pool.price,
                risk_reward=rr,
                reason="4H bullish, 1H POI, 5M sweep+CHoCH, 5M OB limit",
            )

        return ICTSetup(
            symbol=symbol,
            trend="bullish",
            state=TradeState.WAITING_TRIGGER if trade_plan is None else TradeState.WATCHING_POI,
            last_price=last_price,
            poi_type=poi_type,
            poi_low=poi_low,
            poi_high=poi_high,
            sweep_index=sweep_index,
            choch_index=choch_index,
            entry_ob=entry_ob,
            trade_plan=trade_plan,
            daily_context=daily_context,
            trap_30m=trap_30m,
            liquidity_score=liquidity_score,
            notes=notes,
        )

    def _active_1h_poi(self, candles: list[Candle], swings: list, price: float) -> tuple[str, float, float] | None:
        obs = [
            ob for ob in OrderBlockDetector.find_bullish_order_blocks(candles, swings, "1h")
            if not ob.is_stale and ob.contains(price)
        ]
        if obs:
            ob = obs[-1]
            return "order_block", ob.low, ob.high

        fvgs = [
            fvg for fvg in FVGDetector.find_fvgs(candles)
            if fvg.direction == "bullish" and not fvg.is_stale and fvg.contains(price)
        ]
        if fvgs:
            fvg = fvgs[-1]
            return "fvg", fvg.lower, fvg.upper
        return None

    def _find_bullish_choch(self, candles: list[Candle], swings: list, sweep_index: int) -> int | None:
        previous_highs = [s for s in swings if s.kind == "high" and s.index < sweep_index]
        if not previous_highs:
            return None
        target = previous_highs[-1]
        end = min(len(candles), sweep_index + 13)
        for i in range(sweep_index + 1, end):
            if candles[i].close > target.price and is_displacement(candles, i):
                return i
        return None

    def _daily_context(self, candles: list[Candle]) -> str:
        if len(candles) < 5:
            return "neutral"
        swings = SwingDetector.find_swings(candles)
        return StructureDetector.latest_trend(candles, swings)

    def _has_30m_trap(self, candles: list[Candle], poi_low: float, poi_high: float) -> bool:
        if len(candles) < 10:
            return False
        recent = candles[-24:]
        swings = SwingDetector.find_swings(recent)
        sweep = LiquidityDetector.find_bullish_sweep(recent, swings)
        if sweep is None:
            return False
        sweep_index, _target = sweep
        if not any(c.low <= poi_high and c.high >= poi_low for c in recent[max(0, sweep_index - 2):sweep_index + 1]):
            return False
        choch_index = self._find_bullish_choch(recent, swings, sweep_index)
        if choch_index is not None:
            return True
        fvgs = FVGDetector.find_fvgs(recent)
        return any(fvg.direction == "bullish" and fvg.end_index >= sweep_index for fvg in fvgs)

    def _liquidity_score(
        self,
        daily_context: str,
        trend: str,
        trap_30m: bool,
        target_pool,
        candles_5m: list[Candle],
        choch_index: int,
    ) -> int:
        score = 0
        if daily_context != "bearish":
            score += 2
        if trend == "bullish":
            score += 2
        if trap_30m:
            score += 2
        score += min(2, getattr(target_pool, "score", 1))
        if self._volume_confirms(candles_5m, choch_index):
            score += 1
        return score

    @staticmethod
    def _volume_confirms(candles: list[Candle], index: int, lookback: int = 20) -> bool:
        if index <= 0 or index >= len(candles):
            return False
        start = max(0, index - lookback)
        sample = [c.volume for c in candles[start:index] if c.volume > 0]
        if not sample:
            return False
        return candles[index].volume >= (sum(sample) / len(sample)) * 1.2
