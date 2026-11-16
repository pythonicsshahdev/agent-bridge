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


def reset_state() -> None:
    """Forget all orders and restart ids; used between tests."""
    global _order_ids
    _order_ids = itertools.count(_FIRST_ORDER_ID)
    _orders.clear()


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
