"""The TNS public-objects source: download and parse.

Design §5.0 and A.1. The download is streamed to a temporary file and renamed
into place, so a truncated transfer can never be mistaken for a catalogue. The
header scan is alias-based because TNS has shipped several column spellings over
the years and a rename upstream should degrade to "column absent", not "mirror
down".
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import zipfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import requests

from ..config import FULL_SNAPSHOT_FILENAME, Config
from ..errors import ParseError, SourceError
from ..ports.source import TnsRecord, register_source

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024

# Column-name aliases, matched after normalisation (lowercased; spaces,
# underscores and quotes stripped). ra/dec/name are required; the rest are
# best-effort and simply stay NULL when TNS does not publish them.
_ALIASES: Mapping[str, frozenset[str]] = {
    "ra": frozenset({"ra", "radeg", "radegree", "radegrees", "raj2000"}),
    "dec": frozenset({"dec", "declination", "decl", "decdeg", "decd", "decdegrees", "dej2000"}),
    "name": frozenset({"name", "objname", "iauname", "tnsname"}),
    "prefix": frozenset({"nameprefix", "prefix", "objprefix"}),
    "objid": frozenset({"objid", "id", "tnsid"}),
    "type": frozenset({"type", "objtype", "objecttype"}),
    "redshift": frozenset({"redshift", "z"}),
    "discoverydate": frozenset({"discoverydate", "discdate", "discoverydateut"}),
    "discoverymag": frozenset({"discoverymag", "discmag", "discoverymagnitude"}),
    "internal_names": frozenset({"internalnames", "internalname"}),
    "reporting_group": frozenset({"reportinggroup", "reportinggroups", "repgroup"}),
}

_REQUIRED = ("ra", "dec", "name")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _norm(cell: str) -> str:
    return cell.strip().strip('"').lower().replace(" ", "").replace("_", "")


def _clean(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip().strip('"')


def _float_or_none(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int_or_none(raw: str) -> int | None:
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _datetime_or_none(raw: str) -> datetime | None:
    """Parse a TNS discovery date. TNS publishes naive UTC; we attach it."""
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    return None


@register_source("tns")
class TnsPublicObjects:
    """Fetches and parses ``tns_public_objects.csv.zip`` and its hourly deltas."""

    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()
        self.download_throttle_seconds = config.download.throttle_seconds

    # --- authentication (§5.0) ---------------------------------------------

    def _tns_marker(self) -> str:
        """The ``tns_marker`` User-Agent TNS uses to attribute a request."""
        auth = self._config.auth
        if auth.mode == "marker":
            return auth.user_agent
        payload = json.dumps(
            {"tns_id": auth.bot_id, "type": "bot", "name": auth.bot_name},
            separators=(", ", ": "),
        )
        return f"tns_marker{payload}"

    def _build_request(self) -> tuple[str, dict[str, str], dict[str, str] | None]:
        """Return ``(method, headers, form_data)`` for the configured auth mode.

        Re-validates rather than trusting that config was checked: invariant 4
        says the *download* refuses without a credential, so the guard belongs
        here too, not only at startup.
        """
        auth = self._config.auth
        auth.validate()

        headers = {"User-Agent": self._tns_marker()}
        if auth.mode == "marker":
            return "GET", headers, None
        # Bot: TNS expects the api_key as form data on a POST, with the marker
        # identifying which bot is asking. Different verb, not just a header.
        return "POST", headers, {"api_key": auth.api_key}

    # --- download -----------------------------------------------------------

    def _paths_for(self, hour: int | None) -> tuple[str, Path, Path]:
        config = self._config
        if hour is None:
            return config.download.url, config.zip_path, config.csv_path

        if not 0 <= hour <= 23:
            raise ValueError(f"hour must be 0..23, got {hour}")
        delta_zip = f"tns_public_objects_{hour:02d}.csv.zip"
        url = config.download.url.replace(FULL_SNAPSHOT_FILENAME, delta_zip)
        return (
            url,
            config.zip_path.with_name(delta_zip),
            config.csv_path.with_name(f"tns_public_objects_{hour:02d}.csv"),
        )

    def download(self, hour: int | None = None, *, reuse_existing: bool = False) -> Path:
        url, zip_path, csv_path = self._paths_for(hour)

        if reuse_existing and csv_path.exists():
            log.info("reusing existing %s (no download)", csv_path)
            return csv_path

        zip_path.parent.mkdir(parents=True, exist_ok=True)
        method, headers, data = self._build_request()

        log.info("downloading %s", url)
        tmp_zip = zip_path.with_name(zip_path.name + ".tmp")
        try:
            with self._session.request(
                method,
                url,
                headers=headers,
                data=data,
                stream=True,
                timeout=self._config.download.timeout_seconds,
            ) as response:
                self._raise_for_status(response)
                with tmp_zip.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=_CHUNK):
                        if chunk:
                            out.write(chunk)
            # Atomic: a partial download never lands at the real path.
            tmp_zip.replace(zip_path)
        finally:
            tmp_zip.unlink(missing_ok=True)

        self._extract(zip_path, csv_path)
        return csv_path

    def _raise_for_status(self, response: requests.Response) -> None:
        """Turn TNS's rejections into something an operator can act on.

        A rejected credential is the commonest failure by far — a marker for an
        account that was never registered, a mistyped api_key, a bot id that
        does not match the key. Left alone, ``raise_for_status`` surfaces that
        as an unhandled traceback ending in "401 Client Error", which says
        nothing about what to change.

        429 is deliberately *not* translated: the engine distinguishes it from
        other failures to stop a catch-up early, and it can only do that if the
        original ``HTTPError`` reaches it.
        """
        if response.status_code in (401, 403):
            mode = self._config.auth.mode
            if mode == "bot":
                detail = (
                    "TNS rejected the bot credentials. Check TNS_API_KEY, "
                    "TNS_BOT_ID and TNS_BOT_NAME against Bot Management in your "
                    "TNS profile — the id and name must belong to the same bot "
                    "as the key."
                )
            else:
                detail = (
                    "TNS rejected the marker. Check TNS_USER_AGENT is the "
                    "tns_marker for a registered account, copied whole, "
                    "including the surrounding braces."
                )
            raise SourceError(f"HTTP {response.status_code} from TNS. {detail}")

        if response.status_code == 404:
            raise SourceError(
                f"HTTP 404 from TNS for {response.url}. The hourly delta for an "
                f"hour TNS has not staged yet returns 404; if the full snapshot "
                f"404s, check TNS_URL."
            )

        response.raise_for_status()

    @staticmethod
    def _extract(zip_path: Path, csv_path: Path) -> None:
        tmp_csv = csv_path.with_name(csv_path.name + ".tmp")
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
                if not members:
                    raise SourceError(f"no CSV member in TNS archive: {zip_path}")
                with archive.open(members[0], "r") as src, tmp_csv.open("wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
            tmp_csv.replace(csv_path)
        except zipfile.BadZipFile as exc:
            raise SourceError(f"TNS archive is not a valid zip: {zip_path} ({exc})") from exc
        finally:
            tmp_csv.unlink(missing_ok=True)

    # --- parse --------------------------------------------------------------

    def parse(self, source: Path) -> Iterator[TnsRecord]:
        with open(source, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            index = self._find_header(reader)
            for row in reader:
                record = self._row_to_record(row, index)
                if record is not None:
                    yield record

    @staticmethod
    def _row_to_record(row: list[str], index: Mapping[str, int | None]) -> TnsRecord | None:
        """Convert one CSV row, or ``None`` if it is unusable.

        A row is dropped only when it lacks coordinates or a name — TNS ships the
        occasional malformed line and one bad row must not fail a whole snapshot.
        """
        ra = _float_or_none(_clean(row, index["ra"]))
        dec = _float_or_none(_clean(row, index["dec"]))
        if ra is None or dec is None:
            return None

        name = _clean(row, index["name"])
        if not name:
            return None
        prefix = _clean(row, index["prefix"])

        return TnsRecord(
            objid=_int_or_none(_clean(row, index["objid"])),
            name=f"{prefix}{name}".strip(),
            ra=ra,
            dec=dec,
            type=_clean(row, index["type"]) or None,
            redshift=_float_or_none(_clean(row, index["redshift"])),
            discoverydate=_datetime_or_none(_clean(row, index["discoverydate"])),
            discoverymag=_float_or_none(_clean(row, index["discoverymag"])),
            internal_names=_clean(row, index["internal_names"]) or None,
            reporting_group=_clean(row, index["reporting_group"]) or None,
        )

    @staticmethod
    def _find_header(reader: Iterator[list[str]]) -> dict[str, int | None]:
        """Scan past any preamble to the first row naming ra, dec and name."""
        for row in reader:
            columns = {_norm(cell): i for i, cell in enumerate(row)}
            found = {
                field: next((columns[a] for a in aliases if a in columns), None)
                for field, aliases in _ALIASES.items()
            }
            if all(found[required] is not None for required in _REQUIRED):
                return found
        raise ParseError("TNS CSV: could not locate a header row with ra/dec/name columns")
