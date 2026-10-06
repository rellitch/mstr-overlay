#!/usr/bin/env python3
"""
Build a simple, self-contained dashboard (index.html) from mstr_overlay_log.csv.
No external libraries, no internet needed to view it — all data is baked into the file.
Run after mstr_overlay.py:  python build_dashboard.py
"""
import csv, json, html, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).parent / "mstr_overlay_log.csv"
OUT_PATH = Path(__file__).parent / "index.html"
SNAP_PATH = Path(__file__).parent / "live_snapshot.json"
ASST_CSV_PATH = Path(__file__).parent / "asst_overlay_log.csv"
ASST_SNAP_PATH = Path(__file__).parent / "asst_live_snapshot.json"
ET = ZoneInfo("America/New_York")

# ---- ASST tile: display-only context (never wired into the engine) ----------
ASST_WARRANT_BANNER_UNTIL = dt.date(2026, 10, 31)   # banner auto-hides after this date
ASST_WARRANT_TEXT = ("ASST $27 warrants expire mid-Oct 2026 — expect warrant-driven sell pressure "
                     "~2–3 wks into expiry; treat oversold / “sell puts” reads as event-driven, not clean signals.")
ASST_LIQUIDITY_TEXT = ("ASST options are thin: only ATM is liquid, OTM open interest is in the tens and spreads "
                       "run 25–65% of mid. Any read here is advisory — check the actual strike's bid/ask and "
                       "open interest before trading.")
# Mirrors the ASST_* block in mstr_overlay.py. Display only.
ASST_PARAMS_TEXT = ("Parameters are UNVALIDATED — borrowed from MSTR's v2 thresholds (RSI ≤45 puts / ≤30 extreme; "
                    "melt-up at RSI ≥60 above the 50-day MA) with the VRP band widened to ±8 vol pts because "
                    "ASST's spreads make its IV uncertain by about that much. RICH = both smoothed VRPs ≥ +8; "
                    "CHEAP = either ≤ −8; UNCLEAR between. IV30 and HV are 3-session averages. Lookbacks start "
                    "2025-09-12 (merger close); the RV percentile is context only. No mNAV: no clean data source.")


def load_snapshot(path=SNAP_PATH):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def last_completed_session():
    """Date of the most recent fully-closed US session (ET), weekend-aware. Holidays are
    not modeled, so on a holiday this may name the would-be session and show a harmless
    'stale' banner. Used only to decide whether the staleness banner appears. Readings are
    EOD-anchored to the last completed session, so the banner should compare against this
    (not wall-clock 'today'), or it would cry stale all day during the live session."""
    et = dt.datetime.now(ZoneInfo("America/New_York"))
    d = et.date()
    if et.time() < dt.time(16, 15):     # today's session not closed yet
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:              # back off Sat/Sun to Friday
        d -= dt.timedelta(days=1)
    return d

STATE_META = {
    "EXTREME_PUTS":       ("Extremely opportune — SELL PUTS", "#0f7a3d", "Highest-conviction put window (IVP≥80, capitulation)."),
    "OPPORTUNE_PUTS":     ("Opportune — sell puts",            "#1f9d57", "Rich premium + washed-out price."),
    # Legacy states kept only so historical rows (if any) still render:
    "OPPORTUNE_CALLS":    ("(retired) opportune calls",        "#9ca3af", "Retired: directional call states never fired; see the call-premium tier instead."),
    "EXTREME_CALLS_FLAG": ("(retired) extreme calls flag",     "#9ca3af", "Retired: near-contradictory condition; see the call-premium tier instead."),
    "RICH_NO_SIDE":       ("Premium rich — no clear side",     "#6b7a8f", "IV is elevated but no put confirmation. Check the covered-call premium tier."),
    "NEUTRAL":            ("Neutral — do nothing opportunistic","#6b7280", "Low IV or no vol premium. The mechanical wheel runs as usual."),
    "UNKNOWN":            ("Unknown — data issue",             "#9ca3af", "A metric was missing on this run. Check the latest Action log."),
}

# Covered-call premium tiers (orthogonal to the put/neutral state; premium richness, not direction).
TIER_META = {
    "CALLS_EXTREME_PREMIUM": ("EXTREMELY LUCRATIVE call premium", "#7c2d92",
                              "IVP≥75 + VRP≥10: IV paying 10+ vol pts over realized in an elevated-vol "
                              "regime (June-2026 capitulation printed VRP +18..+29). Richest credits."),
    "CALLS_GOOD_PREMIUM":    ("Good call premium", "#1769aa",
                              "IVP≥60 + VRP>0: elevated vol percentile, IV above realized. Covered calls pay decently."),
    "":                      ("No call-premium tier", "#6b7280",
                              "Premium not rich enough (or chain IV unavailable this run)."),
}
RETIRED_STATES = {"OPPORTUNE_CALLS", "EXTREME_CALLS_FLAG"}

VRP_DEADBAND = -2.0   # mirrors mstr_overlay.VRP_DEADBAND. Display only -- state logic lives in the engine.

def vrp_gap_banner(row):
    """One plain-language line for the realized-vs-implied gap, shown whenever VRP is
    meaningfully negative (below the deadband -- the same condition that forces NEUTRAL).
    On a realized-vol spike the vol percentile reads high while premium is actually cheap;
    this makes the reason for standing down explicit. Purely additive display."""
    try:
        vrp = float(row.get("vrp_iv_minus_hv"))
    except (TypeError, ValueError):
        return ""
    if vrp >= VRP_DEADBAND:
        return ""
    hv, iv = fnum(row.get("hv30"), 0), fnum(row.get("iv30"), 0)
    return (
        "<div style='background:#fffaeb;border:1px solid #fedf89;border-left:6px solid #b54708;"
        "border-radius:10px;padding:10px 14px;margin:-8px 0 12px;font-size:13px;color:#7a2e0e'>"
        f"Realized vol <b>{hv}</b> is <b>{abs(vrp):.1f} pts above</b> implied <b>{iv}</b> &mdash; "
        "premium is cheap vs. movement; stand down.</div>")

SKEW_BAND = 3.0   # vol pts; |25-delta skew| below this reads "balanced". Display only.
IV_RANK_MIN_N = 20   # logged sessions needed before an IV rank is shown


def fval(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def iv_rank_series(rows, lookback=252):
    """(IV rank, IV percentile, n) of each row's IV30 against the logged IV30 of the rows
    BEFORE it (no look-ahead). The log is the only free IV history, so this is a young
    sample (starts 2026-06-09) and is shown with its size; blank under IV_RANK_MIN_N."""
    out, hist = [], []
    for r in rows:
        v = fval(r.get("iv30"))
        prior = hist[-lookback:]
        if v is None or len(prior) < IV_RANK_MIN_N:
            out.append((None, None, len(prior)))
        else:
            lo, hi = min(prior + [v]), max(prior + [v])
            rank = (v - lo) / (hi - lo) * 100 if hi > lo else 50.0
            pctl = sum(p < v for p in prior) / len(prior) * 100
            out.append((rank, pctl, len(prior)))
        if v is not None:
            hist.append(v)
    return out


def iv_rank_live(rows, snap):
    """IV rank of the live snapshot's IV30 against logged sessions before its date."""
    prior = [r for r in rows if r.get("date", "") < str(snap.get("asof", ""))]
    return iv_rank_series(prior + [{"iv30": snap.get("iv30")}])[-1]


def rank_word(rank):
    if rank is None:
        return "not enough logged history yet"
    return ("cheap vs its logged range" if rank < 25 else "below mid-range" if rank < 50
            else "above mid-range" if rank < 75 else "rich vs its logged range")


def skew_word(rr):
    if rr is None:
        return "skew unavailable"
    if rr >= SKEW_BAND:
        return f"calls priced {rr:.1f} pts richer than puts"
    if rr <= -SKEW_BAND:
        return f"puts priced {-rr:.1f} pts richer than calls"
    return "balanced (calls ≈ puts)"


def contract_card(row, side, title, basis):
    """One target contract: what selling it pays right now, and how its IV compares to ATM."""
    c = row.get(f"{side}_contract") or ""
    if not c:
        return (f"<div class='card'><div class='k'>{html.escape(title)}</div>"
                f"<div class='v'>—</div><div class='s'>chain unavailable this run</div></div>")
    iv, iv30 = fval(row.get(f"{side}_iv")), fval(row.get("iv30"))
    rel = f" ({iv - iv30:+.0f} vs ATM)" if iv is not None and iv30 is not None else ""
    dte = ""
    try:
        dte = f" · {(dt.date.fromisoformat(c.split()[0]) - dt.date.fromisoformat(row['date'])).days} DTE"
    except (ValueError, KeyError, IndexError):
        pass
    return (f"<div class='card'><div class='k'>{html.escape(title)}</div>"
            f"<div class='v' style='font-size:19px'>{html.escape(c)}</div>"
            f"<div class='s' style='color:#475467;font-size:12px'>mid <b>${fnum(row.get(f'{side}_mid'), 2)}</b>{dte} &middot; "
            f"IV <b>{fnum(iv, 0)}</b>{html.escape(rel)}</div>"
            f"<div class='s' style='color:#475467;font-size:12px'><b>{fnum(row.get(f'{side}_yield_pct'), 2)}%</b> "
            f"&rarr; <b>{fnum(row.get(f'{side}_ann_pct'), 0)}%/yr</b> on {basis}</div></div>")


def premium_panel(row, rank, pctl, n):
    """Option-premium context for the confirmed session. Display only: none of this
    feeds the state machine or the call tier."""
    iv30, vrp, rr = fval(row.get("iv30")), fval(row.get("vrp_iv_minus_hv")), fval(row.get("rr25_skew"))
    parts = []
    if iv30 is not None and rank is not None:
        parts.append(f"IV30 is {rank_word(rank)} (rank {rank:.0f}/100 over {n} logged sessions)")
    if vrp is not None:
        parts.append(f"{abs(vrp):.0f} pts {'above' if vrp >= 0 else 'below'} realized vol"
                     + (" &mdash; premium is cheap relative to movement" if vrp < -2 else
                        " &mdash; sellers are being paid over movement" if vrp > 0 else ""))
    if rr is not None:
        parts.append(f"within the chain, {skew_word(rr)} at 25&Delta;")
    summary = ("Relative premium: " + "; ".join(parts) + ".") if parts else "Option-chain read unavailable this run."
    rank_s = (f"rank {rank:.0f} &middot; pctl {pctl:.0f} (n={n})" if rank is not None
              else f"rank needs {IV_RANK_MIN_N}+ logged sessions")
    cards = (
        f"<div class='card'><div class='k'>IV30 (ATM, 30-day)</div><div class='v'>{fnum(iv30, 1)}</div>"
        f"<div class='s'>{rank_s}</div></div>"
        f"<div class='card'><div class='k'>VRP (IV30 − HV30)</div><div class='v'>{fnum(vrp, 1)}</div>"
        f"<div class='s'>vol pts; state stands down below −2</div></div>"
        f"<div class='card'><div class='k'>25&Delta; skew (call − put IV)</div><div class='v'>{'—' if rr is None else f'{rr:+.1f}'}</div>"
        f"<div class='s'>{html.escape(skew_word(rr))} &middot; info only</div></div>")
    return (
        "<h2>Option premium &mdash; what the chain is paying</h2>"
        f"<p class='muted'>{summary}</p>"
        f"<div class='grid'>{cards}</div>"
        f"<div class='grid' style='margin-top:10px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))'>"
        f"{contract_card(row, 'put', 'Cash-secured put · ~30Δ, 30–45 DTE', 'cash secured')}"
        f"{contract_card(row, 'call', 'Covered call · ~10Δ, 40–45 DTE', 'shares held')}</div>"
        "<p class='muted'>IVs are solved from bid/ask mids (Black-Scholes). Yield = mid &divide; strike (put) "
        "or &divide; spot (call), annualized by days to expiry. Context only &mdash; the signal above is "
        "decided by realized-vol percentile, VRP and RSI.</p>")


def iv_hv_chart(rows, w=720, h=180):
    """IV30 vs HV30 on one vol-point axis: the gap between the lines is the VRP
    (IV above HV = option sellers paid over movement). Hover a day for its values."""
    iv = [fval(r.get("iv30")) for r in rows]
    hv = [fval(r.get("hv30")) for r in rows]
    vals = [v for v in iv + hv if v is not None]
    if len(vals) < 4:
        return "<p style='color:#888'>Not enough data yet for a chart.</p>"
    lo = int(min(vals) // 20 * 20)
    hi = int(-(-max(vals) // 20) * 20)
    n = len(rows)
    L, R, T, B = 36, 76, 10, 22
    def x(i): return L + (w - L - R) * (i / max(n - 1, 1))
    def y(v): return T + (h - T - B) * (1 - (v - lo) / (hi - lo))
    grid = "".join(
        f"<line x1='{L}' x2='{w-R}' y1='{y(g):.1f}' y2='{y(g):.1f}' stroke='#eaecf0' stroke-width='1'/>"
        f"<text x='{L-6}' y='{y(g)+4:.1f}' font-size='11' fill='#98a2b3' text-anchor='end'>{g}</text>"
        for g in range(lo, hi + 1, 20))
    def path(vs):
        d, pen = "", False
        for i, v in enumerate(vs):
            if v is None:
                pen = False
                continue
            d += f"{'L' if pen else 'M'}{x(i):.1f},{y(v):.1f} "
            pen = True
        return d
    lines, marks = "", []
    for vs, color, name in ((iv, "#2a78d6", "IV30"), (hv, "#eb6834", "HV30")):
        lines += (f"<path d='{path(vs)}' fill='none' stroke='{color}' stroke-width='2' "
                  f"stroke-linejoin='round' stroke-linecap='round'/>")
        last = next(((i, v) for i, v in reversed(list(enumerate(vs))) if v is not None), None)
        if last:
            marks.append([x(last[0]), y(last[1]), color, f"{name} {last[1]:.0f}"])
    if len(marks) == 2 and abs(marks[0][1] - marks[1][1]) < 13:   # end labels would collide
        top, bot = sorted(marks, key=lambda m: m[1])
        mid = (top[1] + bot[1]) / 2
        top.append(mid - 7)
        bot.append(mid + 7)
    ends = "".join(
        f"<circle cx='{m[0]:.1f}' cy='{m[1]:.1f}' r='4' fill='{m[2]}' stroke='#fff' stroke-width='2'/>"
        f"<text x='{m[0]+9:.1f}' y='{(m[4] if len(m) > 4 else m[1])+4:.1f}' font-size='11' fill='#344054'>{m[3]}</text>"
        for m in marks)
    band = (w - L - R) / max(n - 1, 1)
    hits = ""
    for i, r in enumerate(rows):
        tip = f"{r.get('date', '')} · IV30 {fnum(iv[i], 1)} · HV30 {fnum(hv[i], 1)} · VRP {fnum(r.get('vrp_iv_minus_hv'), 1)}"
        hits += (f"<rect class='hit' x='{x(i)-band/2:.1f}' y='{T}' width='{band:.1f}' height='{h-T-B}' "
                 f"fill='transparent'><title>{html.escape(tip)}</title></rect>")
    first, last_d = rows[0].get("date", ""), rows[-1].get("date", "")
    axis = (f"<text x='{L}' y='{h-6}' font-size='11' fill='#98a2b3'>{html.escape(first)}</text>"
            f"<text x='{w-R}' y='{h-6}' font-size='11' fill='#98a2b3' text-anchor='end'>{html.escape(last_d)}</text>")
    legend = ("<div class='legend' style='font-size:12px;color:#475467;align-items:center'>"
              "<span><svg width='18' height='8'><line x1='0' x2='18' y1='4' y2='4' stroke='#2a78d6' stroke-width='2'/></svg> IV30 (implied)</span>"
              "<span><svg width='18' height='8'><line x1='0' x2='18' y1='4' y2='4' stroke='#eb6834' stroke-width='2'/></svg> HV30 (realized)</span></div>")
    return (legend + f"<svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px' role='img' "
            f"aria-label='IV30 versus HV30, last {n} sessions'>{grid}{lines}{ends}{hits}{axis}</svg>")


def live_panel(snap, rows=()):
    """Provisional intraday read, shown only while a session is in progress. The confirmed
    end-of-day signal is the banner below; this box is the developing live read so an
    intraday decision isn't made off a stale prior close."""
    if not snap or not snap.get("provisional"):
        return ""   # session closed (or no snapshot) -> the confirmed banner below is current
    st = snap.get("state", "UNKNOWN")
    color = STATE_META.get(st, STATE_META["UNKNOWN"])[1]
    def s(v): return html.escape("—" if v in (None, "") else str(v))
    below = str(snap.get("below_ma50")).lower() == "true"
    ltier = snap.get("call_tier") or ""
    tier_pill = ""
    if ltier:
        tier_pill = f"<span class='pill' style='background:{TIER_META.get(ltier, TIER_META[''])[1]}'>{s(ltier)}</span>"
    if snap.get("meltup_risk"):
        tier_pill += "<span class='pill' style='background:#b42318'>MELT-UP RISK</span>"
    live_rank = iv_rank_live(list(rows), snap)[0]
    return (
        f"<div style='border:2px dashed {color};border-radius:14px;padding:14px 16px;margin:10px 0 4px;background:#fff'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<span class='pill' style='background:{color}'>LIVE · {s(st)}</span>{tier_pill}"
        f"<span style='font-size:12px;color:#b54708;font-weight:600'>PROVISIONAL — session in progress, not final until the close</span>"
        f"</div>"
        f"<div style='font-size:13px;color:#475467;margin-top:8px'>"
        f"RV pct <b>{s(snap.get('rv_percentile'))}</b> &middot; VRP <b>{s(snap.get('vrp_iv_minus_hv'))}</b> &middot; "
        f"RSI <b>{s(snap.get('rsi14'))}</b> &middot; {'below' if below else 'above'} MA50 &middot; "
        f"close <b>{s(snap.get('close'))}</b> &middot; IV30/HV30 {s(snap.get('iv30'))}/{s(snap.get('hv30'))}</div>"
        f"<div style='font-size:13px;color:#475467;margin-top:4px'>"
        f"IV rank <b>{fnum(live_rank, 0)}</b> &middot; 25&Delta; skew <b>{s(snap.get('rr25_skew'))}</b> &middot; "
        f"put {s(snap.get('put_contract'))} <b>{fnum(snap.get('put_ann_pct'), 0)}%/yr</b> &middot; "
        f"call {s(snap.get('call_contract'))} <b>{fnum(snap.get('call_ann_pct'), 0)}%/yr</b></div>"
        f"<div style='font-size:11px;color:#98a2b3;margin-top:6px'>"
        f"Live read for session {s(snap.get('asof'))} &middot; generated {s(snap.get('generated_utc'))}. "
        f"Updates only when a monitor run fires (cron is best-effort) &mdash; force a run for the freshest read.</div>"
        f"</div>")

def load_rows(path=CSV_PATH):
    if not path.exists():
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:   # pre-rename logs: iv_percentile was always the realized-vol percentile
        if "iv_percentile" in r:
            r.setdefault("rv_percentile", r.pop("iv_percentile"))
    return rows

def fnum(v, nd=1, suffix=""):
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return "—"

def sparkline_svg(rows, key, w=720, h=140, lo=0, hi=100, refs=(50, 80)):
    """Minimal SVG line for a 0-100 metric (realized-vol percentile)."""
    pts = []
    vals = []
    for r in rows:
        try:
            vals.append(float(r[key]))
        except (TypeError, ValueError, KeyError):
            vals.append(None)
    clean = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(clean) < 2:
        return "<p style='color:#888'>Not enough data yet for a chart — it fills in as the log grows.</p>"
    n = len(vals)
    def x(i): return 40 + (w - 60) * (i / max(n - 1, 1))
    def y(v): return 10 + (h - 30) * (1 - (v - lo) / (hi - lo))
    path = " ".join(f"{'M' if k == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for k, (i, v) in enumerate(clean))
    ref_lines = ""
    for rv in refs:
        ref_lines += (f"<line x1='40' x2='{w-20}' y1='{y(rv):.1f}' y2='{y(rv):.1f}' "
                      f"stroke='#d0d5dd' stroke-dasharray='4 4'/>"
                      f"<text x='8' y='{y(rv)+4:.1f}' font-size='11' fill='#98a2b3'>{rv}</text>")
    dots = ""
    for i, v in clean:
        st = rows[i].get("state", "")
        c = STATE_META.get(st, ("", "#888", ""))[1]
        dots += f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='3' fill='{c}'/>"
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px'>"
            f"{ref_lines}<path d='{path}' fill='none' stroke='#344054' stroke-width='1.5'/>{dots}</svg>")

RICH_META = {   # ASST richness read (the monitor's replacement for MSTR's RV-percentile gate)
    "RICH":    ("#0f7a3d", "both smoothed VRPs ≥ +8 — IV is paying comfortably over recent movement"),
    "UNCLEAR": ("#6b7a8f", "VRPs inside the ±8 noise band — no honest richness read"),
    "CHEAP":   ("#b54708", "a smoothed VRP ≤ −8 — premium is cheap vs. movement; stand down"),
    "":        ("#9ca3af", "VRP unavailable (no chain IV this run)"),
}


def _flag(text, color):
    return f"<span class='pill' style='background:{color}'>{html.escape(text)}</span>"


def asst_section(rows, snap, today=None):
    """Compact ASST tile. Everything here is labeled unvalidated / monitor-only, and the
    tile shows '—' with a DATA UNAVAILABLE flag whenever the run could not read ASST,
    rather than freezing on an old value. Independent of the MSTR section above it."""
    today = today or dt.datetime.now(ET).date()
    snap = snap or {}
    cur = rows[-1] if rows else {}
    expected = last_completed_session().isoformat()

    unavailable = bool(snap.get("data_unavailable")) or str(cur.get("data_unavailable", "")).lower() == "true"
    reason = snap.get("reason") or ("option chain empty or sparse" if unavailable else "")
    missed = bool(cur) and cur.get("date", "") < expected        # no run landed for the last session
    hide = unavailable or not cur     # metrics show '—' (never an old value) when the read failed
    def v(key, nd=1, prefix=""):
        return "—" if hide else fnum(cur.get(key), nd, prefix)

    flags = ""
    if unavailable:
        flags += _flag(f"DATA UNAVAILABLE — {reason}", "#b42318")
    if missed:
        flags += _flag(f"STALE — latest row {cur.get('date')}, last session {expected}", "#b54708")
    if not unavailable and not missed and cur:
        flags += _flag("market closed — confirmed read for " + cur.get("date", ""), "#475467")
    if int(float(cur.get("smooth_n") or 0)) < 3 and cur and not hide:
        flags += _flag(f"smoothing over {int(float(cur.get('smooth_n') or 0))} session(s) — needs 3 logged", "#6b7a8f")

    state = "UNKNOWN" if hide else cur.get("state", "UNKNOWN")
    title, color, _ = STATE_META.get(state, STATE_META["UNKNOWN"])
    rich = "" if hide else (cur.get("richness") or "")
    rich_color, rich_blurb = RICH_META.get(rich, RICH_META[""])
    tier = "" if hide else (cur.get("call_tier") or "")
    tier_title, tier_color, _ = TIER_META.get(tier, TIER_META[""])
    meltup = (not hide) and str(cur.get("meltup_risk", "")).lower() == "true"

    warrant = ""
    if today <= ASST_WARRANT_BANNER_UNTIL:
        warrant = ("<div style='background:#fffaeb;border:1px solid #fedf89;border-left:6px solid #b54708;"
                   "border-radius:10px;padding:10px 14px;margin:8px 0;font-size:13px;color:#7a2e0e'>"
                   f"&#9888; <b>Warrant overhang:</b> {html.escape(ASST_WARRANT_TEXT)} "
                   f"<span style='color:#98a2b3'>(banner auto-hides after {ASST_WARRANT_BANNER_UNTIL})</span></div>")

    live = ""
    if snap.get("provisional") and not unavailable:
        s = lambda k: html.escape("—" if snap.get(k) in (None, "") else str(snap.get(k)))
        live = (f"<div style='border:2px dashed {STATE_META.get(snap.get('state', 'UNKNOWN'), STATE_META['UNKNOWN'])[1]};"
                "border-radius:12px;padding:10px 14px;margin:8px 0;background:#fff;font-size:13px;color:#475467'>"
                f"{_flag('LIVE · ' + str(snap.get('state')), STATE_META.get(snap.get('state', 'UNKNOWN'), STATE_META['UNKNOWN'])[1])} "
                f"<b style='color:#b54708'>PROVISIONAL</b> &middot; richness <b>{s('richness')}</b> &middot; "
                f"VRP vs HV10/HV21 <b>{s('vrp_hv10')}/{s('vrp_hv21')}</b> &middot; RSI <b>{s('rsi14')}</b> &middot; "
                f"close <b>{s('close')}</b> &middot; session {s('asof')}, generated {s('generated_utc')}</div>")

    cards = [
        ("IV30 (near-money, OTM strikes)", v("iv30"),
         "—" if hide else f"3-day avg {fnum(cur.get('iv30_smooth'))} · {fnum(cur.get('iv_mid_share'), 0)}% of strikes from bid/ask mid"),
        ("HV10 / HV21", "—" if hide else f"{fnum(cur.get('hv10'), 0)} / {fnum(cur.get('hv21'), 0)}", "short realized vol (HV30 " + ("—" if hide else fnum(cur.get("hv30"), 0)) + ")"),
        ("VRP vs HV10", v("vrp_hv10"), "—" if hide else f"smoothed; raw {fnum(cur.get('vrp_hv10_raw'))} · primary"),
        ("VRP vs HV21", v("vrp_hv21"), "—" if hide else f"smoothed; raw {fnum(cur.get('vrp_hv21_raw'))} · read as a range with HV10"),
        ("IV30 ÷ MSTR IV30", v("iv_ratio_mstr", 2), "—" if hide else f"MSTR IV30 {fnum(cur.get('mstr_iv30'), 0)} · BTC-regime normalizer, context only"),
        ("IV30 ÷ IBIT IV30", v("iv_ratio_ibit", 2), "—" if hide else f"IBIT IV30 {fnum(cur.get('ibit_iv30'), 0)} · high = rich vs BTC peers"),
        ("RSI(14)", v("rsi14", 0), "puts side selection; ≥60 = melt-up flag"),
        ("Price", v("close", 2), "ASST close"),
        ("vs 50-day MA", "—" if hide else ("below" if str(cur.get("below_ma50")).lower() == "true" else "above"), "trend context"),
        ("RV percentile", v("rv_percentile"), "—" if hide else f"vs {cur.get('rv_pct_n', '?')} days since 2025-09-12 · LOW CONFIDENCE, context only"),
        ("DTE to sell", "—" if hide else html.escape(str(cur.get("dte_reco", "—"))), "recommended tenor (puts)"),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{html.escape(k)}</div>"
        f"<div class='v'>{html.escape(str(val))}</div><div class='s'>{html.escape(sub)}</div></div>"
        for k, val, sub in cards)

    if hide:
        contracts = ("<div class='card'><div class='k'>Cash-secured put · ~30Δ, 30–45 DTE</div><div class='v'>—</div></div>"
                     "<div class='card'><div class='k'>Covered call · ~10Δ, 40–45 DTE</div><div class='v'>—</div></div>")
    else:
        contracts = (contract_card(cur, "put", "Cash-secured put · ~30Δ, 30–45 DTE", "cash secured")
                     + contract_card(cur, "call", "Covered call · ~10Δ, 40–45 DTE", "shares held"))

    cols = ["date", "state", "richness", "call_tier", "iv30", "hv10", "hv21", "vrp_hv10", "vrp_hv21",
            "iv_ratio_mstr", "iv_ratio_ibit", "rsi14", "close", "below_ma50", "data_unavailable"]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    trs = ""
    for r in rows[-15:][::-1]:
        tds = ""
        for col in cols:
            val = html.escape(str(r.get(col) or ""))
            if col == "state":
                val = f"<span class='pill' style='background:{STATE_META.get(r.get('state', ''), ('', '#888', ''))[1]}'>{val}</span>"
            elif col == "richness" and r.get("richness"):
                val = f"<span class='pill' style='background:{RICH_META.get(r.get('richness'), RICH_META[''])[0]}'>{val}</span>"
            elif col == "call_tier" and r.get("call_tier"):
                val = f"<span class='pill' style='background:{TIER_META.get(r.get('call_tier', ''), TIER_META[''])[1]}'>{val}</span>"
            tds += f"<td>{val}</td>"
        trs += f"<tr>{tds}</tr>"
    table = (f"<div style='overflow-x:auto'><table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table></div>"
             if rows else "<p class='muted'>No ASST readings logged yet.</p>")

    meltup_html = ("<div style='background:#b42318;color:#fff;border-radius:10px;padding:10px 14px;margin:0 0 10px;font-size:13px'>"
                   "&#9888; <b>MELT-UP RISK</b> &mdash; RSI&ge;60 above the 50-day MA (same regime logic as MSTR, "
                   "unvalidated on ASST): if writing covered calls anyway, size down and expect to roll or be assigned.</div>"
                   if meltup else "")
    return f"""
      <hr style='border:0;border-top:2px solid #d0d5dd;margin:34px 0 18px'>
      <h1 style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>ASST (Strive) &mdash; relative-richness monitor
        {_flag('UNVALIDATED · MONITOR-ONLY', '#7c2d92')}</h1>
      <div class='muted'>Not a backtested signal. ASST's Bitcoin-treasury era began 2025-09-12 (~1 year of relevant history),
      so richness is scored RELATIVELY each run: IV30 against short realized vol, and against MSTR/IBIT IV. Full wheel (puts and calls).</div>
      <div class='legend' style='margin:6px 0'>{flags}</div>
      {warrant}
      {live}
      <div class='banner' style='background:{color};margin:10px 0 12px'>
        <div class='banner-state'>{html.escape(title)}</div>
        <div class='banner-blurb'>Richness: <b>{html.escape(rich or '—')}</b> &mdash; {html.escape(rich_blurb)}</div>
        <div class='banner-date'>{'Read unavailable this run' if hide else 'Confirmed read &mdash; last completed session: ' + html.escape(cur.get('date', ''))} &middot; monitor only, parameters unvalidated</div>
      </div>
      <div style='border:1px solid #eaecf0;border-left:6px solid {tier_color};background:#fff;border-radius:12px;padding:10px 16px;margin:0 0 10px'>
        <div style='font-size:13px;color:#667085'>Covered-call premium tier <span style='color:#98a2b3'>(richness, not direction; 10&Delta;, 40&ndash;45 DTE)</span></div>
        <div style='font-size:17px;font-weight:700;color:{tier_color}'>{'—' if hide else html.escape(tier_title)}</div></div>
      {meltup_html}
      <div class='grid'>{card_html}</div>
      <div class='grid' style='margin-top:10px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))'>{contracts}</div>
      <div style='background:#f2f4f7;border-radius:10px;padding:10px 14px;margin:12px 0;font-size:12px;color:#475467'>
        <b>Liquidity reality:</b> {html.escape(ASST_LIQUIDITY_TEXT)}</div>
      <p class='muted'>{html.escape(ASST_PARAMS_TEXT)}</p>
      <h2>ASST recent readings</h2>
      {table}
    """


def build():
    rows = load_rows()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    asst_html = "<!-- asst-section -->" + asst_section(load_rows(ASST_CSV_PATH), load_snapshot(ASST_SNAP_PATH))
    if not rows:
        body = "<p>No readings logged yet. Run the monitor once, then rebuild.</p>"
        OUT_PATH.write_text(PAGE.format(updated=now, body=body + asst_html), encoding="utf-8")
        print("Wrote", OUT_PATH, "(empty log)")
        return

    cur = rows[-1]
    state = cur.get("state", "UNKNOWN")
    title, color, blurb = STATE_META.get(state, STATE_META["UNKNOWN"])

    tier = cur.get("call_tier", "") or ""
    tier_title, tier_color, tier_blurb = TIER_META.get(tier, TIER_META[""])
    meltup = str(cur.get("meltup_risk", "")).lower() == "true"
    tier_html = (
        f"<div style='border-left:6px solid {tier_color};background:#fff;border:1px solid #eaecf0;"
        f"border-left:6px solid {tier_color};border-radius:12px;padding:12px 16px;margin:0 0 10px'>"
        f"<div style='font-size:13px;color:#667085'>Covered-call premium tier "
        f"<span style='color:#98a2b3'>(income view for shares you own &mdash; 10&Delta;, 40&ndash;45 DTE; richness, not direction)</span></div>"
        f"<div style='font-size:18px;font-weight:700;color:{tier_color};margin-top:2px'>{html.escape(tier_title)}</div>"
        f"<div style='font-size:12px;color:#667085;margin-top:2px'>{html.escape(tier_blurb)}</div></div>")
    if meltup:
        tier_html += (
            "<div style='background:#b42318;color:#fff;border-radius:10px;padding:10px 14px;"
            "margin:0 0 10px;font-size:13px'>&#9888; <b>MELT-UP RISK</b> &mdash; RSI&ge;60 above the "
            "50-day MA. Historically the worst days to sell calls (they get run over); if writing "
            "covered calls anyway, size down and expect to roll or be assigned.</div>")

    ranks = iv_rank_series(rows)
    rank, pctl, rank_n = ranks[-1]
    cards = [
        ("RV Percentile", fnum(cur.get("rv_percentile"), 1), "state trigger: realized vol vs 1y (not option prices)"),
        ("RSI(14)", fnum(cur.get("rsi14"), 0), "puts side selection; ≥60 = melt-up flag"),
        ("Price", fnum(cur.get("close"), 2, ""), "MSTR close"),
        ("vs 50-day MA", "below" if str(cur.get("below_ma50")).lower() == "true" else "above", "trend context"),
        ("HV30", fnum(cur.get("hv30"), 0), "30-day realized vol"),
        ("DTE to sell", html.escape(str(cur.get("dte_reco", "—"))), "recommended tenor (puts)"),
        ("mNAV", fnum(cur.get("mnav"), 2) if cur.get("mnav") not in (None, "", "—") else "—", "official (EV / BTC NAV)"),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{html.escape(k)}</div>"
        f"<div class='v'>{html.escape(str(v))}</div><div class='s'>{html.escape(s)}</div></div>"
        for k, v, s in cards)

    chart = sparkline_svg(rows[-90:], "rv_percentile")
    ivhv = iv_hv_chart(rows[-90:])

    for r, (rk, _, _) in zip(rows, ranks):
        r["iv_rank"] = "" if rk is None else f"{rk:.0f}"
    recent = rows[-30:][::-1]
    cols = ["date", "state", "call_tier", "rv_percentile", "iv30", "iv_rank", "vrp_iv_minus_hv", "rr25_skew",
            "put_ann_pct", "call_ann_pct", "rsi14", "close", "below_ma50", "dte_reco"]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    trs = ""
    for r in recent:
        c = STATE_META.get(r.get("state", ""), ("", "#888", ""))[1]
        tds = ""
        for col in cols:
            val = html.escape(str(r.get(col) or ""))
            if col == "state":
                val = f"<span class='pill' style='background:{c}'>{val}</span>"
            elif col == "call_tier" and r.get("call_tier"):
                tc = TIER_META.get(r.get("call_tier", ""), TIER_META[""])[1]
                val = f"<span class='pill' style='background:{tc}'>{val}</span>"
            tds += f"<td>{val}</td>"
        trs += f"<tr>{tds}</tr>"

    legend = "".join(
        f"<span class='pill' style='background:{m[1]}'>{html.escape(k)}</span>"
        for k, m in STATE_META.items() if k != "UNKNOWN" and k not in RETIRED_STATES)
    legend += "".join(
        f"<span class='pill' style='background:{m[1]}'>{html.escape(k)}</span>"
        for k, m in TIER_META.items() if k)

    expected = last_completed_session().isoformat()
    latest_date = cur.get("date", "")
    stale_html = ""
    try:
        if latest_date and latest_date < expected:
            stale_html = (
                "<div style='background:#b54708;color:#fff;border-radius:10px;"
                "padding:10px 14px;margin:10px 0;font-size:13px'>"
                f"&#9888; Heads up: the latest reading is from <b>{html.escape(latest_date)}</b>, "
                f"behind the last completed session (<b>{html.escape(expected)}</b>). Scheduled "
                "(cron) runs are best-effort and GitHub often delays or skips them &mdash; "
                "especially around the US open &mdash; so this is expected from time to time. For "
                "an immediate update, force a run from the Actions tab (“Run workflow”). If a run "
                "did fire but nothing changed, check the latest Actions log for a data-fetch skip.</div>")
    except Exception:
        pass

    body = f"""
      {stale_html}
      {live_panel(load_snapshot(), rows)}
      <div class='banner' style='background:{color}'>
        <div class='banner-state'>{html.escape(title)}</div>
        <div class='banner-blurb'>{html.escape(blurb)}</div>
        <div class='banner-date'>Confirmed signal &mdash; last completed session: {html.escape(cur.get('date',''))}</div>
      </div>
      {vrp_gap_banner(cur)}
      {tier_html}
      {premium_panel(cur, rank, pctl, rank_n)}
      <h2>Price &amp; trend</h2>
      <div class='grid'>{card_html}</div>
      <h2>Implied vs realized vol — last 90 readings</h2>
      <p class='muted'>Blue above orange = options priced over actual movement (positive VRP, good for sellers).
      Orange above blue = premium is cheap vs. movement. Hover a day for its values.</p>
      {ivhv}
      <h2>Realized-vol percentile — last 90 readings</h2>
      <p class='muted'>The state trigger. Dashed lines at 50 (opportune threshold) and 80 (extreme threshold). Dot color = state that day.
      This ranks how much MSTR has been <i>moving</i>, not how expensive its options are &mdash; see IV30 above for that.</p>
      {chart}
      <h2>Recent readings</h2>
      <div class='legend'>{legend}</div>
      <div style='overflow-x:auto'><table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table></div>
      <p class='muted'>This is a volatility/price-timing signal only. It does not size positions or place trades,
      and it is blind to fundamental shocks — your own monitoring sits above it.</p>
    """
    OUT_PATH.write_text(PAGE.format(updated=now, body=body + asst_html), encoding="utf-8")
    print("Wrote", OUT_PATH, "| current state:", state)

PAGE = """<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MSTR + ASST Overlay Dashboard</title>
<style>
  :root {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
  body {{ margin:0; background:#f7f8fa; color:#1d2939; padding:18px; }}
  h1 {{ font-size:18px; margin:0 0 2px; }}
  h2 {{ font-size:15px; margin:26px 0 6px; }}
  .muted {{ color:#667085; font-size:12px; margin:4px 0 10px; }}
  .banner {{ color:#fff; border-radius:14px; padding:20px 22px; margin:14px 0 18px; }}
  .banner-state {{ font-size:24px; font-weight:700; }}
  .banner-blurb {{ font-size:14px; opacity:.95; margin-top:4px; }}
  .banner-date {{ font-size:12px; opacity:.85; margin-top:8px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
  .card {{ background:#fff; border:1px solid #eaecf0; border-radius:12px; padding:12px 14px; }}
  .card .k {{ font-size:12px; color:#667085; }}
  .card .v {{ font-size:22px; font-weight:700; margin:2px 0; }}
  .card .s {{ font-size:11px; color:#98a2b3; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; background:#fff;
           border:1px solid #eaecf0; border-radius:12px; overflow:hidden; }}
  th,td {{ padding:7px 9px; text-align:left; border-bottom:1px solid #f0f1f3; white-space:nowrap; }}
  th {{ background:#fafbfc; color:#475467; font-weight:600; }}
  .pill {{ color:#fff; padding:2px 8px; border-radius:999px; font-size:11px; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:6px 14px; margin-bottom:8px; }}
  svg .hit:hover {{ fill:rgba(52,64,84,.06); }}
</style></head><body>
<h1>MSTR Opportunistic Overlay</h1>
<div class='muted'>Auto-generated {updated}. Read-only signal view.</div>
{body}
</body></html>"""

if __name__ == "__main__":
    build()
