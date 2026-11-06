"""CLI wiring, exit codes, and the grants output."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from conftest import make_config
from tns_mirror_server import cli
from tns_mirror_server.adapters.memory import FakeHistoricSource, FakeSource, MemoryStore
from tns_mirror_server.ports.historic import HistoricRecord
from tns_mirror_server.ports.source import TnsRecord

MARKER = 'tns_marker{"tns_id":"1234","type":"user","name":"tester"}'


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the CLI at an in-memory store and a canned source."""
    store = MemoryStore()
    source = FakeSource(
        full=[TnsRecord(objid=1, name="AT2026abc", ra=203.1, dec=10.2)],
        deltas={5: [TnsRecord(objid=2, name="SN2026xyz", ra=10.5, dec=-45.25)]},
    )

    @contextmanager
    def fake_store(_config):
        yield store

    monkeypatch.setattr(cli, "_store", fake_store)
    monkeypatch.setattr(cli, "load_config", lambda _path: make_config(tmp_path))
    monkeypatch.setattr(cli, "build_source", lambda _config: source)
    monkeypatch.setattr(cli, "build_historic_source", lambda _config: FakeHistoricSource())
    return store, source


HISTORIC = HistoricRecord(
    objid=6568,
    name="SN2015bh",
    ra=137.396,
    dec=33.123,
    ra_hms="09:09:35.059",
    dec_dms="+33:07:22.12",
    obj_type="SN IIn",
)


@pytest.fixture
def wired_historic(monkeypatch, wired):
    """``wired`` plus a historic feed that actually has rows."""
    historic = FakeHistoricSource([HISTORIC])
    monkeypatch.setattr(cli, "build_historic_source", lambda _config: historic)
    return (*wired, historic)


def test_migrate_reports_what_it_applied(wired, capsys):
    assert cli.main(["migrate"]) == 0
    assert "0001_initial" in capsys.readouterr().out


def test_migrate_is_idempotent(wired, capsys):
    cli.main(["migrate"])
    capsys.readouterr()

    assert cli.main(["migrate"]) == 0
    assert "already up to date" in capsys.readouterr().out


def test_sync_runs_a_full_snapshot_by_default(wired, capsys):
    store, source = wired

    assert cli.main(["sync"]) == 0

    assert source.downloaded == [None]
    assert store.count() == 1
    assert "full snapshot" in capsys.readouterr().out


def test_sync_with_an_hour_runs_a_delta(wired, capsys):
    store, source = wired

    assert cli.main(["sync", "--hour", "5"]) == 0

    assert source.downloaded == [5]
    assert store.names() == {"SN2026xyz"}
    assert "delta hour 05" in capsys.readouterr().out


def test_catch_up_defaults_to_the_configured_window(wired, capsys):
    _, source = wired

    assert cli.main(["catch-up"]) == 0

    assert len(source.downloaded) == 24  # schedule.catchup_window_hours


def test_catch_up_accepts_an_explicit_window(wired):
    _, source = wired

    assert cli.main(["catch-up", "3"]) == 0

    assert len(source.downloaded) == 3


def test_sync_historic_fills_its_own_table_only(wired_historic, capsys):
    store, source, historic = wired_historic

    assert cli.main(["sync-historic"]) == 0

    assert historic.fetches == [False]
    assert source.downloaded == []  # the catalogue feed was not touched
    assert store.count_historic() == 1
    assert store.count() == 0
    assert "historic SNe" in capsys.readouterr().out


def test_sync_historic_can_reuse_the_downloaded_export(wired_historic):
    _, _, historic = wired_historic

    assert cli.main(["sync-historic", "--reuse-existing"]) == 0

    assert historic.fetches == [True]


def test_redownload_and_reuse_existing_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sync", "--redownload", "--reuse-existing"])


def test_status_reports_freshness(wired, capsys):
    cli.main(["sync"])
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "schema_version:        1" in out
    assert "rows:                  1" in out
    assert "historic_rows:         0" in out
    assert "last_historic_sync_at: never" in out


def test_status_exits_stale_when_nothing_has_ever_synced(wired, capsys):
    # A row count alone cannot distinguish "synced an hour ago" from "died three
    # weeks ago", which is why the healthcheck asks about age.
    assert cli.main(["status", "--max-age-hours", "26"]) == cli.EXIT_STALE
    assert "no sync has ever completed" in capsys.readouterr().err


def test_status_is_healthy_after_a_sync(wired):
    cli.main(["sync"])
    assert cli.main(["status", "--max-age-hours", "26"]) == 0


# --- grants -----------------------------------------------------------------


def statements(output: str) -> str:
    """The executable SQL only — comments explain intent and must not be asserted on."""
    lines = [line for line in output.splitlines() if not line.strip().startswith("--")]
    return " ".join(" ".join(lines).split())


def test_print_grants_is_least_privilege(wired, capsys):
    assert cli.main(["print-grants", "--database", "tnsdb", "--role", "tns_ro"]) == 0
    sql = statements(capsys.readouterr().out)

    assert "GRANT SELECT ON public.tns_objects TO tns_ro;" in sql
    assert "GRANT SELECT ON public.tns_historic_sne TO tns_ro;" in sql
    # The client needs the metadata table to check the schema version it was
    # built against — granting only the catalogue leaves it unable to.
    assert "GRANT SELECT ON public.tns_mirror_meta TO tns_ro;" in sql
    assert "CREATE ROLE tns_ro LOGIN PASSWORD :'pw';" in sql
    assert "GRANT CONNECT ON DATABASE tnsdb TO tns_ro;" in sql


def test_print_grants_does_not_widen_to_future_tables(wired, capsys):
    # ALTER DEFAULT PRIVILEGES would hand the reader SELECT on every table
    # created in the schema afterwards, which is not "SELECT only, one table".
    cli.main(["print-grants"])
    sql = statements(capsys.readouterr().out)

    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "ALL PRIVILEGES" not in sql


def test_print_grants_aligns_for_any_schema_name(monkeypatch, wired, tmp_path, capsys):
    from dataclasses import replace

    config = make_config(tmp_path)
    config = replace(config, database=replace(config.database, schema="astro_catalogues"))
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    cli.main(["print-grants"])
    sql = statements(capsys.readouterr().out)

    assert "GRANT SELECT ON astro_catalogues.tns_objects TO tns_ro;" in sql
    assert "GRANT SELECT ON astro_catalogues.tns_historic_sne TO tns_ro;" in sql
    assert "GRANT SELECT ON astro_catalogues.tns_mirror_meta TO tns_ro;" in sql


def test_print_grants_generates_no_password(wired, capsys):
    cli.main(["print-grants"])
    out = capsys.readouterr().out

    # The operator supplies it out of band via a psql variable, so it never
    # reaches a shell history or a CI log.
    assert ":'pw'" in out
    assert "PASSWORD '" not in out


# --- errors and serve -------------------------------------------------------


def test_configuration_errors_exit_non_zero_without_a_traceback(monkeypatch, capsys):
    from tns_mirror_server.errors import AuthNotConfigured

    def explode(_path):
        raise AuthNotConfigured("no TNS credential configured")

    monkeypatch.setattr(cli, "load_config", explode)

    assert cli.main(["sync"]) == cli.EXIT_ERROR
    assert "Traceback" not in capsys.readouterr().err


def test_serve_seeds_an_empty_catalogue_then_schedules(wired):
    store, source = wired

    assert cli.main(["serve", "--max-iterations", "0"]) == 0

    assert store.migrated is True
    assert store.count() == 1  # initial full snapshot taken
    assert source.downloaded == [None]


def test_serve_seeds_an_empty_historic_table_too(wired_historic):
    store, _, historic = wired_historic

    assert cli.main(["serve", "--max-iterations", "0"]) == 0

    assert historic.fetches == [False]
    assert store.count_historic() == 1


def test_serve_survives_a_failed_historic_seed(monkeypatch, wired, caplog):
    # The catalogue is the service. A rate-limited search page on first start
    # must not stop serve from running the schedule (invariant 7).
    import requests

    from tns_mirror_server.errors import SourceError

    store, _ = wired
    failing = FakeHistoricSource(SourceError("TNS said no"))
    monkeypatch.setattr(cli, "build_historic_source", lambda _config: failing)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["serve", "--max-iterations", "0"]) == 0

    assert store.count() == 1  # the catalogue seed still happened
    assert store.count_historic() == 0
    assert "initial historic fetch failed" in caplog.text
    del requests  # imported only to document that RequestException is handled too


def test_serve_skips_historic_entirely_when_its_cron_is_empty(monkeypatch, wired, tmp_path):
    from dataclasses import replace

    def refuse(_config):
        raise AssertionError("historic source must not be built when disabled")

    monkeypatch.setattr(cli, "build_historic_source", refuse)
    config = make_config(tmp_path)
    config = replace(config, schedule=replace(config.schedule, historic_cron=""))
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    assert cli.main(["serve", "--max-iterations", "0"]) == 0


def test_serve_does_not_reseed_a_populated_catalogue(wired):
    _, source = wired
    cli.main(["sync"])
    source.downloaded.clear()

    assert cli.main(["serve", "--max-iterations", "0"]) == 0

    assert source.downloaded == []


def test_serve_refuses_when_every_schedule_is_empty(monkeypatch, wired, tmp_path):
    from dataclasses import replace

    config = make_config(tmp_path)
    config = replace(
        config,
        schedule=replace(
            config.schedule, full_cron="", delta_cron="", catchup_cron="", historic_cron=""
        ),
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    assert cli.main(["serve", "--max-iterations", "0"]) == cli.EXIT_ERROR


# --- commands that never contact TNS ----------------------------------------


def test_local_commands_work_without_a_tns_credential(monkeypatch, tmp_path, capsys):
    """migrate, status and print-grants must not demand a TNS account.

    print-grants is how an operator creates the read-only role, and needing a
    TNS marker to print SQL against a local database is a barrier with nothing
    behind it. This is the flow the quickstart documents, in the order it
    documents it, before any credential exists.
    """
    from contextlib import contextmanager

    store = MemoryStore()

    @contextmanager
    def fake_store(_config):
        yield store

    monkeypatch.setattr(cli, "_store", fake_store)
    monkeypatch.setattr(cli, "load_config", lambda _path: make_config(tmp_path, user_agent=""))

    assert cli.main(["migrate"]) == 0
    assert cli.main(["status"]) == 0
    assert cli.main(["print-grants", "--database", "tnsdb"]) == 0
    assert "GRANT SELECT" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["sync"], ["sync", "--hour", "3"], ["catch-up", "2"], ["serve"]]
)
def test_download_commands_still_refuse_without_a_credential(
    monkeypatch, tmp_path, argv, caplog
):
    """Invariant 4: authenticated to TNS, or no download. Checked up front."""
    from contextlib import contextmanager

    @contextmanager
    def fake_store(_config):
        yield MemoryStore()

    monkeypatch.setattr(cli, "_store", fake_store)
    monkeypatch.setattr(cli, "load_config", lambda _path: make_config(tmp_path, user_agent=""))

    with caplog.at_level(logging.ERROR, logger="tns_mirror_server"):
        assert cli.main(argv) == cli.EXIT_ERROR

    assert "never downloads anonymously" in caplog.text
