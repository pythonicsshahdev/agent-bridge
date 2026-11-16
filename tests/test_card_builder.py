from a2a_bridge.config import parse_config
from a2a_bridge.core.card_builder import build_agent_card, card_to_dict

YAML = """
agents:
  - slug: orders
    name: Orders
    description: Order agent
    version: "2.1.0"
    adapter: {type: http_sync, url: "https://x.test", auth: {mode: bearer_passthrough}}
    skills:
      - {id: create, name: Create, description: Make one, tags: [erp], examples: ["create an order"]}
  - slug: open
    name: Open
    adapter: {type: http_sync, url: "https://x.test"}
    skills: [{id: s, name: S}]
"""


def cards():
    cfg = parse_config(YAML, env={})
    return cfg, [build_agent_card(a, "https://t.example/") for a in cfg.agents]


def test_card_core_fields():
    _, (orders, _open) = cards()
    d = card_to_dict(orders)
    assert d["name"] == "Orders" and d["version"] == "2.1.0"
    iface = d["supportedInterfaces"][0]
    assert iface["url"] == "https://t.example/agents/orders/rpc"
    assert iface["protocolBinding"] == "JSONRPC"
    assert d["capabilities"]["streaming"] is True
    skill = d["skills"][0]
    assert skill["id"] == "create" and skill["tags"] == ["erp"] and skill["examples"] == ["create an order"]


def test_bearer_passthrough_advertises_security():
    _, (orders, open_) = cards()
    d = card_to_dict(orders)
    assert d["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert d["securityRequirements"][0]["schemes"] == {"bearer": {}}
    assert "securitySchemes" not in card_to_dict(open_)


def test_root_mount_path():
    cfg, _ = cards()
    card = build_agent_card(cfg.agents[1], "https://t.example", mount_path="")
    assert card.supported_interfaces[0].url == "https://t.example/rpc"


def test_sparse_config_still_yields_spec_compliant_card():
    cfg = parse_config(
        "agents:\n  - {slug: s, name: Sparse, adapter: {type: http_sync, url: 'https://x.test'}, skills: [{id: go, name: Go}]}",
        env={},
    )
    d = card_to_dict(build_agent_card(cfg.agents[0], "https://t.example"))
    assert d["description"] == "Sparse"
    assert d["skills"][0]["description"] == "Go" and d["skills"][0]["tags"] == ["go"]


def test_agentstudio_agent_card_builds_and_has_no_bearer_scheme():
    cfg = parse_config(
        """
agents:
  - slug: as
    name: AS
    adapter: {type: agentstudio, deployment_id: d, execute_url: "https://e.test", account_id: a, username: u, api_token: "${T}"}
    skills: [{id: g, name: G}]
""",
        env={"T": "x"},
    )
    d = card_to_dict(build_agent_card(cfg.agents[0], "https://t.example"))
    assert d["name"] == "AS" and "securitySchemes" not in d
