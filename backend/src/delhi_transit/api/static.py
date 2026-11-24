"""The browser UI served at `/`."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter(include_in_schema=False)


@router.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
