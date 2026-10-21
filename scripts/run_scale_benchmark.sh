#!/usr/bin/env bash
# One-command reproduction of the "Proving it at scale" numbers in
# README.md: brings up docker-compose.scale-demo.yml, applies migrations,
# waits for the stack to be reachable, runs scripts/scale_benchmark.py,
# and tears the stack down again (pass --keep to leave it running).
#
# Usage:
#   ./scripts/run_scale_benchmark.sh [--keep] [extra args passed to scale_benchmark.py]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="docker compose -f docker-compose.scale-demo.yml"
KEEP=0
if [[ "${1:-}" == "--keep" ]]; then
  KEEP=1
  shift
fi

cleanup() {
  if [[ "$KEEP" -eq 0 ]]; then
    echo "Tearing down scale-demo stack..."
    $COMPOSE down -v
  else
    echo "Leaving scale-demo stack running (--keep). Stop it with:"
    echo "  $COMPOSE down -v"
  fi
}
trap cleanup EXIT

echo "Starting scale-demo stack..."
$COMPOSE up --build -d

echo "Waiting for Postgres to be healthy..."
for _ in $(seq 1 30); do
  if $COMPOSE exec -T db pg_isready -U app -d bookingapi >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "Applying migrations..."
for f in db/*.sql; do
  echo "  $f"
  $COMPOSE exec -T db psql -U app -d bookingapi -f "/db/$(basename "$f")" >/dev/null
done

echo "Waiting for the API to answer through nginx..."
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

mkdir -p benchmark_results
python3 scripts/scale_benchmark.py "$@"
