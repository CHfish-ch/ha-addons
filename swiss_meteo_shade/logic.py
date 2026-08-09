#!/usr/bin/env python3
"""Awning / blind decision logic for Swiss Meteo Shade.

Pure functions -- no I/O -- so the whole decision surface is unit-testable.

Inputs (computed by radar + forecast modules):
    rain           bool   -- rain within the radar look-ahead (now..+10 min)
    gust_kmh       float|None -- max forecast gust in the outlook window (km/h)
    sunshine       bool|None  -- meaningful sunshine expected
    temp_c         float|None -- forecast temperature in the window (deg C)
    gust_limit     float   -- retract threshold, km/h
    gust_release   float|None -- re-extend below this (hysteresis); None = no hyst
    min_temp_c     float|None -- don't deploy shade below this; None = no gate
    prev_retract   bool   -- retract state from the previous cycle (for hysteresis)

Derived:
    sun        = sunshine expected AND (temp gate passes)
    wind_high  = hysteresis band around gust_limit / gust_release
    retract    = wind_high OR rain            (fast safety override)

Outputs (sunny branch):
    awning_extend            = sun AND NOT retract
    backup_blinds_close      = sun AND retract
    independent_blinds_close = sun
    recommendation (enum)    = "extend" | "backup" | "none"

An UNKNOWN forecast (gust_kmh is None or sunshine is None) is reported via
forecast_unavailable, and never silently masquerades as "calm" or "no sun":
a missing gust does not force retract, but a missing sunshine forecast means
sun is unknown -> treated as not-sunny (awning stays in) AND flagged.
"""


def _wind_high(gust_kmh, gust_limit, gust_release, prev_wind_high):
    """Hysteresis on the WIND signal alone (A1). With gust_release set, once
    wind_high we stay wind_high until the gust drops below gust_release; when
    not, we cross to wind_high only at/above gust_limit. Keyed on the previous
    WIND state, not the combined retract -- so a rain cycle does not shift the
    wind threshold. Without gust_release, a plain threshold at gust_limit."""
    if gust_kmh is None:
        return False                      # unknown gust never forces retract
    if gust_release is None:
        return gust_kmh >= gust_limit
    if prev_wind_high:
        return gust_kmh >= gust_release   # stay high until below release
    return gust_kmh >= gust_limit         # cross to high only at full limit


_UNSET = object()      # B4: distinguishes "argument not supplied" from None


def decide(rain, gust_kmh, sunshine, gust_limit,
           temp_c=None, gust_release=None, min_temp_c=None,
           prev_wind_high=False, gust_sources_ok=True,
           sun_awning=_UNSET, sun_backup=_UNSET, sun_independent=_UNSET,
           awning_effective=True):

    """gust_sources_ok: True if at least one gust source answered this cycle.
    If NO gust source answered (all failed), we cannot vouch for wind safety,
    so the awning is kept in even when sunny -- 'stay in only if both gust
    sources fail'. A present-but-low gust is trusted normally."""
    rain = bool(rain)
    sunshine_missing = sunshine is None
    gust_missing = not gust_sources_ok        # every gust source failed
    forecast_unavailable = sunshine_missing or gust_missing

    # temperature gate: below min_temp_c we do not want shade out at all.
    # If temp is unavailable the gate simply does not apply this cycle.
    gate_active = min_temp_c is not None and temp_c is not None
    temp_ok = (temp_c >= min_temp_c) if gate_active else True
    temp_blocks = gate_active and not temp_ok      # A10: one derived from other

    # each output has its own sun flag; all share the temperature gate.
    # B4: _UNSET means "caller didn't supply it" -> fall back to `sunshine`.
    # An explicit None means "unknown" and must NOT be read as "not sunny": it
    # propagates into sunshine_missing so forecast_unavailable is flagged.
    if sun_awning is _UNSET:
        sun_awning = sunshine
    if sun_backup is _UNSET:
        sun_backup = sunshine
    if sun_independent is _UNSET:
        sun_independent = sunshine
    if sun_awning is None or sun_backup is None or sun_independent is None:
        sunshine_missing = True
        forecast_unavailable = True       # recompute: a per-output sun is unknown
    sun_awning = bool(sun_awning) and temp_ok
    sun_backup = bool(sun_backup) and temp_ok
    sun_independent = bool(sun_independent) and temp_ok
    sun = sun_awning     # the awning drives the primary "sun expected" signal

    # If all gust sources failed, force retract (fail-safe on the safety input).
    wind_high = _wind_high(gust_kmh, gust_limit, gust_release, prev_wind_high)
    retract = wind_high or rain or gust_missing

    # Two independent reasons the awning cannot do the job:
    #   retract           -- UNSAFE (wind, rain, or no gust reading at all)
    #   not awning_effective -- INEFFECTIVE (sun too low; the beam passes
    #                        under it, so extending would shade nothing)
    # The backup blind covers the same opening and works at any sun height, so
    # it takes over in BOTH cases. Tying it to `retract` alone left a bright
    # low evening sun with the awning correctly in and the blind pointlessly
    # up. awning_effective defaults True, so the sunshine model -- which has
    # no geometric gate -- behaves exactly as before.
    awning_usable = (not retract) and bool(awning_effective)

    extend = sun_awning and awning_usable
    backup = sun_backup and not awning_usable
    indep = sun_independent

    if extend:
        rec = "extend"
    elif backup:
        rec = "backup"
    else:
        rec = "none"

    return {
        "sun": sun,
        "wind_high": wind_high,
        "gust_unknown": gust_missing,
        "forecast_unavailable": forecast_unavailable,
        "temp_blocks": temp_blocks,
        "rain": rain,
        "retract": retract,
        "awning_usable": awning_usable,
        "awning_extend": extend,
        "backup_blinds_close": backup,
        "independent_blinds_close": indep,
        "recommendation": rec,
        "reason": _reason(sun, wind_high, rain, gust_kmh, gust_limit,
                          gust_missing, sunshine_missing, temp_blocks,
                          temp_c, min_temp_c, awning_effective),
    }


def _reason(sun, wind_high, rain, gust_kmh, gust_limit,
            gust_missing, sunshine_missing, temp_blocks, temp_c, min_temp_c,
            awning_effective=True):
    # Safety-first ordering: a missing gust reading is treated as unsafe.
    if gust_missing:
        return ("no gust forecast available (all sources failed) -> awning "
                "kept in as a precaution")
    if temp_blocks:
        return f"too cold ({temp_c:.0f}<{min_temp_c:.0f} C) -> shade not deployed"
    if sunshine_missing:
        return "sunshine forecast unavailable -> treating as not sunny"
    if not sun:
        return "no significant sun expected"
    g = f"{gust_kmh:.0f}" if gust_kmh is not None else "?"
    if (not wind_high) and (not rain) and not awning_effective:
        return ("sun, but too low for the awning to shade -> backup blind "
                "instead")
    if wind_high and rain:
        return f"sun, but gust {g}>={gust_limit:.0f} km/h and rain approaching"
    if wind_high:
        return f"sun, but gust {g}>={gust_limit:.0f} km/h -> awning unsafe"
    if rain:
        return "sun, but rain approaching -> awning unsafe"
    return f"sun, calm (gust {g} km/h), dry -> awning ok"
