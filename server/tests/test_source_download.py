"""Download happy path and failure modes.

Design §11 calls this out explicitly as the gap in the original implementation's
coverage: stream -> zip extract -> atomic swap was never tested end to end.
"""

from __future__ import annotations

import pytest
import requests

from conftest import SAMPLE_CSV, FakeResponse, FakeSession, make_config, zip_bytes
from tns_mirror_server.adapters.source_tns import TnsPublicObjects
from tns_mirror_server.errors import SourceError


def build(tmp_path, responses, **kwargs):
    session = FakeSession(responses)
    return TnsPublicObjects(make_config(tmp_path, **kwargs), session=session), session


def test_full_snapshot_streams_extracts_and_lands_atomically(tmp_path):
    source, session = build(tmp_path, FakeResponse(zip_bytes(SAMPLE_CSV)))

    csv_path = source.download()

    assert csv_path == tmp_path / "tns_public_objects.csv"
    assert csv_path.read_text(encoding="utf-8") == SAMPLE_CSV
    assert (tmp_path / "tns_public_objects.csv.zip").exists()
    # Nothing half-written left behind.
    assert list(tmp_path.glob("*.tmp")) == []
    assert session.calls[0]["url"].endswith("tns_public_objects.csv.zip")
    assert session.calls[0]["stream"] is True
    assert session.calls[0]["timeout"] == 5.0


def test_hourly_delta_rewrites_url_and_filenames(tmp_path):
    source, session = build(tmp_path, FakeResponse(zip_bytes(SAMPLE_CSV)))

    csv_path = source.download(hour=7)

    assert session.calls[0]["url"].endswith("tns_public_objects_07.csv.zip")
    assert csv_path == tmp_path / "tns_public_objects_07.csv"
    assert (tmp_path / "tns_public_objects_07.csv.zip").exists()


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_hour_must_be_a_ut_hour(tmp_path, hour):
    source, _ = build(tmp_path, [])
    with pytest.raises(ValueError, match=r"hour must be 0\.\.23"):
        source.download(hour=hour)


def test_a_truncated_transfer_never_lands_in_place(tmp_path):
    # The atomic rename is the whole point: a connection dropped mid-stream must
    # not leave a partial archive where the next run would read it as a snapshot.
    body = zip_bytes(SAMPLE_CSV)
    source, _ = build(tmp_path, FakeResponse(body, explode_after=1))

    with pytest.raises(OSError, match="connection reset"):
        source.download()

    assert not (tmp_path / "tns_public_objects.csv.zip").exists()
    assert not (tmp_path / "tns_public_objects.csv").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_archive_without_a_csv_member_is_an_error(tmp_path):
    source, _ = build(tmp_path, FakeResponse(zip_bytes("nope", member="readme.txt")))

    with pytest.raises(SourceError, match="no CSV member"):
        source.download()


def test_a_corrupt_archive_is_reported_clearly(tmp_path):
    source, _ = build(tmp_path, FakeResponse(b"this is not a zip file"))

    with pytest.raises(SourceError, match="not a valid zip"):
        source.download()


def test_http_errors_propagate_for_the_engine_to_classify(tmp_path):
    # The engine distinguishes 429 from everything else; it can only do that if
    # the source lets HTTPError through untouched.
    source, _ = build(tmp_path, FakeResponse(b"", status=429))

    with pytest.raises(requests.HTTPError):
        source.download()


def test_reuse_existing_skips_the_network(tmp_path):
    source, session = build(tmp_path, [])
    (tmp_path / "tns_public_objects.csv").write_text(SAMPLE_CSV, encoding="utf-8")

    csv_path = source.download(reuse_existing=True)

    assert csv_path.read_text(encoding="utf-8") == SAMPLE_CSV
    assert session.calls == []


def test_reuse_existing_still_downloads_when_nothing_is_cached(tmp_path):
    source, session = build(tmp_path, FakeResponse(zip_bytes(SAMPLE_CSV)))

    source.download(reuse_existing=True)

    assert len(session.calls) == 1


def test_workdir_is_created_on_demand(tmp_path):
    nested = tmp_path / "deep" / "workdir"
    session = FakeSession(FakeResponse(zip_bytes(SAMPLE_CSV)))
    source = TnsPublicObjects(make_config(nested), session=session)

    source.download()

    assert (nested / "tns_public_objects.csv").exists()
