"""Cone geometry. Pure functions, no database.

The bounding box has one job: never exclude a row that is genuinely inside the
radius. Being generous is fine — the great-circle test runs afterwards — but a
box that clips the cone silently loses cross-matches, which is the kind of bug
that shows up months later as "TNS doesn't have that object".
"""

from __future__ import annotations

import math

import pytest

from tns_mirror_client.geometry import angular_separation_deg, bounding_box

ARCSEC = 1.0 / 3600.0


def test_a_small_cone_makes_a_small_box():
    box = bounding_box(ra=203.1, dec=10.2, radius_deg=3 * ARCSEC)

    assert box.dec_min == pytest.approx(10.2 - 3 * ARCSEC)
    assert box.dec_max == pytest.approx(10.2 + 3 * ARCSEC)
    assert box.constrains_ra
    assert len(box.ra_ranges) == 1


def test_the_ra_bound_widens_towards_the_poles():
    # At dec 60 the sky is half as wide in RA, so the box must be about twice as
    # wide to cover the same angular radius.
    equator = bounding_box(ra=100.0, dec=0.0, radius_deg=1.0)
    mid = bounding_box(ra=100.0, dec=60.0, radius_deg=1.0)

    equator_width = equator.ra_ranges[0][1] - equator.ra_ranges[0][0]
    mid_width = mid.ra_ranges[0][1] - mid.ra_ranges[0][0]

    assert mid_width > equator_width * 1.9


def test_the_ra_bound_uses_the_exact_formula_not_the_small_angle_one():
    # asin(sin r / cos dec), not r / cos dec. The approximation under-covers as
    # the cone widens, and under-covering is the one thing a prefilter may not do.
    radius = 5.0
    dec = 60.0
    box = bounding_box(ra=100.0, dec=dec, radius_deg=radius)
    half_width = (box.ra_ranges[0][1] - box.ra_ranges[0][0]) / 2

    exact = math.degrees(
        math.asin(math.sin(math.radians(radius)) / math.cos(math.radians(dec)))
    )
    approximation = radius / math.cos(math.radians(dec))

    assert half_width >= exact
    assert exact > approximation  # the approximation would have been too small


@pytest.mark.parametrize("dec", [89.6, 90.0, -89.6, -90.0])
def test_a_cone_that_reaches_a_pole_does_not_constrain_ra(dec):
    # Every RA is within reach, so constraining it would drop real matches.
    box = bounding_box(ra=100.0, dec=dec, radius_deg=0.5)

    assert box.ra_ranges == []
    assert box.constrains_ra is False


def test_the_pole_test_is_exact_rather_than_an_arbitrary_cut_off():
    # sin(radius) vs cos(dec) decides it: at dec 89.5 a 0.4 degree cone does not
    # reach the pole and RA is still worth constraining; a 0.6 degree one does.
    assert bounding_box(ra=0.0, dec=89.5, radius_deg=0.4).constrains_ra
    assert not bounding_box(ra=0.0, dec=89.5, radius_deg=0.6).constrains_ra


def test_a_cone_wide_enough_to_span_every_ra_drops_the_bound():
    box = bounding_box(ra=100.0, dec=0.0, radius_deg=180.0)
    assert box.ra_ranges == []


def test_declination_is_clamped_to_the_sphere():
    box = bounding_box(ra=10.0, dec=89.0, radius_deg=5.0)
    assert box.dec_max == 90.0

    box = bounding_box(ra=10.0, dec=-89.0, radius_deg=5.0)
    assert box.dec_min == -90.0


def test_a_cone_crossing_zero_produces_two_ranges():
    # 'ra BETWEEN 359.5 AND 0.5' matches nothing. Two ranges, OR-ed, is the fix.
    box = bounding_box(ra=0.2, dec=0.0, radius_deg=0.5)

    assert len(box.ra_ranges) == 2
    assert box.contains(0.1, 0.0)
    assert box.contains(359.9, 0.0)
    assert not box.contains(180.0, 0.0)


def test_a_cone_crossing_360_produces_two_ranges():
    box = bounding_box(ra=359.8, dec=0.0, radius_deg=0.5)

    assert len(box.ra_ranges) == 2
    assert box.contains(359.9, 0.0)
    assert box.contains(0.1, 0.0)


def test_every_range_stays_within_zero_to_360():
    for ra in (0.0, 0.1, 180.0, 359.9, 360.0):
        box = bounding_box(ra=ra, dec=0.0, radius_deg=1.0)
        for low, high in box.ra_ranges:
            assert 0.0 <= low <= 360.0
            assert 0.0 <= high <= 360.0
            assert low <= high


def test_negative_radius_is_rejected():
    with pytest.raises(ValueError, match="must not be negative"):
        bounding_box(ra=1.0, dec=1.0, radius_deg=-1.0)


# --- the property that actually matters -------------------------------------


@pytest.mark.parametrize(
    "ra,dec",
    [
        (0.0, 0.0),
        (0.05, 0.0),  # hard against the seam
        (359.95, 0.0),
        (180.0, 45.0),
        (100.0, -70.0),
        (12.5, 88.0),  # close to, but not past, the pole cut-off
        (12.5, -88.0),
        (270.0, 30.0),
    ],
)
def test_the_box_never_excludes_a_position_inside_the_radius(ra, dec):
    """Sample the cone's rim and check every sample survives the prefilter.

    This is the one guarantee the box must provide. If it fails, the cone search
    silently returns fewer matches than exist.
    """
    radius_deg = 0.25

    for bearing_deg in range(0, 360, 5):
        bearing = math.radians(bearing_deg)
        lat = math.radians(dec)
        angular = math.radians(radius_deg)

        # Destination point along a great circle at the given bearing.
        sample_lat = math.asin(
            math.sin(lat) * math.cos(angular)
            + math.cos(lat) * math.sin(angular) * math.cos(bearing)
        )
        sample_lon = math.radians(ra) + math.atan2(
            math.sin(bearing) * math.sin(angular) * math.cos(lat),
            math.cos(angular) - math.sin(lat) * math.sin(sample_lat),
        )

        sample_ra = math.degrees(sample_lon) % 360.0
        sample_dec = math.degrees(sample_lat)

        # Sanity: the sample really is on the rim.
        assert angular_separation_deg(ra, dec, sample_ra, sample_dec) == pytest.approx(
            radius_deg, abs=1e-9
        )

        box = bounding_box(ra, dec, radius_deg)
        assert box.contains(sample_ra, sample_dec), (
            f"box excluded a point on the rim at bearing {bearing_deg}° from ({ra}, {dec})"
        )


# --- separation --------------------------------------------------------------


def test_separation_of_a_position_with_itself_is_exactly_zero():
    # The law of cosines returns ~3 milliarcseconds here, because acos loses its
    # significant digits as its argument approaches 1. Haversine returns zero.
    # An object matched against itself is the commonest case in a cross-match.
    assert angular_separation_deg(203.1, 10.2, 203.1, 10.2) == 0.0
    assert angular_separation_deg(0.0, 90.0, 0.0, 90.0) == 0.0
    assert angular_separation_deg(0.0, -90.0, 0.0, -90.0) == 0.0


@pytest.mark.parametrize("offset_arcsec", [0.001, 0.01, 0.1, 1.0, 10.0])
def test_small_separations_stay_accurate(offset_arcsec):
    # The regime a cross-match actually works in. Along the equator an RA offset
    # is the separation, so the expected answer is known exactly.
    offset_deg = offset_arcsec / 3600.0
    measured = angular_separation_deg(10.0, 0.0, 10.0 + offset_deg, 0.0)

    assert measured * 3600 == pytest.approx(offset_arcsec, rel=1e-9)


def test_antipodal_positions_are_180_degrees_apart():
    assert angular_separation_deg(0.0, 0.0, 180.0, 0.0) == pytest.approx(180.0)
    assert angular_separation_deg(0.0, 90.0, 0.0, -90.0) == pytest.approx(180.0)


def test_separation_along_the_equator_is_the_ra_difference():
    assert angular_separation_deg(10.0, 0.0, 11.0, 0.0) == pytest.approx(1.0)


def test_separation_is_symmetric():
    a = angular_separation_deg(12.3, -45.6, 78.9, 10.1)
    b = angular_separation_deg(78.9, 10.1, 12.3, -45.6)
    assert a == pytest.approx(b)


def test_separation_across_the_seam_is_small():
    # 359.99 and 0.01 are 0.02 degrees apart, not 359.98.
    assert angular_separation_deg(359.99, 0.0, 0.01, 0.0) == pytest.approx(0.02)
