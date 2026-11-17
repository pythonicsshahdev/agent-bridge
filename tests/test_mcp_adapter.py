import json

import httpx
import pytest
from pydantic import ValidationError

from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.mcp_bridge import McpAdapter
from a2a_bridge.config import McpAdapterConfig


def ctx(text="", data=None, headers=None):
    return TaskContext(agent_slug="tools", task_id="t", context_id="c", text=text, data=data or [], headers=headers or {})


class FakeMcp:
    """A minimal MCP server over streamable HTTP. `sse` makes it answer in event-stream form."""

    def __init__(self, sse=False, tool_result=None, call_error=None, status=None):
        self.sse, self.log, self.calls, self.deleted = sse, [], [], False
        self.tool_result = tool_result or {"content": [{"type": "text", "text": "42"}]}
        self.call_error, self.status = call_error, status

    def __call__(self, request):
        if request.method == "DELETE":
            self.deleted = True
            return httpx.Response(200)
        if self.status:
            return httpx.Response(self.status, text="denied")
        body = json.loads(request.content)
        self.log.append((body.get("method"), dict(request.headers)))
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return self._respond(body, {"protocolVersion": "2025-06-18", "capabilities": {}}, {"Mcp-Session-Id": "s-1"})
        self.calls.append(body["params"])
        if self.call_error:
            return self._respond(body, error={"code": -32602, "message": self.call_error})
        return self._respond(body, self.tool_result)

    def _respond(self, body, result=None, headers=None, error=None):
        payload = {"jsonrpc": "2.0", "id": body["id"], **({"error": error} if error else {"result": result})}
        if self.sse:
            text = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\nevent: message\ndata: ' + json.dumps(payload) + "\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=payload, headers=headers)


def adapter(server, **overrides):
    cfg = McpAdapterConfig(type="mcp", url="https://mcp.test/mcp", tools=overrides.pop("tools", ["add", "echo"]), **overrides)
    return McpAdapter(cfg, httpx.AsyncClient(transport=httpx.MockTransport(server)))


def test_default_tool_must_be_allowed():
    with pytest.raises(ValidationError):
        McpAdapterConfig(type="mcp", url="https://x", tools=["a"], default_tool="b")
    with pytest.raises(ValidationError):
        McpAdapterConfig(type="mcp", url="https://x", tools=[])


@pytest.mark.parametrize("sse", [False, True])
async def test_named_tool_with_arguments_round_trip(sse):
    server = FakeMcp(sse=sse)
    r = await adapter(server).execute(ctx(data=[{"tool": "add", "arguments": {"a": 40, "b": 2}}]))
    assert r.state == AdapterState.COMPLETED and r.text == "42"
    assert server.calls == [{"name": "add", "arguments": {"a": 40, "b": 2}}]
    assert [m for m, _ in server.log] == ["initialize", "notifications/initialized", "tools/call"]
    assert server.log[2][1]["mcp-session-id"] == "s-1" and server.log[2][1]["mcp-protocol-version"] == "2025-06-18"
    assert server.deleted  # the session is closed


async def test_single_tool_and_bare_data_are_arguments():
    server = FakeMcp()
    await adapter(server, tools=["add"]).execute(ctx(data=[{"a": 1, "b": 2}]))
    assert server.calls == [{"name": "add", "arguments": {"a": 1, "b": 2}}]


async def test_text_argument_fills_from_the_message():
    server = FakeMcp()
    await adapter(server, default_tool="echo", text_argument="message").execute(ctx(text="hello"))
    assert server.calls == [{"name": "echo", "arguments": {"message": "hello"}}]


async def test_tools_outside_the_allow_list_are_never_called():
    server = FakeMcp()
    r = await adapter(server).execute(ctx(data=[{"tool": "delete_everything", "arguments": {}}]))
    assert r.state == AdapterState.REJECTED and "not available" in r.error
    assert server.log == []  # nothing was even sent to the server


@pytest.mark.parametrize("message", [ctx(text="no tool named"), ctx(data=[{"a": 1}])])
async def test_ambiguous_messages_are_rejected_with_the_options(message):
    server = FakeMcp()
    r = await adapter(server).execute(message)
    assert r.state == AdapterState.REJECTED and server.log == []


async def test_structured_content_becomes_data():
    server = FakeMcp(tool_result={"content": [], "structuredContent": {"sum": 42}})
    r = await adapter(server, tools=["add"]).execute(ctx(data=[{"a": 1}]))
    assert r.state == AdapterState.COMPLETED and r.data == {"sum": 42} and r.text is None


async def test_tool_errors_and_protocol_errors_fail_cleanly():
    r = await adapter(FakeMcp(tool_result={"isError": True, "content": [{"type": "text", "text": "division by zero"}]}), tools=["add"]).execute(ctx(data=[{"a": 1}]))
    assert r.state == AdapterState.FAILED and r.error == "division by zero"
    r = await adapter(FakeMcp(call_error="bad params"), tools=["add"]).execute(ctx(data=[{"a": 1}]))
    assert r.state == AdapterState.FAILED and "bad params" in r.error


async def test_http_denials_and_outages():
    r = await adapter(FakeMcp(status=401), tools=["add"]).execute(ctx(data=[{"a": 1}]))
    assert r.state == AdapterState.AUTH_REQUIRED
    r = await adapter(FakeMcp(status=500), tools=["add"]).execute(ctx(data=[{"a": 1}]))
    assert r.state == AdapterState.FAILED and "500" in r.error

    def down(request):
        raise httpx.ConnectError("no")

    assert (await adapter(down, tools=["add"]).execute(ctx(data=[{"a": 1}]))).error == "MCP server unreachable"


async def test_bearer_passthrough_reaches_the_server():
    server = FakeMcp()
    a = adapter(server, tools=["add"], auth={"mode": "bearer_passthrough"})
    assert (await a.execute(ctx(data=[{"a": 1}]))).state == AdapterState.AUTH_REQUIRED
    await a.execute(ctx(data=[{"a": 1}], headers={"authorization": "Bearer tok"}))
    assert all(h["authorization"] == "Bearer tok" for _, h in server.log)
