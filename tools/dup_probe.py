#!/usr/bin/env python3
"""Bug 2 follow-up: some point_ids repeat WITHIN the same point_type_id
(e.g. id 123 'Seengen' appears twice as type 1). Are these identical rows
(harmless) or distinct locations sharing a full (id, type) key (a real
ambiguity our matcher can't resolve)?

    python3 dup_probe.py
"""

import collections
import csv
import io

import requests

META = ("https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/"
        "ogd-local-forecasting_meta_point.csv")


def main():
    raw = requests.get(META, timeout=60).content
    rows = list(csv.DictReader(
        io.StringIO(raw.decode("latin-1")), delimiter=";"))

    # group by (point_id, point_type_id)
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["point_id"], r["point_type_id"])].append(r)

    dup_keys = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"(point_id, point_type_id) pairs that map to >1 row: {len(dup_keys)}")

    identical = 0
    distinct = 0
    for key, members in dup_keys.items():
        coords = {(m["point_coordinates_lv95_east"],
                   m["point_coordinates_lv95_north"]) for m in members}
        names = {m["point_name"] for m in members}
        if len(coords) == 1 and len(names) == 1:
            identical += 1
        else:
            distinct += 1
            if distinct <= 8:
                print(f"\n  DISTINCT rows share key {key}:")
                for m in members:
                    print(f"    {m['point_name']!r}  "
                          f"E{m['point_coordinates_lv95_east']} "
                          f"N{m['point_coordinates_lv95_north']}")

    print(f"\nsummary: {identical} keys are exact duplicates (harmless),")
    print(f"         {distinct} keys map to genuinely DIFFERENT locations.")
    if distinct == 0:
        print("=> same-key duplicates are identical rows. Taking the first is")
        print("   safe; no code change needed beyond the (id,type) match.")
    else:
        print("=> some (id,type) keys are ambiguous. Matching must also use")
        print("   coordinates (pick the row nearest our target) to be correct.")


if __name__ == "__main__":
    main()
