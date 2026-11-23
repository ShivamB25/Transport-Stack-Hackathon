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

## Checks

```bash
/home/factory-user/ps1-scratch/venv/bin/python -m pytest tests -q
/home/factory-user/ps1-scratch/venv/bin/python -m compileall -q src
/home/factory-user/ps1-scratch/venv/bin/ruff check src tests
```
