"""End to end through the mounted FastAPI app with a mock downstream."""

import httpx
import pytest
from starlette.testclient import TestClient

from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import parse_config
from a2a_bridge.main import create_app

KEY = {"X-API-Key": "test-key", "A2A-Version": "1.0"}


def yaml_text(tmp_path, agents=1):
    one = """
  - slug: {slug}
    name: Agent {slug}
    adapter: {{type: http_sync, url: "https://down.test/{slug}", auth: {{mode: bearer_passthrough}}}}
    skills: [{{id: go, name: Go}}]
"""
    return f"""
server:
  database_url: "sqlite+aiosqlite:///{tmp_path}/t.db"
  public_base_url: "https://abc.ngrok.app"
  inbound_auth: {{api_keys: ["${{KEY}}"]}}
agents:""" + "".join(one.format(slug=f"a{i}") for i in range(agents))


def make_client(tmp_path, handler, agents=1):
    cfg = parse_config(yaml_text(tmp_path, agents), env={"KEY": "test-key"})

    def factory(agent):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return HttpSyncAdapter(agent.adapter, http)

    return TestClient(create_app(cfg, factory))


def rpc(method, params, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def send(text="make an order", **message):
    return rpc(
        "SendMessage",
        {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}], **message}},
    )


def test_card_served_publicly_with_tunnel_url(tmp_path):
    with make_client(tmp_path, lambda r: httpx.Response(200, json={}), agents=2) as c:
        card = c.get("/agents/a1/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["url"] == "https://abc.ngrok.app/agents/a1/rpc"
        assert set(card["securitySchemes"]) == {"apiKey", "bearer"}
        assert c.get("/healthz").json()["agents"] == ["a0", "a1"]


def test_rpc_requires_api_key(tmp_path):
    with make_client(tmp_path, lambda r: httpx.Response(200, json={})) as c:
        assert c.post("/agents/a0/rpc", json=send()).status_code == 401


def test_send_message_round_trip_forwards_bearer_and_returns_result(tmp_path):
    seen = {}

    def downstream(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"orderId": 42})

    with make_client(tmp_path, downstream) as c:
        r = c.post("/agents/a0/rpc", json=send(), headers={**KEY, "Authorization": "Bearer jwt.1"})
        assert r.status_code == 200, r.text
        task = r.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["artifacts"][0]["parts"][0]["data"] == {"orderId": 42}
        assert seen == {"auth": "Bearer jwt.1", "url": "https://down.test/a0"}

        got = c.post("/agents/a0/rpc", json=rpc("GetTask", {"id": task["id"]}), headers=KEY).json()
        assert got["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_downstream_error_becomes_failed_task(tmp_path):
    with make_client(tmp_path, lambda r: httpx.Response(500, text="boom")) as c:
        r = c.post("/agents/a0/rpc", json=send(), headers={**KEY, "Authorization": "Bearer t"})
        task = r.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        assert "HTTP 500" in task["status"]["message"]["parts"][0]["text"]


def test_missing_bearer_yields_auth_required(tmp_path):
    with make_client(tmp_path, lambda r: httpx.Response(200, json={})) as c:
        task = c.post("/agents/a0/rpc", json=send(), headers=KEY).json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"


def test_single_agent_is_also_served_at_root(tmp_path):
    with make_client(tmp_path, lambda r: httpx.Response(200, json={"ok": 1})) as c:
        card = c.get("/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["url"] == "https://abc.ngrok.app/rpc"
        r = c.post("/rpc", json=send(), headers={**KEY, "Authorization": "Bearer t"})
        assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_unimplemented_adapter_is_skipped(tmp_path):
    text = yaml_text(tmp_path) + """
  - slug: later
    name: Later
    adapter: {type: http_async, url: "https://x.test/s", status_url: "https://x.test/s/{job_id}"}
    skills: [{id: g, name: G}]
"""
    from a2a_bridge.adapters import create_adapter

    cfg = parse_config(text, env={"KEY": "test-key"})
    with TestClient(create_app(cfg, create_adapter)) as c:
        assert c.get("/healthz").json()["agents"] == ["a0"]
        assert c.get("/agents/later/.well-known/agent-card.json").status_code == 404


def test_v0_3_client_without_version_header_is_served(tmp_path):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"messageId": "m1", "role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    with make_client(tmp_path, lambda r: httpx.Response(200, json={"ok": True})) as c:
        r = c.post("/agents/a0/rpc", json=body, headers={"X-API-Key": "test-key", "Authorization": "Bearer t"})
        assert r.status_code == 200 and "error" not in r.json(), r.text
        assert r.json()["result"]["status"]["state"] == "completed"
