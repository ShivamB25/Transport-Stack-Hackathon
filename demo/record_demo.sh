#!/usr/bin/env bash
# Record Full HD (1920x1080) demo footage with agent-browser, timed to exact
# spoken cues in demo/audio/ (generated from demo/script.json):
#   1. Live journey query (Kashmere Gate ISBT -> Nehru Place) + leg breakdown
#   2. Oct 22 replay on 717A (Badarpur Border (T) -> Kapashera Border) with model_replay legs
#   3. Oct 22 replay off 717A (Kashmere Gate ISBT -> Nehru Place), which abstains
#   4. 4-stage marginal-transfer replay (Oct 22 run 2171_17_09_490):
#      - Early clip: Stage 1 (0-14.2s) -> Stage 2 click synced to "Just minutes later..." (14.2-20.7s)
#      - Late clip:  Stage 3 (0-8.9s)  -> Stage 4 click synced to "Stage four reveals..." (8.9-20.0s)
#   5. Explicit stale-feed abstention ("Not enough data") synced to >45s freshness threshold
#   6. Live recovery after resuming the poller and retrying
#
# Also captures 1920x1080 PNG slides from demo/slides.html (intro, PR #28 Kalman fallback, outro),
# transcodes demo/demo.mp4, and with --narrate muxes the pre-generated Sarvam cloned-voice WAVs
# (demo/audio/) into demo/demo_narrated.mp4 + demo/demo_narrated.srt (no SARVAM_API_KEY needed).
#
# Prerequisites on the recording machine:
#   - OTP 2.8.1 running on http://localhost:3102/otp/gtfs/v1 (otp/serve.sh)
#   - FastAPI running on http://localhost:3100 with DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45
#   - agent-browser, ffmpeg, ffprobe, curl, python3
#
# Usage:
#   demo/record_demo.sh --narrate --width 1920 --height 1080 --fps 15
#   demo/record_demo.sh                           # silent Full HD demo/demo.webm + demo/demo.mp4

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
CLIP_START_MS=0

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

if [[ "$NARRATE" == "1" ]]; then
  if [[ ! -f "${AUDIO_DIR}/manifest.json" ]]; then
    printf '[demo] Missing %s/manifest.json. Pull demo/audio/ before running --narrate.\n' "$AUDIO_DIR" >&2
    exit 1
  fi
  for beat in 01_intro 02_live_query 03_replay_717a 04_replay_abstain 05_kalman_pr28 06_transfer_s1_s2 07_transfer_s3_s4 08_stale_feed 09_recovery 10_outro; do
    if [[ ! -f "${AUDIO_DIR}/${beat}.wav" ]]; then
      printf '[demo] Missing pre-generated audio %s/%s.wav\n' "$AUDIO_DIR" "$beat" >&2
      exit 1
    fi
  done
fi

ab() {
  agent-browser --session "$SESSION" "$@"
}

now_ms() {
  python3 -c 'import time; print(int(time.monotonic() * 1000))'
}

# Wait until elapsed wall-clock time since start_clip reaches target_ms so every
# visual cue lands on the exact spoken narration timestamp regardless of API latency.
wait_until_clip_ms() {
  local target_ms="$1"
  local now elapsed rem
  now="$(now_ms)"
  elapsed=$(( now - CLIP_START_MS ))
  rem=$(( target_ms - elapsed ))
  if (( rem > 0 )); then
    ab wait "$rem"
  fi
}

# agent-browser only emits video frames when the page repaints, which otherwise collapses
# static holds. A 1x1 nearly transparent corner pulse keeps real wall-clock pacing at FPS.
install_keepalive() {
  local interval_ms=$(( 1000 / FPS ))
  (( interval_ms < 33 )) && interval_ms=33
  ab eval "(() => {
    if (window.__demoKeepAlive) clearInterval(window.__demoKeepAlive);
    let el = document.getElementById('__demo-paint-tick');
    if (!el) {
      el = document.createElement('div');
      el.id = '__demo-paint-tick';
      el.style.cssText = 'position:fixed;right:0;bottom:0;width:2px;height:2px;pointer-events:none;z-index:999999;background:#0b1120;opacity:0.01;';
      document.body.appendChild(el);
    }
    let flip = false;
    window.__demoKeepAlive = setInterval(() => {
      flip = !flip;
      el.style.opacity = flip ? '0.02' : '0.01';
    }, ${interval_ms});
  })()" >/dev/null
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
  ab wait 350
}

scroll_sidebar() {
  local steps="$1" px="$2" step_ms="${3:-650}"
  for _ in $(seq "$steps"); do
    ab eval "document.querySelector('aside.sidebar')?.scrollBy({top: $px, behavior: 'smooth'})" >/dev/null
    ab wait "$step_ms"
  done
}

reset_sidebar() {
  ab eval "document.querySelector('aside.sidebar')?.scrollTo({top: 0})" >/dev/null
}

start_clip() {
  local name="$1"
  CLIP_START_MS="$(now_ms)"
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
    ab open "${slides_url}?slide=${slide}#${slide}"
    ab set viewport "$WIDTH" "$HEIGHT"
    ab wait 500
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
install_keepalive

if [[ -z "$SEGMENTS_DIR" ]]; then
  printf '[demo] Starting single-file WebM recording -> %s (%sx%s @ %sfps)\n' "$OUTPUT_WEBM" "$WIDTH" "$HEIGHT" "$FPS"
  ab record start "$OUTPUT_WEBM" --fps "$FPS"
fi

# Segment 1 (02_live_query.wav: 14.9s speech / 15.5s slot):
#   0-10.2s: load Kashmere Gate -> Nehru Place live query, hold arrival window & quiet-hour comparison
#   10.2-15.5s: scroll smoothly into per-leg breakdown ("...and clear data sources for every leg.")
printf '[demo] Segment 1: Live journey query (target 15.5s)\n'
start_clip "seg_01_live.webm"
ab click "#preset-example-live"
ab click "#plan-btn"
ab wait --text "Option 1"
wait_for_tiles
wait_until_clip_ms 10200
scroll_sidebar 4 115 700
wait_until_clip_ms 15600
stop_clip
reset_sidebar

# Segment 2 (03_replay_717a.wav: 21.8s speech / 22.4s slot):
#   0-12.5s: load Oct 22 replay on 717A, expand 717AUP card & South Delhi route
#   12.5-22.4s: scroll smoothly through BOARD, RIDE (model_replay, LightGBM+conformal), ALIGHT legs
printf '[demo] Segment 2: Oct 22 replay on 717A (target 22.4s)\n'
start_clip "seg_02_replay_717a.webm"
ab click "#preset-example-replay"
ab click "#plan-btn"
ab wait --text "717AUP"
ab eval "Array.from(document.querySelectorAll('#options-list .option-card')).find((card) => card.textContent.includes('717AUP'))?.click()"
ab wait --text "model_replay"
wait_for_tiles
wait_until_clip_ms 12200
scroll_sidebar 6 120 750
wait_until_clip_ms 22400
stop_clip
reset_sidebar

# Segment 3 (04_replay_abstain.wav: 12.2s speech / 12.7s slot):
#   0-12.7s: replay corridor off 717A -> hold explicit "Not enough data" abstention card
printf '[demo] Segment 3: Oct 22 replay off 717A abstains (target 12.7s)\n'
start_clip "seg_03_replay_abstain.webm"
ab click "#preset-replay-abstain"
ab click "#plan-btn"
ab wait --text "Not enough data"
wait_until_clip_ms 12800
stop_clip

# Pause live poller now so the >45s stale clock advances while recording the ~41s replay demo
printf '[demo] Pausing live poller via POST /live/pause (threshold=%ss)\n' "$actual_threshold"
curl -sf -X POST "${API_URL}/live/pause?reason=demo_stale_abstention" >/dev/null

# Segment 4a (06_transfer_s1_s2.wav: 20.1s speech / 20.7s slot):
#   0-14.2s: Stage 1 ("...recommends a transfer at Hamdard Nagar." ends at 14.9s)
#   14.2-20.7s: Click Stage 2 right as "Just minutes later, incoming GPS delay..." starts
printf '[demo] Segment 4a: Marginal-transfer replay Stages 1 & 2 (target 20.7s; Stage 2 at 14.2s)\n'
start_clip "seg_04_transfer_early.webm"
ab click "#replay-demo-btn"
ab wait --text "Marginal-Transfer Replay Demo"
ab wait --text "Hamdard Nagar / Sangam Vihar"
wait_for_tiles
wait_until_clip_ms 14200
printf '[demo]   Stage 2: Growing delay at Badarpur M B Road / Rajiv Gandhi Stadium\n'
ab click "#stage-tabs button:nth-child(2)"
wait_until_clip_ms 20700
stop_clip

# Segment 4b (07_transfer_s3_s4.wav: 19.5s speech / 20.0s slot):
#   0-8.9s: Stage 3 ("...advice flips to the direct 717A bus..." ends at 9.35s) + peek comparison
#   8.9-20.0s: Click Stage 4 right on "Stage four reveals...", hold banner+telemetry, then scroll outcome cards
printf '[demo] Segment 4b: Marginal-transfer replay Stages 3 & 4 (target 20.0s; Stage 4 at 8.9s)\n'
start_clip "seg_04_transfer_late.webm"
printf '[demo]   Stage 3: Advice flip at Tuglaqabad MB Road / Okhla more\n'
ab click "#stage-tabs button:nth-child(3)"
wait_until_clip_ms 4200
scroll_sidebar 3 115 650
wait_until_clip_ms 8800
reset_sidebar
printf '[demo]   Stage 4: Observed Oct 22 outcome reveal (missed by 27m 43s)\n'
ab click "#stage-tabs button:nth-child(4)"
wait_until_clip_ms 13800
scroll_sidebar 4 115 700
wait_until_clip_ms 20100
stop_clip

reset_sidebar
ab click "#close-demo-btn"
ab wait 600

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
  sleep 2
done

# Segment 5 (08_stale_feed.wav: 14.6s speech / 15.1s slot):
#   0-3.8s: Show UI with Stale GPS badge ("A trustworthy planner also knows when its own GPS feed goes stale.")
#   3.8-15.1s: Trigger live query right on "Here we pause..." -> hold "Not enough data" stale_feed card
printf '[demo] Segment 5: Triggering stale-feed abstention in UI (target 15.1s; click at 3.8s)\n'
start_clip "seg_05_stale.webm"
ab click "#preset-example-live"
wait_until_clip_ms 3800
ab click "#plan-btn"
ab wait --text "Not enough data"
wait_until_clip_ms 15200
stop_clip

# Segment 6 (09_recovery.wav: 7.0s speech / 7.6s slot):
#   0-7.6s: Resume live poller + click Retry -> wait for clean Leaflet tiles -> hold restored Option 1
printf '[demo] Segment 6: Resuming live poller and recovering UI (target 7.6s)\n'
curl -sf -X POST "${API_URL}/live/resume?poll_now=true" >/dev/null
start_clip "seg_06_recovery.webm"
ab click "#abstention-retry-btn"
ab wait --text "Option 1"
wait_for_tiles
wait_until_clip_ms 7700
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
      --fps "$FPS" \
      --use-existing-audio
  )
fi

printf '[demo] Probing outputs with ffprobe\n'
ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=codec_type,codec_name,width,height,r_frame_rate "$OUTPUT_MP4"
if [[ "$NARRATE" == "1" && -f "$NARRATED_MP4" ]]; then
  ffprobe -v error -show_entries format=duration,size,bit_rate -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate "$NARRATED_MP4"
fi
printf '[demo] Done.\n'
