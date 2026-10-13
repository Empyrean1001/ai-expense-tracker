"""Expense tracker CLI: process / split / settle / doctor.

This is the only file that constructs a real ChatOllama, and the only file
that knows where the flat files live. Everything else takes its dependencies
as parameters.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import click

from src.categorizer import cache_key_for, categorize_transactions
from src.config_loader import load_categories, load_config, load_locale_hints, load_rules
from src.dashboard import generate_dashboard
from src.graph import PipelineDeps, build_graph
from src.ingestion import ingest as ingest_file
from src.ledger import (
    add_direct_entry,
    add_split_entry,
    load_ledger,
    settle_person,
)
from src.models import CategorizationResult
from src.ollama_client import OllamaError, check_ollama_available
from src.review import review_transactions
from src.rules import apply_rules
from src.split_tracker import (
    build_custom_split,
    build_equal_split,
    create_split_entry,
    search_transactions,
)
from src.transactions_store import (
    append_transactions,
    load_existing_hashes,
    read_all_transactions,
)
from src.vendor_cache import load_vendor_cache, save_vendor_cache

PROJECT_DIR = Path(__file__).parent


class _Paths:
    def __init__(self, config: dict):
        data_dir = PROJECT_DIR / config.get("paths", {}).get("data_dir", "data")
        self.data_dir = data_dir
        self.transactions = data_dir / "transactions.csv"
        self.ledger = data_dir / "ledger.json"
        self.vendor_cache = data_dir / "vendor_cache.json"
        self.profiles = data_dir / "profiles"
        self.output = PROJECT_DIR / config.get("paths", {}).get("output_dir", "output")


def _load_all():
    config = load_config(PROJECT_DIR / "config.yaml")
    return config, _Paths(config)


def _make_model_factory(config: dict, schema=None):
    """Lazy, memoized model construction with a pre-flight on first use.
    A run that never needs the LLM never requires Ollama to be up."""
    host = config["ollama"]["host"]
    model_name = config["ollama"]["model"]
    cache: dict = {}

    def factory():
        key = schema or "categorization"
        if key not in cache:
            check_ollama_available(host, model_name)
            from langchain_ollama import ChatOllama

            llm = ChatOllama(base_url=host, model=model_name, temperature=0)
            cache[key] = llm.with_structured_output(schema) if schema else llm
        return cache[key]

    return factory


@click.group()
def cli():
    """Turn bank statements from any bank into a categorized Excel dashboard."""


@cli.command()
@click.argument("statement", type=click.Path(exists=True, path_type=Path))
@click.option("--locale", "locale_code", default=None, help="Locale hint pack (e.g. in, us, de).")
@click.option("--currency", default=None, help="Default currency for formats that carry none.")
def process(statement: Path, locale_code: str | None, currency: str | None):
    """Run the monthly pipeline on one statement file."""
    config, paths = _load_all()
    categories = load_categories(PROJECT_DIR / "categories.yaml")
    rules = load_rules(PROJECT_DIR / "rules.yaml")
    hints = load_locale_hints(
        PROJECT_DIR / "locales", locale_code or config["defaults"]["locale"]
    )
    default_currency = (currency or config["defaults"]["currency"]).upper()
    threshold = config["review"]["confidence_threshold"]

    from src.schema_agent import HeaderLabels

    schema_model_factory = _make_model_factory(config, schema=HeaderLabels)
    categorization_model_factory = _make_model_factory(config, schema=CategorizationResult)

    vendor_cache = load_vendor_cache(paths.vendor_cache)

    def categorize_fn(txns):
        needs_llm = any(
            apply_rules(t, rules) is None and cache_key_for(t) not in vendor_cache
            for t in txns
        )
        model = categorization_model_factory() if needs_llm else None
        categorize_transactions(
            txns, rules=rules, vendor_cache=vendor_cache, model=model,
            categories=categories, hints_text=hints,
        )

    def persist_fn(txns, ledger_entries, corrections):
        for txn in corrections:
            vendor_cache[cache_key_for(txn)] = CategorizationResult(
                vendor=txn.vendor, category=txn.category,
                subcategory=txn.subcategory, confidence=1.0,
            )
        append_transactions(paths.transactions, txns)
        for entry in ledger_entries:
            add_direct_entry(paths.ledger, entry)
        save_vendor_cache(paths.vendor_cache, vendor_cache)
        click.echo(f"Imported {len(txns)} transaction(s).")

    def dashboard_fn(month):
        df = read_all_transactions(paths.transactions)
        out = generate_dashboard(
            df, load_ledger(paths.ledger),
            paths.output / f"expense_dashboard_{month}.xlsx", month,
        )
        click.echo(f"Dashboard written to {out}")
        return str(out)

    deps = PipelineDeps(
        ingest_fn=lambda p: ingest_file(
            Path(p), profiles_dir=paths.profiles,
            model_factory=schema_model_factory,
            default_currency=default_currency,
        ),
        existing_hashes_fn=lambda: load_existing_hashes(paths.transactions),
        categorize_fn=categorize_fn,
        review_fn=lambda txns: review_transactions(txns, categories, threshold),
        persist_fn=persist_fn,
        dashboard_fn=dashboard_fn,
        echo_fn=click.echo,
    )
    try:
        build_graph(deps).invoke({"statement_path": str(statement)})
    except OllamaError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
def split():
    """Record that other people owe you part of an imported expense."""
    _, paths = _load_all()
    df = read_all_transactions(paths.transactions)
    if df.empty:
        raise click.ClickException("No transactions imported yet — run `process` first.")

    matches = search_transactions(df, click.prompt("Search (vendor/amount/date)"))
    if matches.empty:
        raise click.ClickException("No matching transactions.")
    view = matches.tail(15).reset_index(drop=True)
    for i, row in view.iterrows():
        click.echo(f"[{i}] {row['value_date']}  {row['description'][:60]}  "
                   f"{row['amount']} {row['currency']}")
    chosen = view.iloc[int(click.prompt("Which one? (number)"))]
    total = Decimal(str(chosen["amount"]))

    if click.prompt("Split [e]qually or [c]ustom amounts?",
                    type=click.Choice(["e", "c"])) == "e":
        people = [p.strip() for p in
                  click.prompt("Names of the others (comma-separated)").split(",") if p.strip()]
        shares = build_equal_split(total, people)
    else:
        shares_dict: dict[str, Decimal] = {}
        while True:
            person = click.prompt("Person (blank to finish)", default="", show_default=False).strip()
            if not person:
                break
            shares_dict[person] = Decimal(click.prompt(f"Amount {person} owes"))
        shares = build_custom_split(shares_dict)

    entry = create_split_entry(
        chosen["dedup_hash"], total,
        datetime.strptime(chosen["value_date"], "%Y-%m-%d").date(),
        shares, currency=chosen["currency"],
    )
    add_split_entry(paths.ledger, entry)
    click.echo(f"Split recorded: {', '.join(f'{s.person} owes {s.amount}' for s in shares)}")


@cli.command()
@click.argument("person")
def settle(person: str):
    """Mark everything a person owes (or is owed) as settled."""
    _, paths = _load_all()
    count = settle_person(paths.ledger, person)
    if count == 0:
        click.echo(f"No unsettled entries for {person}.")
    else:
        click.echo(f"Settled {count} entr{'y' if count == 1 else 'ies'} for {person}.")


@cli.command()
def doctor():
    """Check that everything this tool needs is set up, and how to fix what isn't."""
    from src.doctor import run_doctor

    config, _ = _load_all()
    checks = run_doctor(config, PROJECT_DIR)
    failed = False
    for check in checks:
        mark = "OK " if check.ok else "FAIL"
        click.echo(f"[{mark}] {check.name}")
        if not check.ok:
            failed = True
            for line in check.detail.splitlines():
                click.echo(f"       {line}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    cli()
