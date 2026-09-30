"""Ask a natural-language question over stored results.
  python scripts/ask.py "how many shipments were flagged this week?"
"""
import _path  # noqa: F401
import sys

from nova.query import ask

q = " ".join(sys.argv[1:]) or "how many shipments were flagged this week?"
r = ask(q)
print(f"Q: {r['question']}\nA: {r['answer']}\n\nmethod: {r['method']}\nSQL: {r['sql']}")
if r["rows"]:
    print("columns:", r["columns"])
    for row in r["rows"][:20]:
        print("  ", row)
if r.get("note"):
    print("note:", r["note"])
