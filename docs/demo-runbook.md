# UK CareRoute: stakeholder demo runbook

A 15-minute demo for a UK health audience, and everything to do before it.
The rule for the whole session: **say clearly what is real and what is
simulated.** A UK stakeholder will notice a fictional pharmacy name or a
booking that couldn't really happen, and trust is lost faster than it's won.

## 1. The week before

**Real pharmacies (optional, recommended).** Without them, the app only knows
the fictional Leeds services, and any other town gets "nothing nearby".

1. Download the pharmacy list. Either:
   - the NHSBSA Open Data Portal dataset **Contractor details**
     (opendata.nhsbsa.net, search "contractor details"), CSV; or
   - an ODS export of pharmacies from NHS England's **ODS Data Search and
     Export** service.
2. Build the directory (postcodes.io is free and needs no key; about 120
   requests for all of England, cached for re-runs):

   ```bash
   python -m scripts.import_nhs_directory --pharmacies ~/Downloads/<file>.csv --postcodes-io
   ```

   It prints what it kept and why it skipped the rest (closed, internet-only,
   private, outside England, unknown postcode). Expect roughly 10,000 to
   11,500 pharmacies for England. If it stops with "couldn't find an
   organisation code and a postcode column", it lists the headers it saw:
   add the right name to `ALIASES` in the script.
3. Restart the app. The startup log shows
   `directory: mixed | demo services: 18 | NHS open data services: …`, and the
   page notice changes to say pharmacies outside Leeds are real, with the Open
   Government Licence attribution underneath. Keep that attribution visible.

The file is about 4 MB. Committing it is fine under the licence (the
attribution travels with it in `meta`), and it means a deployed copy has it.

**Model.** Use a real `OPENAI_API_KEY` with a monthly spending limit set in
the OpenAI dashboard. Keep `CAREROUTE_LLM=offline` in your back pocket (see
section 6).

**Rehearse the script twice**, with the exact wording below. Language models
vary from run to run; rehearsal tells you how much.

## 2. Thirty minutes before

```bash
docker compose up --build --scale app=3 redis postgres migrate app lb pruner
docker compose exec redis redis-cli FLUSHALL     # forget rehearsal bookings and threads
```

Then:

- Open `http://localhost:8000` in a **clean browser window** (a guest profile
  hides your bookmarks bar and history). Zoom to 125% so the room can read it.
- Demo controls: **Next booking attempt: Works normally.**
- Send one throwaway message and click **Start a new conversation**. The first
  request warms the containers and the model connection.
- Mute notifications. Have this runbook on a second screen or printed.

## 3. The demo script (about 15 minutes)

Open with the problem in one sentence: *many people book a GP for things a
pharmacist or a self-referral service could handle the same day, and after a
referral, choosing where to be seen is confusing.* Then point at the dark
panel: *every step is labelled Code or Model; safety, permission, money and
booking are always code.*

| # | Signed in as | Type or do | What to point out |
|---|---|---|---|
| 1 | Priya | "I've had burning when I pee since yesterday" | Live status lines while it works. Pharmacy First, eligibility checked **in code** (women 16 to 64). Pick a pharmacy, one confirmation, the text message has no health details. |
| 2 | Tom | the same message | Not eligible (male), so it goes to his GP. His practice books online, so there's nothing to choose: straight to times. |
| 3 | Priya | "Arjun has had earache since last night" | Proxy access comes from the GP record, not from what was typed. The child's age decides eligibility. |
| 4 | Priya, then Maya | "Maya wants an STI test" | A parent can't book a teenager's confidential service. Switch to Maya: she can, and Priya's bookings never show it. |
| 5 | Priya | Somewhere else box: `BR5 1LZ` (Petts Wood). Message: "burning when I pee" | **Real pharmacies** from NHS open data with real addresses. Not bookable here, and it says plainly that opening hours and Pharmacy First participation must be checked: it never guesses. *(Needs section 1.)* |
| 6 | Tom | Demo controls: send referral, orthopaedics (NHS). Answer "under 8 weeks, within 20 km" | A referral arrives from e-RS. Filtering, widening and ranking are code. Then try "under 2 weeks, within 3 km": nothing fits, so each option shows which limit it bends. |
| 7 | Tom | Demo controls: **Time is taken by someone else**, then confirm a time | Fresh times, no double booking. Then **Booking system is down**: "nothing has been charged; safe to try again". |
| 8 | Signed out | "Crushing chest pain and I'm sweating" | 999 first, before login, with **no model call at all**. Only one status line appears. |

Close on the design rule (*the model handles language; code handles
decisions*) and the next steps in section 5.

## 4. What is real and what is simulated

Say this early, and again if asked.

| Real | Simulated |
|---|---|
| The journey graph, interrupts, resume on any server | NHS login (a demo user picker; a real token verifier exists for NHS login's sandpit) |
| Red-flag rules, Pharmacy First eligibility, authority and consent checks | GP record, proxy links and patients (fictional, test-range NHS numbers) |
| The guard: providers come only from search results, rendered from records | GP appointment systems, e-RS, provider booking, insurer and payments |
| Exactly-once booking (idempotency keys), honest errors | Leeds services, waiting times and prices |
| Drive times (OSRM), UK postcode lookup (postcodes.io) | SMS (a demo outbox; a GOV.UK Notify client exists) |
| **Pharmacies outside Leeds: NHS open data** (when imported) | |

## 5. Questions to expect, and honest answers

**"Is this clinically safe?"** Not yet, and it doesn't claim to be. The
red-flag rules are an engineer's draft. A real deployment needs a clinical
safety officer and DCB0129/DCB0160 hazard logs, and triage software is likely
a medical device under MHRA rules. Clinicians writing and signing off the
rules is step one (docs/uk-design.md).

**"Can the AI make up a pharmacy or a price?"** No, by construction. The
model returns provider ids; code checks every id came from a search in this
conversation, and renders names, addresses, times and prices from records.
Prices come from the insurer, in exact decimal arithmetic.

**"How would it connect to the NHS for real?"** Each simulated part sits
behind a small interface with a named replacement: NHS login, PDS for
demographics, IM1/GP Connect for GP appointments, e-RS for referrals, the
Directory of Healthcare Services for service details, GOV.UK Notify for texts.
Each needs NHS onboarding and assurance (DTAC, DSPT, a DPIA).

**"Where does the pharmacy data come from, and is it current?"** NHS open
data (NHSBSA / ODS) under the Open Government Licence, dated on the page. It
doesn't include opening hours or Pharmacy First participation, which is why
the app says to check; the Directory of Healthcare Services API would supply
them.

**"What does it cost to run?"** gpt-4o-mini, about 4 model calls for a
symptom message. Path B referrals can run with one model call (reading the
limits) or none.

**"What if the model is down?"** The API answers 503 with the 999 message, and
emergency triage never needs the model.

## 6. If something goes wrong

| Symptom | Do this |
|---|---|
| Answers are slow or odd, or OpenAI is down | Restart with `CAREROUTE_LLM=offline` in `.env`: deterministic rules and a scripted route model, same journeys, no network to the model. Say so: "this is the offline mode we use for testing". |
| Distances say "straight line" | OSRM is unreachable. Harmless: say the drive times come from a routing service that isn't responding. |
| "Those options are out of date" | A card from before a newer message was clicked. Use the latest card, or Start a new conversation. |
| A journey misbehaves | **Start a new conversation**. If needed, `docker compose exec redis redis-cli FLUSHALL` and refresh. |
| The whole stack is unhappy | Run without Docker: `uvicorn --factory careroute.api.app:create_app` (single replica, memory mode). Every journey still works. |
