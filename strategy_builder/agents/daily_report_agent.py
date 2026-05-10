"""Daily Report Agent — 장후 일일 복기 자동 작성.

Supabase에서 오늘의 매매 데이터를 읽어 Claude/GPT로 한국어 복기 리포트를 생성한다.
생성된 리포트는 Notion Daily Trading Report DB에 저장하고 Telegram으로 요약을 전송한다.

⚠️ 이 에이전트의 권한:
   ✅ Supabase 읽기 (daily_reports, signals, orders, fills, premarket_scans)
   ✅ Notion 페이지 생성/업데이트
   ✅ Telegram 메시지 전송
   ✅ Google Calendar 이벤트 생성
   🚫 KIS 주문 API 직접 호출 금지
   🚫 runtime_config.trading_enabled 변경 금지
   🚫 LIVE_TRADING_ENABLED 변경 금지

사용법:
    python -m strategy_builder.agents.daily_report_agent
    python -m strategy_builder.agents.daily_report_agent --date 2026-05-09
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from typing import Any
from zoneinfo import ZoneInfo

import requests

# 경로 설정
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, root)

from dotenv import load_dotenv
load_dotenv(os.path.join(root, "deploy", ".env"))

from strategy_builder.agents.llm_client import LLMClient
from strategy_builder.agents.tool_registry import build_agent_tool_registry
from strategy_builder.core.supabase_journal import SupabaseJournal

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 한국 주식 데이트레이딩 복기 전문가입니다.
ICT(Inner Circle Trader) Liquidity Reclaim 전략을 운용하는 자동매매 시스템의 오늘 매매 데이터를 분석하여
트레이더가 내일 더 나은 결정을 내릴 수 있도록 구조적인 복기 리포트를 작성해주세요.

분석 원칙:
- 수익/손실보다 "전략 규칙 준수 여부"를 최우선으로 평가하세요
- 구체적인 숫자(가격, 수량, R배수)를 근거로 사용하세요
- 실수는 비판하되, 개선 방법을 구체적으로 제시하세요
- 시장 환경(지수 방향, 섹터 동향)과 연결해서 해석하세요
- 200자 이상의 충분한 분석을 작성하세요

절대 하지 말아야 할 것:
- 주문 실행, 포지션 변경, 설정 변경 제안
- 근거 없는 내일 예측
- "열심히 하세요" 같은 의미 없는 격려"""


class DailyReportAgent:
    """일일 복기 리포트 생성 에이전트."""

    def __init__(self):
        self.llm = LLMClient()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
        self.notion_token = os.environ.get("NOTION_TOKEN", "").strip()
        self.notion_daily_db = os.environ.get("NOTION_DAILY_REPORT_DB_ID", "").strip()
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.tool_registry = build_agent_tool_registry(audit_sink=SupabaseJournal())

    # ── Agent tool calls ──────────────────────────────────────

    def fetch_today_data(self, trade_date: str) -> dict[str, Any]:
        """오늘의 전체 매매 데이터 수집."""
        logger.info("데이터 수집 중: %s", trade_date)
        return self.tool_registry.call(
            "daily_report_agent",
            "get_daily_report_context",
            trade_date=trade_date,
        )

    # ── LLM 분석 ──────────────────────────────────────────────

    def build_analysis_prompt(self, data: dict[str, Any]) -> str:
        """데이터를 LLM 프롬프트로 변환."""
        dr = data["daily_report"]
        fills = data["fills"]
        signals = data["signals"]
        orders = data["orders"]
        premarket = data["premarket_scan"]
        blocked = data["blocked_entries"]

        # 수익 요약
        total_pnl = float(dr.get("daily_pnl", 0) or 0)
        trades = int(dr.get("trades_count", 0) or 0)
        win_rate = float(dr.get("win_rate", 0) or 0)
        avg_r = float(dr.get("average_r", 0) or 0)
        stopped = dr.get("stopped_reason", "")

        # 체결 상세
        fill_details = []
        for f in fills:
            pnl = float(f.get("realized_pnl", 0) or 0)
            fill_details.append(
                f"  - {f.get('symbol')} {f.get('side')} "
                f"{f.get('filled_qty')}주 @ {f.get('avg_price')} "
                f"→ PnL {pnl:+,.0f}원"
            )

        # 시그널 상세
        signal_details = []
        for s in signals[:10]:
            taken = "✅ 진입" if s.get("action_taken") else f"🚫 차단({s.get('reason_not_taken', '')})"
            signal_details.append(
                f"  - {s.get('ticker')} [{taken}] "
                f"진입후보={s.get('entry_candidate')} "
                f"손절={s.get('stop_candidate')} "
                f"목표={s.get('target_candidate')}"
            )

        # 장전 watchlist
        watchlist_str = ", ".join(
            f"{p.get('ticker')}({p.get('ict_setup', '')[:15]})"
            for p in premarket[:8]
        )

        prompt = f"""=== {data['trade_date']} 매매 데이터 ===

【일일 결산】
- 실현손익: {total_pnl:+,.0f}원
- 거래 횟수: {trades}건 (승률 {win_rate:.0%})
- 평균 R배수: {avg_r:.2f}R
- 중단 사유: {stopped or '정상 종료'}

【장전 watchlist】 ({len(premarket)}개 선정)
{watchlist_str or '없음'}

【시그널 발생 이력】 ({len(signals)}개)
{chr(10).join(signal_details) or '  없음'}

【실체결 내역】 ({len(fills)}건)
{chr(10).join(fill_details) or '  없음'}

【진입 차단 내역】 ({len(blocked)}건)
{chr(10).join(f"  - {b.get('ticker')}: {b.get('reason_not_taken')}" for b in blocked[:5]) or '  없음'}

=== 분석 요청 ===
위 데이터를 바탕으로 아래 JSON 형식으로 복기 리포트를 작성해주세요:

{{
  "summary": "오늘 매매 전체 요약 (3-5문장, 구체적 숫자 포함)",
  "strengths": ["잘한 점 1", "잘한 점 2"],
  "mistakes": ["실수한 점 1 (원인 포함)", "실수한 점 2"],
  "rule_violations": ["위반 규칙 (없으면 빈 배열)"],
  "loss_analysis": "손실이 발생했다면 원인 분석 (없으면 null)",
  "market_context": "오늘 시장 환경 해석 (watchlist 선정 종목 방향, 지수 영향 등)",
  "improvements": ["내일 개선점 1 (구체적)", "내일 개선점 2"],
  "strategy_note": "전략 자체에 대한 메타 관찰 (패턴, 통계적 경향)",
  "telegram_summary": "Telegram 결산 메시지 (이모지 포함, 3-4줄, 핵심만)",
  "notion_body": "Notion Daily Report에 넣을 본문 (마크다운 형식, 500자 이상)"
}}"""
        return prompt

    def run(self, trade_date: str | None = None) -> dict[str, Any]:
        """에이전트 실행. 실행 결과 반환."""
        if trade_date is None:
            trade_date = datetime.now(KST).strftime("%Y-%m-%d")

        logger.info("=== Daily Report Agent 시작: %s ===", trade_date)

        # 1. 데이터 수집
        data = self.fetch_today_data(trade_date)

        if not data["daily_report"] and not data["fills"]:
            logger.warning("오늘 매매 데이터 없음 — 리포트 생략")
            return {"status": "no_data", "date": trade_date}

        # 2. LLM 분석
        prompt = self.build_analysis_prompt(data)
        logger.info("LLM 분석 요청 중 (모델: %s)...", self.llm.model)

        if self.llm.available:
            analysis, usage = self.llm.complete_json(
                SYSTEM_PROMPT,
                prompt,
                max_tokens=3000,
                required_keys=["summary", "what_worked", "what_failed", "next_action"],
                fallback=self._fallback_report(data),
            )
        else:
            logger.warning("LLM 미설정 — 기본 리포트 생성")
            analysis = self._fallback_report(data)
            usage = {}

        # 3. Notion 업데이트는 NotionSyncWorker가 Supabase 기록을 기준으로 처리한다.
        # Agent가 직접 Notion을 만지면 재실행 시 본문 중복 append와 API 결합도가 생긴다.

        # 4. Telegram 전송
        if self.telegram_token and self.telegram_chat:
            self._send_telegram(trade_date, data, analysis)

        # 5. Google Calendar 이벤트
        self._create_gcal_event(trade_date, analysis)

        # 6. agent_runs 기록
        self._record_run(trade_date, data, analysis, usage)

        logger.info("=== Daily Report Agent 완료 ===")
        return {"status": "success", "date": trade_date, "analysis": analysis}

    # ── Telegram 전송 ─────────────────────────────────────────

    def _send_telegram(self, trade_date: str, data: dict, analysis: dict) -> None:
        if not self.telegram_token or not self.telegram_chat:
            return
        dr = data["daily_report"]
        pnl = float(dr.get("daily_pnl", 0) or 0)
        trades = int(dr.get("trades_count", 0) or 0)
        win_rate = float(dr.get("win_rate", 0) or 0)

        telegram_msg = analysis.get("telegram_summary", "")
        if not telegram_msg:
            emoji = "📈" if pnl >= 0 else "📉"
            telegram_msg = (
                f"{emoji} <b>{trade_date} 일일 결산</b>\n"
                f"손익: <b>{pnl:+,.0f}원</b>\n"
                f"거래: {trades}건 (승률 {win_rate:.0%})\n"
                f"AI 복기: Notion 확인"
            )

        try:
            requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={"chat_id": self.telegram_chat, "text": telegram_msg, "parse_mode": "HTML"},
                timeout=10,
            )
            logger.info("Telegram 결산 전송 완료")
        except Exception:
            logger.exception("Telegram 전송 실패")

    # ── Google Calendar 이벤트 ────────────────────────────────

    def _create_gcal_event(self, trade_date: str, analysis: dict) -> None:
        try:
            from core.gcal_reporter import publish_gcal_event_if_configured
            summary_text = analysis.get("summary", "")[:200]
            publish_gcal_event_if_configured(
                workflow="postmarket-feedback",
                report_date=trade_date,
                notion_url=None,
                summary_text=summary_text,
                event_dt=datetime.now(KST),
            )
            logger.info("GCal 장후 피드백 이벤트 생성")
        except Exception:
            logger.debug("GCal 이벤트 생성 실패 (선택적)")

    # ── agent_runs 기록 ───────────────────────────────────────

    def _record_run(self, trade_date: str, data: dict, analysis: dict, usage: dict) -> None:
        self.tool_registry.call(
            "daily_report_agent",
            "save_agent_run",
            agent_name="daily_report_agent",
            trade_date=trade_date,
            input_payload={
                "signals_count": len(data["signals"]),
                "fills_count": len(data["fills"]),
                "orders_count": len(data["orders"]),
            },
            output_payload={
                "summary": analysis.get("summary", "")[:500],
                "strengths": analysis.get("strengths", []),
                "mistakes": analysis.get("mistakes", []),
                "improvements": analysis.get("improvements", []),
            },
            status="SUCCESS",
            llm_model=self.llm.model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    # ── 폴백 (LLM 없을 때) ────────────────────────────────────

    def _fallback_report(self, data: dict) -> dict:
        dr = data["daily_report"]
        pnl = float(dr.get("daily_pnl", 0) or 0)
        trades = int(dr.get("trades_count", 0) or 0)
        fills = data["fills"]

        return {
            "summary": f"오늘 {trades}건 거래, 실현손익 {pnl:+,.0f}원. LLM 미설정으로 상세 분석 생략.",
            "strengths": ["데이터 수집 완료"],
            "mistakes": [],
            "rule_violations": [],
            "loss_analysis": None,
            "market_context": "시장 분석 미실행 (LLM 필요)",
            "improvements": ["ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 설정 후 상세 분석 활성화"],
            "strategy_note": "",
            "telegram_summary": f"{'📈' if pnl >= 0 else '📉'} {data['trade_date']} 결산\n손익: {pnl:+,.0f}원\n거래: {trades}건",
            "notion_body": f"## {data['trade_date']} 매매 복기\n\n실현손익: {pnl:+,.0f}원\n거래 횟수: {trades}건\n\n상세 AI 분석을 위해 LLM API 키를 설정하세요.",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Report Agent")
    parser.add_argument("--date", type=str, default=None, help="분석 날짜 (YYYY-MM-DD, 기본: 오늘)")
    args = parser.parse_args()

    agent = DailyReportAgent()
    result = agent.run(trade_date=args.date)

    if result.get("status") == "success":
        print(f"\n✅ 리포트 생성 완료: {result['date']}")
        analysis = result.get("analysis", {})
        if analysis.get("summary"):
            print(f"\n📋 요약:\n{analysis['summary']}")
        if analysis.get("improvements"):
            print(f"\n💡 개선점:")
            for item in analysis["improvements"]:
                print(f"  • {item}")
    elif result.get("status") == "no_data":
        print(f"⚠️  {result['date']} 매매 데이터 없음")
    else:
        print(f"❌ 실패: {result}")


if __name__ == "__main__":
    main()
