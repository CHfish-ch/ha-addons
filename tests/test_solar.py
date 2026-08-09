"""Tests for solar position and the hour-assembly that consumes it.

Two things are easy to get wrong here and neither shows up as an exception:
the hour a MeteoSwiss timestamp refers to (it is the END of the period), and
mixing the sub-step elevations up with the instantaneous one. Both are
asserted directly.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import irradiance as ir     # noqa: E402
import solar                # noqa: E402

# Lucerne-ish; all reference values below are for this observer.
LAT, LON = 47.05, 8.35


def test_substep_times_cover_the_hour_ENDING_at_the_timestamp():
    """A record stamped 15:00 describes 14:00-15:00, not 15:00-16:00."""
    hour_end = dt.datetime(2026, 6, 21, 15, 0, tzinfo=dt.timezone.utc)
    times = solar.substep_times(hour_end, substeps=12)
    assert len(times) == 12
    assert times[0] > hour_end - dt.timedelta(hours=1), "first sample too early"
    assert times[-1] < hour_end, "last sample must fall inside the hour"
    # midpoints of 5-minute slices: 14:02:30, 14:07:30, ...
    assert times[0] == dt.datetime(2026, 6, 21, 14, 2, 30,
                                   tzinfo=dt.timezone.utc)
    assert times[-1] == dt.datetime(2026, 6, 21, 14, 57, 30,
                                    tzinfo=dt.timezone.utc)


def test_substep_count_is_configurable():
    hour_end = dt.datetime(2026, 6, 21, 15, 0, tzinfo=dt.timezone.utc)
    for n in (6, 12, 60):
        assert len(solar.substep_times(hour_end, n)) == n


def test_elevation_is_negative_at_night_and_high_at_summer_noon():
    obs = solar.observer(LAT, LON)
    noon = dt.datetime(2026, 6, 21, 11, 30, tzinfo=dt.timezone.utc)
    midnight = dt.datetime(2026, 6, 21, 23, 30, tzinfo=dt.timezone.utc)
    assert solar.elevation_at(obs, noon) > 60.0
    assert solar.elevation_at(obs, midnight) < 0.0


def test_elevation_matches_an_independent_computation():
    """Cross-check against values derived from NOAA formulae, +/-0.2 deg
    (the accuracy the specification requires)."""
    obs = solar.observer(LAT, LON)
    for hour, want in ((6, 22.20), (9, 52.17), (12, 65.56), (18, 11.93)):
        t = dt.datetime(2026, 6, 21, hour, 0, tzinfo=dt.timezone.utc)
        got = solar.elevation_at(obs, t)
        assert abs(got - want) < 0.2, f"{hour:02d}:00Z got {got:.2f} want {want}"


def test_azimuth_runs_east_to_west_through_the_day():
    obs = solar.observer(LAT, LON)
    az = [solar.azimuth_at(obs, dt.datetime(2026, 6, 21, h, 0,
                                            tzinfo=dt.timezone.utc))
          for h in (6, 9, 12, 15, 18)]
    assert az == sorted(az), f"azimuth must increase through the day: {az}"
    assert az[0] < 90 and az[-1] > 270, "sunrise NE, sunset NW in midsummer"


def test_evaluate_hour_reproduces_case_g():
    """The assembly must give the same answer as the raw case-G arithmetic.

    Elevations are supplied as literals, so this pins the wiring (which
    elevation feeds the reconstruction vs the instant) without depending on
    any particular date.
    """
    substeps = [6.0 - 0.2 * (5 * i + 2.5) for i in range(12)]
    out = ir.evaluate_hour(ghi=26.0, dhi=16.8, substep_elevations=substeps,
                           now_elevation=5.0, tilts=(90,), albedo=0.20,
                           min_elevation=3.0)
    assert abs(out["hour_sine"] - 0.0262) < 0.0005
    assert abs(out["dni_hour"] - 351.7) < 1.0
    assert abs(out[90]["total"] - 361.4) < 1.0


def test_evaluate_hour_keeps_the_two_elevations_distinct():
    """Swapping the instant for a sub-step must change the answer.

    If an implementation ever collapsed the two, this is what would catch it.
    """
    substeps = [6.0 - 0.2 * (5 * i + 2.5) for i in range(12)]
    at_five = ir.evaluate_hour(26.0, 16.8, substeps, 5.0, (90,))[90]["total"]
    at_mid = ir.evaluate_hour(26.0, 16.8, substeps, 0.0, (90,))[90]["total"]
    assert at_five > 300 and at_mid < 20, \
        f"instant must drive the result: {at_five:.1f} vs {at_mid:.1f}"


def test_real_sunset_hour_is_not_collapsed():
    """Case G against genuine astronomy rather than a linear approximation."""
    obs = solar.observer(LAT, LON)
    # an August evening hour containing sunset at this latitude
    hour_end = dt.datetime(2026, 8, 9, 19, 0, tzinfo=dt.timezone.utc)
    elevs = solar.substep_elevations(obs, hour_end, 12)
    assert min(elevs) < max(elevs), "elevation must vary across the hour"
    s = ir.hour_mean_sine(elevs)
    dni = ir.dni_from_hour(120.0, 60.0, s)
    assert dni > 0.0, "a partially-lit hour must still reconstruct a beam"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
