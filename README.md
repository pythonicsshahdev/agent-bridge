# a2a-bridge

A declarative **Universal Agent-to-Agent (A2A) proxy**. Describe an agent in YAML and expose a REST API, a webhook or a Boomi process as a compliant A2A 1.0 agent, with no custom glue code.

> **Status: early development.** Config loading, agent cards, the HTTP sync adapter, the Boomi Agentstudio adapter, inbound auth and per-agent mounting work and are tested. The agent-to-agent REST facade and outbound A2A client work and are tested. A read-only web control plane and a generated animated walkthrough are included. See [CLAUDE.md](CLAUDE.md) for the roadmap.

## Quickstart

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install "a2a-sdk[fastapi,sql]" fastapi uvicorn pydantic pyyaml httpx aiosqlite pytest pytest-asyncio
cp agents.example.yaml agents.yaml          # edit the downstream URL (production auth: see Security)
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

## Long-running jobs (`http_async`)

For backends that start a job and return an id (a Boomi process behind a 202, an export, a batch), `adapter.type: http_async` starts the job, polls a status endpoint, and reports progress as A2A `working` updates. See the second agent in [`agents.example.yaml`](agents.example.yaml).

- **Mapping:** `job_id_path` finds the id in the start response, `status_path` the status in each poll, and `completed_values` / `failed_values` decide the outcome (case-insensitive). `result_path` picks what to return and `error_path` the failure reason. Dotted paths work through lists (`items.0.id`).
- **Progress** is reported when the status changes, not on every poll.
- **Robust polling:** transient errors are tolerated (`max_poll_errors`), a 404 or 401/403 ends the task, and `max_poll_seconds` bounds the job. On timeout or cancel, `cancel_url` (if set) is called so the downstream job stops too, with the same caller credentials.
- **Safe:** the job id is URL-encoded into the status path, so a hostile backend cannot redirect the poll.
- The caller's bearer token (with `bearer_passthrough`) is used for the start, every poll and the cancel, and is never stored beyond the life of the job.

## Boomi Agentstudio agents

`adapter.type: agentstudio` exposes a deployed Agentstudio agent as an A2A agent. See [`agents.agentstudio.example.yaml`](agents.agentstudio.example.yaml).

- **Conversations continue.** An A2A `contextId` maps to an Agentstudio `session_id` (stored in the database, so it survives restarts). A new `contextId` starts a new session.
- **Conversational agents** take text and stream progress: a tool call shows up as a `working` update, the reply as the result. **Structured agents** take one JSON object.
- **Guardrails.** Set `blocked_message` to the agent's guardrail text and a blocked turn becomes an A2A `rejected` task.
- **Identity.** Every caller acts as the Boomi service account in your config (the Execute API uses HTTP Basic with an API token), so protect the proxy accordingly. The token needs the `AGENT_INTERACT` privilege and must come from `${ENV}`.
- **Not yet supported:** file attachments.
- In YAML flow style (`{a: b}`), quote env references: `api_token: "${BOOMI_API_TOKEN}"`.

Try it without a Boomi account against the bundled mock Execute API: `uvicorn --app-dir tests/mocks agentstudio_execute:app --port 9100`.

## Agent-to-agent: let Boomi agents call other agents

A Boomi Agentstudio agent can use another agent as a tool through the **REST facade**, because an Agentstudio OpenAPI tool calls one HTTP endpoint and cannot speak JSON-RPC.

```
Agent A (Agentstudio) -> OpenAPI tool -> POST /agents/<slug>/invoke -> local or remote A2A agent
```

| Endpoint | What it does |
|---|---|
| `POST /agents/{slug}/invoke` | `{"message": "...", "data": {...}, "context_id": "...", "task_id": "...", "wait_seconds": 60}`. Waits for an outcome, or returns `working` with a `task_id`. |
| `GET /agents/{slug}/tasks/{task_id}?wait_seconds=30` | Poll a running task. |
| `POST /agents/{slug}/tasks/{task_id}/cancel` | Cancel it. |
| `GET /agents/{slug}/openapi.json` | OpenAPI 3 description (public, no secrets), written so an LLM can decide when to call the agent. |

The reply is a small summary: `state` (`completed`, `working`, `failed`, `rejected`, `input_required`, `auth_required`, `canceled`), `text`, `data`, `message` (the reason, or the question the agent is asking), and `task_id` / `context_id`. Send the same `context_id` to continue a conversation. To answer an `input_required` task, send the `task_id` as well.

- **Same rules everywhere.** The facade uses the same auth, scopes, task isolation and executors as the A2A endpoint, so calling `/agents/B/invoke` needs the `agents:B` scope, and callers only see their own tasks.
- **Local and remote agents** share one endpoint. Register external A2A agents under `remote_agents:` (see [`agents.mesh.example.yaml`](agents.mesh.example.yaml)). Their card is fetched lazily and refreshed on a TTL, and an unreachable remote is a 502, not a startup failure.
- **Outbound auth to a remote agent:** `none`, `bearer_passthrough` (forward the caller's token), `static_header`, or `oauth_client_credentials` (token cached until shortly before it expires). Secrets must come from `${ENV}`, and remote URLs must be `https://`.
- **Error mapping:** 401/403 from the proxy's own auth, 404 unknown or foreign task, 422 bad request, 502 remote unreachable or rejected our credentials, 504 remote timed out.
- The endpoint accepts a JSON body, a form-encoded body, or query parameters (body fields win), so tools that cannot build a JSON body safely can still call it.
- Set `server.facade.enabled: false` to turn the facade off.

### Register an agent as an Agentstudio tool

```bash
a2a-bridge tool-spec --config agents.yaml --base-url https://a2a.example.com --out tools/
# writes tools/<slug>-ask.tool.json and tools/<slug>-check-task.tool.json
agentstudio-tool.sh create --type OpenAPI --body tools/it-support-ask.tool.json   # then: install --type OpenAPI <id>
```

The generated files contain **no credentials**. Set authentication in the Boomi console: a `token_auth` header with this proxy's API key. If the target agent uses `identity: caller`, the tool also needs a Boomi credentials header, which in practice identifies the tool's identity, so use a dedicated least-privilege Boomi token (or set the target to `identity: service_account`). `--style query` binds parameters instead of templating a JSON body, in case your Agentstudio does not escape quotes in `{{placeholders}}`. The tool definitions satisfy the documented OpenAPI tool rules (checked in tests) but have not been run against a live Agentstudio tenant.

## Security

**Who can call the proxy** (`server.inbound_auth`). The proxy refuses to start without a way to authenticate callers. The agent card and `/healthz` are public so clients can discover agents; everything else needs credentials.

| Method | Use it for |
|---|---|
| **Per-client API keys** (`clients:`) | Machine clients. Only the SHA-256 is configured (`a2a-bridge keygen` prints a key and its hash). Each client has `scopes` (`agents:<slug>` or `agents:*`) and an optional `expires_at`. List two hashes to rotate with no downtime. |
| **OAuth 2.0 / OIDC JWTs** (`jwt:`) | People and services behind your identity provider. Checks signature (JWKS), issuer, audience, expiry and not-before. Asymmetric algorithms only: `HS*` and `none` are never accepted. Scopes come from the token (`scope`, `scp`, or your own claim). Cards advertise the OIDC discovery URL. |
| `api_key_or_jwt` | Both, so machines and people can use the same proxy. |
| `api_keys:` | Quick-start single key with full access. Prefer `clients:` for anything shared. |

Also built in:
- **Per-agent authorization.** A caller without the scope for an agent gets 403, whether or not that agent exists.
- **Task isolation.** Each caller's tasks are private to them. Another caller who learns a task id still cannot read it.
- **Rate limiting** per caller (429 with `Retry-After`), a **request size cap** (413), and an **audit log** (`a2a_bridge.audit`, one JSON line per request with caller, agent, status and latency, and never credentials).
- **No secrets in config.** Keys are stored as hashes, and downstream credentials must come from `${ENV_VAR}`. Literal secrets are rejected.
- An identity provider outage returns 503 with `Retry-After`, not a silent denial.

**Who the backend sees.**
- `bearer_passthrough` forwards the caller's `Authorization` header, so the backend enforces its own RBAC.
- **Agentstudio** defaults to `identity: caller`: each caller sends their own Boomi credentials in `X-Boomi-Authorization` (`Basic base64(BOOMI_TOKEN.<user>:<api token>)`), so Boomi RBAC and audit apply per person. Conversations are keyed by caller, so nobody can reach another caller's session by guessing a `contextId`. For trusted internal use you can instead set `identity: service_account` (one least-privilege token held by the proxy) or `allow_service_account_fallback: true`.

**Deploying safely**
- Terminate TLS in front of the proxy (a load balancer, or ngrok for testing). The proxy warns if `public_base_url` is plain `http://` on a non-local host.
- Rate limits are in memory per process. With several replicas, each enforces its own limit, so enforce a shared one at your gateway too.
- Set `server.public_base_url` to the tunnel or gateway URL so cards advertise it.

## Control plane (web UI)

A read-only console at `/admin` that shows how the proxy is wired, what is running, who can call what, recent traffic, and lets you check a configuration before deploying it. It is **off by default**.

```yaml
server:
  admin: {enabled: true}
  inbound_auth:
    clients:
      - {id: ops, key_sha256: "<a2a-bridge keygen>", scopes: ["admin:*"]}
```

Open `https://your-host/admin` and sign in with a key (or an OIDC token) that has the `admin:*` scope.

| Page | What it answers |
|---|---|
| Flow | Callers, proxy, agents and backends as a map. A wire lights up when a request goes through it. Configuration findings sit below. |
| Agents | What each agent runs on, its backend host, and its entry points. |
| Access | Sign-in method, rate limit, and each client's scopes and expiry. |
| Traffic | Recent requests (kept in memory) with result, caller and timing, plus per-agent counts. |
| Add an agent | A form that adapts to what the agent runs on (a REST call, a long-running job, or a Boomi Agentstudio agent), checks it against the same rules the proxy uses as you type, and shows the exact YAML to copy or save. It also tells you which `${ENV}` variables it needs and whether they are set here, and what to do next. |
| Check a config | Paste a configuration to see every problem, what would change compared with the running proxy, and anything risky. Nothing is applied. |

Built to be safe to leave on:
- **Explicit permission.** `admin:*` is never implied by agent scopes, and an admin key cannot call agents. The proxy refuses to start with the control plane on if sign-in is `none` or `bearer_passthrough`.
- **Nothing sensitive is shown:** no tokens, key hashes, header values, or URL paths (which can carry secrets), only host names. A test plants secrets everywhere and asserts none appear in any response.
- **Strict browser policy:** the page is served with a Content-Security-Policy that allows no inline script, no outside origins and no framing. Server data is only ever written as text, never as markup (tested with hostile strings).
- **Audited:** admin requests appear in the audit log like any other.
- **Read only:** it cannot change the running proxy. "Add an agent" produces configuration for you to deploy; it never registers anything by itself. Apply changes through your normal deploy.
- **Secrets stay out of files:** the form only accepts environment variable *names* for credentials and writes `${NAME}`. It reports whether a variable is set, never its value.

If the proxy is reachable from the internet, restrict `/admin` at your load balancer (an IP allow-list or VPN) as well.

## Animated walkthrough (field guide)

`a2a-bridge guide` turns your real configuration into a shareable, animated walkthrough for demos and reviews: how a request travels, how an Agentstudio conversation continues, how a long job is started and cancelled, and how one agent calls another. Sections appear only when your configuration has the thing to show.

```bash
a2a-bridge guide --config agents.yaml --template field-guide-template.html --customer "Acme" --logo logo.png --out guide.html
a2a-bridge guide --config agents.yaml --data-only --out flows.js      # just the flow data, no template needed
```

The page itself comes from a field-guide template **that you supply** (`--template`); none is included in this project. Any HTML file with a `const FLOWS = [ ... ]` array in the documented node, wire and sequence format works. The generator fills that array, plus `{{CUSTOMER}}` and `{{LOGO_URI}}`, and leaves everything else untouched. If you omit `--logo`, the empty logo box is left out.

- **Built from your configuration:** clients, agents, adapters, remote agents and the sign-in method. Admin-only clients are not shown as callers.
- **Safe to share:** only host names appear (URL paths can carry secrets), every dynamic value is HTML-escaped, and non-ASCII text is escaped so the file cannot break out of its script.
- The output embeds your template, so do not commit it if the template is private (`guide*.html` is git-ignored here).

## Deploying

The production image is `docker build --target prod .` (non-root, pinned dependencies, JSON logs). It talks to SQLite or Postgres (`pip install ".[postgres]"` and a `postgresql+asyncpg://` URL), serves `/healthz` (liveness) and `/readyz` (database check), and trusts `X-Forwarded-*` only from the peers in `server.forwarded_allow_ips`.

For AWS (EC2 + ALB + WAF + Secrets Manager + CloudWatch, optional RDS), see [`deploy/aws`](deploy/aws/README.md).

## Tests

Python tests run with `.venv/bin/python -m pytest`. The control plane also has browser tests that drive a real Chrome against the demo (`demo/run.sh`, then `cd tests/browser && npm install && npm test`); CI runs both.

```bash
.venv/bin/python -m pytest
```
