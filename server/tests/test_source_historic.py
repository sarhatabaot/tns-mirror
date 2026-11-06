"""The historic-SNe source: paging the TNS search export, and parsing it.

TNS's own etiquette for the search page is the thing under test here — read the
rate-limit headers, pause when they say to, never store an HTML page as a
catalogue, and never leave a half-fetched list where a sync would read it.
"""

from __future__ import annotations

import pytest
import requests

from conftest import make_config
from tns_mirror_server.adapters.source_tns_historic import (
    MAX_RATE_LIMIT_RETRIES,
    TnsHistoricSne,
    normalise_name,
    sexagesimal_to_degrees,
)
from tns_mirror_server.errors import AuthNotConfigured, ParseError, SourceError

HEADER = (
    '"ID","Name","RA","DEC","Obj. Type","Redshift","Host Name","Host Redshift",'
    '"Reporting Group/s","Discovery Data Source/s","Classifying Group/s","Associated Group/s",'
    '"Disc. Internal Name","Disc. Instrument/s","Class. Instrument/s","TNS AT","Public",'
    '"End Prop. Period","Discovery Mag/Flux","Discovery Filter","Discovery Date (UT)","Sender",'
    '"Remarks","Unreal","Discovery Bibcode","Classification Bibcodes","Ext. catalog/s",'
    '"Auto classification"'
)

ROW_6568 = (
    '"6568","SN 2015bh","09:09:35.059","+33:07:22.12","SN IIn","","NGC2770","","","","","",'
    '"","","","0","1","","19.1","","2015-02-07 00:00:00.000","",'
    '"[Discoverer=Howerton, Drake et al. (Catalina Real-time Transient Survey); '
    "DiscoveryRef=CBET 4229; PositionRef=CBET 4229; Orig Type=IIn] "
    '[Object cross-matched with NGC2770]","","","","",""'
)
ROW_6550 = (
    '"6550","SN 2015K","23:35:52.260","+23:36:52.09","SN Ic","","NGC7712","","","","","",'
    '"","","","0","1","","16.3","","2015-04-25 00:00:00.000","",'
    '"[Discoverer=Itagaki; DiscoveryRef=CBET 4116]","","","","",""'
)
ROW_SOUTH = (
    '"6551","SN 2015L","22:02:15.451","-61:39:34.60","SN I","","Anon.           ","","","","",'
    '"","","","","0","1","","17","","2015-06-14 00:00:00.000","",'
    '"[Discoverer=Dong et al.]","","","","",""'
)


def page(*rows: str, header: str = HEADER) -> str:
    return "\n".join((header, *rows)) + "\n"


class PageResponse:
    """One search-page reply, with the rate-limit headers TNS sends."""

    def __init__(
        self,
        text: str,
        status: int = 200,
        *,
        remaining: int | None = 9,
        reset: int = 60,
        url: str = "https://www.wis-tns.org/search",
    ):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers: dict[str, str] = {}
        if remaining is not None:
            self.headers = {
                "x-rate-limit-limit": "10",
                "x-rate-limit-remaining": str(remaining),
                "x-rate-limit-reset": str(reset),
            }

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class PagingSession:
    """Serves queued page responses and records every request."""

    def __init__(self, responses: list[PageResponse]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers=None, data=None, params=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "data": data,
                "params": params or {},
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected extra request: page {params.get('page')}")
        return self.responses.pop(0)


def build(tmp_path, responses, *, page_size=2, **kwargs):
    from dataclasses import replace

    config = make_config(tmp_path, **kwargs)
    config = replace(config, download=replace(config.download, historic_page_size=page_size))
    session = PagingSession(responses)
    return TnsHistoricSne(config, session=session), session, config


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "tns_mirror_server.adapters.source_tns_historic.time.sleep", slept.append
    )
    return slept


# --- fetch: paging ------------------------------------------------------------


def test_fetch_pages_until_a_short_page_and_keeps_one_header(tmp_path, no_sleep):
    source, session, config = build(
        tmp_path, [PageResponse(page(ROW_6568, ROW_6550)), PageResponse(page(ROW_SOUTH))]
    )

    path = source.fetch()

    assert path == config.historic_csv_path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # one header + three rows, not two headers
    assert lines[0].startswith('"ID","Name","RA","DEC"')
    assert [c["params"]["page"] for c in session.calls] == ["0", "1"]
    assert session.calls[0]["params"]["isTNS_AT"] == "no"
    assert session.calls[0]["params"]["format"] == "csv"
    assert session.calls[0]["params"]["num_page"] == "2"
    assert session.calls[0]["url"] == "https://www.wis-tns.org/search"
    assert list(tmp_path.glob("*.tmp")) == []


def test_fetch_stops_on_a_header_only_page(tmp_path, no_sleep):
    # A full last page cannot be told from a non-last one, so the next request
    # comes back with the header alone; that is the end.
    source, session, _ = build(
        tmp_path, [PageResponse(page(ROW_6568, ROW_6550)), PageResponse(page())]
    )

    path = source.fetch()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    assert len(session.calls) == 2


def test_fetch_sends_the_marker_and_no_api_key_in_marker_mode(tmp_path, no_sleep):
    marker = 'tns_marker{"tns_id":"1234","type":"user","name":"tester"}'
    source, session, _ = build(tmp_path, [PageResponse(page(ROW_6568))], user_agent=marker)

    source.fetch()

    (call,) = session.calls
    assert call["method"] == "GET"
    assert call["headers"]["User-Agent"] == marker
    assert call["data"] is None


def test_fetch_posts_the_api_key_in_bot_mode(tmp_path, no_sleep):
    source, session, _ = build(
        tmp_path,
        [PageResponse(page(ROW_6568))],
        mode="bot",
        user_agent="",
        api_key="s3cret",
        bot_id="99",
        bot_name="mirrorbot",
    )

    source.fetch()

    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["data"] == {"api_key": "s3cret"}
    assert call["headers"]["User-Agent"].startswith("tns_marker")


def test_fetch_refuses_anonymously(tmp_path, no_sleep):
    # Invariant 4 applies to the search page exactly as to the staged files.
    source, session, _ = build(tmp_path, [], user_agent="  ")

    with pytest.raises(AuthNotConfigured, match="never downloads anonymously"):
        source.fetch()

    assert session.calls == []


def test_fetch_can_reuse_an_existing_export(tmp_path, no_sleep):
    source, session, config = build(tmp_path, [])
    config.historic_csv_path.parent.mkdir(parents=True, exist_ok=True)
    config.historic_csv_path.write_text(page(ROW_6568), encoding="utf-8")

    assert source.fetch(reuse_existing=True) == config.historic_csv_path
    assert session.calls == []


# --- fetch: rate limit ----------------------------------------------------------


def test_fetch_pauses_for_the_reset_window_when_remaining_hits_zero(tmp_path, no_sleep):
    # TNS's stated best practice: when x-rate-limit-remaining reaches 0, wait
    # x-rate-limit-reset seconds *before* the next request, not after a 429.
    source, session, _ = build(
        tmp_path,
        [
            PageResponse(page(ROW_6568, ROW_6550), remaining=0, reset=42),
            PageResponse(page(ROW_SOUTH), remaining=9),
        ],
    )

    source.fetch()

    assert no_sleep == [43.0]
    assert len(session.calls) == 2


def test_fetch_does_not_pause_while_quota_remains(tmp_path, no_sleep):
    source, _, _ = build(
        tmp_path,
        [PageResponse(page(ROW_6568, ROW_6550), remaining=5), PageResponse(page(ROW_SOUTH))],
    )

    source.fetch()

    assert no_sleep == []


def test_fetch_falls_back_to_the_throttle_without_rate_limit_headers(tmp_path, no_sleep):
    source, _, _ = build(
        tmp_path,
        [
            PageResponse(page(ROW_6568, ROW_6550), remaining=None),
            PageResponse(page(ROW_SOUTH), remaining=None),
        ],
        throttle=7.5,
    )

    source.fetch()

    assert no_sleep == [7.5]


def test_fetch_waits_out_a_429_and_retries_the_same_page(tmp_path, no_sleep):
    source, session, _ = build(
        tmp_path,
        [
            PageResponse("", status=429, remaining=0, reset=30),
            PageResponse(page(ROW_6568)),
        ],
    )

    source.fetch()

    assert no_sleep == [31.0]
    assert [c["params"]["page"] for c in session.calls] == ["0", "0"]


def test_fetch_gives_up_after_repeated_429s_with_the_original_error(tmp_path, no_sleep):
    # The HTTPError must surface intact so the caller can tell 429 apart.
    responses = [
        PageResponse("", status=429, remaining=0, reset=1)
        for _ in range(MAX_RATE_LIMIT_RETRIES + 1)
    ]
    source, session, config = build(tmp_path, responses)

    with pytest.raises(requests.HTTPError) as excinfo:
        source.fetch()

    assert excinfo.value.response.status_code == 429
    assert len(session.calls) == MAX_RATE_LIMIT_RETRIES + 1
    assert not config.historic_csv_path.exists()


# --- fetch: failure modes ---------------------------------------------------------


def test_fetch_refuses_to_store_an_html_page(tmp_path, no_sleep):
    # An expired credential gets a login page back with HTTP 200. That is not a
    # catalogue, and must not become one.
    source, _, config = build(
        tmp_path, [PageResponse("<!DOCTYPE html>\n<html><body>Log in</body></html>")]
    )

    with pytest.raises(SourceError, match="did not return CSV"):
        source.fetch()

    assert not config.historic_csv_path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_fetch_refuses_an_empty_export(tmp_path, no_sleep):
    # Zero rows would go on to wipe the table; refuse before it gets that far.
    source, _, config = build(tmp_path, [PageResponse(page())])

    with pytest.raises(SourceError, match="no historic rows"):
        source.fetch()

    assert not config.historic_csv_path.exists()


def test_a_failure_mid_way_leaves_the_previous_export_in_place(tmp_path, no_sleep):
    source, _, config = build(
        tmp_path,
        [PageResponse(page(ROW_6568, ROW_6550)), PageResponse("boom", status=500)],
    )
    config.historic_csv_path.parent.mkdir(parents=True, exist_ok=True)
    config.historic_csv_path.write_text("previous good list\n", encoding="utf-8")

    with pytest.raises(requests.HTTPError):
        source.fetch()

    assert config.historic_csv_path.read_text(encoding="utf-8") == "previous good list\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_rejected_credentials_are_explained(tmp_path, no_sleep):
    source, _, _ = build(tmp_path, [PageResponse("Forbidden", status=403)])

    with pytest.raises(SourceError, match="TNS rejected the marker"):
        source.fetch()


# --- parse ------------------------------------------------------------------------


def test_parse_keeps_tns_columns_verbatim_and_derives_degrees(tmp_path):
    source, _, _ = build(tmp_path, [])
    path = tmp_path / "hist.csv"
    path.write_text(page(ROW_6568), encoding="utf-8")

    (record,) = list(source.parse(path))

    assert record.objid == 6568
    assert record.name == "SN2015bh"  # the one normalisation
    assert record.ra_hms == "09:09:35.059"
    assert record.dec_dms == "+33:07:22.12"
    assert record.ra == pytest.approx(137.39607917, abs=1e-6)
    assert record.dec == pytest.approx(33.12281111, abs=1e-6)
    assert record.obj_type == "SN IIn"
    assert record.host_name == "NGC2770"
    assert record.discovery_mag_flux == "19.1"  # still a string
    assert record.discovery_date_ut == "2015-02-07 00:00:00.000"
    assert record.tns_at == "0"
    assert record.public == "1"
    assert record.remarks.startswith("[Discoverer=Howerton")
    # Empty cells are None, not ''.
    assert record.redshift is None
    assert record.reporting_groups is None
    assert record.auto_classification is None


def test_parse_handles_southern_declinations(tmp_path):
    source, _, _ = build(tmp_path, [])
    path = tmp_path / "hist.csv"
    path.write_text(page(ROW_SOUTH), encoding="utf-8")

    (record,) = list(source.parse(path))

    assert record.dec == pytest.approx(-61.65961111, abs=1e-6)
    assert record.host_name == "Anon."  # trailing padding trimmed


def test_parse_drops_rows_without_a_usable_position(tmp_path):
    source, _, _ = build(tmp_path, [])
    bad = '"7","SN 1900A","","","SN"' + ',""' * 23
    path = tmp_path / "hist.csv"
    path.write_text(page(bad, ROW_6550), encoding="utf-8")

    names = [r.name for r in source.parse(path)]

    assert names == ["SN2015K"]


def test_parse_tolerates_a_missing_optional_column(tmp_path):
    # TNS renames or drops a non-key column: degrade to NULL, not to "mirror down".
    header = '"ID","Name","RA","DEC","Obj. Type"'
    row = '"1","SN 1885A","00:42:43.0","+41:16:04","SN I"'
    source, _, _ = build(tmp_path, [])
    path = tmp_path / "hist.csv"
    path.write_text(page(row, header=header), encoding="utf-8")

    (record,) = list(source.parse(path))

    assert record.name == "SN1885A"
    assert record.obj_type == "SN I"
    assert record.remarks is None


def test_parse_requires_the_key_columns(tmp_path):
    source, _, _ = build(tmp_path, [])
    path = tmp_path / "hist.csv"
    path.write_text('"Name","Obj. Type"\n"SN 1885A","SN I"\n', encoding="utf-8")

    with pytest.raises(ParseError, match="ID/Name/RA/DEC"):
        list(source.parse(path))


@pytest.mark.parametrize(
    ("raw", "hours", "expected"),
    [
        ("09:09:35.059", True, 137.39607917),
        ("00:00:00", True, 0.0),
        ("23:59:59.999", True, 359.99999583),
        ("+33:07:22.12", False, 33.12281111),
        ("-61:39:34.60", False, -61.65961111),
        ("-00:30:00", False, -0.5),  # the sign must survive a zero degree field
        ("137.396", True, 137.396),  # decimal degrees pass through unchanged
        ("", True, None),
        ("north", False, None),
        ("12:60:00", True, None),  # minutes out of range
    ],
)
def test_sexagesimal_conversion(raw, hours, expected):
    result = sexagesimal_to_degrees(raw, hours=hours)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("SN 2015bh", "SN2015bh"), ("SN2015bh", "SN2015bh"), ("  SN  1885A ", "SN1885A")],
)
def test_name_normalisation(raw, expected):
    assert normalise_name(raw) == expected
