"""FastAPI entrypoint: mounts every configured agent under /agents/{slug}."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import JSONResponse
from starlette.routing import Mount

from a2a_bridge.adapters import create_adapter
from a2a_bridge.adapters.base import BaseAdapter
from a2a_bridge.adapters.session_store import SessionStore, SqlSessionStore
from a2a_bridge.client import RemoteTarget
from a2a_bridge.config import AgentConfig, AppConfig, ConfigError, RemoteAgentConfig, load_config
from a2a_bridge.core.card_builder import build_agent_card
from a2a_bridge.core.executor import BridgeExecutor
from a2a_bridge.core.facade import LocalTarget, TargetInfo, build_facade_routes
from a2a_bridge.security import InboundAuthMiddleware, generate_key, validate_inbound

logger = logging.getLogger("a2a_bridge")

AdapterFactory = Callable[[AgentConfig, SessionStore], BaseAdapter | None]
RemoteFactory = Callable[[RemoteAgentConfig], RemoteTarget]


def create_app(
    config: AppConfig,
    adapter_factory: AdapterFactory = create_adapter,
    remote_factory: RemoteFactory = RemoteTarget,
) -> FastAPI:
    validate_inbound(config.server.inbound_auth)
    facade = config.server.facade
    if config.remote_agents and not facade.enabled:
        raise ConfigError("remote_agents need server.facade.enabled: true")
    inbound = config.server.inbound_auth
    base_url = config.server.base_url
    remotes: list[RemoteTarget] = []
    engine = create_async_engine(config.server.database_url)
    task_store = DatabaseTaskStore(engine)
    session_store = SqlSessionStore(engine)
    adapters: list[BaseAdapter] = []
    mounts: list[Mount] = []
    mounted: list[AgentConfig] = []

    for agent in config.agents:
        adapter = adapter_factory(agent, session_store)
        if adapter is None:
            logger.warning("agent '%s': adapter type '%s' is not implemented yet; skipping", agent.slug, agent.adapter.type)
            continue
        adapters.append(adapter)
        mounted.append(agent)
        executor = BridgeExecutor(adapter, agent.slug)
        is_root = config.root_agent is agent
        mount_paths = [f"/agents/{agent.slug}"] + ([""] if is_root else [])
        for mount_path in mount_paths:
            card = build_agent_card(agent, config.server.base_url, mount_path, config.server.inbound_auth)
            handler = DefaultRequestHandler(agent_executor=executor, task_store=task_store, agent_card=card)
            routes = create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url="/rpc", enable_v0_3_compat=True)
            if facade.enabled and mount_path:
                info = TargetInfo(agent.name, agent.description or agent.name, [(sk.name, sk.description) for sk in agent.skills])
                routes += build_facade_routes(agent.slug, LocalTarget(handler, info), facade, base_url, inbound)
            mounts.append(Mount(mount_path, routes=routes))

    for remote_config in config.remote_agents:
        remote = remote_factory(remote_config)
        remotes.append(remote)
        mounts.append(Mount(f"/agents/{remote_config.slug}", routes=build_facade_routes(remote_config.slug, remote, facade, base_url, inbound)))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await task_store.initialize()
        await session_store.initialize()
        try:
            yield
        finally:
            for adapter in adapters:
                await adapter.aclose()
            for remote in remotes:
                await remote.aclose()
            await engine.dispose()

    app = FastAPI(title="a2a-bridge", lifespan=lifespan)
    app.add_middleware(
        InboundAuthMiddleware,
        auth=config.server.inbound_auth,
        root_slug=config.root_agent.slug if config.root_agent else None,
    )
    base = config.server.base_url
    if base.startswith("http://") and not base.startswith(("http://localhost", "http://127.0.0.1")):
        logger.warning("public base URL %s is not HTTPS; credentials would travel in clear text", base)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Ready only if the database answers; the load balancer uses this, `/healthz` is liveness."""
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            logger.exception("readiness check failed")
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "agents": [a.slug for a in mounted], "remote_agents": [r.slug for r in config.remote_agents]})

    app.router.routes.extend(mounts)
    return app


class JsonFormatter(logging.Formatter):
    """One JSON object per line for CloudWatch Logs Insights. Audit records are merged in as fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name}
        message = record.getMessage()
        if record.name == "a2a_bridge.audit":
            try:
                entry.update(json.loads(message))
            except ValueError:
                entry["message"] = message
        else:
            entry["message"] = message
            if record.exc_info:
                entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging() -> None:
    """A2A_BRIDGE_LOG_FORMAT=json for structured logs; plain text otherwise."""
    handler = logging.StreamHandler()
    if os.environ.get("A2A_BRIDGE_LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logging.basicConfig(level=os.environ.get("A2A_BRIDGE_LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # the audit log already records every request


def keygen() -> None:
    key, digest = generate_key()
    print(f"key (give to the client, shown once): {key}")
    print(f"key_sha256 (put in the config):       {digest}")


def tool_spec(argv: list[str]) -> None:
    from pathlib import Path

    from a2a_bridge.toolspec import STYLES, write_tools

    parser = argparse.ArgumentParser(
        prog="a2a-bridge tool-spec",
        description="Write Boomi Agentstudio OpenAPI tool definitions for agents in a config. No credentials are written.",
    )
    parser.add_argument("--config", default=os.environ.get("A2A_BRIDGE_CONFIG", "agents.yaml"))
    parser.add_argument("--agent", action="append", help="slug to generate (repeatable); default: all local agents")
    parser.add_argument("--base-url", help="where the proxy is reachable; default: server.public_base_url")
    parser.add_argument("--style", choices=STYLES, default="json", help="json body, or query parameters (no JSON escaping concerns)")
    parser.add_argument("--out", default="tools", help="output directory")
    parser.add_argument("--no-task-tool", action="store_true", help="skip the check-task tool")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"a2a-bridge: configuration error: {exc}")
    agents = [a for a in config.agents if not args.agent or a.slug in args.agent]
    unknown = set(args.agent or []) - {a.slug for a in config.agents}
    if unknown or not agents:
        sys.exit(f"a2a-bridge: no such local agent: {', '.join(sorted(unknown)) or '(none selected)'}")
    base_url = args.base_url or config.server.base_url
    if base_url.startswith("http://localhost") or base_url.startswith("http://127."):
        print(f"warning: {base_url} is not reachable from Boomi; pass --base-url with your public address", file=sys.stderr)
    for path in write_tools(agents, base_url, Path(args.out), args.style, not args.no_task_tool):
        print(path)
    print(
        "\nNext, for each file (no secrets are in them):\n"
        "  agentstudio-tool.sh create --type OpenAPI --body <file>   then   install --type OpenAPI <id>\n"
        "Then set authentication in the Boomi console: a token_auth header with this proxy's key "
        f"(header {config.server.inbound_auth.header_name}). If the target agent uses identity: caller, also add its "
        "Boomi credentials header there; use a dedicated least-privilege Boomi token for that.",
        file=sys.stderr,
    )


def main() -> None:
    if sys.argv[1:2] == ["keygen"]:
        keygen()
        return
    if sys.argv[1:2] == ["tool-spec"]:
        tool_spec(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Run the a2a-bridge proxy (`a2a-bridge keygen` makes a client key)")
    parser.add_argument("--config", default=os.environ.get("A2A_BRIDGE_CONFIG", "agents.yaml"))
    args = parser.parse_args()
    configure_logging()
    try:
        config = load_config(args.config)
        app = create_app(config)
    except ConfigError as exc:
        sys.exit(f"a2a-bridge: configuration error: {exc}")
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        proxy_headers=True,
        forwarded_allow_ips=config.server.forwarded_allow_ips,
        server_header=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
