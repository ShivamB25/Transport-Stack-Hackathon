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
# RetimeModel reads the ship decision from <REPO_ROOT>/reports/selection.json.
COPY reports/selection.json /app/reports/selection.json

ENV PATH=/app/backend/.venv/bin:$PATH
EXPOSE 3100
CMD ["uvicorn", "delhi_transit.api:app", "--host", "0.0.0.0", "--port", "3100"]
