"""Server-rendered HTML for the portal.

No Jinja: plain string templates with ``.replace()`` / f-strings, matching the
inline-CSS card style in ``agents.mcp.admin_consent``. All HTML lives here; the
route modules build data and call these renderers. Every dynamic value is passed
through ``esc`` (HTML-escaped) unless it is HTML this module itself produced.
"""

from __future__ import annotations

import html
from typing import Any

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

_STYLE = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, sans-serif; background: #f5f7fa; color: #333;
         padding: 32px; }
  .wrap { max-width: 960px; margin: 0 auto; }
  .card { background: white; padding: 32px; border-radius: 16px;
          box-shadow: 0 4px 24px rgba(0,0,0,0.08); margin-bottom: 24px; }
  h1 { color: #1a3a6b; font-size: 24px; margin-bottom: 16px; font-weight: 700; }
  h2 { color: #1a3a6b; font-size: 18px; margin: 8px 0 16px; font-weight: 600; }
  p { line-height: 1.6; margin-bottom: 12px; }
  a { color: #1a3a6b; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #eef1f5;
           font-size: 14px; vertical-align: top; }
  th { color: #667; font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: 0.04em; }
  form.inline { display: inline; }
  label { display: block; margin-bottom: 12px; font-size: 14px; color: #445; }
  input, select { display: block; width: 100%; margin-top: 4px; padding: 8px 10px;
                  border: 1px solid #ccd; border-radius: 8px; font-size: 14px; }
  form.inline input, form.inline select { display: inline; width: auto; }
  button { background: #1a3a6b; color: white; border: none; padding: 8px 16px;
           border-radius: 8px; font-size: 14px; cursor: pointer; }
  button.warn { background: #c0392b; }
  .pill { display: inline-block; padding: 3px 10px; border-radius: 20px;
          font-size: 12px; font-weight: 600; }
  .pill.active { background: #e6f4ea; color: #1e7e34; }
  .pill.suspended, .pill.cancelled { background: #fdf0ef; color: #c0392b; }
  .error { background: #fdf0ef; color: #c0392b; padding: 10px 14px;
           border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
  .muted { color: #889; }
  .actions { display: flex; gap: 8px; align-items: center; }
  code { background: #eef1f5; padding: 2px 6px; border-radius: 6px; }
"""

_PAGE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title><style>{style}</style></head>
<body><div class="wrap">{body}</div></body>
</html>"""


def esc(value: Any) -> str:
    """HTML-escape a value (None -> empty string)."""

    return html.escape("" if value is None else str(value))


def page(title: str, body: str) -> str:
    """Wrap body HTML in the full page shell."""

    return _PAGE.replace("{title}", esc(title)).replace("{style}", _STYLE).replace(
        "{body}", body
    )


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a table. Header text is escaped; each cell is raw HTML.

    Callers escape their own text cells via ``esc`` and pass action markup
    (forms, links) as-is.
    """

    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    if not rows:
        empty = f'<tr><td colspan="{len(headers)}" class="muted">No rows.</td></tr>'
        body = empty
    else:
        body = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def form(
    action: str,
    fields_html: str,
    csrf: str | None,
    submit_label: str,
    *,
    inline: bool = False,
    button_class: str = "",
) -> str:
    """Render a POST form with a hidden CSRF token and a submit button."""

    csrf_field = (
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">' if csrf is not None else ""
    )
    cls = ' class="inline"' if inline else ""
    btn_cls = f' class="{esc(button_class)}"' if button_class else ""
    return (
        f'<form method="post" action="{esc(action)}"{cls}>'
        f"{csrf_field}{fields_html}"
        f"<button type=\"submit\"{btn_cls}>{esc(submit_label)}</button></form>"
    )


def _pill(status: Any) -> str:
    value = str(status or "").lower()
    return f'<span class="pill {esc(value)}">{esc(status or "—")}</span>'


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def render_error(message: str, *, status_label: str = "Error") -> str:
    body = f'<div class="card"><h1>{esc(status_label)}</h1><p>{esc(message)}</p></div>'
    return page(f"Sales Portal — {status_label}", body)


def render_login(error: str | None = None) -> str:
    msg = f'<div class="error">{esc(error)}</div>' if error else ""
    # Login has no session yet, so no CSRF token is bound; the admin key gates it.
    login_form = (
        '<form method="post" action="/portal/sales/login">'
        '<label>Admin key<input type="password" name="admin_key" autofocus></label>'
        '<button type="submit">Sign in</button></form>'
    )
    body = f'<div class="card"><h1>Headspace Sales Portal</h1>{msg}{login_form}</div>'
    return page("Sales Portal — Sign in", body)


def _tenant_row(tenant: dict[str, Any], csrf: str | None) -> list[str]:
    tenant_id = str(tenant.get("id"))
    plan = tenant.get("license_plan")
    if plan:
        summary = (
            f'{esc(plan)} &times;{esc(tenant.get("license_seats"))} '
            f"({esc(tenant.get('license_status'))})"
        )
    else:
        summary = '<span class="muted">no license</span>'
    active = str(tenant.get("status") or "").lower() == "active"
    toggle_label = "Suspend" if active else "Activate"
    toggle = form(
        f"/portal/sales/tenants/{esc(tenant_id)}/status",
        "",
        csrf,
        toggle_label,
        inline=True,
        button_class="warn" if active else "",
    )
    manage = f'<a href="/portal/sales/tenants/{esc(tenant_id)}/licenses">Licenses</a>'
    return [
        esc(tenant.get("display_name")),
        f'<code>{esc(tenant.get("microsoft_tenant_id"))}</code>',
        _pill(tenant.get("status")),
        summary,
        f'<div class="actions">{toggle}{manage}</div>',
    ]


def render_dashboard(tenants: list[dict[str, Any]], csrf: str | None) -> str:
    rows = [_tenant_row(t, csrf) for t in tenants]
    body = (
        '<div class="card">'
        "<h1>Tenants</h1>"
        '<p class="muted">'
        '<a href="/portal/sales/invite">Invite a tenant</a> &middot; '
        '<a href="/portal/sales/logout">Sign out</a></p>'
        + table(
            ["Tenant", "MS tenant", "Status", "License", "Actions"],
            rows,
        )
        + "</div>"
    )
    return page("Sales Portal — Tenants", body)


def _license_row(lic: dict[str, Any], csrf: str | None) -> list[str]:
    lic_id = str(lic.get("id"))
    status = str(lic.get("status") or "").lower()
    if status == "cancelled":
        action = '<span class="muted">—</span>'
    else:
        action = form(
            f"/portal/sales/licenses/{esc(lic_id)}/cancel",
            "",
            csrf,
            "Cancel",
            inline=True,
            button_class="warn",
        )
    return [
        esc(lic.get("plan")),
        esc(lic.get("seat_count")),
        _pill(lic.get("status")),
        esc(lic.get("ends_at") or "—"),
        esc(lic.get("notes") or ""),
        action,
    ]


def render_licenses(
    tenant: dict[str, Any], licenses: list[dict[str, Any]], tenant_id: str, csrf: str | None
) -> str:
    rows = [_license_row(lic, csrf) for lic in licenses]
    create_fields = (
        '<label>Plan<input type="text" name="plan" required></label>'
        '<label>Seats<input type="number" name="seat_count" value="1" min="1"></label>'
        '<label>Ends at (YYYY-MM-DD, optional)<input type="text" name="ends_at"></label>'
        '<label>Notes (optional)<input type="text" name="notes"></label>'
    )
    create = form(
        f"/portal/sales/tenants/{esc(tenant_id)}/licenses",
        create_fields,
        csrf,
        "Create license",
    )
    body = (
        '<div class="card">'
        f'<h1>Licenses — {esc(tenant.get("display_name"))}</h1>'
        '<p class="muted"><a href="/portal/sales">&larr; Back to tenants</a></p>'
        + table(["Plan", "Seats", "Status", "Ends at", "Notes", ""], rows)
        + "</div>"
        '<div class="card"><h2>New license</h2>' + create + "</div>"
    )
    return page("Sales Portal — Licenses", body)


def render_invite(link: str) -> str:
    body = (
        '<div class="card">'
        "<h1>Invite a tenant</h1>"
        "<p>Send this link to the tenant's Microsoft 365 administrator. They grant "
        "admin consent and their tenant is registered automatically.</p>"
        f"<p><code>{esc(link)}</code></p>"
        '<p class="muted">This is the existing admin-consent onboarding flow '
        "(<code>/auth/start</code>) — nothing else to configure.</p>"
        '<p class="muted"><a href="/portal/sales">&larr; Back to tenants</a></p>'
        "</div>"
    )
    return page("Sales Portal — Invite", body)
