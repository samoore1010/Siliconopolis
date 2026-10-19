"""HTML/CSS -> PDF/PNG with headless Chromium (Playwright).

Claude designs planner pages and previews as HTML; this module turns them into print-ready PDFs and
PNG previews. Rendering is fully offline: every network request is blocked (only inline/data: assets
load) and <script> tags are stripped before rendering, so model-written HTML cannot fetch or execute
anything.
"""

from __future__ import annotations

import atexit
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

PAGE_SIZES = {
    "letter": {"css": "8.5in 11in", "w": "8.5in", "h": "11in", "label": "US Letter 8.5x11 in"},
    "a4": {"css": "210mm 297mm", "w": "210mm", "h": "297mm", "label": "A4 210x297 mm"},
}

# Fonts installed in the Docker image (see Dockerfile). Prompts tell the model to use only these.
AVAILABLE_FONTS = [
    "Inter", "Nunito", "Poppins", "Lato", "Open Sans", "DM Sans",
    "Lora", "Playfair Display", "DM Serif Display", "Liberation Serif",
    "Caveat",  # handwritten accent only
    "DejaVu Sans",
]

BASE_CSS = """
@page { size: %(size)s; margin: 0; }
:root { --page-w: %(w)s; --page-h: %(h)s; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin: 0; padding: 0; }
body { font-family: Inter, 'DejaVu Sans', sans-serif; }
.page { width: var(--page-w); height: var(--page-h); overflow: hidden; position: relative;
        page-break-after: always; break-after: page; }
.page:last-child { page-break-after: auto; break-after: auto; }
"""

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_EVENT_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_HTML_FENCE_RE = re.compile(r"```(?:html)?\s*(.*?)```", re.DOTALL)


class RenderError(RuntimeError):
    pass


@dataclass
class LayoutReport:
    pages: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.pages > 0 and not self.problems


def extract_html(text: str) -> str:
    """Pull an HTML document out of a model reply (handles ```html fences)."""
    m = _HTML_FENCE_RE.search(text)
    html = (m.group(1) if m else text).strip()
    if "<" not in html:
        raise RenderError("model reply did not contain HTML")
    return html


def sanitize(html: str) -> str:
    html = _SCRIPT_RE.sub("", html)
    return _EVENT_ATTR_RE.sub("", html)


def wrap(html: str, page: str = "letter", landscape: bool = False) -> str:
    """Inject page-size CSS so one design renders at Letter or A4 (portrait or landscape)."""
    size = PAGE_SIZES[page]
    w, h = (size["h"], size["w"]) if landscape else (size["w"], size["h"])
    base = "<style>" + BASE_CSS % {"size": f"{w} {h}", "w": w, "h": h} + "</style>"
    html = sanitize(html)
    if re.search(r"<head[^>]*>", html, re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", r"\1" + base, html, count=1, flags=re.IGNORECASE)
    return f"<!doctype html><html><head><meta charset='utf-8'>{base}</head><body>{html}</body></html>"


_LOCAL = threading.local()  # Playwright's sync API is bound to the thread that started it


def _browser():
    """One headless Chromium per thread (launching costs ~1s; agents render many pages per run).
    The web service renders from worker threads, so each thread gets its own instance."""
    browser = getattr(_LOCAL, "browser", None)
    if browser is None or not browser.is_connected():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise RenderError("playwright is not installed; run `pip install playwright && playwright install chromium`") from e
        _LOCAL.pw = sync_playwright().start()
        _LOCAL.browser = _LOCAL.pw.chromium.launch(args=["--disable-gpu", "--no-sandbox"])
        if threading.current_thread() is threading.main_thread():
            atexit.register(close_browser)
    return _LOCAL.browser


def close_browser() -> None:
    try:
        if getattr(_LOCAL, "browser", None) is not None:
            _LOCAL.browser.close()
        if getattr(_LOCAL, "pw", None) is not None:
            _LOCAL.pw.stop()
    except Exception:  # noqa: BLE001
        pass
    _LOCAL.browser = _LOCAL.pw = None


@contextmanager
def _page(viewport: tuple[int, int] = (816, 1056), scale: float = 1.0):
    ctx = _browser().new_context(viewport={"width": viewport[0], "height": viewport[1]},
                                 device_scale_factor=scale, java_script_enabled=True, offline=True)
    try:
        ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("data:", "about:"))
                  else route.abort())
        yield ctx.new_page()
    finally:
        ctx.close()


_LAYOUT_JS = """
() => {
  const pages = [...document.querySelectorAll('.page')];
  const problems = [];
  pages.forEach((pg, i) => {
    const pr = pg.getBoundingClientRect();
    if (pg.scrollHeight > pg.clientHeight + 2) problems.push(`page ${i+1}: content is ${pg.scrollHeight - pg.clientHeight}px taller than the page (overflows the bottom)`);
    if (pg.scrollWidth > pg.clientWidth + 2) problems.push(`page ${i+1}: content is wider than the page (overflows the right edge)`);
    pg.querySelectorAll('*').forEach(el => {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const tooRight = r.right > pr.right + 1, tooLow = r.bottom > pr.bottom + 1;
      const text = (el.childNodes.length && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
      if ((tooRight || tooLow) && text) problems.push(`page ${i+1}: text "${el.textContent.trim().slice(0,40)}" is cut off at the ${tooRight ? 'right' : 'bottom'} edge`);
      if (text && el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflow !== 'visible')
        problems.push(`page ${i+1}: text "${el.textContent.trim().slice(0,40)}" is clipped inside its box`);
    });
  });
  return {pages: pages.length, problems: [...new Set(problems)].slice(0, 12)};
}
"""


def check_layout(html: str, page: str = "letter", landscape: bool = False) -> LayoutReport:
    """Render on screen at page size and report overflow/clipping problems the model should fix."""
    with _page() as pg:
        pg.set_content(wrap(html, page, landscape), wait_until="load")
        data = pg.evaluate(_LAYOUT_JS)
    report = LayoutReport(pages=int(data["pages"]), problems=list(data["problems"]))
    if report.pages == 0:
        report.problems.append('no elements with class="page" found; wrap each page in <div class="page">')
    return report


def html_to_pdf(html: str, page: str = "letter", landscape: bool = False) -> bytes:
    with _page() as pg:
        pg.set_content(wrap(html, page, landscape), wait_until="load")
        pg.emulate_media(media="print")
        return pg.pdf(prefer_css_page_size=True, print_background=True)


def html_to_page_pngs(html: str, page: str = "letter", *, landscape: bool = False, width_px: int = 1700,
                      max_pages: int = 6) -> list[bytes]:
    """Screenshot each .page element (default ~200 dpi for Letter) for previews and listing images."""
    css_w, css_h = (816, 1056) if page == "letter" else (794, 1123)  # CSS px at 96 dpi
    if landscape:
        css_w, css_h = css_h, css_w
    scale = width_px / css_w
    with _page(viewport=(css_w, css_h), scale=scale) as pg:
        pg.set_content(wrap(html, page, landscape), wait_until="load")
        pages = pg.query_selector_all(".page")
        return [el.screenshot(type="png") for el in pages[:max_pages]]


def html_to_png(html: str, *, width: int, height: int, scale: float = 2.0, full_page: bool = False) -> bytes:
    """Screenshot a free-form HTML snippet at width x height CSS px (full_page grows to fit content)."""
    with _page(viewport=(width, height), scale=scale) as pg:
        pg.set_content(sanitize(html), wait_until="load")
        return pg.screenshot(type="png", full_page=full_page)


def chromium_available() -> bool:
    try:
        with _page() as pg:
            pg.set_content("<p>ok</p>")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------- print files
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

PRINT_CSS = """
html, body { margin: 0; padding: 0; background: transparent !important; }
* { box-sizing: border-box; }
.design { width: %(w)sin; height: %(h)sin; position: relative; overflow: hidden; background: transparent; }
[data-fit] { display: block; white-space: nowrap; }
"""

# Trusted code (not model-written): shrink personalised text until it fits its box.
_FIT_JS = """
() => {
  const area = document.querySelector('.design');
  if (!area) return {ok: false, problems: ['no element with class="design"']};
  const ar = area.getBoundingClientRect();
  const problems = [];
  document.querySelectorAll('[data-fit]').forEach(el => {
    let fs = parseFloat(getComputedStyle(el).fontSize), min = Math.max(6, fs * 0.3);
    const over = () => el.scrollWidth > el.clientWidth + 1 || el.getBoundingClientRect().right > ar.right + 1
      || el.getBoundingClientRect().left < ar.left - 1;
    while (over() && fs > min) { fs -= Math.max(1, fs * 0.04); el.style.fontSize = fs + 'px'; }
    if (over()) problems.push(`personalised text "${el.textContent.trim().slice(0, 30)}" does not fit even at minimum size`);
  });
  area.querySelectorAll('*').forEach(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const text = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (text && (r.right > ar.right + 1 || r.bottom > ar.bottom + 1 || r.left < ar.left - 1 || r.top < ar.top - 1))
      problems.push(`text "${el.textContent.trim().slice(0, 30)}" crosses the edge of the print area`);
  });
  const leftovers = (document.body.innerText.match(/\\{\\{[^}]*\\}\\}/g) || []);
  if (leftovers.length) problems.push('unfilled placeholders: ' + leftovers.join(', '));
  return {ok: problems.length === 0, problems: [...new Set(problems)].slice(0, 10)};
}
"""


def fill_template(html: str, values: dict[str, str], *, art_uri: str | None = None) -> str:
    """Substitute {{field}} placeholders (HTML-escaped) and {{art}} (generated artwork data URI)."""
    import html as html_lib

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "art":
            return art_uri or ""
        return html_lib.escape(str(values[key])) if key in values else m.group(0)

    return _PLACEHOLDER_RE.sub(sub, html)


def wrap_print(html: str, width_in: float, height_in: float) -> str:
    css = "<style>" + PRINT_CSS % {"w": width_in, "h": height_in} + "</style>"
    html = sanitize(html)
    if re.search(r"<head[^>]*>", html, re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", r"\1<meta charset='utf-8'>" + css, html, count=1, flags=re.IGNORECASE)
    return f"<!doctype html><html><head><meta charset='utf-8'>{css}</head><body>{html}</body></html>"


@dataclass
class PrintRender:
    png: bytes
    problems: list[str]
    width_px: int
    height_px: int

    @property
    def ok(self) -> bool:
        return not self.problems


def render_print_file(html: str, *, width_in: float, height_in: float, dpi: int = 300,
                      values: dict[str, str] | None = None, art_uri: str | None = None) -> PrintRender:
    """Render a merch design to a transparent PNG at print resolution (e.g. 11x13 in @300 dpi = 3300x3900)."""
    import io

    from PIL import Image

    filled = fill_template(html, values or {}, art_uri=art_uri)
    css_w, css_h = round(width_in * 96), round(height_in * 96)
    with _page(viewport=(css_w, css_h), scale=dpi / 96) as pg:
        pg.set_content(wrap_print(filled, width_in, height_in), wait_until="load")
        pg.evaluate("document.fonts && document.fonts.ready")
        report = pg.evaluate(_FIT_JS)
        el = pg.query_selector(".design")
        if el is None:
            raise RenderError('design has no element with class="design"')
        png = el.screenshot(type="png", omit_background=True)
    img = Image.open(io.BytesIO(png))
    problems = list(report.get("problems") or [])
    alpha = img.convert("RGBA").getchannel("A")
    box = alpha.point(lambda v: 255 if v > 10 else 0).getbbox()
    if not box:
        problems.append("the design is empty (nothing would print)")
    else:
        wide, tall = (box[2] - box[0]) / img.width, (box[3] - box[1]) / img.height
        if wide < 0.3 and tall < 0.3:
            problems.append(f"the design only uses {wide:.0%} x {tall:.0%} of the print area; make it larger")
    if alpha.getextrema()[0] > 250:
        problems.append("the design has a solid background; the background must be transparent")
    return PrintRender(png=png, problems=problems, width_px=img.width, height_px=img.height)
