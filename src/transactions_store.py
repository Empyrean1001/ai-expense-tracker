"""Append-only master log of every categorized transaction.

The CSV is the exact source of truth: Decimal amounts are written as their
exact string form and survive round trips unchanged. `CSV_COLUMNS` is the
contract every downstream consumer (dashboard, insights, split search)
reads against.
"""

import csv
from pathlib import Path

import pandas as pd

from src.models import Transaction

CSV_COLUMNS = [
    "dedup_hash", "posted_at", "value_date", "description", "reference",
    "amount", "direction", "currency", "balance", "vendor",
    "description_note", "category", "subcategory", "confidence",
    "source_month", "flag",
]

# Columns that must stay strings on read. Without this, pandas turns blank
# cells into NaN, and a NaN flag/subcategory prints as "nan" in the workbook.
_STRING_COLUMNS = [
    "dedup_hash", "posted_at", "value_date", "description", "reference",
    "direction", "currency", "balance", "vendor", "description_note",
    "category", "subcategory", "source_month", "flag",
]


def load_existing_hashes(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {row["dedup_hash"] for row in csv.DictReader(f)}


def append_transactions(csv_path: Path, transactions: list[Transaction]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for txn in transactions:
            row = txn.model_dump(mode="json")
            row["balance"] = row["balance"] if row["balance"] is not None else ""
            writer.writerow({col: row[col] for col in CSV_COLUMNS})


def read_all_transactions(csv_path: Path) -> pd.DataFrame:
    """Full history as a DataFrame. `amount` becomes float here — this frame
    feeds display aggregation only, never money arithmetic; exact values stay
    in the CSV."""
    if not csv_path.exists():
        return pd.DataFrame(columns=CSV_COLUMNS)

    df = pd.read_csv(csv_path, dtype={col: str for col in _STRING_COLUMNS})
    for col in _STRING_COLUMNS:
        if col in df.columns:
            df[col] = df[col].fillna("")
    df["amount"] = df["amount"].astype(float)
    return df
