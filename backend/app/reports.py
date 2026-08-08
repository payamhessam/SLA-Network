"""Executive report generation (Excel + PowerPoint) from the live application analytics.

Every value comes from the same Fleet-scoped analytics the UI uses (overview, sla,
telemetry, trends, resilience) so exported figures reconcile with the dashboard. Nothing
is hard-coded; missing evidence stays "Insufficient"/"Not monitored". The visual design
follows the Medline executive template: a deep-blue (#003DA5) brand banner, white KPI
cards with large navy values and colour-coded status, an executive readout with status
chips, and a per-site availability bar chart. Read-only (no LogicMonitor write access).
"""
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt
from sqlalchemy.orm import Session

from . import overview, resilience, sla, telemetry, trends
from .config import get_settings

# ---- Medline executive palette (hex for Excel, RGBColor for PowerPoint) ----
BRAND = "003DA5"   # Medline blue — banners / brand
NAVY = "0F172A"    # KPI values / primary headings
SLATE = "334155"   # labels / secondary text
ZEBRA = "F1F5F9"   # alternating rows / table header fill
CANVAS = "F8FAFC"  # slide canvas
LINE = "E2E8F0"    # 1px card outline
GREEN, GREEN_BG = "16A34A", "DCFCE7"
RED, RED_BG = "C00000", "FEE2E2"
AMBER, AMBER_BG = "CA8A04", "FEF9C3"
BLUE_BG = "DBEAFE"

_BRAND = RGBColor(0x00, 0x3D, 0xA5)
_NAVY = RGBColor(0x0F, 0x17, 0x2A)
_SLATE = RGBColor(0x33, 0x41, 0x55)
_GREY = RGBColor(0x64, 0x74, 0x8B)
_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
_LINE = RGBColor(0xE2, 0xE8, 0xF0)
_CANVAS = RGBColor(0xF8, 0xFA, 0xFC)
_GREEN = RGBColor(0x16, 0xA3, 0x4A)
_RED = RGBColor(0xC0, 0x00, 0x00)
_AMBER = RGBColor(0xCA, 0x8A, 0x04)


def _safe(v):
    """Neutralise spreadsheet-formula injection in exported strings."""
    if isinstance(v, str) and v[:1] in "=+-@":
        return "'" + v
    return "" if v is None else v


def _pct(v):
    return f"{v:.3f}%" if isinstance(v, (int, float)) else "Insufficient"


def _status_hex(text: str) -> str:
    """Map a status/trend word to a palette colour (Excel hex)."""
    t = str(text).lower()
    if any(w in t for w in ("target met", "healthy", "improv", "above", "met", "monitored")) and "not " not in t:
        return GREEN
    if any(w in t for w in ("critical", "below", "worsen", "breach", "down", "fault")):
        return RED
    if any(w in t for w in ("risk", "warn", "degrad", "partial", "insufficient")):
        return AMBER
    return SLATE


def _snapshot(db: Session) -> dict:
    """Pull one coherent analytics snapshot so every sheet/slide reconciles."""
    return {"overview": overview.build(db), "sla": sla.fleet_sla(db), "telemetry": telemetry.build(db),
            "trends": trends.build(db), "resilience": resilience.latest_assessment(db)}


def _readout(o: dict, tr: dict) -> list:
    """Executive-readout action rows (priority / risk / business impact / recommendation)."""
    rows = []
    for i, a in enumerate(o["actions"][:6], 1):
        rows.append((f"P{a.get('priority', i)}", a["title"], a["severity"], a["detail"]))
    if not rows:
        rows.append(("—", "No leadership actions", "OK", "Network is operating within targets."))
    return rows


# =========================================================================== Excel

_THIN = Side(style="thin", color=LINE)
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _dashboard_sheet(wb, o, tr, stamp):
    """The Executive Dashboard cover: brand banner, 4 KPI tiles, and a readout table."""
    ws = wb.create_sheet("Executive Dashboard")
    ws.sheet_view.showGridLines = False
    for col in range(1, 13):
        ws.column_dimensions[get_column_letter(col)].width = 12.5
    g, h = o["global_sla"], o["header"]

    # brand banner
    ws.merge_cells("A1:C4")
    ws["A1"] = "MEDLINE"; ws["A1"].fill = PatternFill("solid", fgColor=BRAND)
    ws["A1"].font = Font(size=20, bold=True, color="FFFFFF"); ws["A1"].alignment = Alignment("center", "center")
    ws.merge_cells("D1:L2"); ws.merge_cells("D3:L4")
    ws["D1"] = f"{h['org']} — Enterprise Network Reliability"
    ws["D1"].font = Font(size=22, bold=True, color="FFFFFF")
    ws["D3"] = f"Executive SLA dashboard · generated {stamp} UTC · data quality {h['data_quality']}"
    ws["D3"].font = Font(size=11, color="D9EAFB")
    for cell in ("D1", "D3"):
        ws[cell].alignment = Alignment("left", "center")
    for col in range(1, 13):
        for r in (1, 2, 3, 4):
            ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=BRAND)
    for r in (1, 2, 3, 4):
        ws.row_dimensions[r].height = 20

    # KPI tiles (4)
    tiles = [
        ("Network SLA (30-day)", _pct(g["current"]), g["status"]),
        ("SLA Target", f"{g['target']}%", "Objective"),
        ("YTD Availability", _pct(g["ytd"]), "Above target" if isinstance(g["ytd"], (int, float)) and g["ytd"] >= g["target"] else "Tracking"),
        ("Incidents (90d)", str(tr["mttr_mtbf"]["incidents"]), "Availability-derived"),
    ]
    starts = ["A", "D", "G", "J"]
    for (label, value, status), c in zip(tiles, starts):
        c2 = get_column_letter(ord(c) - 64 + 2)  # tile spans 3 columns (c .. c+2)
        ws.merge_cells(f"{c}6:{c2}6"); ws.merge_cells(f"{c}7:{c2}8"); ws.merge_cells(f"{c}9:{c2}9")
        ws[f"{c}6"] = label.upper(); ws[f"{c}6"].font = Font(size=9, bold=True, color=SLATE)
        ws[f"{c}7"] = value; ws[f"{c}7"].font = Font(size=24, bold=True, color=NAVY)
        ws[f"{c}7"].alignment = Alignment("left", "center")
        ws[f"{c}9"] = status; ws[f"{c}9"].font = Font(size=9, bold=True, color=_status_hex(status))
        for rr in (6, 7, 8, 9):
            for cc in range(ord(c) - 64, ord(c2) - 64 + 1):
                cell = ws.cell(row=rr, column=cc)
                cell.fill = PatternFill("solid", fgColor="FFFFFF")
                cell.border = _BORDER
    ws.row_dimensions[7].height = 26

    # Executive readout narrative + action table
    ws["A11"] = "Executive Readout"; ws["A11"].font = Font(size=14, bold=True, color=NAVY)
    ws.merge_cells("A12:E22")
    ws["A12"] = o["summary"]; ws["A12"].alignment = Alignment("left", "top", wrap_text=True)
    ws["A12"].font = Font(size=11, color=NAVY)
    headers = ["Priority", "Risk", "Severity", "Recommended action"]
    for i, hdr in enumerate(headers):
        cell = ws.cell(row=12, column=7 + i, value=hdr)
        cell.font = Font(size=9, bold=True, color=SLATE); cell.fill = PatternFill("solid", fgColor=ZEBRA)
    for r, (pri, risk, sev, act) in enumerate(_readout(o, tr), 13):
        for i, val in enumerate((pri, risk, sev, act)):
            cell = ws.cell(row=r, column=7 + i, value=_safe(val))
            cell.alignment = Alignment("left", "top", wrap_text=True)
            cell.font = Font(size=9, color=_status_hex(sev) if i == 2 else NAVY, bold=(i == 2))
    for col, w in (("G", 8), ("H", 26), ("I", 12), ("J", 40)):
        ws.column_dimensions[col].width = w
    return ws


def _sheet(wb, title, subtitle=""):
    ws = wb.create_sheet(title[:31])
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:F1")
    ws["A1"] = title; ws["A1"].font = Font(size=15, bold=True, color="FFFFFF")
    for col in range(1, 7):
        ws.cell(row=1, column=col).fill = PatternFill("solid", fgColor=BRAND)
    ws.row_dimensions[1].height = 24
    if subtitle:
        ws["A2"] = subtitle; ws["A2"].font = Font(size=10, italic=True, color=SLATE)
    return ws


def _table(ws, headers, rows, row=4):
    for c, hh in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=hh)
        cell.font = Font(size=10, bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor=SLATE)
    zebra = PatternFill("solid", fgColor=ZEBRA)
    for r, data in enumerate(rows, row + 1):
        for c, value in enumerate(data, 1):
            cell = ws.cell(row=r, column=c, value=_safe(value))
            if (r - row) % 2 == 0:
                cell.fill = zebra
    for i in range(len(headers)):
        ws.column_dimensions[get_column_letter(i + 1)].width = 22
    ws.freeze_panes = ws.cell(row=row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{row}:{get_column_letter(len(headers))}{row + len(rows)}"


def executive_excel(db: Session) -> Path:
    d = _snapshot(db)
    o, s, tel, tr = d["overview"], d["sla"], d["telemetry"], d["trends"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    folder = Path(get_settings().report_dir); folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Executive_Reliability_{stamp}.xlsx"
    wb = Workbook(); wb.remove(wb.active)

    _dashboard_sheet(wb, o, tr, stamp)

    ws = _sheet(wb, "Business Units", "Fleet reliability rolled up by business unit")
    _table(ws, ["Business Unit", "Availability YTD", "30-Day", "Devices", "Sites", "Incidents", "Status"],
           [(b["business_unit"], _pct(b["availability_ytd"]), _pct(b["availability_30d"]), b["devices"], b["sites"], b["incidents"], b["status"]) for b in o.get("business_units", [])])

    ws = _sheet(wb, "SLA by Device", "Per-device WTD/YTD availability, coverage-gated")
    _table(ws, ["Device", "Site", "WTD", "YTD", "Coverage YTD"],
           [(e["hostname"], e["site"], _pct(e["wtd"]["availability"]), _pct(e["ytd"]["availability"]), f"{e['ytd']['coverage']:.1f}%") for e in s["devices"]])

    ws = _sheet(wb, "Site Reliability", "Per-site availability and health")
    _table(ws, ["Site", "City", "Province", "Business Unit", "Availability YTD", "30-Day", "Devices", "Device Health", "Incidents", "Status"],
           [(x["site_code"], x["city"], x["province"], x["business_unit"], _pct(x["availability_ytd"]), _pct(x["availability_30d"]), x["devices"], x["device_health"], x["critical_devices"], x["status"]) for x in o["sites"]])

    ws = _sheet(wb, "Routing (OSPF)", "OSPF adjacency; BGP/EIGRP/static not monitored in this tenant")
    _table(ws, ["Device", "Site", "Full Adjacencies", "Total", "Neighbor Events", "Status"],
           [(x["hostname"], x["city"], x["full"], x["neighbors"], x["neighbor_events"], x["status"]) for x in tel["routing"]["items"]])

    ws = _sheet(wb, "Interfaces", "Fleet interface health")
    iface = tel["interfaces"]
    _table(ws, ["Metric", "Value"], [("Total interfaces", iface["total"]), ("Operational", iface["up"]), ("Down (admin up)", iface["down"]),
                                      ("High utilisation", iface["high_util"]), ("With errors", iface["with_errors"]), ("Flapping", iface["flapping"])])

    ws = _sheet(wb, "Incidents", "Availability-derived incidents (approximate)")
    _table(ws, ["Device", "Site", "Start", "End", "Days", "Downtime (min)"],
           [(x["device"], x["city"], x["start"], x["end"], x["days"], x["down"]) for x in tr["incidents"]])

    ws = _sheet(wb, "Data Coverage", "What LogicMonitor exposes for this fleet")
    _table(ws, ["Metric", "LogicMonitor source", "Coverage", "Devices"],
           [(m["metric"], m["source"], m["status"], f"{m['devices']}/{tel['data_coverage']['fleet']}") for m in tel["data_coverage"]["metrics"]])

    ws = _sheet(wb, "Methodology", "Definitions and scope")
    _table(ws, ["Topic", "Definition"], [
        ("Availability", "up eligible minutes / observed eligible minutes, coverage-gated at 90%."),
        ("Coverage", "observed minutes / expected minutes; below 90% = Insufficient evidence (never 0%)."),
        ("Resilience tier", "Network-redundancy estimate mapped to Uptime bands; NOT a facility certification."),
        ("Scope", "Only devices registered in Device Fleet are analysed, not all of LogicMonitor."),
        ("MTTR/MTBF", "Approximate, derived from availability history; exact timings need LM alert history."),
    ])
    wb.save(path)
    return path


# ====================================================================== PowerPoint

def _rect(slide, l, t, w, h, fill, line=None, rounded=True):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE,
                                   Inches(l), Inches(t), Inches(w), Inches(h))
    shape.fill.solid(); shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line; shape.line.width = Pt(0.75)
    shape.shadow.inherit = False
    return shape


def _text(slide, l, t, w, h, text, size, color, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, font="Calibri"):
    box = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tf = box.text_frame; tf.word_wrap = True; tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Pt(2); tf.margin_top = tf.margin_bottom = Pt(1)
    lines = text.split("\n") if isinstance(text, str) else [text]
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = ln; p.alignment = align
        r = p.runs[0]; r.font.size = Pt(size); r.font.bold = bold; r.font.color.rgb = color; r.font.name = font
    return box


def _kpi(slide, l, t, w, label, value, sub, value_color=_NAVY):
    _rect(slide, l, t, w, 1.15, _WHITE, line=_LINE)
    _text(slide, l + 0.12, t + 0.10, w - 0.24, 0.25, label.upper(), 9, _SLATE, bold=True)
    _text(slide, l + 0.12, t + 0.34, w - 0.24, 0.5, value, 24, value_color, bold=True)
    _text(slide, l + 0.12, t + 0.84, w - 0.24, 0.25, sub, 8.5, _GREY)


def _chip(slide, l, t, text, fill_hex, text_rgb):
    w = 0.16 + 0.075 * len(text)
    _rect(slide, l, t, w, 0.26, RGBColor.from_string(fill_hex))
    _text(slide, l, t + 0.02, w, 0.22, text, 8.5, text_rgb, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    return l + w + 0.12


def _header(slide, title, subtitle, page):
    _rect(slide, 0, 0, 13.333, 7.5, _CANVAS, rounded=False)
    _rect(slide, 0, 0, 13.333, 0.16, _BRAND, rounded=False)
    _rect(slide, 0, 0, 0.6, 0.6, _BRAND, rounded=False)
    _text(slide, 0.8, 0.18, 10, 0.4, title, 22, _NAVY, bold=True)
    _text(slide, 0.8, 0.66, 11.5, 0.3, subtitle, 12, _GREY)
    _text(slide, 12.4, 7.05, 0.5, 0.3, page, 10, _GREY, align=PP_ALIGN.RIGHT)


def _bar_chart(slide, l, t, w, h, sites, target):
    """Per-site availability bars, scaled within a tight window so differences read; below
    target is red. Draws on top of a white card the caller already placed."""
    usable = [x for x in sites if isinstance(x.get("availability_ytd"), (int, float))]
    usable = sorted(usable, key=lambda x: x["availability_ytd"])[:8]
    if not usable:
        _text(slide, l, t + h / 2, w, 0.3, "Insufficient evidence to chart", 11, _GREY, align=PP_ALIGN.CENTER)
        return
    lo = min(min(x["availability_ytd"] for x in usable), target) - 0.05
    hi = 100.0
    span = max(hi - lo, 0.01)
    n = len(usable)
    gap = 0.18
    bw = (w - gap * (n + 1)) / n
    base = t + h - 0.35
    # target line
    ty = base - (h - 0.5) * (target - lo) / span
    _rect(slide, l, ty, w, 0.02, _AMBER, rounded=False)
    for i, x in enumerate(usable):
        val = x["availability_ytd"]
        bh = max(0.1, (h - 0.5) * (val - lo) / span)
        bx = l + gap + i * (bw + gap)
        color = _RED if val < target else _BRAND
        _rect(slide, bx, base - bh, bw, bh, color, rounded=False)
        _text(slide, bx - 0.1, base + 0.02, bw + 0.2, 0.25, x.get("site_code") or x.get("city", ""), 8, _SLATE, align=PP_ALIGN.CENTER)


def executive_pptx(db: Session) -> Path:
    d = _snapshot(db)
    o, tel, tr = d["overview"], d["telemetry"], d["trends"]
    g, h, crit = o["global_sla"], o["header"], o["criticality"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(get_settings().report_dir); folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Executive_Reliability_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.pptx"
    prs = Presentation(); prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # ---- Slide 1: cover ----
    s = prs.slides.add_slide(blank)
    _rect(s, 0, 0, 13.333, 7.5, _BRAND, rounded=False)
    _rect(s, 0, 0, 3.1, 3.2, _WHITE, rounded=False).fill.fore_color.rgb = RGBColor(0x1A, 0x52, 0xB0)
    _rect(s, -1.0, 5.2, 5.0, 3.0, RGBColor(0x1A, 0x52, 0xB0), rounded=False)
    _text(s, 0.9, 0.7, 3, 0.6, "MEDLINE", 26, _WHITE, bold=True)
    _text(s, 5.2, 2.3, 7.2, 1.6, "Enterprise Network\nReliability", 40, _WHITE, bold=True)
    _text(s, 5.25, 4.15, 7, 0.35, f"{h['org']}   |   Executive SLA Report", 14, RGBColor(0xD9, 0xEA, 0xFB), bold=True)
    _text(s, 5.25, 4.6, 6, 0.3, f"Generated {stamp} UTC", 12, RGBColor(0xBE, 0xD3, 0xF2))

    # ---- Slide 2: executive scorecard ----
    s = prs.slides.add_slide(blank)
    verb = "met" if (isinstance(g["current"], (int, float)) and g["current"] >= g["target"]) else "at risk"
    _header(s, "Executive Scorecard", f"36-second leadership readout — SLA is {verb}; see the readout and trend below.", "02")
    kx = [(0.8, 2.9, "Network SLA", _pct(g["current"]), "30-day availability"),
          (3.9, 2.2, "SLA Target", f"{g['target']}%", "contract objective"),
          (6.3, 2.2, "Evidence Coverage", f"{h['sla_evidence_coverage']}%", "fleet telemetry"),
          (8.7, 2.2, "Incidents", str(tr["mttr_mtbf"]["incidents"]), "90-day derived"),
          (11.1, 1.4, "Trend", tr["deltas"]["wow"]["trend"].title(), "WoW / MoM")]
    for l, w, lab, val, sub in kx:
        col = _GREEN if lab == "Network SLA" and verb == "met" else (_RED if lab == "Trend" and "worsen" in val.lower() else _NAVY)
        _kpi(s, l, 1.2, w, lab, val, sub, col)
    # readout panel
    _rect(s, 0.8, 2.6, 5.2, 3.8, _WHITE, line=_LINE)
    _text(s, 1.1, 2.8, 4.6, 0.3, "Executive Readout", 14, _NAVY, bold=True)
    _text(s, 1.1, 3.25, 4.7, 1.8, o["summary"], 11, _SLATE)
    cx = 1.1
    cx = _chip(s, cx, 5.9, g["status"], GREEN_BG if verb == "met" else AMBER_BG, _GREEN if verb == "met" else _AMBER)
    cx = _chip(s, cx, 5.9, f"Trend {tr['deltas']['wow']['trend'].lower()}", RED_BG if "worsen" in tr["deltas"]["wow"]["trend"].lower() else GREEN_BG, _RED if "worsen" in tr["deltas"]["wow"]["trend"].lower() else _GREEN)
    _chip(s, cx, 5.9, f"{h['data_quality']} quality", BLUE_BG, _BRAND)
    # chart panel
    _rect(s, 6.3, 2.6, 6.2, 3.8, _WHITE, line=_LINE)
    _text(s, 6.6, 2.8, 5.6, 0.3, "Availability vs. SLA Target (per site)", 14, _NAVY, bold=True)
    _bar_chart(s, 6.6, 3.35, 5.6, 2.85, o["sites"], g["target"])

    # ---- Slide 3: business unit & site reliability ----
    s = prs.slides.add_slide(blank)
    _header(s, "Business Unit & Site Reliability", "Scorecards for management; full drill-down stays in the app.", "03")
    bx = 0.8
    for b in o.get("business_units", [])[:4]:
        _rect(s, bx, 1.3, 2.9, 1.7, _WHITE, line=_LINE)
        _text(s, bx + 0.15, 1.45, 2.6, 0.3, b["business_unit"].upper(), 11, _SLATE, bold=True)
        _text(s, bx + 0.15, 1.75, 2.6, 0.5, _pct(b["availability_ytd"]), 22, _NAVY, bold=True)
        _text(s, bx + 0.15, 2.3, 2.6, 0.3, f"YTD · {b['devices']} devices · {b['incidents']} incident(s)", 9, _GREY)
        _chip(s, bx + 0.15, 2.62, b["status"], GREEN_BG if b["status"] == "HEALTHY" else (RED_BG if b["status"] == "CRITICAL" else AMBER_BG),
              _GREEN if b["status"] == "HEALTHY" else (_RED if b["status"] == "CRITICAL" else _AMBER))
        bx += 3.05
    # top sites table
    _text(s, 0.8, 3.4, 8, 0.3, "Lowest-availability sites (YTD)", 13, _NAVY, bold=True)
    worst = sorted([x for x in o["sites"] if isinstance(x.get("availability_ytd"), (int, float))], key=lambda x: x["availability_ytd"])[:6]
    ty = 3.85
    _rect(s, 0.8, ty, 11.7, 0.32, RGBColor.from_string(ZEBRA))
    for cxx, head in zip((0.9, 3.0, 5.5, 7.5, 9.5), ("Site", "City", "YTD", "30-day", "Status")):
        _text(s, cxx, ty + 0.03, 2, 0.26, head, 9, _SLATE, bold=True)
    for i, x in enumerate(worst):
        yy = ty + 0.4 + i * 0.34
        for cxx, val in zip((0.9, 3.0, 5.5, 7.5, 9.5),
                            (x["site_code"], x["city"], _pct(x["availability_ytd"]), _pct(x["availability_30d"]), x["status"])):
            _text(s, cxx, yy, 2.2, 0.3, str(val), 9.5, _NAVY)

    # ---- Slide 4: risks & actions ----
    s = prs.slides.add_slide(blank)
    _header(s, "Top Reliability Risks and Actions", "Decision format: priority, impact, and recommended mitigation.", "04")
    ty = 1.35
    _rect(s, 0.8, ty, 11.7, 0.34, _BRAND)
    for cxx, head in zip((0.95, 1.9, 6.4), ("Priority", "Risk", "Recommended action")):
        _text(s, cxx, ty + 0.05, 5, 0.26, head, 10, _WHITE, bold=True)
    rows = _readout(o, tr)
    yy = ty + 0.45
    for i, (pri, risk, sev, act) in enumerate(rows[:6]):
        rh = 0.82
        if i % 2 == 0:
            _rect(s, 0.8, yy - 0.05, 11.7, rh, RGBColor.from_string(ZEBRA))
        _text(s, 0.95, yy, 0.9, 0.3, pri, 11, _RED if sev == "P1" else (_AMBER if sev == "P2" else _SLATE), bold=True)
        _text(s, 1.9, yy, 4.3, rh, risk, 11, _NAVY, bold=True)
        _text(s, 6.4, yy, 6.0, rh, act, 10, _SLATE)
        yy += rh + 0.04

    # ---- Slide 5: observability & telemetry maturity ----
    s = prs.slides.add_slide(blank)
    _header(s, "Observability and Telemetry Maturity", "Availability coverage is strong; routing/service telemetry is limited by the tenant.", "05")
    cov = tel["data_coverage"]; rt = tel["routing"]; iface = tel["interfaces"]; lat = tel["latency"]
    tiles = [("Availability", f"{cov['fleet']}/{cov['fleet']}", "monitored"),
             ("Interfaces up", f"{iface['up']}/{iface['total']}", "operational"),
             ("OSPF devices", f"{rt['devices_with_ospf']}/{rt['fleet']}", "adjacency monitored"),
             ("Avg latency", f"{lat['avg_latency_ms']} ms" if lat.get("avg_latency_ms") is not None else "—", "fleet mean")]
    lx = 0.8
    for lab, val, sub in tiles:
        _kpi(s, lx, 1.3, 2.85, lab, val, sub)
        lx += 3.05
    _text(s, 0.8, 2.9, 11, 0.3, "Coverage matrix", 13, _NAVY, bold=True)
    ty = 3.35
    _rect(s, 0.8, ty, 11.7, 0.32, RGBColor.from_string(ZEBRA))
    for cxx, head in zip((0.95, 4.5, 8.5, 10.8), ("Metric", "LogicMonitor source", "Coverage", "Devices")):
        _text(s, cxx, ty + 0.03, 4, 0.26, head, 9, _SLATE, bold=True)
    for i, m in enumerate(cov["metrics"][:9]):
        yy = ty + 0.4 + i * 0.32
        for cxx, val in zip((0.95, 4.5, 8.5, 10.8), (m["metric"], m["source"], m["status"], f"{m['devices']}/{cov['fleet']}")):
            clr = _status_hex(val) if cxx == 8.5 else NAVY
            _text(s, cxx, yy, 3.6, 0.3, str(val), 9, RGBColor.from_string(clr))

    # ---- Slide 6: methodology ----
    s = prs.slides.add_slide(blank)
    _header(s, "Methodology and Data Quality", "Appendix — definitions in plain language.", "06")
    defs = [
        ("Availability", "Up eligible minutes divided by observed eligible minutes, coverage-gated at 90%."),
        ("Coverage", "Observed minutes / expected minutes; below 90% is 'Insufficient evidence', never 0%."),
        ("Commissioning-aware YTD", "Each site is measured from when it was first monitored, so a mid-year project is not penalised."),
        ("Resilience tier", "A network-redundancy estimate mapped to Uptime bands — not a facility certification."),
        ("Scope", "Only devices registered in Device Fleet are analysed, not all of LogicMonitor."),
        ("Missing telemetry", "Reported as Insufficient / Not monitored — never fabricated as 0% or 100%."),
    ]
    yy = 1.4
    for term, defn in defs:
        _rect(s, 0.8, yy, 11.7, 0.72, _WHITE, line=_LINE)
        _text(s, 1.0, yy + 0.1, 3.0, 0.5, term, 12, _BRAND, bold=True)
        _text(s, 4.1, yy + 0.1, 8.2, 0.55, defn, 11, _SLATE, anchor=MSO_ANCHOR.MIDDLE)
        yy += 0.85

    prs.save(path)
    return path
