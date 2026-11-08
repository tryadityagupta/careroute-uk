"""
uk/triage_rules.py — red-flag rules that run BEFORE any model call.

Why rules first: an emergency answer must not wait on a model (seconds), and
must not depend on one being available at all. A rule match short-circuits
the turn: no login check, no model call, the 999 / 111 answer goes out.

Combination with the model (graph.py): final level = the HIGHER of the rules
and the model. The model can escalate a case the rules missed; it can never
downgrade a rule match.

CLINICAL STATUS: these rules are an ENGINEER'S DRAFT for a demo. Before any
real use they must be written, signed off and reviewed by clinicians under
DCB0129/DCB0160, and evaluated against a clinician-labelled golden set.
They deliberately over-trigger (no negation handling: "no chest pain"
still matches) — a false alarm costs a phone call, a miss costs far more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVELS = ("routine", "urgent", "emergency")


def higher(a: str | None, b: str | None) -> str:
    ia = LEVELS.index(a) if a in LEVELS else 0
    ib = LEVELS.index(b) if b in LEVELS else 0
    return LEVELS[max(ia, ib)]


@dataclass(frozen=True)
class Rule:
    id: str
    level: str                  # "emergency" | "urgent"
    all_of: tuple[str, ...]     # every pattern must match
    label: str
    mental_health: bool = False


def _r(*patterns: str) -> tuple[str, ...]:
    return patterns


RULES: tuple[Rule, ...] = (
    # --- emergency: call 999 ------------------------------------------------
    Rule("chest_pain_cardiac", "emergency",
         _r(r"chest (pain|tight|pressure|heaviness)|heart attack|crushing",
            r"breath|sweat|clammy|arm|jaw|crushing|sick|nause|heart attack"),
         "Chest pain with features of a heart attack"),
    Rule("stroke", "emergency",
         _r(r"droop|face (has )?fallen|slurred|\bstroke\b|"
            r"(arm|leg|one side|side of (my|his|her|their) body).{0,20}(weak|numb|can'?t move)"),
         "Possible stroke (FAST signs)"),
    Rule("breathing", "emergency",
         _r(r"can'?t breathe|cannot breathe|not breathing|struggling to breathe|"
            r"choking|(lips|face) (turning |gone |are |is )?(blue|grey|gray)|gasping"),
         "Severe difficulty breathing"),
    Rule("unconscious", "emergency",
         _r(r"unconscious|unresponsive|won'?t wake|not waking|collapsed|passed out"),
         "Collapse or loss of consciousness"),
    Rule("seizure", "emergency",
         _r(r"seizure|fitting|having a fit|convuls"),
         "Seizure"),
    Rule("severe_bleeding", "emergency",
         _r(r"bleeding (heavily|a lot|badly)|heavy bleeding|won'?t stop bleeding|"
            r"can'?t stop (the )?bleeding|spurting"),
         "Severe bleeding"),
    Rule("anaphylaxis", "emergency",
         _r(r"(swollen|swelling|swell).{0,15}(lips?|tongue|throat)|"
            r"(lips?|tongue|throat).{0,15}(swollen|swelling|swell)|anaphyla"),
         "Possible severe allergic reaction"),
    Rule("non_blanching_rash", "emergency",
         _r(r"rash.{0,30}(doesn'?t|does not|won'?t) fade|non.?blanching|glass test"),
         "Rash that does not fade under pressure"),
    Rule("sepsis", "emergency", _r(r"\bsepsis\b|septic"), "Possible sepsis"),
    Rule("head_injury", "emergency",
         _r(r"head (injury|bang|knock)|hit (my|his|her|their) head|banged (my|his|her|their) head",
            r"vomit|being sick|unconscious|confus|drows|fit|seizure"),
         "Head injury with warning signs"),
    Rule("suicide_risk", "emergency",
         _r(r"suicid|kill (my|him|her|them)sel(f|ves)|end (my|his|her|their) life|"
            r"taken (an )?overdose|took (an )?overdose|taken too many"),
         "Immediate risk to life (mental health)", mental_health=True),
    # --- urgent: NHS 111 / urgent treatment centre today ---------------------
    Rule("self_harm_thoughts", "urgent",
         _r(r"self.?harm|hurt(ing)? (my|him|her|them)sel(f|ves)|mental health crisis"),
         "Mental health crisis", mental_health=True),
    Rule("infant_fever", "urgent",
         _r(r"\b(baby|newborn|infant|\d+ ?(week|month)s? old)\b",
            r"fever|temperature|hot to touch|high temp"),
         "Fever in a young baby"),
    Rule("uti_complicated", "urgent",
         _r(r"\buti\b|cystitis|burn(s|ing)? when (i |they |she |he )?(pee|wee|urinat)|"
            r"pain(ful)? (when )?(peeing|weeing|urinating)",
            r"fever|temperature|shiver|back pain|side pain|loin|vomit|being sick|blood"),
         "Urine infection with fever, back pain or blood"),
    Rule("pregnancy_bleeding", "urgent",
         _r(r"pregnan", r"bleed|cramp|severe pain"),
         "Bleeding or pain in pregnancy"),
    Rule("possible_fracture", "urgent",
         _r(r"\bbroken?\b|\bbroke (my|his|her|their)\b|fractur|can'?t (put|bear) weight|deformed|bone sticking"),
         "Possible broken bone"),
    Rule("sudden_vision", "urgent",
         _r(r"sudden(ly)? (loss of |lost )?(vision|sight)|chemical in (my |his |her )?eye|"
            r"can'?t see (out of|from)"),
         "Sudden eye problem"),
    Rule("significant_burn", "urgent",
         _r(r"\bburn(t|ed|s)?\b", r"large|blister|child|baby|face|hand|chemical|electric"),
         "Burn needing assessment"),
)

_COMPILED = [(rule, [re.compile(p, re.I) for p in rule.all_of]) for rule in RULES]


def evaluate(text: str) -> dict:
    """{level, red_flags: [labels], rule_ids, mental_health}. 'routine' with
    no flags means the rules found nothing — the model decides the rest."""
    hits = [rule for rule, pats in _COMPILED if all(p.search(text or "") for p in pats)]
    level = "routine"
    for rule in hits:
        level = higher(level, rule.level)
    top = [r for r in hits if r.level == level]
    return {"level": level,
            "red_flags": [r.label for r in top],
            "rule_ids": [r.id for r in top],
            "mental_health": any(r.mental_health for r in top),
            "source": "rules" if hits else None}
