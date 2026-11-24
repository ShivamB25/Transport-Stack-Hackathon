FROM oven/bun:1.4.5 AS frontend

WORKDIR /app/frontend
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend/ ./
RUN bun run build

FROM python:3.12-slim

# LightGBM's wheel links against the OpenMP runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev
# REPO_ROOT resolves to /app, so the API reads the ship decision from /app/reports/selection.json
# and serves the UI from /app/frontend/dist (the default DELHI_TRANSIT_FRONTEND_DIR).
COPY reports/selection.json /app/reports/selection.json
COPY --from=frontend /app/frontend/dist /app/frontend/dist

ENV PATH=/app/backend/.venv/bin:$PATH
EXPOSE 3100
CMD ["uvicorn", "delhi_transit.api:app", "--host", "0.0.0.0", "--port", "3100"]
