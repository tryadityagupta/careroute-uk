"""
uk/directory.py — the NHS service directory the route agent searches.

Two sources, one interface:

    demo   data/uk/services.json: fictional services at real Leeds
           coordinates. Bookable, with hours and Pharmacy First flags, so the
           scripted journeys (booking, slots, payments) work end to end.
    nhs    data/uk/nhs/directory.json, built by scripts/import_nhs_directory.py
           from NHS open data (ODS / NHSBSA, Open Government Licence). Real
           pharmacies across England. Never bookable here: CareRoute must not
           simulate a booking at a real business. Fields the registers don't
           hold (opening hours, Pharmacy First participation, walk-in) are
           None, meaning UNKNOWN, and are rendered as "check with them".

CAREROUTE_UK_DIRECTORY chooses what is searched:

    demo    demo data only (the default when no NHS file exists)
    nhs     NHS pharmacies everywhere; demo services only for the types the
            NHS file doesn't have (sexual health, Talking Therapies, MSK, A&E)
    mixed   demo data inside the demo area (Leeds), NHS data everywhere else
            (the default when the NHS file exists). Leeds behaves exactly like
            the scripted demo; Petts Wood finds real pharmacies.

Every result carries a stable `id`. The route agent recommends ids; code
renders names, addresses and times from these records, so an invented
provider cannot reach a patient.

Search uses a 0.1-degree grid (about 11 km by 7 km in England), so a 20 km
search over ~11,000 pharmacies looks at a few hundred rows, not all of them.
"""

from __future__ import annotations

import copy
import json
import math
from collections import defaultdict
from pathlib import Path

from careroute.maps.distance import haversine_km
from careroute.uk.geo import DistanceService

PUBLIC_FIELDS = ("id", "ods", "service_type", "name", "address", "postcode", "hours",
                 "pharmacy_first", "walk_in", "bookable", "booking_mode", "confidential",
                 "min_age", "distance_km", "distance_type", "drive_min", "lat", "lng",
                 "source", "phone")
MODES = ("demo", "nhs", "mixed")
CELL = 0.1                       # grid cell, degrees
DEMO_AREA_MARGIN_KM = 12         # how far the demo area reaches past its outermost service
NHS_FILE = Path("uk") / "nhs" / "directory.json"


def _cell(lat: float, lng: float) -> tuple[int, int]:
    return math.floor(lat / CELL), math.floor(lng / CELL)


class UkServiceDirectory:
    def __init__(self, services: list[dict], distances: DistanceService, *,
                 nhs: list[dict] | None = None, mode: str = "demo",
                 nhs_meta: dict | None = None):
        if mode not in MODES:
            raise ValueError(f"CAREROUTE_UK_DIRECTORY must be one of {', '.join(MODES)}")
        nhs = nhs or []
        if mode != "demo" and not nhs:
            raise ValueError(f"CAREROUTE_UK_DIRECTORY={mode} needs {NHS_FILE.as_posix()} "
                             "(build it with scripts/import_nhs_directory.py)")
        self.distances = distances
        self.mode = mode
        self.nhs_meta = nhs_meta or {}
        demo = [{**d, "source": d.get("source") or "demo"} for d in services]
        self._demo_box = self._box(demo)
        nhs_types = {r["service_type"] for r in nhs}
        if mode == "demo":
            active = demo
        elif mode == "nhs":
            active = [d for d in demo if d["service_type"] not in nhs_types] + nhs
        else:
            active = demo + [r for r in nhs if not self.in_demo_area(r["lat"], r["lng"])]
        self._services = {s["id"]: s for s in active}
        self._grid: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
        for s in active:
            self._grid[(s["service_type"], *_cell(s["lat"], s["lng"]))].append(s)
        self._counts = {"demo": sum(1 for s in active if s["source"] == "demo"),
                        "nhs": sum(1 for s in active if s["source"] != "demo")}

    @classmethod
    def from_dir(cls, data_dir: Path, distances: DistanceService,
                 mode: str = "auto") -> "UkServiceDirectory":
        body = json.loads((data_dir / "uk" / "services.json").read_text(encoding="utf-8"))
        nhs_path = data_dir / NHS_FILE
        nhs, meta = [], {}
        if nhs_path.exists() and mode != "demo":
            doc = json.loads(nhs_path.read_text(encoding="utf-8"))
            nhs, meta = doc["services"], doc.get("meta") or {}
        if mode == "auto":
            mode = "mixed" if nhs else "demo"
        return cls(body["services"], distances, nhs=nhs, mode=mode, nhs_meta=meta)

    # ---- the demo area ---------------------------------------------------
    @staticmethod
    def _box(rows: list[dict]) -> tuple[float, float, float, float] | None:
        if not rows:
            return None
        lats, lngs = [r["lat"] for r in rows], [r["lng"] for r in rows]
        mid = (min(lats) + max(lats)) / 2
        dlat = DEMO_AREA_MARGIN_KM / 111.0
        dlng = DEMO_AREA_MARGIN_KM / (111.0 * math.cos(math.radians(mid)))
        return min(lats) - dlat, max(lats) + dlat, min(lngs) - dlng, max(lngs) + dlng

    def in_demo_area(self, lat: float, lng: float) -> bool:
        b = self._demo_box
        return bool(b) and b[0] <= lat <= b[1] and b[2] <= lng <= b[3]

    def info(self) -> dict:
        """What the page and the answers say about where the data comes from."""
        has_nhs = self._counts["nhs"] > 0
        if not has_nhs:
            coverage = "This demo's directory only has services in and around Leeds."
        elif self.mode == "nhs":
            coverage = ("Pharmacies come from NHS open data for England; other services "
                        "are demo data around Leeds.")
        else:
            coverage = ("Around Leeds every service is demo data. Elsewhere in England, "
                        "pharmacies come from NHS open data; other services are demo data "
                        "around Leeds.")
        return {"mode": self.mode, "counts": dict(self._counts), "coverage": coverage,
                "attribution": self.nhs_meta.get("attribution") if has_nhs else None,
                "nhs_data_date": self.nhs_meta.get("generated") if has_nhs else None}

    def get(self, service_id: str) -> dict | None:
        s = self._services.get(service_id)
        return copy.deepcopy(s) if s else None

    def _near(self, service_type: str, lat: float, lng: float, radius_km: float):
        dlat = radius_km / 111.0
        dlng = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        (a, b), (c, d) = _cell(lat - dlat, lng - dlng), _cell(lat + dlat, lng + dlng)
        for i in range(a, c + 1):
            for j in range(b, d + 1):
                yield from self._grid.get((service_type, i, j), ())

    async def search(self, service_type: str, lat: float, lng: float, *, radius_km: float = 5,
                     k: int = 3, age: int | None = None, pharmacy_first: bool = False) -> list[dict]:
        """Nearest services of one type within radius_km (straight-line pre-filter),
        upgraded to road distance, nearest first."""
        rows = []
        for s in self._near(service_type, lat, lng, radius_km):
            # Pharmacy First: False rules a pharmacy out; None (unknown, NHS
            # register data) stays in and is rendered as "check with them".
            if pharmacy_first and s.get("pharmacy_first") is False:
                continue
            if age is not None and age < s.get("min_age", 0):
                continue
            d = haversine_km(lat, lng, s["lat"], s["lng"])
            if d <= radius_km:
                rows.append((d, s))
        rows.sort(key=lambda r: (r[0], r[1]["id"]))      # same postcode: stable order
        shortlist = [copy.deepcopy(s) for _, s in rows[:k]]
        annotated = await self.distances.annotate(lat, lng, shortlist)
        annotated.sort(key=lambda s: s["distance_km"])
        return [{f: s.get(f) for f in PUBLIC_FIELDS if f in s or f in ("drive_min",)}
                for s in annotated]
