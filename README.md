# a2a-bridge

A declarative **Universal Agent-to-Agent (A2A) proxy**. Describe an agent in YAML and expose a REST API, a webhook or a Boomi process as a compliant A2A 1.0 agent, with no custom glue code.

> **Status: early development.** Config loading, agent cards, the HTTP sync adapter, inbound auth and per-agent mounting work and are tested. Async job polling, Boomi Agentstudio support and the outbound client are planned. See [CLAUDE.md](CLAUDE.md) for the roadmap.

## Quickstart

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install "a2a-sdk[fastapi,sql]" fastapi uvicorn pydantic pyyaml httpx aiosqlite pytest pytest-asyncio
cp agents.example.yaml agents.yaml          # edit the downstream URL
export A2A_BRIDGE_API_KEY="$(openssl rand -hex 24)"
PYTHONPATH=src .venv/bin/python -m a2a_bridge.main --config agents.yaml
```

Then:

```bash
curl localhost:8000/agents/boomi-order-sync/.well-known/agent-card.json      # public discovery
curl -X POST localhost:8000/agents/boomi-order-sync/rpc \
  -H "X-API-Key: $A2A_BRIDGE_API_KEY" -H "A2A-Version: 1.0" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}}'
```

## Security

- Inbound auth is on by default. The proxy will not start without `inbound_auth.api_keys`, and keys and downstream credentials must come from `${ENV_VAR}`. Literal secrets in the YAML are rejected.
- The agent card and `/healthz` are public. Everything else needs the key.
- `bearer_passthrough` forwards the caller's `Authorization` header to the backend, so the backend enforces its own RBAC.
- If you expose the proxy through a tunnel such as ngrok, set `server.public_base_url` to the tunnel URL so cards advertise it.

## Tests

```bash
.venv/bin/python -m pytest
```
