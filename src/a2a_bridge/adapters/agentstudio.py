"""Boomi Agentstudio (Agent Garden) adapter: one A2A turn becomes one Execute API session call."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext
from a2a_bridge.adapters.session_store import InMemorySessionStore, SessionStore
from a2a_bridge.adapters.sse import parse_sse
from a2a_bridge.config import AgentstudioAdapterConfig

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _failed(message: str) -> AdapterResult:
    return AdapterResult(state=AdapterState.FAILED, error=message)


class AgentstudioAdapter(BaseAdapter):
    def __init__(
        self,
        config: AgentstudioAdapterConfig,
        session_store: SessionStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._sessions = session_store or InMemorySessionStore()
        self._client = client or httpx.AsyncClient()
        self._auth = httpx.BasicAuth(f"BOOMI_TOKEN.{config.username}", config.api_token)
        self._url = f"{config.execute_url.rstrip('/')}/execute/api/v1/{config.account_id}/session"

    async def execute(self, context: TaskContext) -> AdapterResult:
        last = _failed("Agent produced no result")
        async for result in self.stream(context):
            last = result
        return last

    async def stream(self, context: TaskContext) -> AsyncIterator[AdapterResult]:
        message = self._message(context)
        if isinstance(message, AdapterResult):
            yield message
            return
        body: dict[str, Any] = {"deployment_id": self.config.deployment_id, "message": message}
        session_id = await self._sessions.get(context.agent_slug, context.context_id)
        if session_id:
            body["session_id"] = session_id
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._client.stream(
                    "POST",
                    self._url,
                    json=body,
                    auth=self._auth,
                    headers={"Accept": "text/event-stream, application/json"},
                ) as response:
                    if not response.is_success:
                        await response.aread()
                        yield self._http_error(response)
                    elif "text/event-stream" in response.headers.get("content-type", ""):
                        async for result in self._consume_sse(response, context):
                            yield result
                    else:
                        await response.aread()
                        yield await self._structured_result(response, context)
        except TimeoutError:
            yield _failed(f"Agent did not finish within {self.config.timeout_seconds:g}s")
        except httpx.HTTPError as exc:
            logger.warning("agentstudio call failed: %s", type(exc).__name__)
            yield _failed("Agentstudio unreachable")

    def _message(self, context: TaskContext) -> str | dict[str, Any] | AdapterResult:
        """Conversational agents take text; structured agents take one JSON object."""
        if self.config.mode == "conversational":
            if context.text:
                return context.text
            if context.data:
                return json.dumps(context.data[0] if len(context.data) == 1 else context.data)
            return _failed("Message has no text or data")
        if len(context.data) == 1 and isinstance(context.data[0], dict):
            return context.data[0]
        parsed = _json(context.text) if context.text else None
        if isinstance(parsed, dict):
            return parsed
        return _failed("Structured agent needs exactly one JSON object (a data part, or text containing JSON)")

    def _http_error(self, response: httpx.Response) -> AdapterResult:
        status = response.status_code
        detail = response.text[:MAX_ERROR_CHARS].strip()
        if status in (401, 403):
            # The proxy's own Boomi credentials were refused; that is not the caller's problem to fix.
            return _failed(f"Agentstudio rejected the proxy's credentials (HTTP {status}); check account and token privileges")
        if status == 404:
            return _failed("Agentstudio deployment or session not found (HTTP 404)")
        return _failed(f"Agentstudio returned HTTP {status}" + (f": {detail}" if detail else ""))

    async def _remember(self, context: TaskContext, session_id: Any) -> None:
        if isinstance(session_id, str) and session_id:
            await self._sessions.set(context.agent_slug, context.context_id, session_id)

    async def _consume_sse(self, response: httpx.Response, context: TaskContext) -> AsyncIterator[AdapterResult]:
        replies: list[str] = []
        async for event, data in parse_sse(response.aiter_lines()):
            payload = _json(data)
            payload = payload if isinstance(payload, dict) else {}
            if event == "start":
                await self._remember(context, payload.get("session_id"))
            elif event == "progress_notification":
                if payload.get("type") == "ACTION" and payload.get("content"):
                    yield AdapterResult(state=AdapterState.WORKING, text=str(payload["content"]))
            elif event == "message":
                await self._remember(context, payload.get("session_id"))
                if payload.get("content"):
                    replies.append(str(payload["content"]))
            elif event == "error":
                yield _failed(str(payload.get("content") or payload.get("message") or data)[:MAX_ERROR_CHARS])
                return
            elif event == "[DONE]":
                break
        if not replies:
            yield _failed("Agent returned no message")
            return
        text = "\n".join(replies)
        blocked = (self.config.blocked_message or "").strip()
        if blocked and any(r.strip() == blocked for r in replies):
            yield AdapterResult(state=AdapterState.REJECTED, error=text)
            return
        yield AdapterResult(state=AdapterState.COMPLETED, text=text)

    async def _structured_result(self, response: httpx.Response, context: TaskContext) -> AdapterResult:
        payload = _json(response.text)
        if not isinstance(payload, dict):
            return _failed("Agentstudio returned an unreadable response")
        await self._remember(context, payload.get("session_id"))
        if payload.get("success") is False:
            detail = payload.get("error") or payload.get("message") or "agent reported failure"
            return _failed(str(detail)[:MAX_ERROR_CHARS])
        return AdapterResult(state=AdapterState.COMPLETED, data=payload.get("data"))

    async def aclose(self) -> None:
        await self._client.aclose()
