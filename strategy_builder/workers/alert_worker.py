"""Alert Worker - Telegram notification handling."""
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

POLL_INTERVAL = 5


class TelegramAlerter:
    """Telegram Bot API alerter."""

    BASE = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send message."""
        try:
            resp = requests.post(
                f"{self.BASE}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:100])
                return False
            return True
        except Exception:
            logger.exception("Telegram send error")
            return False

    def send_order_filled(self, order: dict[str, Any]) -> None:
        symbol = order.get("symbol", "")
        side = "🟢매수" if order.get("side") == "buy" else "🔴매도"
        qty = order.get("filled_qty") or order.get("quantity", 0)
        price = order.get("avg_price") or order.get("entry", 0)
        pnl = order.get("realized_pnl")
        pnl_str = f"\n💰 실현손익: <b>{pnl:+,.0f}원</b>" if pnl is not None else ""
        msg = (
            f"✅ <b>체결 완료</b>\n"
            f"종목: {symbol}\n"
            f"방향: {side}\n"
            f"수량: {qty:,}주 @ {price:,.0f}원{pnl_str}\n"
            f"⏰ {datetime.now(KST).strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def send_signal_detected(self, signal: dict[str, Any]) -> None:
        ticker = signal.get("ticker", "")
        entry = signal.get("entry_candidate") or 0
        stop = signal.get("stop_candidate") or 0
        target = signal.get("target_candidate") or 0
        risk_pct = ((entry - stop) / entry * 100) if entry > 0 and stop > 0 else 0
        msg = (
            f"📡 <b>ICT 시그널 발생</b>\n"
            f"종목: <b>{ticker}</b>\n"
            f"진입: {entry:,.0f}원\n"
            f"손절: {stop:,.0f}원 ({risk_pct:.1f}%)\n"
            f"목표: {target:,.0f}원\n"
            f"⏰ {datetime.now(KST).strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def send_emergency_stop(self, reason: str) -> None:
        msg = (
            f"🚨 <b>엔진 긴급 정지</b>\n"
            f"사유: {reason}\n"
            f"⏰ {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"즉시 확인이 필요합니다."
        )
        self.send(msg)

    def send_daily_summary(self, report: dict[str, Any]) -> None:
        date = report.get("date", "")
        pnl = report.get("daily_pnl", 0) or 0
        trades = report.get("trades_count", 0) or 0
        win_rate = report.get("win_rate") or 0
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>일일 결산 {date}</b>\n"
            f"손익: <b>{pnl:+,.0f}원</b>\n"
            f"거래: {trades}건 (승률 {win_rate:.0%})\n"
            f"정지 사유: {report.get('stopped_reason') or '-'}"
        )
        self.send(msg)

    def send_engine_started(self, symbols: list[str]) -> None:
        msg = (
            f"🚀 <b>ICT 엔진 시작</b>\n"
            f"모드: 모의투자(VPS)\n"
            f"종목: {', '.join(symbols[:10])}\n"
            f"⏰ {datetime.now(KST).strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def send_heartbeat_failure(self, worker: str, last_beat: str) -> None:
        msg = (
            f"⚠️ <b>Heartbeat 실패</b>\n"
            f"Worker: {worker}\n"
            f"마지막 신호: {last_beat}\n"
            f"서버 확인 필요"
        )
        self.send(msg)


class AlertWorker:
    """Supabase polling alert worker."""

    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            logger.warning("AlertWorker: TELEGRAM_BOT_TOKEN/CHAT_ID not set → disabled")
            self.alerter = None
        else:
            self.alerter = TelegramAlerter(token, chat_id)

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        self.supabase_url = url
        self.supabase_key = key
        self._last_signal_id: str | None = None
        self._last_fill_id: str | None = None
        self._last_heartbeat_check = datetime.now(KST)
        self._sent_daily_report_dates: set[str] = set()

    def _supabase_get(self, path: str, params: dict | None = None) -> list[dict]:
        if not self.supabase_url:
            return []
        try:
            resp = requests.get(
                f"{self.supabase_url}/rest/v1/{path}",
                headers={
                    "apikey": self.supabase_key,
                    "Authorization": f"Bearer {self.supabase_key}",
                },
                params=params or {},
                timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            return []

    def _check_new_signals(self) -> None:
        """Check for new signals."""
        if not self.alerter:
            return
        params = {
            "action_taken": "eq.true",
            "order": "created_at.desc",
            "limit": "5",
        }
        if self._last_signal_id:
            params["id"] = f"gt.{self._last_signal_id}"
        rows = self._supabase_get("signals", params)
        for row in reversed(rows):
            self.alerter.send_signal_detected(row)
            self._last_signal_id = row.get("id")

    def _check_new_fills(self) -> None:
        """Check for new fills."""
        if not self.alerter:
            return
        params = {
            "is_complete": "eq.true",
            "order": "created_at.desc",
            "limit": "5",
        }
        if self._last_fill_id:
            params["id"] = f"gt.{self._last_fill_id}"
        rows = self._supabase_get("fills", params)
        for row in reversed(rows):
            self.alerter.send_order_filled(row)
            self._last_fill_id = row.get("id")

    def _check_emergency_stops(self) -> None:
        """Check for emergency stops."""
        if not self.alerter:
            return
        rows = self._supabase_get("trade_journal", {
            "event": "eq.EMERGENCY_STOP",
            "order": "created_at.desc",
            "limit": "3",
        })
        for row in rows:
            payload = row.get("payload") or {}
            reason = (payload.get("payload") or {}).get("exit_reason", "알 수 없음")
            created = row.get("created_at", "")[:19]
            self.alerter.send_emergency_stop(f"{reason} (at {created})")

    def _check_heartbeat(self) -> None:
        """Check heartbeat."""
        if not self.alerter:
            return
        now = datetime.now(KST)
        if (now - self._last_heartbeat_check).total_seconds() < 300:
            return
        self._last_heartbeat_check = now
        rows = self._supabase_get("engine_heartbeats", {
            "worker": "eq.trading-worker",
            "order": "beat_at.desc",
            "limit": "1",
        })
        if not rows:
            return
        beat_at_str = rows[0].get("beat_at", "")
        if not beat_at_str:
            return
        try:
            from datetime import timezone
            beat_at = datetime.fromisoformat(beat_at_str.replace("Z", "+00:00"))
            elapsed = (now.astimezone(timezone.utc) - beat_at.astimezone(timezone.utc)).total_seconds()
            if elapsed > 300:
                self.alerter.send_heartbeat_failure("trading-worker", beat_at_str[:19])
        except Exception:
            pass

    def _check_daily_report(self) -> None:
        """Check for daily report and send Telegram notification."""
        if not self.alerter:
            return

        today = datetime.now(KST).date().isoformat()
        if today in self._sent_daily_report_dates:
            return

        rows = self._supabase_get("daily_reports", {
            "report_date": f"eq.{today}",
            "order": "report_date.desc",
            "limit": "1",
        })
        if not rows:
            return

        report = rows[0]
        try:
            report_data = dict(
                date=today,
                daily_pnl=report.get("daily_pnl", 0),
                trades_count=report.get("trades_count", 0),
                win_rate=report.get("win_rate", 0),
                stopped_reason=report.get("stopped_reason", "-"),
            )
            self.alerter.send_daily_summary(report_data)
            self._sent_daily_report_dates.add(today)
        except Exception:
            logger.exception("_check_daily_report send failed")

    def run(self) -> None:
        """Main loop."""
        logger.info("AlertWorker started (poll interval: %ds)", POLL_INTERVAL)
        if self.alerter:
            self.alerter.send("🤖 Alert Worker 시작됨")
        while True:
            try:
                self._check_new_signals()
                self._check_new_fills()
                self._check_emergency_stops()
                self._check_heartbeat()
                self._check_daily_report()
            except Exception:
                logger.exception("AlertWorker loop error")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    AlertWorker().run()
