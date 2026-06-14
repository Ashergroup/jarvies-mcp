"""ClickUp MCP tools — fundraising read/write across two lists.

Lists:
- ``investor_relations`` — long-term funder relationship database.
- ``fundraising_pipeline`` — active grant/funder applications in flight.

Native ClickUp statuses (configured in the workspace) are the workflow.
Tools never create or modify statuses — they only read them and validate
status names against the per-list set written by the setup script.

Auth: ClickUp uses a flat personal-access-style token. The ``Authorization``
header carries the raw token value (NOT ``Bearer <token>``) — a v2 quirk.

Layering: HTTP is folded into this module to match the cin7 layering
(this repo has no ``services/`` package). The service class is constructed
per tool call and closed in a ``finally`` block.

Config: ``setup_clickup_fields.py`` produces the JSON at
``CLICKUP_CUSTOM_FIELDS_CONFIG_PATH`` in the multi-list shape:

    {
      "lists": {
        "investor_relations": {
          "list_id": "...",
          "statuses": ["ACTIVE", "NOT A FIT", "DORMANT"],
          "fields": {"Funder Type": {"uuid": ..., "type": ..., "options": ...}, ...}
        },
        "fundraising_pipeline": {...}
      }
    }

Error envelope: API errors come back as
``{"status": "error", "code": <http>, "message": <str>}`` and never raise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from agents.mcp.config import MCPSettings, get_settings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant import current_tenant, get_tenant_credentials
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

IR_KEY = "investor_relations"
PIPELINE_KEY = "fundraising_pipeline"

_DEFAULT_NEEDING_WORK: dict[str, list[str]] = {
    IR_KEY: ["ACTIVE"],
    PIPELINE_KEY: [
        "LEAD IDENTIFIED",
        "INTRO REQUESTED",
        "INTRO COMPLETED",
        "PROPOSAL SENT",
    ],
}

_PROBABILITY_WEIGHTS = {"High": 0.75, "Medium": 0.40, "Low": 0.15}

_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
_COMMENT_RETURN_LIMIT = 20


# ---------------------------------------------------------------------------
# Lists config
# ---------------------------------------------------------------------------


class ClickUpConfigError(Exception):
    """Raised when the config file exists but is in the wrong shape."""


class ClickUpListConfig:
    """One entry under config['lists'][list_key]."""

    def __init__(self, key: str, data: dict[str, Any]):
        self.key = key
        self.list_id: str = data.get("list_id", "") or ""
        self.statuses: list[str] = list(data.get("statuses") or [])
        raw_fields = data.get("fields") or {}
        self.fields: dict[str, dict[str, Any]] = raw_fields
        self._by_uuid: dict[str, tuple[str, dict[str, Any]]] = {}
        for name, entry in raw_fields.items():
            uuid = (entry or {}).get("uuid")
            if uuid:
                self._by_uuid[uuid] = (name, entry)

    def field_by_name(self, name: str) -> dict[str, Any] | None:
        return self.fields.get(name)

    def field_by_uuid(self, uuid: str) -> tuple[str, dict[str, Any]] | None:
        return self._by_uuid.get(uuid)

    def has_status(self, status: str) -> bool:
        # ClickUp displays statuses in upper case but normalises on the
        # server side; match case-insensitively to be forgiving.
        target = status.strip().lower()
        return any(target == s.strip().lower() for s in self.statuses)


class ClickUpListsConfig:
    """Multi-list config produced by ``setup_clickup_fields.py``.

    Shape::

        {"lists": {"investor_relations": {...}, "fundraising_pipeline": {...}}}

    The old flat shape (``{"fields": {...}}``) is detected and rejected — see
    ``from_path``.
    """

    def __init__(self, raw: dict[str, Any]):
        lists_block = raw.get("lists")
        if not isinstance(lists_block, dict):
            raise ClickUpConfigError(
                "ClickUp config is missing the 'lists' block. Re-run "
                "scripts/setup_clickup_fields.py to regenerate it."
            )
        self.lists: dict[str, ClickUpListConfig] = {
            key: ClickUpListConfig(key, entry or {}) for key, entry in lists_block.items()
        }

    def get(self, list_key: str) -> ClickUpListConfig | None:
        return self.lists.get(list_key)

    def find_by_list_id(self, list_id: str) -> ClickUpListConfig | None:
        for entry in self.lists.values():
            if entry.list_id and entry.list_id == list_id:
                return entry
        return None

    @classmethod
    def from_path(cls, path) -> "ClickUpListsConfig | None | str":
        """Load the config file.

        Returns the config object on success, ``None`` if the file does not
        exist or cannot be parsed, or the literal string ``"legacy"`` when
        the file is the old flat ``{"fields": {...}}`` shape and the operator
        needs to re-run the setup script.
        """

        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if "lists" not in data and "fields" in data:
            return "legacy"
        try:
            return cls(data)
        except ClickUpConfigError:
            return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ClickUpAPIError(Exception):
    """ClickUp returned non-2xx. Carries ``code`` and ``message``."""

    def __init__(self, code: int, message: str):
        super().__init__(f"ClickUp HTTP {code}: {message}")
        self.code = code
        self.message = message


class ClickUpService:
    """Async ClickUp v2 client; constructed per tool call.

    Errors are raised as ``ClickUpAPIError``; the tool layer catches them and
    converts to the ``{status, code, message}`` dict shape.
    """

    def __init__(self, settings: MCPSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.integration_http_timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._settings.clickup_api_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        client = await self._http()
        url = f"{self._settings.clickup_base_url.rstrip('/')}/{path.lstrip('/')}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            response = await client.request(
                method,
                url,
                headers=self._headers(),
                params=clean_params or None,
                json=json_body,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            log.info(
                "clickup_api_call",
                extra={
                    "method": method,
                    "path": f"/{path.lstrip('/')}",
                    "status": response.status_code,
                    "latency_ms": round(latency_ms, 1),
                    "attempt": attempt,
                },
            )
            if response.status_code == 429 and attempt < _RETRY_MAX_ATTEMPTS:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        float(retry_after)
                        if retry_after
                        else _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    )
                except ValueError:
                    delay = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
                continue
            if not response.is_success:
                message = _extract_clickup_error(response)
                raise ClickUpAPIError(response.status_code, message)
            if not response.content:
                return {}
            return response.json()

        raise ClickUpAPIError(429, "ClickUp rate limit exceeded after retries")

    async def list_tasks(
        self,
        list_id: str,
        page: int = 0,
        include_closed: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"list/{list_id}/task",
            params={
                "page": page,
                "include_closed": "true" if include_closed else "false",
                "subtasks": "false",
            },
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"task/{task_id}",
            params={
                "include_subtasks": "true",
                "include_markdown_description": "true",
            },
        )

    async def get_comments(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"task/{task_id}/comment")

    async def set_custom_field(
        self,
        task_id: str,
        field_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"task/{task_id}/field/{field_id}",
            json_body=payload,
        )

    async def post_comment(
        self,
        task_id: str,
        comment_text: str,
        notify_all: bool,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"task/{task_id}/comment",
            json_body={"comment_text": comment_text, "notify_all": notify_all},
        )

    async def create_task(
        self,
        list_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"list/{list_id}/task",
            json_body=body,
        )

    async def update_task(
        self,
        task_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"task/{task_id}",
            json_body=body,
        )


def _extract_clickup_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase or "request failed"
    if isinstance(payload, dict):
        return str(
            payload.get("err")
            or payload.get("error")
            or payload.get("ECODE")
            or response.text
            or "request failed"
        )
    return str(payload)


# ---------------------------------------------------------------------------
# Field encoding / decoding
# ---------------------------------------------------------------------------


def _encode_value(field_entry: dict[str, Any], value: Any) -> dict[str, Any]:
    """Build the ``POST /task/{id}/field/{field_id}`` payload.

    Raises ValueError when the value is incompatible with the field type.
    """

    ftype = field_entry.get("type", "")
    if ftype == "drop_down":
        options = field_entry.get("options", [])
        for opt in options:
            if opt.get("name") == value:
                return {"value": opt["uuid"]}
        valid = ", ".join(o.get("name", "") for o in options)
        raise ValueError(f"value must be one of: {valid}")
    if ftype == "labels":
        if not isinstance(value, list):
            raise ValueError("labels value must be a list of strings")
        options = field_entry.get("options", [])
        name_to_id = {o.get("name"): o.get("uuid") for o in options}
        add: list[str] = []
        for item in value:
            opt_id = name_to_id.get(item)
            if opt_id is None:
                raise ValueError(f"label '{item}' not in configured options")
            add.append(opt_id)
        return {"value": {"add": add, "rem": []}}
    if ftype == "date":
        if isinstance(value, bool):
            raise ValueError("date must be YYYY-MM-DD or epoch ms")
        if isinstance(value, (int, float)):
            return {"value": int(value)}
        if isinstance(value, str):
            try:
                struct = time.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("date must be YYYY-MM-DD or epoch ms") from exc
            return {"value": int(time.mktime(struct) * 1000)}
        raise ValueError("date must be YYYY-MM-DD or epoch ms")
    if ftype == "checkbox":
        if not isinstance(value, bool):
            raise ValueError("checkbox value must be a boolean")
        return {"value": value}
    if ftype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("number value must be numeric")
        return {"value": value}
    if ftype in {"short_text", "text", "long_text", "url", "email"}:
        if not isinstance(value, str):
            raise ValueError(f"{ftype} value must be a string")
        return {"value": value}
    if ftype == "task_relationship":
        raise ValueError(
            "task_relationship fields must be set via clickup_link_tasks"
        )
    log.warning("clickup_unknown_field_type", extra={"type": ftype})
    return {"value": value}


def _decode_custom_fields(
    raw_fields: list[dict[str, Any]],
    list_cfg: ClickUpListConfig,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cf in raw_fields:
        cf_id = cf.get("id")
        if not cf_id:
            continue
        match = list_cfg.field_by_uuid(cf_id)
        if match is None:
            continue
        name, entry = match
        out[name] = _decode_value(cf, entry)
    return out


def _decode_value(cf: dict[str, Any], entry: dict[str, Any]) -> Any:
    ftype = entry.get("type", "")
    value = cf.get("value")
    if value is None:
        return None
    options = entry.get("options", [])
    if ftype == "drop_down":
        for opt in options:
            if opt.get("uuid") == value:
                return opt.get("name")
            if isinstance(value, int) and opt.get("orderindex") == value:
                return opt.get("name")
        return value
    if ftype == "labels":
        if not isinstance(value, list):
            return value
        by_id = {o.get("uuid"): o.get("name") for o in options}
        names: list[str] = []
        for v in value:
            if isinstance(v, dict):
                names.append(by_id.get(v.get("id"), v.get("id", "")))
            else:
                names.append(by_id.get(v, v))
        return names
    if ftype == "number":
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value
    return value


def _task_list_id(task: dict[str, Any]) -> str | None:
    list_obj = task.get("list")
    if isinstance(list_obj, dict):
        list_id = list_obj.get("id")
        if isinstance(list_id, str):
            return list_id
    return None


# ---------------------------------------------------------------------------
# Task projection
# ---------------------------------------------------------------------------


def _summarise_task(task: dict[str, Any], list_cfg: ClickUpListConfig) -> dict[str, Any]:
    cfs = _decode_custom_fields(task.get("custom_fields", []) or [], list_cfg)
    return {
        "task_id": task.get("id"),
        "name": task.get("name"),
        "status": _status_string(task.get("status")),
        "priority": _priority_string(task.get("priority")),
        "due_date": task.get("due_date"),
        "assignee": _first_assignee(task.get("assignees")),
        "url": task.get("url"),
        "custom_fields": cfs,
    }


def _full_task(
    task: dict[str, Any],
    comments: list[dict[str, Any]],
    list_cfg: ClickUpListConfig,
) -> dict[str, Any]:
    cfs = _decode_custom_fields(task.get("custom_fields", []) or [], list_cfg)
    subtasks = [_subtask_summary(st) for st in (task.get("subtasks") or [])]
    decoded_comments = [_comment_summary(c) for c in (comments or [])[:_COMMENT_RETURN_LIMIT]]
    return {
        "task_id": task.get("id"),
        "name": task.get("name"),
        "description": task.get("description"),
        "status": _status_string(task.get("status")),
        "priority": _priority_string(task.get("priority")),
        "due_date": task.get("due_date"),
        "assignee": _first_assignee(task.get("assignees")),
        "url": task.get("url"),
        "list_key": list_cfg.key,
        "custom_fields": cfs,
        "subtasks": subtasks,
        "comments": decoded_comments,
    }


def _subtask_summary(st: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": st.get("id"),
        "name": st.get("name"),
        "status": _status_string(st.get("status")),
        "assignee": _first_assignee(st.get("assignees")),
        "due_date": st.get("due_date"),
    }


def _comment_summary(c: dict[str, Any]) -> dict[str, Any]:
    author = c.get("user") or {}
    text_parts: list[str] = []
    if c.get("comment_text"):
        text_parts.append(c["comment_text"])
    elif isinstance(c.get("comment"), list):
        for seg in c["comment"]:
            if isinstance(seg, dict) and seg.get("text"):
                text_parts.append(seg["text"])
    return {
        "id": c.get("id"),
        "author": author.get("username") or author.get("email"),
        "text": "".join(text_parts),
        "created_at": c.get("date"),
    }


def _status_string(status: Any) -> Any:
    if isinstance(status, dict):
        return status.get("status")
    return status


def _priority_string(priority: Any) -> Any:
    if isinstance(priority, dict):
        return priority.get("priority")
    return priority


def _first_assignee(assignees: Any) -> str | None:
    if not isinstance(assignees, list) or not assignees:
        return None
    first = assignees[0]
    if isinstance(first, dict):
        return first.get("username") or first.get("email") or (
            str(first["id"]) if "id" in first else None
        )
    return str(first)


def _coerce_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _ok(**extra: Any) -> dict[str, Any]:
    return {"status": "ok", **extra}


def _not_configured(missing: list[str]) -> dict[str, Any]:
    return {"status": "not_configured", "missing": missing}


def _error(code: int, message: str) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message}


def _missing_runtime(settings: MCPSettings) -> list[str]:
    missing: list[str] = []
    if not settings.clickup_api_token:
        missing.append("CLICKUP_API_TOKEN")
    if not settings.clickup_fields_config_path.exists():
        missing.append(str(settings.clickup_fields_config_path))
    return missing


def _load_config(settings: MCPSettings):
    return ClickUpListsConfig.from_path(settings.clickup_fields_config_path)


def _legacy_config_response(settings: MCPSettings) -> dict[str, Any]:
    return _error(
        500,
        (
            "ClickUp config at "
            f"{settings.clickup_fields_config_path} is in the legacy flat "
            "{'fields': ...} shape. Re-run "
            "`python scripts/setup_clickup_fields.py` to regenerate it with "
            "the multi-list layout."
        ),
    )


def _resolve_list(
    settings: MCPSettings,
    config: ClickUpListsConfig,
    list_key: str,
) -> tuple[ClickUpListConfig, str] | dict[str, Any]:
    """Return ``(list_cfg, list_id)`` or a not_configured/error dict.

    The list_id is taken from env, with the config-recorded value as a
    fallback for callers that haven't pointed env at both lists yet.
    """

    list_cfg = config.get(list_key)
    if list_cfg is None:
        return _error(400, f"unknown list_key: {list_key}")

    env_id = (
        settings.clickup_ir_list_id
        if list_key == IR_KEY
        else settings.clickup_pipeline_list_id
        if list_key == PIPELINE_KEY
        else ""
    )
    list_id = env_id or list_cfg.list_id
    if not list_id:
        env_name = "CLICKUP_IR_LIST_ID" if list_key == IR_KEY else "CLICKUP_PIPELINE_LIST_ID"
        return _not_configured([env_name])
    return list_cfg, list_id


def _context(
    tenant_id: str | None,
    user_id: str | None,
    access_token: str | None,
    permissions: list[str] | None,
):
    return build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )


async def _resolve_settings() -> MCPSettings:
    """Return settings with per-tenant ClickUp credentials overlaid when a
    tenant has been resolved for this request (via the ``X-Tenant-ID`` header);
    otherwise the base env-var settings unchanged.

    Backward compatible: with no tenant context (the Claude Desktop /
    X-API-Key path, and all existing tests) this returns ``get_settings()``
    untouched, so env-var credentials remain in force.
    """

    base = get_settings()
    tenant = current_tenant()
    if not tenant:
        return base
    creds = await get_tenant_credentials(tenant["id"], "clickup")
    if not creds:
        return base
    meta = creds.get("metadata") or {}
    return base.model_copy(
        update={
            "clickup_api_token": creds.get("credential_key") or base.clickup_api_token,
            "clickup_team_id": meta.get("team_id") or base.clickup_team_id,
            "clickup_ir_list_id": meta.get("ir_list_id") or base.clickup_ir_list_id,
            "clickup_pipeline_list_id": (
                meta.get("pipeline_list_id") or base.clickup_pipeline_list_id
            ),
        }
    )


async def _run(fn, tool_name: str, settings: MCPSettings) -> dict[str, Any]:
    service = ClickUpService(settings)
    try:
        return await fn(service)
    except ClickUpAPIError as exc:
        log.warning(
            "clickup_api_error",
            extra={"tool": tool_name, "code": exc.code},
        )
        return _error(exc.code, exc.message)
    except httpx.RequestError as exc:
        log.warning(
            "clickup_request_error",
            extra={"tool": tool_name, "exception": exc.__class__.__name__},
        )
        return _error(0, f"ClickUp request failed: {exc.__class__.__name__}")
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


async def clickup_list_tasks(
    list_key: str = IR_KEY,
    status: str | None = None,
    priority: str | None = None,
    limit: int = 50,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return task summaries from one of the configured lists.

    ``status`` filters on the native ClickUp status (e.g. "ACTIVE",
    "PROPOSAL SENT"). ``priority`` filters on the native ClickUp priority
    name (e.g. "high"). Custom field UUIDs are decoded to human names.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_list_tasks",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        resolved = _resolve_list(settings, config, list_key)
        if isinstance(resolved, dict):
            return resolved
        list_cfg, list_id = resolved

        async def _do(service: ClickUpService) -> dict[str, Any]:
            payload = await service.list_tasks(list_id)
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            results: list[dict[str, Any]] = []
            status_target = status.strip().lower() if status else None
            priority_target = priority.strip().lower() if priority else None
            for task in tasks:
                summary = _summarise_task(task, list_cfg)
                if status_target is not None:
                    s = (summary.get("status") or "").strip().lower()
                    if s != status_target:
                        continue
                if priority_target is not None:
                    p = (summary.get("priority") or "").strip().lower()
                    if p != priority_target:
                        continue
                results.append(summary)
                if len(results) >= limit:
                    break
            log.info(
                "clickup_list_tasks",
                extra={
                    "tool": "clickup_list_tasks",
                    "list_key": list_key,
                    "count": len(results),
                    "status": status,
                    "priority": priority,
                },
            )
            return _ok(list_key=list_key, count=len(results), tasks=results)

        return await _run(_do, "clickup_list_tasks", settings)


async def clickup_get_task(
    task_id: str,
    list_key: str = IR_KEY,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return a single task with custom fields decoded against ``list_key``'s schema."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_get_task",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])
        list_cfg = config.get(list_key)
        if list_cfg is None:
            return _error(400, f"unknown list_key: {list_key}")

        async def _do(service: ClickUpService) -> dict[str, Any]:
            task = await service.get_task(task_id)
            comments_payload = await service.get_comments(task_id)
            comments = (
                comments_payload.get("comments", [])
                if isinstance(comments_payload, dict)
                else []
            )
            log.info(
                "clickup_get_task",
                extra={
                    "tool": "clickup_get_task",
                    "task_id": task_id,
                    "list_key": list_key,
                },
            )
            return _ok(task=_full_task(task, comments, list_cfg))

        return await _run(_do, "clickup_get_task", settings)


async def clickup_get_tasks_needing_work(
    list_key: str = IR_KEY,
    statuses: list[str] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Convenience query — tasks in any of the work-required native statuses.

    Defaults: IR → ``["ACTIVE"]``; Pipeline → the in-flight statuses.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_get_tasks_needing_work",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        resolved = _resolve_list(settings, config, list_key)
        if isinstance(resolved, dict):
            return resolved
        list_cfg, list_id = resolved

        wanted = {
            s.strip().lower()
            for s in (statuses or _DEFAULT_NEEDING_WORK.get(list_key, []))
        }
        if not wanted:
            return _error(400, f"no default needing-work statuses for list_key={list_key}")

        async def _do(service: ClickUpService) -> dict[str, Any]:
            payload = await service.list_tasks(list_id)
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            results = [
                summary
                for summary in (_summarise_task(t, list_cfg) for t in tasks)
                if (summary.get("status") or "").strip().lower() in wanted
            ]
            log.info(
                "clickup_get_tasks_needing_work",
                extra={
                    "tool": "clickup_get_tasks_needing_work",
                    "list_key": list_key,
                    "count": len(results),
                    "statuses": sorted(wanted),
                },
            )
            return _ok(list_key=list_key, count=len(results), tasks=results)

        return await _run(_do, "clickup_get_tasks_needing_work", settings)


async def clickup_list_subtasks(
    parent_task_id: str,
    include_completed: bool = True,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return subtasks of a task. Used to detect cleared blockers."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_list_subtasks",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)

        async def _do(service: ClickUpService) -> dict[str, Any]:
            task = await service.get_task(parent_task_id)
            subtasks = [_subtask_summary(st) for st in (task.get("subtasks") or [])]
            if not include_completed:
                subtasks = [
                    s for s in subtasks
                    if (s.get("status") or "").lower() not in {"complete", "closed", "done"}
                ]
            log.info(
                "clickup_list_subtasks",
                extra={
                    "tool": "clickup_list_subtasks",
                    "task_id": parent_task_id,
                    "count": len(subtasks),
                },
            )
            return _ok(count=len(subtasks), subtasks=subtasks)

        return await _run(_do, "clickup_list_subtasks", settings)


async def clickup_compute_pipeline_totals(
    list_key: str = PIPELINE_KEY,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate ``Estimated Amount`` across Pipeline tasks.

    Returns totals grouped by native status and by ``Probability``, plus a
    probability-weighted total (weights: High=0.75, Medium=0.40, Low=0.15)
    and the grand total. Read-only.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_compute_pipeline_totals",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        resolved = _resolve_list(settings, config, list_key)
        if isinstance(resolved, dict):
            return resolved
        list_cfg, list_id = resolved

        async def _do(service: ClickUpService) -> dict[str, Any]:
            payload = await service.list_tasks(list_id)
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            by_status: dict[str, float] = {}
            by_probability: dict[str, float] = {}
            weighted = 0.0
            grand_total = 0.0
            for task in tasks:
                cfs = _decode_custom_fields(task.get("custom_fields", []) or [], list_cfg)
                amount = _coerce_number(cfs.get("Estimated Amount"))
                status_name = _status_string(task.get("status")) or "(no status)"
                probability = cfs.get("Probability") or "(unset)"
                by_status[status_name] = by_status.get(status_name, 0.0) + amount
                by_probability[probability] = by_probability.get(probability, 0.0) + amount
                weighted += amount * _PROBABILITY_WEIGHTS.get(probability, 0.0)
                grand_total += amount
            log.info(
                "clickup_compute_pipeline_totals",
                extra={
                    "tool": "clickup_compute_pipeline_totals",
                    "list_key": list_key,
                    "task_count": len(tasks),
                },
            )
            return _ok(
                list_key=list_key,
                totals={
                    "by_status": by_status,
                    "by_probability": by_probability,
                    "weighted": weighted,
                    "grand_total": grand_total,
                    "task_count": len(tasks),
                },
            )

        return await _run(_do, "clickup_compute_pipeline_totals", settings)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def clickup_update_task_field(
    task_id: str,
    field_name: str,
    value: Any,
    list_key: str = IR_KEY,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Update one custom field on a task by human name.

    Validates ``field_name`` exists in ``list_key``'s schema and that
    ``value`` matches the field type. ``task_relationship`` fields must go
    through ``clickup_link_tasks`` and are rejected here.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_update_task_field",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        list_cfg = config.get(list_key)
        if list_cfg is None:
            return _error(400, f"unknown list_key: {list_key}")
        entry = list_cfg.field_by_name(field_name)
        if entry is None:
            return _error(400, f"unknown field name: {field_name}")
        try:
            payload = _encode_value(entry, value)
        except ValueError as exc:
            return _error(400, str(exc))

        field_uuid = entry.get("uuid")
        if not field_uuid:
            return _error(
                500,
                f"field '{field_name}' is missing a uuid in the config — "
                "re-run scripts/setup_clickup_fields.py",
            )

        async def _do(service: ClickUpService) -> dict[str, Any]:
            await service.set_custom_field(task_id, field_uuid, payload)
            log.info(
                "clickup_update_task_field",
                extra={
                    "tool": "clickup_update_task_field",
                    "task_id": task_id,
                    "field": field_name,
                    "list_key": list_key,
                },
            )
            return _ok(task_id=task_id, list_key=list_key, field=field_name, value=value)

        return await _run(_do, "clickup_update_task_field", settings)


async def clickup_set_status(
    task_id: str,
    status: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Set the native ClickUp status on a task.

    Auto-detects which configured list the task lives in by issuing a GET
    against the task first, then validates ``status`` against that list's
    configured statuses.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_set_status",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        async def _do(service: ClickUpService) -> dict[str, Any]:
            task = await service.get_task(task_id)
            list_id = _task_list_id(task)
            if not list_id:
                return _error(404, f"task {task_id} did not include a list id")
            list_cfg = config.find_by_list_id(list_id)
            if list_cfg is None:
                return _error(
                    400,
                    f"task {task_id} lives in list {list_id}, which is not "
                    "configured — only investor_relations and "
                    "fundraising_pipeline are tracked",
                )
            if not list_cfg.has_status(status):
                return _error(
                    400,
                    f"status '{status}' not valid for {list_cfg.key}; "
                    f"allowed: {', '.join(list_cfg.statuses)}",
                )
            await service.update_task(task_id, {"status": status})
            log.info(
                "clickup_set_status",
                extra={
                    "tool": "clickup_set_status",
                    "task_id": task_id,
                    "list_key": list_cfg.key,
                    "status": status,
                },
            )
            return _ok(task_id=task_id, list_key=list_cfg.key, status=status)

        return await _run(_do, "clickup_set_status", settings)


async def clickup_link_tasks(
    source_task_id: str,
    target_task_id: str,
    link_field_name: str,
    list_key: str = IR_KEY,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Set a ``task_relationship`` custom field on ``source_task_id``.

    ``link_field_name`` must name a field of type ``task_relationship`` in
    ``list_key``'s schema (e.g. ``"Linked Application"`` on IR, or
    ``"Source Funder"`` on Pipeline). Other field types are rejected.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_link_tasks",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        list_cfg = config.get(list_key)
        if list_cfg is None:
            return _error(400, f"unknown list_key: {list_key}")
        entry = list_cfg.field_by_name(link_field_name)
        if entry is None:
            return _error(400, f"unknown field name: {link_field_name}")
        if entry.get("type") != "task_relationship":
            return _error(
                400,
                f"field '{link_field_name}' is type '{entry.get('type')}', "
                "not 'task_relationship'",
            )
        field_uuid = entry.get("uuid")
        if not field_uuid:
            return _error(
                500,
                f"field '{link_field_name}' missing uuid in config — "
                "re-run scripts/setup_clickup_fields.py",
            )

        async def _do(service: ClickUpService) -> dict[str, Any]:
            await service.set_custom_field(
                source_task_id,
                field_uuid,
                {"value": {"add": [target_task_id], "rem": []}},
            )
            log.info(
                "clickup_link_tasks",
                extra={
                    "tool": "clickup_link_tasks",
                    "source": source_task_id,
                    "target": target_task_id,
                    "field": link_field_name,
                    "list_key": list_key,
                },
            )
            return _ok(
                source=source_task_id,
                target=target_task_id,
                field=link_field_name,
            )

        return await _run(_do, "clickup_link_tasks", settings)


async def clickup_add_comment(
    task_id: str,
    comment_text: str,
    notify_assignees: bool = False,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Post a (markdown-supporting) comment to a task."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_add_comment",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        if not isinstance(comment_text, str) or not comment_text.strip():
            return _error(400, "comment_text must be a non-empty string")

        async def _do(service: ClickUpService) -> dict[str, Any]:
            response = await service.post_comment(
                task_id, comment_text, notify_assignees
            )
            log.info(
                "clickup_add_comment",
                extra={
                    "tool": "clickup_add_comment",
                    "task_id": task_id,
                    "notify_assignees": notify_assignees,
                },
            )
            return _ok(
                task_id=task_id,
                comment_id=response.get("id") if isinstance(response, dict) else None,
            )

        return await _run(_do, "clickup_add_comment", settings)


async def clickup_create_subtask(
    parent_task_id: str,
    name: str,
    description: str | None = None,
    assignee_id: int | None = None,
    due_date_ms: int | None = None,
    list_key: str = IR_KEY,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a subtask under a task in ``list_key``'s list."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "clickup_create_subtask",
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)
        config = _load_config(settings)
        if config == "legacy":
            return _legacy_config_response(settings)
        if config is None or not isinstance(config, ClickUpListsConfig):
            return _not_configured([str(settings.clickup_fields_config_path)])

        resolved = _resolve_list(settings, config, list_key)
        if isinstance(resolved, dict):
            return resolved
        _list_cfg, list_id = resolved

        if not isinstance(name, str) or not name.strip():
            return _error(400, "name must be a non-empty string")

        body: dict[str, Any] = {"name": name, "parent": parent_task_id}
        if description is not None:
            body["description"] = description
        if assignee_id is not None:
            body["assignees"] = [assignee_id]
        if due_date_ms is not None:
            body["due_date"] = due_date_ms

        async def _do(service: ClickUpService) -> dict[str, Any]:
            response = await service.create_task(list_id, body)
            subtask_id = response.get("id") if isinstance(response, dict) else None
            log.info(
                "clickup_create_subtask",
                extra={
                    "tool": "clickup_create_subtask",
                    "parent_task_id": parent_task_id,
                    "subtask_id": subtask_id,
                    "list_key": list_key,
                },
            )
            return _ok(parent_task_id=parent_task_id, subtask_id=subtask_id)

        return await _run(_do, "clickup_create_subtask", settings)


async def clickup_complete_subtask(
    subtask_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Mark a subtask complete by setting its status to ``complete``."""

    return await _set_subtask_status(
        subtask_id,
        status="complete",
        tool_name="clickup_complete_subtask",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )


async def clickup_reopen_subtask(
    subtask_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Reopen a subtask by setting its status to ``to do``."""

    return await _set_subtask_status(
        subtask_id,
        status="to do",
        tool_name="clickup_reopen_subtask",
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )


async def _set_subtask_status(
    subtask_id: str,
    *,
    status: str,
    tool_name: str,
    tenant_id: str | None,
    user_id: str | None,
    access_token: str | None,
    permissions: list[str] | None,
) -> dict[str, Any]:
    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            tool_name,
            context.permissions,
        )
        settings = await _resolve_settings()
        missing = _missing_runtime(settings)
        if missing:
            return _not_configured(missing)

        async def _do(service: ClickUpService) -> dict[str, Any]:
            await service.update_task(subtask_id, {"status": status})
            log.info(
                tool_name,
                extra={"tool": tool_name, "subtask_id": subtask_id, "status": status},
            )
            return _ok(subtask_id=subtask_id, status=status)

        return await _run(_do, tool_name, settings)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(mcp: Any) -> None:
    """Register ClickUp MCP tools."""

    mcp.tool()(clickup_list_tasks)
    mcp.tool()(clickup_get_task)
    mcp.tool()(clickup_get_tasks_needing_work)
    mcp.tool()(clickup_list_subtasks)
    mcp.tool()(clickup_compute_pipeline_totals)
    mcp.tool()(clickup_update_task_field)
    mcp.tool()(clickup_set_status)
    mcp.tool()(clickup_link_tasks)
    mcp.tool()(clickup_add_comment)
    mcp.tool()(clickup_create_subtask)
    mcp.tool()(clickup_complete_subtask)
    mcp.tool()(clickup_reopen_subtask)
