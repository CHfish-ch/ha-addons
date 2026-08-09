"""Tests for choosing between the sunshine and irradiance sun models.

Four things must hold and none of them raises an exception when broken: the
unselected model's data must not be fetched, each output must be judged on the
tilt that matches what it physically is, a radiation outage must fall back to
sunshine ANNOUNCED rather than silently, and the fallback must not invent a
signal when sunshine is missing too.
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
    shade.SUN_MODEL = "sunshine"
    shade.SUN_MIN_AWNING = shade.SUN_MIN_BACKUP = 20
    shade.SUN_MIN_INDEPENDENT = 20
    shade.IRRADIANCE_MIN_AWNING = 250
    shade.IRRADIANCE_MIN_BACKUP = 250
    shade.IRRADIANCE_MIN_INDEPENDENT = 250
    shade.TILT_AWNING = 45
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


def test_missing_radiation_falls_back_to_the_sunshine_model():
    """A radiation outage must not stop the shade working when a perfectly
    good sunshine forecast is in hand."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(None)
    st = shade.evaluate()
    assert st["sun"] is True, "should have used the 45 min/h sunshine forecast"
    assert st["awning_extend"] is True
    assert st["forecast_unavailable"] is False, \
        "the sun signal is known -- it just came from the other model"


def test_the_fallback_is_announced_not_silent():
    """Three channels have to show it, or the entities stop explaining
    themselves: the model sensor, a warning, and the Warning event entity."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(None)
    before = events.snapshot()["last_warning"]
    st = shade.evaluate()
    after = events.snapshot()["last_warning"]

    assert st["sun_model"] == "sunshine_fallback", \
        f"Sun model must reveal the fallback, got {st['sun_model']!r}"
    assert (before or {}).get("time") != (after or {}).get("time"), \
        "falling back must record a warning"
    assert "radiation" in after["message"].lower()
    # and the irradiance sensors must not invent values
    assert st["irradiance_awning"] is None and st["irradiance_wall"] is None


def test_fallback_uses_the_sunshine_thresholds_not_the_irradiance_ones():
    shade.SUN_MODEL = "irradiance"
    shade.SUN_MIN_AWNING = 60          # 45 min/h forecast is now NOT enough
    _set_irradiance(None)
    st = shade.evaluate()
    assert st["awning_extend"] is False, \
        "the fallback must honour sun_min_*, not irradiance_min_*"


def test_sun_is_still_unknown_when_both_models_have_no_data():
    """Fallback is not a licence to invent a signal: if sunshine is missing
    too, the sun stays unknown and the shade is kept in."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(None)

    def no_sunshine(*a, **k):
        CALLS.append("gather")
        return {"forecast_source": "official",
                "gust_sources": {"official": 5.0}, "gust_kmh": 5.0,
                "sunshine": None, "sunshine_minutes": None, "temp_c": 20.0,
                "on_backup": False, "openmeteo_ok": None}
    forecast.gather = no_sunshine

    st = shade.evaluate()
    assert st["forecast_unavailable"] is True
    assert st["awning_extend"] is False


def test_awning_pitch_is_configurable_and_blinds_stay_vertical():
    """Awning pitch varies by installation (5-35 deg typical); a blind is
    vertical by definition and must not follow it."""
    shade.SUN_MODEL = "irradiance"
    shade.TILT_AWNING = 15
    asked = {}

    def fake(*a, **k):
        asked["tilts"] = set(k["tilts"])
        return {15: {"total": 600.0}, 90: {"total": 100.0},
                "ghi": 800.0, "dhi": 100.0, "diffuse_fraction": 0.15}
    forecast.irradiance_now = fake

    st = shade.evaluate()
    assert asked["tilts"] == {15, 90}, \
        f"must evaluate the configured pitch and vertical: {asked['tilts']}"
    assert st["awning_extend"] is True            # 600 on the 15 deg plane
    assert st["independent_blinds_close"] is False  # 100 on the vertical one
    assert shade.TILT_BLIND == 90, "blind tilt must not be configurable"


def test_vertical_awning_pitch_does_not_break_the_tilt_set():
    """awning_tilt=90 collapses both planes onto one; must not duplicate."""
    shade.SUN_MODEL = "irradiance"
    shade.TILT_AWNING = 90
    asked = {}

    def fake(*a, **k):
        asked["tilts"] = tuple(k["tilts"])
        return {90: {"total": 600.0}, "ghi": 800.0, "dhi": 100.0,
                "diffuse_fraction": 0.15}
    forecast.irradiance_now = fake

    st = shade.evaluate()
    assert asked["tilts"] == (90,), f"expected one plane, got {asked['tilts']}"
    assert st["awning_extend"] is True


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
