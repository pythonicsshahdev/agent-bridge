import asyncio

import httpx
import pytest
from mocks.boomi_listener import _jobs, app as boomi_app, reset_state
from starlette.testclient import TestClient  # noqa: F401

from a2a_bridge.adapters.base import AdapterState, TaskContext
from a2a_bridge.adapters.http_async import _MISSING, HttpAsyncAdapter, get_path
from a2a_bridge.config import HttpAsyncAdapterConfig

BASE = {
    "type": "http_async",
    "url": "http://boomi.test/ws/simple/startExport",
    "status_url": "http://boomi.test/ws/simple/exportStatus/{job_id}",
    "cancel_url": "http://boomi.test/ws/simple/exportCancel/{job_id}",
    "job_id_path": "executionId",
    "result_path": "result",
    "error_path": "message",
    "completed_values": ["COMPLETE"],
    "failed_values": ["ERROR", "ABORTED"],
    "poll_interval_seconds": 0.01,
    "max_poll_seconds": 5,
    "auth": {"mode": "bearer_passthrough"},
}


def make(handler=None, **overrides):
    cfg = HttpAsyncAdapterConfig(**{**BASE, **overrides})
    transport = httpx.MockTransport(handler) if handler else httpx.ASGITransport(app=boomi_app)
    return HttpAsyncAdapter(cfg, httpx.AsyncClient(transport=transport))


def ctx(data=None, task_id="t1", bearer="Bearer jwt.1"):
    return TaskContext(
        agent_slug="x", task_id=task_id, context_id="c", data=[data or {}], headers={"authorization": bearer} if bearer else {}
    )


async def collect(adapter, context):
    return [r async for r in adapter.stream(context)]


@pytest.fixture(autouse=True)
def _reset():
    reset_state()


def test_get_path_walks_dicts_and_lists():
    body = {"a": {"b": [{"c": 7}]}}
    assert get_path(body, "a.b.0.c") == 7 and get_path(body, None) is body
    assert get_path(body, "a.x") is _MISSING and get_path(body, "a.b.5") is _MISSING and get_path(body, "nope") is _MISSING


async def test_job_runs_to_completion_and_returns_the_result():
    results = await collect(make(), ctx({"polls": 3}))
    assert results[0].state == AdapterState.WORKING and "EXE-1 started" in results[0].text
    final = results[-1]
    assert final.state == AdapterState.COMPLETED and final.data == {"rows": 42, "file": "export.csv"}


async def test_progress_is_reported_once_per_status_change_not_once_per_poll():
    results = await collect(make(), ctx({"polls": 5}))
    texts = [r.text for r in results if r.state == AdapterState.WORKING]
    assert texts == ["Job EXE-1 started", "Status: RUNNING"]


async def test_failed_job_reports_the_reason():
    final = (await collect(make(), ctx({"polls": 1, "fail": True})))[-1]
    assert final.state == AdapterState.FAILED and "ERROR" in final.error and "disk full" in final.error


async def test_the_callers_token_is_used_for_every_downstream_call():
    await collect(make(), ctx({"polls": 2}, bearer="Bearer caller-token"))
    assert _jobs["EXE-1"]["auth"] == "Bearer caller-token"


async def test_missing_bearer_needs_auth_and_calls_nothing():
    def boom(request):
        raise AssertionError("no downstream call without credentials")

    final = (await collect(make(boom), ctx(bearer=None)))[-1]
    assert final.state == AdapterState.AUTH_REQUIRED


@pytest.mark.parametrize(
    "status, state",
    [(500, AdapterState.FAILED), (401, AdapterState.AUTH_REQUIRED), (403, AdapterState.AUTH_REQUIRED), (400, AdapterState.FAILED)],
)
async def test_trigger_errors_map_to_states(status, state):
    final = (await collect(make(lambda r: httpx.Response(status, text="no")), ctx()))[-1]
    assert final.state == state and f"HTTP {status}" in final.error


@pytest.mark.parametrize("body", [{"other": 1}, {"executionId": None}, {"executionId": ""}, {"executionId": True}, [1]])
async def test_start_response_without_a_usable_job_id_fails(body):
    final = (await collect(make(lambda r: httpx.Response(202, json=body)), ctx()))[-1]
    assert final.state == AdapterState.FAILED and "no job id" in final.error


async def test_non_json_start_response_fails():
    final = (await collect(make(lambda r: httpx.Response(202, text="accepted")), ctx()))[-1]
    assert final.state == AdapterState.FAILED


async def test_a_transient_poll_error_is_tolerated():
    results = await collect(make(), ctx({"polls": 3, "flaky": True}))
    assert results[-1].state == AdapterState.COMPLETED


async def test_persistent_poll_errors_give_up():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"executionId": "J"})
        return httpx.Response(503, text="down")

    final = (await collect(make(handler, max_poll_errors=3), ctx()))[-1]
    assert final.state == AdapterState.FAILED and "after 3 attempts" in final.error


async def test_unknown_job_mid_poll_fails_immediately():
    def handler(request):
        return httpx.Response(202, json={"executionId": "J"}) if request.method == "POST" else httpx.Response(404, text="gone")

    final = (await collect(make(handler), ctx()))[-1]
    assert final.state == AdapterState.FAILED and "HTTP 404" in final.error


async def test_a_job_that_overruns_is_failed_and_canceled_downstream():
    adapter = make(max_poll_seconds=0.15)
    final = (await collect(adapter, ctx({"polls": 10_000})))[-1]
    assert final.state == AdapterState.FAILED and "did not finish within 0.15s" in final.error
    assert _jobs["EXE-1"]["cancels"] == 1


async def test_a_hostile_job_id_cannot_rewrite_the_status_path():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(202, json={"executionId": "../../admin?x=1"})
        return httpx.Response(200, json={"status": "COMPLETE"})

    await collect(make(handler), ctx())
    assert seen[-1] == "http://boomi.test/ws/simple/exportStatus/..%2F..%2Fadmin%3Fx%3D1"


async def test_cancel_reaches_the_downstream_job_with_the_callers_credentials():
    adapter = make(poll_interval_seconds=0.02)
    task = asyncio.create_task(collect(adapter, ctx({"polls": 10_000}, task_id="t9")))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if "EXE-1" in _jobs and _jobs["EXE-1"]["calls"] >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.cancel("t9")  # what the executor does after the framework cancels the run
    assert _jobs["EXE-1"]["cancels"] == 1
    await adapter.cancel("t9")  # nothing left to cancel; must not raise
    assert _jobs["EXE-1"]["cancels"] == 1


async def test_without_a_cancel_url_cancel_is_a_no_op():
    adapter = make(cancel_url=None, max_poll_seconds=0.1)
    await collect(adapter, ctx({"polls": 10_000}))
    assert _jobs["EXE-1"]["cancels"] == 0


def test_cancel_and_status_urls_must_contain_the_job_placeholder():
    with pytest.raises(ValueError, match="job_id"):
        HttpAsyncAdapterConfig(**{**BASE, "cancel_url": "http://boomi.test/cancel"})


# ---------------------------------------------------------------- through the proxy

APP_YAML = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  inbound_auth: {{api_keys: ["${{KEY}}"], rate_limit: null}}
agents:
  - slug: export
    name: Export
    adapter:
      type: http_async
      url: "http://boomi.test/ws/simple/startExport"
      status_url: "http://boomi.test/ws/simple/exportStatus/{{job_id}}"
      cancel_url: "http://boomi.test/ws/simple/exportCancel/{{job_id}}"
      job_id_path: executionId
      result_path: result
      completed_values: [COMPLETE]
      failed_values: [ERROR, ABORTED]
      poll_interval_seconds: 0.02
      auth: {{mode: bearer_passthrough}}
    skills: [{{id: run, name: Run export}}]
"""
HDR = {"X-API-Key": "k", "Authorization": "Bearer jwt.1"}


async def app_client(tmp_path):
    from a2a_bridge.config import parse_config
    from a2a_bridge.main import create_app

    cfg = parse_config(APP_YAML.format(db=tmp_path / "t.db"), env={"KEY": "k"})
    factory = lambda agent, _s: HttpAsyncAdapter(agent.adapter, httpx.AsyncClient(transport=httpx.ASGITransport(app=boomi_app)))
    app = create_app(cfg, factory)
    return app


async def test_long_job_is_working_then_completes_through_the_facade(tmp_path):
    app = await app_client(tmp_path)
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
        first = (await c.post("/agents/export/invoke", json={"data": {"polls": 10}, "wait_seconds": 0.05}, headers=HDR)).json()
        assert first["state"] == "working" and first["task_id"]
        done = (await c.get(f"/agents/export/tasks/{first['task_id']}", params={"wait_seconds": 10}, headers=HDR)).json()
        assert done["state"] == "completed" and done["data"] == {"rows": 42, "file": "export.csv"}
        await c.aclose()


async def test_canceling_through_the_facade_cancels_the_downstream_job(tmp_path):
    app = await app_client(tmp_path)
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p")
        first = (await c.post("/agents/export/invoke", json={"data": {"polls": 100000}, "wait_seconds": 0.1}, headers=HDR)).json()
        assert first["state"] == "working"
        canceled = (await c.post(f"/agents/export/tasks/{first['task_id']}/cancel", headers=HDR)).json()
        assert canceled["state"] == "canceled"
        assert _jobs["EXE-1"]["cancels"] == 1
        await c.aclose()
