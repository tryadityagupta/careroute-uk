"""
tests/test_geo_pushdown.py — the PostGIS query gives the SAME answers as the
Python filters.

Specialty/pharmacy/narrow/ER matching, name dedupe and the shortlist cut now
run inside PostGIS (maps/postgis.py, on columns classify.py stored at import).
The Python filters in OsmProviderDirectory still exist (Overpass uses them).
This test holds the two to the same answers on a city-sized synthetic extract
(tests/geo_synth.py): for each query point it runs every lookup twice,

  pushdown   PostgisSource: filtered, deduped, LIMITed in SQL
  reference  the same rows, ALL of them (nearby_elements), filtered in Python

and compares the full tool payloads. Both sides see the same rows, so any
difference is a filtering difference.

The reference rows are passed in exact-distance order. The directory sorts on
distance rounded to 10 m, so among rows tied at 10 m the old code kept
whichever Postgres happened to return first; the SQL breaks such ties by exact
distance. Giving the reference the same order makes the comparison strict.

Imports into a SEPARATE database (<db>_pushdown_test) so test_geo.py's fixture
import is untouched. Skips without osm2pgsql/psql/GEO_TEST_DATABASE_URL or the
right to CREATE DATABASE.
"""

import os
import random
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from careroute.config import PROJECT_ROOT
from careroute.domain.emergency import EmergencyNumberResolver, EmergencyService
from careroute.maps.postgis import GeoDatabase
from careroute.maps.routing import OsrmRouter
from careroute.providers.osm.directory import OsmProviderDirectory
from careroute.providers.osm.sources import PostgisSource
from tests.fakes import FakeGeocoder, StaticSource
from tests.geo_synth import CENTRE, generate

DSN = os.getenv("GEO_TEST_DATABASE_URL", "").strip()
GEO = PROJECT_ROOT / "infra" / "geo"

pytestmark = [
    pytest.mark.skipif(not DSN or not shutil.which("osm2pgsql") or not shutil.which("psql"),
                       reason="needs GEO_TEST_DATABASE_URL, osm2pgsql and psql"),
    pytest.mark.anyio,
]

SPECIALTIES = ["Cardiology", "Orthopedics", "Pediatrics", "Gynecology", "ENT",
               "Dermatology", "Neurology", "Psychiatry", "Ophthalmology", "Dentistry",
               "General Medicine", "Nephrology",
               # unknown to the pattern table: the stem path
               "Physiotherapy", "Ayurveda", "Diagnostics"]

# A far-off cluster of ONLY narrow boutiques (two sharing a name): exercises
# the "general list would be empty, fall back to boutiques" path.
ISLAND = (13.60, 78.40)
_ISLAND_NODES = """
  <node id="900001" version="1" lat="13.6010" lon="78.4000">
    <tag k="amenity" v="clinic"/><tag k="name" v="Glow Skin &amp; Hair Clinic"/></node>
  <node id="900002" version="1" lat="13.6000" lon="78.4020">
    <tag k="healthcare" v="dentist"/><tag k="name" v="Island Dental"/></node>
  <node id="900003" version="1" lat="13.6030" lon="78.4030">
    <tag k="healthcare" v="dentist"/><tag k="name" v="Island Dental"/></node>
"""
# Same-name pairs whose members classify DIFFERENTLY. Whether a lookup dedupes
# before or after it filters changes the answer here, and only here: the
# synthetic city's same-name rows are always tagged alike.
#   "Hill Medical"   nearer = clinic, farther = chemist: pharmacies filter
#                    first, so the chemist IS listed.
#   "Hill Hospital"  nearer = emergency=no, farther = plain hospital: the ER
#                    list dedupes first, so NEITHER is an ER candidate.
#   "Hill Care"      nearer = no specialty, farther = cardiology tag: a match.
DEDUPE_ORDER = (13.70, 78.50)
_DEDUPE_ORDER_NODES = """
  <node id="900011" version="1" lat="13.7004" lon="78.5000">
    <tag k="amenity" v="clinic"/><tag k="name" v="Hill Medical"/></node>
  <node id="900012" version="1" lat="13.7030" lon="78.5000">
    <tag k="shop" v="chemist"/><tag k="name" v="Hill Medical"/></node>
  <node id="900013" version="1" lat="13.6995" lon="78.5000">
    <tag k="amenity" v="hospital"/><tag k="emergency" v="no"/><tag k="name" v="Hill Hospital"/></node>
  <node id="900014" version="1" lat="13.6960" lon="78.5000">
    <tag k="amenity" v="hospital"/><tag k="name" v="Hill Hospital"/></node>
  <node id="900015" version="1" lat="13.7000" lon="78.5006">
    <tag k="amenity" v="clinic"/><tag k="name" v="Hill Care"/></node>
  <node id="900016" version="1" lat="13.7000" lon="78.5040">
    <tag k="amenity" v="clinic"/><tag k="healthcare:speciality" v="cardiology"/>
    <tag k="name" v="Hill Care"/></node>
"""
NOWHERE = (14.50, 79.50)          # nothing within 30 km


def _db_url(dsn: str, dbname: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path="/" + dbname))


@pytest.fixture(scope="module")
def synth_dsn(tmp_path_factory):
    """Create <db>_pushdown_test and import the synthetic city into it, once."""
    psycopg = pytest.importorskip("psycopg")
    base = urlsplit(DSN).path.lstrip("/") or "postgres"
    name = f"{base}_pushdown_test"
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error as e:
        pytest.skip(f"cannot create a test database: {e}")
    dsn = _db_url(DSN, name)
    osm = tmp_path_factory.mktemp("synth") / "city.osm"
    osm.write_text(generate().replace(
        "</osm>", _ISLAND_NODES + _DEDUPE_ORDER_NODES + "</osm>"), encoding="utf-8")
    subprocess.run(["bash", str(GEO / "import.sh")], check=True, capture_output=True,
                   env={**os.environ, "GEO_DATABASE_URL": dsn, "PBF": str(osm),
                        "STYLE": str(GEO / "healthcare.lua")})
    yield dsn
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def db(synth_dsn):
    geo_db = GeoDatabase(synth_dsn)
    yield geo_db
    await geo_db.aclose()


EXACT_ORDER_SQL = GeoDatabase.NEARBY_SQL + """
ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography
"""


async def _all_rows(db, lat, lng, radius_m):
    rows = await db._fetch(EXACT_ORDER_SQL, {"lat": lat, "lng": lng, "radius_m": radius_m})
    return [{"lat": r[0], "lon": r[1], "tags": r[2]} for r in rows]


def _points():
    rng = random.Random(11)
    dense = [(CENTRE[0] + rng.uniform(-0.05, 0.05), CENTRE[1] + rng.uniform(-0.05, 0.05))
             for _ in range(20)]
    outskirts = [(CENTRE[0] + rng.choice([-1, 1]) * rng.uniform(0.15, 0.3),
                  CENTRE[1] + rng.choice([-1, 1]) * rng.uniform(0.15, 0.3)) for _ in range(6)]
    return dense + outskirts + [ISLAND, DEDUPE_ORDER, NOWHERE]


async def _lookups(directory, emergency, p, radius, k):
    out = {}
    for spec in SPECIALTIES:
        out[f"providers:{spec}"] = await directory.find_providers(spec, *p, k=k, radius_m=radius)
    out["general"] = await directory.find_general_facilities(*p, k=k, radius_m=radius)
    out["pharmacies"] = await directory.find_pharmacies(*p, k=k, radius_m=radius)
    if radius == EmergencyService.SEARCH_RADIUS_M:
        out["emergency"] = await emergency.nearest_hospitals(*p, k=k)
    return out


async def test_import_classified_the_city(db):
    info = await db.dataset_info()
    assert info["schema_version"] == 2
    assert info["rows"] > 10000
    pool = await db.pool()
    async with pool.connection() as conn:
        cur = await conn.execute("""
            SELECT count(*) FILTER (WHERE is_pharmacy), count(*) FILTER (WHERE er_candidate),
                   count(*) FILTER (WHERE is_narrow), count(*) FILTER (WHERE spec_any <> '{}'),
                   count(*) FILTER (WHERE name = 'Cafe Medical Road'),
                   count(*) FILTER (WHERE name = 'Ganesh Pharma & General' AND is_pharmacy)
            FROM geo.healthcare""")
        pharm, er, narrow, spec, cafe, named_chemist = await cur.fetchone()
    assert min(pharm, er, narrow, spec) > 100          # every class is exercised
    assert cafe == 0                                   # not healthcare: dropped
    assert named_chemist > 0                           # shop=convenience "X Pharma": kept


async def test_pushdown_matches_python_filtering(db):
    """Every lookup, every point: identical payloads."""
    async with httpx.AsyncClient() as http:
        router = OsrmRouter("http://127.0.0.1:9", client=http)     # straight-line
        pushdown = OsmProviderDirectory(PostgisSource(db), router)
        number = EmergencyNumberResolver(FakeGeocoder(country="IN"))
        checked = 0
        for i, p in enumerate(_points()):
            radii = (500, 2000, 8000) if i % 4 else (500, 2000, 8000, 30000)
            k = 3 if i % 2 else 1
            for radius in radii:
                reference = OsmProviderDirectory(StaticSource(await _all_rows(db, *p, radius)),
                                                 router)
                got = await _lookups(pushdown, EmergencyService(number, pushdown), p, radius, k)
                want = await _lookups(reference, EmergencyService(number, reference),
                                      p, radius, k)
                for name in want:
                    assert got[name] == want[name], (p, radius, k, name)
                    checked += 1
    assert checked > 1500


async def test_fallbacks_are_exercised(db):
    """The comparison above must cover misses, empties and the boutique-only
    fallback, or it proves less than it seems to."""
    async with httpx.AsyncClient() as http:
        d = OsmProviderDirectory(PostgisSource(db), OsrmRouter("http://127.0.0.1:9", client=http))
        island = await d.find_general_facilities(*ISLAND, k=3, radius_m=2000)
        assert [f["name"] for f in island["facilities"]] == [
            "Glow Skin & Hair Clinic", "Island Dental"]              # deduped, boutiques
        miss = await d.find_providers("Cardiology", *ISLAND, k=3, radius_m=2000)
        assert miss["match_found"] is False and miss["general_facilities_nearby"] == 3
        assert len(miss["general_alternatives"]) == 2
        none = await d.find_general_facilities(*NOWHERE, k=3, radius_m=30000)
        assert none["match_found"] is False
        hill_pharm = await d.find_pharmacies(*DEDUPE_ORDER, k=3, radius_m=2000)
        assert [f["name"] for f in hill_pharm["pharmacies"]] == ["Hill Medical"]
        hill_er = await EmergencyService(EmergencyNumberResolver(FakeGeocoder(country="IN")),
                                         d).nearest_hospitals(*DEDUPE_ORDER)
        assert hill_er == []
        hill_cardio = await d.find_providers("Cardiology", *DEDUPE_ORDER, k=3, radius_m=2000)
        assert [f["name"] for f in hill_cardio] == ["Hill Care"]
        assert hill_cardio[0]["matched_via"] == "speciality_tag"
        stem = await d.find_providers("Physiotherapy", *CENTRE, k=3, radius_m=8000)
        assert isinstance(stem, list) and stem                       # unknown specialty


async def test_old_schema_says_reimport(db):
    """An app deployed before the re-import gets an actionable error."""
    pool = await db.pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE geo.dataset SET schema_version = 1")
    try:
        with pytest.raises(Exception, match="re-run the import"):
            await db.dataset_info()
    finally:
        async with pool.connection() as conn:
            await conn.execute("UPDATE geo.dataset SET schema_version = 2")
