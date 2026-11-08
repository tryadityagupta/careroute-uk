"""
careroute/container.py — the composition root: the ONE place objects are built.

Every class in the package receives its collaborators through its constructor
(dependency injection); this file decides which concrete ones. Swapping Redis
for memory, Overpass for PostGIS or OpenAI for a scripted model is a decision
made here, from Settings — never by an `if` buried inside a module.

Everything is built LAZILY (cached_property) on first access, which gives two
properties tests rely on:
  * nothing connects to Redis/Postgres/the network just because you made a
    Container;
  * any part can be replaced before first use, e.g.
        c = Container(settings)
        c.geocoder = FakeGeocoder(...)        # before c.agent is touched
        c.agent.model = ScriptedModel([...])  # swap the model any time

ASYNC LIFECYCLE: the I/O parts (HTTP client, Redis client, Postgres pools)
bind to the event loop they are first used on. aclose() closes whichever were
built; the FastAPI app calls it on shutdown, tests call it at the end of each
test, and the CLI calls it before exiting.
"""

from __future__ import annotations

from functools import cached_property

import httpx

from careroute.agent.checkpointer import CheckpointerFactory
from careroute.agent.graph import CareRouteAgent
from careroute.agent.toolkit import LangChainToolkit
from careroute.agent.tracing import TurnTracer
from careroute.api.location import LocationResolver
from careroute.api.services import CareService, ChatService
from careroute.config import Settings
from careroute.domain.emergency import EmergencyNumberResolver, EmergencyService
from careroute.domain.patients import PatientRepository
from careroute.domain.tools import CareTools
from careroute.maps.geocoding import NominatimGeocoder
from careroute.maps.postgis import GeoDatabase
from careroute.maps.routing import OsrmRouter
from careroute.observability.interaction_log import InteractionLogger
from careroute.providers.base import ProviderDirectory
from careroute.providers.dummy import JsonProviderDirectory
from careroute.providers.osm.directory import OsmProviderDirectory
from careroute.providers.osm.matching import RULES_VERSION
from careroute.providers.osm.sources import (ElementSource, OverpassDiskCache,
                                             OverpassSource, PostgisSource)
from careroute.security.gate import RequestGate
from careroute.security.limits import (MemoryDailyCap, MemoryRateLimiter,
                                       RedisDailyCap, RedisRateLimiter)
from careroute.storage.patient_store import MemoryPatientStore, RedisPatientStore
from careroute.storage.redis_client import RedisConnection
from careroute.storage.seed_data import SeedDataSource
from careroute.storage.sessions import MemorySessionStore, RedisSessionStore


class Container:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.settings.validate()

    def describe(self) -> str:
        s = self.settings
        uk = (f"[careroute-uk] llm: {s.llm_mode} | NHS login: {s.nhs_login_mode} | "
              f"sms: {'GOV.UK Notify' if s.notify_api_key else 'demo outbox'} | "
              f"demo tools: {'on' if s.demo_tools else 'off'}\n")
        d = self.uk_directory.info()
        uk += (f"[careroute-uk] directory: {d['mode']} | demo services: {d['counts']['demo']} | "
               f"NHS open data services: {d['counts']['nhs']}\n")
        if s.nhs_login_mode == "demo":
            uk += ("[careroute-uk] WARNING: demo login accepts any demo NHS number in a "
                   "header. Never expose this mode with real data.\n")
        return uk + (f"[careroute] legacy backend: {self.directory.backend_name}"
                f" (osm source: {s.osm_source}) | sessions/patients/rate-limits: "
                f"{'redis' if s.use_redis else 'memory (single replica only)'} | "
                f"checkpoints: {'postgres' if s.use_postgres else 'memory (single replica only)'}"
                f"\n[careroute] {self.gate.describe()}")

    # --- infrastructure --------------------------------------------------------
    @cached_property
    def redis(self) -> RedisConnection | None:
        s = self.settings
        if not s.use_redis:
            return None
        return RedisConnection(s.redis_url, timeout_s=s.redis_timeout_s, key_prefix=s.key_prefix)

    @cached_property
    def http(self) -> httpx.AsyncClient:
        """ONE pooled async HTTP client for OSRM / Nominatim / Overpass /
        Google: keep-alive connections, shared by every request. Per-call
        timeouts are set by each service. The pool limits are sized for a
        replica serving hundreds of concurrent turns."""
        return httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))

    @cached_property
    def geo_db(self) -> GeoDatabase:
        s = self.settings
        return GeoDatabase(s.geo_dsn, pool_max=s.geo_pool_max,
                           statement_timeout_ms=s.geo_statement_timeout_ms,
                           rules_version=RULES_VERSION)

    @cached_property
    def checkpointers(self) -> CheckpointerFactory:
        s = self.settings
        return CheckpointerFactory(s.database_url, pool_max=s.pg_pool_max,
                                   auto_migrate=s.pg_auto_migrate)

    # --- storage -----------------------------------------------------------
    @cached_property
    def sessions(self):
        s = self.settings
        if self.redis:
            return RedisSessionStore(self.redis, ttl_s=s.session_ttl_s, turn_lock_s=s.turn_lock_s)
        return MemorySessionStore(ttl_s=s.session_ttl_s, turn_lock_s=s.turn_lock_s,
                                  max_sessions=s.max_sessions)

    @cached_property
    def patient_store(self):
        s = self.settings
        if self.redis:
            return RedisPatientStore(self.redis, ttl_s=s.session_ttl_s)
        return MemoryPatientStore(ttl_s=s.session_ttl_s)

    @cached_property
    def seed_data(self) -> SeedDataSource:
        s = self.settings
        return SeedDataSource(s.data_dir, blob_account_url=s.blob_account_url,
                              blob_container=s.blob_container)

    @cached_property
    def patients(self) -> PatientRepository:
        return PatientRepository(self.seed_data.patients(), self.patient_store)

    # --- maps and providers --------------------------------------------------
    @cached_property
    def geocoder(self) -> NominatimGeocoder:
        s = self.settings
        return NominatimGeocoder(s.nominatim_url, countries=s.geocode_countries, client=self.http)

    @cached_property
    def router(self) -> OsrmRouter:
        s = self.settings
        return OsrmRouter(s.osrm_base_url, timeout_s=s.osrm_timeout_s, client=self.http)

    @cached_property
    def osm_source(self) -> ElementSource:
        if self.settings.osm_source == "postgis":
            return PostgisSource(self.geo_db)
        return OverpassSource(OverpassDiskCache(self.settings.data_dir / "osm_cache.json"),
                              client=self.http)

    @cached_property
    def osm_directory(self) -> OsmProviderDirectory:
        """Always built: the emergency service finds hospitals through OSM
        whichever backend serves specialist searches."""
        return OsmProviderDirectory(self.osm_source, self.router)

    @cached_property
    def directory(self) -> ProviderDirectory:
        backend = self.settings.provider_backend
        if backend == "osm":
            return self.osm_directory
        if backend == "google":
            from careroute.providers.google import GooglePlacesDirectory
            return GooglePlacesDirectory(self.settings.google_maps_api_key, client=self.http)
        return JsonProviderDirectory(self.seed_data.providers())

    @cached_property
    def emergency(self) -> EmergencyService:
        return EmergencyService(EmergencyNumberResolver(self.geocoder), self.osm_directory)

    # --- agent -------------------------------------------------------------
    @cached_property
    def care_tools(self) -> CareTools:
        return CareTools(self.patients, self.directory, self.geocoder, self.emergency)

    @cached_property
    def toolkit(self) -> LangChainToolkit:
        return LangChainToolkit(self.care_tools)

    @cached_property
    def model(self):
        """The real model. Tests replace this (or agent.model) with a script."""
        from langchain_openai import ChatOpenAI
        s = self.settings
        llm = ChatOpenAI(model=s.openai_model, timeout=s.llm_timeout_s,
                         max_retries=s.llm_max_retries)
        return llm.bind_tools(self.toolkit.tools)

    @cached_property
    def agent(self) -> CareRouteAgent:
        return CareRouteAgent(self.model, self.toolkit.tools, checkpointers=self.checkpointers,
                              tracer=TurnTracer(self.settings.log_coord_dp),
                              max_llm_calls=self.settings.max_llm_calls)

    # --- HTTP-facing ---------------------------------------------------------
    @cached_property
    def gate(self) -> RequestGate:
        s = self.settings
        if self.redis:
            limiter = RedisRateLimiter(self.redis, s.rate_per_min, s.burst)
            daily = RedisDailyCap(self.redis, s.daily_call_cap)
        else:
            limiter = MemoryRateLimiter(s.rate_per_min, s.burst)
            daily = MemoryDailyCap(s.daily_call_cap)
        return RequestGate(limiter, daily, api_keys=s.api_keys, trust_xff=s.trust_xff)

    @cached_property
    def interaction_log(self) -> InteractionLogger:
        s = self.settings
        return InteractionLogger(log_pii=s.log_pii, coord_dp=s.log_coord_dp, log_file=s.log_file)

    @cached_property
    def locations(self) -> LocationResolver:
        return LocationResolver(self.geocoder)

    def _service_args(self) -> dict:
        return dict(agent=self.agent, patients=self.patients, locations=self.locations,
                    log=self.interaction_log, backend_name=self.directory.backend_name)

    @cached_property
    def care_service(self) -> CareService:
        return CareService(**self._service_args())

    @cached_property
    def chat_service(self) -> ChatService:
        return ChatService(**self._service_args(), sessions=self.sessions)

    # --- UK CareRoute ----------------------------------------------------------
    @cached_property
    def uk_clock(self):
        from careroute.uk.clock import Clock
        return Clock()

    @cached_property
    def uk_store(self):
        from careroute.uk.store import MemoryJourneyStore, RedisJourneyStore
        return RedisJourneyStore(self.redis) if self.redis else MemoryJourneyStore()

    @cached_property
    def uk_records(self):
        from careroute.uk.records import NhsRecords
        return NhsRecords.from_dir(self.settings.data_dir, self.uk_clock)

    @cached_property
    def uk_geocoder(self):
        from careroute.uk.geo import UkGeocoder
        from careroute.uk.integrations import load_json
        s = self.settings
        return UkGeocoder(self.geocoder, offline_postcodes=load_json(s.data_dir, "postcodes.json"),
                          postcodes_io_url=s.postcodes_io_url, client=self.http,
                          use_network=s.uk_geocode_network)

    @cached_property
    def uk_distances(self):
        from careroute.uk.geo import DistanceService
        return DistanceService(self.router)

    @cached_property
    def uk_directory(self):
        from careroute.uk.directory import UkServiceDirectory
        return UkServiceDirectory.from_dir(self.settings.data_dir, self.uk_distances,
                                           mode=self.settings.uk_directory)

    @cached_property
    def uk_consents(self):
        from careroute.uk.authority import ConsentService
        return ConsentService(self.uk_store, self.uk_clock)

    @cached_property
    def uk_authority(self):
        from careroute.uk.authority import AuthorityService
        return AuthorityService(self.uk_records, self.uk_consents)

    @cached_property
    def uk_identity(self):
        from careroute.uk.identity import DemoIdentity, NhsLoginIdentity
        s = self.settings
        if s.nhs_login_mode == "oidc":
            return NhsLoginIdentity(s.nhs_login_issuer, s.nhs_login_client_id, client=self.http)
        return DemoIdentity(self.uk_records)

    @cached_property
    def uk_booking_core(self):
        from careroute.uk.integrations import DemoBookingCore
        return DemoBookingCore(self.uk_store, self.uk_clock)

    @cached_property
    def uk_ers(self):
        from careroute.uk.integrations import DemoErs, load_json
        return DemoErs(load_json(self.settings.data_dir, "ers.json"), self.uk_booking_core)

    @cached_property
    def uk_private(self) -> dict:
        from careroute.uk.integrations import load_json
        return load_json(self.settings.data_dir, "private.json")

    @cached_property
    def uk_notifier(self):
        from careroute.uk.integrations import DemoOutbox, GovUkNotify
        s = self.settings
        if s.notify_api_key:
            return GovUkNotify(s.notify_api_key, s.notify_sms_template_id, self.uk_store,
                               client=self.http)
        return DemoOutbox(self.uk_store, self.uk_clock)

    @cached_property
    def uk_steps(self):
        from careroute.uk.llm import OfflineSteps, OpenAiSteps
        s = self.settings
        if s.llm_mode == "offline":
            return OfflineSteps()
        from langchain_openai import ChatOpenAI
        main = ChatOpenAI(model=s.openai_model, timeout=s.llm_timeout_s,
                          max_retries=s.llm_max_retries, temperature=0)
        small = ChatOpenAI(model=s.extraction_model, timeout=s.llm_timeout_s,
                           max_retries=s.llm_max_retries, temperature=0)
        return OpenAiSteps(main, small)

    @cached_property
    def uk_route_toolkit(self):
        from careroute.uk.route_agent import RouteToolkit
        return RouteToolkit(self.uk_directory, self.uk_records, self.uk_geocoder)

    @cached_property
    def uk_route_model(self):
        s = self.settings
        if s.llm_mode == "offline":
            from careroute.uk.offline_model import OfflineRouteModel
            return OfflineRouteModel()
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=s.openai_model, timeout=s.llm_timeout_s,
                          max_retries=s.llm_max_retries, temperature=0).bind_tools(
            self.uk_route_toolkit.tools)

    @cached_property
    def uk_route_agent(self):
        from careroute.uk.route_agent import RouteAgent
        return RouteAgent(self.uk_route_model, self.uk_route_toolkit,
                          max_llm_calls=self.settings.route_max_llm_calls)

    @cached_property
    def journey(self):
        from careroute.uk.graph import JourneyDeps, JourneyGraph
        from careroute.uk.integrations import (DemoGpSystem, DemoInsurer, DemoPayments,
                                               DemoProviderBooking)
        core = self.uk_booking_core
        return JourneyGraph(JourneyDeps(
            records=self.uk_records, directory=self.uk_directory, distances=self.uk_distances,
            authority=self.uk_authority, consents=self.uk_consents, steps=self.uk_steps,
            route_agent=self.uk_route_agent, ers=self.uk_ers, gp=DemoGpSystem(core),
            booking=DemoProviderBooking(core), insurer=DemoInsurer(self.uk_private, core),
            payments=DemoPayments(core), notifier=self.uk_notifier, store=self.uk_store,
            clock=self.uk_clock, private_providers=self.uk_private["providers"],
            public_base_url=self.settings.public_base_url))

    @cached_property
    def journey_service(self):
        from careroute.uk.service import JourneyService
        return JourneyService(self.journey, self.checkpointers, store=self.uk_store,
                              authority=self.uk_authority, sessions=self.sessions,
                              geocoder=self.uk_geocoder, ers=self.uk_ers, log=True)

    # --- lifecycle -----------------------------------------------------------
    async def aclose(self) -> None:
        """Close every pool this container opened, on the loop that opened it.
        Only touches parts that were actually built (cached_property stores
        built parts in the instance __dict__)."""
        built = self.__dict__
        if "http" in built:
            await self.http.aclose()
        if built.get("redis") is not None:
            await self.redis.aclose()
        if "checkpointers" in built:
            await self.checkpointers.aclose()
        if "geo_db" in built:
            await self.geo_db.aclose()
