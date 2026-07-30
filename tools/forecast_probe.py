#!/usr/bin/env python3
"""Show the real structure of one ogd-local-forecasting parameter file, so we
can read the right column instead of guessing.

    pip install requests
    python3 forecast_probe.py --lv95 2669302,1211420
"""

import argparse
import csv
import io
import math
from datetime import datetime, timezone

import requests

BASE = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting"
STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-local-forecasting")


def get_csv(url, s):
    r = s.get(url, timeout=30)
    print(f"GET {url} -> {r.status_code} ({len(r.content)} B)")
    r.raise_for_status()
    return r.content.decode("utf-8-sig", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lv95", default="2669302,1211420")
    args = ap.parse_args()
    e, n = (float(x) for x in args.lv95.split(","))
    s = requests.Session()

    # 1. point metadata: how are points identified and located?
    print("=" * 70, "\nPOINT METADATA\n", "=" * 70, sep="")
    text = get_csv(f"{BASE}/ogd-local-forecasting_meta_point.csv", s)
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    print(f"rows: {len(rows)}")
    if rows:
        print("columns:", list(rows[0].keys()))
        print("first row:", dict(rows[0]))
        # find nearest point to our coords
        ek = next((k for k in rows[0] if "east" in k.lower()), None)
        nk = next((k for k in rows[0] if "north" in k.lower()), None)
        idk = list(rows[0].keys())[0]
        print(f"\ninferred: id={idk!r} east={ek!r} north={nk!r}")
        if ek and nk:
            best, bd = None, 1e18
            for row in rows:
                try:
                    d = math.hypot(float(row[ek]) - e, float(row[nk]) - n)
                except (ValueError, TypeError):
                    continue
                if d < bd:
                    best, bd = row, d
            print(f"nearest point: {dict(best)}  ({bd/1000:.1f} km)")
            point_id = best[idk]
        else:
            point_id = None

    # 1b. what items actually exist, and what assets does the newest hold?
    print("\n" + "=" * 70, "\nITEMS + ASSETS\n", "=" * 70, sep="")
    r = s.get(f"{STAC}/items", params={"limit": 10}, timeout=30)
    feats = r.json().get("features", []) if r.ok else []
    print(f"items returned: {len(feats)}")
    for f in feats:
        props = f.get("properties", {})
        print(f"  id={f.get('id')}  datetime={props.get('datetime')}  "
              f"assets={len(f.get('assets', {}))}")
    if feats:
        newest = feats[0]
        assets = newest.get("assets", {})
        print(f"\nnewest item {newest.get('id')} -- ALL asset param codes:")
        params_seen = sorted({nm.split('.')[-2] for nm in assets
                              if nm.endswith('.csv') and nm.count('.') >= 2})
        print(f"  {params_seen}")
        print(f"\n  sample asset names:")
        for nm in list(assets)[:6]:
            print(f"    {nm}")
        # look specifically for wind/gust/sun codes
        for want in ("fu3", "fkl", "sre", "gre"):
            match = [nm.split('.')[-2] for nm in assets if f".{want}" in nm]
            print(f"  codes containing {want!r}: {sorted(set(match))}")

    # 2. read the actual values from the newest POPULATED item
    populated = [f for f in feats if f.get("assets")]
    populated.sort(key=lambda f: f.get("id",""), reverse=True)
    if not populated:
        print("no populated items!"); return
    chosen = populated[0]
    assets = chosen.get("assets", {})
    print(f"\nusing newest populated item: {chosen.get('id')} ({len(assets)} assets)")
    for param in ("fu3010h1", "sre000h0"):
        print("\n" + "=" * 70, f"\nPARAMETER FILE: {param}\n", "=" * 70, sep="")
        href = next((a["href"] for nm, a in assets.items()
                     if f".{param}." in nm and nm.endswith(".csv")), None)
        if not href:
            names = [nm for nm in assets if param[:3] in nm][:5]
            print(f"no asset for {param}. similar: {names}")
            continue
        text = get_csv(href, s)
        lines = text.splitlines()
        print(f"\n--- first 6 raw lines ---")
        for ln in lines[:6]:
            print("  " + ln[:200])
        rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
        if rows:
            cols = list(rows[0].keys())
            print(f"\ncolumns ({len(cols)}): {cols[:12]}"
                  + (" ..." if len(cols) > 12 else ""))
            print(f"total rows (lead times): {len(rows)}")
            print("first row:", {k: rows[0][k] for k in cols[:6]})
            if point_id:
                print(f"\nour point id {point_id!r} in columns? "
                      f"{point_id in cols}")
                if point_id in cols:
                    series = [r[point_id] for r in rows[:6]]
                    print(f"first 6 values for our point: {series}")


if __name__ == "__main__":
    main()
