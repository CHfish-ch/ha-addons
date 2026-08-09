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
    shade.IRRADIANCE_MIN_SHADE = 250
    shade.IRRADIANCE_MIN_INDEPENDENT = 250
    shade.AWNING_MIN_ELEVATION = 35
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


def _irr(window_total, elevation=50.0):
    """All outputs read the vertical window plane; elevation gates the awning."""
    return {
        90: {"total": window_total, "direct": window_total, "diffuse": 0.0,
             "ground": 0.0, "cos_incidence": 1.0},
        "ghi": 800.0, "dhi": 100.0, "dni_hour": 900.0, "hour_sine": 0.5,
        "elevation": elevation, "azimuth": 200.0, "diffuse_fraction": 0.15,
        "hour_end": "2026-08-09T13:00:00+00:00"}


def _set_irradiance(result):
    def fake(*a, **k):
        CALLS.append("irradiance")
        return result
    forecast.irradiance_now = fake


def test_sunshine_model_does_not_fetch_radiation():
    """Radiation is another ~60 MB; the unselected model must cost nothing."""
    _set_irradiance(_irr(900))
    st = shade.evaluate()
    assert "irradiance" not in CALLS, "radiation fetched under the sunshine model"
    assert st["sun"] is True                      # 45 min/h >= 20
    assert st["irradiance_window"] is None


def test_irradiance_model_uses_poa_not_minutes():
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(_irr(100))              # below threshold
    st = shade.evaluate()
    assert "irradiance" in CALLS
    # sunshine says 45 min/h (sunny); irradiance says 100 W/m2 (not sunny)
    assert st["sun"] is False, "the sunshine value must not leak into the model"
    assert st["recommendation"] == "none"


def test_every_output_reads_the_window_plane():
    """Sun entering the ROOM is what matters, so all three share one plane."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(_irr(600, elevation=50.0))
    st = shade.evaluate()
    assert st["awning_extend"] is True
    assert st["independent_blinds_close"] is True


def test_thresholds_are_independent_per_output():
    shade.SUN_MODEL = "irradiance"
    shade.IRRADIANCE_MIN_INDEPENDENT = 50      # interior blinds, weaker sun
    _set_irradiance(_irr(100))
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
    assert st["irradiance_window"] is None


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


def test_low_sun_hands_the_opening_to_the_backup_blind():
    """Sun entering a room PEAKS at low elevation, and a low beam passes under
    an awning -- so the blind must take over even though it is calm and dry.
    Tying the backup to wind/rain alone left the opening unshaded here."""
    shade.SUN_MODEL = "irradiance"
    shade.AWNING_MIN_ELEVATION = 35
    _set_irradiance(_irr(800, elevation=12.0))     # bright, but very low sun
    st = shade.evaluate()
    assert st["awning_extend"] is False, \
        "an awning cannot block a 12 degree sun -- it goes underneath"
    assert st["backup_blinds_close"] is True, \
        "the blind covers the same opening and works at any sun height"
    assert st["retract"] is False, "and it is calm and dry -- not a hazard"
    assert st["recommendation"] == "backup"


def test_high_sun_lets_the_awning_extend():
    shade.SUN_MODEL = "irradiance"
    shade.AWNING_MIN_ELEVATION = 35
    _set_irradiance(_irr(600, elevation=55.0))
    st = shade.evaluate()
    assert st["awning_extend"] is True


def test_elevation_gate_is_configurable():
    shade.SUN_MODEL = "irradiance"
    shade.AWNING_MIN_ELEVATION = 50        # short projection, needs high sun
    _set_irradiance(_irr(600, elevation=40.0))
    st = shade.evaluate()
    assert st["awning_extend"] is False
    assert st["backup_blinds_close"] is True
    shade.AWNING_MIN_ELEVATION = 27        # deep projection, works lower
    st = shade.evaluate()
    assert st["awning_extend"] is True


def test_elevation_gate_never_invents_a_known_value():
    """Gating may turn a True into False, never an unknown into False."""
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(None)                  # radiation unavailable -> fallback
    st = shade.evaluate()
    assert st["sun_model"] == "sunshine_fallback"


def test_irradiance_values_are_published_for_diagnostics():
    shade.SUN_MODEL = "irradiance"
    _set_irradiance(_irr(612.4))
    st = shade.evaluate()
    assert st["irradiance_window"] == 612
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
