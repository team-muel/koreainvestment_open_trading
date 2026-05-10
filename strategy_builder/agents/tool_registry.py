"""Safe tool registry for analysis agents.

This is an internal MCP-like layer. Agents call named tools instead of
directly reaching into broker, Supabase, Notion, or calendar clients.
Order-affecting tools must not be registered here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class ToolAuditSink(Protocol):
    def record_agent_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any] | None = None,
        status: str = "SUCCESS",
        error_message: str = "",
    ) -> None:
        ...


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    func: Callable[..., Any]
    description: str
    allowed_agents: frozenset[str]
    dangerous: bool = False


class AgentToolRegistry:
    """Whitelist-based tool registry for read/report agents."""

    def __init__(self, audit_sink: ToolAuditSink | None = None):
        self._tools: dict[str, RegisteredTool] = {}
        self._audit_sink = audit_sink

    def register(
        self,
        name: str,
        func: Callable[..., Any],
        *,
        description: str,
        allowed_agents: set[str] | list[str] | tuple[str, ...],
        dangerous: bool = False,
    ) -> None:
        if not name:
            raise ValueError("tool name is required")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            name=name,
            func=func,
            description=description,
            allowed_agents=frozenset(allowed_agents),
            dangerous=dangerous,
        )

    def describe_for_agent(self, agent_name: str) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "dangerous": tool.dangerous,
            }
            for tool in self._tools.values()
            if agent_name in tool.allowed_agents
        ]

    def call(self, agent_name: str, name: str, **kwargs: Any) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown agent tool: {name}")
        if agent_name not in tool.allowed_agents:
            raise PermissionError(f"{agent_name} is not allowed to call {name}")
        if tool.dangerous:
            raise PermissionError(f"dangerous tool is blocked for agents: {name}")

        try:
            result = tool.func(**kwargs)
            self._record(agent_name, name, kwargs, self._summarize(result), "SUCCESS", "")
            return result
        except Exception as exc:
            self._record(agent_name, name, kwargs, None, "ERROR", str(exc))
            raise

    def _record(
        self,
        agent_name: str,
        tool_name: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any] | None,
        status: str,
        error_message: str,
    ) -> None:
        if not self._audit_sink:
            return
        try:
            self._audit_sink.record_agent_tool_call(
                agent_name=agent_name,
                tool_name=tool_name,
                input_payload=input_payload,
                output_payload=output_payload,
                status=status,
                error_message=error_message,
            )
        except Exception:
            logger.exception("agent tool audit logging failed")

    @staticmethod
    def _summarize(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                key: len(item) if isinstance(item, list) else item
                for key, item in value.items()
                if key in {
                    "trade_date",
                    "start_date",
                    "end_date",
                    "daily_reports",
                    "signals",
                    "orders",
                    "fills",
                    "positions",
                    "premarket_scan",
                    "agent_runs",
                    "heartbeats",
                    "journal",
                    "success",
                }
            }
        if isinstance(value, list):
            return {"rows": len(value)}
        return {"type": type(value).__name__}


def build_agent_tool_registry(audit_sink: ToolAuditSink | None = None) -> AgentToolRegistry:
    from strategy_builder.agents.supabase_tools import SupabaseDomainTools

    tools = SupabaseDomainTools()
    registry = AgentToolRegistry(audit_sink=audit_sink)

    registry.register(
        "get_daily_report_context",
        tools.get_daily_report_context,
        description="Read daily reports, signals, orders, fills, premarket scan, and positions.",
        allowed_agents={"daily_report_agent"},
    )
    registry.register(
        "get_strategy_review_context",
        tools.get_strategy_review_context,
        description="Read recent reports, signals, fills, and risk-review runs for strategy review.",
        allowed_agents={"strategy_reviewer_agent"},
    )
    registry.register(
        "get_audit_context",
        tools.get_audit_context,
        description="Read one trading day's events for audit reconstruction.",
        allowed_agents={"audit_agent"},
    )
    registry.register(
        "save_agent_run",
        tools.save_agent_run,
        description="Persist agent run metadata and output summary.",
        allowed_agents={"daily_report_agent", "strategy_reviewer_agent", "audit_agent", "premarket_agent"},
    )

    return registry
