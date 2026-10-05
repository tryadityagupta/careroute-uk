"""
providers/osm/matching.py — deciding what an OSM facility IS.

THE ONE PLACE these rules live. Two consumers:

  * OsmProviderDirectory applies them at request time to elements from
    Overpass (and the test fakes).
  * The PostGIS import (infra/geo/import.sh -> classify.py) applies them ONCE
    PER ROW and stores the answers as columns, so a request filters on stored
    booleans and arrays instead of running regexes on the event loop.

Because both run THIS code, the two paths cannot drift apart. (The rules used
to be half-copied into the Lua import style, with a "keep in sync" comment.)

Questions answered here, each with rules learnt from real misses:
  * Is this element in the directory at all?        in_directory()
  * Does this facility match a specialty?           SpecialtyMatcher
  * Is it a pharmacy / chemist?                     looks_like_pharmacy()
  * Is it a narrow boutique (skin/hair/dental...)?  is_narrow_specialty()
  * Could it be an emergency department?            is_er_candidate()
  * Everything above for one element, at once:      Facility.from_tags()

OSM is community-mapped, so specialty tagging is sparse and inconsistent:
"orthopaedics" (British), clinic NAMES like "Sparsh Ortho Care", and Indian
chemists mapped as shop=medical_supply or a generic shop called "X Medicals".
Every rule below exists because of a wrong answer someone actually got.

Stdlib only: the geo import image runs this file without the app's
dependencies (see classify.py).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Specialty matching. Keys are regexes matched at a WORD START of the
# requested specialty (a bare "ent" would otherwise fire for
# "Gastroenterology" and "Dentistry"); include/exclude are matched against
# facility names and speciality-tag tokens. An EXCLUDE handles ambiguous
# short forms: "ortho" must not match an orthodontic dental clinic.
#
# The first field is a stable id. The import stores, per row, the ids it
# matches; a lookup filters on the requested id. Changing these rules needs a
# re-import (and renaming an id, a SCHEMA_VERSION bump in maps/postgis.py and
# import.sh).
# ---------------------------------------------------------------------------
_SPECIALTY_PATTERNS = [
    ("orthopedics", ("ortho",),
     r"orthop(a)?ed|\bortho\b|bone\s*(and|&)\s*joint|joint\s*replacement|fracture",
     r"orthodont|dental|dentist|\bteeth\b"),
    ("pediatrics", ("pediatr", "paediatr", "child"),
     r"pa?ediatr|children'?s\s*(hospital|clinic)|\bchild\s*care\b", None),
    ("gynecology", ("gyn", "obstet", "women"),
     r"gyn(a)?ecolog|obstetric|maternity|women'?s\s*(hospital|clinic)|\bivf\b", None),
    ("ent", ("ent\\b", "otolaryng", "ear\\b"),
     r"\bent\b|otolaryng|otorhinolaryng|ear,?\s*nose", None),
    ("cardiology", ("cardio", "heart"), r"cardi|\bheart\b", None),
    ("dermatology", ("derma", "skin"), r"dermatolog|\bskin\b", None),
    ("neurology", ("neuro",), r"neurolog|neuro\s*(care|clinic|hospital)|\bneuro\b",
     r"neurosurg(?!ery)"),
    ("psychiatry", ("psychiat", "mental"), r"psychiatr|mental\s*health|de-?addiction", None),
    ("ophthalmology", ("ophthalm", "eye"), r"ophthalm|\beye\b", None),
    ("gastroenterology", ("gastro",), r"gastro", None),
    ("urology", ("uro",), r"urolog|\buro\b", None),
    ("oncology", ("oncol", "cancer"), r"oncolog|cancer", None),
    ("pulmonology", ("pulmon", "chest", "respir"), r"pulmon|\bchest\b|respirat", None),
    ("nephrology", ("nephro", "kidney"), r"nephrolog|kidney|dialysis", None),
    ("endocrinology", ("endocrin", "diabet"), r"endocrin|diabet", None),
    ("dentistry", ("dent",), r"dent|orthodont", None),
    ("general", ("general", "family"),
     r"general\s*(medicine|practice|physician)|family\s*(medicine|doctor)", None),
]


@dataclass(frozen=True)
class Specialty:
    id: str
    keys: tuple[str, ...]
    include: re.Pattern
    exclude: re.Pattern | None

    def matches(self, text: str) -> bool:
        return bool(self.include.search(text)) and not (
            self.exclude and self.exclude.search(text))


SPECIALTIES: tuple[Specialty, ...] = tuple(
    Specialty(sid, keys, re.compile(inc, re.I), re.compile(exc, re.I) if exc else None)
    for sid, keys, inc, exc in _SPECIALTY_PATTERNS)


class SpecialtyMatcher:
    """matcher = SpecialtyMatcher("Orthopedics"); matcher("Sparsh Ortho Care")

    .key   the Specialty id the request resolved to, or None if unknown
    .stem  for an unknown specialty, the substring that is searched instead
    """

    def __init__(self, specialty: str):
        self.specialty = specialty
        spec = specialty.lower()
        self.entry: Specialty | None = next(
            (s for s in SPECIALTIES if any(re.search(r"\b" + k, spec) for k in s.keys)),
            None)
        self.key = self.entry.id if self.entry else None
        # Unknown specialty: a crude stem ('psychiatry' -> 'psychiatr' matches
        # 'Psychiatric'). A heuristic, not lemmatisation.
        self.stem = None if self.entry else spec.rstrip("sy")

    def __call__(self, text: str) -> bool:
        if self.entry is None:
            return self.stem in text.lower()
        return self.entry.matches(text)


# ---------------------------------------------------------------------------
# Which elements are in the directory at all. Overpass's query (sources.py)
# is built from these same constants; the PostGIS import applies
# in_directory() to every candidate row the Lua style hands it.
# Overpass regexes are POSIX ERE: keep PHARMACY_NAME_RE simple.
# ---------------------------------------------------------------------------
DIRECTORY_AMENITIES = ("hospital", "clinic", "doctors", "pharmacy")
PHARMACY_SHOPS = ("chemist", "pharmacy", "medical_supply", "medical")
PHARMACY_NAME_RE = "medical|pharma|chemist|drug ?store|aushadh"
_PHARMACY_NAME = re.compile(PHARMACY_NAME_RE, re.I)


def in_directory(tags: dict) -> bool:
    """Named, and a hospital/clinic/doctor/pharmacy by amenity, any
    healthcare=* tag, a chemist-type shop, or any shop whose NAME says chemist
    ("... Medicals", "... Pharma"). A place you cannot name cannot be
    recommended, so unnamed elements are out."""
    name = tags.get("name")
    if not name:
        return False
    shop = tags.get("shop")
    return (tags.get("amenity") in DIRECTORY_AMENITIES
            or tags.get("healthcare") is not None
            or shop in PHARMACY_SHOPS
            or (bool(shop) and bool(_PHARMACY_NAME.search(name))))


# ---------------------------------------------------------------------------
# Pharmacy detection.
# ---------------------------------------------------------------------------
def looks_like_pharmacy(name: str, amenity: str, healthcare: str, shop: str) -> bool:
    if "pharmacy" in amenity or "pharmacy" in healthcare:
        return True
    if shop in PHARMACY_SHOPS:
        return True
    # Name-based only for SHOPS: "Manipal Medical Centre" (amenity=hospital)
    # is not a chemist and must not be relabelled.
    return bool(shop) and not amenity and bool(_PHARMACY_NAME.search(name))


# ---------------------------------------------------------------------------
# Narrow single-specialty boutiques: noise in a GENERAL list (a hair clinic
# kept being suggested for diarrhoea). Never filtered from specialist results,
# never applied to pharmacies or full hospitals.
# ---------------------------------------------------------------------------
_NARROW_SPECIALTY = (
    "skin", "hair", "laser", "cosmetic", "aesthetic", "derma",
    "dental", "dentist", "orthodont", "ophthal", "optical", "optician",
    "fertility", "ivf", "veterinary",
)


def _is_narrow(is_pharmacy: bool, facility: str, signal: str) -> bool:
    if is_pharmacy or "hospital" in facility:
        return False
    return any(kw in signal for kw in _NARROW_SPECIALTY)


def is_narrow_specialty(item: dict) -> bool:
    return _is_narrow(bool(item.get("is_pharmacy")), item.get("facility") or "",
                      item.get("_signal", ""))


# ---------------------------------------------------------------------------
# Emergency departments. OSM tags many single-doctor or single-specialty
# practices as hospitals ("Dr Ramesh Dalwai Spine Surgeon" was offered as an
# ER for chest pain).
# ---------------------------------------------------------------------------
NOT_AN_ER = re.compile(
    r"^dr\.?\s|\bspine\b|\beye\b|ophthalm|dental|dentist|\bskin\b|\bhair\b|"
    r"cosmetic|\bivf\b|fertility|physiotherap|ayurved|homo?eopath|\bdiagnostic", re.I)


def is_er_candidate(name: str, facility: str, er: str | None) -> bool:
    """A hospital, not tagged emergency=no, not named like a single practice."""
    return "hospital" in facility and er != "no" and not NOT_AN_ER.search(name)


# ---------------------------------------------------------------------------
# One element, classified. Independent of where the patient is, so the
# PostGIS import can compute it once and store it.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Facility:
    name: str
    facility: str            # amenity or healthcare or shop, as tagged
    is_pharmacy: bool
    signal: str              # what the place IS, lowered: for the narrow filter
    er: str                  # OSM emergency=yes|no, lowered ("" if untagged)
    speciality_tag: str      # raw healthcare:speciality
    tokens: tuple[str, ...]  # that tag split on ";", stripped, lowered

    @classmethod
    def from_tags(cls, tags: dict) -> Facility | None:
        """None for an unnamed element."""
        name = tags.get("name")
        if not name:
            return None
        amenity = (tags.get("amenity") or "").lower()
        healthcare = (tags.get("healthcare") or "").lower()
        shop = (tags.get("shop") or "").lower()
        raw_tag = tags.get("healthcare:speciality", "")
        return cls(
            name=name,
            facility=(tags.get("amenity") or tags.get("healthcare")
                      or tags.get("shop") or "healthcare"),
            is_pharmacy=looks_like_pharmacy(name, amenity, healthcare, shop),
            signal=" ".join((name.lower(), amenity, healthcare, shop, raw_tag.lower())),
            er=(tags.get("emergency") or "").lower(),
            speciality_tag=raw_tag,
            # Speciality tags are often long ";" lists, sometimes pasted
            # across many POIs; substring-matching the blob confirmed a DENTAL
            # clinic for Psychiatry. Matching is done token by token.
            tokens=tuple(t.strip().lower() for t in raw_tag.split(";") if t.strip()),
        )

    @property
    def is_hospital(self) -> bool:
        return "hospital" in self.facility

    @property
    def is_narrow(self) -> bool:
        return _is_narrow(self.is_pharmacy, self.facility, self.signal)

    @property
    def is_er_candidate(self) -> bool:
        return is_er_candidate(self.name, self.facility, self.er)

    @property
    def dedupe_key(self) -> str | None:
        """Big facilities exist as a node AND a way: same name, keep one."""
        key = self.name.lower()
        return None if key == "unnamed facility" else key

    def specialty_ids(self) -> tuple[list[str], list[str]]:
        """(ids matched by NAME, ids matched by a speciality-tag TOKEN)."""
        by_name = [s.id for s in SPECIALTIES if s.matches(self.name)]
        by_tag = [s.id for s in SPECIALTIES if any(s.matches(t) for t in self.tokens)]
        return by_name, by_tag

    def match(self, matcher: SpecialtyMatcher) -> dict:
        """The specialty fields the directory puts on an item. Evidence is
        recorded so no match is ever opaque; name beats tag."""
        if matcher(self.name):
            return {"specialty_match": True, "matched_via": "name"}
        if any(matcher(t) for t in self.tokens):
            return {"specialty_match": True, "matched_via": "speciality_tag",
                    "speciality_tag": self.speciality_tag, "_tag_tokens": len(self.tokens)}
        return {"specialty_match": False}


# ---------------------------------------------------------------------------
# A fingerprint of every rule above. The import stores it in geo.dataset;
# /healthz reports whether the stored answers were computed with the rules
# this build is running, so an edited regex without a re-import is visible.
# ---------------------------------------------------------------------------
RULES_VERSION = hashlib.sha256(repr((
    _SPECIALTY_PATTERNS, DIRECTORY_AMENITIES, PHARMACY_SHOPS, PHARMACY_NAME_RE,
    _NARROW_SPECIALTY, NOT_AN_ER.pattern,
)).encode("utf-8")).hexdigest()[:12]
