# Scraper microservice: screener.in -> pypdf text -> S3. Stateless, single small
# container. Uses gunicorn (no in-memory cross-request state to preserve here).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8090

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY . .

EXPOSE 8090

# 2 gthread workers: scrapes are I/O-bound; the app's own semaphore caps real
# concurrency. No shared in-memory state across workers, so >1 worker is safe.
CMD ["gunicorn", "--bind", "0.0.0.0:8090", "--workers", "2", "--threads", "4", \
     "--timeout", "180", "app:app"]
