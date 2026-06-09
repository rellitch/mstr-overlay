#!/usr/bin/env python3
"""
MSTR Opportunistic Overlay - daily monitor (Yahoo data, no brokerage API)
=========================================================================
Computes everything from Yahoo (reachable from GitHub's runners), since
Tastytrade's API blocks cloud/datacenter IPs. Signals:
  - Vol percentile  : trailing 252-day percentile of 30-day realized vol
                      (this is exactly the basis the backtest used)
  - VRP             : chain-derived IV30 minus realized HV30, in vol points
  - RSI(14), MA20, MA50 : from price
Classifies the v2 state, logs one row per trading day (in place), and
optionally alerts on a state change. No API keys required.

Optional env vars:
  WEBHOOK_URL   - Discord/Slack incoming webhook for state-change alerts
  BTC_HOLDINGS  - Strategy's BTC count, for mNAV context (needs market cap; left blank here)

Run:  python mstr_overlay.py            (normal run; auto-runs on GitHub every 30 min)
"""

import os, sys, csv, time, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import requests

SYMBOL = "MSTR"
CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"

# ---- v2 framework thresholds (single place to tune) -------------------------
IVP_OPP, IVP_XTREME, IVP_XCALL = 50, 80, 90
RSI_PUT, RSI_XPUT, RSI_CALL, RSI_XCALL = 45, 30, 60, 65
# Calls also require Close <= MA50 (trend gate). Active states need VRP > 0 when VRP is known.

DTE_BY_STATE = {
    "EXTREME_PUTS": "30-45",
    "OPPORTUNE_PUTS": "30-38",
    "OPPORTUNE_CALLS": "30-45",
    "EXTREME_CALLS_FLAG": "30-45 (manual judgment only)",
    "NEUTRAL": "-",
    "RICH_NO_SIDE": "- (premium rich, no directional confirmation)",
    "UNKNOWN": "-",
}


def get_price_frame():
    """~2y of daily closes (enough for a 252-day percentile + the moving averages)."""
    last_err = None
    for _ in range(3):
        try:
            px = yf.download(SYMBOL, period="2y", interval="1d",
                             auto_adjust=True, progress=False)["Close"].squeeze().dropna()
            if len(px) > 260:
                return px
        except Exception as e:
            last_err = e
        time.sleep(5)
    raise ConnectionError(f"price data unavailable: {last_err}")


def chain_iv30(spot):
    """Best-effort constant-maturity 30-day ATM IV (percent) from the Yahoo chain; NaN if unavailable."""
    try:
        t = yf.Ticker(SYMBOL)
        now = pd.Timestamp.now("UTC").tz_localize(None)
        rows = []
        for e in t.options:
            dte = (pd.Timestamp(e) - now).days
            if 7 <= dte <= 80:
                ch = t.option_chain(e)
                df = pd.concat([ch.calls, ch.puts])
                df = df[(df["impliedVolatility"] > 0.01) & (df["impliedVolatility"] < 5)]
                if len(df) < 3:
                    continue
                df = df.assign(d=(df["strike"] - spot).abs())
                rows.append((dte, float(np.average(df.nsmallest(6, "d")["impliedVolatility"]))))
        if len(rows) < 2:
            return float("nan")
        r = pd.DataFrame(rows, columns=["dte", "iv"]).sort_values("dte")
        below, above = r[r.dte <= 30], r[r.dte >= 30]
        if len(below) and len(above):
            lo, hi = below.iloc[-1], above.iloc[0]
            w = 0 if hi.dte == lo.dte else (30 - lo.dte) / (hi.dte - lo.dte)
            iv = lo.iv + w * (hi.iv - lo.iv)
        else:
            iv = r.iloc[(r.dte - 30).abs().argmin()].iv
        return float(iv * 100)
    except Exception:
        return float("nan")


def compute_metrics():
    px = get_price_frame()
    ret = np.log(px).diff()
    rv30_series = ret.rolling(30).std() * np.sqrt(252) * 100          # percent
    rv30 = float(rv30_series.iloc[-1])
    # trailing 252-day percentile of RV30 (the backtest's trigger basis)
    win = rv30_series.dropna().tail(252)
    ivp = float((win.iloc[:-1] < win.iloc[-1]).mean() * 100) if len(win) > 30 else float("nan")

    delta = px.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    al = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rsi = float((100 - 100/(1+(ag/al))).iloc[-1])
    ma20 = float(px.rolling(20).mean().iloc[-1])
    ma50 = float(px.rolling(50).mean().iloc[-1])
    last = float(px.iloc[-1])

    iv30 = chain_iv30(last)
    vrp = (iv30 - rv30) if not np.isnan(iv30) else float("nan")        # vol points

    return dict(close=last, rsi=rsi, ma20=ma20, ma50=ma50,
                dist_ma20=(last/ma20-1)*100, below_ma50=bool(last <= ma50),
                rv30=rv30, iv30=iv30, vrp=vrp, ivp=ivp)


def classify(ivp, vrp, rsi, below_ma50):
    if np.isnan(ivp) or np.isnan(rsi):
        return "UNKNOWN"
    if ivp < IVP_OPP:
        return "NEUTRAL"
    if (not np.isnan(vrp)) and vrp <= 0:     # known negative VRP -> stand down
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


def load_rows():
    if not CSV_PATH.exists():
        return []
    try:
        with open(CSV_PATH, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def save_rows(rows, fieldnames):
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def notify(text):
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"content": text, "text": text}, timeout=15)
    except Exception as e:
        print(f"[warn] webhook failed: {e}", file=sys.stderr)


def main():
    try:
        m = compute_metrics()
    except Exception as e:
        # Could not get price data this run; skip without crashing. The dashboard's
        # staleness banner makes any freeze visible, so this won't masquerade as live.
        print(f"[skip] market data unavailable this run ({type(e).__name__}: {e}); no update written.")
        return

    state = classify(m["ivp"], m["vrp"], m["rsi"], m["below_ma50"])
    row = {
        "date": dt.date.today().isoformat(),
        "state": state,
        "dte_reco": DTE_BY_STATE.get(state, "-"),
        "iv_percentile": round(m["ivp"], 1) if not np.isnan(m["ivp"]) else "",
        "vrp_iv_minus_hv": round(m["vrp"], 1) if not np.isnan(m["vrp"]) else "",
        "iv30": round(m["iv30"], 1) if not np.isnan(m["iv30"]) else "",
        "hv30": round(m["rv30"], 1),
        "close": round(m["close"], 2),
        "rsi14": round(m["rsi"], 1),
        "ma20": round(m["ma20"], 2),
        "ma50": round(m["ma50"], 2),
        "dist_ma20_pct": round(m["dist_ma20"], 1),
        "below_ma50": m["below_ma50"],
        "mnav": "",
    }

    rows = load_rows()
    prev = rows[-1]["state"] if rows else None
    if rows and rows[-1].get("date") == row["date"]:
        rows[-1] = row
    else:
        rows.append(row)
    save_rows(rows, list(row.keys()))

    line = (f"{row['date']}  MSTR ${row['close']}  STATE={state}  "
            f"VolPct={row['iv_percentile']}  VRP={row['vrp_iv_minus_hv']}  "
            f"RSI={row['rsi14']}  vs50dMA={'below' if m['below_ma50'] else 'above'}  "
            f"DTE={row['dte_reco']}")
    print(line)

    if prev is not None and prev != state and state != "UNKNOWN":
        notify(f"MSTR overlay STATE CHANGE: {prev} -> {state}\n{line}")
        print(f"[ALERT] state changed: {prev} -> {state}")


if __name__ == "__main__":
    main()
