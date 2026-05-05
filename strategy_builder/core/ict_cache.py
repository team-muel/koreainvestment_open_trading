"""SQLite OHLCV cache for ICT strategy timeframes."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ict_core import Candle


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
