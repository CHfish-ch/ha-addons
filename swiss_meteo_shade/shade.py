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
from version import VERSION, USER_AGENT

# ---------------------------------------------------------------------------
# CONFIG (standalone defaults; run.py overrides from options.json)
# ---------------------------------------------------------------------------
POS_LV95 = (2665512.66, 1211882.47)   # PLACEHOLDER central Lucerne -- replace
PLZ = "6006"
GUST_LIMIT_KMH = 40.0
GUST_RELEASE_KMH = None                # hysteresis re-extend threshold; None=off
MIN_TEMP_C = None                      # don't deploy shade below this; None=off
OPENMETEO_MODE = "always"              # always | fallback_only | never
PREFER_APP = False                     # official primary; True = app first
LOOKAHEAD_H = 1
RADAR_THRESHOLD_MMH = 0.1
RADAR_TOLERANCE_KM = 1
RADAR_FAIL_SAFE = False               # radar outage -> treat as rain (retract)?
SUN_MODELS = ("sunshine", "irradiance")
SUN_MODEL = "sunshine"                # how "sunny enough" is judged
SUN_MIN_AWNING = 20                   # per-output sunshine thresholds (min/h)
SUN_MIN_BACKUP = 20
SUN_MIN_INDEPENDENT = 20
# Irradiance model. Thresholds are W/m2 on the WINDOW plane (vertical) for all
# three outputs: what matters is sun entering the room, not sun landing on a
# shading device. Only one set of thresholds is consulted -- see SUN_MODEL.
# The awning and the backup blind cover the SAME opening and are a strict
# partition of one decision (the blind substitutes for the awning), so they
# share a threshold. The independent blinds are different windows -- often
# interior glare blinds wanted in weaker sun -- so they keep their own.
IRRADIANCE_MIN_SHADE = 250
IRRADIANCE_MIN_INDEPENDENT = 250
# Every output is judged on the vertical window plane. An awning was once
# judged on a 45 deg plane -- the irradiance on its own fabric -- which is
# anti-correlated with the thing that matters: sun entering a room PEAKS at low
# elevation (885 W/m2 vertical at 10 deg vs 529 at 65), while a 45 deg surface
# peaks near 50 deg. The awning's real limit is geometric, below.
TILT_WINDOW = 90

ALBEDO = 0.20
MIN_SOLAR_ELEVATION = 3.0
IRRADIANCE_SUBSTEPS = 12
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
# `event` entities fire once per NEW event instead of holding a value, so an
# automation needs no timestamp arithmetic. Published non-retained, and Home
# Assistant discards replayed retained messages anyway, so neither a restart
# nor a reconnect can re-announce an old error.
_EVENT_ERROR_TOPIC = f"{MQTT_BASE}/event/error"
_EVENT_WARNING_TOPIC = f"{MQTT_BASE}/event/warning"


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


def _sun_from_irradiance(irr, tilt, threshold):
    """Per-output sun flag from plane-of-array irradiance.

    Returns None when radiation is unavailable, exactly as _sun_at does for a
    missing sunshine forecast: unknown must never read as "not sunny", or a
    failed fetch would silently look like a cloudy day.
    """
    if not irr:
        return None
    poa = irr.get(tilt)
    if not poa or poa.get("total") is None:
        return None
    return poa["total"] >= threshold


def _sun_flags(fc, irr, model):
    """(awning, backup, independent) sun flags for the model actually in use.

    `model` is the ACTIVE model, which is not always the configured one -- see
    the fallback in evaluate().
    """
    if model == "irradiance":
        shade_sun = _sun_from_irradiance(irr, TILT_WINDOW,
                                         IRRADIANCE_MIN_SHADE)
        return (shade_sun, shade_sun,
                _sun_from_irradiance(irr, TILT_WINDOW,
                                     IRRADIANCE_MIN_INDEPENDENT))
    return (_sun_at(fc, SUN_MIN_AWNING),
            _sun_at(fc, SUN_MIN_BACKUP),
            _sun_at(fc, SUN_MIN_INDEPENDENT))


def _events_state():
    """Flatten the ACTIVE error/warning into state fields for the two sensors.

    The state reads "<message> since <when>", or "none" once a clean cycle has
    cleared it -- so a resolved fault stops looking live. History is not lost:
    the `event` entities fire once per new event, so the logbook and any
    notification keep the permanent record. The message and first-seen time
    also travel as attributes on the dedicated events topic, which stays
    byte-identical between cycles while a condition persists."""
    snap = events.snapshot()
    err, warn = snap["last_error"], snap["last_warning"]
    return {
        "active_error": events.state_text("error"),
        "active_warning": events.state_text("warning"),
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
    # Anything not re-reported during this cycle counts as resolved.
    events.start_cycle()
    e, n = POS_LV95
    lat, lon = _lv95_to_wgs84(e, n)

    # 1. radar rain -- failures degrade to rain-unknown, not a dead cycle
    radar.POS_LV95 = POS_LV95
    radar.POS_WGS84 = None
    radar.THRESHOLD_MMH = RADAR_THRESHOLD_MMH
    radar.FORECAST_TOLERANCE_KM = RADAR_TOLERANCE_KM
    rain_source = "radar"
    rain_sources = {}
    radar_why = None            # why the radar is out, for the ONE warning
    try:
        rad = radar.evaluate(session=session)
        if rad.get("stale"):
            # A6: a frame older than the radar module's limit must not be
            # reported as confident rain=False. Treat as unavailable.
            radar_ok = False
            radar_why = f"newest frame {rad.get('age_min')} min old"
        else:
            rain = bool(rad.get("any"))
            radar_ok = True
    except Exception as exc:
        rad, radar_ok = {}, False
        radar_why = f"{type(exc).__name__}: {exc}"

    # ONE warning for one condition. Emitting a "radar is stale" warning here
    # and a "using the forecast" warning below would ALTERNATE, and since only
    # the latest warning is kept, each alternation reads as a new event and
    # re-fires the notification -- the same storm a moving value causes.
    if not radar_ok:
        # Radar down. Rather than guess with a constant, ask the forecast --
        # hourly and point-resolution, so a real downgrade, but a downgrade
        # beats a coin flip. RADAR_FAIL_SAFE remains the last resort for when
        # even that is unavailable.
        fb = None
        try:
            fb = forecast.precipitation_now(PLZ, e, n, session=session)
        except Exception as exc:
            events.warn(f"precipitation fallback failed: {type(exc).__name__}",
                        detail=str(exc))
        if fb and fb.get("mm") is not None:
            rain = fb["mm"] >= RADAR_THRESHOLD_MMH
            rain_source = "forecast"
            rain_sources = fb["sources"]
            events.warn("radar unavailable -> rain taken from the forecast "
                        "(hourly, point resolution)",
                        detail=f"{radar_why}; {fb['mm']:.2f} mm from "
                               f"{', '.join(sorted(fb['sources']))}")
        else:
            rain = bool(RADAR_FAIL_SAFE)
            rain_source = "assumed"
            events.warn("radar AND precipitation forecast unavailable -> "
                        f"rain assumed {'wet' if RADAR_FAIL_SAFE else 'dry'} "
                        "(radar_fail_safe)", detail=radar_why)

    # 2. forecast gust + sunshine + temp
    fc = forecast.gather(PLZ, e, n, lat, lon, openmeteo_mode=OPENMETEO_MODE,
                         prefer_app=PREFER_APP, lookahead_h=LOOKAHEAD_H,
                         session=session, need_temp=MIN_TEMP_C is not None)

    # 2b. irradiance, only when that model is selected -- it is another ~60 MB
    # of radiation files, so it is never fetched for the sunshine model.
    irr = None
    sun_model_active = SUN_MODEL
    if SUN_MODEL == "irradiance":
        try:
            irr = forecast.irradiance_now(
                e, n, lat, lon, session=session,
                tilts=(TILT_WINDOW,), albedo=ALBEDO,
                min_elevation=MIN_SOLAR_ELEVATION,
                substeps=IRRADIANCE_SUBSTEPS)
        except Exception as exc:
            events.warn(f"irradiance unavailable: {type(exc).__name__}",
                        detail=str(exc))
        if irr is None:
            # Fall back to the sunshine model rather than losing the sun
            # signal outright -- a radiation outage should not stop the shade
            # working when a perfectly good sunshine forecast is in hand. The
            # switch is never SILENT: it warns (Last warning + the Warning
            # event entity) and `Sun model` reports `sunshine_fallback`, so
            # the entities still explain themselves.
            sun_model_active = "sunshine_fallback"
            events.warn("radiation forecast unavailable -> falling back to "
                        "the sunshine model (thresholds sun_min_*)")

    # 3. decision (with hysteresis + temp gate)
    _sun_a, _sun_b, _sun_i = _sun_flags(fc, irr, sun_model_active)
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
        sun_awning=_sun_a, sun_backup=_sun_b, sun_independent=_sun_i)

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
        "rain_source": rain_source,
        "rain_forecast_mm": (round(max(rain_sources.values()), 2)
                             if rain_sources else None),
        "sun": dec["sun"],
        "wind_high": dec["wind_high"],
        "gust_kmh": fc["gust_kmh"],
        "gust_unknown": dec["gust_unknown"],
        "gust_limit_kmh": GUST_LIMIT_KMH,
        "sunshine_minutes": fc["sunshine_minutes"],
        "temp_c": fc.get("temp_c"),
        "forecast_unavailable": dec["forecast_unavailable"],
        "temp_blocks": dec["temp_blocks"],
        "sun_model": sun_model_active,
        "irradiance_window": (round(irr[TILT_WINDOW]["total"])
                              if irr and irr.get(TILT_WINDOW) else None),
        "solar_elevation": (round(irr["elevation"], 1)
                            if irr and irr.get("elevation") is not None
                            else None),
        "ghi": round(irr["ghi"]) if irr and irr.get("ghi") is not None else None,
        "diffuse_fraction": (round(100 * irr["diffuse_fraction"])
                             if irr and irr.get("diffuse_fraction") is not None
                             else None),
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
    # Irradiance model. The two POA sensors are weather INPUTS to the decision
    # (like Forecast sunshine), so they are primary; GHI and diffuse fraction
    # explain where they came from, so they are diagnostic. All four read
    # Unknown under the sunshine model, which is the honest representation --
    # they are not being computed at all.
    ("irradiance_window", "Irradiance (window)", "irradiance_window",
     "W/m²", None),
    ("solar_elevation", "Solar elevation", "solar_elevation", "°",
     "diagnostic"),
    ("ghi", "Global radiation", "ghi", "W/m²", "diagnostic"),
    ("diffuse_fraction", "Diffuse fraction", "diffuse_fraction", "%",
     "diagnostic"),
    ("sun_model", "Sun model", "sun_model", None, "diagnostic"),
    ("rain_source", "Rain source", "rain_source", None, "diagnostic"),
    ("rain_forecast_mm", "Rain (forecast fallback)",
     "rain_forecast_mm", "mm", "diagnostic"),
    ("forecast_source", "Forecast source", "forecast_source", None, "diagnostic"),
    ("radar_age_min", "Radar age", "radar_age_min", "min", "diagnostic"),
    ("reason", "Shade reason", "reason", None, "diagnostic"),
    ("active_error", "Active error", "active_error", None, "diagnostic"),
    ("active_warning", "Active warning", "active_warning", None, "diagnostic"),
    # Only the ACTIVE primary source has a value: official and app are a
    # primary/fallback pair, never queried in parallel, so whichever is not in
    # use reads Unknown. Open-Meteo is populated whenever openmeteo_mode is
    # 'always', or under 'fallback_only' when the MeteoSwiss gust failed.
    ("gust_official", "Gust (MeteoSwiss official)", "gust_official", "km/h",
     "diagnostic"),
    ("gust_app", "Gust (MeteoSwiss app, fallback)", "gust_app", "km/h",
     "diagnostic"),
    ("gust_openmeteo", "Gust (Open-Meteo ICON)", "gust_openmeteo", "km/h",
     "diagnostic"),
]

# (slug, friendly, topic, event_type) -- the `event` platform. Diagnostic, and
# deliberately WITHOUT expire_after: events are sporadic by nature, and an
# entity that goes unavailable between them would break automations that
# trigger on it.
EVENT_ENTITIES = [
    ("event_error", "Error", _EVENT_ERROR_TOPIC, "error"),
    ("event_warning", "Warning", _EVENT_WARNING_TOPIC, "warning"),
]

# numeric sensors need value_template guards so JSON null doesn't become "None"
_NULLABLE_STR = {"forecast_source", "sun_model", "rain_source"}   # B8: else Jinja renders the string "None"
_NUMERIC = {"gust_kmh", "sunshine_minutes", "temp_c", "radar_age_min",
            "gust_official", "gust_app", "gust_openmeteo",
            "irradiance_window", "solar_elevation", "ghi",
            "rain_forecast_mm",
            "diffuse_fraction"}


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
                                  "sw_version": VERSION})   # B7
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
        if key in ("active_error", "active_warning"):
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
        # No timestamp device_class any more: the state is now
        # "<message> since <when>" rather than a bare timestamp, so HA must
        # render it verbatim.
        pub("sensor", slug, cfg, diagnostic=(cat == "diagnostic"))

    for slug, name, topic, etype in EVENT_ENTITIES:
        # No expire_after and no state value template: the `event` platform
        # takes the payload's event_type itself, and every other key in the
        # payload becomes an attribute (that is where `message` arrives).
        cfg = {"name": name, "state_topic": topic, "event_types": [etype],
               "entity_category": "diagnostic",
               "availability_topic": _DIAG_AVAIL_TOPIC,
               "unique_id": f"sms_{slug}", "device": DEVICE,
               "origin": {"name": "Swiss Meteo Shade", "sw_version": VERSION}}
        client.publish(f"homeassistant/event/sms_{slug}/config",
                       json.dumps(cfg), retain=True, qos=1)

    # diagnostics are always online so they can explain an outage (B2)
    client.publish(_DIAG_AVAIL_TOPIC, "online", retain=True, qos=1)


_event_seen = {"error": None, "warning": None}
_event_seeded = False

# (event_type, state key holding its timestamp, state key holding its message,
#  topic to fire on)
_EVENT_FIELDS = (
    ("error", "last_error_time", "last_error_message", _EVENT_ERROR_TOPIC),
    ("warning", "last_warning_time", "last_warning_message",
     _EVENT_WARNING_TOPIC),
)


def _publish_new_events(client, state):
    """Fire the `event` entities, but only for genuinely NEW events.

    The timestamp is when the CURRENT message first appeared, so an unchanged
    timestamp means the same condition is merely recurring and must not fire
    again -- a stale radar lasting hours notifies once, as with the sensors.

    The first publish after a start only SEEDS the tracker and fires nothing:
    events.py restores the last error/warning from /data, and announcing those
    would re-report something from before this run every time the add-on
    restarts. That is the exact trap the sensor-based automation needed a
    hand-written template condition to avoid.
    """
    global _event_seeded
    if not _event_seeded:
        for kind, tkey, _mkey, _topic in _EVENT_FIELDS:
            _event_seen[kind] = state.get(tkey)
        _event_seeded = True
        return
    for kind, tkey, mkey, topic in _EVENT_FIELDS:
        stamp = state.get(tkey)
        if not stamp or stamp == _event_seen[kind]:
            continue
        _event_seen[kind] = stamp
        client.publish(topic, json.dumps({
            "event_type": kind,
            "message": state.get(mkey),
            "time": stamp,
        }), retain=False, qos=1)      # never retained: no replay on reconnect


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
    # after the state is on the wire, so an automation reacting to the event
    # sees the matching sensor values already updated
    _publish_new_events(client, state)


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
        s.headers["User-Agent"] = USER_AGENT
        st = evaluate(session=s)
    print(json.dumps(st, indent=2))
