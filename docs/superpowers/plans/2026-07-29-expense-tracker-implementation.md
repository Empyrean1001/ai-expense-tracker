# Expense Tracker Implementation Plan

> **SUPERSEDED** by `2026-09-08-expense-tracker-v2-implementation.md`. Kept as the reference for module designs the v2 plan ports (vendor cache, p2p detector, review, ledger, split tracker, insights, dashboard).

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI that turns a monthly bank statement CSV into a categorized, insight-rich Excel dashboard, using a locally-hosted LLM (Ollama) for categorization and a user-driven ledger for lend/borrow and split tracking.

**Architecture:** A `process` command runs the monthly pipeline, modeled as a **LangGraph `StateGraph`** (parse → categorize via local Ollama model → flag ambiguous items for interactive terminal review → append to a flat-file transaction log → generate an Excel workbook). Two standalone commands, `split` and `settle`, manage the lend/borrow ledger independently of the monthly run, never touch the LLM, and are plain CLI commands rather than graph nodes. All persistence is flat files (CSV/JSON/YAML) — no database.

**Testing:** deliberately out of scope for this plan. The modules are written so tests can be added later without restructuring — every unit that talks to the outside world (the LLM, the terminal) takes it as an injected parameter (`model`, `prompt_fn`, `echo_fn`) rather than reaching for a global. Each task ends with a runnable smoke check against the real sample statement instead of a test suite.

**Tech Stack:**
- Python 3.11+
- `langgraph` — orchestrates the monthly pipeline as a `StateGraph` (parse/categorize/review/persist/dashboard nodes, with a conditional edge that short-circuits when there are no new transactions)
- `langchain-ollama` (`ChatOllama`) + `langchain-core` — the categorization LLM call, using `.with_structured_output(CategorizationResult)` against the local Ollama server
- `httpx` — the Ollama pre-flight reachability/model check (already a transitive dependency of `langchain-ollama`; pinned explicitly because we import it directly)
- `pandas` — CSV/data wrangling, groupby/aggregation, the Trends sheet's month×category matrix
- `openpyxl` — Excel workbook generation: native Tables, PieChart/BarChart/LineChart, conditional formatting
- `pydantic` (v2) — validates LLM structured output and models all persisted records
- `PyYAML` — reads `categories.yaml` / `config.yaml`
- `click` — CLI commands (`process`, `split`, `settle`) and interactive terminal prompts

## Global Constraints

- No cloud LLM calls anywhere — categorization only ever calls the local Ollama host configured in `config.yaml` (spec: "LLM runtime").
- Default Ollama model is `qwen2.5:3b-instruct`, overridable via `config.yaml` without code changes (spec: "LLM runtime").
- No database — `data/transactions.csv`, `data/ledger.json`, `data/vendor_cache.json` are the only persistent stores (spec: "Persistent data files").
- The parser targets exactly one column layout (`Sl. No.`, `Transaction Date`, `Value Date`, `Description`, `Chq /Ref No.`, `Amount`, `Dr / Cr`, `Balance`, `Dr / Cr`), matched whitespace-normalized (spec: "Input format").
- Splits are never LLM-inferred — `split` is a standalone command operating only on already-imported transactions (spec: "Purpose", "Split Tracker").
- A split's recorded shares must sum to strictly less than the transaction total — the remainder is the user's own share (spec: "Error handling").
- The Excel workbook has exactly six sheets in the documented order (Dashboard, Transactions, Category Summary, Trends, Lend & Borrow Ledger, Insights); "pivot-style" tables are pandas-computed value tables, not native Excel PivotTable objects (spec: "Excel Dashboard Format").
- Real bank data (`example doc/`, any real statement, `data/`, `output/`, `statements/`) must never be committed to git. The existing `.gitignore` already covers all of these — do not add exceptions to it.
- The monthly pipeline is a LangGraph `StateGraph`; every node wraps a plain function from `src/` (parser, categorizer, review, ledger, dashboard) so the graph stays a thin orchestration layer.
- The Interactive CLI Review step runs as an ordinary synchronous function call inside the `review` node (blocking on `click.prompt`), **not** LangGraph's `interrupt()`/checkpointer mechanism — a deliberate scope decision: `interrupt()` earns its complexity when a graph run must survive a process restart, and this is a single local CLI invocation. Revisit if the pipeline ever needs to pause/resume across sessions.
- **Column names are fixed by `transactions_store.CSV_COLUMNS`.** Every consumer (dashboard, insights, split search) reads those exact names — `txn_datetime`, `value_date`, `source_month`, `dr_cr`, `dedup_hash`. There is no column named `date`.

---

## File Structure

```
expense tracker/
├── track_expenses.py          # CLI entry point (click group: process/split/settle)
├── requirements.txt
├── config.yaml                # Ollama host + model name + review threshold
├── categories.yaml            # editable category/subcategory taxonomy
├── src/
│   ├── __init__.py
│   ├── models.py              # Pydantic models shared by every module
│   ├── config_loader.py       # loads config.yaml / categories.yaml
│   ├── parser.py              # Statement Parser
│   ├── transactions_store.py  # data/transactions.csv read/append/dedup
│   ├── vendor_cache.py        # data/vendor_cache.json read/write
│   ├── p2p_detector.py        # person-vs-merchant heuristic
│   ├── ollama_client.py       # categorization LLM call + Ollama pre-flight check
│   ├── categorizer.py         # Categorization Agent (vendor cache + LLM call)
│   ├── review.py              # Interactive CLI Review
│   ├── ledger.py              # Ledger store: direct/split entries, settle, outstanding
│   ├── split_tracker.py       # Split Tracker: search + equal/custom split
│   ├── insights.py            # Insights Engine
│   ├── dashboard.py           # Excel Dashboard Generator (6 sheets)
│   └── graph.py               # LangGraph StateGraph wiring the monthly pipeline
├── example doc/    # gitignored — the real sample statement lives here
├── data/           # gitignored — runtime state, created on first run
├── output/         # gitignored — generated workbooks
└── statements/     # gitignored — where monthly bank exports get dropped
```

Each `src/` module has one responsibility. `track_expenses.py` is the only file that constructs a real `ChatOllama`; `src/graph.py` is the only file that knows the order the modules run in.

**Dependency order** (drives task order): `models` → `config_loader` / `parser` / `transactions_store` / `vendor_cache` / `p2p_detector` / `ledger` / `insights` → `ollama_client` → `categorizer` (needs `p2p_detector`) → `review` (needs `p2p_detector`) → `split_tracker` → `dashboard` (needs `insights` + `ledger`) → `graph` → `track_expenses`.

---

### Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`, `config.yaml`, `categories.yaml`, `src/__init__.py`

**Produces:** the taxonomy and runtime config every later task reads.

- [ ] **Step 1: Create `requirements.txt`**

```
pandas>=2.2
openpyxl>=3.1
pydantic>=2.6
PyYAML>=6.0
click>=8.1
httpx>=0.27
langgraph>=0.2
langchain-core>=0.3
langchain-ollama>=0.3
```

- [ ] **Step 2: Create `config.yaml`**

```yaml
ollama:
  host: "http://localhost:11434"
  model: "qwen2.5:3b-instruct"

review:
  # Categorizations below this confidence get shown to you for confirmation.
  confidence_threshold: 0.6
```

- [ ] **Step 3: Create `categories.yaml`**

```yaml
Food & Dining:
  - Groceries
  - Restaurants
  - Delivery (Swiggy/Zomato)
Transport:
  - Fuel
  - Cabs (Uber/Ola)
  - Public Transit
Bills & Utilities:
  - Electricity
  - Internet/Phone
  - Rent
Shopping:
  - Clothing
  - Electronics
  - Online (Amazon/Flipkart)
  - Home
Entertainment:
  - Streaming
  - Movies/Events
  - Subscriptions
Health:
  - Pharmacy
  - Doctor/Hospital
Income:
  - Salary
  - Refunds
  - Gifts
Other/Uncategorized:
  - ""
```

Note: `Lend/Borrow` is deliberately **not** a category here. Lend/borrow lives in `data/ledger.json` as its own record type, and a split transaction keeps its real spending category — putting it in the taxonomy would let the LLM assign it, which the spec forbids.

- [ ] **Step 4: Create `src/__init__.py`**

Empty file.

- [ ] **Step 5: Install and verify**

```bash
pip install -r requirements.txt
python -c "import yaml, pandas, openpyxl, pydantic, click, httpx, langgraph, langchain_ollama; print('deps ok')"
python -c "import yaml; print(list(yaml.safe_load(open('categories.yaml')).keys()))"
```

Expected: `deps ok`, then the list of category names.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt config.yaml categories.yaml src/__init__.py
git commit -m "Scaffold project: dependencies, runtime config, category taxonomy"
```

---

### Task 2: Data models

**Files:** Create `src/models.py`

**Produces:** `Transaction`, `CategorizationResult`, `DirectLedgerEntry`, `SplitShare`, `SplitLedgerEntry` — used by every later task.

- [ ] **Step 1: Write `src/models.py`**

```python
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """One row of a bank statement, enriched as it moves through the pipeline."""

    dedup_hash: str
    txn_datetime: datetime
    value_date: date
    description: str
    ref_no: str = ""
    amount: float
    dr_cr: Literal["DR", "CR"]
    balance: float
    vendor: str = ""
    description_note: str = ""
    category: str = "Other/Uncategorized"
    subcategory: str = ""
    confidence: float = 0.0
    source_month: str = ""
    flag: str = ""


class CategorizationResult(BaseModel):
    """What the local model returns for one transaction.

    Every field has a default: this doubles as the JSON schema handed to a 3B
    model, and a missing field should degrade to "review this one" rather than
    blow up the whole run.
    """

    vendor: str = ""
    category: str = "Other/Uncategorized"
    subcategory: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DirectLedgerEntry(BaseModel):
    """A whole transaction that was actually a loan to/from one person."""

    type: Literal["direct"] = "direct"
    person: str
    direction: Literal["lent", "borrowed"]
    amount: float
    entry_date: date
    status: Literal["unsettled", "settled"] = "unsettled"
    linked_transaction_hash: str


class SplitShare(BaseModel):
    person: str
    amount: float
    status: Literal["unsettled", "settled"] = "unsettled"


class SplitLedgerEntry(BaseModel):
    """A real expense you paid in full, where others owe you part of it.

    The linked transaction keeps its own category and full amount — this record
    only tracks what comes back.
    """

    type: Literal["split"] = "split"
    linked_transaction_hash: str
    total_amount: float
    splits: list[SplitShare]
    entry_date: date
```

- [ ] **Step 2: Verify**

```bash
python -c "from src.models import Transaction, CategorizationResult, DirectLedgerEntry, SplitLedgerEntry, SplitShare; print(CategorizationResult().model_dump())"
```

Expected: `{'vendor': '', 'category': 'Other/Uncategorized', 'subcategory': '', 'confidence': 0.0}`

- [ ] **Step 3: Commit**

```bash
git add src/models.py
git commit -m "Add Pydantic models for transactions and ledger entries"
```

---

### Task 3: Config loader

**Files:** Create `src/config_loader.py`

**Produces:** `load_config(path) -> dict`, `load_categories(path) -> dict[str, list[str]]`

- [ ] **Step 1: Write `src/config_loader.py`**

```python
from pathlib import Path

import yaml


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_categories(path: Path) -> dict[str, list[str]]:
    """Category -> list of subcategories. Empty-string subcategories are kept as
    written so callers can distinguish 'no subcategories' from a missing key."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
```

- [ ] **Step 2: Verify**

```bash
python -c "from pathlib import Path; from src.config_loader import load_config, load_categories; print(load_config(Path('config.yaml'))['ollama']); print(load_categories(Path('categories.yaml'))['Transport'])"
```

Expected: the ollama dict, then `['Fuel', 'Cabs (Uber/Ola)', 'Public Transit']`.

- [ ] **Step 3: Commit**

```bash
git add src/config_loader.py
git commit -m "Add config.yaml/categories.yaml loader"
```

---

### Task 4: Statement Parser

**Files:** Create `src/parser.py`

**Produces:** `ParserError`, `extract_vendor_and_note(description) -> tuple[str, str]`, `compute_dedup_hash(value_date, ref_no, amount, dr_cr) -> str`, `parse_statement(csv_path) -> list[Transaction]`

The real export is a CSV with a non-tabular metadata block on top, a `Closing Balance` row plus free-text notes at the bottom, Indian comma grouping in the numeric columns, and two separate `Dr / Cr` columns (one qualifies `Amount`, one qualifies `Balance` — only the first is used).

- [ ] **Step 1: Write `src/parser.py`**

```python
import csv
import hashlib
import re
from datetime import date, datetime
from pathlib import Path

from src.models import Transaction

# Header cells are compared with all whitespace stripped and lowercased, because
# the bank's own spacing is inconsistent ("Chq /Ref No.", "Dr / Cr").
EXPECTED_HEADER = [
    "sl.no.", "transactiondate", "valuedate", "description",
    "chq/refno.", "amount", "dr/cr", "balance", "dr/cr",
]


class ParserError(Exception):
    pass


def _normalize_header_cell(cell: str) -> str:
    return re.sub(r"\s+", "", cell).lower()


def _find_table_bounds(rows: list[list[str]]) -> tuple[int, int]:
    """Return (header_row_index, end_index_exclusive) for the transaction table."""
    start = None
    for i, row in enumerate(rows):
        if row and _normalize_header_cell(row[0]) == "sl.no.":
            start = i
            break
    if start is None:
        raise ParserError(
            "Could not find the transaction table — no row starts with 'Sl. No.'. "
            "Is this a bank statement export?"
        )

    end = len(rows)
    for i in range(start + 1, len(rows)):
        if rows[i] and rows[i][0].strip().lower() == "closing balance":
            end = i
            break
    return start, end


def _parse_amount(raw: str) -> float:
    """'1,33,116.37' -> 133116.37"""
    cleaned = raw.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ParserError(f"Could not read amount {raw!r}") from exc


def _parse_datetime(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ParserError(f"Could not read date/time {raw!r} (expected DD-MM-YYYY[ HH:MM:SS])")


def extract_vendor_and_note(description: str) -> tuple[str, str]:
    """Split 'UPI/<NAME>/<ref>/<note>' into (name, note).

    The trailing note is what the payer typed at payment time ('auto', 'lunch').
    It is the only clue to what a payment to an individual was actually for, so
    it is carried separately all the way to the model prompt.
    """
    if description.startswith("UPI/"):
        parts = description.split("/")
        vendor = parts[1].strip() if len(parts) > 1 else description.strip()
        note = parts[3].strip() if len(parts) > 3 else ""
        return vendor, note
    return description.strip(), ""


def compute_dedup_hash(value_date: date, ref_no: str, amount: float, dr_cr: str) -> str:
    key = f"{value_date.isoformat()}|{ref_no}|{amount:.2f}|{dr_cr}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_statement(csv_path: Path) -> list[Transaction]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    start, end = _find_table_bounds(rows)
    header = [_normalize_header_cell(c) for c in rows[start]]
    if header != EXPECTED_HEADER:
        raise ParserError(
            "Unexpected column layout.\n"
            f"  Expected: {EXPECTED_HEADER}\n"
            f"  Found:    {header}"
        )

    transactions = []
    for row in rows[start + 1:end]:
        if not row or not row[0].strip():
            continue
        if len(row) != len(EXPECTED_HEADER):
            raise ParserError(
                f"Expected {len(EXPECTED_HEADER)} columns in a transaction row, found {len(row)}: {row}"
            )

        _, txn_dt_raw, value_date_raw, description, ref_no, amount_raw, dr_cr, balance_raw, _ = row

        txn_datetime = _parse_datetime(txn_dt_raw)
        value_date = _parse_datetime(value_date_raw).date()
        amount = _parse_amount(amount_raw)
        balance = _parse_amount(balance_raw)
        dr_cr = dr_cr.strip().upper()
        if dr_cr not in ("DR", "CR"):
            raise ParserError(f"Expected DR or CR, found {dr_cr!r} in row: {row}")

        vendor, note = extract_vendor_and_note(description)
        ref_no = ref_no.strip()

        transactions.append(Transaction(
            dedup_hash=compute_dedup_hash(value_date, ref_no, amount, dr_cr),
            txn_datetime=txn_datetime,
            value_date=value_date,
            description=description.strip(),
            ref_no=ref_no,
            amount=amount,
            dr_cr=dr_cr,
            balance=balance,
            vendor=vendor,
            description_note=note,
            # The statement's period is defined by value dates, not posting
            # timestamps — an interest credit posted on 01-08 with a value date
            # of 31-07 belongs to July.
            source_month=value_date.strftime("%Y-%m"),
        ))

    return transactions
```

- [ ] **Step 2: Verify against the real sample statement**

```bash
python -c "import glob; from src.parser import parse_statement; t = parse_statement(glob.glob('example doc/*.csv')[0]); print(len(t), 'transactions'); [print(x.value_date, x.vendor, '|', x.description_note, '|', x.amount, x.dr_cr, x.source_month) for x in t]"
```

Expected: every transaction row from the sample, with vendors and notes split out of the UPI descriptions, amounts as floats, and `source_month` derived from the value date.

- [ ] **Step 3: Commit**

```bash
git add src/parser.py
git commit -m "Add Statement Parser: header/footer detection, Indian number format, dedup hashing"
```

---

### Task 5: Transactions store

**Files:** Create `src/transactions_store.py`

**Produces:** `CSV_COLUMNS`, `load_existing_hashes(path) -> set[str]`, `append_transactions(path, transactions) -> None`, `read_all_transactions(path) -> DataFrame`

`CSV_COLUMNS` is the contract every downstream consumer reads against.

- [ ] **Step 1: Write `src/transactions_store.py`**

```python
import csv
from pathlib import Path

import pandas as pd

from src.models import Transaction

CSV_COLUMNS = [
    "dedup_hash", "txn_datetime", "value_date", "description", "ref_no",
    "amount", "dr_cr", "balance", "vendor", "description_note",
    "category", "subcategory", "confidence", "source_month", "flag",
]

# Columns that must stay strings. Without this, pandas turns blank cells into
# NaN, and a NaN flag/subcategory ends up printed as "nan" in the workbook.
_STRING_COLUMNS = [
    "dedup_hash", "txn_datetime", "value_date", "description", "ref_no",
    "dr_cr", "vendor", "description_note", "category", "subcategory",
    "source_month", "flag",
]


def load_existing_hashes(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {row["dedup_hash"] for row in csv.DictReader(f)}


def append_transactions(csv_path: Path, transactions: list[Transaction]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for txn in transactions:
            row = txn.model_dump(mode="json")
            writer.writerow({col: row[col] for col in CSV_COLUMNS})


def read_all_transactions(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(columns=CSV_COLUMNS)

    df = pd.read_csv(csv_path, dtype={col: str for col in _STRING_COLUMNS})
    for col in _STRING_COLUMNS:
        if col in df.columns:
            df[col] = df[col].fillna("")
    return df
```

- [ ] **Step 2: Verify a parse → append → read round trip**

```bash
python -c "import glob, tempfile, os; from pathlib import Path; from src.parser import parse_statement; from src.transactions_store import append_transactions, read_all_transactions, load_existing_hashes; p = Path(tempfile.mkdtemp())/'t.csv'; t = parse_statement(glob.glob('example doc/*.csv')[0]); append_transactions(p, t); df = read_all_transactions(p); print(df.shape); print(list(df.columns)); print(len(load_existing_hashes(p)), 'hashes')"
```

Expected: shape `(<n>, 15)`, the exact `CSV_COLUMNS` list, and one hash per transaction.

- [ ] **Step 3: Commit**

```bash
git add src/transactions_store.py
git commit -m "Add transactions.csv store with dedup-hash lookup and append"
```

---

### Task 6: Vendor cache

**Files:** Create `src/vendor_cache.py`

**Produces:** `normalize_vendor_key(vendor) -> str`, `load_vendor_cache(path) -> dict[str, CategorizationResult]`, `save_vendor_cache(path, cache) -> None`

- [ ] **Step 1: Write `src/vendor_cache.py`**

```python
import json
from pathlib import Path

from src.models import CategorizationResult


def normalize_vendor_key(vendor: str) -> str:
    return vendor.strip().upper()


def load_vendor_cache(path: Path) -> dict[str, CategorizationResult]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {key: CategorizationResult(**value) for key, value in raw.items()}


def save_vendor_cache(path: Path, cache: dict[str, CategorizationResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({key: result.model_dump() for key, result in cache.items()}, f, indent=2)
```

- [ ] **Step 2: Verify**

```bash
python -c "import tempfile; from pathlib import Path; from src.models import CategorizationResult; from src.vendor_cache import *; p = Path(tempfile.mkdtemp())/'v.json'; save_vendor_cache(p, {'SWIGGY': CategorizationResult(vendor='Swiggy', category='Food & Dining', subcategory='Delivery (Swiggy/Zomato)', confidence=0.95)}); print(load_vendor_cache(p)['SWIGGY'])"
```

Expected: the round-tripped `CategorizationResult`.

- [ ] **Step 3: Commit**

```bash
git add src/vendor_cache.py
git commit -m "Add vendor_cache.json store to avoid re-categorizing known vendors"
```

---

### Task 7: Person-vs-merchant heuristic

**Files:** Create `src/p2p_detector.py`

**Produces:** `looks_like_individual_name(vendor) -> bool`, `is_p2p_candidate(transaction, confidence_threshold) -> bool`

This lands before the categorizer because both the categorizer (cache keying) and the review step (what to prompt about) depend on it.

- [ ] **Step 1: Write `src/p2p_detector.py`**

```python
import re

from src.models import Transaction

# Two to four plain alphabetic words: "RAHUL SHARMA", "MANOJ KUMAR S".
# Anything with a digit or symbol ("BMTC BUS KA01F2", "SWIGGY*ORDER123") is a
# merchant string, and a single word is almost always a brand.
_INDIVIDUAL_NAME_PATTERN = re.compile(r"^[A-Za-z]+(?:\s+[A-Za-z]+){1,3}$")


def looks_like_individual_name(vendor: str) -> bool:
    return bool(_INDIVIDUAL_NAME_PATTERN.match(vendor.strip()))


def is_p2p_candidate(transaction: Transaction, confidence_threshold: float) -> bool:
    """Should this transaction be offered to the user as a possible loan?

    Real statements are full of UPI payments to individuals that are just
    ordinary small expenses — auto fare, bus fare, paying a shopkeeper. So
    "paid a person" on its own is far too broad. We only ask when the payment
    is to a person AND the pipeline could not confidently explain what it was
    for: either the model had low confidence, or there was no payment note to
    go on in the first place.

    This never decides lend vs. borrow — that is always the user's call.
    """
    if not looks_like_individual_name(transaction.vendor):
        return False

    unexplained = transaction.confidence < confidence_threshold or not transaction.description_note
    return unexplained
```

- [ ] **Step 2: Verify against the real sample's vendors**

```bash
python -c "from src.p2p_detector import looks_like_individual_name as f; [print(f(v), v) for v in ['RAHUL SHARMA','SWIGGY','MANOJ KUMAR S','BMTC BUS KA01F2','VIKRAM  R','Int.Pd:0000000001:01-05-2026 to 31-07-2026']]"
```

Expected: `True` for the person names, `False` for `SWIGGY`, the bus, and the interest row.

- [ ] **Step 3: Commit**

```bash
git add src/p2p_detector.py
git commit -m "Add person-vs-merchant heuristic for surfacing loan candidates"
```

---

### Task 8: Categorization LLM call

**Files:** Create `src/ollama_client.py`

**Produces:** `OllamaCategorizationError`, `check_ollama_available(host, model_name) -> None`, `build_prompt(description, note, categories) -> str`, `categorize_via_llm(description, note, categories, model) -> CategorizationResult`

`model` is any object with `.invoke(prompt) -> CategorizationResult` — exactly what `ChatOllama(...).with_structured_output(CategorizationResult)` returns. Nothing in `src/` constructs it; `track_expenses.py` does and passes it down.

Connection failures are caught up-front by `check_ollama_available` rather than inside the per-transaction call. A dead Ollama server surfacing as a `httpx.ConnectError` deep inside the retry loop would silently mark every single transaction "Other/Uncategorized" — technically a completed run, practically a garbage one.

- [ ] **Step 1: Write `src/ollama_client.py`**

```python
import httpx

from src.models import CategorizationResult

UNCATEGORIZED = "Other/Uncategorized"


class OllamaCategorizationError(Exception):
    pass


def check_ollama_available(host: str, model_name: str) -> None:
    """Fail fast, with the command that fixes it, before processing anything."""
    url = f"{host.rstrip('/')}/api/tags"
    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
    except Exception as exc:
        raise OllamaCategorizationError(
            f"Cannot reach Ollama at {host}.\n"
            f"  Start it with:  ollama serve\n"
            f"  (original error: {exc})"
        ) from exc

    installed = {model.get("name", "") for model in response.json().get("models", [])}
    installed_bases = {name.split(":")[0] for name in installed}
    if model_name not in installed and model_name.split(":")[0] not in installed_bases:
        raise OllamaCategorizationError(
            f"Model '{model_name}' is not installed in Ollama at {host}.\n"
            f"  Pull it with:  ollama pull {model_name}\n"
            f"  Installed: {', '.join(sorted(installed)) or '(none)'}"
        )


def build_prompt(description: str, note: str, categories: dict) -> str:
    lines = []
    for category, subcategories in categories.items():
        subs = ", ".join(s for s in (subcategories or []) if s)
        lines.append(f"- {category}: {subs}" if subs else f"- {category}")
    taxonomy = "\n".join(lines)

    return (
        "You categorize personal bank transactions.\n"
        "Choose exactly one category and one of that category's subcategories "
        "from the list below. Also return a short human-readable vendor name "
        "and how confident you are, from 0.0 to 1.0.\n"
        f"If the description does not make the purpose clear, answer "
        f"'{UNCATEGORIZED}' with a low confidence instead of guessing.\n\n"
        f"Categories:\n{taxonomy}\n\n"
        f"Transaction description: {description}\n"
        f"Payment note: {note or '(none)'}\n"
    )


def categorize_via_llm(
    description: str,
    note: str,
    categories: dict,
    model,
    attempts: int = 2,
) -> CategorizationResult:
    """Categorize one transaction, falling back to 'needs review' rather than raising.

    A small local model will occasionally return output that does not fit the
    schema. That is worth one retry, and after that the transaction should land
    in the user's review queue — not abort a run that may already be dozens of
    transactions deep.
    """
    prompt = build_prompt(description, note, categories)

    for _ in range(attempts):
        try:
            result = model.invoke(prompt)
        except Exception:
            continue
        if result is not None:
            return result

    return CategorizationResult(vendor=description, category=UNCATEGORIZED, subcategory="", confidence=0.0)
```

- [ ] **Step 2: Verify the pre-flight check and a real categorization**

With Ollama running and the model pulled:

```bash
python -c "from src.ollama_client import check_ollama_available; check_ollama_available('http://localhost:11434', 'qwen2.5:3b-instruct'); print('ollama ok')"
```

Expected: `ollama ok`. Then point it at a dead port to confirm the message is useful:

```bash
python -c "
from src.ollama_client import check_ollama_available, OllamaCategorizationError
try:
    check_ollama_available('http://localhost:1', 'qwen2.5:3b-instruct')
except OllamaCategorizationError as exc:
    print(exc)
"
```

Expected: the "Cannot reach Ollama… ollama serve" message, no traceback.

- [ ] **Step 3: Commit**

```bash
git add src/ollama_client.py
git commit -m "Add categorization LLM call via ChatOllama plus Ollama pre-flight check"
```

---

### Task 9: Categorization Agent

**Files:** Create `src/categorizer.py`

**Produces:** `cache_key_for(transaction) -> str`, `categorize_transactions(transactions, vendor_cache, model, categories) -> list[Transaction]`

Mutates the transactions in place and fills `vendor_cache` with anything newly seen; the caller persists the cache.

- [ ] **Step 1: Write `src/categorizer.py`**

```python
from src.models import CategorizationResult, Transaction
from src.ollama_client import categorize_via_llm
from src.p2p_detector import looks_like_individual_name
from src.vendor_cache import normalize_vendor_key


def cache_key_for(transaction: Transaction) -> str:
    """Decide what a cached categorization is actually keyed on.

    For a merchant the vendor alone is the whole story: SWIGGY is Food & Dining
    today and next month, so one cache entry per vendor is right.

    A person's name is not. 'RAHUL SHARMA' says nothing about what the money was
    for — the payment note does ('lunch', 'rent', 'auto'). Keying those on the
    name alone would let the first note ever seen decide the category for every
    future payment to that person, so their key includes the note.
    """
    vendor_key = normalize_vendor_key(transaction.vendor or transaction.description)
    if looks_like_individual_name(transaction.vendor):
        return f"{vendor_key}|{transaction.description_note.strip().upper()}"
    return vendor_key


def categorize_transactions(
    transactions: list[Transaction],
    vendor_cache: dict[str, CategorizationResult],
    model,
    categories: dict,
) -> list[Transaction]:
    for txn in transactions:
        key = cache_key_for(txn)

        if key not in vendor_cache:
            vendor_cache[key] = categorize_via_llm(
                txn.description, txn.description_note, categories, model
            )

        cached = vendor_cache[key]
        txn.vendor = cached.vendor or txn.vendor
        txn.category = cached.category
        txn.subcategory = cached.subcategory
        txn.confidence = cached.confidence

    return transactions
```

- [ ] **Step 2: Verify cache keying separates people from merchants**

```bash
python -c "import glob; from src.parser import parse_statement; from src.categorizer import cache_key_for; [print(cache_key_for(t)) for t in parse_statement(glob.glob('example doc/*.csv')[0])]"
```

Expected: individual-name rows show `NAME|NOTE` keys (so the same person with a different note is categorized separately); merchant/interest rows show a bare vendor key.

- [ ] **Step 3: Commit**

```bash
git add src/categorizer.py
git commit -m "Add Categorization Agent with note-aware cache keys for person payments"
```

---

### Task 10: Interactive CLI Review

**Files:** Create `src/review.py`

**Produces:** `review_transactions(transactions, categories, confidence_threshold, prompt_fn=click.prompt, echo_fn=click.echo) -> list[DirectLedgerEntry]`

Mutates transactions in place (category/subcategory/flag) and returns any direct lend/borrow entries the user confirmed.

- [ ] **Step 1: Write `src/review.py`**

```python
import click

from src.models import DirectLedgerEntry, Transaction
from src.p2p_detector import is_p2p_candidate


def _needs_category(transaction: Transaction, confidence_threshold: float) -> bool:
    return (
        transaction.category == "Other/Uncategorized"
        or transaction.confidence < confidence_threshold
    )


def review_transactions(
    transactions: list[Transaction],
    categories: dict,
    confidence_threshold: float,
    prompt_fn=click.prompt,
    echo_fn=click.echo,
) -> list[DirectLedgerEntry]:
    """Ask the user about anything the pipeline could not settle on its own.

    Two independent reasons to stop on a transaction, and a row can hit both:
      - it looks like money moving between you and a person (possible loan)
      - nothing confidently categorized it

    Answering the loan question can resolve the row entirely, which is why the
    category prompt is skipped once an entry is recorded or the row is skipped.
    """
    ledger_entries: list[DirectLedgerEntry] = []
    category_names = list(categories.keys())

    for txn in transactions:
        is_candidate = is_p2p_candidate(txn, confidence_threshold)
        needs_category = _needs_category(txn, confidence_threshold)
        if not (is_candidate or needs_category):
            continue

        echo_fn(f"\n{txn.value_date}  {txn.description}  {txn.amount:.2f} {txn.dr_cr}")

        if is_candidate:
            choice = prompt_fn(
                "  Money lent or borrowed? [l]ent / [b]orrowed / [e]xpense / [s]kip",
                type=click.Choice(["l", "b", "e", "s"]),
            )
            if choice in ("l", "b"):
                person = prompt_fn("  Person's name").strip()
                ledger_entries.append(DirectLedgerEntry(
                    person=person,
                    direction="lent" if choice == "l" else "borrowed",
                    amount=txn.amount,
                    entry_date=txn.value_date,
                    linked_transaction_hash=txn.dedup_hash,
                ))
                txn.flag = "Lent" if choice == "l" else "Borrowed"
                continue
            if choice == "s":
                continue
            # "e" — an ordinary expense after all; fall through to categorize it.

        if needs_category:
            category = prompt_fn("  Category", type=click.Choice(category_names))
            subcategories = [s for s in (categories.get(category) or []) if s]
            subcategory = (
                prompt_fn("  Subcategory", type=click.Choice(subcategories))
                if subcategories else ""
            )
            txn.category = category
            txn.subcategory = subcategory
            txn.confidence = 1.0

    return ledger_entries
```

- [ ] **Step 2: Verify the prompt flow with a scripted stand-in**

```bash
python -c "
from pathlib import Path
from src.config_loader import load_categories
from src.parser import parse_statement
from src.review import review_transactions
import glob
txns = parse_statement(glob.glob('example doc/*.csv')[0])
answers = iter(['l', 'Test Person'] + ['Other/Uncategorized'] * 20)
entries = review_transactions(txns, load_categories(Path('categories.yaml')), 0.6,
                              prompt_fn=lambda *a, **k: next(answers), echo_fn=print)
print('ledger entries:', entries)
"
```

Expected: it stops on the individual-name rows (everything is uncategorized at this point, since no LLM ran), records one `DirectLedgerEntry` for the first, and does not crash.

- [ ] **Step 3: Commit**

```bash
git add src/review.py
git commit -m "Add Interactive CLI Review for loan candidates and low-confidence rows"
```

---

### Task 11: Ledger store

**Files:** Create `src/ledger.py`

**Produces:** `load_ledger(path)`, `save_ledger(path, entries)`, `add_direct_entry(path, entry)`, `add_split_entry(path, entry)`, `settle_person(path, person) -> int`, `get_outstanding_by_person(entries) -> dict[str, dict[str, float]]`

Entries are stored as plain dicts so the file stays readable and hand-editable.

- [ ] **Step 1: Write `src/ledger.py`**

```python
import json
from collections import defaultdict
from pathlib import Path

from src.models import DirectLedgerEntry, SplitLedgerEntry


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_ledger(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _append(path: Path, entry) -> None:
    entries = load_ledger(path)
    entries.append(json.loads(entry.model_dump_json()))
    save_ledger(path, entries)


def add_direct_entry(path: Path, entry: DirectLedgerEntry) -> None:
    _append(path, entry)


def add_split_entry(path: Path, entry: SplitLedgerEntry) -> None:
    _append(path, entry)


def settle_person(path: Path, person: str) -> int:
    """Mark every unsettled amount for one person as paid. Returns how many."""
    entries = load_ledger(path)
    settled = 0

    for entry in entries:
        if entry["type"] == "direct":
            if entry["person"] == person and entry["status"] == "unsettled":
                entry["status"] = "settled"
                settled += 1
        else:
            for share in entry["splits"]:
                if share["person"] == person and share["status"] == "unsettled":
                    share["status"] = "settled"
                    settled += 1

    save_ledger(path, entries)
    return settled


def get_outstanding_by_person(entries: list[dict]) -> dict[str, dict[str, float]]:
    """Unsettled totals per person. A split share is always money owed to you."""
    outstanding: dict[str, dict[str, float]] = defaultdict(lambda: {"lent": 0.0, "borrowed": 0.0})

    for entry in entries:
        if entry["type"] == "direct":
            if entry["status"] == "unsettled":
                outstanding[entry["person"]][entry["direction"]] += entry["amount"]
        else:
            for share in entry["splits"]:
                if share["status"] == "unsettled":
                    outstanding[share["person"]]["lent"] += share["amount"]

    return dict(outstanding)
```

- [ ] **Step 2: Verify**

```bash
python -c "
import tempfile
from datetime import date
from pathlib import Path
from src.ledger import *
from src.models import DirectLedgerEntry, SplitLedgerEntry, SplitShare
p = Path(tempfile.mkdtemp())/'ledger.json'
add_direct_entry(p, DirectLedgerEntry(person='Rahul', direction='lent', amount=500.0, entry_date=date(2026,7,12), linked_transaction_hash='h1'))
add_split_entry(p, SplitLedgerEntry(linked_transaction_hash='h2', total_amount=2000.0, entry_date=date(2026,7,15), splits=[SplitShare(person='Rahul', amount=300.0), SplitShare(person='Priya', amount=500.0)]))
print(get_outstanding_by_person(load_ledger(p)))
print('settled:', settle_person(p, 'Rahul'))
print(get_outstanding_by_person(load_ledger(p)))
"
```

Expected: Rahul at 800.0 lent (500 direct + 300 split), Priya at 500.0; then `settled: 2` and Rahul drops out while Priya remains.

- [ ] **Step 3: Commit**

```bash
git add src/ledger.py
git commit -m "Add ledger store: direct/split entries, settle, outstanding-by-person"
```

---

### Task 12: Split Tracker

**Files:** Create `src/split_tracker.py`

**Produces:** `search_transactions(df, term) -> DataFrame`, `build_equal_split(total_amount, people) -> list[SplitShare]`, `build_custom_split(shares) -> list[SplitShare]`, `validate_split(total_amount, shares) -> None`, `create_split_entry(transaction_hash, total_amount, entry_date, shares) -> SplitLedgerEntry`

- [ ] **Step 1: Write `src/split_tracker.py`**

```python
from datetime import date

import pandas as pd

from src.models import SplitLedgerEntry, SplitShare


def search_transactions(df: pd.DataFrame, term: str) -> pd.DataFrame:
    """Free-text search over description, vendor, amount and date.

    regex=False matters: a user searching for a vendor like 'CAFE (MG ROAD)'
    should not have their parentheses interpreted as a regex group.
    """
    if df.empty:
        return df

    term_lower = term.strip().lower()
    mask = (
        df["description"].str.lower().str.contains(term_lower, na=False, regex=False)
        | df["vendor"].str.lower().str.contains(term_lower, na=False, regex=False)
        | df["amount"].astype(str).str.contains(term_lower, na=False, regex=False)
        | df["value_date"].astype(str).str.contains(term_lower, na=False, regex=False)
    )
    return df[mask]


def build_equal_split(total_amount: float, people: list[str]) -> list[SplitShare]:
    """Split the bill evenly, including yourself in the head count.

    Only the other people become ledger entries — your own share is spending,
    not a receivable.
    """
    per_person = round(total_amount / (len(people) + 1), 2)
    return [SplitShare(person=person, amount=per_person) for person in people]


def build_custom_split(shares: dict[str, float]) -> list[SplitShare]:
    return [SplitShare(person=person, amount=amount) for person, amount in shares.items()]


def validate_split(total_amount: float, shares: list[SplitShare]) -> None:
    if not shares:
        raise ValueError("A split needs at least one other person.")

    if any(share.amount <= 0 for share in shares):
        raise ValueError("Every share must be greater than zero.")

    total_shared = round(sum(share.amount for share in shares), 2)
    if total_shared >= total_amount:
        raise ValueError(
            f"Split shares total {total_shared:.2f}, which is not less than the "
            f"transaction total of {total_amount:.2f} — your own share must be "
            "greater than zero."
        )


def create_split_entry(
    transaction_hash: str,
    total_amount: float,
    entry_date: date,
    shares: list[SplitShare],
) -> SplitLedgerEntry:
    validate_split(total_amount, shares)
    return SplitLedgerEntry(
        linked_transaction_hash=transaction_hash,
        total_amount=total_amount,
        splits=shares,
        entry_date=entry_date,
    )
```

- [ ] **Step 2: Verify the split math and its guard rail**

```bash
python -c "
from datetime import date
from src.models import SplitShare
from src.split_tracker import build_equal_split, create_split_entry, validate_split

shares = build_equal_split(2000.0, ['Rahul', 'Priya', 'Amit'])
print([(s.person, s.amount) for s in shares])
print(create_split_entry('h1', 2000.0, date(2026, 7, 15), shares).total_amount)

try:
    validate_split(1000.0, [SplitShare(person='A', amount=500.0), SplitShare(person='B', amount=600.0)])
except ValueError as exc:
    print('rejected:', exc)
"
```

Expected: three shares of 500.0 each (2000 split four ways including you), `2000.0`, then a `rejected:` line explaining the over-total split.

- [ ] **Step 3: Commit**

```bash
git add src/split_tracker.py
git commit -m "Add Split Tracker: transaction search, equal/custom splits, validation"
```

---

### Task 13: Insights Engine

**Files:** Create `src/insights.py`

**Produces:** `compute_top_vendors(df, month, n=5)`, `detect_recurring_charges(df, min_occurrences=3, amount_tolerance=0.05)`, `compute_month_over_month_deltas(df)`

All three read the `CSV_COLUMNS` schema and only ever consider debits.

- [ ] **Step 1: Write `src/insights.py`**

```python
import numpy as np
import pandas as pd


def _debits(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["dr_cr"] == "DR"]


def compute_top_vendors(df: pd.DataFrame, month: str, n: int = 5) -> pd.DataFrame:
    month_df = _debits(df)
    month_df = month_df[month_df["source_month"] == month]
    if month_df.empty:
        return pd.DataFrame(columns=["vendor", "amount"])

    grouped = month_df.groupby("vendor", as_index=False)["amount"].sum()
    return grouped.sort_values("amount", ascending=False).head(n).reset_index(drop=True)


def detect_recurring_charges(
    df: pd.DataFrame,
    min_occurrences: int = 3,
    amount_tolerance: float = 0.05,
) -> pd.DataFrame:
    """Vendors billing a near-identical amount across several distinct months.

    Counting distinct months rather than transactions is what separates a
    subscription from a shop you happened to visit three times in one week.
    """
    debit_df = _debits(df)
    rows = []

    for vendor, group in debit_df.groupby("vendor"):
        if group["source_month"].nunique() < min_occurrences:
            continue

        mean_amount = group["amount"].mean()
        if mean_amount <= 0:
            continue

        if ((group["amount"] - mean_amount).abs() <= mean_amount * amount_tolerance).all():
            rows.append({
                "vendor": vendor,
                "avg_amount": mean_amount,
                "occurrences": group["source_month"].nunique(),
            })

    return pd.DataFrame(rows, columns=["vendor", "avg_amount", "occurrences"])


def compute_month_over_month_deltas(df: pd.DataFrame) -> pd.DataFrame:
    debit_df = _debits(df)
    if debit_df.empty:
        return pd.DataFrame(columns=["category", "source_month", "amount", "prev_amount", "pct_change"])

    monthly = debit_df.groupby(["category", "source_month"], as_index=False)["amount"].sum()
    monthly = monthly.sort_values(["category", "source_month"])
    monthly["prev_amount"] = monthly.groupby("category")["amount"].shift(1)
    monthly["pct_change"] = (
        (monthly["amount"] - monthly["prev_amount"]) / monthly["prev_amount"] * 100
    )
    # A category's first month has no baseline, and a jump from zero is not a
    # percentage — leave both blank rather than printing inf.
    monthly["pct_change"] = monthly["pct_change"].replace([np.inf, -np.inf], np.nan)
    return monthly.reset_index(drop=True)
```

- [ ] **Step 2: Verify**

```bash
python -c "
import pandas as pd
from src.insights import *
df = pd.DataFrame([
 {'source_month':'2026-05','vendor':'Netflix','category':'Entertainment','amount':199.0,'dr_cr':'DR'},
 {'source_month':'2026-06','vendor':'Netflix','category':'Entertainment','amount':199.0,'dr_cr':'DR'},
 {'source_month':'2026-07','vendor':'Netflix','category':'Entertainment','amount':199.0,'dr_cr':'DR'},
 {'source_month':'2026-06','vendor':'Swiggy','category':'Food & Dining','amount':1000.0,'dr_cr':'DR'},
 {'source_month':'2026-07','vendor':'Swiggy','category':'Food & Dining','amount':1350.0,'dr_cr':'DR'},
])
print(compute_top_vendors(df,'2026-07'))
print(detect_recurring_charges(df))
print(compute_month_over_month_deltas(df)[['category','source_month','pct_change']])
"
```

Expected: Swiggy above Netflix for July; Netflix (only) detected as recurring; Food & Dining showing `+35.0` for 2026-07 and blank for its first month.

- [ ] **Step 3: Commit**

```bash
git add src/insights.py
git commit -m "Add Insights Engine: top vendors, recurring charges, month-over-month deltas"
```

---

### Task 14: Excel dashboard — shared helpers and the three data sheets

**Files:** Create `src/dashboard.py`

**Produces:** `write_transactions_sheet(wb, month_df)`, `write_category_summary_sheet(wb, month_df)`, `write_trends_sheet(wb, full_df, current_month)`

Every chart in this file is anchored to a small block of computed values written onto the same sheet, because an openpyxl chart can only reference cells — not a DataFrame.

Guard rails throughout: openpyxl will happily emit a table whose range holds only a header row, or a chart pointing at an empty range, and Excel then reports the file as corrupt and "repairs" it. Both are real possibilities on a first run or a sparse month, so each table and chart is written only when it has at least one data row behind it.

- [ ] **Step 1: Write `src/dashboard.py`**

```python
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from src.insights import (
    compute_month_over_month_deltas,
    compute_top_vendors,
    detect_recurring_charges,
)
from src.ledger import get_outstanding_by_person

DATE_FORMAT = "yyyy-mm-dd"


def _remove_default_sheet(wb: Workbook) -> None:
    """Workbook() ships with an empty 'Sheet'; drop it once a real one exists."""
    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
        del wb["Sheet"]


def _add_table(ws, last_row: int, last_col: int, name: str) -> None:
    """Add a native Excel table over A1:<last_col><last_row>, header row included.

    Skipped entirely when there is no data — a header-only table range makes
    Excel flag the workbook as needing repair.
    """
    if last_row < 2:
        return
    table = Table(ref=f"A1:{get_column_letter(last_col)}{last_row}", displayName=name)
    # A fresh style object per table: openpyxl serializes these per-table and
    # sharing one instance across sheets produces a malformed workbook.
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
    ws.add_table(table)


def _write_block(ws, top_row: int, headers: tuple[str, str], pairs) -> int:
    """Write a two-column label/value block for a chart to reference.

    Returns the last row written. The header row is included in the chart's data
    range so `titles_from_data` can name the series.
    """
    ws.cell(row=top_row, column=1, value=headers[0])
    ws.cell(row=top_row, column=2, value=headers[1])
    row = top_row
    for i, (label, value) in enumerate(pairs, start=1):
        row = top_row + i
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=float(value))
    return row


def write_transactions_sheet(wb: Workbook, month_df: pd.DataFrame) -> None:
    ws = wb.create_sheet("Transactions")
    ws.append([
        "Date", "Value Date", "Description", "Vendor", "Category",
        "Subcategory", "Amount", "Dr/Cr", "Balance", "Flag",
    ])

    for _, row in month_df.iterrows():
        ws.append([
            pd.to_datetime(row["txn_datetime"]).to_pydatetime(),
            pd.to_datetime(row["value_date"]).to_pydatetime(),
            row["description"],
            row["vendor"],
            row["category"],
            row["subcategory"],
            float(row["amount"]),
            row["dr_cr"],
            float(row["balance"]),
            row["flag"],
        ])

    for cells in ws.iter_rows(min_row=2, min_col=1, max_col=2):
        for cell in cells:
            cell.number_format = DATE_FORMAT

    _add_table(ws, ws.max_row, 10, "TransactionsTable")
    _remove_default_sheet(wb)


def write_category_summary_sheet(wb: Workbook, month_df: pd.DataFrame) -> None:
    ws = wb.create_sheet("Category Summary")
    debit_df = month_df[month_df["dr_cr"] == "DR"]
    total = float(debit_df["amount"].sum())

    summary = (
        debit_df.groupby(["category", "subcategory"], as_index=False)["amount"]
        .sum()
        .sort_values("amount", ascending=False)
    )
    summary["pct_of_month"] = (summary["amount"] / total * 100).round(2) if total else 0.0

    ws.append(["Category", "Subcategory", "Total Amount", "% of Month"])
    for _, row in summary.iterrows():
        ws.append([
            row["category"], row["subcategory"],
            float(row["amount"]), float(row["pct_of_month"]),
        ])
    last_row = ws.max_row
    _add_table(ws, last_row, 4, "CategorySummaryTable")

    if summary.empty:
        _remove_default_sheet(wb)
        return

    category_totals = (
        debit_df.groupby("category", as_index=False)["amount"]
        .sum()
        .sort_values("amount", ascending=False)
    )
    block_top = last_row + 2
    block_end = _write_block(
        ws, block_top, ("Category", "Total"),
        zip(category_totals["category"], category_totals["amount"]),
    )

    pie = PieChart()
    pie.title = "Spend by Category"
    pie.add_data(Reference(ws, min_col=2, min_row=block_top, max_row=block_end), titles_from_data=True)
    pie.set_categories(Reference(ws, min_col=1, min_row=block_top + 1, max_row=block_end))
    ws.add_chart(pie, f"F{block_top}")

    bar = BarChart()
    bar.type = "bar"
    bar.title = "Subcategory Breakdown"
    bar.add_data(Reference(ws, min_col=3, min_row=1, max_row=last_row), titles_from_data=True)
    bar.set_categories(Reference(ws, min_col=2, min_row=2, max_row=last_row))
    ws.add_chart(bar, f"F{block_end + 2}")

    _remove_default_sheet(wb)


def write_trends_sheet(wb: Workbook, full_df: pd.DataFrame, current_month: str) -> None:
    ws = wb.create_sheet("Trends")
    debit_df = full_df[full_df["dr_cr"] == "DR"]

    if debit_df.empty:
        ws.append(["Month"])
        _remove_default_sheet(wb)
        return

    matrix = debit_df.pivot_table(
        index="source_month", columns="category", values="amount",
        aggfunc="sum", fill_value=0.0,
    ).sort_index()

    ws.append(["Month"] + [str(c) for c in matrix.columns])
    for month, row in matrix.iterrows():
        ws.append([str(month)] + [float(v) for v in row.values])
    matrix_last_row = ws.max_row
    matrix_last_col = matrix.shape[1] + 1

    monthly_totals = debit_df.groupby("source_month")["amount"].sum().sort_index()
    totals_col = matrix_last_col + 2
    ws.cell(row=1, column=totals_col, value="Month")
    ws.cell(row=1, column=totals_col + 1, value="Total")
    for i, (month, total) in enumerate(monthly_totals.items(), start=2):
        ws.cell(row=i, column=totals_col, value=str(month))
        ws.cell(row=i, column=totals_col + 1, value=float(total))
    totals_last_row = 1 + len(monthly_totals)

    line = LineChart()
    line.title = "Total Spend by Month"
    line.add_data(Reference(ws, min_col=totals_col + 1, min_row=1, max_row=totals_last_row), titles_from_data=True)
    line.set_categories(Reference(ws, min_col=totals_col, min_row=2, max_row=totals_last_row))
    ws.add_chart(line, f"A{matrix_last_row + 3}")

    stacked = BarChart()
    stacked.type = "col"
    stacked.grouping = "stacked"
    stacked.overlap = 100
    stacked.title = "Monthly Spend by Category"
    stacked.add_data(
        Reference(ws, min_col=2, max_col=matrix_last_col, min_row=1, max_row=matrix_last_row),
        titles_from_data=True,
    )
    stacked.set_categories(Reference(ws, min_col=1, min_row=2, max_row=matrix_last_row))
    ws.add_chart(stacked, f"A{matrix_last_row + 20}")

    daily = (
        debit_df[debit_df["source_month"] == current_month]
        .groupby("value_date")["amount"].sum().sort_index()
    )
    daily_col = totals_col + 3
    ws.cell(row=1, column=daily_col, value=f"Daily Spend ({current_month})")
    ws.cell(row=2, column=daily_col, value="Date")
    ws.cell(row=2, column=daily_col + 1, value="Amount")
    for i, (day, amount) in enumerate(daily.items(), start=3):
        ws.cell(row=i, column=daily_col, value=str(day))
        ws.cell(row=i, column=daily_col + 1, value=float(amount))
    daily_last_row = 2 + len(daily)

    if len(daily):
        daily_bar = BarChart()
        daily_bar.type = "col"
        daily_bar.title = f"Daily Spend ({current_month})"
        daily_bar.add_data(
            Reference(ws, min_col=daily_col + 1, min_row=2, max_row=daily_last_row),
            titles_from_data=True,
        )
        daily_bar.set_categories(Reference(ws, min_col=daily_col, min_row=3, max_row=daily_last_row))
        ws.add_chart(daily_bar, f"A{matrix_last_row + 37}")

    _remove_default_sheet(wb)
```

- [ ] **Step 2: Verify the three sheets render from real parsed data**

```bash
python -c "
import glob, tempfile
from pathlib import Path
from openpyxl import Workbook, load_workbook
from src.parser import parse_statement
from src.transactions_store import append_transactions, read_all_transactions
from src.dashboard import write_transactions_sheet, write_category_summary_sheet, write_trends_sheet
p = Path(tempfile.mkdtemp())
append_transactions(p/'t.csv', parse_statement(glob.glob('example doc/*.csv')[0]))
df = read_all_transactions(p/'t.csv')
month = df['source_month'].mode()[0]
wb = Workbook()
write_transactions_sheet(wb, df[df['source_month']==month])
write_category_summary_sheet(wb, df[df['source_month']==month])
write_trends_sheet(wb, df, month)
wb.save(p/'out.xlsx')
print(load_workbook(p/'out.xlsx').sheetnames)
"
```

Expected: `['Transactions', 'Category Summary', 'Trends']` and no exception. Open the file in Excel to confirm it does not prompt to repair.

- [ ] **Step 3: Commit**

```bash
git add src/dashboard.py
git commit -m "Add Excel dashboard: Transactions, Category Summary and Trends sheets"
```

---

### Task 15: Excel dashboard — ledger, insights, landing sheet and orchestrator

**Files:** Modify `src/dashboard.py` (append; the import block from Task 14 already covers everything used here)

**Produces:** `write_ledger_sheet(wb, ledger_entries)`, `write_insights_sheet(wb, full_df, current_month)`, `write_dashboard_sheet(wb, month_df, full_df, ledger_entries, current_month)`, `generate_dashboard(output_path, month_df, full_df, ledger_entries, current_month)`

- [ ] **Step 1: Append to `src/dashboard.py`**

```python
def write_ledger_sheet(wb: Workbook, ledger_entries: list[dict]) -> None:
    ws = wb.create_sheet("Lend & Borrow Ledger")
    outstanding = get_outstanding_by_person(ledger_entries)
    owed_to_you = sum(v["lent"] for v in outstanding.values())
    owed_by_you = sum(v["borrowed"] for v in outstanding.values())

    ws.append([f"Owed to you: {owed_to_you:.2f}", f"Owed by you: {owed_by_you:.2f}"])
    ws.append(["Person", "Type", "Direction", "Amount", "Date", "Status", "Linked Transaction"])

    for entry in ledger_entries:
        if entry["type"] == "direct":
            ws.append([
                entry["person"], "Direct", entry["direction"], float(entry["amount"]),
                entry["entry_date"], entry["status"], entry["linked_transaction_hash"],
            ])
        else:
            # One row per person in the split, so the sheet reads person-by-person
            # exactly like the direct loans above it.
            for share in entry["splits"]:
                ws.append([
                    share["person"], "Split", "lent", float(share["amount"]),
                    entry["entry_date"], share["status"], entry["linked_transaction_hash"],
                ])

    if not outstanding:
        _remove_default_sheet(wb)
        return

    block_top = ws.max_row + 2
    block_end = _write_block(
        ws, block_top, ("Person", "Net Owed To You"),
        ((person, amounts["lent"] - amounts["borrowed"]) for person, amounts in outstanding.items()),
    )

    bar = BarChart()
    bar.type = "bar"
    bar.title = "Outstanding by Person"
    bar.add_data(Reference(ws, min_col=2, min_row=block_top, max_row=block_end), titles_from_data=True)
    bar.set_categories(Reference(ws, min_col=1, min_row=block_top + 1, max_row=block_end))
    ws.add_chart(bar, f"I{block_top}")

    _remove_default_sheet(wb)


def write_insights_sheet(wb: Workbook, full_df: pd.DataFrame, current_month: str) -> None:
    ws = wb.create_sheet("Insights")

    ws.append(["Top Vendors This Month"])
    ws.append(["Vendor", "Amount"])
    top_vendors = compute_top_vendors(full_df, current_month)
    for _, row in top_vendors.iterrows():
        ws.append([row["vendor"], float(row["amount"])])
    top_vendors_end = ws.max_row

    ws.append([])
    ws.append(["Recurring Charges"])
    ws.append(["Vendor", "Avg Amount", "Occurrences"])
    for _, row in detect_recurring_charges(full_df).iterrows():
        ws.append([row["vendor"], round(float(row["avg_amount"]), 2), int(row["occurrences"])])

    ws.append([])
    ws.append(["Month-over-Month Category Deltas"])
    ws.append(["Category", "Month", "Amount", "% Change"])
    deltas_start = ws.max_row + 1
    for _, row in compute_month_over_month_deltas(full_df).iterrows():
        pct = row["pct_change"]
        ws.append([
            row["category"], row["source_month"], float(row["amount"]),
            round(float(pct), 2) if pd.notna(pct) else None,
        ])
    deltas_end = ws.max_row

    if deltas_end >= deltas_start:
        # Spending more than last month is the thing to notice, so up is red.
        cell_range = f"D{deltas_start}:D{deltas_end}"
        ws.conditional_formatting.add(
            cell_range,
            CellIsRule(operator="greaterThan", formula=["0"], font=Font(color="FF0000")),
        )
        ws.conditional_formatting.add(
            cell_range,
            CellIsRule(operator="lessThan", formula=["0"], font=Font(color="008000")),
        )

    if not top_vendors.empty:
        bar = BarChart()
        bar.title = "Top Vendors"
        bar.add_data(Reference(ws, min_col=2, min_row=2, max_row=top_vendors_end), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=1, min_row=3, max_row=top_vendors_end))
        ws.add_chart(bar, "G2")

    _remove_default_sheet(wb)


def write_dashboard_sheet(
    wb: Workbook,
    month_df: pd.DataFrame,
    full_df: pd.DataFrame,
    ledger_entries: list[dict],
    current_month: str,
) -> None:
    """The landing sheet: charts only, each backed by its own small value block."""
    ws = wb.create_sheet("Dashboard", 0)
    debit_df = month_df[month_df["dr_cr"] == "DR"]

    category_totals = (
        debit_df.groupby("category", as_index=False)["amount"]
        .sum()
        .sort_values("amount", ascending=False)
    )
    cat_end = _write_block(
        ws, 1, ("Category", "Total"),
        zip(category_totals["category"], category_totals["amount"]),
    )
    if not category_totals.empty:
        pie = PieChart()
        pie.title = "This Month's Spend by Category"
        pie.add_data(Reference(ws, min_col=2, min_row=1, max_row=cat_end), titles_from_data=True)
        pie.set_categories(Reference(ws, min_col=1, min_row=2, max_row=cat_end))
        ws.add_chart(pie, "D1")

    monthly_totals = (
        full_df[full_df["dr_cr"] == "DR"].groupby("source_month")["amount"].sum().sort_index()
    )
    monthly_top = cat_end + 2
    monthly_end = _write_block(ws, monthly_top, ("Month", "Total"), monthly_totals.items())
    if len(monthly_totals):
        line = LineChart()
        line.title = "Monthly Spend Trend"
        line.add_data(Reference(ws, min_col=2, min_row=monthly_top, max_row=monthly_end), titles_from_data=True)
        line.set_categories(Reference(ws, min_col=1, min_row=monthly_top + 1, max_row=monthly_end))
        ws.add_chart(line, "D20")

    top_vendors = compute_top_vendors(full_df, current_month, n=5)
    top_top = monthly_end + 2
    top_end = _write_block(
        ws, top_top, ("Vendor", "Amount"),
        zip(top_vendors["vendor"], top_vendors["amount"]),
    )
    if not top_vendors.empty:
        bar = BarChart()
        bar.title = "Top 5 Vendors This Month"
        bar.add_data(Reference(ws, min_col=2, min_row=top_top, max_row=top_end), titles_from_data=True)
        bar.set_categories(Reference(ws, min_col=1, min_row=top_top + 1, max_row=top_end))
        ws.add_chart(bar, "D39")

    outstanding = get_outstanding_by_person(ledger_entries)
    summary_row = top_end + 2
    ws.cell(row=summary_row, column=1, value="Owed to you")
    ws.cell(row=summary_row, column=2, value=float(sum(v["lent"] for v in outstanding.values())))
    ws.cell(row=summary_row + 1, column=1, value="Owed by you")
    ws.cell(row=summary_row + 1, column=2, value=float(sum(v["borrowed"] for v in outstanding.values())))


def generate_dashboard(
    output_path: Path,
    month_df: pd.DataFrame,
    full_df: pd.DataFrame,
    ledger_entries: list[dict],
    current_month: str,
) -> None:
    """Build the six-sheet workbook.

    Dashboard is written last but inserted at index 0, so the reader lands on
    the charts while every other sheet was already there to be referenced.
    """
    wb = Workbook()
    write_transactions_sheet(wb, month_df)
    write_category_summary_sheet(wb, month_df)
    write_trends_sheet(wb, full_df, current_month)
    write_ledger_sheet(wb, ledger_entries)
    write_insights_sheet(wb, full_df, current_month)
    write_dashboard_sheet(wb, month_df, full_df, ledger_entries, current_month)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
```

- [ ] **Step 2: Verify the full six-sheet workbook**

```bash
python -c "
import glob, tempfile
from pathlib import Path
from datetime import date
from openpyxl import load_workbook
from src.parser import parse_statement
from src.transactions_store import append_transactions, read_all_transactions
from src.ledger import add_direct_entry, load_ledger
from src.models import DirectLedgerEntry
from src.dashboard import generate_dashboard
p = Path(tempfile.mkdtemp())
append_transactions(p/'t.csv', parse_statement(glob.glob('example doc/*.csv')[0]))
add_direct_entry(p/'l.json', DirectLedgerEntry(person='Rahul', direction='lent', amount=500.0, entry_date=date(2026,7,12), linked_transaction_hash='h1'))
df = read_all_transactions(p/'t.csv')
month = df['source_month'].mode()[0]
generate_dashboard(p/'dash.xlsx', df[df['source_month']==month], df, load_ledger(p/'l.json'), month)
print(load_workbook(p/'dash.xlsx').sheetnames)
print(p/'dash.xlsx')
"
```

Expected exactly: `['Dashboard', 'Transactions', 'Category Summary', 'Trends', 'Lend & Borrow Ledger', 'Insights']`. Open the printed path in Excel — it must open clean, with no repair prompt, and the charts must render.

- [ ] **Step 3: Commit**

```bash
git add src/dashboard.py
git commit -m "Add ledger, insights and landing sheets plus the workbook orchestrator"
```

---

### Task 16: Pipeline graph

**Files:** Create `src/graph.py`

**Produces:** `PipelineState`, the `DATA_DIR`/`OUTPUT_DIR`/`TRANSACTIONS_CSV`/`LEDGER_JSON`/`VENDOR_CACHE_JSON` path constants, and `build_graph(model, categories, confidence_threshold, prompt_fn=click.prompt, echo_fn=click.echo)` returning a compiled graph.

The path constants live here — this is the module that owns where runtime state goes, and `track_expenses.py` imports them rather than defining its own copies.

- [ ] **Step 1: Write `src/graph.py`**

```python
from collections import Counter
from pathlib import Path
from typing import TypedDict

import click
from langgraph.graph import END, StateGraph

from src.categorizer import categorize_transactions
from src.dashboard import generate_dashboard
from src.ledger import add_direct_entry, load_ledger
from src.parser import parse_statement
from src.review import review_transactions
from src.transactions_store import (
    append_transactions,
    load_existing_hashes,
    read_all_transactions,
)
from src.vendor_cache import load_vendor_cache, save_vendor_cache

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
LEDGER_JSON = DATA_DIR / "ledger.json"
VENDOR_CACHE_JSON = DATA_DIR / "vendor_cache.json"


class PipelineState(TypedDict, total=False):
    statement_path: str
    new_transactions: list
    ledger_entries: list
    current_month: str
    output_path: str


def _route_after_parse(state: PipelineState) -> str:
    """Re-running the same statement is a no-op, not an error — stop before the
    model gets involved."""
    return "categorize" if state["new_transactions"] else "end"


def build_graph(
    model,
    categories: dict,
    confidence_threshold: float,
    prompt_fn=click.prompt,
    echo_fn=click.echo,
):
    def parse_node(state: PipelineState) -> dict:
        transactions = parse_statement(Path(state["statement_path"]))
        known = load_existing_hashes(TRANSACTIONS_CSV)
        new_transactions = [t for t in transactions if t.dedup_hash not in known]

        already_imported = len(transactions) - len(new_transactions)
        if already_imported:
            echo_fn(f"Skipping {already_imported} transaction(s) already imported.")
        return {"new_transactions": new_transactions}

    def categorize_node(state: PipelineState) -> dict:
        echo_fn(f"Categorizing {len(state['new_transactions'])} transaction(s)...")
        cache = load_vendor_cache(VENDOR_CACHE_JSON)
        categorize_transactions(state["new_transactions"], cache, model, categories)
        save_vendor_cache(VENDOR_CACHE_JSON, cache)
        return {}

    def review_node(state: PipelineState) -> dict:
        entries = review_transactions(
            state["new_transactions"], categories, confidence_threshold,
            prompt_fn=prompt_fn, echo_fn=echo_fn,
        )
        return {"ledger_entries": entries}

    def persist_node(state: PipelineState) -> dict:
        for entry in state["ledger_entries"]:
            add_direct_entry(LEDGER_JSON, entry)
        append_transactions(TRANSACTIONS_CSV, state["new_transactions"])

        # A statement can straddle a month boundary (an interest credit posted
        # on the 1st with a value date of the 31st). The month this run is
        # *about* is the one most of its rows belong to.
        months = [t.source_month for t in state["new_transactions"]]
        return {"current_month": Counter(months).most_common(1)[0][0]}

    def dashboard_node(state: PipelineState) -> dict:
        full_df = read_all_transactions(TRANSACTIONS_CSV)
        month_df = full_df[full_df["source_month"] == state["current_month"]]
        output_path = OUTPUT_DIR / f"expense_dashboard_{state['current_month']}.xlsx"
        generate_dashboard(
            output_path, month_df, full_df, load_ledger(LEDGER_JSON), state["current_month"]
        )
        return {"output_path": str(output_path)}

    graph = StateGraph(PipelineState)
    graph.add_node("parse", parse_node)
    graph.add_node("categorize", categorize_node)
    graph.add_node("review", review_node)
    graph.add_node("persist", persist_node)
    graph.add_node("dashboard", dashboard_node)

    graph.set_entry_point("parse")
    graph.add_conditional_edges("parse", _route_after_parse, {"categorize": "categorize", "end": END})
    graph.add_edge("categorize", "review")
    graph.add_edge("review", "persist")
    graph.add_edge("persist", "dashboard")
    graph.add_edge("dashboard", END)

    return graph.compile()
```

- [ ] **Step 2: Verify the graph runs end to end without a real model**

Run this from the repo root. It swaps in a fake model and scripted answers, then works inside a temp directory so the real `data/` is untouched.

```bash
python -c "
import glob, os, tempfile
from pathlib import Path

# Import everything and read the config BEFORE changing directory — the graph
# writes data/ and output/ relative to the working directory.
from src.config_loader import load_categories
from src.graph import build_graph
from src.models import CategorizationResult

categories = load_categories(Path('categories.yaml'))
statement = os.path.abspath(glob.glob('example doc/*.csv')[0])
os.chdir(tempfile.mkdtemp())

class FakeModel:
    def invoke(self, prompt):
        return CategorizationResult(vendor='Test Vendor', category='Transport',
                                    subcategory='Public Transit', confidence=0.9)

answers = iter(['e'] * 50)
graph = build_graph(FakeModel(), categories, 0.6,
                    prompt_fn=lambda *a, **k: next(answers), echo_fn=print)

first = graph.invoke({'statement_path': statement})
print('new:', len(first['new_transactions']), '| month:', first['current_month'], '| out:', first['output_path'])

second = graph.invoke({'statement_path': statement})
print('rerun new:', len(second['new_transactions']), '| wrote a workbook:', 'output_path' in second)
"
```

Expected: the first run reports every transaction plus a written workbook path; the rerun reports `rerun new: 0` and `wrote a workbook: False`, proving the dedup short-circuit works.

- [ ] **Step 3: Commit**

```bash
git add src/graph.py
git commit -m "Add LangGraph StateGraph orchestrating the monthly pipeline"
```

---

### Task 17: CLI — `process`

**Files:** Create `track_expenses.py`

**Produces:** the `cli` click group and the `process` command. The only place a real `ChatOllama` is constructed.

- [ ] **Step 1: Write `track_expenses.py`**

```python
from pathlib import Path

import click
from langchain_ollama import ChatOllama

from src.config_loader import load_categories, load_config
from src.graph import build_graph
from src.models import CategorizationResult
from src.ollama_client import OllamaCategorizationError, check_ollama_available
from src.parser import ParserError

CONFIG_PATH = Path("config.yaml")
CATEGORIES_PATH = Path("categories.yaml")


def _build_categorization_model(host: str, model_name: str):
    # temperature=0: the same transaction description should categorize the same
    # way every run, otherwise the vendor cache records a coin flip.
    return ChatOllama(
        base_url=host, model=model_name, temperature=0,
    ).with_structured_output(CategorizationResult)


@click.group()
def cli():
    """Turn a monthly bank statement into a categorized Excel dashboard."""


@cli.command()
@click.argument("statement_path", type=click.Path(exists=True, dir_okay=False))
def process(statement_path):
    """Parse, categorize and review STATEMENT_PATH, then write the dashboard."""
    config = load_config(CONFIG_PATH)
    categories = load_categories(CATEGORIES_PATH)
    host = config["ollama"]["host"]
    model_name = config["ollama"]["model"]

    # Checked up front: a dead Ollama discovered mid-run would quietly mark every
    # transaction "Other/Uncategorized" and still finish.
    try:
        check_ollama_available(host, model_name)
    except OllamaCategorizationError as exc:
        raise click.ClickException(str(exc))

    graph = build_graph(
        _build_categorization_model(host, model_name),
        categories,
        config["review"]["confidence_threshold"],
    )

    try:
        result = graph.invoke({"statement_path": statement_path})
    except ParserError as exc:
        raise click.ClickException(str(exc))

    if not result.get("new_transactions"):
        click.echo("No new transactions — everything in this statement was already imported.")
        return

    click.echo(f"\nProcessed {len(result['new_transactions'])} new transaction(s).")
    click.echo(f"Dashboard written to {result['output_path']}")


if __name__ == "__main__":
    cli()
```

- [ ] **Step 2: Verify against the real statement**

```bash
python track_expenses.py --help
python track_expenses.py process --help
```

Then, with Ollama running:

```bash
python track_expenses.py process "example doc/<your-statement>.csv"
```

Expected: categorization runs, the review prompts appear for anything ambiguous, and `output/expense_dashboard_<YYYY-MM>.xlsx` is written. Run it a second time and confirm it reports no new transactions and does not duplicate rows in `data/transactions.csv`.

Also confirm the failure path is friendly — stop Ollama (or point `config.yaml` at a bad port) and re-run: it should print the "Cannot reach Ollama… ollama serve" message with no traceback.

- [ ] **Step 3: Commit**

```bash
git add track_expenses.py
git commit -m "Add 'process' command wiring config, Ollama and the pipeline graph"
```

---

### Task 18: CLI — `split` and `settle`

**Files:** Modify `track_expenses.py`

**Produces:** the `split` and `settle` commands. Neither touches the LLM.

- [ ] **Step 1: Extend the imports at the top of `track_expenses.py`**

Replace the existing import block with:

```python
from pathlib import Path

import click
import pandas as pd
from langchain_ollama import ChatOllama

from src.config_loader import load_categories, load_config
from src.graph import LEDGER_JSON, TRANSACTIONS_CSV, build_graph
from src.ledger import add_split_entry, settle_person
from src.models import CategorizationResult
from src.ollama_client import OllamaCategorizationError, check_ollama_available
from src.parser import ParserError
from src.split_tracker import (
    build_custom_split,
    build_equal_split,
    create_split_entry,
    search_transactions,
)
from src.transactions_store import read_all_transactions
```

- [ ] **Step 2: Append the two commands, above the `if __name__ == "__main__":` block**

```python
@cli.command()
def split():
    """Record who owes you part of an already-imported transaction."""
    full_df = read_all_transactions(TRANSACTIONS_CSV)
    if full_df.empty:
        raise click.ClickException("No transactions imported yet — run 'process' on a statement first.")

    matches = search_transactions(full_df, click.prompt("Search transactions (date / vendor / amount)"))
    if matches.empty:
        click.echo("No matching transactions found.")
        return

    matches = matches.reset_index(drop=True)
    for i, row in matches.iterrows():
        click.echo(f"  {i + 1}. {row['value_date']}  {row['description']}  {float(row['amount']):.2f}")

    selected = matches.iloc[click.prompt("Select transaction", type=click.IntRange(1, len(matches))) - 1]
    total = float(selected["amount"])

    if click.prompt("Split type", type=click.Choice(["equal", "custom"])) == "equal":
        names = [n.strip() for n in click.prompt("Other people (comma-separated)").split(",") if n.strip()]
        if not names:
            raise click.ClickException("A split needs at least one other person.")
        shares = build_equal_split(total, names)
    else:
        raw = click.prompt("Amounts as name:amount, comma-separated (e.g. 'Rahul:300, Priya:700')")
        amounts = {}
        for pair in raw.split(","):
            if ":" not in pair:
                raise click.ClickException(f"Could not read {pair.strip()!r} — expected name:amount.")
            name, amount = pair.split(":", 1)
            try:
                amounts[name.strip()] = float(amount.strip())
            except ValueError:
                raise click.ClickException(f"{amount.strip()!r} is not a number.")
        shares = build_custom_split(amounts)

    try:
        entry = create_split_entry(
            selected["dedup_hash"], total,
            pd.to_datetime(selected["value_date"]).date(), shares,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc))

    add_split_entry(LEDGER_JSON, entry)
    click.echo(f"\nRecorded split on '{selected['description']}' ({total:.2f}):")
    for share in shares:
        click.echo(f"  {share.person} owes {share.amount:.2f}")
    click.echo(f"  your share: {total - sum(s.amount for s in shares):.2f}")


@cli.command()
@click.argument("person")
def settle(person):
    """Mark PERSON's outstanding lend/borrow/split amounts as settled."""
    settled = settle_person(LEDGER_JSON, person)
    if not settled:
        click.echo(f"No unsettled entries found for {person}.")
        return
    click.echo(f"Settled {settled} entr{'y' if settled == 1 else 'ies'} for {person}.")
```

- [ ] **Step 3: Verify both commands against real imported data**

```bash
python track_expenses.py split
```

Search for a vendor you recognize, pick it, split it equally with one or two names, and confirm the printed shares and your own remainder are right. Then try a deliberately invalid custom split (shares totalling more than the transaction) and confirm it is rejected with a readable message rather than a traceback.

```bash
python track_expenses.py settle "<that person>"
python -c "import json; print(json.load(open('data/ledger.json')))"
```

Expected: the settle command reports what it settled, and the ledger shows those shares flipped to `settled`.

- [ ] **Step 4: Commit**

```bash
git add track_expenses.py
git commit -m "Add 'split' and 'settle' commands for the lend/borrow ledger"
```

---

## Review notes

**Defects found in the previous draft of this plan and fixed here:**

1. **`write_transactions_sheet` read a column that does not exist.** It indexed `row["date"]`, but `transactions_store.CSV_COLUMNS` defines `txn_datetime` and `value_date` — there is no `date`. Every real dashboard run would have died with a `KeyError`. The bug survived review because the task's test built its own DataFrame with an invented `date` column instead of one from `read_all_transactions`. Now fixed, and the schema is pinned in Global Constraints.
2. **A dead Ollama would have produced a silently useless run.** The old retry logic caught `ConnectionError` to distinguish "server down" from "bad output", but `langchain-ollama` raises `httpx.ConnectError`, which is not a builtin `ConnectionError`. A stopped server would have fallen through to the generic handler and marked every transaction "Other/Uncategorized" while reporting success. Replaced with a `check_ollama_available` pre-flight in the `process` command that names the exact fix command, per the spec's error-handling requirement.
3. **The vendor cache would have mis-categorized repeat payments to the same person.** Keying purely on vendor meant the first note ever seen for "RAHUL SHARMA" (say `lunch`) would decide the category for every future payment to him, including rent. Person-payments now key on vendor + note; merchants still key on vendor alone.
4. **Two different definitions of "needs review" disagreed.** `review.py` gated on `category == Other/Uncategorized or confidence < threshold`, so a confidently-categorized payment to an individual with no note could never reach the P2P question, even though `is_p2p_candidate` would have returned `True` for it. The two conditions are now evaluated independently and either one stops on the transaction.
5. **Empty tables and charts would have corrupted the workbook.** On a first run or a sparse month, several sheets would emit an Excel Table whose range covers only the header row, and charts pointing at empty ranges — both make Excel declare the file damaged. Every table and chart is now written only when it has data behind it.
6. **A shared `TableStyleInfo` instance was reused across tables.** openpyxl serializes the style per table; sharing one object across sheets produces a malformed workbook. Now constructed per table.
7. **`current_month` was taken from the first transaction.** The real sample contains a row posted on 01-04 with a value date of 31-03, so a statement can straddle a month boundary and the first row is an arbitrary pick. Now uses the most common `source_month` across the batch.
8. **`p2p_detector` was ordered after its consumer.** `categorizer.py` imports it, but it was scheduled as the later task. Reordered.
9. **Blank CSV fields came back as `NaN`.** `read_all_transactions` did not coerce string columns, so an empty `flag` or `subcategory` would render as the literal `nan` in the workbook. Now explicitly filled.
10. **`search_transactions` treated the search term as a regex.** Searching a vendor containing `(`, `+` or `*` would raise or silently mismatch. Now `regex=False`.
11. **Smaller fixes:** `split` had no bounds check on the transaction selection (`IndexError` on bad input) and asked for a headcount it then ignored in favour of the name list; `%` change divided by zero into `inf`; `CategorizationResult` required fields a 3B model may omit; unparseable `DR`/`CR` values were accepted; mid-file imports in `dashboard.py` are now a single top block.

**Spec coverage:** LLM runtime → Tasks 1, 8, 17 · persistent flat files → Tasks 5, 6, 11 · Statement Parser → Task 4 · Categorization Agent + vendor cache → Tasks 6, 8, 9 · P2P detector → Task 7 · Interactive Review → Task 10 · Ledger + settle → Tasks 11, 18 · Split Tracker → Tasks 12, 18 · Insights Engine → Task 13 · six-sheet Excel dashboard → Tasks 14, 15 · pipeline orchestration → Task 16 · `process` command and safe re-runs → Tasks 16, 17.
