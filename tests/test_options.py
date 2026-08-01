"""Tests for option parsing in run.py.

Options arrive from a file the user edits through the Configuration screen, so
a typo must warn and fall back to the default rather than crash the container
on boot. Checked behaviourally: a bad value has to produce the right effective
setting, not merely fail to raise.
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
