import unittest
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "strategy_builder"))

from strategy_builder.agents.supabase_tools import SupabaseDomainTools
from strategy_builder.agents.tool_registry import AgentToolRegistry


class FakeAuditSink:
    def __init__(self):
        self.calls = []

    def record_agent_tool_call(self, **kwargs):
        self.calls.append(kwargs)


class FakeSupabaseClient:
    def __init__(self):
        self.upserts = []
        self.tables = {
            "daily_reports": [{"report_date": "2026-05-10", "daily_pnl": 1000}],
            "signals": [
                {"ticker": "005930", "reason_not_taken": ""},
                {"ticker": "000660", "reason_not_taken": "spread too wide"},
            ],
            "orders": [{"order_no": "1"}],
            "fills": [{"order_no": "1", "is_complete": True}],
            "positions": [{"symbol": "005930"}],
            "premarket_scan": [{"ticker": "005930"}],
            "agent_runs": [{"agent_name": "risk_review_agent"}],
            "engine_heartbeats": [{"worker": "trading-worker"}],
            "trade_journal": [{"event": "ENTRY_SUBMITTED"}],
        }

    def select(self, table, *, filters=None, limit=100, order=None):
        return list(self.tables.get(table, []))[: int(limit)]

    def upsert(self, table, data, on_conflict=None):
        self.upserts.append((table, data, on_conflict))


class AgentToolRegistryTest(unittest.TestCase):
    def test_registry_blocks_dangerous_tool_for_agents(self):
        registry = AgentToolRegistry()
        registry.register(
            "place_order",
            lambda: {"ok": True},
            description="dangerous order tool",
            allowed_agents={"daily_report_agent"},
            dangerous=True,
        )

        with self.assertRaises(PermissionError):
            registry.call("daily_report_agent", "place_order")

    def test_registry_whitelist_and_audit_log(self):
        sink = FakeAuditSink()
        registry = AgentToolRegistry(audit_sink=sink)
        registry.register(
            "get_context",
            lambda trade_date: {"trade_date": trade_date, "signals": [1, 2]},
            description="safe read",
            allowed_agents={"daily_report_agent"},
        )

        result = registry.call("daily_report_agent", "get_context", trade_date="2026-05-10")

        self.assertEqual(result["trade_date"], "2026-05-10")
        self.assertEqual(len(sink.calls), 1)
        self.assertEqual(sink.calls[0]["tool_name"], "get_context")
        self.assertEqual(sink.calls[0]["status"], "SUCCESS")

        with self.assertRaises(PermissionError):
            registry.call("audit_agent", "get_context", trade_date="2026-05-10")


class SupabaseDomainToolsTest(unittest.TestCase):
    def test_daily_report_context_has_stable_shape_and_blocked_entries(self):
        tools = SupabaseDomainTools(client=FakeSupabaseClient())

        context = tools.get_daily_report_context("2026-05-10")

        self.assertEqual(context["trade_date"], "2026-05-10")
        self.assertIn("daily_report", context)
        self.assertIn("signals", context)
        self.assertIn("orders", context)
        self.assertIn("fills", context)
        self.assertIn("positions", context)
        self.assertIn("premarket_scan", context)
        self.assertEqual(len(context["blocked_entries"]), 1)

    def test_save_agent_run_uses_agent_runs_table(self):
        client = FakeSupabaseClient()
        tools = SupabaseDomainTools(client=client)

        result = tools.save_agent_run(
            agent_name="daily_report_agent",
            trade_date="2026-05-10",
            input_payload={"signals_count": 2},
            output_payload={"summary": "ok"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(client.upserts[0][0], "agent_runs")
        self.assertEqual(client.upserts[0][2], "agent_name,trade_date")


if __name__ == "__main__":
    unittest.main()
