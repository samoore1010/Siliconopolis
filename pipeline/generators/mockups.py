"""Listing images: primary artwork + simple room/frame mockups rendered with Pillow."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

LISTING_SIZE = (2000, 2000)  # Etsy recommends >= 2000px on the shortest side


def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:  # pragma: no cover
        return ImageFont.load_default()


def _to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def fit(img: Image.Image, box: tuple[int, int]) -> Image.Image:
    img = img.copy()
    img.thumbnail(box, Image.LANCZOS)
    return img


def framed_mockup(art: Image.Image, *, wall: str = "#EDE7DF", frame: str = "#1F1F1F", mat: int = 60) -> bytes:
    W, H = LISTING_SIZE
    canvas = Image.new("RGB", (W, H), wall)
    d = ImageDraw.Draw(canvas)
    # soft floor line
    d.rectangle([0, int(H * 0.82), W, H], fill="#D9D2C7")
    inner = fit(art, (int(W * 0.5), int(H * 0.6)))
    fw, fh = inner.width + 2 * mat + 40, inner.height + 2 * mat + 40
    fx, fy = (W - fw) // 2, int(H * 0.12)
    d.rectangle([fx + 25, fy + 35, fx + fw + 25, fy + fh + 35], fill="#C9C2B8")  # shadow
    d.rectangle([fx, fy, fx + fw, fy + fh], fill=frame)
    d.rectangle([fx + 20, fy + 20, fx + fw - 20, fy + fh - 20], fill="#FFFFFF")
    canvas.paste(inner, (fx + 20 + mat, fy + 20 + mat))
    return _to_png(canvas)


def flat_lay(art: Image.Image, *, bg: str = "#F6F1EA") -> bytes:
    W, H = LISTING_SIZE
    canvas = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    inner = fit(art, (int(W * 0.7), int(H * 0.8)))
    x, y = (W - inner.width) // 2, (H - inner.height) // 2
    d.rectangle([x + 18, y + 24, x + inner.width + 18, y + inner.height + 24], fill="#DDD6CC")
    canvas.paste(inner, (x, y))
    return _to_png(canvas)


def size_chart(sizes: list[str], *, bg: str = "#FFFFFF", ink: str = "#2B2B2B", title: str = "Included sizes") -> bytes:
    W, H = LISTING_SIZE
    canvas = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    d.text((140, 140), title, fill=ink, font=_font(96))
    y = 360
    for s in sizes[:8]:
        d.rounded_rectangle([140, y, W - 140, y + 150], radius=24, outline=ink, width=4)
        d.text((190, y + 40), s, fill=ink, font=_font(64))
        y += 190
    d.text((140, H - 220), "Instant digital download • print at home or at any print shop", fill=ink, font=_font(44))
    return _to_png(canvas)


def primary_image(art: Image.Image) -> bytes:
    W, H = LISTING_SIZE
    canvas = Image.new("RGB", (W, H), "#FFFFFF")
    inner = fit(art, (W, H))
    canvas.paste(inner, ((W - inner.width) // 2, (H - inner.height) // 2))
    return _to_png(canvas)


def document_preview(pdf_title: str, sections: list[str], *, palette: list[str]) -> Image.Image:
    """Render a faux page preview for PDF/XLSX products (used as 'art' for mockups)."""
    W, H = 1700, 2200
    img = Image.new("RGB", (W, H), palette[0] if palette else "#F7F3EC")
    d = ImageDraw.Draw(img)
    ink = palette[1] if len(palette) > 1 else "#2B2B2B"
    accent = palette[2] if len(palette) > 2 else ink
    d.text((120, 120), pdf_title, fill=ink, font=_font(84))
    d.line([120, 240, W - 120, 240], fill=accent, width=4)
    y = 320
    block = (H - 420) // max(1, len(sections))
    for s in sections:
        d.text((120, y), s, fill=ink, font=_font(48))
        for ln in range(1, int(block / 70)):
            d.line([120, y + 70 * ln, W - 120, y + 70 * ln], fill="#C8C2B8", width=2)
        y += block
    return img


def upscale_for_print(art: Image.Image, *, ratio: tuple[int, int], long_side_px: int = 3600) -> bytes:
    """Resize/crop artwork to a print aspect ratio (e.g. 4:5 for 8x10/16x20) at ~300dpi."""
    rw, rh = ratio
    target_w = int(long_side_px * rw / max(rw, rh))
    target_h = int(long_side_px * rh / max(rw, rh))
    scale = max(target_w / art.width, target_h / art.height)
    resized = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    left, top = (resized.width - target_w) // 2, (resized.height - target_h) // 2
    return _to_png(resized.crop((left, top, left + target_w, top + target_h)))


def load(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")
