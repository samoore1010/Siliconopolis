# Single image used by every Railway service; services differ only by start command.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# fonts for Pillow/reportlab placeholder + mockup text rendering
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core libjpeg62-turbo zlib1g \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# non-root user; storage dir only used when STORAGE_BACKEND=local (never on Railway)
RUN useradd --create-home --uid 10001 app && mkdir -p /app/storage && chown -R app:app /app
USER app

# Default start command = web service. Cron services override with e.g.
#   python -m pipeline run researcher
CMD ["python", "-m", "pipeline", "web"]
