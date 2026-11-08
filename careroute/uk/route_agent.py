"""
uk/route_agent.py — the ONE agent: find the right front door.

    agent --(tool calls)--> tools --(recommend accepted / ask_patient)--> END
      ^                       |
      +-----------------------+   (searches, or a recommend the guard rejected)

What changed from the regex guard in careroute/agent/guard.py: the agent no
longer writes provider lists. It finishes by calling
    recommend(care_route, provider_ids, reason_for_patient)
and the RecommendationGuard checks, in code, that every id was returned by a
search in this conversation and fits the route (a pharmacy id can't be sent
to physio; Pharmacy First needs an eligibility check that passed). Rejected
recommendations go back to the model as a tool error it can fix. Names,
addresses and drive times are then rendered from the search records, so an
invented provider is impossible rather than merely blocked.

Patient context (age, sex, location, registered practice) reaches the tools
through config, never through model arguments: the model can't search "as"
someone else or move them.

Tool-call history stays PRIVATE to this subgraph; the parent graph receives
only the result (route, provider records, reason).
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from careroute.agent.messages import payload_of
from careroute.uk import pharmacy_first
from careroute.uk.directory import UkServiceDirectory
from careroute.uk.geo import UkGeocoder
from careroute.uk.records import NhsRecords

CARE_ROUTES = ("pharmacy_first", "pharmacy", "sexual_health", "talking_therapies",
               "msk_physio", "gp", "self_care")
ROUTE_SERVICE_TYPE = {"pharmacy_first": "pharmacy", "pharmacy": "pharmacy",
                      "sexual_health": "sexual_health", "talking_therapies": "talking_therapies",
                      "msk_physio": "msk_physio", "gp": "gp_practice", "self_care": None}
SEARCH_TOOLS = ("find_pharmacy_first", "find_pharmacy", "find_self_referral_service",
                "get_registered_gp")
TERMINAL_TOOLS = ("recommend", "ask_patient")

ROUTE_PROMPT = """You are the care-navigation step of UK CareRoute, an NHS England
care navigator. Triage has already run: this is NOT an emergency. Your job is to
choose the right FRONT DOOR for the patient and the services to offer them.

Front doors (care_route), most patients do NOT need a GP:
- pharmacy_first: NHS Pharmacy First. A pharmacist can assess and, if needed,
  supply treatment for seven conditions: acute_sore_throat, acute_sinusitis,
  acute_otitis_media (earache), infected_insect_bite, impetigo, shingles,
  uncomplicated_uti. Call find_pharmacy_first with the condition. Eligibility
  (age, sex) is checked by the tool; if it says eligible=false, do not use
  Pharmacy First.
- pharmacy: advice and over-the-counter treatment for minor illness (colds,
  coughs, hay fever, mild aches, diarrhoea). Call find_pharmacy.
- sexual_health: STI testing, contraception, sexual health concerns. Confidential.
- talking_therapies: NHS Talking Therapies for anxiety, low mood, depression,
  stress (self-referral).
- msk_physio: back, neck, knee, shoulder or joint pain and sprains with no
  red flags (self-referral physiotherapy).
- gp: anything else needing a clinician, problems lasting weeks, repeat or
  worsening symptoms, anything you are unsure about. Call get_registered_gp.
- self_care: very minor problems that will settle with self-care and need no
  service at all.

Rules:
1. Search before you recommend. Recommend ONLY provider ids returned by a search
   in this conversation. Never invent ids.
2. Finish by calling recommend(care_route, provider_ids, reason_for_patient).
   For gp and self_care, provider_ids may be empty. reason_for_patient is one or
   two plain sentences to the patient about WHY this front door. Do not name
   providers, addresses, times or medicines in it: those are shown from records.
3. If you genuinely need one more fact to choose (e.g. how long symptoms have
   lasted), call ask_patient with ONE short question instead.
4. You route; you do not diagnose or prescribe.
5. If the patient names a different place, pass near_place to the search tool.
"""


class RouteState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    seen: dict                 # provider id -> record, from successful searches
    eligibility: dict          # pharmacy first condition -> eligibility result
    llm_calls: int
    tool_errors: list
    result: dict | None        # set when the agent is done
    nudged: bool


def _ctx(config: RunnableConfig | None) -> dict:
    return ((config or {}).get("configurable") or {}).get("journey") or {}


def _progress(text: str) -> None:
    try:
        get_stream_writer()({"progress": text})
    except Exception:
        pass                                   # not streaming: nothing to tell


class RouteToolkit:
    def __init__(self, directory: UkServiceDirectory, records: NhsRecords, geocoder: UkGeocoder):
        self.directory = directory
        self.records = records
        self.geocoder = geocoder
        self.tools = self._build()

    async def _where(self, ctx: dict, near_place: str | None) -> tuple[dict | None, str | None]:
        if near_place:
            g = await self.geocoder.geocode(near_place)
            if g is None:
                return None, f"Could not find '{near_place}'. Ask the patient for a postcode."
            return g, None
        loc = ctx.get("location")
        if not loc:
            return None, "The patient's location is unknown. Ask for their postcode."
        return loc, None

    def _build(self):
        directory, records = self.directory, self.records

        @tool
        async def find_pharmacy_first(
            condition: Annotated[Literal[tuple(pharmacy_first.PATHWAYS)],
                                 "The Pharmacy First condition you think this is"],
            config: RunnableConfig,
            radius_km: Annotated[float, "Search radius in km (default 5, max 20)"] = 5,
            near_place: Annotated[Optional[str], "A place or postcode the patient named"] = None,
        ) -> dict:
            """Check Pharmacy First eligibility for this patient and condition, and find
            the nearest pharmacies offering Pharmacy First. Returns eligible=false with a
            reason when the patient's age or sex is outside the pathway; then choose
            another route (usually gp)."""
            ctx = _ctx(config)
            p = ctx.get("patient") or {}
            elig = pharmacy_first.eligibility(condition, age=p.get("age"), sex=p.get("sex"))
            if not elig["eligible"]:
                _progress(f"Pharmacy First: not eligible ({elig['reason']})")
                return {**elig, "results": []}
            where, err = await self._where(ctx, near_place)
            if err:
                return {**elig, "error": err, "error_type": "location"}
            radius = max(1.0, min(float(radius_km), 20.0))
            res = await directory.search("pharmacy", where["lat"], where["lng"],
                                         radius_km=radius, k=4, pharmacy_first=True)
            _progress(f"Found {len(res)} Pharmacy First pharmacies within {radius:g} km")
            return {**elig, "searched_near": where.get("label"), "radius_km": radius,
                    "results": res,
                    "hint": None if res else "None in range: retry with a larger radius_km."}

        @tool
        async def find_pharmacy(
            config: RunnableConfig,
            radius_km: Annotated[float, "Search radius in km (default 5, max 20)"] = 5,
            near_place: Annotated[Optional[str], "A place or postcode the patient named"] = None,
        ) -> dict:
            """Nearest community pharmacies, for advice and over-the-counter treatment of
            minor illness."""
            ctx = _ctx(config)
            where, err = await self._where(ctx, near_place)
            if err:
                return {"error": err, "error_type": "location"}
            radius = max(1.0, min(float(radius_km), 20.0))
            res = await directory.search("pharmacy", where["lat"], where["lng"],
                                         radius_km=radius, k=4)
            _progress(f"Found {len(res)} pharmacies within {radius:g} km")
            return {"searched_near": where.get("label"), "radius_km": radius, "results": res}

        @tool
        async def find_self_referral_service(
            service: Annotated[Literal["sexual_health", "talking_therapies", "msk_physio"],
                               "Which self-referral service"],
            config: RunnableConfig,
            radius_km: Annotated[float, "Search radius in km (default 8, max 30)"] = 8,
            near_place: Annotated[Optional[str], "A place or postcode the patient named"] = None,
        ) -> dict:
            """Nearest NHS services patients can refer themselves to: sexual health
            clinics, NHS Talking Therapies, community MSK physiotherapy. Services with a
            minimum age above the patient's are excluded."""
            ctx = _ctx(config)
            where, err = await self._where(ctx, near_place)
            if err:
                return {"error": err, "error_type": "location"}
            radius = max(1.0, min(float(radius_km), 30.0))
            age = (ctx.get("patient") or {}).get("age")
            res = await directory.search(service, where["lat"], where["lng"], radius_km=radius,
                                         k=3, age=age)
            _progress(f"Found {len(res)} {service.replace('_', ' ')} services within {radius:g} km")
            return {"service": service, "searched_near": where.get("label"),
                    "radius_km": radius, "results": res,
                    "hint": None if res else "None in range: retry with a larger radius_km."}

        @tool
        async def get_registered_gp(config: RunnableConfig) -> dict:
            """The patient's registered GP practice and how it takes requests
            (bookable appointments, or an online consultation form)."""
            p = (_ctx(config).get("patient") or {})
            practice = records.practice(p.get("practice_ods"))
            if practice is None:
                return {"error": "No registered GP practice on record.", "results": []}
            item = {"id": practice["id"], "ods": practice["ods"],
                    "service_type": "gp_practice", "name": practice["name"],
                    "address": practice["address"], "postcode": practice["postcode"],
                    "lat": practice["lat"], "lng": practice["lng"], "mode": practice["mode"],
                    "bookable": practice["mode"] == "appointments", "walk_in": False,
                    "confidential": False}
            _progress(f"Checked {practice['name']}")
            return {"results": [item]}

        @tool
        async def recommend(
            care_route: Annotated[Literal[CARE_ROUTES], "The front door"],
            provider_ids: Annotated[list[str], "Ids from search results, best first (max 3)"],
            reason_for_patient: Annotated[str, "One or two sentences: why this front door"],
            pharmacy_first_condition: Annotated[Optional[str],
                                                "Required when care_route is pharmacy_first"] = None,
        ) -> dict:
            """Finish: recommend a front door and the services to offer. Checked in code;
            if rejected, the result says why so you can fix it."""
            return {"submitted": True}               # validated by the graph

        @tool
        async def ask_patient(
            question: Annotated[str, "One short question for the patient"],
        ) -> dict:
            """Finish this turn by asking the patient one question you need answered
            to choose the front door."""
            return {"asked": True}

        tools = [find_pharmacy_first, find_pharmacy, find_self_referral_service,
                 get_registered_gp, recommend, ask_patient]
        for t in tools:
            t.description = inspect.cleandoc(t.description)
        return tools


class RecommendationGuard:
    """Structural check of recommend(...). Returns (result, error)."""
    MAX_REASON = 400

    def check(self, args: dict, seen: dict, eligibility: dict,
              registered_gp: str | None) -> tuple[dict | None, str | None]:
        route = args.get("care_route")
        if route not in CARE_ROUTES:
            return None, f"Unknown care_route {route!r}. Use one of {', '.join(CARE_ROUTES)}."
        reason = " ".join(str(args.get("reason_for_patient") or "").split())[: self.MAX_REASON]
        ids = list(dict.fromkeys(args.get("provider_ids") or []))[:3]
        if route == "self_care":
            return {"care_route": route, "providers": [], "reason": reason}, None
        if route == "gp":
            gp = seen.get(registered_gp) if registered_gp else None
            if gp is None:
                return None, "Call get_registered_gp before recommending gp."
            return {"care_route": route, "providers": [gp], "reason": reason}, None
        if not ids:
            return None, f"{route} needs at least one provider id from a search result."
        unknown = [i for i in ids if i not in seen]
        if unknown:
            return None, (f"These ids were not returned by any search in this conversation: "
                          f"{', '.join(unknown)}. Recommend only ids from search results.")
        want = ROUTE_SERVICE_TYPE[route]
        wrong = [i for i in ids if seen[i].get("service_type") != want]
        if wrong:
            return None, f"These ids are not {route} services: {', '.join(wrong)}."
        if route == "pharmacy_first":
            cond = args.get("pharmacy_first_condition")
            if not (eligibility.get(cond) or {}).get("eligible"):
                return None, ("pharmacy_first needs a pharmacy_first_condition whose "
                              "find_pharmacy_first check returned eligible=true.")
            if not all(seen[i].get("pharmacy_first") for i in ids):
                return None, "Only pharmacies that offer Pharmacy First can be recommended."
        return {"care_route": route, "providers": [seen[i] for i in ids], "reason": reason,
                "pharmacy_first_condition": args.get("pharmacy_first_condition")}, None


class RouteAgent:
    NUDGE = ("Finish by calling recommend(...) with ids from your search results, "
             "or ask_patient(...) if you need one more fact.")

    def __init__(self, model, toolkit: RouteToolkit, *, max_llm_calls: int = 6,
                 guard: RecommendationGuard | None = None):
        self.model = model                       # .ainvoke(messages) -> AIMessage
        self.toolkit = toolkit
        self.max_llm_calls = max_llm_calls
        self.guard = guard or RecommendationGuard()
        self._tool_node = ToolNode([t for t in toolkit.tools if t.name in SEARCH_TOOLS])
        # checkpointer=False: this subgraph never checkpoints into the parent's
        # thread. Its tool-call history is private and lives only for the run.
        self.graph = self._build().compile(checkpointer=False)

    def _build(self):
        b = StateGraph(RouteState)
        b.add_node("agent", self._agent)
        b.add_node("tools", self._tools)
        b.add_edge(START, "agent")
        b.add_conditional_edges("agent", self._after_agent,
                                {"tools": "tools", "agent": "agent", "end": END})
        b.add_conditional_edges("tools", lambda s: "end" if s.get("result") else "agent",
                                {"agent": "agent", "end": END})
        return b

    async def _agent(self, state: RouteState, config: RunnableConfig):
        n = state.get("llm_calls", 0)
        if n >= self.max_llm_calls:
            return {"result": {"type": "no_match", "reason": "step budget spent"}}
        # Static prompt first, per-turn context last: a stable prefix lets the
        # provider's prompt caching cut latency and cost on every call.
        context = _ctx(config).get("context") or ""
        msgs = [SystemMessage(content=ROUTE_PROMPT + ("\n" + context if context else "")),
                *state["messages"]]
        response = await self.model.ainvoke(msgs)
        update = {"messages": [response], "llm_calls": n + 1}
        if not response.tool_calls:
            if state.get("nudged"):
                update["result"] = {"type": "no_match", "reason": "no recommendation"}
            else:
                update["messages"].append(HumanMessage(content=self.NUDGE))
                update["nudged"] = True
        return update

    @staticmethod
    def _after_agent(state: RouteState) -> str:
        if state.get("result"):
            return "end"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "agent"

    async def _tools(self, state: RouteState, config: RunnableConfig):
        last: AIMessage = state["messages"][-1]
        search_calls = [c for c in last.tool_calls if c["name"] in SEARCH_TOOLS]
        other_calls = [c for c in last.tool_calls if c["name"] not in SEARCH_TOOLS]
        seen = dict(state.get("seen") or {})
        eligibility = dict(state.get("eligibility") or {})
        errors = list(state.get("tool_errors") or [])
        out_msgs: list = []
        if search_calls:
            msg = last.model_copy(update={"tool_calls": search_calls})
            res = await self._tool_node.ainvoke({"messages": [msg]}, config)
            for m in res["messages"]:
                payload = payload_of(m)
                if isinstance(payload, dict):
                    if "error" in payload:
                        errors.append(f"{m.name}: {payload['error']}")
                    if m.name == "find_pharmacy_first" and payload.get("condition"):
                        eligibility[payload["condition"]] = {
                            "eligible": payload.get("eligible"), "reason": payload.get("reason")}
                    for item in payload.get("results") or []:
                        if isinstance(item, dict) and item.get("id"):
                            seen[item["id"]] = item
                out_msgs.append(m)
        result = None
        ctx = _ctx(config)
        gp_id = "gp:" + ((ctx.get("patient") or {}).get("practice_ods") or "")
        for c in other_calls:
            if result is not None:
                out_msgs.append(ToolMessage(content='{"ignored": true}', tool_call_id=c["id"],
                                            name=c["name"]))
                continue
            if c["name"] == "ask_patient":
                q = " ".join(str(c["args"].get("question") or "").split())[:300]
                result = {"type": "ask", "question": q or "Could you tell me a bit more?"}
                out_msgs.append(ToolMessage(content='{"asked": true}', tool_call_id=c["id"],
                                            name=c["name"]))
            elif c["name"] == "recommend":
                ok, err = self.guard.check(c["args"], seen, eligibility, gp_id)
                if err:
                    errors.append(f"recommend rejected: {err}")
                    _progress("Recommendation checked: rejected, asking the agent to fix it")
                    out_msgs.append(ToolMessage(
                        content=json.dumps({"error": err, "accepted": False}),
                        tool_call_id=c["id"], name=c["name"]))
                else:
                    result = {"type": "recommend", **ok}
                    out_msgs.append(ToolMessage(content='{"accepted": true}', tool_call_id=c["id"],
                                                name=c["name"]))
            else:
                out_msgs.append(ToolMessage(content=json.dumps({"error": f"unknown tool {c['name']}"}),
                                            tool_call_id=c["id"], name=c["name"]))
        return {"messages": out_msgs, "seen": seen, "eligibility": eligibility,
                "tool_errors": errors, "result": result}

    async def run(self, conversation: list, config: RunnableConfig) -> dict:
        """conversation: the patient's messages (and earlier answers). Returns
        {result, llm_calls, tool_errors, seen_ids}. Progress events from the
        tools are forwarded to the caller's stream (a nested graph's custom
        events don't reach the parent's stream on their own)."""
        try:
            forward = get_stream_writer()
        except Exception:
            forward = None
        final: dict = {}
        async for mode, chunk in self.graph.astream(
                {"messages": list(conversation), "seen": {}, "eligibility": {}, "llm_calls": 0,
                 "tool_errors": [], "result": None, "nudged": False}, config,
                stream_mode=["custom", "values"]):
            if mode == "values":
                final = chunk
            elif forward is not None:
                forward(chunk)
        result = final.get("result") or {"type": "no_match", "reason": "no result"}
        return {"result": result, "llm_calls": final.get("llm_calls", 0),
                "tool_errors": final.get("tool_errors", []),
                "seen_ids": sorted((final.get("seen") or {}).keys())}
