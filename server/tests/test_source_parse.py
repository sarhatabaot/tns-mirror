"""Parser behaviour: preamble, column aliases, name join, bad-row tolerance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import SAMPLE_CSV, make_config
from tns_mirror_server.adapters.source_tns import TnsPublicObjects
from tns_mirror_server.errors import ParseError


def parse_text(tmp_path, text: str):
    path = tmp_path / "catalog.csv"
    path.write_text(text, encoding="utf-8")
    return list(TnsPublicObjects(make_config(tmp_path)).parse(path))


def test_skips_preamble_and_reads_every_column(tmp_path):
    records = parse_text(tmp_path, SAMPLE_CSV)

    assert [r.objid for r in records] == [1001, 1002]
    first, second = records
    assert first.name == "AT2026abc"  # name_prefix + name joined
    assert first.ra == pytest.approx(203.1)
    assert first.dec == pytest.approx(10.2)
    assert first.type is None  # empty cell -> NULL, not ""
    assert first.redshift is None
    assert first.discoverydate == datetime(2026, 7, 1, 3, 22, 11, tzinfo=UTC)
    assert first.discoverymag == pytest.approx(19.4)
    assert first.internal_names == "ZTF26aaa"
    assert first.reporting_group == "ZTF"

    assert second.name == "SN2026xyz"
    assert second.type == "SN Ia"
    assert second.redshift == pytest.approx(0.031)
    assert second.dec == pytest.approx(-45.25)


def test_tolerates_alternative_column_spellings(tmp_path):
    # TNS has shipped several spellings over the years; a rename upstream should
    # degrade to "column absent", not "mirror down".
    text = "TNSName,RAdeg,DEJ2000,ObjID\nAT2026aaa,1.5,2.5,77\n"
    (record,) = parse_text(tmp_path, text)

    assert record.name == "AT2026aaa"
    assert record.ra == pytest.approx(1.5)
    assert record.dec == pytest.approx(2.5)
    assert record.objid == 77
    assert record.type is None


def test_rows_that_cannot_be_used_are_skipped_not_fatal(tmp_path):
    text = (
        "objid,name_prefix,name,ra,declination\n"
        "1,AT,good,1.0,2.0\n"
        "2,AT,bad_ra,not-a-number,2.0\n"
        "3,AT,,5.0,6.0\n"  # no name
        "4,AT\n"  # truncated row
        ",AT,no_id,7.0,8.0\n"  # parses, but the engine will drop it
        "5,SN,alsogood,9.0,10.0\n"
    )
    records = parse_text(tmp_path, text)

    assert [r.name for r in records] == ["ATgood", "ATno_id", "SNalsogood"]
    assert records[1].objid is None


def test_missing_header_raises_a_valueerror(tmp_path):
    # The engine tolerates a quiet delta hour by catching ValueError, so ParseError
    # must remain one — that inheritance is load-bearing, not decorative.
    with pytest.raises(ParseError) as excinfo:
        parse_text(tmp_path, "just,some,noise\n1,2,3\n")

    assert isinstance(excinfo.value, ValueError)


def test_name_without_prefix_column_is_used_verbatim(tmp_path):
    (record,) = parse_text(tmp_path, "objid,name,ra,dec\n9,SN2026zzz,3.0,4.0\n")
    assert record.name == "SN2026zzz"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-07-01 03:22:11.000", datetime(2026, 7, 1, 3, 22, 11, tzinfo=UTC)),
        ("2026-07-01 03:22:11", datetime(2026, 7, 1, 3, 22, 11, tzinfo=UTC)),
        ("2026-07-01T03:22:11", datetime(2026, 7, 1, 3, 22, 11, tzinfo=UTC)),
        ("2026-07-01", datetime(2026, 7, 1, tzinfo=UTC)),
        ("not a date", None),
        ("", None),
    ],
)
def test_discovery_dates_are_parsed_as_utc(tmp_path, raw, expected):
    text = f"objid,name,ra,dec,discoverydate\n1,AT2026a,1.0,2.0,{raw}\n"
    (record,) = parse_text(tmp_path, text)
    assert record.discoverydate == expected
