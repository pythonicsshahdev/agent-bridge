"""Helpers for protobuf Value payloads."""

from __future__ import annotations

from typing import Any


def restore_ints(value: Any) -> Any:
    """protobuf Value stores every number as a double; give whole numbers back as ints (2.0 -> 2).

    Backends with strict schemas (Boomi profiles, typed APIs) often reject 2.0 where 2 is expected.
    """
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: restore_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [restore_ints(v) for v in value]
    return value
