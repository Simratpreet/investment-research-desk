# scraper_service

Standalone microservice that populates the voice S3 bucket (`VOICE_S3_BUCKET`)
for one symbol on request. The main watchlist app's **"Fetch filings"** form
(on `/voice`) proxies to it server-side; the token never reaches the browser.

## Endpoints
- `POST /scrape` — `Authorization: Bearer <SCRAPER_TOKEN>`, body
  `{"symbol":"VENUSPIPES","transcripts":4,"annual":true,"force":false}`.
  Scrapes `screener.in/company/<SYMBOL>/consolidated/`, extracts transcript (and
  optional annual-report) text with pypdf, uploads `<SYMBOL>/<name>.txt` to S3.
  Returns `{symbol, transcripts_found, uploaded[], skipped[], errors[]}`.
- `GET /healthz` — liveness + whether the bucket is configured.

## Run locally
```
VOICE_S3_BUCKET=simrat-company-docs SCRAPER_TOKEN=dev AWS_REGION=us-east-1 \
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... python app.py   # :8090
```

## Deploy to Fly (separate app)
```
cd scraper_service
fly launch --no-deploy --copy-config --name stock-scraper
fly secrets set SCRAPER_TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(24))") \
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
fly deploy
```
Then on the **main app** set the matching `SCRAPER_URL=https://stock-scraper.fly.dev`
and the SAME `SCRAPER_TOKEN` (`fly secrets set` on the main app), and set
`VOICE_S3_BUCKET=simrat-company-docs` so voice reads what this service uploads.

Scales to zero when idle (`min_machines_running = 0`); the first request after
idle incurs a cold start.
