"""Tests for the `event` entities -- what actually reaches a notification.

These drive the real path: `events.warn()` / `events.error()` inside a cycle,
then `shade._publish_new_events()`. An earlier version inferred "is this new?"
from a single timestamp field in the published state, which could only ever
represent ONE active condition.

THE BUG THAT MOTIVATED THE REWRITE (2026-08-14)
    A home internet outage raised four DIFFERENT warnings per cycle -- radar,
    precipitation, radiation and gust all fail together. Only the newest was
    remembered, so every cycle each of the four differed from the single
    stored predecessor and read as new. Four notifications every five minutes,
    for as long as the outage lasted.

    `test_four_simultaneous_faults_*` is the regression.
"""
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import events      # noqa: E402
import shade       # noqa: E402


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload,
                               "retain": retain})
        return self

    def wait_for_publish(self, timeout=None):
        return True

    def events(self):
        return [p for p in self.published if "/event/" in p["topic"]]


def setup_function():
    events.reset()


def _cycle(client, *, errors=(), warnings=()):
    """One full add-on cycle: fresh cycle, report faults, publish events."""
    events.start_cycle()
    for m in errors:
        events.error(m)
    for m in warnings:
        events.warn(m)
    shade._publish_new_events(client)


# --- the regression ---------------------------------------------------------
def test_four_simultaneous_faults_fire_four_events_once():
    c = FakeClient()
    four = ["radar AND precipitation forecast unavailable",
            "app forecast unreachable",
            "radiation forecast unavailable",
            "all gust sources failed"]
    _cycle(c, warnings=four)
    assert len(c.events()) == 4, "each distinct fault is worth announcing once"
    assert {json.loads(p["payload"])["message"] for p in c.events()} == set(four)


def test_four_simultaneous_faults_do_not_refire_every_cycle():
    """The actual complaint: ~40 notifications an hour during one outage."""
    c = FakeClient()
    four = ["radar AND precipitation forecast unavailable",
            "app forecast unreachable",
            "radiation forecast unavailable",
            "all gust sources failed"]
    for _ in range(10):                      # ten cycles of the same outage
        _cycle(c, warnings=four)
    assert len(c.events()) == 4, \
        f"one outage fired {len(c.events())} events across ten cycles"


def test_a_fifth_fault_appearing_later_fires_alone():
    c = FakeClient()
    _cycle(c, warnings=["a", "b"])
    c.published.clear()
    _cycle(c, warnings=["a", "b", "c"])
    assert [json.loads(p["payload"])["message"] for p in c.events()] == ["c"]


# --- single-fault behaviour, unchanged --------------------------------------
def test_a_new_error_fires_once():
    c = FakeClient()
    _cycle(c, errors=["radar unreachable"])
    evts = c.events()
    assert len(evts) == 1
    body = json.loads(evts[0]["payload"])
    assert body["event_type"] == "error"
    assert body["message"] == "radar unreachable"
    assert body["time"]
    assert evts[0]["topic"].endswith("/event/error")


def test_a_persisting_condition_does_not_refire():
    c = FakeClient()
    for _ in range(6):
        _cycle(c, errors=["stale radar"])
    assert len(c.events()) == 1


def test_a_fault_present_on_the_very_first_cycle_is_announced():
    """Regression: the old code SEEDED on the first publish and fired nothing,
    to avoid re-announcing an event restored from /data. Persistence was
    removed in 1.5.2, so that suppression only meant a fault already present
    at startup was never reported at all -- it never changed afterwards
    either."""
    c = FakeClient()
    _cycle(c, errors=["radar unreachable at startup"])
    assert len(c.events()) == 1, "a fault at startup must still notify"


def test_a_fault_returning_after_a_clean_spell_fires_again():
    c = FakeClient()
    _cycle(c, warnings=["intermittent"])
    _cycle(c)                                # clean cycle -> resolved
    _cycle(c, warnings=["intermittent"])     # back again -> a new occurrence
    assert len(c.events()) == 2


def test_errors_and_warnings_use_separate_topics():
    c = FakeClient()
    _cycle(c, errors=["an error"], warnings=["a warning"])
    bodies = {p["topic"]: json.loads(p["payload"]) for p in c.events()}
    assert any(t.endswith("/event/error") for t in bodies)
    assert any(t.endswith("/event/warning") for t in bodies)
    for topic, body in bodies.items():
        assert body["event_type"] == ("error" if topic.endswith("error")
                                      else "warning")


def test_events_are_never_retained():
    """A retained event would be replayed to every new subscriber."""
    c = FakeClient()
    _cycle(c, errors=["boom"])
    assert c.events() and all(p["retain"] is False for p in c.events())


# --- collapsing an outage into one fact -------------------------------------
def test_a_total_outage_collapses_to_one_warning():
    """Four failures with ONE cause should read as one problem. All four are
    still in the Log; only the sensor and the notification collapse."""
    c = FakeClient()
    events.start_cycle()
    for m in ("radar unreachable", "app forecast unreachable",
              "radiation unavailable", "all gust sources failed"):
        events.warn(m)
    events.collapse_warnings("no internet connection")
    shade._publish_new_events(c)
    assert len(c.events()) == 1, "one cause must not notify four times"
    assert json.loads(c.events()[0]["payload"])["message"] == \
        "no internet connection"
    assert events.state_text("warning").startswith("no internet connection")
    assert "more" not in events.state_text("warning"), \
        "a collapsed outage must not still advertise hidden warnings"


def test_a_sustained_outage_collapses_without_refiring():
    c = FakeClient()
    for _ in range(8):
        events.start_cycle()
        events.warn("radar unreachable")
        events.warn("app forecast unreachable")
        events.collapse_warnings("no internet connection")
        shade._publish_new_events(c)
    assert len(c.events()) == 1, \
        f"a sustained outage fired {len(c.events())} times"


def test_the_collapsed_warning_dates_from_when_the_outage_started():
    """`since` must point at the first failure, not at the cycle in which the
    outage happened to be recognised."""
    events.start_cycle()
    events.warn("radar unreachable")
    first = events.active("warning")[0]["time"]
    events.warn("app forecast unreachable")
    rec = events.collapse_warnings("no internet connection")
    assert rec["time"] == first


def test_collapsing_nothing_is_a_no_op():
    events.start_cycle()
    assert events.collapse_warnings("no internet connection") is None
    assert events.state_text("warning") == "none"


# --- what the sensors show while several are active -------------------------
def test_the_sensor_names_the_newest_and_counts_the_rest():
    events.start_cycle()
    for m in ("first", "second", "third"):
        events.warn(m)
    text = events.state_text("warning")
    assert text.startswith("third since "), text
    assert "(+2 more)" in text, text
    assert len(text) <= 255


def test_the_sensor_clears_once_a_cycle_passes_without_the_fault():
    events.start_cycle()
    events.warn("something")
    assert events.state_text("warning") != "none"
    events.start_cycle()
    assert events.state_text("warning") == "none"


if __name__ == "__main__":
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
