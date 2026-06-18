from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.permissions import MCPPermissionError
from agents.mcp.tools import clickup_tools


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures: multi-list config + env helpers
# ---------------------------------------------------------------------------

IR_LIST_ID = "ir-list-uuid"
PIPELINE_LIST_ID = "pl-list-uuid"


def _write_multi_list_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "lists": {
            "investor_relations": {
                "list_id": IR_LIST_ID,
                "statuses": ["ACTIVE", "NOT A FIT", "DORMANT"],
                "fields": {
                    "Funder Type": {
                        "uuid": "fld-ir-ftype",
                        "type": "drop_down",
                        "options": [
                            {"uuid": "opt-found", "name": "Foundation", "orderindex": 0},
                            {"uuid": "opt-dfi", "name": "DFI", "orderindex": 1},
                            {"uuid": "opt-gov", "name": "Government", "orderindex": 2},
                        ],
                    },
                    "Typical Ticket Size": {
                        "uuid": "fld-ir-ticket",
                        "type": "short_text",
                    },
                    "Next Eligible Date": {
                        "uuid": "fld-ir-ned",
                        "type": "date",
                    },
                    "Linked Application": {
                        "uuid": "fld-ir-link",
                        "type": "task_relationship",
                    },
                    "Description (fund)": {
                        "uuid": "fld-ir-desc",
                        "type": "short_text",
                    },
                },
            },
            "fundraising_pipeline": {
                "list_id": PIPELINE_LIST_ID,
                "statuses": [
                    "LEAD IDENTIFIED",
                    "INTRO REQUESTED",
                    "INTRO COMPLETED",
                    "PROPOSAL SENT",
                    "EVALUATION IN PROGRESS",
                    "BOARD REVIEW",
                ],
                "fields": {
                    "Source Funder": {
                        "uuid": "fld-pl-src",
                        "type": "task_relationship",
                    },
                    "Application Folder URL": {
                        "uuid": "fld-pl-afu",
                        "type": "url",
                    },
                    "Probability": {
                        "uuid": "fld-pl-prob",
                        "type": "drop_down",
                        "options": [
                            {"uuid": "opt-high", "name": "High", "orderindex": 0},
                            {"uuid": "opt-med", "name": "Medium", "orderindex": 1},
                            {"uuid": "opt-low", "name": "Low", "orderindex": 2},
                        ],
                    },
                    "Estimated Amount": {
                        "uuid": "fld-pl-amt",
                        "type": "number",
                    },
                },
            },
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_legacy_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fields": {
            "Stage": {
                "id": "fld-stage",
                "type": "drop_down",
                "options": [{"id": "opt-id", "name": "Identified", "orderindex": 0}],
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _set_clickup_env(
    monkeypatch: pytest.MonkeyPatch,
    fields_path: Path,
    **overrides: str,
) -> None:
    env = {
        "CLICKUP_API_TOKEN": "tok-do-not-log",
        "CLICKUP_TEAM_ID": "team-uuid",
        "CLICKUP_IR_LIST_ID": IR_LIST_ID,
        "CLICKUP_PIPELINE_LIST_ID": PIPELINE_LIST_ID,
        "CLICKUP_BASE_URL": "https://api.clickup.test/api/v2",
        "CLICKUP_CUSTOM_FIELDS_CONFIG_PATH": str(fields_path),
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# 1. Permission gate denies when caller lacks fundraising_access.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_denied_without_fundraising_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with pytest.raises(MCPPermissionError):
        await clickup_tools.clickup_list_tasks(permissions=["m365_access"])


# ---------------------------------------------------------------------------
# 2. Write tool denies when caller has read_only and not admin_access.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_denied_for_read_only_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with pytest.raises(MCPPermissionError):
        await clickup_tools.clickup_update_task_field(
            task_id="tk1",
            field_name="Funder Type",
            value="Foundation",
            permissions=["fundraising_access", "read_only"],
        )


# ---------------------------------------------------------------------------
# 3. `not_configured` when CLICKUP_API_TOKEN is missing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_configured_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path, CLICKUP_API_TOKEN="")

    result = await clickup_tools.clickup_list_tasks(permissions=["fundraising_access"])
    assert result["status"] == "not_configured"
    assert "CLICKUP_API_TOKEN" in result["missing"]


# ---------------------------------------------------------------------------
# 4. `not_configured` when the config file is missing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_configured_when_config_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"  # not created
    _set_clickup_env(monkeypatch, fields_path)

    result = await clickup_tools.clickup_get_task(
        task_id="tk1",
        permissions=["fundraising_access"],
    )
    assert result["status"] == "not_configured"
    assert str(fields_path) in result["missing"]


# ---------------------------------------------------------------------------
# 5. clickup_update_task_field rejects invalid drop_down value.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_field_rejects_invalid_dropdown_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=False):
        result = await clickup_tools.clickup_update_task_field(
            task_id="tk1",
            field_name="Funder Type",
            value="NotARealType",
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "must be one of" in result["message"]


# ---------------------------------------------------------------------------
# 6. clickup_update_task_field rejects unknown field name.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_field_rejects_unknown_field_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=False):
        result = await clickup_tools.clickup_update_task_field(
            task_id="tk1",
            field_name="MysteryField",
            value="foo",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "unknown field name" in result["message"]


# ---------------------------------------------------------------------------
# 7. clickup_set_status rejects invalid status (replaces old set_stage test).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_rejects_invalid_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    task_payload = {
        "id": "tk1",
        "name": "Some funder",
        "list": {"id": IR_LIST_ID},
        "status": {"status": "ACTIVE"},
        "custom_fields": [],
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.clickup.test/api/v2/task/tk1").mock(
            return_value=httpx.Response(200, json=task_payload)
        )
        result = await clickup_tools.clickup_set_status(
            task_id="tk1",
            status="NOT_A_REAL_STATUS",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "investor_relations" in result["message"]


# ---------------------------------------------------------------------------
# 8. Successful read decodes custom fields from UUIDs to human names.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_decodes_custom_field_uuids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    payload = {
        "tasks": [
            {
                "id": "tk1",
                "name": "Acme Foundation",
                "status": {"status": "ACTIVE"},
                "custom_fields": [
                    {"id": "fld-ir-ftype", "value": "opt-found"},
                    {"id": "fld-ir-desc", "value": "African ed grants"},
                ],
            },
            {
                "id": "tk2",
                "name": "Beta DFI",
                "status": {"status": "DORMANT"},
                "custom_fields": [{"id": "fld-ir-ftype", "value": "opt-dfi"}],
            },
        ]
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(
            f"https://api.clickup.test/api/v2/list/{IR_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json=payload))
        result = await clickup_tools.clickup_list_tasks(
            list_key="investor_relations",
            status="ACTIVE",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["count"] == 1
    task = result["tasks"][0]
    assert task["task_id"] == "tk1"
    assert task["status"] == "ACTIVE"
    assert task["custom_fields"]["Funder Type"] == "Foundation"

    request = route.calls[0].request
    assert request.headers["authorization"] == "tok-do-not-log"
    # Critical: ClickUp uses raw token, NOT Bearer.
    assert not request.headers["authorization"].lower().startswith("bearer ")


# ---------------------------------------------------------------------------
# 9. Successful write sends correct payload (option_uuid, not name).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_field_sends_option_id_to_clickup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(
            "https://api.clickup.test/api/v2/task/tk1/field/fld-ir-ftype"
        ).mock(return_value=httpx.Response(200, json={}))

        result = await clickup_tools.clickup_update_task_field(
            task_id="tk1",
            field_name="Funder Type",
            value="DFI",
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )

    assert result == {
        "status": "ok",
        "task_id": "tk1",
        "list_key": "investor_relations",
        "field": "Funder Type",
        "value": "DFI",
    }
    body = json.loads(route.calls[0].request.content.decode())
    # The wire payload carries the option UUID, not the human name.
    assert body == {"value": "opt-dfi"}


# ---------------------------------------------------------------------------
# 10. API error returns {"status": "error", ...} and does NOT raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clickup_api_error_returns_error_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"https://api.clickup.test/api/v2/list/{IR_LIST_ID}/task").mock(
            return_value=httpx.Response(
                500, json={"err": "Server error", "ECODE": "OAUTH_019"}
            )
        )
        result = await clickup_tools.clickup_list_tasks(
            permissions=["fundraising_access"]
        )

    assert result["status"] == "error"
    assert result["code"] == 500
    assert "Server error" in result["message"]


# ---------------------------------------------------------------------------
# 11. clickup_set_status rejects invalid status name (basic case).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_rejects_status_not_in_any_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    task_payload = {
        "id": "tk1",
        "list": {"id": PIPELINE_LIST_ID},
        "status": {"status": "LEAD IDENTIFIED"},
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.clickup.test/api/v2/task/tk1").mock(
            return_value=httpx.Response(200, json=task_payload)
        )
        result = await clickup_tools.clickup_set_status(
            task_id="tk1",
            status="MADE UP STATUS",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "fundraising_pipeline" in result["message"]


# ---------------------------------------------------------------------------
# 12. clickup_set_status rejects a status valid on one list but not the other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_validates_against_correct_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ACTIVE is valid on IR but NOT on Pipeline; PROPOSAL SENT is the inverse."""

    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    # Task lives on Pipeline; ACTIVE (IR-only) must be rejected.
    pipeline_task = {
        "id": "tk-pl",
        "list": {"id": PIPELINE_LIST_ID},
        "status": {"status": "LEAD IDENTIFIED"},
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.clickup.test/api/v2/task/tk-pl").mock(
            return_value=httpx.Response(200, json=pipeline_task)
        )
        result = await clickup_tools.clickup_set_status(
            task_id="tk-pl",
            status="ACTIVE",  # valid on IR, NOT on Pipeline
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "fundraising_pipeline" in result["message"]

    # And the reverse: task on IR, status valid only on Pipeline.
    ir_task = {
        "id": "tk-ir",
        "list": {"id": IR_LIST_ID},
        "status": {"status": "ACTIVE"},
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.clickup.test/api/v2/task/tk-ir").mock(
            return_value=httpx.Response(200, json=ir_task)
        )
        result = await clickup_tools.clickup_set_status(
            task_id="tk-ir",
            status="PROPOSAL SENT",  # valid on Pipeline, NOT on IR
            permissions=["fundraising_access"],
        )

    assert result["status"] == "error"
    assert result["code"] == 400
    assert "investor_relations" in result["message"]


# ---------------------------------------------------------------------------
# 13. clickup_link_tasks rejects unknown link field name + wrong-type field.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_tasks_rejects_unknown_link_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=False):
        result = await clickup_tools.clickup_link_tasks(
            source_task_id="tk1",
            target_task_id="tk2",
            link_field_name="MysteryLink",
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )
    assert result["status"] == "error"
    assert result["code"] == 400
    assert "unknown field name" in result["message"]

    # Also reject when the field exists but is not a task_relationship.
    with respx.mock(assert_all_called=False):
        result = await clickup_tools.clickup_link_tasks(
            source_task_id="tk1",
            target_task_id="tk2",
            link_field_name="Typical Ticket Size",  # short_text, not relationship
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )
    assert result["status"] == "error"
    assert result["code"] == 400
    assert "task_relationship" in result["message"]


# ---------------------------------------------------------------------------
# 14. clickup_compute_pipeline_totals computes weighted + grouped sums.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_pipeline_totals_weighted_and_grouped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    payload = {
        "tasks": [
            {
                "id": "tk1",
                "status": {"status": "PROPOSAL SENT"},
                "custom_fields": [
                    {"id": "fld-pl-amt", "value": 100000},
                    {"id": "fld-pl-prob", "value": "opt-high"},
                ],
            },
            {
                "id": "tk2",
                "status": {"status": "PROPOSAL SENT"},
                "custom_fields": [
                    {"id": "fld-pl-amt", "value": 200000},
                    {"id": "fld-pl-prob", "value": "opt-med"},
                ],
            },
            {
                "id": "tk3",
                "status": {"status": "LEAD IDENTIFIED"},
                "custom_fields": [
                    {"id": "fld-pl-amt", "value": 50000},
                    {"id": "fld-pl-prob", "value": "opt-low"},
                ],
            },
        ]
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.clickup.test/api/v2/list/{PIPELINE_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json=payload))
        result = await clickup_tools.clickup_compute_pipeline_totals(
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    totals = result["totals"]
    assert totals["task_count"] == 3
    assert totals["grand_total"] == 350000
    assert totals["by_status"] == {
        "PROPOSAL SENT": 300000,
        "LEAD IDENTIFIED": 50000,
    }
    assert totals["by_probability"] == {
        "High": 100000,
        "Medium": 200000,
        "Low": 50000,
    }
    # Weighted: 100000*0.75 + 200000*0.40 + 50000*0.15 = 75000 + 80000 + 7500 = 162500
    assert totals["weighted"] == pytest.approx(162500)


# ---------------------------------------------------------------------------
# 15. clickup_compute_pipeline_totals returns zeros on empty list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_pipeline_totals_empty_list_returns_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"https://api.clickup.test/api/v2/list/{PIPELINE_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json={"tasks": []}))
        result = await clickup_tools.clickup_compute_pipeline_totals(
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    totals = result["totals"]
    assert totals == {
        "by_status": {},
        "by_probability": {},
        "weighted": 0.0,
        "grand_total": 0.0,
        "task_count": 0,
    }


# ---------------------------------------------------------------------------
# 16. clickup_list_tasks(list_key="fundraising_pipeline") hits Pipeline, not IR.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_routes_to_pipeline_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        pipeline_route = mock.get(
            f"https://api.clickup.test/api/v2/list/{PIPELINE_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json={"tasks": []}))
        # IR route would NOT be called — respx will fail loudly if it were.
        result = await clickup_tools.clickup_list_tasks(
            list_key="fundraising_pipeline",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["list_key"] == "fundraising_pipeline"
    assert pipeline_route.call_count == 1


# ---------------------------------------------------------------------------
# 17. Legacy flat config raises a clear error pointing to the setup script.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_config_is_rejected_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_legacy_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    result = await clickup_tools.clickup_list_tasks(
        permissions=["fundraising_access"]
    )
    assert result["status"] == "error"
    assert result["code"] == 500
    assert "scripts/setup_clickup_fields.py" in result["message"]
    assert "legacy" in result["message"].lower()


# ---------------------------------------------------------------------------
# 18. clickup_create_subtask — dynamic list resolution (#7B).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_subtask_with_known_list_key_uses_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Backward compatible: a configured list_key still creates in that list.
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(
            f"https://api.clickup.test/api/v2/list/{IR_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json={"id": "sub-cfg"}))
        result = await clickup_tools.clickup_create_subtask(
            parent_task_id="pt1",
            name="Configured subtask",
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["subtask_id"] == "sub-cfg"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["parent"] == "pt1"
    assert body["name"] == "Configured subtask"


@pytest.mark.asyncio
async def test_create_subtask_with_raw_list_id_bypasses_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(
            "https://api.clickup.test/api/v2/list/raw-list-999/task"
        ).mock(return_value=httpx.Response(200, json={"id": "sub-raw"}))
        result = await clickup_tools.clickup_create_subtask(
            parent_task_id="pt1",
            name="Raw subtask",
            description="hello",
            list_id="raw-list-999",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["subtask_id"] == "sub-raw"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["parent"] == "pt1"
    assert body["description"] == "hello"


@pytest.mark.asyncio
async def test_create_subtask_without_list_key_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # list_key=None and no list_id → create directly under the parent task.
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.clickup.test/api/v2/task/pt1").mock(
            return_value=httpx.Response(200, json={"id": "sub-direct"})
        )
        result = await clickup_tools.clickup_create_subtask(
            parent_task_id="pt1",
            name="No-list subtask",
            list_key=None,
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["subtask_id"] == "sub-direct"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["name"] == "No-list subtask"
    # parent is in the URL for the direct endpoint, not the body.
    assert "parent" not in body


@pytest.mark.asyncio
async def test_create_subtask_unknown_list_key_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_multi_list_config(fields_path)
    _set_clickup_env(monkeypatch, fields_path)

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.clickup.test/api/v2/task/pt1").mock(
            return_value=httpx.Response(200, json={"id": "sub-unknown"})
        )
        result = await clickup_tools.clickup_create_subtask(
            parent_task_id="pt1",
            name="Adhoc subtask",
            list_key="not_a_real_list",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    assert result["subtask_id"] == "sub-unknown"
