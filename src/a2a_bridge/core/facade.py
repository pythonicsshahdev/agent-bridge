"""REST facade over A2A: one-endpoint access to any agent, for callers that cannot speak JSON-RPC.

POST /agents/{slug}/invoke            send a message, wait up to `wait_seconds` for the outcome
GET  /agents/{slug}/tasks/{task_id}   poll (optionally waiting)
POST /agents/{slug}/tasks/{task_id}/cancel
GET  /agents/{slug}/openapi.json      machine-readable description, for tool registration

This is protocol translation only: the same executors, task store, owner scoping and auth apply.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part, new_text_part
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from a2a.utils.errors import InvalidParamsError, InvalidRequestError, TaskNotCancelableError, TaskNotFoundError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_bridge.config import FacadeConfig, InboundAuthConfig
from a2a_bridge.core.values import restore_ints

logger = logging.getLogger(__name__)

_NAMES = {
    TaskState.TASK_STATE_SUBMITTED: "submitted",
    TaskState.TASK_STATE_WORKING: "working",
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}
SETTLED = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
}
MAX_TEXT_CHARS = 100_000


class TargetError(Exception):
    """A failure to report to the caller as an HTTP error (not as a task state)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class TargetInfo:
    name: str
    description: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)  # (name, description)


class Target(Protocol):
    async def describe(self) -> TargetInfo: ...

    async def send(self, request: SendMessageRequest, http: Request) -> Task | Message: ...

    async def get(self, task_id: str, http: Request) -> Task: ...

    async def cancel(self, task_id: str, http: Request) -> Task: ...


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field("", max_length=MAX_TEXT_CHARS, description="What to ask the agent, in plain language.")
    data: dict[str, Any] | None = Field(None, description="Optional structured input (a JSON object).")
    context_id: str | None = Field(None, max_length=128, description="Reuse to continue a conversation.")
    task_id: str | None = Field(None, max_length=128, description="Set to answer an input_required task.")
    wait_seconds: float | None = Field(None, ge=0, description="How long to wait for an outcome before returning 'working'.")

    @model_validator(mode="after")
    def _has_content(self) -> InvokeRequest:
        if not self.message.strip() and not self.data:
            raise ValueError("send a message, data, or both")
        return self


class TaskView(BaseModel):
    task_id: str | None = None
    context_id: str | None = None
    state: str
    text: str = ""
    data: Any | None = None
    message: str = ""


def _parts_text(parts: Any) -> str:
    return "\n".join(get_text_parts(list(parts)))


def view_of(result: Task | Message) -> TaskView:
    """A small, LLM- and tool-friendly summary of an A2A task or message."""
    if isinstance(result, Message):
        data = restore_ints(get_data_parts(list(result.parts)))
        return TaskView(
            context_id=result.context_id or None,
            state="completed",
            text=_parts_text(result.parts)[:MAX_TEXT_CHARS],
            data=data[0] if len(data) == 1 else (data or None),
        )
    texts, datas = [], []
    for artifact in result.artifacts:
        if text := _parts_text(artifact.parts):
            texts.append(text)
        datas.extend(get_data_parts(list(artifact.parts)))
    datas = restore_ints(datas)
    status_message = _parts_text(result.status.message.parts) if result.status.HasField("message") else ""
    return TaskView(
        task_id=result.id,
        context_id=result.context_id,
        state=_NAMES.get(result.status.state, "unknown"),
        text="\n".join(texts)[:MAX_TEXT_CHARS],
        data=datas[0] if len(datas) == 1 else (datas or None),
        message=status_message[:MAX_TEXT_CHARS],
    )


async def settle(target: Target, http: Request, task: Task, wait_seconds: float) -> Task:
    """Poll until the task completes or needs something from the caller, or the wait runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    delay = 0.1
    while task.status.state not in SETTLED:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(delay, remaining))
        task = await target.get(task.id, http)
        delay = min(delay * 1.5, 1.0)
    return task


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _guard(handler):
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except TargetError as exc:
            return _error(exc.status, exc.message)
        except TaskNotFoundError:
            return _error(404, "task not found")  # also what a caller sees for someone else's task
        except TaskNotCancelableError:
            return _error(409, "task can no longer be canceled")
        except (InvalidParamsError, InvalidRequestError):
            return _error(422, "the agent rejected the request as invalid")
        except Exception:
            logger.exception("facade call failed")
            return _error(502, "agent call failed")

    return wrapped


def build_facade_routes(
    slug: str, target: Target, config: FacadeConfig, base_url: str, inbound: InboundAuthConfig | None
) -> list[Route]:
    def wait_for(requested: float | None) -> float:
        return min(config.default_wait_seconds if requested is None else requested, config.max_wait_seconds)

    @_guard
    async def invoke(request: Request) -> JSONResponse:
        try:
            body = InvokeRequest.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            return _error(422, _validation_summary(exc))
        parts = ([new_text_part(body.message)] if body.message.strip() else []) + (
            [new_data_part(body.data)] if body.data else []
        )
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=parts,
            context_id=body.context_id or "",
            task_id=body.task_id or "",
        )
        sent = SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=True))
        result = await target.send(sent, request)
        if isinstance(result, Task):
            result = await settle(target, request, result, wait_for(body.wait_seconds))
        return JSONResponse(view_of(result).model_dump())

    @_guard
    async def get_task(request: Request) -> JSONResponse:
        task = await target.get(request.path_params["task_id"], request)
        wait = request.query_params.get("wait_seconds")
        try:
            wait_value = wait_for(float(wait)) if wait is not None else 0.0
        except ValueError:
            return _error(422, "wait_seconds must be a number")
        if wait_value > 0:
            task = await settle(target, request, task, wait_value)
        return JSONResponse(view_of(task).model_dump())

    @_guard
    async def cancel_task(request: Request) -> JSONResponse:
        return JSONResponse(view_of(await target.cancel(request.path_params["task_id"], request)).model_dump())

    @_guard
    async def openapi(request: Request) -> JSONResponse:
        return JSONResponse(openapi_document(slug, await target.describe(), base_url, inbound))

    return [
        Route("/invoke", invoke, methods=["POST"]),
        Route("/tasks/{task_id}", get_task, methods=["GET"]),
        Route("/tasks/{task_id}/cancel", cancel_task, methods=["POST"]),
        Route("/openapi.json", openapi, methods=["GET"]),
    ]


def _validation_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in exc.errors())
    return "request body must be valid JSON"


def openapi_document(slug: str, info: TargetInfo, base_url: str, inbound: InboundAuthConfig | None) -> dict[str, Any]:
    """OpenAPI 3.0 for one agent's facade. Descriptions are written for an LLM deciding when to call it."""
    op = slug.replace("-", "_")
    skills = "; ".join(f"{n}: {d}" if d else n for n, d in info.skills)
    summary = f"Ask {info.name}"
    description = " ".join(filter(None, [info.description, f"Skills: {skills}." if skills else ""]))
    security_schemes: dict[str, Any] = {}
    if inbound is not None and inbound.mode in ("api_key", "api_key_or_jwt"):
        security_schemes["apiKey"] = {"type": "apiKey", "in": "header", "name": inbound.header_name}
    if inbound is not None and inbound.mode in ("jwt", "api_key_or_jwt", "bearer_passthrough"):
        security_schemes["bearer"] = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    task_id = {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}
    ok = {"200": {"description": "The task's current state.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskView"}}}}}
    document: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": info.name, "description": description or info.name, "version": "1.0.0"},
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": {
            f"/agents/{slug}/invoke": {
                "post": {
                    "operationId": f"invoke_{op}",
                    "summary": summary,
                    "description": f"{description} Returns the outcome, or state 'working' with a task_id to poll.".strip(),
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/InvokeRequest"}}}},
                    "responses": ok,
                }
            },
            f"/agents/{slug}/tasks/{{task_id}}": {
                "get": {
                    "operationId": f"get_task_{op}",
                    "summary": f"Check a {info.name} task",
                    "description": "Use when invoke returned state 'working'.",
                    "parameters": [task_id, {"name": "wait_seconds", "in": "query", "required": False, "schema": {"type": "number"}}],
                    "responses": ok,
                }
            },
            f"/agents/{slug}/tasks/{{task_id}}/cancel": {
                "post": {"operationId": f"cancel_task_{op}", "summary": f"Cancel a {info.name} task", "parameters": [task_id], "responses": ok}
            },
        },
        "components": {
            "schemas": {
                "InvokeRequest": InvokeRequest.model_json_schema(),
                "TaskView": TaskView.model_json_schema(),
            },
            "securitySchemes": security_schemes,
        },
    }
    if security_schemes:
        document["security"] = [{name: []} for name in security_schemes]
    return document


class LocalTarget:
    """An agent served by this proxy, called in-process through the same request handler as /rpc."""

    def __init__(self, handler: Any, info: TargetInfo) -> None:
        from a2a.server.routes import DefaultServerCallContextBuilder

        self._handler, self._info = handler, info
        self._contexts = DefaultServerCallContextBuilder()

    async def describe(self) -> TargetInfo:
        return self._info

    async def send(self, request: SendMessageRequest, http: Request) -> Task | Message:
        return await self._handler.on_message_send(request, self._contexts.build(http))

    async def get(self, task_id: str, http: Request) -> Task:
        task = await self._handler.on_get_task(GetTaskRequest(id=task_id), self._contexts.build(http))
        if task is None:
            raise TargetError(404, "task not found")
        return task

    async def cancel(self, task_id: str, http: Request) -> Task:
        task = await self._handler.on_cancel_task(CancelTaskRequest(id=task_id), self._contexts.build(http))
        if task is None:
            raise TargetError(404, "task not found")
        return task
