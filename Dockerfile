# Stock-watchlist: single always-on container (Flask dashboard + voice module +
# in-process APScheduler). Runs ONE process on purpose — the rate limiter, voice
# semaphore and scheduler all keep state in memory, so do not scale to >1 worker.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code (secrets and the venv/state are excluded via .dockerignore).
COPY . .

# Mutable state (watchlist.json, alert_log.json, research/) lives on a mounted
# volume so it survives redeploys; seeded from the baked watchlist on first boot.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8088

# main.py: start the scheduler, then serve (threaded) — see main.main().
CMD ["python", "main.py"]
