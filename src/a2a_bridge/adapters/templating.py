"""Field mapping for request bodies: fill `{{placeholders}}` in a JSON template from the A2A message.

This is mapping, not logic: placeholders only read values. Supported names are `text`, `context_id`,
`task_id`, `data` (all data parts) and `data.<path>` (a dotted path into them, e.g. `data.0.sku`).
A string that is exactly one placeholder keeps the value's type; inside longer text it is stringified.
Placeholders that do not resolve become an empty string (or null when they are the whole string).
"""

from __future__ import annotations

import json
import re
from typing import Any

from a2a_bridge.adapters.base import TaskContext

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_WHOLE = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}\s*$")
_MISSING = object()


def _walk(value: Any, path: list[str]) -> Any:
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def get_path(value: Any, path: str | None) -> Any:
    """Follow a dotted path through dicts and lists (`a.b.0.c`). Returns _MISSING if it does not resolve."""
    return value if not path else _walk(value, path.split("."))


def _lookup(name: str, context: TaskContext) -> Any:
    root, *rest = name.split(".")
    values = {"text": context.text, "context_id": context.context_id, "task_id": context.task_id, "data": context.data}
    if root not in values:
        return _MISSING
    return _walk(values[root], rest)


def render_template(template: Any, context: TaskContext) -> Any:
    if isinstance(template, str):
        whole = _WHOLE.match(template)
        if whole:
            value = _lookup(whole.group(1), context)
            return None if value is _MISSING else value

        def sub(match: re.Match[str]) -> str:
            value = _lookup(match.group(1), context)
            if value is _MISSING or value is None:
                return ""
            return value if isinstance(value, str) else json.dumps(value)

        return _PLACEHOLDER.sub(sub, template)
    if isinstance(template, dict):
        return {render_template(k, context) if isinstance(k, str) else k: render_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render_template(v, context) for v in template]
    return template
