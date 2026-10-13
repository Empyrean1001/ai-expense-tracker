from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.categorizer import cache_key_for, categorize_transactions
from src.config_loader import load_categories, load_locale_hints, load_rules
from src.models import CategorizationResult, Rule, Transaction
from src.ollama_client import build_prompt
from src.p2p_detector import is_p2p_candidate, looks_like_individual_name
from src.vendor_cache import load_vendor_cache, save_vendor_cache

PROJECT = Path(__file__).parent.parent


def make_txn(description: str, vendor: str = "", note: str = "", **overrides) -> Transaction:
    base = dict(
        posted_at=datetime(2026, 7, 15, 12, 0),
        value_date=date(2026, 7, 15),
        description=description,
        amount=Decimal("100"),
        direction="debit",
        currency="INR",
        vendor=vendor,
        description_note=note,
    )
    base.update(overrides)
    return Transaction(**base)


class CountingModel:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or CategorizationResult(
            vendor="Swiggy", category="Food & Dining", subcategory="Delivery", confidence=0.9
        )

    def invoke(self, prompt):
        self.calls += 1
        return self.result


class FailingModel:
    def invoke(self, prompt):
        raise RuntimeError("model exploded")


class TestCascade:
    def test_rule_short_circuits_cache_and_model(self):
        txn = make_txn("EDEKA SAGT DANKE", vendor="EDEKA MUENCHEN")
        rule = Rule(pattern="EDEKA", match="contains", category="Food & Dining", subcategory="Groceries")
        model = CountingModel()
        cache: dict = {}
        categorize_transactions([txn], rules=[rule], vendor_cache=cache, model=model, categories={})
        assert txn.category == "Food & Dining"
        assert txn.confidence == 1.0
        assert model.calls == 0
        assert cache == {}  # rules are not cached — they already are memory

    def test_cache_hit_skips_model(self):
        txn = make_txn("SWIGGY*ORDER1", vendor="SWIGGY")
        cache = {"DEBIT|SWIGGY": CategorizationResult(
            vendor="Swiggy", category="Food & Dining", subcategory="Delivery", confidence=0.95
        )}
        model = CountingModel()
        categorize_transactions([txn], rules=[], vendor_cache=cache, model=model, categories={})
        assert model.calls == 0
        assert txn.vendor == "Swiggy"

    def test_new_vendor_goes_to_model_once(self):
        txns = [make_txn("SWIGGY*ORDER1", vendor="SWIGGY"),
                make_txn("SWIGGY*ORDER2", vendor="SWIGGY")]
        model = CountingModel()
        cache: dict = {}
        categorize_transactions(txns, rules=[], vendor_cache=cache, model=model, categories={})
        assert model.calls == 1  # second one hit the fresh cache entry
        assert all(t.category == "Food & Dining" for t in txns)

    def test_failing_model_degrades_to_review(self):
        txn = make_txn("MYSTERY SHOP 42", vendor="MYSTERY SHOP 42")
        cache: dict = {}
        categorize_transactions([txn], rules=[], vendor_cache=cache, model=FailingModel(), categories={})
        assert txn.category == "Other/Uncategorized"
        assert txn.confidence == 0.0


class TestDirectionConsistency:
    """Income <=> money in. The model must be told the direction, and any
    automatic answer that contradicts it goes to review instead of being
    trusted (the ₹106 auto fare once came back as Income at confidence 1.0)."""

    def income(self):
        return CountingModel(CategorizationResult(
            vendor="X", category="Income", subcategory="Interest", confidence=1.0))

    def test_debit_labelled_income_goes_to_review(self):
        txn = make_txn("UPI/RAVI KUMAR S/YESB/1/auto", vendor="RAVI KUMAR S", note="auto")
        cache: dict = {}
        categorize_transactions([txn], rules=[], vendor_cache=cache, model=self.income(), categories={})
        assert txn.category == "Other/Uncategorized"
        assert txn.confidence == 0.0
        assert cache == {}  # an impossible answer is never remembered

    def test_credit_labelled_as_spending_goes_to_review(self):
        txn = make_txn("REV-UPI/SWIGGY/1/complaint", vendor="SWIGGY", direction="credit")
        categorize_transactions([txn], rules=[], vendor_cache={}, model=CountingModel(), categories={})
        assert txn.category == "Other/Uncategorized"
        assert txn.confidence == 0.0

    def test_consistent_answers_pass_through(self):
        txn = make_txn("Int.Pd", vendor="Int.Pd", direction="credit")
        categorize_transactions([txn], rules=[], vendor_cache={}, model=self.income(), categories={})
        assert txn.category == "Income" and txn.confidence == 1.0

    def test_rules_are_checked_too(self):
        txn = make_txn("REV-UPI/SWIGGY/1/x", vendor="SWIGGY", direction="credit")
        rule = Rule(pattern="SWIGGY", category="Food & Dining", subcategory="Delivery")
        categorize_transactions([txn], rules=[rule], vendor_cache={}, model=CountingModel(), categories={})
        assert txn.confidence == 0.0

    def test_refund_does_not_reuse_the_purchase_cache_entry(self):
        purchase = make_txn("SWIGGY*1", vendor="SWIGGY")
        refund = make_txn("REV-UPI/SWIGGY/1/x", vendor="SWIGGY", direction="credit")
        cache: dict = {}
        food = CountingModel()
        categorize_transactions([purchase], rules=[], vendor_cache=cache, model=food, categories={})
        refund_model = CountingModel(CategorizationResult(
            vendor="Swiggy", category="Income", subcategory="Refunds", confidence=0.9))
        categorize_transactions([refund], rules=[], vendor_cache=cache, model=refund_model, categories={})
        assert refund_model.calls == 1
        assert refund.category == "Income" and refund.subcategory == "Refunds"
        assert purchase.category == "Food & Dining"

    def test_prompt_states_direction_and_amount(self):
        out = build_prompt("UPI/X/1/auto", "auto", {"Transport": ["Fuel"]},
                           direction="debit", amount="106.00 INR")
        assert "OUT of the account" in out and "106.00 INR" in out
        assert "never Income" in out
        incoming = build_prompt("Int.Pd", "", {}, direction="credit", amount="342.00 INR")
        assert "INTO the account" in incoming


class TestCacheKeys:
    def test_person_key_includes_note_and_direction(self):
        assert cache_key_for(make_txn("UPI/...", vendor="RAHUL SHARMA", note="auto")) == "DEBIT|RAHUL SHARMA|AUTO"
        assert cache_key_for(make_txn("UPI/...", vendor="RAHUL SHARMA", note="rent")) == "DEBIT|RAHUL SHARMA|RENT"

    def test_merchant_key_is_direction_and_vendor(self):
        assert cache_key_for(make_txn("SWIGGY*X", vendor="SWIGGY")) == "DEBIT|SWIGGY"
        assert cache_key_for(make_txn("x", vendor="SWIGGY", direction="credit")) == "CREDIT|SWIGGY"
        assert cache_key_for(make_txn("x", vendor="BMTC BUS KA01F2")) == "DEBIT|BMTC BUS KA01F2"


class TestP2P:
    def test_names_vs_merchants(self):
        assert looks_like_individual_name("RAHUL SHARMA")
        assert looks_like_individual_name("MANOJ KUMAR S")
        assert not looks_like_individual_name("SWIGGY")
        assert not looks_like_individual_name("BMTC BUS KA01F2")
        assert not looks_like_individual_name("Int.Pd:0001:X")

    @pytest.mark.parametrize("business", [
        "Swiggy Ltd", "ZOMATO LIMITED", "Amazon India", "UBER INDIA SYS",
        "Bangalore Metro", "Blink Commerce", "ETERNAL LIMITED", "SUNRISE GARDEN",
        # Payee names are cut off at ~15 characters in exports.
        "APOLLO PHARMAC", "Bundl technolo", "Redbus India P",
    ])
    def test_business_names_are_not_people(self, business):
        assert not looks_like_individual_name(business)

    @pytest.mark.parametrize("person", [
        "MR SURESH K", "Mr ROHAN MEHT", "NEHA SHARMA", "KIRAN H P",
        "Priya Agarw", "MEERA PATIL",
        # Short truncated surnames must not hit a business-word prefix.
        "RAMESH SH", "VIJAY CO",
        # A full surname that happens to start a business word ("BAKERY",
        # "TOURS") is not truncation — the name is nowhere near the limit.
        "JOHN BAKER", "MARIE TOUR",
    ])
    def test_people_still_recognized(self, person):
        assert looks_like_individual_name(person)

    def test_confident_note_suppresses_candidate(self):
        confident = make_txn("UPI/X", vendor="ARSHAD AHMED", note="bus", confidence=0.9)
        assert not is_p2p_candidate(confident, 0.6)
        no_note = make_txn("UPI/X", vendor="ARSHAD AHMED", confidence=0.9)
        assert is_p2p_candidate(no_note, 0.6)
        low_conf = make_txn("UPI/X", vendor="ARSHAD AHMED", note="hm", confidence=0.2)
        assert is_p2p_candidate(low_conf, 0.6)


class TestVendorCacheStore:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "cache.json"
        save_vendor_cache(path, {"DEBIT|SWIGGY": CategorizationResult(vendor="Swiggy", confidence=0.9)})
        assert load_vendor_cache(path)["DEBIT|SWIGGY"].vendor == "Swiggy"

    def test_old_keys_without_direction_are_migrated_by_category(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text(
            '{"SWIGGY": {"vendor": "Swiggy", "category": "Food & Dining"},'
            ' "INT.PD": {"vendor": "Interest", "category": "Income"}}'
        )
        cache = load_vendor_cache(path)
        assert set(cache) == {"DEBIT|SWIGGY", "CREDIT|INT.PD"}


class TestConfigAndPrompt:
    def test_real_project_files_load(self):
        categories = load_categories(PROJECT / "categories.yaml")
        assert "Food & Dining" in categories
        assert load_rules(PROJECT / "rules.yaml") == []
        hints = load_locale_hints(PROJECT / "locales", "in")
        assert "UPI" in hints
        assert load_locale_hints(PROJECT / "locales", "zz") == ""

    def test_prompt_includes_hints_and_taxonomy(self):
        prompt = build_prompt("UPI/X/1/auto", "auto", {"Transport": ["Fuel"]}, "Hint: autos.")
        assert "Transport: Fuel" in prompt
        assert "Hint: autos." in prompt
        assert "Payment note: auto" in prompt
