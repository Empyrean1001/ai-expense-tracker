from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.ledger import (
    add_direct_entry,
    add_split_entry,
    get_outstanding_by_person,
    load_ledger,
    settle_person,
)
from src.models import DirectLedgerEntry, SplitLedgerEntry, SplitShare
from src.review import review_transactions
from src.split_tracker import (
    build_equal_split,
    create_split_entry,
    search_transactions,
    validate_split,
)
from src.transactions_store import (
    CSV_COLUMNS,
    append_transactions,
    load_existing_hashes,
    read_all_transactions,
)
from tests.test_categorizer import make_txn

CATEGORIES = {"Transport": ["Fuel", "Public Transit"], "Other/Uncategorized": [""]}


class TestReview:
    def test_loan_recorded_and_category_prompt_skipped(self):
        txn = make_txn("UPI/RAHUL SHARMA/1/x", vendor="RAHUL SHARMA", confidence=0.1)
        answers = iter(["l", "Rahul Sharma"])
        entries, corrections = review_transactions(
            [txn], CATEGORIES, 0.6,
            prompt_fn=lambda *a, **k: next(answers), echo_fn=lambda *a: None,
        )
        assert len(entries) == 1
        assert entries[0].person == "Rahul Sharma"
        assert entries[0].amount == Decimal("100")
        assert entries[0].currency == "INR"
        assert txn.flag == "Lent"
        assert corrections == []  # loan answer resolved the row

    def test_expense_answer_falls_through_to_category(self):
        txn = make_txn("UPI/ARSHAD AHMED/1/x", vendor="ARSHAD AHMED", confidence=0.1)
        answers = iter(["e", "Transport", "Public Transit"])
        entries, corrections = review_transactions(
            [txn], CATEGORIES, 0.6,
            prompt_fn=lambda *a, **k: next(answers), echo_fn=lambda *a: None,
        )
        assert entries == []
        assert txn.category == "Transport"
        assert txn.subcategory == "Public Transit"
        assert txn.confidence == 1.0
        assert corrections == [txn]  # caller writes this back into the cache

    def test_confident_rows_not_prompted(self):
        txn = make_txn("SWIGGY", vendor="SWIGGY", confidence=0.95, category="Food & Dining")
        entries, corrections = review_transactions(
            [txn], CATEGORIES, 0.6,
            prompt_fn=lambda *a, **k: pytest.fail("should not prompt"),
            echo_fn=lambda *a: None,
        )
        assert entries == [] and corrections == []


class TestTransactionsStore:
    def test_round_trip_preserves_exact_amounts(self, tmp_path):
        path = tmp_path / "t.csv"
        txn = make_txn("X", vendor="X", amount=Decimal("0.10"))
        append_transactions(path, [txn])
        raw = path.read_text()
        assert ",0.10," in raw  # exact Decimal string in the CSV
        df = read_all_transactions(path)
        assert list(df.columns) == CSV_COLUMNS
        assert df.shape[0] == 1
        assert df.iloc[0]["currency"] == "INR"
        assert load_existing_hashes(path) == {txn.dedup_hash}

    def test_missing_file(self, tmp_path):
        assert load_existing_hashes(tmp_path / "no.csv") == set()
        assert read_all_transactions(tmp_path / "no.csv").empty


class TestLedger:
    def test_outstanding_and_settle(self, tmp_path):
        path = tmp_path / "ledger.json"
        add_direct_entry(path, DirectLedgerEntry(
            person="Rahul", direction="lent", amount=Decimal("500"),
            entry_date=date(2026, 7, 12), linked_transaction_hash="h1",
        ))
        add_split_entry(path, SplitLedgerEntry(
            linked_transaction_hash="h2", total_amount=Decimal("2000"),
            entry_date=date(2026, 7, 15),
            splits=[SplitShare(person="Rahul", amount=Decimal("300")),
                    SplitShare(person="Priya", amount=Decimal("500"))],
        ))
        outstanding = get_outstanding_by_person(load_ledger(path))
        assert outstanding["Rahul"]["lent"] == Decimal("800")
        assert outstanding["Priya"]["lent"] == Decimal("500")
        assert settle_person(path, "Rahul") == 2
        outstanding = get_outstanding_by_person(load_ledger(path))
        assert "Rahul" not in outstanding or outstanding["Rahul"]["lent"] == 0
        assert outstanding["Priya"]["lent"] == Decimal("500")


class TestSplitTracker:
    def test_equal_split_excludes_own_share(self):
        shares = build_equal_split(Decimal("2000"), ["Rahul", "Priya", "Amit"])
        assert [s.amount for s in shares] == [Decimal("500.00")] * 3

    def test_over_total_rejected(self):
        with pytest.raises(ValueError, match="not less than"):
            validate_split(Decimal("1000"), [
                SplitShare(person="A", amount=Decimal("500")),
                SplitShare(person="B", amount=Decimal("600")),
            ])

    def test_create_entry(self):
        entry = create_split_entry(
            "h1", Decimal("2000"), date(2026, 7, 15),
            build_equal_split(Decimal("2000"), ["Rahul"]), currency="INR",
        )
        assert entry.total_amount == Decimal("2000")
        assert entry.splits[0].amount == Decimal("1000.00")

    def test_search_is_literal_not_regex(self, tmp_path):
        path = tmp_path / "t.csv"
        append_transactions(path, [make_txn("CAFE (MG ROAD) BILL", vendor="CAFE (MG ROAD)")])
        df = read_all_transactions(path)
        assert len(search_transactions(df, "CAFE (MG ROAD)")) == 1
        assert len(search_transactions(df, "no match")) == 0
