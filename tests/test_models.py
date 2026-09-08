import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models import (
    BankProfile,
    CategorizationResult,
    DirectLedgerEntry,
    SplitLedgerEntry,
    SplitShare,
    Transaction,
    compute_dedup_hash,
)


def make_txn(**overrides) -> Transaction:
    base = dict(
        posted_at=datetime(2026, 7, 31, 7, 48, 39),
        value_date=date(2026, 7, 31),
        description="UPI/SURESH NAIK LA/700000000102/bus",
        reference="UPI-700000000104",
        amount=Decimal("40.00"),
        direction="debit",
        currency="INR",
        balance=Decimal("133116.37"),
    )
    base.update(overrides)
    return Transaction(**base)


class TestTransaction:
    def test_amount_is_decimal_from_string(self):
        txn = make_txn(amount="40.00")
        assert txn.amount == Decimal("40.00")
        assert isinstance(txn.amount, Decimal)

    def test_derived_fields_filled(self):
        txn = make_txn()
        assert txn.source_month == "2026-07"
        assert txn.dedup_hash == compute_dedup_hash(
            date(2026, 7, 31), "UPI-700000000104", Decimal("40.00"), "debit", "INR"
        )

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError):
            make_txn(amount=Decimal("-1"))

    def test_currency_must_be_three_letters(self):
        with pytest.raises(ValidationError):
            make_txn(currency="RUPEES")

    def test_json_serializes_decimals_as_strings(self):
        data = json.loads(make_txn().model_dump_json())
        assert data["amount"] == "40.00"
        assert data["balance"] == "133116.37"

    def test_json_round_trip_preserves_exact_amount(self):
        txn = make_txn(amount=Decimal("0.10"))
        restored = Transaction(**json.loads(txn.model_dump_json()))
        assert restored.amount == Decimal("0.10")


class TestBankProfile:
    def make(self, **overrides) -> BankProfile:
        base = dict(
            header_signature=["sl.no.", "date", "description", "amount", "dr/cr"],
            column_map={"value_date": 1, "description": 2, "amount": 3, "dr_cr": 4},
            date_formats=["%d-%m-%Y"],
            amount_convention="dual_drcr",
        )
        base.update(overrides)
        return BankProfile(**base)

    def test_valid_dual_drcr(self):
        assert self.make().amount_convention == "dual_drcr"

    def test_dual_drcr_requires_drcr_column(self):
        with pytest.raises(ValidationError, match="dr_cr"):
            self.make(column_map={"value_date": 1, "description": 2, "amount": 3})

    def test_separate_requires_debit_and_credit_columns(self):
        with pytest.raises(ValidationError, match="credit_amount"):
            self.make(
                amount_convention="separate",
                column_map={"value_date": 1, "description": 2, "debit_amount": 3},
            )

    def test_needs_at_least_one_date_column(self):
        with pytest.raises(ValidationError, match="posted_at/value_date"):
            self.make(column_map={"description": 2, "amount": 3, "dr_cr": 4})


class TestLedgerModels:
    def test_direct_entry_json_decimal(self):
        entry = DirectLedgerEntry(
            person="Rahul",
            direction="lent",
            amount=Decimal("500.50"),
            entry_date=date(2026, 7, 12),
            linked_transaction_hash="h1",
        )
        assert json.loads(entry.model_dump_json())["amount"] == "500.50"

    def test_split_entry_json_decimal(self):
        entry = SplitLedgerEntry(
            linked_transaction_hash="h2",
            total_amount=Decimal("2000"),
            entry_date=date(2026, 7, 15),
            splits=[SplitShare(person="Priya", amount=Decimal("500"))],
        )
        data = json.loads(entry.model_dump_json())
        assert data["total_amount"] == "2000"
        assert data["splits"][0]["amount"] == "500"


def test_categorization_result_defaults_degrade_to_review():
    result = CategorizationResult()
    assert result.category == "Other/Uncategorized"
    assert result.confidence == 0.0
