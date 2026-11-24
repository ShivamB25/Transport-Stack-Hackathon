"""Live VehiclePositions poller: decoding, persistence, rejected payloads and key handling."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from delhi_transit.live.poller import (
    LivePoller,
    decode_vehicle_positions,
    persist_decoded_feed,
)


def test_poller_decodes_protobuf_persists_snapshots_and_never_logs_key(tmp_path: Path, make_protobuf) -> None:
    secret_key = "super-secret-delhi-key-do-not-log"
    now = int(time.time())
    header_ts = now - 22
    payload = make_protobuf(
        header_ts,
        [
            ("DL1PD1001", "100", 28.65, 77.22, 6.0, header_ts - 10),
            ("DL1PD1002", "100", 28.64, 77.23, 5.0, header_ts - 20),
            # Stale entity (> 15 min) retained by source
            ("DL1PD1003", "100", 28.63, 77.24, 4.0, header_ts - 1200),
            # Out-of-bbox garbage coordinate
            ("DL51EV2001", "200", 99.0, 200.0, 3.0, header_ts - 5),
        ],
    )
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("x-api-key"))
        return httpx.Response(200, content=payload, headers={"content-type": "application/octet-stream"})

    poller = LivePoller(tmp_path, feed_url="https://example.test/vp.pb", api_key=secret_key)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = poller.poll_once(client=client)

    assert seen_headers == [secret_key]
    assert state["status"] == "ok"
    assert state["vehicle_count"] == 4
    assert state["fresh_vehicle_count"] == 3
    assert state["in_bbox_count"] == 3
    assert state["route_count"] == 2
    assert 0.0 <= state["feed_age_seconds_at_poll"] <= 60.0
    assert Path(state["snapshot_path"]).exists()
    assert (tmp_path / "live" / "latest.parquet").exists()

    route_stats = json.loads((tmp_path / "live" / "route_stats.json").read_text())
    assert route_stats["routes"]["100"]["fresh_vehicle_count"] == 2
    assert route_stats["routes"]["100"]["median_speed_mps"] == 5.5

    # API key must NEVER appear in persisted state, stats, or logs
    for file_path in [
        tmp_path / "live" / "feed_state.json",
        tmp_path / "live" / "route_stats.json",
        tmp_path / "live" / "poller.log",
    ]:
        assert secret_key not in file_path.read_text(encoding="utf-8")


def test_poller_rejects_html_spa_shell_and_preserves_last_good_timestamp(tmp_path: Path, make_protobuf) -> None:
    now = int(time.time())
    good_ts = now - 15
    good_pb = make_protobuf(good_ts, [("DL1PD1001", "100", 28.65, 77.22, 6.0, good_ts - 5)])
    decoded = decode_vehicle_positions(good_pb, polled_at=now)
    persist_decoded_feed(tmp_path, decoded)

    def html_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<!doctype html><html><body>SPA shell</body></html>")

    poller = LivePoller(tmp_path, feed_url="https://example.test/vp.pb", api_key="k")
    with httpx.Client(transport=httpx.MockTransport(html_handler)) as client:
        state = poller.poll_once(client=client)

    assert state["status"] == "degraded"
    assert state["consecutive_failures"] == 1
    assert state["header_timestamp"] == good_ts
    assert state["last_error"] == "upstream_non_protobuf_payload"
