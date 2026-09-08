"""Decide which ingestion path a statement file takes. No LLM involved."""

from pathlib import Path
from typing import Literal

FormatTag = Literal["csv", "xlsx", "ofx", "qif", "mt940", "camt053"]

SUPPORTED = "CSV, XLSX, OFX/QFX, QIF, MT940 (.sta), CAMT.053 XML"

_EXTENSION_MAP: dict[str, FormatTag] = {
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".ofx": "ofx",
    ".qfx": "ofx",
    ".qif": "qif",
    ".sta": "mt940",
    ".mt940": "mt940",
}


class UnknownFormatError(ValueError):
    pass


def _sniff_text(head: str) -> FormatTag | None:
    upper = head.upper()
    if "OFXHEADER" in upper or "<OFX" in upper:
        return "ofx"
    if head.lstrip().startswith("!Type:"):
        return "qif"
    if ":20:" in head and ":61:" in head:
        return "mt940"
    if "BkToCstmrStmt" in head:
        return "camt053"
    return None


def detect_format(path: Path) -> FormatTag:
    """Extension first, then content sniffing — extensions lie sometimes
    (banks ship OFX with a .txt name, CAMT with a bare .xml name)."""
    with open(path, "rb") as f:
        head_bytes = f.read(4096)
    if head_bytes[:4] == b"PK\x03\x04":
        return "xlsx"
    head = head_bytes.decode("utf-8", errors="replace")

    sniffed = _sniff_text(head)
    suffix = path.suffix.lower()

    if suffix == ".xml":
        if sniffed == "camt053":
            return "camt053"
        raise UnknownFormatError(
            f"{path.name} is XML but not a CAMT.053 statement. Supported formats: {SUPPORTED}"
        )
    if suffix in _EXTENSION_MAP:
        return sniffed or _EXTENSION_MAP[suffix]
    if sniffed:
        return sniffed
    raise UnknownFormatError(
        f"Could not recognize the format of {path.name}. Supported formats: {SUPPORTED}"
    )
