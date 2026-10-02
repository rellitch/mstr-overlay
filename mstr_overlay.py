#!/usr/bin/env python3
"""
MSTR Opportunistic Overlay - daily monitor (Yahoo data, no brokerage API)
=========================================================================
Computes everything from Yahoo (reachable from GitHub's runners), since
Tastytrade's API blocks cloud/datacenter IPs. Signals:
  - RV percentile   : trailing 252-day percentile of 30-day REALIZED vol
                      (this is exactly the basis the backtest used). It measures how
                      much MSTR is moving, NOT how expensive its options are -- logged as
                      `rv_percentile` (formerly the misnamed `iv_percentile`).
  - VRP             : chain-derived IV30 minus realized HV30, in vol points
  - RSI(14), MA20, MA50 : from price
Option-premium context (display only; does not change the state machine):
  - IV30 from bid/ask mid quotes (Black-Scholes), not Yahoo's last-trade IV
  - 25-delta skew (call IV - put IV): which side is priced richer
  - the target contracts actually being sold -- ~30-delta put at 30-45 DTE and
    ~10-delta covered call at 40-45 DTE -- with their mid, IV and annualized yield
Classifies the v2 put/neutral state, plus an orthogonal covered-call premium
tier (CALLS_GOOD_PREMIUM / CALLS_EXTREME_PREMIUM, keyed to premium richness --
not direction) and a MELT-UP RISK warning (overbought above the 50d MA = the
regime where sold calls historically get run over). Logs one row per completed
session and optionally alerts on state or tier changes. No API keys required.

Optional env vars:
  WEBHOOK_URL   - Discord/Slack incoming webhook for state-change alerts
  BTC_HOLDINGS  - Strategy's BTC count, for mNAV context (needs market cap; left blank here)

Run:  python mstr_overlay.py            (normal run; on GitHub the cron is best-effort
                                         and is often delayed/skipped, especially at the
                                         US open — force a run from the Actions tab when needed)
"""

import os, sys, csv, json, math, time, datetime as dt
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
IVP_OPP, IVP_XTREME = 50, 80
RSI_PUT, RSI_XPUT = 45, 30
VRP_DEADBAND = -2.0   # vol points. Ignore small negative VRP within the deadband of 0 to
                      # avoid daily on/off chatter at the zero line; stand down only below this.
# Put states need VRP > VRP_DEADBAND when VRP is known.

# ---- covered-call premium tiers (orthogonal to the put/neutral state) --------
# Backtest findings (2020-2026): the old directional call states never fired
# (OPPORTUNE_CALLS gated to 0 days; EXTREME_CALLS_FLAG was a near-contradiction),
# and RSI-overbought entries were the WORST days to sell calls in every IVP band.
# So call signaling is re-keyed to PREMIUM RICHNESS, and RSI is demoted to a
# melt-up *warning*. Tiers are for covered calls (shares owned), ~10-delta, 40-45 DTE.
CALL_GOOD_IVP, CALL_GOOD_VRP = 60, 0     # good premium: vol elevated & IV > realized
CALL_EXT_IVP, CALL_EXT_VRP = 75, 10      # extremely lucrative: fat VRP (IV pays 10+ pts over
                                         # realized) in an elevated-vol regime. VRP is the primary
                                         # richness axis: June-2026 capitulation printed VRP +18..+29
                                         # while interp IVP peaked at 88 -- an IVP>=90 requirement
                                         # would have missed the entire episode (the never-fires bug).
MELTUP_RSI = 60                          # warning: overbought above the 50d MA -> calls get run over

ET = ZoneInfo("America/New_York")   # for end-of-day (last completed session) anchoring

# ---- option-chain reads (premium context; display only) ----------------------
RISK_FREE = 0.04                 # annual rate for Black-Scholes IV/delta (MSTR pays no dividend)
PUT_DELTA, PUT_DTE = -0.30, (30, 45)     # the cash-secured put being sold
CALL_DELTA, CALL_DTE = 0.10, (40, 45)    # the covered call being sold
SKEW_DELTA = 0.25                # risk-reversal wing, read at the put-target expiry
MAX_REL_SPREAD = 0.5             # (ask-bid)/mid above this -> quote too wide to trust its mid

DTE_BY_STATE = {
    "EXTREME_PUTS": "30-45",
    "OPPORTUNE_PUTS": "30-38",
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


_erf = np.vectorize(math.erf, otypes=[float])


def _ncdf(x):
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))


def _d1(S, K, T, sig, r):
    return (np.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * np.sqrt(T))


def bs_price(S, K, T, sig, is_call, r=RISK_FREE):
    """European Black-Scholes price (vectorized over K/sig/is_call). Puts via parity."""
    d1 = _d1(S, K, T, sig, r)
    disc = K * np.exp(-r * T)
    call = S * _ncdf(d1) - disc * _ncdf(d1 - sig * np.sqrt(T))
    return np.where(is_call, call, call - S + disc)


def bs_delta(S, K, T, sig, is_call, r=RISK_FREE):
    nd1 = _ncdf(_d1(S, K, T, sig, r))
    return np.where(is_call, nd1, nd1 - 1.0)


def implied_vol(price, S, K, T, is_call, r=RISK_FREE, lo=0.01, hi=5.0):
    """Vectorized bisection for Black-Scholes IV (decimal). NaN where the price is
    missing or outside the [lo, hi] vol band (e.g. below intrinsic)."""
    price = np.asarray(price, dtype=float)
    K = np.asarray(K, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)
    a, b = np.full(price.shape, lo), np.full(price.shape, hi)
    for _ in range(60):
        m = 0.5 * (a + b)
        over = bs_price(S, K, T, m, is_call, r) > price
        b, a = np.where(over, m, b), np.where(over, a, m)
    ok = ((price > bs_price(S, K, T, np.full(price.shape, lo), is_call, r))
          & (price < bs_price(S, K, T, np.full(price.shape, hi), is_call, r)))
    return np.where(ok, 0.5 * (a + b), np.nan)


def _expiry_T(exp, now_et):
    """(years to the 16:00 ET expiry, calendar DTE) for an 'YYYY-MM-DD' expiry."""
    close = dt.datetime.combine(dt.date.fromisoformat(exp), dt.time(16, 0), tzinfo=ET)
    return (close - now_et).total_seconds() / (365.0 * 86400), (close.date() - now_et.date()).days


def _price_expiry(ch, spot, T):
    """One expiry's calls+puts with a mid price, IV and delta per strike. IV is solved
    from the bid/ask MID: Yahoo's own impliedVolatility is computed from lastPrice,
    which can be hours stale (or junk off-hours). Yahoo's IV is kept only as a
    fallback for strikes with no usable two-sided quote."""
    df = pd.concat([ch.calls.assign(is_call=True), ch.puts.assign(is_call=False)],
                   ignore_index=True)
    bid = df["bid"].fillna(0).to_numpy(float)
    ask = df["ask"].fillna(0).to_numpy(float)
    mid = (bid + ask) / 2
    quoted = (bid > 0) & (ask >= bid) & ((ask - bid) <= MAX_REL_SPREAD * mid)
    strike = df["strike"].to_numpy(float)
    is_call = df["is_call"].to_numpy(bool)
    iv = implied_vol(np.where(quoted, mid, np.nan), spot, strike, T, is_call)
    yiv = df["impliedVolatility"].to_numpy(float)
    iv = np.where(np.isfinite(iv), iv, np.where((yiv > 0.01) & (yiv < 5), yiv, np.nan))
    df = df.assign(mid=np.where(quoted, mid, df["lastPrice"].to_numpy(float)),
                   quoted=quoted, iv=iv)
    df = df[np.isfinite(df["iv"])]
    return df.assign(delta=bs_delta(spot, df["strike"].to_numpy(float), T,
                                    df["iv"].to_numpy(float), df["is_call"].to_numpy(bool)))


def _const_maturity(pts, T_target):
    """pts: [(T, iv)] sorted by T. Linear in total variance (iv^2 * T) between the
    bracketing expiries; nearest expiry if the target isn't bracketed."""
    below = [p for p in pts if p[0] <= T_target]
    above = [p for p in pts if p[0] >= T_target]
    if below and above:
        (t0, v0), (t1, v1) = below[-1], above[0]
        if t1 == t0:
            return v0
        w = (T_target - t0) / (t1 - t0)
        return math.sqrt((v0 ** 2 * t0 + w * (v1 ** 2 * t1 - v0 ** 2 * t0)) / T_target)
    return min(pts, key=lambda p: abs(p[0] - T_target))[1]


def _pick_expiry(frames, lo, hi):
    """The expiry inside [lo, hi] DTE nearest its middle; if none is inside (MSTR lists
    weeklies ~6 weeks out, then monthlies), the nearest expiry to that middle."""
    if not frames:
        return None
    target = (lo + hi) / 2
    inside = [e for e, (_, d, _) in frames.items() if lo <= d <= hi]
    return min(inside or list(frames), key=lambda e: abs(frames[e][1] - target))


def _target_contract(frames, want_delta, window, spot):
    """The strike nearest `want_delta` at the chosen expiry, with its credit and yield.
    Put yield is on the cash secured (strike); covered-call yield is on the shares (spot)."""
    e = _pick_expiry(frames, *window)
    if e is None:
        return None
    _, dte, df = frames[e]
    side = df[df["is_call"] == (want_delta > 0)]
    side = side[side["quoted"]] if side["quoted"].any() else side
    if side.empty or dte <= 0:
        return None
    r = side.loc[(side["delta"] - want_delta).abs().idxmin()]
    y = float(r["mid"]) / (spot if want_delta > 0 else float(r["strike"]))
    return dict(contract=f"{e} {float(r['strike']):g}{'C' if want_delta > 0 else 'P'}",
                dte=dte, mid=float(r["mid"]), iv=float(r["iv"]) * 100,
                delta=float(r["delta"]), yield_pct=y * 100, ann_pct=y * 365 / dte * 100)


def _iv_at_delta(side, want):
    s = side.sort_values("delta")
    if len(s) < 2 or not (s["delta"].min() <= want <= s["delta"].max()):
        return float("nan")
    return float(np.interp(want, s["delta"], s["iv"]))


def chain_read(spot):
    """Best-effort read of the live chain in ONE pass (each field NaN/None if unavailable):
      iv30  constant-maturity 30-day ATM IV (percent), variance-interpolated
      rr25  25-delta risk reversal, call IV minus put IV (vol pts) at the put-target
            expiry; > 0 means OTM calls are priced richer than OTM puts
      put   the ~30-delta put at 30-45 DTE     (dict from _target_contract)
      call  the ~10-delta call at 40-45 DTE    (dict from _target_contract)"""
    out = dict(iv30=float("nan"), rr25=float("nan"), put=None, call=None)
    try:
        t = yf.Ticker(SYMBOL)
        now = dt.datetime.now(ET)
        frames = {}
        for e in t.options:
            T, dte = _expiry_T(e, now)
            if 7 <= dte <= 80 and T > 0:
                try:
                    df = _price_expiry(t.option_chain(e), spot, T)
                except Exception:
                    continue
                if len(df) >= 3:
                    frames[e] = (T, dte, df)
        if not frames:
            return out
        atm = sorted((T, float(df.assign(d=(df["strike"] - spot).abs()).nsmallest(6, "d")["iv"].mean()))
                     for T, _, df in frames.values())
        if len(atm) >= 2:
            out["iv30"] = _const_maturity(atm, 30 / 365) * 100
        out["put"] = _target_contract(frames, PUT_DELTA, PUT_DTE, spot)
        out["call"] = _target_contract(frames, CALL_DELTA, CALL_DTE, spot)
        e = _pick_expiry(frames, *PUT_DTE)
        df = frames[e][2]
        c = _iv_at_delta(df[df["is_call"] & (df["strike"] >= spot)], SKEW_DELTA)
        p = _iv_at_delta(df[~df["is_call"] & (df["strike"] <= spot)], -SKEW_DELTA)
        out["rr25"] = (c - p) * 100
    except Exception as e:
        print(f"[warn] option chain read failed ({type(e).__name__}: {e})", file=sys.stderr)
    return out


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
        rvp = float(np.interp(win.iloc[-1], prior, np.linspace(0, 100, len(prior))))
    else:
        rvp = float("nan")

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
                rv30=rv30, rvp=rvp, asof=px.index[-1].date().isoformat())


def _attach_chain(m, chain):
    m = dict(m)
    iv30 = chain["iv30"]
    m["iv30"] = iv30
    m["vrp"] = (iv30 - m["rv30"]) if not np.isnan(iv30) else float("nan")   # vol points
    m["rr25"] = chain["rr25"]
    m["put"], m["call"] = chain["put"], chain["call"]
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
    chain = chain_read(float(full.iloc[-1]))            # current chain (one pass, shared)
    eod = _attach_chain(_frame_metrics(eod_px), chain)
    live = _attach_chain(_frame_metrics(full), chain) if partial else eod
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


def classify(rvp, vrp, rsi, below_ma50):
    """Put/neutral state machine. The old OPPORTUNE_CALLS / EXTREME_CALLS_FLAG
    branches are retired (0 fires in 5.9 backtested years; overbought entries were
    the worst call-selling days in every IVP band) -- covered-call opportunity now
    lives in call_tier()/meltup_risk() below. below_ma50 kept for signature compat.
    rvp is the realized-vol percentile (the IVP_* threshold names predate that label)."""
    if np.isnan(rvp) or np.isnan(rsi):
        return "UNKNOWN"
    if rvp < IVP_OPP:
        return "NEUTRAL"
    if (not np.isnan(vrp)) and vrp <= VRP_DEADBAND:   # clearly negative VRP -> stand down
        return "NEUTRAL"
    if rvp >= IVP_XTREME and rsi <= RSI_XPUT:
        return "EXTREME_PUTS"
    if rsi <= RSI_PUT:
        return "OPPORTUNE_PUTS"
    return "RICH_NO_SIDE"


def call_tier(rvp, vrp):
    """Covered-call premium tier, keyed to premium richness (NOT direction).
    Requires live VRP (real chain IV30 minus HV30) -- if the chain was unavailable
    this run, there is no honest richness read, so no tier. VRP >= 10 vol pts marks
    the extremely-lucrative regime seen at capitulation (June 2026 hit +18..+29 while
    IVP sat at 84-88). VRP thresholds are calibrated to the live log, NOT backtestable
    (no free historical IV) -- revisit as the log grows."""
    if np.isnan(rvp) or np.isnan(vrp):
        return ""
    if rvp >= CALL_EXT_IVP and vrp >= CALL_EXT_VRP:
        return "CALLS_EXTREME_PREMIUM"
    if rvp >= CALL_GOOD_IVP and vrp > CALL_GOOD_VRP:
        return "CALLS_GOOD_PREMIUM"
    return ""


def meltup_risk(rsi, below_ma50):
    """Warning flag (inverted role of the old call trigger): overbought ABOVE the
    50d MA is the regime where sold calls historically got run over (mean edge
    negative in every IVP band; worst episodes -85%..-160%). Not a trade signal --
    a 'size down / expect to roll or be assigned' caution for covered-call writers."""
    if np.isnan(rsi):
        return False
    return bool(rsi >= MELTUP_RSI and not below_ma50)


RENAMED_COLS = {"iv_percentile": "rv_percentile"}   # old log column -> new (always realized-vol based)
CHAIN_COLS = ("iv30", "vrp_iv_minus_hv", "mnav", "btc_price", "rr25_skew",
              "put_contract", "put_mid", "put_iv", "put_yield_pct", "put_ann_pct",
              "call_contract", "call_mid", "call_iv", "call_yield_pct", "call_ann_pct")


def load_rows():
    if not CSV_PATH.exists():
        return []
    try:
        with open(CSV_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    for r in rows:
        for old, new in RENAMED_COLS.items():
            if old in r:
                r.setdefault(new, r.pop(old))
    return rows


def _dedup_sorted(rows):
    """Enforce exactly one row per trading date, ascending by date. If the log already
    holds duplicates for a date, keep the FIRST-recorded row: it was written closest to
    that session's close, and under the freeze rule in main() its chain-derived values
    (IV30/VRP/mNAV/BTC) are the canonical ones for that session."""
    seen = {}
    for r in rows:
        seen.setdefault(r.get("date", ""), r)
    return [seen[d] for d in sorted(seen)]


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


def _r(v, nd=1):
    """Round for the log/snapshot; None for NaN/missing."""
    return None if v is None or np.isnan(v) else round(v, nd)


def contract_fields(m):
    """Flatten the target put/call reads into log/snapshot columns (None if unavailable)."""
    out = {"rr25_skew": _r(m["rr25"])}
    for side in ("put", "call"):
        c = m[side] or {}
        out[f"{side}_contract"] = c.get("contract")
        out[f"{side}_mid"] = _r(c.get("mid"), 2)
        out[f"{side}_iv"] = _r(c.get("iv"))
        out[f"{side}_yield_pct"] = _r(c.get("yield_pct"), 2)
        out[f"{side}_ann_pct"] = _r(c.get("ann_pct"))
    return out


def main():
    try:
        eod, live, partial = compute_metrics()
    except Exception as e:
        # Could not get price data this run; skip without crashing. The dashboard's
        # staleness banner makes any freeze visible, so this won't masquerade as live.
        print(f"[skip] market data unavailable this run ({type(e).__name__}: {e}); no update written.")
        return

    # --- canonical end-of-day row (written to the log; one per completed session) ---
    state = classify(eod["rvp"], eod["vrp"], eod["rsi"], eod["below_ma50"])
    tier = call_tier(eod["rvp"], eod["vrp"])
    meltup = meltup_risk(eod["rsi"], eod["below_ma50"])
    mnav, btc_px = strategy_mnav()
    row = {
        "date": eod["asof"],                # last completed session (EOD-anchored), not wall-clock today
        "state": state,
        "dte_reco": DTE_BY_STATE.get(state, "-"),
        "call_tier": tier,
        "meltup_risk": meltup,
        "rv_percentile": round(eod["rvp"], 1) if not np.isnan(eod["rvp"]) else "",
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
        **{k: ("" if v is None else v) for k, v in contract_fields(eod).items()},
    }

    rows = _dedup_sorted(load_rows())
    # Look the session up BY DATE across the whole log, not just the last row. Yahoo
    # occasionally serves a daily frame that lags one or two sessions (seen 2026-07-15,
    # 07-27, 08-18, 08-31): the EOD anchor then points at an OLDER date than the last
    # logged row, and a last-row-only check appended it as a duplicate -- after which the
    # next correct run re-appended the newer date too. Matching by date makes every rerun
    # for a trading date overwrite that date's single row, wherever it sits in the log.
    idx = next((i for i, r in enumerate(rows) if r.get("date") == row["date"]), None)
    if idx is not None:
        ref = rows[idx]                     # rerun: alert only if THIS date's logged state changes
    else:
        earlier = [r for r in rows if r.get("date", "") < row["date"]]
        ref = earlier[-1] if earlier else None   # new session: compare to the prior session
    prev = ref["state"] if ref else None
    prev_tier = ref.get("call_tier", "") if ref else None
    if idx is not None:
        # This completed session already has a row. Price/vol fields are reproducible; the
        # chain-derived ones (IV30/VRP/mNAV/BTC/skew/target contracts) are not, so freeze them at their first
        # recorded values. This covers ALL later same-date writes -- including next-morning
        # intraday runs whose EOD anchor still points at this (now-closed) prior session --
        # so a completed session's numbers are never revised by a later chain read. State is
        # then recomputed from the frozen VRP so it stays consistent with the logged row.
        existing = rows[idx]
        for k in CHAIN_COLS:
            if existing.get(k) not in (None, ""):
                row[k] = existing[k]
        fv = row["vrp_iv_minus_hv"]
        frozen_vrp = float(fv) if fv not in (None, "") else float("nan")
        state = classify(eod["rvp"], frozen_vrp, eod["rsi"], eod["below_ma50"])
        row["state"] = state
        row["dte_reco"] = DTE_BY_STATE.get(state, "-")
        tier = call_tier(eod["rvp"], frozen_vrp)   # tier depends on VRP -> recompute from frozen value
        row["call_tier"] = tier
        rows[idx] = row
    else:
        rows.append(row)
    rows = _dedup_sorted(rows)              # exactly one row per trading date, ascending
    save_rows(rows, list(row.keys()))

    # --- provisional intraday snapshot (for the dashboard; NOT in the canonical log) ---
    # During a live session this reflects today's developing conditions so an intraday
    # decision isn't made off a stale prior close. After the close it equals the EOD row.
    live_state = classify(live["rvp"], live["vrp"], live["rsi"], live["below_ma50"])
    snap = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "provisional": bool(partial),
        "asof": live["asof"],
        "state": live_state,
        "dte_reco": DTE_BY_STATE.get(live_state, "-"),
        "call_tier": call_tier(live["rvp"], live["vrp"]),
        "meltup_risk": meltup_risk(live["rsi"], live["below_ma50"]),
        "rv_percentile": round(live["rvp"], 1) if not np.isnan(live["rvp"]) else None,
        "vrp_iv_minus_hv": round(live["vrp"], 1) if not np.isnan(live["vrp"]) else None,
        "iv30": round(live["iv30"], 1) if not np.isnan(live["iv30"]) else None,
        "hv30": round(live["rv30"], 1),
        "close": round(live["close"], 2),
        "rsi14": round(live["rsi"], 1),
        "below_ma50": live["below_ma50"],
        **contract_fields(live),
    }
    SNAP_PATH.write_text(json.dumps(snap, indent=2), encoding="utf-8")

    line = (f"{row['date']}  MSTR ${row['close']}  STATE={state}  "
            f"RVPct={row['rv_percentile']}  VRP={row['vrp_iv_minus_hv']}  "
            f"RSI={row['rsi14']}  vs50dMA={'below' if eod['below_ma50'] else 'above'}  "
            f"DTE={row['dte_reco']}  CallTier={row['call_tier'] or '-'}  Meltup={row['meltup_risk']}  "
            f"Skew25={row['rr25_skew'] or '-'}  "
            + "  ".join(f"{k.title()}={row[k + '_contract']}@{row[k + '_mid']}({row[k + '_ann_pct']}%/yr)"
                        if row[k + "_contract"] else f"{k.title()}=-" for k in ("put", "call")))
    print(line)
    if partial:
        print(f"[live] provisional {live_state}  RVPct={snap['rv_percentile']} "
              f"VRP={snap['vrp_iv_minus_hv']} RSI={snap['rsi14']} close={snap['close']} "
              f"CallTier={snap['call_tier'] or '-'}")

    # Alerts fire on canonical (session-to-session) changes only -> no intraday flapping.
    if prev is not None and prev != state and state != "UNKNOWN":
        notify(f"MSTR overlay STATE CHANGE: {prev} -> {state}\n{line}")
        print(f"[ALERT] state changed: {prev} -> {state}")
    if prev_tier is not None and prev_tier != tier:
        notify(f"MSTR covered-call premium tier: {prev_tier or 'none'} -> {tier or 'none'}\n{line}")
        print(f"[ALERT] call tier changed: {prev_tier or 'none'} -> {tier or 'none'}")


if __name__ == "__main__":
    main()
