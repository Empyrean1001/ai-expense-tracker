from datetime import datetime
from decimal import Decimal

import pytest

from src.localeparse import (
    LocaleParseError,
    disambiguate_date_order,
    parse_amount,
    parse_datetime_multi,
)


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,sep,expected",
        [
            ("1,33,116.37", ".", "133116.37"),  # Indian grouping
            ("1,234.56", ".", "1234.56"),  # US grouping
            ("1.234,56", ",", "1234.56"),  # European grouping
            ("1 234,56", ",", "1234.56"),  # space grouping
            ("40.00", ".", "40.00"),
            ("-500.25", ".", "-500.25"),
            ("(123.45)", ".", "-123.45"),  # accountant negative
            ("₹1,000.00", ".", "1000.00"),  # currency symbol
            ("EUR 99,90", ",", "99.90"),
            ("0,10", ",", "0.10"),
        ],
    )
    def test_locales(self, raw, sep, expected):
        assert parse_amount(raw, sep) == Decimal(expected)

    def test_exactness(self):
        assert parse_amount("0.10") == Decimal("0.10")  # not 0.1000000000000000055...

    @pytest.mark.parametrize("raw", ["", "abc", "--", "12..3.4"])
    def test_garbage_raises(self, raw):
        with pytest.raises(LocaleParseError):
            parse_amount(raw)


class TestParseDatetimeMulti:
    def test_first_matching_format_wins(self):
        result = parse_datetime_multi(
            "31-03-2026 07:48:39", ["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"]
        )
        assert result == datetime(2026, 3, 31, 7, 48, 39)

    def test_falls_through_to_second_format(self):
        result = parse_datetime_multi("31-03-2026", ["%d-%m-%Y %H:%M:%S", "%d-%m-%Y"])
        assert result == datetime(2026, 3, 31)

    def test_no_format_fits(self):
        with pytest.raises(LocaleParseError, match="could not read date"):
            parse_datetime_multi("March 31", ["%d-%m-%Y"])


class TestDisambiguateDateOrder:
    def test_day_first_proven(self):
        assert disambiguate_date_order(["05/01/2026", "31/01/2026"]) == "day_first"

    def test_month_first_proven(self):
        assert disambiguate_date_order(["01/05/2026", "01/31/2026"]) == "month_first"

    def test_ambiguous_when_all_components_small(self):
        assert disambiguate_date_order(["05/01/2026", "03/02/2026"]) == "ambiguous"

    def test_iso_dates_do_not_mislead(self):
        assert disambiguate_date_order(["2026-01-31"]) == "ambiguous"

    def test_empty(self):
        assert disambiguate_date_order([]) == "ambiguous"
