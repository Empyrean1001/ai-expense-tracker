from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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
        cache = {"SWIGGY": CategorizationResult(
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


class TestCacheKeys:
    def test_person_key_includes_note(self):
        assert cache_key_for(make_txn("UPI/...", vendor="RAHUL SHARMA", note="auto")) == "RAHUL SHARMA|AUTO"
        assert cache_key_for(make_txn("UPI/...", vendor="RAHUL SHARMA", note="rent")) == "RAHUL SHARMA|RENT"

    def test_merchant_key_is_vendor_only(self):
        assert cache_key_for(make_txn("SWIGGY*X", vendor="SWIGGY")) == "SWIGGY"
        assert cache_key_for(make_txn("x", vendor="BMTC BUS KA01F2")) == "BMTC BUS KA01F2"


class TestP2P:
    def test_names_vs_merchants(self):
        assert looks_like_individual_name("RAHUL SHARMA")
        assert looks_like_individual_name("MANOJ KUMAR S")
        assert not looks_like_individual_name("SWIGGY")
        assert not looks_like_individual_name("BMTC BUS KA01F2")
        assert not looks_like_individual_name("Int.Pd:0001:X")

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
        save_vendor_cache(path, {"SWIGGY": CategorizationResult(vendor="Swiggy", confidence=0.9)})
        assert load_vendor_cache(path)["SWIGGY"].vendor == "Swiggy"


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
