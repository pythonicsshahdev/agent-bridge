"""OpenAI-compatible chat adapter: one A2A turn becomes one /chat/completions call with the running history."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext
from a2a_bridge.adapters.http_sync import MAX_TEXT_CHARS, build_headers, map_response
from a2a_bridge.adapters.session_store import InMemorySessionStore, SessionStore
from a2a_bridge.config import OpenAIChatAdapterConfig

logger = logging.getLogger(__name__)


def completions_url(url: str) -> str:
    base = url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def user_text(context: TaskContext) -> str:
    """The text of the turn. Structured parts with no text are sent as JSON so nothing is silently dropped."""
    if context.text:
        return context.text
    return "\n".join(json.dumps(d) for d in context.data)


def reply_text(content: Any) -> str:
    """`message.content` is a string, or a list of {type: text, text} parts on some servers."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type", "text") == "text")
    return ""


class OpenAIChatAdapter(BaseAdapter):
    def __init__(
        self,
        config: OpenAIChatAdapterConfig,
        session_store: SessionStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._store = session_store or InMemorySessionStore()
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._url = completions_url(config.url)

    async def execute(self, context: TaskContext) -> AdapterResult:
        cfg = self.config
        headers, missing = build_headers(cfg, context)
        if missing:
            return AdapterResult(state=AdapterState.AUTH_REQUIRED, error=missing)
        turn = user_text(context)
        if not turn:
            return AdapterResult(state=AdapterState.INPUT_REQUIRED, text="Send a message to start the conversation.")
        history = (
            await self._store.get_history(context.agent_slug, context.principal, context.context_id)
            if cfg.max_history_messages
            else []
        )
        messages = [*history, {"role": "user", "content": turn}]
        body = {**cfg.parameters, "model": cfg.model, "messages": messages, "stream": False}
        try:
            response = await self._client.post(self._url, json=body, headers=headers, timeout=cfg.timeout_seconds)
        except httpx.TimeoutException:
            return AdapterResult(state=AdapterState.FAILED, error=f"Model timed out after {cfg.timeout_seconds:g}s")
        except httpx.HTTPError as exc:
            logger.warning("chat call failed: %s", type(exc).__name__)
            return AdapterResult(state=AdapterState.FAILED, error="Chat endpoint unreachable")
        if not 200 <= response.status_code < 300:
            return map_response(response)
        try:
            choice = response.json()["choices"][0]
            text = reply_text(choice["message"].get("content"))
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return AdapterResult(state=AdapterState.FAILED, error="Chat endpoint returned an unexpected response")
        if finish == "content_filter":
            return AdapterResult(state=AdapterState.REJECTED, error="The model's content filter blocked this turn")
        if not text:
            return AdapterResult(state=AdapterState.FAILED, error="Chat endpoint returned no text")
        text = text[:MAX_TEXT_CHARS]
        if cfg.max_history_messages:
            kept = [*messages, {"role": "assistant", "content": text}][-cfg.max_history_messages :]
            while kept and kept[0]["role"] != "user":  # never start a window on an orphaned reply
                kept = kept[1:]
            await self._store.set_history(context.agent_slug, context.principal, context.context_id, kept)
        return AdapterResult(state=AdapterState.COMPLETED, text=text)

    async def aclose(self) -> None:
        await self._client.aclose()
