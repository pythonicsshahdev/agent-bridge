"""Minimal Server-Sent Events parser."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator


async def parse_sse(lines: AsyncIterable[str]) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) per SSE event. `data` lines are joined with newlines; comments are skipped."""
    event, data = "message", []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data or event != "message":
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data:  # stream ended without a trailing blank line
        yield event, "\n".join(data)
