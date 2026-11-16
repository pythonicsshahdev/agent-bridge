import httpx
import pytest

from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import HttpSyncAdapterConfig


def adapter(handler, **overrides):
    cfg = HttpSyncAdapterConfig(type="http_sync", url="https://down.test/hook", **overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpSyncAdapter(cfg, client)


def ctx(text="hello", data=None, headers=None):
    return TaskContext(
        agent_slug="svc", task_id="t1", context_id="c1", text=text, data=data or [], headers=headers or {}
    )


async def test_json_success_maps_to_completed_data():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"orderId": 42})

    result = await adapter(handler).execute(ctx())
    assert result.state == AdapterState.COMPLETED and result.data == {"orderId": 42}
    assert b'"taskId": "t1"' in seen["body"] or b'"taskId":"t1"' in seen["body"]


async def test_single_data_part_is_sent_unchanged():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={})

    await adapter(handler).execute(ctx(text="", data=[{"sku": "A1", "qty": 2}]))
    assert httpx.Request("POST", "https://x", content=seen["body"]).read() == b'{"sku":"A1","qty":2}'


async def test_plain_text_and_empty_bodies():
    r = await adapter(lambda req: httpx.Response(200, text="ok")).execute(ctx())
    assert (r.state, r.text, r.data) == (AdapterState.COMPLETED, "ok", None)
    r = await adapter(lambda req: httpx.Response(204)).execute(ctx())
    assert r.state == AdapterState.COMPLETED and r.text is None and r.data is None


async def test_bearer_passthrough_forwards_incoming_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    a = adapter(handler, auth={"mode": "bearer_passthrough"})
    await a.execute(ctx(headers={"authorization": "Bearer jwt.abc"}))
    assert seen["auth"] == "Bearer jwt.abc"


async def test_bearer_passthrough_without_header_needs_auth_and_skips_call():
    def handler(request):
        raise AssertionError("downstream must not be called")

    r = await adapter(handler, auth={"mode": "bearer_passthrough"}).execute(ctx())
    assert r.state == AdapterState.AUTH_REQUIRED


async def test_static_header_and_extra_headers_sent():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={})

    a = adapter(
        handler,
        headers={"X-Tenant": "acme"},
        auth={"mode": "static_header", "header_name": "X-Key", "value": "s3cret"},
    )
    await a.execute(ctx())
    assert seen["x-key"] == "s3cret" and seen["x-tenant"] == "acme"


@pytest.mark.parametrize(
    "status, state",
    [
        (400, AdapterState.FAILED),
        (401, AdapterState.AUTH_REQUIRED),
        (403, AdapterState.AUTH_REQUIRED),
        (404, AdapterState.FAILED),
        (429, AdapterState.FAILED),
        (500, AdapterState.FAILED),
        (503, AdapterState.FAILED),
    ],
)
async def test_error_statuses_map_to_states(status, state):
    r = await adapter(lambda req: httpx.Response(status, text="nope")).execute(ctx())
    assert r.state == state and f"HTTP {status}" in r.error and "nope" in r.error


async def test_error_body_is_truncated():
    r = await adapter(lambda req: httpx.Response(500, text="x" * 5000)).execute(ctx())
    assert len(r.error) < 600


async def test_timeout_and_connect_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def refused(request):
        raise httpx.ConnectError("refused to 10.0.0.5")

    r = await adapter(timeout, timeout_seconds=5).execute(ctx())
    assert r.state == AdapterState.FAILED and "timed out after 5s" in r.error
    r = await adapter(refused).execute(ctx())
    assert r.state == AdapterState.FAILED and r.error == "Downstream unreachable"


async def test_get_uses_query_param_not_body():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), request.content
        return httpx.Response(200, json={})

    await adapter(handler, method="GET").execute(ctx(text="find me"))
    assert seen["url"].endswith("?text=find+me") and seen["body"] == b""
