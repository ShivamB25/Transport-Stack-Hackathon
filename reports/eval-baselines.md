# 717A held-out baseline evaluation

Protocol: [docs/eval-protocol.md](../docs/eval-protocol.md). All rungs use the same causal persistence-eligible queries within each stratum and group.

**Ground-truth caveat:** all-stop metrics include schedule-interpolated pseudo-ground-truth derived from the timetable. This is circular and biases comparisons toward rung 0. Only observed-only metrics, whose target stretch endpoints are both directly observed, are used for the persistence gate.

| Day | Direction | Rung | Stratum | Queries | MAE s | Median AE s | p90 AE s | >60 s % | Observed % | Interpolated/partial % | History fallbacks |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-10-22 | DOWN | 0 | all-stop | 6480 | 42.649 | 0.000 | 73.000 | 12.099 | 33.873 | 66.127 | 0 |
| 2026-10-22 | DOWN | 1 | all-stop | 6480 | 50.483 | 10.000 | 92.000 | 16.034 | 33.873 | 66.127 | 0 |
| 2026-10-22 | DOWN | 1h | all-stop | 6480 | 45.198 | 10.000 | 76.500 | 13.102 | 33.873 | 66.127 | 0 |
| 2026-10-22 | UP | 0 | all-stop | 6630 | 40.911 | 0.000 | 74.100 | 11.931 | 36.199 | 63.801 | 0 |
| 2026-10-22 | UP | 1 | all-stop | 6630 | 48.270 | 12.000 | 98.000 | 15.671 | 36.199 | 63.801 | 0 |
| 2026-10-22 | UP | 1h | all-stop | 6630 | 41.339 | 10.000 | 78.200 | 13.017 | 36.199 | 63.801 | 0 |
| 2026-10-27 | DOWN | 0 | all-stop | 5553 | 39.400 | 10.000 | 87.000 | 15.631 | 42.770 | 57.230 | 0 |
| 2026-10-27 | DOWN | 1 | all-stop | 5553 | 45.346 | 17.000 | 101.000 | 18.693 | 42.770 | 57.230 | 0 |
| 2026-10-27 | DOWN | 1h | all-stop | 5553 | 37.537 | 12.000 | 78.400 | 13.668 | 42.770 | 57.230 | 0 |
| 2026-10-27 | UP | 0 | all-stop | 6375 | 34.123 | 7.000 | 69.000 | 11.733 | 37.192 | 62.808 | 0 |
| 2026-10-27 | UP | 1 | all-stop | 6375 | 39.995 | 14.000 | 83.000 | 14.839 | 37.192 | 62.808 | 0 |
| 2026-10-27 | UP | 1h | all-stop | 6375 | 33.577 | 11.000 | 72.000 | 12.173 | 37.192 | 62.808 | 0 |
| 2026-10-28 | DOWN | 0 | all-stop | 5985 | 31.121 | 0.000 | 71.000 | 12.598 | 34.236 | 65.764 | 0 |
| 2026-10-28 | DOWN | 1 | all-stop | 5985 | 39.388 | 12.000 | 88.000 | 16.725 | 34.236 | 65.764 | 0 |
| 2026-10-28 | DOWN | 1h | all-stop | 5985 | 31.310 | 9.000 | 67.500 | 11.846 | 34.236 | 65.764 | 0 |
| 2026-10-28 | UP | 0 | all-stop | 7242 | 38.086 | 0.000 | 70.000 | 11.627 | 26.595 | 73.405 | 0 |
| 2026-10-28 | UP | 1 | all-stop | 7242 | 46.849 | 11.000 | 98.000 | 15.617 | 26.595 | 73.405 | 0 |
| 2026-10-28 | UP | 1h | all-stop | 7242 | 41.054 | 8.500 | 82.000 | 13.201 | 26.595 | 73.405 | 0 |
| 2026-10-22 | DOWN | 0 | observed-only | 2195 | 101.726 | 34.000 | 187.200 | 29.248 | 100.000 | 0.000 | 0 |
| 2026-10-22 | DOWN | 1 | observed-only | 2195 | 93.364 | 29.000 | 170.200 | 27.380 | 100.000 | 0.000 | 0 |
| 2026-10-22 | DOWN | 1h | observed-only | 2195 | 92.013 | 24.000 | 167.600 | 24.328 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 0 | observed-only | 2400 | 95.737 | 32.000 | 190.200 | 27.042 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 1 | observed-only | 2400 | 84.795 | 25.000 | 168.100 | 24.917 | 100.000 | 0.000 | 0 |
| 2026-10-22 | UP | 1h | observed-only | 2400 | 80.873 | 22.000 | 155.050 | 21.792 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 0 | observed-only | 2375 | 75.542 | 34.000 | 156.600 | 29.305 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 1 | observed-only | 2375 | 63.651 | 28.000 | 133.000 | 24.968 | 100.000 | 0.000 | 0 |
| 2026-10-27 | DOWN | 1h | observed-only | 2375 | 63.525 | 24.000 | 135.200 | 21.642 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 0 | observed-only | 2371 | 73.799 | 30.000 | 150.000 | 25.053 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 1 | observed-only | 2371 | 63.314 | 23.000 | 131.000 | 22.311 | 100.000 | 0.000 | 0 |
| 2026-10-27 | UP | 1h | observed-only | 2371 | 59.716 | 22.000 | 120.000 | 19.654 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 0 | observed-only | 2049 | 70.615 | 34.000 | 159.400 | 29.527 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 1 | observed-only | 2049 | 62.899 | 30.000 | 134.200 | 25.866 | 100.000 | 0.000 | 0 |
| 2026-10-28 | DOWN | 1h | observed-only | 2049 | 59.280 | 22.000 | 132.600 | 22.401 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 0 | observed-only | 1926 | 108.839 | 33.000 | 224.000 | 31.049 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 1 | observed-only | 1926 | 99.221 | 27.000 | 195.000 | 28.401 | 100.000 | 0.000 | 0 |
| 2026-10-28 | UP | 1h | observed-only | 1926 | 94.714 | 22.000 | 188.500 | 26.272 | 100.000 | 0.000 | 0 |

## October 22 observed-only persistence gate

- UP: **PASS** — rung 1 MAE 84.795 s vs rung 0 MAE 95.737 s; 2400 observed-only paired queries in each rung.
- DOWN: **PASS** — rung 1 MAE 93.364 s vs rung 0 MAE 101.726 s; 2195 observed-only paired queries in each rung.

The historical approximately 30% improvement is a reference only. The gate is the strict direction-by-direction MAE comparison above.
