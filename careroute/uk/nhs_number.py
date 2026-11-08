"""
uk/nhs_number.py — NHS number validation (Modulus 11).

Every identifier that crosses a boundary (a token, a referral, a proxy link)
is checked before use. Demo numbers use the 999 range, which NHS England
reserves for test data and never issues to real patients.
"""

from __future__ import annotations

import re

_WEIGHTS = range(10, 1, -1)


def normalise(value: str | None) -> str:
    return re.sub(r"[\s-]", "", value or "")


def check_digit(first_nine: str) -> int | None:
    """The check digit for nine digits, or None if no valid number exists."""
    total = sum(int(d) * w for d, w in zip(first_nine, _WEIGHTS))
    digit = 11 - (total % 11)
    if digit == 11:
        return 0
    if digit == 10:
        return None
    return digit


def is_valid(value: str | None) -> bool:
    n = normalise(value)
    if not re.fullmatch(r"\d{10}", n):
        return False
    return check_digit(n[:9]) == int(n[9])


def is_test_number(value: str | None) -> bool:
    return is_valid(value) and normalise(value).startswith("999")


def make_test_number(seed: int) -> str:
    """Deterministic valid 999-range number (for demo data generation)."""
    for i in range(seed, seed + 100):
        nine = f"999{i:06d}"
        d = check_digit(nine)
        if d is not None:
            return nine + str(d)
    raise ValueError("no valid number near seed")


def pretty(value: str) -> str:
    n = normalise(value)
    return f"{n[:3]} {n[3:6]} {n[6:]}" if len(n) == 10 else value
