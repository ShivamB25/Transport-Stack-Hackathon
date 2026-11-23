# Transport-Stack-Hackathon

Delhi DTC bus travel-time model and journey planner (PS1). Data lives outside
the repo in `PS1_DATA_DIR` (default `/home/factory-user/ps1-scratch/data`);
the Python venv is `/home/factory-user/ps1-scratch/venv`.

## Setup

```bash
/home/factory-user/ps1-scratch/venv/bin/python -m pip install -r requirements.txt
```

Configuration is environment-driven (`src/ps1/config.py`): `PS1_DATA_DIR`,
`PS1_OTP_URL` (default `http://localhost:3102/otp/gtfs/v1`), ports 3100 (API)
and 3102 (OTP), and the Delhi bbox 27.5-29.0 N / 76.7-77.6 E.

## OpenTripPlanner

Build the Delhi GTFS + BBBike NewDelhi graph and serve it on port 3102 (Java 21,
OTP 2.8.1, maximum heap 2500 MB):

```bash
scripts/otp_build.sh > /home/factory-user/ps1-scratch/otp/build.log 2>&1
scripts/otp_serve.sh
```

See `docs/otp.md` for inputs, the GraphQL `planConnection` smoke query, and schema
notes. `reports/otp-smoke.md` records the successful real-itinerary verification.

## GPS acquisition (October 2026)

The raw feed ships as one 16 GB non-solid 7z with one SQLite per day. We never
download the whole archive: `src/ps1/range_reader.py` range-reads single
members with `User-Agent: curl/8.5.0`.

Regenerate one day end-to-end from the archive:

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.extract_day --day 2026-10-15
```

This extracts only that day's SQLite, cleans it (dedupe on
`(vehicle_id, ts, lat, lng)`; `route_id` REAL to INTEGER, NULL if unparseable;
`ts` epoch seconds UTC; rows outside the Delhi bbox dropped), writes
`$PS1_DATA_DIR/gps/day=2026-10-15.parquet` (ZSTD), verifies row/vehicle counts
and the IST time span, and deletes the intermediate SQLite. Options:
`--output PATH` to write elsewhere, `--overwrite` to replace an existing file.

Full month (priority days 15, 22, 27, 28, 5, 6, 8, 13, 14 first; two workers;
resumes by verifying existing Parquet in place; appends to `download.log` and
rewrites `STATUS.md`):

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.acquire
```

## 717A stop passages

Reconstruct the training days (October 5–8 and 13–15) and held-out days
(October 22, 27, and 28). October 7 is training-only because its feed is truncated.

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.stop_passages
```

Use `--day 2026-10-22` for one day and `--overwrite` to replace existing outputs.
The command reads `$PS1_DATA_DIR/gps/day=YYYY-MM-DD.parquet` and the static GTFS,
then writes DuckDB-queryable Parquet to
`$PS1_DATA_DIR/passages/day=YYYY-MM-DD.parquet` plus
`reports/coverage-717a.md`. Each row includes the run key, stop sequence,
observed/interpolated source, epoch timestamp, scheduled timestamp, and schedule
error. GPS trip IDs are run labels only, not GTFS join keys. Matching uses a 60 m
radius, minimum-distance observations, and time-ordered anchors; gaps use
schedule-proportional interpolation. Runs with fewer than two anchors or an
observed span over six hours are excluded and counted in the report.

## 717A baseline evaluation

The frozen protocol is in `docs/eval-protocol.md`. Regenerate timetable (rung 0),
previous-run persistence (rung 1), and historical hourly medians (rung 1h) for
717A UP/DOWN on October 22, 27, and 28:

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate
```

The command writes `reports/eval-baselines.csv` and `reports/eval-baselines.md`
with paired MAE, median AE, p90 AE, share over 60 seconds, and observed versus
interpolated coverage for both observed-only and all-stop strata. All-stop metrics
include timetable-derived interpolated targets and carry a circularity caveat. The
strict October 22 persistence gate uses observed-only targets only and exits nonzero
if persistence fails in either direction; the reports remain available for inspection.

## Checks

```bash
/home/factory-user/ps1-scratch/venv/bin/python -m pytest tests -q
/home/factory-user/ps1-scratch/venv/bin/python -m compileall -q src
/home/factory-user/ps1-scratch/venv/bin/ruff check src tests
```
