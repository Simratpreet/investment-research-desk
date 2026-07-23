# Stock Watchlist & Alert System

## Goal

A personal stock watchlist where you add tickers (any exchange globally), and it **proactively alerts you** when:
- 📅 **Earnings date is approaching** (e.g. 7 days, 1 day before)
- 📈 **Significant price action** (big daily moves, volume spikes, breakouts)

## Architecture

```
stock-watchlist/
├── main.py              # Background daemon — runs checks every hour
├── watchlist.json       # Your tracked stocks [{ticker, exchange, added_date, notes}]
├── config.py            # Alert thresholds, Telegram bot token
├── alerts/
│   ├── earnings.py      # Fetches next earnings date, checks proximity
│   └── price_action.py  # Detects % moves, volume spikes, EMA breakouts
├── notifier.py          # Sends alerts via Telegram (or email/desktop)
├── server.py            # Tiny web UI to add/remove tickers
├── requirements.txt
└── templates/
    └── index.html       # Simple UI to manage watchlist
```

### How It Works

1. **You add tickers** via a simple web UI (`localhost:8080`) — just type ticker + exchange (e.g. `RFIL / NASDAQ`, `ACMESOLAR / NSE`)
2. **A background loop** runs every hour and checks:
   - Yahoo Finance for next earnings date → alerts if ≤7 days away
   - Daily price change → alerts if |move| ≥ 5%
   - Volume vs. 20-day average → alerts if volume ≥ 2× average
   - Weekly EMA 10 vs EMA 30 crossover (aligns with your Stage 2 methodology)
3. **Alerts are pushed** to you via **Telegram bot** (instant, works on phone)

### Alert Types

| Alert | Trigger | Example |
|-------|---------|---------|
| Earnings Soon | ≤7 days to earnings | "⚡ RFIL earnings in 3 days (Apr 1)" |
| Big Move | Daily change ≥ ±5% | "🔥 ACMESOLAR up +8.2% today (₹452 → ₹489)" |
| Volume Spike | Volume ≥ 2× 20d avg | "📊 ISSC volume 3.1× average today" |
| EMA Crossover | Weekly EMA 10 crosses EMA 30 | "🚀 GRAVITA: bullish EMA crossover" |
| EMA Breakdown | Weekly EMA 10 drops below EMA 30 | "⚠️ HDFCBANK: bearish EMA breakdown" |

### Data Sources

- **Yahoo Finance** (via `yfinance`) — prices, volume, earnings dates. Works for NASDAQ, NSE, LSE, XETRA, TSX, SGX.
- Ticker format: `RFIL` (NASDAQ), `ACMESOLAR.NS` (NSE), `VOD.L` (LSE) — the system auto-appends the suffix based on exchange.

---

## Notification Channel: Telegram

> [!IMPORTANT]
> Telegram is the simplest alert channel — free, instant, works on phone & desktop. You create a bot via @BotFather (takes 30 seconds), paste the token into `config.py`, and you're done. No email server setup, no push notification infra.

**Alternative options** (can add later):
- Desktop notifications (macOS `osascript`)
- Email via Gmail SMTP
- Discord webhook

---

## Web UI (Minimal)

A single-page UI at `localhost:8080` with:
- **Add stock**: ticker input + exchange dropdown (NASDAQ, NSE, LSE, XETRA, TSX, SGX)
- **Watchlist table**: all tracked stocks with current price, next earnings date, last alert
- **Remove stock**: one-click delete
- **Alert history**: recent alerts log

---

## Questions

1. **Telegram OK for alerts?** If you already use Telegram, this is the easiest path. Otherwise I can use macOS desktop notifications or email.

2. **Alert thresholds** — are these defaults reasonable?
   - Earnings: alert at 7 days and 1 day before
   - Price move: ≥ 5% daily change
   - Volume: ≥ 2× 20-day average

3. **How often should it check?** Hourly during market hours seems right, but I can make it configurable.
