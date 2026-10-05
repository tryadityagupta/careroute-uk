-- infra/geo/healthcare.lua — osm2pgsql flex style for CareRoute's provider directory.
--
-- A deliberately COARSE first pass. It keeps every named element that could
-- possibly be in the directory:
--
--   amenity  in hospital|clinic|doctors|pharmacy
--   healthcare = *            (the newer tagging scheme)
--   shop = *                  (any shop: "X Medicals" on shop=yes is a chemist)
--
-- and nothing else is decided here. infra/geo/import.sh then runs every row
-- through careroute/providers/osm/classify.py, which applies the directory's
-- own rules (matching.py: in_directory, pharmacy, narrow, ER, specialties)
-- and drops what is not in the directory. Those rules used to be half-copied
-- into this file with a "keep in sync" comment; now there is one copy.
-- (tests/test_geo_pushdown.py checks AMENITY below equals
-- DIRECTORY_AMENITIES, the one list this pass must not narrow.)
--
-- Differences from the Overpass query, both deliberate:
--   * Unnamed POIs are dropped here. The directory discards them anyway ("a
--     place you cannot name cannot be recommended").
--   * Multipolygon RELATIONS are included. Large hospital campuses are often
--     mapped as relations, which the Overpass query (node + way only) missed.
--
-- Every object becomes ONE point (ways/relations -> centroid), the same thing
-- Overpass's `out center` gave us. The full tag set is kept as jsonb.

local SCHEMA = os.getenv("GEO_IMPORT_SCHEMA") or "geo_import"

local healthcare = osm2pgsql.define_table({
    name = "healthcare",
    schema = SCHEMA,
    ids = { type = "any", id_column = "osm_id", type_column = "osm_type" },
    columns = {
        { column = "name", type = "text", not_null = true },
        { column = "amenity", type = "text" },
        { column = "healthcare", type = "text" },
        { column = "shop", type = "text" },
        { column = "tags", type = "jsonb", not_null = true },
        { column = "geom", type = "point", projection = 4326, not_null = true },
    },
})

local AMENITY = { hospital = true, clinic = true, doctors = true, pharmacy = true }

local function candidate(tags)
    return AMENITY[tags.amenity] or tags.healthcare ~= nil or tags.shop ~= nil
end

local function insert(object, geom)
    local tags = object.tags
    if not tags.name or not candidate(tags) or geom == nil then
        return
    end
    healthcare:insert({
        name = tags.name,
        amenity = tags.amenity,
        healthcare = tags.healthcare,
        shop = tags.shop,
        tags = tags,
        geom = geom,
    })
end

function osm2pgsql.process_node(object)
    insert(object, object:as_point())
end

function osm2pgsql.process_way(object)
    if object.is_closed then
        insert(object, object:as_polygon():centroid())
    else
        insert(object, object:as_linestring():centroid())
    end
end

function osm2pgsql.process_relation(object)
    if object.tags.type == "multipolygon" then
        insert(object, object:as_multipolygon():centroid())
    end
end
