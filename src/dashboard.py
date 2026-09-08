"""Excel dashboard generator: six sheets per currency.

The workbook is a generated *view* of transactions.csv and ledger.json — it
is never the source of truth and can be regenerated at any time.

Guard rails throughout: openpyxl will happily emit a table whose range holds
only a header row, or a chart pointing at an empty range, and Excel then
reports the file as corrupt. Every table and chart is written only when at
least one data row exists behind it.
"""

from decimal import Decimal
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from src.insights import (
    compute_month_over_month_deltas,
    compute_top_vendors,
    detect_recurring_charges,
)
from src.ledger import get_outstanding_by_person

SHEET_ORDER = [
    "Dashboard", "Transactions", "Category Summary", "Trends",
    "Lend & Borrow Ledger", "Insights",
]

_TABLE_COUNTER = {"n": 0}


def _sheet_name(base: str, currency: str, multi: bool) -> str:
    return f"{base} {currency}" if multi else base


def _add_table(ws, last_row: int, last_col: int) -> None:
    if last_row < 2:
        return
    _TABLE_COUNTER["n"] += 1
    table = Table(
        ref=f"A1:{get_column_letter(last_col)}{last_row}",
        displayName=f"Table{_TABLE_COUNTER['n']}",
    )
    # A fresh style object per table: sharing one instance across sheets
    # produces a malformed workbook.
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
    ws.add_table(table)


def _write_df(ws, df: pd.DataFrame, start_row: int = 1) -> int:
    """Write a DataFrame with headers starting at start_row; returns last row."""
    for j, col in enumerate(df.columns, start=1):
        ws.cell(row=start_row, column=j, value=str(col)).font = Font(bold=True)
    row = start_row
    for _, record in df.iterrows():
        row += 1
        for j, value in enumerate(record, start=1):
            if isinstance(value, (pd.Timestamp,)):
                value = str(value)
            if pd.isna(value):
                value = ""
            ws.cell(row=row, column=j, value=value)
    return row


def _pie(ws, anchor, data_ref, labels_ref, title):
    chart = PieChart()
    chart.title = title
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(labels_ref)
    ws.add_chart(chart, anchor)


def _bar(ws, anchor, data_ref, labels_ref, title, horizontal=False):
    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.title = title
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(labels_ref)
    ws.add_chart(chart, anchor)


def _line(ws, anchor, data_ref, labels_ref, title):
    chart = LineChart()
    chart.title = title
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(labels_ref)
    ws.add_chart(chart, anchor)


def _write_block(ws, top_row: int, headers: tuple[str, str], pairs) -> int:
    """Two-column label/value block for a chart to reference; returns last row.
    The header row is included so titles_from_data can name the series."""
    ws.cell(row=top_row, column=1, value=headers[0])
    ws.cell(row=top_row, column=2, value=headers[1])
    row = top_row
    for i, (label, value) in enumerate(pairs, start=1):
        row = top_row + i
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=float(value))
    return row


def _dashboard_sheet(wb, name, month_df, full_df, ledger_entries, currency, current_month):
    ws = wb.create_sheet(name)
    ws["A1"] = f"Expense Dashboard — {current_month} ({currency})"
    ws["A1"].font = Font(bold=True, size=14)

    debits = month_df[month_df["direction"] == "debit"]
    by_category = debits.groupby("category")["amount"].sum().sort_values(ascending=False)
    row = 3
    if not by_category.empty:
        last = _write_block(ws, row, ("Category", "Spend"), by_category.items())
        _pie(
            ws, "D3",
            Reference(ws, min_col=2, min_row=row, max_row=last),
            Reference(ws, min_col=1, min_row=row + 1, max_row=last),
            f"Spend by category — {current_month}",
        )
        row = last + 2

    monthly = (
        full_df[full_df["direction"] == "debit"]
        .groupby("source_month")["amount"].sum().sort_index()
    )
    if not monthly.empty:
        last = _write_block(ws, row, ("Month", "Total spend"), monthly.items())
        _line(
            ws, "D20",
            Reference(ws, min_col=2, min_row=row, max_row=last),
            Reference(ws, min_col=1, min_row=row + 1, max_row=last),
            "Total spend per month",
        )
        row = last + 2

    top_vendors = compute_top_vendors(full_df, current_month)
    if not top_vendors.empty:
        last = _write_block(
            ws, row, ("Vendor", "Spend"),
            zip(top_vendors["vendor"], top_vendors["amount"]),
        )
        _bar(
            ws, "D37",
            Reference(ws, min_col=2, min_row=row, max_row=last),
            Reference(ws, min_col=1, min_row=row + 1, max_row=last),
            f"Top vendors — {current_month}",
        )
        row = last + 2

    outstanding = get_outstanding_by_person(ledger_entries)
    owed_to_you = sum((v["lent"] for v in outstanding.values()), Decimal("0"))
    owed_by_you = sum((v["borrowed"] for v in outstanding.values()), Decimal("0"))
    ws.cell(row=row + 1, column=1, value="Owed to you").font = Font(bold=True)
    ws.cell(row=row + 1, column=2, value=float(owed_to_you))
    ws.cell(row=row + 2, column=1, value="Owed by you").font = Font(bold=True)
    ws.cell(row=row + 2, column=2, value=float(owed_by_you))


def _transactions_sheet(wb, name, month_df):
    ws = wb.create_sheet(name)
    columns = [
        "value_date", "posted_at", "description", "vendor", "category",
        "subcategory", "amount", "direction", "balance", "flag",
    ]
    view = month_df[columns] if not month_df.empty else pd.DataFrame(columns=columns)
    last = _write_df(ws, view)
    _add_table(ws, last, len(columns))


def _category_summary_sheet(wb, name, month_df):
    ws = wb.create_sheet(name)
    debits = month_df[month_df["direction"] == "debit"]
    if debits.empty:
        ws["A1"] = "No spending this month."
        return
    total = debits["amount"].sum()
    summary = (
        debits.groupby(["category", "subcategory"], as_index=False)["amount"].sum()
        .sort_values("amount", ascending=False)
    )
    summary["% of month"] = (summary["amount"] / total * 100).round(1)
    last = _write_df(ws, summary)
    _add_table(ws, last, len(summary.columns))

    by_cat = debits.groupby("category")["amount"].sum().sort_values(ascending=False)
    block_start = last + 2
    block_last = _write_block(ws, block_start, ("Category", "Spend"), by_cat.items())
    _pie(
        ws, "F2",
        Reference(ws, min_col=2, min_row=block_start, max_row=block_last),
        Reference(ws, min_col=1, min_row=block_start + 1, max_row=block_last),
        "Category split",
    )


def _trends_sheet(wb, name, full_df):
    ws = wb.create_sheet(name)
    debits = full_df[full_df["direction"] == "debit"]
    if debits.empty:
        ws["A1"] = "No history yet."
        return
    matrix = (
        debits.pivot_table(
            index="source_month", columns="category", values="amount",
            aggfunc="sum", fill_value=0.0,
        )
        .sort_index()
        .reset_index()
    )
    last = _write_df(ws, matrix)
    _add_table(ws, last, len(matrix.columns))
    if last >= 3:  # at least two months: a trend line means something
        _line(
            ws, f"A{last + 3}",
            Reference(ws, min_col=2, min_row=1, max_col=len(matrix.columns), max_row=last),
            Reference(ws, min_col=1, min_row=2, max_row=last),
            "Monthly spend by category",
        )


def _ledger_sheet(wb, name, ledger_entries, currency, multi):
    ws = wb.create_sheet(name)
    relevant = [
        e for e in ledger_entries
        if not multi or (e.get("currency") or currency) == currency
    ]
    outstanding = get_outstanding_by_person(relevant)
    owed_to_you = sum((v["lent"] for v in outstanding.values()), Decimal("0"))
    owed_by_you = sum((v["borrowed"] for v in outstanding.values()), Decimal("0"))
    ws["A1"] = f"Owed to you: {owed_to_you}   Owed by you: {owed_by_you}"
    ws["A1"].font = Font(bold=True)

    rows = []
    for entry in relevant:
        if entry["type"] == "direct":
            rows.append({
                "Person": entry["person"], "Type": "Direct",
                "Direction": entry["direction"], "Amount": float(entry["amount"]),
                "Date": entry["entry_date"], "Status": entry["status"],
                "Linked": entry["linked_transaction_hash"][:12],
            })
        else:
            for share in entry["splits"]:
                rows.append({
                    "Person": share["person"], "Type": "Split", "Direction": "lent",
                    "Amount": float(share["amount"]), "Date": entry["entry_date"],
                    "Status": share["status"],
                    "Linked": entry["linked_transaction_hash"][:12],
                })
    if rows:
        df = pd.DataFrame(rows)
        for j, col in enumerate(df.columns, start=1):
            ws.cell(row=3, column=j, value=col).font = Font(bold=True)
        row = 3
        for _, record in df.iterrows():
            row += 1
            for j, value in enumerate(record, start=1):
                ws.cell(row=row, column=j, value=value)


def _insights_sheet(wb, name, full_df, current_month):
    ws = wb.create_sheet(name)
    ws["A1"] = "Top vendors"
    ws["A1"].font = Font(bold=True)
    last = _write_df(ws, compute_top_vendors(full_df, current_month), start_row=2)

    ws.cell(row=last + 2, column=1, value="Recurring charges").font = Font(bold=True)
    last = _write_df(ws, detect_recurring_charges(full_df), start_row=last + 3)

    ws.cell(row=last + 2, column=1, value="Month-over-month category change").font = Font(bold=True)
    deltas = compute_month_over_month_deltas(full_df)
    if not deltas.empty:
        deltas = deltas.copy()
        deltas["pct_change"] = deltas["pct_change"].round(1)
    _write_df(ws, deltas, start_row=last + 3)


def generate_dashboard(
    full_df: pd.DataFrame,
    ledger_entries: list[dict],
    output_path: Path,
    current_month: str,
) -> Path:
    """Write the workbook. One set of six sheets per currency in history —
    figures in different currencies are never added together."""
    _TABLE_COUNTER["n"] = 0
    wb = Workbook()
    wb.remove(wb.active)

    currencies = sorted(full_df["currency"].unique()) if not full_df.empty else ["---"]
    multi = len(currencies) > 1

    for currency in currencies:
        cur_df = full_df[full_df["currency"] == currency] if not full_df.empty else full_df
        month_df = cur_df[cur_df["source_month"] == current_month]
        _dashboard_sheet(
            wb, _sheet_name("Dashboard", currency, multi),
            month_df, cur_df, ledger_entries, currency, current_month,
        )
        _transactions_sheet(wb, _sheet_name("Transactions", currency, multi), month_df)
        _category_summary_sheet(wb, _sheet_name("Category Summary", currency, multi), month_df)
        _trends_sheet(wb, _sheet_name("Trends", currency, multi), cur_df)
        _ledger_sheet(
            wb, _sheet_name("Lend & Borrow Ledger", currency, multi),
            ledger_entries, currency, multi,
        )
        _insights_sheet(wb, _sheet_name("Insights", currency, multi), cur_df, current_month)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path
