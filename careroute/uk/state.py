"""
uk/state.py — the one typed object that flows through the journey graph.

Each node reads a few keys and returns only the keys it changes; LangGraph
merges them and checkpoints once per turn (durability="exit").

Reducers: `messages` appends (add_messages) and `confirmed_providers` is a
set union that grows across turns. Everything else is last-write-wins.

SECURITY RULE: only `authorise` (Path A) and `load_referral` (Path B, from
e-RS) write patient_id, and every tool and integration takes the patient
from state — never from model arguments. requester_id is set by the API from
the verified NHS login identity before the graph runs.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages


def union(a: set | None, b: set | None) -> set:
    return set(a or ()) | set(b or ())


class JourneyState(TypedDict, total=False):
    # conversation
    messages: Annotated[list, add_messages]
    entry: Literal["symptoms", "referral"]

    # who
    requester_id: str | None            # verified NHS number, set by the API
    location_hint: dict | None          # browser GPS / typed place, set by the API
    subject: dict | None                # candidate from identify_subject (LLM)
    authority: dict | None              # decided by authorise (code)
    patient_id: str | None              # set ONLY by authorise / load_referral
    patient: dict | None                # read-only snapshot for this turn
    location: dict | None               # where to search from

    # clinical
    triage: dict | None                 # level, red_flags, reason, source
    care_route: str | None              # pharmacy_first | sexual_health | gp | ...
    route_reason: str | None
    referral: dict | None               # ubrn, specialty, priority, source
    episode_id: str | None              # links a GP request to a later referral

    # search and choice
    pref_text: str | None
    preferences: dict | None            # max_wait_weeks, max_km, max_cost_gbp, rank_by
    confirmed_providers: Annotated[set, union]
    options: list
    excluded_options: list
    selected_option: dict | None
    available_slots: list
    selected_slot: dict | None

    # booking
    consent: bool
    idempotency_key: str | None
    booking_attempts: int
    booking: dict | None
    sms_status: str | None
    outcome: str | None

    # per-turn counters
    llm_calls: int
    tool_errors: list


def fresh_turn() -> dict:
    """Reset at the start of every /chat turn. patient_id and authority are
    reset too: permission is re-checked every turn (a consent link may have
    expired, or this message may be about someone else)."""
    return {"outcome": None, "care_route": None, "route_reason": None, "options": [],
            "excluded_options": [], "selected_option": None, "available_slots": [],
            "selected_slot": None, "consent": False, "idempotency_key": None,
            "booking_attempts": 0, "booking": None, "sms_status": None, "llm_calls": 0,
            "tool_errors": [], "triage": None, "subject": None, "authority": None,
            "patient_id": None, "patient": None, "location": None, "pref_text": None}
