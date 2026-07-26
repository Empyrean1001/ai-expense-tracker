# Expense Tracker — Design Spec

Date: 2026-07-26

## Purpose

A monthly workflow that turns a raw bank statement export into a categorized,
insight-rich Excel dashboard. The user manually uploads one bank statement
Excel file per month (once the month's statement is fully available). The
tool categorizes transactions, extracts vendors, tracks money lent/borrowed
between the user and other people, and surfaces spending patterns — all with
the user reviewing and confirming anything ambiguous rather than the LLM
guessing silently.

This is v1 of a tool intended to be extended over time; it is not limited to
Excel output long-term, but Excel is the only output format for this phase.

## Architecture

Standalone Python CLI application, run once a month, using flat files (no
database) as persistent storage:

```
bank_statement.xlsx → Parser → Categorization Agent (Claude API + vendor cache)
                              → P2P Transfer Detector (heuristic)
                              → Interactive terminal review (user confirms/flags)
                              → Append to data/transactions.csv (master log)
                              → Update data/ledger.json (lend/borrow)
                              → Insights Engine (reads transactions.csv history)
                              → Excel Dashboard Generator → output/expense_dashboard_<month>.xlsx
```

### Persistent data files

- **`data/transactions.csv`** — append-only master log of every categorized
  transaction ever processed. Columns: date, description, vendor, category,
  subcategory, amount, dr/cr, source statement/month, dedup hash.
- **`data/ledger.json`** — lend/borrow entries: person, direction
  (lent/borrowed), amount, date, status (settled/unsettled), linked
  transaction reference.
- **`data/vendor_cache.json`** — vendor → category/subcategory mapping
  learned over time, so previously-seen vendors are not re-sent to the LLM.
  Keeps categorization consistent across months and reduces API calls.
- **`categories.yaml`** — editable category/subcategory taxonomy used by the
  categorization agent (see Categories section below).

The Excel dashboard is a generated *view* of `transactions.csv` and
`ledger.json` — it is not the source of truth and can be regenerated at any
time.

## Categories (starter taxonomy, editable in `categories.yaml`)

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
Entertainment:
  - Streaming
  - Movies/Events
Health:
  - Pharmacy
  - Doctor/Hospital
Income:
  - Salary
  - Refunds
Lend/Borrow:
  - (tracked separately in ledger.json, not a spending category)
Other/Uncategorized:
  -
```

## Input format

Bank statement Excel columns (as provided by the user's bank export):
S.No, Transaction Date, Value Date, Description, Chq/Ref Number, Amount,
Dr/Cr, Balance, Dr/Cr. The parser is built against this exact layout; the
user will supply a sample file to validate against before implementation
begins in earnest. If other banks/formats are needed later, the parser can
be extended with a column-mapping config — out of scope for v1.

## Components

1. **Statement Parser** — reads the bank Excel, validates expected columns
   are present, normalizes rows into clean transaction records (date,
   description, amount, debit/credit direction). Computes a dedup hash per
   row (date + ref number + amount) to detect already-imported transactions
   on re-runs.

2. **Categorization Agent** — for each transaction whose vendor is not
   already in `vendor_cache.json`, batches calls to the Claude API with
   `categories.yaml` as the taxonomy. Returns category, subcategory, and a
   normalized vendor name (e.g. "SWIGGY*ORDER38292" → "Swiggy"). Updates the
   vendor cache with new mappings.

3. **P2P Transfer Detector** — a non-LLM heuristic that flags transactions
   matching person-to-person transfer patterns (e.g. UPI/NEFT to what looks
   like an individual's name rather than a known merchant) as **candidates**
   for lend/borrow. It never assigns lend/borrow itself — that decision is
   always the user's.

4. **Interactive CLI Review** — after categorization and P2P detection, the
   tool prints each flagged candidate and any low-confidence categorization
   for the user to resolve via terminal prompts:
   - P2P candidates: user picks `[l]ent / [b]orrowed / [e]xpense / [s]kip`
     and supplies the person's name if lent/borrowed.
   - Low-confidence categorizations: user picks/overrides the
     category/subcategory.

5. **Ledger Manager** — writes confirmed lend/borrow entries to
   `ledger.json`. Also exposes a `settle` CLI command (e.g.
   `python track_expenses.py settle "Rahul Sharma"`) to mark a person's
   outstanding balance as settled.

6. **Insights Engine** — reads the full `transactions.csv` history to
   compute: month-over-month category spending deltas, top vendors by
   spend, recurring-charge detection (same vendor + similar amount
   repeating across months), and notable spending spikes.

7. **Excel Dashboard Generator** — builds `output/expense_dashboard_<month>.xlsx`
   with:
   - Raw transactions sheet (that month's transactions)
   - Category summary sheet (pivot-style breakdown + pie chart)
   - Month-over-month trend sheet (line/bar chart using transaction history)
   - Lend/Borrow ledger sheet (current outstanding + settled history)
   - Insights sheet (recurring charges, top vendors, notable changes)

## Data flow (one monthly run)

1. User runs `python track_expenses.py process statements/july_2026.xlsx`
2. Parser validates columns, extracts transactions, computes dedup hashes,
   and skips rows already present in `transactions.csv` (safe to re-run on
   the same file).
3. Categorization Agent checks `vendor_cache.json` first; only new/unknown
   vendors are sent to the Claude API.
4. P2P Transfer Detector flags likely person-to-person transfers.
5. Interactive CLI Review presents flagged candidates and low-confidence
   categorizations to the user for confirmation/override.
6. Confirmed transactions are appended to `transactions.csv`; lend/borrow
   ones are also written to `ledger.json`.
7. Insights Engine reads full transaction history to compute trends.
8. Excel Dashboard Generator writes the output workbook.

## Error handling

- Missing/unexpected columns in the input Excel → clear error listing
  expected vs. found columns; abort before any writes.
- Claude API failure/rate-limit → retry with backoff; on persistent
  failure, save progress made so far (already-categorized transactions) so
  the user can resume rather than losing the whole run.
- Re-running the same statement file → duplicate rows detected via hash and
  skipped, not double-counted.
- Ambiguous/unparseable amount or date in a row → flagged and shown to the
  user rather than silently guessed or dropped.

## Testing

- Parser tested against a sample statement fixture (to be supplied by the
  user before implementation).
- P2P heuristic tested against known transfer-pattern examples (positive
  and negative cases).
- Categorization Agent tested with mocked Claude API responses — no live
  API calls in the test suite.
- End-to-end test: sample statement → full pipeline → assert the output
  workbook has the expected sheets and the ledger is updated correctly.

## Explicitly out of scope for v1

- Automatic bank API integration / receipt scanning (manual monthly Excel
  upload only, per user's current preference).
- Multi-bank column-mapping configuration (single hardcoded column layout
  for now).
- Non-Excel output formats (dashboards, notifications, etc.) — noted as
  possible future extensions but not built now.
- LLM-driven lend/borrow classification — this is always a user decision.
