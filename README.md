# Expense Tracker

Turn the statement export from your bank into a categorized Excel dashboard. It works with any bank, and your data never leaves your machine.

Once a month, drop in your statement. The tool works out the file layout, categorizes every transaction with a small LLM running locally through [Ollama](https://ollama.com), asks you about anything it isn't sure of, and builds a six-sheet Excel workbook with your spending breakdown, trends, recurring charges, and who owes you money.

## Features

- **Works with any bank.** Standard formats (OFX/QFX, QIF, MT940, CAMT.053) are read directly. For a CSV or XLSX layout it hasn't seen before, the tool works out the column mapping once, checks it against the running balance, and saves it, so the same bank's statements parse instantly from then on.
- **Handles local number and date formats.** It reads `1,33,116.37`, `1,234.56` and `1.234,56` correctly, tells day-first dates from month-first ones, and tracks the currency of every account. All money is handled as exact decimals.
- **Gets smarter each month.** Categories come from your own rules first, then from vendors it has already seen, then from the local model, and finally from you. Every correction you make is remembered.
- **Never guesses silently.** Low-confidence categories and possible loans to friends are shown to you for confirmation. Whether a payment was a loan is always your decision.
- **Tracks loans and split bills.** Record money you lent or borrowed, and split a group expense equally or with custom amounts. Mark a person settled when they pay you back.
- **Excel dashboard.** Six sheets: Dashboard, Transactions, Category Summary, Trends, Lend & Borrow Ledger, and Insights. If your history has more than one currency, each currency gets its own set of sheets, and currencies are never added together.
- **Private by default.** No cloud APIs, no accounts, and no bank logins. Everything runs and stays on your computer.

## Requirements

- Python 3.11 or newer
- [Ollama](https://ollama.com), with a model pulled (the default is `qwen2.5:7b-instruct`, about 4.7 GB; it needs roughly 6 GB of GPU memory to run fast)

## Setup

```bash
git clone https://github.com/Empyrean1001/ai-expense-tracker.git
cd ai-expense-tracker
pip install -r requirements.txt

ollama pull qwen2.5:7b-instruct
ollama serve            # if it isn't already running

python track_expenses.py doctor
```

`doctor` checks your dependencies, the data folder, and the Ollama connection. For anything that fails, it prints the command that fixes it.

## Usage

### Process a monthly statement

```bash
python track_expenses.py process statements/july_2026.csv
```

Options:

- `--locale in|us|de` picks the regional hint pack used for categorization.
- `--currency EUR` sets the currency for formats that don't include one, such as QIF.

The first time you use a statement from a new bank, the tool learns its layout. If it can't work the layout out with confidence, it walks you through mapping the columns yourself. Either way, it never imports amounts it might have read wrong.

Running the same file twice is safe: transactions that were already imported are skipped.

The dashboard is written to `output/expense_dashboard_<month>.xlsx`.

### Split a bill

```bash
python track_expenses.py split
```

Search for the transaction, then split it equally or enter custom amounts. Only the other people's shares are tracked as money owed to you; the transaction itself keeps its full amount and category.

### Settle up

```bash
python track_expenses.py settle "Rahul Sharma"
```

This marks everything that person owes you, or that you owe them, as settled.

## Configuration

| File | What it's for |
|---|---|
| `config.yaml` | Ollama host and model, review confidence threshold, default locale and currency |
| `categories.yaml` | Your category and subcategory list |
| `rules.yaml` | Rules that always win, e.g. anything containing "EDEKA" is Groceries |
| `locales/*.yaml` | Regional merchant hints (India, US and Germany included; add your own) |

Your data lives in `data/`, which git ignores:

- `transactions.csv`: the full transaction history (the source of truth)
- `ledger.json`: loans and splits
- `vendor_cache.json`: categories learned for each vendor
- `profiles/`: saved bank layouts

These are plain text files, so you can open and edit them by hand. The Excel workbook is generated from them and can be rebuilt at any time.

## How it works

```
statement file
  → detect the format
  → standard format? parse it directly
  → CSV/XLSX? use the saved bank profile, or learn a new one (checked against the running balance)
  → categorize: your rules → vendor cache → local model → your review
  → append to the transaction history and ledger
  → build the Excel dashboard
```

The reasoning behind each design choice is written up in [`docs/ai-system-design-decisions.md`](docs/ai-system-design-decisions.md). Full specs are in [`docs/specs/`](docs/specs/) and implementation plans in [`docs/plans/`](docs/plans/).

## Project layout

```
track_expenses.py     CLI entry point (process / split / settle / doctor)
src/                  ingestion, parsing, categorization, ledger, dashboard
tests/                pytest suite with synthetic sample statements
evals/                accuracy checks for parsing and categorization
locales/              regional hint packs
docs/                 specs, plans and design notes
```

## Testing

```bash
python -m pytest              # unit tests (no Ollama needed)
python -m evals.run_evals     # parsing and categorization quality checks
```

The eval script always runs its parsing checks. The model-based checks (categorization accuracy, and whether high-confidence answers are actually right) run only when Ollama is reachable; otherwise they are skipped.

All test and eval data is synthetic. Never commit real bank statements: `data/`, `output/`, `statements/` and `*.csv` are git-ignored for that reason.

## Not supported yet

- Connecting directly to banks (Plaid, open banking, etc.), so you still export statements manually
- Converting between currencies
- PDF statements
- A web or mobile interface
