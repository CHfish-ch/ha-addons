#!/usr/bin/env python3
"""Forecast sources for Swiss Meteo Shade.

Produces two numbers for the near-term outlook. The primary source is the
official MeteoSwiss OGD product (documented, licensed, stable); the app's
plzDetail feed is an optional fresher-but-unofficial fallback, opt-in via
prefer_app. An independent Open-Meteo gust source is added on top:

    gust_kmh    : peak wind gust expected in the look-ahead window (km/h)
    sunshine    : whether meaningful sunshine is expected in that window

All parameter identifiers here were confirmed against live probes and the
MeteoSwiss metadata CSVs (see project notes), not guessed:

    app plzDetail    gustSpeed1h   (km/h, hourly)   sunshine1h  (min/h, hourly)
    ogd-local-fcst   fu3010h1      (gust 1s, km/h)  sre000h0    (sunshine, h)
    open-meteo ICON  wind_gusts_10m (km/h, hourly)

Every source returns None on failure rather than raising, so the caller can
fall back and record which source actually answered.
"""

import csv
import hashlib
import io
import math
from datetime import datetime, timezone, timedelta

import requests

import events

APP = "https://app-prod-ws.meteoswiss-app.ch"
STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-local-forecasting")
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

UA = "swiss-meteo-shade/1.0 (Home Assistant add-on)"
MISSING_APP = 32767          # MaxInt16 sentinel used by the app endpoint

# How many hours ahead the outlook looks. Gust: the worst in this window drives
# the retract decision. Sunshine: any good hour in this window counts as sunny.
LOOKAHEAD_H = 2


def _session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s




# ---------------------------------------------------------------------------
# 1. App plzDetail  -- optional fallback / opt-in primary (gust + sunshine).
#    Undocumented app endpoint; used only when prefer_app=True, or when the
#    official source below is unavailable.
# ---------------------------------------------------------------------------
def from_app(plz, session=None, lookahead_h=LOOKAHEAD_H):
    """Return {'gust_kmh':float, 'sunshine':bool} from the app, or None."""
    s = session or _session()
    try:
        r = s.get(f"{APP}/v1/plzDetail", params={"plz": f"{plz}00"}, timeout=25)
        r.raise_for_status()
        graph = r.json().get("graph", {})
    except (requests.RequestException, ValueError):
        return None

    # Bug 6 (verified 2026-07-26 via timing_probe): the 144-long HOURLY arrays
    # are anchored to graph['start'] (local midnight), NOT startLowResolution
    # (which belongs to the low-res 3-hourly series). Index 0 = 'start'; the
    # current hour is (now - start) hours in. _app_window_slice reads the
    # window around now from that anchor.
    start_ms = graph.get("start")
    gust = _app_window_slice(graph.get("gustSpeed1h", []), start_ms, lookahead_h)
    sun = _app_window_slice(graph.get("sunshine1h", []), start_ms, lookahead_h)
    temp = _app_window_slice(graph.get("temperatureMean1h", []), start_ms,
                             lookahead_h)
    if not gust and not sun:
        return None

    return {
        "gust_kmh": max(gust) if gust else None,
        "sunshine": (None if not sun else True),   # known vs unknown; shade thresholds
        "sunshine_minutes": max(sun) if sun else None,
        "temp_c": min(temp) if temp else None,                # A4: min, not max
    }


def _app_window_slice(arr, start_ms, lookahead_h):
    """Values covering now .. now+lookahead_h from an hourly app series anchored
    at epoch `start_ms` (index 0 = start). If start is missing, fall back to the
    first hours. If now precedes the series (rare pre-dawn edge), start at 0.

    IMPORTANT: index into the RAW array by time FIRST, then drop missing values
    -- filtering before slicing would compress the array and break the 1:1
    index-to-hour mapping, pulling the wrong forecast hour."""
    if not arr:
        return []
    if not start_ms:
        window = arr[:lookahead_h + 1]
    else:
        base = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        i0 = int((now - base).total_seconds() // 3600)
        i0 = max(0, min(i0, len(arr) - 1))     # clamp into range
        window = arr[i0:i0 + lookahead_h + 1]
    # drop missing values ONLY within the already-correct time window
    return [v for v in window if v is not None and v != MISSING_APP]


# ---------------------------------------------------------------------------
# 2. Official ogd-local-forecasting  -- DEFAULT primary forecast source.
#    Documented, licensed federal open data. Preferred for durability.
# ---------------------------------------------------------------------------
_POINT_META = ("https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/"
               "ogd-local-forecasting_meta_point.csv")


# Cache the resolved point across cycles, revalidated by ETag: the metadata
# CSV (~800 KB, ~5600 rows) is static but not immutable (points get added or
# renumbered). A conditional GET returns 304 (near-zero bytes) when unchanged,
# so we recompute only when the file actually changes -- cheap AND correct.
_point_cache = {"key": None, "etag": None, "point_id": None}


def _find_forecast_point(session, lv95_e, lv95_n):
    """Nearest forecast point_id, cached and ETag-revalidated across cycles."""
    key = (round(lv95_e), round(lv95_n))
    c = _point_cache
    headers = {}
    if c["point_id"] is not None and c["key"] == key and c["etag"]:
        headers["If-None-Match"] = c["etag"]

    try:
        r = session.get(_POINT_META, timeout=30, headers=headers)
    except requests.RequestException:
        return c["point_id"]              # network blip: keep cached value
    if r.status_code == 304:
        return c["point_id"]              # unchanged -> cached point still valid
    if not r.ok:
        return c["point_id"]

    text = r.content.decode("latin-1")   # MeteoSwiss CSVs are ISO-8859-1
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    if not rows:
        return c["point_id"]
    best, bd = None, 1e18
    for row in rows:
        try:
            d = math.hypot(float(row["point_coordinates_lv95_east"]) - lv95_e,
                           float(row["point_coordinates_lv95_north"]) - lv95_n)
        except (ValueError, TypeError, KeyError):
            continue
        if d < bd:
            best, bd = (row["point_id"], row.get("point_type_id", "")), d
    _point_cache.update(key=key, etag=r.headers.get("ETag"), point_id=best)
    return best


def _item_has(assets, params):
    """True if the assets dict contains a CSV for every required param code."""
    names = list(assets)
    return all(any(f".{p}." in n and n.endswith(".csv") for n in names)
               for p in params)


def _newest_item_with(session, params):
    """Assets of the newest item that contains ALL of `params`.

    Items are listed oldest-first and dated by forecast target date. Today's
    item can exist but be only PARTIALLY populated just after the run starts
    (some files present, the one we need not yet). So "newest with any assets"
    is not enough -- we require the specific files, and fall back to the newest
    fully-populated older item otherwise.
    """
    r = session.get(f"{STAC}/items", params={"limit": 10}, timeout=30)
    if not r.ok:
        return None
    feats = r.json().get("features", [])
    feats.sort(key=lambda f: f.get("id", ""), reverse=True)   # newest first
    for f in feats:
        assets = f.get("assets", {})
        if assets and _item_has(assets, params):
            return assets
    return None


# --- forecast file cache -----------------------------------------------------
# Per-URL cache to avoid re-downloading the ~30 MB CSVs every cycle.
#   fast path  : conditional GET (If-None-Match). 304 -> skip download, reuse
#                the cached ALL-POINTS parse for this file.
#   safety net : if the cache is older than FORECAST_MAX_CACHE_MINUTES, force a
#                full download regardless of ETag -- a clock WE own, so we never
#                depend on the server's freshness claims being honest.
#   integrity  : on a forced download, hash the body; if the server implied the
#                file was unchanged (same ETag) but the hash differs, the CDN's
#                freshness signal lied -> log ERROR and use the fresh data.
#
# We cache the parsed rows for OUR point across the parameters of one file? No:
# each param is a separate file, so the cache is keyed by URL and stores the
# raw-but-filtered lines for our point is not possible (point varies). Instead
# we cache the extracted per-point series keyed by (url, point_id), plus the
# file-level etag/hash/time keyed by url.
FORECAST_MAX_CACHE_MINUTES = 60
# Per-output sunshine thresholds now live in shade.py (sun_min_*). Here we
# only decide known-vs-unknown: sunshine is None if no data, else the peak
# minutes (shade applies each output's threshold).
NEED_TEMP = False   # set True by shade when a min-temp gate is configured

_file_meta = {}    # url -> {"etag": str|None, "hash": str, "time": datetime}
_file_series = {}  # (url, point_id) -> list[(date_str, value_str)] for that point


def _parse_body_for_point(body_bytes, point_ref, param):
    """Extract [(date, value)] rows for one point from a raw CSV body.

    point_ref is (point_id, point_type_id). MeteoSwiss point_id is only unique
    WITHIN a point_type_id, so we match on BOTH columns -- otherwise a station
    and a postcode sharing an id would collide and the early-break could grab
    the wrong location entirely. Rows are kept as (date, value) strings so the
    time window can be re-applied each cycle without re-downloading."""
    want_id, want_type = (str(point_ref[0]), str(point_ref[1])) \
        if isinstance(point_ref, (tuple, list)) else (str(point_ref), None)
    text = body_bytes.decode("latin-1")   # MeteoSwiss CSVs are ISO-8859-1
    lines = text.splitlines()
    if not lines:
        return None
    cols = lines[0].split(";")
    try:
        i_id = cols.index("point_id")
        i_date = cols.index("Date")
        i_val = cols.index(param)
    except ValueError:
        return None
    i_type = cols.index("point_type_id") if "point_type_id" in cols else None
    # Scan the whole file rather than breaking at the first block boundary. A
    # full scan of ~1.2M rows is ~160 ms and only happens on a cache MISS (when
    # the file actually changed, a few times a day), so the cost is negligible
    # -- and it removes any dependency on the rows for one point being
    # contiguous, which is not a documented guarantee.
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        f = line.split(";")
        if len(f) <= i_val:
            continue
        if f[i_id] == want_id and (
                want_type is None or i_type is None or f[i_type] == want_type):
            rows.append((f[i_date], f[i_val]))
    return rows


def _checked_sun(minutes):
    """sre000h0 is sunshine MINUTES per hour (verified against live data: values
    of 11, 30 and 55 were observed, which only make sense as minutes). Anything
    above 60 would mean the unit changed underneath us and every sun_min_*
    threshold would be wrong, so flag it loudly rather than silently misjudging."""
    if minutes is not None and minutes > 60:
        events.error(f"sunshine forecast {minutes} exceeds 60 min/h -- the unit "
                     f"of sre000h0 may have changed; sun thresholds unreliable")
    return minutes


def _window_from_rows(rows, lookahead_h, back_hours=0):
    """Apply the current UTC time window to cached (date, value) rows.

    back_hours is how far BEFORE now the window reaches: 1 for gust/temp (an
    already-started hour can still bring a damaging gust), but 0 for sunshine
    (A8) -- an elapsed hour must not set sun=True for a time already past."""
    if not rows:
        return None
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=lookahead_h)
    # B1: rows are timestamped at the START of their hour, so subtracting whole
    # hours from `now` drops the hour currently in progress (at 17:32 a lower
    # bound of 17:32 excludes the 17:00 row, though 28 min of it are still
    # ahead). Floor to the hour first: back_hours=0 then means "the hour we are
    # in, plus the look-ahead", which is what every source should use.
    lower = (now.replace(minute=0, second=0, microsecond=0)
             - timedelta(hours=back_hours))
    vals = []
    for date_str, val_str in rows:
        try:
            dt = datetime.strptime(date_str[:10], "%Y%m%d%H").replace(
                tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        if lower <= dt <= horizon:
            try:
                v = float(val_str)
            except ValueError:
                continue
            if not math.isnan(v):        # never let NaN reach json.dumps
                vals.append(v)
    return vals or None


def _fetch_forecast_param(session, point_id, param, lookahead_h, assets,
                          back_hours=0):
    """Values of one parameter for our point, cached with conditional GET.

    Files are ~30 MB LONG format (point_id ; point_type_id ; Date ; <param>).
    We cache the per-point extracted rows and only re-download when the file
    changes (ETag) or the cache exceeds FORECAST_MAX_CACHE_MINUTES."""
    href = next((a["href"] for n, a in assets.items()
                 if f".{param}." in n and n.endswith(".csv")), None)
    if not href:
        return None

    key = (href, str(point_id))   # point_id may be (id, type) tuple
    now = datetime.now(timezone.utc)
    meta = _file_meta.get(href)
    age_min = ((now - meta["time"]).total_seconds() / 60.0
               if meta else float("inf"))
    # C1c: FORECAST_MAX_CACHE_MINUTES == 0 means "never force by timer" (rely
    # on the conditional GET alone), NOT "force every cycle".
    force = meta is None or (FORECAST_MAX_CACHE_MINUTES > 0
                             and age_min >= FORECAST_MAX_CACHE_MINUTES)

    headers = {}
    if meta and meta.get("etag") and not force:
        headers["If-None-Match"] = meta["etag"]

    try:
        r = session.get(href, timeout=90, headers=headers)
    except requests.RequestException:
        # network blip: serve cached rows if we have them, else give up
        rows = _file_series.get(key)
        return _window_from_rows(rows, lookahead_h, back_hours) if rows else None

    if r.status_code == 304 and not force:
        # unchanged -> reuse cached rows, just re-window against the clock
        return _window_from_rows(_file_series.get(key), lookahead_h, back_hours)

    if not r.ok:
        rows = _file_series.get(key)
        return _window_from_rows(rows, lookahead_h, back_hours) if rows else None

    body = r.content
    new_hash = hashlib.sha256(body).hexdigest()
    new_etag = r.headers.get("ETag")

    # integrity check: forced refresh + server implied unchanged + hash differs
    if force and meta and meta.get("etag") and new_etag == meta["etag"] \
            and new_hash != meta["hash"]:
        events.error(f"{param} file hash changed while ETag stayed {new_etag} "
                     f"-- CDN freshness signal unreliable; using fresh data")

    rows = _parse_body_for_point(body, point_id, param)
    _file_meta[href] = {"etag": new_etag, "hash": new_hash, "time": now}
    if rows is not None:
        _file_series[key] = rows
    return _window_from_rows(rows, lookahead_h, back_hours)


def from_official(lv95_e, lv95_n, session=None, lookahead_h=LOOKAHEAD_H):
    """Return {'gust_kmh':float, 'sunshine':bool} from official OGD, or None."""
    s = session or _session()
    try:
        point = _find_forecast_point(s, lv95_e, lv95_n)
        if not point:
            return None
        assets = _newest_item_with(s, ("fu3010h1", "sre000h0"))
        if not assets:
            return None
        gust = _fetch_forecast_param(s, point, "fu3010h1", lookahead_h, assets)
        sun = _fetch_forecast_param(s, point, "sre000h0", lookahead_h, assets)
        # A3: warn when a file was fetched but yielded nothing for our point --
        # otherwise a silent empty series looks like a confident "not sunny".
        if sun is None:
            events.warn(f"sunshine file had no rows for point {point} "
                        f"-> sunshine unknown this cycle")
        # C1b: only fetch temperature when the gate is actually on.
        temp = (_fetch_forecast_param(s, point, "tre200h0", lookahead_h, assets)
                if NEED_TEMP else None)
    except (requests.RequestException, ValueError):
        return None
    if gust is None and sun is None:
        return None
    return {
        "gust_kmh": max(gust) if gust else None,
        # None = unknown (no rows for our point) so it never masquerades as
        # "not sunny"; the actual per-output threshold is applied in shade.py
        # against sunshine_minutes.
        "sunshine": (None if not sun else True),
        "sunshine_minutes": _checked_sun(max(sun) if sun else None),
        "temp_c": min(temp) if temp else None,                # A4: coldest hour
    }


# ---------------------------------------------------------------------------
# 3. Open-Meteo ICON  -- independent gust source (better located)
# ---------------------------------------------------------------------------
def from_openmeteo(lat, lon, session=None, lookahead_h=LOOKAHEAD_H):
    """Return {'gust_kmh':float} from Open-Meteo ICON, or None. Gust only."""
    s = session or _session()
    try:
        r = s.get(OPEN_METEO, params={
            "latitude": round(lat, 4), "longitude": round(lon, 4),
            "hourly": "wind_gusts_10m", "wind_speed_unit": "kmh",
            "models": "meteoswiss_icon_ch1", "forecast_days": 2,
            "timezone": "UTC",
        }, timeout=25)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    gusts = hourly.get("wind_gusts_10m", [])
    if not times or not gusts:
        return None

    now = datetime.now(timezone.utc)
    window = []
    for t, g in zip(times, gusts):
        try:
            ft = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - timedelta(hours=1) <= ft <= now + timedelta(hours=lookahead_h):
            if g is not None:
                gv = float(g)
                if not math.isnan(gv):   # guard NaN before it reaches output
                    window.append(gv)
    if not window:
        return None
    return {"gust_kmh": max(window)}


# ---------------------------------------------------------------------------
# Orchestration: combine sources into the signals the awning logic needs
# ---------------------------------------------------------------------------
def gather(plz, lv95_e, lv95_n, lat, lon, use_openmeteo=True,
           prefer_app=False, lookahead_h=LOOKAHEAD_H, session=None,
           need_temp=None):
    """Return the merged forecast signals and provenance.

    gust_kmh is the MAX across every source that answered -- the cautious
    direction for a safety decision. The sunshine/primary-gust source is the
    official OGD product by default (documented, licensed, stable); set
    prefer_app=True to try the app's fresher-but-unofficial feed first.
    """
    global NEED_TEMP
    if need_temp is not None:
        NEED_TEMP = need_temp
    s = session or _session()
    result = {
        "forecast_source": None,      # which source gave sunshine/primary gust
        "gust_sources": {},           # per-source gust, for visibility
        "gust_kmh": None,
        "sunshine": None,
        "sunshine_minutes": None,
        "temp_c": None,
        "on_backup": False,
        "openmeteo_ok": None,
    }

    # primary forecast source. Default: official OGD (stable), app as fallback.
    # prefer_app flips the order for users who want the app's faster refresh.
    if prefer_app:
        fc = from_app(plz, s, lookahead_h)
        if fc is None:
            fc = from_official(lv95_e, lv95_n, s, lookahead_h)
            result["on_backup"] = fc is not None
            result["forecast_source"] = "official" if fc else None
        else:
            result["forecast_source"] = "app"
    else:
        fc = from_official(lv95_e, lv95_n, s, lookahead_h)
        if fc is None:
            fc = from_app(plz, s, lookahead_h)
            result["on_backup"] = fc is not None
            result["forecast_source"] = "app" if fc else None
        else:
            result["forecast_source"] = "official"

    if fc is not None:
        result["sunshine"] = fc.get("sunshine")
        result["sunshine_minutes"] = fc.get("sunshine_minutes")
        result["temp_c"] = fc.get("temp_c")
        if fc.get("gust_kmh") is not None:
            result["gust_sources"][result["forecast_source"]] = round(fc["gust_kmh"], 1)

    # independent Open-Meteo gust
    if use_openmeteo:
        om = from_openmeteo(lat, lon, s, lookahead_h)
        result["openmeteo_ok"] = om is not None
        if om and om.get("gust_kmh") is not None:
            result["gust_sources"]["openmeteo"] = round(om["gust_kmh"], 1)

    gusts = list(result["gust_sources"].values())
    result["gust_kmh"] = max(gusts) if gusts else None
    return result
