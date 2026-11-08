"""
uk/llm.py — the three single-shot language steps (no tools, structured output).

    identify_subject     "my son's rash"  -> {relationship: child, name: None}
    triage               message          -> {level, red_flags, reason}
    extract_preferences  "under 6 weeks, within 20 km, under £200"
                                          -> {max_wait_weeks, max_km, max_cost_gbp, rank_by}

Each returns a CANDIDATE that code then checks: authority.py confirms the
subject against records, the triage level is combined with the rules (the
model can escalate, never downgrade), and preference values are converted
and clamped here, in code (miles -> km, months -> weeks, money as Decimal).

Two implementations behind one interface:
  OpenAiSteps   gpt-4o-mini via LangChain structured output (function calling)
  OfflineSteps  deterministic regex rules: no key, no network. Used by tests
                and by CAREROUTE_LLM=offline for an always-works demo.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

RELATIONSHIPS = ("self", "child", "parent", "partner", "grandparent", "sibling",
                 "friend", "other")


class SubjectOut(BaseModel):
    """Who the person needs care for."""
    relationship: Literal["self", "child", "parent", "partner", "grandparent", "sibling",
                          "friend", "other"] = Field(
        description="The patient's relationship TO THE PERSON WRITING. 'self' if they "
                    "are writing about themselves.")
    name: Optional[str] = Field(None, description="The patient's first name if the "
                                                  "message gives one, else null.")


class TriageOut(BaseModel):
    """Urgency of the problem described."""
    level: Literal["emergency", "urgent", "routine"] = Field(
        description="emergency: call 999 now. urgent: needs help today (NHS 111, urgent "
                    "treatment centre). routine: can wait for a pharmacy, GP or "
                    "self-referral service. If unsure between two levels, choose the higher.")
    red_flags: list[str] = Field(default_factory=list,
                                 description="Short phrases naming any red-flag symptoms.")
    reason: str = Field("", description="One sentence explaining the level.")


class PrefsOut(BaseModel):
    """Limits the patient stated for choosing where to be seen."""
    max_wait_value: Optional[float] = Field(None, description="Longest acceptable wait, as stated")
    max_wait_unit: Optional[Literal["days", "weeks", "months"]] = None
    max_distance_value: Optional[float] = Field(None, description="Furthest acceptable distance")
    max_distance_unit: Optional[Literal["km", "miles"]] = None
    max_cost_gbp: Optional[float] = Field(None, description="Most they will pay, in pounds. "
                                                            "0 if they want NHS / free only.")
    rank_by: Optional[Literal["wait", "distance", "cost"]] = Field(
        None, description="What matters most, if they said (soonest, nearest, cheapest).")


SUBJECT_PROMPT = """You identify WHO a UK patient-facing health message is about.
The writer is signed in. Return the patient's relationship to the writer and the
patient's first name if given. "My son", "my daughter", "my little boy" -> child.
"My mum", "my dad" -> parent. "My wife", "my husband", "my partner" -> partner.
"My friend Sam" -> friend, name Sam. Otherwise self.
People the writer can act for (first names): {linked}. If the message names one
of them, use that name."""

TRIAGE_PROMPT = """You are a cautious UK urgent-care triage classifier. Classify the
message only; do not give advice. emergency = call 999 now (e.g. chest pain with
breathlessness, stroke signs, severe breathing difficulty, collapse, seizure,
severe bleeding, anaphylaxis, immediate risk to life). urgent = needs NHS 111 or
an urgent treatment centre today (e.g. possible fracture, high fever in a baby,
urine infection with fever or back pain, bleeding in pregnancy, mental health
crisis). routine = can be handled by a pharmacy, GP or self-referral service.
If unsure between two levels, choose the higher."""

PREFS_PROMPT = """Extract the limits a UK patient states for choosing a hospital or
clinic: longest wait, furthest distance, most they will pay, and what matters
most. Copy numbers and units exactly as stated; do not convert units. If they
want NHS only or free, max_cost_gbp is 0. Leave anything not stated as null."""


def normalise_preferences(p: PrefsOut | dict) -> dict:
    """Unit conversion and clamping, in code. None = no limit."""
    p = p if isinstance(p, PrefsOut) else PrefsOut(**p)
    weeks = None
    if p.max_wait_value is not None:
        factor = {"days": 1 / 7, "weeks": 1, "months": 52 / 12}[p.max_wait_unit or "weeks"]
        weeks = round(max(0.0, min(p.max_wait_value * factor, 104)), 1)
    km = None
    if p.max_distance_value is not None:
        factor = 1.609344 if (p.max_distance_unit or "km") == "miles" else 1
        km = round(max(1.0, min(p.max_distance_value * factor, 300)), 1)
    cost = None
    if p.max_cost_gbp is not None:
        cost = str(Decimal(str(max(0.0, min(p.max_cost_gbp, 100000)))).quantize(Decimal("0.01")))
    return {"max_wait_weeks": weeks, "max_km": km, "max_cost_gbp": cost,
            "rank_by": p.rank_by}


class OpenAiSteps:
    def __init__(self, triage_model, extraction_model):
        self._subject = extraction_model.with_structured_output(SubjectOut, method="function_calling")
        self._prefs = extraction_model.with_structured_output(PrefsOut, method="function_calling")
        self._triage = triage_model.with_structured_output(TriageOut, method="function_calling")

    async def identify_subject(self, text: str, linked: list[str]) -> dict:
        out: SubjectOut = await self._subject.ainvoke([
            ("system", SUBJECT_PROMPT.format(linked=", ".join(linked) or "none")),
            ("human", text)])
        return out.model_dump()

    async def triage(self, text: str) -> dict:
        out: TriageOut = await self._triage.ainvoke([("system", TRIAGE_PROMPT), ("human", text)])
        return out.model_dump()

    async def extract_preferences(self, text: str) -> dict:
        out: PrefsOut = await self._prefs.ainvoke([("system", PREFS_PROMPT), ("human", text)])
        return normalise_preferences(out)


class OfflineSteps:
    """Deterministic stand-ins. Narrow on purpose: good enough to drive the
    demo scripts and the tests, not a substitute for the model."""

    _REL = [
        (r"\bmy (son|daughter|child|kid|boy|girl|little one|baby|toddler)\b", "child"),
        (r"\bmy (mum|mom|mother|dad|father|mam)\b", "parent"),
        (r"\bmy (wife|husband|partner|girlfriend|boyfriend)\b", "partner"),
        (r"\bmy (gran|grandma|grandmother|grandad|granddad|grandfather|nan)\b", "grandparent"),
        (r"\bmy (brother|sister)\b", "sibling"),
        (r"\b(my )?(friend|mate|flatmate|colleague|neighbour)\b", "friend"),
    ]
    # Writing about someone else without a recognised relationship ("her",
    # "Margaret's carer"): never "self". Authority then decides, from records.
    _THIRD_PARTY = re.compile(r"\b(she|he|her|him|they|them)\b|\b[A-Z][a-z]+'s\b|\bcarer\b|"
                              r"\bfor (someone|somebody)\b", re.I)
    _FULL_NAME = re.compile(r"\b([A-Z][a-z]+) [A-Z][a-z]+(?:'s)?\b")
    _NAME_AFTER = re.compile(r"\b(?:friend|mate|flatmate|colleague|neighbour|son|daughter|"
                             r"mum|dad|mother|father)\s+([A-Z][a-z]+)\b")

    async def identify_subject(self, text: str, linked: list[str]) -> dict:
        name = next((n for n in linked if re.search(rf"\b{re.escape(n)}\b", text, re.I)), None)
        rel = "self"
        for pat, r in self._REL:
            if re.search(pat, text, re.I):
                rel = r
                break
        if name is None:
            m = self._NAME_AFTER.search(text) or self._FULL_NAME.search(text)
            name = m.group(1) if m else None
        if rel == "self" and (name or self._THIRD_PARTY.search(text)):
            rel = "other"
        return {"relationship": rel, "name": name}

    async def triage(self, text: str) -> dict:
        return {"level": "routine", "red_flags": [],
                "reason": "Offline mode: only the red-flag rules ran."}

    async def extract_preferences(self, text: str) -> dict:
        t = text.lower()
        p: dict = {}
        if m := re.search(r"(\d+(?:\.\d+)?)\s*(day|week|month)s?", t):
            p["max_wait_value"], p["max_wait_unit"] = float(m.group(1)), m.group(2) + "s"
        if m := re.search(r"(\d+(?:\.\d+)?)\s*(km|kilomet|mile|mi\b)", t):
            p["max_distance_value"] = float(m.group(1))
            p["max_distance_unit"] = "miles" if m.group(2).startswith("mi") else "km"
        if m := re.search(r"£\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:pounds|gbp|quid)", t):
            p["max_cost_gbp"] = float(m.group(1) or m.group(2))
        elif re.search(r"nhs only|free only|no cost|not pay|don'?t want to pay|free\b", t):
            p["max_cost_gbp"] = 0.0
        if re.search(r"soonest|quickest|fastest|asap|as soon as", t):
            p["rank_by"] = "wait"
        elif re.search(r"nearest|closest|close to home", t):
            p["rank_by"] = "distance"
        elif re.search(r"cheapest|lowest cost", t):
            p["rank_by"] = "cost"
        return normalise_preferences(PrefsOut(**p))
