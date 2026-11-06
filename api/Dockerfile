# syntax=docker/dockerfile:1
#
# tns-mirror-api — multi-stage Wolfi build, same shape as the server image.
#
# A separate image from the sync server on purpose. The server holds write
# credentials and is not exposed; this is the only piece that faces the network,
# and it connects with the read-only role.

# --- builder ----------------------------------------------------------------
FROM cgr.dev/chainguard/wolfi-base@sha256:8acb3a7694eda954b3ad4256aec3291bdd92715d7a8c030abd1a21546337b4e4 AS builder

# Wolfi's package repository is rolling, so a pinned base does not pin what apk
# installs. Every package added here is pinned, including transitive ones, or a
# newer Python can require a newer glibc than the base has. Move these and the
# base digest together: see "Containers" in AGENTS.md.
RUN apk add --no-cache \
    gdbm=1.26-r9 \
    libbz2-1=1.0.8-r30 \
    libexpat1=2.10.0-r1 \
    libffi=3.8.0-r2 \
    libstdc++=16.2.0-r2 \
    libuuid=2.42.4-r2 \
    mpdecimal=4.0.1-r6 \
    ncurses-terminfo-base=6.6.20261031-r0 \
    ncurses=6.6.20261031-r0 \
    py3-pip-wheel=26.2.2-r2 \
    py3.13-pip-base=26.2.2-r2 \
    py3.13-pip=26.2.2-r2 \
    py3.13-setuptools=84.2.1-r0 \
    python-3.13-base=3.13.18_git20261030-r0 \
    python-3.13=3.13.18_git20261030-r0 \
    readline=8.3-r3 \
    sqlite-libs=3.54.1-r0 \
    xz=5.8.4-r1

ENV UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir --break-system-packages "uv==0.11.28"

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src/ ./src/
# --no-editable copies the package into site-packages; without it uv installs a
# link back to /build/src, which does not exist in the runtime stage.
RUN uv sync --frozen --no-dev --no-editable

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/wolfi-base@sha256:8acb3a7694eda954b3ad4256aec3291bdd92715d7a8c030abd1a21546337b4e4 AS runtime

# tzdata: psycopg resolves the server timezone, and everything here is UTC.
RUN apk add --no-cache \
    gdbm=1.26-r9 \
    libbz2-1=1.0.8-r30 \
    libexpat1=2.10.0-r1 \
    libffi=3.8.0-r2 \
    libstdc++=16.2.0-r2 \
    libuuid=2.42.4-r2 \
    mpdecimal=4.0.1-r6 \
    ncurses-terminfo-base=6.6.20261031-r0 \
    ncurses=6.6.20261031-r0 \
    py3-pip-wheel=26.2.2-r2 \
    python-3.13-base=3.13.18_git20261030-r0 \
    python-3.13=3.13.18_git20261030-r0 \
    readline=8.3-r3 \
    sqlite-libs=3.54.1-r0 \
    tzdata=2026f-r0 \
    xz=5.8.4-r1

RUN addgroup -g 10001 -S tns \
    && adduser -u 10001 -S -G tns -h /home/tns tns

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TNS_API_PORT=8000

USER 10001:10001
WORKDIR /app

EXPOSE 8000

# Probes liveness, not readiness: a restart does not fix a stale mirror, and
# probing /readyz would put the container in a restart loop while it waits for
# a sync it cannot perform itself. Point your load balancer at /readyz instead.
#
# The module honours TNS_API_ROOT_PATH — a service mounted at /tns does not
# answer /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "tns_mirror_api.healthcheck"]

ENTRYPOINT ["tns-mirror-api"]

LABEL org.opencontainers.image.title="tns-mirror-api" \
      org.opencontainers.image.description="Read-only HTTP API over a tns-mirror catalogue." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/sarhatabaot/tns-mirror" \
      io.tns-mirror.schema-version="1"
