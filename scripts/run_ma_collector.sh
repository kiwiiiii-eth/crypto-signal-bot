#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

mkdir -p data
exec 9>data/ma_collector.lock
if ! flock -n 9; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') skip crypto_ma: previous run still active"
  exit 0
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${INFLUXDB_TOKEN:-}" ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') skip crypto_ma: INFLUXDB_TOKEN not set"
  exit 0
fi

exec venv/bin/python scripts/ma_collector.py \
  --exchange "${MA_SOURCE_EXCHANGE:-bitget}" \
  --intervals "${MA_INTERVALS:-5m,15m,1h,4h,1d}" \
  --workers "${MA_WORKERS:-2}" \
  --request-sleep "${MA_REQUEST_SLEEP:-0.05}"
