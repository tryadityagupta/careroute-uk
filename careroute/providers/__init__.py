"""OpenStreetMap provider directory.

    directory.py  OsmProviderDirectory — filtering, ranking, structured misses
    sources.py    where elements come from: OverpassSource | PostgisSource
    matching.py   what a facility IS: specialty, pharmacy, narrow boutique, ER
    classify.py   matching.py run once per row at PostGIS import time

Import-free on purpose (see careroute/providers/__init__.py): import
OsmProviderDirectory from careroute.providers.osm.directory.
"""
