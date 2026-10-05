"""
tests/test_classify.py — the import-time classifier, without a database.

classify.py runs matching.py once per row during infra/geo/import.sh; the
pushdown queries trust its columns. These checks need no PostGIS, so they run
in memory mode on every platform (test_geo_pushdown.py proves the columns
give identical answers end to end, when PostGIS is available).
"""

import csv
import io
import json
import re
import subprocess
import sys

from careroute.config import PROJECT_ROOT
from careroute.providers.osm.classify import COLUMNS, classify_row, main
from careroute.providers.osm.matching import DIRECTORY_AMENITIES


def test_lua_prefilter_keeps_every_directory_amenity():
    """The Lua pass is coarse on purpose, but its amenity list must not be
    narrower than the directory's, or rows vanish before classify sees them."""
    lua = (PROJECT_ROOT / "infra" / "geo" / "healthcare.lua").read_text(encoding="utf-8")
    line = re.search(r"local AMENITY = \{(.*?)\}", lua).group(1)
    assert set(re.findall(r"(\w+) = true", line)) == set(DIRECTORY_AMENITIES)


def test_classifier_needs_only_the_stdlib():
    """The geo-import image has python3 and the source, not the app's deps."""
    code = ("import sys, careroute.providers.osm.classify; "
            "print(sorted(m for m in ('httpx', 'fastapi', 'psycopg', 'langgraph', 'redis') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, check=True,
                         capture_output=True, text=True).stdout.strip()
    assert out == "[]"


def _row(tags):
    out = classify_row("N", "1", tags)
    return None if out is None else dict(zip(COLUMNS, out))


def test_rows_outside_the_directory_are_dropped():
    assert _row({"amenity": "cafe", "name": "Cafe Medical Road"}) is None
    assert _row({"healthcare": "clinic"}) is None                      # unnamed
    assert _row({"shop": "yes", "name": "Om General Stores"}) is None


def test_columns_carry_the_directory_rules():
    chemist = _row({"shop": "convenience", "name": "Ganesh Pharma & General"})
    assert chemist["is_pharmacy"] == "t" and chemist["is_narrow"] == "f"

    clinic = _row({"amenity": "clinic", "name": "Sparsh Ortho Care",
                   "healthcare:speciality": "orthodontics; Cardiology "})
    assert clinic["spec_name"] == '{"orthopedics"}'
    assert clinic["spec_tag"] == '{"cardiology","dentistry"}'      # table order
    assert clinic["spec_tokens"] == '{"orthodontics","cardiology"}'
    assert clinic["tag_tokens"] == 2

    spine = _row({"amenity": "hospital", "name": "Dr Ramesh Spine Hospital"})
    assert spine["er_candidate"] == "f" and spine["is_hospital"] == "t"
    er = _row({"amenity": "hospital", "name": "Manipal Hospital", "emergency": "yes"})
    assert er["er_candidate"] == "t"


def test_csv_round_trip_with_awkward_names():
    """Quotes, commas, braces and Kannada survive the COPY pipe."""
    tags = {"amenity": "clinic", "name": 'Dr "Sai", {Clinic} ಶ್ರೀ',
            "healthcare:speciality": 'a,b;c"d'}
    src = io.StringIO()
    csv.writer(src).writerow(["W", "42", json.dumps(tags)])
    src.seek(0)
    out, err = io.StringIO(), io.StringIO()
    assert main(src, out, err) == 0
    row = next(csv.reader(io.StringIO(out.getvalue())))
    assert row[:2] == ["W", "42"] and row[3] == tags["name"].lower()
    assert row[-1] == '{"a,b","c\\"d"}'
    assert "1 candidate rows, 1 in the directory" in err.getvalue()
