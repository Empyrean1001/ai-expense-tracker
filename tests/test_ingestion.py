from decimal import Decimal
from pathlib import Path

import pytest

from src.ingestion import ingest
from src.ollama_client import OllamaError
from src.profile_store import load_profiles, save_profile
from src.schema_agent import HeaderLabels
from tests.test_profile import KOTAK_PROFILE
from tests.test_schema_agent import ODD_LABELS, FakeModel, ScriptedPrompts

FIXTURES = Path(__file__).parent / "fixtures"


def raising_factory():
    raise AssertionError("model_factory must not be called on this path")


def no_prompts(*args, **kwargs):
    raise AssertionError("no user prompt expected on this path")


def silent(*args, **kwargs):
    pass


def run(name, tmp_path, **overrides):
    kwargs = dict(profiles_dir=tmp_path, model_factory=raising_factory,
                  prompt_fn=no_prompts, echo_fn=silent)
    kwargs.update(overrides)
    return ingest(FIXTURES / name, **kwargs)


class TestStandardFormatRouting:
    @pytest.mark.parametrize("name,count", [
        ("sample.ofx", 2), ("sample.sta", 2), ("sample_camt.xml", 2),
    ])
    def test_standard_formats_never_touch_llm(self, tmp_path, name, count):
        assert len(run(name, tmp_path)) == count

    def test_qif_gets_default_currency(self, tmp_path):
        txns = run("sample.qif", tmp_path, default_currency="GBP")
        assert all(t.currency == "GBP" for t in txns)


class TestLayoutPaths:
    def test_cached_profile_skips_detection(self, tmp_path):
        save_profile(tmp_path, KOTAK_PROFILE)
        txns = run("kotak_style.csv", tmp_path)
        assert len(txns) == 4
        assert txns[0].amount == Decimal("40.00")

    def test_known_layout_detected_without_llm_or_questions(self, tmp_path):
        txns = run("kotak_style.csv", tmp_path)
        assert len(txns) == 4
        assert len(load_profiles(tmp_path)) == 1
        # Second run: cache hit, same transactions.
        again = run("kotak_style.csv", tmp_path)
        assert [t.dedup_hash for t in again] == [t.dedup_hash for t in txns]

    def test_missing_currency_asked_once_and_stored(self, tmp_path):
        prompts = ScriptedPrompts(["EUR"])
        txns = run("eu_style.csv", tmp_path, prompt_fn=prompts)
        assert all(t.currency == "EUR" for t in txns)
        assert len(prompts.questions) == 1 and "currency" in prompts.questions[0]
        assert load_profiles(tmp_path)[0].currency == "EUR"

    def test_llm_labels_used_when_detection_fails(self, tmp_path):
        model = FakeModel([HeaderLabels(labels=ODD_LABELS)])
        txns = run("odd_headers.csv", tmp_path, model_factory=lambda: model,
                   prompt_fn=ScriptedPrompts(["GBP"]))
        assert len(txns) == 3
        assert txns[1].direction == "credit" and txns[1].amount == Decimal("2500.00")
        assert len(model.prompts) == 1

    def test_failed_llm_falls_back_to_interactive(self, tmp_path):
        model = FakeModel([HeaderLabels(labels=["other"] * 6)] * 2)
        prompts = ScriptedPrompts([None, "0", "1", "2", "3", "3", "4", "5", "", "GBP"])
        txns = run("odd_headers.csv", tmp_path, model_factory=lambda: model, prompt_fn=prompts)
        assert len(txns) == 3
        assert len(load_profiles(tmp_path)) == 1

    def test_ollama_down_falls_back_to_interactive(self, tmp_path):
        def dead_factory():
            raise OllamaError("Cannot reach Ollama")

        prompts = ScriptedPrompts([None, "0", "1", "2", "3", "3", "4", "5", "", "GBP"])
        txns = run("odd_headers.csv", tmp_path, model_factory=dead_factory, prompt_fn=prompts)
        assert len(txns) == 3
