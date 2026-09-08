"""Insights over transaction history: top vendors, recurring charges,
month-over-month category deltas.

Every function expects a DataFrame already filtered to a single currency
(the caller groups by currency — figures across currencies are never mixed).
Only debits are considered spending.
"""

import numpy as np
import pandas as pd


def _debits(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["direction"] == "debit"]


def compute_top_vendors(df: pd.DataFrame, month: str, n: int = 5) -> pd.DataFrame:
    month_df = _debits(df)
    month_df = month_df[month_df["source_month"] == month]
    if month_df.empty:
        return pd.DataFrame(columns=["vendor", "amount"])
    grouped = month_df.groupby("vendor", as_index=False)["amount"].sum()
    return grouped.sort_values("amount", ascending=False).head(n).reset_index(drop=True)


def detect_recurring_charges(
    df: pd.DataFrame,
    min_occurrences: int = 3,
    amount_tolerance: float = 0.05,
) -> pd.DataFrame:
    """Vendors billing a near-identical amount across several distinct months.

    Counting distinct months rather than transactions is what separates a
    subscription from a shop visited three times in one week."""
    debit_df = _debits(df)
    rows = []
    for vendor, group in debit_df.groupby("vendor"):
        if not vendor or group["source_month"].nunique() < min_occurrences:
            continue
        mean_amount = group["amount"].mean()
        if mean_amount <= 0:
            continue
        if ((group["amount"] - mean_amount).abs() <= mean_amount * amount_tolerance).all():
            rows.append({
                "vendor": vendor,
                "avg_amount": round(mean_amount, 2),
                "occurrences": group["source_month"].nunique(),
            })
    return pd.DataFrame(rows, columns=["vendor", "avg_amount", "occurrences"])


def compute_month_over_month_deltas(df: pd.DataFrame) -> pd.DataFrame:
    debit_df = _debits(df)
    if debit_df.empty:
        return pd.DataFrame(
            columns=["category", "source_month", "amount", "prev_amount", "pct_change"]
        )
    monthly = debit_df.groupby(["category", "source_month"], as_index=False)["amount"].sum()
    monthly = monthly.sort_values(["category", "source_month"])
    monthly["prev_amount"] = monthly.groupby("category")["amount"].shift(1)
    monthly["pct_change"] = (
        (monthly["amount"] - monthly["prev_amount"]) / monthly["prev_amount"] * 100
    )
    # A category's first month has no baseline, and a jump from zero is not a
    # percentage — leave both blank rather than printing inf.
    monthly["pct_change"] = monthly["pct_change"].replace([np.inf, -np.inf], np.nan)
    return monthly.reset_index(drop=True)
