"""Build compliant A2A AgentCards from agent config."""

from __future__ import annotations

from typing import Any

from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    APIKeySecurityScheme,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

from a2a_bridge.config import AgentConfig, InboundAuthConfig

# The A2A spec requires a card description, and skill description and tags; fall back rather than emit a non-compliant card.
DEFAULT_MODES = ["text/plain", "application/json"]


def build_agent_card(
    agent: AgentConfig,
    base_url: str,
    mount_path: str | None = None,
    inbound: InboundAuthConfig | None = None,
) -> AgentCard:
    """Card for one agent. `mount_path` defaults to /agents/{slug}; pass "" for the root default agent."""
    mount = f"/agents/{agent.slug}" if mount_path is None else mount_path.rstrip("/")
    card = AgentCard(
        name=agent.name,
        description=agent.description or agent.name,
        version=agent.version,
        supported_interfaces=[
            AgentInterface(
                url=f"{base_url.rstrip('/')}{mount}/rpc",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=list(DEFAULT_MODES),
        default_output_modes=list(DEFAULT_MODES),
        skills=[
            AgentSkill(
                id=s.id, name=s.name, description=s.description or s.name, tags=s.tags or [s.id], examples=s.examples
            )
            for s in agent.skills
        ],
    )
    # One requirement listing every scheme the caller must satisfy together.
    required: list[str] = []
    if inbound is not None and inbound.mode == "api_key":
        card.security_schemes["apiKey"].CopyFrom(
            SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(
                    location="header", name=inbound.header_name, description="Proxy access key."
                )
            )
        )
        required.append("apiKey")
    downstream = getattr(agent.adapter, "auth", None)  # agentstudio adapters authenticate with their own service account
    passes_bearer = downstream is not None and downstream.mode == "bearer_passthrough"
    if passes_bearer or (inbound is not None and inbound.mode == "bearer_passthrough"):
        # The caller's bearer token is forwarded downstream, so clients must send one.
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer", description="Forwarded to the downstream service."
                )
            )
        )
        required.append("bearer")
    if required:
        card.security_requirements.append(SecurityRequirement(schemes={name: StringList() for name in required}))
    return card


def card_to_dict(card: AgentCard) -> dict[str, Any]:
    """JSON-ready dict, as served at /.well-known/agent-card.json."""
    return agent_card_to_dict(card)
