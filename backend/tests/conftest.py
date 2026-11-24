from __future__ import annotations

import pytest
from google.transit import gtfs_realtime_pb2


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
