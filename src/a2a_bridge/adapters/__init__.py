from __future__ import annotations

from a2a_bridge.adapters.base import BaseAdapter
from a2a_bridge.adapters.http_sync import HttpSyncAdapter
from a2a_bridge.config import AgentConfig


def create_adapter(agent: AgentConfig) -> BaseAdapter | None:
    """Adapter for an agent, or None when its adapter type is not implemented yet."""
    if agent.adapter.type == "http_sync":
        return HttpSyncAdapter(agent.adapter)
    return None
