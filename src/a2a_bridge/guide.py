"""Generate an animated, shareable walkthrough ("field guide") of a configuration.

The flows are built from the real config: which clients exist, which agents run on which adapter, whether the
REST facade and remote agents are set up. They are injected into a field-guide template that YOU supply
(`--template`); no template ships with this project. Everything dynamic is HTML-escaped, and only host names are
shown (URL paths can carry secrets).
"""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from typing import Any

from a2a_bridge.admin.graph import agent_callers, host_of
from a2a_bridge.config import AgentConfig, AppConfig

GRID_X = {"caller": 95, "gate": 290, "core": 480, "adapter": 670, "backend": 860}
MAX_AGENTS = 4
MAX_CALLERS = 3
PAYLOAD_MAX = 22  # longer labels crowd the travelling bubble
TRANSPARENT_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rows(count: int, top: int = 110, bottom: int = 450) -> list[int]:
    if count <= 1:
        return [(top + bottom) // 2]
    step = (bottom - top) / (count - 1)
    return [round(top + i * step) for i in range(count)]


def _node(id_: str, kind: str, icon: str, label: str, fn: str, pos: tuple[int, int], what: str, val: str) -> dict[str, Any]:
    return {"id": id_, "kind": kind, "icon": icon, "label": esc(_clip(label, 26)), "fn": esc(_clip(fn, 30)), "pos": list(pos), "what": esc(what), "val": esc(val)}


def _step(node: str, text: str, kind: str | None = None, payload: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"node": node, "t": esc(text)}
    if kind:
        step["kind"] = kind
    if payload:
        step["payload"] = esc(_clip(payload, PAYLOAD_MAX))
    return step


def _wire(a: str, b: str, label: str | None = None, kind: str | None = None) -> list[Any]:
    extra = {k: v for k, v in (("label", esc(label) if label else None), ("kind", kind)) if v}
    return [a, b, extra] if extra else [a, b]


def _eyebrow(customer: str, topic: str) -> str:
    return f"{esc(customer)} × a2a-bridge · {esc(topic)}"


def _gate_node(config: AppConfig, pos: tuple[int, int]) -> dict[str, Any]:
    mode = config.server.inbound_auth.mode.replace("_", " ")
    limit = config.server.inbound_auth.rate_limit
    return _node(
        "gate", "guard", "\U0001f6e1", "Access checks", f"sign-in: {mode}", pos,
        "Checks the key or token, that this caller may reach this agent, and the rate limit. Every request is written to an audit log without credentials.",
        "Only the right callers reach each agent" + (f", at most {limit.requests_per_minute} requests a minute each." if limit else "."),
    )


def _callers(config: AppConfig, ys: list[int]) -> list[dict[str, Any]]:
    auth = config.server.inbound_auth
    nodes: list[dict[str, Any]] = []
    entries: list[tuple[str, str, str]] = [(f"caller:{c.id}", c.id, ", ".join(c.scopes)) for c in agent_callers(config)[:MAX_CALLERS]]
    if auth.jwt is not None and len(entries) < MAX_CALLERS:
        entries.append(("caller:oidc", "People and services", "signed in with your identity provider"))
    if not entries:
        entries.append(("caller:any", "A2A clients", f"sign-in: {auth.mode.replace('_', ' ')}"))
    for (id_, label, fn), y in zip(entries, ys):
        nodes.append(_node(id_, "ext", "\U0001f916", label, fn, (GRID_X["caller"], y), "A client calling the proxy over A2A or the REST facade.", "Each caller has its own scopes and its own private tasks."))
    return nodes


def _adapter_text(agent: AgentConfig) -> tuple[str, str, str]:
    a = agent.adapter
    if a.type == "agentstudio":
        who = "each caller's own Boomi token" if a.identity == "caller" else "a shared service account"
        return "\U0001f9e0", "Boomi Agentstudio", f"Calls the deployed agent through the Execute API using {who}. A2A conversations map to Boomi sessions."
    if a.type == "http_async":
        return "⏳", "Async job", "Starts a downstream job, polls its status, reports progress, and can cancel it."
    return "⚡", "REST call", "Turns the A2A message into one HTTP call and maps the answer or error back."


def _overview(config: AppConfig, customer: str) -> dict[str, Any]:
    agents = config.agents[:MAX_AGENTS]
    remotes = config.remote_agents[: max(0, 6 - len(agents))]
    caller_rows = _rows(min(MAX_CALLERS, max(1, len(agent_callers(config)) + (1 if config.server.inbound_auth.jwt else 0))))
    callers = _callers(config, caller_rows)
    agent_rows = _rows(len(agents))
    backend_rows = _rows(len(agents) + len(remotes))
    nodes = callers + [
        _gate_node(config, (GRID_X["gate"], 280)),
        _node("core", "brain", "⚙", "a2a-bridge", "translates A2A", (GRID_X["core"], 280), "Speaks the A2A protocol to callers and turns each task into a call to the right adapter, then returns the outcome.", "Anything with an API becomes an agent, with no glue code."),
    ]
    wires: list[list[Any]] = [_wire(c["id"], "gate") for c in callers] + [_wire("gate", "core")]
    scenarios: dict[str, list[str]] = {}
    seqs: dict[str, list[dict[str, Any]]] = {}
    first = callers[0]
    first_name = html.unescape(first["label"])  # node labels are already escaped; _step escapes again
    for i, agent in enumerate(agents):
        icon, kind_label, what = _adapter_text(agent)
        aid, bid = f"adapter:{agent.slug}", f"backend:{agent.slug}"
        host = host_of(agent.adapter.execute_url if agent.adapter.type == "agentstudio" else agent.adapter.url) or "backend"
        nodes.append(_node(aid, "step", icon, agent.name, agent.adapter.type, (GRID_X["adapter"], agent_rows[i]), what, agent.description or f"Served at /agents/{agent.slug}."))
        nodes.append(_node(bid, "ext", "\U0001f5c4", kind_label, host, (GRID_X["backend"], backend_rows[i]), f"Where the work for {agent.name} actually happens.", "Your system stays as it is; only the protocol changes."))
        wires += [_wire("core", aid), _wire(aid, bid)]
        label = esc(_clip(agent.name, 28))  # pill text is inserted as HTML, and seqs are keyed by it
        scenarios[label] = [c["id"] for c in callers] + ["gate", "core", aid, bid]
        seqs[label] = [
            _step(first["id"], f"{first_name} asks {agent.name}", payload=f"/agents/{agent.slug}"),
            _step("gate", "Key checked, scope and rate limit passed", "guard"),
            _step("core", "The A2A message is translated"),
            _step(aid, f"{agent.adapter.type} adapter makes the call"),
            _step(bid, f"{host} answers", "ok", "200 OK"),
            _step("core", "The reply goes back as a completed A2A task", "ok"),
        ]
    for j, remote in enumerate(remotes):
        rid = f"remote:{remote.slug}"
        nodes.append(_node(rid, "ext", "\U0001f310", remote.slug, f"remote {host_of(remote.card_url) or ''}", (GRID_X["backend"], backend_rows[len(agents) + j]), "An external A2A agent reached through the REST facade.", "Agents in other organisations or other proxies join the same mesh."))
        wires.append(_wire("core", rid, "facade", "async"))
    scenarios["Caller refused"] = [first["id"], "gate"]
    seqs["Caller refused"] = [_step(first["id"], "A request arrives with a wrong key or no scope for this agent"), _step("gate", "Refused with 401 or 403: nothing reaches the agent", "guard")]
    default = next(iter(seqs))
    flow: dict[str, Any] = {
        "id": "mesh", "nav": "The mesh", "type": "flow",
        "eyebrow": _eyebrow(customer, "Overview"),
        "h1": 'Every system an <span class="hl">agent</span>, one front door.',
        "sub": f"{len(config.agents)} agent{'s' if len(config.agents) != 1 else ''} served{f' and {len(config.remote_agents)} remote' if config.remote_agents else ''}. <b>Requests are checked before they reach anything</b>, then translated to the protocol each system already speaks.",
        "title": "Follow a request",
        "desc": "From the caller through the access checks and the proxy to the agent's backend. Pick an agent, then press Run. Hover any box to see what it does.",
        "scenarios": scenarios, "nodes": nodes, "wires": wires, "seq": seqs[default], "seqs": seqs,
    }
    if len(config.agents) > MAX_AGENTS:
        flow["note"] = esc(f"Showing the first {MAX_AGENTS} of {len(config.agents)} agents.")
    return flow


def _conversation(config: AppConfig, customer: str) -> dict[str, Any] | None:
    studio = next((a for a in config.agents if a.adapter.type == "agentstudio"), None)
    if studio is None:
        return None
    a = studio.adapter
    caller_token = a.identity == "caller"
    nodes = [
        _node("client", "ext", "\U0001f916", "A2A client", "sends a message", (95, 290), "Any A2A client, or a Boomi agent through the REST facade.", "Works with the tools people already use."),
        _gate_node(config, (290, 290)),
        _node("map", "step", "\U0001f9f5", "Conversation map", "context to session", (480, 160), "Remembers which Boomi session belongs to which A2A conversation, per caller, in the database.", "Follow-up messages continue the same conversation, even after a restart."),
        _node("exec", "ext", "☁", "Execute API", host_of(a.execute_url) or "Agentstudio", (670, 290), "Boomi's endpoint for running a deployed agent." + (" Called with the caller's own Boomi token." if caller_token else " Called with a shared service account."), "Boomi's own access control and audit still apply."),
        _node("agent", "brain", "✦", studio.name, a.mode, (860, 290), "The deployed Boomi Agentstudio agent: it reasons, calls its tools and replies.", studio.description or "Conversational agents stream their progress."),
        _node("guard", "guard", "⛔", "Agent guardrails", "blocks unsafe turns", (860, 450), "Policies on the Boomi agent. A blocked turn comes back as a message equal to its blocked text.", "A blocked turn is reported as rejected, not as an answer."),
        _node("reply", "step", "✅", "A2A task", "state and result", (480, 430), "The outcome as an A2A task: working updates while tools run, then completed, rejected or failed.", "Callers get a standard result whatever sits behind it."),
    ]
    wires = [_wire("client", "gate"), _wire("gate", "map"), _wire("map", "exec", "invoke / continue", "async"), _wire("exec", "agent"), _wire("agent", "guard", "policy"), _wire("agent", "reply", "answer", "ok"), _wire("guard", "reply", "rejected", "error"), _wire("reply", "client", "result", "return")]
    base = ["client", "gate", "map", "exec", "agent"]
    scenarios = {"First message": [*base, "reply"], "Follow-up in the same conversation": [*base, "reply"], "Guardrail blocks the turn": [*base, "guard", "reply"]}
    token_step = _step("exec", "Called with the caller's own Boomi token" if caller_token else "Called with the service account")
    seqs = {
        "First message": [
            _step("client", "A new conversation: no contextId yet", payload="hello"),
            _step("gate", "Authenticated and allowed", "guard"),
            _step("map", "New contextId, so a new Boomi session is started"),
            token_step,
            _step("agent", "The agent thinks and may call its tools"),
            _step("reply", "Completed: the reply and a contextId go back", "ok", "completed"),
        ],
        "Follow-up in the same conversation": [
            _step("client", "Same contextId as before", payload="and the second?"),
            _step("gate", "Authenticated and allowed", "guard"),
            _step("map", "Found this caller's session and continues it"),
            token_step,
            _step("agent", "The agent remembers the earlier turns"),
            _step("reply", "Completed, in the same conversation", "ok", "completed"),
        ],
        "Guardrail blocks the turn": [
            _step("client", "A message that breaks a policy", payload="blocked text"),
            _step("gate", "Authenticated and allowed", "guard"),
            _step("map", "Continues the session"),
            token_step,
            _step("agent", "The agent's guardrails inspect the turn"),
            _step("guard", "The turn is blocked", "guard"),
            _step("reply", "Reported as rejected, with the reason", "guard", "rejected"),
        ],
    }
    return {
        "id": "conversation", "nav": "Conversations", "type": "flow",
        "eyebrow": _eyebrow(customer, "Boomi Agentstudio"),
        "h1": 'A conversation that <span class="hl">keeps going</span>.',
        "sub": f"<b>{esc(studio.name)}</b> runs on Boomi Agentstudio. The proxy keeps each caller's conversations apart and maps every A2A conversation to one Boomi session.",
        "title": "One conversation, several turns",
        "desc": "Pick a scenario and press Run to see what happens to the first message, a follow-up, and a message the agent's guardrails block.",
        "scenarios": scenarios, "nodes": nodes, "wires": wires, "seq": seqs["First message"], "seqs": seqs,
    }


def _long_job(config: AppConfig, customer: str) -> dict[str, Any] | None:
    job = next((a for a in config.agents if a.adapter.type == "http_async"), None)
    if job is None:
        return None
    a = job.adapter
    nodes = [
        _node("client", "ext", "\U0001f916", "Caller", "starts the job", (95, 290), "Anyone who may call this agent.", "No need to hold a connection open for a slow job."),
        _gate_node(config, (290, 290)),
        _node("start", "step", "▶", "Start the job", "sends the request", (480, 150), "Calls the backend to start the work and reads the job id from the reply.", "A long job becomes a normal A2A task."),
        _node("job", "ext", "\U0001f5c4", job.name, host_of(a.url) or "Job API", (670, 290), "The system that actually does the work.", "Nothing changes in the backend."),
        _node("poll", "step", "\U0001f504", "Check progress", f"every {a.poll_interval_seconds:g}s", (480, 430), "Polls the status endpoint and reports each change as a working update. Tolerates short outages.", "Callers see progress, not silence."),
        _node("done", "step", "✅", "Result returned", "completed or failed", (860, 430), "When the job reports a finished or failed status the task ends with the mapped result or the reason.", "A clear outcome either way."),
        _node("cancel", "guard", "⛔", "Stop the job", "on cancel or timeout", (860, 150), "If the caller cancels or the job overruns " + f"{a.max_poll_seconds:g}s, the downstream job is cancelled too" + (" (a cancel URL is set)." if a.cancel_url else " (set a cancel_url to enable this)."), "No orphaned jobs running up cost."),
    ]
    wires = [_wire("client", "gate"), _wire("gate", "start"), _wire("start", "job", "202 + job id", "async"), _wire("job", "poll", "status", "async"), _wire("poll", "done", "finished", "ok"), _wire("poll", "cancel", "cancel / timeout", "error"), _wire("cancel", "job", "cancel", "error")]
    scenarios = {"Job finishes": ["client", "gate", "start", "job", "poll", "done"], "Caller cancels": ["client", "gate", "start", "job", "poll", "cancel"]}
    seqs = {
        "Job finishes": [
            _step("client", "Asks for the job", payload="start export"),
            _step("gate", "Authenticated and allowed", "guard"),
            _step("start", "Starts the job downstream"),
            _step("job", "Accepted: a job id comes back", payload="202 + id"),
            _step("poll", "Checks the status and reports progress"),
            _step("done", "Finished: the result is returned", "ok", "completed"),
        ],
        "Caller cancels": [
            _step("client", "Asks for the job", payload="start export"),
            _step("gate", "Authenticated and allowed", "guard"),
            _step("start", "Starts the job downstream"),
            _step("job", "Accepted: a job id comes back", payload="202 + id"),
            _step("poll", "Still running"),
            _step("cancel", "The caller cancels the task", "guard"),
            _step("job", "The downstream job is stopped too", "guard", "DELETE"),
        ],
    }
    return {
        "id": "jobs", "nav": "Long jobs", "type": "flow",
        "eyebrow": _eyebrow(customer, "Long-running work"),
        "h1": 'Slow work, <span class="hl">visible progress</span>.',
        "sub": f"<b>{esc(job.name)}</b> starts a job and takes a while. The proxy starts it, watches it, and stops it if the caller walks away.",
        "title": "From start to result",
        "desc": "Pick a scenario and press Run to see a job finish normally, or be cancelled all the way down.",
        "scenarios": scenarios, "nodes": nodes, "wires": wires, "seq": seqs["Job finishes"], "seqs": seqs,
    }


def _agent_to_agent(config: AppConfig, customer: str) -> dict[str, Any] | None:
    if not config.server.facade.enabled or (len(config.agents) < 2 and not config.remote_agents):
        return None
    local = config.agents[-1] if len(config.agents) > 1 else config.agents[0]
    remote = config.remote_agents[0] if config.remote_agents else None
    wait = f"{config.server.facade.default_wait_seconds:g}s"
    nodes = [
        _node("a", "ext", "\U0001f9e0", "Boomi agent A", "needs another agent", (95, 150), "A Boomi Agentstudio agent in the middle of its work.", "Agents can use each other like any other tool."),
        _node("tool", "step", "\U0001f527", "OpenAPI tool", "one HTTP call", (330, 150), "Registered once with `a2a-bridge tool-spec`. It calls a single REST endpoint, which is all an Agentstudio tool can do.", "No protocol knowledge needed on the Boomi side."),
        _gate_node(config, (565, 150)),
        _node("facade", "brain", "⇄", "REST facade", f"waits up to {wait}", (820, 300), "Turns the one HTTP call into an A2A task, waits for the outcome, and returns a short summary. Slow tasks come back as working with a task id to check.", "Same checks and same isolation as any other caller."),
        _node("b", "step", "\U0001f9e0", local.name, "local agent", (820, 460), "An agent this proxy serves. The call goes through the same handler as a direct A2A request.", local.description or "Reached in process, with no extra hop."),
    ]
    wires = [_wire("a", "tool"), _wire("tool", "gate", "POST /invoke"), _wire("gate", "facade"), _wire("facade", "b", "A2A task", "ok")]
    local_label = esc(f"Local agent: {_clip(local.name, 20)}")
    scenarios = {local_label: ["a", "tool", "gate", "facade", "b"]}
    seqs = {
        local_label: [
            _step("a", "Agent A decides it needs another agent", payload="ask(message)"),
            _step("tool", "The tool sends one HTTP request", payload="POST /invoke"),
            _step("gate", "Key checked and scope for the target verified", "guard"),
            _step("facade", f"Starts the task and waits up to {wait}"),
            _step("b", f"{local.name} answers: completed", "ok", "completed"),
        ]
    }
    if remote is not None:
        label = esc(f"Remote agent: {_clip(remote.slug, 18)}")
        nodes.append(_node("r", "ext", "\U0001f310", remote.slug, f"remote {host_of(remote.card_url) or ''}", (565, 460), "An external A2A agent. Its card is fetched and cached, and the proxy signs in to it as configured.", "Partners join the mesh without bespoke integration."))
        wires.append(_wire("facade", "r", "A2A over HTTPS", "async"))
        scenarios[label] = ["a", "tool", "gate", "facade", "r"]
        seqs[label] = [
            _step("a", "Agent A needs the partner's agent", payload="ask(message)"),
            _step("tool", "The tool sends one HTTP request", payload="POST /invoke"),
            _step("gate", "Key checked and scope for the target verified", "guard"),
            _step("facade", "Reads the remote card and signs in as configured"),
            _step("r", f"{remote.slug} answers", "ok", "completed"),
        ]
    default = next(iter(seqs))
    return {
        "id": "agents", "nav": "Agents calling agents", "type": "flow",
        "eyebrow": _eyebrow(customer, "Boomi to Boomi"),
        "h1": 'Agents that <span class="hl">call each other</span>.',
        "sub": "A Boomi agent uses another agent, local or in another organisation, <b>through one ordinary HTTP tool</b>. The proxy does the protocol work.",
        "title": "From one agent to the next",
        "desc": "Press Run to follow a call from agent A to the agent it needs.",
        "scenarios": scenarios, "nodes": nodes, "wires": wires, "seq": seqs[default], "seqs": seqs,
    }


def _security(config: AppConfig, customer: str) -> dict[str, Any]:
    auth = config.server.inbound_auth
    caller_boomi = [a for a in config.agents if a.adapter.type == "agentstudio" and a.adapter.identity == "caller"]
    cards = [
        {"ic": "\U0001f511", "h": "Signed in, every time", "p": esc(f"Callers sign in with {auth.mode.replace('_', ' ')}. Keys are stored only as hashes and can expire and rotate with no downtime.")},
        {"ic": "\U0001f3af", "h": "Only what each caller needs", "p": esc(f"{len(auth.clients)} client{'s' if len(auth.clients) != 1 else ''} with scopes per agent. A caller can't read anyone else's tasks.")},
        {"ic": "\U0001f9fe", "h": "Seen and recorded", "p": "Every request is an audit line with who, which agent, result and time. Credentials are never recorded."},
    ]
    if caller_boomi:
        cards.append({"ic": "\U0001f464", "h": "Boomi sees the real person", "p": "Callers use their own Boomi token, so Boomi's access control and audit apply to each person, not one shared account."})
    cards.append({"ic": "\U0001f510", "h": "No secrets in files", "p": "Credentials come from the environment or a secrets manager. Configuration that contains a literal secret is refused."})
    return {"id": "security", "nav": "Security", "type": "content", "eyebrow": _eyebrow(customer, "Security"), "h1": 'Safe to put in <span class="hl">front of everything</span>.', "sub": "The proxy checks every request before it reaches a system, and keeps what it knows to a minimum.", "cards": cards}


def build_flows(config: AppConfig, customer: str = "Your organisation") -> list[dict[str, Any]]:
    flows = [f for f in (_overview(config, customer), _conversation(config, customer), _long_job(config, customer), _agent_to_agent(config, customer), _security(config, customer)) if f]
    for number, flow in enumerate(flows, start=1):
        flow["nav"] = f"{number:02d} {flow['nav']}"
    return flows


# ---- injection into a template -------------------------------------------------------------------------------

_FLOWS_START = re.compile(r"const\s+FLOWS\s*=\s*\[")


def _matching_bracket(text: str, open_index: int) -> int:
    """Index of the `]` closing the `[` at open_index, skipping strings, template literals and comments."""
    depth, i, n = 0, open_index, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote, i = ch, i + 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i == -1 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            i = n if i == -1 else i + 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("could not find the end of the FLOWS array in the template")


def flows_js(flows: list[dict[str, Any]]) -> str:
    """A JS array literal. ASCII only (non-ASCII becomes \\u escapes) and safe to place inside <script>."""
    return json.dumps(flows, ensure_ascii=True, indent=1).replace("</", "<\\/")


def _code_mask(js: str) -> list[bool]:
    """True where a character is real code, False inside a string, template literal or comment."""
    mask, i, n = [True] * len(js), 0, len(js)
    while i < n:
        if js[i] in "'\"`":
            quote, start = js[i], i
            i += 1
            while i < n and js[i] != quote:
                i += 2 if js[i] == "\\" else 1
            mask[start : i + 1] = [False] * (min(i + 1, n) - start)
            i += 1
        elif js.startswith("//", i):
            j = js.find("\n", i)
            j = n if j == -1 else j
            mask[i:j] = [False] * (j - i)
            i = j
        elif js.startswith("/*", i):
            j = js.find("*/", i)
            j = n if j == -1 else j + 2
            mask[i:j] = [False] * (min(j, n) - i)
            i = j
        else:
            i += 1
    return mask


def _find_flows(template: str) -> tuple[int, int]:
    """(start, end) of the real `const FLOWS = [ ... ]`, ignoring mentions in comments, strings and HTML."""
    for block in re.finditer(r"<script\b[^>]*>(.*?)</script\s*>", template, re.DOTALL | re.IGNORECASE):
        body, base = block.group(1), block.start(1)
        mask = _code_mask(body)
        for match in _FLOWS_START.finditer(body):
            if mask[match.start()]:
                return base + match.start(), _matching_bracket(template, base + match.end() - 1)
    raise ValueError("the template has no `const FLOWS = [` array to fill")


def render_guide(template: str, flows: list[dict[str, Any]], customer: str, logo_uri: str = TRANSPARENT_PNG) -> str:
    start, end = _find_flows(template)
    out = template[:start] + "const FLOWS = " + flows_js(flows) + template[end + 1 :]
    if logo_uri == TRANSPARENT_PNG:  # no logo supplied: leave out the empty chip rather than show a blank box
        out = re.sub(r'<span class="x">&times;</span>\s*<img src="\{\{LOGO_URI\}\}"[^>]*>', "", out)
    return out.replace("{{CUSTOMER}}", esc(customer)).replace("{{LOGO_URI}}", logo_uri)


def logo_data_uri(path: Path) -> str:
    kinds = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
    kind = kinds.get(path.suffix.lower())
    if kind is None:
        raise ValueError("the logo must be a .png, .jpg or .svg file")
    return f"data:{kind};base64," + base64.b64encode(path.read_bytes()).decode()
