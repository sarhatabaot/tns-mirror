"""Invariant 4: authenticated to TNS, or no download at all.

Both credential paths are covered, including the fact that they differ in HTTP
verb — the bot path submits its ``api_key`` as form data on a POST.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeResponse, FakeSession, make_config, zip_bytes
from tns_mirror_server.adapters.source_tns import TnsPublicObjects
from tns_mirror_server.errors import AuthNotConfigured, ConfigError

MARKER = 'tns_marker{"tns_id":"1234","type":"user","name":"tester"}'


def test_marker_mode_sends_the_user_agent_on_a_get(tmp_path):
    session = FakeSession(FakeResponse(zip_bytes("objid,name,ra,dec\n1,AT1,1.0,2.0\n")))
    source = TnsPublicObjects(make_config(tmp_path, user_agent=MARKER), session=session)

    source.download()

    (call,) = session.calls
    assert call["method"] == "GET"
    assert call["headers"]["User-Agent"] == MARKER
    assert call["data"] is None  # nothing to submit


def test_bot_mode_posts_the_api_key_and_identifies_the_bot(tmp_path):
    session = FakeSession(FakeResponse(zip_bytes("objid,name,ra,dec\n1,AT1,1.0,2.0\n")))
    config = make_config(
        tmp_path, mode="bot", user_agent="", api_key="s3cret", bot_id="99", bot_name="mirrorbot"
    )
    source = TnsPublicObjects(config, session=session)

    source.download()

    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["data"] == {"api_key": "s3cret"}

    marker = call["headers"]["User-Agent"]
    assert marker.startswith("tns_marker")
    payload = json.loads(marker.removeprefix("tns_marker"))
    assert payload == {"tns_id": "99", "type": "bot", "name": "mirrorbot"}


def test_marker_mode_refuses_with_an_empty_user_agent(tmp_path):
    session = FakeSession([])
    source = TnsPublicObjects(make_config(tmp_path, user_agent="   "), session=session)

    with pytest.raises(AuthNotConfigured, match="never downloads anonymously"):
        source.download()

    assert session.calls == []  # no anonymous request was attempted


@pytest.mark.parametrize("missing", ["api_key", "bot_id", "bot_name"])
def test_bot_mode_refuses_when_any_credential_is_missing(tmp_path, missing):
    fields = {"api_key": "k", "bot_id": "1", "bot_name": "b"} | {missing: ""}
    session = FakeSession([])
    source = TnsPublicObjects(
        make_config(tmp_path, mode="bot", user_agent="", **fields), session=session
    )

    with pytest.raises(AuthNotConfigured, match=missing):
        source.download()

    assert session.calls == []


def test_unknown_auth_mode_is_rejected(tmp_path):
    source = TnsPublicObjects(make_config(tmp_path, mode="anonymous"), session=FakeSession([]))

    with pytest.raises(ConfigError, match="must be 'marker' or 'bot'"):
        source.download()
