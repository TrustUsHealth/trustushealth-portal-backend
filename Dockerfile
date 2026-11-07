# Multi-stage, slim image (PRD target: under 250 MB). Runs on Render; also used locally and in CI.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
# Install dependencies only (cached until pyproject.toml changes); the app code is copied below.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --no-create-home portal
WORKDIR /srv
COPY --from=builder /install /usr/local
COPY app ./app
COPY config ./config
USER portal
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz')" || exit 1
# Render injects $PORT. --proxy-headers so audit logs see the real client IP behind Render/Cloudflare.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
