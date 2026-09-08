from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.descriptions import extract_vendor_and_note
from src.format_detector import UnknownFormatError, detect_format
from src.standard_formats import parse_camt053, parse_mt940, parse_ofx, parse_qif

FIXTURES = Path(__file__).parent / "fixtures"


class TestDetectFormat:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("sample.ofx", "ofx"),
            ("sample.qif", "qif"),
            ("sample.sta", "mt940"),
            ("sample_camt.xml", "camt053"),
        ],
    )
    def test_fixtures(self, name, expected):
        assert detect_format(FIXTURES / name) == expected

    def test_sniffs_ofx_despite_wrong_extension(self, tmp_path):
        path = tmp_path / "statement.txt"
        path.write_text((FIXTURES / "sample.ofx").read_text())
        assert detect_format(path) == "ofx"

    def test_unknown_raises_with_supported_list(self, tmp_path):
        path = tmp_path / "mystery.dat"
        path.write_text("hello world")
        with pytest.raises(UnknownFormatError, match="Supported formats"):
            detect_format(path)


class TestParseOfx:
    def test_parses_fixture(self):
        txns = parse_ofx(FIXTURES / "sample.ofx")
        assert len(txns) == 2
        debit, credit = txns
        assert debit.amount == Decimal("45.67")
        assert debit.direction == "debit"
        assert debit.currency == "USD"
        assert debit.posted_at == datetime(2026, 7, 15, 12, 0, 0)
        assert debit.vendor == "STARBUCKS STORE 123"
        assert "COFFEE" in debit.description
        assert debit.reference == "2026071501"
        assert credit.direction == "credit"
        assert credit.amount == Decimal("1500.00")
        assert credit.value_date == date(2026, 7, 16)
        assert all(t.dedup_hash for t in txns)


class TestParseQif:
    def test_parses_fixture_with_supplied_currency(self):
        txns = parse_qif(FIXTURES / "sample.qif", currency="USD", date_formats=["%d/%m/%Y"])
        assert len(txns) == 2
        assert txns[0].direction == "debit"
        assert txns[0].amount == Decimal("45.67")
        assert txns[0].vendor == "STARBUCKS"
        assert txns[0].value_date == date(2026, 7, 15)
        assert txns[1].direction == "credit"
        assert txns[1].amount == Decimal("1500.00")
        assert txns[1].currency == "USD"


class TestParseMt940:
    def test_parses_fixture(self):
        txns = parse_mt940(FIXTURES / "sample.sta")
        assert len(txns) == 2
        assert txns[0].currency == "EUR"
        assert txns[0].amount == Decimal("45.67")
        assert txns[0].direction == "debit"
        assert txns[0].value_date == date(2026, 7, 15)
        assert "EDEKA" in txns[0].description
        assert txns[1].direction == "credit"
        assert txns[1].amount == Decimal("1500.00")
        assert "GEHALT" in txns[1].description


class TestParseCamt053:
    def test_parses_fixture(self):
        txns = parse_camt053(FIXTURES / "sample_camt.xml")
        assert len(txns) == 2
        assert txns[0].currency == "EUR"
        assert txns[0].amount == Decimal("45.67")
        assert txns[0].direction == "debit"
        assert txns[0].value_date == date(2026, 7, 15)
        assert "EDEKA" in txns[0].description
        assert txns[0].reference == "REF20260715001"
        assert txns[1].direction == "credit"


class TestDescriptions:
    def test_upi_convention_splits_vendor_and_note(self):
        vendor, note = extract_vendor_and_note("UPI/SURESH NAIK LA/700000000102/bus", "upi")
        assert vendor == "SURESH NAIK LA"
        assert note == "bus"

    def test_upi_convention_passes_through_non_upi(self):
        vendor, note = extract_vendor_and_note("Int.Pd:0001:JULY", "upi")
        assert vendor == "Int.Pd:0001:JULY"
        assert note == ""

    def test_generic_convention(self):
        vendor, note = extract_vendor_and_note("  KARTENZAHLUNG EDEKA  ", "generic")
        assert vendor == "KARTENZAHLUNG EDEKA"
        assert note == ""
