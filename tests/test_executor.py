from types import SimpleNamespace

import pytest
from a2a.helpers.proto_helpers import new_data_part, new_text_part
from a2a.types.a2a_pb2 import Message, Role, TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter
from a2a_bridge.core.executor import BridgeExecutor


class Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)

    def states(self):
        return [e.status.state for e in self.events if isinstance(e, TaskStatusUpdateEvent)]


class Adapter(BaseAdapter):
    def __init__(self, results=None, exc=None):
        self.results, self.exc, self.seen, self.cancelled = results or [], exc, None, []

    async def execute(self, context):
        return self.results[-1]

    async def stream(self, context):
        self.seen = context
        for r in self.results:
            yield r
        if self.exc:
            raise self.exc

    async def cancel(self, task_id):
        self.cancelled.append(task_id)


def request(text="hi", current_task=None, headers=None):
    msg = Message(
        message_id="m1", role=Role.ROLE_USER, parts=[new_text_part(text), new_data_part({"qty": 2})]
    )
    return SimpleNamespace(
        message=msg,
        task_id="t1",
        context_id="c1",
        current_task=current_task,
        call_context=SimpleNamespace(state={"headers": headers or {}}),
        metadata={},
        get_user_input=lambda: text,
    )


async def run(adapter, **kw):
    q = Queue()
    await BridgeExecutor(adapter, "svc").execute(request(**kw), q)
    return q


async def test_completed_publishes_artifact_then_complete():
    a = Adapter([AdapterResult(state=AdapterState.COMPLETED, text="done", data={"id": 7})])
    q = await run(a, headers={"Authorization": "Bearer abc"})
    assert q.states() == [TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_COMPLETED]
    art = next(e for e in q.events if isinstance(e, TaskArtifactUpdateEvent))
    assert len(art.artifact.parts) == 2
    assert a.seen.headers == {"authorization": "Bearer abc"}
    assert a.seen.text == "hi" and a.seen.data == [{"qty": 2.0}]


async def test_working_updates_before_terminal():
    a = Adapter([AdapterResult(state=AdapterState.WORKING, text="50%"), AdapterResult(state=AdapterState.COMPLETED)])
    q = await run(a)
    assert q.states() == [TaskState.TASK_STATE_WORKING] * 2 + [TaskState.TASK_STATE_COMPLETED]


@pytest.mark.parametrize(
    "state, expected",
    [
        (AdapterState.FAILED, TaskState.TASK_STATE_FAILED),
        (AdapterState.REJECTED, TaskState.TASK_STATE_REJECTED),
        (AdapterState.INPUT_REQUIRED, TaskState.TASK_STATE_INPUT_REQUIRED),
        (AdapterState.AUTH_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED),
    ],
)
async def test_non_success_states(state, expected):
    q = await run(Adapter([AdapterResult(state=state, error="why")]))
    assert q.states()[-1] == expected


async def test_adapter_exception_becomes_failed_without_leaking_detail():
    q = await run(Adapter(exc=RuntimeError("secret-token-123")))
    assert q.states()[-1] == TaskState.TASK_STATE_FAILED
    assert "secret-token-123" not in str(q.events)


async def test_stream_without_terminal_fails():
    q = await run(Adapter([AdapterResult(state=AdapterState.WORKING)]))
    assert q.states()[-1] == TaskState.TASK_STATE_FAILED


async def test_cancel_calls_adapter_and_publishes_canceled():
    a, q = Adapter(), Queue()
    await BridgeExecutor(a, "svc").cancel(request(), q)
    assert a.cancelled == ["t1"]
    assert q.states() == [TaskState.TASK_STATE_CANCELED]
