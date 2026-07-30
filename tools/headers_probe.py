#!/usr/bin/env python3
"""Check what freshness signals the forecast files expose, so we can avoid
re-downloading 30 MB when nothing changed.

Tests, on one real gust file:
  * response headers: ETag, Last-Modified, Cache-Control, Content-Length
  * a conditional GET with If-None-Match -> do we get 304?
  * a conditional GET with If-Modified-Since -> 304?
  * the STAC item's created/updated/expires timestamps

    pip install requests
    python3 headers_probe.py
"""

import requests
from datetime import datetime, timezone

STAC = ("https://data.geo.admin.ch/api/stac/v1/collections/"
        "ch.meteoschweiz.ogd-local-forecasting")


def main():
    s = requests.Session()

    # newest item that actually CONTAINS the gust file (not just any assets --
    # today's item can be present but only partially populated after the run)
    r = s.get(f"{STAC}/items", params={"limit": 10}, timeout=30)
    feats = r.json().get("features", [])
    feats.sort(key=lambda f: f.get("id", ""), reverse=True)
    item = href = None
    for f in feats:
        h = next((a["href"] for n, a in f.get("assets", {}).items()
                  if ".fu3010h1." in n and n.endswith(".csv")), None)
        if h:
            item, href = f, h
            break
    if not href:
        print("no item currently has the gust file; try again shortly.")
        return
    print("newest item WITH gust file:", item["id"])
    props = item.get("properties", {})
    for k in ("datetime", "created", "updated", "expires"):
        print(f"  {k}: {props.get(k)}")
    print("\ngust file:", href.rsplit("/", 1)[-1])

    # 1. HEAD -- see the caching headers without downloading the body
    h = s.head(href, timeout=30)
    print(f"\nHEAD -> {h.status_code}")
    for k in ("ETag", "Last-Modified", "Cache-Control", "Content-Length",
              "Expires", "Age", "x-amz-meta-crc32"):
        if k in h.headers:
            print(f"  {k}: {h.headers[k]}")
    etag = h.headers.get("ETag")
    lastmod = h.headers.get("Last-Modified")
    size_mb = int(h.headers.get("Content-Length", 0)) / 1e6
    print(f"  (body is {size_mb:.1f} MB -- this is what we avoid re-fetching)")

    # 2. conditional GET with If-None-Match
    if etag:
        c = s.get(href, headers={"If-None-Match": etag}, timeout=30, stream=True)
        body = 0
        for chunk in c.iter_content(8192):
            body += len(chunk)
        print(f"\nIf-None-Match -> {c.status_code}  (downloaded {body} bytes)")
        print("  => 304 with ~0 bytes means conditional GET WORKS"
              if c.status_code == 304 else "  => full body returned; ETag not honoured")

    # 3. conditional GET with If-Modified-Since
    if lastmod:
        c = s.get(href, headers={"If-Modified-Since": lastmod}, timeout=30,
                  stream=True)
        body = sum(len(x) for x in c.iter_content(8192))
        print(f"If-Modified-Since -> {c.status_code}  (downloaded {body} bytes)")

    print("\nVerdict:")
    print("  If 304s work, the add-on can issue a cheap conditional GET every")
    print("  cycle and only pay the 30 MB when the file truly changed --")
    print("  no fixed TTL, no stale data, minimal bandwidth.")


if __name__ == "__main__":
    main()
