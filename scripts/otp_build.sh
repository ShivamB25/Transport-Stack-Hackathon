#!/usr/bin/env bash
set -euo pipefail

OTP_DIR="${PS1_OTP_DIR:-/home/factory-user/ps1-scratch/otp}"
DATA_DIR="${PS1_DATA_DIR:-/home/factory-user/ps1-scratch/data}"
JAVA="${PS1_JAVA:-/home/factory-user/ps1-scratch/jdk/bin/java}"
JAR="$OTP_DIR/otp-shaded-2.8.1.jar"
OSM="$OTP_DIR/NewDelhi.osm.pbf"
GTFS="$DATA_DIR/gtfs/delhi_buses_static_gtfs_v1.zip"
GRAPH="$OTP_DIR/graph"

mkdir -p "$OTP_DIR" "$GRAPH"
if [[ ! -s "$JAR" ]]; then
  curl -fL --retry 3 \
    https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.8.1/otp-shaded-2.8.1.jar \
    -o "$JAR.part"
  mv "$JAR.part" "$JAR"
fi
if [[ ! -s "$OSM" ]]; then
  curl -fL --retry 3 \
    https://download.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf \
    -o "$OSM.part"
  mv "$OSM.part" "$OSM"
fi
for input in "$JAR" "$OSM" "$GTFS"; do
  if [[ ! -s "$input" ]]; then
    printf 'Missing required input: %s\n' "$input" >&2
    exit 1
  fi
done
# Keep the graph directory self-contained so it resolves when mounted elsewhere
# (for example at /otp in the Compose otp service).
rm -f "$GRAPH/delhi.gtfs.zip"
cp "$GTFS" "$GRAPH/delhi.gtfs.zip"
ln -sfn ../NewDelhi.osm.pbf "$GRAPH/NewDelhi.osm.pbf"
cat > "$GRAPH/build-config.json" <<'JSON'
{
  "osm": [
    { "source": "NewDelhi.osm.pbf", "osmTagMapping": "default" }
  ],
  "transitFeeds": [
    { "type": "gtfs", "feedId": "DTC", "source": "delhi.gtfs.zip" }
  ],
  "transitServiceStart": "2026-10-01",
  "transitServiceEnd": "2026-12-31",
  "maxTransferDuration": "PT10M",
  "osmCacheDataInMem": false,
  "areaVisibility": false,
  "dataImportReport": false
}
JSON

exec "$JAVA" -Xmx2500m -XX:+UseG1GC -jar "$JAR" --build --save "$GRAPH"
