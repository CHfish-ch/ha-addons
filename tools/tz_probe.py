#!/usr/bin/env python3
"""Determine whether the forecast file's `Date` column is UTC or Swiss local.

Strategy: compare the forecast's SUNSHINE series against the sun's real
position. Sunshine can only be non-zero between sunrise and sunset. At this
time of year in Switzerland (CEST, UTC+2) the sun is up roughly 04:00-19:30
LOCAL = 02:00-17:30 UTC. If the first non-zero sunshine hour in the file lines
up with local-time daylight, the column is local; if with UTC daylight, it's
UTC. The gap is 2h now, so it is easy to see.

    pip install requests
    python3 tz_probe.py --point 604500
"""

import argparse
import csv
import io
from datetime import datetime, timezone, timedelta

import requests

STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-local-forecasting")


def newest_with(session, param):
    r = session.get(f"{STAC}/items", params={"limit": 10}, timeout=30)
    feats = sorted(r.json().get("features", []),
                   key=lambda f: f.get("id", ""), reverse=True)
    for f in feats:
        h = next((a["href"] for n, a in f.get("assets", {}).items()
                  if f".{param}." in n and n.endswith(".csv")), None)
        if h:
            return f.get("id"), h, f.get("properties", {})
    return None, None, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", default="604500")
    args = ap.parse_args()
    s = requests.Session()

    item_id, href, props = newest_with(s, "sre000h0")
    print(f"item {item_id}  run datetime {props.get('datetime')}")
    print(f"file {href.rsplit('/',1)[-1]}\n")

    # stream the sunshine file, collect our point's 24h of values
    want = args.point
    series = []
    with s.get(href, timeout=60, stream=True) as rc:
        rc.encoding = "utf-8"
        it = rc.iter_lines(decode_unicode=True)
        cols = next(it).lstrip("\ufeff").split(";")
        i_id, i_date, i_val = (cols.index("point_id"), cols.index("Date"),
                               cols.index("sre000h0"))
        seen = False
        for line in it:
            if not line:
                continue
            f = line.split(";")
            if f[i_id] != want:
                if seen:
                    break
                continue
            seen = True
            series.append((f[i_date], f[i_val]))

    if not series:
        print(f"point {want} not found"); return

    print(f"first 30 hourly sunshine values for point {want}:")
    print(f"{'Date(file)':14} {'sun_min':>7}  {'as-if-UTC->localCH':>20}")
    first_sun = None
    for date, val in series[:30]:
        dt = datetime.strptime(date[:10], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        local = dt + timedelta(hours=2)   # CEST now
        flag = ""
        try:
            if float(val) > 0 and first_sun is None:
                first_sun = date
                flag = "  <- first sun"
        except ValueError:
            pass
        print(f"{date:14} {val:>7}  {local:%Y-%m-%d %H:%M} CEST{flag}")

    print("\nINTERPRETATION:")
    print("  Sunrise in central CH right now is ~06:00 LOCAL (CEST) = 04:00 UTC.")
    print("  * If first non-zero sunshine is at file-hour ~04-05 -> column is UTC")
    print("    (04 UTC = 06 local sunrise). Code's tzinfo=utc is CORRECT.")
    print("  * If first non-zero sunshine is at file-hour ~06-07 -> column is")
    print("    LOCAL time. Code must parse as Europe/Zurich, NOT utc.")


if __name__ == "__main__":
    main()
