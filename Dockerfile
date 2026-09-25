# Multi-stage build with a pre-deploy test gate (same pattern as bix-ai):
#   base    — runtime deps + source
#   test    — adds pytest and runs the suite; a failure FAILS the build
#   runtime — lean production image; depends on `test` via COPY --from

FROM python:3.12-slim AS base
WORKDIR /app
# Fonts for the Pillow renderer (screens.py resolves them at runtime):
# dejavu-core for sans, dejavu-extra for the serif italic the talking point
# uses. Michroma (display font, same as the rage channel) ships in ./fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core fonts-dejavu-extra \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p logs data

# ── Test gate ─────────────────────────────────────────────────────────────────
FROM base AS test
RUN pip install --no-cache-dir pytest==8.3.4
RUN python -m pytest -q
RUN touch /app/.tests-passed

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM base AS runtime
COPY --from=test /app/.tests-passed /app/.tests-passed
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
