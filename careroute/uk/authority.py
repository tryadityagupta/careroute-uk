"""
uk/authority.py — who may act for whom. Decided in code, from records.

Three sources of authority, strongest first:
  self     the NHS login user is the patient;
  proxy    a linked profile from the GP record (a child, or a parent who
           agreed to proxy access at their practice);
  consent  a friend approved a one-time link, signed in as themselves:
           one booking, for a limited time.

Nothing a person TYPES grants authority. "I'm her carer" changes nothing;
only records and approved links do. The identified subject (from the LLM)
is a candidate that this module confirms or rejects.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from careroute.uk.clock import Clock
from careroute.uk.records import NhsRecords
from careroute.uk.store import DAY, JourneyStore

FAMILY = {"child", "parent", "partner", "grandparent", "grandchild", "sibling", "family"}
CONSENT_TTL_S = DAY


def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0].lower()


class ConsentService:
    NS = "consent"

    def __init__(self, store: JourneyStore, clock: Clock):
        self.store = store
        self.clock = clock

    async def invite(self, requester: str, label: str) -> dict:
        """A pending one-time link. Reuses a live pending invite for the same
        person, so re-asking doesn't mint a new link every turn."""
        index_key = f"pending:{requester}:{_first(label) or 'friend'}"
        existing = await self.store.get(self.NS, index_key)
        if existing:
            inv = await self.store.get(self.NS, existing)
            if inv and inv["status"] == "pending":
                return inv
        token = secrets.token_urlsafe(12)
        inv = {"token": token, "requester": requester, "label": label or "your friend",
               "status": "pending", "scope": "one_booking",
               "created": self.clock.now().isoformat(),
               "expires": (self.clock.now() + timedelta(seconds=CONSENT_TTL_S)).isoformat(),
               "patient": None}
        await self.store.put(self.NS, token, inv, ttl_s=CONSENT_TTL_S)
        await self.store.put(self.NS, index_key, token, ttl_s=CONSENT_TTL_S)
        await self.store.append(self.NS, f"by:{requester}", token, ttl_s=CONSENT_TTL_S)
        return inv

    async def get(self, token: str) -> dict | None:
        inv = await self.store.get(self.NS, token)
        if inv and inv["expires"] < self.clock.now().isoformat():
            return None
        return inv

    async def approve(self, token: str, approver: str) -> dict:
        inv = await self.get(token)
        if inv is None:
            raise ValueError("This consent link has expired or does not exist.")
        if inv["status"] != "pending":
            raise ValueError("This consent link has already been used.")
        if approver == inv["requester"]:
            raise ValueError("You can't approve your own request.")
        inv.update(status="approved", patient=approver,
                   approved=self.clock.now().isoformat(),
                   expires=(self.clock.now() + timedelta(seconds=CONSENT_TTL_S)).isoformat())
        await self.store.put(self.NS, token, inv, ttl_s=CONSENT_TTL_S)
        return inv

    async def decline(self, token: str, approver: str) -> dict:
        inv = await self.get(token)
        if inv is None or inv["status"] != "pending" or approver == inv["requester"]:
            raise ValueError("This consent link can't be declined.")
        inv.update(status="declined")
        await self.store.put(self.NS, token, inv, ttl_s=CONSENT_TTL_S)
        return inv

    async def active_grants(self, requester: str) -> list[dict]:
        out = []
        for token in await self.store.items(self.NS, f"by:{requester}"):
            inv = await self.get(token)
            if inv and inv["status"] == "approved":
                out.append(inv)
        return out

    async def consume(self, token: str) -> None:
        inv = await self.get(token)
        if inv and inv["status"] == "approved":
            inv["status"] = "used"
            await self.store.put(self.NS, token, inv, ttl_s=CONSENT_TTL_S)


class AuthorityService:
    def __init__(self, records: NhsRecords, consents: ConsentService):
        self.records = records
        self.consents = consents

    @staticmethod
    def _verified(kind: str, patient: dict, relationship: str, source: str, **extra) -> dict:
        return {"status": "verified", "type": kind, "patient_id": patient["nhs_number"],
                "patient_first_name": patient["first_name"], "relationship": relationship,
                "source": source, **extra}

    async def decide(self, requester: str | None, subject: dict | None,
                     *, create_invite: bool = True) -> dict:
        """Confirm or reject the candidate subject. Returns an authority dict
        with status verified | ambiguous | needs_consent | unauthenticated |
        unknown_requester."""
        if not requester:
            return {"status": "unauthenticated"}
        me = self.records.get(requester)
        if me is None:
            return {"status": "unknown_requester"}
        subject = subject or {"relationship": "self"}
        rel = (subject.get("relationship") or "self").lower()
        name = _first(subject.get("name"))
        if rel == "self" or (name and name == _first(me["name"]) and rel not in FAMILY):
            return self._verified("self", me, "self", "NHS login")

        links = self.records.linked_profiles(requester)
        candidates = links
        if name:
            candidates = [p for p in links if p["first_name"].lower() == name]
        elif rel in FAMILY:
            candidates = [p for p in links if p["relationship"] == rel]
        else:
            candidates = []
        if len(candidates) == 1:
            p = self.records.get(candidates[0]["nhs_number"])
            return self._verified("proxy", p, candidates[0]["relationship"], "GP proxy access")
        if len(candidates) > 1:
            return {"status": "ambiguous", "relationship": rel,
                    "choices": [p["first_name"] for p in candidates]}

        grants = await self.consents.active_grants(requester)
        if name:
            grants = [g for g in grants if _first(g["label"]) == name]
        if len(grants) == 1:
            p = self.records.get(grants[0]["patient"])
            if p:
                return self._verified("consent", p, rel if rel != "self" else "friend",
                                      "One-time consent link", grant_token=grants[0]["token"])
        if len(grants) > 1:
            return {"status": "ambiguous", "relationship": rel,
                    "choices": [g["label"] for g in grants]}

        out = {"status": "needs_consent", "relationship": rel,
               "label": subject.get("name") or ("your " + rel if rel not in ("other",) else "them")}
        if rel in FAMILY and not name:
            out["no_proxy_link"] = True
        if create_invite:
            inv = await self.consents.invite(requester, subject.get("name") or "")
            out["invite_token"] = inv["token"]
        return out

    async def can_act_for(self, requester: str | None, patient_id: str | None) -> dict | None:
        """Authority over a SPECIFIC patient (threads, bookings, audit)."""
        if not requester or not patient_id:
            return None
        if requester == patient_id:
            me = self.records.get(requester)
            return self._verified("self", me, "self", "NHS login") if me else None
        for p in self.records.linked_profiles(requester):
            if p["nhs_number"] == patient_id:
                return self._verified("proxy", self.records.get(patient_id),
                                      p["relationship"], "GP proxy access")
        for g in await self.consents.active_grants(requester):
            if g["patient"] == patient_id:
                return self._verified("consent", self.records.get(patient_id), "friend",
                                      "One-time consent link", grant_token=g["token"])
        return None

    async def actable_patients(self, requester: str) -> list[str]:
        ids = [requester] + [p["nhs_number"] for p in self.records.linked_profiles(requester)]
        ids += [g["patient"] for g in await self.consents.active_grants(requester)]
        return list(dict.fromkeys(ids))
