import asyncio
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ALLOW_PRIVATE_UPSTREAM_URLS", "false")

_app_pkg = types.ModuleType("app")
_app_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
sys.modules.setdefault("app", _app_pkg)

import pytest

from app.chat.tools import add_pending_mcp_confirmation, get_pending_mcp_confirmations, set_approved_mcp_pending_id
from app.mcp_client import tool_policy
from app.utils import egress
from app.utils.ssrf import UnsafeUrlError, validate_public_http_url


def test_ssrf_blocks_metadata_cgnat_mapped_ipv4_and_credentials():
    for url in (
        "http://100.100.100.200/latest/meta-data/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "http://user:pass@example.com/",
    ):
        with pytest.raises(UnsafeUrlError):
            validate_public_http_url(url)


def test_pinned_sync_backend_connects_to_validated_ip(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):
        return [(None, None, None, None, ("93.184.216.34", port))]

    monkeypatch.setattr("app.utils.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    upstream = validate_public_http_url("http://example.com:8080/path")

    calls = []

    class FakeSocket:
        def setsockopt(self, *args):
            return None

    def fake_create_connection(address, timeout=None, source_address=None):
        calls.append((address, timeout, source_address))
        return FakeSocket()

    monkeypatch.setattr(egress.socket, "create_connection", fake_create_connection)
    backend = egress._PinnedNetworkBackend(upstream.ips)
    backend.connect_tcp("example.com", 8080, timeout=1)
    assert calls
    assert calls[0][0] == ("93.184.216.34", 8080)


def test_pinned_llm_client_kwargs_builds_clients(monkeypatch):
    def fake_getaddrinfo(host, port, type=None):
        return [(None, None, None, None, ("93.184.216.34", port))]

    monkeypatch.setattr("app.utils.ssrf.socket.getaddrinfo", fake_getaddrinfo)
    clients = egress.pinned_llm_client_kwargs("http://example.com:8080")
    assert set(clients) == {"http_client", "http_async_client"}
    clients["http_client"].close()
    asyncio.run(clients["http_async_client"].aclose())


def test_mcp_confirm_policy_and_annotations():
    class ReadOnlyTool:
        name = "get_issue"
        description = "Get a GitHub issue"
        annotations = {"readOnlyHint": True}

    class DeleteTool:
        name = "delete_issue"
        description = "Delete a GitHub issue"
        annotations = {"destructiveHint": True}

    class WebReadTool:
        name = "get-library-docs"
        description = "Fetch documentation for a library"
        annotations = {"readOnlyHint": False, "openWorldHint": True}

    assert not tool_policy.should_confirm_mcp_tool("Context7", WebReadTool(), "auto")
    assert not tool_policy.should_confirm_mcp_tool("GitHub", ReadOnlyTool(), "auto")
    assert tool_policy.should_confirm_mcp_tool("GitHub", DeleteTool(), "auto")
    assert not tool_policy.should_confirm_mcp_tool("GitHub", DeleteTool(), "never")
    assert tool_policy.should_confirm_mcp_tool("GitHub", ReadOnlyTool(), "always")

    class ListCommitTool:
        name = "list_commit"
        description = "List commits on a repository"
        annotations = None

    class MergeTool:
        name = "merge_pull_request"
        description = "Merge a pull request"
        annotations = None

    assert not tool_policy.should_confirm_mcp_tool("GitHub", ListCommitTool(), "auto")
    assert tool_policy.should_confirm_mcp_tool("GitHub", MergeTool(), "auto")


class _DummyHighRiskTool:
    name = "create_issue"
    description = "Create a GitHub issue"
    args_schema = None

    def __init__(self):
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(payload)
        return "ok"


def test_mcp_pending_dedupe_and_cap():
    get_pending_mcp_confirmations(clear=True)
    first = add_pending_mcp_confirmation({"pending_id": "p1", "tool_name": "create_issue", "args_hash": "h1"})
    assert first["status"] == "added"
    dup = add_pending_mcp_confirmation({"pending_id": "p2", "tool_name": "create_issue", "args_hash": "h1"})
    assert dup["status"] == "duplicate"
    assert dup["pending"]["pending_id"] == "p1"
    assert add_pending_mcp_confirmation({"pending_id": "p3", "tool_name": "delete_repo", "args_hash": "h2"})["status"] == "added"
    assert add_pending_mcp_confirmation({"pending_id": "p4", "tool_name": "merge_pull_request", "args_hash": "h3"})["status"] == "added"
    capped = add_pending_mcp_confirmation({"pending_id": "p5", "tool_name": "push", "args_hash": "h4"})
    assert capped["status"] == "capped"
    get_pending_mcp_confirmations(clear=True)


def test_high_risk_mcp_tool_requires_one_time_approval(monkeypatch):
    store = {}

    def fake_get_json(key):
        return store.get(key)

    def fake_set_json(key, value, ttl=None):
        store[key] = value
        return True

    def fake_delete(key):
        store.pop(key, None)

    monkeypatch.setattr(tool_policy.cache, "get_json", fake_get_json)
    monkeypatch.setattr(tool_policy.cache, "set_json", fake_set_json)
    monkeypatch.setattr(tool_policy.cache, "delete", fake_delete)

    dummy = _DummyHighRiskTool()
    wrapped = tool_policy.wrap_mcp_tool_with_confirmation(
        dummy,
        server_name="GitHub",
        user_id=1,
        agent_id=2,
        session_id="s1",
    )

    payload = {"title": "hello"}
    first = asyncio.run(wrapped.coroutine(payload))
    assert first.startswith("MCP_CONFIRMATION_REQUIRED")
    assert dummy.calls == []
    pending_key = next(key for key in store if key.startswith("mcp_pending:"))
    pending_id = store[pending_key]["pending_id"]

    assert tool_policy.approve_mcp_confirmation(pending_id, 1, True)
    set_approved_mcp_pending_id(pending_id)
    second = asyncio.run(wrapped.coroutine({"title": "changed by model replay"}))
    assert second == "ok"
    assert dummy.calls == [payload]
    assert not any(key.startswith("mcp_pending:") for key in store)
    assert not any(key.startswith("mcp_approved:") for key in store)
