from __future__ import annotations

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.permissions import MCPPermissionError
from agents.mcp.tools import clickup_write_tools as cwt

BASE = "https://api.clickup.test/api/v2"
TEAM_ID = "90121402212"
PERMS = ["fundraising_access"]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "CLICKUP_API_TOKEN": "tok-do-not-log",
        "CLICKUP_TEAM_ID": TEAM_ID,
        "CLICKUP_BASE_URL": BASE,
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# Permission / config gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_denied_without_fundraising_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(MCPPermissionError):
        await cwt.clickup_get_spaces(permissions=["m365_access"])


@pytest.mark.asyncio
async def test_write_denied_for_read_only_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with pytest.raises(MCPPermissionError):
        await cwt.clickup_create_space(
            space_name="X", permissions=["fundraising_access", "read_only"]
        )


@pytest.mark.asyncio
async def test_not_configured_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, CLICKUP_API_TOKEN="")
    result = await cwt.clickup_get_spaces(permissions=PERMS)
    assert result["status"] == "not_configured"
    assert "CLICKUP_API_TOKEN" in result["missing"]


# ---------------------------------------------------------------------------
# 1. clickup_get_spaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_spaces_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {"spaces": [{"id": "sp1", "name": "Fundraising", "status": "open"}]}

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/team/{TEAM_ID}/space").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_get_spaces(permissions=PERMS)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["spaces"][0]["id"] == "sp1"
    # ClickUp v2 uses a raw token, NOT Bearer.
    assert route.calls[0].request.headers["authorization"] == "tok-do-not-log"


@pytest.mark.asyncio
async def test_get_spaces_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/team/{TEAM_ID}/space").mock(
            return_value=httpx.Response(401, json={"err": "Token invalid"})
        )
        result = await cwt.clickup_get_spaces(permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 401


# ---------------------------------------------------------------------------
# 2. clickup_get_folders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_folders_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {"folders": [{"id": "fd1", "name": "Grants", "task_count": "7"}]}

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/space/sp1/folder").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_get_folders(space_id="sp1", permissions=PERMS)

    assert result["status"] == "ok"
    assert result["folders"][0]["id"] == "fd1"
    assert result["folders"][0]["task_count"] == "7"


@pytest.mark.asyncio
async def test_get_folders_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/space/sp1/folder").mock(
            return_value=httpx.Response(404, json={"err": "Space not found"})
        )
        result = await cwt.clickup_get_folders(space_id="sp1", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 404


# ---------------------------------------------------------------------------
# 3. clickup_create_folder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/space/sp1/folder").mock(
            return_value=httpx.Response(200, json={"id": "fd9", "name": "Q3"})
        )
        result = await cwt.clickup_create_folder(
            space_id="sp1", folder_name="Q3", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["folder_id"] == "fd9"
    assert result["name"] == "Q3"
    assert "Q3" in route.calls[0].request.content.decode()


@pytest.mark.asyncio
async def test_create_folder_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/space/sp1/folder").mock(
            return_value=httpx.Response(400, json={"err": "Bad name"})
        )
        result = await cwt.clickup_create_folder(
            space_id="sp1", folder_name="Q3", permissions=PERMS
        )

    assert result["status"] == "error"
    assert result["code"] == 400


# ---------------------------------------------------------------------------
# 4. clickup_create_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_list_in_folder_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(200, json={"id": "ls1", "name": "Applications"})
        )
        result = await cwt.clickup_create_list(
            list_name="Applications", folder_id="fd1", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["list_id"] == "ls1"


@pytest.mark.asyncio
async def test_create_list_in_space_when_no_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/space/sp1/list").mock(
            return_value=httpx.Response(200, json={"id": "ls2", "name": "Ad hoc"})
        )
        result = await cwt.clickup_create_list(
            list_name="Ad hoc", space_id="sp1", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["list_id"] == "ls2"


@pytest.mark.asyncio
async def test_create_list_prefers_folder_when_both_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        # Only the folder route is registered → asserts the space route is unused.
        mock.post(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(200, json={"id": "ls1", "name": "L"})
        )
        result = await cwt.clickup_create_list(
            list_name="L", folder_id="fd1", space_id="sp1", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["list_id"] == "ls1"


@pytest.mark.asyncio
async def test_create_list_requires_folder_or_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    result = await cwt.clickup_create_list(list_name="L", permissions=PERMS)
    assert result["status"] == "error"
    assert result["code"] == 400


@pytest.mark.asyncio
async def test_create_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(400, json={"err": "Bad"})
        )
        result = await cwt.clickup_create_list(
            list_name="L", folder_id="fd1", permissions=PERMS
        )

    assert result["status"] == "error"
    assert result["code"] == 400


# ---------------------------------------------------------------------------
# 5. clickup_create_space
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_space_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/team/{TEAM_ID}/space").mock(
            return_value=httpx.Response(200, json={"id": "sp9", "name": "Ops"})
        )
        result = await cwt.clickup_create_space(
            space_name="Ops", is_private=True, permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["space_id"] == "sp9"
    body = route.calls[0].request.content.decode()
    assert "Ops" in body
    assert "true" in body.lower()


@pytest.mark.asyncio
async def test_create_space_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/team/{TEAM_ID}/space").mock(
            return_value=httpx.Response(403, json={"err": "No access"})
        )
        result = await cwt.clickup_create_space(space_name="Ops", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 403


# ---------------------------------------------------------------------------
# 6. clickup_delete_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_task_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.delete(f"{BASE}/task/tk1").mock(return_value=httpx.Response(204))
        result = await cwt.clickup_delete_task(task_id="tk1", permissions=PERMS)

    assert result["status"] == "ok"
    assert result["deleted"] is True
    assert result["task_id"] == "tk1"


@pytest.mark.asyncio
async def test_delete_task_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.delete(f"{BASE}/task/tk1").mock(
            return_value=httpx.Response(404, json={"err": "Task not found"})
        )
        result = await cwt.clickup_delete_task(task_id="tk1", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 404


# ---------------------------------------------------------------------------
# 7. clickup_get_members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_members_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {
        "members": [
            {"id": 11, "username": "Thandi", "email": "thandi@nichegroup.africa"}
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/list/ls1/member").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_get_members(list_id="ls1", permissions=PERMS)

    assert result["status"] == "ok"
    assert result["members"][0]["username"] == "Thandi"
    assert result["members"][0]["email"] == "thandi@nichegroup.africa"


@pytest.mark.asyncio
async def test_get_members_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/list/ls1/member").mock(
            return_value=httpx.Response(404, json={"err": "List not found"})
        )
        result = await cwt.clickup_get_members(list_id="ls1", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 404


# ---------------------------------------------------------------------------
# 8. clickup_create_form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_form_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/list/ls1/view").mock(
            return_value=httpx.Response(
                200, json={"view": {"id": "vw1", "name": "Intake", "type": "form"}}
            )
        )
        result = await cwt.clickup_create_form(
            list_id="ls1", form_name="Intake", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["form_id"] == "vw1"
    assert result["name"] == "Intake"
    body = route.calls[0].request.content.decode()
    assert "form" in body


@pytest.mark.asyncio
async def test_create_form_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/list/ls1/view").mock(
            return_value=httpx.Response(400, json={"err": "Bad view"})
        )
        result = await cwt.clickup_create_form(
            list_id="ls1", form_name="Intake", permissions=PERMS
        )

    assert result["status"] == "error"
    assert result["code"] == 400


# ---------------------------------------------------------------------------
# 9. clickup_get_lists (dynamic list discovery)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_lists_in_folder_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {
        "lists": [
            {"id": "ls1", "name": "Applications", "task_count": "12", "status": None}
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_get_lists(folder_id="fd1", permissions=PERMS)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["lists"][0]["id"] == "ls1"
    assert result["lists"][0]["task_count"] == "12"


@pytest.mark.asyncio
async def test_get_lists_in_space_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {"lists": [{"id": "ls7", "name": "Folderless", "task_count": "3"}]}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/space/sp1/list").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_get_lists(space_id="sp1", permissions=PERMS)

    assert result["status"] == "ok"
    assert result["lists"][0]["id"] == "ls7"


@pytest.mark.asyncio
async def test_get_lists_prefers_folder_when_both_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        # Only the folder route is registered → asserts the space route is unused.
        mock.get(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(200, json={"lists": [{"id": "ls1", "name": "L"}]})
        )
        result = await cwt.clickup_get_lists(
            folder_id="fd1", space_id="sp1", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["lists"][0]["id"] == "ls1"


@pytest.mark.asyncio
async def test_get_lists_requires_folder_or_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    result = await cwt.clickup_get_lists(permissions=PERMS)
    assert result["status"] == "error"
    assert result["code"] == 400


@pytest.mark.asyncio
async def test_get_lists_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/folder/fd1/list").mock(
            return_value=httpx.Response(404, json={"err": "Folder not found"})
        )
        result = await cwt.clickup_get_lists(folder_id="fd1", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 404


# ---------------------------------------------------------------------------
# 10. clickup_list_tasks_by_id (config-free dynamic task access)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_by_id_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    payload = {
        "tasks": [
            {
                "id": "tk1",
                "name": "Acme grant",
                "status": {"status": "ACTIVE"},
                "assignees": [{"id": 11, "username": "Thandi"}],
                "due_date": "1735689600000",
                "priority": {"priority": "high"},
                "description": "Renewal application",
            }
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/list/anylist123/task").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_list_tasks_by_id(
            list_id="anylist123", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["list_id"] == "anylist123"
    task = result["tasks"][0]
    assert task["id"] == "tk1"
    assert task["status"] == "ACTIVE"
    assert task["assignee"] == ["Thandi"]
    assert task["priority"] == "high"
    assert task["description"] == "Renewal application"
    # Raw token, NOT Bearer.
    assert route.calls[0].request.headers["authorization"] == "tok-do-not-log"


@pytest.mark.asyncio
async def test_list_tasks_by_id_applies_limit_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch)
    payload = {
        "tasks": [
            {"id": "tk1", "name": "A", "status": {"status": "OPEN"}},
            {"id": "tk2", "name": "B", "status": {"status": "OPEN"}},
            {"id": "tk3", "name": "C", "status": {"status": "OPEN"}},
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/list/ls1/task").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cwt.clickup_list_tasks_by_id(
            list_id="ls1", limit=2, status="OPEN", assignee="11", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["count"] == 2  # client-side limit applied
    sent_url = str(route.calls[0].request.url)
    assert "statuses%5B%5D=OPEN" in sent_url or "statuses[]=OPEN" in sent_url
    assert "assignees%5B%5D=11" in sent_url or "assignees[]=11" in sent_url


@pytest.mark.asyncio
async def test_list_tasks_by_id_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/list/ls1/task").mock(
            return_value=httpx.Response(404, json={"err": "List not found"})
        )
        result = await cwt.clickup_list_tasks_by_id(list_id="ls1", permissions=PERMS)

    assert result["status"] == "error"
    assert result["code"] == 404
