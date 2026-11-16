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


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
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
            if not scope.startswith("agents:") or not _SLUG_OR_STAR.match(scope.removeprefix("agents:")):
                raise ValueError(f"scope '{scope}' must be 'agents:<slug>' or 'agents:*'")
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


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    database_url: str = "sqlite+aiosqlite:///./a2a_bridge.db"
    # Externally reachable base URL advertised in agent cards. Falls back to host:port.
    public_base_url: str | None = None
    inbound_auth: InboundAuthConfig = Field(default_factory=InboundAuthConfig)

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

    _url = field_validator("url")(lambda cls, v: _check_http_url(v))


class HttpAsyncAdapterConfig(_Model):
    """Trigger a job (usually 202 + job id), then poll a status endpoint."""

    type: Literal["http_async"]
    url: str
    method: _HTTP_METHODS = "POST"
    timeout_seconds: float = Field(30, gt=0, le=600)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    job_id_path: str = "job_id"  # dotted path into the trigger response body
    status_url: str  # must contain {job_id}
    status_method: Literal["GET", "POST"] = "GET"
    status_path: str = "status"  # dotted path into the status response body
    result_path: str | None = None  # dotted path to the result; whole body if unset
    completed_values: list[str] = Field(default_factory=lambda: ["completed", "success", "done"])
    failed_values: list[str] = Field(default_factory=lambda: ["failed", "error"])
    poll_interval_seconds: float = Field(2.0, gt=0)
    max_poll_seconds: float = Field(300.0, gt=0)

    _url = field_validator("url", "status_url")(lambda cls, v: _check_http_url(v))

    @field_validator("status_url")
    @classmethod
    def _has_job_id(cls, v: str) -> str:
        if "{job_id}" not in v:
            raise ValueError("status_url must contain the {job_id} placeholder")
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


AdapterConfig = Annotated[
    HttpSyncAdapterConfig | HttpAsyncAdapterConfig | AgentstudioAdapterConfig, Field(discriminator="type")
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


class AppConfig(_Model):
    server: ServerConfig = Field(default_factory=ServerConfig)
    agents: list[AgentConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_slugs(self) -> AppConfig:
        slugs = [a.slug for a in self.agents]
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
        auth = adapter.get("auth") or {}
        value = auth.get("value")
        if auth.get("mode") == "static_header" and isinstance(value, str) and not _ENV_REF.search(value):
            slug = agent.get("slug", "?")
            raise ConfigError(f"agent '{slug}': auth.value must reference an environment variable, e.g. ${{MY_KEY}}")


def parse_config(text: str, env: Mapping[str, str] | None = None) -> AppConfig:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping with an 'agents' list")
    _reject_literal_credentials(raw)
    try:
        return AppConfig.model_validate(expand_env(raw, env))
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_config(text, env)
