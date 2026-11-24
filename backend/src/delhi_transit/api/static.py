"""The React UI built into `frontend/dist`, served at `/` and `/assets`."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse

from delhi_transit.core import config

router = APIRouter(include_in_schema=False)

NOT_BUILT = "The UI is not built. Run `bun install --frozen-lockfile && bun run build` in frontend/, then reload.\n"


@router.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@router.get("/", response_model=None)
async def index() -> FileResponse | PlainTextResponse:
    page = config.FRONTEND_DIR / "index.html"
    if not page.is_file():
        return PlainTextResponse(NOT_BUILT)
    return FileResponse(page, media_type="text/html")


@router.get("/assets/{asset_path:path}")
async def asset(asset_path: str) -> FileResponse:
    assets = (config.FRONTEND_DIR / "assets").resolve()
    target = (assets / asset_path).resolve()
    if not target.is_relative_to(assets) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)
