"""FastAPI application: runtime wiring, the background live poller and the routers."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from delhi_transit.api import static
from delhi_transit.api.routes import ApiRuntime, StopResolver, build_router
from delhi_transit.core.config import (
    DATA_DIR,
    LIVE_POLL_INTERVAL_S,
    OTP_URL,
    live_poller_enabled,
    runtime_mode,
)
from delhi_transit.live.poller import LivePoller
from delhi_transit.planner.otp_client import OTPClient
from delhi_transit.planner.rides import RetimeModel


def create_app(
    data_dir: Path | None = None,
    otp_url: str | None = None,
    otp: Any | None = None,
    model: Any | None = None,
    *,
    enable_poller: bool | None = None,
    poller: LivePoller | None = None,
    stale_threshold_s: float | None = None,
    min_live_vehicles: int | None = None,
    min_route_coverage: float | None = None,
) -> FastAPI:
    using_default_runtime = data_dir is None and otp is None and model is None
    resolved_data_dir = Path(data_dir or DATA_DIR)
    resolver = StopResolver(resolved_data_dir / "gtfs" / "stops.txt")
    otp_client = otp or OTPClient(otp_url or OTP_URL)
    timing_model = model or RetimeModel(resolved_data_dir)

    if enable_poller is None:
        poller_enabled = using_default_runtime and live_poller_enabled() and runtime_mode() != "replay"
    else:
        poller_enabled = enable_poller

    live_poller = poller or LivePoller(resolved_data_dir, interval_s=LIVE_POLL_INTERVAL_S)
    poller_stop = asyncio.Event()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task: asyncio.Task[None] | None = None
        if poller_enabled and live_poller.auth_configured:
            task = asyncio.create_task(live_poller.run_loop(poller_stop))
        try:
            yield
        finally:
            poller_stop.set()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="Delhi Model-Timed Journeys",
        version="1.0.0",
        description="OTP bus candidates re-timed from causal GPS replay and live VehiclePositions with joint journey windows.",
        lifespan=lifespan,
    )
    runtime = ApiRuntime(
        data_dir=resolved_data_dir,
        resolver=resolver,
        otp_client=otp_client,
        otp_url=otp_url or OTP_URL,
        timing_model=timing_model,
        live_poller=live_poller,
        poller_enabled=poller_enabled,
        stale_threshold_override=stale_threshold_s,
        min_vehicles_override=min_live_vehicles,
        min_route_coverage_override=min_route_coverage,
    )
    app.include_router(build_router(runtime))
    app.include_router(static.router)
    return app


app = create_app()
