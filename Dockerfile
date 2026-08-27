# syntax=docker/dockerfile:1

# =====================================================================
# Stage 1 — build the React/Vite frontend into frontend/dist
# =====================================================================
FROM node:20-bookworm-slim AS frontend
WORKDIR /web

# Install deps from the lockfile first (cached unless the lockfile changes).
# `npm ci` requires frontend/package-lock.json to be committed — a plain
# `npm install` here would silently float versions between builds.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the SPA. FastAPI mounts the resulting /web/dist at "/".
COPY frontend/ ./
RUN npm run build


# =====================================================================
# Stage 2 — Python runtime that serves the API + the built frontend
# =====================================================================
FROM python:3.11-slim-bookworm AS runtime

# curl (for the container healthcheck) is the ONLY apt package this image needs.
# Pillow's manylinux wheels vendor their own zlib/libjpeg/libtiff/libwebp/
# freetype/lcms, so no image codec packages are required on slim.
#
# Note what is deliberately absent, because the sibling project (cownting) has
# it and copying that list over would be pure cargo cult: no ffmpeg and no
# libgl1/libglib2.0-0 (both exist there for OpenCV video decoding — this app has
# neither OpenCV nor video), and no torch/ultralytics/CUDA. Bienenblech never
# runs a model: it tiles JPEGs and stores polygons. The image stays a few
# hundred MB instead of several GB, and that is a feature worth protecting.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Application Python dependencies (own layer: source changes don't re-resolve).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application source (run from /app via `python -m`, NOT pip-installed, so that
# api.py resolves frontend/dist relative to /app).
COPY bienenblech/ ./bienenblech/
COPY config/ ./config/

# Built frontend from stage 1.
COPY --from=frontend /web/dist ./frontend/dist

# The app runs as an unprivileged user, but the container STARTS as root: the
# entrypoint re-owns anything in the /app/data bind mount that a root-run host
# tool left behind (the build-time chown below can't reach a mount), then drops
# to `bienenblech` via setpriv before exec'ing the CMD. setpriv comes from
# util-linux, which is already in the slim base image.
RUN useradd --create-home --uid 10001 bienenblech \
    && mkdir -p /app/data \
    && chown -R bienenblech:bienenblech /app
COPY entrypoint.sh ./
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]

EXPOSE 8000

# /api/health is the one route with no session gate, so it answers 200 as soon
# as the app is really serving. start-period is short because there is no model
# to load — boot is uvicorn + a DuckDB open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health >/dev/null || exit 1

CMD ["python", "-m", "bienenblech.cli", "serve", \
     "--config", "config/bienenblech.prod.yaml", \
     "--host", "0.0.0.0", "--port", "8000"]
