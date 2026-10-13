"""Pluggable conventions for pulling a vendor and payment note out of a raw
statement description. Which convention applies is a property of the bank
profile, not of the transaction."""

import re

_UPI_PREFIX = re.compile(r"^(?:REV-)?UPI/", re.IGNORECASE)
# The UPI reference (RRN) is 12 digits; it's the one segment whose position
# never varies relative to payee and note, so everything is anchored on it.
_UPI_REFERENCE = re.compile(r"^\d{10,}$")
# Newer exports put the payee's bank (first 4 letters of its IFSC code)
# right before the reference.
_BANK_CODE = re.compile(r"^[A-Za-z]{4}$")
_VALUE_DATE_SUFFIX = re.compile(r"\(Value Date:[^)]*\)?\s*$", re.IGNORECASE)
# Text the payment app fills in by itself. It says nothing about what a
# payment was for, so it must not count as a note: the note is what the
# payer typed, and both the categorizer and the loan detector rely on that.
_APP_BOILERPLATE = re.compile(
    r"^(upi|upi\s*intent|na|null|none|-+"
    r"|pay(ment)?\s*(to|for|via)\b.*|payvia.*|you are paying.*"
    r"|paid\s*(via|using|through)\b.*|sent\s*(from|via|using)\b.*)$",
    re.IGNORECASE,
)


def _upi_vendor_and_note(description: str) -> tuple[str, str]:
    text = _VALUE_DATE_SUFFIX.sub("", description).strip()
    segments = [s.strip() for s in text.split("/")[1:]]

    reference = next((i for i, s in enumerate(segments) if _UPI_REFERENCE.match(s)), None)
    if reference is None:
        # No reference found: fall back to the classic payee-first,
        # note-last reading.
        payee_parts = segments[:1]
        note = segments[-1] if len(segments) >= 3 else ""
    else:
        payee_parts = segments[:reference]
        if len(payee_parts) > 1 and _BANK_CODE.match(payee_parts[-1]):
            payee_parts = payee_parts[:-1]
        note = "/".join(segments[reference + 1:]).strip()

    vendor = " ".join("/".join(payee_parts).split()) or text
    if _APP_BOILERPLATE.match(note):
        note = ""
    return vendor, note


def extract_vendor_and_note(description: str, convention: str = "generic") -> tuple[str, str]:
    """Return (vendor, note) for one description.

    "upi": 'UPI/<payee>[/<bank code>]/<reference>/<note>' (and 'REV-UPI/...'
    reversals). The note is what the payer typed at payment time ('auto',
    'lunch'). It is the only clue to what a payment to an individual was for,
    so it is carried separately all the way to the model prompt.

    "generic": the cleaned description itself, no note.
    """
    text = description.strip()
    if convention == "upi" and _UPI_PREFIX.match(text):
        return _upi_vendor_and_note(text)
    return text, ""
