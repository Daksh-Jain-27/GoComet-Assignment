import json
from types import SimpleNamespace

import pytest

from nova import llm as llm_mod
from nova.config import ROOT
from nova.graph import run_document
from nova.query import UnsafeSQL, ask, guard_sql

S = ROOT / "data" / "samples"
GOOD = {"consignee_name": "Acme Electronics Pvt Ltd", "hs_code": "8471.30.10",
        "port_of_loading": "Nhava Sheva, India", "port_of_discharge": "Rotterdam, Netherlands",
        "incoterms": "FOB Nhava Sheva", "description_of_goods": "Laptop computers, 14-inch, model VX-14 (1,200 units)",
        "gross_weight": "12,450.50 KG", "invoice_number": "INV-2026-0142"}


def extraction(values: dict, conf=0.95, evidence=None):
    return {"doc_type": "commercial_invoice",
            "fields": {k: {"value": v, "evidence": (evidence or {}).get(k, v), "page": 1, "confidence": conf}
                       for k, v in values.items()}}


class FakeLiteLLM:
    def __init__(self, script: dict, cost=0.002, fail_nodes=()):
        self.script, self.cost, self.fail_nodes, self.calls = script, cost, set(fail_nodes), []

    def completion(self, **kw):
        node = kw["metadata"]["generation_name"]
        self.calls.append(node)
        if node in self.fail_nodes:
            raise ConnectionError("provider down")
        payload = self.script[node]
        payload = payload(kw) if callable(payload) else payload
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
                               usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200))

    def completion_cost(self, completion_response=None):
        return self.cost


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeLiteLLM(**kw)
        monkeypatch.setattr(llm_mod, "_setup_litellm", lambda: f)
        monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)
        return f
    return install


def run(name, **kw):
    return run_document(str(S / name), "acme_electronics", mode="llm", force=True, **kw)


def test_clean_invoice_auto_approves(fake):
    fake(script={"extract": extraction(GOOD)})
    s = run("invoice_clean.pdf")
    assert s["decision"]["action"] == "auto_approve"


def test_hallucinated_value_is_caught_by_grounding(fake):
    # model invents an HS code that is NOT printed on the document
    fake(script={"extract": extraction({**GOOD, "hs_code": "8471.41.00"}),
                 "refine": extraction({"hs_code": "8471.41.00"})})
    s = run("invoice_clean.pdf")
    hs = next(f for f in s["extraction"]["fields"] if f["name"] == "hs_code")
    assert hs["signals"]["grounded"] is False and hs["confidence"] <= 0.25
    assert s["decision"]["action"] != "auto_approve"          # never silently approved


def test_low_confidence_never_auto_approves_even_if_rule_matches(fake):
    fake(script={"extract": extraction(GOOD, conf=0.6), "refine": extraction(GOOD, conf=0.6)})
    s = run("invoice_clean.pdf")
    assert s["decision"]["action"] == "human_review"


def test_scan_refine_agreement_raises_confidence_but_policy_keeps_cg_in_loop(fake):
    f = fake(script={"extract": extraction(GOOD, conf=0.8), "refine": extraction(GOOD, conf=0.95)})
    s = run("invoice_clean_scan.jpg")
    assert "refine" in f.calls
    refined = [x for x in s["extraction"]["fields"] if "second_read" in x["signals"]]
    assert refined and all(x["signals"]["agreement"] and x["confidence"] >= 0.85 for x in refined)
    # values are right, but a scan can't be grounded -> customer policy requires a CG look
    assert s["decision"]["action"] == "human_review"
    assert any("Rule 3a" in t for t in s["decision"]["policy_trace"])


def test_refine_disagreement_lowers_confidence(fake):
    fake(script={"extract": extraction(GOOD, conf=0.8),
                 "refine": extraction({**GOOD, "hs_code": "8473.30.00"}, conf=0.9)})
    s = run("invoice_clean_scan.jpg")
    hs = next(f for f in s["extraction"]["fields"] if f["name"] == "hs_code")
    assert hs["confidence"] <= 0.40 and hs["signals"]["agreement"] is False
    assert s["decision"]["action"] != "auto_approve"


def test_llm_outage_falls_back_loudly(fake):
    fake(script={}, fail_nodes={"extract", "refine", "route_draft"})
    s = run("invoice_errors.pdf")
    assert any("fell back" in e for e in s["errors"])
    assert s["decision"]["action"] == "amendment" and s["decision"]["drafted_by"] == "template"


def test_draft_that_omits_a_discrepancy_is_rejected(fake):
    bad = {"subject": "Fix", "body": "Please fix the Incoterms (CIF Rotterdam)."}   # omits HS code
    fake(script={"extract": extraction({**GOOD, "incoterms": "CIF Rotterdam", "hs_code": "8528.72.00"}),
                 "route_draft": bad})
    s = run("invoice_errors.pdf")
    assert s["decision"]["drafted_by"] == "template"
    assert "HS code" in s["decision"]["amendment_draft"]
    assert any("omitted" in e for e in s["errors"])


def test_budget_blocks_extra_calls(fake, monkeypatch):
    f = fake(script={"extract": extraction(GOOD, conf=0.6), "refine": extraction(GOOD, conf=0.6)}, cost=1.0)
    s = run("invoice_clean.pdf")
    assert "refine" not in f.calls            # budget spent by first call -> no refinement
    assert s["decision"]["action"] == "human_review"


def test_sql_guard_blocks_writes():
    for bad in ["DELETE FROM documents", "SELECT 1; DROP TABLE documents", "PRAGMA table_info(documents)",
                "UPDATE documents SET decision='auto_approve'"]:
        with pytest.raises(UnsafeSQL):
            guard_sql(bad)


def test_offline_query_is_grounded():
    run_document(str(S / "invoice_errors.pdf"), "acme_electronics", mode="offline", force=True)
    r = ask("how many shipments were flagged this week?", mode="offline")
    assert r["sql"] and r["rows"][0][0] >= 1 and str(r["rows"][0][0]) in r["answer"]


def test_llm_query_shows_sql_and_rows(fake):
    run_document(str(S / "invoice_errors.pdf"), "acme_electronics", mode="offline", force=True)
    fake(script={"nl2sql": {"sql": "SELECT COUNT(*) AS n FROM documents WHERE decision='amendment'"},
                 "nl_answer": lambda kw: {"answer": "Rows say " + kw["messages"][1]["content"].split("Rows")[1][:40]}})
    r = ask("how many amendments?", mode="llm")
    assert r["method"] == "llm" and r["columns"] == ["n"] and r["rows"][0][0] >= 1


def test_llm_generated_write_query_is_refused(fake):
    fake(script={"nl2sql": {"sql": "DELETE FROM documents"}})
    r = ask("delete everything", mode="llm")
    assert r["rows"] == [] and "Could not produce a valid query" in r["answer"]

def test_same_model_agreement_is_not_enough(fake, monkeypatch):
    # primary is down -> fallback extracts -> refine uses the SAME fallback model
    f = fake(script={"extract": extraction(GOOD, conf=0.95), "refine": extraction(GOOD, conf=0.95)})
    real = f.completion
    def completion(**kw):
        if kw["model"] != "fake/strong-vision" and kw["metadata"]["generation_name"] == "extract":
            raise ConnectionError("primary down")
        return real(**kw)
    f.completion = completion
    s = run("invoice_errors_scan.jpg")
    assert "fake/strong-vision (refine)" in s["extraction"]["extractor"]
    assert all(x["confidence"] < 0.85 for x in s["extraction"]["fields"] if x["value"] is not None)
    assert s["decision"]["action"] == "human_review"

def test_review_surfaces_likely_violations_first_with_provisional_draft(fake):
    bad = {**GOOD, "incoterms": "CIF Rotterdam", "hs_code": "8528.72.00"}
    fake(script={"extract": extraction(bad, conf=0.6), "refine": extraction(bad, conf=0.6)})
    s = run("invoice_errors.pdf")
    d = s["decision"]
    assert d["action"] == "human_review"                       # still not confident
    assert "LIKELY PROBLEMS" in d["reasoning"]
    assert d["cg_checklist"][0].startswith("Check first")      # likely problems listed first
    assert d["drafted_by"] == "template (provisional)"
    assert "HS code" in d["amendment_draft"] and "Incoterms" in d["amendment_draft"]

