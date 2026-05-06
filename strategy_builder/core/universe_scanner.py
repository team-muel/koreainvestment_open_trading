"""KOSPI/KOSDAQ universe scanner for ICT watchlist selection."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ict_core import ICTSetup, IntradayLiquidityReclaimBuilder

from . import data_fetcher
from .ict_cache import MinuteBarCache
from .symbol_master import SymbolInfo, SymbolMaster


EXCLUDED_NAME_KEYWORDS = (
    "ETF",
    "ETN",
    "ELW",
    "스팩",
    "SPAC",
    "리츠",
    "REIT",
    "인버스",
    "레버리지",
    "선물",
    "옵션",
)


@dataclass(frozen=True)
class UniverseFilterConfig:
    min_price: int = 2000
    min_avg_trading_value: int = 3_000_000_000
    min_last_trading_value: int = 5_000_000_000
    min_prev_change_pct: float = 0.01
    max_prev_change_pct: float = 0.08
    daily_lookback_days: int = 25
    max_scan_symbols: int = 200
    watchlist_limit: int = 30
    request_delay: float = 1.0
    require_ready_cache: bool = False
    use_volume_rank: bool = True


@dataclass(frozen=True)
class UniverseCandidate:
    symbol: SymbolInfo
    last_close: float
    last_volume: int
    last_trading_value: float
    avg_trading_value: float
    prev_change_pct: float
    ready_coverage_days: int
    setup: dict | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.symbol.code,
            "name": self.symbol.name,
            "exchange": self.symbol.exchange,
            "exchange_name": self.symbol.exchange_name,
            "last_close": self.last_close,
            "last_volume": self.last_volume,
            "last_trading_value": self.last_trading_value,
            "avg_trading_value": self.avg_trading_value,
            "prev_change_pct": self.prev_change_pct,
            "ready_coverage_days": self.ready_coverage_days,
            "setup": self.setup,
        }


class KRXUniverseScanner:
    def __init__(
        self,
        master: SymbolMaster | None = None,
        cache: MinuteBarCache | None = None,
        builder: IntradayLiquidityReclaimBuilder | None = None,
    ):
        self.master = master or SymbolMaster()
        self.cache = cache or MinuteBarCache()
        self.builder = builder or IntradayLiquidityReclaimBuilder()

    def collect_master(self) -> dict:
        return self.master.collect(["kospi", "kosdaq"])

    def scan(self, config: UniverseFilterConfig | None = None, env_dv: str = "vps") -> dict:
        config = config or UniverseFilterConfig()
        symbols = self.master.load_all()
        if not symbols:
            collected = self.collect_master()
            symbols = self.master.load_all()
        else:
            collected = {"success": True, "counts": {}, "total_count": len(symbols), "errors": []}

        filtered_symbols = [symbol for symbol in symbols if self._basic_symbol_ok(symbol)]
        filtered_symbols = self._liquidity_first_order(filtered_symbols, config, env_dv)
        if config.use_volume_rank:
            time.sleep(config.request_delay)
        candidates: list[UniverseCandidate] = []
        scanned = 0
        skipped = 0

        for symbol in filtered_symbols[: config.max_scan_symbols]:
            scanned += 1
            candidate = self._liquidity_candidate(symbol, config, env_dv)
            if candidate is None:
                skipped += 1
            else:
                candidates.append(candidate)
            time.sleep(config.request_delay)

        ranked = sorted(candidates, key=lambda item: item.avg_trading_value, reverse=True)
        watchlist = [candidate.to_dict() for candidate in ranked[: config.watchlist_limit]]
        return {
            "master": collected,
            "filters": {
                "min_price": config.min_price,
                "min_avg_trading_value": config.min_avg_trading_value,
                "min_last_trading_value": config.min_last_trading_value,
                "min_prev_change_pct": config.min_prev_change_pct,
                "max_prev_change_pct": config.max_prev_change_pct,
                "max_scan_symbols": config.max_scan_symbols,
                "watchlist_limit": config.watchlist_limit,
                "require_ready_cache": config.require_ready_cache,
                "use_volume_rank": config.use_volume_rank,
            },
            "universe_count": len(symbols),
            "eligible_name_count": len(filtered_symbols),
            "scanned_count": scanned,
            "skipped_count": skipped,
            "candidate_count": len(ranked),
            "watchlist": watchlist,
            "symbols_for_ict_start": [item["code"] for item in watchlist],
        }

    def scan_cached_ict_setups(self, symbols: list[str], limit: int = 30) -> dict:
        setups: list[dict] = []
        for symbol in symbols[:limit]:
            setup = self._build_setup_from_cache(symbol)
            if setup is not None:
                setups.append(setup.to_dict())
        actionable = [
            setup for setup in setups
            if setup.get("trade_plan") is not None and setup.get("state") != "WARMING_UP"
        ]
        return {
            "scanned_count": min(len(symbols), limit),
            "setup_count": len(setups),
            "actionable_count": len(actionable),
            "setups": setups,
            "actionable_symbols": [setup["symbol"] for setup in actionable],
        }

    def _basic_symbol_ok(self, symbol: SymbolInfo) -> bool:
        name = symbol.name.upper()
        if symbol.exchange not in ("kospi", "kosdaq"):
            return False
        if any(keyword.upper() in name for keyword in EXCLUDED_NAME_KEYWORDS):
            return False
        if symbol.name.endswith(("우", "우B", "우C")):
            return False
        return True

    def _liquidity_candidate(
        self,
        symbol: SymbolInfo,
        config: UniverseFilterConfig,
        env_dv: str,
    ) -> UniverseCandidate | None:
        df = data_fetcher.get_daily_prices(symbol.code, days=config.daily_lookback_days, env_dv=env_dv)
        if df.empty:
            return None
        last = df.iloc[-1]
        last_close = float(last["close"])
        last_volume = int(last["volume"])
        last_trading_value = last_close * last_volume
        avg_trading_value = float((df["close"] * df["volume"]).tail(20).mean())
        prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0.0
        prev_change_pct = ((last_close - prev_close) / prev_close) if prev_close > 0 else 0.0
        ready_days = self.cache.ready_coverage_days(symbol.code)

        if last_close < config.min_price:
            return None
        if last_trading_value < config.min_last_trading_value:
            return None
        if avg_trading_value < config.min_avg_trading_value:
            return None
        if prev_change_pct < config.min_prev_change_pct:
            return None
        if prev_change_pct > config.max_prev_change_pct:
            return None
        if config.require_ready_cache and ready_days < 20:
            return None

        setup = self._build_setup_from_cache(symbol.code)
        return UniverseCandidate(
            symbol=symbol,
            last_close=last_close,
            last_volume=last_volume,
            last_trading_value=last_trading_value,
            avg_trading_value=avg_trading_value,
            prev_change_pct=prev_change_pct,
            ready_coverage_days=ready_days,
            setup=None if setup is None else setup.to_dict(),
        )

    def _liquidity_first_order(
        self,
        symbols: list[SymbolInfo],
        config: UniverseFilterConfig,
        env_dv: str,
    ) -> list[SymbolInfo]:
        if not config.use_volume_rank:
            return symbols
        rank = data_fetcher.get_volume_rank(
            env_dv=env_dv,
            min_price=config.min_price,
            min_volume=100_000,
        )
        if rank.empty or "code" not in rank.columns:
            return symbols
        by_code = {symbol.code: symbol for symbol in symbols}
        ranked: list[SymbolInfo] = []
        seen: set[str] = set()
        for code in rank["code"].astype(str):
            symbol = by_code.get(code.zfill(6))
            if symbol is not None and symbol.code not in seen:
                ranked.append(symbol)
                seen.add(symbol.code)
        ranked.extend(symbol for symbol in symbols if symbol.code not in seen)
        return ranked

    def _build_setup_from_cache(self, symbol: str) -> ICTSetup | None:
        bars_1m = self.cache.get_1m_bars(symbol)
        if len(bars_1m) < 300:
            return None
        bars_1d = self.cache.resample(bars_1m, 390, "1d")
        return self.builder.build_long_setup(symbol, bars_1m, bars_1d)
