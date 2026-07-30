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
