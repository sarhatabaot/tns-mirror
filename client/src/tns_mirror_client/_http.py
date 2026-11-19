"""The HTTP transport: the same queries, answered by ``tns-mirror-api``.

The API runs this library's SQL path on the server, so nothing here computes a
cone, a separation or an ordering. This module only moves the question over
HTTP, turns the answer back into the same dataclasses, and refuses to return a
partial answer where the server capped a query.

``requests`` is imported here and nowhere else, so the SQL path never needs it.
It is installed by the ``http`` extra: ``pip install 'tns-mirror-client[http]'``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit

import requests

from .errors import AccessDenied, MirrorUnavailable, QueryLimitExceeded, TnsMirrorClientError
from .models import MirrorMeta, TnsObject

__all__ = ["DEFAULT_TIMEOUT", "HttpMirror", "Timeout"]

#: Seconds to connect, and seconds to wait for the response. A cross-match on an
#: ingest path must not stall on an outage, so there is always a timeout.
DEFAULT_TIMEOUT: Final = 5.0

#: Seconds for both phases, or ``(connect, read)``.
Timeout = float | tuple[float, float]

_KEY_HEADER: Final = "X-API-Key"

#: Sent as ``limit`` when the caller set none: "as many as you will serve". The
#: server clamps it to its own ceiling and reports ``truncated`` when it did.
_ALL: Final = 2**31 - 1

#: Names per ``/v1/objects`` request. Keeps the URL well under any proxy's
#: limit, and under the API's smallest sensible ``TNS_API_MAX_LIMIT``.
_NAMES_PER_REQUEST: Final = 50

#: The prefix of the API's own 404 detail for a lookup that found nothing. Any
#: other 404 means the URL is not the endpoint we meant to reach.
_NOT_FOUND_DETAIL: Final = "no object"


def _check_url(url: str) -> str:
    """The base URL with exactly one trailing slash, or a ValueError.

    The URL itself is never echoed: someone may have pasted a key into it.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute http:// or https:// URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not carry credentials; pass the key as api_key")
    if parts.query or parts.fragment:
        raise ValueError("url must not have a query string or a fragment")
    return url.rstrip("/") + "/"


def _check_key(api_key: str | None) -> str:
    if not api_key:
        raise ValueError("api_key is required with url")
    # Checked here so that requests never rejects the header itself: its error
    # message quotes the offending value, which would put the key in a log.
    if not all(33 <= ord(char) <= 126 for char in api_key):
        raise ValueError("api_key must be printable ASCII with no whitespace")
    return api_key


def _check_timeout(timeout: Timeout) -> Timeout:
    values = timeout if isinstance(timeout, tuple) else (timeout,)
    if len(values) not in (1, 2) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        for v in values
    ):
        raise ValueError("timeout must be a positive number of seconds, or (connect, read)")
    return timeout


class HttpMirror:
    """One session to one API. Use from one thread at a time."""

    def __init__(
        self,
        url: str,
        api_key: str | None,
        *,
        timeout: Timeout,
        trust_env: bool,
        session: requests.Session | None,
    ) -> None:
        self.url = _check_url(url)
        self._key = _check_key(api_key)
        self._timeout = _check_timeout(timeout)
        self._trust_env = trust_env
        self._owns_session = session is None
        self._session: requests.Session | None = session

    def _open(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            # False ignores HTTP_PROXY, HTTPS_PROXY and NO_PROXY (and .netrc
            # and REQUESTS_CA_BUNDLE). True, the default, honours them.
            session.trust_env = self._trust_env
            self._session = session
        return self._session

    def __repr__(self) -> str:
        return f"HttpMirror(url={self.url!r})"

    def close(self) -> None:
        """Close the session, if this transport opened it. Reopens on next use."""
        if self._session is not None and self._owns_session:
            self._session.close()
            self._session = None

    # --- the wire ------------------------------------------------------------

    def _redact(self, text: str) -> str:
        # Belt and braces: our server never echoes the key, but a proxy in front
        # of it might, and its body ends up in an exception message.
        return text.replace(self._key, "<redacted>")

    def _get(self, path: str, params: dict[str, Any] | None = None, *, lookup: bool = False):
        """GET a path under the base URL and return its JSON.

        Returns ``None`` only for a lookup the API answered "no such object".
        Everything else that is not a 200 becomes one of this library's errors.
        """
        where = f"{self.url}{path}"
        try:
            response = self._open().get(
                where,
                params=params,
                # Per request rather than on the session, so a session the
                # caller lent us never carries the key to anything else.
                headers={_KEY_HEADER: self._key, "Accept": "application/json"},
                timeout=self._timeout,
                # A redirect may point at another host. requests drops its own
                # Authorization header there, but not ours, so never follow one.
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise MirrorUnavailable(
                f"the mirror API at {self.url} did not answer within {self._timeout}s"
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise MirrorUnavailable(
                f"TLS verification failed for the mirror API at {self.url}"
            ) from exc
        except requests.ConnectionError as exc:
            raise MirrorUnavailable(f"could not reach the mirror API at {self.url}") from exc
        except requests.RequestException as exc:
            raise MirrorUnavailable(
                f"request to the mirror API at {self.url} failed: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise MirrorUnavailable(
                    f"{where} did not answer with JSON; is url the tns-mirror API?"
                ) from exc

        detail = self._redact(_detail(response))
        if status == 404 and lookup and detail.startswith(_NOT_FOUND_DETAIL):
            return None
        if status in (401, 403):
            raise AccessDenied(
                f"the mirror API at {self.url} refused the API key (HTTP {status})"
            )
        if status == 400:
            # The caller's argument, as on the SQL path (e.g. dec beyond ±90).
            raise ValueError(detail or "the mirror API rejected the query (HTTP 400)")
        if status == 404:
            raise MirrorUnavailable(
                f"{where} does not exist (HTTP 404). Either url does not point at a "
                f"tns-mirror API, or the API predates this method; upgrade it."
            )
        if status == 429:
            retry = response.headers.get("Retry-After")
            hint = f"; retry after {retry}s" if retry else ""
            raise MirrorUnavailable(f"the mirror API at {self.url} is rate limiting{hint}")
        if 300 <= status < 400:
            raise MirrorUnavailable(
                f"the mirror API at {self.url} redirected (HTTP {status}) to "
                f"{response.headers.get('Location', '?')}; set url to the final address"
            )
        if status >= 500:
            raise MirrorUnavailable(
                f"the mirror API at {self.url} failed (HTTP {status}): {detail}"
            )
        raise TnsMirrorClientError(f"unexpected HTTP {status} from {where}: {detail}")

    # --- queries -------------------------------------------------------------

    def meta(self) -> tuple[MirrorMeta, int]:
        """The mirror's metadata, and its row count, which the API sends with it."""
        body = self._get("v1/meta")
        try:
            meta = MirrorMeta(
                schema_version=int(body["schema_version"]),
                last_full_sync_at=_datetime(body["last_full_sync_at"]),
                last_delta_sync_at=_datetime(body["last_delta_sync_at"]),
                last_source_snapshot=_date(body["last_source_snapshot"]),
            )
            return meta, int(body["rows"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _malformed("v1/meta") from exc

    def search(
        self, ra: float, dec: float, radius_arcsec: float, limit: int | None
    ) -> list[TnsObject]:
        wanted = _ALL if limit is None else limit
        body = self._get(
            "v1/cone",
            {"ra": ra, "dec": dec, "radius_arcsec": radius_arcsec, "limit": wanted},
        )
        try:
            served_radius = float(body["radius_arcsec"])
            results = [_object(row) for row in body["results"]]
            truncated = body.get("truncated")
        except (KeyError, TypeError, ValueError) as exc:
            raise _malformed("v1/cone") from exc

        if served_radius < radius_arcsec:
            raise QueryLimitExceeded(
                f'the mirror API serves cones up to {served_radius}" and '
                f'{radius_arcsec}" was asked for; narrow the radius'
            )

        if len(results) == wanted:
            return results  # exactly what was asked for, whatever lies beyond
        if truncated is None:
            # An API older than 1.1.0 does not say whether it clamped the limit.
            # An empty answer cannot have been clamped; anything else might.
            if not results:
                return results
            raise TnsMirrorClientError(
                f"the mirror API at {self.url} predates tns-mirror-api 1.1.0 and "
                f"cannot say whether this result is complete; upgrade it, or pass "
                f"a limit no larger than the number of results you need"
            )
        if truncated:
            raise QueryLimitExceeded(
                f'more than {len(results)} objects lie within {radius_arcsec}", and '
                f"{len(results)} is the most the mirror API serves in one query; "
                f"narrow the radius or pass a smaller limit"
            )
        return results

    def by_name(self, name: str) -> TnsObject | None:
        if not name:
            # The path would collapse to the collection; no TNS name is empty.
            return None
        body = self._get(f"v1/objects/{quote(name, safe='')}", lookup=True)
        return None if body is None else _parsed(body, "v1/objects/{name}")

    def by_objid(self, objid: int) -> TnsObject | None:
        body = self._get(f"v1/objid/{int(objid)}", lookup=True)
        return None if body is None else _parsed(body, "v1/objid/{objid}")

    def by_names(self, names: Sequence[str]) -> dict[str, TnsObject]:
        unique = list(dict.fromkeys(names))
        found: list[TnsObject] = []
        for start in range(0, len(unique), _NAMES_PER_REQUEST):
            chunk = unique[start : start + _NAMES_PER_REQUEST]
            body = self._get("v1/objects", {"name": chunk})
            try:
                found.extend(_object(row) for row in body["results"])
            except (KeyError, TypeError, ValueError) as exc:
                raise _malformed("v1/objects") from exc

        # The SQL path builds its dict from rows ordered by objid; so does this,
        # across every chunk, so a name that collides resolves the same way.
        found.sort(key=lambda obj: obj.objid)
        return {obj.name: obj for obj in found}


# --- parsing ----------------------------------------------------------------


def _detail(response: requests.Response) -> str:
    """The API's error detail, or the start of whatever else came back."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200].strip()
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return str(body)[:200]


def _malformed(path: str) -> TnsMirrorClientError:
    return TnsMirrorClientError(f"the mirror API sent an unexpected shape for {path}")


def _parsed(row: Any, path: str) -> TnsObject:
    try:
        return _object(row)
    except (KeyError, TypeError, ValueError) as exc:
        raise _malformed(path) from exc


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _float(value: float | None) -> float | None:
    # JSON may carry 10 for 10.0; the model says float.
    return None if value is None else float(value)


def _object(row: dict[str, Any]) -> TnsObject:
    """An ``ObjectOut`` back into the exact row the SQL path would return."""
    if "internal_names_raw" in row:
        internal_names = row["internal_names_raw"]
    else:
        # An API older than 1.1.0 sends only the split list. Joining it back is
        # as close as it allows; the spacing TNS used is gone.
        listed = row["internal_names"]
        internal_names = ", ".join(listed) if listed else None

    refreshed_at = _datetime(row["refreshed_at"])
    source_snapshot = _date(row["source_snapshot"])
    if refreshed_at is None or source_snapshot is None:
        raise ValueError("refreshed_at and source_snapshot are never null")

    return TnsObject(
        objid=int(row["objid"]),
        name=str(row["name"]),
        ra=float(row["ra"]),
        dec=float(row["dec"]),
        type=row["type"],
        redshift=_float(row["redshift"]),
        discoverydate=_datetime(row["discoverydate"]),
        discoverymag=_float(row["discoverymag"]),
        internal_names=internal_names,
        reporting_group=row["reporting_group"],
        source_snapshot=source_snapshot,
        refreshed_at=refreshed_at,
        separation_arcsec=_float(row.get("separation_arcsec")),
    )
