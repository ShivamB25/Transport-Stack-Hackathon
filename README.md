# Delhi Bus Journey Planner (PS1)

**Team DROIDERS · Shivam Bansal**

Causal bus travel-time modelling, calibrated `[p10, p90]` arrival windows, joint transfer-miss risk, and freshness-gated abstention for Delhi DTC buses—built over 31 days of October 2026 GPS data (`560.4M` clean pings), static GTFS v32, live GTFS-RT `VehiclePositions`, and OpenTripPlanner 2.8.1.

OpenTripPlanner only proposes candidate routes; this engine owns transit leg timing, transfer risk, uncertainty intervals, and abstention.

## Demo

Watch the 3m 19s Full HD narrated walkthrough: **[`demo/demo_narrated.mp4`](demo/demo_narrated.mp4)** ([subtitles](demo/demo_narrated.srt)).

1. **Live query** (Kashmere Gate ISBT → Nehru Place) with live GTFS-RT ingestion, `[p10, p90]` windows, quiet-hour comparison, and per-leg provenance.
2. **Oct 22 causal replay on 717A** (Badarpur Border → Kapashera Border) with `model_replay` LightGBM + split-conformal leg timings.
3. **Off-717A replay abstention** returning explicit `Not enough data` instead of guessing.
4. **PR #28 scalar Kalman ratio filter** for unmodelled routes (`80.3 s → 57.3 s` offline MAE).
5. **4-stage missed-transfer replay** (actual Oct 22 GPS, run `2171_17_09_490`): timetable transfer advice flips to direct `717AUP` before boarding; real GPS confirms the connection was missed by `27m 43s`.
6. **Stale-feed abstention & live recovery** (poller paused past 45 s demo threshold → `stale_feed` → resumed).

## Headline Results

All figures come from committed reproducible reports under [`reports/`](reports/):

| Evaluation | Metric | Baseline | Result | Report |
|---|---|---:|---:|---|
| GPS archive cleaning (31 days) | Clean pings retained | 912.9M raw | **560.4M** (0 dupes, 0 bbox violations) | [`data-quality.md`](reports/data-quality.md) |
| 717A held-out (Oct 22, 27, 28; `n = 13,299` observed-only) | MAE | 87.3 s timetable / 72.8 s persist | **60.2 s** (**−31.0%**) | [`eval-full.md`](reports/eval-full.md) |
| 717A split-conformal intervals (80% target) | Pooled `[p10, p90]` coverage | — | **79.2%** (81.5 s median width) | [`selection.json`](reports/selection.json) |
| Unmodelled routes fallback (PR #28; 6 routes, `n = 16,247`, offline) | Scalar Kalman ratio MAE | 80.3 s timetable / 63.1 s prior chain | **57.3 s** (**−28.6%**) | `fallback_check --reconstruct` |
| Nov 25 5-hour morning live check (`722/722` polls, `n = 177` matched legs) | Live 717A MAE & coverage | 393.3 s timetable | **227.5 s** (**96.6%** in `[p10, p90]`) | [`live-check-2026-11-25.md`](reports/live-check-2026-11-25.md) |
| Google Routes comparator (Nov 24, `n = 19` compared slots) | Door-to-door containment | — | **94.7%** inside `[p10, p90]` | [`google-comparator.md`](reports/google-comparator.md) |

## Architecture

- **Data & causality (`delhi_transit.data`):** Streams 31 days of October GPS into 9.96 GB of clean Parquet, matches pings to 717A stops geometrically (60 m radius; never joining GPS `trip_id` to GTFS), and stamps every passage with strict `available_at` causality.
- **Timing models (`delhi_transit.model`, `planner/rides.py`):** Shipped Rung 4 (LightGBM quantile + split-conformal) serves route 717A (`2171` UP / `2170` DOWN). All other routes use live GTFS-RT vehicle speeds first (`model_live`) and otherwise a causal per-stretch, per-day scalar Kalman ratio filter (`Q = 0.01`, `R = 0.05`) degrading cleanly to `historical_fallback` and `timetable_fallback`.
- **Joint journey simulation (`delhi_transit.planner`):** Runs 192 joint Monte Carlo scenarios over OTP 2.8.1 candidates to compute transfer miss probabilities and end-to-end `[p10, p90]` windows (per-leg `p90`s are never summed).
- **Live & replay (`delhi_transit.live`, `api`):** Polls live `VehiclePositions` every 25 s in live mode; replays October 2026 departures strictly from passages with `available_at < depart`.
- **Honest abstention:** Returns `status: "insufficient_data"` with explicit codes (`stale_feed`, `feed_unavailable`, `insufficient_coverage`, `insufficient_route_coverage`, `replay_data_unavailable`) when telemetry is stale or routes lack observations.

## Quickstart

Requires Python 3.12 (`uv`), Bun, Java 21 (`DELHI_TRANSIT_JAVA`), a `.env` file copied from `.env.example` (`DELHI_TS_API_KEY`; `GOOGLE_MAPS_API_KEY` only for the comparator), and paths configured via `DELHI_TRANSIT_SCRATCH_DIR` (or individually via `DELHI_TRANSIT_DATA_DIR`, `DELHI_TRANSIT_OTP_DIR`, `DELHI_TRANSIT_OTP_URL` — see [`backend/src/delhi_transit/core/config.py`](backend/src/delhi_transit/core/config.py)).

### Native

```bash
otp/build.sh                                                          # one-time: fetch OTP 2.8.1 + OSM extract and build graph (~1.5 min)
otp/serve.sh                                                          # run OTP on :3102
(cd frontend && bun install --frozen-lockfile && bun run build)       # build React UI -> frontend/dist
cd backend && uv sync --frozen
DELHI_TRANSIT_DATA_DIR=/path/to/data \
DELHI_TRANSIT_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
  uv run uvicorn delhi_transit.api:app --host 127.0.0.1 --port 3100
```

### Docker Compose

```bash
docker compose --profile build run --rm otp-build   # one-time graph build
docker compose up -d --build                        # OTP (:3102) + API/UI (:3100)
```

### Example API Calls

Open the UI at `http://localhost:3100/` or OpenAPI docs at `http://localhost:3100/docs`:

```bash
curl -s 'http://localhost:3100/health'
curl -s 'http://localhost:3100/journeys?from=Kashmere%20Gate%20ISBT&to=Nehru%20Place'
curl -s 'http://localhost:3100/journeys?from=Badarpur%20Border%20(T)&to=Kapashera%20Border&depart=2026-10-22T08:30:00%2B05:30'
curl -s 'http://localhost:3100/replay/demo'
```

### Common Checks & Pipelines

```bash
# Backend checks (from backend/)
uv run ruff check src tests && uv run python -m compileall -q src && uv run pytest -q

# Frontend checks (from frontend/)
bun run lint && bun run typecheck && bun run build

# Recompute passages, Rung 4 evaluation, or PR #28 Kalman fallback (from backend/)
uv run python -m delhi_transit.data.stop_passages --overwrite
uv run python -m delhi_transit.model.evaluate
uv run python -m delhi_transit.data.fallback_check --reconstruct
```

## Key Caveats

- **Model scope:** Learned Rung 4 covers route 717A (`2171`/`2170`) only; unmodelled routes use live speeds first and the scalar Kalman ratio filter otherwise (28.6% gain is measured offline on October data and not yet live-verified).
- **Archive window:** Models and replay use October 2026 GPS on static GTFS v32; ~50% of 717A stop passages are directly observed and the rest interpolated, so gating uses `observed-only` stop pairs exclusively.
- **Comparator boundary:** Google Routes is an external comparator (19/90 slots on Nov 24, no October/past dates), never ground truth.
- **Local verification:** No CI is configured; run the backend and frontend checks above before merging.

## Documentation & Layout

- [`docs/technical-note.md`](docs/technical-note.md) — full methodology, Kalman filter equations, and error analysis
- [`docs/eval-protocol.md`](docs/eval-protocol.md) — frozen train/test split and gating protocol
- [`docs/otp.md`](docs/otp.md) — OTP 2.8.1 graph setup and configuration
- [`reports/`](reports/) — committed data-quality, evaluation, comparator, and live-check reports
- `backend/` (`uv` Python 3.12), `frontend/` (`bun` React 19 + Vite + Tailwind v4 + Leaflet), `otp/`, `livecheck/`, `demo/`
