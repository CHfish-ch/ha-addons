"""Acceptance tests for plane-of-array irradiance (spec section 7, cases A-H).

Expected values come from the specification. Tolerance is +/-1 W/m2 as it
states -- a few of its figures carry ~0.4 of rounding, so an exact-equality
assertion would fail against correct code.

Case G is the one that matters most: it is a regression against evaluating
solar geometry only at the hour midpoint, which reports ~11 W/m2 for a sunset
hour whose true value is ~361 and would retract an awning while the sun is
still fully on the facade.
"""
import math
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import irradiance as ir      # noqa: E402

TOL = 1.0                    # W/m2, per spec section 7


def _close(got, want, what):
    assert abs(got - want) <= TOL, f"{what}: got {got:.1f}, want {want:.1f}"


def _spot(ghi, dhi, elev, tilt, albedo=0.20, min_elevation=3.0):
    """Instantaneous evaluation, DNI reconstructed from this instant."""
    dni = ir.dni_instantaneous(ghi, dhi, elev)
    return ir.poa(dni, ghi, dhi, elev, tilt, albedo, min_elevation)


# --- Case A: high sun, clear -------------------------------------------------
def test_case_a_high_sun_wall():
    r = _spot(900, 120, 60, 90)
    _close(r["cos_incidence"], 0.5000, "cos theta")
    _close(r["direct"], 450.3, "direct")
    _close(r["diffuse"], 60.0, "diffuse")
    _close(r["ground"], 90.0, "ground")
    _close(r["total"], 600.3, "E_poa")


def test_case_a_high_sun_awning():
    r = _spot(900, 120, 60, 45)
    _close(r["cos_incidence"], 0.9659, "cos theta")
    _close(r["direct"], 869.9, "direct")
    _close(r["diffuse"], 102.4, "diffuse")
    _close(r["ground"], 26.4, "ground")
    _close(r["total"], 998.7, "E_poa")


# --- Case B: low sun, clear --------------------------------------------------
def test_case_b_low_sun_wall():
    r = _spot(264, 70, 15, 90)
    _close(r["cos_incidence"], 0.9659, "cos theta")
    _close(r["total"], 785.8, "E_poa")


def test_case_b_low_sun_awning():
    r = _spot(264, 70, 15, 45)
    _close(r["cos_incidence"], 0.8660, "cos theta")
    _close(r["total"], 717.0, "E_poa")


def test_tilt_crossover_between_high_and_low_sun():
    """The 45 awning wins at high sun, the vertical wall at low sun.

    Any implementation without this crossover has the geometry wrong, so it
    is asserted directly rather than left implicit in the numbers above.
    """
    high_wall = _spot(900, 120, 60, 90)["total"]
    high_awn = _spot(900, 120, 60, 45)["total"]
    low_wall = _spot(264, 70, 15, 90)["total"]
    low_awn = _spot(264, 70, 15, 45)["total"]
    assert high_awn > high_wall, "awning must collect more at high sun"
    assert low_wall > low_awn, "wall must collect more at low sun"


# --- Case C: overcast --------------------------------------------------------
def test_case_c_overcast():
    wall = _spot(180, 180, 30, 90)
    awn = _spot(180, 180, 30, 45)
    assert wall["direct"] == 0.0 and awn["direct"] == 0.0
    _close(wall["total"], 108.0, "wall E_poa")
    _close(awn["total"], 158.9, "awning E_poa")


# --- Case D: guards ----------------------------------------------------------
def test_case_d_grazing_sun_gates_direct_only():
    r = _spot(900, 120, 1.0, 90)          # below min_elevation of 3
    assert r["direct"] == 0.0
    assert r["diffuse"] > 0.0 and r["ground"] > 0.0, \
        "only the DIRECT term is gated above the horizon"


def test_case_d_below_horizon_zeroes_everything():
    r = _spot(900, 120, -5.0, 90)
    assert r["total"] == 0.0
    assert r["diffuse"] == 0.0 and r["ground"] == 0.0


def test_case_d_diffuse_exceeding_global_does_not_raise():
    r = _spot(180, 200, 30, 90)           # DHI > GHI, a rounding artefact
    assert ir.beam_horizontal(180, 200) == 0.0
    assert r["direct"] == 0.0


# --- Case E: albedo sensitivity ---------------------------------------------
def test_case_e_albedo_sensitivity():
    for rho, want in ((0.10, 555.3), (0.20, 600.3), (0.70, 825.3)):
        _close(_spot(900, 120, 60, 90, albedo=rho)["total"], want,
               f"E_poa at albedo {rho}")


# --- Case F: degenerate hour -------------------------------------------------
def test_case_f_degenerate_hour_has_no_direct_and_does_not_raise():
    dni = ir.dni_from_hour(26.0, 16.8, hour_sine=0.004)
    assert dni == 0.0, "S below the sin(0.5 deg) floor must yield no beam"
    r = ir.poa(dni, 26.0, 16.8, 5.0, 90)
    assert r["direct"] == 0.0
    assert r["total"] > 0.0, "diffuse and ground still apply"


# --- Case G: sunset partial hour (mandatory regression) ----------------------
def _case_g_elevations(substeps=12):
    """Hour 19:00-20:00: 6.0 deg at 19:00 falling 12 deg/h, sunset 19:30.

    Elevations are taken at sub-step MIDPOINTS, per spec 3.5.
    """
    step = 60.0 / substeps
    return [6.0 - 0.2 * (i * step + step / 2.0) for i in range(substeps)]


def test_case_g_hour_reconstruction():
    _close(ir.beam_horizontal(26.0, 16.8), 9.2, "BHI_hour")
    s = ir.hour_mean_sine(_case_g_elevations())
    assert abs(s - 0.0262) < 0.0005, f"S: got {s:.4f}, want 0.0262"
    _close(ir.dni_from_hour(26.0, 16.8, s), 351.7, "DNI_hour")


def test_case_g_sunset_hour_is_not_zeroed():
    """THE regression: 19:05 must be ~361, not the ~11 a midpoint-only
    implementation reports."""
    s = ir.hour_mean_sine(_case_g_elevations())
    dni = ir.dni_from_hour(26.0, 16.8, s)
    got = ir.poa(dni, 26.0, 16.8, 5.0, 90, 0.20, 3.0)["total"]
    _close(got, 361.4, "E_poa at 19:05")
    assert got > 300.0, f"sunset hour collapsed to {got:.1f} W/m2"


def test_case_g_after_sunset_is_zero():
    s = ir.hour_mean_sine(_case_g_elevations())
    dni = ir.dni_from_hour(26.0, 16.8, s)
    assert ir.poa(dni, 26.0, 16.8, -2.0, 90, 0.20, 3.0)["total"] == 0.0


def test_midpoint_only_evaluation_would_fail():
    """Pin the defect itself, so the regression cannot be silently undone.

    At the hour midpoint the sun is exactly on the horizon, so a midpoint
    implementation gates the beam away and loses ~350 W/m2.
    """
    s = ir.hour_mean_sine(_case_g_elevations())
    dni = ir.dni_from_hour(26.0, 16.8, s)
    midpoint_elev = 6.0 - 0.2 * 30          # 0.0 deg
    naive = ir.poa(dni, 26.0, 16.8, midpoint_elev, 90, 0.20, 3.0)["total"]
    correct = ir.poa(dni, 26.0, 16.8, 5.0, 90, 0.20, 3.0)["total"]
    assert naive < 20.0 and correct > 300.0, \
        f"expected the midpoint approach to collapse; got {naive:.1f}"


# --- Case H: min_elevation is per-instant ------------------------------------
def test_case_h_raising_min_elevation_never_changes_the_reconstruction():
    s = ir.hour_mean_sine(_case_g_elevations())
    dni = ir.dni_from_hour(26.0, 16.8, s)
    _close(dni, 351.7, "DNI_hour must not depend on min_elevation")
    gated = ir.poa(dni, 26.0, 16.8, 5.0, 90, 0.20, min_elevation=10.0)
    assert gated["direct"] == 0.0
    _close(gated["total"], 11.0, "diffuse + ground survive the gate")
    # and evaluating the same hour higher up must gate rather than throw
    assert ir.poa(dni, 26.0, 16.8, 6.0, 90, 0.20,
                  min_elevation=10.0)["direct"] == 0.0


def test_case_h_high_min_elevation_does_not_zero_the_whole_hour():
    """Raising the gate must not wipe an hour whose earlier minutes qualify."""
    elevs = _case_g_elevations()
    s = ir.hour_mean_sine(elevs)
    dni = ir.dni_from_hour(26.0, 16.8, s)
    hourly = ir.hourly_mean_poa(dni, 26.0, 16.8, elevs, 90, 0.20,
                                min_elevation=3.0)
    assert hourly > 11.0, \
        f"sub-steps above the gate must still contribute; got {hourly:.1f}"


# --- guard 4: DNI ceiling ----------------------------------------------------
def test_dni_is_clamped_to_the_extraterrestrial_normal():
    e0 = ir.extraterrestrial_normal(172)                 # ~21 June
    assert 1300 < e0 < 1420, f"E0 out of range: {e0:.0f}"
    absurd = ir.dni_from_hour(900, 0, hour_sine=0.01, day_of_year=172)
    assert absurd <= e0 + 1e-9, "DNI above E0 must be capped"


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
