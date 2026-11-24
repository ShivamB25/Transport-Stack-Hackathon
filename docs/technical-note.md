# Technical note: Delhi bus travel-time model and journey planner

This note describes how `backend/src/delhi_transit/` reconstructs stop passages, estimates travel times, blends live and historical data, selects a model and decides when to abstain. Every number comes from a committed report under `reports/`, computed from the October 2026 DTC GPS archive and static GTFS feed v32 (`delhi_buses_static_gtfs_v1.zip`).

---

## 1. GPS-to-Route and Stop Matching (Geometric, No `trip_id` Join)

### Why no `trip_id` join exists

The October 2026 GPS archive and the live GTFS-RT `VehiclePositions.pb` feed emit a `trip_id` field (for example `2170_05_17_5358` or `3097_22_55`), but it does **not** match the static GTFS `trips.txt` identifier format (`{route_id}_{HH}_{MM}`). In addition, the live `VehiclePositions` protobuf omits `bearing`, `stop_id`, and `direction_id`. Any join on `trip_id` fails silently or pairs unrelated departures, and city-wide stop-name fuzzy matching is prohibited because Delhi route stops share ambiguous landmark names across opposite directions and corridors.

In this pipeline (`delhi_transit/data/stop_passages.py`), GPS `trip_id` is used **only as a local run label** to group consecutive pings belonging to one vehicle run (`day, route_id, vehicle_id, run_id`). Route direction is identified from the integer `route_id`, which maps directly to direction-suffixed GTFS route short names (for example `717AUP -> route_id 2171` and `717ADOWN -> route_id 2170`).

### Raw GPS cleaning and archive structure

Raw October 2026 SQLite days are range-extracted one day at a time (`delhi_transit/data/extract_day.py`, `User-Agent: curl/8.5.0`) and cleaned in DuckDB before writing daily Parquet files (`$DELHI_TRANSIT_DATA_DIR/gps/day=YYYY-MM-DD.parquet`):

1. Parse `ts = TRY_CAST(timestamp AS BIGINT)` (Unix epoch seconds UTC); drop unparseable rows.
2. Filter null coordinates and coordinates outside the Delhi bounding box (`27.5–29.0 N`, `76.7–77.6 E`).
3. Deduplicate on `(vehicle_id, ts, lat, lng)`, keeping the first source row by SQLite `rowid`.
4. Cast `route_id` from `REAL` to `INTEGER` (`NULL` if unparseable; retained for spatial accounting, excluded from route-level modelling).

Across all 31 October days (`reports/data-quality.md`), cleaning reduces **912,877,061 raw rows** to **560,357,771 clean rows** (38.62% removed overall; 33.74%–43.65% per day, all inside the 30–45% tolerance). On an instrumented run of `2026-10-15`, 37.490% of raw rows were exact `(vehicle_id, ts, lat, lng)` re-emissions and 0.084% were out-of-bbox/invalid timestamps. Every clean daily Parquet file has **0 duplicate groups**, **0 bbox violations**, and **0 fractional `route_id` values**. Two source anomalies are preserved and documented rather than hidden: `2026-10-07` ends at `19:53:23 IST` (training-only, never an evaluation day) and `2026-10-08` starts at `02:02:10 IST` (genuine overnight source gap).

### Geometric stop-passage reconstruction and coverage

For each scoped corridor direction, `data/gtfs_patterns.py` loads the dominant FULL static GTFS stop pattern:

| Direction | GTFS `route_id` | Shape ID | Origin | Destination | Pattern stops |
|---|---:|---|---|---|---:|
| `DOWN` | `2170` | `shp_3_1407` | Kapashera Border (`3253`) | Badarpur Border (T) (`2640`) | 46 |
| `UP` | `2171` | `shp_3_271` | Badarpur Border (T) (`2640`) | Kapashera Border (`3253`) | 52 |

For each `(vehicle_id, run_id)` ping sequence sorted by timestamp:

1. **Minimum-distance matching (`DEFAULT_RADIUS_M = 60.0` m):** compute great-circle Haversine distances from all pings in the run to every pattern stop. For each stop that has at least one ping within 60 m, select the ping with the minimum distance (earliest ping breaks exact ties).
2. **Monotonic time-ordered anchor chain:** run dynamic programming (`drop_time_order_breaking_anchors`) to keep the maximum-cardinality subsequence of anchors that is strictly increasing in `stop_index` and nondecreasing in timestamp `ts`, breaking length ties by minimum total match distance. This removes GPS loops, layover spillovers, and stray reverse matches.
3. **Run quality filter:** discard candidate runs with fewer than 2 surviving anchors or an observed anchor span exceeding 6 hours (`MAX_OBSERVED_SPAN_S = 21600`).
4. **Gap interpolation and endpoint extrapolation:** for stops between consecutive anchors `(left, right)`, interpolate timestamps proportionally to scheduled travel time (`gap_pace = (right.ts - left.ts) / (right.schedule_s - left.schedule_s)`). Stops before the first anchor or after the last anchor are extrapolated at the scheduled rate (`1.0 s/s`). Every emitted stop row carries an explicit `source` (`observed` or `interpolated`), boolean `observed`, reconstructed `ts`, source-availability epoch `available_at`, `scheduled_ts`, `schedule_error_s = ts - scheduled_ts`, and `distance_m` (`NULL` for interpolated rows). Each row's `available_at` is the latest of three times: its own anchor rule (observed = own ping, interior interpolated = later anchor, extrapolated endpoint = anchor used); the run's second surviving anchor, because a run cannot qualify before it has two anchors; and the ping after which every reconstruction from the raw GPS prefix reproduces that row, because a later anchor can revise the anchor chain. `backend/tests/data/test_stop_passages.py` covers the October 22 case where a run's leading passages would otherwise have been visible before its second anchor.

Recomputed coverage on GTFS feed v32 (`reports/coverage-717a.md`, across the 10 scoped training + held-out days: 1,264 UP trips, 1,282 DOWN trips):

| Slice | Direction | Trips | Pattern Stops | Observed | Interpolated | Observed Coverage |
|---|---|---:|---:|---:|---:|---:|
| All 10 scoped days | `DOWN` (`2170`) | 1,282 | 58,972 | 29,931 | 29,041 | **50.75%** |
| All 10 scoped days | `UP` (`2171`) | 1,264 | 65,728 | 31,909 | 33,819 | **48.55%** |
| Held-out `2026-10-22` | `DOWN` / `UP` | 146 / 131 | 6,716 / 6,812 | 2,619 / 2,887 | 4,097 / 3,925 | **39.00%** / **42.38%** |
| Held-out `2026-10-27` | `DOWN` / `UP` | 125 / 125 | 5,750 / 6,500 | 2,932 / 3,012 | 2,818 / 3,488 | **50.99%** / **46.34%** |
| Held-out `2026-10-28` | `DOWN` / `UP` | 134 / 144 | 6,164 / 7,488 | 2,542 / 2,546 | 3,622 / 4,942 | **41.24%** / **34.00%** |

These figures are close to the older-feed reference (about 46% UP, 42–48% DOWN) and vary from day to day. In every row of the report, observed plus interpolated equals the pattern stop count.

---

## 2. Stretch Travel-Time Estimation

### Adjacent-stop stretch target and strictly causal replay

Rather than predicting full-trip durations from noisy origin-to-destination endpoints, the model predicts **per-stretch schedule error** across every adjacent stop pair `(from_stop, to_stop)` in a route pattern (`delhi_transit/model/features.py` and `model/causal_core.py`):

- `query_ts` = reconstructed arrival timestamp at the upstream stop;
- `scheduled_duration_s` = `to.scheduled_ts - from.scheduled_ts`;
- `actual_duration_s` = `to.ts - from.ts`;
- `error_s` = `actual_duration_s - scheduled_duration_s` (prediction target, in seconds).

Every query is constructed in a **strictly time-ordered replay**: at `query_ts`, only passages whose event timestamp and `available_at` are strictly earlier than `query_ts` (and training days strictly prior to the evaluated day) are visible. In particular, a previous bus's stretch error is eligible only once **both** its upstream and downstream passages have completed and become available before `query_ts`. `features.py` raises `CausalityViolationError` if any future timestamp or availability epoch enters feature construction, verified by automated future-data rejection tests in `backend/tests/model/`.

### Whole-day partitions (zero random row splits)

Every training, calibration, and evaluation partition is a disjoint set of whole days (`docs/eval-protocol.md`, `reports/eval-full.md`):

- **Training days (rungs 1h, 2):** `2026-10-05`, `06`, `07` (training-only), `08`, `13`, `14`, `15`.
- **Rung 4 LightGBM fit days:** `2026-10-05`, `06`, `07`, `08`, `13`, `14` (32,418 observed queries).
- **Rung 4 split-conformal calibration day:** `2026-10-15` (4,693 observed queries; strictly later than fit days, never touches held-out days).
- **Held-out evaluation days:** `2026-10-22`, `2026-10-27`, `2026-10-28` (13,299 observed-only paired queries across 6 day × direction groups).
- **Excluded holidays:** `2026-10-02`, `2026-10-20`, `2026-10-26`.

### Model ladder (Rungs 0, 1, 1h, 2, 4) and conformal calibration

All rungs share one fit/predict interface (`delhi_transit/model/rungs.py`, with rung 4 in `model/lgbm.py`) and are scored on identical paired queries within each stratum, day, and direction:

- **Rung 0 (Timetable):** predicts `error_s = 0`.
- **Rung 1 (Persistence):** predicts the latest causal same-day previous run's `error_s` on the exact same stretch.
- **Rung 1h (Historical hourly median):** median training `error_s` by `(route, direction, from_stop, to_stop, IST hour)`, falling back to `0` on empty bins (fallback count reported; 0 on all held-out groups).
- **Rung 2 (+ Day-type):** hierarchical observed-target median table (`HistoricalMedianTable`) backing off cleanly across `(stretch, day_of_week, hour) -> (stretch, coarse_day_type, hour) -> (stretch, hour) -> stretch -> (route, hour) -> 0` (requiring `min_day_type_samples = 3`).
- **Rung 3 (removed, protocol amendment 1.4):** a Rung 2 baseline plus a `HuberRegressor` adjustment from this bus's own causal last delay. On training days its coefficients moved predictions by about 0.5 s per standard deviation of each feature, and it was worse than Rung 2 in 14/14 leave-one-training-day-out groups, so it was dropped. Its last held-out observed-only MAE before removal was 69.558 s against 69.498 s for Rung 2.
- **Rung 4 (Quantile LightGBM + split-conformal CQR):** three gradient-boosted quantile models (`alpha = 0.10, 0.50, 0.90`, `n_estimators = 220`, `num_leaves = 31`, `learning_rate = 0.05`) trained on 28 strictly causal features: 24 base numeric features (remaining stops/distance, stretch distance, scheduled duration, ping age, upstream observed flag, recent vehicle progress ratio and speed, last stop delay, last stretch error, previous run error/age/observed flag, recent 30-minute same-direction speed and pace ratio, headway, hour/minute/cyclical time-of-day, day of week, weekend indicator, route ID, stop sequence) plus 4 LODO hierarchical historical median features (`hist_dow_hour_median_s`, `hist_hour_median_s`, `hist_stretch_median_s`, `prev_minus_hist_s`).
  - Raw quantile outputs are sorted to guarantee `p10 <= p50 <= p90`, then expanded symmetrically by the split-conformal CQR offset `q_conf = 2.359 s` computed on `2026-10-15` for nominal 80% coverage (`alpha = 0.20`). On the calibration day, conformalization raises coverage from **76.092% raw** to **80.055% calibrated** (mean width 144.406 s, median width 80.249 s).

### Door-to-door journey re-timing and joint scenarios

OpenTripPlanner 2.8.1 (unmodified core, GraphQL `planConnection` on port 3102) generates candidate bus itineraries. Our API (`delhi_transit.planner.itinerary` and `delhi_transit.planner.rides`) owns **all** transit timing:

1. Walk legs keep OTP street-network durations (`timing_source = "otp_walk"`).
2. Each transit ride leg is re-timed by `RetimeModel.predict_ride()`. Within a multi-stretch covered ride leg, calibrated per-stretch half-widths are combined with intra-leg correlation `rho = 0.35` (`_correlated_leg_interval`) rather than naively summed.
3. First boarding waits are estimated from causal stop-event headways and recent delays (or live fleet headway `clip(3600 / fresh_vehicles, 240, 1200)` in live mode when no recent stop passages exist).
4. Door-to-door duration and `[total_window_p10_s, total_window_p90_s]` are constructed from **192 correlated joint replay scenarios** (`SCENARIOS = 192`, `scenario_windows()`). Each scenario draws a shared corridor congestion shock (`0.8 * shared + 0.6 * idiosyncratic`) across legs and evaluates every transfer sequentially. The passenger is ready to board once the previous ride and the transfer walk are done. The connecting bus is expected at its scheduled departure plus its own estimated departure delay (`WaitEstimate.departure_delay_s`). If the passenger is ready by then, the connection is made and they wait for that bus. Otherwise the scenario registers a missed connection and adds the wait for the next bus, `headway - ((ready - expected_departure) % headway)`. Per-leg `p90` quantiles are **never summed** into an end-to-end `p90`, and every transfer leg reports `connection_missed_probability` and `next_bus_headway_s`.

---

## 3. Blending Live and Historical Observations

`RetimeModel` (`delhi_transit.planner.rides`) loads `reports/selection.json` (`selected_rung = "4"`) and the native LightGBM + JSON artifact from `$DELHI_TRANSIT_DATA_DIR/model/` and applies the following causal fallback chain. Each leg reports which step it used.

### Live mode (`mode = "live"`)

The background poller (`delhi_transit/live/poller.py`) fetches `VehiclePositions.pb` from `dts-backend.transportstack.in` every 25 seconds (matching the feed's 20–30 s cadence) and writes per-route aggregates to `$DELHI_TRANSIT_DATA_DIR/live/route_stats.json` (`vehicle_count`, `fresh_vehicle_count`, `median_speed_mps`, `observed_coverage`) together with the timestamp and speed of every fresh entity. Freshness is measured from each entity timestamp, not from the protobuf header. It is checked at poll time and again at request time: the API drops entities that have aged past the limit since the last poll and recomputes the route aggregates and the fleet count from what remains, so a ride falls back to historical or timetable timing, or the request abstains with `insufficient_coverage`, when nothing fresh is left. The effective entity limit is `min(DELHI_TRANSIT_LIVE_ENTITY_MAX_AGE_S, DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S)` (600 s with stock settings), so retained 11–15-minute-old positions cannot drive live timing. Transport failures are persisted and exposed only as fixed error categories; raw exception text is never written to poller state, `poller.log`, or `/live/status`.

- **Covered corridor routes (`2170`, `2171` — 717A, shipped Rung 4 artifact):**
  - The leg is decomposed into its constituent pattern stretches. When `fresh_vehicle_count >= 1` on the route, live observations populate the causal speed/pace/headway features (`recent_same_direction_speed_mps = median_speed_mps`, `recent_same_direction_pace_ratio = clip(5.5 / median_speed_mps, 0.5, 2.5)`, `headway_s = clip(3600 / fresh_vehicle_count, 120, 1800)`), which the Rung 4 LightGBM boosters combine nonlinearly with the fitted `HistoricalMedianTable` (`dow_hour`, `hour`, `stretch` medians) and time-of-day features. Label: `source = "live"`, `timing_source = "model_live"`.
  - When `fresh_vehicle_count == 0` on the route (sparse live coverage), Rung 4 still runs using its time-of-day and historical features (with live fields `NaN`, handled natively by LightGBM). Label: `source = "historical"`, `timing_source = "historical_fallback"`.
- **City-wide routes outside Rung 4 scope (all other Delhi routes):**
  - Historical baseline ratio `base_ratio` is taken from the first non-empty causal observed-only pool in hierarchy order: `stretch_hour -> stretch -> route_hour -> corridor_hour`.
  - When `fresh_vehicle_count >= 1` and `median_speed_mps > 0.5`, live route speed adjusts the historical baseline with a bounded 35% blend:
    $$\text{blended\_factor} = 0.65 + 0.35 \cdot \operatorname{clip}\!\left(\frac{5.5}{\text{median\_speed\_mps}},\, 0.75,\, 1.50\right)$$
    $$\text{duration} = \text{scheduled} \times \text{base\_ratio} \times \text{blended\_factor}$$
    Label: `source = "live"`, `timing_source = "model_live"`, with `coverage_note` explicitly naming both the fresh vehicle count/speed and that the route is outside Rung 4 model scope.
  - When `fresh_vehicle_count == 0` **or the fresh positions have no usable speed**, the ride falls back to `scheduled * base_ratio` (`source = "historical"`, `timing_source = "historical_fallback"`). Live fleet headway may still independently label the boarding wait `model_live`.
  - When neither usable live speed nor historical data exists for that route and hour, the ride falls back to `scheduled` (`source = "timetable"`, `timing_source = "timetable_fallback"`), never `model_live`.

### Replay mode (`mode = "replay"`, October 2026 departures)

Replay mode reads strictly causal October observations (`to_ts <= depart_ts` and `available_at < depart_ts`) from `$DELHI_TRANSIT_DATA_DIR/passages/` and **never contacts the live feed** (`feed_summary.live_feed_contacted = false`). On covered routes (`2170`/`2171`), same-day causal passages populate `previous_error_s`, `recent_same_direction_speed_mps`, `headway_s`, `last_stop_delay_s`, and `last_stretch_error_s` for Rung 4 (`source = "replay"`, `timing_source = "model_replay"`). Response metadata names only passage files actually loaded. If no reconstructed observation exists before the request cutoff, replay returns `replay_data_unavailable` with null observation timestamps and age rather than presenting the request time as fresh data.

Replay ride timing follows one of two branches:

- **Eligible artifact (covered route, request cutoff on or after the artifact's eligibility date, Oct 16):** Rung 4 times the leg. With same-day causal observations on the route the leg is `source = "replay"`, `timing_source = "model_replay"`; without them Rung 4 runs on its time-of-day and historical features and the leg is `source = "historical"`, `timing_source = "historical_fallback"`.
- **Ineligible artifact (cutoff before Oct 16) or route outside Rung 4 scope:** the causal Rung 1 chain applies. Same-day persistence comes first: the latest same-day observed error on the exact stretch, otherwise the median same-day pace on the route over the last two hours (`source = "replay"`, `timing_source = "model_replay"`). Without same-day observations the leg uses the causal historical ratio (`historical_fallback`), and without history it uses the timetable (`timetable_fallback`). An ineligible artifact is named in the `coverage_note`.

**Response mode and per-leg provenance are independent.** `mode = "replay"` identifies the request context; it does not imply that every ride is backed by same-day GPS. The `replay_delay_s` test hook shifts the first ride's time but keeps its provenance label. Boarding-wait provenance is likewise reported separately from ride provenance.

The scripted marginal-transfer demo uses two observed passages at Hamdard Nagar / Sangam Vihar on October 22. The 717AUP run `2171_17_09_490` (vehicle `DL1PD7867`) had scheduled arrival 17:28:47 IST and observed arrival **18:03:17 IST**. The matched OMS(+)BURARI-DHAULAKUAN vehicle `DL51EV4293` (GPS run label `1416_15_08`) had scheduled departure 17:32:42 IST and an observed stop passage at **17:35:34 IST**. The actual outcome is therefore a missed connection by **1,663 s (27m 43s)**. The demo never substitutes the connecting timetable departure for an observed outcome; its scheduled 235 s transfer gap is displayed separately. The outcome is computed only for the OTP itinerary that rides trips `DTC:2171_FULL_17_09` and `DTC:1416_FULL_15_08` through stop 2300, whatever order OTP returns candidates in. If OTP does not return that pair, `GET /replay/demo` responds with `status = "unavailable"` and a message instead of attaching the observation to another itinerary.

---

## 4. Why Persistence Is the Bar (and Model-Selection Results)

### Why the timetable is too weak — and why `all-stop` targets are circular

Beating the static GTFS timetable (Rung 0) is necessary, not sufficient: static timetables ignore day-to-day traffic regimes, incidents, and corridor slowdowns. Even more importantly, evaluating on `all-stop` reconstructed passages introduces **circular ground truth**: roughly 57%–73% of stretches on held-out days have at least one schedule-interpolated endpoint whose timestamp was computed from the timetable itself. As seen in `reports/eval-full.md`, Rung 0 gets a **0.000 s median absolute error** on three of the six `all-stop` groups (e.g., Oct 22 DOWN MAE 42.646 s for Rung 0 vs 57.966 s for Rung 1) purely because the timetable is being scored against its own interpolations.

Amendment 1.1 of `docs/eval-protocol.md` therefore reports both strata for transparency while restricting the baseline gate and model-selection rule **exclusively to the `observed-only` stratum**, where both the upstream and downstream stop passages of the target stretch are directly observed within 60 m.

### Why previous-run persistence (Rung 1) is the operational bar

Previous-run persistence (rung 1) needs no training, reacts to the current day's conditions, and beats the timetable on every held-out day and direction on observed stop pairs:

- **October 22 strict persistence gate (`observed-only`, paired queries, `reports/eval-baselines.md`):**
  - `717A UP` (`2171`, 2,397 queries): Rung 1 MAE **79.936 s** vs Rung 0 **95.608 s** (**PASS**, `-15.672 s`, `-16.4%`).
  - `717A DOWN` (`2170`, 2,193 queries): Rung 1 MAE **87.112 s** vs Rung 0 **101.801 s** (**PASS**, `-14.689 s`, `-14.4%`).
- Across all three held-out days (Oct 22, 27, 28; 13,299 observed-only queries), Rung 1 reduces MAE from **87.273 s** (Rung 0) to **72.766 s** (`-14.506 s`, `-16.6%`).

*(Note on feed v32 vs historical reference: the older-feed historical reference numbers of 41 s vs 59 s MAE were measured on an earlier GTFS snapshot; per our protocol, absolute differences on feed v32 are reported transparently rather than tuned away, and the strict direction-by-direction MAE comparison on observed-only ground truth is the binding gate.)*

A more complex model ships only if it beats rung 1 on held-out days at **identical per-group observed-vs-interpolated coverage**.

### Full held-out model-selection comparison (`reports/selection.json`, `reports/eval-full.md`)

**Declared selection rule:** *Ship the highest shippable rung in `(4, 2)` whose held-out `observed-only` query-weighted MAE is strictly lower than Rung 1 (persistence) at identical per-group observed-vs-interpolated coverage; otherwise ship Rung 1. Rung 1h is report-only and cannot be selected.*

| Rung | Model | Observed Queries | Overall MAE (s) | Delta vs Rung 1 (s) | Coverage Matches Rung 1 | Groups Beating Rung 1 | p10–p90 Coverage | Mean Width (s) | Pooled Median Width (s) |
|---:|---|---:|---:|---:|---|---:|---:|---:|---:|
| `0` | Timetable | 13,299 | 87.273 | +14.506 | yes | 0/6 | — | — | — |
| `1` | Persistence (bar) | 13,299 | 72.766 | 0.000 | yes | 0/6 | — | — | — |
| `1h` | Stretch-hour median (report-only) | 13,299 | 74.525 | +1.759 | yes | 2/6 | — | — | — |
| `2` | + Day-type (observed targets) | 13,299 | 69.498 | -3.269 | yes | 6/6 | — | — | — |
| **`4`** | **Quantile LightGBM + CQR (shipped)** | **13,299** | **60.178** | **-12.588** | **yes** | **6/6** | **79.156%** | **144.496** | **81.518** |

Per-day, per-direction `observed-only` MAE (seconds) and Rung 4 interval performance:

| Day | Direction | Queries | Rung 0 | Rung 1 | Rung 1h | Rung 2 | Rung 4 | Rung 4 Cov % | Rung 4 Mean / Med Width (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `2026-10-22` | `DOWN` (`2170`) | 2,193 | 101.801 | 87.112 | 92.108 | 83.199 | **75.881** | 76.197% | 144.360 / 86.570 |
| `2026-10-22` | `UP` (`2171`) | 2,397 | 95.608 | 79.936 | 80.703 | 79.477 | **65.930** | 80.434% | 156.702 / 79.057 |
| `2026-10-27` | `DOWN` (`2170`) | 2,372 | 75.619 | 60.186 | 63.577 | 57.603 | **49.172** | 79.764% | 131.762 / 81.423 |
| `2026-10-27` | `UP` (`2171`) | 2,368 | 73.865 | 60.938 | 59.691 | 55.859 | **47.914** | 81.208% | 136.326 / 73.107 |
| `2026-10-28` | `DOWN` (`2170`) | 2,047 | 70.603 | 59.895 | 59.278 | 53.989 | **46.648** | 79.238% | 135.248 / 82.424 |
| `2026-10-28` | `UP` (`2171`) | 1,922 | 108.954 | 91.263 | 94.787 | 89.418 | **78.193** | 77.575% | 165.060 / 87.092 |

**Ship decision (`reports/selection.json`):** **`selected_rung = "4"`**. On the held-out `observed-only` stratum (13,299 queries, 100% observed, 0% interpolated; 34.740% of the 38,282 all-stop held-out queries), Rung 4 beats persistence by **12.588 s** overall (**-17.3%** relative to Rung 1, and **-31.0%** relative to the Rung 0 timetable), wins on **6/6** held-out day × direction groups at identical coverage, and achieves **79.156%** held-out empirical coverage on its nominal 80% conformal `[p10, p90]` intervals.

**Marginal, not conditional, coverage.** The single conformal offset `q_conf = 2.359 s` is calibrated on one training day (`2026-10-15`) and applied to every day and direction. Split conformal prediction guarantees only marginal coverage, over queries exchangeable with the calibration set; it gives no per-day or per-direction (conditional) guarantee. The per-group column above shows the consequence: held-out `observed-only` coverage ranges from 76.197% (`2026-10-22` DOWN) to 81.208% (`2026-10-27` UP), and two of six groups (`2026-10-22` DOWN, `2026-10-28` UP) fall below the nominal 80%. The pooled 79.156% is itself slightly below nominal; the marginal guarantee assumes held-out queries are exchangeable with the calibration day, and later days need not be. A group-conditional (Mondrian) calibration, for example one offset per direction, could target per-group validity, but it would have to be calibrated on training days only and is not implemented.

---

## 5. Coverage Provenance and Abstention Behavior (Consistent with the API)

Every `GET /journeys` response (`delhi_transit/api/routes.py`) carries response-level mode and freshness metadata and per-leg provenance, so clients can tell fallbacks and stale data apart from fresh live estimates. When `depart` is omitted, the request plans from the current Asia/Kolkata time in live mode. If replay is explicit (`mode=replay`, or `mode=auto` with `DELHI_TRANSIT_MODE=replay`), it plans from the scripted replay departure, 2026-10-22T08:30+05:30, in replay mode instead.

### Response-level provenance

- `status`: `"ok"` or `"insufficient_data"`.
- `mode`: `"live"` or `"replay"` (auto-selected as `"replay"` for October 2026 departures and `"live"` otherwise; explicitly overridable via `mode=live|replay` or `DELHI_TRANSIT_MODE`).
- `feed_age_seconds`, `feed_timestamp`, `reference_timestamp`, `feed_summary`, `selected_rung` (`"4"`).

### Per-leg provenance (`options[].legs[]`)

Every leg exposes:

| Field | Values / Meaning |
|---|---|
| `source` | `"live"`, `"replay"`, `"historical"`, or `"timetable"` |
| `timing_source` | `"model_live"`, `"model_replay"`, `"historical_fallback"`, `"timetable_fallback"`, or `"otp_walk"` |
| `observed_coverage` | Fraction `[0.0, 1.0]` of observed passages or fresh vehicles backing this leg (`null` on walks) |
| `observation_count` | Number of fresh live vehicles or causal historical/replay observations used |
| `coverage_note` | Human-readable provenance naming the model rung, fallback level, and observation count |

### Explicit abstention contract (`status = "insufficient_data"`)

When fresh data is required and unavailable, the API **suppresses all options (`options: []`)** and returns an explicit `abstention` object with `label: "not enough data"`. The React UI (`frontend/`, `AbstentionPanel`) clears `#options-list` and map polylines and shows the **Not enough data** panel with the reason, feed age and freshness threshold.

| Abstention `code` | Trigger Condition | Default Threshold / Config |
|---|---|---|
| `feed_unavailable` | No `VehiclePositions` snapshot has been ingested in `live` mode | `$DELHI_TRANSIT_DATA_DIR/live/feed_state.json` missing |
| `stale_feed` | `live` mode feed age exceeds freshness threshold | `DELHI_TRANSIT_LIVE_STALE_THRESHOLD_S=600` s (10 min default; overridable, e.g. `45` s for testing/demo) |
| `insufficient_coverage` | `fresh_vehicle_count < min_vehicles` in `live` mode | `DELHI_TRANSIT_LIVE_MIN_VEHICLES=10` |
| `insufficient_route_coverage` | No candidate has any route-level support, or the best candidate's average ride `observed_coverage` is below an optional additional floor. On `ok` responses the options that pass this check are listed first, so `options[0]` is always supported | No-supported-route check always active; optional floor via `min_coverage` or `DELHI_TRANSIT_MIN_ROUTE_COVERAGE` (default `0.0`) |
| `replay_data_unavailable` | No loaded reconstructed observation is visible before the replay cutoff | Null `feed_timestamp`, `reference_timestamp`, and `feed_age_seconds`; zero visible observations |

Control endpoints `GET /live/status`, `POST /live/pause`, and `POST /live/resume` allow controlled stale-feed testing and immediate recovery polling without touching external services.

### Role of the Google Routes API comparator

`reports/google-comparator.md` compares our model against Google Routes API (`TRANSIT` bus-only, English) across 30 Delhi OD pairs × 3 weekday time slots on upcoming Tuesday `2026-11-24`. All 90 planned slots are retained: 19 have numeric comparisons, 49 are explicit `insufficient_data` abstentions under the fixed supported-route rule, and 22 are `our_unsupported_option`: the response was `ok` because some candidate was supported, but the option listed first had no ride with route-level observations. The comparator compares only an option that passes the support gate, and `GET /journeys` lists those options first. The frozen inputs record only the first option, so for these 22 slots the supported alternative is not available and the slot is excluded rather than replaced. The report used only cached Google responses (`92 / 300` mission-wide calls, zero new calls). Google is a comparator for future departures, not ground truth, and it cannot be queried for October 2026. On the compared subset, mean Google durations are 68.8 / 59.2 / 58.1 min across morning peak, midday, and evening peak versus our 72.6 / 68.3 / 72.3 min; the overall mean difference is +509 s / +8.5 min, with **94.7%** of Google bus durations inside our joint `[p10, p90]` arrival window. Reports render deterministically from `reports/google-comparator-inputs.json`, not from changing live planner state.
