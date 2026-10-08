from __future__ import annotations

import pytest

from portolan_registry_qgis.core.footprint import covers, rects, window


def test_small_extent_gets_the_minimum_span_at_the_aspect():
    # Bologna, about 0.3 degrees across.
    view = window((11.23, 44.42, 11.43, 44.56), aspect=1.5)
    west, south, east, north = view
    assert east - west == pytest.approx(30.0)
    assert north - south == pytest.approx(20.0)
    assert (west + east) / 2 == pytest.approx(11.33)


def test_large_extent_gets_context_and_keeps_the_aspect():
    view = window((6.7, 35.5, 18.7, 47.1), aspect=1.0, min_span=10.0, context=2.0)
    assert view[2] - view[0] == pytest.approx(view[3] - view[1])
    assert view[2] - view[0] == pytest.approx(24.0)


def test_window_stays_inside_the_world():
    view = window((170.0, 80.0, 179.0, 89.0), aspect=2.0)
    assert view[2] == pytest.approx(180.0)
    assert view[3] == pytest.approx(90.0)
    assert window((-180, -90, 180, 90), aspect=2.0) == pytest.approx((-180, -90, 180, 90))


@pytest.mark.parametrize("bbox", [None, (170.0, -10.0, -170.0, 10.0)])
def test_missing_or_antimeridian_extent_shows_the_world(bbox):
    assert window(bbox, aspect=2.0) == (-180.0, -90.0, 180.0, 90.0)
    assert window(bbox, aspect=1.0) == (-90.0, -90.0, 90.0, 90.0)


def test_rects_maps_degrees_to_pixels_with_y_down():
    (rect,) = rects((0.0, 0.0, 90.0, 45.0), (-180.0, -90.0, 180.0, 90.0), 360, 180)
    assert rect == pytest.approx((180.0, 45.0, 90.0, 45.0))


def test_rects_splits_an_antimeridian_extent():
    east, west = rects((170.0, -10.0, -170.0, 10.0), (-180.0, -90.0, 180.0, 90.0), 360, 180)
    assert east == pytest.approx((350.0, 80.0, 10.0, 20.0))
    assert west == pytest.approx((0.0, 80.0, 10.0, 20.0))


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ((-180.0, -90.0, 180.0, 90.0), True),
        ((-180.0, -60.0, 180.0, 85.0), True),
        ((-10.0, 35.0, 30.0, 70.0), False),
        # Crosses the antimeridian, but still covers most of the world.
        ((-170.0, -90.0, -175.0, 90.0), True),
        ((170.0, -10.0, -170.0, 10.0), False),
    ],
)
def test_covers(bbox, expected):
    assert covers(bbox, (-180.0, -90.0, 180.0, 90.0)) is expected
