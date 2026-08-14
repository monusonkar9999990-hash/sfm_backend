# Production image for the SFM API.
#
#   docker build -t sfm-api .
#   docker run --env-file .env -p 8000:8000 sfm-api
#
# Two stages: mysqlclient and Pillow need a C toolchain to build, and none of
# that belongs in the image that runs in production.

# --- build ----------------------------------------------------------------
# 3.14, not 3.13. Every model's primary key defaults to `uuid.uuid7`, which
# only exists from Python 3.14 — on 3.13 the image builds cleanly and then dies
# on the first import with "module 'uuid' has no attribute 'uuid7'".
FROM python:3.14-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# mysqlclient needs pkg-config and the MySQL client headers; Pillow needs the
# jpeg and zlib headers. Missing any of them fails at `pip install` with a
# compiler error rather than anything that names the cause.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --- run ------------------------------------------------------------------
# Must match the build stage: the virtualenv copied across is tied to the
# interpreter's minor version.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings

# Runtime libraries only — the shared objects the wheels link against, not the
# headers and compiler used to build them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-mysql-client \
        libjpeg62-turbo \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Never root. A container that runs as root gives a container escape the whole
# host, and this process needs to own nothing but its media directory.
RUN useradd --system --create-home --uid 10001 sfm

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sfm:sfm . .

# Media lives on a volume; the image only needs the mount point to exist and
# to be writable by the unprivileged user.
RUN mkdir -p /app/media /app/staticfiles && chown -R sfm:sfm /app/media /app/staticfiles

USER sfm

EXPOSE 8000

# Same endpoint the load balancer uses. It checks the database, so an instance
# whose MySQL has gone away is reported unhealthy instead of quietly serving
# 500s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health/ || exit 1

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "config/gunicorn.conf.py"]
