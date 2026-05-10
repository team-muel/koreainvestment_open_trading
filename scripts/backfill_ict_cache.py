"""Backfill KIS 1-minute bars into the local ICT cache.

Example:
    python scripts/backfill_ict_cache.py --symbols 005930,000660 --days 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "strategy_builder"))

import kis_auth as ka  # noqa: E402
from ict_core import Candle  # noqa: E402
from strategy_builder.core import data_fetcher  # noqa: E402
from strategy_builder.core.ict_cache import MinuteBarCache, build_minute_bar_cache  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill KIS minute bars for ICT live/backtest cache")
    parser.add_argument("--symbols", required=True, help="Comma-separated 6-digit domestic stock codes")
    parser.add_argument("--days", type=int, default=30, help="Calendar days to scan backwards")
    parser.add_argument("--max-pages", type=int, default=8, help="KIS minute pagination depth per date")
    parser.add_argument("--delay", type=float, default=0.35, help="Delay between date requests in seconds")
    parser.add_argument("--mode", choices=["vps", "prod"], default="vps", help="KIS auth mode; default is paper")
    parser.add_argument("--db", default=None, help="Optional SQLite cache path")
    return parser.parse_args()


def _iter_dates(days: int) -> list[str]:
    today = date.today()
    result = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        if day.weekday() < 5:
            result.append(day.strftime("%Y%m%d"))
    return result


def _df_to_candles(symbol: str, df) -> list[Candle]:
    candles: list[Candle] = []
    for row in df.itertuples(index=False):
        candles.append(Candle(
            timestamp=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=int(row.volume or 0),
            symbol=symbol,
            timeframe="1m",
        ))
    return candles


def main() -> int:
    args = parse_args()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("--symbols must include at least one symbol")

    ka.auth(svr=args.mode)
    cache = MinuteBarCache(args.db) if args.db else build_minute_bar_cache()
    dates = _iter_dates(args.days)
    summary: dict[str, dict[str, int]] = {}

    for symbol in symbols:
        stored = 0
        non_empty_days = 0
        for target_date in dates:
            df = data_fetcher.get_intraday_minute_prices(
                symbol,
                target_date=target_date,
                env_dv=args.mode,
                max_pages=args.max_pages,
            )
            if df.empty:
                time.sleep(args.delay)
                continue
            inserted = cache.upsert_bars(symbol, _df_to_candles(symbol, df))
            stored += inserted
            non_empty_days += 1
            print(f"{symbol} {target_date}: {inserted} bars", flush=True)
            time.sleep(args.delay)

        coverage = cache.coverage_summary(symbol)
        summary[symbol] = {
            "stored_bars_this_run": stored,
            "non_empty_days_this_run": non_empty_days,
            "coverage_days": int(coverage["coverage_days"]),
            "ready_coverage_days": int(coverage["ready_coverage_days"]),
        }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
