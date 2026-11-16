import json
import sys

import httpx
import pytest

from a2a_bridge.config import parse_config
from a2a_bridge.toolspec import invoke_tool, problems, task_tool, write_tools

from test_facade import ALICE, BOOMI, stack  # reuse the in-process proxy stack

YAML = """
server: {public_base_url: "https://a2a.example.com"}
agents:
  - slug: it-support
    name: IT Support Assistant
    description: First-line IT support.
    adapter: {type: http_sync, url: "https://x.test"}
    skills: [{id: triage, name: Triage, description: Diagnoses a problem}]
"""


@pytest.fixture
def agent():
    return parse_config(YAML, env={}).agents[0]


@pytest.mark.parametrize("style", ["json", "query"])
def test_generated_tools_satisfy_the_agentstudio_openapi_rules(agent, style):
    assert problems(invoke_tool(agent, "https://a2a.example.com", style)) == []
    assert problems(task_tool(agent, "https://a2a.example.com")) == []


def test_json_style_tool_shape(agent):
    tool = invoke_tool(agent, "https://a2a.example.com/", "json")
    assert tool["path"] == "/agents/it-support/invoke" and tool["method"] == "POST"
    assert tool["base_url"] == "https://a2a.example.com"  # trailing slash trimmed
    assert [p["name"] for p in tool["input_parameters"]] == ["message", "context_id", "task_id"]
    assert [p["required"] for p in tool["input_parameters"]] == [True, False, False]
    template = json.loads(tool["request_body"]["template"].replace("{{message}}", "x").replace("{{context_id}}", "").replace("{{task_id}}", ""))
    assert template["wait_seconds"] == 50
    assert "Triage: Diagnoses a problem" in tool["description"] and "context_id" in tool["description"]
    assert tool["authentication"] is None and tool["headers"] == [] and tool["source_id"] is None


def test_query_style_binds_parameters_instead_of_a_body(agent):
    tool = invoke_tool(agent, "https://a2a.example.com", "query")
    assert tool["request_body"] is None
    bound = {q["name"]: q for q in tool["query_parameters"]}
    assert bound["message"]["input_parameter_name"] == "message" and bound["message"]["static_value"] is None
    assert bound["wait_seconds"]["static_value"] == "50" and bound["wait_seconds"]["input_parameter_name"] is None


def test_task_tool_uses_a_path_placeholder(agent):
    tool = task_tool(agent, "https://a2a.example.com")
    assert tool["method"] == "GET" and tool["path"] == "/agents/it-support/tasks/{{task_id}}"
    assert tool["input_parameters"][0]["name"] == "task_id"


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda t: t["input_parameters"].append({"name": "n", "description": "", "required": False, "type": "number"}), "not one of"),
        (lambda t: t.update(method="FETCH"), "method"),
        (lambda t: t.update(path="/x/{{nope}}"), "no matching input parameter"),
        (lambda t: t["request_body"].update(type="application/yaml"), "request_body.type"),
        (lambda t: t.update(authentication={"type": "token_auth", "config": {"key": "X-API-Key", "value": "s3cret"}}), "console"),
        (lambda t: t["headers"].append({"name": "X-API-Key", "static_value": "s3cret", "input_parameter_name": None}), "credential"),
        (lambda t: t["headers"].append({"name": "X-Tenant", "static_value": None, "input_parameter_name": None}), "exactly one"),
        (lambda t: t.update(name="x" * 51), "longer"),
    ],
)
def test_validator_catches_rule_violations(agent, mutate, expected):
    tool = invoke_tool(agent, "https://a2a.example.com", "json")
    mutate(tool)
    assert any(expected in p for p in problems(tool)), problems(tool)


def test_write_tools_creates_files_without_secrets(agent, tmp_path):
    paths = write_tools([agent], "https://a2a.example.com", tmp_path / "out")
    assert sorted(p.name for p in paths) == ["it-support-ask.tool.json", "it-support-check-task.tool.json"]
    for path in paths:
        body = json.loads(path.read_text())
        assert body["authentication"] is None
    assert [p.name for p in write_tools([agent], "https://a2a.example.com", tmp_path / "o2", with_task_tool=False)] == ["it-support-ask.tool.json"]


def test_cli_writes_files_and_explains_next_steps(tmp_path, monkeypatch, capsys):
    from a2a_bridge.main import main

    cfg = tmp_path / "agents.yaml"
    cfg.write_text(YAML)
    monkeypatch.setattr(sys, "argv", ["a2a-bridge", "tool-spec", "--config", str(cfg), "--out", str(tmp_path / "t"), "--style", "query"])
    main()
    captured = capsys.readouterr()
    assert str(tmp_path / "t" / "it-support-ask.tool.json") in captured.out
    assert "console" in captured.err and "X-API-Key" in captured.err
    assert json.loads((tmp_path / "t" / "it-support-ask.tool.json").read_text())["request_body"] is None


def test_cli_rejects_unknown_agents(tmp_path, monkeypatch):
    from a2a_bridge.main import main

    cfg = tmp_path / "agents.yaml"
    cfg.write_text(YAML)
    monkeypatch.setattr(sys, "argv", ["a2a-bridge", "tool-spec", "--config", str(cfg), "--agent", "nope", "--out", str(tmp_path)])
    with pytest.raises(SystemExit) as err:
        main()
    assert "nope" in str(err.value)


# The facade must accept exactly what these generated tools send.


async def test_facade_accepts_the_json_template_with_unused_placeholders_blank(tmp_path, agent):
    async with stack(tmp_path) as (c, _):
        tool = invoke_tool(agent, "https://a2a.example.com", "json")
        rendered = tool["request_body"]["template"].replace("{{message}}", "hello").replace("{{context_id}}", "").replace("{{task_id}}", "")
        r = await c.post("/agents/echo/invoke", content=rendered, headers={**ALICE, "content-type": "application/json"})
        assert r.status_code == 200 and r.json()["state"] == "completed"


async def test_facade_accepts_query_parameters_only(tmp_path):
    async with stack(tmp_path) as (c, _):
        r = await c.post("/agents/echo/invoke", params={"message": "hello there", "context_id": "", "task_id": "", "wait_seconds": "5"}, headers=ALICE)
        assert r.status_code == 200 and r.json()["state"] == "completed"


async def test_facade_accepts_form_encoded_bodies_and_json_data_strings(tmp_path):
    async with stack(tmp_path) as (c, _):
        r = await c.post("/agents/echo/invoke", data={"message": "hi", "data": '{"qty": 2}', "context_id": ""}, headers=ALICE)
        assert r.status_code == 200 and r.json()["state"] == "completed"
        bad = await c.post("/agents/echo/invoke", data={"message": "hi", "data": "{nope"}, headers=ALICE)
        assert bad.status_code == 422 and "JSON object" in bad.json()["error"]
        worse = await c.post("/agents/echo/invoke", params={"wait_seconds": "soon", "message": "x"}, headers=ALICE)
        assert worse.status_code == 422 and "number" in worse.json()["error"]


async def test_body_fields_override_query_parameters(tmp_path):
    async with stack(tmp_path) as (c, _):
        # agent-b echoes what it was asked, so we can see which value reached it
        r = await c.post("/agents/agent-b/invoke", params={"message": "from query"}, json={"message": "from body"}, headers={**ALICE, **BOOMI})
        assert r.json()["text"] == "Turn 1: you said from body"
        q = await c.post("/agents/agent-b/invoke", params={"message": "only query"}, headers={**ALICE, **BOOMI})
        assert q.json()["text"] == "Turn 1: you said only query"
