"""Stand-in for a Boomi web-services listener, for local demos and tests.

Run:  uvicorn --app-dir tests/mocks boomi_listener:app --port 9000
Set MOCK_BOOMI_TOKEN to require that exact bearer token; otherwise any bearer token is accepted.
"""

from __future__ import annotations

import itertools
import os

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="mock-boomi-listener")
_FIRST_ORDER_ID = 1001
_order_ids = itertools.count(_FIRST_ORDER_ID)
_orders: dict[str, dict] = {}


_job_ids = itertools.count(1)
_jobs: dict[str, dict] = {}


def reset_state() -> None:
    """Forget all orders and jobs and restart ids; used between tests."""
    global _order_ids, _job_ids
    _order_ids = itertools.count(_FIRST_ORDER_ID)
    _job_ids = itertools.count(1)
    _orders.clear()
    _jobs.clear()


def _require_bearer(authorization: str | None) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    expected = os.environ.get("MOCK_BOOMI_TOKEN")
    if scheme.lower() != "bearer" or not token or (expected and token != expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@app.post("/ws/simple/createOrder")
async def create_order(request: Request, authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    body = await request.json()
    if not isinstance(body, dict) or not (body.get("sku") or body.get("text")):
        raise HTTPException(status_code=400, detail="sku or text is required")
    order_id = f"ORD-{next(_order_ids)}"
    order = {"orderId": order_id, "status": "CREATED", "received": body}
    _orders[order_id] = order
    return order


@app.get("/ws/simple/orders/{order_id}")
async def get_order(order_id: str, authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    if order_id not in _orders:
        raise HTTPException(status_code=404, detail=f"no order {order_id}")
    return _orders[order_id]


@app.post("/ws/simple/alwaysFails")
async def always_fails() -> None:
    raise HTTPException(status_code=500, detail="simulated Boomi process error")


# ---- async jobs: trigger returns 202 + an execution id, then poll the status endpoint -------------------------
# Start body (all optional): {"polls": 2, "fail": false, "flaky": false}. The job reports RUNNING for `polls`
# status calls, then COMPLETE (with a result) or ERROR. `flaky` makes the second status call return a 503 once.
# Canceling reports ABORTED afterwards.


@app.post("/ws/simple/startExport", status_code=202)
async def start_export(request: Request, authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    job_id = f"EXE-{next(_job_ids)}"
    _jobs[job_id] = {"polls": int(body.get("polls", 2)), "fail": bool(body.get("fail")), "flaky": bool(body.get("flaky")), "calls": 0, "cancels": 0, "flaked": False, "auth": authorization}
    return {"executionId": job_id, "status": "SUBMITTED"}


@app.get("/ws/simple/exportStatus/{job_id}")
async def export_status(job_id: str, authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    job["calls"] += 1
    if job["flaky"] and job["calls"] == 2 and not job["flaked"]:
        job["flaked"] = True
        raise HTTPException(status_code=503, detail="try again")
    if job["cancels"]:
        return {"status": "ABORTED"}
    if job["calls"] <= job["polls"]:
        return {"status": "RUNNING", "percent": min(99, job["calls"] * 100 // (job["polls"] + 1))}
    if job["fail"]:
        return {"status": "ERROR", "message": "export failed: disk full"}
    return {"status": "COMPLETE", "result": {"rows": 42, "file": "export.csv"}}


@app.delete("/ws/simple/exportCancel/{job_id}")
async def export_cancel(job_id: str, authorization: str | None = Header(None)) -> dict:
    _require_bearer(authorization)
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    job["cancels"] += 1
    return {"status": "CANCELING"}
