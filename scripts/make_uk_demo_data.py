"""
scripts/make_uk_demo_data.py — regenerate data/uk/*.json (deterministic).

    python -m scripts.make_uk_demo_data

EVERYTHING here is fictional: organisations, names, addresses, waiting times,
prices and insurer terms. NHS numbers are in the 999 range NHS England keeps
for test data. Phone numbers are in Ofcom's 07700 900xxx drama range.
Coordinates are real places in Leeds (West Yorkshire) so drive times are
meaningful; no real service sits behind any of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from careroute.uk.nhs_number import make_test_number

OUT = Path(__file__).resolve().parent.parent / "data" / "uk"

N = {k: make_test_number(seed) for k, seed in
     (("priya", 1010), ("arjun", 1020), ("maya", 1030), ("tom", 1040),
      ("margaret", 1050), ("sam", 1060))}

# --- GP practices (fictional) ------------------------------------------------
PRACTICES = [
    {"id": "gp:ZGP01", "ods": "ZGP01", "name": "Larchfield Surgery",
     "address": "12 Larchfield Road, Headingley, Leeds", "postcode": "LS6 3HN",
     "lat": 53.8209, "lng": -1.5762, "mode": "online_consultation",
     "online_consultation": "Practice online consultation form (total triage)"},
    {"id": "gp:ZGP02", "ods": "ZGP02", "name": "Brookside Health Centre",
     "address": "3 Brookside Parade, Chapel Allerton, Leeds", "postcode": "LS7 3PD",
     "lat": 53.8287, "lng": -1.5396, "mode": "appointments",
     "online_consultation": "Practice online consultation form"},
    {"id": "gp:ZGP03", "ods": "ZGP03", "name": "Elmtree Practice",
     "address": "88 Elmtree Lane, Roundhay, Leeds", "postcode": "LS8 2LJ",
     "lat": 53.8352, "lng": -1.5008, "mode": "appointments",
     "online_consultation": "Practice online consultation form"},
    {"id": "gp:ZGP04", "ods": "ZGP04", "name": "Ashgrove Medical Centre",
     "address": "40 Ashgrove, Woodhouse, Leeds", "postcode": "LS2 9JT",
     "lat": 53.8105, "lng": -1.5545, "mode": "online_consultation",
     "online_consultation": "Practice online consultation form (total triage)"},
]

# --- Patients (demo PDS + GP record) ------------------------------------------
PATIENTS = [
    {"nhs_number": N["priya"], "name": "Priya Shah", "dob": "1991-03-12", "sex": "F",
     "postcode": "LS6 3HN", "lat": 53.8215, "lng": -1.5770, "area": "Headingley",
     "phone": "07700 900123", "practice_ods": "ZGP01", "insurance": None,
     "linked_profiles": [{"nhs_number": N["arjun"], "relationship": "child"},
                         {"nhs_number": N["maya"], "relationship": "child"}]},
    {"nhs_number": N["arjun"], "name": "Arjun Shah", "dob": "2019-05-02", "sex": "M",
     "postcode": "LS6 3HN", "lat": 53.8215, "lng": -1.5770, "area": "Headingley",
     "phone": None, "practice_ods": "ZGP01", "insurance": None, "linked_profiles": []},
    {"nhs_number": N["maya"], "name": "Maya Shah", "dob": "2011-01-20", "sex": "F",
     "postcode": "LS6 3HN", "lat": 53.8215, "lng": -1.5770, "area": "Headingley",
     "phone": "07700 900124", "practice_ods": "ZGP01", "insurance": None,
     "linked_profiles": []},
    {"nhs_number": N["tom"], "name": "Tom Walker", "dob": "1984-08-09", "sex": "M",
     "postcode": "LS7 4AB", "lat": 53.8296, "lng": -1.5384, "area": "Chapel Allerton",
     "phone": "07700 900456", "practice_ods": "ZGP02",
     "insurance": {"insurer": "NMH", "policy_number": "NMH-448812"},
     "linked_profiles": [{"nhs_number": N["margaret"], "relationship": "parent"}]},
    {"nhs_number": N["margaret"], "name": "Margaret Walker", "dob": "1948-02-14", "sex": "F",
     "postcode": "LS8 2LJ", "lat": 53.8347, "lng": -1.5021, "area": "Roundhay",
     "phone": "07700 900457", "practice_ods": "ZGP03", "insurance": None,
     "linked_profiles": []},
    {"nhs_number": N["sam"], "name": "Sam Okafor", "dob": "1997-11-30", "sex": "M",
     "postcode": "LS2 7EY", "lat": 53.8031, "lng": -1.5479, "area": "Leeds city centre",
     "phone": "07700 900789", "practice_ods": "ZGP04", "insurance": None,
     "linked_profiles": []},
]

# --- Direct-access services (Path A) ------------------------------------------
def pharmacy(i, name, area, address, postcode, lat, lng, pf=True, bookable=False):
    return {"id": f"pharm:ZPH{i:02d}", "ods": f"ZPH{i:02d}", "service_type": "pharmacy",
            "name": f"{name}, {area}", "address": address, "postcode": postcode,
            "lat": lat, "lng": lng, "pharmacy_first": pf, "walk_in": True,
            "bookable": bookable, "confidential": False, "min_age": 0,
            "hours": "Mon-Sat 9am-6pm"}


def clinic(sid, stype, name, address, postcode, lat, lng, *, bookable=True,
           walk_in=False, confidential=False, min_age=0, booking_mode="appointment",
           hours="Mon-Fri 9am-5pm"):
    return {"id": sid, "ods": sid.split(":")[1], "service_type": stype, "name": name,
            "address": address, "postcode": postcode, "lat": lat, "lng": lng,
            "pharmacy_first": False, "walk_in": walk_in, "bookable": bookable,
            "confidential": confidential, "min_age": min_age,
            "booking_mode": booking_mode, "hours": hours}


SERVICES = [
    pharmacy(1, "Larkspur Pharmacy", "Headingley", "21 North Lane, Leeds", "LS6 3HW",
             53.8195, -1.5781, pf=True, bookable=True),
    pharmacy(2, "Brightwell Chemist", "Hyde Park", "5 Brudenell Road, Leeds", "LS6 1HA",
             53.8136, -1.5664, pf=True, bookable=False),
    pharmacy(3, "Kestrel Pharmacy", "Kirkstall", "140 Kirkstall Road, Leeds", "LS4 2AE",
             53.8052, -1.5797, pf=False),
    pharmacy(4, "Juniper Pharmacy", "Chapel Allerton", "9 Harrogate Road, Leeds", "LS7 3NB",
             53.8293, -1.5387, pf=True, bookable=True),
    pharmacy(5, "Fernleigh Pharmacy", "Roundhay", "2 Street Lane, Leeds", "LS8 2ET",
             53.8341, -1.5012, pf=True),
    pharmacy(6, "Oakhurst Pharmacy", "City Centre", "1 Park Square, Leeds", "LS1 2NE",
             53.7967, -1.5468, pf=True, bookable=True),
    pharmacy(7, "Willowbank Pharmacy", "Meanwood", "60 Green Road, Leeds", "LS6 4JP",
             53.8329, -1.5618, pf=True),
    pharmacy(8, "Hollins Pharmacy", "Armley", "77 Town Street, Leeds", "LS12 3HD",
             53.7981, -1.5878, pf=False),
    clinic("sh:ZSH01", "sexual_health", "City Sexual Health Clinic",
           "Merrion Way, Leeds", "LS2 8NG", 53.8016, -1.5441, walk_in=True,
           confidential=True, min_age=13, hours="Mon-Fri 8am-7pm, walk-in 8am-10am"),
    clinic("sh:ZSH02", "sexual_health", "Eastgate Sexual Health Clinic",
           "Seacroft Crescent, Leeds", "LS14 6NX", 53.8181, -1.4583,
           confidential=True, min_age=13),
    clinic("tt:ZTT01", "talking_therapies", "NHS Talking Therapies, West Leeds",
           "Online and phone self-referral", "LS1 4AP", 53.7981, -1.5421,
           bookable=True, confidential=True, min_age=16, booking_mode="self_referral",
           hours="Self-refer online any time; assessment call within 2 weeks"),
    clinic("msk:ZMS01", "msk_physio", "Community MSK Physiotherapy, Woodhouse",
           "St Mark's Street, Leeds", "LS2 9EN", 53.8103, -1.5557, min_age=16),
    clinic("msk:ZMS02", "msk_physio", "Community MSK Physiotherapy, Seacroft",
           "York Road, Leeds", "LS14 6UH", 53.8179, -1.4592, min_age=16),
    clinic("msk:ZMS03", "msk_physio", "Community MSK Physiotherapy, Pudsey",
           "Church Lane, Pudsey", "LS28 7RF", 53.7951, -1.6629, min_age=16),
    clinic("utc:ZUT01", "urgent_treatment_centre", "City Urgent Treatment Centre",
           "Marsh Lane, Leeds", "LS9 8AB", 53.7976, -1.5310, bookable=False, walk_in=True,
           hours="Open 24 hours"),
    clinic("utc:ZUT02", "urgent_treatment_centre", "Wharfedale Urgent Treatment Centre",
           "Newall Carr Road, Otley", "LS21 2LY", 53.9068, -1.6922, bookable=False,
           walk_in=True, hours="8am-10pm"),
    clinic("ae:ZAE01", "emergency_department", "Aire Valley General Hospital A&E",
           "Great George Street, Leeds", "LS1 3EX", 53.8018, -1.5527, bookable=False,
           walk_in=True, hours="Open 24 hours"),
    clinic("ae:ZAE02", "emergency_department", "Calderbrook Royal Infirmary A&E",
           "Aberford Road, Wakefield", "WF1 4DG", 53.6904, -1.4926, bookable=False,
           walk_in=True, hours="Open 24 hours"),
]

# --- e-RS services (NHS-funded first outpatient appointments) ----------------
def ers(sid, name, specialty, lat, lng, wait, postcode, independent=False):
    return {"id": f"ers:{sid}", "service_id": sid, "name": name, "specialty": specialty,
            "lat": lat, "lng": lng, "postcode": postcode, "wait_weeks": wait,
            "independent_sector": independent, "funding": "NHS"}


ERS_SERVICES = [
    ers("S1001", "Aire Valley General Hospital, Orthopaedics", "trauma_orthopaedics",
        53.8018, -1.5527, 18, "LS1 3EX"),
    ers("S1002", "Northgate Orthopaedic Centre (independent sector, NHS-funded)",
        "trauma_orthopaedics", 53.8712, -1.6897, 5, "LS19 7XB", independent=True),
    ers("S1003", "Calderbrook Royal Infirmary, Orthopaedics", "trauma_orthopaedics",
        53.6904, -1.4926, 9, "WF1 4DG"),
    ers("S1004", "Wharfe Valley Hospital, Orthopaedics", "trauma_orthopaedics",
        53.9068, -1.6922, 12, "LS21 2LY"),
    ers("S1005", "Kirkstall Treatment Centre (independent sector, NHS-funded)",
        "trauma_orthopaedics", 53.8160, -1.6040, 7, "LS5 3BT", independent=True),
    ers("S2001", "Aire Valley General Hospital, Eye Clinic", "ophthalmology",
        53.8018, -1.5527, 22, "LS1 3EX"),
    ers("S2002", "Moorside Eye Unit (independent sector, NHS-funded)", "ophthalmology",
        53.7440, -1.6000, 6, "LS27 9BE", independent=True),
    ers("S2003", "Wharfe Valley Hospital, Eye Clinic", "ophthalmology",
        53.9068, -1.6922, 8, "LS21 2LY"),
    ers("S3001", "Aire Valley General Hospital, Dermatology", "dermatology",
        53.8018, -1.5527, 30, "LS1 3EX"),
    ers("S3002", "Pennine Skin Clinic (independent sector, NHS-funded)", "dermatology",
        53.6458, -1.7850, 12, "HD1 2QF", independent=True),
    ers("S4001", "Aire Valley General Hospital, Cardiology", "cardiology",
        53.8018, -1.5527, 14, "LS1 3EX"),
    ers("S4002", "Calderbrook Royal Infirmary, Cardiology", "cardiology",
        53.6904, -1.4926, 7, "WF1 4DG"),
    ers("S5001", "Aire Valley General Hospital, Gastroenterology", "gastroenterology",
        53.8018, -1.5527, 16, "LS1 3EX"),
    ers("S5002", "Calderbrook Royal Infirmary, Gastroenterology", "gastroenterology",
        53.6904, -1.4926, 10, "WF1 4DG"),
]

SPECIALTIES = {
    "trauma_orthopaedics": "Trauma and orthopaedics",
    "ophthalmology": "Ophthalmology",
    "dermatology": "Dermatology",
    "cardiology": "Cardiology",
    "gastroenterology": "Gastroenterology",
}

# --- Referrals already in e-RS (Path B starting points) ----------------------
REFERRALS = [
    {"ubrn": "000100020003", "nhs_number": N["tom"], "specialty": "trauma_orthopaedics",
     "priority": "routine", "source": "nhs_ers", "referrer_ods": "ZGP02",
     "reason": "Knee pain, referred for orthopaedic assessment", "episode_id": None},
    {"ubrn": "000100020004", "nhs_number": N["margaret"], "specialty": "ophthalmology",
     "priority": "routine", "source": "nhs_ers", "referrer_ods": "ZGP03",
     "reason": "Cataract assessment", "episode_id": None},
    {"ubrn": "PRV-2026-0007", "nhs_number": N["tom"], "specialty": "dermatology",
     "priority": "routine", "source": "private", "referrer_ods": "ZGP02",
     "reason": "Open referral letter: skin lesion review", "episode_id": None},
]

# --- Private providers (self-pay prices; insurer networks) -------------------
PRIVATE = [
    {"id": "prv:ZPR01", "name": "Headrow Consulting Rooms, Dermatology", "specialty": "dermatology",
     "lat": 53.8004, "lng": -1.5451, "postcode": "LS1 6PU", "price_gbp": "180.00",
     "wait_weeks": 2, "networks": ["NMH"]},
    {"id": "prv:ZPR02", "name": "Roundhay Private Clinic, Dermatology", "specialty": "dermatology",
     "lat": 53.8361, "lng": -1.5019, "postcode": "LS8 1AA", "price_gbp": "220.00",
     "wait_weeks": 1, "networks": ["NMH"]},
    {"id": "prv:ZPR03", "name": "Harewood Skin and Laser Clinic", "specialty": "dermatology",
     "lat": 53.8978, "lng": -1.5132, "postcode": "LS17 9LF", "price_gbp": "250.00",
     "wait_weeks": 1, "networks": []},
    {"id": "prv:ZPR04", "name": "Headrow Consulting Rooms, Orthopaedics",
     "specialty": "trauma_orthopaedics", "lat": 53.8004, "lng": -1.5451,
     "postcode": "LS1 6PU", "price_gbp": "240.00", "wait_weeks": 1, "networks": ["NMH"]},
    {"id": "prv:ZPR05", "name": "Linton Private Hospital, Orthopaedics",
     "specialty": "trauma_orthopaedics", "lat": 53.9283, "lng": -1.3874,
     "postcode": "LS22 6AA", "price_gbp": "200.00", "wait_weeks": 2, "networks": ["NMH"]},
]

INSURERS = {
    "NMH": {"name": "Northern Mutual Health", "policies": {
        "NMH-448812": {"holder": N["tom"], "status": "active",
                       "covered_specialties": ["dermatology", "trauma_orthopaedics"],
                       "outpatient_cover": "full", "excess_remaining_gbp": "100.00",
                       "needs_preauth": True}}},
}

POSTCODES = {p["postcode"]: [p["lat"], p["lng"]] for p in PATIENTS + PRACTICES + SERVICES}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "patients.json": {"patients": PATIENTS, "practices": PRACTICES},
        "services.json": {"services": SERVICES},
        "ers.json": {"specialties": SPECIALTIES, "services": ERS_SERVICES,
                     "referrals": REFERRALS},
        "private.json": {"providers": PRIVATE, "insurers": INSURERS},
        "postcodes.json": POSTCODES,
    }
    for name, body in files.items():
        (OUT / name).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        print("wrote", OUT / name)
    for k, v in N.items():
        print(f"  {k:9s} {v}")


if __name__ == "__main__":
    main()
