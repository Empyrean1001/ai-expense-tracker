import csv
from pathlib import Path

import pytest

from src.models import BankProfile
from src.profile_store import fingerprint, load_profiles, match_profile, save_profile
from src.schema_agent import (
    SchemaInferenceError,
    build_inference_prompt,
    infer_profile,
    interactive_column_mapping,
)
from tests.test_profile import EU_PROFILE, KOTAK_PROFILE

FIXTURES = Path(__file__).parent / "fixtures"


def read_rows(name: str) -> list[list[str]]:
    with open(FIXTURES / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


class FakeModel:
    """Returns queued responses; records prompts."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestProfileStore:
    def test_fingerprint_ignores_whitespace_and_case(self):
        a = fingerprint(["Sl. No.", "Transaction Date"])
        b = fingerprint(["SL.NO.", "TRANSACTION  DATE"])
        assert a == b

    def test_save_and_match(self, tmp_path):
        save_profile(tmp_path, KOTAK_PROFILE)
        save_profile(tmp_path, EU_PROFILE)
        assert len(load_profiles(tmp_path)) == 2
        match = match_profile(tmp_path, read_rows("kotak_style.csv"))
        assert match is not None and match.bank_name == "Kotak-style"
        match = match_profile(tmp_path, read_rows("eu_style.csv"))
        assert match is not None and match.bank_name == "EU-style"

    def test_no_match(self, tmp_path):
        save_profile(tmp_path, EU_PROFILE)
        assert match_profile(tmp_path, read_rows("kotak_style.csv")) is None

    def test_empty_dir(self, tmp_path):
        assert match_profile(tmp_path / "missing", [["a"]]) is None


class TestInferProfile:
    def test_happy_path(self):
        model = FakeModel([KOTAK_PROFILE])
        result = infer_profile(read_rows("kotak_style.csv"), model)
        assert result.bank_name == "Kotak-style"
        assert len(model.prompts) == 1
        assert "0-based column indices" in model.prompts[0]

    def test_bad_then_good_feeds_errors_back(self):
        bad = KOTAK_PROFILE.model_copy(
            update={"column_map": {**KOTAK_PROFILE.column_map, "amount": 0}}
        )
        model = FakeModel([bad, KOTAK_PROFILE])
        result = infer_profile(read_rows("kotak_style.csv"), model)
        assert result == KOTAK_PROFILE
        assert "failed validation" in model.prompts[1]
        assert "continuity" in model.prompts[1]

    def test_gives_up_after_max_attempts(self):
        bad = KOTAK_PROFILE.model_copy(
            update={"column_map": {**KOTAK_PROFILE.column_map, "amount": 0}}
        )
        model = FakeModel([bad, bad])
        with pytest.raises(SchemaInferenceError, match="Could not infer"):
            infer_profile(read_rows("kotak_style.csv"), model)

    def test_model_exception_counts_as_attempt(self):
        model = FakeModel([RuntimeError("boom"), KOTAK_PROFILE])
        result = infer_profile(read_rows("kotak_style.csv"), model)
        assert result == KOTAK_PROFILE

    def test_prompt_mentions_all_conventions(self):
        prompt = build_inference_prompt([["a", "b"]])
        for token in ("dual_drcr", "signed", "separate", "decimal_separator"):
            assert token in prompt


class TestInteractiveMapping:
    def test_scripted_kotak_mapping(self):
        rows = read_rows("kotak_style.csv")
        answers = iter([
            "8",              # header row index
            "1",              # posted_at column
            "2",              # value date
            "3",              # description
            "4",              # reference
            "1",              # convention: dual_drcr
            "5",              # amount
            "6",              # dr/cr marker column
            "DR",             # debit marker
            "7",              # balance
            "%d-%m-%Y %H:%M:%S",  # date format
            ".",              # decimal separator
            "INR",            # currency
            "Closing Balance",
        ])
        profile = interactive_column_mapping(rows, lambda *a, **k: next(answers), lambda *a: None)
        profile = profile.model_copy(update={"description_convention": "upi"})
        from src.profile_validator import validate_profile

        assert validate_profile(rows, profile) == []
