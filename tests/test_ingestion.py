from decimal import Decimal
from pathlib import Path

import pytest

from src.ingestion import ingest
from src.profile_store import load_profiles, save_profile
from tests.test_profile import EU_PROFILE, KOTAK_PROFILE
from tests.test_schema_agent import FakeModel

FIXTURES = Path(__file__).parent / "fixtures"


def raising_factory():
    raise AssertionError("model_factory must not be called on this path")


def no_prompts(*args, **kwargs):
    raise AssertionError("no user prompt expected on this path")


class TestStandardFormatRouting:
    @pytest.mark.parametrize("name,count", [
        ("sample.ofx", 2), ("sample.sta", 2), ("sample_camt.xml", 2),
    ])
    def test_standard_formats_never_touch_llm(self, tmp_path, name, count):
        txns = ingest(
            FIXTURES / name,
            profiles_dir=tmp_path,
            model_factory=raising_factory,
            prompt_fn=no_prompts,
            echo_fn=lambda *a: None,
        )
        assert len(txns) == count

    def test_qif_gets_default_currency(self, tmp_path):
        txns = ingest(
            FIXTURES / "sample.qif",
            profiles_dir=tmp_path,
            model_factory=raising_factory,
            prompt_fn=no_prompts,
            echo_fn=lambda *a: None,
            default_currency="GBP",
        )
        assert all(t.currency == "GBP" for t in txns)


class TestProfilePaths:
    def test_cached_profile_skips_llm(self, tmp_path):
        save_profile(tmp_path, KOTAK_PROFILE)
        txns = ingest(
            FIXTURES / "kotak_style.csv",
            profiles_dir=tmp_path,
            model_factory=raising_factory,
            prompt_fn=no_prompts,
            echo_fn=lambda *a: None,
        )
        assert len(txns) == 4
        assert txns[0].amount == Decimal("40.00")

    def test_inference_path_caches_profile(self, tmp_path):
        model = FakeModel([KOTAK_PROFILE])
        txns = ingest(
            FIXTURES / "kotak_style.csv",
            profiles_dir=tmp_path,
            model_factory=lambda: model,
            prompt_fn=no_prompts,  # dates prove day-first (15-07), currency set
            echo_fn=lambda *a: None,
        )
        assert len(txns) == 4
        assert len(load_profiles(tmp_path)) == 1
        # Second run: cache hit, no model.
        txns2 = ingest(
            FIXTURES / "kotak_style.csv",
            profiles_dir=tmp_path,
            model_factory=raising_factory,
            prompt_fn=no_prompts,
            echo_fn=lambda *a: None,
        )
        assert [t.dedup_hash for t in txns2] == [t.dedup_hash for t in txns]

    def test_missing_currency_asked_once_and_stored(self, tmp_path):
        profile = EU_PROFILE.model_copy(update={"currency": ""})
        model = FakeModel([profile])
        prompts = []

        def prompt_fn(message, **kwargs):
            prompts.append(message)
            return "EUR"

        txns = ingest(
            FIXTURES / "eu_style.csv",
            profiles_dir=tmp_path,
            model_factory=lambda: model,
            prompt_fn=prompt_fn,
            echo_fn=lambda *a: None,
        )
        assert all(t.currency == "EUR" for t in txns)
        assert any("currency" in p for p in prompts)
        assert load_profiles(tmp_path)[0].currency == "EUR"

    def test_failed_inference_falls_back_to_interactive(self, tmp_path):
        bad = KOTAK_PROFILE.model_copy(
            update={"column_map": {**KOTAK_PROFILE.column_map, "amount": 0}}
        )
        model = FakeModel([bad, bad])  # both attempts invalid
        answers = iter([
            "8", "1", "2", "3", "4", "1", "5", "6", "DR", "7",
            "%d-%m-%Y %H:%M:%S", ".", "INR", "Closing Balance",
        ])
        txns = ingest(
            FIXTURES / "kotak_style.csv",
            profiles_dir=tmp_path,
            model_factory=lambda: model,
            prompt_fn=lambda *a, **k: next(answers),
            echo_fn=lambda *a: None,
        )
        assert len(txns) == 4
        assert len(load_profiles(tmp_path)) == 1
