"""Read-only descriptions of the running configuration for the control plane and the field guide.

Everything here is safe to show to an administrator: hostnames only (URL paths can carry secrets), no key
hashes, no tokens, no header values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from a2a_bridge import __version__
from a2a_bridge.config import AgentConfig, AppConfig, RemoteAgentConfig

MAX_CALLER_NODES = 6


def host_of(url: str | None) -> str | None:
    return urlsplit(url).hostname if url else None


def describe_adapter(agent: AgentConfig) -> dict[str, Any]:
    a = agent.adapter
    info: dict[str, Any] = {"type": a.type, "target": host_of(a.url if a.type != "agentstudio" else a.execute_url)}
    if a.type in ("http_sync", "http_async"):
        info.update(method=a.method, auth=a.auth.mode)
    if a.type == "http_async":
        info.update(poll_seconds=a.poll_interval_seconds, max_seconds=a.max_poll_seconds, cancellable=bool(a.cancel_url))
    if a.type == "agentstudio":
        info.update(mode=a.mode, identity=a.identity, fallback=a.allow_service_account_fallback, guardrail=bool(a.blocked_message))
    return info


def describe_remote(remote: RemoteAgentConfig) -> dict[str, Any]:
    return {"slug": remote.slug, "target": host_of(remote.card_url), "auth": remote.auth.mode}


def describe_inbound(config: AppConfig) -> dict[str, Any]:
    auth = config.server.inbound_auth
    now = datetime.now(UTC)
    clients = [
        {
            "id": c.id,
            "scopes": c.scopes,
            "keys": len(c.key_sha256),
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "expired": bool(c.expires_at and c.expires_at <= now),
        }
        for c in auth.clients
    ]
    jwt = None
    if auth.jwt is not None:
        jwt = {
            "issuer": auth.jwt.issuer,
            "audience": auth.jwt.audience,
            "algorithms": auth.jwt.algorithms,
            "jwks_host": host_of(auth.jwt.jwks_url),
            "scope_claim": auth.jwt.scope_claim,
        }
    return {
        "mode": auth.mode,
        "header": auth.header_name,
        "clients": clients,
        "simple_keys": len(auth.api_keys),
        "jwt": jwt,
        "rate_limit": auth.rate_limit.model_dump() if auth.rate_limit else None,
        "max_request_bytes": auth.max_request_bytes,
    }


def agent_callers(config: AppConfig) -> list[Any]:
    """Clients that may call at least one agent (admin-only clients are managers, not callers)."""
    return [c for c in config.server.inbound_auth.clients if any(sc.startswith("agents:") for sc in c.scopes)]


def lint(config: AppConfig) -> list[dict[str, str]]:
    """Findings an operator should see. `warn` is worth fixing before production; `info` is context."""
    found: list[dict[str, str]] = []
    server, auth = config.server, config.server.inbound_auth

    def add(level: str, message: str) -> None:
        found.append({"level": level, "message": message})

    if auth.mode == "none":
        add("warn", "Inbound authentication is disabled: anyone who can reach the proxy can call every agent.")
    if auth.mode == "bearer_passthrough":
        add("info", "Inbound mode bearer_passthrough only requires some bearer token; the backend must validate it.")
    for client in auth.clients:
        if "agents:*" in client.scopes and len(config.agents) + len(config.remote_agents) > 1:
            add("warn", f"Client '{client.id}' can call every agent (agents:*). Scope it to the agents it needs.")
        if client.expires_at is None:
            add("info", f"Client '{client.id}' has no expiry; consider setting expires_at and rotating keys.")
        elif client.expires_at <= datetime.now(UTC):
            add("warn", f"Client '{client.id}' has expired and can no longer authenticate.")
    if auth.api_keys:
        add("info", f"{len(auth.api_keys)} simple api_keys entry(ies) grant full access; prefer scoped clients.")
    if auth.rate_limit is None:
        add("warn", "Rate limiting is disabled.")
    base = server.base_url
    if base.startswith("http://") and not base.startswith(("http://localhost", "http://127.0.0.1")):
        add("warn", f"The public base URL {base} is not HTTPS, so credentials would travel in clear text.")
    if server.forwarded_allow_ips == "*":
        add("info", "X-Forwarded-* is trusted from any peer (forwarded_allow_ips: '*'); safe only if the app port is reachable solely from your load balancer.")
    if server.admin.enabled:
        add("info", "The admin control plane is enabled; keep admin:* keys to a small set of people.")
    for agent in config.agents:
        a = agent.adapter
        if a.type == "agentstudio" and a.identity == "service_account":
            add("info", f"Agent '{agent.slug}' runs as one shared Boomi service account; Boomi audit shows a single identity.")
        if a.type == "agentstudio" and a.identity == "caller" and a.allow_service_account_fallback:
            add("info", f"Agent '{agent.slug}' falls back to a shared service account when a caller sends no Boomi credentials.")
        url = a.execute_url if a.type == "agentstudio" else a.url
        if url.startswith("http://"):
            add("warn", f"Agent '{agent.slug}' calls its backend over plain HTTP.")
        if a.type in ("http_sync", "http_async") and a.auth.mode == "none":
            add("info", f"Agent '{agent.slug}' sends no credentials to its backend.")
    return found


def build_graph(config: AppConfig, mounted: set[str]) -> dict[str, Any]:
    """Nodes and edges for the picture: callers -> proxy -> agents -> backends."""
    auth = config.server.inbound_auth
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def node(id_: str, kind: str, label: str, sub: str = "", **detail: Any) -> None:
        nodes.append({"id": id_, "kind": kind, "label": label, "sub": sub, "detail": detail})

    def edge(src: str, dst: str, label: str = "", style: str = "solid") -> None:
        edges.append({"from": src, "to": dst, "label": label, "style": style})

    sub = f"sign-in: {auth.mode}" + (f" · {auth.rate_limit.requests_per_minute}/min" if auth.rate_limit else "")
    node("proxy", "proxy", "a2a-bridge", sub, base_url=config.server.base_url, facade=config.server.facade.enabled, sign_in=auth.mode)

    caller_ids: list[str] = []
    callers = agent_callers(config)  # an admin-only client never calls an agent, so it is not drawn as a caller
    for client in callers[:MAX_CALLER_NODES]:
        cid = f"caller:{client.id}"
        node(cid, "caller", client.id, "API key · " + ", ".join(client.scopes), scopes=client.scopes, keys=len(client.key_sha256))
        caller_ids.append(cid)
    if len(callers) > MAX_CALLER_NODES:
        node("caller:more", "caller", f"+{len(callers) - MAX_CALLER_NODES} more clients")
        caller_ids.append("caller:more")
    if auth.jwt is not None:
        node("caller:oidc", "caller", "OIDC callers", f"issuer {host_of(auth.jwt.issuer) or auth.jwt.issuer}")
        caller_ids.append("caller:oidc")
    if not caller_ids:
        node("caller:any", "caller", "A2A clients", f"auth: {auth.mode}")
        caller_ids.append("caller:any")
    for cid in caller_ids:
        edge(cid, "proxy", "A2A / REST")

    for agent in config.agents:
        info = describe_adapter(agent)
        live = agent.slug in mounted
        sub = info["type"] + (f" · {info['mode']}" if info.get("mode") else "")
        node(f"agent:{agent.slug}", "agent", agent.name, sub, slug=agent.slug, skills=[s.name for s in agent.skills], live=live, adapter=info)
        edge("proxy", f"agent:{agent.slug}", f"/agents/{agent.slug}")
        backend = f"backend:{agent.slug}"
        label = {"agentstudio": "Boomi Agentstudio", "http_async": "Async job API", "http_sync": "REST / Boomi listener"}[info["type"]]
        node(backend, "backend", label, info["target"] or "", **info)
        edge(f"agent:{agent.slug}", backend, info["type"])
    for remote in config.remote_agents:
        info = describe_remote(remote)
        node(f"remote:{remote.slug}", "remote", remote.slug, f"remote A2A · {info['target']}", **info)
        edge("proxy", f"remote:{remote.slug}", "facade /invoke", "dashed")
    return {"nodes": nodes, "edges": edges}


def overview(config: AppConfig, mounted: set[str], started: float, now: float) -> dict[str, Any]:
    return {
        "version": __version__,
        "uptime_seconds": int(now - started),
        "public_base_url": config.server.base_url,
        "facade": config.server.facade.model_dump(),
        "inbound": describe_inbound(config),
        "agents": [
            {
                "slug": a.slug,
                "name": a.name,
                "description": a.description,
                "version": a.version,
                "live": a.slug in mounted,
                "adapter": describe_adapter(a),
                "skills": [{"id": s.id, "name": s.name, "description": s.description} for s in a.skills],
                "card_url": f"{config.server.base_url}/agents/{a.slug}/.well-known/agent-card.json",
                "openapi_url": f"{config.server.base_url}/agents/{a.slug}/openapi.json",
            }
            for a in config.agents
        ],
        "remote_agents": [describe_remote(r) for r in config.remote_agents],
        "findings": lint(config),
    }
