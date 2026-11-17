import hashlib
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import ClientConfig, ConfigError, parse_config
from a2a_bridge.main import create_app


def h(key):
    return hashlib.sha256(key.encode()).hexdigest()


ADMIN, AGENT = {"X-API-Key": "admin-key"}, {"X-API-Key": "agent-key", "A2A-Version": "1.0"}

CONFIG = """
server:
  database_url: "sqlite+aiosqlite:///{db}"
  public_base_url: "https://a2a.example.com"
  admin: {{enabled: {admin}}}
  inbound_auth:
    clients:
      - {{id: ops, key_sha256: "{admin_hash}", scopes: ["admin:*"]}}
      - {{id: crm, key_sha256: "{agent_hash}", scopes: ["agents:*"]}}
    rate_limit: null
agents:
  - slug: orders
    name: Orders
    description: Order agent
    adapter:
      type: http_sync
      url: "https://user:pw@backend.example.com/hooks/SENTINEL-PATH-CCC?token=SENTINEL-QUERY-EEE"
      headers: {{X-Token: "${{HDR}}"}}
      auth: {{mode: static_header, header_name: X-Key, value: "${{TOK}}"}}
    skills: [{{id: create, name: Create order, description: makes one}}]
  - slug: studio
    name: Studio
    adapter: {{type: agentstudio, identity: service_account, deployment_id: dep, execute_url: "https://c01.example.com", account_id: acct, username: boomi-user, api_token: "${{TOK}}"}}
    skills: [{{id: ask, name: Ask}}]
remote_agents:
  - slug: partner
    card_url: "https://partner.example.com/agents/x/.well-known/agent-card.json"
    auth: {{mode: static_header, header_name: X-Key, value: "${{REMOTE}}"}}
"""
ENV = {"TOK": "SENTINEL-TOKEN-AAA", "HDR": "SENTINEL-HEADER-BBB", "REMOTE": "SENTINEL-REMOTE-DDD"}


def build(tmp_path, admin="true"):
    cfg = parse_config(CONFIG.format(db=tmp_path / "t.db", admin=admin, admin_hash=h("admin-key"), agent_hash=h("agent-key")), env=ENV)
    factory = lambda agent, _s: HttpSyncAdapter(agent.adapter, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1}))))
    return cfg, create_app(cfg, factory)


@pytest.fixture
def client(tmp_path):
    _, app = build(tmp_path)
    with TestClient(app) as c:
        yield c


def all_admin_json(c):
    c.post("/agents/orders/invoke", json={"message": "hi"}, headers=AGENT)  # make some traffic
    parts = [c.get(f"/admin/api/{p}", headers=ADMIN).text for p in ("overview", "diagram", "events", "stats")]
    parts.append(c.post("/admin/api/validate", json={"yaml": "agents: ["}, headers=ADMIN).text)
    return " ".join(parts)


# ---------------------------------------------------------------- access


def test_admin_is_off_by_default(tmp_path):
    _, app = build(tmp_path, admin="false")
    with TestClient(app) as c:
        assert c.get("/admin").status_code == 404
        assert c.get("/admin/api/overview", headers=ADMIN).status_code == 404


def test_admin_needs_an_authentication_mode_that_grants_scopes():
    for mode in ("none", "bearer_passthrough"):
        text = f"server: {{admin: {{enabled: true}}, inbound_auth: {{mode: {mode}}}}}\nagents:\n  - {{slug: a, name: A, adapter: {{type: http_sync, url: 'https://x.test'}}, skills: [{{id: s, name: S}}]}}"
        with pytest.raises(ConfigError, match="admin"):
            create_app(parse_config(text, env={}))


def test_the_ui_shell_is_public_but_the_api_is_not(client):
    for path in ("/admin", "/admin/", "/admin/app.js", "/admin/app.css"):
        assert client.get(path).status_code == 200, path
    for path in ("overview", "diagram", "events", "stats"):
        assert client.get(f"/admin/api/{path}").status_code == 401
    assert client.post("/admin/api/validate", json={"yaml": "x"}).status_code == 401


def test_agent_scopes_never_imply_admin(client):
    for path in ("overview", "diagram", "events", "stats"):
        assert client.get(f"/admin/api/{path}", headers=AGENT).status_code == 403
    assert client.post("/admin/api/validate", json={"yaml": "x"}, headers=AGENT).status_code == 403
    assert client.get("/admin/api/overview", headers=ADMIN).status_code == 200


def test_an_admin_client_does_not_get_agent_access_either(client):
    assert client.post("/agents/orders/invoke", json={"message": "x"}, headers=ADMIN).status_code == 403


def test_the_ui_is_served_with_a_strict_content_security_policy(client):
    for path in ("/admin", "/admin/app.js", "/admin/app.css"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "text/html" in client.get("/admin").headers["content-type"]


def test_scope_validation_for_admin():
    assert ClientConfig(id="c", key_sha256=[h("x")], scopes=["admin:*"]).scopes == ["admin:*"]
    with pytest.raises(ValueError):
        ClientConfig(id="c", key_sha256=[h("x")], scopes=["admin:other"])


# ---------------------------------------------------------------- content


def test_nothing_sensitive_ever_appears_in_any_admin_response(client):
    blob = all_admin_json(client)
    for needle in ("SENTINEL", "pw@", "token=", h("admin-key"), h("agent-key"), "admin-key", "agent-key", "boomi-user", "/hooks/"):
        assert needle not in blob, f"leaked {needle!r}"
    assert "backend.example.com" in blob  # hostnames are fine and useful


def test_overview_describes_the_running_configuration(client):
    data = client.get("/admin/api/overview", headers=ADMIN).json()
    assert data["public_base_url"] == "https://a2a.example.com" and data["inbound"]["mode"] == "api_key"
    assert {c["id"]: c["scopes"] for c in data["inbound"]["clients"]} == {"ops": ["admin:*"], "crm": ["agents:*"]}
    assert all("key_sha256" not in c and c["keys"] == 1 for c in data["inbound"]["clients"])
    orders = next(a for a in data["agents"] if a["slug"] == "orders")
    assert orders["live"] is True and orders["adapter"]["target"] == "backend.example.com" and orders["adapter"]["auth"] == "static_header"
    assert orders["card_url"] == "https://a2a.example.com/agents/orders/.well-known/agent-card.json"
    studio = next(a for a in data["agents"] if a["slug"] == "studio")
    assert studio["adapter"]["identity"] == "service_account" and studio["adapter"]["target"] == "c01.example.com"
    assert data["remote_agents"] == [{"slug": "partner", "target": "partner.example.com", "auth": "static_header"}]


def test_findings_point_out_risky_settings(client):
    findings = client.get("/admin/api/overview", headers=ADMIN).json()["findings"]
    text = " | ".join(f["level"] + ": " + f["message"] for f in findings)
    assert "warn: Client 'crm' can call every agent" in text
    assert "warn: Rate limiting is disabled" in text
    assert "info: Client 'ops' has no expiry" in text and "shared Boomi service account" in text


def test_diagram_links_callers_proxy_agents_and_backends(client):
    graph = client.get("/admin/api/diagram", headers=ADMIN).json()
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds["proxy"] == "proxy" and kinds["caller:crm"] == "caller" and kinds["agent:orders"] == "agent"
    assert kinds["backend:studio"] == "backend" and kinds["remote:partner"] == "remote"
    pairs = {(e["from"], e["to"]) for e in graph["edges"]}
    assert {("caller:crm", "proxy"), ("proxy", "agent:orders"), ("agent:orders", "backend:orders"), ("proxy", "remote:partner")} <= pairs
    assert next(e for e in graph["edges"] if e["to"] == "remote:partner")["style"] == "dashed"


def test_events_show_recent_requests_newest_first_without_credentials(client):
    client.post("/agents/orders/invoke", json={"message": "one"}, headers=AGENT)
    client.post("/agents/orders/invoke", json={"message": "two"})  # unauthenticated
    events = client.get("/admin/api/events", headers=ADMIN).json()["events"]
    agent_events = [e for e in events if e["agent"] == "orders"]
    assert [(e["principal"], e["status"]) for e in agent_events][:2] == [(None, 401), ("key:crm", 200)]
    assert all("authorization" not in json.dumps(e).lower() and "agent-key" not in json.dumps(e) for e in events)
    assert client.get("/admin/api/events", params={"limit": "1"}, headers=ADMIN).json()["events"][0]["agent"] in ("orders", "admin")


def test_stats_summarize_the_window(client):
    for _ in range(3):
        client.post("/agents/orders/invoke", json={"message": "x"}, headers=AGENT)
    client.post("/agents/orders/invoke", json={"message": "x"})
    stats = client.get("/admin/api/stats", headers=ADMIN).json()
    assert stats["agents"]["orders"]["requests"] == 4 and stats["agents"]["orders"]["errors"] == 1
    assert stats["statuses"]["401"] >= 1 and stats["callers"]["key:crm"] == 3


def test_admin_requests_are_audited_too(client):
    client.get("/admin/api/overview", headers=ADMIN)
    client.get("/admin/api/overview", headers=AGENT)
    events = client.get("/admin/api/events", headers=ADMIN).json()["events"]
    admin = [e for e in events if e["agent"] == "admin"]
    assert ("key:ops", 200) in [(e["principal"], e["status"]) for e in admin] and ("key:crm", 403) in [(e["principal"], e["status"]) for e in admin]


def test_event_collection_stops_when_the_app_shuts_down(tmp_path):
    from a2a_bridge.admin import EventBuffer

    _, app = build(tmp_path)
    with TestClient(app):
        assert any(isinstance(x, EventBuffer) for x in logging.getLogger("a2a_bridge.audit").handlers)
    assert not any(isinstance(x, EventBuffer) for x in logging.getLogger("a2a_bridge.audit").handlers)


# ---------------------------------------------------------------- validate

GOOD = """
server: {inbound_auth: {clients: [{id: c, key_sha256: "%s", scopes: ["agents:newagent"]}]}}
agents:
  - {slug: orders, name: Orders, adapter: {type: http_async, url: "https://x.test/s", status_url: "https://x.test/s/{job_id}"}, skills: [{id: s, name: S}]}
  - {slug: newagent, name: New, adapter: {type: http_sync, url: "https://x.test/n"}, skills: [{id: s, name: S}]}
""" % h("k")


def test_validate_accepts_a_good_config_and_reports_what_would_change(client):
    r = client.post("/admin/api/validate", json={"yaml": GOOD}, headers=ADMIN).json()
    assert r["ok"] is True and r["summary"]["inbound_mode"] == "api_key"
    assert r["changes_vs_running"] == {"added": ["newagent"], "removed": ["partner", "studio"], "adapter_changed": ["orders"]}
    assert isinstance(r["findings"], list)


def test_validate_reports_each_problem_without_echoing_input(client):
    pasted_secret = "my-real-raw-key-1234567890"
    bad = f'''
server: {{inbound_auth: {{clients: [{{id: c, key_sha256: "{pasted_secret}"}}]}}}}
agents:
  - {{slug: Bad_Slug, name: X, adapter: {{type: http_sync, url: "ftp://x"}}, skills: [{{id: s, name: S}}]}}
'''
    r = client.post("/admin/api/validate", json={"yaml": bad}, headers=ADMIN).json()
    assert r["ok"] is False and len(r["errors"]) >= 3
    text = " ".join(r["errors"])
    assert pasted_secret not in text and "errors.pydantic.dev" not in text
    assert "key_sha256" in text and "slug" in text


def test_validate_refuses_literal_secrets_and_does_not_echo_them(client):
    text = "agents:\n  - {slug: a, name: A, adapter: {type: agentstudio, identity: service_account, deployment_id: d, execute_url: 'https://e.test', account_id: a, username: u, api_token: hunter2hunter2}, skills: [{id: s, name: S}]}"
    r = client.post("/admin/api/validate", json={"yaml": text}, headers=ADMIN).json()
    assert r["ok"] is False and "hunter2" not in json.dumps(r) and "environment variable" in " ".join(r["errors"])


def test_validate_names_unset_environment_variables_but_not_values(client):
    r = client.post("/admin/api/validate", json={"yaml": "agents:\n  - {slug: a, name: A, adapter: {type: http_sync, url: 'https://x.test', headers: {X-Key: '${NOT_SET_ANYWHERE}'}}, skills: [{id: s, name: S}]}"}, headers=ADMIN).json()
    assert r["ok"] is False and "NOT_SET_ANYWHERE" in " ".join(r["errors"])


@pytest.mark.parametrize("body, status", [({"nope": 1}, 422), ({"yaml": 5}, 422), ({"yaml": "x" * 300_000}, 413)])
def test_validate_rejects_malformed_requests(client, body, status):
    assert client.post("/admin/api/validate", json=body, headers=ADMIN).status_code == status


def test_a_literal_credential_in_a_sensitive_adapter_header_is_refused():
    text = "agents:\n  - {slug: a, name: A, adapter: {type: http_sync, url: 'https://x.test', headers: {X-Api-Key: hunter2}}, skills: [{id: s, name: S}]}"
    with pytest.raises(ConfigError, match="looks like a credential"):
        parse_config(text, env={})
    ok = text.replace("hunter2", "'${K}'")
    assert parse_config(ok, env={"K": "v"}).agents[0].adapter.headers == {"X-Api-Key": "v"}
    harmless = text.replace("X-Api-Key: hunter2", "X-Tenant: acme")
    assert parse_config(harmless, env={}).agents[0].adapter.headers == {"X-Tenant": "acme"}


def test_the_consoles_own_requests_are_not_counted_as_traffic(client):
    client.post("/agents/orders/invoke", json={"message": "x"}, headers=AGENT)
    for _ in range(5):
        client.get("/admin/api/overview", headers=ADMIN)
    stats = client.get("/admin/api/stats", headers=ADMIN).json()
    assert "admin" not in stats["agents"] and stats["window"] == 1
    assert "key:ops" not in stats["callers"]


def test_admin_works_when_a_single_agent_is_also_mounted_at_the_root(tmp_path):
    """A lone agent is served at '/', and a root mount matches every path, so it must not shadow /admin."""
    text = CONFIG.format(db=tmp_path / "t.db", admin="true", admin_hash=h("admin-key"), agent_hash=h("agent-key"))
    one = text[: text.index("  - slug: studio")] + "\n"
    cfg = parse_config(one, env=ENV)
    assert cfg.root_agent is not None
    factory = lambda agent, _s: HttpSyncAdapter(agent.adapter, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    with TestClient(create_app(cfg, factory)) as c:
        assert c.get("/admin").status_code == 200
        assert c.get("/admin/app.js").status_code == 200
        assert c.get("/admin/api/overview", headers=ADMIN).status_code == 200
        assert c.get("/.well-known/agent-card.json").status_code == 200  # the root agent still works
        assert c.get("/healthz").status_code == 200


def test_the_flow_map_does_not_draw_admin_only_clients_as_callers(client):
    nodes = {n["id"] for n in client.get("/admin/api/diagram", headers=ADMIN).json()["nodes"]}
    assert "caller:crm" in nodes and "caller:ops" not in nodes


# ---------------------------------------------------------------- register an agent: preview


def preview(client, agent, headers=ADMIN):
    return client.post("/admin/api/agent/preview", json={"agent": agent}, headers=headers)


REST_AGENT = {
    "slug": "invoices",
    "name": "Invoices",
    "description": "Looks up invoices",
    "adapter": {"type": "http_sync", "url": "https://erp.example.com/invoices", "method": "POST", "auth": {"mode": "bearer_passthrough"}},
    "skills": [{"id": "lookup", "name": "Look up an invoice"}],
}
ASYNC_AGENT = {
    "slug": "report",
    "name": "Report",
    "adapter": {
        "type": "http_async", "url": "https://x.example.com/start", "status_url": "https://x.example.com/s/{job_id}",
        "cancel_url": "https://x.example.com/c/{job_id}", "job_id_path": "executionId", "completed_values": ["COMPLETE"],
        "failed_values": ["ERROR"], "auth": {"mode": "static_header", "header_name": "X-Key", "value": "${REPORT_KEY}"},
    },
    "skills": [{"id": "run", "name": "Run"}],
}
STUDIO_AGENT = {
    "slug": "helpdesk",
    "name": "Help desk",
    "adapter": {"type": "agentstudio", "deployment_id": "d-1", "execute_url": "https://c01-usa-east.ai-agent-garden.boomi.com", "account_id": "acct", "mode": "conversational"},
    "skills": [{"id": "ask", "name": "Ask"}],
}


@pytest.mark.parametrize("agent", [REST_AGENT, ASYNC_AGENT, STUDIO_AGENT])
def test_preview_accepts_each_adapter_type_and_renders_yaml_that_loads_back(client, agent):
    r = preview(client, agent).json()
    assert r["ok"] is True and r["slug"] == agent["slug"] and r["slug_taken"] is False
    reloaded = parse_config("agents:\n" + "".join("  " + line + "\n" for line in r["yaml"].splitlines()), env={"REPORT_KEY": "x"})
    assert reloaded.agents[0].slug == agent["slug"] and reloaded.agents[0].adapter.type == agent["adapter"]["type"]


def test_preview_keeps_env_references_intact_and_reports_whether_they_are_set(client, monkeypatch):
    monkeypatch.setenv("REPORT_KEY", "SENTINEL-VALUE-123")
    r = preview(client, ASYNC_AGENT).json()
    assert "${REPORT_KEY}" in r["yaml"] and r["env"] == [{"name": "REPORT_KEY", "set": True}]
    assert "SENTINEL-VALUE-123" not in json.dumps(r), "the value of an environment variable must never be returned"
    monkeypatch.delenv("REPORT_KEY")
    unset = preview(client, ASYNC_AGENT).json()
    assert unset["ok"] is True, "an unset variable here must not fail the check: the agent may run elsewhere"
    assert unset["env"] == [{"name": "REPORT_KEY", "set": False}]


def test_preview_flags_a_slug_that_is_already_in_use(client):
    assert preview(client, {**REST_AGENT, "slug": "orders"}).json()["slug_taken"] is True
    assert preview(client, {**REST_AGENT, "slug": "partner"}).json()["slug_taken"] is True  # a remote agent's slug counts too


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda a: a.update(slug="Bad Slug"), "slug"),
        (lambda a: a["adapter"].update(url="ftp://x"), "http"),
        (lambda a: a.update(skills=[]), "skills"),
        (lambda a: a.update(bogus=1), "bogus"),
        (lambda a: a["adapter"].update(type="telepathy"), "adapter"),
    ],
)
def test_preview_reports_problems_by_field_without_the_agents_0_prefix(client, mutate, expected):
    agent = json.loads(json.dumps(REST_AGENT))
    mutate(agent)
    r = preview(client, agent).json()
    assert r["ok"] is False and any(expected in e for e in r["errors"]) and not any(e.startswith("agents.0.") for e in r["errors"])
    assert "yaml" not in r


def test_preview_refuses_literal_secrets_and_does_not_echo_them(client):
    leaky = json.loads(json.dumps(ASYNC_AGENT))
    leaky["adapter"]["auth"]["value"] = "hunter2hunter2"
    r = preview(client, leaky).json()
    assert r["ok"] is False and "hunter2" not in json.dumps(r) and "environment variable" in " ".join(r["errors"])
    studio = json.loads(json.dumps(STUDIO_AGENT))
    studio["adapter"].update(identity="service_account", username="u", api_token="sup3r-secret-token")
    r = preview(client, studio).json()
    assert r["ok"] is False and "sup3r-secret-token" not in json.dumps(r)


def test_preview_gives_findings_for_this_agent_only(client):
    plain = json.loads(json.dumps(REST_AGENT))
    plain["adapter"]["url"] = "http://erp.example.com/x"
    findings = preview(client, plain).json()["findings"]
    assert any("plain HTTP" in f["message"] for f in findings)
    assert not any("Client" in f["message"] or "Rate limiting" in f["message"] for f in findings), "server-wide findings do not belong here"


def test_preview_is_admin_only_and_validates_its_input(client):
    assert client.post("/admin/api/agent/preview", json={"agent": REST_AGENT}).status_code == 401
    assert preview(client, REST_AGENT, AGENT).status_code == 403
    assert client.post("/admin/api/agent/preview", json={"nope": 1}, headers=ADMIN).status_code == 422
    assert client.post("/admin/api/agent/preview", content=b"not json", headers=ADMIN).status_code == 422
    assert client.post("/admin/api/agent/preview", json={"agent": "text"}, headers=ADMIN).json()["ok"] is False
    big = {"agent": {**REST_AGENT, "description": "x" * 70_000}}
    assert client.post("/admin/api/agent/preview", json=big, headers=ADMIN).status_code == 413


# ---------------------------------------------------------------- bundled fonts


def test_bundled_fonts_are_served_without_signing_in_with_the_right_type_and_policy(client):
    names = ["poppins-latin-400-normal.woff2", "poppins-latin-600-normal.woff2", "jetbrains-mono-latin-400-normal.woff2"]
    for name in names:
        r = client.get(f"/admin/fonts/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "font/woff2", name
        assert r.content[:4] == b"wOF2", "not a real woff2 file"
        assert "font-src 'self'" in r.headers["content-security-policy"]


def test_the_font_route_cannot_be_used_to_read_other_files(client):
    # The HTTP client collapses a literal "../" before sending, so these are the forms that actually reach the server.
    probes = [
        "/admin/fonts/%2e%2e%2fapp.js",
        "/admin/fonts/%2e%2e/app.js",
        "/admin/fonts/..%2fapp.js",
        "/admin/fonts/..%5capp.js",
        "/admin/fonts/%2fetc%2fpasswd",
        "/admin/fonts/LICENSE-poppins.txt",  # a real file in the folder, but not a font
        "/admin/fonts/nope.woff2",
        "/admin/fonts/POPPINS-LATIN-400-NORMAL.WOFF2",
        "/admin/fonts/poppins-latin-400-normal.woff2%00.js",
    ]
    for path in probes:
        r = client.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
        assert b"use strict" not in r.content and b"root:" not in r.content and b"Copyright" not in r.content, path


def test_the_security_policy_still_allows_nothing_external(client):
    csp = client.get("/admin").headers["content-security-policy"]
    assert "font-src 'self'" in csp and "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "http" not in csp.replace("https:", "") and "*" not in csp
