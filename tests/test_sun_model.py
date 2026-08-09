"""Tests for choosing between the sunshine and irradiance sun models.

Three things must hold and none of them raises an exception when broken:
the unselected model's data must not be fetched, each output must be judged
on the tilt that matches what it physically is, and a missing radiation
forecast must read as UNKNOWN rather than quietly reverting to sunshine.
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import forecast     # noqa: E402
import radar        # noqa: E402
import shade        # noqa: E402

CALLS = []


def setup_function():
    CALLS.clear()
    shade.SUN_MODEL = "sunshine"
    shade.SUN_MIN_AWNING = shade.SUN_MIN_BACKUP = 20
    shade.SUN_MIN_INDEPENDENT = 20
    shade.IRRADIANCE_MIN_AWNING = 250
    shade.IRRADIANCE_MIN_BACKUP = 250
    shade.IRRADIANCE_MIN_INDEPENDENT = 250
    shade.MIN_TEMP_C = None
    shade.GUST_RELEASE_KMH = None

    radar.evaluate = lambda session=None: {
        "any": False, "stale": False, "age_min": 1.0,
        "radar_time": "2026-08-09T12:00:00+00:00"}

    def fake_gather(*a, **k):
        CALLS.append("gather")
        return {"forecast_source": "official", "gust_sources": {"official": 5.0},
                "gust_kmh": 5.0, "sunshine": True, "sunshine_minutes": 45,
                "temp_c": 20.0, "on_backup": False, "openmeteo_ok": None}
    forecast.gather = fake_gather


def _irr(awning_total, wall_total):
    return {
        45: {"total": awning_total, "direct": awning_total, "diffuse": 0.0,
             "ground": 0.0, "cos_incidence": 1.0},
        90: {"total": wall_total, "direct": wall_total, "diffuse": 0.0,
             "ground": 0.0, "cos_incidence": 1.0},
        "ghi": 800.0, "dhi": 100.0, "dni_hour": 900.0, "hour_sine": 0.5,
        "elevation": 40.0, "azimuth": 200.0, "diffuse_fraction": 0.15,
        "hour_end": "2026-08-09T13:00:00+00:00"}


def _set_irradiance(result):
    def fake(*a, **k):
        CALLS.append("irradiance")
        return result
    forecast.irradiance_now = fake


def test_sunshine_model_does_not_fetch_radiation():
    """Radiation is another ~60 MB; the unselected model must cost nothing."""
    _set_irradiance(_irr(900, 900))
    st = shade.evaluate()
    assert "irradiance" not in CALLS, "radiation fetched under the sunshine model"
    assert st["sun"] is True                      # 45 min/h >= 20
    assert st["irradiance_awning"] is None


def test_irradiance_model_uses_poa_not_minutes():
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(_irr(awning_total=100, wall_total=100))   # below threshold
    st = shade.evaluate()
    assert "irradiance" in CALLS
    # sunshine says 45 min/h (sunny); irradiance says 100 W/m2 (not sunny)
    assert st["sun"] is False, "the sunshine value must not leak into the model"
    assert st["recommendation"] == "none"


def test_each_output_is_judged_on_its_own_tilt():
    """Awning on the 45 plane, both blinds on the vertical one."""
    shade.SUN_MODEL = "irradiance"
    # awning plane bright, vertical plane dim -> only the awning qualifies
    _set_irradiance(_irr(awning_total=600, wall_total=100))
    st = shade.evaluate()
    assert st["awning_extend"] is True
    assert st["independent_blinds_close"] is False, \
        "blinds must read the vertical tilt, not the awning's"


def test_thresholds_are_independent_per_output():
    shade.SUN_MODEL = "irradiance"
    shade.IRRADIANCE_MIN_INDEPENDENT = 50      # interior blinds, weaker sun
    _set_irradiance(_irr(awning_total=100, wall_total=100))
    st = shade.evaluate()
    assert st["independent_blinds_close"] is True
    assert st["awning_extend"] is False


def test_missing_radiation_is_unknown_not_a_silent_fallback():
    """The whole point: a failed fetch must not look like a cloudy day, and
    must not quietly revert to the sunshine model that WOULD have said sunny."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(None)
    st = shade.evaluate()
    assert st["forecast_unavailable"] is True, "unknown sun must be flagged"
    assert st["awning_extend"] is False, "unknown sun must keep the awning in"
    assert st["irradiance_awning"] is None
    # sunshine said 45 min/h; if that had leaked through, sun would be True
    assert st["sun"] is not True, "silently fell back to the sunshine model"


def test_irradiance_values_are_published_for_diagnostics():
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(_irr(awning_total=612.4, wall_total=388.6))
    st = shade.evaluate()
    assert st["irradiance_awning"] == 612
    assert st["irradiance_wall"] == 389
    assert st["ghi"] == 800
    assert st["diffuse_fraction"] == 15          # 0.15 -> 15 %
    assert st["sun_model"] == "irradiance"


def test_sun_model_is_reported_in_state():
    st = shade.evaluate()
    assert st["sun_model"] == "sunshine"


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
