# Stock Watchlist & Alert System — Walkthrough

## What was built

A personal stock watchlist at `stock-watchlist/` that monitors stocks across global exchanges and sends proactive alerts for earnings dates, big price moves, volume spikes, and EMA crossovers.

## Architecture

```
stock-watchlist/
├── main.py              # Entry point — runs web UI + background scheduler
├── config.py            # Exchange mappings, thresholds, Telegram config
├── server.py            # Flask REST API (CRUD watchlist + alert history)
├── notifier.py          # Telegram push + alert dedup + logging
├── alerts/
│   ├── earnings.py      # Yahoo Finance earnings date proximity
│   └── price_action.py  # Daily moves, volume spikes, EMA crossovers
├── templates/
│   └── index.html       # Dark-themed web UI
├── watchlist.json       # Your tracked stocks
├── alert_log.json       # Alert history (capped at 500)
└── requirements.txt     # yfinance, flask, apscheduler, requests
```

## Alert Types

| Type | Trigger | Example |
|------|---------|---------|
| Earnings imminent | ≤1 day | ⚡ RFIL (NASDAQ) — earnings TOMORROW (Apr 01) |
| Earnings soon | ≤7 days | 📅 ACMESOLAR (NSE) — earnings in 5 days (Apr 05) |
| Big daily move | ≥±5% | 🔥 TSEM (NASDAQ) up +8.2% today ($42 → $45.45) |
| Volume spike | ≥2× 20d avg | 📊 ISSC (NASDAQ) — volume 3.1× average today |
| EMA bullish cross | EMA10 > EMA30 | 🚀 GRAVITA (NSE): bullish EMA crossover |
| EMA breakdown | EMA10 < EMA30 | ⚠️ HDFCBANK (NSE): bearish EMA breakdown |

## Testing

Added 3 stocks (RFIL/NASDAQ, ACMESOLAR/NSE, TSEM/NASDAQ) via the web UI and verified all components:

![Watchlist with 3 tracked stocks across 2 exchanges](/Users/simrat/.gemini/antigravity/brain/5b6117ec-bbc2-46b3-b3db-c4c30df97c9a/watchlist_with_stocks_1774777644910.png)

![7 real alerts fired — price moves, volume spikes, and EMA crossover with live Yahoo Finance data](/Users/simrat/.gemini/antigravity/brain/5b6117ec-bbc2-46b3-b3db-c4c30df97c9a/recent_alerts_verification_1774777865526.png)

![UI interaction recording](/Users/simrat/.gemini/antigravity/brain/5b6117ec-bbc2-46b3-b3db-c4c30df97c9a/alerts_proof_1774777839683.webp)

## How to run

```bash
cd ~/Desktop/stock-watchlist
source venv/bin/activate
python main.py
```

Open **http://localhost:8088** to manage your watchlist.

## Setting up Telegram alerts

1. Open Telegram, search for **@BotFather**, send `/newbot`
2. Copy the bot token
3. Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your chat ID
4. Set environment variables:

```bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

Without Telegram configured, alerts print to the console.
