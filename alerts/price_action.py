"""
Price action alert module.
Detects significant daily moves, volume spikes, and EMA crossovers.
"""

import yfinance as yf
import numpy as np
from datetime import datetime, timezone
from config import (
    PRICE_MOVE_THRESHOLD,
    VOLUME_SPIKE_MULTIPLIER,
    EXCHANGE_SUFFIXES,
)


def get_yahoo_ticker(ticker: str, exchange: str) -> str:
    """Convert ticker + exchange to Yahoo Finance format."""
    suffix = EXCHANGE_SUFFIXES.get(exchange.upper(), "")
    return f"{ticker}{suffix}"


def _compute_ema(series, span):
    """Compute exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def check_price_action(watchlist: list[dict]) -> list[dict]:
    """
    Check each stock for significant price action.
    Returns a list of alert dicts.
    """
    alerts = []
    now = datetime.now(timezone.utc)

    for stock in watchlist:
        ticker = stock["ticker"]
        exchange = stock["exchange"]
        yf_ticker = get_yahoo_ticker(ticker, exchange)

        try:
            yf_stock = yf.Ticker(yf_ticker)

            # --- Daily data for price move & volume spike ---
            daily = yf_stock.history(period="3mo", interval="1d")
            if daily.empty or len(daily) < 2:
                continue

            latest = daily.iloc[-1]
            prev = daily.iloc[-2]

            current_price = latest["Close"]
            prev_close = prev["Close"]
            daily_change_pct = ((current_price - prev_close) / prev_close) * 100

            currency = ""
            try:
                info = yf_stock.info
                currency = info.get("currency", "")
            except Exception:
                pass

            # 1. Big daily move
            if abs(daily_change_pct) >= PRICE_MOVE_THRESHOLD:
                direction = "up" if daily_change_pct > 0 else "down"
                emoji = "🔥" if daily_change_pct > 0 else "🔻"
                alerts.append({
                    "type": "big_move",
                    "ticker": ticker,
                    "exchange": exchange,
                    "message": (
                        f"{emoji} {ticker} ({exchange}) {direction} "
                        f"{daily_change_pct:+.1f}% today "
                        f"({currency}{prev_close:.2f} → {currency}{current_price:.2f})"
                    ),
                    "change_pct": round(daily_change_pct, 2),
                    "price": round(current_price, 2),
                    "timestamp": now.isoformat(),
                })

            # 2. Volume spike
            if len(daily) >= 21:
                avg_volume_20d = daily["Volume"].iloc[-21:-1].mean()
                current_volume = latest["Volume"]

                if avg_volume_20d > 0:
                    volume_ratio = current_volume / avg_volume_20d

                    if volume_ratio >= VOLUME_SPIKE_MULTIPLIER:
                        alerts.append({
                            "type": "volume_spike",
                            "ticker": ticker,
                            "exchange": exchange,
                            "message": (
                                f"📊 {ticker} ({exchange}) — volume "
                                f"{volume_ratio:.1f}× average today "
                                f"({int(current_volume):,} vs avg {int(avg_volume_20d):,})"
                            ),
                            "volume_ratio": round(volume_ratio, 1),
                            "timestamp": now.isoformat(),
                        })

            # --- Weekly data for EMA crossover ---
            weekly = yf_stock.history(period="1y", interval="1wk")
            if weekly.empty or len(weekly) < 31:
                continue

            ema10 = _compute_ema(weekly["Close"], 10)
            ema30 = _compute_ema(weekly["Close"], 30)

            # Check for crossover in the last week
            curr_above = ema10.iloc[-1] > ema30.iloc[-1]
            prev_above = ema10.iloc[-2] > ema30.iloc[-2]

            if curr_above and not prev_above:
                alerts.append({
                    "type": "ema_crossover_bullish",
                    "ticker": ticker,
                    "exchange": exchange,
                    "message": (
                        f"🚀 {ticker} ({exchange}) — bullish EMA crossover! "
                        f"EMA10 ({ema10.iloc[-1]:.2f}) crossed above EMA30 ({ema30.iloc[-1]:.2f})"
                    ),
                    "timestamp": now.isoformat(),
                })
            elif not curr_above and prev_above:
                alerts.append({
                    "type": "ema_crossover_bearish",
                    "ticker": ticker,
                    "exchange": exchange,
                    "message": (
                        f"⚠️ {ticker} ({exchange}) — bearish EMA breakdown! "
                        f"EMA10 ({ema10.iloc[-1]:.2f}) dropped below EMA30 ({ema30.iloc[-1]:.2f})"
                    ),
                    "timestamp": now.isoformat(),
                })

        except Exception as e:
            print(f"[price_action] Error checking {ticker} ({exchange}): {e}")

    return alerts
