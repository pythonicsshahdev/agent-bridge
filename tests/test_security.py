import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_bridge.config import ConfigError, InboundAuthConfig, parse_config
from a2a_bridge.security import InboundAuthMiddleware, validate_inbound


def client(**auth):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/agents/x/rpc", ok, methods=["POST"]),
            Route("/agents/x/.well-known/agent-card.json", ok),
            Route("/healthz", ok),
        ]
    )
    app.add_middleware(InboundAuthMiddleware, auth=InboundAuthConfig(**auth))
    return TestClient(app)


def test_api_key_required_for_rpc():
    c = client(api_keys=["k1", "k2"])
    assert c.post("/agents/x/rpc").status_code == 401
    assert c.post("/agents/x/rpc", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/agents/x/rpc", headers={"X-API-Key": "k2"}).status_code == 200


def test_card_and_health_stay_public():
    c = client(api_keys=["k1"])
    assert c.get("/agents/x/.well-known/agent-card.json").status_code == 200
    assert c.get("/healthz").status_code == 200


def test_custom_header_name():
    c = client(api_keys=["k"], header_name="X-Bridge-Key")
    assert c.post("/agents/x/rpc", headers={"X-API-Key": "k"}).status_code == 401
    assert c.post("/agents/x/rpc", headers={"X-Bridge-Key": "k"}).status_code == 200


def test_bearer_passthrough_requires_a_token_only():
    c = client(mode="bearer_passthrough")
    assert c.post("/agents/x/rpc").status_code == 401
    assert c.post("/agents/x/rpc", headers={"Authorization": "Basic abc"}).status_code == 401
    assert c.post("/agents/x/rpc", headers={"Authorization": "Bearer "}).status_code == 401
    assert c.post("/agents/x/rpc", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_mode_none_is_open_but_explicit():
    assert client(mode="none").post("/agents/x/rpc").status_code == 200


def test_api_key_mode_without_keys_fails_at_startup():
    with pytest.raises(ConfigError, match="api_keys"):
        validate_inbound(InboundAuthConfig())


def test_literal_inbound_key_rejected_in_yaml():
    text = """
server: {inbound_auth: {api_keys: [hunter2]}}
agents:
  - {slug: s, name: S, adapter: {type: http_sync, url: "https://x.test"}, skills: [{id: a, name: A}]}
"""
    with pytest.raises(ConfigError, match="environment variable"):
        parse_config(text, env={})
