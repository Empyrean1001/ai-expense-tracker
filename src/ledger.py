"""Lend/borrow ledger: direct loans and split shares, per person.

Entries are stored as plain dicts so the file stays readable and
hand-editable; amounts are exact Decimal strings.
"""

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from src.models import DirectLedgerEntry, SplitLedgerEntry


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_ledger(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _append(path: Path, entry) -> None:
    entries = load_ledger(path)
    entries.append(json.loads(entry.model_dump_json()))
    save_ledger(path, entries)


def add_direct_entry(path: Path, entry: DirectLedgerEntry) -> None:
    _append(path, entry)


def add_split_entry(path: Path, entry: SplitLedgerEntry) -> None:
    _append(path, entry)


def settle_person(path: Path, person: str) -> int:
    """Mark every unsettled amount for one person as paid. Returns how many."""
    entries = load_ledger(path)
    settled = 0
    for entry in entries:
        if entry["type"] == "direct":
            if entry["person"] == person and entry["status"] == "unsettled":
                entry["status"] = "settled"
                settled += 1
        else:
            for share in entry["splits"]:
                if share["person"] == person and share["status"] == "unsettled":
                    share["status"] = "settled"
                    settled += 1
    save_ledger(path, entries)
    return settled


def get_outstanding_by_person(entries: list[dict]) -> dict[str, dict[str, Decimal]]:
    """Unsettled totals per person. A split share is always money owed to you."""
    outstanding: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"lent": Decimal("0"), "borrowed": Decimal("0")}
    )
    for entry in entries:
        if entry["type"] == "direct":
            if entry["status"] == "unsettled":
                outstanding[entry["person"]][entry["direction"]] += Decimal(str(entry["amount"]))
        else:
            for share in entry["splits"]:
                if share["status"] == "unsettled":
                    outstanding[share["person"]]["lent"] += Decimal(str(share["amount"]))
    return dict(outstanding)
