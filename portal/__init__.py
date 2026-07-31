"""Jarvies portal — server-rendered admin/sales UI mounted into the MCP app.

Day 4 ships the Headspace sales portal (``portal.sales``). The package renders
plain server-side HTML (no Jinja) reusing the inline-CSS card style from
``agents.mcp.admin_consent`` and enforces its own signed-cookie sessions
(``portal.sessions``) — it does not rely on the MCP auth middleware, which
exempts ``/portal/*`` by path prefix.
"""
