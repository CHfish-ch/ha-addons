"""Tests for the event recorder and its integration into state."""
import os
import sys

# tests/ and tools/ sit beside the add-on directory, so put it on the path.
# Works however the file is invoked: pytest, `python3 tests/test_x.py`, or from
# inside the directory itself.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import events


def setup_function():
    events._last["error"] = None
    events._last["warning"] = None
    events._this_cycle["error"] = False
    events._this_cycle["warning"] = False


def test_records_latest_warning():
    events.warn("first")
    events.warn("second")
    assert events.last_warning()["message"] == "second"   # latest wins
    assert events.last_warning()["time"]


def test_records_latest_error():
    events.error("boom")
    assert events.last_error()["message"] == "boom"
    assert events.last_error()["time"]


def test_error_and_warning_independent():
    events.warn("w")
    events.error("e")
    assert events.last_warning()["message"] == "w"        # not overwritten
    assert events.last_error()["message"] == "e"


def test_snapshot_shape():
    events.error("x")
    snap = events.snapshot()
    assert set(snap) == {"last_error", "last_warning"}
    assert snap["last_error"]["message"] == "x"
    assert snap["last_warning"] is None                   # none yet


def test_none_when_empty():
    assert events.last_error() is None
    assert events.last_warning() is None


def test_varying_detail_does_not_defeat_deduplication():
    """Regression: a moving value in the MESSAGE re-fires the event entity.

    Seen 2026-08-12 during a 4 h MeteoSwiss radar outage. The warning read
    "radar frame 250.7 min old", and the age ticked up every cycle, so every
    cycle looked like a brand-new event: the sensor timestamp moved and the
    Warning event entity fired ~50 times for one fault. Varying values belong
    in `detail`, which is logged but never deduplicated on.
    """
    for cycle in range(50):
        events.warn("radar frames are stale",
                    detail=f"newest frame {20 + cycle * 5:.1f} min old")
    w = events.last_warning()
    assert w["count"] == 50, "repeats must accumulate, not restart"
    assert w["message"] == "radar frames are stale", \
        "detail must not leak into the stored message"


def test_first_seen_time_is_stable_across_repeats_with_detail():
    events.warn("radar frames are stale", detail="newest frame 20.0 min old")
    first = events.last_warning()["time"]
    for age in (25.0, 30.0, 250.7):
        events.warn("radar frames are stale", detail=f"newest frame {age} min old")
    assert events.last_warning()["time"] == first, \
        "a moving detail must not move the event timestamp"


def test_a_genuinely_different_message_still_fires():
    """Dedup must not be so eager that a real change goes unnoticed."""
    events.warn("radar frames are stale", detail="20 min")
    first = events.last_warning()["time"]
    events.warn("all gust sources failed -> awning kept in (fail-safe)")
    assert events.last_warning()["time"] != first
    assert events.last_warning()["count"] == 1


def test_a_clean_cycle_clears_immediately():
    """A resolved fault must stop looking live on the FIRST clean cycle, not
    one cycle later -- the lag is invisible and reads as a stuck sensor."""
    events.start_cycle()
    events.warn("radar frames are stale")
    assert events.state_text("warning").startswith("radar frames are stale")

    events.start_cycle()                      # cycle with nothing reported
    assert events.state_text("warning") == "none"
    assert events.snapshot()["last_warning"] is None, \
        "attributes must agree with the state, not lag behind it"


def test_state_reads_message_since_when():
    events.start_cycle()
    events.warn("radar frames are stale")
    text = events.state_text("warning")
    assert " since " in text
    assert text.startswith("radar frames are stale")
    assert len(text) <= 255, "Home Assistant caps a state at 255 characters"


def test_a_persisting_fault_keeps_its_original_since():
    """Otherwise the state rewrites every cycle and floods the recorder."""
    events.start_cycle(); events.warn("radar frames are stale")
    first = events.state_text("warning")
    for _ in range(5):
        events.start_cycle(); events.warn("radar frames are stale")
    assert events.state_text("warning") == first, \
        "'since' must not move while the same fault continues"


def test_a_recurrence_after_a_clean_spell_gets_a_new_since():
    """A fault that ended and came back is a NEW occurrence -- pointing at the
    old one would misreport how long it has been going, and would stop the
    event entity firing for it."""
    events.start_cycle(); events.warn("radar frames are stale")
    first = events.last_warning()["time"]
    events.start_cycle()                      # clean
    events.start_cycle()                      # clean again -> forget it
    events.start_cycle(); events.warn("radar frames are stale")
    assert events.last_warning()["time"] != first


def test_clearing_one_level_leaves_the_other_alone():
    events.start_cycle()
    events.error("gust file hash changed")
    events.warn("radar frames are stale")
    events.start_cycle()
    events.error("gust file hash changed")    # error persists, warning does not
    assert events.state_text("error").startswith("gust file hash changed")
    assert events.state_text("warning") == "none"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        setup_function()
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
