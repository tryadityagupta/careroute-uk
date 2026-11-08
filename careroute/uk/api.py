"""
uk/api.py — the UK CareRoute HTTP routes.

    POST /chat                  new message; JSON, or SSE with ?stream=1
    POST /chat/resume           answer a paused interrupt (option, limits, confirm)
    GET  /chat/{thread_id}      history + current pause (reloads, other devices)
    POST /referrals/events      a referral arrives (e-RS): starts a Path B thread
    GET  /me                    who I am, who I can act for
    GET  /inbox                 referral threads I can act on
    GET  /bookings, /audit      what was booked, and every action taken for me
    GET/POST /consents/{token}  view / approve / decline a one-time consent link
    /demo/*                     demo-only helpers (CAREROUTE_DEMO_TOOLS=1)

Identity comes from NHS login (or demo headers) and is resolved here, before
anything else runs.
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from careroute.uk.identity import IdentityError

ROUTES_NEEDING_STREAM = ("/chat", "/chat/resume")


class ChatBody(BaseModel):
    message: str
    thread_id: str | None = None
    lat: float | None = None
    lng: float | None = None
    location_text: str | None = None


class ResumeBody(BaseModel):
    thread_id: str
    resume: dict = {}


class ReferralEvent(BaseModel):
    ubrn: str


class GpOutcome(BaseModel):
    thread_id: str
    specialty: str = "trauma_orthopaedics"


class Faults(BaseModel):
    next_booking: str | None = None      # "conflict" | "error" | None


def build_router(c, guards: list) -> APIRouter:
    """c: the Container. guards: rate limit + optional API key."""
    s = c.settings
    r = APIRouter()

    async def requester(request: Request) -> str | None:
        try:
            return await c.uk_identity.resolve(request.headers)
        except IdentityError as e:
            raise HTTPException(401, str(e))

    async def signed_in(who: str | None = Depends(requester)) -> str:
        if not who:
            raise HTTPException(401, "Sign in with NHS login first.")
        return who

    def wants_stream(request: Request, stream: int) -> bool:
        return bool(stream) or "text/event-stream" in request.headers.get("accept", "")

    async def respond(events, stream: bool):
        svc = c.journey_service
        if not stream:
            return await svc.collect(events)
        try:
            first = await events.__anext__()            # validation errors surface here
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

        async def sse():
            yield f"data: {json.dumps(first)}\n\n"
            try:
                async for ev in events:
                    yield f"data: {json.dumps(ev)}\n\n"
            except Exception as e:                       # tell the page, don't hang it
                print(f"[uk] stream failed: {type(e).__name__}: {str(e)[:200]}")
                yield "data: " + json.dumps({
                    "type": "error", "detail": "Something went wrong. If this is an "
                    "emergency, call 999. Otherwise try again, or use 111.nhs.uk."}) + "\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @r.post("/chat", dependencies=guards)
    async def chat(body: ChatBody, request: Request, stream: int = 0,
                   who: str | None = Depends(requester)):
        events = c.journey_service.stream_chat(who, message=body.message,
                                               thread_id=body.thread_id, lat=body.lat,
                                               lng=body.lng, location_text=body.location_text)
        return await respond(events, wants_stream(request, stream))

    @r.post("/chat/resume", dependencies=guards)
    async def resume(body: ResumeBody, request: Request, stream: int = 0,
                     who: str | None = Depends(requester)):
        events = c.journey_service.stream_resume(who, thread_id=body.thread_id, value=body.resume)
        return await respond(events, wants_stream(request, stream))

    @r.get("/chat/{thread_id}")
    async def thread(thread_id: str, who: str | None = Depends(requester)):
        return await c.journey_service.thread_view(who, thread_id)

    @r.post("/referrals/events")
    async def referral_event(body: ReferralEvent, x_event_key: str | None = Header(None)):
        if s.referral_event_key and not hmac.compare_digest(x_event_key or "",
                                                            s.referral_event_key):
            raise HTTPException(401, "Bad event key.")
        if not s.referral_event_key and not s.demo_tools:
            raise HTTPException(403, "Set CAREROUTE_REFERRAL_EVENT_KEY to accept referral events.")
        return await c.journey_service.start_referral(body.ubrn)

    @r.get("/me")
    async def me(who: str = Depends(signed_in)):
        rec = c.uk_records.get(who)
        practice = c.uk_records.practice(rec.get("practice_ods")) or {}
        grants = await c.uk_consents.active_grants(who)
        return {"nhs_number": who, "name": rec["name"], "first_name": rec["first_name"],
                "age": rec["age"], "area": rec["area"], "practice": practice.get("name"),
                "insurance": bool(rec.get("insurance")),
                "linked_profiles": c.uk_records.linked_profiles(who),
                "consent_grants": [{"label": g["label"], "expires": g["expires"]} for g in grants]}

    @r.get("/inbox")
    async def inbox(who: str = Depends(signed_in)):
        out = []
        for pid in await c.uk_authority.actable_patients(who):
            for tid in dict.fromkeys(await c.uk_store.items("patient_threads", pid)):
                meta = await c.journey_service.meta(tid)
                if meta:
                    view = await c.journey_service.thread_view(who, tid)
                    out.append({"thread_id": tid, "ubrn": meta.get("ubrn"),
                                "specialty": meta.get("specialty"), "for": view["for"],
                                "waiting_for": (view["pending"] or {}).get("type"),
                                "outcome": view["outcome"]})
        return {"threads": out}

    async def visible_bookings(who: str) -> list[dict]:
        out = []
        for pid in await c.uk_authority.actable_patients(who):
            rec = c.uk_records.get(pid) or {}
            for b in await c.uk_store.items("booking", pid):
                if b.get("confidential") and pid != who and rec.get("age", 0) >= 13:
                    continue                    # a parent never sees a teen's confidential care
                out.append(b)
        return out

    @r.get("/bookings")
    async def bookings(who: str = Depends(signed_in)):
        return {"bookings": await visible_bookings(who)}

    @r.get("/audit")
    async def audit(who: str = Depends(signed_in)):
        entries = await c.uk_store.items("audit", who)
        return {"entries": [e for e in entries
                            if not (e.get("confidential") and who not in (e.get("patient"), e.get("actor")))]}

    @r.get("/consents/{token}")
    async def consent_view(token: str, who: str = Depends(signed_in)):
        inv = await c.uk_consents.get(token)
        if inv is None:
            raise HTTPException(404, "This link has expired or does not exist.")
        requester = c.uk_records.get(inv["requester"]) or {}
        return {"token": token, "status": inv["status"], "label": inv["label"],
                "requester_first_name": requester.get("first_name"),
                "scope": "one booking, within 24 hours", "expires": inv["expires"],
                "is_requester": who == inv["requester"]}

    @r.post("/consents/{token}/approve")
    async def consent_approve(token: str, who: str = Depends(signed_in)):
        try:
            inv = await c.uk_consents.approve(token, who)
        except ValueError as e:
            raise HTTPException(409, str(e))
        await c.uk_store.append("audit", who, {
            "ts": c.uk_clock.now().isoformat(), "actor": who, "patient": who,
            "action": "approved a one-time consent link",
            "detail": f"for {(c.uk_records.get(inv['requester']) or {}).get('first_name')} "
                      "to book once for you", "confidential": False})
        return {"status": inv["status"], "expires": inv["expires"]}

    @r.post("/consents/{token}/decline")
    async def consent_decline(token: str, who: str = Depends(signed_in)):
        try:
            inv = await c.uk_consents.decline(token, who)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"status": inv["status"]}

    # ---- demo helpers ---------------------------------------------------
    if s.demo_tools:
        @r.get("/demo/users")
        async def demo_users():
            return {"login_mode": c.uk_identity.mode, "users": [
                {"nhs_number": p["nhs_number"], "name": p["name"], "age": p["age"],
                 "area": p["area"],
                 "can_act_for": [f"{lp['first_name']} ({lp['relationship']})"
                                 for lp in c.uk_records.linked_profiles(p["nhs_number"])]}
                for p in c.uk_records.all_patients()]}

        @r.post("/demo/referral/{ubrn}")
        async def demo_referral(ubrn: str):
            return await c.journey_service.start_referral(ubrn)

        @r.post("/demo/gp-outcome")
        async def demo_gp_outcome(body: GpOutcome, who: str = Depends(signed_in)):
            """Simulate the GP's decision after a Path A request: an e-RS
            referral carrying the same episode id, which starts Path B."""
            await c.journey_service.check_access(who, body.thread_id)
            state = await (await c.journey_service.graph()).aget_state(
                {"configurable": {"thread_id": body.thread_id}})
            ep_id = (state.values or {}).get("episode_id")
            ep = await c.uk_store.get("episode", ep_id) if ep_id else None
            if ep is None:
                raise HTTPException(409, "This conversation has no GP request to refer from.")
            if body.specialty not in c.uk_ers.specialties:
                raise HTTPException(422, f"specialty must be one of {list(c.uk_ers.specialties)}")
            ref = await c.uk_ers.create_referral(
                nhs_number=ep["patient_id"], specialty=body.specialty,
                referrer_ods=ep["practice_ods"], reason="Referred after GP review",
                episode_id=ep_id)
            return await c.journey_service.start_referral(ref["ubrn"])

        @r.post("/demo/faults")
        async def demo_faults(body: Faults):
            if body.next_booking in ("conflict", "error"):
                await c.uk_store.put("fault", "next_booking", body.next_booking, ttl_s=3600)
            else:
                await c.uk_store.delete("fault", "next_booking")
            return {"next_booking": body.next_booking}

        @r.get("/demo/outbox")
        async def demo_outbox():
            return {"messages": list(reversed(await c.uk_store.items("outbox", "all")))}

    return r
