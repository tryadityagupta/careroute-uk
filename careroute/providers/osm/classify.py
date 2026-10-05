"""
providers/osm/classify.py — run matching.py over every imported row, once.

infra/geo/import.sh pipes the freshly imported rows through this module:

    psql "\\copy (SELECT osm_type, osm_id, tags ...) TO STDOUT (FORMAT csv)" \\
      | python -m careroute.providers.osm.classify \\
      | psql "\\copy geo_import.classified FROM STDIN (FORMAT csv)"

Input rows:   osm_type, osm_id, tags (jsonb as text)
Output rows:  one per element that is in_directory(), with the columns in
              COLUMNS. Elements that are not are simply not written, and the
              import's join drops them.

Stdlib only (csv, json, sys + matching.py), so the geo image needs python3
and this package's source, not the app's dependencies.
"""

from __future__ import annotations

import csv
import json
import sys

from careroute.providers.osm.matching import Facility, in_directory

# Order = column order of geo_import.classified in import.sh.
COLUMNS = ("osm_type", "osm_id", "dedupe_key", "name_lc", "facility", "is_pharmacy",
           "is_hospital", "is_narrow", "er_candidate", "tag_tokens",
           "spec_name", "spec_tag", "spec_any", "spec_tokens")


def pg_array(items) -> str:
    """A Postgres text[] literal; every element quoted, so commas, braces and
    spaces inside OSM values are safe."""
    out = []
    for s in items:
        out.append('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "{" + ",".join(out) + "}"


def _bool(b: bool) -> str:
    return "t" if b else "f"


def classify_row(osm_type: str, osm_id: str, tags: dict) -> list | None:
    if not in_directory(tags):
        return None
    f = Facility.from_tags(tags)
    by_name, by_tag = f.specialty_ids()
    return [
        osm_type, osm_id,
        f.dedupe_key,                       # None -> empty unquoted -> SQL NULL
        f.name.lower(),
        f.facility,
        _bool(f.is_pharmacy), _bool(f.is_hospital), _bool(f.is_narrow),
        _bool(f.is_er_candidate),
        len(f.tokens),
        pg_array(by_name), pg_array(by_tag),
        pg_array(dict.fromkeys(by_name + by_tag)),
        pg_array(f.tokens),
    ]


def main(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr) -> int:
    reader = csv.reader(stdin)
    writer = csv.writer(stdout, lineterminator="\n")
    seen = kept = 0
    for osm_type, osm_id, tags_json in reader:
        seen += 1
        row = classify_row(osm_type, osm_id, json.loads(tags_json))
        if row is not None:
            writer.writerow(row)
            kept += 1
    print(f"[classify] {seen} candidate rows, {kept} in the directory", file=stderr)
    return 0


if __name__ == "__main__":
    # Text mode with explicit UTF-8: OSM names are full Unicode (Kannada etc.).
    sys.stdin.reconfigure(encoding="utf-8", newline="")
    sys.stdout.reconfigure(encoding="utf-8", newline="")
    raise SystemExit(main())
