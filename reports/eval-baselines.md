# 717A held-out baseline evaluation

Protocol: [docs/eval-protocol.md](../docs/eval-protocol.md). All rungs use the same causal persistence-eligible queries within each stratum and group. Every source passage has event time and available_at strictly before the query.

**Ground-truth caveat:** all-stop metrics include schedule-interpolated pseudo-ground-truth derived from the timetable. This is circular and biases comparisons toward rung 0. Only observed-only metrics, whose target stretch endpoints are both directly observed, are used for the persistence gate.

| Day | Direction | Rung | Stratum | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interpolated/partial % | History fallbacks |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-22 | DOWN | 0 | all-stop | 6504 | 42.646 | 0.000 | 73.000 | 12.054 | 33.718 | 66.282 | 0 |
| 2026-10-22 | DOWN | 1 | all-stop | 6504 | 57.966 | 15.000 | 109.700 | 19.157 | 33.718 | 66.282 | 0 |
| 2026-10-22 | DOWN | 1h | all-stop | 6504 | 45.319 | 10.000 | 76.500 | 13.130 | 33.718 | 66.282 | 0 |
| 2026-10-22 | UP | 0 | all-stop | 6630 | 40.825 | 0.500 | 74.000 | 11.885 | 36.154 | 63.846 | 0 |
| 2026-10-22 | UP | 1 | all-stop | 6630 | 50.129 | 14.500 | 100.100 | 16.471 | 36.154 | 63.846 | 0 |
| 2026-10-22 | UP | 1h | all-stop | 6630 | 41.255 | 10.000 | 78.000 | 12.941 | 36.154 | 63.846 | 0 |
| 2026-10-27 | DOWN | 0 | all-stop | 5548 | 39.423 | 10.000 | 87.000 | 15.663 | 42.754 | 57.246 | 0 |
| 2026-10-27 | DOWN | 1 | all-stop | 5548 | 51.618 | 22.000 | 118.000 | 21.215 | 42.754 | 57.246 | 0 |
| 2026-10-27 | DOWN | 1h | all-stop | 5548 | 37.562 | 12.000 | 79.150 | 13.753 | 42.754 | 57.246 | 0 |
| 2026-10-27 | UP | 0 | all-stop | 6324 | 34.375 | 7.000 | 70.000 | 11.812 | 37.445 | 62.555 | 0 |
| 2026-10-27 | UP | 1 | all-stop | 6324 | 43.827 | 17.000 | 92.000 | 16.556 | 37.445 | 62.555 | 0 |
| 2026-10-27 | UP | 1h | all-stop | 6324 | 33.812 | 11.000 | 72.000 | 12.287 | 37.445 | 62.555 | 0 |
| 2026-10-28 | DOWN | 0 | all-stop | 5983 | 31.091 | 0.000 | 71.000 | 12.586 | 34.214 | 65.786 | 0 |
| 2026-10-28 | DOWN | 1 | all-stop | 5983 | 46.296 | 19.000 | 110.000 | 19.923 | 34.214 | 65.786 | 0 |
| 2026-10-28 | DOWN | 1h | all-stop | 5983 | 31.340 | 9.000 | 67.900 | 11.884 | 34.214 | 65.786 | 0 |
| 2026-10-28 | UP | 0 | all-stop | 7293 | 37.800 | 0.000 | 69.000 | 11.532 | 26.354 | 73.646 | 0 |
| 2026-10-28 | UP | 1 | all-stop | 7293 | 54.970 | 16.000 | 120.000 | 18.648 | 26.354 | 73.646 | 0 |
| 2026-10-28 | UP | 1h | all-stop | 7293 | 41.015 | 9.000 | 82.000 | 13.136 | 26.354 | 73.646 | 0 |
| 2026-10-22 | DOWN | 0 | observed-only | 2193 | 101.801 | 34.000 | 187.600 | 29.275 | 100.000 | 0.000 | 0 |
| 2026-10-22 | DOWN | 1 | observed-only | 2193 | 87.112 | 27.000 | 158.600 | 26.539 | 100.000 | 0.000 | 0 |
| 2026-10-22 | DOWN | 1h | observed-only | 2193 | 92.108 | 24.000 | 167.800 | 24.350 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 0 | observed-only | 2397 | 95.608 | 32.000 | 190.000 | 26.950 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 1 | observed-only | 2397 | 79.936 | 23.000 | 155.000 | 23.821 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 1h | observed-only | 2397 | 80.703 | 22.000 | 154.700 | 21.610 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 0 | observed-only | 2372 | 75.619 | 34.000 | 156.900 | 29.384 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 1 | observed-only | 2372 | 60.186 | 27.000 | 121.000 | 23.693 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 1h | observed-only | 2372 | 63.577 | 24.000 | 134.000 | 21.838 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 0 | observed-only | 2368 | 73.865 | 30.000 | 150.000 | 25.084 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 1 | observed-only | 2368 | 60.938 | 22.000 | 121.000 | 21.748 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 1h | observed-only | 2368 | 59.691 | 22.000 | 120.000 | 19.679 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 0 | observed-only | 2047 | 70.603 | 34.000 | 159.800 | 29.507 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 1 | observed-only | 2047 | 59.895 | 30.000 | 126.400 | 25.696 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 1h | observed-only | 2047 | 59.278 | 22.000 | 132.000 | 22.423 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 0 | observed-only | 1922 | 108.954 | 33.000 | 224.000 | 31.061 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 1 | observed-only | 1922 | 91.263 | 25.000 | 179.000 | 26.951 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 1h | observed-only | 1922 | 94.787 | 22.000 | 189.700 | 26.223 | 100.000 | 0.000 | 0 |

## October 22 observed-only persistence gate

- UP: **PASS** — rung 1 MAE 79.936 s vs rung 0 MAE 95.608 s; 2397 observed-only paired queries in each rung.
- DOWN: **PASS** — rung 1 MAE 87.112 s vs rung 0 MAE 101.801 s; 2193 observed-only paired queries in each rung.

The historical approximately 30% improvement is a reference only. The gate is the strict direction-by-direction MAE comparison above.
