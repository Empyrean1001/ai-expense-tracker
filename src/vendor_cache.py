"""Tier 1 of the cascade: learned vendor -> categorization memory.

Keeps categorization consistent across months and means a vendor is sent to
the model at most once."""

import json
from pathlib import Path

from src.models import INCOME_CATEGORY, CategorizationResult


def normalize_vendor_key(vendor: str) -> str:
    return vendor.strip().upper()


def load_vendor_cache(path: Path) -> dict[str, CategorizationResult]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cache = {}
    for key, value in raw.items():
        result = CategorizationResult(**value)
        # Keys gained a direction prefix ("DEBIT|SWIGGY"). For an older,
        # unprefixed entry the category says which way the money went:
        # Income is money in, everything else money out.
        if not key.startswith(("DEBIT|", "CREDIT|")):
            direction = "CREDIT" if result.category == INCOME_CATEGORY else "DEBIT"
            key = f"{direction}|{key}"
        cache[key] = result
    return cache


def save_vendor_cache(path: Path, cache: dict[str, CategorizationResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {key: result.model_dump() for key, result in cache.items()}, f, indent=2
        )
