"""Minimal Notion API reporter for ICT daily operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests


NOTION_VERSION = "2022-06-28"


@dataclass(frozen=True)
class NotionConfig:
    token: str
    daily_reports_db_id: str

    @classmethod
    def from_env(cls) -> "NotionConfig | None":
        token = os.environ.get("NOTION_TOKEN", "").strip()
        db_id = os.environ.get("NOTION_DAILY_REPORTS_DB_ID", "").strip()
        if not token or not db_id:
            return None
        return cls(token=token, daily_reports_db_id=db_id)


class NotionReporter:
    def __init__(self, config: NotionConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {config.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def upsert_daily_report(
        self,
        *,
        report_date: str,
        workflow: str,
        status: str,
        title: str,
        markdown_path: str,
        json_path: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self._find_daily_report(report_date)
        properties = self._daily_properties(
            report_date=report_date,
            workflow=workflow,
            status=status,
            title=title,
            markdown_path=markdown_path,
            json_path=json_path,
            summary=summary,
        )
        children = self._markdown_blocks(markdown_path)

        if existing:
            page_id = existing["id"]
            self._patch(f"https://api.notion.com/v1/pages/{page_id}", {"properties": properties})
            if children:
                self._patch(f"https://api.notion.com/v1/blocks/{page_id}/children", {"children": children[:80]})
            page = self._get(f"https://api.notion.com/v1/pages/{page_id}")
        else:
            page = self._post("https://api.notion.com/v1/pages", {
                "parent": {"database_id": self.config.daily_reports_db_id},
                "properties": properties,
                "children": children[:80],
            })
        return {"id": page.get("id"), "url": page.get("url")}

    def _find_daily_report(self, report_date: str) -> dict[str, Any] | None:
        response = self._post(
            f"https://api.notion.com/v1/databases/{self.config.daily_reports_db_id}/query",
            {
                "filter": {
                    "property": "Date",
                    "date": {"equals": report_date},
                },
                "page_size": 1,
            },
        )
        results = response.get("results", [])
        return results[0] if results else None

    def _daily_properties(
        self,
        *,
        report_date: str,
        workflow: str,
        status: str,
        title: str,
        markdown_path: str,
        json_path: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": report_date}},
            "Workflow": {"select": {"name": workflow}},
            "Status": {"select": {"name": status}},
            "Watchlist Count": {"number": int(summary.get("watchlist_count", 0) or 0)},
            "Actionable Count": {"number": int(summary.get("actionable_count", 0) or 0)},
            "Symbols": {"rich_text": [{"text": {"content": ", ".join(summary.get("symbols", [])[:50])}}]},
            "Markdown Path": {"rich_text": [{"text": {"content": markdown_path}}]},
            "JSON Path": {"rich_text": [{"text": {"content": json_path}}]},
        }

    def _markdown_blocks(self, markdown_path: str) -> list[dict[str, Any]]:
        path = Path(markdown_path)
        if not path.exists():
            return []
        blocks: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("# "):
                blocks.append({
                    "object": "block",
                    "type": "heading_1",
                    "heading_1": {"rich_text": [{"type": "text", "text": {"content": line[2:][:2000]}}]},
                })
            elif line.startswith("## "):
                blocks.append({
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": line[3:][:2000]}}]},
                })
            else:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": line[:2000]}}]},
                })
        return blocks

    def _get(self, url: str) -> dict[str, Any]:
        response = self.session.get(url, timeout=20)
        response.raise_for_status()
        return response.json()

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(url, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def _patch(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.patch(url, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()


def publish_daily_report_if_configured(
    *,
    report_date: str,
    workflow: str,
    status: str,
    title: str,
    markdown_path: str,
    json_path: str,
    summary: dict[str, Any],
) -> dict[str, Any] | None:
    config = NotionConfig.from_env()
    if config is None:
        return None
    return NotionReporter(config).upsert_daily_report(
        report_date=report_date,
        workflow=workflow,
        status=status,
        title=title,
        markdown_path=markdown_path,
        json_path=json_path,
        summary=summary,
    )
