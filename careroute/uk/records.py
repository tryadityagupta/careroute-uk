"""
uk/records.py — NHS records, read-only.

Stands in for two national systems:
  * PDS (Personal Demographics Service): name, date of birth, address,
    registered GP practice;
  * the GP record's proxy access ("linked profiles" in the NHS App): who a
    person may act for, set up at their GP practice.

The agent never writes a record. Proxy links come ONLY from here, never from
what someone types — "I'm her carer" in a chat message grants nothing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from careroute.uk.clock import Clock, age_on
from careroute.uk.nhs_number import is_valid, normalise


class NhsRecords:
    def __init__(self, patients: list[dict], practices: list[dict], clock: Clock):
        self._patients = {p["nhs_number"]: p for p in patients}
        self._practices = {p["ods"]: p for p in practices}
        self.clock = clock

    @classmethod
    def from_dir(cls, data_dir: Path, clock: Clock) -> "NhsRecords":
        body = json.loads((data_dir / "uk" / "patients.json").read_text(encoding="utf-8"))
        return cls(body["patients"], body["practices"], clock)

    def get(self, nhs_number: str | None) -> dict | None:
        n = normalise(nhs_number)
        if not is_valid(n):
            return None
        rec = self._patients.get(n)
        if rec is None:
            return None
        out = copy.deepcopy(rec)
        out["age"] = age_on(rec["dob"], self.clock.today())
        out["first_name"] = rec["name"].split()[0]
        return out

    def practice(self, ods: str | None) -> dict | None:
        p = self._practices.get(ods or "")
        return copy.deepcopy(p) if p else None

    def linked_profiles(self, nhs_number: str) -> list[dict]:
        """[{nhs_number, name, first_name, relationship, age}] this person may act for."""
        rec = self._patients.get(normalise(nhs_number))
        out = []
        for link in (rec or {}).get("linked_profiles", []):
            other = self.get(link["nhs_number"])
            if other:
                out.append({"nhs_number": other["nhs_number"], "name": other["name"],
                            "first_name": other["first_name"], "age": other["age"],
                            "relationship": link["relationship"],
                            "source": "GP proxy access"})
        return out

    def all_patients(self) -> list[dict]:
        return [self.get(n) for n in self._patients]
