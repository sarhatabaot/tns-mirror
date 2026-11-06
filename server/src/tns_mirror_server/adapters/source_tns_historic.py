"""The TNS historic-SNe source: the pre-2016 supernovae, paged off the search page.

TNS's staged ``tns_public_objects.csv`` deliberately excludes the CBAT-era SNe it
imported when it took over IAU naming on 2016-01-01. Its documentation points at
one route for them: ``search?isTNS_AT=no&format=csv``, paged 100 rows at a time.
About 6,600 objects, so roughly 66 requests — and a closed set, so this runs
rarely (weekly by default), never hourly.

Etiquette matters here more than for the staged files. TNS says it "highly
discourages" search-page downloads for large sets; what makes this one
defensible is that there is no other route, it is small, it is infrequent, and
it follows TNS's own published best practice for the rate limit: read
``x-rate-limit-remaining`` on every response and, when it reaches zero, sleep
``x-rate-limit-reset + 1`` seconds *before* the next request rather than waiting
to be told 429. When the headers are absent the configured download throttle is
used between pages instead.

Every page is streamed into one temporary file — the first page's header kept,
the others' dropped — and renamed into place only once the last page has been
read, so a run interrupted mid-way never leaves a partial list where the next
sync would read it as complete.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import requests

from ..config import Config
from ..errors import ParseError, SourceError
from ..ports.historic import HistoricRecord, register_historic_source
from .tns_auth import build_request, rejection_detail

log = logging.getLogger(__name__)

#: Hard ceiling on pages per run. The real list is ~66 pages; this only stops a
#: misbehaving upstream (a page that never comes back empty) from looping forever.
MAX_PAGES: Final = 500

#: How many consecutive 429s to wait out before giving up on the run.
MAX_RATE_LIMIT_RETRIES: Final = 3

#: The query TNS documents for the historic SNe. ``page`` is appended per request.
_QUERY: Final = {"isTNS_AT": "no", "format": "csv"}

# TNS's export header -> our column. Matched after normalisation (lowercased;
# spaces, dots, slashes, parentheses, underscores stripped) so "Obj. Type",
# "Reporting Group/s" and "Discovery Date (UT)" all resolve regardless of
# punctuation. An unknown header is ignored; a missing one leaves NULL.
_COLUMNS: Mapping[str, str] = {
    "objtype": "obj_type",
    "redshift": "redshift",
    "hostname": "host_name",
    "hostredshift": "host_redshift",
    "reportinggroups": "reporting_groups",
    "discoverydatasources": "discovery_data_sources",
    "classifyinggroups": "classifying_groups",
    "associatedgroups": "associated_groups",
    "discinternalname": "disc_internal_name",
    "discinstruments": "disc_instruments",
    "classinstruments": "class_instruments",
    "tnsat": "tns_at",
    "public": "public",
    "endpropperiod": "end_prop_period",
    "discoverymagflux": "discovery_mag_flux",
    "discoveryfilter": "discovery_filter",
    "discoverydateut": "discovery_date_ut",
    "sender": "sender",
    "remarks": "remarks",
    "unreal": "unreal",
    "discoverybibcode": "discovery_bibcode",
    "classificationbibcodes": "classification_bibcodes",
    "extcatalogs": "ext_catalogs",
    "autoclassification": "auto_classification",
}

_ALIASES: Mapping[str, frozenset[str]] = {
    "objid": frozenset({"id", "objid"}),
    "name": frozenset({"name", "objname"}),
    "ra": frozenset({"ra"}),
    "dec": frozenset({"dec", "decl", "declination"}),
}

_REQUIRED = ("objid", "name", "ra", "dec")

_SEXAGESIMAL = re.compile(
    r"^\s*(?P<sign>[+-])?\s*(?P<a>\d{1,3})\s*[:\s]\s*(?P<b>\d{1,2})\s*[:\s]\s*(?P<c>\d{1,2}(?:\.\d*)?)\s*$"
)


def _norm(cell: str) -> str:
    return re.sub(r"[\s._/()\-]", "", cell.strip().strip('"').lower())


def _clean(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _text_or_none(raw: str) -> str | None:
    return raw if raw else None


def _int_or_none(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def sexagesimal_to_degrees(raw: str, *, hours: bool) -> float | None:
    """``'09:09:35.059'`` -> degrees (RA, x15) or ``'+33:07:22.12'`` -> degrees (Dec).

    Decimal degrees are accepted too, in case TNS ever changes the export. This is
    a unit conversion, not geometry: the server still does no cone search.
    """
    if not raw:
        return None
    match = _SEXAGESIMAL.match(raw)
    if match is None:
        try:
            return float(raw)
        except ValueError:
            return None
    a, b, c = int(match["a"]), int(match["b"]), float(match["c"])
    if b >= 60 or c >= 60:
        return None
    value = a + b / 60 + c / 3600
    if hours:
        value *= 15.0
    if match["sign"] == "-":
        value = -value
    return value


def normalise_name(raw: str) -> str:
    """``'SN 2015bh'`` -> ``'SN2015bh'``, matching ``tns_objects.name``."""
    return "".join(raw.split())


@register_historic_source("tns")
class TnsHistoricSne:
    """Fetches and parses the historic SN list from the TNS search page."""

    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()

    # --- fetch --------------------------------------------------------------

    def fetch(self, *, reuse_existing: bool = False) -> Path:
        csv_path = self._config.historic_csv_path
        if reuse_existing and csv_path.exists():
            log.info("reusing existing %s (no download)", csv_path)
            return csv_path

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        method, headers, data = build_request(self._config.auth)
        page_size = self._config.download.historic_page_size
        base = self._config.download.historic_url

        tmp = csv_path.with_name(csv_path.name + ".tmp")
        pages = 0
        rows = 0
        pause = 0.0
        try:
            with tmp.open("w", encoding="utf-8", newline="") as out:
                writer = csv.writer(out, quoting=csv.QUOTE_ALL)
                for page in range(MAX_PAGES):
                    if page and pause:
                        log.debug("pausing %.0fs before page %d", pause, page)
                        time.sleep(pause)

                    params = {**_QUERY, "num_page": str(page_size), "page": str(page)}
                    response = self._request(method, base, headers, data, params)
                    pause = self._pause_after(response)

                    # Parsed as CSV rather than split on newlines: a Remarks
                    # cell may legitimately contain one.
                    if page == 0:
                        self._check_looks_like_csv(response)
                    records = list(csv.reader(io.StringIO(response.text)))
                    header, body = records[:1], records[1:]
                    if page == 0 and header:
                        writer.writerow(header[0])
                    if not body:
                        break  # TNS answers an out-of-range page with the header alone

                    writer.writerows(body)
                    pages += 1
                    rows += len(body)
                    log.info("historic page %d: %d rows (%d so far)", page, len(body), rows)

                    if len(body) < page_size:
                        break  # a short page is the last page; skip the empty request
                else:
                    raise SourceError(
                        f"historic export exceeded {MAX_PAGES} pages without ending; "
                        f"refusing to loop. Check download.historic_url."
                    )
            if rows == 0:
                raise SourceError(
                    "TNS returned no historic rows at all. Keeping the previous file."
                )
            tmp.replace(csv_path)  # atomic: a partial list never lands in place
        finally:
            tmp.unlink(missing_ok=True)

        log.info("historic export complete: %d rows over %d pages -> %s", rows, pages, csv_path)
        return csv_path

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: dict[str, str] | None,
        params: dict[str, str],
    ) -> requests.Response:
        """One page, waiting out up to MAX_RATE_LIMIT_RETRIES 429s."""
        timeout = self._config.download.timeout_seconds
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            response = self._session.request(
                method, url, headers=headers, data=data, params=params, timeout=timeout
            )
            if response.status_code != 429:
                self._raise_for_status(response)
                return response
            if attempt == MAX_RATE_LIMIT_RETRIES:
                break
            reset = _header_int(response, "x-rate-limit-reset") or 60
            log.warning(
                "TNS rate-limited the historic export (429); waiting %ds before retrying",
                reset + 1,
            )
            time.sleep(reset + 1)
        response.raise_for_status()  # a 429 the engine can recognise
        raise AssertionError("unreachable")  # pragma: no cover

    def _pause_after(self, response: requests.Response) -> float:
        """Seconds to wait before the next page — TNS's stated best practice.

        Zero remaining means the next request would be a 429; wait for the
        window to reset. Otherwise no pause is needed: the limit is what paces
        us. Without the headers at all (an unexpected proxy, say) fall back to
        the configured throttle so we never fire pages back-to-back blind.
        """
        remaining = _header_int(response, "x-rate-limit-remaining")
        if remaining is None:
            return self._config.download.throttle_seconds
        if remaining <= 0:
            return float((_header_int(response, "x-rate-limit-reset") or 60) + 1)
        return 0.0

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code in (401, 403):
            detail = rejection_detail(self._config.auth)
            raise SourceError(f"HTTP {response.status_code} from TNS. {detail}")
        response.raise_for_status()

    @staticmethod
    def _check_looks_like_csv(response: requests.Response) -> None:
        """A login page or an error page is HTML; refuse to store it as a catalogue."""
        first_line = response.text.lstrip()[:200].splitlines()[:1]
        head = first_line[0] if first_line else ""
        if head.startswith("<") or "," not in head:
            raise SourceError(
                f"TNS search export did not return CSV (first line {head[:60]!r}) "
                f"from {response.url}. Check the credential and download.historic_url."
            )

    # --- parse --------------------------------------------------------------

    def parse(self, source: Path) -> Iterator[HistoricRecord]:
        with open(source, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            index, extras = self._find_header(reader)
            for row in reader:
                record = self._row_to_record(row, index, extras)
                if record is not None:
                    yield record

    @staticmethod
    def _row_to_record(
        row: list[str], index: Mapping[str, int | None], extras: Mapping[str, int]
    ) -> HistoricRecord | None:
        """Convert one row, or ``None`` if it lacks a usable name or position."""
        ra_hms = _clean(row, index["ra"])
        dec_dms = _clean(row, index["dec"])
        ra = sexagesimal_to_degrees(ra_hms, hours=True)
        dec = sexagesimal_to_degrees(dec_dms, hours=False)
        if ra is None or dec is None:
            return None
        name = normalise_name(_clean(row, index["name"]))
        if not name:
            return None

        verbatim = {column: _text_or_none(_clean(row, i)) for column, i in extras.items()}
        return HistoricRecord(
            objid=_int_or_none(_clean(row, index["objid"])),
            name=name,
            ra=ra,
            dec=dec,
            ra_hms=ra_hms,
            dec_dms=dec_dms,
            **verbatim,
        )

    @staticmethod
    def _find_header(
        reader: Iterator[list[str]],
    ) -> tuple[dict[str, int | None], dict[str, int]]:
        """Locate the header; return the key columns and every verbatim column found."""
        for row in reader:
            columns = {_norm(cell): i for i, cell in enumerate(row)}
            found = {
                field: next((columns[a] for a in aliases if a in columns), None)
                for field, aliases in _ALIASES.items()
            }
            if all(found[required] is not None for required in _REQUIRED):
                extras = {
                    column: columns[header]
                    for header, column in _COLUMNS.items()
                    if header in columns
                }
                known = set(_COLUMNS) | {a for aliases in _ALIASES.values() for a in aliases}
                if unknown := sorted(set(columns) - known):
                    log.info("historic export has columns not stored: %s", ", ".join(unknown))
                return found, extras
        raise ParseError(
            "TNS historic CSV: could not locate a header row with ID/Name/RA/DEC columns"
        )


def _header_int(response: requests.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
