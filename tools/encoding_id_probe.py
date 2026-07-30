#!/usr/bin/env python3
"""Settle two review questions against live data:
  BUG 2 -- are point_id values unique globally, or only within point_type_id?
  BUG 3 -- are the CSVs utf-8 or latin-1 (ISO-8859-1)?

    pip install requests
    python3 encoding_id_probe.py
"""

import collections
import requests

META = ("https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/"
        "ogd-local-forecasting_meta_point.csv")


def main():
    raw = requests.get(META, timeout=60).content

    print("=" * 70, "\nBUG 3: encoding\n", "=" * 70, sep="")
    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            txt = raw.decode(enc)
            # count replacement chars / look for a known accented name
            repl = txt.count("\ufffd")
            has_zurich = "Zürich" in txt
            has_neuch = "Neuchâtel" in txt
            sample = next((ln for ln in txt.splitlines()
                           if "rich" in ln or "tel" in ln), "")[:80]
            print(f"{enc:10}: replacement_chars={repl}  Zürich={has_zurich}  "
                  f"Neuchâtel={has_neuch}")
            if sample:
                print(f"            sample: {sample!r}")
        except UnicodeDecodeError as e:
            print(f"{enc:10}: UnicodeDecodeError {e}")

    print("\n" + "=" * 70, "\nBUG 2: point_id uniqueness\n", "=" * 70, sep="")
    # decode with whichever worked; latin-1 never fails
    txt = raw.decode("latin-1")
    import csv, io
    rows = list(csv.DictReader(io.StringIO(txt), delimiter=";"))
    by_id = collections.Counter(r["point_id"] for r in rows)
    dupes = {pid: n for pid, n in by_id.items() if n > 1}
    print(f"total points: {len(rows)}")
    print(f"distinct point_id: {len(by_id)}")
    print(f"point_id values appearing more than once: {len(dupes)}")
    if dupes:
        print("  => point_id is NOT globally unique. Examples:")
        shown = 0
        for pid in dupes:
            matches = [(r["point_id"], r["point_type_id"], r["point_name"])
                       for r in rows if r["point_id"] == pid]
            print(f"    id {pid}: {matches}")
            shown += 1
            if shown >= 5:
                break
        print("  -> MUST match on (point_id, point_type_id) together.")
    else:
        print("  => point_id IS globally unique; matching on it alone is safe.")


if __name__ == "__main__":
    main()
