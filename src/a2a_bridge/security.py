"""Inbound access control for the proxy. The agent card stays public so clients can discover it."""

from __future__ import annotations

import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from a2a_bridge.config import ConfigError, InboundAuthConfig

logger = logging.getLogger(__name__)

PUBLIC_SUFFIXES = ("/.well-known/agent-card.json",)
PUBLIC_PATHS = frozenset({"/healthz"})


def validate_inbound(auth: InboundAuthConfig) -> None:
    """Fail fast at startup rather than serve a proxy that cannot authenticate anyone."""
    if auth.mode == "api_key":
        if not auth.api_keys or any(not k for k in auth.api_keys):
            raise ConfigError(
                "server.inbound_auth.mode is api_key but no non-empty api_keys are set; "
                "set e.g. api_keys: ['${A2A_BRIDGE_API_KEY}'] or choose mode: none explicitly"
            )
    elif auth.mode == "none":
        logger.warning("inbound auth is DISABLED: anyone who can reach this proxy can call its agents")


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.endswith(PUBLIC_SUFFIXES)


class InboundAuthMiddleware:
    def __init__(self, app: ASGIApp, auth: InboundAuthConfig) -> None:
        validate_inbound(auth)
        self.app = app
        self.auth = auth
        self._header = auth.header_name.lower().encode()
        self._keys = [k.encode() for k in auth.api_keys]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.auth.mode == "none" or _is_public(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        error = self._check(headers)
        if error:
            response = JSONResponse({"error": error}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, headers: dict[bytes, bytes]) -> str | None:
        if self.auth.mode == "api_key":
            supplied = headers.get(self._header, b"")
            # Compare against every key so timing does not reveal which one matched.
            matched = [hmac.compare_digest(supplied, key) for key in self._keys]
            return None if any(matched) else f"missing or invalid {self.auth.header_name}"
        # bearer_passthrough: the downstream service validates the token; we only require one.
        value = headers.get(b"authorization", b"")
        if value[:7].lower() == b"bearer " and value[7:].strip():
            return None
        return "Authorization: Bearer token required"
