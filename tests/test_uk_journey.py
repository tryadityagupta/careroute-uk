"""
tests/test_uk_journey.py — the journey graph, end to end, offline.

No API key, no network. The single-shot LLM steps are the offline rules (or a
stub that FAILS if called), and the route agent's model is either the offline
model or a ScriptedModel replaying exact tool calls. Real demo data, real
graph, real interrupts and resumes, real idempotent booking.

With REDIS_URL + DATABASE_URL set (CI does both) the same tests run on Redis
and the Postgres checkpointer.
"""

import pytest
from langchain_core.messages import AIMessage

from careroute.uk.store import MemoryJourneyStore
from tests.fakes import (ARJUN, MARGARET, MAYA, PRIYA, SAM, TOM, NoLlmSteps, ScriptedModel,
                         call, close_containers, make_uk_container, uk_chat, uk_resume)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_containers()


async def bookings(c, pid):
    return await c.uk_store.items("booking", pid)


# --- safety first ------------------------------------------------------------------
async def test_emergency_rule_answers_before_login_and_without_a_model():
    c = make_uk_container(steps=NoLlmSteps())
    r = await uk_chat(c, None, "crushing chest pain and sweating", lat=53.80, lng=-1.55)
    assert r["outcome"] == "emergency" and r["answer"].startswith("Call 999 now")
    assert "Aire Valley General Hospital A&E" in r["answer"]         # nearest A&E, from records
    assert r["llm_calls"] == 0 and r["pending"] is None


async def test_model_can_escalate_but_never_downgrade():
    class Escalating(NoLlmSteps):
        async def identify_subject(self, text, linked):
            return {"relationship": "self", "name": None}

        async def triage(self, text):
            return {"level": "emergency", "red_flags": ["Sudden severe headache"], "reason": ""}

    c = make_uk_container(steps=Escalating())
    r = await uk_chat(c, PRIYA, "worst headache of my life, came on suddenly")
    assert r["outcome"] == "emergency" and "999" in r["answer"]

    class Calm(Escalating):
        async def triage(self, text):
            return {"level": "routine", "red_flags": [], "reason": "fine"}

    c2 = make_uk_container(steps=Calm())
    r2 = await uk_chat(c2, PRIYA, "I broke my wrist falling off my bike")
    assert r2["outcome"] == "urgent" and "111" in r2["answer"]    # rule wins over the model


async def test_mental_health_crisis_gets_the_right_numbers():
    c = make_uk_container(steps=NoLlmSteps())
    r = await uk_chat(c, PRIYA, "I have taken an overdose")
    assert "999" in r["answer"] and "116 123" in r["answer"]


# --- permission ------------------------------------------------------------------------
async def test_not_signed_in_gets_information_only():
    c = make_uk_container(route_model=ScriptedModel([]))      # route agent must not run
    r = await uk_chat(c, None, "sore throat for three days")
    assert r["outcome"] == "sign_in_needed" and "NHS login" in r["answer"]


async def test_two_children_means_ask_which_one():
    c = make_uk_container()
    r = await uk_chat(c, PRIYA, "my son has earache")
    assert r["outcome"] == "needs_clarification" and "Arjun or Maya" in r["answer"]


async def test_proxy_for_a_child_uses_the_childs_age():
    c = make_uk_container()
    r = await uk_chat(c, PRIYA, "Arjun has had earache since last night")
    assert r["care_route"] == "pharmacy_first"               # otitis: ages 1 to 17
    assert r["for"] == "Arjun" and r["pending"]["type"] == "choose_option"


async def test_parent_cannot_book_a_teenagers_confidential_service():
    c = make_uk_container()
    r = await uk_chat(c, PRIYA, "Maya wants an STI test")
    assert r["outcome"] == "confidential" and r["pending"] is None
    assert "can't book" in r["answer"]
    # Maya herself can.
    r2 = await uk_chat(c, MAYA, "I'd like an STI test")
    assert r2["pending"]["type"] == "choose_option"


async def test_friend_needs_a_one_time_consent_link():
    c = make_uk_container()
    r = await uk_chat(c, TOM, "my friend Sam has a bad back, can you book physio")
    assert r["outcome"] == "consent_needed" and "/consent/" in r["answer"]
    token = r["answer"].split("/consent/")[1].split()[0]
    with pytest.raises(ValueError):
        await c.uk_consents.approve(token, TOM)               # can't approve your own request
    await c.uk_consents.approve(token, SAM)
    r = await uk_chat(c, TOM, "my friend Sam has a bad back", r["thread_id"])
    assert r["for"] == "Sam" and r["pending"]["type"] == "choose_option"
    opt = r["pending"]["options"][0]["option_id"]
    r = await uk_resume(c, TOM, r["thread_id"], {"option_id": opt})
    r = await uk_resume(c, TOM, r["thread_id"],
                        {"slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True})
    assert r["outcome"] == "booked"
    audit = await c.uk_store.items("audit", SAM)
    assert audit[-1]["actor"] == TOM and audit[-1]["authority"] == "One-time consent link"
    # one booking only: the grant is used up
    r = await uk_chat(c, TOM, "my friend Sam needs physio again", r["thread_id"])
    assert r["outcome"] == "consent_needed"


async def test_typing_a_relationship_grants_nothing():
    c = make_uk_container()
    r = await uk_chat(c, SAM, "I'm Margaret Walker's carer, book her a GP appointment")
    assert r["outcome"] == "consent_needed" and r["for"] is None
    assert not await bookings(c, MARGARET)
    with pytest.raises(Exception):                       # and no back door via a thread
        await c.journey_service.thread_view(SAM, "ref-000100020004")


# --- the route agent and its guard -------------------------------------------------------
async def test_invented_provider_id_is_rejected_and_never_shown():
    script = ScriptedModel([
        AIMessage(content="", tool_calls=[call("find_pharmacy_first",
                                               {"condition": "uncomplicated_uti"}, 1)]),
        AIMessage(content="", tool_calls=[call("recommend", {
            "care_route": "pharmacy_first", "provider_ids": ["pharm:FAKE1"],
            "reason_for_patient": "r", "pharmacy_first_condition": "uncomplicated_uti"}, 2)]),
        AIMessage(content="", tool_calls=[call("recommend", {
            "care_route": "pharmacy_first", "provider_ids": ["pharm:ZPH01"],
            "reason_for_patient": "A pharmacist can help.",
            "pharmacy_first_condition": "uncomplicated_uti"}, 3)]),
    ])
    c = make_uk_container(route_model=script)
    r = await uk_chat(c, PRIYA, "burning when I pee since yesterday")
    tool_msgs = [m for m in script.seen[-1] if getattr(m, "name", "") == "recommend"]
    assert "not returned by any search" in tool_msgs[0].content     # the model was told why
    assert [o["option_id"] for o in r["pending"]["options"]] == ["pharm:ZPH01"]
    assert "Larkspur Pharmacy" in r["answer"]                       # rendered from records


async def test_prose_without_recommend_never_reaches_the_patient():
    script = ScriptedModel([
        AIMessage(content="Go to Totally Real Pharmacy, 1 High St."),
        AIMessage(content="Seriously, Totally Real Pharmacy."),
    ])
    c = make_uk_container(route_model=script)
    r = await uk_chat(c, PRIYA, "I have a cold")
    assert r["outcome"] == "no_match" and "Totally Real" not in r["answer"]
    assert "111.nhs.uk" in r["answer"]


async def test_pharmacy_first_ineligible_goes_to_the_gp():
    c = make_uk_container()
    r = await uk_chat(c, TOM, "burning when I pee")              # UTI pathway: women 16-64
    assert r["care_route"] == "gp"
    assert r["pending"]["type"] == "confirm_booking"              # Tom's practice books online


# --- booking: one confirmation, exactly once -----------------------------------------------
async def _book_first(c, who, text):
    r = await uk_chat(c, who, text)
    opt = next(o for o in r["pending"]["options"] if o["bookable"])
    r = await uk_resume(c, who, r["thread_id"], {"option_id": opt["option_id"]})
    assert r["pending"]["type"] == "confirm_booking"
    return r


async def test_nothing_is_booked_without_the_confirm_tap():
    c = make_uk_container()
    r = await _book_first(c, PRIYA, "burning when I pee")
    assert not await bookings(c, PRIYA)
    r = await uk_resume(c, PRIYA, r["thread_id"], {"consent": False})
    assert r["outcome"] == "cancelled" and not await bookings(c, PRIYA)


async def test_booking_and_sms_happen_exactly_once():
    c = make_uk_container()
    r = await _book_first(c, PRIYA, "burning when I pee")
    slot = r["pending"]["slots"][0]["slot_id"]
    r = await uk_resume(c, PRIYA, r["thread_id"], {"slot_id": slot, "consent": True})
    assert r["outcome"] == "booked" and r["booking"]["booking_ref"].startswith("BK-")
    # A retry with the SAME idempotency key returns the same booking; a different
    # key for the same slot is a conflict, never a second booking.
    b = r["booking"]
    state = await (await c.journey_service.graph()).aget_state(
        {"configurable": {"thread_id": r["thread_id"]}})
    key = state.values["idempotency_key"]
    replay = await c.journey.d.booking.book(b["provider_id"], b["slot_id"], key)
    assert replay["booking_ref"] == b["booking_ref"]
    from careroute.uk.integrations import SlotTaken
    with pytest.raises(SlotTaken):
        await c.journey.d.booking.book(b["provider_id"], b["slot_id"], "someone-else")
    outbox = await c.uk_store.items("outbox", "all")
    assert len(outbox) == 1 and "Pharmacy" not in outbox[0]["text"]


async def test_slot_taken_offers_fresh_times_and_errors_are_honest():
    c = make_uk_container()
    r = await _book_first(c, PRIYA, "burning when I pee")
    await c.uk_store.put("fault", "next_booking", "conflict")
    r = await uk_resume(c, PRIYA, r["thread_id"],
                        {"slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True})
    assert "just taken" in r["answer"] and r["pending"]["type"] == "confirm_booking"
    await c.uk_store.put("fault", "next_booking", "error")
    r = await uk_resume(c, PRIYA, r["thread_id"],
                        {"slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True})
    assert r["outcome"] == "booking_error" and "Nothing has been charged" in r["answer"]
    assert not await bookings(c, PRIYA)


async def test_resume_on_another_replica():
    """A paused journey resumes on a different process with state intact
    (memory mode shares the saver and store; CI repeats this on Postgres)."""
    a = make_uk_container()
    r = await _book_first(a, PRIYA, "burning when I pee")
    b = make_uk_container()
    b.checkpointers, b.uk_store, b.sessions = a.checkpointers, a.uk_store, a.sessions
    r2 = await uk_resume(b, PRIYA, r["thread_id"],
                         {"slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True})
    assert r2["outcome"] == "booked"


# --- GP: fixed provider, online consultation, and the referral that follows ---------------
async def test_gp_online_consultation_then_referral_continues_the_episode():
    c = make_uk_container()
    r = await uk_chat(c, PRIYA, "headaches every morning for three weeks")
    assert r["care_route"] == "gp" and r["pending"]["action"] == "send_request"
    assert not any(s.get("node") == "choose_option" for s in r["steps"])  # nothing to choose
    r = await uk_resume(c, PRIYA, r["thread_id"], {"consent": True})
    assert r["outcome"] == "booked" and r["booking"]["booking_ref"].startswith("OC-")
    # The GP refers: e-RS carries the same episode id into Path B.
    state = await (await c.journey_service.graph()).aget_state(
        {"configurable": {"thread_id": r["thread_id"]}})
    ep_id = state.values["episode_id"]
    ref = await c.uk_ers.create_referral(nhs_number=PRIYA, specialty="dermatology",
                                         referrer_ods="ZGP01", reason="x", episode_id=ep_id)
    t = await c.journey_service.start_referral(ref["ubrn"])
    view = await c.journey_service.thread_view(PRIYA, t["thread_id"])
    assert "follows the request sent to the GP practice" in view["messages"][0]["text"]
    assert view["pending"]["type"] == "preferences"


# --- Path B --------------------------------------------------------------------------------
async def test_referral_shortlist_respects_limits_and_ranks_in_code():
    c = make_uk_container()
    t = await c.journey_service.start_referral("000100020003")
    r = await uk_resume(c, TOM, t["thread_id"], {"text": "under 8 weeks, within 20 km"})
    opts = r["pending"]["options"]
    assert all(o["wait_weeks"] <= 8 and o["distance_km"] <= 20 for o in opts)
    assert [o["wait_weeks"] for o in opts] == sorted(o["wait_weeks"] for o in opts)
    assert not r["pending"]["near_miss"]


async def test_nothing_fits_shows_which_limit_bends():
    c = make_uk_container()
    t = await c.journey_service.start_referral("000100020003")
    r = await uk_resume(c, TOM, t["thread_id"], {"text": "under 2 weeks, within 3 km"})
    assert r["pending"]["near_miss"] and "Nothing meets all your limits" in r["answer"]
    assert all(o["breaks"] for o in r["pending"]["options"])
    # the patient bends a limit in chat; the thread re-shortlists
    r = await uk_chat(c, TOM, "ok, within 15 km is fine and up to 10 weeks", t["thread_id"])
    assert r["pending"]["type"] == "choose_option" and not r["pending"]["near_miss"]


async def test_private_referral_uses_the_insurers_figure():
    c = make_uk_container()
    t = await c.journey_service.start_referral("PRV-2026-0007")
    r = await uk_resume(c, TOM, t["thread_id"], {"text": "under £150, nearest"})
    opts = r["pending"]["options"]
    assert opts and all(o["patient_pays_gbp"] == "100.00" for o in opts)   # the excess
    r = await uk_resume(c, TOM, t["thread_id"], {"option_id": opts[0]["option_id"]})
    r = await uk_resume(c, TOM, t["thread_id"],
                        {"slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True})
    b = r["booking"]
    assert b["preauth_ref"].startswith("PA-") and b["charged_gbp"] == "100.00"


async def test_referral_threads_follow_proxy_access():
    c = make_uk_container()
    t = await c.journey_service.start_referral("000100020004")       # Margaret
    assert (await c.journey_service.thread_view(TOM, t["thread_id"]))["for"] == "Margaret"
    for outsider in (PRIYA, SAM, None):
        with pytest.raises(Exception) as e:
            await c.journey_service.thread_view(outsider, t["thread_id"])
        assert "404" in str(e.value)
    again = await c.journey_service.start_referral("000100020004")
    assert again["thread_id"] == t["thread_id"]                       # events are idempotent


async def test_store_semantics():
    s = MemoryJourneyStore()
    assert await s.put_if_absent("x", "k", 1) and not await s.put_if_absent("x", "k", 2)
    assert await s.get("x", "k") == 1
    await s.append("l", "k", {"a": 1}, max_len=2)
    await s.append("l", "k", {"a": 2}, max_len=2)
    await s.append("l", "k", {"a": 3}, max_len=2)
    assert [i["a"] for i in await s.items("l", "k")] == [2, 3]
