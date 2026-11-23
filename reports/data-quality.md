# GPS data quality — October 2026 DTC bus feed

Updated 2026-11-23 from `data/gps/day=*.parquet` (PS1_DATA_DIR=`/home/factory-user/ps1-scratch/data`, 8.869 GB total across 31 days) after re-acquiring the 10 passage days (`2026-10-05, 06, 07, 08, 13, 14, 15, 22, 27, 28`) with the hardened deterministic extractor and verifying all 31 days in place.

## Pipeline cleaning rules

Each raw archive day (SQLite `vehicle_feed`) is converted to Parquet by `src/ps1/extract_day.py`:

1. `ts` = `TRY_CAST(timestamp AS BIGINT)` (source stores epoch seconds UTC as text); rows with unparseable timestamps are dropped.
2. Delhi bbox filter: rows with null coords or outside 27.5–29.0 N / 76.7–77.6 E are dropped.
3. Deterministic first-row dedupe on `(vehicle_id, ts, lat, lng)` ordered by SQLite `rowid ASC` (the feed re-emits identical positions; this is the documented ~38% duplicate class).
4. `route_id` = `TRY_CAST(route_id AS INTEGER)` (stored as REAL in the archive); unparseable values become NULL.
5. Staged verification and atomic publication: each day is written to a staged `.parquet` file, checked by `verify_day` (exact schema, row/vehicle counts, IST archive-day window, bbox, and zero duplicates), and published atomically via `os.replace` so failed runs never remove an existing canonical Parquet.

**Tie-break status across the 31 days:** The 10 passage days (`2026-10-05, 06, 07, 08, 13, 14, 15, 22, 27, 28`) were re-acquired on 2026-11-23 with the deterministic `rowid`-ordered first-row dedupe extractor. The other 21 October days still use the older arbitrary tie-break until `reacquire-remaining-days` runs (an Oct 22 audit measured 13,942 `route_id` [0.258%] and 14,247 `trip_id` [0.264%] disagreements across 5,402,665 duplicate-key groups between the legacy pick and SQLite `rowid` first-row selection; row counts, vehicle counts, bbox, and timestamp bounds are invariant).

## Per-day reconciliation (raw vs clean)

`rows_in` is the raw SQLite row count recorded in `data/download.log`; `rows_out` is the Parquet row count. DuckDB checks per day: duplicate groups on `(vehicle_id, ts, lat, lng)` = 0, null/out-of-bbox coordinates = 0, fractional `route_id` = 0 for every day, and all 31 files pass hardened `verify_day` via `ps1.acquire` (verified 2026-11-23).

| Day | Rows in | Rows out | Removed % | Vehicles | Dup groups | Bbox viol. | route_id NULL | min ts (IST) | max ts (IST) | Dedupe tie-break |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| 2026-10-01 | 31,290,299 | 19,204,544 | 38.62 | 6,264 | 0 | 0 | 0 | 2026-09-30 23:30:51 | 2026-10-01 23:59:44 | legacy |
| 2026-10-02 | 29,316,111 | 18,822,155 | 35.80 | 6,104 | 0 | 0 | 0 | 2026-10-01 23:30:06 | 2026-10-02 23:59:44 | legacy |
| 2026-10-03 | 31,516,372 | 19,991,945 | 36.57 | 6,264 | 0 | 0 | 0 | 2026-10-02 23:30:58 | 2026-10-03 23:59:44 | legacy |
| 2026-10-04 | 30,213,040 | 19,544,131 | 35.31 | 6,197 | 0 | 0 | 0 | 2026-10-03 23:30:44 | 2026-10-04 23:59:43 | legacy |
| 2026-10-05 | 30,991,505 | 18,870,774 | 39.11 | 6,262 | 0 | 0 | 0 | 2026-10-04 23:30:01 | 2026-10-05 23:59:44 | rowid first |
| 2026-10-06 | 30,857,544 | 18,857,803 | 38.89 | 6,269 | 0 | 0 | 0 | 2026-10-05 23:32:01 | 2026-10-06 23:59:44 | rowid first |
| 2026-10-07 | 25,535,790 | 15,770,083 | 38.24 | 6,258 | 0 | 0 | 0 | 2026-10-06 23:30:47 | 2026-10-07 19:53:23 | rowid first |
| 2026-10-08 | 30,213,919 | 18,929,870 | 37.35 | 6,238 | 0 | 0 | 0 | 2026-10-08 02:02:10 | 2026-10-08 23:59:43 | rowid first |
| 2026-10-09 | 29,099,431 | 18,071,519 | 37.90 | 6,198 | 0 | 0 | 0 | 2026-10-08 23:30:09 | 2026-10-09 23:59:44 | legacy |
| 2026-10-10 | 26,763,426 | 17,016,411 | 36.42 | 5,870 | 0 | 0 | 0 | 2026-10-09 23:30:37 | 2026-10-10 23:59:44 | legacy |
| 2026-10-11 | 25,936,964 | 17,185,361 | 33.74 | 5,632 | 0 | 0 | 0 | 2026-10-10 23:32:00 | 2026-10-11 23:59:34 | legacy |
| 2026-10-12 | 26,210,474 | 16,431,708 | 37.31 | 5,848 | 0 | 0 | 0 | 2026-10-11 23:30:13 | 2026-10-12 23:59:49 | legacy |
| 2026-10-13 | 28,695,465 | 17,782,690 | 38.03 | 6,021 | 0 | 0 | 0 | 2026-10-12 23:30:59 | 2026-10-13 23:59:44 | rowid first |
| 2026-10-14 | 28,847,932 | 17,715,593 | 38.59 | 6,032 | 0 | 0 | 0 | 2026-10-13 23:31:19 | 2026-10-14 23:59:38 | rowid first |
| 2026-10-15 | 29,271,900 | 18,273,380 | 37.57 | 6,093 | 0 | 0 | 0 | 2026-10-14 23:30:13 | 2026-10-15 23:59:39 | rowid first |
| 2026-10-16 | 29,043,138 | 17,961,738 | 38.15 | 6,073 | 0 | 0 | 0 | 2026-10-15 23:30:41 | 2026-10-16 23:59:44 | legacy |
| 2026-10-17 | 30,024,953 | 18,701,841 | 37.71 | 6,125 | 0 | 0 | 0 | 2026-10-17 00:00:00 | 2026-10-17 23:59:33 | legacy |
| 2026-10-18 | 28,848,015 | 18,339,084 | 36.43 | 6,107 | 0 | 0 | 0 | 2026-10-17 23:30:10 | 2026-10-18 23:59:43 | legacy |
| 2026-10-19 | 29,270,257 | 18,284,760 | 37.53 | 6,189 | 0 | 0 | 0 | 2026-10-18 23:31:25 | 2026-10-19 23:59:48 | legacy |
| 2026-10-20 | 27,362,204 | 17,362,435 | 36.55 | 6,119 | 0 | 0 | 0 | 2026-10-19 23:45:05 | 2026-10-20 23:59:38 | legacy |
| 2026-10-21 | 30,243,480 | 18,593,596 | 38.52 | 6,242 | 0 | 0 | 0 | 2026-10-20 23:44:53 | 2026-10-21 23:59:48 | legacy |
| 2026-10-22 | 30,971,604 | 18,785,849 | 39.34 | 6,270 | 0 | 0 | 0 | 2026-10-21 23:45:33 | 2026-10-22 23:59:49 | rowid first |
| 2026-10-23 | 31,377,176 | 19,027,013 | 39.36 | 6,290 | 0 | 0 | 0 | 2026-10-22 23:45:27 | 2026-10-23 23:59:42 | legacy |
| 2026-10-24 | 31,327,972 | 18,588,058 | 40.67 | 6,287 | 0 | 0 | 0 | 2026-10-23 23:45:16 | 2026-10-24 23:59:42 | legacy |
| 2026-10-25 | 29,396,065 | 18,188,802 | 38.13 | 6,221 | 0 | 0 | 0 | 2026-10-24 23:46:34 | 2026-10-25 23:59:53 | legacy |
| 2026-10-26 | 29,972,620 | 17,804,250 | 40.60 | 6,209 | 0 | 0 | 0 | 2026-10-26 00:00:00 | 2026-10-26 23:59:37 | legacy |
| 2026-10-27 | 31,038,199 | 17,490,107 | 43.65 | 6,295 | 0 | 0 | 0 | 2026-10-26 23:47:10 | 2026-10-27 23:59:43 | rowid first |
| 2026-10-28 | 31,359,638 | 17,800,017 | 43.24 | 6,328 | 0 | 0 | 0 | 2026-10-27 23:45:40 | 2026-10-28 23:59:52 | rowid first |
| 2026-10-29 | 28,264,904 | 16,329,823 | 42.23 | 6,319 | 0 | 0 | 0 | 2026-10-29 00:00:00 | 2026-10-29 23:59:37 | legacy |
| 2026-10-30 | 28,643,700 | 16,336,026 | 42.97 | 6,266 | 0 | 0 | 0 | 2026-10-29 23:46:02 | 2026-10-30 23:59:38 | legacy |
| 2026-10-31 | 30,972,964 | 18,296,405 | 40.93 | 6,327 | 0 | 0 | 0 | 2026-10-30 23:47:06 | 2026-10-31 23:59:43 | legacy |
| **Total** | **912,877,061** | **560,357,771** | **38.62** | | **0** | **0** | | | | **10 rowid first / 21 legacy** |

## Duplicate-removal reconciliation

Per-day removal share: mean 38.56%, min 33.74% (2026-10-11), max 43.65%. Tolerance: the expected duplicate class is ~38%; every day falls inside 30–45%, so no day deviates beyond the documented tolerance.

Composition of the removal, measured on 2026-10-15 with an instrumented run of the same pipeline stages (raw SQLite re-extracted 2026-11-23, then deleted):

| Stage | Rows | Dropped | Share of raw |
|---|---:|---:|---:|
| raw SQLite | 29,271,900 | | |
| after ts/bbox filter | 29,247,452 | 24,448 | 0.084% |
| after dedupe (final Parquet) | 18,273,380 | 10,974,072 | 37.490% |

The ~38% removal is therefore dominated by the `(vehicle_id, ts, lat, lng)` duplicate class; the bbox/invalid-ts filter accounts for ~0.1%. Day-to-day variance in the total removal share comes from the duplicate share, not from coordinate loss.

## Notes

- 2026-10-07 is truncated by the source (last observation 19:53 IST) and has the lowest row count; it is usable for training but never an evaluation day. Its following night is also affected: 2026-10-08 starts at 02:02 IST, so 00:00-02:02 IST of Oct 8 is a genuine source gap.
- Archive-day window: every nominal day d starts between 23:30 IST of d-1 and 00:00 IST of d, and ends 23:59:3x-23:59:5x IST of d (the nominal window runs ~23:30 to ~23:30 IST; the last ~30 min is the source's stale-entity retention). The full nominal IST calendar date 00:00-23:59 is covered by every day except the truncated 07 and late-starting 08 above. All timestamps lie within 2026-09-30 23:30 IST to 2026-11-01 00:00 IST, i.e. each file is confined to its nominal October archive day under this documented window.
- `route_id` NULL count is 0 on every day: every retained archive `route_id` cast cleanly to INTEGER (the REAL storage held integral values). Rows with a NULL route_id would still be kept (position valid) but excluded from route-level analysis.

## Reproduction (one day, end-to-end)

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.extract_day --day 2026-10-15
```

Verified 2026-11-23 with `--output /tmp/repro15.parquet`: exit 0, rows_out=18,273,380 (identical to the existing `day=2026-10-15.parquet`), vehicles=6,093, min_ts=1792000813, max_ts=1792088979, 0 duplicate groups, 0 bbox violations, 0 fractional route_ids; intermediate SQLite deleted after conversion.
