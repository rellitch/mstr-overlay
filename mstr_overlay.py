#!/usr/bin/env python3
"""
MSTR Opportunistic Overlay - daily monitor (REST version)
=========================================================
Calls the Tastytrade REST API directly with your username/password to fetch
market-metrics (IV percentile, IV rank, IV30, IV-HV/VRP), adds price-based
indicators (RSI14, MA20, MA50), classifies the v2 state, logs one row to a CSV,
and (optionally) alerts on a state change. No Tastytrade SDK is used, so it is
unaffected by SDK version changes.

Auth: env vars TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD.
Optional alert: env var WEBHOOK_URL (Discord/Slack incoming webhook).
Optional mNAV context: env var BTC_HOLDINGS.

Run:  python mstr_overlay.py            (normal daily run)
      python mstr_overlay.py --debug    (also dumps the raw market-metrics JSON)
"""

import os, sys, json, csv, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import requests

SYMBOL = "MSTR"
CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"
TT_BASE = "https://api.tastyworks.com"
UA = {"User-Agent": "mstr-overlay/1.0", "Accept": "application/json"}

# ---- v2 framework thresholds (single place to tune) -------------------------
IVP_OPP, IVP_XTREME, IVP_XCALL = 50, 80, 90
RSI_PUT, RSI_XPUT, RSI_CALL, RSI_XCALL = 45, 30, 60, 65
# Calls also require Close <= MA50 (trend gate). All active states need VRP > 0.

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
    """Normalize a percentile/rank that may arrive as 0-1 or 0-100 (or a string)."""
    if x in (None, ""):
        return float("nan")
    x = float(x)
    return x * 100 if x <= 1.0 else x


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def get_tasty_metrics(debug=False):
    user = os.environ["TASTYTRADE_USERNAME"]
    pw = os.environ["TASTYTRADE_PASSWORD"]

    # 1) create a session -> session token
    r = requests.post(f"{TT_BASE}/sessions",
                      json={"login": user, "password": pw},
                      headers={**UA, "Content-Type": "application/json"}, timeout=30)
    if r.status_code >= 400:
        raise SystemExit(f"Tastytrade login failed (HTTP {r.status_code}). "
                         f"Check the TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD secrets. "
                         f"Body: {r.text[:300]}")
    token = r.json()["data"]["session-token"]

    # 2) fetch market metrics for the symbol
    r = requests.get(f"{TT_BASE}/market-metrics",
                     params={"symbols": SYMBOL},
                     headers={**UA, "Authorization": token}, timeout=30)
    r.raise_for_status()
    items = r.json().get("data", {}).get("items", [])
    if not items:
        raise SystemExit("No market-metrics returned for MSTR.")
    m = items[0]
    if debug:
        print("=== RAW Tastytrade market-metrics for", SYMBOL, "===")
        print(json.dumps(m, indent=2))
    return m


def get_price_indicators():
    px = yf.download(SYMBOL, period="200d", interval="1d",
                     auto_adjust=True, progress=False)["Close"].squeeze().dropna()
    delta = px.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rsi = (100 - 100/(1+(avg_gain/avg_loss))).iloc[-1]
    ma20 = px.rolling(20).mean().iloc[-1]
    ma50 = px.rolling(50).mean().iloc[-1]
    last = px.iloc[-1]
    return dict(close=float(last), rsi=float(rsi), ma20=float(ma20), ma50=float(ma50),
                dist_ma20=float(last/ma20-1)*100, below_ma50=bool(last <= ma50))


def mnav_context(market_cap):
    holdings = os.environ.get("BTC_HOLDINGS")
    if not holdings or not market_cap:
        return None
    try:
        btc = yf.download("BTC-USD", period="2d", interval="1d",
                          auto_adjust=True, progress=False)["Close"].squeeze().dropna().iloc[-1]
        nav = float(holdings) * float(btc)
        return float(market_cap) / nav if nav else None
    except Exception:
        return None


def classify(ivp, vrp, rsi, below_ma50):
    if np.isnan(ivp) or np.isnan(rsi):
        return "UNKNOWN"
    if ivp < IVP_OPP or vrp <= 0:
        return "NEUTRAL"
    if ivp >= IVP_XTREME and rsi <= RSI_XPUT:
        return "EXTREME_PUTS"
    if ivp >= IVP_XCALL and rsi >= RSI_XCALL and below_ma50:
        return "EXTREME_CALLS_FLAG"
    if rsi <= RSI_PUT:
        return "OPPORTUNE_PUTS"
    if rsi >= RSI_CALL and below_ma50:
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
        requests.post(url, json={"content": text, "text": text}, timeout=15)
    except Exception as e:
        print(f"[warn] webhook failed: {e}", file=sys.stderr)


def main():
    debug = "--debug" in sys.argv
    m = get_tasty_metrics(debug=debug)

    ivp = pct(m.get("implied-volatility-percentile"))
    ivr = pct(m.get("implied-volatility-index-rank"))
    iv30 = fnum(m.get("implied-volatility-30-day"))
    hv30 = fnum(m.get("historical-volatility-30-day"))
    vrp = fnum(m.get("iv-hv-30-day-difference"))
    beta = fnum(m.get("beta"))
    mcap = fnum(m.get("market-cap"))

    p = get_price_indicators()
    mnav = mnav_context(mcap if not np.isnan(mcap) else None)
    state = classify(ivp, vrp, p["rsi"], p["below_ma50"])

    row = {
        "date": dt.date.today().isoformat(),
        "state": state,
        "dte_reco": DTE_BY_STATE.get(state, "-"),
        "iv_percentile": round(ivp, 1) if not np.isnan(ivp) else "",
        "iv_rank": round(ivr, 1) if not np.isnan(ivr) else "",
        "iv30": round(iv30, 4) if not np.isnan(iv30) else "",
        "hv30": round(hv30, 4) if not np.isnan(hv30) else "",
        "vrp_iv_minus_hv": round(vrp, 4) if not np.isnan(vrp) else "",
        "close": round(p["close"], 2),
        "rsi14": round(p["rsi"], 1),
        "ma20": round(p["ma20"], 2),
        "ma50": round(p["ma50"], 2),
        "dist_ma20_pct": round(p["dist_ma20"], 1),
        "below_ma50": p["below_ma50"],
        "beta": round(beta, 2) if not np.isnan(beta) else "",
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

    if prev is not None and prev != state and state != "UNKNOWN":
        notify(f"MSTR overlay STATE CHANGE: {prev} -> {state}\n{line}")
        print(f"[ALERT] state changed: {prev} -> {state}")


if __name__ == "__main__":
    main()
