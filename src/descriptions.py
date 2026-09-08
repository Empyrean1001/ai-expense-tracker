"""Pluggable conventions for pulling a vendor and payment note out of a raw
statement description. Which convention applies is a property of the bank
profile, not of the transaction."""


def extract_vendor_and_note(description: str, convention: str = "generic") -> tuple[str, str]:
    """Return (vendor, note) for one description.

    "upi": 'UPI/<NAME>/<ref>/<note>' — the trailing note is what the payer
    typed at payment time ('auto', 'lunch') and is the only clue to what a
    payment to an individual was for, so it is carried separately all the way
    to the model prompt.

    "generic": the cleaned description itself, no note.
    """
    text = description.strip()
    if convention == "upi" and text.startswith("UPI/"):
        parts = text.split("/")
        vendor = parts[1].strip() if len(parts) > 1 else text
        note = parts[3].strip() if len(parts) > 3 else ""
        return vendor, note
    return text, ""
