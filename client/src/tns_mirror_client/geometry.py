"""Spherical geometry for cone search.

Pure functions, no database. This module exists on its own because the awkward
parts of a cone search are arithmetic, not SQL: the RA bound widens towards the
poles, wraps at the 0/360 seam, and stops meaning anything once the cone reaches
a pole. Getting those right once, here, is the reason this library exists rather
than every consumer writing the query again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["BoundingBox", "angular_separation_deg", "bounding_box"]

#: Padding added to every bound, in degrees (~3.6 microarcseconds).
#:
#: The box is a prefilter, so being a hair generous costs nothing and being a
#: hair tight loses real matches: an object sitting exactly on the search radius
#: lands on the boundary, where double-precision rounding decides membership by
#: coin flip. Far below any astrometric precision, far above the ~1e-12 degree
#: error of the arithmetic that produces the bound.
_EPSILON_DEG = 1e-9


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """An indexable prefilter for a cone.

    ``ra_ranges`` is empty when right ascension cannot usefully be constrained —
    the cone reaches a pole, or is wide enough to span every RA. It holds two
    ranges when the cone crosses the 0/360 seam, which is why it is a list rather
    than a pair of floats: a naive ``ra BETWEEN 359.5 AND 0.5`` matches nothing.
    """

    dec_min: float
    dec_max: float
    ra_ranges: list[tuple[float, float]]

    @property
    def constrains_ra(self) -> bool:
        return bool(self.ra_ranges)

    def contains(self, ra: float, dec: float) -> bool:
        """Whether a position falls inside the box. Used in tests, not in SQL."""
        if not self.dec_min <= dec <= self.dec_max:
            return False
        if not self.constrains_ra:
            return True
        return any(low <= ra <= high for low, high in self.ra_ranges)


def bounding_box(ra: float, dec: float, radius_deg: float) -> BoundingBox:
    """The smallest indexable box guaranteed to contain the cone.

    The box may be generous — it must never exclude a position inside the radius,
    which is the only property that matters, because the exact great-circle test
    runs afterwards and removes anything the box let through.
    """
    if radius_deg < 0:
        raise ValueError(f"radius must not be negative, got {radius_deg}")

    dec_min = max(-90.0, dec - radius_deg - _EPSILON_DEG)
    dec_max = min(90.0, dec + radius_deg + _EPSILON_DEG)

    cos_dec = math.cos(math.radians(dec))
    sin_radius = math.sin(math.radians(radius_deg))

    # Exact maximum RA extent of a cone: asin(sin r / cos δ). The small-angle
    # form r / cos δ under-covers as the cone widens or approaches a pole, and
    # under-covering is the one thing a prefilter may not do.
    #
    # When sin r >= cos δ the cone swallows a pole, so every RA is in range —
    # which is also the numerically graceful way to detect that, rather than
    # picking an arbitrary declination cut-off.
    if radius_deg >= 90.0 or cos_dec <= sin_radius:
        return BoundingBox(dec_min, dec_max, [])

    ra_halfwidth = math.degrees(math.asin(sin_radius / cos_dec)) + _EPSILON_DEG

    if ra_halfwidth >= 180.0:
        return BoundingBox(dec_min, dec_max, [])

    low = ra - ra_halfwidth
    high = ra + ra_halfwidth

    if low < 0.0:
        # Wraps below zero: [0, high] plus the tail up at [360 + low, 360].
        return BoundingBox(dec_min, dec_max, [(0.0, high), (low + 360.0, 360.0)])
    if high > 360.0:
        return BoundingBox(dec_min, dec_max, [(low, 360.0), (0.0, high - 360.0)])
    return BoundingBox(dec_min, dec_max, [(low, high)])


def angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation in degrees, via the haversine formula.

    Haversine rather than the law of cosines (``acos`` of a dot product) because
    the law of cosines is ill-conditioned exactly where a cross-match spends its
    time. At separations near zero its argument approaches 1, where ``acos``
    loses most of its significant digits: an object matched against itself comes
    back at ~3 milliarcseconds instead of 0. Haversine stays accurate all the way
    down.

    The argument is still clamped before ``asin`` — rounding can push it a hair
    past 1 for near-antipodal positions, which would be a domain error.

    This mirrors the SQL the client emits, so a test can assert the two agree.
    """
    lat1, lat2 = math.radians(dec1), math.radians(dec2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(ra2) - math.radians(ra1)

    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(haversine))))
