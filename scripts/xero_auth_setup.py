"""One-time Xero OAuth2 authorization-code dance.

Usage (from the jarvies-mcp project root):
    python scripts/xero_auth_setup.py

What this does:
1. Reads XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REDIRECT_URI from settings.
2. Builds an authorize URL, prints it, asks you to open it in your browser.
3. Starts a one-shot localhost HTTP server (built-in http.server) that listens
   for Xero's `/xero/callback?code=...&state=...` redirect.
4. Validates the CSRF `state`, exchanges the code for tokens at
   `https://identity.xero.com/connect/token`, fetches connected orgs from
   `https://api.xero.com/connections`, and prints the env block you need to
   paste into `.env`.

Nothing is written to disk. The dance is interactive and must be re-run if
you ever lose your refresh token.
"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# Allow `python scripts/xero_auth_setup.py` from any cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.mcp.config import get_settings  # noqa: E402

AUTHORIZE_BASE = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
SCOPES = (
    "offline_access "
    "accounting.contacts.read "
    "accounting.transactions.read "
    "accounting.reports.read"
)
CALLBACK_TIMEOUT_SECONDS = 5 * 60


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Xero `/identity/connect/authorize` URL."""

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_BASE}?{urllib.parse.urlencode(params)}"


class _CallbackResult:
    """Mutable container the handler writes into and the main thread reads."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None


def _make_handler(expected_state: str, callback_path: str, result: _CallbackResult):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default access-log noise — the parent thread prints status.
            return

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != callback_path:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not found.\n")
                return

            params = urllib.parse.parse_qs(parsed.query)
            received_state = (params.get("state") or [""])[0]
            code = (params.get("code") or [""])[0]
            err = (params.get("error") or [""])[0]

            if err:
                result.error = f"Xero returned error={err}"
                self._html(400, "Xero returned an error.", result.error)
                return

            if received_state != expected_state:
                result.error = "CSRF state mismatch on /xero/callback"
                self._html(400, "State mismatch.", result.error)
                return

            if not code:
                result.error = "/xero/callback arrived without a code parameter"
                self._html(400, "Missing code.", result.error)
                return

            result.code = code
            result.state = received_state
            self._html(
                200,
                "Xero authorization complete.",
                "You can close this window and return to your terminal.",
            )

        def _html(self, status: int, title: str, body: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title></head>"
                "<body style='font-family:sans-serif;max-width:560px;margin:60px auto;'>"
                f"<h1>{title}</h1><p>{body}</p></body></html>"
            )
            self.wfile.write(html.encode("utf-8"))

    return _Handler


def _wait_for_callback(host: str, port: int, callback_path: str, expected_state: str) -> _CallbackResult:
    result = _CallbackResult()
    handler_cls = _make_handler(expected_state, callback_path, result)
    try:
        server = HTTPServer((host, port), handler_cls)
    except OSError as exc:
        if isinstance(exc, OSError) and exc.errno in {10048, 98}:
            # 10048 = WSAEADDRINUSE on Windows, 98 = EADDRINUSE on Linux/mac.
            raise SystemExit(
                f"Port {port} is in use. Stop any running Jarvies server (or "
                f"whatever else is on {port}) and try again."
            ) from exc
        raise

    print(f"Listening on http://{host}:{port}{callback_path} (timeout: 5 min)…")
    server.timeout = CALLBACK_TIMEOUT_SECONDS

    deadline_thread = threading.Timer(CALLBACK_TIMEOUT_SECONDS, server.shutdown)
    deadline_thread.daemon = True
    deadline_thread.start()

    try:
        # handle_request blocks until exactly one connection is served.
        while result.code is None and result.error is None:
            server.handle_request()
    finally:
        deadline_thread.cancel()
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass

    return result


def _post_form(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    body = urllib.parse.urlencode(data).encode("ascii")
    req_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": raw}


def _get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": raw}


def _choose_tenant(connections: list[dict[str, Any]]) -> dict[str, Any]:
    if not connections:
        print(
            "ERROR: No Xero organisations are connected. Re-run the OAuth dance "
            "and make sure to select an organisation when prompted."
        )
        raise SystemExit(1)

    if len(connections) == 1:
        return connections[0]

    print("\nMultiple Xero organisations are connected. Choose one:")
    for idx, conn in enumerate(connections, start=1):
        name = conn.get("tenantName", "<unknown>")
        ttype = conn.get("tenantType", "?")
        tid = conn.get("tenantId", "?")
        print(f"  {idx}. {name} [{ttype}] — {tid}")

    while True:
        choice = input("Enter the number of the org you want to connect: ").strip()
        try:
            n = int(choice)
        except ValueError:
            print("Please enter a number.")
            continue
        if 1 <= n <= len(connections):
            return connections[n - 1]
        print(f"Out of range (1..{len(connections)}).")


def main() -> int:
    settings = get_settings()

    client_id = settings.xero_client_id
    client_secret = settings.xero_client_secret
    redirect_uri = settings.xero_redirect_uri

    missing = [
        name
        for name, value in (
            ("XERO_CLIENT_ID", client_id),
            ("XERO_CLIENT_SECRET", client_secret),
            ("XERO_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        print("ERROR: Missing required env vars:")
        for name in missing:
            print(f"  - {name}")
        print(
            "\nAdd them to your .env file (or export them) and re-run.\n"
            "XERO_CLIENT_ID and XERO_CLIENT_SECRET come from the Xero Developer "
            "portal. XERO_REDIRECT_URI must match what you registered there — "
            "for local setup, use http://localhost:8080/xero/callback."
        )
        return 1

    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    callback_path = parsed.path or "/xero/callback"

    # Bind preflight — surface a clear message before we print the URL.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        print(
            f"Port {port} is in use. Stop any running Jarvies server (or "
            f"whatever else is on {port}) and try again."
        )
        return 1
    finally:
        probe.close()

    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(client_id, redirect_uri, state)

    print("=" * 70)
    print("Xero OAuth2 authorization-code setup")
    print("=" * 70)
    print(
        "\nOpen this URL in your browser and authorize the Jarvies app against "
        "the Xero organisation you want to connect:\n"
    )
    print(auth_url)
    print(
        f"\nAfter clicking Allow, you'll be redirected to {redirect_uri} — "
        "leave this terminal running until that happens.\n"
    )

    callback = _wait_for_callback(host, port, callback_path, state)
    if callback.error:
        print(f"ERROR: {callback.error}")
        return 1
    if not callback.code:
        print("ERROR: callback timed out without delivering a code.")
        return 1

    print("Received authorization code. Exchanging for tokens…")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    status, payload = _post_form(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": callback.code,
            "redirect_uri": redirect_uri,
        },
        headers={"Authorization": f"Basic {basic}"},
    )
    if status != 200:
        print(f"ERROR: Xero token exchange failed (HTTP {status}).")
        print(json.dumps(payload, indent=2)[:2000])
        return 1

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not access_token or not refresh_token:
        print("ERROR: token response missing access_token or refresh_token.")
        print(json.dumps({k: v for k, v in payload.items() if k != "id_token"}, indent=2))
        return 1

    print("Fetching connected organisations from /connections…")
    status, connections = _get_json(
        CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    if status != 200 or not isinstance(connections, list):
        print(f"ERROR: /connections call failed (HTTP {status}).")
        print(json.dumps(connections, indent=2)[:2000])
        return 1

    chosen = _choose_tenant(connections)
    tenant_id = chosen.get("tenantId")
    tenant_name = chosen.get("tenantName", "<unknown>")
    if not tenant_id:
        print("ERROR: chosen tenant had no tenantId.")
        return 1

    print("\n" + "=" * 70)
    print(f"SUCCESS — Xero is now authorized for org: {tenant_name}")
    print()
    print("Add these two lines to your .env file (or update if present):")
    print()
    print(f"XERO_TENANT_ID={tenant_id}")
    print(f"XERO_REFRESH_TOKEN={refresh_token}")
    print()
    print(
        "Refresh token expires 60 days from last use. Jarvies will refresh it "
        "automatically as long as it's used regularly."
    )
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
