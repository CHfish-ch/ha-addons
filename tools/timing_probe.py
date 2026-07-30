#!/usr/bin/env python3
"""Resolve the two data-correctness unknowns before fixing the code:

  BUG 6 -- does plzDetail's hourly series start at LOCAL MIDNIGHT rather than
           now? If so, reading graph[...][:2] gives last night's values.
  BUG 5 -- does ch.meteoschweiz.ogd-local-forecasting keep more than 10 items
           in its rolling window? If so, limit=10 newest-first can miss today.

    pip install requests
    python3 timing_probe.py --plz 6006
"""

import argparse
from datetime import datetime, timezone, timedelta

import requests

APP = "https://app-prod-ws.meteoswiss-app.ch"
STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-local-forecasting")
UA = "swiss-meteo-shade-probe/1.0"


def probe_app_timing(session, plz):
    print("=" * 70, "\nBUG 6: plzDetail series alignment\n", "=" * 70, sep="")
    r = session.get(f"{APP}/v1/plzDetail", params={"plz": f"{plz}00"}, timeout=25)
    r.raise_for_status()
    g = r.json().get("graph", {})

    now = datetime.now(timezone.utc)
    print(f"now (UTC)            : {now:%Y-%m-%d %H:%M}")
    print(f"now (local CH approx): {now + timedelta(hours=2):%Y-%m-%d %H:%M} "
          f"(CEST; +1 in winter)\n")

    for key in ("start", "startLowResolution"):
        v = g.get(key)
        if v is not None:
            dt = datetime.fromtimestamp(v / 1000, tz=timezone.utc)
            print(f"{key:20}: {v}  ->  {dt:%Y-%m-%d %H:%M} UTC "
                  f"= {dt + timedelta(hours=2):%H:%M} local")
    print()

    # For each hourly series, work out which real time index 0 corresponds to,
    # using whichever 'start' the array length implies.
    start_ms = g.get("start")
    start_lo = g.get("startLowResolution", start_ms)
    for key in ("gustSpeed1h", "sunshine1h", "windSpeed1h", "temperatureMean1h"):
        arr = g.get(key)
        if not arr:
            continue
        # 1h arrays are typically 144 long; low-res start applies to some
        base_ms = start_lo if len(arr) <= 145 else start_ms
        base = datetime.fromtimestamp(base_ms / 1000, tz=timezone.utc)
        # find index nearest to "now"
        idx_now = round((now - base).total_seconds() / 3600)
        print(f"{key} (len {len(arr)}):")
        print(f"  series starts    : {base:%Y-%m-%d %H:%M} UTC")
        print(f"  index 0 value    : {arr[0]}")
        print(f"  index for 'now'  : {idx_now}"
              + (f"  value there: {arr[idx_now]}" if 0 <= idx_now < len(arr)
                 else "  (out of range)"))
        print(f"  first 4 values   : {arr[:4]}")
        print(f"  => reading [:2] gives times "
              f"{base:%H:%M}-{base + timedelta(hours=1):%H:%M} UTC "
              f"{'(WRONG: not now!)' if idx_now >= 2 else '(≈now, ok)'}\n")


def probe_item_window(session):
    print("=" * 70, "\nBUG 5: forecast item window size\n", "=" * 70, sep="")
    for lim in (10, 100):
        r = session.get(f"{STAC}/items", params={"limit": lim}, timeout=30)
        if not r.ok:
            print(f"limit={lim} -> HTTP {r.status_code}")
            continue
        feats = r.json().get("features", [])
        ids = sorted(f.get("id", "") for f in feats)
        populated = [f.get("id") for f in feats if f.get("assets")]
        print(f"limit={lim}: returned {len(feats)} items")
        print(f"  ids: {ids}")
        print(f"  with assets: {sorted(populated)}")
        # is today present?
        today = datetime.now(timezone.utc).strftime("%Y%m%d") + "-ch"
        print(f"  today's id {today} present: {today in ids}"
              f"  populated: {today in populated}\n")

    # does sortby help?
    r = session.get(f"{STAC}/items",
                    params={"limit": 5, "sortby": "-datetime"}, timeout=30)
    if r.ok:
        ids = [f.get("id") for f in r.json().get("features", [])]
        print(f"sortby=-datetime, limit=5 -> {ids}")
        print("  (if this puts newest first, we can rely on it instead of "
              "fetching a big window)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plz", default="6006")
    args = ap.parse_args()
    s = requests.Session()
    s.headers["User-Agent"] = UA

    try:
        probe_app_timing(s, args.plz)
    except Exception as e:
        print("app timing probe failed:", e)
    try:
        probe_item_window(s)
    except Exception as e:
        print("item window probe failed:", e)

    print("\nPaste the whole output back to finalise the bug 5 / bug 6 fixes.")


if __name__ == "__main__":
    main()
