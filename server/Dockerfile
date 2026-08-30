# syntax=docker/dockerfile:1
#
# tns-mirror-server — multi-stage Wolfi build.
#
# Compilers live only in the builder; the runtime carries Python, the virtualenv
# and the source, and nothing else. Bases are digest-pinned so a rebuild cannot
# silently pick up a different image. Debugging tools belong in an ephemeral
# sidecar, never baked in here.

# --- builder ----------------------------------------------------------------
FROM cgr.dev/chainguard/wolfi-base@sha256:e624c5d5e42382ce7165ddafcbbf8e6769a24cbd02ea6114b880b05ae5ba2a8d AS builder

# hadolint ignore=DL3018
RUN apk add --no-cache python-3.13 python-3.13-dev py3.13-pip build-base

ENV UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir --break-system-packages "uv==0.11.28"

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
FROM cgr.dev/chainguard/wolfi-base@sha256:e624c5d5e42382ce7165ddafcbbf8e6769a24cbd02ea6114b880b05ae5ba2a8d AS runtime

# tzdata is not optional here: everything this service schedules is in UTC, and
# without the zone database psycopg cannot resolve the server's timezone.
# hadolint ignore=DL3018
RUN apk add --no-cache python-3.13 tzdata

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
