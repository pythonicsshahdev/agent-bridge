"""Inbound access control: API keys, OAuth 2.0 / OIDC JWTs, scopes, rate limits, audit.

The agent card and /healthz stay public so clients can discover agents. Everything else
authenticates a Principal, authorizes it for the requested agent, and runs as that principal,
so tasks are isolated per caller by the SDK's owner scoping.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError
from starlette.authentication import AuthCredentials, BaseUser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from a2a_bridge.config import ConfigError, InboundAuthConfig, JwtConfig

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("a2a_bridge.audit")

PUBLIC_SUFFIXES = ("/.well-known/agent-card.json", "/openapi.json")  # discovery documents, no secrets
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/admin", "/admin/", "/admin/app.js", "/admin/app.css"})  # the UI shell holds no data
SCOPE_PREFIX = "agents:"
ADMIN_SCOPE = "admin:*"
ADMIN_PREFIX = "/admin"
MAX_CREDENTIAL_CHARS = 8192


class AuthError(Exception):
    """Credentials missing or invalid. `reason` is for the audit log only, never sent to the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthUnavailable(Exception):
    """The identity provider could not be reached; the caller should retry."""


@dataclass(frozen=True)
class Principal:
    id: str  # stable owner id: `key:<client>`, `jwt:<sub>`, `passthrough`, `anonymous`
    method: str
    scopes: frozenset[str]

    def can_access(self, slug: str) -> bool:
        return f"{SCOPE_PREFIX}*" in self.scopes or f"{SCOPE_PREFIX}{slug}" in self.scopes

    @property
    def is_admin(self) -> bool:
        """Admin is never implied by agent scopes; it must be granted explicitly."""
        return ADMIN_SCOPE in self.scopes


class PrincipalUser(BaseUser):
    """Starlette user whose name becomes the SDK's task owner."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.principal.id


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> tuple[str, str]:
    """A new random API key and its SHA-256, which is all that goes in the config."""
    key = "a2a_" + secrets.token_urlsafe(32)
    return key, hash_key(key)


class ApiKeyAuthenticator:
    def __init__(self, config: InboundAuthConfig, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._entries: list[tuple[str, str, frozenset[str], datetime | None]] = []
        for client in config.clients:
            for digest in client.key_sha256:
                self._entries.append((digest, client.id, frozenset(client.scopes), client.expires_at))
        for index, key in enumerate(config.api_keys, start=1):
            self._entries.append((hash_key(key), f"key-{index}", frozenset({f"{SCOPE_PREFIX}*"}), None))

    def authenticate(self, supplied: str) -> Principal:
        if not supplied or len(supplied) > MAX_CREDENTIAL_CHARS:
            raise AuthError("missing or oversized api key")
        digest = hash_key(supplied)
        match = None
        for stored, client_id, scopes, expires in self._entries:  # no early exit: constant work per request
            if hmac.compare_digest(digest, stored):
                match = (client_id, scopes, expires)
        if match is None:
            raise AuthError("unknown api key")
        client_id, scopes, expires = match
        if expires is not None and self._now() >= expires:
            raise AuthError(f"api key for client {client_id} expired")
        return Principal(id=f"key:{client_id}", method="api_key", scopes=scopes)


class JwtAuthenticator:
    """Validates signature, issuer, audience, expiry and not-before. Asymmetric algorithms only."""

    def __init__(self, config: JwtConfig, key_resolver: Callable[[str], Any] | None = None) -> None:
        self._config = config
        if key_resolver is None:
            client = PyJWKClient(config.jwks_url, cache_keys=True, lifespan=config.jwks_cache_seconds, timeout=5)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._resolve = key_resolver

    async def authenticate(self, token: str) -> Principal:
        if not token or len(token) > MAX_CREDENTIAL_CHARS:
            raise AuthError("missing or oversized token")
        try:
            key = await asyncio.to_thread(self._resolve, token)
        except PyJWKClientConnectionError as exc:
            raise AuthUnavailable("jwks endpoint unreachable") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"no usable signing key: {type(exc).__name__}") from exc
        cfg = self._config
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(cfg.algorithms),
                audience=cfg.audience,
                issuer=cfg.issuer,
                leeway=cfg.leeway_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {type(exc).__name__}") from exc
        subject = claims.get("sub") or claims.get("azp") or claims.get("client_id")
        if not subject:
            raise AuthError("token has no sub, azp or client_id")
        return Principal(id=f"jwt:{subject}", method="jwt", scopes=frozenset(self._scopes(claims)))

    def _scopes(self, claims: dict[str, Any]) -> list[str]:
        raw = claims.get(self._config.scope_claim, claims.get("scp", []))
        values = raw.split() if isinstance(raw, str) else [str(v) for v in raw] if isinstance(raw, list) else []
        return [v for v in values if v.startswith(SCOPE_PREFIX) or v == ADMIN_SCOPE]


class TokenBucket:
    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """0 when allowed, otherwise seconds until a token is available."""
        now = self._clock()
        tokens, last = self._state.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens >= 1:
            self._state[key] = (tokens - 1, now)
            return 0.0
        self._state[key] = (tokens, now)
        return (1 - tokens) / self._rate


def validate_inbound(auth: InboundAuthConfig) -> None:
    """Fail fast at startup rather than serve a proxy that cannot authenticate anyone."""
    uses_keys = auth.mode in ("api_key", "api_key_or_jwt")
    uses_jwt = auth.mode in ("jwt", "api_key_or_jwt")
    if uses_keys and not (auth.clients or any(auth.api_keys)):
        raise ConfigError(
            f"server.inbound_auth.mode is {auth.mode} but no clients or api_keys are set; "
            "add a client (see `a2a-bridge keygen`) or choose mode: none explicitly"
        )
    if uses_keys and any(not k for k in auth.api_keys):
        raise ConfigError("server.inbound_auth.api_keys contains an empty key")
    if uses_jwt and auth.jwt is None:
        raise ConfigError(f"server.inbound_auth.mode is {auth.mode} but server.inbound_auth.jwt is not configured")
    if auth.mode == "none":
        logger.warning("inbound auth is DISABLED: anyone who can reach this proxy can call its agents")


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.endswith(PUBLIC_SUFFIXES) or path.startswith("/admin/fonts/")


def _is_admin_path(path: str) -> bool:
    return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


def _agent_slug(path: str, root_slug: str | None) -> str | None:
    if _is_admin_path(path):
        return None  # authorized by the admin scope, not an agent scope
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "agents":
        return parts[1]
    return root_slug


class _BodyTooLarge(Exception):
    pass


class InboundAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        auth: InboundAuthConfig,
        root_slug: str | None = None,
        known_slugs: set[str] | None = None,
        key_authenticator: ApiKeyAuthenticator | None = None,
        jwt_authenticator: JwtAuthenticator | None = None,
    ) -> None:
        validate_inbound(auth)
        self.app, self.auth, self.root_slug = app, auth, root_slug
        self.known_slugs = known_slugs
        uses_keys = auth.mode in ("api_key", "api_key_or_jwt")
        uses_jwt = auth.mode in ("jwt", "api_key_or_jwt")
        self._keys = key_authenticator or (ApiKeyAuthenticator(auth) if uses_keys else None)
        self._jwt = jwt_authenticator or (JwtAuthenticator(auth.jwt) if uses_jwt and auth.jwt else None)
        self._header = auth.header_name.lower().encode()
        self._limiter = TokenBucket(auth.rate_limit.requests_per_minute, auth.rate_limit.burst) if auth.rate_limit else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:12]
        slug = _agent_slug(scope["path"], self.root_slug)
        label = "admin" if _is_admin_path(scope["path"]) else slug  # what the audit log calls the target
        status = {"code": 0}

        async def send_logged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        principal: Principal | None = None
        try:
            principal = await self._authenticate(dict(scope["headers"]))
        except AuthError as exc:
            await self._reject(scope, receive, send_logged, 401, "authentication required", {"WWW-Authenticate": "Bearer"})
            self._audit(request_id, None, label, scope, status["code"], started, exc.reason)
            return
        except AuthUnavailable as exc:
            await self._reject(scope, receive, send_logged, 503, "identity provider unavailable", {"Retry-After": "5"})
            self._audit(request_id, None, label, scope, status["code"], started, str(exc))
            return

        if _is_admin_path(scope["path"]) and not principal.is_admin:
            await self._reject(scope, receive, send_logged, 403, "admin scope required")
            self._audit(request_id, principal, label, scope, status["code"], started, "admin scope denied")
            return
        if slug is not None and not principal.can_access(slug):
            await self._reject(scope, receive, send_logged, 403, "not permitted for this agent")
            self._audit(request_id, principal, label, scope, status["code"], started, "scope denied")
            return
        if self._limiter is not None:
            wait = self._limiter.take(principal.id)
            if wait > 0:
                await self._reject(scope, receive, send_logged, 429, "rate limit exceeded", {"Retry-After": str(int(wait) + 1)})
                self._audit(request_id, principal, label, scope, status["code"], started, "rate limited")
                return

        scope["user"] = PrincipalUser(principal)
        scope["auth"] = AuthCredentials(sorted(principal.scopes))
        limit = self.auth.max_request_bytes
        length = dict(scope["headers"]).get(b"content-length")
        if length and length.isdigit() and int(length) > limit:
            await self._reject(scope, receive, send_logged, 413, "request too large")
            self._audit(request_id, principal, label, scope, status["code"], started, "body too large")
            return

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send_logged)
        except _BodyTooLarge:
            if not status["code"]:
                await self._reject(scope, receive, send_logged, 413, "request too large")
        self._audit(request_id, principal, label, scope, status["code"], started, None)

    async def _authenticate(self, headers: dict[bytes, bytes]) -> Principal:
        mode = self.auth.mode
        if mode == "none":
            return Principal("anonymous", "none", frozenset({f"{SCOPE_PREFIX}*"}))
        bearer = self._bearer(headers)
        if mode == "bearer_passthrough":
            if not bearer:
                raise AuthError("bearer token required")
            return Principal("passthrough", "bearer_passthrough", frozenset({f"{SCOPE_PREFIX}*"}))
        key = headers.get(self._header, b"").decode("latin-1")
        if self._keys is not None and key:
            return self._keys.authenticate(key)
        if self._jwt is not None and bearer:
            return await self._jwt.authenticate(bearer)
        raise AuthError("no credentials presented")

    @staticmethod
    def _bearer(headers: dict[bytes, bytes]) -> str:
        value = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = value.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, code: int, error: str, headers: dict[str, str] | None = None) -> None:
        await JSONResponse({"error": error}, status_code=code, headers=headers)(scope, receive, send)

    @staticmethod
    def _audit(request_id: str, principal: Principal | None, slug: str | None, scope: Scope, code: int, started: float, note: str | None) -> None:
        audit_logger.info(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "request_id": request_id,
                    "client": scope["client"][0] if scope.get("client") else None,
                    "principal": principal.id if principal else None,
                    "auth": principal.method if principal else None,
                    "agent": slug,
                    "path": scope["path"],
                    "status": code,
                    "ms": round((time.monotonic() - started) * 1000),
                    "note": note,
                }
            )
        )
