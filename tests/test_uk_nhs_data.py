"""
tests/test_uk_nhs_data.py — real NHS open data in the directory, and what the
patient is told when nothing is near them.

    importer        ODS / NHSBSA files -> postcodes -> data/uk/nhs/directory.json
    directory       demo | nhs | mixed, and the grid search
    journeys        a real pharmacy is never bookable and unknowns are said to
                    be unknown; nothing nearby is said plainly, never a question
    stale answers   an answer to a replaced card is refused
"""

import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from careroute.api.app import create_app
from careroute.runtime import ANYIO_BACKEND_OPTIONS
from careroute.uk.directory import UkServiceDirectory
from careroute.uk.route_agent import ROUTE_PROMPT
from scripts import import_nhs_directory as imp
from tests import fakes
from tests.fakes import (PRIYA, ScriptedModel, call, close_containers, make_uk_container,
                         uk_chat)

PETTS_WOOD = (51.3897, 0.0713)
LEEDS = (53.8195, -1.5781)



@pytest.fixture
async def _close():
    yield
    await close_containers()


# async tests run on anyio and close the containers they open
aio = [pytest.mark.anyio, pytest.mark.usefixtures("_close")]


def run_async(f):
    for m in aio:
        f = m(f)
    return f


# ------------------------------------------------------------------ fixtures
NHSBSA_CSV = """CONTRACTOR_CODE,CONTRACTOR_NAME,TRADING_NAME,ADDRESS_LINE_1,ADDRESS_LINE_2,POSTCODE,DISPENSER_ACCOUNT_TYPE,PRIVATE_CONTRACTOR,OUT_OF_HRS_DISP_CONTRACTOR,DSP,CLOSE_DATE
FAA01,BOOTS UK LIMITED,BOOTS,1 STATION SQUARE,PETTS WOOD,br5 1lz,English Pharmacy,N,N,N,
FAA02,ACME LTD,QUEENSWAY PHARMACY,22 QUEENSWAY,PETTS WOOD,BR5 1DH,English Pharmacy,N,N,N,
FAA03,WEBMEDS LTD,WEBMEDS,UNIT 4,ORPINGTON,BR6 0JA,English Pharmacy,N,N,Y,
FAA04,OLD CHEMIST LTD,OLD CHEMIST,3 HIGH ST,ORPINGTON,BR6 0JA,English Pharmacy,N,N,N,20240101
FAA05,PRIVATE CO,PRIVATE PHARMACY,9 HIGH ST,ORPINGTON,BR6 0JA,English Pharmacy,Y,N,N,
FAA06,APPLIANCES LTD,APPLIANCES,1 ROAD,ORPINGTON,BR6 0JA,Appliance Contractor,N,N,N,
FAA07,HEADINGLEY CHEM LTD,HEADINGLEY CHEMIST,5 NORTH LANE,LEEDS,LS6 3HW,English Pharmacy,N,N,N,
FAA08,NOWHERE LTD,NOWHERE PHARMACY,1 LANE,NOWHERE,ZZ99 9ZZ,English Pharmacy,N,N,N,
FAA09,CARDIFF LTD,CARDIFF PHARMACY,1 QUEEN ST,CARDIFF,CF10 1AA,English Pharmacy,N,N,N,
"""
# Classic ODS headerless layout: code, name, grouping, geography, 5 address
# lines, postcode, open, close, status, ... telephone (18th), ... setting (26th).
EPRACCUR = ("G1,THE PETTS WOOD SURGERY,Y56,QE1,1 FAIRWAY,PETTS WOOD,,,,BR5 1DH,19740401,,A,B,"
            "93C,,,01689 820159,,,,,,,,4,\n"
            "G2,CLOSED SURGERY,Y56,QE1,2 ROAD,,,,,BR5 1LZ,19740401,20200101,C,B,"
            "93C,,,,,,,,,,,4,\n"
            "G3,OUT OF HOURS HUB,Y56,QE1,3 ROAD,,,,,BR5 1LZ,19740401,,A,B,"
            "93C,,,,,,,,,,,8,\n")
ONSPD = """pcds,doterm,lat,long,ctry
BR5 1LZ,,51.38956,0.07114,E92000001
BR5 1DH,,51.39120,0.07560,E92000001
BR6 0JA,,51.37290,0.09810,E92000001
LS6 3HW,,53.81950,-1.57810,E92000001
CF10 1AA,,51.47950,-3.17710,W92000004
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def build_fixture(tmp_path):
    return imp.build([write(tmp_path, "pharm.csv", NHSBSA_CSV)],
                     [write(tmp_path, "epraccur.csv", EPRACCUR)],
                     imp.OnspdLookup(write(tmp_path, "onspd.csv", ONSPD)))


def data_dir_with_nhs(tmp_path) -> "Path":
    """A copy of data/ with an NHS directory file next to the demo data."""
    d = tmp_path / "data"
    shutil.copytree(fakes.make_settings().data_dir / "uk", d / "uk",
                    ignore=shutil.ignore_patterns("nhs"))
    doc, _ = build_fixture(tmp_path)
    (d / "uk" / "nhs").mkdir()
    (d / "uk" / "nhs" / "directory.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


# ------------------------------------------------------------------ importer
def test_importer_keeps_open_visitable_pharmacies_in_england(tmp_path):
    doc, skipped = build_fixture(tmp_path)
    ph = {s["ods"]: s for s in doc["services"] if s["service_type"] == "pharmacy"}
    assert set(ph) == {"FAA01", "FAA02", "FAA07"}
    assert skipped == {"closed": 2, "distance selling (internet only)": 1,
                       "not a community pharmacy": 1, "not a GP practice": 1,
                       "outside England": 1, "postcode not found": 1,
                       "private contractor": 1}
    boots = ph["FAA01"]
    assert boots["name"] == "Boots" and boots["postcode"] == "BR5 1LZ"
    assert boots["address"] == "1 Station Square, Petts Wood"
    assert (boots["lat"], boots["lng"]) == (51.38956, 0.07114)
    # not in the registers -> unknown, never guessed; never bookable
    assert boots["hours"] is None and boots["pharmacy_first"] is None
    assert boots["walk_in"] is None and boots["bookable"] is False
    assert doc["meta"]["counts"] == {"pharmacy": 3, "gp_practice": 1}
    assert "Open Government Licence v3.0" in doc["meta"]["attribution"]
    assert "NHSBSA Copyright" in doc["meta"]["attribution"]


def test_importer_reads_headerless_ods_gp_practices(tmp_path):
    doc, _ = build_fixture(tmp_path)
    gp = [s for s in doc["services"] if s["service_type"] == "gp_practice"]
    assert [(g["ods"], g["name"], g["phone"]) for g in gp] == [
        ("G1", "The Petts Wood Surgery", "01689 820159")]


def test_importer_names_the_headers_it_could_not_use(tmp_path):
    p = write(tmp_path, "odd.csv", "Shop,Town\nA,B\n")
    with pytest.raises(imp.ImportError_) as e:
        imp.records_from(p)
    assert "Shop, Town" in str(e.value)
    # a wide file with an unknown header is NOT mistaken for headerless ODS data
    wide = ",".join(f"COL_{i}" for i in range(20)) + "\n" + ",".join("x" * 20) + "\n"
    with pytest.raises(imp.ImportError_, match="COL_9"):
        imp.records_from(write(tmp_path, "wide.csv", wide))


def test_postcodes_io_lookup_batches_and_caches(tmp_path):
    calls = []

    def handler(request):
        asked = json.loads(request.content)["postcodes"]
        calls.append(len(asked))
        return httpx.Response(200, json={"status": 200, "result": [
            {"query": pc, "result": None if pc == "ZZ99 9ZZ" else
             {"latitude": 51.39, "longitude": 0.07, "country": "England"}} for pc in asked]})

    cache = tmp_path / "cache.json"
    geo = imp.PostcodesIoLookup(cache=cache, pause_s=0,
                                client=httpx.Client(transport=httpx.MockTransport(handler)))
    wanted = {f"BR5 {i}AA" for i in range(1, 10)} | {"ZZ99 9ZZ"}
    got = geo.lookup(wanted)
    assert calls == [10] and len(got) == 9 and "ZZ99 9ZZ" not in got
    assert geo.lookup(wanted) == got and calls == [10]          # second run: from cache


def test_names_from_upper_case_registers_read_naturally():
    assert imp.nice("BOOTS UK LIMITED") == "Boots UK Limited"
    assert imp.nice("LLOYDS PHARMACY IN SAINSBURY'S") == "Lloyds Pharmacy in Sainsbury's"
    assert imp.nice("CO-OP PHARMACY 24") == "Co-Op Pharmacy 24"


# ------------------------------------------------------------------ directory
class _NoRoads:
    async def annotate(self, lat, lng, items):
        from careroute.maps.distance import haversine_km
        return [dict(i, distance_km=round(haversine_km(lat, lng, i["lat"], i["lng"]), 2),
                     distance_type="straight_line", drive_min=None) for i in items]


def _demo_services():
    path = fakes.make_settings().data_dir / "uk" / "services.json"
    return json.loads(path.read_text(encoding="utf-8"))["services"]


@run_async
async def test_mixed_mode_keeps_leeds_demo_only_and_finds_real_pharmacies_elsewhere(tmp_path):
    doc, _ = build_fixture(tmp_path)
    d = UkServiceDirectory(_demo_services(), _NoRoads(), nhs=doc["services"], mode="mixed",
                           nhs_meta=doc["meta"])
    leeds = await d.search("pharmacy", *LEEDS, radius_km=5, k=10)
    assert leeds and all(r["source"] == "demo" for r in leeds)   # FAA07 is inside the demo area
    petts = await d.search("pharmacy", *PETTS_WOOD, radius_km=5, k=10, pharmacy_first=True)
    assert [r["ods"] for r in petts] == ["FAA01", "FAA02"]       # unknown PF status stays in
    assert d.info()["counts"]["nhs"] == 3                         # 2 pharmacies + 1 GP outside Leeds
    assert "Open Government Licence" in d.info()["attribution"]


@run_async
async def test_nhs_mode_swaps_demo_pharmacies_but_keeps_demo_clinics(tmp_path):
    doc, _ = build_fixture(tmp_path)
    d = UkServiceDirectory(_demo_services(), _NoRoads(), nhs=doc["services"], mode="nhs")
    leeds = await d.search("pharmacy", *LEEDS, radius_km=5, k=10)
    assert [r["ods"] for r in leeds] == ["FAA07"]
    assert await d.search("sexual_health", *LEEDS, radius_km=10)  # demo clinics still there


@run_async
async def test_grid_search_matches_a_full_scan():
    d = UkServiceDirectory(_demo_services(), _NoRoads())
    from careroute.maps.distance import haversine_km
    for radius in (1, 3, 8, 20):
        got = {r["id"] for r in await d.search("pharmacy", *LEEDS, radius_km=radius, k=100)}
        want = {s["id"] for s in _demo_services() if s["service_type"] == "pharmacy"
                and haversine_km(*LEEDS, s["lat"], s["lng"]) <= radius}
        assert got == want


def test_a_mode_that_needs_nhs_data_says_how_to_build_it():
    with pytest.raises(ValueError, match="import_nhs_directory"):
        UkServiceDirectory(_demo_services(), _NoRoads(), mode="nhs")


# ------------------------------------------------------------------ journeys
@run_async
async def test_petts_wood_finds_real_pharmacies_that_are_never_booked(tmp_path):
    c = make_uk_container(data_dir=data_dir_with_nhs(tmp_path))
    r = await uk_chat(c, PRIYA, "burning when I pee since yesterday",
                      lat=PETTS_WOOD[0], lng=PETTS_WOOD[1])
    assert r["care_route"] == "pharmacy_first"
    assert r["outcome"] == "contact_directly" and r["pending"] is None   # no booking card
    assert "Boots" in r["answer"] and "BR5 1LZ" in r["answer"]
    assert "offer NHS Pharmacy First" in r["answer"]                     # unknown, so: check


@run_async
async def test_leeds_still_runs_the_scripted_demo_in_mixed_mode(tmp_path):
    c = make_uk_container(data_dir=data_dir_with_nhs(tmp_path))
    r = await uk_chat(c, PRIYA, "burning when I pee since yesterday")
    ids = [o["option_id"] for o in r["pending"]["options"]]
    assert ids and all(i.startswith("pharm:ZPH") for i in ids)          # bookable demo pharmacies


def _empty_then(*final):
    return ScriptedModel([
        AIMessage(content="", tool_calls=[call("find_pharmacy_first", {
            "condition": "uncomplicated_uti", "radius_km": 20}, 1)]),
        *final])


@run_async
async def test_nothing_nearby_is_said_plainly_instead_of_a_filler_question():
    script = _empty_then(AIMessage(content="", tool_calls=[call("ask_patient", {
        "question": "How long have you had the burning?"}, 2)]))
    c = make_uk_container(route_model=script)                            # demo data: Leeds only
    r = await uk_chat(c, PRIYA, "burning when I pee", lat=PETTS_WOOD[0], lng=PETTS_WOOD[1])
    assert r["outcome"] == "no_services_nearby"
    assert "How long" not in r["answer"]
    assert "couldn't find a pharmacy offering Pharmacy First within 20 km" in r["answer"]
    assert "only has services in and around Leeds" in r["answer"]


@run_async
async def test_giving_up_after_empty_searches_is_also_nothing_nearby_not_no_match():
    script = _empty_then(AIMessage(content="I'm not sure."), AIMessage(content="Still not sure."))
    c = make_uk_container(route_model=script)
    r = await uk_chat(c, PRIYA, "burning when I pee", lat=PETTS_WOOD[0], lng=PETTS_WOOD[1])
    assert r["outcome"] == "no_services_nearby"


@run_async
async def test_a_question_is_still_allowed_before_anything_was_searched():
    script = ScriptedModel([AIMessage(content="", tool_calls=[call("ask_patient", {
        "question": "Is it you or someone else?"}, 1)])])
    c = make_uk_container(route_model=script)
    r = await uk_chat(c, PRIYA, "it hurts")
    assert r["outcome"] == "question"


def test_the_prompt_no_longer_suggests_asking_how_long():
    assert "how long symptoms have lasted" not in ROUTE_PROMPT
    assert "Never ask a question because a search found" in ROUTE_PROMPT


# ------------------------------------------------------------------ stale answers
def _http(c):
    if c in fakes._OPEN:
        fakes._OPEN.remove(c)
    return TestClient(create_app(c), backend_options=ANYIO_BACKEND_OPTIONS)


def test_an_answer_to_a_replaced_card_is_refused():
    with _http(make_uk_container()) as http:
        me = {"X-Demo-User": PRIYA}
        first = http.post("/chat", json={"message": "burning when I pee"}, headers=me).json()
        tid, old = first["thread_id"], first["pending"]
        second = http.post("/chat", json={"message": "I have a sore throat too",
                                          "thread_id": tid}, headers=me).json()
        new = second["pending"]
        assert old["interrupt_id"] != new["interrupt_id"]
        stale = http.post("/chat/resume", headers=me, json={"thread_id": tid, "resume": {
            "option_id": old["options"][0]["option_id"], "interrupt_id": old["interrupt_id"]}})
        assert stale.status_code == 409 and "out of date" in stale.json()["detail"]
        ok = http.post("/chat/resume", headers=me, json={"thread_id": tid, "resume": {
            "option_id": new["options"][0]["option_id"], "interrupt_id": new["interrupt_id"]}})
        assert ok.status_code == 200 and ok.json()["pending"]["type"] == "confirm_booking"


def test_directory_endpoint_reports_demo_data_by_default():
    with _http(make_uk_container()) as http:
        d = http.get("/directory").json()
    assert d["mode"] == "demo" and d["counts"]["nhs"] == 0 and d["attribution"] is None
