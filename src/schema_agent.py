"""Fallbacks for statement layouts that deterministic detection
(layout_detect.py) can't read with confidence.

1. The LLM labels the header cells — a language task small local models are
   good at. It is never asked to read the cell grid or transcribe header
   text: a 7B model does that badly (it invents header names), so code finds
   the header row, copies its text, and builds and validates the profile
   from the model's labels (decision D-15).
2. The human maps the columns interactively: pre-filled with everything that
   was detected, every answer checked against the file as it's given, and
   no question that needs strptime syntax to answer.
"""

import re
from datetime import datetime
from typing import Literal

import click
from pydantic import BaseModel

from src.layout_detect import (
    build_profiles,
    detect_currency,
    detect_date_formats,
    detect_decimal_separator,
    find_header_row_index,
    label_headers_by_name,
    pick_debit_marker,
    table_rows,
)
from src.localeparse import human_date_pattern_to_strptime
from src.models import BankProfile
from src.profile_validator import validate_profile

ColumnLabel = Literal[
    "posted_at", "value_date", "description", "reference", "amount", "dr_cr",
    "debit_amount", "credit_amount", "balance", "other",
]

LABEL_GUIDE = """\
posted_at      - the transaction / booking / posting date
value_date     - the value date, if there is a separate one
description    - the narration / purpose / payee text
reference      - a cheque, reference or transaction-id number
amount         - a single amount column (signed, or paired with a dr_cr column)
dr_cr          - a column marking each amount as debit or credit (e.g. DR/CR)
debit_amount   - money-out column, when debits and credits have separate columns
credit_amount  - money-in column, when debits and credits have separate columns
balance        - the running balance after each transaction
other          - anything else (serial numbers, a second marker column, ...)"""


class HeaderLabels(BaseModel):
    """What the model returns: one label per column, in column order."""

    labels: list[ColumnLabel]


class SchemaInferenceError(Exception):
    pass


def build_label_prompt(header: list[str], data: list[list[str]], feedback: str = "") -> str:
    columns = []
    for i, name in enumerate(header):
        examples = [r[i].strip() for r in data[:3] if i < len(r) and r[i].strip()]
        shown = ", ".join(f'"{e[:40]}"' for e in examples) or "(empty)"
        columns.append(f'[{i}] "{name}"  e.g. {shown}')
    prompt = (
        "Label each column of this bank statement table by what it contains.\n"
        f"Allowed labels:\n{LABEL_GUIDE}\n\n"
        f"Return exactly {len(header)} labels, one per column, in order.\n"
    )
    if feedback:
        prompt += f"\nYour previous labels were rejected: {feedback}\nFix them.\n"
    return prompt + "\nColumns:\n" + "\n".join(columns) + "\n"


def _labels_to_column_map(labels: list[str]) -> dict[str, int]:
    column_map: dict[str, int] = {}
    for index, label in enumerate(labels):
        if label != "other" and label not in column_map:
            column_map[label] = index
    return column_map


def infer_profile(rows: list[list[str]], model, max_attempts: int = 2) -> BankProfile:
    """`model.invoke(prompt) -> HeaderLabels`. Each rejected attempt feeds
    the reason back into the retry prompt."""
    header_index = find_header_row_index(rows)
    if header_index is None:
        raise SchemaInferenceError("Could not find a transaction table in this file.")
    header = rows[header_index]
    data = table_rows(rows, header_index)
    currency = detect_currency(rows, header_index)

    feedback = ""
    for _ in range(max_attempts):
        try:
            result = model.invoke(build_label_prompt(header, data, feedback))
        except Exception as exc:
            feedback = f"the response could not be read ({str(exc)[:200]})"
            continue
        if result is None or len(result.labels) != len(header):
            got = 0 if result is None else len(result.labels)
            feedback = f"expected {len(header)} labels, got {got}"
            continue

        column_map = _labels_to_column_map(result.labels)
        candidates = build_profiles(rows, header_index, column_map, currency)
        if not candidates:
            feedback = (
                f"labels {result.labels} don't fit the data — date columns must "
                "hold dates, money columns numbers, and there must be a description "
                "and either amount (+ optional dr_cr) or debit_amount + credit_amount"
            )
            continue
        errors: list[str] = []
        for candidate in candidates:
            errors = validate_profile(
                rows, candidate.model_copy(update={"currency": candidate.currency or "XXX"})
            )
            if not errors:
                return candidate
        feedback = "; ".join(errors)

    raise SchemaInferenceError(
        f"Could not work out this file's columns automatically ({feedback})."
    )


def _ask_index(prompt_fn, echo_fn, question: str, width: int, default: int | None, optional: bool):
    """Ask for a column index; re-ask until it's a valid one (or blank, when
    optional)."""
    while True:
        suffix = " (blank if none)" if optional else ""
        kwargs = {"default": "" if default is None else str(default), "show_default": default is not None}
        answer = str(prompt_fn(f"{question}{suffix}", **kwargs)).strip()
        if not answer and optional:
            return None
        if answer.isdigit() and int(answer) < width:
            return int(answer)
        echo_fn(f"  Please enter a column number between 0 and {width - 1}.")


def interactive_column_mapping(rows: list[list[str]], prompt_fn, echo_fn) -> BankProfile:
    """Manual fallback, only ever needed once per bank."""
    echo_fn("\nI couldn't work out this file's columns automatically — "
            "let's map them together (only needed once per bank).")

    header_index = find_header_row_index(rows)
    if header_index is not None:
        echo_fn(f"\nThe table header looks like row {header_index}:")
        echo_fn("  " + " | ".join(rows[header_index]))
        confirmed = str(prompt_fn("Is that the header row? [y/n]",
                                  type=click.Choice(["y", "n"]), default="y")).strip().lower()
        if confirmed != "y":
            header_index = None
    if header_index is None:
        echo_fn("\nFile preview (row number: cells):")
        for i, row in enumerate(rows[:25]):
            echo_fn(f"  row {i}: " + " | ".join(row))
        while True:
            answer = str(prompt_fn("Which row number is the table header?")).strip()
            if answer.isdigit() and int(answer) < len(rows) and table_rows(rows, int(answer)):
                header_index = int(answer)
                break
            echo_fn("  That row isn't followed by any table rows — try again.")

    header = rows[header_index]
    data = table_rows(rows, header_index)
    width = len(header)
    guess = label_headers_by_name(header)

    echo_fn("\nColumns:")
    for j, name in enumerate(header):
        example = data[0][j] if data and j < len(data[0]) else ""
        echo_fn(f"  [{j}] {name or '(no name)'}   e.g. {example}")

    def column(field: str, question: str, optional: bool = False) -> int | None:
        return _ask_index(prompt_fn, echo_fn, question, width, guess.get(field), optional)

    column_map: dict[str, int] = {}
    column_map["posted_at"] = column("posted_at", "Column number of the transaction date?")
    value_date = column("value_date", "Column number of the value date?", optional=True)
    if value_date is not None:
        column_map["value_date"] = value_date
    column_map["description"] = column("description", "Column number of the description?")
    reference = column("reference", "Column number of the reference number?", optional=True)
    if reference is not None:
        column_map["reference"] = reference

    default_style = ("1" if "dr_cr" in guess else "3" if "debit_amount" in guess else "2")
    style = str(prompt_fn(
        "How are amounts shown? [1] one amount column + a DR/CR column, "
        "[2] one signed amount column, [3] separate debit and credit columns",
        type=click.Choice(["1", "2", "3"]), default=default_style,
    )).strip()
    if style == "1":
        column_map["amount"] = column("amount", "Column number of the amount?")
        column_map["dr_cr"] = column("dr_cr", "Column number of the DR/CR marker?")
    elif style == "2":
        column_map["amount"] = column("amount", "Column number of the amount?")
    else:
        column_map["debit_amount"] = column("debit_amount", "Column number of debits (money out)?")
        column_map["credit_amount"] = column("credit_amount", "Column number of credits (money in)?")
    balance = column("balance", "Column number of the running balance?", optional=True)
    if balance is not None:
        column_map["balance"] = balance

    # Date format: detected from the column itself; only asked if that fails,
    # in plain 'dd-mm-yyyy' terms, and checked against the file.
    date_formats: list[str] = []
    for field in ("posted_at", "value_date"):
        if field not in column_map:
            continue
        samples = [r[column_map[field]] for r in data if column_map[field] < len(r)]
        found = detect_date_formats(samples)
        while not found:
            example = next((s for s in samples if s.strip()), "")
            pattern = str(prompt_fn(
                f"How are dates written in column {column_map[field]} (e.g. '{example}')? "
                "Write it like dd-mm-yyyy hh:mm:ss"
            )).strip()
            fmt = human_date_pattern_to_strptime(pattern)
            if _format_fits(samples, fmt):
                found = [fmt]
            else:
                echo_fn(f"  '{pattern}' doesn't match '{example}' — try again.")
        if found[0] not in date_formats:
            date_formats.append(found[0])

    money = [r[c] for f in ("amount", "debit_amount", "credit_amount", "balance")
             if f in column_map for r in data
             for c in [column_map[f]] if c < len(r) and r[c].strip()]
    debit_marker = "DR"
    if style == "1":
        markers = sorted({r[column_map["dr_cr"]].strip() for r in data
                          if column_map["dr_cr"] < len(r) and r[column_map["dr_cr"]].strip()})
        debit_marker = pick_debit_marker(markers)
        if markers and debit_marker not in markers:
            debit_marker = str(prompt_fn(
                f"Which of these means money out? {', '.join(markers)}",
                type=click.Choice(markers),
            )).strip()

    currency = detect_currency(rows, header_index)
    while not re.fullmatch(r"[A-Z]{3}", currency):
        currency = str(prompt_fn(
            "Which currency is this account in? (3-letter code, e.g. INR, USD, EUR)"
        )).strip().upper()

    descriptions = [r[column_map["description"]] for r in data if column_map["description"] < len(r)]
    upi_share = sum(d.startswith("UPI/") for d in descriptions) / max(len(descriptions), 1)

    return BankProfile(
        header_signature=header,
        column_map=column_map,
        date_formats=date_formats,
        decimal_separator=detect_decimal_separator(money),
        amount_convention={"1": "dual_drcr", "2": "signed", "3": "separate"}[style],
        debit_marker=debit_marker,
        currency=currency,
        description_convention="upi" if upi_share >= 0.1 else "generic",
    )


def _format_fits(samples: list[str], fmt: str) -> bool:
    """Does one specific format parse every non-blank sample?"""
    values = [s.strip() for s in samples if s and s.strip()]
    if not values:
        return False
    try:
        for value in values:
            datetime.strptime(value, fmt)
    except ValueError:
        return False
    return True
