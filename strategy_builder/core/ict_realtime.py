"""KIS realtime trade collector for the ICT 1-minute cache."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime
from typing import Any

import websockets

import kis_auth as ka
from core.ict_cache import MinuteBarCache

logger = logging.getLogger(__name__)


CONTRACT_TR_ID = "H0STCNT0"
CONTRACT_COLUMNS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN",
    "PRDY_VRSS", "PRDY_CTRT", "WGHN_AVRG_STCK_PRC", "STCK_OPRC",
    "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL", "ACML_VOL",
    "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU",
    "CTTR", "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR",
    "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1",
    "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]


class RealtimeTickCollector:
    """Subscribe to KIS domestic stock trade ticks and persist them as 1m bars."""

    def __init__(self, cache: MinuteBarCache, env_dv: str = "vps"):
        self.cache = cache
        self.env_dv = env_dv
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._symbols: list[str] = []
        self._connected = False
        self._last_error: str | None = None

    def start(self, symbols: list[str]) -> None:
        clean_symbols = [s for s in dict.fromkeys(symbols) if len(s) == 6 and s.isdigit()]
        thread_to_join: threading.Thread | None = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                thread_to_join = self._thread
                self._stop_event.set()

        if thread_to_join is not None:
            thread_to_join.join(timeout=2)

        with self._lock:
            self._symbols = clean_symbols
            self._last_error = None
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="ict-realtime-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._connected = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set(),
                "connected": self._connected,
                "symbols": list(self._symbols),
                "last_error": self._last_error,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ka.auth_ws("vps" if self.env_dv in ("vps", "demo") else "prod")
                asyncio.run(self._connect_once())
            except Exception as e:
                logger.exception("ICT realtime collector error")
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
                self._stop_event.wait(5)

    async def _connect_once(self) -> None:
        url = ka.getTREnv().my_url_ws
        if not url:
            raise RuntimeError("KIS websocket URL is not configured")

        async with websockets.connect(url, ping_interval=None) as ws:
            with self._lock:
                self._connected = True
                symbols = list(self._symbols)

            for symbol in symbols:
                await ws.send(json.dumps(self._subscribe_message(symbol)))
                await asyncio.sleep(0.1)

            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await self._handle_message(ws, raw)

        with self._lock:
            self._connected = False

    @staticmethod
    def _subscribe_message(symbol: str) -> dict[str, Any]:
        return ka.data_fetch(CONTRACT_TR_ID, "1", {"tr_key": symbol})

    async def _handle_message(self, ws, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        if not raw:
            return

        if raw[0] == "0":
            parts = raw.split("|")
            if len(parts) < 4 or parts[1] != CONTRACT_TR_ID:
                return
            self._persist_contract_payload(parts[3])
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        header = payload.get("header", {})
        if header.get("tr_id") == "PINGPONG":
            await ws.pong(raw)

    def _persist_contract_payload(self, payload: str) -> None:
        values = payload.split("^")
        width = len(CONTRACT_COLUMNS)
        for offset in range(0, len(values), width):
            row = values[offset:offset + width]
            if len(row) < width:
                continue
            symbol = row[0]
            timestamp = self._parse_timestamp(row[33], row[1])
            price = self._to_float(row[2])
            volume = int(self._to_float(row[12]))
            if symbol and price > 0:
                self.cache.upsert_tick_as_minute(symbol, timestamp, price, volume)

    @staticmethod
    def _parse_timestamp(date_text: str, time_text: str) -> datetime:
        date_part = date_text if len(date_text) == 8 and date_text.isdigit() else datetime.now().strftime("%Y%m%d")
        time_part = time_text[:6].ljust(6, "0")
        return datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
