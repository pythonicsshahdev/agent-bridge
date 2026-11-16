"""Proxy -> adapter -> mock Boomi listener, wired in-process (no sockets)."""

import httpx
import pytest
from mocks.boomi_listener import _orders, reset_state, app as boomi_app
from starlette.testclient import TestClient

from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import parse_config
from a2a_bridge.main import create_app

HEADERS = {"X-API-Key": "k", "A2A-Version": "1.0", "Authorization": "Bearer jwt.abc"}

CONFIG = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  inbound_auth: {{api_keys: ["${{KEY}}"]}}
agents:
  - slug: orders
    name: Orders
    adapter: {{type: http_sync, url: "http://boomi.test/ws/simple/createOrder", auth: {{mode: bearer_passthrough}}}}
    skills: [{{id: create, name: Create}}]
  - slug: broken
    name: Broken
    adapter: {{type: http_sync, url: "http://boomi.test/ws/simple/alwaysFails"}}
    skills: [{{id: go, name: Go}}]
"""


@pytest.fixture
def client(tmp_path):
    cfg = parse_config(CONFIG.format(db=tmp_path / "t.db"), env={"KEY": "k"})

    def factory(agent, _sessions):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=boomi_app))
        return HttpSyncAdapter(agent.adapter, http)

    reset_state()
    with TestClient(create_app(cfg, factory)) as c:
        yield c


def message(parts):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": parts}},
    }


def task_of(response):
    assert response.status_code == 200, response.text
    return response.json()["result"]["task"]


def test_structured_order_round_trip_keeps_integers(client):
    task = task_of(client.post("/agents/orders/rpc", json=message([{"data": {"sku": "A1", "qty": 2}}]), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["orderId"] == "ORD-1001" and result["status"] == "CREATED"
    # The downstream must see qty as the integer 2, not the protobuf double 2.0.
    assert _orders["ORD-1001"]["received"] == {"sku": "A1", "qty": 2}
    assert isinstance(_orders["ORD-1001"]["received"]["qty"], int)


def test_text_message_goes_in_envelope(client):
    task = task_of(client.post("/agents/orders/rpc", json=message([{"text": "two widgets please"}]), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _orders["ORD-1001"]["received"]["text"] == "two widgets please"


def test_downstream_validation_error_is_a_failed_task(client):
    task = task_of(client.post("/agents/orders/rpc", json=message([{"data": {"qty": 1}}]), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "HTTP 400" in task["status"]["message"]["parts"][0]["text"]


def test_downstream_500_is_a_failed_task(client):
    task = task_of(client.post("/agents/broken/rpc", json=message([{"text": "go"}]), headers=HEADERS))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "HTTP 500" in task["status"]["message"]["parts"][0]["text"]


def test_second_turn_in_same_context_creates_a_new_task(client):
    first = task_of(client.post("/agents/orders/rpc", json=message([{"data": {"sku": "A"}}]), headers=HEADERS))
    body = message([{"data": {"sku": "B"}}])
    body["params"]["message"].update(messageId="m2", contextId=first["contextId"])
    second = task_of(client.post("/agents/orders/rpc", json=body, headers=HEADERS))
    assert second["contextId"] == first["contextId"] and second["id"] != first["id"]
    assert [o for o in _orders] == ["ORD-1001", "ORD-1002"]
