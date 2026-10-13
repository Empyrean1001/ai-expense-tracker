"""Golden-dataset eval runner — the regression gate for AI behavior
(decision D-8), run before shipping any prompt/model/taxonomy change:

    python -m evals.run_evals

Deterministic checks always run (committed profiles must reproduce expected
parses; layout detection must find those same parses with no LLM; the
validator must reject corrupted profiles). LLM checks (header labelling for
a layout code can't read, categorization accuracy + calibration) run only
when Ollama is reachable, otherwise they are reported SKIPPED. Exit code is non-zero on
any regression.
"""

import csv
import json
import sys
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

from src.config_loader import load_categories, load_config, load_locale_hints  # noqa: E402
from src.models import BankProfile, CategorizationResult  # noqa: E402
from src.ollama_client import (  # noqa: E402
    OllamaError,
    build_prompt,
    check_ollama_available,
)
from src.profile import parse_rows_with_profile  # noqa: E402
from src.profile_validator import validate_profile  # noqa: E402
from src.layout_detect import detect_profile  # noqa: E402
from src.schema_agent import HeaderLabels, SchemaInferenceError, infer_profile  # noqa: E402

CONFIDENCE_THRESHOLD = 0.6


def read_rows(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def load_cases() -> list[dict]:
    return json.loads((PROJECT / "evals/schema/cases.json").read_text())


def _summarize(rows, profile, currency: str) -> dict:
    txns = parse_rows_with_profile(rows, profile.model_copy(update={"currency": currency}))
    total_debit = sum((t.amount for t in txns if t.direction == "debit"), Decimal(0))
    total_credit = sum((t.amount for t in txns if t.direction == "credit"), Decimal(0))
    return {
        "count": len(txns),
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "currency": txns[0].currency,
    }


def eval_profile_execution() -> list[str]:
    """Committed known-good profiles must reproduce the expected parses."""
    failures = []
    for case in load_cases():
        rows = read_rows(PROJECT / case["fixture"])
        profile = BankProfile(**json.loads((PROJECT / case["profile"]).read_text()))
        expected = case["expected"]
        try:
            got = _summarize(rows, profile, profile.currency)
        except Exception as exc:
            failures.append(f"{case['fixture']}: parse failed: {exc}")
            continue
        if got != expected:
            failures.append(f"{case['fixture']}: expected {expected}, got {got}")
    return failures


def eval_layout_detection() -> list[str]:
    """Detection with no LLM must reach the same parse as the golden profile."""
    failures = []
    for case in load_cases():
        rows = read_rows(PROJECT / case["fixture"])
        profile = detect_profile(rows)
        if profile is None:
            failures.append(f"{case['fixture']}: layout not detected")
            continue
        got = _summarize(rows, profile, case["expected"]["currency"])
        if got != case["expected"]:
            failures.append(f"{case['fixture']}: expected {case['expected']}, got {got}")
    return failures


def eval_validator_rejects_corruption() -> list[str]:
    """Deliberately corrupted profiles must be caught, never imported."""
    failures = []
    for case in load_cases():
        rows = read_rows(PROJECT / case["fixture"])
        profile = BankProfile(**json.loads((PROJECT / case["profile"]).read_text()))
        corruptions = {
            "wrong amount column": profile.model_copy(update={
                "column_map": {**profile.column_map, "amount": profile.column_map.get("balance", 0)}
            }),
            "wrong decimal separator": profile.model_copy(update={
                "decimal_separator": "," if profile.decimal_separator == "." else "."
            }),
        }
        for label, corrupted in corruptions.items():
            if not validate_profile(rows, corrupted):
                failures.append(f"{case['fixture']}: validator accepted {label}")
    return failures


def get_live_model(schema):
    config = load_config(PROJECT / "config.yaml")
    host, model_name = config["ollama"]["host"], config["ollama"]["model"]
    check_ollama_available(host, model_name)
    from langchain_ollama import ChatOllama

    return ChatOllama(base_url=host, model=model_name, temperature=0).with_structured_output(schema)


def eval_header_labelling() -> list[str]:
    """For layouts code can't read, the LLM labels header cells; the result
    must parse to the expected totals."""
    model = get_live_model(HeaderLabels)
    failures = []
    for case in json.loads((PROJECT / "evals/schema/llm_cases.json").read_text()):
        rows = read_rows(PROJECT / case["fixture"])
        try:
            profile = infer_profile(rows, model)
        except SchemaInferenceError as exc:
            failures.append(f"{case['fixture']}: {exc}")
            continue
        got = _summarize(rows, profile, case["expected"]["currency"])
        if got != case["expected"]:
            failures.append(f"{case['fixture']}: expected {case['expected']}, got {got}")
    return failures


def eval_categorization() -> tuple[list[str], str]:
    model = get_live_model(CategorizationResult)
    categories = load_categories(PROJECT / "categories.yaml")
    rows = list(csv.DictReader(open(PROJECT / "evals/categorization/labeled.csv", encoding="utf-8")))

    correct = 0
    high_confidence = 0
    high_confidence_correct = 0
    misses = []
    for row in rows:
        hints = load_locale_hints(PROJECT / "locales", row["locale"])
        prompt = build_prompt(row["description"], row["note"], categories, hints)
        result = model.invoke(prompt) or CategorizationResult()
        hit = result.category == row["expected_category"]
        correct += hit
        if result.confidence >= CONFIDENCE_THRESHOLD:
            high_confidence += 1
            high_confidence_correct += hit
        if not hit:
            misses.append(
                f"  {row['description'][:50]!r}: expected {row['expected_category']}, "
                f"got {result.category} (conf {result.confidence:.2f})"
            )

    accuracy = correct / len(rows)
    calibration = (high_confidence_correct / high_confidence) if high_confidence else 1.0
    summary = (
        f"accuracy {correct}/{len(rows)} = {accuracy:.0%}; "
        f"calibration (of {high_confidence} high-confidence answers, correct): {calibration:.0%}"
    )
    failures = []
    if accuracy < 0.7:
        failures.append(f"categorization accuracy {accuracy:.0%} below 70% floor")
    if calibration < 0.8:
        failures.append(
            f"calibration {calibration:.0%} below 80% — the review threshold is not trustworthy"
        )
    return failures + misses if failures else [], summary


def main() -> int:
    exit_code = 0

    def report(name: str, failures: list[str], extra: str = ""):
        nonlocal exit_code
        if failures:
            exit_code = 1
            print(f"[FAIL] {name}" + (f" — {extra}" if extra else ""))
            for failure in failures:
                print(f"       {failure}")
        else:
            print(f"[PASS] {name}" + (f" — {extra}" if extra else ""))

    report("profile execution matches goldens", eval_profile_execution())
    report("layout detection without LLM matches goldens", eval_layout_detection())
    report("validator rejects corrupted profiles", eval_validator_rejects_corruption())

    try:
        check_ollama_available(
            load_config(PROJECT / "config.yaml")["ollama"]["host"],
            load_config(PROJECT / "config.yaml")["ollama"]["model"],
        )
        ollama_up = True
    except OllamaError as exc:
        ollama_up = False
        reason = str(exc).splitlines()[0]
        print(f"[SKIP] header labelling (LLM) — {reason}")
        print(f"[SKIP] categorization accuracy+calibration (LLM) — {reason}")

    if ollama_up:
        report("header labelling (LLM)", eval_header_labelling())
        failures, summary = eval_categorization()
        report("categorization accuracy+calibration (LLM)", failures, summary)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
