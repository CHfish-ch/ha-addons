"""Tests for the pure decision logic. Run: python3 -m pytest test_logic.py -q
(or just python3 test_logic.py for a dependency-free run)."""

import os
import sys

# tests/ and tools/ sit beside the add-on directory, so put it on the path.
# Works however the file is invoked: pytest, `python3 tests/test_x.py`, or from
# inside the directory itself.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import logic


def d(**kw):
    base = dict(rain=False, gust_kmh=10, sunshine=True, gust_limit=40,
                gust_sources_ok=True)
    base.update(kw)
    return logic.decide(**base)


# --- basic truth table ------------------------------------------------------
def test_sunny_calm_dry_extends():
    r = d()
    assert r["awning_extend"] and not r["backup_blinds_close"]
    assert r["independent_blinds_close"]
    assert r["recommendation"] == "extend"


def test_sunny_windy_backs_up():
    r = d(gust_kmh=50)
    assert not r["awning_extend"] and r["backup_blinds_close"]
    assert r["independent_blinds_close"] and r["recommendation"] == "backup"


def test_sunny_rain_backs_up():
    r = d(rain=True)
    assert r["backup_blinds_close"] and r["recommendation"] == "backup"


def test_not_sunny_nothing():
    r = d(sunshine=False)
    assert not any((r["awning_extend"], r["backup_blinds_close"],
                    r["independent_blinds_close"]))
    assert r["recommendation"] == "none"


def test_invariant_exactly_one_on_sunny_branch():
    for rain in (False, True):
        for gust in (20, 50):
            r = d(rain=rain, gust_kmh=gust, sunshine=True)
            assert r["awning_extend"] != r["backup_blinds_close"]  # exactly one


# --- unknown handling -------------------------------------------------------
def test_all_gust_sources_failed_stays_in():
    # design decision: stay in only if BOTH/all gust sources fail
    r = d(gust_kmh=None, gust_sources_ok=False)
    assert not r["awning_extend"] and r["retract"]
    assert r["gust_unknown"] and r["forecast_unavailable"]
    assert "gust forecast" in r["reason"]


def test_present_low_gust_is_trusted():
    # a real low gust value (sources answered) extends normally
    r = d(gust_kmh=8, gust_sources_ok=True)
    assert r["awning_extend"] and not r["gust_unknown"]
    assert not r["forecast_unavailable"]


def test_unknown_sunshine_flags_unavailable_and_stays_in():
    r = d(sunshine=None)
    assert not r["awning_extend"] and r["forecast_unavailable"]
    assert "not sunny" in r["reason"] or "unavailable" in r["reason"]


# --- hysteresis -------------------------------------------------------------
def test_hysteresis_stays_retracted_in_band():
    # was retracted; gust now 35, between release 32 and limit 40 -> stay retracted
    r = d(gust_kmh=35, gust_limit=40, gust_release=32, prev_wind_high=True)
    assert r["wind_high"] and r["retract"]


def test_hysteresis_reextends_below_release():
    r = d(gust_kmh=30, gust_limit=40, gust_release=32, prev_wind_high=True)
    assert not r["wind_high"]  # dropped below release -> allow extend


def test_hysteresis_no_early_retract_when_extended():
    # was extended; gust 35 below limit 40 -> do not retract yet
    r = d(gust_kmh=35, gust_limit=40, gust_release=32, prev_wind_high=False)
    assert not r["wind_high"]


def test_plain_threshold_without_hysteresis():
    assert d(gust_kmh=40, gust_limit=40)["wind_high"]
    assert not d(gust_kmh=39.9, gust_limit=40)["wind_high"]


# --- temperature gate -------------------------------------------------------
def test_min_temp_blocks_shade():
    r = d(sunshine=True, temp_c=3, min_temp_c=5)
    assert not r["sun"] and r["temp_blocks"]
    assert r["recommendation"] == "none" and "cold" in r["reason"]


def test_min_temp_allows_when_warm():
    r = d(sunshine=True, temp_c=20, min_temp_c=5)
    assert r["sun"] and not r["temp_blocks"]


def test_min_temp_ignored_when_temp_unknown():
    r = d(sunshine=True, temp_c=None, min_temp_c=5)
    assert r["sun"]  # no temp -> gate can't block, don't fail closed on comfort
# --- app window slicing (regression for the filter-before-slice bug) ---
def test_app_window_slice_no_index_shift():
    """A missing value BEFORE the window must not shift which hour we read."""
    import forecast
    from datetime import datetime, timezone, timedelta
    from unittest import mock
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(hours=3)).timestamp() * 1000)
    arr = [10, forecast.MISSING_APP, 20, 30, 40, 50]   # missing at index 1
    with mock.patch.object(forecast, "datetime", wraps=datetime) as md:
        md.now.return_value = now
        win = forecast._app_window_slice(arr, start_ms, 2)
    assert win == [30, 40, 50], win     # hour 3 = value 30, not shifted to 40


def test_app_window_slice_drops_missing_inside_window():
    import forecast
    from datetime import datetime, timezone, timedelta
    from unittest import mock
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(hours=3)).timestamp() * 1000)
    arr = [10, 20, 30, forecast.MISSING_APP, 40, 50]   # missing at current hour
    with mock.patch.object(forecast, "datetime", wraps=datetime) as md:
        md.now.return_value = now
        win = forecast._app_window_slice(arr, start_ms, 2)
    assert win == [40, 50], win
def test_a1_rain_does_not_shift_wind_threshold():
    """A1 regression: hysteresis keyed on wind, not the combined retract.
    A rainy cycle must not switch the wind test to the release threshold."""
    # gust 30, limit 40, release 25, wind was NOT high -> must stay not-high
    r = logic.decide(rain=True, gust_kmh=30, sunshine=True, gust_limit=40,
                     gust_release=25, prev_wind_high=False)
    assert r["wind_high"] is False


def test_a1_wind_hysteresis_holds_across_calm_dip():
    r = logic.decide(rain=False, gust_kmh=30, sunshine=True, gust_limit=40,
                     gust_release=25, prev_wind_high=True)
    assert r["wind_high"] is True     # 30 >= release 25 -> stay high


def test_a3_sunshine_none_is_unavailable():
    r = logic.decide(rain=False, gust_kmh=15, sunshine=None, gust_limit=40)
    assert r["forecast_unavailable"] and not r["sun"]


def test_a5_min_temp_zero_is_active_gate():
    r = logic.decide(rain=False, gust_kmh=15, sunshine=True, gust_limit=40,
                     temp_c=-2, min_temp_c=0)
    assert r["temp_blocks"] and not r["sun"]
def test_three_independent_sun_thresholds():
    """Each output thresholds sunshine independently."""
    # awning wants strong sun, independent blinds accept weak sun
    r = logic.decide(rain=False, gust_kmh=10, sunshine=True, gust_limit=40,
                     sun_awning=False, sun_backup=False, sun_independent=True)
    assert not r["awning_extend"]
    assert r["independent_blinds_close"]
    # backup needs its own sun flag AND retract
    r = logic.decide(rain=True, gust_kmh=10, sunshine=True, gust_limit=40,
                     sun_awning=False, sun_backup=True, sun_independent=True)
    assert r["backup_blinds_close"]      # sun_backup True and rain -> retract
    assert not r["awning_extend"]        # sun_awning False


def test_sun_flags_default_to_shared_sunshine():
    """Without per-output flags, all three fall back to `sunshine`."""
    r = logic.decide(rain=False, gust_kmh=10, sunshine=True, gust_limit=40)
    assert r["awning_extend"] and r["independent_blinds_close"]
def test_window_includes_current_hour_b1():
    """B1 regression: the hour in progress must be in the window."""
    import forecast
    from datetime import datetime, timezone
    from unittest import mock
    now = datetime(2026, 7, 27, 17, 32, tzinfo=timezone.utc)
    rows = [("2026072716", "20"), ("2026072717", "30"),
            ("2026072718", "40"), ("2026072719", "50")]
    with mock.patch.object(forecast, "datetime", wraps=datetime) as md:
        md.now.return_value = now
        w = forecast._window_from_rows(rows, 2)
    assert w == [30.0, 40.0, 50.0], w   # 17:00 in, 16:00 out


def test_explicit_none_sun_flags_unavailable_b4():
    """B4 regression: an explicit None must not fall back to `sunshine`."""
    r = logic.decide(rain=False, gust_kmh=10, sunshine=True, gust_limit=40,
                     sun_awning=None)
    assert not r["awning_extend"] and r["forecast_unavailable"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
