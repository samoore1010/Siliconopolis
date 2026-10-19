"""Spreadsheet templates generated with openpyxl, plus an HTML preview for listing images.

spec = {"title": str, "sheets": [{"name": str,
          "columns": [str | {"name", "type", "formula", "width"}],
          "rows": int, "totals": bool, "sample_rows": [[...]]}]}
Column types: text, date, currency, number, percent, checkbox, formula. `formula` uses {r} for the row.
The delivered workbook is blank (formulas and validation only); sample_rows appear in the preview.
"""

from __future__ import annotations

import html as html_lib
import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from pipeline.generators.mockups import short_title

NUMBER_FORMATS = {"date": "yyyy-mm-dd", "currency": '"$"#,##0.00', "number": "#,##0.00", "percent": "0.0%"}
SUMMABLE = ("currency", "number")
HEADER_ROW = 3


def normalize_columns(cols: list[Any]) -> list[dict[str, Any]]:
    out = []
    for c in cols[:20]:
        if isinstance(c, dict):
            name = str(c.get("name") or "Column")
            ctype = str(c.get("type") or "text").lower()
            fmt = str(c.get("format") or "").lower() or None
            out.append({"name": name, "type": ctype if ctype in (*NUMBER_FORMATS, "text", "checkbox", "formula") else "text",
                        "formula": c.get("formula"), "format": fmt if fmt in NUMBER_FORMATS else None,
                        "width": c.get("width")})
        else:
            name = str(c)
            lowered = name.lower()
            ctype = "checkbox" if lowered.endswith("?") else "date" if "date" in lowered \
                else "currency" if any(k in lowered for k in MONEY_WORDS) else "text"
            out.append({"name": name, "type": ctype, "formula": None, "format": None, "width": None})
    return out or [{"name": "Column A", "type": "text", "formula": None, "format": None, "width": None}]


def _hex(c: str, default: str) -> str:
    c = (c or default).lstrip("#")
    return c if len(c) == 6 else default.lstrip("#")


def render_spreadsheet(spec: dict[str, Any], *, palette: list[str] | None = None) -> bytes:
    palette = palette or ["#F7F3EC", "#2B2B2B", "#A3B18A"]
    accent, ink = _hex(palette[2] if len(palette) > 2 else "", "#A3B18A"), _hex(palette[1] if len(palette) > 1 else "", "#2B2B2B")
    header_fill = PatternFill("solid", fgColor=accent)
    band_fill = PatternFill("solid", fgColor="F7F7F5")
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    wb.remove(wb.active)
    sheets = spec.get("sheets") or [{"name": "Sheet1", "columns": ["Date", "Item", "Amount"]}]
    for sheet in sheets[:8]:
        ws = wb.create_sheet(title=str(sheet.get("name", "Sheet"))[:31])
        cols = normalize_columns(sheet.get("columns", []))
        rows = max(5, min(int(sheet.get("rows") or 50), 500))
        first, last = HEADER_ROW + 1, HEADER_ROW + rows
        ws["A1"] = spec.get("title", "Template")
        ws["A1"].font = Font(bold=True, size=16, color=ink)
        ws["A2"] = str(sheet.get("name", ""))
        ws["A2"].font = Font(italic=True, color="888888")
        checkbox_dv = DataValidation(type="list", formula1='"☐,☑"', allow_blank=True)
        ws.add_data_validation(checkbox_dv)
        for i, col in enumerate(cols, start=1):
            letter = get_column_letter(i)
            cell = ws.cell(row=HEADER_ROW, column=i, value=col["name"])
            cell.fill, cell.font, cell.border = header_fill, header_font, border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[letter].width = int(col["width"] or max(14, min(40, len(col["name"]) + 6)))
            for r in range(first, last + 1):
                c = ws.cell(row=r, column=i)
                c.border = border
                if (r - first) % 2:
                    c.fill = band_fill
                if col["type"] in NUMBER_FORMATS:
                    c.number_format = NUMBER_FORMATS[col["type"]]
                if col["type"] == "formula" and col.get("formula"):
                    c.value = str(col["formula"]).replace("{r}", str(r))
                    c.number_format = NUMBER_FORMATS.get(_display_type(col), "General")
            if col["type"] == "checkbox":
                checkbox_dv.add(f"{letter}{first}:{letter}{last}")
                for r in range(first, last + 1):
                    ws.cell(row=r, column=i).alignment = Alignment(horizontal="center")
        if sheet.get("totals", True):
            for i, col in enumerate(cols, start=1):
                if col["type"] in SUMMABLE:
                    letter = get_column_letter(i)
                    tc = ws.cell(row=last + 1, column=i, value=f"=SUM({letter}{first}:{letter}{last})")
                    tc.font, tc.number_format = Font(bold=True), NUMBER_FORMATS[col["type"]]
            ws.cell(row=last + 1, column=1, value="Total").font = Font(bold=True)
        ws.freeze_panes = f"A{first}"
        ws.row_dimensions[HEADER_ROW].height = 28
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


MONEY_WORDS = ("amount", "income", "expense", "profit", "total", "cost", "price", "balance", "revenue", "budget", "$",
               "outstanding", "owed", "billed", "fee", "spent", "salary", "payment", "deposit")


PERCENT_WORDS = ("margin", "%", "percent", "rate", "change", "growth", "share", "ratio")


def _display_type(col: dict[str, Any]) -> str:
    if col.get("format"):
        return col["format"]
    if col["type"] in ("formula", "number"):
        name = col["name"].lower()
        if any(w in name for w in PERCENT_WORDS):
            return "percent"
        if any(w in name for w in MONEY_WORDS):
            return "currency"
    return col["type"]


def _fmt(value: Any, ctype: str) -> str:
    if value is None or value == "":
        return ""
    if ctype == "currency":
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
    if ctype == "percent":
        try:
            v = float(value)
            return f"{v * 100:.0f}%" if abs(v) <= 1.5 else f"{v:.0f}%"
        except (TypeError, ValueError):
            return str(value)
    if ctype == "checkbox":
        return "☑" if str(value).lower() in ("true", "yes", "1", "x", "☑", "paid", "done") else "☐"
    return str(value)


def preview_html(spec: dict[str, Any], *, palette: list[str] | None = None, sheets: list[int] | None = None,
                 subtitle: str | None = None) -> str:
    """A clean, spreadsheet-app-style preview (tabs, header row, example data) for listing images."""
    palette = palette or ["#F7F3EC", "#2B2B2B", "#A3B18A"]
    bg, ink, accent = (palette + ["#F7F3EC", "#2B2B2B", "#A3B18A"])[:3]
    esc = html_lib.escape
    all_sheets = spec.get("sheets") or []
    idx = [i for i in (sheets if sheets is not None else range(min(2, len(all_sheets)))) if i < len(all_sheets)]
    chosen = [all_sheets[i] for i in idx]
    active = idx[0] if idx else 0
    tabs = "".join(f"<span class='tab{' on' if i == active else ''}'>{esc(str(s.get('name', 'Sheet')))}</span>" for i, s in enumerate(all_sheets))
    widest = max((len(normalize_columns(s.get("columns", []))) for s in chosen), default=1)
    font = 14 if widest <= 6 else 12 if widest <= 8 else 11
    blocks = []
    for s in chosen:
        cols = normalize_columns(s.get("columns", []))[:9]
        head = "".join(f"<th class='{_display_type(c)}'>{esc(c['name'])}</th>" for c in cols)
        body = ""
        for row in (s.get("sample_rows") or [])[:6]:
            cells = list(row) + [""] * len(cols)
            body += "<tr>" + "".join(
                f"<td class='{_display_type(c)}'>{esc(_fmt(cells[i], _display_type(c)))}</td>" for i, c in enumerate(cols)) + "</tr>"
        body += "<tr class='empty'>" + "<td></td>" * len(cols) + "</tr>"
        blocks.append(f"<div class='sheet'><div class='sname'>{esc(str(s.get('name', '')))}</div>"
                      f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
body{{margin:0;background:{bg};font-family:Inter,'DejaVu Sans',sans-serif;color:{ink}}}
.badge{{display:inline-block;margin-top:14px;padding:6px 12px;border-radius:999px;background:{accent}22;color:{ink};font-size:14px}}
.wrap{{padding:48px}} h1{{font-family:Poppins,Inter,sans-serif;font-size:40px;margin:0 0 6px}}
.sub{{opacity:.65;font-size:18px;margin-bottom:22px}}
.win{{background:#fff;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.14);overflow:hidden}}
.bar{{height:34px;background:#EEECE8;display:flex;align-items:center;gap:8px;padding:0 14px}}
.dot{{width:11px;height:11px;border-radius:50%;background:#D6D2CB}}
.tabs{{display:flex;gap:4px;padding:10px 14px 0;background:#F6F5F2;border-bottom:1px solid #E4E1DC;flex-wrap:wrap}}
.tab{{padding:7px 16px;font-size:14px;border-radius:8px 8px 0 0;background:#ECEAE6}}
.tab.on{{background:{accent};color:#fff;font-weight:600}}
.sheet{{padding:16px 18px 6px}} .sname{{font-weight:600;font-size:15px;margin:0 0 8px;color:{accent}}}
table{{border-collapse:collapse;width:100%;font-size:{font}px;table-layout:auto}}
th{{background:{accent};color:#fff;text-align:left;padding:9px 10px;font-weight:600}}
td{{padding:8px 10px;border-bottom:1px solid #EEE}} tr:nth-child(even) td{{background:#FAFAF8}}
td.currency,td.number,td.percent,td.formula,th.currency,th.number,th.percent,th.formula{{text-align:right;font-variant-numeric:tabular-nums}}
th.checkbox{{text-align:center}}
td.checkbox{{text-align:center;color:{accent};font-size:17px}} tr.empty td{{height:22px}}
</style></head><body><div class='wrap'><h1>{esc(short_title(str(spec.get('title', 'Template'))))}</h1>
<div class='sub'>{esc(subtitle or 'Editable spreadsheet · Excel & Google Sheets · formulas included')}</div>
<span class='badge'>Example data shown · your file arrives blank, ready for your numbers</span><div style='height:18px'></div>
<div class='win'><div class='bar'><span class='dot'></span><span class='dot'></span><span class='dot'></span></div>
<div class='tabs'>{tabs}</div>{''.join(blocks)}</div></div></body></html>"""
