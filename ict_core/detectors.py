"""Pure ICT detectors used by live trading and backtest code."""

from __future__ import annotations

from .models import Candle, FVG, LiquidityPool, OrderBlock, SwingPoint


def atr_at(candles: list[Candle], index: int, period: int = 14) -> float:
    """Return ATR at index using completed bars up to index."""
    if index <= 0 or not candles:
        return 0.0
    start = max(1, index - period + 1)
    ranges: list[float] = []
    for i in range(start, index + 1):
        current = candles[i]
        prev_close = candles[i - 1].close
        ranges.append(max(
            current.high - current.low,
            abs(current.high - prev_close),
            abs(current.low - prev_close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


def is_displacement(candles: list[Candle], index: int) -> bool:
    atr = atr_at(candles, index, 14)
    if atr <= 0:
        return False
    candle = candles[index]
    return candle.body >= atr * 1.2 or candle.range >= atr * 1.5


class SwingDetector:
    @staticmethod
    def find_swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[SwingPoint]:
        swings: list[SwingPoint] = []
        if len(candles) < left + right + 1:
            return swings
        for i in range(left, len(candles) - right):
            window = candles[i - left:i + right + 1]
            high = candles[i].high
            low = candles[i].low
            if high == max(c.high for c in window) and sum(1 for c in window if c.high == high) == 1:
                swings.append(SwingPoint(i, candles[i].timestamp, high, "high"))
            if low == min(c.low for c in window) and sum(1 for c in window if c.low == low) == 1:
                swings.append(SwingPoint(i, candles[i].timestamp, low, "low"))
        return swings


class StructureDetector:
    @staticmethod
    def latest_trend(candles: list[Candle], swings: list[SwingPoint]) -> str:
        """Return bullish, bearish, or neutral based on latest close break of confirmed swings."""
        last_event: tuple[int, str] | None = None
        swing_highs = [s for s in swings if s.kind == "high"]
        swing_lows = [s for s in swings if s.kind == "low"]
        for i, candle in enumerate(candles):
            prev_highs = [s for s in swing_highs if s.index < i]
            prev_lows = [s for s in swing_lows if s.index < i]
            if prev_highs and candle.close > prev_highs[-1].price:
                last_event = (i, "bullish")
            if prev_lows and candle.close < prev_lows[-1].price:
                last_event = (i, "bearish")
        return last_event[1] if last_event else "neutral"


class FVGDetector:
    @staticmethod
    def find_fvgs(candles: list[Candle]) -> list[FVG]:
        fvgs: list[FVG] = []
        for i in range(2, len(candles)):
            left = candles[i - 2]
            current = candles[i]
            if left.high < current.low:
                fvg = FVG(
                    direction="bullish",
                    start_index=i - 2,
                    end_index=i,
                    lower=left.high,
                    upper=current.low,
                    created_at=current.timestamp,
                    mitigations=FVGDetector._count_mitigations(candles, i + 1, left.high, current.low),
                )
                fvgs.append(fvg)
            if left.low > current.high:
                fvg = FVG(
                    direction="bearish",
                    start_index=i - 2,
                    end_index=i,
                    lower=current.high,
                    upper=left.low,
                    created_at=current.timestamp,
                    mitigations=FVGDetector._count_mitigations(candles, i + 1, current.high, left.low),
                )
                fvgs.append(fvg)
        return fvgs

    @staticmethod
    def _count_mitigations(candles: list[Candle], start: int, lower: float, upper: float) -> int:
        count = 0
        in_zone = False
        for candle in candles[start:]:
            touched = candle.low <= upper and candle.high >= lower
            if touched and not in_zone:
                count += 1
            in_zone = touched
        return count


class OrderBlockDetector:
    @staticmethod
    def find_bullish_order_blocks(
        candles: list[Candle],
        swings: list[SwingPoint],
        timeframe: str,
    ) -> list[OrderBlock]:
        obs: list[OrderBlock] = []
        swing_highs = [s for s in swings if s.kind == "high"]
        for i in range(1, len(candles)):
            candle = candles[i]
            previous_highs = [s for s in swing_highs if s.index < i]
            if not previous_highs:
                continue
            if not candle.is_bullish or candle.close <= previous_highs[-1].price:
                continue
            if not is_displacement(candles, i):
                continue
            source_index = OrderBlockDetector._last_bearish_index(candles, 0, i)
            if source_index is None:
                continue
            source = candles[source_index]
            obs.append(OrderBlock(
                direction="bullish",
                index=source_index,
                timestamp=source.timestamp,
                low=source.low,
                high=source.high,
                source_timeframe=timeframe,
                displacement_index=i,
                mitigations=OrderBlockDetector._count_mitigations(
                    candles, i + 1, source.low, source.high
                ),
            ))
        return obs

    @staticmethod
    def entry_ob_before_choch(candles: list[Candle], sweep_index: int, choch_index: int) -> OrderBlock | None:
        source_index = OrderBlockDetector._last_bearish_index(candles, sweep_index, choch_index)
        if source_index is None:
            return None
        source = candles[source_index]
        return OrderBlock(
            direction="bullish",
            index=source_index,
            timestamp=source.timestamp,
            low=source.low,
            high=source.high,
            source_timeframe="5m",
            displacement_index=choch_index,
            mitigations=0,
        )

    @staticmethod
    def _last_bearish_index(candles: list[Candle], start: int, end: int) -> int | None:
        for i in range(end - 1, start - 1, -1):
            if candles[i].is_bearish:
                return i
        return None

    @staticmethod
    def _count_mitigations(candles: list[Candle], start: int, low: float, high: float) -> int:
        count = 0
        in_zone = False
        for candle in candles[start:]:
            touched = candle.low <= high and candle.high >= low
            if touched and not in_zone:
                count += 1
            in_zone = touched
        return count


class LiquidityDetector:
    @staticmethod
    def pools_from_swings(swings: list[SwingPoint], timeframe: str) -> list[LiquidityPool]:
        pools: list[LiquidityPool] = []
        for swing in swings:
            pools.append(LiquidityPool(
                side="buy_side" if swing.kind == "high" else "sell_side",
                price=swing.price,
                timestamp=swing.timestamp,
                source_timeframe=timeframe,
                source_index=swing.index,
            ))
        return pools

    @staticmethod
    def nearest_buy_side_above(price: float, pools: list[LiquidityPool]) -> LiquidityPool | None:
        candidates = [p for p in pools if p.side == "buy_side" and p.price > price]
        return min(candidates, key=lambda p: p.price) if candidates else None

    @staticmethod
    def clustered_pools(
        pools: list[LiquidityPool],
        tolerance_pct: float = 0.002,
    ) -> list[LiquidityPool]:
        if not pools:
            return []
        result: list[LiquidityPool] = []
        for pool in pools:
            tolerance = max(abs(pool.price) * tolerance_pct, 1e-9)
            cluster_size = sum(
                1
                for other in pools
                if other.side == pool.side and abs(other.price - pool.price) <= tolerance
            )
            result.append(LiquidityPool(
                side=pool.side,
                price=pool.price,
                timestamp=pool.timestamp,
                source_timeframe=pool.source_timeframe,
                source_index=pool.source_index,
                score=max(1, cluster_size),
            ))
        return result

    @staticmethod
    def find_bullish_sweep(candles: list[Candle], swings: list[SwingPoint]) -> tuple[int, SwingPoint] | None:
        swing_lows = [s for s in swings if s.kind == "low"]
        for i in range(len(candles) - 1, -1, -1):
            previous_lows = [s for s in swing_lows if s.index < i]
            if not previous_lows:
                continue
            target = previous_lows[-1]
            candle = candles[i]
            if candle.low < target.price and candle.close > target.price:
                return i, target
        return None
