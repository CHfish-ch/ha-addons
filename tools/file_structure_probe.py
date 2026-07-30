#!/usr/bin/env python3
"""Resolve whether each forecast CSV is ONE valid-hour or ALL lead-times.

Filenames like ...202607250000.fu3010h1.csv, ...202607250100... suggest one
file per hour, but the current code assumes one file holds all lead-times as
rows. This downloads two hourly files and reports, for our point, how many
distinct Date values each contains.

    python3 file_structure_probe.py --point 604500
"""
import argparse
import csv
import io
import requests

BASE = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting"


def inspect(url, point, s):
    r = s.get(url, timeout=90)
    print("\nGET %s -> %s, %.2f MB" % (url.rsplit("/", 1)[-1], r.status_code,
                                       len(r.content) / 1e6))
    if not r.ok:
        return
    rows = list(csv.DictReader(
        io.StringIO(r.content.decode("latin-1")), delimiter=";"))
    print("  total rows: %d" % len(rows))
    if not rows:
        return
    print("  columns: %s" % list(rows[0].keys()))
    ours = [x for x in rows if x.get("point_id") == point]
    dates = sorted({x["Date"] for x in ours}) if ours else []
    print("  rows for point %s: %d" % (point, len(ours)))
    print("  distinct Date values for our point: %d" % len(dates))
    if dates:
        print("    %s%s" % (dates[:6], " ..." if len(dates) > 6 else ""))
    all_dates = sorted({x["Date"] for x in rows})
    print("  distinct Date values in whole file: %d" % len(all_dates))
    if all_dates:
        print("    range: %s .. %s" % (all_dates[0], all_dates[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", default="604500")
    ap.add_argument("--item", default="20260725-ch")
    args = ap.parse_args()
    s = requests.Session()
    base = "%s/%s/vnut12.lssw" % (BASE, args.item)
    day = args.item[:8]
    inspect("%s.%s0000.fu3010h1.csv" % (base, day), args.point, s)
    inspect("%s.%s0100.fu3010h1.csv" % (base, day), args.point, s)
    print("\nINTERPRETATION:")
    print("  1 distinct Date/file for our point -> PER-HOUR files (fetch one")
    print("     file per look-ahead hour; each is small).")
    print("  many distinct Dates -> current single-file approach is correct.")


if __name__ == "__main__":
    main()
