from __future__ import annotations

from a2a_bridge.adapters.agentstudio import AgentstudioAdapter
from a2a_bridge.adapters.base import BaseAdapter
from a2a_bridge.adapters.http_async import HttpAsyncAdapter
from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.adapters.mcp_bridge import McpAdapter
from a2a_bridge.adapters.openai_chat import OpenAIChatAdapter
from a2a_bridge.adapters.session_store import InMemorySessionStore, SessionStore
from a2a_bridge.config import AgentConfig


def create_adapter(agent: AgentConfig, session_store: SessionStore | None = None) -> BaseAdapter | None:
    """Adapter for an agent, or None when its adapter type is not implemented yet."""
    cfg = agent.adapter
    if cfg.type == "http_sync":
        return HttpSyncAdapter(cfg)
    if cfg.type == "http_async":
        return HttpAsyncAdapter(cfg)
    if cfg.type == "agentstudio":
        return AgentstudioAdapter(cfg, session_store or InMemorySessionStore())
    if cfg.type == "openai_chat":
        return OpenAIChatAdapter(cfg, session_store or InMemorySessionStore())
    if cfg.type == "mcp":
        return McpAdapter(cfg)
    return None
