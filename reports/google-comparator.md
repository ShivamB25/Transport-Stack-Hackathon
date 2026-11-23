# Google Routes API (Bus-Only) Comparator Report

## Methodological Notice: Comparator Only, Not Ground Truth

- **Google Maps Routes API is a live/future comparator, NOT ground truth.** It reflects Google's own bus schedule and traffic model, not observed GPS vehicle arrivals on Delhi roads. Disagreement between our model and Google does not imply either system is right or wrong; real ground-truth accuracy is measured strictly against observed October 2026 GPS stop passages in `reports/eval-full.md`.
- **October 2026 backtesting against Google is impossible.** Google Routes API `TRANSIT` does not support past departure times (verified during our feasibility probe: querying `2026-10-20T08:30:00+05:30` returns HTTP 200 with `routes: []`). All comparator queries therefore use **upcoming matching weekday/time slots** (`2026-11-24`, Tuesday, matching held-out Tuesday `2026-10-27` and the brief's Tuesday `08:30` example).
- **Definition differences to keep in mind:** Our `total_duration_s` includes walk-to-stop, estimated first boarding wait, model-timed bus ride(s), joint-scenario transfer risk/wait, and egress walk. Google's `routes.duration` measures door-to-door travel from leaving origin to arriving at destination (excluding pre-departure waiting at origin before walking), and OTP vs Google may select different bus lines or transfer points. Both total duration and wait-excluded differences are tracked in `reports/google-comparator.csv`.

## Collection Contract & Call Accounting

- **Target weekday / date (IST):** `2026-11-24` (Tuesday; upcoming, zero October/past dates)
- **Time slots per OD pair:** morning_peak (08:30 IST), midday (14:00 IST), evening_peak (18:30 IST)
- **Corpus size:** 30 OD pairs × 3 time slots = **90 planned slots** (90 compared, 0 missing/error, 0 silently omitted)
- **Google request settings:** `travelMode=TRANSIT`, `transitPreferences.allowedTravelModes=['BUS']`, `languageCode="en"`, `computeAlternativeRoutes=true`, field-masked (`X-Goog-FieldMask`)
- **Strict bus-only post-filtering:** Every compared Google itinerary is verified to contain >=1 `TRANSIT` step with vehicle type `BUS` only (and `WALK` access/egress/transfers). **3** non-bus Google itinerary(ies) (e.g., Delhi Metro / `SUBWAY` or `RAIL`) were filtered out across the corpus.
- **Mission-wide Google call budget:** **92 / 300** (prior probe calls: 2, comparator network calls cached: 90, remaining budget: 208; cache hits on this run: 90)
- **Artifacts:** Report `reports/google-comparator.md`, CSV table `reports/google-comparator.csv`, request manifest `reports/google-manifest.json` (mirrored at `/home/factory-user/ps1-scratch/data/google/manifest.json`), raw field-masked cache directory `/home/factory-user/ps1-scratch/data/google/responses`.

## Aggregate Summary

Difference is defined as **Our Total Duration − Google Bus-Only Duration** (positive = our model estimates longer door-to-door time; negative = shorter).

| Slice | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **All OD × Time Slots** | 90 | 90 | 0 | 49.9 min | 47.9 min | +123 s (+2.0 m) | +126 s | 371 s (6.2 m) | 96.7% |
| Brief Example (Kashmere Gate -> Nehru Place) | 3 | 3 | 0 | 67.7 min | 64.1 min | +216 s (+3.6 m) | -104 s | 538 s (9.0 m) | 100.0% |
| 717A Corridor UP (Route 2171, Rung 4) | 15 | 15 | 0 | 55.4 min | 52.0 min | +199 s (+3.3 m) | +113 s | 306 s (5.1 m) | 100.0% |
| 717A Corridor DOWN (Route 2170, Rung 4) | 15 | 15 | 0 | 60.5 min | 59.6 min | +56 s (+0.9 m) | +149 s | 571 s (9.5 m) | 93.3% |
| City-Wide Corridors (19 OD pairs) | 57 | 57 | 0 | 44.8 min | 42.9 min | +115 s (+1.9 m) | +138 s | 327 s (5.4 m) | 96.5% |

### Breakdown by Time of Day

| Time Slot (IST) | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 08:30 IST (Morning Peak) | 30 | 30 | 0 | 45.8 min | 47.8 min | -121 s (-2.0 m) | -102 s | 310 s (5.2 m) | 96.7% |
| 14:00 IST (Midday) | 30 | 30 | 0 | 48.7 min | 47.9 min | +44 s (+0.7 m) | +66 s | 258 s (4.3 m) | 100.0% |
| 18:30 IST (Evening Peak) | 30 | 30 | 0 | 55.4 min | 48.0 min | +445 s (+7.4 m) | +376 s | 546 s (9.1 m) | 93.3% |

## Missing / Error / Non-Bus-Only Rows

All **90 / 90** planned OD × time slots returned both a model-timed itinerary from our API and at least one strictly bus-only itinerary from Google Routes API (0 missing or error rows).

## Full Per-OD / Per-Time Comparison Table (All Planned Slots)

| OD | Group | Origin -> Destination | Slot | Status | Our Duration | Our [p10, p90] | Google Bus Duration | Diff (Ours − Google) | Our Route(s) | Google Bus Route(s) | Our Timing Source | Note |
|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|
| `od_01` | `brief_example` | Kashmere Gate ISBT -> Nehru Place | 08:30 IST | `compared` | 3467 s (57.8 m) | [12.3, 107.7] m | 3846 s (64.1 m) | -379 s (-9.8%) | 425UP | 425 | `historical_fallback` | — |
| `od_01` | `brief_example` | Kashmere Gate ISBT -> Nehru Place | 14:00 IST | `compared` | 3742 s (62.4 m) | [12.7, 116.9] m | 3846 s (64.1 m) | -104 s (-2.7%) | 425UP | 425 | `historical_fallback` | — |
| `od_01` | `brief_example` | Kashmere Gate ISBT -> Nehru Place | 18:30 IST | `compared` | 4977 s (83.0 m) | [59.5, 142.6] m | 3846 s (64.1 m) | +1131 s (+29.4%) | 429UP + D-044UP | 449 | `historical_fallback` | — |
| `od_02` | `corridor_717a_up` | Badarpur Border -> Kapashera Border | 08:30 IST | `compared` | 6733 s (112.2 m) | [61.1, 167.4] m | 6315 s (105.2 m) | +418 s (+6.6%) | 717AUP | 717A | `historical_fallback` | — |
| `od_02` | `corridor_717a_up` | Badarpur Border -> Kapashera Border | 14:00 IST | `compared` | 6240 s (104.0 m) | [51.9, 160.2] m | 6315 s (105.2 m) | -75 s (-1.2%) | 717AUP | 717A | `historical_fallback` | — |
| `od_02` | `corridor_717a_up` | Badarpur Border -> Kapashera Border | 18:30 IST | `compared` | 7481 s (124.7 m) | [66.5, 187.6] m | 6315 s (105.2 m) | +1166 s (+18.5%) | 717AUP | 717A | `historical_fallback` | — |
| `od_03` | `corridor_717a_up` | Badarpur Border -> Lado Sarai Crossing | 08:30 IST | `compared` | 2376 s (39.6 m) | [3.8, 77.2] m | 2629 s (43.8 m) | -253 s (-9.6%) | 525UP | 717B | `historical_fallback` | — |
| `od_03` | `corridor_717a_up` | Badarpur Border -> Lado Sarai Crossing | 14:00 IST | `compared` | 2594 s (43.2 m) | [4.5, 84.2] m | 2629 s (43.8 m) | -35 s (-1.3%) | 525UP | 717A | `historical_fallback` | — |
| `od_03` | `corridor_717a_up` | Badarpur Border -> Lado Sarai Crossing | 18:30 IST | `compared` | 3154 s (52.6 m) | [25.1, 80.2] m | 2629 s (43.8 m) | +525 s (+20.0%) | 717AUP | 525STL | `historical_fallback` | — |
| `od_04` | `corridor_717a_up` | Air Force Station -> Vasant Kunj Sec A | 08:30 IST | `compared` | 1946 s (32.4 m) | [3.5, 62.8] m | 2002 s (33.4 m) | -56 s (-2.8%) | 717UP | 717 | `historical_fallback` | — |
| `od_04` | `corridor_717a_up` | Air Force Station -> Vasant Kunj Sec A | 14:00 IST | `compared` | 2165 s (36.1 m) | [17.3, 54.8] m | 2023 s (33.7 m) | +142 s (+7.0%) | 717AUP | 717A | `historical_fallback` | — |
| `od_04` | `corridor_717a_up` | Air Force Station -> Vasant Kunj Sec A | 18:30 IST | `compared` | 2376 s (39.6 m) | [21.0, 58.1] m | 2002 s (33.4 m) | +374 s (+18.7%) | 717AUP | 717 | `historical_fallback` | — |
| `od_05` | `corridor_717a_up` | Ambedkar Nagar Depot -> Masood Pur Village | 08:30 IST | `compared` | 1841 s (30.7 m) | [3.5, 59.4] m | 1897 s (31.6 m) | -56 s (-3.0%) | 717UP | 717 | `historical_fallback` | — |
| `od_05` | `corridor_717a_up` | Ambedkar Nagar Depot -> Masood Pur Village | 14:00 IST | `compared` | 1861 s (31.0 m) | [16.1, 46.5] m | 1918 s (32.0 m) | -57 s (-3.0%) | 717AUP | 717C | `historical_fallback` | — |
| `od_05` | `corridor_717a_up` | Ambedkar Nagar Depot -> Masood Pur Village | 18:30 IST | `compared` | 2289 s (38.1 m) | [1.7, 79.8] m | 1918 s (32.0 m) | +371 s (+19.3%) | 717BUP | 717A | `historical_fallback` | — |
| `od_06` | `corridor_717a_up` | Maidan Garhi Crossing -> Rajokari Xing | 08:30 IST | `compared` | 2464 s (41.1 m) | [3.9, 80.1] m | 2737 s (45.6 m) | -273 s (-10.0%) | 717UP | 717 | `historical_fallback` | — |
| `od_06` | `corridor_717a_up` | Maidan Garhi Crossing -> Rajokari Xing | 14:00 IST | `compared` | 2886 s (48.1 m) | [24.8, 71.2] m | 2773 s (46.2 m) | +113 s (+4.1%) | 717AUP | 717A | `historical_fallback` | — |
| `od_06` | `corridor_717a_up` | Maidan Garhi Crossing -> Rajokari Xing | 18:30 IST | `compared` | 3420 s (57.0 m) | [30.9, 83.1] m | 2737 s (45.6 m) | +683 s (+24.9%) | 717AUP | 717 | `historical_fallback` | — |
| `od_07` | `corridor_717a_down` | Kapashera Border -> Badarpur Border | 08:30 IST | `compared` | 5902 s (98.4 m) | [56.5, 141.8] m | 5700 s (95.0 m) | +202 s (+3.5%) | 717ADOWN | 717A | `historical_fallback` | — |
| `od_07` | `corridor_717a_down` | Kapashera Border -> Badarpur Border | 14:00 IST | `compared` | 6248 s (104.1 m) | [54.4, 157.8] m | 5700 s (95.0 m) | +548 s (+9.6%) | 717ADOWN | 717A | `historical_fallback` | — |
| `od_07` | `corridor_717a_down` | Kapashera Border -> Badarpur Border | 18:30 IST | `compared` | 7480 s (124.7 m) | [69.1, 184.7] m | 5700 s (95.0 m) | +1780 s (+31.2%) | 717ADOWN | 717A | `historical_fallback` | — |
| `od_08` | `corridor_717a_down` | Kapashera Border -> Maidan Garhi Crossing | 08:30 IST | `compared` | 3821 s (63.7 m) | [38.4, 88.9] m | 3391 s (56.5 m) | +430 s (+12.7%) | 717ADOWN | 717A | `historical_fallback` | — |
| `od_08` | `corridor_717a_down` | Kapashera Border -> Maidan Garhi Crossing | 14:00 IST | `compared` | 3537 s (59.0 m) | [5.4, 117.5] m | 3388 s (56.5 m) | +149 s (+4.4%) | 717EDOWN | 717E | `historical_fallback` | — |
| `od_08` | `corridor_717a_down` | Kapashera Border -> Maidan Garhi Crossing | 18:30 IST | `compared` | 4020 s (67.0 m) | [1.7, 143.9] m | 3388 s (56.5 m) | +632 s (+18.6%) | 717EDOWN | 717E | `historical_fallback` | — |
| `od_09` | `corridor_717a_down` | Shiv Murti NH8 -> Ambedkar Nagar Depot | 08:30 IST | `compared` | 3209 s (53.5 m) | [31.9, 74.9] m | 4634 s (77.2 m) | -1425 s (-30.8%) | 717ADOWN | 717 | `historical_fallback` | — |
| `od_09` | `corridor_717a_down` | Shiv Murti NH8 -> Ambedkar Nagar Depot | 14:00 IST | `compared` | 3209 s (53.5 m) | [5.3, 106.5] m | 4634 s (77.2 m) | -1425 s (-30.8%) | 717EDOWN | 717 | `historical_fallback` | — |
| `od_09` | `corridor_717a_down` | Shiv Murti NH8 -> Ambedkar Nagar Depot | 18:30 IST | `compared` | 3952 s (65.9 m) | [13.6, 138.3] m | 4634 s (77.2 m) | -682 s (-14.7%) | 717DOWN | 717 | `model_live` | — |
| `od_10` | `corridor_717a_down` | Vasant Kunj Sec-A -> Air Force Station | 08:30 IST | `compared` | 1991 s (33.2 m) | [3.5, 64.3] m | 2067 s (34.5 m) | -76 s (-3.7%) | 717BDOWN | 717 | `historical_fallback` | — |
| `od_10` | `corridor_717a_down` | Vasant Kunj Sec-A -> Air Force Station | 14:00 IST | `compared` | 2151 s (35.9 m) | [17.7, 53.9] m | 2078 s (34.6 m) | +73 s (+3.5%) | 717ADOWN | 717B | `historical_fallback` | — |
| `od_10` | `corridor_717a_down` | Vasant Kunj Sec-A -> Air Force Station | 18:30 IST | `compared` | 2575 s (42.9 m) | [21.8, 63.9] m | 2067 s (34.5 m) | +508 s (+24.6%) | 717ADOWN | 717 | `historical_fallback` | — |
| `od_11` | `corridor_717a_down` | Ahinsa Sthal -> Lal Kuan | 08:30 IST | `compared` | 1866 s (31.1 m) | [17.2, 45.7] m | 2092 s (34.9 m) | -226 s (-10.8%) | 717ADOWN | 717B | `historical_fallback` | — |
| `od_11` | `corridor_717a_down` | Ahinsa Sthal -> Lal Kuan | 14:00 IST | `compared` | 2060 s (34.3 m) | [16.6, 51.9] m | 2088 s (34.8 m) | -28 s (-1.3%) | 717ADOWN | 525 | `historical_fallback` | — |
| `od_11` | `corridor_717a_down` | Ahinsa Sthal -> Lal Kuan | 18:30 IST | `compared` | 2467 s (41.1 m) | [1.7, 86.9] m | 2088 s (34.8 m) | +379 s (+18.1%) | 525DOWN | 525 | `historical_fallback` | — |
| `od_12` | `city_wide` | Azadpur Terminal -> Kashmere Gate ISBT | 08:30 IST | `compared` | 1922 s (32.0 m) | [10.8, 54.5] m | 2183 s (36.4 m) | -261 s (-12.0%) | 103DOWN | 128 | `historical_fallback` | — |
| `od_12` | `city_wide` | Azadpur Terminal -> Kashmere Gate ISBT | 14:00 IST | `compared` | 2059 s (34.3 m) | [11.1, 58.8] m | 2183 s (36.4 m) | -124 s (-5.7%) | 103DOWN | 128 | `historical_fallback` | filtered 1 non-bus Google itinerary(ies) (SUBWAY) |
| `od_12` | `city_wide` | Azadpur Terminal -> Kashmere Gate ISBT | 18:30 IST | `compared` | 2356 s (39.3 m) | [9.6, 72.8] m | 2183 s (36.4 m) | +173 s (+7.9%) | 883DOWN | 191 | `historical_fallback` | — |
| `od_13` | `city_wide` | Kashmere Gate ISBT -> AIIMS / Safdarjung | 08:30 IST | `compared` | 3078 s (51.3 m) | [13.7, 91.0] m | 3686 s (61.4 m) | -608 s (-16.5%) | 605UP | 502 | `historical_fallback` | — |
| `od_13` | `city_wide` | Kashmere Gate ISBT -> AIIMS / Safdarjung | 14:00 IST | `compared` | 3438 s (57.3 m) | [20.0, 104.7] m | 3470 s (57.8 m) | -32 s (-0.9%) | 502UP | 502 + 794A | `model_live` | filtered 1 non-bus Google itinerary(ies) (SUBWAY) |
| `od_13` | `city_wide` | Kashmere Gate ISBT -> AIIMS / Safdarjung | 18:30 IST | `compared` | 3606 s (60.1 m) | [11.4, 117.9] m | 3686 s (61.4 m) | -80 s (-2.2%) | 605UP | 502 | `historical_fallback` | filtered 1 non-bus Google itinerary(ies) (SUBWAY) |
| `od_14` | `city_wide` | Shivaji Stadium -> Nehru Place | 08:30 IST | `compared` | 3523 s (58.7 m) | [20.4, 95.7] m | 3338 s (55.6 m) | +185 s (+5.5%) | 359UP + 440UP | 281 + 445 | `historical_fallback` | — |
| `od_14` | `city_wide` | Shivaji Stadium -> Nehru Place | 14:00 IST | `compared` | 3896 s (64.9 m) | [27.9, 103.8] m | 3353 s (55.9 m) | +543 s (+16.2%) | 440UP | 181 + 522 + D-014 | `historical_fallback` | — |
| `od_14` | `city_wide` | Shivaji Stadium -> Nehru Place | 18:30 IST | `compared` | 3976 s (66.3 m) | [12.9, 118.4] m | 3376 s (56.3 m) | +600 s (+17.8%) | 359UP + 440AUP | 164 + 440 | `historical_fallback` | — |
| `od_15` | `city_wide` | Anand Vihar ISBT -> ITO | 08:30 IST | `compared` | 2152 s (35.9 m) | [9.7, 63.6] m | 2377 s (39.6 m) | -225 s (-9.5%) | 943UP | 88A | `historical_fallback` | — |
| `od_15` | `city_wide` | Anand Vihar ISBT -> ITO | 14:00 IST | `compared` | 2616 s (43.6 m) | [10.1, 78.5] m | 2608 s (43.5 m) | +8 s (+0.3%) | 740AUP | D-5101 | `historical_fallback` | — |
| `od_15` | `city_wide` | Anand Vihar ISBT -> ITO | 18:30 IST | `compared` | 2574 s (42.9 m) | [8.0, 82.7] m | 2436 s (40.6 m) | +138 s (+5.7%) | 943UP | 943 | `historical_fallback` | — |
| `od_16` | `city_wide` | Anand Vihar ISBT -> Kashmere Gate ISBT | 08:30 IST | `compared` | 2728 s (45.5 m) | [9.4, 83.3] m | 2999 s (50.0 m) | -271 s (-9.0%) | GL-23DOWN | 143 | `historical_fallback` | — |
| `od_16` | `city_wide` | Anand Vihar ISBT -> Kashmere Gate ISBT | 14:00 IST | `compared` | 2844 s (47.4 m) | [8.0, 89.2] m | 2999 s (50.0 m) | -155 s (-5.2%) | 143UP | 143 | `historical_fallback` | — |
| `od_16` | `city_wide` | Anand Vihar ISBT -> Kashmere Gate ISBT | 18:30 IST | `compared` | 3229 s (53.8 m) | [7.3, 109.0] m | 2999 s (50.0 m) | +230 s (+7.7%) | GL-23DOWN | 143 | `historical_fallback` | — |
| `od_17` | `city_wide` | Sarai Kale Khan ISBT -> Dhaula Kuan | 08:30 IST | `compared` | 2923 s (48.7 m) | [4.7, 87.0] m | 3498 s (58.3 m) | -575 s (-16.4%) | D-056UP + 711DOWN | TMS(+) | `historical_fallback` | — |
| `od_17` | `city_wide` | Sarai Kale Khan ISBT -> Dhaula Kuan | 14:00 IST | `compared` | 3441 s (57.4 m) | [8.7, 98.6] m | 3472 s (57.9 m) | -31 s (-0.9%) | 311ADOWN + 392STLDOWN | 763 | `historical_fallback` | — |
| `od_17` | `city_wide` | Sarai Kale Khan ISBT -> Dhaula Kuan | 18:30 IST | `compared` | 3776 s (62.9 m) | [7.0, 117.9] m | 3498 s (58.3 m) | +278 s (+8.0%) | 311ADOWN + 567UP | 567 | `historical_fallback` | — |
| `od_18` | `city_wide` | Dhaula Kuan -> AIIMS / Safdarjung | 08:30 IST | `compared` | 2011 s (33.5 m) | [16.0, 51.9] m | 2273 s (37.9 m) | -262 s (-11.5%) | 724AUP + 442UP | 392 | `historical_fallback,model_live` | — |
| `od_18` | `city_wide` | Dhaula Kuan -> AIIMS / Safdarjung | 14:00 IST | `compared` | 2157 s (36.0 m) | [21.3, 52.0] m | 2488 s (41.5 m) | -331 s (-13.3%) | TMS(-)DOWN | 507 | `historical_fallback` | — |
| `od_18` | `city_wide` | Dhaula Kuan -> AIIMS / Safdarjung | 18:30 IST | `compared` | 2222 s (37.0 m) | [27.0, 57.2] m | 2488 s (41.5 m) | -266 s (-10.7%) | 392UP + D-028UP | 507 | `historical_fallback` | — |
| `od_19` | `city_wide` | Karol Bagh Terminal -> Lajpat Nagar | 08:30 IST | `compared` | 3196 s (53.3 m) | [23.6, 87.6] m | 2822 s (47.0 m) | +374 s (+13.2%) | 894DOWN + 429UP | 894 + 427 | `historical_fallback` | — |
| `od_19` | `city_wide` | Karol Bagh Terminal -> Lajpat Nagar | 14:00 IST | `compared` | 3420 s (57.0 m) | [41.5, 96.0] m | 2822 s (47.0 m) | +598 s (+21.2%) | 966DOWN + 427UP | 894 + 427 | `historical_fallback,model_live` | — |
| `od_19` | `city_wide` | Karol Bagh Terminal -> Lajpat Nagar | 18:30 IST | `compared` | 3609 s (60.1 m) | [50.1, 109.0] m | 2815 s (46.9 m) | +794 s (+28.2%) | 894DOWN + D-044UP | 966 + 429 | `historical_fallback` | — |
| `od_20` | `city_wide` | Uttam Nagar Terminal -> Shivaji Stadium | 08:30 IST | `compared` | 4725 s (78.8 m) | [60.1, 116.8] m | 4374 s (72.9 m) | +351 s (+8.0%) | 817NDOWN + 410UP + 957DOWN | 703 | `historical_fallback` | — |
| `od_20` | `city_wide` | Uttam Nagar Terminal -> Shivaji Stadium | 14:00 IST | `compared` | 4764 s (79.4 m) | [16.9, 147.4] m | 4359 s (72.7 m) | +405 s (+9.3%) | 703DOWN | 816 + 910 | `historical_fallback` | — |
| `od_20` | `city_wide` | Uttam Nagar Terminal -> Shivaji Stadium | 18:30 IST | `compared` | 5750 s (95.8 m) | [76.0, 174.8] m | 4317 s (72.0 m) | +1433 s (+33.2%) | 740EXTDOWN + 910DOWN | 816B + 940 | `historical_fallback` | — |
| `od_21` | `city_wide` | Uttam Nagar Terminal -> Dhaula Kuan | 08:30 IST | `compared` | 2447 s (40.8 m) | [3.9, 79.5] m | 3415 s (56.9 m) | -968 s (-28.4%) | 724AUP | 874 + 479 | `historical_fallback` | — |
| `od_21` | `city_wide` | Uttam Nagar Terminal -> Dhaula Kuan | 14:00 IST | `compared` | 2667 s (44.5 m) | [4.6, 86.8] m | 3397 s (56.6 m) | -730 s (-21.5%) | 724AUP | 874 + 724B3 | `historical_fallback` | — |
| `od_21` | `city_wide` | Uttam Nagar Terminal -> Dhaula Kuan | 18:30 IST | `compared` | 2962 s (49.4 m) | [1.7, 105.9] m | 3444 s (57.4 m) | -482 s (-14.0%) | 724AUP | 740 Ext. + 724 Ext. + 711A | `historical_fallback` | — |
| `od_22` | `city_wide` | New Delhi Railway Station -> Hauz Khas | 08:30 IST | `compared` | 2573 s (42.9 m) | [11.1, 76.1] m | 2837 s (47.3 m) | -264 s (-9.3%) | 505UP | 456A + 505 | `historical_fallback` | — |
| `od_22` | `city_wide` | New Delhi Railway Station -> Hauz Khas | 14:00 IST | `compared` | 2767 s (46.1 m) | [11.5, 82.3] m | 2844 s (47.4 m) | -77 s (-2.7%) | 505UP | 615 + D-054STL | `historical_fallback` | — |
| `od_22` | `city_wide` | New Delhi Railway Station -> Hauz Khas | 18:30 IST | `compared` | 3070 s (51.2 m) | [10.2, 99.1] m | 2878 s (48.0 m) | +192 s (+6.7%) | 548UP | 460 + 519 | `historical_fallback` | — |
| `od_23` | `city_wide` | Mehrauli Terminal -> AIIMS / Safdarjung | 08:30 IST | `compared` | 1226 s (20.4 m) | [2.8, 39.3] m | 1240 s (20.7 m) | -14 s (-1.1%) | 502DOWN | 725 | `historical_fallback` | — |
| `od_23` | `city_wide` | Mehrauli Terminal -> AIIMS / Safdarjung | 14:00 IST | `compared` | 1345 s (22.4 m) | [3.1, 43.0] m | 1240 s (20.7 m) | +105 s (+8.5%) | 725UP | 725 | `historical_fallback` | — |
| `od_23` | `city_wide` | Mehrauli Terminal -> AIIMS / Safdarjung | 18:30 IST | `compared` | 1527 s (25.4 m) | [1.7, 52.5] m | 1240 s (20.7 m) | +287 s (+23.1%) | 725UP | 725 | `historical_fallback` | — |
| `od_24` | `city_wide` | Punjabi Bagh Terminal -> Karol Bagh Terminal | 08:30 IST | `compared` | 1822 s (30.4 m) | [9.0, 52.9] m | 1654 s (27.6 m) | +168 s (+10.2%) | 957DOWN | 957 | `historical_fallback` | — |
| `od_24` | `city_wide` | Punjabi Bagh Terminal -> Karol Bagh Terminal | 14:00 IST | `compared` | 1959 s (32.6 m) | [9.3, 57.3] m | 1654 s (27.6 m) | +305 s (+18.4%) | 957DOWN | 957 | `historical_fallback` | — |
| `od_24` | `city_wide` | Punjabi Bagh Terminal -> Karol Bagh Terminal | 18:30 IST | `compared` | 2176 s (36.3 m) | [7.6, 68.7] m | 1654 s (27.6 m) | +522 s (+31.6%) | 957DOWN | 957 | `historical_fallback` | — |
| `od_25` | `city_wide` | Shahdara -> Red Fort | 08:30 IST | `compared` | 1933 s (32.2 m) | [11.0, 54.6] m | 1859 s (31.0 m) | +74 s (+4.0%) | 214DOWN | 214 | `historical_fallback` | — |
| `od_25` | `city_wide` | Shahdara -> Red Fort | 14:00 IST | `compared` | 2070 s (34.5 m) | [11.3, 59.0] m | 1859 s (31.0 m) | +211 s (+11.3%) | 214DOWN | 214 | `historical_fallback` | — |
| `od_25` | `city_wide` | Shahdara -> Red Fort | 18:30 IST | `compared` | 2285 s (38.1 m) | [9.6, 70.3] m | 1859 s (31.0 m) | +426 s (+22.9%) | 214DOWN | 214 | `historical_fallback` | — |
| `od_26` | `city_wide` | Lajpat Nagar -> Badarpur Border | 08:30 IST | `compared` | 2104 s (35.1 m) | [3.5, 68.0] m | 2253 s (37.5 m) | -149 s (-6.6%) | 418AUP | 460STL | `historical_fallback` | — |
| `od_26` | `city_wide` | Lajpat Nagar -> Badarpur Border | 14:00 IST | `compared` | 2466 s (41.1 m) | [4.2, 79.9] m | 2370 s (39.5 m) | +96 s (+4.0%) | 433UP | 469 + Sarai Kale Khan to Etawah | `historical_fallback` | — |
| `od_26` | `city_wide` | Lajpat Nagar -> Badarpur Border | 18:30 IST | `compared` | 2556 s (42.6 m) | [1.7, 90.4] m | 2253 s (37.5 m) | +303 s (+13.4%) | 418AUP | 460STL | `historical_fallback` | — |
| `od_27` | `city_wide` | ITO -> Nehru Place | 08:30 IST | `compared` | 2339 s (39.0 m) | [9.1, 70.4] m | 2467 s (41.1 m) | -128 s (-5.2%) | 425UP | 425 | `historical_fallback` | — |
| `od_27` | `city_wide` | ITO -> Nehru Place | 14:00 IST | `compared` | 2526 s (42.1 m) | [9.3, 76.2] m | 2467 s (41.1 m) | +59 s (+2.4%) | 425UP | 425 | `historical_fallback` | — |
| `od_27` | `city_wide` | ITO -> Nehru Place | 18:30 IST | `compared` | 2781 s (46.4 m) | [7.3, 91.7] m | 2441 s (40.7 m) | +340 s (+13.9%) | 425UP | 425A | `historical_fallback` | — |
| `od_28` | `city_wide` | Azadpur Terminal -> Punjabi Bagh Terminal | 08:30 IST | `compared` | 1485 s (24.8 m) | [8.5, 42.1] m | 1471 s (24.5 m) | +14 s (+0.9%) | 938AUP | 938A | `historical_fallback` | — |
| `od_28` | `city_wide` | Azadpur Terminal -> Punjabi Bagh Terminal | 14:00 IST | `compared` | 1594 s (26.6 m) | [8.8, 45.5] m | 1406 s (23.4 m) | +188 s (+13.4%) | 978UP | 935 | `historical_fallback` | — |
| `od_28` | `city_wide` | Azadpur Terminal -> Punjabi Bagh Terminal | 18:30 IST | `compared` | 1771 s (29.5 m) | [7.6, 54.5] m | 1406 s (23.4 m) | +365 s (+26.0%) | 978UP | 935 | `historical_fallback` | — |
| `od_29` | `city_wide` | Hauz Khas -> Kalkaji Mandir | 08:30 IST | `compared` | 2093 s (34.9 m) | [11.3, 57.3] m | 1996 s (33.3 m) | +97 s (+4.9%) | 507UP + 433UP | 465A | `historical_fallback` | — |
| `od_29` | `city_wide` | Hauz Khas -> Kalkaji Mandir | 14:00 IST | `compared` | 2292 s (38.2 m) | [14.1, 62.7] m | 1953 s (32.5 m) | +339 s (+17.4%) | 507UP + 433UP | 507 + D-042 | `historical_fallback` | — |
| `od_29` | `city_wide` | Hauz Khas -> Kalkaji Mandir | 18:30 IST | `compared` | 2566 s (42.8 m) | [10.3, 73.2] m | 2057 s (34.3 m) | +509 s (+24.7%) | 507UP + 433UP | 507 + 433 | `historical_fallback` | — |
| `od_30` | `city_wide` | Nehru Place -> Sarai Kale Khan ISBT | 08:30 IST | `compared` | 2477 s (41.3 m) | [22.9, 60.9] m | 1958 s (32.6 m) | +519 s (+26.5%) | 534ADOWN | 306 | `historical_fallback` | — |
| `od_30` | `city_wide` | Nehru Place -> Sarai Kale Khan ISBT | 14:00 IST | `compared` | 2601 s (43.4 m) | [23.2, 64.7] m | 1958 s (32.6 m) | +643 s (+32.8%) | 534DOWN | 306 | `historical_fallback` | — |
| `od_30` | `city_wide` | Nehru Place -> Sarai Kale Khan ISBT | 18:30 IST | `compared` | 2681 s (44.7 m) | [21.0, 71.7] m | 1958 s (32.6 m) | +723 s (+36.9%) | 306UP | 306 | `historical_fallback` | — |

## Reproducing From Cache (0 Additional Google API Calls)

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon && \
  /home/factory-user/ps1-scratch/venv/bin/python scripts/compare_google.py \
  --date 2026-11-24 --cache-only
```
