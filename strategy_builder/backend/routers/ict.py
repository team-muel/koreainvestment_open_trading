"""ICT v1 automatic paper-trading API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import get_current_mode, is_authenticated
from backend.ict_engine import ict_engine

router = APIRouter()


class StartRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    config_id: str = "default_ict_v1"


class StopRequest(BaseModel):
    cancel_pending: bool = True


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    start: str
    end: str


def _ensure_paper_authenticated() -> None:
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="KIS authentication is required")
    if get_current_mode() != "vps":
        raise HTTPException(status_code=400, detail="ICT v1 automatic trading is vps paper mode only")


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
