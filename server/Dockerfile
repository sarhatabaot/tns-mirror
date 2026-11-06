# syntax=docker/dockerfile:1
#
# tns-mirror-server — multi-stage Wolfi build.
#
# Neither stage has a compiler, because every dependency installs from a wheel.
# The runtime carries Python, the virtualenv and the source, and nothing else.
# The base is digest-pinned and apk packages are version-pinned, so a rebuild
# cannot silently pick up different ones. Debugging tools belong in an ephemeral
# sidecar, never baked in here.

# --- uv ---------------------------------------------------------------------
# A separate stage so Dependabot can bump it; only the binary is copied out.
FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv

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
    python-3.13-base=3.13.18_git20261030-r0 \
    python-3.13=3.13.18_git20261030-r0 \
    readline=8.3-r3 \
    sqlite-libs=3.54.1-r0 \
    xz=5.8.4-r1

# Bytecode is compiled here because the runtime never writes it, so without this
# every process, healthchecks included, would recompile on start.
ENV UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1

COPY --from=uv /uv /usr/bin/uv

WORKDIR /build

# Dependencies resolve from the lockfile alone, so this layer is cached until
# the lockfile actually changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src/ ./src/
# --no-editable copies the package (and its packaged migration SQL) into the
# venv's site-packages. Without it uv installs a link back to /build/src, which
# does not exist in the runtime stage.
RUN uv sync --frozen --no-dev --no-editable

# --- runtime ----------------------------------------------------------------
FROM cgr.dev/chainguard/wolfi-base@sha256:8acb3a7694eda954b3ad4256aec3291bdd92715d7a8c030abd1a21546337b4e4 AS runtime

# tzdata is not optional here: everything this service schedules is in UTC, and
# without the zone database psycopg cannot resolve the server's timezone.
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

# Non-root, fixed uid so a mounted volume's ownership is predictable.
RUN addgroup -g 10001 -S tns \
    && adduser -u 10001 -S -G tns -h /home/tns tns \
    && mkdir -p /var/lib/tns-mirror \
    && chown -R 10001:10001 /var/lib/tns-mirror

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TNS_WORKDIR=/var/lib/tns-mirror

USER 10001:10001
WORKDIR /var/lib/tns-mirror

# Transient download working area. Losing it costs one re-download, nothing more.
VOLUME ["/var/lib/tns-mirror"]

# Freshness, not just liveness: a row count looks identical whether the last
# sync was an hour ago or three weeks ago.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3 \
    CMD ["tns-mirror-server", "status", "--max-age-hours", "26"]

ENTRYPOINT ["tns-mirror-server"]
CMD ["serve"]

LABEL org.opencontainers.image.title="tns-mirror-server" \
      org.opencontainers.image.description="Mirror the TNS public objects catalogue into SQL." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/tns-mirror/tns-mirror" \
      io.tns-mirror.schema-version="1"
