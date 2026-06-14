"""Trust the edge proxy's ``X-Forwarded-*`` headers.

ECS (behind an ALB) and App Runner terminate TLS and forward plain HTTP to the
container. Without this, the app sees ``scheme=http`` and emits ``http://``
redirects and OAuth discovery URLs even though the client reached it over
``https``. The most visible symptom was ``POST /mcp/`` returning a 307 whose
``Location`` downgraded to ``http://`` (and dropped the trailing slash),
breaking MCP clients.

This pure-ASGI middleware rewrites the request scope's ``scheme`` (and host,
when forwarded) from the proxy headers so every generated URL is correct. Keep
it the outermost middleware so the corrected scheme is visible to routing,
redirects, auth, and the OAuth handlers alike.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class ForwardedProtoMiddleware:
    """Apply ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` to the ASGI scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])

        proto = headers.get(b"x-forwarded-proto")
        if proto:
            # A comma-separated list means multiple proxies; the client-facing
            # value is the first hop.
            scope["scheme"] = proto.decode("latin-1").split(",")[0].strip()

        forwarded_host = headers.get(b"x-forwarded-host")
        if forwarded_host:
            host = forwarded_host.split(b",")[0].strip()
            # Replace the Host header so ``request.base_url`` netloc is correct.
            new_headers = [(k, v) for (k, v) in scope["headers"] if k != b"host"]
            new_headers.append((b"host", host))
            scope = dict(scope)
            scope["headers"] = new_headers

        await self.app(scope, receive, send)
