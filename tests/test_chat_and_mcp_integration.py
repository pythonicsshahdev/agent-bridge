"""A2A client -> proxy -> OpenAI-compatible endpoint / MCP server (mock ASGI apps), with the real SQL stores."""

import httpx
import pytest
from mocks.mcp_server import app as mcp_app, SESSIONS
from mocks.openai_chat import app as chat_app
from starlette.testclient import TestClient

from a2a_bridge.adapters.mcp_bridge import McpAdapter
from a2a_bridge.adapters.openai_chat import OpenAIChatAdapter
from a2a_bridge.config import parse_config
from a2a_bridge.main import create_app

HEADERS = {"X-API-Key": "k", "A2A-Version": "1.0"}
CONFIG = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  inbound_auth: {{api_keys: ["${{KEY}}"]}}
agents:
  - slug: assistant
    name: Assistant
    adapter: {{type: openai_chat, url: "http://llm.test/v1", model: tiny, max_history_messages: 10}}
    skills: [{{id: chat, name: Chat}}]
  - slug: calc
    name: Calc
    adapter: {{type: mcp, url: "http://mcp.test/mcp", tools: [add, echo, divide], text_argument: message}}
    skills: [{{id: add, name: Add numbers}}]
"""


@pytest.fixture
def client(tmp_path):
    cfg = parse_config(CONFIG.format(db=tmp_path / "t.db"), env={"KEY": "k"})

    def factory(agent, sessions):
        a = agent.adapter
        if a.type == "openai_chat":
            return OpenAIChatAdapter(a, sessions, httpx.AsyncClient(transport=httpx.ASGITransport(app=chat_app)))
        return McpAdapter(a, httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_app)))

    SESSIONS.clear()
    with TestClient(create_app(cfg, factory)) as c:
        yield c


def send(parts, context_id=None, message_id="m1"):
    message = {"messageId": message_id, "role": "ROLE_USER", "parts": parts}
    if context_id:
        message["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}


def task_of(response):
    assert response.status_code == 200, response.text
    return response.json()["result"]["task"]


def test_chat_conversation_continues_in_one_context_and_survives_in_the_database(client):
    first = task_of(client.post("/agents/assistant/rpc", json=send([{"text": "hello"}]), headers=HEADERS))
    assert first["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "turn 1: you said 'hello'" in first["artifacts"][0]["parts"][0]["text"]
    second = task_of(client.post("/agents/assistant/rpc", json=send([{"text": "again"}], first["contextId"], "m2"), headers=HEADERS))
    text = second["artifacts"][0]["parts"][0]["text"]
    assert "turn 2: you said 'again'; first was 'hello'" in text and "model tiny" in text
    other = task_of(client.post("/agents/assistant/rpc", json=send([{"text": "new"}], None, "m3"), headers=HEADERS))
    assert "turn 1" in other["artifacts"][0]["parts"][0]["text"]  # a new context starts fresh


def test_chat_upstream_failure_and_content_filter_map_to_task_states(client):
    boom = task_of(client.post("/agents/assistant/rpc", json=send([{"text": "boom"}]), headers=HEADERS))
    assert boom["status"]["state"] == "TASK_STATE_FAILED"
    blocked = task_of(client.post("/agents/assistant/rpc", json=send([{"text": "filter"}], None, "m2"), headers=HEADERS))
    assert blocked["status"]["state"] == "TASK_STATE_REJECTED"


def test_mcp_tool_call_through_the_proxy_with_arguments_and_text(client):
    added = task_of(client.post("/agents/calc/rpc", json=send([{"data": {"tool": "add", "arguments": {"a": 40, "b": 2}}}]), headers=HEADERS))
    assert added["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = [p for a in added["artifacts"] for p in a["parts"]]
    assert any(p.get("text") == "42" for p in parts) and any(p.get("data") == {"sum": 42} for p in parts)
    echoed = task_of(client.post("/agents/calc/rpc", json=send([{"data": {"tool": "echo"}}, ], None, "m2"), headers=HEADERS))
    assert echoed["status"]["state"] == "TASK_STATE_COMPLETED"
    assert not SESSIONS  # every MCP session was closed


def test_mcp_hidden_tool_is_unreachable_and_tool_errors_fail(client):
    hidden = task_of(client.post("/agents/calc/rpc", json=send([{"data": {"tool": "drop_database", "arguments": {}}}]), headers=HEADERS))
    assert hidden["status"]["state"] == "TASK_STATE_REJECTED"
    zero = task_of(client.post("/agents/calc/rpc", json=send([{"data": {"tool": "divide", "arguments": {"a": 1, "b": 0}}}], None, "m2"), headers=HEADERS))
    assert zero["status"]["state"] == "TASK_STATE_FAILED"


def test_facade_reaches_both(client):
    r = client.post("/agents/assistant/invoke", json={"message": "hi"}, headers={"X-API-Key": "k"})
    assert r.json()["state"] == "completed" and "turn 1" in r.json()["text"]
    r = client.post("/agents/calc/invoke", json={"data": {"tool": "add", "arguments": {"a": 1, "b": 2}}}, headers={"X-API-Key": "k"})
    assert r.json()["state"] == "completed" and r.json()["data"] == {"sum": 3}
