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
    OpenIdConnectSecurityScheme,
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
    _add_security(card, agent, inbound)
    return card


def _scheme_api_key(name: str, description: str) -> SecurityScheme:
    return SecurityScheme(
        api_key_security_scheme=APIKeySecurityScheme(location="header", name=name, description=description)
    )


def _add_security(card: AgentCard, agent: AgentConfig, inbound: InboundAuthConfig | None) -> None:
    """Advertise how to call this agent. Inbound methods are alternatives; downstream needs apply to each."""
    schemes: dict[str, SecurityScheme] = {}
    alternatives: list[list[str]] = [[]]
    uses_jwt = inbound is not None and inbound.mode in ("jwt", "api_key_or_jwt")
    if inbound is not None:
        inbound_options: list[str] = []
        if inbound.mode in ("api_key", "api_key_or_jwt"):
            schemes["apiKey"] = _scheme_api_key(inbound.header_name, "Proxy access key.")
            inbound_options.append("apiKey")
        if uses_jwt and inbound.jwt is not None:
            if inbound.jwt.discovery_url:
                schemes["oidc"] = SecurityScheme(
                    open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                        description="OAuth 2.0 / OIDC access token.", open_id_connect_url=inbound.jwt.discovery_url
                    )
                )
            else:
                schemes["oidc"] = SecurityScheme(
                    http_auth_security_scheme=HTTPAuthSecurityScheme(
                        scheme="Bearer", bearer_format="JWT", description="OAuth 2.0 access token."
                    )
                )
            inbound_options.append("oidc")
        if inbound.mode == "bearer_passthrough":
            schemes["bearer"] = SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer", description="Bearer token.")
            )
            inbound_options.append("bearer")
        if inbound_options:
            alternatives = [[name] for name in inbound_options]

    downstream: list[str] = []
    adapter = agent.adapter
    auth = getattr(adapter, "auth", None)
    if auth is not None and auth.mode == "bearer_passthrough" and not uses_jwt and "bearer" not in schemes:
        # With JWT inbound the same Authorization header is forwarded downstream, so no extra scheme.
        schemes["bearer"] = SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="Bearer", description="Forwarded to the downstream service."
            )
        )
        downstream.append("bearer")
    if adapter.type == "agentstudio" and adapter.identity == "caller":
        schemes["boomiCredentials"] = _scheme_api_key(
            adapter.caller_credentials_header, "Your Boomi credentials: Basic base64(BOOMI_TOKEN.<user>:<api token>)."
        )
        downstream.append("boomiCredentials")

    for name, scheme in schemes.items():
        card.security_schemes[name].CopyFrom(scheme)
    for option in alternatives:
        names = option + downstream
        if names:
            card.security_requirements.append(SecurityRequirement(schemes={n: StringList() for n in names}))


def card_to_dict(card: AgentCard) -> dict[str, Any]:
    """JSON-ready dict, as served at /.well-known/agent-card.json."""
    return agent_card_to_dict(card)
