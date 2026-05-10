"""Run the ICT replay backtest from the local 1-minute cache.

Example:
    python scripts/run_ict_backtest.py --symbols 005930,000660 --start 2026-01-01 --end 2026-05-05
"""

from __future__ import annotations

import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "strategy_builder"))

from strategy_builder.backend.ict_engine import ICTTradingEngine  # noqa: E402
from strategy_builder.core.ict_cache import MinuteBarCache, build_minute_bar_cache  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ICT replay backtest from local cache")
    parser.add_argument("--symbols", required=True, help="Comma-separated 6-digit domestic symbols")
    parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    parser.add_argument("--db", default=None, help="Optional SQLite cache path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    cache = MinuteBarCache(args.db) if args.db else build_minute_bar_cache()
    engine = ICTTradingEngine(cache=cache)
    result = engine.backtest_from_cache(symbols, args.start, args.end)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
