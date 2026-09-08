# Expense Tracker v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v2 worldwide-ready expense tracker: universal bank-statement ingestion (LLM-inferred cached bank profiles + deterministic standard-format parsers), a tiered categorization cascade over a local Ollama LLM, human review with write-back learning, flat-file persistence, and a per-currency Excel dashboard.

**Architecture:** Ingestion normalizes any supported statement file into canonical `Transaction` records (`Decimal` money, ISO dates, ISO 4217 currency). CSV/XLSX layouts are parsed by a declarative `BankProfile` that the LLM infers once per bank format and that deterministic code validates (balance continuity) and executes forever after (cached by header fingerprint). Standard formats (OFX/QIF/MT940/CAMT.053) never touch the LLM. Categorization runs a cascade: user rules → vendor cache → local LLM → human review, with corrections written back to the cache. The monthly pipeline is a thin LangGraph `StateGraph`; `split`/`settle`/`doctor` are plain CLI commands.

**Tech Stack:** Python 3.11+, pydantic v2, pandas, openpyxl, PyYAML, click, httpx, langgraph, langchain-ollama, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-expense-tracker-v2-worldwide-design.md` (v2) layered on `docs/superpowers/specs/2026-07-26-expense-tracker-design.md` (v1, still authoritative for ledger/split/review/dashboard behavior). This plan supersedes `2026-07-29-expense-tracker-implementation.md`.

## Global Constraints

- Money is `Decimal` everywhere in `src/`; `float` never touches an amount (analytics reads of the CSV for display aggregation may use pandas floats — the CSV is the exact source of truth).
- All LLM calls go to the local Ollama host in `config.yaml`; default model `qwen2.5:3b-instruct`; the model object is injected (`.invoke(prompt) -> <pydantic>`), never constructed inside `src/` except in `track_expenses.py`.
- Standard formats (OFX/QFX, QIF, MT940, CAMT.053) are parsed deterministically — no LLM (spec D-9).
- CSV/XLSX go through `BankProfile`; a profile must pass deterministic validation (balance continuity + date plausibility) before being cached or used; on 2× inference failure fall back to interactive column mapping; never import misparsed money (spec D-2).
- During a run of a known bank format, zero LLM calls are made for parsing (spec D-1).
- Cascade order is fixed: rules → vendor cache → LLM → review; review corrections write back to the vendor cache (spec D-3/D-4). Lend/borrow is never LLM-decided.
- `categories.yaml` is locale-neutral; region merchant knowledge lives only in `locales/<code>.yaml`.
- Dedup hash = sha256 of `value_date|reference|amount|direction|currency`.
- Persistent state lives under `data/` (gitignored): `data/transactions.csv`, `data/ledger.json`, `data/vendor_cache.json`, `data/profiles/*.json`. `rules.yaml` and `locales/` are user-editable and committed.
- Excel workbook: six sheets in v1's order; when >1 currency exists in history, per-currency figures (sheet names suffixed with the currency code); no FX conversion.
- Tests: pytest, no dependency on a running Ollama (mock the model); fixtures are synthetic, never real bank data.
- Real data dirs (`data/`, `output/`, `statements/`, `example doc/`) stay gitignored; `tests/fixtures/` (synthetic) is committed.

## File Structure

```
track_expenses.py        # click CLI: process / split / settle / doctor
config.yaml              # ollama host+model, review threshold, defaults (locale, currency)
categories.yaml          # locale-neutral taxonomy
rules.yaml               # tier-0 user rules (starter: empty list + commented example)
locales/in.yaml          # India hint pack        locales/us.yaml, locales/de.yaml
src/
  models.py              # Transaction, CategorizationResult, BankProfile, Rule, ledger models
  config_loader.py       # yaml loaders (config, categories, rules, locale pack)
  localeparse.py         # parse_amount(raw, decimal_separator) -> Decimal; parse_datetime_multi; disambiguate_date_order
  format_detector.py     # detect_format(path) -> FormatTag
  standard_formats.py    # parse_ofx / parse_qif / parse_mt940 / parse_camt053 -> list[Transaction]
  descriptions.py        # extract_vendor_and_note(description, convention)
  profile.py             # normalize_cell, find_header_row, parse_rows_with_profile
  profile_validator.py   # validate_profile(rows, profile) -> list[str] errors
  profile_store.py       # fingerprint, load/save/match cached profiles
  schema_agent.py        # infer_profile (LLM) + interactive_column_mapping fallback
  ingestion.py           # ingest(path, ...) -> list[Transaction]: routes everything above
  rules.py               # apply_rules (tier 0)
  vendor_cache.py        # tier 1 (v1 design, unchanged)
  ollama_client.py       # preflight check + categorize_via_llm (tier 2, + locale hints)
  p2p_detector.py        # v1 design, unchanged
  categorizer.py         # the cascade + cache_key_for
  review.py              # tier 3 + returns corrections for write-back
  transactions_store.py  # v1 design + currency column, Decimal-safe serialization
  ledger.py              # v1 design, Decimal amounts
  split_tracker.py       # v1 design, Decimal math
  insights.py            # v1 design, operating on a single-currency df
  dashboard.py           # v1 six sheets, generated per currency
  graph.py               # LangGraph StateGraph
  doctor.py              # environment diagnostics
tests/                   # pytest, one test module per src module (merged where trivial)
tests/fixtures/          # synthetic: kotak-style.csv, eu-style.csv, sample.ofx, sample.qif, sample.sta, sample_camt.xml
evals/run_evals.py       # golden-dataset eval runner (schema + categorization)
evals/schema/            # synthetic statements + expected canonical parses
evals/categorization/labeled.csv
```

Dependency order (drives task order): models → config/localeparse → detector/standard_formats/descriptions → profile/validator/store → schema_agent → ingestion → rules/vendor_cache/ollama_client/p2p → categorizer/review → stores (transactions/ledger/split) → insights/dashboard → graph/CLI/doctor → evals.

---

### Task 1: Scaffolding + canonical models

**Files:** Create `requirements.txt` (add `pytest>=8`), rewrite `config.yaml` (add `defaults: {locale, currency}`), rewrite `categories.yaml` (locale-neutral), create `rules.yaml`, `locales/{in,us,de}.yaml`, `src/models.py`, `tests/test_models.py`. Update `.gitignore` if `data/` not covered.

**Produces:** `Transaction` (Decimal amount ≥ 0, `direction: Literal["debit","credit"]`, `currency: str`, `balance: Decimal|None`, plus v1 enrichment fields), `CategorizationResult` (v1), `BankProfile` (header_signature, footer_sentinels, column_map: dict[str,int], date_formats: list[str], decimal_separator: `"."|","`, amount_convention: `"dual_drcr"|"signed"|"separate"`, debit_marker, signed_debit_negative, currency, description_convention: `"upi"|"generic"`, bank_name), `Rule` (pattern, match: `"exact"|"contains"`, category, subcategory), v1 ledger models with Decimal. All Decimal fields serialize to str in JSON mode (field_serializer).

- [x] Tests: model construction, Decimal-from-string validation, BankProfile column_map completeness validator (per amount_convention), Decimal→str JSON round-trip.
- [x] Implement; `pytest -q` green; commit.

### Task 2: Locale parsing primitives

**Files:** `src/localeparse.py`, `tests/test_localeparse.py`.

**Produces:**
- `parse_amount(raw: str, decimal_separator: str = ".") -> Decimal` — handles `1,33,116.37` / `1,234.56` / `1.234,56` / `1 234,56` / `(123.45)`→negative / leading currency symbols; raises `LocaleParseError` on garbage.
- `parse_datetime_multi(raw: str, formats: list[str]) -> datetime` — first format that fits.
- `disambiguate_date_order(raws: list[str], separator_agnostic=True) -> Literal["day_first","month_first","ambiguous"]` — proven if any first/second component >12.

- [x] Tests for each locale grouping, negatives, garbage, ambiguity proof cases; implement; commit.

### Task 3: Format detector + standard-format parsers

**Files:** `src/format_detector.py`, `src/standard_formats.py`, `src/descriptions.py`, fixtures `tests/fixtures/{sample.ofx,sample.qif,sample.sta,sample_camt.xml}`, tests.

**Produces:**
- `detect_format(path: Path) -> Literal["csv","xlsx","ofx","qif","mt940","camt053"]` — extension first, then content sniff (`<OFX`/`OFXHEADER`, `!Type:`, `:20:`+`:61:`, `BkToCstmrStmt`, xlsx magic `PK`); `UnknownFormatError` naming supported list.
- `parse_ofx(path) -> list[Transaction]` (regex over STMTTRN; CURDEF currency; signed TRNAMT), `parse_qif(path, currency, date_formats) -> list[Transaction]`, `parse_mt940(path) -> list[Transaction]` (`:60F:` currency, `:61:` value date + D/C + comma-decimal amount, `:86:` description), `parse_camt053(path) -> list[Transaction]` (namespace-agnostic Ntry walk).
- `extract_vendor_and_note(description: str, convention: str) -> tuple[str, str]` — `"upi"` splits `UPI/<name>/<ref>/<note>`; `"generic"` returns (cleaned description, "").

- [x] Fixture-driven tests per format (dates, Decimal amounts, direction, currency, dedup hash present); implement; commit.

### Task 4: BankProfile executor + validator

**Files:** `src/profile.py`, `src/profile_validator.py`, fixtures `tests/fixtures/{kotak_style.csv,eu_style.csv}` (synthetic), tests.

**Produces:**
- `normalize_cell(cell) -> str` (strip all whitespace, lowercase), `find_header_row(rows, signature) -> int | None`, `parse_rows_with_profile(rows: list[list[str]], profile: BankProfile) -> list[Transaction]` (skips pre-header, stops at footer sentinel/blank-key rows, applies localeparse per profile, value_date falls back to posted date, computes dedup hash and source_month from value_date).
- `validate_profile(rows, profile) -> list[str]` — empty list = valid. Checks: all rows parse; dates within 2000-01-01..today+366d; **balance continuity** (`prev ± amount == balance`) holding in either row orientation when balance is mapped; direction sanity (signed convention yields both signs or matches marker).

- [x] Tests: kotak-style profile parses fixture exactly; eu-style (comma decimal, signed amounts, day-first) parses; validator catches wrong amount column, wrong decimal separator, swapped date order (via impossible dates or broken continuity); implement; commit.

### Task 5: Profile store + Schema Mapping Agent

**Files:** `src/profile_store.py`, `src/schema_agent.py`, tests (mocked model).

**Produces:**
- `fingerprint(header_signature: list[str]) -> str`; `save_profile(dir, profile)`, `load_profiles(dir) -> list[BankProfile]`, `match_profile(dir, rows) -> BankProfile | None` (any cached profile whose header signature is found in rows).
- `build_inference_prompt(sample_rows) -> str`; `infer_profile(sample_rows, model, max_attempts=2) -> BankProfile` — model is `.with_structured_output(BankProfile)`-shaped; each failed validation feeds errors back into the retry prompt; raises `SchemaInferenceError` after attempts.
- `interactive_column_mapping(rows, prompt_fn, echo_fn) -> BankProfile` — shows a numbered preview of the detected header row + first rows; user picks column indices, date order, decimal separator, amount convention, currency.

- [x] Tests: fingerprint stability, save/match round trip, infer_profile happy path with fake model, retry-then-fallback on invalid profile, interactive mapping with scripted prompt_fn; implement; commit.

### Task 6: Ingestion orchestrator

**Files:** `src/ingestion.py`, tests.

**Produces:** `ingest(path, *, profiles_dir, model_factory, prompt_fn, echo_fn, default_currency, default_locale) -> list[Transaction]`:
1. `detect_format`; standard formats → their parser (QIF gets default_currency).
2. CSV/XLSX → raw rows (csv.reader / openpyxl values-as-str) → `match_profile` hit → parse.
3. Miss → `infer_profile(model_factory())` → validate → (ambiguous date order → one user question) → cache → parse. Two failures → `interactive_column_mapping` → validate → cache → parse.
4. Currency missing → asked once, stored in profile.

`model_factory` is lazy so known-bank runs build no LLM client.

- [x] Tests: cached-profile path never calls model_factory (assert via raising factory); inference path caches then parses; fallback path; implement; commit.

### Task 7: Categorization cascade (tiers 0–2)

**Files:** `src/rules.py`, `src/vendor_cache.py`, `src/ollama_client.py`, `src/p2p_detector.py`, `src/categorizer.py`, tests.

**Produces:** v1 designs (vendor_cache, p2p_detector verbatim; ollama_client with preflight `check_ollama_available` and `categorize_via_llm(description, note, categories, hints_text, model)`), plus:
- `apply_rules(txn, rules: list[Rule]) -> CategorizationResult | None` (confidence 1.0).
- `cache_key_for(txn)` — v1: person-name keys include the payment note.
- `categorize_transactions(txns, *, rules, vendor_cache, model, categories, hints_text) -> None` — tier order rules→cache→LLM; only LLM results enter the cache.

- [x] Tests: rule short-circuits cache and model; cache hit skips model; person/merchant cache keys; LLM fallback to Uncategorized after retry (fake failing model); implement; commit.

### Task 8: Review with write-back + stores

**Files:** `src/review.py`, `src/transactions_store.py`, `src/ledger.py`, `src/split_tracker.py`, tests.

**Produces:** v1 designs with v2 deltas:
- `review_transactions(...) -> tuple[list[DirectLedgerEntry], list[Transaction]]` — second element = user-corrected transactions; caller writes them back into vendor_cache at confidence 1.0.
- `transactions_store.CSV_COLUMNS` gains `currency`; amounts/balances written as exact Decimal strings.
- `ledger.py` / `split_tracker.py`: v1 logic, Decimal amounts, split validation strict-less-than total.

- [x] Tests: review scripted-prompt flow incl. correction write-back list; store round trip preserves amount strings exactly; ledger outstanding math; split equal/custom/over-total rejection; implement; commit.

### Task 9: Insights + per-currency dashboard

**Files:** `src/insights.py`, `src/dashboard.py`, tests.

**Produces:** v1 insights (top vendors, recurring charges, MoM deltas) operating on a df pre-filtered to one currency. `generate_dashboard(transactions_csv, ledger_path, output_path, current_month)` groups history by currency: single currency → v1's exact six sheets; multiple → six sheets per currency with ` <CCY>` suffix. All v1 guard rails (no empty tables/charts).

- [x] Tests: insights math on synthetic df; workbook opens, has expected sheets for 1- and 2-currency histories; implement; commit.

### Task 10: Graph, CLI, doctor

**Files:** `src/graph.py`, `src/doctor.py`, `track_expenses.py`, tests (graph routing with all-fakes).

**Produces:**
- `build_graph(deps) -> compiled StateGraph`: ingest → dedup-filter → (end if empty) → categorize → review → persist → dashboard; nodes are thin wrappers over the functions above.
- `run_doctor(config) -> list[Check]` — deps importable, data dirs writable, Ollama reachable, model pulled; each failure prints its fix command; CLI `doctor` exits non-zero on failure.
- CLI: `process <file> [--locale] [--currency]`, `split`, `settle <person>`, `doctor`. Only `track_expenses.py` constructs `ChatOllama`; preflight runs before the graph only when the run may need the LLM.

- [x] Tests: graph short-circuits on no-new-transactions; end-to-end fake run appends and generates workbook; implement; commit.

### Task 11: Eval harness

**Files:** `evals/run_evals.py`, `evals/schema/` (each fixture + `expected.json` canonical parse + committed known-good profile), `evals/categorization/labeled.csv` (synthetic labeled rows per locale).

**Produces:** `python -m evals.run_evals` — always runs deterministic checks (committed profiles reproduce expected parses; validator rejects corrupted variants); runs LLM schema-inference and categorization accuracy+calibration only when Ollama is reachable, else prints SKIPPED with reason. Non-zero exit on regression.

- [x] Implement, run deterministic portion green, commit.

## Self-review notes

- Spec coverage: every v2 spec component maps to a task (ingestion §1-4→T3-6, cascade §5→T7, taxonomy §6→T1, consumer layer §7→T10, evals §8→T11, unchanged v1 parts §9→T8-10). FX conversion, PDF, bank APIs correctly absent (out of scope).
- Interfaces named in later tasks match earlier definitions (checked: `BankProfile` fields, `parse_rows_with_profile`, `model_factory`, `CSV_COLUMNS + currency`).
