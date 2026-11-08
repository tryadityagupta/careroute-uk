"""
uk/integrations.py — the external systems a booking touches.

    system               production integration              demo class
    -------------------  ----------------------------------  -----------------
    referrals            NHS e-Referral Service (e-RS) API    DemoErs
    GP appointments      GP system via IM1 / GP Connect       DemoGpSystem
    direct-access and    the provider's booking API           DemoProviderBooking
      private bookings
    insurance            insurer eligibility / pre-auth API   DemoInsurer
    payments             payment provider (e.g. Stripe)       DemoPayments
    SMS                  GOV.UK Notify / NHS Notify           GovUkNotify | DemoOutbox

The demo classes behave like the real APIs where it matters for the design:
  * IDEMPOTENCY: every write takes an Idempotency-Key. The same key returns
    the same booking — a retried request never double-books or double-charges.
  * CONFLICTS: a slot held by someone else raises SlotTaken (HTTP 409), so
    the graph's "slot taken -> pick another time" loop is real.
  * FAILURES: BookingFailed (5xx/timeout) is distinct from SlotTaken; the
    patient is told plainly that nothing was booked.
Slot holds live in the JourneyStore, so they are consistent across replicas.
A demo fault switch (POST /demo/faults) forces the next booking to conflict
or fail, for showing those paths live.

Money is Decimal, quantised to pence, and only ever computed here (from the
insurer's terms) — never by the model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

from careroute.uk.clock import Clock
from careroute.uk.store import DAY, JourneyStore

PENNY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(PENNY, rounding=ROUND_HALF_UP)


class IntegrationError(Exception):
    pass


class SlotTaken(IntegrationError):
    pass


class BookingFailed(IntegrationError):
    pass


def _ref(prefix: str, key: str) -> str:
    return prefix + "-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper()


class DemoBookingCore:
    """Slot generation and idempotent, conflict-aware holds, shared by every
    demo system."""

    def __init__(self, store: JourneyStore, clock: Clock):
        self.store = store
        self.clock = clock

    def working_days(self, start: date, n: int) -> list[date]:
        out, d = [], start
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def local_now(self) -> str:
        """Now in UK local time, as 'YYYY-MM-DDTHH:MM' (slot times are local)."""
        try:
            from zoneinfo import ZoneInfo
            now = self.clock.now().astimezone(ZoneInfo("Europe/London"))
        except Exception:                      # no tz database (Windows without tzdata)
            now = self.clock.now()
        return now.strftime("%Y-%m-%dT%H:%M")

    async def slots(self, system: str, key: str, *, first_day: date, days: int,
                    times: tuple[str, ...]) -> list[dict]:
        out, now = [], self.local_now()
        for d in self.working_days(first_day, days):
            for t in times:
                start = f"{d.isoformat()}T{t}"
                sid = f"{system}|{key}|{start}"
                if start > now and await self.store.get("slot", sid) is None:
                    out.append({"slot_id": sid, "start": start})
        return out

    async def _fault(self) -> str | None:
        f = await self.store.get("fault", "next_booking")
        if f:
            await self.store.delete("fault", "next_booking")
        return f

    async def hold(self, system: str, slot_id: str, idem_key: str, booking: dict) -> dict:
        """Book a slot exactly once per idempotency key."""
        done = await self.store.get("idem", f"{system}|{idem_key}")
        if done:
            return done                                # retry: same answer
        fault = await self._fault()
        if fault == "error":
            raise BookingFailed("Provider booking system returned 503.")
        if fault == "conflict" or not slot_id.startswith(system + "|"):
            raise SlotTaken("That time was just taken.")
        if not await self.store.put_if_absent("slot", slot_id, idem_key, ttl_s=30 * DAY):
            if await self.store.get("slot", slot_id) != idem_key:
                raise SlotTaken("That time was just taken.")
        booking = {**booking, "booking_ref": _ref("BK", idem_key), "slot_id": slot_id,
                   "start": slot_id.split("|")[-1],
                   "created": self.clock.now().isoformat()}
        await self.store.put("idem", f"{system}|{idem_key}", booking, ttl_s=7 * DAY)
        return booking

    async def once(self, system: str, idem_key: str, result: dict) -> dict:
        """Idempotent write with no slot (form submissions)."""
        done = await self.store.get("idem", f"{system}|{idem_key}")
        if done:
            return done
        fault = await self._fault()
        if fault == "error":
            raise BookingFailed("Receiving system returned 503.")
        await self.store.put("idem", f"{system}|{idem_key}", result, ttl_s=7 * DAY)
        return result


# --- NHS e-Referral Service --------------------------------------------------
class DemoErs:
    """Stands in for the e-RS API: referral lookup by UBRN, the services a
    patient may choose from (with waiting times), slots, and booking."""
    SYSTEM = "ers"

    def __init__(self, data: dict, core: DemoBookingCore):
        self.specialties: dict = data["specialties"]
        self._services = {s["service_id"]: s for s in data["services"]}
        self._referrals = {r["ubrn"]: r for r in data["referrals"]}
        self.core = core

    async def get_referral(self, ubrn: str) -> dict | None:
        r = self._referrals.get(ubrn) or await self.core.store.get("ers_ref", ubrn)
        if r is None:
            return None
        return {**r, "specialty_label": self.specialties.get(r["specialty"], r["specialty"])}

    async def create_referral(self, *, nhs_number: str, specialty: str, referrer_ods: str,
                              reason: str, episode_id: str | None, source: str = "nhs_ers",
                              priority: str = "routine") -> dict:
        """Demo only: what a GP's referral would create. Real UBRNs are issued by e-RS."""
        ubrn = str(int(hashlib.sha256((episode_id or reason + nhs_number).encode())
                       .hexdigest()[:10], 16) % 10**12).zfill(12)
        ref = {"ubrn": ubrn, "nhs_number": nhs_number, "specialty": specialty,
               "priority": priority, "source": source, "referrer_ods": referrer_ods,
               "reason": reason, "episode_id": episode_id}
        await self.core.store.put("ers_ref", ubrn, ref, ttl_s=90 * DAY)
        return await self.get_referral(ubrn)

    def services(self, specialty: str) -> list[dict]:
        return [dict(s) for s in self._services.values() if s["specialty"] == specialty]

    async def slots(self, ubrn: str, service_id: str) -> list[dict]:
        s = self._services.get(service_id)
        if s is None:
            return []
        first = self.core.clock.today() + timedelta(weeks=s["wait_weeks"])
        return await self.core.slots(self.SYSTEM, service_id, first_day=first, days=3,
                                     times=("09:20", "11:40", "14:10"))

    async def book(self, ubrn: str, service_id: str, slot_id: str, idem_key: str) -> dict:
        return await self.core.hold(self.SYSTEM, slot_id, idem_key,
                                    {"system": "e-RS", "ubrn": ubrn, "service_id": service_id})


# --- GP practice systems (IM1-style patient-facing services) -----------------
class DemoGpSystem:
    """Practices either expose bookable appointments (IM1 patient-facing
    services) or take requests through an online consultation form (total
    triage). Both are writes on the patient's behalf, so both need consent."""
    SYSTEM = "gp"

    def __init__(self, core: DemoBookingCore):
        self.core = core

    async def slots(self, ods: str) -> list[dict]:
        first = self.core.clock.today() + timedelta(days=1)
        return await self.core.slots(self.SYSTEM, ods, first_day=first, days=2,
                                     times=("08:40", "10:10", "15:30"))

    async def book(self, ods: str, slot_id: str, nhs_number: str, idem_key: str) -> dict:
        return await self.core.hold(self.SYSTEM, slot_id, idem_key,
                                    {"system": "GP (IM1)", "ods": ods})

    async def submit_online_consultation(self, ods: str, nhs_number: str, summary: str,
                                         idem_key: str) -> dict:
        return await self.core.once(self.SYSTEM + "-oc", idem_key, {
            "system": "GP online consultation", "ods": ods,
            "booking_ref": _ref("OC", idem_key), "start": None,
            "created": self.core.clock.now().isoformat(),
            "next_step": "The practice will review your request and contact you, "
                         "usually by the end of the next working day."})


# --- Direct-access services and private providers ----------------------------
class DemoProviderBooking:
    SYSTEM = "prov"
    WINDOWS = {   # service_type -> (days from today, number of days, times)
        "pharmacy": (0, 2, ("11:00", "14:30", "16:15")),
        "sexual_health": (1, 3, ("09:30", "13:00", "16:40")),
        "msk_physio": (5, 3, ("09:00", "12:15", "15:45")),
    }

    def __init__(self, core: DemoBookingCore):
        self.core = core

    async def slots(self, provider_id: str, service_type: str, wait_weeks: int | None = None):
        if wait_weeks is not None:                       # private providers
            first, days, times = self.core.clock.today() + timedelta(weeks=wait_weeks), 3, \
                ("10:00", "13:30", "17:00")
        else:
            off, days, times = self.WINDOWS.get(service_type, (1, 3, ("10:00", "14:00")))
            first = self.core.clock.today() + timedelta(days=off)
        return await self.core.slots(self.SYSTEM, provider_id, first_day=first, days=days,
                                     times=times)

    async def book(self, provider_id: str, slot_id: str, idem_key: str, **extra) -> dict:
        return await self.core.hold(self.SYSTEM, slot_id, idem_key,
                                    {"system": "provider", "provider_id": provider_id, **extra})

    async def self_refer(self, provider_id: str, nhs_number: str, idem_key: str) -> dict:
        return await self.core.once(self.SYSTEM + "-sr", idem_key, {
            "system": "self-referral", "provider_id": provider_id,
            "booking_ref": _ref("SR", idem_key), "start": None,
            "created": self.core.clock.now().isoformat(),
            "next_step": "The service will contact you to arrange an assessment."})


# --- Insurance ---------------------------------------------------------------
class DemoInsurer:
    """The insurer, not CareRoute, says what a patient pays. CareRoute only
    compares that figure with the patient's limit."""

    def __init__(self, data: dict, core: DemoBookingCore):
        self._insurers = data["insurers"]
        self.core = core

    def _policy(self, insurer: str, policy_number: str) -> dict | None:
        return (self._insurers.get(insurer) or {}).get("policies", {}).get(policy_number)

    async def quote(self, insurance: dict | None, *, patient_id: str, provider: dict) -> dict:
        price = money(provider["price_gbp"])
        self_pay = {"funding": "self-pay", "covered": False, "price_gbp": str(price),
                    "insurer_pays_gbp": "0.00", "patient_pays_gbp": str(price),
                    "needs_preauth": False}
        if not insurance:
            return {**self_pay, "note": "No insurance on record: self-pay price."}
        pol = self._policy(insurance["insurer"], insurance["policy_number"])
        if not pol or pol.get("status") != "active" or pol.get("holder") != patient_id:
            return {**self_pay, "note": "Policy not active for this patient: self-pay price."}
        if provider["specialty"] not in pol["covered_specialties"]:
            return {**self_pay, "note": "Not covered by the policy: self-pay price."}
        if insurance["insurer"] not in provider.get("networks", []):
            return {**self_pay, "note": "Outside the insurer's network: self-pay price."}
        patient_pays = min(price, money(pol["excess_remaining_gbp"]))
        return {"funding": "insurance", "covered": True, "price_gbp": str(price),
                "insurer_pays_gbp": str(price - patient_pays),
                "patient_pays_gbp": str(patient_pays),
                "needs_preauth": bool(pol.get("needs_preauth")),
                "insurer_name": self._insurers[insurance["insurer"]]["name"],
                "note": "Covered after your remaining excess."}

    async def preauthorise(self, insurance: dict, provider_id: str, idem_key: str) -> dict:
        return await self.core.once("insurer", idem_key, {
            "preauth_ref": _ref("PA", idem_key), "policy": insurance["policy_number"],
            "provider_id": provider_id})


class DemoPayments:
    def __init__(self, core: DemoBookingCore):
        self.core = core

    async def charge(self, amount_gbp: str, idem_key: str, description: str) -> dict | None:
        amount = money(amount_gbp)
        if amount == 0:
            return None
        return await self.core.once("pay", idem_key, {
            "payment_ref": _ref("PAY", idem_key), "amount_gbp": str(amount),
            "description": description})


def load_json(data_dir: Path, name: str) -> dict:
    return json.loads((data_dir / "uk" / name).read_text(encoding="utf-8"))


# --- SMS ---------------------------------------------------------------------
class DemoOutbox:
    """Demo SMS: stored, shown at GET /demo/outbox. Idempotent per reference."""

    def __init__(self, store: JourneyStore, clock: Clock):
        self.store = store
        self.clock = clock

    async def send_sms(self, phone: str, text: str, fields: dict, reference: str) -> str:
        if not await self.store.put_if_absent("sms", reference, True, ttl_s=7 * DAY):
            return "already_sent"
        await self.store.append("outbox", "all", {
            "to": phone, "text": text, "reference": reference,
            "sent": self.clock.now().isoformat()}, max_len=200, ttl_s=7 * DAY)
        return "sent"


class GovUkNotify:
    """GOV.UK Notify SMS. The template must use ((when)) and ((reference))
    (and may use ((first_name))); no health details are ever sent."""
    URL = "https://api.notifications.service.gov.uk/v2/notifications/sms"

    def __init__(self, api_key: str, template_id: str, store: JourneyStore,
                 client: httpx.AsyncClient | None = None):
        # key format: <key_name>-<service_id (uuid)>-<secret (uuid)>
        self.service_id = api_key[-73:-37]
        self.secret = api_key[-36:]
        self.template_id = template_id
        self.store = store
        self._http = client or httpx.AsyncClient()

    def _token(self) -> str:
        import time

        import jwt
        return jwt.encode({"iss": self.service_id, "iat": int(time.time())},
                          self.secret, algorithm="HS256")

    async def send_sms(self, phone: str, text: str, fields: dict, reference: str) -> str:
        if not await self.store.put_if_absent("sms", reference, True, ttl_s=7 * DAY):
            return "already_sent"
        try:
            r = await self._http.post(self.URL, timeout=5, json={
                "phone_number": phone, "template_id": self.template_id,
                "personalisation": fields, "reference": reference},
                headers={"Authorization": f"Bearer {self._token()}"})
            r.raise_for_status()
            return "sent"
        except httpx.HTTPError as e:
            await self.store.delete("sms", reference)        # allow a retry
            print(f"[notify] GOV.UK Notify failed: {str(e)[:120]}")
            return "failed"


def fmt_start(start: str | None) -> str:
    """'2026-10-14T09:20' -> 'Wed 14 Oct, 9:20am'."""
    if not start:
        return ""
    dt = datetime.fromisoformat(start)
    hour = dt.strftime("%I").lstrip("0")
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {hour}:{dt.strftime('%M%p').lower()}"
