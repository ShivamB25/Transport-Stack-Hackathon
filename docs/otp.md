# OpenTripPlanner 2.8.1

OTP generates Delhi bus itinerary candidates from the supplied static GTFS and
BBBike NewDelhi street extract. The planner API re-times those candidates
separately; OTP itself remains schedule-based.

## Inputs and pins

- Java: Temurin 21 at `/home/factory-user/ps1-scratch/jdk/bin/java`.
- OTP: `otp-shaded-2.8.1.jar` from
  `https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.8.1/otp-shaded-2.8.1.jar`.
- GTFS: `$PS1_DATA_DIR/gtfs/delhi_buses_static_gtfs_v1.zip` (feed ID `DTC`).
- OSM: BBBike `NewDelhi.osm.pbf` from
  `https://download.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf`.

The wrappers default to `/home/factory-user/ps1-scratch/otp` and download the jar
or OSM file only when missing. `PS1_OTP_DIR`, `PS1_DATA_DIR`, and `PS1_JAVA` can
override the runtime paths.

## Build and serve

```bash
cd /home/factory-user/repos/Transport-Stack-Hackathon
scripts/otp_build.sh > /home/factory-user/ps1-scratch/otp/build.log 2>&1
scripts/otp_serve.sh
```

Both commands enforce `-Xmx2500m`. The build uses `--build --save` and writes
`/home/factory-user/ps1-scratch/otp/graph/graph.obj`. The server uses `--load
--serve --port 3102`. The generated build config limits transit service to
2026-10-01 through 2026-12-31 and walking transfers to 10 minutes. The complete
37.5 MB BBBike extract fit within the heap cap, so no OSM clipping was needed.

The declared service healthcheck is:

```bash
curl -sf -X POST http://localhost:3102/otp/gtfs/v1 \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ __schema { queryType { name } } }"}'
```

## Smoke query

The checked-in request asks for Kashmere Gate ISBT (28.6675, 77.2282) to Nehru
Place (28.5494, 77.2510), departing Tuesday 2026-11-24 at 08:30 IST:

```bash
curl -sf -X POST http://localhost:3102/otp/gtfs/v1 \
  -H 'Content-Type: application/json' \
  -H 'OTPTimeout: 30000' \
  --data @reports/otp-plan-request.json
```

The full response is in `reports/otp-plan-response.json`, and the observed
results are summarized in `reports/otp-smoke.md`.

Focused introspection against the running 2.8.1 schema confirmed the
`planConnection` arguments and all fields used by this request, including
`PlanCoordinateInput.latitude/longitude`, `Itinerary.legs`, `Leg.start/end`,
`Place.stop`, and `PlanPageInfo.searchWindowUsed`. The 2.10 research sample's
optional `stopCalls` selection is not needed for this smoke test. Raw focused
introspection output is at
`/home/factory-user/ps1-scratch/otp/schema-introspection.json`.
