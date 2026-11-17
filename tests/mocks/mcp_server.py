"""Stand-in for an MCP server over streamable HTTP, with three tools: add, echo, divide (errors on zero).

Run:  uvicorn --app-dir tests/mocks mcp_server:app --port 9015
Answers as JSON by default, or as an event stream when MOCK_MCP_SSE=1. Tools not listed here return a JSON-RPC error.
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import FastAPI, Request, Response

app = FastAPI(title="mock-mcp")
SESSIONS: set[str] = set()
SECRET_TOOLS = {"drop_database": lambda a: "gone"}  # exists on the server but must never be reachable through the bridge


def call_tool(name: str, args: dict) -> dict | None:
    if name == "add":
        total = args["a"] + args["b"]
        return {"content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}}
    if name == "echo":
        return {"content": [{"type": "text", "text": str(args.get("message", ""))}]}
    if name == "divide":
        if args.get("b") == 0:
            return {"isError": True, "content": [{"type": "text", "text": "division by zero"}]}
        return {"content": [{"type": "text", "text": str(args["a"] / args["b"])}]}
    if name in SECRET_TOOLS:
        return {"content": [{"type": "text", "text": SECRET_TOOLS[name](args)}]}
    return None


def reply(body: dict, result=None, error=None, headers=None) -> Response:
    payload = {"jsonrpc": "2.0", "id": body["id"], **({"error": error} if error else {"result": result})}
    if os.environ.get("MOCK_MCP_SSE") == "1":
        return Response(f"event: message\ndata: {json.dumps(payload)}\n\n", media_type="text/event-stream", headers=headers)
    return Response(json.dumps(payload), media_type="application/json", headers=headers)


@app.post("/mcp")
async def rpc(request: Request) -> Response:
    token = os.environ.get("MOCK_MCP_TOKEN")
    if token and request.headers.get("authorization") != f"Bearer {token}":
        return Response("unauthorized", status_code=401)
    body = await request.json()
    method = body.get("method")
    if "id" not in body:
        return Response(status_code=202)
    if method == "initialize":
        sid = uuid.uuid4().hex
        SESSIONS.add(sid)
        return reply(body, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "1"}}, headers={"Mcp-Session-Id": sid})
    if request.headers.get("mcp-session-id") not in SESSIONS:
        return Response("no session", status_code=404)
    if method == "tools/call":
        result = call_tool(body["params"]["name"], body["params"].get("arguments", {}))
        if result is None:
            return reply(body, error={"code": -32602, "message": f"Unknown tool: {body['params']['name']}"})
        return reply(body, result)
    return reply(body, error={"code": -32601, "message": "Method not found"})


@app.delete("/mcp")
async def close(request: Request) -> Response:
    SESSIONS.discard(request.headers.get("mcp-session-id", ""))
    return Response(status_code=200)
