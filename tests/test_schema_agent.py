import csv
from decimal import Decimal
from pathlib import Path

import pytest

from src.profile import parse_rows_with_profile
from src.profile_store import fingerprint, load_profiles, match_profile, save_profile
from src.profile_validator import validate_profile
from src.schema_agent import (
    HeaderLabels,
    SchemaInferenceError,
    build_label_prompt,
    infer_profile,
    interactive_column_mapping,
)
from tests.test_profile import EU_PROFILE, KOTAK_PROFILE

FIXTURES = Path(__file__).parent / "fixtures"

ODD_LABELS = ["posted_at", "value_date", "description", "reference",
              "debit_amount", "credit_amount"]


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


class ScriptedPrompts:
    """Stands in for click.prompt. A None answer accepts the offered default,
    like pressing Enter."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question, **kwargs):
        self.questions.append(question)
        answer = self.answers.pop(0)
        if answer is None:
            return kwargs.get("default", "")
        return answer


def silent(*args, **kwargs):
    pass


class TestProfileStore:
    def test_fingerprint_ignores_whitespace_and_case(self):
        assert fingerprint(["Sl. No.", "Transaction Date"]) == fingerprint(["SL.NO.", "TRANSACTION  DATE"])

    def test_save_and_match(self, tmp_path):
        save_profile(tmp_path, KOTAK_PROFILE)
        save_profile(tmp_path, EU_PROFILE)
        assert len(load_profiles(tmp_path)) == 2
        assert match_profile(tmp_path, read_rows("kotak_style.csv")).bank_name == "Kotak-style"
        assert match_profile(tmp_path, read_rows("eu_style.csv")).bank_name == "EU-style"

    def test_no_match(self, tmp_path):
        save_profile(tmp_path, EU_PROFILE)
        assert match_profile(tmp_path, read_rows("kotak_style.csv")) is None

    def test_empty_dir(self, tmp_path):
        assert match_profile(tmp_path / "missing", [["a"]]) is None


class TestInferProfile:
    """The LLM only labels header cells; code builds and validates."""

    def test_labels_become_a_validated_profile(self):
        model = FakeModel([HeaderLabels(labels=ODD_LABELS)])
        profile = infer_profile(read_rows("odd_headers.csv"), model)
        assert profile.amount_convention == "separate"
        assert profile.column_map["debit_amount"] == 4
        assert profile.header_signature == ["When", "Booked", "Details", "Ref", "Out", "In"]
        assert len(model.prompts) == 1

    def test_prompt_shows_header_text_and_examples_not_the_grid(self):
        prompt = build_label_prompt(["When", "Out"], [["15/07/2026", "12.50"]])
        assert '[0] "When"  e.g. "15/07/2026"' in prompt
        assert "Return exactly 2 labels" in prompt

    def test_wrong_label_count_fed_back(self):
        model = FakeModel([HeaderLabels(labels=["posted_at"]), HeaderLabels(labels=ODD_LABELS)])
        infer_profile(read_rows("odd_headers.csv"), model)
        assert "expected 6 labels, got 1" in model.prompts[1]

    def test_labels_contradicting_the_data_fed_back(self):
        swapped = ["description", "value_date", "posted_at", "reference",
                   "debit_amount", "credit_amount"]  # 'posted_at' on a text column
        model = FakeModel([HeaderLabels(labels=swapped), HeaderLabels(labels=ODD_LABELS)])
        infer_profile(read_rows("odd_headers.csv"), model)
        assert "don't fit the data" in model.prompts[1]

    def test_wrong_marker_column_rejected_by_balance_check(self):
        kotak_wrong = ["other", "posted_at", "value_date", "description", "reference",
                       "amount", "other", "balance", "dr_cr"]  # balance's own Dr/Cr
        kotak_right = ["other", "posted_at", "value_date", "description", "reference",
                       "amount", "dr_cr", "balance", "other"]
        model = FakeModel([HeaderLabels(labels=kotak_wrong), HeaderLabels(labels=kotak_right)])
        profile = infer_profile(read_rows("kotak_style.csv"), model)
        assert profile.column_map["dr_cr"] == 6
        assert "continuity" in model.prompts[1]

    def test_gives_up_after_max_attempts(self):
        model = FakeModel([HeaderLabels(labels=["other"] * 6)] * 2)
        with pytest.raises(SchemaInferenceError, match="Could not work out"):
            infer_profile(read_rows("odd_headers.csv"), model)

    def test_model_exception_counts_as_attempt(self):
        model = FakeModel([RuntimeError("boom"), HeaderLabels(labels=ODD_LABELS)])
        assert infer_profile(read_rows("odd_headers.csv"), model).amount_convention == "separate"

    def test_no_table_at_all(self):
        with pytest.raises(SchemaInferenceError, match="transaction table"):
            infer_profile([["just"], ["text"]], FakeModel([]))


class TestInteractiveMapping:
    def test_known_layout_is_all_defaults(self):
        """Replay of the reported failure: pressing Enter at every question
        must produce a valid Kotak profile — no strptime, no decimal
        separator, no footer question."""
        rows = read_rows("kotak_style.csv")
        prompts = ScriptedPrompts([None] * 9)
        profile = interactive_column_mapping(rows, prompts, silent)
        assert prompts.answers == []  # exactly 9 questions asked
        asked = " ".join(prompts.questions).lower()
        assert "decimal" not in asked and "strptime" not in asked and "ends the table" not in asked
        assert profile.date_formats == ["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"]
        assert profile.decimal_separator == "."
        assert profile.currency == "INR"
        assert validate_profile(rows, profile) == []
        txns = parse_rows_with_profile(rows, profile)
        assert len(txns) == 4  # nothing silently dropped
        assert txns[-1].amount == Decimal("249.00")

    def test_unknown_layout_mapped_by_hand(self):
        rows = read_rows("odd_headers.csv")
        prompts = ScriptedPrompts([
            None,          # header row: yes
            "0", "1", "2", "3",  # date, value date, description, reference
            "3",           # separate debit/credit columns
            "4", "5",      # debit, credit
            "",            # no balance column
            "gbp",         # currency (not in the file)
        ])
        profile = interactive_column_mapping(rows, prompts, silent)
        assert profile.currency == "GBP"
        assert profile.amount_convention == "separate"
        assert validate_profile(rows, profile) == []

    def test_bad_column_number_is_reasked(self):
        rows = read_rows("kotak_style.csv")
        prompts = ScriptedPrompts([None, "99", None] + [None] * 7)
        profile = interactive_column_mapping(rows, prompts, silent)
        assert profile.column_map["posted_at"] == 1
        assert prompts.answers == []

    def test_wrong_header_guess_can_be_corrected(self):
        rows = read_rows("odd_headers.csv")
        prompts = ScriptedPrompts(["n", "0", "0", "1", "2", "3", "3", "4", "5", "", "GBP"])
        profile = interactive_column_mapping(rows, prompts, silent)
        assert profile.header_signature[0] == "When"
