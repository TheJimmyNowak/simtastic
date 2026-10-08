#!/bin/bash
# Uruchamia backendy kampanii na portach podanych w argumentach (kolejno — każdy przy
# starcie usuwa osierocone kontenery symulatora, więc nie wolno ich restartować w trakcie).
cd "$(dirname "$0")/../.."
mkdir -p data/campaign
for port in "$@"; do
  PYTHONPATH=backend SIM_NODE_ACCESS=host nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$port" \
    > "data/campaign/backend-$port.log" 2>&1 &
  echo "$port $!"
  for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$port/api/scenarios" >/dev/null && break; sleep 1; done
done
