"""Audit Agent — 주문/차단/청산의 전체 근거 감사 로그 자동 작성.

"왜 이 주문이 나갔는가?" "왜 이 종목은 차단됐는가?"에
완전한 추적 가능성(traceability)을 제공합니다.

매일 장 종료 후 실행하여 Supabase에 저장된 모든 이벤트를
시간 순으로 재구성하고, 각 결정의 근거를 명확히 기록합니다.

⚠️ 권한:
   ✅ Supabase 전체 읽기 (signals, orders, fills, agent_runs, engine_heartbeats)
   ✅ Notion Daily Trading Report 업데이트 (감사 섹션)
   ✅ Telegram 이상 감지 알림
   🚫 어떠한 데이터도 변경/삭제 불가

사용법:
    python -m strategy_builder.agents.audit_agent
    python -m strategy_builder.agents.audit_agent --date 2026-05-09
"""
from __future__ import annotations

import argparse
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
from strategy_builder.agents.tool_registry import build_agent_tool_registry
from strategy_builder.core.supabase_journal import SupabaseJournal

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

SYSTEM_PROMPT = """당신은 자동매매 시스템 감사(Audit) 전문가입니다.
하루의 모든 트레이딩 이벤트를 시간 순으로 재구성하여
각 결정의 근거를 명확하고 간결하게 설명합니다.

감사 원칙:
- 모든 주장은 실제 데이터에 근거해야 합니다
- 이벤트 간 인과관계를 명확히 서술
- 이상 징후 (규칙 위반, 시스템 오류, 예상치 못한 동작)를 강조
- 정상 동작은 간결하게, 이상 동작은 상세하게
- "의심스러운 패턴"도 데이터가 뒷받침될 때만 언급

절대 하지 말 것:
- 데이터 없이 추측하거나 가정
- 사용자를 비판하거나 자책 유도
- 시스템 변경 직접 지시"""


class AuditAgent:
    """일일 트레이딩 감사 로그 생성."""

    def __init__(self):
        self.llm = LLMClient()
        self.supabase_url = os.environ.get("SUPABASE_URL", "").strip()
        self.supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
        self.notion_token = os.environ.get("NOTION_TOKEN", "").strip()
        self.notion_daily_db = os.environ.get("NOTION_DAILY_REPORT_DB_ID", "").strip()
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.tool_registry = build_agent_tool_registry(audit_sink=SupabaseJournal())

    # Supabase access for this agent goes through AgentToolRegistry.

    # ── 데이터 수집 ───────────────────────────────────────────

    def fetch_day_events(self, trade_date: str) -> dict[str, Any]:
        """하루의 모든 이벤트 수집."""
        return self.tool_registry.call(
            "audit_agent",
            "get_audit_context",
            trade_date=trade_date,
        )

    # ── 타임라인 재구성 ───────────────────────────────────────

    def build_timeline(self, events: dict[str, Any]) -> list[dict]:
        """모든 이벤트를 시간 순으로 정렬."""
        timeline = []

        for s in events["signals"]:
            timeline.append({
                "time": s.get("created_at", "")[:19],
                "type": "SIGNAL",
                "action": "진입" if s.get("action_taken") else f"차단({s.get('reason_not_taken', '?')})",
                "ticker": s.get("ticker"),
                "detail": {
                    "entry": s.get("entry_candidate"),
                    "stop": s.get("stop_candidate"),
                    "target": s.get("target_candidate"),
                    "sweep": s.get("sweep_confirmed"),
                    "vwap": s.get("vwap_reclaim"),
                    "mss": s.get("mss_confirmed"),
                    "volume": s.get("volume_confirmed"),
                    "reason": s.get("reason_not_taken", ""),
                },
            })

        for o in events["orders"]:
            timeline.append({
                "time": (o.get("submitted_at") or "")[:19],
                "type": "ORDER",
                "action": f"{o.get('side', '?').upper()} 주문 제출",
                "ticker": o.get("symbol"),
                "detail": {
                    "order_no": o.get("order_no"),
                    "qty": o.get("quantity"),
                    "price": o.get("price"),
                    "status": o.get("status"),
                },
            })

        for f in events["fills"]:
            pnl = float(f.get("realized_pnl", 0) or 0)
            timeline.append({
                "time": (f.get("fill_time") or "")[:19],
                "type": "FILL",
                "action": f"체결 완료 ({'+' if pnl >= 0 else ''}{pnl:,.0f}원)",
                "ticker": f.get("symbol"),
                "detail": {
                    "qty": f.get("filled_qty"),
                    "avg_price": f.get("avg_price"),
                    "pnl": pnl,
                    "fees": f.get("fees"),
                },
            })

        for j in events["journal"]:
            event_type = j.get("event", "")
            if event_type in ("EMERGENCY_STOP", "EXIT_SUBMITTED", "POSITION_CLOSED", "ENTRY_SUBMITTED", "PREMARKET_READY"):
                timeline.append({
                    "time": (j.get("created_at") or "")[:19],
                    "type": "ENGINE",
                    "action": event_type,
                    "ticker": j.get("ticker") or "ENGINE",
                    "detail": {"exit_reason": (j.get("payload", {}) or {}).get("exit_reason", "")},
                })

        for h in events["heartbeats"]:
            if h.get("degraded"):
                timeline.append({
                    "time": (h.get("beat_at") or "")[:19],
                    "type": "ALERT",
                    "action": f"엔진 DEGRADED: {h.get('last_error', '')[:80]}",
                    "ticker": "ENGINE",
                    "detail": {},
                })

        # 시간 순 정렬
        timeline.sort(key=lambda x: x.get("time", ""))
        return timeline

    # ── 이상 징후 감지 ────────────────────────────────────────

    def detect_anomalies(self, events: dict, timeline: list[dict]) -> list[dict]:
        """규칙 위반, 시스템 오류, 예상치 못한 동작 감지."""
        anomalies = []

        # 1. EMERGENCY_STOP 발생
        emergency_stops = [e for e in timeline if e["action"] == "EMERGENCY_STOP"]
        if emergency_stops:
            for e in emergency_stops:
                anomalies.append({
                    "severity": "CRITICAL",
                    "time": e["time"],
                    "description": f"엔진 비상 정지 발생: {e['detail'].get('exit_reason', '')}",
                })

        # 2. 주문 제출 후 체결 없음 (미체결 주문)
        order_nos = {o.get("order_no") for o in events["orders"]}
        filled_nos = {f.get("order_no") for f in events["fills"]}
        unfilled = order_nos - filled_nos
        if unfilled:
            anomalies.append({
                "severity": "WARNING",
                "time": "",
                "description": f"미체결 주문 {len(unfilled)}건: {list(unfilled)[:3]}",
            })

        # 3. 같은 종목 중복 시그널 (5분 내)
        signal_by_ticker: dict[str, list] = {}
        for s in events["signals"]:
            ticker = s.get("ticker", "")
            if ticker not in signal_by_ticker:
                signal_by_ticker[ticker] = []
            signal_by_ticker[ticker].append(s.get("created_at", ""))
        for ticker, times in signal_by_ticker.items():
            if len(times) > 3:
                anomalies.append({
                    "severity": "INFO",
                    "time": times[-1][:19],
                    "description": f"{ticker} 당일 시그널 {len(times)}회 반복",
                })

        # 4. 엔진 degraded 상태
        degraded = [h for h in events["heartbeats"] if h.get("degraded")]
        if degraded:
            anomalies.append({
                "severity": "WARNING",
                "time": (degraded[0].get("beat_at") or "")[:19],
                "description": f"엔진 degraded {len(degraded)}회 감지",
            })

        return anomalies

    # ── LLM 분석 ──────────────────────────────────────────────

    def build_audit_prompt(self, events: dict, timeline: list[dict], anomalies: list[dict]) -> str:
        dr = events["daily_report"]

        # 타임라인 요약 (최대 20개)
        tl_text = "\n".join([
            f"  {e['time'][11:16] if len(e['time']) >= 16 else '?'} [{e['type']}] {e['ticker']} — {e['action']}"
            for e in timeline[:20]
        ]) or "  이벤트 없음"

        # 차단 내역
        blocked = [s for s in events["signals"] if not s.get("action_taken")]
        block_text = "\n".join([
            f"  {s.get('created_at','')[:16]} {s.get('ticker')}: {s.get('reason_not_taken','?')}"
            for s in blocked[:10]
        ]) or "  없음"

        # 이상 징후
        anomaly_text = "\n".join([
            f"  [{a['severity']}] {a['time'][:16] if a['time'] else '?'} — {a['description']}"
            for a in anomalies
        ]) or "  이상 징후 없음"

        return f"""=== {events['trade_date']} 트레이딩 감사 데이터 ===

【일일 결산】
  손익: {float(dr.get('daily_pnl', 0) or 0):+,.0f}원
  거래: {dr.get('trades_count', 0)}건 (승률 {float(dr.get('win_rate', 0) or 0):.0%})
  정지 사유: {dr.get('stopped_reason') or '정상 종료'}

【이벤트 타임라인 ({len(timeline)}개)】
{tl_text}

【진입 차단 내역 ({len(blocked)}건)】
{block_text}

【이상 징후】
{anomaly_text}

=== 감사 보고서 작성 요청 ===
위 데이터를 기반으로 아래 JSON 형식의 감사 보고서를 작성하세요.
모든 주장은 타임라인 데이터에 근거해야 합니다.

{{
  "audit_summary": "오늘 트레이딩 시스템 동작 전체 요약 (3-4문장)",
  "decision_trail": [
    {{
      "time": "HH:MM",
      "ticker": "종목코드",
      "decision": "진입/차단/청산",
      "reason": "결정 근거 (데이터 기반)",
      "outcome": "결과 (체결가, 손익 등)"
    }}
  ],
  "rule_compliance": {{
    "compliant": ["준수된 규칙들"],
    "violations": ["위반된 규칙 (있으면)"],
    "edge_cases": ["규칙 경계 사례"]
  }},
  "anomaly_analysis": "이상 징후 상세 분석 (없으면 '정상 동작')",
  "system_health": "시스템 건강 상태 평가 (heartbeat, degraded 여부 등)",
  "traceability_gaps": ["추적 불가한 이벤트 (데이터 부족)"],
  "telegram_alert": "이상 징후가 있으면 Telegram 알림 메시지, 없으면 null"
}}"""

    def analyze(self, events: dict, timeline: list[dict], anomalies: list[dict]) -> dict[str, Any]:
        if not timeline:
            return {"audit_summary": "거래 이벤트 없음", "decision_trail": [],
                    "anomaly_analysis": "정상 동작", "telegram_alert": None}

        if not self.llm.available:
            return self._fallback_audit(events, timeline, anomalies)

        try:
            prompt = self.build_audit_prompt(events, timeline, anomalies)
            result, usage = self.llm.complete_json(
                SYSTEM_PROMPT,
                prompt,
                max_tokens=2500,
                required_keys=["audit_summary", "decision_trail", "anomaly_analysis", "telegram_alert"],
                fallback=self._fallback_audit(events, timeline, anomalies),
            )
            logger.info("AuditAgent LLM 완료 — 토큰: %s", usage)
            return result
        except Exception:
            logger.exception("LLM 감사 분석 실패")
            return self._fallback_audit(events, timeline, anomalies)

    def _fallback_audit(self, events: dict, timeline: list[dict], anomalies: list[dict]) -> dict[str, Any]:
        dr = events.get("daily_report", {})
        pnl = float(dr.get("daily_pnl", 0) or 0)
        critical = [a for a in anomalies if a["severity"] == "CRITICAL"]

        telegram_alert = None
        if critical:
            telegram_alert = (
                f"🚨 <b>감사 알림 — 이상 징후</b>\n"
                + "\n".join([f"  • {a['description']}" for a in critical[:3]])
            )

        return {
            "audit_summary": (
                f"총 {len(timeline)}개 이벤트, 손익 {pnl:+,.0f}원. "
                f"이상 징후 {len(anomalies)}건. "
                f"LLM 상세 분석은 GEMINI_API_KEY 설정 후 가능."
            ),
            "decision_trail": [],
            "rule_compliance": {"compliant": [], "violations": [], "edge_cases": []},
            "anomaly_analysis": "\n".join([a["description"] for a in anomalies]) or "정상 동작",
            "system_health": "DEGRADED" if any(a["severity"] == "CRITICAL" for a in anomalies) else "NORMAL",
            "traceability_gaps": [],
            "telegram_alert": telegram_alert,
        }

    # ── Notion 업데이트 ───────────────────────────────────────

    def update_notion_audit(self, trade_date: str, timeline: list[dict], anomalies: list[dict], audit: dict) -> None:
        if not self.notion_token or not self.notion_daily_db:
            return
        headers = {"Authorization": f"Bearer {self.notion_token}",
                   "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

        # 기존 일일 리포트 페이지에 감사 섹션 추가
        try:
            search = requests.post(
                f"https://api.notion.com/v1/databases/{self.notion_daily_db}/query",
                headers=headers,
                json={"filter": {"property": "Date", "date": {"equals": trade_date}}, "page_size": 1},
                timeout=15,
            )
            results = search.json().get("results", []) if search.ok else []
            if not results:
                logger.warning("Notion: %s 일일 리포트 페이지 없음 — 감사 섹션 스킵", trade_date)
                return

            page_id = results[0]["id"]
            children = [
                {"object": "block", "type": "divider", "divider": {}},
                {"object": "block", "type": "heading_2",
                 "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🔍 감사 로그 (Audit)"}}]}},
            ]

            summary = audit.get("audit_summary", "")
            if summary:
                children.append({
                    "object": "block", "type": "callout",
                    "callout": {"icon": {"emoji": "📋"},
                                "rich_text": [{"type": "text", "text": {"content": summary[:2000]}}]},
                })

            # 이상 징후
            if anomalies:
                children.append({
                    "object": "block", "type": "callout",
                    "callout": {
                        "icon": {"emoji": "⚠️"},
                        "rich_text": [{"type": "text", "text": {"content":
                            "이상 징후\n" + "\n".join([f"[{a['severity']}] {a['description']}" for a in anomalies])
                        }}],
                    },
                })

            # 의사결정 추적
            decisions = audit.get("decision_trail", [])
            if decisions:
                children.append({"object": "block", "type": "heading_3",
                                 "heading_3": {"rich_text": [{"type": "text", "text": {"content": "의사결정 추적"}}]}})
                for d in decisions[:8]:
                    text = f"{d.get('time','?')} {d.get('ticker','')} [{d.get('decision','')}]\n근거: {d.get('reason','')}\n결과: {d.get('outcome','')}"
                    children.append({"object": "block", "type": "bulleted_list_item",
                                     "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}})

            # 시스템 건강
            health = audit.get("system_health", "NORMAL")
            children.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {
                    "content": f"시스템 상태: {'✅ NORMAL' if health == 'NORMAL' else f'⚠️ {health}'}"
                }}]},
            })

            requests.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                           headers=headers, json={"children": children[:80]}, timeout=20)
            logger.info("Notion 감사 로그 추가 완료: %s", trade_date)
        except Exception:
            logger.exception("Notion 감사 업데이트 오류")

    # ── Telegram 이상 알림 ────────────────────────────────────

    def send_alert_if_needed(self, audit: dict) -> None:
        alert = audit.get("telegram_alert")
        if not alert or not self.telegram_token or not self.telegram_chat:
            return
        try:
            chat_id = int(self.telegram_chat) if self.telegram_chat.lstrip('-').isdigit() else self.telegram_chat
            requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": alert, "parse_mode": "HTML"},
                timeout=10,
            )
            logger.info("Telegram 이상 감지 알림 전송")
        except Exception:
            pass

    # ── 메인 실행 ─────────────────────────────────────────────

    def run(self, trade_date: str | None = None) -> dict[str, Any]:
        if trade_date is None:
            trade_date = datetime.now(KST).strftime("%Y-%m-%d")

        logger.info("=== Audit Agent 시작: %s ===", trade_date)

        # 1. 이벤트 수집
        events = self.fetch_day_events(trade_date)

        # 2. 타임라인 재구성
        timeline = self.build_timeline(events)
        logger.info("타임라인 재구성: %d개 이벤트", len(timeline))

        # 3. 이상 징후 감지
        anomalies = self.detect_anomalies(events, timeline)
        if anomalies:
            logger.warning("이상 징후 %d건 감지", len(anomalies))

        # 4. LLM 감사 분석
        audit = self.analyze(events, timeline, anomalies)

        # 5. Notion 업데이트
        self.update_notion_audit(trade_date, timeline, anomalies, audit)

        # 6. Telegram 이상 알림
        self.send_alert_if_needed(audit)

        # 7. agent_runs 기록
        self.tool_registry.call(
            "audit_agent",
            "save_agent_run",
            agent_name="audit_agent",
            trade_date=trade_date,
            input_payload={"events_count": len(timeline), "anomalies_count": len(anomalies)},
            output_payload={
                "summary": audit.get("audit_summary", "")[:300],
                "system_health": audit.get("system_health", "NORMAL"),
                "has_violations": bool(audit.get("rule_compliance", {}).get("violations")),
                "anomalies": [a["description"][:100] for a in anomalies],
            },
            status="SUCCESS",
            llm_model=self.llm.model,
        )

        logger.info("=== Audit Agent 완료 ===")
        return {
            "status": "success",
            "date": trade_date,
            "events_count": len(timeline),
            "anomalies": anomalies,
            "system_health": audit.get("system_health", "NORMAL"),
            "audit_summary": audit.get("audit_summary", ""),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Agent — 일일 감사 로그")
    parser.add_argument("--date", type=str, default=None, help="감사 날짜 (YYYY-MM-DD)")
    args = parser.parse_args()

    agent = AuditAgent()
    result = agent.run(trade_date=args.date)

    if result.get("status") == "success":
        print(f"\n✅ 감사 완료: {result['date']}")
        print(f"   이벤트: {result['events_count']}개")
        print(f"   시스템: {result['system_health']}")
        if result["anomalies"]:
            print(f"\n⚠️  이상 징후 {len(result['anomalies'])}건:")
            for a in result["anomalies"]:
                print(f"   [{a['severity']}] {a['description']}")
        print(f"\n📋 요약:\n{result.get('audit_summary', '')}")


if __name__ == "__main__":
    main()
