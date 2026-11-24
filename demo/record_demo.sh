#!/usr/bin/env bash
# Record demo/demo.webm with agent-browser showing:
#   1. Live journey query (Kashmere Gate ISBT -> Nehru Place, Tue 08:30 IST)
#   2. Oct 22 replay on 717A (Badarpur Border (T) -> Kapashera Border) with model_replay legs
#   3. Oct 22 replay off 717A (Kashmere Gate ISBT -> Nehru Place), which abstains
#   4. Complete 4-stage marginal-transfer replay journey (Oct 22 run 2171_17_09_490:
#      initial timetable recommendation -> growing delay -> advice flip -> observed missed outcome)
#   5. Explicit stale-feed abstention ("Not enough data") and live recovery
#
# Prerequisites:
#   - OTP 2.8.1 running on http://localhost:3102/otp/gtfs/v1 (scripts/otp_serve.sh)
#   - FastAPI running on http://localhost:3100 with PS1_LIVE_STALE_THRESHOLD_S=45
#     (production default is 600 s; 45 s override enables rapid stale-feed demo):
#       PS1_LIVE_STALE_THRESHOLD_S=45 \
#       PS1_DATA_DIR=/home/factory-user/ps1-scratch/data \
#       PS1_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
#         /home/factory-user/ps1-scratch/venv/bin/python -m uvicorn ps1.api:app \
#         --app-dir src --host 127.0.0.1 --port 3100

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_URL="${PS1_API_URL:-http://127.0.0.1:3100}"
OUTPUT_WEBM="$REPO_ROOT/demo/demo.webm"
STALE_THRESHOLD_S="${PS1_LIVE_STALE_THRESHOLD_S:-45}"
SESSION="${AGENT_BROWSER_SESSION:-ps1-demo}"
FPS="${DEMO_FPS:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION="$2"
      shift 2
      ;;
    --output)
      OUTPUT_WEBM="$2"
      shift 2
      ;;
    --api-url)
      API_URL="$2"
      shift 2
      ;;
    --fps)
      FPS="$2"
      shift 2
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$SESSION" == "default" ]]; then
  printf 'Refusing to use the shared "default" agent-browser session; pass --session <name>\n' >&2
  exit 2
fi

ab() {
  agent-browser --session "$SESSION" "$@"
}

# The recorder only emits frames when the page changes, so a static pause is
# dropped from the video. Scrolling the sidebar in steps keeps the view on screen.
scroll_sidebar() {
  local steps="$1" px="$2"
  for _ in $(seq "$steps"); do
    ab eval "document.querySelector('aside.sidebar').scrollBy({top: $px, behavior: 'smooth'})" >/dev/null
    ab wait 700
  done
}

cleanup() {
  curl -sf -X POST "${API_URL}/live/resume?poll_now=false" >/dev/null 2>&1 || true
  agent-browser --session "$SESSION" record stop >/dev/null 2>&1 || true
  agent-browser --session "$SESSION" close >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf '[demo] Checking API health and live feed freshness at %s (expected threshold=%ss)\n' "$API_URL" "$STALE_THRESHOLD_S"
curl -sf "${API_URL}/health" >/dev/null
curl -sf -X POST "${API_URL}/live/resume?poll_now=true" >/dev/null

live_json="$(curl -sf "${API_URL}/live/status")"
actual_threshold="$(printf '%s' "$live_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stale_threshold_seconds"])')"
is_fresh="$(printf '%s' "$live_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["is_fresh"])')"
printf '[demo] Live status: is_fresh=%s stale_threshold_seconds=%s\n' "$is_fresh" "$actual_threshold"
if ! python3 -c 'import sys; raise SystemExit(float(sys.argv[1]) != float(sys.argv[2]))' "$actual_threshold" "$STALE_THRESHOLD_S"; then
  printf '[demo] API threshold %ss does not match required demo override %ss; aborting.\n' "$actual_threshold" "$STALE_THRESHOLD_S" >&2
  exit 1
fi
if [[ "$is_fresh" != "True" ]]; then
  printf '[demo] Live feed is not fresh before recording; aborting.\n' >&2
  exit 1
fi

printf '[demo] Opening UI in agent-browser session=%s (1280x860)\n' "$SESSION"
ab open "${API_URL}/"
ab set viewport 1280 860
ab wait --text "Delhi Bus Journey Planner"
ab wait 1500

printf '[demo] Starting WebM recording -> %s (%s fps)\n' "$OUTPUT_WEBM" "$FPS"
ab record start "$OUTPUT_WEBM" --fps "$FPS"

# Segment 1: Live journey planning (Kashmere Gate ISBT -> Nehru Place, Tue 08:30)
printf '[demo] Segment 1: Live journey query\n'
ab click "#preset-example-live"
ab click "#plan-btn"
ab wait --text "Option 1"
ab wait 3500

printf '[demo] Segment 2: Oct 22 replay on 717A\n'
ab click "#preset-example-replay"
ab click "#plan-btn"
ab wait --text "717AUP"
ab eval "Array.from(document.querySelectorAll('#options-list .option-card')).find((card) => card.textContent.includes('717AUP')).click()"
ab wait --text "model_replay"
ab wait 1000
scroll_sidebar 6 110
ab wait 1500
ab eval "document.querySelector('aside.sidebar').scrollTo({top: 0})" >/dev/null

printf '[demo] Segment 3: Oct 22 replay off 717A abstains\n'
ab click "#preset-replay-abstain"
ab click "#plan-btn"
ab wait --text "Not enough data"
ab wait 3500

# Pause live poller now so the >45s stale clock advances while the viewer watches the replay demo
printf '[demo] Pausing live poller via POST /live/pause (threshold=%ss)\n' "$actual_threshold"
curl -sf -X POST "${API_URL}/live/pause?reason=demo_stale_abstention" >/dev/null

# Segment 4: Complete 4-stage marginal-transfer replay journey
printf '[demo] Segment 4: Marginal-transfer replay demo (4 stages)\n'
ab click "#replay-demo-btn"
ab wait --text "Marginal-Transfer Replay Demo"
ab wait --text "Hamdard Nagar / Sangam Vihar"
ab wait 3500

printf '[demo]   Stage 1: Growing delay at Badarpur M B Road / Rajiv Gandhi Stadium\n'
ab click "#stage-tabs button:nth-child(2)"
ab wait 3500

printf '[demo]   Stage 2: Advice flip at Tuglaqabad MB Road / Okhla more\n'
ab click "#stage-tabs button:nth-child(3)"
ab wait 4000

printf '[demo]   Stage 3: Observed Oct 22 outcome reveal (missed by 27m 43s)\n'
ab click "#stage-tabs button:nth-child(4)"
ab wait 4000

ab click "#close-demo-btn"
ab wait 1000

# Wait until feed age strictly exceeds stale_threshold_seconds (e.g., >45 s)
printf '[demo] Waiting for live feed age to exceed %ss...\n' "$actual_threshold"
while true; do
  status_json="$(curl -sf "${API_URL}/live/status")"
  stale_ready="$(printf '%s' "$status_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); age=d.get("current_feed_age_seconds") or 0; thr=d.get("stale_threshold_seconds") or 45; print(f"{age:.1f} {1 if age > thr + 1 else 0}")')"
  age_val="${stale_ready%% *}"
  ready_flag="${stale_ready##* }"
  printf '[demo]   feed_age=%ss (threshold=%ss)\n' "$age_val" "$actual_threshold"
  if [[ "$ready_flag" == "1" ]]; then
    break
  fi
  sleep 3
done

# Segment 5: Explicit stale-feed abstention in UI ("Not enough data")
printf '[demo] Segment 5: Triggering stale-feed abstention in UI\n'
ab click "#preset-example-live"
ab click "#plan-btn"
ab wait --text "Not enough data"
ab wait 4500

# Segment 6: Resume live poller and show UI recovery
printf '[demo] Segment 6: Resuming live poller via POST /live/resume and recovering UI\n'
curl -sf -X POST "${API_URL}/live/resume?poll_now=true" >/dev/null
ab click "#abstention-retry-btn"
ab wait --text "Option 1"
ab wait 3500

printf '[demo] Stopping recording\n'
ab record stop

printf '[demo] Checking console errors\n'
ab errors --json || true

printf '[demo] Probing %s with ffprobe\n' "$OUTPUT_WEBM"
ffprobe -v error -show_format -show_streams "$OUTPUT_WEBM"
printf '[demo] Done: %s\n' "$OUTPUT_WEBM"
