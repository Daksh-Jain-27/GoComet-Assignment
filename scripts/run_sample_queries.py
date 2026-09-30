"""Run a fixed set of questions against the stored results and save the evidence
(question, method, SQL, rows, answer) to docs/sample_queries.md.
  python scripts/run_sample_queries.py            # uses NOVA_MODE from .env
  python scripts/run_sample_queries.py offline
"""
import _path  # noqa: F401
import sys
from datetime import datetime

from nova.config import ROOT, SETTINGS
from nova.query import ask

QUESTIONS = [
    "How many shipments were flagged this week?",
    "Show me everything pending review for Acme",
    "What are the most common mismatches this month?",
    "How many were auto-approved in the last 7 days?",
    "What is the average cost per document?",
    "Which documents had an HS code problem?",            # free-form: LLM mode only
    "How many documents needed an amendment last week?",
]

mode = sys.argv[1] if len(sys.argv) > 1 else SETTINGS.mode
out = [f"# Sample queries against stored output",
       f"Run {datetime.now():%Y-%m-%d %H:%M}, mode `{mode}`. Each answer is computed from the rows shown.", ""]
for q in QUESTIONS:
    r = ask(q, mode=mode)
    out += [f"## {q}", f"**Answer:** {r['answer']}", f"*Method: {r['method']}*", ""]
    if r["sql"]:
        out += ["```sql", r["sql"], "```"]
    if r["rows"]:
        out += ["| " + " | ".join(r["columns"]) + " |", "|" + "---|" * len(r["columns"])]
        out += ["| " + " | ".join(str(v) for v in row) + " |" for row in r["rows"][:10]]
        if len(r["rows"]) > 10:
            out.append(f"*... {len(r['rows']) - 10} more rows*")
    if r.get("note"):
        out.append(f"*Note: {r['note']}*")
    out.append("")
path = ROOT / "sample_queries.md"
path.write_text("\n".join(out), encoding="utf-8")
print(f"Wrote {path}")