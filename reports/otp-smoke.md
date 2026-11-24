# OTP Delhi graph smoke evidence

Verified on 2026-11-23 against the real local service.

## Build

- OTP 2.8.1 (`719cb97337e6df44dba5c8fee94ec83cc9110aee`) on Temurin Java 21.0.13.1.
- Command: `otp/build.sh`, which runs Java with `-Xmx2500m -XX:+UseG1GC --build --save`.
- GTFS input: `/home/factory-user/ps1-scratch/data/gtfs/delhi_buses_static_gtfs_v1.zip`, SHA-256 `8eb05d9c21e7a6b42d7b49de45cb095b70ddee5b8f7716859330c9784c322f05`.
- OSM input: `/home/factory-user/ps1-scratch/otp/NewDelhi.osm.pbf`, SHA-256 `c4bea92fba4541eeaac2898c12b8e30307e57cd9dae2391422340aa77fd1ec81`.
- OTP jar SHA-256: `8ddd736db68846c5d40797d62a6a76dde5d9e6076f23b711311fb8c5b0082749`.
- Build completed in 1m22.896s with no `ERROR`, `OutOfMemoryError`, or GC-overhead messages in `/home/factory-user/ps1-scratch/otp/build.log`.
- Output: `/home/factory-user/ps1-scratch/otp/graph/graph.obj`, 85,610,586 bytes, SHA-256 `90f6af538901125953461cae71279af52ff6be3199c16740f83bb911cf427ffd`.
- Server startup loaded 6,862 stops, 3,498 patterns, 390,843 street vertices, and 1,052,434 street edges. Startup log: `/home/factory-user/ps1-scratch/otp/serve.log`.
- After the final smoke query through `otp/serve.sh`, `ps -C java` showed only the intended server (PID 35976), with `-Xmx2500m` and 1,421,996 KiB RSS.

## GraphQL planConnection

Request: `reports/otp-plan-request.json` (also at
`/home/factory-user/ps1-scratch/otp/plan-request.json`). Response:
`reports/otp-plan-response.json` (also at
`/home/factory-user/ps1-scratch/otp/plan-response.json`).

The response has no GraphQL `errors`, no `routingErrors`, five itineraries, and
six transit legs. Every transit leg has non-null boarding and alighting stop
objects, nonempty stop names, and scheduled start/end times.

Observed examples:

| Itinerary | Transit legs | Start | End | Transfers |
| --- | --- | --- | --- | --- |
| 1 | 429UP: ISBT Kashmere Gate Terminal → Sunder Nagar; 445UP: Sunder Nagar → Nehru Place | 2026-11-24T08:38:10+05:30 | 2026-11-24T09:36:33+05:30 | 1 |
| 2 | 425CLUP: Kashmere Gate City Bus Terminal → Nehru Place | 2026-11-24T08:36:55+05:30 | 2026-11-24T09:36:54+05:30 | 0 |
| 3 | 425UP: ISBT Kashmere Gate Terminal → Nehru Place | 2026-11-24T08:41:10+05:30 | 2026-11-24T09:39:44+05:30 | 0 |

For example, itinerary 2's bus departs Kashmere Gate City Bus Terminal at
08:43:00 IST and arrives at Nehru Place at 09:31:20 IST.
