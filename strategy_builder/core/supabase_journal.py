"""Supabase journal - ICTJournal의 Supabase(PostgreSQL) 버전.

환경변수:
    SUPABASE_URL      예: https://xxxx.supabase.co
    SUPABASE_KEY      service_role key (anon key는 RLS 제한 있을 수 있음)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class SupabaseClient:
    """경량 Supabase REST client. supabase-py 없이도 동작."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    def upsert(self, table: str, data: dict | list[dict], on_conflict: str | None = None) -> None:
        headers = {**self.headers, "Prefer": f"resolution=merge-duplicates,return=minimal"}
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        resp = requests.post(
            f"{self.base}/{table}",
            headers=headers,
            json=data if isinstance(data, list) else [data],
            params=params,
            timeout=10,
        )
        if not resp.ok:
            logger.error("Supabase upsert %s failed: %s %s", table, resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def insert(self, table: str, data: dict | list[dict]) -> None:
        resp = requests.post(
            f"{self.base}/{table}",
            headers=self.headers,
            json=data if isinstance(data, list) else [data],
            timeout=10,
        )
        if not resp.ok:
            logger.error("Supabase insert %s failed: %s %s", table, resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def select(self, table: str, *, filters: dict | None = None, limit: int = 100, order: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        headers = {**self.headers, "Prefer": "return=representation"}
        resp = requests.get(f"{self.base}/{table}", headers=headers, params=params, timeout=10)
        if not resp.ok:
            logger.error("Supabase select %s failed: %s", table, resp.status_code)
            return []
        return resp.json()

    def delete(self, table: str, filters: dict) -> int:
        params = {**filters}
        resp = requests.delete(f"{self.base}/{table}", headers=self.headers, params=params, timeout=10)
        if not resp.ok:
            logger.warning("Supabase delete %s failed: %s", table, resp.status_code)
            return 0
        return 1

    def rpc(self, func_name: str, params: dict) -> Any:
        resp = requests.post(
            f"{self.base.replace('/rest/v1', '')}/rest/v1/rpc/{func_name}",
            headers=self.headers,
            json=params,
            timeout=10,
        )
        if not resp.ok:
            logger.error("Supabase rpc %s failed: %s", func_name, resp.status_code)
            return None
        return resp.json()


@dataclass
class SupabaseJournal:
    """ICTJournal과 동일한 인터페이스를 제공하는 Supabase 기반 저널.

    환경변수 SUPABASE_URL, SUPABASE_KEY가 없으면 자동으로 비활성화(no-op).
    """
    _client: SupabaseClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        if url and key:
            self._client = SupabaseClient(url, key)
            logger.info("SupabaseJournal initialized: %s", url)
        else:
            logger.warning("SupabaseJournal: SUPABASE_URL/KEY not set → running in no-op mode")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ──────────────────────────────────────────────
    # ICTJournal 호환 public methods
    # ──────────────────────────────────────────────

    def record_premarket_candidate(self, item: dict[str, Any], scan_date: str) -> None:
        if not self._client:
            return
        try:
            self._client.upsert("premarket_scan", {
                "scan_date": scan_date,
                "ticker": item.get("code"),
                "name": item.get("name"),
                "market": item.get("exchange"),
                "prev_change_pct": item.get("prev_change_pct"),
                "relative_volume": item.get("relative_volume"),
                "volume_rank": item.get("volume_rank"),
                "liquidity_level": item.get("liquidity_level"),
                "ict_setup": item.get("ict_setup"),
                "scan_reason": item.get("scan_reason"),
                "priority": item.get("priority"),
                "invalidation_level": item.get("invalidation_level"),
                "plan": item.get("plan"),
                "status": item.get("status", "WATCHLIST"),
                "payload": item,
            }, on_conflict="scan_date,ticker")
        except Exception:
            logger.exception("SupabaseJournal.record_premarket_candidate failed")

    def record_signal(self, setup: dict[str, Any], action_taken: bool, reason_not_taken: str = "") -> None:
        if not self._client:
            return
        try:
            trade_plan = setup.get("trade_plan") or {}
            details = setup.get("details") or {}
            trigger = details.get("trigger", {})
            execution = details.get("execution", {})
            ticker = setup.get("symbol")
            signal_type = "LONG_RECLAIM"
            now = datetime.now(KST).isoformat()
            signal_id = f"{ticker}-{now}"

            # 중복 방지: 최근 5분 내 동일 ticker 미실행 신호 3건 이상 스킵
            if not action_taken:
                from datetime import timedelta
                five_min_ago = (datetime.now(KST) - timedelta(minutes=5)).isoformat()
                recent = self._client.select(
                    "signals",
                    filters={
                        "ticker": f"eq.{ticker}",
                        "signal_type": f"eq.{signal_type}",
                        "action_taken": "eq.false",
                        "created_at": f"gte.{five_min_ago}",
                    },
                    limit=5,
                )
                if len(recent) >= 3:
                    return

            self._client.insert("signals", {
                "signal_id": signal_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "liquidity_level": trigger.get("liquidity_level"),
                "sweep_confirmed": bool(trigger.get("sweep_confirmed")),
                "vwap_reclaim": bool(trigger.get("vwap_reclaim_confirmed")),
                "mss_confirmed": bool(trigger.get("mss_confirmed")),
                "volume_confirmed": bool((trigger.get("volume_ratio") or 0) >= 1.5),
                "entry_candidate": trade_plan.get("entry") or execution.get("entry_candidate"),
                "stop_candidate": trade_plan.get("stop") or execution.get("stop_candidate"),
                "target_candidate": trade_plan.get("take_profit") or execution.get("final_take_profit"),
                "signal_quality_score": setup.get("liquidity_score"),
                "action_taken": bool(action_taken),
                "reason_not_taken": reason_not_taken,
                "payload": setup,
                "created_at": now,
            })
        except Exception:
            logger.exception("SupabaseJournal.record_signal failed")

    def record_trade_event(self, event: str, order: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        if not self._client:
            return
        try:
            risk_per_share = float(order.get("entry", 0) or 0) - float(order.get("stop", 0) or 0)
            risk_amount = risk_per_share * int(order.get("quantity", 0) or 0)
            trade_id = order.get("order_no") or f"{order.get('symbol')}-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}"
            self._client.insert("trade_journal", {
                "trade_id": trade_id,
                "event": event,
                "ticker": order.get("symbol"),
                "side": order.get("side"),
                "entry_time": order.get("submitted_at"),
                "entry_price": order.get("entry"),
                "stop_price": order.get("stop"),
                "target_price": order.get("take_profit"),
                "size": order.get("quantity"),
                "risk_amount": risk_amount,
                "risk_per_share": risk_per_share,
                "setup": "KIS ICT Intraday Liquidity Reclaim",
                "entry_reason": (payload or {}).get("entry_reason", ""),
                "exit_reason": (payload or {}).get("exit_reason", ""),
                "payload": {"order": order, "payload": payload or {}},
                "created_at": datetime.now(KST).isoformat(),
            })
        except Exception:
            logger.exception("SupabaseJournal.record_trade_event failed")

    def record_daily_report(self, report: dict[str, Any]) -> None:
        if not self._client:
            return
        try:
            summary = report.get("summary", {})
            self._client.upsert("daily_reports", {
                "report_date": report.get("date") or datetime.now(KST).date().isoformat(),
                "start_equity": summary.get("start_equity"),
                "end_equity": summary.get("end_equity"),
                "daily_pnl": summary.get("daily_pnl"),
                "daily_return_pct": summary.get("daily_return_pct"),
                "max_intraday_drawdown_pct": summary.get("max_intraday_drawdown_pct"),
                "trades_count": summary.get("trades_count"),
                "win_rate": summary.get("win_rate"),
                "average_r": summary.get("average_r"),
                "profit_factor": summary.get("profit_factor"),
                "rule_violation": summary.get("rule_violation"),
                "goal_hit": summary.get("goal_hit"),
                "stopped_reason": summary.get("stopped_reason"),
                "market_condition": summary.get("market_condition"),
                "what_worked": summary.get("what_worked"),
                "what_failed": summary.get("what_failed"),
                "next_rule_change": summary.get("next_rule_change"),
                "payload": report,
                "updated_at": datetime.now(KST).isoformat(),
            }, on_conflict="report_date")
        except Exception:
            logger.exception("SupabaseJournal.record_daily_report failed")

    # ──────────────────────────────────────────────
    # P0-4 호환: 엔진 상태 영속화
    # ──────────────────────────────────────────────

    def save_engine_state(self, state: dict[str, Any]) -> None:
        if not self._client:
            return
        try:
            self._client.upsert("engine_heartbeats", {
                "worker": "trading-worker",
                "status": "running" if state.get("running") else "stopped",
                "loop_count": state.get("loop_count", 0),
                "last_error": state.get("last_error"),
                "degraded": state.get("degraded", False),
                "metadata": state,
                "beat_at": datetime.now(KST).isoformat(),
            })
        except Exception:
            logger.exception("SupabaseJournal.save_engine_state failed")

    def save_order(self, order: dict[str, Any]) -> None:
        if not self._client:
            return
        try:
            self._client.upsert("orders", {
                "order_no": order.get("order_no"),
                "symbol": order.get("symbol"),
                "side": order.get("side", "buy"),
                "status": order.get("status", "LIMIT_SUBMITTED"),
                "quantity": order.get("quantity", 0),
                "filled_qty": order.get("filled_quantity", 0),
                "price": order.get("price"),
                "entry": order.get("entry"),
                "stop": order.get("stop"),
                "take_profit": order.get("take_profit"),
                "tp_order_no": order.get("tp_order_no"),
                "tp2_order_no": order.get("tp2_order_no"),
                "exit_order_no": order.get("exit_order_no"),
                "submitted_at": order.get("submitted_at"),
                "updated_at": datetime.now(KST).isoformat(),
                "payload": order,
            }, on_conflict="order_no")
        except Exception:
            logger.exception("SupabaseJournal.save_order failed")

    def save_fill(self, fill: dict[str, Any]) -> None:
        if not self._client:
            return
        try:
            self._client.insert("fills", {
                "order_no": fill.get("order_no"),
                "symbol": fill.get("symbol"),
                "side": fill.get("side", "buy"),
                "filled_qty": fill.get("filled_qty", 0),
                "avg_price": fill.get("avg_price"),
                "fees": fill.get("fees", 0),
                "realized_pnl": fill.get("realized_pnl"),
                "is_complete": fill.get("is_complete", False),
                "fill_time": fill.get("fill_time") or datetime.now(KST).isoformat(),
                "payload": fill,
            })
        except Exception:
            logger.exception("SupabaseJournal.save_fill failed")

    def save_position(self, symbol: str, order: dict[str, Any] | None) -> None:
        if not self._client:
            return
        try:
            if order is None:
                self._client.delete("positions", {"symbol": f"eq.{symbol}"})
            else:
                self._client.upsert("positions", {
                    "symbol": symbol,
                    "order_no": order.get("order_no"),
                    "quantity": order.get("quantity", 0),
                    "entry": order.get("entry"),
                    "stop": order.get("stop"),
                    "take_profit": order.get("take_profit"),
                    "opened_at": order.get("submitted_at"),
                    "updated_at": datetime.now(KST).isoformat(),
                    "payload": order,
                }, on_conflict="symbol")
        except Exception:
            logger.exception("SupabaseJournal.save_position failed")

    def save_daily_risk(self, date: str, entries: int, loss: float, realized: float, total_eval: int) -> None:
        """daily_risk는 portfolio_snapshots로 기록."""
        if not self._client:
            return
        try:
            self._client.insert("portfolio_snapshots", {
                "snapshot_at": datetime.now(KST).isoformat(),
                "total_eval": total_eval,
                "daily_realized": realized,
                "daily_loss": loss,
                "daily_entries": entries,
                "payload": {"date": date},
            })
        except Exception:
            logger.exception("SupabaseJournal.save_daily_risk failed")

    def load_engine_state(self) -> dict[str, Any] | None:
        if not self._client:
            return None
        try:
            rows = self._client.select(
                "engine_heartbeats",
                filters={"worker": "eq.trading-worker"},
                limit=1,
                order="beat_at.desc",
            )
            return rows[0].get("metadata") if rows else None
        except Exception:
            logger.exception("SupabaseJournal.load_engine_state failed")
            return None

    def load_orders(self, status_filter: list[str] | None = None) -> list[dict[str, Any]]:
        if not self._client:
            return []
        try:
            filters: dict[str, Any] = {}
            if status_filter:
                filters["status"] = f"in.({','.join(status_filter)})"
            rows = self._client.select("orders", filters=filters, limit=50, order="submitted_at.desc")
            return [r.get("payload") or r for r in rows]
        except Exception:
            logger.exception("SupabaseJournal.load_orders failed")
            return []

    def load_positions(self) -> list[dict[str, Any]]:
        if not self._client:
            return []
        try:
            rows = self._client.select("positions", limit=20)
            return [r.get("payload") or r for r in rows]
        except Exception:
            logger.exception("SupabaseJournal.load_positions failed")
            return []

    def load_daily_risk(self, date: str) -> dict[str, Any] | None:
        if not self._client:
            return None
        try:
            rows = self._client.select(
                "portfolio_snapshots",
                filters={"payload->>date": f"eq.{date}"},
                limit=1,
                order="snapshot_at.desc",
            )
            if not rows:
                return None
            row = rows[0]
            return {
                "entries": row.get("daily_entries", 0),
                "loss": float(row.get("daily_loss", 0) or 0),
                "realized": float(row.get("daily_realized", 0) or 0),
                "total_eval": int(row.get("total_eval", 0) or 0),
            }
        except Exception:
            logger.exception("SupabaseJournal.load_daily_risk failed")
            return None

    def get_runtime_config(self, key: str, default: str = "") -> str:
        """runtime_config 테이블에서 설정값 조회."""
        if not self._client:
            return default
        try:
            rows = self._client.select("runtime_config", filters={"key": f"eq.{key}"}, limit=1)
            return rows[0]["value"] if rows else default
        except Exception:
            return default

    def set_runtime_config(self, key: str, value: str) -> None:
        """runtime_config 테이블에 설정값 저장."""
        if not self._client:
            return
        try:
            self._client.upsert("runtime_config", {
                "key": key,
                "value": value,
                "updated_at": datetime.now(KST).isoformat(),
            }, on_conflict="key")
        except Exception:
            logger.exception("SupabaseJournal.set_runtime_config failed")

    def send_heartbeat(self, worker: str, loop_count: int = 0, metadata: dict | None = None) -> None:
        """워커 생존 신호 전송."""
        if not self._client:
            return
        try:
            self._client.insert("engine_heartbeats", {
                "worker": worker,
                "status": "running",
                "loop_count": loop_count,
                "metadata": metadata or {},
                "beat_at": datetime.now(KST).isoformat(),
            })
        except Exception:
            pass  # heartbeat 실패는 무시

    def record_strategy_change(
        self,
        changed_rule: str,
        before_value: str,
        after_value: str,
        reason: str,
        expected_effect: str = "",
        review_date: str = "",
    ) -> None:
        """전략 변경 로그 기록."""
        if not self._client:
            return
        try:
            self._client.insert("strategy_change_log", {
                "created_at": datetime.now(KST).isoformat(),
                "changed_rule": changed_rule,
                "before_value": before_value,
                "after_value": after_value,
                "reason": reason,
                "expected_effect": expected_effect,
                "review_date": review_date,
            })
        except Exception:
            logger.exception("SupabaseJournal.record_strategy_change failed")

    # ── job_runs: 워크플로 멱등성 보장 ──────────────────────────

    def start_job(self, job_name: str, trade_date: str) -> bool:
        """워크플로 시작 기록. 이미 SUCCESS면 False 반환 (중복 실행 방지)."""
        if not self._client:
            return True  # no-op 모드에서는 항상 실행 허용
        try:
            # 이미 SUCCESS인지 확인
            rows = self._client.select(
                "job_runs",
                filters={"job_name": f"eq.{job_name}", "trade_date": f"eq.{trade_date}", "status": "eq.SUCCESS"},
                limit=1,
            )
            if rows:
                logger.info("job_runs: %s %s already SUCCESS — skip", job_name, trade_date)
                return False
            # RUNNING으로 upsert
            self._client.upsert("job_runs", {
                "job_name": job_name,
                "trade_date": trade_date,
                "status": "RUNNING",
                "started_at": datetime.now(KST).isoformat(),
                "finished_at": None,
                "error_message": None,
            }, on_conflict="job_name,trade_date")
            return True
        except Exception:
            logger.exception("start_job failed")
            return True  # 실패 시 실행 허용 (안전한 방향)

    def finish_job(self, job_name: str, trade_date: str, success: bool, error: str = "", payload: dict | None = None) -> None:
        """워크플로 완료 기록."""
        if not self._client:
            return
        try:
            self._client.upsert("job_runs", {
                "job_name": job_name,
                "trade_date": trade_date,
                "status": "SUCCESS" if success else "FAILED",
                "finished_at": datetime.now(KST).isoformat(),
                "error_message": error[:500] if error else None,
                "payload": payload or {},
            }, on_conflict="job_name,trade_date")
        except Exception:
            logger.exception("finish_job failed")

    def get_job_status(self, job_name: str, trade_date: str) -> str:
        """워크플로 상태 조회. 없으면 'NOT_RUN' 반환."""
        if not self._client:
            return "NOT_RUN"
        try:
            rows = self._client.select(
                "job_runs",
                filters={"job_name": f"eq.{job_name}", "trade_date": f"eq.{trade_date}"},
                limit=1,
            )
            return rows[0]["status"] if rows else "NOT_RUN"
        except Exception:
            return "NOT_RUN"

    # ── watchlists: 장전 스캔 종목 영속화 ──────────────────────

    def save_watchlist(self, trade_date: str, symbols: list[dict]) -> None:
        """장전 스캔 watchlist를 Supabase에 저장 (VM 재시작 시에도 유지)."""
        if not self._client or not symbols:
            return
        try:
            for item in symbols:
                self._client.upsert("watchlists", {
                    "trade_date": trade_date,
                    "symbol": item.get("code") or item.get("symbol", ""),
                    "name": item.get("name", ""),
                    "priority": item.get("priority"),
                    "scan_reason": item.get("scan_reason", ""),
                    "status": "ACTIVE",
                    "payload": item,
                }, on_conflict="trade_date,symbol")
            logger.info("save_watchlist: %d종목 저장 (%s)", len(symbols), trade_date)
        except Exception:
            logger.exception("save_watchlist failed")

    def load_watchlist(self, trade_date: str) -> list[str]:
        """오늘 watchlist 종목코드 목록 반환."""
        if not self._client:
            return []
        try:
            rows = self._client.select(
                "watchlists",
                filters={"trade_date": f"eq.{trade_date}", "status": "eq.ACTIVE"},
                limit=30,
                order="priority.asc",
            )
            return [r["symbol"] for r in rows if r.get("symbol")]
        except Exception:
            logger.exception("load_watchlist failed")
            return []

    # ICTJournal 호환용 no-op stub
    def cleanup_signal_log(self, max_rows: int = 10000, keep_days: int = 30) -> int:
        return 0
