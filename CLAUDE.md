# Universal A2A Proxy (`a2a-bridge`)

## Project Vision
Build an open-source, declarative **Universal Agent-to-Agent (A2A) Protocol Proxy** in Python.
The goal is to "A2A-ify anything"—allowing developers to expose legacy REST APIs, Boomi processes, webhooks, or MCP servers as compliant A2A 1.0 agents using only a YAML configuration file, without writing custom backend glue code.

## Core Tech Stack
- **Language:** Python 3.11+
- **Web Framework:** FastAPI + Uvicorn
- **A2A Protocol:** Official `a2a-sdk` (`pip install "a2a-sdk[fastapi,sql]"`)
- **Config & Validation:** Pydantic v2 + PyYAML
- **HTTP Client:** `httpx` (async)
- **Persistence:** SQLite (default via SQLAlchemy/a2a-sdk) with optional Redis support for async task state
- **Testing:** `pytest` + `pytest-asyncio`

## Repository Architecture
Scaffold and maintain the following clean, plugin-friendly directory layout:

```text
a2a-bridge/
├── CLAUDE.md
├── pyproject.toml
├── README.md
├── docker-compose.yml
├── agents.example.yaml          # Sample declarative config (Boomi, REST, Webhook)
├── src/
│   └── a2a_bridge/
│       ├── __init__.py
│       ├── main.py              # FastAPI app entrypoint & dynamic route mounting
│       ├── config.py            # Pydantic models for parsing agents.yaml & env vars
│       ├── security.py          # Auth passthrough (Bearer/JWT), API key validation
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py          # Abstract BaseAdapter interface
│       │   ├── http_sync.py     # Synchronous REST POST/GET adapter
│       │   ├── http_async.py    # Trigger + status-polling adapter (bridges to SSE)
│       │   └── mcp_bridge.py    # (Phase 2) Wraps MCP servers as A2A skills
│       └── core/
│           ├── card_builder.py  # Generates /.well-known/agent-card.json per agent
│           └── executor.py      # Bridges a2a-sdk AgentExecutor to BaseAdapter
└── tests/
    ├── test_config.py
    ├── test_card_builder.py
    └── test_http_sync_adapter.py
```

## Architectural Rules & Guardrails
1. **Strict Protocol Translation Only:** Never put LLM prompts, orchestration logic, or heavy business transformations inside the proxy. The proxy strictly translates A2A discovery (`agent-card.json`), JSON-RPC (`tasks/send`, `tasks/get`), and SSE (`tasks/sendSubscribe`) into downstream adapter calls.
2. **Multi-Tenant Agent Routing:** Each agent defined in `agents.yaml` must mount cleanly under `/agents/{slug}` (serving `/agents/{slug}/.well-known/agent-card.json` and `/agents/{slug}/rpc`). Also support a root default agent if only one is configured.
3. **Zero-Trust Auth Passthrough:** Never hardcode downstream credentials in the YAML schema. Support `${ENV_VAR}` interpolation for static service keys and `passthrough` mode to forward incoming `Authorization` headers/JWTs directly to the backend (critical for Boomi/enterprise RBAC).
4. **Stateful Async Bridge:** Many enterprise endpoints (like Boomi async listeners) return a `202 Accepted` with a job/execution ID. The `http_async` adapter must poll a configured status endpoint and emit standard A2A task state transitions (`submitted` -> `working` -> `completed` / `failed`).
5. **Strict Typing & Async:** All I/O (HTTP calls, DB reads, SSE streams) must be fully `async`/`await`. Use Pydantic v2 models for all config and payload schemas.

## Declarative Config Specification (`agents.example.yaml`)
Ensure `config.py` validates this exact schema structure:

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  database_url: "sqlite+aiosqlite:///./a2a_bridge.db"
agents:
  - slug: "boomi-order-sync"
    name: "Boomi Order Fulfillment Agent"
    description: "Submits and tracks ERP orders through Boomi Web Services."
    version: "1.0.0"
    adapter:
      type: "http_sync"
      url: "https://boomi.com"
      method: "POST"
      timeout_seconds: 30
      auth:
        mode: "bearer_passthrough" # Options: none, bearer_passthrough, static_header
        header_name: "Authorization"
    skills:
      - id: "create_order"
        name: "Create ERP Order"
        description: "Creates a new order payload in the downstream ERP."
        tags: ["erp", "boomi", "orders"]
```

## Step-by-Step Build Plan
Execute these phases sequentially, verifying tests pass at each stage:

- **Phase 1: Project Scaffolding & Config**
  - Initialize `pyproject.toml` with dependencies (`fastapi`, `uvicorn`, `a2a-sdk[fastapi,sql]`, `pydantic`, `pyyaml`, `httpx`, `aiosqlite`, `pytest`, `pytest-asyncio`).
  - Implement `config.py` with environment variable expansion (`${VAR_NAME}`) and create `agents.example.yaml`.
- **Phase 2: Agent Card & Core Executor**
  - Implement `core/card_builder.py` to dynamically build compliant A2A `AgentCard` JSON payloads from YAML definitions.
  - Implement `adapters/base.py` defining `async def execute(self, context: TaskContext) -> AdapterResult`.
  - Implement `core/executor.py` connecting `a2a-sdk`'s `AgentExecutor` lifecycle to `BaseAdapter`.
- **Phase 3: HTTP Sync Adapter & FastAPI Mounting**
  - Implement `adapters/http_sync.py` using `httpx.AsyncClient` with header/JWT forwarding and structured error mapping (converting HTTP 4xx/5xx into clean A2A `TaskState.failed` messages).
  - Wire up `main.py` to mount all configured agents dynamically on startup.
- **Phase 4: End-to-End Verification**
  - Write unit and integration tests using FastAPI's `TestClient` / `httpx.AsyncClient` and a mock downstream HTTP server.
  - Add a clean `README.md` with a 60-second Quickstart and `docker-compose.yml`.

---

# a2a-bridge Extension: Bidirectional Boomi Agent Mesh

**Goal:** Boomi agents can be *called by* any A2A client (inbound) AND *call* any A2A agent, including other Boomi agents (outbound). This extends the phases above; Phases 1-4 remain as written.

## Verified Environment Facts (check before relying on them)
- Local Python is 3.9.6 and `pip` is not on PATH. The stack needs **3.11+**. Ask the user before installing Python or packages; use a venv (`python3.11 -m venv .venv`) once available.
- Protocol method names in the Phase 1-4 text (`tasks/send`, `tasks/sendSubscribe`) may be the old A2A names. Treat the installed `a2a-sdk` as the source of truth and inspect its imports before coding. Do not hardcode method names the SDK does not use.
- Boomi Agentstudio ("Agent Garden") facts, from the `boomi-agentstudio` skill:
  - The Execute API invokes by **`deployment_id`**, not `agent_id`. Only `PRODUCTION` deployments are reachable.
  - Conversational agents reply with an **SSE stream** (`text/event-stream`). Structured agents reply with **one JSON object** `{success, agent_id, session_id, data}`.
  - `session_id` is a ULID. The first reply returns it. Pass it back to continue the conversation. Memory is session-scoped.
  - A guardrail block arrives as a normal message whose `content` equals the agent's `blocked_message`. It is not a distinct event.
  - Hosts are region-specific. `AGENTSTUDIO_DESIGN_URL` and `AGENTSTUDIO_EXECUTE_URL` must be paired (US/UK/ANZ/JP). The caller's token needs `AGENT_INTERACT`.
  - The exact Execute API URL path and auth header are NOT documented in this file. Read `~/.claude/plugins/cache/boomi-companion/bc-agentstudio/*/skills/boomi-agentstudio/scripts/agentstudio-common.sh` and `agentstudio-session.sh` to derive them. Never guess.

## Direction 1: Inbound (A2A client -> Boomi)
Add `adapters/agentstudio.py`, adapter `type: "agentstudio"`:
- Config: `deployment_id`, `execute_url` (`${AGENTSTUDIO_EXECUTE_URL}`), `mode: conversational|structured`, `auth` (credentials from env only).
- Map A2A `contextId` <-> Agentstudio `session_id`. The first message calls `invoke`. Later messages in the same context call `continue`. Persist the mapping in the DB.
- Conversational: parse the SSE stream into A2A `working` status updates and artifact chunks, then `completed`. Detect a guardrail block by comparing content to the optional config `blocked_message`, and surface it as `rejected` or `failed` with a clear reason.
- Structured: map the A2A message to the JSON `message` object (validate against optional `input_schema`). Return `data` as a data artifact.
- Boomi processes behind web-services listeners keep using `http_sync` / `http_async`.

## Direction 2: Outbound (Boomi agent -> A2A agent)
An Agentstudio **OpenAPI tool** calls ONE REST endpoint with static headers and `{{param}}` placeholders. It cannot comfortably build JSON-RPC envelopes. So expose a thin REST facade over the proxy's A2A client:
- `POST /agents/{slug}/invoke` takes `{"message": str, "context_id": str|null}` and returns `{"task_id", "context_id", "state", "text", "data"}`. It blocks until a terminal or input-required state, with a configurable timeout, and returns `state: working` plus `task_id` on timeout.
- `GET /agents/{slug}/tasks/{task_id}` polls a running task (tasks/get equivalent).
- `GET /agents/{slug}/openapi.json` serves a per-agent OpenAPI doc, so tools are easy to register.
- New config section `remote_agents:` (card URL, auth mode) registers external A2A agents, including other a2a-bridge instances. Each is exposed through the same facade. Fetch and cache the remote agent card at startup and refresh it on TTL.
- Add `a2a_bridge/client.py`: an async A2A client built on `a2a-sdk` (card discovery, send, get, subscribe), reused by the facade and by tests.
- Add CLI `a2a-bridge tool-spec <slug>`. It writes an Agentstudio OpenAPI tool body JSON (name, description, `input_parameters`, `base_url`, `path`, `method`, `request_body`, `authentication`) to `active-development/feedback/`. The user then runs the `agentstudio-tool.sh create --type OpenAPI` and `install` steps from the `boomi-agentstudio` skill. Never put secrets in the body. Secret-bearing `token_auth` is provisioned in the Boomi console. Use `platform_jwt` only for Boomi Platform API targets.
- Facade auth: require an inbound credential on `/invoke`, configurable as static header from env or JWT validation. The proxy forwards or maps it per the zero-trust rules. Never open it unauthenticated by default.

## Direction 1+2 together: Boomi-to-Boomi
Agent A (Agentstudio) -> OpenAPI tool -> `POST /agents/B/invoke` -> proxy A2A client -> proxy A2A server -> `agentstudio` adapter -> Agent B (Execute API). Route this through the A2A server path, not a shortcut, so the same card, auth, and task semantics apply to every caller. Cross-tenant and cross-region calls work as long as each agent entry holds its own paired Execute host.

## Additional Phases (execute after Phase 4, tests green at each stage)
- **Phase 5: Agentstudio adapter.** `adapters/agentstudio.py`, SSE parsing, session mapping, guardrail-block detection, structured mode. Tests use a mock Execute API server (SSE and JSON modes).
- **Phase 6: A2A client + outbound facade.** `client.py`, `remote_agents` config, `/invoke`, `/tasks/{id}`, `/openapi.json`, timeout and polling behaviour.
- **Phase 7: Tool-spec generator CLI** (`tool-spec`) emitting valid Agentstudio OpenAPI tool bodies. Test against the field rules in the `boomi-agentstudio` skill (placeholder names match `input_parameters`, `type` in string/integer/float/boolean, no secrets).
- **Phase 8: Async bridge hardening.** Complete `http_async` (202 + polling), task persistence across restarts, cancellation, SSE resubscribe.
- **Phase 9: Mocks and round-trip E2E.** `tests/mocks/` holds a mock Boomi listener (sync and 202+status), a mock Agentstudio Execute API, and a mock remote A2A agent. One E2E test runs agent A -> facade -> proxy -> agent B and asserts state transitions, context continuity across two turns, and error mapping (4xx/5xx, guardrail block, timeout).
- **Phase 10: Optional Agent Control Tower (ACT) registration.** `a2a-bridge act-register <slug>` as a thin wrapper around the `agentstudio-act.sh` flow. Only if the user wants it.
- **Phase 11: Packaging and docs.** `README.md` with a 60-second Quickstart and a Boomi-to-Boomi walkthrough, `docker-compose.yml` (proxy + mocks), `.env.example`, and a security section (auth modes, the facade being authenticated, no secrets in YAML).

## Extra Guardrails for This Extension
- The facade is still protocol translation. No LLM calls, no routing logic, no payload transformation beyond field mapping.
- Hosts and deployment IDs live in YAML. Credentials only via `${ENV_VAR}` or passthrough. Redact `Authorization` and tokens from logs.
- Never invoke live Boomi tenants from automated tests. Use the mocks. Live smoke tests are manual and need the user's explicit go-ahead, because invoking an agent can have side effects and cost.
- Do not create or deploy anything in the user's Boomi account (tools, agents, packages) without asking first.

## Verified a2a-sdk 1.3.0 Facts (supersedes the older method names above)
- Python 3.14 venv at `.venv`. Run tests with `.venv/bin/python -m pytest`. Outside pytest, set `PYTHONPATH=src`.
- Types are protobuf: `a2a.types.a2a_pb2` (`AgentCard`, `Message`, `Part`, `TaskState.TASK_STATE_*`). The card uses `supported_interfaces[]` (`AgentInterface(url, protocol_binding, protocol_version)`), not a single `url`.
- JSON-RPC methods are `SendMessage`, `SendStreamingMessage`, `GetTask`, etc., not `tasks/send`. Mount with `a2a.server.routes.create_jsonrpc_routes(request_handler, rpc_url)` and `create_agent_card_routes(card)`. Handler: `DefaultRequestHandler(agent_executor, task_store, agent_card)`. Stores: `InMemoryTaskStore`, `DatabaseTaskStore(engine)`.
- Executors subclass `a2a.server.agent_execution.AgentExecutor` (`execute`, `cancel`) and publish via `TaskUpdater`. Enqueue a `Task` first when `current_task` is None. Incoming HTTP headers are at `context.call_context.state["headers"]`.
- Implemented so far (27 tests green): `config.py`, `core/card_builder.py`, `adapters/base.py` (`BaseAdapter.execute` plus streaming `stream`), `core/executor.py` (`BridgeExecutor`).
- **Public exposure (ngrok for initial tests):** set `server.public_base_url` so cards advertise the tunnel URL. Inbound auth in `security.py` is mandatory and on by default before any tunnel is started. Never start a tunnel without the user's go-ahead.
