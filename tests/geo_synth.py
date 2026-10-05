"""
tests/geo_synth.py — a deterministic, city-sized synthetic OSM extract.

The hand-made fixture (7 rows) proves import SHAPES; it cannot prove that the
PostGIS query returns the same answers as the Python filters, because nothing
in it is deduped, cut by a LIMIT or tied on distance. This generator makes a
Bengaluru-like extract with the cases that broke things before:

  * chains whose many branches share one name (dedupe keeps the nearest)
  * long ";" speciality lists, British spellings, "Ortho" vs orthodontic
  * chemists mapped as shop=medical_supply / shop=yes "X Medicals"
  * "Manipal Medical Centre" (a clinic, NOT a chemist), "Dr X Spine" hospitals
  * narrow boutiques (skin/hair/dental), emergency=yes|no, Kannada names
  * decoys the import must drop: unnamed clinics, cafes, ordinary shops

Density falls off with distance from the centre (radius drawn uniformly), so
~3,000 rows sit within 8 km of the centre, like central Bengaluru.

    python -m tests.geo_synth out.osm [n]
"""

from __future__ import annotations

import math
import random
import sys
from xml.sax.saxutils import quoteattr

CENTRE = (12.9716, 77.5946)
SPREAD_KM = 35.0

_TOKENS = ["orthopaedics", "orthopedics", "cardiology", "dermatology", "paediatrics",
           "pediatrics", "gynaecology", "obstetrics", "ent", "otolaryngology",
           "ophthalmology", "psychiatry", "dentistry", "orthodontics", "general",
           "neurology", "neurosurgery", "urology", "oncology", "pulmonology",
           "nephrology", "endocrinology", "gastroenterology", "physiotherapy",
           "Cardiology ", " ORTHOPAEDICS", "diabetology", "dialysis"]
_PREFIX = ["Sri Sai", "Lakshmi", "Ganesh", "Sparsh", "Sanjeevini", "City", "Care",
           "Dr Ramesh", "Dr. Anitha", "Nagarjuna", "Vasavi", "Sakra", "Aster",
           "Fortis", "Prakash", "Kaveri", "Mahaveer", "Om", "New Life", "Sunrise",
           "ಶ್ರೀ ಸಾಯಿ", "ಲಕ್ಷ್ಮಿ", "İstanbul", "Ärzte"]
_SUFFIX = {
    "pharmacy": ["Medicals", "Pharma", "Pharmacy", "Chemist & Druggist", "Drug Store",
                 "Aushadhalaya", "Medical Stores", "ಮೆಡಿಕಲ್ಸ್"],
    "clinic": ["Clinic", "Ortho Care", "Bone and Joint Clinic", "Heart Care", "Skin & Hair Clinic",
               "Dental Care", "Orthodontic Centre", "Eye Clinic", "Children's Clinic",
               "ENT Clinic", "Neuro Clinic", "Kidney Care", "Diabetes Centre",
               "Women's Clinic", "IVF Centre", "Cancer Care", "Chest Clinic",
               "Physiotherapy", "Ayurveda Centre", "Homoeopathy", "Diagnostics",
               "Medical Centre", "Family Doctor", "Gastro Clinic", "Uro Care",
               "Laser & Cosmetic", "Psychiatric Clinic", "Veterinary Clinic"],
    "hospital": ["Hospital", "Multispeciality Hospital", "Eye Hospital", "Spine Hospital",
                 "Children's Hospital", "Maternity Hospital", "Heart Hospital",
                 "Medical College Hospital", "Nursing Home", "Dental Hospital"],
}
_CHAINS = [  # (name, tags) — many branches, one name
    ("Apollo Pharmacy", {"amenity": "pharmacy"}),
    ("MedPlus", {"shop": "chemist"}),
    ("Wellness Forever", {"healthcare": "pharmacy"}),
    ("Apollo Clinic", {"amenity": "clinic", "healthcare:speciality": "general;pediatrics"}),
    ("Clove Dental", {"amenity": "dentist", "healthcare": "dentist"}),
    ("Manipal Hospital", {"amenity": "hospital", "emergency": "yes"}),
    ("Cloudnine Hospital", {"amenity": "hospital",
                            "healthcare:speciality": "gynaecology;paediatrics"}),
    ("Narayana Health City", {"amenity": "hospital"}),
]


def _point(rng: random.Random) -> tuple[float, float]:
    r_km = SPREAD_KM * rng.random()                  # uniform radius: dense centre
    theta = rng.random() * 2 * math.pi
    lat = CENTRE[0] + (r_km / 111.32) * math.sin(theta)
    lng = CENTRE[1] + (r_km / (111.32 * math.cos(math.radians(CENTRE[0])))) * math.cos(theta)
    return round(lat, 7), round(lng, 7)


def _speciality(rng: random.Random) -> str:
    n = rng.choice([1, 1, 1, 2, 3, 5, 12, 20])
    return ";".join(rng.sample(_TOKENS, min(n, len(_TOKENS))))


def _healthcare_tags(rng: random.Random) -> dict:
    roll = rng.random()
    if roll < 0.12:
        name, tags = rng.choice(_CHAINS)
        return {"name": name, **tags}
    kind = rng.choices(["pharmacy", "clinic", "hospital"], [4, 5, 2])[0]
    name = f"{rng.choice(_PREFIX)} {rng.choice(_SUFFIX[kind])}"
    if kind == "pharmacy":
        tags = rng.choice([{"amenity": "pharmacy"}, {"shop": "chemist"},
                           {"shop": "medical_supply"}, {"shop": "medical"},
                           {"shop": "yes"}, {"shop": "convenience"},
                           {"healthcare": "pharmacy"}, {"shop": "pharmacy"}])
    elif kind == "clinic":
        tags = rng.choice([{"amenity": "clinic"}, {"amenity": "doctors"},
                           {"healthcare": "clinic"}, {"healthcare": "doctor"},
                           {"amenity": "clinic", "healthcare": "clinic"},
                           {"healthcare": "dentist"}, {"healthcare": "laboratory"}])
    else:
        tags = {"amenity": "hospital"}
        if rng.random() < 0.3:
            tags["healthcare"] = "hospital"
    if rng.random() < 0.35:
        tags["healthcare:speciality"] = _speciality(rng)
    if kind == "hospital" and rng.random() < 0.5:
        tags["emergency"] = rng.choice(["yes", "yes", "no"])
    return {"name": name, **tags}


def _decoy_tags(rng: random.Random) -> dict:
    """Things the import must DROP (or must keep only by the pharmacy-name rule)."""
    return rng.choice([
        {"healthcare": "clinic"},                                   # unnamed
        {"amenity": "pharmacy"},                                    # unnamed
        {"amenity": "cafe", "name": "Cafe Medical Road"},           # not healthcare
        {"amenity": "restaurant", "name": "Sri Sai Pharma Canteen"},  # not a shop
        {"shop": "clothes", "name": "Lakshmi Silks"},
        {"shop": "supermarket", "name": "More Supermarket"},
        {"shop": "yes", "name": "Om General Stores"},
        {"shop": "convenience", "name": "Ganesh Pharma & General"},  # KEPT: name rule
        {"amenity": "veterinary", "name": "Pet Care Veterinary"},
    ])


def generate(n: int = 13000, seed: int = 7) -> str:
    """An .osm XML document with n healthcare-ish nodes plus ~n/4 decoys."""
    rng = random.Random(seed)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<osm version="0.6" generator="careroute-tests">']
    node_id = 0
    for i in range(n + n // 4):
        tags = _healthcare_tags(rng) if i < n else _decoy_tags(rng)
        lat, lng = _point(rng)
        node_id += 1
        out.append(f'  <node id="{node_id}" version="1" lat="{lat}" lon="{lng}">')
        out.extend(f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items())
        out.append("  </node>")
        if i < n and rng.random() < 0.02:          # same facility mapped twice, ~30 m apart
            node_id += 1
            out.append(f'  <node id="{node_id}" version="1" lat="{lat + 0.0002}" lon="{lng}">')
            out.extend(f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items())
            out.append("  </node>")
    out.append("</osm>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 13000
    with open(path, "w", encoding="utf-8") as f:
        f.write(generate(n))
