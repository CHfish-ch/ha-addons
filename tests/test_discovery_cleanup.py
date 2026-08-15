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


def _valid_mqtt_filter(f):
    """MQTT 3.1.1 4.7.1.2 / 4.7.1.3, the rules a broker actually enforces:
    `+` must occupy an ENTIRE level, `#` must be the last level and alone."""
    if not f or len(f.encode()) > 65535:
        return False
    levels = f.split("/")
    for i, lvl in enumerate(levels):
        if "+" in lvl and lvl != "+":
            return False                      # e.g. "sms_+" -- not a wildcard
        if "#" in lvl and (lvl != "#" or i != len(levels) - 1):
            return False
    return True


def _matches(f, topic):
    """Does topic match filter? Single-level wildcards only, which is all we
    use -- deliberately not a general implementation."""
    fl, tl = f.split("/"), topic.split("/")
    return len(fl) == len(tl) and all(a == "+" or a == b
                                      for a, b in zip(fl, tl))


def test_the_subscribe_filter_is_a_LEGAL_mqtt_filter():
    """1.7.1 shipped `homeassistant/+/sms_+/config`. A `+` glued to a prefix is
    not a wildcard -- the client rejects the whole filter with "Invalid
    subscription filter", so the subscribe never happened and the sweep never
    ran. The previous test asserted the filter equalled the string I had
    written, which cannot catch that; this one applies the spec's rules."""
    assert _valid_mqtt_filter(shade.DISCOVERY_FILTER), \
        f"broker will reject {shade.DISCOVERY_FILTER!r}"


def test_the_subscribe_filter_matches_our_own_topics():
    """A filter that misses our namespace would collect nothing and silently
    never clean anything up."""
    for topic in shade.discovery_topics():
        assert _matches(shade.DISCOVERY_FILTER, topic), \
            f"{topic} would not be delivered by {shade.DISCOVERY_FILTER}"


def test_the_filter_also_reaches_topics_from_older_versions():
    """The orphans are the POINT: pre-repository slugs we cannot enumerate."""
    for topic in ("homeassistant/sensor/sms_last_error/config",
                  "homeassistant/binary_sensor/sms_on_backup/config",
                  "homeassistant/event/sms_whatever_we_called_it/config"):
        assert _matches(shade.DISCOVERY_FILTER, topic), topic


def test_the_validator_itself_rejects_the_1_7_1_filter():
    """Guard the guard: a validator that passes everything proves nothing."""
    assert not _valid_mqtt_filter("homeassistant/+/sms_+/config")
    assert not _valid_mqtt_filter("homeassistant/#/config")
    assert _valid_mqtt_filter("homeassistant/+/+/config")
    assert _valid_mqtt_filter("homeassistant/#")


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
