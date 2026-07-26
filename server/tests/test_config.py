"""Configuration: precedence, expansion, validation, and secret redaction."""

from __future__ import annotations

import re

import pytest

from tns_mirror_server.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    DownloadConfig,
    load_config,
)
from tns_mirror_server.errors import AuthNotConfigured, ConfigError

MARKER = 'tns_marker{"tns_id":"1234","type":"user","name":"tester"}'
BASE_ENV = {"TNS_USER_AGENT": MARKER}


def write_yaml(tmp_path, text: str):
    path = tmp_path / "tns-mirror.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_apply_when_only_a_credential_is_supplied():
    config = load_config(None, BASE_ENV)

    assert config.auth.mode == "marker"
    assert config.download.timeout_seconds == 120.0
    assert config.download.throttle_seconds == 10.0
    assert config.database.table == "tns_objects"
    assert config.schedule.full_cron == "0 0 * * *"
    assert config.schedule.catchup_window_hours == 24


def test_environment_overrides_the_config_file(tmp_path):
    path = write_yaml(
        tmp_path,
        "download:\n  timeout_seconds: 30\ndatabase:\n  table: from_file\n",
    )

    config = load_config(path, BASE_ENV | {"TNS_TABLE": "from_env"})

    assert config.database.table == "from_env"  # env wins
    assert config.download.timeout_seconds == 30.0  # file still applies


def test_yaml_expands_environment_variables(tmp_path):
    path = write_yaml(
        tmp_path,
        "auth:\n"
        "  user_agent: ${MY_MARKER}\n"
        "database:\n"
        "  dsn: postgresql://user:${DB_PASSWORD}@db/tnsdb\n"
        "  table: ${MISSING_TABLE:-tns_objects}\n",
    )

    config = load_config(path, {"MY_MARKER": MARKER, "DB_PASSWORD": "hunter2"})

    assert config.auth.user_agent == MARKER
    assert config.database.dsn == "postgresql://user:hunter2@db/tnsdb"
    assert config.database.table == "tns_objects"  # :- default used


def test_named_config_file_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml", BASE_ENV)


# --- invariant 4 ------------------------------------------------------------


def test_marker_mode_without_a_marker_refuses():
    with pytest.raises(AuthNotConfigured, match="TNS_USER_AGENT"):
        load_config(None, {})


def test_bot_mode_lists_every_missing_credential():
    with pytest.raises(AuthNotConfigured) as excinfo:
        load_config(None, {"TNS_AUTH_MODE": "bot", "TNS_API_KEY": "k"})

    message = str(excinfo.value)
    assert "TNS_BOT_ID" in message
    assert "TNS_BOT_NAME" in message
    assert "TNS_API_KEY" not in message  # that one was supplied


def test_bot_mode_accepts_a_complete_credential_set():
    config = load_config(
        None,
        {
            "TNS_AUTH_MODE": "bot",
            "TNS_API_KEY": "k",
            "TNS_BOT_ID": "42",
            "TNS_BOT_NAME": "mirrorbot",
        },
    )
    assert config.auth.mode == "bot"


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["tns objects", "tns-objects", "1objects", 'x"; DROP TABLE users; --', "Objects", ""],
)
def test_table_names_that_are_not_plain_identifiers_are_rejected(table):
    # These names reach DDL. psycopg's Identifier quoting is what makes that
    # safe; this validation is defence in depth and a much better error message.
    with pytest.raises(ConfigError, match="identifier"):
        load_config(None, BASE_ENV | {"TNS_TABLE": table})


def test_download_url_must_be_https():
    url = "http://www.wis-tns.org/x/tns_public_objects.csv.zip"
    with pytest.raises(ConfigError, match="must be https"):
        load_config(None, BASE_ENV | {"TNS_URL": url})


def test_download_url_must_name_the_snapshot_file():
    # The hourly delta filename is derived by rewriting this name; a URL that
    # does not contain it would silently only ever fetch the full snapshot.
    with pytest.raises(ConfigError, match=re.escape("tns_public_objects.csv.zip")):
        load_config(None, BASE_ENV | {"TNS_URL": "https://example.org/catalogue.zip"})


def test_numeric_settings_report_the_offending_variable():
    with pytest.raises(ConfigError, match="TNS_TIMEOUT"):
        load_config(None, BASE_ENV | {"TNS_TIMEOUT": "soon"})


def test_catchup_window_must_fit_in_a_day():
    with pytest.raises(ConfigError, match="catchup_window_hours"):
        load_config(None, BASE_ENV | {"TNS_CATCHUP_WINDOW_HOURS": "48"})


# --- secrets ----------------------------------------------------------------


def test_credentials_are_redacted_from_repr():
    # A traceback or a debug log must not be able to leak a TNS api_key or a DSN
    # password (design §7).
    config = Config(
        auth=AuthConfig(mode="bot", api_key="super-secret-key", bot_id="1", bot_name="b"),
        database=DatabaseConfig(dsn="postgresql://user:hunter2@db/tnsdb"),
        download=DownloadConfig(),
    )

    rendered = repr(config)

    assert "super-secret-key" not in rendered
    assert "hunter2" not in rendered
    assert "***" in rendered
    assert "mode='bot'" in rendered  # non-secret fields stay useful
