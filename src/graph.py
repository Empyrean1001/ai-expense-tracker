"""The monthly pipeline as a LangGraph StateGraph.

Deliberately thin (decision D-12): every node wraps an injected plain
function, and the graph only encodes order plus one conditional edge — skip
everything when the statement contains no new transactions. Interactive
review runs as an ordinary blocking call inside its node; interrupt()/
checkpointer machinery earns its complexity only when a run must survive a
process restart, and this is a single local CLI invocation.
"""

from dataclasses import dataclass
from typing import Callable, TypedDict

from langgraph.graph import END, StateGraph

from src.models import DirectLedgerEntry, Transaction


class PipelineState(TypedDict, total=False):
    statement_path: str
    transactions: list[Transaction]
    new_transactions: list[Transaction]
    ledger_entries: list[DirectLedgerEntry]
    corrections: list[Transaction]
    current_month: str
    dashboard_path: str


@dataclass
class PipelineDeps:
    ingest_fn: Callable[[str], list[Transaction]]
    existing_hashes_fn: Callable[[], set[str]]
    categorize_fn: Callable[[list[Transaction]], None]
    review_fn: Callable[[list[Transaction]], tuple[list[DirectLedgerEntry], list[Transaction]]]
    persist_fn: Callable[[list[Transaction], list[DirectLedgerEntry], list[Transaction]], None]
    dashboard_fn: Callable[[str], str]
    echo_fn: Callable[[str], None] = print


def build_graph(deps: PipelineDeps):
    def ingest(state: PipelineState) -> PipelineState:
        return {"transactions": deps.ingest_fn(state["statement_path"])}

    def filter_new(state: PipelineState) -> PipelineState:
        seen = deps.existing_hashes_fn()
        new = [t for t in state["transactions"] if t.dedup_hash not in seen]
        skipped = len(state["transactions"]) - len(new)
        if skipped:
            deps.echo_fn(f"Skipping {skipped} already-imported transaction(s).")
        month = max((t.source_month for t in new), default="")
        return {"new_transactions": new, "current_month": month}

    def route_after_filter(state: PipelineState) -> str:
        if not state["new_transactions"]:
            deps.echo_fn("Nothing new to import.")
            return END
        return "categorize"

    def categorize(state: PipelineState) -> PipelineState:
        deps.categorize_fn(state["new_transactions"])
        return {}

    def review(state: PipelineState) -> PipelineState:
        entries, corrections = deps.review_fn(state["new_transactions"])
        return {"ledger_entries": entries, "corrections": corrections}

    def persist(state: PipelineState) -> PipelineState:
        deps.persist_fn(
            state["new_transactions"], state["ledger_entries"], state["corrections"]
        )
        return {}

    def dashboard(state: PipelineState) -> PipelineState:
        return {"dashboard_path": deps.dashboard_fn(state["current_month"])}

    graph = StateGraph(PipelineState)
    graph.add_node("ingest", ingest)
    graph.add_node("filter_new", filter_new)
    graph.add_node("categorize", categorize)
    graph.add_node("review", review)
    graph.add_node("persist", persist)
    graph.add_node("dashboard", dashboard)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "filter_new")
    graph.add_conditional_edges("filter_new", route_after_filter, {END: END, "categorize": "categorize"})
    graph.add_edge("categorize", "review")
    graph.add_edge("review", "persist")
    graph.add_edge("persist", "dashboard")
    graph.add_edge("dashboard", END)
    return graph.compile()
