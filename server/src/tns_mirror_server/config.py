"""Configuration: one YAML file, every value env-overridable.

Design §6 and A.5. Precedence, lowest to highest:

    built-in defaults  <  config file  <  environment

Credentials (the TNS marker, the bot ``api_key``, the database DSN) are
*operator-supplied* and never baked into the image or committed. The dataclasses
below redact them from ``repr()`` so a stray log line or traceback cannot leak
one (§7).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

import yaml

from .errors import AuthNotConfigured, ConfigError

__all__ = [
    "AuthConfig",
    "Config",
    "DatabaseConfig",
    "DownloadConfig",
    "ScheduleConfig",
    "load_config",
]

DEFAULT_URL: Final = (
    "https://www.wis-tns.org/system/files/tns_public_objects/tns_public_objects.csv.zip"
)
DEFAULT_WORKDIR: Final = "/var/lib/tns-mirror"
FULL_SNAPSHOT_FILENAME: Final = "tns_public_objects.csv.zip"

#: Unquoted lowercase SQL identifiers only. Config-supplied names reach DDL, so
#: they are validated here *and* rendered through psycopg's Identifier quoting
#: at the point of use — belt and braces, so no injection suppression is needed.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_REDACTED: Final = "***"


def _require_identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ConfigError(
            f"{what} must be a lowercase unquoted SQL identifier "
            f"(letters, digits, underscore; not starting with a digit), got {value!r}"
        )
    return value


# --- ${VAR} expansion -------------------------------------------------------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` in loaded YAML.

    An unset variable with no default expands to the empty string, which then
    fails validation with a useful message rather than embedding the literal
    ``${VAR}`` into a URL or DSN.
    """
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def _as_float(raw: str, name: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _as_int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


# --- sections ---------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    """TNS credentials. Exactly one mode is active; neither configured is fatal.

    ``marker``
        A registered ``tns_marker{...}`` User-Agent on a plain GET. The simplest
        path, and what most TNS accounts have.

    ``bot``
        A TNS bot: the file is requested with ``api_key`` submitted as form data
        alongside a ``tns_marker`` naming the bot. This is a POST, not a GET —
        the two modes differ in HTTP verb, not merely in headers.
    """

    mode: str = "marker"
    user_agent: str = ""
    api_key: str = ""
    bot_id: str = ""
    bot_name: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing for §7
        return (
            f"AuthConfig(mode={self.mode!r}, "
            f"user_agent={_REDACTED if self.user_agent else ''!r}, "
            f"api_key={_REDACTED if self.api_key else ''!r}, "
            f"bot_id={self.bot_id!r}, bot_name={self.bot_name!r})"
        )

    def validate(self) -> None:
        """Enforce invariant 4: authenticated to TNS, or refuse to download."""
        if self.mode == "marker":
            if not self.user_agent.strip():
                raise AuthNotConfigured(
                    "auth.mode is 'marker' but auth.user_agent (TNS_USER_AGENT) is empty. "
                    "Register a TNS account and supply its tns_marker User-Agent; "
                    "tns-mirror never downloads anonymously."
                )
        elif self.mode == "bot":
            missing = [
                name
                for name, value in (
                    ("auth.api_key (TNS_API_KEY)", self.api_key),
                    ("auth.bot_id (TNS_BOT_ID)", self.bot_id),
                    ("auth.bot_name (TNS_BOT_NAME)", self.bot_name),
                )
                if not value.strip()
            ]
            if missing:
                raise AuthNotConfigured(
                    "auth.mode is 'bot' but these are empty: "
                    + ", ".join(missing)
                    + ". Register a TNS bot and supply its credentials; "
                    "tns-mirror never downloads anonymously."
                )
        else:
            raise ConfigError(f"auth.mode must be 'marker' or 'bot', got {self.mode!r}")


@dataclass(frozen=True)
class DownloadConfig:
    url: str = DEFAULT_URL
    timeout_seconds: float = 120.0
    throttle_seconds: float = 10.0
    workdir: Path = Path(DEFAULT_WORKDIR)

    def validate(self) -> None:
        if not self.url.startswith("https://"):
            raise ConfigError(
                f"download.url must be https (TNS credentials must never cross "
                f"the wire in the clear), got {self.url!r}"
            )
        if FULL_SNAPSHOT_FILENAME not in self.url:
            raise ConfigError(
                f"download.url must end in {FULL_SNAPSHOT_FILENAME!r} — the hourly "
                f"delta filename is derived from it by rewriting that name. "
                f"Got {self.url!r}"
            )
        if self.timeout_seconds <= 0:
            raise ConfigError("download.timeout_seconds must be positive")
        if self.throttle_seconds < 0:
            raise ConfigError("download.throttle_seconds must not be negative")


@dataclass(frozen=True)
class DatabaseConfig:
    """Write credentials for the mirror's *own* database. Never shared (invariant 5)."""

    dsn: str = ""
    schema: str = "public"
    table: str = "tns_objects"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"DatabaseConfig(dsn={_REDACTED if self.dsn else ''!r}, "
            f"schema={self.schema!r}, table={self.table!r})"
        )

    def validate(self) -> None:
        _require_identifier(self.schema, "database.schema (TNS_SCHEMA)")
        _require_identifier(self.table, "database.table (TNS_TABLE)")


@dataclass(frozen=True)
class ScheduleConfig:
    """Used only by ``serve``. Kubernetes deployments use CronJobs and ignore these.

    Three jobs, resolving the design's §5.4-vs-A.6 ambiguity explicitly:

    * ``full_cron`` — the daily full snapshot.
    * ``delta_cron`` — the hourly delta (``catchup_hours`` defaults to 1, i.e.
      exactly the current hour).
    * ``catchup_cron`` — a daily 24-hour catch-up that repairs any hours missed
      while the server was down. Idempotent, so re-applying is harmless.

    An empty cron string disables that job.
    """

    full_cron: str = "0 0 * * *"
    delta_cron: str = "10 * * * *"
    catchup_cron: str = "30 0 * * *"
    catchup_hours: int = 1
    catchup_window_hours: int = 24

    def validate(self) -> None:
        if not 1 <= self.catchup_hours <= 24:
            raise ConfigError(f"schedule.catchup_hours must be 1..24, got {self.catchup_hours}")
        if not 1 <= self.catchup_window_hours <= 24:
            raise ConfigError(
                f"schedule.catchup_window_hours must be 1..24, got {self.catchup_window_hours}"
            )


@dataclass(frozen=True)
class Config:
    source: str = "tns"
    auth: AuthConfig = field(default_factory=AuthConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    def validate(self) -> Config:
        self.auth.validate()
        self.download.validate()
        self.database.validate()
        self.schedule.validate()
        return self

    # Working files. Transient — a wiped workdir costs one re-download.
    @property
    def zip_path(self) -> Path:
        return self.download.workdir / FULL_SNAPSHOT_FILENAME

    @property
    def csv_path(self) -> Path:
        return self.download.workdir / FULL_SNAPSHOT_FILENAME.removesuffix(".zip")


# --- loading ----------------------------------------------------------------


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"config section {name!r} must be a mapping, got {type(value).__name__}"
        )
    return value


def load_config(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Build a validated :class:`Config` from a YAML file and the environment.

    ``path`` defaults to ``$TNS_MIRROR_CONFIG`` if set; a missing file is only an
    error when it was named explicitly.
    """
    env = os.environ if env is None else env

    explicit = path is not None
    if path is None:
        path = env.get("TNS_MIRROR_CONFIG") or None
        explicit = path is not None

    data: Mapping[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, Mapping):
                raise ConfigError(f"{config_path} must contain a YAML mapping")
            data = _expand(loaded, env)
        elif explicit:
            raise ConfigError(f"config file not found: {config_path}")

    auth_data = _section(data, "auth")
    download_data = _section(data, "download")
    database_data = _section(data, "database")
    schedule_data = _section(data, "schedule")

    def pick(section: Mapping[str, Any], key: str, default: Any) -> Any:
        value = section.get(key, default)
        return default if value is None else value

    config = Config(
        source=str(pick(data, "source", "tns")),
        auth=AuthConfig(
            mode=str(pick(auth_data, "mode", "marker")),
            user_agent=str(pick(auth_data, "user_agent", "")),
            api_key=str(pick(auth_data, "api_key", "")),
            bot_id=str(pick(auth_data, "bot_id", "")),
            bot_name=str(pick(auth_data, "bot_name", "")),
        ),
        download=DownloadConfig(
            url=str(pick(download_data, "url", DEFAULT_URL)),
            timeout_seconds=float(pick(download_data, "timeout_seconds", 120.0)),
            throttle_seconds=float(pick(download_data, "throttle_seconds", 10.0)),
            workdir=Path(str(pick(download_data, "workdir", DEFAULT_WORKDIR))),
        ),
        database=DatabaseConfig(
            dsn=str(pick(database_data, "dsn", "")),
            schema=str(pick(database_data, "schema", "public")),
            table=str(pick(database_data, "table", "tns_objects")),
        ),
        schedule=ScheduleConfig(
            full_cron=str(pick(schedule_data, "full_cron", "0 0 * * *")),
            delta_cron=str(pick(schedule_data, "delta_cron", "10 * * * *")),
            catchup_cron=str(pick(schedule_data, "catchup_cron", "30 0 * * *")),
            catchup_hours=int(pick(schedule_data, "catchup_hours", 1)),
            catchup_window_hours=int(pick(schedule_data, "catchup_window_hours", 24)),
        ),
    )

    return _apply_env(config, env).validate()


def _apply_env(config: Config, env: Mapping[str, str]) -> Config:
    """Overlay environment variables (design §6 / A.5). Env always wins."""
    auth = config.auth
    if (v := env.get("TNS_AUTH_MODE")) is not None:
        auth = replace(auth, mode=v)
    if (v := env.get("TNS_USER_AGENT")) is not None:
        auth = replace(auth, user_agent=v)
    if (v := env.get("TNS_API_KEY")) is not None:
        auth = replace(auth, api_key=v)
    if (v := env.get("TNS_BOT_ID")) is not None:
        auth = replace(auth, bot_id=v)
    if (v := env.get("TNS_BOT_NAME")) is not None:
        auth = replace(auth, bot_name=v)

    download = config.download
    if (v := env.get("TNS_URL")) is not None:
        download = replace(download, url=v)
    if (v := env.get("TNS_TIMEOUT")) is not None:
        download = replace(download, timeout_seconds=_as_float(v, "TNS_TIMEOUT"))
    if (v := env.get("TNS_THROTTLE")) is not None:
        download = replace(download, throttle_seconds=_as_float(v, "TNS_THROTTLE"))
    if (v := env.get("TNS_WORKDIR")) is not None:
        download = replace(download, workdir=Path(v))

    database = config.database
    if (v := env.get("DATABASE_URL")) is not None:
        database = replace(database, dsn=v)
    if (v := env.get("TNS_SCHEMA")) is not None:
        database = replace(database, schema=v)
    if (v := env.get("TNS_TABLE")) is not None:
        database = replace(database, table=v)

    schedule = config.schedule
    if (v := env.get("TNS_FULL_CRON")) is not None:
        schedule = replace(schedule, full_cron=v)
    if (v := env.get("TNS_DELTA_CRON")) is not None:
        schedule = replace(schedule, delta_cron=v)
    if (v := env.get("TNS_CATCHUP_CRON")) is not None:
        schedule = replace(schedule, catchup_cron=v)
    if (v := env.get("TNS_CATCHUP_HOURS")) is not None:
        schedule = replace(schedule, catchup_hours=_as_int(v, "TNS_CATCHUP_HOURS"))
    if (v := env.get("TNS_CATCHUP_WINDOW_HOURS")) is not None:
        schedule = replace(
            schedule, catchup_window_hours=_as_int(v, "TNS_CATCHUP_WINDOW_HOURS")
        )

    source = env.get("TNS_SOURCE", config.source)
    return Config(
        source=source, auth=auth, download=download, database=database, schedule=schedule
    )
