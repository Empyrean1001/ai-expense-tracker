"""Execute a BankProfile against raw statement rows.

A profile is a program the Schema Mapping Agent wrote once; this module is
its deterministic interpreter. During a normal monthly run of a known bank,
only this code touches the rows — never the LLM.
"""

import re
from src.descriptions import extract_vendor_and_note
from src.localeparse import parse_amount, parse_datetime_multi
from src.models import BankProfile, Transaction

_WHITESPACE = re.compile(r"\s+")
# The time-of-day tail of a strptime format: an optional space or ISO "T"
# separator, then everything from the first hour directive on.
_TIME_TAIL = re.compile(r"[ T]?%[HI].*$")


class ProfileExecutionError(ValueError):
    pass


def normalize_cell(cell: str) -> str:
    """Banks are inconsistent about their own spacing ('Chq /Ref No.',
    'Dr / Cr') — compare header cells with all whitespace stripped."""
    return _WHITESPACE.sub("", cell or "").lower()


def find_header_row(rows: list[list[str]], signature: list[str]) -> int | None:
    """Index of the row whose leading cells match the normalized signature."""
    want = [normalize_cell(cell) for cell in signature]
    for i, row in enumerate(rows):
        got = [normalize_cell(cell) for cell in row[: len(want)]]
        if got == want:
            return i
    return None


def expand_date_formats(formats: list[str]) -> list[str]:
    """Add the date-only form of every timestamp format, after the original.

    Banks routinely put a timestamp in one date column ('15-07-2026 07:48:39')
    and a plain date in another ('15-07-2026'), while a profile — inferred or
    hand-entered — tends to list only the timestamp format. The date part is
    the same in both, so the date-only variant is always safe to try.
    """
    expanded: list[str] = []
    for fmt in formats:
        for candidate in (fmt, _TIME_TAIL.sub("", fmt)):
            if candidate and candidate not in expanded:
                expanded.append(candidate)
    return expanded


def _cell(row: list[str], column_map: dict[str, int], key: str) -> str:
    index = column_map.get(key)
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def parse_rows_with_profile(rows: list[list[str]], profile: BankProfile) -> list[Transaction]:
    if not profile.currency:
        raise ProfileExecutionError(
            "Profile has no currency set — it must be filled before parsing."
        )
    header = find_header_row(rows, profile.header_signature)
    if header is None:
        raise ProfileExecutionError(
            "Could not find the transaction table header "
            f"{profile.header_signature} in the file."
        )

    sentinels = {normalize_cell(s) for s in profile.footer_sentinels}
    transactions = []

    for line_no, row in enumerate(rows[header + 1 :], start=header + 2):
        if not row or all(not (cell or "").strip() for cell in row):
            continue
        if normalize_cell(row[0]) in sentinels:
            break

        description = _cell(row, profile.column_map, "description")
        amount_raw = _cell(row, profile.column_map, "amount")
        debit_raw = _cell(row, profile.column_map, "debit_amount")
        credit_raw = _cell(row, profile.column_map, "credit_amount")
        if not description and not (amount_raw or debit_raw or credit_raw):
            break  # ran off the end of the table into footer text

        try:
            transactions.append(_parse_row(row, profile, description))
        except Exception as exc:
            raise ProfileExecutionError(f"Row {line_no}: {exc} — row was {row}") from exc

    if not transactions:
        raise ProfileExecutionError("The transaction table contained no parseable rows.")
    return transactions


def _parse_row(row: list[str], profile: BankProfile, description: str) -> Transaction:
    column_map = profile.column_map
    date_formats = expand_date_formats(profile.date_formats)

    posted_raw = _cell(row, column_map, "posted_at")
    value_raw = _cell(row, column_map, "value_date")
    posted = parse_datetime_multi(posted_raw or value_raw, date_formats)
    value_date = (
        parse_datetime_multi(value_raw, date_formats).date()
        if value_raw
        else posted.date()
    )

    if profile.amount_convention == "dual_drcr":
        amount = parse_amount(_cell(row, column_map, "amount"), profile.decimal_separator)
        marker = _cell(row, column_map, "dr_cr").upper()
        direction = "debit" if marker == profile.debit_marker.upper() else "credit"
    elif profile.amount_convention == "signed":
        signed = parse_amount(_cell(row, column_map, "amount"), profile.decimal_separator)
        is_debit = (signed < 0) == profile.signed_debit_negative
        direction, amount = ("debit" if is_debit else "credit"), abs(signed)
    else:  # separate
        debit_raw = _cell(row, column_map, "debit_amount")
        credit_raw = _cell(row, column_map, "credit_amount")
        if debit_raw:
            direction, amount = "debit", parse_amount(debit_raw, profile.decimal_separator)
        elif credit_raw:
            direction, amount = "credit", parse_amount(credit_raw, profile.decimal_separator)
        else:
            raise ValueError("neither debit nor credit column has a value")

    balance_raw = _cell(row, column_map, "balance")
    balance = parse_amount(balance_raw, profile.decimal_separator) if balance_raw else None

    vendor, note = extract_vendor_and_note(description, profile.description_convention)
    return Transaction(
        posted_at=posted,
        value_date=value_date,
        description=description,
        reference=_cell(row, column_map, "reference"),
        amount=amount,
        direction=direction,
        currency=profile.currency,
        balance=balance,
        vendor=vendor,
        description_note=note,
    )
