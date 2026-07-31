#!/usr/bin/env python3
"""
Binary "is it raining on my 1 km cell?" sensors for Home Assistant,
from the MeteoSwiss PRECIP (RZC) open-data radar composite.

  now   : value of the radar cell containing your coordinates
  +5min : same cell, after advecting the field forward one 5-min step
  +10min: ... two steps

Method: Lagrangian persistence. Motion of the precipitation field is estimated
by FFT phase correlation between consecutive 5-min radar frames, then the
current field is shifted along that motion vector. No physics, no growth/decay,
but at 5-10 min lead that is where nearly all the skill lives anyway.

Data: https://opendatadocs.meteoswiss.ch/d-radar-data/d1-precipitation-radar-products
Free to use. You must cite "Source: MeteoSwiss" if you redistribute.

This module is a library used by shade.py -- it exposes evaluate(session) and
returns the radar rain signals. It has no __main__/CLI of its own.
"""

import json
import io
import os
from datetime import datetime, timedelta, timezone

import requests

from version import USER_AGENT

# numpy and h5py are only needed to read radar files. Keeping them optional
# means `--probe` works anywhere with just `pip install requests`.
try:
    import numpy as np
except ImportError:
    np = None
try:
    import h5py
except ImportError:
    h5py = None

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# Position. Pick ONE. Priority: Home Assistant > LV95 > WGS84.
# LV95 is the native grid of the radar data -- read it straight off
# https://map.geo.admin.ch/ (right-click a point, "Position anzeigen").
POS_LV95 = (2665512.66, 1211882.47)   # (easting, northing) -- PLACEHOLDER:
POS_WGS84 = None                      # central Lucerne. Replace with your own.
                                      # Or set POS_WGS84 = (lat, lon) instead.

# Ask Home Assistant for its home location instead. Off by default: many
# installs deliberately fuzz the home zone, and a few hundred metres of
# fuzzing can land you in a different 1 km cell.

THRESHOLD_MMH = 0.1           # mm/h at or above this counts as "precipitation"

# Tolerance radius in km applied to the FORECAST steps only, to absorb
# advection error. 0 = strictly the single 1 km cell. 1 = 3x3 km max.
FORECAST_TOLERANCE_KM = 1
NOW_TOLERANCE_KM = 0

# Motion estimation needs real echo to track. Below this many cells at or above
# THRESHOLD_MMH inside the correlation window, the vector is noise -- see
# estimate_motion. 3 cells is deliberately low: enough to reject a blank sky
# without discarding a small genuine shower.
MOTION_MIN_ECHO_CELLS = 3
MAX_MOTION_KMH = 120         # discard motion vectors implying faster than this
MOTION_WINDOW_KM = 128        # size of the box used to estimate field motion
MAX_DATA_AGE_MIN = 20         # older than this -> sensors go "unavailable"

# Radar frames publish roughly 8-15 minutes after the time they describe. With
# anchoring on, "in 5 minutes" means 5 minutes from NOW: the extra steps needed
# to catch up to the present are added automatically. Turn off to interpret the
# lead times relative to the radar frame instead.
ANCHOR_TO_NOW = True
MAX_ANCHOR_STEPS = 4          # cap the catch-up extrapolation at 20 minutes

# How to get the values into Home Assistant.
#   "mqtt" -- MQTT discovery. Binds to a stable wire protocol rather than to
#             HA's internal Python APIs, so it survives HA upgrades without
#             maintenance. Entities are permanent and grouped in a device.
#   "rest" -- push straight into HA's API. No broker, but entities are not
#             registered: they vanish on restart until the next cycle and
#             cannot be renamed or assigned to an area in the UI.


STAC = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.meteoschweiz.ogd-radar-precip"
ITEM_ID_FORMAT = "%Y%m%d-ch"   # daily items, e.g. 20260724-ch
RZC_PREFIX = "rzc"             # PRECIP product; assets are lowercase

# Swiss radar composite grid: 710 x 640 cells of 1 km, LV95 / EPSG:2056.
GRID_E_MIN, GRID_N_MAX, CELL_M = 2_255_000, 1_480_000, 1000
GRID_COLS, GRID_ROWS = 710, 640


# ----------------------------------------------------------------------------
# Coordinates: WGS84 -> LV95 (swisstopo approximate formulas, ~1 m accuracy)
# ----------------------------------------------------------------------------
def wgs84_to_lv95(lat, lon):
    p = (lat * 3600 - 169028.66) / 10000.0
    l = (lon * 3600 - 26782.5) / 10000.0
    e = (2600072.37 + 211455.93 * l - 10938.51 * l * p
         - 0.36 * l * p**2 - 44.54 * l**3)
    n = (1200147.07 + 308807.95 * p + 3745.25 * l**2 + 76.63 * p**2
         - 194.56 * l**2 * p + 119.79 * p**3)
    return e, n


def lv95_to_grid(e, n, origin_e=None, origin_n=None):
    origin_e = GRID_E_MIN if origin_e is None else origin_e
    origin_n = GRID_N_MAX if origin_n is None else origin_n
    col = int((e - origin_e) // CELL_M)
    row = int((origin_n - n) // CELL_M)
    if not (0 <= col < GRID_COLS and 0 <= row < GRID_ROWS):
        raise RuntimeError(f"Coordinates fall outside the radar grid (row={row}, col={col})")
    return row, col


def resolve_position(session):
    """Return (easting, northing) in LV95, plus a note on where it came from.

    Position comes from the add-on options only. An earlier draft could query
    the Home Assistant API for the home coordinates, but that path was dead: an
    add-on runs in its own network-isolated container, so 127.0.0.1 reaches the
    add-on itself, not Home Assistant. (Reaching Core from an add-on requires
    the Supervisor proxy at http://supervisor/core/api with SUPERVISOR_TOKEN.)
    Since the coordinates are a one-off setup value, options are simpler and
    avoid carrying a long-lived token."""
    if POS_LV95:
        return float(POS_LV95[0]), float(POS_LV95[1]), "lv95"
    if POS_WGS84:
        return wgs84_to_lv95(float(POS_WGS84[0]), float(POS_WGS84[1])) + ("wgs84",)
    raise SystemExit("No position configured: set the easting/northing options.")


def read_grid_origin(source):
    """Northwest corner of the northwest pixel, in LV95, from the ODIM
    /where attributes. Per ODIM_H5 Table 5 these are the OUTER corners of
    the corner pixels, so they land on cell boundaries, not cell centres."""
    if h5py is None:
        return None, None
    with h5py.File(source, "r") as f:
        if "where" not in f:
            return None, None
        w = f["where"].attrs
        if "UL_lat" not in w or "UL_lon" not in w:
            return None, None
        def _f(v):
            return float(v.decode() if isinstance(v, bytes) else v)
        e, n = wgs84_to_lv95(_f(w["UL_lat"]), _f(w["UL_lon"]))
        # The corner conversion carries a few metres of approximation error.
        # Snap to the km grid when the offset is clearly noise, but leave a
        # genuine shift (e.g. half a cell) visible rather than hiding it.
        se, sn = round(e / CELL_M) * CELL_M, round(n / CELL_M) * CELL_M
        if abs(e - se) < 100 and abs(n - sn) < 100:
            return float(se), float(sn)
        return e, n


# ----------------------------------------------------------------------------
# STAC: find the most recent RZC files
# ----------------------------------------------------------------------------
def rzc_time_from_name(name):
    """RZCyyjjjHHMMKK.XYZ.h5 -> UTC datetime, or None if the name doesn't match."""
    base = os.path.basename(name)
    try:
        yy, jjj, hhmm = int(base[3:5]), int(base[5:8]), base[8:12]
        d = datetime(2000 + yy, 1, 1, tzinfo=timezone.utc) + timedelta(days=jjj - 1)
        return d.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:4]))
    except (ValueError, IndexError):
        return None


def _rfc3339(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _harvest(payload, found):
    for feature in payload.get("features", []):
        for key, asset in (feature.get("assets") or {}).items():
            href = asset.get("href", "")
            name = os.path.basename(href.split("?")[0]) or key
            found[name] = href


def _extend(session, dated, count):
    """Take the newest listed frames, then reach past the metadata lag."""
    picked = [(n, h) for _, n, h in dated]
    newest_name, newest_href = picked[-1]
    picked.extend(probe_newer(session, newest_name, newest_href))
    return picked[-count:]


def _item_assets(session, item_id):
    """Assets of one daily STAC item, keyed by filename. {} if it doesn't exist.

    Network errors raise RuntimeError with a concise message (not a stack
    trace) so a transient blip logs one clean line upstream."""
    url = f"{STAC}/collections/{COLLECTION}/items/{item_id}"
    try:
        r = session.get(url, timeout=30)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"radar STAC unreachable: {type(exc).__name__}")
    except ValueError as exc:
        raise RuntimeError(f"radar STAC bad response: {exc}")
    found = {}
    _harvest({"features": [payload]}, found)
    return found


def _name_at(template, t):
    """Rebuild an asset filename for a different timestamp."""
    return (template[:3]
            + f"{t.year % 100:02d}{t.timetuple().tm_yday:03d}{t.hour:02d}{t.minute:02d}"
            + template[12:])


def _url_for(template_href, name, t):
    base = template_href.rsplit("/", 2)[0]
    return f"{base}/{t.strftime(ITEM_ID_FORMAT)}/{name}"


def probe_newer(session, name, href, max_steps=6):
    """Files often exist before the daily STAC item lists them, so walk forward
    from the newest listed frame and keep whatever actually resolves.

    Verified against the live CDN: data.geo.admin.ch honours HEAD with 200 for
    existing objects, and returns 403 (not 404) for objects that don't exist
    yet. So a non-200 here means "no frame at this timestamp" -- we stop
    walking, which is correct: frames are contiguous, so the first gap is the
    end of what's published."""
    out = []
    t0 = rzc_time_from_name(name)
    if t0 is None:
        return out
    now = datetime.now(timezone.utc)
    for i in range(1, max_steps + 1):
        t = t0 + timedelta(minutes=5 * i)
        if t > now:
            break
        n = _name_at(name, t)
        u = _url_for(href, n, t)
        try:
            r = session.head(u, timeout=15, allow_redirects=True)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break                         # 403/404 = not published yet -> stop
        out.append((n, u))
    return out


def _rzc_sorted(found):
    """(time, name, href) for RZC files only, oldest first."""
    out = []
    for name, href in found.items():
        if not name.lower().startswith(RZC_PREFIX):
            continue
        t = rzc_time_from_name(name)
        if t is not None:
            out.append((t, name, href))
    out.sort()
    return out


def latest_rzc_assets(count=3, session=None):
    """Newest `count` RZC files, oldest first.

    Items in this collection are one per day, with ids like `20260724-ch`, so
    we address today directly instead of paging the listing -- the listing
    starts at the oldest day of the rolling window, and every item reports the
    same ingest timestamp in `datetime`, making a time filter useless.
    """
    s = session or requests.Session()
    now = datetime.now(timezone.utc)
    found, diag = {}, []

    for back in (0, 1):          # today, then yesterday just after UTC midnight
        item_id = (now - timedelta(days=back)).strftime(ITEM_ID_FORMAT)
        try:
            assets = _item_assets(s, item_id)
        except RuntimeError as exc:
            # _item_assets wraps network/HTTP errors as RuntimeError, so that
            # is what we catch here -- a blip on one item falls through to the
            # next (yesterday), not a crash.
            diag.append(f"{item_id}={exc}")
            continue
        found.update(assets)
        diag.append(f"{item_id}={len(assets)} assets")
        dated = _rzc_sorted(found)
        if len(dated) >= count:
            return _extend(s, dated, count)

    dated = _rzc_sorted(found)
    if dated:
        return _extend(s, dated, count)

    prefixes = sorted({n[:3].lower() for n in found if n.lower().endswith(".h5")})
    raise RuntimeError(
        f"No '{RZC_PREFIX}' files found. Checked {' | '.join(diag)}. "
        f"Prefixes present: {prefixes or 'none'}. "
        f"If the product prefix changed, update RZC_PREFIX.")



def read_rzc(source):
    """Read an RZC file from a path OR an in-memory buffer (io.BytesIO).
    h5py.File accepts both, so radar data never needs to touch disk."""
    if h5py is None or np is None:
        raise RuntimeError("numpy and h5py are required to read radar files.")
    with h5py.File(source, "r") as f:
        ds = f["dataset1"]["data1"]
        raw = ds["data"][:].astype(np.float32)
        w = dict(ds["what"].attrs) if "what" in ds else {}
        gain = float(w.get("gain", 1.0))
        offset = float(w.get("offset", 0.0))
        nodata = w.get("nodata")
        undetect = w.get("undetect")
        unit = w.get("unit")
        if unit is not None:
            unit = unit.decode() if isinstance(unit, bytes) else str(unit)
            if unit.lower() not in ("mm/h", "mm h-1"):
                raise RuntimeError(f"Unexpected unit {unit!r}; expected mm/h.")

        rate = raw * gain + offset

        # Verified against a real file (2026-07-23): dtype is float64 and the
        # attrs read quantity=RATE, unit=mm/h, gain=1.0, offset=0.0 -- the
        # stored values already ARE mm/h, so the transform is an identity.
        # `nodata` is NaN, which never compares equal to anything, so it has to
        # be masked by isnan rather than by equality.
        for flag in (nodata, undetect):
            if flag is None:
                continue
            flag = float(flag)
            rate[np.isnan(raw) if np.isnan(flag) else (raw == flag)] = 0.0

        rate[~np.isfinite(rate)] = 0.0
        rate[rate < 0] = 0.0

        # The product documents its own ceiling in how/MeteoSwiss
        # (usr_max_rainrate, 120 mm/h). Well past it means a decoding problem.
        if float(rate.max()) > 500:
            raise RuntimeError(f"Implausible rain rate {rate.max():.0f} mm/h; "
                               f"check gain/offset. See tools/ for the diagnostic scripts.")

    if rate.shape != (GRID_ROWS, GRID_COLS):
        raise RuntimeError(f"Unexpected grid shape {rate.shape}; "
                           f"expected {(GRID_ROWS, GRID_COLS)}. See tools/ for the diagnostic scripts.")
    return rate


def clean_tmp_dir():
    """No-op retained for API compatibility: radar data is now read from memory
    (io.BytesIO), so nothing is ever written to disk and there is nothing to
    sweep. This avoids SD-card wear on Raspberry-Pi HA installs."""
    return


def download(href, session):
    """Fetch an RZC file into an in-memory buffer -- no disk write."""
    r = session.get(href, timeout=60)
    r.raise_for_status()
    return io.BytesIO(r.content)


# ----------------------------------------------------------------------------
# Motion estimation: FFT phase correlation
# ----------------------------------------------------------------------------
def _window(field, row, col, size):
    h = size // 2
    r0, c0 = max(0, row - h), max(0, col - h)
    r1, c1 = min(field.shape[0], row + h), min(field.shape[1], col + h)
    return field[r0:r1, c0:c1]


def phase_correlate(prev, cur):
    """Pixel displacement (drow, dcol) of the field from prev to cur."""
    a = np.log1p(prev)
    b = np.log1p(cur)
    if a.std() < 1e-3 or b.std() < 1e-3:      # essentially empty -> no motion
        return 0.0, 0.0

    hann = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    A = np.fft.fft2((a - a.mean()) * hann)
    B = np.fft.fft2((b - b.mean()) * hann)
    R = np.conj(A) * B
    R /= np.abs(R) + 1e-9
    corr = np.fft.ifft2(R).real

    dr, dc = np.unravel_index(np.argmax(corr), corr.shape)
    if dr > a.shape[0] // 2:
        dr -= a.shape[0]
    if dc > a.shape[1] // 2:
        dc -= a.shape[1]
    return float(dr), float(dc)


def _has_echo(window):
    """True when the window holds enough precipitation to track (see
    MOTION_MIN_ECHO_CELLS). NaN is nodata and never counts."""
    if window is None or np is None:
        return False
    finite = window[~np.isnan(window)]
    if finite.size == 0:
        return False
    return int((finite >= THRESHOLD_MMH).sum()) >= MOTION_MIN_ECHO_CELLS


def estimate_motion(frames, row, col):
    """Mean displacement per 5-min step, from consecutive frame pairs.

    Gated on the correlation window actually containing precipitation. Phase
    correlation is a cross-correlation: on a field with no echo it locks onto
    numerical noise and returns a large, randomly-directed vector. Observed in
    the wild on a cloudless afternoon -- two overlapping frame triplets gave
    (+5.5, -4.5) and (-7.0, -3.0), i.e. ~90 km/h in opposite directions, which
    no real weather system does. That bogus vector then projected the sample
    point ~14 km away and picked up marginal echo that was never approaching,
    producing a single cycle of spurious rain.

    With no echo to track there is also nothing that could arrive within the
    lead time, so zero motion (pure persistence) is both safer and more
    physically honest than a fabricated vector."""
    est = []
    for prev, cur in zip(frames, frames[1:]):
        w_prev = _window(prev, row, col, MOTION_WINDOW_KM)
        w_cur = _window(cur, row, col, MOTION_WINDOW_KM)
        # only correlate when there is something to correlate
        if not (_has_echo(w_prev) and _has_echo(w_cur)):
            continue
        est.append(phase_correlate(w_prev, w_cur))
    if not est:
        return 0.0, 0.0
    drow = float(np.mean([e[0] for e in est]))
    dcol = float(np.mean([e[1] for e in est]))
    # Discard implausibly fast motion (corrupt FFT shift): 1 cell/5min = 12 km/h.
    # Beyond MAX_MOTION_KMH we cannot trust the vector, so fall back to zero
    # motion (persistence) rather than sampling a wildly wrong future cell.
    max_cells = MAX_MOTION_KMH / 12.0
    if (drow * drow + dcol * dcol) ** 0.5 > max_cells:
        return 0.0, 0.0
    return drow, dcol


# ----------------------------------------------------------------------------
# Sampling
# ----------------------------------------------------------------------------
def sample(field, row, col, tolerance_km):
    t = int(tolerance_km)
    r0, r1 = max(0, row - t), min(field.shape[0], row + t + 1)
    c0, c1 = max(0, col - t), min(field.shape[1], col + t + 1)
    patch = field[r0:r1, c0:c1]
    return float(patch.max()) if patch.size else 0.0


def evaluate(session=None):
    own = session is None
    if own:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT

    assets = latest_rzc_assets(3, session)
    frames, buffers = [], []
    origin_e = origin_n = None
    for name, href in assets:
        buf = download(href, session)
        buffers.append(buf)
        frames.append(read_rzc(buf))
    # read_grid_origin needs to re-seek the newest buffer to the start
    buffers[-1].seek(0)
    origin_e, origin_n = read_grid_origin(buffers[-1])

    latest_name = assets[-1][0]
    frame_time = rzc_time_from_name(latest_name)
    if frame_time is None:
        raise RuntimeError(f"Unparseable radar frame name: {latest_name}")
    age_min = (datetime.now(timezone.utc) - frame_time).total_seconds() / 60.0

    e, n, pos_source = resolve_position(session)
    row, col = lv95_to_grid(e, n, origin_e, origin_n)
    current = frames[-1]
    drow, dcol = estimate_motion(frames, row, col)

    oe = GRID_E_MIN if origin_e is None else origin_e
    on = GRID_N_MAX if origin_n is None else origin_n

    result = {
        "radar_time": frame_time.isoformat(),
        "age_min": round(age_min, 1),
        "stale": age_min > MAX_DATA_AGE_MIN,
        "cell": [row, col],
        "position_source": pos_source,
        "position_lv95": [round(e, 1), round(n, 1)],
        "cell_origin_from_file": origin_e is not None,
        "cell_bounds_lv95": {
            "e": [round(oe + col * CELL_M), round(oe + (col + 1) * CELL_M)],
            "n": [round(on - (row + 1) * CELL_M), round(on - row * CELL_M)],
        },
        "motion_km_per_5min": [round(-drow, 1), round(dcol, 1)],  # north+, east+
        "speed_kmh": round(float(np.hypot(drow, dcol)) * 12, 1),
        "threshold_mmh": THRESHOLD_MMH,
    }

    anchor = 0
    if ANCHOR_TO_NOW:
        anchor = max(0, min(int(round(age_min / 5.0)), MAX_ANCHOR_STEPS))
    result["anchor_steps"] = anchor
    result["anchored_to_now"] = ANCHOR_TO_NOW

    for lead in (0, 5, 10):
        k = anchor + lead // 5
        # the cell that will have drifted onto us in `lead` minutes
        r = int(round(row - k * drow))
        c = int(round(col - k * dcol))
        r = min(max(r, 0), GRID_ROWS - 1)
        c = min(max(c, 0), GRID_COLS - 1)
        tol = NOW_TOLERANCE_KM if lead == 0 else FORECAST_TOLERANCE_KM
        rate = sample(current, r, c, tol)
        key = "now" if lead == 0 else f"t{lead}"
        result[f"rate_{key}_mmh"] = round(rate, 2)
        result[key] = bool(rate >= THRESHOLD_MMH)

    result["any"] = bool(result["now"] or result["t5"] or result["t10"])

    # Whole-grid stats: if these are zero across all of Switzerland it is
    # either genuinely dry, or the mm/h decoding is wrong. Cheap sanity check.
    result["field_max_mmh"] = round(float(current.max()), 2)
    result["field_wet_cells"] = int((current >= THRESHOLD_MMH).sum())

    return result
