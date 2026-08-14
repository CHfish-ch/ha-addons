"""Tests for retracting discovery configs left behind by earlier versions.

WHY THIS EXISTS
    Publishing the current entity set does not remove entities an older
    version published. A retained config sits on the broker until an empty
    payload retracts it, so Home Assistant recreates the dead entity on every
    restart, forever. An install was found on 2026-08-14 carrying five
    entities from builds that predate this repository -- and one of those
    frozen registry rows then attached its old entity_id to a NEW entity that
    happened to reuse its unique_id, which is how a sensor named "Awning
    unsafe" ended up as `..._awning_retract`.

THE DANGEROUS DIRECTION
    This code DELETES entities. Every test below exists to pin the direction
    it fails in: an entity we cannot positively identify as ours must survive.
    Leaving one dead entity of our own is a nuisance; deleting somebody else's
    is data loss in their automations.
"""
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import shade        # noqa: E402


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload,
                               "retain": retain})
        return self


def setup_function():
    shade._seen_discovery.clear()


def _ours(topic, **over):
    cfg = {"name": "Whatever", "device": dict(shade.DEVICE)}
    cfg.update(over)
    shade.note_discovery(topic, json.dumps(cfg).encode())


# --- what gets retracted ----------------------------------------------------
def test_an_entity_we_no_longer_publish_is_retracted():
    _ours("homeassistant/sensor/sms_last_error/config")
    c = FakeClient()
    gone = shade.retire_orphan_discovery(c)
    assert gone == ["homeassistant/sensor/sms_last_error/config"]
    assert c.published[0]["payload"] == "", "must be an EMPTY payload"
    assert c.published[0]["retain"] is True, \
        "a non-retained empty payload leaves the retained config in place"


def test_everything_we_still_publish_survives():
    for topic in shade.discovery_topics():
        _ours(topic)
    c = FakeClient()
    assert shade.retire_orphan_discovery(c) == []
    assert c.published == [], "retracted a live entity"


def test_the_five_orphans_found_in_the_field():
    """Regression on the real case, using the pre-repository slugs."""
    stale = ["homeassistant/binary_sensor/sms_on_backup/config",
             "homeassistant/sensor/sms_irradiance_awning/config",
             "homeassistant/sensor/sms_irradiance_wall/config",
             "homeassistant/sensor/sms_last_error/config",
             "homeassistant/sensor/sms_last_warning/config"]
    for t in stale:
        _ours(t)
    for t in shade.discovery_topics():
        _ours(t)
    c = FakeClient()
    assert sorted(shade.retire_orphan_discovery(c)) == sorted(stale)


# --- what must NEVER be retracted -------------------------------------------
def test_another_integrations_config_is_never_touched():
    """The topic filter can collide; the payload is what decides."""
    shade.note_discovery(
        "homeassistant/sensor/sms_something/config",
        json.dumps({"name": "Not ours",
                    "device": {"identifiers": ["some_other_addon"]}}).encode())
    c = FakeClient()
    assert shade.retire_orphan_discovery(c) == []
    assert c.published == []


def test_an_unparseable_payload_is_never_touched():
    shade.note_discovery("homeassistant/sensor/sms_x/config", b"{not json")
    shade.note_discovery("homeassistant/sensor/sms_y/config", b"")
    c = FakeClient()
    assert shade.retire_orphan_discovery(c) == []


def test_a_config_with_no_device_is_never_touched():
    shade.note_discovery("homeassistant/sensor/sms_z/config",
                         json.dumps({"name": "deviceless"}).encode())
    c = FakeClient()
    assert shade.retire_orphan_discovery(c) == []


def test_an_already_retracted_topic_is_not_retracted_again():
    """An empty payload arriving for a topic we saw must drop it: otherwise
    the sweep re-publishes deletions for entities that are already gone."""
    _ours("homeassistant/sensor/sms_last_error/config")
    shade.note_discovery("homeassistant/sensor/sms_last_error/config", b"")
    c = FakeClient()
    assert shade.retire_orphan_discovery(c) == []


# --- the topic set the sweep compares against -------------------------------
def test_discovery_topics_matches_what_announce_actually_publishes():
    """If these two ever drift, the sweep deletes live entities."""
    c = FakeClient()
    shade.announce_discovery(c)
    announced = {p["topic"] for p in c.published
                 if p["topic"].endswith("/config")}
    assert announced == shade.discovery_topics(), \
        f"drift: {announced ^ shade.discovery_topics()}"


def test_the_subscribe_filter_matches_our_own_topics():
    """A filter that misses our namespace would collect nothing and silently
    never clean anything up."""
    assert shade.DISCOVERY_FILTER == "homeassistant/+/sms_+/config"
    for topic in shade.discovery_topics():
        parts = topic.split("/")
        assert len(parts) == 4 and parts[0] == "homeassistant" \
            and parts[2].startswith("sms_") and parts[3] == "config", \
            f"{topic} would not be matched by the subscription"


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
