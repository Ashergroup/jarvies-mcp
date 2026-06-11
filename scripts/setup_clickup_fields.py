"""Idempotent ClickUp custom-field setup for the IR and Pipeline lists.

Usage (from the jarvies-mcp project root):
    python scripts/setup_clickup_fields.py

What this does:
1. Reads CLICKUP_API_TOKEN, CLICKUP_TEAM_ID, CLICKUP_IR_LIST_ID,
   CLICKUP_PIPELINE_LIST_ID and CLICKUP_CUSTOM_FIELDS_CONFIG_PATH from
   settings (i.e. `.env`).
2. For each list, in order:
   a. GET `/list/{list_id}` — read the list metadata. ClickUp returns the
      list's configured native statuses; record them in the config.
   b. GET `/list/{list_id}/field` — enumerate existing custom fields.
   c. For each field defined in the per-list schema:
      - If a field with the same name already exists, record its UUID.
      - Otherwise, POST `/list/{list_id}/field` (best-effort — ClickUp's
        public v2 API does not formally document a create-field endpoint).
      - On creation failure, mark the field PENDING.
3. Writes the multi-list config to CLICKUP_CUSTOM_FIELDS_CONFIG_PATH.
4. Prints a per-list report showing statuses detected, plus fields reused,
   created, and pending.
5. Exits non-zero if ANY field is still pending in either list.

Idempotent: re-running with all fields present reports 0 created, 0 pending,
N reused per list.

Schema is FINAL — only the fields listed in `LISTS` are managed. The script
does NOT recreate fields that already exist on either list with the same
name; it records and re-uses their UUIDs.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.mcp.config import get_settings  # noqa: E402

# ---------------------------------------------------------------------------
# Per-list schema. Only fields listed here are managed by this script.
#
# Fields the user already has on each list (e.g. "Description (fund)",
# "Investor Type", etc.) are not declared here. They will be detected on
# GET /list/{id}/field and recorded under their existing UUID, but they are
# not part of the schema this script tries to create.
# ---------------------------------------------------------------------------

IR_KEY = "investor_relations"
PIPELINE_KEY = "fundraising_pipeline"


LISTS: dict[str, dict[str, Any]] = {
    IR_KEY: {
        "env_var": "CLICKUP_IR_LIST_ID",
        "display": "Investor Relations",
        # Managed fields — created if missing, reused if a name match exists.
        "managed_fields": [
            {
                "name": "Funder Type",
                "type": "drop_down",
                "options": [
                    "Foundation",
                    "DFI",
                    "Government",
                    "Corporate",
                    "Multilateral",
                    "Other",
                ],
            },
            {"name": "Typical Ticket Size", "type": "short_text"},
            {"name": "Application Cycle", "type": "short_text"},
            {"name": "Eligibility Notes", "type": "long_text"},
            {"name": "Next Eligible Date", "type": "date"},
            {"name": "Linked Application", "type": "task_relationship"},
        ],
        # Pre-existing fields we expect on the list. We don't create these —
        # we just record their UUIDs once we see them in the GET response.
        "expected_existing": [
            "Description (fund)",
            "Primary Contact",
            "Last Contact Date",
            "Preferred Update Frequency",
            "Sector Interest",
            "Location Focus",
            "Website Link",
        ],
    },
    PIPELINE_KEY: {
        "env_var": "CLICKUP_PIPELINE_LIST_ID",
        "display": "Fundraising Pipeline",
        "managed_fields": [
            {"name": "Source Funder", "type": "task_relationship"},
            {"name": "Application Folder URL", "type": "url"},
            {"name": "Requirements Source", "type": "short_text"},
            {"name": "Submission Deadline", "type": "date"},
            {
                "name": "Probability",
                "type": "drop_down",
                "options": ["High", "Medium", "Low"],
            },
            {"name": "Decision Date", "type": "date"},
        ],
        "expected_existing": [
            "Investor Type",
            "Lead Partner",
            "Estimated Amount",
        ],
    },
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": raw}


def _list_metadata(base_url: str, list_id: str, token: str) -> dict[str, Any]:
    status, payload = _request("GET", f"{base_url}/list/{list_id}", token)
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(
            f"ERROR: GET /list/{list_id} returned HTTP {status}.\n"
            f"Body: {json.dumps(payload, indent=2)[:1000]}"
        )
    return payload


def _list_existing_fields(base_url: str, list_id: str, token: str) -> list[dict[str, Any]]:
    status, payload = _request("GET", f"{base_url}/list/{list_id}/field", token)
    if status != 200:
        raise SystemExit(
            f"ERROR: GET /list/{list_id}/field returned HTTP {status}.\n"
            f"Body: {json.dumps(payload, indent=2)[:1000]}"
        )
    if not isinstance(payload, dict) or "fields" not in payload:
        raise SystemExit(
            f"ERROR: unexpected response from /list/{list_id}/field: "
            f"{json.dumps(payload, indent=2)[:1000]}"
        )
    fields = payload.get("fields") or []
    if not isinstance(fields, list):
        raise SystemExit(f"ERROR: 'fields' was not a list: {fields!r}")
    return fields


def _existing_entry(name: str, existing: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in existing:
        if entry.get("name") == name:
            return entry
    return None


def _build_create_body(spec: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"name": spec["name"], "type": spec["type"]}
    if spec["type"] in {"drop_down", "labels"}:
        body["type_config"] = {
            "options": [
                {"name": opt, "orderindex": idx}
                for idx, opt in enumerate(spec.get("options", []))
            ]
        }
    return body


def _attempt_create(
    base_url: str,
    list_id: str,
    token: str,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    status, payload = _request(
        "POST",
        f"{base_url}/list/{list_id}/field",
        token,
        body=_build_create_body(spec),
    )
    if status in (200, 201) and isinstance(payload, dict) and payload.get("id"):
        return payload
    return None


def _normalise_options(
    spec_options: list[str], api_field: dict[str, Any]
) -> list[dict[str, Any]]:
    type_config = api_field.get("type_config") or {}
    api_options = type_config.get("options") or []
    by_name = {opt.get("name"): opt for opt in api_options if isinstance(opt, dict)}
    out: list[dict[str, Any]] = []
    for idx, name in enumerate(spec_options):
        opt = by_name.get(name)
        if opt is None:
            print(
                f"    WARNING: option '{name}' missing from ClickUp field "
                f"'{api_field.get('name')}' — add it in the UI."
            )
            continue
        out.append(
            {
                "uuid": opt.get("id"),
                "name": name,
                "orderindex": opt.get("orderindex", idx),
            }
        )
    return out


def _existing_field_entry(api_field: dict[str, Any]) -> dict[str, Any]:
    """Project a pre-existing ClickUp field (one we did not create) into the
    config shape. Includes options for drop_down / labels."""

    ftype = api_field.get("type", "")
    entry: dict[str, Any] = {"uuid": api_field.get("id"), "type": ftype}
    if ftype in {"drop_down", "labels"}:
        type_config = api_field.get("type_config") or {}
        api_options = type_config.get("options") or []
        entry["options"] = [
            {
                "uuid": opt.get("id"),
                "name": opt.get("name"),
                "orderindex": opt.get("orderindex", idx),
            }
            for idx, opt in enumerate(api_options)
            if isinstance(opt, dict)
        ]
    return entry


# ---------------------------------------------------------------------------
# Per-list processing
# ---------------------------------------------------------------------------


def _process_list(
    base_url: str,
    list_id: str,
    token: str,
    list_spec: dict[str, Any],
) -> dict[str, Any]:
    """Process one list — return the config block plus a per-list report."""

    meta = _list_metadata(base_url, list_id, token)
    raw_statuses = meta.get("statuses") or []
    statuses: list[str] = []
    for s in raw_statuses:
        if isinstance(s, dict) and isinstance(s.get("status"), str):
            statuses.append(s["status"])
        elif isinstance(s, str):
            statuses.append(s)

    existing = _list_existing_fields(base_url, list_id, token)

    out_fields: dict[str, dict[str, Any]] = {}
    created: list[str] = []
    reused: list[str] = []
    pending: list[str] = []

    # Pre-existing fields are recorded by name if present on the list.
    for name in list_spec.get("expected_existing", []):
        api_field = _existing_entry(name, existing)
        if api_field is None:
            continue
        out_fields[name] = _existing_field_entry(api_field)
        reused.append(name)

    # Managed fields are created if missing, reused if present.
    for spec in list_spec.get("managed_fields", []):
        name = spec["name"]
        ftype = spec["type"]
        api_field = _existing_entry(name, existing)
        if api_field is None:
            api_field = _attempt_create(base_url, list_id, token, spec)
            if api_field is not None:
                created.append(name)
        else:
            reused.append(name)

        if api_field is None:
            pending.append(name)
            continue

        entry: dict[str, Any] = {"uuid": api_field.get("id"), "type": ftype}
        if ftype in {"drop_down", "labels"}:
            entry["options"] = _normalise_options(spec.get("options", []), api_field)
        out_fields[name] = entry

    return {
        "block": {
            "list_id": list_id,
            "statuses": statuses,
            "fields": out_fields,
        },
        "report": {
            "statuses": statuses,
            "created": created,
            "reused": reused,
            "pending": pending,
        },
    }


def _print_report(list_display: str, list_id: str, report: dict[str, Any]) -> None:
    statuses = report["statuses"]
    created = report["created"]
    reused = report["reused"]
    pending = report["pending"]

    print()
    print("-" * 70)
    print(f"  {list_display}  ({list_id})")
    print("-" * 70)
    print(f"  Statuses detected ({len(statuses)}): {', '.join(statuses) or '(none)'}")
    print(f"  Reused : {len(reused):>3}   {', '.join(reused) or '(none)'}")
    print(f"  Created: {len(created):>3}   {', '.join(created) or '(none)'}")
    print(f"  Pending: {len(pending):>3}   {', '.join(pending) or '(none)'}")
    if pending:
        print()
        print("  Add the pending fields manually in the ClickUp UI on this list")
        print("  (Customize → Fields), then re-run this script:")
        for name in pending:
            print(f"    - {name}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    settings = get_settings()
    token = settings.clickup_api_token
    team_id = settings.clickup_team_id
    ir_list_id = settings.clickup_ir_list_id
    pipeline_list_id = settings.clickup_pipeline_list_id
    config_path = settings.clickup_fields_config_path
    base_url = settings.clickup_base_url.rstrip("/")

    missing_env = [
        name
        for name, value in (
            ("CLICKUP_API_TOKEN", token),
            ("CLICKUP_TEAM_ID", team_id),
            ("CLICKUP_IR_LIST_ID", ir_list_id),
            ("CLICKUP_PIPELINE_LIST_ID", pipeline_list_id),
        )
        if not value
    ]
    if missing_env:
        print("ERROR: Missing required env vars:")
        for name in missing_env:
            print(f"  - {name}")
        return 1

    print("=" * 70)
    print(f"ClickUp multi-list setup — team_id={team_id}")
    print("=" * 70)

    lists_block: dict[str, dict[str, Any]] = {}
    reports: dict[str, dict[str, Any]] = {}
    list_ids = {IR_KEY: ir_list_id, PIPELINE_KEY: pipeline_list_id}

    for key, spec in LISTS.items():
        list_id = list_ids[key]
        result = _process_list(base_url, list_id, token, spec)
        lists_block[key] = result["block"]
        reports[key] = result["report"]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"lists": lists_block}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print()
    print(f"Config written to: {config_path}")

    total_pending = 0
    for key, spec in LISTS.items():
        _print_report(spec["display"], list_ids[key], reports[key])
        total_pending += len(reports[key]["pending"])

    print()
    print("=" * 70)
    if total_pending:
        print(f"FAILED — {total_pending} field(s) still pending across lists.")
        return 1
    print("OK — all fields present.")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
