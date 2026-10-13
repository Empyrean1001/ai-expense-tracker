import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.models import BankProfile
from src.profile import (
    ProfileExecutionError,
    expand_date_formats,
    find_header_row,
    parse_rows_with_profile,
)
from src.profile_validator import validate_profile

FIXTURES = Path(__file__).parent / "fixtures"


def read_rows(name: str) -> list[list[str]]:
    with open(FIXTURES / name, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


KOTAK_PROFILE = BankProfile(
    bank_name="Kotak-style",
    header_signature=[
        "Sl. No.", "Transaction Date", "Value Date", "Description",
        "Chq /Ref No.", "Amount", "Dr / Cr", "Balance", "Dr / Cr",
    ],
    footer_sentinels=["Closing Balance"],
    column_map={
        "posted_at": 1, "value_date": 2, "description": 3, "reference": 4,
        "amount": 5, "dr_cr": 6, "balance": 7,
    },
    date_formats=["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"],
    decimal_separator=".",
    amount_convention="dual_drcr",
    debit_marker="DR",
    currency="INR",
    description_convention="upi",
)

EU_PROFILE = BankProfile(
    bank_name="EU-style",
    header_signature=["Buchungstag", "Wertstellung", "Verwendungszweck", "Betrag", "Saldo"],
    column_map={"posted_at": 0, "value_date": 1, "description": 2, "amount": 3, "balance": 4},
    date_formats=["%d.%m.%Y"],
    decimal_separator=",",
    amount_convention="signed",
    currency="EUR",
)


class TestKotakStyle:
    def test_parses_fixture(self):
        txns = parse_rows_with_profile(read_rows("kotak_style.csv"), KOTAK_PROFILE)
        assert len(txns) == 4  # footer sentinel stopped the scan
        first = txns[0]
        assert first.amount == Decimal("40.00")
        assert first.direction == "debit"
        assert first.balance == Decimal("133116.37")  # Indian grouping stripped
        assert first.vendor == "SURESH NAIK LA"
        assert first.description_note == "bus"
        assert first.value_date == date(2026, 7, 15)
        assert txns[2].direction == "credit"
        assert txns[2].amount == Decimal("1500.00")

    def test_validator_accepts(self):
        assert validate_profile(read_rows("kotak_style.csv"), KOTAK_PROFILE) == []


class TestEuStyle:
    def test_parses_fixture(self):
        txns = parse_rows_with_profile(read_rows("eu_style.csv"), EU_PROFILE)
        assert len(txns) == 3
        assert txns[0].amount == Decimal("45.67")
        assert txns[0].direction == "debit"
        assert txns[1].direction == "credit"
        assert txns[1].amount == Decimal("1500.00")
        assert txns[1].balance == Decimal("2454.33")
        assert txns[0].value_date == date(2026, 7, 15)

    def test_validator_accepts(self):
        assert validate_profile(read_rows("eu_style.csv"), EU_PROFILE) == []


class TestValidatorCatchesBadProfiles:
    def test_wrong_amount_column_breaks_continuity(self):
        bad = KOTAK_PROFILE.model_copy(update={
            "column_map": {**KOTAK_PROFILE.column_map, "amount": 0}  # Sl. No. as amount
        })
        errors = validate_profile(read_rows("kotak_style.csv"), bad)
        assert any("continuity" in e for e in errors)

    def test_wrong_decimal_separator_fails(self):
        bad = KOTAK_PROFILE.model_copy(update={"decimal_separator": ","})
        errors = validate_profile(read_rows("kotak_style.csv"), bad)
        assert errors  # 1,33,116.37 read as 133116.37 -> continuity or parse break

    def test_wrong_sign_convention_breaks_continuity(self):
        bad = EU_PROFILE.model_copy(update={"signed_debit_negative": False})
        errors = validate_profile(read_rows("eu_style.csv"), bad)
        assert any("continuity" in e for e in errors)

    def test_swapped_date_columns_or_format_reports_parse_error(self):
        bad = EU_PROFILE.model_copy(update={"date_formats": ["%m.%d.%Y"]})
        errors = validate_profile(read_rows("eu_style.csv"), bad)
        assert errors  # 15.07.2026 has no month 15

    def test_missing_header_reported(self):
        with pytest.raises(ProfileExecutionError, match="header"):
            parse_rows_with_profile([["nothing", "here"]], KOTAK_PROFILE)


class TestDateOnlyFallback:
    """Banks often put a timestamp in one date column and a plain date in
    another; a profile listing only the timestamp format must still read the
    date-only column."""

    def test_timestamp_format_also_reads_date_only_column(self):
        profile = KOTAK_PROFILE.model_copy(update={"date_formats": ["%d-%m-%Y %H:%M:%S"]})
        txns = parse_rows_with_profile(read_rows("kotak_style.csv"), profile)
        assert txns[0].value_date == date(2026, 7, 15)
        assert validate_profile(read_rows("kotak_style.csv"), profile) == []

    @pytest.mark.parametrize("fmt,expected", [
        ("%d-%m-%Y %H:%M:%S", ["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"]),
        ("%Y-%m-%dT%H:%M:%S", ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]),
        ("%d %b %Y %I:%M %p", ["%d %b %Y %I:%M %p", "%d %b %Y"]),
        ("%d.%m.%Y", ["%d.%m.%Y"]),
    ])
    def test_expand_date_formats(self, fmt, expected):
        assert expand_date_formats([fmt]) == expected

    def test_expand_keeps_order_and_dedupes(self):
        assert expand_date_formats(["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"]) == [
            "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
        ]


def test_find_header_row_normalizes_whitespace():
    rows = [["junk"], ["SL.NO.", "TRANSACTION  DATE", "Value date"]]
    assert find_header_row(rows, ["Sl. No.", "Transaction Date", "Value Date"]) == 1
