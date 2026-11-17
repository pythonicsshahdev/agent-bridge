"""Maps an A2A (agent, contextId) to the downstream conversation/session id, so turns continue."""

from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()
sessions_table = Table(
    "a2a_bridge_sessions",
    metadata,
    Column("agent_slug", String(128), primary_key=True),
    Column("owner", String(256), primary_key=True),
    Column("context_id", String(128), primary_key=True),
    Column("session_id", String(128), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)
history_table = Table(
    "a2a_bridge_chat_history",
    metadata,
    Column("agent_slug", String(128), primary_key=True),
    Column("owner", String(256), primary_key=True),
    Column("context_id", String(128), primary_key=True),
    Column("messages", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


class SessionStore(Protocol):
    async def get(self, agent_slug: str, owner: str, context_id: str) -> str | None: ...

    async def set(self, agent_slug: str, owner: str, context_id: str, session_id: str) -> None: ...

    async def get_history(self, agent_slug: str, owner: str, context_id: str) -> list[dict[str, str]]: ...

    async def set_history(self, agent_slug: str, owner: str, context_id: str, messages: list[dict[str, str]]) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], str] = {}
        self._history: dict[tuple[str, str, str], list[dict[str, str]]] = {}

    async def get(self, agent_slug: str, owner: str, context_id: str) -> str | None:
        return self._data.get((agent_slug, owner, context_id))

    async def set(self, agent_slug: str, owner: str, context_id: str, session_id: str) -> None:
        self._data[(agent_slug, owner, context_id)] = session_id

    async def get_history(self, agent_slug: str, owner: str, context_id: str) -> list[dict[str, str]]:
        return list(self._history.get((agent_slug, owner, context_id), []))

    async def set_history(self, agent_slug: str, owner: str, context_id: str, messages: list[dict[str, str]]) -> None:
        self._history[(agent_slug, owner, context_id)] = list(messages)


class SqlSessionStore:
    """Durable mapping in the same database the SDK uses for tasks, so restarts keep conversations."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def initialize(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def get(self, agent_slug: str, owner: str, context_id: str) -> str | None:
        stmt = select(sessions_table.c.session_id).where(
            sessions_table.c.agent_slug == agent_slug,
            sessions_table.c.owner == owner,
            sessions_table.c.context_id == context_id,
        )
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one_or_none()

    async def set(self, agent_slug: str, owner: str, context_id: str, session_id: str) -> None:
        key = (
            (sessions_table.c.agent_slug == agent_slug)
            & (sessions_table.c.owner == owner)
            & (sessions_table.c.context_id == context_id)
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(update(sessions_table).where(key).values(session_id=session_id))
            if result.rowcount:
                return
            try:
                await conn.execute(
                    insert(sessions_table).values(
                        agent_slug=agent_slug, owner=owner, context_id=context_id, session_id=session_id
                    )
                )
            except IntegrityError:  # lost a race with a concurrent first turn
                await conn.execute(update(sessions_table).where(key).values(session_id=session_id))

    async def get_history(self, agent_slug: str, owner: str, context_id: str) -> list[dict[str, str]]:
        stmt = select(history_table.c.messages).where(
            history_table.c.agent_slug == agent_slug,
            history_table.c.owner == owner,
            history_table.c.context_id == context_id,
        )
        async with self._engine.connect() as conn:
            raw = (await conn.execute(stmt)).scalar_one_or_none()
        try:
            loaded = json.loads(raw) if raw else []
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []

    async def set_history(self, agent_slug: str, owner: str, context_id: str, messages: list[dict[str, str]]) -> None:
        key = (
            (history_table.c.agent_slug == agent_slug)
            & (history_table.c.owner == owner)
            & (history_table.c.context_id == context_id)
        )
        payload = json.dumps(messages)
        async with self._engine.begin() as conn:
            result = await conn.execute(update(history_table).where(key).values(messages=payload))
            if result.rowcount:
                return
            try:
                await conn.execute(
                    insert(history_table).values(
                        agent_slug=agent_slug, owner=owner, context_id=context_id, messages=payload
                    )
                )
            except IntegrityError:
                await conn.execute(update(history_table).where(key).values(messages=payload))
