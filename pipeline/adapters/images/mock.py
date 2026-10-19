"""Mock image provider: renders a labeled placeholder so the pipeline runs end to end."""

from __future__ import annotations

import hashlib
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

from .base import GeneratedImage, ImageProvider


def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:  # pragma: no cover
        return ImageFont.load_default()


class MockImageProvider(ImageProvider):
    name = "mock"

    def generate(self, prompt, *, size=(1024, 1024), style=None, negative_prompt=None, quality=None) -> GeneratedImage:
        w, h = size
        seed = hashlib.md5(prompt.encode()).digest()
        bg = (235 + seed[0] % 20, 230 + seed[1] % 20, 220 + seed[2] % 25)
        fg = (40 + seed[3] % 40, 40 + seed[4] % 40, 40 + seed[5] % 40)
        img = Image.new("RGB", (w, h), bg)
        d = ImageDraw.Draw(img)
        # deterministic but prompt-dependent "artwork": shapes placed from the prompt hash so
        # different prompts produce visually distinct placeholders (keeps the QA phash check honest)
        rng_bytes = hashlib.sha512(prompt.encode()).digest()  # 64 bytes: enough for 16 shapes
        n_shapes = 4 + rng_bytes[0] % 6
        for i in range(n_shapes):
            b = rng_bytes[i * 4 : i * 4 + 4]
            x0, y0 = int(b[0] / 255 * w * 0.8), int(b[1] / 255 * h * 0.8)
            x1, y1 = x0 + int(w * 0.1 + b[2] / 255 * w * 0.4), y0 + int(h * 0.1 + b[3] / 255 * h * 0.4)
            shade = (fg[0] + b[0] % 60, fg[1] + b[1] % 60, fg[2] + b[2] % 60)
            if i % 3 == 0:
                d.ellipse([x0, y0, min(x1, w), min(y1, h)], outline=shade, width=max(3, w // 200))
            elif i % 3 == 1:
                d.rounded_rectangle([x0, y0, min(x1, w), min(y1, h)], radius=w // 30, outline=shade, width=max(3, w // 200))
            else:
                d.line([x0, y0, min(x1, w), min(y1, h)], fill=shade, width=max(4, w // 150))
        label = "MOCK IMAGE\n" + "\n".join(textwrap.wrap(prompt, 36)[:4])
        f = _font(max(18, w // 32))
        bbox = d.multiline_textbbox((0, 0), label, font=f, align="center")
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.multiline_text(((w - tw) / 2, (h - th) / 2), label, fill=fg, font=f, align="center")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return GeneratedImage(data=buf.getvalue(), media_type="image/png", width=w, height=h,
                              cost_usd=0.0, model="mock-placeholder", metadata={"prompt": prompt, "style": style})

    def ping(self) -> None:
        return None
