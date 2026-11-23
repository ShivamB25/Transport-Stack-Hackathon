# Delhi Real-Time Bus Travel-Time Model and Journey Planner (PS1)

End-to-end Delhi DTC bus travel-time modelling, OpenTripPlanner 2.8.1 candidate routing, model-timed FastAPI journey planner, live GTFS-RT polling with explicit abstention, Leaflet map UI with marginal-transfer replay demo, and Google Routes bus-only comparator.

- **Technical note (modelling, blending, selection, abstention):** [`docs/technical-note.md`](docs/technical-note.md)
- **Frozen evaluation protocol:** [`docs/eval-protocol.md`](docs/eval-protocol.md)
- **OTP 2.8.1 setup & GraphQL notes:** [`docs/otp.md`](docs/otp.md)
- **Recorded demo walkthrough:** [`demo/demo.webm`](demo/demo.webm) (script: [`demo/record_demo.sh`](demo/record_demo.sh))
- **Reports:** [`reports/eval-full.md`](reports/eval-full.md), [`reports/eval-baselines.md`](reports/eval-baselines.md), [`reports/selection.json`](reports/selection.json), [`reports/coverage-717a.md`](reports/coverage-717a.md), [`reports/data-quality.md`](reports/data-quality.md), [`reports/google-comparator.md`](reports/google-comparator.md), [`reports/otp-smoke.md`](reports/otp-smoke.md)

---

## Architecture Summary

```text
October 2026 GPS Archive (R2, HTTP range reads)     Live GTFS-RT VehiclePositions.pb (x-api-key)
               │                                                    │
               ▼                                                    ▼
   extract_day.py / acquire.py                              live_poller.py (25 s cadence)
   SQLite → dedupe (~38%) → bbox → Parquet                  → data/live/{latest.parquet,
               │                                              route_stats.json, feed_state.json}
               ▼                                                    │
       stop_passages.py                                             │
   Geometric 60 m match (no trip_id join),                          │
   monotonic anchor chain, schedule-prop.                           │
   interpolation + per-trip coverage                                │
               │                                                    │
               ▼                                                    │
    features.py + model.py + evaluate.py                            │
   Whole-day splits, causal replay, rungs 0–4                       │
   (LightGBM quantile + CQR conformal on Oct 15)                    │
   → selection.json (ships Rung 4) + data/model/                    │
               │                                                    │
               └────────────────────────┬───────────────────────────┘
                                        ▼
  OTP 2.8.1 (:3102, GraphQL planConnection) ──► FastAPI (:3100, src/ps1/api.py)
   Unmodified candidate router                   retime.py: Rung 4 (717A) + live/historical
                                                 tiered fallback (city-wide), 192 joint
                                                 door-to-door scenarios, explicit abstention
                                                 when GPS stale/insufficient
                                                        │
                                                        ▼
                                           Leaflet UI (GET /, port 3100)
                                            Journey form, map polylines, live/replay badge,
                                            Not enough data abstention, 4-stage marginal-
                                            transfer replay demo (GET /replay/demo)
```

---

## Pinned Versions & Runtime Paths

All runtime and library versions used to build, evaluate, and serve PS1 are pinned:

| Component | Pinned Version | Location / Source |
|---|---|---|
| Python | `3.12.3` | `/home/factory-user/ps1-scratch/venv/bin/python` |
| Java (Temurin LTS) | `21.0.13.1` (`21.0.13.1+2-LTS`) | `/home/factory-user/ps1-scratch/jdk/bin/java` |
| OpenTripPlanner | `2.8.1` (`otp-shaded-2.8.1.jar`) | Maven Central (`-Xmx2500m` hard heap cap) |
| Static GTFS | Feed v32 (`delhi_buses_static_gtfs_v1.zip`, 6,862 stops, 2,574 routes) | `$PS1_DATA_DIR/gtfs/delhi_buses_static_gtfs_v1.zip` |
| Street Network OSM | BBBike `NewDelhi.osm.pbf` (37.5 MB) | `/home/factory-user/ps1-scratch/otp/NewDelhi.osm.pbf` |
| Map UI Library | Leaflet `1.9.4` + OpenStreetMap tiles | `https://unpkg.com/leaflet@1.9.4` |
| `duckdb` | `2.0.1` | `requirements.txt` |
| `py7zr` | `1.1.4` | `requirements.txt` |
| `httpx` | `0.28.1` | `requirements.txt` |
| `fastapi` | `0.165.0` | `requirements.txt` |
| `uvicorn` | `0.54.0` | `requirements.txt` |
| `lightgbm` | `4.7.0` | `requirements.txt` |
| `scikit-learn` | `1.9.1` | `requirements.txt` |
| `numpy` | `2.5.5` | `requirements.txt` |
| `pyarrow` | `26.0.1` | `requirements.txt` |
| `gtfs-realtime-bindings` | `3.0.0` | `requirements.txt` |
| `protobuf` | `7.36.2` | `requirements.txt` |
| `python-dotenv` | `1.2.4` | `requirements.txt` |
| `pytest` | `9.2.0` | `requirements.txt` |
| `ruff` | `0.17.6` | `requirements.txt` |

---

## Data-Access Notes & Secrets

- **External data root:** datasets, Parquet files, GTFS extracts, OTP jars/graphs, model artifacts, live snapshots, and Google API caches live **outside the repo** under `PS1_DATA_DIR` (default `/home/factory-user/ps1-scratch/data`). Never commit Parquet, SQLite, OTP graphs, `.jar`, venvs, or `.env` files (all gitignored in `.gitignore`).
- **API keys (`.env`, gitignored):** place credentials in the repository root `.env` file. Keys are loaded in-process by `python-dotenv` and are never logged, echoed, or committed:
  - `DELHI_TS_API_KEY`: portal `x-api-key` for `https://dts-backend.transportstack.in` (`static_gtfs` and live `realtime_gtfs/VehiclePositions.pb`). Note: always query the backend host `dts-backend.transportstack.in` (the SPA host returns HTML with HTTP 200).
  - `GOOGLE_MAPS_API_KEY`: Google Routes API (`directions/v2:computeRoutes`, bus-only comparator; hard cap 300 calls mission-wide; responses cached under `$PS1_DATA_DIR/google/`).
- **October 2026 GPS archive:** hosted as a 16.1 GB non-solid LZMA2 7z on Cloudflare R2 (`delhi-dtc-bus-gtfs-rt-2026-10.7z`). `src/ps1/range_reader.py` extracts one day at a time via HTTP Range requests and always sends `User-Agent: curl/8.5.0`. Intermediate SQLite files are deleted immediately after Parquet conversion.
- **Environment variables (`src/ps1/config.py`):**
  - `PS1_DATA_DIR` (default `/home/factory-user/ps1-scratch/data`)
  - `PS1_OTP_URL` (default `http://localhost:3102/otp/gtfs/v1`)
  - `PS1_API_PORT` (default `3100`), `PS1_OTP_PORT` (default `3102`)
  - `PS1_MODE` (`auto` | `live` | `replay`, default `auto`)
  - `PS1_ENABLE_LIVE_POLLER` (`1` | `0`, default `1`)
  - `PS1_LIVE_POLL_INTERVAL_S` (default `25`)
  - `PS1_LIVE_STALE_THRESHOLD_S` (default **`600`** seconds = 10 minutes; override e.g. `PS1_LIVE_STALE_THRESHOLD_S=45` for rapid abstention testing and the recorded demo)
  - `PS1_LIVE_ENTITY_MAX_AGE_S` (default `900` s)
  - `PS1_LIVE_MIN_VEHICLES` (default `10`)
  - `PS1_MIN_ROUTE_COVERAGE` (default `0.0`)

---

## Setup, Run & Test Commands

All commands below match `services.yaml` and the verified repository CLIs.

### 1. Install pinned Python dependencies

```bash
/home/factory-user/ps1-scratch/venv/bin/python -m pip install -r requirements.txt
```

### 2. Build and serve OpenTripPlanner 2.8.1 (port 3102)

One-time graph build (skip if `/home/factory-user/ps1-scratch/otp/graph/graph.obj` already exists; enforces `-Xmx2500m`):

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && scripts/otp_build.sh
```

Start OTP on port `3102` and verify its GraphQL healthcheck:

```bash
/home/factory-user/repos/Transport-Stack-Hackathon/scripts/otp_serve.sh
```

```bash
curl -sf -X POST http://localhost:3102/otp/gtfs/v1 \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ __schema { queryType { name } } }"}'
```

Stop OTP:

```bash
lsof -ti :3102 | xargs -r kill
```

### 3. Run one live poll (optional manual check) & start FastAPI + UI (port 3100)

Verify a single authenticated `VehiclePositions.pb` poll:

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && scripts/live_poller.sh --once
```

Start the FastAPI journey planner and Leaflet UI (default 600 s stale-feed threshold; requires OTP on port 3102):

```bash
PS1_DATA_DIR=/home/factory-user/ps1-scratch/data \
PS1_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
  /home/factory-user/ps1-scratch/venv/bin/python -m uvicorn ps1.api:app \
  --app-dir src --host 127.0.0.1 --port 3100
```

Healthcheck & OpenAPI docs:

```bash
curl -sf http://localhost:3100/health
# Interactive OpenAPI docs: http://localhost:3100/docs
# Leaflet Journey Planner UI: http://localhost:3100/
```

Stop FastAPI:

```bash
lsof -ti :3100 | xargs -r kill
```

### 4. Run tests, bytecode check, and linter

```bash
/home/factory-user/ps1-scratch/venv/bin/python -m pytest tests -q
/home/factory-user/ps1-scratch/venv/bin/python -m compileall -q src
/home/factory-user/ps1-scratch/venv/bin/ruff check src tests
```

---

## Live Mode, Replay Mode, and Stale-Feed Abstention

### Example queries (`GET /journeys`)

- **Live mode** (brief example: Kashmere Gate ISBT → Nehru Place, Tuesday 08:30 IST):
  ```bash
  curl -s 'http://localhost:3100/journeys?from=28.6675,77.2282&to=28.5494,77.2510&depart=2026-11-24T08:30:00%2B05:30'
  ```
- **Stop-name endpoints** (exact/alias resolution; no city-wide fuzzy matching):
  ```bash
  curl -s 'http://localhost:3100/journeys?from=Kashmere%20Gate%20ISBT&to=Nehru%20Place&depart=2026-11-24T08:30:00%2B05:30'
  ```
- **October replay mode** (causal October 2026 passages; `mode=replay`, `live_feed_contacted=false`):
  ```bash
  curl -s 'http://localhost:3100/journeys?from=28.6675,77.2282&to=28.5494,77.2510&depart=2026-10-22T08:30:00%2B05:30&mode=replay'
  ```
- **Marginal-transfer replay scenario API** (4-stage real Oct 22 run `2171_17_37` on 717AUP):
  ```bash
  curl -s 'http://localhost:3100/replay/demo'
  ```

### Stale-feed abstention threshold (`600 s` default vs `45 s` override)

- **Production default:** `PS1_LIVE_STALE_THRESHOLD_S=600` (10 minutes). Whenever live `VehiclePositions` age exceeds this threshold (or `fresh_vehicle_count < 10`, or no snapshot exists), `GET /journeys` in `live` mode returns `status: "insufficient_data"`, `options: []`, and `abstention.label: "not enough data"` with the stale-feed code, reason, feed age, and threshold. The UI suppresses all options and displays the red **Not enough data** panel.
- **Fast abstention testing / demo override (`PS1_LIVE_STALE_THRESHOLD_S=45`):**
  ```bash
  PS1_LIVE_STALE_THRESHOLD_S=45 \
  PS1_DATA_DIR=/home/factory-user/ps1-scratch/data \
  PS1_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
    /home/factory-user/ps1-scratch/venv/bin/python -m uvicorn ps1.api:app \
    --app-dir src --host 127.0.0.1 --port 3100

  # Pause background polling, wait >45 s, observe abstention, then resume:
  curl -sf -X POST 'http://localhost:3100/live/pause?reason=demo_stale_test'
  curl -sf http://localhost:3100/live/status
  # After >45 s, GET /journeys returns status="insufficient_data" ("not enough data")
  curl -sf -X POST 'http://localhost:3100/live/resume'
  ```
  The CLI wrapper also supports `scripts/live_poller.sh --status | --pause | --resume | --once`.

---

## Full Metrics Output & Regeneration Commands

### Regeneration commands

```bash
# 1. Regenerate one October GPS day end-to-end from R2 archive (31/31 days already acquired)
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.extract_day --day 2026-10-15 --output /tmp/day15.parquet

# 2. Reconstruct 717A stop passages (10 scoped train+holdout days) + reports/coverage-717a.md
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages --overwrite

# 3. Run full ladder evaluation (rungs 0, 1, 1h, 2, 3, 4), split-conformal calibration,
#    model selection, and model artifact export -> reports/eval-{baselines,full}.{csv,md}, reports/selection.json
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate

# 4. Independently re-apply the ladder selection rule to reports/eval-full.csv
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.selection reports/eval-full.csv
```

### Data & stop-passage coverage summary (`reports/data-quality.md`, `reports/coverage-717a.md`)

- **October 2026 GPS acquisition:** 31/31 days complete (`912,877,061` raw rows → `560,357,771` clean rows, `38.62%` removed, `0` duplicate groups, `0` bbox violations, `0` fractional `route_id`).
- **717A geometric stop passages (10 scoped days, GTFS v32):**
  - `DOWN` (`2170`, 46 stops): `1,281` trips, `58,926` stops (`29,940` observed / `28,986` interpolated = **50.81%** observed coverage).
  - `UP` (`2171`, 52 stops): `1,267` trips, `65,884` stops (`31,932` observed / `33,952` interpolated = **48.47%** observed coverage).

### Held-out model ladder & selection (`reports/eval-full.md`, `reports/selection.json`)

- **Whole-day splits:** Training `2026-10-05, 06, 07, 08, 13, 14, 15` (Rung 4 fit: `05–08, 13–14` [32,526 observed queries]; split-conformal CQR calibration: `2026-10-15` [4,694 observed queries, `q_conf = 2.366 s`, calibration coverage `76.118% raw -> 80.038% calibrated`]). Held out: `2026-10-22, 27, 28` (`13,316` observed-only queries). Excluded holidays: `2026-10-02, 20, 26`.
- **October 22 strict persistence gate (`observed-only`):** `UP` PASS (`84.795 s` vs `95.737 s`, 2,400 queries); `DOWN` PASS (`93.364 s` vs `101.726 s`, 2,195 queries).
- **Selected rung:** **`4`** (`61.736 s` MAE vs Rung 1 persistence `77.329 s`; delta **`-15.593 s`**, beating Rung 1 on `6/6` groups at identical coverage).
- **Ground-truth caveat:** `all-stop` rows (reported in full in `reports/eval-full.md` and `reports/eval-baselines.md`) include timetable-interpolated pseudo-ground-truth which biases toward Rung 0 (median AE `0.000 s` on 4/6 groups). The persistence gate and model-selection rule use `observed-only` exclusively.

#### Model selection summary (`observed-only`, 13,316 paired queries)

| Rung | Observed queries | Overall MAE s | Delta vs rung 1 s | Coverage matches rung 1 | Groups beating rung 1 | Interval cov % | Mean width s | Median width s |
|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 0 | 13316 | 87.245 | +9.917 | yes | 0/6 | - | - | - |
| 1 | 13316 | 77.329 | +0.000 | yes | 0/6 | - | - | - |
| 1h | 13316 | 74.527 | -2.802 | yes | 6/6 | - | - | - |
| 2 | 13316 | 69.532 | -7.797 | yes | 6/6 | - | - | - |
| 3 | 13316 | 69.486 | -7.843 | yes | 6/6 | - | - | - |
| **4** | **13316** | **61.736** | **-15.593** | **yes** | **6/6** | **79.010** | **143.998** | **81.434** |

#### Per-day, per-direction `observed-only` metrics (`reports/eval-full.md`)

| Day | Direction | Rung | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interp % | Interval cov % | Mean width s | Median width s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-22 | DOWN | 0 | 2195 | 101.726 | 34.000 | 187.200 | 29.248 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 1 | 2195 | 93.364 | 29.000 | 170.200 | 27.380 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 1h | 2195 | 92.013 | 24.000 | 167.600 | 24.328 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 2 | 2195 | 83.127 | 22.000 | 160.300 | 23.189 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 3 | 2195 | 82.893 | 22.301 | 159.622 | 23.098 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 4 | 2195 | 77.657 | 20.180 | 145.240 | 20.182 | 100.000 | 0.000 | 76.492 | 141.536 | 85.396 |
| 2026-10-22 | UP | 0 | 2400 | 95.737 | 32.000 | 190.200 | 27.042 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 1 | 2400 | 84.795 | 25.000 | 168.100 | 24.917 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 1h | 2400 | 80.873 | 22.000 | 155.050 | 21.792 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 2 | 2400 | 79.574 | 22.000 | 158.150 | 23.125 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 3 | 2400 | 79.531 | 21.804 | 156.929 | 23.083 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 4 | 2400 | 67.689 | 18.416 | 118.477 | 19.750 | 100.000 | 0.000 | 79.500 | 156.013 | 79.487 |
| 2026-10-27 | DOWN | 0 | 2375 | 75.542 | 34.000 | 156.600 | 29.305 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 1 | 2375 | 63.651 | 28.000 | 133.000 | 24.968 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 1h | 2375 | 63.525 | 24.000 | 135.200 | 21.642 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 2 | 2375 | 57.516 | 22.000 | 115.500 | 19.158 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 3 | 2375 | 57.460 | 21.850 | 114.421 | 18.947 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 4 | 2375 | 50.817 | 19.885 | 96.186 | 17.095 | 100.000 | 0.000 | 78.821 | 130.751 | 81.317 |
| 2026-10-27 | UP | 0 | 2371 | 73.799 | 30.000 | 150.000 | 25.053 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 1 | 2371 | 63.314 | 23.000 | 131.000 | 22.311 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 1h | 2371 | 59.716 | 22.000 | 120.000 | 19.654 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 2 | 2371 | 55.843 | 21.500 | 122.000 | 19.781 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 3 | 2371 | 55.899 | 20.930 | 121.345 | 19.739 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 4 | 2371 | 48.297 | 18.240 | 89.118 | 16.407 | 100.000 | 0.000 | 81.991 | 137.712 | 75.378 |
| 2026-10-28 | DOWN | 0 | 2049 | 70.615 | 34.000 | 159.400 | 29.527 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 1 | 2049 | 62.899 | 30.000 | 134.200 | 25.866 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 1h | 2049 | 59.280 | 22.000 | 132.600 | 22.401 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 2 | 2049 | 54.123 | 22.000 | 121.000 | 22.157 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 3 | 2049 | 54.122 | 22.038 | 120.564 | 22.108 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 4 | 2049 | 47.476 | 19.834 | 97.614 | 18.204 | 100.000 | 0.000 | 79.161 | 137.563 | 83.277 |
| 2026-10-28 | UP | 0 | 1926 | 108.839 | 33.000 | 224.000 | 31.049 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 1 | 1926 | 99.221 | 27.000 | 195.000 | 28.401 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 1h | 1926 | 94.714 | 22.000 | 188.500 | 26.272 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 2 | 1926 | 89.584 | 22.000 | 170.250 | 26.064 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 3 | 1926 | 89.590 | 23.011 | 167.451 | 25.857 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 4 | 1926 | 81.354 | 20.094 | 148.155 | 23.468 | 100.000 | 0.000 | 77.674 | 162.752 | 84.986 |

The complete 72-row table (including all-stop rows and history fallback counts) is in [`reports/eval-full.md`](reports/eval-full.md) and [`reports/eval-full.csv`](reports/eval-full.csv).

---

## Google Routes API Comparator Summary

**Comparator only, not ground truth:** Google Routes API reflects Google's schedule/traffic model on upcoming departures (past October dates return `routes: []`), not observed Delhi GPS arrivals. Ground-truth accuracy is measured strictly against observed October 2026 stop passages above.

- **Corpus:** 30 Delhi OD pairs × 3 slots (`08:30`, `14:00`, `18:30` IST on upcoming Tuesday `2026-11-24`) = **90/90 slots compared** (`0` missing/error, `3` non-bus `SUBWAY` itineraries filtered out so all 90 comparisons are strictly bus-only, `languageCode="en"`).
- **Mission-wide call budget:** **`92 / 300`** (`2` probe calls + `90` cached comparator calls; `208` remaining).

| Slice | Planned | Compared | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **All OD × Time Slots** | 90 | 90 | 49.9 min | 47.9 min | +123 s (+2.0 m) | +126 s | 371 s (6.2 m) | 96.7% |
| 08:30 IST (Morning Peak) | 30 | 30 | 45.8 min | 47.8 min | -121 s (-2.0 m) | -102 s | 310 s (5.2 m) | 96.7% |
| 14:00 IST (Midday) | 30 | 30 | 48.7 min | 47.9 min | +44 s (+0.7 m) | +66 s | 258 s (4.3 m) | 100.0% |
| 18:30 IST (Evening Peak) | 30 | 30 | 55.4 min | 48.0 min | +445 s (+7.4 m) | +376 s | 546 s (9.1 m) | 93.3% |

Regenerate `reports/google-comparator.{md,csv}` and `reports/google-manifest.json` from the local disk cache with **0 additional Google API calls** (requires OTP on 3102 and FastAPI on 3100):

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py \
  --date 2026-11-24 --cache-only
```

---

## Recorded Demo Walkthrough (`demo/demo.webm`)

`demo/demo.webm` was recorded with `agent-browser` via [`demo/record_demo.sh`](demo/record_demo.sh) and captures:

1. **Live journey planning:** Kashmere Gate ISBT → Nehru Place (Tue 08:30 IST) with the green `live` badge, arrival windows, per-leg provenance, and Leaflet route rendering.
2. **Complete 4-stage marginal-transfer replay journey:** real October 22, 2026 run `2171_17_37` (bus `DL1PD5280`, route `717AUP`, Badarpur Border (T) → Hauz Khas): initial timetable transfer recommendation (`717AUP -> D-7702UP`) → growing delay at Pul Pehladpur (`+211 s`) → advice flip at Surajkund Crossing (`+613 s`, 95% miss risk, recommends direct `774UP`) → actual Oct 22 outcome reveal (**MISSED by 7m 33s** at Ambedkar Nagar Depot).
3. **Explicit stale-feed abstention and recovery:** API started with `PS1_LIVE_STALE_THRESHOLD_S=45` (overriding the 600 s default), `POST /live/pause` invoked, waited until live feed age exceeded `45 s`, UI submitted live query and displayed **`Not enough data`** with stale-feed age/threshold and suppressed options, then `POST /live/resume` restored fresh `VehiclePositions` and clicking **Retry live query** recovered the live recommendations.

Re-record the demo (requires OTP running on 3102 and API running on 3100 with `PS1_LIVE_STALE_THRESHOLD_S=45`):

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  demo/record_demo.sh --session ps1-demo
```
