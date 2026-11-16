"""FastAPI entrypoint: mounts every configured agent under /agents/{slug}."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.responses import JSONResponse
from starlette.routing import Mount

from a2a_bridge.adapters import create_adapter
from a2a_bridge.adapters.base import BaseAdapter
from a2a_bridge.config import AgentConfig, AppConfig, load_config
from a2a_bridge.core.card_builder import build_agent_card
from a2a_bridge.core.executor import BridgeExecutor
from a2a_bridge.security import InboundAuthMiddleware, validate_inbound

logger = logging.getLogger("a2a_bridge")

AdapterFactory = Callable[[AgentConfig], BaseAdapter | None]


def create_app(config: AppConfig, adapter_factory: AdapterFactory = create_adapter) -> FastAPI:
    validate_inbound(config.server.inbound_auth)
    engine = create_async_engine(config.server.database_url)
    task_store = DatabaseTaskStore(engine)
    adapters: list[BaseAdapter] = []
    mounts: list[Mount] = []
    mounted: list[AgentConfig] = []

    for agent in config.agents:
        adapter = adapter_factory(agent)
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
            mounts.append(Mount(mount_path, routes=routes))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await task_store.initialize()
        try:
            yield
        finally:
            for adapter in adapters:
                await adapter.aclose()
            await engine.dispose()

    app = FastAPI(title="a2a-bridge", lifespan=lifespan)
    app.add_middleware(InboundAuthMiddleware, auth=config.server.inbound_auth)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "agents": [a.slug for a in mounted]})

    app.router.routes.extend(mounts)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the a2a-bridge proxy")
    parser.add_argument("--config", default=os.environ.get("A2A_BRIDGE_CONFIG", "agents.yaml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
