"""
Earnings date alert module.
Fetches upcoming earnings dates from Yahoo Finance and checks proximity.
"""

import yfinance as yf
from datetime import datetime, timezone
from config import EARNINGS_WARN_DAYS, EARNINGS_IMMINENT_DAYS, EXCHANGE_SUFFIXES


def get_yahoo_ticker(ticker: str, exchange: str) -> str:
    """Convert ticker + exchange to Yahoo Finance format."""
    suffix = EXCHANGE_SUFFIXES.get(exchange.upper(), "")
    return f"{ticker}{suffix}"


def check_earnings(watchlist: list[dict]) -> list[dict]:
    """
    Check each stock in the watchlist for upcoming earnings.
    Returns a list of alert dicts.
    """
    alerts = []
    now = datetime.now(timezone.utc)

    for stock in watchlist:
        ticker = stock["ticker"]
        exchange = stock["exchange"]
        yf_ticker = get_yahoo_ticker(ticker, exchange)

        try:
            info = yf.Ticker(yf_ticker)
            calendar = info.calendar

            if calendar is None or len(calendar) == 0:
                continue

            # calendar can be a dict with 'Earnings Date' key (list of dates)
            earnings_dates = None

            if isinstance(calendar, dict):
                earnings_dates = calendar.get("Earnings Date")
            elif hasattr(calendar, "get"):
                earnings_dates = calendar.get("Earnings Date")

            if not earnings_dates:
                continue

            # Take the nearest future earnings date
            for ed in earnings_dates if isinstance(earnings_dates, list) else [earnings_dates]:
                import datetime as dt_mod
                if isinstance(ed, dt_mod.date) and not isinstance(ed, datetime):
                    # Pure date object — convert to tz-aware datetime
                    earnings_date = datetime.combine(ed, datetime.min.time(), tzinfo=timezone.utc)
                elif isinstance(ed, datetime):
                    earnings_date = ed
                    if earnings_date.tzinfo is None:
                        earnings_date = earnings_date.replace(tzinfo=timezone.utc)
                elif isinstance(ed, str):
                    earnings_date = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc)
                else:
                    continue

                days_until = (earnings_date - now).days

                if days_until < 0:
                    continue

                if days_until <= EARNINGS_IMMINENT_DAYS:
                    alerts.append({
                        "type": "earnings_imminent",
                        "ticker": ticker,
                        "exchange": exchange,
                        "message": f"⚡ {ticker} ({exchange}) — earnings TOMORROW ({earnings_date.strftime('%b %d')})",
                        "days_until": days_until,
                        "timestamp": now.isoformat(),
                    })
                elif days_until <= EARNINGS_WARN_DAYS:
                    alerts.append({
                        "type": "earnings_soon",
                        "ticker": ticker,
                        "exchange": exchange,
                        "message": f"📅 {ticker} ({exchange}) — earnings in {days_until} days ({earnings_date.strftime('%b %d')})",
                        "days_until": days_until,
                        "timestamp": now.isoformat(),
                    })
                break  # Only alert on the nearest earnings date

        except Exception as e:
            print(f"[earnings] Error checking {ticker} ({exchange}): {e}")

    return alerts
