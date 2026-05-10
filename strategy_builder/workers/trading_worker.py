"""Trading Worker 진입점.

Docker 컨테이너에서 trading-worker 프로세스로 실행된다.

시작 흐름:
  1. 엔진을 PREMARKET_WAIT 모드로 시작 (warmup만 실행, 주문 금지)
  2. 장전 스캔 완료 시 ict_engine.update_symbols() 호출 → ACTIVE 전환
  3. 09:10 이후에도 스캔 미완료 시 현재 종목으로 자동 ACTIVE 전환 (안전망)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# 프로젝트 루트를 path에 추가
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, root)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def main() -> None:
    logger.info("Trading worker starting...")

    # Supabase 연결
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    if not journal.enabled:
        logger.warning("Supabase not configured — SQLite fallback 사용")
    else:
        journal.send_heartbeat("trading-worker", metadata={"status": "starting"})

    # 실전 모드 이중 잠금 확인
    live_enabled = journal.get_runtime_config("live_trading_enabled", "false").lower() == "true"
    if live_enabled:
        logger.critical("LIVE_TRADING_ENABLED=true — 실전 모드 미지원. 종료합니다.")
        sys.exit(1)

    # 엔진 초기화
    from core.ict_cache import build_minute_bar_cache
    from backend.ict_engine import ICTTradingEngine, ICTConfig

    cache = build_minute_bar_cache()
    config = ICTConfig()
    engine = ICTTradingEngine(cache=cache, config=config)

    # 시작 종목 결정:
    # - 오늘 장전 스캔 결과가 Supabase watchlists에 있으면 사용
    # - 없으면 runtime_config watch_symbols 사용
    # - 둘 다 없으면 기본 종목 (warmup용)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    default_symbols = ["005930", "000660", "035720"]  # 삼성전자, SK하이닉스, 카카오

    scanned_symbols = journal.load_watchlist(today) if journal.enabled else []
    if scanned_symbols:
        start_symbols = scanned_symbols
        premarket_already_done = True
        logger.info("오늘 장전 스캔 결과 로드: %s", start_symbols)
    else:
        symbols_val = journal.get_runtime_config("watch_symbols", "") if journal.enabled else ""
        start_symbols = [s.strip() for s in symbols_val.split(",") if s.strip()] if symbols_val else default_symbols
        premarket_already_done = False
        logger.info("장전 스캔 대기 중 — 임시 종목으로 warmup 시작: %s", start_symbols)

    # 엔진 시작 (PREMARKET_WAIT 또는 ACTIVE)
    try:
        engine.start(start_symbols)
        if premarket_already_done:
            # 이미 스캔 결과가 있으면 바로 ACTIVE 전환
            engine.update_symbols(start_symbols)
            logger.info("엔진 ACTIVE 상태로 시작 (오늘 스캔 결과 사용)")
        else:
            logger.info("엔진 PREMARKET_WAIT 상태로 시작 — 장전 스캔 완료 대기 중")
    except Exception:
        logger.exception("엔진 시작 실패")
        sys.exit(1)

    # Heartbeat 루프
    loop_count = 0
    while True:
        loop_count += 1
        time.sleep(60)
        if journal.enabled:
            try:
                status = engine.status()
                journal.send_heartbeat(
                    "trading-worker",
                    loop_count=loop_count,
                    metadata={
                        "running": status.get("running"),
                        "degraded": status.get("degraded"),
                        "engine_state": status.get("engine_state", "UNKNOWN"),
                        "premarket_ready": status.get("premarket_ready", False),
                        "positions": len(status.get("positions", [])),
                        "daily_entries": status.get("daily_risk", {}).get("entries", 0),
                        "symbols": status.get("symbols", []),
                    },
                )
            except Exception:
                logger.exception("Heartbeat 전송 실패")


if __name__ == "__main__":
    main()
