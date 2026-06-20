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


def load_snapshot():
    try:
        return json.loads(SNAP_PATH.read_text(encoding="utf-8"))
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
    "OPPORTUNE_CALLS":    ("Opportune — sell calls",           "#1769aa", "Rich premium + overbought bounce below the 50-day MA."),
    "EXTREME_CALLS_FLAG": ("Manual flag — extreme calls",      "#8a5cc4", "Rare; treat as judgment call, not a mechanical signal."),
    "RICH_NO_SIDE":       ("Premium rich — no clear side",     "#6b7a8f", "IV is elevated but no directional confirmation. Wait."),
    "NEUTRAL":            ("Neutral — do nothing opportunistic","#6b7280", "Low IV or no vol premium. The mechanical wheel runs as usual."),
    "UNKNOWN":            ("Unknown — data issue",             "#9ca3af", "A metric was missing on this run. Check the latest Action log."),
}

def live_panel(snap):
    """Provisional intraday read, shown only while a session is in progress. The confirmed
    end-of-day signal is the banner below; this box is the developing live read so an
    intraday decision isn't made off a stale prior close."""
    if not snap or not snap.get("provisional"):
        return ""   # session closed (or no snapshot) -> the confirmed banner below is current
    st = snap.get("state", "UNKNOWN")
    color = STATE_META.get(st, STATE_META["UNKNOWN"])[1]
    def s(v): return html.escape("—" if v in (None, "") else str(v))
    below = str(snap.get("below_ma50")).lower() == "true"
    return (
        f"<div style='border:2px dashed {color};border-radius:14px;padding:14px 16px;margin:10px 0 4px;background:#fff'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<span class='pill' style='background:{color}'>LIVE · {s(st)}</span>"
        f"<span style='font-size:12px;color:#b54708;font-weight:600'>PROVISIONAL — session in progress, not final until the close</span>"
        f"</div>"
        f"<div style='font-size:13px;color:#475467;margin-top:8px'>"
        f"VolPct <b>{s(snap.get('iv_percentile'))}</b> &middot; VRP <b>{s(snap.get('vrp_iv_minus_hv'))}</b> &middot; "
        f"RSI <b>{s(snap.get('rsi14'))}</b> &middot; {'below' if below else 'above'} MA50 &middot; "
        f"close <b>{s(snap.get('close'))}</b> &middot; IV30/HV30 {s(snap.get('iv30'))}/{s(snap.get('hv30'))}</div>"
        f"<div style='font-size:11px;color:#98a2b3;margin-top:6px'>"
        f"Live read for session {s(snap.get('asof'))} &middot; generated {s(snap.get('generated_utc'))}. "
        f"Updates only when a monitor run fires (cron is best-effort) &mdash; force a run for the freshest read.</div>"
        f"</div>")

def load_rows():
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH) as f:
        return list(csv.DictReader(f))

def fnum(v, nd=1, suffix=""):
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return "—"

def sparkline_svg(rows, key, w=720, h=140, lo=0, hi=100, refs=(50, 80)):
    """Minimal SVG line for a 0-100 metric (IV percentile)."""
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

def build():
    rows = load_rows()
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if not rows:
        body = "<p>No readings logged yet. Run the monitor once, then rebuild.</p>"
        OUT_PATH.write_text(PAGE.format(updated=now, body=body), encoding="utf-8")
        print("Wrote", OUT_PATH, "(empty log)")
        return

    cur = rows[-1]
    state = cur.get("state", "UNKNOWN")
    title, color, blurb = STATE_META.get(state, STATE_META["UNKNOWN"])

    cards = [
        ("Vol Percentile", fnum(cur.get("iv_percentile"), 1), "primary trigger (RV basis)"),
        ("VRP (IV−HV)", fnum(cur.get("vrp_iv_minus_hv"), 1), "vol points; act unless < −2"),
        ("RSI(14)", fnum(cur.get("rsi14"), 0), "side selection"),
        ("Price", fnum(cur.get("close"), 2, ""), "MSTR close"),
        ("vs 50-day MA", "below" if str(cur.get("below_ma50")).lower() == "true" else "above", "calls need 'below'"),
        ("IV30 / HV30", f"{fnum(cur.get('iv30'),0)} / {fnum(cur.get('hv30'),0)}", "implied vs realized"),
        ("DTE to sell", html.escape(str(cur.get("dte_reco", "—"))), "recommended tenor"),
        ("mNAV", fnum(cur.get("mnav"), 2) if cur.get("mnav") not in (None, "", "—") else "—", "official (EV / BTC NAV)"),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{html.escape(k)}</div>"
        f"<div class='v'>{html.escape(str(v))}</div><div class='s'>{html.escape(s)}</div></div>"
        for k, v, s in cards)

    chart = sparkline_svg(rows[-90:], "iv_percentile")

    recent = rows[-30:][::-1]
    cols = ["date", "state", "iv_percentile", "vrp_iv_minus_hv", "rsi14", "close", "below_ma50", "dte_reco"]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    trs = ""
    for r in recent:
        c = STATE_META.get(r.get("state", ""), ("", "#888", ""))[1]
        tds = ""
        for col in cols:
            val = html.escape(str(r.get(col, "")))
            if col == "state":
                val = f"<span class='pill' style='background:{c}'>{val}</span>"
            tds += f"<td>{val}</td>"
        trs += f"<tr>{tds}</tr>"

    legend = "".join(
        f"<span class='pill' style='background:{m[1]}'>{html.escape(k)}</span>"
        for k, m in STATE_META.items() if k != "UNKNOWN")

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
      {live_panel(load_snapshot())}
      <div class='banner' style='background:{color}'>
        <div class='banner-state'>{html.escape(title)}</div>
        <div class='banner-blurb'>{html.escape(blurb)}</div>
        <div class='banner-date'>Confirmed signal &mdash; last completed session: {html.escape(cur.get('date',''))}</div>
      </div>
      <div class='grid'>{card_html}</div>
      <h2>Vol Percentile — last 90 readings</h2>
      <p class='muted'>Dashed lines at 50 (opportune threshold) and 80 (extreme threshold). Dot color = state that day.</p>
      {chart}
      <h2>Recent readings</h2>
      <div class='legend'>{legend}</div>
      <table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table>
      <p class='muted'>This is a volatility/price-timing signal only. It does not size positions or place trades,
      and it is blind to fundamental shocks — your own monitoring sits above it.</p>
    """
    OUT_PATH.write_text(PAGE.format(updated=now, body=body), encoding="utf-8")
    print("Wrote", OUT_PATH, "| current state:", state)

PAGE = """<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MSTR Overlay Dashboard</title>
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
  .legend {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }}
</style></head><body>
<h1>MSTR Opportunistic Overlay</h1>
<div class='muted'>Auto-generated {updated}. Read-only signal view.</div>
{body}
</body></html>"""

if __name__ == "__main__":
    build()
