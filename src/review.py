"""Tier 3 of the cascade: the human.

Asks about anything the pipeline could not settle on its own, and returns
what it learned so the caller can write corrections back into the vendor
cache — every answer makes the cheaper tiers smarter and the system asks
less next month (decision D-4).
"""

import click

from src.models import UNCATEGORIZED, DirectLedgerEntry, Transaction
from src.p2p_detector import is_p2p_candidate


def _needs_category(transaction: Transaction, confidence_threshold: float) -> bool:
    return (
        transaction.category == UNCATEGORIZED
        or transaction.confidence < confidence_threshold
    )


def review_transactions(
    transactions: list[Transaction],
    categories: dict,
    confidence_threshold: float,
    prompt_fn=click.prompt,
    echo_fn=click.echo,
) -> tuple[list[DirectLedgerEntry], list[Transaction]]:
    """Mutates transactions in place (category/subcategory/flag).

    Returns (confirmed direct lend/borrow entries, user-corrected
    transactions to be written back into the vendor cache).

    Two independent reasons to stop on a row, and a row can hit both: it
    looks like money moving between you and a person (possible loan), or
    nothing confidently categorized it. Answering the loan question can
    resolve the row entirely, which is why the category prompt is skipped
    once a ledger entry is recorded or the row is skipped.
    """
    ledger_entries: list[DirectLedgerEntry] = []
    corrections: list[Transaction] = []
    category_names = list(categories.keys())

    for txn in transactions:
        is_candidate = is_p2p_candidate(txn, confidence_threshold)
        needs_category = _needs_category(txn, confidence_threshold)
        if not (is_candidate or needs_category):
            continue

        echo_fn(
            f"\n{txn.value_date}  {txn.description}  "
            f"{txn.amount} {txn.currency} {txn.direction.upper()}"
        )

        if is_candidate:
            choice = prompt_fn(
                "  Money lent or borrowed? [l]ent / [b]orrowed / [e]xpense / [s]kip",
                type=click.Choice(["l", "b", "e", "s"]),
            )
            if choice in ("l", "b"):
                person = str(prompt_fn("  Person's name")).strip()
                ledger_entries.append(DirectLedgerEntry(
                    person=person,
                    direction="lent" if choice == "l" else "borrowed",
                    amount=txn.amount,
                    currency=txn.currency,
                    entry_date=txn.value_date,
                    linked_transaction_hash=txn.dedup_hash,
                ))
                txn.flag = "Lent" if choice == "l" else "Borrowed"
                continue
            if choice == "s":
                continue
            # "e" — an ordinary expense after all; fall through to categorize.

        if needs_category:
            category = prompt_fn("  Category", type=click.Choice(category_names))
            subcategories = [s for s in (categories.get(category) or []) if s]
            subcategory = (
                prompt_fn("  Subcategory", type=click.Choice(subcategories))
                if subcategories
                else ""
            )
            txn.category = category
            txn.subcategory = subcategory
            txn.confidence = 1.0
            corrections.append(txn)

    return ledger_entries, corrections
