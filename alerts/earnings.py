"""
Earnings date alert module.
Fetches upcoming earnings dates from Yahoo Finance and checks proximity.
"""

import datetime as dt
import pytz
import yfinance as yf
from datetime import datetime, timezone
from config import (EARNINGS_WARN_DAYS, EARNINGS_IMMINENT_DAYS, EXCHANGE_SUFFIXES,
                    ALERT_SCHEDULE_TZ)


def get_yahoo_ticker(ticker: str, exchange: str) -> str:
    """Convert ticker + exchange to Yahoo Finance format."""
    suffix = EXCHANGE_SUFFIXES.get(exchange.upper(), "")
    return f"{ticker}{suffix}"


def _earnings_day(value):
    """Yahoo's date / datetime / ISO string as a plain calendar date, or None.

    A calendar date is what the alert is really about: reporting is announced
    for a day, not an instant, so counting elapsed 24-hour periods towards it
    lands a day short for most of the day.
    """
    # datetime before date — every datetime is also a date.
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _proximity(days_until: int) -> str:
    if days_until == 0:
        return "TODAY"
    if days_until == 1:
        return "TOMORROW"
    return f"in {days_until} day{'' if days_until == 1 else 's'}"


def check_earnings(watchlist: list[dict]) -> list[dict]:
    """
    Check each stock in the watchlist for upcoming earnings.
    Returns a list of alert dicts.
    """
    alerts = []
    now = datetime.now(timezone.utc)
    # "Days until" is counted in the same timezone the alert schedule runs in,
    # so "today" on the dashboard means the reader's today.
    today = now.astimezone(pytz.timezone(ALERT_SCHEDULE_TZ)).date()

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
                earnings_day = _earnings_day(ed)
                if earnings_day is None:
                    continue

                days_until = (earnings_day - today).days

                if days_until < 0:
                    continue

                when = _proximity(days_until)
                on = earnings_day.strftime("%b %d")
                if days_until <= EARNINGS_IMMINENT_DAYS:
                    alerts.append({
                        "type": "earnings_imminent",
                        "ticker": ticker,
                        "exchange": exchange,
                        "message": f"⚡ {ticker} ({exchange}) — earnings {when} ({on})",
                        "days_until": days_until,
                        "timestamp": now.isoformat(),
                    })
                elif days_until <= EARNINGS_WARN_DAYS:
                    alerts.append({
                        "type": "earnings_soon",
                        "ticker": ticker,
                        "exchange": exchange,
                        "message": f"📅 {ticker} ({exchange}) — earnings {when} ({on})",
                        "days_until": days_until,
                        "timestamp": now.isoformat(),
                    })
                break  # Only alert on the nearest earnings date

        except Exception as e:
            print(f"[earnings] Error checking {ticker} ({exchange}): {e}")

    return alerts
