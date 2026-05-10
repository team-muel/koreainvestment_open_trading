"""SQLite OHLCV cache for ICT strategy timeframes."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from ict_core import Candle

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class MinuteBarCache:
    MIN_READY_BARS_PER_DAY = 300

    def __init__(self, db_path: str | Path | None = None):
        root = Path(__file__).resolve().parents[1]
        self.db_path = Path(db_path) if db_path else root / "data" / "ict_bars.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minute_bars (
                    symbol TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'backfill',
                    PRIMARY KEY (symbol, ts)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_minute_bars_symbol_ts ON minute_bars(symbol, ts)")
            columns = [row[1] for row in conn.execute("PRAGMA table_info(minute_bars)").fetchall()]
            if "source" not in columns:
                conn.execute("ALTER TABLE minute_bars ADD COLUMN source TEXT NOT NULL DEFAULT 'backfill'")

    def upsert_bars(self, symbol: str, bars: Iterable[Candle]) -> int:
        rows = [
            (
                symbol,
                bar.timestamp.replace(second=0, microsecond=0).isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                "backfill",
            )
            for bar in bars
        ]
        if not rows:
            return 0
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO minute_bars(symbol, ts, open, high, low, close, volume, source)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    open=excluded.open,
                    high=MAX(minute_bars.high, excluded.high),
                    low=MIN(minute_bars.low, excluded.low),
                    close=excluded.close,
                    volume=excluded.volume,
                    source=excluded.source
                """,
                rows,
            )
        return len(rows)

    def upsert_tick_as_minute(
        self,
        symbol: str,
        timestamp: datetime,
        price: float,
        volume: int = 0,
        source: str = "realtime",
    ) -> None:
        ts = timestamp.replace(second=0, microsecond=0).isoformat()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT open, high, low, close, volume, source FROM minute_bars WHERE symbol=? AND ts=?",
                (symbol, ts),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO minute_bars(symbol, ts, open, high, low, close, volume, source) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, ts, price, price, price, price, volume, source),
                )
            else:
                open_price, high, low, _close, current_volume, current_source = row
                next_source = source if current_source == "poll" else current_source
                conn.execute(
                    """
                    UPDATE minute_bars
                    SET open=?, high=?, low=?, close=?, volume=?, source=?
                    WHERE symbol=? AND ts=?
                    """,
                    (
                        open_price,
                        max(float(high), price),
                        min(float(low), price),
                        price,
                        int(current_volume or 0) + volume,
                        next_source,
                        symbol,
                        ts,
                    ),
                )

    def get_1m_bars(self, symbol: str, limit: int | None = None) -> list[Candle]:
        sql = "SELECT ts, open, high, low, close, volume FROM minute_bars WHERE symbol=? ORDER BY ts"
        params: tuple = (symbol,)
        if limit:
            sql = (
                "SELECT ts, open, high, low, close, volume FROM ("
                "SELECT ts, open, high, low, close, volume FROM minute_bars "
                "WHERE symbol=? ORDER BY ts DESC LIMIT ?"
                ") ORDER BY ts"
            )
            params = (symbol, limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Candle(
                timestamp=datetime.fromisoformat(ts),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=int(volume or 0),
                symbol=symbol,
                timeframe="1m",
            )
            for ts, open_, high, low, close, volume in rows
        ]

    def coverage_days(self, symbol: str) -> int:
        with self._connection() as conn:
            return int(conn.execute(
                "SELECT COUNT(DISTINCT substr(ts, 1, 10)) FROM minute_bars WHERE symbol=?",
                (symbol,),
            ).fetchone()[0] or 0)

    def ready_coverage_days(self, symbol: str, min_bars_per_day: int | None = None) -> int:
        minimum = min_bars_per_day or self.MIN_READY_BARS_PER_DAY
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS trade_date, COUNT(*) AS bar_count
                FROM minute_bars
                WHERE symbol=? AND source IN ('backfill', 'realtime')
                GROUP BY trade_date
                HAVING bar_count >= ?
                """,
                (symbol, minimum),
            ).fetchall()
        return len(rows)

    def ready_dates(self, symbol: str, min_bars_per_day: int | None = None) -> set[str]:
        minimum = min_bars_per_day or self.MIN_READY_BARS_PER_DAY
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(ts, 1, 10) AS trade_date
                FROM minute_bars
                WHERE symbol=? AND source IN ('backfill', 'realtime')
                GROUP BY trade_date
                HAVING COUNT(*) >= ?
                """,
                (symbol, minimum),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def coverage_summary(self, symbol: str) -> dict:
        return {
            "coverage_days": self.coverage_days(symbol),
            "ready_coverage_days": self.ready_coverage_days(symbol),
            "min_bars_per_ready_day": self.MIN_READY_BARS_PER_DAY,
        }

    @staticmethod
    def resample(bars: list[Candle], interval_minutes: int, timeframe: str) -> list[Candle]:
        if not bars:
            return []
        buckets: dict[datetime, list[Candle]] = {}
        for bar in bars:
            minute_of_day = bar.timestamp.hour * 60 + bar.timestamp.minute
            bucket_minute = (minute_of_day // interval_minutes) * interval_minutes
            bucket_start = bar.timestamp.replace(
                hour=bucket_minute // 60,
                minute=bucket_minute % 60,
                second=0,
                microsecond=0,
            )
            buckets.setdefault(bucket_start, []).append(bar)

        result: list[Candle] = []
        for bucket_start in sorted(buckets):
            items = sorted(buckets[bucket_start], key=lambda b: b.timestamp)
            result.append(Candle(
                timestamp=bucket_start,
                open=items[0].open,
                high=max(i.high for i in items),
                low=min(i.low for i in items),
                close=items[-1].close,
                volume=sum(i.volume for i in items),
                symbol=items[-1].symbol,
                timeframe=timeframe,
            ))
        return result

    @staticmethod
    def resample_krx_session_daily(bars: list[Candle]) -> list[Candle]:
        """Build KRX regular-session daily candles from 1-minute bars.

        Generic 390-minute bucketing depends on the first timestamp of a day and
        can split a 09:00-15:30 Korean session into the wrong daily candle. The
        ICT intraday strategy uses previous-day high/low as a core liquidity
        level, so daily candles must be grouped by actual KRX session date.
        """
        if not bars:
            return []
        session_open = time(9, 0)
        session_close = time(15, 30)
        buckets: dict[object, list[Candle]] = {}
        for bar in bars:
            ts = bar.timestamp
            if ts.tzinfo is not None:
                ts = ts.astimezone(KST).replace(tzinfo=None)
            if not (session_open <= ts.time() <= session_close):
                continue
            buckets.setdefault(ts.date(), []).append(bar)

        result: list[Candle] = []
        for trade_date in sorted(buckets):
            items = sorted(buckets[trade_date], key=lambda b: b.timestamp)
            result.append(Candle(
                timestamp=datetime.combine(trade_date, session_close),
                open=items[0].open,
                high=max(i.high for i in items),
                low=min(i.low for i in items),
                close=items[-1].close,
                volume=sum(i.volume for i in items),
                symbol=items[-1].symbol,
                timeframe="1d",
            ))
        return result


class SupabaseMinuteBarCache:
    """Supabase-backed minute bar cache with the same public API as MinuteBarCache."""

    MIN_READY_BARS_PER_DAY = MinuteBarCache.MIN_READY_BARS_PER_DAY

    def __init__(self):
        from .supabase_journal import SupabaseClient

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = (
            os.environ.get("SUPABASE_KEY", "").strip()
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        )
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required for SupabaseMinuteBarCache")
        self._client = SupabaseClient(url, key)

    def upsert_bars(self, symbol: str, bars: Iterable[Candle]) -> int:
        rows = [
            {
                "symbol": symbol,
                "ts": self._format_ts(bar.timestamp),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "source": "backfill",
            }
            for bar in bars
        ]
        if not rows:
            return 0
        self._client.upsert("bars_1m", rows, on_conflict="symbol,ts")
        return len(rows)

    def upsert_tick_as_minute(
        self,
        symbol: str,
        timestamp: datetime,
        price: float,
        volume: int = 0,
        source: str = "realtime",
    ) -> None:
        ts = self._format_ts(timestamp)
        existing = self._client.select(
            "bars_1m",
            filters={"symbol": f"eq.{symbol}", "ts": f"eq.{ts}"},
            limit=1,
        )
        if not existing:
            self._client.upsert("bars_1m", {
                "symbol": symbol,
                "ts": ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "source": source,
            }, on_conflict="symbol,ts")
            return

        row = existing[0]
        current_source = row.get("source") or source
        next_source = source if current_source == "poll" else current_source
        next_row = {
            "symbol": symbol,
            "ts": ts,
            "open": float(row.get("open") or price),
            "high": max(float(row.get("high") or price), price),
            "low": min(float(row.get("low") or price), price),
            "close": price,
            "volume": int(row.get("volume") or 0) + int(volume or 0),
            "source": next_source,
        }
        self._client.upsert("bars_1m", next_row, on_conflict="symbol,ts")

    def get_1m_bars(self, symbol: str, limit: int | None = None) -> list[Candle]:
        query_limit = int(limit) if limit else 20000
        order = "ts.desc" if limit else "ts.asc"
        rows = self._client.select(
            "bars_1m",
            filters={"symbol": f"eq.{symbol}"},
            limit=query_limit,
            order=order,
        )
        if limit:
            rows = list(reversed(rows))
        return [
            Candle(
                timestamp=self._parse_ts(row.get("ts")),
                open=float(row.get("open") or 0),
                high=float(row.get("high") or 0),
                low=float(row.get("low") or 0),
                close=float(row.get("close") or 0),
                volume=int(row.get("volume") or 0),
                symbol=symbol,
                timeframe="1m",
            )
            for row in rows
            if row.get("ts")
        ]

    def coverage_days(self, symbol: str) -> int:
        return len({self._trade_date(row) for row in self._coverage_rows(symbol) if row.get("ts")})

    def ready_coverage_days(self, symbol: str, min_bars_per_day: int | None = None) -> int:
        return len(self.ready_dates(symbol, min_bars_per_day))

    def ready_dates(self, symbol: str, min_bars_per_day: int | None = None) -> set[str]:
        minimum = min_bars_per_day or self.MIN_READY_BARS_PER_DAY
        counts: dict[str, int] = {}
        for row in self._coverage_rows(symbol):
            if (row.get("source") or "") not in {"backfill", "realtime"}:
                continue
            trade_date = self._trade_date(row)
            counts[trade_date] = counts.get(trade_date, 0) + 1
        return {trade_date for trade_date, count in counts.items() if count >= minimum}

    def coverage_summary(self, symbol: str) -> dict:
        return {
            "coverage_days": self.coverage_days(symbol),
            "ready_coverage_days": self.ready_coverage_days(symbol),
            "min_bars_per_ready_day": self.MIN_READY_BARS_PER_DAY,
            "backend": "supabase",
        }

    @staticmethod
    def resample(bars: list[Candle], interval_minutes: int, timeframe: str) -> list[Candle]:
        return MinuteBarCache.resample(bars, interval_minutes, timeframe)

    @staticmethod
    def resample_krx_session_daily(bars: list[Candle]) -> list[Candle]:
        return MinuteBarCache.resample_krx_session_daily(bars)

    def _coverage_rows(self, symbol: str) -> list[dict]:
        return self._client.select(
            "bars_1m",
            filters={"symbol": f"eq.{symbol}"},
            limit=20000,
            order="ts.desc",
        )

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(KST).replace(tzinfo=None)

    @staticmethod
    def _format_ts(value: datetime) -> str:
        timestamp = value.replace(second=0, microsecond=0)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=KST)
        else:
            timestamp = timestamp.astimezone(KST)
        return timestamp.isoformat()

    @classmethod
    def _trade_date(cls, row: dict) -> str:
        return cls._parse_ts(str(row.get("ts"))).date().isoformat()


def build_minute_bar_cache(db_path: str | Path | None = None):
    backend = os.environ.get("ICT_CACHE_BACKEND", "").strip().lower()
    use_supabase = backend == "supabase" or (
        backend not in {"sqlite", "local"} and os.environ.get("SUPABASE_URL") and (
            os.environ.get("SUPABASE_KEY")
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
    )
    if use_supabase:
        try:
            return SupabaseMinuteBarCache()
        except Exception:
            logger.exception("Supabase minute-bar cache initialization failed; falling back to SQLite")
    return MinuteBarCache(db_path)
