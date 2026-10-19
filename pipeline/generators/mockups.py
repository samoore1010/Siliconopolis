"""Listing images: primary artwork + simple room/frame mockups rendered with Pillow."""

from __future__ import annotations

import io
import re

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


def primary_image(art: Image.Image, *, bg: str | None = None) -> bytes:
    W, H = LISTING_SIZE
    if bg is None:  # extend the artwork's own edge colour so letterboxing is invisible
        r, g, b = art.convert("RGB").getpixel((2, 2))[:3]
        bg = f"#{r:02x}{g:02x}{b:02x}"
    canvas = Image.new("RGB", (W, H), bg)
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


# --------------------------------------------------------------------------- v2 listing images
from PIL import ImageFilter  # noqa: E402

SCENE_PROMPTS = {
    "desk": [
        "Top-down flat-lay photograph of a clean light oak desk surface. A pen, a small potted succulent and a "
        "ceramic coffee mug sit near the edges. The large centre area is completely empty and uncluttered. "
        "Soft natural window daylight, gentle shadows, warm neutral tones, high-end product photography.",
        "Top-down photograph of a white marble desk with a gold paperclip, a pencil and a sprig of eucalyptus "
        "placed near the corners. The centre is completely empty. Bright soft daylight, minimal, airy.",
        "Top-down photograph of a soft beige linen tablecloth with a cup of tea, reading glasses and a small "
        "vase of dried flowers arranged around the edges. The centre is completely empty. Cozy morning light.",
        "Top-down photograph of a matte sage-green desk mat on a walnut desk with a mechanical pencil, sticky "
        "notes and a plant leaf at the edges. The centre is completely empty. Even, soft studio lighting.",
    ],
    "wall": [
        "Interior photograph of a calm Scandinavian living room. A large completely blank warm-white wall fills "
        "the upper two thirds of the frame above a low oak sideboard with a vase and books. No frames, no "
        "pictures, no art on the wall. Soft natural daylight, photorealistic.",
        "Interior photograph of a serene bedroom. A large completely blank light greige wall fills the upper two "
        "thirds of the frame above a linen-covered bed headboard. No frames or art on the wall. Morning light.",
        "Interior photograph of a modern home office. A large completely blank off-white wall fills the upper "
        "two thirds of the frame above a light wood desk with a lamp and a plant. No frames or art on the wall.",
        "Interior photograph of an entryway with a completely blank warm white wall filling the upper two thirds "
        "of the frame above a rattan console table with a ceramic bowl. No frames or art on the wall.",
    ],
}
SCENE_NEGATIVE = "paper, notebook, planner, frame, artwork, poster, text, letters, words, logo, watermark, people, hands"


def _shadow(size: tuple[int, int], radius: int = 28, opacity: int = 90) -> Image.Image:
    w, h = size
    pad = radius * 3
    sh = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rectangle([pad, pad, pad + w, pad + h], fill=(0, 0, 0, opacity))
    return sh.filter(ImageFilter.GaussianBlur(radius))


def _place(canvas: Image.Image, art: Image.Image, center: tuple[int, int], *, rotate: float = 0.0,
           shadow_offset: tuple[int, int] = (14, 22)) -> None:
    art = art.convert("RGBA")
    if rotate:
        art = art.rotate(rotate, resample=Image.BICUBIC, expand=True)
    radius = max(12, art.width // 40)
    sh = _shadow(art.size, radius=radius)
    pad = radius * 3
    x, y = int(center[0]) - art.width // 2, int(center[1]) - art.height // 2
    canvas.alpha_composite(sh, (max(0, x - pad + shadow_offset[0]), max(0, y - pad + shadow_offset[1])))
    canvas.alpha_composite(art, (x, y))


def page_hero(page: Image.Image, *, bg: str = "#F4EFE8") -> bytes:
    """Primary listing image: the real first page, large and crisp on a soft background."""
    W, H = LISTING_SIZE
    canvas = Image.new("RGBA", (W, H), bg)
    _place(canvas, fit(page, (int(W * 0.78), int(H * 0.88))), (W // 2, H // 2))
    return _to_png(canvas)


def scene_composite(art: Image.Image, scene_png: bytes, *, kind: str = "desk", framed: bool = False,
                    rotate: float = -2.5) -> bytes:
    """Place the product onto an AI-generated lifestyle scene (desk flat-lay or empty wall)."""
    W, H = LISTING_SIZE
    scene = load(scene_png)
    scale = max(W / scene.width, H / scene.height)
    scene = scene.resize((int(scene.width * scale) + 1, int(scene.height * scale) + 1), Image.LANCZOS)
    left, top = (scene.width - W) // 2, (scene.height - H) // 2
    canvas = scene.crop((left, top, left + W, top + H)).convert("RGBA")
    if kind == "wall":
        inner = fit(art, (int(W * 0.34), int(H * 0.40)))
        mat = max(24, inner.width // 10)
        frame = Image.new("RGB", (inner.width + 2 * mat + 24, inner.height + 2 * mat + 24), "#1E1E1E")
        ImageDraw.Draw(frame).rectangle([12, 12, frame.width - 13, frame.height - 13], fill="#FBFAF7")
        frame.paste(inner, (12 + mat, 12 + mat))
        _place(canvas, frame, (W // 2, int(H * 0.34)), shadow_offset=(10, 16))
    else:
        piece = art
        if framed:
            piece = Image.new("RGB", (art.width + 40, art.height + 40), "#FFFFFF")
            piece.paste(art, (20, 20))
        _place(canvas, fit(piece, (int(W * 0.62), int(H * 0.72))), (W // 2, H // 2), rotate=rotate)
    return _to_png(canvas)


def styled_flat_lay(art: Image.Image, *, bg: str = "#EFE8DE", accent: str = "#A3B18A") -> bytes:
    """Code-only fallback lifestyle image when no image provider is configured."""
    W, H = LISTING_SIZE
    canvas = Image.new("RGBA", (W, H), bg)
    d = ImageDraw.Draw(canvas)
    d.ellipse([W - 520, -180, W + 160, 500], fill=accent)            # soft decorative shapes
    d.ellipse([-220, H - 460, 380, H + 140], fill="#E2D8CA")
    d.rounded_rectangle([W - 380, H - 300, W - 120, H - 250], radius=25, fill="#2B2B2B")  # "pen"
    _place(canvas, fit(art, (int(W * 0.6), int(H * 0.74))), (W // 2, H // 2), rotate=-2.0)
    return _to_png(canvas)


def pages_spread(pages: list[Image.Image], *, bg: str = "#F4EFE8") -> bytes:
    """All pages at a glance: overlapping fan for 2-3 pages, grid for 4-6."""
    W, H = LISTING_SIZE
    canvas = Image.new("RGBA", (W, H), bg)
    pages = pages[:6]
    if len(pages) <= 3:
        n = len(pages)
        for i, pg in enumerate(pages):
            thumb = fit(pg, (int(W * 0.55), int(H * 0.68)))
            cx = int(W * (0.5 + (i - (n - 1) / 2) * 0.22))
            _place(canvas, thumb, (cx, int(H // 2 + abs(i - (n - 1) / 2) * 30)), rotate=(i - (n - 1) / 2) * -4)
    else:
        cols, rows = 3, 2
        cw, ch = W // cols, H // rows
        for i, pg in enumerate(pages):
            thumb = fit(pg, (int(cw * 0.86), int(ch * 0.86)))
            _place(canvas, thumb, (cw * (i % cols) + cw // 2, ch * (i // cols) + ch // 2), shadow_offset=(8, 12))
    return _to_png(canvas)


def detail_crop(page: Image.Image, *, bg: str = "#F4EFE8") -> bytes:
    """Close-up of the top half of the page so buyers can see layout and type detail."""
    W, H = LISTING_SIZE
    crop = page.crop((0, 0, page.width, int(page.height * 0.55)))
    canvas = Image.new("RGBA", (W, H), bg)
    _place(canvas, fit(crop, (int(W * 0.9), int(H * 0.8))), (W // 2, H // 2))
    return _to_png(canvas)


def short_title(title: str, limit: int = 48) -> str:
    """Human product name for images: drop SEO tails ("A | B, C") and cut at a word boundary."""
    t = re.split(r"\s[|•–—-]\s|,\s", title.strip())[0].strip() or title.strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(" -,:;&")
    return cut or t[:limit]


def included_html(title: str, items: list[str], *, palette: list[str], kicker: str = "What's included",
                  foot: str = "Instant digital download · no physical item will be shipped") -> str:
    import html as html_lib

    bg, ink, accent = (list(palette) + ["#F7F3EC", "#2B2B2B", "#A3B18A"])[:3]
    rows = "".join(f"<li><span class='tick'>✓</span>{html_lib.escape(i)}</li>" for i in items[:8])
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
body{{margin:0;width:1000px;min-height:1000px;background:{bg};color:{ink};font-family:Inter,'DejaVu Sans',sans-serif;
display:flex;align-items:center;justify-content:center}}
.card{{width:820px;padding:64px 70px;background:#fff;border-radius:28px;box-shadow:0 20px 60px rgba(0,0,0,.10)}}
.k{{text-transform:uppercase;letter-spacing:.18em;font-size:18px;color:{accent};font-weight:700}}
h1{{font-family:Poppins,Inter,sans-serif;font-size:{40 if len(title) <= 32 else 34}px;line-height:1.15;margin:14px 0 34px;overflow-wrap:break-word}}
ul{{list-style:none;padding:0;margin:0}} li{{font-size:24px;padding:14px 0;border-top:1px solid #EEE;display:flex;gap:18px;overflow-wrap:anywhere}}
.tick{{color:{accent};font-weight:700}} .foot{{margin-top:34px;font-size:19px;opacity:.7}}
</style></head><body><div class='card'><div class='k'>{html_lib.escape(kicker)}</div><h1>{html_lib.escape(title)}</h1>
<ul>{rows}</ul><div class='foot'>{html_lib.escape(foot)}</div></div></body></html>"""
