#!/usr/bin/env python3
"""Two things the C1a 403 raised:

  1. Does data.geo.admin.ch refuse HEAD for the RADAR bucket too? If so,
     radar.py's probe_newer (which uses HEAD to find fresher frames) is broken.
  2. Does conditional GET (If-None-Match) still work for the FORECAST files?
     That's what the cache relies on; the HEAD 403 doesn't necessarily affect it.

    python3 head_get_probe.py
"""
import requests

S = requests.Session()
S.headers["User-Agent"] = "swiss-meteo-shade-probe/1.0"
STAC = "https://data.geo.admin.ch/api/stac/v1/collections"


def newest_forecast_asset():
    r = S.get(f"{STAC}/ch.meteoschweiz.ogd-local-forecasting/items",
              params={"limit": 10}, timeout=30)
    feats = sorted(r.json().get("features", []),
                   key=lambda f: f.get("id", ""), reverse=True)
    for f in feats:
        for n, a in f.get("assets", {}).items():
            if ".fu3010h1." in n and n.endswith(".csv"):
                return a["href"]
    return None


def newest_radar_asset():
    r = S.get(f"{STAC}/ch.meteoschweiz.ogd-radar-precip/items",
              params={"limit": 5}, timeout=30)
    feats = sorted(r.json().get("features", []),
                   key=lambda f: f.get("id", ""), reverse=True)
    for f in feats:
        for n, a in f.get("assets", {}).items():
            if n.lower().endswith("h5") and "rzc" in n.lower():
                return a["href"]
    return None


def test(url, label):
    print(f"\n=== {label} ===\n{url.rsplit('/', 1)[-1]}")
    # HEAD
    try:
        h = S.head(url, timeout=20, allow_redirects=True)
        print(f"  HEAD -> {h.status_code}"
              + ("  (HEAD works)" if h.ok else "  (HEAD BLOCKED)"))
    except requests.RequestException as e:
        print(f"  HEAD -> error {e}")
    # GET (range: just 1 byte, to avoid pulling 32 MB)
    try:
        g = S.get(url, headers={"Range": "bytes=0-0"}, timeout=30, stream=True)
        etag = g.headers.get("ETag")
        print(f"  GET(range) -> {g.status_code}, ETag={etag}")
        g.close()
        # conditional GET with that ETag
        if etag:
            c = S.get(url, headers={"If-None-Match": etag}, timeout=30,
                      stream=True)
            body = sum(len(x) for x in c.iter_content(8192))
            print(f"  If-None-Match -> {c.status_code} ({body} bytes)"
                  + ("  (conditional GET WORKS)" if c.status_code == 304
                     else "  (NOT honoured)"))
    except requests.RequestException as e:
        print(f"  GET -> error {e}")


fa = newest_forecast_asset()
ra = newest_radar_asset()
if fa:
    test(fa, "FORECAST bucket")
if ra:
    test(ra, "RADAR bucket (probe_newer relies on HEAD here)")
print("\nSummary decides: (1) whether radar probe_newer needs HEAD->GET, "
      "(2) whether the forecast cache's conditional GET still works.")
