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
| `pydantic` | `2.14.4` | `requirements.txt` |
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
  - `PS1_LIVE_ENTITY_MAX_AGE_S` (default `900` s source-retention cap; usable entities are also limited by `PS1_LIVE_STALE_THRESHOLD_S`, so the stock effective maximum is `600` s measured from each entity timestamp)
  - `PS1_LIVE_MIN_VEHICLES` (default `10`)
  - `PS1_MIN_ROUTE_COVERAGE` (optional additional average-coverage floor, default `0.0`; even at zero, the API abstains when no candidate has any route-level support)

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
- **Marginal-transfer replay scenario API** (4-stage real Oct 22 run `2171_17_09_490` on 717AUP):
  ```bash
  curl -s 'http://localhost:3100/replay/demo'
  ```

### Stale-feed abstention threshold (`600 s` default vs `45 s` override)

- **Production default:** `PS1_LIVE_STALE_THRESHOLD_S=600` (10 minutes). A fresh protobuf header is not sufficient: each entity timestamp must also be within the effective threshold before it contributes to route timing or `fresh_vehicle_count`. `GET /journeys` returns `status: "insufficient_data"` and `options: []` for a stale header (`stale_feed`), too few fresh vehicles (`insufficient_coverage`), no snapshot (`feed_unavailable`), or no route-supported candidate (`insufficient_route_coverage`). Sparse fallback legs remain visible inside a journey supported on another route. The UI suppresses options, displays the red **Not enough data** panel, and labels the badge for the actual reason (stale feed, unavailable feed, insufficient fleet/route coverage, or unavailable replay data). Replay requests without any reconstructed observation before the cutoff return `replay_data_unavailable`. Poller state, logs, and `/live/status` expose only fixed error categories, never raw transport exception text.
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
  - `DOWN` (`2170`, 46 stops): `1,282` trips, `58,972` stops (`29,931` observed / `29,041` interpolated = **50.75%** observed coverage).
  - `UP` (`2171`, 52 stops): `1,264` trips, `65,728` stops (`31,909` observed / `33,819` interpolated = **48.55%** observed coverage).

### Held-out model ladder & selection (`reports/eval-full.md`, `reports/selection.json`)

- **Whole-day splits:** Training `2026-10-05, 06, 07, 08, 13, 14, 15` (Rung 4 fit: `05–08, 13–14` [32,418 observed queries]; split-conformal CQR calibration: `2026-10-15` [4,693 observed queries, `q_conf = 2.359 s`, calibration coverage `76.092% raw -> 80.055% calibrated`]). Held out: `2026-10-22, 27, 28` (`13,299` observed-only queries). Excluded holidays: `2026-10-02, 20, 26`.
- **October 22 strict persistence gate (`observed-only`):** `UP` PASS (`79.936 s` vs `95.608 s`, 2,397 queries); `DOWN` PASS (`87.112 s` vs `101.801 s`, 2,193 queries).
- **Selected rung:** **`4`** (`60.178 s` MAE vs Rung 1 persistence `72.766 s`; delta **`-12.588 s`**, beating Rung 1 on `6/6` groups at identical coverage).
- **Ground-truth caveat:** `all-stop` rows (reported in full in `reports/eval-full.md` and `reports/eval-baselines.md`) include timetable-interpolated pseudo-ground-truth which biases toward Rung 0 (median AE `0.000 s` on 4/6 groups). The persistence gate and model-selection rule use `observed-only` exclusively.

#### Model selection summary (`observed-only`, 13,299 paired queries)

| Rung | Observed queries | Overall MAE s | Delta vs rung 1 s | Coverage matches rung 1 | Groups beating rung 1 | Interval cov % | Mean width s | Pooled median width s |
|---:|---:|---:|---:|---|---:|---:|---:|---:|
| 0 | 13299 | 87.273 | +14.506 | yes | 0/6 | - | - | - |
| 1 | 13299 | 72.766 | +0.000 | yes | 0/6 | - | - | - |
| 1h | 13299 | 74.525 | +1.759 | yes | 2/6 | - | - | - |
| 2 | 13299 | 69.498 | -3.269 | yes | 6/6 | - | - | - |
| 3 | 13299 | 69.558 | -3.209 | yes | 6/6 | - | - | - |
| **4** | **13299** | **60.178** | **-12.588** | **yes** | **6/6** | **79.156** | **144.496** | **81.518** |

#### Per-day, per-direction `observed-only` metrics (`reports/eval-full.md`)

| Day | Direction | Rung | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interp % | Interval cov % | Mean width s | Median width s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-22 | DOWN | 0 | 2193 | 101.801 | 34.000 | 187.600 | 29.275 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 1 | 2193 | 87.112 | 27.000 | 158.600 | 26.539 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 1h | 2193 | 92.108 | 24.000 | 167.800 | 24.350 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 2 | 2193 | 83.199 | 22.000 | 160.400 | 23.210 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 3 | 2193 | 83.262 | 22.191 | 160.663 | 23.028 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | DOWN | 4 | 2193 | 75.881 | 19.807 | 140.240 | 20.520 | 100.000 | 0.000 | 76.197 | 144.360 | 86.570 |
| 2026-10-22 | UP | 0 | 2397 | 95.608 | 32.000 | 190.000 | 26.950 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 1 | 2397 | 79.936 | 23.000 | 155.000 | 23.821 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 1h | 2397 | 80.703 | 22.000 | 154.700 | 21.610 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 2 | 2397 | 79.477 | 22.000 | 156.200 | 23.029 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 3 | 2397 | 79.557 | 21.744 | 158.436 | 23.071 | 100.000 | 0.000 | - | - | - |
| 2026-10-22 | UP | 4 | 2397 | 65.930 | 18.144 | 112.947 | 19.483 | 100.000 | 0.000 | 80.434 | 156.702 | 79.057 |
| 2026-10-27 | DOWN | 0 | 2372 | 75.619 | 34.000 | 156.900 | 29.384 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 1 | 2372 | 60.186 | 27.000 | 121.000 | 23.693 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 1h | 2372 | 63.577 | 24.000 | 134.000 | 21.838 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 2 | 2372 | 57.603 | 22.000 | 115.500 | 19.266 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 3 | 2372 | 57.616 | 21.976 | 114.870 | 19.013 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | DOWN | 4 | 2372 | 49.172 | 19.394 | 94.255 | 16.737 | 100.000 | 0.000 | 79.764 | 131.762 | 81.423 |
| 2026-10-27 | UP | 0 | 2368 | 73.865 | 30.000 | 150.000 | 25.084 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 1 | 2368 | 60.938 | 22.000 | 121.000 | 21.748 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 1h | 2368 | 59.691 | 22.000 | 120.000 | 19.679 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 2 | 2368 | 55.859 | 21.500 | 122.000 | 19.848 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 3 | 2368 | 55.912 | 21.126 | 122.073 | 19.764 | 100.000 | 0.000 | - | - | - |
| 2026-10-27 | UP | 4 | 2368 | 47.914 | 17.537 | 89.572 | 16.385 | 100.000 | 0.000 | 81.208 | 136.326 | 73.107 |
| 2026-10-28 | DOWN | 0 | 2047 | 70.603 | 34.000 | 159.800 | 29.507 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 1 | 2047 | 59.895 | 30.000 | 126.400 | 25.696 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 1h | 2047 | 59.278 | 22.000 | 132.000 | 22.423 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 2 | 2047 | 53.989 | 22.000 | 121.000 | 22.032 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 3 | 2047 | 54.036 | 22.087 | 120.523 | 21.935 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | DOWN | 4 | 2047 | 46.648 | 19.934 | 95.384 | 18.124 | 100.000 | 0.000 | 79.238 | 135.248 | 82.424 |
| 2026-10-28 | UP | 0 | 1922 | 108.954 | 33.000 | 224.000 | 31.061 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 1 | 1922 | 91.263 | 25.000 | 179.000 | 26.951 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 1h | 1922 | 94.787 | 22.000 | 189.700 | 26.223 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 2 | 1922 | 89.418 | 22.000 | 169.100 | 26.015 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 3 | 1922 | 89.531 | 22.707 | 165.166 | 25.963 | 100.000 | 0.000 | - | - | - |
| 2026-10-28 | UP | 4 | 1922 | 78.193 | 19.761 | 139.511 | 22.841 | 100.000 | 0.000 | 77.575 | 165.060 | 87.092 |

The complete 72-row table (including all-stop rows and history fallback counts) is in [`reports/eval-full.md`](reports/eval-full.md) and [`reports/eval-full.csv`](reports/eval-full.csv).

---

## Google Routes API Comparator Summary

**Comparator only, not ground truth:** Google Routes API reflects Google's schedule/traffic model on upcoming departures (past October dates return `routes: []`), not observed Delhi GPS arrivals. Ground-truth accuracy is measured strictly against observed October 2026 stop passages above.

- **Corpus:** 30 Delhi OD pairs × 3 slots (`08:30`, `14:00`, `18:30` IST on upcoming Tuesday `2026-11-24`) = **90 planned slots, 41 compared, 49 abstentions retained**. The fixed planner reported `insufficient_data` where no candidate had supported route-level observations; those rows are not omitted. Three non-bus `SUBWAY` alternatives were filtered out, every numeric comparison is strictly bus-only, and `languageCode="en"`.
- **Mission-wide call budget:** **`92 / 300`** (`2` probe calls + `90` cached comparator calls; `208` remaining). Regeneration used 90/90 cache hits and zero Google calls.

| Slice | Planned | Compared | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **All OD × Time Slots** | 90 | 41 | 63.1 min | 56.0 min | +428 s (+7.1 m) | +484 s | 624 s (10.4 m) | 97.6% |
| 08:30 IST (Morning Peak) | 30 | 14 | 56.1 min | 54.8 min | +78 s (+1.3 m) | +134 s | 397 s (6.6 m) | 92.9% |
| 14:00 IST (Midday) | 30 | 13 | 61.9 min | 56.0 min | +354 s (+5.9 m) | +459 s | 492 s (8.2 m) | 100.0% |
| 18:30 IST (Evening Peak) | 30 | 14 | 71.1 min | 57.0 min | +846 s (+14.1 m) | +748 s | 972 s (16.2 m) | 100.0% |

Regenerate `reports/google-comparator.{md,csv}` and `reports/google-manifest.json` exclusively from committed `reports/google-comparator-inputs.json`. This makes **0 planner calls and 0 Google API calls**, and repeated renders are byte-identical:

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py --render-only
```

Fresh collection is a separate operation that replaces the frozen inputs. With OTP on 3102 and FastAPI on 3100, use `--date 2026-11-24 --cache-only` to query the planner while prohibiting Google network calls.

---

## Recorded Demo Walkthrough (`demo/demo.webm`)

`demo/demo.webm` was recorded with `agent-browser` via [`demo/record_demo.sh`](demo/record_demo.sh) and captures:

1. **Live journey planning:** Kashmere Gate ISBT → Nehru Place (Tue 08:30 IST) with the green `live` badge, arrival windows, per-leg provenance, and Leaflet route rendering.
2. **Complete 4-stage marginal-transfer replay journey:** real October 22, 2026 run `2171_17_09_490` (bus `DL1PD7867`, route `717AUP`, Badarpur Border (T) → Hauz Khas): initial timetable transfer recommendation (`717AUP -> OMS(+)BURARI-DHAULAKUAN`) → growing delay at Badarpur M B Road / Rajiv Gandhi Stadium (`+1,768 s`) → advice flip at Tuglaqabad MB Road / Okhla more (`+2,133 s`, recommends direct `774UP`) → observed Oct 22 outcome at Hamdard Nagar / Sangam Vihar. The first bus had an observed arrival at **18:03:17 IST**; connecting bus `DL51EV4293` (run `1416_15_08`) had an observed departure at **17:35:34 IST**, so the connection was **MISSED by 27m 43s**. Scheduled times remain separate: scheduled first-bus arrival 17:28:47 and scheduled connecting departure 17:32:42.
3. **Explicit stale-feed abstention and recovery:** API started with `PS1_LIVE_STALE_THRESHOLD_S=45` (overriding the 600 s default), `POST /live/pause` invoked, waited until live feed age exceeded `45 s`, UI submitted live query and displayed **`Not enough data`** with stale-feed age/threshold and suppressed options, then `POST /live/resume` restored fresh `VehiclePositions` and clicking **Retry live query** recovered the live recommendations.

Re-record the demo (requires OTP running on 3102 and API running on 3100 with `PS1_LIVE_STALE_THRESHOLD_S=45`):

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  demo/record_demo.sh --session ps1-demo
```
