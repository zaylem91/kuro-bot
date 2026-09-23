# syntax=docker/dockerfile:1
FROM python:3.14-slim

# Production defaults: no .pyc files, unbuffered logs (docker logs shows output live)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (gcc: builds wheels like uvloop/asyncpg if no manylinux wheel;
# libpq-dev: asyncpg runtime). Create a non-root user for hosting safety.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser

# Install deps first with BuildKit cache mount (faster layer caching & builds)
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy application source with proper user permissions (avoids slow chown -R layer)
COPY --chown=appuser:appuser run.py .
COPY --chown=appuser:appuser bot/ bot/
COPY --chown=appuser:appuser dashboard/ dashboard/

USER appuser

# Health endpoint (/health, /metrics) — see bot/services/health.py
EXPOSE 8080

# Container healthcheck (no curl in slim image — use stdlib urllib).
# Honors HEALTH_PORT and HEALTH_TOKEN if set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=int(os.getenv('HEALTH_PORT','8080')); t=os.getenv('HEALTH_TOKEN','').strip(); r=urllib.request.Request(f'http://127.0.0.1:{p}/health', headers={'Authorization': f'Bearer {t}'} if t else {}); sys.exit(0 if urllib.request.urlopen(r, timeout=4).status==200 else 1)"

# -O: optimized bytecode (silences the hikari 'optimization level 0' warning, faster)
CMD ["python", "-O", "run.py"]
