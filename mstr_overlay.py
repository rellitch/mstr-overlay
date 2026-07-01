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

Run:  python mstr_overlay.py            (normal run; on GitHub the cron is best-effort
                                         and is often delayed/skipped, especially at the
                                         US open — force a run from the Actions tab when needed)
"""

import os, sys, csv, json, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
import requests

SYMBOL = "MSTR"
CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"
SNAP_PATH = Path(__file__).parent / "live_snapshot.json"   # provisional intraday read for the dashboard

# ---- v2 framework thresholds (single place to tune) -------------------------
IVP_OPP, IVP_XTREME, IVP_XCALL = 50, 80, 90
RSI_PUT, RSI_XPUT, RSI_CALL, RSI_XCALL = 45, 30, 60, 65
VRP_DEADBAND = -2.0   # vol points. Ignore small negative VRP within the deadband of 0 to
                      # avoid daily on/off chatter at the zero line; stand down only below this.
# Calls also require Close <= MA50 (trend gate). Active states need VRP > VRP_DEADBAND when known.

ET = ZoneInfo("America/New_York")   # for end-of-day (last completed session) anchoring

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


def _frame_metrics(px):
    """Price-derived metrics from a daily close series (no chain/mNAV)."""
    ret = np.log(px).diff()
    rv30_series = ret.rolling(30).std() * np.sqrt(252) * 100          # percent
    rv30 = float(rv30_series.iloc[-1])
    # Trailing 252-day percentile of RV30 (the backtest's trigger basis), via linear
    # interpolation of the empirical CDF rather than a raw count/251. The discrete count
    # snapped to ~0.4% steps and looked "frozen" in flat-vol stretches; interpolation lets
    # it vary smoothly with small RV30 moves. Same logic, just finer resolution.
    win = rv30_series.dropna().tail(252)
    if len(win) > 30:
        prior = np.sort(win.iloc[:-1].to_numpy())
        ivp = float(np.interp(win.iloc[-1], prior, np.linspace(0, 100, len(prior))))
    else:
        ivp = float("nan")

    delta = px.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    al = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rsi = float((100 - 100/(1+(ag/al))).iloc[-1])
    ma20 = float(px.rolling(20).mean().iloc[-1])
    ma50 = float(px.rolling(50).mean().iloc[-1])
    last = float(px.iloc[-1])
    return dict(close=last, rsi=rsi, ma20=ma20, ma50=ma50,
                dist_ma20=(last/ma20-1)*100, below_ma50=bool(last <= ma50),
                rv30=rv30, ivp=ivp, asof=px.index[-1].date().isoformat())


def _attach_iv(m, iv30):
    m = dict(m)
    m["iv30"] = iv30
    m["vrp"] = (iv30 - m["rv30"]) if not np.isnan(iv30) else float("nan")   # vol points
    return m


def compute_metrics():
    """Compute BOTH anchors in one shot, sharing a single chain pull:
      eod  = last COMPLETED session -> canonical, written to the log (reproducible,
             backtest-consistent, immune to intraday run timing).
      live = includes today's in-progress bar -> provisional intraday read for the
             dashboard, so an intraday decision isn't made off a stale prior close.
    If the regular session is already closed (or there is no live bar today, e.g. a
    holiday/weekend), `live` is `eod` and `partial` is False. Returns (eod, live, partial)."""
    full = get_price_frame()
    now = dt.datetime.now(ET)
    partial = (full.index[-1].date() == now.date()) and (now.time() < dt.time(16, 15))
    eod_px = full.iloc[:-1] if partial else full
    iv30 = chain_iv30(float(full.iloc[-1]))            # current chain (one network call, shared)
    eod = _attach_iv(_frame_metrics(eod_px), iv30)
    live = _attach_iv(_frame_metrics(full), iv30) if partial else eod
    return eod, live, partial


def strategy_mnav():
    """Official mNAV from Strategy's own site API: EV / BTC NAV. Returns (mnav, btc_price) or (None, None)."""
    try:
        h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        kpi = requests.get("https://api.strategy.com/btc/mstrKpiData", headers=h, timeout=20).json()
        btc = requests.get("https://api.strategy.com/btc/bitcoinKpis", headers=h, timeout=20).json()
        ev = float(str(kpi[0]["entVal"]).replace(",", ""))
        nav = float(btc["results"]["btcNavNumber"])
        btc_px = float(btc["results"]["ufPrice"])
        return (ev / nav if nav else None), btc_px
    except Exception:
        return None, None


def classify(ivp, vrp, rsi, below_ma50):
    if np.isnan(ivp) or np.isnan(rsi):
        return "UNKNOWN"
    if ivp < IVP_OPP:
        return "NEUTRAL"
    if (not np.isnan(vrp)) and vrp <= VRP_DEADBAND:   # clearly negative VRP -> stand down
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
        eod, live, partial = compute_metrics()
    except Exception as e:
        # Could not get price data this run; skip without crashing. The dashboard's
        # staleness banner makes any freeze visible, so this won't masquerade as live.
        print(f"[skip] market data unavailable this run ({type(e).__name__}: {e}); no update written.")
        return

    # --- canonical end-of-day row (written to the log; one per completed session) ---
    state = classify(eod["ivp"], eod["vrp"], eod["rsi"], eod["below_ma50"])
    mnav, btc_px = strategy_mnav()
    row = {
        "date": eod["asof"],                # last completed session (EOD-anchored), not wall-clock today
        "state": state,
        "dte_reco": DTE_BY_STATE.get(state, "-"),
        "iv_percentile": round(eod["ivp"], 1) if not np.isnan(eod["ivp"]) else "",
        "vrp_iv_minus_hv": round(eod["vrp"], 1) if not np.isnan(eod["vrp"]) else "",
        "iv30": round(eod["iv30"], 1) if not np.isnan(eod["iv30"]) else "",
        "hv30": round(eod["rv30"], 1),
        "close": round(eod["close"], 2),
        "rsi14": round(eod["rsi"], 1),
        "ma20": round(eod["ma20"], 2),
        "ma50": round(eod["ma50"], 2),
        "dist_ma20_pct": round(eod["dist_ma20"], 1),
        "below_ma50": eod["below_ma50"],
        "mnav": round(mnav, 3) if mnav else "",
        "btc_price": round(btc_px, 0) if btc_px else "",
    }

    rows = load_rows()
    prev = rows[-1]["state"] if rows else None
    if rows and rows[-1].get("date") == row["date"]:
        # This completed session already has a row. Price/vol fields are reproducible; the
        # chain-derived ones (IV30/VRP/mNAV/BTC) are not, so freeze them at their first
        # recorded values. This covers ALL later same-date writes -- including next-morning
        # intraday runs whose EOD anchor still points at this (now-closed) prior session --
        # so a completed session's numbers are never revised by a later chain read. State is
        # then recomputed from the frozen VRP so it stays consistent with the logged row.
        existing = rows[-1]
        for k in ("iv30", "vrp_iv_minus_hv", "mnav", "btc_price"):
            if existing.get(k) not in (None, ""):
                row[k] = existing[k]
        fv = row["vrp_iv_minus_hv"]
        frozen_vrp = float(fv) if fv not in (None, "") else float("nan")
        state = classify(eod["ivp"], frozen_vrp, eod["rsi"], eod["below_ma50"])
        row["state"] = state
        row["dte_reco"] = DTE_BY_STATE.get(state, "-")
        rows[-1] = row
    else:
        rows.append(row)
    save_rows(rows, list(row.keys()))

    # --- provisional intraday snapshot (for the dashboard; NOT in the canonical log) ---
    # During a live session this reflects today's developing conditions so an intraday
    # decision isn't made off a stale prior close. After the close it equals the EOD row.
    live_state = classify(live["ivp"], live["vrp"], live["rsi"], live["below_ma50"])
    snap = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "provisional": bool(partial),
        "asof": live["asof"],
        "state": live_state,
        "dte_reco": DTE_BY_STATE.get(live_state, "-"),
        "iv_percentile": round(live["ivp"], 1) if not np.isnan(live["ivp"]) else None,
        "vrp_iv_minus_hv": round(live["vrp"], 1) if not np.isnan(live["vrp"]) else None,
        "iv30": round(live["iv30"], 1) if not np.isnan(live["iv30"]) else None,
        "hv30": round(live["rv30"], 1),
        "close": round(live["close"], 2),
        "rsi14": round(live["rsi"], 1),
        "below_ma50": live["below_ma50"],
    }
    SNAP_PATH.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    line = (f"{row['date']}  MSTR ${row['close']}  STATE={state}  "
            f"VolPct={row['iv_percentile']}  VRP={row['vrp_iv_minus_hv']}  "
            f"RSI={row['rsi14']}  vs50dMA={'below' if eod['below_ma50'] else 'above'}  "
            f"DTE={row['dte_reco']}")
    print(line)
    if partial:
        print(f"[live] provisional {live_state}  VolPct={snap['iv_percentile']} "
              f"VRP={snap['vrp_iv_minus_hv']} RSI={snap['rsi14']} close={snap['close']}")

    # Alerts fire on canonical (session-to-session) state changes only -> no intraday flapping.
    if prev is not None and prev != state and state != "UNKNOWN":
        notify(f"MSTR overlay STATE CHANGE: {prev} -> {state}\n{line}")
        print(f"[ALERT] state changed: {prev} -> {state}")


if __name__ == "__main__":
    main()
