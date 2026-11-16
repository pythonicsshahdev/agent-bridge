import json
import os
import sys
from pathlib import Path

import pytest

from a2a_bridge.config import parse_config
from a2a_bridge.guide import GRID_X, PAYLOAD_MAX, build_flows, flows_js, logo_data_uri, render_guide

HASH = "a" * 64


def config(extra_agents="", remote="", facade="true", clients=True):
    client = f'clients: [{{id: crm, key_sha256: "{HASH}", scopes: ["agents:*"]}}]' if clients else "api_keys: ['${K}']"
    return parse_config(
        f"""
server: {{public_base_url: "https://a2a.example.com", facade: {{enabled: {facade}}}, inbound_auth: {{{client}}}}}
agents:
  - {{slug: orders, name: Orders, description: Makes orders, adapter: {{type: http_sync, url: "https://boomi.example.com/hooks/SECRET-PATH?token=SECRET-QUERY"}}, skills: [{{id: s, name: S}}]}}
{extra_agents}
remote_agents:{remote or " []"}
""",
        env={"K": "x", "T": "x"},
    )


STUDIO = '  - {slug: it, name: IT Support, adapter: {type: agentstudio, deployment_id: d, execute_url: "https://c01.example.com", account_id: a}, skills: [{id: s, name: S}]}'
JOB = '  - {slug: export, name: Nightly Export, adapter: {type: http_async, url: "https://x.test/start", status_url: "https://x.test/s/{job_id}", cancel_url: "https://x.test/c/{job_id}"}, skills: [{id: s, name: S}]}'
REMOTE = '\n  - {slug: partner, card_url: "https://partner.example.com/c.json", auth: {mode: none}}'


def ids(flow):
    return {n["id"] for n in flow.get("nodes", [])}


def by_id(flows):
    return {f["id"]: f for f in flows}


# ---------------------------------------------------------------- structure the engine needs


@pytest.mark.parametrize("cfg", [config(), config(STUDIO + "\n" + JOB, REMOTE), config(JOB, clients=False)])
def test_every_flow_is_internally_consistent(cfg):
    for flow in build_flows(cfg, "Acme"):
        assert flow["id"] and flow["nav"] and flow["type"] in ("flow", "content")
        if flow["type"] == "content":
            assert flow["cards"] and all({"ic", "h", "p"} <= set(c) for c in flow["cards"])
            continue
        nodes = ids(flow)
        assert len(nodes) == len(flow["nodes"]), "duplicate node ids"
        for n in flow["nodes"]:
            assert len(n["pos"]) == 2 and 0 <= n["pos"][0] <= 960 and 0 <= n["pos"][1] <= 560, n["id"]  # a missing pos kills the page
            assert n["kind"] in ("ext", "step", "brain", "guard")
        for wire in flow["wires"]:
            assert wire[0] in nodes and wire[1] in nodes, wire
        assert set(flow["seqs"]) == set(flow["scenarios"]), "pill labels must match seqs keys exactly"
        for label, lit in flow["scenarios"].items():
            assert set(lit) <= nodes, label
            assert flow["seqs"][label] and all(step["node"] in nodes for step in flow["seqs"][label]), label
        assert flow["seq"] == next(iter(flow["seqs"].values()))
        for steps in flow["seqs"].values():
            for step in steps:
                assert len(step.get("payload", "")) <= PAYLOAD_MAX, step


def test_sequences_follow_the_wires_so_no_bubble_starts_from_nowhere():
    for cfg in (config(), config(STUDIO + "\n" + JOB, REMOTE)):
        for flow in build_flows(cfg):
            if flow["type"] != "flow":
                continue
            wired = {(a, b) for a, b, *_ in flow["wires"]}
            for label, steps in flow["seqs"].items():
                lit: list[str] = []
                for step in steps:
                    if lit and lit[-1] != step["node"]:
                        assert (lit[-1], step["node"]) in wired or any((p, step["node"]) in wired for p in lit), f"{flow['id']}/{label}: {lit[-1]} -> {step['node']}"
                    lit.append(step["node"])


def test_sections_appear_only_when_the_config_has_the_thing_to_show():
    assert [f["id"] for f in build_flows(config(facade="false"))] == ["mesh", "security"]
    assert [f["id"] for f in build_flows(config())] == ["mesh", "security"]  # one agent and no remotes: nothing to call
    full = [f["id"] for f in build_flows(config(STUDIO + "\n" + JOB, REMOTE))]
    assert full == ["mesh", "conversation", "jobs", "agents", "security"]
    assert [f["id"] for f in build_flows(config(JOB))] == ["mesh", "jobs", "agents", "security"]
    assert [f["id"] for f in build_flows(config(remote=REMOTE))] == ["mesh", "agents", "security"]


def test_navigation_is_numbered_in_order():
    assert [f["nav"][:2] for f in build_flows(config(STUDIO + "\n" + JOB, REMOTE))] == ["01", "02", "03", "04", "05"]


def test_the_overview_reflects_real_clients_agents_and_scenarios():
    flow = by_id(build_flows(config(STUDIO, REMOTE)))["mesh"]
    assert {"caller:crm", "gate", "core", "adapter:orders", "backend:orders", "adapter:it", "remote:partner"} <= ids(flow)
    assert {"Orders", "IT Support", "Caller refused"} <= set(flow["scenarios"])
    assert ["gate", "core"] in [w[:2] for w in flow["wires"]]
    assert next(w for w in flow["wires"] if w[1] == "remote:partner")[2]["kind"] == "async"


def test_a_long_agent_list_is_capped_with_a_note():
    many = "\n".join(f'  - {{slug: a{i}, name: Agent {i}, adapter: {{type: http_sync, url: "https://x.test"}}, skills: [{{id: s, name: S}}]}}' for i in range(7))
    flow = by_id(build_flows(config(many)))["mesh"]
    assert len([n for n in flow["nodes"] if n["id"].startswith("adapter:")]) == 4 and "first 4 of 8" in flow["note"]


def test_the_conversation_flow_reflects_the_identity_mode():
    caller = by_id(build_flows(config(STUDIO)))["conversation"]
    assert "own Boomi token" in json.dumps(caller)
    shared = by_id(build_flows(config(STUDIO.replace("account_id: a}", "account_id: a, identity: service_account, username: u, api_token: \"${T}\"}"))))["conversation"]
    assert "service account" in json.dumps(shared) and "own Boomi token" not in json.dumps(shared)


# ---------------------------------------------------------------- safety


def test_only_host_names_appear_never_url_paths_or_queries():
    blob = json.dumps(build_flows(config(STUDIO + "\n" + JOB, REMOTE)))
    for secret in ("SECRET-PATH", "SECRET-QUERY", "token=", "/hooks/"):
        assert secret not in blob
    assert "boomi.example.com" in blob


def test_dynamic_text_is_html_escaped_and_cannot_break_out_of_the_script():
    evil = config('  - {slug: x, name: "<img src=x onerror=alert(1)></script><script>alert(2)", description: \'"><svg onload=alert(3)>\', adapter: {type: http_sync, url: "https://x.test"}, skills: [{id: s, name: S}]}')
    js = flows_js(build_flows(evil, customer="</script><b>Acme"))
    assert "</script" not in js and "<img" not in js and "<svg" not in js and "<b>Acme" not in js
    assert "&lt;img" in js
    assert js.isascii(), "non-ASCII must be \\u escaped"
    flow = by_id(build_flows(evil))["mesh"]
    assert set(flow["scenarios"]) == set(flow["seqs"]), "escaped pill labels must still match their sequences"
    assert not any("&amp;lt;" in step["t"] for steps in flow["seqs"].values() for step in steps), "narration must be escaped once"


# ---------------------------------------------------------------- injecting into a template

TEMPLATE = """<html><title>{{CUSTOMER}} guide</title><img src="{{LOGO_URI}}">
<script>
// a comment mentioning const FLOWS = [ ignored ] and a bracket ]
const FLOWS = [
 { id:'x', h1:'has ] and [ inside', s:"also ] here", t:`tpl ] ${1}` , /* ] */ n:[1,[2]] },
];
const AFTER = [1,2,3]; function buildFlow(){}
</script></html>"""


def test_the_flows_array_is_replaced_and_everything_around_it_is_kept():
    out = render_guide(TEMPLATE, build_flows(config(), "Acme & Co"), "Acme & Co")
    assert "const AFTER = [1,2,3]; function buildFlow(){}" in out
    assert "has ] and [ inside" not in out and "const FLOWS = [" in out
    assert "Acme &amp; Co guide" in out and "{{CUSTOMER}}" not in out and "{{LOGO_URI}}" not in out
    assert out.count("\nconst FLOWS = [") == 1 and "a comment mentioning" in out  # one real declaration; the comment is kept


def test_a_template_without_flows_is_rejected():
    with pytest.raises(ValueError, match="FLOWS"):
        render_guide("<html></html>", [], "x")


def test_logo_must_be_a_known_image(tmp_path):
    png = tmp_path / "l.png"
    png.write_bytes(b"\x89PNG")
    assert logo_data_uri(png).startswith("data:image/png;base64,")
    with pytest.raises(ValueError, match="logo"):
        logo_data_uri(tmp_path / "l.exe")


# ---------------------------------------------------------------- CLI


def run_cli(monkeypatch, *args):
    from a2a_bridge.main import main

    monkeypatch.setattr(sys, "argv", ["a2a-bridge", "guide", *args])
    main()


def write_config(tmp_path):
    path = tmp_path / "agents.yaml"
    path.write_text(f'agents:\n  - {{slug: a, name: A, adapter: {{type: http_sync, url: "https://x.test"}}, skills: [{{id: s, name: S}}]}}\nserver: {{inbound_auth: {{clients: [{{id: c, key_sha256: "{HASH}"}}]}}}}\n')
    return path


def test_cli_writes_a_guide_from_a_template(tmp_path, monkeypatch, capsys):
    (tmp_path / "t.html").write_text(TEMPLATE)
    out = tmp_path / "g.html"
    run_cli(monkeypatch, "--config", str(write_config(tmp_path)), "--template", str(tmp_path / "t.html"), "--customer", "Acme", "--out", str(out))
    assert "wrote" in capsys.readouterr().out and "Acme guide" in out.read_text()


def test_cli_data_only_needs_no_template(tmp_path, monkeypatch):
    out = tmp_path / "flows.js"
    run_cli(monkeypatch, "--config", str(write_config(tmp_path)), "--data-only", "--out", str(out))
    assert out.read_text().startswith("const FLOWS = [") and out.read_text().rstrip().endswith("];")


def test_cli_explains_a_missing_template(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as err:
        run_cli(monkeypatch, "--config", str(write_config(tmp_path)), "--out", str(tmp_path / "g.html"))
    assert "--template is required" in str(err.value)
    with pytest.raises(SystemExit) as err:
        run_cli(monkeypatch, "--config", str(write_config(tmp_path)), "--template", str(tmp_path / "nope.html"), "--out", str(tmp_path / "g.html"))
    assert "nope.html" in str(err.value)


# ---------------------------------------------------------------- against a real template, when you have one

REAL = os.environ.get("A2A_FIELD_GUIDE_TEMPLATE")


@pytest.mark.skipif(not REAL, reason="set A2A_FIELD_GUIDE_TEMPLATE to a compatible template file")
def test_renders_into_a_real_template(tmp_path):
    out = render_guide(Path(REAL).read_text(encoding="utf-8"), build_flows(config(STUDIO + "\n" + JOB, REMOTE), "Acme"), "Acme")
    assert "{{CUSTOMER}}" not in out and out.count("\nconst FLOWS = [") == 1 and "function buildFlow" in out


def test_mentions_of_flows_in_comments_strings_and_html_are_not_mistaken_for_the_declaration():
    tricky = """<p>const FLOWS = [ in html text ]</p>
<script>
const NOTE = "const FLOWS = [ inside a string ]"; // const FLOWS = [ in a comment ]
/* const FLOWS = [ in a block comment ] */
const FLOWS = [ {id:'real'} ];
</script>"""
    out = render_guide(tricky, build_flows(config()), "x")
    assert "<p>const FLOWS = [ in html text ]</p>" in out and 'const NOTE = "const FLOWS = [ inside a string ]";' in out
    assert "{id:'real'}" not in out and out.count("\nconst FLOWS = [") == 1


def test_apostrophes_in_html_before_the_script_do_not_confuse_the_search():
    out = render_guide("<p>Don't panic, it's fine</p><script>const FLOWS = [];</script>", build_flows(config()), "x")
    assert "Don't panic, it's fine" in out and '"id": "mesh"' in out


def test_admin_only_clients_are_not_drawn_as_callers():
    cfg = parse_config(
        f"""
server: {{inbound_auth: {{clients: [{{id: ops, key_sha256: "{HASH}", scopes: ["admin:*"]}}, {{id: crm, key_sha256: "{HASH}", scopes: ["agents:*"]}}]}}}}
agents:
  - {{slug: a, name: A, adapter: {{type: http_sync, url: "https://x.test"}}, skills: [{{id: s, name: S}}]}}
""",
        env={},
    )
    mesh = by_id(build_flows(cfg))["mesh"]
    assert "caller:crm" in ids(mesh) and "caller:ops" not in ids(mesh)


def test_without_a_logo_the_empty_chip_is_left_out_and_with_one_it_is_kept():
    template = '<div class="brand"><span class="x">&times;</span><img src="{{LOGO_URI}}" alt="{{CUSTOMER}}"></div><script>const FLOWS = [];</script>'
    bare = render_guide(template, build_flows(config()), "Acme")
    assert "<img" not in bare and "&times;" not in bare and "{{LOGO_URI}}" not in bare
    kept = render_guide(template, build_flows(config()), "Acme", "data:image/png;base64,AAAA")
    assert '<img src="data:image/png;base64,AAAA"' in kept and "&times;" in kept
