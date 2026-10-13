import csv
from decimal import Decimal
from pathlib import Path

import pytest

from src.layout_detect import (
    build_profiles,
    detect_currency,
    detect_date_formats,
    detect_decimal_separator,
    detect_profile,
    find_header_row_index,
    label_headers_by_name,
    pick_debit_marker,
    table_rows,
)
from src.localeparse import human_date_pattern_to_strptime
from src.profile import parse_rows_with_profile

FIXTURES = Path(__file__).parent / "fixtures"


def read_rows(name: str) -> list[list[str]]:
    with open(FIXTURES / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


class TestFindHeaderRow:
    def test_kotak_style_skips_metadata_block(self):
        rows = read_rows("kotak_style.csv")
        assert rows[find_header_row_index(rows)][0] == "Sl. No."

    def test_eu_style_header_is_first_row(self):
        assert find_header_row_index(read_rows("eu_style.csv")) == 0

    def test_no_table(self):
        assert find_header_row_index([["hello"], ["world", "again"]]) is None

    def test_table_rows_stop_at_footer(self):
        rows = read_rows("kotak_style.csv")
        data = table_rows(rows, find_header_row_index(rows))
        assert len(data) == 4
        assert all(r[0].isdigit() for r in data)


class TestDateFormats:
    def test_timestamp(self):
        assert detect_date_formats(["01-04-2026 20:27:49", "31-03-2026 07:48:39"])[0] == "%d-%m-%Y %H:%M:%S"

    def test_date_only(self):
        assert detect_date_formats(["01-04-2026", "31-03-2026"])[0] == "%d-%m-%Y"

    def test_dotted(self):
        assert detect_date_formats(["15.07.2026"]) == ["%d.%m.%Y"]

    def test_month_first_proven(self):
        assert detect_date_formats(["07/15/2026"]) == ["%m/%d/%Y"]

    def test_ambiguous_lists_day_first_first(self):
        assert detect_date_formats(["05/07/2026"])[:2] == ["%d/%m/%Y", "%m/%d/%Y"]

    def test_iso_and_month_names(self):
        assert detect_date_formats(["2026-07-15"])[0] == "%Y-%m-%d"
        assert detect_date_formats(["15 Jul 2026"])[0] == "%d %b %Y"

    def test_not_dates(self):
        assert detect_date_formats(["UPI-700000000104", "hello"]) == []
        assert detect_date_formats([]) == []


class TestDecimalSeparator:
    def test_indian_grouping_is_dot(self):
        assert detect_decimal_separator(["1,33,116.37", "40.00", "1,500.00"]) == "."

    def test_european_is_comma(self):
        assert detect_decimal_separator(["-45,67", "1.500,00", "2.454,33"]) == ","

    def test_no_decimals_defaults_to_dot(self):
        assert detect_decimal_separator(["1,500", "20"]) == "."


class TestSmallDetectors:
    def test_currency_from_metadata_block(self):
        rows = read_rows("kotak_style.csv")
        assert detect_currency(rows, find_header_row_index(rows)) == "INR"

    def test_currency_from_header_parenthetical(self):
        rows = [["Date", "Details", "Amount (GBP)"], ["15/07/2026", "x", "1.00"]]
        assert detect_currency(rows, 0) == "GBP"

    def test_currency_unknown(self):
        assert detect_currency(read_rows("eu_style.csv"), 0) == ""

    def test_debit_marker(self):
        assert pick_debit_marker(["DR", "CR", "DR"]) == "DR"
        assert pick_debit_marker(["Debit", "Credit"]) == "Debit"
        assert pick_debit_marker(["S", "H"]) == "S"

    def test_header_names(self):
        header = ["Sl. No.", "Transaction Date", "Value Date", "Description",
                  "Chq /Ref No.", "Amount", "Dr / Cr", "Balance", "Dr / Cr"]
        assert label_headers_by_name(header) == {
            "posted_at": 1, "value_date": 2, "description": 3, "reference": 4,
            "amount": 5, "dr_cr": 6, "balance": 7,  # second Dr / Cr is the balance's
        }

    def test_header_names_german_and_parenthetical(self):
        mapping = label_headers_by_name(["Buchungstag", "Verwendungszweck", "Betrag (EUR)", "Saldo"])
        assert mapping == {"posted_at": 0, "description": 1, "amount": 2, "balance": 3}


class TestHumanDatePatterns:
    @pytest.mark.parametrize("human,expected", [
        ("dd-mm-yyyy hh:mm:ss", "%d-%m-%Y %H:%M:%S"),
        ("%d-%m-%Y hh:mm:ss", "%d-%m-%Y %H:%M:%S"),  # what was typed in the bug report
        ("DD/MM/YYYY", "%d/%m/%Y"),
        ("mm/dd/yy", "%m/%d/%y"),
        ("dd.mm.yyyy", "%d.%m.%Y"),
        ("dd mmm yyyy", "%d %b %Y"),
        ("yyyy-mm-dd hh:mm", "%Y-%m-%d %H:%M"),
        ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S"),  # already strptime: unchanged
    ])
    def test_translation(self, human, expected):
        assert human_date_pattern_to_strptime(human) == expected


class TestDetectProfile:
    def test_kotak_style_detected_without_llm(self):
        rows = read_rows("kotak_style.csv")
        profile = detect_profile(rows)
        assert profile is not None
        assert profile.amount_convention == "dual_drcr"
        assert profile.column_map["dr_cr"] == 6
        assert profile.column_map["balance"] == 7
        assert profile.decimal_separator == "."
        assert profile.currency == "INR"
        assert profile.description_convention == "upi"
        txns = parse_rows_with_profile(rows, profile)
        assert len(txns) == 4  # last transaction kept, footer skipped
        assert txns[0].amount == Decimal("40.00")

    def test_eu_style_detected_without_llm(self):
        rows = read_rows("eu_style.csv")
        profile = detect_profile(rows)
        assert profile is not None
        assert profile.amount_convention == "signed"
        assert profile.decimal_separator == ","
        assert profile.currency == ""  # not in the file: ingestion asks once
        txns = parse_rows_with_profile(rows, profile.model_copy(update={"currency": "EUR"}))
        assert txns[1].amount == Decimal("1500.00")
        assert txns[1].direction == "credit"

    def test_unknown_header_names_resolved_by_content_and_balance(self):
        """Header names nothing recognizes: the column roles are found from
        the cell contents, and the running balance picks the right
        amount/balance/marker combination."""
        rows = read_rows("kotak_style.csv")
        header_index = find_header_row_index(rows)
        rows = [list(r) for r in rows]
        rows[header_index] = ["#", "When", "Booked", "What", "Ref", "Sum", "Way", "Left", "Way2"]
        profile = detect_profile(rows)
        assert profile is not None
        assert profile.column_map["amount"] == 5
        assert profile.column_map["balance"] == 7
        assert profile.column_map["dr_cr"] == 6
        assert profile.column_map["description"] == 3
        assert len(parse_rows_with_profile(rows, profile)) == 4

    def test_unrecognizable_without_balance_is_not_guessed(self):
        """No header names and no running balance to verify against: refuse
        rather than guess (the LLM or the human decides)."""
        assert detect_profile(read_rows("odd_headers.csv")) is None

    def test_build_profiles_rejects_non_date_mapped_as_date(self):
        rows = read_rows("kotak_style.csv")
        header_index = find_header_row_index(rows)
        assert build_profiles(rows, header_index, {
            "posted_at": 3, "description": 3, "amount": 5, "dr_cr": 6,
        }) == []
