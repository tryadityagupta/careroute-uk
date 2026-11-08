#!/usr/bin/env bash
# infra/geo/import.sh — load healthcare POIs from the clipped extract into PostGIS.
#
#   docker compose --profile geo-setup run --rm geo-import
#
# Three stages, all inside staging schema geo_import:
#   1. osm2pgsql + healthcare.lua: every named element that COULD be in the
#      directory (coarse; see the Lua header).
#   2. classify.py: the directory's own rules (providers/osm/matching.py) run
#      once per row; the answers become columns, and rows that are not in the
#      directory are dropped. A lookup then filters on stored columns in SQL
#      instead of running regexes on the app's event loop.
#   3. index, ANALYZE, then swap geo_import in for schema geo inside ONE
#      transaction. Running replicas keep answering from the old table until
#      the commit and from the new one right after; they never see an empty
#      or half-indexed table.
#
# SCHEMA VERSION 2 (the classified columns). An app built for schema 2 refuses
# a schema-1 table with "re-run the import". Re-run it monthly with a newer
# extract, and after any change to matching.py (healthz shows rules_current).
#
# Needs python3 and the careroute package source (stdlib only, no app
# dependencies): CAREROUTE_SRC defaults to the repo root this script sits in.
set -euo pipefail

DSN="${GEO_DATABASE_URL:-${DATABASE_URL:?set DATABASE_URL or GEO_DATABASE_URL}}"
PBF="${PBF:-/data/osm/region.osm.pbf}"
STYLE="${STYLE:-/geo/healthcare.lua}"
META="${PBF%.osm.pbf}.extract_date"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${CAREROUTE_SRC:-$HERE/../..}${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
SCHEMA_VERSION=2

[ -f "$PBF" ] || { echo "missing $PBF; run geo-fetch first" >&2; exit 1; }
EXTRACT_DATE="$(cat "$META" 2>/dev/null || echo unknown)"
RULES_VERSION="$("$PYTHON" -c \
    'from careroute.providers.osm.matching import RULES_VERSION as v; print(v)')" || {
    echo "cannot import careroute (python3 + CAREROUTE_SRC=$PYTHONPATH)" >&2; exit 1; }

echo "[geo-import] preparing staging schema"
psql "$DSN" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
DROP SCHEMA IF EXISTS geo_import CASCADE;
CREATE SCHEMA geo_import;
SQL

echo "[geo-import] osm2pgsql $(basename "$PBF") (extract date: $EXTRACT_DATE)"
# No --slim: the extract is state-sized, so node locations fit in RAM and the
# import is much faster. Nothing here is ever updated incrementally; a refresh
# is a full re-import plus swap.
GEO_IMPORT_SCHEMA=geo_import osm2pgsql --create --output=flex \
    --style="$STYLE" --database="$DSN" "$PBF"

echo "[geo-import] classifying rows (rules $RULES_VERSION)"
psql "$DSN" -v ON_ERROR_STOP=1 -q <<'SQL'
-- Column order = classify.COLUMNS.
CREATE TABLE geo_import.classified (
    osm_type     char(1) NOT NULL,
    osm_id       bigint  NOT NULL,
    dedupe_key   text,               -- lower(name); NULL = never deduped
    name_lc      text    NOT NULL,   -- Python-lowered (Unicode-correct) name
    facility     text    NOT NULL,
    is_pharmacy  boolean NOT NULL,
    is_hospital  boolean NOT NULL,
    is_narrow    boolean NOT NULL,
    er_candidate boolean NOT NULL,
    tag_tokens   integer NOT NULL,   -- entries in healthcare:speciality
    spec_name    text[]  NOT NULL,   -- specialty ids matched by the name
    spec_tag     text[]  NOT NULL,   -- ... by a speciality-tag token
    spec_any     text[]  NOT NULL,   -- union of the two
    spec_tokens  text[]  NOT NULL    -- the tokens (unknown-specialty stem search)
);
SQL
psql "$DSN" -v ON_ERROR_STOP=1 -q -c "\\copy (SELECT osm_type, osm_id, tags::text \
FROM geo_import.healthcare) TO STDOUT WITH (FORMAT csv)" \
  | "$PYTHON" -m careroute.providers.osm.classify \
  | psql "$DSN" -v ON_ERROR_STOP=1 -q -c "\\copy geo_import.classified FROM STDIN WITH (FORMAT csv)"

echo "[geo-import] building the directory table, indexing, swapping into schema geo"
psql "$DSN" -v ON_ERROR_STOP=1 -q -v extract_date="$EXTRACT_DATE" \
     -v rules_version="$RULES_VERSION" -v schema_version="$SCHEMA_VERSION" <<'SQL'
-- Only classified rows survive: the join drops what is not in the directory.
CREATE TABLE geo_import.directory AS
SELECT h.osm_type, h.osm_id, h.name, h.amenity, h.healthcare, h.shop, h.tags, h.geom,
       c.dedupe_key, c.name_lc, c.facility, c.is_pharmacy, c.is_hospital, c.is_narrow,
       c.er_candidate, c.tag_tokens, c.spec_name, c.spec_tag, c.spec_any, c.spec_tokens
FROM geo_import.healthcare h
JOIN geo_import.classified c USING (osm_type, osm_id);
DROP TABLE geo_import.healthcare, geo_import.classified;
ALTER TABLE geo_import.directory RENAME TO healthcare;
ALTER TABLE geo_import.healthcare ALTER COLUMN geom SET NOT NULL;

-- The app queries in metres with ST_DWithin(geom::geography, ...). An index
-- on the same EXPRESSION is what lets that use the index; a plain index on
-- geom (degrees) would not be used for a metre-radius search.
CREATE INDEX healthcare_geog_idx
    ON geo_import.healthcare USING gist ((geom::geography));
-- Specialty lookups: spec_any @> ARRAY[id].
CREATE INDEX healthcare_spec_idx ON geo_import.healthcare USING gin (spec_any);
ANALYZE geo_import.healthcare;

CREATE TABLE geo_import.dataset AS
SELECT 'openstreetmap (Geofabrik southern-zone, clipped to Karnataka)'::text AS source,
       :'extract_date'::text AS extract_date,
       now() AS imported_at,
       (SELECT count(*) FROM geo_import.healthcare) AS rows,
       :schema_version::int AS schema_version,
       :'rules_version'::text AS rules_version;

BEGIN;
DROP SCHEMA IF EXISTS geo CASCADE;
ALTER SCHEMA geo_import RENAME TO geo;
COMMIT;
SQL

psql "$DSN" -At -c "SELECT 'rows=' || rows || ' extract_date=' || extract_date \
|| ' schema=' || schema_version || ' rules=' || rules_version FROM geo.dataset"
echo "[geo-import] done"
