"""Bridge the a2a-sdk AgentExecutor lifecycle to a BaseAdapter."""

from __future__ import annotations

import logging
from contextlib import aclosing
from typing import Any

from a2a.helpers.proto_helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Part, TaskState

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext

logger = logging.getLogger(__name__)

_INTERRUPTED = {
    AdapterState.INPUT_REQUIRED: "requires_input",
    AdapterState.AUTH_REQUIRED: "requires_auth",
}


def _restore_ints(value: Any) -> Any:
    """protobuf Value stores every number as a double; give whole numbers back as ints (2.0 -> 2).

    Backends with strict schemas (Boomi profiles, typed APIs) often reject 2.0 where 2 is expected.
    """
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: _restore_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_ints(v) for v in value]
    return value


def build_task_context(agent_slug: str, context: RequestContext) -> TaskContext:
    message = context.message
    headers = context.call_context.state.get("headers", {}) if context.call_context else {}
    return TaskContext(
        agent_slug=agent_slug,
        task_id=context.task_id or "",
        context_id=context.context_id or "",
        text=context.get_user_input(),
        data=_restore_ints(get_data_parts(message.parts)) if message else [],
        headers={k.lower(): v for k, v in headers.items()},
        metadata=dict(context.metadata),
    )


def _parts(result: AdapterResult) -> list[Part]:
    parts: list[Part] = []
    if result.text:
        parts.append(new_text_part(result.text))
    if result.data is not None:
        parts.append(new_data_part(result.data))
    return parts


class BridgeExecutor(AgentExecutor):
    def __init__(self, adapter: BaseAdapter, agent_slug: str) -> None:
        self.adapter = adapter
        self.agent_slug = agent_slug

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ValueError("request has no message")
        task_id, context_id = context.task_id, context.context_id
        if context.current_task is None:
            task = new_task_from_user_message(context.message)
            task_id, context_id = task.id, task.context_id
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work()

        tctx = build_task_context(self.agent_slug, context).model_copy(
            update={"task_id": task_id, "context_id": context_id}
        )
        try:
            async with aclosing(self.adapter.stream(tctx)) as results:
                async for result in results:
                    if await self._publish(updater, result):
                        return
        except Exception as exc:  # CancelledError is a BaseException and propagates.
            logger.exception("adapter failed for agent %s task %s", self.agent_slug, task_id)
            await updater.failed(updater.new_agent_message([new_text_part(f"Adapter error: {type(exc).__name__}")]))
            return
        await updater.failed(updater.new_agent_message([new_text_part("Adapter ended without a final result")]))

    async def _publish(self, updater: TaskUpdater, result: AdapterResult) -> bool:
        """Publish one adapter result. Returns True when the task has reached a terminal/interrupted state."""
        state = result.state
        if state == AdapterState.WORKING:
            message = updater.new_agent_message(_parts(result)) if result.text else None
            await updater.update_status(TaskState.TASK_STATE_WORKING, message=message)
            return False
        if state == AdapterState.COMPLETED:
            parts = _parts(result)
            if parts:
                await updater.add_artifact(parts, name="result", last_chunk=True)
            await updater.complete()
            return True
        text = result.error or result.text or state.value
        message = updater.new_agent_message([new_text_part(text)])
        if state == AdapterState.FAILED:
            await updater.failed(message)
        elif state == AdapterState.REJECTED:
            await updater.reject(message)
        else:
            await getattr(updater, _INTERRUPTED[state])(message)
        return True

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        await self.adapter.cancel(task_id)
        updater = TaskUpdater(event_queue, task_id, context.context_id or "")
        await updater.cancel()
