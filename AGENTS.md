# AGENTS.md

Guide for anyone (human or agent) working in this repository. Keep it short and accurate. Update it in the same commit as any change to commands, module responsibilities, rules or headline numbers. Never write secret values here.

## Purpose

Bus travel-time modelling and journey planning for Delhi DTC buses. Inputs: the October 2026 GTFS-RT GPS archive (31 days), static GTFS feed v32, the live GTFS-RT VehiclePositions feed, and OpenTripPlanner 2.8.1. OTP only proposes candidate itineraries; this code owns transit leg timing, transfer risk, `[p10, p90]` windows and abstention.

## Layout

- `backend/`: uv project (`pyproject.toml`, `uv.lock`, `.python-version` 3.12). Package `src/delhi_transit/`:
  - `core/config.py`: `REPO_ROOT` (every repo-relative path such as `reports/` and `.env` resolves through it), env-driven paths (`DELHI_TRANSIT_DATA_DIR`, `DELHI_TRANSIT_WORK_DIR`, `DELHI_TRANSIT_FRONTEND_DIR`), `DELHI_TRANSIT_OTP_URL`, the shared `IST` timezone, Delhi bbox, archive URL and user agent, live feed settings, request-time getters for the API settings (`live_stale_threshold_s`, `live_min_vehicles`, `min_route_coverage`, `runtime_mode`, `live_poller_enabled`), Google 300-call cap and ledger path, and `load_env_key` (the one reader of `.env` secrets; never logs values).
  - `core/time.py` (IST/UTC formatting), `core/geo.py` (`haversine_m`), `core/io.py` (atomic text/JSON/Parquet writers: sibling temp file, fsync for text, `os.replace`).
  - `data/range_reader.py`: bounded HTTP range reader for the remote 7z archive.
  - `data/extract_day.py`: extracts one archive day, dedupes on `(vehicle_id, ts, lat, lng)` keeping the first SQLite `rowid`, filters bbox/timestamps, casts `route_id` to integer, verifies (`verify_day`) and publishes Parquet atomically, deletes the SQLite file.
  - `data/acquire.py`: resumable 31-day orchestrator (2 workers); writes `download.log` and `STATUS.md`, never deletes a canonical Parquet on failure.
  - `data/stop_passages.py`: geometric GPS-to-stop matching for 717A (60 m, max-cardinality time-ordered anchor chain, schedule-proportional interpolation, adjacent-service-date resolution, per-passage `available_at`). `data/gtfs_patterns.py` loads the dominant FULL GTFS pattern and timetable starts; `data/coverage_report.py` writes `reports/coverage-717a.md` (only from full canonical runs; scoped runs write next to their output).
  - `model/causal_core.py`: the pure causal feature arithmetic shared by evaluation and the planner (`CausalFeatures`, route geometry, calendar fields, vehicle progress, pace/speed medians, headway and delay limits). `model/features.py`: passages, stretch error tables, the causality validator and strictly causal replay queries.
  - `model/rungs.py`: rungs `0`, `1`, `1h`, `2`, `3` and the historical median tables (one shared `predict_batch`). `model/lgbm.py`: rung `4` (LightGBM quantile + split-conformal). `model/artifact.py`: artifact save/load (table-driven by rung) and as-of eligibility.
  - `model/evaluate.py`: whole-day evaluation; writes the model artifact and, through `model/reports.py` (CSV and Markdown writers), `reports/eval-baselines.*`, `reports/eval-full.*` and `reports/selection.json`.
  - `model/selection.py`: ship rule (highest of rungs `4`/`3`/`2` beating rung 1 on held-out observed-only MAE at matching coverage, else rung 1; `1h` is report-only). Only observed-only decisions can be written, and `load_selected_rung` refuses to read any other.
  - `live/poller.py`: polls VehiclePositions into `data/live/` (one poll core behind `poll_once`/`apoll_once`), stores only fixed error categories; pause/resume/status controls. `live/freshness.py`: request-time freshness, each entity gated by its own timestamp.
  - `planner/otp_client.py`: GraphQL `planConnection` client for `/otp/gtfs/v1`.
  - `planner/rides.py`: `RetimeModel`, ride timing with the selected-rung artifact on covered routes (only when eligible as of the replay cutoff), then the causal rung-1 fallback chain (replay persistence, live speed, historical, timetable). Reads `REPO_ROOT/reports/selection.json` and refuses a non-observed-only selection. `planner/causal_context.py`: `TimingRecord`, the `visible` cutoff filter and the replay/live observations a query can see, assembled through `model/causal_core.py`. `planner/waits.py`: first-boarding and connection waits. `planner/risk.py`: ratio clamp, ride windows and the joint scenarios (leg quantiles are never added). `planner/provenance.py`: `TimingEstimate`/`WaitEstimate`, timing-source labels and observed coverage. `planner/itinerary.py`: `retime_itinerary` (one OTP candidate to timed legs, transfer risk and windows).
  - `comparator/client.py`: cached, field-masked Google Routes client with atomic reservations in the mission-wide ledger. A missing fixed ledger is seeded from the canonical `data/google/call_counter.json`, never restarted from zero; if neither exists, paid cache misses are refused while cache reads keep working. `unavailable_response` builds the one row input for a slot with no Google response.
  - `comparator/slots.py`: the 30 OD pairs (one compact table), three Tuesday slots and the schedule check run before any call. `comparator/compare.py`: planner queries (the compared option is the first one that passes the API support gate), slot comparison (a reported option without route-level observations becomes `our_unsupported_option`), frozen-input collection and rendering. `comparator/report.py`: CSV, manifest and Markdown writers. `comparator/__main__.py`: CLI (`python -m delhi_transit.comparator`).
  - `api/app.py`: `create_app` (runtime wiring, background live poller) and the app exported as `delhi_transit.api:app`. `api/routes.py`: the routes (`GET /journeys`, `/health`, `/live/status`, `POST /live/pause`, `POST /live/resume`, `GET /replay/demo`, `/docs`, UI at `/` from `api/static.py`), exact stop-name resolution, depart/mode parsing, live-feed checks and the one abstention builder; request-time settings come from the `core/config.py` getters. `api/schemas.py`: response models. `depart` is optional: it defaults to now in Asia/Kolkata in live mode, or to 2026-10-22T08:30+05:30 when replay is explicit (`mode=replay`, or `mode=auto` with `DELHI_TRANSIT_MODE=replay`).
  - `api/replay_demo.py`: scripted Oct 22 missed-transfer replay for OTP trips `DTC:2171_FULL_17_09` -> `DTC:1416_FULL_15_08` at stop 2300; returns `status="unavailable"` if OTP does not return that pair.
  - `livecheck/`: the one-day live check, `python -m delhi_transit.livecheck {run,actuals,analyze}`. `record.py` (`run`): polls the feed through `LivePoller` into its own directory and copies every snapshot to `OUT/gps/`, queries `GET /journeys?mode=live` for the 30 comparator OD pairs plus six 717A stop pairs every slot (`OUT/journeys/slot_NNN.jsonl`, each with request time and feed age), and at the Google slots calls the comparator client for the ten 717A corridor pairs, stopping at `--google-max` paid calls or the `--google-ledger-limit` ledger total (`OUT/google/calls.jsonl`); writes `OUT/polls.jsonl`, `OUT/run.log`, `OUT/run-summary.json`. `actuals.py`: dedupes the snapshots to the archive GPS schema and runs `data/stop_passages.reconstruct_day` into `OUT/passages/`. `analyze.py`: the leg-to-actual matching rule (module docstring), accuracy by provenance and observed-only, response, abstention and feed-health stats. `report.py`: `reports/live-check-<day>.{md,csv,json}`.
  - `api/static.py`: serves the built UI from `FRONTEND_DIR` (default `frontend/dist`) at `/` and `/assets/*` (paths cannot escape the directory); if the UI is not built, `/` returns a short plain-text message.
- `livecheck/launch.sh`: reuses a healthy OTP on 3102 (otherwise starts it and stops it afterwards), starts the API with the live poller on 3120, runs `livecheck run`, then stops what it started.
- `frontend/`: the UI. Bun + Vite + React 19 + TypeScript + Tailwind v4 + shadcn/ui (`src/components/ui/`) + TanStack Query, with plain Leaflet through `hooks/useLeafletMap.ts` (no react-leaflet). `src/api/` holds the response types matching the API JSON, the fetch client and the query options; `hooks/useLatestAction.ts` gives every user action a new id and the results pane reads only the newest action's query. Components: `JourneyForm` (presets), `ModeBadge` (live/replay and feed age), `OptionList`, `LegBreakdown` (windows, provenance, coverage), `RouteMap`, `AbstentionPanel` (reason code), `ReplayDemo` (stages, flip, outcome, quiet-hour comparison). `bun.lock` is committed; `dist/` and `node_modules/` are gitignored.
- `backend/tests/`: pytest suites in `data`, `model`, `planner`, `live`, `api`, `comparator` (`conftest.py` provides the VehiclePositions protobuf builder and, when the data directory has no `gtfs/stops.txt`, points `DELHI_TRANSIT_DATA_DIR` at a temporary directory with a header-only stops file so the suite runs without the real data).
- `.github/workflows/ci.yml`: on every push and pull request, runs `uv sync --frozen`, `uv run pytest -q`, `uv run ruff check src tests` and `uv run python -m compileall -q src` in `backend/`, and `bun install --frozen-lockfile`, `bun run typecheck` and `bun test` in `frontend/`.
- `otp/`: `build.sh`, `serve.sh`, `build-config.json` (copied into the graph directory by `build.sh`).
- `reports/`: committed results (`data-quality.md`, `coverage-717a.md`, `eval-baselines.*`, `eval-full.*`, `selection.json`, `google-comparator.*`, `google-comparator-inputs.json`, `google-manifest.json`, `otp-smoke.md`, `otp-plan-*.json`).
- `docs/`: `technical-note.md`, `eval-protocol.md`, `otp.md`.
- `demo/`: `record_demo.sh` and `demo.webm`.
- `Dockerfile` (an `oven/bun` stage builds `frontend/dist`; a `python:3.12-slim` stage runs `uv sync --frozen --no-dev` and copies the UI to `/app/frontend/dist`, the default `FRONTEND_DIR` there), `docker-compose.yml`, `.dockerignore`, `.env.example`: container packaging.

## Setup

- Python through uv only: `cd backend && uv sync --frozen` creates `backend/.venv` (gitignored) with the exact pinned versions (Python 3.12). Run Python with `uv run ...` from `backend/`.
- Frontend through Bun only (`/usr/bin/bun`, no npm/yarn/pnpm lockfiles): `cd frontend && bun install --frozen-lockfile && bun run build` writes `frontend/dist`, which the API serves.
- Paths: every machine path in docs and reports is written relative to `DELHI_TRANSIT_SCRATCH_DIR` and the variables below; the code defaults point at the development machine (`/home/factory-user/ps1-scratch`, see `core/config.py` and the README path table).
- Java: Temurin 21 at `$DELHI_TRANSIT_SCRATCH_DIR/jdk` (`DELHI_TRANSIT_JAVA`). OTP jar, OSM extract and graph: `$DELHI_TRANSIT_OTP_DIR/` (`DELHI_TRANSIT_OTP_DIR`, default `$DELHI_TRANSIT_SCRATCH_DIR/otp`). OTP heap is capped at `-Xmx2500m`.
- `.env` in the repository root (gitignored, see `.env.example`): `DELHI_TS_API_KEY` (live feed and static GTFS), `GOOGLE_MAPS_API_KEY` (comparator). Read in-process only; never print, log or commit values.
- Docker and Podman are not installed on the development machine. Validate `docker-compose.yml` against the compose-spec schema and by running the same commands natively. `.github/workflows/compose.yml` builds the images with `docker compose build` in CI and smoke-tests the API image with a stub data directory; `docker compose up` with the real data and graph mounts is not exercised there.

## Ports and data

- Ports: `3100` (API and UI), `3102` (OTP), `3110` (Vite dev server, proxies API paths to 3100), `3120` (API started by `livecheck/launch.sh` for the live check recording). Stay within `3100–3199`.
- `DELHI_TRANSIT_DATA_DIR` (default `$DELHI_TRANSIT_SCRATCH_DIR/data`, outside the repo):
  - `gps/day=YYYY-MM-DD.parquet`: 31 deduplicated days.
  - `gtfs/`: static feed v32 (unzipped files plus `delhi_buses_static_gtfs_v1.zip`).
  - `passages/day=YYYY-MM-DD.parquet`: 717A passages for the 10 train and held-out days.
  - `model/`: selected model artifact. `live/`: poller snapshots and state. `google/`: cached Google responses.
  - `download.log`, `STATUS.md`: acquisition ledger.
- `DELHI_TRANSIT_WORK_DIR` (default `$DELHI_TRANSIT_SCRATCH_DIR/work`): temporary SQLite files (at most 2 at once) and DuckDB temp space.
- Other settings: `DELHI_TRANSIT_MODE` (`auto`/`live`/`replay`), `DELHI_TRANSIT_ENABLE_LIVE_POLLER`, `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S` (600), `DELHI_TRANSIT_LIVE_MIN_VEHICLES` (10), `DELHI_TRANSIT_MIN_ROUTE_COVERAGE`, `DELHI_TRANSIT_LIVE_*` poller settings (see `core/config.py`).

## Commands

Python commands run from `backend/` (`cd backend`); shell scripts from the repository root. These match the mission `services.yaml`.

- Install: `uv sync --frozen`
- Frontend (from `frontend/`): `bun install --frozen-lockfile`, `bun run typecheck`, `bun run build`, `bun test`, dev server on 3110: `bun run dev`
- Tests: `uv run pytest -q`
- Bytecode check: `uv run python -m compileall -q src`
- Lint: `uv run ruff check src tests`
- Acquire all GPS days: `uv run python -m delhi_transit.data.acquire`
- One GPS day: `uv run python -m delhi_transit.data.extract_day --day 2026-10-15 --output /tmp/repro15.parquet`
- 717A passages (canonical 10 days and coverage report): `uv run python -m delhi_transit.data.stop_passages --overwrite`
- 717A passages for one day (never touches the canonical report): `uv run python -m delhi_transit.data.stop_passages --day 2026-10-22 --output-dir /tmp/passages-check --overwrite`
- Evaluation and selection: `uv run python -m delhi_transit.model.evaluate`
- Re-apply the selection rule to `reports/eval-full.csv`: `uv run python -m delhi_transit.model.selection`
- OTP graph build: `otp/build.sh`. OTP serve (3102): `otp/serve.sh`
- API serve (3100): `DELHI_TRANSIT_DATA_DIR=$DELHI_TRANSIT_SCRATCH_DIR/data DELHI_TRANSIT_OTP_URL=http://localhost:3102/otp/gtfs/v1 uv run uvicorn delhi_transit.api:app --host 127.0.0.1 --port 3100`
- Live poller, single poll: `uv run python -m delhi_transit.live.poller --once`
- Google comparator render (no network): `uv run python -m delhi_transit.comparator --render-only`
- Google comparator collection (planner calls, cached Google only): `uv run python -m delhi_transit.comparator --date 2026-11-24 --cache-only`
- Demo recording (API started with `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45`; the normal default is 600 s): `demo/record_demo.sh`
- Live check recording (from the repository root; raw data stays outside the repo): `livecheck/launch.sh $DELHI_TRANSIT_SCRATCH_DIR/live-check/2026-11-24 23:30` (extra options go to `livecheck run`, for example `--google-max 0`)
- Live check actuals and report: `uv run python -m delhi_transit.livecheck actuals --in $DELHI_TRANSIT_SCRATCH_DIR/live-check/2026-11-24` then `uv run python -m delhi_transit.livecheck analyze --in $DELHI_TRANSIT_SCRATCH_DIR/live-check/2026-11-24 --report-dir ../reports`
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
- Selection ([`reports/eval-full.md`](reports/eval-full.md), [`reports/selection.json`](reports/selection.json)): 13,299 held-out observed-only queries; MAE rung 0 87.273 s, rung 1 72.766 s, rung 4 60.178 s. Rung 4 ships with 79.156% pooled `[p10, p90]` coverage (marginal; per day-direction group 76.197% to 81.208%, two of six groups below 80%) and 81.518 s pooled median width.
- Google ([`reports/google-comparator.md`](reports/google-comparator.md)): 90 slots on 2026-11-24, 19 compared, 49 abstentions, 22 excluded because the reported option was unsupported; mean difference (ours minus Google) +509 s; 94.7% of Google durations inside our window; 92/300 calls used.
- **Live check, 2026-11-24** ([`reports/live-check-2026-11-24.md`](reports/live-check-2026-11-24.md)): a shortened recording, 21:04 to 21:44 IST (40.4 min, after the evening peak; 98/98 polls ok). 108 live `/journeys` responses in three slots, all `ok`. 66 predicted 717A ride legs, 0 matched to an actual ride (48 had an extrapolated actual endpoint, 18 boarded after the last recorded run; most rides predicted in the last two slots end after the recording stopped), so the check gives no accuracy number. One Google call was made at 21:45 IST before the run was stopped (ledger 93/300).
