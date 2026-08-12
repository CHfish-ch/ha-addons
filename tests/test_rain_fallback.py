"""Tests for the rain signal when the radar is unavailable.

MeteoSwiss stopped publishing radar frames for four hours on 2026-08-12. Before
this, an outage left `radar_fail_safe` -- a constant -- as the only rain signal.
Now the forecast stands in, which is a real downgrade (hourly, point
resolution, predicted rather than observed) but beats a coin flip.

The behaviours that matter, none of which raise when broken:
  * a working radar must never consult the forecast;
  * the fallback takes the MAX across series, because for an awning a miss
    (soaked) costs more than a false alarm (an hour of lost shade);
  * radar_fail_safe still applies when even the forecast is unavailable;
  * the substitution is reported, never silent.
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import events       # noqa: E402
import forecast     # noqa: E402
import radar        # noqa: E402
import shade        # noqa: E402

CALLS = []


def setup_function():
    CALLS.clear()
    # Dedup is global and persists between tests: without this, a test that
    # emits the same warning as an earlier one sees no timestamp change and
    # reads that as a failure to announce.
    events._last["error"] = None
    events._last["warning"] = None
    shade.SUN_MODEL = "sunshine"
    shade.SUN_MIN_AWNING = shade.SUN_MIN_BACKUP = shade.SUN_MIN_INDEPENDENT = 20
    shade.MIN_TEMP_C = None
    shade.GUST_RELEASE_KMH = None
    shade.RADAR_FAIL_SAFE = False
    shade.RADAR_THRESHOLD_MMH = 0.1

    forecast.gather = lambda *a, **k: {
        "forecast_source": "official", "gust_sources": {"official": 5.0},
        "gust_kmh": 5.0, "sunshine": True, "sunshine_minutes": 45,
        "temp_c": 20.0, "on_backup": False, "openmeteo_ok": None}


def _radar(ok=True, raining=False):
    def fake(session=None):
        CALLS.append("radar")
        if ok:
            return {"any": raining, "stale": False, "age_min": 2.0,
                    "radar_time": "2026-08-12T12:00:00+00:00"}
        return {"any": False, "stale": True, "age_min": 250.7,
                "radar_time": "2026-08-12T09:30:00+00:00"}
    radar.evaluate = fake


def _precip(result):
    def fake(*a, **k):
        CALLS.append("precip")
        return result
    forecast.precipitation_now = fake


def test_working_radar_never_consults_the_forecast():
    """The forecast is a downgrade; it must not dilute a good radar."""
    _radar(ok=True, raining=True)
    _precip({"mm": 0.0, "sources": {"official": 0.0}})
    st = shade.evaluate()
    assert "precip" not in CALLS, "forecast queried while the radar was fine"
    assert st["rain"] is True and st["rain_source"] == "radar"


def test_radar_outage_takes_rain_from_the_forecast():
    _radar(ok=False)
    _precip({"mm": 0.8, "sources": {"official": 0.8}})
    st = shade.evaluate()
    assert st["rain"] is True
    assert st["rain_source"] == "forecast"
    assert st["retract"] is True, "forecast rain must drive the retract"
    assert st["radar_ok"] is False, "radar is still honestly reported as down"


def test_fallback_takes_the_max_across_series():
    """Cautious direction, as with the gust sources. The two app arrays are
    known to miss DIFFERENT events, so either alone is worse than both."""
    _radar(ok=False)
    _precip({"mm": 0.9, "sources": {"official": 0.0, "app_10min": 0.9,
                                    "app_hourly": 0.0}})
    st = shade.evaluate()
    assert st["rain"] is True, \
        "one series seeing rain must be enough -- a miss costs more than a false alarm"
    assert st["rain_forecast_mm"] == 0.9


def test_dry_forecast_during_an_outage_leaves_the_awning_out():
    _radar(ok=False)
    _precip({"mm": 0.0, "sources": {"official": 0.0}})
    st = shade.evaluate()
    assert st["rain"] is False
    assert st["rain_source"] == "forecast"
    assert st["awning_extend"] is True


def test_fail_safe_still_applies_when_the_forecast_is_gone_too():
    for fail_safe, expect in ((False, False), (True, True)):
        setup_function()
        shade.RADAR_FAIL_SAFE = fail_safe
        _radar(ok=False)
        _precip(None)
        st = shade.evaluate()
        assert st["rain"] is expect, f"radar_fail_safe={fail_safe} not honoured"
        assert st["rain_source"] == "assumed"


def test_the_substitution_is_announced():
    _radar(ok=False)
    _precip({"mm": 0.4, "sources": {"official": 0.4}})
    before = events.snapshot()["last_warning"]
    st = shade.evaluate()
    after = events.snapshot()["last_warning"]
    assert (before or {}).get("time") != (after or {}).get("time")
    assert "forecast" in after["message"].lower()
    assert st["rain_source"] == "forecast", "and it is visible on an entity"


def test_a_persistent_outage_warns_once_not_every_cycle():
    """Regression against the 2026-08-12 notification storm: the warning must
    dedupe, so the varying millimetres belong in `detail`, not the message."""
    _radar(ok=False)
    stamps = set()
    for mm in (0.4, 0.5, 0.6, 0.7):
        _precip({"mm": mm, "sources": {"official": mm}})
        shade.evaluate()
        stamps.add(events.snapshot()["last_warning"]["time"])
    assert len(stamps) == 1, \
        f"a sustained outage re-fired {len(stamps)} times"


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
