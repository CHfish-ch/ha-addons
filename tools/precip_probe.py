#!/usr/bin/env python3
"""Compare the MeteoSwiss app's precipitation forecast against the radar, to
settle whether the app feed could stand in for the radar during an outage.

The radar is the ground truth here: it is an observation (RZC composite,
1 km / 5 min) at your exact cell. The app's plzDetail feed is a forecast at
postcode resolution. This script puts both side by side for the same place
and moment, so the comparison is measured rather than assumed.

    pip install requests numpy h5py
    python3 precip_probe.py --lv95 2670793.84,1193869.21
    python3 precip_probe.py --lv95 2670793.84,1193869.21 --plz 6386

Without --plz the postcode is looked up from the coordinates via
api3.geo.admin.ch.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

# tools/ sits beside the add-on directory; put it on the path so `radar`
# imports cleanly however this script is invoked.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import radar                                                  # noqa: E402

APP = "https://app-prod-ws.meteoswiss-app.ch/v1/plzDetail"
IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
UA = "swiss-meteo-shade-probe/1.0"
MISSING_APP = 32767


def lookup_plz(e, n, s):
    """Reverse-geocode LV95 coordinates to a Swiss postcode."""
    r = s.get(IDENTIFY, params={
        "geometry": f"{e},{n}", "geometryType": "esriGeometryPoint",
        "layers": "all:ch.swisstopo-vd.ortschaftenverzeichnis_plz",
        "tolerance": 0, "sr": 2056,
        "mapExtent": f"{e-500},{n-500},{e+500},{n+500}",
        "imageDisplay": "100,100,96",
    }, timeout=20)
    r.raise_for_status()
    for res in r.json().get("results", []):
        plz = res.get("attributes", {}).get("plz")
        if plz:
            return str(plz)
    return None


def app_precip(plz, s):
    """Return the app's precipitation series around now, with its anchor.

    Both precipitation10m and precipitation1h are anchored to graph['start']
    (local midnight) -- the same anchor already verified for the hourly gust
    and sunshine arrays. precipitation10m spans start..startLowResolution.
    """
    r = s.get(APP, params={"plz": f"{plz}00"}, timeout=25)
    r.raise_for_status()
    g = r.json().get("graph", {})
    start_ms = g.get("start")
    if not start_ms:
        return None
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    out = {"start": start, "now": now}
    for field, step in (("precipitation10m", 600), ("precipitation1h", 3600)):
        arr = g.get(field, [])
        if not arr:
            out[field] = None
            continue
        i0 = int((now - start).total_seconds() // step)
        i0 = max(0, min(i0, len(arr) - 1))
        window = []
        for i in range(i0, min(len(arr), i0 + 3)):
            v = arr[i]
            t = datetime.fromtimestamp(start.timestamp() + i * step,
                                       tz=timezone.utc)
            window.append((t, None if v in (None, MISSING_APP) else v))
        nz = [(i, v) for i, v in enumerate(arr)
              if v not in (None, 0, 0.0, MISSING_APP)]
        out[field] = {
            "len": len(arr), "i0": i0, "window": window,
            "nonzero": [(datetime.fromtimestamp(start.timestamp() + i * step,
                                                tz=timezone.utc), v)
                        for i, v in nz],
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lv95", required=True,
                    help="easting,northing in LV95, e.g. 2670793,1193869")
    ap.add_argument("--plz", help="postcode (default: look up from --lv95)")
    ap.add_argument("--threshold", type=float, default=0.1,
                    help="radar mm/h threshold (default 0.1)")
    a = ap.parse_args()

    e, n = (float(x) for x in a.lv95.split(","))
    s = requests.Session()
    s.headers["User-Agent"] = UA

    plz = a.plz or lookup_plz(e, n, s)
    print(f"position LV95 {e},{n}   postcode {plz or '(not found)'}")

    print("\n--- radar (observation, 1 km cell, ground truth) ---")
    radar.POS_LV95 = (e, n)
    radar.POS_WGS84 = None
    radar.THRESHOLD_MMH = a.threshold
    radar.FORECAST_TOLERANCE_KM = 1
    rad = radar.evaluate(session=s)
    print(f"  frame {rad.get('radar_time')} (age {rad.get('age_min')} min)")
    print(f"  rate now   {rad.get('rate_now_mmh')} mm/h  -> rain={rad.get('now')}")
    print(f"  rate +5    {rad.get('rate_t5_mmh')} mm/h")
    print(f"  rate +10   {rad.get('rate_t10_mmh')} mm/h")
    print(f"  field: {rad.get('field_wet_cells')} wet cells, "
          f"max {rad.get('field_max_mmh')} mm/h")

    if not plz:
        print("\nno postcode -- skipping the app comparison")
        return

    print("\n--- app plzDetail (forecast, postcode resolution) ---")
    ap_data = app_precip(plz, s)
    if not ap_data:
        print("  app feed unavailable")
        return
    print(f"  graph start {ap_data['start']}  now {ap_data['now']:%Y-%m-%d %H:%M}")
    for field in ("precipitation10m", "precipitation1h"):
        d = ap_data.get(field)
        if not d:
            print(f"  {field}: absent")
            continue
        print(f"  {field} (len {d['len']}, current index {d['i0']}):")
        for t, v in d["window"]:
            print(f"     {t:%H:%M}  {v}")
        nz = d["nonzero"]
        if nz:
            print(f"     non-zero anywhere in series ({len(nz)}): "
                  + ", ".join(f"{t:%H:%M}={v}" for t, v in nz[:8]))
        else:
            print("     non-zero anywhere in series: none")

    print("\n--- verdict ---")
    rate = rad.get("rate_now_mmh")
    p10 = ap_data.get("precipitation10m")
    p1h = ap_data.get("precipitation1h")
    cur10 = p10["window"][0][1] if p10 and p10["window"] else None
    cur1h = p1h["window"][0][1] if p1h and p1h["window"] else None

    # The app buckets are ACCUMULATION (mm within the bucket), not a rate, so
    # a 10-min bucket must be x6 to compare against the radar's mm/h.
    as_rate = None if not cur10 else cur10 * 6
    if as_rate is not None:
        print(f"  app 10-min bucket {cur10} mm/10min -> {as_rate:.1f} mm/h "
              f"equivalent (radar reads {rate} mm/h)")

    if rate is None:
        print("  radar gave no rate; inconclusive")
    elif rate >= a.threshold and not cur10:
        print(f"  MISS: radar sees rain ({rate} mm/h) but app "
              f"precipitation10m reads {cur10} -- app not tracking this event.")
    elif rate >= a.threshold and cur10:
        print(f"  HIT: both see rain (radar {rate} mm/h, app {as_rate:.1f} mm/h eq)")
    else:
        print(f"  radar dry ({rate} mm/h) -- run this during rain to be useful")

    # The two app series are independent products and have been observed to
    # contradict each other; report it rather than silently trusting one.
    if cur10 is not None and cur1h is not None:
        if bool(cur10) != bool(cur1h):
            print(f"  WARNING: app series disagree -- precipitation10m={cur10}, "
                  f"precipitation1h={cur1h} for the same moment.")


if __name__ == "__main__":
    main()
