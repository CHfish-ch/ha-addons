#!/usr/bin/env python3
"""Solar position, wrapped thinly around `astral`.

Deliberately the ONLY module that knows any astronomy. `irradiance.py` takes
elevations as arguments so its acceptance cases stay expressible as literals,
and this module is what supplies them in production.

Under the tracking assumption only ELEVATION affects the result -- the surface
azimuth is defined to equal the solar azimuth, so the azimuth term cancels out
of the incidence angle. `azimuth_at` exists purely so the diagnostics can show
where the sun is; nothing in the decision path reads it.

`astral` is pure Python (`py3-none-any`), so unlike numpy/h5py it carries no
architecture or build concerns.
"""

from datetime import timedelta

from astral import Observer
from astral.sun import azimuth as _astral_azimuth
from astral.sun import elevation as _astral_elevation


def observer(lat, lon, height_m=0.0):
    return Observer(latitude=float(lat), longitude=float(lon),
                    elevation=float(height_m))


def elevation_at(obs, when_utc):
    """Solar elevation in degrees; negative below the horizon."""
    return float(_astral_elevation(obs, when_utc))


def azimuth_at(obs, when_utc):
    """Solar azimuth in degrees clockwise from north (diagnostics only)."""
    return float(_astral_azimuth(obs, when_utc))


def substep_times(hour_end_utc, substeps=12):
    """Midpoints of `substeps` equal slices of the hour ENDING at hour_end_utc.

    MeteoSwiss stamps an hourly mean with the END of the period it covers: the
    record stamped 15:00 describes 14:00-15:00. Sub-sampling therefore runs
    over [hour_end - 1h, hour_end], and midpoints are used so no sample sits
    exactly on a boundary.
    """
    if substeps < 1:
        raise ValueError("substeps must be >= 1")
    step = timedelta(hours=1) / substeps
    start = hour_end_utc - timedelta(hours=1)
    return [start + step * i + step / 2 for i in range(substeps)]


def substep_elevations(obs, hour_end_utc, substeps=12):
    """Solar elevations at each sub-step midpoint of the hour."""
    return [elevation_at(obs, t) for t in substep_times(hour_end_utc, substeps)]
