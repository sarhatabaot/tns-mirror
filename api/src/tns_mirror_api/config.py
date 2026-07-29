"""Configuration for the read-only HTTP API.

Environment only. This service has no config file because it has almost nothing
to configure: where the database is, how hard callers may hit it, and how large
a query they may ask for.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

__all__ = ["Config", "ConfigError", "load_config"]

#: Beyond this, a "cone search" is a table scan wearing a hat. TNS is ~10**5
#: rows, so a degree is already generous for the cross-match this serves.
DEFAULT_MAX_RADIUS_ARCSEC: Final = 3600.0
DEFAULT_MAX_LIMIT: Final = 1000


class ConfigError(Exception):
    """Configuration is missing or malformed."""


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    #: Read-only DSN. Empty means libpq reads the standard PG* variables.
    dsn: str = ""
    schema: str = "public"
    table: str = "tns_objects"

    pool_min_size: int = 1
    pool_max_size: int = 10

    #: Requests per minute, per caller. Generous by intent — this is a public
    #: read API over a catalogue that is already public.
    rate_limit_per_minute: int = 60

    #: Trust X-Forwarded-For for the caller's identity. Off by default, and
    #: that default is the security-relevant part: with it on and no proxy in
    #: front, anyone can set the header and get a fresh bucket per request.
    trust_proxy: bool = False

    max_radius_arcsec: float = DEFAULT_MAX_RADIUS_ARCSEC
    max_limit: int = DEFAULT_MAX_LIMIT

    #: Refuse to serve from a mirror that has not synced recently. A stale
    #: catalogue answering confidently is worse than an honest 503.
    max_age_hours: float = 26.0

    cors_origins: tuple[str, ...] = field(default_factory=tuple)

    #: Mount every route under this prefix, for running behind a reverse proxy
    #: at e.g. https://example.org/tns/. Empty means mount at the root.
    #:
    #: Set this when the proxy passes the prefix through. If instead your proxy
    #: *strips* it before forwarding, leave this empty — the app is already
    #: being asked for the paths it serves.
    root_path: str = ""

    def validate(self) -> Config:
        if self.pool_min_size < 1 or self.pool_max_size < self.pool_min_size:
            raise ConfigError(
                f"pool sizes must satisfy 1 <= min <= max, got "
                f"min={self.pool_min_size} max={self.pool_max_size}"
            )
        if self.rate_limit_per_minute < 1:
            raise ConfigError("TNS_API_RATE_LIMIT must be at least 1")
        if self.max_radius_arcsec <= 0:
            raise ConfigError("TNS_API_MAX_RADIUS_ARCSEC must be positive")
        if self.max_limit < 1:
            raise ConfigError("TNS_API_MAX_LIMIT must be at least 1")
        if self.root_path and not self.root_path.startswith("/"):
            raise ConfigError(f"TNS_API_ROOT_PATH must start with '/', got {self.root_path!r}")
        return self


def _normalise_prefix(raw: str) -> str:
    """Turn any of '', 'tns', '/tns', '/tns/' into '' or '/tns'."""
    stripped = raw.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    origins = tuple(
        origin.strip()
        for origin in env.get("TNS_API_CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    return Config(
        dsn=env.get("DATABASE_URL", ""),
        schema=env.get("TNS_SCHEMA", "public"),
        table=env.get("TNS_TABLE", "tns_objects"),
        pool_min_size=_int(env, "TNS_API_POOL_MIN", 1),
        pool_max_size=_int(env, "TNS_API_POOL_MAX", 10),
        rate_limit_per_minute=_int(env, "TNS_API_RATE_LIMIT", 60),
        trust_proxy=_bool(env, "TNS_API_TRUST_PROXY", False),
        max_radius_arcsec=_float(env, "TNS_API_MAX_RADIUS_ARCSEC", DEFAULT_MAX_RADIUS_ARCSEC),
        max_limit=_int(env, "TNS_API_MAX_LIMIT", DEFAULT_MAX_LIMIT),
        max_age_hours=_float(env, "TNS_API_MAX_AGE_HOURS", 26.0),
        cors_origins=origins,
        # Normalised so '/tns', 'tns/' and '/tns/' all behave the same — the
        # difference is invisible in a browser and maddening in a config file.
        root_path=_normalise_prefix(env.get("TNS_API_ROOT_PATH", "")),
    ).validate()
