#!/usr/bin/env python3
"""
MSTR Opportunistic Overlay — daily monitor
==========================================
Pulls Tastytrade market-metrics (IV percentile, IV rank, IV30, IV-HV diff/VRP)
plus price-based indicators (RSI14, MA20, MA50) and classifies the current
state under the v2 framework. Appends one row per run to a CSV and emits an
alert (optional webhook) only when the state CHANGES.

Auth: set env vars TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD.
Optional alert: set env var WEBHOOK_URL (Discord/Slack-compatible incoming webhook).
Optional mNAV context: set BTC_HOLDINGS (Strategy's disclosed BTC count; update occasionally).

Run:  python mstr_overlay.py          (normal daily run)
      python mstr_overlay.py --debug  (dumps every raw Tastytrade field once)
"""

import os, sys, json, csv, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from tastytrade import Session
from tastytrade.metrics import get_market_metrics

SYMBOL = "MSTR"
CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"

# ---- v2 framework thresholds (single place to tune) -------------------------
IVP_OPP   = 50    # IV percentile floor to be "opportune" at all
IVP_XTREME= 80    # IV percentile floor for the EXTREME put tier
IVP_XCALL = 90    # IV percentile floor for the manual extreme-call flag
RSI_PUT   = 45    # RSI at/below -> put-side posture
RSI_XPUT  = 30    # RSI at/below -> capitulation (extreme puts)
RSI_CALL  = 60    # RSI at/above -> call-side posture
RSI_XCALL = 65    # RSI at/above -> extreme-call flag
# Calls additionally require Close <= MA50 (the backtested trend gate).
# All active states require VRP (iv_hv_30_day_difference) > 0.

DTE_BY_STATE = {
    "EXTREME_PUTS": "30-45",
    "OPPORTUNE_PUTS": "30-38",
    "OPPORTUNE_CALLS": "30-45",
    "EXTREME_CALLS_FLAG": "30-45 (manual judgment only)",
    "NEUTRAL": "-",
    "RICH_NO_SIDE": "- (premium rich, no directional confirmation)",
    "UNKNOWN": "-",
}


def pct(x):
    """Normalize a percentile/rank that may arrive as 0-1 or 0-100."""
    if x is None:
        return float("nan")
    x = float(x)
    return x * 100 if x <= 1.0 else x


def get_tasty_metrics():
    user = os.environ["TASTYTRADE_USERNAME"]
    pw = os.environ["TASTYTRADE_PASSWORD"]
    session = Session(user, pw)
    m = get_market_metrics(session, [SYMBOL])[0]
    return m


def get_price_indicators():
    """RSI14 (Wilder), MA20, MA50, last close — from free price data."""
    px = yf.download(SYMBOL, period="200d", interval="1d",
                     auto_adjust=True, progress=False)["Close"].squeeze().dropna()
    close = px
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = (100 - 100/(1+rs)).iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1]
    last = close.iloc[-1]
    return dict(close=float(last), rsi=float(rsi),
                ma20=float(ma20), ma50=float(ma50),
                dist_ma20=float(last/ma20-1)*100,
                below_ma50=bool(last <= ma50))


def mnav_context(market_cap):
    """Optional slow overlay. Needs BTC_HOLDINGS env + live BTC price."""
    holdings = os.environ.get("BTC_HOLDINGS")
    if not holdings or market_cap in (None, 0):
        return None
    try:
        btc = yf.download("BTC-USD", period="2d", interval="1d",
                          auto_adjust=True, progress=False)["Close"].squeeze().dropna().iloc[-1]
        btc_nav = float(holdings) * float(btc)
        return float(market_cap) / btc_nav if btc_nav else None
    except Exception:
        return None


def classify(ivp, vrp, rsi, below_ma50):
    """v2 framework: 4 live states + 1 manual flag."""
    if np.isnan(ivp) or np.isnan(rsi):
        return "UNKNOWN"
    if ivp < IVP_OPP or vrp <= 0:          # low IV or no vol premium -> stand down
        return "NEUTRAL"
    # premium is rich (IVP>=50 and VRP>0) from here on
    if ivp >= IVP_XTREME and rsi <= RSI_XPUT:
        return "EXTREME_PUTS"
    if ivp >= IVP_XCALL and rsi >= RSI_XCALL and below_ma50:
        return "EXTREME_CALLS_FLAG"        # manual judgment only (historically never fires)
    if rsi <= RSI_PUT:
        return "OPPORTUNE_PUTS"
    if rsi >= RSI_CALL and below_ma50:     # trend gate: fade a bounce, never a parabola
        return "OPPORTUNE_CALLS"
    return "RICH_NO_SIDE"


def last_logged_state():
    if not CSV_PATH.exists():
        return None
    try:
        df = pd.read_csv(CSV_PATH)
        return df.iloc[-1]["state"] if len(df) else None
    except Exception:
        return None


def notify(text):
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        return
    try:
        import urllib.request
        data = json.dumps({"content": text, "text": text}).encode()  # Discord+Slack keys
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[warn] webhook failed: {e}", file=sys.stderr)


def main():
    debug = "--debug" in sys.argv
    m = get_tasty_metrics()

    if debug:
        print("=== RAW Tastytrade MarketMetricInfo for", SYMBOL, "===")
        print(m.model_dump_json(indent=2))

    ivp = pct(m.implied_volatility_percentile)
    ivr = pct(m.implied_volatility_index_rank)
    iv30 = float(m.implied_volatility_30_day) if m.implied_volatility_30_day is not None else float("nan")
    hv30 = float(m.historical_volatility_30_day) if m.historical_volatility_30_day is not None else float("nan")
    vrp = float(m.iv_hv_30_day_difference) if m.iv_hv_30_day_difference is not None else float("nan")
    beta = float(m.beta) if m.beta is not None else float("nan")
    mcap = float(m.market_cap) if m.market_cap is not None else None

    p = get_price_indicators()
    mnav = mnav_context(mcap)

    state = classify(ivp, vrp, p["rsi"], p["below_ma50"])

    row = {
        "date": dt.date.today().isoformat(),
        "state": state,
        "dte_reco": DTE_BY_STATE.get(state, "-"),
        "iv_percentile": round(ivp, 1),
        "iv_rank": round(ivr, 1),
        "iv30": round(iv30, 4),
        "hv30": round(hv30, 4),
        "vrp_iv_minus_hv": round(vrp, 4),
        "close": round(p["close"], 2),
        "rsi14": round(p["rsi"], 1),
        "ma20": round(p["ma20"], 2),
        "ma50": round(p["ma50"], 2),
        "dist_ma20_pct": round(p["dist_ma20"], 1),
        "below_ma50": p["below_ma50"],
        "beta": round(beta, 2),
        "mnav": round(mnav, 3) if mnav else "",
    }

    prev = last_logged_state()
    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)

    line = (f"{row['date']}  MSTR ${row['close']}  STATE={state}  "
            f"IVP={row['iv_percentile']}  VRP={row['vrp_iv_minus_hv']}  "
            f"RSI={row['rsi14']}  vs50dMA={'below' if p['below_ma50'] else 'above'}  "
            f"DTE={row['dte_reco']}")
    print(line)

    if prev is not None and prev != state and state not in ("UNKNOWN",):
        notify(f"⚠️ MSTR overlay STATE CHANGE: {prev} → {state}\n{line}")
        print(f"[ALERT] state changed: {prev} -> {state}")


if __name__ == "__main__":
    main()
