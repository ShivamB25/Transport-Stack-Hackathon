#!/usr/bin/env bash
# Usage: livecheck/launch.sh OUT_DIR UNTIL [extra `livecheck run` options]
# Reuses a healthy OTP on 3102 (otherwise starts it and stops it afterwards), starts the API
# with the live poller on LIVECHECK_API_PORT (default 3120), records until UNTIL, then stops
# only what it started.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 OUT_DIR UNTIL(HH:MM|+Nm) [livecheck run options]" >&2
  exit 2
fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"
UNTIL="$2"
shift 2
API_PORT="${LIVECHECK_API_PORT:-3120}"
# Development-machine default; set DELHI_TRANSIT_SCRATCH_DIR (or the specific variables) elsewhere.
SCRATCH_DIR="${DELHI_TRANSIT_SCRATCH_DIR:-/home/factory-user/ps1-scratch}"
DATA_DIR="${DELHI_TRANSIT_DATA_DIR:-$SCRATCH_DIR/data}"
OTP_URL="http://localhost:3102/otp/gtfs/v1"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$OUT"

log() { printf '%s launch: %s\n' "$(TZ=Asia/Kolkata date -Iseconds)" "$*" | tee -a "$OUT/run.log"; }

otp_healthy() {
  curl -sf -m 10 -X POST "$OTP_URL" -H 'Content-Type: application/json' \
    -d '{"query":"{ __schema { queryType { name } } }"}' >/dev/null
}

OTP_PID=""
API_PID=""
cleanup() {
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    log "stopping API (pid $API_PID)"
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  if [[ -n "$OTP_PID" ]] && kill -0 "$OTP_PID" 2>/dev/null; then
    log "stopping OTP started by this script (pid $OTP_PID)"
    kill "$OTP_PID" 2>/dev/null || true
    wait "$OTP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT

if otp_healthy; then
  log "reusing healthy OTP on 3102"
else
  log "starting OTP on 3102"
  "$REPO/otp/serve.sh" >"$OUT/otp.log" 2>&1 &
  OTP_PID=$!
  for _ in $(seq 1 90); do
    otp_healthy && break
    kill -0 "$OTP_PID" 2>/dev/null || { log "OTP exited during startup"; exit 1; }
    sleep 5
  done
  otp_healthy || { log "OTP not healthy after 450 s"; exit 1; }
fi

if curl -sf -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null; then
  log "port $API_PORT already serves an API; refusing to start a second one"
  exit 1
fi
log "starting API with the live poller on $API_PORT"
cd "$REPO/backend"
DELHI_TRANSIT_DATA_DIR="$DATA_DIR" DELHI_TRANSIT_OTP_URL="$OTP_URL" DELHI_TRANSIT_ENABLE_LIVE_POLLER=1 \
  uv run --frozen uvicorn delhi_transit.api:app --host 127.0.0.1 --port "$API_PORT" >"$OUT/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  curl -sf -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null && break
  kill -0 "$API_PID" 2>/dev/null || { log "API exited during startup"; exit 1; }
  sleep 2
done
curl -sf -m 5 "http://127.0.0.1:$API_PORT/health" >/dev/null || { log "API not healthy"; exit 1; }
# Let the API's own poller ingest a first snapshot before the first slot.
sleep 30

log "recording until $UNTIL into $OUT"
uv run --frozen python -m delhi_transit.livecheck run --until "$UNTIL" --out "$OUT" \
  --api-url "http://127.0.0.1:$API_PORT" --data-dir "$DATA_DIR" "$@"
log "recording finished"
