"""Strategy Reviewer Agent — 최근 2~4주 로그 기반 전략 개선 제안.

매주 금요일 또는 수동 호출로 실행하여:
- 최근 거래 패턴 분석 (승률, R배수, 진입 차단 원인 등)
- ICT 원칙 대비 실제 실행 품질 평가
- 구체적인 파라미터 조정 제안 (코드 변경은 수동으로 결정)
- Notion에 주간 전략 리뷰 리포트 저장
- Telegram으로 핵심 인사이트 전송

⚠️ 권한:
   ✅ Supabase 읽기 전용 (daily_reports, signals, fills, agent_runs)
   ✅ Notion Strategy Review DB 쓰기
   ✅ Telegram 알림
   🚫 runtime_config 변경 금지 (파라미터 변경은 사람이 결정)
   🚫 LIVE_TRADING_ENABLED 변경 금지

사용법:
    python -m strategy_builder.agents.strategy_reviewer_agent
    python -m strategy_builder.agents.strategy_reviewer_agent --weeks 2
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, root)

from dotenv import load_dotenv
load_dotenv(os.path.join(root, "deploy", ".env"))

from strategy_builder.agents.llm_client import LLMClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 ICT 데이트레이딩 전략 분석 전문가입니다.
자동매매 시스템의 최근 거래 이력을 분석하여 전략 개선안을 제시합니다.

분석 원칙:
- 통계적으로 유의미한 패턴만 언급 (표본 크기 명시)
- "이렇게 느껴진다"가 아닌 "데이터가 이렇게 보인다"로 서술
- 파라미터 변경 제안 시 구체적인 수치와 근거 명시
- ICT 원칙(Liquidity Reclaim, VWAP, OR Structure)과 연결하여 해석
- 승리 패턴은 강화하고 손실 패턴은 원인을 규명
- 개선안은 즉시 적용 가능한 것 (진입 조건)과 장기 과제 (전략 구조)로 구분

절대 하지 말 것:
- 자동으로 설정 변경하거나 코드 수정 지시
- 데이터가 없는 구간에 대한 추측
- 단기 결과만 보고 전략 폐기 추천"""


class StrategyReviewerAgent:
    """최근 2~4주 로그 기반 전략 리뷰 생성."""

    def __init__(self):
        self.llm = LLMClient()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
        self.notion_token = os.environ.get("NOTION_TOKEN", "").strip()
        self.notion_daily_db = os.environ.get("NOTION_DAILY_REPORT_DB_ID", "").strip()
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    def _sb_get(self, table: str, params: dict | None = None) -> list[dict]:
        if not self.supabase_url:
            return []
        try:
            resp = requests.get(
                f"{self.supabase_url}/rest/v1/{table}",
                headers={"apikey": self.supabase_key, "Authorization": f"Bearer {self.supabase_key}",
                         "Prefer": "return=representation"},
                params=params or {}, timeout=15,
            )
            return resp.json() if resp.ok else []
        except Exception:
            return []

    def _sb_upsert(self, table: str, data: dict, on_conflict: str = "") -> bool:
        if not self.supabase_url:
            return False
        try:
            params = {}
            if on_conflict:
                params["on_conflict"] = on_conflict
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/{table}",
                headers={"apikey": self.supabase_key, "Authorization": f"Bearer {self.supabase_key}",
                         "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=[data], params=params, timeout=15,
            )
            return resp.ok
        except Exception:
            return False

    # ── 데이터 수집 ───────────────────────────────────────────

    def fetch_period_data(self, weeks: int = 2) -> dict[str, Any]:
        """최근 N주 거래 데이터 수집."""
        start_date = (datetime.now(KST) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        logger.info("전략 리뷰 데이터 수집: %s 이후 %d주", start_date, weeks)

        # 일일 리포트
        daily_reports = self._sb_get("daily_reports", {
            "report_date": f"gte.{start_date}",
            "order": "report_date.asc",
            "limit": "30",
        })

        # 시그널 (진입 성공/차단 모두)
        signals = self._sb_get("signals", {
            "created_at": f"gte.{start_date}T00:00:00+09:00",
            "order": "created_at.asc",
            "limit": "200",
        })

        # 실체결
        fills = self._sb_get("fills", {
            "fill_time": f"gte.{start_date}T00:00:00+09:00",
            "is_complete": "eq.true",
            "order": "fill_time.asc",
            "limit": "100",
        })

        # agent_runs (리스크 리뷰 포함)
        risk_reviews = self._sb_get("agent_runs", {
            "agent_name": "eq.risk_review_agent",
            "trade_date": f"gte.{start_date}",
            "order": "trade_date.asc",
            "limit": "50",
        })

        return {
            "period_weeks": weeks,
            "start_date": start_date,
            "end_date": datetime.now(KST).strftime("%Y-%m-%d"),
            "daily_reports": daily_reports,
            "signals": signals,
            "fills": fills,
            "risk_reviews": risk_reviews,
        }

    # ── 통계 계산 ─────────────────────────────────────────────

    def calculate_stats(self, data: dict[str, Any]) -> dict[str, Any]:
        """기간 통계 계산."""
        reports = data["daily_reports"]
        signals = data["signals"]
        fills = data["fills"]

        # 거래일 통계
        trading_days = len(reports)
        total_pnl = sum(float(r.get("daily_pnl", 0) or 0) for r in reports)
        avg_daily_pnl = total_pnl / trading_days if trading_days > 0 else 0

        # 시그널 통계
        total_signals = len(signals)
        taken = [s for s in signals if s.get("action_taken")]
        blocked = [s for s in signals if not s.get("action_taken")]
        entry_rate = len(taken) / total_signals if total_signals > 0 else 0

        # 차단 원인 분포
        block_reasons: dict[str, int] = {}
        for s in blocked:
            reason = s.get("reason_not_taken", "UNKNOWN")
            block_reasons[reason] = block_reasons.get(reason, 0) + 1

        # 실체결 수익 통계
        sell_fills = [f for f in fills if f.get("side") == "sell" and f.get("realized_pnl") is not None]
        pnls = [float(f.get("realized_pnl", 0)) for f in sell_fills]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / len(pnls) if pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf") if wins else 0

        # 스윕 유형별 성과 (signal에 sweep_type 있는 경우)
        sweep_stats: dict[str, dict] = {}
        for s in taken:
            payload = s.get("payload", {}) if isinstance(s.get("payload"), dict) else {}
            sweep_type = (payload.get("details", {}).get("trigger", {}).get("sweep_type", "UNKNOWN"))
            if sweep_type not in sweep_stats:
                sweep_stats[sweep_type] = {"count": 0}
            sweep_stats[sweep_type]["count"] += 1

        # 시간대별 진입 분포
        hour_dist: dict[int, int] = {}
        for s in taken:
            created = s.get("created_at", "")
            if created and len(created) >= 13:
                try:
                    # UTC → KST (+9)
                    hour_utc = int(created[11:13])
                    hour_kst = (hour_utc + 9) % 24
                    hour_dist[hour_kst] = hour_dist.get(hour_kst, 0) + 1
                except ValueError:
                    pass

        return {
            "trading_days": trading_days,
            "total_pnl": total_pnl,
            "avg_daily_pnl": avg_daily_pnl,
            "total_signals": total_signals,
            "signals_taken": len(taken),
            "signals_blocked": len(blocked),
            "entry_rate": entry_rate,
            "block_reasons": block_reasons,
            "fills_count": len(pnls),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": min(profit_factor, 99.0),
            "sweep_stats": sweep_stats,
            "hour_distribution": hour_dist,
        }

    # ── LLM 분석 ──────────────────────────────────────────────

    def build_review_prompt(self, data: dict, stats: dict) -> str:
        # 일별 손익 요약
        daily_summary = "\n".join([
            f"  {r.get('report_date')}: {float(r.get('daily_pnl',0) or 0):+,.0f}원 "
            f"(거래 {r.get('trades_count',0)}건, 승률 {float(r.get('win_rate',0) or 0):.0%})"
            for r in data["daily_reports"]
        ]) or "  없음"

        # 차단 원인 정렬
        block_sorted = sorted(stats["block_reasons"].items(), key=lambda x: x[1], reverse=True)
        block_summary = "\n".join([f"  {reason}: {count}회" for reason, count in block_sorted[:8]]) or "  없음"

        # 시간대 분포
        hour_sorted = sorted(stats["hour_distribution"].items())
        hour_summary = ", ".join([f"{h}시:{c}건" for h, c in hour_sorted]) or "없음"

        return f"""=== 전략 리뷰 데이터 ({data['start_date']} ~ {data['end_date']}, {data['period_weeks']}주) ===

【기간 통계】
  거래일: {stats['trading_days']}일
  누적 손익: {stats['total_pnl']:+,.0f}원 (일평균 {stats['avg_daily_pnl']:+,.0f}원)
  총 시그널: {stats['total_signals']}개 (진입 {stats['signals_taken']}개, 차단 {stats['signals_blocked']}개)
  진입율: {stats['entry_rate']:.1%}
  체결 건수: {stats['fills_count']}건
  승률: {stats['win_rate']:.1%}
  평균 수익: {stats['avg_win']:+,.0f}원 / 평균 손실: {stats['avg_loss']:+,.0f}원
  Profit Factor: {stats['profit_factor']:.2f}

【일별 손익】
{daily_summary}

【진입 차단 원인 (상위)】
{block_summary}

【진입 시간대 분포 (KST)】
  {hour_summary}

【스윕 유형별 진입 수】
{chr(10).join([f"  {k}: {v['count']}건" for k, v in stats['sweep_stats'].items()]) or '  데이터 없음'}

=== 분석 요청 ===
위 {data['period_weeks']}주 데이터를 기반으로 아래 JSON 형식의 전략 리뷰를 작성하세요.
파라미터 변경 제안은 구체적인 수치로, 근거는 데이터 기반으로 작성하세요.

{{
  "performance_summary": "기간 전체 성과 평가 (3-5문장, 수치 기반)",
  "strengths": [
    {{"finding": "잘 작동하는 부분", "evidence": "근거 데이터"}}
  ],
  "weaknesses": [
    {{"finding": "개선이 필요한 부분", "evidence": "근거 데이터", "severity": "high/medium/low"}}
  ],
  "pattern_insights": [
    "발견된 패턴 1 (예: '09:15~09:30 진입 성공률이 09:30 이후보다 높음')",
    "발견된 패턴 2"
  ],
  "parameter_suggestions": [
    {{
      "parameter": "변경 제안 파라미터명",
      "current": "현재 값",
      "suggested": "제안 값",
      "rationale": "근거 (데이터 기반)",
      "priority": "즉시/다음주/장기"
    }}
  ],
  "entry_quality_assessment": "진입 조건(Sweep/VWAP/MSS) 각각의 품질 평가",
  "exit_quality_assessment": "청산 관리(TP/손절/강제청산) 품질 평가",
  "next_week_focus": ["다음 주 집중할 개선 사항 1", "개선 사항 2"],
  "telegram_summary": "Telegram 주간 리뷰 요약 (이모지 포함, 6-8줄)"
}}"""

    def analyze(self, data: dict, stats: dict) -> dict[str, Any]:
        if stats["trading_days"] == 0:
            return {"performance_summary": "거래 데이터 없음", "parameter_suggestions": [],
                    "telegram_summary": "📊 분석할 거래 데이터가 없습니다."}

        if not self.llm.available:
            return self._fallback_review(stats)

        try:
            prompt = self.build_review_prompt(data, stats)
            result, usage = self.llm.complete_json(SYSTEM_PROMPT, prompt, max_tokens=3000)
            logger.info("StrategyReviewerAgent LLM 완료 — 토큰: %s", usage)
            return result
        except Exception:
            logger.exception("LLM 분석 실패")
            return self._fallback_review(stats)

    def _fallback_review(self, stats: dict) -> dict[str, Any]:
        pf = stats.get("profit_factor", 0)
        wr = stats.get("win_rate", 0)
        status = "양호" if pf > 1.5 and wr > 0.5 else "개선 필요"

        tg = (
            f"📊 <b>주간 전략 리뷰</b>\n"
            f"기간: {stats.get('trading_days', 0)}거래일\n"
            f"누적: {stats.get('total_pnl', 0):+,.0f}원\n"
            f"승률: {wr:.0%} / PF: {pf:.2f}\n"
            f"시그널 {stats.get('total_signals', 0)}개 → 진입 {stats.get('signals_taken', 0)}개\n"
            f"상태: {status}\n"
            f"(LLM 상세 분석: GEMINI_API_KEY 설정 필요)"
        )
        return {
            "performance_summary": f"{stats.get('trading_days', 0)}거래일, 승률 {wr:.0%}, PF {pf:.2f}",
            "strengths": [],
            "weaknesses": [],
            "pattern_insights": [],
            "parameter_suggestions": [],
            "entry_quality_assessment": "LLM 미설정",
            "exit_quality_assessment": "LLM 미설정",
            "next_week_focus": [],
            "telegram_summary": tg,
        }

    # ── Notion 저장 ───────────────────────────────────────────

    def save_to_notion(self, data: dict, stats: dict, review: dict) -> None:
        if not self.notion_token or not self.notion_daily_db:
            return
        headers = {
            "Authorization": f"Bearer {self.notion_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        title = f"📈 주간 전략 리뷰 {data['start_date']} ~ {data['end_date']}"
        properties = {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": data["end_date"]}},
            "Workflow": {"select": {"name": "strategy-review"}},
            "Status": {"select": {"name": "DONE"}},
        }
        if stats.get("total_pnl") is not None:
            properties["Daily PnL"] = {"number": round(stats["total_pnl"], 2)}
        if stats.get("win_rate") is not None:
            properties["Win Rate"] = {"number": round(stats["win_rate"], 4)}

        # 본문 블록
        children = []
        summary = review.get("performance_summary", "")
        if summary:
            children.append({"object": "block", "type": "callout",
                             "callout": {"icon": {"emoji": "🤖"},
                                         "rich_text": [{"type": "text", "text": {"content": f"AI 분석 요약\n{summary}"[:2000]}}]}})

        # 파라미터 제안
        suggestions = review.get("parameter_suggestions", [])
        if suggestions:
            children.append({"object": "block", "type": "heading_2",
                             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "파라미터 조정 제안"}}]}})
            for s in suggestions[:5]:
                text = (f"[{s.get('priority', '?')}] {s.get('parameter')}: "
                        f"{s.get('current')} → {s.get('suggested')}\n근거: {s.get('rationale', '')}")
                children.append({"object": "block", "type": "to_do",
                                 "to_do": {"checked": False,
                                            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}})

        # 다음 주 집중 사항
        focus = review.get("next_week_focus", [])
        if focus:
            children.append({"object": "block", "type": "heading_2",
                             "heading_2": {"rich_text": [{"type": "text", "text": {"content": "다음 주 집중 사항"}}]}})
            for f in focus[:5]:
                children.append({"object": "block", "type": "bulleted_list_item",
                                 "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": str(f)[:2000]}}]}})

        try:
            resp = requests.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json={"parent": {"database_id": self.notion_daily_db},
                      "properties": properties, "children": children[:80]},
                timeout=20,
            )
            if resp.ok:
                logger.info("Notion 전략 리뷰 저장 완료")
            else:
                logger.warning("Notion 저장 실패: %s", resp.text[:100])
        except Exception:
            logger.exception("Notion 저장 오류")

    # ── Telegram 전송 ─────────────────────────────────────────

    def send_telegram(self, review: dict) -> None:
        if not self.telegram_token or not self.telegram_chat:
            return
        msg = review.get("telegram_summary", "")
        if not msg:
            return
        try:
            chat_id = int(self.telegram_chat) if self.telegram_chat.lstrip('-').isdigit() else self.telegram_chat
            requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
            logger.info("Telegram 주간 리뷰 전송 완료")
        except Exception:
            logger.exception("Telegram 전송 오류")

    # ── 메인 실행 ─────────────────────────────────────────────

    def run(self, weeks: int = 2) -> dict[str, Any]:
        logger.info("=== Strategy Reviewer Agent 시작 (최근 %d주) ===", weeks)

        # 1. 데이터 수집
        data = self.fetch_period_data(weeks)

        if not data["daily_reports"] and not data["fills"]:
            logger.warning("분석할 거래 데이터 없음")
            return {"status": "no_data", "weeks": weeks}

        # 2. 통계 계산
        stats = self.calculate_stats(data)
        logger.info("통계: %d거래일, 승률 %.0%%, PF %.2f",
                    stats["trading_days"], stats["win_rate"], stats["profit_factor"])

        # 3. LLM 분석
        review = self.analyze(data, stats)

        # 4. Notion 저장
        self.save_to_notion(data, stats, review)

        # 5. Telegram 전송
        self.send_telegram(review)

        # 6. agent_runs 기록
        self._sb_upsert("agent_runs", {
            "agent_name": "strategy_reviewer_agent",
            "trade_date": datetime.now(KST).date().isoformat(),
            "input_payload": {"weeks": weeks, "trading_days": stats["trading_days"]},
            "output_payload": {
                "summary": review.get("performance_summary", "")[:300],
                "suggestions_count": len(review.get("parameter_suggestions", [])),
                "win_rate": stats["win_rate"],
                "profit_factor": stats["profit_factor"],
            },
            "status": "SUCCESS",
            "llm_model": self.llm.model,
        }, on_conflict="agent_name,trade_date")

        logger.info("=== Strategy Reviewer Agent 완료 ===")
        return {
            "status": "success",
            "weeks": weeks,
            "trading_days": stats["trading_days"],
            "stats": stats,
            "review_summary": review.get("performance_summary", ""),
            "suggestions": review.get("parameter_suggestions", []),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy Reviewer Agent")
    parser.add_argument("--weeks", type=int, default=2, help="분석 기간 (주, 기본: 2)")
    args = parser.parse_args()

    agent = StrategyReviewerAgent()
    result = agent.run(weeks=args.weeks)

    if result.get("status") == "success":
        print(f"\n✅ 전략 리뷰 완료 (최근 {result['weeks']}주, {result['trading_days']}거래일)")
        print(f"\n📋 요약:\n{result.get('review_summary', '')}")
        suggestions = result.get("suggestions", [])
        if suggestions:
            print(f"\n💡 파라미터 제안 ({len(suggestions)}개):")
            for s in suggestions:
                print(f"  [{s.get('priority')}] {s.get('parameter')}: {s.get('current')} → {s.get('suggested')}")
                print(f"    근거: {s.get('rationale', '')[:80]}")
    elif result.get("status") == "no_data":
        print(f"⚠️  분석할 거래 데이터가 없습니다 (최근 {result['weeks']}주)")


if __name__ == "__main__":
    main()
