#!/usr/bin/env python3
"""Add-on entrypoint for Swiss Meteo Shade.

- Reads /data/options.json (tolerant of missing keys via .get()).
- Holds ONE persistent MQTT connection so the last-will works and discovery is
  published once; state is published each cycle and expires in HA if it stalls.
- Reuses ONE requests.Session across cycles (TCP/TLS reuse).
- Re-fetches MQTT credentials on reconnect (handles broker restarts).
"""

import json
import os
import sys
import time
import signal
import traceback

_stop = False


def _on_sigterm(signum, frame):
    global _stop
    _stop = True

# Exit for a Supervisor Watchdog restart after this many consecutive failures.
WATCHDOG_EXIT_FAILS = 10

import requests

sys.path.insert(0, "/app")
import events     # noqa: E402
import shade      # noqa: E402

_WIND_STATE_FILE = "/data/wind_high.json"


def _log(msg):
    from datetime import datetime, timezone
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def _load_wind_high():
    try:
        with open(_WIND_STATE_FILE) as fh:
            return bool(json.load(fh).get("wind_high", False))
    except (OSError, ValueError):
        return False


def _save_wind_high(value):
    try:
        with open(_WIND_STATE_FILE, "w") as fh:
            json.dump({"wind_high": bool(value)}, fh)
    except OSError:
        pass


def load_options():
    try:
        with open("/data/options.json") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _num(value, default, cast, name):
    """Cast an option to float/int, falling back to default on bad input with a
    warning -- a typo in the options UI must not crash the container on boot."""
    if value in (None, ""):
        return default
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")   # Swiss decimal comma
        return cast(value)
    except (ValueError, TypeError):
        print(f"WARNING: option {name}={value!r} is not a valid "
              f"{cast.__name__}; using default {default}", flush=True)
        return default


def _opt_or_none(value, cast, name):
    """Like _num but empty/None means 'feature off' (returns None)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return cast(value)
    except (ValueError, TypeError):
        print(f"WARNING: option {name}={value!r} invalid; feature disabled",
              flush=True)
        return None


def apply_options(opts):
    g = opts.get
    shade.POS_LV95 = (_num(g("easting"), 2665512.66, float, "easting"),
                      _num(g("northing"), 1211882.47, float, "northing"))
    shade.PLZ = str(g("plz", "6006"))
    shade.GUST_LIMIT_KMH = _num(g("gust_limit_kmh"), 40.0, float, "gust_limit_kmh")
    shade.GUST_RELEASE_KMH = _opt_or_none(g("gust_release_kmh"), float,
                                          "gust_release_kmh")
    if (shade.GUST_RELEASE_KMH is not None
            and shade.GUST_RELEASE_KMH > shade.GUST_LIMIT_KMH):
        print(f"WARNING: gust_release_kmh ({shade.GUST_RELEASE_KMH}) > "
              f"gust_limit_kmh ({shade.GUST_LIMIT_KMH}); disabling hysteresis",
              flush=True)
        shade.GUST_RELEASE_KMH = None
    # A5: a temperature of 0 C is a valid threshold, so min_temp_c must NOT use
    # _opt_or_none (which treats 0 as "off"). Only None / "" disable it.
    _mt = g("min_temp_c")
    shade.MIN_TEMP_C = _num(_mt, None, float, "min_temp_c") if _mt not in (None, "") else None
    shade.USE_OPENMETEO = bool(g("use_openmeteo", True))
    shade.PREFER_APP = bool(g("prefer_app_forecast", False))
    shade.LOOKAHEAD_H = _num(g("lookahead_hours"), 2, int, "lookahead_hours")
    shade.RADAR_THRESHOLD_MMH = _num(g("radar_threshold_mmh"), 0.1, float,
                                     "radar_threshold_mmh")
    shade.RADAR_TOLERANCE_KM = _num(g("radar_tolerance_km"), 1, int,
                                    "radar_tolerance_km")
    shade.INTERVAL_SECONDS = _num(g("interval_seconds"), 300, int,
                                  "interval_seconds")
    shade.RADAR_FAIL_SAFE = bool(g("radar_fail_safe", False))
    import forecast
    forecast.FORECAST_MAX_CACHE_MINUTES = _num(g("forecast_max_cache_minutes"),
                                               60, int, "forecast_max_cache_minutes")
    shade.SUN_MIN_AWNING = _num(g("sun_min_awning"), 20, int, "sun_min_awning")
    shade.SUN_MIN_BACKUP = _num(g("sun_min_backup"), 20, int, "sun_min_backup")
    shade.SUN_MIN_INDEPENDENT = _num(g("sun_min_independent"), 20, int,
                                     "sun_min_independent")


def mqtt_credentials():
    """Fetch broker host/port/user/pass (and ssl flag) from the Supervisor."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise SystemExit("SUPERVISOR_TOKEN missing -- not running as an add-on?")
    r = requests.get("http://supervisor/services/mqtt",
                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json()["data"]


def make_client():
    import paho.mqtt.client as mqtt
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    return c


_tls_done = False


def _apply_credentials(client):
    """Fetch fresh broker credentials from the Supervisor and set them.

    B5: paho raises ValueError('SSL/TLS has already been configured') on a
    second tls_set() for the same client, so on a TLS broker every credential
    refresh would fail -- exactly the path B6 exists to support. Guard it."""
    global _tls_done
    mq = mqtt_credentials()
    if mq.get("username"):
        client.username_pw_set(mq["username"], mq.get("password"))
    if mq.get("ssl") and not _tls_done:
        client.tls_set()
        _tls_done = True
    return mq["host"], int(mq["port"])


# B6: the disconnect callback must NOT block paho's network thread with a
# 20 s Supervisor request. It only sets a flag; the main loop does the actual
# credential refresh between cycles.
_creds_stale = False


def _on_disconnect(client, userdata, *args):
    """V2/V1-compatible on_disconnect. On an UNEXPECTED drop, flag the main loop
    to re-fetch credentials (they may have rotated). rc == 0 is a clean,
    intentional disconnect and is ignored."""
    global _creds_stale
    rc = args[-2] if len(args) >= 2 else (args[0] if args else 0)
    try:
        clean = (int(rc) == 0)
    except (TypeError, ValueError):
        clean = False
    if not clean:
        _creds_stale = True             # main loop refreshes, off the paho thread


def _on_connect(client, userdata, flags, reason_code, properties=None):
    """B6: (re-)announce discovery on EVERY successful connect, and subscribe to
    Home Assistant's birth topic. Retained discovery survives a broker restart
    only while the broker's retained store does; and HA itself republishes
    `homeassistant/status: online` after a restart, which is the convention for
    integrations to re-announce. Publishing again is idempotent -- the topics
    are retained and the payloads identical."""
    try:
        shade.announce_discovery(client)
        client.subscribe("homeassistant/status", qos=1)
    except Exception as exc:
        print(f"discovery announce failed: {exc}", flush=True)


def _on_message(client, userdata, msg):
    """Re-announce when Home Assistant comes back online."""
    try:
        if msg.topic == "homeassistant/status" and \
                msg.payload.decode().strip().lower() == "online":
            print("Home Assistant back online -> re-announcing discovery",
                  flush=True)
            shade.announce_discovery(client)
    except Exception as exc:
        print(f"birth-message handling failed: {exc}", flush=True)


def connect(client):
    """(Re)fetch credentials and open a persistent connection with LWT."""
    host, port = _apply_credentials(client)
    client.will_set(shade._AVAIL_TOPIC, "offline", retain=True, qos=1)
    client.on_disconnect = _on_disconnect   # re-fetch creds on unexpected drop
    client.on_connect = _on_connect         # B6: re-announce on every connect
    client.on_message = _on_message         # B6: react to HA birth message
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.connect(host, port, keepalive=max(60, shade.INTERVAL_SECONDS // 2))
    client.loop_start()
    return host, port


def main():
    global _creds_stale
    # register signal handlers before any slow startup work (the Supervisor
    # credential call has a 20 s timeout) so an early stop is still clean
    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)
    opts = load_options()
    apply_options(opts)
    # persist diagnostic events across restarts if /data is available
    if os.path.isdir("/data"):
        events.set_persist_path("/data/last_events.json")

    e, n = shade.POS_LV95
    if not shade.in_radar_grid(e, n):
        raise SystemExit(
            f"Coordinates E{e:.0f}/N{n:.0f} are outside the Swiss radar grid. "
            f"Set easting/northing to a location in Switzerland "
            f"(read them off https://map.geo.admin.ch/).")

    session = requests.Session()
    session.headers["User-Agent"] = "swiss-meteo-shade/1.0"

    client = make_client()
    host, port = connect(client)
    # discovery is announced by _on_connect, which fires on this connect and
    # on every reconnect -- no separate one-shot call needed (B6).
    print(f"Swiss Meteo Shade | broker {host}:{port} | "
          f"LV95 {e:.0f}/{n:.0f} | plz {shade.PLZ} | "
          f"gust limit {shade.GUST_LIMIT_KMH} km/h"
          + (f" (release {shade.GUST_RELEASE_KMH})" if shade.GUST_RELEASE_KMH else "")
          + (f" | min_temp {shade.MIN_TEMP_C} C" if shade.MIN_TEMP_C is not None else "")
          + f" | every {shade.INTERVAL_SECONDS}s", flush=True)

    # A2: hysteresis state persists across restarts so a mid-storm restart does
    # not drop back to the extend side of the band.
    prev_wind_high = _load_wind_high()
    backoff = 0
    fails = 0
    while not _stop:
        # B6: refresh credentials here (main loop) if the paho callback flagged
        # a disconnect, rather than blocking paho's network thread.
        if _creds_stale:
            _creds_stale = False
            try:
                _apply_credentials(client)
                print("refreshed MQTT credentials after disconnect", flush=True)
            except Exception as exc:
                print(f"credential refresh failed: {exc}", flush=True)
        try:
            state = shade.evaluate(session=session,
                                   prev_wind_high=prev_wind_high)
            shade.publish_state(client, state)
            if state["wind_high"] != prev_wind_high:
                prev_wind_high = state["wind_high"]
                _save_wind_high(prev_wind_high)
            _log(json.dumps({k: state[k] for k in
                             ("recommendation", "retract", "gust_kmh", "sun",
                              "rain", "temp_c", "forecast_source", "on_backup",
                              "forecast_unavailable", "radar_ok", "healthy")}))
            backoff = 0
            fails = 0
        except Exception:
            traceback.print_exc()
            fails += 1
            if fails >= 3:                       # persistent failure -> go offline
                try:
                    client.publish(shade._AVAIL_TOPIC, "offline",
                                   retain=True, qos=1)
                except Exception:
                    pass
            # After many consecutive failures, exit non-zero so the Supervisor
            # Watchdog fully restarts the container -- a fresh process clears
            # stuck network/MQTT/memory state a continuous loop cannot. This is
            # only reached if failures persist across ~WATCHDOG_EXIT cycles.
            if fails >= WATCHDOG_EXIT_FAILS:
                print(f"{fails} consecutive failures -> exiting for Supervisor "
                      f"Watchdog restart", flush=True)
                try:
                    client.publish(shade._AVAIL_TOPIC, "offline",
                                   retain=True, qos=1)
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
                sys.exit(1)
            backoff = min(backoff + 60, 600)
        # interruptible sleep so SIGTERM is honoured promptly
        slept = 0
        while slept < shade.INTERVAL_SECONDS + backoff and not _stop:
            time.sleep(min(2, shade.INTERVAL_SECONDS + backoff - slept))
            slept += 2

    # clean shutdown (C7)
    print("shutting down; publishing offline", flush=True)
    try:
        client.publish(shade._AVAIL_TOPIC, "offline", retain=True, qos=1)
        client.loop_stop()
        client.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
