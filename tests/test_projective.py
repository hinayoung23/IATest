import struct

import pytest

from iatest.engines.projective import (
    compose,
    frame_matrix,
    inverse,
    project,
    region,
)
from iatest.engines.web_visual import stable_png


def test_capture_waits_for_consecutive_pixels_without_replaying_input():
    header = b'\x89PNG\r\n\x1a\n' + bytes(8) + struct.pack('>II', 2, 2)
    frames = iter([header+b'old', header+b'new', header+b'new'])
    data, width, height = stable_png(lambda: next(frames))
    assert data.endswith(b'new') and (width, height) == (2,2)
    frames = iter([header+b'old', header+b'new', header+b'old'])
    with pytest.raises(ValueError, match='did not stabilize'):
        stable_png(lambda: next(frames))


def test_capture_quota_is_checked_before_accepting_stable_frames():
    invalid = b'\x89PNG\r\n\x1a\n' + bytes(8) + struct.pack('>II', 1024, 1024)
    with pytest.raises(ValueError, match='pixel budget'):
        stable_png(lambda: invalid)


def test_perspective_maps_four_corners_and_roundtrips_interior():
    quad = [50, 30, 270, 10, 240, 160, 80, 130]
    matrix = frame_matrix(300, 200, quad)
    for x, y, expected in [
        (0, 0, quad[:2]),
        (300, 0, quad[2:4]),
        (300, 200, quad[4:6]),
        (0, 200, quad[6:]),
    ]:
        assert project(matrix, x, y) == pytest.approx(expected)
    point = project(matrix, 91, 67)
    assert project(inverse(matrix), *point) == pytest.approx((91, 67))
    assert project(compose(inverse(matrix), matrix), 91, 67) == pytest.approx((91, 67))
    assert region(
        matrix, {"x": 0, "y": 0, "width": 300, "height": 200}
    ) == pytest.approx({"x": 50, "y": 10, "width": 220, "height": 150})


@pytest.mark.parametrize(
    "quad",
    [
        [0, 0, 1, 1, 0, 1, 1, 0],
        [0, 0, 1, 0, 2, 0, 3, 0],
        [0, 0, float("nan"), 0, 1, 1, 0, 1],
    ],
)
def test_degenerate_or_nonfinite_frames_fail_before_input(quad):
    with pytest.raises(ValueError):
        frame_matrix(100, 100, quad)
