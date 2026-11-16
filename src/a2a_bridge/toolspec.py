"""Generate Boomi Agentstudio OpenAPI tool definitions that call an agent through the REST facade.

The output is the body of `agentstudio-tool.sh create --type OpenAPI --body <file>`. It deliberately holds
no credentials: secret-bearing authentication is set in the Boomi console, never written to a file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from a2a_bridge.config import AgentConfig

STYLES = ("json", "query")
WAIT_SECONDS = 50  # stay inside a typical tool-call timeout; the agent can poll the task afterwards
ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
ALLOWED_TYPES = {"string", "integer", "float", "boolean"}
ALLOWED_BODY_TYPES = {
    "application/json",
    "application/xml",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
}
_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")
_SECRET_HEADERS = ("authorization", "x-api-key", "x-boomi-authorization", "cookie", "token", "secret", "password")
MAX_NAME_CHARS = 50


def _param(name: str, description: str, required: bool, type_: str = "string") -> dict[str, Any]:
    return {"name": name, "description": description, "required": required, "type": type_}


def _bound(name: str, parameter: str | None = None, static: str | None = None) -> dict[str, Any]:
    return {"name": name, "static_value": static, "input_parameter_name": parameter}


def _agent_blurb(agent: AgentConfig) -> str:
    skills = "; ".join(f"{s.name}: {s.description}" if s.description else s.name for s in agent.skills)
    return " ".join(filter(None, [agent.description, f"Skills: {skills}." if skills else ""]))


def invoke_tool(agent: AgentConfig, base_url: str, style: str = "json") -> dict[str, Any]:
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    description = (
        f"{_agent_blurb(agent)} Send the request in plain language. The reply has a `state`: completed (use `text` or "
        "`data`), input_required (answer the question in `message`, passing the same `context_id` and `task_id`), "
        "working (call the check-task tool with `task_id`), or failed/rejected/auth_required (explain `message` to the user). "
        "Pass the returned `context_id` on follow-up calls to continue the same conversation."
    ).strip()
    tool: dict[str, Any] = {
        "name": f"Ask {agent.name}"[:MAX_NAME_CHARS],
        "description": description,
        "input_parameters": [
            _param("message", "What to ask the agent, in plain language.", True),
            _param("context_id", "The context_id from an earlier reply, to continue that conversation. Leave empty to start a new one.", False),
            _param("task_id", "The task_id from an earlier input_required reply, when answering its question. Otherwise leave empty.", False),
        ],
        "base_url": base_url.rstrip("/"),
        "path": f"/agents/{agent.slug}/invoke",
        "method": "POST",
        "query_parameters": [],
        "path_parameters": [],
        "headers": [],
        "authentication": None,
        "request_body": None,
        "source_id": None,
        "source_name": None,
    }
    if style == "json":
        template = json.dumps(
            {"message": "{{message}}", "context_id": "{{context_id}}", "task_id": "{{task_id}}", "wait_seconds": WAIT_SECONDS}
        )
        tool["request_body"] = {"type": "application/json", "template": template}
    else:
        tool["query_parameters"] = [
            _bound("message", "message"),
            _bound("context_id", "context_id"),
            _bound("task_id", "task_id"),
            _bound("wait_seconds", static=str(WAIT_SECONDS)),
        ]
    return tool


def task_tool(agent: AgentConfig, base_url: str) -> dict[str, Any]:
    return {
        "name": f"Check {agent.name} task"[:MAX_NAME_CHARS],
        "description": f"Check on a task started by 'Ask {agent.name}' that returned state 'working'. Returns the same fields; call again if it is still working.",
        "input_parameters": [_param("task_id", "The task_id returned by the ask tool.", True)],
        "base_url": base_url.rstrip("/"),
        "path": f"/agents/{agent.slug}/tasks/{{{{task_id}}}}",
        "method": "GET",
        "query_parameters": [_bound("wait_seconds", static=str(WAIT_SECONDS))],
        "path_parameters": [],
        "headers": [],
        "authentication": None,
        "request_body": None,
        "source_id": None,
        "source_name": None,
    }


def problems(tool: dict[str, Any]) -> list[str]:
    """Check a tool body against the Agentstudio OpenAPI tool rules. Empty means valid."""
    found: list[str] = []
    if not tool.get("name"):
        found.append("name is required")
    elif len(tool["name"]) > MAX_NAME_CHARS:
        found.append(f"name is longer than {MAX_NAME_CHARS} characters")
    declared = {p.get("name") for p in tool.get("input_parameters", [])}
    for p in tool.get("input_parameters", []):
        if p.get("type") not in ALLOWED_TYPES:
            found.append(f"input parameter {p.get('name')}: type {p.get('type')!r} is not one of {sorted(ALLOWED_TYPES)}")
    if tool.get("method") not in ALLOWED_METHODS:
        found.append(f"method {tool.get('method')!r} is not allowed")
    body = tool.get("request_body")
    if body is not None and body.get("type") not in ALLOWED_BODY_TYPES:
        found.append(f"request_body.type {body.get('type')!r} is not allowed")
    used = set(_PLACEHOLDER.findall(tool.get("path", "")))
    used |= set(_PLACEHOLDER.findall((body or {}).get("template", "")))
    for ref in used - declared:
        found.append(f"placeholder {{{{{ref}}}}} has no matching input parameter")
    for group in ("query_parameters", "path_parameters", "headers"):
        for entry in tool.get(group, []):
            static, param = entry.get("static_value"), entry.get("input_parameter_name")
            if (static is None) == (param is None):
                found.append(f"{group}.{entry.get('name')}: set exactly one of static_value and input_parameter_name")
            if param is not None and param not in declared:
                found.append(f"{group}.{entry.get('name')}: input_parameter_name {param!r} is not declared")
    for header in tool.get("headers", []):
        if header.get("static_value") and any(s in header.get("name", "").lower() for s in _SECRET_HEADERS):
            found.append(f"header {header.get('name')} carries a static value that looks like a credential; set it in the console")
    if tool.get("authentication") not in (None, "None"):
        found.append("authentication must be set in the Boomi console, not in a file")
    return found


def write_tools(agents: list[AgentConfig], base_url: str, out: Path, style: str = "json", with_task_tool: bool = True) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for agent in agents:
        specs = {f"{agent.slug}-ask.tool.json": invoke_tool(agent, base_url, style)}
        if with_task_tool:
            specs[f"{agent.slug}-check-task.tool.json"] = task_tool(agent, base_url)
        for filename, tool in specs.items():
            if issues := problems(tool):
                raise ValueError(f"{filename}: " + "; ".join(issues))
            path = out / filename
            path.write_text(json.dumps(tool, indent=2) + "\n", encoding="utf-8")
            written.append(path)
    return written
