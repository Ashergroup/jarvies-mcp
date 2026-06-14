"""Tests for ForwardedProtoMiddleware and the /mcp trailing-slash handling.

Regression cover for the live ECS bug: POST /mcp/ returned a 307 whose Location
downgraded to http:// (TLS terminated at the proxy, app saw scheme=http).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.mcp.proxy import ForwardedProtoMiddleware


def _scheme_app() -> Starlette:
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"scheme": request.url.scheme, "base_url": str(request.base_url)})

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(ForwardedProtoMiddleware)
    return app


def test_forwarded_proto_rewrites_scheme_to_https() -> None:
    client = TestClient(_scheme_app())
    resp = client.get("/whoami", headers={"x-forwarded-proto": "https"})
    assert resp.json()["scheme"] == "https"
    assert resp.json()["base_url"].startswith("https://")


def test_forwarded_proto_takes_first_hop() -> None:
    client = TestClient(_scheme_app())
    resp = client.get("/whoami", headers={"x-forwarded-proto": "https, http"})
    assert resp.json()["scheme"] == "https"


def test_no_forwarded_header_leaves_scheme_untouched() -> None:
    client = TestClient(_scheme_app())
    resp = client.get("/whoami")
    assert resp.json()["scheme"] == "http"


def test_forwarded_host_rewrites_base_url() -> None:
    client = TestClient(_scheme_app())
    resp = client.get(
        "/whoami",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "live.example.aws"},
    )
    assert resp.json()["base_url"].rstrip("/") == "https://live.example.aws"


def test_mcp_served_at_both_slash_variants_without_redirect() -> None:
    """Both /mcp and /mcp/ route to the same endpoint; no 307 is emitted."""

    hits: list[str] = []

    async def endpoint(request: Request) -> JSONResponse:
        hits.append(request.url.path)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", endpoint)])
    mcp_route = next(r for r in app.router.routes if getattr(r, "path", None) == "/mcp")
    app.router.routes.append(Route("/mcp/", endpoint=mcp_route.endpoint))
    app.router.redirect_slashes = False

    client = TestClient(app)
    for path in ("/mcp", "/mcp/"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200, (path, resp.status_code)
    assert hits == ["/mcp", "/mcp/"]
