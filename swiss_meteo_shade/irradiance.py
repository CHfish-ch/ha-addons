#!/usr/bin/env python3
"""Plane-of-array irradiance on a sun-tracking surface.

Pure functions -- no I/O, no astronomy, no clock. Solar elevation is always an
argument, so every branch is reproducible from literals and the acceptance
cases in the spec are expressible directly as unit tests.

THE TRACKING ASSUMPTION
    The surface azimuth is taken to equal the solar azimuth at all times; only
    the tilt is fixed. That removes the azimuth term from the incidence angle:

        cos(theta) = cos(h)*sin(b)*cos(g_sun - g_surf) + sin(h)*cos(b)
                   = sin(h + b)                        when g_surf == g_sun

    The result is an UPPER BOUND for any surface at that tilt -- a real facade
    with a fixed azimuth always receives less. Do not add a configurable
    surface azimuth without revisiting every formula here.

    tilt 90 (wall)   -> cos(theta) = cos(h)
    tilt 45 (awning) -> cos(theta) = sin(h + 45)

    Both stay positive for every h > 0, so no back-face clipping is needed;
    it is applied anyway as a defensive measure.

HOURLY INPUTS, INSTANTANEOUS GEOMETRY
    MeteoSwiss reports GHI and DHI as means over the PRECEDING clock hour (the
    record stamped 15:00 covers 14:00-15:00). The irradiance is constant across
    that hour; the sun is not. Pairing an hour-mean numerator with an
    instantaneous denominator is badly wrong in the sunrise and sunset hours,
    so the two are separated: `hour_mean_sine` + `dni_from_hour` reconstruct a
    single hour-representative DNI, and `poa` then applies whatever geometry
    the caller is evaluating for.

    Concretely, for an hour where the sun sets halfway through, evaluating the
    geometry only at the hour midpoint reports ~11 W/m2 against a true ~361 --
    it would pull the awning in while the sun is still fully on the facade.
    Locked by test case G.
"""

import math

# Solar constant, W/m2. Used for the DNI sanity ceiling (spec 3.4 guard 4).
SOLAR_CONSTANT = 1361.0

# Below this hour-averaged sine of elevation the DNI reconstruction carries no
# information -- the sun was up for a minute or two of the hour. sin(0.5 deg).
MIN_HOUR_SINE = math.sin(math.radians(0.5))

# Isotropic sky. Structured as its own function so an anisotropic model
# (Hay-Davies, Perez) can be dropped in later without touching the callers.
MODEL = "isotropic"


def cos_incidence(elevation_deg, tilt_deg):
    """cos(theta) on a sun-tracking surface, clipped at zero."""
    return max(math.sin(math.radians(elevation_deg + tilt_deg)), 0.0)


def sky_view_factor(tilt_deg):
    """Fraction of the sky dome the surface sees: (1 + cos b) / 2."""
    return (1.0 + math.cos(math.radians(tilt_deg))) / 2.0


def ground_view_factor(tilt_deg):
    """Fraction of the ground the surface sees: (1 - cos b) / 2."""
    return (1.0 - math.cos(math.radians(tilt_deg))) / 2.0


def extraterrestrial_normal(day_of_year):
    """Normal irradiance at the top of the atmosphere, W/m2."""
    return SOLAR_CONSTANT * (
        1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0))


def beam_horizontal(ghi, dhi):
    """GHI - DHI, floored at zero.

    At low irradiance the two are rounded independently and DHI can exceed
    GHI; that is a rounding artefact, not negative beam (spec 3.4 guard 5).
    """
    return max(float(ghi) - float(dhi), 0.0)


def dni_instantaneous(ghi, dhi, elevation_deg):
    """DNI from an instantaneous elevation. Zero at or below the horizon.

    For an hour of forecast data prefer dni_from_hour: this divides by the
    sine at ONE instant, which is exactly the sunrise/sunset trap.
    """
    if elevation_deg <= 0.0:
        return 0.0
    s = math.sin(math.radians(elevation_deg))
    if s < MIN_HOUR_SINE:
        return 0.0
    return beam_horizontal(ghi, dhi) / s


def hour_mean_sine(elevations_deg):
    """Hour-averaged max(sin h, 0) over sub-step elevations (spec 3.5 step 1).

    Sub-steps below the horizon contribute zero, which is what makes a partial
    sunrise/sunset hour self-correcting: the dark minutes drop out of the
    denominator exactly as they were absent from MeteoSwiss's numerator.
    """
    if not elevations_deg:
        return 0.0
    total = sum(max(math.sin(math.radians(e)), 0.0) for e in elevations_deg)
    return total / len(elevations_deg)


def dni_from_hour(ghi, dhi, hour_sine, day_of_year=None):
    """Hour-representative DNI, W/m2.

    Returns 0.0 for a degenerate hour (guard 3) rather than dividing by a
    vanishing sine. Clamped to the extraterrestrial normal when day_of_year is
    given (guard 4); a clamp means the inputs disagree with the astronomy.
    """
    if hour_sine < MIN_HOUR_SINE:
        return 0.0
    dni = beam_horizontal(ghi, dhi) / hour_sine
    if day_of_year is not None:
        dni = min(dni, extraterrestrial_normal(day_of_year))
    return dni


def poa(dni, ghi, dhi, elevation_deg, tilt_deg, albedo=0.20,
        min_elevation=3.0):
    """Plane-of-array irradiance at one instant.

    `dni` is supplied by the caller -- dni_from_hour for forecast data,
    dni_instantaneous for a spot check -- so this function never has to guess
    whether it is looking at an hour mean or a moment.

    Two guards that are deliberately NOT the same thing (spec 3.4):
      - at or below the horizon, EVERY component is zero;
      - between the horizon and min_elevation only the DIRECT term is gated,
        because diffuse and ground-reflected light are still arriving.

    Returns a dict so callers can show the breakdown; `total` is the number
    the decision uses.
    """
    zero = {"direct": 0.0, "diffuse": 0.0, "ground": 0.0, "total": 0.0,
            "cos_incidence": 0.0}
    if elevation_deg <= 0.0:                     # night: nothing arrives
        return zero

    ct = cos_incidence(elevation_deg, tilt_deg)
    # Grazing sun: refraction makes the elevation unreliable and the beam
    # reconstruction carries large relative error. Per instant, never per hour.
    direct = 0.0 if elevation_deg < min_elevation else max(dni, 0.0) * ct
    diffuse = max(float(dhi), 0.0) * sky_view_factor(tilt_deg)
    ground = albedo * max(float(ghi), 0.0) * ground_view_factor(tilt_deg)
    return {"direct": direct, "diffuse": diffuse, "ground": ground,
            "total": direct + diffuse + ground, "cos_incidence": ct}


def evaluate_hour(ghi, dhi, substep_elevations, now_elevation, tilts,
                  albedo=0.20, min_elevation=3.0, day_of_year=None):
    """One forecast hour -> POA per tilt, at the instant `now_elevation`.

    `substep_elevations` are the sub-sampled elevations across the hour, used
    ONLY to reconstruct DNI_hour; `now_elevation` is the moment being
    published. Keeping them separate is the whole point -- see the module
    docstring and test case G.

    Elevations arrive as plain numbers, so this stays free of astronomy and of
    the clock; `solar.py` supplies them in production and the tests supply
    literals.

    Returns {"dni_hour": float, "hour_sine": float, tilt: {...}, ...}.
    """
    s = hour_mean_sine(substep_elevations)
    dni = dni_from_hour(ghi, dhi, s, day_of_year)
    out = {"dni_hour": dni, "hour_sine": s}
    for tilt in tilts:
        out[tilt] = poa(dni, ghi, dhi, now_elevation, tilt, albedo,
                        min_elevation)
    return out


def hourly_mean_poa(dni, ghi, dhi, elevations_deg, tilt_deg, albedo=0.20,
                    min_elevation=3.0):
    """True hourly mean POA across the hour's sub-steps (spec 3.5).

    For a future hour an instantaneous value is meaningless, so the direct
    term is averaged over the same sub-steps used to build the DNI. Diffuse
    and ground terms are already hour means and pass through.

    This differs from `poa` evaluated inside the same hour, by design.
    """
    if not elevations_deg:
        return 0.0
    direct = 0.0
    for e in elevations_deg:
        if e <= 0.0 or e < min_elevation:
            continue
        direct += max(dni, 0.0) * cos_incidence(e, tilt_deg)
    direct /= len(elevations_deg)
    diffuse = max(float(dhi), 0.0) * sky_view_factor(tilt_deg)
    ground = albedo * max(float(ghi), 0.0) * ground_view_factor(tilt_deg)
    return direct + diffuse + ground
