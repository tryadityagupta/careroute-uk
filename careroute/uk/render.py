"""
uk/render.py — every sentence that names a provider is built HERE, from
records. The model's only free text is a short reason; it never writes a
name, address, time or price.
"""

from __future__ import annotations

from careroute.uk.integrations import fmt_start


def distance(o: dict) -> str:
    d = o.get("distance_km")
    if d is None:
        return ""
    if o.get("distance_type") == "road":
        mins = o.get("drive_min")
        return f"{d:g} km by road" + (f", about {mins} min by car" if mins is not None else "")
    return f"{d:g} km (straight line)"


def money(gbp: str | None) -> str:
    if gbp in (None, ""):
        return ""
    return "£" + (gbp[:-3] if gbp.endswith(".00") else gbp)


def weeks(n) -> str:
    return f"about {n:g} week{'' if n == 1 else 's'} wait"


def option_line(i: int, o: dict) -> str:
    bits = [distance(o)]
    if o.get("wait_weeks") is not None:
        bits.append(weeks(o["wait_weeks"]))
    if o.get("patient_pays_gbp") is not None and o.get("kind") in ("ers", "private"):
        bits.append("free on the NHS" if o.get("funding") == "NHS"
                    else f"you pay {money(o['patient_pays_gbp'])}")
    if o.get("walk_in") and not o.get("bookable"):
        bits.append("walk in, no appointment")
    if o.get("hours"):
        bits.append(o["hours"])
    return f"{i}. {o['name']} — " + "; ".join(b for b in bits if b)


def option_list(options: list[dict]) -> str:
    return "\n".join(option_line(i, o) for i, o in enumerate(options, 1))


def emergency(triage: dict, places: list[dict]) -> str:
    flags = ", ".join(triage.get("red_flags") or []) or "your symptoms"
    if triage.get("mental_health"):
        return ("If you or they are in immediate danger, call 999 now.\n\n"
                "You can also call NHS 111 and choose the mental health option, any time, "
                "day or night. Samaritans are there 24 hours a day on 116 123 (free).\n\n"
                "You don't have to go through this alone.")
    text = (f"Call 999 now. This could be a medical emergency ({flags.lower()}).\n\n"
            "An ambulance can start treatment on the way. Don't drive yourself.")
    if places:
        text += ("\n\nIf you have been told to go to A&E, the nearest are:\n"
                 + option_list(places))
    return text


def urgent(triage: dict, places: list[dict]) -> str:
    flags = ", ".join(triage.get("red_flags") or []) or "what you've described"
    if triage.get("mental_health"):
        return ("This needs support today. Call NHS 111 and choose the mental health "
                "option, any time, day or night. Samaritans: 116 123 (free, 24 hours).\n\n"
                "If anyone is in immediate danger, call 999.")
    text = (f"This needs to be looked at today ({flags.lower()}), but it doesn't sound like "
            "a 999 emergency. Call NHS 111 or go to 111.nhs.uk: they can assess you and "
            "book you into the right service.")
    if places:
        text += ("\n\nUrgent treatment centres near you (no appointment needed):\n"
                 + option_list(places))
    return text + "\n\nIf it gets worse, call 999."


def info_only(auth: dict, base_url: str) -> tuple[str, str]:
    status = auth.get("status")
    if status == "ambiguous":
        choices = auth.get("choices") or []
        return (f"Who is this for: {' or '.join(choices)}?", "needs_clarification")
    if status == "needs_consent":
        label = auth.get("label") or "them"
        text = (f"I can't act for {label} yet: you don't have access to act for them in "
                "their NHS record.")
        if auth.get("invite_token"):
            text += (f"\n\nAsk {label} to open this link, sign in with NHS login and approve "
                     f"it. It is valid for 24 hours and covers one booking:\n"
                     f"{base_url}/consent/{auth['invite_token']}\n\n"
                     "Once they approve, send your message again.")
        if auth.get("no_proxy_link"):
            text += ("\n\nFor family members, proxy access can also be set up at their GP "
                     "practice, and then shows as a linked profile in the NHS App.")
        return text, "consent_needed"
    return ("Sign in with NHS login so I can check your record and book for you. "
            "If you need help now, use 111.nhs.uk or call NHS 111. "
            "In an emergency, call 999.", "sign_in_needed")


def booked(b: dict) -> str:
    who = b.get("for_name")
    if b.get("kind") == "gp" and b.get("booking_mode") == "online_consultation":
        head = f"Sent to {b['provider_name']}" + (f" for {who}" if who else "") + "."
    elif b.get("booking_mode") == "self_referral":
        head = f"Self-referral sent to {b['provider_name']}" + (f" for {who}" if who else "") + "."
    else:
        head = (f"Booked: {b['provider_name']}, {fmt_start(b.get('start'))}"
                + (f", for {who}" if who else "") + ".")
    lines = [head, f"Reference: {b['booking_ref']}"]
    if b.get("address"):
        lines.append(b["address"])
    if b.get("next_step"):
        lines.append(b["next_step"])
    if b.get("kind") in ("ers", "private"):
        lines.append("Cost to you: " + ("nothing, NHS-funded" if b.get("funding") == "NHS"
                                        else money(b.get("patient_pays_gbp"))))
    if b.get("preauth_ref"):
        lines.append(f"Insurer pre-authorisation: {b['preauth_ref']}")
    if b.get("payment_ref"):
        lines.append(f"Payment taken: {money(b.get('charged_gbp'))} ({b['payment_ref']})")
    return "\n".join(lines)


def sms(b: dict) -> tuple[str, dict]:
    """Text with NO health details: no service, provider type or reason."""
    first = b.get("for_first_name") or "you"
    when = fmt_start(b.get("start"))
    if when:
        text = (f"CareRoute: an appointment for {first} is confirmed for {when}. "
                f"Ref {b['booking_ref']}. Details are in the app.")
    else:
        text = f"CareRoute: your request for {first} has been sent. Ref {b['booking_ref']}."
    return text, {"first_name": first, "when": when or "", "reference": b["booking_ref"]}
