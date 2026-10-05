"""
maps/postgis.py — the provider directory as a PostGIS spatial query.

The data is OpenStreetMap, imported from a Geofabrik extract by
infra/geo/import.sh; this changes WHERE it is served from, not what it is.

  * Overpass: shared public service, 2-25 s per query, rate-limited, and
    hammering it in a load test breaks its usage policy.
  * PostGIS: an indexed query in milliseconds, ours to load-test, and
    stateless — every replica reads the same table, no per-replica cache.

PUSHDOWN (schema 2). The import runs providers/osm/matching.py once per row
(classify.py) and stores the answers as columns: is_pharmacy, is_narrow,
er_candidate, the specialty ids matched by name / by tag, dedupe_key. A lookup
then filters, dedupes and cuts in SQL and returns only the rows the directory
will look at (8 instead of ~3,000 at 8 km). Before this, every lookup pulled
the whole radius and ran regexes over it ON THE EVENT LOOP (~15 ms of CPU per
lookup, during which the replica served nobody else).

The SQL reproduces the Python directory's semantics exactly, including the
details that decide WHICH rows survive a LIMIT:
  * the radius test is the old one: ST_DWithin on the spheroid AND the
    directory's haversine, rounded to 10 m, <= radius (both applied, as before);
  * nearest-first = great-circle order (geography <->, a sphere, the same order
    as haversine);
  * dedupe keeps the nearest row per lower-cased name, and is applied BEFORE
    or AFTER a filter exactly where the directory applies it.
tests/test_geo_pushdown.py holds the two paths to identical payloads.

Rows come back in Overpass's element shape ({lat, lon, tags}); the directory
runs its normal filtering on them (a no-op on these rows), so pushdown changes
how many rows reach Python, never the answer.

ASYNC: psycopg's AsyncConnectionPool. An async pool must be OPENED on the
running event loop, so it is opened lazily on first use (under an
asyncio.Lock) and closed by Container.aclose() at shutdown.
"""

from __future__ import annotations

import asyncio

from careroute.runtime import ensure_psycopg_compatible_loop

#: Bump together with infra/geo/import.sh whenever the table's columns change.
SCHEMA_VERSION = 2

#: Lookup kinds a pushdown query understands (see GeoDatabase.facilities).
KINDS = ("any", "general", "pharmacy", "specialty", "er")

_REIMPORT = ("re-run the import (docker compose --profile geo-setup run --rm "
             "geo-import, or infra/geo/import.sh)")


class GeoUnavailable(Exception):
    """The provider directory could not be queried (DB down, table missing)."""


# The directory's haversine (maps/distance.py), in SQL, rounded the same way.
# Same formula and constants, so the 10 m rounding agrees at the boundary.
_HAVERSINE_KM = """round((6371 * 2 * atan2(sqrt(hv.a), sqrt(1 - hv.a)))::numeric, 2)"""
_HAVERSINE_A = """
CROSS JOIN LATERAL (SELECT
    sin(radians(ST_Y(h.geom) - %(lat)s) / 2) ^ 2
  + cos(radians(%(lat)s)) * cos(radians(ST_Y(h.geom)))
    * sin(radians(ST_X(h.geom) - %(lng)s) / 2) ^ 2 AS a) hv"""

# Every row in the radius, with what the filters below need.
_NEAR = f"""
near AS (
  SELECT h.osm_type, h.osm_id, ST_Y(h.geom) AS lat, ST_X(h.geom) AS lon, h.tags,
         coalesce(h.dedupe_key, h.osm_type || h.osm_id::text) AS dkey,
         h.is_pharmacy, h.is_narrow, h.er_candidate, h.tag_tokens,
         {{via_name}} AS via_name,
         {_HAVERSINE_KM} AS dist_10m,
         h.geom::geography <-> pt.g AS dist
  FROM geo.healthcare h
  CROSS JOIN pt
  {_HAVERSINE_A}
  WHERE ST_DWithin(h.geom::geography, pt.g, %(radius_m)s)
    AND {_HAVERSINE_KM} * 1000 <= %(radius_m)s
    {{prefilter}}
),
dedup AS (                      -- the directory's dedupe: nearest row per name
  SELECT DISTINCT ON (dkey) * FROM near ORDER BY dkey, dist
)"""
_PT = "pt AS (SELECT ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography AS g)"

# Specialty: a known specialty is an id the import stored per row; an unknown
# one is the matcher's stem, searched in the Python-lowered name and tokens.
_SPEC_KEY = "h.spec_any @> ARRAY[%(key)s]::text[]"
_SPEC_KEY_NAME = "h.spec_name @> ARRAY[%(key)s]::text[]"
_SPEC_STEM_NAME = "strpos(h.name_lc, %(stem)s) > 0"
_SPEC_STEM = ("(strpos(h.name_lc, %(stem)s) > 0 OR EXISTS ("
              "SELECT 1 FROM unnest(h.spec_tokens) t WHERE strpos(t, %(stem)s) > 0))")


def _query(prefilter: str, select_from: str, *, via_name: str = "false") -> str:
    near = _NEAR.replace("{via_name}", via_name).replace(
        "{prefilter}", f"AND {prefilter}" if prefilter else "")
    return f"WITH {_PT},{near}\nSELECT lat, lon, tags FROM {select_from}"


class GeoDatabase:
    # ST_DWithin on geography = true metres, and it matches the
    # (geom::geography) expression index that import.sh creates, so this is
    # an index scan. Every row, unordered: the Overpass-equivalent fetch.
    NEARBY_SQL = """
SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon, tags
FROM geo.healthcare
WHERE ST_DWithin(geom::geography,
                 ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                 %(radius_m)s)
"""

    # One statement per lookup kind. Where the directory filters BEFORE it
    # dedupes (pharmacies, specialty matches) the filter is a prefilter; where
    # it dedupes ALL facilities first (general list, ER) it filters after.
    _SQL = {
        "any": _query("", "near ORDER BY dist"),
        # Nearest per name, non-boutiques first: the directory keeps the
        # non-narrow ones, or the boutiques if there are no others.
        "general": _query("", "dedup ORDER BY is_narrow, dist LIMIT %(limit)s"),
        "pharmacy": _query("h.is_pharmacy", "dedup ORDER BY dist LIMIT %(limit)s"),
        "er": _query("", "dedup WHERE er_candidate ORDER BY dist LIMIT %(limit)s"),
        # Evidence before distance, exactly as find_providers ranks: name
        # beats tag, a short tag list beats a 20-specialty mega-list.
        ("specialty", "key"): _query(
            _SPEC_KEY, """dedup ORDER BY NOT via_name,
                CASE WHEN via_name THEN 0 ELSE tag_tokens END, dist_10m, dist
                LIMIT %(limit)s""", via_name=_SPEC_KEY_NAME),
        ("specialty", "stem"): _query(
            _SPEC_STEM, """dedup ORDER BY NOT via_name,
                CASE WHEN via_name THEN 0 ELSE tag_tokens END, dist_10m, dist
                LIMIT %(limit)s""", via_name=_SPEC_STEM_NAME),
    }

    COUNT_SQL = f"""
WITH {_PT}
SELECT count(*) FROM geo.healthcare h
CROSS JOIN pt
{_HAVERSINE_A}
WHERE ST_DWithin(h.geom::geography, pt.g, %(radius_m)s)
  AND {_HAVERSINE_KM} * 1000 <= %(radius_m)s
"""

    def __init__(self, dsn: str, *, pool_max: int = 10,
                 statement_timeout_ms: int = 2000, rules_version: str | None = None):
        self.dsn = dsn
        self.pool_max = pool_max
        # A spatial lookup should take milliseconds; if one doesn't, fail it
        # rather than hold a pooled connection while users queue behind it.
        self.statement_timeout_ms = statement_timeout_ms
        # The matching rules this build runs (matching.RULES_VERSION), compared
        # with the ones the import stored. None: don't compare.
        self.rules_version = rules_version
        self._pool = None
        self._lock: asyncio.Lock | None = None

    async def pool(self):
        """The async connection pool, opened on first use."""
        if self._pool is None:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if self._pool is None:
                    if not self.dsn:
                        raise GeoUnavailable(
                            "OSM_SOURCE=postgis but neither GEO_DATABASE_URL "
                            "nor DATABASE_URL is set")
                    ensure_psycopg_compatible_loop()     # clear error on Windows
                    from psycopg_pool import AsyncConnectionPool
                    pool = AsyncConnectionPool(
                        self.dsn, min_size=1, max_size=self.pool_max, open=False,
                        kwargs={"autocommit": True,
                                "options": f"-c statement_timeout={self.statement_timeout_ms}"})
                    try:
                        await pool.open(wait=True, timeout=2)
                    except Exception:
                        await pool.close()
                        raise
                    self._pool = pool
        return self._pool

    async def _fetch(self, sql: str, params: dict, *, one: bool = False):
        try:
            pool = await self.pool()
            async with pool.connection(timeout=2) as conn:
                cur = await conn.execute(sql, params)
                return await (cur.fetchone() if one else cur.fetchall())
        except GeoUnavailable:
            raise
        except Exception as e:         # psycopg errors, pool timeout, missing table
            if type(e).__name__ == "UndefinedColumn":
                raise GeoUnavailable(
                    f"geo.healthcare predates schema {SCHEMA_VERSION}: {_REIMPORT}") from e
            raise GeoUnavailable(f"{type(e).__name__}: {str(e)[:120]}") from e

    @staticmethod
    def _elements(rows) -> list[dict]:
        return [{"lat": r[0], "lon": r[1], "tags": r[2]} for r in rows]

    async def nearby_elements(self, lat: float, lng: float, radius_m: int) -> list[dict]:
        """Every named healthcare POI within radius_m metres, Overpass-shaped."""
        return self._elements(await self._fetch(self.NEARBY_SQL, {
            "lat": lat, "lng": lng, "radius_m": radius_m}))

    async def facilities(self, lat: float, lng: float, radius_m: int, *, kind: str,
                         limit: int = 8, key: str | None = None,
                         stem: str | None = None) -> list[dict]:
        """Just the rows one lookup will look at, Overpass-shaped.

        kind="specialty" needs key (a known specialty id) or stem (unknown).
        """
        if kind == "specialty":
            if (key is None) == (stem is None):
                raise ValueError("kind='specialty' needs exactly one of key / stem")
            sql = self._SQL[("specialty", "key" if key is not None else "stem")]
        elif kind in self._SQL:
            sql = self._SQL[kind]
        else:
            raise ValueError(f"unknown lookup kind {kind!r}")
        return self._elements(await self._fetch(sql, {
            "lat": lat, "lng": lng, "radius_m": radius_m, "limit": int(limit),
            "key": key, "stem": stem}))

    async def count_nearby(self, lat: float, lng: float, radius_m: int) -> int:
        """How many facilities the directory would see in the radius."""
        row = await self._fetch(self.COUNT_SQL, {"lat": lat, "lng": lng,
                                                 "radius_m": radius_m}, one=True)
        return row[0]

    async def dataset_info(self) -> dict:
        """Row count, data date and schema/rules versions, for /healthz.
        Raises GeoUnavailable if the import has never been run, or was run
        with an older schema than this code queries."""
        try:
            row = await self._fetch(
                "SELECT rows, extract_date, imported_at, schema_version, rules_version "
                "FROM geo.dataset", {}, one=True)
        except GeoUnavailable as e:
            if "predates schema" in str(e):            # no schema_version column
                raise GeoUnavailable(
                    f"geo.dataset predates schema {SCHEMA_VERSION}: {_REIMPORT}") from e
            raise
        if not row:
            raise GeoUnavailable("geo.dataset is empty")
        rows, extract_date, imported_at, schema_version, rules_version = row
        if schema_version != SCHEMA_VERSION:
            raise GeoUnavailable(
                f"geo schema is version {schema_version}, this build needs "
                f"{SCHEMA_VERSION}: {_REIMPORT}")
        info = {"rows": rows, "extract_date": extract_date,
                "imported_at": imported_at.isoformat(),
                "schema_version": schema_version, "rules_version": rules_version}
        if self.rules_version is not None:
            info["rules_current"] = rules_version == self.rules_version
            if not info["rules_current"]:
                info["warning"] = ("matching rules changed since the import; stored "
                                   f"classifications are stale: {_REIMPORT}")
        return info

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
