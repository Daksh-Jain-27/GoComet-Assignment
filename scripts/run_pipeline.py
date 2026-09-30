"""Run (or resume) the pipeline on one document from the command line.
  NOVA_CRASH_AT=validate python scripts/run_pipeline.py data/samples/invoice_clean.pdf   # crash
  python scripts/run_pipeline.py data/samples/invoice_clean.pdf                          # resumes
"""
import _path  # noqa: F401
import argparse
import json

from nova.graph import run_document
from nova.schemas import FIELD_LABELS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--customer", default="acme_electronics")
    ap.add_argument("--mode", choices=["offline", "llm"], default=None)
    ap.add_argument("--force", action="store_true", help="re-run even if this file was already processed")
    ap.add_argument("--json", action="store_true", help="print full final state as JSON")
    a = ap.parse_args()

    s = run_document(a.file, a.customer, mode=a.mode, force=a.force,
                     on_update=lambda node, _: print(f"  [done] {node}"))
    if a.json:
        print(json.dumps({k: v for k, v in s.items() if k != "doc"}, indent=2, default=str))
        return
    d, v = s["decision"], s["validation"]
    print(f"\nrun_id   : {s['run_id']}  (resumed={s.get('resumed')}, cached={s.get('cached')})")
    print(f"extractor: {s['extraction']['extractor']}  text_source={s['extraction'].get('text_source')}")
    print(f"doc_type : {s['extraction']['doc_type']}")
    print(f"{'field':22} {'status':10} {'conf':>5}  found  ->  expected")
    for c in v["checks"]:
        print(f"{FIELD_LABELS[c['name']]:22} {c['status']:10} {c['confidence']:5.2f}  {c['found']}  ->  {c['expected']}")
    print(f"\nDECISION : {d['action'].upper()}")
    print(f"REASONING: {d['reasoning']}")
    for t in d["policy_trace"]:
        print(f"  trace  : {t}")
    for t in d["cg_checklist"]:
        print(f"  CG todo: {t}")
    if d.get("amendment_draft"):
        print(f"\n--- draft ({d['drafted_by']}, NOT sent) ---\nSubject: {d['amendment_subject']}\n{d['amendment_draft']}")
    if s.get("errors"):
        print("\nERRORS  :", *s["errors"], sep="\n  - ")
    print(f"\ncost=${s.get('cost_usd', 0):.5f}  timings(ms)={s.get('timings_ms')}")


if __name__ == "__main__":
    main()
