"""Adapter contract: translate one A2A task into a downstream call and report state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AdapterState(StrEnum):
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"


class TaskContext(BaseModel):
    """What an adapter needs to know about the incoming A2A request."""

    agent_slug: str
    task_id: str
    context_id: str
    text: str = ""
    data: list[Any] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)  # incoming HTTP headers, lower-cased
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdapterResult(BaseModel):
    state: AdapterState
    text: str | None = None
    data: Any | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state != AdapterState.WORKING


class BaseAdapter(ABC):
    @abstractmethod
    async def execute(self, context: TaskContext) -> AdapterResult:
        """Run the task to a single result."""

    async def stream(self, context: TaskContext) -> AsyncIterator[AdapterResult]:
        """Yield progress then a terminal result. Defaults to one `execute` call."""
        yield await self.execute(context)

    async def cancel(self, task_id: str) -> None:
        """Best-effort downstream cancellation. No-op by default."""

    async def aclose(self) -> None:
        """Release clients and connections."""
