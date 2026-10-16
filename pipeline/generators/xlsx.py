"""Spreadsheet templates generated with openpyxl."""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def render_spreadsheet(spec: dict[str, Any], *, palette: list[str] | None = None) -> bytes:
    """spec: {title, sheets: [{name, columns: [str]}]}"""
    palette = palette or ["#F7F3EC", "#2B2B2B", "#A3B18A"]
    header_fill = PatternFill("solid", fgColor=palette[2].lstrip("#") if len(palette) > 2 else "A3B18A")
    header_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    wb.remove(wb.active)
    sheets = spec.get("sheets") or [{"name": "Sheet1", "columns": ["Date", "Item", "Amount"]}]
    for sheet in sheets[:8]:
        ws = wb.create_sheet(title=str(sheet.get("name", "Sheet"))[:31])
        ws["A1"] = spec.get("title", "Template")
        ws["A1"].font = Font(bold=True, size=14)
        cols = [str(c) for c in sheet.get("columns", [])][:20] or ["Column A"]
        for i, col in enumerate(cols, start=1):
            cell = ws.cell(row=3, column=i, value=col)
            cell.fill, cell.font, cell.border = header_fill, header_font, border
            cell.alignment = Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(i)].width = max(14, len(col) + 4)
        for r in range(4, 44):
            for i in range(1, len(cols) + 1):
                ws.cell(row=r, column=i).border = border
        # totals for amount-like columns
        for i, col in enumerate(cols, start=1):
            if "amount" in col.lower() or col.lower() in ("income", "expenses", "profit", "total"):
                letter = get_column_letter(i)
                ws.cell(row=45, column=i, value=f"=SUM({letter}4:{letter}43)").font = Font(bold=True)
                ws.cell(row=45, column=max(1, i - 1), value="Total").font = Font(bold=True)
        ws.freeze_panes = "A4"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
