# a2a-bridge

A declarative **Universal Agent-to-Agent (A2A) proxy**. Describe an agent in YAML and expose a REST API, a webhook or a Boomi process as a compliant A2A 1.0 agent, with no custom glue code.

> **Status: early development.** Config loading, agent cards, the HTTP sync adapter, the Boomi Agentstudio adapter, inbound auth and per-agent mounting work and are tested. Async job polling, the outbound client and a web UI are planned. See [CLAUDE.md](CLAUDE.md) for the roadmap.

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

## Try it with Docker (proxy + mock Boomi listener)

```bash
export A2A_BRIDGE_API_KEY=$(openssl rand -hex 24)
docker compose up --build        # or: docker-compose up --build
curl -X POST localhost:8000/agents/boomi-order-sync/rpc \
  -H "X-API-Key: $A2A_BRIDGE_API_KEY" -H "A2A-Version: 1.0" -H "Authorization: Bearer demo-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"data":{"sku":"A1","qty":2}}]}}}'
```

The mock listener (`tests/mocks/boomi_listener.py`) creates orders at `/ws/simple/createOrder`, requires a bearer token (set `MOCK_BOOMI_TOKEN` to pin an exact value), and has `/ws/simple/alwaysFails` for error-path testing. Point `agents.yaml` at your real Boomi listener URL when you are ready.

## Expose it with ngrok (initial testing)

```bash
export A2A_BRIDGE_API_KEY=$(openssl rand -hex 24)
ngrok http 8000
```

Put the `https://...ngrok-free.app` URL in `server.public_base_url` and restart, so agent cards advertise the public address. Share the API key only with the clients you want to allow. The card is public, and every other endpoint needs the key.

## How messages map to your backend

| A2A message | Downstream request body |
|---|---|
| One structured data part, no text | the data object, unchanged |
| Text, or several parts | `{"text", "data", "taskId", "contextId"}` |

| Downstream result | A2A task state |
|---|---|
| 2xx | `completed`, body returned as a data (JSON) or text artifact |
| 401 / 403, or missing bearer with `bearer_passthrough` | `auth-required` |
| other 4xx / 5xx, timeout, unreachable | `failed`, with a short reason |

A2A carries JSON numbers as doubles, so whole numbers in a **response** can read back as `2.0`. Numbers in **requests** are converted back to integers before they reach your backend.

## Boomi Agentstudio agents

`adapter.type: agentstudio` exposes a deployed Agentstudio agent as an A2A agent. See [`agents.agentstudio.example.yaml`](agents.agentstudio.example.yaml).

- **Conversations continue.** An A2A `contextId` maps to an Agentstudio `session_id` (stored in the database, so it survives restarts). A new `contextId` starts a new session.
- **Conversational agents** take text and stream progress: a tool call shows up as a `working` update, the reply as the result. **Structured agents** take one JSON object.
- **Guardrails.** Set `blocked_message` to the agent's guardrail text and a blocked turn becomes an A2A `rejected` task.
- **Identity.** Every caller acts as the Boomi service account in your config (the Execute API uses HTTP Basic with an API token), so protect the proxy accordingly. The token needs the `AGENT_INTERACT` privilege and must come from `${ENV}`.
- **Not yet supported:** file attachments.
- In YAML flow style (`{a: b}`), quote env references: `api_token: "${BOOMI_API_TOKEN}"`.

Try it without a Boomi account against the bundled mock Execute API: `uvicorn --app-dir tests/mocks agentstudio_execute:app --port 9100`.

## Security

- Inbound auth is on by default. The proxy will not start without `inbound_auth.api_keys`, and keys and downstream credentials must come from `${ENV_VAR}`. Literal secrets in the YAML are rejected.
- The agent card and `/healthz` are public. Everything else needs the key.
- `bearer_passthrough` forwards the caller's `Authorization` header to the backend, so the backend enforces its own RBAC.
- If you expose the proxy through a tunnel such as ngrok, set `server.public_base_url` to the tunnel URL so cards advertise it.

## Tests

```bash
.venv/bin/python -m pytest
```
