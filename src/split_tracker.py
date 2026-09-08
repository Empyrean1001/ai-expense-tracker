"""Split tracker: record that others owe you part of an expense you paid.

Splits cannot be detected from a bank statement at all, so this is an
on-demand, LLM-free mechanism over transactions already imported (decision
D-10). The underlying transaction keeps its category and full amount — a
split only tracks what comes back.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

from src.models import SplitLedgerEntry, SplitShare

_CENT = Decimal("0.01")


def search_transactions(df: pd.DataFrame, term: str) -> pd.DataFrame:
    """Free-text search over description, vendor, amount and date.

    regex=False matters: a vendor like 'CAFE (MG ROAD)' must not have its
    parentheses read as a regex group."""
    if df.empty:
        return df
    term_lower = term.strip().lower()
    mask = (
        df["description"].str.lower().str.contains(term_lower, na=False, regex=False)
        | df["vendor"].str.lower().str.contains(term_lower, na=False, regex=False)
        | df["amount"].astype(str).str.contains(term_lower, na=False, regex=False)
        | df["value_date"].astype(str).str.contains(term_lower, na=False, regex=False)
    )
    return df[mask]


def build_equal_split(total_amount: Decimal, people: list[str]) -> list[SplitShare]:
    """Split the bill evenly, including yourself in the head count. Only the
    other people become ledger entries — your own share is spending, not a
    receivable."""
    per_person = (total_amount / (len(people) + 1)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return [SplitShare(person=person, amount=per_person) for person in people]


def build_custom_split(shares: dict[str, Decimal]) -> list[SplitShare]:
    return [SplitShare(person=person, amount=amount) for person, amount in shares.items()]


def validate_split(total_amount: Decimal, shares: list[SplitShare]) -> None:
    if not shares:
        raise ValueError("A split needs at least one other person.")
    if any(share.amount <= 0 for share in shares):
        raise ValueError("Every share must be greater than zero.")
    total_shared = sum((share.amount for share in shares), Decimal("0"))
    if total_shared >= total_amount:
        raise ValueError(
            f"Split shares total {total_shared}, which is not less than the "
            f"transaction total of {total_amount} — your own share must be "
            "greater than zero."
        )


def create_split_entry(
    transaction_hash: str,
    total_amount: Decimal,
    entry_date: date,
    shares: list[SplitShare],
    currency: str = "",
) -> SplitLedgerEntry:
    validate_split(total_amount, shares)
    return SplitLedgerEntry(
        linked_transaction_hash=transaction_hash,
        total_amount=total_amount,
        currency=currency,
        splits=shares,
        entry_date=entry_date,
    )
