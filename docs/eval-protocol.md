# Evaluation protocol — 717A baselines

Status: version 1.0 frozen on 2026-11-23 before baseline fitting, prediction, or metric generation.
Version: 1.1, amended on 2026-11-23 after the all-stop gate failure and before stratified results.
Version: 1.2, amended on 2026-11-23 before causal re-evaluation on rebuilt passages (enforcing source-GPS `available_at` causality, shippable rung output domain `{"1", "2", "3", "4"}`, artifact as-of cutoff metadata, and explicit interval-width aggregation labels).
Version: 1.3, amended on 2026-11-23 before regenerating results: `available_at` also waits for run qualification and raw-GPS prefix stability, and only `observed-only` decisions may be written to `selection.json`.

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

Rung 1 requires a causal previous-run error on the same route, direction, day, and stretch. Candidate previous runs must have both upstream and downstream reconstructed timestamps and both endpoint source-availability timestamps strictly earlier than the current query time, and must not be the current run. The candidate with the latest upstream timestamp is selected, with run key as a deterministic tie-breaker. If none exists, persistence is unavailable.

To make all baseline comparisons paired, the scored query set for a day and direction is exactly the set of eligible stretches for which that causal previous run exists. Rungs 0 and 1h are scored on this same set, not on a larger set. Rung 1h falls back to zero timetable error if its training bin is empty, and the number of such fallbacks is reported. There is no per-rung row filtering.

## Whole-day partitions

- Training: 2026-10-05, 06, 07, 08, 13, 14, and 15.
- Held out: 2026-10-22, 27, and 28.
- Holidays 2026-10-02, 20, and 26 are excluded from normal-day training and are not evaluation days.
- October 7 is training-only despite its truncated feed; the October 8 early-hours source gap remains in its training data.

No random row split, shuffled split, or day may cross fitting and evaluation. Rung 1h uses training days only. Held-out days are never used to fit, calibrate, select hyperparameters, or tune fallbacks.

## Time-ordered replay and rungs

Runs and stretches are replayed in query-time order. At each query, only passages whose event and source-availability timestamps are both strictly earlier than the query, plus frozen training days, are visible. In particular, a previous run is not available until its downstream passage and every source anchor for that stretch are strictly before the current query. Current-run and future-run target values are invisible to feature construction.

- **Rung 0, timetable:** predicted stretch error is 0 seconds.
- **Rung 1, persistence:** predicted stretch error is the selected previous run's actual stretch error on the same stretch and day.
- **Rung 1h, historical median:** predicted stretch error is the median training target for route/direction, stretch, and the query's Indian Standard Time hour. An empty bin falls back to 0 seconds and is counted.

All rungs use one fit/predict interface and predict stretch error; estimated arrival time is scheduled arrival plus predicted error when needed by downstream consumers.

## Metrics and coverage

For every stratum × rung × day × direction group, prediction error is predicted stretch error minus actual stretch error. Report:

- query count;
- MAE (mean absolute prediction error), median AE, and p90 AE in seconds;
- share of queries with absolute error strictly greater than 60 seconds;
- observed query count and coverage: both target stretch endpoints are observed;
- interpolated/partial query count and coverage: at least one target stretch endpoint is interpolated;
- historical-median empty-bin fallback count for rung 1h (zero for other rungs).

Observed and interpolated/partial counts sum to query count, and their coverage percentages sum to 100%. The coverage columns describe target-truth provenance and must accompany every accuracy number. Percentiles use NumPy's linear quantile convention. Metrics are computed over all paired queries in the group, with no averaging of per-stretch scores.

Interval-producing rungs must additionally report empirical p10–p90 coverage and mean/median interval width, calibrated only on training days. Point-only baseline rungs leave interval fields empty. Per-leg quantiles are never added to form an end-to-end interval.

## Amendment 1.1 — directly observed ground truth

The version 1.0 all-stop results exposed circular ground truth: rung 0 had median AE of 0 in several groups because schedule-interpolated passage timestamps are derived from the timetable. Scoring the timetable against those timestamps is therefore not independent evidence and biases rung comparisons toward rung 0.

Every metric is now reported for two strata:

- **observed-only:** only queries whose target stretch upstream and downstream endpoints are both directly observed, never interpolated;
- **all-stop:** the complete causal paired query set, including interpolated/partial targets.

The all-stop rows remain available with this circularity caveat and are not valid for the persistence gate. No model, fallback, or hyperparameter may be tuned on held-out days in response to these results.

## Amendment 1.2 — source-GPS availability causality, shippable selection domain, and artifact cutoffs

Scrutiny of version 1.1 results identified that interpolated stop passages become knowable only when their later GPS anchor arrives (`available_at`), not at their reconstructed event timestamp (`ts`). Admitting another run's interpolated stretch or stop arrival by `ts < query_ts` alone allowed future GPS anchors (observed 27–57 s after the query on October 22) into persistence, headway, and recent-stretch features even on `observed-only` target queries.

The following clarifications are binding before regenerating results from the rebuilt 10-day passages:

- **Source-GPS availability (`available_at`):** Every stop passage carries `available_at` (observed stop = own ping `ts`; interior interpolated stop = later anchor `ts`; extrapolated endpoint = anchor used; tightened by amendment 1.3). For all other-run inputs (persistence previous run, headway prior arrival, recent same-direction stretches), both event timestamps and `available_at` must be strictly earlier than `query_ts` (`ts < query_ts` and `available_at < query_ts`; for stretches, `max(from_available_at, to_available_at) < query_ts`). For same-run prior passages and stretches, `stop_sequence <= from_sequence`, event timestamps `< query_ts`, and `available_at < query_ts`. Thus an observation exactly at the query timestamp is not used as an already-available feature.
- **Shippable selection domain (`reports/selection.json` and API schema):** Evaluation reports score all six ladder rungs (`0`, `1`, `1h`, `2`, `3`, `4`) for full comparability. Rung `1h` (historical hourly median) is retained in all tables and `selection.json["candidates"]` as a **non-shippable historical baseline**. Shippable rungs are explicitly `{"1", "2", "3", "4"}`: the selection rule ships the highest rung in `("4", "3", "2")` whose held-out `observed-only` query-weighted MAE is strictly lower than rung `1` (persistence) at identical per-group observed-vs-interpolated coverage; otherwise rung `1` ships (including when only `1h` beats rung `1` and none of `2`, `3`, `4` beats rung `1`).
- **Model artifact as-of eligibility:** Persisted model artifacts record their training cutoff day, calibration cutoff day, overall data cutoff day, and earliest causal eligibility timestamp (`eligible_from_day` / `eligible_from_ts`, starting at `00:00:00+05:30` IST on the calendar day after the latest training or calibration day, and strictly after the latest training/calibration observation availability). Replay requests earlier than an artifact's eligibility cutoff must not invoke that artifact.
- **Cross-group interval width summary:** Per-day/direction rows report the exact group `median_interval_width_s`. In cross-group summaries derived solely from `eval-full.csv`, the query-weighted average of group medians is explicitly labelled `weighted_mean_group_median_width_s`; the exact pooled held-out `observed-only` median interval width is additionally computed from saved per-query predictions and reported in `selection.json` and `eval-full.md`.

## Amendment 1.3 — run qualification and raw-GPS prefix availability

Round-2 scrutiny found that the stored per-passage rule still admitted reconstruction decisions that depended on later GPS. A run is published only once it has two surviving anchors, yet its leading extrapolated passages were stamped with the first anchor's time. On 2026-10-22 at query `1792636652`, DOWN run `2170_08_04` had its first anchor at `1792636203` and its second at `1792636760`, so its extrapolated 0 s error replaced the +85 s persistence value from `2170_07_50`. Later anchors can also revise earlier stops through the maximum-cardinality anchor chain.

Binding before regenerating results:

- **As-of availability:** `available_at` is the latest of the amendment 1.2 per-passage rule, the run's second surviving anchor `ts`, and the ping after which every reconstruction from the raw GPS prefix reproduces the passage (same `ts`, `scheduled_ts` and `source`). All amendment 1.2 visibility rules apply unchanged to this stricter timestamp.
- **Shipped stratum:** `reports/selection.json` can only be written from the `observed-only` stratum. Other strata may be printed for exploration but are never shipped, and the planner refuses a selection file from any other stratum.

No model, hyperparameter, fallback, or split changed with this amendment.

## Outputs and explicit gate

One scoped command regenerates `reports/eval-baselines.{csv,md}`, `reports/eval-full.{csv,md}`, `reports/selection.json`, and the persisted model artifact for both directions and all three held-out days:

```bash
PYTHONPATH=src /home/factory-user/ps1-scratch/venv/bin/python -m ps1.evaluate
```

The gate is evaluated independently on 2026-10-22 for UP and DOWN using only the **observed-only** paired rows and identical coverage: **rung 1 MAE must be strictly less than rung 0 MAE in each direction**. The older-feed values and approximately 30% improvement are historical references only, not targets or pass thresholds. Failure in either direction is recorded as a negative result and escalated without held-out tuning, rather than hidden by pooling directions, days, or strata.
