# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build stage: compile the CSS.
#
# static/css/app.css is generated from Tailwind, and CI already fails if the
# committed copy is stale. Building it here as well means the image cannot
# depend on whatever happened to be on the machine that built it.
# ---------------------------------------------------------------------------
FROM node:20-slim AS css
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tailwind.config.js ./
COPY templates ./templates
COPY static ./static
RUN npm run build:css


# ---------------------------------------------------------------------------
# Runtime stage.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED so logs appear in real time rather than when a buffer fills,
# which is the difference between watching a deploy and guessing at one.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq for psycopg, and curl only so the image can health-check itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# An unprivileged user, created before the copy so the layers below are owned
# correctly rather than chowned afterwards.
RUN useradd --create-home --uid 10001 wams
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=wams:wams . .
COPY --from=css --chown=wams:wams /build/static/css/app.css ./static/css/app.css

# collectstatic needs settings to import, but not a database. A throwaway key
# is supplied for this step alone; the real one arrives from the environment at
# run time, and DJANGO_DEBUG stays unset so the manifest storage is used.
RUN DJANGO_SECRET_KEY=build-time-only-not-used-at-runtime \
    DJANGO_ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput --clear

USER wams
EXPOSE 8000

# Liveness only. Readiness depends on the database, which is not this
# container's to judge, and a failing readiness check should not restart it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz/ || exit 1

# Migrations are deliberately NOT run here. Every replica starting at once
# would race, and a schema change should be a decision someone makes, not a
# side effect of a container restart. Run them as a release step:
#     python manage.py migrate
CMD ["gunicorn", "--config", "gunicorn.conf.py", "config.wsgi:application"]
