"""Declarative config for a2a-bridge: Pydantic models, ${ENV} expansion, YAML loading."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_HTTP_METHODS = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class ConfigError(ValueError):
    """Raised when agents.yaml cannot be loaded, expanded or validated."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_http_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    return value


_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_TEMPLATE_ROOTS = ("text", "context_id", "task_id", "data")


def _check_template(value: Any) -> Any:
    """Reject placeholders the adapters cannot fill, so a typo fails at startup and not on the first call."""
    unknown: set[str] = set()

    def scan(node: Any) -> None:
        if isinstance(node, str):
            unknown.update(n for n in _TEMPLATE_PLACEHOLDER.findall(node) if n.split(".")[0] not in _TEMPLATE_ROOTS)
        elif isinstance(node, dict):
            for k, v in node.items():
                scan(k)
                scan(v)
        elif isinstance(node, list):
            for v in node:
                scan(v)

    scan(value)
    if unknown:
        raise ValueError(f"unknown placeholder(s) {sorted(unknown)}; use text, context_id, task_id, data")
    return value


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_HEADER = re.compile(r"authorization|api[-_]?key|token|secret|password|cookie", re.IGNORECASE)
_SLUG_OR_STAR = re.compile(r"^(\*|[a-z0-9][a-z0-9-]*)$")


class ClientConfig(_Model):
    """A machine client of the proxy. Only the SHA-256 of its key is configured, never the key.

    List several hashes to rotate: add the new one, move clients over, then remove the old one.
    Generate a key and hash with `a2a-bridge keygen`.
    """

    id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    key_sha256: list[str] = Field(min_length=1)
    scopes: list[str] = Field(default_factory=lambda: ["agents:*"])  # agents:<slug> or agents:*
    expires_at: datetime | None = None

    @field_validator("key_sha256", mode="before")
    @classmethod
    def _one_or_many(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @field_validator("key_sha256")
    @classmethod
    def _hex_digests(cls, v: list[str]) -> list[str]:
        v = [h.strip().lower() for h in v]
        if any(not _SHA256_HEX.match(h) for h in v):
            raise ValueError("key_sha256 entries must be 64-character hex SHA-256 digests (see `a2a-bridge keygen`)")
        return v

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        for scope in v:
            if scope == "admin:*":
                continue
            if not scope.startswith("agents:") or not _SLUG_OR_STAR.match(scope.removeprefix("agents:")):
                raise ValueError(f"scope '{scope}' must be 'agents:<slug>', 'agents:*' or 'admin:*'")
        return v


class JwtConfig(_Model):
    """Validate OAuth 2.0 / OIDC access tokens (JWTs) from your identity provider."""

    issuer: str = Field(min_length=1)
    audience: str | list[str]
    jwks_url: str
    discovery_url: str | None = None  # OIDC discovery document, advertised in agent cards
    algorithms: list[Literal["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]] = Field(
        default_factory=lambda: ["RS256"], min_length=1
    )  # asymmetric only: HS* and `none` are never accepted
    leeway_seconds: int = Field(30, ge=0, le=300)
    scope_claim: str = "scope"  # space-delimited string, or a list (also reads `scp`)
    jwks_cache_seconds: int = Field(3600, ge=60)

    _urls = field_validator("jwks_url", "discovery_url")(lambda cls, v: v if v is None else _check_http_url(v))


class RateLimitConfig(_Model):
    requests_per_minute: int = Field(120, ge=1)
    burst: int = Field(30, ge=1)


class InboundAuthConfig(_Model):
    """Who may call this proxy. Defaults to API keys; `none` must be chosen explicitly."""

    mode: Literal["api_key", "jwt", "api_key_or_jwt", "bearer_passthrough", "none"] = "api_key"
    header_name: str = "X-API-Key"
    clients: list[ClientConfig] = Field(default_factory=list)
    api_keys: list[str] = Field(default_factory=list)  # simple form: each must reference ${ENV}; full access
    jwt: JwtConfig | None = None
    rate_limit: RateLimitConfig | None = Field(default_factory=RateLimitConfig)
    max_request_bytes: int = Field(1_048_576, ge=1024)

    @model_validator(mode="after")
    def _clients_unique(self) -> InboundAuthConfig:
        ids = [c.id for c in self.clients]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate client ids: {dupes}")
        return self


class FacadeConfig(_Model):
    """REST facade over A2A, so tools that can only call one HTTP endpoint (e.g. an Agentstudio
    OpenAPI tool) can reach any agent: POST /agents/{slug}/invoke, GET /agents/{slug}/tasks/{id}."""

    enabled: bool = True
    default_wait_seconds: float = Field(60, ge=0, le=900)
    max_wait_seconds: float = Field(300, gt=0, le=900)


class AdminConfig(_Model):
    """Read-only control plane at /admin. Off by default; needs a client with the admin:* scope."""

    enabled: bool = False
    event_buffer: int = Field(500, ge=50, le=5000)  # recent requests kept in memory for the Events view


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    database_url: str = "sqlite+aiosqlite:///./a2a_bridge.db"
    # Which peers may set X-Forwarded-For/Proto (comma-separated IPs, or "*"). Behind a load balancer use "*"
    # only if the security group admits nothing but the load balancer.
    forwarded_allow_ips: str = "127.0.0.1"
    # Externally reachable base URL advertised in agent cards. Falls back to host:port.
    public_base_url: str | None = None
    inbound_auth: InboundAuthConfig = Field(default_factory=InboundAuthConfig)
    facade: FacadeConfig = Field(default_factory=FacadeConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

    @field_validator("public_base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else _check_http_url(v).rstrip("/")

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url
        host = "localhost" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"


class AuthConfig(_Model):
    """Downstream auth. Credentials are never literal: static values must come from ${ENV}."""

    mode: Literal["none", "bearer_passthrough", "static_header"] = "none"
    header_name: str = "Authorization"
    value: str | None = None

    @model_validator(mode="after")
    def _value_matches_mode(self) -> AuthConfig:
        if self.mode == "static_header" and not self.value:
            raise ValueError("auth.value is required when mode is static_header")
        if self.mode != "static_header" and self.value is not None:
            raise ValueError("auth.value is only allowed when mode is static_header")
        return self


class HttpSyncAdapterConfig(_Model):
    type: Literal["http_sync"]
    url: str
    method: _HTTP_METHODS = "POST"
    timeout_seconds: float = Field(30, gt=0, le=600)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Optional body shape for frameworks that expect their own input (LangServe, CrewAI, n8n, ...).
    # Placeholders: {{text}}, {{context_id}}, {{task_id}}, {{data}}, {{data.0.field}}.
    request_template: Any | None = None
    response_text_path: str | None = None  # dotted path to the reply text in a JSON response
    response_data_path: str | None = None  # dotted path to a structured result; whole body if unset

    _url = field_validator("url")(lambda cls, v: _check_http_url(v))
    _template = field_validator("request_template")(lambda cls, v: _check_template(v))


class HttpAsyncAdapterConfig(_Model):
    """Trigger a job (usually 202 + job id), then poll a status endpoint."""

    type: Literal["http_async"]
    url: str
    method: _HTTP_METHODS = "POST"
    timeout_seconds: float = Field(30, gt=0, le=600)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    request_template: Any | None = None  # same placeholders as http_sync
    job_id_path: str = "job_id"  # dotted path into the trigger response body
    status_url: str  # must contain {job_id}
    status_method: Literal["GET", "POST"] = "GET"
    status_path: str = "status"  # dotted path into the status response body
    result_path: str | None = None  # dotted path to the result; whole body if unset
    error_path: str | None = None  # dotted path to a failure reason in the status body
    cancel_url: str | None = None  # must contain {job_id}; called when the A2A task is canceled or times out
    cancel_method: Literal["POST", "DELETE"] = "DELETE"
    max_poll_errors: int = Field(3, ge=1, le=20)  # consecutive failed polls tolerated before giving up
    completed_values: list[str] = Field(default_factory=lambda: ["completed", "success", "done"])
    failed_values: list[str] = Field(default_factory=lambda: ["failed", "error"])
    poll_interval_seconds: float = Field(2.0, gt=0)
    max_poll_seconds: float = Field(300.0, gt=0)

    _template = field_validator("request_template")(lambda cls, v: _check_template(v))
    _url = field_validator("url", "status_url", "cancel_url")(lambda cls, v: v if v is None else _check_http_url(v))

    @field_validator("status_url", "cancel_url")
    @classmethod
    def _has_job_id(cls, v: str | None) -> str | None:
        if v is not None and "{job_id}" not in v:
            raise ValueError("status_url and cancel_url must contain the {job_id} placeholder")
        return v


class AgentstudioAdapterConfig(_Model):
    """Invoke a deployed Boomi Agentstudio (Agent Garden) agent through the Execute API.

    The Execute API uses HTTP Basic with a Boomi API token. By default each caller supplies their own
    (`identity: caller`), so Boomi RBAC and audit apply per person. A shared service account is optional.
    """

    type: Literal["agentstudio"]
    deployment_id: str = Field(min_length=1)
    execute_url: str  # region host, e.g. https://c01-usa-east.ai-agent-garden.boomi.com
    account_id: str = Field(min_length=1)
    identity: Literal["caller", "service_account"] = "caller"
    # Caller identity: the caller sends `Basic base64(BOOMI_TOKEN.<user>:<token>)` in this header.
    caller_credentials_header: str = "X-Boomi-Authorization"
    allow_service_account_fallback: bool = False  # identity=caller: use the service account when no header is sent
    username: str | None = None  # service account (identity=service_account or fallback)
    api_token: str | None = None  # service account token; must reference ${ENV}
    mode: Literal["conversational", "structured"] = "conversational"
    timeout_seconds: float = Field(120, gt=0, le=900)
    blocked_message: str | None = None  # the agent's guardrail message; matching replies become `rejected`

    _url = field_validator("execute_url")(lambda cls, v: _check_http_url(v))

    @model_validator(mode="after")
    def _identity_credentials(self) -> AgentstudioAdapterConfig:
        needs_service = self.identity == "service_account" or self.allow_service_account_fallback
        has_service = bool(self.username and self.api_token)
        if needs_service and not has_service:
            raise ValueError("username and api_token are required for a service account")
        if not needs_service and (self.username or self.api_token):
            raise ValueError(
                "username/api_token are set but unused: identity is 'caller' with no service-account fallback"
            )
        return self


class OpenAIChatAdapterConfig(_Model):
    """Any OpenAI-compatible chat endpoint (LiteLLM, Ollama, vLLM, LangGraph servers, hosted APIs).

    The proxy keeps the running conversation per caller and A2A context and sends it with each turn.
    It adds no prompts of its own: the system prompt belongs to the service behind the endpoint.
    """

    type: Literal["openai_chat"]
    url: str  # base URL ending in /v1, or the full .../chat/completions URL
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(120, gt=0, le=900)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    parameters: dict[str, Any] = Field(default_factory=dict)  # passed through, e.g. temperature, max_tokens
    max_history_messages: int = Field(20, ge=0, le=200)  # 0 = every turn stands alone

    _url = field_validator("url")(lambda cls, v: _check_http_url(v))

    @field_validator("parameters")
    @classmethod
    def _no_reserved(cls, v: dict[str, Any]) -> dict[str, Any]:
        reserved = sorted({"messages", "model", "stream"} & set(v))
        if reserved:
            raise ValueError(f"{reserved} are set by the proxy and cannot be overridden here")
        return v


class McpAdapterConfig(_Model):
    """An MCP server (streamable HTTP) exposed as an A2A agent. Only the tools listed here can be called."""

    type: Literal["mcp"]
    url: str  # the server's MCP endpoint
    tools: list[str] = Field(min_length=1)  # allow-list; nothing else is callable
    default_tool: str | None = None  # used when the message does not name a tool; optional with one tool
    text_argument: str | None = None  # argument that receives the message text when there is no data part
    timeout_seconds: float = Field(60, gt=0, le=600)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)

    _url = field_validator("url")(lambda cls, v: _check_http_url(v))

    @model_validator(mode="after")
    def _default_is_allowed(self) -> McpAdapterConfig:
        if self.default_tool and self.default_tool not in self.tools:
            raise ValueError("default_tool must be one of tools")
        return self


AdapterConfig = Annotated[
    HttpSyncAdapterConfig | HttpAsyncAdapterConfig | AgentstudioAdapterConfig | OpenAIChatAdapterConfig | McpAdapterConfig,
    Field(discriminator="type"),
]


class SkillConfig(_Model):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentConfig(_Model):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    description: str = ""
    version: str = "1.0.0"
    adapter: AdapterConfig
    skills: list[SkillConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_skill_ids(self) -> AgentConfig:
        ids = [s.id for s in self.skills]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate skill ids: {dupes}")
        return self


class RemoteAuthConfig(_Model):
    """How the proxy authenticates to a remote A2A agent."""

    mode: Literal["none", "bearer_passthrough", "static_header", "oauth_client_credentials"] = "none"
    header_name: str = "Authorization"
    value: str | None = None  # static_header; must reference ${ENV}
    token_url: str | None = None  # oauth_client_credentials
    client_id: str | None = None
    client_secret: str | None = None  # must reference ${ENV}
    scope: str | None = None
    audience: str | None = None

    @model_validator(mode="after")
    def _fields_match_mode(self) -> RemoteAuthConfig:
        if self.mode == "static_header" and not self.value:
            raise ValueError("auth.value is required for static_header")
        if self.mode == "oauth_client_credentials":
            missing = [f for f in ("token_url", "client_id", "client_secret") if not getattr(self, f)]
            if missing:
                raise ValueError(f"oauth_client_credentials requires: {', '.join(missing)}")
            _check_http_url(self.token_url or "")
        return self


class RemoteAgentConfig(_Model):
    """An external A2A agent (another a2a-bridge, Boomi, or any A2A server) reachable through the facade."""

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    card_url: str
    allow_insecure_http: bool = False  # plain http only for local testing
    auth: RemoteAuthConfig = Field(default_factory=RemoteAuthConfig)
    timeout_seconds: float = Field(30, gt=0, le=600)  # per HTTP call, not per task
    card_refresh_seconds: float = Field(300, ge=10)

    @model_validator(mode="after")
    def _https(self) -> RemoteAgentConfig:
        _check_http_url(self.card_url)
        urls = [self.card_url] + ([self.auth.token_url] if self.auth.token_url else [])
        if not self.allow_insecure_http and any(not u.startswith("https://") for u in urls):
            raise ValueError("card_url and token_url must be https:// (set allow_insecure_http for local testing)")
        return self


class AppConfig(_Model):
    server: ServerConfig = Field(default_factory=ServerConfig)
    agents: list[AgentConfig] = Field(min_length=1)
    remote_agents: list[RemoteAgentConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_slugs(self) -> AppConfig:
        slugs = [a.slug for a in self.agents] + [r.slug for r in self.remote_agents]
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        if dupes:
            raise ValueError(f"duplicate agent slugs: {dupes}")
        return self

    @property
    def root_agent(self) -> AgentConfig | None:
        """The agent served at the root when exactly one is configured."""
        return self.agents[0] if len(self.agents) == 1 else None


def expand_env(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings. Unset vars without a default raise."""
    env = os.environ if env is None else env
    missing: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):

            def sub(m: re.Match[str]) -> str:
                name, default = m.group(1), m.group(2)
                if name in env:
                    return env[name]
                if default is not None:
                    return default
                if name not in missing:
                    missing.append(name)
                return m.group(0)

            return _ENV_REF.sub(sub, node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(value)
    if missing:
        raise ConfigError(f"unset environment variables referenced in config: {', '.join(missing)}")
    return result


def _reject_literal_credentials(raw: Any) -> None:
    """Zero-trust rule: a static_header auth value must reference ${ENV}, never embed a secret."""
    if not isinstance(raw, dict):
        return
    keys = (((raw.get("server") or {}).get("inbound_auth") or {}).get("api_keys")) or []
    for key in keys:
        if isinstance(key, str) and not _ENV_REF.search(key):
            raise ConfigError("server.inbound_auth.api_keys entries must reference an environment variable, e.g. ${A2A_BRIDGE_API_KEY}")
    for agent in raw.get("agents") or []:
        adapter = (agent or {}).get("adapter") or {}
        token = adapter.get("api_token")
        if adapter.get("type") == "agentstudio" and isinstance(token, str) and not _ENV_REF.search(token):
            raise ConfigError(f"agent '{agent.get('slug', '?')}': api_token must reference an environment variable, e.g. ${{BOOMI_API_TOKEN}}")
        for name, header_value in (adapter.get("headers") or {}).items():
            if _SENSITIVE_HEADER.search(str(name)) and isinstance(header_value, str) and not _ENV_REF.search(header_value):
                raise ConfigError(
                    f"agent '{agent.get('slug', '?')}': header '{name}' looks like a credential, so its value must reference an environment variable, e.g. ${{MY_KEY}}"
                )
        auth = adapter.get("auth") or {}
        value = auth.get("value")
        if auth.get("mode") == "static_header" and isinstance(value, str) and not _ENV_REF.search(value):
            slug = agent.get("slug", "?")
            raise ConfigError(f"agent '{slug}': auth.value must reference an environment variable, e.g. ${{MY_KEY}}")


def _reject_literal_remote_secrets(raw: dict) -> None:
    for remote in raw.get("remote_agents") or []:
        auth = (remote or {}).get("auth") or {}
        for field in ("value", "client_secret"):
            value = auth.get(field)
            needed = (field == "value" and auth.get("mode") == "static_header") or field == "client_secret"
            if needed and isinstance(value, str) and not _ENV_REF.search(value):
                raise ConfigError(
                    f"remote agent '{remote.get('slug', '?')}': auth.{field} must reference an environment variable, e.g. ${{MY_SECRET}}"
                )


def describe_errors(exc: ValidationError) -> str:
    """One line per problem, naming the field and the rule, never echoing the offending value
    (it may be a pasted secret) and never including pydantic's documentation URLs."""
    lines = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in error["loc"]) or "config"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {message}")
    return "; ".join(lines)


def parse_config(text: str, env: Mapping[str, str] | None = None) -> AppConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping with an 'agents' list")
    _reject_literal_credentials(raw)
    _reject_literal_remote_secrets(raw)
    try:
        return AppConfig.model_validate(expand_env(raw, env))
    except ValidationError as exc:
        raise ConfigError(describe_errors(exc)) from exc


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_config(text, env)
