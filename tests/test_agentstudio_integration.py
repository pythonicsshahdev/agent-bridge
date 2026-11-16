"""A2A client -> proxy -> Agentstudio adapter -> mock Execute API, with the real DB session map."""

import httpx
import pytest
from mocks.agentstudio_execute import BLOCKED, app as execute_app, reset_state
from starlette.testclient import TestClient

from a2a_bridge.adapters.agentstudio import AgentstudioAdapter
from a2a_bridge.config import parse_config
from a2a_bridge.main import create_app

HEADERS = {"X-API-Key": "k", "A2A-Version": "1.0"}

CONFIG = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  inbound_auth: {{api_keys: ["${{KEY}}"]}}
agents:
  - slug: chat
    name: Chat
    adapter: &base
      type: agentstudio
      deployment_id: dep-chat
      execute_url: "https://exec.test"
      account_id: acct-1
      identity: service_account
      username: mock-user
      api_token: "${{TOK}}"
    skills: [{{id: ask, name: Ask}}]
  - slug: guarded
    name: Guarded
    adapter:
      <<: *base
      deployment_id: dep-guarded
      blocked_message: "{blocked}"
    skills: [{{id: ask, name: Ask}}]
"""


@pytest.fixture
def client(tmp_path):
    cfg = parse_config(CONFIG.format(db=tmp_path / "t.db", blocked=BLOCKED), env={"KEY": "k", "TOK": "mock-token"})

    def factory(agent, sessions):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=execute_app))
        return AgentstudioAdapter(agent.adapter, sessions, http)

    reset_state()
    with TestClient(create_app(cfg, factory)) as c:
        yield c


def send(text, context_id=None, message_id="m1"):
    message = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context_id:
        message["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}


def task_of(response):
    assert response.status_code == 200, response.text
    return response.json()["result"]["task"]


def reply_text(task):
    return task["artifacts"][0]["parts"][0]["text"]


def test_conversation_continues_across_a2a_turns_in_one_context(client):
    first = task_of(client.post("/agents/chat/rpc", json=send("hello"), headers=HEADERS))
    assert first["status"]["state"] == "TASK_STATE_COMPLETED" and reply_text(first) == "Turn 1: you said hello"
    second = task_of(client.post("/agents/chat/rpc", json=send("again", first["contextId"], "m2"), headers=HEADERS))
    assert reply_text(second) == "Turn 2: you said again" and second["id"] != first["id"]
    fresh = task_of(client.post("/agents/chat/rpc", json=send("new topic"), headers=HEADERS))
    assert reply_text(fresh) == "Turn 1: you said new topic"


def test_guardrail_block_surfaces_as_rejected(client):
    task = task_of(client.post("/agents/guarded/rpc", json=send("my ssn is 123"), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert task["status"]["message"]["parts"][0]["text"] == BLOCKED


def test_agentstudio_failure_surfaces_as_failed_without_secrets(client):
    task = task_of(client.post("/agents/chat/rpc", json=send("boom"), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "HTTP 500" in task["status"]["message"]["parts"][0]["text"]
    assert "mock-token" not in str(task)
