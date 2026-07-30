"""Radar motion-estimation tests.

Regression for the 2026-07-28 incident: on a cloudless afternoon the phase
correlation produced ~90 km/h vectors pointing in opposite directions between
overlapping frame triplets, projected the sample point ~14 km away, and picked
up marginal echo that was never approaching -- one cycle of spurious rain.
"""
import os
import sys

# tests/ and tools/ sit beside the add-on directory, so put it on the path.
# Works however the file is invoked: pytest, `python3 tests/test_x.py`, or from
# inside the directory itself.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import numpy as np                                             # noqa: E402

import radar                                                  # noqa: E402

# radar.py guards its numpy/h5py imports and degrades to None, so no stubbing is
# needed here -- but these tests do need real numpy to build the frames.
radar.THRESHOLD_MMH = 0.1


def _flat(value, n=60):
    return np.full((n, n), value, dtype=np.float32)


def test_empty_field_produces_no_motion():
    """The incident: a field with no echo must not yield a motion vector."""
    frames = [_flat(0.01) for _ in range(3)]
    assert radar.estimate_motion(frames, 30, 30) == (0.0, 0.0)


def test_all_nodata_produces_no_motion():
    nan = np.full((60, 60), np.nan, dtype=np.float32)
    assert radar.estimate_motion([nan, nan, nan], 30, 30) == (0.0, 0.0)


def test_real_shower_is_still_tracked():
    """A genuine block of echo must still produce a vector."""
    a, b = _flat(0.0), _flat(0.0)
    a[20:30, 20:30] = 2.0
    b[22:32, 23:33] = 2.0
    assert radar.estimate_motion([a, b], 30, 30) != (0.0, 0.0)


def test_echo_below_min_cells_is_ignored():
    """One or two stray wet cells are clutter, not something to track."""
    a, b = _flat(0.0), _flat(0.0)
    a[30, 30] = 5.0
    b[31, 31] = 5.0
    assert radar.estimate_motion([a, b], 30, 30) == (0.0, 0.0)


def test_implausible_speed_still_rejected():
    """The existing MAX_MOTION_KMH guard must survive the new gate."""
    a, b = _flat(0.0), _flat(0.0)
    a[10:20, 10:20] = 3.0
    b[40:50, 40:50] = 3.0          # absurd jump between frames
    d = radar.estimate_motion([a, b], 30, 30)
    speed = (d[0] ** 2 + d[1] ** 2) ** 0.5 * 12
    assert speed <= radar.MAX_MOTION_KMH


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
