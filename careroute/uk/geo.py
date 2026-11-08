"""
uk/geo.py — UK places to coordinates, and distances with drive times.

Postcodes resolve through postcodes.io (free, open, ONS data) with an offline
table for the demo postcodes, so the demo never depends on it. Other place
names go to Nominatim restricted to GB. Distances reuse the OSRM router:
straight-line pre-filter, one Table call for road distance, and an honest
"straight_line" label if OSRM is unavailable.
"""

from __future__ import annotations

import re

import httpx

from careroute.maps.cache import LRUCache
from careroute.maps.distance import haversine_km
from careroute.maps.geocoding import NominatimGeocoder
from careroute.maps.routing import OsrmRouter
from careroute.uk.status import status

UK_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


def normalise_postcode(text: str) -> str | None:
    m = UK_POSTCODE.search(text or "")
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


class UkGeocoder:
    def __init__(self, nominatim: NominatimGeocoder, *, offline_postcodes: dict | None = None,
                 postcodes_io_url: str = "https://api.postcodes.io",
                 client: httpx.AsyncClient | None = None, use_network: bool = True):
        self.nominatim = nominatim
        self.offline = {k.upper(): v for k, v in (offline_postcodes or {}).items()}
        self.base_url = postcodes_io_url.rstrip("/")
        self._http = client or httpx.AsyncClient()
        self.use_network = use_network
        self._cache = LRUCache(20000)

    async def postcode(self, pc: str) -> dict | None:
        pc = normalise_postcode(pc)
        if not pc:
            return None
        if pc in self.offline:
            lat, lng = self.offline[pc]
            return {"lat": lat, "lng": lng, "label": pc, "source": "postcode"}
        hit = self._cache.get(pc)
        if hit:
            return hit
        if not self.use_network:
            return None
        try:
            r = await self._http.get(f"{self.base_url}/postcodes/{pc.replace(' ', '')}",
                                     timeout=3)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            res = r.json().get("result") or {}
            if res.get("latitude") is None:
                return None
            out = {"lat": res["latitude"], "lng": res["longitude"], "label": pc,
                   "source": "postcode"}
            self._cache.put(pc, out)
            return out
        except (httpx.HTTPError, ValueError) as e:
            print(f"[geo] postcodes.io failed ({str(e)[:60]})")
            return None

    async def geocode(self, text: str) -> dict | None:
        """{lat, lng, label, source} or None."""
        text = (text or "").strip()
        if not text:
            return None
        if normalise_postcode(text):
            hit = await self.postcode(text)
            if hit:
                return hit
        if not self.use_network:
            return None
        g = await self.nominatim.geocode(text)
        if "lat" not in g:
            return None
        label = ", ".join(p.strip() for p in g["display_name"].split(",")[:2])
        return {"lat": g["lat"], "lng": g["lng"], "label": label, "source": "place",
                "approximate": g.get("approximate", False)}


class DistanceService:
    def __init__(self, router: OsrmRouter):
        self.router = router

    async def annotate(self, lat: float, lng: float, items: list[dict]) -> list[dict]:
        """Copies of items with distance_km, distance_type and drive_min.
        Road distance when OSRM answers, otherwise labelled straight-line."""
        out = [dict(i, distance_km=haversine_km(lat, lng, i["lat"], i["lng"]),
                    distance_type="straight_line", drive_min=None) for i in items]
        if not out:
            return out
        road = None
        status("Getting drive times\u2026")
        try:
            road = await self.router.road_distances(lat, lng, [(i["lat"], i["lng"]) for i in out])
        except Exception as e:                     # never fail a lookup on OSRM
            print(f"[geo] OSRM failed ({str(e)[:60]})")
        if road:
            for i, r in zip(out, road):
                i["distance_km"] = r["distance_km"]
                i["drive_min"] = r.get("duration_min")
                i["distance_type"] = "road"
        return out
