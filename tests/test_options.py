"""Tests for option parsing and upgrade migration in run.py.

These matter because options come from a file written by an EARLIER version of
the add-on: a rename that reads fine on a fresh install can silently invert a
returning user's setting. Verified behaviourally rather than by inspection --
1.1.0 shipped a crash that passed both py_compile and the whole test suite.
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import run          # noqa: E402
import shade        # noqa: E402

BASE = {"easting": 2669292.99, "northing": 1211426.35, "plz": "6006"}


def _apply(**extra):
    opts = dict(BASE)
    opts.update(extra)
    run.apply_options(opts)
    return shade.OPENMETEO_MODE


def test_new_option_is_honoured():
    assert _apply(openmeteo_mode="fallback_only") == "fallback_only"
    assert _apply(openmeteo_mode="never") == "never"
    assert _apply(openmeteo_mode="always") == "always"


def test_default_is_always_when_nothing_stored():
    assert _apply() == "always"


def test_legacy_use_openmeteo_false_maps_to_never():
    """The regression: a deliberate opt-out must not become fully ON."""
    assert _apply(use_openmeteo=False) == "never"


def test_legacy_use_openmeteo_true_maps_to_always():
    assert _apply(use_openmeteo=True) == "always"


def test_new_option_wins_over_legacy_key():
    """Once the user sets the new option, the stale key must not override it."""
    assert _apply(openmeteo_mode="fallback_only", use_openmeteo=True) \
        == "fallback_only"
    assert _apply(openmeteo_mode="never", use_openmeteo=True) == "never"


def test_invalid_value_falls_back_to_default():
    assert _apply(openmeteo_mode="nonsense") == "always"


def test_lookahead_default_is_one():
    run.apply_options(dict(BASE))
    assert shade.LOOKAHEAD_H == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
