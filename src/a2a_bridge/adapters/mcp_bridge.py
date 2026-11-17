"""MCP bridge: call tools on an MCP server (streamable HTTP) as A2A tasks.

Each task opens a short MCP session (initialize, tools/call, close). Only tools named in the config
can be called, and the bridge never lists or exposes the rest of the server.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext
from a2a_bridge.adapters.http_sync import MAX_ERROR_CHARS, MAX_TEXT_CHARS, build_headers
from a2a_bridge.adapters.sse import parse_sse
from a2a_bridge.config import McpAdapterConfig

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"


class McpFailure(Exception):
    def __init__(self, state: AdapterState, message: str) -> None:
        super().__init__(message)
        self.result = AdapterResult(state=state, error=message)


def _http_failure(response: httpx.Response, what: str) -> McpFailure:
    detail = response.text[:MAX_ERROR_CHARS].strip()
    message = f"{what} returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
    state = AdapterState.AUTH_REQUIRED if response.status_code in (401, 403) else AdapterState.FAILED
    return McpFailure(state, message)


class McpAdapter(BaseAdapter):
    def __init__(self, config: McpAdapterConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    def select_call(self, context: TaskContext) -> tuple[str, dict[str, Any]]:
        """Which tool and arguments this message asks for. Raises McpFailure when it cannot say."""
        cfg = self.config
        tool: str | None = None
        arguments: dict[str, Any] = {}
        if len(context.data) == 1 and isinstance(context.data[0], dict):
            part = context.data[0]
            if isinstance(part.get("tool"), str):
                tool = part["tool"]
                given = part.get("arguments", {})
                if not isinstance(given, dict):
                    raise McpFailure(AdapterState.REJECTED, "'arguments' must be an object")
                arguments = given
            else:
                arguments = part
        elif context.text and cfg.text_argument:
            arguments = {cfg.text_argument: context.text}
        elif context.text or context.data:
            raise McpFailure(
                AdapterState.REJECTED,
                'Send one data part such as {"tool": "name", "arguments": {...}}'
                + (" or plain text" if cfg.text_argument else ""),
            )
        if context.text and cfg.text_argument and cfg.text_argument not in arguments:
            arguments = {**arguments, cfg.text_argument: context.text}
        tool = tool or cfg.default_tool or (cfg.tools[0] if len(cfg.tools) == 1 else None)
        if tool is None:
            raise McpFailure(AdapterState.REJECTED, f"Name a tool. Available: {', '.join(cfg.tools)}")
        if tool not in cfg.tools:
            raise McpFailure(AdapterState.REJECTED, f"Tool '{tool[:80]}' is not available through this agent")
        return tool, arguments

    async def _rpc(self, headers: dict[str, str], payload: dict[str, Any]) -> tuple[Any, httpx.Headers]:
        """POST one JSON-RPC message. Returns (result, response headers); notifications return (None, headers)."""
        wanted = payload.get("id")
        async with self._client.stream(
            "POST", self.config.url, json=payload, headers=headers, timeout=self.config.timeout_seconds
        ) as response:
            if not 200 <= response.status_code < 300:
                await response.aread()
                raise _http_failure(response, "MCP server")
            if wanted is None:
                return None, response.headers
            message: Any = None
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                async for _event, data in parse_sse(response.aiter_lines()):
                    try:
                        candidate = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and candidate.get("id") == wanted:
                        message = candidate
                        break
            else:
                try:
                    message = json.loads(await response.aread())
                except ValueError:
                    message = None
            if not isinstance(message, dict):
                raise McpFailure(AdapterState.FAILED, "MCP server sent no usable response")
            if "error" in message:
                err = message["error"] if isinstance(message["error"], dict) else {}
                raise McpFailure(AdapterState.FAILED, f"MCP error: {str(err.get('message', 'unknown'))[:MAX_ERROR_CHARS]}")
            return message.get("result"), response.headers

    async def execute(self, context: TaskContext) -> AdapterResult:
        headers, missing = build_headers(self.config, context)
        if missing:
            return AdapterResult(state=AdapterState.AUTH_REQUIRED, error=missing)
        try:
            tool, arguments = self.select_call(context)
        except McpFailure as exc:
            return exc.result
        base = {**headers, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        session_headers = dict(base)
        session_id: str | None = None
        try:
            _, init_headers = await self._rpc(
                base,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "a2a-bridge", "version": "1"},
                    },
                },
            )
            session_id = init_headers.get("mcp-session-id")
            session_headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            if session_id:
                session_headers["Mcp-Session-Id"] = session_id
            await self._rpc(session_headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            result, _ = await self._rpc(
                session_headers,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            )
        except McpFailure as exc:
            return exc.result
        except httpx.TimeoutException:
            return AdapterResult(state=AdapterState.FAILED, error=f"MCP server timed out after {self.config.timeout_seconds:g}s")
        except httpx.HTTPError as exc:
            logger.warning("mcp call failed: %s", type(exc).__name__)
            return AdapterResult(state=AdapterState.FAILED, error="MCP server unreachable")
        finally:
            if session_id:
                try:
                    await self._client.delete(self.config.url, headers=session_headers, timeout=5)
                except httpx.HTTPError:
                    pass  # closing is best effort
        return self._map_result(result)

    @staticmethod
    def _map_result(result: Any) -> AdapterResult:
        if not isinstance(result, dict):
            return AdapterResult(state=AdapterState.FAILED, error="MCP server returned an unexpected result")
        content = result.get("content")
        text = "\n".join(
            p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )[:MAX_TEXT_CHARS] if isinstance(content, list) else ""
        if result.get("isError"):
            return AdapterResult(state=AdapterState.FAILED, error=(text or "The tool reported an error")[:MAX_ERROR_CHARS])
        structured = result.get("structuredContent")
        return AdapterResult(state=AdapterState.COMPLETED, text=text or None, data=structured)

    async def aclose(self) -> None:
        await self._client.aclose()
