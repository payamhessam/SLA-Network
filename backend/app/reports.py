"""Executive report generation (Excel + PowerPoint) from the live application analytics.

Every value comes from the same Fleet-scoped analytics the UI uses (overview, sla,
telemetry, trends, resilience) so exported figures reconcile with the dashboard.
Nothing is hard-coded; missing evidence stays "Insufficient"/"Not monitored". Reports are
Medline-styled and read-only (no LogicMonitor write access is ever required).
"""
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt
from sqlalchemy.orm import Session

from . import overview, resilience, sla, telemetry, trends
from .config import get_settings

# Palette aligned to the "Executive Precision" design system (Deep Navy / Slate + blue).
NAVY = "0F172A"   # primary — headers
BLUE = "2563EB"   # accent  — links / emphasis
STEEL = "334155"  # secondary — table header fill
ZEBRA = "F1F5F9"  # alternating table rows
_BLUE = RGBColor(0x25, 0x63, 0xEB)
_NAVY = RGBColor(0x0F, 0x17, 0x2A)
_GREY = RGBColor(0x64, 0x74, 0x8B)


def _safe(v):
    """Neutralise spreadsheet-formula injection in exported strings."""
    if isinstance(v, str) and v[:1] in "=+-@":
        return "'" + v
    return "" if v is None else v


def _pct(v):
    return f"{v:.3f}%" if isinstance(v, (int, float)) else "Insufficient"


def _snapshot(db: Session) -> dict:
    """Pull one coherent analytics snapshot so every sheet/slide reconciles."""
    return {"overview": overview.build(db), "sla": sla.fleet_sla(db), "telemetry": telemetry.build(db),
            "trends": trends.build(db), "resilience": resilience.latest_assessment(db)}


def _sheet(wb, title):
    ws = wb.create_sheet(title[:31])
    ws.sheet_view.showGridLines = False
    ws["A1"] = title
    ws["A1"].font = Font(size=15, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    return ws


def _table(ws, headers, rows, row=3):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=STEEL)
    zebra = PatternFill("solid", fgColor=ZEBRA)
    for r, data in enumerate(rows, row + 1):
        for c, value in enumerate(data, 1):
            cell = ws.cell(row=r, column=c, value=_safe(value))
            if (r - row) % 2 == 0:  # alternating-row shading, per the design system
                cell.fill = zebra
    for i, _ in enumerate(headers):
        ws.column_dimensions[chr(65 + i)].width = 24
    ws.freeze_panes = ws.cell(row=row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{row}:{chr(64 + len(headers))}{row + len(rows)}"


def executive_excel(db: Session) -> Path:
    d = _snapshot(db)
    o, s, tel, tr = d["overview"], d["sla"], d["telemetry"], d["trends"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    folder = Path(get_settings().report_dir); folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Executive_Reliability_{stamp}.xlsx"
    wb = Workbook(); wb.remove(wb.active)

    ws = _sheet(wb, "Executive Summary")
    g = o["global_sla"]; h = o["header"]
    rows = [
        ("Reporting organisation", h["org"]), ("Report", h["title"]),
        ("Generated (UTC)", stamp), ("Fleet devices (scope)", h["fleet_coverage"]["monitored"]),
        ("LM-matched", f"{h['lm_coverage']['matched']}/{h['lm_coverage']['total']}"),
        ("SLA evidence coverage", f"{h['sla_evidence_coverage']}%"), ("Data quality", h["data_quality"]),
        ("Network SLA (30-day)", _pct(g["current"])), ("SLA target", f"{g['target']}%"), ("Status", g["status"]),
        ("YTD availability", _pct(g["ytd"])), ("Estimated network tier", o["resilience"]["fleet_tier"]),
        ("Week-over-week", tr["deltas"]["wow"]["trend"]), ("Month-over-month", tr["deltas"]["mom"]["trend"]),
        ("Incidents (90d, availability-derived)", tr["mttr_mtbf"]["incidents"]),
    ]
    _table(ws, ["Metric", "Value"], rows)
    ws.cell(row=len(rows) + 5, column=1, value=o["summary"]).alignment = Alignment(wrap_text=True, vertical="top")

    ws = _sheet(wb, "Business Units")
    _table(ws, ["Business Unit", "Availability YTD", "30-Day", "Devices", "Sites", "Incidents", "Status"],
           [(b["business_unit"], _pct(b["availability_ytd"]), _pct(b["availability_30d"]), b["devices"], b["sites"], b["incidents"], b["status"]) for b in o.get("business_units", [])])

    ws = _sheet(wb, "SLA by Device")
    _table(ws, ["Device", "Site", "WTD", "YTD", "Coverage YTD"],
           [(e["hostname"], e["site"], _pct(e["wtd"]["availability"]), _pct(e["ytd"]["availability"]), f"{e['ytd']['coverage']:.1f}%") for e in s["devices"]])

    ws = _sheet(wb, "Site Reliability")
    _table(ws, ["Site", "City", "Province", "Business Unit", "Availability YTD", "30-Day", "Devices", "Device Health", "Incidents", "Status"],
           [(x["site_code"], x["city"], x["province"], x["business_unit"], _pct(x["availability_ytd"]), _pct(x["availability_30d"]), x["devices"], x["device_health"], x["critical_devices"], x["status"]) for x in o["sites"]])

    ws = _sheet(wb, "Routing (OSPF)")
    _table(ws, ["Device", "Site", "Full Adjacencies", "Total", "Neighbor Events", "Status"],
           [(x["hostname"], x["city"], x["full"], x["neighbors"], x["neighbor_events"], x["status"]) for x in tel["routing"]["items"]])
    ws.cell(row=len(tel["routing"]["items"]) + 5, column=1,
            value=f"BGP: {tel['routing']['bgp']} · EIGRP: {tel['routing']['eigrp']} · Static: {tel['routing']['static']} (not monitored in this tenant).")

    ws = _sheet(wb, "Interfaces")
    iface = tel["interfaces"]
    _table(ws, ["Metric", "Value"], [("Total interfaces", iface["total"]), ("Operational", iface["up"]), ("Down (admin up)", iface["down"]),
                                      ("High utilisation", iface["high_util"]), ("With errors", iface["with_errors"]), ("Flapping", iface["flapping"])])

    ws = _sheet(wb, "Incidents")
    _table(ws, ["Device", "Site", "Start", "End", "Days", "Downtime (min)"],
           [(x["device"], x["city"], x["start"], x["end"], x["days"], x["down"]) for x in tr["incidents"]])

    ws = _sheet(wb, "Data Coverage")
    _table(ws, ["Metric", "LogicMonitor source", "Coverage", "Devices"],
           [(m["metric"], m["source"], m["status"], f"{m['devices']}/{tel['data_coverage']['fleet']}") for m in tel["data_coverage"]["metrics"]])

    ws = _sheet(wb, "Methodology")
    _table(ws, ["Topic", "Definition"], [
        ("Availability", "up eligible minutes / observed eligible minutes, coverage-gated at 90%."),
        ("Coverage", "observed minutes / expected minutes; below 90% = Insufficient evidence (never 0%)."),
        ("Resilience tier", "Network-redundancy estimate mapped to Uptime bands; NOT a facility certification."),
        ("Scope", "Only devices registered in Device Fleet are analysed, not all of LogicMonitor."),
        ("MTTR/MTBF", "Approximate, derived from availability history; exact timings need LM alert history."),
    ])
    wb.save(path)
    return path


def _title(slide, text, subtitle=None):
    box = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(12), Inches(1.1))
    tf = box.text_frame; tf.text = text
    tf.paragraphs[0].font.size = Pt(30); tf.paragraphs[0].font.bold = True; tf.paragraphs[0].font.color.rgb = _NAVY
    if subtitle:
        p = tf.add_paragraph(); p.text = subtitle; p.font.size = Pt(14); p.font.color.rgb = _GREY


def _bullets(slide, lines, top=1.7):
    box = slide.shapes.add_textbox(Inches(0.7), Inches(top), Inches(12), Inches(5))
    tf = box.text_frame; tf.word_wrap = True
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"•  {line}"; p.font.size = Pt(16); p.font.color.rgb = _NAVY; p.space_after = Pt(8)


def executive_pptx(db: Session) -> Path:
    d = _snapshot(db)
    o, tel, tr = d["overview"], d["telemetry"], d["trends"]
    g, h = o["global_sla"], o["header"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    folder = Path(get_settings().report_dir); folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Executive_Reliability_{stamp}.pptx"
    prs = Presentation(); prs.slide_width = Inches(13.33); prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    s = prs.slides.add_slide(blank)
    _title(s, f"{h['org']} — {h['title']}", f"Executive Network Reliability · generated {stamp} UTC")
    _bullets(s, [
        f"Network SLA (30-day): {_pct(g['current'])} against a {g['target']}% target — {g['status']}.",
        f"Fleet scope: {h['fleet_coverage']['monitored']} devices · SLA evidence coverage {h['sla_evidence_coverage']}% · data quality {h['data_quality']}.",
        f"Estimated network resilience tier: {o['resilience']['fleet_tier']}.",
        f"Trend: week-over-week {tr['deltas']['wow']['trend']}, month-over-month {tr['deltas']['mom']['trend']}.",
    ], top=2.2)

    s = prs.slides.add_slide(blank)
    _title(s, "Executive Summary")
    _bullets(s, [o["summary"]])

    s = prs.slides.add_slide(blank)
    _title(s, "Reliability by Business Unit")
    _bullets(s, [f"{b['business_unit']}: YTD {_pct(b['availability_ytd'])}, 30-day {_pct(b['availability_30d'])} — {b['devices']} devices across {b['sites']} site(s), {b['incidents']} incident(s) — {b['status']}."
                 for b in o.get("business_units", [])] or ["No business units assigned."])

    s = prs.slides.add_slide(blank)
    _title(s, "Top Reliability Risks")
    _bullets(s, [f"{a['severity']} — {a['title']}: {a['detail']}" for a in o["actions"][:5]] or ["No leadership actions — network within targets."])

    s = prs.slides.add_slide(blank)
    _title(s, "Routing & Telemetry Coverage")
    _bullets(s, [
        f"OSPF: {tel['routing']['devices_with_ospf']}/{tel['routing']['fleet']} devices monitored; BGP/EIGRP/static = NOT MONITORED in this tenant.",
        f"Interfaces: {tel['interfaces']['total']} total, {tel['interfaces']['down']} down, {tel['interfaces']['high_util']} high-utilisation.",
        f"Latency: avg {tel['latency']['avg_latency_ms']} ms, max {tel['latency']['max_latency_ms']} ms, loss {tel['latency']['avg_loss_pct']}%; jitter not monitored.",
        f"Incidents (90d): {tr['mttr_mtbf']['incidents']} · MTTR {tr['mttr_mtbf']['mttr_minutes']} min · MTBF {tr['mttr_mtbf']['mtbf_hours']} h (availability-derived).",
    ])

    s = prs.slides.add_slide(blank)
    _title(s, "Methodology & Data Quality")
    _bullets(s, [
        "Availability = up eligible minutes / observed eligible minutes, coverage-gated at 90%.",
        "Only Device Fleet devices are analysed — not all of LogicMonitor.",
        "Resilience tier is a network-redundancy estimate, not an Uptime Institute facility certification.",
        "Missing telemetry is reported as Insufficient / Not monitored — never fabricated as 0% or 100%.",
    ])
    prs.save(path)
    return path
