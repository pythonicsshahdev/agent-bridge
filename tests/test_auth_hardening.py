import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import PyJWKClientConnectionError
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import ClientConfig, ConfigError, InboundAuthConfig, JwtConfig, parse_config
from a2a_bridge.main import create_app
from a2a_bridge.security import (
    ApiKeyAuthenticator,
    InboundAuthMiddleware,
    JwtAuthenticator,
    TokenBucket,
    generate_key,
    hash_key,
    validate_inbound,
)

ISS, AUD = "https://idp.test/", "a2a-bridge"


def h(key):
    return hashlib.sha256(key.encode()).hexdigest()


def client_cfg(id_, key, scopes=("agents:*",), expires=None, extra_keys=()):
    return ClientConfig(id=id_, key_sha256=[h(key), *map(h, extra_keys)], scopes=list(scopes), expires_at=expires)


def starlette_client(auth, root_slug=None, **mw):
    async def who(request):
        return JSONResponse({"user": request.user.display_name, "bytes": len(await request.body())})

    async def public(request):
        return JSONResponse({"public": True})

    app = Starlette(
        routes=[
            Route("/agents/a/rpc", who, methods=["POST"]),
            Route("/agents/b/rpc", who, methods=["POST"]),
            Route("/rpc", who, methods=["POST"]),
            Route("/agents/a/.well-known/agent-card.json", public),
            Route("/healthz", public),
        ]
    )
    app.add_middleware(InboundAuthMiddleware, auth=auth, root_slug=root_slug, **mw)
    return TestClient(app)


# ---------------------------------------------------------------- API keys


def test_hashed_client_key_authenticates_and_runs_as_that_client():
    c = starlette_client(InboundAuthConfig(clients=[client_cfg("crm", "k1")]))
    r = c.post("/agents/a/rpc", headers={"X-API-Key": "k1"})
    assert r.status_code == 200 and r.json()["user"] == "key:crm"
    assert r.headers["x-request-id"]


def test_failure_responses_do_not_reveal_why():
    c = starlette_client(InboundAuthConfig(clients=[client_cfg("crm", "k1")]))
    for headers in ({}, {"X-API-Key": "nope"}, {"X-API-Key": ""}):
        r = c.post("/agents/a/rpc", headers=headers)
        assert r.status_code == 401 and r.json() == {"error": "authentication required"}


def test_key_rotation_accepts_old_and_new_hash():
    c = starlette_client(InboundAuthConfig(clients=[client_cfg("crm", "old", extra_keys=["new"])]))
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "old"}).status_code == 200
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "new"}).status_code == 200


def test_expired_client_key_is_rejected():
    now = datetime(2027, 1, 1, tzinfo=UTC)
    cfg = InboundAuthConfig(clients=[client_cfg("crm", "k", expires=now - timedelta(seconds=1))])
    c = starlette_client(cfg, key_authenticator=ApiKeyAuthenticator(cfg, now=lambda: now))
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "k"}).status_code == 401
    cfg = InboundAuthConfig(clients=[client_cfg("crm", "k", expires=now + timedelta(days=1))])
    c = starlette_client(cfg, key_authenticator=ApiKeyAuthenticator(cfg, now=lambda: now))
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "k"}).status_code == 200


def test_scopes_limit_which_agents_a_client_may_call():
    cfg = InboundAuthConfig(clients=[client_cfg("only-a", "ka", ["agents:a"]), client_cfg("all", "kall")])
    c = starlette_client(cfg, root_slug="a")
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "ka"}).status_code == 200
    assert c.post("/agents/b/rpc", headers={"X-API-Key": "ka"}).status_code == 403
    assert c.post("/agents/b/rpc", headers={"X-API-Key": "kall"}).status_code == 200
    assert c.post("/rpc", headers={"X-API-Key": "ka"}).status_code == 200  # root path maps to the root agent


def test_legacy_api_keys_list_still_works_with_full_access():
    c = starlette_client(InboundAuthConfig(api_keys=["legacy"]))
    assert c.post("/agents/b/rpc", headers={"X-API-Key": "legacy"}).json()["user"] == "key:key-1"


def test_card_and_health_stay_public():
    c = starlette_client(InboundAuthConfig(clients=[client_cfg("crm", "k")]))
    assert c.get("/agents/a/.well-known/agent-card.json").status_code == 200
    assert c.get("/healthz").status_code == 200


def test_generated_keys_hash_consistently_and_are_unique():
    (k1, d1), (k2, d2) = generate_key(), generate_key()
    assert d1 == hash_key(k1) and k1 != k2 and k1.startswith("a2a_") and len(k1) > 40


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key_sha256": ["not-a-hash"]},
        {"key_sha256": [h("x")], "scopes": ["admin"]},
        {"key_sha256": [h("x")], "scopes": ["agents:Bad Slug"]},
        {"key_sha256": []},
    ],
)
def test_invalid_client_config_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        ClientConfig(id="c", **kwargs)


def test_duplicate_client_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate client ids"):
        InboundAuthConfig(clients=[client_cfg("c", "a"), client_cfg("c", "b")])


# ---------------------------------------------------------------- rate and size limits


def test_token_bucket_refills_over_time():
    now = [0.0]
    bucket = TokenBucket(per_minute=60, burst=2, clock=lambda: now[0])
    assert bucket.take("a") == 0 and bucket.take("a") == 0
    assert bucket.take("a") > 0
    assert bucket.take("b") == 0  # other principals are unaffected
    now[0] += 1.0
    assert bucket.take("a") == 0


def test_rate_limit_returns_429_with_retry_after():
    cfg = InboundAuthConfig(
        clients=[client_cfg("a", "ka"), client_cfg("b", "kb")], rate_limit={"requests_per_minute": 1, "burst": 2}
    )
    c = starlette_client(cfg)
    codes = [c.post("/agents/a/rpc", headers={"X-API-Key": "ka"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = c.post("/agents/a/rpc", headers={"X-API-Key": "ka"})
    assert int(limited.headers["retry-after"]) >= 1
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "kb"}).status_code == 200


def test_oversized_bodies_rejected_by_header_and_when_chunked():
    cfg = InboundAuthConfig(clients=[client_cfg("a", "k")], max_request_bytes=2048)
    c = starlette_client(cfg)
    ok = c.post("/agents/a/rpc", headers={"X-API-Key": "k"}, content=b"x" * 2048)
    assert ok.status_code == 200
    assert c.post("/agents/a/rpc", headers={"X-API-Key": "k"}, content=b"x" * 5000).status_code == 413

    def chunks():
        for _ in range(6):
            yield b"x" * 1024

    assert c.post("/agents/a/rpc", headers={"X-API-Key": "k"}, content=chunks()).status_code == 413


# ---------------------------------------------------------------- JWT


@pytest.fixture(scope="module")
def rsa_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"private": pem(private), "other": pem(other), "public": private.public_key(), "public_pem": pub_pem}


def token(keys, which="private", alg="RS256", **claims):
    payload = {"iss": ISS, "aud": AUD, "sub": "alice", "exp": int(time.time()) + 300, "scope": "agents:a", **claims}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, keys[which], algorithm=alg)


def jwt_client(keys, mode="jwt", resolver=None, **jwt_kwargs):
    jcfg = JwtConfig(issuer=ISS, audience=AUD, jwks_url="https://idp.test/jwks", **jwt_kwargs)
    cfg = InboundAuthConfig(mode=mode, jwt=jcfg, clients=[client_cfg("crm", "k")] if mode == "api_key_or_jwt" else [])
    auth = JwtAuthenticator(jcfg, key_resolver=resolver or (lambda t: keys["public"]))
    return starlette_client(cfg, jwt_authenticator=auth)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_valid_jwt_authenticates_with_scopes_and_identity(rsa_keys):
    c = jwt_client(rsa_keys)
    r = c.post("/agents/a/rpc", headers=bearer(token(rsa_keys)))
    assert r.status_code == 200 and r.json()["user"] == "jwt:alice"
    assert c.post("/agents/b/rpc", headers=bearer(token(rsa_keys))).status_code == 403  # no agents:b scope


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 3600},
        {"aud": "someone-else"},
        {"iss": "https://evil.test/"},
        {"exp": None},
        {"sub": None},
        {"nbf": int(time.time()) + 3600},
    ],
)
def test_bad_claims_are_rejected(rsa_keys, claims):
    assert jwt_client(rsa_keys).post("/agents/a/rpc", headers=bearer(token(rsa_keys, **claims))).status_code == 401


def test_token_signed_by_a_different_key_is_rejected(rsa_keys):
    assert jwt_client(rsa_keys).post("/agents/a/rpc", headers=bearer(token(rsa_keys, which="other"))).status_code == 401


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_algorithm_confusion_and_none_attacks_are_rejected(rsa_keys):
    claims = {"iss": ISS, "aud": AUD, "sub": "mallory", "exp": int(time.time()) + 300, "scope": "agents:*"}
    body = _b64(json.dumps(claims).encode())
    # HS256 forged using the server's PUBLIC key as the HMAC secret.
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    sig = hmac.new(rsa_keys["public_pem"], f"{head}.{body}".encode(), hashlib.sha256).digest()
    forged = f"{head}.{body}.{_b64(sig)}"
    # Unsigned token.
    unsigned = f"{_b64(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{body}."
    c = jwt_client(rsa_keys)
    for t in (forged, unsigned):
        assert c.post("/agents/a/rpc", headers=bearer(t)).status_code == 401


def test_scope_claim_formats(rsa_keys):
    c = jwt_client(rsa_keys)
    assert c.post("/agents/b/rpc", headers=bearer(token(rsa_keys, scope="openid agents:b"))).status_code == 200
    assert c.post("/agents/b/rpc", headers=bearer(token(rsa_keys, scope=["agents:b"]))).status_code == 200
    assert c.post("/agents/b/rpc", headers=bearer(token(rsa_keys, scope=None, scp=["agents:b"]))).status_code == 200
    custom = jwt_client(rsa_keys, scope_claim="permissions")
    assert custom.post("/agents/b/rpc", headers=bearer(token(rsa_keys, scope=None, permissions=["agents:b"]))).status_code == 200


def test_identity_provider_outage_is_503_not_a_silent_deny(rsa_keys):
    def down(_):
        raise PyJWKClientConnectionError("idp unreachable")

    r = jwt_client(rsa_keys, resolver=down).post("/agents/a/rpc", headers=bearer(token(rsa_keys)))
    assert r.status_code == 503 and r.headers["retry-after"]


def test_jwt_mode_ignores_api_keys_and_hybrid_accepts_both(rsa_keys):
    assert jwt_client(rsa_keys).post("/agents/a/rpc", headers={"X-API-Key": "k"}).status_code == 401
    hybrid = jwt_client(rsa_keys, mode="api_key_or_jwt")
    assert hybrid.post("/agents/a/rpc", headers={"X-API-Key": "k"}).json()["user"] == "key:crm"
    assert hybrid.post("/agents/a/rpc", headers=bearer(token(rsa_keys))).json()["user"] == "jwt:alice"
    assert hybrid.post("/agents/a/rpc").status_code == 401


def test_symmetric_algorithms_cannot_be_configured():
    with pytest.raises(ValidationError):
        JwtConfig(issuer=ISS, audience=AUD, jwks_url="https://x.test/j", algorithms=["HS256"])


# ---------------------------------------------------------------- startup validation and audit


def test_startup_validation():
    with pytest.raises(ConfigError, match="no clients"):
        validate_inbound(InboundAuthConfig())
    with pytest.raises(ConfigError, match="jwt is not configured"):
        validate_inbound(InboundAuthConfig(mode="jwt"))
    with pytest.raises(ConfigError, match="no clients"):
        validate_inbound(InboundAuthConfig(mode="api_key_or_jwt", jwt=JwtConfig(issuer=ISS, audience=AUD, jwks_url="https://x.test/j")))


def test_audit_log_records_who_and_what_but_never_credentials(caplog, rsa_keys):
    c = jwt_client(rsa_keys, mode="api_key_or_jwt")
    secret_token = token(rsa_keys)
    with caplog.at_level(logging.INFO, logger="a2a_bridge.audit"):
        c.post("/agents/a/rpc", headers={"X-API-Key": "k"})
        c.post("/agents/a/rpc", headers=bearer(secret_token))
        c.post("/agents/a/rpc", headers={"X-API-Key": "wrong-key-value"})
        c.post("/agents/b/rpc", headers=bearer(secret_token))
    lines = [json.loads(r.message) for r in caplog.records if r.name == "a2a_bridge.audit"]
    assert [(l["principal"], l["status"]) for l in lines] == [("key:crm", 200), ("jwt:alice", 200), (None, 401), ("jwt:alice", 403)]
    text = " ".join(r.message for r in caplog.records)
    assert "wrong-key-value" not in text and secret_token not in text and '"k"' not in text


# ---------------------------------------------------------------- through the real app


APP_YAML = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  inbound_auth:
    clients:
      - {{id: alice, key_sha256: "{alice}", scopes: ["agents:orders"]}}
      - {{id: bob,   key_sha256: "{bob}",   scopes: ["agents:orders", "agents:other"]}}
agents:
  - slug: orders
    name: Orders
    adapter: {{type: http_sync, url: "https://down.test/orders"}}
    skills: [{{id: go, name: Go}}]
  - slug: other
    name: Other
    adapter: {{type: http_sync, url: "https://down.test/other"}}
    skills: [{{id: go, name: Go}}]
"""


@pytest.fixture
def app_client(tmp_path):
    cfg = parse_config(APP_YAML.format(db=tmp_path / "t.db", alice=h("alice-key"), bob=h("bob-key")), env={})
    factory = lambda agent, _s: HttpSyncAdapter(
        agent.adapter, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1})))
    )
    with TestClient(create_app(cfg, factory)) as c:
        yield c


def rpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def hdr(key):
    return {"X-API-Key": key, "A2A-Version": "1.0"}


def test_one_client_cannot_read_another_clients_task(app_client):
    send = rpc("SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}})
    task = app_client.post("/agents/orders/rpc", json=send, headers=hdr("alice-key")).json()["result"]["task"]
    mine = app_client.post("/agents/orders/rpc", json=rpc("GetTask", {"id": task["id"]}), headers=hdr("alice-key")).json()
    assert mine["result"]["id"] == task["id"]
    theirs = app_client.post("/agents/orders/rpc", json=rpc("GetTask", {"id": task["id"]}), headers=hdr("bob-key")).json()
    assert "result" not in theirs and "error" in theirs


def test_scopes_apply_per_agent_through_the_app(app_client):
    send = rpc("SendMessage", {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}})
    assert app_client.post("/agents/other/rpc", json=send, headers=hdr("alice-key")).status_code == 403
    assert app_client.post("/agents/other/rpc", json=send, headers=hdr("bob-key")).status_code == 200


def test_cards_advertise_how_to_authenticate(app_client):
    card = app_client.get("/agents/orders/.well-known/agent-card.json").json()
    assert card["securitySchemes"]["apiKey"]["apiKeySecurityScheme"]["name"] == "X-API-Key"
    assert card["securityRequirements"] == [{"schemes": {"apiKey": {}}}]
