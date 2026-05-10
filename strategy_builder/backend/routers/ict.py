"""ICT v1 automatic paper-trading API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from backend import get_current_mode, is_authenticated
from backend.ict_engine import ict_engine
from core import data_fetcher
from core.universe_scanner import KRXUniverseScanner, UniverseFilterConfig

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
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


# Workflow automation endpoints

def _run_premarket_workflow() -> dict:
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    scan_date = datetime.now(KST).strftime("%Y-%m-%d")

    if not journal.start_job("premarket", scan_date):
        return {"status": "already_completed", "scan_date": scan_date}

    logger.info("Premarket scan workflow started: %s", scan_date)
    try:
        result = universe_scanner.scan(config=UniverseFilterConfig(), env_dv="vps")
        premarket_list = result.get("premarket_list", [])
        watchlist = result.get("watchlist", [])
        engine_symbols = result.get("engine_symbols") or result.get("symbols_for_ict_start") or []

        for item in premarket_list:
            try:
                journal.record_premarket_candidate(item, scan_date)
            except Exception:
                logger.exception("premarket candidate save failed: %s", item.get("code"))

        journal.save_watchlist(scan_date, watchlist)

        try:
            from core.gcal_reporter import publish_gcal_premarket_if_configured
            publish_gcal_premarket_if_configured(
                scan_date=scan_date,
                symbols=[item["code"] for item in watchlist[:10]],
                event_dt=datetime.now(KST),
            )
        except Exception:
            logger.exception("GCal premarket event creation failed")

        # runtime_config watch_symbols 업데이트
        top_symbols = [str(code) for code in engine_symbols[:5] if str(code).isdigit()]
        if top_symbols and journal.enabled:
            journal.set_runtime_config("watch_symbols", ",".join(top_symbols))

        # 핵심: 엔진에 스캔 결과 종목 적용 → PREMARKET_WAIT → ACTIVE 전환
        if top_symbols:
            try:
                engine_status = ict_engine.update_symbols(top_symbols)
                logger.info(
                    "장전 스캔 완료 → 엔진 ACTIVE 전환: %s (engine_state=%s)",
                    top_symbols,
                    engine_status.get("engine_state"),
                )
            except Exception:
                logger.exception("ict_engine.update_symbols 호출 실패")
        else:
            logger.warning("장전 스캔 결과 없음 — 엔진 PREMARKET_WAIT 유지, 신규 진입 차단")

        payload = {
            "scan_date": scan_date,
            "premarket_count": len(premarket_list),
            "watchlist_count": len(watchlist),
            "symbols": top_symbols,
        }
        journal.finish_job("premarket", scan_date, success=True, payload=payload)

        # Pre-market Reason Agent 비동기 실행 (LLM 근거 작성 + Notion 업데이트)
        try:
            from agents.premarket_agent import PremarketAgent
            import threading
            threading.Thread(
                target=lambda: PremarketAgent().run(trade_date=scan_date),
                name="premarket-reason-agent",
                daemon=True,
            ).start()
            logger.info("Pre-market Reason Agent 백그라운드 시작")
        except Exception:
            logger.exception("Pre-market Reason Agent 시작 실패 (비필수)")

        return {"status": "success", **payload}

    except Exception as e:
        logger.exception("Premarket scan workflow error")
        journal.finish_job("premarket", scan_date, success=False, error=str(e))
        return {"status": "error", "error": str(e)}


def _run_postmarket_workflow() -> dict:
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    if not journal.start_job("postmarket", today):
        return {"status": "already_completed", "date": today}

    try:
        from workers.daily_report_worker import DailyReportWorker
        result = DailyReportWorker().run_once(run_agents=True)
        journal.finish_job("postmarket", today, success=True, payload=result)
        return {"status": "success", **result}
    except Exception as e:
        logger.exception("Postmarket workflow error")
        journal.finish_job("postmarket", today, success=False, error=str(e))
        return {"status": "error", "error": str(e)}


@router.post("/workflow/premarket")
async def run_premarket_workflow(background_tasks: BackgroundTasks, sync: bool = False):
    """Trigger premarket scan workflow."""
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return {"status": "skipped", "reason": "weekend"}
    if not (6 <= now.hour < 10):
        return {"status": "skipped", "reason": f"outside premarket hours ({now.strftime('%H:%M')} KST)"}
    if sync:
        return _run_premarket_workflow()
    background_tasks.add_task(_run_premarket_workflow)
    return {"status": "accepted", "message": "premarket scan in background", "time": now.isoformat()}


@router.post("/workflow/postmarket")
async def run_postmarket_workflow(background_tasks: BackgroundTasks, sync: bool = False):
    """Trigger postmarket report workflow."""
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return {"status": "skipped", "reason": "weekend"}
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        return {"status": "skipped", "reason": f"outside postmarket hours ({now.strftime('%H:%M')} KST)"}
    if sync:
        return _run_postmarket_workflow()
    background_tasks.add_task(_run_postmarket_workflow)
    return {"status": "accepted", "message": "postmarket report in background", "time": now.isoformat()}


@router.get("/workflow/status")
async def get_workflow_status():
    """Check today workflow execution status."""
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    premarket_status = journal.get_job_status("premarket", today)
    postmarket_status = journal.get_job_status("postmarket", today)
    watchlist = journal.load_watchlist(today)
    return {
        "date": today,
        "premarket_scan": premarket_status,
        "postmarket_report": postmarket_status,
        "watchlist_symbols": watchlist,
        "watchlist_count": len(watchlist),
        "engine_status": ict_engine.status().get("daily_risk", {}),
    }


@router.get("/workflow/shutdown-check")
async def shutdown_check():
    """VM 종료 전 안전 체크 (16:05 Cloud Scheduler 호출용).
    
    이슈 있으면 503 반환 → VM 종료 중단 + Telegram 알림.
    확인: 보유 포지션 0, 미체결 주문 0, 일일 리포트 완료, 엔진 정상.
    """
    from fastapi.responses import JSONResponse
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    issues = []
    warnings = []
    positions = []

    try:
        status = ict_engine.status()
        positions = status.get("positions", [])
        pending = status.get("pending_orders", [])
        degraded = status.get("degraded", False)
        if positions:
            issues.append(f"보유 포지션 {len(positions)}개: {[p.get('symbol') for p in positions]}")
        if pending:
            issues.append(f"미체결 주문 {len(pending)}개 존재")
        if degraded:
            issues.append(f"엔진 degraded: {status.get('last_error', '')}")
    except Exception as e:
        warnings.append(f"엔진 상태 확인 실패: {e}")

    try:
        holdings, holdings_ok = data_fetcher.get_holdings_checked("vps")
        if not holdings_ok:
            issues.append("KIS 보유종목 조회 실패")
        elif not holdings.empty:
            broker_positions = holdings.to_dict("records")
            if broker_positions:
                issues.append(
                    f"KIS 실제 보유 {len(broker_positions)}개: "
                    f"{[p.get('stock_code') for p in broker_positions[:10]]}"
                )
    except Exception as e:
        issues.append(f"KIS 보유종목 확인 실패: {e}")

    try:
        broker_pending, pending_ok = data_fetcher.get_pending_orders("vps")
        if not pending_ok:
            issues.append("KIS 미체결 조회 실패")
        elif not broker_pending.empty:
            issues.append(f"KIS 실제 미체결 주문 {len(broker_pending)}건 존재")
    except Exception as e:
        issues.append(f"KIS 미체결 확인 실패: {e}")

    try:
        if journal.enabled and journal._client:
            open_positions = journal._client.select("positions", limit=10)
            open_orders = journal._client.select(
                "orders",
                filters={"status": "in.(LIMIT_SUBMITTED,ENTERED,EXITING)"},
                limit=10,
            )
            if open_positions:
                issues.append(f"Supabase positions {len(open_positions)}건 존재")
            if open_orders:
                issues.append(f"Supabase open orders {len(open_orders)}건 존재")
    except Exception as e:
        warnings.append(f"Supabase 상태 확인 실패: {e}")

    report_status = journal.get_job_status("postmarket", today)
    if report_status == "RUNNING":
        warnings.append("일일 리포트 아직 실행 중")
    elif report_status == "FAILED":
        warnings.append("일일 리포트 생성 실패")

    try:
        if journal.enabled and journal._client:
            incomplete = journal._client.select("fills", filters={"is_complete": "eq.false"}, limit=5)
            if incomplete:
                issues.append(f"미완료 fill {len(incomplete)}건 존재")
    except Exception:
        pass

    result = {
        "date": today,
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "positions_count": len(positions),
        "report_status": report_status,
        "checked_at": datetime.now(KST).isoformat(),
    }
    if issues:
        logger.warning("shutdown-check FAILED: %s", issues)
        return JSONResponse(status_code=503, content=result)
    return result
