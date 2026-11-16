"""Trigger-and-poll adapter: start a downstream job (usually 202 + a job id), then poll until it finishes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from a2a_bridge.adapters.base import AdapterResult, AdapterState, BaseAdapter, TaskContext
from a2a_bridge.adapters.http_sync import MAX_ERROR_CHARS, build_headers, build_payload
from a2a_bridge.config import HttpAsyncAdapterConfig

logger = logging.getLogger(__name__)

_MISSING = object()


def get_path(value: Any, path: str | None) -> Any:
    """Follow a dotted path through dicts and lists (`a.b.0.c`). Returns _MISSING if it does not resolve."""
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _failed(message: str) -> AdapterResult:
    return AdapterResult(state=AdapterState.FAILED, error=message)


def _http_failure(response: httpx.Response, what: str) -> AdapterResult:
    detail = response.text[:MAX_ERROR_CHARS].strip()
    message = f"{what} returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
    if response.status_code in (401, 403):
        return AdapterResult(state=AdapterState.AUTH_REQUIRED, error=message)
    return _failed(message)


class HttpAsyncAdapter(BaseAdapter):
    def __init__(self, config: HttpAsyncAdapterConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        # task_id -> (job id, headers) so a later cancel can reach the right job with the right credentials.
        self._jobs: dict[str, tuple[str, dict[str, str]]] = {}

    async def execute(self, context: TaskContext) -> AdapterResult:
        last = _failed("Job produced no result")
        async for result in self.stream(context):
            last = result
        return last

    async def stream(self, context: TaskContext) -> AsyncIterator[AdapterResult]:
        cfg = self.config
        headers, missing = build_headers(cfg, context)
        if missing:
            yield AdapterResult(state=AdapterState.AUTH_REQUIRED, error=missing)
            return
        try:
            trigger = await self._client.request(
                cfg.method, cfg.url, json=build_payload(context), headers=headers, timeout=cfg.timeout_seconds
            )
        except httpx.TimeoutException:
            yield _failed(f"Starting the job timed out after {cfg.timeout_seconds:g}s")
            return
        except httpx.HTTPError as exc:
            logger.warning("job trigger failed: %s", type(exc).__name__)
            yield _failed("Downstream unreachable")
            return
        if not trigger.is_success:
            yield _http_failure(trigger, "Starting the job")
            return
        try:
            job_id = get_path(trigger.json(), cfg.job_id_path)
        except ValueError:
            job_id = _MISSING
        if job_id is _MISSING or not isinstance(job_id, (str, int)) or isinstance(job_id, bool) or not str(job_id):
            yield _failed(f"The start response had no job id at '{cfg.job_id_path}'")
            return
        job_id = str(job_id)
        self._jobs[context.task_id] = (job_id, headers)
        yield AdapterResult(state=AdapterState.WORKING, text=f"Job {job_id} started")

        finished = False
        try:
            async for result in self._poll(job_id, headers):
                yield result
                if result.is_terminal:
                    finished = True
                    return
        finally:
            if finished:
                self._jobs.pop(context.task_id, None)
            # On cancellation the entry stays so cancel() can use it; cancel() removes it.

    async def _poll(self, job_id: str, headers: dict[str, str]) -> AsyncIterator[AdapterResult]:
        cfg = self.config
        url = cfg.status_url.replace("{job_id}", quote(job_id, safe=""))  # a hostile job id cannot rewrite the path
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.max_poll_seconds
        errors, last_status = 0, None
        while True:
            await asyncio.sleep(cfg.poll_interval_seconds)
            if loop.time() > deadline:
                await self._cancel_job(job_id, headers)
                yield _failed(f"Job {job_id} did not finish within {cfg.max_poll_seconds:g}s")
                return
            try:
                response = await self._client.request(cfg.status_method, url, headers=headers, timeout=cfg.timeout_seconds)
                if response.status_code in (401, 403, 404):
                    yield _http_failure(response, f"Checking job {job_id}")
                    return
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                errors += 1
                logger.warning("status poll failed (%d/%d): %s", errors, cfg.max_poll_errors, type(exc).__name__)
                if errors >= cfg.max_poll_errors:
                    yield _failed(f"Could not read the status of job {job_id} after {errors} attempts")
                    return
                continue
            errors = 0
            raw = get_path(body, cfg.status_path)
            status = "" if raw is _MISSING else str(raw)
            normalized = status.strip().lower()
            if normalized in {v.lower() for v in cfg.completed_values}:
                yield self._completed(body)
                return
            if normalized in {v.lower() for v in cfg.failed_values}:
                reason = get_path(body, cfg.error_path) if cfg.error_path else _MISSING
                detail = f": {str(reason)[:MAX_ERROR_CHARS]}" if reason not in (_MISSING, None, "") else ""
                yield _failed(f"Job {job_id} {status}{detail}")
                return
            if status and status != last_status:
                last_status = status
                yield AdapterResult(state=AdapterState.WORKING, text=f"Status: {status}")

    def _completed(self, body: Any) -> AdapterResult:
        result = get_path(body, self.config.result_path)
        if result is _MISSING or result is None:
            return AdapterResult(state=AdapterState.COMPLETED)
        if isinstance(result, str):
            return AdapterResult(state=AdapterState.COMPLETED, text=result)
        return AdapterResult(state=AdapterState.COMPLETED, data=result)

    async def _cancel_job(self, job_id: str, headers: dict[str, str]) -> None:
        cfg = self.config
        if not cfg.cancel_url:
            return
        url = cfg.cancel_url.replace("{job_id}", quote(job_id, safe=""))
        try:
            await self._client.request(cfg.cancel_method, url, headers=headers, timeout=cfg.timeout_seconds)
        except httpx.HTTPError as exc:  # best effort: the task is ending either way
            logger.warning("job cancel failed: %s", type(exc).__name__)

    async def cancel(self, task_id: str) -> None:
        entry = self._jobs.pop(task_id, None)
        if entry:
            await self._cancel_job(*entry)

    async def aclose(self) -> None:
        await self._client.aclose()
