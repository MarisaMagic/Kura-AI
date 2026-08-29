"""High-risk MCP tool gating: require one-time user confirmation before execution."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from langchain_core.tools import StructuredTool

from app.chat.cache import cache
from app.settings import settings

_WRITE_ACTIONS = {
    "create",
    "update",
    "delete",
    "remove",
    "merge",
    "push",
    "invite",
    "publish",
    "write",
    "secret",
    "deploy",
    "execute",
    "add",
    "assign",
    "close",
    "reopen",
    "lock",
    "unlock",
    "transfer",
    "fork",
}
_READ_ACTIONS = {
    "get",
    "list",
    "search",
    "read",
    "fetch",
    "resolve",
    "query",
    "describe",
    "show",
    "download",
}


def _tokenize(text: str) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _write_actions() -> set[str]:
    return set(_WRITE_ACTIONS)


def _confirmation_ttl() -> int:
    return max(60, int(getattr(settings, "MCP_" + "CONFIRMATION_TTL_SECONDS", 300)))


def confirmation_required() -> bool:
    return bool(getattr(settings, "MCP_" + "CONFIRMATION_REQUIRED", True))


def _tool_annotations(tool: object) -> dict:
    for value in (
        getattr(tool, "annotations", None),
        getattr(tool, "metadata", None),
        getattr(tool, "extra", None),
    ):
        if isinstance(value, dict):
            if isinstance(value.get("annotations"), dict):
                return value["annotations"]
            if isinstance(value.get("mcp"), dict) and isinstance(value["mcp"].get("annotations"), dict):
                return value["mcp"]["annotations"]
    return {}


def _ann_bool(annotations: dict, key: str) -> bool | None:
    value = annotations.get(key)
    return value if isinstance(value, bool) else None


def is_high_risk_mcp_tool(server_name: str, tool_name: str, description: str, annotations: dict | None = None) -> bool:
    annotations = annotations or {}
    read_only = _ann_bool(annotations, "readOnlyHint")
    destructive = _ann_bool(annotations, "destructiveHint")
    if read_only is True:
        return False
    if destructive is True:
        return True
    tokens = _tokenize(f"{server_name} {tool_name} {description}")
    return bool(tokens & _write_actions())


def should_confirm_mcp_tool(server_name: str, tool: object, confirm_policy: str = "auto") -> bool:
    if not confirmation_required():
        return False
    policy = (confirm_policy or "auto").strip().lower()
    if policy == "never":
        return False
    if policy == "always":
        return True
    tool_name = getattr(tool, "name", None) or "mcp_tool"
    description = getattr(tool, "description", "") or ""
    return is_high_risk_mcp_tool(server_name, tool_name, description, _tool_annotations(tool))


def _canonical_args(payload: object) -> dict:
    return payload if isinstance(payload, dict) else {"input": payload}


def _args_hash(payload: object) -> str:
    canonical = json.dumps(_canonical_args(payload), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _args_preview(payload: object) -> str:
    return json.dumps(_canonical_args(payload), ensure_ascii=False, default=str)[:800]


def _pending_key(pending_id: str) -> str:
    return f"mcp_pending:{pending_id}"


def _approved_key(pending_id: str) -> str:
    return f"mcp_approved:{pending_id}"


def _public_pending(record: dict) -> dict:
    return {
        "pending_id": record.get("pending_id"),
        "server_name": record.get("server_name"),
        "tool_name": record.get("tool_name"),
        "args_hash": record.get("args_hash"),
        "args_preview": record.get("args_preview"),
        "expires_at": record.get("expires_at"),
    }


def _load_approved_call(pending_id: str, *, user_id: int, agent_id: int, session_id: str, tool_name: str) -> dict | None:
    record = cache.get_json(_approved_key(pending_id))
    if not isinstance(record, dict):
        return None
    if int(record.get("user_id") or -1) != int(user_id):
        return None
    if int(record.get("agent_id") or -1) != int(agent_id):
        return None
    if str(record.get("session_id") or "default_session") != (session_id or "default_session"):
        return None
    if str(record.get("tool_name") or "") != tool_name:
        return None
    return record


def _call_payload(args: tuple, kwargs: dict) -> object:
    if kwargs:
        return dict(kwargs)
    if len(args) == 1:
        return args[0]
    if args:
        return list(args)
    return {}


def wrap_mcp_tool_with_confirmation(tool: object, *, server_name: str, user_id: int | None, agent_id: int, session_id: str | None, confirm_policy: str = "auto") -> object:
    if user_id is None or not session_id:
        return tool
    if not should_confirm_mcp_tool(server_name, tool, confirm_policy):
        return tool
    tool_name = getattr(tool, "name", None) or "mcp_tool"
    description = (getattr(tool, "description", "") or "") or tool_name
    args_schema = getattr(tool, "args_schema", None)

    async def _confirmed_coro(*args, _tool=tool, **kwargs):
        from app.chat.tools import add_pending_mcp_confirmation, get_approved_mcp_pending_id

        payload = _call_payload(args, kwargs)
        approved_id = get_approved_mcp_pending_id(clear=False)
        if approved_id:
            approved = _load_approved_call(
                approved_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                tool_name=tool_name,
            )
            if approved is not None:
                get_approved_mcp_pending_id(clear=True)
                cache.delete(_approved_key(approved_id))
                return await _tool.ainvoke(_canonical_args(approved.get("args") or payload))
            get_approved_mcp_pending_id(clear=True)

        ttl = _confirmation_ttl()
        pending_id = uuid.uuid4().hex
        record = {
            "pending_id": pending_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "server_name": server_name,
            "tool_name": tool_name,
            "args": payload,
            "args_hash": _args_hash(payload),
            "args_preview": _args_preview(payload),
            "expires_at": time.time() + ttl,
        }
        cache.set_json(_pending_key(pending_id), record, ttl)
        gate = add_pending_mcp_confirmation(_public_pending(record))
        status = gate.get("status")
        if status == "duplicate":
            cache.delete(_pending_key(pending_id))
            existing = gate.get("pending") or {}
            return (
                "MCP_" + "CONFIRMATION_REQUIRED pending_id=" + str(existing.get("pending_id")) + f" server={server_name} tool={tool_name}."
                " A confirmation for this exact call is already pending; stop and ask the user to confirm it."
            )
        if status == "capped":
            cache.delete(_pending_key(pending_id))
            return (
                "MCP_" + "CONFIRMATION_LIMIT_REACHED: too many high-risk MCP calls in this turn. "
                "Do not call more MCP write tools; ask the user to narrow the request."
            )
        return (
            "MCP_" + f"CONFIRMATION_REQUIRED pending_id={pending_id} server={server_name} tool={tool_name}."
            " Stop and ask the user to confirm this exact action; do not retry until confirmed."
        )

    options = {
        "name": tool_name,
        "description": description,
        "coroutine": _confirmed_coro,
    }
    if args_schema is not None:
        options["args_schema"] = args_schema
    else:
        options["infer_schema"] = False
    return StructuredTool.from_function(**options)


def peek_approved_mcp_call(pending_id: str, *, user_id: int, agent_id: int, session_id: str) -> dict | None:
    record = cache.get_json(_approved_key(pending_id))
    if not isinstance(record, dict):
        return None
    if int(record.get("user_id") or -1) != int(user_id):
        return None
    if int(record.get("agent_id") or -1) != int(agent_id):
        return None
    if str(record.get("session_id") or "default_session") != (session_id or "default_session"):
        return None
    return record


def approve_mcp_confirmation(pending_id: str, user_id: int, approve: bool) -> bool:
    record = cache.get_json(_pending_key(pending_id))
    if not isinstance(record, dict):
        return False
    if int(record.get("user_id") or -1) != int(user_id):
        return False
    if approve:
        cache.set_json(_approved_key(pending_id), record, _confirmation_ttl())
    cache.delete(_pending_key(pending_id))
    return True
