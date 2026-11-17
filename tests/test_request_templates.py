import json

import httpx
import pytest
from pydantic import ValidationError

from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.adapters.templating import render_template
from a2a_bridge.config import HttpSyncAdapterConfig


def ctx(text="hello", data=None):
    return TaskContext(agent_slug="svc", task_id="t1", context_id="c1", text=text, data=data or [])


def adapter(handler, **overrides):
    cfg = HttpSyncAdapterConfig(type="http_sync", url="https://down.test/invoke", **overrides)
    return HttpSyncAdapter(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_whole_placeholder_keeps_type_and_embedded_is_text():
    c = ctx(data=[{"qty": 2}])
    assert render_template("{{data}}", c) == [{"qty": 2}]
    assert render_template("{{data.0.qty}}", c) == 2
    assert render_template("n={{data.0.qty}} {{text}}", c) == "n=2 hello"
    assert render_template("{{data.5.x}}", c) is None
    assert render_template("a{{data.5.x}}b", c) == "ab"


def test_nested_template_with_keys_and_lists():
    out = render_template({"input": {"question": "{{text}}"}, "config": {"configurable": {"thread_id": "{{context_id}}"}}, "n": 1}, ctx())
    assert out == {"input": {"question": "hello"}, "config": {"configurable": {"thread_id": "c1"}}, "n": 1}


def test_unknown_placeholder_fails_at_config_time():
    with pytest.raises(ValidationError) as exc:
        HttpSyncAdapterConfig(type="http_sync", url="https://x.test", request_template={"a": "{{txt}}"})
    assert "txt" in str(exc.value)


async def test_langserve_shape_round_trip():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"content": "Hi there"}, "metadata": {"run_id": "r1"}})

    a = adapter(
        handler,
        request_template={"input": "{{text}}", "config": {"configurable": {"thread_id": "{{context_id}}"}}},
        response_text_path="output.content",
    )
    r = await a.execute(ctx("what is 2+2"))
    assert seen["body"] == {"input": "what is 2+2", "config": {"configurable": {"thread_id": "c1"}}}
    assert r.state == AdapterState.COMPLETED and r.text == "Hi there" and r.data is None


async def test_data_path_and_missing_text_path():
    body = {"result": {"rows": [1, 2]}, "other": 1}
    r = await adapter(lambda req: httpx.Response(200, json=body), response_data_path="result").execute(ctx())
    assert r.data == {"rows": [1, 2]} and r.text is None
    r = await adapter(lambda req: httpx.Response(200, json=body), response_text_path="nope").execute(ctx())
    assert r.state == AdapterState.COMPLETED and r.text is None and r.data == body  # nothing matched: keep the body


async def test_get_template_goes_in_the_query_string():
    seen = {}

    def handler(request):
        seen["q"] = dict(request.url.params)
        return httpx.Response(200, json={})

    await adapter(handler, method="GET", request_template={"q": "{{text}}", "thread": "{{context_id}}"}).execute(ctx("x"))
    assert seen["q"] == {"q": "x", "thread": "c1"}
