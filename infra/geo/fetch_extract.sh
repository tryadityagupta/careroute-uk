#!/usr/bin/env bash
# infra/geo/fetch_extract.sh — download the OSM extract for the demo region.
#
#   docker compose --profile geo-setup run --rm geo-fetch
#
# Default: West Yorkshire from Geofabrik (~60 MB), which covers every demo
# service in data/uk (Leeds, Otley, Pudsey, Morley, Wakefield, Huddersfield).
# Point REGION_URL at another Geofabrik extract for a different area, and set
# BBOX (min_lon,min_lat,max_lon,max_lat) to clip it; with no BBOX the whole
# extract is used. Everything downstream (PostGIS, OSRM, Nominatim) reads
# /data/osm/region.osm.pbf.
set -euo pipefail

OUT_DIR="${OUT_DIR:-/data/osm}"
REGION_URL="${REGION_URL:-https://download.geofabrik.de/europe/united-kingdom/england/west-yorkshire-latest.osm.pbf}"
# min_lon,min_lat,max_lon,max_lat
BBOX="${BBOX:-}"
SRC="$OUT_DIR/source.osm.pbf"
DST="$OUT_DIR/region.osm.pbf"

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

if [ ! -f "$SRC" ] || [ "${FORCE:-0}" = "1" ]; then
    echo "[geo-fetch] downloading $REGION_URL"
    curl -fL --retry 3 -o "$SRC.part" "$REGION_URL"
    curl -fsSL -o "$SRC.md5" "$REGION_URL.md5"
    # Geofabrik's .md5 names the dated file; compare the hash only.
    expected="$(cut -d' ' -f1 "$SRC.md5")"
    actual="$(md5sum "$SRC.part" | cut -d' ' -f1)"
    [ "$expected" = "$actual" ] || { echo "[geo-fetch] md5 mismatch" >&2; exit 1; }
    mv "$SRC.part" "$SRC"
else
    echo "[geo-fetch] $SRC exists (FORCE=1 to re-download)"
fi

# Record the data date BEFORE clipping; it ends up in geo.dataset so you can
# always tell how old the provider directory is.
osmium fileinfo -g header.option.osmosis_replication_timestamp "$SRC" \
    > "${DST%.osm.pbf}.extract_date" 2>/dev/null || echo unknown > "${DST%.osm.pbf}.extract_date"

if [ -n "$BBOX" ]; then
    echo "[geo-fetch] clipping to bbox $BBOX"
    # complete_ways: a road crossing the box edge is kept whole, so OSRM doesn't
    # get dangling half-roads at the border.
    osmium extract --bbox "$BBOX" --strategy complete_ways --overwrite \
        -o "$DST" "$SRC"
else
    echo "[geo-fetch] no BBOX: using the whole extract"
    cp "$SRC" "$DST"
fi

ls -lh "$SRC" "$DST"
echo "[geo-fetch] done: $DST (data as of $(cat "${DST%.osm.pbf}.extract_date"))"