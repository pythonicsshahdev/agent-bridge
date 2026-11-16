"""Outbound A2A client: call a remote A2A agent (another a2a-bridge, Boomi, or any A2A server)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.errors import A2AClientError, A2AClientTimeoutError, AgentCardResolutionError
from a2a.types.a2a_pb2 import AgentCard, CancelTaskRequest, GetTaskRequest, Message, SendMessageRequest, Task
from starlette.requests import Request

from a2a_bridge.config import RemoteAgentConfig, RemoteAuthConfig
from a2a_bridge.core.facade import TargetError, TargetInfo

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_MARGIN = 30.0


class ClientCredentialsToken:
    """OAuth 2.0 client-credentials access tokens, cached until shortly before they expire."""

    def __init__(self, auth: RemoteAuthConfig, http: httpx.AsyncClient, clock: Callable[[], float] = time.monotonic) -> None:
        self._auth, self._http, self._clock = auth, http, clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and self._clock() < self._expires_at - TOKEN_EXPIRY_MARGIN:
                return self._token
            form = {
                "grant_type": "client_credentials",
                "client_id": self._auth.client_id,
                "client_secret": self._auth.client_secret,
            }
            if self._auth.scope:
                form["scope"] = self._auth.scope
            if self._auth.audience:
                form["audience"] = self._auth.audience
            try:
                response = await self._http.post(self._auth.token_url, data=form, headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
                self._token = str(payload["access_token"])
                self._expires_at = self._clock() + float(payload.get("expires_in", 300))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                # Never include the response body or form: they may echo credentials.
                logger.warning("token request failed: %s", type(exc).__name__)
                raise TargetError(502, "could not obtain an access token for the remote agent") from exc
            return self._token


class RemoteTarget:
    """A remote A2A agent behind the facade. The card is fetched lazily and refreshed on a TTL."""

    def __init__(
        self,
        config: RemoteAgentConfig,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._http = http or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._clock = clock
        self._token = ClientCredentialsToken(config.auth, self._http, clock) if config.auth.mode == "oauth_client_credentials" else None
        self._card: AgentCard | None = None
        self._client = None
        self._card_expires = 0.0
        self._lock = asyncio.Lock()

    async def _resolved(self):
        async with self._lock:
            if self._client is not None and self._clock() < self._card_expires:
                return self._card, self._client
            parts = urlsplit(self.config.card_url)
            resolver = A2ACardResolver(self._http, f"{parts.scheme}://{parts.netloc}", agent_card_path=parts.path or "/")
            try:
                card = await resolver.get_agent_card(http_kwargs={"timeout": self.config.timeout_seconds})
                client = ClientFactory(ClientConfig(httpx_client=self._http, streaming=False)).create(card)
            except AgentCardResolutionError as exc:
                raise TargetError(502, f"could not read the remote agent card (HTTP {exc.status_code or 'error'})") from exc
            except (A2AClientError, httpx.HTTPError, ValueError) as exc:
                logger.warning("remote card unavailable for %s: %s", self.config.slug, type(exc).__name__)
                raise TargetError(502, "remote agent unreachable") from exc
            self._card, self._client = card, client
            self._card_expires = self._clock() + self.config.card_refresh_seconds
            return card, client

    async def _call_context(self, http: Request) -> ClientCallContext:
        auth, headers = self.config.auth, {}
        if auth.mode == "bearer_passthrough":
            value = http.headers.get(auth.header_name)
            if not value:
                raise TargetError(401, f"{auth.header_name} header required for this agent")
            headers[auth.header_name] = value
        elif auth.mode == "static_header":
            headers[auth.header_name] = auth.value or ""
        elif self._token is not None:
            headers["Authorization"] = f"Bearer {await self._token.get()}"
        return ClientCallContext(timeout=self.config.timeout_seconds, service_parameters=headers)

    @staticmethod
    def _failure(exc: Exception) -> TargetError:
        if isinstance(exc, A2AClientTimeoutError):
            return TargetError(504, "remote agent timed out")
        text = str(exc)
        if "HTTP Error 401" in text or "HTTP Error 403" in text:
            return TargetError(502, "remote agent rejected the credentials sent to it")
        if "HTTP Error 404" in text:
            return TargetError(404, "remote agent or task not found")
        logger.warning("remote call failed: %s", type(exc).__name__)
        return TargetError(502, "remote agent call failed")

    async def describe(self) -> TargetInfo:
        card, _ = await self._resolved()
        return TargetInfo(card.name, card.description, [(s.name, s.description) for s in card.skills])

    async def send(self, request: SendMessageRequest, http: Request) -> Task | Message:
        _, client = await self._resolved()
        context = await self._call_context(http)
        result: Task | Message | None = None
        try:
            async for event in client.send_message(request, context=context):
                if event.HasField("task"):
                    result = event.task
                elif event.HasField("message"):
                    result = event.message
        except (A2AClientError, httpx.HTTPError) as exc:
            raise self._failure(exc) from exc
        if result is None:
            raise TargetError(502, "remote agent returned nothing")
        return result

    async def get(self, task_id: str, http: Request) -> Task:
        _, client = await self._resolved()
        context = await self._call_context(http)
        try:
            return await client.get_task(GetTaskRequest(id=task_id), context=context)
        except (A2AClientError, httpx.HTTPError) as exc:
            raise self._failure(exc) from exc

    async def cancel(self, task_id: str, http: Request) -> Task:
        _, client = await self._resolved()
        context = await self._call_context(http)
        try:
            return await client.cancel_task(CancelTaskRequest(id=task_id), context=context)
        except (A2AClientError, httpx.HTTPError) as exc:
            raise self._failure(exc) from exc

    async def aclose(self) -> None:
        await self._http.aclose()
