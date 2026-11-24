# AGENTS.md

Guide for anyone (human or agent) working in this repository. Keep it short and accurate. Update it in the same commit as any change to commands, module responsibilities, rules or headline numbers. Never write secret values here.

## Purpose

Bus travel-time modelling and journey planning for Delhi DTC buses. Inputs: the October 2026 GTFS-RT GPS archive (31 days), static GTFS feed v32, the live GTFS-RT VehiclePositions feed, and OpenTripPlanner 2.8.1. OTP only proposes candidate itineraries; this code owns transit leg timing, transfer risk, `[p10, p90]` windows and abstention.

## Layout

- `src/ps1/`
  - `config.py`: env-driven paths (`PS1_DATA_DIR`, `PS1_WORK_DIR`), `PS1_OTP_URL`, the shared `IST` timezone, Delhi bbox, archive URL and user agent, live feed settings, Google 300-call cap and ledger path.
  - `range_reader.py`: bounded HTTP range reader for the remote 7z archive.
  - `extract_day.py`: extracts one archive day, dedupes on `(vehicle_id, ts, lat, lng)` keeping the first SQLite `rowid`, filters bbox/timestamps, casts `route_id` to integer, verifies (`verify_day`) and publishes Parquet atomically, deletes the SQLite file.
  - `acquire.py`: resumable 31-day orchestrator (2 workers); writes `download.log` and `STATUS.md`, never deletes a canonical Parquet on failure.
  - `stop_passages.py`: geometric GPS-to-stop matching for 717A (60 m, max-cardinality time-ordered anchor chain, schedule-proportional interpolation, adjacent-service-date resolution, per-passage `available_at`) and `reports/coverage-717a.md` (written only by full canonical runs).
  - `features.py`: stretch error tables and strictly causal replay features.
  - `model.py`: rungs `0`, `1`, `1h`, `2`, `3`, `4` (LightGBM quantile + split-conformal), artifact save/load and as-of eligibility.
  - `evaluate.py`: whole-day evaluation; writes `reports/eval-baselines.*`, `reports/eval-full.*`, `reports/selection.json` and the model artifact.
  - `selection.py`: ship rule (highest of rungs `4`/`3`/`2` beating rung 1 on held-out observed-only MAE at matching coverage, else rung 1; `1h` is report-only). Only observed-only decisions can be written.
  - `live_poller.py`: polls VehiclePositions into `data/live/`, gates entities by their own timestamps, stores only fixed error categories; pause/resume/status controls.
  - `otp_client.py`: GraphQL `planConnection` client for `/otp/gtfs/v1`.
  - `retime.py`: re-times OTP transit legs with the selected rung and fallback chain, waits, joint-scenario transfer risk and windows. Reads `reports/selection.json` relative to the package and refuses a non-observed-only selection.
  - `replay_demo.py`: scripted Oct 22 missed-transfer replay for OTP trips `DTC:2171_FULL_17_09` -> `DTC:1416_FULL_15_08` at stop 2300; returns `status="unavailable"` if OTP does not return that pair.
  - `api.py`: FastAPI app (`GET /journeys`, `/health`, `/live/status`, `POST /live/pause`, `POST /live/resume`, `GET /replay/demo`, `/docs`, UI at `/`). `depart` is optional: it defaults to now in Asia/Kolkata in live mode, or to 2026-10-22T08:30+05:30 when replay is explicit (`mode=replay`, or `mode=auto` with `PS1_MODE=replay`).
  - `google_client.py`: cached, field-masked Google Routes client with atomic reservations in the mission-wide ledger. A missing fixed ledger is seeded from the canonical `data/google/call_counter.json`, never restarted from zero.
  - `static/index.html`: single-file Leaflet UI (badge, feed age, abstention panel, presets, replay demo).
- `scripts/`: `otp_build.sh`, `otp_serve.sh`, `live_poller.sh`, `compare_google.py`.
- `tests/`: pytest suites in `tests/data`, `tests/model`, `tests/api`, `tests/comparator`.
- `reports/`: committed results (`data-quality.md`, `coverage-717a.md`, `eval-baselines.*`, `eval-full.*`, `selection.json`, `google-comparator.*`, `google-comparator-inputs.json`, `google-manifest.json`, `otp-smoke.md`, `otp-plan-*.json`).
- `docs/`: `technical-note.md`, `eval-protocol.md`, `otp.md`.
- `demo/`: `record_demo.sh` and `demo.webm`.
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`: container packaging.

## Setup

- Python 3.12 venv: `/home/factory-user/ps1-scratch/venv` (`pip install -r requirements.txt`).
- Java: Temurin 21 at `/home/factory-user/ps1-scratch/jdk`. OTP jar, OSM extract and graph: `/home/factory-user/ps1-scratch/otp/` (`PS1_OTP_DIR`). OTP heap is capped at `-Xmx2500m`.
- `.env` (gitignored, see `.env.example`): `DELHI_TS_API_KEY` (live feed and static GTFS), `GOOGLE_MAPS_API_KEY` (comparator). Read in-process only; never print, log or commit values.
- Docker and Podman are not installed on the development machine. Validate `docker-compose.yml` against the compose-spec schema and by running the same commands natively.

## Ports and data

- Ports: `3100` (API and UI), `3102` (OTP). Stay within `3100–3199`.
- `PS1_DATA_DIR` (default `/home/factory-user/ps1-scratch/data`, outside the repo):
  - `gps/day=YYYY-MM-DD.parquet`: 31 deduplicated days.
  - `gtfs/`: static feed v32 (unzipped files plus `delhi_buses_static_gtfs_v1.zip`).
  - `passages/day=YYYY-MM-DD.parquet`: 717A passages for the 10 train and held-out days.
  - `model/`: selected model artifact. `live/`: poller snapshots and state. `google/`: cached Google responses.
  - `download.log`, `STATUS.md`: acquisition ledger.
- `PS1_WORK_DIR` (default `/home/factory-user/ps1-scratch/work`): temporary SQLite files (at most 2 at once) and DuckDB temp space.

## Commands

Run from `/home/factory-user/repos/Transport-Stack-Hackathon`. These match the mission `services.yaml`.

- Tests: `/home/factory-user/ps1-scratch/venv/bin/python -m pytest tests -q`
- Bytecode check: `/home/factory-user/ps1-scratch/venv/bin/python -m compileall -q src`
- Lint: `/home/factory-user/ps1-scratch/venv/bin/ruff check src tests`
- Acquire all GPS days: `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.acquire`
- One GPS day: `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.extract_day --day 2026-10-15 --output /tmp/repro15.parquet`
- 717A passages (canonical 10 days and coverage report): `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages --overwrite`
- 717A passages for one day (never touches the canonical report): `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages --day 2026-10-22 --output-dir /tmp/passages-check --overwrite`
- Evaluation and selection: `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate`
- OTP graph build: `scripts/otp_build.sh`. OTP serve (3102): `scripts/otp_serve.sh`
- API serve (3100): `PS1_DATA_DIR=/home/factory-user/ps1-scratch/data PS1_OTP_URL=http://localhost:3102/otp/gtfs/v1 /home/factory-user/ps1-scratch/venv/bin/python -m uvicorn ps1.api:app --app-dir src --host 127.0.0.1 --port 3100`
- Live poller, single poll: `scripts/live_poller.sh --once`
- Google comparator render (no network): `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py --render-only`
- Google comparator collection (planner calls, cached Google only): `PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py --date 2026-11-24 --cache-only`
- Demo recording (API started with `PS1_LIVE_STALE_THRESHOLD_S=45`; the normal default is 600 s): `demo/record_demo.sh`
- Docker Compose: `docker compose --profile build run --rm otp-build` (graph build), `docker compose up -d --build`

## Rules

- **Causality.** Every passage carries `available_at`, the latest of its own rule (observed = own ping; interior interpolated = later anchor; extrapolated = anchor used), the run's second surviving anchor, and the ping after which every raw-GPS prefix reconstruction reproduces it. Features, replay and evaluation use only passages with `available_at` strictly before the query time.
- **Replay cutoff.** A replay request uses one observation cutoff, the requested departure, for every leg and the quiet-hour comparison. A model artifact is used only when eligible as of that cutoff.
- **Clock.** Normalise request times to Asia/Kolkata (`+05:30`) before service-date, hour and quiet-hour logic. Resolve GTFS service dates for midnight-crossing runs across `day-1`, `day`, `day+1`.
- **Evaluation.** Whole-day splits only: train Oct 5–8 and 13–15, held out Oct 22, 27, 28. Holidays Oct 2, 20, 26 are excluded from normal-day training; Oct 7 is never an evaluation day. The gate and selection use the observed-only stratum. Never sum per-leg p90s into an end-to-end p90.
- **Matching.** Stop matching is geometric. Never join GPS `trip_id` to GTFS `trip_id`, and never fuzzy-match stop names city-wide.
- **Google.** At most 300 calls mission-wide, reserved atomically in the fixed ledger before each request. Cache every response under `data/google/`. Never query past or October dates. Always send `languageCode="en"` and keep only bus-only itineraries. Google is a comparator, not ground truth.
- **Provenance and abstention.** Label `mode` (`live`/`replay`) separately from per-leg provenance (`model_live`, `model_replay`, `historical_fallback`, `timetable_fallback`) and observed coverage. Live entities older than the freshness threshold cannot drive timing. Abstain (`status="insufficient_data"`) for a stale or missing feed, too few fresh vehicles, no supported candidate route, or no replay observations before the cutoff. Never synthesise a fresh replay timestamp. Never log API keys.
- **UI.** The latest user action wins: superseded responses must not change newer UI state. The abstention badge follows the API reason code.
- **Commits.** Commit and push to `origin/main` after each passing coherent step. Plain commit messages, no co-author or tool trailers.

## Known data gaps

- Archive day `d` runs from about 23:30 IST on `d-1` to 23:59 IST on `d`.
- 2026-10-07 ends at 19:53:23 IST (training only). 2026-10-08 starts at 02:02:10 IST.
- Static GTFS v32 has 6,862 stops and is newer than the snapshot behind the older reference numbers.

## Headline results

- GPS ([`reports/data-quality.md`](reports/data-quality.md)): 31/31 days, 912,877,061 raw rows to 560,357,771 clean (38.62% removed), 9.955 GB Parquet, 0 duplicate groups, 0 bbox violations, all files deterministic (`rowid` first-row dedupe) and verified.
- 717A passages ([`reports/coverage-717a.md`](reports/coverage-717a.md)): 2,546 runs over 10 days; observed coverage 48.55% UP (2171), 50.75% DOWN (2170).
- Gate ([`reports/eval-baselines.md`](reports/eval-baselines.md)): Oct 22 observed-only persistence beats timetable, UP 79.936 s vs 95.608 s (2,397 queries), DOWN 87.112 s vs 101.801 s (2,193).
- Selection ([`reports/eval-full.md`](reports/eval-full.md), [`reports/selection.json`](reports/selection.json)): 13,299 held-out observed-only queries; MAE rung 0 87.273 s, rung 1 72.766 s, rung 4 60.178 s. Rung 4 ships with 79.156% `[p10, p90]` coverage and 81.518 s pooled median width.
- Google ([`reports/google-comparator.md`](reports/google-comparator.md)): 90 slots on 2026-11-24, 41 compared, 49 abstentions; mean difference (ours minus Google) +428 s; 97.6% of Google durations inside our window; 92/300 calls used.
