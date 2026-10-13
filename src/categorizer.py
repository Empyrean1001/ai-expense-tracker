"""The categorization cascade: rules -> cache -> local LLM.

Each tier only sees what the previous tier couldn't settle; only LLM output
enters the cache (a rule already is permanent memory). Every automatic answer
is then checked against the money's direction — Income means money in, every
other category means money out — and a contradiction goes to review instead
of being trusted. Tier 3 — the human — lives in review.py.
"""

from src.models import (
    INCOME_CATEGORY,
    UNCATEGORIZED,
    CategorizationResult,
    Rule,
    Transaction,
)
from src.ollama_client import categorize_via_llm
from src.p2p_detector import looks_like_individual_name
from src.rules import apply_rules
from src.vendor_cache import normalize_vendor_key


def cache_key_for(transaction: Transaction) -> str:
    """What a cached categorization is actually keyed on.

    Direction: a payment to SWIGGY is Food & Dining, money back from SWIGGY
    is a refund — same vendor, different answer.

    For a merchant the vendor is otherwise the whole story. A person's name
    is not — the payment note is ('lunch' vs 'rent' vs 'auto'), so person
    keys include the note. Keying people on name alone would let the first
    note ever seen decide the category of every future payment to them.
    """
    vendor_key = normalize_vendor_key(transaction.vendor or transaction.description)
    key = f"{transaction.direction.upper()}|{vendor_key}"
    if looks_like_individual_name(transaction.vendor):
        return f"{key}|{transaction.description_note.strip().upper()}"
    return key


def direction_consistent(category: str, direction: str) -> bool:
    if category == UNCATEGORIZED:
        return True
    return (category == INCOME_CATEGORY) == (direction == "credit")


def _apply(transaction: Transaction, result: CategorizationResult) -> None:
    transaction.vendor = result.vendor or transaction.vendor
    transaction.category = result.category
    transaction.subcategory = result.subcategory
    transaction.confidence = result.confidence


def categorize_transactions(
    transactions: list[Transaction],
    *,
    rules: list[Rule],
    vendor_cache: dict[str, CategorizationResult],
    model,
    categories: dict,
    hints_text: str = "",
) -> list[Transaction]:
    """Mutates transactions in place; fills vendor_cache with new LLM results.
    The caller persists the cache."""
    for transaction in transactions:
        result = apply_rules(transaction, rules)
        if result is None:
            key = cache_key_for(transaction)
            result = vendor_cache.get(key)
            if result is None:
                result = categorize_via_llm(
                    transaction.description,
                    transaction.description_note,
                    categories,
                    model,
                    hints_text=hints_text,
                    direction=transaction.direction,
                    amount=f"{transaction.amount} {transaction.currency}",
                )
                if direction_consistent(result.category, transaction.direction):
                    vendor_cache[key] = result

        if not direction_consistent(result.category, transaction.direction):
            # An answer that contradicts where the money went can't be
            # right, however confident: send it to the human.
            result = CategorizationResult(
                vendor=result.vendor, category=UNCATEGORIZED, confidence=0.0
            )
        _apply(transaction, result)

    return transactions
