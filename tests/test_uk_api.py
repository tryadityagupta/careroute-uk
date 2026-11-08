"""
tests/test_uk_api.py — UK CareRoute over HTTP (FastAPI TestClient), plus the
two security-sensitive clients tested against mocked transports: NHS login
ID-token verification and GOV.UK Notify.
"""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from careroute.api.app import create_app
from careroute.runtime import ANYIO_BACKEND_OPTIONS
from careroute.uk.identity import IdentityError, NhsLoginIdentity
from careroute.uk.integrations import GovUkNotify
from careroute.uk.store import MemoryJourneyStore
from tests import fakes
from tests.fakes import MAYA, PRIYA, SAM, TOM, make_uk_container


def client_for(c) -> TestClient:
    if c in fakes._OPEN:
        fakes._OPEN.remove(c)
    return TestClient(create_app(c), backend_options=ANYIO_BACKEND_OPTIONS)


def as_user(nhs):
    return {"X-Demo-User": nhs}


def test_unknown_demo_user_is_401_and_anonymous_gets_info_only():
    with client_for(make_uk_container()) as http:
        assert http.post("/chat", json={"message": "sore throat"},
                         headers=as_user("1234567890")).status_code == 401
        r = http.post("/chat", json={"message": "sore throat"}).json()
        assert r["outcome"] == "sign_in_needed"
        r = http.post("/chat", json={"message": "can't breathe", "lat": 53.8, "lng": -1.55}).json()
        assert r["outcome"] == "emergency"           # no login needed for 999


def test_full_booking_over_http_and_thread_ownership():
    with client_for(make_uk_container()) as http:
        r = http.post("/chat", json={"message": "burning when I pee"},
                      headers=as_user(PRIYA)).json()
        tid = r["thread_id"]
        assert r["pending"]["type"] == "choose_option" and r["steps"]
        assert http.get(f"/chat/{tid}", headers=as_user(TOM)).status_code == 404
        assert http.post("/chat/resume", json={"thread_id": tid, "resume": {}},
                         headers=as_user(TOM)).status_code == 404
        opt = next(o for o in r["pending"]["options"] if o["bookable"])["option_id"]
        r = http.post("/chat/resume", json={"thread_id": tid, "resume": {"option_id": opt}},
                      headers=as_user(PRIYA)).json()
        slot = r["pending"]["slots"][0]["slot_id"]
        r = http.post("/chat/resume", json={"thread_id": tid, "resume": {
            "slot_id": slot, "consent": True}}, headers=as_user(PRIYA)).json()
        assert r["outcome"] == "booked"
        assert http.post("/chat/resume", json={"thread_id": tid, "resume": {}},
                         headers=as_user(PRIYA)).status_code == 409      # nothing waiting
        view = http.get(f"/chat/{tid}", headers=as_user(PRIYA)).json()
        assert view["messages"][0]["role"] == "user" and view["outcome"] == "booked"
        assert len(http.get("/bookings", headers=as_user(PRIYA)).json()["bookings"]) == 1
        assert http.get("/audit", headers=as_user(PRIYA)).json()["entries"]
        assert http.get("/demo/outbox").json()["messages"]


def test_sse_stream_sends_progress_then_a_result():
    with client_for(make_uk_container()) as http:
        with http.stream("POST", "/chat?stream=1", json={"message": "I have a sore throat"},
                         headers=as_user(PRIYA)) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
        kinds = [e["type"] for e in events]
        assert kinds[-1] == "result" and "progress" in kinds and "step" in kinds
        # a validation error before the stream starts is a normal JSON error
        r = http.post("/chat?stream=1", json={"message": " "}, headers=as_user(PRIYA))
        assert r.status_code == 400


def sse_events(http, path, body, who):
    with http.stream("POST", path + "?stream=1", json=body, headers=as_user(who)) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_live_status_arrives_before_the_work_it_describes():
    with client_for(make_uk_container()) as http:
        events = sse_events(http, "/chat", {"message": "I've had burning when I pee since yesterday"},
                            PRIYA)
    texts = [e.get("text") or e.get("label") for e in events]

    def first(kind, startswith):
        return next(i for i, e in enumerate(events)
                    if e["type"] == kind and (e.get("text") or e.get("label")).startswith(startswith))

    # a node's live line comes before the node's finished step
    assert first("status", "Checking for emergency signs") < first("step", "Checked for emergency")
    assert first("status", "Working out the right place") < first("step", "Chose the right front")
    # a tool's live line names what is being searched, and where, before the result
    search = first("status", "Searching Pharmacy First pharmacies near Headingley")
    assert search < first("status", "Getting drive times") < first("progress", "Found")
    assert events[-1]["type"] == "result", texts


def test_emergency_status_is_rules_only_and_never_mentions_a_search():
    with client_for(make_uk_container()) as http:
        events = sse_events(http, "/chat", {"message": "Crushing chest pain and I'm sweating"},
                            PRIYA)
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert statuses == ["Checking for emergency signs\u2026"]
    assert events[-1]["outcome"] == "emergency"


def test_json_responses_leave_out_live_status_lines():
    with client_for(make_uk_container()) as http:
        r = http.post("/chat", json={"message": "I have a sore throat"},
                      headers=as_user(PRIYA)).json()
    kinds = {s["type"] for s in r["steps"]}
    assert "status" not in kinds and "step" in kinds


def test_confidential_bookings_are_hidden_from_parents():
    with client_for(make_uk_container()) as http:
        r = http.post("/chat", json={"message": "I'd like an STI test"},
                      headers=as_user(MAYA)).json()
        tid = r["thread_id"]
        opt = r["pending"]["options"][0]["option_id"]
        r = http.post("/chat/resume", json={"thread_id": tid, "resume": {"option_id": opt}},
                      headers=as_user(MAYA)).json()
        http.post("/chat/resume", json={"thread_id": tid, "resume": {
            "slot_id": r["pending"]["slots"][0]["slot_id"], "consent": True}},
            headers=as_user(MAYA))
        assert len(http.get("/bookings", headers=as_user(MAYA)).json()["bookings"]) == 1
        assert http.get("/bookings", headers=as_user(PRIYA)).json()["bookings"] == []
        assert not http.get("/audit", headers=as_user(PRIYA)).json()["entries"]


def test_referral_inbox_and_consent_endpoints():
    with client_for(make_uk_container()) as http:
        t = http.post("/referrals/events", json={"ubrn": "000100020004"}).json()
        inbox = http.get("/inbox", headers=as_user(TOM)).json()["threads"]
        assert [x["thread_id"] for x in inbox] == [t["thread_id"]]
        assert inbox[0]["for"] == "Margaret" and inbox[0]["waiting_for"] == "preferences"
        assert http.get("/inbox", headers=as_user(PRIYA)).json()["threads"] == []

        r = http.post("/chat", json={"message": "my friend Sam needs physio for his back"},
                      headers=as_user(TOM)).json()
        token = r["answer"].split("/consent/")[1].split()[0]
        assert http.get(f"/consents/{token}", headers=as_user(SAM)).json()["status"] == "pending"
        assert http.post(f"/consents/{token}/approve", headers=as_user(TOM)).status_code == 409
        assert http.post(f"/consents/{token}/approve",
                         headers=as_user(SAM)).json()["status"] == "approved"
        me = http.get("/me", headers=as_user(TOM)).json()
        assert me["consent_grants"][0]["label"] == "Sam"
        assert "Margaret" in [p["first_name"] for p in me["linked_profiles"]]


def test_referral_events_need_a_key_outside_demo_mode():
    with client_for(make_uk_container(demo_tools=False)) as http:
        assert http.post("/referrals/events", json={"ubrn": "000100020003"}).status_code == 403
        assert http.get("/demo/users").status_code == 404
    c = make_uk_container(demo_tools=False, referral_event_key="s3cret")
    with client_for(c) as http:
        assert http.post("/referrals/events", json={"ubrn": "000100020003"},
                         headers={"X-Event-Key": "nope"}).status_code == 401
        assert http.post("/referrals/events", json={"ubrn": "000100020003"},
                         headers={"X-Event-Key": "s3cret"}).status_code == 200


def test_demo_gp_outcome_starts_a_linked_referral_thread():
    with client_for(make_uk_container()) as http:
        r = http.post("/chat", json={"message": "headaches every morning for three weeks"},
                      headers=as_user(PRIYA)).json()
        http.post("/chat/resume", json={"thread_id": r["thread_id"], "resume": {"consent": True}},
                  headers=as_user(PRIYA))
        t = http.post("/demo/gp-outcome", json={"thread_id": r["thread_id"],
                                                "specialty": "dermatology"},
                      headers=as_user(PRIYA)).json()
        view = http.get(f"/chat/{t['thread_id']}", headers=as_user(PRIYA)).json()
        assert "follows the request sent to the GP practice" in view["messages"][0]["text"]


def test_version_reports_the_uk_modes():
    with client_for(make_uk_container()) as http:
        v = http.get("/version").json()
        assert v["llm"] == "offline" and v["nhs_login"] == "demo"
        assert http.get("/").status_code == 200 and http.get("/legacy").status_code == 200


# --- NHS login ID tokens ---------------------------------------------------------------
ISS, AUD = "https://auth.sandpit.signin.nhs.uk", "careroute-client"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return key, {**jwk, "kid": "k1", "use": "sig", "alg": "RS512"}


def _token(key, **claims):
    now = int(time.time())
    body = {"iss": ISS, "aud": AUD, "sub": "abc", "iat": now, "exp": now + 300,
            "identity_proofing_level": "P9", "nhs_number": TOM, **claims}
    return jwt.encode(body, key, algorithm="RS512", headers={"kid": "k1"})


@pytest.mark.anyio
async def test_nhs_login_token_verification():
    key, jwk = _keypair()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"keys": [jwk]}))
    idp = NhsLoginIdentity(ISS, AUD, client=httpx.AsyncClient(transport=transport))
    ok = await idp.resolve({"authorization": f"Bearer {_token(key)}"})
    assert ok == TOM
    assert await idp.resolve({}) is None
    for bad in (_token(key, aud="someone-else"), _token(key, identity_proofing_level="P5"),
                _token(key, exp=int(time.time()) - 10), _token(key, nhs_number="123"),
                _token(_keypair()[0])):                        # signed by another key
        with pytest.raises(IdentityError):
            await idp.resolve({"authorization": f"Bearer {bad}"})


# --- GOV.UK Notify -------------------------------------------------------------------------
@pytest.mark.anyio
async def test_govuk_notify_request_and_idempotency():
    service, secret = "26785a09-ab16-4eb0-8407-a37497a57506", "3d844edf-8d35-48ac-975b-e847b4f122b0"
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(201, json={"id": "n1"})
    n = GovUkNotify(f"careroute-{service}-{secret}", "tmpl-1", MemoryJourneyStore(),
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    fields = {"first_name": "Tom", "when": "Wed 14 Oct, 9:20am", "reference": "BK-1"}
    assert await n.send_sms("07700 900456", "x", fields, "ref-1") == "sent"
    assert await n.send_sms("07700 900456", "x", fields, "ref-1") == "already_sent"
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["template_id"] == "tmpl-1" and body["personalisation"] == fields
    claims = jwt.decode(seen[0].headers["authorization"].split()[1], secret, algorithms=["HS256"])
    assert claims["iss"] == service
