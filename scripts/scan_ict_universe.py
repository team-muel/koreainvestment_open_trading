"""Scan KOSPI/KOSDAQ symbols into an ICT watchlist.

Example:
    python scripts/scan_ict_universe.py --max-scan-symbols 200 --watchlist-limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "strategy_builder"))

import kis_auth as ka  # noqa: E402
from strategy_builder.core.universe_scanner import KRXUniverseScanner, UniverseFilterConfig  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan KOSPI/KOSDAQ universe for ICT watchlist candidates")
    parser.add_argument("--mode", choices=["vps", "prod"], default="vps")
    parser.add_argument("--min-price", type=int, default=2000)
    parser.add_argument("--min-avg-trading-value", type=int, default=3_000_000_000)
    parser.add_argument("--min-last-trading-value", type=int, default=5_000_000_000)
    parser.add_argument("--min-prev-change-pct", type=float, default=0.01)
    parser.add_argument("--max-prev-change-pct", type=float, default=0.08)
    parser.add_argument("--daily-lookback-days", type=int, default=25)
    parser.add_argument("--max-scan-symbols", type=int, default=200)
    parser.add_argument("--watchlist-limit", type=int, default=30)
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--require-ready-cache", action="store_true")
    parser.add_argument("--no-volume-rank", action="store_true", help="Scan in master-file order instead of KIS volume-rank order")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ka.auth(svr=args.mode)
    config = UniverseFilterConfig(
        min_price=args.min_price,
        min_avg_trading_value=args.min_avg_trading_value,
        min_last_trading_value=args.min_last_trading_value,
        min_prev_change_pct=args.min_prev_change_pct,
        max_prev_change_pct=args.max_prev_change_pct,
        daily_lookback_days=args.daily_lookback_days,
        max_scan_symbols=args.max_scan_symbols,
        watchlist_limit=args.watchlist_limit,
        request_delay=args.request_delay,
        require_ready_cache=args.require_ready_cache,
        use_volume_rank=not args.no_volume_rank,
    )
    result = KRXUniverseScanner().scan(config=config, env_dv=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
