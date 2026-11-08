"""
uk/pharmacy_first.py — NHS Pharmacy First (England) clinical pathways.

The seven conditions and their eligibility bands, as published in the NHS
England service specification (January 2024). Eligibility is decided HERE,
in code, from the patient's record (age, sex) — never by the model. The
model only names the condition it heard.

Keep this table in step with the current service specification; it is
data, not logic, so a change is a one-line edit plus a test.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pathway:
    key: str
    label: str
    min_age: int
    max_age: int | None = None          # inclusive
    sex: str | None = None              # "F" | "M" | None (any)


PATHWAYS: dict[str, Pathway] = {p.key: p for p in (
    Pathway("acute_otitis_media", "Acute otitis media (earache)", 1, 17),
    Pathway("impetigo", "Impetigo", 1),
    Pathway("infected_insect_bite", "Infected insect bite", 1),
    Pathway("shingles", "Shingles", 18),
    Pathway("acute_sinusitis", "Acute sinusitis", 12),
    Pathway("acute_sore_throat", "Acute sore throat", 5),
    Pathway("uncomplicated_uti", "Uncomplicated urinary tract infection", 16, 64, "F"),
)}


def eligibility(condition: str, *, age: int | None, sex: str | None) -> dict:
    """{eligible, condition, label, reason}. Unknown age/sex is NOT eligible:
    the pathway's criteria can't be confirmed, so the patient goes elsewhere."""
    p = PATHWAYS.get(condition)
    if p is None:
        return {"eligible": False, "condition": condition, "label": condition,
                "reason": "Not one of the seven Pharmacy First conditions."}
    out = {"condition": p.key, "label": p.label}
    if age is None:
        return {**out, "eligible": False, "reason": "Age unknown, so eligibility can't be confirmed."}
    if age < p.min_age or (p.max_age is not None and age > p.max_age):
        band = f"{p.min_age}+" if p.max_age is None else f"{p.min_age} to {p.max_age}"
        return {**out, "eligible": False,
                "reason": f"Pharmacy First covers this for ages {band}; the patient is {age}."}
    if p.sex and (sex or "").upper() != p.sex:
        return {**out, "eligible": False,
                "reason": "Pharmacy First covers this pathway for women only."}
    return {**out, "eligible": True, "reason": "Meets the Pharmacy First criteria."}
