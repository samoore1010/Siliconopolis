# Single image used by every Railway service; services differ only by start command.
# bookworm: Playwright's Chromium system dependencies are supported on Debian 12.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Fonts used by Claude-designed planner pages and listing images (all open-licensed).
# LibreOffice Calc (headless) recalculates every generated workbook so broken formulas never ship,
# and renders the real file for QA and listing images. Carlito/Caladea are metric-compatible
# stand-ins for Calibri/Cambria so the render matches what buyers see in Excel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates fontconfig libjpeg62-turbo zlib1g \
      fonts-dejavu-core fonts-liberation2 fonts-inter fonts-lato fonts-open-sans \
      fonts-crosextra-carlito fonts-crosextra-caladea libreoffice-calc-nogui \
 && rm -rf /var/lib/apt/lists/*
# Google Fonts (OFL) not packaged in Debian; a failed download only means a font fallback.
RUN mkdir -p /usr/share/fonts/truetype/google && cd /usr/share/fonts/truetype/google \
 && for f in "nunito/Nunito%5Bwght%5D.ttf" "poppins/Poppins-Regular.ttf" "poppins/Poppins-SemiBold.ttf" \
             "poppins/Poppins-Bold.ttf" "lora/Lora%5Bwght%5D.ttf" "playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf" \
             "dmserifdisplay/DMSerifDisplay-Regular.ttf" "dmsans/DMSans%5Bopsz,wght%5D.ttf" "caveat/Caveat%5Bwght%5D.ttf"; do \
      curl -fsSL -o "$(basename "$f" | sed 's/%5B/[/;s/%5D/]/')" "https://github.com/google/fonts/raw/main/ofl/$f" || echo "font $f skipped"; \
    done \
 && fc-cache -f

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && chmod -R a+rx /ms-playwright

COPY . .

# non-root user; storage dir only used when STORAGE_BACKEND=local (never on Railway)
RUN useradd --create-home --uid 10001 app && mkdir -p /app/storage && chown -R app:app /app
USER app

# Default start command = migrate, then web service. Running `alembic upgrade head` here means a
# service created from the dashboard (no pre-deploy command configured) still gets its tables.
# Cron services override the start command with e.g. `python -m pipeline run researcher`.
CMD ["sh", "-c", "python -m pipeline migrate && python -m pipeline web"]
