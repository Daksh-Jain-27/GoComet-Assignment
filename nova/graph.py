"""Orchestration: LangGraph StateGraph + SQLite checkpointer.

    ingest -> extract -> [refine]? -> validate -> route -> persist

- Agents communicate by STRUCTURED HANDOFF through one typed state object.
  Each node reads only the keys it needs and writes its own key; every value is
  a Pydantic-validated dict (schemas.py), so a malformed handoff fails at the boundary.
- The graph is a DAG. `refine` runs at most once (guarded by state['refined']),
  so loops are impossible; recursion_limit is a second backstop.
- CRASH RECOVERY: the checkpointer writes state after every node, keyed by
  thread_id = run_id. If the process dies in `validate`, re-running the same
  document resumes at `validate` - extraction (the expensive LLM call) is not
  repeated and the cost already spent stays in state, so the budget still holds.
  Test it:  NOVA_CRASH_AT=validate python scripts/run_pipeline.py <file>
            python scripts/run_pipeline.py <file>          # resumes
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from functools import lru_cache
from typing import Any, Callable, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from . import storage
from .agents import extractor, router, validator
from .config import SETTINGS
from .docio import file_id, load_document
from .llm import BudgetExceeded, LLMClient, LLMError
from .rules import load_rules
from .schemas import ExtractionResult, ValidationResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nova.graph")


class PipelineState(TypedDict, total=False):
    run_id: str
    doc_path: str
    doc_id: str
    customer_id: str
    mode: str                 # offline | llm
    created_at: Optional[str]
    rule_version: str
    doc: dict                 # ingest output
    extraction: dict          # ExtractionResult
    validation: dict          # ValidationResult
    decision: dict            # Decision
    status: str
    refined: bool
    cost_usd: float
    timings_ms: dict
    errors: list


def _node(name: str):
    def deco(fn: Callable[[PipelineState], dict]):
        def wrapped(state: PipelineState) -> dict:
            if os.getenv("NOVA_CRASH_AT") == name:            # for the crash-recovery demo
                raise RuntimeError(f"Simulated crash at node '{name}'")
            t0 = time.perf_counter()
            log.info("run=%s node=%s start", state.get("run_id"), name)
            update = fn(state)
            ms = (time.perf_counter() - t0) * 1000
            update["timings_ms"] = {**state.get("timings_ms", {}), name: round(ms, 1)}
            log.info("run=%s node=%s done %.0fms cost=$%.5f", state.get("run_id"), name, ms,
                     update.get("cost_usd", state.get("cost_usd", 0.0)))
            return update
        wrapped.__name__ = name
        return wrapped
    return deco


@_node("ingest")
def ingest(s: PipelineState) -> dict:
    doc = load_document(s["doc_path"], SETTINGS.runs_dir / s["run_id"], SETTINGS.render_dpi, SETTINGS.max_pages)
    return {"doc": doc, "rule_version": str(load_rules(s["customer_id"]).get("version", "")),
            "cost_usd": s.get("cost_usd", 0.0), "errors": s.get("errors", []), "refined": False}


@_node("extract")
def extract(s: PipelineState) -> dict:
    errors = list(s.get("errors", []))
    if s["mode"] != "llm":
        ex = extractor.text_layer_extract(s["doc_id"], s["doc"])
        return {"extraction": ex.model_dump(), "errors": errors}
    client = LLMClient(s["run_id"], s.get("cost_usd", 0.0))
    # Model routing: primary vision model, then the fallback model if the primary is down.
    models = [SETTINGS.extractor_model] + (
        [SETTINGS.fallback_model] if SETTINGS.fallback_model and SETTINGS.fallback_model != SETTINGS.extractor_model else [])
    ex, last_err = None, None
    for model in models:
        try:
            ex = extractor.llm_extract(s["doc_id"], s["doc"], client, model)
            if model != SETTINGS.extractor_model:
                errors.append(f"Primary model {SETTINGS.extractor_model} unavailable "
                              f"({type(last_err).__name__}); extracted with fallback {model}")
            break
        except BudgetExceeded as e:
            last_err = e
            break                                   # no money left: don't try another model
        except (LLMError, ValueError) as e:
            last_err = e
    if ex is None:
        # Fail loud, degrade safely: deterministic parser. On a scan this yields
        # nothing -> all 'uncertain' -> human review. Never a silent approval.
        msg = (f"Vision extraction failed on all models ({type(last_err).__name__}: {str(last_err)[:160]}); "
               f"fell back to text-layer parser")
        errors.append(msg)
        ex = extractor.text_layer_extract(s["doc_id"], s["doc"], extra_warning=msg)
    return {"extraction": ex.model_dump(), "cost_usd": client.spent_usd, "errors": errors}


def needs_refine(s: PipelineState) -> str:
    if s["mode"] != "llm" or s.get("refined") or not SETTINGS.fallback_model:
        return "validate"
    ex = ExtractionResult.model_validate(s["extraction"])
    thr = float(load_rules(s["customer_id"])["confidence_threshold"])
    low = extractor.low_confidence_fields(ex, thr)
    budget_left = SETTINGS.budget_usd_per_doc - s.get("cost_usd", 0.0)
    if low and budget_left > SETTINGS.budget_usd_per_doc * 0.25:
        return "refine"
    return "validate"


@_node("refine")
def refine(s: PipelineState) -> dict:
    errors = list(s.get("errors", []))
    ex = ExtractionResult.model_validate(s["extraction"])
    thr = float(load_rules(s["customer_id"])["confidence_threshold"])
    low = extractor.low_confidence_fields(ex, thr)
    client = LLMClient(s["run_id"], s.get("cost_usd", 0.0))
    try:
        doc_hi = load_document(s["doc_path"], SETTINGS.runs_dir / s["run_id"], SETTINGS.refine_dpi, SETTINGS.max_pages)
        ex = extractor.refine(s["doc_id"], ex, doc_hi, client, SETTINGS.fallback_model, low)
    except (LLMError, BudgetExceeded, ValueError) as e:
        errors.append(f"Refinement skipped ({type(e).__name__}); low-confidence fields stay uncertain")
    return {"extraction": ex.model_dump(), "refined": True, "cost_usd": client.spent_usd, "errors": errors}


@_node("validate")
def validate(s: PipelineState) -> dict:
    ex = ExtractionResult.model_validate(s["extraction"])
    return {"validation": validator.validate(ex, load_rules(s["customer_id"])).model_dump()}


@_node("route")
def route(s: PipelineState) -> dict:
    ex = ExtractionResult.model_validate(s["extraction"])
    val = ValidationResult.model_validate(s["validation"])
    rules = load_rules(s["customer_id"])
    client = LLMClient(s["run_id"], s.get("cost_usd", 0.0)) if s["mode"] == "llm" else None
    decision, errs = router.decide(ex, val, rules, client, SETTINGS.text_model if client else None)
    return {"decision": decision.model_dump(), "errors": list(s.get("errors", [])) + errs,
            "cost_usd": client.spent_usd if client else s.get("cost_usd", 0.0)}


@_node("persist")
def persist(s: PipelineState) -> dict:
    return {"status": storage.save_run(dict(s))}


@lru_cache(maxsize=1)
def get_graph():
    g = StateGraph(PipelineState)
    for name, fn in [("ingest", ingest), ("extract", extract), ("refine", refine),
                     ("validate", validate), ("route", route), ("persist", persist)]:
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "extract")
    g.add_conditional_edges("extract", needs_refine, {"refine": "refine", "validate": "validate"})
    g.add_edge("refine", "validate")
    g.add_edge("validate", "route")
    g.add_edge("route", "persist")
    g.add_edge("persist", END)
    conn = sqlite3.connect(SETTINGS.checkpoint_path, check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn))


def run_document(path: str, customer_id: str, *, mode: Optional[str] = None, force: bool = False,
                 created_at: Optional[str] = None,
                 on_update: Optional[Callable[[str, dict], Any]] = None) -> dict:
    """Run (or resume) the pipeline for one document. Returns the final state.

    Idempotency: run_id = sha256(file)[:16] + customer. Same file + customer, already
    completed -> stored result is returned without new LLM calls (unless force=True).
    Crash mid-run -> the pending checkpoint is resumed from the failed node.
    """
    mode = (mode or SETTINGS.mode).lower()
    doc_id = file_id(path)
    run_id = f"{doc_id}-{customer_id}-{mode}"
    if force:
        run_id += f"-{int(time.time() * 1000)}"
    graph = get_graph()
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": SETTINGS.recursion_limit}

    snapshot = graph.get_state(config)
    if snapshot.values and snapshot.next:
        log.info("run=%s resuming from checkpoint at %s", run_id, snapshot.next)
        inp = None
    elif snapshot.values and not snapshot.next:
        log.info("run=%s already completed; returning stored result (use force=True to re-run)", run_id)
        return {**snapshot.values, "resumed": False, "cached": True}
    else:
        inp = {"run_id": run_id, "doc_path": str(path), "doc_id": doc_id, "customer_id": customer_id,
               "mode": mode, "created_at": created_at, "cost_usd": 0.0, "timings_ms": {}, "errors": []}

    for chunk in graph.stream(inp, config, stream_mode="updates"):
        for node, update in chunk.items():
            if on_update:
                on_update(node, update)
    final = graph.get_state(config).values
    return {**final, "resumed": inp is None, "cached": False}
