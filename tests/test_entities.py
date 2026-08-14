"""Tests for the entity manifest, and for the `Awning unsafe` signal.

WHY `Awning unsafe` EXISTS
    `retract` is true for three hazards -- wind, rain, and every gust source
    failing -- but only the first two had an entity. An automation closing on
    `Rain within 10 min` and `Wind high` therefore missed the gust fail-safe
    entirely: with no gust reading, `wind_high` is False by design (an unknown
    gust must not by itself force retract) while `retract` is True. The awning
    would have stayed out in exactly the case the fail-safe was built for.

    It is a binary sensor rather than a fourth `recommendation` value because
    it CROSSES the enum: it holds in `backup` (hazard while sunny) and in the
    hazardous half of `none` (hazard while not sunny). As an enum value every
    consumer would have to match a SET, and adding a value later would break
    each of those conditions silently.

The manifest test is the cheap general guard: a state-key typo in BINARY or
SENSORS publishes an entity that reads Unknown forever, and nothing else
notices.
"""
import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import forecast     # noqa: E402
import logic        # noqa: E402
import radar        # noqa: E402
import shade        # noqa: E402


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append({"topic": topic, "payload": payload})
        return self


def setup_function():
    shade.SUN_MODEL = "sunshine"
    shade.SUN_MIN_AWNING = shade.SUN_MIN_BACKUP = shade.SUN_MIN_INDEPENDENT = 20
    shade.MIN_TEMP_C = None
    shade.GUST_RELEASE_KMH = None
    shade.GUST_LIMIT_KMH = 40
    shade.RADAR_FAIL_SAFE = False
    shade.RADAR_THRESHOLD_MMH = 0.1
    radar.evaluate = lambda session=None: {
        "any": False, "stale": False, "age_min": 2.0,
        "radar_time": "2026-08-14T12:00:00+00:00"}
    forecast.gather = lambda *a, **k: {
        "forecast_source": "official", "gust_sources": {"official": 5.0},
        "gust_kmh": 5.0, "sunshine": True, "sunshine_minutes": 45,
        "temp_c": 20.0, "on_backup": False, "openmeteo_ok": None}


def d(**kw):
    base = dict(rain=False, gust_kmh=10, sunshine=True, gust_limit=40,
                gust_sources_ok=True)
    base.update(kw)
    return logic.decide(**base)


# --- the signal itself ------------------------------------------------------
def test_unsafe_holds_for_every_hazard():
    for label, kw in (("wind", dict(gust_kmh=50)),
                      ("rain", dict(rain=True)),
                      ("no gust source", dict(gust_sources_ok=False))):
        assert d(**kw)["retract"] is True, f"{label} did not mark the awning unsafe"


def test_the_gust_failsafe_is_invisible_to_rain_and_wind_high():
    """The regression this entity exists for: closing on those two alone
    leaves the awning out when every gust source fails."""
    r = d(gust_sources_ok=False)
    assert r["rain"] is False and r["wind_high"] is False, \
        "if either of these fired, the gap this entity fills has moved"
    assert r["retract"] is True
    assert r["awning_extend"] is False, "the decision itself keeps it in"


def test_unsafe_never_contradicts_the_awning_output():
    """Whenever the awning must be in, the add-on must not also be asking for
    it to be out -- otherwise two automations fight."""
    for rain in (False, True):
        for gust in (10, 50):
            for sunshine in (False, True):
                for ok in (False, True):
                    r = d(rain=rain, gust_kmh=gust, sunshine=sunshine,
                          gust_sources_ok=ok)
                    if r["retract"]:
                        assert not r["awning_extend"], \
                            f"unsafe but extending: {rain} {gust} {sunshine} {ok}"


def test_unsafe_crosses_the_recommendation_enum():
    """Why this is not a fourth enum value: it is true under two different
    recommendations, so a single `state:` condition could never express it."""
    sunny = d(rain=True)                      # hazard while sunny
    dark = d(rain=True, sunshine=False)       # hazard while not sunny
    assert sunny["recommendation"] == "backup" and sunny["retract"]
    assert dark["recommendation"] == "none" and dark["retract"]


def test_none_alone_cannot_distinguish_calm_from_hazard():
    """The ambiguity that prompted this: both read `none`, and only the new
    signal separates 'may stay out' from 'must come in'."""
    calm = d(sunshine=False)
    storm = d(sunshine=False, gust_kmh=80)
    assert calm["recommendation"] == storm["recommendation"] == "none"
    assert calm["retract"] is False and storm["retract"] is True


# --- the manifest -----------------------------------------------------------
def _state():
    return shade.evaluate()


def test_every_declared_entity_has_a_state_key():
    """A typo here publishes an entity that reads Unknown forever."""
    st = _state()
    missing = [key for _, _, key, _ in shade.BINARY if key not in st]
    missing += [key for _, _, key, _, _ in shade.SENSORS if key not in st]
    assert not missing, f"declared but never published: {missing}"


def test_unsafe_is_declared_and_published():
    st = _state()
    assert any(slug == "retract" for slug, _, _, _ in shade.BINARY), \
        "Awning unsafe missing from the entity manifest"
    assert "retract" in st and isinstance(st["retract"], bool)
    assert "gust_unknown" in st and isinstance(st["gust_unknown"], bool)


def _configs():
    c = FakeClient()
    shade.announce_discovery(c)
    out = {}
    for p in c.published:
        if p["topic"].endswith("/config"):
            out[p["topic"].split("/")[2]] = json.loads(p["payload"])
    return out


def test_unsafe_is_operational_and_gust_data_is_diagnostic():
    """`Awning unsafe` drives covers, so it must go unavailable with the rest
    of the operational set rather than reporting into an outage. `Gust data`
    EXPLAINS an outage, so it must stay online through one."""
    cfg = _configs()
    unsafe, gust = cfg["sms_retract"], cfg["sms_gust_unknown"]
    extend = cfg["sms_awning_extend"]
    assert unsafe["availability_topic"] == extend["availability_topic"], \
        "Awning unsafe must share availability with the outputs it guards"
    assert gust["availability_topic"] != extend["availability_topic"], \
        "Gust data must stay online to explain an outage"
    assert gust.get("entity_category") is None or \
        gust["availability_topic"] == cfg["sms_radar_ok"]["availability_topic"]


def test_state_is_published_before_availability():
    """ORDER IS LOAD-BEARING and nothing else would catch a swap.

    In a total outage `healthy` is False, so availability turns the
    operational entities unavailable -- while that same cycle's decision is
    `retract: true`, both fail-safes having fired. State first means Home
    Assistant sees `Awning unsafe` turn ON and runs the automation, then marks
    it unavailable. Availability first means the entity is already unavailable
    when the state lands, the transition is never visible, and the fail-safe
    never reaches the covers. Verified against a real outage on 2026-08-14.
    """
    c = FakeClient()
    shade.publish_state(c, {"healthy": False, "retract": True,
                            "recommendation": "none"})
    topics = [p["topic"] for p in c.published]
    assert shade._STATE_TOPIC in topics and shade._AVAIL_TOPIC in topics
    assert topics.index(shade._STATE_TOPIC) < topics.index(shade._AVAIL_TOPIC), \
        "availability published before state: the fail-safe becomes invisible"
    avail = next(p for p in c.published if p["topic"] == shade._AVAIL_TOPIC)
    assert avail["payload"] == "offline", "unhealthy cycle must go offline"


def test_gust_data_reads_as_a_subject_not_a_condition():
    """device_class 'problem' renders OK / Problem, so the name must be a
    subject -- 'Gust missing: OK' would be unreadable."""
    gust = _configs()["sms_gust_unknown"]
    assert gust["device_class"] == "problem"
    assert gust["name"] == "Gust data"


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
