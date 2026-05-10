"""Pre-market Reason Agent — 장전 스캔 결과를 한국어 근거로 자동 작성.

유니버스 스캐너가 생성한 ICT 점수(숫자)를 사람이 읽기 좋은
한국어 선정 근거 문장으로 변환하고, Notion ICT Pre-market Scan DB를 업데이트한다.

⚠️ 권한:
   ✅ Supabase premarket_scan / watchlists 읽기
   ✅ Notion ICT Pre-market Scan DB 쓰기
   ✅ Google Calendar 장전 이벤트 업데이트
   ✅ Telegram 장전 브리핑 전송
   🚫 KIS 주문 API 직접 호출 금지

사용법:
    python -m strategy_builder.agents.premarket_agent
    python -m strategy_builder.agents.premarket_agent --date 2026-05-09
"""
from __future__ import annotations

import argparse
import json
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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 ICT(Inner Circle Trader) Liquidity Reclaim 전략 전문가입니다.
장전 스캔 데이터를 보고 각 종목이 왜 오늘 watchlist에 선정되었는지
트레이더가 즉시 이해할 수 있도록 간결하고 구체적인 한국어 문장으로 설명해주세요.

작성 원칙:
- 각 종목당 2~4문장 이내로 핵심만 작성
- 구체적인 수치(거리%, 배수, 점수 등)를 반드시 포함
- ICT 용어를 자연스럽게 사용 (Sell-side liquidity, OR sweep, VWAP reclaim 등)
- 진입 시나리오를 명확히 서술 (언제, 어떤 조건에서)
- A급/B급 등 등급에 따라 문체 강도 조절
- 경고 사항이 있으면 반드시 언급

절대 하지 말 것:
- 주문 추천 또는 매매 시점 지정
- 근거 없는 수익 예측
- 종목에 대한 과장된 표현"""


class PremarketAgent:
    """장전 스캔 근거 자동 작성 에이전트."""

    def __init__(self):
        self.llm = LLMClient()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
        self.notion_token = os.environ.get("NOTION_TOKEN", "").strip()
        self.notion_premarket_db = os.environ.get("NOTION_PREMARKET_DB_ID", "").strip()
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
                params=params or {},
                timeout=15,
            )
            return resp.json() if resp.ok else []
        except Exception:
            logger.exception("Supabase GET %s 실패", table)
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

    def fetch_today_watchlist(self, trade_date: str) -> list[dict]:
        """오늘 watchlist 종목 수집 (점수 포함)."""
        candidates = self._sb_get("premarket_scan", {
            "scan_date": f"eq.{trade_date}",
            "order": "priority.desc",
            "limit": "30",
        })
        logger.info("장전 스캔 결과: %d개 종목", len(candidates))
        return candidates

    # ── LLM 분석 ──────────────────────────────────────────────

    def build_analysis_prompt(self, candidates: list[dict]) -> str:
        """종목 데이터를 LLM 프롬프트로 변환."""
        items = []
        for c in candidates[:15]:  # 최대 15개 분석
            score = c.get("payload", {}).get("ict_score", {}) if isinstance(c.get("payload"), dict) else {}
            items.append(f"""
종목: {c.get('ticker')} ({c.get('name', '')}) [{c.get('market', '')}]
ICT 점수: {score.get('total', c.get('ict_score_total', 'N/A'))}점 [{score.get('grade', '')}]
전일 저점 거리: {score.get('details', {}).get('prev_low_dist_pct', 'N/A')}
전일 고점 거리: {score.get('details', {}).get('prev_high_dist_pct', 'N/A')}
상대거래량: {score.get('details', {}).get('relative_volume', 'N/A')}x
평균거래대금: {score.get('details', {}).get('avg_trading_value_bn', 'N/A')}억
전일 등락률: {c.get('prev_change_pct', 'N/A')}
선정 사유(자동): {score.get('selection_reason_ko', c.get('scan_reason', 'N/A'))}
경고: {', '.join(score.get('warnings_ko', [])) or '없음'}
""")

        prompt = f"""=== {candidates[0].get('scan_date', '') if candidates else ''} 장전 스캔 결과 ({len(candidates)}개 종목) ===

{chr(10).join(items)}

=== 요청 ===
위 종목들 각각에 대해 아래 JSON 형식으로 선정 근거를 작성해주세요.
watchlist 전체 요약도 포함하세요.

{{
  "watchlist_summary": "오늘 장전 스캔 전체 요약 (시장 컨텍스트 포함, 3-5문장)",
  "market_theme": "오늘 watchlist의 공통 테마 또는 섹터 특성",
  "top_pick": "{{ticker}}: 최우선 관심 종목과 이유",
  "candidates": [
    {{
      "ticker": "005930",
      "grade": "A+",
      "reason_ko": "삼성전자는 전일 저점(XX,XXX원) 1.4% 상단에 위치해 장초반 sell-side liquidity sweep 후 VWAP 리클레임 시 매수 구조가 형성될 수 있습니다. 상대거래량 5.2배로 수급이 강하고, 전일 거래대금 720억으로 체결 안정성이 양호합니다. 전일 고점까지 약 3.1%의 여유가 있어 reclaim 성공 시 buy-side liquidity target으로 설정 가능합니다.",
      "entry_scenario": "09:15 이후 OR Low 또는 전일 저점 스윕 → VWAP 리클레임 확인 후 진입 고려",
      "risk_note": "경고 사항이 있으면 작성, 없으면 null",
      "watch_level": "장중 주목해야 할 가격 레벨 (예: 전일 저점 XX,XXX원)"
    }}
  ],
  "telegram_briefing": "Telegram 장전 브리핑 (이모지 포함, 5-8줄, watchlist 핵심 요약)"
}}
"""
        return prompt

    def analyze(self, candidates: list[dict]) -> dict[str, Any]:
        """LLM으로 장전 분석 실행."""
        if not candidates:
            return {"watchlist_summary": "오늘 스캔 결과 없음", "candidates": [], "telegram_briefing": ""}

        if not self.llm.available:
            return self._fallback_analysis(candidates)

        prompt = self.build_analysis_prompt(candidates)
        logger.info("LLM 분석 중 (종목 %d개, 모델: %s)...", len(candidates), self.llm.model)

        try:
            result, usage = self.llm.complete_json(
                SYSTEM_PROMPT,
                prompt,
                max_tokens=3000,
                required_keys=["watchlist_summary", "candidates", "telegram_briefing"],
                fallback=self._fallback_analysis(candidates),
            )
            logger.info("LLM 완료 — 토큰: 입력 %d / 출력 %d",
                        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return result
        except Exception:
            logger.exception("LLM 분석 실패 — fallback 사용")
            return self._fallback_analysis(candidates)

    def _fallback_analysis(self, candidates: list[dict]) -> dict[str, Any]:
        """LLM 없을 때 자동 생성 근거 사용."""
        fallback_candidates = []
        for c in candidates[:10]:
            score = c.get("payload", {}).get("ict_score", {}) if isinstance(c.get("payload"), dict) else {}
            fallback_candidates.append({
                "ticker": c.get("ticker", ""),
                "grade": score.get("grade", "?"),
                "reason_ko": score.get("selection_reason_ko", c.get("scan_reason", "자동 선정")),
                "entry_scenario": "09:15 이후 OR Low 스윕 + VWAP 리클레임 확인 후 진입",
                "risk_note": ", ".join(score.get("warnings_ko", [])) or None,
                "watch_level": f"전일 저점 {c.get('invalidation_level', 'N/A')}원",
            })

        tickers = [c.get("ticker", "") for c in candidates[:5]]
        return {
            "watchlist_summary": f"오늘 {len(candidates)}개 종목 스캔 완료. 상위 후보: {', '.join(tickers)}",
            "market_theme": "ICT Liquidity Reclaim 전략 기반 선정",
            "top_pick": candidates[0].get("ticker", "") + ": ICT 점수 최고 종목" if candidates else "",
            "candidates": fallback_candidates,
            "telegram_briefing": (
                f"📊 <b>장전 스캔 완료</b>\n"
                f"선정 종목: {len(candidates)}개\n"
                f"상위 후보: {', '.join(tickers)}\n"
                f"LLM 분석: 미설정 (GEMINI_API_KEY 필요)"
            ),
        }

    # ── Notion 업데이트 ───────────────────────────────────────

    def update_notion(self, trade_date: str, candidates: list[dict], analysis: dict) -> None:
        if not self.notion_token or not self.notion_premarket_db:
            return

        headers = {
            "Authorization": f"Bearer {self.notion_token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        analysis_by_ticker = {c["ticker"]: c for c in analysis.get("candidates", [])}

        for item in candidates[:15]:
            ticker = item.get("ticker", "")
            name = item.get("name", "")
            score_data = item.get("payload", {}).get("ict_score", {}) if isinstance(item.get("payload"), dict) else {}
            grade = score_data.get("grade", "?")
            ai_info = analysis_by_ticker.get(ticker, {})

            priority_emoji = {"A+": "🔥", "A": "⭐", "B": "👀", "C": "📌", "D": "🔍"}.get(grade, "📊")
            title = f"{priority_emoji} [{grade}] {ticker} {name}"

            reason = ai_info.get("reason_ko", score_data.get("selection_reason_ko", item.get("scan_reason", "")))
            entry_scenario = ai_info.get("entry_scenario", "09:15 이후 OR Low 스윕 + VWAP 리클레임 확인")
            risk_note = ai_info.get("risk_note") or ""
            watch_level = ai_info.get("watch_level", "")

            properties = {
                "Name": {"title": [{"text": {"content": title[:200]}}]},
                "Date": {"date": {"start": trade_date}},
                "Ticker": {"rich_text": [{"text": {"content": ticker}}]},
                "Status": {"select": {"name": "WATCHLIST"}},
            }

            # 수치 필드
            for notion_key, data_key in [
                ("Priority", "priority"),
            ]:
                val = item.get(data_key)
                if val is not None:
                    properties[notion_key] = {"number": int(val)}

            if item.get("prev_change_pct") is not None:
                properties["PrevChangePct"] = {"number": round(float(item["prev_change_pct"]) * 100, 2)}

            if score_data.get("total"):
                properties["ICTScore"] = {"number": round(float(score_data["total"]), 1)}

            # 텍스트 필드
            if item.get("liquidity_level"):
                properties["LiquidityLevel"] = {"select": {"name": item["liquidity_level"]}}
            if reason:
                properties["ScanReason"] = {"rich_text": [{"text": {"content": reason[:2000]}}]}

            # 블록 내용 (AI 분석)
            children = []
            if reason:
                children.append({
                    "object": "block", "type": "callout",
                    "callout": {
                        "icon": {"emoji": "🤖"},
                        "rich_text": [{"type": "text", "text": {"content": f"AI 선정 근거\n{reason}"[:2000]}}],
                    }
                })
            if entry_scenario:
                children.append({
                    "object": "block", "type": "callout",
                    "callout": {
                        "icon": {"emoji": "📍"},
                        "rich_text": [{"type": "text", "text": {"content": f"진입 시나리오\n{entry_scenario}"[:2000]}}],
                    }
                })
            if risk_note:
                children.append({
                    "object": "block", "type": "callout",
                    "callout": {
                        "icon": {"emoji": "⚠️"},
                        "rich_text": [{"type": "text", "text": {"content": f"주의사항\n{risk_note}"[:2000]}}],
                    }
                })
            if watch_level:
                children.append({
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {
                        "content": f"👁 주목 레벨: {watch_level}"
                    }}]},
                })

            # 기존 페이지 검색 후 upsert
            try:
                search = requests.post(
                    f"https://api.notion.com/v1/databases/{self.notion_premarket_db}/query",
                    headers=headers,
                    json={"filter": {"and": [
                        {"property": "Date", "date": {"equals": trade_date}},
                        {"property": "Ticker", "rich_text": {"equals": ticker}},
                    ]}, "page_size": 1},
                    timeout=15,
                )
                existing = search.json().get("results", []) if search.ok else []

                if existing:
                    page_id = existing[0]["id"]
                    requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                                   headers=headers, json={"properties": properties}, timeout=15)
                    if children:
                        requests.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                                       headers=headers, json={"children": children}, timeout=15)
                    logger.debug("Notion 업데이트: %s", ticker)
                else:
                    body = {"parent": {"database_id": self.notion_premarket_db},
                            "properties": properties}
                    if children:
                        body["children"] = children
                    resp = requests.post("https://api.notion.com/v1/pages",
                                         headers=headers, json=body, timeout=15)
                    if resp.ok:
                        logger.debug("Notion 생성: %s", ticker)
                    else:
                        logger.warning("Notion 생성 실패 %s: %s", ticker, resp.text[:100])
            except Exception:
                logger.exception("Notion 업데이트 오류: %s", ticker)

        logger.info("Notion 장전 스캔 업데이트 완료: %d개 종목", len(candidates[:15]))

    # ── Telegram 브리핑 ───────────────────────────────────────

    def send_telegram_briefing(self, trade_date: str, candidates: list[dict], analysis: dict) -> None:
        if not self.telegram_token or not self.telegram_chat:
            return

        briefing = analysis.get("telegram_briefing", "")
        if not briefing:
            tickers = [c.get("ticker", "") for c in candidates[:5]]
            summary = analysis.get("watchlist_summary", "")
            top = analysis.get("top_pick", "")
            briefing = (
                f"📊 <b>{trade_date} 장전 브리핑</b>\n\n"
                f"선정 종목: {len(candidates)}개\n"
                f"상위 후보: {', '.join(tickers)}\n"
            )
            if top:
                briefing += f"\n⭐ 최우선: {top}"
            if summary:
                briefing += f"\n\n{summary[:300]}"

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={"chat_id": int(self.telegram_chat) if self.telegram_chat.lstrip('-').isdigit()
                      else self.telegram_chat,
                      "text": briefing, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                logger.info("Telegram 장전 브리핑 전송 완료")
            else:
                logger.warning("Telegram 전송 실패: %s", resp.text[:100])
        except Exception:
            logger.exception("Telegram 전송 오류")

    # ── agent_runs 기록 ───────────────────────────────────────

    def _record_run(self, trade_date: str, candidates: list[dict], analysis: dict) -> None:
        self._sb_upsert("agent_runs", {
            "agent_name": "premarket_agent",
            "trade_date": trade_date,
            "input_payload": {"candidates_count": len(candidates),
                              "tickers": [c.get("ticker") for c in candidates[:10]]},
            "output_payload": {
                "summary": analysis.get("watchlist_summary", "")[:300],
                "top_pick": analysis.get("top_pick", ""),
                "market_theme": analysis.get("market_theme", ""),
            },
            "status": "SUCCESS",
            "llm_model": self.llm.model,
        }, on_conflict="agent_name,trade_date")

    # ── 메인 실행 ─────────────────────────────────────────────

    def run(self, trade_date: str | None = None) -> dict[str, Any]:
        if trade_date is None:
            trade_date = datetime.now(KST).strftime("%Y-%m-%d")

        logger.info("=== Pre-market Reason Agent 시작: %s ===", trade_date)

        # 1. 데이터 수집
        candidates = self.fetch_today_watchlist(trade_date)
        if not candidates:
            logger.warning("오늘 장전 스캔 데이터 없음")
            return {"status": "no_data", "date": trade_date}

        # 2. LLM 분석
        analysis = self.analyze(candidates)

        # 3. Notion 업데이트 (AI 근거 포함)
        self.update_notion(trade_date, candidates, analysis)

        # 4. Telegram 브리핑
        self.send_telegram_briefing(trade_date, candidates, analysis)

        # 5. Supabase에 AI 근거 업데이트
        analysis_by_ticker = {c["ticker"]: c for c in analysis.get("candidates", [])}
        for item in candidates:
            ticker = item.get("ticker", "")
            ai = analysis_by_ticker.get(ticker, {})
            if ai.get("reason_ko"):
                self._sb_upsert("premarket_scan", {
                    "scan_date": trade_date,
                    "ticker": ticker,
                    "ict_setup": ai.get("reason_ko", "")[:500],
                    "scan_reason": ai.get("entry_scenario", "")[:300],
                }, on_conflict="scan_date,ticker")

        # 6. agent_runs 기록
        self._record_run(trade_date, candidates, analysis)

        logger.info("=== Pre-market Reason Agent 완료 ===")
        return {
            "status": "success",
            "date": trade_date,
            "candidates_count": len(candidates),
            "analysis": {
                "summary": analysis.get("watchlist_summary", ""),
                "top_pick": analysis.get("top_pick", ""),
                "market_theme": analysis.get("market_theme", ""),
            }
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-market Reason Agent")
    parser.add_argument("--date", type=str, default=None, help="분석 날짜 (YYYY-MM-DD)")
    args = parser.parse_args()

    agent = PremarketAgent()
    result = agent.run(trade_date=args.date)

    if result.get("status") == "success":
        print(f"\n✅ 장전 분석 완료: {result['date']}")
        print(f"   종목 수: {result['candidates_count']}개")
        a = result.get("analysis", {})
        if a.get("top_pick"):
            print(f"   최우선: {a['top_pick']}")
        if a.get("summary"):
            print(f"\n📋 요약:\n{a['summary']}")
    elif result.get("status") == "no_data":
        print(f"⚠️  {result['date']} 장전 스캔 데이터 없음 — 먼저 스캔을 실행하세요")
    else:
        print(f"❌ 실패: {result}")


if __name__ == "__main__":
    main()
