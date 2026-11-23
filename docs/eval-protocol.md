# Evaluation protocol — 717A baselines

Status: frozen on 2026-11-23 before baseline fitting, prediction, or metric generation.
Version: 1.0.

## Scope and directions

The scoped corridor is DTC 717A, using the dominant FULL stop patterns in the supplied static GTFS:

| Direction | GTFS route | Shape | Origin | Destination | Stops |
|---|---:|---|---|---|---:|
| DOWN | 2170 | shp_3_1407 | Kapashera Border (stop 3253) | Badarpur Border (T) (stop 2640) | 46 |
| UP | 2171 | shp_3_271 | Badarpur Border (T) (stop 2640) | Kapashera Border (stop 3253) | 52 |

An OD pair is the ordered origin/destination pair above. A stretch is every pair of adjacent stops within that direction's pattern. GPS trip IDs label reconstructed runs only and are never joined to GTFS trip IDs.

## Inputs and inclusion rules

Inputs are the reconstructed Parquet files at `$PS1_DATA_DIR/passages/day=YYYY-MM-DD.parquet`. A run is identified by day, route, vehicle, and GPS run label. Reconstruction requires at least two time-ordered geometric anchors within 60 m and an observed anchor span of at most six hours; candidate runs failing those rules are excluded before modelling.

For each run and adjacent stop pair, define:

- query time = reconstructed arrival at the upstream stop;
- scheduled duration = downstream scheduled timestamp minus upstream scheduled timestamp;
- actual duration = downstream reconstructed timestamp minus upstream reconstructed timestamp;
- target stretch error = actual duration minus scheduled duration, in seconds.

Every adjacent stretch with nonnegative scheduled and actual durations is eligible. No outcome-based trimming or winsorisation is allowed. The upstream and downstream `observed` flags are retained. Target values are revealed only for scoring after the downstream arrival; they are never model inputs at query time.

Rung 1 requires a causal previous-run error on the same route, direction, day, and stretch. Candidate previous runs must have both upstream and downstream reconstructed timestamps strictly earlier than the current query time and must not be the current run. The candidate with the latest upstream timestamp is selected, with run key as a deterministic tie-breaker. If none exists, persistence is unavailable.

To make all baseline comparisons paired, the scored query set for a day and direction is exactly the set of eligible stretches for which that causal previous run exists. Rungs 0 and 1h are scored on this same set, not on a larger set. Rung 1h falls back to zero timetable error if its training bin is empty, and the number of such fallbacks is reported. There is no per-rung row filtering.

## Whole-day partitions

- Training: 2026-10-05, 06, 07, 08, 13, 14, and 15.
- Held out: 2026-10-22, 27, and 28.
- Holidays 2026-10-02, 20, and 26 are excluded from normal-day training and are not evaluation days.
- October 7 is training-only despite its truncated feed; the October 8 early-hours source gap remains in its training data.

No random row split, shuffled split, or day may cross fitting and evaluation. Rung 1h uses training days only. Held-out days are never used to fit, calibrate, select hyperparameters, or tune fallbacks.

## Time-ordered replay and rungs

Runs and stretches are replayed in query-time order. At each query, only passages from earlier timestamps and training days are visible. In particular, a previous run is not available until its downstream passage is strictly before the current query. Current-run and future-run target values are invisible to feature construction.

- **Rung 0, timetable:** predicted stretch error is 0 seconds.
- **Rung 1, persistence:** predicted stretch error is the selected previous run's actual stretch error on the same stretch and day.
- **Rung 1h, historical median:** predicted stretch error is the median training target for route/direction, stretch, and the query's Indian Standard Time hour. An empty bin falls back to 0 seconds and is counted.

All rungs use one fit/predict interface and predict stretch error; estimated arrival time is scheduled arrival plus predicted error when needed by downstream consumers.

## Metrics and coverage

For every rung × day × direction group, prediction error is predicted stretch error minus actual stretch error. Report:

- query count;
- MAE (mean absolute prediction error), median AE, and p90 AE in seconds;
- share of queries with absolute error strictly greater than 60 seconds;
- observed query count and coverage: both target stretch endpoints are observed;
- interpolated/partial query count and coverage: at least one target stretch endpoint is interpolated;
- historical-median empty-bin fallback count for rung 1h (zero for other rungs).

Observed and interpolated/partial counts sum to query count, and their coverage percentages sum to 100%. The coverage columns describe target-truth provenance and must accompany every accuracy number. Percentiles use NumPy's linear quantile convention. Metrics are computed over all paired queries in the group, with no averaging of per-stretch scores.

Interval-producing rungs must additionally report empirical p10–p90 coverage and mean/median interval width, calibrated only on training days. Point-only baseline rungs leave interval fields empty. Per-leg quantiles are never added to form an end-to-end interval.

## Outputs and explicit gate

One scoped command regenerates `reports/eval-baselines.csv` and `reports/eval-baselines.md` for both directions and all three held-out days:

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate
```

The gate is evaluated independently on 2026-10-22 for UP and DOWN using the paired rows and identical coverage: **rung 1 MAE must be strictly less than rung 0 MAE in each direction**. The older-feed values and approximately 30% improvement are historical references only, not targets or pass thresholds. Failure in either direction is escalated rather than hidden by pooling directions or days.
