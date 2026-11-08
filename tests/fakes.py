"""
tests/fakes.py — test doubles, injected through the Container.

Before the OOP refactor, tests swapped module globals (tools._nominatim,
osm._fetch_nearby, agent_langgraph.llm_with_tools ...) and had to set env
vars BEFORE importing anything. Now a test builds a Container and replaces
the parts it cares about. No network, no API key, no import-order tricks.
"""

from __future__ import annotations

import os
import uuid

from careroute.config import Settings
from careroute.container import Container
from careroute.maps.geocoding import NominatimGeocoder

os.environ.setdefault("OPENAI_API_KEY", "offline-test")   # never used

LOC = {"patient_lat": 12.9352, "patient_lng": 77.6245}    # P001, Koramangala


def make_settings(**overrides) -> Settings:
    """Real env (so CI's REDIS_URL/DATABASE_URL select shared mode), never the
    .env file, the offline dummy backend, and a private Redis namespace (Redis
    state outlives the process, so a previous run's empty rate-limit bucket
    would otherwise cause 429s)."""
    base = Settings.from_env(load_env_file=False).replace(
        provider_backend="", key_prefix=f"careroute-test-{uuid.uuid4().hex[:8]}",
        rate_per_min=1000, burst=1000,
        osrm_base_url="http://127.0.0.1:9")      # closed port: straight-line
    return base.replace(**overrides)


class ScriptedModel:
    """Duck-types a tool-bound chat model: .ainvoke() pops the next scripted
    AIMessage and records what the graph sent (so tests can read the prompt)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self.script.pop(0)


class FailingModel:
    """A model whose every call raises (e.g. openai.APIConnectionError)."""

    def __init__(self, exc: Exception):
        self.exc = exc

    async def ainvoke(self, messages):
        raise self.exc


def call(name, args, i):
    return {"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}


class FakeGeocoder(NominatimGeocoder):
    """known: {query: (lat, lng, place_rank)}; anything else is not found."""

    def __init__(self, known: dict | None = None, country: str | None = "IN"):
        super().__init__("http://geocoder.invalid")
        self.known = known or {}
        self.country = country

    async def _search(self, query):
        if query in self.known:
            lat, lng, rank = self.known[query]
            return [{"lat": str(lat), "lon": str(lng), "place_rank": rank,
                     "display_name": f"{query}, Bengaluru, Karnataka, India"}]
        return []

    async def reverse_country(self, lat, lng):
        return self.country


class StaticSource:
    """An OSM ElementSource returning fixed elements (or a fixed error)."""
    name = "static"

    def __init__(self, elements=None, error=None):
        self._elements = elements or []
        self._error = error

    async def elements(self, lat, lng, radius_m):
        return (None, self._error) if self._error else (self._elements, None)


# Containers made in the current test, closed by close_containers(). Each test
# runs on its own event loop, and a Redis client or Postgres pool must be
# closed on the loop that opened it.
_OPEN: list[Container] = []


def make_container(model=None, **settings_overrides) -> Container:
    c = Container(make_settings(**settings_overrides))
    c.geocoder = FakeGeocoder()
    c.osm_source = StaticSource()          # emergency hospital lookups: no network
    c.model = model or ScriptedModel([])
    _OPEN.append(c)
    return c


async def close_containers() -> None:
    while _OPEN:
        await _OPEN.pop().aclose()


def as_async(value):
    """Wrap a fixed value as an async callable (to stub a coroutine method)."""
    async def stub(*args, **kwargs):
        return value
    return stub


# --- UK CareRoute ------------------------------------------------------------
UK_NOW = __import__("datetime").datetime(2026, 10, 13, 9, 0)      # a Tuesday, 10am BST
PRIYA, ARJUN, MAYA, TOM, MARGARET, SAM = (
    "9990010102", "9990010218", "9990010307", "9990010404", "9990010501", "9990010609")


class NoLlmSteps:
    """Fails the test if any single-shot LLM step runs."""

    async def identify_subject(self, *a, **k):
        raise AssertionError("identify_subject must not run")

    async def triage(self, *a, **k):
        raise AssertionError("LLM triage must not run")

    async def extract_preferences(self, *a, **k):
        raise AssertionError("extract_preferences must not run")


def make_uk_container(route_model=None, steps=None, **settings_overrides) -> Container:
    """Offline LLM steps and route model unless replaced, no network geocoding,
    a fixed clock. Replace parts BEFORE first use of c.journey."""
    from careroute.uk.clock import FixedClock
    c = make_container(**{"llm_mode": "offline", "uk_geocode_network": False,
                          **settings_overrides})
    c.uk_clock = FixedClock(UK_NOW)
    if steps is not None:
        c.uk_steps = steps
    if route_model is not None:
        c.uk_route_model = route_model
    return c


async def uk_chat(c, who, message, thread_id=None, **kw) -> dict:
    svc = c.journey_service
    return await svc.collect(svc.stream_chat(who, message=message, thread_id=thread_id, **kw))


async def uk_resume(c, who, thread_id, value) -> dict:
    svc = c.journey_service
    return await svc.collect(svc.stream_resume(who, thread_id=thread_id, value=value))
