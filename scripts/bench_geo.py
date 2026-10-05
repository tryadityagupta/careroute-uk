"""
scripts/bench_geo.py — what one provider-directory lookup costs a replica.

Runs each lookup SEQUENTIALLY on one event loop and records, per lookup:
  wall  total time the caller waits (DB round trip + Python)
  cpu   this process's CPU time (time.process_time) = time the EVENT LOOP was
        busy and could not serve anyone else. Postgres runs in its own process,
        so its work is not in this number.

OSRM points at a closed port (straight-line distances), so routing is excluded.

    python -m scripts.bench_geo postgresql://careroute:careroute@localhost:5432/careroute
"""

from __future__ import annotations

import contextlib
import io
import random
import statistics
import sys
import time

import httpx

from careroute.domain.emergency import EmergencyNumberResolver, EmergencyService
from careroute.maps.postgis import GeoDatabase
from careroute.maps.routing import OsrmRouter
from careroute.providers.osm.directory import OsmProviderDirectory
from careroute.providers.osm.sources import PostgisSource
from careroute.runtime import new_event_loop
from tests.fakes import FakeGeocoder
from tests.geo_synth import CENTRE

LOOKUPS = {
    "specialty (Cardiology)": lambda d, e, p, r: d.find_providers("Cardiology", *p, k=3, radius_m=r),
    "specialty (Psychiatry)": lambda d, e, p, r: d.find_providers("Psychiatry", *p, k=3, radius_m=r),
    "general facilities": lambda d, e, p, r: d.find_general_facilities(*p, k=3, radius_m=r),
    "pharmacies": lambda d, e, p, r: d.find_pharmacies(*p, k=3, radius_m=r),
    "emergency hospitals": lambda d, e, p, r: e.nearest_hospitals(*p),
}


def points(n: int, seed: int = 1) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    return [(CENTRE[0] + rng.uniform(-0.04, 0.04), CENTRE[1] + rng.uniform(-0.04, 0.04))
            for _ in range(n)]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main(dsn: str, n_points: int = 40) -> None:
    db = GeoDatabase(dsn)
    async with httpx.AsyncClient() as http:
        directory = OsmProviderDirectory(PostgisSource(db), OsrmRouter("http://127.0.0.1:9",
                                                                       client=http))
        emergency = EmergencyService(EmergencyNumberResolver(FakeGeocoder()), directory)
        print(f"rows in geo.healthcare: {(await db.dataset_info())['rows']}")
        pts = points(n_points)
        for radius in (8000, 30000):
            print(f"\nradius {radius / 1000:.0f} km  ({n_points} points, ms)")
            print(f"  {'lookup':<24}{'wall p50':>9}{'wall p95':>9}{'cpu p50':>9}{'cpu p95':>9}")
            for label, fn in LOOKUPS.items():
                if label.startswith("emergency") and radius != 8000:
                    continue                              # fixed 8 km radius
                wall, cpu = [], []
                for p in [pts[0]] + pts:                  # first call warms the pool
                    with contextlib.redirect_stdout(io.StringIO()):   # [osm] log lines
                        t0, c0 = time.perf_counter(), time.process_time()
                        await fn(directory, emergency, p, radius)
                        wall.append((time.perf_counter() - t0) * 1000)
                        cpu.append((time.process_time() - c0) * 1000)
                wall, cpu = wall[1:], cpu[1:]
                print(f"  {label:<24}{statistics.median(wall):9.1f}{pct(wall, .95):9.1f}"
                      f"{statistics.median(cpu):9.1f}{pct(cpu, .95):9.1f}")
    await db.aclose()


if __name__ == "__main__":
    loop = new_event_loop()
    try:
        loop.run_until_complete(main(sys.argv[1]))
    finally:
        loop.close()
