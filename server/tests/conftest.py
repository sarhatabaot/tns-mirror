"""Shared test scaffolding.

No test on the unit path touches the network or a database: the ``Source`` port
is exercised against a fake ``requests.Session`` and the engine against the
in-memory store.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from tns_mirror_server.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    DownloadConfig,
)

TNS_URL = "https://www.wis-tns.org/system/files/tns_public_objects/tns_public_objects.csv.zip"

# A realistic TNS export: a preamble line, then the header, then rows.
SAMPLE_CSV = """\
"TNS Public Objects","2026-07-26 00:00:00"
"objid","name_prefix","name","ra","declination","type","redshift","discoverydate","discoverymag","internal_names","reporting_group"
"1001","AT","2026abc","203.1000","10.2000","","","2026-07-01 03:22:11.000","19.4","ZTF26aaa","ZTF"
"1002","SN","2026xyz","10.5","-45.25","SN Ia","0.031","2026-06-15 22:10:00.000","18.1","ATLAS26x,GOTO26y","ATLAS"
"""


def make_config(
    tmp_path: Path,
    *,
    mode: str = "marker",
    user_agent: str = 'tns_marker{"tns_id":"1234","type":"user","name":"tester"}',
    api_key: str = "",
    bot_id: str = "",
    bot_name: str = "",
    throttle: float = 0.0,
    table: str = "tns_objects",
    schema: str = "public",
    dsn: str = "",
) -> Config:
    return Config(
        auth=AuthConfig(
            mode=mode,
            user_agent=user_agent,
            api_key=api_key,
            bot_id=bot_id,
            bot_name=bot_name,
        ),
        download=DownloadConfig(
            url=TNS_URL, timeout_seconds=5.0, throttle_seconds=throttle, workdir=tmp_path
        ),
        database=DatabaseConfig(dsn=dsn, schema=schema, table=table),
    )


def zip_bytes(csv_text: str, *, member: str = "tns_public_objects.csv") -> bytes:
    """A zip archive containing ``csv_text``, as TNS serves it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, csv_text)
    return buffer.getvalue()


class FakeResponse:
    """A streamed response.

    The body is always split into :attr:`parts` chunks regardless of the caller's
    ``chunk_size``, so a mid-stream failure can be simulated even though the test
    payloads are far smaller than the real 1 MiB read size.
    """

    def __init__(
        self,
        body: bytes,
        status: int = 200,
        *,
        explode_after: int | None = None,
        parts: int = 4,
    ):
        self.body = body
        self.status_code = status
        self.parts = max(1, parts)
        self._explode_after = explode_after

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size: int = 1):
        size = max(1, -(-len(self.body) // self.parts))  # ceil division
        for emitted, start in enumerate(range(0, len(self.body), size)):
            if self._explode_after is not None and emitted >= self._explode_after:
                raise OSError("connection reset mid-stream")
            yield self.body[start : start + size]


class FakeSession:
    """Records every request and replays queued responses."""

    def __init__(self, responses: list[FakeResponse] | FakeResponse):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers=None, data=None, stream=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "data": data,
                "stream": stream,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


@pytest.fixture
def sample_zip() -> bytes:
    return zip_bytes(SAMPLE_CSV)
