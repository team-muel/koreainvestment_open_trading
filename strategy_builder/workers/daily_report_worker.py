"""Daily Report Worker — 장후 일일 리포트 자동 생성.

장 종료 후(15:40~) 실행하여:
1. 당일 trade_journal에서 거래 내역 집계
2. 손익/승률/R배수/profit_factor 계산
3. Supabase daily_reports 저장
4. Notion 일일 리포트 동기화 (notion_sync_worker가 처리)
5. Google Calendar 장후 피드백 이벤트 생성
6. Telegram 일일 결산 메시지 전송

환경변수:
    SUPABASE_URL
    SUPABASE_KEY
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GOOGLE_OAUTH_CLIENT_JSON
    GOOGLE_TOKEN_JSON
    GOOGLE_CALENDAR_ID
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from core.gcal_reporter import publish_gcal_postmarket_if_configured

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

POLL_INTERVAL = 60  # 초


class SupabaseClient:
    """Supabase REST client."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def select(
        self,
        table: str,
        filters: dict | None = None,
        limit: int = 1000,
        order: str | None = None,
    ) -> list[dict]:
        """SELECT from table with filters."""
        params: dict[str, Any] = {"limit": limit}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        try:
            resp = requests.get(
                f"{self.base}/{table}",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            logger.exception("SupabaseClient.select failed")
            return []

    def insert(self, table: str, data: dict | list[dict]) -> None:
        """INSERT into table."""
        try:
            resp = requests.post(
                f"{self.base}/{table}",
                headers=self.headers,
                json=data if isinstance(data, list) else [data],
                timeout=10,
            )
            if not resp.ok:
                logger.error("SupabaseClient.insert %s failed: %s", table, resp.text[:200])
        except Exception:
            logger.exception("SupabaseClient.insert error")

    def upsert(self, table: str, data: dict | list[dict], on_conflict: str | None = None) -> None:
        """UPSERT into table."""
        try:
            headers = {**self.headers, "Prefer": "resolution=merge-duplicates"}
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
                logger.error("SupabaseClient.upsert %s failed: %s", table, resp.text[:200])
        except Exception:
            logger.exception("SupabaseClient.upsert error")


class TelegramAlerter:
    """Telegram 알림 전송."""

    BASE = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send_daily_summary(self, report: dict[str, Any]) -> bool:
        """일일 결산 메시지 전송."""
        try:
            date = report.get("report_date", "")
            pnl = report.get("daily_pnl", 0) or 0
            trades = report.get("trades_count", 0) or 0
            win_rate = report.get("win_rate", 0) or 0
            emoji = "📈" if pnl >= 0 else "📉"

            msg = (
                f"{emoji} <b>일일 결산 {date}</b>\n"
                f"손익: <b>{pnl:+,.0f}원</b>\n"
                f"거래: {trades}건 (승률 {win_rate:.0%})\n"
                f"정지 사유: {report.get('stopped_reason') or '-'}"
            )

            resp = requests.post(
                f"{self.BASE}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s", resp.text[:100])
                return False
            return True
        except Exception:
            logger.exception("TelegramAlerter.send_daily_summary error")
            return False


class DailyReportWorker:
    """일일 리포트 자동 생성 워커."""

    def __init__(self):
        # Supabase 초기화
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_KEY", "").strip()
        if url and key:
            self.supabase = SupabaseClient(url, key)
        else:
            logger.warning("DailyReportWorker: SUPABASE_URL/KEY not set → disabled")
            self.supabase = None

        # Telegram 초기화
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id:
            self.alerter = TelegramAlerter(token, chat_id)
        else:
            logger.warning("DailyReportWorker: TELEGRAM_BOT_TOKEN/CHAT_ID not set → disabled")
            self.alerter = None

    def fetch_today_fills(self) -> list[dict]:
        """오늘 실체결 내역 조회 (fills 테이블 기준).

        orders나 trade_journal이 아닌 fills를 사용해야
        실제 체결가/수량/수수료가 정확하게 반영된다.
        """
        if not self.supabase:
            return []

        today = datetime.now(KST).strftime("%Y-%m-%d")
        today_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        try:
            rows = self.supabase.select(
                "fills",
                filters={
                    "is_complete": "eq.true",
                    "fill_time": f"gte.{today_start}",
                },
                limit=100,
                order="fill_time.asc",
            )
            return rows
        except Exception:
            logger.exception("fetch_today_fills 실패")
            return []

    def calculate_metrics(self, fills: list[dict]) -> dict[str, Any]:
        """fills 기준 일일 지표 계산."""
        if not fills:
            return {
                "total_trades": 0,
                "wins": 0, "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "avg_r": 0.0,
                "profit_factor": 0.0,
                "max_win": 0.0,
                "max_loss": 0.0,
            }

        # 매도 체결만 손익 계산 대상
        sell_fills = [f for f in fills if f.get("side") == "sell" and f.get("realized_pnl") is not None]

        pnls = [float(f.get("realized_pnl", 0) or 0) for f in sell_fills]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        win_rate = len(wins) / len(pnls) if pnls else 0.0
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf") if wins else 0.0

        # R 배수 계산: realized_pnl / risk_amount (fills에 없으면 trade_journal에서 보완)
        avg_r = 0.0
        r_multiples = []
        for f in sell_fills:
            pnl = float(f.get("realized_pnl", 0) or 0)
            risk = float(
                f.get("risk_amount")
                or (f.get("payload") or {}).get("risk_amount")
                or 0
            )
            if risk > 0:
                r_multiples.append(pnl / risk)
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

        return {
            "total_trades": len(sell_fills),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(pnls), 2) if pnls else 0.0,
            "avg_r": round(avg_r, 4),
            "profit_factor": round(min(profit_factor, 99.0), 4),
            "max_win": round(max(wins), 2) if wins else 0.0,
            "max_loss": round(min(losses), 2) if losses else 0.0,
        }

    def save_report(self, metrics: dict[str, Any]) -> None:
        """Supabase에 일일 리포트 저장."""
        if not self.supabase:
            return

        today = datetime.now(KST).date().isoformat()
        try:
            report = {
                "report_date": today,
                "trades_count": metrics["total_trades"],
                "win_rate": metrics["win_rate"],
                "daily_pnl": metrics["total_pnl"],
                "average_r": metrics["avg_r"],
                "profit_factor": metrics["profit_factor"],
                "max_win": metrics["max_win"],
                "max_loss": metrics["max_loss"],
                "updated_at": datetime.now(KST).isoformat(),
            }
            self.supabase.upsert("daily_reports", report, on_conflict="report_date")
            logger.info("Daily report saved: %s, pnl=%.0f, trades=%d", today, metrics["total_pnl"], metrics["total_trades"])
        except Exception:
            logger.exception("save_report error")

    def run_once(self, run_agents: bool = True) -> dict[str, Any]:
        """한 번 실행 후 결과 반환."""
        fills = self.fetch_today_fills()
        metrics = self.calculate_metrics(fills)
        self.save_report(metrics)

        # Google Calendar 이벤트 생성
        today = datetime.now(KST).date().isoformat()
        try:
            publish_gcal_postmarket_if_configured(
                report_date=today,
                summary=metrics,
                event_dt=datetime.now(KST),
            )
        except Exception:
            logger.exception("publish_gcal_postmarket_if_configured error")

        # Telegram 알림
        if self.alerter:
            try:
                report = {
                    "report_date": today,
                    "daily_pnl": metrics["total_pnl"],
                    "trades_count": metrics["total_trades"],
                    "win_rate": metrics["win_rate"],
                    "stopped_reason": "Daily report completed",
                }
                self.alerter.send_daily_summary(report)
            except Exception:
                logger.exception("Telegram send_daily_summary error")

        if run_agents:
            self._run_postmarket_agents(today)

        return metrics

    def _run_postmarket_agents(self, trade_date: str) -> None:
        """Run non-ordering postmarket agents synchronously for scheduler observability."""
        try:
            from agents.daily_report_agent import DailyReportAgent
            DailyReportAgent().run(trade_date=trade_date)
        except Exception as e:
            logger.warning("DailyReportAgent 실패: %s", e)
        try:
            from agents.audit_agent import AuditAgent
            AuditAgent().run(trade_date=trade_date)
        except Exception as e:
            logger.warning("AuditAgent 실패: %s", e)

    def run_scheduled(self) -> None:
        """15:40 이후 한 번 실행하고 다음날 대기하는 메인 루프."""
        logger.info("DailyReportWorker scheduled started")

        last_execution_date: str | None = None

        while True:
            try:
                now = datetime.now(KST)
                current_date = now.date().isoformat()
                current_time = now.time()

                # 평일 확인 (월~금: 0-4)
                if now.weekday() >= 5:
                    # 주말이면 다음 평일 아침까지 대기
                    logger.info("DailyReportWorker: weekend, sleeping until next Monday")
                    time.sleep(3600)  # 1시간마다 확인
                    continue

                # 15:40 이후인지 확인
                cutoff_time = now.replace(hour=15, minute=40, second=0, microsecond=0).time()
                if current_time >= cutoff_time:
                    # 오늘 리포트가 아직 생성되지 않았으면 실행
                    if last_execution_date != current_date:
                        logger.info("DailyReportWorker: running at %s", now.strftime("%H:%M:%S"))
                        metrics = self.run_once(run_agents=True)
                        logger.info(
                            "DailyReportWorker completed: pnl=%.0f, trades=%d, win_rate=%.0%%",
                            metrics["total_pnl"],
                            metrics["total_trades"],
                            metrics["win_rate"] * 100,
                        )
                        last_execution_date = current_date

                        logger.info("장후 AI 에이전트 완료 (DailyReport + Audit)")
                    else:
                        # 이미 오늘 실행했으면 다음날까지 대기
                        time.sleep(3600)  # 1시간마다 확인
                else:
                    # 15:40 전이면 다음 15:40까지 대기
                    time.sleep(300)  # 5분마다 확인

            except Exception:
                logger.exception("DailyReportWorker loop error")
                time.sleep(60)  # 오류 발생 시 1분 후 재시도


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = DailyReportWorker()
    worker.run_scheduled()
