"""Google Calendar reporter for ICT daily workflow events.

Uses OAuth 2.0 (installed-app / desktop client) credentials.

One-time setup (run locally, not on VPS):
    python scripts/authorize_gcal.py

This generates gcal_token.json containing a refresh token.
Upload that file to the VPS — subsequent runs refresh the access token silently.

Environment variables:
    GOOGLE_OAUTH_CLIENT_JSON   Path to the downloaded OAuth 2.0 client JSON.
    GOOGLE_TOKEN_JSON          Path where the token file is stored (default: same dir as client JSON / gcal_token.json).
    GOOGLE_CALENDAR_ID         Calendar ID (e.g. your Google account email).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GCalConfig:
    client_json_path: str
    token_json_path: str
    calendar_id: str

    @classmethod
    def from_env(cls) -> "GCalConfig | None":
        client_path = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "").strip()
        cal_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
        if not client_path or not cal_id:
            return None
        if not Path(client_path).exists():
            return None
        token_path = os.environ.get(
            "GOOGLE_TOKEN_JSON",
            str(Path(client_path).parent / "gcal_token.json"),
        )
        return cls(
            client_json_path=client_path,
            token_json_path=token_path,
            calendar_id=cal_id,
        )


class _OAuth2Token:
    """Loads tokens from gcal_token.json and refreshes silently using refresh_token."""

    def __init__(self, client_json_path: str, token_json_path: str):
        self._client = self._load_client(client_json_path)
        self._token_path = Path(token_json_path)
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def _load_client(path: str) -> dict[str, Any]:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        # supports both "installed" and "web" client types
        return raw.get("installed") or raw.get("web") or raw

    def get(self) -> str:
        import time
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        self._refresh()
        return self._access_token  # type: ignore[return-value]

    def _refresh(self) -> None:
        import time
        if not self._token_path.exists():
            raise RuntimeError(
                f"Token file not found: {self._token_path}\n"
                "Run `python scripts/authorize_gcal.py` first to authorize."
            )
        token_data = json.loads(self._token_path.read_text(encoding="utf-8"))
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "No refresh_token in token file. "
                "Re-run `python scripts/authorize_gcal.py` to re-authorize."
            )
        resp = requests.post(
            TOKEN_URI,
            data={
                "client_id": self._client["client_id"],
                "client_secret": self._client["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))

        # persist updated token (access_token may change; refresh_token usually stays)
        token_data["access_token"] = self._access_token
        if "refresh_token" in data:
            token_data["refresh_token"] = data["refresh_token"]
        self._token_path.write_text(
            json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class GCalReporter:
    BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, config: GCalConfig):
        self.config = config
        self._token = _OAuth2Token(config.client_json_path, config.token_json_path)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.get()}",
            "Content-Type": "application/json",
        }

    def upsert_workflow_event(
        self,
        *,
        workflow: str,
        report_date: str,
        notion_url: str | None,
        summary_text: str,
        event_dt: datetime,
        duration_minutes: int = 15,
        reminder_minutes: int = 5,
    ) -> dict[str, Any]:
        existing_id = self._find_event(workflow=workflow, report_date=report_date)
        body = self._build_body(
            workflow=workflow,
            report_date=report_date,
            notion_url=notion_url,
            summary_text=summary_text,
            event_dt=event_dt,
            duration_minutes=duration_minutes,
            reminder_minutes=reminder_minutes,
        )
        if existing_id:
            resp = requests.put(
                f"{self.BASE}/calendars/{self.config.calendar_id}/events/{existing_id}",
                headers=self._headers(),
                json=body,
                timeout=20,
            )
        else:
            resp = requests.post(
                f"{self.BASE}/calendars/{self.config.calendar_id}/events",
                headers=self._headers(),
                json=body,
                timeout=20,
            )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id"), "url": data.get("htmlLink")}

    def create_signal_event(
        self,
        *,
        ticker: str,
        title: str,
        description: str,
        event_dt: datetime,
        reminder_minutes: int = 0,
    ) -> dict[str, Any]:
        end_dt = event_dt + timedelta(minutes=10)
        fmt = "%Y-%m-%dT%H:%M:%S"
        body = {
            "summary": f"[ICT SIGNAL] {ticker} {title}",
            "description": description,
            "start": {"dateTime": event_dt.strftime(fmt), "timeZone": "Asia/Seoul"},
            "end": {"dateTime": end_dt.strftime(fmt), "timeZone": "Asia/Seoul"},
            "extendedProperties": {
                "private": {
                    "ict_workflow": "signal",
                    "ticker": ticker,
                }
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": reminder_minutes},
                ],
            },
        }
        resp = requests.post(
            f"{self.BASE}/calendars/{self.config.calendar_id}/events",
            headers=self._headers(),
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data.get("id"), "url": data.get("htmlLink")}

    def _build_body(
        self,
        *,
        workflow: str,
        report_date: str,
        notion_url: str | None,
        summary_text: str,
        event_dt: datetime,
        duration_minutes: int,
        reminder_minutes: int,
    ) -> dict[str, Any]:
        label = "ICT Pre-market Scan" if workflow == "premarket-scan" else "ICT Post-market Feedback"
        title = f"[ICT] {label} - {report_date}"

        desc_lines = [summary_text, ""]
        if notion_url:
            desc_lines.append(f"Notion: {notion_url}")
        desc_lines += ["", f"workflow: {workflow}", f"date: {report_date}"]

        end_dt = event_dt + timedelta(minutes=duration_minutes)
        fmt = "%Y-%m-%dT%H:%M:%S"

        return {
            "summary": title,
            "description": "\n".join(desc_lines),
            "start": {"dateTime": event_dt.strftime(fmt), "timeZone": "Asia/Seoul"},
            "end": {"dateTime": end_dt.strftime(fmt), "timeZone": "Asia/Seoul"},
            "extendedProperties": {
                "private": {
                    "ict_workflow": workflow,
                    "ict_report_date": report_date,
                }
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": reminder_minutes},
                    {"method": "email", "minutes": reminder_minutes},
                ],
            },
        }

    def _find_event(self, *, workflow: str, report_date: str) -> str | None:
        resp = requests.get(
            f"{self.BASE}/calendars/{self.config.calendar_id}/events",
            headers=self._headers(),
            params={
                "privateExtendedProperty": [
                    f"ict_workflow={workflow}",
                    f"ict_report_date={report_date}",
                ],
                "maxResults": 5,
                "singleEvents": "true",
            },
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return items[0]["id"] if items else None


def publish_gcal_event_if_configured(
    *,
    workflow: str,
    report_date: str,
    notion_url: str | None,
    summary_text: str,
    event_dt: datetime,
    duration_minutes: int = 15,
    reminder_minutes: int = 5,
) -> dict[str, Any] | None:
    config = GCalConfig.from_env()
    if config is None:
        return None
    return GCalReporter(config).upsert_workflow_event(
        workflow=workflow,
        report_date=report_date,
        notion_url=notion_url,
        summary_text=summary_text,
        event_dt=event_dt,
        duration_minutes=duration_minutes,
        reminder_minutes=reminder_minutes,
    )


def publish_gcal_signal_if_configured(
    *,
    ticker: str,
    title: str,
    description: str,
    event_dt: datetime,
    reminder_minutes: int = 0,
) -> dict[str, Any] | None:
    config = GCalConfig.from_env()
    if config is None:
        return None
    return GCalReporter(config).create_signal_event(
        ticker=ticker,
        title=title,
        description=description,
        event_dt=event_dt,
        reminder_minutes=reminder_minutes,
    )
