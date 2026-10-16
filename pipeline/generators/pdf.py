"""Code-generated planners / trackers / checklists as print-ready PDFs (US Letter + A4)."""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

PAGE_SIZES = {"letter": letter, "a4": A4}


def _hex(c: str):
    return colors.HexColor(c)


def render_planner_pdf(spec: dict[str, Any], *, page: str = "letter", palette: list[str] | None = None,
                       kind: str = "planner") -> bytes:
    """spec: {title, subtitle, sections: [str], pages: int}"""
    palette = palette or ["#F7F3EC", "#2B2B2B", "#A3B18A"]
    bg, ink, accent = (_hex(palette[0]), _hex(palette[1]), _hex(palette[2] if len(palette) > 2 else palette[1]))
    size = PAGE_SIZES.get(page, letter)
    w, h = size
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.setTitle(spec.get("title", "Planner"))
    sections: list[str] = spec.get("sections") or ["Priorities", "Schedule", "Notes"]
    n_pages = max(1, min(int(spec.get("pages", 1)), 12))
    margin = 0.6 * inch

    for p in range(n_pages):
        c.setFillColor(bg)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        c.setFillColor(ink)
        c.setFont("Helvetica-Bold", 24)
        c.drawString(margin, h - margin - 10, spec.get("title", "Planner"))
        c.setFont("Helvetica", 11)
        c.setFillColor(accent)
        c.drawString(margin, h - margin - 30, spec.get("subtitle", ""))
        c.setStrokeColor(accent)
        c.setLineWidth(1.2)
        c.line(margin, h - margin - 42, w - margin, h - margin - 42)

        # date / week line
        c.setFillColor(ink)
        c.setFont("Helvetica", 10)
        label = "Date: ______________" if kind != "tracker" else "Week of: ______________"
        c.drawRightString(w - margin, h - margin - 10, label)

        top = h - margin - 70
        avail = top - margin
        block_h = avail / len(sections)
        for i, sec in enumerate(sections):
            y_top = top - i * block_h
            c.setFillColor(ink)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(margin, y_top - 14, sec)
            c.setStrokeColor(colors.Color(ink.red, ink.green, ink.blue, alpha=0.35))
            c.setLineWidth(0.5)
            if kind == "tracker":
                # 7 checkbox columns
                cols = 7
                col_w = (w - 2 * margin) / cols
                for col in range(cols):
                    x = margin + col * col_w
                    c.rect(x + 4, y_top - block_h + 12, min(14, col_w - 8), 14, fill=0)
                    c.setFont("Helvetica", 8)
                    c.drawString(x + 22, y_top - block_h + 15, ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"][col])
            else:
                lines = max(2, int((block_h - 28) / 18))
                for ln in range(lines):
                    y = y_top - 30 - ln * 18
                    if y > y_top - block_h + 6:
                        c.line(margin, y, w - margin, y)
        c.setFont("Helvetica-Oblique", 7)
        c.setFillColor(accent)
        c.drawCentredString(w / 2, margin / 2, f"Page {p + 1} of {n_pages} • for personal use")
        c.showPage()
    c.save()
    return buf.getvalue()


def image_to_pdf(png_bytes: bytes, *, page: str = "letter") -> bytes:
    """Wrap an image as a single-page PDF, fitted to the page with a small margin."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    size = PAGE_SIZES.get(page, letter)
    w, h = size
    img = Image.open(io.BytesIO(png_bytes))
    iw, ih = img.size
    margin = 0.25 * inch
    scale = min((w - 2 * margin) / iw, (h - 2 * margin) / ih)
    dw, dh = iw * scale, ih * scale
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    c.drawImage(ImageReader(io.BytesIO(png_bytes)), (w - dw) / 2, (h - dh) / 2, dw, dh)
    c.showPage()
    c.save()
    return buf.getvalue()
