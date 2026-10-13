# Expense Tracker v2 — Universal Bank Ingestion, Worldwide-Ready Design

**Status:** Proposed (supersedes the v1 spec `2026-07-26-expense-tracker-design.md` for everything ingestion- and locale-related; v1's ledger, split tracker, review flow, and Excel dashboard carry forward unchanged unless stated).

Companion document: [`docs/ai-system-design-decisions.md`](../ai-system-design-decisions.md) records *why* each AI-architecture choice was made, as a study reference. This spec records *what* is being built.

## Purpose

Same product idea as v1 — a monthly, privacy-first workflow that turns a raw bank statement export into a categorized, insight-rich Excel dashboard with lend/borrow and split tracking — but redesigned so that:

1. **Any bank's statement export works**, not one hardcoded Kotak Mahindra CSV layout. A user in Berlin, São Paulo, or Bangalore drops in whatever their bank gives them (CSV, XLSX, OFX/QFX, QIF, CAMT.053, MT940) and the tool figures the format out.
2. **Any locale's conventions parse correctly**: `1,33,116.37` (Indian grouping), `1.234,56` (European), `1,234.56` (US); `DD-MM-YYYY` vs `MM/DD/YYYY`; any ISO 4217 currency.
3. **It behaves like a consumer product**: guided first run, a `doctor` command that diagnoses setup problems, graceful errors, and no assumption that the user knows what Ollama is before the tool tells them.

The core v1 principles are kept deliberately: all bank data stays on the user's machine by default, the LLM never silently decides anything ambiguous, and lend/borrow/split classification is always a human decision.

## What changes vs. v1 (summary)

| Area | v1 | v2 |
|---|---|---|
| Input formats | One CSV layout, matched by exact header | CSV/XLSX via LLM schema inference; OFX/QFX, QIF, CAMT.053, MT940 via deterministic standard parsers |
| Bank coverage | Kotak Mahindra only | Any bank; learned `BankProfile` cached per format fingerprint |
| Number/date parsing | Indian grouping, `DD-MM-YYYY` hardcoded | Locale inferred per profile, validated deterministically |
| Currency | Implicit INR | ISO 4217 per account; multi-currency reporting (no FX conversion in v2) |
| Money type | `float` | `Decimal` end-to-end |
| Taxonomy | India-specific names baked in | Locale-neutral base taxonomy + per-locale merchant hint packs |
| Setup | Assumes Ollama installed | `doctor` + guided first run; clear remediation messages |
| Quality control | Manual smoke checks | Golden-dataset evals for schema mapping and categorization |
| Pipeline, ledger, splits, review, Excel output | — | Unchanged from v1 |

## Architecture

Still a standalone local Python CLI with flat-file persistence and a local Ollama LLM. The pipeline gains one stage at the front — **Ingestion** — and the categorizer becomes a tiered cascade.

```
statement file (csv/xlsx/ofx/qif/camt/mt940)
        │
        ▼
┌─ Ingestion ────────────────────────────────────────────────┐
│ 1. Format detector (extension + content sniffing)          │
│ 2a. Standard formats → deterministic parser (no LLM)       │
│ 2b. CSV/XLSX → fingerprint → BankProfile cache hit?        │
│       hit  → deterministic parse using cached profile      │
│       miss → Schema Mapping Agent (LLM, once per format)   │
│              → profile validation (round-trip + balance    │
│                continuity) → cache profile → parse         │
└────────────────────────────────────────────────────────────┘
        │  canonical Transaction records (Decimal, ISO dates,
        ▼  ISO 4217 currency, signed direction, dedup hash)
┌─ Categorization cascade ───────────────────────────────────┐
│ tier 0: user rules (rules.yaml)                            │
│ tier 1: vendor cache (learned memory)                      │
│ tier 2: local LLM (Ollama, structured output)              │
│ tier 3: human review queue (low confidence / P2P flags)    │
└────────────────────────────────────────────────────────────┘
        │
        ▼
Interactive CLI review → transactions.csv → ledger.json
        → Insights Engine → Excel Dashboard Generator
```

`split` and `settle` remain standalone LLM-free commands exactly as in v1.

## Components

### 1. Format Detector

Decides which ingestion path a file takes. Extension first, then content sniffing (OFX/QFX and CAMT are XML-ish; QIF starts with `!Type:`; MT940 has `:20:`/`:61:` tags; otherwise treat as delimited text / spreadsheet). Output: a format tag. No LLM involved.

### 2. Standard-Format Parsers (deterministic, no LLM)

OFX/QFX, QIF, CAMT.053 (ISO 20022), and MT940 are *specifications* — banks that export them follow documented field semantics. Each gets a small deterministic parser straight to canonical `Transaction` records. The LLM is never used where a standard already answers the question.

### 3. Schema Mapping Agent (LLM, design-time only)

For CSV/XLSX files — the formats where every bank invents its own layout — the LLM is used **once per bank format**, not once per row:

- Input: the first ~40 raw rows of the file (enough to show metadata block, header row, sample transactions) plus the canonical field list.
- Output (structured, Pydantic-validated): a declarative **`BankProfile`**:
  - header row locator (matched cell signature) and footer sentinel
  - column mapping → canonical fields (date, description, amount(s), balance, reference)
  - date format string(s); timestamp presence
  - number locale (decimal separator, grouping style)
  - debit/credit representation: dual Dr/Cr columns, signed amounts, or separate debit/credit columns
  - currency (from metadata block if present, else asked of the user once)
- The profile is a *program the LLM wrote*, executed deterministically by the row parser. The LLM never sees individual transactions during a normal monthly run of a known bank.

**Profile validation (deterministic, before the profile is trusted):**
- every sample row parses without error under the profile;
- dates fall in a plausible range and are internally consistent (resolves `DD/MM` vs `MM/DD` ambiguity — if any day value exceeds 12 the order is proven; if not, ask the user once);
- **balance continuity**: where a running balance column exists, `previous_balance ± amount == balance` must hold across sample rows. This single check catches wrong amount columns, wrong sign conventions, and wrong number-locale guesses simultaneously.
- On validation failure: one retry with the failure fed back to the LLM, then fall back to an interactive column-mapping prompt (the user picks columns from a preview). Never silently import misparsed money.

**Profile cache:** `profiles/<fingerprint>.json`, keyed by a fingerprint of the normalized header signature. Next month's statement from the same bank is a cache hit — zero LLM calls for parsing. Profiles are human-readable JSON and hand-editable.

### 4. Canonical Transaction Model

One internal representation regardless of source format:

- `posted_at` (ISO 8601 datetime), `value_date` (ISO date)
- `amount`: `Decimal`, always positive, with `direction: debit|credit`
- `currency`: ISO 4217 code (per statement/account, stamped on every record)
- `description`, `reference`, `balance: Decimal | None`
- `vendor`, `description_note` (extracted by locale-aware description parsing — the UPI `UPI/<name>/<ref>/<note>` splitter becomes one of several pluggable description conventions; SEPA/ACH descriptions get their own)
- `dedup_hash` (value_date + reference + amount + direction + currency)
- `source_month`, categorization fields, `flag` — as v1

All money math uses `Decimal`. `float` never touches an amount.

### 5. Categorization Cascade (tiered)

Cheapest capable tier first; each tier only sees what the previous couldn't settle:

- **Tier 0 — user rules** (`rules.yaml`, optional): exact/substring vendor patterns the user pins (`"EDEKA" → Groceries`). Deterministic, instant, always wins.
- **Tier 1 — vendor cache**: v1's learned memory, unchanged, including the person-name+note compound cache key.
- **Tier 2 — local LLM**: Ollama structured-output call, as v1, with the taxonomy and any locale hint pack in the prompt.
- **Tier 3 — human review**: low-confidence results and P2P candidates, exactly the v1 interactive review. User corrections are written back into the vendor cache (and optionally promoted to a rule), so every correction makes tier 1/0 smarter and the system asks less over time.

### 6. Taxonomy & locale packs

`categories.yaml` becomes locale-neutral (`Delivery` not `Delivery (Swiggy/Zomato)`, `Ride-hailing` not `Cabs (Uber/Ola)`). Optional `locales/<code>.yaml` hint packs carry region-specific merchant knowledge (India: Swiggy, Zomato, UPI conventions; Germany: EDEKA, DB, SEPA `Lastschrift` semantics; …) and are appended to the tier-2 prompt. Users pick a locale on first run; hints are additive and editable.

### 7. Consumer-readiness layer

- **`track_expenses.py doctor`** — checks Python deps, Ollama reachability, model availability, writable data dirs; prints the exact fix command for each failure.
- **Guided first run** — on first `process`: pick locale/currency defaults, confirm data directory, run `doctor` automatically.
- **Graceful errors everywhere** — every failure names the file/row/column and the remediation, never a raw traceback (v1 policy, extended to ingestion).
- **Packaging** — installable via `pipx install` / `uv tool install`; still a CLI in v2 (a GUI/web front-end remains a future output adapter, same as v1's stance on non-Excel outputs).

### 8. Evaluation harness (`evals/`)

Two golden datasets, run by `python -m evals` before shipping any prompt/model/taxonomy change:

- **Schema-mapping evals**: a set of *synthetic* statement files in different bank layouts and locales (built to mimic real structures — no real bank data committed), each with the expected canonical parse. Metric: profiles inferred correctly, validation catches deliberately corrupted variants.
- **Categorization evals**: anonymized labeled transactions per locale. Metrics: accuracy vs. gold labels, and **calibration** — of the items the model marks high-confidence, how many are right (this decides whether the review threshold is trustworthy).

Eval results are the regression gate for AI behavior, the same way unit tests gate code behavior.

### 9. Unchanged from v1

Ledger manager, split tracker (LLM-free, on-demand), settle command, insights engine, Excel dashboard (6 sheets), LangGraph `StateGraph` orchestration with synchronous in-node review, flat-file persistence, `.gitignore` policy for real data. The v1 spec remains the reference for these.

## Persistent data (v2 additions)

- `profiles/<fingerprint>.json` — learned bank profiles (schema maps)
- `rules.yaml` — optional user categorization rules (tier 0)
- `locales/<code>.yaml` — merchant hint packs
- `transactions.csv` gains a `currency` column; totals in insights/dashboard group by currency (no FX conversion in v2 — a multi-currency user sees one set of figures per currency)

## Error handling (v2 additions)

- Unrecognized file format → name the detected content type and the supported list.
- Schema inference fails validation twice → interactive column mapping, never a bad import.
- Ambiguous date order with no disambiguating row → one explicit user question, answer stored in the profile.
- Mixed/missing currency metadata → asked once, stored in the profile.
- All v1 error-handling rules stand.

## Testing

- Standard-format parsers: fixture files per format (synthetic).
- Profile validator: unit tests for balance continuity, locale detection, date-order proof.
- Schema Mapping Agent: mocked LLM responses (valid, invalid, adversarially wrong) → assert validation catches bad profiles.
- Cascade routing: assert tier order and that corrections write back to cache.
- Everything in the v1 testing section stands.
- The eval harness (section 8) covers behavioral quality; the test suite covers code correctness. Both must pass.

## Explicitly out of scope for v2

- **Bank API / open-banking integrations** (Plaid, GoCardless, Account Aggregator): manual statement upload remains the input. Rationale in decisions doc (D-13).
- **FX conversion / consolidated multi-currency totals**: report per currency instead.
- **PDF statement ingestion**: deferred; the profile architecture accommodates it later (table extraction feeding the same Schema Mapping Agent), but table-from-PDF quality is its own project.
- **Cloud LLM tier**: the architecture reserves a config seam for a BYO-key remote model, but v2 ships local-only (D-5).
- **GUI/web dashboard, database, multi-user**: future output adapters / scaling steps, not now.
