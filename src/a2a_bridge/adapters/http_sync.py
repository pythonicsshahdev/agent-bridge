"""Synchronous REST adapter: one A2A task becomes one downstream HTTP call."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext
from a2a_bridge.adapters.templating import _MISSING, get_path, render_template
from a2a_bridge.config import HttpSyncAdapterConfig

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500
MAX_TEXT_CHARS = 100_000


def build_headers(config: HttpSyncAdapterConfig | Any, context: TaskContext) -> tuple[dict[str, str], str | None]:
    """Static headers plus auth. Returns (headers, error) where error means credentials are missing."""
    headers = dict(config.headers)
    auth = config.auth
    if auth.mode == "bearer_passthrough":
        incoming = context.headers.get(auth.header_name.lower())
        if not incoming:
            return headers, f"{auth.header_name} header required"
        headers[auth.header_name] = incoming
    elif auth.mode == "static_header":
        headers[auth.header_name] = auth.value or ""
    return headers, None


def build_payload(context: TaskContext, template: Any | None = None) -> Any:
    """A template, when configured, shapes the body. Otherwise a lone data part is sent as-is, else a small envelope."""
    if template is not None:
        return render_template(template, context)
    if len(context.data) == 1 and isinstance(context.data[0], dict) and not context.text:
        return context.data[0]
    return {
        "text": context.text,
        "data": context.data,
        "taskId": context.task_id,
        "contextId": context.context_id,
    }


def map_response(
    response: httpx.Response, text_path: str | None = None, data_path: str | None = None
) -> AdapterResult:
    status = response.status_code
    if 200 <= status < 300:
        if not response.content:
            return AdapterResult(state=AdapterState.COMPLETED)
        try:
            body = response.json()
            if text_path or data_path:
                text = get_path(body, text_path) if text_path else _MISSING
                data = get_path(body, data_path) if data_path else (body if text is _MISSING else _MISSING)
                return AdapterResult(
                    state=AdapterState.COMPLETED,
                    text=None if text is _MISSING else (text if isinstance(text, str) else json.dumps(text)),
                    data=None if data is _MISSING else data,
                )
            return AdapterResult(state=AdapterState.COMPLETED, data=body)
        except ValueError:
            return AdapterResult(state=AdapterState.COMPLETED, text=response.text[:MAX_TEXT_CHARS])
    detail = response.text[:MAX_ERROR_CHARS].strip()
    message = f"Downstream returned HTTP {status}" + (f": {detail}" if detail else "")
    if status in (401, 403):
        return AdapterResult(state=AdapterState.AUTH_REQUIRED, error=message)
    return AdapterResult(state=AdapterState.FAILED, error=message)


class HttpSyncAdapter(BaseAdapter):
    def __init__(self, config: HttpSyncAdapterConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def execute(self, context: TaskContext) -> AdapterResult:
        headers, missing = build_headers(self.config, context)
        if missing:
            return AdapterResult(state=AdapterState.AUTH_REQUIRED, error=missing)
        payload = build_payload(context, self.config.request_template)
        kwargs: dict[str, Any] = {"headers": headers, "timeout": self.config.timeout_seconds}
        if self.config.method in ("GET", "DELETE"):
            tpl = self.config.request_template
            if isinstance(payload, dict) and tpl is not None:
                kwargs["params"] = payload
            else:
                kwargs["params"] = {"text": context.text} if context.text else None
        else:
            kwargs["json"] = payload
        try:
            response = await self._client.request(self.config.method, self.config.url, **kwargs)
        except httpx.TimeoutException:
            return AdapterResult(
                state=AdapterState.FAILED, error=f"Downstream timed out after {self.config.timeout_seconds:g}s"
            )
        except httpx.HTTPError as exc:
            logger.warning("downstream call failed: %s", type(exc).__name__)
            return AdapterResult(state=AdapterState.FAILED, error="Downstream unreachable")
        return map_response(response, self.config.response_text_path, self.config.response_data_path)

    async def aclose(self) -> None:
        await self._client.aclose()
