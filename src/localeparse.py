"""Locale-aware primitives for parsing money and dates from statement text.

Once grouping characters are stripped, the only locale knob that matters for
an amount is the decimal separator: Indian ("1,33,116.37") and US
("1,234.56") grouping both reduce to a dot-decimal number, while European
text ("1.234,56", "1 234,56") uses a comma decimal.
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

# Currency symbols/codes that may prefix or suffix an amount cell.
_AMOUNT_NOISE = re.compile(r"[^\d.,\-()]")
_DATE_COMPONENTS = re.compile(r"(\d{1,4})[./\-](\d{1,2})[./\-](\d{1,4})")


class LocaleParseError(ValueError):
    pass


def parse_amount(raw: str, decimal_separator: str = ".") -> Decimal:
    """Parse one amount cell into an exact Decimal.

    Handles grouping in any style, leading/trailing currency symbols, and
    accountant's parentheses for negatives. The sign is preserved — callers
    decide what a sign means under their amount convention.
    """
    text = raw.strip()
    if not text:
        raise LocaleParseError("empty amount")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = _AMOUNT_NOISE.sub("", text)
    if text.startswith("-"):
        negative = not negative if negative else True
        text = text[1:]

    if decimal_separator == ",":
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    if not text:
        raise LocaleParseError(f"could not read amount {raw!r}")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise LocaleParseError(f"could not read amount {raw!r}") from exc
    return -value if negative else value


def parse_datetime_multi(raw: str, formats: list[str]) -> datetime:
    """Parse with the first format that fits; error names what was tried."""
    text = raw.strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise LocaleParseError(f"could not read date {raw!r} with any of {formats}")


def disambiguate_date_order(
    raws: list[str],
) -> Literal["day_first", "month_first", "ambiguous"]:
    """Decide whether numeric dates are day-first or month-first.

    Proven the moment any sample has a first or second component greater
    than 12 — only one order can then be legal. If every sample stays ≤ 12,
    the data cannot tell, and the caller must ask the user once.
    """
    for raw in raws:
        match = _DATE_COMPONENTS.search(raw.strip())
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 31 or len(match.group(1)) == 4:
            continue  # year-first (ISO) — no d/m ambiguity to resolve
        if first > 12 and second <= 12:
            return "day_first"
        if second > 12 and first <= 12:
            return "month_first"
    return "ambiguous"
