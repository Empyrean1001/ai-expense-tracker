"""Deterministic validation of a BankProfile before it is trusted.

The model that inferred a profile cannot be its own reviewer — these checks
come from a different epistemic source: accounting invariants (design
decision D-2). A profile that fails here is never cached and never imports
money.
"""

from datetime import date, timedelta

from src.models import BankProfile, Transaction
from src.profile import ProfileExecutionError, parse_rows_with_profile

_MIN_DATE = date(2000, 1, 1)


def _continuity_violations(transactions: list[Transaction]) -> int | None:
    """Count breaks in prev_balance ± amount == balance across consecutive
    rows. Returns None when fewer than two balances exist to compare."""
    with_balance = [t for t in transactions if t.balance is not None]
    if len(with_balance) < 2:
        return None
    violations = 0
    for prev, curr in zip(with_balance, with_balance[1:]):
        delta = curr.amount if curr.direction == "credit" else -curr.amount
        if prev.balance + delta != curr.balance:
            violations += 1
    return violations


def validate_profile(rows: list[list[str]], profile: BankProfile) -> list[str]:
    """Empty list = the profile is safe to cache and execute."""
    try:
        transactions = parse_rows_with_profile(rows, profile)
    except ProfileExecutionError as exc:
        return [str(exc)]

    errors: list[str] = []

    max_date = date.today() + timedelta(days=366)
    bad_dates = [
        str(t.value_date)
        for t in transactions
        if not (_MIN_DATE <= t.value_date <= max_date)
    ]
    if bad_dates:
        errors.append(
            f"Implausible dates parsed ({', '.join(bad_dates[:3])}) — "
            "the date format or day/month order is probably wrong."
        )

    # Balance continuity is the single check that catches a wrong amount
    # column, a wrong sign convention, and a wrong decimal separator at once:
    # any of them breaks the arithmetic chain. Statements come newest-first or
    # oldest-first, so accept either orientation.
    forward = _continuity_violations(transactions)
    if forward is not None and forward > 0:
        backward = _continuity_violations(list(reversed(transactions)))
        if backward > 0:
            errors.append(
                f"Balance continuity broken in both row orders "
                f"({forward} forward / {backward} backward violations) — "
                "amount column, debit/credit convention, or number locale is wrong."
            )

    return errors
