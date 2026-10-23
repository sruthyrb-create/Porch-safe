"""Exposure report: a printable record to give the pharmacy (or keep) after a porch excursion.

Mail-order pharmacies generally replace medicines damaged in transit when told about it; this
gives the patient facts instead of a guess: when Ring saw the delivery, how long it sat outside,
the temperatures the National Weather Service recorded, and what the label allows.
"""

from __future__ import annotations

import base64
import html
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .monitor import Monitor, Package


def _t(ts: float | None) -> str:
    if ts is None:
        return "still outside"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%b %d, %Y %I:%M %p %Z")


def render(mon: Monitor, pkg: Package) -> str:
    prof = profiles.get(pkg.profile) if pkg.profile else None
    end = pkg.inside_at or mon.clock.now()
    minutes = int((end - pkg.delivered_at) // 60)
    temps = [s["temp_f"] for s in pkg.samples if s["temp_f"] is not None]
    lo, hi = (min(temps), max(temps)) if temps else (None, None)
    rows = "".join(
        f"<tr><td>{_t(s['t'])}</td><td>{'' if s['temp_f'] is None else round(s['temp_f'])}°F</td>"
        f"<td>{html.escape(s['status'].replace('_', ' '))}</td></tr>" for s in pkg.samples[-40:])
    alerts = "".join(f"<li>{_t(n['t'])}: to {html.escape(n['to'])}: {html.escape(n['text'])}</li>" for n in pkg.notified)
    img = ""
    if pkg.snapshot_file and (mon.dir / pkg.snapshot_file).exists():
        b64 = base64.b64encode((mon.dir / pkg.snapshot_file).read_bytes()).decode()
        img = f'<img alt="Ring snapshot at delivery" src="data:image/jpeg;base64,{b64}">'
    verdict = {
        "OK": "Stayed inside the labeled range while on the porch.",
        "WATCH": "Spent time outside the labeled storage range but within the label's permitted excursions.",
        "ACT_NOW": "Was exposed to temperatures outside what the label permits.",
        "EXCEEDED": "Was exposed to temperatures outside what the label permits for an extended time.",
    }[pkg.worst_status]
    ask = ("Ask your pharmacist whether this medicine is still OK to use, and request a replacement if not."
           if prof and prof.key != "perishable_food" else "Follow food-safety guidance before eating.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>PorchSafe exposure report</title>
<style>body{{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#14202b}}
h1{{font-size:22px;margin-bottom:4px}}.sub{{color:#5a6977}}table{{border-collapse:collapse;width:100%;margin:8px 0}}
td,th{{border-bottom:1px solid #dde3ea;padding:5px 8px;text-align:left}}.box{{border:1px solid #dde3ea;border-radius:10px;padding:12px 14px;margin:12px 0}}
.v{{font-weight:650}}img{{max-width:100%;border-radius:8px}}@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Print / save as PDF</button>
<h1>Porch exposure report</h1><div class="sub">{html.escape(mon.settings.household_name)} · generated {_t(mon.clock.now())}</div>
<div class="box"><div><b>Item:</b> {html.escape(pkg.description)}{f" ({html.escape(prof.label)})" if prof else ""}</div>
<div><b>Delivered (seen by Ring):</b> {_t(pkg.delivered_at)}</div>
<div><b>Brought inside:</b> {_t(pkg.inside_at)}{f" ({html.escape(pkg.inside_by)})" if pkg.inside_by else ""}</div>
<div><b>Time on porch:</b> {minutes} minutes</div>
<div><b>Outdoor temperature while outside:</b> {f"{round(lo)}°F to {round(hi)}°F" if lo is not None else "unavailable"} (source: {html.escape(", ".join(sorted({s.get("src", "NWS") for s in pkg.samples})) or "n/a")})</div>
<div class="v">Result: {verdict}</div><div>{ask}</div></div>
{f'<div class="box"><b>What the label says</b><div>{html.escape(prof.guidance)}</div><div class="sub">Source: <a href="{prof.source_url}">{html.escape(prof.source)}</a></div></div>' if prof else ""}
{img}
<h3>Temperature log</h3><table><tr><th>Time</th><th>Outdoor temp</th><th>Status</th></tr>{rows}</table>
<h3>Alerts sent</h3><ul>{alerts or "<li>none</li>"}</ul>
<p class="sub">Outdoor air temperature is used as a stand-in for the package; the inside of a package can differ. This report records facts; it is not medical advice.</p>
</body></html>"""
