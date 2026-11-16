from pathlib import Path

import pytest

from a2a_bridge.config import ConfigError, expand_env, load_config, parse_config

EXAMPLE = Path(__file__).parent.parent / "agents.example.yaml"

MINIMAL = """
agents:
  - slug: svc
    name: Svc
    adapter: {type: http_sync, url: "https://x.test/hook"}
    skills: [{id: s1, name: S1}]
"""


def test_example_loads():
    cfg = load_config(EXAMPLE, env={"BOOMI_EXPORT_API_KEY": "k"})
    assert [a.slug for a in cfg.agents] == ["boomi-order-sync", "boomi-batch-export"]
    first = cfg.agents[0]
    assert first.adapter.type == "http_sync"
    assert first.adapter.auth.mode == "bearer_passthrough"
    assert cfg.agents[1].adapter.auth.value == "k"
    assert cfg.root_agent is None


def test_defaults_and_root_agent():
    cfg = parse_config(MINIMAL, env={})
    assert cfg.server.port == 8000
    assert cfg.root_agent is cfg.agents[0]
    assert cfg.agents[0].adapter.method == "POST"
    assert cfg.agents[0].version == "1.0.0"


def test_expand_env_variants():
    env = {"A": "1"}
    assert expand_env({"k": ["x${A}y", "${B:-dflt}"]}, env) == {"k": ["x1y", "dflt"]}


def test_expand_env_reports_all_missing():
    with pytest.raises(ConfigError, match="B, C"):
        expand_env(["${B}", "${C}"], {})


def test_literal_static_credential_rejected():
    text = MINIMAL.replace(
        '{type: http_sync, url: "https://x.test/hook"}',
        '{type: http_sync, url: "https://x.test/hook", auth: {mode: static_header, header_name: X-Key, value: hunter2}}',
    )
    with pytest.raises(ConfigError, match="environment variable"):
        parse_config(text, env={})


def test_static_header_from_env_ok():
    text = MINIMAL.replace(
        '{type: http_sync, url: "https://x.test/hook"}',
        '{type: http_sync, url: "https://x.test/hook", auth: {mode: static_header, header_name: X-Key, value: "${K}"}}',
    )
    assert parse_config(text, env={"K": "s"}).agents[0].adapter.auth.value == "s"


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda t: t.replace("slug: svc", "slug: Bad_Slug"), "slug"),
        (lambda t: t.replace("https://x.test/hook", "ftp://x.test"), "http"),
        (lambda t: t.replace("name: Svc", "name: Svc\n    bogus: 1"), "bogus"),
        (lambda t: t.replace("[{id: s1, name: S1}]", "[]"), "skills"),
        (lambda t: t.replace("type: http_sync", "type: carrier_pigeon"), "adapter"),
    ],
)
def test_invalid_config_rejected(mutate, match):
    with pytest.raises(ConfigError, match=match):
        parse_config(mutate(MINIMAL), env={})


def test_duplicate_slugs_rejected():
    agent = MINIMAL.split("agents:\n")[1]
    with pytest.raises(ConfigError, match="duplicate agent slugs"):
        parse_config("agents:\n" + agent + agent, env={})


def test_http_async_requires_job_id_placeholder():
    text = MINIMAL.replace(
        '{type: http_sync, url: "https://x.test/hook"}',
        '{type: http_async, url: "https://x.test/start", status_url: "https://x.test/status"}',
    )
    with pytest.raises(ConfigError, match="job_id"):
        parse_config(text, env={})


def test_public_base_url_used_for_cards():
    text = "server: {public_base_url: 'https://abc.ngrok.app/'}\n" + MINIMAL
    assert parse_config(text, env={}).server.base_url == "https://abc.ngrok.app"


def test_bad_yaml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_config("agents: [", env={})
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")
