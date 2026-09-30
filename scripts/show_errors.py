"""Show recent failed LLM calls with the full error text.
  python scripts/show_errors.py            # last 5 failures
  python scripts/show_errors.py 10         # last 10
  python scripts/show_errors.py 5 flash    # only models whose name contains 'flash'
"""
import _path  # noqa: F401
import sqlite3
import sys

from nova.config import SETTINGS

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
model_filter = f"%{sys.argv[2]}%" if len(sys.argv) > 2 else "%"

conn = sqlite3.connect(SETTINGS.db_path)
rows = conn.execute(
    "SELECT created_at, node, model, error FROM llm_calls "
    "WHERE ok = 0 AND model LIKE ? ORDER BY id DESC LIMIT ?",
    (model_filter, limit),
).fetchall()

if not rows:
    print("No failed LLM calls found.")
for created_at, node, model, error in rows:
    print(f"--- {created_at}  node={node}  model={model}")
    print(error)
    print()