"""Trading Worker 진입점.

Docker 컨테이너에서 trading-worker 프로세스로 실행된다.
"""
from __future__ import annotations

import logging
import os
import sys
import time

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


def main() -> None:
    logger.info("Trading worker starting...")

    # Supabase 연결 확인
    from core.supabase_journal import SupabaseJournal
    journal = SupabaseJournal()
    if not journal.enabled:
        logger.warning("Supabase not configured — using local SQLite fallback")
        from core.ict_journal import ICTJournal
        journal_instance = ICTJournal()
    else:
        journal_instance = journal
        journal.send_heartbeat("trading-worker", metadata={"status": "starting"})

    # 런타임 설정 확인
    live_enabled = False
    if journal.enabled:
        live_val = journal.get_runtime_config("live_trading_enabled", "false")
        live_enabled = live_val.lower() == "true"
    if live_enabled:
        logger.critical("LIVE_TRADING_ENABLED=true — 실전 모드는 아직 지원하지 않습니다. 종료합니다.")
        sys.exit(1)

    # 엔진 초기화 및 시작
    from core.ict_cache import MinuteBarCache
    from backend.ict_engine import ICTTradingEngine, ICTConfig

    cache = MinuteBarCache()
    config = ICTConfig()
    engine = ICTTradingEngine(cache=cache, config=config)

    # Supabase runtime_config에서 감시 종목 로드 (없으면 기본 종목 사용)
    default_symbols = ["005930", "000660", "035720"]  # 삼성전자, SK하이닉스, 카카오
    watch_symbols = default_symbols
    if journal.enabled:
        symbols_val = journal.get_runtime_config("watch_symbols", "")
        if symbols_val:
            watch_symbols = [s.strip() for s in symbols_val.split(",") if s.strip()]

    logger.info("엔진 시작 — 감시 종목: %s", watch_symbols)
    try:
        engine.start(watch_symbols)
        logger.info("ICT 트레이딩 엔진 시작 완료")
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
                        "positions": len(status.get("positions", [])),
                        "daily_entries": status.get("daily_risk", {}).get("entries", 0),
                    },
                )
            except Exception:
                logger.exception("Heartbeat send failed")


if __name__ == "__main__":
    main()
