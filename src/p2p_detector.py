"""Heuristic for surfacing possible person-to-person loans.

Real statements are full of payments to individuals that are ordinary small
expenses (auto fare, paying a shopkeeper), so "paid a person" alone is far
too broad. We only ask when the payment is to a person AND the pipeline
could not confidently explain what it was for. This never decides lend vs
borrow — that is always the user's call.
"""

import re

from src.models import Transaction

# Two to four plain alphabetic words: "RAHUL SHARMA", "MANOJ KUMAR S".
# Anything with a digit or symbol ("BMTC BUS KA01F2", "SWIGGY*ORDER123") is a
# merchant string, and a single word is almost always a brand.
_INDIVIDUAL_NAME_PATTERN = re.compile(r"^[A-Za-z]+(?:\s+[A-Za-z]+){1,3}$")


def looks_like_individual_name(vendor: str) -> bool:
    return bool(_INDIVIDUAL_NAME_PATTERN.match(vendor.strip()))


def is_p2p_candidate(transaction: Transaction, confidence_threshold: float) -> bool:
    if not looks_like_individual_name(transaction.vendor):
        return False
    return (
        transaction.confidence < confidence_threshold
        or not transaction.description_note
    )
