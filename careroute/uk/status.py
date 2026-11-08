"""
uk/status.py — live "what I'm doing now" lines for the patient.

Two kinds of stream event travel through LangGraph's custom stream:

    status      present tense, sent BEFORE slow work starts
                ("Searching pharmacies near Headingley (LS6 3HW)…").
                The page shows the latest one in the chat as a single live
                line and removes it when the answer arrives.
    progress    past tense, sent AFTER work finishes ("Found 3 pharmacies
                within 5 km"), for the step trace panel.

Node-level status lines ("Checking for emergency signs…") come from the
graph's "tasks" stream in service.py, so nodes don't need to announce
themselves. Call status() only for work inside a node that is worth naming:
a tool search, a drive-time lookup, an insurer call, a booking.

Both are best-effort. Outside a stream (the plain JSON API, unit tests)
there is no writer and they do nothing.

A status line is shown to whoever is signed in, so it may only describe what
that person asked for: a place they named, a service they asked about. Never
put clinical detail they didn't type, or another person's data, in one.
"""

from __future__ import annotations

from langgraph.config import get_stream_writer

MAX_PLACE = 60


def status(text: str) -> None:
    try:
        get_stream_writer()({"status": text})
    except Exception:
        pass                                    # not streaming: nothing to tell


def place(where: dict | None) -> str:
    """A short, safe label for a searched location."""
    label = " ".join(str((where or {}).get("label") or "").split())[:MAX_PLACE]
    return label or "your location"
