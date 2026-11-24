# Delhi bus travel times and journey planner (PS1)

This project estimates how long Delhi DTC bus journeys actually take. It reconstructs stop arrivals from the October 2026 GPS archive, trains travel-time models on them, and serves a journey planner that times OpenTripPlanner (OTP) candidate itineraries with those models instead of the timetable. Every answer is labelled `live` or `replay`, carries per-leg provenance and a door-to-door `[p10, p90]` window, and the API abstains when it does not have the data to answer.

## How it works

1. **Data.** `delhi_transit.data.acquire` range-reads the 31-day GPS archive one day at a time, removes duplicate pings and out-of-area points, and writes one Parquet file per day. `delhi_transit.data.stop_passages` matches pings to the 717A stop pattern geometrically (60 m radius; GPS `trip_id` is not a GTFS key) and interpolates unobserved stops. Each passage records `available_at`, the time it became knowable from the GPS.
2. **Model.** `delhi_transit.model.evaluate` scores a ladder of models on whole held-out days: timetable (rung 0), previous-run persistence (rung 1), historical medians (1h, 2), a per-bus delay adjustment (3) and LightGBM quantile regression with split-conformal calibration (4). `delhi_transit.model.selection` ships the highest rung that beats persistence on observed stop pairs. Rung 4 is shipped.
3. **Planning.** OTP 2.8.1 proposes candidate bus itineraries from the static GTFS and an OSM street graph. `delhi_transit.planner` (`itinerary.retime_itinerary`) replaces every transit leg's scheduled time with a model estimate, estimates boarding waits, simulates 192 joint scenarios to get transfer miss probabilities and the end-to-end `[p10, p90]` window (per-leg p90s are never summed), and labels each leg `model_live`, `model_replay`, `historical_fallback` or `timetable_fallback`.
4. **Live and replay.** In live mode a background poller reads the Delhi GTFS-RT VehiclePositions feed every 25 s. October 2026 departures run in replay mode from the reconstructed passages, using only observations available before the requested departure.
5. **Abstention.** `GET /journeys` returns `status: "insufficient_data"` with a reason code (`stale_feed`, `feed_unavailable`, `insufficient_coverage`, `insufficient_route_coverage`, `replay_data_unavailable`) instead of options when the feed is stale or no candidate route has observations.

The Leaflet UI at `http://localhost:3100/` shows the options on a map, the live/replay badge and feed age, the abstention panel, and a scripted October 22 missed-transfer replay.

## Quickstart

Both paths need the data directory (GPS Parquet, passages, GTFS, model artifact), the OTP directory with the jar and a built graph, and a `.env` file in the repository root. Copy `.env.example` to `.env` and fill in `DELHI_TS_API_KEY` (live feed and static GTFS) and `GOOGLE_MAPS_API_KEY` (comparator only). Keys are read in-process and never logged.

### Native

Requirements: [uv](https://docs.astral.sh/uv/) (Python 3.12; `backend/pyproject.toml` and `backend/uv.lock` pin every version), Temurin 21 at `/home/factory-user/ps1-scratch/jdk`, data in `/home/factory-user/ps1-scratch/data` (`DELHI_TRANSIT_DATA_DIR`) and OTP files in `/home/factory-user/ps1-scratch/otp` (`DELHI_TRANSIT_OTP_DIR`).

```bash
otp/build.sh                  # one-time: downloads the OTP jar and OSM extract if missing, builds the graph (about 1.5 min)
otp/serve.sh                  # OTP on port 3102; leave it running and start the API in another shell
cd backend
uv sync --frozen
DELHI_TRANSIT_DATA_DIR=/home/factory-user/ps1-scratch/data DELHI_TRANSIT_OTP_URL=http://localhost:3102/otp/gtfs/v1 \
  uv run uvicorn delhi_transit.api:app --host 127.0.0.1 --port 3100
```

Health checks:

```bash
curl -sf -X POST http://localhost:3102/otp/gtfs/v1 -H 'Content-Type: application/json' \
  -d '{"query":"{ __schema { queryType { name } } }"}'
curl -sf http://localhost:3100/health
```

### Docker Compose

`docker-compose.yml` defines `otp` (the `eclipse-temurin:21-jre` image running the bind-mounted OTP jar and graph), `api` (built from `Dockerfile`; Python 3.12 slim, uvicorn on port 3100, waits for a healthy `otp`) and an optional `otp-build` service under the `build` profile that runs `otp/build.sh` inside the container. Data and the graph are bind-mounted, and keys come from `.env` at runtime; neither is copied into an image. Host paths default to the native layout and can be changed with `DELHI_TRANSIT_HOST_DATA_DIR` and `DELHI_TRANSIT_HOST_OTP_DIR`. Ports bind to `127.0.0.1` unless `DELHI_TRANSIT_BIND_ADDR` is set.

```bash
docker compose --profile build run --rm otp-build   # one-time graph build
docker compose up -d --build
docker compose ps                                   # both services should report healthy
```

## Getting the data

- **GPS archive.** `uv run python -m delhi_transit.data.acquire` (from `backend/`) fetches all 31 October days (two at a time, resumable from `data/download.log`). It reads the 7z archive with HTTP range requests and never downloads it whole; at most two unpacked SQLite days exist at once and each is deleted after conversion. Output: about 10 GB of Parquet in `data/gps/`.
- **Static GTFS.** Download `delhi_buses_static_gtfs_v1.zip` from `https://dts-backend.transportstack.in/api/dataset/otd/get-file?agency=delhi-buses&category=static_gtfs&filename=delhi_buses_static_gtfs_v1.zip` with the `x-api-key` header set to `DELHI_TS_API_KEY`, and unzip it into `data/gtfs/` (keep the zip there too; the OTP build reads it). This repository uses feed v32.
- **OTP inputs.** `otp/build.sh` downloads `otp-shaded-2.8.1.jar` from Maven Central and the BBBike `NewDelhi.osm.pbf` extract when they are missing.
- **Passages and model.** Run the stop-passage and evaluation commands below. They write `data/passages/`, `data/model/` and the committed reports.

## Commands

Python commands run from `backend/` after `uv sync --frozen`. Report paths default to the repository's `reports/` directory whatever the working directory.

| Task | Command |
|---|---|
| Tests | `uv run pytest -q` |
| Bytecode check | `uv run python -m compileall -q src` |
| Lint | `uv run ruff check src tests` |
| Extract one GPS day | `uv run python -m delhi_transit.data.extract_day --day 2026-10-15 --output /tmp/repro15.parquet` |
| 717A stop passages (10 days) and `reports/coverage-717a.md` | `uv run python -m delhi_transit.data.stop_passages --overwrite` |
| Evaluate all rungs, write reports, selection and model artifact | `uv run python -m delhi_transit.model.evaluate` |
| Re-apply the selection rule to `reports/eval-full.csv` | `uv run python -m delhi_transit.model.selection` |
| One live feed poll | `uv run python -m delhi_transit.live.poller --once` |
| Render the Google comparison from frozen inputs (no network calls) | `uv run python -m delhi_transit.comparator --render-only` |
| Record the demo (from the repository root) | `demo/record_demo.sh` (API started with `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45`; the normal stale-feed threshold is 600 s) |

Example API calls:

```bash
# Live: departs now (Asia/Kolkata) when depart is omitted
curl -s 'http://localhost:3100/journeys?from=Kashmere%20Gate%20ISBT&to=Nehru%20Place'
# Replay on 717A, October 22 08:30 IST
curl -s 'http://localhost:3100/journeys?from=Badarpur%20Border%20(T)&to=Kapashera%20Border&depart=2026-10-22T08:30:00%2B05:30'
# Scripted missed-transfer replay
curl -s 'http://localhost:3100/replay/demo'
```

`from` and `to` accept `lat,lng` or an exact GTFS stop name. `depart` is ISO 8601 with an offset. OpenAPI docs are at `http://localhost:3100/docs`.

How `depart` and `mode` combine:

- **No `depart`.** Replay is used only when it is explicit: `mode=replay`, or the default `mode=auto` with `DELHI_TRANSIT_MODE=replay`. The request then uses the scripted demo departure, 2026-10-22T08:30+05:30. In every other case the request departs now in Asia/Kolkata in live mode.
- **Explicit `depart`.** `mode=live` or `mode=replay` is used as given. With `mode=auto`, `DELHI_TRANSIT_MODE` (`live` or `replay`) decides if it is set; otherwise October 2026 departures run in replay and all others in live. Replay outside October 2026 returns 422.

## Results

All numbers come from the committed reports and are regenerated by the commands above.

- **GPS acquisition** ([`reports/data-quality.md`](reports/data-quality.md)): 31/31 days, 912,877,061 raw rows reduced to 560,357,771 (38.62% removed, mostly repeated identical pings). Every file has 0 duplicate groups and 0 out-of-area points.
- **717A stop passages** ([`reports/coverage-717a.md`](reports/coverage-717a.md)): 2,546 runs over 10 days. 48.55% of UP (2171) and 50.75% of DOWN (2170) stop passages are directly observed; the rest are interpolated.
- **Persistence gate, October 22, observed stop pairs** ([`reports/eval-baselines.md`](reports/eval-baselines.md)): persistence beats the timetable in both directions, UP 79.936 s vs 95.608 s MAE (2,397 queries) and DOWN 87.112 s vs 101.801 s (2,193 queries).
- **Model selection, held-out Oct 22, 27 and 28** ([`reports/eval-full.md`](reports/eval-full.md), [`reports/selection.json`](reports/selection.json)), 13,299 observed-only stop-pair queries:

  | Rung | Model | MAE (s) | vs persistence (s) |
  |---|---|---:|---:|
  | 0 | Timetable | 87.273 | +14.506 |
  | 1 | Previous-run persistence | 72.766 | 0.000 |
  | 1h | Stretch-hour median (report only) | 74.525 | +1.759 |
  | 2 | + day type | 69.498 | -3.269 |
  | 3 | + this bus's last delay | 69.558 | -3.209 |
  | **4** | **LightGBM quantile + conformal (shipped)** | **60.178** | **-12.588** |

  Rung 4 beats persistence in all 6 day-direction groups. Its nominal 80% `[p10, p90]` intervals cover 79.156% of held-out observations with a pooled median width of 81.518 s.
- **Google Routes comparison, Tuesday 2026-11-24** ([`reports/google-comparator.md`](reports/google-comparator.md)): of 90 planned OD-time slots (30 pairs at 08:30, 14:00 and 18:30 IST), 41 were compared and 49 are recorded abstentions. On the compared slots our door-to-door estimate is on average 428 s (7.1 min) longer than Google's bus-only duration, and 97.6% of Google durations fall inside our `[p10, p90]` window. 92 of the 300 allowed Google calls were used.
- **OTP smoke test** ([`reports/otp-smoke.md`](reports/otp-smoke.md)): the graph builds in 1m22.896s under the 2.5 GB heap cap and returns five itineraries for Kashmere Gate ISBT to Nehru Place.

Method details are in [`docs/technical-note.md`](docs/technical-note.md), the frozen evaluation protocol in [`docs/eval-protocol.md`](docs/eval-protocol.md), and OTP setup in [`docs/otp.md`](docs/otp.md).

## Demo

[`demo/demo.webm`](demo/demo.webm), recorded with [`demo/record_demo.sh`](demo/record_demo.sh), shows:

1. A live query (Kashmere Gate ISBT to Nehru Place) with the `live` badge, arrival windows and per-leg provenance.
2. A replay query on 717A for October 22 08:30, with legs timed by `model_replay`.
3. The same Kashmere Gate query in October replay, which abstains because no candidate route has replay observations.
4. The October 22 missed-transfer replay: the timetable recommends a 717AUP transfer at Hamdard Nagar / Sangam Vihar, the advice flips as the first bus falls behind, and the observed GPS passages show the connection was missed by 27m 43s.
5. A stale live feed producing "Not enough data", then recovery after the poller resumes. The recording runs the API with `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=45` so the feed goes stale quickly; the normal default is 600 s.

## Known limitations

- **October data.** Models and replay use October 2026 GPS. Live mode applies them to current feed positions; nothing has been re-evaluated on later months.
- **One modelled corridor.** Stop passages, replay observations and the learned rung 4 model exist only for route 717A (UP 2171, DOWN 2170). On any other route, live mode blends the route's fresh vehicle speeds with the historical profile and labels the ride `model_live` (502UP in [`reports/google-comparator.md`](reports/google-comparator.md) is an example). Without fresh vehicles, a ride falls back to the historical profile (`historical_fallback`) and then to the timetable (`timetable_fallback`). In replay mode, journeys with no 717A leg abstain with `insufficient_route_coverage`.
- **GTFS v32 drift.** The static feed (v32, 6,862 stops) is newer than the snapshot behind the older reference numbers (41 s persistence vs 59 s timetable MAE). Those numbers are not reproduced; the results above are on v32.
- **About half the stops are observed.** Roughly 50% of stop passages are observed and the rest are schedule-interpolated, so model selection uses observed stop pairs only.
- **Google is a comparator, not ground truth.** It covers one future date, includes only 41 of 90 slots, and measures journeys differently (our totals include the first boarding wait). Google cannot be queried for October dates.
- **Docker is validated statically.** Docker is not installed on the development machine. The Compose file is checked against the compose-spec schema, and the same container commands were run natively; the images have not been built here.

## Repository layout

`backend/` is the uv project: `src/delhi_transit/` holds the `core`, `data`, `model`, `planner`, `live`, `comparator` and `api` packages and `tests/` the pytest suite mirroring them; `otp/` the OTP build and serve scripts and build config; `reports/` the committed results; `docs/` the technical note, protocol and OTP notes; `demo/` the recording. [`AGENTS.md`](AGENTS.md) describes each module and the rules contributors follow.
