#!/usr/bin/env python3
"""Replay a past radar decision to explain a rain=true blip.

Downloads the RZC frames around a given UTC minute, decodes them exactly as the
add-on does, and prints what the decision logic actually saw: the value in your
cell, the maximum inside the tolerance box, the surrounding neighbourhood, the
estimated motion vector, and the value at each projected lead time. That is
enough to tell a real shower from an isolated speck of clutter.

    pip install numpy h5py requests
    python3 rain_forensics.py --at 2026-07-28T15:44 --east 2665512 --north 1211882

Notes
  * --at is UTC and is the time of the LOG LINE; the script pulls frames from
    30 min before to 10 min after so you can see the echo arrive and leave.
  * Values are mm/h. The add-on's default trigger is >= 0.1 mm/h, which is a
    very light drizzle -- low enough that clutter can reach it.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

# tools/ sits beside the add-on directory; put it on the path so `radar`
# imports cleanly however this script is invoked.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "swiss_meteo_shade")))

import radar                                                  # noqa: E402

STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-radar-precip/items")


def assets_for_day(session, day):
    """{filename: href} for one YYYYMMDD-ch item."""
    r = session.get(f"{STAC}/{day.strftime('%Y%m%d')}-ch", timeout=30)
    r.raise_for_status()
    return {n: a["href"] for n, a in r.json().get("assets", {}).items()
            if n.lower().endswith("h5") and "rzc" in n.lower()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", required=True, help="UTC time, e.g. 2026-07-28T15:44")
    ap.add_argument("--east", type=float, required=True)
    ap.add_argument("--north", type=float, required=True)
    ap.add_argument("--before", type=int, default=30, help="minutes before")
    ap.add_argument("--after", type=int, default=10, help="minutes after")
    ap.add_argument("--threshold", type=float, default=0.1)
    ap.add_argument("--tolerance-km", type=int, default=1)
    a = ap.parse_args()

    t0 = datetime.fromisoformat(a.at).replace(tzinfo=timezone.utc)
    t0 = t0.replace(minute=t0.minute - t0.minute % 5, second=0, microsecond=0)
    s = requests.Session()
    s.headers["User-Agent"] = "swiss-meteo-shade-forensics/1.0"

    # collect assets across the day(s) the window touches
    start = t0 - timedelta(minutes=a.before)
    end = t0 + timedelta(minutes=a.after)
    assets = {}
    for day in {start.date(), end.date()}:
        try:
            assets.update(assets_for_day(s, datetime.combine(
                day, datetime.min.time(), tzinfo=timezone.utc)))
        except requests.RequestException as e:
            print(f"  (could not list {day}: {e})")
    if not assets:
        raise SystemExit("No radar assets found for that date -- archive may "
                         "have rolled off, or the date is wrong.")

    # frames in the window, oldest first
    wanted = []
    t = start
    while t <= end:
        name = next((n for n in assets if radar.rzc_time_from_name(n) == t), None)
        if name:
            wanted.append((t, name, assets[name]))
        t += timedelta(minutes=5)
    if not wanted:
        raise SystemExit("No frames matched the window.")

    print(f"Replaying {len(wanted)} frames around {t0:%Y-%m-%d %H:%M} UTC")
    print(f"cell = LV95 E{a.east:.0f} N{a.north:.0f}   "
          f"threshold {a.threshold} mm/h   tolerance {a.tolerance_km} km\n")

    fields, origin = [], (None, None)
    for t, name, href in wanted:
        buf = radar.download(href, s)
        f = radar.read_rzc(buf)
        if origin == (None, None):
            buf.seek(0)
            origin = radar.read_grid_origin(buf)
        fields.append((t, name, f))

    row, col = radar.lv95_to_grid(a.east, a.north, *origin)
    print(f"grid cell: row={row} col={col}\n")
    print(f"{'time UTC':>9}  {'exact':>7}  {'max±1km':>8}  {'max±5km':>8}  "
          f"{'wet cells ±5km':>14}")
    print("-" * 58)
    for t, name, f in fields:
        exact = float(f[row, col]) if not np.isnan(f[row, col]) else float("nan")
        box1 = radar.sample(f, row, col, a.tolerance_km)
        w = radar._window(f, row, col, 5)
        finite = w[~np.isnan(w)]
        box5 = float(finite.max()) if finite.size else float("nan")
        wet = int((finite >= a.threshold).sum())
        print(f"{t:%H:%M}      {exact:7.3f}  {box1:8.3f}  {box5:8.3f}  "
              f"{wet:9d}/{finite.size}")

    # Motion + projection using ONLY frames available at the decision time.
    # (The first version of this script used the last three frames in the whole
    # window, including ones published AFTER the decision -- which gives a
    # motion vector the add-on never had.)
    avail = [(t, f) for t, _, f in fields if t <= t0]
    print(f"\nframes the add-on could have seen at {t0:%H:%M}: "
          f"{', '.join(t.strftime('%H:%M') for t, _ in avail[-3:])}")
    if len(avail) >= 3:
        last3 = [f for _, f in avail[-3:]]
        drow, dcol = radar.estimate_motion(last3, row, col)
        speed = float(np.hypot(drow, dcol)) * 12
        print(f"\nmotion from the last 3 frames: drow={drow:+.2f} dcol={dcol:+.2f}"
              f"  (~{speed:.0f} km/h)")
        cur = last3[-1]
        print("projected cells the add-on would sample:")
        for lead in (0, 5, 10):
            k = lead // 5
            r = int(round(row - k * drow))
            c = int(round(col - k * dcol))
            r = max(0, min(r, cur.shape[0] - 1))
            c = max(0, min(c, cur.shape[1] - 1))
            v = radar.sample(cur, r, c, a.tolerance_km)
            hit = "RAIN" if v >= a.threshold else "dry"
            print(f"  +{lead:2d} min -> cell ({r},{c})  max {v:.3f} mm/h   {hit}")

    # Where was the nearest real rain? A large motion vector samples cells far
    # away, so an echo tens of km off can be what actually triggered.
    cur = fields[-1][2] if fields else None
    for label, (t, _, f) in (("at decision time",
                              [x for x in fields if x[0] <= t0][-1]),):
        R = 50   # km
        w = radar._window(f, row, col, R)
        finite = np.where(np.isnan(w), -1.0, w)
        hits = np.argwhere(finite >= a.threshold)
        print(f"\nwide scan {label} ({t:%H:%M}), radius {R} km, "
              f"threshold {a.threshold}:")
        if hits.size == 0:
            print("  NO cell anywhere within 50 km reached the threshold.")
        else:
            centre = np.array([w.shape[0] // 2, w.shape[1] // 2])
            d = np.hypot(*(hits - centre).T)
            i = int(np.argmin(d))
            hr, hc = hits[i]
            print(f"  {len(hits)} cells >= threshold; nearest is "
                  f"{d[i]:.0f} km away, value {w[hr, hc]:.3f} mm/h")
            print(f"  offset from home: {hr - centre[0]:+d} rows, "
                  f"{hc - centre[1]:+d} cols")

    print("\nHow to read this:")
    print("  * a real shower shows a BLOCK of wet cells that grows/moves across")
    print("    consecutive frames;")
    print("  * 1-2 wet cells appearing for a single frame and vanishing is")
    print("    almost certainly clutter or a decayed echo, not rain;")
    print("  * values just over 0.1 mm/h are the lightest drizzle the product")
    print("    reports -- raising radar_threshold_mmh filters these out.")


if __name__ == "__main__":
    main()
