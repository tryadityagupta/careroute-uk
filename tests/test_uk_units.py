"""
tests/test_uk_units.py — the pure, deterministic pieces of UK CareRoute.

These encode decisions that must never depend on a model: NHS number
validation, Pharmacy First eligibility, red-flag rules, unit conversion of a
patient's limits, the insurer's money maths, SMS privacy, and the structural
recommendation guard.
"""

from decimal import Decimal

import pytest

from careroute.uk import nhs_number, pharmacy_first, render, triage_rules
from careroute.uk.integrations import DemoBookingCore, DemoInsurer, money
from careroute.uk.llm import normalise_preferences
from careroute.uk.route_agent import RecommendationGuard
from careroute.uk.store import MemoryJourneyStore
from careroute.uk.clock import FixedClock
from tests.fakes import TOM, UK_NOW


# --- NHS numbers ----------------------------------------------------------------
def test_nhs_number_mod11():
    assert nhs_number.is_valid("943 476 5919")            # NHS England's published example
    assert not nhs_number.is_valid("943 476 5918")        # wrong check digit
    assert not nhs_number.is_valid("12345")
    assert nhs_number.is_test_number(TOM)
    assert nhs_number.is_valid(nhs_number.make_test_number(4242))


# --- Pharmacy First ---------------------------------------------------------------
@pytest.mark.parametrize("cond,age,sex,ok", [
    ("uncomplicated_uti", 34, "F", True),
    ("uncomplicated_uti", 34, "M", False),      # women only
    ("uncomplicated_uti", 70, "F", False),      # 16 to 64
    ("acute_otitis_media", 7, "M", True),
    ("acute_otitis_media", 30, "F", False),     # 1 to 17
    ("shingles", 17, "F", False),               # 18+
    ("acute_sore_throat", 4, "M", False),       # 5+
    ("acute_sinusitis", 12, "M", True),
    ("impetigo", None, "F", False),             # unknown age: not confirmed
    ("chest_infection", 40, "M", False),        # not a pathway
])
def test_pharmacy_first_eligibility(cond, age, sex, ok):
    assert pharmacy_first.eligibility(cond, age=age, sex=sex)["eligible"] is ok


# --- red-flag rules -----------------------------------------------------------------
@pytest.mark.parametrize("text,level", [
    ("crushing chest pain and I'm sweating", "emergency"),
    ("her face is drooping and her speech is slurred", "emergency"),
    ("he can't breathe properly", "emergency"),
    ("my lips are swelling up after eating nuts", "emergency"),
    ("a rash that doesn't fade with the glass test", "emergency"),
    ("I want to end my life", "emergency"),
    ("my 6 week old baby has a high temperature", "urgent"),
    ("burning when I pee and a fever with back pain", "urgent"),
    ("I think my wrist is broken", "urgent"),
    ("burning when I pee", "routine"),
    ("sore throat for three days", "routine"),
    ("my knee aches after running", "routine"),
])
def test_red_flag_rules(text, level):
    assert triage_rules.evaluate(text)["level"] == level


def test_rules_never_downgraded():
    assert triage_rules.higher("emergency", "routine") == "emergency"
    assert triage_rules.higher("routine", "urgent") == "urgent"
    assert triage_rules.higher(None, "routine") == "routine"


def test_mental_health_flag_changes_the_message():
    t = triage_rules.evaluate("I'm going to kill myself")
    assert t["mental_health"]
    text = render.emergency(t, [])
    assert "999" in text and "116 123" in text and "mental health option" in text


# --- the patient's limits -------------------------------------------------------------
def test_preferences_are_converted_in_code():
    p = normalise_preferences({"max_wait_value": 2, "max_wait_unit": "months",
                               "max_distance_value": 10, "max_distance_unit": "miles",
                               "max_cost_gbp": 199.999, "rank_by": "distance"})
    assert p == {"max_wait_weeks": 8.7, "max_km": 16.1, "max_cost_gbp": "200.00",
                 "rank_by": "distance"}
    assert normalise_preferences({})["max_km"] is None        # unstated: no limit


# --- money --------------------------------------------------------------------------------
@pytest.mark.anyio
async def test_insurer_quote_is_exact_and_comes_from_the_insurer():
    core = DemoBookingCore(MemoryJourneyStore(), FixedClock(UK_NOW))
    data = {"insurers": {"NMH": {"name": "N", "policies": {"P1": {
        "holder": TOM, "status": "active", "covered_specialties": ["dermatology"],
        "excess_remaining_gbp": "100.00", "needs_preauth": True}}}}}
    ins = DemoInsurer(data, core)
    prov = {"price_gbp": "220.00", "specialty": "dermatology", "networks": ["NMH"]}
    q = await ins.quote({"insurer": "NMH", "policy_number": "P1"}, patient_id=TOM, provider=prov)
    assert (q["patient_pays_gbp"], q["insurer_pays_gbp"]) == ("100.00", "120.00")
    out = await ins.quote({"insurer": "NMH", "policy_number": "P1"}, patient_id=TOM,
                          provider={**prov, "networks": []})
    assert out["patient_pays_gbp"] == "220.00" and not out["covered"]
    other = await ins.quote({"insurer": "NMH", "policy_number": "P1"}, patient_id="x",
                            provider=prov)
    assert other["funding"] == "self-pay"           # someone else's policy never applies
    assert money("0.1") + money("0.2") == Decimal("0.30")


# --- SMS privacy --------------------------------------------------------------------------
def test_sms_has_no_health_details():
    b = {"for_first_name": "Maya", "start": "2026-10-14T09:30", "booking_ref": "BK-1",
         "provider_name": "City Sexual Health Clinic", "kind": "direct"}
    text, fields = render.sms(b)
    assert "Sexual" not in text and "Clinic" not in text and "BK-1" in text
    assert set(fields) == {"first_name", "when", "reference"}


# --- the recommendation guard ---------------------------------------------------------------
SEEN = {"pharm:A": {"id": "pharm:A", "service_type": "pharmacy", "pharmacy_first": True},
        "pharm:B": {"id": "pharm:B", "service_type": "pharmacy", "pharmacy_first": False},
        "msk:A": {"id": "msk:A", "service_type": "msk_physio"},
        "gp:ZGP01": {"id": "gp:ZGP01", "service_type": "gp_practice"}}
ELIG = {"uncomplicated_uti": {"eligible": True}, "shingles": {"eligible": False}}


@pytest.mark.parametrize("args,ok", [
    ({"care_route": "pharmacy_first", "provider_ids": ["pharm:A"],
      "pharmacy_first_condition": "uncomplicated_uti"}, True),
    ({"care_route": "pharmacy_first", "provider_ids": ["pharm:Z"],
      "pharmacy_first_condition": "uncomplicated_uti"}, False),          # invented id
    ({"care_route": "pharmacy_first", "provider_ids": ["pharm:A"],
      "pharmacy_first_condition": "shingles"}, False),                   # not eligible
    ({"care_route": "pharmacy_first", "provider_ids": ["pharm:B"],
      "pharmacy_first_condition": "uncomplicated_uti"}, False),          # not a PF pharmacy
    ({"care_route": "msk_physio", "provider_ids": ["pharm:A"]}, False),  # wrong service
    ({"care_route": "msk_physio", "provider_ids": []}, False),           # nothing searched
    ({"care_route": "msk_physio", "provider_ids": ["msk:A"]}, True),
    ({"care_route": "gp", "provider_ids": []}, True),
    ({"care_route": "self_care", "provider_ids": ["pharm:A"]}, True),
    ({"care_route": "dentist", "provider_ids": []}, False),              # unknown route
])
def test_recommendation_guard(args, ok):
    res, err = RecommendationGuard().check({"reason_for_patient": "r", **args}, SEEN, ELIG,
                                           "gp:ZGP01")
    assert (res is not None) is ok and (err is None) is ok


def test_guard_needs_the_gp_lookup_first():
    res, err = RecommendationGuard().check({"care_route": "gp", "provider_ids": []},
                                           {}, {}, "gp:ZGP01")
    assert res is None and "get_registered_gp" in err
