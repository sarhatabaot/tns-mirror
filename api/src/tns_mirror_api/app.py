"""The read-only HTTP API.

A transport, not a reimplementation. Every query goes through
``tns-mirror-client``, so the cone-search geometry — the clamped haversine, the
exact RA half-width, the pole and 0/360-seam handling — exists once in the
project rather than once per consumer.

The service connects with the mirror's **read-only** role. That is the point of
it being a separate artifact: the sync server holds write credentials and is not
exposed, and an exploit here reaches a role that can `SELECT` two tables.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import psycopg
from litestar import Litestar, Request, get
from litestar.config.cors import CORSConfig
from litestar.datastructures import State
from litestar.exceptions import (
    HTTPException,
    NotFoundException,
    ServiceUnavailableException,
)
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.openapi import OpenAPIConfig
from litestar.openapi.spec import Server
from litestar.params import FromPath, QueryParameter
from psycopg_pool import ConnectionPool
from tns_mirror_client import SCHEMA_VERSION, SchemaVersionError, TnsMirror

from . import __version__
from .config import Config, load_config
from .schemas import ConeOut, MetaOut, ObjectOut

log = logging.getLogger("tns_mirror_api")

__all__ = ["create_app"]


def client_identity(request: Request[Any, Any, Any]) -> str:
    """Who to charge a request to.

    Behind a proxy every caller shares the proxy's socket address, so the limit
    would apply to the proxy rather than to anyone using it. ``X-Forwarded-For``
    fixes that — but only when something trustworthy sets it. Left on by
    default, any caller could spoof the header and mint a fresh bucket per
    request, so honouring it is opt-in.
    """
    config: Config = request.app.state.config
    if config.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None]:
    """Open the pool, and refuse to start against a mirror we cannot speak to."""
    config: Config = app.state.config

    pool = ConnectionPool(
        conninfo=config.dsn,
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
        open=False,
        # autocommit is not a preference here. Without it the SET below opens a
        # transaction and leaves the connection INTRANS, which the pool treats
        # as a broken connection and discards — every time, so the pool never
        # fills and the service never starts.
        kwargs={"autocommit": True},
        # Read-only at the session level, so a bug in this service cannot write
        # even if it were handed a privileged role by mistake.
        configure=lambda conn: conn.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
        ),
    )
    pool.open(wait=True, timeout=30)
    app.state.pool = pool

    # Checked once here rather than per request: a version mismatch means a
    # column may have been renamed underneath us, and answering anyway would be
    # worse than not starting.
    try:
        with pool.connection() as conn:
            found = TnsMirror(connection=conn, schema=config.schema, table=config.table).meta()
        if found.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(found=found.schema_version, expected=SCHEMA_VERSION)
        log.info(
            "connected: schema v%s, %s rows",
            found.schema_version,
            "unknown" if found.last_sync_at is None else "populated",
        )
    except Exception:
        pool.close()
        raise

    try:
        yield
    finally:
        pool.close()


def _mirror(state: State, config: Config, conn: psycopg.Connection) -> TnsMirror:
    # The version was verified at startup; re-checking on every request would
    # add a round trip to answer a question that cannot have changed.
    return TnsMirror(
        connection=conn,
        schema=config.schema,
        table=config.table,
        check_schema_version=False,
    )


@get("/healthz", summary="Liveness", sync_to_thread=False, exclude_from_auth=True)
def healthz() -> dict[str, str]:
    """Is the process up. Deliberately does not touch the database."""
    return {"status": "ok"}


@get("/readyz", summary="Readiness", sync_to_thread=True)
def readyz(state: State) -> dict[str, Any]:
    """Can we actually serve: database reachable, and the mirror not stale.

    A mirror that stopped syncing three weeks ago still answers every query
    confidently, which is the failure worth catching here.
    """
    config: Config = state.config
    try:
        with state.pool.connection() as conn:
            meta = _mirror(state, config, conn).meta()
    except Exception as exc:
        raise ServiceUnavailableException(detail=f"database unavailable: {exc}") from exc

    if not meta.is_fresh(config.max_age_hours):
        age = "never" if meta.age is None else str(meta.age)
        raise ServiceUnavailableException(
            detail=f"mirror is stale: last sync {age} ago (limit {config.max_age_hours}h)"
        )
    return {"status": "ok", "schema_version": meta.schema_version}


@get("/v1/meta", summary="Schema version and freshness", sync_to_thread=True)
def meta(state: State) -> MetaOut:
    """What a consumer should check before trusting a result."""
    config: Config = state.config
    with state.pool.connection() as conn:
        mirror = _mirror(state, config, conn)
        return MetaOut.of(mirror.meta(), mirror.count(), config.max_age_hours)


@get("/v1/objects/{name:str}", summary="Look up by IAU name", sync_to_thread=True)
def by_name(state: State, name: FromPath[str]) -> ObjectOut:
    config: Config = state.config
    with state.pool.connection() as conn:
        found = _mirror(state, config, conn).by_name(name)
    if found is None:
        raise NotFoundException(detail=f"no object named {name!r}")
    return ObjectOut.of(found)


@get("/v1/objid/{objid:int}", summary="Look up by TNS objid", sync_to_thread=True)
def by_objid(state: State, objid: FromPath[int]) -> ObjectOut:
    config: Config = state.config
    with state.pool.connection() as conn:
        found = _mirror(state, config, conn).by_objid(objid)
    if found is None:
        raise NotFoundException(detail=f"no object with objid {objid}")
    return ObjectOut.of(found)


def _check_position(ra: float, dec: float) -> None:
    if not 0.0 <= ra < 360.0:
        raise HTTPException(status_code=400, detail=f"ra must be in [0, 360), got {ra}")
    if not -90.0 <= dec <= 90.0:
        raise HTTPException(status_code=400, detail=f"dec must be in [-90, 90], got {dec}")


def _capped_radius(config: Config, radius_arcsec: float) -> float:
    if radius_arcsec <= 0:
        raise HTTPException(
            status_code=400, detail=f"radius_arcsec must be positive, got {radius_arcsec}"
        )
    # Clamped rather than rejected: a caller asking for too much gets the
    # largest answer we will serve, not an error they have to handle. Rate
    # limiting alone would not stop one enormous query.
    return min(radius_arcsec, config.max_radius_arcsec)


@get("/v1/nearest", summary="Nearest object within a radius", sync_to_thread=True)
def nearest(
    state: State,
    ra: Annotated[float, QueryParameter(description="Right ascension, degrees, J2000")],
    dec: Annotated[float, QueryParameter(description="Declination, degrees, J2000")],
    radius_arcsec: Annotated[
        float, QueryParameter(description="Search radius, arcseconds")
    ] = 3.0,
) -> ObjectOut:
    """The closest object within the radius. 404 when there is nothing there."""
    config: Config = state.config
    _check_position(ra, dec)
    radius = _capped_radius(config, radius_arcsec)

    with state.pool.connection() as conn:
        found = _mirror(state, config, conn).nearest(ra=ra, dec=dec, radius_arcsec=radius)
    if found is None:
        raise NotFoundException(detail=f'no object within {radius}" of ({ra}, {dec})')
    return ObjectOut.of(found)


@get("/v1/cone", summary="Every object within a radius", sync_to_thread=True)
def cone(
    state: State,
    ra: Annotated[float, QueryParameter(description="Right ascension, degrees, J2000")],
    dec: Annotated[float, QueryParameter(description="Declination, degrees, J2000")],
    radius_arcsec: Annotated[
        float, QueryParameter(description="Search radius, arcseconds")
    ] = 3.0,
    limit: Annotated[int, QueryParameter(description="Maximum results", ge=1)] = 100,
) -> ConeOut:
    """Everything within the radius, nearest first, each with its separation."""
    config: Config = state.config
    _check_position(ra, dec)
    radius = _capped_radius(config, radius_arcsec)
    capped_limit = min(limit, config.max_limit)

    with state.pool.connection() as conn:
        found = _mirror(state, config, conn).search(
            ra=ra, dec=dec, radius_arcsec=radius, limit=capped_limit
        )
    return ConeOut(
        ra=ra,
        dec=dec,
        radius_arcsec=radius,
        count=len(found),
        results=[ObjectOut.of(obj) for obj in found],
    )


def create_app(config: Config | None = None) -> Litestar:
    config = config or load_config()

    rate_limit = RateLimitConfig(
        rate_limit=("minute", config.rate_limit_per_minute),
        identifier_for_request=client_identity,
        # Liveness is what an orchestrator polls; throttling it would make the
        # service look unhealthy precisely when it is busiest.
        # Prefixed too, or a mounted deployment would throttle its own
        # liveness probe.
        exclude=[f"{config.root_path}/healthz", f"{config.root_path}/docs"],
    )

    cors = CORSConfig(allow_origins=list(config.cors_origins)) if config.cors_origins else None

    app = Litestar(
        route_handlers=[healthz, readyz, meta, by_name, by_objid, nearest, cone],
        lifespan=[lifespan],
        middleware=[rate_limit.middleware],
        cors_config=cors,
        openapi_config=OpenAPIConfig(
            title="tns-mirror",
            version=__version__,
            description=(
                "Read-only HTTP access to a mirror of the IAU Transient Name Server "
                "public objects catalogue. Not an official TNS service."
            ),
            path="/docs",
            # Without this, /docs offers a "try it" button pointing at the
            # origin root, which 404s for anyone reaching us through a prefix.
            servers=[Server(url=config.root_path or "/")],
        ),
        # Mounts every route under the prefix, so the app serves the paths the
        # proxy actually forwards.
        path=config.root_path or None,
        state=State({"config": config}),
    )
    return app
