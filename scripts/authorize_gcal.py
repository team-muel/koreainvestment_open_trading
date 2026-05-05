"""One-time Google Calendar OAuth 2.0 authorization.

Run this ONCE on your local PC (not the VPS) to generate gcal_token.json.
Then upload gcal_token.json to the VPS alongside the client JSON file.

Usage:
    python scripts/authorize_gcal.py --client secrets/client_secret.json

The token file is saved next to the client JSON as gcal_token.json,
or specify a custom path with --token.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests


AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Authorize Google Calendar OAuth 2.0")
    parser.add_argument(
        "--client",
        required=True,
        help="Path to the downloaded OAuth 2.0 client JSON (client_secret_*.json)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Where to save the token file (default: <client dir>/gcal_token.json)",
    )
    return parser.parse_args()


def load_client(path: str) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web") or raw
    if "client_id" not in client or "client_secret" not in client:
        sys.exit(f"Invalid client JSON: {path}")
    return client


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_code(port: int) -> str:
    """Start a one-shot local HTTP server and return the auth code from the redirect."""
    code_holder: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                code_holder.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h2>인증 완료!</h2>"
                    "<p>이 창을 닫고 터미널로 돌아가세요.</p></body></html>".encode("utf-8")
                )
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_):
            pass  # suppress access logs

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 120

    def serve():
        while not code_holder:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    t.join(timeout=180)

    if not code_holder:
        sys.exit("타임아웃: 180초 이내에 인증이 완료되지 않았습니다.")
    return code_holder[0]


def build_auth_url(client: dict, redirect_uri: str) -> str:
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_URI + "?" + urllib.parse.urlencode(params)


def exchange_code(client: dict, code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        TOKEN_URI,
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    args = parse_args()
    client = load_client(args.client)
    token_path = Path(args.token) if args.token else Path(args.client).parent / "gcal_token.json"

    port = find_free_port()
    redirect_uri = f"http://localhost:{port}"

    auth_url = build_auth_url(client, redirect_uri)

    print("\n[1] 브라우저에서 Google 계정으로 승인합니다...")
    webbrowser.open(auth_url)
    print(f"    (브라우저가 열리지 않으면 직접 방문: {auth_url})\n")

    print("[2] 승인 완료를 기다리는 중...")
    code = wait_for_code(port)

    print("\n[3] 토큰 교환 중...")
    token_data = exchange_code(client, code, redirect_uri)

    if "refresh_token" not in token_data:
        sys.exit(
            "refresh_token이 없습니다.\n"
            "아래 페이지에서 앱 권한을 취소한 후 다시 실행하세요:\n"
            "https://myaccount.google.com/permissions"
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[완료] 토큰 저장됨: {token_path}")
    print("\n다음 단계 (VPS 배포):")
    print(f"  scp {args.client} {token_path} ubuntu@<VPS_IP>:/opt/kis-ict/secrets/")
    print("\n.env 설정:")
    print(f"  GOOGLE_OAUTH_CLIENT_JSON=/opt/kis-ict/secrets/client_secret.json")
    print(f"  GOOGLE_TOKEN_JSON=/opt/kis-ict/secrets/gcal_token.json")
    print(f"  GOOGLE_CALENDAR_ID=your-email@gmail.com")


if __name__ == "__main__":
    main()
