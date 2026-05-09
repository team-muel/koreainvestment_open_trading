"""Risk Review Agent — 주문 전 리스크 설명 자동 생성.

⚠️ 핵심 원칙:
   이 에이전트는 주문을 허용하거나 차단하지 않습니다.
   RiskManager 코드(ict_engine.py)가 이미 결정을 내린 후,
   그 결정의 이유를 사람이 읽기 쉽게 설명하는 역할만 합니다.

입력: 트레이드 플랜 + 현재 엔진 상태 + 일일 리스크 현황
출력:
  - 리스크 체크리스트 (통과/차단 항목)
  - 결론 (규칙상 가능/불가 + 시장 컨텍스트)
  - Telegram 알림용 요약

사용법:
    python -m strategy_builder.agents.risk_review_agent --help

    # 코드에서 호출 (진입 시그널 발생 시)
    from agents.risk_review_agent import RiskReviewAgent
    agent = RiskReviewAgent()
    result = agent.review(plan_dict, engine_status_dict)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, root)

from dotenv import load_dotenv
load_dotenv(os.path.join(root, "deploy", ".env"))

from strategy_builder.agents.llm_client import LLMClient

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 ICT 데이트레이딩 리스크 관리 전문가입니다.
자동매매 시스템이 진입 시그널을 포착했을 때, 현재 상황이 규칙상 허용 가능한지
체계적으로 분석하고 트레이더가 즉시 이해할 수 있는 리스크 리뷰 보고서를 작성합니다.

작성 원칙:
- 모든 판단은 제공된 데이터에 근거해야 합니다
- "괜찮아 보인다" 같은 막연한 표현 금지
- 구체적인 수치와 규칙 기준을 명시
- 주문 가능 여부는 코드가 이미 결정했으므로 그 근거만 설명
- 시장 컨텍스트(지수 방향, 당일 누적 성과)와 연결하여 해석

절대 하지 말 것:
- 직접적인 주문 실행 지시
- 포지션 크기 변경 제안
- 손절/목표가 임의 수정 제안"""


class RiskReviewAgent:
    """진입 시그널에 대한 리스크 리뷰 생성."""

    def __init__(self):
        self.llm = LLMClient()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
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
                params=params or {}, timeout=10,
            )
            return resp.json() if resp.ok else []
        except Exception:
            return []

    def _sb_insert(self, table: str, data: dict) -> bool:
        if not self.supabase_url:
            return False
        try:
            resp = requests.post(
                f"{self.supabase_url}/rest/v1/{table}",
                headers={"apikey": self.supabase_key, "Authorization": f"Bearer {self.supabase_key}",
                         "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=[data], timeout=10,
            )
            return resp.ok
        except Exception:
            return False

    # ── 체크리스트 계산 (코드 기반, LLM 불필요) ──────────────

    def build_checklist(self, plan: dict, engine_status: dict) -> dict[str, Any]:
        """규칙 기반 체크리스트 생성 (주문 가능 여부는 이미 엔진이 결정함)."""
        daily_risk = engine_status.get("daily_risk", {})
        entries = int(daily_risk.get("entries", 0))
        max_entries = int(daily_risk.get("max_entries", 2))
        daily_loss = float(daily_risk.get("loss", 0))
        daily_realized = float(daily_risk.get("realized", 0))
        loss_limit_reached = bool(daily_risk.get("loss_limit_reached", False))
        profit_target_reached = bool(daily_risk.get("profit_target_reached", False))
        loss_limit_pct = float(daily_risk.get("loss_limit_pct", 0.005))
        profit_target_pct = float(daily_risk.get("profit_target_pct", 0.005))

        entry = float(plan.get("entry", 0))
        stop = float(plan.get("stop", 0))
        take_profit = float(plan.get("take_profit", 0))
        quantity = int(plan.get("quantity", 0))
        symbol = plan.get("symbol", "")

        risk_pct = (entry - stop) / entry if entry > 0 and stop > 0 else 0
        rr = (take_profit - entry) / (entry - stop) if entry > stop > 0 and take_profit > entry else 0
        risk_amount = (entry - stop) * quantity if quantity > 0 else 0

        checks = {
            "진입 횟수 여유": {
                "pass": entries < max_entries,
                "detail": f"{entries}/{max_entries}회 사용",
                "rule": f"일일 최대 {max_entries}회",
            },
            "일일 손실 한도": {
                "pass": not loss_limit_reached,
                "detail": f"누적 손실 {daily_loss:+,.0f}원 (한도 {loss_limit_pct:.1%})",
                "rule": f"일일 손실 -{loss_limit_pct:.1%} 이하 시 중단",
            },
            "일일 목표수익 미달성": {
                "pass": not profit_target_reached,
                "detail": f"누적 수익 {daily_realized:+,.0f}원 (목표 {profit_target_pct:.1%})",
                "rule": f"목표수익 +{profit_target_pct:.1%} 달성 시 신규 진입 중단",
            },
            "손절폭 1% 이내": {
                "pass": 0 < risk_pct <= 0.01,
                "detail": f"손절폭 {risk_pct:.2%} (진입 {entry:,.0f} → 손절 {stop:,.0f})",
                "rule": "손절폭 > 1% 진입 거부",
            },
            "R:R 1.5 이상": {
                "pass": rr >= 1.5,
                "detail": f"R:R {rr:.2f} (목표 {take_profit:,.0f}원)",
                "rule": "R:R < 1.5 진입 거부",
            },
            "최대 포지션 1개": {
                "pass": len(engine_status.get("positions", [])) < 1,
                "detail": f"현재 포지션 {len(engine_status.get('positions', []))}개",
                "rule": "동시 포지션 최대 1개",
            },
        }

        all_pass = all(c["pass"] for c in checks.values())

        return {
            "symbol": symbol,
            "entry": entry,
            "stop": stop,
            "take_profit": take_profit,
            "quantity": quantity,
            "risk_pct": risk_pct,
            "rr": rr,
            "risk_amount": risk_amount,
            "daily_entries": entries,
            "max_entries": max_entries,
            "daily_loss": daily_loss,
            "daily_realized": daily_realized,
            "checks": checks,
            "all_pass": all_pass,
            "engine_state": engine_status.get("engine_state", "UNKNOWN"),
            "degraded": engine_status.get("degraded", False),
        }

    # ── LLM 설명 생성 ─────────────────────────────────────────

    def build_review_prompt(self, checklist: dict, signal_detail: dict | None = None) -> str:
        checks = checklist["checks"]
        pass_items = [k for k, v in checks.items() if v["pass"]]
        fail_items = [k for k, v in checks.items() if not v["pass"]]

        checks_text = "\n".join([
            f"  {'✅' if v['pass'] else '❌'} {k}: {v['detail']} (규칙: {v['rule']})"
            for k, v in checks.items()
        ])

        signal_text = ""
        if signal_detail:
            signal_text = f"""
【시그널 상세】
  - Sweep 유형: {signal_detail.get('sweep_type', 'N/A')}
  - Sweep 깊이: {signal_detail.get('sweep_depth_pct', 'N/A')}
  - VWAP 리클레임: {'확인' if signal_detail.get('vwap_reclaim_confirmed') else '미확인'}
  - MSS 확인: {'확인' if signal_detail.get('mss_confirmed') else '미확인'}
  - 거래량 배율: {signal_detail.get('volume_ratio', 'N/A')}x
  - 5분봉 VWAP 위 마감: {'확인' if signal_detail.get('five_min_vwap_close_confirmed') else '미확인'}"""

        return f"""=== 리스크 리뷰 요청 ===

【진입 계획】
  종목: {checklist['symbol']}
  진입가: {checklist['entry']:,.0f}원
  손절가: {checklist['stop']:,.0f}원 (폭: {checklist['risk_pct']:.2%})
  목표가: {checklist['take_profit']:,.0f}원
  수량: {checklist['quantity']:,}주
  R:R: {checklist['rr']:.2f}
  리스크 금액: {checklist['risk_amount']:,.0f}원

【일일 현황】
  진입 횟수: {checklist['daily_entries']}/{checklist['max_entries']}회
  누적 손익: {checklist['daily_realized']:+,.0f}원 (손실: {checklist['daily_loss']:+,.0f}원)
  엔진 상태: {checklist['engine_state']} {'⚠️ DEGRADED' if checklist['degraded'] else ''}
{signal_text}

【규칙 체크리스트】
{checks_text}

【전체 결과】: {'✅ 모든 규칙 통과' if checklist['all_pass'] else '❌ 차단된 규칙 있음'}

=== 분석 요청 ===
위 데이터를 기반으로 아래 JSON 형식의 리스크 리뷰를 작성해주세요.
실제 주문 결정은 코드가 이미 내렸으므로, 그 근거와 맥락만 설명하세요.

{{
  "conclusion": "규칙상 [가능/불가] — 한 문장 결론",
  "key_risk_factors": ["주요 리스크 요인 1", "주요 리스크 요인 2"],
  "strengths": ["이 진입의 강점 1", "강점 2"],
  "market_context": "현재 시장 상황과 이 진입의 관계 (2-3문장)",
  "caution": "주의사항 또는 모니터링 포인트 (없으면 null)",
  "telegram_summary": "Telegram 알림용 요약 (이모지 포함, 4-6줄)"
}}"""

    def generate_review(self, checklist: dict, signal_detail: dict | None = None) -> dict[str, Any]:
        """LLM으로 리스크 리뷰 생성."""
        if not self.llm.available:
            return self._fallback_review(checklist)

        try:
            prompt = self.build_review_prompt(checklist, signal_detail)
            result, usage = self.llm.complete_json(SYSTEM_PROMPT, prompt, max_tokens=1500)
            logger.debug("RiskReviewAgent LLM 완료 — 토큰: %s", usage)
            return result
        except Exception:
            logger.exception("LLM 리스크 리뷰 실패 — fallback 사용")
            return self._fallback_review(checklist)

    def _fallback_review(self, checklist: dict) -> dict[str, Any]:
        """LLM 없을 때 자동 생성."""
        pass_count = sum(1 for v in checklist["checks"].values() if v["pass"])
        total = len(checklist["checks"])
        fail_items = [k for k, v in checklist["checks"].items() if not v["pass"]]

        if checklist["all_pass"]:
            conclusion = f"규칙상 가능 — {total}/{total}개 규칙 통과"
        else:
            conclusion = f"규칙상 불가 — 차단: {', '.join(fail_items)}"

        emoji = "✅" if checklist["all_pass"] else "🚫"
        tg_summary = (
            f"{emoji} <b>리스크 리뷰</b>\n"
            f"종목: {checklist['symbol']}\n"
            f"손절폭: {checklist['risk_pct']:.2%} / R:R: {checklist['rr']:.2f}\n"
            f"규칙: {pass_count}/{total}개 통과\n"
        )
        if not checklist["all_pass"]:
            tg_summary += f"차단 사유: {', '.join(fail_items)}"

        return {
            "conclusion": conclusion,
            "key_risk_factors": [v["detail"] for k, v in checklist["checks"].items() if not v["pass"]],
            "strengths": [v["detail"] for k, v in checklist["checks"].items() if v["pass"]],
            "market_context": "LLM 미설정으로 시장 분석 생략",
            "caution": None,
            "telegram_summary": tg_summary,
        }

    # ── Telegram 알림 ─────────────────────────────────────────

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
        except Exception:
            logger.exception("RiskReviewAgent Telegram 전송 실패")

    # ── 메인 API ──────────────────────────────────────────────

    def review(
        self,
        plan: dict[str, Any],
        engine_status: dict[str, Any],
        signal_detail: dict[str, Any] | None = None,
        send_telegram: bool = True,
    ) -> dict[str, Any]:
        """리스크 리뷰 실행.

        Args:
            plan: TradePlan 딕셔너리 (entry, stop, take_profit, quantity, symbol, reason)
            engine_status: ict_engine.status() 반환값
            signal_detail: ICT 시그널 상세 (선택, trigger 딕셔너리)
            send_telegram: 텔레그램 알림 전송 여부

        Returns:
            {"checklist": ..., "review": ..., "all_pass": bool}
        """
        symbol = plan.get("symbol", "")
        logger.info("RiskReviewAgent: %s 리스크 리뷰 시작", symbol)

        # 1. 규칙 기반 체크리스트 (빠름, LLM 불필요)
        checklist = self.build_checklist(plan, engine_status)

        # 2. LLM 설명 생성
        review = self.generate_review(checklist, signal_detail)

        # 3. Supabase 기록
        self._sb_insert("agent_runs", {
            "agent_name": "risk_review_agent",
            "trade_date": datetime.now(KST).date().isoformat(),
            "input_payload": {
                "symbol": symbol,
                "entry": plan.get("entry"),
                "stop": plan.get("stop"),
                "take_profit": plan.get("take_profit"),
                "quantity": plan.get("quantity"),
                "risk_pct": checklist["risk_pct"],
                "rr": checklist["rr"],
            },
            "output_payload": {
                "all_pass": checklist["all_pass"],
                "conclusion": review.get("conclusion", ""),
                "fail_checks": [k for k, v in checklist["checks"].items() if not v["pass"]],
            },
            "status": "SUCCESS",
            "llm_model": self.llm.model,
        })

        # 4. Telegram 알림 (주문 진입 시에만)
        if send_telegram and checklist["all_pass"]:
            self.send_telegram(review)

        result = {
            "symbol": symbol,
            "all_pass": checklist["all_pass"],
            "checklist": checklist,
            "review": review,
            "conclusion": review.get("conclusion", ""),
        }

        logger.info(
            "RiskReviewAgent 완료: %s — %s",
            symbol,
            "통과" if checklist["all_pass"] else "차단",
        )
        return result

    def format_for_log(self, result: dict) -> str:
        """엔진 로그용 포맷."""
        checklist = result.get("checklist", {})
        review = result.get("review", {})
        fail_items = [k for k, v in checklist.get("checks", {}).items() if not v["pass"]]

        lines = [
            f"[RiskReview] {result.get('symbol')} — {result.get('conclusion', '')}",
            f"  손절폭 {checklist.get('risk_pct', 0):.2%} / R:R {checklist.get('rr', 0):.2f}",
        ]
        if fail_items:
            lines.append(f"  ❌ 차단: {', '.join(fail_items)}")
        if review.get("caution"):
            lines.append(f"  ⚠️  {review['caution']}")
        return "\n".join(lines)
