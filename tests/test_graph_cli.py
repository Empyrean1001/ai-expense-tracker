from pathlib import Path

from click.testing import CliRunner

from src.doctor import run_doctor
from src.graph import PipelineDeps, build_graph
from tests.test_categorizer import make_txn


def make_deps(calls, transactions):
    return PipelineDeps(
        ingest_fn=lambda p: (calls.append("ingest"), transactions)[1],
        existing_hashes_fn=lambda: set(),
        categorize_fn=lambda txns: calls.append("categorize"),
        review_fn=lambda txns: (calls.append("review"), ([], []))[1],
        persist_fn=lambda txns, entries, corrections: calls.append("persist"),
        dashboard_fn=lambda month: (calls.append("dashboard"), "out.xlsx")[1],
        echo_fn=lambda *a: None,
    )


class TestGraph:
    def test_full_run_order(self):
        calls: list[str] = []
        txn = make_txn("SWIGGY*1", vendor="SWIGGY")
        result = build_graph(make_deps(calls, [txn])).invoke({"statement_path": "x.csv"})
        assert calls == ["ingest", "categorize", "review", "persist", "dashboard"]
        assert result["current_month"] == "2026-07"
        assert result["dashboard_path"] == "out.xlsx"

    def test_short_circuits_when_nothing_new(self):
        calls: list[str] = []
        txn = make_txn("SWIGGY*1", vendor="SWIGGY")
        deps = make_deps(calls, [txn])
        deps.existing_hashes_fn = lambda: {txn.dedup_hash}
        build_graph(deps).invoke({"statement_path": "x.csv"})
        assert calls == ["ingest"]  # everything downstream skipped


class TestDoctor:
    def test_dead_ollama_fails_with_fix_command(self, tmp_path):
        config = {
            "ollama": {"host": "http://localhost:1", "model": "qwen2.5:3b-instruct"},
            "paths": {"data_dir": "data"},
        }
        checks = run_doctor(config, tmp_path)
        by_name = {c.name.split(" ")[0]: c for c in checks}
        assert by_name["Python"].ok
        assert any(c.ok and "writable" in c.name for c in checks)
        ollama_check = [c for c in checks if not c.ok]
        assert len(ollama_check) == 1
        assert "ollama serve" in ollama_check[0].detail


class TestCli:
    def test_help_lists_commands(self):
        from track_expenses import cli

        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        for command in ("process", "split", "settle", "doctor"):
            assert command in result.output

    def test_settle_unknown_person(self, monkeypatch, tmp_path):
        import track_expenses

        class FakePaths:
            ledger = tmp_path / "ledger.json"

        monkeypatch.setattr(track_expenses, "_load_all", lambda: ({}, FakePaths()))
        result = CliRunner().invoke(track_expenses.cli, ["settle", "Nobody"])
        assert result.exit_code == 0
        assert "No unsettled entries" in result.output
