import asyncio

import httpx
import pytest
from mocks.agentstudio_execute import BLOCKED, app as execute_app, reset_state

from a2a_bridge.adapters.agentstudio import AgentstudioAdapter
from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.session_store import InMemorySessionStore, SqlSessionStore
from a2a_bridge.adapters.sse import parse_sse
from a2a_bridge.config import AgentstudioAdapterConfig

BASE = {
    "type": "agentstudio",
    "execute_url": "https://exec.test",
    "account_id": "acct-1",
    "username": "mock-user",
    "api_token": "mock-token",
}


def make(deployment="dep-chat", store=None, **overrides):
    cfg = AgentstudioAdapterConfig(**{**BASE, "deployment_id": deployment, **overrides})
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=execute_app))
    return AgentstudioAdapter(cfg, store or InMemorySessionStore(), client)


def ctx(text="hello", data=None, context_id="ctx-1"):
    return TaskContext(agent_slug="a", task_id="t", context_id=context_id, text=text, data=data or [])


async def collect(adapter, context):
    return [r async for r in adapter.stream(context)]


@pytest.fixture(autouse=True)
def _reset():
    reset_state()


async def test_parse_sse_handles_events_comments_and_multiline_data():
    async def lines():
        for line in [": ping", "event: message", "data: a", "data: b", "", "event: [DONE]", "data: null"]:
            yield line

    assert [e async for e in parse_sse(lines())] == [("message", "a\nb"), ("[DONE]", "null")]


async def test_conversational_reply_completes_with_text():
    results = await collect(make(), ctx("hello"))
    assert results[-1].state == AdapterState.COMPLETED and results[-1].text == "Turn 1: you said hello"


async def test_action_progress_surfaces_as_working_but_thinking_does_not():
    results = await collect(make(), ctx("tool"))
    working = [r for r in results if r.state == AdapterState.WORKING]
    assert [r.text for r in working] == ["Calling Tool: lookup_order"]
    assert results[-1].state == AdapterState.COMPLETED


async def test_second_turn_in_same_context_continues_the_session():
    a = make()
    first = await collect(a, ctx("one"))
    second = await collect(a, ctx("two"))
    assert first[-1].text == "Turn 1: you said one" and second[-1].text == "Turn 2: you said two"
    other = await collect(a, ctx("three", context_id="ctx-2"))
    assert other[-1].text == "Turn 1: you said three"


async def test_session_mapping_survives_a_restart(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/s.db"
    for expected in ("Turn 1: you said a", "Turn 2: you said b"):  # fresh engine + adapter each time
        engine = create_async_engine(url)
        store = SqlSessionStore(engine)
        await store.initialize()
        result = (await collect(make(store=store), ctx("a" if "1" in expected else "b")))[-1]
        assert result.text == expected
        await engine.dispose()


async def test_guardrail_block_is_rejected():
    results = await collect(make("dep-guarded", blocked_message=BLOCKED), ctx("my ssn is 1"))
    assert results[-1].state == AdapterState.REJECTED and results[-1].error == BLOCKED
    ok = await collect(make("dep-guarded", blocked_message=BLOCKED), ctx("hello"))
    assert ok[-1].state == AdapterState.COMPLETED


async def test_without_blocked_message_configured_a_block_looks_like_a_normal_reply():
    results = await collect(make("dep-guarded"), ctx("my ssn is 1"))
    assert results[-1].state == AdapterState.COMPLETED


async def test_sse_error_event_fails_the_task():
    results = await collect(make(), ctx("error-event"))
    assert results[-1].state == AdapterState.FAILED and "model unavailable" in results[-1].error


async def test_structured_agent_round_trip():
    a = make("dep-structured", mode="structured")
    first = (await collect(a, ctx(data=[{"q": "x"}])))[-1]
    assert first.state == AdapterState.COMPLETED and first.data["echo"] == {"q": "x"} and first.data["turn"] == 1
    second = (await collect(a, ctx(text='{"q": "y"}')))[-1]  # JSON in text is accepted too
    assert second.data["echo"] == {"q": "y"} and second.data["turn"] == 2


async def test_structured_agent_rejects_non_object_input_without_calling_out():
    result = (await collect(make("dep-structured", mode="structured"), ctx("just words")))[-1]
    assert result.state == AdapterState.FAILED and "JSON object" in result.error


async def test_structured_success_false_is_failed():
    result = (await collect(make("dep-structured", mode="structured"), ctx(data=[{"fail": True}])))[-1]
    assert result.state == AdapterState.FAILED and "classify" in result.error


@pytest.mark.parametrize(
    "overrides, deployment, text, needle",
    [
        ({"api_token": "wrong"}, "dep-chat", "hi", "rejected the proxy's credentials"),
        ({}, "dep-missing", "hi", "not found"),
        ({}, "dep-chat", "boom", "HTTP 500"),
    ],
)
async def test_http_errors_are_failed_with_clear_reasons(overrides, deployment, text, needle):
    result = (await collect(make(deployment, **overrides), ctx(text)))[-1]
    assert result.state == AdapterState.FAILED and needle in result.error


async def test_credentials_failure_message_does_not_leak_the_token():
    result = (await collect(make(api_token="super-secret-token"), ctx("hi")))[-1]
    assert "super-secret-token" not in result.error


async def test_turn_overrunning_the_timeout_fails():
    result = (await collect(make(timeout_seconds=0.2), ctx("slow")))[-1]
    assert result.state == AdapterState.FAILED and "did not finish within 0.2s" in result.error


async def test_unreachable_host():
    def refuse(request):
        raise httpx.ConnectError("no route to 10.1.1.1")

    cfg = AgentstudioAdapterConfig(**{**BASE, "deployment_id": "dep-chat"})
    adapter = AgentstudioAdapter(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    result = (await collect(adapter, ctx("hi")))[-1]
    assert result.error == "Agentstudio unreachable"


async def test_data_only_message_is_sent_to_a_conversational_agent_as_json_text():
    result = (await collect(make(), ctx(text="", data=[{"sku": "A1"}])))[-1]
    assert result.text == 'Turn 1: you said {"sku": "A1"}'


def test_literal_api_token_in_yaml_is_rejected():
    from a2a_bridge.config import ConfigError, parse_config

    text = """
agents:
  - slug: s
    name: S
    adapter: {type: agentstudio, deployment_id: d, execute_url: "https://e.test", account_id: a, username: u, api_token: hunter2}
    skills: [{id: g, name: G}]
"""
    with pytest.raises(ConfigError, match="api_token"):
        parse_config(text, env={})
    ok = text.replace("hunter2", "\"${TOK}\"")
    assert parse_config(ok, env={"TOK": "x"}).agents[0].adapter.api_token == "x"
