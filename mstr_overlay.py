#!/usr/bin/env python3
"""
MSTR Opportunistic Overlay - daily monitor (Tastytrade OAuth version)
=====================================================================
Authenticates with Tastytrade via OAuth (client secret + refresh token), which
is the supported method for automation and is NOT blocked by device challenges.
Fetches market-metrics (IV percentile, IV rank, IV30, IV-HV/VRP), adds price
indicators (RSI14, MA20, MA50), classifies the v2 state, logs one row to a CSV,
and optionally alerts on a state change.

Required env vars (set as GitHub secrets):
  TASTYTRADE_CLIENT_SECRET   - from your Tastytrade OAuth application
  TASTYTRADE_REFRESH_TOKEN   - from a "Personal OAuth Grant" on that application
Optional:
  WEBHOOK_URL   - Discord/Slack incoming webhook for state-change alerts
  BTC_HOLDINGS  - Strategy's BTC count, for mNAV context

Run:  python mstr_overlay.py            (normal run; auto-runs every 30 min on GitHub)
      python mstr_overlay.py --debug    (also dumps the raw market-metrics JSON)
"""

import os, sys, json, csv, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

SYMBOL = "MSTR"
CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"
TT_BASE = "https://api.tastyworks.com"
UA = {"User-Agent": "mstr-overlay/1.0", "Accept": "application/json"}


def _make_session():
    """requests session that auto-retries transient network/5xx errors with backoff."""
    s = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=3,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET", "POST"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


SESSION = _make_session()
TIMEOUT = (10, 30)  # (connect, read) seconds

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
    if x in (None, ""):
        return float("nan")
    x = float(x)
    return x * 100 if x <= 1.0 else x


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def get_access_token():
    secret = os.environ["TASTYTRADE_CLIENT_SECRET"]
    refresh = os.environ["TASTYTRADE_REFRESH_TOKEN"]
    r = SESSION.post(f"{TT_BASE}/oauth/token",
                     json={"grant_type": "refresh_token",
                           "refresh_token": refresh,
                           "client_secret": secret},
                     headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise SystemExit(f"Tastytrade OAuth token request failed (HTTP {r.status_code}). "
                         f"Check the TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN secrets. "
                         f"Body: {r.text[:300]}")
    return r.json()["access_token"]


def get_tasty_metrics(debug=False):
    token = get_access_token()
    r = SESSION.get(f"{TT_BASE}/market-metrics",
                    params={"symbols": SYMBOL},
                    headers={**UA, "Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
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
    debug = "--debug" in sys.argv
    try:
        m = get_tasty_metrics(debug=debug)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        # transient network/reachability blip (e.g. Tastytrade slow to this runner).
        # Don't fail the run red; just skip this slot and try again next time.
        print(f"[skip] Tastytrade not reachable this run ({type(e).__name__}); "
              f"no update written. Will retry on the next scheduled run.")
        return

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

    rows = load_rows()
    prev = rows[-1]["state"] if rows else None
    if rows and rows[-1].get("date") == row["date"]:
        rows[-1] = row              # same day -> update in place (keeps one row per day)
    else:
        rows.append(row)            # new day -> add a row
    save_rows(rows, list(row.keys()))

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
