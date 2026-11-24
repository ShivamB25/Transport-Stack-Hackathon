"""Minimal OpenTripPlanner 2.8 GraphQL candidate client."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

PLAN_QUERY = """
query Plan($from: PlanCoordinateInput!, $to: PlanCoordinateInput!, $t: OffsetDateTime!) {
  planConnection(
    origin: { location: { coordinate: $from } }
    destination: { location: { coordinate: $to } }
    dateTime: { earliestDeparture: $t }
    first: 5
    searchWindow: "PT60M"
    modes: { direct: [WALK], transit: { transit: [{ mode: BUS }] } }
    preferences: {
      street: { walk: { speed: 1.2, reluctance: 2.0 } }
      transit: { transfer: { slack: "PT1M", maximumTransfers: 3 } }
    }
    itineraryFilter: { groupSimilarityKeepOne: 0.95, groupSimilarityKeepThree: 0.85 }
  ) {
    routingErrors { code description }
    edges { node {
      start end duration numberOfTransfers
      legs {
        mode transitLeg duration distance serviceDate headsign
        start { scheduledTime }
        end { scheduledTime }
        from { name lat lon stop { gtfsId code } }
        to { name lat lon stop { gtfsId code } }
        route { gtfsId shortName longName }
        trip { gtfsId }
        legGeometry { points }
      }
    } }
  }
}
"""


class OTPClientError(RuntimeError):
    """A controlled upstream OTP failure."""


class OTPClient:
    def __init__(self, url: str, timeout_s: float = 25.0) -> None:
        self.url = url
        self.timeout_s = timeout_s

    async def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart: datetime,
    ) -> list[dict[str, Any]]:
        payload = {
            "query": PLAN_QUERY,
            "variables": {
                "from": {"latitude": origin[0], "longitude": origin[1]},
                "to": {"latitude": destination[0], "longitude": destination[1]},
                "t": depart.isoformat(),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(self.url, json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OTPClientError("OTP candidate service is unavailable") from exc
        if body.get("errors"):
            raise OTPClientError("OTP rejected the candidate query")
        connection = body.get("data", {}).get("planConnection")
        if connection is None:
            raise OTPClientError("OTP returned no candidate response")
        if connection.get("routingErrors"):
            raise OTPClientError("OTP could not route between these endpoints")
        return [edge["node"] for edge in connection.get("edges", [])]
