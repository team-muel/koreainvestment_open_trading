"""ICT v1 automatic paper-trading API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import get_current_mode, is_authenticated
from backend.ict_engine import ict_engine
from core.universe_scanner import KRXUniverseScanner, UniverseFilterConfig

router = APIRouter()
universe_scanner = KRXUniverseScanner(cache=ict_engine.cache)


class StartRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    config_id: str = "default_ict_v1"


class StopRequest(BaseModel):
    cancel_pending: bool = True


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    start: str
    end: str


class UniverseScanRequest(BaseModel):
    min_price: int = 2000
    min_avg_trading_value: int = 3_000_000_000
    min_last_trading_value: int = 5_000_000_000
    min_prev_change_pct: float = 0.01
    max_prev_change_pct: float = 0.08
    min_relative_volume: float = 2.0
    daily_lookback_days: int = 25
    max_scan_symbols: int = 200
    watchlist_limit: int = 30
    request_delay: float = 1.0
    require_ready_cache: bool = False
    use_volume_rank: bool = True


class CachedSetupScanRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    limit: int = 30


def _ensure_paper_authenticated() -> None:
    from backend.ict_engine import LIVE_TRADING_ENABLED
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="KIS authentication is required")
    if get_current_mode() != "vps":
        raise HTTPException(status_code=400, detail="ICT v1 automatic trading is vps paper mode only")
    if LIVE_TRADING_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="LIVE_TRADING_ENABLED=True blocks paper mode. Set to False in ict_engine.py."
        )


@router.get("/status")
async def get_status():
    status = ict_engine.status()
    status["mode"] = get_current_mode()
    status["authenticated"] = is_authenticated()
    return status


@router.post("/start")
async def start_engine(request: StartRequest):
    _ensure_paper_authenticated()
    if request.config_id != "default_ict_v1":
        raise HTTPException(status_code=400, detail="unsupported ICT config_id")
    try:
        return ict_engine.start(request.symbols)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop")
async def stop_engine(request: StopRequest):
    if request.cancel_pending:
        _ensure_paper_authenticated()
    return ict_engine.stop(cancel_pending=request.cancel_pending, env_dv="vps")


@router.get("/setups")
async def get_setups():
    return {"setups": ict_engine.setups()}


@router.get("/chart/{symbol}")
async def get_chart(symbol: str, timeframe: str = "5m", limit: int = 160):
    try:
        return ict_engine.chart_data(symbol=symbol, timeframe=timeframe, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/backtest")
async def backtest(request: BacktestRequest):
    return ict_engine.backtest_from_cache(request.symbols, request.start, request.end)


@router.post("/universe/collect")
async def collect_universe_master():
    return universe_scanner.collect_master()


@router.post("/universe/scan")
async def scan_universe(request: UniverseScanRequest):
    _ensure_paper_authenticated()
    config = UniverseFilterConfig(
        min_price=request.min_price,
        min_avg_trading_value=request.min_avg_trading_value,
        min_last_trading_value=request.min_last_trading_value,
        min_prev_change_pct=request.min_prev_change_pct,
        max_prev_change_pct=request.max_prev_change_pct,
        min_relative_volume=request.min_relative_volume,
        daily_lookback_days=request.daily_lookback_days,
        max_scan_symbols=request.max_scan_symbols,
        watchlist_limit=request.watchlist_limit,
        request_delay=request.request_delay,
        require_ready_cache=request.require_ready_cache,
        use_volume_rank=request.use_volume_rank,
    )
    return universe_scanner.scan(config=config, env_dv="vps")


@router.post("/universe/cached-setups")
async def scan_cached_setups(request: CachedSetupScanRequest):
    return universe_scanner.scan_cached_ict_setups(request.symbols, limit=request.limit)
