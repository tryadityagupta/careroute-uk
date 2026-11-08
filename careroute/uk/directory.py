"""
uk/directory.py — the NHS service directory the route agent searches.

Demo data lives in data/uk/services.json (fictional services at real Leeds
coordinates). The shape mirrors what a production import from the NHS
Service Search API / Directory of Services would hold, so moving it into
PostGIS (like the OSM directory in careroute/providers/osm) changes this
class, not its callers.

Every result carries a stable `id`. The route agent recommends ids; code
renders names, addresses and times from these records, so an invented
provider cannot reach a patient.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from careroute.maps.distance import haversine_km
from careroute.uk.geo import DistanceService

PUBLIC_FIELDS = ("id", "ods", "service_type", "name", "address", "postcode", "hours",
                 "pharmacy_first", "walk_in", "bookable", "booking_mode", "confidential",
                 "min_age", "distance_km", "distance_type", "drive_min", "lat", "lng")


class UkServiceDirectory:
    def __init__(self, services: list[dict], distances: DistanceService):
        self._services = {s["id"]: s for s in services}
        self.distances = distances

    @classmethod
    def from_dir(cls, data_dir: Path, distances: DistanceService) -> "UkServiceDirectory":
        body = json.loads((data_dir / "uk" / "services.json").read_text(encoding="utf-8"))
        return cls(body["services"], distances)

    def get(self, service_id: str) -> dict | None:
        s = self._services.get(service_id)
        return copy.deepcopy(s) if s else None

    async def search(self, service_type: str, lat: float, lng: float, *, radius_km: float = 5,
                     k: int = 3, age: int | None = None, pharmacy_first: bool = False) -> list[dict]:
        """Nearest services of one type within radius_km (straight-line pre-filter),
        upgraded to road distance, nearest first."""
        rows = []
        for s in self._services.values():
            if s["service_type"] != service_type:
                continue
            if pharmacy_first and not s.get("pharmacy_first"):
                continue
            if age is not None and age < s.get("min_age", 0):
                continue
            d = haversine_km(lat, lng, s["lat"], s["lng"])
            if d <= radius_km:
                rows.append((d, s))
        rows.sort(key=lambda r: r[0])
        shortlist = [copy.deepcopy(s) for _, s in rows[:k]]
        annotated = await self.distances.annotate(lat, lng, shortlist)
        annotated.sort(key=lambda s: s["distance_km"])
        return [{f: s.get(f) for f in PUBLIC_FIELDS if f in s or f in ("drive_min",)}
                for s in annotated]
