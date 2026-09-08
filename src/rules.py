"""Tier 0 of the categorization cascade: user-pinned rules. Deterministic,
instant, and they always win — a rule is the user telling us the answer."""

from src.models import CategorizationResult, Rule, Transaction


def apply_rules(transaction: Transaction, rules: list[Rule]) -> CategorizationResult | None:
    vendor = transaction.vendor.strip().lower()
    description = transaction.description.strip().lower()
    for rule in rules:
        pattern = rule.pattern.strip().lower()
        if rule.match == "exact":
            hit = vendor == pattern
        else:
            hit = pattern in vendor or pattern in description
        if hit:
            return CategorizationResult(
                vendor=transaction.vendor or rule.pattern,
                category=rule.category,
                subcategory=rule.subcategory,
                confidence=1.0,
            )
    return None
