from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2

# Importing delhi_transit.api builds the default app, which reads gtfs/stops.txt from the data
# directory. Without the real data (for example in CI), point the data directory at an empty one
# with a header-only stops file; tests that need the real data skip on their own.
_DATA_DIR = Path(os.environ.get("DELHI_TRANSIT_DATA_DIR", "/home/factory-user/ps1-scratch/data"))
if not (_DATA_DIR / "gtfs" / "stops.txt").exists():
    _stub = Path(tempfile.mkdtemp(prefix="delhi-transit-test-data-"))
    (_stub / "gtfs").mkdir()
    (_stub / "gtfs" / "stops.txt").write_text("stop_id,stop_name,stop_lat,stop_lon\n", encoding="utf-8")
    os.environ["DELHI_TRANSIT_DATA_DIR"] = str(_stub)


def build_vehicle_positions(
    header_ts: int,
    vehicles: list[tuple[str, str, float, float, float, int]],
) -> bytes:
    """GTFS-RT VehiclePositions bytes from (vehicle_id, route_id, lat, lng, speed, ts) tuples."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = header_ts
    for idx, (vid, route_id, lat, lng, speed, ts) in enumerate(vehicles):
        entity = feed.entity.add()
        entity.id = f"ent-{idx}"
        entity.vehicle.vehicle.id = vid
        entity.vehicle.vehicle.label = vid
        entity.vehicle.trip.route_id = route_id
        entity.vehicle.trip.trip_id = f"{route_id}_08_00_{idx}"
        entity.vehicle.trip.start_time = "08:00:00"
        entity.vehicle.trip.start_date = "20261124"
        entity.vehicle.position.latitude = lat
        entity.vehicle.position.longitude = lng
        entity.vehicle.position.speed = speed
        entity.vehicle.timestamp = ts
    return feed.SerializeToString()


@pytest.fixture
def make_protobuf():
    return build_vehicle_positions
