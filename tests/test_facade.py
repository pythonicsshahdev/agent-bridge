"""REST facade and outbound A2A client, end to end and in process."""

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest
from mocks.agentstudio_execute import app as execute_app, reset_state

from a2a_bridge.adapters.agentstudio import AgentstudioAdapter
from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter
from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.client import ClientCredentialsToken, RemoteTarget
from a2a_bridge.config import ConfigError, RemoteAuthConfig, parse_config
from a2a_bridge.main import create_app


def h(key):
    return hashlib.sha256(key.encode()).hexdigest()


ALICE, BOB = {"X-API-Key": "alice-key"}, {"X-API-Key": "bob-key"}
BOOMI = {"X-Boomi-Authorization": "Basic " + base64.b64encode(b"BOOMI_TOKEN.mock-user:mock-token").decode()}


class SlowAdapter(BaseAdapter):
    def __init__(self):
        self.release = asyncio.Event()

    async def execute(self, context):
        return AdapterResult(state=AdapterState.COMPLETED, text="done")

    async def stream(self, context):
        yield AdapterResult(state=AdapterState.WORKING, text="started")
        await self.release.wait()
        yield AdapterResult(state=AdapterState.COMPLETED, text="finished")


class AskerAdapter(BaseAdapter):
    async def execute(self, context):
        return AdapterResult(state=AdapterState.COMPLETED, text="ok")

    async def stream(self, context):
        if "large" in context.text:
            yield AdapterResult(state=AdapterState.COMPLETED, text="ordered large")
        else:
            yield AdapterResult(state=AdapterState.INPUT_REQUIRED, text="Which size?")


class FixedAdapter(BaseAdapter):
    def __init__(self, result):
        self.result = result

    async def execute(self, context):
        return self.result


def agents_yaml():
    def block(slug, adapter="{type: http_sync, url: 'https://down.test/x'}"):
        return f"  - {{slug: {slug}, name: {slug.title()} Agent, description: Does {slug} things, adapter: {adapter}, skills: [{{id: s, name: Skill for {slug}, description: handles {slug}}}]}}\n"

    agentstudio = "{type: agentstudio, deployment_id: dep-chat, execute_url: 'https://exec.test', account_id: a1}"
    return "".join(block(s) for s in ("echo", "slow", "asker", "failer", "rejector")) + block("agent-b", agentstudio)


def local_yaml(tmp_path, facade="true", remotes=""):
    return f"""
server:
  database_url: "sqlite+aiosqlite:///{tmp_path}/local.db"
  public_base_url: "https://proxy.example"
  facade: {{enabled: {facade}}}
  inbound_auth:
    rate_limit: null
    clients:
      - {{id: alice, key_sha256: "{h('alice-key')}"}}
      - {{id: bob, key_sha256: "{h('bob-key')}", scopes: ["agents:echo"]}}
agents:
{agents_yaml()}
remote_agents:{remotes or " []"}
"""


def make_adapters(handles):
    def factory(agent, sessions):
        slug = agent.slug
        if slug == "slow":
            handles["slow"] = SlowAdapter()
            return handles["slow"]
        if slug == "asker":
            return AskerAdapter()
        if slug == "failer":
            return FixedAdapter(AdapterResult(state=AdapterState.FAILED, error="backend exploded"))
        if slug == "rejector":
            return FixedAdapter(AdapterResult(state=AdapterState.REJECTED, error="policy says no"))
        if slug == "agent-b":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=execute_app))
            return AgentstudioAdapter(agent.adapter, sessions, client)
        reply = httpx.Response(200, json={"qty": 2, "ok": True})
        return HttpSyncAdapter(agent.adapter, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: reply)))

    return factory


@asynccontextmanager
async def stack(tmp_path, facade="true", remotes="", remote_factory=RemoteTarget, config_text=None):
    handles = {}
    cfg = parse_config(config_text or local_yaml(tmp_path, facade, remotes), env={"R_KEY": "remote-key", "R_SECRET": "s3"})
    app = create_app(cfg, make_adapters(handles), remote_factory)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test")
        try:
            yield client, handles
        finally:
            await client.aclose()


@pytest.fixture(autouse=True)
def _reset():
    reset_state()


async def invoke(client, slug, body, headers=ALICE):
    return await client.post(f"/agents/{slug}/invoke", json=body, headers=headers)


# ---------------------------------------------------------------- local targets


async def test_invoke_returns_a_small_task_view_with_integers_restored(tmp_path):
    async with stack(tmp_path) as (c, _):
        r = await invoke(c, "echo", {"message": "hi"})
        view = r.json()
        assert r.status_code == 200 and view["state"] == "completed"
        assert view["data"] == {"qty": 2, "ok": True} and isinstance(view["data"]["qty"], int)
        assert view["task_id"] and view["context_id"]


async def test_slow_task_returns_working_then_can_be_polled_to_completion(tmp_path):
    async with stack(tmp_path) as (c, handles):
        first = (await invoke(c, "slow", {"message": "go", "wait_seconds": 0.3})).json()
        assert first["state"] == "working" and first["task_id"]
        handles["slow"].release.set()
        done = (await c.get(f"/agents/slow/tasks/{first['task_id']}", params={"wait_seconds": 3}, headers=ALICE)).json()
        assert done["state"] == "completed" and done["text"] == "finished"


async def test_a_working_task_can_be_canceled(tmp_path):
    async with stack(tmp_path) as (c, _):
        first = (await invoke(c, "slow", {"message": "go", "wait_seconds": 0.2})).json()
        r = await c.post(f"/agents/slow/tasks/{first['task_id']}/cancel", headers=ALICE)
        assert r.status_code == 200 and r.json()["state"] == "canceled"


async def test_input_required_can_be_answered_on_the_same_task(tmp_path):
    async with stack(tmp_path) as (c, _):
        first = (await invoke(c, "asker", {"message": "order a shirt"})).json()
        assert first["state"] == "input_required" and first["message"] == "Which size?"
        second = (await invoke(c, "asker", {"message": "large", "task_id": first["task_id"], "context_id": first["context_id"]})).json()
        assert second["state"] == "completed" and second["text"] == "ordered large"


async def test_failures_and_rejections_carry_the_reason(tmp_path):
    async with stack(tmp_path) as (c, _):
        failed = (await invoke(c, "failer", {"message": "x"})).json()
        rejected = (await invoke(c, "rejector", {"message": "x"})).json()
    assert (failed["state"], failed["message"]) == ("failed", "backend exploded")
    assert (rejected["state"], rejected["message"]) == ("rejected", "policy says no")


async def test_scopes_and_task_ownership_apply_to_the_facade(tmp_path):
    async with stack(tmp_path) as (c, handles):
        assert (await invoke(c, "slow", {"message": "x"}, BOB)).status_code == 403  # bob only has agents:echo
        assert (await invoke(c, "echo", {"message": "x"}, BOB)).status_code == 200
        assert (await invoke(c, "echo", {"message": "x"}, {})).status_code == 401
        mine = (await invoke(c, "echo", {"message": "x"})).json()
        # bob cannot read alice's task even through an agent he can call
        r = await c.get(f"/agents/echo/tasks/{mine['task_id']}", headers=BOB)
        assert r.status_code == 404


@pytest.mark.parametrize("body", [{}, {"message": "  "}, {"message": "x", "bogus": 1}, {"message": "x", "wait_seconds": -1}])
async def test_invalid_requests_are_422_with_a_reason(tmp_path, body):
    async with stack(tmp_path) as (c, _):
        r = await invoke(c, "echo", body)
        assert r.status_code == 422 and r.json()["error"]


async def test_non_json_body_is_422(tmp_path):
    async with stack(tmp_path) as (c, _):
        r = await c.post("/agents/echo/invoke", content=b"not json", headers=ALICE)
        assert r.status_code == 422


async def test_unknown_task_is_404(tmp_path):
    async with stack(tmp_path) as (c, _):
        assert (await c.get("/agents/echo/tasks/nope", headers=ALICE)).status_code == 404


async def test_facade_can_be_disabled(tmp_path):
    async with stack(tmp_path, facade="false") as (c, _):
        assert (await invoke(c, "echo", {"message": "x"})).status_code == 404
        assert (await c.get("/agents/echo/.well-known/agent-card.json")).status_code == 200


async def test_openapi_describes_the_agent_for_tool_registration_and_is_public(tmp_path):
    async with stack(tmp_path) as (c, _):
        r = await c.get("/agents/echo/openapi.json")  # no credentials
        doc = r.json()
        assert r.status_code == 200 and doc["openapi"].startswith("3.")
        assert doc["servers"] == [{"url": "https://proxy.example"}]
        invoke_op = doc["paths"]["/agents/echo/invoke"]["post"]
        assert invoke_op["operationId"] == "invoke_echo" and "Skill for echo" in invoke_op["description"]
        assert set(doc["paths"]) == {"/agents/echo/invoke", "/agents/echo/tasks/{task_id}", "/agents/echo/tasks/{task_id}/cancel"}
        assert doc["components"]["securitySchemes"]["apiKey"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
        assert doc["components"]["schemas"]["InvokeRequest"]["properties"]["message"]


# ---------------------------------------------------------------- Boomi agent -> Boomi agent


async def test_boomi_to_boomi_conversation_with_the_callers_own_boomi_token(tmp_path):
    async with stack(tmp_path) as (c, _):
        hdr = {**ALICE, **BOOMI}
        first = (await invoke(c, "agent-b", {"message": "hello"}, hdr)).json()
        assert first["state"] == "completed" and first["text"] == "Turn 1: you said hello"
        second = (await invoke(c, "agent-b", {"message": "again", "context_id": first["context_id"]}, hdr)).json()
        assert second["text"] == "Turn 2: you said again"  # the session continued across facade calls


async def test_boomi_to_boomi_without_or_with_wrong_boomi_credentials_needs_auth(tmp_path):
    async with stack(tmp_path) as (c, _):
        none = (await invoke(c, "agent-b", {"message": "hi"})).json()
        bad = (await invoke(c, "agent-b", {"message": "hi"}, {**ALICE, "X-Boomi-Authorization": "Basic " + base64.b64encode(b"BOOMI_TOKEN.mock-user:wrong").decode()})).json()
    assert none["state"] == "auth_required" and "X-Boomi-Authorization" in none["message"]
    assert bad["state"] == "auth_required" and "wrong" not in bad["message"]


# ---------------------------------------------------------------- remote A2A agents


def remote_stack_app(tmp_path, inbound, capture):
    text = f"""
server:
  database_url: "sqlite+aiosqlite:///{tmp_path}/remote.db"
  public_base_url: "https://remote.test"
  inbound_auth: {inbound}
agents:
  - slug: partner
    name: Partner Agent
    description: A partner's fulfilment agent
    adapter: {{type: http_sync, url: "https://down.test/p", auth: {{mode: bearer_passthrough}}}}
    skills: [{{id: ship, name: Ship it, description: ships things}}]
"""

    def handler(request):
        capture.append(dict(request.headers))
        return httpx.Response(200, json={"shipped": True})

    factory = lambda agent, _s: HttpSyncAdapter(agent.adapter, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return create_app(parse_config(text, env={}), factory)


@asynccontextmanager
async def remote_stack(tmp_path, inbound, remote_auth, capture, token_transport=None):
    remote_app = remote_stack_app(tmp_path, inbound, capture)
    remotes = f"""
  - slug: partner
    card_url: "https://remote.test/agents/partner/.well-known/agent-card.json"
    auth: {remote_auth}
"""

    def remote_factory(cfg):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=remote_app))
        target = RemoteTarget(cfg, http)
        if token_transport is not None and target._token is not None:
            target._token._http = httpx.AsyncClient(transport=token_transport)
        return target

    async with remote_app.router.lifespan_context(remote_app):
        async with stack(tmp_path, remotes=remotes, remote_factory=remote_factory) as (client, _):
            yield client


KEYS = '{clients: [{id: proxy, key_sha256: "%s"}], rate_limit: null}' % h("remote-key")
BEARER_IN = "{mode: bearer_passthrough, rate_limit: null}"


async def test_remote_agent_through_the_facade_with_a_static_key(tmp_path):
    seen = []
    # The remote proxy requires X-API-Key, which we send. Its own backend needs a bearer token we do not
    # have, so it answers auth_required: proof the call got through the remote's auth and ran its executor.
    async with remote_stack(tmp_path, KEYS, '{mode: static_header, header_name: X-API-Key, value: "${R_KEY}"}', seen) as c:
        r = await invoke(c, "partner", {"message": "ship it", "wait_seconds": 5})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "auth_required" and r.json()["task_id"]
        assert seen == []  # the backend was never called without a token


async def test_remote_bearer_passthrough_forwards_the_callers_token(tmp_path):
    seen = []
    async with remote_stack(tmp_path, BEARER_IN, "{mode: bearer_passthrough}", seen) as c:
        r = await invoke(c, "partner", {"message": "ship it"}, {**ALICE, "Authorization": "Bearer caller-jwt"})
        assert r.json()["state"] == "completed" and r.json()["data"] == {"shipped": True}
        assert seen[-1]["authorization"] == "Bearer caller-jwt"
        # Without a token there is nothing to forward.
        assert (await invoke(c, "partner", {"message": "x"})).status_code == 401


async def test_remote_oauth_client_credentials_token_is_fetched_once_and_reused(tmp_path):
    seen, token_calls = [], []

    def token_endpoint(request):
        token_calls.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    auth = '{mode: oauth_client_credentials, token_url: "https://idp.test/token", client_id: svc, client_secret: "${R_SECRET}", scope: "a2a"}'
    async with remote_stack(tmp_path, BEARER_IN, auth, seen, httpx.MockTransport(token_endpoint)) as c:
        for _ in range(2):
            r = await invoke(c, "partner", {"message": "ship it"})
            assert r.json()["state"] == "completed", r.text
        assert seen[-1]["authorization"] == "Bearer tok-1"
    assert len(token_calls) == 1 and "grant_type=client_credentials" in token_calls[0] and "scope=a2a" in token_calls[0]


async def test_remote_agent_openapi_reflects_the_remote_card(tmp_path):
    async with remote_stack(tmp_path, BEARER_IN, "{mode: bearer_passthrough}", []) as c:
        doc = (await c.get("/agents/partner/openapi.json")).json()
        assert doc["info"]["title"] == "Partner Agent" and "ships things" in doc["paths"]["/agents/partner/invoke"]["post"]["description"]


async def test_remote_unreachable_is_a_502_without_leaking_internals(tmp_path):
    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("no route to host 10.9.9.9")

    remotes = '\n  - {slug: partner, card_url: "https://remote.test/c.json", auth: {mode: none}}'
    factory = lambda cfg: RemoteTarget(cfg, httpx.AsyncClient(transport=Down()))
    async with stack(tmp_path, remotes=remotes, remote_factory=factory) as (c, _):
        r = await invoke(c, "partner", {"message": "x"})
        assert r.status_code == 502 and "10.9.9.9" not in r.text


async def test_remote_card_404_is_a_502(tmp_path):
    remotes = '\n  - {slug: partner, card_url: "https://remote.test/missing.json", auth: {mode: none}}'
    factory = lambda cfg: RemoteTarget(cfg, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    async with stack(tmp_path, remotes=remotes, remote_factory=factory) as (c, _):
        r = await invoke(c, "partner", {"message": "x"})
        assert r.status_code == 502 and "agent card" in r.json()["error"]


async def test_remote_rejecting_our_credentials_is_a_502_naming_the_cause(tmp_path):
    async with remote_stack(tmp_path, KEYS, '{mode: static_header, header_name: X-API-Key, value: "wrong"}'.replace('"wrong"', '"${R_KEY}x"'), []) as c:
        r = await invoke(c, "partner", {"message": "x"})
        assert r.status_code == 502 and "rejected the credentials" in r.json()["error"]


# ---------------------------------------------------------------- unit: token provider and config


async def test_client_credentials_token_refreshes_after_expiry_and_hides_failures():
    now, calls = [0.0], []

    def endpoint(request):
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 100})

    auth = RemoteAuthConfig(mode="oauth_client_credentials", token_url="https://idp.test/t", client_id="i", client_secret="s")
    provider = ClientCredentialsToken(auth, httpx.AsyncClient(transport=httpx.MockTransport(endpoint)), clock=lambda: now[0])
    assert await provider.get() == "t1" and await provider.get() == "t1"
    now[0] = 80  # inside the 30s expiry margin of a 100s token
    assert await provider.get() == "t2"

    bad = ClientCredentialsToken(auth, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="client_secret=s is wrong"))))
    with pytest.raises(Exception) as err:
        await bad.get()
    assert "client_secret" not in str(err.value)


REMOTE_BASE = """
agents:
  - {slug: local, name: L, adapter: {type: http_sync, url: "https://x.test"}, skills: [{id: s, name: S}]}
remote_agents:
%s
"""


@pytest.mark.parametrize(
    "remote, match",
    [
        ('  - {slug: local, card_url: "https://r.test/c.json"}', "duplicate agent slugs"),
        ('  - {slug: r, card_url: "http://r.test/c.json"}', "https://"),
        ('  - {slug: r, card_url: "https://r.test/c.json", auth: {mode: static_header, header_name: X, value: hunter2}}', "environment variable"),
        ('  - {slug: r, card_url: "https://r.test/c.json", auth: {mode: oauth_client_credentials, token_url: "https://i.test/t", client_id: a, client_secret: hunter2}}', "environment variable"),
        ('  - {slug: r, card_url: "https://r.test/c.json", auth: {mode: oauth_client_credentials, client_id: a}}', "requires"),
    ],
)
def test_remote_agent_config_validation(remote, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(REMOTE_BASE % remote, env={})


def test_insecure_http_allowed_only_when_explicit():
    cfg = parse_config(REMOTE_BASE % '  - {slug: r, card_url: "http://localhost:9/c.json", allow_insecure_http: true}', env={})
    assert cfg.remote_agents[0].slug == "r"
