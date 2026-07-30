#!/usr/bin/env python3
"""Swiss Meteo Shade -- orchestrator.

Combines three MeteoSwiss-derived signals into weather-driven awning/blind
sensors for Home Assistant, published over MQTT discovery:

  rain      : radar RZC, 1 km / 5 min, now..+10 min
  gust      : max forecast gust over the outlook window (official -> app
              fallback, plus Open-Meteo ICON, all km/h)
  sunshine  : forecast sunshine in the outlook window (official -> app)
  temp      : forecast temperature, for the optional min-temp gate

Config comes from /data/options.json under the add-on; run.py sets module
globals before calling. Standalone: edit CONFIG below and run --dry-run.

Data (c) MeteoSwiss, open government data ("Source: MeteoSwiss").
Open-Meteo data (c) Open-Meteo, CC-BY.
"""

import argparse
import json
from datetime import datetime, timezone

import requests

import events
import radar
import forecast
import logic

# ---------------------------------------------------------------------------
# CONFIG (standalone defaults; run.py overrides from options.json)
# ---------------------------------------------------------------------------
POS_LV95 = (2665512.66, 1211882.47)   # PLACEHOLDER central Lucerne -- replace
PLZ = "6006"
GUST_LIMIT_KMH = 40.0
GUST_RELEASE_KMH = None                # hysteresis re-extend threshold; None=off
MIN_TEMP_C = None                      # don't deploy shade below this; None=off
USE_OPENMETEO = True
PREFER_APP = False                     # official primary; True = app first
LOOKAHEAD_H = 2
RADAR_THRESHOLD_MMH = 0.1
RADAR_TOLERANCE_KM = 1
RADAR_FAIL_SAFE = False               # radar outage -> treat as rain (retract)?
SUN_MIN_AWNING = 20                   # per-output sunshine thresholds (min/h)
SUN_MIN_BACKUP = 20
SUN_MIN_INDEPENDENT = 20
INTERVAL_SECONDS = 300                 # used to size expire_after

MQTT_BASE = "swiss_meteo_shade"

DEVICE = {
    "identifiers": ["swiss_meteo_shade"],
    "name": "Swiss Meteo Shade",
    "manufacturer": "MeteoSwiss (open data) + Open-Meteo",
    "model": "Weather-driven awning & blind automation",
}

_STATE_TOPIC = f"{MQTT_BASE}/state"
_AVAIL_TOPIC = f"{MQTT_BASE}/availability"
_DIAG_AVAIL_TOPIC = f"{MQTT_BASE}/availability_diag"
_EVENTS_TOPIC = f"{MQTT_BASE}/events"   # small payload: only event fields


def _lv95_to_wgs84(e, n):
    """Approximate LV95 -> WGS84 (swisstopo formulas, ~1 m)."""
    y = (e - 2600000) / 1_000_000
    x = (n - 1200000) / 1_000_000
    lon = (2.6779094 + 4.728982 * y + 0.791484 * y * x
           + 0.1306 * y * x * x - 0.0436 * y**3)
    lat = (16.9023892 + 3.238272 * x - 0.270978 * y * y
           - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x**3)
    return lat * 100 / 36, lon * 100 / 36


def in_radar_grid(e, n):
    """Cheap startup check: are the coordinates inside the Swiss radar grid?"""
    return 2255000 <= e < 2965000 and 840000 <= n < 1480000


def _sun_at(fc, threshold):
    """Per-output sun flag from the raw peak sunshine minutes and this output's
    threshold. Preserves None (unknown) so decide() still flags it unavailable
    rather than reading a missing forecast as 'not sunny'."""
    if fc.get("sunshine") is None:        # unknown -> stay unknown
        return None
    mins = fc.get("sunshine_minutes")
    if mins is None:
        return None
    return mins >= threshold


def _events_state():
    """Flatten the latest error/warning into state fields for the two sensors.

    Each sensor's state is the event's timestamp, or '' when nothing has been
    recorded yet (HA shows that as unknown). The message travels as an attribute
    on the dedicated events topic, not the main state topic, so ordinary cycle
    updates can't churn it."""
    snap = events.snapshot()
    err, warn = snap["last_error"], snap["last_warning"]
    return {
        "last_error_time": err["time"] if err else None,
        "last_error_message": err["message"] if err else "none",
        "last_error_count": err.get("count", 1) if err else 0,
        "last_error_seen": err.get("last_seen") if err else None,
        "last_warning_time": warn["time"] if warn else None,
        "last_warning_message": warn["message"] if warn else "none",
        "last_warning_count": warn.get("count", 1) if warn else 0,
        "last_warning_seen": warn.get("last_seen") if warn else None,
    }


def evaluate(session=None, prev_wind_high=False):
    """Run all sources and the decision. `session` is reused across cycles."""
    e, n = POS_LV95
    lat, lon = _lv95_to_wgs84(e, n)

    # 1. radar rain -- failures degrade to rain-unknown, not a dead cycle
    radar.POS_LV95 = POS_LV95
    radar.POS_WGS84 = None
    radar.THRESHOLD_MMH = RADAR_THRESHOLD_MMH
    radar.FORECAST_TOLERANCE_KM = RADAR_TOLERANCE_KM
    try:
        rad = radar.evaluate(session=session)
        if rad.get("stale"):
            # A6: a frame older than the radar module's limit must not be
            # reported as confident rain=False. Treat as unavailable.
            events.warn(f"radar frame {rad.get('age_min')} min old "
                        f"-> treated as unavailable")
            radar_ok = False
            rain = bool(RADAR_FAIL_SAFE)
        else:
            rain = bool(rad.get("any"))
            radar_ok = True
    except Exception as exc:
        events.warn(f"radar unavailable: {type(exc).__name__}: {exc}")
        rad, radar_ok = {}, False
        rain = bool(RADAR_FAIL_SAFE)      # A7: honour fail-safe on outage too

    # 2. forecast gust + sunshine + temp
    fc = forecast.gather(PLZ, e, n, lat, lon, use_openmeteo=USE_OPENMETEO,
                         prefer_app=PREFER_APP, lookahead_h=LOOKAHEAD_H,
                         session=session, need_temp=MIN_TEMP_C is not None)

    # 3. decision (with hysteresis + temp gate)
    gust_sources_ok = bool(fc.get("gust_sources"))   # at least one answered
    # If a temperature gate is configured but no temperature was returned, the
    # gate can't apply this cycle -- log it rather than silently ignoring.
    if MIN_TEMP_C is not None and fc.get("temp_c") is None:
        events.warn("min_temp_c set but no temperature forecast this cycle "
                    "-> temperature gate skipped")
    dec = logic.decide(
        rain=rain, gust_kmh=fc["gust_kmh"], sunshine=fc["sunshine"],
        gust_limit=GUST_LIMIT_KMH, temp_c=fc.get("temp_c"),
        gust_release=GUST_RELEASE_KMH, min_temp_c=MIN_TEMP_C,
        prev_wind_high=prev_wind_high, gust_sources_ok=gust_sources_ok,
        sun_awning=_sun_at(fc, SUN_MIN_AWNING),
        sun_backup=_sun_at(fc, SUN_MIN_BACKUP),
        sun_independent=_sun_at(fc, SUN_MIN_INDEPENDENT))

    # surface degradations as warnings (they populate the Last warning sensor)
    if fc.get("on_backup"):
        events.warn(f"primary forecast unavailable -> using "
                    f"{fc.get('forecast_source') or 'no'} source")
    if dec["forecast_unavailable"]:
        if dec["gust_unknown"]:
            events.warn("all gust sources failed -> awning kept in (fail-safe)")
        else:
            events.warn("sunshine forecast unavailable this cycle")

    # a cycle is "healthy" if the decision rests on real data: radar known
    # (rain) OR at least the forecast produced a sunshine/gust signal
    healthy = radar_ok or not dec["forecast_unavailable"]

    return {
        "awning_extend": dec["awning_extend"],
        "backup_blinds_close": dec["backup_blinds_close"],
        "independent_blinds_close": dec["independent_blinds_close"],
        "retract": dec["retract"],
        "recommendation": dec["recommendation"],
        "reason": dec["reason"],
        "rain": dec["rain"],
        "sun": dec["sun"],
        "wind_high": dec["wind_high"],
        "gust_kmh": fc["gust_kmh"],
        "gust_unknown": dec["gust_unknown"],
        "gust_limit_kmh": GUST_LIMIT_KMH,
        "sunshine_minutes": fc["sunshine_minutes"],
        "temp_c": fc.get("temp_c"),
        "forecast_unavailable": dec["forecast_unavailable"],
        "temp_blocks": dec["temp_blocks"],
        "forecast_source": fc["forecast_source"],
        "gust_sources": fc["gust_sources"],
        "gust_official": fc["gust_sources"].get("official"),
        "gust_app": fc["gust_sources"].get("app"),
        "gust_openmeteo": fc["gust_sources"].get("openmeteo"),
        "openmeteo_ok": fc["openmeteo_ok"],
        "on_backup": fc["on_backup"],
        "radar_ok": radar_ok,
        "radar_time": rad.get("radar_time"),
        "radar_age_min": rad.get("age_min"),
        "healthy": healthy,
        "updated": datetime.now(timezone.utc).isoformat(),
        **_events_state(),
    }


# ---------------------------------------------------------------------------
# MQTT discovery + publishing (persistent connection handled by caller)
# ---------------------------------------------------------------------------
# (slug, friendly, state-key, device_class-or-None)
BINARY = [
    ("awning_extend", "Awning extend", "awning_extend", None),
    ("backup_blinds_close", "Backup blinds close", "backup_blinds_close", None),
    ("independent_blinds_close", "Independent blinds close",
     "independent_blinds_close", None),
    ("rain", "Rain within 10 min", "rain", "moisture"),
    ("sun", "Sun expected", "sun", None),
    ("wind_high", "Wind high", "wind_high", None),
    # These two use device_classes whose state WORDS are fixed by HA:
    #   'problem'      always renders  OK / Problem      (on = problem)
    #   'connectivity' always renders  Connected / Disconnected  (on = connected)
    # So the name must be a SUBJECT, not a condition -- "Forecast data: OK"
    # reads correctly, whereas "Forecast missing: OK" or "Forecast present: OK"
    # are ambiguous or inverted.
    ("forecast_unavailable", "Forecast data", "forecast_unavailable",
     "problem"),
    ("radar_ok", "Radar data", "radar_ok", "connectivity"),
]
# (slug, friendly, state-key, unit-or-None, entity_category-or-None)
SENSORS = [
    ("recommendation", "Shade recommendation", "recommendation", None, None),
    ("gust_kmh", "Forecast gust", "gust_kmh", "km/h", None),
    ("sunshine_minutes", "Forecast sunshine", "sunshine_minutes", "min", None),
    ("temp_c", "Forecast temperature", "temp_c", "°C", None),
    ("forecast_source", "Forecast source", "forecast_source", None, "diagnostic"),
    ("radar_age_min", "Radar age", "radar_age_min", "min", "diagnostic"),
    ("reason", "Shade reason", "reason", None, "diagnostic"),
    ("last_error_time", "Last error", "last_error_time", None, "diagnostic"),
    ("last_warning_time", "Last warning", "last_warning_time", None, "diagnostic"),
    # Only the ACTIVE primary source has a value: official and app are a
    # primary/fallback pair, never queried in parallel, so whichever is not in
    # use reads Unknown. Open-Meteo is queried alongside, so it is normally
    # populated whenever use_openmeteo is on.
    ("gust_official", "Gust (MeteoSwiss official)", "gust_official", "km/h",
     "diagnostic"),
    ("gust_app", "Gust (MeteoSwiss app, fallback)", "gust_app", "km/h",
     "diagnostic"),
    ("gust_openmeteo", "Gust (Open-Meteo ICON)", "gust_openmeteo", "km/h",
     "diagnostic"),
]

# numeric sensors need value_template guards so JSON null doesn't become "None"
_NULLABLE_STR = {"forecast_source"}   # B8: else Jinja renders the string "None"
_NUMERIC = {"gust_kmh", "sunshine_minutes", "temp_c", "radar_age_min",
            "gust_official", "gust_app", "gust_openmeteo"}


def _num_tmpl(key):
    # On a state_class=measurement sensor, a non-numeric string like 'unknown'
    # triggers HA warning logs. Emit an empty string instead, which HA maps to
    # 'unknown' state without complaint.
    return ("{{ value_json.%s if value_json.%s is not none else '' }}"
            % (key, key))


def announce_discovery(client):
    """Publish retained discovery configs ONCE at connect (bug: was every cycle)."""
    # B5: entities must not expire during a normal failure-backoff retry. The
    # loop sleeps up to INTERVAL_SECONDS + 600 (max backoff), so expiry must
    # exceed that with headroom.
    expire = max(3 * INTERVAL_SECONDS, INTERVAL_SECONDS + 600 + 300)

    def pub(domain, slug, cfg, diagnostic=False):
        # B2: diagnostic entities (the ones that EXPLAIN an outage) must stay
        # available during one, so they use a separate always-online topic and
        # no expiry. Operational entities use the health-gated topic.
        if diagnostic:
            cfg["availability_topic"] = _DIAG_AVAIL_TOPIC
            # B7: still expire, but generously -- long enough to survive a
            # failure-backoff cycle, short enough that a dead container stops
            # showing stale diagnostics as if they were live.
            cfg["expire_after"] = 6 * INTERVAL_SECONDS
        else:
            cfg["availability_topic"] = _AVAIL_TOPIC
            cfg["expire_after"] = expire
        cfg["unique_id"] = f"sms_{slug}"
        cfg["device"] = DEVICE
        cfg.setdefault("origin", {"name": "Swiss Meteo Shade",
                                  "sw_version": "1.0.0"})   # B7
        client.publish(f"homeassistant/{domain}/sms_{slug}/config",
                       json.dumps(cfg), retain=True, qos=1)

    _diag_binary = {"forecast_unavailable", "radar_ok"}
    for slug, name, key, dev_class in BINARY:
        cfg = {"name": name, "state_topic": _STATE_TOPIC,
               "value_template": "{{ 'ON' if value_json.%s else 'OFF' }}" % key}
        if dev_class:
            cfg["device_class"] = dev_class
        pub("binary_sensor", slug, cfg, diagnostic=(key in _diag_binary))

    for slug, name, key, unit, cat in SENSORS:
        cfg = {"name": name, "state_topic": _STATE_TOPIC}
        # B4: only the two event sensors need the full state as attributes
        # (for last_error_message / last_warning_message). Putting it on every
        # sensor writes the whole ~30-key JSON to the recorder each cycle.
        if key in ("last_error_time", "last_warning_time"):
            # dedicated topic: it only changes when an event actually changes,
            # so attribute churn can't re-trigger notification automations, and
            # the recorder isn't handed ~35 keys twice a cycle.
            cfg["json_attributes_topic"] = _EVENTS_TOPIC
        if key in _NUMERIC or key in _NULLABLE_STR:
            cfg["value_template"] = _num_tmpl(key)     # emits '' when null
        else:
            cfg["value_template"] = "{{ value_json.%s }}" % key
        if unit:
            cfg["unit_of_measurement"] = unit
            cfg["state_class"] = "measurement"
        if cat:
            cfg["entity_category"] = cat
        if key in ("last_error_time", "last_warning_time"):
            cfg["device_class"] = "timestamp"
            cfg["value_template"] = (
                "{{ value_json.%s if value_json.%s else '' }}" % (key, key))
        pub("sensor", slug, cfg, diagnostic=(cat == "diagnostic"))

    # diagnostics are always online so they can explain an outage (B2)
    client.publish(_DIAG_AVAIL_TOPIC, "online", retain=True, qos=1)


def publish_state(client, state):
    """Publish one state + availability, waiting for delivery (QoS 1)."""
    info1 = client.publish(_STATE_TOPIC, json.dumps(state), retain=True, qos=1)
    online = "online" if state.get("healthy") else "offline"
    info2 = client.publish(_AVAIL_TOPIC, online, retain=True, qos=1)
    # Small, STABLE payload for the two event sensors' attributes: only the
    # message and its first-seen timestamp. `count`/`last_seen` are deliberately
    # excluded -- they tick on every repeat, and any change here (attributes
    # included) re-fires a `trigger: state` automation. Identical bytes between
    # cycles mean Home Assistant registers no change at all, so an ongoing
    # condition never produces a repeat notification. Every occurrence is still
    # visible, with its own timestamp, in the add-on Log.
    _stable = ("last_error_message", "last_error_time",
               "last_warning_message", "last_warning_time")
    events_payload = {k: state.get(k) for k in _stable}
    info3 = client.publish(_EVENTS_TOPIC, json.dumps(events_payload),
                           retain=True, qos=1)
    for info in (info1, info2, info3):
        try:
            info.wait_for_publish(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the decision as JSON, publish nothing")
    args = ap.parse_args()

    e, n = POS_LV95
    if not in_radar_grid(e, n):
        raise SystemExit(f"Coordinates E{e:.0f}/N{n:.0f} are outside the Swiss "
                         f"radar grid. Set easting/northing to a location in "
                         f"Switzerland (read them off https://map.geo.admin.ch/).")

    with requests.Session() as s:
        s.headers["User-Agent"] = "swiss-meteo-shade/1.0"
        st = evaluate(session=s)
    print(json.dumps(st, indent=2))
