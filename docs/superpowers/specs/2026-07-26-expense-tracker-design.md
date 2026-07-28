# Expense Tracker — Design Spec

## Purpose

A monthly workflow that turns a raw bank statement export into a categorized, insight-rich Excel dashboard. The user manually uploads one bank statement export per month (once the month's statement is fully available). 
The tool categorizes transactions, extracts vendors, tracks money lent/borrowed between the user and other people (including partial amounts from split/group expenses), and surfaces spending patterns — all with the user reviewing and confirming anything ambiguous rather than the LLM guessing silently.

Splits cannot be detected from a bank statement at all — the description of a merchant payment gives no signal that a bill was shared with others. So split tracking is a separate, on-demand, LLM-free mechanism (see **Split Tracker** below), not something the categorization pipeline attempts to infer.

This is v1 of a tool intended to be extended over time; it is not limited to Excel output long-term, but Excel is the only output format for this phase.

## Architecture

Standalone Python CLI application, run once a month, using flat files as persistent storage. Categorization runs against a **locally
hosted LLM via Ollama** — all bank data stays on the
user's machine.

Currently it is not designed to be able to detect the table of transactions, but built around an already existing csv format of the statement. However this can be extended to support other formats in the future. The table of transactions is detected by looking for the row that contains the string "Sl. No." which is a column that is present in the statement.

```
bank_statement.csv → Parser → Categorization Agent (local Ollama model + vendor cache)
                             → P2P Transfer Detector (heuristic, refined by category confidence)
                             → Interactive terminal review (user confirms/flags)
                             → Append to data/transactions.csv (master log)
                             → Update data/ledger.json (direct lend/borrow)
                             → Insights Engine (reads transactions.csv history)
                             → Excel Dashboard Generator → output/expense_dashboard_<month>.xlsx
```

Separately, run anytime (not part of the monthly pipeline above, and never
touches the LLM):

```
python track_expenses.py split → search data/transactions.csv → pick a transaction
                                → user enters split (equal or custom amounts)
                                → Update data/ledger.json (split entries)
```

### LLM runtime

- Runs via [Ollama](https://ollama.com) on local host.
- Default model: **`qwen2.5:3b-instruct`** — lightweight, reliable structured
  (JSON) output, good fit for local categorization on modest hardware.
  Model name is a config value (`config.yaml`), so it can be swapped for
  `gemma2:9b` or any other pulled model without code changes.
- No API key, no internet dependency, no per-call cost. The tool assumes
  Ollama is already installed and the configured model has been pulled
  (`ollama pull qwen2.5:3b-instruct`) — it does not manage the Ollama
  installation itself.
- Requests use Ollama's JSON-mode/structured output so responses parse
  reliably into category/subcategory/vendor/confidence fields. Also utilized Pydantic to verify the final output structure.

### Persistent data files

- **`data/transactions.csv`** — append-only master log of every categorized
  transaction ever processed. Columns: date, description, vendor, category,
  subcategory, amount, dr/cr, source statement/month, dedup hash.
- **`data/ledger.json`** — lend/borrow entries, of two kinds:
  - **Direct loan** — the whole transaction amount is a loan (e.g. sending
    someone money directly via UPI): `{type: "direct", person, direction
    (lent/borrowed), amount, date, status (settled/unsettled), linked
    transaction hash}`.
  - **Split** — you paid a merchant/vendor the full amount, and one or more
    people owe back a portion of it: `{type: "split", linked transaction
    hash, total_amount, splits: [{person, amount, status}], date}`. The
    underlying transaction keeps its normal category and full amount (it's
    a real expense) — the split entry only tracks what's owed back, it
    does not reclassify the transaction.
- **`data/vendor_cache.json`** — vendor → category/subcategory mapping
  learned over time, so previously-seen vendors are not re-sent to the LLM.
  Keeps categorization consistent across months and reduces API calls.
- **`categories.yaml`** — editable category/subcategory taxonomy used by the
  categorization agent (see Categories section below).
- **`config.yaml`** — Ollama host URL and model name, so the runtime can be
  pointed at a different host/model without touching code.

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
Lend/Borrow:
  - (tracked separately in ledger.json, not a spending category)
Other/Uncategorized:
  -
```

## Input format

Validated against a real sample export.
The file is a **CSV** with structure the parser must handle:

- **Header metadata block**: account holder name, address, account number, IFSC, period, currency, etc. Not tabular — must be skipped.
- **Transaction table**: begins at the row whose first cells are exactly
  `Sl. No.`, `Transaction Date`, `Value Date`, `Description`,
  `Chq /Ref No.`, `Amount`, `Dr / Cr`, `Balance`, `Dr / Cr`.
- **Footer block**: a `Closing Balance` row followed by free-text notes.
  Must be skipped.
- **`Transaction Date`** includes a timestamp (`31-03-2026 07:48:39`);
  **`Value Date`** is date-only. Both are `DD-MM-YYYY[ HH:MM:SS]`.
- **Numeric columns** (`Amount`, `Balance`) use Indian comma grouping (e.g.
  `1,33,116.37`) — parser must strip grouping commas before converting to
  float, for both 2-comma and no-comma values.
- **Description** for UPI transactions follows the pattern
  `UPI/<NAME>/<UPI reference>/<short note>`. The trailing note (`bus`, `bike`,
  `auto`, etc.) is a short user-entered remark from when the payment was
  made, and is a useful categorization signal.
- Non-UPI rows exist too (e.g. `Int.Pd:<account>:<period>` for interest
  credited) with an empty `Chq /Ref No.`.

The parser targets this exact column layout for v1. If other banks/formats
are needed later, it can be extended with a column-mapping config — out of
scope for v1.

## Components

1. **Statement Parser** — reads the bank statement CSV, validates expected columns
   are present, normalizes rows into clean transaction records (date,
   description, amount, debit/credit direction). Computes a dedup hash per
   row (date + ref number + amount) to detect already-imported transactions
   on re-runs.

2. **Categorization Agent** — for each transaction whose vendor is not
   already in `vendor_cache.json`, batches calls to the local Ollama model
   with `categories.yaml` as the taxonomy. Returns category, subcategory, a
   normalized vendor name (e.g. "SWIGGY*ORDER38292" → "Swiggy"), and a
   confidence score. Updates the vendor cache with new mappings. For UPI
   transactions paid to an individual name, the trailing description note
   (e.g. `/auto`, `/bus`, `/bike`) is passed to the LLM as context — this is
   what lets `UPI/<name>/<transaction id>/auto` be confidently
   categorized as Transport rather than treated as an unexplained transfer
   to a person.

3. **P2P Transfer Detector** — real statement data shows that most UPI
   payments to an individual name are ordinary small expenses (auto-rickshaw
   fare, bus fare, paying a shopkeeper by UPI), *not* lend/borrow — so
   "paid to a person" alone is too broad a signal. Candidates are flagged
   only when a payment to an individual-looking name **also** has low
   categorization confidence or no usable descriptive note (i.e. the
   Categorization Agent couldn't confidently explain what the payment was
   for). This heuristic never assigns lend/borrow itself — flagged items
   still go to the user for a decision, same as before.

4. **Interactive CLI Review** — after categorization and P2P detection, the
   tool prints each flagged candidate and any low-confidence categorization
   for the user to resolve via terminal prompts:
   - P2P candidates: user picks `[l]ent / [b]orrowed / [e]xpense / [s]kip`
     and supplies the person's name if lent/borrowed.
   - Low-confidence categorizations: user picks/overrides the
     category/subcategory.

5. **Ledger Manager** — writes confirmed direct lend/borrow entries to
   `ledger.json`. Also exposes a `settle` CLI command (e.g.
   `python track_expenses.py settle "Rahul Sharma"`) to mark a person's
   outstanding balance as settled — this works across both direct-loan and
   split entries for that person.

6. **Split Tracker** — a separate on-demand command
   (`python track_expenses.py split`), run whenever a group expense needs
   recording, entirely independent of the monthly categorization run and
   never involving the LLM:
   - Prompts for a search term (date/vendor/amount) and lists matching
     transactions already in `transactions.csv`.
   - User picks the transaction, then either does an equal split (enter
     number of people + names) or a custom split (enter each person's
     owed amount individually, for uneven shares like "drinks cost more").
   - Validates that the sum of split amounts is less than the transaction
     total (the remainder is the user's own share, not a receivable).
   - Writes a `split`-type entry to `ledger.json`, linked to the
     transaction's dedup hash. The transaction itself is untouched — it
     keeps its original category and full amount.

7. **Insights Engine** — reads the full `transactions.csv` history to
   compute: month-over-month category spending deltas, top vendors by
   spend, recurring-charge detection (same vendor + similar amount
   repeating across months), and notable spending spikes.

8. **Excel Dashboard Generator** — builds `output/expense_dashboard_<month>.xlsx`
   per the finalized workbook format below.

## Excel Dashboard Format

Six sheets, in this order:

1. **Dashboard** (landing sheet, charts only, no raw data)
   - Pie chart: this month's spend by category
   - Line chart: total spend per month over time (full history so far)
   - Bar chart: top 5 vendors by spend this month
   - Summary box: total currently owed to you / owed by you

2. **Transactions** (raw data for the month, as a native Excel Table for
   sort/filter)
   - Columns: `Date, Value Date, Description, Vendor, Category,
     Subcategory, Amount, Dr/Cr, Balance, Flag` (Flag is blank, or
     "Split"/"Lent"/"Borrowed" if the row is linked to a ledger entry)

3. **Category Summary**
   - Table: `Category, Subcategory, Total Amount, % of Month` (pandas
     groupby, written as values)
   - Pie chart: category-level split
   - Horizontal bar chart: subcategory breakdown within top categories

4. **Trends** (uses full transaction history across all months processed
   so far, not just the current month)
   - Table: months as rows, categories as columns, total spend as values
     (a computed matrix — see pivot-table note below)
   - Line chart: overall monthly spend trend
   - Stacked bar chart: monthly spend broken down by category
   - Bar chart: daily spend within the current month (within-month
     pattern — early vs. late month spikes)

5. **Lend & Borrow Ledger**
   - Summary line: total currently owed to you / owed by you
   - Table: `Person, Type (Direct/Split), Direction, Amount, Date, Status,
     Linked Transaction`
   - Horizontal bar chart: outstanding amount per person, colored by
     direction

6. **Insights**
   - Table: top vendors by spend, recurring charges detected (vendor,
     amount, frequency), month-over-month category deltas with
     conditional-formatting arrows (green ↓ / red ↑)
   - Small bar chart: top vendors

**Pivot-table note:** all "pivot-style" tables (Category Summary, Trends
matrix) are pandas-computed aggregates written as plain values in native
Excel Tables, not literal interactive Excel PivotTable objects —
openpyxl's support for writing real PivotTables is limited/fragile. Charts
reference these computed ranges directly. Sortable/filterable Excel Tables
and real native charts are still produced; the user can build their own
manual PivotTable from the Transactions sheet later if desired.

## Data flow (one monthly run)

1. User runs `python track_expenses.py process statements/july_2026.csv`
2. Parser skips the header metadata block, validates the transaction table
   columns, extracts transactions, computes dedup hashes, and skips rows
   already present in `transactions.csv` (safe to re-run on the same file).
3. Categorization Agent checks `vendor_cache.json` first; only new/unknown
   vendors are sent to the local Ollama model.
4. P2P Transfer Detector flags likely person-to-person transfers with low
   categorization confidence.
5. Interactive CLI Review presents flagged candidates and low-confidence
   categorizations to the user for confirmation/override.
6. Confirmed transactions are appended to `transactions.csv`; lend/borrow
   ones are also written to `ledger.json`.
7. Insights Engine reads full transaction history to compute trends.
8. Excel Dashboard Generator writes the output workbook.

## Error handling

- Missing/unexpected columns in the input file → clear error listing
  expected vs. found columns; abort before any writes.
- Ollama not running / model not pulled → clear error naming the exact
  command to fix it (`ollama serve`, `ollama pull qwen2.5:3b-instruct`)
  rather than a raw connection-refused traceback.
- Malformed/unparseable LLM response (not valid JSON, missing fields) →
  retry once, then fall back to flagging the transaction as
  "Other/Uncategorized" for manual review rather than crashing the run.
- On persistent failure mid-run, save progress made so far
  (already-categorized transactions) so the user can resume rather than
  losing the whole run.
- Re-running the same statement file → duplicate rows detected via hash and
  skipped, not double-counted.
- Ambiguous/unparseable amount or date in a row → flagged and shown to the
  user rather than silently guessed or dropped.
- Split search with no matching transactions, or an ambiguous match (many
  results) → show the list and let the user narrow/reselect rather than
  guessing which transaction they meant.
- Split amounts entered that sum to more than (or equal to) the
  transaction total → rejected with an explanation; the user's own share
  must be greater than zero.

## Testing

- Parser tested against the real sample fixture in `example doc/`
  (header/footer skipping, comma-grouped numbers, dual Dr/Cr columns).
- P2P heuristic tested against known transfer-pattern examples, including
  the real "auto/bus/bike" cases that should *not* be flagged since they
  categorize confidently.
- Categorization Agent tested with mocked Ollama HTTP responses — no
  dependency on Ollama actually running in the test suite.
- End-to-end test: sample statement → full pipeline → assert the output
  workbook has the expected sheets and the ledger is updated correctly.
- Split Tracker tested independently of the LLM pipeline: transaction
  search/matching, equal vs. custom split math, and rejection of
  over-total split amounts.

## Explicitly out of scope for v1

- Automatic bank API integration / receipt scanning (manual monthly
  statement upload only, per user's current preference).
- Multi-bank column-mapping configuration (single hardcoded column layout
  matching the Kotak Mahindra export for now).
- Non-Excel output formats (dashboards, notifications, etc.) — noted as
  possible future extensions but not built now.
- LLM-driven lend/borrow classification — this is always a user decision.
- Managing the Ollama installation/model download itself — the tool
  assumes Ollama is installed and the configured model is already pulled.
