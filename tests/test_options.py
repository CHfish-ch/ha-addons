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



# --- the hysteresis latch across a config change ----------------------------
# Reported 2026-08-15: retracted at gust_limit 40, user raised the limit to 50
# and restarted, and was retracted again at 41.8 km/h. The latch had been
# persisted with no record of the limit it was earned under, so it kept
# asserting a crossing that never happened under the new setting.
import json                                                     # noqa: E402
import tempfile                                                 # noqa: E402


def _latch_env(limit, release, saved):
    """Point run.py at a temp state file holding `saved`, with these thresholds
    now in force. Returns what _load_wind_high() makes of it."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    if saved is not None:
        with open(path, "w") as fh:
            json.dump(saved, fh)
    else:
        os.unlink(path)
    old_file = run._WIND_STATE_FILE
    old_l, old_r = shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH
    run._WIND_STATE_FILE = path
    shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH = limit, release
    try:
        return run._load_wind_high()
    finally:
        run._WIND_STATE_FILE = old_file
        shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH = old_l, old_r
        if os.path.exists(path):
            os.unlink(path)


def test_the_latch_survives_a_restart_when_nothing_changed():
    """A2: a restart mid-storm must not drop back to the extend side."""
    assert _latch_env(40.0, 30.0, {"wind_high": True, "gust_limit": 40.0,
                                   "gust_release": 30.0}) is True


def test_raising_the_limit_discards_the_latch():
    """THE REPORTED BUG. Latched at limit 40; user raises it to 50. A crossing
    of 40 says nothing about 50, so holding the awning in at 41.8 km/h is
    asserting something that never happened."""
    assert _latch_env(50.0, 40.0, {"wind_high": True, "gust_limit": 40.0,
                                   "gust_release": 40.0}) is False


def test_changing_only_the_release_discards_the_latch():
    """The release decides when the latch CLEARS, so it is equally part of
    what the stored boolean means."""
    assert _latch_env(40.0, 20.0, {"wind_high": True, "gust_limit": 40.0,
                                   "gust_release": 30.0}) is False


def test_a_pre_1_7_4_file_is_discarded_once():
    """Old files carry no thresholds, so they cannot be shown to still apply."""
    assert _latch_env(40.0, 30.0, {"wind_high": True}) is False


def test_a_missing_or_corrupt_file_is_not_a_latch():
    assert _latch_env(40.0, 30.0, None) is False


def test_lowering_the_limit_also_rederives_rather_than_guessing():
    """Kept for the same reason: the honest answer is the next forecast, not
    an inference about whether the old crossing implies the new one."""
    assert _latch_env(30.0, 20.0, {"wind_high": True, "gust_limit": 40.0,
                                   "gust_release": 30.0}) is False


def test_saving_records_the_thresholds_in_force():
    """Without this the next start cannot tell whether the latch still means
    anything, which is the whole bug."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    old_file = run._WIND_STATE_FILE
    old_l, old_r = shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH
    run._WIND_STATE_FILE = path
    shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH = 55.0, 45.0
    try:
        run._save_wind_high(True)
        with open(path) as fh:
            saved = json.load(fh)
    finally:
        run._WIND_STATE_FILE = old_file
        shade.GUST_LIMIT_KMH, shade.GUST_RELEASE_KMH = old_l, old_r
        os.unlink(path)
    assert saved == {"wind_high": True, "gust_limit": 55.0,
                     "gust_release": 45.0}

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
