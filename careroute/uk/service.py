"""
uk/service.py — one HTTP call, end to end.

    1. identity     verified NHS number from NHS login (the API, never the model)
    2. access       a symptoms thread belongs to whoever started it; a
                    referral thread to anyone who may act for its patient.
                    No access looks exactly like "not found".
    3. lock         one run per thread at a time (Redis SET NX, else 409)
    4. run          astream the graph; events go out as they happen:
                      status    live line, BEFORE work ("Searching pharmacies…")
                      step      a node finished (the trace panel)
                      progress  a detail of finished work ("Found 3 …")
    5. result       the new answer, the pending interrupt (as buttons), the outcome
    6. checkpoint   one per run (durability="exit"); lock released

Resuming an interrupt is the same flow with Command(resume=...), and the
current requester and their authority are written into state in the same
step, so a booking always runs as the person who confirmed it.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from careroute.agent.messages import text_of
from careroute.uk.authority import AuthorityService
from careroute.uk.geo import UkGeocoder
from careroute.uk.graph import JourneyGraph
from careroute.uk.integrations import DemoErs
from careroute.uk.state import fresh_turn
from careroute.uk.store import DAY, JourneyStore

THREAD_TTL_S = 30 * DAY

STEP_LABELS = {
    "assess": "Checked for emergency signs",
    "authorise": "Checked who you can act for",
    "intake": "Read the patient record",
    "route_agent": "Chose the right front door",
    "direct_access": "Checked how each service takes patients",
    "registered_gp": "Checked the GP practice",
    "load_referral": "Loaded the referral from e-RS",
    "extract_preferences": "Read your limits",
    "shortlist": "Shortlisted services against your limits",
    "check_slots": "Checked available times",
    "book": "Made the booking",
    "notify": "Sent a confirmation text",
}

# Present tense, shown live in the chat as each node STARTS (from the graph's
# "tasks" stream). Interrupt nodes are left out: there the patient is
# deciding, and the page shows a card instead.
NODE_STATUS = {
    "assess": "Checking for emergency signs\u2026",
    "authorise": "Checking who you can act for\u2026",
    "intake": "Reading the patient record\u2026",
    "route_agent": "Working out the right place to go\u2026",
    "direct_access": "Checking how each service takes patients\u2026",
    "registered_gp": "Checking the GP practice\u2026",
    "load_referral": "Loading the referral from e-RS\u2026",
    "extract_preferences": "Reading your limits\u2026",
    "shortlist": "Comparing services against your limits\u2026",
    "check_slots": "Checking available times\u2026",
    "book": "Making the booking\u2026",
    "notify": "Sending a confirmation text\u2026",
}


class JourneyService:
    def __init__(self, journey: JourneyGraph, checkpointers, *, store: JourneyStore,
                 authority: AuthorityService, sessions, geocoder: UkGeocoder, ers: DemoErs,
                 log=None):
        self.journey = journey
        self.checkpointers = checkpointers
        self.store = store
        self.authority = authority
        self.sessions = sessions                 # provides the per-thread turn lock
        self.geocoder = geocoder
        self.ers = ers
        self.log = log
        self._graph = None
        self._lock: asyncio.Lock | None = None

    async def graph(self):
        if self._graph is None:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if self._graph is None:
                    self._graph = self.journey.builder.compile(
                        checkpointer=await self.checkpointers.get())
        return self._graph

    # ------------------------------------------------------------ access
    async def meta(self, thread_id: str) -> dict | None:
        return await self.store.get("thread", thread_id)

    async def check_access(self, requester: str | None, thread_id: str) -> dict:
        meta = await self.meta(thread_id)
        if meta is None:
            raise HTTPException(404, "Conversation not found.")
        if meta["entry"] == "symptoms":
            if meta.get("owner") != requester:
                raise HTTPException(404, "Conversation not found.")
        else:
            if not await self.authority.can_act_for(requester, meta.get("patient_id")):
                raise HTTPException(404, "Conversation not found.")
        return meta

    async def location_hint(self, lat, lng, location_text) -> dict | None:
        text = (location_text or "").strip()
        if text:
            if len(text) > 200:
                raise HTTPException(422, "That location is too long. Try a postcode.")
            g = await self.geocoder.geocode(text)
            if g is None:
                raise HTTPException(422, f"We couldn't find \u201c{text}\u201d. Try a postcode, "
                                         "e.g. LS6 3HN.")
            return g
        if lat is not None and lng is not None:
            return {"lat": lat, "lng": lng, "label": "your current location", "source": "gps"}
        return None

    # --------------------------------------------------------------- runs
    async def _locked(self, thread_id: str):
        token = await self.sessions.acquire_turn_lock(thread_id)
        if token is None:
            raise HTTPException(409, "Still working on your previous message. Please wait "
                                     "for that answer before sending another.")
        return token

    async def _release(self, thread_id: str, token: str) -> None:
        try:
            await self.sessions.release_turn_lock(thread_id, token)
        except Exception:
            pass                                    # the lock's TTL frees it

    async def stream_chat(self, requester: str | None, *, message: str, thread_id: str | None,
                          lat=None, lng=None, location_text=None):
        """Async iterator of events: {"type": "progress"|"step"|"result", ...}."""
        if not (message or "").strip():
            raise HTTPException(400, "message must not be empty.")
        if len(message) > 4000:
            raise HTTPException(413, "That message is too long.")
        hint = await self.location_hint(lat, lng, location_text)
        meta = await self.check_access(requester, thread_id) if thread_id else None
        thread_id = thread_id or uuid.uuid4().hex
        entry = meta["entry"] if meta else "symptoms"
        if entry == "referral":
            inp = {"messages": [HumanMessage(content=message)], "entry": "referral",
                   "requester_id": requester, "outcome": None, "selected_option": None,
                   "available_slots": [], "selected_slot": None, "consent": False,
                   "booking": None, "booking_attempts": 0, "excluded_options": [],
                   "authority": await self.authority.can_act_for(requester, meta["patient_id"])}
        else:
            inp = {**fresh_turn(), "messages": [HumanMessage(content=message)],
                   "entry": "symptoms", "requester_id": requester, "location_hint": hint}
        token = await self._locked(thread_id)
        try:
            async for ev in self._run(inp, thread_id):
                yield ev
            if meta is None:
                state = (await (await self.graph()).aget_state(self._config(thread_id))).values
                await self.store.put("thread", thread_id, {
                    "thread_id": thread_id, "entry": "symptoms", "owner": requester,
                    "patient_id": state.get("patient_id"), "created": time.time()},
                    ttl_s=THREAD_TTL_S)
        finally:
            await self._release(thread_id, token)

    async def stream_resume(self, requester: str | None, *, thread_id: str, value: dict):
        meta = await self.check_access(requester, thread_id)
        g = await self.graph()
        state = await g.aget_state(self._config(thread_id))
        if not any(t.interrupts for t in state.tasks):
            raise HTTPException(409, "There is nothing waiting for an answer in this conversation.")
        patient_id = state.values.get("patient_id") or meta.get("patient_id")
        update = {"requester_id": requester}
        auth = await self.authority.can_act_for(requester, patient_id) if patient_id else None
        if meta["entry"] == "referral" or auth:
            update["authority"] = auth
        token = await self._locked(thread_id)
        try:
            async for ev in self._run(Command(resume=value or {}, update=update), thread_id):
                yield ev
        finally:
            await self._release(thread_id, token)

    async def start_referral(self, ubrn: str) -> dict:
        """A referral event from e-RS starts (or returns) a Path B thread.
        Idempotent: the thread id is derived from the UBRN."""
        ref = await self.ers.get_referral(ubrn)
        if ref is None:
            raise HTTPException(404, "Unknown UBRN.")
        thread_id = f"ref-{ubrn}"
        if await self.meta(thread_id) is None:
            token = await self._locked(thread_id)
            try:
                if await self.meta(thread_id) is None:
                    async for _ in self._run({"messages": [], "entry": "referral",
                                              "referral": {"ubrn": ubrn}, "requester_id": None,
                                              "llm_calls": 0, "tool_errors": []}, thread_id):
                        pass
                    await self.store.put("thread", thread_id, {
                        "thread_id": thread_id, "entry": "referral", "owner": None,
                        "patient_id": ref["nhs_number"], "ubrn": ubrn,
                        "specialty": ref["specialty_label"], "created": time.time()},
                        ttl_s=THREAD_TTL_S)
                    await self.store.append("patient_threads", ref["nhs_number"], thread_id,
                                            ttl_s=THREAD_TTL_S)
            finally:
                await self._release(thread_id, token)
        return {"thread_id": thread_id, "ubrn": ubrn}

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def _run(self, inp, thread_id: str):
        g = await self.graph()
        config = self._config(thread_id)
        before = await g.aget_state(config)
        n_before = len((before.values or {}).get("messages") or [])
        t0 = time.perf_counter()
        async for mode, chunk in g.astream(inp, config,
                                           stream_mode=["tasks", "updates", "custom"],
                                           durability="exit"):
            if mode == "tasks":
                # A task-start event carries "input"; its finish event doesn't.
                if "input" in chunk and chunk.get("name") in NODE_STATUS:
                    yield {"type": "status", "text": NODE_STATUS[chunk["name"]]}
            elif mode == "custom" and isinstance(chunk, dict):
                if chunk.get("status"):
                    yield {"type": "status", "text": chunk["status"]}
                elif chunk.get("progress"):
                    yield {"type": "progress", "text": chunk["progress"]}
            elif mode == "updates":
                for node in chunk:
                    if node in STEP_LABELS:
                        yield {"type": "step", "node": node, "label": STEP_LABELS[node]}
        after = await g.aget_state(config)
        result = self._result(thread_id, after, n_before)
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        if self.log is not None:
            self._log(result)
        yield {"type": "result", **result}

    @staticmethod
    def _pending(state) -> dict | None:
        for t in state.tasks:
            for i in t.interrupts:
                return i.value
        return None

    def _result(self, thread_id: str, state, n_before: int) -> dict:
        v = state.values or {}
        msgs = v.get("messages") or []
        new_ai = [text_of(m) for m in msgs[n_before:] if isinstance(m, AIMessage)]
        return {"thread_id": thread_id, "entry": v.get("entry"),
                "answer": "\n\n".join(t for t in new_ai if t),
                "outcome": v.get("outcome"), "pending": self._pending(state),
                "triage": v.get("triage"), "care_route": v.get("care_route"),
                "booking": v.get("booking"), "llm_calls": v.get("llm_calls", 0),
                "for": (v.get("patient") or {}).get("first_name")}

    def _log(self, r: dict) -> None:
        """Privacy-conscious: no message text, no names, no NHS numbers."""
        try:
            import json
            print(json.dumps({"event": "uk_turn", "thread": r["thread_id"][:12],
                              "entry": r["entry"], "outcome": r["outcome"],
                              "triage": (r.get("triage") or {}).get("level"),
                              "care_route": r["care_route"],
                              "pending": (r.get("pending") or {}).get("type"),
                              "llm_calls": r["llm_calls"], "latency_ms": r["latency_ms"]}))
        except Exception:
            pass

    async def collect(self, events) -> dict:
        """Drain an event iterator into the JSON response body."""
        steps, result = [], {}
        async for ev in events:
            if ev["type"] == "result":
                result = ev
            elif ev["type"] != "status":          # live lines only matter while streaming
                steps.append(ev)
        result.pop("type", None)
        return {**result, "steps": steps}

    async def thread_view(self, requester: str | None, thread_id: str) -> dict:
        meta = await self.check_access(requester, thread_id)
        state = await (await self.graph()).aget_state(self._config(thread_id))
        v = state.values or {}
        msgs = [{"role": "user" if isinstance(m, HumanMessage) else "assistant",
                 "text": text_of(m)} for m in v.get("messages") or []
                if isinstance(m, (HumanMessage, AIMessage)) and text_of(m)]
        return {"thread_id": thread_id, "entry": meta["entry"], "messages": msgs,
                "pending": self._pending(state), "outcome": v.get("outcome"),
                "booking": v.get("booking"), "for": (v.get("patient") or {}).get("first_name")}
