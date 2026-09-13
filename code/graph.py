"""LangGraph workflow: shared data layer, then one request at a time."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from assemble import assemble_context, resolve_request
from decide import decide_context
from facts import apply_facts_to_context
from ledger import build_ledger
from load import load_indexes
from llm_gemini import load_env
from models import DataIndexes, Request, RequestContext
from ocr_fill import fill_blank_amounts, fill_unresolved_with_gemini
from persist import persist_contexts, persist_indexes, persist_one_context
from verify import output_path_for_mode, verify_and_write, write_output


class PipelineState(TypedDict, total=False):
    repo_root: str
    mode: Literal["eval", "samples", "one"]
    request_id: str
    indexes: DataIndexes
    queue: list[str]
    total: int
    contexts: list[RequestContext]
    rows: list[dict[str, str]]
    stats: dict[str, Any]


def load_indexes_node(state: PipelineState) -> PipelineState:
    repo = Path(state["repo_root"])
    load_env(repo)
    indexes = load_indexes(repo)
    indexes_path = persist_indexes(indexes)
    return {
        "indexes": indexes,
        "stats": {
            "loaded": indexes.stats(),
            "indexes_file": str(indexes_path),
        },
    }


def _requests_for_mode(state: PipelineState) -> list[Request]:
    indexes = state["indexes"]
    mode = state.get("mode", "eval")
    if mode == "samples":
        return indexes.sample_requests
    if mode == "one":
        return [resolve_request(indexes, state["request_id"])]
    return indexes.requests


def fill_images_node(state: PipelineState) -> PipelineState:
    """Shared once: fill blank event amounts before any request is decided."""
    indexes = state["indexes"]
    ocr_stats = fill_blank_amounts(indexes, [])
    gemini_stats = fill_unresolved_with_gemini(indexes, [])
    requests = _requests_for_mode(state)
    stats = dict(state.get("stats") or {})
    stats.update(ocr_stats)
    stats.update(gemini_stats)
    stats["data_layer_dir"] = str(indexes.repo_root / "code" / "data_layer")
    stats["blank_amount_events_after_ocr"] = sum(
        1 for event in indexes.events_by_id.values() if event.amount is None
    )
    stats["queued"] = len(requests)
    return {
        "queue": [item.request_id for item in requests],
        "total": len(requests),
        "contexts": [],
        "rows": [],
        "stats": stats,
    }


def process_one_node(state: PipelineState) -> PipelineState:
    """Assemble → filter → ledger → rank for a single request, then the next."""
    indexes = state["indexes"]
    queue = list(state.get("queue") or [])
    request_id = queue.pop(0)
    request = resolve_request(indexes, request_id)
    context = assemble_context(indexes, request)
    fact_stats = apply_facts_to_context(context, indexes.repo_root)
    ledger = build_ledger(context, indexes)
    row = decide_context(context, ledger)
    persist_one_context(indexes, context)

    rows = list(state.get("rows") or [])
    rows.append(row)
    contexts = list(state.get("contexts") or [])
    contexts.append(context)
    write_output(output_path_for_mode(indexes.repo_root, state.get("mode", "eval")), rows)
    done = len(rows)
    total = int(state.get("total") or done)
    print(
        f"[{done}/{total}] {request_id} {row['affordability_status']} "
        f"{row['recommended_payment_method']} safe={row['amount_safe_to_pay']}",
        flush=True,
    )

    stats = dict(state.get("stats") or {})
    stats["assembled"] = done
    stats["message_facts"] = int(stats.get("message_facts") or 0) + int(fact_stats.get("message_facts") or 0)
    stats["message_llm_calls"] = int(stats.get("message_llm_calls") or 0) + int(
        fact_stats.get("message_llm_calls") or 0
    )
    stats["message_cache_hits"] = int(stats.get("message_cache_hits") or 0) + int(
        fact_stats.get("message_cache_hits") or 0
    )
    stats["message_llm_failures"] = int(stats.get("message_llm_failures") or 0) + int(
        fact_stats.get("message_llm_failures") or 0
    )
    stats["ledgers"] = done
    stats["decided"] = done
    stats["last_request_id"] = request_id
    return {
        "queue": queue,
        "contexts": contexts,
        "rows": rows,
        "stats": stats,
    }


def route_after_request(state: PipelineState) -> Literal["process_one", "verify"]:
    return "process_one" if state.get("queue") else "verify"


def verify_node(state: PipelineState) -> PipelineState:
    contexts = state.get("contexts") or []
    persist_contexts(state["indexes"], contexts)
    result = verify_and_write(
        state["indexes"],
        contexts,
        state.get("rows") or [],
        state.get("mode", "eval"),
        run_stats=state.get("stats") or {},
    )
    stats = dict(state.get("stats") or {})
    stats.update({key: value for key, value in result.items() if key != "sample_score"})
    if result.get("sample_score"):
        stats["sample_score"] = result["sample_score"]["matches"]
        stats["sample_mismatch_count"] = len(result["sample_score"]["mismatches"])
        stats["sample_mismatches"] = result["sample_score"]["mismatches"][:12]
    return {"stats": stats}


def build_graph():
    workflow = StateGraph(PipelineState)
    workflow.add_node("load_indexes", load_indexes_node)
    workflow.add_node("fill_images", fill_images_node)
    workflow.add_node("process_one", process_one_node)
    workflow.add_node("verify", verify_node)

    workflow.add_edge(START, "load_indexes")
    workflow.add_edge("load_indexes", "fill_images")
    workflow.add_edge("fill_images", "process_one")
    workflow.add_conditional_edges(
        "process_one",
        route_after_request,
        {"process_one": "process_one", "verify": "verify"},
    )
    workflow.add_edge("verify", END)
    return workflow.compile()


PIPELINE = build_graph()
