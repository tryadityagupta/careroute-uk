"""
uk/graph.py — the UK CareRoute journey as one LangGraph state machine.

  Path A (symptoms)                         Path B (referral event)
  START -> assess --emergency--> emergency_exit -> END
             |   --urgent-----> urgent_exit ----> END
             v
          authorise --not verified--> info_only -> END
             v
          intake -> route_agent --gp--------> registered_gp --+
                        |  --self_care / ask / no match -> END |
                        v                                      |
                    direct_access --walk-in/confidential-> END |
                        |                                      |
                        |       load_referral -> ask_preferences* -> extract_preferences
                        |                                               -> shortlist
                        v                                                    |
      +------------ choose_option* <-------------------------------------- -+
      |  (declined / walk-in -> END)                                          |
      v                                                                       |
  check_slots --no slots--> choose_option*  (or END)       registered_gp -----+
      v                                     (fixed provider: skips choose_option)
  confirm_booking* --cancelled--> END
      v
    book --slot taken--> check_slots      --API error--> END
      v
    notify -> END                           * = interrupt: the patient decides

Only `assess` (two LLM calls in parallel), `route_agent` (the agent) and
`extract_preferences` use a model. Everything that touches safety,
permission, money or a booking is code.

A GP request (registered_gp) records an EPISODE. When the GP later refers,
the e-RS referral carries that episode id, and the referral thread (Path B)
starts with the earlier context: one journey, two threads.

Nothing with a side effect runs before an interrupt() in the same node: a
resumed node re-runs from its start up to interrupt().
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from decimal import Decimal

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from careroute.agent.messages import text_of
from careroute.uk import render, triage_rules
from careroute.uk.authority import AuthorityService, ConsentService
from careroute.uk.clock import Clock
from careroute.uk.directory import UkServiceDirectory
from careroute.uk.geo import DistanceService
from careroute.uk.integrations import (BookingFailed, DemoErs, DemoGpSystem, DemoInsurer,
                                       DemoPayments, DemoProviderBooking, SlotTaken, money)
from careroute.uk.records import NhsRecords
from careroute.uk.route_agent import RouteAgent
from careroute.uk.state import JourneyState
from careroute.uk.store import DAY, JourneyStore

DIRECT_ROUTES = ("pharmacy_first", "pharmacy", "sexual_health", "talking_therapies",
                 "msk_physio")
MAX_BOOKING_ATTEMPTS = 3
CONFIDENTIAL_AGE = 13


def progress(text: str) -> None:
    try:
        get_stream_writer()({"progress": text})
    except Exception:
        pass


def last_human_text(state: dict) -> str:
    for m in reversed(state.get("messages") or []):
        if isinstance(m, HumanMessage):
            return text_of(m)
    return ""


def thread_id_of(config: RunnableConfig) -> str:
    return ((config or {}).get("configurable") or {}).get("thread_id") or "no-thread"


def say(text: str) -> dict:
    return {"messages": [AIMessage(content=text)]}


@dataclass
class JourneyDeps:
    records: NhsRecords
    directory: UkServiceDirectory
    distances: DistanceService
    authority: AuthorityService
    consents: ConsentService
    steps: object                   # OpenAiSteps | OfflineSteps
    route_agent: RouteAgent
    ers: DemoErs
    gp: DemoGpSystem
    booking: DemoProviderBooking
    insurer: DemoInsurer
    payments: DemoPayments
    notifier: object                # DemoOutbox | GovUkNotify
    store: JourneyStore
    clock: Clock
    private_providers: list
    public_base_url: str = "http://localhost:8000"


class JourneyGraph:
    def __init__(self, deps: JourneyDeps):
        self.d = deps
        self.builder = self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> StateGraph:
        b = StateGraph(JourneyState)
        for name in ("assess", "emergency_exit", "urgent_exit", "authorise", "info_only",
                     "intake", "route_agent", "direct_access", "registered_gp",
                     "load_referral", "ask_preferences", "extract_preferences", "shortlist",
                     "choose_option", "check_slots", "confirm_booking", "book", "notify"):
            b.add_node(name, getattr(self, name))

        b.add_conditional_edges(START, self._entry,
                                ["assess", "load_referral", "extract_preferences"])
        b.add_conditional_edges("assess", self._after_assess,
                                ["emergency_exit", "urgent_exit", "authorise"])
        b.add_edge("emergency_exit", END)
        b.add_edge("urgent_exit", END)
        b.add_conditional_edges("authorise", lambda s: "intake" if s.get("patient_id")
                                else "info_only", ["intake", "info_only"])
        b.add_edge("info_only", END)
        b.add_edge("intake", "route_agent")
        b.add_conditional_edges("route_agent", self._after_route,
                                ["registered_gp", "direct_access", END])
        b.add_conditional_edges("direct_access", self._to_choose_or_end,
                                ["choose_option", END])
        b.add_conditional_edges("registered_gp", self._after_registered_gp,
                                ["check_slots", "confirm_booking", END])

        b.add_conditional_edges("load_referral", lambda s: END if s.get("outcome")
                                else "ask_preferences", ["ask_preferences", END])
        b.add_edge("ask_preferences", "extract_preferences")
        b.add_edge("extract_preferences", "shortlist")
        b.add_conditional_edges("shortlist", self._to_choose_or_end, ["choose_option", END])

        b.add_conditional_edges("choose_option", lambda s: "check_slots"
                                if s.get("selected_option") else END, ["check_slots", END])
        b.add_conditional_edges("check_slots", self._after_check_slots,
                                ["confirm_booking", "choose_option", END])
        b.add_conditional_edges("confirm_booking", self._after_confirm,
                                ["book", "check_slots", END])
        b.add_conditional_edges("book", self._after_book, ["notify", "check_slots", END])
        b.add_edge("notify", END)
        return b

    # ----------------------------------------------------------------- routers
    @staticmethod
    def _entry(state: JourneyState) -> str:
        if state.get("entry") == "referral":
            return "extract_preferences" if state.get("preferences") else "load_referral"
        return "assess"

    @staticmethod
    def _after_assess(state: JourneyState) -> str:
        level = (state.get("triage") or {}).get("level")
        return {"emergency": "emergency_exit", "urgent": "urgent_exit"}.get(level, "authorise")

    @staticmethod
    def _after_route(state: JourneyState):
        route = state.get("care_route")
        if state.get("outcome") or not route:
            return END
        if route == "gp":
            return "registered_gp"
        return "direct_access" if route in DIRECT_ROUTES else END

    @staticmethod
    def _to_choose_or_end(state: JourneyState):
        return END if state.get("outcome") else "choose_option"

    @staticmethod
    def _after_registered_gp(state: JourneyState):
        opt = state.get("selected_option")
        if state.get("outcome") or not opt:
            return END
        return "check_slots" if opt["booking_mode"] == "appointment" else "confirm_booking"

    @staticmethod
    def _after_check_slots(state: JourneyState):
        if state.get("outcome"):
            return END
        if state.get("selected_option"):
            return "confirm_booking"
        return "choose_option"

    @staticmethod
    def _after_confirm(state: JourneyState):
        if state.get("outcome"):
            return END
        return "book" if state.get("consent") else "check_slots"

    @staticmethod
    def _after_book(state: JourneyState):
        if state.get("booking"):
            return "notify"
        return END if state.get("outcome") else "check_slots"

    # ================================================================ Path A
    async def assess(self, state: JourneyState):
        """Rules first (no model, instant); then identify_subject and the
        model's triage IN PARALLEL. The model can escalate, never downgrade."""
        text = last_human_text(state)
        rules = triage_rules.evaluate(text)
        if rules["level"] == "emergency":
            progress("Emergency signs found")
            return {"triage": {**rules, "reason": "Matched a red-flag rule."}}
        linked = []
        if state.get("requester_id"):
            linked = [p["first_name"] for p in
                      self.d.records.linked_profiles(state["requester_id"])]
        subject, llm = await asyncio.gather(self.d.steps.identify_subject(text, linked),
                                            self.d.steps.triage(text))
        level = triage_rules.higher(rules["level"], llm.get("level"))
        flags = list(rules["red_flags"])
        if llm.get("level") == level and level != "routine":
            flags += [f for f in llm.get("red_flags") or [] if f not in flags]
        return {"subject": subject, "llm_calls": state.get("llm_calls", 0) + 2,
                "triage": {"level": level, "red_flags": flags,
                           "reason": llm.get("reason") or "",
                           "mental_health": rules["mental_health"],
                           "source": "rules+llm" if rules["source"] else "llm"}}

    async def _nearest(self, state, service_type: str, radius_km: float) -> list[dict]:
        loc = state.get("location_hint")
        if not loc:
            return []
        return await self.d.directory.search(service_type, loc["lat"], loc["lng"],
                                             radius_km=radius_km, k=2)

    async def emergency_exit(self, state: JourneyState):
        places = [] if (state["triage"] or {}).get("mental_health") else \
            await self._nearest(state, "emergency_department", 40)
        return {**say(render.emergency(state["triage"], places)), "outcome": "emergency"}

    async def urgent_exit(self, state: JourneyState):
        places = [] if (state["triage"] or {}).get("mental_health") else \
            await self._nearest(state, "urgent_treatment_centre", 30)
        return {**say(render.urgent(state["triage"], places)), "outcome": "urgent"}

    async def authorise(self, state: JourneyState):
        """Code, not model: confirm the candidate subject against NHS records,
        proxy links and consent grants. The ONLY Path A writer of patient_id."""
        auth = await self.d.authority.decide(state.get("requester_id"), state.get("subject"))
        if auth["status"] == "needs_consent" and auth.get("invite_token"):
            await self._audit(state, actor=state.get("requester_id"), patient=None,
                              action="created a one-time consent link",
                              detail=f"for {auth.get('label')}")
        return {"authority": auth,
                "patient_id": auth.get("patient_id") if auth["status"] == "verified" else None}

    async def info_only(self, state: JourneyState):
        text, outcome = render.info_only(state.get("authority") or {}, self.d.public_base_url)
        return {**say(text), "outcome": outcome}

    def _snapshot(self, patient_id: str) -> dict | None:
        rec = self.d.records.get(patient_id)
        if rec is None:
            return None
        return {k: rec.get(k) for k in ("nhs_number", "name", "first_name", "age", "sex",
                                        "practice_ods", "postcode", "area", "insurance",
                                        "lat", "lng")}

    async def intake(self, state: JourneyState):
        patient = self._snapshot(state["patient_id"])
        loc = state.get("location_hint") or {"lat": patient["lat"], "lng": patient["lng"],
                                              "label": f"{patient['area']} ({patient['postcode']})",
                                              "source": "home"}
        return {"patient": patient, "location": loc}

    async def route_agent(self, state: JourneyState, config: RunnableConfig):
        patient, auth = state["patient"], state["authority"]
        rel = auth.get("relationship")
        context = (f"Patient: {patient['first_name']}, age {patient['age']}, sex {patient['sex']}. "
                   f"Searching near {state['location']['label']}. "
                   + ("The patient is writing about themselves." if rel == "self" else
                      f"Written by someone acting for the patient ({rel})."))
        convo = [m for m in state["messages"] if isinstance(m, (HumanMessage, AIMessage))]
        sub_config = {**(config or {}), "configurable": {
            **((config or {}).get("configurable") or {}),
            "journey": {"patient": patient, "location": state["location"],
                        "authority_type": auth.get("type"), "context": context}}}
        out = await self.d.route_agent.run(convo, sub_config)
        res = out["result"]
        update = {"llm_calls": state.get("llm_calls", 0) + out["llm_calls"],
                  "tool_errors": list(state.get("tool_errors") or []) + out["tool_errors"]}
        if res["type"] == "ask":
            return {**update, **say(res["question"]), "outcome": "question"}
        if res["type"] != "recommend":
            return {**update, "outcome": "no_match", **say(
                "I couldn't match this to a service with confidence. NHS 111 online "
                "(111.nhs.uk) can help you work out where to go, or contact your GP "
                "practice. If it gets worse, call 111, or 999 in an emergency.")}
        route = res["care_route"]
        update.update(care_route=route, route_reason=res.get("reason"),
                      confirmed_providers={p["id"] for p in res["providers"]},
                      options=[self._option(p, route) for p in res["providers"]])
        if route == "self_care":
            return {**update, "outcome": "self_care", **say(
                (res.get("reason") or "This should settle with self-care.")
                + "\n\nIf it isn't getting better, or you're worried, a pharmacist can "
                  "help, or use 111.nhs.uk.")}
        return update

    @staticmethod
    def _option(p: dict, route: str) -> dict:
        kind = "gp" if route == "gp" else "direct"
        mode = (p.get("booking_mode") or "appointment") if kind == "direct" else (
            "appointment" if p.get("mode") == "appointments" else "online_consultation")
        if not p.get("bookable") and kind == "direct":
            mode = "walk_in"
        return {"option_id": p["id"], "provider_id": p["id"], "kind": kind,
                "service_type": p.get("service_type"), "name": p["name"],
                "address": p.get("address"), "postcode": p.get("postcode"),
                "ods": p.get("ods"), "distance_km": p.get("distance_km"),
                "distance_type": p.get("distance_type"), "drive_min": p.get("drive_min"),
                "hours": p.get("hours"), "walk_in": bool(p.get("walk_in")),
                "bookable": bool(p.get("bookable")), "booking_mode": mode,
                "confidential": bool(p.get("confidential")), "pharmacy_first":
                bool(p.get("pharmacy_first")), "patient_pays_gbp": "0.00", "funding": "NHS",
                "why": [], "breaks": []}

    async def direct_access(self, state: JourneyState):
        options, patient, auth = state["options"], state["patient"], state["authority"]
        reason = state.get("route_reason") or ""
        if (auth.get("type") != "self" and any(o["confidential"] for o in options)
                and (patient.get("age") or 0) >= CONFIDENTIAL_AGE):
            return {**say(
                f"{reason}\n\n{patient['first_name']} can contact these services directly, "
                "and they will keep it confidential. Because it's a confidential service, "
                f"I can't book it on {patient['first_name']}'s behalf:\n"
                + render.option_list(options)), "outcome": "confidential"}
        if not any(o["bookable"] for o in options):
            return {**say(f"{reason}\n\nYou can walk in, no appointment needed:\n"
                          + render.option_list(options)), "outcome": "walk_in"}
        return say(f"{reason}\n\nNearest options:\n" + render.option_list(options))

    async def registered_gp(self, state: JourneyState, config: RunnableConfig):
        patient = state["patient"]
        practice = self.d.records.practice(patient.get("practice_ods"))
        if practice is None:
            return {**say("I can't find a registered GP practice on the record. You can "
                          "register with a practice at nhs.uk/register-with-a-gp."),
                    "outcome": "no_gp"}
        episode_id = "ep-" + hashlib.sha256(
            f"{thread_id_of(config)}|{patient['nhs_number']}".encode()).hexdigest()[:12]
        await self.d.store.put("episode", episode_id, {
            "episode_id": episode_id, "patient_id": patient["nhs_number"],
            "requester_id": state.get("requester_id"), "thread_id": thread_id_of(config),
            "practice_ods": practice["ods"], "summary": last_human_text(state)[:1000],
            "created": self.d.clock.now().isoformat()}, ttl_s=90 * DAY)
        item = {**practice, "service_type": "gp_practice",
                "bookable": practice["mode"] == "appointments"}
        opt = self._option(item, "gp")
        reason = state.get("route_reason") or ""
        if opt["booking_mode"] == "appointment":
            text = f"{reason}\n\n{practice['name']} offers appointments online."
        else:
            text = (f"{reason}\n\n{practice['name']} asks patients to use its online "
                    "consultation form first; the practice then decides the right "
                    "appointment. I can send your description to them now.")
        return {**say(text), "selected_option": opt, "options": [opt],
                "episode_id": episode_id}

    # ================================================================ Path B
    async def load_referral(self, state: JourneyState):
        ubrn = (state.get("referral") or {}).get("ubrn")
        ref = await self.d.ers.get_referral(ubrn) if ubrn else None
        if ref is None:
            return {**say("This referral could not be found in the e-Referral Service."),
                    "outcome": "referral_not_found"}
        patient = self._snapshot(ref["nhs_number"])
        if patient is None:
            return {**say("The referral's patient could not be matched to an NHS record."),
                    "outcome": "referral_not_found"}
        episode = await self.d.store.get("episode", ref["episode_id"]) \
            if ref.get("episode_id") else None
        funding = "NHS (you choose where)" if ref["source"] == "nhs_ers" else "private"
        text = (f"{patient['first_name']} has been referred to "
                f"{ref['specialty_label'].lower()} ({funding}). ")
        if episode:
            text += "This follows the request sent to the GP practice earlier. "
        text += "Tell me what matters: for example \u201cunder 6 weeks, within 20 km\u201d."
        return {**say(text), "referral": ref, "patient_id": ref["nhs_number"],
                "patient": patient, "episode_id": ref.get("episode_id"),
                "location": {"lat": patient["lat"], "lng": patient["lng"],
                             "label": f"{patient['area']} ({patient['postcode']})"}}

    async def ask_preferences(self, state: JourneyState):
        ref = state["referral"]
        answer = interrupt({
            "type": "preferences", "referral": {
                "ubrn": ref["ubrn"], "specialty": ref["specialty_label"],
                "priority": ref["priority"], "source": ref["source"],
                "for": state["patient"]["first_name"]},
            "examples": ["under 6 weeks, within 20 km",
                         "nearest, NHS only" if ref["source"] == "nhs_ers"
                         else "under £150, within 15 miles",
                         "soonest, within 30 km"]})
        answer = answer or {}
        if answer.get("preferences"):
            return {"preferences": answer["preferences"], "pref_text": None}
        return {"pref_text": str(answer.get("text") or "")[:500],
                "messages": [HumanMessage(content=str(answer.get("text") or ""))]}

    async def extract_preferences(self, state: JourneyState):
        text = state.get("pref_text") or (last_human_text(state)
                                         if state.get("preferences") else "")
        old = dict(state.get("preferences") or {})
        update = {"pref_text": None}
        if text:
            new = await self.d.steps.extract_preferences(text)
            update["llm_calls"] = state.get("llm_calls", 0) + 1
            merged = {k: (new.get(k) if new.get(k) is not None else old.get(k))
                      for k in ("max_wait_weeks", "max_km", "max_cost_gbp", "rank_by")}
        else:
            merged = {k: old.get(k) for k in ("max_wait_weeks", "max_km", "max_cost_gbp",
                                              "rank_by")}
        merged["rank_by"] = merged.get("rank_by") or "wait"
        if (state.get("referral") or {}).get("priority") in ("urgent", "2ww"):
            merged["rank_by"] = "wait"            # clinical priority beats preference
        update["preferences"] = merged
        progress("Understood your limits: " + self._limits_text(merged))
        return update

    @staticmethod
    def _limits_text(p: dict) -> str:
        bits = []
        if p.get("max_wait_weeks") is not None:
            bits.append(f"wait up to {p['max_wait_weeks']:g} weeks")
        if p.get("max_km") is not None:
            bits.append(f"within {p['max_km']:g} km")
        if p.get("max_cost_gbp") is not None:
            bits.append("NHS only" if Decimal(p["max_cost_gbp"]) == 0
                        else f"pay up to {render.money(p['max_cost_gbp'])}")
        bits.append(f"ranked by {p.get('rank_by') or 'wait'}")
        return ", ".join(bits)

    async def _candidates(self, state: JourneyState) -> list[dict]:
        ref, patient = state["referral"], state["patient"]
        out = []
        if ref["source"] == "nhs_ers":
            for s in self.d.ers.services(ref["specialty"]):
                out.append({"option_id": s["id"], "provider_id": s["id"], "kind": "ers",
                            "service_id": s["service_id"], "name": s["name"],
                            "address": None, "postcode": s["postcode"], "lat": s["lat"],
                            "lng": s["lng"], "wait_weeks": s["wait_weeks"],
                            "funding": "NHS", "patient_pays_gbp": "0.00",
                            "independent_sector": s["independent_sector"]})
        else:
            providers = [p for p in self.d.private_providers
                         if p["specialty"] == ref["specialty"]]
            quotes = await asyncio.gather(*[
                self.d.insurer.quote(patient.get("insurance"), patient_id=patient["nhs_number"],
                                     provider=p) for p in providers])
            progress("Checked insurance cover with the insurer")
            for p, q in zip(providers, quotes):
                out.append({"option_id": p["id"], "provider_id": p["id"], "kind": "private",
                            "name": p["name"], "address": None, "postcode": p["postcode"],
                            "lat": p["lat"], "lng": p["lng"], "wait_weeks": p["wait_weeks"],
                            "funding": q["funding"], "patient_pays_gbp": q["patient_pays_gbp"],
                            "price_gbp": q["price_gbp"], "insurer_note": q.get("note"),
                            "needs_preauth": q.get("needs_preauth", False),
                            "insurer_name": q.get("insurer_name")})
        loc = state["location"]
        return await self.d.distances.annotate(loc["lat"], loc["lng"], out)

    @staticmethod
    def _breaks(o: dict, p: dict) -> list[dict]:
        out = []
        if p.get("max_wait_weeks") is not None and o["wait_weeks"] > p["max_wait_weeks"]:
            out.append({"limit": "wait", "text": f"wait is {o['wait_weeks']} weeks "
                        f"(your limit {p['max_wait_weeks']:g})"})
        if p.get("max_km") is not None and o["distance_km"] > p["max_km"]:
            out.append({"limit": "distance", "text": f"{o['distance_km']:g} km away "
                        f"(your limit {p['max_km']:g})"})
        if p.get("max_cost_gbp") is not None and \
                money(o["patient_pays_gbp"]) > money(p["max_cost_gbp"]):
            out.append({"limit": "cost", "text": f"you'd pay {render.money(o['patient_pays_gbp'])} "
                        f"(your limit {render.money(p['max_cost_gbp'])})"})
        return out

    @staticmethod
    def _rank_key(rank_by: str):
        if rank_by == "distance":
            return lambda o: (o["distance_km"], o["wait_weeks"])
        if rank_by == "cost":
            return lambda o: (money(o["patient_pays_gbp"]), o["wait_weeks"], o["distance_km"])
        return lambda o: (o["wait_weeks"], o["distance_km"])

    async def shortlist(self, state: JourneyState):
        """Code, not model: filter on the patient's limits, widen the search
        WITHIN those limits, rank, and explain each option."""
        prefs = state["preferences"]
        cands = await self._candidates(state)
        if not cands:
            return {**say("No services are listed for this referral yet. Your GP practice "
                          "or the e-RS helpline can help you book."), "outcome": "no_options"}
        limit_km = prefs.get("max_km") or 80
        radius = min(10.0, limit_km)
        while True:
            fits = [c for c in cands if c["distance_km"] <= radius and not self._breaks(c, prefs)]
            if len(fits) >= 3 or radius >= limit_km:
                break
            radius = min(radius * 2, limit_km)
            progress(f"Widened the search to {radius:g} km (within your limit)")
        key = self._rank_key(prefs.get("rank_by") or "wait")
        for c in cands:
            c["breaks"] = self._breaks(c, prefs)
            c["why"] = [b for b in (
                render.weeks(c["wait_weeks"]), render.distance(c),
                "free on the NHS" if c["funding"] == "NHS" else
                f"you pay {render.money(c['patient_pays_gbp'])} ({c['funding']})",
                "independent sector, NHS-funded" if c.get("independent_sector") else "",
                c.get("insurer_note") or "") if b]
            c.update(booking_mode="appointment", bookable=True, walk_in=False, confidential=False)
        if fits:
            fits.sort(key=key)
            fits[0]["why"].insert(0, "Best match for what you said matters most")
            text = (f"These meet your limits ({self._limits_text(prefs)}):\n"
                    + render.option_list(fits[:5]))
            return {**say(text), "options": fits[:5], "excluded_options": []}
        near = sorted(cands, key=lambda c: (len(c["breaks"]), key(c)))[:3]
        text = ("Nothing meets all your limits. These come closest; each shows which limit "
                "it bends, and you decide:\n" + "\n".join(
                    render.option_line(i, c) + "  [" + "; ".join(b["text"] for b in c["breaks"])
                    + "]" for i, c in enumerate(near, 1)))
        progress("Nothing fits every limit: showing the closest options")
        return {**say(text), "options": near, "excluded_options": []}

    # ======================================================= shared booking
    def _available_options(self, state) -> list[dict]:
        excluded = set(state.get("excluded_options") or [])
        return [o for o in state.get("options") or [] if o["option_id"] not in excluded]

    async def choose_option(self, state: JourneyState):
        options = self._available_options(state)
        answer = interrupt({"type": "choose_option", "options": options,
                            "preferences": state.get("preferences"),
                            "near_miss": any(o.get("breaks") for o in options)}) or {}
        if answer.get("decline"):
            return {**say("OK, nothing has been booked."), "outcome": "declined",
                    "selected_option": None}
        chosen = next((o for o in options if o["option_id"] == answer.get("option_id")), None)
        if chosen is None:
            return {**say("That option isn't available any more. Nothing has been booked."),
                    "outcome": "declined", "selected_option": None}
        if chosen["booking_mode"] == "walk_in":
            return {**say(f"You can walk in to {chosen['name']}"
                          + (f", {chosen['address']}" if chosen.get("address") else "")
                          + (f" ({chosen['hours']})" if chosen.get("hours") else "")
                          + ". No appointment needed."),
                    "outcome": "walk_in", "selected_option": None}
        return {"selected_option": chosen}

    async def check_slots(self, state: JourneyState):
        opt = state["selected_option"]
        mode = opt["booking_mode"]
        if mode in ("self_referral", "online_consultation"):
            return {"available_slots": []}
        if opt["kind"] == "gp":
            slots = await self.d.gp.slots(opt["ods"])
        elif opt["kind"] == "ers":
            slots = await self.d.ers.slots(state["referral"]["ubrn"], opt["service_id"])
        elif opt["kind"] == "private":
            slots = await self.d.booking.slots(opt["provider_id"], "private", opt["wait_weeks"])
        else:
            slots = await self.d.booking.slots(opt["provider_id"], opt["service_type"])
        slots = slots[:6]
        progress(f"Found {len(slots)} times at {opt['name']}")
        if slots:
            return {"available_slots": slots}
        excluded = list(state.get("excluded_options") or []) + [opt["option_id"]]
        remaining = [o for o in state.get("options") or [] if o["option_id"] not in excluded]
        if opt["kind"] == "gp" or not remaining:
            return {**say(f"{opt['name']} has no times available to book online right now. "
                          "Please contact them directly."), "outcome": "no_slots",
                    "selected_option": None, "excluded_options": excluded}
        return {**say(f"{opt['name']} has no times available right now. Please choose "
                      "another option."), "selected_option": None, "excluded_options": excluded}

    async def confirm_booking(self, state: JourneyState, config: RunnableConfig):
        opt, patient, auth = state["selected_option"], state["patient"], state["authority"] or {}
        action = {"online_consultation": "send_request", "self_referral": "self_refer"}.get(
            opt["booking_mode"], "book")
        answer = interrupt({
            "type": "confirm_booking", "action": action, "option": opt,
            "slots": state.get("available_slots") or [],
            "for": {"first_name": patient["first_name"],
                    "relationship": auth.get("relationship") or "self",
                    "authority": auth.get("source")},
            "patient_pays_gbp": opt.get("patient_pays_gbp", "0.00"),
            "funding": opt.get("funding", "NHS"),
            "shares": ("Your description of the problem, name, date of birth and NHS number"
                       if action == "send_request" else
                       "Name, date of birth, NHS number and contact number")}) or {}
        if not answer.get("consent"):
            return {**say("Cancelled. Nothing has been booked or paid."), "outcome": "cancelled"}
        slot = None
        if action == "book":
            slot = next((s for s in state.get("available_slots") or []
                         if s["slot_id"] == answer.get("slot_id")), None)
            if slot is None:
                attempts = state.get("booking_attempts", 0) + 1
                if attempts >= MAX_BOOKING_ATTEMPTS:
                    return {**say("Nothing has been booked. Please try again later."),
                            "outcome": "cancelled"}
                return {**say("Please pick one of the available times."), "consent": False,
                        "booking_attempts": attempts}
        key = hashlib.sha256("|".join([thread_id_of(config), patient["nhs_number"],
                                       opt["option_id"], slot["slot_id"] if slot else action])
                             .encode()).hexdigest()[:32]
        return {"consent": True, "selected_slot": slot, "idempotency_key": key}

    async def book(self, state: JourneyState, config: RunnableConfig):
        opt, patient, key = state["selected_option"], state["patient"], state["idempotency_key"]
        slot = state.get("selected_slot")
        auth = None
        if state.get("requester_id"):
            auth = await self.d.authority.can_act_for(state["requester_id"], patient["nhs_number"])
        if auth is None:
            return {**say("I can no longer act for this person (permission expired or was "
                          "withdrawn). Nothing has been booked."), "outcome": "not_authorised"}
        extra = {}
        try:
            if opt["kind"] == "gp" and opt["booking_mode"] == "online_consultation":
                summary = " ".join(text_of(m) for m in state["messages"]
                                   if isinstance(m, HumanMessage))[-2000:]
                res = await self.d.gp.submit_online_consultation(opt["ods"], patient["nhs_number"],
                                                                 summary, key)
            elif opt["kind"] == "gp":
                res = await self.d.gp.book(opt["ods"], slot["slot_id"], patient["nhs_number"], key)
            elif opt["kind"] == "ers":
                res = await self.d.ers.book(state["referral"]["ubrn"], opt["service_id"],
                                            slot["slot_id"], key)
            elif opt["kind"] == "private":
                if opt.get("needs_preauth"):
                    pa = await self.d.insurer.preauthorise(patient["insurance"],
                                                           opt["provider_id"], key)
                    extra["preauth_ref"] = pa["preauth_ref"]
                res = await self.d.booking.book(opt["provider_id"], slot["slot_id"], key,
                                                specialty=state["referral"]["specialty"])
                pay = await self.d.payments.charge(opt["patient_pays_gbp"], key,
                                                   f"Consultation, {opt['name']}")
                if pay:
                    extra.update(payment_ref=pay["payment_ref"], charged_gbp=pay["amount_gbp"])
            elif opt["booking_mode"] == "self_referral":
                res = await self.d.booking.self_refer(opt["provider_id"], patient["nhs_number"], key)
            else:
                res = await self.d.booking.book(opt["provider_id"], slot["slot_id"], key,
                                                service_type=opt["service_type"])
        except SlotTaken:
            attempts = state.get("booking_attempts", 0) + 1
            if attempts >= MAX_BOOKING_ATTEMPTS:
                return {**say("Those times keep being taken. Nothing has been booked; please "
                              "try again shortly."), "outcome": "slot_taken",
                        "booking_attempts": attempts}
            progress("That time was just taken: fetching fresh times")
            return {**say("That time was just taken by someone else. Here are the latest "
                          "times."), "booking_attempts": attempts, "selected_slot": None,
                    "consent": False}
        except BookingFailed:
            return {**say("I couldn't confirm the booking because the provider's system didn't "
                          "respond. Nothing has been charged. It's safe to try again: the "
                          "same booking key is used, so you can't be booked twice."),
                    "outcome": "booking_error"}
        acting = state["requester_id"] != patient["nhs_number"]
        booking = {**res, **extra, "kind": opt["kind"], "booking_mode": opt["booking_mode"],
                   "provider_id": opt["provider_id"], "provider_name": opt["name"],
                   "address": opt.get("address") or opt.get("postcode"),
                   "patient_id": patient["nhs_number"], "for_first_name": patient["first_name"],
                   "for_name": patient["first_name"] if acting else None,
                   "booked_by": state["requester_id"], "funding": opt.get("funding"),
                   "patient_pays_gbp": opt.get("patient_pays_gbp"),
                   "confidential": opt.get("confidential", False),
                   "thread_id": thread_id_of(config), "episode_id": state.get("episode_id")}
        await self.d.store.append("booking", patient["nhs_number"], booking, ttl_s=None)
        what = {"online_consultation": "sent a GP online consultation",
                "self_referral": "made a self-referral"}.get(opt["booking_mode"],
                                                              "booked an appointment")
        await self._audit(state, actor=state["requester_id"], patient=patient["nhs_number"],
                          action=what, detail=f"{opt['name']} (ref {booking['booking_ref']})",
                          confidential=booking["confidential"], authority=auth["source"])
        if auth["type"] == "consent" and auth.get("grant_token"):
            await self.d.consents.consume(auth["grant_token"])
        if state.get("episode_id") and opt["kind"] == "gp":
            ep = await self.d.store.get("episode", state["episode_id"])
            if ep:
                ep["gp_request_ref"] = booking["booking_ref"]
                await self.d.store.put("episode", state["episode_id"], ep, ttl_s=90 * DAY)
        progress("Booked")
        return {"booking": booking}

    async def notify(self, state: JourneyState):
        b = state["booking"]
        text, fields = render.sms(b)
        recipients = []
        requester = self.d.records.get(state["requester_id"])
        if requester and requester.get("phone"):
            recipients.append(requester["phone"])
        patient = self.d.records.get(b["patient_id"])
        if (patient and patient.get("phone") and patient["nhs_number"] != state["requester_id"]
                and patient["age"] >= 16):
            recipients.append(patient["phone"])      # people are told when someone acts for them
        statuses = []
        for i, phone in enumerate(dict.fromkeys(recipients)):
            try:
                statuses.append(await asyncio.wait_for(
                    self.d.notifier.send_sms(phone, text, fields,
                                             f"{state['idempotency_key']}-{i}"), timeout=5))
            except Exception as e:                     # a text never undoes a booking
                print(f"[notify] failed: {type(e).__name__}")
                statuses.append("failed")
        status = "sent" if statuses and all(s in ("sent", "already_sent") for s in statuses) \
            else ("failed" if statuses else "no_phone")
        msg = render.booked(b)
        msg += ("\n\nA confirmation text is on its way (it has no health details in it)."
                if status == "sent" else "")
        return {**say(msg), "sms_status": status, "outcome": "booked"}

    async def _audit(self, state, *, actor, patient, action, detail="", confidential=False,
                     authority=None):
        entry = {"ts": self.d.clock.now().isoformat(), "actor": actor, "patient": patient,
                 "action": action, "detail": detail, "confidential": confidential,
                 "authority": authority}
        for who in {p for p in (actor, patient) if p}:
            await self.d.store.append("audit", who, entry, max_len=1000)
