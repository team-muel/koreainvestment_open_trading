"""Daily ICT workflow helpers.

Premarket:
    python scripts/ict_daily_workflow.py premarket-scan --max-scan-symbols 100 --watchlist-limit 30

Postmarket:
    python scripts/ict_daily_workflow.py postmarket-feedback
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "strategy_builder"))

import kis_auth as ka  # noqa: E402
from strategy_builder.core import data_fetcher  # noqa: E402
from strategy_builder.core.ict_cache import MinuteBarCache  # noqa: E402
from strategy_builder.core.gcal_reporter import publish_gcal_event_if_configured  # noqa: E402
from strategy_builder.core.notion_reporter import publish_daily_report_if_configured  # noqa: E402
from strategy_builder.core.universe_scanner import KRXUniverseScanner, UniverseFilterConfig  # noqa: E402


REPORT_ROOT = ROOT / "strategy_builder" / "data" / "reports" / "ict"
LATEST_WATCHLIST = REPORT_ROOT / "latest_watchlist.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily ICT scan/feedback workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("premarket-scan", help="Build the daily KOSPI/KOSDAQ ICT watchlist")
    scan.add_argument("--mode", choices=["vps", "prod"], default="vps")
    scan.add_argument("--min-price", type=int, default=2000)
    scan.add_argument("--min-avg-trading-value", type=int, default=3_000_000_000)
    scan.add_argument("--min-last-trading-value", type=int, default=5_000_000_000)
    scan.add_argument("--daily-lookback-days", type=int, default=25)
    scan.add_argument("--max-scan-symbols", type=int, default=100)
    scan.add_argument("--watchlist-limit", type=int, default=30)
    scan.add_argument("--request-delay", type=float, default=1.0)
    scan.add_argument("--require-ready-cache", action="store_true")
    scan.add_argument("--no-volume-rank", action="store_true")

    feedback = sub.add_parser("postmarket-feedback", help="Create a daily feedback report from the latest watchlist")
    feedback.add_argument("--mode", choices=["vps", "prod"], default="vps")
    feedback.add_argument("--watchlist", default=str(LATEST_WATCHLIST))
    feedback.add_argument("--limit", type=int, default=30)
    feedback.add_argument("--request-delay", type=float, default=1.0)
    return parser.parse_args()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _today_stamp() -> str:
    return date.today().isoformat()


def run_premarket_scan(args: argparse.Namespace) -> int:
    ka.auth(svr=args.mode)
    scanner = KRXUniverseScanner()
    config = UniverseFilterConfig(
        min_price=args.min_price,
        min_avg_trading_value=args.min_avg_trading_value,
        min_last_trading_value=args.min_last_trading_value,
        daily_lookback_days=args.daily_lookback_days,
        max_scan_symbols=args.max_scan_symbols,
        watchlist_limit=args.watchlist_limit,
        request_delay=args.request_delay,
        require_ready_cache=args.require_ready_cache,
        use_volume_rank=not args.no_volume_rank,
    )
    result = scanner.scan(config=config, env_dv=args.mode)
    result["generated_at"] = datetime.now().isoformat()
    result["workflow"] = "premarket-scan"

    stamp = _today_stamp()
    json_path = REPORT_ROOT / f"{stamp}_premarket_scan.json"
    md_path = REPORT_ROOT / f"{stamp}_premarket_scan.md"
    _write_json(json_path, result)
    _write_json(LATEST_WATCHLIST, result)
    _write_text(md_path, _premarket_markdown(result))
    notion_page = _publish_notion(
        report_date=stamp,
        workflow="premarket-scan",
        status="success",
        title=f"ICT Daily Report - {stamp}",
        markdown_path=md_path,
        json_path=json_path,
        summary={
            "watchlist_count": len(result.get("watchlist", [])),
            "actionable_count": 0,
            "symbols": result.get("symbols_for_ict_start", []),
        },
    )
    if notion_page:
        result["notion"] = notion_page
        _write_json(json_path, result)
        _write_json(LATEST_WATCHLIST, result)

    notion_url = (notion_page or {}).get("url")
    watchlist_count = len(result.get("watchlist", []))
    gcal_event = _publish_gcal(
        workflow="premarket-scan",
        report_date=stamp,
        notion_url=notion_url,
        summary_text=f"오늘 워치리스트: {watchlist_count}종목",
        event_dt=datetime.now(KST).replace(hour=8, minute=30, second=0, microsecond=0),
    )
    if gcal_event:
        result["gcal"] = gcal_event
        _write_json(json_path, result)
        _write_json(LATEST_WATCHLIST, result)

    print(json.dumps({
        "status": "success",
        "json": str(json_path),
        "markdown": str(md_path),
        "notion": notion_page,
        "gcal": gcal_event,
        "watchlist_count": watchlist_count,
        "symbols_for_ict_start": result.get("symbols_for_ict_start", []),
    }, ensure_ascii=False, indent=2))
    return 0


def run_postmarket_feedback(args: argparse.Namespace) -> int:
    watchlist_path = Path(args.watchlist)
    if not watchlist_path.exists():
        raise SystemExit(f"watchlist not found: {watchlist_path}")

    ka.auth(svr=args.mode)
    watchlist_data = json.loads(watchlist_path.read_text(encoding="utf-8"))
    watchlist = watchlist_data.get("watchlist", [])[: args.limit]
    symbols = [item["code"] for item in watchlist if item.get("code")]
    cache = MinuteBarCache()
    scanner = KRXUniverseScanner(cache=cache)
    cached_setups = scanner.scan_cached_ict_setups(symbols, limit=args.limit)

    rows = []
    for item in watchlist:
        code = item.get("code")
        price = data_fetcher.get_current_price(code, env_dv=args.mode) if code else {}
        current_price = float(price.get("price", 0) or 0)
        scan_price = float(item.get("last_close", 0) or 0)
        change_from_scan = ((current_price - scan_price) / scan_price * 100.0) if current_price and scan_price else None
        rows.append({
            "code": code,
            "name": item.get("name", ""),
            "exchange": item.get("exchange", ""),
            "scan_price": scan_price,
            "current_price": current_price,
            "change_from_scan_pct": change_from_scan,
            "ready_coverage_days": cache.ready_coverage_days(code) if code else 0,
        })
        if code:
            import time
            time.sleep(args.request_delay)

    result = {
        "generated_at": datetime.now().isoformat(),
        "workflow": "postmarket-feedback",
        "source_watchlist": str(watchlist_path),
        "watchlist_count": len(watchlist),
        "symbols": symbols,
        "cached_setups": cached_setups,
        "rows": rows,
        "notes": [
            "Use this report to review whether the premarket universe was liquid enough.",
            "Trade execution feedback still requires broker order/fill reconciliation.",
        ],
    }

    stamp = _today_stamp()
    json_path = REPORT_ROOT / f"{stamp}_postmarket_feedback.json"
    md_path = REPORT_ROOT / f"{stamp}_postmarket_feedback.md"
    _write_json(json_path, result)
    _write_text(md_path, _postmarket_markdown(result))
    notion_page = _publish_notion(
        report_date=stamp,
        workflow="postmarket-feedback",
        status="success",
        title=f"ICT Daily Report - {stamp}",
        markdown_path=md_path,
        json_path=json_path,
        summary={
            "watchlist_count": len(watchlist),
            "actionable_count": cached_setups.get("actionable_count", 0),
            "symbols": symbols,
        },
    )
    if notion_page:
        result["notion"] = notion_page
        _write_json(json_path, result)

    notion_url = (notion_page or {}).get("url")
    actionable = cached_setups.get("actionable_count", 0)
    gcal_event = _publish_gcal(
        workflow="postmarket-feedback",
        report_date=stamp,
        notion_url=notion_url,
        summary_text=f"워치리스트 {len(watchlist)}종목 / 액션어블 {actionable}종목",
        event_dt=datetime.now(KST).replace(hour=15, minute=45, second=0, microsecond=0),
    )
    if gcal_event:
        result["gcal"] = gcal_event
        _write_json(json_path, result)

    print(json.dumps({
        "status": "success",
        "json": str(json_path),
        "markdown": str(md_path),
        "notion": notion_page,
        "gcal": gcal_event,
        "watchlist_count": len(watchlist),
        "actionable_count": actionable,
    }, ensure_ascii=False, indent=2))
    return 0


def _premarket_markdown(result: dict) -> str:
    lines = [
        f"# ICT Premarket Scan - {result.get('generated_at', '')}",
        "",
        f"- Universe: {result.get('universe_count', 0)}",
        f"- Eligible after name filters: {result.get('eligible_name_count', 0)}",
        f"- Scanned: {result.get('scanned_count', 0)}",
        f"- Candidates: {result.get('candidate_count', 0)}",
        f"- Watchlist: {len(result.get('watchlist', []))}",
        "",
        "| Rank | Code | Name | Exchange | Last Close | Avg Trading Value | Ready Days |",
        "|---:|---|---|---|---:|---:|---:|",
    ]
    for idx, item in enumerate(result.get("watchlist", []), start=1):
        lines.append(
            f"| {idx} | {item.get('code')} | {item.get('name')} | {item.get('exchange')} | "
            f"{item.get('last_close', 0):,.0f} | {item.get('avg_trading_value', 0):,.0f} | "
            f"{item.get('ready_coverage_days', 0)} |"
        )
    return "\n".join(lines) + "\n"


def _postmarket_markdown(result: dict) -> str:
    lines = [
        f"# ICT Postmarket Feedback - {result.get('generated_at', '')}",
        "",
        f"- Source watchlist: {result.get('source_watchlist', '')}",
        f"- Watchlist count: {result.get('watchlist_count', 0)}",
        f"- Cached setup count: {result.get('cached_setups', {}).get('setup_count', 0)}",
        f"- Actionable setup count: {result.get('cached_setups', {}).get('actionable_count', 0)}",
        "",
        "| Code | Name | Scan Price | Current Price | Change From Scan | Ready Days |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for item in result.get("rows", []):
        change = item.get("change_from_scan_pct")
        change_text = "" if change is None else f"{change:.2f}%"
        lines.append(
            f"| {item.get('code')} | {item.get('name')} | {item.get('scan_price', 0):,.0f} | "
            f"{item.get('current_price', 0):,.0f} | {change_text} | {item.get('ready_coverage_days', 0)} |"
        )
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in result.get("notes", []))
    return "\n".join(lines) + "\n"


def _publish_notion(
    *,
    report_date: str,
    workflow: str,
    status: str,
    title: str,
    markdown_path: Path,
    json_path: Path,
    summary: dict,
) -> dict | None:
    try:
        return publish_daily_report_if_configured(
            report_date=report_date,
            workflow=workflow,
            status=status,
            title=title,
            markdown_path=str(markdown_path),
            json_path=str(json_path),
            summary=summary,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _publish_gcal(
    *,
    workflow: str,
    report_date: str,
    notion_url: str | None,
    summary_text: str,
    event_dt: datetime,
) -> dict | None:
    try:
        return publish_gcal_event_if_configured(
            workflow=workflow,
            report_date=report_date,
            notion_url=notion_url,
            summary_text=summary_text,
            event_dt=event_dt,
        )
    except Exception as exc:
        return {"error": str(exc)}


def main() -> int:
    args = parse_args()
    if args.command == "premarket-scan":
        return run_premarket_scan(args)
    if args.command == "postmarket-feedback":
        return run_postmarket_feedback(args)
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
