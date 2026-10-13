"""Deterministic layout detection for CSV/XLSX statements — no LLM.

Almost everything about a statement layout can be measured from the file
itself: the header row is the one right before the first run of rows holding
dates and numbers; column roles come from the header names or, failing that,
from the cells (dates parse as dates, amounts as numbers, DR/CR markers have
a handful of distinct values); date formats and the decimal separator are
found by trying candidates against real cells. Whatever is still ambiguous —
which numeric column is the amount and which the balance, which of two
'Dr / Cr' columns matters — is settled by generating each combination and
keeping the one that passes the running-balance check (decision D-15).
"""

import re
from datetime import datetime

from pydantic import ValidationError

from src.models import BankProfile
from src.profile import normalize_cell
from src.profile_validator import validate_profile

_DATE_PARTS = [
    "%d-%m-%Y", "%m-%d-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
    "%d.%m.%Y", "%m.%d.%Y", "%d-%m-%y", "%m-%d-%y", "%d/%m/%y", "%m/%d/%y",
    "%d.%m.%y", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%b %d, %Y",
    "%d %B %Y", "%Y%m%d",
]
_TIME_PARTS = ["", " %H:%M:%S", " %H:%M", "T%H:%M:%S", " %I:%M %p", " %I:%M:%S %p"]
DATE_CANDIDATES = [d + t for d in _DATE_PARTS for t in _TIME_PARTS]

_NUMERIC = re.compile(
    r"^[(\-+]?\s*(?:[A-Za-z]{3}\s*|[^\w\s,.\-+()]\s*)?\d[\d.,'\s ]*\s*(?:[A-Za-z]{3})?\)?\s*-?$"
)
_DEBIT_MARKERS = ["DR", "D", "DEBIT", "DB", "DBIT", "WITHDRAWAL", "S", "SOLL"]

# Normalized header text (whitespace removed, lowercased, parentheticals
# dropped) -> canonical field. First matching column wins, so the second of
# Kotak's two 'Dr / Cr' columns (the balance's sign) is left alone.
_SYNONYMS: dict[str, str] = {}
for _field, _names in {
    "posted_at": ["transactiondate", "txndate", "trandate", "tran.date", "date",
                  "postingdate", "postdate", "postedon", "bookingdate", "bookingdt",
                  "transactiondate&time", "buchungstag", "buchungsdatum", "datum",
                  "fecha", "data", "datadeoperação"],
    "value_date": ["valuedate", "valuedt", "valuedt.", "valdate", "wertstellung",
                   "valuta", "valutadatum", "fechavalor"],
    "description": ["description", "narration", "particulars", "details",
                    "transactiondetails", "transactiondescription", "remarks",
                    "memo", "payee", "verwendungszweck", "buchungstext",
                    "concepto", "descripción", "descrição", "libellé"],
    "reference": ["chq/refno.", "chq./ref.no.", "chq/refno", "refno.", "refno",
                  "reference", "referenceno.", "referenceno", "ref", "chequeno.",
                  "chequeno", "chqno.", "chqno", "transactionid", "utr", "utrno."],
    "amount": ["amount", "amt", "amt.", "transactionamount", "betrag", "importe",
               "valor", "montant"],
    "dr_cr": ["dr/cr", "cr/dr", "drcr", "debit/credit", "credit/debit", "type",
              "txntype", "transactiontype", "s/h", "soll/haben"],
    "debit_amount": ["debit", "debits", "withdrawal", "withdrawals", "withdrawalamt.",
                     "withdrawalamount", "debitamount", "paidout", "moneyout",
                     "soll", "out"],
    "credit_amount": ["credit", "credits", "deposit", "deposits", "depositamt.",
                      "depositamount", "creditamount", "paidin", "moneyin",
                      "haben", "in"],
    "balance": ["balance", "closingbalance", "runningbalance", "availablebalance",
                "saldo", "kontostand", "solde"],
}.items():
    for _name in _names:
        _SYNONYMS[_name] = _field


def _normalize_header(cell: str) -> str:
    return normalize_cell(re.sub(r"\(.*?\)", "", cell or ""))


def _is_numeric(cell: str) -> bool:
    return bool(_NUMERIC.match(cell.strip()))


def _parses(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def detect_date_formats(samples: list[str]) -> list[str]:
    """Every candidate format that parses all samples, day-first before
    month-first. Two results that differ only in d/m order mean the data
    cannot tell — the caller asks the user once."""
    values = [s.strip() for s in samples if s and s.strip()]
    if not values:
        return []
    matches = [fmt for fmt in DATE_CANDIDATES if _parses(values[0], fmt)]
    return [fmt for fmt in matches if all(_parses(v, fmt) for v in values[1:])]


def detect_decimal_separator(samples: list[str]) -> str:
    dot = comma = 0
    for sample in samples:
        text = sample.strip().rstrip(")-").strip()
        if re.search(r"\.\d{1,2}$", text):
            dot += 1
        elif re.search(r",\d{1,2}$", text):
            comma += 1
    return "," if comma > dot else "."


def pick_debit_marker(values: list[str]) -> str:
    present = {v.strip().upper(): v.strip() for v in values if v and v.strip()}
    for marker in _DEBIT_MARKERS:
        if marker in present:
            return present[marker]
    return "DR"


def detect_currency(rows: list[list[str]], header_index: int) -> str:
    """From a 'Currency | INR' cell pair above the table, or an
    'Amount (GBP)'-style header. '' when the file doesn't say."""
    for row in rows[:header_index]:
        cells = [c.strip() for c in row]
        for i, cell in enumerate(cells):
            if normalize_cell(cell) == "currency":
                for value in cells[i + 1:]:
                    if re.fullmatch(r"[A-Za-z]{3}", value):
                        return value.upper()
    for cell in rows[header_index]:
        match = re.search(r"\(([A-Za-z]{3})\)", cell)
        if match:
            return match.group(1).upper()
    return ""


def label_headers_by_name(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        field = _SYNONYMS.get(_normalize_header(cell))
        if field and field not in mapping:
            mapping[field] = index
    return mapping


def _looks_like_header(row: list[str]) -> bool:
    cells = [c.strip() for c in row if c and c.strip()]
    if len(cells) < 3:
        return False
    texty = sum(1 for c in cells if not _is_numeric(c) and not detect_date_formats([c]))
    return texty >= 0.6 * len(cells)


def _looks_like_transaction(row: list[str]) -> bool:
    cells = [c.strip() for c in row if c and c.strip()]
    has_date = any(detect_date_formats([c]) for c in cells)
    # An amount-like cell; bare digit strings are serials or references.
    has_number = any(_is_numeric(c) and not c.isdigit() for c in cells)
    return has_date and has_number


def find_header_row_index(rows: list[list[str]]) -> int | None:
    """The first header-looking row immediately followed by a
    transaction-looking row of the same width."""
    for i, row in enumerate(rows[:-1]):
        if not _looks_like_header(row):
            continue
        following = next((r for r in rows[i + 1:] if any(c.strip() for c in r)), None)
        if following and len(following) == len(row) and _looks_like_transaction(following):
            return i
    return None


def table_rows(rows: list[list[str]], header_index: int, limit: int = 400) -> list[list[str]]:
    """Data rows under the header, until the first non-blank row of a
    different width (the footer)."""
    width = len(rows[header_index])
    data = []
    for row in rows[header_index + 1:]:
        if not any(c.strip() for c in row):
            continue
        if len(row) != width:
            break
        data.append(row)
        if len(data) >= limit:
            break
    return data


def _column(data: list[list[str]], index: int) -> list[str]:
    return [r[index].strip() for r in data if index < len(r) and r[index].strip()]


def build_profiles(
    rows: list[list[str]],
    header_index: int,
    column_map: dict[str, int],
    currency: str = "",
) -> list[BankProfile]:
    """Turn a column mapping into concrete profile(s), filling in date
    formats, decimal separator, amount convention and debit marker from the
    cells. Returns [] when the mapping contradicts the data (e.g. a 'date'
    column that doesn't hold dates). A signed-amount mapping yields both sign
    conventions; validation decides between them."""
    data = table_rows(rows, header_index)
    if not data:
        return []

    date_formats: list[str] = []
    for field in ("posted_at", "value_date"):
        if field in column_map:
            found = detect_date_formats(_column(data, column_map[field]))
            if not found:
                return []
            if found[0] not in date_formats:
                date_formats.append(found[0])
    if not date_formats:
        return []

    money = [v for f in ("amount", "debit_amount", "credit_amount", "balance")
             if f in column_map for v in _column(data, column_map[f])]
    if any(not _is_numeric(v) for v in money):
        return []

    if "amount" in column_map and "dr_cr" in column_map:
        convention = "dual_drcr"
    elif "debit_amount" in column_map and "credit_amount" in column_map:
        convention = "separate"
    elif "amount" in column_map:
        convention = "signed"
    else:
        return []

    descriptions = _column(data, column_map["description"]) if "description" in column_map else []
    upi_share = sum(d.startswith("UPI/") for d in descriptions) / max(len(descriptions), 1)

    base = dict(
        header_signature=rows[header_index],
        column_map=dict(column_map),
        date_formats=date_formats,
        decimal_separator=detect_decimal_separator(money),
        amount_convention=convention,
        debit_marker=(pick_debit_marker(_column(data, column_map["dr_cr"]))
                      if convention == "dual_drcr" else "DR"),
        currency=currency,
        description_convention="upi" if upi_share >= 0.1 else "generic",
    )
    profiles = []
    for debit_negative in ([True, False] if convention == "signed" else [True]):
        try:
            profiles.append(BankProfile(**base, signed_debit_negative=debit_negative))
        except ValidationError:
            pass
    return profiles


def _content_candidates(rows, header_index, currency) -> list[BankProfile]:
    """Column roles inferred from cell contents alone. Every candidate maps
    a balance column, so the running-balance check can verify it — without a
    balance there's nothing to verify against, and we don't guess."""
    data = table_rows(rows, header_index)
    width = len(rows[header_index])
    dates, numeric, markers, texts = [], [], [], []
    for c in range(width):
        values = _column(data, c)
        if not values:
            continue
        ints = [v for v in values if v.isdigit()]
        if len(ints) == len(values) and len(values) > 1:
            nums = [int(v) for v in values]
            steps = {b - a for a, b in zip(nums, nums[1:])}
            if steps <= {1} or steps <= {-1}:
                continue  # serial-number column
        formats = detect_date_formats(values)
        if formats:
            dates.append((c, formats[0]))
        elif all(_is_numeric(v) for v in values):
            numeric.append(c)
        elif (len({v.upper() for v in values}) <= 4
              and all(v.isalpha() and len(v) <= 10 for v in values)):
            markers.append(c)
        else:
            texts.append((c, sum(map(len, values)) / len(values)))
    if not dates or not texts or len(numeric) < 2:
        return []

    # A timestamped date column is the posting date; another is value date.
    dates.sort(key=lambda d: (":" not in d[1], d[0]))
    base = {"posted_at": dates[0][0], "description": max(texts, key=lambda t: t[1])[0]}
    if len(dates) > 1:
        base["value_date"] = dates[1][0]

    maps: list[dict[str, int]] = []
    for balance in numeric:
        others = [c for c in numeric if c != balance]
        for amount in others:
            for marker in markers:
                maps.append({**base, "amount": amount, "dr_cr": marker, "balance": balance})
            maps.append({**base, "amount": amount, "balance": balance})
        for debit in others:
            for credit in others:
                if debit != credit:
                    maps.append({**base, "debit_amount": debit,
                                 "credit_amount": credit, "balance": balance})

    candidates = []
    for column_map in maps:
        candidates.extend(build_profiles(rows, header_index, column_map, currency))
    return candidates


def _valid(rows, profile: BankProfile) -> bool:
    checkable = profile.model_copy(update={"currency": profile.currency or "XXX"})
    return not validate_profile(rows, checkable)


def detect_profile(rows: list[list[str]]) -> BankProfile | None:
    """A validated profile, or None when the file can't be read with
    confidence (the caller then asks the LLM to label the headers, and
    finally the user)."""
    header_index = find_header_row_index(rows)
    if header_index is None:
        return None
    currency = detect_currency(rows, header_index)

    named = label_headers_by_name(rows[header_index])
    for profile in build_profiles(rows, header_index, named, currency):
        if _valid(rows, profile):
            return profile
    for profile in _content_candidates(rows, header_index, currency):
        if _valid(rows, profile):
            return profile
    return None
