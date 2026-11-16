"""Stand-in for the Boomi Agentstudio Execute API, for local demos and tests.

Run:  uvicorn --app-dir tests/mocks agentstudio_execute:app --port 9100
Auth is HTTP Basic: user "BOOMI_TOKEN.<MOCK_AS_USER>", password MOCK_AS_TOKEN (defaults mock-user / mock-token).
Deployments: dep-chat (conversational), dep-guarded (blocks messages containing "ssn"), dep-structured (JSON in/out).
Messages: "boom" -> HTTP 500, "slow" -> sleeps 5s, "tool" -> emits an ACTION progress event, "error-event" -> SSE error event.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock-agentstudio-execute")
BLOCKED = "I can't help with that request."
_sessions: dict[str, int] = {}


def reset_state() -> None:
    _sessions.clear()


def _authorized(request: Request) -> bool:
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode().partition(":")
    except ValueError:
        return False
    return user == f"BOOMI_TOKEN.{os.environ.get('MOCK_AS_USER', 'mock-user')}" and password == os.environ.get(
        "MOCK_AS_TOKEN", "mock-token"
    )


def _session_id() -> str:
    return "01" + secrets.token_hex(12).upper()[:24]


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/execute/api/v1/{account_id}/session")
async def session(account_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    body = await request.json()
    deployment = body.get("deployment_id")
    if deployment not in {"dep-chat", "dep-guarded", "dep-structured"}:
        return JSONResponse({"detail": f"deployment {deployment} not found"}, status_code=404)
    session_id = body.get("session_id")
    if session_id is not None and session_id not in _sessions:
        return JSONResponse({"detail": "unknown session"}, status_code=404)
    session_id = session_id or _session_id()
    _sessions[session_id] = _sessions.get(session_id, 0) + 1
    turn = _sessions[session_id]
    message = body.get("message")

    if deployment == "dep-structured":
        if not isinstance(message, dict):
            return JSONResponse({"detail": "The input does not match the schema configured for this Agent"}, status_code=422)
        if message.get("fail"):
            return JSONResponse({"success": False, "error": "agent could not classify input"})
        return JSONResponse(
            {"success": True, "agent_id": "agent-1", "session_id": session_id, "data": {"intent": "other", "turn": turn, "echo": message}}
        )

    text = str(message)
    if text == "boom":
        return JSONResponse({"detail": "internal error"}, status_code=500)
    if text == "slow":
        await asyncio.sleep(5)

    async def events():
        yield _sse("start", {"session_id": session_id, "agent_id": "agent-1", "invoked_from": "API"})
        yield _sse("progress_notification", {"type": "THINKING", "content": "Thinking"})
        if text == "tool":
            yield _sse("progress_notification", {"type": "ACTION", "content": "Calling Tool: lookup_order"})
        if text == "error-event":
            yield _sse("error", {"content": "model unavailable"})
            return
        if deployment == "dep-guarded" and "ssn" in text.lower():
            reply = BLOCKED
        else:
            reply = f"Turn {turn}: you said {text}"
        yield _sse("message", {"id": "m", "session_id": session_id, "role": "agent", "content": reply, "timestamp": "1"})
        yield "event: [DONE]\ndata: null\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
