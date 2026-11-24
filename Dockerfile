FROM python:3.12-slim

# LightGBM's wheel links against the OpenMP runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
# RetimeModel reads the ship decision from <repo>/reports/selection.json.
COPY reports/selection.json ./reports/selection.json

EXPOSE 3100
CMD ["uvicorn", "ps1.api:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "3100"]
