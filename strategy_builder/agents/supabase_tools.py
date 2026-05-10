"""Domain-level Supabase tools for read/report agents."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from strategy_builder.core.supabase_journal import SupabaseClient

KST = ZoneInfo("Asia/Seoul")


class SupabaseDomainTools:
    """Narrow Supabase tool set for agents.

    This layer intentionally exposes read/report operations only. It does not
    contain order submission, cancel, force-exit, or runtime switch mutation.
    """

    def __init__(self, client: SupabaseClient | None = None):
        self.client = client or self._client_from_env()

    def get_daily_report_context(self, trade_date: str) -> dict[str, Any]:
        date_start = f"{trade_date}T00:00:00+09:00"
        context = {
            "trade_date": trade_date,
            "daily_report": self._first("daily_reports", {"report_date": f"eq.{trade_date}", "limit": "1"}),
            "signals": self._select("signals", {
                "created_at": f"gte.{date_start}",
                "order": "created_at.asc",
                "limit": "50",
            }),
            "orders": self._select("orders", {
                "submitted_at": f"gte.{date_start}",
                "order": "submitted_at.asc",
                "limit": "20",
            }),
            "fills": self._select("fills", {
                "fill_time": f"gte.{date_start}",
                "is_complete": "eq.true",
                "order": "fill_time.asc",
                "limit": "20",
            }),
            "positions": self._select("positions", {"limit": "20"}),
            "premarket_scan": self._select("premarket_scan", {
                "scan_date": f"eq.{trade_date}",
                "order": "priority.desc",
                "limit": "15",
            }),
        }
        context["blocked_entries"] = [s for s in context["signals"] if s.get("reason_not_taken")]
        return context

    def get_strategy_review_context(self, weeks: int = 2) -> dict[str, Any]:
        start_date = (datetime.now(KST) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        return {
            "period_weeks": weeks,
            "start_date": start_date,
            "end_date": datetime.now(KST).strftime("%Y-%m-%d"),
            "daily_reports": self._select("daily_reports", {
                "report_date": f"gte.{start_date}",
                "order": "report_date.asc",
                "limit": "30",
            }),
            "signals": self._select("signals", {
                "created_at": f"gte.{start_date}T00:00:00+09:00",
                "order": "created_at.asc",
                "limit": "200",
            }),
            "fills": self._select("fills", {
                "fill_time": f"gte.{start_date}T00:00:00+09:00",
                "is_complete": "eq.true",
                "order": "fill_time.asc",
                "limit": "100",
            }),
            "risk_reviews": self._select("agent_runs", {
                "agent_name": "eq.risk_review_agent",
                "trade_date": f"gte.{start_date}",
                "order": "trade_date.asc",
                "limit": "50",
            }),
        }

    def get_audit_context(self, trade_date: str) -> dict[str, Any]:
        date_start = f"{trade_date}T00:00:00+09:00"
        return {
            "trade_date": trade_date,
            "signals": self._select("signals", {
                "created_at": f"gte.{date_start}",
                "order": "created_at.asc",
                "limit": "100",
            }),
            "orders": self._select("orders", {
                "submitted_at": f"gte.{date_start}",
                "order": "submitted_at.asc",
                "limit": "50",
            }),
            "fills": self._select("fills", {
                "fill_time": f"gte.{date_start}",
                "order": "fill_time.asc",
                "limit": "50",
            }),
            "agent_runs": self._select("agent_runs", {
                "trade_date": f"eq.{trade_date}",
                "order": "created_at.asc",
                "limit": "20",
            }),
            "heartbeats": self._select("engine_heartbeats", {
                "beat_at": f"gte.{date_start}",
                "order": "beat_at.asc",
                "limit": "50",
            }),
            "journal": self._select("trade_journal", {
                "created_at": f"gte.{date_start}",
                "order": "created_at.asc",
                "limit": "50",
            }),
            "daily_report": self._first("daily_reports", {"report_date": f"eq.{trade_date}", "limit": "1"}),
        }

    def save_agent_run(
        self,
        agent_name: str,
        trade_date: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        status: str = "SUCCESS",
        llm_model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> dict[str, Any]:
        self.client.upsert("agent_runs", {
            "agent_name": agent_name,
            "trade_date": trade_date,
            "input_payload": input_payload,
            "output_payload": output_payload,
            "status": status,
            "llm_model": llm_model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "updated_at": datetime.now(KST).isoformat(),
        }, on_conflict="agent_name,trade_date")
        return {"success": True, "agent_name": agent_name, "trade_date": trade_date}

    def _select(self, table: str, filters: dict[str, Any]) -> list[dict]:
        return self.client.select(table, filters=filters, limit=int(filters.get("limit", 100)))

    def _first(self, table: str, filters: dict[str, Any]) -> dict:
        rows = self._select(table, filters)
        return rows[0] if rows else {}

    @staticmethod
    def _client_from_env() -> SupabaseClient:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = (
            os.environ.get("SUPABASE_KEY", "").strip()
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        )
        if not url or not key:
            return NoopSupabaseClient()
        return SupabaseClient(url, key)


class NoopSupabaseClient:
    def select(self, table: str, *, filters: dict | None = None, limit: int = 100, order: str | None = None) -> list[dict]:
        return []

    def upsert(self, table: str, data: dict | list[dict], on_conflict: str | None = None) -> None:
        return None

    def insert(self, table: str, data: dict | list[dict]) -> None:
        return None
