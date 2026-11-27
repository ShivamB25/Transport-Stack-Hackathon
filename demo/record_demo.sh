#!/usr/bin/env bash
# Record Full HD (1920x1080) demo footage with agent-browser showing:
#   1. Live journey query (Kashmere Gate ISBT -> Nehru Place, Tue 08:30 IST) + leg breakdown
#   2. Oct 22 replay on 717A (Badarpur Border (T) -> Kapashera Border) with model_replay legs
#   3. Oct 22 replay off 717A (Kashmere Gate ISBT -> Nehru Place), which abstains
#   4. Complete 4-stage marginal-transfer replay journey (Oct 22 run 2171_17_09_490:
#      initial timetable recommendation -> growing delay -> advice flip -> observed missed outcome)
#   5. Explicit stale-feed abstention ("Not enough data") and live recovery
#
# Also captures 1920x1080 PNG slides from demo/slides.html (intro, PR #28 Kalman fallback, outro),
# transcodes demo/demo.mp4, and with --narrate invokes demo/build_narrated_demo.py to mux the
# pre-generated Sarvam Bulbul narration WAVs (demo/audio/) into demo/demo_narrated.mp4 + .srt.
#
# Prerequisites on the recording machine:
#   - OTP 2.8.1 running on http://localhost:3102/otp/gtfs/v1 (otp/serve.sh)
#   - FastAPI running on http://localhost:3100 with DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45
#     (production default is 600 s; 45 s override enables rapid stale-feed demo):
#       cd backend && DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45 \
#       DELHI_TRANSIT_DATA_DIR=/path/to/data \
#       DELHI_TRANSIT_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
#         uv run uvicorn delhi_transit.api:app --host 127.0.0.1 --port 3100
#   - agent-browser, ffmpeg, ffprobe, curl, python3
#
# Usage:
#   demo/record_demo.sh --narrate                 # Full HD 1920x1080 + narrated demo/demo_narrated.mp4
#   demo/record_demo.sh                           # Full HD 1920x1080 silent demo/demo.webm + demo/demo.mp4

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
API_URL="${DELHI_TRANSIT_API_URL:-http://127.0.0.1:3100}"
OUTPUT_WEBM="$REPO_ROOT/demo/demo.webm"
OUTPUT_MP4="$REPO_ROOT/demo/demo.mp4"
NARRATED_MP4="$REPO_ROOT/demo/demo_narrated.mp4"
SEGMENTS_DIR=""
AUDIO_DIR="$REPO_ROOT/demo/audio"
STALE_THRESHOLD_S="${DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S:-45}"
STALE_WAIT_TIMEOUT_S="${DEMO_STALE_TIMEOUT_S:-120}"
SESSION="${AGENT_BROWSER_SESSION:-delhi-transit-demo}"
WIDTH="${DEMO_WIDTH:-1920}"
HEIGHT="${DEMO_HEIGHT:-1080}"
FPS="${DEMO_FPS:-15}"
NARRATE=0

require_arg() {
  if [[ $# -lt 2 ]]; then
    printf 'Missing value for %s\n' "$1" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      require_arg "$@"
      SESSION="$2"
      shift 2
      ;;
    --output)
      require_arg "$@"
      OUTPUT_WEBM="$2"
      shift 2
      ;;
    --mp4-output)
      require_arg "$@"
      OUTPUT_MP4="$2"
      shift 2
      ;;
    --narrated-output)
      require_arg "$@"
      NARRATED_MP4="$2"
      shift 2
      ;;
    --segments-dir)
      require_arg "$@"
      SEGMENTS_DIR="$2"
      shift 2
      ;;
    --audio-dir)
      require_arg "$@"
      AUDIO_DIR="$2"
      shift 2
      ;;
    --api-url)
      require_arg "$@"
      API_URL="$2"
      shift 2
      ;;
    --width)
      require_arg "$@"
      WIDTH="$2"
      shift 2
      ;;
    --height)
      require_arg "$@"
      HEIGHT="$2"
      shift 2
      ;;
    --fps)
      require_arg "$@"
      FPS="$2"
      shift 2
      ;;
    --narrate)
      NARRATE=1
      shift
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$NARRATE" == "1" && -z "$SEGMENTS_DIR" ]]; then
  SEGMENTS_DIR="$REPO_ROOT/demo/segments"
fi

if [[ "$SESSION" == "default" ]]; then
  printf 'Refusing to use the shared "default" agent-browser session; pass --session <name>\n' >&2
  exit 2
fi

for bin in agent-browser curl python3 ffmpeg ffprobe; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    printf '[demo] Missing required command: %s\n' "$bin" >&2
    exit 1
  fi
done

ab() {
  agent-browser --session "$SESSION" "$@"
}

# Wait until Leaflet map tiles finish loading so frames never flash grey tiles.
wait_for_tiles() {
  ab eval "new Promise((resolve) => {
    const deadline = Date.now() + 4500;
    const check = () => {
      const tiles = Array.from(document.querySelectorAll('.leaflet-tile'));
      const ready = tiles.length > 0 && tiles.every((img) => img.complete && img.naturalWidth > 0);
      if (ready || Date.now() > deadline) return resolve(ready);
      setTimeout(check, 150);
    };
    check();
  })" >/dev/null || true
  ab wait 450
}

# The recorder only emits frames when the page changes, so stepping scroll keeps views on screen.
scroll_sidebar() {
  local steps="$1" px="$2"
  for _ in $(seq "$steps"); do
    ab eval "document.querySelector('aside.sidebar')?.scrollBy({top: $px, behavior: 'smooth'})" >/dev/null
    ab wait 550
  done
}

reset_sidebar() {
  ab eval "document.querySelector('aside.sidebar')?.scrollTo({top: 0})" >/dev/null
}

start_clip() {
  local name="$1"
  if [[ -n "$SEGMENTS_DIR" ]]; then
    ab record start "${SEGMENTS_DIR}/${name}" --fps "$FPS"
  fi
}

stop_clip() {
  if [[ -n "$SEGMENTS_DIR" ]]; then
    ab record stop
  fi
}

capture_slides() {
  local out_dir="$1"
  local slides_url="file://${REPO_ROOT}/demo/slides.html"
  printf '[demo] Capturing %sx%s slides -> %s\n' "$WIDTH" "$HEIGHT" "$out_dir"
  for slide in intro kalman outro; do
    ab open "${slides_url}#${slide}"
    ab set viewport "$WIDTH" "$HEIGHT"
    ab wait 400
    ab screenshot "${out_dir}/slide_${slide}.png"
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

if [[ -n "$SEGMENTS_DIR" ]]; then
  mkdir -p "$SEGMENTS_DIR"
  capture_slides "$SEGMENTS_DIR"
fi

printf '[demo] Opening UI in agent-browser session=%s (%sx%s @ %sfps)\n' "$SESSION" "$WIDTH" "$HEIGHT" "$FPS"
ab open "${API_URL}/"
ab set viewport "$WIDTH" "$HEIGHT"
ab wait --text "Delhi Bus Journey Planner"
wait_for_tiles

if [[ -z "$SEGMENTS_DIR" ]]; then
  printf '[demo] Starting single-file WebM recording -> %s (%sx%s @ %sfps)\n' "$OUTPUT_WEBM" "$WIDTH" "$HEIGHT" "$FPS"
  ab record start "$OUTPUT_WEBM" --fps "$FPS"
fi

# Segment 1: Live journey planning (Kashmere Gate ISBT -> Nehru Place, Tue 08:30)
printf '[demo] Segment 1: Live journey query\n'
start_clip "seg_01_live.webm"
ab click "#preset-example-live"
ab click "#plan-btn"
ab wait --text "Option 1"
wait_for_tiles
ab wait 1500
scroll_sidebar 4 110
ab wait 1200
reset_sidebar
ab wait 600
stop_clip

# Segment 2: Oct 22 replay on 717A
printf '[demo] Segment 2: Oct 22 replay on 717A\n'
start_clip "seg_02_replay_717a.webm"
ab click "#preset-example-replay"
ab click "#plan-btn"
ab wait --text "717AUP"
ab eval "Array.from(document.querySelectorAll('#options-list .option-card')).find((card) => card.textContent.includes('717AUP'))?.click()"
ab wait --text "model_replay"
wait_for_tiles
ab wait 1000
scroll_sidebar 6 120
ab wait 1200
reset_sidebar
ab wait 600
stop_clip

# Segment 3: Oct 22 replay off 717A abstains
printf '[demo] Segment 3: Oct 22 replay off 717A abstains\n'
start_clip "seg_03_replay_abstain.webm"
ab click "#preset-replay-abstain"
ab click "#plan-btn"
ab wait --text "Not enough data"
ab wait 2500
stop_clip

# Pause live poller now so the >45s stale clock advances while the viewer watches the replay demo
printf '[demo] Pausing live poller via POST /live/pause (threshold=%ss)\n' "$actual_threshold"
curl -sf -X POST "${API_URL}/live/pause?reason=demo_stale_abstention" >/dev/null

# Segment 4a: Marginal-transfer replay Stages 1 & 2 (initial advice + growing delay)
printf '[demo] Segment 4a: Marginal-transfer replay demo (Stages 1 & 2)\n'
start_clip "seg_04_transfer_early.webm"
ab click "#replay-demo-btn"
ab wait --text "Marginal-Transfer Replay Demo"
ab wait --text "Hamdard Nagar / Sangam Vihar"
wait_for_tiles
ab wait 2200
printf '[demo]   Stage 2: Growing delay at Badarpur M B Road / Rajiv Gandhi Stadium\n'
ab click "#stage-tabs button:nth-child(2)"
ab wait 2500
stop_clip

# Segment 4b: Marginal-transfer replay Stages 3 & 4 (advice flip + observed missed outcome)
printf '[demo] Segment 4b: Marginal-transfer replay demo (Stages 3 & 4)\n'
start_clip "seg_04_transfer_late.webm"
printf '[demo]   Stage 3: Advice flip at Tuglaqabad MB Road / Okhla more\n'
ab click "#stage-tabs button:nth-child(3)"
ab wait 2200
scroll_sidebar 3 110
ab wait 1000
reset_sidebar
printf '[demo]   Stage 4: Observed Oct 22 outcome reveal (missed by 27m 43s)\n'
ab click "#stage-tabs button:nth-child(4)"
ab wait 2200
scroll_sidebar 4 115
ab wait 1500
reset_sidebar
stop_clip

ab click "#close-demo-btn"
ab wait 800

# Wait until feed age strictly exceeds stale_threshold_seconds (bounded timeout)
printf '[demo] Waiting for live feed age to exceed %ss (timeout=%ss)...\n' "$actual_threshold" "$STALE_WAIT_TIMEOUT_S"
wait_start="$(date +%s)"
while true; do
  status_json="$(curl -sf "${API_URL}/live/status")"
  stale_ready="$(printf '%s' "$status_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); age=d.get("current_feed_age_seconds") or 0; thr=d.get("stale_threshold_seconds") or 45; print(f"{age:.1f} {1 if age > thr + 1 else 0}")')"
  age_val="${stale_ready%% *}"
  ready_flag="${stale_ready##* }"
  printf '[demo]   feed_age=%ss (threshold=%ss)\n' "$age_val" "$actual_threshold"
  if [[ "$ready_flag" == "1" ]]; then
    break
  fi
  if (( $(date +%s) - wait_start > STALE_WAIT_TIMEOUT_S )); then
    printf '[demo] Timed out waiting for stale feed (>%ss).\n' "$STALE_WAIT_TIMEOUT_S" >&2
    exit 1
  fi
  sleep 3
done

# Segment 5: Explicit stale-feed abstention in UI ("Not enough data")
printf '[demo] Segment 5: Triggering stale-feed abstention in UI\n'
start_clip "seg_05_stale.webm"
ab click "#preset-example-live"
ab click "#plan-btn"
ab wait --text "Not enough data"
ab wait 3000
stop_clip

# Segment 6: Resume live poller and show UI recovery
printf '[demo] Segment 6: Resuming live poller via POST /live/resume and recovering UI\n'
curl -sf -X POST "${API_URL}/live/resume?poll_now=true" >/dev/null
start_clip "seg_06_recovery.webm"
ab click "#abstention-retry-btn"
ab wait --text "Option 1"
wait_for_tiles
ab wait 2500
stop_clip

if [[ -z "$SEGMENTS_DIR" ]]; then
  printf '[demo] Stopping single-file recording\n'
  ab record stop
else
  printf '[demo] Concatenating silent clips -> %s\n' "$OUTPUT_WEBM"
  concat_list="${SEGMENTS_DIR}/concat.txt"
  : > "$concat_list"
  for clip in seg_01_live.webm seg_02_replay_717a.webm seg_03_replay_abstain.webm seg_04_transfer_early.webm seg_04_transfer_late.webm seg_05_stale.webm seg_06_recovery.webm; do
    printf "file '%s'\n" "${SEGMENTS_DIR}/${clip}" >> "$concat_list"
  done
  ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i "$concat_list" \
    -c:v libvpx -b:v 2500k -crf 18 -r "$FPS" -an "$OUTPUT_WEBM"
fi

printf '[demo] Checking console errors\n'
ab errors --json || true

printf '[demo] Transcoding silent MP4 -> %s\n' "$OUTPUT_MP4"
ffmpeg -y -hide_banner -loglevel error -i "$OUTPUT_WEBM" \
  -vf "scale=${WIDTH}:${HEIGHT}:force_original_aspect_ratio=decrease,pad=${WIDTH}:${HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0x0b1120,setsar=1" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -movflags +faststart -an "$OUTPUT_MP4"

if [[ "$NARRATE" == "1" ]]; then
  printf '[demo] Building narrated Full HD MP4 -> %s\n' "$NARRATED_MP4"
  (
    cd "$REPO_ROOT/backend"
    uv run python "$REPO_ROOT/demo/build_narrated_demo.py" \
      --segments-dir "$SEGMENTS_DIR" \
      --audio-dir "$AUDIO_DIR" \
      --output "$NARRATED_MP4" \
      --width "$WIDTH" \
      --height "$HEIGHT" \
      --fps "$FPS"
  )
fi

printf '[demo] Probing outputs with ffprobe\n'
ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=codec_type,codec_name,width,height,r_frame_rate "$OUTPUT_MP4"
if [[ "$NARRATE" == "1" && -f "$NARRATED_MP4" ]]; then
  ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate "$NARRATED_MP4"
fi
printf '[demo] Done.\n'
