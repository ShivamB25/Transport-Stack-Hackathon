# AGENTS.md

Keep this guide short and accurate. Update it in the same commit as any change to standing rules, settled project calls, commands, or headline results. Never write secret values here.

## Purpose

Delhi DTC bus travel-time modelling and journey planning over the October 2026 GPS archive (31 days), static GTFS feed v32, the live GTFS-RT VehiclePositions feed, and OpenTripPlanner 2.8.1. OTP only proposes candidate itineraries; this codebase owns transit leg timing, transfer risk, `[p10, p90]` windows, and abstention.

## Persistent Principles

- **Causality:** Compute every passage `available_at` as the latest of its per-passage rule (observed = own ping; interior interpolated = later anchor; extrapolated = anchor used), the run's second surviving anchor, and the ping after which every raw-GPS prefix reconstruction reproduces it. Consume only passages with `available_at` strictly before the query time in features, replay, and evaluation.
- **Replay cutoff:** Use one observation cutoff—the requested departure—for every leg and the quiet-hour comparison in a replay request. Invoke a persisted model artifact only when eligible as of that cutoff.
- **Clock:** Normalise request times to Asia/Kolkata (`+05:30`) before service-date, hour, and quiet-hour logic. Resolve GTFS service dates for midnight-crossing runs across `day-1`, `day`, and `day+1`.
- **Evaluation:** Use whole-day splits only (train Oct 5–8 and 13–15; held-out Oct 22, 27, 28; exclude holidays Oct 2, 20, 26; never evaluate on Oct 7). Gate and select models exclusively on the `observed-only` stratum; refuse non-`observed-only` selection files. Never sum per-leg `p90` quantiles into an end-to-end `p90`.
- **Matching:** Match GPS pings to stops geometrically. Never join GPS `trip_id` to GTFS `trip_id`, and never fuzzy-match stop names city-wide.
- **Google comparator:** Reserve every paid call atomically in the fixed mission ledger before sending a request, staying within the 300-call mission-wide cap. Seed a missing fixed ledger from the canonical `data/google/call_counter.json`, never from zero; refuse paid misses when prior usage cannot be established. Cache every response under `data/google/`. Never query past or October dates. Always send `languageCode="en"` and retain only bus-only itineraries. Treat Google as a comparator, never ground truth.
- **Provenance and abstention:** Report request `mode` (`live`/`replay`) independently from per-leg provenance (`model_live`, `model_replay`, `historical_fallback`, `timetable_fallback`) and observed coverage. Reject live entities older than the freshness threshold at request time. Abstain (`status="insufficient_data"`) on a stale or missing feed, too few fresh vehicles, no supported candidate route, or no replay observations before the cutoff. Never synthesise a fresh replay timestamp.
- **UI state:** Let the latest user action win so superseded responses never overwrite newer UI state. Drive the abstention badge from the API `abstention.code`.
- **Secrets and paths:** Read `.env` secrets only via `delhi_transit.core.config.load_env_key` and never print, persist, or log secret values. Spell machine paths in committed reports through `portable_path` (repo-relative or `$DELHI_TRANSIT_*`-relative).
- **Commits:** Commit and push to `origin/main` after each passing coherent step using plain commit messages without co-author or tool trailers.

## Settled Calls

- **Toolchains:** Manage Python 3.12 exclusively with `uv` (`backend/uv.lock`) and the React 19 / Vite / TypeScript / Tailwind v4 / shadcn/ui / TanStack Query / plain-Leaflet frontend exclusively with `bun` (`frontend/bun.lock`). Run Python via `uv run` from `backend/`.
- **Runtime and ports:** Keep services within `3100–3199`: `3100` (API + built `frontend/dist`), `3102` (OTP 2.8.1 with `-Xmx2500m`), `3110` (Vite dev proxy), `3120` (livecheck API).
- **Environment paths:** Configure external data/work/OTP/JDK locations via `DELHI_TRANSIT_SCRATCH_DIR`, `DELHI_TRANSIT_DATA_DIR`, `DELHI_TRANSIT_WORK_DIR`, `DELHI_TRANSIT_OTP_DIR`, `DELHI_TRANSIT_JAVA`, `DELHI_TRANSIT_OTP_URL`, and `DELHI_TRANSIT_FRONTEND_DIR` (see `core/config.py` and `README.md`). Importing `delhi_transit.api` builds the default app and reads `$DELHI_TRANSIT_DATA_DIR/gtfs/stops.txt`, so pytest requires a data directory with `gtfs/stops.txt`.
- **Default `depart`:** Omit `depart` to plan from now in Asia/Kolkata in `live` mode, or from `2026-10-22T08:30+05:30` when replay is explicit (`mode=replay`, or `mode=auto` with `DELHI_TRANSIT_MODE=replay`).
- **Model scope and shipped rung:** Stop passages and learned Rung 4 (LightGBM quantile + split-conformal) cover route 717A (`2171` UP, `2170` DOWN) only; all other routes use the causal Rung 1 fallback chain in `planner/rides.py`.
- **Known archive gaps:** Archive day `d` runs from ~23:30 IST on `d-1` to 23:59 IST on `d`. `2026-10-07` ends at 19:53:23 IST (training only); `2026-10-08` starts at 02:02:10 IST. Static GTFS v32 has 6,862 stops.
- **No CI:** Run backend and frontend checks locally before merging; frontend has no automated test suite.
- **Headline committed results:**
  - GPS (`reports/data-quality.md`): 31/31 days, 912,877,061 raw rows to 560,357,771 clean (38.62% removed), 9.955 GB Parquet, 0 duplicate groups, 0 bbox violations.
  - 717A passages (`reports/coverage-717a.md`): 2,546 runs over 10 days; observed coverage 48.55% UP (`2171`), 50.75% DOWN (`2170`).
  - Persistence gate (`reports/eval-baselines.md`): Oct 22 observed-only persistence beats timetable—UP 79.936 s vs 95.608 s (2,397 queries), DOWN 87.112 s vs 101.801 s (2,193).
  - Selection (`reports/eval-full.md`, `reports/selection.json`): 13,299 held-out observed-only queries; MAE rung 0 87.273 s, rung 1 72.766 s, rung 4 60.178 s. Rung 4 ships with 79.156% pooled `[p10, p90]` coverage (76.197%–81.208% across 6 day-direction groups) and 81.518 s pooled median width.
  - Google comparator (`reports/google-comparator.md`): 90 slots on 2026-11-24, 19 compared, 49 abstentions, 22 `our_unsupported_option`; mean diff (ours minus Google) +273 s excluding first boarding wait, +509 s door-to-door; 94.7% inside `[p10, p90]`; 92/300 calls used.
  - Live check, 2026-11-24 (`reports/live-check-2026-11-24.md`): shortened 40.4 min post-peak window (21:04–21:44 IST; 98/98 polls ok; 108/108 live `/journeys` ok; 0/66 717A legs matched; ledger 93/300).
  - Live check, 2026-11-25 (`reports/live-check-2026-11-25.md`): 300.4 min morning window (06:29–11:30 IST; 722/722 polls ok; 396/396 live `/journeys` ok; 177/287 predicted 717A legs matched at 227.5 s MAE vs 393.3 s timetable MAE; 67 observed-only matched legs at 300.5 s MAE vs 492.4 s timetable MAE, 94.0% `[p10, p90]` coverage; 0 Google calls).

## Commands

Run Python commands from `backend/`, frontend commands from `frontend/`, and shell scripts from the repository root.

- Backend install & checks: `uv sync --frozen`, `uv run ruff check src tests`, `uv run python -m compileall -q src`, `uv run pytest -q`
- Frontend install & checks: `bun install --frozen-lockfile`, `bun run lint`, `bun run typecheck`, `bun run build`, `bun run dev` (port 3110)
- GPS archive: `uv run python -m delhi_transit.data.acquire` (31 days), `uv run python -m delhi_transit.data.extract_day --day 2026-10-15 --output /tmp/repro15.parquet`
- 717A passages: `uv run python -m delhi_transit.data.stop_passages --overwrite` (canonical 10 days + `reports/coverage-717a.md`), or `--day 2026-10-22 --output-dir /tmp/passages-check --overwrite` (scoped; never touches canonical report)
- Evaluation & selection: `uv run python -m delhi_transit.model.evaluate`, `uv run python -m delhi_transit.model.selection`
- OTP & API: `otp/build.sh`, `otp/serve.sh` (3102), `DELHI_TRANSIT_DATA_DIR=$DELHI_TRANSIT_SCRATCH_DIR/data DELHI_TRANSIT_OTP_URL=http://localhost:3102/otp/gtfs/v1 uv run uvicorn delhi_transit.api:app --host 127.0.0.1 --port 3100`
- Live poller & comparator: `uv run python -m delhi_transit.live.poller --once`, `uv run python -m delhi_transit.comparator --render-only`, `uv run python -m delhi_transit.comparator --date 2026-11-24 --cache-only`
- Live check & demo: `livecheck/launch.sh $DELHI_TRANSIT_SCRATCH_DIR/live-check/<day> <HH:MM>` (extra flags pass to `livecheck run`), `uv run python -m delhi_transit.livecheck actuals --in <dir>`, `uv run python -m delhi_transit.livecheck analyze --in <dir> --report-dir ../reports`, `demo/record_demo.sh` (requires API started with `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45`)
- Docker Compose: `docker compose --profile build run --rm otp-build`, `docker compose up -d --build`
