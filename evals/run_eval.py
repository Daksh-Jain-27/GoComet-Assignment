"""OFFLINE EVAL on the labelled sample set (ground truth from generate_samples.py).

Measures, per run:
  - field accuracy (canonicalised: HS digits, UN/LOCODE, Incoterm code, weight+unit, normalised names)
  - hallucination rate : value returned where ground truth is null
  - miss rate          : null returned where ground truth has a value
  - calibration        : accuracy per confidence bucket (is 0.9 really ~90% right?)
  - decision accuracy  : decision in the document's acceptable set
  - FALSE AUTO-APPROVE : auto_approve on a document with planted errors / not acceptable  <- must be 0
  - cost and latency per document

Uses a separate DB so eval runs don't pollute production metrics.
  python evals/run_eval.py                 # current NOVA_MODE
  python evals/run_eval.py --mode llm
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("NOVA_DB_PATH", str(ROOT / "data" / "eval.db"))
os.environ.setdefault("NOVA_CHECKPOINT_PATH", str(ROOT / "data" / "eval_checkpoints.db"))
sys.path.insert(0, str(ROOT))

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import datetime  # noqa: E402

from rapidfuzz import fuzz  # noqa: E402

from nova.config import SETTINGS  # noqa: E402
from nova.graph import run_document  # noqa: E402
from nova.rules import canonical  # noqa: E402
from nova.schemas import FIELD_NAMES  # noqa: E402

BUCKETS = [(0.0, 0.5), (0.5, 0.85), (0.85, 0.95), (0.95, 1.01)]


def same(field, pred, gold) -> bool:
    if pred is None or gold is None:
        return pred is None and gold is None
    a, b = canonical(field, pred), canonical(field, gold)
    if field == "description_of_goods":
        return fuzz.token_set_ratio(a, b) >= 90
    return a == b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offline", "llm"], default=SETTINGS.mode)
    ap.add_argument("--dir", default=str(SETTINGS.samples_dir))
    a = ap.parse_args()

    truths = sorted(Path(a.dir).glob("*.truth.json"))
    per_doc, per_field = [], {f: [0, 0] for f in FIELD_NAMES}
    buckets = {b: [0, 0] for b in BUCKETS}
    halluc = misses = present = absent = 0
    false_auto = 0
    for tp in truths:
        t = json.loads(tp.read_text())
        doc = tp.parent / t["file"]
        s = run_document(str(doc), t["customer_id"], mode=a.mode, force=True)
        fields = {f["name"]: f for f in s["extraction"]["fields"]}
        correct = 0
        for name in FIELD_NAMES:
            gold, f = t["fields"].get(name), fields[name]
            ok = same(name, f["value"], gold)
            correct += ok
            per_field[name][0] += ok
            per_field[name][1] += 1
            for lo, hi in BUCKETS:
                if lo <= f["confidence"] < hi:
                    buckets[(lo, hi)][0] += ok
                    buckets[(lo, hi)][1] += 1
            if gold is None:
                absent += 1
                halluc += f["value"] is not None
            else:
                present += 1
                misses += f["value"] is None
        action = s["decision"]["action"]
        acceptable = t.get("acceptable_decisions") or [t["expected_decision"]]
        fa = action == "auto_approve" and "auto_approve" not in acceptable
        false_auto += fa
        per_doc.append({"file": t["file"], "decision": action, "acceptable": acceptable,
                        "decision_ok": action in acceptable, "false_auto_approve": fa,
                        "field_acc": round(correct / len(FIELD_NAMES), 3),
                        "cost_usd": round(s.get("cost_usd", 0.0), 5),
                        "latency_ms": round(sum(s.get("timings_ms", {}).values()), 1),
                        "extractor": s["extraction"]["extractor"], "notes": t.get("notes", "")})

    n = len(per_doc)
    total_fields = sum(v[1] for v in per_field.values())
    summary = {
        "mode": a.mode, "documents": n, "run_at": datetime.now().isoformat(timespec="seconds"),
        "field_accuracy": round(sum(v[0] for v in per_field.values()) / total_fields, 3),
        "hallucination_rate": round(halluc / absent, 3) if absent else None,
        "miss_rate": round(misses / present, 3) if present else None,
        "decision_accuracy": round(sum(d["decision_ok"] for d in per_doc) / n, 3),
        "false_auto_approvals": false_auto,
        "avg_cost_usd": round(sum(d["cost_usd"] for d in per_doc) / n, 5),
        "avg_latency_ms": round(sum(d["latency_ms"] for d in per_doc) / n, 1),
        "per_field_accuracy": {k: round(v[0] / v[1], 3) for k, v in per_field.items()},
        "calibration": {f"{lo:.2f}-{min(hi, 1):.2f}": {"n": v[1], "accuracy": round(v[0] / v[1], 3) if v[1] else None}
                        for (lo, hi), v in buckets.items()},
    }
    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out / f"eval_{a.mode}_{stamp}.json").write_text(json.dumps({"summary": summary, "documents": per_doc}, indent=2))

    md = [f"# Offline eval - mode `{a.mode}` - {summary['run_at']}", "",
          "| Metric | Value |", "|---|---|"]
    for k in ["documents", "field_accuracy", "hallucination_rate", "miss_rate", "decision_accuracy",
              "false_auto_approvals", "avg_cost_usd", "avg_latency_ms"]:
        md.append(f"| {k} | {summary[k]} |")
    md += ["", "## Calibration (accuracy per confidence bucket)", "", "| Confidence | n | Accuracy |", "|---|---|---|"]
    md += [f"| {k} | {v['n']} | {v['accuracy']} |" for k, v in summary["calibration"].items()]
    md += ["", "## Per field", "", "| Field | Accuracy |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in summary["per_field_accuracy"].items()]
    md += ["", "## Per document", "", "| File | Decision | Acceptable | OK | False auto-approve | Field acc | Notes |",
           "|---|---|---|---|---|---|---|"]
    md += [f"| {d['file']} | {d['decision']} | {', '.join(d['acceptable'])} | {'yes' if d['decision_ok'] else 'NO'} | "
           f"{'YES' if d['false_auto_approve'] else 'no'} | {d['field_acc']} | {d['notes']} |" for d in per_doc]
    (out / f"eval_{a.mode}_{stamp}.md").write_text("\n".join(md))
    (out / f"latest_{a.mode}.md").write_text("\n".join(md))
    print("\n".join(md))
    if false_auto:
        print(f"\n!! {false_auto} FALSE AUTO-APPROVAL(S) - this is a No-Go condition.")
        sys.exit(1)


if __name__ == "__main__":
    main()
