"""Runs against a real Postgres. Skipped unless A2A_TEST_POSTGRES_URL is set, e.g.
   docker run -d -e POSTGRES_PASSWORD=test -e POSTGRES_DB=a2a -p 55432:5432 postgres:16-alpine
   A2A_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:test@localhost:55432/a2a pytest tests/test_postgres.py
"""

import os
import uuid

import httpx
import pytest
from mocks.agentstudio_execute import app as execute_app, reset_state

from a2a_bridge.adapters.agentstudio import AgentstudioAdapter
from a2a_bridge.adapters.session_store import SqlSessionStore
from a2a_bridge.config import parse_config
from a2a_bridge.main import create_app

URL = os.environ.get("A2A_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="A2A_TEST_POSTGRES_URL not set")

YAML = """
server:
  database_url: "%s"
  inbound_auth: {api_keys: ["${KEY}"], rate_limit: null}
agents:
  - slug: chat
    name: Chat
    adapter: {type: agentstudio, identity: service_account, deployment_id: dep-chat, execute_url: "https://e.test", account_id: a, username: mock-user, api_token: "${TOK}"}
    skills: [{id: ask, name: Ask}]
"""
HEADERS = {"X-API-Key": "k", "A2A-Version": "1.0"}


def build():
    cfg = parse_config(YAML % URL, env={"KEY": "k", "TOK": "mock-token"})
    factory = lambda agent, sessions: AgentstudioAdapter(agent.adapter, sessions, httpx.AsyncClient(transport=httpx.ASGITransport(app=execute_app)))
    return create_app(cfg, factory)


async def send(client, text, context_id=None):
    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context_id:
        message["contextId"] = context_id
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    r = await client.post("/agents/chat/rpc", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["result"]["task"]


async def test_conversations_and_tasks_survive_an_app_restart_on_postgres():
    reset_state()
    context_id = None
    for expected in ("Turn 1: you said one", "Turn 2: you said two"):  # a brand new app each time
        app = build()
        async with app.router.lifespan_context(app):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
            assert (await client.get("/readyz")).json() == {"status": "ready"}
            task = await send(client, "one" if "one" in expected else "two", context_id)
            assert task["artifacts"][0]["parts"][0]["text"] == expected
            context_id = task["contextId"]
            got = await client.post(
                "/agents/chat/rpc",
                json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task["id"]}},
                headers=HEADERS,
            )
            assert got.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
            await client.aclose()


async def test_session_store_upsert_is_idempotent_and_owner_scoped():
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(URL)
    store = SqlSessionStore(engine)
    await store.initialize()
    slug = f"s-{uuid.uuid4().hex[:8]}"
    await store.set(slug, "alice", "ctx", "S1")
    await store.set(slug, "alice", "ctx", "S2")  # update, not a duplicate-key error
    assert await store.get(slug, "alice", "ctx") == "S2"
    assert await store.get(slug, "bob", "ctx") is None
    await engine.dispose()
