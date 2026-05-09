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
        signal_id = f"{setup.get('symbol')}-{datetime.now().isoformat()}"
        ticker = setup.get("symbol")
        signal_type = "LONG_RECLAIM"

        # 중복 저장 방지: 최근 5분 내 동일 ticker + signal_type의 미실행 신호가 있으면 스킵
        if not action_taken:
            with sqlite3.connect(self.path) as conn:
                recent = conn.execute(
                    """
                    SELECT COUNT(*) FROM signal_log
                    WHERE ticker = ? AND signal_type = ? AND action_taken = 0
                      AND created_at > datetime('now', '-5 minutes')
                    """,
                    (ticker, signal_type),
                ).fetchone()[0]
                if recent >= 3:
                    return  # 5분 내 같은 종목 미실행 신호 3건 이상 → 스킵

        # 기존 insert 로직
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
                signal_type,
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

    def save_engine_state(self, state: dict[str, Any]) -> None:
        """Upsert engine state (id=1 singleton)."""
        self._execute(
            """
            insert or replace into engine_state (
                id, updated_at, running, degraded, daily_entries, daily_loss,
                daily_realized, last_total_eval, last_error, symbols_json,
                warmup_dates_json
            ) values (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.get("updated_at", datetime.now().isoformat()),
                int(state.get("running", False)),
                int(state.get("degraded", False)),
                int(state.get("daily_entries", 0)),
                float(state.get("daily_loss", 0.0)),
                float(state.get("daily_realized", 0.0)),
                int(state.get("last_total_eval", 0)),
                state.get("last_error", ""),
                state.get("symbols_json", "[]"),
                state.get("warmup_dates_json", "{}"),
            ),
        )

    def save_order(self, order: dict[str, Any]) -> None:
        """Upsert order into orders table."""
        self._execute(
            """
            insert or replace into orders (
                order_no, symbol, side, status, quantity, filled_qty,
                price, entry, stop, take_profit, tp_order_no, tp2_order_no,
                exit_order_no, submitted_at, updated_at, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.get("order_no", ""),
                order.get("symbol", ""),
                order.get("side", "buy"),
                order.get("status", ""),
                int(order.get("quantity", 0)),
                int(order.get("filled_qty", 0)),
                float(order.get("price", 0.0)),
                float(order.get("entry", 0.0)),
                float(order.get("stop", 0.0)),
                float(order.get("take_profit", 0.0)),
                order.get("tp_order_no", ""),
                order.get("tp2_order_no", ""),
                order.get("exit_order_no", ""),
                order.get("submitted_at", datetime.now().isoformat()),
                datetime.now().isoformat(),
                self._json(order),
            ),
        )

    def save_fill(self, fill: dict[str, Any]) -> None:
        """Insert fill record into fills table."""
        self._execute(
            """
            insert into fills (
                order_no, symbol, side, filled_qty, avg_price, fees,
                realized_pnl, fill_time, is_complete, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.get("order_no", ""),
                fill.get("symbol", ""),
                fill.get("side", "buy"),
                int(fill.get("filled_qty", 0)),
                float(fill.get("avg_price", 0.0)),
                float(fill.get("fees", 0.0)),
                float(fill.get("realized_pnl", 0.0)),
                fill.get("fill_time", datetime.now().isoformat()),
                int(fill.get("is_complete", False)),
                self._json(fill.get("payload_json", {})),
            ),
        )

    def save_position(self, symbol: str, order: dict[str, Any] | None) -> None:
        """Upsert or delete position."""
        if order is None:
            self._execute("delete from positions where symbol = ?", (symbol,))
        else:
            self._execute(
                """
                insert or replace into positions (
                    symbol, order_no, quantity, entry, stop, take_profit,
                    opened_at, updated_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    order.get("order_no", ""),
                    int(order.get("quantity", 0)),
                    float(order.get("entry", 0.0)),
                    float(order.get("stop", 0.0)),
                    float(order.get("take_profit", 0.0)),
                    order.get("submitted_at", datetime.now().isoformat()),
                    datetime.now().isoformat(),
                    self._json(order),
                ),
            )

    def save_daily_risk(self, date: str, entries: int, loss: float, realized: float, total_eval: int) -> None:
        """Upsert daily risk record."""
        self._execute(
            """
            insert or replace into daily_risk (
                date, entries, loss, realized, total_eval, updated_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                int(entries),
                float(loss),
                float(realized),
                int(total_eval),
                datetime.now().isoformat(),
            ),
        )

    def load_engine_state(self) -> dict[str, Any] | None:
        """Load engine state (id=1)."""
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("select * from engine_state where id = 1")
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    def load_orders(self, status_filter: list[str] | None = None) -> list[dict[str, Any]]:
        """Load orders from database, optionally filtered by status."""
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            if status_filter:
                placeholders = ",".join("?" * len(status_filter))
                cursor = conn.execute(f"select * from orders where status in ({placeholders})", status_filter)
            else:
                cursor = conn.execute("select * from orders")
            return [dict(row) for row in cursor.fetchall()]

    def load_positions(self) -> list[dict[str, Any]]:
        """Load all positions from database."""
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("select * from positions")
            return [dict(row) for row in cursor.fetchall()]

    def load_daily_risk(self, date: str) -> dict[str, Any] | None:
        """Load daily risk record for a specific date."""
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("select * from daily_risk where date = ?", (date,))
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)

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
        self._execute(
            """insert into strategy_change_log (
                created_at, changed_rule, before_value, after_value,
                reason, expected_effect, review_date
            ) values (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), changed_rule, before_value,
             after_value, reason, expected_effect, review_date),
        )

    def cleanup_signal_log(self, max_rows: int = 10000, keep_days: int = 30) -> int:
        """signal_log 보관량 제한.

        max_rows 초과 시 오래된 행부터 삭제.
        keep_days 이전 데이터도 삭제.
        반환값: 삭제된 행 수
        """
        deleted = 0
        with sqlite3.connect(self.path) as conn:
            # 오래된 데이터 삭제
            cursor = conn.execute(
                f"DELETE FROM signal_log WHERE created_at < datetime('now', '-{keep_days} days')"
            )
            deleted += cursor.rowcount

            # 전체 row 수 확인 후 초과분 삭제
            total = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            if total > max_rows:
                excess = total - max_rows
                cursor = conn.execute(
                    "DELETE FROM signal_log WHERE id IN (SELECT id FROM signal_log ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                deleted += cursor.rowcount
            conn.commit()
        return deleted

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
                create table if not exists engine_state (
                    id integer primary key default 1,
                    updated_at text,
                    running integer,
                    degraded integer,
                    daily_entries integer,
                    daily_loss real,
                    daily_realized real,
                    last_total_eval integer,
                    last_error text,
                    symbols_json text,
                    warmup_dates_json text
                );
                create table if not exists orders (
                    order_no text primary key,
                    symbol text,
                    side text,
                    status text,
                    quantity integer,
                    filled_qty integer,
                    price real,
                    entry real,
                    stop real,
                    take_profit real,
                    tp_order_no text,
                    tp2_order_no text,
                    exit_order_no text,
                    submitted_at text,
                    updated_at text,
                    payload_json text
                );
                create table if not exists fills (
                    id integer primary key autoincrement,
                    order_no text,
                    symbol text,
                    side text,
                    filled_qty integer,
                    avg_price real,
                    fees real,
                    realized_pnl real,
                    fill_time text,
                    is_complete integer,
                    payload_json text
                );
                create table if not exists positions (
                    symbol text primary key,
                    order_no text,
                    quantity integer,
                    entry real,
                    stop real,
                    take_profit real,
                    opened_at text,
                    updated_at text,
                    payload_json text
                );
                create table if not exists daily_risk (
                    date text primary key,
                    entries integer,
                    loss real,
                    realized real,
                    total_eval integer,
                    updated_at text
                );
                """
            )

    def _execute(self, sql: str, params: tuple | None = None) -> None:
        """Execute SQL (write only)."""
        try:
            with sqlite3.connect(self.path) as conn:
                if params:
                    conn.execute(sql, params)
                else:
                    conn.execute(sql)
                conn.commit()
        except Exception as e:
            pass

    def _json(self, obj: Any) -> str:
        """Serialize to JSON."""
        try:
            return json.dumps(obj, default=str, ensure_ascii=False)
        except Exception:
            return "{}"
