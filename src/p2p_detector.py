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

# Words that mark a payee as a business ("ZOMATO LIMITED", "Amazon India").
_BUSINESS_WORDS = {
    "LIMITED", "LTD", "PRIVATE", "PVT", "INC", "LLP", "LLC", "CORP", "GMBH",
    "PLC", "INDIA", "TECHNOLOGIES", "TECHNOLOGY", "TECH", "SERVICES", "SERVICE",
    "SYSTEMS", "SOLUTIONS", "ENTERPRISES", "ENTERPRISE", "COMMERCE", "RETAIL",
    "TRADERS", "TRADING", "STORE", "STORES", "SHOP", "SUPERMARKET", "MART",
    "PHARMACY", "PHARMA", "MEDICAL", "MEDICALS", "HOSPITAL", "CLINIC",
    "RESTAURANT", "HOTEL", "CAFE", "BAKERY", "FOODS", "KITCHEN", "GARDEN",
    "BHAVAN", "SWEETS", "METRO", "RAILWAYS", "TRAVELS", "TOURS", "MOTORS",
    "AGENCY", "AGENCIES", "INSURANCE", "FINANCE", "BANK", "PAYMENTS",
    "NETWORKS", "INTERNATIONAL", "GLOBAL", "VENTURES", "INDUSTRIES",
    "CORPORATION", "COMPANY", "STUDIO", "CENTRE", "CENTER",
}
# Exports cut payee names off at ~15 characters ("APOLLO PHARMAC", "UBER
# INDIA SYS"). Only the last word of a name that long can be a cut-off
# business word; anywhere else, or in a short name, a word like "BAKER" is
# just a surname.
_TRUNCATION_LENGTH = 13


def _ends_in_cut_off_business_word(text: str) -> bool:
    last = text.split()[-1].upper()
    return (
        len(text) >= _TRUNCATION_LENGTH
        and len(last) >= 3
        and any(word.startswith(last) for word in _BUSINESS_WORDS)
    )


def looks_like_individual_name(vendor: str) -> bool:
    text = vendor.strip()
    if not _INDIVIDUAL_NAME_PATTERN.match(text):
        return False
    if any(word.upper() in _BUSINESS_WORDS for word in text.split()):
        return False
    return not _ends_in_cut_off_business_word(text)


def is_p2p_candidate(transaction: Transaction, confidence_threshold: float) -> bool:
    if not looks_like_individual_name(transaction.vendor):
        return False
    return (
        transaction.confidence < confidence_threshold
        or not transaction.description_note
    )
