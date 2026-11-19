"""The HTTP transport, against a fake adapter: no network, no database.

The adapter is mounted on a real ``requests.Session``, so header handling,
redirects and proxy resolution all run through requests itself; only the socket
is replaced. Whether the HTTP answers equal the SQL answers is tested against
the real API and a real Postgres, in ``api/tests/test_transport_parity.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from tns_mirror_client import (
    AccessDenied,
    MirrorUnavailable,
    QueryLimitExceeded,
    SchemaVersionError,
    TnsMirror,
    TnsMirrorClientError,
    TnsObject,
    _http,
)
from tns_mirror_client import client as client_module

BASE = "http://mirror.example/tns-mirror/api/"
KEY = "k_9e04d1c2b3a4f5e6d7c8b9a0"

META = {
    "schema_version": 1,
    "rows": 7,
    "last_full_sync_at": "2026-11-18T03:00:00Z",
    "last_delta_sync_at": "2026-11-18T10:00:00.123456Z",
    "last_source_snapshot": "2026-11-18",
    "stale": False,
}


def row(objid: int = 2, name: str = "SN2026xyz", **overrides: Any) -> dict[str, Any]:
    """One ``ObjectOut`` as tns-mirror-api 1.1 sends it."""
    body = {
        "objid": objid,
        "name": name,
        "ra": 203.1005,
        "dec": 10,
        "type": "SN Ia",
        "redshift": 0.031,
        "discoverydate": "2026-01-02T03:04:05.678000+00:00",
        "discoverymag": 18,
        "internal_names": ["ZTF26aaa", "ATLAS26x"],
        "internal_names_raw": "ZTF26aaa,ATLAS26x",
        "reporting_group": "ZTF",
        "source_snapshot": "2026-11-18",
        "refreshed_at": "2026-11-18T10:00:00Z",
        "separation_arcsec": None,
    }
    body.update(overrides)
    return body


def cone(*rows: dict[str, Any], radius: float = 3.0, limit: int = 1000, **extra: Any):
    body = {
        "ra": 203.1,
        "dec": 10.2,
        "radius_arcsec": radius,
        "limit": limit,
        "count": len(rows),
        "truncated": False,
        "results": list(rows),
    }
    body.update(extra)
    return body


Handler = Callable[[requests.PreparedRequest], Any]


class FakeAdapter(BaseAdapter):
    """Answers from a handler; records what requests actually sent."""

    def __init__(self, handler: Handler) -> None:
        super().__init__()
        self.handler = handler
        self.sent: list[SimpleNamespace] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.sent.append(
            SimpleNamespace(request=request, timeout=timeout, verify=verify, proxies=proxies)
        )
        answer = self.handler(request)
        if isinstance(answer, Exception):
            raise answer
        status, body, headers = answer if len(answer) == 3 else (*answer, {})
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response.headers.update(headers)
        response.request = request
        response.url = request.url
        return response

    def close(self) -> None:
        pass


def routes(table: dict[str, Any]) -> Handler:
    """Route on path; /v1/meta answers META unless the table says otherwise."""

    def handle(request: requests.PreparedRequest):
        path = urlsplit(request.url).path.removeprefix("/tns-mirror/api/")
        if path in table:
            answer = table[path]
            return answer(request) if callable(answer) else answer
        if path == "v1/meta":
            return 200, META
        return 404, {"status_code": 404, "detail": "Not Found"}

    return handle


def mirror(handler: Handler, **kwargs: Any) -> tuple[TnsMirror, FakeAdapter]:
    session = requests.Session()
    adapter = FakeAdapter(handler)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    kwargs.setdefault("url", BASE)
    return TnsMirror(api_key=KEY, session=session, **kwargs), adapter


def query(sent: SimpleNamespace) -> dict[str, list[str]]:
    return parse_qs(urlsplit(sent.request.url).query)


# --- construction -----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"dsn": "postgresql://x", "url": BASE, "api_key": KEY},
        {"dsn": "postgresql://x", "connection": object()},
    ],
)
def test_exactly_one_transport_is_required(kwargs):
    with pytest.raises(ValueError, match="exactly one of dsn, connection or url"):
        TnsMirror(**kwargs)


def test_url_needs_a_key():
    with pytest.raises(ValueError, match="api_key is required"):
        TnsMirror(url=BASE)


def test_a_key_without_a_url_is_a_mistake():
    with pytest.raises(ValueError, match="only with url"):
        TnsMirror(dsn="postgresql://x", api_key=KEY)


def test_schema_and_table_belong_to_the_api_over_http():
    with pytest.raises(ValueError, match="TNS_SCHEMA"):
        TnsMirror(url=BASE, api_key=KEY, schema="other")


@pytest.mark.parametrize(
    "url",
    [
        "mirror.example/api",
        "ftp://mirror.example/api",
        f"http://user:{KEY}@mirror.example/api",
        f"http://mirror.example/api?api_key={KEY}",
        "http://mirror.example/api#x",
    ],
)
def test_bad_urls_are_refused_without_echoing_them(url):
    with pytest.raises(ValueError) as excinfo:
        TnsMirror(url=url, api_key=KEY)
    assert KEY not in str(excinfo.value)
    assert "mirror.example" not in str(excinfo.value)


@pytest.mark.parametrize("key", ["has space", "new\nline", "tab\t", "ключ"])
def test_keys_that_cannot_be_a_header_are_refused_without_echoing_them(key):
    with pytest.raises(ValueError) as excinfo:
        TnsMirror(url=BASE, api_key=key)
    assert key not in str(excinfo.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), (1.0,) * 3, (1.0, 0), "5", True])
def test_timeouts_must_be_positive(timeout):
    with pytest.raises(ValueError, match="timeout"):
        TnsMirror(url=BASE, api_key=KEY, timeout=timeout)


def test_the_default_timeout_is_one_number_in_two_places():
    assert client_module.DEFAULT_TIMEOUT == _http.DEFAULT_TIMEOUT


def test_construction_does_no_io():
    tns, adapter = mirror(routes({}))
    assert adapter.sent == []
    tns.close()


# --- the request ------------------------------------------------------------


def test_the_key_travels_in_the_header_and_nowhere_else():
    tns, adapter = mirror(routes({}))
    tns.meta()
    for sent in adapter.sent:
        assert sent.request.headers["X-API-Key"] == KEY
        assert KEY not in sent.request.url


def test_a_lent_session_never_carries_the_key_itself():
    tns, _ = mirror(routes({}))
    tns.meta()
    assert "X-API-Key" not in tns._http._session.headers


def test_every_request_has_a_timeout():
    tns, adapter = mirror(routes({}), timeout=(2.0, 7.0))
    tns.count()
    assert [s.timeout for s in adapter.sent] == [(2.0, 7.0), (2.0, 7.0)]


def test_certificates_are_verified_by_default():
    tns, adapter = mirror(routes({}), url="https://mirror.example/tns-mirror/api")
    tns.meta()
    assert all(s.verify is True for s in adapter.sent)


def test_the_base_url_may_omit_its_trailing_slash():
    tns, adapter = mirror(routes({}), url=BASE.rstrip("/"))
    tns.meta()
    assert adapter.sent[0].request.url == BASE + "v1/meta"


def test_redirects_are_not_followed():
    # A redirect may point at another host, and requests strips its own
    # Authorization header there but not X-API-Key.
    tns, adapter = mirror(
        routes({"v1/meta": (301, b"", {"Location": "http://elsewhere.example/"})})
    )
    with pytest.raises(MirrorUnavailable, match="redirected"):
        tns.meta()
    assert len(adapter.sent) == 1


# --- proxies ----------------------------------------------------------------


def owned(trust_env: bool) -> tuple[TnsMirror, FakeAdapter]:
    tns = TnsMirror(url=BASE, api_key=KEY, trust_env=trust_env)
    adapter = FakeAdapter(routes({}))
    tns._http._open().mount("http://", adapter)
    return tns, adapter


def test_environment_proxies_are_honoured_by_default(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    tns, adapter = owned(trust_env=True)
    tns.meta()
    assert adapter.sent[0].proxies.get("http") == "http://proxy.example:3128"


def test_no_proxy_keeps_the_mirror_off_the_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "mirror.example")
    tns, adapter = owned(trust_env=True)
    tns.meta()
    assert not adapter.sent[0].proxies.get("http")


def test_trust_env_false_ignores_the_proxy_variables(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    tns, adapter = owned(trust_env=False)
    tns.meta()
    assert not adapter.sent[0].proxies.get("http")


# --- the session's lifetime -------------------------------------------------


def test_close_drops_an_owned_session_and_the_next_call_reopens_it():
    tns = TnsMirror(url=BASE, api_key=KEY)
    first = tns._http._open()
    first.mount("http://", FakeAdapter(routes({})))
    tns.meta()
    tns.close()
    assert tns._http._session is None
    second = tns._http._open()
    assert second is not first


def test_a_lent_session_is_not_closed():
    session = requests.Session()
    closed = []
    session.close = lambda: closed.append(True)
    with TnsMirror(url=BASE, api_key=KEY, session=session):
        pass
    assert closed == []


def test_one_session_serves_every_call():
    tns, adapter = mirror(routes({"v1/objid/2": (200, row())}))
    tns.by_objid(2)
    tns.by_objid(2)
    assert len(adapter.sent) == 3  # one schema check, then two lookups


# --- the schema check -------------------------------------------------------


def test_the_schema_version_is_checked_once_on_first_use():
    tns, adapter = mirror(routes({"v1/objid/2": (200, row())}))
    tns.by_objid(2)
    tns.by_objid(2)
    paths = [urlsplit(s.request.url).path for s in adapter.sent]
    assert paths.count("/tns-mirror/api/v1/meta") == 1


def test_a_different_schema_version_is_refused():
    tns, _ = mirror(routes({"v1/meta": (200, {**META, "schema_version": 2})}))
    with pytest.raises(SchemaVersionError) as excinfo:
        tns.nearest(203.1, 10.2)
    assert excinfo.value.found == 2


def test_the_check_can_be_skipped():
    tns, adapter = mirror(routes({"v1/objid/2": (200, row())}), check_schema_version=False)
    tns.by_objid(2)
    assert len(adapter.sent) == 1


# --- answers ----------------------------------------------------------------


def test_meta_and_count():
    tns, _ = mirror(routes({}))
    meta = tns.meta()
    assert meta.schema_version == 1
    assert meta.last_full_sync_at == datetime(2026, 11, 18, 3, tzinfo=UTC)
    assert meta.last_delta_sync_at == datetime(2026, 11, 18, 10, 0, 0, 123456, tzinfo=UTC)
    assert meta.last_source_snapshot == date(2026, 11, 18)
    assert tns.count() == 7


def test_a_never_synced_mirror_has_null_times():
    body = {**META, "last_full_sync_at": None, "last_delta_sync_at": None}
    tns, _ = mirror(routes({"v1/meta": (200, {**body, "last_source_snapshot": None})}))
    assert tns.meta().last_sync_at is None


def test_an_object_comes_back_exactly_typed():
    tns, _ = mirror(routes({"v1/objid/2": (200, row())}))
    hit = tns.by_objid(2)
    assert hit == TnsObject(
        objid=2,
        name="SN2026xyz",
        ra=203.1005,
        dec=10.0,
        type="SN Ia",
        redshift=0.031,
        discoverydate=datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC),
        discoverymag=18.0,
        internal_names="ZTF26aaa,ATLAS26x",
        reporting_group="ZTF",
        source_snapshot=date(2026, 11, 18),
        refreshed_at=datetime(2026, 11, 18, 10, tzinfo=UTC),
    )
    assert isinstance(hit.dec, float)
    assert isinstance(hit.discoverymag, float)


def test_an_older_api_without_the_raw_names_still_answers():
    old = row(internal_names=["ZTF26aaa", "ATLAS26x"])
    del old["internal_names_raw"]
    tns, _ = mirror(routes({"v1/objid/2": (200, old)}))
    assert tns.by_objid(2).internal_names == "ZTF26aaa, ATLAS26x"


def test_nearest_asks_the_cone_for_one_result():
    hit = row(separation_arcsec=0.354)
    tns, adapter = mirror(routes({"v1/cone": (200, cone(hit, limit=1))}))
    found = tns.nearest(203.1004, 10.2, radius_arcsec=3.0)
    assert found.separation_arcsec == 0.354
    params = query(adapter.sent[-1])
    assert params == {
        "ra": ["203.1004"],
        "dec": ["10.2"],
        "radius_arcsec": ["3.0"],
        "limit": ["1"],
    }


def test_nearest_with_nothing_in_range_is_none():
    tns, _ = mirror(routes({"v1/cone": (200, cone(limit=1))}))
    assert tns.nearest(100.0, -40.0) is None


def test_search_keeps_the_servers_order():
    rows = [row(1, "AT2026abc", separation_arcsec=0.0), row(2, separation_arcsec=1.8)]
    tns, _ = mirror(routes({"v1/cone": (200, cone(*rows, radius=400))}))
    found = tns.search(203.1, 10.2, radius_arcsec=400)
    assert [o.objid for o in found] == [1, 2]


def test_search_without_a_limit_asks_for_everything():
    tns, adapter = mirror(routes({"v1/cone": (200, cone())}))
    tns.search(203.1, 10.2, 3.0)
    assert query(adapter.sent[-1])["limit"] == [str(2**31 - 1)]


def test_a_truncated_answer_is_refused_rather_than_returned_short():
    rows = [row(i, f"AT{i}") for i in range(2)]
    body = cone(*rows, radius=400, limit=2, truncated=True)
    tns, _ = mirror(routes({"v1/cone": (200, body)}))
    with pytest.raises(QueryLimitExceeded, match="narrow the radius"):
        tns.search(203.1, 10.2, 400)


def test_a_limit_the_server_met_is_complete_even_if_more_exist():
    rows = [row(i, f"AT{i}") for i in range(2)]
    body = cone(*rows, radius=400, limit=2, truncated=True)
    tns, _ = mirror(routes({"v1/cone": (200, body)}))
    assert len(tns.search(203.1, 10.2, 400, limit=2)) == 2


def test_a_clamped_radius_is_refused():
    tns, _ = mirror(routes({"v1/cone": (200, cone(radius=3600.0))}))
    with pytest.raises(QueryLimitExceeded, match=r'3600\.0"'):
        tns.search(0.0, 0.0, 7200.0)


def test_an_older_api_is_trusted_only_where_it_cannot_have_clamped():
    def old(*rows):
        body = cone(*rows)
        del body["truncated"], body["limit"]
        return 200, body

    empty, _ = mirror(routes({"v1/cone": old()}))
    assert empty.search(0.0, 0.0, 3.0) == []

    one, _ = mirror(routes({"v1/cone": old(row())}))
    assert one.nearest(0.0, 0.0) is not None  # limit 1, one row: complete

    some, _ = mirror(routes({"v1/cone": old(row())}))
    with pytest.raises(TnsMirrorClientError, match=r"predates tns-mirror-api 1\.1\.0"):
        some.search(0.0, 0.0, 3.0)


def test_negative_radius_and_bad_limit_fail_before_any_request():
    tns, adapter = mirror(routes({}))
    with pytest.raises(ValueError):
        tns.search(0.0, 0.0, -1.0)
    with pytest.raises(ValueError):
        tns.search(0.0, 0.0, 1.0, limit=0)
    assert adapter.sent == []


def test_by_name_quotes_the_name_into_the_path():
    tns, adapter = mirror(routes({"v1/objects/AT%202026a%2Fb": (200, row())}))
    tns.by_name("AT 2026a/b")
    assert adapter.sent[-1].request.path_url.endswith("/v1/objects/AT%202026a%2Fb")


def test_a_lookup_that_finds_nothing_is_none():
    missing = (404, {"status_code": 404, "detail": "no object named 'SN1987A'"})
    tns, _ = mirror(routes({"v1/objects/SN1987A": missing, "v1/objid/9": missing}))
    assert tns.by_name("SN1987A") is None
    assert tns.by_objid(9) is None


def test_an_empty_name_matches_nothing_without_asking():
    tns, adapter = mirror(routes({}), check_schema_version=False)
    assert tns.by_name("") is None
    assert adapter.sent == []


def test_a_404_that_is_not_a_lookup_miss_is_not_quietly_none():
    tns, _ = mirror(routes({"v1/objects/AT1": (404, b"<html>nginx</html>")}))
    with pytest.raises(MirrorUnavailable, match="404"):
        tns.by_name("AT1")


def test_by_names_batches_and_keys_by_name():
    names = [f"AT{i}" for i in range(120)]
    seen: list[list[str]] = []

    def objects(request):
        asked = parse_qs(urlsplit(request.url).query)["name"]
        seen.append(asked)
        # Answer in reverse, to prove the client orders by objid itself.
        found = [row(int(n[2:]), n) for n in asked if int(n[2:]) % 2 == 0]
        return 200, {"count": len(found), "results": found[::-1]}

    tns, _ = mirror(routes({"v1/objects": objects}))
    found = tns.by_names([*names, "AT0"])  # a duplicate is asked for once
    assert [len(chunk) for chunk in seen] == [50, 50, 20]
    assert list(found) == [f"AT{i}" for i in range(0, 120, 2)]
    assert found["AT4"].objid == 4


def test_by_names_of_nothing_asks_nothing():
    tns, adapter = mirror(routes({}))
    assert tns.by_names([]) == {}
    assert adapter.sent == []


def test_by_names_on_an_api_without_the_endpoint_says_so():
    tns, _ = mirror(routes({}))
    with pytest.raises(MirrorUnavailable, match="predates this method"):
        tns.by_names(["AT1"])


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (requests.ConnectTimeout("connect"), "did not answer within"),
        (requests.ReadTimeout("read"), "did not answer within"),
        (requests.exceptions.SSLError("cert"), "TLS verification failed"),
        (requests.ConnectionError("refused"), "could not reach"),
        (requests.exceptions.ChunkedEncodingError("cut"), "ChunkedEncodingError"),
    ],
)
def test_transport_failures_are_mirror_unavailable(raised, message):
    tns, _ = mirror(lambda request: raised)
    with pytest.raises(MirrorUnavailable, match=message) as excinfo:
        tns.meta()
    assert isinstance(excinfo.value.__cause__, requests.RequestException)


@pytest.mark.parametrize(
    ("status", "body", "headers", "error", "message"),
    [
        (500, {"status_code": 500, "detail": "boom"}, {}, MirrorUnavailable, "HTTP 500"),
        (502, b"<html>Bad Gateway</html>", {}, MirrorUnavailable, "Bad Gateway"),
        (503, {"detail": "stale"}, {}, MirrorUnavailable, "HTTP 503"),
        (429, {"detail": "slow down"}, {"Retry-After": "30"}, MirrorUnavailable, "30s"),
        (401, {"detail": "missing or unknown X-API-Key"}, {}, AccessDenied, "refused"),
        (403, b"Forbidden", {}, AccessDenied, "HTTP 403"),
        (418, {"detail": "teapot"}, {}, TnsMirrorClientError, "unexpected HTTP 418"),
        (200, b"<html>not json</html>", {}, MirrorUnavailable, "did not answer with JSON"),
    ],
)
def test_http_failures_map_to_the_librarys_errors(status, body, headers, error, message):
    tns, _ = mirror(routes({"v1/meta": (status, body, headers)}))
    with pytest.raises(error, match=message):
        tns.meta()


def test_every_error_is_a_client_error():
    for error in (AccessDenied, MirrorUnavailable, QueryLimitExceeded, SchemaVersionError):
        assert issubclass(error, TnsMirrorClientError)


def test_a_rejected_argument_is_a_value_error_with_the_servers_reason():
    detail = {"status_code": 400, "detail": "dec must be in [-90, 90], got 91.0"}
    tns, _ = mirror(routes({"v1/cone": (400, detail)}))
    with pytest.raises(ValueError, match=r"dec must be in \[-90, 90\]"):
        tns.nearest(10.0, 91.0)


def test_a_malformed_answer_is_a_client_error():
    tns, _ = mirror(routes({"v1/cone": (200, {"results": "no"})}))
    with pytest.raises(TnsMirrorClientError, match="unexpected shape"):
        tns.nearest(0.0, 0.0)


# --- the key stays secret ---------------------------------------------------


def test_the_key_is_not_in_any_repr():
    tns, _ = mirror(routes({}))
    assert KEY not in repr(tns)
    assert KEY not in repr(tns._http)
    assert repr(tns) == f"TnsMirror(url={BASE!r})"


def test_the_dsn_is_not_in_the_repr():
    assert "secret" not in repr(TnsMirror(dsn="postgresql://ro:secret@db/tns"))


@pytest.mark.parametrize("status", [401, 403, 500, 418])
def test_the_key_is_never_in_an_error_or_a_log_line(caplog, status):
    # A proxy that echoes the request back into its error body is the worst
    # case: the key would otherwise land in the exception message.
    echo = {"detail": f"bad key {KEY}", "headers": {"X-API-Key": KEY}}
    tns, _ = mirror(routes({"v1/meta": (status, echo)}))

    caplog.set_level(logging.DEBUG)
    with pytest.raises(TnsMirrorClientError) as excinfo:
        tns.meta()

    chain = []
    error: BaseException | None = excinfo.value
    while error is not None:
        chain.append(f"{error!s} {error!r}")
        error = error.__cause__ or error.__context__
    assert KEY not in " ".join(chain)
    assert all(KEY not in record.getMessage() for record in caplog.records)


def test_the_key_is_not_in_a_transport_error(caplog):
    caplog.set_level(logging.DEBUG)
    tns, _ = mirror(lambda request: requests.ConnectionError("refused"))
    with pytest.raises(MirrorUnavailable) as excinfo:
        tns.meta()
    assert KEY not in str(excinfo.value)
    assert KEY not in str(excinfo.value.__cause__)
    assert all(KEY not in record.getMessage() for record in caplog.records)
