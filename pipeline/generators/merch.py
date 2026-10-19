"""Mockups for in-house apparel and mugs.

`draw_blank` paints a clean flat product (shirt, sweatshirt, hoodie or mug) in any blank colour, and
`place_design` prints the real print file onto it, shaded by the garment's folds so it looks printed
rather than pasted. These are exact (the design is never redrawn), so they double as the colour-chart
image and as the print proof the shop checks against. A photoreal lifestyle shot is made separately by
the image model from this composite.
"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SS = 2  # supersampling for smooth edges
BG = "#F3F0EB"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _shade(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(c * f))) for c in rgb)  # type: ignore[return-value]


def _luma(rgb: tuple[int, int, int]) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _font(size: int):
    for name in ("Poppins-SemiBold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------------------------------- shapes (1000x1000)
TEE = [(420, 150), (360, 168), (305, 190), (150, 300), (215, 440), (300, 398), (300, 905), (700, 905),
       (700, 398), (785, 440), (850, 300), (695, 190), (640, 168), (580, 150), (548, 196), (500, 210), (452, 196)]
SWEAT = [(420, 150), (360, 168), (300, 192), (205, 270), (150, 520), (128, 830), (205, 845), (238, 560),
         (300, 430), (300, 900), (700, 900), (700, 430), (762, 560), (795, 845), (872, 830), (850, 520),
         (795, 270), (700, 192), (640, 168), (580, 150), (548, 192), (500, 205), (452, 192)]


def _garment(draw: ImageDraw.ImageDraw, kind: str, rgb, s: float) -> None:
    edge = _shade(rgb, 0.82 if _luma(rgb) > 0.25 else 1.35)
    pts = [(x * s, y * s) for x, y in (TEE if kind == "shirt" else SWEAT)]
    if kind == "hoodie":  # hood behind the neckline
        draw.ellipse([355 * s, 70 * s, 645 * s, 300 * s], fill=_shade(rgb, 0.9), outline=edge, width=int(4 * s))
    draw.polygon(pts, fill=rgb, outline=edge)
    draw.line(pts + [pts[0]], fill=edge, width=int(4 * s), joint="curve")
    if kind == "hoodie":
        draw.ellipse([415 * s, 140 * s, 585 * s, 255 * s], fill=_shade(rgb, 0.7))
        for x in (470, 530):  # drawstrings
            draw.line([(x * s, 230 * s), ((x + (x - 500) * 0.3) * s, 390 * s)], fill=_shade(rgb, 0.75), width=int(7 * s))
        draw.rounded_rectangle([370 * s, 640 * s, 630 * s, 820 * s], radius=int(30 * s), outline=edge, width=int(4 * s))
    else:
        draw.arc([425 * s, 125 * s, 575 * s, 240 * s], 15, 165, fill=edge, width=int(10 * s))  # collar rib
    if kind in ("sweatshirt", "hoodie"):
        draw.line([(300 * s, 870 * s), (700 * s, 870 * s)], fill=edge, width=int(3 * s))  # waistband
        for x0, x1 in ((128, 205), (795, 872)):
            draw.line([(x0 * s, 800 * s), (x1 * s, 812 * s)], fill=edge, width=int(3 * s))  # cuffs


def _mug(draw: ImageDraw.ImageDraw, rgb, s: float) -> None:
    edge = _shade(rgb, 0.8)
    draw.ellipse([680 * s, 360 * s, 860 * s, 640 * s], outline=_shade(rgb, 0.9), width=int(46 * s))  # handle
    draw.rounded_rectangle([190 * s, 240 * s, 720 * s, 790 * s], radius=int(36 * s), fill=rgb, outline=edge, width=int(3 * s))
    draw.ellipse([190 * s, 215 * s, 720 * s, 275 * s], fill=_shade(rgb, 0.93), outline=edge, width=int(3 * s))
    draw.ellipse([210 * s, 222 * s, 700 * s, 268 * s], fill=_shade(rgb, 0.78))


def _shading(kind: str, size: int) -> Image.Image:
    """Greyscale light map (folds for garments, cylinder for mugs); 255 = full light."""
    w = h = size
    if kind == "mug":
        row = Image.new("L", (w, 1))
        for x in range(w):
            t = (x / w - 0.18) / 0.55  # across the mug body
            v = 0.78 + 0.22 * math.sin(max(0.0, min(1.0, t)) * math.pi) if 0 <= t <= 1 else 1.0
            row.putpixel((x, 0), int(255 * v))
        return row.resize((w, h))
    light = Image.new("L", (w, h), 245)
    d = ImageDraw.Draw(light)
    for x0, x1, v in ((0.30, 0.36, 225), (0.64, 0.70, 228), (0.46, 0.50, 238)):
        d.rectangle([x0 * w, 0.3 * h, x1 * w, 0.9 * h], fill=v)
    return light.filter(ImageFilter.GaussianBlur(size / 30))


def draw_blank(kind: str, color: str, *, size: int = 1600, bg: str = BG) -> Image.Image:
    rgb = _rgb(color)
    big = size * SS
    s = big / 1000
    canvas = Image.new("RGB", (big, big), _rgb(bg))
    shadow = Image.new("L", (big, big), 0)
    sd = ImageDraw.Draw(shadow)
    if kind == "mug":
        sd.ellipse([200 * s, 760 * s, 820 * s, 840 * s], fill=90)
    else:
        sd.polygon([(x * s + 14 * s, y * s + 18 * s) for x, y in (TEE if kind == "shirt" else SWEAT)], fill=70)
    canvas.paste(Image.new("RGB", (big, big), (0, 0, 0)), (0, 0), shadow.filter(ImageFilter.GaussianBlur(18 * s)))
    product = Image.new("RGBA", (big, big), (0, 0, 0, 0))  # drawn separately so shading stays on the product
    draw = ImageDraw.Draw(product)
    if kind == "mug":
        _mug(draw, rgb, s)
    else:
        _garment(draw, kind, rgb, s)
    light = _shading(kind, big)
    shaded = ImageChops.multiply(product.convert("RGB"), Image.merge("RGB", (light, light, light)))
    canvas.paste(shaded, (0, 0), product.getchannel("A"))
    return canvas.resize((size, size), Image.LANCZOS)


def _mug_warp(design: Image.Image, visible: float) -> Image.Image:
    """Show the centre of a wrap print as it would appear on the front of a cylinder."""
    w, h = design.size
    vis = max(1, int(w * visible))
    front = design.crop(((w - vis) // 2, 0, (w + vis) // 2, h))
    out = Image.new("RGBA", front.size, (0, 0, 0, 0))
    fw = front.width
    for x in range(fw):  # arcsin mapping: compress towards the edges
        u = (x / (fw - 1)) * 2 - 1
        src = int((math.asin(u) / (math.pi / 2) + 1) / 2 * (fw - 1))
        out.paste(front.crop((src, 0, src + 1, h)), (x, 0))
    return out


def place_design(blank: Image.Image, design_png: bytes, mockup: dict, kind: str) -> Image.Image:
    """Print the design into the catalog's print box on the blank, keeping its proportions."""
    design = Image.open(io.BytesIO(design_png)).convert("RGBA")
    if kind == "mug":
        design = _mug_warp(design, float(mockup.get("visible", 0.42)))
    bx, by, bw, bh = (float(v) for v in mockup.get("box", (0.33, 0.27, 0.33, 0.39)))
    W, H = blank.size
    box_w, box_h = int(bw * W), int(bh * H)
    scale = min(box_w / design.width, box_h / design.height)
    d = design.resize((max(1, int(design.width * scale)), max(1, int(design.height * scale))), Image.LANCZOS)
    x = int(bx * W + (box_w - d.width) / 2)
    y = int(by * H + ((box_h - d.height) / 2 if kind == "mug" else 0))  # apparel prints hang from the top
    out = blank.convert("RGBA")
    # ink follows the garment's light and shade, so the print sits in the fabric
    light = _shading(kind, W).crop((x, y, x + d.width, y + d.height)).convert("RGB")
    shaded = ImageChops.multiply(d.convert("RGB"), Image.eval(light, lambda v: min(255, v + 12)))
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
    layer.paste(Image.merge("RGBA", (*shaded.split(), d.getchannel("A"))), (x, y))
    return Image.alpha_composite(out, layer).convert("RGB")


def mockup(item, design_png: bytes, color: str, *, size: int = 1600, bg: str = BG) -> Image.Image:
    kind = str(item.mockup.get("kind", "shirt"))
    return place_design(draw_blank(kind, item.colors.get(color, "#FFFFFF"), size=size, bg=bg), design_png, item.mockup, kind)


def color_chart(item, design_png: bytes, colors: list[str], *, size: int = 2000, title: str = "Available colours") -> Image.Image:
    colors = [c for c in colors if c in item.colors][:8] or list(item.colors)[:1]
    cols = 1 if len(colors) == 1 else 2 if len(colors) <= 4 else 3 if len(colors) <= 9 else 4
    rows = math.ceil(len(colors) / cols)
    label_h, top = 70, 150
    cell = int(min((size - 80) / cols, (size - top - 40) / rows - label_h))
    canvas = Image.new("RGB", (size, size), _rgb(BG))
    d = ImageDraw.Draw(canvas)
    d.text((size / 2, 80), title, fill=(40, 40, 40), font=_font(64), anchor="mm")
    grid_w, grid_h = cols * cell, rows * (cell + label_h)
    x0, y0 = (size - grid_w) // 2, top + max(0, (size - top - grid_h) // 2)
    for i, c in enumerate(colors):
        img = mockup(item, design_png, c, size=cell)
        x, y = x0 + (i % cols) * cell, y0 + (i // cols) * (cell + label_h)
        canvas.paste(img, (x, y))
        d.text((x + cell / 2, y + cell + 20), c, fill=(60, 60, 60), font=_font(38), anchor="mm")
    return canvas


def design_closeup(design_png: bytes, color: str, *, size: int = 2000) -> Image.Image:
    design = Image.open(io.BytesIO(design_png)).convert("RGBA")
    canvas = Image.new("RGBA", (size, size), (*_rgb(color), 255))
    design.thumbnail((int(size * 0.86), int(size * 0.86)), Image.LANCZOS)
    canvas.alpha_composite(design, ((size - design.width) // 2, (size - design.height) // 2))
    return canvas.convert("RGB")


def best_contrast_color(item, design_png: bytes, colors: list[str]) -> str:
    """Pick the offered blank colour the design reads best on (for the hero image)."""
    design = Image.open(io.BytesIO(design_png)).convert("RGBA")
    design.thumbnail((200, 200))
    px = [p for p in design.getdata() if p[3] > 128]
    if not px:
        return colors[0]
    return max(colors, key=lambda c: contrast(item, design_png, c))


def contrast(item, design_png: bytes, color: str) -> float:
    """Average luminance difference between the design's ink and a blank colour (0 = invisible, 1 = max)."""
    design = Image.open(io.BytesIO(design_png)).convert("RGBA")
    design.thumbnail((200, 200))
    px = [p for p in design.getdata() if p[3] > 128]
    if not px:
        return 0.0
    blank = _luma(_rgb(item.colors.get(color, "#FFFFFF")))
    return sum(abs(_luma(p[:3]) - blank) for p in px) / len(px)
