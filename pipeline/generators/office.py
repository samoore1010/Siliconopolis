"""Headless LibreOffice + PDF rasterising helpers.

Used to (1) recalculate a generated workbook the way a buyer's spreadsheet app would, so broken
formulas are caught before QA, (2) render the real delivered file (xlsx or pdf) to images so QA
and listing images show exactly what the buyer gets.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pipeline.logging_setup import get_logger

log = get_logger(__name__)

CONVERT_TIMEOUT = 120


class OfficeError(RuntimeError):
    pass


def soffice_bin() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    return soffice_bin() is not None


def convert(data: bytes, in_ext: str, out_fmt: str, *, timeout: int = CONVERT_TIMEOUT) -> bytes:
    """Convert a document with LibreOffice (e.g. xlsx -> xlsx to recalculate, xlsx -> pdf to render)."""
    binary = soffice_bin()
    if not binary:
        raise OfficeError("LibreOffice (soffice) is not installed")
    with tempfile.TemporaryDirectory(prefix="lo-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / f"input.{in_ext.lstrip('.')}"
        src.write_bytes(data)
        outdir = tmpdir / "out"
        outdir.mkdir()
        cmd = [binary, "--headless", "--norestore", "--nolockcheck", "--nodefault",
               f"-env:UserInstallation={(tmpdir / 'profile').as_uri()}",
               "--convert-to", out_fmt, "--outdir", str(outdir), str(src)]
        env = {**os.environ, "SAL_USE_VCLPLUGIN": "svp", "HOME": str(tmpdir)}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            raise OfficeError(f"LibreOffice timed out after {timeout}s") from e
        produced = [p for p in outdir.iterdir() if p.is_file()]
        if proc.returncode != 0 or not produced:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise OfficeError(f"LibreOffice could not open/convert the file: {detail or 'no output'}")
        return produced[0].read_bytes()


def pdf_to_pngs(pdf: bytes, *, scale: float = 1.6, max_pages: int = 12) -> list[bytes]:
    """Rasterise PDF pages (pypdfium2; no system dependencies)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    out = []
    try:
        for i in range(min(len(doc), max_pages)):
            img = doc[i].render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out.append(buf.getvalue())
    finally:
        doc.close()
    return out


def trim(png: bytes, *, pad: int = 24, bg: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    """Crop surrounding blank page margin from a rendered page."""
    from PIL import Image, ImageChops

    img = Image.open(io.BytesIO(png)).convert("RGB")
    diff = ImageChops.difference(img, Image.new("RGB", img.size, bg))
    box = diff.point(lambda v: 255 if v > 12 else 0).getbbox()
    if not box:
        return png
    l, t, r, b = box
    img = img.crop((max(0, l - pad), max(0, t - pad), min(img.width, r + pad), min(img.height, b + pad)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
