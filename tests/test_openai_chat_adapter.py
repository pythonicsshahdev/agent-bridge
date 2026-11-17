import json

import httpx
import pytest
from pydantic import ValidationError

from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.openai_chat import OpenAIChatAdapter, completions_url
from a2a_bridge.adapters.session_store import InMemorySessionStore
from a2a_bridge.config import OpenAIChatAdapterConfig


def ctx(text="hi", context_id="c1", principal="alice", headers=None, data=None):
    return TaskContext(agent_slug="chat", task_id="t", context_id=context_id, principal=principal, text=text, data=data or [], headers=headers or {})


def make(handler, store=None, **overrides):
    cfg = OpenAIChatAdapterConfig(type="openai_chat", url="https://llm.test/v1", model="m1", **overrides)
    return OpenAIChatAdapter(cfg, store or InMemorySessionStore(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def reply(text, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]})


def test_url_forms():
    assert completions_url("https://h/v1") == "https://h/v1/chat/completions"
    assert completions_url("https://h/v1/") == "https://h/v1/chat/completions"
    assert completions_url("https://h/v1/chat/completions") == "https://h/v1/chat/completions"


def test_the_proxy_owns_model_messages_and_stream():
    with pytest.raises(ValidationError):
        OpenAIChatAdapterConfig(type="openai_chat", url="https://h/v1", model="m", parameters={"messages": []})


async def test_history_carries_across_turns_and_is_private_per_caller():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return reply(f"answer {len(sent)}")

    a = make(handler, parameters={"temperature": 0.2})
    assert (await a.execute(ctx("first"))).text == "answer 1"
    assert (await a.execute(ctx("second"))).text == "answer 2"
    assert sent[1]["messages"] == [
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "answer 1"}, {"role": "user", "content": "second"},
    ]
    assert sent[1]["temperature"] == 0.2 and sent[1]["model"] == "m1" and sent[1]["stream"] is False
    await a.execute(ctx("hello", principal="bob"))  # same context id, different caller
    assert sent[2]["messages"] == [{"role": "user", "content": "hello"}]


async def test_history_window_never_starts_on_an_orphaned_reply():
    store = InMemorySessionStore()
    a = make(lambda r: reply("ok"), store, max_history_messages=3)
    for i in range(4):
        await a.execute(ctx(f"q{i}"))
    kept = await store.get_history("chat", "alice", "c1")
    assert kept[0]["role"] == "user" and len(kept) <= 3


async def test_zero_history_means_every_turn_stands_alone():
    sent = []
    a = make(lambda r: (sent.append(json.loads(r.content)), reply("ok"))[1], max_history_messages=0)
    await a.execute(ctx("a")); await a.execute(ctx("b"))
    assert len(sent[1]["messages"]) == 1


async def test_failures_do_not_poison_the_history():
    store = InMemorySessionStore()
    calls = iter([httpx.Response(500, text="boom"), reply("fine")])
    a = make(lambda r: next(calls), store)
    r = await a.execute(ctx("one"))
    assert r.state == AdapterState.FAILED and "500" in r.error
    assert await store.get_history("chat", "alice", "c1") == []
    assert (await a.execute(ctx("two"))).text == "fine"


@pytest.mark.parametrize("response,state", [
    (httpx.Response(401, text="no"), AdapterState.AUTH_REQUIRED),
    (httpx.Response(200, json={"choices": []}), AdapterState.FAILED),
    (httpx.Response(200, text="not json"), AdapterState.FAILED),
    (reply(""), AdapterState.FAILED),
    (reply("blocked", finish="content_filter"), AdapterState.REJECTED),
])
async def test_error_mapping(response, state):
    assert (await make(lambda r: response).execute(ctx())).state == state


async def test_content_parts_and_data_only_turns():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "finish_reason": "stop"}]})

    r = await make(handler).execute(ctx(text="", data=[{"sku": "A1"}]))
    assert r.text == "ab" and sent[0]["messages"][0]["content"] == '{"sku": "A1"}'


async def test_empty_turn_asks_for_input_and_auth_header_is_forwarded():
    a = make(lambda r: reply("x"), auth={"mode": "bearer_passthrough"})
    assert (await a.execute(ctx(text="", headers={"authorization": "Bearer t"}))).state == AdapterState.INPUT_REQUIRED
    assert (await a.execute(ctx())).state == AdapterState.AUTH_REQUIRED  # no bearer sent


async def test_timeout_and_unreachable():
    def boom(request):
        raise httpx.ConnectTimeout("slow")

    assert "timed out" in (await make(boom).execute(ctx())).error
    def down(request):
        raise httpx.ConnectError("no")

    assert (await make(down).execute(ctx())).error == "Chat endpoint unreachable"
