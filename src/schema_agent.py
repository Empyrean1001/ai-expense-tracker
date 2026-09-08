"""The Schema Mapping Agent: turn an unknown CSV/XLSX layout into a
BankProfile.

The LLM is used at design time, once per bank format (decision D-1). Its
output is validated deterministically before being trusted (D-2); after two
failed attempts the human maps the columns interactively — a wrong import is
never the fallback.
"""

from src.models import BankProfile
from src.profile_validator import validate_profile

CANONICAL_FIELDS = """\
posted_at      - date (or datetime) the transaction was made/posted
value_date     - value date, if a separate column exists
description    - the transaction narrative/purpose text
reference      - cheque/reference number column, if any
amount         - the single amount column (dual_drcr and signed conventions)
dr_cr          - the debit/credit marker column (dual_drcr convention only)
debit_amount   - debit amount column (separate convention only)
credit_amount  - credit amount column (separate convention only)
balance        - running balance column, if any"""


class SchemaInferenceError(Exception):
    pass


def _render_rows(sample_rows: list[list[str]]) -> str:
    lines = []
    for i, row in enumerate(sample_rows):
        cells = " | ".join(f"[{j}] {cell}" for j, cell in enumerate(row))
        lines.append(f"row {i}: {cells}")
    return "\n".join(lines)


def build_inference_prompt(sample_rows: list[list[str]], feedback: str = "") -> str:
    prompt = (
        "You are mapping the layout of a bank statement export to a canonical schema.\n"
        "Below are the first rows of the file, with 0-based column indices in brackets.\n"
        "Identify the transaction table's header row and produce a BankProfile:\n"
        "- header_signature: the exact cell texts of the header row, in order\n"
        "- footer_sentinels: first-cell texts of rows that end the table "
        "(e.g. 'Closing Balance'), if visible\n"
        "- column_map: canonical field name -> 0-based column index, using only:\n"
        f"{CANONICAL_FIELDS}\n"
        "- date_formats: Python strptime format(s) for the date columns\n"
        "- decimal_separator: '.' or ',' as used in amounts\n"
        "- amount_convention: 'dual_drcr' (amount + DR/CR marker column), "
        "'signed' (one signed amount column), or 'separate' (debit and credit columns)\n"
        "- debit_marker: the marker text meaning debit, if dual_drcr\n"
        "- currency: the ISO 4217 code if visible in the file metadata, else ''\n"
        "- description_convention: 'upi' if descriptions look like "
        "'UPI/<NAME>/<ref>/<note>', else 'generic'\n"
    )
    if feedback:
        prompt += (
            "\nYour previous attempt failed validation with these errors — fix them:\n"
            f"{feedback}\n"
        )
    return prompt + f"\nFile sample:\n{_render_rows(sample_rows)}\n"


def infer_profile(sample_rows: list[list[str]], model, max_attempts: int = 2) -> BankProfile:
    """`model.invoke(prompt) -> BankProfile` (structured output). Each failed
    validation feeds its errors back into the retry prompt."""
    feedback = ""
    last_errors: list[str] = []
    for _ in range(max_attempts):
        try:
            profile = model.invoke(build_inference_prompt(sample_rows, feedback))
        except Exception as exc:
            last_errors = [f"model call failed: {exc}"]
            feedback = last_errors[0]
            continue
        if profile is None:
            last_errors = ["model returned nothing"]
            feedback = last_errors[0]
            continue
        candidate = profile.model_copy(update={"currency": profile.currency or "XXX"})
        errors = validate_profile(sample_rows, candidate)
        if not errors:
            return profile
        last_errors = errors
        feedback = "\n".join(errors)
    raise SchemaInferenceError(
        "Could not infer a valid layout for this file:\n  " + "\n  ".join(last_errors)
    )


def interactive_column_mapping(rows: list[list[str]], prompt_fn, echo_fn) -> BankProfile:
    """Manual fallback: the user maps columns from a preview. Slower than the
    LLM, but always correct — and only ever needed once per bank."""
    echo_fn("\nAutomatic layout detection failed. Let's map the columns together.")
    echo_fn("File preview (row number: [column index] cell):\n")
    for i, row in enumerate(rows[:15]):
        echo_fn(f"  row {i}: " + " | ".join(f"[{j}] {cell}" for j, cell in enumerate(row)))

    header_index = int(prompt_fn("Which row number is the table header?"))
    header = rows[header_index]

    def ask_column(question: str, optional: bool = False) -> int | None:
        answer = str(prompt_fn(question + (" (blank if none)" if optional else ""))).strip()
        return int(answer) if answer else None

    column_map: dict[str, int] = {}
    date_col = ask_column("Column index of the transaction date?")
    column_map["posted_at"] = date_col
    value_col = ask_column("Column index of the value date?", optional=True)
    if value_col is not None:
        column_map["value_date"] = value_col
    column_map["description"] = ask_column("Column index of the description?")
    ref_col = ask_column("Column index of the reference number?", optional=True)
    if ref_col is not None:
        column_map["reference"] = ref_col

    convention = str(prompt_fn(
        "Amount style: [1] one amount + DR/CR column, [2] one signed amount, "
        "[3] separate debit and credit columns"
    )).strip()
    debit_marker = "DR"
    if convention == "1":
        amount_convention = "dual_drcr"
        column_map["amount"] = ask_column("Column index of the amount?")
        column_map["dr_cr"] = ask_column("Column index of the DR/CR marker?")
        debit_marker = str(prompt_fn("Marker text that means debit (e.g. DR)?")).strip() or "DR"
    elif convention == "2":
        amount_convention = "signed"
        column_map["amount"] = ask_column("Column index of the amount?")
    else:
        amount_convention = "separate"
        column_map["debit_amount"] = ask_column("Column index of the debit amount?")
        column_map["credit_amount"] = ask_column("Column index of the credit amount?")

    balance_col = ask_column("Column index of the running balance?", optional=True)
    if balance_col is not None:
        column_map["balance"] = balance_col

    date_format = str(prompt_fn(
        "Date format (strptime, e.g. %d-%m-%Y or %m/%d/%Y or %d.%m.%Y)?"
    )).strip()
    decimal_separator = str(prompt_fn("Decimal separator, '.' or ','?")).strip() or "."
    currency = str(prompt_fn("ISO currency code of this account (e.g. USD, EUR, INR)?")).strip().upper()
    footer = str(prompt_fn(
        "Text in the first cell of the row that ends the table (blank if none)?"
    )).strip()

    return BankProfile(
        header_signature=header,
        footer_sentinels=[footer] if footer else [],
        column_map=column_map,
        date_formats=[date_format, date_format.split(" ")[0]] if " " in date_format else [date_format],
        decimal_separator=decimal_separator,
        amount_convention=amount_convention,
        debit_marker=debit_marker,
        currency=currency,
    )
