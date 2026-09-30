"""Online metrics computed from the store (what the pilot dashboard shows).

North star  : First-pass resolution rate = share of CG-reviewed documents where CG
              accepted the agent's decision without overriding it.
Guardrail   : auto-approved documents later overridden by CG (target: 0).
Supporting  : decision mix, uncertain-field share, field correction rate,
              p50/p95 latency, cost per doc, LLM error rate, pipeline error count.
Offline eval metrics (accuracy, calibration, false auto-approve on labelled data)
live in evals/run_eval.py.
"""
from __future__ import annotations

from .storage import connect


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def _quantile(values: list[float], q: float, digits: int = 1):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))], digits)


def compute() -> dict:
    with connect(readonly=True) as c:
        docs = [dict(r) for r in c.execute("SELECT * FROM documents")]
        n_fields = c.execute("SELECT COUNT(*) FROM fields").fetchone()[0]
        n_unc = c.execute("SELECT COUNT(*) FROM fields WHERE status='uncertain'").fetchone()[0]
        corrections = c.execute("SELECT COUNT(*) FROM feedback WHERE action='correct_field'").fetchone()[0]
        calls = [dict(r) for r in c.execute("SELECT ok, latency_ms, cost_usd FROM llm_calls")]
    n = len(docs)
    reviewed = [d for d in docs if d["reviewer_action"]]
    accepted = sum(d["reviewer_action"] == "accepted" for d in reviewed)
    auto = [d for d in docs if d["decision"] == "auto_approve"]
    auto_overridden = sum(d["reviewer_action"] == "overridden" for d in auto)
    lat = [d["latency_ms"] for d in docs if d["latency_ms"] is not None]
    cost = [d["cost_usd"] for d in docs if d["cost_usd"] is not None]
    return {
        "documents": n,
        "north_star_first_pass_resolution_pct": _pct(accepted, len(reviewed)),
        "reviewed_documents": len(reviewed),
        "guardrail_auto_approved_then_overridden": auto_overridden,
        "auto_approve_pct": _pct(len(auto), n),
        "human_review_pct": _pct(sum(d["decision"] == "human_review" for d in docs), n),
        "amendment_pct": _pct(sum(d["decision"] == "amendment" for d in docs), n),
        "uncertain_field_pct": _pct(n_unc, n_fields),
        "field_corrections_by_cg": corrections,
        "latency_p50_ms": _quantile(lat, 0.5),
        "latency_p95_ms": _quantile(lat, 0.95),
        "cost_avg_usd": round(sum(cost) / len(cost), 5) if cost else None,
        "cost_p95_usd": _quantile(cost, 0.95, digits=5),
        "llm_calls": len(calls),
        "llm_error_pct": _pct(sum(1 for x in calls if not x["ok"]), len(calls)),
        "docs_with_pipeline_errors": sum(1 for d in docs if d["errors"] not in (None, "[]")),
    }
