"""Canonical data models shared by every module.

Money is always Decimal. Amounts are stored positive with an explicit
debit/credit direction; sign conventions from source formats are resolved at
the ingestion boundary and never leak past it.
"""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, model_validator

UNCATEGORIZED = "Other/Uncategorized"

Direction = Literal["debit", "credit"]


def compute_dedup_hash(
    value_date: date, reference: str, amount: Decimal, direction: str, currency: str
) -> str:
    key = f"{value_date.isoformat()}|{reference}|{amount}|{direction}|{currency}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Transaction(BaseModel):
    """One bank transaction in canonical form, enriched as it moves through
    the pipeline."""

    dedup_hash: str = ""
    posted_at: datetime
    value_date: date
    description: str
    reference: str = ""
    amount: Decimal = Field(ge=0)
    direction: Direction
    currency: str = Field(min_length=3, max_length=3)
    balance: Decimal | None = None
    vendor: str = ""
    description_note: str = ""
    category: str = UNCATEGORIZED
    subcategory: str = ""
    confidence: float = 0.0
    source_month: str = ""
    flag: str = ""

    @model_validator(mode="after")
    def _fill_derived(self) -> "Transaction":
        if not self.source_month:
            self.source_month = self.value_date.strftime("%Y-%m")
        if not self.dedup_hash:
            self.dedup_hash = compute_dedup_hash(
                self.value_date, self.reference, self.amount, self.direction, self.currency
            )
        return self

    @field_serializer("amount", "balance", when_used="json")
    def _decimal_as_str(self, value: Decimal | None) -> str | None:
        return None if value is None else str(value)


class CategorizationResult(BaseModel):
    """What a categorization tier returns for one transaction.

    Every field has a default: this doubles as the JSON schema handed to a
    small local model, and a missing field should degrade to "review this one"
    rather than blow up the run.
    """

    vendor: str = ""
    category: str = UNCATEGORIZED
    subcategory: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class BankProfile(BaseModel):
    """A declarative description of one bank's CSV/XLSX statement layout.

    Worked out once per bank (detected from the file, or from LLM header
    labels, or mapped interactively), validated deterministically, cached by
    header fingerprint, and executed by plain code on every later statement
    from the same bank — no LLM sees the rows of a known format.
    """

    bank_name: str = ""
    header_signature: list[str] = Field(min_length=1)
    footer_sentinels: list[str] = Field(default_factory=list)
    # canonical field -> 0-based column index. Recognized keys: posted_at,
    # value_date, description, reference, amount, dr_cr, debit_amount,
    # credit_amount, balance.
    column_map: dict[str, int]
    date_formats: list[str] = Field(min_length=1)
    decimal_separator: Literal[".", ","] = "."
    amount_convention: Literal["dual_drcr", "signed", "separate"]
    debit_marker: str = "DR"
    signed_debit_negative: bool = True
    currency: str = ""
    description_convention: Literal["upi", "generic"] = "generic"

    @model_validator(mode="after")
    def _check_columns(self) -> "BankProfile":
        required = {"description"}
        if self.amount_convention == "dual_drcr":
            required |= {"amount", "dr_cr"}
        elif self.amount_convention == "signed":
            required |= {"amount"}
        else:  # separate
            required |= {"debit_amount", "credit_amount"}
        if "posted_at" not in self.column_map and "value_date" not in self.column_map:
            raise ValueError("column_map needs at least one of posted_at/value_date")
        missing = required - self.column_map.keys()
        if missing:
            raise ValueError(
                f"column_map is missing {sorted(missing)} required by "
                f"amount_convention={self.amount_convention!r}"
            )
        return self


class Rule(BaseModel):
    """A user-pinned tier-0 categorization rule. Always wins."""

    pattern: str = Field(min_length=1)
    match: Literal["exact", "contains"] = "contains"
    category: str
    subcategory: str = ""


class DirectLedgerEntry(BaseModel):
    """A whole transaction that was actually a loan to/from one person."""

    type: Literal["direct"] = "direct"
    person: str
    direction: Literal["lent", "borrowed"]
    amount: Decimal
    currency: str = ""
    entry_date: date
    status: Literal["unsettled", "settled"] = "unsettled"
    linked_transaction_hash: str

    @field_serializer("amount", when_used="json")
    def _decimal_as_str(self, value: Decimal) -> str:
        return str(value)


class SplitShare(BaseModel):
    person: str
    amount: Decimal
    status: Literal["unsettled", "settled"] = "unsettled"

    @field_serializer("amount", when_used="json")
    def _decimal_as_str(self, value: Decimal) -> str:
        return str(value)


class SplitLedgerEntry(BaseModel):
    """A real expense you paid in full, where others owe you part of it.

    The linked transaction keeps its own category and full amount — this
    record only tracks what comes back.
    """

    type: Literal["split"] = "split"
    linked_transaction_hash: str
    total_amount: Decimal
    currency: str = ""
    splits: list[SplitShare]
    entry_date: date

    @field_serializer("total_amount", when_used="json")
    def _decimal_as_str(self, value: Decimal) -> str:
        return str(value)
