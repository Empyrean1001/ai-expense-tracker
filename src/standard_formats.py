"""Deterministic parsers for standardized statement formats.

OFX/QFX, QIF, MT940 and CAMT.053 are specifications — the LLM is never used
where a standard already answers the question (design decision D-9).
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from src.localeparse import LocaleParseError, parse_amount, parse_datetime_multi
from src.models import Transaction


class StandardFormatError(ValueError):
    pass


# --- OFX / QFX -------------------------------------------------------------

_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.DOTALL | re.IGNORECASE)
_OFX_FIELD = re.compile(r"<(\w+)>([^<\r\n]*)")


def _ofx_datetime(raw: str) -> datetime:
    match = re.match(r"\d+", raw.strip())  # drop timezone suffixes like [-5:EST]
    digits = match.group(0) if match else ""
    if len(digits) >= 14:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
    if len(digits) >= 8:
        return datetime.strptime(digits[:8], "%Y%m%d")
    raise StandardFormatError(f"Unreadable OFX date {raw!r}")


def parse_ofx(path: Path) -> list[Transaction]:
    text = path.read_text(encoding="utf-8", errors="replace")
    currency_match = re.search(r"<CURDEF>(\w{3})", text, re.IGNORECASE)
    currency = currency_match.group(1).upper() if currency_match else ""
    if not currency:
        raise StandardFormatError(f"{path.name}: no <CURDEF> currency in OFX file")

    transactions = []
    for block in _STMTTRN.findall(text):
        fields = {name.upper(): value.strip() for name, value in _OFX_FIELD.findall(block)}
        amount = Decimal(fields["TRNAMT"])
        posted = _ofx_datetime(fields["DTPOSTED"])
        name = fields.get("NAME", "")
        memo = fields.get("MEMO", "")
        description = f"{name} {memo}".strip() if memo else name
        transactions.append(Transaction(
            posted_at=posted,
            value_date=posted.date(),
            description=description or "(no description)",
            reference=fields.get("FITID", ""),
            amount=abs(amount),
            direction="debit" if amount < 0 else "credit",
            currency=currency,
            vendor=name,
        ))
    return transactions


# --- QIF -------------------------------------------------------------------

def parse_qif(
    path: Path,
    currency: str,
    date_formats: list[str] | None = None,
) -> list[Transaction]:
    """QIF carries no currency — the caller supplies the account's currency."""
    date_formats = date_formats or ["%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y"]
    transactions = []
    record: dict[str, str] = {}

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.rstrip()
        if not line or line.startswith("!"):
            continue
        if line == "^":
            if record:
                transactions.append(_qif_record(record, currency, date_formats))
                record = {}
            continue
        record[line[0]] = line[1:].strip()
    if record:
        transactions.append(_qif_record(record, currency, date_formats))
    return transactions


def _qif_record(record: dict[str, str], currency: str, date_formats: list[str]) -> Transaction:
    if "D" not in record or ("T" not in record and "U" not in record):
        raise StandardFormatError(f"QIF record missing date or amount: {record}")
    posted = parse_datetime_multi(record["D"].replace("'", "/"), date_formats)
    amount = parse_amount(record.get("T") or record["U"], ".")
    payee = record.get("P", "")
    memo = record.get("M", "")
    description = f"{payee} {memo}".strip() if memo else payee
    return Transaction(
        posted_at=posted,
        value_date=posted.date(),
        description=description or "(no description)",
        reference=record.get("N", ""),
        amount=abs(amount),
        direction="debit" if amount < 0 else "credit",
        currency=currency,
        vendor=payee,
    )


# --- MT940 -----------------------------------------------------------------

_MT940_61 = re.compile(
    r"^:61:(?P<value>\d{6})(?P<entry>\d{4})?(?P<mark>R?[DC])(?P<funds>[A-Z])?(?P<amount>[\d,]+)"
)
_MT940_60F = re.compile(r"^:60F:[CD]\d{6}(?P<ccy>[A-Z]{3})")


def parse_mt940(path: Path) -> list[Transaction]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    currency = ""
    transactions: list[Transaction] = []
    pending: dict | None = None

    def flush():
        nonlocal pending
        if pending is not None:
            transactions.append(Transaction(**pending))
            pending = None

    for line in lines:
        line = line.strip()
        opening = _MT940_60F.match(line)
        if opening:
            currency = opening.group("ccy")
            continue
        txn_line = _MT940_61.match(line)
        if txn_line:
            flush()
            if not currency:
                raise StandardFormatError(f"{path.name}: :61: before :60F: opening balance")
            value_date = datetime.strptime(txn_line.group("value"), "%y%m%d")
            mark = txn_line.group("mark")
            pending = dict(
                posted_at=value_date,
                value_date=value_date.date(),
                description="",
                reference="",
                amount=parse_amount(txn_line.group("amount"), ","),
                direction="debit" if mark.endswith("D") else "credit",
                currency=currency,
            )
            continue
        if line.startswith(":86:") and pending is not None:
            detail = line[4:].strip()
            pending["description"] = (
                f"{pending['description']} {detail}".strip() if pending["description"] else detail
            )
        elif line.startswith(":") and pending is not None and not line.startswith(":86:"):
            flush()
    flush()
    for txn in transactions:
        if not txn.description:
            txn.description = "(no description)"
    return transactions


# --- CAMT.053 --------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(element: ET.Element, local_name: str) -> str:
    for node in element.iter():
        if _local(node.tag) == local_name and node.text:
            return node.text.strip()
    return ""


def parse_camt053(path: Path) -> list[Transaction]:
    root = ET.parse(path).getroot()
    transactions = []
    for entry in root.iter():
        if _local(entry.tag) != "Ntry":
            continue
        amount_node = next(
            (node for node in entry.iter() if _local(node.tag) == "Amt"), None
        )
        if amount_node is None or not amount_node.text:
            raise StandardFormatError(f"{path.name}: CAMT entry without <Amt>")
        currency = amount_node.get("Ccy", "")
        if not currency:
            raise StandardFormatError(f"{path.name}: CAMT <Amt> without Ccy attribute")
        indicator = _find_text(entry, "CdtDbtInd")
        booking = _find_text(entry, "Dt") or _find_text(entry, "DtTm")
        value = ""
        for node in entry.iter():
            if _local(node.tag) == "ValDt":
                value = _find_text(node, "Dt")
                break
        posted = datetime.fromisoformat(booking)
        description = _find_text(entry, "Ustrd") or _find_text(entry, "AddtlNtryInf")
        transactions.append(Transaction(
            posted_at=posted,
            value_date=datetime.fromisoformat(value).date() if value else posted.date(),
            description=description or "(no description)",
            reference=_find_text(entry, "AcctSvcrRef"),
            amount=Decimal(amount_node.text.strip()),
            direction="debit" if indicator == "DBIT" else "credit",
            currency=currency,
        ))
    if not transactions:
        raise StandardFormatError(f"{path.name}: no <Ntry> entries found")
    return transactions
