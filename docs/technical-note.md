# Technical Note — Delhi Real-Time Bus Travel-Time Model and Journey Planner (PS1)

This note documents the modelling, data reconstruction, live/historical blending, evaluation ladder, and abstention contract implemented in `src/ps1/`. Every number below is generated from the committed reports under `reports/` on the October 2026 DTC GPS archive and static GTFS feed v32 (`delhi_buses_static_gtfs_v1.zip`).

---

## 1. GPS-to-Route and Stop Matching (Geometric, No `trip_id` Join)

### Why no `trip_id` join exists

The October 2026 GPS archive and the live GTFS-RT `VehiclePositions.pb` feed emit a `trip_id` field (for example `2170_05_17_5358` or `3097_22_55`), but it does **not** match the static GTFS `trips.txt` identifier format (`{route_id}_{HH}_{MM}`). In addition, the live `VehiclePositions` protobuf omits `bearing`, `stop_id`, and `direction_id`. Any join on `trip_id` fails silently or pairs unrelated departures, and city-wide stop-name fuzzy matching is prohibited because Delhi route stops share ambiguous landmark names across opposite directions and corridors.

In this pipeline (`src/ps1/stop_passages.py`), GPS `trip_id` is used **only as a local run label** to group consecutive pings belonging to one vehicle run (`day, route_id, vehicle_id, run_id`). Route direction is identified from the integer `route_id`, which maps directly to direction-suffixed GTFS route short names (for example `717AUP -> route_id 2171` and `717ADOWN -> route_id 2170`).

### Raw GPS cleaning and archive structure

Raw October 2026 SQLite days are range-extracted one day at a time (`src/ps1/extract_day.py`, `User-Agent: curl/8.5.0`) and cleaned in DuckDB before writing daily Parquet files (`$PS1_DATA_DIR/gps/day=YYYY-MM-DD.parquet`):

1. Parse `ts = TRY_CAST(timestamp AS BIGINT)` (Unix epoch seconds UTC); drop unparseable rows.
2. Filter null coordinates and coordinates outside the Delhi bounding box (`27.5–29.0 N`, `76.7–77.6 E`).
3. Deduplicate on `(vehicle_id, ts, lat, lng)`, keeping one row per group.
4. Cast `route_id` from `REAL` to `INTEGER` (`NULL` if unparseable; retained for spatial accounting, excluded from route-level modelling).

Across all 31 October days (`reports/data-quality.md`), cleaning reduces **912,877,061 raw rows** to **560,357,771 clean rows** (38.62% removed overall; 33.74%–43.65% per day, all inside the 30–45% tolerance). On an instrumented run of `2026-10-15`, 37.490% of raw rows were exact `(vehicle_id, ts, lat, lng)` re-emissions and 0.084% were out-of-bbox/invalid timestamps. Every clean daily Parquet file has **0 duplicate groups**, **0 bbox violations**, and **0 fractional `route_id` values**. Two source anomalies are preserved and documented rather than hidden: `2026-10-07` ends at `19:53:23 IST` (training-only, never an evaluation day) and `2026-10-08` starts at `02:02:10 IST` (genuine overnight source gap).

### Geometric stop-passage reconstruction and coverage

For each scoped corridor direction, `stop_passages.py` loads the dominant FULL static GTFS stop pattern:

| Direction | GTFS `route_id` | Shape ID | Origin | Destination | Pattern stops |
|---|---:|---|---|---|---:|
| `DOWN` | `2170` | `shp_3_1407` | Kapashera Border (`3253`) | Badarpur Border (T) (`2640`) | 46 |
| `UP` | `2171` | `shp_3_271` | Badarpur Border (T) (`2640`) | Kapashera Border (`3253`) | 52 |

For each `(vehicle_id, run_id)` ping sequence sorted by timestamp:

1. **Minimum-distance matching (`DEFAULT_RADIUS_M = 60.0` m):** compute great-circle Haversine distances from all pings in the run to every pattern stop. For each stop that has at least one ping within 60 m, select the ping with the minimum distance (earliest ping breaks exact ties).
2. **Monotonic time-ordered anchor chain:** run dynamic programming (`drop_time_order_breaking_anchors`) to keep the maximum-cardinality subsequence of anchors that is strictly increasing in `stop_index` and nondecreasing in timestamp `ts`, breaking length ties by minimum total match distance. This removes GPS loops, layover spillovers, and stray reverse matches.
3. **Run quality filter:** discard candidate runs with fewer than 2 surviving anchors or an observed anchor span exceeding 6 hours (`MAX_OBSERVED_SPAN_S = 21600`).
4. **Gap interpolation and endpoint extrapolation:** for stops between consecutive anchors `(left, right)`, interpolate timestamps proportionally to scheduled travel time (`gap_pace = (right.ts - left.ts) / (right.schedule_s - left.schedule_s)`). Stops before the first anchor or after the last anchor are extrapolated at the scheduled rate (`1.0 s/s`). Every emitted stop row carries an explicit `source` (`observed` or `interpolated`), boolean `observed`, reconstructed `ts`, `scheduled_ts`, `schedule_error_s = ts - scheduled_ts`, and `distance_m` (`NULL` for interpolated rows).

Recomputed coverage on GTFS feed v32 (`reports/coverage-717a.md`, across the 10 scoped training + held-out days: 1,267 UP trips, 1,281 DOWN trips):

| Slice | Direction | Trips | Pattern Stops | Observed | Interpolated | Observed Coverage |
|---|---|---:|---:|---:|---:|---:|
| All 10 scoped days | `DOWN` (`2170`) | 1,281 | 58,926 | 29,940 | 28,986 | **50.81%** |
| All 10 scoped days | `UP` (`2171`) | 1,267 | 65,884 | 31,932 | 33,952 | **48.47%** |
| Held-out `2026-10-22` | `DOWN` / `UP` | 145 / 131 | 6,670 / 6,812 | 2,617 / 2,888 | 4,053 / 3,924 | **39.24%** / **42.40%** |
| Held-out `2026-10-27` | `DOWN` / `UP` | 125 / 126 | 5,750 / 6,552 | 2,931 / 3,016 | 2,819 / 3,536 | **50.97%** / **46.03%** |
| Held-out `2026-10-28` | `DOWN` / `UP` | 134 / 143 | 6,164 / 7,436 | 2,544 / 2,548 | 3,620 / 4,888 | **41.27%** / **34.27%** |

These recomputed figures are broadly consistent with the historical older-feed reference (~46% UP, 42–48% DOWN) while reflecting day-to-day variation on feed v32. Zero monotonicity violations and exact `observed + interpolated == stops` reconciliation hold across all reconstructed trips.

---

## 2. Stretch Travel-Time Estimation

### Adjacent-stop stretch target and strictly causal replay

Rather than predicting full-trip durations from noisy origin-to-destination endpoints, the model predicts **per-stretch schedule error** across every adjacent stop pair `(from_stop, to_stop)` in a route pattern (`src/ps1/features.py`):

- `query_ts` = reconstructed arrival timestamp at the upstream stop;
- `scheduled_duration_s` = `to.scheduled_ts - from.scheduled_ts`;
- `actual_duration_s` = `to.ts - from.ts`;
- `error_s` = `actual_duration_s - scheduled_duration_s` (prediction target, in seconds).

Every query is constructed in a **strictly time-ordered replay**: at `query_ts`, only passages whose timestamps are strictly earlier than `query_ts` (and training days strictly prior to the evaluated day) are visible. In particular, a previous bus's stretch error is eligible only once **both** its upstream and downstream passages have completed before `query_ts`. `features.py` raises `CausalityViolationError` if any future timestamp enters feature construction, verified by automated future-data rejection tests in `tests/model/`.

### Whole-day partitions (zero random row splits)

Every training, calibration, and evaluation partition is a disjoint set of whole days (`docs/eval-protocol.md`, `reports/eval-full.md`):

- **Training days (rungs 1h, 2, 3):** `2026-10-05`, `06`, `07` (training-only), `08`, `13`, `14`, `15`.
- **Rung 4 LightGBM fit days:** `2026-10-05`, `06`, `07`, `08`, `13`, `14` (32,526 observed queries).
- **Rung 4 split-conformal calibration day:** `2026-10-15` (4,694 observed queries; strictly later than fit days, never touches held-out days).
- **Held-out evaluation days:** `2026-10-22`, `2026-10-27`, `2026-10-28` (13,316 observed-only paired queries across 6 day × direction groups).
- **Excluded holidays:** `2026-10-02`, `2026-10-20`, `2026-10-26`.

### Model ladder (Rungs 0–4) and conformal calibration

All rungs share one fit/predict interface (`src/ps1/model.py`) and are scored on identical paired queries within each stratum, day, and direction:

- **Rung 0 (Timetable):** predicts `error_s = 0`.
- **Rung 1 (Persistence):** predicts the latest causal same-day previous run's `error_s` on the exact same stretch.
- **Rung 1h (Historical hourly median):** median training `error_s` by `(route, direction, from_stop, to_stop, IST hour)`, falling back to `0` on empty bins (fallback count reported; 0 on all held-out groups).
- **Rung 2 (+ Day-type):** hierarchical observed-target median table (`HistoricalMedianTable`) backing off cleanly across `(stretch, day_of_week, hour) -> (stretch, coarse_day_type, hour) -> (stretch, hour) -> stretch -> (route, hour) -> 0` (requiring `min_day_type_samples = 3`).
- **Rung 3 (+ This bus's last delay):** Rung 2 baseline plus a `HuberRegressor` adjustment fitted on leave-one-day-out (LODO) residuals using this vehicle's own causal prior stretch error, last stop delay, and recent pace delta.
- **Rung 4 (Quantile LightGBM + split-conformal CQR):** three gradient-boosted quantile models (`alpha = 0.10, 0.50, 0.90`, `n_estimators = 220`, `num_leaves = 31`, `learning_rate = 0.05`) trained on 28 strictly causal features: 24 base numeric features (remaining stops/distance, stretch distance, scheduled duration, ping age, upstream observed flag, recent vehicle progress ratio and speed, last stop delay, last stretch error, previous run error/age/observed flag, recent 30-minute same-direction speed and pace ratio, headway, hour/minute/cyclical time-of-day, day of week, weekend indicator, route ID, stop sequence) plus 4 LODO hierarchical historical median features (`hist_dow_hour_median_s`, `hist_hour_median_s`, `hist_stretch_median_s`, `prev_minus_hist_s`).
  - Raw quantile outputs are sorted to guarantee `p10 <= p50 <= p90`, then expanded symmetrically by the split-conformal CQR offset `q_conf = 2.366 s` computed on `2026-10-15` for nominal 80% coverage (`alpha = 0.20`). On the calibration day, conformalization raises coverage from **76.118% raw** to **80.038% calibrated** (mean width 143.577 s, median width 80.892 s). Across all 38,265 held-out predictions saved at `$PS1_DATA_DIR/model/eval-predictions-rung4.parquet`, there are **0 quantile-ordering violations**.

### Door-to-door journey re-timing and joint scenarios

OpenTripPlanner 2.8.1 (unmodified core, GraphQL `planConnection` on port 3102) generates candidate bus itineraries. Our API (`src/ps1/retime.py`) owns **all** transit timing:

1. Walk legs keep OTP street-network durations (`timing_source = "otp_walk"`).
2. Each transit ride leg is re-timed by `RetimeModel.predict_ride()`. Within a multi-stretch covered ride leg, calibrated per-stretch half-widths are combined with intra-leg correlation `rho = 0.35` (`_correlated_leg_interval`) rather than naively summed.
3. First boarding waits are estimated from causal stop-event headways and recent delays (or live fleet headway `clip(3600 / fresh_vehicles, 240, 1200)` in live mode when no recent stop passages exist).
4. Door-to-door duration and `[total_window_p10_s, total_window_p90_s]` are constructed from **192 correlated joint replay scenarios** (`SCENARIOS = 192`, `scenario_windows()`). Each scenario draws a shared corridor congestion shock (`0.8 * shared + 0.6 * idiosyncratic`) across legs and evaluates every transfer sequentially: if the first bus arrives after the connecting bus's scheduled departure, the scenario registers a missed connection and adds a next-bus headway wait (`headway - ((elapsed - scheduled_departure) % headway)`). Per-leg `p90` quantiles are **never summed** into an end-to-end `p90`, and every transfer leg reports `connection_missed_probability` and `next_bus_headway_s`.

---

## 3. Blending Live and Historical Observations

`RetimeModel` (`src/ps1/retime.py`) loads `reports/selection.json` (`selected_rung = "4"`) and the native LightGBM + JSON artifact from `$PS1_DATA_DIR/model/` and applies a tiered causal blending strategy that never hides coverage gaps:

### Live mode (`mode = "live"`)

The background poller (`src/ps1/live_poller.py`) fetches `VehiclePositions.pb` from `dts-backend.transportstack.in` every 25 seconds (matching the feed's 20–30 s cadence) and writes per-route aggregates to `$PS1_DATA_DIR/live/route_stats.json` (`vehicle_count`, `fresh_vehicle_count`, `median_speed_mps`, `observed_coverage`). Freshness is measured from each entity timestamp at poll time, not from the protobuf header. The effective entity limit is `min(PS1_LIVE_ENTITY_MAX_AGE_S, PS1_LIVE_STALE_THRESHOLD_S)` (600 s with stock settings), so retained 11–15-minute-old positions cannot drive live timing. Transport failures are persisted and exposed only as fixed error categories; raw exception text is never written to poller state, `poller.log`, or `/live/status`.

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

Replay mode reads strictly causal October observations (`to_ts <= depart_ts` and `available_at < depart_ts`) from `$PS1_DATA_DIR/passages/` and **never contacts the live feed** (`feed_summary.live_feed_contacted = false`). On covered routes (`2170`/`2171`), same-day causal passages populate `previous_error_s`, `recent_same_direction_speed_mps`, `headway_s`, `last_stop_delay_s`, and `last_stretch_error_s` for Rung 4 (`source = "replay"`, `timing_source = "model_replay"`). Without same-day observations, a covered route uses its historical model inputs and is labelled `historical_fallback`; uncovered routes fall back through same-day persistence -> historical ratio -> timetable. Response metadata names only passage files actually loaded. If no reconstructed observation exists before the request cutoff, replay returns `replay_data_unavailable` with null observation timestamps and age rather than presenting the request time as fresh data.

---

## 4. Why Persistence Is the Bar (and Model-Selection Results)

### Why the timetable is too weak — and why `all-stop` targets are circular

Beating the static GTFS timetable (Rung 0) is necessary, not sufficient: static timetables ignore day-to-day traffic regimes, incidents, and corridor slowdowns. Even more importantly, evaluating on `all-stop` reconstructed passages introduces **circular ground truth**: roughly 57%–73% of stretches on held-out days have at least one schedule-interpolated endpoint whose timestamp was computed from the timetable itself. As seen in `reports/eval-full.md`, Rung 0 gets a **0.000 s median absolute error** on four of the six `all-stop` groups (e.g., Oct 22 DOWN MAE 42.649 s for Rung 0 vs 50.483 s for Rung 1) purely because the timetable is being scored against its own interpolations.

Amendment 1.1 of `docs/eval-protocol.md` therefore reports both strata for transparency while restricting the baseline gate and model-selection rule **exclusively to the `observed-only` stratum**, where both the upstream and downstream stop passages of the target stretch are directly observed within 60 m.

### Why previous-run persistence (Rung 1) is the operational bar

Previous-run persistence (Rung 1) requires zero offline training, adapts immediately to live incidents and congestion waves, and already beats the static timetable on every single held-out day and direction on real observed ground truth:

- **October 22 strict persistence gate (`observed-only`, paired queries, `reports/eval-baselines.md`):**
  - `717A UP` (`2171`, 2,400 queries): Rung 1 MAE **84.795 s** vs Rung 0 **95.737 s** (**PASS**, `-10.942 s`, `-11.4%`).
  - `717A DOWN` (`2170`, 2,195 queries): Rung 1 MAE **93.364 s** vs Rung 0 **101.726 s** (**PASS**, `-8.362 s`, `-8.2%`).
- Across all three held-out days (Oct 22, 27, 28; 13,316 observed-only queries), Rung 1 reduces MAE from **87.245 s** (Rung 0) to **77.329 s** (`-9.917 s`, `-11.4%`).

*(Note on feed v32 vs historical reference: the older-feed historical reference numbers of 41 s vs 59 s MAE were measured on an earlier GTFS snapshot; per our protocol, absolute differences on feed v32 are reported transparently rather than tuned away, and the strict direction-by-direction MAE comparison on observed-only ground truth is the binding gate.)*

Any higher-complexity model only earns its operational maintenance cost if it beats Rung 1 on held-out days at **identical per-group observed-vs-interpolated coverage**.

### Full held-out model-selection comparison (`reports/selection.json`, `reports/eval-full.md`)

**Declared selection rule:** *Ship the highest rung in ladder order (`0 < 1 < 1h < 2 < 3 < 4`) whose held-out `observed-only` query-weighted MAE is strictly lower than Rung 1 (persistence) at identical per-group observed-vs-interpolated coverage; otherwise ship Rung 1.*

| Rung | Model | Observed Queries | Overall MAE (s) | Delta vs Rung 1 (s) | Coverage Matches Rung 1 | Groups Beating Rung 1 | p10–p90 Coverage | Mean Width (s) | Median Width (s) |
|---:|---|---:|---:|---:|---|---:|---:|---:|---:|
| `0` | Timetable | 13,316 | 87.245 | +9.917 | yes | 0/6 | — | — | — |
| `1` | Persistence (bar) | 13,316 | 77.329 | 0.000 | yes | 0/6 | — | — | — |
| `1h` | Stretch-hour median | 13,316 | 74.527 | -2.802 | yes | 6/6 | — | — | — |
| `2` | + Day-type (observed targets) | 13,316 | 69.532 | -7.797 | yes | 6/6 | — | — | — |
| `3` | + This bus's causal last delay | 13,316 | 69.486 | -7.843 | yes | 6/6 | — | — | — |
| **`4`** | **Quantile LightGBM + CQR (shipped)** | **13,316** | **61.736** | **-15.593** | **yes** | **6/6** | **79.010%** | **143.998** | **81.434** |

Per-day, per-direction `observed-only` MAE (seconds) and Rung 4 interval performance:

| Day | Direction | Queries | Rung 0 | Rung 1 | Rung 1h | Rung 2 | Rung 3 | Rung 4 | Rung 4 Cov % | Rung 4 Mean / Med Width (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `2026-10-22` | `DOWN` (`2170`) | 2,195 | 101.726 | 93.364 | 92.013 | 83.127 | 82.893 | **77.657** | 76.492% | 141.536 / 85.396 |
| `2026-10-22` | `UP` (`2171`) | 2,400 | 95.737 | 84.795 | 80.873 | 79.574 | 79.531 | **67.689** | 79.500% | 156.013 / 79.487 |
| `2026-10-27` | `DOWN` (`2170`) | 2,375 | 75.542 | 63.651 | 63.525 | 57.516 | 57.460 | **50.817** | 78.821% | 130.751 / 81.317 |
| `2026-10-27` | `UP` (`2171`) | 2,371 | 73.799 | 63.314 | 59.716 | 55.843 | 55.899 | **48.297** | 81.991% | 137.712 / 75.378 |
| `2026-10-28` | `DOWN` (`2170`) | 2,049 | 70.615 | 62.899 | 59.280 | 54.123 | 54.122 | **47.476** | 79.161% | 137.563 / 83.277 |
| `2026-10-28` | `UP` (`2171`) | 1,926 | 108.839 | 99.221 | 94.714 | 89.584 | 89.590 | **81.354** | 77.674% | 162.752 / 84.986 |

**Ship decision (`reports/selection.json`):** **`selected_rung = "4"`**. Rung 4 beats persistence by **15.593 s** overall (**-20.2%** relative to Rung 1, and **-29.2%** relative to the Rung 0 timetable), wins on **6/6** held-out day × direction groups at identical coverage, and achieves **79.010%** held-out empirical coverage on its nominal 80% conformal `[p10, p90]` intervals.

---

## 5. Coverage Provenance and Abstention Behavior (Consistent with the API)

Every `GET /journeys` response (`src/ps1/api.py`) exposes both response-level mode/freshness metadata and per-leg provenance so downstream clients and the Leaflet UI can never mistake a fallback or stale feed for fresh live data.

### Response-level provenance

- `status`: `"ok"` or `"insufficient_data"`.
- `mode`: `"live"` or `"replay"` (auto-selected as `"replay"` for October 2026 departures and `"live"` otherwise; explicitly overridable via `mode=live|replay` or `PS1_MODE`).
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

When fresh data is required and unavailable, the API **suppresses all options (`options: []`)** and returns an explicit `abstention` object with `label: "not enough data"`. The UI (`src/ps1/static/index.html`) clears `#options-list` and map polylines and renders the prominent **Not enough data** panel with the exact reason, feed age, and freshness threshold.

| Abstention `code` | Trigger Condition | Default Threshold / Config |
|---|---|---|
| `feed_unavailable` | No `VehiclePositions` snapshot has been ingested in `live` mode | `$PS1_DATA_DIR/live/feed_state.json` missing |
| `stale_feed` | `live` mode feed age exceeds freshness threshold | `PS1_LIVE_STALE_THRESHOLD_S=600` s (10 min default; overridable, e.g. `45` s for testing/demo) |
| `insufficient_coverage` | `fresh_vehicle_count < min_vehicles` in `live` mode | `PS1_LIVE_MIN_VEHICLES=10` |
| `insufficient_route_coverage` | No candidate has any route-level support, or the best candidate's average ride `observed_coverage` is below an optional additional floor | No-supported-route check always active; optional floor via `min_coverage` or `PS1_MIN_ROUTE_COVERAGE` (default `0.0`) |
| `replay_data_unavailable` | No loaded reconstructed observation is visible before the replay cutoff | Null `feed_timestamp`, `reference_timestamp`, and `feed_age_seconds`; zero visible observations |

Control endpoints `GET /live/status`, `POST /live/pause`, and `POST /live/resume` allow controlled stale-feed testing and immediate recovery polling without touching external services.

### Role of the Google Routes API comparator

`reports/google-comparator.md` compares our model against Google Routes API (`TRANSIT` bus-only, English) across 30 Delhi OD pairs × 3 weekday time slots (90/90 slots compared on upcoming Tuesday `2026-11-24`, `92 / 300` mission-wide calls used). Google is treated strictly as a **live/future comparator, never as ground truth**: Google does not support historical October 2026 backtesting and returns nearly flat bus durations across morning peak, midday, and evening peak (47.8 / 47.9 / 48.0 min), whereas our GPS-trained model captures evening-peak congestion (45.8 / 48.7 / 55.4 min; mean difference +123 s / +2.0 min overall, with **96.7%** of Google bus durations falling inside our joint `[p10, p90]` arrival window).
