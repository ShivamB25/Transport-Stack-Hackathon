# AGENTS.md — Delhi PS1 Bus Travel-Time Model & Journey Planner

Living guide for anyone (human or agent) working in this repository. Keep it concise and accurate: update this file in the same commit whenever you change commands, module responsibilities, rules, or published headline numbers. Never put secret values here.

## Project Purpose

Real-time bus travel-time modeling and journey planning for Delhi DTC buses using October 2026 historical GTFS-RT GPS archives (31 days), static GTFS feed v32, live GTFS-RT VehiclePositions, and OpenTripPlanner 2.8.1 for candidate itinerary generation. OTP generates candidate routes only; this service owns all transit leg timing, transfer connection risk, uncertainty windows, and abstention.

## Repository Layout

- `src/ps1/`
  - `config.py`: Env-driven paths, ports, Delhi bbox (`27.5–29.0 N`, `76.7–77.6 E`), archive URL/UA, live feed settings, Google 300-call cap.
  - `range_reader.py`: Bounded seekable HTTP range reader (`HTTPRangeFile`) for remote 7z archives; rejects non-206 and oversized responses before buffering the body.
  - `extract_day.py`: Range-extracts one archive day's SQLite member, deterministically deduplicates on `(vehicle_id, ts, lat, lng)` by SQLite `rowid` (keep first), filters bbox/ts, casts `route_id` to `INTEGER`, stages and verifies (`verify_day`) before atomic Parquet publication, and removes temporary SQLite files.
  - `acquire.py`: Resumable 31-day orchestrator (priority days first, 2 workers); verifies existing Parquets in place via `verify_day`, never deletes canonical Parquets on refresh failure, and writes `download.log` + `STATUS.md`.
  - `stop_passages.py`: Geometric GPS-to-stop-pattern matching (60 m radius, min-distance per stop, max-cardinality nondecreasing anchor chain, schedule-proportional interpolation, adjacent IST service-date resolution for midnight-crossing runs, per-passage `available_at` GPS availability epoch) and coverage reporting (`reports/coverage-717a.md`, protected from scoped runs).
  - `features.py`: Stretch error tables and strictly causal replay features.
  - `model.py`: Model ladder rungs `0` (timetable), `1` (persistence), `1h` (historical median), `2` (+ day-type), `3` (+ vehicle last observed delay), `4` (LightGBM quantile p10/p50/p90 + split-conformal calibration), and artifact persistence.
  - `evaluate.py`: Frozen whole-day evaluation runner emitting `reports/eval-baselines.{csv,md}` and `reports/eval-full.{csv,md}`.
  - `selection.py`: Ship-decision rule (`reports/selection.json`) picking the highest rung beating rung 1 on held-out observed-only MAE at matching coverage.
  - `live_poller.py`: Polls live Delhi GTFS-RT `VehiclePositions.pb`, writes snapshots/route stats under `data/live/`, tracks feed age.
  - `otp_client.py`: GraphQL `planConnection` client for OTP 2.8.1 (`/otp/gtfs/v1`).
  - `retime.py`: Re-times OTP candidate transit legs using the selected model artifact and fallback chain, computes waits, joint-scenario transfer risk, and door-to-door `[p10, p90]` windows.
  - `replay_demo.py`: Scripted October 22 marginal-transfer replay scenario for the UI demo.
  - `api.py`: FastAPI service (`GET /journeys`, `GET /health`, `GET /live/status`, `POST /live/pause`, `POST /live/resume`, `/docs`, static UI).
  - `google_client.py`: Cached, field-masked Google Routes API (`TRANSIT` / `BUS`) client with call-budget accounting.
  - `static/index.html`: Single-file Leaflet (CDN) UI with live/replay badge, feed age, abstention state, and replay demo controls.
- `scripts/`: `otp_build.sh`, `otp_serve.sh`, `live_poller.sh`, `compare_google.py`.
- `tests/`: Pytest suite split across `tests/data/`, `tests/model/`, `tests/api/`, `tests/comparator/`.
- `reports/`: Committed CSV/Markdown/JSON outputs (`data-quality.md`, `coverage-717a.md`, `eval-baselines.*`, `eval-full.*`, `selection.json`, `google-comparator.*`, `google-manifest.json`, `otp-smoke.md`).
- `docs/`: `eval-protocol.md`, `otp.md`, `technical-note.md`.
- `demo/`: `record_demo.sh` and recorded walkthrough `demo.webm`.

## Setup

- **Python venv:** `/home/factory-user/ps1-scratch/venv` (Python 3.12; install/verify via `pip install -r requirements.txt`).
- **Java & OTP:** Temurin 21 at `/home/factory-user/ps1-scratch/jdk`; OTP 2.8.1 shaded jar and graph at `/home/factory-user/ps1-scratch/otp/` (`-Xmx2500m` hard cap).
- **Secrets (`.env`, gitignored):** Required variable names are `DELHI_TS_API_KEY` (Delhi live VehiclePositions feed) and `GOOGLE_MAPS_API_KEY` (Google Routes comparator). Read in-process only; never print, log, or commit values.

## Ports & Data Locations

- **Ports:** `3100` (FastAPI + Leaflet UI), `3102` (OTP 2.8.1 GraphQL). Do not bind outside `3100–3199`.
- **Data directory (`PS1_DATA_DIR`, default `/home/factory-user/ps1-scratch/data`, outside repo):**
  - `gps/day=YYYY-MM-DD.parquet`: 31 October 2026 deduplicated GPS Parquet files (8.87 GB total; the 10 passage days use deterministic `rowid`-ordered first-row dedupe, and the remaining 21 days await `reacquire-remaining-days`).
  - `gtfs/`: Static GTFS feed v32 (`routes.txt`, `trips.txt`, `stops.txt`, `stop_times.txt`, etc.).
  - `passages/day=YYYY-MM-DD.parquet`: Reconstructed 717A stop passages for train/held-out days (`available_at` included).
  - `model/`: Persisted selected model artifact(s).
  - `live/`: Live VehiclePositions snapshots, route stats, and poller state.
  - `google/`: Cached field-masked Google Routes API responses and call manifest.
  - `download.log`, `STATUS.md`: Acquisition ledger and status.
- **Scratch work dir (`PS1_WORK_DIR`, default `/home/factory-user/ps1-scratch/work`):** Temporary unpacked SQLite files (max 2 concurrent; always deleted after Parquet conversion) and `duckdb-tmp/`.

## Commands

Run from repo root `/home/factory-user/repos/Transport-Stack-Hackathon`:

- **Tests:** `/home/factory-user/ps1-scratch/venv/bin/python -m pytest tests -q` (scoped: `tests/data`, `tests/model`, `tests/api`, `tests/comparator`)
- **Type/bytecode check:** `/home/factory-user/ps1-scratch/venv/bin/python -m compileall -q src`
- **Lint:** `/home/factory-user/ps1-scratch/venv/bin/ruff check src tests`
- **One-day GPS extraction (end-to-end):** `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.extract_day --day 2026-10-15 --output /tmp/repro15.parquet`
- **717A stop passages (canonical 10 days + `reports/coverage-717a.md`):** `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages --overwrite`
- **717A stop passages (scoped day; never overwrites canonical report):** `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages --day 2026-10-22 --output-dir /tmp/passages-check --overwrite`
- **Model evaluation & selection:** `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate`
- **OTP graph build (one-time):** `scripts/otp_build.sh`
- **OTP serve (port 3102):** `scripts/otp_serve.sh`
- **API serve (port 3100):** `PS1_DATA_DIR=/home/factory-user/ps1-scratch/data PS1_OTP_URL=http://localhost:3102/otp/gtfs/v1 /home/factory-user/ps1-scratch/venv/bin/python -m uvicorn ps1.api:app --app-dir src --host 127.0.0.1 --port 3100`
- **Live poller (single poll):** `scripts/live_poller.sh --once`
- **Google comparator (cache-only):** `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py --cache-only`
- **Demo recording:** `demo/record_demo.sh`

## Key Rules

- **Causality & `available_at`:** Every stop passage carries `available_at` (observed = own ping `ts`; interior interpolated = later anchor `ts`; extrapolated endpoints = anchor used). Features, replay, and evaluation must only consume passages with `available_at` strictly before the query/request time.
- **Request-wide replay cutoff:** Replay uses one request-wide observation cutoff equal to the requested departure (including quiet-hour comparisons). Model artifacts must only be used when eligible as of that cutoff.
- **Clock & service dates:** Normalize request times to `Asia/Kolkata` (`+05:30`) before service-date, profile-hour, and quiet-hour logic. GTFS service dates for midnight-crossing runs are resolved across adjacent service dates (`day-1`, `day`, `day+1`), never assumed equal to the archive partition date.
- **Evaluation & splits:** Whole-day splits only (train: Oct 5–8, 13–15; held-out: Oct 22, 27, 28; holidays Oct 2/20/26 excluded from normal-day training; Oct 7 never an eval day). Gate and model selection run on the `observed-only` stratum; never sum per-leg p90s into end-to-end p90s.
- **No GPS↔GTFS trip_id join:** Stop matching is strictly geometric; never join GPS `trip_id` to GTFS `trip_id`, and never fuzzy-match stop names city-wide.
- **Google comparator:** Hard cap of 300 calls mission-wide; cache every response under `data/google/`; never query past/October departure times (Google returns zero transit routes); always `languageCode="en"` and post-filter strictly bus-only itineraries. Google is a comparator, not ground truth.
- **Honesty, provenance & abstention:** Always label `mode` (`live` vs `replay`) separately from per-leg provenance (`model_live`, `model_replay`, `historical_fallback`, `timetable_fallback`) and observed coverage. Abstain visibly (`status="insufficient_data"`) when GPS data is stale or coverage is insufficient. Never print or log API keys.
- **Commit cadence:** Commit and push to `origin/main` after each passing coherent sub-step; keep repo `AGENTS.md` updated in the same commit.

## Known Data Gaps & Archive Conventions

- **Archive-day windows:** Nominal day `d` spans roughly `23:30 IST` of `d-1` through `23:59:5x IST` of `d` (preserving ~30 min previous-evening overlap; `>=99.99%` of rows lie inside nominal IST day `d`).
- **2026-10-07:** Truncated by source at `19:53:23 IST` (15,770,083 clean rows); allowed for training, never an evaluation day.
- **2026-10-08:** Source gap from `00:00` to `02:02:10 IST`.
- **Static GTFS v32:** 6,862 stops (newer than the snapshot behind historical reference numbers).

## Current Headline Results

- **31-day GPS acquisition (`reports/data-quality.md`):** 912,877,061 raw rows -> 560,357,771 clean rows (38.62% removed, 8.87 GB Parquet, 0 duplicate groups, 0 bbox violations, 0 fractional `route_id`; all 31 files pass hardened `verify_day`). The 10 passage days (`2026-10-05, 06, 07, 08, 13, 14, 15, 22, 27, 28`) were re-acquired on 2026-11-23 with deterministic `rowid` first-row dedupe; the remaining 21 days still use the legacy arbitrary tie-break until `reacquire-remaining-days` runs (Oct 22 audit measured 13,942 `route_id` [0.258%] and 14,247 `trip_id` [0.264%] disagreements across 5,402,665 duplicate-key groups).
- **717A stop passages (`reports/coverage-717a.md`, rebuilt with adjacent service-date fix & `available_at`):** 2,548 runs, 124,810 passages across 10 days; observed coverage 48.47% UP (2171) / 50.81% DOWN (2170); max `|schedule_error_s|` = 14,065 s (Oct 22 `2171_23_57_4191` stop 2 fixed from `-80,064 s` to `+6,336 s`; Oct 28 `2171_22_16_8617` stop 50 fixed from `-85,253 s` to `+1,155 s`); 0 monotonicity or availability violations.
- **Model evaluation & selection (`reports/eval-baselines.md`, `reports/eval-full.md`, `reports/selection.json`):** Pending causal re-evaluation using the newly added `available_at` column (current committed reports predate `available_at` filtering: recorded observed-only overall MAE rung 0 = 87.25 s, rung 1 = 77.33 s, rung 4 = 61.74 s, selected rung `"4"`).
- **Google comparator (`reports/google-comparator.md`, `reports/google-manifest.json`):** 90/90 planned slots compared on `2026-11-24`, 92/300 mission-wide Google calls used (cached), mean diff (ours - Google) `+123 s` (+2.0 min), 96.7% of Google durations inside our `[p10, p90]`.
