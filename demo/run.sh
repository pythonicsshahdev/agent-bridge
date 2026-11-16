#!/bin/bash
# See the control plane in about a minute: runs the proxy against bundled mock backends and seeds some traffic.
#   demo/run.sh          then open http://localhost:8013/admin and sign in with:  demo-admin-key
# Stop with Ctrl-C. Listens on localhost only. The keys are throwaway demo keys.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
for port in 8013 9013; do
  if curl -s -o /dev/null "http://127.0.0.1:$port/"; then echo "Port $port is already in use. Stop the other process first." >&2; exit 1; fi
done
rm -f demo/demo.db
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; rm -f demo/demo.db; }
trap cleanup EXIT INT TERM

"$PY" -m uvicorn --app-dir tests/mocks boomi_listener:app --host 127.0.0.1 --port 9013 --log-level warning & pids+=($!)
PYTHONPATH=src "$PY" -m a2a_bridge.main --config demo/agents.demo.yaml >demo/proxy.log 2>&1 & pids+=($!)
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:8013/readyz >/dev/null 2>&1 && break; sleep 1; done
curl -sf http://127.0.0.1:8013/readyz >/dev/null || { echo "The proxy did not start; see demo/proxy.log" >&2; exit 1; }

call() { curl -s -o /dev/null -X POST "http://127.0.0.1:8013/agents/$1/invoke" -H "X-API-Key: ${2:-demo-crm-key}" -H "Authorization: Bearer demo" -H 'Content-Type: application/json' -d "$3"; }
for i in 1 2 3 4; do call orders demo-crm-key '{"data":{"sku":"A'$i'","qty":2}}'; done
call orders demo-billing-key '{"data":{"sku":"B1","qty":1}}'
call export demo-crm-key '{"data":{"polls":2},"wait_seconds":5}'
call orders wrong-key '{"message":"hi"}'
call export demo-billing-key '{"message":"hi"}'   # billing-bot may only call orders: refused

cat <<MSG

  Control plane:  http://localhost:8013/admin
  Sign in with:   demo-admin-key      (leave the header name as X-API-Key)

  Other keys: demo-crm-key (all agents), demo-billing-key (orders only). Ctrl-C stops everything.
  Try:  curl -s -X POST localhost:8013/agents/orders/invoke -H 'X-API-Key: demo-crm-key' -H 'Authorization: Bearer x' \\
          -H 'Content-Type: application/json' -d '{"data":{"sku":"Z9","qty":3}}'

MSG
wait
