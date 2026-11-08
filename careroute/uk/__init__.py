"""
careroute/uk — UK CareRoute: from "something is wrong" to a booked
appointment at the right NHS front door, with one confirmation.

    graph.py          the journey graph (Path A symptoms, Path B referral,
                      one shared booking subgraph)
    route_agent.py    the ONE agent: picks the front door; returns provider
                      ids, never free-text provider lists
    llm.py            the three single-shot LLM calls (subject, triage,
                      preferences) + an offline rules implementation
    offline_model.py  a deterministic stand-in for the route agent's model
    triage_rules.py   red-flag rules that run BEFORE any model call
    pharmacy_first.py the 7 Pharmacy First conditions and their age/sex rules
    authority.py      who may act for whom (NHS login, proxy links, consent)
    identity.py       NHS login token verification (demo + OIDC)
    records.py        NHS records (demo PDS / GP record)
    directory.py      NHS service directory (demo data, PostGIS-ready shape)
    integrations.py   e-RS, GP (IM1-style), provider booking, insurer,
                      payments, SMS — each behind a small interface
    store.py          shared journey state (memory or Redis)
    service.py        one HTTP turn: identity, thread ownership, lock, run
    api.py            the FastAPI routes

The design rule throughout: the LLM handles language, code handles
decisions. Safety, permission, money and booking are decided in code.
"""
