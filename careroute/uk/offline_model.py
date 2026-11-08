"""
uk/offline_model.py — a deterministic stand-in for the route agent's model.

Duck-types a tool-bound chat model (.ainvoke(messages) -> AIMessage). It
reads the transcript and calls the route agent's tools the way a careful
model would: search first, widen once on an empty result, fall back to the
registered GP when Pharmacy First says not eligible, and finish with
recommend(...) using ids from the search results.

Used by CAREROUTE_LLM=offline (a demo that works with no API key and no
network) and by the graph tests. Keyword routing is deliberately simple; the
real model is evaluated against a golden set, not against this.
"""

from __future__ import annotations

import json
import re
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

PF_CONDITIONS = [
    (r"\buti\b|cystitis|water infection|burn\w* when (?:i |she |they )?(?:pee|wee)|"
     r"pain\w* (?:when )?(?:peeing|weeing|urinating)", "uncomplicated_uti"),
    (r"sore throat|tonsil", "acute_sore_throat"),
    (r"sinus", "acute_sinusitis"),
    (r"ear ?ache|ear infection|ear pain|pain in (?:his|her|my|their) ear", "acute_otitis_media"),
    (r"insect bite|bitten|bite.{0,25}(?:swollen|infected|red|hot)", "infected_insect_bite"),
    (r"impetigo|crusty sores|yellow crust", "impetigo"),
    (r"shingles", "shingles"),
]
SELF_REFERRAL = [
    (r"\bstis?\b|\bstds?\b|chlamydia|gonorr|sexual health|contracepti|morning.after|"
     r"\bcoil\b|unprotected sex|discharge", "sexual_health"),
    (r"anxi|depress|low mood|panic|stressed|can'?t cope|worry(?:ing)? all the time",
     "talking_therapies"),
    (r"back pain|bad back|\bneck\b|shoulder|\bknee|ankle|sprain|joint|hamstring|tendon|"
     r"physio|\bhip\b", "msk_physio"),
]
PHARMACY = r"\bcold\b|cough|hay ?fever|runny nose|blocked nose|diarrh|constipat|heartburn|" \
           r"indigestion|verruca|cold sore|head lice|thrush"

REASONS = {
    "pharmacy_first": "A pharmacist can assess this through NHS Pharmacy First and, if "
                      "you need it, give you treatment without a GP appointment.",
    "pharmacy": "A pharmacist can advise on this and suggest treatment you can buy, "
                "with no appointment needed.",
    "sexual_health": "Sexual health clinics see people directly and confidentially; "
                     "you don't need a GP referral.",
    "talking_therapies": "You can refer yourself to NHS Talking Therapies for this; "
                         "you don't need to see a GP first.",
    "msk_physio": "You can refer yourself to NHS physiotherapy for joint or muscle "
                  "pain like this, without seeing a GP first.",
    "gp": "This needs a clinician to assess it, so your GP practice is the right place.",
}


def _call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                              "id": "call_" + uuid.uuid4().hex[:10],
                                              "type": "tool_call"}])


class OfflineRouteModel:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def _read(messages: list) -> tuple[str, list[tuple[str, dict, object]]]:
        humans = [m for m in messages if isinstance(m, HumanMessage)
                  and not str(m.content).startswith("Finish by calling")]
        text = " ".join(str(m.content) for m in humans)
        names = {}
        results = []
        for m in messages:
            if isinstance(m, AIMessage):
                for c in m.tool_calls or []:
                    names[c["id"]] = (c["name"], c["args"])
            elif isinstance(m, ToolMessage):
                name, args = names.get(m.tool_call_id, (m.name, {}))
                try:
                    payload = json.loads(m.content)
                except (TypeError, ValueError):
                    payload = {"error": str(m.content)}
                results.append((name, args, payload))
        return text, results

    @staticmethod
    def classify(text: str) -> tuple[str, str | None]:
        for pat, cond in PF_CONDITIONS:
            if re.search(pat, text, re.I):
                return "pharmacy_first", cond
        for pat, svc in SELF_REFERRAL:
            if re.search(pat, text, re.I):
                return svc, None
        if re.search(PHARMACY, text, re.I):
            return "pharmacy", None
        return "gp", None

    def _gp(self, results) -> AIMessage:
        if not any(n == "get_registered_gp" for n, _, _ in results):
            return _call("get_registered_gp", {})
        return _call("recommend", {"care_route": "gp", "provider_ids": [],
                                   "reason_for_patient": REASONS["gp"]})

    async def ainvoke(self, messages: list) -> AIMessage:
        self.calls += 1
        text, results = self._read(messages)
        route, cond = self.classify(text)
        if any(n == "recommend" and isinstance(p, dict) and p.get("error")
               for n, _, p in results):
            return self._gp(results)                    # a rejected recommendation
        if route == "gp":
            return self._gp(results)

        tool = {"pharmacy_first": "find_pharmacy_first", "pharmacy": "find_pharmacy"}.get(
            route, "find_self_referral_service")
        mine = [(a, p) for n, a, p in results if n == tool]
        if not mine:
            args = {"condition": cond} if route == "pharmacy_first" else (
                {} if route == "pharmacy" else {"service": route})
            return _call(tool, args)
        args, payload = mine[-1]
        if route == "pharmacy_first" and payload.get("eligible") is False:
            return self._gp(results)
        found = payload.get("results") or []
        if not found:
            radius = float(args.get("radius_km") or (8 if route not in
                                                      ("pharmacy", "pharmacy_first") else 5))
            if radius < 20:
                return _call(tool, {**args, "radius_km": min(radius * 2.5, 30)})
            return self._gp(results)
        rec = {"care_route": route, "provider_ids": [r["id"] for r in found[:3]],
               "reason_for_patient": REASONS[route]}
        if route == "pharmacy_first":
            rec["pharmacy_first_condition"] = cond
        return _call("recommend", rec)
