#!/usr/bin/env python3
"""Score forecast precipitation against the radar, to decide whether any of it
is trustworthy enough to stand in when the radar is unavailable.

WHY THIS EXISTS
    The radar can go down for hours (MeteoSwiss stopped publishing for 4 h on
    2026-08-12). While it is out, `radar_fail_safe` is a blind constant. A
    forecast series would be better than a constant IF it does not cry wolf.

    An earlier round of this probe rejected the app feed for MISSING events.
    That was the wrong criterion: in a fallback role a miss degrades to "dry",
    which is exactly what `radar_fail_safe: false` already gives -- no worse
    than the baseline. The failure that actually costs you is a FALSE ALARM:
    the series claims rain, the radar sees none, and the awning comes in on a
    dry afternoon. That is what this scores.

WHAT IT COMPARES
    app  precipitation10m   mm per 10-min bucket   (x6 -> mm/h equivalent)
    app  precipitation1h    mm per hour
    ogd  rre150h0           mm per hour            (official, documented)
    ...against the radar's peak rate at your cell during the same hour.

BANDWIDTH
    Radar frames are ~30 MB. By default only hours where at least one series
    CLAIMS rain are checked, since those are the only ones that can produce a
    false alarm -- on a dry day that costs nothing. Pass --all-hours to also
    measure misses, which needs a frame for every hour and runs to hundreds of
    megabytes.

ACCUMULATING
    One run only sees ~16 h (how far back the forecast series reach). Results
    append to a JSON ledger, so running it daily builds a real sample instead
    of an anecdote. Two events are not evidence; twenty dry afternoons are.

    pip install requests numpy h5py
    python3 precip_probe.py --lv95 2669292.99,1211426.35
    python3 precip_probe.py --lv95 2669292.99,1211426.35 --all-hours
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import forecast                                               # noqa: E402
import radar                                                  # noqa: E402

APP = "https://app-prod-ws.meteoswiss-app.ch/v1/plzDetail"
IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
UA = "swiss-meteo-shade-probe/2.0"
MISSING_APP = 32767
SERIES = ("app_10min", "app_hourly", "official_hourly")

# Do not pronounce on a series until it has had a fair chance to fail. A week
# of dry afternoons says nothing about a rain forecast.
MIN_HOURS = 100
MIN_WET_HOURS = 10


def lookup_plz(e, n, s):
    try:
        r = s.get(IDENTIFY, params={
            "geometry": f"{e},{n}", "geometryType": "esriGeometryPoint",
            "layers": "all:ch.swisstopo-vd.ortschaftenverzeichnis_plz",
            "tolerance": 0, "sr": 2056,
            "mapExtent": f"{e-500},{n-500},{e+500},{n+500}",
            "imageDisplay": "100,100,96"}, timeout=20)
        for res in r.json().get("results", []):
            if res.get("attributes", {}).get("plz"):
                return str(res["attributes"]["plz"])
    except (requests.RequestException, ValueError):
        pass
    return None


def app_series(plz, s):
    """{hour_end: mm/h equivalent} for the two app precipitation arrays."""
    out = {"app_10min": {}, "app_hourly": {}}
    try:
        g = s.get(APP, params={"plz": f"{plz}00"}, timeout=25).json().get("graph", {})
    except (requests.RequestException, ValueError):
        return out
    start_ms = g.get("start")
    if not start_ms:
        return out
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)

    # 10-min buckets: take the wettest bucket in each hour and scale to mm/h,
    # so a brief intense shower is not averaged away into nothing.
    per_hour = defaultdict(float)
    for i, v in enumerate(g.get("precipitation10m", [])):
        if v in (None, MISSING_APP):
            continue
        t = start + timedelta(minutes=10 * i)
        hour_end = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        per_hour[hour_end] = max(per_hour[hour_end], float(v) * 6.0)
    out["app_10min"] = dict(per_hour)

    for i, v in enumerate(g.get("precipitation1h", [])):
        if v in (None, MISSING_APP):
            continue
        t = start + timedelta(hours=i)
        out["app_hourly"][t + timedelta(hours=1)] = float(v)
    return out


def official_series(lv95_e, lv95_n, s):
    """{hour_end: mm} from the documented rre150h0 product."""
    try:
        point = forecast._find_forecast_point(s, lv95_e, lv95_n)
        assets = forecast._newest_item_with(s, ("rre150h0",))
        if not point or not assets:
            return {}
        rows = forecast.hourly_series(s, point, "rre150h0", assets)
    except (requests.RequestException, ValueError):
        return {}
    return {t: float(v) for t, v in (rows or [])}


def radar_peak(hour_end, e, n, frames_per_hour, s, tol_km):
    """Peak radar rate at the cell during the hour ending `hour_end`, or None.

    Sampling rather than reading all 12 frames is a deliberate trade: it can
    only UNDER-report rain, which biases the score toward finding MORE false
    alarms. A series that scores well under that pessimism is genuinely good.
    """
    item = (hour_end - timedelta(hours=1)).strftime(radar.ITEM_ID_FORMAT)
    try:
        available = radar._item_assets(s, item)
    except RuntimeError:
        return None
    step = 60 // max(1, frames_per_hour)
    peak, seen = None, 0
    for k in range(frames_per_hour):
        t = hour_end - timedelta(hours=1) + timedelta(minutes=k * step + step // 2)
        t = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
        name = next((n_ for n_ in available
                     if radar.rzc_time_from_name(n_) == t), None)
        if not name:
            continue
        # NB radar._item_assets returns {name: href}, whereas forecast's
        # equivalent returns {name: {"href": ...}}. Indexing this one as if it
        # were the other silently produced "no radar" for every hour until a
        # bare except was narrowed enough to show it.
        try:
            field = radar.read_rzc(radar.download(available[name], s))
            row, col = radar.lv95_to_grid(e, n)
            val = radar.sample(field, row, col, tol_km)
        except (requests.RequestException, RuntimeError, OSError,
                ValueError, KeyError, TypeError) as exc:
            print(f"    ! {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        seen += 1
        peak = val if peak is None else max(peak, val)
    return peak if seen else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lv95", required=True, help="easting,northing")
    ap.add_argument("--plz", help="postcode (default: looked up)")
    ap.add_argument("--threshold", type=float, default=0.1,
                    help="mm/h counted as rain (default 0.1, as the add-on)")
    ap.add_argument("--frames-per-hour", type=int, default=2)
    ap.add_argument("--tolerance-km", type=int, default=1)
    ap.add_argument("--all-hours", action="store_true",
                    help="also check hours nobody forecast rain for, to count "
                         "MISSES -- costs a radar frame per hour")
    ap.add_argument("--ledger", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "precip_scores.json"))
    a = ap.parse_args()

    e, n = (float(x) for x in a.lv95.split(","))
    s = requests.Session()
    s.headers["User-Agent"] = UA
    plz = a.plz or lookup_plz(e, n, s)
    now = datetime.now(timezone.utc)

    series = app_series(plz, s) if plz else {"app_10min": {}, "app_hourly": {}}
    series["official_hourly"] = official_series(e, n, s)

    hours = sorted({h for d in series.values() for h in d if h <= now})
    if not hours:
        print("no past forecast hours available")
        return

    claimed = [h for h in hours
               if any(series[k].get(h, 0.0) >= a.threshold for k in SERIES)]
    todo = hours if a.all_hours else claimed
    print(f"position LV95 {e},{n}   postcode {plz or '-'}   threshold "
          f"{a.threshold} mm/h")
    print(f"{len(hours)} past forecast hours; {len(claimed)} with a rain claim; "
          f"checking {len(todo)} against radar "
          f"(~{len(todo) * a.frames_per_hour * 30} MB)")
    if not todo:
        print("\nNothing claimed rain -- no false alarms possible in this "
              "window. Re-run after a wet spell, or pass --all-hours.")

    ledger = {}
    if os.path.exists(a.ledger):
        try:
            ledger = json.load(open(a.ledger))
        except (OSError, ValueError):
            ledger = {}

    if todo:
        print(f"\n{'hour ending':>16} {'radar':>8} " +
              " ".join(f"{k:>15}" for k in SERIES))
    for h in todo:
        key = h.isoformat()
        if key in ledger:
            continue
        peak = radar_peak(h, e, n, a.frames_per_hour, s, a.tolerance_km)
        if peak is None:
            continue                       # no radar that hour -> unscoreable
        row = {"radar": peak,
               **{k: series[k].get(h) for k in SERIES}}
        ledger[key] = row
        vals = " ".join(
            f"{(row[k] if row[k] is not None else float('nan')):15.2f}"
            for k in SERIES)
        print(f"{h:%m-%d %H:%M}Z {peak:8.2f} {vals}")

    try:
        json.dump(ledger, open(a.ledger, "w"), indent=1, sort_keys=True)
    except OSError:
        pass

    # ---- cumulative scorecard -------------------------------------------
    print(f"\n=== cumulative over {len(ledger)} scored hours "
          f"({a.ledger}) ===")
    print(f"{'series':>16} {'hit':>5} {'miss':>5} {'FALSE ALARM':>12} "
          f"{'true dry':>9}   verdict")
    for k in SERIES:
        tp = fn = fp = tn = 0
        for row in ledger.values():
            f_val, r_val = row.get(k), row.get("radar")
            if f_val is None or r_val is None:
                continue
            f_rain, r_rain = f_val >= a.threshold, r_val >= a.threshold
            tp += f_rain and r_rain
            fp += f_rain and not r_rain
            fn += (not f_rain) and r_rain
            tn += (not f_rain) and not r_rain
        total = tp + fp + fn + tn
        if not total:
            print(f"{k:>16} {'-':>5} {'-':>5} {'-':>12} {'-':>9}   no data yet")
            continue
        rate = 100.0 * fp / (fp + tn) if (fp + tn) else 0.0
        # Refuse to conclude from a thin sample. The first round of this
        # investigation drew a firm conclusion from two events and got the
        # criterion wrong; a run of dry hours proves nothing about a series
        # that has never been given the chance to cry wolf.
        wet = tp + fn
        if total < MIN_HOURS or wet < MIN_WET_HOURS:
            need = []
            if total < MIN_HOURS:
                need.append(f"{MIN_HOURS - total} more hours")
            if wet < MIN_WET_HOURS:
                need.append(f"{MIN_WET_HOURS - wet} more wet hours")
            verdict = "inconclusive -- need " + " and ".join(need)
        elif fp == 0:
            verdict = "no false alarms yet -- usable"
        else:
            verdict = f"{rate:.0f}% of dry hours cry wolf"
        print(f"{k:>16} {tp:5d} {fn:5d} {fp:12d} {tn:9d}   {verdict}")
    print("\nA MISS is harmless in a fallback -- it degrades to 'dry', which is\n"
          "what radar_fail_safe:false already does. A FALSE ALARM is the cost:\n"
          "the awning comes in on a dry day. Judge on that column.\n"
          f"Run this daily; it accumulates. Needs {MIN_HOURS} hours including\n"
          f"{MIN_WET_HOURS} wet ones before it will call a verdict.")


if __name__ == "__main__":
    main()
