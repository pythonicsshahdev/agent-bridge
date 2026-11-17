"""Stand-in for an OpenAI-compatible chat endpoint (LiteLLM, Ollama, vLLM, ...). No model: replies are deterministic.

Run:  uvicorn --app-dir tests/mocks openai_chat:app --port 9014
Each reply says how many user turns it has seen and repeats the first thing the user said, so tests can prove the
proxy sent the whole conversation. Send "boom" for HTTP 500, "filter" for a content-filter finish.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="mock-openai-chat")
KEY = os.environ.get("MOCK_CHAT_KEY")  # when set, require "Authorization: Bearer <key>"


@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    if KEY and request.headers.get("authorization") != f"Bearer {KEY}":
        return JSONResponse({"error": {"message": "bad key"}}, status_code=401)
    body = await request.json()
    users = [m["content"] for m in body.get("messages", []) if m.get("role") == "user"]
    last = users[-1] if users else ""
    if last == "boom":
        return JSONResponse({"error": {"message": "upstream exploded"}}, status_code=500)
    finish = "content_filter" if last == "filter" else "stop"
    text = f"turn {len(users)}: you said '{last}'; first was '{users[0] if users else ''}' (model {body.get('model')})"
    return JSONResponse({"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}]})
