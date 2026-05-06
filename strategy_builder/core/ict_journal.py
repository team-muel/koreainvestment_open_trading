"""SQLite journal for ICT automation decisions and broker workflow logs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "ict_journal.sqlite3"


@dataclass
class ICTJournal:
    path: Path = DEFAULT_DB

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def record_premarket_candidate(self, item: dict[str, Any], scan_date: str) -> None:
        self._execute(
            """
            insert or replace into premarket_scan (
                scan_date, ticker, name, market, prev_change_pct,
                prev_high_distance_pct, prev_low_distance_pct, relative_volume,
                volume_rank, liquidity_level, ict_setup, scan_reason,
                priority, invalidation_level, plan, status, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_date,
                item.get("code"),
                item.get("name"),
                item.get("exchange"),
                item.get("prev_change_pct"),
                item.get("prev_high_distance_pct"),
                item.get("prev_low_distance_pct"),
                item.get("relative_volume"),
                item.get("volume_rank"),
                item.get("liquidity_level"),
                item.get("ict_setup"),
                item.get("scan_reason"),
                item.get("priority"),
                item.get("invalidation_level"),
                item.get("plan"),
                item.get("status", "WATCHLIST"),
                self._json(item),
            ),
        )

    def record_signal(self, setup: dict[str, Any], action_taken: bool, reason_not_taken: str = "") -> None:
        trade_plan = setup.get("trade_plan") or {}
        details = setup.get("details") or {}
        trigger = details.get("trigger", {})
        execution = details.get("execution", {})
        signal_id = f"{setup.get('symbol')}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self._execute(
            """
            insert into signal_log (
                signal_id, created_at, ticker, signal_type, liquidity_level,
                sweep_confirmed, vwap_reclaim, mss_confirmed, volume_confirmed,
                entry_candidate, stop_candidate, target_candidate,
                signal_quality_score, action_taken, reason_not_taken, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                datetime.now().isoformat(),
                setup.get("symbol"),
                "LONG_RECLAIM",
                trigger.get("liquidity_level"),
                bool(trigger.get("sweep_confirmed")),
                bool(trigger.get("vwap_reclaim_confirmed")),
                bool(trigger.get("mss_confirmed")),
                bool((trigger.get("volume_ratio") or 0) >= 1.5),
                trade_plan.get("entry") or execution.get("entry_candidate"),
                trade_plan.get("stop") or execution.get("stop_candidate"),
                trade_plan.get("take_profit") or execution.get("final_take_profit"),
                setup.get("liquidity_score"),
                bool(action_taken),
                reason_not_taken,
                self._json(setup),
            ),
        )

    def record_trade_event(self, event: str, order: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        risk_per_share = float(order.get("entry", 0) or 0) - float(order.get("stop", 0) or 0)
        risk_amount = risk_per_share * int(order.get("quantity", 0) or 0)
        self._execute(
            """
            insert into trade_journal (
                trade_id, event, created_at, ticker, side, entry_time,
                entry_price, stop_price, target_price, size, risk_amount,
                risk_per_share, r_multiple, setup, entry_reason, exit_reason,
                payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.get("order_no") or f"{order.get('symbol')}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                event,
                datetime.now().isoformat(),
                order.get("symbol"),
                order.get("side"),
                order.get("submitted_at"),
                order.get("entry"),
                order.get("stop"),
                order.get("take_profit"),
                order.get("quantity"),
                risk_amount,
                risk_per_share,
                None,
                "KIS ICT Intraday Liquidity Reclaim",
                payload.get("entry_reason", "") if payload else "",
                payload.get("exit_reason", "") if payload else "",
                self._json({"order": order, "payload": payload or {}}),
            ),
        )

    def record_daily_report(self, report: dict[str, Any]) -> None:
        summary = report.get("summary", {})
        self._execute(
            """
            insert or replace into daily_report (
                report_date, start_equity, end_equity, daily_pnl, daily_return_pct,
                max_intraday_drawdown_pct, trades_count, win_rate, average_r,
                profit_factor, rule_violation, goal_hit, stopped_reason,
                market_condition, what_worked, what_failed, next_rule_change,
                payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.get("date") or datetime.now().date().isoformat(),
                summary.get("start_equity"),
                summary.get("end_equity"),
                summary.get("daily_pnl"),
                summary.get("daily_return_pct"),
                summary.get("max_intraday_drawdown_pct"),
                summary.get("trades_count"),
                summary.get("win_rate"),
                summary.get("average_r"),
                summary.get("profit_factor"),
                summary.get("rule_violation"),
                summary.get("goal_hit"),
                summary.get("stopped_reason"),
                summary.get("market_condition"),
                summary.get("what_worked"),
                summary.get("what_failed"),
                summary.get("next_rule_change"),
                self._json(report),
            ),
        )

    def _init_schema(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                create table if not exists premarket_scan (
                    scan_date text not null,
                    ticker text not null,
                    name text,
                    market text,
                    prev_change_pct real,
                    prev_high_distance_pct real,
                    prev_low_distance_pct real,
                    relative_volume real,
                    volume_rank integer,
                    liquidity_level text,
                    ict_setup text,
                    scan_reason text,
                    priority integer,
                    invalidation_level real,
                    plan text,
                    status text,
                    payload_json text,
                    primary key (scan_date, ticker)
                );
                create table if not exists signal_log (
                    id integer primary key autoincrement,
                    signal_id text,
                    created_at text,
                    ticker text,
                    signal_type text,
                    liquidity_level real,
                    sweep_confirmed integer,
                    vwap_reclaim integer,
                    mss_confirmed integer,
                    volume_confirmed integer,
                    entry_candidate real,
                    stop_candidate real,
                    target_candidate real,
                    signal_quality_score real,
                    action_taken integer,
                    reason_not_taken text,
                    payload_json text
                );
                create table if not exists trade_journal (
                    id integer primary key autoincrement,
                    trade_id text,
                    event text,
                    created_at text,
                    ticker text,
                    side text,
                    entry_time text,
                    entry_price real,
                    stop_price real,
                    target_price real,
                    size integer,
                    risk_amount real,
                    risk_per_share real,
                    r_multiple real,
                    setup text,
                    entry_reason text,
                    exit_reason text,
                    payload_json text
                );
                create table if not exists daily_report (
                    report_date text primary key,
                    start_equity real,
                    end_equity real,
                    daily_pnl real,
                    daily_return_pct real,
                    max_intraday_drawdown_pct real,
                    trades_count integer,
                    win_rate real,
                    average_r real,
                    profit_factor real,
                    rule_violation text,
                    goal_hit text,
                    stopped_reason text,
                    market_condition text,
                    what_worked text,
                    what_failed text,
                    next_rule_change text,
                    payload_json text
                );
                create table if not exists strategy_change_log (
                    id integer primary key autoincrement,
                    created_at text,
                    changed_rule text,
                    before_value text,
                    after_value text,
                    reason text,
                    expected_effect text,
                    review_date text,
                    result text,
                    decision text
                );
                """
            )

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(sql, params)
            conn.commit()

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)
