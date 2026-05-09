"""Notion Sync Worker — Supabase → Notion 주기적 동기화.

5~30초마다 Supabase의 미동기화 row를 Notion에 업로드한다.
매매 루프와 완전히 분리되어 Notion API 지연/실패가 트레이딩에 영향을 주지 않는다.

환경변수:
    SUPABASE_URL
    SUPABASE_KEY
    NOTION_TOKEN
    NOTION_TRADE_JOURNAL_DB_ID   매매일지 DB ID
    NOTION_PREMARKET_DB_ID       장전 스캔 DB ID
    NOTION_DAILY_REPORT_DB_ID    일일 리포트 DB ID
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYNC_INTERVAL = 15
NOTION_VERSION = "2022-06-28"
BATCH_SIZE = 10


class NotionSyncClient:
    """Notion API client."""

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def create_page(self, database_id: str, properties: dict, children: list | None = None) -> dict:
        body: dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            body["children"] = children[:80]
        resp = self.session.post("https://api.notion.com/v1/pages", json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def update_page(self, page_id: str, properties: dict) -> dict:
        resp = self.session.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            json={"properties": properties},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def query_db(self, database_id: str, filters: dict | None = None) -> list[dict]:
        body = filters or {}
        resp = self.session.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])


class SupabasePoller:
    """Supabase polling client."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }

    def get_unsynced_trades(self, limit: int = BATCH_SIZE) -> list[dict]:
        try:
            resp = requests.get(
                f"{self.base}/trade_journal",
                headers=self.headers,
                params={
                    "notion_synced": "eq.false",
                    "order": "created_at.asc",
                    "limit": limit,
                },
                timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            logger.exception("SupabasePoller.get_unsynced_trades failed")
            return []

    def get_unsynced_daily_reports(self, limit: int = 5) -> list[dict]:
        try:
            resp = requests.get(
                f"{self.base}/daily_reports",
                headers=self.headers,
                params={
                    "notion_synced_at": "is.null",
                    "order": "report_date.asc",
                    "limit": limit,
                },
                timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            logger.exception("SupabasePoller.get_unsynced_daily_reports failed")
            return []

    def get_today_premarket(self) -> list[dict]:
        today = datetime.now(KST).date().isoformat()
        try:
            resp = requests.get(
                f"{self.base}/premarket_scan",
                headers=self.headers,
                params={
                    "scan_date": f"eq.{today}",
                    "order": "priority.desc",
                    "limit": "30",
                },
                timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            return []

    def mark_trade_synced(self, trade_id: str) -> None:
        try:
            requests.patch(
                f"{self.base}/trade_journal",
                headers={**self.headers, "Content-Type": "application/json"},
                params={"trade_id": f"eq.{trade_id}"},
                json={"notion_synced": True},
                timeout=10,
            )
        except Exception:
            pass

    def mark_daily_report_synced(self, report_date: str, page_id: str) -> None:
        try:
            requests.patch(
                f"{self.base}/daily_reports",
                headers={**self.headers, "Content-Type": "application/json"},
                params={"report_date": f"eq.{report_date}"},
                json={
                    "notion_synced_at": datetime.now(KST).isoformat(),
                    "notion_page_id": page_id,
                },
                timeout=10,
            )
        except Exception:
            pass


def _trade_to_notion_properties(trade: dict) -> dict:
    event = trade.get("event", "")
    ticker = trade.get("ticker", "")
    side_emoji = "🟢" if trade.get("side") == "buy" else "🔴"
    pnl = trade.get("realized_pnl")
    pnl_str = f" ({pnl:+,.0f}원)" if pnl is not None else ""
    title = f"{side_emoji} {ticker} [{event}]{pnl_str}"

    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": title[:200]}}]},
        "Date": {"date": {"start": (trade.get("created_at") or "")[:10] or datetime.now(KST).date().isoformat()}},
        "Ticker": {"rich_text": [{"text": {"content": ticker}}]},
        "Event": {"select": {"name": event[:100]}},
        "Side": {"select": {"name": trade.get("side", "buy")}},
    }
    if trade.get("entry_price"):
        props["Entry Price"] = {"number": float(trade["entry_price"])}
    if trade.get("stop_price"):
        props["Stop Price"] = {"number": float(trade["stop_price"])}
    if trade.get("target_price"):
        props["Target Price"] = {"number": float(trade["target_price"])}
    if trade.get("size"):
        props["Size"] = {"number": int(trade["size"])}
    if trade.get("risk_amount"):
        props["Risk Amount"] = {"number": float(trade["risk_amount"])}
    if pnl is not None:
        props["Realized PnL"] = {"number": float(pnl)}
    if trade.get("entry_reason"):
        props["Entry Reason"] = {"rich_text": [{"text": {"content": trade["entry_reason"][:2000]}}]}
    if trade.get("exit_reason"):
        props["Exit Reason"] = {"rich_text": [{"text": {"content": trade["exit_reason"][:2000]}}]}
    return props


def _daily_report_to_notion_properties(report: dict) -> dict:
    date = report.get("report_date", datetime.now(KST).date().isoformat())
    pnl = report.get("daily_pnl", 0) or 0
    emoji = "📈" if pnl >= 0 else "📉"
    title = f"{emoji} {date} 일일 결산"
    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Date": {"date": {"start": date}},
    }
    number_fields = {
        "Start Equity": "start_equity",
        "End Equity": "end_equity",
        "Daily PnL": "daily_pnl",
        "Daily Return %": "daily_return_pct",
        "Trades Count": "trades_count",
        "Win Rate": "win_rate",
        "Average R": "average_r",
        "Profit Factor": "profit_factor",
    }
    for notion_key, report_key in number_fields.items():
        if report.get(report_key) is not None:
            props[notion_key] = {"number": float(report[report_key] or 0)}
    text_fields = {
        "Market Condition": "market_condition",
        "What Worked": "what_worked",
        "What Failed": "what_failed",
        "Stopped Reason": "stopped_reason",
    }
    for notion_key, report_key in text_fields.items():
        if report.get(report_key):
            props[notion_key] = {"rich_text": [{"text": {"content": str(report[report_key])[:2000]}}]}
    return props


def _premarket_to_notion_properties(scan: dict) -> dict:
    ticker = scan.get("ticker", "")
    name = scan.get("name", "")
    priority = scan.get("priority", 0) or 0
    emoji = "🔴" if priority >= 8 else "🟡" if priority >= 5 else "🟢"
    title = f"[{priority}] {ticker} {name}"

    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": title[:200]}}]},
        "Date": {"date": {"start": scan.get("scan_date") or datetime.now(KST).date().isoformat()}},
        "Ticker": {"rich_text": [{"text": {"content": ticker}}]},
        "Market": {"select": {"name": scan.get("market", "KOSPI")[:50]}},
        "Priority": {"number": int(priority)},
        "Status": {"select": {"name": scan.get("status", "WATCHLIST")[:50]}},
    }

    if scan.get("prev_change_pct") is not None:
        props["PrevChangePct"] = {"number": float(scan["prev_change_pct"])}
    if scan.get("relative_volume") is not None:
        props["RelativeVolume"] = {"number": float(scan["relative_volume"])}
    if scan.get("liquidity_level"):
        props["LiquidityLevel"] = {"select": {"name": str(scan["liquidity_level"])[:50]}}
    if scan.get("ict_setup"):
        props["ICTSetup"] = {"rich_text": [{"text": {"content": str(scan["ict_setup"])[:2000]}}]}
    if scan.get("scan_reason"):
        props["ScanReason"] = {"rich_text": [{"text": {"content": str(scan["scan_reason"])[:2000]}}]}

    return props


class NotionSyncWorker:
    """Main sync worker."""

    def __init__(self):
        token = os.environ.get("NOTION_TOKEN", "").strip()
        self.trade_db_id = os.environ.get("NOTION_TRADE_JOURNAL_DB_ID", "").strip()
        self.premarket_db_id = os.environ.get("NOTION_PREMARKET_DB_ID", "").strip()
        self.daily_report_db_id = os.environ.get("NOTION_DAILY_REPORT_DB_ID", "").strip()

        if not token:
            logger.warning("NotionSyncWorker: NOTION_TOKEN not set -> disabled")
            self.notion = None
        else:
            self.notion = NotionSyncClient(token)

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        if url and key:
            self.poller = SupabasePoller(url, key)
        else:
            logger.warning("NotionSyncWorker: SUPABASE_URL/KEY not set -> disabled")
            self.poller = None

        self._sync_log: dict[str, set[str]] = {
            "premarket": set(),
        }

    def _sync_trades(self) -> int:
        if not self.notion or not self.poller or not self.trade_db_id:
            return 0
        trades = self.poller.get_unsynced_trades()
        synced = 0
        for trade in trades:
            try:
                props = _trade_to_notion_properties(trade)
                self.notion.create_page(self.trade_db_id, props)
                self.poller.mark_trade_synced(trade.get("trade_id", ""))
                synced += 1
            except Exception:
                logger.exception("Notion trade sync failed for %s", trade.get("trade_id"))
        return synced

    def _sync_daily_reports(self) -> int:
        if not self.notion or not self.poller or not self.daily_report_db_id:
            return 0
        reports = self.poller.get_unsynced_daily_reports()
        synced = 0
        for report in reports:
            try:
                props = _daily_report_to_notion_properties(report)
                result = self.notion.create_page(self.daily_report_db_id, props)
                page_id = result.get("id", "")
                self.poller.mark_daily_report_synced(report.get("report_date", ""), page_id)
                synced += 1
            except Exception:
                logger.exception("Notion daily report sync failed for %s", report.get("report_date"))
        return synced

    def _sync_premarket(self) -> int:
        """Sync today's pre-market scans to Notion."""
        if not self.notion or not self.poller or not self.premarket_db_id:
            return 0

        scans = self.poller.get_today_premarket()
        synced = 0
        for scan in scans:
            try:
                scan_key = f"{scan.get('scan_date', '')}:{scan.get('ticker', '')}"
                if scan_key in self._sync_log["premarket"]:
                    continue

                props = _premarket_to_notion_properties(scan)
                self.notion.create_page(self.premarket_db_id, props)
                self._sync_log["premarket"].add(scan_key)
                synced += 1
            except Exception:
                logger.exception("Notion premarket sync failed for %s", scan.get("ticker"))
        return synced

    def run(self) -> None:
        logger.info("NotionSyncWorker started (interval: %ds)", SYNC_INTERVAL)
        loop = 0
        while True:
            loop += 1
            try:
                trades_synced = self._sync_trades()
                reports_synced = self._sync_daily_reports()
                premarket_synced = self._sync_premarket()
                if trades_synced or reports_synced or premarket_synced:
                    logger.info("Notion sync: %d trades, %d reports, %d premarket", trades_synced, reports_synced, premarket_synced)
            except Exception:
                logger.exception("NotionSyncWorker loop error")
            time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    NotionSyncWorker().run()
