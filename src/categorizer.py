"""The categorization cascade: rules -> cache -> local LLM.

Each tier only sees what the previous tier couldn't settle; only LLM output
enters the cache (a rule already is permanent memory). Tier 3 — the human —
lives in review.py.
"""

from src.models import CategorizationResult, Rule, Transaction
from src.ollama_client import categorize_via_llm
from src.p2p_detector import looks_like_individual_name
from src.rules import apply_rules
from src.vendor_cache import normalize_vendor_key


def cache_key_for(transaction: Transaction) -> str:
    """What a cached categorization is actually keyed on.

    For a merchant the vendor alone is the whole story. A person's name is
    not — the payment note is ('lunch' vs 'rent' vs 'auto'), so person keys
    include the note. Keying people on name alone would let the first note
    ever seen decide the category of every future payment to that person.
    """
    vendor_key = normalize_vendor_key(transaction.vendor or transaction.description)
    if looks_like_individual_name(transaction.vendor):
        return f"{vendor_key}|{transaction.description_note.strip().upper()}"
    return vendor_key


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
        ruled = apply_rules(transaction, rules)
        if ruled is not None:
            _apply(transaction, ruled)
            continue

        key = cache_key_for(transaction)
        if key not in vendor_cache:
            vendor_cache[key] = categorize_via_llm(
                transaction.description,
                transaction.description_note,
                categories,
                model,
                hints_text=hints_text,
            )
        _apply(transaction, vendor_cache[key])

    return transactions
