"""Tests for the MQTT `event` entities.

The whole point of these entities is that an automation needs no hand-written
timestamp condition. That only holds if the add-on fires them exactly once per
NEW event -- never on a restart, never on a repeat -- so those are the cases
tested here rather than the happy path alone.
"""
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import shade      # noqa: E402


class FakeClient:
    """Captures publishes instead of talking to a broker."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload,
                               "retain": retain, "qos": qos})
        return self

    def wait_for_publish(self, timeout=None):
        return True

    def events(self):
        return [p for p in self.published if "/event/" in p["topic"]]


def setup_function():
    shade._event_seeded = False
    shade._event_seen.update({"error": None, "warning": None})


def _state(err_time=None, err_msg="none", warn_time=None, warn_msg="none"):
    return {"last_error_time": err_time, "last_error_message": err_msg,
            "last_warning_time": warn_time, "last_warning_message": warn_msg,
            "healthy": True}


def test_first_publish_seeds_and_fires_nothing():
    """A restart must not re-announce the error restored from /data."""
    c = FakeClient()
    shade._publish_new_events(c, _state(err_time="2026-08-01T10:00:00+00:00",
                                        err_msg="old error from before"))
    assert c.events() == []


def test_new_error_fires_once():
    c = FakeClient()
    shade._publish_new_events(c, _state())                    # seed: nothing
    shade._publish_new_events(c, _state(err_time="2026-08-01T11:00:00+00:00",
                                        err_msg="radar unreachable"))
    evts = c.events()
    assert len(evts) == 1
    body = json.loads(evts[0]["payload"])
    assert body["event_type"] == "error"
    assert body["message"] == "radar unreachable"
    assert evts[0]["topic"].endswith("/event/error")


def test_repeat_of_same_event_does_not_refire():
    """A condition persisting for hours notifies once, not every cycle."""
    c = FakeClient()
    shade._publish_new_events(c, _state())
    same = _state(err_time="2026-08-01T11:00:00+00:00", err_msg="stale radar")
    shade._publish_new_events(c, same)
    for _ in range(5):                       # five more cycles, same condition
        shade._publish_new_events(c, same)
    assert len(c.events()) == 1


def test_a_genuinely_different_event_fires_again():
    c = FakeClient()
    shade._publish_new_events(c, _state())
    shade._publish_new_events(c, _state(err_time="2026-08-01T11:00:00+00:00",
                                        err_msg="first"))
    shade._publish_new_events(c, _state(err_time="2026-08-01T12:00:00+00:00",
                                        err_msg="second"))
    evts = c.events()
    assert len(evts) == 2
    assert json.loads(evts[1]["payload"])["message"] == "second"


def test_errors_and_warnings_use_separate_topics():
    c = FakeClient()
    shade._publish_new_events(c, _state())
    shade._publish_new_events(c, _state(err_time="2026-08-01T11:00:00+00:00",
                                        err_msg="an error",
                                        warn_time="2026-08-01T11:00:01+00:00",
                                        warn_msg="a warning"))
    topics = {p["topic"]: json.loads(p["payload"]) for p in c.events()}
    assert any(t.endswith("/event/error") for t in topics)
    assert any(t.endswith("/event/warning") for t in topics)
    for topic, body in topics.items():
        assert body["event_type"] == ("error" if topic.endswith("error")
                                      else "warning")


def test_events_are_never_retained():
    """A retained event would be replayed to every new subscriber."""
    c = FakeClient()
    shade._publish_new_events(c, _state())
    shade._publish_new_events(c, _state(err_time="2026-08-01T11:00:00+00:00",
                                        err_msg="boom"))
    assert all(p["retain"] is False for p in c.events())


def test_discovery_declares_both_event_entities():
    c = FakeClient()
    shade.announce_discovery(c)
    cfgs = {p["topic"]: json.loads(p["payload"]) for p in c.published
            if p["topic"].startswith("homeassistant/event/")}
    assert len(cfgs) == 2, f"expected 2 event entities, got {list(cfgs)}"
    for topic, cfg in cfgs.items():
        assert cfg["event_types"], f"{topic} declares no event_types"
        assert "state_topic" in cfg                  # both are required keys
        assert cfg["entity_category"] == "diagnostic"
        assert "expire_after" not in cfg, "events must not expire"


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
