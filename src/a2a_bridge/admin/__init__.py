"""Read-only control plane: see how the proxy is configured, watch recent traffic, validate a config.

It never changes the running proxy and never reveals secrets. It is off unless `server.admin.enabled`,
and its API needs a caller with the explicit admin:* scope.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import yaml

from a2a_bridge.admin.graph import build_graph, lint, lint_agent, overview
from a2a_bridge.config import AppConfig, ConfigError, parse_config

STATIC = Path(__file__).parent / "static"
FONTS = STATIC / "fonts"
_FONT_NAME = re.compile(r"[a-z0-9-]+\.woff2")
MAX_VALIDATE_BYTES = 256 * 1024
# No inline script or style, no external origins, no framing: the page may only talk to this server.
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class EventBuffer(logging.Handler):
    """Keeps the most recent audit records in memory for the Events view. Audit lines never contain credentials."""

    def __init__(self, size: int) -> None:
        super().__init__(level=logging.INFO)
        self._events: deque[dict[str, Any]] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._events.append(json.loads(record.getMessage()))
        except ValueError:
            pass

    def recent(self, limit: int) -> list[dict[str, Any]]:
        return list(self._events)[-limit:][::-1]  # newest first

    def stats(self) -> dict[str, Any]:
        per_agent: dict[str, dict[str, Any]] = defaultdict(lambda: {"requests": 0, "errors": 0, "ms": 0, "last": None})
        statuses: Counter[str] = Counter()
        callers: Counter[str] = Counter()
        window = 0
        for e in self._events:
            if e.get("agent") == "admin":
                continue  # the console polling itself is not traffic
            window += 1
            row = per_agent[e.get("agent") or "-"]
            row["requests"] += 1
            row["errors"] += 1 if (e.get("status") or 0) >= 400 else 0
            row["ms"] += e.get("ms") or 0
            row["last"] = e.get("ts")
            statuses[str(e.get("status"))] += 1
            callers[e.get("principal") or "unauthenticated"] += 1
        agents = {
            slug: {"requests": r["requests"], "errors": r["errors"], "avg_ms": round(r["ms"] / r["requests"]) if r["requests"] else 0, "last": r["last"]}
            for slug, r in per_agent.items()
        }
        return {"window": window, "agents": agents, "statuses": dict(statuses), "callers": dict(callers.most_common(10))}


def _diff(running: AppConfig, proposed: AppConfig) -> dict[str, list[str]]:
    old = {a.slug: a.adapter.type for a in running.agents} | {r.slug: "remote" for r in running.remote_agents}
    new = {a.slug: a.adapter.type for a in proposed.agents} | {r.slug: "remote" for r in proposed.remote_agents}
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "adapter_changed": sorted(s for s in set(old) & set(new) if old[s] != new[s]),
    }


_ENV_NAME = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
MAX_AGENT_BYTES = 64 * 1024


def preview_agent(candidate: Any, config: AppConfig) -> dict[str, Any]:
    """Check one agent definition and render it as YAML. Reports which ${ENV} names are set where this proxy
    runs (true/false only, never values), because the agent may be deployed somewhere else."""
    if not isinstance(candidate, dict):
        return {"ok": False, "errors": ["the agent must be an object"]}
    text = yaml.safe_dump({"agents": [candidate]}, sort_keys=False, default_flow_style=False, allow_unicode=True)
    names = sorted(set(_ENV_NAME.findall(text)))
    # Unset variables must not fail the check: this proxy is not necessarily where the agent will run.
    env = {**os.environ, **{n: "placeholder" for n in names if n not in os.environ}}
    try:
        parsed = parse_config(text, env)
    except ConfigError as exc:
        return {"ok": False, "errors": [line.removeprefix("agents.0.") for line in str(exc).split("; ") if line]}
    agent = parsed.agents[0]
    rendered = yaml.safe_dump([candidate], sort_keys=False, default_flow_style=False, allow_unicode=True)
    return {
        "ok": True,
        "yaml": rendered,
        "slug": agent.slug,
        "slug_taken": any(a.slug == agent.slug for a in config.agents) or any(r.slug == agent.slug for r in config.remote_agents),
        "env": [{"name": n, "set": n in os.environ} for n in names],
        "findings": lint_agent(agent),
    }


def build_admin_routes(config: AppConfig, mounted: set[str], events: EventBuffer) -> list[Route]:
    started = time.time()

    def page(name: str, media_type: str):
        body = (STATIC / name).read_bytes()

        async def handler(_: Request) -> Response:
            return Response(body, media_type=media_type, headers=SECURITY_HEADERS)

        return handler

    def api(payload) -> JSONResponse:
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    async def get_overview(_: Request) -> JSONResponse:
        return api(overview(config, mounted, started, time.time()))

    async def get_diagram(_: Request) -> JSONResponse:
        return api(build_graph(config, mounted))

    async def get_events(request: Request) -> JSONResponse:
        try:
            limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        return api({"events": events.recent(limit)})

    async def get_stats(_: Request) -> JSONResponse:
        return api(events.stats())

    async def validate(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_VALIDATE_BYTES:
            return JSONResponse({"error": "config too large"}, status_code=413)
        try:
            text = json.loads(body)["yaml"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'send {"yaml": "<config text>"}'}, status_code=422)
        try:
            proposed = parse_config(text, os.environ)
        except ConfigError as exc:
            return api({"ok": False, "errors": [line for line in str(exc).split("; ") if line]})
        return api(
            {
                "ok": True,
                "summary": {
                    "agents": [{"slug": a.slug, "adapter": a.adapter.type} for a in proposed.agents],
                    "remote_agents": [r.slug for r in proposed.remote_agents],
                    "inbound_mode": proposed.server.inbound_auth.mode,
                },
                "changes_vs_running": _diff(config, proposed),
                "findings": lint(proposed),
            }
        )

    async def agent_preview(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > MAX_AGENT_BYTES:
            return JSONResponse({"error": "agent definition too large"}, status_code=413)
        try:
            candidate = json.loads(body)["agent"]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'send {"agent": {...}}'}, status_code=422)
        return api(preview_agent(candidate, config))

    async def font(request: Request) -> Response:
        name = request.path_params["name"]
        path = FONTS / name
        if not _FONT_NAME.fullmatch(name) or not path.is_file():  # only bundled .woff2 files, never an arbitrary path
            return Response(status_code=404)
        return Response(path.read_bytes(), media_type="font/woff2", headers={**SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"})

    return [
        Route("/admin/fonts/{name}", font),
        Route("/admin", page("index.html", "text/html; charset=utf-8")),
        Route("/admin/", page("index.html", "text/html; charset=utf-8")),
        Route("/admin/app.js", page("app.js", "text/javascript; charset=utf-8")),
        Route("/admin/app.css", page("app.css", "text/css; charset=utf-8")),
        Route("/admin/api/overview", get_overview),
        Route("/admin/api/diagram", get_diagram),
        Route("/admin/api/events", get_events),
        Route("/admin/api/stats", get_stats),
        Route("/admin/api/validate", validate, methods=["POST"]),
        Route("/admin/api/agent/preview", agent_preview, methods=["POST"]),
    ]


def validate_admin(config: AppConfig) -> None:
    """The admin plane must be reachable only with an explicit admin scope, which needs real authentication."""
    if not config.server.admin.enabled:
        return
    mode = config.server.inbound_auth.mode
    if mode in ("none", "bearer_passthrough"):
        raise ConfigError(f"server.admin.enabled needs inbound auth that can grant scopes (api_key or jwt), not '{mode}'")
